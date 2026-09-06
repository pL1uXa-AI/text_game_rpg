# -*- coding: utf-8 -*-
"""D11 (аудит 41): `db.update_world(**fields)` больше не «тихий no-op» на опечатке.

Было: неизвестный ключ молча отсеивался (`if k in allowed`), а если он был единственным —
`sets` оставался пустым и функция выходила через ранний `return`. Написать
`update_world(wid, setings=...)` = состояние мира не обновилось и в журнале НОЛЬ строк
(правило 14: тихого проглатывания нет).

Стало: на каждый ключ вне белого списка — `log.warning` с миром, именем поля и списком
допустимых. Поведение не изменилось: неизвестные поля по-прежнему НЕ попадают в SQL (белый
список — единственная защита от подстановки имени колонки через `**fields`), известные
пишутся как писались.
"""
from __future__ import annotations

import inspect
import json
import logging

import pytest

from backend import db as db_mod


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    @property
    def messages(self) -> list[str]:
        return [r.getMessage() for r in self.records]


@pytest.fixture
def logs(monkeypatch):
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
    """Мир в изолированной БД прогона (conftest уже выставил DB_PATH)."""
    return db_mod.create_world(
        "D11", theme="fantasy", genre="проза", difficulty="normal", perspective="third",
        language="ru", custom_hook="", setting={"player": {"gold": 5}},
        gen_settings={"max_tokens": 100})


# ── 1. предупреждение есть ───────────────────────────────────────────────────

def test_unknown_field_warns(world, logs):
    db_mod.update_world(world, setings={"player": {"gold": 9}})
    hits = [m for m in logs.messages if "неизвестное поле" in m]
    assert hits, f"опечатка в имени поля снова молча игнорируется: {logs.messages}"
    assert "setings" in hits[0]
    assert str(world) in hits[0], "в предупреждении нет world_id — непонятно, какой мир"


def test_warning_names_allowed_fields(world, logs):
    db_mod.update_world(world, snaptshot={})
    msg = next(m for m in logs.messages if "неизвестное поле" in m)
    for field in ("setting", "snapshot", "tts_settings"):
        assert field in msg, "не перечислены допустимые поля — опечатку не найти"


def test_warning_emitted_once_per_key(world, logs):
    db_mod.update_world(world, bad_one=1, bad_two=2)
    assert sum("неизвестное поле" in m for m in logs.messages) == 2, \
        "лишние ключи обязаны быть видны поштучно, а не одним «что-то не так»"


def test_warning_is_warning_level(world, logs):
    """WARNING, а не DEBUG: в бою уровень WARNING (см. .env/админку), иначе не виден."""
    db_mod.update_world(world, nope=1)
    assert all(r.levelno == logging.WARNING for r in logs.records
               if "неизвестное поле" in r.getMessage())


# ── 2. поведение НЕ изменилось ───────────────────────────────────────────────

def test_unknown_field_still_not_written(world, logs):
    db_mod.update_world(world, setings={"player": {"gold": 999}})
    assert json.loads(db_mod.get_world(world)["setting"])["player"]["gold"] == 5, \
        "неизвестное поле обязано и дальше НЕ попадать в SQL"


def test_only_unknown_field_changes_nothing_yet_survives(world, logs):
    """Ранний `return` при пустом `sets` остаётся (SQL `UPDATE worlds SET` без колонок — ошибка)."""
    before = db_mod.get_world(world)["updated_at"]
    db_mod.update_world(world, nonsense=1)
    assert db_mod.get_world(world)["updated_at"] == before


def test_known_fields_unaffected(world, logs):
    db_mod.update_world(world, setting={"player": {"gold": 42}}, name="переименован")
    row = db_mod.get_world(world)
    assert json.loads(row["setting"])["player"]["gold"] == 42
    assert row["name"] == "переименован"
    assert not [m for m in logs.messages if "неизвестное поле" in m], \
        "на валидных полях предупреждений быть не должно"


def test_mixed_known_and_unknown_writes_known_only(world, logs):
    db_mod.update_world(world, nonsense=1, gen_settings={"max_tokens": 7})
    row = db_mod.get_world(world)
    assert json.loads(row["gen_settings"]) == {"max_tokens": 7}
    assert json.loads(row["setting"])["player"]["gold"] == 5
    assert any("неизвестное поле" in m for m in logs.messages)


def test_empty_kwargs_stays_silent_and_noop(world, logs):
    db_mod.update_world(world)
    assert logs.messages == [], "пустой вызов — не ошибка, шуметь не о чем"
    assert json.loads(db_mod.get_world(world)["setting"])["player"]["gold"] == 5


# ── 3. сторож исходника ─────────────────────────────────────────────────────

def test_source_guards_filter_with_warning():
    src = inspect.getsource(db_mod.update_world)
    assert "log.warning" in src, "фильтр `if k in allowed` снова без предупреждения"
    assert "if not sets:" in src, "защита от пустого SET-списка трогать нельзя (SQL упадёт)"
