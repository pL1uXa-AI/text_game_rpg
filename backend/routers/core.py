# -*- coding: utf-8 -*-
"""Общие помощники и ядро хода (раньше жили в app.py). Разделяются роутерами."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import random
import re
from pathlib import Path

import time

from fastapi import HTTPException

from .. import db, graph, llm, metrics, narrator, tts
from ..config import est_tokens, get_config, KEY_MASK
from ..schemas import ProviderIn

log = logging.getLogger("textgame")

FRONTEND_DIR = str(Path(narrator.__file__).resolve().parent.parent / "frontend")

# Фоновое обновление карточек: один процесс на мир (чтобы не плодить очередь LLM-вызовов)
_cards_busy: set[int] = set()

# Фоновые динамические события мира: один процесс на мир
_event_busy: set[int] = set()

# Фоновый судья логики: один процесс на мир
_judge_busy: set[int] = set()

# Фоновый автономный «мастер» (сюжетный вывод из тупика): один процесс на мир
_master_busy: set[int] = set()

# Фоновый боевой ИИ врагов (тактические решения живых врагов): один процесс на мир
_enemy_ai_busy: set[int] = set()

# Фоновое разыгрывание видений/снов (сессия 32): один процесс на мир
_vision_busy: set[int] = set()

# Реестр событий последнего сыгранного хода (миров: список id). При перегенерации ↻ эти события
# удаляются и заменяются новыми — в истории не накапливаются «копии одного ответа». Хранится в
# памяти (single-process uvicorn); после рестарта реген просто работает по-старому (append).
# Фоновые события мира (динамические/судья) сюда НЕ попадают и перегенерацией не стираются.
_turn_events: dict[int, list[int]] = {}
_turn_seq: dict[int, int] = {}

# Максимальная длина действия игрока (символов) — защита от многотысячного ввода,
# который ломает токен-бюджет контекста. Эвристика: len/3.2 ≈ 200 токенов при 640.
MAX_ACTION_CHARS = int(get_config().max_action_chars)


def _ensure_action_len(text: str) -> str:
    """Валидация длины действия: 400/429 с понятным сообщением (фронт покажет в чате)."""
    t = (text or "").strip()
    if not t:
        raise HTTPException(400, "Пустое действие")
    if len(t) > MAX_ACTION_CHARS:
        raise HTTPException(
            400,
            f"Действие слишком длинное: {len(t)} символов (максимум {MAX_ACTION_CHARS}).\n"
            "Разбей на несколько шагов или сократи.",
        )
    return t


def _mask_provider(p: dict) -> dict:
    """Скрываем api_key при отдаче в UI."""
    out = dict(p)
    out["api_key"] = KEY_MASK if p.get("api_key") else ""
    return out


def _world_provider_settings(world: dict) -> dict:
    try:
        return json.loads(world.get("provider_settings") or "{}")
    except Exception:
        return {}


def _world_providers(world: dict) -> dict:
    return get_config().resolve_world_providers(_world_provider_settings(world))


def _masked_world_providers(world: dict) -> dict:
    return {k: _mask_provider(dict(v)) for k, v in _world_providers(world).items()}


async def _context_guard(world_id: int, providers: dict, gen_settings: dict) -> dict:
    """Авто-детект реального окна модели и защита от обрезов (сессия 33).

    Стандарт `CONTEXT_TOKENS=32768` рассчитан на облачные модели; локальная llama.cpp
    ходит с n_ctx 8192. Если мир заявляет больше, чем модель потянет, промпт обрезается
    молча и рассказчик «теряет» память. Здесь:
      * просим модель отдать её лимит (`llm.probe_context`, коротко и с кэшем);
      * если лимит меньше заявленного контекста мира — ПОНИЖАЕМ `context_tokens` до
        безопасного значения (с запасом под ответ) и пишем в `gen_settings.context_limit`;
      * если лимита не нашли — ничего не трогаем (не додумываем за пользователя).

    Возвращает (возможно обновлённый) dict gen_settings. Никогда не бросает —
    недоступный /models не должен валить создание мира или сохранение настроек."""
    g = dict(gen_settings or {})
    if not get_config().detect_model_context:
        return g
    try:
        prov = providers.get("main") or {}
        info = await llm.probe_context(prov)
    except Exception as e:
        log.warning("context_guard (world %s): probe не удался — %s", world_id, e)
        return g
    if not info:
        g.pop("context_limit", None)
        return g
    limit = int(info["max_context"])
    g["context_limit"] = limit
    g["context_limit_source"] = info["source"]
    declared = int(g.get("context_tokens") or 0)
    # запас: под ответ (max_tokens) и оверхед промпта уже заложены в world_recent_budget,
    # но сам промпт целиком обязан влезть в n_ctx — берём 95% от лимита как потолок.
    safe = int(limit * 0.95)
    if declared > safe:
        g["context_tokens"] = max(512, safe)
        log.warning("context_guard (world %s): контекст снижен %s → %s (лимит модели %s, %s)",
                    world_id, declared, g["context_tokens"], limit, info["source"])
    return g


def _world_persona(world: dict) -> str | None:
    nid = world.get("narrator_id")
    if not nid:
        return None
    try:
        nr = db.get_narrator(int(nid))
        return (nr or {}).get("prompt") or None
    except Exception:
        return None


async def _index_world_lore(world_id: int, providers: dict | None = None) -> None:
    """Фоновая индексация лора мира в Chroma (после сидинга при создании мира)."""
    try:
        p = (providers or {}).get("embedding")
        await narrator.index_all_lore(world_id, provider=p)
    except Exception as e:
        log.warning("_index_world_lore (world %s): %s", world_id, e)


def _summaries(world_id: int) -> list[dict]:
    evs = db.get_events(world_id)
    return [e for e in evs if e["role"] == "summary"][-3:]


def _memory_audit(rag_chunks: list[str], lore_chunks: list[str], note: str | None = None) -> list[dict]:
    """Компактный список подхваченных фрагментов памяти/лора для прозрачности RAG.
    `note` — необязательная подсказка о деградации памяти (см. `_rag_degraded_note`)."""
    out: list[dict] = []
    if note:
        out.append({"kind": "⚠", "text": note})
    for kind, chunks in (("Память", rag_chunks), ("Лор", lore_chunks)):
        for c in chunks or []:
            text = (c or "").strip()
            if not text:
                continue
            out.append({"kind": kind, "text": text[:220] + ("…" if len(text) > 220 else "")})
    return out[:20]


def _rag_note(world: dict, rag_chunks: list[str]) -> str | None:
    """Если RAG-поиск памяти вернул пусто из-за выключенных/недоступных эмбеддингов —
    короткая подсказка, что векторная память деградировала (но игра живёт)."""
    if rag_chunks:
        return None
    try:
        providers = get_config().resolve_world_providers(_world_provider_settings(world))
        emb = providers.get("embedding") or {}
    except Exception:
        return None
    if not emb.get("enabled", True):
        return "Память понижена: эмбеддинги выключены — глубокий поиск недоступен."
    return None


def _recent_block(world_id: int) -> list[dict]:
    """Последние несвёрнутые события в пределах токен-бюджета (per-world контекст)."""
    world = db.get_world(world_id)
    budget = narrator.world_recent_budget(world) if world else get_config().recent_token_budget
    evs = [e for e in db.get_events(world_id) if not e["folded"] and e["role"] in ("player", "narrator")]
    out: list[dict] = []
    used = 0
    for e in reversed(evs):
        t = est_tokens(e["content"])
        if used + t > budget and out:
            break
        used += t
        out.append(e)
    return list(reversed(out))


def _gen_params(world: dict) -> dict:
    g = json.loads(world.get("gen_settings") or "{}")
    cfg = get_config()
    return {"temperature": g.get("temperature", cfg.default_temp),
            "top_p": g.get("top_p", cfg.default_top_p),
            "max_tokens": g.get("max_tokens", cfg.max_tokens)}


def _repetition_score(text: str) -> float:
    """Метрика качества (ИИ-качество): доля слов, повторяющихся внутри одного ответа.
    Признак «деталей» рассказчика — если рассказчик зациклился/зациклился на одних фразах,
    доля дубликатов растёт. 0..1, 0 = без повторов, >=0.4 ≈ заметная цикличность."""
    try:
        tokens = re.findall(r"[а-яА-Яa-zA-ZёЁ0-9]{3,}", (text or "").lower())
        if len(tokens) < 8:
            return 0.0
        seen = set(tokens)
        dup = len(tokens) - len(seen)
        return round(dup / len(tokens), 3)
    except Exception:
        return 0.0


def _apply_provider_override(current: dict, body: ProviderIn | None) -> dict:
    """Объединяет пользовательскую правку провайдера с текущими переопределениями мира.
    api_key='' или KEY_MASK означает «оставить как есть»."""
    if not body:
        return current
    out = dict(current)
    if body.id is not None:
        out["id"] = body.id
    if body.enabled is not None:
        out["enabled"] = body.enabled
    if body.base_url is not None and body.base_url.strip():
        out["base_url"] = body.base_url.strip().rstrip("/")
    if body.base_url == "":
        out.pop("base_url", None)
    if body.api_key is not None and body.api_key.strip() and body.api_key != KEY_MASK:
        out["api_key"] = body.api_key.strip()
    if body.api_key == "" and out.get("id") not in ("llamacpp", "ollama", "none"):
        out.pop("api_key", None)
    if body.model is not None and body.model.strip():
        out["model"] = body.model.strip()
    if body.model == "":
        out.pop("model", None)
    return out


async def _narrate_mech_outcome(world: dict, setting: dict, persona: str | None,
                                 providers: dict, sys_msgs: list[str], action: str) -> str:
    """Когда модель вернула только механику (tool_call/<<ENGINE>>) без прозы,
    генерирует короткий связный текст, что произошло в мире. Возвращает прозу
    или пустую строку (тогда вызывающий пишет «Механика применена»)."""
    try:
        prov_main = providers["main"]
        mech = "\n".join(sys_msgs).strip() if sys_msgs else "механика сработала, но без видимых сообщений."
        msgs = [
            {"role": "system", "content": narrator.build_system_prompt(world, setting, persona=persona)
             + "\n\nТы только что применил механику хода, но забыл написать игроку текст."
               " Не вызывай директивы и не пиши <<ENGINE>>/game_engine. Опиши результат как связный эпизод."},
            {"role": "user", "content": f"Действие игрока: {action}\n\nЧто произошло в итоге: {mech}\n"
               " Напиши 1–3 предложения игрового текста."},
        ]
        out = (await llm.complete(msgs, temperature=0.7, max_tokens=250,
                                  provider=prov_main)).strip()
        return out[:900]
    except Exception as e:
        log.warning("нарративное дописывание механики (world %s): %s", world.get("id"), e)
        return ""


_FINISH_CHARS = (".", "!", "?", "…", "\"", "»", "'", "”", "\n")


def _looks_finished(text: str) -> bool:
    """Закончен ли ответ рассказчика (не оборван на полуслове/незакрытой мысли).
    Модель в tools-режиме иногда прерывает текст на вызове game_engine (roll) —
    хвост остаётся оборванным («Ты идёшь к груде кам»)."""
    t = (text or "").strip()
    if not t:
        return True
    if len(t) < 20:
        return True  # короткие ответы не трогаем (может быть намеренно)
    last = t[-1]
    if last in _FINISH_CHARS:
        return True
    # последние символы — закрывающие скобки/кавычки после знака препинания
    return bool(re.search(r"[.!?…»”\"]\s*[)\]»\"']*$", t[-4:]))


def _dedupe_repeats(text: str) -> str:
    """Убирает ПОДРЯД идущие точные дубликаты абзацев/предложений (модель «зациклилась»:
    один и тот же кусок написан дважды). Не трогает намеренные повторы (короткие фразы)."""
    t = (text or "").strip()
    if len(t) < 80:
        return text or ""
    # блоки = абзацы или предложения
    blocks = [b.strip() for b in re.split(r"(?<=\n)\s*|(?<=[.!?…])\s+", t) if b.strip()]
    out: list[str] = []
    for b in blocks:
        if out and len(b) >= 30 and b == out[-1]:
            continue  # точный дубликат подряд — пропускаем повтор
        out.append(b)
    res = " ".join(out).strip()
    return res if res else text or ""


async def _finish_cut_reply(world: dict, setting: dict, persona: str | None,
                            providers: dict, text: str, action: str, sys_msgs: list[str]) -> str:
    """Страховка обрыва: модель прервала текст на полуслове (часто из-за tool_call в середине,
    дублирующего абзаца). Дописывает 1–3 завершающих предложения, чтобы игрок не видел «кам».
    При сбое возвращает исходный текст (ход не роняем)."""
    try:
        prov_main = providers["main"]
        mech = "\n".join(sys_msgs).strip() if sys_msgs else ""
        tail = "\n\nПрименённая механика хода: " + mech if mech else ""
        msgs = [
            {"role": "system", "content": narrator.build_system_prompt(world, setting, persona=persona)
             + "\n\nТвой ответ оборвался на полуслове (модель прервалась). Допиши ТОЛЬКО завершение "
               "эпизода: 1–3 предложения в том же стиле, закончи мысль полностью. НЕ начинай заново, "
               "НЕ вызывай директивы, НЕ пиши <<ENGINE>>/game_engine, НЕ повторяй уже написанное."},
            {"role": "user", "content": f"Действие игрока: {action}\n\nОборванный текст:\n{text[-1200:]}"
               + tail + "\n\nПродолжи с последнего слова и заверши сцену."},
        ]
        out = (await llm.complete(msgs, temperature=0.6, max_tokens=200,
                                  provider=prov_main)).strip()
        out = _dedupe_repeats(out)
        if not out:
            return text
        # подчищаем, если модель начала дублировать конец оборванного текста
        if out[:40] == text[-40:]:
            out = out[40:].lstrip(",. ")
        return (text.rstrip() + " " + out.lstrip()).strip()
    except Exception as e:
        log.warning("дописывание оборванного ответа (world %s): %s", world.get("id"), e)
        return text


async def _process_action(world_id: int, text: str, stream_emit=None, regenerate: bool = False):
    """Ядро обработки действия. stream_emit(text) — опциональный колбэк для токенов.
    regenerate=True: не создаёт новое событие игрока, а перегенерирует ответ на прошлый ход."""
    world = db.get_world(world_id)
    if not world:
        raise HTTPException(404, "Мир не найден")
    setting = json.loads(world["setting"])
    if setting.get("game_over"):
        raise HTTPException(409, "Игра окончена в этом мире. Создай новый мир или загрузи сохранение.")

    # Снапшот состояния до хода: для перегенерации (откат тика эффектов и директив)
    restored_snap = False
    if regenerate:
        snap = world.get("snapshot")
        if snap:
            try:
                restored = json.loads(snap) if isinstance(snap, str) else snap
                if isinstance(restored, dict):
                    setting = restored
                    restored_snap = True
            except Exception as e:
                log.warning("восстановление снепшота (world %s): %s", world_id, e)
    else:
        db.update_world(world_id, snapshot=copy.deepcopy(setting))

    # Статус-эффекты (яд и т.п.) тикают в начале каждого хода.
    # Это автоматика по умолчанию, но рассказчик-мастер может выключить её глобально
    # (.env TICK_EFFECTS_ENABLED=false) и вести урон/лечение сам директивами player.hp.
    # При перегенерации тикаем только после отката к снапшоту (иначе задвоится урон).
    tick_msgs: list[str] = []
    if (not regenerate or restored_snap) and get_config().tick_effects_enabled:
        tick_msgs = narrator.tick_effects(setting)
        if setting["player"].get("hp", 0) <= 0 and not setting.get("game_over"):
            setting["game_over"] = True
            tick_msgs.append("💀 Игрок погиб от эффекта. Мир замирает…")

    # Сессия 32: таймеры мира (дедлайны) и потребности/рассудок тикают в начале хода
    if not regenerate or restored_snap:
        if get_config().tick_needs_enabled:
            try:
                tick_msgs += narrator.tick_needs_mental(setting)
            except Exception as e:
                log.warning("tick_needs_mental (world %s): %s", world_id, e, exc_info=True)
        try:
            tick_msgs += narrator.tick_world_timers(setting)
        except Exception as e:
            log.warning("tick_world_timers (world %s): %s", world_id, e, exc_info=True)

    providers = _world_providers(world)
    persona = _world_persona(world)
    # ── Механика: облачные openai_compat → function calling (tool_calls), локальные → <<ENGINE>> ──
    prov_main = providers["main"]
    use_tools = narrator.use_tools_for_provider(prov_main)
    game_tools = narrator.GAME_ENGINE_TOOL if use_tools else None
    tool_calls_out: list[dict] = []

    text = text.strip()
    # если в начале хода сработали эффекты — рассказчик должен знать об этом
    action_ctx = text
    if tick_msgs:
        action_ctx = "[Начало хода]\n" + "\n".join(tick_msgs) + "\n\n" + text
    tmp = re.search(r"<<ENGINE>>", text, re.DOTALL)
    player_ev = None
    if regenerate:
        # ищем последнее действие игрока — под него индексируем новую версию ответа в памяти
        prevs = db.get_events(world_id)
        prev_player = next((e for e in reversed(prevs) if e["role"] == "player"), None)
        idx_seq = prev_player["seq"] if prev_player else db.latest_seq(world_id)
        # заменяем события прошлого хода (только если регенерируем именно последний ход)
        if _turn_seq.get(world_id) == idx_seq:
            replaced_ids = _turn_events.pop(world_id, [])
        else:
            replaced_ids = []
    else:
        player_ev = db.add_event(world_id, "player", text.replace("<<ENGINE>>", ""))
        idx_seq = player_ev["seq"]
        replaced_ids = []
        # счётчик действий игрока — для динамических событий мира (раз в N ходов)
        setting["_player_turns"] = setting.get("_player_turns", 0) + 1

    # Память + карточки
    rag_chunks = await narrator.retrieve_memory(world_id, text, setting, providers=providers)
    lore_chunks = await narrator.retrieve_lore(world_id, text, setting, providers=providers)
    recent = _recent_block(world_id)
    summaries = _summaries(world_id)
    entity_cards = narrator.select_relevant_entities(world_id, setting, text)
    messages = narrator.build_messages(world, setting, action_ctx, recent, summaries, rag_chunks,
                                      entity_cards, persona=persona, use_tools=use_tools,
                                      lore=lore_chunks)

    params = _gen_params(world)
    full = ""
    _t0 = time.monotonic()
    if stream_emit:
        stopped = False
        safe = ""
        async for delta in llm.stream_chat(messages, provider=prov_main, tools=game_tools,
                                           tool_calls_out=tool_calls_out, **params):
            full += delta
            if not stopped:
                idx = narrator.find_engine_start(full)
                if idx >= 0:
                    safe_part = full[: idx]
                    if len(safe_part) > len(safe):
                        await stream_emit(safe_part[len(safe):])
                    safe = safe_part
                    stopped = True
                else:
                    await stream_emit(delta)
        full = full  # noop
    else:
        full = await llm.complete(messages, provider=prov_main, tools=game_tools,
                                tool_calls_out=tool_calls_out, **params)
    _llm_ms = (time.monotonic() - _t0) * 1000

    # Директивы: приоритет у tool_calls (function calling), фолбэк на промпт-формат <<ENGINE>>
    clean, d_engine = narrator.split_engine(full)
    directives: dict | None = None
    if tool_calls_out and tool_calls_out[0].get("arguments"):
        directives = narrator.parse_tool_args(tool_calls_out[0]["arguments"]) or None
    if directives is None:
        directives = d_engine
    # Аудит — ЕДИНСТВЕННЫЙ источник механики на ходах с явным действием (mech_trigger).
    # Рассказчик пишет художку; механику из ТЕКСТА извлекает отдельный проход (надёжнее, чем
    # просить модель самой эмитить enemy_add/enemy_apply/эффекты — она их регулярно забывает).
    # Его результат АВТОРИТЕТЕН и перекрывает директивы рассказчика (чтобы не было рассинхрона).
    need_audit = (use_tools and narrator.mech_trigger(text))
    if need_audit:
        audit_out: list[dict] = []
        try:
            await llm.complete(narrator.audit_messages(setting, text, reply=full[:1400]),
                               temperature=0.1, max_tokens=320, provider=prov_main,
                               tools=narrator.GAME_ENGINE_TOOL, tool_choice="required",
                               tool_calls_out=audit_out)
            if audit_out and audit_out[0].get("arguments"):
                d2 = narrator.parse_tool_args(audit_out[0]["arguments"]) or {}
                if d2:
                    # Аудит авторитетен: если он вернул меx/директивы — используем их как основу,
                    # сохраняя только roll (брошенный рассказчиком) поверх.
                    kept = {k: v for k, v in (directives or {}).items() if k == "roll"}
                    kept.update(d2)
                    directives = kept
        except Exception as e:
            log.warning("нормилзация директив (world %s): %s", world_id, e)
    # Нормализация типов директив: «str вместо dict» из LLM выправляется/отбрасывается,
    # чтобы ход не падал с 'str' object has no attribute 'get'.
    directives = narrator.normalize_directives(directives) or None
    sys_msgs: list[str] = []
    roll_extra = ""
    dice_events: list[dict] = []
    try:
        # Директивы механики (mutates setting). Смена профессии/класса — решение рассказчика
        # (директива profession/class), а НЕ автотриггер по счётчику действий — см. правило 19.
        if directives:
            # Сессия 32: при перемещении (move) эффекты старой локации-зоны снимаются,
            # новой — накладываются (радиация/туман/проклятие). Это «физика»: защита и
            # последствия решает мастер (закон 3).
            try:
                if "move" in directives:
                    old_loc = setting.get("current_location", "start")
                    msgs_loc = narrator.apply_location_effects(setting, old_loc, apply=False)
                    sys_msgs.extend(msgs_loc)
            except Exception as e:
                log.warning("снятие эффектов зоны при move (world %s): %s", world_id, e, exc_info=True)
            sys_msgs = narrator.apply_directives(setting, directives)
            try:
                if "move" in directives:
                    new_loc = setting.get("current_location", "start")
                    msgs_loc = narrator.apply_location_effects(setting, new_loc, apply=True)
                    sys_msgs.extend(msgs_loc)
            except Exception as e:
                log.warning("наложение эффектов зоны при move (world %s): %s", world_id, e, exc_info=True)

        # Бросок куба по запросу рассказчика
        if directives and "roll" in directives:
            r = directives["roll"] if isinstance(directives["roll"], dict) else {}
            try:
                mod = int(r.get("mod", 0))
            except (TypeError, ValueError):
                mod = 0
            try:
                dc = int(r.get("dc", 15))
            except (TypeError, ValueError):
                dc = 15
            expr = str(r.get("expr", "d20")).strip() or "d20"
            label = str(r.get("label", "проверка"))[:120]
            res = narrator.roll_expr(expr, mod)
            total = res["total"]
            outcome = narrator.roll_outcome(total, dc, expr)
            emoji = {"критический успех": "🎉", "успех": "✅", "провал": "❌", "критический провал": "💥"}.get(outcome, "")
            dice_ev = db.add_event(world_id, "dice",
                                   f"🎲 Проверка «{label}»\nКуб: {expr}" + (f" +{mod}" if mod else "")
                                   + f" → {' '.join(map(str, res['rolls']))} = {total}\nСложность: {dc}\n"
                                   + f"Итог: {emoji} {outcome}")
            dice_events.append(dice_ev)
            roll_desc = await narrator.narrate_roll(world_id, label, expr, mod, dc, total,
                                                    outcome, world.get("language", "ru"),
                                                    persona=persona, provider=providers["main"],
                                                    action=text,
                                                    situation=clean)
            if roll_desc:
                roll_extra = "\n\n" + roll_desc
    except Exception:
        # Последний рубеж: «мусорные» директивы не должны ронять ход —
        # текст ответа уже сгенерирован, механика просто не применится.
        pass

    final_text = (clean or full.strip()) + roll_extra
    # ── Защита от «деградации» ответа: ──
    # 1) убираем ПОДРЯД идущие точные дубликаты абзацев/предложений (модель «зациклилась»);
    # 2) если текст оборван на полуслове/незакрытой мысли (часто: модель вызвала game_engine
    #    в середине ответа и бросила писать) — дописываем завершение отдельным проходом.
    if final_text.strip():
        cleaned = _dedupe_repeats(final_text)
        if cleaned != final_text:
            final_text = cleaned
        if not _looks_finished(final_text) and not final_text.strip().endswith((".", "!", "?", "…")):
            final_text = await _finish_cut_reply(world, setting, persona, providers,
                                                 final_text.strip(), text, sys_msgs)
    if not final_text.strip():
        # модель отправила ТОЛЬКО механику (tool_call / <<ENGINE>>) без прозы —
        # не пишем бессмысленное «Механика применена», а сгенерируем связный текст.
        final_text = await _narrate_mech_outcome(world, setting, persona, providers, sys_msgs, text)
    if not final_text.strip():
        final_text = ("Механика применена." + roll_extra).strip()

    # Прозрачность RAG: какие фрагменты памяти/лора были подхвачены в этом ответе
    # (для UI «🧠 Память»). Пусто — ничего подхвачено не было. Сохраняем в meta события,
    # чтобы плашка «Память» оставалась у ответа и после перезагрузки страницы.
    memory_used = _memory_audit(rag_chunks, lore_chunks, note=_rag_note(world, rag_chunks))

    # ── Атомарная запись хода: ответ + системные сообщения + состояние мира ──
    # Всё создание событий и обновление setting — одна транзакция, чтобы при сбое
    # не осталось «событий без обновлённого состояния» (половины хода).
    with db.transaction():
        # Перегенерация: убираем события прошлого хода, чтобы не копились «копии ответа»
        if replaced_ids:
            db.delete_events_by_id(world_id, replaced_ids)
        narrator_ev = db.add_event(world_id, "narrator", final_text,
                                   meta={"memory_used": memory_used} if memory_used else None)

        # ── Озвучка (TTS): фоновый синтез, не блокирует ответ. Статус 1 = «в работе» ──
        try:
            if final_text.strip() and tts.tts_effective(world).get("enabled"):
                db.set_event_tts(narrator_ev["id"], 1, "")
                narrator_ev["tts_status"] = 1   # фронт сразу покажет «⟳» и опросит готовность
                asyncio.get_event_loop().create_task(
                    tts.ensure_event_audio(world_id, narrator_ev["id"], final_text))
        except Exception as e:
            log.warning("TTS-задача (world %s, seq %s): %s", world_id, idx_seq, e)

        # Системные сообщения (урон/предметы/квесты...)
        system_events = [db.add_event(world_id, "system", m) for m in tick_msgs + sys_msgs]

        db.update_world(world_id, setting=setting)
        if setting.get("game_over"):
            db.add_event(world_id, "system", "💀 Игра окончена. Используй «сохранить слот» или создай новый мир.")

        # 🌐 Графовая БД: синхронизируем карту мира с актуальным состоянием локаций
        # (рёбра/connections) — атомарно, в той же транзакции хода. best-effort:
        # граф не критичен для хода, при сбое лишь логируем.
        try:
            graph.sync_from_setting(world_id, setting)
        except Exception as e:
            log.warning("граф (world %s, seq %s): %s", world_id, idx_seq, e)

        # Карточки знаний: раса/класс/профессия/навык/эффект/предмет — чтобы память не противоречила.
        # Создаются детерминированно (без LLM), условие получения берётся из системных сообщений хода.
        try:
            kcards = narrator.ensure_knowledge_cards(world_id, setting, seq=idx_seq, origins=sys_msgs)
            if kcards:
                asyncio.get_event_loop().create_task(narrator.index_entities(world_id, kcards))
        except Exception as e:
            log.warning("карточки знаний (world %s, seq %s): %s", world_id, idx_seq, e)

    # ── Автосохранение: после каждого обычного хода (не при перегенерации) ──
    if not regenerate:
        try:
            db.upsert_auto_save(world_id, setting, idx_seq)
        except Exception as e:
            log.warning("автосохранение (world %s, seq %s): %s", world_id, idx_seq, e)

    # Варианты действий от ИИ — ситуационные («бытовые»), кнопки под чатом.
    # При сбое или таймауте фронт подставит бытовые заготовки от состояния.
    suggestions: list[str] = []
    if final_text.strip():
        try:
            suggestions = await asyncio.wait_for(
                narrator.generate_suggestions(setting, text, final_text, provider=prov_main),
                timeout=12)
        except Exception:
            suggestions = []

    # Память: индексация + свёртка + карточки сущностей в фоне (глобальный выключатель фоновых задач)
    if get_config().background_tasks_enabled:
        asyncio.get_event_loop().create_task(_background_memory(world_id, idx_seq, text, final_text))
        asyncio.get_event_loop().create_task(_background_cards(world_id, text, final_text))
    # Динамические события мира — отдельный флаг (dynamic_events_enabled), не зависят от фоновых задач
    asyncio.get_event_loop().create_task(_maybe_dynamic_event(world_id))
    # Судья логики: фоновая проверка противоречий (не блокирует ответ; по интервалу ходов)
    if _maybe_logic_judge_enabled(world):
        asyncio.get_event_loop().create_task(
            _maybe_logic_judge(world_id, copy.deepcopy(setting), text, final_text))

    # Автономный «мастер»: если игрок «застрял» (повторяет действие / без квестов) — фоново
    # генерирует квест/сюжетный поворот, выводит из тупика. Не блокирует ответ, по интервалу ходов.
    if get_config().autonomous_master_enabled:
        asyncio.get_event_loop().create_task(
            _maybe_autonomous_master(world_id, copy.deepcopy(setting), text, final_text))

    # Боевой ИИ врагов: пока в бою есть живые враги, фоново выбирает их тактический ход
    # (охрана/отступление/переговоры/ловушка). Не блокирует ответ, по интервалу ходов.
    if get_config().enemy_ai_enabled:
        asyncio.get_event_loop().create_task(
            _maybe_enemy_ai(world_id, copy.deepcopy(setting), text, final_text))

    # Сны/видения (сессия 32): если рассказчик вызвал trigger_vision и в очереди есть
    # видение — разыгрываем его отдельным LLM-проходом в фоне (память как сюжет).
    try:
        if isinstance(directives, dict) and "trigger_vision" in directives \
                and (setting.get("pending_visions") or []):
            asyncio.get_event_loop().create_task(
                _maybe_trigger_vision(world_id, copy.deepcopy(setting)))
    except Exception as e:
        log.warning("запуск видения (world %s): %s", world_id, e, exc_info=True)

    # Реестр событий последнего хода — чтобы следующая перегенерация заменила их (не накапливалось)
    _turn_events[world_id] = [e["id"] for e in dice_events] + [narrator_ev["id"]] + [e["id"] for e in system_events]
    _turn_seq[world_id] = idx_seq

    # ── Профилирование и мониторинг + ИИ-качество (метрики) ──
    # Намеренно в try/except с логированием (не роняет ход, даже если метрика сломается):
    # сбор метрик — побочное, не критично для игры.
    try:
        completion_tokens = est_tokens(final_text)  # видимый ответ (в tools-режиме проза живёт не в `full`)
        prompt_tokens = est_tokens("\n".join(str(m.get("content", "") or "") for m in messages))
        memory_tokens = sum(len(c or "") for c in rag_chunks + lore_chunks) // 4
        metrics.record(
            world_id=world_id,
            llm_ms=_llm_ms,
            completion_tokens=completion_tokens,
            prompt_tokens=prompt_tokens,
            memory_tokens=memory_tokens,
            repetition=_repetition_score(final_text),
            provider=prov_main.get("id"),
            game_over=bool(setting.get("game_over")),
        )
    except Exception as e:
        log.warning("метрики хода (world %s): %s", world_id, e)

    return {
        "world_id": world_id,
        "reply": final_text,
        "reply_event_id": narrator_ev["id"],
        "events": ([player_ev] if player_ev else []) + dice_events + [narrator_ev, *system_events],
        "state": setting,
        "game_over": setting.get("game_over", False),
        "suggestions": suggestions,
        "replaced_events": replaced_ids,
        # Прозрачность RAG: подхваченные фрагменты памяти/лора (уже сохранены в meta хода)
        "memory_used": memory_used,
    }


def _maybe_logic_judge_enabled(world: dict) -> bool:
    """Включён ли судья логики для мира: глобально (config) && per-world (gen_settings.logic_judge)."""
    if not get_config().logic_judge_enabled:
        return False
    try:
        g = json.loads(world.get("gen_settings") or "{}")
        return bool(g.get("logic_judge", True))  # per-world не задано → включено по умолчанию
    except Exception:
        return True


async def _maybe_logic_judge(world_id: int, setting: dict, action: str, reply: str) -> None:
    """Фоновая проверка логики (судья): сверяет последний ответ с критичными фактами мира И
    согласованность биографии персонажа с его ролью (раса/класс/профессия/навыки).
    - Противоречие фактам → системное сообщение-«искажение реальности» (сюжетный поворот);
    - Несогласованность роли/биографии → применяет корректировку (движок директив) + системное сообщение.
    Не блокирует ответ игроку, не запускается повторно для мира и не чаще интервала ходов."""
    if world_id in _judge_busy:
        return
    _judge_busy.add(world_id)
    _t0 = time.monotonic()
    try:
        await asyncio.sleep(1.5)  # даём приоритет ответу игроку
        cfg = get_config()
        if not cfg.logic_judge_enabled:
            return
        world = db.get_world(world_id)
        if not world or not _maybe_logic_judge_enabled(world):
            return
        providers = _world_providers(world)
        s = json.loads(world["setting"])
        # не чаще интервала ходов (экономия токенов)
        interval = max(1, cfg.logic_judge_interval)
        last = s.get("_judge_last_turn", 0) or 0
        turns = s.get("_player_turns", 0) or 0
        if turns - last < interval:
            return
        res = await narrator.logic_judge(world_id, setting, action, reply,
                                         lang=world.get("language", "ru"),
                                         provider=providers["main"])
        if not res:
            return
        # свежее состояние: игрок мог успеть походить; не дублируем при активной игре
        s2 = json.loads(db.get_world(world_id)["setting"])
        if (s2.get("_judge_last_turn", 0) or 0) > last:
            return
        s2["_judge_last_turn"] = turns
        db.update_world(world_id, setting=s2)
        # 1) сюжетный поворот по фактам
        if res.get("twist"):
            db.add_event(world_id, "system", res["twist"])
        # 2) корректировка согласованности биографии и роли (если судья нашёл несоответствие)
        corr_msgs: list[str] = []
        if res.get("corrections"):
            s3 = json.loads(db.get_world(world_id)["setting"])
            try:
                corr_msgs = narrator._apply_judge_corrections(s3, res["corrections"])
                if corr_msgs:
                    db.update_world(world_id, setting=s3)
            except Exception as e:
                log.warning("применение корректировок судьи (world %s): %s", world_id, e)
        # индексируем искажение/поправку в память
        try:
            seq = db.latest_seq(world_id)
            cur_world = db.get_world(world_id)
            embed = _world_providers(cur_world).get("embedding") if cur_world else None
            for content in ([res.get("twist")] if res.get("twist") else []) + \
                            ([msg for msg in corr_msgs] if corr_msgs else []):
                if content:
                    await narrator.index_exchange(world_id, seq, "🌫 Судья логики", content,
                                                  provider=embed)
        except Exception as e:
            log.warning("index за судью (world %s): %s", world_id, e)
        metrics.record_agent("judge", (time.monotonic() - _t0) * 1000, world_id, ok=True)
    except Exception as e:
        log.warning("_maybe_logic_judge (world %s): %s", world_id, e)
        metrics.record_agent("judge", (time.monotonic() - _t0) * 1000, world_id, ok=False)
    finally:
        _judge_busy.discard(world_id)


async def _background_memory(world_id: int, seq: int, action: str, reply: str) -> None:
    _t0 = time.monotonic()
    try:
        world = db.get_world(world_id)
        providers = _world_providers(world) if world else None
        await narrator.index_exchange(world_id, seq, action, reply,
                                      provider=(providers or {}).get("embedding"))
        await narrator.summarize_and_compress(world_id, provider=(providers or {}).get("main"))
    except Exception as e:
        log.warning("_background_memory (world %s): %s", world_id, e)
        metrics.record_agent("memory", (time.monotonic() - _t0) * 1000, world_id, ok=False)
    else:
        metrics.record_agent("memory", (time.monotonic() - _t0) * 1000, world_id, ok=True)


async def _background_cards(world_id: int, action: str, reply: str) -> None:
    """Фоновое обновление карточек сущностей (персонажи/локации/квесты...).
    Не запускается повторно, пока предыдущий проход для мира не закончен."""
    if world_id in _cards_busy:
        return
    _cards_busy.add(world_id)
    _t0 = time.monotonic()
    try:
        await asyncio.sleep(1)  # даём приоритет ответу игроку
        world = db.get_world(world_id)
        provider = _world_providers(world)["main"] if world else None
        await narrator.update_entity_cards(world_id, action, reply, provider=provider)
    except Exception as e:
        log.warning("_background_cards (world %s): %s", world_id, e)
        metrics.record_agent("cards", (time.monotonic() - _t0) * 1000, world_id, ok=False)
    else:
        metrics.record_agent("cards", (time.monotonic() - _t0) * 1000, world_id, ok=True)
    finally:
        _cards_busy.discard(world_id)


async def _maybe_dynamic_event(world_id: int) -> None:
    """Фоновая генерация случайных событий мира — только по ходам игрока: шанс растёт с каждым
    действием (DYNAMIC_EVENTS_ENABLED / EVENT_EVERY_TURNS в .env), таймера по реальному времени НЕТ.
    Событие — системное сообщение + механические изменения (через apply_directives).
    Не блокирует ответ игроку и не запускается повторно для мира."""
    if world_id in _event_busy:
        return
    _event_busy.add(world_id)
    _t0 = time.monotonic()
    try:
        await asyncio.sleep(2)  # даём приоритет ответу игроку
        cfg = get_config()
        if not cfg.dynamic_events_enabled:
            return
        world = db.get_world(world_id)
        if not world:
            return
        providers = _world_providers(world)
        setting = json.loads(world["setting"])
        turns = setting.get("_player_turns", 0) or 0
        last_turn = setting.get("_event_last_turn", 0) or 0
        if turns <= 0:
            return
        # Случайные события — ТОЛЬКО от активности игрока: шанс растёт с каждым его ходом
        # p = (ходы с прошлого события) / EVENT_EVERY_TURNS, но НИКОГДА не гарантирован
        # (потолок 0.5 — гарантии нет даже у порога, см. narrator.event_chance).
        # Таймер по реальному времени убран: игрок может отойти — события сами не лезут.
        turns_since = turns - last_turn
        # Динамическая сложность: частота событий растёт с уровнем игрока
        # (narrator.event_chance учитывает level).
        plvl = int((setting.get("player") or {}).get("level", 1) or 1)
        if not (turns_since > 0 and random.random() < narrator.event_chance(
                turns_since, cfg.event_every_turns, plvl)):
            return
        ev = await narrator.generate_dynamic_event(world, setting, provider=providers["main"])
        if not ev:
            return
        event_text, directives = ev
        # свежее состояние: игрок мог успеть походить, пока генерировали; не задваиваем событие
        world2 = db.get_world(world_id)
        setting2 = json.loads(world2["setting"])
        if (setting2.get("_event_last_turn", 0) or 0) != last_turn:
            return
        msgs = narrator.apply_directives(setting2, directives) if directives else []
        setting2["_event_last_turn"] = turns
        db.update_world(world_id, setting=setting2)
        content = event_text
        if msgs:
            content += "\n" + "\n".join(msgs)
        ev_db = db.add_event(world_id, "system", "🌍 " + content)
        try:
            asyncio.get_event_loop().create_task(narrator.index_exchange(
                world_id, ev_db["seq"], "🌍 Случайное событие мира", content,
                provider=providers.get("embedding")))
        except Exception as e:
            log.warning("index за события (world %s): %s", world_id, e)
        metrics.record_agent("event", (time.monotonic() - _t0) * 1000, world_id, ok=True)
    except Exception as e:
        log.warning("_maybe_dynamic_event (world %s): %s", world_id, e)
        metrics.record_agent("event", (time.monotonic() - _t0) * 1000, world_id, ok=False)
    finally:
        _event_busy.discard(world_id)


async def _maybe_trigger_vision(world_id: int, setting: dict) -> None:
    """Фоновое разыгрывание видения/сна (сессия 32): рассказчик вызвал trigger_vision —
    берём первое видение из pending_visions, LLM-проход (narrator.generate_vision)
    оборачивает старые факты в художественный сон. Не блокирует ответ; при сбое видение
    возвращается в очередь (не теряется)."""
    if world_id in _vision_busy:
        return
    _vision_busy.add(world_id)
    try:
        await asyncio.sleep(1.5)  # даём приоритет ответу игроку
        world = db.get_world(world_id)
        if not world:
            return
        s = json.loads(world["setting"])
        pv = s.get("pending_visions") or []
        if not isinstance(pv, list) or not pv:
            return
        vision = pv.pop(0)
        providers = _world_providers(world)
        # Память как сюжет: подмешиваем RAG-факты в видение (эхо прошлого)
        memories: list[str] = []
        try:
            mem = await narrator.retrieve_memory(world_id, "видение, сон, прошлое", s,
                                                 providers=providers)
            memories = [str(m) for m in (mem or [])[:6]]
        except Exception as e:
            log.warning("RAG для видения (world %s): %s", world_id, e)
        txt = await narrator.generate_vision(s, vision, lang=world.get("language", "ru"),
                                             provider=providers["main"], memories=memories)
        if not txt:
            pv.insert(0, vision)  # не теряем видение при сбое
            return
        content = f"🌙 Видение:\n{txt}"
        with db.transaction():
            vis_ev = db.add_event(world_id, "narrator", content)
            db.update_world(world_id, setting=s)
        try:
            seq = db.latest_seq(world_id)
            embed = _world_providers(db.get_world(world_id)).get("embedding")
            await narrator.index_exchange(world_id, seq, "🌙 Видение", content, provider=embed)
        except Exception as e:
            log.warning("index за видение (world %s): %s", world_id, e)
    except Exception as e:
        log.warning("_maybe_trigger_vision (world %s): %s", world_id, e, exc_info=True)
    finally:
        _vision_busy.discard(world_id)


async def _maybe_autonomous_master(world_id: int, setting: dict, action: str, reply: str) -> None:
    """Фоновый автономный «мастер» (сюжетный режиссёр): когда игрок «застрял» (повторяет одно
    действие или долго без активных квестов), детерминированный сигнал + компактный LLM-проход
    генерируют квест или сюжетный поворот, выводящий игрока из тупика. Системное сообщение +
    опциональные директивы (через apply_directives). Вмешивается редко: интервал ходов из
    AUTONOMOUS_MASTER_INTERVAL; при разнообразии действий — молчит. Не блокирует ответ игроку."""
    if world_id in _master_busy:
        return
    _master_busy.add(world_id)
    _t0 = time.monotonic()
    try:
        await asyncio.sleep(2.5)  # даём приоритет ответу игроку и другим фоновым агентам
        cfg = get_config()
        if not cfg.autonomous_master_enabled:
            return
        world = db.get_world(world_id)
        if not world:
            return
        s = json.loads(world["setting"])
        turns = s.get("_player_turns", 0) or 0
        interval = max(1, cfg.autonomous_master_interval)
        last = s.get("_master_last_turn", 0) or 0
        if turns - last < interval:
            return
        # Детерминированный признак «застрял»: повтор последних действий игрока / брождение без квестов
        evs = db.get_events(world_id)
        actions = [e["content"] for e in evs if e["role"] == "player"]
        quests = s.get("quests") or {}
        active = sum(1 for q in quests.values() if isinstance(q, dict) and q.get("status") in ("active", None))
        reason = narrator.master_stuck_reason(actions, active, turns)
        if not reason:
            return
        providers = _world_providers(world)
        # компактный контекст — последний обмен + состояние (не раздуваем токены)
        recent_text = (("Действие игрока: " + (action or "")[:300])
                       + ("\nОтвет рассказчика: " + (reply or "")[:500]))
        res = await narrator.generate_master_nudge(s, reason, recent_text,
                                                   lang=world.get("language", "ru"),
                                                   provider=providers["main"])
        if not res:
            return
        event_text, directives = res
        # свежее состояние: игрок мог успеть походить/мастер уже вмешался
        s2 = json.loads(db.get_world(world_id)["setting"])
        if (s2.get("_master_last_turn", 0) or 0) > last:
            return
        s2["_master_last_turn"] = turns
        msgs = narrator.apply_directives(s2, directives) if directives else []
        db.update_world(world_id, setting=s2)
        content = event_text
        if msgs:
            content += "\n" + "\n".join(msgs)
        ev_db = db.add_event(world_id, "system", "🤖 Мастер: " + content)
        try:
            asyncio.get_event_loop().create_task(narrator.index_exchange(
                world_id, ev_db["seq"], "🤖 Действие автономного мастера",
                event_text + ("\n" + "\n".join(msgs) if msgs else ""),
                provider=(_world_providers(db.get_world(world_id)) or {}).get("embedding")))
        except Exception as e:
            log.warning("index за автономного мастера (world %s): %s", world_id, e)
        metrics.record_agent("master", (time.monotonic() - _t0) * 1000, world_id, ok=True)
    except Exception as e:
        log.warning("_maybe_autonomous_master (world %s): %s", world_id, e)
        metrics.record_agent("master", (time.monotonic() - _t0) * 1000, world_id, ok=False)
        _master_busy.discard(world_id)
        return
    finally:
        pass


async def _maybe_enemy_ai(world_id: int, setting: dict, action: str, reply: str) -> None:
    """Фоновый боевой ИИ врагов: пока в бою есть живые враги, выбирает тактический ход противника
    (преследование/охрана/отступление/переговоры/ловушка) с оглядкой на стиль игрока и соотношение сил.
    Не наносит урон игроку напрямую — это делает рассказчик в своём ходе; «Мастеру» просто подсказывает
    стратегию. По интервалу ходов, не блокирует ответ игроку, один процесс на мир."""
    if world_id in _enemy_ai_busy:
        return
    _enemy_ai_busy.add(world_id)
    _t0 = time.monotonic()
    try:
        await asyncio.sleep(2)  # даём приоритет ответу игроку и другим фоновым агентам
        cfg = get_config()
        if not cfg.enemy_ai_enabled:
            return
        world = db.get_world(world_id)
        if not world:
            return
        s = json.loads(world["setting"])
        turns = s.get("_player_turns", 0) or 0
        interval = max(1, cfg.enemy_ai_interval)
        last = s.get("_enemy_ai_last_turn", 0) or 0
        if turns - last < interval:
            return
        if not narrator._living_enemies(s):
            return  # нет живых врагов — боевой ИИ нечего решать
        providers = _world_providers(world)
        recent_text = (("Действие игрока: " + (action or "")[:300])
                       + ("\nОтвет рассказчика: " + (reply or "")[:500]))
        res = await narrator.generate_enemy_ai(s, recent_text,
                                               lang=world.get("language", "ru"),
                                               provider=providers["main"])
        if not res:
            return
        eid, etext, mode, directives = res
        # свежее состояние: игрок мог добить/увести врагов; не вмешиваемся, если врагов уж нет
        s2 = json.loads(db.get_world(world_id)["setting"])
        if (s2.get("_enemy_ai_last_turn", 0) or 0) > last:
            return
        if not narrator._living_enemies(s2):
            return
        s2["_enemy_ai_last_turn"] = turns
        msgs = narrator._apply_enemy_ai(s2, eid, mode, directives)
        db.update_world(world_id, setting=s2)
        content = "⚔️ " + etext
        if msgs:
            content += "\n" + "\n".join(msgs)
        ev_db = db.add_event(world_id, "system", content)
        try:
            asyncio.get_event_loop().create_task(narrator.index_exchange(
                world_id, ev_db["seq"], "⚔️ Боевой ИИ врагов",
                etext + ("\n" + "\n".join(msgs) if msgs else ""),
                provider=(_world_providers(db.get_world(world_id)) or {}).get("embedding")))
        except Exception as e:
            log.warning("index за боевой ИИ (world %s): %s", world_id, e)
        metrics.record_agent("enemy_ai", (time.monotonic() - _t0) * 1000, world_id, ok=True)
    except Exception as e:
        log.warning("_maybe_enemy_ai (world %s): %s", world_id, e)
        metrics.record_agent("enemy_ai", (time.monotonic() - _t0) * 1000, world_id, ok=False)
    finally:
        _enemy_ai_busy.discard(world_id)