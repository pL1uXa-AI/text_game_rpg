# -*- coding: utf-8 -*-
"""Сессия 46 — A6 (аудит 41): «заглушки для ошибок, которые не должны быть тихими».

Правило 14 запрещает молча глотать ошибки в фоне. Отчёт перечислял три части; здесь
закреплены тестом все три (тесты — на ПОВЕДЕНИЕ, а не на исходники, правило 19):

  1 `db.close()`            — два `except Exception: pass` на shutdown; при залоченном
                              game.db (Windows) игрок не узнавал, что файл не освобождён
                              → теперь `log.warning` на каждый шаг и на «поток ещё жив»;
  2 `narrator.generate_*`  — «тихие» `return None` на разборе JSON у event/master/
                              enemy_ai/vision/roll → общий хелпер `_agent_quiet`:
                              warning ОДИН раз на (агент, мир) с traceback, а не лог-шторм
                              (агент живёт каждый ход); мёртвые замыкания `_parse()` в
                              divine/logic_judge удалены (там отказ уже логируется);
  3 `logsetup`              — docstring модуля обещал `quiet()` и `log_debug_once()`,
                              которых никогда не было → обещание снято, и тест это сторожит;
                              `log_once` теперь умеет `exc_info`;
  4 `ratelimit.make_guard` — отказ 429 был без единой строки в журнале (п. «по желанию»)
                              → warning с ключом и причиной (окно / burst).

Тесты не поднимают сервер: логи перехватывает `logging.Handler`, LLM — заглушка.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from backend import logsetup
from backend import narrator as N


class _Capture(logging.Handler):
    """Минимальная «заглушка логгера»: собирает записи, чтобы сверять и текст, и уровень."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self, level: int | None = None) -> list[str]:
        return [r.getMessage() for r in self.records if level is None or r.levelno == level]

    def warnings_with(self, needle: str) -> list[str]:
        return [m for m in self.messages(logging.WARNING) if needle in m]


@pytest.fixture
def logs(monkeypatch):
    """Перехват записей textgame.* + изоляция отметок log_once (иначе «первый раз» уже прошёл)."""
    cap = _Capture()
    root = logging.getLogger("textgame")
    root.addHandler(cap)
    lvl = root.level
    root.setLevel(logging.DEBUG)
    logsetup.reset_once()
    try:
        yield cap
    finally:
        logsetup.reset_once()
        root.setLevel(lvl)
        root.removeHandler(cap)


# ══════════════════════════════════════════════════════════════════════════
# 1. db.close(): тихие except → warning
# ══════════════════════════════════════════════════════════════════════════

def _stub_dispatch(monkeypatch, exc: Exception | None = None):
    """Подмена `asyncio.run_coroutine_threadsafe`: корутину ОБЯЗАТЕЛЬНО закрываем
    (иначе pytest ловит RuntimeWarning «coroutine was never awaited» и приписывает его
    случайному тесту), а `future.result()` возвращает None или поднятую ошибку."""
    class _F:
        def result(self, timeout=None):
            if exc is not None:
                raise exc
            return None

    def _dispatch(coro, loop, **kw):
        coro.close()
        return _F()

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", _dispatch)


def test_db_close_logs_when_shutdown_fails(logs, monkeypatch):
    """Отказ закрытия соединения (залоченный файл на Windows) больше не `pass`."""
    from backend import db

    class _Loop:
        """Макет цикла: и закрытие, и остановка падают — как при живом залоченном соединении."""

        def call_soon_threadsafe(self, fn):
            raise RuntimeError("loop is stopped")

    monkeypatch.setattr(db, "_loop", _Loop())
    monkeypatch.setattr(db, "_loop_thread", None)
    _stub_dispatch(monkeypatch, exc=RuntimeError("database is locked"))

    db.close()  # не бросает (вызывается на shutdown)

    assert logs.warnings_with("закрытие соединения не удалось"), \
        f"нет warning о незакрытом соединении: {logs.messages()}"
    assert logs.warnings_with("не остановлен"), \
        f"нет warning о незакрытом цикле: {logs.messages()}"
    # в тексте — путь к файлу: главное, что игрок/админ видит, КАКОЙ файл держат
    assert any(".db" in m for m in logs.warnings_with("закрытие"))
    # и traceback доехал (правило 14 — диагностика, а не голый факт отказа)
    rec = [r for r in logs.records if r.levelno == logging.WARNING
           and "закрытие соединения" in r.getMessage()]
    assert rec and rec[0].exc_info is not None


def test_db_close_logs_stuck_loop_thread(logs, monkeypatch):
    """Поток цикла пережил stop() → тоже видно в журнале (файл БД ещё держат)."""
    from backend import db

    class _Loop:
        def call_soon_threadsafe(self, fn):
            fn()

    class _Thread:
        def join(self, timeout=None):
            pass

        def is_alive(self):
            return True

    monkeypatch.setattr(db, "_loop", _Loop())
    monkeypatch.setattr(db, "_loop_thread", _Thread())
    _stub_dispatch(monkeypatch)

    db.close()

    assert logs.warnings_with("поток цикла жив"), \
        f"нет warning о живом потоке: {logs.messages()}"


