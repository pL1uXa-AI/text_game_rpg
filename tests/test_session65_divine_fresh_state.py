# -*- coding: utf-8 -*-
"""Сессия 65 — D2 (аудит 41): база записи Провидения — свежее состояние мира.

Было (routers/worlds.py, `divine`): после сверки с `fresh` в БД уходило
`new_state = res.get("state") or setting` — т.е. снимок состояния, прочитанный ДО
LLM-прохода. `_bg_may_write` бережёт только от сдвига `_player_turns` и от перегенерации,
а фоновые агенты (судья, мастер, боевой ИИ, динамическое событие, карточки) пишут setting,
НЕ двигая счётчик хода — их правки, случившиеся за секунды воззвания, Провидение зтирало
обратно «старым» состоянием.

Стало: базой записи служит `fresh`, а директивы Провидения применяются повторно уже к нему
(сам LLM-проход меняет мир ТОЛЬКО директивами — `narrator.divine_intervene` →
`apply_directives`), поэтому на свежий снимок переносится ровно то, что боги решили.

Каждый тест обязан был бы падать до фикса.
"""
from __future__ import annotations

import json

import backend.db as db_mod
from backend import narrator as narrator_mod


def _mk_world(client, name="Мир"):
    r = client.post("/api/worlds", json={"theme_id": narrator_mod.THEMES[0]["id"], "name": name})
    assert r.status_code == 200, r.text
    return r.json()["world_id"]


def _setting(wid: int) -> dict:
    return json.loads(db_mod.get_world(wid)["setting"])


def test_divine_keeps_background_edits(api_client, monkeypatch):
    """Правка фоновых агентов, случившаяся за LLM-проход, обязана пережить воззвание (D2)."""
    client, _ = api_client
    wid = _mk_world(client, "D2-фон")

    async def _racing_divine(world_id, world, setting, complaint, **kw):
        # фоновый агент правит мир в обход HTTP: он НЕ двигает _player_turns, поэтому
        # `_bg_may_write` его не замечает (и не должен — ход игрока тут ни при чём)
        s = _setting(world_id)
        s["_bg_note"] = "карточку обновил фон во время воззвания"
        s["player"]["gold"] = 777          # то, что добавил НЕ Провидение
        db_mod.update_world(world_id, setting=s)
        # прежний divine_intervene вернул бы именно ЭТОТ (старый) снимок
        return {"decline": False, "twist": "Реальность вздрагивает.",
                "directives": {"player": {"gold": 5}}, "sys_msgs": ["+5 золотого"],
                "state": setting}

    monkeypatch.setattr(narrator_mod, "divine_intervene", _racing_divine)
    r = client.post(f"/api/worlds/{wid}/divine", json={"complaint": "мне не дали золото"})
    assert r.status_code == 200, r.text

    saved = _setting(wid)
    assert saved.get("_bg_note") == "карточку обновил фон во время воззвания", \
        "правка фоновых агентов затёрта старым снимком состояния (D2)"
    assert saved["player"]["gold"] == 782, \
        f"фоновое золото потеряно, а выдача не леглась: {saved['player']['gold']}"
    assert r.json()["state"] == saved, "ответ разошёлся с записанным состоянием"


def test_divine_grants_apply_to_fresh_state(api_client, monkeypatch):
    """Директивы Провидения применяются к свежей базе: выдача в БД, а не только в ответе."""
    client, _ = api_client
    wid = _mk_world(client, "D2-выдача")
    gold0 = int(_setting(wid)["player"].get("gold", 0) or 0)

    async def _stale_divine(world_id, world, setting, complaint, **kw):
        # «насолничал» на старом снимке и вернул его же
        narrator_mod.apply_directives(setting, {"player": {"gold": 3}})
        return {"decline": False, "twist": "Боги щедричают.",
                "directives": {"player": {"gold": 3}}, "sys_msgs": [], "state": setting}

    monkeypatch.setattr(narrator_mod, "divine_intervene", _stale_divine)
    assert client.post(f"/api/worlds/{wid}/divine",
                       json={"complaint": "обещанное золото не выдано"}).status_code == 200
    assert _setting(wid)["player"]["gold"] == gold0 + 3, \
        "выдача Провидения не леглась на свежее состояние"


def test_divine_decline_without_state_changes_only_cooldown(api_client, monkeypatch):
    """`state: None` (отказ) больше не означает «записать старый снимок» — только кулдаун."""
    client, _ = api_client
    wid = _mk_world(client, "D2-отказ")

    async def _decline(world_id, world, setting, complaint, **kw):
        # фоново мир успели потрогать, а Провидение отказало и состояние не вернуло
        s = _setting(world_id)
        s["_bg_flag"] = 1
        db_mod.update_world(world_id, setting=s)
        return {"decline": True, "twist": "Ошибки нет, путник.",
                "directives": None, "sys_msgs": [], "state": None}

    monkeypatch.setattr(narrator_mod, "divine_intervene", _decline)
    r = client.post(f"/api/worlds/{wid}/divine", json={"complaint": "давай всё равно золото"})
    assert r.status_code == 200, r.text
    after = _setting(wid)
    turns = after.get("_player_turns", 0) or 0
    assert after.get("_bg_flag") == 1, "отказ Провидения затёр правку фона"
    assert after.get("_divine_last_turn") == turns, "метка кулдауна не из свежего состояния"
    assert "player" in after and after.get("game_over", False) is False


def test_divine_still_rejects_stale_turn(api_client, monkeypatch):
    """A15 не пострадала: новый ход за время воззвания — по-прежнему 409, мир цел."""
    client, _ = api_client
    wid = _mk_world(client, "D2-гонка")

    async def _racing(world_id, world, setting, complaint, **kw):
        s = _setting(world_id)
        s["_player_turns"] = int(s.get("_player_turns", 0) or 0) + 3
        s["_race_marker"] = "ход игрока"
        db_mod.update_world(world_id, setting=s)
        return {"decline": False, "twist": "боги молчат", "sys_msgs": [],
                "directives": {"player": {"gold": 99}}, "state": setting}

    monkeypatch.setattr(narrator_mod, "divine_intervene", _racing)
    r = client.post(f"/api/worlds/{wid}/divine", json={"complaint": "мне не дали награду"})
    assert r.status_code == 409, f"устаревшее состояние принято ({r.status_code})"
    saved = _setting(wid)
    assert saved.get("_race_marker") == "ход игрока"
    assert "_divine_last_turn" not in saved, "отказавшееся воззвание поставило метку кулдауна"


def test_no_stale_snapshot_left_in_divine_write(api_client):
    """Сторож формата: в теле `divine` нет возврата к `res["state"] or setting`."""
    src = open("backend/routers/worlds.py", encoding="utf-8").read()
    seg = src[src.index("async def divine("):]
    seg = seg[:seg.index("async def trigger_vision")]
    assert 'res.get("state") or setting' not in seg, "база записи снова старый снимок (D2)"
    assert "new_state = fresh" in seg, "база записи больше не перечитанный fresh"
