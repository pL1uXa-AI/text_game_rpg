# -*- coding: utf-8 -*-
"""Сессия 34 — функциональные фичи C1/C2/C7/C9 через API (end-to-end).

Проверяет, что новые эндпоинты и директивы реально работают в связке
(закон 2/3: движок хранит/проверяет/показывает, решает рассказчик):
  C1 — перемотка к ходу (delete/hide), точки перемотки;
  C2 — дневник пишется в транзакции хода и отдаётся через /journal;
  C7 — /risk справка по ресурсам;
  C9 — «ружья Чехова»: заряжаются из диффа и гаснут по TTL.
"""
from __future__ import annotations

import json

from backend import db, journal, risk


def _mk_base_world() -> int:
    return db.create_world(
        "тест", "custom", "фэнтези", "normal", "second", "ru", "",
        {"player": {"hp": 50, "max_hp": 50, "mp": 10, "max_mp": 10, "gold": 12, "level": 1,
                    "stats": {"сила": 14, "ловкость": 9, "интеллект": 16}, "inventory": [],
                    "abilities": {}, "progress": {}},
         "locations": {"a": {"name": "Таверна"}}, "current_location": "a",
         "npc": {}, "quests": {}, "flags": {}, "timers": {}, "enemies": {}}, {})


def _two_turns(api_client, wid, holder):
    """Два хода: первый приносит вещь+знакомство, второй — двигает сюжет."""
    for text, engine in (
        ("осмотреться", '{"add_item":[{"name":"Старый компас","qty":1}],"npc_set":{"id":"barman","name":"Трактирщик"},"quest":{"id":"q1","title":"Найти дневник","status":"active"}}'),
        ("поговорить с трактирщиком", '{"flag":{"name":"подвал_найден","value":true}}'),
    ):
        holder["reply"] = f"Ты: {text}. <<ENGINE>>{engine}"
        r = api_client.post(f"/api/worlds/{wid}/action", json={"text": text})
        assert r.status_code == 200, r.text


def test_journal_is_written_on_turn_and_served(api_client):
    client, holder = api_client
    wid = client.post("/api/worlds", json={"theme_id": "custom", "name": "x",
                                           "custom_plot": "сюжет", "genres": []}).json()["world_id"]
    _two_turns(client, wid, holder)
    d = client.get(f"/api/worlds/{wid}/journal").json()
    titles = [e["title"] for e in d["entries"]]
    assert any("Старый компас" in t for t in titles), titles
    assert any("Трактирщик" in t for t in titles), titles
    assert any("Найти дневник" in t for t in titles), titles
    assert any("подвал_найден" in t for t in titles), titles
    cats = {c["cat"] for c in d["categories"]}
    assert {"item", "npc", "quest", "flag"} <= cats, cats


def test_journal_player_note_via_slash(api_client):
    client, holder = api_client
    holder["reply"] = "Ты записываешь мысль."
    wid = client.post("/api/worlds", json={"theme_id": "custom", "name": "x",
                                           "custom_plot": "сюжет", "genres": []}).json()["world_id"]
    r = client.post(f"/api/worlds/{wid}/action", json={"text": "/journal note помнить про руны"})
    assert r.status_code == 200
    d = client.get(f"/api/worlds/{wid}/journal").json()
    assert any("руны" in e["title"] for e in d["entries"])


def test_risk_returns_resources_and_no_verdict(api_client):
    client, _ = api_client
    wid = client.post("/api/worlds", json={"theme_id": "custom", "name": "x",
                                           "custom_plot": "сюжет", "genres": []}).json()["world_id"]
    d = client.get(f"/api/worlds/{wid}/risk?idea=переговоры").json()
    assert "переговоры" in d["reply"] or "харизма" in d["reply"] or "обман" in d["reply"]
    assert "решает Рассказчик" in d["reply"]   # закон 3: справка, не вердикт


