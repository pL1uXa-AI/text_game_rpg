"""Сессия 64 — D1 (аудит 41): «фейкового мира 0» в логе больше нет.

Было: `routers/worlds.py` при создании мира звал `_context_guard(0, …)`, и в
`data/logs/game.log` появлялся несуществующий «context_guard (world 0): контекст снижен
…». Ищешь причину обрезов у реального мира — и натыкаешься на строку, которая ни к какому
миру не относится (id=0 в SQLite не выдаётся: `worlds.id` — AUTOINCREMENT с 1).

Стало: `world_id: int | None`, `None` = «мир ещё не создан» и в логе называется честно.
Проверки деградационные: тест ловит и возврат к `0`, и потерю `None`-ветки в форматтере.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from backend import llm
from backend.routers import core

BACKEND = Path(__file__).resolve().parent.parent / "backend"


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture
def logs(monkeypatch):
    cap = _Capture()
    root = logging.getLogger("textgame")
    root.addHandler(cap)
    lvl = root.level
    root.setLevel(logging.DEBUG)
    try:
        yield cap
    finally:
        root.removeHandler(cap)
        root.setLevel(lvl)


def _guard(world_id, gen, probe_result, exc=None):
    async def probe(p):
        if exc:
            raise exc
        return probe_result

    orig = llm.probe_context
    llm.probe_context = probe
    try:
        return asyncio.run(core._context_guard(world_id, {"main": {}}, gen))
    finally:
        llm.probe_context = orig


def test_new_world_logs_honestly_not_world_zero(logs, fake_config):
    """None (мир ещё не создан) — в логе нет ни «world 0», ни пустого подстановочного места."""
    fake_config(detect_model_context=True)
    _guard(None, {"context_tokens": 32768}, {"max_context": 8192, "source": "llamacpp:/props"})
    warn = [m for m in logs.messages if "context_guard" in m]
    assert warn, "понижение контекста обязано остаться в логе"
    assert not any("world 0" in m for m in warn), warn
    assert "новый мир" in warn[0] and "context_guard (новый мир)" in warn[0]


def test_probe_failure_for_new_world_names_it_too(logs, fake_config):
    fake_config(detect_model_context=True)
    _guard(None, {"context_tokens": 8192}, None, exc=RuntimeError("сеть легла"))
    warn = [m for m in logs.messages if "probe не удался" in m]
    assert warn and "context_guard (новый мир)" in warn[0] and "world 0" not in warn[0]


def test_existing_world_still_logs_its_real_id(logs, fake_config):
    """Реальный id обязан печататься как раньше — D1 не должен «затереть» диагностику."""
    fake_config(detect_model_context=True)
    _guard(7, {"context_tokens": 200000}, {"max_context": 8192, "source": "openai:/models"})
    warn = [m for m in logs.messages if "context_guard" in m]
    assert warn and "world 7" in warn[0], warn


def test_guard_accepts_none_by_signature():
    """Сигнатура обязана допускать None (иначе mypy/читатель считают 0 правильным вариантом)."""
    import typing

    ann = typing.get_type_hints(core._context_guard)["world_id"]
    assert type(None) in typing.get_args(ann), ann


def test_create_world_does_not_pass_fake_ids():
    """Ни один вызов `_context_guard` в бэкенде не передаёт числовой литерал id мира."""
    import re

    bad = []
    for p in BACKEND.rglob("*.py"):
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"_context_guard\(\s*\d", line):
                bad.append(f"{p.name}:{n}: {line.strip()}")
    assert not bad, "фейковый world_id в логах: " + "; ".join(bad)
