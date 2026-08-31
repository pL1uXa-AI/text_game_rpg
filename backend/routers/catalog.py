# -*- coding: utf-8 -*-
"""Роутер каталога: темы миров, жанры, рассказчики (CRUD), свои сюжеты (CRUD), опции провайдеров."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import db, narrator
from ..config import get_config, PROVIDER_OPTIONS
from ..schemas import NarratorIn, PlotIn

router = APIRouter(tags=["Каталог"])


@router.get("/api/themes")
async def themes():
    # Сюжеты-миры живут в папке plots/ (system+user): дешёвая проверка mtime подхватывает
    # новые/изменённые файлы без перезапуска; полная перезагрузка — кнопкой «Обновить сюжеты».
    narrator.ensure_plots_fresh()
    # Рекомендуемые рассказчики: имя из сюжета → id в БД (для авто-подстановки в UI при выборе).
    name_to_id = {n["name"]: n["id"] for n in db.list_narrators()}
    out = []
    for t in narrator.THEMES:
        nid = None
        if t.get("narrator"):
            nid = name_to_id.get(t["narrator"])
        out.append({"id": t["id"], "name": t["name"], "genre": t["genre"], "desc": t["desc"],
                    "group": t.get("group", "user"),
                    "narrator_id": nid, "narrator": t.get("narrator")})
    return out


@router.post("/api/plots/reload")
async def plots_reload():
    """Перечитать сюжеты с диска (кнопка «🔄 Обновить сюжеты») без перезапуска сервера."""
    return {"ok": True, **narrator.reload_plots()}


@router.get("/api/genres")
async def genres():
    """Доступные жанры (GENRE_HINTS) — для выбора нескольких жанров при создании мира."""
    return [{"id": g, "name": g, "hint": h} for g, h in narrator.GENRE_HINTS.items()]


# ───────────────────────────── Рассказчики ─────────────────────────────
@router.get("/api/narrators")
async def narrators():
    # Пресеты живут файлами в plots/narrators/*.js: дешёвая проверка mtime подхватывает
    # новые/изменённые файлы без перезапуска; изменения перезасеиваются в БД
    # (INSERT OR IGNORE по name — идемпотентно, существующие записи не трогаются).
    if narrator.ensure_narrators_fresh():
        db.seed_narrators(narrator.NARRATOR_PRESETS)
    return db.list_narrators()


@router.post("/api/narrators")
async def narrator_create(body: NarratorIn):
    name = body.name.strip()
    prompt = body.prompt.strip()
    if not name or not prompt:
        raise HTTPException(400, "Имя и промпт рассказчика обязательны.")
    try:
        nr = db.create_narrator(name, prompt, (body.desc or "").strip())
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "narrator": nr}


@router.patch("/api/narrators/{narrator_id}")
async def narrator_update(narrator_id: int, body: NarratorIn):
    try:
        nr = db.update_narrator(narrator_id, name=body.name, prompt=body.prompt,
                                desc=(body.desc if body.desc is not None else None))
    except KeyError:
        raise HTTPException(404, "Рассказчик не найден")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "narrator": nr}


@router.delete("/api/narrators/{narrator_id}")
async def narrator_delete(narrator_id: int):
    if not db.get_narrator(narrator_id):
        raise HTTPException(404, "Рассказчик не найден")
    in_use = db.narrators_in_use(narrator_id)
    if in_use:
        raise HTTPException(409, f"Рассказчик используется {in_use} миром(-ами). Сначала смени его в настройках миров.")
    db.delete_narrator(narrator_id)
    return {"ok": True}


# ───────────────────────────── Свои сюжеты (кастомные заготовки) ─────────────────────────────
@router.get("/api/plots")
async def plots_list():
    return db.list_plots()


@router.post("/api/plots")
async def plot_create(body: PlotIn):
    try:
        pl = db.create_plot(body.name, body.plot, lore=body.lore or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "plot": pl}


@router.patch("/api/plots/{plot_id}")
async def plot_update(plot_id: int, body: PlotIn):
    try:
        pl = db.update_plot(plot_id, name=body.name, plot=body.plot, lore=body.lore)
    except KeyError:
        raise HTTPException(404, "Сюжет не найден")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "plot": pl}


@router.delete("/api/plots/{plot_id}")
async def plot_delete(plot_id: int):
    if not db.get_plot(plot_id):
        raise HTTPException(404, "Сюжет не найден")
    db.delete_plot(plot_id)
    return {"ok": True}


# ───────────────────────────── Провайдеры ─────────────────────────────
@router.get("/api/providers")
async def providers_options():
    return {
        "main": [dict(o) for o in PROVIDER_OPTIONS["main"]],
        "embedding": [dict(o) for o in PROVIDER_OPTIONS["embedding"]],
        "rerank": [dict(o) for o in PROVIDER_OPTIONS["rerank"]],
        "tts": [dict(o) for o in PROVIDER_OPTIONS["tts"]],
        "rerank_enabled_global": get_config().rerank_enabled,
    }