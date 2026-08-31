# -*- coding: utf-8 -*-
"""Графовая база данных — карта мира и связи сущностей.

Персистентный граф хранится в SQLite (таблицы `graph_nodes`, `graph_edges`, см. db.py).
Этот модуль — «фаcаd»: чистые алгоритмы (BFS/достижимость/кратчайший путь) над словарём
смежности + высокоуровневые операции, которые читают/пишут граф через `db` и
синхронизируют его с текущим состоянием мира (setting).

Типы узлов (kind), хранимые в графе:
  - `location` — локации карты (связь: `connections` между локациями);
  - `shop`     — магазины (связь: локация `shops[].location`, фракция `shops[].faction`);
  - `faction`  — фракции (узел-хаб, связь с членами);
  - `npc`      — живые NPC (связь: принадлежность фракции `npc[].faction`);
  - `quest`    — зарезервировано (пока без геопривязки в этой итерации).

Разделение слоёв (AGENT.md): `db.py` — только доступ к данным; здесь — бизнес-логика
графа (setting → граф, поиск путей, слои BFS, JSON для фронтенда).
"""
from __future__ import annotations

import json
from typing import Any, Optional

from . import db

LOCATION_KIND = "location"
FACTION_KIND = "faction"
NPC_KIND = "npc"
SHOP_KIND = "shop"
QUEST_KIND = "quest"


def entity_node_id(kind: str, key: Any) -> str:
    """Стабильный id узла-сущности: `kind:key` (пустой при пустом key)."""
    k = str(key or "").strip()
    return f"{kind}:{k}" if k else ""


