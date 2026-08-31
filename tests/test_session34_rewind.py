# -*- coding: utf-8 -*-
"""Сессия 34 — перемотка таймлайна и её следствия (фиксы A1, A4; фича C1).

Проверяет ровно то, что было сломано:
  A1 — загрузка сохранения сворачивала ВСЕ обмены (прошлое ≤ точки и будущее > точки)
       одним флагом `folded`, и недавняя история мира умирала безвозвратно;
  A4 — события, удалённые откатом, оставались в векторной памяти (ChromaDB);
  C1 — откат к произвольному ходу по снапшоту состояния.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend import db, rewind
from conftest import create_world_payload


def _mk_world(hp0: int = 50) -> int:
    return db.create_world(
        "тест", "custom", "фэнтези", "normal", "second", "ru", "",
        {"player": {"hp": hp0}, "locations": {}, "npc": {}, "quests": {}, "flags": {}},
        {})


def _play(wid: int, turns: int, snap_from: int = 1) -> None:
    """Ходы 1..turns: действие (seq 2i-1) + ответ (seq 2i); снапшот состояния перед ходом."""
    for i in range(1, turns + 1):
        if i >= snap_from:
            db.save_turn_snapshot(wid, 2 * i - 1,
                                  {"player": {"hp": 50 + i}, "locations": {}, "npc": {},
                                   "quests": {}, "flags": {}})
        db.add_event(wid, "player", f"действие {i}", seq=2 * i - 1)
        db.add_event(wid, "narrator", f"ответ {i}", seq=2 * i)


def _fold_into_summary(wid: int, first_seq: int, last_seq: int, sum_seq: int) -> None:
    """Как summarize_and_compress: сводка с meta.covers + пометка свёрнутых событий."""
    db.add_event(wid, "summary", f"сводка {first_seq}-{last_seq}", sum_seq,
                 meta={"covers": {"from": first_seq, "to": last_seq, "tokens": 600}})
    db.mark_folded(wid, last_seq)


def test_rewind_restores_state_and_removes_future():
    wid = _mk_world()
    _play(wid, 6)
    res = asyncio.run(rewind.rewind_to(wid, 7))          # к началу хода 4
    assert res["removed_events"] == 6                     # ходы 4,5,6 (по 2 события)
    st = json.loads(db.get_world(wid)["setting"])
    assert st["player"]["hp"] == 54          # снапшот хода 4 (seq 7) = 50+4, т.е. ДО четвёртого хода
    alive = [e["seq"] for e in db.get_events(wid)]
    assert max(alive) <= 7                   # хвост удалён; 7 — служебное сообщение о перемотке


def test_rewind_unfolds_history_that_summary_covered():
    """A1: то, что было свёрнуто в ставшую недостоверной сводку, обязано вернуться в окно."""
    wid = _mk_world()
    _play(wid, 6)
    _fold_into_summary(wid, 1, 6, 13)                     # ходы 1..3 свёрнуты
    assert db.get_unfolded_events(wid) and len(db.get_unfolded_events(wid)) == 6
    res = asyncio.run(rewind.rewind_to(wid, 7))           # откат к началу хода 4
    assert res["unfolded"] == 6, "свёрнутые ходы 1-3 должны вернуться в недавнее окно"
    assert res["removed_summaries"] == 1, "сводка про удалённое покрытие недостоверна"
    seqs = sorted({e["seq"] for e in db.get_unfolded_events(wid)})
    assert seqs == [1, 2, 3, 4, 5, 6]


def test_rewind_hide_mode_keeps_lines_but_out_of_prompt():
    wid = _mk_world()
    _play(wid, 6)
    res = asyncio.run(rewind.rewind_to(wid, 9, mode="hide"))
    assert res["mode"] == "hide"
    total = db.count_events(wid)
    visible = [e["seq"] for e in db.get_unfolded_events(wid)]
    assert total > len(visible), "hide НЕ удаляет строки журнала"
    assert all(s < 9 for s in visible), "сокрытое будущее не должно попасть в промпт"
    assert not any(s >= 9 for s in visible)


def test_rewind_hide_does_not_unhide_future_via_summary():
    """Ловушка, которую я сам заложил: разворот покрытия не должен вернуть сокрытое будущее."""
    wid = _mk_world()
    _play(wid, 6)
    _fold_into_summary(wid, 1, 6, 13)                     # свёрнуты ходы 1..3
    asyncio.run(rewind.rewind_to(wid, 5, mode="hide"))    # откат к началу хода 3
    visible = sorted({e["seq"] for e in db.get_unfolded_events(wid)})
    assert visible and max(visible) < 5, f"будущее сокрыто, а видно: {visible}"
    assert 1 in visible, "ходы до точки отката должны быть в окне"


def test_rewind_rejects_without_snapshot(api_client):
    """Нет снапшота — честная ошибка, а не молчаливый рассинхрон состояния и таймлайна."""
    client, _ = api_client
    r = client.post("/api/worlds", json=create_world_payload())
    assert r.status_code == 200, r.text
    wid = r.json()["world_id"]
    db.add_event(wid, "player", "ход", seq=1)
    r2 = client.post(f"/api/worlds/{wid}/rewind", json={"seq": 1, "mode": "delete"})
    assert r2.status_code == 400
    assert "сним" in r2.json()["detail"].lower()


def test_rewind_points_endpoint_lists_turns(api_client):
    client, holder = api_client
    holder["reply"] = "Ты идёшь дальше."
    wid = client.post("/api/worlds", json=create_world_payload()).json()["world_id"]
    for i in range(3):
        r = client.post(f"/api/worlds/{wid}/action", json={"text": f"осмотреться {i}"})
        assert r.status_code == 200, r.text
    pts = client.get(f"/api/worlds/{wid}/rewind/points").json()["points"]
    assert len(pts) >= 3
    assert all(p["seq"] > 0 and p["preview"] for p in pts)


def test_rewind_bad_input(api_client):
    client, _ = api_client
    wid = client.post("/api/worlds", json=create_world_payload()).json()["world_id"]
    assert client.post(f"/api/worlds/{wid}/rewind", json={"seq": 0}).status_code == 400
    assert client.post(f"/api/worlds/{wid}/rewind",
                       json={"seq": 1, "mode": "explode"}).status_code == 400
    assert client.post("/api/worlds/999999/rewind", json={"seq": 1}).status_code == 404


def test_load_save_restores_recent_memory(api_client):
    """A1 сквозняком: загрузил сохранение — недавняя история ЖИВАЯ (не пустая)."""
    client, holder = api_client
    holder["reply"] = "Ты идёшь вперёд."
    wid = client.post("/api/worlds", json=create_world_payload()).json()["world_id"]
    for i in range(4):
        client.post(f"/api/worlds/{wid}/action", json={"text": f"шаг {i}"})
    before = len(db.get_unfolded_events(wid))
    assert before >= 6
    save = client.post(f"/api/worlds/{wid}/saves", json={"name": "слот"}).json()["save"]
    for i in range(2):
        client.post(f"/api/worlds/{wid}/action", json={"text": f"лишнее {i}"})
    r = client.post(f"/api/worlds/{wid}/saves/{save['id']}/load", json={})
    assert r.status_code == 200, r.text
    after = db.get_unfolded_events(wid)
    assert after, "после загрузки сохранения недавняя история не должна быть пуста"
    assert all(e["seq"] <= save["seq"] for e in after)


def test_fold_state_helpers_roundtrip():
    wid = _mk_world()
    _play(wid, 3, snap_from=99)
    assert len(db.get_unfolded_events(wid)) == 6
    db.mark_folded(wid, 4)
    assert len(db.get_unfolded_events(wid)) == 2
    db.unfold_events(wid, 1, roles=("player", "narrator"))
    assert len(db.get_unfolded_events(wid)) == 6
    # mark_folded больше не трогает сводки и системные сообщения (иначе их не отличить)
    db.add_event(wid, "summary", "сводка", seq=20)
    db.add_event(wid, "system", "системное", seq=21)
    db.mark_folded(wid, 21)
    assert [e["seq"] for e in db.get_summary_events(wid)] == [20]
    sys_ev = [e for e in db.get_events(wid) if e["role"] == "system"]
    assert sys_ev and sys_ev[0]["folded"] == 0, "системные события не сворачиваются"


def test_turn_snapshots_pruned():
    wid = _mk_world()
    for i in range(1, 11):
        db.save_turn_snapshot(wid, i, {"player": {"hp": i}, "locations": {}, "npc": {},
                                       "quests": {}, "flags": {}}, keep=3)
    seqs = [s["seq"] for s in db.list_turn_snapshots(wid)]
    assert seqs == [8, 9, 10], f"должно остаться 3 самых свежих, есть {seqs}"


def test_count_events_sql_side():
    wid = _mk_world()
    _play(wid, 3, snap_from=99)
    db.add_event(wid, "system", "s")
    assert db.count_events(wid) == 7
    assert db.count_events(wid, roles=("player",)) == 3
    assert db.count_events(wid, roles=("player", "narrator"), unfolded_only=True) == 6


@pytest.mark.parametrize("mode", ["delete", "hide"])
def test_rewind_clears_game_over(mode):
    wid = _mk_world()
    _play(wid, 4)
    st = json.loads(db.get_world(wid)["setting"])
    st["game_over"] = True
    db.update_world(wid, setting=st)
    res = asyncio.run(rewind.rewind_to(wid, 5, mode=mode))
    assert not res["setting"].get("game_over"), "откат = жизнь продолжается"
