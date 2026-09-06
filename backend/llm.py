# -*- coding: utf-8 -*-
"""
llm.py — клиент основной модели (OpenAI-совместимый /v1).
Поддерживает провайдеры: llama.cpp (локальный), Ollama, любой OpenAI-совместимый API.
Провайдер выбирается из конфига (MAIN_PROVIDER) или per-world переопределением.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import AsyncGenerator, Optional

import httpx

from .config import get_config

from .logsetup import get_logger
from .retry import with_retries, is_transient

log = get_logger(__name__)

_client: Optional[httpx.AsyncClient] = None


def _retry_conf() -> tuple[int, float]:
    """(повторы, базовая задержка) из конфига — для стрима, где with_retries неприменим."""
    try:
        cfg = get_config()
        return max(0, int(getattr(cfg, "llm_retries", 2) or 0)), max(0.0, float(
            getattr(cfg, "llm_retry_backoff", 0.8) or 0.8))
    except Exception:
        return 0, 0.8


def _bg_retries() -> int:
    return _retry_conf()[0]


# Тело ответа провайдера попадает в текст ошибки (и дальше в лог/UI). Редко, но провайдер
# может вернуть эхо заголовка/ключа — наружу он обязан уйти замаскированным (правило 3).
_KEY_TOKEN_RE = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")
_BEARER_RE = re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._\-]{6,})")


def _safe_body(body, limit: int = 400) -> str:
    """Короткий фрагмент тела ответа без секретов (маска вместо ключей)."""
    try:
        text = body if isinstance(body, str) else str(body, "utf-8", "replace")
    except Exception:
        return "<не разбирается>"
    text = _KEY_TOKEN_RE.sub("***", text)
    text = _BEARER_RE.sub(lambda m: m.group(1) + "***", text)
    return text[:limit]


def _backoff_delay(attempt: int) -> float:
    import random
    base = _retry_conf()[1]
    return base * (2 ** max(0, attempt - 1)) * (1.0 + random.random() * 0.25)


def _timeout() -> httpx.Timeout:
    """Таймаут запроса к модели: общий из LLM_TIMEOUT (по умолчанию 300с — локальная 8B на
    длинном промпте считается долго), connect — жёсткий (сервис либо есть, либо его нет)."""
    try:
        total = float(getattr(get_config(), "llm_timeout", 300.0) or 300.0)
    except Exception:
        total = 300.0
    return httpx.Timeout(total, connect=10.0)


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=_timeout())
    return _client


async def close() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


def _provider() -> dict:
    """Эффективный провайдер основной модели (env-дефолт)."""
    return get_config().get_provider("main")


async def probe(provider: dict) -> tuple[bool, bool, str]:
    """ЕДИНСТВЕННАЯ точка истины «жива ли модель»: GET {base_url}/models (таймаут 8 с).

    Возвращает (up, needs_key, detail):
      * up        — сервер ОТВЕТИЛ (< 500). 401/403/404 тоже «жив»: llama.cpp с --api-key
                    и облачные шлюзы отвечают так на служебный /models, а ход при этом
                    работает нормально — считать такое «сервер мёртв» нельзя (A12, аудит 41);
      * needs_key — ответ требует ключа (401/403): отдельно от «не отвечает», чтобы UI
                    подсказал «проверь MAIN_API_KEY», а не «запусти llama.cpp»;
      * detail    — короткая строка для диагностики (код ответа или имя исключения).

    Ключ ОТПРАВЛЯЕМ всегда, когда он есть: без `Authorization` защищённый сервер даёт 401,
    и «проверка без ключа» вракала о недоступности ровно там, где ключ включили.
    """
    base = (provider.get("base_url") or "").strip().rstrip("/")
    if not base:
        return False, False, "base_url пуст"
    headers = {}
    if provider.get("api_key"):
        headers["Authorization"] = f"Bearer {provider['api_key']}"
    try:
        r = await _get_client().get(f"{base}/models", headers=headers, timeout=8)
    except Exception as e:
        return False, False, f"{type(e).__name__}"
    return (r.status_code < 500), r.status_code in (401, 403), str(r.status_code)


async def check_available(provider: dict) -> bool:
    """Доступность провайдера — то же, что `probe` (одна точка истины, без второго критерия)."""
    return (await probe(provider))[0]


# ── Авто-детект реального размера контекста модели (сессия 33) ──────────────
# Защита от обрезов (жёсткое правило 4 в AGENT.md): стандарт .env — CONTEXT_TOKENS=32768
# под облачные модели, а локальная llama.cpp ходит с n_ctx 8192. Если мир заявляет окно
# больше, чем модель потянет, ход обрезается молча. Провайдеры отдают лимит по-разному,
# поэтому пробуем несколько источников и кэшируем ответ на жизнь процесса.
_ctx_cache: dict[tuple[str, str], Optional[dict]] = {}

# Поля, в которых разные OpenAI-совместимые шлюзы прячут лимит контекста,
# от большему к меньшему (first int wins).
_CTX_FIELDS = ("context_length", "max_context_tokens", "n_ctx", "context_window",
               "max_input_tokens", "max_model_len", "context")


def _first_int(obj, fields) -> Optional[int]:
    if not isinstance(obj, dict):
        return None
    for f in fields:
        v = obj.get(f)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) and int(v) > 0:
            return int(v)
        if isinstance(v, str) and v.isdigit() and int(v) > 0:
            return int(v)
    return None


async def probe_context(provider: dict) -> Optional[dict]:
    """Узнать реальный лимит контекста модели. Возвращает `{"max_context": int, "source": str}`
    или None (не известно / сервис недоступен / провайдер «выключен»). НЕ бросает и НЕ виснет:
    короткие таймауты, результат кэшируется по (base_url, model).

    Источники (в порядке надёжности для конкретного провайдера):
      * llamacpp — нет-OpenAI эндпоинт `/props` (`default_generation_settings.n_ctx`) —
        это фактический n_ctx запущенного сервера, а не максимум весов;
      * ollama   — `POST /api/show` (`model_info["general.architecture.context_length"]`);
      * все OpenAI-совместимые (включая облачные) — запись нужной модели в `GET /models`.
    """
    if not provider or not provider.get("base_url") or provider.get("id") in ("none", ""):
        return None
    base = str(provider["base_url"]).rstrip("/")
    model = str(provider.get("model") or "")
    key = (base, model)
    if key in _ctx_cache:
        return _ctx_cache[key]
    headers = {}
    if provider.get("api_key"):
        headers["Authorization"] = f"Bearer {provider['api_key']}"
    out: Optional[dict] = None
    client = _get_client()

    # 1) llama.cpp: /props отдаёт живой n_ctx (базу режем до origin)
    if provider.get("id") == "llamacpp":
        try:
            origin = re.sub(r"/v\d?/?$", "", base)
            r = await client.get(f"{origin}/props", headers=headers, timeout=6)
            if r.status_code == 200:
                d = r.json()
                n = _first_int(d, ("n_ctx", "n_ctx_train")) or _first_int(
                    d.get("default_generation_settings") or {}, ("n_ctx",))
                if n:
                    out = {"max_context": int(n), "source": "llamacpp:/props"}
        except Exception as e:
            log.debug("probe_context /props (%s): %s", base, e)

    # 2) Ollama: /api/show
    if out is None and provider.get("id") == "ollama":
        try:
            origin = re.sub(r"/v1/?$", "", base)
            r = await client.post(f"{origin}/api/show", json={"model": model},
                                  headers=headers, timeout=6)
            if r.status_code == 200:
                d = r.json()
                info = d.get("model_info") or {}
                # ключи вида "general.architecture": "llama" и "general.context_length": 8192
                n = next((int(v) for k, v in info.items()
                          if str(k).endswith("context_length") and isinstance(v, (int, float))
                          and int(v) > 0), None)
                if not n:
                    n = _first_int(d.get("parameters") or {}, ("num_ctx", "context_length"))
                if n:
                    out = {"max_context": int(n), "source": "ollama:/api/show"}
        except Exception as e:
            log.debug("probe_context /api/show (%s): %s", base, e)

    # 3) OpenAI-совместимый каталог моделей
    if out is None:
        try:
            r = await client.get(f"{base}/models", headers=headers, timeout=8)
            if r.status_code == 200:
                items = (r.json() or {}).get("data") or []
                want = model.lower()
                hit = None
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    if want and str(it.get("id", "")).lower() == want:
                        hit = it
                        break
                if hit is None and len(items) == 1 and isinstance(items[0], dict):
                    hit = items[0]  # сервер отдаёт одну модель — она и есть наша
                n = _first_int(hit, _CTX_FIELDS)
                if n:
                    out = {"max_context": int(n), "source": "openai:/models"}
        except Exception as e:
            log.debug("probe_context /models (%s): %s", base, e)

    _ctx_cache[key] = out
    return out


def probe_context_cached(provider: dict) -> Optional[dict]:
    """То же, но только из кэша (для синхронных мест — формат состояния, UI-подсказки)."""
    if not provider or not provider.get("base_url"):
        return None
    return _ctx_cache.get((str(provider["base_url"]).rstrip("/"), str(provider.get("model") or "")))


def _payload(messages: list[dict], temperature: float, max_tokens: int, top_p: float,
             stream: bool, provider: dict, tools: list | None = None,
             tool_choice: str | None = None) -> dict:
    payload: dict = {
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "top_p": top_p,
        "stream": stream,
    }
    if provider.get("model"):
        payload["model"] = provider["model"]
    if tools:
        payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice
    return payload


def _collect_tool_calls(message: dict, out: list[dict] | None) -> None:
    """Достаёт tool_calls (имя + аргументы-JSON-строка) из финального сообщения."""
    if out is None:
        return
    for tc in (message.get("tool_calls") or []):
        fn = tc.get("function") or {}
        out.append({"name": fn.get("name", ""),
                    "arguments": fn.get("arguments", "") or ""})


async def complete(messages: list[dict], temperature: float = 0.8, max_tokens: int = 2000,
                   top_p: float = 0.95, provider: dict | None = None,
                   tools: list | None = None, tool_choice: str | None = None,
                   tool_calls_out: list[dict] | None = None,
                   finish_out: dict | None = None) -> str:
    """Один тарелочный проход к модели.

    `finish_out` (если передан dict) дополняется служебными сведениями ответа:
    finish_reason / usage (prompt_tokens, completion_tokens) — нужно для метрики
    «ответ обрезан лимитом» (E3), которую иначе по одному тексту не увидеть.
    Временные сбои (429/5xx/таймаут) повторяются по LLM_RETRIES (сессия 34, B2).
    """
    prov = provider or _provider()
    url = f"{prov['base_url']}/chat/completions"
    headers = {"Authorization": f"Bearer {prov['api_key']}"} if prov.get("api_key") else {}
    payload = _payload(messages, temperature, max_tokens, top_p, False, prov, tools=tools, tool_choice=tool_choice)

    async def _attempt():
        resp = await _get_client().post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(f"LLM HTTP {resp.status_code} ({prov['id']}): {_safe_body(resp.text)}")
        return resp

    resp = await with_retries(_attempt, what=f"LLM complete ({prov['id']})")
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(f"LLM error ({prov['id']}): {data['error']}")
    if finish_out is not None:
        _capture_finish(data, finish_out)
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    _collect_tool_calls(message, tool_calls_out)
    return (message.get("content") or "") if message else ""


def _capture_finish(data: dict, out: dict) -> None:
    """Достаёт из ответа OpenAI-совместимого API finish_reason и usage (в поля `out`)."""
    try:
        ch = (data.get("choices") or [{}])[0]
        if ch.get("finish_reason"):
            out["finish_reason"] = str(ch["finish_reason"])
        u = data.get("usage") or {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if isinstance(u.get(key), int):
                out[key] = u[key]
    except Exception as e:      # служебная информация — не роняет ход
        log.debug("finish_out: не разобран ответ модели: %s", e)


async def stream_chat(messages: list[dict], temperature: float = 0.8, max_tokens: int = 2000,
                      top_p: float = 0.95, provider: dict | None = None,
                      tools: list | None = None, tool_choice: str | None = None,
                      tool_calls_out: list[dict] | None = None,
                      finish_out: dict | None = None) -> AsyncGenerator[str, None]:
    """Потоковая генерация: отдаёт дельты текста. SSE из провайдера → [DONE].
    tools — если заданы, дельты tool_calls накапливаются в tool_calls_out (по индексам).
    finish_out — см. complete(): собирает finish_reason/usage из последнего чанка.

    Повторы: стрим повторяется ТОЛЬКО если сбой произошёл ДО первого отданного токена
    (иначе игрок увидел бы склеенный из двух генераций текст)."""
    prov = provider or _provider()
    url = f"{prov['base_url']}/chat/completions"
    headers = {"Authorization": f"Bearer {prov['api_key']}"} if prov.get("api_key") else {}
    payload = _payload(messages, temperature, max_tokens, top_p, True, prov, tools=tools, tool_choice=tool_choice)
    headers["Accept"] = "text/event-stream"
    retries = _bg_retries()
    attempt = 0
    while True:
        tools_acc: dict[int, dict] = {}
        yielded = False
        try:
            async with _get_client().stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise RuntimeError(f"LLM HTTP {resp.status_code} ({prov['id']}): {_safe_body(body)}")
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        data = json.loads(chunk)
                    except Exception:
                        continue
                    if finish_out is not None:
                        _capture_finish(data, finish_out)
                    for c in (data.get("choices") or []):
                        delta = (c.get("delta") or {}) or {}
                        content = delta.get("content")
                        if content:
                            yielded = True
                            yield content
                        for tc in (delta.get("tool_calls") or []):
                            idx = int(tc.get("index", 0))
                            acc = tools_acc.setdefault(idx, {"name": "", "arguments": ""})
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                acc["name"] += fn["name"]
                            if fn.get("arguments"):
                                acc["arguments"] += fn["arguments"]
            break
        except Exception as e:
            # повтор допустим, только если игрок ещё НЕ увидел ни одного токена
            if yielded or attempt >= retries or not is_transient(e):
                raise
            attempt += 1
            delay = _backoff_delay(attempt)
            log.warning("LLM stream (%s): сбой до первого токена (%s), повтор %d/%d через %.1fс",
                        prov['id'], str(e)[:160], attempt, retries, delay)
            await asyncio.sleep(delay)
    if tool_calls_out is not None:
        for idx in sorted(tools_acc):
            acc = tools_acc[idx]
            tool_calls_out.append({"name": acc["name"], "arguments": acc["arguments"]})