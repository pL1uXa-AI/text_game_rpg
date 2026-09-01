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

from .. import bg, db, graph, journal, llm, metrics, narrator, tts
from ..config import est_tokens, get_config, KEY_MASK
from ..schemas import ProviderIn

from ..logsetup import get_logger

log = get_logger(__name__)

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

# Реестр событий последнего сыгранного хода (мир: список id). При перегенерации ↻ эти события
# удаляются и заменяются новыми — в истории не накапливаются «копии одного ответа». Хранится в
# памяти, НО восстанавливается из БД при промахе (сессия 36, п.3B): раньше после рестарта
# сервера реестр был пуст и ↻ работало «append» — в чате оставался второй комплект того же
# хода, а состояние откатывалось к снапшоту → задвоение механики.
# Фоновые события мира (динамические/судья) сюда НЕ попадают и перегенерацией не стираются.
_turn_events: dict[int, list[int]] = {}
_turn_seq: dict[int, int] = {}

# «Барьер перегенерации» (сессия 36, п.3A) живёт в bg.py (`bg.regen_block` /
# `bg.is_regenerating`): о нём должен помнить не только этот модуль, но и слой памяти
# (memory.update_entity_cards пишет setting), а импорт core из memory создал бы цикл.
# Здесь — тонкие обёртки: существующие места чтения и тесты обращаются к ним.


def _regen_active(world_id: int) -> bool:
    """Идёт ли для мира перегенерация (фоновым агентам писать нельзя)."""
    return bg.is_regenerating(world_id)


def _turn_registry(world_id: int, idx_seq: int) -> list[int]:
    """id событий хода `idx_seq`, ПОДЛЕЖАЩИХ замене при перегенерации.

    Быстрый путь — in-memory реестр, сформированный тем же процессом, что писал ход.
    Если его нет/он промахнулся (рестарт сервера) — набор собирается из БД по метке
    `meta.turn` (= seq действия игрока): события одного хода лежат в нескольких seq
    (действие → кубы → ответ → системки), а рядом с ними — фоновые события мира,
    поэтому диапазон seq не различает «свои» и «чужие», а метка различает.
    Раньше после рестарта реестр был пуст и ↻ работало «append»: в чате оставался
    второй комплект того же хода, а состояние откатывалось к снапшоту → задвоение
    механики.

    Метку носят только НОВЫЕ ходы. Для миров, записанных до неё (живые сохранения),
    — консервативный фолбэк: нарратив/кубы в пределах нескольких seq после действия,
    без системок (среди них фоновые события мира) и без 🌙-видений. Худший исход
    такого случая — осевшие в чате старые системные сообщения хода, но не затирание
    чужих событий.
    """
    cached = _turn_events.get(world_id)
    if _turn_seq.get(world_id) == idx_seq and cached is not None:
        return list(cached)
    try:
        evs = db.get_turn_events(world_id, idx_seq, None,
                                 roles=("narrator", "dice", "system"), unfolded_only=False)
    except Exception as e:
        log.warning("реестр хода из БД (world %s, seq %s): %s", world_id, idx_seq, e)
        return []
    ids = [e["id"] for e in evs
           if isinstance(e.get("meta"), dict) and e["meta"].get("turn") == idx_seq]
    if not ids:
        # Консервативный фолбэк для миров, записанных ДО меты (живые сохранения
        # пользователя): берём нарратив и кубы сразу за ходом, но СИСТЕМКИ не трогаем —
        # среди них фоновые события мира, которые ↻ стирать нельзя. 🌙-видение тоже
        # роль narrator, и оно фоновое — исключаем по префиксу.
        lo, hi = idx_seq, idx_seq + 4        # действие + кубы + ответ + пара системок
        ids = [e["id"] for e in evs
               if lo < e["seq"] <= hi
               and e["role"] in ("narrator", "dice")
               and not str(e.get("content") or "").startswith("\U0001f319")]
    if ids:
        _turn_events[world_id] = ids
        _turn_seq[world_id] = idx_seq
    return ids


