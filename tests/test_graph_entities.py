# -*- coding: utf-8 -*-
"""Тесты расширенных типов узлов графа (фракции/NPC/магазины), инкрементального
синка и безопасности graph_connect."""
from __future__ import annotations

import json

from backend import db, graph


def _clear(w):
    db.graph_clear_world(w)


def test_sync_adds_faction_npc_shop_nodes():
    _clear(10)  # изоляция: id может быть занят миром из API-тестов (общая тест-БД)
    setting = {
        "current_location": "start",
        "locations": {"start": {"name": "Таверна"}},
        "factions": {"guild": {"name": "Гильдия", "desc": "торговцы"}},
        "npc": {"barman": {"name": "Трактирщик", "alive": True, "faction": "guild"},
                "deadguy": {"name": "Мёртвый", "alive": False, "faction": "guild"}},
        "shops": {"shop_1": {"name": "Лавка", "location": "start", "faction": "guild"}},
    }
    graph.sync_from_setting(10, setting)
    kinds = {n["node_id"]: n["kind"] for n in db.graph_nodes(10)}
    assert kinds.get("start") == "location"
    assert kinds.get("faction:guild") == "faction"
    assert kinds.get("npc:barman") == "npc"
    assert "npc:deadguy" not in kinds          # мёртвый NPC не попадает в граф
    assert kinds.get("shop:shop_1") == "shop"
    # рёбра: NPC→фракция, магазин→локация, магазин→фракция (канонический порядок s<=t)
    edges = {(e["source"], e["target"]) for e in db.graph_edges(10)}
    assert ("faction:guild", "npc:barman") in edges
    assert ("shop:shop_1", "start") in edges
    assert ("faction:guild", "shop:shop_1") in edges
    _clear(10)


def test_sync_incremental_no_write_when_unchanged():
    _clear(11)
    setting = {"locations": {"a": {"name": "A", "connections": ["b"]}, "b": {"name": "B"}}}
    graph.sync_from_setting(11, setting)
    # (node_id, label, kind) — сравним до/после повторного синка
    def snap():
        return ({(n["node_id"], n["label"], n["kind"]) for n in db.graph_nodes(11)}
                | {(e["source"], e["target"]) for e in db.graph_edges(11)})
    s1 = snap()
    # повторный синк с тем же состоянием (deep-copy): граф не перестраивается
    graph.sync_from_setting(11, json.loads(json.dumps(setting)))
    assert snap() == s1
    # изменение connections → граф обновляется (сосед в edges меняется)
    changed = {"locations": {"a": {"name": "A", "connections": ["c"]},
                              "b": {"name": "B"}, "c": {"name": "C"}}}
    graph.sync_from_setting(11, changed)
    edges = {(e["source"], e["target"]) for e in db.graph_edges(11)}
    assert ("a", "c") in edges and ("a", "b") not in edges
    _clear(11)


def test_graph_connect_skips_missing_and_selfloop():
    _clear(12)  # изоляция: id может быть занят миром из API-тестов (общая тест-БД)
    db.graph_set_node(12, "a", "A")
    db.graph_connect(12, "a", "b")        # b отсутствует → связь не создаётся
    db.graph_connect(12, "a", "a")        # само-петля → пропуск
    assert db.graph_edges(12) == []
    db.graph_set_node(12, "b", "B")
    db.graph_connect(12, "a", "b")
    assert len(db.graph_edges(12)) == 1
    _clear(12)


def test_sync_rebuilds_when_node_data_changes():
    # изменение только мета-данных (data) узла фракции → граф обязан пересинхронизироваться
    s1 = {"locations": {"a": {"name": "A"}},
          "factions": {"guild": {"name": "Гильдия", "desc": "торговцы"}}}
    graph.sync_from_setting(14, s1)
    # 1) desc без изменений → граф не трогаем
    n1 = {(n["node_id"], n["data"]) for n in db.graph_nodes(14)}
    graph.sync_from_setting(14, json.loads(json.dumps(s1)))
    assert {(n["node_id"], n["data"]) for n in db.graph_nodes(14)} == n1
    # 2) поменяли только desc фракции → data узла обновляется в БД
    s2 = json.loads(json.dumps(s1))
    s2["factions"]["guild"]["desc"] = "суровые торговцы"
    graph.sync_from_setting(14, s2)
    data = {n["node_id"]: n["data"] for n in db.graph_nodes(14)}
    assert "суровые торговцы" in data["faction:guild"]
    _clear(14)


def test_payload_includes_non_location_kinds():
    setting = {
        "current_location": "start",
        "locations": {"start": {"name": "S", "connections": ["forest"]}, "forest": {"name": "F"}},
        "factions": {"guild": {"name": "Гильдия"}},
    }
    graph.sync_from_setting(13, setting)
    out = graph.payload(13, setting)
    kinds = {n["id"]: n.get("kind") for n in out["nodes"]}
    assert kinds.get("start") == "location"
    assert kinds.get("faction:guild") == "faction"
    assert out["layers"]["start"] == 0
    _clear(13)