def test_db_close_quiet_when_all_well(logs, monkeypatch):
    """Штатный close() не должен писать ни одного warning (иначе журнал замусорен)."""
    from backend import db

    class _Loop:
        def call_soon_threadsafe(self, fn):
            fn()

        def stop(self):
            pass

    class _Thread:
        def join(self, timeout=None):
            pass

        def is_alive(self):
            return False

    async def _fake_shutdown():
        pass

    monkeypatch.setattr(db, "_loop", _Loop())
    monkeypatch.setattr(db, "_loop_thread", _Thread())
    monkeypatch.setattr(db, "_shutdown", _fake_shutdown)
    _stub_dispatch(monkeypatch)

    db.close()

    assert [r for r in logs.records if r.levelno >= logging.WARNING] == []


# ══════════════════════════════════════════════════════════════════════════
# 2. narrator: «тихие» return None у фоновых агентов
# ══════════════════════════════════════════════════════════════════════════

WORLD = {"id": 7, "name": "Проба", "language": "ru"}


def _setting():
    return {
        "player": {"name": "Тест", "level": 1, "hp": 30, "max_hp": 30, "mp": 10, "max_mp": 10,
                   "gold": 5, "xp": 0, "stats": {"сила": 10}},
        "enemies": {"w1": {"name": "Волк", "hp": 20, "max_hp": 20, "dmg": 4}},
        "npcs": {}, "items": [], "effects": [], "flags": {}, "quests": {},
        "_player_turns": 9,
    }


def _broken_llm(monkeypatch, reply: str = "не JSON вовсе"):
    """LLM отвечает мусором (tools-режим выключен → разбор '{...}' из текста)."""
    async def _complete(messages, **kw):
        return reply

    async def _raise(messages, **kw):
        raise ConnectionError("модель лёг")

    monkeypatch.setattr(N.llm, "complete", _complete)
    return _raise


def _run_in_world_context(coro, world_id=7, agent="agent"):
    """Прогон в контексте хода — как в фоне (`bg` поднимает turn_context перед задачей)."""
    async def _main():
        with logsetup.turn_context(world_id=world_id, agent=agent):
            return await coro
    return asyncio.run(_main())


@pytest.mark.parametrize("coro_factory, label", [
    (lambda: N.generate_dynamic_event(WORLD, _setting()), "event"),
    (lambda: N.generate_master_nudge(_setting(), "drifting", "…"), "master"),
    (lambda: N.generate_enemy_ai(_setting(), "…"), "enemy_ai"),
])
def test_agent_failure_is_logged_not_silent(logs, monkeypatch, coro_factory, label):
    """LLM упал → агент вернул None, но в журнале есть warning с меткой агента (правило 14)."""
    _broken_llm(monkeypatch)

    async def _raise(messages, **kw):
        raise ConnectionError("подключение сброшено")

    monkeypatch.setattr(N.llm, "complete", _raise)
    res = _run_in_world_context(coro_factory(), agent=label)
    assert res is None, "сбой модели = None (ход не падает)"

    hits = logs.warnings_with(f"агент «{label}»")
    assert hits, f"нет warning об отказе агента {label}: {logs.messages()}"
    assert "world 7" in hits[0], "в тексте должен быть мир (ключ диагностики)"


def test_agent_exception_carries_traceback(logs, monkeypatch):
    """Исключение, пойманное самим агентом (а не `llm_json_tool`, который логирует сам),
    доезжает до журнала с traceback — иначе «тихий» except не разобрать."""
    async def _raise(messages, **kw):
        raise ConnectionError("нет соединения с моделью")

    monkeypatch.setattr(N.llm, "complete", _raise)
    assert _run_in_world_context(N.generate_vision(_setting(), {"text": "сон"}),
                                 agent="vision") is None
    rec = [r for r in logs.records if r.levelno == logging.WARNING
           and "агент «vision»" in r.getMessage()]
    assert rec and rec[0].exc_info is not None, logs.messages()


def test_agent_json_garbage_is_logged(logs, monkeypatch):
    """LLM жив, но дважды вернул не-JSON → отказ тоже виден (не «молча нет события»)."""
    _broken_llm(monkeypatch)
    res = _run_in_world_context(N.generate_dynamic_event(WORLD, _setting()), agent="event")
    assert res is None
    assert logs.warnings_with("не вернула валидный JSON"), logs.messages()


def test_agent_logs_once_per_world_and_agent(logs, monkeypatch):
    """Лог-шторм запрещён: тот же отказ на сотнях ходов даёт ОДИН warning, дальше debug."""
    async def _raise(messages, **kw):
        raise ConnectionError("облако легло")

    monkeypatch.setattr(N.llm, "complete", _raise)
    for _ in range(5):
        assert _run_in_world_context(N.generate_master_nudge(_setting(), "repetition", "…"),
                                     agent="master") is None

    warnings = logs.warnings_with("агент «master»")
    assert len(warnings) == 1, f"ожидали один warning, получили {len(warnings)}: {warnings}"
    debugs = [m for m in logs.messages(logging.DEBUG) if "agent-fail:master" in m]
    assert debugs, "повторы обязаны оставаться в debug (иначе диагностика потеряна)"
    # другой мир — другой ключ: его отказ обязан быть виден
    assert _run_in_world_context(N.generate_master_nudge(_setting(), "repetition", "…"),
                                 world_id=8, agent="master") is None
    assert len(logs.warnings_with("агент «master»")) == 2, "ключ (agent, world) — раздельный"