def invalidate_turn_registry(world_id: int) -> None:
    """Сбросить in-memory реестр хода (после перемотки/загрузки сохранения: те id событий
    уже удалены или сокрыты, а следующий ↻ не должен опираться на устаревший набор)."""
    _turn_events.pop(int(world_id), None)
    _turn_seq.pop(int(world_id), None)


def _bg_may_write(world_id: int, fresh: dict, marker: str, last: int, base_turns: int) -> bool:
    """Может ли фоновый агент записать результат в мир (сессия 36, п.3A/п.17).

    Три условия: не идёт перегенерация этого мира; его собственный маркер хода ещё не
    обновлён другим процессом; и с момента, когда агент снял состояние, игрок не успел
    сходить (иначе агент «дописал» бы мир поверх нового хода несвежими дельтами).
    Проверяется СРАЗУ ПЕРЕД записью, между проверкой и записью await нет — в одном
    цикле событий это атомарно.
    """
    if _regen_active(world_id):
        log.debug("фон %s (world %s): идёт перегенерация — пропуск", marker or "agent", world_id)
        return False
    if int(fresh.get(marker, 0) or 0) > int(last or 0):
        return False
    if int(fresh.get("_player_turns", 0) or 0) > int(base_turns or 0):
        return False
    return True


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


def _summaries(world_id: int, limit: int = 0) -> list[dict]:
    """Сводки для промпта. limit=0 — все (по умолчанию срезалось `[-3:]`, из-за чего
    масштабирование количества сводок от размера контекста в build_messages было мёртвым —
    баг A3). Теперь количество задаёт build_messages/dynamic_memory_k, здесь только потолок."""
    return db.get_summary_events(world_id, limit=limit or 0)


def _memory_audit(rag_chunks: list[str], lore_chunks: list[str], note: str | None = None,
                  rag_scores: list[dict] | None = None) -> list[dict]:
    """Компактный список подхваченных фрагментов памяти/лора для прозрачности RAG.
    `note` — необязательная подсказка о деградации памяти (см. `_rag_note`).
    `rag_scores` (E3, сессия 34) — оценки релевантности от retrieve_memory в том же порядке:
    плашка «🧠 Память» показывает НЕ только «что вспомнилось», но и «насколько сильно»
    (косинус/гибрид/реранкер) — иначе нельзя заметить, что RAG мажет мимо темы хода.
    """
    out: list[dict] = []
    if note:
        out.append({"kind": "⚠", "text": note})
    for kind, chunks in (("Память", rag_chunks), ("Лор", lore_chunks)):
        for i, c in enumerate(chunks or []):
            text = (c or "").strip()
            if not text:
                continue
            item: dict = {"kind": kind, "text": text[:220] + ("…" if len(text) > 220 else "")}
            if kind == "Память" and rag_scores and i < len(rag_scores):
                s = rag_scores[i] or {}
                sim = s.get("hybrid") if s.get("hybrid") is not None else s.get("similarity")
                if sim is not None:
                    item["score"] = round(float(sim), 3)
                if s.get("rerank") is not None:
                    item["rerank"] = s["rerank"]
            out.append(item)
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


