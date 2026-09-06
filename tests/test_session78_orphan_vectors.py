# -*- coding: utf-8 -*-
"""Аудит 41, [A1 §5] (ROADMAP 🟡) — сиротские векторы в Chroma больше не живут вечно.

Было: удаление мира начинается с чистки Chroma и при её отказе даёт 503 (с36), сирот в
SQLite вычищает `db.prune_orphan_rows` (аудит 38, A8) — но ОБРАТНОЙ сверки «вектор ↔ живой
мир» в проекте не было НИГДЕ. Векторы миров, стёртых прямым SQL или осиротевших после отката
на бэкап, оставались навсегда: RAG доставал сюжет удалённого мира, а счётчик чанков врал.
Замер на боевой базе перед правкой: 1103 вектора, 35 живых миров, 55 разных `world_id` —
23 удалённых мира держали 160 осиротевших векторов.

Стало: `chroma_client.get_ids_by_world()` (постраничный обход коллекции, владелец вектора —
`metadata.world_id`, запасной путь — префикс id) + `memory.sweep_orphan_vectors()` (идёт по
образцу `prune_orphan_rows`: идемпотентно, молчит на чистых данных, при недоступной Chroma —
пропуск с warning-ом) + проход при старте (`app._startup_heal`, A13: фоном, не на импорте) +
счётчик в админке (`effective.vector_memory`, без нового роута и без обхода на каждый GET).

Законы 2/3 целы: сводка трогает ТОЛЬКО векторы памяти, ничего не решает за мастера и не
пишет в SQLite (тест `test_sweep_never_writes_to_sqlite`). Вектор, чей владелец не опознан,
НЕ удаляется: лучше осиротевший вектор, чем стёртый живой.

Инвариант 19: проверяется ПОВЕДАНИЕ (реальный SQLite, реальный grouping, реальные вызовы
клиента — подменён только сетевой слой Chroma), а не тексты SQL/исходников.
"""
from __future__ import annotations

import asyncio

import pytest

from backend import app as app_mod
from backend import chroma_client as cc
from backend import db as db_mod
from backend import memory as mem_mod
from backend import config as config_mod

WANTED = "чистка сиротских векторов"


# ─────────────────────────── фейковый сетевой слой Chroma ───────────────────────────
class FakeChroma:
    """Подменяет `chroma_client` ровно на тех методах, которых касается сводка.

    `store` — {имя коллекции: {world_id: [id, …]}}; всё остальное (пагинация, группировка,
    сравнение с БД, чанки удаления) исполняется НАСТОЯЩИМ кодом.
    """

    def __init__(self, store: dict[str, dict[int, list[str]]], *, up: bool = True) -> None:
        self.store = store
        self.up = up
        self.deleted: list[list[str]] = []
        self.fail_get: set[str] = set()
        self.fail_delete = False

    async def ping(self) -> bool:
        return self.up

    def collection_names_all(self) -> list[str]:
        return list(self.store)

    async def get_ids_by_world(self, name: str, page: int = 1000, max_pages: int = 2000):
        if name in self.fail_get:
            raise RuntimeError("коллекция не отвечает")
        return {int(k): list(v) for k, v in self.store.get(name, {}).items()}

    async def delete_by_ids(self, ids: list[str]) -> None:
        if self.fail_delete:
            raise RuntimeError("Chroma HTTP 500")
        self.deleted.append(list(ids))
        for vecs in self.store.values():
            for wid, lst in list(vecs.items()):
                left = [x for x in lst if x not in set(ids)]
                if left:
                    vecs[wid] = left
                else:
                    vecs.pop(wid, None)


@pytest.fixture
def fake(monkeypatch):
    def _make(store, **kw):
        f = FakeChroma(store, **kw)
        monkeypatch.setattr(mem_mod.chroma_client, "ping", f.ping)
        monkeypatch.setattr(mem_mod.chroma_client, "collection_names_all", f.collection_names_all)
        monkeypatch.setattr(mem_mod.chroma_client, "get_ids_by_world", f.get_ids_by_world)
        monkeypatch.setattr(mem_mod.chroma_client, "delete_by_ids", f.delete_by_ids)
        return f
    return _make


@pytest.fixture
def live_world(api_client):
    client, _ = api_client
    return client.post("/api/worlds", json={"theme_id": "custom", "name": "сводка",
                                            "custom_plot": "сюжет", "genres": []}).json()["world_id"]


def _all_flat(store: dict) -> set[str]:
    return {i for vecs in store.values() for ids in vecs.values() for i in ids}


