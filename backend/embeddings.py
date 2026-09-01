# -*- coding: utf-8 -*-
"""
embeddings.py — облачный эмбеддинг + реранкер (как в py_docs/rag).
Эмбеддинг: RouterAI qwen/qwen3-embedding-8b (4096-dim, префиксы Passage:/Query:).
Реранкер: RouterAI voyageai/rerank-2.5 (/v1/rerank).
Гибрид: косинус (ChromaDB) + BM25 → взвешенная сумма.
"""
from __future__ import annotations

import math
import os
import re
import time
from collections import OrderedDict
from typing import Optional

import httpx

from .config import Config, get_config
from .logsetup import get_logger

log = get_logger(__name__)

_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))
    return _client


async def close() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


# ──────────────────────────────────────────────
# Локальные эмбеддинги (fastembed) — оффлайн-фолбэк
# ──────────────────────────────────────────────
import asyncio
import threading

_embed_model = None           # fastembed.TextEmbedding (привязка к модели)
_embed_model_name = ""        # какая модель загружена
_embed_lock = threading.Lock()


def _local_model():
    """Лениво загружает fastembed-модель (модель кэшируется в local_embedding_cache).
    Импорт fastembed отложенный: если не установлен — пробрасываем ImportError наверх
    (а вызывающие оборачивают в try/except и тихо деградируют)."""
    global _embed_model, _embed_model_name
    cfg = get_config()
    model = cfg.local_embedding_model
    with _embed_lock:
        if _embed_model is not None and _embed_model_name == model:
            return _embed_model
        # на Windows hf_xet иногда вешает загрузку — отключаем, оффлайн-кэш и так хорош
        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
        from fastembed import TextEmbedding  # тяжёлый импорт — только когда нужен
        _embed_model = TextEmbedding(model, cache_dir=cfg.local_embedding_cache)
        _embed_model_name = model
        return _embed_model


async def embed_local(texts: list[str]) -> list[list[float]]:
    """Локальный эмбеддинг через fastembed (работает без сети; модель скачивается один раз).
    Векторы нормализуются (L2), чтобы совпадать с ожиданиями Chroma/`cosine_from_distance`
    (быстрый `_normalize` применяет к запросу, а тут — к документам)."""
    if not texts:
        return []

    def _run():
        model = _local_model()
        out = [list(map(float, v)) for v in model.embed(texts)]
        # L2-нормализация каждого вектора
        normed = []
        for v in out:
            n = sum(x * x for x in v) ** 0.5
            normed.append([x / n if n else x for x in v])
        return normed

    # fastembed — синхронный и CPU-затратный: выполняем в отдельном потоке,
    # чтобы не блокировать event loop.
    return await asyncio.to_thread(_run)


# ──────────────────────────────────────────────
# LRU-кэш эмбеддингов (сессия 30, задача «Кеширование эмбеддингов»)
# Повторяющиеся запросы (общие фразы, частые действия, повторная индексация карточек)
# не пересчитывают вектор — бережёт и токены облака, и CPU локального fastembed.
# Ключ: (provider_id, model, prefix+text) — смена провайдера/модели не даёт устаревших векторов.
# ──────────────────────────────────────────────

_EMB_CACHE_MAX = 512
_emb_cache: "OrderedDict[str, list[float]]" = OrderedDict()


def _cache_key(prov: dict, prepared: str) -> str:
    return f"{prov.get('id') or '?'}|{prov.get('model') or ''}|{prepared}"


def _cache_get(key: str) -> Optional[list[float]]:
    v = _emb_cache.get(key)
    if v is None:
        return None
    _emb_cache.move_to_end(key)
    return v


def _cache_put(key: str, vec: list[float]) -> None:
    _emb_cache[key] = vec
    _emb_cache.move_to_end(key)
    while len(_emb_cache) > _EMB_CACHE_MAX:
        _emb_cache.popitem(last=False)


def _cache_clear() -> None:
    _emb_cache.clear()


async def _http_json(method: str, url: str, headers: dict | None = None,
                     payload: dict | None = None, timeout: int = 120) -> dict:
    c = _get_client()
    resp = await c.request(method, url, json=payload, headers=headers or {})
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code} on {url}: {resp.text[:400]}")
    return resp.json() if resp.text else {}


