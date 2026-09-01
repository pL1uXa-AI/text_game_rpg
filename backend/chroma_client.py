# -*- coding: utf-8 -*-
"""
chroma_client.py — HTTP-клиент собственного экземпляра ChromaDB игры
(по-умолчанию 127.0.0.1:8001, данные в data/chroma — отдельно от py_docs:8000).
API v2 (chromadb 0.6.x): /api/v2/tenants/default_tenant/databases/default_database/collections

Коллекция выбирается ПО РАЗМЕРНОСТИ ВХОДЯЩИХ ВЕКТОРОВ (add/query) — это надёжно вне
зависимости от того, какой провайдер эмбеддингов использует конкретный мир/per-world:
  - размерность == cfg.local_embedding_dim (384) → collection "{base}_local"
  - иначе (напр. 4096 от RouterAI) → "{base}"
Chroma принудительно держит ОДНУ размерность на коллекцию, поэтому мешать нельзя.
delete_by_* применяется к ОБЕИМ коллекциям (base и local), т.к. не привязан к размерности.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import httpx

from .config import get_config
from .logsetup import get_logger, log_once
from .retry import with_retries

log = get_logger(__name__)

_TENANT = "default_tenant"
_DATABASE = "default_database"

_client: Optional[httpx.AsyncClient] = None
# кэш: имя коллекции -> её id (get_or_create)
_collections: dict[str, str] = {}


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0))
    return _client


async def close() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


def _collections_url() -> str:
    return f"/api/v2/tenants/{_TENANT}/databases/{_DATABASE}/collections"


def _collection_url(cid: str, suffix: str = "") -> str:
    return f"{_collections_url()}/{cid}{suffix}"


async def _attempt(method: str, url: str, payload: dict | None):
    try:
        return await _get_client().request(method, url, json=payload)
    except httpx.ConnectError as e:
        raise RuntimeError(
            f"ChromaDB не отвечает на {url} ({e}). Запусти start_chroma.bat в корне игры."
        ) from e


async def _raw(method: str, api_path: str, payload: dict | None = None):
    """Запрос к ChromaDB. Временные сбои (429/5xx/обрыв) повторяются — локальная база
    регулярно «отходит» после перезапуска, а один пропущенный вызов значил молча
    потерянную индексацию памяти (сессия 34, B2)."""
    cfg = get_config()
    url = f"http://{cfg.chroma_host}:{cfg.chroma_port}{api_path}"
    retries = max(0, int(getattr(cfg, "chroma_retries", 1) or 0))

    async def _go():
        resp = await _attempt(method, url, payload)
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise RuntimeError(f"ChromaDB HTTP {resp.status_code} on {api_path}: {resp.text[:300]}")
        return resp.json() if resp.text else None

    return await with_retries(_go, what=f"ChromaDB {method} {api_path}", retries=retries,
                              config_key="chroma_retries", backoff=0.3)


def collection_name_for_dim(dim: int) -> str:
    """Имя коллекции по размерности векторов (облачные 4096 → base, локальные 384 → base_local)."""
    cfg = get_config()
    base = cfg.chroma_collection or "text_game_memory"
    if dim == int(getattr(cfg, "local_embedding_dim", 384)):
        return f"{base}_local"
    return base


def collection_names_all() -> list[str]:
    """Все имена коллекций игры (base + local) — для delete_by_*, которые не привязаны к размерности."""
    cfg = get_config()
    base = cfg.chroma_collection or "text_game_memory"
    local = f"{base}_local"
    return list(dict.fromkeys([base, local]))


def _effective_collection_name() -> str:
    """(Обратная совместимость) Имя коллекции по глобальному провайдеру эмбеддингов.
    Основной путь — collection_name_for_dim, но этот используется status/миграциями."""
    cfg = get_config()
    base = cfg.chroma_collection or "text_game_memory"
    emb = cfg.get_provider("embedding")
    if emb.get("id") == "local":
        return f"{base}_local"
    return base


async def ensure_collection(name: str | None = None) -> str:
    """get_or_create коллекции по имени (default — глобальная). Возвращает id. Кэшируется."""
    global _collections
    if name is None:
        name = _effective_collection_name()
    if name in _collections:
        return _collections[name]
    created = await _raw("POST", _collections_url(), {"name": name, "get_or_create": True})
    cid = str(created.get("id") or "") if created else ""
    if not cid:
        cols = await _raw("GET", _collections_url()) or []
        cid = str(next((c for c in cols if c.get("name") == name), {}).get("id", ""))
    if cid:
        _collections[name] = cid
    return cid


async def add(ids: list[str], embeddings: list[list[float]], metadatas: list[dict], documents: list[str]) -> None:
    if not ids or not embeddings:
        return
    # нормализуем все векторы (чтобы L2-расстояние → косинус был корректен;
    # query уже нормализует запрос, а документы — здесь)
    normed = [_normalize(v) for v in embeddings]
    dim = len(normed[0])
    name = collection_name_for_dim(dim)
    cid = await ensure_collection(name)
    payload = {"ids": ids, "embeddings": normed, "metadatas": metadatas, "documents": documents}
    # D9 (аудит 38): в этом обработчике было два дефекта.
    #   (а) условие «"dimension" in str(e).lower() or "dimension" in str(e)» — вторая часть
    #       является подмножеством первой, то есть мертва (смысл был в регистронезависимости);
    #   (б) dimension-ветка имела собственный `continue` без потолка attempt: при
    #       повторяющемся конфликте размерностей имя удлинялось каждый круг
    #       (`base_local_4096_4096_…`), в Chroma плодились мусорные коллекции, а кэш
    #       `_collections` очищался только по последнему имени. Теперь на конфликт — ОДНА
    #       попытка переименования, затем предупреждение и наружу (закон 2: код замечает
    #       неладное, а не молча копит мусор). `import asyncio` из тела перенесён наверх.
    renamed = False
    for attempt in range(1, 4):
        try:
            await _raw("POST", _collection_url(cid, "/add"), payload)
            return
        except RuntimeError as e:
            msg = str(e)
            if "dimension" in msg.lower():
                if renamed:
                    log.warning("ChromaDB: повторный конфликт размерности (коллекция %s, "
                                "вектор %d) — добавление прервано вместо бесконечного "
                                "переименования коллекции", name, dim)
                    raise
                _collections.pop(name, None)
                name = f"{name}_{dim}"
                cid = await ensure_collection(name)
                renamed = True
                continue
            if "econnreset" not in msg.lower() and "reset" not in msg.lower():
                raise
            if attempt >= 3:
                raise
            await asyncio.sleep(2 * attempt)


async def delete_by_ids(ids: list[str]) -> None:
    """Удалить по id из ВСЕХ коллекций игры (base и local) — провайдер не известен."""
    if not ids:
        return
    for name in collection_names_all():
        try:
            cid = await ensure_collection(name)
            await _raw("POST", _collection_url(cid, "/delete"), {"ids": ids})
        except Exception as e:
            # удаление из одной коллекции (другая может не существовать) — не фатально,
            # но молчать нельзя: иначе «память помнит удалённое» станет невидимой проблемой
            log_once(log, f"chroma-del-{name}", 30, "ChromaDB: удаление из %s не удалось: %s",
                     name, e)


async def delete_by_where(where: dict) -> None:
    """Удалить всё по фильтру (напр. {"world_id": 3}) из ВСЕХ коллекций (base и local)."""
    for name in collection_names_all():
        try:
            cid = await ensure_collection(name)
            await _raw("POST", _collection_url(cid, "/delete"), {"where": where})
        except Exception as e:
            log_once(log, f"chroma-delw-{name}", 30, "ChromaDB: delete where из %s не удалось: %s",
                     name, e)


async def query(query_embedding: list[float], n_results: int = 10, where: dict | None = None) -> list[dict]:
    if not query_embedding:
        return []
    dim = len(query_embedding)
    name = collection_name_for_dim(dim)
    cid = await ensure_collection(name)
    qv = _normalize(query_embedding)
    payload: dict = {
        "query_embeddings": [qv],
        "n_results": n_results,
        "include": ["documents", "metadatas", "distances"],
    }
    if where:
        payload["where"] = where
    result = await _raw("POST", _collection_url(cid, "/query"), payload) or {}
    ids = (result.get("ids") or [[]])[0]
    docs = (result.get("documents") or [[]])[0]
    metas = (result.get("metadatas") or [[]])[0]
    dists = (result.get("distances") or [[]])[0]
    out = []
    for i in range(len(ids)):
        out.append({
            "chunk_id": ids[i],
            "content": docs[i] if i < len(docs) else "",
            "metadata": metas[i] if i < len(metas) else {},
            "distance": float(dists[i]) if i < len(dists) else 0.0,
        })
    return out


async def count(name: str | None = None) -> int:
    """Количество векторов в коллекции. name=None — глобальная (обратная совместимость)."""
    cid = await ensure_collection(name)
    return int(await _raw("GET", _collection_url(cid, "/count")) or 0)


async def ping() -> bool:
    cfg = get_config()
    try:
        resp = await _get_client().get(f"http://{cfg.chroma_host}:{cfg.chroma_port}/api/v2/heartbeat")
        return resp.status_code == 200
    except Exception:
        return False


def _normalize(vec: list[float]) -> list[float]:
    norm = sum(v * v for v in vec) ** 0.5
    if norm == 0:
        return vec
    return [v / norm for v in vec]


def cosine_from_distance(dist: float) -> float:
    """L2-расстояние нормализованных векторов → косинусная близость."""
    return max(0.0, min(1.0, 1.0 - dist / 2.0))

