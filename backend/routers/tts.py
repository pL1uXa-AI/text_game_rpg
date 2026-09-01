# -*- coding: utf-8 -*-
"""Роутер озвучки (TTS): статус движков, проверка/скачивание голосов,
настройки per-world, аудио событий и поллинг готовности."""
from __future__ import annotations

import base64
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .. import bg, db, narrator, tts
from ..config import get_config
from ..schemas import TtsDownloadIn, TtsSettingsIn, TtsTestIn

router = APIRouter(tags=["Озвучка (TTS)"])


@router.get("/api/tts/status")
async def tts_status():
    return tts.status_for_ui()


@router.get("/api/worlds/{world_id}/tts/settings")
async def world_tts_settings_get(world_id: int):
    world = db.get_world(world_id)
    if not world:
        raise HTTPException(404, "Мир не найден")
    return {"effective": tts.tts_effective(world),
            "overrides": json.loads(world.get("tts_settings") or "{}")}


@router.post("/api/worlds/{world_id}/tts/settings")
async def world_tts_settings_set(world_id: int, body: TtsSettingsIn):
    world = db.get_world(world_id)
    if not world:
        raise HTTPException(404, "Мир не найден")
    ov = json.loads(world.get("tts_settings") or "{}")
    if body.enabled is not None:
        ov["enabled"] = body.enabled
    if body.provider is not None:
        p = body.provider.strip()
        if p in ("piper", "kokoro", "edge", "none"):
            ov["provider"] = p
        elif p == "":
            ov.pop("provider", None)
        else:
            raise HTTPException(400, f"Неизвестный TTS-провайдер: {body.provider}")
    if body.voice is not None:
        v = body.voice.strip()
        ov["voice"] = v if v else ""
    if body.rate is not None:
        r = body.rate.strip()
        ov["rate"] = r if r else "+0%"
    if body.auto_play is not None:
        ov["auto_play"] = body.auto_play
    db.update_world(world_id, tts_settings=ov)
    return {"ok": True, "effective": tts.tts_effective(db.get_world(world_id)), "overrides": ov}


@router.post("/api/tts/test")
async def tts_test(body: TtsTestIn):
    """Мгновенный синтез тестовой фразы: возвращает аудио base64 (для кнопки «Проверить голос»)."""
    cfg = get_config()
    provider = (body.provider or "").strip() or cfg.tts_provider or "none"
    if provider == "none":
        return {"ok": False, "error": "Озвучка выключена — выбери провайдера"}
    voice = (body.voice or "").strip() or tts.DEFAULT_VOICES.get(provider, "")
    rate = (body.rate or "").strip() or cfg.tts_rate or "+0%"
    text = (body.text or "").strip() or "Привет, путник! Это проверка голоса рассказчика."
    try:
        audio, fmt, _sr = await tts.synthesize(text, provider, voice, rate)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    mime = "audio/wav" if fmt == "wav" else "audio/mpeg"
    return {"ok": True, "mime": mime, "data": base64.b64encode(audio).decode("ascii")}


@router.post("/api/tts/download")
async def tts_download(body: TtsDownloadIn):
    """Скачивание голосовых моделей: piper (голос) / kokoro (модель целиком)."""
    if body.provider == "piper":
        voice = body.voice or tts.DEFAULT_VOICES["piper"]
        try:
            return await tts.download_piper_voice(voice)
        except Exception as e:
            return {"ok": False, "error": str(e)}
    if body.provider == "kokoro":
        try:
            return await tts.download_kokoro_model()
        except Exception as e:
            return {"ok": False, "error": str(e)}
    return {"ok": False, "error": "Неизвестный провайдер для скачивания"}


@router.get("/api/worlds/{world_id}/events/{event_id}/audio")
async def event_audio(world_id: int, event_id: int):
    """Отдаёт готовый аудиофайл озвучки события (только status=2)."""
    ev = db.get_event(event_id)
    if not ev or ev.get("world_id") != world_id:
        raise HTTPException(404, "Событие не найдено")
    if ev.get("tts_status") != 2:
        raise HTTPException(404, "Аудио ещё не готово (или озвучка выключена)")
    rel = ev.get("tts_file") or ""
    if not rel:
        raise HTTPException(404, "Аудио не найдено")
    p = Path(rel)
    if not p.is_absolute():
        p = Path(narrator.__file__).resolve().parent.parent / "data" / rel
    if not p.exists():
        raise HTTPException(404, "Файл аудио отсутствует")
    mime = "audio/wav" if rel.endswith(".wav") else "audio/mpeg"
    return FileResponse(str(p), media_type=mime, headers={"Cache-Control": "no-store"})


@router.get("/api/worlds/{world_id}/events/{event_id}/tts/status")
async def event_tts_status(world_id: int, event_id: int):
    """Статус озвучки конкретного события (поллинг с фронта)."""
    ev = db.get_event(event_id)
    if not ev or ev.get("world_id") != world_id:
        raise HTTPException(404, "Событие не найдено")
    return {"id": event_id, "tts_status": ev.get("tts_status", 0), "tts_file": ev.get("tts_file", "")}


@router.post("/api/worlds/{world_id}/events/{event_id}/tts/retry")
async def event_tts_retry(world_id: int, event_id: int):
    """Перезапускает фоновый синтез для события (ошибка/вручную)."""
    ev = db.get_event(event_id)
    if not ev or ev.get("world_id") != world_id:
        raise HTTPException(404, "Событие не найдено")
    db.set_event_tts(event_id, 1, "")
    # D6 (аудит 38): bg.spawn вместо get_event_loop().create_task — держит ссылку на
    # задачу (её мог собрать GC: «озвучка не появилась, в логе ничего») и берёт цикл
    # через get_running_loop.
    bg.spawn(tts.ensure_event_audio(world_id, event_id, ev.get("content") or ""),
             name="tts-retry")
    return {"ok": True, "tts_status": 1}