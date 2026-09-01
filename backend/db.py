# -*- coding: utf-8 -*-
"""db.py — SQLite: миры, события (полная история), слоты сохранений, настройки генерации.

Доступ к БД выполнен на **aiosqlite** (асинхронный драйвер). Единственное соединение живёт
на выделенном фоновом asyncio-цикле в отдельном потоке; все операции SQLite выполняются через
aiosqlite (не блокируют поток приложения напрямую), а публичный API модуля остался
**синхронным** — каждый вызов ставит корутину в очередь фонового цикла и ждёт результат.

Это сделано осознанно: конвертировать ~250 мест вызовов (роутеры/narrator/tts) в `await`
нельзя без слома двух инвариантов архитектуры —
  1) `db.transaction()` держит `threading.RLock` на время тела блока, а тело ОБЯЗАНО быть
     синхронным (внутри него вызывается `narrator.ensure_knowledge_cards`, который сам
     обращается к db);
  2) `admin_settings.read_overrides()` читает админ-настройки ОТДЕЛЬНЫМ синхронным
     соединением (иначе рекурсия config→db→config вешает сервер).
Поэтому драйвер заменён на aiosqlite, а сигнатуры/семантика сохранены без изменений.

Атомарность прежняя: `threading.RLock` + `_tx_depth` сериализуют транзакции между потоками;
на фоновом цикле в каждый момент выполняется не более одной корутины (следующая ставится
только после завершения предыдущей через future), поэтому операции одного соединения не
перемешиваются.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from concurrent.futures import TimeoutError as FuturesTimeout
import aiosqlite

from .config import get_config

from .logsetup import get_logger

log = get_logger(__name__)

ROOT_PATH = Path(__file__).resolve().parent.parent

# ── Асинхронный движок на фоновом потоке ──────────────────────────────────
_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_thread: Optional[threading.Thread] = None
_conn: Optional[aiosqlite.Connection] = None

# RLock (реентерабельный): db.transaction() держит его на время тела, а внутренние
# функции (add_event/update_world/...) снова берут `with _lock:` в том же потоке — не дедлок.
# Глобально сериализует доступ к БД между потоками (как и раньше).
_lock = threading.RLock()

# Потолок ожидания ответа от БД (секунды). Зависание соединения раньше означало вечный
# стоп вызывающего потока (ход игрока/фоновая задача) без единой строчки в логе.
_RUN_TIMEOUT = 120.0
_stuck_lock = threading.Lock()
_stuck_count = 0

# Атомарные группы записей: пока _tx_depth > 0, внутренние _maybe_commit() ничего не коммитят,
# финальный COMMIT/ROLLBACK делает верхний уровень db.transaction().
_tx_depth = 0


def _loop_main() -> None:
    """Фоновый поток: собственный asyncio-цикл, на котором живёт aiosqlite-соединение."""
    global _loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _loop = loop
    try:
        loop.run_forever()
    finally:
        loop.close()


def _ensure_loop() -> None:
    """Лениво поднимает фоновый поток с циклом (daemon — не мешает завершению процесса)."""
    global _loop_thread
    if _loop is None:
        _loop_thread = threading.Thread(target=_loop_main, name="aiosqlite-loop", daemon=True)
        _loop_thread.start()
        while _loop is None:
            time.sleep(0.001)


def _run(factory: Callable[[], Any]) -> Any:
    """Выполняет корутину (созданную `factory()`) на фоновом цикле и блокирует вызывающий
    поток до получения результата. Исключения из БД (sqlite3.*) пробрасываются как есть.

    Защиты (сессия 36, п.11):
      * вызов ИЗ потока фонового цикла — гарантированный само-дедлок (fut ждёт того же
        цикла, который заблокирован этим же потоком). Раньше такое зависало молча и
        навечно; теперь — явная ошибка с контекстом (правило 14).
      * потолок ожидания: «БД не отвечает» превращается в понятное исключение с логом,
        а не в вечный стоп хода/задач."""
    _ensure_loop()
    if _loop_thread is not None and threading.current_thread() is _loop_thread:
        # рекурсивный вызов с собственного цикла: выполнить нельзя, зависнем навсегда
        raise RuntimeError(
            "db._run() вызван из потока фонового цикла БД — это взаимоблокировка. "
            "Фоновые задачи должны обращаться к БД из своего цикла (asyncio), а не из "
            "корутины, поставленной на цикл db.py.")
    fut = asyncio.run_coroutine_threadsafe(factory(), _loop)  # type: ignore[arg-type]
    try:
        return fut.result(timeout=_RUN_TIMEOUT)
    except FuturesTimeout:
        fut.cancel()
        with _stuck_lock:
            global _stuck_count
            _stuck_count += 1
        log.error("БД не ответила за %s с (одновременных зависаний: %d) — запрос отменён",  # noqa: E501
                  _RUN_TIMEOUT, _stuck_count, exc_info=True)
        raise RuntimeError(f"База данных не отвечает (ждём >{_RUN_TIMEOUT}с)")


async def _shutdown() -> None:
    """Закрыть единственное соединение (работает на фоновом цикле)."""
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


def close() -> None:
    """Корректно закрыть соединение aiosqlite и остановить фоновый цикл.
    Используется в тестах/скриптах для освобождения файла БД при завершении.
    Повторный вызов любой функции БД лениво поднимет цикл и соединение заново."""
    global _loop, _loop_thread
    if _loop is not None:
        try:
            asyncio.run_coroutine_threadsafe(_shutdown(), _loop).result(timeout=10)
        except Exception:
            pass
        try:
            _loop.call_soon_threadsafe(_loop.stop)
        except Exception:
            pass
        if _loop_thread is not None:
            _loop_thread.join(timeout=10)
            _loop_thread = None
        _loop = None


async def _open() -> aiosqlite.Connection:
    """Открыть (лениво) единственное соединение на фоновом цикле + схема + миграции."""
    global _conn
    if _conn is None:
        cfg = get_config()
        path = Path(cfg.db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level='' — как у прежнего sqlite3 по умолчанию: неявный BEGIN на DML,
        # ручной `BEGIN IMMEDIATE` внутри db.transaction() управляет транзакцией явно.
        _conn = await aiosqlite.connect(str(path), isolation_level="")
        _conn.row_factory = aiosqlite.Row
        await _init_schema(_conn)
    return _conn


async def _db() -> aiosqlite.Connection:
    """Внутренний получитель соединения для корутин (используется только в этом модуле)."""
    return await _open()


async def _maybe_commit() -> None:
    """Коммит операции, если мы НЕ внутри обёртки db.transaction() (иначе транзакция
    потеряла бы атомарность — commit из середины закрыл бы всю группу).

    Вызывается только из корутин (с фонового цикла), поэтому коммит выполняется
    напрямую — использовать `_run` здесь нельзя: мы уже на том же цикле, и повторная
    постановка задачи через run_coroutine_threadsafe привела бы к дедлоку.
    """
    global _tx_depth
    if _tx_depth == 0:
        await _commit_now()


async def _commit_now() -> None:
    conn = await _open()
    await conn.commit()
    if _tx_depth == 0:
        _flush_pending_events()


@contextmanager
def transaction() -> Iterator[None]:
    """Группирует несколько записей (add_event/update_world/upsert_entity/...) в одну
    атомарную транзакцию SQLite. ВАЖНО: тело блока должно быть синхронным (без await) —
    на время тела _lock удерживается, другие потоки ждут завершения группы."""
    global _tx_depth
    with _lock:
        if _tx_depth == 0:
            _run(_begin_now)
        _tx_depth += 1
    try:
        yield
    except BaseException:
        with _lock:
            _tx_depth -= 1
            if _tx_depth == 0:
                _run(_rollback_now)
        raise
    else:
        with _lock:
            _tx_depth -= 1
            if _tx_depth == 0:
                _run(_commit_now)


async def _begin_now() -> None:
    conn = await _open()
    await conn.execute("BEGIN IMMEDIATE")


async def _rollback_now() -> None:
    conn = await _open()
    await conn.rollback()
    # откат = этих событий нет: рассылайть их нельзя
    _pending_events.clear()


async def _init_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS worlds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            theme TEXT NOT NULL,
            genre TEXT,
            difficulty TEXT DEFAULT 'normal',
            perspective TEXT DEFAULT 'second',
            language TEXT DEFAULT 'ru',
            custom_hook TEXT DEFAULT '',
            setting TEXT NOT NULL,
            gen_settings TEXT NOT NULL DEFAULT '{}',
            snapshot TEXT,              -- снапшот состояния до последнего хода (для перегенерации)
            created_at REAL,
            updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            world_id INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            role TEXT NOT NULL,          -- narrator|player|system|dice|summary
            content TEXT NOT NULL,
            folded INTEGER DEFAULT 0,
            feedback INTEGER DEFAULT 0,  -- 1 лайк / -1 дизлайк / 0
            meta TEXT NOT NULL DEFAULT '{}',  -- JSON: служебное (memory_used и т.п.)
            ts REAL
        );
        CREATE INDEX IF NOT EXISTS idx_events_world ON events(world_id, seq);
        -- (сессия 34) окно недавней истории и счётчики выбираются по world_id + folded,
        -- сводки/последние обмены — по world_id + role: индексы закрывают эти выборки,
        -- иначе SQLite сканирует все события мира на каждый ход (B5).
        CREATE INDEX IF NOT EXISTS idx_events_world_folded ON events(world_id, folded, seq);
        CREATE INDEX IF NOT EXISTS idx_events_world_role ON events(world_id, role, seq);
        CREATE TABLE IF NOT EXISTS saves (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            world_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            seq INTEGER NOT NULL,
            setting TEXT NOT NULL,
            created_at REAL
        );
        -- Карточки сущностей (NPC, локации, фракции, квесты, предметы, события):
        -- персональная история, отношения с игроком, статус — чтобы мир не забывал.
        CREATE TABLE IF NOT EXISTS entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            world_id INTEGER NOT NULL,
            kind TEXT NOT NULL,           -- npc|location|faction|quest|item|event
            entity_key TEXT NOT NULL,
            name TEXT NOT NULL,
            summary TEXT DEFAULT '',      -- краткое описание/статус сейчас
            relationship TEXT DEFAULT '', -- отношения с игроком ("друг 8/10", "враг")
            bio TEXT DEFAULT '',          -- накопленная история/факты
            meta TEXT NOT NULL DEFAULT '{}',  -- JSON: alive, location, faction, флаги и т.п.
            seq INTEGER DEFAULT 0,        -- seq хода, на котором карточка последний раз обновлена
            created_at REAL,
            updated_at REAL,
            UNIQUE(world_id, kind, entity_key)
        );
        CREATE INDEX IF NOT EXISTS idx_entities_world ON entities(world_id, kind);
        -- Рассказчики: сменяемые персоны повествователя (предустановленные + свои)
        CREATE TABLE IF NOT EXISTS narrators (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            desc TEXT DEFAULT '',
            prompt TEXT NOT NULL,          -- персона/стиль, подставляется в систему рассказчика
            is_preset INTEGER DEFAULT 0,
            created_at REAL,
            updated_at REAL
        );
        -- Свои сюжеты (кастомные заготовки миров): имя + текст сюжета
        CREATE TABLE IF NOT EXISTS story_plots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            plot TEXT NOT NULL,            -- описание сюжета/завязки для генерации мира
            created_at REAL,
            updated_at REAL
        );
        -- Лор мира (библия): статьи, которые рассказчик получает через RAG/сжатие.
        -- Могут быть очень большими; в промпт попадают релевантные/сжатые фрагменты.
        CREATE TABLE IF NOT EXISTS lore (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            world_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,        -- весь текст статьи (может быть большим)
            tags TEXT DEFAULT '',          -- теги/ключевые слова через запятую (для поиска)
            is_core INTEGER DEFAULT 0,     -- якорная статья: всегда даётся в промпт (сжато)
            source TEXT DEFAULT '',        -- theme | custom | user
            created_at REAL,
            updated_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_lore_world ON lore(world_id);
        -- Настройки админки (глобальные дефолты LLM/провайдеров/генерации) — поверх .env
        CREATE TABLE IF NOT EXISTS admin_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at REAL
        );
        -- Кэш озвучки (TTS): аудиофайлы по хешу текста+голоса+скорости
        CREATE TABLE IF NOT EXISTS tts_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text_hash TEXT NOT NULL UNIQUE,
            provider TEXT NOT NULL,
            voice TEXT NOT NULL DEFAULT '',
            rate TEXT NOT NULL DEFAULT '+0%',
            fmt TEXT NOT NULL,
            rel_path TEXT NOT NULL,
            created_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_tts_hash ON tts_cache(text_hash);
        -- 🌐 Графовая БД (карта мира и связи сущностей): персистентный граф вместо плоских
        -- `connections` в setting.locations как источник истины для рёбер/связей. Узлы и рёбра
        -- привязаны к world_id; рёбра неориентированные, хранятся канонически (source <= target).
        CREATE TABLE IF NOT EXISTS graph_nodes (
            world_id INTEGER NOT NULL,
            node_id TEXT NOT NULL,              -- ключ узла (локация "start", фракция "guild" и т.п.)
            label TEXT NOT NULL DEFAULT '',     -- человекочитаемое имя (напр. название локации)
            kind TEXT NOT NULL DEFAULT 'custom',-- location | faction | shop | npc | quest | custom
            data TEXT NOT NULL DEFAULT '{}',    -- JSON: desc и прочие доп. метаданные узла
            created_at REAL,
            updated_at REAL,
            PRIMARY KEY(world_id, node_id)
        );
        CREATE INDEX IF NOT EXISTS idx_graph_nodes_world ON graph_nodes(world_id);
        CREATE TABLE IF NOT EXISTS graph_edges (
            world_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            target TEXT NOT NULL,
            weight REAL NOT NULL DEFAULT 1,     -- стоимость перехода (для взвешенных путей)
            data TEXT NOT NULL DEFAULT '{}',    -- JSON доп. свойства ребра (напр. {type: "дорога"})
            created_at REAL,
            updated_at REAL,
            PRIMARY KEY(world_id, source, target)
        );
        CREATE INDEX IF NOT EXISTS idx_graph_edges_world ON graph_edges(world_id);
        -- Снапшоты состояния по ходам (сессия 34): точка перемотки «назад к ходу N» (C1).
        -- worlds.snapshot хранит только ПОСЛЕДНИй ход (перегенерация); эта таблица — история
        -- состояний перед каждым ходом, чтобы откат/загрузка сохранения были обратимыми.
        CREATE TABLE IF NOT EXISTS turn_snapshots (
            world_id INTEGER NOT NULL,
            seq INTEGER NOT NULL,             -- ход (seq события игрока), ПЕРЕД которым снято состояние
            setting TEXT NOT NULL,            -- JSON: полное состояние мира
            created_at REAL,
            PRIMARY KEY(world_id, seq)
        );
        CREATE INDEX IF NOT EXISTS idx_turn_snap_world ON turn_snapshots(world_id, seq);
        """
    )
    await conn.commit()
    await _migrate_worlds(conn)
    await _migrate_events(conn)


