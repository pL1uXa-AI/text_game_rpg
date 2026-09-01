# -*- coding: utf-8 -*-
"""retry.py — повторы с экспоненциальной задержкой для «внешних» сервисов (сессия 34, B2).

Проблема, которую это закрывает: отказ llama.cpp/Chroma/облака на **одном** 429 или
таймауте стоил игроку либо ответа (ход падал с RuntimeError), либо молча проглоченной
фоновой задачи (судья/карточки/мастер просто не отрабатывали — и по логам это было
неотличимо от «отключено»). Все шесть фоновых агентов и ход игрока идут через llm.*,
поэтому один нестабильный провайдер обесценивал целый слой системы.

Политика:
  * повторяем **только временные** сбои (408/409/425/429/5xx, таймаут, обрыв соединения);
    4xx по существу (400 плохой запрос, 401 ключ, 404) — не повторяем, это не поможет;
  * экспоненциальная задержка + джиттер (LLM_RETRY_BACKOFF, по умолчанию 0.8с → 1.6с);
  * потолок попыток из конфига (LLM_RETRIES), 0 = выключить;
  * каждый повтор — в лог с контекстом (правило 14), чтобы «почему ход занял 20 секунд»
    было где увидеть.

Использование:
    from .retry import with_retries, is_transient
    res = await with_retries(lambda: llm.complete(...), what="ход", retries=2)
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, Callable, Awaitable, Optional

import httpx

from .logsetup import get_logger

log = get_logger(__name__)

# Коды, при которых запрос имеет смысл повторить.
_TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 509, 520, 521, 522, 524}


def is_transient(err: BaseException | str) -> bool:
    """Временный ли сбой? Опознаём httpx-исключения таймаутов/соединений и наши
    RuntimeError вида «LLM HTTP 503 (…)» / «ChromaDB HTTP 429 …», «ChromaDB не отвечает»."""
    if isinstance(err, str):
        return _transient_in_text(err)
    if isinstance(err, (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError,
                        httpx.RemoteProtocolError, asyncio.TimeoutError)):
        return True
    status = getattr(err, "status_code", None)
    if isinstance(status, int) and status in _TRANSIENT_STATUS:
        return True
    return _transient_in_text(str(err))


def _transient_in_text(text: str) -> bool:
    t = (text or "").lower()
    if "не отвечает" in t or "connect" in t or "timed out" in t or "timeout" in t:
        return True
    for code in _TRANSIENT_STATUS:
        if f"http {code}" in t or f"({code}" in t or f"status {code}" in t:
            return True
    return False


def _retries(default_key: str = "llm_retries") -> int:
    """Число ПОВТОРОВ из конфига (env/админка). Ошибку конфига трактуем как «без повторов»."""
    try:
        from .config import get_config
        return max(0, int(getattr(get_config(), default_key, 2) or 0))
    except Exception:
        return 0


def _backoff() -> float:
    try:
        from .config import get_config
        return max(0.0, float(getattr(get_config(), "llm_retry_backoff", 0.8) or 0.8))
    except Exception:
        return 0.8


async def with_retries(factory: Callable[[], Awaitable[Any]], *, what: str = "запрос",
                       retries: int | None = None, backoff: float | None = None,
                       config_key: str = "llm_retries",
                       transient_fn: Optional[Callable[[BaseException], bool]] = None,
                       on_retry: Optional[Callable[[int, BaseException], Any]] = None) -> Any:
    """Выполняет асинхронную операцию, повторяя временные сбои.

    factory — функция без аргументов, возвращающая корутину (лямбда достаточно важна:
    корутина не должна создаваться до попытки, иначе «coroutine never awaited»).
    transient_fn — своя функция «повторять ли этот сбой» (по умолчанию is_transient);
    нужна для клиентов со своими классами ошибок (например Edge TTS), которые в
    общих текстовых признаках не опознаются.
    Последнее исключение пробрасывается вызывающему (повторы не прячут беду).
    """
    n = _retries(config_key) if retries is None else max(0, int(retries))
    base = _backoff() if backoff is None else max(0.0, float(backoff))
    is_trans = transient_fn or is_transient
    attempt = 0
    while True:
        try:
            return await factory()
        except Exception as e:
            if attempt >= n or not is_trans(e):
                raise
            delay = base * (2 ** attempt) * (1.0 + random.random() * 0.25)  # экспонента + джиттер
            attempt += 1
            log.warning("%s: временный сбой (%s), повтор %d/%d через %.1fс",
                        what, str(e)[:180], attempt, n, delay,
                        extra={"fields": {"retry_attempt": attempt, "retry_max": n,
                                          "retry_delay": round(delay, 2)}})
            if on_retry:
                try:
                    on_retry(attempt, e)
                except Exception:
                    log.debug("on_retry-колбэк не отработал", exc_info=True)
            await asyncio.sleep(delay)
