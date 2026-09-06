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

Кроме сборки, здесь живёт ОДИН стартовый хелпер — `self_heal()` (сид рассказчиков, бэкап БД,
чистка сирот и починка старых миров); зовёт его `_lifespan` фоновой задачей, а НЕ импорт
модуля (аудит 41, A13, правило 26 в AGENT.md).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles

from . import bg, bus, chroma_client, db, embeddings, flag_titles, graph, llm, memory, metrics, narrator, ratelimit, tts
from .config import get_config
from .routers import admin, catalog, entities, lore, system, tts as tts_router, worlds
from .routers.core import FRONTEND_DIR

from .logsetup import configure as _configure_logs, get_logger

_configure_logs()          # JSON-лог data/logs/game.log + контекст хода (сессия 34, правило 14)
log = get_logger(__name__)

# ── Жизненный цикл приложения (lifespan; сессия 36, п.30) ──
# startup: шина живого чата запоминает цикл событий + одна строка диагностики.
# shutdown: поток `aiosqlite-loop` — daemon, без явного close() соединение SQLite
# оставалось открытым, пока жив процесс, и на Windows файл game.db мог
# оказаться залочен при рестарте («database is locked»). Закрываем по
# порядку: сетевые httpx-клиенты → очередь фоновых агентов → цикл БД.
# Каждый шаг в своёй страховке: падение одного не мешает закрыть остальные.
async def _tts_cache_keepalive() -> None:
    """Фоновый цикл ротации кеша озвучки (аудит 38, A18): почасовая сверка, внутри —
    «не чаще раза в сутки» (метка-файл в tts.rotate_tts_cache)."""
    while True:
        try:
            await asyncio.sleep(3600)          # почасовая сверка: метку режет сам модуль tts
            if not get_config().tts_cache_enabled:
                continue
            r = await asyncio.to_thread(tts.rotate_tts_cache)
            if r.get("pruned") or r.get("swept"):
                log.info("ротация кеша озвучки в фоне: %s", r)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # правило 14: тихо сдохший keepalive = бесконечно растущий кеш без единой строки
            log.warning("фоновая ротация кеша озвучки сбойнула (continue): %s", e)
            await asyncio.sleep(3600)


def self_heal() -> dict:
    """Один идемпотентный проход «само-исцеления» базы (A13, аудит 41: вызывается из
    `_lifespan`, а НЕ при импорте модуля).

    Что внутри (каждый шаг переживает отказ и не роняет остальные — правило 14: пишет
    warning с контекстом, игрок продолжает играть):
      * сид предустановленных рассказчиков (файлы `plots/narrators/*.js`);
      * снимок `game.db` в `data/backups/` с ротацией (сессия 33, `BACKUP_DB_ON_START`);
      * чистка сирот в БД (аудит 38, A8);
      * буквенные ранги навыков в старых сохранениях;
      * машинные имена карточек (сессия 40, п.9), названия флагов (п.12), игрок-как-NPC
        (п.13) и пересинхронизация карты мира после этих правок.

    Возвращает счётчики (`worlds`, `orphans`, `ranks`, `cards`, `flags`, `npc`) — их
    печатает `_startup_heal` одной строкой журнала.
    """
    r: dict = {"worlds": 0, "orphans": 0, "ranks": 0, "cards": 0, "flags": 0, "npc": 0}

    # Предустановленные рассказчики (идемпотентно, INSERT OR IGNORE по name).
    try:
        db.seed_narrators(narrator.NARRATOR_PRESETS)
    except Exception:
        log.exception("сид рассказчиков не удался (игра стартует без пресетов)")

    # ── Резервное копирование БД при старте (сессия 33) ──
    # data/game.db — единственное место, где живут миры/события/карточки/лор; в git он не лежит.
    # Консистентный снимок (SQLite Backup API) в data/backups/ с ротацией старых.
    # Выключается BACKUP_DB_ON_START=false (.env/админка).
    try:
        cfg = get_config()
        if cfg.backup_db_on_start:
            _bak = db.backup_database(keep=cfg.backup_keep)
            if _bak:
                log.info("снимок БД сохранён: %s", _bak)
    except Exception as e:
        log.warning("стартовый бэкап БД не удался (игра стартует без него): %s", e)

    # ── Само-исцеление сирот в БД (аудит 38, A8) ──
    # Удаление мира раньше чистило только 4 таблицы из 8, и карточки/лор/граф удалённых миров
    # оставались навсегда (в боевой БД на момент аудита: entities — 47 миров-сирот, lore — 37,
    # graph_nodes — 31, graph_edges — 26). Идемпотентно: при чистых данных ничего не делает.
    try:
        _orphans = db.prune_orphan_rows()
        if _orphans:
            r["orphans"] = sum(_orphans.values())
            log.info("чистка сирот: убрано %s (строки удалённых миров)",
                     ", ".join(f"{k}:{v}" for k, v in sorted(_orphans.items())))
    except Exception as e:
        log.warning("чистка сирот в БД не удалась (игра стартует с ними): %s", e)

    # ── Правки по всем сохранённым мирам ──
    # (а) цифровые ранги навыков генератор иногда писал числами 1/2/3… вместо букв F/E/D…;
    # (б) машинные `name` карточек (сессия 40, п.9) — правятся ТОЛЬКО там, где состояние
    #     мира знает настоящее имя сущности, движок ничего не выдумывает (закон 3);
    # (в) человекочитаемые названия флагов (п.12) — берутся у сюжета-источника мира;
    # (г) игрок, заведённый как «персонаж окружения» (п.13);
    # (д) карта мира пересобирается из исправленного состояния: узел `npc:Игрок` пережил
    #     бы чистку, пока карту никто не звал (sync ничего не пишет, если граф уже равен
    #     состоянию — проверка дешёвая, два SELECT на мир).
    # Всё идемпотентно: на чистых данных ничего не делает.
    try:
        for _w in db.list_worlds():
            _wid = _w["id"]
            r["worlds"] += 1
            _full = db.get_world(_wid) or {}
            _s = _full.get("setting")
            try:
                _s = json.loads(_s) if isinstance(_s, str) else (_s or {})
            except Exception:
                _s = {}
            if not isinstance(_s, dict):
                continue
            try:
                if narrator.normalize_setting_ranks(_s):
                    r["ranks"] += 1
                    db.update_world(_wid, setting=_s)
            except Exception:
                log.warning("само-исцеление рангов (world %s) пропущено", _wid, exc_info=True)
            try:
                r["cards"] += memory.repair_machine_card_names(_wid, _s)
                _w_flags = flag_titles.repair_flag_titles(_s)
                _w_npc = memory.heal_player_as_npc(_wid, _s)
                r["flags"] += _w_flags
                r["npc"] += _w_npc
                if _w_flags or _w_npc:
                    db.update_world(_wid, setting=_s)
            except Exception:
                log.warning("само-исцеление имён (карточки/флаги/NPC) (world %s) пропущено",
                            _wid, exc_info=True)
            try:
                graph.sync_from_setting(_wid, _s)
            except Exception as _e:
                log.warning("пересинхронизация карты (world %s) после чистки NPC: %s",
                            _wid, _e)
    except Exception:
        log.warning("само-исцеление миров пропущено", exc_info=True)
    return r