def _recent_block(world: dict | None, world_id: int,
                  prompt_tokens: int | None = None) -> list[dict]:
    """Последние несвёрнутые события в пределах токен-бюджета (per-world контекст).

    B5: ограниченный запрос к SQLite вместо «весь лог мира в Python».
    A2: бюджет считается с ИЗМЕРЕННЫМ размером системного промпта этого мира.
    """
    budget = narrator.world_recent_budget(world, prompt_tokens=prompt_tokens) if world \
        else get_config().recent_token_budget
    evs = db.get_unfolded_events(world_id, limit=max(40, int(budget / 40)))
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
    Признак деградации рассказчика — если он зациклился на одних фразах, доля
    дубликатов растёт. 0..1, 0 = без повторов, >=0.4 ≈ заметная цикличность.

    ⚠ ИНВАРИАНТ (сессия 36, п.31): это МЕТРИКА для дашборда, а не цензор — на текст
    ответа она не влияет. Завышается у стилистически повторяющихся моделей (короткие
    предложения, рефрены, «ты/тебя» в каждом) — это ожидаемо, лечить порогами нельзя:
    мера должна оставаться дешёвой и детерминированной.
    """
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
    хвост остаётся оборванным («Ты идёшь к груде кам»).

    ⚠ ИНВАРИАНТ (сессия 36, п.31): решение по ПОСЛЕДНЕМУ символу — сознательная
    эвристика, а не синтаксический анализ. Следствия, которые нельзя считать багами:
      * ответ, обрезанный на `)`/`»` без точки, считается незаконченным → один лишний
        LLM-проход `_finish_cut_reply` (дороже, но честнее, чем показать оборванный текст);
      * короткие (<20 символов) ответы не трогаем вовсе — там обрыв вероятен меньше,
        чем намеренная реплика.
    Если «лишние проходы дописывания» станут заметны в метрике cut_mid — пороги правятся
    ЗДЕСЬ, а не отключением механизма.
    """
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
    один и тот же кусок написан дважды). Не трогает намеренные повторы (короткие фразы).

    ⚠ ИЗВЕСТНЫЕ КОМПРОМИССЫ (сессия 36, п.31) — это защита от деградации ответа, а не
    лингвистический анализ, и «чинить» их агрессивно нельзя:
      * сравнение строго соседнее (`b == out[-1]`), поэтому рефрен «A B A» сохраняется —
        вырезается только дословный повтор подряд;
      * порог len(b) >= 30: короткий рефрен («И всё замерло.») не трогается никогда, а
        ДЛИННАЯ намеренная повторная фраза (рефрен-абзац ≥30 символов подряд) будет
        вырезана. Это осознанная цена: ложный позитив здесь — cosmetic, а пропущенный
        цикл модели игрок читает как сломанный вывод;
      * блоки склеиваются разделителем: абзацы (разделитель — перевод строки) сохраняются
        как абзацы, а предложения внутри абзаца склеиваются через пробел.
    Если появится жалоба «рассказчик повторил фразу намеренно, а система её съела» —
    пересматривать порог 30 или требовать точного совпадения всей ПАРЫ соседних абзацев,
    а не молча отключать дедуп (иначе цикл модели вернётся в чат).
    """
    t = (text or "").strip()
    if len(t) < 80:
        return text or ""
    # split с сохраняющей группой даёт НЕ строгую чередёшку (между абзацами попадает
    # несколько пустых совпадений), поэтому идём по кускам токен-проходом: кусок без
    # непробельных символов — разделитель, остальное — блок.
    pieces = re.split(r"((?<=\n)\s*|(?<=[.!?…])\s+)", t)
    blocks: list[str] = []
    seps: list[str] = []          # seps[i] — разделитель ПЕРЕД blocks[i]
    pending = ""
    for piece in pieces:
        if not piece.strip():
            pending += piece
            continue
        seps.append(pending)
        blocks.append(piece.strip())
        pending = ""
    out: list[str] = []
    dropped = 0
    for i, b in enumerate(blocks):
        if out and len(b) >= 30 and b == out[-1][1]:
            dropped += 1
            continue              # точный дубликат подряд — пропускаем и блок, и разделитель
        out.append((seps[i], b))
    if not dropped:
        return text or ""         # ничего не убрали — не трогаем исходный текст
    res = "".join(s + b for s, b in out).strip()
    return res if res else text or ""


async def _finish_cut_reply(world: dict, setting: dict, persona: str | None,
                            providers: dict, text: str, action: str, sys_msgs: list[str]) -> str:
    """Страховка обрыва: модель прервала текст на полуслове (часто из-за tool_call в середине,
    дублирующего абзаца). Дописывает 1–3 завершающих предложения, чтобы игрок не видел «кам».
    При сбое возвращает исходный текст (ход не роняем)."""
    try:
        prov_main = providers["main"]
        # Страховка (сессия 36, п.4): если в «оборванном» тексте всё ещё остался хвост
        # механики (<<ENGINE>>/game_engine, в т.ч. недописанный), дописывать к нему нельзя —
        # иначе служебный блок приклеится к прозе и уйдёт в чат. Режем от маркера до конца.
        cut = narrator.find_engine_start(text)
        if cut >= 0:
            text = text[:cut].rstrip()
        if not text.strip():
            return text
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
    regenerate=True: не создаёт новое событие игрока, а перегенерирует ответ на прошлый ход.

    B3 (сессия 34): тело оборачивается в `bg.player_turn()` — пока идёт ход, фоновые
    агенты (карточки/судья/мастер/боевой ИИ/события/видения) в очередь к модели не лезут.
    Раньше их пять-шесть стартовало сразу после ответа и «съедали» следующий ход игрока:
    локальная llama.cpp обслуживает запросы по одному.

    Сессия 36, п.3A: на время перегенерации мир ставится в барьер `bg.regen_block` —
    фоновые агенты старого хода (их директивы уже могли лечь в setting) не пишут в мир,
    пока состояние откатано к снапшоту и пересчитывается заново. Иначе ↻ теряло/двоило
    их механику."""
    if regenerate:
        with bg.regen_block(world_id) as acquired:
            # мир уже под перегенерацией (задвоенный клик/ретрай) — не лезем вторым проходом
            if not acquired:
                raise HTTPException(409, "Перегенерация этого хода уже выполняется. Подожди.")
            with bg.player_turn():
                return await _process_action_inner(world_id, text, stream_emit=stream_emit,
                                                   regenerate=regenerate)
    with bg.player_turn():
        return await _process_action_inner(world_id, text, stream_emit=stream_emit,
                                           regenerate=regenerate)


