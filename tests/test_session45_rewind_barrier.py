# -*- coding: utf-8 -*-
"""Сессия 45 — A5 (аудит 41): перемотка/загрузка сохранения и «барьер» фона.

Что было сломано: `rewind.rewind_to` писал в БД (setting, события, снапшоты), НЕ заявляя
ни о каком барьере, хотя все фоновые агенты защищены только `bg.regen_block`/`is_regenerating`
(через `_bg_may_write`), а идущий ход игрока вообще ни с чем не сверялся. Практический
симптом — «перемотал, а мир снова уехал в будущее»: откат состоявался в середине хода (или
в окне 1.5–2.5 с, пока живёт фон), и ход/агент дописывали СТАРОЕ состояние поверх отката.

Чиним честно и дёшево: весь откат идёт под `bg.rewind_block` — ТЕМ ЖЕ барьером `_regen`,
что и ↻ (поэтому фон молчит автоматически), плюс новый per-world флаг хода `bg.turn_block`
(по миру нельзя вести откат, а ход не стартует, пока мир откатывается). Занятый мир отдаёт
игроку честный 409 вместо тихой порчи прохождения.
"""
from __future__ import annotations

import asyncio
import copy
import json

import pytest
from fastapi import HTTPException

from backend import bg, db, rewind
from backend.routers import core as core_mod
from conftest import create_world_payload


def _mk_world(client) -> int:
    return client.post("/api/worlds", json=create_world_payload()).json()["world_id"]


def _play(client, wid: int, n: int) -> None:
    for i in range(n):
        r = client.post(f"/api/worlds/{wid}/action", json={"text": f"шаг {i}"})
        assert r.status_code == 200, r.text


def _rewind_point(wid: int, idx: int = 0) -> int:
    """Реальная точка перемотки (seq хода, по которому есть снимок состояния)."""
    return db.list_turn_snapshots(wid)[idx]["seq"]


def _fingerprint(wid: int) -> dict:
    """Всё, что откат обязан оставить нетронутым, если его не пустили."""
    return {"world": db.get_world(wid),
            "events": db.get_unfolded_events(wid, limit=0),
            "snapshots": db.list_turn_snapshots(wid)}


# ═══════════════════════ слой bg: сами барьеры ═══════════════════
def test_rewind_block_raises_regen_barrier_for_background():
    """Откат поднимает `_regen` → фон (судья/мастер/карточки/память) не пишет автоматически."""
    wid = 4501
    with bg.rewind_block(wid) as ok:
        assert ok is True
        assert bg.is_regenerating(wid), "откат не объявил барьер — фонпишет в откатанное"
        assert core_mod._bg_may_write(wid, {"_judge_last_turn": 0, "_player_turns": 5},
                                      "_judge_last_turn", 0, 5) is False
    assert not bg.is_regenerating(wid), "барьер не снят — мир заблокирован навсегда"
    assert core_mod._bg_may_write(wid, {"_judge_last_turn": 0, "_player_turns": 5},
                                  "_judge_last_turn", 0, 5) is True


def test_rewind_block_refuses_busy_world():
    """Занятый мир (↻, ход, второй откат) — второй откат обязан получить False."""
    wid = 4502
    with bg.regen_block(wid):                     # идёт ↻
        with bg.rewind_block(wid) as ok:
            assert ok is False, "откат влез в перегенерацию того же мира"
    with bg.turn_block(wid):                      # идёт ход игрока
        with bg.rewind_block(wid) as ok:
            assert ok is False, "откат влез в идущий ход того же мира"
        with bg.turn_block(wid) as again:         # задвоенный ход того же мира
            assert again is False
    with bg.rewind_block(wid) as first:           # после освобождения — откат возможен
        assert first is True
        with bg.turn_block(wid) as free:          # ...а ход в это окно — нет
            assert free is False


def test_turn_block_allows_regenerate_own_barrier():
    """↻ сам держит `_regen` — ему нельзя отказывать из-за собственного барьера."""
    wid = 4503
    with bg.regen_block(wid):
        with bg.turn_block(wid, allow_regen=True) as ok:
            assert ok is True, "перегенерация заблокирована собственным барьером — ↻ мёртв"


def test_rewind_busy_is_not_http_error():
    """`RewindBusy` — не ValueError: роутеры обязаны различать «занято» и «кривые данные»."""
    assert issubclass(rewind.RewindBusy, RuntimeError)
    assert not issubclass(rewind.RewindBusy, ValueError)


