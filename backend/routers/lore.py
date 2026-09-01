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
from ..schemas import LoreIn
from .core import _json_object

log = get_logger(__name__)

router = APIRouter(tags=["Лор мира"])

# A4 (аудит 38): хелпер «мир обязан существовать» больше не дублируется — единая точка в
# routers/core.py, откуда его берут и остальные роутеры. Локальное имя сохранено как
# реэкспорт, т.к. на него были ссылки внутри модуля и в тестах.
from .core import _world_or_404   # noqa: E402  (реэкспорт единого хелпера)


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
    # A4-bis (аудит 38): статья скоупится по id — без сверки с миром её можно было править
    # адресом другого мира (та же порода, что «фидбек чужому событию»). Проверка постфактум
    # дешевле новой сигнатуры db-функции, а гонка с удалением мира здесь безвредна.
    if entry.get("world_id") != world_id:
        raise HTTPException(404, "Статья лора не найдена в этом мире")
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
    _row = db.get_lore(lore_id)
    if not _row:
        raise HTTPException(404, "Статья лора не найдена")
    if _row.get("world_id") != world_id:
        # A4-bis (аудит 38): удалить статью чужого мира по её id — раньше было можно
        raise HTTPException(404, "Статья лора не найдена в этом мире")
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
    from .core import _world_providers as _wp
    # A4 (аудит 38): состояние берём из уже проверенного мира — раньше мир перечитывался
    # вторым запросом БЕЗ проверки (`__import__("json").loads(w["setting"])`) — узкое место,
    # дающее TypeError на гонке с удалением мира.
    w = _world_or_404(world_id)
    setting = _json_object(w.get("setting"))
    chunks = await narrator.retrieve_lore(world_id, q or "", setting,
                                          providers=_wp(w), budget=1200)
    return {"query": q, "chunks": chunks}