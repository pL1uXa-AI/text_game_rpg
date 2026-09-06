# -*- coding: utf-8 -*-
"""D12 (аудит 41): таймаут внутри `db.transaction()` больше не оставляет соединение в
открытом `BEGIN IMMEDIATE`.

Было: `transaction()` уменьшает `_tx_depth` и только потом зовёт `_run(_commit_now)`. Если
ответ от БД не пришёл (`_RUN_TIMEOUT`), корутина коммита отменяется, группа уже «закрыта»,
и дозакрывать `BEGIN IMMEDIATE` некому — единственное соединение висит в транзакции до
следующего обращения: строки откатанного хода остаются видны этому же соединению, события
из буфера `_pending_events` уходят в шину живого чана со СЛЕДУЮЩИМ коммитом (фантомы),
а файл базы держит write-lock (чужой процесс — `database is locked`). И всё это молча:
в журнале только «БД не ответила», без слова «транзакция».

Стало: обработчик таймаута в `_run` видит незакрытый BEGIN (флаг `_tx_open` ставит
`_begin_now`, снимают `_commit_now`/`_rollback_now`) при закрытой группе (`_tx_depth == 0`)
и вызывает `_rescue_transaction`: `log.error` с контекстом + явный `_rollback_now` +
очистка `_pending_events`. Отказ спасительного отката — тоже `log.error`, а не молчание
(правило 14). Исключение таймаута при этом пробрасывается как раньше.

Инвариант «тело `transaction()` синхронное» и сама схема (`transaction()` / RLock /
`_tx_depth`) не тронуты — правка только в обработчике таймаута.
"""
from __future__ import annotations

import asyncio
import inspect
import io
import logging
import sqlite3
import time
from pathlib import Path

import pytest

from backend import db as db_mod

ROOT = Path(__file__).resolve().parent.parent
BODY = "D12 — ход, который не дошёл до COMMIT"
AFTER = "D12 — запись после спасения"


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    @property
    def messages(self) -> list[str]:
        return [r.getMessage() for r in self.records]

    def errors_containing(self, *needles: str) -> list[str]:
        return [r.getMessage() for r in self.records
                if r.levelno == logging.ERROR and all(n in r.getMessage() for n in needles)]


@pytest.fixture
def logs():
    cap = _Capture()
    root = logging.getLogger("textgame")
    root.addHandler(cap)
    lvl = root.level
    root.setLevel(logging.DEBUG)
    yield cap
    root.removeHandler(cap)
    root.setLevel(lvl)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Мир в изолированной БД прогона (conftest уже выставил DB_PATH на temp)."""
    return db_mod.create_world(
        "D12", theme="fantasy", genre="проза", difficulty="normal", perspective="third",
        language="ru", custom_hook="", setting={"player": {"gold": 1}}, gen_settings={})


class _Stalled:
    """Обёртка живого aiosqlite-соединения: `commit()` (или `rollback()`) «не отвечает»
    `delay` секунд, остальное — делегация реальному соединению.

    Это ровно то, что происходит в бою при зависании БД: вызывающий поток отваливается по
    `_RUN_TIMEOUT`, а корутина всё ещё живёт на цикле и «доделывает» работу позже.
    """

    def __init__(self, real, delay: float = 0.6, stall: str = "commit") -> None:
        self._r = real
        self.delay = delay
        self.stall = stall
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1
        await asyncio.sleep(self.delay)     # «БД молчит», реальный COMMIT не делается
        if self.stall != "commit":
            await self._r.commit()

    async def rollback(self) -> None:
        self.rollbacks += 1
        if self.stall == "rollback":
            await asyncio.sleep(self.delay)
        await self._r.rollback()            # откат доходит: так честнее для проверок лога

    def __getattr__(self, name):
        return getattr(self._r, name)


def _stall_commit_in_transaction(monkeypatch, world_id: int, *, delay: float = 0.6,
                                 run_timeout: float = 0.2, stall: str = "commit",
                                 rescue_timeout: float | None = None) -> _Stalled:
    """Открывает настоящую транзакцию, пишет в неё событие и подвешивает COMMIT.

    Возвращает прокси-соединение: на нём видно, сколько раз реально доехал откат.
    Исключение таймаута — ожидаемое (его и проверяем)."""
    real = db_mod._run(lambda: db_mod._open())
    proxy = _Stalled(real, delay=delay, stall=stall)
    with monkeypatch.context() as m:
        m.setattr(db_mod, "_conn", proxy)
        m.setattr(db_mod, "_RUN_TIMEOUT", run_timeout)
        if rescue_timeout is not None:
            m.setattr(db_mod, "_RESCUE_TIMEOUT", rescue_timeout)
        with pytest.raises(RuntimeError) as ei:
            with db_mod.transaction():
                db_mod.add_event(world_id, "narrator", BODY)
        assert "не отвечает" in str(ei.value), f"не та ошибка: {ei.value!r}"
        # прокси возвращён на место (context() закрылся) — соединение живое
    return proxy


def _wait(seconds: float = 0.9) -> None:
    """Дать фоновому циклу БД доесть «поздние» корутины (иначе они перейдут в следующий
    тест и оставят там свои следы)."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(0.05)