# ═══════════════════ роутеры: честный 409 вместо порчи ═══════════════════
def test_rewind_endpoint_409_while_turn_active(api_client):
    """Главный сценарий A5: ход в работе → POST /rewind = 409, мир НЕ тронут."""
    client, _ = api_client
    wid = _mk_world(client)
    _play(client, wid, 3)
    before = _fingerprint(wid)
    with bg.turn_block(wid):
        r = client.post(f"/api/worlds/{wid}/rewind", json={"seq": 3, "mode": "delete"})
        assert r.status_code == 409, r.text
        assert "ход" in r.json()["detail"].lower()
    after = _fingerprint(wid)
    assert json.loads(after["world"]["setting"]) == json.loads(before["world"]["setting"]), \
        "отказ перемотки всё равно изменил состояние мира"
    assert [e["id"] for e in after["events"]] == [e["id"] for e in before["events"]], \
        "отказ перемотки тронул таймлайн"
    # и после освобождения бара мир перемотается как обычно
    ok = client.post(f"/api/worlds/{wid}/rewind", json={"seq": 3, "mode": "delete"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["removed_events"] > 0


def test_rewind_endpoint_409_while_regen(api_client):
    """↻ этого же мира → перемотка не стартует вторым проходом (409)."""
    client, _ = api_client
    wid = _mk_world(client)
    _play(client, wid, 2)
    before = _fingerprint(wid)
    with bg.regen_block(wid):
        assert client.post(f"/api/worlds/{wid}/rewind",
                           json={"seq": 2}).status_code == 409
    assert _fingerprint(wid)["events"] == before["events"]


def test_load_save_409_while_turn_active(api_client):
    """Загрузка сохранения — тот же путь, что перемотка, и то же право на 409."""
    client, _ = api_client
    wid = _mk_world(client)
    _play(client, wid, 3)
    save = client.post(f"/api/worlds/{wid}/saves", json={"name": "слот"}).json()["save"]
    before = _fingerprint(wid)
    with bg.turn_block(wid):
        r = client.post(f"/api/worlds/{wid}/saves/{save['id']}/load")
        assert r.status_code == 409, r.text
    after = _fingerprint(wid)
    assert json.loads(after["world"]["setting"]) == json.loads(before["world"]["setting"])
    assert client.post(f"/api/worlds/{wid}/saves/{save['id']}/load").status_code == 200


def test_action_409_while_rewind_holds_barrier(api_client):
    """Ход не должен стартовать, пока мир откатывается (иначе он допишет прошлое)."""
    client, _ = api_client
    wid = _mk_world(client)
    _play(client, wid, 2)
    latest = db.latest_seq(wid)
    with bg.rewind_block(wid):
        r = client.post(f"/api/worlds/{wid}/action", json={"text": "поверх отката"})
        assert r.status_code == 409, r.text
        r2 = client.post(f"/api/worlds/{wid}/action",
                         json={"text": " перегенерация поверх отката", "regenerate": True})
        assert r2.status_code == 409, r2.text
    assert db.latest_seq(wid) == latest, "ход всё равно дописался в откатываемый таймлайн"


# ═══════════ реальная конкуренция: ход против идущего отката ═══════════
def test_concurrent_action_yields_to_running_rewind(api_client, monkeypatch):
    """Два прохода в одном цикле событий: откат «спит» посреди работы → ход получает 409.

    Проверяет не наличие флага, а именно окно гонки: без барьера ход дописал бы
    действие и setting ПОСЛЕ того, как таймлайн уже откатан (тот симптом, что описан в A5).
    """
    client, _ = api_client
    wid = _mk_world(client)
    _play(client, wid, 3)
    setting_before = json.loads(db.get_world(wid)["setting"])
    seqs_before = [e["id"] for e in db.get_unfolded_events(wid, limit=0)]

    async def slow_purge(*a, **kw):
        await asyncio.sleep(0.3)          # держим барьер на «длиенном await» внутри отката

    monkeypatch.setattr(rewind, "_purge_vectors", slow_purge)

    async def main():
        task = asyncio.ensure_future(rewind.rewind_to(wid, 3, mode="delete"))
        await asyncio.sleep(0.05)
        assert bg.is_regenerating(wid), "откат ещё не занял барьер — тест не про гонку"
        with pytest.raises(HTTPException) as ei:
            await core_mod._process_action(wid, "поверх отката")
        assert ei.value.status_code == 409
        return await task

    res = asyncio.run(main())
    assert res["ok"] is True
    assert json.loads(db.get_world(wid)["setting"]) == res["setting"]
    assert [e["id"] for e in db.get_unfolded_events(wid, limit=0)] != seqs_before or \
        res["removed_events"] == 0, "откат не подействовал вовсе"
    assert not bg.is_regenerating(wid)
    # откатанное состояние не испорчено гонкой
    assert json.loads(db.get_world(wid)["setting"]) != setting_before


def test_rewind_after_cancelled_attempt_leaves_no_stuck_barrier(api_client, monkeypatch):
    """Отменённый посреди работы откат обязан снять барьер, иначе мир заблокирован навсегда."""
    client, _ = api_client
    wid = _mk_world(client)
    _play(client, wid, 3)

    async def slow_purge(*a, **kw):
        await asyncio.sleep(5)

    monkeypatch.setattr(rewind, "_purge_vectors", slow_purge)

    async def main():
        task = asyncio.ensure_future(rewind.rewind_to(wid, 3, mode="delete"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(main())
    assert not bg.is_regenerating(wid), "барьер залип после отмены"
    assert client.post(f"/api/worlds/{wid}/rewind",
                       json={"seq": 3, "mode": "delete"}).status_code == 200


# ══════════════════ инварианты: не протухает ли старая защита ══════════════════
def test_rewind_still_invalidates_turn_registry(api_client, monkeypatch):
    """П.3B (сессия 36) должен остаться: сброс реестра хода — после отката, а не вместо него."""
    client, _ = api_client
    wid = _mk_world(client)
    _play(client, wid, 2)
    core_mod._turn_events[wid] = [1, 2, 3]
    core_mod._turn_seq[wid] = 9
    assert client.post(f"/api/worlds/{wid}/rewind",
                       json={"seq": _rewind_point(wid)}).status_code == 200
    assert wid not in core_mod._turn_events and wid not in core_mod._turn_seq


def test_plain_turn_does_not_leave_turn_flag(api_client):
    """Обычный ход не должен оставить флаг `is_turn_active` (иначе следующий — вечно 409)."""
    client, _ = api_client
    wid = _mk_world(client)
    _play(client, wid, 2)
    assert not bg.is_turn_active(wid)
    assert not bg.is_regenerating(wid)
    snap = copy.deepcopy(json.loads(db.get_world(wid)["setting"]))
    assert client.post(f"/api/worlds/{wid}/action", json={"text": "ещё шаг"}).status_code == 200
    assert not bg.is_turn_active(wid)
    assert json.loads(db.get_world(wid)["setting"]) != snap
