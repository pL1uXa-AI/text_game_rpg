# -*- coding: utf-8 -*-
"""🌐 Тесты графовой БД: алгоритмы (BFS/достижимость/путь), слой db (graph_nodes/edges),
синхронизация из setting и JSON-выжимка для фронтенда."""
from __future__ import annotations

from backend import db, graph


# ────────────────────────── чистые алгоритмы ──────────────────────────

def _edges():
    # граф: a-b, b-c, b-d, d-e (дороги), плюс отдельная компонента x-y (не связана с a)
    return [
        {"source": "a", "target": "b"},
        {"source": "b", "target": "c"},
        {"source": "b", "target": "d"},
        {"source": "d", "target": "e"},
        {"source": "x", "target": "y"},
    ]


def test_adjacency_undirected():
    adj = graph.adjacency(_edges())
    assert adj["a"] == {"b"}
    assert adj["b"] == {"a", "c", "d"}
    assert "y" in adj["x"] and "x" in adj["y"]


def test_bfs_layers():
    layers = graph.bfs_layers(_edges(), "a")
    assert layers == {"a": 0, "b": 1, "c": 2, "d": 2, "e": 3}
    # недосягаемая компонента не попадает
    assert "x" not in layers and "y" not in layers


def test_reachable():
    r = graph.reachable(_edges(), "a")
    assert r == {"a", "b", "c", "d", "e"}
    assert "x" not in r


def test_shortest_path():
    path = graph.shortest_path(_edges(), "a", "e")
    assert path == ["a", "b", "d", "e"]
    # недостижимая цель → None
    assert graph.shortest_path(_edges(), "a", "x") is None
    # старт == цель → одноэлементный (если узел есть)
    assert graph.shortest_path(_edges(), "a", "a") == ["a"]
    # цель, которой нет в графе → None
    assert graph.shortest_path(_edges(), "a", "z") is None


def test_nearest_by_kind():
    nodes = [
        {"node_id": "a", "kind": "location"},
        {"node_id": "d", "kind": "location"},
        {"node_id": "e", "kind": "faction"},
        {"node_id": "c", "kind": "location"},
    ]
    layers = graph.bfs_layers(_edges(), "a")
    near = graph.nearest_by_kind(nodes, layers, "location", start="a")
    # отсортировано по (слой, лексический id): d(2) затем c(2), оба ближе чем e(3)
    assert near == ["c", "d"]
    assert graph.nearest_by_kind(nodes, layers, "npc", start="a") == []


# ────────────────────────── слой БД ──────────────────────────

def test_graph_replace_nodes_and_edges():
    db.graph_replace(1, [
        {"node_id": "start", "label": "Таверна", "kind": "location", "data": {"desc": "raw"}},
        {"node_id": "forest", "label": "Лес", "kind": "location"},
    ], [("start", "forest", 1.0, {"type": "дорога"})])
    nodes = db.graph_nodes(1)
    assert {n["node_id"] for n in nodes} == {"start", "forest"}
    by_id = {n["node_id"]: n for n in nodes}
    assert by_id["start"]["label"] == "Таверна"

    edges = db.graph_edges(1)
    assert len(edges) == 1
    assert edges[0]["source"] == "forest" and edges[0]["target"] == "start"  # канонический порядок
    assert edges[0]["weight"] == 1.0
    db.graph_clear_world(1)


def test_graph_replace_skips_edges_to_missing_nodes_and_selfloop():
    db.graph_replace(2, [{"node_id": "a", "label": "A"}], [("a", "missing", 1, None), ("a", "a")])
    edges = db.graph_edges(2)
    assert edges == []
    db.graph_clear_world(2)


def test_graph_get_set_node_and_connect():
    db.graph_set_node(3, "town", "Город", kind="location", data={"pop": "1000"})
    n = db.graph_get_node(3, "town")
    assert n is not None and n["node_id"] == "town"

    db.graph_set_node(3, "harbor", "Гавань")
    db.graph_connect(3, "town", "harbor")
    edges = db.graph_edges(3)
    assert {tuple(sorted((e["source"], e["target"]))) for e in edges} == {("harbor", "town")}

    # направленность не теряется: связь та же при обратном порядке
    db.graph_connect(3, "harbor", "town")
    assert len(db.graph_edges(3)) == 1

    db.graph_clear_world(3)


def test_graph_worlds_isolated():
    db.graph_replace(1, [{"node_id": "a"}], [])
    db.graph_replace(2, [{"node_id": "b"}], [])
    assert {n["node_id"] for n in db.graph_nodes(1)} == {"a"}
    assert {n["node_id"] for n in db.graph_nodes(2)} == {"b"}
    db.graph_clear_world(1)
    db.graph_clear_world(2)


# ────────────────────────── синхронизация с миром ──────────────────────────

def test_sync_from_setting_and_payload():
    setting = {
        "current_location": "start",
        "locations": {
            "start": {"name": "Таверна", "connections": ["forest", "cave"]},
            "forest": {"name": "Лес", "connections": ["start"]},
            "cave": {"name": "Пещера", "connections": ["start"]},
            "ruin": {"name": "Руины", "connections": ["forest"]},
        },
    }
    graph.sync_from_setting(5, setting)
    node_ids = {n["node_id"] for n in db.graph_nodes(5)}
    assert node_ids == {"start", "forest", "cave", "ruin"}
    # рёбра: start-forest, start-cave, forest-ruin (все двунаправленно в связях)
    edge_keys = {tuple(sorted((e["source"], e["target"]))) for e in db.graph_edges(5)}
    assert edge_keys == {("cave", "start"), ("forest", "start"), ("forest", "ruin")}
    # Узлы-метки
    labels = {n["node_id"]: n["label"] for n in db.graph_nodes(5)}
    assert labels["start"] == "Таверна"

    pl = graph.payload(5, setting)
    assert pl["current"] == "start"
    assert pl["layers"]["start"] == 0 and pl["layers"]["ruin"] == 2
    assert pl["path"] is None

    pl2 = graph.payload(5, setting, target="ruin")
    assert pl2["path"] == ["start", "forest", "ruin"]

    db.graph_clear_world(5)


def test_payload_current_flag_and_edges():
    setting = {"current_location": "b",
               "locations": {"a": {"name": "A", "connections": ["b"]}, "b": {"name": "B"}}}
    graph.sync_from_setting(6, setting)
    payload = graph.payload(6, setting)
    cur_nodes = [n["id"] for n in payload["nodes"] if n["current"]]
    assert cur_nodes == ["b"]
    assert {"source": "a", "target": "b"} in payload["edges"]
    db.graph_clear_world(6)


def test_sync_removes_stale_edges():
    # старая связь start-cave убрана из локалей → после синка ребра не должно быть
    db.graph_replace(9, [
        {"node_id": "start"}, {"node_id": "cave"}, {"node_id": "forest"},
    ], [("start", "cave"), ("start", "forest")])
    setting = {"locations": {"start": {"name": "S", "connections": ["forest"]},
                             "forest": {"name": "F"}}}
    graph.sync_from_setting(9, setting)
    edge_keys = {(e["source"], e["target"]) for e in db.graph_edges(9)}
    assert ("cave", "start") not in edge_keys and ("forest", "start") in edge_keys
    db.graph_clear_world(9)