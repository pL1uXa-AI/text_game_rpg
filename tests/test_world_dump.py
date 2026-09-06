# -*- coding: utf-8 -*-
"""Сессия 33 — переносимость и бэкап (реальная проблема из «Актуального анализа»).

Закрывает дыру: до этой сессии из игры нельзя было вынести мир — /export отдавал
только текст истории, а `data/` (841 МБ) целиком лежал в .gitignore.

Проверяет:
  * db.world_dump собирает состояние + события + карточки + лор + слоты + граф;
  * из дампа ВЫРЕЗАНЫ ключи API (дамп — файл, он не должен утаскивать секреты);
  * db.restore_world заводит НОВЫЙ мир и ничего не затирает;
  * GET /api/worlds/{id}/export/json и POST /api/worlds/import/json (round-trip);
  * битый/чужой payload → 400, а не 500;
  * db.backup_database делает согласованный снимок БД и режет старые.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
from pathlib import Path

import pytest

from backend import db


@pytest.fixture()
def world_with_history(api_client):
    """Мир с ходами, карточкой NPC, якорной лор-статьёй, слотом сохранения и правками
    провайдера (чтобы проверить, что ключ не уезжает в дамп)."""
    client, _holder = api_client
    from backend import narrator as narrator_mod
    theme = narrator_mod.THEMES[0]["id"]
    r = client.post("/api/worlds", json={"theme_id": theme, "name": "Дамп-мир"})
    assert r.status_code == 200, r.text
    wid = r.json()["world_id"]
    for _ in range(2):
        rr = client.post(f"/api/worlds/{wid}/action", json={"text": "осмотреться"})
        assert rr.status_code == 200, rr.text

    db.upsert_entity(wid, "npc", "guard", name="Стражник", summary="на посту",
                     relationship="настороже", bio_add="встретились у ворот",
                     meta={"alive": True})
    db.create_lore(wid, "Ордено стражи", "Стражники служат городу с древности.",
                   tags="стража", is_core=True, source="user")
    setting = json.loads(db.get_world(wid)["setting"])
    db.create_save(wid, "ручной слот", setting, db.latest_seq(wid))
    # per-world переопределение провайдера с СЕКРЕТОМ — не должно попасть в дамп
    client.post(f"/api/worlds/{wid}/providers", json={
        "main": {"id": "openai_compat", "api_key": "TESTKEY-DONOTDUMP-0000",
                 "model": "some/model"}})
    return client, wid


def test_world_dump_contains_everything(world_with_history):
    _client, wid = world_with_history
    d = db.world_dump(wid)
    assert d and d["format"] == "textgame.world.dump"
    assert d["world"]["name"] == "Дамп-мир"
    assert isinstance(d["world"]["setting"], dict)
    assert d["counts"]["events"] >= 4            # 2 хода × (игрок + рассказчик)
    assert d["counts"]["entities"] >= 1
    assert d["counts"]["lore"] >= 1
    assert d["counts"]["saves"] >= 1
    assert d["counts"]["graph_nodes"] >= 1       # карта синхронизируется в транзакции хода
    roles = {e["role"] for e in d["events"]}
    assert {"player", "narrator"} <= roles
    kinds = {c["kind"] for c in d["entities"]}
    assert "npc" in kinds


def test_world_dump_strips_api_keys(world_with_history):
    """Жёсткое требование: дамп — файл, который будут копировать/коммитить."""
    _client, wid = world_with_history
    d = db.world_dump(wid)
    blob = json.dumps(d, ensure_ascii=False)
    assert "TESTKEY-DONOTDUMP-0000" not in blob
    for kind, prov in (d["world"]["provider_settings"] or {}).items():
        if isinstance(prov, dict):
            assert "api_key" not in prov, f"ключ провайдера {kind} утёк в дамп"


def test_world_dump_missing_world_returns_none():
    assert db.world_dump(999_999) is None


def test_restore_world_creates_new_world(world_with_history):
    _client, wid = world_with_history
    d = db.world_dump(wid)
    new_id = db.restore_world(d)
    assert new_id != wid
    # состояние, события, карточки, лор и слоты перенесены один-в-один
    src, dst = json.loads(db.get_world(wid)["setting"]), json.loads(db.get_world(new_id)["setting"])
    assert dst == src
    assert [(e["seq"], e["role"], e["content"]) for e in db.get_events(new_id)] == \
           [(e["seq"], e["role"], e["content"]) for e in db.get_events(wid)]
    assert db.get_entity(new_id, "npc", "guard")["name"] == "Стражник"
    assert any(a["title"] == "Ордено стражи" for a in db.list_lore(new_id))
    assert any(s["name"] == "ручной слот" for s in db.list_saves(new_id))
    # исходный мир цел
    assert db.get_world(wid) is not None


def test_export_import_json_roundtrip(world_with_history):
    client, wid = world_with_history
    r = client.get(f"/api/worlds/{wid}/export/json")
    assert r.status_code == 200, r.text
    payload = r.json()
    assert "api_key" not in json.dumps(payload)
    r2 = client.post("/api/worlds/import/json", json={"payload": payload})
    assert r2.status_code == 200, r2.text
    new_id = r2.json()["world_id"]
    assert client.get(f"/api/worlds/{new_id}").status_code == 200
    # A8 (аудит 41): сравниваем события страницы, а не сам dict ответа {events, truncated}
    assert len(client.get(f"/api/worlds/{new_id}/history").json()["events"]) == \
           len(client.get(f"/api/worlds/{wid}/history").json()["events"])


def test_export_json_download_header(world_with_history):
    client, wid = world_with_history
    r = client.get(f"/api/worlds/{wid}/export/json?download=1")
    assert r.status_code == 200
    cd = r.headers.get("content-disposition", "")
    assert "attachment;" in cd
    # русское имя мира не должно ломать заголовок: есть ASCII-фолбэк и UTF-8 версия
    assert 'filename="' in cd and "filename*=UTF-8''" in cd


def test_export_json_404_and_import_bad_payload(world_with_history):
    client, _wid = world_with_history
    assert client.get("/api/worlds/999999/export/json").status_code == 404
    # не наш формат и пустой setting → 400 (не 500)
    assert client.post("/api/worlds/import/json", json={"payload": {"nope": 1}}).status_code == 400
    assert client.post("/api/worlds/import/json",
                       json={"payload": {"format": "textgame.world.dump"}}).status_code == 400


def test_reindex_after_import_is_best_effort(world_with_history, monkeypatch):
    """Фоновая перестройка памяти после импорта не должна уметь ронять сервер/ход."""
    from backend.routers import worlds as worlds_mod
    from backend import narrator as narrator_mod

    client, wid = world_with_history
    payload = client.get(f"/api/worlds/{wid}/export/json").json()
    new_id = client.post("/api/worlds/import/json", json={"payload": payload}).json()["world_id"]

    calls = {"exchange": 0, "entities": 0, "lore": 0}

    async def fake_index_exchange(world_id, seq, action, reply, provider=None):
        calls["exchange"] += 1

    async def fake_index_entities(world_id, cards):
        calls["entities"] += 1

    async def fake_index_lore(world_id, providers=None):
        calls["lore"] += 1

    monkeypatch.setattr(narrator_mod, "index_exchange", fake_index_exchange)
    monkeypatch.setattr(narrator_mod, "index_entities", fake_index_entities)
    monkeypatch.setattr(worlds_mod, "_index_world_lore", fake_index_lore)
    asyncio.run(worlds_mod._reindex_imported_world(new_id))
    assert calls["lore"] == 1 and calls["entities"] == 1
    assert calls["exchange"] >= 2, "обмены (действие→ответ) обязаны переиндексироваться парами"

    # недоступная память/облако — только warning, не исключение
    async def boom(*a, **kw):
        raise RuntimeError("chroma лег")

    monkeypatch.setattr(narrator_mod, "index_entities", boom)
    asyncio.run(worlds_mod._reindex_imported_world(new_id))


def test_reindex_skips_deleted_world(world_with_history, monkeypatch):
    from backend.routers import worlds as worlds_mod
    client, wid = world_with_history
    payload = client.get(f"/api/worlds/{wid}/export/json").json()
    new_id = client.post("/api/worlds/import/json", json={"payload": payload}).json()["world_id"]
    db.delete_world(new_id)
    asyncio.run(worlds_mod._reindex_imported_world(new_id))  # тихо выходит, не падает


def test_backup_database_makes_readable_snapshot(api_client):
    """Снимок БД должен открываться и содержать данные — иначе бэкап декоративный.
    В тестах DB_PATH указывает во временную папку (см. conftest), боевая БД не трогается."""
    client, _holder = api_client
    r = client.post("/api/worlds", json={"theme_id": __import__("backend.narrator", fromlist=["THEMES"]).THEMES[0]["id"],
                                        "name": "Бэкап-мир"})
    assert r.status_code == 200
    worlds_before = len(db.list_worlds())
    assert worlds_before >= 1

    path = db.backup_database(keep=2)
    assert path and path.endswith(".db")
    # ВАЖНО: `with sqlite3.connect(...)` — это транзакция, а НЕ закрытие файла.
    # Оставив соединение открытым, мы вешаем файл на Windows и ротация не может его удалить.
    with contextlib.closing(sqlite3.connect(path)) as con:
        n = con.execute("select count(*) from worlds").fetchone()[0]
        name = con.execute("select name from worlds order by id desc limit 1").fetchone()[0]
    assert n == worlds_before
    assert name == "Бэкап-мир"

    # ротация: держим не больше `keep` снимков
    for _ in range(3):
        db.backup_database(keep=2)
    snaps = list(Path(path).parent.glob("*.db"))
    assert len(snaps) <= 2


def test_backup_database_survives_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "get_config",
                        lambda: type("C", (), {"db_path": str(tmp_path / "нет-такой-базы.db")})())
    assert db.backup_database() is None  # не бросает: бэкап не валит старт сервера