async def _migrate_events(conn: aiosqlite.Connection) -> None:
    """Идемпотентная миграция: колонки TTS и meta у событий."""
    cur = await conn.execute("PRAGMA table_info(events)")
    cols = {r[1] for r in await cur.fetchall()}
    if "tts_status" not in cols:
        await conn.execute("ALTER TABLE events ADD COLUMN tts_status INTEGER NOT NULL DEFAULT 0")
    if "tts_file" not in cols:
        await conn.execute("ALTER TABLE events ADD COLUMN tts_file TEXT NOT NULL DEFAULT ''")
    if "meta" not in cols:
        await conn.execute("ALTER TABLE events ADD COLUMN meta TEXT NOT NULL DEFAULT '{}'")
    await conn.commit()


async def _migrate_worlds(conn: aiosqlite.Connection) -> None:
    """Идемпотентная миграция: добавляем новые колонки worlds."""
    cur = await conn.execute("PRAGMA table_info(worlds)")
    cols = {r[1] for r in await cur.fetchall()}
    if "narrator_id" not in cols:
        await conn.execute("ALTER TABLE worlds ADD COLUMN narrator_id INTEGER")
    if "provider_settings" not in cols:
        await conn.execute("ALTER TABLE worlds ADD COLUMN provider_settings TEXT NOT NULL DEFAULT '{}'")
    if "snapshot" not in cols:
        await conn.execute("ALTER TABLE worlds ADD COLUMN snapshot TEXT")
    if "tts_settings" not in cols:
        await conn.execute("ALTER TABLE worlds ADD COLUMN tts_settings TEXT NOT NULL DEFAULT '{}'")
    try:
        pcur = await conn.execute("PRAGMA table_info(story_plots)")
        pcols = {r[1] for r in await pcur.fetchall()}
        if "lore" not in pcols:
            await conn.execute("ALTER TABLE story_plots ADD COLUMN lore TEXT NOT NULL DEFAULT ''")
    except Exception as e:
        # без колонки «свой сюжет» не сохранит лор — молча терять это нельзя
        log.warning("миграция story_plots.lore не удалась (свои сюжеты будут без лора): %s", e)
    await conn.commit()


# ─────────────────────────── миры ───────────────────────────

def create_world(name: str, theme: str, genre: str, difficulty: str, perspective: str,
                 language: str, custom_hook: str, setting: dict,
                 gen_settings: dict, narrator_id: int | None = None,
                 provider_settings: dict | None = None) -> int:
    with _lock:
        return _run(lambda: _create_world(
            name, theme, genre, difficulty, perspective, language, custom_hook, setting,
            gen_settings, narrator_id, provider_settings))


async def _create_world(name: str, theme: str, genre: str, difficulty: str, perspective: str,
                        language: str, custom_hook: str, setting: dict,
                        gen_settings: dict, narrator_id: int | None,
                        provider_settings: dict | None) -> int:
    conn = await _open()
    cur = await conn.execute(
        "INSERT INTO worlds (name, theme, genre, difficulty, perspective, language, "
        "custom_hook, setting, gen_settings, narrator_id, provider_settings, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (name, theme, genre, difficulty, perspective, language, custom_hook,
         json.dumps(setting, ensure_ascii=False),
         json.dumps(gen_settings, ensure_ascii=False), narrator_id,
         json.dumps(provider_settings or {}, ensure_ascii=False), time.time(), time.time()),
    )
    await _maybe_commit()
    return int(cur.lastrowid)  # type: ignore


def list_worlds() -> list[dict]:
    with _lock:
        return _run(_list_worlds)


