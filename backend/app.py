# -*- coding: utf-8 -*-
"""
app.py — FastAPI-бэкенд текстовой RPG с ИИ-рассказчиком (llama.cpp + гибридная память).

После рефакторинга (сессия: app.py ~1300 строк → роутеры) здесь только сборка:

    backend/routers/
      core.py     — общие помощники + ядро хода (_process_action) + фоновые задачи
      worlds.py   — миры, действия (SSE), слоты, настройки, провайдеры, память
      catalog.py  — темы, жанры, рассказчики, сюжеты, опции провайдеров
      admin.py    — админка (глобальные настройки)
      tts.py      — озвучка
      entities.py — карточки сущностей
      system.py   — страницы /, /admin и /api/system/status

Запуск:  uvicorn backend.app:app --host 127.0.0.1 --port 8002
"""
from __future__ import annotations

import contextlib
import json
import logging
import time

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import bus, db, narrator, tts
from .config import get_config
from .routers import admin, catalog, entities, lore, system, tts as tts_router, worlds
from .routers.core import FRONTEND_DIR

from .logsetup import configure as _configure_logs, get_logger

_configure_logs()          # JSON-лог data/logs/game.log + контекст хода (сессия 34, правило 14)
log = get_logger(__name__)

app = FastAPI(
    title="Text Game RPG",
    version="1.0",
    description=(
        "Живая текстовая RPG с ИИ-рассказчиком (llama.cpp / Ollama / OpenAI-совместимый) и "
        "гибридной долгосрочной памятью (сводки + ChromaDB RAG + реранкер) и персональными "
        "карточками сущностей.\n\n"
        "Ссылки: `/docs` — эта интерактивная документация, `/static/` — SPA-фронтенд, `/admin` — админка."
    ),
    openapi_tags=[
        {"name": "Каталог", "description": "Темы миров, жанры, рассказчики, свои сюжеты, опции провайдеров."},
        {"name": "Миры и действия", "description": "Миры, ходы (в т.ч. SSE-стриминг), слоты, настройки, провайдеры, память, экспорт, маска ключей."},
        {"name": "Админка", "description": "Глобальные настройки LLM/провайдеров/генерации/TTS и фоновых задач."},
        {"name": "Озвучка (TTS)", "description": "Движки/голоса, проверка и скачивание голосов, статусы."},
        {"name": "Карточки сущностей", "description": "Память о NPC/локациях/фракциях/квестах/предметах/событиях и карточках знаний."},
        {"name": "Лор мира", "description": "Статьи «библии» вселенной, CRUD и RAG-поиск по лору."},
        {"name": "Система", "description": "Страницы и статус LLM/Chroma/эмбеддингов."},
    ],
)

app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


# ── Контекст запроса для лога (сессия 34): каждый HTTP-вызов получает request_id, а
# всё залогированное внутри — метку мира из пути (/api/worlds/{id}/...). Так ход игрока,
# фоновые агенты и ошибки видны в data/logs/game.log связанными по одной цепочке.
@app.middleware("http")
async def _log_request_context(request, call_next):
    import re as _re
    import uuid as _uuid
    from . import logsetup

    rid = _uuid.uuid4().hex[:8]
    m = _re.search(r"/api/worlds/(\d+)", request.url.path)
    tok_w = logsetup._ctx_rid.set(rid)
    tok_r = None
    if m:
        tok_r = logsetup._ctx_world.set(int(m.group(1)))
    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("необработанное исключение в %s %s", request.method, request.url.path,
                      extra={"fields": {"request_id": rid, "path": request.url.path}})
        raise
    finally:
        with contextlib.suppress(Exception):
            logsetup._ctx_rid.reset(tok_w)
            if tok_r is not None:
                logsetup._ctx_world.reset(tok_r)
    dt = (time.perf_counter() - t0) * 1000
    # служебные/статические — не шумим; API-вызовы пишем на INFO с длительностью
    path = request.url.path
    if path.startswith("/api/"):
        log.log(logging.WARNING if response.status_code >= 400 else logging.INFO,
                "%s %s → %s за %.0f мс", request.method, path, response.status_code, dt,
                extra={"fields": {"request_id": rid, "status": response.status_code, "ms": round(dt)}})
    return response

# Роутеры API (порядок включения не важен: пути уникальны)
app.include_router(catalog.router)
app.include_router(worlds.router)
app.include_router(admin.router)
app.include_router(tts_router.router)
app.include_router(entities.router)
app.include_router(lore.router)
app.include_router(system.router)

# Живой чат (сессия 34, D3): шина событий мира получает каждое записанное событие БД
# и мгновенно раздаёт открытым вкладкам вместо поллинга раз в 15 секунд.
db.add_event_listener(bus.listener())


@app.on_event("startup")
async def _bus_attach_loop() -> None:
    """Запомнить цикл приложения: синхронный код БД публикует через call_soon_threadsafe."""
    import asyncio

    bus.attach_loop(asyncio.get_running_loop())


@app.on_event("startup")
async def _startup_summary() -> None:
    """Одна строка в логе о том, в каком режиме стартует игра (диагностика, правило 14)."""
    cfg = get_config()
    log.info("старт: модель=%s контекст_стандарт=%d фон=%s судья=%s мастер=%s враж_ии=%s "
             "события=%s озвучка=%s фонов_LLM_параллельно=%d", cfg.main_provider,
             cfg.context_tokens, cfg.background_tasks_enabled, cfg.logic_judge_enabled,
             cfg.autonomous_master_enabled, cfg.enemy_ai_enabled, cfg.dynamic_events_enabled,
             cfg.tts_provider, cfg.llm_bg_concurrency)


# Предустановленные рассказчики записываются в БД при первом старте (идемпотентно).
# Пресеты живут файлами в plots/narrators/*.js (backend/narrators_loader.py).
try:
    db.seed_narrators(narrator.NARRATOR_PRESETS)
except Exception:
    log.exception("сид рассказчиков не удался (игра стартует без пресетов)")

# ── Резервное копирование БД при старте (сессия 33) ──
# data/game.db — единственное место, где живут миры/события/карточки/лор; в git он не лежит.
# Консистентный снимок (SQLite Backup API) в data/backups/ с ротацией старых.
# Выключается BACKUP_DB_ON_START=false (.env/админка).
try:
    if get_config().backup_db_on_start:
        _bak = db.backup_database(keep=get_config().backup_keep)
        if _bak:
            log.info("снимок БД сохранён: %s", _bak)
except Exception as e:
    log.warning("стартовый бэкап БД не удался (игра стартует без него): %s", e)

# Предзагрузка голосов TTS (Piper/Kokoro) при старте — прогревает sherpa-onnx,
# чтобы первый ответ игрока озвучился без задержки на инициализацию движка (сессия 30).
try:
    tts.preload_configured_voices()
except Exception:
    log.warning("предзагрузка голосов TTS не удалась (озвучка прогреется при первом синтезе)",
                exc_info=True)

# Само-исцеление цифровых рангов навыков в сохранённых мирах (баг сессии: генератор
# отдавал ранги числами 1/2/3… вместо букв F/E/D…). Идемпотентно: ничего не делает,
# если ранги уже буквенные.
try:
    for _w in db.list_worlds():
        _wid = _w["id"]
        _full = db.get_world(_wid) or {}
        _s = json.loads(_full.get("setting") or "{}")
        if isinstance(_s, dict) and narrator.normalize_setting_ranks(_s):
            db.update_world(_wid, setting=_s)
except Exception:
    log.warning("само-исцеление рангов в мирах пропущено", exc_info=True)