# ──────────────────────────────────────────────
# Эмбеддинги
# ──────────────────────────────────────────────
async def embed_batch(texts: list[str], is_query: bool = False,
                      provider: dict | None = None) -> list[list[float]]:
    if not texts:
        return []
    cfg: Config = get_config()
    prov = provider or cfg.get_provider("embedding")
    if not prov.get("enabled"):
        raise RuntimeError("Провайдер эмбеддингов выключен (EMBEDDING_PROVIDER=none) — RAG-память недоступна.")
    # Локальный оффлайн-фолбэк (fastembed): не нужен ключ/base_url/интернет
    if prov.get("id") == "local":
        try:
            return await embed_local(texts)
        except Exception:
            raise  # вызывающие ловят и тихо деградируют
    if not prov.get("api_key"):
        raise RuntimeError(f"Нет API-ключа для провайдера эмбеддингов «{prov['id']}» (EMBEDDING_API_KEY).")
    if not prov.get("base_url"):
        raise RuntimeError(f"Нет base_url для провайдера эмбеддингов «{prov['id']}».")

    prefix = cfg.prefix_query if is_query else cfg.prefix_doc
    prepared = [prefix + t for t in texts]

    # LRU-кэш: точное совпадение по (провайдер, модель, prefix+текст)
    cached: dict[int, list[float]] = {}
    missing: list[int] = []
    for i, p in enumerate(prepared):
        v = _cache_get(_cache_key(prov, p))
        if v is not None:
            cached[i] = v
        else:
            missing.append(i)

    if missing:
        seen: dict[str, int] = {}
        unique: list[str] = []
        for i in missing:
            p = prepared[i]
            if p not in seen:
                seen[p] = len(unique)
                unique.append(p)

        result: dict[str, list[float]] = {}
        MAX_BATCH = 512
        url = f"{prov['base_url']}/embeddings"
        headers = {"Authorization": f"Bearer {prov['api_key']}"}
        for start in range(0, len(unique), MAX_BATCH):
            sub = unique[start:start + MAX_BATCH]
            for attempt in range(1, 6):
                try:
                    resp = await _http_json(
                        "POST",
                        url,
                        headers=headers,
                        payload={"model": prov["model"], "input": sub, "encoding_format": "float"},
                    )
                    for item in resp.get("data", []):
                        idx = item.get("index")
                        vec = item.get("embedding")
                        if idx is not None and vec:
                            if len(vec) != cfg.embedding_dim:
                                raise RuntimeError(
                                    f"Размерность эмбеддинга {len(vec)} != {cfg.embedding_dim} "
                                    f"(модель {prov['model']})"
                                )
                            result[sub[int(idx)]] = [float(v) for v in vec]
                    break
                except (RuntimeError, httpx.HTTPError, ConnectionError, TimeoutError):
                    if attempt == 5:
                        raise
                    time.sleep(2 * attempt)

        for i in missing:
            p = prepared[i]
            vec = result.get(p)
            if vec:
                cached[i] = vec
                _cache_put(_cache_key(prov, p), vec)

    return [cached.get(i) for i in range(len(prepared))]


async def embed_documents(texts: list[str], provider: dict | None = None) -> list[list[float]]:
    return await embed_batch(texts, is_query=False, provider=provider)


async def embed_query(text: str, provider: dict | None = None) -> list[float]:
    out = await embed_batch([text], is_query=True, provider=provider)
    return out[0] if out else []


# ──────────────────────────────────────────────
# BM25 (гибрид, без внешних зависимостей)
# ──────────────────────────────────────────────
_STOPWORDS_RU = set("""и в во не на по он она оно они я ты мы вы это как так что с со к ко из от у о за
при для да но а же если то только еще уже можно надо будет было были есть нет дать быть был бы или
либо ни ну вот же как так где когда который которая которые которого которых этот эта это эти та том
тех всем всему вся весь всякий сам сама само свой своя свой каждый каждое каждый какая какой какие
каких кого кому кем о ком чего чему чем о чем очень более менее самый самое самая самыми почти совсем
совершенно весьма довольно также тоже затем потом теперь сейчас всегда иногда часто редко много мало
немного больше меньше всего всех всей одно один одна одни одного одному одной нескольк""".split())

_STOPWORDS_EN = set("""a an the and or but if then than so for with without from by at in on of to as is
are was were be been being have has had do does did will would can could should shall may might must this
that these those it its i you he she they we them him her my your our their there here where when why how
what which who whom not no yes very much many""".split())

_STOPWORDS = _STOPWORDS_RU | _STOPWORDS_EN


def preprocess_text(text: str, remove_stopwords: bool = True) -> str:
    t = text
    t = re.sub(r"```.*?```", " ", t, flags=re.DOTALL)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"[#>*_~|]", " ", t)
    t = re.sub(r"\[[^\]]*\]\([^)]*\)", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"[^\w\sа-яА-ЯёЁA-Za-z-]", " ", t)
    t = re.sub(r"\s+", " ", t).lower().strip()
    words = [w for w in t.split() if (w not in _STOPWORDS if remove_stopwords else True) and len(w) > 1]
    return " ".join(words)


def _tokenize(text: str) -> list[str]:
    return preprocess_text(text).split()