async def _list_worlds() -> list[dict]:
    conn = await _open()
    # Сессия 36, п.10: счётчик ходов одним GROUP BY-подзапросом вместо correlated COUNT(*)
    # на каждую строку (на каждом GET /api/worlds). На больших базах это был самый
    # горячий запрос меню.
    cur = await conn.execute(
        "SELECT id, name, theme, genre, difficulty, perspective, language, "
        "COALESCE(ev.c, 0) AS events, updated_at "
        "FROM worlds LEFT JOIN (SELECT world_id, COUNT(*) AS c FROM events "
        "WHERE role = 'player' GROUP BY world_id) ev ON ev.world_id = worlds.id "
        "ORDER BY updated_at DESC"
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


def get_world(world_id: int) -> Optional[dict]:
    with _lock:
        return _run(lambda: _get_world(world_id))


async def _get_world(world_id: int) -> Optional[dict]:
    conn = await _open()
    cur = await conn.execute("SELECT * FROM worlds WHERE id = ?", (world_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


def update_world(world_id: int, **fields: Any) -> None:
    if not fields:
        return
    allowed = {"name", "setting", "gen_settings", "narrator_id", "provider_settings", "snapshot",
               "tts_settings"}
    if "setting" in fields:
        fields["setting"] = json.dumps(fields["setting"], ensure_ascii=False)
    if "gen_settings" in fields:
        fields["gen_settings"] = json.dumps(fields["gen_settings"], ensure_ascii=False)
    if "provider_settings" in fields:
        fields["provider_settings"] = json.dumps(fields["provider_settings"], ensure_ascii=False)
    if "tts_settings" in fields and not isinstance(fields["tts_settings"], str):
        fields["tts_settings"] = json.dumps(fields["tts_settings"], ensure_ascii=False)
    if "snapshot" in fields and not isinstance(fields["snapshot"], str):
        fields["snapshot"] = json.dumps(fields["snapshot"], ensure_ascii=False)
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k} = ?")
            vals.append(v)
    if not sets:
        return
    vals.append(time.time())
    with _lock:
        _run(lambda: _update_world(world_id, sets, vals))


async def _update_world(world_id: int, sets: list[str], vals: list) -> None:
    conn = await _open()
    await conn.execute(f"UPDATE worlds SET {', '.join(sets)}, updated_at = ? WHERE id = ?",
                       (*vals, world_id))
    await _maybe_commit()


def delete_world(world_id: int) -> None:
    with _lock:
        _run(lambda: _delete_world(world_id))


async def _delete_world(world_id: int) -> None:
    # A8 (аудит 38): раньше удалялись только worlds/events/saves/turn_snapshots — карточки,
    # лор и узлы/рёбра графа оставались навсегда (в боевой БД на момент аудита: entities —
    # 47 миров-сирот, lore — 37, graph_nodes — 31, graph_edges — 26). Миры живут под
    # AUTOINCREMENT-id, и после VACUUM/импорта дампа id может совпасть с остатками сирот →
    # чужие карточки/лор всплыли бы в новом мире.
    # tts_cache к событиям НЕ привязан (ключ — хеш текста+голоса), это общий кэш аудио:
    # его прибивает ротация по TTL (A18), а не удаление мира.
    conn = await _open()
    for table in ("entities", "lore", "graph_nodes", "graph_edges",
                  "turn_snapshots", "saves", "events"):
        await conn.execute(f"DELETE FROM {table} WHERE world_id = ?", (world_id,))
    await conn.execute("DELETE FROM worlds WHERE id = ?", (world_id,))
    await _maybe_commit()


# ── Аудит 38 (A8): чистка сирот ─────────────────────────────────────────────
# Строки связанных таблиц, чей world_id больше не принадлежит ни одному миру
# (миры, удалённые ДО появления этих таблиц, и следы прошлых удалений).
# Идемпотентно и безопасно: удаляются только строки, у которых нет родителя в worlds.
ORPHAN_TABLES: tuple[str, ...] = ("entities", "lore", "graph_nodes", "graph_edges",
                                  "events", "saves", "turn_snapshots")


def prune_orphan_rows() -> dict:
    """Убрать строки связанных таблиц у несуществующих миров. Возвращает {таблица: число}.

    Запускается один раз при старте сервера (само-исцеление, как нормылизация рангов в
    app.py). Идемпотентно: при чистых данных ничего не делает."""
    with _lock:
        return _run(_prune_orphan_rows)


async def _prune_orphan_rows() -> dict:
    conn = await _open()
    out: dict[str, int] = {}
    for table in ORPHAN_TABLES:
        cur = await conn.execute(
            f"DELETE FROM {table} WHERE world_id NOT IN (SELECT id FROM worlds)")
        if cur.rowcount:
            out[table] = int(cur.rowcount)
    if out:
        await _maybe_commit()
    return out


# ─────────────────────────── события ───────────────────────────

def add_event(world_id: int, role: str, content: str, seq: int | None = None,
              meta: dict | None = None) -> dict:
    with _lock:
        return _run(lambda: _add_event(world_id, role, content, seq, meta))


# ── Крючок «записано событие» (сессия 34, живые рассылки) ──────────────────────
# Чистое уведомление слоя данных: никакого форматирования/бизнес-логики, слушатель
# (backend/bus.py) навешивается из app.py при старте. Нужен, чтобы фоновые системки
# доходили до вкладки мгновенно, а не через поллинг раз в 15 секунд.
_event_listeners: list = []
# Внутри db.transaction() события копятся и рассылаются ТОЛЬКО после успешного COMMIT:
# иначе при откате половины хода клиент получил бы «фantomные» сообщения, которых в БД нет.
_pending_events: list[dict] = []


def add_event_listener(fn) -> None:
    """Поставить слушателя записанных событий (вызывается с dict события). Идемпотентно."""
    if fn not in _event_listeners:
        _event_listeners.append(fn)


def _notify_event(row: dict) -> None:
    if _tx_depth > 0:
        _pending_events.append(row)
        return
    for fn in list(_event_listeners):
        try:
            fn(row)
        except Exception as e:
            # рассылка не должна ломать запись хода
            log.warning("слушатель события %s не отработал (world %s): %s",
                        getattr(fn, "__name__", fn), row.get("world_id"), e)


def _flush_pending_events() -> None:
    """Разослать накопленные за транзакцию события (после COMMIT)."""
    if not _pending_events:
        return
    batch, _pending_events[:] = list(_pending_events), []
    for ev in batch:
        _notify_event(ev)


async def _add_event(world_id: int, role: str, content: str, seq: int | None,
                     meta: dict | None) -> dict:
    # A11 (аудит 38, страховка слоя данных): пустой текст события — баг вызывающего,
    # а не данные. Раньше на `content=None` SQLite давал голый IntegrityError
    # («NOT NULL constraint failed: events.content») с трейсбеком вместо внятной ошибки,
    # а `content=""` проходил молча и рождал пустое сообщение в чате.
    if content is None or not str(content).strip():
        raise ValueError(f"Пустой content события (world {world_id}, роль {role!r}) — "
                         "вызывающий обязан передать непустой текст")
    conn = await _open()
    if seq is None:
        cur0 = await conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM events WHERE world_id = ?",
                                  (world_id,))
        row = await cur0.fetchone()
        seq = int(row["m"]) + 1
    meta_json = json.dumps(meta or {}, ensure_ascii=False)
    cur = await conn.execute(
        "INSERT INTO events (world_id, seq, role, content, folded, meta, ts) VALUES (?,?,?,?,0,?,?)",
        (world_id, seq, role, content, meta_json, time.time()),
    )
    await _maybe_commit()
    ev = {"id": int(cur.lastrowid), "world_id": world_id, "seq": seq, "role": role,
          "content": content, "folded": 0, "feedback": 0, "tts_status": 0, "tts_file": "",
          "meta": meta or {}}
    _notify_event(ev)
    return ev


def delete_events_by_id(world_id: int, ids: list[int]) -> int:
    """Удалить события по их id (только если принадлежат миру). Используется при перегенерации
    ответа, чтобы заменить события прошлого хода, не трогая фоновые события мира."""
    ids = [i for i in ids if i]
    if not ids:
        return 0
    with _lock:
        return _run(lambda: _delete_events_by_id(world_id, ids))


async def _delete_events_by_id(world_id: int, ids: list[int]) -> int:
    conn = await _open()
    ph = ",".join("?" * len(ids))
    cur = await conn.execute(f"DELETE FROM events WHERE world_id = ? AND id IN ({ph})",
                             [world_id, *ids])
    await _maybe_commit()
    return cur.rowcount


def _event_meta(row) -> dict:
    """Достаёт из БД-строки события словарь служебных данных (meta JSON)."""
    m = dict(row).get("meta") or "{}"
    try:
        parsed = json.loads(m) if isinstance(m, str) else m
    except Exception:
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def _event_obj(row) -> dict:
    d = dict(row)
    d["meta"] = _event_meta(row)
    return d


def get_events(world_id: int, limit: int | None = None, since_seq: int = 0,
               roles: tuple[str, ...] | None = None) -> list[dict]:
    """События мира (хвост `limit` или всё с `since_seq`). `roles` — фильтр по ролям в SQL."""
    with _lock:
        return _run(lambda: _get_events(world_id, limit, since_seq, roles))


def get_events_after(world_id: int, after_seq: int = 0, limit: int = 500) -> list[dict]:
    """ПЕРВЫЕ `limit` событий с seq > after_seq (хронологическом порядке) — для
    постранинного обхода лога вперёд (сессия 36, п.8).

    Отличие от get_events(limit=...): там лимит режет ХВОСТ (последние N), что для
    «пройти весь лог страницами» не годилось бы — страницы перекрывались бы хвостом."""
    with _lock:
        return _run(lambda: _get_events_after(world_id, after_seq, limit))


async def _get_events_after(world_id: int, after_seq: int, limit: int) -> list[dict]:
    conn = await _open()
    cur = await conn.execute(
        "SELECT * FROM events WHERE world_id = ? AND seq > ? ORDER BY seq ASC LIMIT ?",
        (world_id, int(after_seq), max(1, int(limit))))
    return [_event_obj(r) for r in await cur.fetchall()]


async def _get_events(world_id: int, limit: int | None, since_seq: int,
                      roles: tuple[str, ...] | None = None) -> list[dict]:
    conn = await _open()
    role_sql = ""
    rargs: list[Any] = []
    if roles:
        role_sql = " AND role IN (" + ",".join("?" * len(roles)) + ")"
        rargs = list(roles)
    q = "SELECT * FROM events WHERE world_id = ? AND seq > ?" + role_sql + " ORDER BY seq"
    args: list = [world_id, since_seq] + rargs
    if limit:
        q = ("SELECT * FROM (SELECT * FROM events WHERE world_id = ? AND seq > ?" + role_sql
             + " ORDER BY seq DESC LIMIT ?) ORDER BY seq ASC")
        args = [world_id, since_seq] + rargs + [limit]
    cur = await conn.execute(q, args)
    rows = await cur.fetchall()
    return [_event_obj(r) for r in rows]


def get_history_page(world_id: int, before_seq: int = 0, limit: int = 60,
                     roles: tuple[str, ...] | None = None) -> list[dict]:
    """Страница истории (сессия 34, B5): последние `limit` событий раньше before_seq —
    ОГРАНИЧЕННО в SQL. Раньше роутер тянул ВЕСЬ лог мира и резал его в Python, поэтому
    открытие длинного прохождения тем медленнее, чем дальше прошёл игрок.

    `roles` (A10, аудит 38): если задан — только эти роли (роль `summary` и прочие
    служебные строки не должны попадать в чат/лог игрока). Фильтр — в SQL, а не в Python,
    иначе «последние N» резались бы до фильтрации и счётчик пагинации врал."""
    with _lock:
        return _run(lambda: _get_history_page(world_id, before_seq, limit, roles))


async def _get_history_page(world_id: int, before_seq: int, limit: int,
                            roles: tuple[str, ...] | None = None) -> list[dict]:
    conn = await _open()
    args: list[Any] = [world_id]
    where = "world_id = ?"
    if before_seq:
        where += " AND seq < ?"
        args.append(int(before_seq))
    if roles:
        where += " AND role IN (" + ",".join("?" * len(roles)) + ")"
        args.extend(roles)
    n = max(1, int(limit or 0)) if limit else 100000
    args.append(n)
    cur = await conn.execute(
        f"SELECT * FROM (SELECT * FROM events WHERE {where} ORDER BY seq DESC LIMIT ?) ORDER BY seq ASC",
        args)
    return [_event_obj(r) for r in await cur.fetchall()]


def mark_folded(world_id: int, up_to_seq: int,
                roles: tuple[str, ...] = ("player", "narrator")) -> int:
    """Свернуть обмены с seq <= up_to_seq в сводку (folded=FOLD_SUMMARY).
    По умолчанию трогает ТОЛЬКО player/narrator: раньше заодно помечались и сами
    сводки/системные события, из-за чего их стало невозможно отличить/вернуть."""
    with _lock:
        return _run(lambda: _fold_state_range(world_id, FOLD_SUMMARY, 0, up_to_seq, roles))


def latest_seq(world_id: int) -> int:
    with _lock:
        return _run(lambda: _latest_seq(world_id))


async def _latest_seq(world_id: int) -> int:
    conn = await _open()
    cur = await conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM events WHERE world_id = ?",
                             (world_id,))
    row = await cur.fetchone()
    return int(row["m"])


def get_event(event_id: int) -> Optional[dict]:
    with _lock:
        return _run(lambda: _get_event(event_id))


