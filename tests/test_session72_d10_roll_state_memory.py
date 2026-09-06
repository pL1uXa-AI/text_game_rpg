# -*- coding: utf-8 -*-
"""D10 (аудит 41): `/roll` больше не «бросок вне мира».

Было: `routers/worlds.py::_slash_roll` возвращал `"state": None` (фронт не обновлял
сайдбар — `app.js` делает `if (res.state) { state.setting = res.state; renderSetting(...) }`)
и не писал событие броска в векторную память (RAG «не помнил» ключевые проверки).

Стало: отдаётся актуальное состояние мира, а событие индексируется в память фоновой
задачей через очередь `bg` (как ход в `routers/core.py`: `db.add_event(..., "dice")` +
`narrator.index_exchange`), без блокировки ответа игроку (правило 9).
"""
from __future__ import annotations

import json

from backend import db as db_mod
from backend import narrator as narrator_mod
from backend import bg as bg_mod
from backend.routers import worlds as worlds_mod


def _mk_world(client, name):
    return client.post("/api/worlds",
                       json={"theme_id": narrator_mod.THEMES[0]["id"], "name": name}).json()["world_id"]


# ── 1. state возвращается и он живой ────────────────────────────────────────

def test_roll_returns_world_state(api_client):
    client, _ = api_client
    wid = _mk_world(client, "D10-1")
    r = client.post(f"/api/worlds/{wid}/action", json={"text": "/roll d20"})
    assert r.status_code == 200
    body = r.json()
    assert body["state"], "`/roll` отдал state=None — сайдбар не обновится"
    assert isinstance(body["state"], dict)
    assert body["state"] == json.loads(db_mod.get_world(wid)["setting"])
    assert "🎲" in body["reply"]


def test_roll_state_reflects_turn_changes(api_client):
    """После хода с механикой `/roll` обязан отдать НОВОЕ состояние, а не старое снимок-пустышку."""
    client, holder = api_client
    wid = _mk_world(client, "D10-2")
    holder["reply"] = "Меч падает в руку. <<ENGINE>>{\"player\": {\"gold\": -7}}"
    client.post(f"/api/worlds/{wid}/action", json={"text": "достать меч"})
    body = client.post(f"/api/worlds/{wid}/action", json={"text": "/roll 1d6"}).json()
    gold = json.loads(db_mod.get_world(wid)["setting"])["player"]["gold"]
    assert body["state"]["player"]["gold"] == gold, "state в ответе броска разошёлся с БД"


def test_roll_bad_format_still_returns_state(api_client):
    """Даже «Формат: …» обязан нести состояние — иначе фронт молчит на любой опечатке."""
    client, _ = api_client
    wid = _mk_world(client, "D10-3")
    body = client.post(f"/api/worlds/{wid}/action", json={"text": "/roll абракадабра"}).json()
    assert "Формат" in body["reply"]
    assert isinstance(body["state"], dict) and body["state"]
    assert body["events"] == []


# ── 2. бросок уходит в память ───────────────────────────────────────────────

def test_roll_indexes_event_into_memory(api_client, monkeypatch):
    client, _ = api_client
    wid = _mk_world(client, "D10-4")
    calls: list = []

    async def _spy(world_id, seq, action, reply, provider=None):
        calls.append({"world_id": world_id, "seq": seq, "action": action, "reply": reply})

    monkeypatch.setattr(narrator_mod, "index_exchange", _spy)
    body = client.post(f"/api/worlds/{wid}/action", json={"text": "/roll d100"}).json()

    assert len(calls) == 1, f"бросок не проиндексирован в память: {calls}"
    ev = body["events"][0]
    assert calls[0]["world_id"] == wid
    assert calls[0]["seq"] == ev["seq"], "проиндексировано не то событие"
    assert "куб" in calls[0]["action"].lower()
    assert calls[0]["reply"] == ev["content"], "в память ушёл текст, отличный от показанного"


def test_roll_memory_goes_through_bg_queue(api_client, monkeypatch):
    """Индексация — фоновая задача очереди, а не `await` в ответе (правило 9)."""
    client, _ = api_client
    wid = _mk_world(client, "D10-5")
    seen: dict = {}

    async def _never(*a, **kw):  # если бы индексировали синхронно — счётчик вырос бы
        seen["ran"] = seen.get("ran", 0) + 1

    async def _capture(name, factory, **kw):
        seen["name"], seen["kw"] = name, kw
        return None  # фабрику НЕ вызываем: задача должна остаться в очереди

    monkeypatch.setattr(narrator_mod, "index_exchange", _never)
    monkeypatch.setattr(bg_mod, "submit", _capture)
    client.post(f"/api/worlds/{wid}/action", json={"text": "/roll 2d6+1"})

    assert seen.get("name") == "roll_memory", f"задача не ушла в bg-очередь: {seen}"
    assert seen["kw"].get("priority") == bg_mod.PRIO_MEMORY
    assert seen["kw"].get("agent") == "memory"
    assert not seen.get("ran"), "индексация выполнилась синхронно в ответе игроку"


def test_roll_survives_memory_failure(api_client, monkeypatch):
    """Отказ памяти не роняет бросок в 500: кубы уже сыграны и записаны (правило 14)."""
    client, _ = api_client
    wid = _mk_world(client, "D10-6")

    async def _boom(world_id, seq, action, reply, provider=None):
        raise RuntimeError("Chroma лежит")

    async def _submit(name, factory, **kw):
        return await factory()

    monkeypatch.setattr(narrator_mod, "index_exchange", _boom)
    monkeypatch.setattr(bg_mod, "submit", _submit)
    r = client.post(f"/api/worlds/{wid}/action", json={"text": "/roll d20"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "🎲" in body["reply"] and body["events"], "бросок потерялся из-за сбоя памяти"
    assert body["state"], "при отказе памяти state обязан остаться"


def test_roll_404_on_missing_world(api_client):
    client, _ = api_client
    assert client.post("/api/worlds/999999/action", json={"text": "/roll d20"}).status_code == 404


# ── 3. сторож исходника и дока ──────────────────────────────────────────────

def test_source_no_longer_returns_null_state():
    import inspect

    body = inspect.getsource(worlds_mod._slash_roll)
    assert '"state": None' not in body, "`/roll` снова отдаёт state=None"
    assert "index_exchange" in body, "`/roll` снова не пишет память"
    assert "_world_or_404" in body, "состояние должно читаться из БД (404 вместо 500)"
