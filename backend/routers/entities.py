# -*- coding: utf-8 -*-
"""Роутер карточек сущностей: чтение/создание/правка/удаление (режим мастера + знания)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from .. import chroma_client, db, narrator
from ..logsetup import get_logger
from ..ratelimit import guard_for
from ..schemas import EntityIn
from .core import _world_or_404

log = get_logger(__name__)

router = APIRouter(tags=["Карточки сущностей"])

# A2 (аудит 41): ЕДИНЫЙ список видов. Раньше он был инлайном только в POST, и PATCH
# принимал ЛЮБОЙ `kind` — карточка с kind=`<img src=x onerror=…>` возвращалась во фронте
# в data-kind (а оттуда — в URL запроса). Синхронизирован с memory.ENTITY_KINDS.
ENTITY_KINDS = ("npc", "location", "faction", "quest", "item", "event",
                "enemy", "shop", "companion", "craft",
                "race", "class", "profession", "skill", "effect")


def _check_kind(world_id: int, kind: str) -> None:
    """Неизвестный/грязный вид карточки — 400 (а не тихое создание строки с мусором)."""
    if kind not in ENTITY_KINDS:
        log.warning("entities (world %s): отклонён неизвестный вид карточки %r", world_id, kind[:60])
        raise HTTPException(400, f"Неизвестный вид сущности: {kind}")


@router.get("/api/worlds/{world_id}/entities")
async def entities(world_id: int, kind: Optional[str] = None):
    world = db.get_world(world_id)
    if not world:
        raise HTTPException(404, "Мир не найден")
    return db.list_entities(world_id, kind)


@router.get("/api/worlds/{world_id}/entities/{kind}/{entity_key}")
async def entity_detail(world_id: int, kind: str, entity_key: str):
    # A4 (аудит 38): мир обязан существовать — иначе 404 «карточки нет» на удалённом мире
    # неотличим от «мира нет».
    _world_or_404(world_id)
    e = db.get_entity(world_id, kind, entity_key)
    if not e:
        raise HTTPException(404, "Карточка не найдена")
    return e


@router.post("/api/worlds/{world_id}/entities")
async def entity_create(world_id: int, body: EntityIn,
                        _rl: None = Depends(guard_for("entity_write"))):
    # A4 (аудит 38): карточку нельзя завести для несуществующего мира (осиротевшая строка,
    # см. A8) — проверка мира единая для всех методов этого роутера.
    _world_or_404(world_id)
    _check_kind(world_id, body.kind)
    ent = db.upsert_entity(world_id, body.kind, body.key, name=body.name, summary=body.summary,
                           relationship=body.relationship, bio_add=body.bio_add, meta=body.meta,
                           seq=db.latest_seq(world_id))
    await narrator.index_entities(world_id, [ent])
    return {"ok": True, "entity": ent}


@router.patch("/api/worlds/{world_id}/entities/{kind}/{entity_key}")
async def entity_patch(world_id: int, kind: str, entity_key: str, body: EntityIn,
                       _rl: None = Depends(guard_for("entity_write"))):
    _world_or_404(world_id)
    _check_kind(world_id, kind)   # A2 (аудит 41): PATCH раньше пропускал любой kind
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
    # A4 (аудит 38): раньше удаление возвращало 200 даже на несуществующих мире/карточке —
    # тихий no-op. Теперь: сначала проверяем мир (404), потом саму карточку (404).
    _world_or_404(world_id)
    if not db.get_entity(world_id, kind, entity_key):
        raise HTTPException(404, "Карточка не найдена")
    db.delete_entity(world_id, kind, entity_key)
    try:
        await chroma_client.delete_by_ids([f"ent_{world_id}_{kind}_{entity_key}".replace(' ', '_')])
    except Exception as e:
        # карточка удалена из SQLite, но может остаться в векторной памяти — видно в логе
        log.warning("чистка карточки из Chroma (world %s, %s/%s): %s", world_id, kind, entity_key, e)
    return {"ok": True}