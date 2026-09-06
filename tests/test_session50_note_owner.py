# -*- coding: utf-8 -*-
"""Сессия 50 — A10 (аудит 41): «/journal note дублирует journal.add_player_note()».

Было: `/journal note …` в `routers/worlds.py::_slash_journal` строил ключ карточки
РУКАМИ (`t{seq}-note-{md5}`), сам резал заметку и сам задавал `cat`/`icon`, а единственный
владелец этой логики — `journal.add_player_note()` (экспорт `__all__`, тест стабильного
ключа в test_session35_bugfixes) — приложением не вызывался вовсе. Два пути: разные
лимиты, разные мета-значения, и правки в `journal.py` (новый ключ/категория/иконка) в чате
просто не применялись.

Стало: роутер зовёт `journal.add_player_note(world_id, note)`.

Тесты — на ПОВЕДЕНИЕ (правило 19): проверяем, ЧТО попадает в БД через живой API и что
владелец логики реально участвует в пути (шпион поверх настоящего `add_player_note`, а не
чтение исходников роутера).
"""
from __future__ import annotations

import json

import pytest

from backend import journal as jr
from backend import db as db_mod


@pytest.fixture
def wid(api_client):
    client, _ = api_client
    return client.post("/api/worlds", json={"theme_id": "custom", "name": "x",
                                            "custom_plot": "сюжет", "genres": []}).json()["world_id"]


def _spy_owner(monkeypatch):
    """Шпион поверх НАСТОЯЩЕГО владельца: пишет вызовы, но не подменяет поведение."""
    calls: list[tuple] = []
    orig = jr.add_player_note

    def spy(world_id, note):
        calls.append((world_id, note))
        return orig(world_id, note)

    monkeypatch.setattr(jr, "add_player_note", spy)
    return calls


def test_slash_note_delegates_to_owner(wid, api_client, monkeypatch):
    """Чат-путь проходит через journal.add_player_note (единственный владелец логики)."""
    client, _ = api_client
    calls = _spy_owner(monkeypatch)
    r = client.post(f"/api/worlds/{wid}/action", json={"text": "/journal note помнить про мост"})
    assert r.status_code == 200, r.text
    assert "Записано" in r.json()["reply"]
    assert calls == [(wid, "помнить про мост")], calls


def test_slash_note_card_matches_owner_directly(wid, api_client):
    """Карточка из чата идентична той, что даёт владелец (ключ, cat, icon, seq)."""
    client, _ = api_client
    assert client.post(f"/api/worlds/{wid}/action",
                       json={"text": "/journal note через команду"}).status_code == 200
    rows = [dict(r) for r in db_mod.list_entities(wid, kind=jr.KIND)]
    got = next((x for x in rows if x["name"] == "через команду"), None)
    assert got, [x["name"] for x in rows]
    seq = db_mod.latest_seq(wid)
    want = jr.add_player_note(wid, "через команду")  # тот же текст → тот же ключ
    assert want and got["entity_key"] == want["entity_key"]
    meta = got["meta"]
    if isinstance(meta, str):  # list_entities отдаёт сырую строку JSON
        meta = json.loads(meta or "{}")
    assert meta["cat"] == jr.CAT_NOTE and meta["icon"] == jr._ICON[jr.CAT_NOTE]
    want_meta = want["meta"]
    if isinstance(want_meta, str):
        want_meta = json.loads(want_meta or "{}")
    assert meta["seq"] == seq == want_meta["seq"]


def test_slash_note_limits_and_dedup_come_from_owner(wid, api_client):
    """Лимит 400 и дедуп по (ход, текст) — единые для обоих путей."""
    client, _ = api_client
    long_note = "а" * 500
    assert client.post(f"/api/worlds/{wid}/action",
                       json={"text": f"/journal note {long_note}"}).status_code == 200
    body = client.get(f"/api/worlds/{wid}/journal").json()["entries"]
    mine = [e for e in body if e["cat"] == jr.CAT_NOTE]
    assert len(mine) == 1 and len(mine[0]["title"]) == 400, [len(e["title"]) for e in mine]
    # та же заметка второй раз — перезапись, а не дубль (стабильный ключ владельца)
    client.post(f"/api/worlds/{wid}/action", json={"text": f"/journal note {long_note}"})
    again = [e for e in client.get(f"/api/worlds/{wid}/journal").json()["entries"]
             if e["cat"] == jr.CAT_NOTE]
    assert len(again) == 1, again


def test_empty_note_writes_nothing(wid, api_client, monkeypatch):
    """«/journal note» без текста — не заметка: карточка не создаётся, «Записано» не врать."""
    client, _ = api_client
    calls = _spy_owner(monkeypatch)
    rep = client.post(f"/api/worlds/{wid}/action", json={"text": "/journal note  "}).json()["reply"]
    assert "Записано" not in rep
    assert not [c for c in calls if not (c[1] or "").strip()], calls
    assert db_mod.list_entities(wid, kind=jr.KIND) == []
