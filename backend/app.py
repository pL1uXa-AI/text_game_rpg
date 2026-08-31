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

import json
import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import db, narrator, tts
from .config import get_config
from .routers import admin, catalog, entities, lore, system, tts as tts_router, worlds
from .routers.core import FRONTEND_DIR

log = logging.getLogger("textgame")

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

# Роутеры API (порядок включения не важен: пути уникальны)
app.include_router(catalog.router)
app.include_router(worlds.router)
app.include_router(admin.router)
app.include_router(tts_router.router)
app.include_router(entities.router)
app.include_router(lore.router)
app.include_router(system.router)

# Предустановленные рассказчики записываются в БД при первом старте (идемпотентно).
# Пресеты живут файлами в plots/narrators/*.js (backend/narrators_loader.py).
try:
    db.seed_narrators(narrator.NARRATOR_PRESETS)
except Exception:
    pass

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
    pass

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
    pass