async def _flush_pending_vectors(tag: str = "") -> None:
    """Убрать из Chroma векторы карточек, вырезанных само-исцелением (сессия 40, п.13).

    Отдельно от `self_heal()` потому, что корутине нужен активный цикл событий: на импорте
    его нет (A13, аудит 41), поэтому вызывается только из `_lifespan` и из фонового прохода
    после старта. Молча терять их нельзя — иначе RAG и дальше доставал бы «Игрока» как
    персонажа окружения (правило 14).
    """
    if not memory.PENDING_VECTOR_KEYS:
        return
    _pending = list(memory.PENDING_VECTOR_KEYS)
    memory.PENDING_VECTOR_KEYS.clear()
    try:
        await chroma_client.delete_by_ids(_pending)
        log.info("само-исцеление NPC%s: убрано %s векторных карточек из памяти",
                 "" if not tag else f" ({tag})", len(_pending))
    except Exception as e:
        log.warning("чистка векторов удалённых NPC-карточек не удалась: %s", e)


async def _startup_heal() -> None:
    """Проход само-исцеления при старте — в потоке, НЕ на импорте модуля (A13, аудит 41).

    На импорте эти же вызовы стоили: (а) второму экземпляру сервера — `database is locked`
    с полным таймаутом `db._run` на каждой группе (замерено: 12 с против 0.3 с на свободной
    БД), (б) pytest/CI — полную цену сида и обхода всех миров на `import backend.app`.
    Здесь — `bg.spawn` из `_lifespan`: сервер поднимается сразу, исцеление догоняет в фоне
    и одна строка лога говорит, что именно и за сколько было почищено (правило 14).
    """
    t0 = time.perf_counter()
    try:
        r = await asyncio.to_thread(self_heal)
        dt = (time.perf_counter() - t0) * 1000
        fixed = {k: v for k, v in r.items() if k != "worlds" and v}
        log.info("само-исцеление: %s миров за %.0f мс%s",
                 r.get("worlds", 0), dt,
                 "" if not fixed else " (" + ", ".join(f"{k}:{v}" for k, v in
                                                       sorted(fixed.items())) + ")")
    except Exception as e:
        # правило 14: неуспех виден; миры при этом играбельны — исцеление только косметика
        log.warning("само-исцеление при старте не удалось (миры играются как есть): %s",
                    e, exc_info=True)
    finally:
        # ключи могли появиться и в НЕудавшемся проходе: оставить их в памяти Chroma
        # «до следующего перезапуска» — значит держать удалённого «Игрока» в RAG (правило 14)
        await _flush_pending_vectors("после старта")
    # Обратная сверка «вектор ↔ живой мир» (аудит 41, [A1 §5]): сирот в SQLite вычищает
    # `prune_orphan_rows`, а векторы удалённых миров не смотрел НИКТО — при мёртвой Chroma
    # мир не удаляется (503, с36), но векторы, осиротевшие после прямого SQL или отката на
    # бэкап, жили вечно. Проход идемпотентен и молчит на чистых данных; при недоступной
    # Chroma — warning и пропуск (тот же путь, что у остальных стартовых само-исцелений).
    try:
        await memory.sweep_orphan_vectors()
    except Exception as e:
        log.warning("чистка сиротских векторов не удалась (память работает как есть): %s", e,
                    exc_info=True)


