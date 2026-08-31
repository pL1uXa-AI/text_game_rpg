# -*- coding: utf-8 -*-
"""Роутер лора мира (библия вселенной): CRUD статей + RAG-поиск по лору.

Лор — большие статьи о мире (история, география, системы, фракции, термины),
которые можно дополнять/редактировать вручную. В промпт рассказчику попадает
релевантное/сжатое через narrator.retrieve_lore (RAG по чанкам + якорные статьи).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import db, narrator
from ..logsetup import get_logger

log = get_logger(__name__)
from ..schemas import LoreIn

router = APIRouter(tags=["Лор мира"])


def _world_or_404(world_id: int) -> dict:
    w = db.get_world(world_id)
    if not w:
        raise HTTPException(404, "Мир не найден")
    return w


@router.get("/api/worlds/{world_id}/lore")
async def lore_list(world_id: int):
    _world_or_404(world_id)
    return db.list_lore(world_id)


@router.post("/api/worlds/{world_id}/lore")
async def lore_create(world_id: int, body: LoreIn):
    _world_or_404(world_id)
    title = (body.title or "").strip()
    content = (body.content or "").strip()
    if not title or not content:
        raise HTTPException(400, "Заголовок и текст статьи лора обязательны")
    entry = db.create_lore(world_id, title, content,
                           tags=body.tags or "", is_core=bool(body.is_core), source="user")
    from .core import _world_providers as _wp
    try:
        w = db.get_world(world_id)
        await narrator.index_lore_entry(world_id, entry,
                                        provider=_wp(w).get("embedding") if w else None)
    except Exception as e:
        # статья уже сохранена в БД — не удалось только попасть в RAG-поиск
        log.warning("лор: переиндексация статьи (world %s) не удалась: %s", world_id, e)
    return {"ok": True, "lore": entry}


@router.patch("/api/worlds/{world_id}/lore/{lore_id}")
async def lore_update(world_id: int, lore_id: int, body: LoreIn):
    _world_or_404(world_id)
    try:
        entry = db.update_lore(lore_id,
                               title=body.title, content=body.content,
                               tags=body.tags, is_core=body.is_core)
    except KeyError:
        raise HTTPException(404, "Статья лора не найдена")
    from .core import _world_providers as _wp
    try:
        w = db.get_world(world_id)
        await narrator.index_lore_entry(world_id, entry,
                                        provider=_wp(w).get("embedding") if w else None)
    except Exception as e:
        log.warning("лор: переиндексация статьи (world %s) не удалась: %s", world_id, e)
    return {"ok": True, "lore": entry}


@router.delete("/api/worlds/{world_id}/lore/{lore_id}")
async def lore_delete(world_id: int, lore_id: int):
    _world_or_404(world_id)
    if not db.get_lore(lore_id):
        raise HTTPException(404, "Статья лора не найдена")
    db.delete_lore(lore_id)
    try:
        from .. import chroma_client
        await chroma_client.delete_by_where({"$and": [{"world_id": world_id}, {"kind": "lore"},
                                                     {"lore_id": lore_id}]})
    except Exception as e:
        log.warning("лор: удаление чанков статьи %s (world %s) из Chroma не удалось: %s",
                    lore_id, world_id, e)
    return {"ok": True}


@router.get("/api/worlds/{world_id}/lore/search")
async def lore_search(world_id: int, q: str):
    """RAG-поиск по лору: набор релевантных фрагментов (для UI предпросмотра)."""
    _world_or_404(world_id)
    from .core import _world_providers as _wp
    w = db.get_world(world_id)
    setting = __import__("json").loads(w["setting"])
    chunks = await narrator.retrieve_lore(world_id, q or "", setting,
                                          providers=_wp(w), budget=1200)
    return {"query": q, "chunks": chunks}