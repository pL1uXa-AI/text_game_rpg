# -*- coding: utf-8 -*-
"""Роутер страниц и статуса: / (SPA), /admin, /api/system/status."""
from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse

from .. import chroma_client, db, llm, metrics, tts
from ..config import get_config
from .core import FRONTEND_DIR, _mask_provider

router = APIRouter(tags=["Система"])


@router.get("/", include_in_schema=False)
async def index():
    return FileResponse(f"{FRONTEND_DIR}/index.html")


@router.get("/admin", include_in_schema=False)
async def admin_page():
    # no-store: иначе браузер кеширует старую админку и ломается (null в скрипте)
    return FileResponse(f"{FRONTEND_DIR}/admin.html", headers={"Cache-Control": "no-store"})


@router.get("/api/metrics")
async def metrics_report(limit: int = Query(20, ge=1, le=100)):
    """Профилирование и мониторинг: агрегированные метрики ходов
    (время LLM, токены промпта/ответа, размер памяти, повторения — ИИ-качество)
    и последние снапшоты. In-memory — сбрасываются при перезапуске сервера."""
    return metrics.as_json(limit=limit)


@router.get("/api/system/status")
async def system_status():
    cfg = get_config()
    llm_up = False
    prov_main = cfg.get_provider("main")
    try:
        r = await llm._get_client().get(f"{prov_main['base_url']}/models", timeout=10)
        llm_up = r.status_code == 200
    except Exception:
        pass
    chroma_up = await chroma_client.ping()
    chroma_count = 0
    if chroma_up:
        try:
            chroma_count = await chroma_client.count()
        except Exception:
            pass
    tts_st = tts.engine_status()
    try:
        tts_cache_count = db.count_tts_cache()
    except Exception:
        tts_cache_count = 0
    return {"llm": {"up": llm_up, "base_url": prov_main["base_url"],
                     "provider": prov_main["id"], "model": prov_main.get("model")},
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