async def _startup_tts_preload() -> None:
    """Прогрев голосов Piper/Kokoro (сессия 30) — тоже в фоне после старта (A13):
    инициализация sherpa-onnx на импорте тормозила и pytest, и поднятие сервера."""
    try:
        await asyncio.to_thread(tts.preload_configured_voices)
    except Exception:
        log.warning("предзагрузка голосов TTS не удалась (озвучка прогреется при первом синтезе)",
                    exc_info=True)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    bus.attach_loop(asyncio.get_running_loop())   # синхронный код БД публикует через него
    await _flush_pending_vectors()   # ключи могли появиться до старта цикла (тесты/скрипты)
    cfg = get_config()
    # B2 (аудит 41): RATE_LIMIT_ENABLED из .env/админки применялся бы «сам собой», если бы
    # модуль читал его на импорте — но импорт не должен трогать ничего (правило 26/A13),
    # поэтому выключатель поднимается здесь. По умолчанию лимитер ВКЛЮЧЁН (как и был),
    # false — осознанная отладка/автотесты; отказ лимитера виден в журнале (A6).
    try:
        ratelimit.configure(enabled=bool(cfg.rate_limit_enabled))
    except Exception as e:
        log.warning("rate-limit не настроен (работает с умолчаниями): %s", e)
    log.info("старт: модель=%s контекст_стандарт=%d фон=%s судья=%s мастер=%s враж_ии=%s "
             "события=%s озвучка=%s фоновые_LLM_параллельно=%d rate_limit=%s",
             cfg.main_provider,
             cfg.context_tokens, cfg.background_tasks_enabled, cfg.logic_judge_enabled,
             cfg.autonomous_master_enabled, cfg.enemy_ai_enabled, cfg.dynamic_events_enabled,
             cfg.tts_provider, cfg.llm_bg_concurrency, cfg.rate_limit_enabled)
    # A18: ротация кеша озвучки — сразу после старта (в потоке: большой кеш не должен
    # тормозить поднятие сервера) и далее почасово в фоне.
    _rot_task: asyncio.Task | None = None
    try:
        if cfg.tts_enabled and cfg.tts_cache_enabled and int(cfg.tts_cache_ttl_days or 0) > 0:
            _rot_task = bg.spawn(_tts_cache_keepalive(), name="tts-cache-keepalive")
            bg.spawn(asyncio.to_thread(tts.rotate_tts_cache), name="tts-cache-rotation")
        else:
            log.debug("ротация кеша озвучки отключена (кеш выкл или TTL=0)")
    except Exception as e:
        log.warning("ротация кеша озвучки не запущена (кеш может расти): %s", e)
    # A13 (аудит 41): сид рассказчиков, бэкап БД и все само-исцеления — здесь, а не на
    # импорте модуля; фоном, чтобы старт не ждал ни обхода миров, ни прогрев озвучки.
    bg.spawn(_startup_heal(), name="startup-self-heal")
    if cfg.tts_enabled:
        bg.spawn(_startup_tts_preload(), name="tts-preload")
    try:
        yield
    finally:
        if _rot_task is not None:
            _rot_task.cancel()
        for name, fn in (("chroma_client", chroma_client.close),
                         ("embeddings", embeddings.close),
                         ("llm", llm.close)):
            try:
                await fn()
            except Exception as e:
                log.warning("shutdown: %s не закрылся: %s", name, e)
        try:
            bg.shutdown_nowait()
        except Exception as e:
            log.warning("shutdown: фоновая очередь не остановлена: %s", e)
        # D14 (аудит 41): журнал метрик пишется пачками из буфера — при остановке
        # дочитваем остаток, иначе последние ≤ batch метрик прохождения пропали бы.
        try:
            n = metrics.flush()
            if n:
                log.info("shutdown: журнал метрик дописан (%d записей из буфера)", n)
        except Exception as e:
            log.warning("shutdown: буфер метрик не сброшен: %s", e)
        try:
            await asyncio.to_thread(db.close)   # синхронный close ждёт завершения цикла БД
            log.info("shutdown: соединение БД и цикл aiosqlite закрыты")
        except Exception as e:
            log.warning("shutdown: БД не закрыта корректно (файл может остаться залоченным): %s", e)


