# -*- coding: utf-8 -*-
"""Сессия 47 — A7 (аудит 41): «/journal и категории дневника сканируют ВСЕ карточки».

Было: `journal.entries()` брал `db.list_entities(world_id, kind="journal")` — ВСЕ карточки
мира, парсил meta в Python, а `cat`/`limit` резал уже ПОСЛЕ этого; `entry_categories()`
звал `entries(limit=0)` (ещё один полный перебор) на каждый GET /journal и на каждый
`/journal <поиск>`. Дневник — карточка на каждое значимое событие хода, поэтому на длинном
прохождении это ровно тот класс B5, что уже чинили для events.

Стало: потолок (`LIMIT ?`), фильтр категории и группировка-счётчик считаются SQLite
(`db.list_cards` / `db.count_cards_by_cat`); JSON в Python на этом пути не парсится.

Тесты — на ПОВЕДЕНИЕ (правило 19): шпион сверяет, что ушло в слой запроса, и что отдаёт
живая тестовая БД / API; текст исходников не читаем.
"""
from __future__ import annotations

import sqlite3

import pytest

from backend import config as config_mod
from backend import db as db_mod
from backend import journal as jr


def _spy(monkeypatch):
    """Шпион над слоем запросов карточек: пишет, ЧТО ушло в БД, и зовёт оригинал."""
    seen: list[dict] = []
    orig = db_mod.list_cards

    def spy(world_id, kind, limit=0, cat=""):
        seen.append({"limit": limit, "cat": cat})
        return orig(world_id, kind, limit=limit, cat=cat)

    monkeypatch.setattr(jr.db, "list_cards", spy)
    return seen


def _add(wid, key, seq, cat, title=None):
    db_mod.upsert_entity(wid, jr.KIND, key, name=title or f"запись {key}",
                         summary="текст", seq=seq, meta={"seq": seq, "cat": cat})


def _raw_meta(wid, key, meta):
    """Прямая правка строки: проверяем поведение на битой/пустой meta (её даёт жизнь)."""
    conn = sqlite3.connect(config_mod.get_config().db_path)
    try:
        conn.execute("UPDATE entities SET meta = ? WHERE world_id = ? AND entity_key = ?",
                     (meta, wid, key))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def wid(api_client):
    client, _ = api_client
    return client.post("/api/worlds", json={"theme_id": "custom", "name": "x",
                                            "custom_plot": "сюжет", "genres": []}).json()["world_id"]


def test_entries_send_limit_and_cat_to_query_layer(wid, monkeypatch):
    """`limit` и `cat` обязаны уходить в запрос, а не срезаться в Python после всей выдачи."""
    seen = _spy(monkeypatch)
    for i in range(1, 26):
        _add(wid, f"t{i}", i, "quest" if i % 2 else "npc")

    out = jr.entries(wid, limit=5)
    assert len(out) == 5, "лимит не доехал до запроса — отдаётся вся выборка"
    assert [o["seq"] for o in out] == [21, 22, 23, 24, 25], "хронологический порядок сломан"
    assert seen[-1] == {"limit": 5, "cat": ""}

    jr.entries(wid, limit=50, cat="npc")
    assert seen[-1]["cat"] == "npc", "фильтр категории режется в Python, а не в SQL"
    assert {o["cat"] for o in jr.entries(wid, limit=50, cat="npc")} == {"npc"}
    assert {o["cat"] for o in jr.entries(wid, limit=0)} == {"quest", "npc"}


def test_categories_never_enumerate_cards(wid, monkeypatch):
    """Счётчик категорий не выбирает карточки вообще — GROUP BY делает SQLite."""
    for i in range(1, 8):
        _add(wid, f"c{i}", i, "quest" if i < 5 else "note")
    called: list = []
    monkeypatch.setattr(jr.db, "list_cards", lambda *a, **k: called.append(a) or [])
    cats = {c["cat"]: c for c in jr.entry_categories(wid)}
    assert not called, "entry_categories по-прежнему перебирает карточки через entries()"
    assert (cats["quest"]["count"], cats["note"]["count"]) == (4, 3)
    assert cats["quest"]["icon"] == "📜"
    assert [c["cat"] for c in jr.entry_categories(wid)][0] == "quest", "сортировка по убыванию"


def test_broken_or_absent_meta_falls_back_to_world(wid):
    """Битая/пустая/не-объект meta: карточка не роняет дневник и не пропадает из счётчика."""
    for key in ("empty", "broken", "listy", "nocat"):
        _add(wid, key, 1, "quest")
    _raw_meta(wid, "empty", "")
    _raw_meta(wid, "broken", "не json")
    _raw_meta(wid, "listy", "[]")
    _raw_meta(wid, "nocat", '{"x": 1}')
    items = jr.entries(wid, limit=100)
    assert len(items) == 4, [i["cat"] for i in items]
    assert {i["cat"] for i in items} == {"world"}
    assert all(i["icon"] == "🌍" for i in items)
    counts = {c["cat"]: c["count"] for c in jr.entry_categories(wid)}
    assert counts == {"world": 4}, counts


def test_journal_endpoint_bounded_but_counts_full(wid, api_client):
    """GET /journal?limit=… режет выдачу, категории считают весь дневник (как раньше)."""
    client, _ = api_client
    for i in range(1, 13):
        _add(wid, f"e{i}", i, "quest")
    d = client.get(f"/api/worlds/{wid}/journal?limit=3").json()
    assert [e["seq"] for e in d["entries"]] == [10, 11, 12]
    assert d["categories"] == [{"cat": "quest", "icon": "📜", "count": 12}]
    assert client.get(f"/api/worlds/{wid}/journal?limit=100&cat=note").json()["entries"] == []


def test_slash_journal_note_and_search_still_work(wid, api_client):
    """Пользовательский путь не поехал: заметка появляется, поиск находит, пустой мир не падает."""
    client, holder = api_client
    holder["reply"] = "Ты записываешь мысль."
    r = client.post(f"/api/worlds/{wid}/action", json={"text": "/journal note помнить про руны"})
    assert r.status_code == 200, r.text
    body = client.get(f"/api/worlds/{wid}/journal").json()["entries"]
    assert any("руны" in e["title"] for e in body)
    hit = client.post(f"/api/worlds/{wid}/action", json={"text": "/journal руны"}).json()["reply"]
    assert "руны" in hit and "Дневник" in hit, hit
    miss = client.post(f"/api/worlds/{wid}/action", json={"text": "/journal ежа"}).json()["reply"]
    assert "ничего нет" in miss, miss


def test_render_and_record_turn_unchanged(wid):
    """После перехода фильтра в SQL render()/record_turn отдают то же, что и раньше."""
    saved = jr.record_turn(wid, {}, {"quests": {"q1": {"title": "Найти дневник",
                                                       "status": "active"}}}, seq=4)
    assert saved and jr.entries(wid, limit=10)[0]["title"]
    txt = jr.render(wid, limit=5)
    assert "Найти дневник" in txt and "ход 4" in txt, txt
