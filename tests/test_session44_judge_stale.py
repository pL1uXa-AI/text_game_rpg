# -*- coding: utf-8 -*-
"""A4 (аудит 41): судья логики судил по УСТАРЕВШЕМУ состоянию мира.

`_maybe_logic_judge(world_id, setting, …)` принимал `setting` — снимок хода
(`agents_setting`, deepcopy на момент подачи задачи в `core.py`), честно перечитывал мир
из БД в `s`, но LLM-проход звал со СТАРЫМ аргументом: `narrator.logic_judge(world_id,
setting, …)`. За `await asyncio.sleep(1.5)` и ожидание bg-очереди мир успевал измениться
(или агент ждал минуты), и судья сверял ход с состоянием «на момент прошлого хода» →
ложные «🌫 искажения реальности» и `corrections`, правящие уже несуществующую роль.

Лечится одной строкой: судьё передаём `s` (перечитанное). `_maybe_autonomous_master`
(`generate_master_nudge(s, …)`) и `_maybe_enemy_ai` (`generate_enemy_ai(s, …)`) делали это
правильно всегда — `logic_judge` был единственным, кто брал аргумент.
"""
from __future__ import annotations

import asyncio
import json
import re

from backend import db as db_mod
from backend import narrator as narrator_mod
from backend.routers import core as core_mod

THEME_ID = narrator_mod.THEMES[0]["id"]


def _mk_world(client, name: str) -> int:
    r = client.post("/api/worlds", json={
        "theme_id": THEME_ID, "name": name, "difficulty": "normal",
        "perspective": "second", "language": "ru"})
    assert r.status_code == 200, r.text[:300]
    return r.json()["world_id"]


def _run_judge(monkeypatch, wid: int, stale: dict) -> dict:
    """Прогнать `_maybe_logic_judge` и вернуть состояние, с которым реально ушёл LLM-проход."""
    seen: dict = {}

    async def _no_sleep(*a, **kw):
        return None

    async def _judge(world_id, setting, action, reply, **kw):
        seen["setting"] = setting
        return None            # вердикт «чисто» — ничего не пишем, тест про один лишь вход

    monkeypatch.setattr(core_mod.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(narrator_mod, "logic_judge", _judge)
    core_mod._judge_busy.discard(wid)
    asyncio.run(core_mod._maybe_logic_judge(wid, stale, "ход", "ответ"))
    assert "setting" in seen, "судья вообще не дошёл до LLM-прохода (настроить интервал?)"
    return seen["setting"]


def test_judge_sees_state_from_db_not_turn_snapshot(monkeypatch, api_client):
    """В `logic_judge` уходит состояние, прочитанное из БД в момент прохода, а не снимок хода."""
    client, _holder = api_client
    wid = _mk_world(client, "A4")

    # «момент подачи задачи»: состояние из последнего хода
    snap = json.loads(db_mod.get_world(wid)["setting"])
    snap["_player_turns"] = 25           # интервал судьи накоплен — дойдём до прохода
    snap["player"]["gold"] = 10
    db_mod.update_world(wid, setting=snap)
    stale = json.loads(json.dumps(snap))  # именно так агенту передают deepcopy-снимок

    # фон мир УСПЕЛ изменить (агент стоял в очереди): в БД другие факты
    moved = json.loads(json.dumps(snap))
    moved["player"]["gold"] = 777
    moved["npc"] = {"бронз": {"name": "Бронз", "alive": False}}
    db_mod.update_world(wid, setting=moved)

    got = _run_judge(monkeypatch, wid, stale)

    assert got["player"]["gold"] == 777, (
        "судья получил золото из снимка хода — вердикт вынесен по устаревшему состоянию")
    assert got["npc"]["бронз"]["alive"] is False, "судья не видит, что NPC умер после снимка"
    assert got is not stale, "судье передали сам объект снимка, а не перечитанное состояние"


def test_judge_snapshot_unchanged_world_still_matches(monkeypatch, api_client):
    """Если мир за время ожидания не двигался — судья получает то же самое (регрессии нет)."""
    client, _holder = api_client
    wid = _mk_world(client, "A4-b")
    snap = json.loads(db_mod.get_world(wid)["setting"])
    snap["_player_turns"] = 9
    snap["player"]["gold"] = 42
    db_mod.update_world(wid, setting=snap)

    got = _run_judge(monkeypatch, wid, json.loads(json.dumps(snap)))
    assert got["player"]["gold"] == 42
    assert int(got["_player_turns"]) == 9


def test_bg_agents_llm_calls_get_fresh_state_not_argument():
    """Страж: фоновые агенты ядра зовут свои LLM-проходы на перечитанном `s`, не на аргументе.

    Текстовая проверка, потому что «пришёл старый снимок» не видно ни из типов, ни из
    поведения изолированного модуля — это срез одной функции, где рядом уже есть `s`.
    """
    src = open("backend/routers/core.py", encoding="utf-8").read()

    def body(fname: str) -> str:
        i = src.index(f"async def {fname}(")
        j = re.search(r"\n(?=(?:async )?def |class |# ═)", src[i + 1:])
        return src[i:i + 1 + (j.start() if j else len(src))]

    for fname, call in (("_maybe_logic_judge", r"narrator\.logic_judge\(\s*world_id,\s*s\b"),
                        ("_maybe_autonomous_master", r"narrator\.generate_master_nudge\(\s*s\b"),
                        ("_maybe_enemy_ai", r"narrator\.generate_enemy_ai\(\s*s\b")):
        text = body(fname)
        assert re.search(call, text), f"{fname}: LLM-проход снова берёт аргумент вместо `s`"