def test_agent_success_logs_nothing(logs, monkeypatch):
    """Зелёный путь не пишет warning-ов (фикс не должен сделать журнал шумным)."""
    async def _ok(messages, **kw):
        return '{"event": "Где-то гудит колокол.", "directives": {"weather": "дождь"}}'

    monkeypatch.setattr(N.llm, "complete", _ok)
    res = _run_in_world_context(N.generate_dynamic_event(WORLD, _setting()), agent="event")
    assert res is not None
    assert res[1].get("weather") == "дождь"
    assert not [r for r in logs.records if r.levelno >= logging.WARNING], logs.messages()


def test_divine_and_judge_keep_logging_their_own_refusals(logs, monkeypatch):
    """divine/logic_judge: у них и раньше были warning-и на отказ — они обязаны остаться
    (мёртвые `_parse()` удалены, живые ветки не тронуты)."""
    async def _raise(messages, **kw):
        raise ConnectionError("нет связи")

    monkeypatch.setattr(N.llm, "complete", _raise)
    assert _run_in_world_context(
        N.divine_intervene(7, WORLD, _setting(), "ошибка мастера"), agent="divine") is None
    assert logs.warnings_with("Провидение"), logs.messages()

    assert _run_in_world_context(
        N.logic_judge(7, _setting(), "иду", "Ты идёшь."), agent="judge") is None
    assert logs.warnings_with("судья логики"), logs.messages()


def test_narrate_roll_fallback_is_logged(logs, monkeypatch):
    """Описание результата броска — не None, а "" (ход не роняется), но отказ виден."""
    async def _raise(messages, **kw):
        raise ConnectionError("стрим оборвался")

    monkeypatch.setattr(N.llm, "complete", _raise)
    out = _run_in_world_context(N.narrate_roll(7, "атака", "d20", 0, 10, 23, "успех",
                                               "Ты замахиваешься…"), agent="roll")
    assert out == ""
    assert logs.warnings_with("агент «roll»"), logs.messages()


# ══════════════════════════════════════════════════════════════════════════
# 3. logsetup: док не обещает несуществующего; log_once умеет exc_info
# ══════════════════════════════════════════════════════════════════════════

def test_logsetup_docstring_promises_only_real_helpers():
    """Docstring модуля раньше обещал `quiet()` и `log_debug_once()` — их нет (A6)."""
    names = [n for n in ("quiet", "log_debug_once") if f"{n}()" in (logsetup.__doc__ or "")]
    assert not names, f"док обещает несуществующие хелперы: {names}"
    for real in ("get_logger", "turn_context", "log_once"):
        assert callable(getattr(logsetup, real)), f"{real} обязан быть в модуле"


def test_log_once_attaches_traceback_when_asked(logs):
    """`exc_info` в log_once — чтобы «тихий» except можно было разобрать по трейсу."""
    log = logsetup.get_logger("probe")
    try:
        raise ValueError("образцовая поломка")
    except ValueError as e:
        log_once = logsetup.log_once
        log_once(log, "probe-key", logging.WARNING, "упало: %s", e, exc_info=e)
    warn = [r for r in logs.records if r.levelno == logging.WARNING and "упало" in r.getMessage()]
    assert warn and warn[0].exc_info is not None
    # повтор с тем же ключом — молчит на warning (шторм запрещён)
    log_once(log, "probe-key", logging.WARNING, "упало: %s", "снова", exc_info=None)
    assert len([r for r in logs.records if r.levelno == logging.WARNING
                and "упало" in r.getMessage()]) == 1


# ══════════════════════════════════════════════════════════════════════════
# 4. ratelimit: отказ 429 виден в журнале с ключом
# ══════════════════════════════════════════════════════════════════════════

def test_rate_limit_refusal_is_logged(logs, monkeypatch):
    from backend import ratelimit as rl

    class _Req:
        class _C:
            host = "127.0.0.1"
        client = _C()

    rl.reset()
    monkeypatch.setattr(rl, "ENABLED", True)
    guard = rl.make_guard("probe-scope", limit=2, window=60, burst=2, burst_window=5)

    guard(_Req())          # 1
    guard(_Req())          # 2 — лимит выбран
    with pytest.raises(Exception) as ei:
        guard(_Req())      # 3 — отказ
    assert getattr(ei.value, "status_code", None) == 429

    hits = logs.warnings_with("rate-limit")
    assert hits, f"отказ 429 не попал в журнал: {logs.messages()}"
    assert "probe-scope" in hits[0] and "127.0.0.1" in hits[0], "нужны scope и ключ"
    # успехи не пишутся
    assert len(hits) == 1, hits
    rl.reset()
    rl.configure(enabled=False)   # возвращаем тестовую среду (conftest)