# ── 1. журнал: таймаут в транзакции виден ────────────────────────────────────

def test_timeout_in_transaction_is_logged(monkeypatch, world, logs):
    """Раньше в логе была только строка «БД не ответила» — про незакрытый BEGIN ни слова."""
    _stall_commit_in_transaction(monkeypatch, world)
    hits = logs.errors_containing("BEGIN IMMEDIATE")
    assert hits, f"таймаут внутри транзакции снова молчит о висящем BEGIN: {logs.messages}"
    assert any("ROLLBACK" in m for m in hits), hits


def test_timeout_error_names_the_stalled_operation(monkeypatch, world, logs):
    """Без имени запроса в журнале непонятно, что встало: чтение, запись или COMMIT."""
    _stall_commit_in_transaction(monkeypatch, world)
    hits = logs.errors_containing("не ответила")
    assert hits and "_commit_now" in hits[0], logs.messages


def test_rescue_reports_success(monkeypatch, world, logs):
    """Спасение обязано быть отмечено и в сторону «успех», и в сторону «не вышло»."""
    _stall_commit_in_transaction(monkeypatch, world)
    assert logs.errors_containing("откатана"), "нет строки о закрытии транзакции"


# ── 2. состояние соединения: транзакция действительно закрыта ────────────────

def test_write_lock_released_after_rescue(monkeypatch, world):
    """Второе соединение может взять BEGIN IMMEDIATE — значит write-lock не висит."""
    _stall_commit_in_transaction(monkeypatch, world)
    other = sqlite3.connect(str(db_mod.get_config().db_path), timeout=0.1)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.execute("SELECT COUNT(*) FROM events").fetchone()
        other.commit()
    finally:
        other.close()


def test_rolled_back_row_not_visible_after_rescue(monkeypatch, world):
    """Строка откатанного хода не должна «висеть» в открытом BEGIN этого же соединения."""
    _stall_commit_in_transaction(monkeypatch, world)
    contents = [e["content"] for e in db_mod.get_events(world)]
    assert BODY not in contents, f"половина хода всё ещё видна: {contents}"


def test_flag_tx_open_cleared(monkeypatch, world):
    _stall_commit_in_transaction(monkeypatch, world)
    assert db_mod._tx_open is False, "флаг незакрытого BEGIN остался взведён"
    assert db_mod._tx_depth == 0
    assert db_mod._pending_events == [], "буфер событий откатанной транзакции не очищен"


def test_database_usable_after_rescue(monkeypatch, world, logs):
    """После спасения обычная запись работает и сохраняется (соединение не «отравлено»)."""
    _stall_commit_in_transaction(monkeypatch, world)
    _wait(0.9)
    db_mod.add_event(world, "system", AFTER)
    _wait(0.2)
    assert AFTER in [e["content"] for e in db_mod.get_events(world)]


def test_phantom_events_never_reach_the_bus(monkeypatch, world):
    """События откатанного хода не должны прийти слушателям с ЧУЖИМ коммитом (A9 по смыслу).

    На старом коде буфер `_pending_events` не чистился, и «фантом» улетал в шину либо
    поздним коммитом той же корутины, либо следующей операцией."""
    got: list[str] = []

    def _listener(ev: dict) -> None:
        got.append(ev["content"])

    db_mod.add_event_listener(_listener)
    try:
        _stall_commit_in_transaction(monkeypatch, world)
        _wait(1.0)                            # ждём и «поздний» коммит отменённой корутины
        db_mod.add_event(world, "system", AFTER)
        _wait(0.3)
    finally:
        db_mod._event_listeners.remove(_listener)
    assert BODY not in got, f"фантом живого чата дошёл до слушателя: {got}"
    assert AFTER in got, "слушатель вообще не работает — проверка бессмысленна"


# ── 3. отказ спасения не глотается и не подменяет первую ошибку ──────────────