async def _get_event(event_id: int) -> Optional[dict]:
    conn = await _open()
    cur = await conn.execute("SELECT * FROM events WHERE id = ?", (event_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


def set_feedback(event_id: int, value: int, world_id: int | None = None) -> bool:
    """Поставить оценку событию. Возвращает False, если события нет (или оно не этого
    мира — A4-bis: раньше WHERE был только по id, и фидбек чужому миру был возможен).
    Молчаливый no-op превращался в «200 OK» на несуществующем событии."""
    with _lock:
        return _run(lambda: _set_feedback(event_id, value, world_id))


async def _set_feedback(event_id: int, value: int, world_id: int | None) -> bool:
    conn = await _open()
    if world_id is None:
        cur = await conn.execute("UPDATE events SET feedback = ? WHERE id = ?",
                                 (value, event_id))
    else:
        cur = await conn.execute("UPDATE events SET feedback = ? WHERE id = ? AND world_id = ?",
                                 (value, event_id, world_id))
    await _maybe_commit()
    return bool(cur.rowcount)


# ══════════════ свёртка событий: три состояния (сессия 34) ══════════════
#
# Раньше `folded` использовался ОДНИМ значением для двух разных смыслов, и загрузка
# сохранения уничтожала краткосрочную память безвозвратно: `mark_folded(world, save.seq)`
# сворачивал ПРОШЛОЕ (≤ точки), а цикл в роутере — БУДУЩЕЕ. Итог: в recent-окно не
# попадало ничего, развернуть было нельзя (проверено вживую: мир 33 — 9 из 9 свёрнуты).
#
#   0 = FOLD_VISIBLE  — событие в недавнем окне;
#   1 = FOLD_SUMMARY  — свёрнуто в сводку (summarize_and_compress), покрыто ролью summary;
#   2 = FOLD_HIDDEN   — вынуто из таймлайна перемоткой/загрузкой, сводки о нём удалены.
# В промпт recent не попадают ни 1, ни 2 (фильтр folded = 0).

FOLD_VISIBLE = 0
FOLD_SUMMARY = 1
FOLD_HIDDEN = 2


def fold_state_range(world_id: int, state: int, from_seq: int = 0,
                     to_seq: int | None = None,
                     roles: tuple[str, ...] | None = None) -> int:
    """Поставить события мира в состояние `state` для seq ∈ [from_seq; to_seq].
    roles ограничивает роли (напр. только player/narrator). Возвращает число изменённых."""
    with _lock:
        return _run(lambda: _fold_state_range(world_id, state, from_seq, to_seq, roles))


async def _fold_state_range(world_id: int, state: int, from_seq: int, to_seq: int | None,
                            roles: tuple[str, ...] | None) -> int:
    conn = await _open()
    q = "UPDATE events SET folded = ? WHERE world_id = ? AND seq >= ?"
    args: list[Any] = [int(state), world_id, int(from_seq)]
    if to_seq is not None:
        q += " AND seq <= ?"
        args.append(int(to_seq))
    if roles:
        q += " AND role IN (" + ",".join("?" * len(roles)) + ")"
        args.extend(roles)
    cur = await conn.execute(q, args)
    await _maybe_commit()
    return cur.rowcount


def unfold_events(world_id: int, from_seq: int = 0,
                  roles: tuple[str, ...] | None = None) -> int:
    """Вернуть события в недавнее окно (folded=0) — обратимость свёртки/сокрытия."""
    return fold_state_range(world_id, FOLD_VISIBLE, from_seq, None, roles)


def delete_events_after(world_id: int, from_seq: int,
                        roles: tuple[str, ...] | None = None) -> list[int]:
    """Удалить события с seq > from_seq (перемотка таймлайна). Возвращает seq удалённых
    обменов (player/narrator) — роутер по ним чистит векторы в ChromaDB, иначе память
    помнит ходы, которых в таймлайне уже нет (баг A4)."""
    with _lock:
        return _run(lambda: _delete_events_after(world_id, from_seq, roles))


async def _delete_events_after(world_id: int, from_seq: int,
                               roles: tuple[str, ...] | None) -> list[int]:
    conn = await _open()
    q = "SELECT seq, role FROM events WHERE world_id = ? AND seq > ?"
    args: list[Any] = [world_id, int(from_seq)]
    role_sql = ""
    if roles:
        role_sql = " AND role IN (" + ",".join("?" * len(roles)) + ")"
        args.extend(roles)
    cur = await conn.execute(q + role_sql, args)
    rows = await cur.fetchall()
    if not rows:
        return []
    await conn.execute("DELETE FROM events WHERE world_id = ? AND seq > ?" + role_sql,
                       [world_id, int(from_seq), *(roles or ())])
    await _maybe_commit()
    return [int(r["seq"]) for r in rows if r["role"] in ("player", "narrator")]


# ══════════ снапшоты состояния по ходам (перемотка назад, C1) ══════════

def save_turn_snapshot(world_id: int, seq: int, setting: dict, keep: int = 0) -> None:
    """Запомнить состояние мира ПЕРЕД ходом `seq`, чтобы к нему можно было откатиться.
    keep > 0 — держать только последние N снапшотов мира (таблица не растёт бесконечно)."""
    with _lock:
        _run(lambda: _save_turn_snapshot(world_id, seq, setting, keep))


async def _save_turn_snapshot(world_id: int, seq: int, setting: dict, keep: int) -> None:
    conn = await _open()
    await conn.execute(
        "INSERT OR REPLACE INTO turn_snapshots (world_id, seq, setting, created_at) VALUES (?,?,?,?)",
        (world_id, int(seq), json.dumps(setting, ensure_ascii=False), time.time()))
    if keep and keep > 0:
        await conn.execute(
            "DELETE FROM turn_snapshots WHERE world_id = ? AND seq NOT IN "
            "(SELECT seq FROM turn_snapshots WHERE world_id = ? ORDER BY seq DESC LIMIT ?)",
            (world_id, world_id, int(keep)))
    await _maybe_commit()


def get_turn_snapshot(world_id: int, seq: int) -> Optional[dict]:
    """Снапшот состояния перед ходом `seq` (dict) или None."""
    with _lock:
        return _run(lambda: _get_turn_snapshot(world_id, seq))


async def _get_turn_snapshot(world_id: int, seq: int) -> Optional[dict]:
    conn = await _open()
    cur = await conn.execute("SELECT setting FROM turn_snapshots WHERE world_id = ? AND seq = ?",
                             (world_id, int(seq)))
    row = await cur.fetchone()
    if not row:
        return None
    try:
        data = json.loads(row["setting"])
    except Exception as e:
        log.warning("turn_snapshot (world %s, seq %s) не разобран: %s", world_id, seq, e)
        return None
    return data if isinstance(data, dict) else None


def list_turn_snapshots(world_id: int) -> list[dict]:
    """Доступные точки перемотки: [{seq, ts}] по возрастанию."""
    with _lock:
        return _run(lambda: _list_turn_snapshots(world_id))


async def _list_turn_snapshots(world_id: int) -> list[dict]:
    conn = await _open()
    cur = await conn.execute(
        "SELECT seq, created_at FROM turn_snapshots WHERE world_id = ? ORDER BY seq", (world_id,))
    return [{"seq": int(r["seq"]), "ts": r["created_at"]} for r in await cur.fetchall()]


# ══════════ ограниченные выборки вместо «вытащить весь лог» (B5) ══════════

def count_events(world_id: int, roles: tuple[str, ...] | None = None,
                 unfolded_only: bool = False) -> int:
    """Счётчик событий на стороне SQLite (раньше считали len(get_events(..., limit=4000)))."""
    with _lock:
        return _run(lambda: _count_events(world_id, roles, unfolded_only))


async def _count_events(world_id: int, roles: tuple[str, ...] | None,
                        unfolded_only: bool) -> int:
    conn = await _open()
    q = "SELECT COUNT(*) AS c FROM events WHERE world_id = ?"
    args: list[Any] = [world_id]
    if unfolded_only:
        q += " AND folded = 0"
    if roles:
        q += " AND role IN (" + ",".join("?" * len(roles)) + ")"
        args.extend(roles)
    cur = await conn.execute(q, args)
    row = await cur.fetchone()
    return int(row["c"]) if row else 0


def get_unfolded_events(world_id: int, limit: int = 120,
                        roles: tuple[str, ...] = ("player", "narrator")) -> list[dict]:
    """Последние `limit` несвёрнутых обменов в хронологическом порядке — окно недавней
    истории. Ограничено в SQL (раньше тянули ВСЕ события мира и резали в Python)."""
    with _lock:
        return _run(lambda: _get_unfolded_events(world_id, limit, roles))


async def _get_unfolded_events(world_id: int, limit: int,
                               roles: tuple[str, ...]) -> list[dict]:
    conn = await _open()
    n = max(2, int(limit))
    q = ("SELECT * FROM (SELECT * FROM events WHERE world_id = ? AND folded = 0"
         " AND role IN (" + ",".join("?" * len(roles)) + ")"
         " ORDER BY seq DESC LIMIT ?) ORDER BY seq ASC")
    cur = await conn.execute(q, [world_id, *roles, n])
    return [_event_obj(r) for r in await cur.fetchall()]


def get_turn_events(world_id: int, from_seq: int, to_seq: int | None = None,
                    roles: tuple[str, ...] = ("player", "narrator"),
                    unfolded_only: bool = True) -> list[dict]:
    """События ролей в диапазоне seq (для архивариуса/хроники). to_seq=None — без верха."""
    with _lock:
        return _run(lambda: _get_turn_events(world_id, from_seq, to_seq, roles, unfolded_only))


async def _get_turn_events(world_id: int, from_seq: int, to_seq: int | None,
                           roles: tuple[str, ...], unfolded_only: bool) -> list[dict]:
    conn = await _open()
    q = "SELECT * FROM events WHERE world_id = ? AND seq >= ?"
    args: list[Any] = [world_id, int(from_seq)]
    if to_seq is not None:
        q += " AND seq <= ?"
        args.append(int(to_seq))
    if roles:
        q += " AND role IN (" + ",".join("?" * len(roles)) + ")"
        args.extend(roles)
    if unfolded_only:
        q += " AND folded = 0"
    q += " ORDER BY seq"
    cur = await conn.execute(q, args)
    return [_event_obj(r) for r in await cur.fetchall()]


def get_latest_by_role(world_id: int, role: str, before_seq: int = 0) -> Optional[dict]:
    """Последнее событие роли (опц. — раньше before_seq). Одна строка, а не весь лог."""
    with _lock:
        return _run(lambda: _get_latest_by_role(world_id, role, before_seq))


async def _get_latest_by_role(world_id: int, role: str, before_seq: int) -> Optional[dict]:
    conn = await _open()
    q = "SELECT * FROM events WHERE world_id = ? AND role = ?"
    args: list[Any] = [world_id, role]
    if before_seq:
        q += " AND seq < ?"
        args.append(int(before_seq))
    q += " ORDER BY seq DESC LIMIT 1"
    cur = await conn.execute(q, args)
    row = await cur.fetchone()
    return _event_obj(row) if row else None


def get_last_exchange(world_id: int) -> tuple[str, str]:
    """Последняя пара (действие игрока, ответ рассказчика) — двумя крошечными запросами
    вместо всего лога (нужно Провидению и автономному мастеру)."""
    ev_n = get_latest_by_role(world_id, "narrator")
    reply = ev_n["content"] if ev_n else ""
    guard = int(ev_n["seq"]) if ev_n else 0
    ev_p = get_latest_by_role(world_id, "player", before_seq=guard + 1)
    return (ev_p["content"] if ev_p else ""), reply


def get_summary_events(world_id: int, limit: int = 10, unfolded_only: bool = True) -> list[dict]:
    """Последние `limit` сводок (роль summary) хронологически. limit=0 — все сводки.

    unfolded_only (сессия 35, баг 2): по умолчанию отдаём ТОЛЬКО живые сводки (folded=0).
    Сводки, сокрытые перемоткой/загрузкой сохранения (folded=FOLD_HIDDEN), в промпт не
    должны попадать — иначе рассказчик «помнит» отменённое. Старые вызовы (rewind, тесты)
    явно передают unfolded_only=False, когда им нужны все строки для анализа/чистки.
    """
    with _lock:
        return _run(lambda: _get_summary_events(world_id, limit, unfolded_only))


async def _get_summary_events(world_id: int, limit: int, unfolded_only: bool) -> list[dict]:
    conn = await _open()
    fold_sql = " AND folded = 0" if unfolded_only else ""
    if limit:
        cur = await conn.execute(
            "SELECT * FROM (SELECT * FROM events WHERE world_id = ? AND role = 'summary'"
            + fold_sql
            + " ORDER BY seq DESC LIMIT ?) ORDER BY seq ASC", (world_id, int(limit)))
    else:
        cur = await conn.execute(
            "SELECT * FROM events WHERE world_id = ? AND role = 'summary'" + fold_sql
            + " ORDER BY seq",
            (world_id,))
    return [_event_obj(r) for r in await cur.fetchall()]


def delete_summaries_after(world_id: int, from_seq: int) -> list[int]:
    """Удалить сводки с seq > from_seq (перемотка: сводка о «будущем» недостоверна).
    Возвращает seq удалённых — для чистки векторов в ChromaDB."""
    with _lock:
        return _run(lambda: _delete_summaries_after(world_id, from_seq))


async def _delete_summaries_after(world_id: int, from_seq: int) -> list[int]:
    conn = await _open()
    cur = await conn.execute(
        "SELECT seq FROM events WHERE world_id = ? AND role = 'summary' AND seq > ? ORDER BY seq",
        (world_id, int(from_seq)))
    rows = await cur.fetchall()
    if not rows:
        return []
    await conn.execute(
        "DELETE FROM events WHERE world_id = ? AND role = 'summary' AND seq > ?",
        (world_id, int(from_seq)))
    await _maybe_commit()
    return [int(r["seq"]) for r in rows]


def delete_events_by_seq(world_id: int, seqs: list[int]) -> int:
    """Удалить события по их seq (перемотка: недостоверные сводки с seq < точки отката,
    покрывающие откатываемый диапазон). Возвращает число удалённых."""
    seqs = sorted({int(s) for s in seqs})
    if not seqs:
        return 0
    with _lock:
        return _run(lambda: _delete_events_by_seq(world_id, seqs))


async def _delete_events_by_seq(world_id: int, seqs: list[int]) -> int:
    conn = await _open()
    marks = ",".join("?" * len(seqs))
    cur = await conn.execute(
        f"DELETE FROM events WHERE world_id = ? AND seq IN ({marks})",
        (world_id, *seqs))
    await _maybe_commit()
    return cur.rowcount


# ─────────────────────────── озвучка (TTS) ───────────────────────────

TTTSS_STATUS = {0: "выкл", 1: "готовится", 2: "готово", -1: "ошибка"}


def set_event_tts(event_id: int, status: int, file_rel: str = "") -> None:
    """Статус озвучки события: 0=нет, 1=в работе, 2=готово, -1=ошибка."""
    with _lock:
        _run(lambda: _set_event_tts(event_id, status, file_rel))


async def _set_event_tts(event_id: int, status: int, file_rel: str) -> None:
    conn = await _open()
    await conn.execute("UPDATE events SET tts_status = ?, tts_file = ? WHERE id = ?",
                       (int(status), file_rel or "", event_id))
    await _maybe_commit()


def add_tts_cache(text_hash: str, provider: str, voice: str, rate: str, fmt: str, rel_path: str) -> None:
    with _lock:
        _run(lambda: _add_tts_cache(text_hash, provider, voice, rate, fmt, rel_path))


async def _add_tts_cache(text_hash: str, provider: str, voice: str, rate: str, fmt: str, rel_path: str) -> None:
    conn = await _open()
    await conn.execute(
        "INSERT OR REPLACE INTO tts_cache (text_hash, provider, voice, rate, fmt, rel_path, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (text_hash, provider, voice, rate, fmt, rel_path, time.time()),
    )
    await _maybe_commit()


def find_tts_cache(text_hash: str) -> list[dict]:
    with _lock:
        return _run(lambda: _find_tts_cache(text_hash))


async def _find_tts_cache(text_hash: str) -> list[dict]:
    conn = await _open()
    cur = await conn.execute("SELECT * FROM tts_cache WHERE text_hash = ?", (text_hash,))
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


def count_tts_cache() -> int:
    with _lock:
        return _run(_count_tts_cache)


async def _count_tts_cache() -> int:
    conn = await _open()
    cur = await conn.execute("SELECT COUNT(*) AS c FROM tts_cache")
    row = await cur.fetchone()
    return int(row["c"])


def all_tts_cache_paths() -> list[dict]:
    """Все относительные пути файлов кеша озвучки (для обхода сирот, аудит 38, A18).
    Только rel_path — полный дамп таблицы для этого не нужен."""
    with _lock:
        return _run(_all_tts_cache_paths)


async def _all_tts_cache_paths() -> list[dict]:
    conn = await _open()
    cur = await conn.execute("SELECT rel_path FROM tts_cache")
    return [{"rel_path": r["rel_path"]} for r in await cur.fetchall()]


def prune_tts_cache(ttl_days: int = 60) -> int:
    """Удаляет старые записи кэша озвучки, если TTL > 0. Возвращает число удалённых.

    Аудит 38 (A18): функция жила с сессии, но не вызывалась НИОТКУДА — настройка
    `TTS_CACHE_TTL_DAYS` была декоративной. Теперь её крутит `tts.rotate_tts_cache`
    (старт сервера + фоновый интервал)."""
    if not ttl_days:
        return 0
    cutoff = time.time() - ttl_days * 86400
    with _lock:
        return _run(lambda: _prune_tts_cache(cutoff))


async def _prune_tts_cache(cutoff: float) -> int:
    conn = await _open()
    cur = await conn.execute("SELECT rel_path FROM tts_cache WHERE created_at < ?", (cutoff,))
    rows = await cur.fetchall()
    deleted = 0
    for r in rows:
        p = ROOT_PATH / "data" / r["rel_path"]
        try:
            if p.exists():
                p.unlink()
        except Exception as e:
            # файл кэша остался на диске (занят плеером/антивирусом) — не фатально
            log.debug("кэш озвучки: файл %s не удалён: %s", p, e)
        deleted += 1
    await conn.execute("DELETE FROM tts_cache WHERE created_at < ?", (cutoff,))
    await _maybe_commit()
    return deleted


# ─────────────────────── карточки сущностей ───────────────────────

def upsert_entity(world_id: int, kind: str, entity_key: str, *,
                  name: Optional[str] = None, summary: Optional[str] = None,
                  relationship: Optional[str] = None, bio_add: Optional[str] = None,
                  meta: Optional[dict] = None, seq: int = 0) -> dict:
    """Создаёт или обновляет карточку сущности. bio_add дописывается в историю."""
    with _lock:
        return _run(lambda: _upsert_entity(world_id, kind, entity_key, name=name, summary=summary,
                                           relationship=relationship, bio_add=bio_add,
                                           meta=meta, seq=seq))


async def _upsert_entity(world_id: int, kind: str, entity_key: str, *,
                         name: Optional[str], summary: Optional[str],
                         relationship: Optional[str], bio_add: Optional[str],
                         meta: Optional[dict], seq: int) -> dict:
    conn = await _open()
    cur = await conn.execute(
        "SELECT * FROM entities WHERE world_id = ? AND kind = ? AND entity_key = ?",
        (world_id, kind, entity_key),
    )
    row = await cur.fetchone()
    now = time.time()
    if row:
        cur_meta = json.loads(row["meta"] or "{}")
        if meta:
            cur_meta.update(meta)
        bio = row["bio"] or ""
        if bio_add:
            add = bio_add.strip()
            # Дедупликация: не дописываем повторно то, что уже есть в истории (баг: квесты
            # каждый ход дописывали свой desc в bio → карточка превращалась в простыню).
            # Сравниваем по началу (первые ~80 симв.) — надёжно для одинаковых desc квестов.
            if add and add[:80] not in (bio or ""):
                bio = (bio + "\n• " + add) if bio else add
                # ограничиваем историю последними ~2500 символами (память, не простыня)
                if len(bio) > 2500:
                    bio = "…" + bio[-2450:]
        await conn.execute(
            "UPDATE entities SET name = ?, summary = ?, relationship = ?, bio = ?, "
            "meta = ?, seq = ?, updated_at = ? WHERE id = ?",
            (name or row["name"], summary if summary is not None else row["summary"],
             relationship if relationship is not None else row["relationship"],
             bio, json.dumps(cur_meta, ensure_ascii=False),
             max(seq, int(row["seq"])), now, row["id"]),
        )
        await _maybe_commit()
        cur2 = await conn.execute("SELECT * FROM entities WHERE id = ?", (row["id"],))
        return dict(await cur2.fetchone())
    ent = {
        "world_id": world_id, "kind": kind, "entity_key": entity_key,
        "name": name or entity_key,
        "summary": summary or "", "relationship": relationship or "",
        "bio": bio_add.strip() if bio_add else "",
        "meta": json.dumps(meta or {}, ensure_ascii=False),
        "seq": seq, "created_at": now, "updated_at": now,
    }
    cur3 = await conn.execute(
        "INSERT INTO entities (world_id, kind, entity_key, name, summary, relationship, bio, meta, seq, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (ent["world_id"], ent["kind"], ent["entity_key"], ent["name"], ent["summary"],
         ent["relationship"], ent["bio"], ent["meta"], ent["seq"], ent["created_at"], ent["updated_at"]),
    )
    await _maybe_commit()
    ent["id"] = int(cur3.lastrowid)
    return ent


def list_entities(world_id: int, kind: Optional[str] = None,
                  limit: Optional[int] = None) -> list[dict]:
    """Карточки мира. `limit` — максимум последних (по updated_at) карточек; нужен
    горячим путям на длинных играх (сессия 36, п.9): полная выдача тысяч карточек
    каждый ход раздувал CPU/память. Без limit — все (UI, экспорт, индексация)."""
    with _lock:
        return _run(lambda: _list_entities(world_id, kind, limit))


async def _list_entities(world_id: int, kind: Optional[str],
                         limit: Optional[int]) -> list[dict]:
    conn = await _open()
    n = int(limit) if limit and int(limit) > 0 else None
    if kind:
        q = ("SELECT * FROM entities WHERE world_id = ? AND kind = ? ORDER BY updated_at DESC")
        args: list = [world_id, kind]
    else:
        q = ("SELECT * FROM entities WHERE world_id = ? ORDER BY updated_at DESC")
        args = [world_id]
    if n:
        q += " LIMIT ?"
        args.append(n)
    cur = await conn.execute(q, args)
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


def get_entity(world_id: int, kind: str, entity_key: str) -> Optional[dict]:
    with _lock:
        return _run(lambda: _get_entity(world_id, kind, entity_key))


async def _get_entity(world_id: int, kind: str, entity_key: str) -> Optional[dict]:
    conn = await _open()
    cur = await conn.execute(
        "SELECT * FROM entities WHERE world_id = ? AND kind = ? AND entity_key = ?",
        (world_id, kind, entity_key),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


def delete_entity(world_id: int, kind: str, entity_key: str) -> None:
    with _lock:
        _run(lambda: _delete_entity(world_id, kind, entity_key))


async def _delete_entity(world_id: int, kind: str, entity_key: str) -> None:
    conn = await _open()
    await conn.execute("DELETE FROM entities WHERE world_id = ? AND kind = ? AND entity_key = ?",
                       (world_id, kind, entity_key))
    await _maybe_commit()


# ─────────────────────────── сохранения ───────────────────────────

def create_save(world_id: int, name: str, setting: dict, seq: int) -> int:
    with _lock:
        return _run(lambda: _create_save(world_id, name, setting, seq))


async def _create_save(world_id: int, name: str, setting: dict, seq: int) -> int:
    conn = await _open()
    cur = await conn.execute(
        "INSERT INTO saves (world_id, name, seq, setting, created_at) VALUES (?,?,?,?,?)",
        (world_id, name, seq, json.dumps(setting, ensure_ascii=False), time.time()),
    )
    await _maybe_commit()
    return int(cur.lastrowid)  # type: ignore


def upsert_auto_save(world_id: int, setting: dict, seq: int) -> Optional[int]:
    """Автосохранение: хранится в одном выделенном слоте (name='auto') за мир.
    Перезаписывает предыдущее автосохранение вместо создания нового слота."""
    with _lock:
        return _run(lambda: _upsert_auto_save(world_id, setting, seq))


async def _upsert_auto_save(world_id: int, setting: dict, seq: int) -> Optional[int]:
    conn = await _open()
    # убираем старый слот-автосохранение этого мира
    await conn.execute("DELETE FROM saves WHERE world_id = ? AND name = 'auto'", (world_id,))
    cur = await conn.execute(
        "INSERT INTO saves (world_id, name, seq, setting, created_at) VALUES (?,?,?,?,?)",
        (world_id, "auto", seq, json.dumps(setting, ensure_ascii=False), time.time()),
    )
    await _maybe_commit()
    return int(cur.lastrowid)  # type: ignore


def list_saves(world_id: int) -> list[dict]:
    with _lock:
        return _run(lambda: _list_saves(world_id))


async def _list_saves(world_id: int) -> list[dict]:
    conn = await _open()
    cur = await conn.execute(
        "SELECT id, name, seq, created_at FROM saves WHERE world_id = ? ORDER BY created_at DESC",
        (world_id,),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


def get_save(save_id: int) -> Optional[dict]:
    with _lock:
        return _run(lambda: _get_save(save_id))


async def _get_save(save_id: int) -> Optional[dict]:
    conn = await _open()
    cur = await conn.execute("SELECT * FROM saves WHERE id = ?", (save_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


def delete_save(save_id: int) -> None:
    with _lock:
        _run(lambda: _delete_save(save_id))


async def _delete_save(save_id: int) -> None:
    conn = await _open()
    await conn.execute("DELETE FROM saves WHERE id = ?", (save_id,))
    await _maybe_commit()


def export_data(world_id: int) -> tuple[Optional[dict], list[dict]]:
    """Чистое получение данных для экспорта истории (мир + события) — без форматирования.

    Форматирование в текст идёт в бизнес-слое (routers/worlds.py::render_export_history),
    чтобы db.py оставался чисто слоем доступа к данным."""
    with _lock:      # мир и его события — один согласованный снимок (см. world_dump)
        w = get_world(world_id)
        if not w:
            return None, []
        return w, get_events(world_id)


# ─────────────────────────── дамп мира (переносимость) ───────────────────────────

DUMP_FORMAT = "textgame.world.dump"
# v2 (сессия 34): в дамп добавлены точки перемотки (turn_snapshots). Читатель терпит
# дампы v1 — просто без истории состояний по ходам (перемотка будет недоступна).
DUMP_VERSION = 2


def backup_database(keep: int = 10) -> Optional[str]:
    """Консистентный бэкап файла БД в data/backups/ (имя game-ГГГГММДД-ЧЧМССС.db).

    Использует SQLite Online Backup API через ОТДЕЛЬНОЕ чтение, поэтому снимок
    согласованный даже при живых ходах (в отличие от простого copy). Возвращает путь
    к созданному файлу или None (БД ещё не создана / ошибка). Старые снимки срезаются
    до `keep` штук, чтобы папка не разрасталась. Никогда не бросает — бэкап не должен
    ронять старт сервера."""
    try:
        cfg = get_config()
        src = Path(cfg.db_path)
        if not src.exists():
            return None
        dst_dir = src.parent / "backups"
        dst_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dst = dst_dir / f"{src.stem}-{stamp}.db"
        n = 0
        while dst.exists():
            n += 1
            dst = dst_dir / f"{src.stem}-{stamp}-{n}.db"
        con = sqlite3.connect(str(src))
        try:
            out = sqlite3.connect(str(dst))
            try:
                with out:
                    con.backup(out)
            finally:
                out.close()
        finally:
            con.close()
        # ротация: держим только последние `keep` снимков
        if keep > 0:
            snaps = sorted(dst_dir.glob(f"{src.stem}-*.db"),
                           key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
            for old in snaps[keep:]:
                try:
                    old.unlink()
                except OSError as e:
                    log.warning("не удалось удалить старый бэкап %s: %s", old, e)
        return str(dst)
    except Exception as e:  # бэкап — не причина валить сервер
        log.warning("backup_database: %s", e)
        return None


DUMP_SECRET_KEYS = ("api_key", "apikey")


def _dump_strip_secrets(obj: Any) -> Any:
    """Рекурсивно вырезает из дамп-структуры поля-секреты (api_key и т.п.).

    Обходит dict/list, а также JSON-строки (setting/saves/turn_snapshots хранятся
    как текст): если строка разбирается в JSON-структуру с секретным ключом —
    пересобирается без него. Иначе возвращается как есть (обычный текст не трогаем)."""
    if isinstance(obj, dict):
        return {k: _dump_strip_secrets(v) for k, v in obj.items()
                if not any(h in str(k).lower() for h in DUMP_SECRET_KEYS)}
    if isinstance(obj, list):
        return [_dump_strip_secrets(x) for x in obj]
    if isinstance(obj, str) and len(obj) > 1 and obj.lstrip()[:1] in ("{", "["):
        try:
            parsed = json.loads(obj)
        except Exception:
            return obj
        if isinstance(parsed, (dict, list)) and _dump_has_secrets(parsed):
            return json.dumps(_dump_strip_secrets(parsed), ensure_ascii=False)
        return obj
    return obj


def _dump_has_secrets(obj: Any) -> bool:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if any(h in str(k).lower() for h in DUMP_SECRET_KEYS):
                return True
            if _dump_has_secrets(v):
                return True
        return False
    if isinstance(obj, list):
        return any(_dump_has_secrets(x) for x in obj)
    if isinstance(obj, str) and len(obj) > 1 and obj.lstrip()[:1] in ("{", "["):
        try:
            return _dump_has_secrets(json.loads(obj))
        except Exception:
            return False
    return False


def world_dump(world_id: int) -> Optional[dict]:
    """Собирает ПОЛНЫЙ JSON-дамп мира: состояние, все события (включая свёрнутые),
    карточки сущностей и знаний, лор, слоты сохранений, граф карты, настройки.

    Ключи API из provider_settings ВЫРЕЗАЮТСЯ (поле api_key выбрасывается) — дамп
    предназначен для переноса/бэкапа файлом, а не для хранения секретов. Провайдеры
    после импорта резолвятся заново из глобального .env/админки.

    Возвращает None, если мира нет."""
    # Весь снимок берётся ПОД _lock (сессия 36, п.11): дамп читает семь таблиц отдельными
    # запросами, и без блокировки между ними мог вклиниться чужой `db.transaction()` —
    # наружу ушёл бы мир «наполовину из хода» (события есть, состояния нет).
    # _lock — RLock, вложенные get_world/get_narrator его же и берут (реентерно, ок).
    with _lock:
        return _world_dump_locked(world_id)


def _world_dump_locked(world_id: int) -> Optional[dict]:
    w = get_world(world_id)
    if not w:
        return None

    def _j(field: str, default):
        try:
            return json.loads(w.get(field) or "")
        except Exception:
            return default

    prov = _j("provider_settings", {})
    if isinstance(prov, dict):
        prov = {k: {kk: vv for kk, vv in (v or {}).items() if kk != "api_key"}
                if isinstance(v, dict) else v for k, v in prov.items()}
    else:
        prov = {}

    events = [_event_obj(r) for r in _run(lambda: _dump_events(world_id))]
    entities = _run(lambda: _dump_entities(world_id))
    lore_rows = _run(lambda: _dump_lore(world_id))
    saves = _run(lambda: _dump_saves(world_id))
    nodes = _run(lambda: _dump_graph_nodes(world_id))
    edges = _run(lambda: _dump_graph_edges(world_id))
    snaps = _run(lambda: _dump_turn_snapshots(world_id))

    narrator_name = ""
    if w.get("narrator_id"):
        try:
            narrator_name = (get_narrator(int(w["narrator_id"])) or {}).get("name") or ""
        except Exception:
            narrator_name = ""

    dump = {
        "format": DUMP_FORMAT,
        "version": DUMP_VERSION,
        "exported_at": time.time(),
        "world": {
            "name": w["name"],
            "theme": w["theme"],
            "genre": w.get("genre") or "",
            "difficulty": w.get("difficulty") or "normal",
            "perspective": w.get("perspective") or "second",
            "language": w.get("language") or "ru",
            "custom_hook": w.get("custom_hook") or "",
            "setting": _j("setting", {}),
            "gen_settings": _j("gen_settings", {}),
            "provider_settings": prov,
            "tts_settings": _j("tts_settings", {}),
            "snapshot": w.get("snapshot") or "",
            "narrator_name": narrator_name,
            "created_at": w.get("created_at"),
            "updated_at": w.get("updated_at"),
        },
        "events": events,
        "entities": entities,
        "lore": lore_rows,
        "saves": saves,
        "turn_snapshots": snaps,
        "graph": {"nodes": nodes, "edges": edges},
        "counts": {"events": len(events), "entities": len(entities), "lore": len(lore_rows),
                   "saves": len(saves), "graph_nodes": len(nodes), "turn_snapshots": len(snaps)},
    }
    # Правило 3/15 (страховка, сессия 36, п.18): рекурсивно вырезаем ЛЮБЫЕ поля api_key
    # во всём дампе, а не только в provider_settings. Дамп уходит наружу файлом и может
    # содержать вложенные JSON-строки (setting/saves/turn_snapshots) — если когда-нибудь
    # ключ туда и попадёт (старые сохранения, чужая правка), он всё равно не утечёт.
    return _dump_strip_secrets(dump)


async def _dump_events(world_id: int):
    conn = await _open()
    cur = await conn.execute("SELECT * FROM events WHERE world_id = ? ORDER BY seq", (world_id,))
    return await cur.fetchall()


async def _dump_entities(world_id: int):
    conn = await _open()
    cur = await conn.execute("SELECT * FROM entities WHERE world_id = ? ORDER BY id", (world_id,))
    return [dict(r) for r in await cur.fetchall()]


async def _dump_lore(world_id: int):
    conn = await _open()
    cur = await conn.execute("SELECT * FROM lore WHERE world_id = ? ORDER BY id", (world_id,))
    return [dict(r) for r in await cur.fetchall()]


async def _dump_saves(world_id: int):
    conn = await _open()
    cur = await conn.execute("SELECT * FROM saves WHERE world_id = ? ORDER BY id", (world_id,))
    return [dict(r) for r in await cur.fetchall()]


async def _dump_graph_nodes(world_id: int):
    conn = await _open()
    cur = await conn.execute("SELECT * FROM graph_nodes WHERE world_id = ? ORDER BY node_id", (world_id,))
    return [dict(r) for r in await cur.fetchall()]


async def _dump_graph_edges(world_id: int):
    conn = await _open()
    cur = await conn.execute("SELECT * FROM graph_edges WHERE world_id = ? ORDER BY source, target", (world_id,))
    return [dict(r) for r in await cur.fetchall()]


async def _dump_turn_snapshots(world_id: int):
    """Точки перемотки мира (сессия 34): setting остаётся строкой JSON — как в таблице."""
    conn = await _open()
    cur = await conn.execute(
        "SELECT seq, setting, created_at FROM turn_snapshots WHERE world_id = ? ORDER BY seq",
        (world_id,))
    return [dict(r) for r in await cur.fetchall()]


def restore_world(data: dict) -> int:
    """Создаёт НОВЫЙ мир из дампа (`world_dump`): состояние, события, карточки, лор,
    слоты, граф. Идемпотенности нет — каждый вызов заводит отдельный мир (перенос не
    должен молча затирать чужое сохранение). Возвращает id нового мира.

    Бросает ValueError, если дамп не похож на дамп мира."""
    if not isinstance(data, dict) or data.get("format") != DUMP_FORMAT:
        raise ValueError("не формат дампа мира")
    w = data.get("world")
    if not isinstance(w, dict) or not isinstance(w.get("setting"), dict):
        raise ValueError("в дампе нет world.setting")

    # рассказчик — по имени (id в другой базе не совпадёт)
    narrator_id: Optional[int] = None
    nm = (w.get("narrator_name") or "").strip()
    if nm:
        try:
            narrator_id = next((int(n["id"]) for n in list_narrators() if n["name"] == nm), None)
        except Exception:
            narrator_id = None

    setting = w["setting"]
    # Валидация/само-исцеление структуры состояния (сессия 36, п.5): импорт может прийти
    # из дампа старой/битой версии. Без player/статов ход падал бы с KeyError где-то в
    # движке уже ПОСЛЕ создания мира. Правки идемпотентны и только досылают недостающее
    # (ensure_player_schema / normalize_setting_ranks) — чужие данные не переписываются.
    try:
        from .mechanics import ensure_player_schema, normalize_setting_ranks
        if not isinstance(setting.get("player"), dict):
            log.warning("restore_world: в дампе нет player — достраивается дефолтный")
            setting["player"] = {}
        ensure_player_schema(setting["player"])
        normalize_setting_ranks(setting)
    except Exception as e:
        log.warning("restore_world: схема состояния не доведена (%s): %s",
                    w.get("name") or "?", e)
    # снапшот хода/перегенерации — из дампа, он относится именно к этому состоянию
    new_id = create_world(
        name=w.get("name") or "Восстановленный мир",
        theme=w.get("theme") or "custom",
        genre=w.get("genre") or "",
        difficulty=w.get("difficulty") or "normal",
        perspective=w.get("perspective") or "second",
        language=w.get("language") or "ru",
        custom_hook=w.get("custom_hook") or "",
        setting=setting,
        gen_settings=w.get("gen_settings") or {},
        narrator_id=narrator_id,
        provider_settings=w.get("provider_settings") or {},
    )
    with _lock:
        _run(lambda: _restore_world(new_id, data))
    if w.get("tts_settings"):
        update_world(new_id, tts_settings=w["tts_settings"])
    if w.get("snapshot"):
        update_world(new_id, snapshot=w["snapshot"])
    return new_id


async def _restore_world(new_id: int, data: dict) -> None:
    conn = await _open()
    # Битые строки пропускаем с логом (сессия 36, п.5): одна мусорная запись в дампе
    # не должна ронять весь импорт (иначе транзакция откатится и мир останется пустым).
    for e in data.get("events") or []:
        if not isinstance(e, dict) or "seq" not in e or "role" not in e:
            log.warning("restore_world (world %s): событие без seq/role пропущено: %r",
                        new_id, str(e)[:120])
            continue
        try:
            seq_i = int(e["seq"])
        except (TypeError, ValueError):
            log.warning("restore_world (world %s): событие с нечисловым seq пропущено: %r",
                        new_id, e.get("seq"))
            continue
        role = str(e["role"])
        if not role.strip():
            log.warning("restore_world (world %s): событие seq=%s с пустой ролью пропущено",
                        new_id, seq_i)
            continue
        meta = e.get("meta")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        await conn.execute(
            "INSERT INTO events (world_id, seq, role, content, folded, feedback, ts, tts_status, tts_file, meta) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (new_id, seq_i, role, str(e.get("content") or ""),
             int(e.get("folded") or 0), int(e.get("feedback") or 0), e.get("ts"),
             int(e.get("tts_status") or 0), str(e.get("tts_file") or ""),
             json.dumps(meta or {}, ensure_ascii=False)),
        )
    for c in data.get("entities") or []:
        if not isinstance(c, dict) or not c.get("kind") or c.get("entity_key") is None:
            continue
        await conn.execute(
            "INSERT OR REPLACE INTO entities (world_id, kind, entity_key, name, summary, relationship, "
            "bio, meta, seq, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (new_id, str(c["kind"]), str(c["entity_key"]), str(c.get("name") or c["entity_key"]),
             str(c.get("summary") or ""), str(c.get("relationship") or ""), str(c.get("bio") or ""),
             c.get("meta") if isinstance(c.get("meta"), str) else json.dumps(c.get("meta") or {}, ensure_ascii=False),
             int(c.get("seq") or 0), c.get("created_at"), c.get("updated_at")),
        )
    for a in data.get("lore") or []:
        if not isinstance(a, dict) or not a.get("title"):
            continue
        await conn.execute(
            "INSERT INTO lore (world_id, title, content, tags, is_core, source, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (new_id, str(a["title"]), str(a.get("content") or ""), str(a.get("tags") or ""),
             int(a.get("is_core") or 0), str(a.get("source") or "imported"),
             a.get("created_at"), a.get("updated_at")),
        )
    for s in data.get("saves") or []:
        if not isinstance(s, dict) or not isinstance(s.get("setting"), str):
            continue
        await conn.execute(
            "INSERT INTO saves (world_id, name, seq, setting, created_at) VALUES (?,?,?,?,?)",
            (new_id, str(s.get("name") or "slot"), int(s.get("seq") or 0), s["setting"], s.get("created_at")),
        )
    g = data.get("graph") or {}
    for n in g.get("nodes") or []:
        if not isinstance(n, dict) or not n.get("node_id"):
            continue
        await conn.execute(
            "INSERT OR REPLACE INTO graph_nodes (world_id, node_id, label, kind, data, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (new_id, str(n["node_id"]), str(n.get("label") or ""), str(n.get("kind") or "location"),
             n.get("data") if isinstance(n.get("data"), str) else json.dumps(n.get("data") or {}, ensure_ascii=False),
             n.get("created_at"), n.get("updated_at")),
        )
    for ed in g.get("edges") or []:
        if not isinstance(ed, dict) or not ed.get("source") or not ed.get("target"):
            continue
        await conn.execute(
            "INSERT OR REPLACE INTO graph_edges (world_id, source, target, weight, data, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (new_id, str(ed["source"]), str(ed["target"]), float(ed.get("weight") or 1),
             ed.get("data") if isinstance(ed.get("data"), str) else json.dumps(ed.get("data") or {}, ensure_ascii=False),
             ed.get("created_at"), ed.get("updated_at")),
        )
    for s in data.get("turn_snapshots") or []:
        # точки перемотки (v2); setting — строка JSON как в таблице
        if not isinstance(s, dict) or "seq" not in s or not isinstance(s.get("setting"), str):
            continue
        await conn.execute(
            "INSERT OR REPLACE INTO turn_snapshots (world_id, seq, setting, created_at) VALUES (?,?,?,?)",
            (new_id, int(s["seq"]), s["setting"], s.get("created_at")),
        )
    await _maybe_commit()


def seed_narrators(presets: list[dict]) -> None:
    """Предустановленные рассказчики (INSERT OR IGNORE — идемпотентно)."""
    now = time.time()
    with _lock:
        _run(lambda: _seed_narrators(presets, now))


async def _seed_narrators(presets: list[dict], now: float) -> None:
    conn = await _open()
    for p in presets:
        await conn.execute(
            "INSERT OR IGNORE INTO narrators (name, desc, prompt, is_preset, created_at, updated_at) "
            "VALUES (?,?,?,1,?,?)",
            (p["name"], p.get("desc", ""), p["prompt"], now, now),
        )
    await _maybe_commit()


def default_narrator_id() -> int | None:
    """Первый (дефолтный) рассказчик — или None, если таблица пуста."""
    with _lock:
        return _run(_default_narrator_id)


async def _default_narrator_id() -> int | None:
    conn = await _open()
    cur = await conn.execute("SELECT id FROM narrators ORDER BY id LIMIT 1")
    row = await cur.fetchone()
    return int(row["id"]) if row else None


def list_narrators() -> list[dict]:
    with _lock:
        return _run(_list_narrators)


async def _list_narrators() -> list[dict]:
    conn = await _open()
    cur = await conn.execute("SELECT * FROM narrators ORDER BY is_preset DESC, id")
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


def get_narrator(narrator_id: int) -> Optional[dict]:
    with _lock:
        return _run(lambda: _get_narrator(narrator_id))


async def _get_narrator(narrator_id: int) -> Optional[dict]:
    conn = await _open()
    cur = await conn.execute("SELECT * FROM narrators WHERE id = ?", (narrator_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


def create_narrator(name: str, prompt: str, desc: str = "") -> dict:
    with _lock:
        return _run(lambda: _create_narrator(name, prompt, desc))


async def _create_narrator(name: str, prompt: str, desc: str) -> dict:
    conn = await _open()
    try:
        cur = await conn.execute(
            "INSERT INTO narrators (name, desc, prompt, is_preset, created_at, updated_at) "
            "VALUES (?,?,?,0,?,?)",
            (name, desc, prompt, time.time(), time.time()),
        )
        await _maybe_commit()
    except sqlite3.IntegrityError:
        raise ValueError(f"Рассказчик с именем «{name}» уже существует")
    cur2 = await conn.execute("SELECT * FROM narrators WHERE id = ?", (int(cur.lastrowid),))
    return dict(await cur2.fetchone())


def update_narrator(narrator_id: int, *, name: Optional[str] = None,
                    prompt: Optional[str] = None, desc: Optional[str] = None) -> dict:
    with _lock:
        return _run(lambda: _update_narrator(narrator_id, name=name, prompt=prompt, desc=desc))


async def _update_narrator(narrator_id: int, *, name: Optional[str],
                           prompt: Optional[str], desc: Optional[str]) -> dict:
    conn = await _open()
    cur0 = await conn.execute("SELECT * FROM narrators WHERE id = ?", (narrator_id,))
    row = await cur0.fetchone()
    if not row:
        raise KeyError("Рассказчик не найден")
    new_name = (name or "").strip() or row["name"]
    try:
        await conn.execute(
            "UPDATE narrators SET name = ?, desc = ?, prompt = ?, updated_at = ? WHERE id = ?",
            (new_name, desc if desc is not None else row["desc"],
             (prompt or "").strip() or row["prompt"], time.time(), narrator_id),
        )
        await _maybe_commit()
    except sqlite3.IntegrityError:
        raise ValueError(f"Рассказчик с именем «{new_name}» уже существует")
    cur1 = await conn.execute("SELECT * FROM narrators WHERE id = ?", (narrator_id,))
    return dict(await cur1.fetchone())


def narrators_in_use(narrator_id: int) -> int:
    """Сколько миров используют этого рассказчика."""
    with _lock:
        return _run(lambda: _narrators_in_use(narrator_id))


async def _narrators_in_use(narrator_id: int) -> int:
    conn = await _open()
    cur = await conn.execute("SELECT COUNT(*) AS c FROM worlds WHERE narrator_id = ?",
                             (narrator_id,))
    row = await cur.fetchone()
    return int(row["c"])


def delete_narrator(narrator_id: int) -> None:
    with _lock:
        _run(lambda: _delete_narrator(narrator_id))


async def _delete_narrator(narrator_id: int) -> None:
    conn = await _open()
    await conn.execute("DELETE FROM narrators WHERE id = ?", (narrator_id,))
    await conn.execute("UPDATE worlds SET narrator_id = NULL WHERE narrator_id = ?", (narrator_id,))
    await _maybe_commit()


# ─────────────────────────── свои сюжеты (кастомные заготовки) ───────────────────────────

def list_plots() -> list[dict]:
    with _lock:
        return _run(_list_plots)


async def _list_plots() -> list[dict]:
    conn = await _open()
    cur = await conn.execute("SELECT * FROM story_plots ORDER BY updated_at DESC")
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


def get_plot(plot_id: int) -> Optional[dict]:
    with _lock:
        return _run(lambda: _get_plot(plot_id))


async def _get_plot(plot_id: int) -> Optional[dict]:
    conn = await _open()
    cur = await conn.execute("SELECT * FROM story_plots WHERE id = ?", (plot_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


def create_plot(name: str, plot: str, lore: str = "") -> dict:
    name_ = name.strip()
    plot_ = plot.strip()
    if not name_ or not plot_:
        raise ValueError("Имя и текст сюжета обязательны")
    with _lock:
        return _run(lambda: _create_plot(name_, plot_, (lore or "").strip()))


async def _create_plot(name: str, plot: str, lore: str) -> dict:
    conn = await _open()
    cur = await conn.execute(
        "INSERT INTO story_plots (name, plot, lore, created_at, updated_at) VALUES (?,?,?,?,?)",
        (name, plot, lore, time.time(), time.time()),
    )
    await _maybe_commit()
    cur2 = await conn.execute("SELECT * FROM story_plots WHERE id = ?", (int(cur.lastrowid),))
    return dict(await cur2.fetchone())


def update_plot(plot_id: int, name: str | None = None, plot: str | None = None,
                lore: str | None = None) -> dict:
    with _lock:
        return _run(lambda: _update_plot(plot_id, name=name, plot=plot, lore=lore))


async def _update_plot(plot_id: int, name: str | None, plot: str | None,
                       lore: str | None) -> dict:
    conn = await _open()
    cur0 = await conn.execute("SELECT * FROM story_plots WHERE id = ?", (plot_id,))
    row = await cur0.fetchone()
    if not row:
        raise KeyError("Сюжет не найден")
    new_name = (name or "").strip() or row["name"]
    new_plot = (plot or "").strip() or row["plot"]
    if not new_name or not new_plot:
        raise ValueError("Имя и текст сюжета обязательны")
    new_lore = lore if lore is not None else (row["lore"] if "lore" in row.keys() else "")
    await conn.execute("UPDATE story_plots SET name = ?, plot = ?, lore = ?, updated_at = ? WHERE id = ?",
                       (new_name, new_plot, (new_lore or "").strip(), time.time(), plot_id))
    await _maybe_commit()
    cur1 = await conn.execute("SELECT * FROM story_plots WHERE id = ?", (plot_id,))
    return dict(await cur1.fetchone())


def delete_plot(plot_id: int) -> None:
    with _lock:
        _run(lambda: _delete_plot(plot_id))


async def _delete_plot(plot_id: int) -> None:
    conn = await _open()
    await conn.execute("DELETE FROM story_plots WHERE id = ?", (plot_id,))
    await _maybe_commit()


# ─────────────────────────── лор мира (библия) ───────────────────────────

def list_lore(world_id: int) -> list[dict]:
    """Все статьи лора мира (сначала якорные, потом свежие)."""
    with _lock:
        return _run(lambda: _list_lore(world_id))


async def _list_lore(world_id: int) -> list[dict]:
    conn = await _open()
    cur = await conn.execute(
        "SELECT * FROM lore WHERE world_id = ? ORDER BY is_core DESC, updated_at DESC",
        (world_id,),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


def get_lore(lore_id: int) -> Optional[dict]:
    with _lock:
        return _run(lambda: _get_lore(lore_id))


async def _get_lore(lore_id: int) -> Optional[dict]:
    conn = await _open()
    cur = await conn.execute("SELECT * FROM lore WHERE id = ?", (lore_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


def create_lore(world_id: int, title: str, content: str, tags: str = "",
                is_core: bool = False, source: str = "user") -> dict:
    with _lock:
        return _run(lambda: _create_lore(world_id, title, content, tags, is_core, source))


async def _create_lore(world_id: int, title: str, content: str, tags: str,
                       is_core: bool, source: str) -> dict:
    conn = await _open()
    cur = await conn.execute(
        "INSERT INTO lore (world_id, title, content, tags, is_core, source, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (world_id, title.strip(), content.strip(), tags.strip(), 1 if is_core else 0,
         source, time.time(), time.time()),
    )
    await _maybe_commit()
    lid = int(cur.lastrowid)
    cur2 = await conn.execute("SELECT * FROM lore WHERE id = ?", (lid,))
    return dict(await cur2.fetchone())  # type: ignore


def update_lore(lore_id: int, *, title: str | None = None, content: str | None = None,
                tags: str | None = None, is_core: bool | None = None) -> dict:
    with _lock:
        return _run(lambda: _update_lore(lore_id, title=title, content=content,
                                         tags=tags, is_core=is_core))


async def _update_lore(lore_id: int, *, title: str | None, content: str | None,
                       tags: str | None, is_core: bool | None) -> dict:
    conn = await _open()
    cur0 = await conn.execute("SELECT * FROM lore WHERE id = ?", (lore_id,))
    row = await cur0.fetchone()
    if not row:
        raise KeyError("Статья лора не найдена")
    await conn.execute(
        "UPDATE lore SET title = ?, content = ?, tags = ?, is_core = ?, updated_at = ? WHERE id = ?",
        (title.strip() if title is not None else row["title"],
         content.strip() if content is not None else row["content"],
         tags.strip() if tags is not None else row["tags"],
         1 if is_core else 0 if is_core is not None else row["is_core"],
         time.time(), lore_id),
    )
    await _maybe_commit()
    cur2 = await conn.execute("SELECT * FROM lore WHERE id = ?", (lore_id,))
    return dict(await cur2.fetchone())


def delete_lore(lore_id: int) -> None:
    with _lock:
        _run(lambda: _delete_lore(lore_id))


async def _delete_lore(lore_id: int) -> None:
    conn = await _open()
    await conn.execute("DELETE FROM lore WHERE id = ?", (lore_id,))
    await _maybe_commit()


def get_admin_settings() -> dict:
    """Все админ-настройки: {ключ: значение} (только непустые)."""
    with _lock:
        return _run(_get_admin_settings)


async def _get_admin_settings() -> dict:
    conn = await _open()
    cur = await conn.execute("SELECT key, value FROM admin_settings")
    rows = await cur.fetchall()
    return {r["key"]: r["value"] for r in rows}


def set_admin_settings(settings: dict) -> None:
    """Upsert админ-настроек; пустое значение = удалить ключ (вернуть .env-дефолт)."""
    now = time.time()
    with _lock:
        _run(lambda: _set_admin_settings(settings, now))


async def _set_admin_settings(settings: dict, now: float) -> None:
    conn = await _open()
    for k, v in settings.items():
        v = (v or "").strip()
        if v == "":
            await conn.execute("DELETE FROM admin_settings WHERE key = ?", (k,))
        else:
            await conn.execute(
                "INSERT OR REPLACE INTO admin_settings (key, value, updated_at) VALUES (?,?,?)",
                (k, v, now),
            )
    await _maybe_commit()


# ───────────────────────🌐 графовая БД (карта и связи) ───────────────────────
# Узлы и рёбра хранятся канонически: рёбра неориентированные, ключ (source<=target).

def _G_canon(a: str, b: str) -> tuple[str, str]:
    """Канонический порядок пары узлов (source <= target) для неориентированного ребра."""
    return (a, b) if a <= b else (b, a)


def graph_replace(world_id: int, nodes: list[dict], edges: list[tuple]) -> None:
    """Полная перестройка графа мира за одну атомарную идемпотентную операцию.

    `nodes` — [{id, label, kind, data}]; рёбра как (a, b, weight=None, data=None).
    Ребро на несуществующий узел пропускается.
    """
    with _lock:
        _run(lambda: _graph_replace(world_id, nodes, edges))


async def _graph_replace(world_id: int, nodes: list[dict], edges: list[tuple]) -> None:
    conn = await _open()
    now = time.time()
    # 1) сброс старых узлов и рёбер мира
    await conn.execute("DELETE FROM graph_edges WHERE world_id = ?", (world_id,))
    await conn.execute("DELETE FROM graph_nodes WHERE world_id = ?", (world_id,))
    # 2) пересоздать узлы, запомнить какие есть
    have: set[str] = set()
    for n in nodes:
        nid = str(n.get("id") or n.get("node_id") or "").strip()
        if not nid:
            continue
        await conn.execute(
            "INSERT OR REPLACE INTO graph_nodes "
            "(world_id, node_id, label, kind, data, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (world_id, nid, str(n.get("label", "")), str(n.get("kind", "custom")),
             json.dumps(n.get("data", {}) or {}, ensure_ascii=False), now, now),
        )
        have.add(nid)
    # 3) рёбра только между существующими узлами
    for e in edges:
        a, b = str(e[0]), str(e[1])
        if a == b or a not in have or b not in have:
            continue
        s, t = _G_canon(a, b)
        wgt = float(e[2]) if len(e) > 2 and e[2] is not None else 1.0
        edata = dict(e[3]) if len(e) > 3 and isinstance(e[3], dict) else {}
        await conn.execute(
            "INSERT OR REPLACE INTO graph_edges "
            "(world_id, source, target, weight, data, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (world_id, s, t, wgt, json.dumps(edata, ensure_ascii=False), now, now),
        )
    await _maybe_commit()


def graph_nodes(world_id: int) -> list[dict]:
    """Все узлы графа мира."""
    with _lock:
        return _run(lambda: _graph_nodes(world_id))


async def _graph_nodes(world_id: int) -> list[dict]:
    conn = await _open()
    cur = await conn.execute(
        "SELECT node_id, label, kind, data FROM graph_nodes WHERE world_id = ? ORDER BY node_id",
        (world_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


def graph_edges(world_id: int) -> list[dict]:
    """Все рёбра графа мира (source/target, weight, data)."""
    with _lock:
        return _run(lambda: _graph_edges(world_id))


async def _graph_edges(world_id: int) -> list[dict]:
    conn = await _open()
    cur = await conn.execute(
        "SELECT source, target, weight, data FROM graph_edges WHERE world_id = ?",
        (world_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


def graph_get_node(world_id: int, node_id: str) -> Optional[dict]:
    """Один узел графа или None."""
    with _lock:
        return _run(lambda: _graph_get_node(world_id, str(node_id)))


async def _graph_get_node(world_id: int, node_id: str) -> Optional[dict]:
    conn = await _open()
    cur = await conn.execute(
        "SELECT node_id, label, kind, data FROM graph_nodes WHERE world_id = ? AND node_id = ?",
        (world_id, node_id),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


def graph_set_node(world_id: int, node_id: str, label: str = "", kind: str = "custom",
                   data: dict | None = None) -> None:
    """Upsert одного узла графа (без рёбер)."""
    now = time.time()
    with _lock:
        _run(lambda: _graph_set_node(world_id, str(node_id), label, kind, data or {}, now))


async def _graph_set_node(world_id: int, node_id: str, label: str, kind: str,
                          data: dict, now: float) -> None:
    conn = await _open()
    await conn.execute(
        "INSERT OR REPLACE INTO graph_nodes "
        "(world_id, node_id, label, kind, data, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (world_id, node_id, label, kind, json.dumps(data, ensure_ascii=False), now, now),
    )
    await _maybe_commit()


def graph_connect(world_id: int, a: str, b: str, weight: float = 1.0, data: dict | None = None) -> None:
    """Добавить/обновить неориентированную связь между двумя существующими узлами.

    Безопасная: пропускает селфи-петлю (a==b) и связь, если хоть один узел не существует
    в графе (не создаёт «висячих» рёбер).
    """
    a, b = str(a), str(b)
    if a == b or not a or not b:
        return
    have = {str(n["node_id"]) for n in graph_nodes(world_id)}
    if a not in have or b not in have:
        return
    s, t = _G_canon(a, b)
    now = time.time()
    with _lock:
        _run(lambda: _graph_connect(world_id, s, t, weight, data or {}, now))


async def _graph_connect(world_id: int, s: str, t: str, weight: float, data: dict, now: float) -> None:
    conn = await _open()
    await conn.execute(
        "INSERT OR REPLACE INTO graph_edges "
        "(world_id, source, target, weight, data, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (world_id, s, t, weight, json.dumps(data, ensure_ascii=False), now, now),
    )
    await _maybe_commit()


def graph_clear_world(world_id: int) -> None:
    """Удалить все узлы и рёбра мира (для полного сброса карты)."""
    with _lock:
        _run(lambda: _graph_clear(world_id))


async def _graph_clear(world_id: int) -> None:
    conn = await _open()
    await conn.execute("DELETE FROM graph_edges WHERE world_id = ?", (world_id,))
    await conn.execute("DELETE FROM graph_nodes WHERE world_id = ?", (world_id,))
    await _maybe_commit()