# ─────────────────────────────── 1. владелец вектора ───────────────────────────────
def test_world_id_prefers_metadata_and_falls_back_to_id():
    """Мир вектора читается из metadata; если её нет — из префикса id; иначе — None.

    Это граница безопасности сводки: `None` означает «не трогаем» (лучше осиротевший
    вектор, чем удалённый живой).
    """
    assert cc._world_id_of("ex_19_5", {"world_id": 19}) == 19
    assert cc._world_id_of("ent_21_npc_herbalist", {"kind": "entity"}) == 21   # префикс
    assert cc._world_id_of("ex_7_3", {"world_id": "9"}) == 9                   # metadata главнее
    assert cc._world_id_of("lore_4_2_0", {"world_id": None}) == 4
    assert cc._world_id_of("sum_3_1", {}) == 3
    assert cc._world_id_of("не-ид", {"world_id": "мусор"}) is None
    assert cc._world_id_of("", None) is None


def test_get_ids_by_world_pages_and_groups(monkeypatch):
    """Постраничный `/get` читается целиком и группируется по миру.

    Полная страница (ровно `page` id) обязана продолжиться следующей — иначе сводка
    молча пропустила бы хвост большой коллекции; короткая страница завершает обход.
    """
    pages = [
        ({"ids": [f"ex_1_{i}" for i in range(3)],
          "metadatas": [{"world_id": 1}] * 3}),
        ({"ids": ["ent_2_npc_a", "lore_2_1_0"],
          "metadatas": [{"world_id": 2}, {"world_id": 2}]}),
        ({"ids": ["NEBOSI_IZNET"], "metadatas": [{"world_id": 99}]},),
    ]

    async def _fake_raw(method, api_path, payload=None):
        return pages.pop(0)

    monkeypatch.setattr(cc, "_raw", _fake_raw)
    async def _fake_ensure(name=None):
        return "cid"
    monkeypatch.setattr(cc, "ensure_collection", _fake_ensure)

    out = asyncio.run(cc.get_ids_by_world("any", page=3))
    assert out == {1: ["ex_1_0", "ex_1_1", "ex_1_2"], 2: ["ent_2_npc_a", "lore_2_1_0"]}
    assert len(pages) == 1, "обход ушёл за конец выдачи (полная страница после короткой)"


def test_ownerless_vectors_are_not_grounded(monkeypatch):
    """Записи без опознаваемого мира не попадают в группировку → сводка их не видит."""
    async def _fake_raw(method, api_path, payload=None):
        return {"ids": ["something_else", "x_abc"], "metadatas": [{"kind": "?"}, None]}

    async def _fake_ensure(name=None):
        return "cid"
    monkeypatch.setattr(cc, "_raw", _fake_raw)
    monkeypatch.setattr(cc, "ensure_collection", _fake_ensure)
    assert asyncio.run(cc.get_ids_by_world("any")) == {}


# ───────────────────────────────── 2. сводка ─────────────────────────────────
def test_sweep_deletes_only_dead_worlds(fake, live_world):
    """Живой мир остаётся целиком, мёртвый — исчезает полностью, счётчики сходятся."""
    dead = 999999
    store = {"text_game_memory": {
        live_world: ["ex_1", "ent_1_a"],
        dead: ["ex_2", "ent_2_b", "sum_2_1"],
    }}
    f = fake(store)
    out = asyncio.run(mem_mod.sweep_orphan_vectors())

    assert out["scanned"] == 5
    assert out["orphan_worlds"] == 1 and out["orphan_vectors"] == 3
    assert out["deleted"] == 3 and not out["dry_run"]
    assert _all_flat(store) == {"ex_1", "ent_1_a"}, "удалён вектор живого мира"
    assert sorted(sum(f.deleted, [])) == sorted(["ex_2", "ent_2_b", "sum_2_1"])
    assert out["collections"] == ["text_game_memory"]


def test_sweep_dry_run_counts_without_deleting(fake):
    """`delete=False` — только отчёт: ни одного вызова удаления (ручной «посмотреть»)."""
    store = {"c": {12345: ["a", "b"]}}
    f = fake(store)
    out = asyncio.run(mem_mod.sweep_orphan_vectors(delete=False))
    assert out["dry_run"] is True and out["orphan_vectors"] == 2 and out["deleted"] == 0
    assert f.deleted == [] and _all_flat(store) == {"a", "b"}


def test_sweep_is_idempotent_on_clean_store(fake, live_world):
    """Повторный проход по чистым данным ничего не делает и не пишет в журнал (правило 14 —
    warning только когда есть что чистить или что-то сломалось)."""
    store = {"c": {live_world: ["a"]}}
    f = fake(store)
    first = asyncio.run(mem_mod.sweep_orphan_vectors())
    assert first["orphan_worlds"] == 0 and first["scanned"] == 1 and f.deleted == []
    second = asyncio.run(mem_mod.sweep_orphan_vectors())
    assert second["orphan_vectors"] == 0 and f.deleted == []


def test_sweep_chunks_big_deletes(fake, monkeypatch):
    """Миллион осиротевших id не уходит одним запросом: чанки ≤ `VECTOR_DELETE_CHUNK`."""
    monkeypatch.setattr(mem_mod, "VECTOR_DELETE_CHUNK", 3)
    ids = [f"ex_777_{i}" for i in range(10)]
    store = {"c": {777: list(ids)}}
    f = fake(store)
    out = asyncio.run(mem_mod.sweep_orphan_vectors())
    assert out["deleted"] == 10
    assert [len(c) for c in f.deleted] == [3, 3, 3, 1]