def test_rescue_failure_is_logged_and_flag_stays(monkeypatch, world, logs):
    """Если и ROLLBACK виснет — пишем вторую ошибку; транзакция считается незакрытой."""
    real = db_mod._run(lambda: db_mod._open())

    class _Both(_Stalled):
        async def commit(self) -> None:
            self.commits += 1
            await asyncio.sleep(self.delay)
            await self._r.commit()

        async def rollback(self) -> None:
            self.rollbacks += 1
            await asyncio.sleep(self.delay)   # спасение не успевает за _RESCUE_TIMEOUT
            await self._r.rollback()

    hang = _Both(real, delay=1.2)
    with monkeypatch.context() as m:
        m.setattr(db_mod, "_conn", hang)
        m.setattr(db_mod, "_RUN_TIMEOUT", 0.2)
        m.setattr(db_mod, "_RESCUE_TIMEOUT", 0.2)
        with pytest.raises(RuntimeError) as ei:
            with db_mod.transaction():
                db_mod.add_event(world, "narrator", BODY)
        assert "не отвечает" in str(ei.value)
        assert hang.rollbacks >= 1, "спасательный откат не был даже начат"
    assert db_mod._tx_open is True, "неудачный откат обязан оставить флаг взведённым"
    assert logs.errors_containing("ROLLBACK не прошёл"), "отказ спасения проглочен молча"
    _wait(1.5)                                # пусть поздний откат доедет
    assert db_mod._tx_open is False, "поздний откат обязан снять флаг сам"


# ── 4. ложных откатов нет ────────────────────────────────────────────────────

def test_no_rescue_when_timeout_outside_transaction(monkeypatch, world, logs):
    """Таймаут обычного чтения вне группы ничего откатывать не должен (BEGIN не открыт)."""
    real = db_mod._run(lambda: db_mod._open())

    class _SlowRead:
        def __init__(self, r, delay):
            self._r, self.delay = r, delay

        async def execute(self, *a, **kw):
            await asyncio.sleep(self.delay)
            return await self._r.execute(*a, **kw)

        def __getattr__(self, name):
            return getattr(self._r, name)

    with monkeypatch.context() as m:
        m.setattr(db_mod, "_conn", _SlowRead(real, 0.8))
        m.setattr(db_mod, "_RUN_TIMEOUT", 0.2)
        with pytest.raises(RuntimeError):
            db_mod.get_events(world)
    assert not logs.errors_containing("BEGIN IMMEDIATE"), \
        f"вне транзакции всё равно лезет спасение: {logs.messages}"
    assert db_mod._tx_open is False
    _wait(1.0)


def test_transaction_without_timeout_is_untouched(world, logs):
    """Обычная транзакция: ничего не откатывается, всё пишется, журнал чист."""
    with db_mod.transaction():
        db_mod.add_event(world, "narrator", "обычный ход")
    assert "обычный ход" in [e["content"] for e in db_mod.get_events(world)]
    assert db_mod._tx_open is False and db_mod._pending_events == []
    assert not logs.errors_containing("BEGIN IMMEDIATE")


# ── 5. сторожа исходника (не дают «починить» удаление) ───────────────────────

def test_source_rescue_rolls_back_explicitly():
    src = inspect.getsource(db_mod)
    i = src.index("def _run(")
    body = src[i:src.index("\ndef ", i + 1)]
    assert "_rescue_transaction" in body, "обработчик таймаута снова не закрывает транзакцию"
    rescue = src[src.index("def _rescue_transaction("):]
    rescue = rescue[:rescue.index("\n\nasync def ", len("def _rescue_transaction("))]
    assert "_rollback_now" in rescue, "спасение не зовёт явный ROLLBACK"
    assert "_pending_events.clear()" in rescue, "буфер фантомных событий не чистится"
    assert "log.error" in rescue, "правило 14: отказ фона не должен молчать"


def test_source_begin_and_endings_track_the_flag():
    src = inspect.getsource(db_mod)
    for name in ("_begin_now", "_commit_now", "_rollback_now"):
        i = src.index(f"async def {name}(")
        body = src[i:src.index("\n\n", i)]
        assert "_tx_open" in body, f"{name} не ведёт признак открытого BEGIN"


def test_agent_md_documents_d12():
    doc = io.open(ROOT / "AGENT.md", encoding="utf-8").read()
    assert "D12" in doc, "правило/инвариант не описан в AGENT.md"
    assert "_rescue_transaction" in doc, "в доке нет имени точки спасения"