async def _process_action_inner(world_id: int, text: str, stream_emit=None,
                                regenerate: bool = False):
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

    # Копия состояния ДО тика эффектов/директив — она уходит в turn_snapshots (перемотка, C1).
    pre_turn_snapshot = copy.deepcopy(setting)

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
    player_ev = None
    if regenerate:
        # ищем последнее действие игрока — под него индексируем новую версию ответа в памяти
        prev_player = db.get_latest_by_role(world_id, "player")
        idx_seq = prev_player["seq"] if prev_player else db.latest_seq(world_id)
        # заменяем события прошлого хода — по реестру, а при его отсутствии (после рестарта
        # сервера) по БД: тот же ход обязан быть заменён, а не задвоен (сессия 36, п.3B)
        replaced_ids = _turn_registry(world_id, idx_seq)
    else:
        player_ev = db.add_event(world_id, "player", text.replace("<<ENGINE>>", ""))
        idx_seq = player_ev["seq"]
        replaced_ids = []
        # счётчик действий игрока — для динамических событий мира (раз в N ходов)
        setting["_player_turns"] = setting.get("_player_turns", 0) + 1
        # C1 (сессия 34): точка перемотки — состояние ПЕРЕД этим ходом (копия до тика
        # эффектов и до директив — та же, что отдаётся при «↻ перегенерировать»). По ней
        # «назад к ходу N» и загрузка сохранения возвращают мир без задвоенных эффектов.
        try:
            db.save_turn_snapshot(world_id, idx_seq, pre_turn_snapshot,
                                  keep=get_config().turn_snapshot_keep)
        except Exception as e:
            log.warning("снапшот для перемотки (world %s, seq %s): %s", world_id, idx_seq, e,
                        exc_info=True)

    # Память + карточки.
    # B1 (сессия 34): RAG-память и лор — ДВА независимых сетевых похода (эмбеддинг запроса →
    # Chroma → реранкер). Раньше они ждали друг друга последовательно; теперь идут параллельно
    # и суммарно стоят ходу один сетевой цикл, а не два. Ошибки каждого — изолированы
    # (return_exceptions), ход не падает из-за недоступной памяти (закон 2: память помогает,
    # но не блокирует игру), и каждая ошибка остаётся в логе (правило 14).
    rag_scores: list[dict] = []
    prompt_tokens_prev = int(setting.get("_ctx_prompt_tokens") or 0) or None
    recent = _recent_block(world, world_id, prompt_tokens=prompt_tokens_prev)
    summaries = _summaries(world_id)
    entity_cards = narrator.select_relevant_entities(world_id, setting, text)
    mem_res, lore_res = await asyncio.gather(
        narrator.retrieve_memory(world_id, text, setting, providers=providers,
                                 scores_out=rag_scores),
        narrator.retrieve_lore(world_id, text, setting, providers=providers),
        return_exceptions=True)
    if isinstance(mem_res, BaseException):
        log.warning("RAG-память не отработала (world %s): %s", world_id, mem_res)
        rag_chunks = []
    else:
        rag_chunks = mem_res or []
    if isinstance(lore_res, BaseException):
        log.warning("лор-память не отработала (world %s): %s", world_id, lore_res)
        lore_chunks = []
    else:
        lore_chunks = lore_res or []
    messages, prompt_meta = narrator.build_messages(world, setting, action_ctx, recent,
                                                    summaries, rag_chunks, entity_cards,
                                                    persona=persona, use_tools=use_tools,
                                                    lore=lore_chunks)

    params = _gen_params(world)
    full = ""
    # E3 (сессия 34): служебные сведения об ответе модели (finish_reason/usage) — чтобы
    # «обрезан ли ответ лимитом» стало измеримой метрикой, а не догадкой по тексту.
    llm_finish: dict = {}
    _t0 = time.monotonic()
    if stream_emit:
        safe = ""                        # сколько текста уже отдано игроку
        decided = False                  # служебный блок найден → текст больше не отдаём
        async for delta in llm.stream_chat(messages, provider=prov_main, tools=game_tools,
                                           tool_calls_out=tool_calls_out,
                                           finish_out=llm_finish, **params):
            full += delta
            if decided:
                continue
            idx = narrator.find_engine_start(full)
            if idx >= 0:
                # начало механики: дописываем прозу перед маркером и закрываем стрим текста
                if idx > len(safe):
                    await stream_emit(full[len(safe):idx])
                safe = full[:idx]
                decided = True
                continue
            # неразобранный хвост держим в буфере: «…идём к <» или «…game_eng» ещё может
            # оказаться маркером, а показывать половину служебного блока нельзя (п.4)
            cut = len(full) - narrator.engine_tail_hold(full)
            if cut > len(safe):
                await stream_emit(full[len(safe):cut])
                safe = full[:cut]
        # стрим закончился, а маркера не было — отдаём накопленный хвост целиком
        if not decided and len(full) > len(safe):
            await stream_emit(full[len(safe):])
    else:
        full = await llm.complete(messages, provider=prov_main, tools=game_tools,
                                tool_calls_out=tool_calls_out, finish_out=llm_finish, **params)
    _llm_ms = (time.monotonic() - _t0) * 1000
    # A2: запоминаем ИЗМЕРЕННЫЙ размер промпта за этот ход (реальным usage модели, иначе
    # нашей оценкой) — следующий ход построит бюджет recent по фактическому оверхеду,
    # а не по догадке CONTEXT_OVERHEAD=2600.
    measured_prompt = int(llm_finish.get("prompt_tokens") or 0) or est_tokens(
        "\n".join(str(m.get("content", "") or "") for m in messages))
    setting["_ctx_prompt_tokens"] = measured_prompt

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
                    # Аудит авторитетен: если он вернул мех/директивы — используем их как основу,
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
                                   + f"Итог: {emoji} {outcome}",
                                   meta={"turn": idx_seq})
            dice_events.append(dice_ev)
            roll_desc = await narrator.narrate_roll(world_id, label, expr, mod, dc, total,
                                                    outcome, world.get("language", "ru"),
                                                    persona=persona, provider=providers["main"],
                                                    action=text,
                                                    situation=clean)
            if roll_desc:
                roll_extra = "\n\n" + roll_desc
    except Exception as e:
        # Последний рубеж: «мусорные» директивы не должны ронять ход —
        # текст ответа уже сгенерирован, механика просто не применится.
        # Правило 14: раньше это был голый `pass`, и молча пропавшая механика
        # (урон не нанесён, предмет не выдан) была неотличима от «механики не было».
        log.exception("применение директив провалилось (world %s, seq %s) — ход записан "
                      "без части механики: %s", world_id, idx_seq, e)

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
    memory_used = _memory_audit(rag_chunks, lore_chunks, note=_rag_note(world, rag_chunks),
                                rag_scores=rag_scores)

    # ── Атомарная запись хода: ответ + системные сообщения + состояние мира ──
    # Всё создание событий и обновление setting — одна транзакция, чтобы при сбое
    # не осталось «событий без обновлённого состояния» (половины хода).
    with db.transaction():
        # Перегенерация: убираем события прошлого хода, чтобы не копились «копии ответа»
        if replaced_ids:
            db.delete_events_by_id(world_id, replaced_ids)
        narrator_ev = db.add_event(world_id, "narrator", final_text,
                                   meta={"memory_used": memory_used, "turn": idx_seq}
                                   if memory_used else {"turn": idx_seq})

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
        # meta.turn — метка хода: по ней перегенерация находит СВОИ события после рестарта
        # сервера (in-memory реестр к тому моменту пуст), не задевая фоновые системки мира.
        system_events = [db.add_event(world_id, "system", m, meta={"turn": idx_seq})
                         for m in tick_msgs + sys_msgs]

        db.update_world(world_id, setting=setting)
        if setting.get("game_over"):
            db.add_event(world_id, "system", "💀 Игра окончена. Используй «сохранить слот» или создай новый мир.",
                         meta={"turn": idx_seq})

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

        # 📔 Дневник приключений (сессия 34, C2): что в этом ходу было ЗНАЧИМО (квест/итог,
        # первая встреча, уникальная находка, смена роли, новое место, смерть, истёкший срок).
        # Детерминированно по диффу «до/после» (pre_turn_snapshot снят до тика эффектов) —
        # без LLM: хронике нужна точность фактов, а не фантазия (закон 2: только отображение).
        try:
            jcards = journal.record_turn(world_id, pre_turn_snapshot, setting, idx_seq,
                                         action=text, sys_msgs=sys_msgs)
            if jcards:
                asyncio.get_event_loop().create_task(narrator.index_entities(world_id, jcards))
        except Exception as e:
            log.warning("дневник (world %s, seq %s): %s", world_id, idx_seq, e, exc_info=True)

        # C9 «Чеховские ружья» (сессия 34): держим список заряженных, но ещё
        # не прозвучавших намёков (знакомство/предмет/флаг/место).
        # Попадает в format_state как «на горизонте…» — только подсказка:
        # выстрелит или нет, решает мастер (законы 2/3).
        try:
            journal.chekhov_update(setting, pre_turn_snapshot, setting,
                                   reply=final_text, action=text, seq=idx_seq)
        except Exception as e:
            log.warning("ружья Чехова (world %s, seq %s): %s", world_id, idx_seq, e,
                        exc_info=True)

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

    # ── Фоновые задачи (B3, сессия 34) ──
    # Раньше каждая стартовала своим create_task() и все ОНИ лезли в единую очередь модели
    # одновременно, конкурируя со СЛЕДУЮЩИМ ходом игрока. Теперь они идут через bg-очередь:
    # ограниченный параллелизм (LLM_BG_CONCURRENCY), порядок по важности и старт только
    # после того, как ответ игроку отдан (этот ход обёрнут в bg.player_turn()).
    loop = asyncio.get_event_loop()
    # Снимок состояния для фоновых агентов — ОДИН раз, в момент подачи задачи (как и раньше):
    # deepcopy внутри лямбды выполнился бы позже, и агент увидел бы уже изменённое состояние.
    agents_setting = copy.deepcopy(setting)
    if get_config().background_tasks_enabled:
        loop.create_task(bg.submit("memory", lambda: _background_memory(world_id, idx_seq, text,
                                                                        final_text),
                                   priority=bg.PRIO_MEMORY, world_id=world_id, agent="memory"))
        loop.create_task(bg.submit("cards", lambda: _background_cards(world_id, text, final_text),
                                   priority=bg.PRIO_CARDS, world_id=world_id, agent="cards"))
    # Динамические события мира — отдельный флаг (dynamic_events_enabled), не зависят от фоновых задач
    loop.create_task(bg.submit("event", lambda: _maybe_dynamic_event(world_id),
                               priority=bg.PRIO_EVENT, world_id=world_id, agent="event"))
    # Судья логики: фоновая проверка противоречий (не блокирует ответ; по интервалу ходов)
    if _maybe_logic_judge_enabled(world):
        loop.create_task(bg.submit(
            "judge", lambda: _maybe_logic_judge(world_id, agents_setting, text, final_text),
            priority=bg.PRIO_JUDGE, world_id=world_id, agent="judge"))

    # Автономный «мастер»: если игрок «застрял» (повторяет действие / без квестов) — фоново
    # генерирует квест/сюжетный поворот, выводит из тупика. Не блокирует ответ, по интервалу ходов.
    if get_config().autonomous_master_enabled:
        loop.create_task(bg.submit(
            "master",
            lambda: _maybe_autonomous_master(world_id, agents_setting, text, final_text),
            priority=bg.PRIO_MASTER, world_id=world_id, agent="master"))

    # Боевой ИИ врагов: пока в бою есть живые враги, фоново выбирает их тактический ход
    # (охрана/отступление/переговоры/ловушка). Не блокирует ответ, по интервалу ходов.
    if get_config().enemy_ai_enabled:
        loop.create_task(bg.submit(
            "enemy_ai",
            lambda: _maybe_enemy_ai(world_id, agents_setting, text, final_text),
            priority=bg.PRIO_ENEMY_AI, world_id=world_id, agent="enemy_ai"))

    # Сны/видения (сессия 32): если рассказчик вызвал trigger_vision и в очереди есть
    # видение — разыгрываем его отдельным LLM-проходом в фоне (память как сюжет).
    try:
        if isinstance(directives, dict) and "trigger_vision" in directives \
                and (setting.get("pending_visions") or []):
            loop.create_task(bg.submit(
                "vision", lambda: _maybe_trigger_vision(world_id, agents_setting),
                priority=bg.PRIO_VISION, world_id=world_id, agent="vision"))
    except Exception as e:
        log.warning("запуск видения (world %s): %s", world_id, e, exc_info=True)

    # Реестр событий последнего хода — чтобы следующая перегенерация заменила их (не накапливалось)
    _turn_events[world_id] = [e["id"] for e in dice_events] + [narrator_ev["id"]] + [e["id"] for e in system_events]
    _turn_seq[world_id] = idx_seq

    # ── Профилирование и мониторинг + ИИ-качество (метрики) ──
    # Намеренно в try/except с логированием (не роняет ход, даже если метрика сломается):
    # сбор метрик — побочное, не критично для игры.
    try:
        # видимый ответ (в tools-режиме проза живёт не в `full`, поэтому НЕ usage модели),
        # а реальный расход модели кладём отдельными полями — это разные величины.
        completion_tokens = est_tokens(final_text)
        usage_completion = int(llm_finish.get("completion_tokens") or 0)
        memory_tokens = sum(len(c or "") for c in rag_chunks + lore_chunks) // 4
        # E3 (сессия 34): «обрыв фразы» как отдельный измеримый сигнал.
        # Два источника: finish_reason=length (модель упёрлась в лимит токенов — лечится
        # настройкой Max tokens) и finish_reason=stop, но текст закончился на полуслове
        # (модель бросила мысль — лечится промптом/моделью). Раньше дедуп и дописывание
        # были, а статистики по частоте обрезов — нет.
        fr = str(llm_finish.get("finish_reason") or "")
        cut_by_limit = fr == "length"
        cut_mid = (not cut_by_limit) and bool(final_text.strip()) \
            and not _looks_finished(final_text)
        metrics.record(
            world_id=world_id,
            llm_ms=_llm_ms,
            completion_tokens=completion_tokens,
            prompt_tokens=measured_prompt,
            memory_tokens=memory_tokens,
            repetition=_repetition_score(final_text),
            provider=prov_main.get("id"),
            game_over=bool(setting.get("game_over")),
            finish_reason=fr or None,
            usage_completion_tokens=usage_completion or None,
            usage_prompt_tokens=int(llm_finish.get("prompt_tokens") or 0) or None,
            cut_by_limit=cut_by_limit,
            cut_mid=cut_mid,
            prompt_trimmed=", ".join(prompt_meta.get("trimmed") or []) or None,
            memory_k=len(rag_chunks),
            # средняя оценка релевантности вспомненного (E3) — по ней видно, что RAG не мажет мимо
            rag_score_avg=(round(sum((s.get("hybrid") if s.get("hybrid") is not None
                                      else s.get("similarity", 0)) or 0 for s in rag_scores)
                                 / len(rag_scores), 3) if rag_scores else None),
            bg_queue=bg.stats().get("queued"),
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
        # E3: был ли ответ обрезан (видимо в UI — «модель уперлась в лимит, подними Max tokens»)
        "reply_cut": bool(str(llm_finish.get("finish_reason") or "") == "length"),
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
        # свежее состояние: игрок мог успеть походить; не дублируем при активной игре.
        # п.17/п.3A: отказ от записи и если идёт перегенерация того же хода, и если
        # _player_turns сдвинулся (иначе судья перезаписал бы setting состоянием на
        # момент своего чтения и «съел» ход игрока).
        s2 = json.loads(db.get_world(world_id)["setting"])
        if not _bg_may_write(world_id, s2, "_judge_last_turn", last, turns):
            log.debug("судья (world %s): запись пропущена (перегенерация/новый ход)", world_id)
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
            # перечитали состояние — снова сверяемся: за время индекса игрок мог сходить
            if not _bg_may_write(world_id, s3, "_judge_last_turn", turns, turns):
                log.debug("судья (world %s): корректировки отложены (мир изменился)", world_id)
            else:
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
        if not _bg_may_write(world_id, setting2, "_event_last_turn", last_turn, turns):
            log.debug("событие мира (world %s): запись пропущена (перегенерация/новый ход)",
                      world_id)
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
        # с момента чтения s прошло два LLM-прохода: перечитаем состояние и не затираем
        # собой ход игрока/перегенерацию (сессия 36, п.17)
        fresh = json.loads(db.get_world(world_id)["setting"])
        if _regen_active(world_id) or int(fresh.get("_player_turns", 0) or 0) > \
                int(s.get("_player_turns", 0) or 0):
            log.debug("видение (world %s): запись пропущена (перегенерация/новый ход)", world_id)
            return
        fpv = fresh.get("pending_visions")
        if isinstance(fpv, list) and fpv:
            fpv.pop(0)
        content = f"🌙 Видение:\n{txt}"
        with db.transaction():
            db.add_event(world_id, "narrator", content)
            db.update_world(world_id, setting=fresh)
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
        # B5: берём ровно хвост действий из БД (нужны последние ≤6), а не весь лог мира
        actions = [e["content"] for e in db.get_events(world_id, limit=24)
                   if e["role"] == "player"]
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
        if not _bg_may_write(world_id, s2, "_master_last_turn", last, turns):
            log.debug("автономный мастер (world %s): запись пропущена (перегенерация/новый ход)",
                      world_id)
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
    finally:
        _master_busy.discard(world_id)


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
        if not _bg_may_write(world_id, s2, "_enemy_ai_last_turn", last, turns):
            log.debug("боевой ИИ (world %s): запись пропущена (перегенерация/новый ход)",
                      world_id)
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