def test_chekhov_guns_lifecycle():
    """C9: ружья заряжаются из диффа, снимаются по упоминанию, гаснут по TTL."""
    prev = {"player": {"hp": 50, "inventory": []}, "npc": {}, "locations": {"a": {"name": "Таверна"}},
            "flags": {}, "quests": {}, "timers": {}}
    now = {"player": {"hp": 50, "inventory": [{"name": "Старый компас"}]},
           "npc": {"bar": {"name": "Трактирщик у камина"}},
           "locations": {"a": {"name": "Таверна"}, "b": {"name": "Крыша"}},
           "current_location": "b", "flags": {}, "quests": {}, "timers": {}}
    st = dict(now)
    guns = journal.chekhov_update(st, prev, now, reply="Ты на крыше.", action="залезть", seq=1)
    subjects = [g["subject"] for g in guns]
    assert "Старый компас" in subjects and "Трактирщик у камина" in subjects, subjects
    # упоминание компаса коротко — ружьё снимается
    st2 = dict(st, _chekhov=[dict(g) for g in guns])
    after = journal.chekhov_update(st2, now, st2, reply="Компас мёртв.", action="", seq=2)
    assert not any("омпас" in g["subject"] for g in after), after
    # TTL: молчание 13 ходов гасит всё
    st3 = dict(st2, _chekhov=[dict(g) for g in after])
    for i in range(3, 16):
        st3 = dict(st3, _chekhov=[dict(g) for g in st3["_chekhov"]])
        journal.chekhov_update(st3, now, st3, reply="тишина", action="", seq=i)
    assert st3["_chekhov"] == []


def test_rewind_via_api_delete(api_client):
    client, holder = api_client
    holder["reply"] = "Ты идёшь дальше."
    wid = client.post("/api/worlds", json={"theme_id": "custom", "name": "x",
                                           "custom_plot": "сюжет", "genres": []}).json()["world_id"]
    for i in range(4):
        client.post(f"/api/worlds/{wid}/action", json={"text": f"шаг {i}"})
    pts = client.get(f"/api/worlds/{wid}/rewind/points").json()["points"]
    assert len(pts) >= 4
    target = pts[1]["seq"]          # откат к началу второго хода
    r = client.post(f"/api/worlds/{wid}/rewind", json={"seq": target, "mode": "delete"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["removed_events"] > 0
    seqs = [e["seq"] for e in client.get(f"/api/worlds/{wid}/history?limit=0").json()["events"]]
    assert max(seqs) <= target, f"после перемотки не должно быть ходов после {target}, есть {max(seqs)}"


def test_rewind_hide_keeps_history_but_hides():
    wid = _mk_base_world()
    for i in range(1, 5):
        db.save_turn_snapshot(wid, i, {"player": {"hp": 10 + i}, "locations": {}, "npc": {},
                                       "quests": {}, "flags": {}})
        db.add_event(wid, "player", f"шаг {i}", seq=i)
    from backend import rewind as rw
    import asyncio
    res = asyncio.run(rw.rewind_to(wid, 3, mode="hide"))
    assert res["mode"] == "hide"
    visible = [e["seq"] for e in db.get_unfolded_events(wid)]
    assert all(s < 3 for s in visible), visible
    total = db.count_events(wid)
    assert total >= 4, "hide не удаляет строки журнала"


def test_risk_module_axes():
    axes = risk.risk_axes_ids()
    assert len(axes) == 9
    assert "stealth" in axes and "craft" in axes
    out = risk.describe_risk({}, "съесть камень")
    assert isinstance(out, str) and out


def test_journal_entries_are_excluded_from_model_context():
    """Карточки дневника не должны попадать в select_relevant_entities (приоритет −100)."""
    from backend import memory
    wid = _mk_base_world()
    db.upsert_entity(wid, "journal", "t1-npc-x", name="Новое знакомство: Трактирщик",
                     meta={"seq": 1, "cat": "npc"}, seq=1)
    db.upsert_entity(wid, "npc", "barman", name="Трактирщик", summary="жив", seq=1)
    setting = json.loads(db.get_world(wid)["setting"])
    cards = memory.select_relevant_entities(wid, setting, "поговорить с трактирщиком", limit=5)
    kinds = [c["kind"] for c in cards]
    assert "journal" not in kinds, kinds
    assert "npc" in kinds, kinds