def bm25_scores(query_tokens: list[str], corpus_texts: list[str], k1: float = 1.5, b: float = 0.75) -> list[float]:
    if not query_tokens:
        return [0.0] * len(corpus_texts)
    n_docs = len(corpus_texts)
    if n_docs == 0:
        return []
    doc_token_lists = [_tokenize(d) for d in corpus_texts]
    doc_lens = [len(toks) for toks in doc_token_lists]
    avg_len = sum(doc_lens) / n_docs if n_docs else 0.0
    df: dict[str, int] = {}
    for toks in doc_token_lists:
        for t in set(toks):
            df[t] = df.get(t, 0) + 1
    scores = []
    for i, toks in enumerate(doc_token_lists):
        doc_len = doc_lens[i]
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        s = 0.0
        for qt in query_tokens:
            if qt not in tf:
                continue
            idf = math.log(1 + (n_docs - df.get(qt, 0) + 0.5) / (df.get(qt, 0) + 0.5))
            tf_i = tf[qt]
            denom = tf_i + k1 * (1 - b + b * doc_len / avg_len) if avg_len else 1.0
            s += idf * (tf_i * (k1 + 1)) / denom
        scores.append(s)
    return scores


def hybrid_rerank(query: str, candidates: list[dict], weight_bm25: float = 0.4) -> list[dict]:
    if not candidates:
        return candidates
    # Сессия 36, п.29: HYBRID_WEIGHT_BM25 приходит из .env/админки и может оказаться
    # бессмысленным (2, −1, «abc» от кривого сохранения). Формула (1−w)·cos + w·bm при
    # w∉[0,1] даёт score вне диапазона и перекос сортировки (при w>1 косинус уходит с
    # ОТРИЦАТЕЛЬНЫМ весом — память начинает топить релевантное). Клампим здесь, у
    # точки применения, а не только во валидации админки: конфиг — внешний вход.
    try:
        w = float(weight_bm25)
    except (TypeError, ValueError):
        w = 0.4
    if math.isnan(w):      # NaN прошёл бы мимо min/max и обнулил бы оба слагаемых
        w = 0.4
    w = min(1.0, max(0.0, w))
    q_tokens = _tokenize(query)
    texts = [c.get("content", "") for c in candidates]
    bm = bm25_scores(q_tokens, texts)
    max_cos = max((c.get("similarity", 0) for c in candidates), default=1.0) or 1.0
    max_bm = max(bm) if bm else 1.0
    for c, b in zip(candidates, bm):
        cos_n = c.get("similarity", 0.0) / max_cos
        bm_n = b / max_bm if max_bm else 0.0
        c["_hybrid"] = (1 - w) * cos_n + w * bm_n
    candidates.sort(key=lambda c: c.get("_hybrid", 0), reverse=True)
    return candidates


# ──────────────────────────────────────────────
# Облачный реранкер (RouterAI /v1/rerank и OpenAI-совместимые /rerank)
# ──────────────────────────────────────────────
async def rerank_results(query: str, candidates: list[dict], top_n: int = 5,
                        provider: dict | None = None) -> list[dict]:
    """Облачный ререранк кандидатов.

    A17 (аудит 38): `RERANK_THRESHOLD` из конфига наконец применяется — фильтр по
    релевантности (скор провайдера `relevance_score >= rerank_threshold`). Порог 0 =
    выключен (ничего не отбрасываем). `top_n` остаётся размером выдачи вызывающего
    (K памяти / лор-чанки) — второй ручки на то же самое нет сознательно.
    """
    if not candidates:
        return candidates
    cfg: Config = get_config()
    prov = provider or cfg.get_provider("rerank")
    # Реранкер выключен (опция в настройках или провайдер none) — отдаём кандидатов как есть
    if not cfg.rerank_enabled or not prov.get("enabled"):
        return candidates
    if not prov.get("api_key"):
        return candidates
    texts = [c.get("content", "")[:2000] for c in candidates]
    try:
        resp = await _http_json(
            "POST",
            f"{prov['base_url']}/rerank",
            headers={"Authorization": f"Bearer {prov['api_key']}"},
            payload={"model": prov["model"], "query": query, "documents": texts},
            timeout=90,
        )
    except Exception:
        return candidates  # реранкер недоступен — отдаём как есть
    results = resp.get("results") or []
    scored: dict[int, float] = {}
    for r in results:
        idx = r.get("index")
        score = r.get("relevance_score", 0.0)
        if idx is not None and 0 <= idx < len(candidates):
            scored[int(idx)] = float(score)
    for i, c in enumerate(candidates):
        c["_rerank"] = scored.get(i, 0.0)
    candidates.sort(key=lambda c: c.get("_rerank", 0), reverse=True)
    # A17: порог релевантности (0 = фильтр выключен). Отбрасываем ХВОСТ ниже порога —
    # память обязана помнить, но не тащить в промпт мусор, который реранкер посчитал
    # нерелевантным. Порог выше всех скоров = отдаём пустой список (для вызывающего это
    # штатный «ничего не вспомнилось», с фолбэками слоя памяти).
    try:
        thr = float(get_config().rerank_threshold or 0.0)
    except (TypeError, ValueError):
        thr = 0.0
    if thr > 0:
        kept = [c for c in candidates if float(c.get("_rerank", 0) or 0.0) >= thr]
        if len(kept) != len(candidates):
            log.debug("реранкер: отброшено %d кандидатов с relevance_score < %s",
                      len(candidates) - len(kept), thr)
        candidates = kept
    return candidates[:top_n]