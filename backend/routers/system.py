# -*- coding: utf-8 -*-
"""Роутер страниц и статуса: / (SPA), /admin, /api/system/status."""
from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse

from .. import chroma_client, db, llm, metrics, tts
from ..config import get_config
from ..logsetup import get_logger

log = get_logger(__name__)
from .core import FRONTEND_DIR, _mask_provider

router = APIRouter(tags=["Система"])

# D15 (аудит 41): обе HTML-страницы отдаются ОДИНАКОВО. Раньше `/admin` имел no-store, а `/`
# — никакого Cache-Control, и браузер кешировал index.html по эвристике (last-modified), поэтому
# после правки фронта обычный F5 приносил старый HTML (правило 6: спасал только hard refresh).
# `no-store` (а не `no-cache`): SPA — один файл на весь мир, экономия на ревалидации незначима,
# зато исключён и «304 из прокси», и кеш в приватном режиме; та же метка, что у озвучки (tts.py).
# ⚠ Касается только HTML: `/static/app.js` и `/static/style.css` отдаёт StaticFiles без этого
# заголовка (у него своя ревалидация по ETag), поэтому после правки фронта hard refresh всё ещё
# полезен — см. правило 6.
_PAGE_HEADERS = {"Cache-Control": "no-store"}


@router.get("/", include_in_schema=False)
async def index():
    # no-store: см. _PAGE_HEADERS (D15) — без него браузер кеширует оболочку SPA
    return FileResponse(f"{FRONTEND_DIR}/index.html", headers=_PAGE_HEADERS)


@router.get("/admin", include_in_schema=False)
async def admin_page():
    # no-store: иначе браузер кеширует старую админку и ломается (null в скрипте)
    return FileResponse(f"{FRONTEND_DIR}/admin.html", headers=_PAGE_HEADERS)


@router.get("/api/metrics")
async def metrics_report(limit: int = Query(20, ge=1, le=100)):
    """Профилирование и мониторинг: агрегированные метрики ходов
    (время LLM, токены промпта/ответа, размер памяти, повторения — ИИ-качество)
    и последние снапшоты. In-memory — сбрасываются при перезапуске сервера."""
    return metrics.as_json(limit=limit)


@router.get("/api/system/status")
async def system_status():
    cfg = get_config()
    prov_main = cfg.get_provider("main")
    # A12 (аудит 41): тот же критерий живости, что у игры (llm.probe: «<500» + заголовок
    # ключа), а не свой «200 без ключа» — иначе на llama.cpp с --api-key или облачном
    # шлюзе мир играется, а плашка врёт «LLM ✗». «Требует ключ» — отдельным полем.
    llm_up, llm_needs_key, llm_detail = await llm.probe(prov_main)
    if not llm_up:
        log.debug("статус: основная модель недоступна (%s): %s", prov_main.get('base_url'), llm_detail)
    chroma_up = await chroma_client.ping()
    chroma_count = 0
    if chroma_up:
        try:
            chroma_count = await chroma_client.count()
        except Exception as e:
            log.debug("статус: счётчик Chroma недоступен: %s", e)
    tts_st = tts.engine_status()
    try:
        tts_cache_count = db.count_tts_cache()
    except Exception as e:
        log.debug("статус: кэш озвучки не посчитан: %s", e)
        tts_cache_count = 0
    return {"llm": {"up": llm_up, "base_url": prov_main["base_url"],
                     "provider": prov_main["id"], "model": prov_main.get("model"),
                     "needs_key": llm_needs_key, "detail": llm_detail},
            "chroma": {"up": chroma_up, "port": cfg.chroma_port,
                       "collection": cfg.chroma_collection, "chunks": chroma_count},
            "providers": {
                "main": _mask_provider(dict(prov_main)),
                "embedding": _mask_provider(dict(cfg.get_provider("embedding"))),
                "rerank": _mask_provider(dict(cfg.get_provider("rerank"))),
            },
            "rerank_enabled": cfg.rerank_enabled,
            "embedding_model": cfg.embedding_model,
            "tts": {
                "provider": cfg.tts_provider,
                "global_enabled": bool(cfg.tts_enabled),
                "libs": tts_st["libs"],
                "piper_voices": tts_st["voices"]["piper"],
                "kokoro_ready": tts_st["kokoro_ready"],
                "cache_chunks": tts_cache_count,
                "default_voice": tts.DEFAULT_VOICES.get(cfg.tts_provider, ""),
            }}