app = FastAPI(
    title="Text Game RPG",
    version="1.0",
    lifespan=_lifespan,
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
    wid = int(m.group(1)) if m else None
    tok_w = logsetup._ctx_rid.set(rid)
    tok_r = None
    if m:
        tok_r = logsetup._ctx_world.set(wid)
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
        # D13 (аудит 41): мир передаём ЯВНО полем, а не через контекст-переменную —
        # `_ctx_world` сбрасывается в `finally` выше, а строка длительности пишется
        # после сброса, так что в «GET /api/worlds/3/… → 200 за 12 мс» world_id
        # пропадал (request_id и так шёл явно). Логику middleware не трогаем.
        fields = {"request_id": rid, "status": response.status_code, "ms": round(dt)}
        if wid is not None:
            fields["world_id"] = wid
        log.log(logging.WARNING if response.status_code >= 400 else logging.INFO,
                "%s %s → %s за %.0f мс", request.method, path, response.status_code, dt,
                extra={"fields": fields})
    return response

# ── B3 (аудит 41): внятный ответ на отказ по границе текста ───────────
# Pydantic-схемы режут длину пользовательских текстов (`schemas.*_MAX`), но дефолтный
# 422 — это JSON-список объектов с `loc`/`type`, который фронт показывает как
# «[object Object]» (он читает `data.detail`). Обработчик делает две вещи:
#   1) пишет отказ в журнал (правило 14 — тихого «почему мир не создался» нет);
#   2) отдаёт одну человекочитаемую строку («Слишком длинное значение: prompt
#      (максимум 20000 символов)»). Ветки «тип»/«не задано» — не гипотеза: их тоже
#      ловит этот же текст, и обе покрыты тестом (иначе обработчик портил бы прежние
#      ошибки валидации, превращая их в нечитаемое).
@app.exception_handler(RequestValidationError)
async def _on_validation_error(request, exc: RequestValidationError):
    from fastapi.responses import JSONResponse

    msgs: list[str] = []
    for err in exc.errors():
        field = next((str(p) for p in reversed(err.get("loc", ())) if p not in ("body",)), "")
        etype = str(err.get("type", ""))
        ctx = err.get("ctx") or {}
        if "too_long" in etype:
            unit = "символов" if "string" in etype else "элементов"
            msgs.append(f"Слишком длинное значение: {field or '?'} "
                        f"(максимум {ctx.get('max_length', '?')} {unit})")
        elif any(etype.startswith(p) for p in ("string_", "int_", "float_", "bool_", "list_")):
            # и «не тот тип», и «не разбиралось» (int_parsing / json_invalid): для игрока
            # это одно и то же — поле заполнено не тем, что ждёт API
            msgs.append(f"Неверный тип значения: {field}")
        elif "missing" in etype:
            msgs.append(f"Не задано обязательное поле: {field}")
        else:
            msgs.append(str(err.get("msg") or "Неверные данные запроса"))
    log.warning("422 %s %s: %s", request.method, request.url.path, "; ".join(msgs[:4])
                or "не удалось разобрать ошибки валидации")
    if not msgs:
        msgs = ["Неверные данные запроса"]
    return JSONResponse(status_code=422,
                        content={"detail": msgs[0] if len(msgs) == 1 else msgs})


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

# ── Само-исцеление и сид — НЕ здесь (аудит 41, A13) ──
# Раньше сид рассказчиков, стартовый бэкап БД, `prune_orphan_rows`, нормализация рангов,
# починка имён карточек/флагов/NPC и ремсинхронизация карт исполнялись на `import
# backend.app`: второй экземпляр сервера (или просто открытая другим процессом база) ловил
# `database is locked` с полным таймаутом `db._run` на каждой группе (замерено: 12.6 с
# импорта против 0.3 с на свободной БД), а pytest/CI платили полную цену обхода всех миров.
# Теперь весь этот проход живёт в `self_heal()` и стартует фоном из `_lifespan` (там же,
# где ротация кеша озвучки — A18 поступил точно так же). `import backend.app` больше
# НЕ трогает данные: тестам, которым нужен сид/чистка, достаточно контекстного менеджера
# TestClient (`with TestClient(app) as c:` поднимает lifespan) или вызова `self_heal()`.