def test_sweep_skips_silently_when_chroma_is_down(monkeypatch, caplog):
    """Chroma лежит → пропуск с warning-ом, НЕ исключение (игра стартует без чистки)."""
    async def _down():
        return False
    monkeypatch.setattr(mem_mod.chroma_client, "ping", _down)
    with caplog.at_level("WARNING"):
        out = asyncio.run(mem_mod.sweep_orphan_vectors())
    assert out["skipped"] == "chroma_unavailable" and out["deleted"] == 0
    assert any(WANTED in r.message or WANTED in r.getMessage() for r in caplog.records)


def test_sweep_survives_broken_collection_and_failed_delete(fake, live_world, caplog):
    """Правило 14: сбой одной коллекции/удаления виден в журнале, остальное дочистляется."""
    store = {"broken": {777: ["x"]}, "ok": {live_world: ["keep"], 888: ["drop"]}}
    f = fake(store)
    f.fail_get = {"broken"}
    with caplog.at_level("WARNING"):
        out = asyncio.run(mem_mod.sweep_orphan_vectors())
    assert "broken: коллекция не отвечает" in out["error"]
    assert _all_flat(store) == {"keep", "x"}, "сбойная коллекция задела живую"

    # теперь удаление падает: сбой виден, счётчик найденного остаётся честным
    store["ok"] = {live_world: ["keep"], 999: ["also-drop"]}
    f.fail_delete = True
    with caplog.at_level("WARNING"):
        out2 = asyncio.run(mem_mod.sweep_orphan_vectors())
    assert out2["deleted"] == 0 and out2["orphan_vectors"] == 1
    assert any(WANTED in r.getMessage() for r in caplog.records)


def test_sweep_never_writes_to_sqlite(fake, live_world, monkeypatch):
    """Законы 2/3: сводка — обслуживание памяти. Ни одной записи в БД за проход."""
    def _boom(*a, **kw):
        raise AssertionError("сиротские векторы чинятся НЕ правками БД")

    for name in ("update_world", "delete_world", "add_event", "prune_orphan_rows"):
        monkeypatch.setattr(mem_mod.db, name, _boom)
    store = {"c": {live_world: ["keep"], 555: ["drop"]}}
    fake(store)
    assert asyncio.run(mem_mod.sweep_orphan_vectors())["deleted"] == 1


# ─────────────────────────── 3. точка сверки и запуск ───────────────────────────
def test_world_ids_matches_world_list(live_world):
    """`db.world_ids()` — то же множество, что у `list_worlds` (и это ЧТЕНИЕ, без записи)."""
    ids = db_mod.world_ids()
    assert isinstance(ids, set) and live_world in ids
    assert ids == {w["id"] for w in db_mod.list_worlds()}


def test_startup_heal_runs_the_sweep_after_self_heal(monkeypatch, live_world):
    """Проход обязан стартовать из `_startup_heal` (A13: фоном, а не на импорте)."""
    calls: list[bool] = []

    async def _spy(delete: bool = True):
        calls.append(delete)
        return {"orphan_vectors": 0}

    monkeypatch.setattr(mem_mod, "sweep_orphan_vectors", _spy)
    monkeypatch.setattr(app_mod, "_flush_pending_vectors",
                        lambda tag="": asyncio.sleep(0))
    asyncio.run(app_mod._startup_heal())
    assert calls == [True], "стартовый само-исцеляющий проход не зовёт сводку векторов"


def test_sweep_result_is_visible_in_admin(monkeypatch, fake, api_client, live_world):
    """Счётчик в админке (`effective.vector_memory`) — из последнего прохода, без обхода."""
    store = {"c": {live_world: ["keep"], 4242: ["drop1", "drop2"]}}
    fake(store)
    asyncio.run(mem_mod.sweep_orphan_vectors())

    client, _ = api_client
    body = client.get("/api/admin/settings").json()
    vm = body["effective"]["vector_memory"]
    assert vm["orphan_worlds"] == 1 and vm["orphan_vectors"] == 2 and vm["deleted"] == 2
    assert vm["collections"] == ["c"]


def test_registry_of_owners_covers_every_indexed_kind():
    """Каждый вид записываемых векторов имеет читаемый `world_id` — иначе сводка слепа.

    Формы id живут в `memory.index_*` / `lore_retriever`: `ex_<w>_<seq>`, `sum_<w>_<seq>`,
    `ent_<w>_<kind>_<key>`, `lore_<w>_<id>_<i>`. Префикс-фолбэк обязан разбирать все четыре.
    """
    for vid in ("ex_5_1", "sum_5_2", "ent_5_npc_bill", "lore_5_3_0"):
        assert cc._world_id_of(vid, {}) == 5, vid
    cfg = config_mod.get_config()
    base = cfg.chroma_collection or "text_game_memory"
    assert cc.collection_names_all() == [base, f"{base}_local"]
