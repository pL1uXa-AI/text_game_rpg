# -*- coding: utf-8 -*-
"""Роутер карточек сущностей: чтение/создание/правка/удаление (режим мастера + знания)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException

from .. import chroma_client, db, narrator
from ..schemas import EntityIn

router = APIRouter(tags=["Карточки сущностей"])


@router.get("/api/worlds/{world_id}/entities")
async def entities(world_id: int, kind: Optional[str] = None):
    world = db.get_world(world_id)
    if not world:
        raise HTTPException(404, "Мир не найден")
    return db.list_entities(world_id, kind)


@router.get("/api/worlds/{world_id}/entities/{kind}/{entity_key}")
async def entity_detail(world_id: int, kind: str, entity_key: str):
    e = db.get_entity(world_id, kind, entity_key)
    if not e:
        raise HTTPException(404, "Карточка не найдена")
    return e


@router.post("/api/worlds/{world_id}/entities")
async def entity_create(world_id: int, body: EntityIn):
    if body.kind not in ("npc", "location", "faction", "quest", "item", "event",
                          "enemy", "shop", "companion", "craft",
                          "race", "class", "profession", "skill", "effect"):
        raise HTTPException(400, f"Неизвестный вид сущности: {body.kind}")
    ent = db.upsert_entity(world_id, body.kind, body.key, name=body.name, summary=body.summary,
                           relationship=body.relationship, bio_add=body.bio_add, meta=body.meta,
                           seq=db.latest_seq(world_id))
    await narrator.index_entities(world_id, [ent])
    return {"ok": True, "entity": ent}


@router.patch("/api/worlds/{world_id}/entities/{kind}/{entity_key}")
async def entity_patch(world_id: int, kind: str, entity_key: str, body: EntityIn):
    existing = db.get_entity(world_id, kind, entity_key)
    if not existing:
        raise HTTPException(404, "Карточка не найдена")
    ent = db.upsert_entity(world_id, kind, entity_key, name=body.name, summary=body.summary,
                           relationship=body.relationship, bio_add=body.bio_add, meta=body.meta,
                           seq=db.latest_seq(world_id))
    await narrator.index_entities(world_id, [ent])
    return {"ok": True, "entity": ent}


@router.delete("/api/worlds/{world_id}/entities/{kind}/{entity_key}")
async def entity_delete(world_id: int, kind: str, entity_key: str):
    db.delete_entity(world_id, kind, entity_key)
    try:
        await chroma_client.delete_by_ids([f"ent_{world_id}_{kind}_{entity_key}".replace(' ', '_')])
    except Exception:
        pass
    return {"ok": True}