def _canon(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


# ────────────────────────── чистые алгоритмы ──────────────────────────

def adjacency(edges: list[dict]) -> dict[str, set[str]]:
    """Неориентированный словарь смежности из рёбер `[{source, target}, ...]`."""
    adj: dict[str, set[str]] = {}
    for e in edges:
        a, b = str(e.get("source", "")), str(e.get("target", ""))
        if not a or not b:
            continue
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    return adj


def bfs_layers(edges: list[dict], start: str) -> dict[str, int]:
    """Слои BFS от `start`: {node_id: глубина}. Только достижимые; старт = 0."""
    adj = adjacency(edges)
    if not start:
        return {}
    layers: dict[str, int] = {start: 0}
    frontier: list[str] = [start]
    depth = 0
    while frontier:
        depth += 1
        nxt: list[str] = []
        for node in frontier:
            for nb in adj.get(node, ()):
                if nb not in layers:
                    layers[nb] = depth
                    nxt.append(nb)
        frontier = nxt
    return layers


def reachable(edges: list[dict], start: str) -> set[str]:
    """Все узлы, достижимые из `start` (включая сам старт)."""
    return set(bfs_layers(edges, start).keys())


def shortest_path(edges: list[dict], start: str, goal: str) -> Optional[list[str]]:
    """Кратчайший путь (по числу рёбер, BFS) от `start` к `goal` включительно, либо None."""
    nodes: set[str] = set()
    for e in edges:
        nodes.add(str(e.get("source", "")))
        nodes.add(str(e.get("target", "")))
    if start == goal:
        return [start] if start in nodes else None
    adj = adjacency(edges)
    if start not in adj:
        return None
    parent: dict[str, Optional[str]] = {start: None}
    frontier: list[str] = [start]
    while frontier:
        nxt: list[str] = []
        for node in frontier:
            for nb in adj.get(node, ()):
                if nb not in parent:
                    parent[nb] = node
                    if nb == goal:
                        path = [goal]
                        cur: str = goal
                        while parent[cur] is not None:
                            cur = parent[cur]  # type: ignore[assignment]
                            path.append(cur)
                        path.reverse()
                        return path
                    nxt.append(nb)
        frontier = nxt
    return None


def nearest_by_kind(nodes: list[dict], layers: dict[str, int], kind: str,
                    start: str, limit: int = 5) -> list[str]:
    """До `limit` ближайших (по слою BFS `layers`) узлов `kind`, кроме `start`."""
    res = []
    for n in nodes:
        nid = n.get("node_id", "")
        if n.get("kind") != kind or nid == start or nid not in layers:
            continue
        res.append((layers[nid], nid))
    res.sort()
    return [nid for _, nid in res[:limit]]


# ────────────────────────── синхронизация с миром ──────────────────────────

def build_graph(setting: dict) -> tuple[list[dict], list[tuple]]:
    """Желаемый граф из `setting`: (nodes, edges). Инкрементальный diff делает вызывающий.

    - локации + связи `connections`;
    - фракции (узлы-хабы `faction:<id>`) + связь с членами;
    - живые NPC (`npc:<id>`) -> фракция;
    - магазины (`shop:<id>`) -> локация + фракция.
    """
    locations = (setting or {}).get("locations") or {}
    npcs = (setting or {}).get("npc") or {}
    factions = (setting or {}).get("factions") or {}
    shops = (setting or {}).get("shops") or {}

    nodes: list[dict] = []
    edges: list[tuple] = []
    loc_ids: set[str] = set()

    def add_node(nid: str, label: str, kind: str, data: dict) -> None:
        nodes.append({"node_id": nid, "label": label, "kind": kind, "data": data or {}})

    # 1) локации + карта локация<->локация
    for lid, loc in locations.items():
        lid_str = str(lid).strip()
        if not lid_str:
            continue
        ln = loc if isinstance(loc, dict) else {}
        add_node(lid_str, ln.get("name", lid_str), LOCATION_KIND, {"desc": ln.get("desc", "")})
        loc_ids.add(lid_str)
    for lid, loc in locations.items():
        lid_str = str(lid).strip()
        ln = loc if isinstance(loc, dict) else {}
        for c in (ln.get("connections") or []):
            cid = c if isinstance(c, str) else ((c or {}).get("id") if isinstance(c, dict) else str(c))
            cid = str(cid or "").strip()
            if cid and cid != lid_str and cid in loc_ids:
                edges.append((lid_str, cid, 1.0, {}))

    # 2) фракции: узлы-хабы
    faction_key: dict[str, str] = {}
    for fid, f in factions.items():
        if not isinstance(f, dict):
            continue
        key = entity_node_id("faction", fid)
        if not key:
            continue
        add_node(key, f.get("name", str(fid)), FACTION_KIND,
                 {"desc": f.get("desc", ""), "alignment": f.get("alignment", "")})
        faction_key[str(fid).strip()] = key

    # 3) живые NPC
    for nid, n in npcs.items():
        if not isinstance(n, dict) or n.get("alive") is False:
            continue
        key = entity_node_id(NPC_KIND, nid)
        if not key:
            continue
        add_node(key, n.get("name", str(nid)), NPC_KIND,
                 {"desc": n.get("desc", ""), "faction": n.get("faction", "")})
        fac = str(n.get("faction", "")).strip()
        if fac in faction_key:
            edges.append((key, faction_key[fac], 1.0, {}))

    # 4) магазины
    for sid, sh in shops.items():
        if not isinstance(sh, dict):
            continue
        key = entity_node_id(SHOP_KIND, sid)
        if not key:
            continue
        add_node(key, sh.get("name", str(sid)), SHOP_KIND,
                 {"desc": sh.get("desc", ""), "owner": sh.get("owner", ""),
                  "faction": sh.get("faction", "")})
        sloc = str(sh.get("location", "")).strip()
        if sloc and sloc in loc_ids:
            edges.append((key, sloc, 1.0, {}))
        sfac = str(sh.get("faction", "")).strip()
        if sfac in faction_key:
            edges.append((key, faction_key[sfac], 1.0, {}))

    return nodes, edges


def sync_from_setting(world_id: int, setting: dict) -> None:
    """Синхронизировать граф мира с `setting` (инкрементально).

    Строит желаемый граф; если он не изменился с прошлого хода — БД не затрагивается
    (нет лишних DELETE/INSERT каждый ход). При изменении — атомарный `db.graph_replace`.
    """
    nodes, edges = build_graph(setting)
    if _equal_snapshot(world_id, nodes, edges):
        return
    db.graph_replace(world_id, nodes, edges)


def _graph_snapshot(world_id: int) -> tuple[set, set]:
    nodes = {(n["node_id"], n.get("label", ""), n.get("kind", "custom"),
              _stablize(json_loads(n.get("data")))) for n in db.graph_nodes(world_id)}
    edges = {(e["source"], e["target"]) for e in db.graph_edges(world_id)}
    return nodes, edges


def _stablize(d: dict) -> tuple:
    """Дикты в JSON богатые порядком ключей — представить в виде кортежа пар (value тоже),
    чтобы сравнивать независимо от порядка ключей и вложенности."""
    if isinstance(d, dict):
        return tuple(sorted((k, _stablize(v)) for k, v in d.items()))
    if isinstance(d, (list, tuple)):
        return tuple(_stablize(v) for v in d)
    return d


def _equal_snapshot(world_id: int, nodes: list[dict], edges: list[tuple]) -> bool:
    have_nodes, have_edges = _graph_snapshot(world_id)
    want_nodes = {(str(n.get("node_id", "")).strip(), n.get("label", ""), n.get("kind", "custom"),
                   _stablize(n.get("data") or {})) for n in nodes}
    want_edges = set()
    for a, b, _w, _d in edges:
        s, t = _canon(str(a), str(b))
        if s == t:
            continue
        want_edges.add((s, t))
    return have_nodes == want_nodes and have_edges == want_edges


# ────────────────────────── JSON для фронтенда ──────────────────────────

def payload(world_id: int, setting: dict, target: Optional[str] = None) -> dict:
    """JSON-выжимка графа для API: `nodes` (с флагом current), `edges`, `layers`
    (BFS от current), `path` (кратчайший путь к `target`, если достижим)."""
    nodes = db.graph_nodes(world_id)
    edges = db.graph_edges(world_id)
    current = str((setting or {}).get("current_location", "start"))
    node_ids = {n["node_id"] for n in nodes}
    edges_out = [{"source": e["source"], "target": e["target"]} for e in edges
                 if e["source"] in node_ids and e["target"] in node_ids]
    layers = bfs_layers(edges_out, current)
    path = None
    if target and target in node_ids:
        if target == current:
            path = [current]
        else:
            path = shortest_path(edges_out, current, target)
    out_nodes = []
    for n in nodes:
        data = json_loads(n.get("data"))
        out_nodes.append({
            "id": n["node_id"],
            "label": n.get("label") or n["node_id"],
            "kind": n.get("kind", "custom"),
            "current": n["node_id"] == current,
            **({"data": data} if data else {}),
        })
    return {
        "nodes": out_nodes,
        "edges": edges_out,
        "current": current,
        "layers": layers,
        "path": path,
    }


def json_loads(s: Any) -> Any:
    """Смягчённый json.loads — {} при пустом / невалидном значении."""
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception:
        return {}