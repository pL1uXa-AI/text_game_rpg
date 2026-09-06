# -*- coding: utf-8 -*-
"""
narrator.py — движок игры: темы миров, сборка промптов рассказчика,
механика (кубы d20/d100, урон, инвентарь, квесты, NPC, флаги),
гибридная память (сводки + RAG через ChromaDB/облачные эмбеддинги).
"""
from __future__ import annotations

import json
import logging
import random
import re
from typing import Any, Optional

from . import db, llm
from .config import clamp_gen_settings, est_tokens, get_config

# Движок директив (механика) живёт в mechanics.py; отсюда реэкспортируется ТОЛЬКО то,
# на что ссылаются через фасад (narrator.apply_directives, narrator.tick_effects, …).
# Сессия 38 (D2): список стал явным и коротким — раньше «на всякий случай» импортировалось
# 39 имён, из которых ни одно не читалось ни внутри narrator.py, ни как narrator.X
# (ruff глушился глобальным ignore = ["F401"]). Остальное берут из backend.mechanics:
# RANK_ORDER, RANK_TEXT, STAT_HINTS, RACES/CLASSES/PROFESSIONS (+*_NAMES), rank_index,
# has_station, equip_item/unequip_item/equipped_bonuses, board_add, check_profession_advance.
from .mechanics import (
    DEFAULT_STATS, PROF_ACTION_MAP,
    norm_rank, ensure_player_schema, effective_stats, recalc_derived,
    apply_directives, normalize_directives, reputation_standing, faction_rep_value,
    dynamic_adversary_scale, inventory_weight, carry_capacity,
    total_sell_value, location_stations, can_craft,
    board_text, location_effects_for, desc_compact, effect_needs_turns,
    find_similar_effect, is_player_npc,
    # — то, что вызывает router-слой и скрипты через narrator.X —
    tick_effects, tick_world_timers, tick_needs_mental, tick_time,
    apply_location_effects, normalize_setting_ranks,
)
# Сессия 63 («живой мир»): отходы от канвы сюжета — отдельный крошечный модуль состояния
# (без циклов: plot_deviation никого не импортирует из backend, кроме typing).
# Правило 37 сознательно НЕ добавлено в _GATED_RULES: в отличие от таймеров/нужд/зон, это
# не подсистема, которую можно «не использовать в мире», а право мастера отойти от канвы —
# оно нужно КАЖДОМУ ходу каждого нового мира, иначе мир снова станет рельсовым.
from .plot_deviation import plot_deviation_text

# ── Фасад (реэкспорт) ────────────────────────────────────────────────
# narrator.py — тонкий фасад: имена НИЖЕ импортированы не для внутреннего использования,
# а чтобы работал прежний стиль вызовов (narrator.tick_effects(...), narrator.NARRATOR_PRESETS).
# Явный __all__ (аудит 38, D2) заменяет прежний глобальный ruff-ignore F401: линтер больше не
# глушится на весь файл, а «транзит» виден и защищён от случайного удаления.
__all__ = [
    # механика через фасад (зовут routers/core.py, app.py, scripts/test_directives.py)
    "tick_effects", "tick_world_timers", "tick_needs_mental", "tick_time",
    "apply_location_effects", "normalize_setting_ranks",
    # пресеты рассказчиков (app.py / routers/catalog.py: db.seed_narrators(...))
    "NARRATOR_PRESETS",
]

from .logsetup import current_context, get_logger, log_once

log = get_logger(__name__)


from . import plots
# NARRATOR_PRESETS — реэкспорт: на него смотрят app.py и routers/catalog.py
# (db.seed_narrators(narrator.NARRATOR_PRESETS)), поэтому он здесь не «транзит».
from .narrators_loader import (
    NARRATOR_PRESETS,
    reload as reload_narrators_impl,
    ensure_fresh as ensure_narrators_fresh_impl,
)
from .narrator_data import GENRE_HINTS, TAIL_RULES, TAIL_RULE_BASE


def _agent_quiet(agent: str, why: str, exc: BaseException | None = None) -> None:
    """A6 (аудит 41, правило 14): отказ фонового агента больше не тихий.

    `generate_*` возвращали `None` по `except Exception` молча — игрок видел просто
    «ничего не произошло», а в журнале не было ни следа. При этом агент живёт каждый
    ход, поэтому обычный `log.warning` залил бы журнал одним и тем же текстом (когда
    модель лежит — падает КАЖДЫЙ проход). Отсюда `log_once` с ключом по (агент, мир):
    первая запись — с полным трейсбеком, дальше — только в debug.

    `agent` — короткая метка (event/master/enemy_ai/vision/roll); мир берётся из
    контекста фона (`bg` поднимает `turn_context(world_id=..., agent=...)` перед задачей),
    поэтому запись в журнале всё равно привязана к конкретному миру.
    """
    wid = current_context().get("world_id")
    log_once(log, f"agent-fail:{agent}:{wid}", logging.WARNING,
             "агент «%s» (world %s) не сработал: %s", agent, wid, why,
             exc_info=exc)


# Живой список тем взято из загрузчика сюжетов (plots/system + plots/user), а НЕ хардкод.
# narrator.THEMES == plots.THEMES (тот же список): правки файлов сюжетов видны после reload().
THEMES = plots.THEMES


def get_theme(theme_id: str) -> Optional[dict]:
    return plots.get_theme(theme_id)


def get_plot(plot_id: str) -> Optional[dict]:
    """Сырой сюжет по id (для применения стартового состояния при создании мира)."""
    return plots.get_plot(plot_id)


def reload_plots() -> dict:
    """Перечитать сюжеты с диска (кнопка «Обновить сюжеты» в UI) без перезапуска."""
    return plots.reload()


def ensure_plots_fresh() -> None:
    """Дешёвая проверка файлов сюжетов на изменения (вызывается при GET /api/themes)."""
    plots.ensure_fresh()


def reload_narrators() -> list[dict]:
    """Перечитать пресеты рассказчиков с диска (файлы plots/narrators/*.js) без перезапуска."""
    return reload_narrators_impl()


def ensure_narrators_fresh() -> bool:
    """Дешёвая проверка файлов рассказчиков на изменения (вызывается при GET /api/narrators).
    Возвращает True, если файлы изменились (пресеты перечитаны) — тогда нужно пересидить в БД."""
    return ensure_narrators_fresh_impl()


# ── Свой (кастомный) сюжет: псевдо-тема из имени и текста сюжета ──
def theme_from_custom(name: str, plot: str, genres: list[str] | None = None) -> dict:
    """Собирает тему-обёртку для кастомного сюжета (не хранится в THEMES)."""
    sel = [g.strip().lower() for g in (genres or []) if g and g.strip().lower() in GENRE_HINTS]
    genre = ", ".join(sel) or "приключение"
    return {
        "id": "custom",
        "name": name or "Свой сюжет",
        "genre": genre,
        "desc": plot[:300],
        "style": ("Пиши живо и атмосферно, в русле этого жанра: держи интригу, "
                  "не противоречь фактам мира и логике, плавно раскрывай завязку. "
                  "Заявленный сюжет — СТАРТОВАЯ КАНВА (точка отсчёта), а не обязательный путь: "
                  "мир живёт по действиям игрока и может свернуть с канвы."),
        "starter": {"gold": 20, "inventory": []},
        "opening": plot,
    }


def _world_theme(world: dict, setting: dict) -> dict:
    """Тема мира с фолбэком на снапшот: если сюжет из файла позже удалён/отредактирован,
    мир продолжает жить по данным, которые были на момент создания (правило: миры не зависят
    от файлов сюжетов). Снапшот хранится в setting['_theme_snapshot'] (ставится при создании).
    Для ОЧЕНЬ старых миров (созданы ещё при встроенных темах, без снапшота) синтезирует
    минимальную тему из полей мира — чтобы рассказчик не оставался совсем без стиля/жанра."""
    t = get_theme(world.get("theme") or "")
    if t:
        return t
    snap = (setting or {}).get("_theme_snapshot")
    if isinstance(snap, dict) and snap:
        return snap
    # Фолбэк для старых/внешних миров: темы нет в plots/ и нет снапшота
    name = world.get("name") or "Мир"
    genre = (world.get("genre") or "приключение")
    hook = (world.get("custom_hook") or "").strip()
    return {
        "name": name, "genre": genre, "id": world.get("theme") or "legacy",
        "desc": hook[:300] or name,
        "style": f"Пиши живо и атмосферно в русле этого мира (жанр: {genre}); держи канон и логику мира, "
                  "не противоречь установленным фактам. Сюжет/завязка — точка отсчёта, а не обязательный путь: "
                  "мир меняется под действия игрока.",
        "starter": {}, "opening": hook or "Мир пробуждается. Что ты делаешь?", "lore": [],
    }


def _pl_int(v, default=0):
    """Безопасный int для полей стартового состояния сюжета."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _pl_float(v, default=0.0):
    """Безопасный float для весов предметов стартового состояния сюжета."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def apply_plot_start(setting: dict, plot: dict) -> list[str]:
    """Применяет starting_state сюжета (схема PLOTS.md) к состоянию мира при создании.
    Локации/NPC/магазины/фракции/квесты/флаги/время/погода/золото/инвентарь игрока.
    Роль игрока (раса/класс/статы/уровень) НЕ трогает — её генерирует generate_character,
    т.к. сюжет не задаёт жёсткую личность (см. PLOTS.md п.5.1).
    Шаблон полей совпадает с тем, как движок хранит их в setting (directives)."""
    if not isinstance(plot, dict):
        return []
    ss = plot.get("starting_state") or {}
    meta = plot.get("metadata") or {}
    story = plot.get("story") or {}
    if not isinstance(ss, dict) or not isinstance(story, dict):
        return []
    p = setting.setdefault("player", {})

    # Золото и инвентарь игрока (канонический старт из сюжета)
    if ss.get("gold") is not None:
        _g = _pl_int(ss.get("gold"), None)
        if _g is not None:
            p["gold"] = _g
    inv = ss.get("inventory")
    if isinstance(inv, list) and inv:
        clean = []
        for it in inv:
            if isinstance(it, str) and it.strip():
                clean.append({"name": it.strip()[:60], "qty": 1, "desc": ""})
            elif isinstance(it, dict) and str(it.get("name") or "").strip():
                clean.append({"name": str(it["name"]).strip()[:60],
                              "qty": max(1, _pl_int(it.get("qty"), 1)),
                              "desc": str(it.get("desc") or "").strip()[:300],
                              "weight": _pl_float(it.get("weight"))})
        if clean:
            p["inventory"] = clean

    # Время и погода
    if str(ss.get("start_time") or "").strip():
        setting["time"] = str(ss["start_time"]).strip().lower()
    if str(ss.get("start_weather") or "").strip():
        setting["weather"] = str(ss["start_weather"]).strip().lower()

    # Локации (граф карты) + текущая
    locs = {}
    for _loc in (ss.get("locations") or []):
        if isinstance(_loc, dict) and str(_loc.get("id") or "").strip():
            lid = str(_loc["id"]).strip()
            _st = _loc.get("stations")
            if isinstance(_st, str):
                _stations = [x.strip() for x in _st.split(",") if x.strip()]
            else:
                _stations = [str(x) for x in (_st or []) if str(x).strip()]
            _co = _loc.get("connections")
            if isinstance(_co, str):
                _conns = [x.strip() for x in _co.split(",") if x.strip()]
            else:
                _conns = [str(x) for x in (_co or []) if str(x).strip()]
            locs[lid] = {"name": str(_loc.get("name") or lid)[:80],
                         "desc": str(_loc.get("desc") or ""),
                         "connections": _conns,
                         "stations": _stations}
    if locs:
        sid = (meta.get("start_location_id") or "").strip()
        cur = sid if sid in locs else next(iter(locs))
        setting["locations"] = locs
        setting["current_location"] = cur

    # NPC
    for n in (ss.get("npcs") or []):
        if isinstance(n, dict) and str(n.get("id") or "").strip():
            nid = str(n["id"]).strip()
            entry = {"name": str(n.get("name") or nid)[:60], "mood": str(n.get("mood") or ""),
                     "alive": bool(n.get("alive", True)), "desc": str(n.get("desc") or ""),
                     "faction": str(n.get("faction") or "")}
            sch = n.get("schedule")
            if isinstance(sch, dict):
                entry["schedule"] = {str(k): str(v) for k, v in sch.items() if str(v).strip()}
            if n.get("money") is not None:
                entry["money"] = max(0, _pl_int(n.get("money")))
            setting.setdefault("npc", {})[nid] = entry

    # Магазины (структура как у shop_add)
    shops = setting.setdefault("shops", {})
    for s in (ss.get("shops") or []):
        if isinstance(s, dict) and str(s.get("id") or "").strip():
            sid = str(s["id"]).strip()
            items = []
            for it in (s.get("items") or []):
                if isinstance(it, dict) and str(it.get("name") or "").strip():
                    items.append({"name": str(it["name"]).strip()[:60],
                                  "price": _pl_int(it.get("price")),
                                  "qty": max(1, _pl_int(it.get("qty"), 1)),
                                  "value": _pl_int(it.get("value")),
                                  "weight": _pl_float(it.get("weight")),
                                  "desc": str(it.get("desc") or "")})
            shops[sid] = {"id": sid, "name": str(s.get("name") or sid), "owner": str(s.get("owner") or ""),
                          "faction": str(s.get("faction") or ""), "location": str(s.get("location") or ""),
                          "items": items}

    # Фракции (связи)
    factions = setting.setdefault("factions", {})
    for f in (plot.get("factions") or []):
        if isinstance(f, dict) and str(f.get("id") or "").strip():
            fid = str(f["id"]).strip()
            entry = {"name": str(f.get("name") or fid), "desc": str(f.get("desc") or ""),
                     "alignment": str(f.get("alignment") or "")}
            rels = f.get("relations")
            if isinstance(rels, dict):
                entry["relations"] = {str(k): str(v) for k, v in rels.items()}
            factions[fid] = entry

    # Квесты (активные из цепочек)
    for q in (story.get("quest_chains") or []):
        if isinstance(q, dict) and str(q.get("id") or "").strip():
            qid = str(q["id"]).strip()
            entry = {"title": str(q.get("title") or qid), "desc": str(q.get("desc") or ""),
                     "status": str(q.get("status") or "active")}
            for k in ("steps", "branches", "next_quest_id"):
                if q.get(k) is not None:
                    entry[k] = q[k]
            setting.setdefault("quests", {})[qid] = entry

    # Флаги
    flags = ss.get("flags")
    if isinstance(flags, dict):
        for k, v in flags.items():
            # п.12 (сессия 40): сюжет может задать и человекочитаемое название флага —
            # тогда значение пишется как {"value": true, "title": "Дверь открыта"}
            # (старый вид «ключ: true» остаётся валидным).
            if isinstance(v, dict):
                setting.setdefault("flags", {})[str(k)] = v.get("value", True)
                t = str(v.get("title") or "").strip()[:120]
                if t:
                    setting.setdefault("flag_titles", {})[str(k)] = t
            else:
                setting.setdefault("flags", {})[str(k)] = bool(v) if isinstance(v, bool) else v
    return []


DIFF_LABELS = {"easy": "лёгкая", "normal": "средняя", "hardcore": "хардкор"}


def diff_label(difficulty: str) -> str:
    return DIFF_LABELS.get(difficulty, difficulty)


# ── Жанры: мир может иметь несколько жанров (жанр в worlds.genre — строка через запятую) ──
def split_genres(genre: str) -> list[str]:
    """Разбивает строку жанров («а, б; в») на список нормализованных жанров."""
    return [g.strip().lower() for g in re.split(r"[,;]", genre or "") if g.strip()]


def genre_hint_text(genre: str) -> str:
    """Жанровые правила для всех выбранных жанров мира (совмещённые)."""
    hints = [GENRE_HINTS.get(g, "") for g in split_genres(genre)]
    return " ".join(h for h in hints if h)


# ── Размер контекста мира (per-world): чем больше — тем точнее память ──
CONTEXT_OVERHEAD = 2600   # ЗАПАС, если реальный размер системного промпта ещё не измерен
MIN_RECENT_BUDGET = 400
# База шкалы масштабирования памяти/лора: СТАНДАРТ 32k (как в .env CONTEXT_TOKENS=32768).
# Не используем get_config().context_tokens, потому что админка может поставить глобальный
# контекст 262144 — тогда формула «контекст/база» схлопывалась к 1.0 и память не росла.
CONTEXT_BASE_TOKENS = 32768
# Резерв токенов под RAG-память + лор + сводки + карточки (поверх recent).
# (сессия 34, A2) Раньше это была плоская константа MEMORY_EXTRA_BUDGET=16384 — и при
# локальном окне 8192 она была БОЛЬШЕ всего контекста: recent всегда падал на пол 400,
# а суммарный промпт с системными ~6200 токенов всё равно вылезал за n_ctx (молчаливый
# обрез). Теперь резерв = доля окна, но не больше MAX_MEMORY_EXTRA_BUDGET.
MEMORY_EXTRA_RATIO = 0.2
MAX_MEMORY_EXTRA_BUDGET = 16384   # достаточно для МАКСИМАЛЬНОГО RAG/лора при 262k
MEMORY_EXTRA_BUDGET = MAX_MEMORY_EXTRA_BUDGET   # обратная совместимость (тесты/скрипты)


def world_gen_settings(world: dict) -> dict:
    """gen_settings мира как dict (переживают и JSON-строку, и dict).

    A9 (аудит 41): значения проходят общий кламп (`config.clamp_gen_settings`) ПРИ ЧТЕНИИ.
    Причина не в UI: кривое число (`max_tokens: 0`, `temperature: 1e9`) могло попасть в мир
    из старого сохранения, ручной правки БД или импортированного дампа — и тогда ломался
    КАЖДЫЙ следующий ход (ошибка модели) или бюджет окна уезжал в минус. Ниже этого места
    настройки читают и `_gen_params`, и бюджеты памяти (`world_recent_budget` и др.), так что
    кламп здесь закрывает все пути."""
    try:
        g = world.get("gen_settings") or "{}"
        g = g if isinstance(g, dict) else json.loads(g)
    except Exception:
        g = {}
    if not isinstance(g, dict):
        g = {}
    return clamp_gen_settings(g, where=f"мир {world.get('id')}")


def world_context_tokens(world: dict) -> int:
    """Размер контекста мира (используется для бюджета памяти)."""
    g = world_gen_settings(world)
    ctx = int(g.get("context_tokens") or 0)
    return ctx if ctx > 0 else get_config().context_tokens


def memory_extra_budget(world: dict) -> int:
    """Резерв окна под память (RAG/лор/сводки/карточки): доля контекста, но не больше
    MAX_MEMORY_EXTRA_BUDGET. При локальном n_ctx 8192 это ~1.6k вместо прежних 16k."""
    ctx = world_context_tokens(world)
    return max(512, min(MAX_MEMORY_EXTRA_BUDGET, int(ctx * MEMORY_EXTRA_RATIO)))


def world_prompt_overhead(world: dict, setting: dict | None = None,
                          prompt_tokens: int | None = None) -> int:
    """Сколько окно съедают системный промпт + max_tokens ответа.

    A2: оверхед берётся ИЗМЕРЕННЫЙ (т.ч. реальный размер промпта этого мира), а не из
    догадки. Порядок приоритета:
      1) prompt_tokens — передан вызывающим (ядро хода уже считает его для метрик);
      2) setting["_ctx_prompt_tokens"] — измерение с последнего хода этого мира;
      3) CONTEXT_OVERHEAD — запас для нового мира/старых сохранений (после первого хода
         само-корректируется).
    """
    g = world_gen_settings(world)
    mtok = int(g.get("max_tokens") or get_config().max_tokens)
    pt = prompt_tokens
    if not pt:
        try:
            st = setting if isinstance(setting, dict) else json.loads(world.get("setting") or "{}")
            pt = int(st.get("_ctx_prompt_tokens") or 0)
        except Exception:
            pt = 0
    return max(CONTEXT_OVERHEAD, int(pt or 0)) + mtok


def world_recent_budget(world: dict, prompt_tokens: int | None = None) -> int:
    """Сколько токенов мира уходит под недавнюю историю.

    total ≈ context_tokens = (системный промпт + ответ) + recent + резерв на память.
    A2 (сессия 34): раньше из окна вычиталась плоская догадка CONTEXT_OVERHEAD=2600 при
    реальном промпте ~6200 токенов и резерв 16384 — больше всего локального окна. Теперь
    оверхед измеряется, резерв — доля окна.
    """
    ctx = world_context_tokens(world)
    return max(MIN_RECENT_BUDGET,
               ctx - world_prompt_overhead(world, prompt_tokens=prompt_tokens)
               - memory_extra_budget(world))


def dynamic_memory_k(world: dict, base_k: int, max_k: int) -> int:
    """Масштабирует количество RAG-фактов/лор-чанков от размера контекста мира.
    Базовая шкала — 32k (СТАНДАРТ, константа CONTEXT_BASE_TOKENS, НЕ cfg.context_tokens):
    при контексте 32k возвращаем base_k, при 128k/256k — пропорционально больше
    (но не больше max_k), чтобы окно памяти реально использовалось.
    Per-world параметр (gen_settings.rag_memory_k / lore_rag_k) переопределяет базу;
    0 = авто."""
    g = world_gen_settings(world)
    base = int(g.get("rag_memory_k") or 0) if g.get("rag_memory_k") is not None else 0
    if base <= 0:
        base = base_k
    ctx = world_context_tokens(world)
    base_ctx = max(8192, CONTEXT_BASE_TOKENS)
    scale = max(1.0, ctx / base_ctx)
    k = int(round(base * scale))
    return max(1, min(int(max_k), max(k, base)))


def dynamic_lore_budget(world: dict, base_budget: int, max_budget: int) -> int:
    """Бюджет токенов лора тоже растёт с контекстом (не фиксированные 900 при 256k).
    Per-world параметр gen_settings.lore_token_budget переопределяет базу; 0 = авто."""
    g = world_gen_settings(world)
    base = int(g.get("lore_token_budget") or 0) if g.get("lore_token_budget") is not None else 0
    if base <= 0:
        base = base_budget
    ctx = world_context_tokens(world)
    base_ctx = max(8192, CONTEXT_BASE_TOKENS)
    scale = max(1.0, ctx / base_ctx)
    b = int(round(base * scale))
    return max(base, min(int(max_budget), b))


# ══════════════════════════════════════════════════════════════
# Начальное состояние мира
# ══════════════════════════════════════════════════════════════
def default_setting(theme: dict, difficulty: str = "normal") -> dict:
    hp_map = {"easy": 120, "normal": 100, "hardcore": 80}
    starter = theme.get("starter", {})
    p = {
        "name": "Путник",
        "hp": hp_map.get(difficulty, 100),
        "max_hp": hp_map.get(difficulty, 100),
        "mp": 50, "max_mp": 50,
        "level": 1, "xp": 0,
        "gold": starter.get("gold", 20),
        "race": "",              # раса — назначит рассказчик (или задай директивой)
        "class": "",             # класс (например «Воин») и его эволюции/специализация
        "class_rank": "F",       # ранг класса
        "secondary_class": "",   # мультикласс (опц.)
        "secondary_rank": "F",
        "profession": "",        # профессия/ремесло (даёт бафф)
        "stats": dict(DEFAULT_STATS),
        "skills": {},             # навык → {rank, kind, desc, mp_cost}
        "titles": [],
        "reputation": {},
        "effects": {},            # эффект → {turns, damage, heal, kind, stacks, mods, desc, tag}
        "actions": {},            # накопление действий: {действие: счётчик} → смена профессии
        "identity": "",           # краткое описание персонажа (кто он, происхождение), задаётся при/в начале
        "inventory": list(starter.get("inventory", [])),
    }
    recalc_derived(p, difficulty)
    # Стартовая локация: имя по теме (или настраиваемая start_location в теме — чтобы локация
    # не называлась именем мира/сюжета, а была реальным местом действия).
    start = theme.get("start_location")
    if isinstance(start, dict):
        start_loc = {"name": str(start.get("name") or theme["name"]),
                     "desc": str(start.get("desc") or theme.get("desc", ""))}
    elif isinstance(start, str) and start.strip():
        start_loc = {"name": start.strip(), "desc": theme.get("desc", "")}
    else:
        start_loc = {"name": theme["name"], "desc": theme.get("desc", "")}
    return {
        "player": p,
        "enemies": {},
        "npc": {},
        "factions": {},       # фракции: id → {name, desc, alignment, relations:{other: stance}}
        "companions": {},
        "locations": {"start": start_loc},
        "current_location": "start",
        "quests": {},
        "flags": {},
        # п.12: машинный ключ флага → человекочитаемое название (водит рассказчик/сюжет)
        "flag_titles": {},
        "weather": "ясно",
        "time": "вечер",
        "game_over": False,
        "_difficulty": difficulty,
        "style_notes": [],
    }


# ══════════════════════════════════════════════════════════════
# Влияние среды (время суток + погода) — универсальные геймплейные эффекты,
# не привязаны к фэнтези: работают и в реал/техно/космос мирах.
# ══════════════════════════════════════════════════════════════
# Время суток: ключ — что слово в директиве time (ночь/день/вечер/утро и т.д.)
_TIME_MODS = {
    "ночь": ["−1 к зрительным проверкам и дальнему бою, +1 к скрытности"],
    "глубокая ночь": ["−2 к зрительным проверкам, +2 к скрытности"],
    "вечер": ["−1 к зрительным проверкам вдали"],
    "сумерки": ["−1 к зрительным проверкам вдали"],
    "рассвет": ["−1 к вниманию (полусон)"],
    "утро": [],
    "день": [],
    "полдень": [],
}
# Погода: влияние на проверки/передвижение.
_WEATHER_MODS = {
    "дождь": ["−1 к зрительным/слуховым проверкам на расстоянии, мокрая земля (−1 к скорости)"],
    "ливень": ["−2 к зрительным/слуховым, тяжело двигаться", "плохая видимость"],
    "гроза": ["−2 к зрительным/слуховым, опасно на открытой местности", "могут ломаться мосты/переправы"],
    "буря": ["−2 к проверкам, передвижение затруднено", "риск обрушения/затопления"],
    "шторм": ["−3 ко внешним проверкам", "выход в море/открытое небо крайне опасен"],
    "туман": ["−2 к видимости, легко заблудиться"],
    "снегопад": ["−1 к видимости и скорости, следы заметны"],
    "метель": ["−2 к видимости, передвижение затруднено", "риск сбиться с пути"],
    "жара": ["−1 к выносливости при долгих усилиях"],
    "мороз": ["−1 к выносливости и ловкости на холоде", "вода замерзает"],
    "пыльная буря": ["−2 к видимости, трудно дышать"],
    "смог": ["−1 к видимости на расстоянии"],
    "радиация": ["опасно долго находиться без защиты"],
    "солнечный шторм": ["−2 к электронике/связи"],
    "кислотный дождь": ["повреждает открытую технику/броню"],
}
# Сезон (сессия 32): зима/лето/осень/весна дают универсальные моды (все жанры)
_SEASON_MODS = {
    "зим": ["−передвижение в пути", "холод: нужна тёплая одежда/огонь", "−видимость (снег/туман)"],
    "весн": ["+скрытность (листва)", "+ресурсы (ягоды/добыча)", "грязь: −скорость по бездорожью"],
    "лет": ["+жара: −выносливость в пути днём", "+долгий световой день", "−скрытность (открытые пространства)"],
    "осен": ["+туманы и сырость", "−видимость в сумерках", "+ресурсы (урожай/грибы)"],
}


def environment_mods(time_of_day: str, weather: str, season: str = "") -> list[str]:
    """Список геймплейных эффектов от времени суток, погоды и сезона (для состояния в промпте).
    Ключи-подстроки сверяются от САМОЙ СПЕЦИФИЧНОЙ записи к общей (по убыванию длины),
    чтобы «глубокая ночь» не получала эффекты «ночь», «пыльная буря» — «бури» и т.п."""
    out: list[str] = []
    td = str(time_of_day or "").strip().lower()
    for t in sorted(_TIME_MODS, key=len, reverse=True):
        if t in td:
            out.extend(_TIME_MODS[t])
            break
    w = str(weather or "").strip().lower()
    if "ясно" not in w and "ясная" not in w:
        for tag in sorted(_WEATHER_MODS, key=len, reverse=True):
            if tag in w:
                out.extend(_WEATHER_MODS[tag])
                break
    # Сезон (сессия 32): зима/лето/осень/весна дают универсальные моды (все жанры)
    s = str(season or "").strip().lower()
    for tag in sorted(_SEASON_MODS, key=len, reverse=True):
        if tag in s:
            out.extend(_SEASON_MODS[tag])
            break
    return out

def _notes_text(notes: Any) -> str:
    """C5: заметки мастера о NPC (dict/str/список) в одну строку; '' если пусто."""
    if not notes:
        return ""
    if isinstance(notes, str):
        return notes.strip()[:200]
    if isinstance(notes, list):
        return "; ".join(str(x).strip()[:100] for x in notes[:4] if x)
    if isinstance(notes, dict):
        return "; ".join(f"{k}: {str(v).strip()[:120]}" for k, v in list(notes.items())[:5] if v)
    return ""


def npc_schedule_text(setting: dict, npc_id: str, npc: dict) -> str:
    """Активная запись расписания NPC под текущее время: "расписание: …" или "" (нет)."""
    sch = npc.get("schedule")
    if not isinstance(sch, dict) or not sch:
        return ""
    if not npc.get("alive", True):
        return ""  # мёртвый не по расписанию
    td = str(setting.get("time", "") or "").strip().lower()
    for t, act in sch.items():
        k = str(t).strip().lower()
        if k in td or td in k:
            return f" (расписание: {act})"
    return ""


# ══════════════════════════════════════════════════════════════
# Форматирование состояния мира (компактно, для промпта)
# ══════════════════════════════════════════════════════════════
def _safe_ifint(v, default=0):
    """Безопасный int для отображательных полей (цена/ценность)."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _w0(v) -> str:
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return "0"


def format_state(setting: dict) -> str:
    p = setting["player"]
    # Сессия 40, п.2: помечаем предметы БЕЗ описания — иначе модель не видит, что карточка
    # пустая, и не закреплёт раскрытые позже свойства директивой item_update.
    inv = ", ".join(f"{i['name']} ×{i.get('qty', 1)}" + ("" if str(i.get('desc') or '').strip() else " (описание не известно)")
                    for i in p.get("inventory", [])) or "пусто"
    loc = setting.get("locations", {}).get(setting.get("current_location", "start"), {})

    lines = [
        "=== ТЕКУЩЕЕ СОСТОЯНИЕ МИРА ===",
        f"Игрок: {p.get('name','Путник')} | HP {p.get('hp',0)}/{p.get('max_hp',0)} | MP {p.get('mp',0)}/{p.get('max_mp',0)}",
        f"Уровень {p.get('level',1)} (XP {p.get('xp',0)}) | Золото: {p.get('gold',0)}",
    ]
    # Динамическая сложность: показываем рассказчику множители силы врагов под текущий
    # уровень, чтобы он задавал БАЗОВУЮ силу (движок сам усилит HP/урон) и описывал врагов
    # соответствующе (сильный сюжетно должен быть и в тексте, а не только в цифрах).
    dsc = dynamic_adversary_scale(p, setting.get("_difficulty", "normal"))
    lines.append(
        f"⚖️ Сложность: {diff_label(setting.get('_difficulty','normal'))} | уровень игрока {p.get('level',1)} → "
        f"враги HP×{dsc['hp']}, dmg×{dsc['dmg']}, события ×{dsc['event']}. "
        "Задавай базовые HP/урон врагов (движок сам отмасштабирует); "
        "сильные враги должны подкрепляться описанием/сюжетом.")
    ident = (p.get("identity") or "").strip()
    if ident:
        lines.insert(1, f"Персонаж (кто ты): {ident[:800]}")
    race = (p.get("race") or "").strip()
    cls = (p.get("class") or "").strip()
    prof = (p.get("profession") or "").strip()
    sec = (p.get("secondary_class") or "").strip()
    role_parts = []
    if race:
        role_parts.append(f"раса: {race}")
    if cls:
        role_parts.append(f"класс: {cls} (ранг {p.get('class_rank','F')})")
    if sec:
        role_parts.append(f"мультикласс: {sec} (ранг {p.get('secondary_rank','F')})")
    if prof:
        role_parts.append(f"профессия: {prof}")
    if role_parts:
        lines.append("Роль: " + "; ".join(role_parts))
    st = p.get("stats", {})
    eff = effective_stats(p)
    has_mods = any(eff.get(k, 0) != int(st.get(k, 0) or 0) for k in (st or {}))
    prefix = "Характеристики (с учётом эффектов/экипировки): " if has_mods else "Характеристики: "
    lines.append(prefix + ", ".join(f"{k} {eff.get(k, v)}" for k, v in st.items()))
    skills = p.get("skills") or {}
    if skills:
        parts = []
        for k, sk in list(skills.items())[:14]:
            if isinstance(sk, dict):
                r = str(sk.get("rank", "F"))
                parts.append(f"{k} ({r}" + (f", {sk.get('kind')}" if sk.get("kind") else "") + ")")
            else:
                parts.append(f"{k} ур.{sk}")
        lines.append("Навыки: " + "; ".join(parts))
    abilities = p.get("abilities") or {}
    if abilities:
        ap = []
        for nm, ab in list(abilities.items())[:10]:
            if isinstance(ab, dict):
                school = f"[{ab.get('school')}]" if ab.get("school") else ""
                cost = f" (энергия {ab.get('cost')})" if ab.get("cost") else ""
                ap.append(f"{nm}{school}{cost}")
            else:
                ap.append(str(nm))
        lines.append("Способности: " + "; ".join(ap))
    progress = p.get("progress") or {}
    if progress:
        lines.append("Статистика пути: " + "; ".join(f"{k} {v}" for k, v in list(progress.items())[:12]))
    achievements = p.get("achievements") or []
    if achievements:
        lines.append("🏆 Достижения: " + "; ".join(
            str(a.get("name") or a) if isinstance(a, dict) else str(a)
            for a in achievements[:10]))
    titles = p.get("titles") or []
    if titles:
        lines.append("Титулы: " + ", ".join(str(t) for t in titles[:8]))
    factions = setting.get("factions") or {}
    rep = p.get("reputation") or {}
    if rep or factions:
        rep_parts = [f"{k} — {reputation_standing(v)} ({v:+d})" for k, v in list(rep.items())[:8]] if rep \
            else ([str(x) for x in list(factions)][:8] if factions else [])
        lines.append("Репутация/фракции: " + "; ".join(rep_parts))
        fl = []
        for rid, f in list(factions.items())[:10]:
            nm = f.get("name", rid)
            rels = f.get("relations") or {}
            rel_s = "; ".join(f"{o}:{s}" for o, s in list(rels.items())[:4]) if rels else "без связей"
            st = reputation_standing(faction_rep_value(p, rid))
            fl.append(f"{nm} [{st}, связи: {rel_s}]")
        if fl:
            lines.append("Фракции (роль для рассказчика): " + "; ".join(fl))
    effects = p.get("effects") or {}
    if effects:
        parts = []
        # п.8 (сессия 40): имена-синонимы одного состояния. Разовая системка о дубле
        # могла уйти в историю, поэтому пометку «имя близко» мастер видит каждый ход.
        dup = {str(n) for n in effects if find_similar_effect(p, str(n))}
        for name, ef in list(effects.items())[:8]:
            label = str(name).strip()
            if re.fullmatch(r"[A-Za-z0-9_\-]+", label):
                label = (label.replace("_", " ").strip().title() or label)[:60]
            t = ef.get("turns", -1)
            dur = "постоянно" if t in (-1, None) else f"{int(t)} ход."
            dmg = int(ef.get("damage", 0) or 0)
            heal = int(ef.get("heal", 0) or 0)
            mpd = int(ef.get("mp_damage", 0) or 0)
            mph = int(ef.get("mp_heal", 0) or 0)
            tick_txt = f", −{dmg} HP/ход" if dmg else (f", +{heal} HP/ход" if heal else "")
            if mpd:
                tick_txt += f", −{mpd} MP/ход"
            elif mph:
                tick_txt += f", +{mph} MP/ход"
            stk = int(ef.get("stacks", 1) or 1)
            if stk > 1:
                tick_txt += f", стаков {stk}"
            mods = ef.get("mods") or {}
            if mods and isinstance(mods, dict):
                tick_txt += ", моды: " + ", ".join(f"{k}{v:+}" for k, v in mods.items())
            kind = str(ef.get("kind") or "").strip()
            # D5: НЕ dsc — выше по функции под этим именем живёт dict масштабирования
            # сложности; прежнее затирание имени мюпи ловил как «str в dict».
            edesc = str(ef.get("desc") or "").strip()
            # П.7 (сессия 40): desc_compact, а не edesc[:70] — срок/условие снятия эффекта
            # почти всегда во ВТОРОЙ половине фразы, и при жёстком обрезе мастер их не
            # видел: перекладывал «Требуется ещё 1-2 сессии медитации» как бессрочный.
            # П.7 (сессия 40): ОТПРАВИТЬ desc ЦЕЛИКОМ (без лимита). Условие снятия/срок
            # почти всегда во второй половине фразы: при обрезе мастер их не видел и
            # перекладывал «Требуется ещё 1-2 сессии медитации» как бессрочный эффект.
            desc_txt = f" — {desc_compact(edesc)}" if edesc else ""
            # бессрочный эффект со сроком в описании помечается: мастер видит противоречие
            # КАЖДЫЙ ход (а не один раз в момент наложения — системка могла уйти в историю)
            if effect_needs_turns(ef):
                # эффект висит бессрочно, а срок в тексте назван — напоминание мастеру
                # каждый ход (системка о наложении могла уйти в историю)
                dur += " ⚠в описании срок: задай turns или сними effect_remove"
            base = f"{label}{desc_txt}" + (" ⚠возможен дубль имени (ср. другой эффект: обнови прежний или сними effect_remove)" if str(name) in dup else "")
            parts.append(f"{base} ({kind}, {dur}{tick_txt})" if kind else f"{base} ({dur}{tick_txt})")
        lines.append("Эффекты: " + "; ".join(parts))
    acts = p.get("actions") or {}
    if acts:
        prog = []
        for act, c in list(acts.items())[:10]:
            prof = PROF_ACTION_MAP.get(str(act).strip().lower())
            if prof:
                pname, thr = prof
                prog.append(f"{act} {c}/{thr} → {pname}" if c < thr else f"{act} {c}/{thr} (порог взят → {pname})")
            else:
                prog.append(f"{act} {c}")
        lines.append("Действия (прогресс профессий): " + "; ".join(prog))
    lines.append("Инвентарь: " + inv)
    # ═══ Сессия 32: экипировка (слоты) ═══
    equipped = p.get("equipped") or {}
    if equipped:
        eq_parts = []
        for slot, item_name in equipped.items():
            it = next((x for x in p.get("inventory", []) if x.get("name") == item_name), None)
            bn = ""
            if isinstance(it, dict):
                b = it.get("bonus")
                if isinstance(b, dict):
                    bn = " (" + ", ".join(f"{k}{v:+}" for k, v in b.items()) + ")"
            eq_parts.append(f"{slot}: {item_name}{bn}")
        lines.append("🛡 Экипировано: " + "; ".join(eq_parts))
    # ═══ Сессия 32: потребности и рассудок ═══
    needs = p.get("needs") or {}
    mental = p.get("mental") or {}
    if needs:
        lines.append("Потребности: " + "; ".join(f"{k} {float(v.get('value', 0)):.0f}/{float(v.get('max', 100)):.0f}" for k, v in needs.items() if isinstance(v, dict)))
    if mental:
        lines.append("Рассадок/мораль: " + "; ".join(f"{k} {float(v.get('value', 0)):.0f}/{float(v.get('max', 100)):.0f}" for k, v in mental.items() if isinstance(v, dict)))
    # ═══ Сессия 32: звания во фракциях ═══
    fra = p.get("faction_ranks") or {}
    if fra:
        lines.append("🏅 Звания: " + "; ".join(f"{k}: {v}" for k, v in fra.items()))
    # ═══ Сессия 32: таймеры мира (дедлайны) ═══
    timers = setting.get("timers") or {}
    if isinstance(timers, dict) and timers:
        t_parts = []
        for name, t in list(timers.items())[:8]:
            if not isinstance(t, dict):
                continue
            tl = t.get("turns_left")
            dur = "без срока" if tl in (-1, None, "∞") else f"{int(tl)} ход."
            t_parts.append(f"{name} ({dur})" + (f" — {t.get('desc')}" if t.get("desc") else ""))
        lines.append("⏳ Таймеры мира: " + "; ".join(t_parts))
    # ═══ Сессия 32: доска объявлений ═══
    _board = board_text(setting, limit=5)
    if _board:
        lines.append("📜 Доска объявлений:\n  " + "\n  ".join(_board.splitlines()[:5]))
    # ═══ Сессия 63 («живой мир»): счётчик хода и «куда мир уже свернул с канвы» ═══
    # Оба — чистое отображение (закон 2). Счётчик нужен как ОРИЕНТИР частоты глобальных
    # сдвигов (правило 37): без него модель не знает, сколько идёт игра, и либо топчет мир
    # поворотом каждый ход, либо не трогает его никогда. Список отходОв — чтобы через 30
    # ходов не «реанимировать» арку, от которой мастер уже отказался.
    _turns = setting.get("_player_turns")
    _dev_txt = plot_deviation_text(setting)
    if isinstance(_turns, int) or _dev_txt:
        _ev: list[str] = [f"ход {int(_turns)}" if isinstance(_turns, int) else "ход —"]
        _arcs = [q.get("title", k) for k, q in (setting.get("quests") or {}).items()
                 if isinstance(q, dict) and q.get("status") == "active"]
        if _arcs:
            _ev.append("активные арки: " + "; ".join(_arcs[:8]))
        lines.append("🧭 ЖИВОЙ МИР (" + "; ".join(_ev) + ")")
        if _dev_txt:
            lines.append("🧭 Мир уже отходил от канвы сюжета (это РЕАЛЬНОСТЬ, канва — нет; "
                         "не возвращай игрока в отброшенное и не противоречь этим записям):\n  "
                         + "\n  ".join(_dev_txt.splitlines()))
    # ═══ Сессия 34 (C9): «ружья Чехова» — что введено и с тех пор не звучало ═══
    # Только подсказка-отображение (закон 2): «выстрелит» намёк или нет, когда и как —
    # решение рассказчика (закон 3).
    try:
        from . import journal as _jr
        _guns = _jr.chekhov_text(setting)
        if _guns:
            lines.append("🏹 На горизонте (введено и ждёт своего часа — верни это в сюжет,"
                         " если это уместно, или забудь): " + _guns)
    except Exception:
        log.debug("ружья Чехова не показаны в состоянии", exc_info=True)
    # ═══ Сессия 32: очередь видений (сыграют при отдыхе/сне/trigger_vision) ═══
    _pv = setting.get("pending_visions") or []
    if isinstance(_pv, list) and _pv:
        _pv_txt = "; ".join(
            (str(v.get("hint") or v.get("text") or "")[:80] if isinstance(v, dict) else str(v)[:80])
            for v in _pv[:3])
        lines.append(f"🌙 Очередь видений ({len(_pv)}): {_pv_txt}")
    # Система веса: показываем загрузку рюкзака, если есть предметы с весом
    _invw = inventory_weight(p)
    _cap = carry_capacity(p)
    if _invw:
        _pct = f" ({(100*_invw/_cap):.0f}%" + (" — ПЕРЕГРУЗ, не бери больше!" if _invw > _cap else ")")
        lines.append(f"🎒 Загрузка: {_invw:.0f}/{_cap} кг{_pct}")
    _sell = total_sell_value(p)
    if _sell:
        lines.append(f"💰 Продажу/сделки: всё лишнее в инвентаре оценивается ≈ {_sell} 🪙 (можно продать в магазине).")
    _stations = location_stations(setting)
    if _stations:
        lines.append("🔧 Станции здесь (для крафта): " + ", ".join(_stations))
    lines.append(f"Локация: {loc.get('name','?')} — {loc.get('desc','')[:200]}")
    # ═══ Сессия 32: локации-зоны с постоянными эффектами (радиация/туман/проклятие) ═══
    zone_fx = location_effects_for(setting, setting.get("current_location", "start"))
    if zone_fx:
        z_parts = []
        for fx in zone_fx:
            if isinstance(fx, dict) and fx.get("name"):
                dmg = int(fx.get("damage", 0) or 0)
                z_parts.append(f"{fx['name']}" + (f" (−{dmg} HP/ход)" if dmg else "") + (f" — {fx.get('desc')}" if fx.get("desc") else ""))
        lines.append("🌫 Влияние места (зоны): " + "; ".join(z_parts[:6]))
    lines.append(f"Погода: {setting.get('weather','ясно')} | Время суток: {setting.get('time','')}")
    # п.14 (сессия 40, мир «Новый мир»): «весь текст про туман». Погода ставится один раз
    # при создании мира (сюжетный start_weather) и дальше двигается ТОЛЬКО директивой
    # weather. Пока её не двигали, строка выше — якорь: модель каждый ход начинает с
    # «Туман …» и ответ превращается в одно и то же. Код погоду не меняет (закон 3), он
    # показывает возраст среды и напоминает, что её можно сдвинуть. Время суток с п.14b
    # ведёт авто-тик (tick_time), поэтому оно в этом предупреждении не нуждается.
    try:
        _wx_from = setting.get("_weather_last_turn", setting.get("_env_last_turn", 0))
        _env_age = int(setting.get("_player_turns", 0) or 0) - int(_wx_from or 0)
        if _env_age >= 3:
            lines.append(f"⚠ Погода не менялась {_env_age} ходов — не тащи её через весь текст: "
                         "сдвинь weather или пиши сцену без погоды.")
    except (TypeError, ValueError):
        pass
    _date = setting.get("date") or {}
    if isinstance(_date, dict) and _date:
        _dp = ", ".join(f"{k}: {v}" for k, v in _date.items() if v)
        lines.append(f"📅 Дата/сезон: {_dp}")
    # Влияние среды (время суток + погода + сезон) — геймплейные эффекты.
    env_effects = environment_mods(setting.get("time", ""), setting.get("weather", ""),
                                   season=(setting.get("date") or {}).get("season", "") if isinstance(setting.get("date"), dict) else "")
    if env_effects:
        lines.append("Влияние среды (учитывай в проверках/поведении): " + "; ".join(env_effects))
    npc = setting.get("npc", {})
    if npc:
        # Сессия 40, п.13: «player = Игрок» — не персонаж окружения (в мире №103 модель
        # заводила игрока как NPC, и тот стоял в одном списке с торговцем). Показ
        # не зависит от само-исцеления: своё состояние игрок видит строкой «Игрок: …» выше.
        _here = str(setting.get("current_location") or "")
        npc_items = [(k, v) for k, v in npc.items()
                     if isinstance(v, dict) and not is_player_npc(k, v)]
        # рядом стоящие — первыми (у NPC может быть поле location: npc_set/карточка)
        npc_items.sort(key=lambda kv: 0 if not str(kv[1].get("location") or "")
                       or str(kv[1].get("location")) == _here else 1)
        npc_lines = []
        for k, v in npc_items[:10]:
            alive = "жив" if v.get('alive', True) else "мёртв"
            extra = npc_schedule_text(setting, k, v)
            _loc = str(v.get("location") or "")
            if _loc and _loc != _here:
                _lname = ((setting.get("locations") or {}).get(_loc) or {}).get("name") or _loc
                extra += f", не рядом: {_lname}"
            frac = ""
            if v.get('faction'):
                fr = str(v['faction'])
                if fr in (rep or {}) or fr in (factions or {}):
                    frac = f", {fr} ({reputation_standing(faction_rep_value(p, fr))})"
                else:
                    frac = f", {fr}"
            _coin = f", {v['money']} 🪙" if v.get("money") else ""
            npc_lines.append(f"{k}={v.get('name',k)}({alive}{_coin}, {v.get('mood','')}{frac}){extra}")
        if npc_lines:
            lines.append("NPC (окружение, не ты сам): " + "; ".join(npc_lines))
        # C5 (сессия 34): «заметки мастера» — что персонаж знает/скрывает/хочет. Ведёт
        # их мастер: подача игроку (намёк, проверка, цена молчания) — его решение.
        note_lines = [f"{v.get('name', k)}: {_notes_text(v.get('notes'))}"
                      for k, v in npc_items[:12] if _notes_text(v.get("notes"))]
        if note_lines:
            lines.append("🗝 Знания и тайны NPC (не вываливай прямо — веди через намёки,"
                         " проверки и поведение по репутации):\n  " + "\n  ".join(note_lines))
    enemies = setting.get("enemies", {})
    if enemies:
        _ai_phrase = {"attack": "преследует/рвётся в бой", "guard": "в обороне/держит дистанцию",
                      "retreat": "готовится отступить", "negotiate": "пытается переговорить/сдаться",
                      "trap": "готовит ловушку/засаду"}
        en_line = []
        for k, v in list(enemies.items())[:8]:
            base = f"{k}={v.get('name',k)} HP {v.get('hp',0)}/{v.get('max_hp',v.get('hp',0))}"
            if v.get("money"):
                base += f" (🪙{v.get('money')})"
            ai = str(v.get("ai") or "").strip().lower()
            if ai in _ai_phrase:
                base += f" (намерение: {_ai_phrase[ai]})"
            # C6: статусы и метки на враге — ПОДСКАЗКА мастеру. Авто-тика по врагам нет:
            # урон/лечение ведит рассказчик директивой enemy_apply (правило 9а, закон 3).
            eefs = v.get("effects")
            if isinstance(eefs, dict) and eefs:
                base += " [статусы: " + ", ".join(
                    f"{n}×{(e or {}).get('stacks', 1)}"
                    + (f" {int((e or {}).get('damage') or 0)}/ход" if (e or {}).get("damage") else "")
                    for n, e in list(eefs.items())[:4]) + "]"
            marks = v.get("marks")
            if isinstance(marks, dict) and marks:
                base += " (" + ", ".join(f"{kk}: {val}" for kk, val in list(marks.items())[:3]
                                         if val) + ")"
            en_line.append(base)
        lines.append("Враги: " + "; ".join(en_line))
        if any(isinstance(v, dict) and (v.get("effects") or v.get("marks"))
               for v in enemies.values()):
            lines.append("⚠ Статусы врагов сами не тикают — урон по врагам применяй"
                         " директивой enemy_apply, когда это происходит по сюжету.")
    companions = setting.get("companions", {})
    if companions:
        comp_lines = []
        for cid, cp in list(companions.items())[:8]:
            sk = "; ".join(f"{s}({v.get('rank','F')})" for s, v in list((cp.get('skills') or {}).items())[:5]) or "—"
            comp_lines.append(f"{cp.get('name', cid)} (Lv{cp.get('level',1)}, HP {cp.get('hp',0)}/{cp.get('max_hp',cp.get('hp',0))}, верность {cp.get('loyalty',0)}, навыки: {sk})")
        lines.append("Компаньоны: " + "; ".join(comp_lines))
    quests = setting.get("quests", {})
    if quests:
        q_lines = []
        for k, v in list(quests.items())[:10]:
            st = v.get("status", "active")
            prog = v.get("progress")
            prog_s = f" | шаг: {prog}" if prog else ""
            # C8: итог квеста (success/failed + причина) и привязанный дедлайн
            if st == "success":
                st = "🏅 выполнен"
            elif st == "failed":
                st = "💀 провален"
            if v.get("outcome_reason"):
                prog_s += f" | итог: {v['outcome_reason']}"
            if v.get("timer"):
                _t = (setting.get("timers") or {}).get(v["timer"])
                if isinstance(_t, dict):
                    prog_s += f" | ⏳ срок: {_t.get('turns_left')} ход."
            q_lines.append(f"[{st}] {v.get('title',k)}{prog_s}: {v.get('desc','')[:120]}")
        lines.append("Квесты:\n  " + "\n  ".join(q_lines))
    shops = setting.get("shops", {})
    if shops:
        shop_lines = []
        for sid, sh in list(shops.items())[:6]:
            items = "; ".join(
                f"{i.get('name')}(п{_safe_ifint(i.get('price'))}🪙/прод.{_safe_ifint(i.get('value'))} ×{i.get('qty')}"
                + (f"·{_w0(i.get('weight'))}кг" if i.get("weight") else "")
                + ")"
                for i in (sh.get('items') or [])[:10]) or "пусто"
            frac = f", фракция: {sh.get('faction')}" if sh.get("faction") else ""
            shop_lines.append(f"{sh.get('name', sid)}{frac} [{items}]")
        lines.append("Магазины: " + " | ".join(shop_lines))
    crafts = setting.get("crafts", {})
    if crafts:
        c_lines = []
        for rid, rc in list(crafts.items())[:8]:
            res = rc.get("result") or {}
            ing = ", ".join(f"{i.get('name')} ×{i.get('qty')}" for i in (rc.get('ingredients') or [])) or "без вложений"
            _req = []
            if rc.get("station"):
                _req.append("станция " + ",".join(rc["station"]))
            if rc.get("profession"):
                _req.append("профессия " + str(rc.get("profession")))
            _req_s = ("; " + ", ".join(_req)) if _req else ""
            _miss, _status = can_craft(setting, rc)
            _rc = "✓" if _status == "ok" else "✗"
            c_lines.append(f"{rc.get('name', rid)} → {res.get('name','?')}×{res.get('qty',1)} [{ing}]{_req_s} {_rc}")
        lines.append("Рецепты крафта: " + " | ".join(c_lines))
    flags = setting.get("flags", {})
    if flags:
        # п.12 (сессия 40): машинный ключ флага — то, что видит игрок в дневнике и в UI.
        # Если мастер/сюжет дали человекочитаемое название (`flag_titles`), показываем его
        # рядом с id: id остаётся (по нему судья сверяет факты), имя — читабельно.
        lines.append("Флаги: " + _flag_line(flags, setting.get("flag_titles"), limit=12))
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════
# Ярусы промпта (сессия 34, B4): отсечь правила о подсистемах, которых в мире нет
# ════════════════════════════════════════════════════════════
#
# Замер: системный промпт рассказчика ≈ 6200 токенов, из них ≈ 4400 — блок «Правила
# рассказчика». На локальной модели (llama.cpp, n_ctx 8192) это окно тратится каждый ход,
# хотя правила про крафт/фракции/таймеры/зоны бесполезны, пока в мире нет ни одного
# объекта такой подсистемы. Здесь тяжёлые правила НЕ ПОДАЮТСЯ, если подсистема не живёт в
# состоянии И действие игрока о ней не просит.
#
# Это только объём контекста, а не решения за мастера (закон 3): возможность обратиться к
# директиве сохраняется (её имя остаётся в «словаре» RULE_VOCAB и в блоке «Механика»), а
# полный текст правила мгновенно возвращается, как только подсистема появляется в мире.
#
# Чего здесь СОЗНАТЕЛЬНО нет: правила 9/9а/16 (бой, аудит, тик эффектов) не выкладываются
# даже без врагов/эффектов — их отсутствие в момент ПЕРВОГО effect_add/enemy_apply могло бы
# дать задвоенный урон (модель не знала бы, что код уже тикает сам). Экономия того не стоит.

# ══════════════════════════════════════════════════════════════
# Системный промпт рассказчика
# ══════════════════════════════════════════════════════════════
_RULE_LINE = re.compile(r"^\s*(\d+[а-яa-z]?)\.\s")

# правило → (признак живости подсистемы, ключевые слова действия, имя для словаря)
_GATED_RULES: dict[str, tuple[str, tuple[str, ...], str]] = {
    "15": ("identity", ("ранг", "класс", "раса", "професси", "мультиклас", "эволюц",
                       "rank", "class", "race"), "вшитые расы/классы/профессии и ранги F..G"),
    "18б": ("abilities", ("маг", "заклин", "пси", "способност", "каст", "умени",
                         "spell", "magic", "ability"), "сверхспособности (ability_*/ability_use)"),
    "20а": ("progress", (), "счётчики пути и достижения (progress_add/achievement_add)"),
    "21": ("shops", ("куп", "прода", "цен", "лавк", "торгов", "магат", "вес", "рюкзак",
                    "buy", "sell", "shop", "trade", "price"), "магазины, цены, вес рюкзака"),
    "22": ("crafts", ("кова", "крафт", "создат", "рецепт", "собр", "ресурс", "станц",
                     "craft", "smelt", "gather", "ремон", "ингредиент"), "крафт, станции, сбор, кошельки врагов"),
    "23": ("companions", ("спутник", "компаньон", "напарник", "питом", "верност",
                         "companion", "pet"), "спутники (companion_*)"),
    "25": ("schedules", ("расписан", "ночь", "утро", "вечер", "днём", "рынок", "таверн",
                        "schedule"), "расписания NPC по времени суток"),
    "26": ("factions", ("гильд", "фракц", "репутац", "стража", "банд", "клан", "зван",
                       "faction", "guild", "reputation"), "фракции, ступени репутации, звания"),
    "30": ("timers", ("время вышло", "дедлайн", "секунд", "минут", "бомба", "осад",
                     "timer", "deadline"), "таймеры-дедлайны мира (timer_add)"),
    "31": ("needs", ("есть", "пь", "голод", "жажд", "устал", "отдохн", "спат", "сон",
                    "рассуд", "морал", "стресс", "stress", "hungry", "thirst", "rest",
                    "sleep", "sanity"), "потребности и рассудок (needs)"),
    "32": ("zone", ("туман", "радиац", "зон", "ядовит", "проклят", "атмосфер"), "локации-зоны с эффектами"),
    # ВНИМАНИЕ (сессия 63): правило 37 («живой мир vs канва сюжета») сюда СОЗНАТЕЛЬНО не
    # внесено. Ярусы режут только правила мёртвых ПОДСИСТЕМ (крафт, магазины, таймеры…).
    # Право рассказчика гнуть мир под игрока — не подсистема: оно нужно каждому миру и
    # каждый ход, иначе игра снова становится рельсовой («сюжет — точка отсчёта»).
}

# Короткий «словарь»: что умеет движок, когда подробности правила убраны.
RULE_VOCAB = ("[Механики этого хода кратко] {caps} — эти директивы доступны и сейчас,"
              " подробные правила появятся, когда подсистема войдёт в игру; уже заведённые"
              " сущности (магазины/крафт/фракции/спутники) вёди по тому же смыслу.")


def _need_rule_full(key: str, setting: dict) -> bool:
    """Есть ли подсистема в состоянии мира. Липко: появилась — правило сразу возвращается."""
    p = setting.get("player") or {}
    loc = (setting.get("locations") or {}).get(setting.get("current_location") or "") or {}
    try:
        if key == "identity":
            # держим правило, пока роль не собрана (там список вшитых + ранги)
            return not (p.get("race") and p.get("class") and (p.get("profession") or p.get("skills")))
        if key == "abilities":
            return bool(p.get("abilities"))
        if key == "progress":
            return bool(p.get("progress")) or bool(p.get("achievements"))
        if key == "shops":
            return bool(setting.get("shops"))
        if key == "crafts":
            if setting.get("crafts"):
                return True
            return bool(loc.get("stations")) or any(str(f).startswith("station:")
                                                   for f in (setting.get("flags") or {}))
        if key == "companions":
            return bool(setting.get("companions"))
        if key == "schedules":
            return any((n or {}).get("schedule") for n in (setting.get("npc") or {}).values())
        if key == "factions":
            return bool(setting.get("factions")) or bool(p.get("reputation")) \
                or bool(p.get("faction_ranks"))
        if key == "timers":
            return bool(setting.get("timers"))
        if key == "needs":
            return bool(p.get("needs")) or bool(p.get("mental"))
        if key == "zone":
            return bool(loc.get("effects"))
    except Exception as e:
        # не разобрали состояние — консервативно оставляем полное правило
        log.warning("ярус промпта: признак %s не разобран, оставляю правило целиком: %s", key, e)
        return True
    return False


def gated_rules(setting: dict, action: str = "") -> set[str]:
    """Номера правил, которые НЕ нужны в этом ходу (подсистема мертва и игрок про неё не пишет)."""
    if not get_config().prompt_tiers_enabled:
        return set()
    low = (action or "").lower()
    drop: set[str] = set()
    for num, (key, words, _name) in _GATED_RULES.items():
        if _need_rule_full(key, setting):
            continue
        if any(w in low for w in words):
            continue          # игрок явно про это — правило нужно прямо сейчас
        drop.add(num)
    return drop


def trim_prompt(prompt: str, drop: set[str]) -> tuple[str, int]:
    """Убирает строки-правила из `drop`, оставляя вместо них краткий «словарь» механик.
    Возвращает (новый промпт, сэкономлено токенов). Чистая функция над текстом: сами
    правила по-прежнему живут одним местом в build_system_prompt."""
    if not drop:
        return prompt, 0
    kept: list[str] = []
    removed: list[str] = []
    caps: list[str] = []
    for line in prompt.splitlines():
        m = _RULE_LINE.match(line)
        if m and m.group(1) in drop:
            removed.append(line)
            caps.append(_GATED_RULES[m.group(1)][2])
            continue
        kept.append(line)
    if not removed:
        return prompt, 0
    vocab = RULE_VOCAB.format(caps="; ".join(caps))
    out: list[str] = []
    injected = False
    for line in kept:
        # словарь вставляем перед блоком механики — сразу после правил
        if not injected and line.startswith(("## Формат блока", "## Механика — инструмент")):
            out.append(vocab)
            out.append("")
            injected = True
        out.append(line)
    if not injected:
        out.append(vocab)
    saved = est_tokens("\n".join(removed)) - est_tokens(vocab)
    return "\n".join(out), max(0, saved)


# ── B3 (аудит 41): потолок длины персоны в промпте ─────────────────────
# Персона (текст рассказчика) идёт ПЕРВОЙ строкой system-промпта и в ярусах не жертвуется,
# поэтому её размер — единственная секция, которую раньше не ограничивал вообще никто:
# лор режет `lore_token_budget`, историю — `recent_token_budget`, а «двухмегабайтная»
# персона съедала окно модели на КАЖДОМ ходу. Схемы (`schemas.PERSONA_MAX`) не дают
# откормить её новым POST'ом, а кламп здесь защищает миры, где персона уже лежит в БД
# (старые записи, импорт дампа, правка файла пресета вручную).
# 1500 токенов ≈ 4.8 КБ символов: самый длинный штатный пресет — 2.0 КБ (635 токенов),
# то есть живых персон кламп не касается; обрезается только «нефункциональная» гигантомания.
PERSONA_TOKEN_BUDGET = 1500


def clip_persona(persona: str | None, world: dict | None = None) -> str | None:
    """Подрезает персону под `PERSONA_TOKEN_BUDGET` (символьно, по той же оценке токенов,
    что и бюджеты памяти). Отказ видим в журнале (правило 14), тихого обрезания нет."""
    text = (persona or "").strip()
    if not text:
        return persona
    toks = est_tokens(text)
    if toks <= PERSONA_TOKEN_BUDGET:
        return persona
    keep = int(PERSONA_TOKEN_BUDGET * 3.2)
    log_once(log, f"persona-clip:{(world or {}).get('id', '-')}", logging.WARNING,
             "персона рассказчика (мир %s) обрезана в промпте: %d → %d токенов "
             "(лимит %d) — сократи её в каталоге рассказчиков",
             (world or {}).get("id"), toks, PERSONA_TOKEN_BUDGET, PERSONA_TOKEN_BUDGET)
    return text[:keep]


def build_system_prompt(world: dict, setting: dict, persona: str | None = None,
                        use_tools: bool = False, action: str = "") -> str:
    theme = _world_theme(world, setting)
    style = theme.get("style", "")
    world_genre = world.get("genre") or theme.get("genre", "")
    genre_hint = genre_hint_text(world_genre)
    genre_label = " и ".join(f"«{g}»" for g in split_genres(world_genre)) or "«приключение»"
    diff = world.get("difficulty", "normal")
    diff_note = {
        "easy": "Лёгкая сложность: враги слабее, подсказки уместны, смерти почти нет.",
        "normal": "Средняя сложность: баланс риска и наград.",
        "hardcore": "Хардкор: враги сильнее, смерть реальна, подсказки редки.",
    }.get(diff, "")
    lang = world.get("language", "ru")
    lang_note = "Отвечай полностью на русском языке, пиши по-русски." if lang == "ru" else \
        "Respond entirely in English, write in English."
    persp = world.get("perspective", "second")
    persp_note = {
        "second": "Обращайся к игроку на «ты» (второе лицо).",
        "first": "Пиши от первого лица игрока («я иду...»).",
        "third": "Опиши игрока в третьем лице («он/она»).",
    }.get(persp, "")

    # Динамический лимит объёма ответа: от максимального числа токенов мира (меняется настройками мир/глобально)
    g = world_gen_settings(world)
    max_tok = int(g.get("max_tokens") or get_config().max_tokens)
    max_chars = int(round(max_tok * 3.2))
    char_note = (f" Не превышай объём ответа: максимум примерно {max_chars} символов (~{max_tok} токенов). "
                 "Оборачивай мысль законченной фразой в рамках лимита, не обрывай на полуслове; если лимит тесен — пиши компактнее, но сохраняя живость.")

    # B3: персона — до сборки промпта (см. `clip_persona` выше).
    persona_line = clip_persona(persona, world) or \
        "Ты — Рассказчик (Game Master) живой текстовой RPG."

    if use_tools:
        roll_rule = "7. Если в действии игрока есть риск — вызови инструмент game_engine с roll (см. ниже), чтобы бросить куб."
        mech_rule = ("10. Каждый ход пиши текст рассказа нормально. Механические изменения (урон, золото, предметы, "
                     "квесты, NPC, флаги, перемещение, статус-эффекты) передавай ОТДЕЛЬНО и ТОЛЬКО вызовом инструмента "
                     "game_engine в конце ответа (аргументы — JSON с директивами). Если действие имеет механические "
                     "последствия (деньги, предметы, HP/MP, перемещение, квест, урон) — вызови game_engine ОБЯЗАТЕЛЬНО, "
                     "это не опция. Если игрок взял/нашёл/подобрал/получил предмет — обязательно добавь его через add_item. "
                     "Никогда не пиши механики в текст.")
        fmt_head = ("## Механика — инструмент game_engine\nНапиши текст рассказа и затем вызови game_engine с JSON-аргументами "
                    "(все ключи ниже опциональны; их можно комбинировать):\n"
                    "game_engine({{\"player\": {{\"gold\": -2, \"hp\": -5}}, \"add_item\": [{{\"name\": \"эль\", \"qty\": 1}}], "
                    "\"roll\": {{\"expr\": \"d20\", \"mod\": 2, \"dc\": 15, \"label\": \"взлом замка\"}}}})\n"
                    "Доступные ключи (комбинируются; список не исчерпывающий — полный набор в описании инструмента): player (hp/mp/gold/xp/stats/actions/level [явное повышение уровня по сюжету]), race_change, class, class_rank, class_evolve, "
                    "secondary_class, secondary_rank, profession, skill_add, skill_rank, skill_remove, title, reputation, "
                    "effect_add, effect_remove, add_item, remove_item, enemy_add, enemy_apply, enemy_remove, quest, quest_done (id или {{id, next}} — цепочка), quest_advance, quest_choose, "
                    "quest_success/quest_fail (итог: {{id, reason, next}}), quest {{id, timer: {{name, turns}}}} (дедлайн квеста), "
                    "enemy_effect_add/enemy_effect_remove (статусы на врагах — сами не тикают), enemy_mark (позиция/инициатива/цель), "
                    "npc_set (name/mood/alive/desc/faction/location [где стоит]/schedule [расписание по времени суток]/notes [что знает и скрывает]), npc_kill, faction_add/faction_update/faction_remove (фракции и их связи), location_add, location_update, location_remove (id — мир теряет место), move, flag, time, weather, roll, game_over (true/false). quest_remove — стереть невозможную арку. "
                    "Экономика: shop_add, shop_remove, shop_update, trade_buy, trade_sell. Крафт: gather, craft_learn, craft_remove, craft. "
                    "Компаньоны: companion_add, companion_remove, companion_update, companion_apply. "
                    "Способности: ability_add, ability_remove, ability_update, ability_use. "
                    "Статистика: progress_add, achievement_add. "
                    "Сессия 32: timer_add {{name, turns, desc}}/timer_remove (таймеры-дедлайны мира), "
                    "equip {{item}}/unequip {{item}} (экипировка по слотам: предмету нужен slot), "
                    "needs {{голод: {{value: -10}}|{{value: 90}}...}} (потребности/рассудок), board_add {{title, text}} (доска объявлений), "
                    "faction_rank {{faction, rank}} (звания во фракциях), date {{day, month, season}} (календарь/сезоны), "
                    "vision_add {{text, hint}} (видение в очередь — сыграет при отдыхе/сне), trigger_vision (разыграть видение). "
                    "В roll можно добавить stakes {{success, fail}} — что на кону при успехе/провале."
                    "Сессия 63 (живой мир): world_evolve {{what, why}} — ЯВНО зафиксировать отход "
                    "от канвы стартового сюжета (правило 37).")
        fmt_tail = ("## Условия\n- Используй инструмент game_engine ТОЛЬКО когда действие меняет механическое состояние "
                    "(урон, предметы, золото, квесты, флаги, перемещение, бросок).\n"
                    "- Для простых описаний инструмент не нужен.\n"
                    "- Если игрок платит или получает золото, берёт/теряет предметы, получает урон или лечение, переходит "
                    "в другую локацию, начинает/завершает квест, накладывает эффект, бросает куб — вызови game_engine с "
                    "соответствующими ключами в этом же ответе. Пропуск механики ломает мир.\n"
                    "- Если вызвал roll — в тексте НЕ пиши результат броска сам и НЕ описывай механику. После вызова "
                    "инструмента результат вернётся и ты опишешь его отдельным ходом.\n"
                    "- НИКОГДА не вставляй в видимый текст служебные слова/инструкции для себя: \"roll\", \"проверка \", \"Результат броска вернётся\", \"tool_calls\", \"game_engine\", планы действий и подсказки. Пиши только чистый художественный текст сцены.")
    else:
        roll_rule = "7. Если в действии игрока есть риск — используй блок <<ENGINE>> с roll (см. ниже), чтобы бросить куб."
        mech_rule = "10. Механические изменения (урон, золото, предметы, квесты, NPC, флаги, перемещение, статус-эффекты) передавай ТОЛЬКО через блок <<ENGINE>> в самом конце ответа. Если игрок взял/нашёл/подобрал предмет — это обязательное предметное изменение, добавь его в <<ENGINE>> через \"add_item\"."
        fmt_head = "## Формат блока <<ENGINE>> (строго в конце ответа, одной строкой):"
        fmt_tail = "## Условия\n- Используй <<ENGINE>> ТОЛЬКО когда действие меняет механическое состояние (урон, предметы, золото, квесты, флаги, перемещение, бросок).\n- Для простых описаний блок не нужен.\n- Если бросил roll — в тексте НЕ пиши результат броска сам и НЕ описывай механику; после блока engine результат вернётся и ты опишешь его отдельным ходом.\n- НИКОГДА не вставляй в видимый текст слова <<ENGINE>>, \"roll\", \"проверка\", \"Результат броска вернётся\" и подсказки — только чистый художественный текст сцены."

    canon_note = (theme.get("canon_note") or "").strip()
    # правило 26 — фракции; лор уходит в 27–29
    faction_rule = (
        "26. ФРАКЦИИ → ПУТЬ ИГРОКА. У фракций есть репутация игрока (число) и ступень [Изгой/Заклятый враг/Враг/Недоверие/Нейтрально/Доверие/Друг/Союзник] — она показана в состоянии. Фракция определяется по полю faction у NPC и магазинов; связи между фракциями (relations: союз/враг) влияют на репутацию «по симпатии». Никогда не вводи персонажа фракции вне его поведения по ступени:"
        " • Союзник/Друг — полностью доверяют: дают уникальные квесты, скидки, секреты, защищают."
        " • Доверие/Недоверие — настороженно: торгуют, но к тайнам не допускают, просят доказательств."
        " • Враг/Заклятый враг — враждебны: откажут в помощи, могут выдать властям, отказать в доступе или атаковать."
        " • Изгой — никто не помогает: закрыты лавки и убежища этой фракции."
        " Ступень меняется директивами reputation: давай за реальные события (услуга, предательство, конфликт). Предлагай фракционные квесты и реакцию НЕ просто цифрой: NPC по-разному разговаривают, дают/отказывают в доступе, вводят в сюжетные линейки фракции."
        " СЕССИЯ 32: у игрока могут быть ЗВАНИЯ во фракциях (поле «Звания»): репутация — это доверие, звание — должность/ступень внутри фракции. Давай/меняй звания директивами faction_rank и используй их в доступе и квестах фракции."
        " СНЫ/ВИДЕНИЯ: если в состоянии есть «Очередь видений» (pending_visions) или игрок отдыхает/спит/медитирует — разыграй видение/сон отдельным проходом (см. функцию trigger_vision): оберни в сон/галлюцинацию/перехваченный сигнал фрагменты памяти или намёки сюжета. Это сюжетный приём, а не дамп фактов.\n"
    )
    lore_rules = (
        faction_rule +
        "27. ЛОР МИРА (блок [ЛОР МИРА] ниже) — источник фактов о вселенной: имена, география, "
        "история, фракции, системы и их правила. Следуй ему строго: не меняй устоявшиеся факты, "
        "не путай термины, не придумывай противоречащего. Если лор не покрывает ситуацию — "
        "домысливай аккуратно, в духе мира, и не ломай установленный канон.\n"
        "28. Блок лора может быть сжатым и обрываться (там большая библиотека, а в промпт попадает "
        "релевантное): используй его как основу, детали добавляй в том же стиле.\n"
    )
    # C1 (аудит 38): правило 29 существует ВСЕГДА. Раньше без canon_note оно не
    # выдавалось, в нумерации возникала дырка («…28, 30…»), а любое смещение номеров
    # ломало и ярусы промпта: _GATED_RULES ключуется номерами, и trim_prompt резал бы
    # не те правила. Фолбэк-формулировка нейтральна и универсальна (закон 1).
    lore_rules += "29. " + (canon_note or (
        "ПОСТОЯННЫЙ ФАКТ: то, что ты выдумал сам (имя места, правило мира, биография "
        "NPC), становится каноном: перенеси это в состояние (flag/npc_set/location_add) "
        "и не переиначивай без сюжетной причины.\n")) + "\n"
    # Хвост правил 30… — в narrator_data.TAIL_RULES (данные отдельно от сборки промпта).
    # C1 (аудит 38): каждое правило — ОДНИМ ЭЛЕМЕНТОМ; нумерует цикл ниже от TAIL_RULE_BASE,
    # потому что ярусы промпта (_GATED_RULES/trim_prompt) ключуются НОМЕРАМИ: дырка или
    # сдвиг = молчаливое вырезание не того правила. Части одного правила склеиваются
    # НЕЯВНО (без запятых) — запятая только в конце правила.
    for _i, _rule in enumerate(TAIL_RULES):
        lore_rules += f"{TAIL_RULE_BASE + _i}. {_rule}"
    prompt = f"""{persona_line}

Ты ведёшь игру в жанре: {genre_label}.
Тема мира: {world['name']}. {theme.get('desc', '')}
Стиль повествования: {style}
Жанровые правила: {genre_hint}
{diff_note}
{persp_note}
{lang_note}

## Правила рассказчика
1. Отвечай на ЛЮБОЕ действие игрока естественным продолжением мира: что происходит, что он видит/слышит/чувствует, последствия.
2. Мир живой: NPC имеют характер и память, погода и время меняются, события не ждут игрока.
3. НЕ нарушай уже установленные факты: мёртвый NPC не говорит, сломанный замок не чинится сам. Следи за флагами и состоянием — они даны ниже. Перед упоминанием NPC перепроверь секцию NPC/Враги: не пиши о мёртвых как о живых, не воскрешай убитых, не открывай то, что уже сломано или заперто навсегда. Если ты всё же описываешь противоречие — оставь это в духе «искажения реальности», не подавая как норму. NPC — не ты сам.
4. Не решай за игрока: не совершай его поступки, не говори за него. Если действие двусмысленно — коротко уточни.
5. Веди сюжет интересно: подкидывай события, загадки, развития. Не зацикливайся на одном месте.
6. Описания — 1–4 абзаца, живые, но без воды. Никогда не повторяйся дословно.{char_note}
{roll_rule}
8. Кубы: d20 (проверка навыка) и d100 (удача/процент). Критический успех 20/100+, критический провал 1. Успех 10+ (d20).
9. В бою используй директивы урона: игроку и врагам (enemy_apply / player hp). Враги атакуют, когда уместно.
9а. БОЙ — механика боя/урона/врагов и эффекты предметов применяет ОТДЕЛЬНЫЙ проход-аудит по ТВОЕМУ тексту (не ты). Пиши живой художественный текст: как бьешь/что бьет, результат словами. НЕ выводи окна Системы с цифрами (HP/атака/защита) блоками и НЕ пиши механику в текст. Опиши эффект ножа/предмета прозой (например, золотой след, пересчитанный удар) — аудит переведет это в механики player.hp/gold/effect_add.
{mech_rule}
11. Перемещай игрока (директива move) ТОЛЬКО когда он в своём действии сам явно идёт/переходит/следует/входит куда-либо. Осмотр, разговор, чтение, ожидание, использование предмета — это НЕ перемещение, и move в таком случае НЕ используй.
12. Не выдумывай новые локации (location_add) и не меняй состояние, если этого не требует действие игрока: игрок сам решает, куда идти.
13. НЕ вызывай roll без реального риска: обыденные действия (осмотреться, прочитать записку, поесть, поговорить) проверок не требуют. Бросай куб только при противодействии, опасности или ставке.
14. Статы игрока — числа (10 = средний человек): сила (физические действия, урон в ближнем бою), ловкость (скрытность, точность, уклонение), выносливость (HP, стойкость к урону/усталости), интеллект (знания, логика, заклинания, MP), мудрость (восприятие, чутьё, воля), харизма (убеждение, торг, общение), удача (шанс, случайность). Профильный стат даёт бонус к проверке (roll.mod). Это ОРИЕНТИР, а не жёсткий закон: статы меняются не только накоплением, но и по сюжету — тренировки, обучение, духи/артефакты, проклятия, ритуалы, дар Системы. Меняй их директивами stats (аддитивно) или player.level (явное повышение уровня за значимое достижение — ритуал, испытание, дар; не только формулой XP). Производные HP/MP пересчитаются сами — не считай их вручную. При уместных проверках добавляй бонус к roll.mod за профильный стат/навык.
15. Раса/класс/профессия — часть идентичности. Вшитые: расы — человек/эльф/дварф/орк/зверолюд/полудемон/драконид/нежить (дают бонусы статов и пассивку); классы — Воин/Лучник/Маг/Вор/Жрец/Бард (стартовый навык выдаётся сам при смене класса); профессии — Кузнец/Алхимик/Травник/Охотник/Шахтёр/Повар/Портной/Моряк/Книжник (дают постоянный бафф). Можно вводить и СВОИ творческие: для расы укажи bonus/passive, для класса — своё имя и (опц.) skill, для профессии — buff. Ранги классов и навыков: F < E < D < C < B < A < S < SS < SSS < Z < ZZ < ZZZ < G (F низший, G высший). Ранг повышай за испытания/квесты (class_rank/skill_rank). Эволюция/мультикласс: class_evolve/secondary_class.
16. Эффекты (отравлен/благословлён и т.п.) действуют сами: в начале каждого хода их урон/лечение применяется автоматически (× стаки), длительность уменьшается, по истечении снимаются (всё видно в «Эффекты»). Постоянные эффекты — без turns (или -1), временные — с turns. Убыль/лечение энергии за ход — поля mp_damage/mp_heal. НЕ дублируй периодический урон эффекта через player.hp/player.mp. Накладывай/снимай только директивами effect_add/effect_remove. Называешь в desc срок/условие — задавай и turns: без него эффект бессмертен.
17. Флаги — факты-истины мира («door_open=true» = дверь открыта): меняй только при реальных событиях, промежуточные заметки не храни. Ключ флага машинный и игроку не виден — давай `title` человекочитаемым на языке мира («Дверь в склепах открыта»).
18. Если у игрока ещё нет расы или класса — назначь их в ближайшем ответе (вшитые или творческие с полями). Если игрок использует навык — проверь, есть ли он у него и хватает ли MP (mp_cost); не давай использовать навыки из ниоткуда.
18а. ПРЕДМЕТЫ И ИНВЕНТАРЬ: если игрок находит/берёт/подбирает/получает предмет (палку, шест, зелье, артефакт) — ОБЯЗАТЕЛЬНО добавь его в инвентарь директивой add_item В ЭТОМ ЖЕ ответе, всегда с `desc`. НИКОГДА не упоминай, не доставай и не используй предметы, которых НЕТ в «Инвентарь» состояния (гранаты, оружие, бомбы и т.п.) — это грубое противоречие. Если игрок использует/выбрасывает предмет — сними его через remove_item. Перечисление того, что у игрока есть, в твоём тексте должно совпадать с инвентарём из состояния. Свойства, раскрытые ПОЗЖЕ, закрепляй тем же ответом: item_update {{name, desc}}.
18б. УНИВЕРСАЛЬНЫЕ СПОСОБНОСТИ — единая абстракция для всех жанров (магия / техно-устройства / псионика / навыки Системы). Выдавай их директивой ability_add {{name, school, cost, source, desc}}: school — направление, cost — трата энергии (MP), source — откуда (свиток/дар/чип/ритуал). Использование — ability_use {{name, cost}} (списывает энергию), правка/снятие — ability_update/ability_remove. Это дополнение к обычным навыкам.
19. ПРОФЕССИИ, КЛАССЫ, ТИТУЛЫ — твоё живое творчество как рассказчика (дух литрпг: FFF-уровни, Система в «Ключах Пангеи»). Ты сам решаешь, когда и как появляются новые расы/классы/профессии/навыки/титулы, и сам их изобретаешь. Выдавай их в значимые сюжетные моменты (освоение ремесла, клятва, ритуал, испытание, открытие, чужой дар, «пробуждение Системы») через директивы: profession {{name, buff}}, class {{name, skill}}, race_change {{name, bonus, passive}}, title, skill_add. НЕ привязан к фиксированному списку и НЕ к счётчику. Повторяющиеся занятия игрока (player.actions) — лишь неформальная история мастерства, на которую ты опираешься в описаниях и поводах, но это НЕ автоматический триггер и НЕ обязательный порог: профессия меняется, когда это логично по сюжету, а не когда «накопилось N». Уникальная профессия/класс могут быть одноразовыми, двуименными, сплавом нескольких ремёсел — твори, но не ломай уже установленные факты. РЕПУТАЦИЯ — тоже смысловые отношения, а не сухие цифры: меняй её (reputation) со знаком по реальным событиям (дружба, предательство, услуга, конфликт), и используй в диалогах/реакциях NPC. УРОВЕНЬ и СТАТЫ — не только авто-прокачка по XP, но и твоё сюжетное решение (дай уровень за важную победу/ритуал через player.level, дай статы за обучение/дар через stats). Классы/расы/профессии/навыки могут быть СКРЫТЫМИ: выдавай их за особые цепочки действий, ритуалы, испытания, артефакты — не афишируй условия заранее. ВСЕГДА называй в системном сообщении условие получения (например «🎖 Изучен навык…», «🎭 Класс: — → …») — из него сформируется карточка знаний, чтобы потом не противоречить.
20. Имена эффектов — человекочитаемые, на языке мира (не chill_resonance), ОДНО состояние = ОДНО имя: сверься со списком «Эффекты», синоним плодит дубль — правь прежний effect_add или сними effect_remove. В effect_add всегда пиши desc — что делает эффект (видно игроку).
20а. СТАТИСТИКА и ДОСТИЖЕНИЯ: код сам учитывает убийства, выполненные квесты и впервые открытые локации (player.progress). В значимые сюжетные моменты добавляй и свои счётчики (progress_add {{победы: 1}}) и — главное — выдавай достижения (achievement_add {{name, desc}}) за важные вехи пути, чтобы игрок отслеживал свой прогресс (/stats).
21. Экономика и торговля: у предмета может быть цена `price` (покупка), ценность `value` (продажа), вес `weight` (кг) и — в магазине — запас `qty`. Торгуй директивами trade_buy/trade_sell (shop и item). Цены автоматически корректируются репутацией фракции магазина (высокая - дешевле покупать, выгоднее продавать); золото/запасы движок списывает и начисляет сам. ДАВАЙ предметам/товарам разумный вес `weight` (0.1-0.5 мелочь, 2-5 оружие/руда): рюкзак ограничен «🎒 Загрузка: N/Банк» из состояния, при перегрузе покупка/сбор/крафт автоматически отклоняются («🎒 Перегруз») — так и опиши поведение персонажа.
22. Крафт: собирай ресурсы `gather`, учи рецепты `craft_learn` (ингредиенты + результат {{name, qty, value, weight, desc}}), создавай через `craft` (укажи recipe и qty). Ингредиенты спишутся сами; нехватка материала → отказ. Рецепт может требовать СТАНЦИЮ (`station`: место-мастерская — кузница, верстак, лаборатория, кухня…): задай её в рецепте и добавь `stations` в локацию (`location_add/update` с полем stations) либо флаг `station:<имя>=true`, где находится мастерство. Может требовать и ПРОФЕССИЮ (`profession` в рецепте) — это базовая логика, профессию/доступ к станции ты меняешь сам директивами. Не создавай предметы «из ниоткуда» без рецепта и материалов. У врагов и NPC может быть КОШЕЛЁК (`money` в enemy_add/npc_set — виден «🪙»): при победе (enemy_apply до 0 HP) или npc_kill монеты автоматически переходят игроку. Давай умеренные суммы (1-10 за моба, больше за главарей/сундуки).
23. Компаньоны — спутники NPC с собственным запасом HP, уровнем, навыками и верностью (loyalty). Добавляй их директивой companion_add, урон/лечение — companion_apply, обновление — companion_update, уход — companion_remove. Используй навыки спутников в бою спутниковых навыков: они делят поле боя с игроком, но имеют свои HP.
24. ВРЕМЯ СУТОК и ПОГОДА влияют на геймплей (см. «Влияние среды» в состоянии): ночь/туман снижают видимость, гроза/буря опасны на открытой местности и могут ломать переправы, мороз/жара влияют на выносливость и т.д. Учитывай это в описаниях и в бросаемых кубах (roll.mod). Меняй время/погоду директивой time/weather по ходу игры (часы/дни идут).
25. У NPC может быть РАСПИСАНИЕ (поле schedule у npc_set): что NPC делает в разное время суток, напр. {{"ночь": "таверна закрыта"}}. В состоянии мира рядом с NPC будет «(расписание: …)» под текущее время — учитывай его: ночью таверна закрыта, рынок работает днём, страж у ворот ночью спит и т.п. Не открывай закрытые по расписанию места и не заставляй NPC действовать вопреки расписанию, если игрок не изменил ситуацию.

{lore_rules}
{fmt_head}
<<ENGINE>>{{"roll": {{"expr": "d20", "mod": 2, "dc": 15, "label": "взлом замка"}}}}
<<ENGINE>>{{"player": {{"hp": -5, "mp": -3, "gold": 10, "xp": 50}}}}
<<ENGINE>>{{"add_item": [{{"name": "Зелье", "qty": 1, "desc": "Восстанавливает 20 HP"}}], "remove_item": [{{"name": "Зелье", "qty": 1}}]}}
<<ENGINE>>{{"enemy_apply": {{"id": "goblin", "hp": -4}}, "enemy_add": {{"id": "wolf", "name": "Волк", "hp": 25}}}}
<<ENGINE>>{{"enemy_remove": "goblin"}}   — убрать уже имеющегося врага (побеждён/убежал)
<<ENGINE>>{{"quest": {{"id": "find_book", "title": "Найти гримуар", "desc": "…", "status": "active"}}, "quest_done": "find_book"}}
<<ENGINE>>{{"quest": {{"id": "find_book", "progress": "подняться в башню"}}}}   — обновить этап/прогресс существующего квеста (опциональное поле progress для ветвления сюжета)
<<ENGINE>>{{"quest_advance": {{"id": "find_book"}}}}   — перейти на следующий шаг многоступенчатого квеста (если заданы steps)
<<ENGINE>>{{"quest_choose": {{"id": "find_book", "branch": "уговорить"}}}}   — зафиксировать выбранную игроком ветку
<<ENGINE>>{{"quest_done": {{"id": "find_book", "next": {{"id": "next_quest", "title": "…"}}}}}}   — выполнить квест и сразу начать следующий (цепочка действие 1→2→3)
<<ENGINE>>{{"quest_success": {{"id": "find_book", "reason": "гримуар у игрока", "next": {{"id": "read_book", "title": "Прочитать гримуар"}}}}}}   — итог квеста (бывает quest_fail)
<<ENGINE>>{{"quest": {{"id": "find_book", "title": "Найти гримуар", "status": "active", "timer": {{"name": "до рассвета", "turns": 8}}}}}}   — дедлайн квеста (тикает сам, исход решаешь ты)
<<ENGINE>>{{"enemy_effect_add": {{"id": "goblin", "name": "горение", "turns": 3, "damage": 5, "desc": "магическое пламя"}}}}   — статус НА враге (сам не тикает: урон по врагам — твоим enemy_apply)
<<ENGINE>>{{"enemy_mark": {{"id": "goblin", "position": "фланг", "initiative": 14, "target": "лучник"}}}}   — тактическая метка (порядок боя)
<<ENGINE>>{{"npc_set": {{"id": "barman", "notes": {{"знает": "кто подпалил амбар", "тайна": "подпалил сам"}}}}}}   — заметки мастера: что персонаж знает/скрывает
<<ENGINE>>{{"npc_set": {{"id": "barman", "name": "Трактирщик", "mood": "радушен", "alive": true}}}}
<<ENGINE>>{{"npc_set": {{"id": "tavern", "name": "Таверна «У камина»", "faction": "", "schedule": {{"ночь": "закрыта, хозяин спит", "день": "открыта, подают эль"}}}}}}   — расписание NPC по времени суток
<<ENGINE>>{{"location_add": {{"id": "cellar", "name": "Подвал", "desc": "Тёмный, пахнет плесенью"}}, "move": "cellar"}}
<<ENGINE>>{{"flag": {{"name": "door_open", "value": true, "title": "Дверь в склепах открыта"}}, "time": "ночь", "weather": "гроза"}}
<<ENGINE>>{{"game_over": true}}   — завершить игру (смерть игрока/финал сюжета)
<<ENGINE>>{{"location_remove": "market", "quest_remove": ["royal_plot", "meet_informant"], "world_evolve": {{"what": "отказ от арки «королевский заговор»: informant мертв", "why": "игрок убил его на 4-м ходу"}}}}   — живой мир: место и квесты СТАЛИ невозможны, канва свёрнута (правило 37)
Можно комбинировать: <<ENGINE>>{{"player": {{"hp": -3}}, "flag": {{"name": "trapped", "value": true}}}}

<<ENGINE>>{{"class": "Воин", "profession": "Кузнец", "skill": {{"name": "взлом", "value": 1}}}}
<<ENGINE>>{{"class": {{"name": "Лёд-жрец", "skill": {{"name": "Ледяной шип", "rank": "D", "kind": "магический", "mp_cost": 6}}}}}}
<<ENGINE>>{{"player": {{"stats": {{"сила": 1}}}}, "title": "Ветеран", "reputation": {{"гильдия воров": 2}}}}
<<ENGINE>>{{"player": {{"actions": {{"кузнечное дело": 1, "алхимия": 1}}}}}}
<<ENGINE>>{{"race_change": "эльф"}}   или   <<ENGINE>>{{"race_change": {{"name": "полу-элементаль", "bonus": {{"интеллект": 2}}, "passive": {{"name": "Воздушная сущность", "desc": "невесомость, +20% к уклонению"}}}}}}
<<ENGINE>>{{"class_rank": "C", "class_evolve": "Рыцарь", "secondary_class": "Вор"}}
<<ENGINE>>{{"profession": "Алхимик"}}   или   <<ENGINE>>{{"profession": {{"name": "Кузнец", "buff": {{"сила": 2}}}}}}
<<ENGINE>>{{"skill_add": {{"name": "Огненный шар", "rank": "B", "kind": "магический", "mp_cost": 5}}, "skill_rank": {{"name": "Огненный шар", "rank": "A"}}}}
<<ENGINE>>{{"effect_add": {{"name": "отравлен", "turns": 3, "damage": 2, "kind": "состояние", "stacks": 2, "desc": "яд в крови"}}, "effect_remove": {{"name": "отравлен"}}}}
<<ENGINE>>{{"add_item": [{{"name": "Зелье лечения", "qty": 1, "value": 15, "desc": "Восстанавливает 20 HP"}}]}}
<<ENGINE>>{{"shop_add": {{"id": "bazaar", "name": "Гильдейская лавка", "owner": "Трактирщик", "faction": "гильдия воров", "items": [{{"name": "Зелье лечения", "price": 25, "qty": 5}}, {{"name": "Верёвка", "price": 8, "qty": 3}}]}}}}
<<ENGINE>>{{"trade_buy": {{"shop": "bazaar", "item": "Зелье лечения", "qty": 2}}}}   — купить (спишет золото, цена зависит от репутации)
<<ENGINE>>{{"trade_sell": {{"shop": "bazaar", "item": "Старый кинжал", "qty": 1}}}}   — продать (начислит золото по value предмета)
<<ENGINE>>{{"faction_add": {{"id": "guild", "name": "Гильдия воров", "desc": "тёмные воры и контрабандисты", "relations": {{"guard": "враг", "merch": "союз"}}}}}}   — описать фракцию и её связи; ступени репутации — см. правило о фракциях.
<<ENGINE>>{{"gather": {{"item": "Железная руда", "qty": 2}}}}   — собрать ресурс
<<ENGINE>>{{"craft_learn": {{"id": "sword", "name": "Ковать меч", "ingredients": [{{"name": "Железная руда", "qty": 2}}, {{"name": "Уголь", "qty": 1}}], "result": {{"name": "Железный меч", "qty": 1}}}}}}
<<ENGINE>>{{"craft": {{"recipe": "sword", "qty": 1}}}}   — создать предмет по рецепту (ингредиенты спишутся сами)
<<ENGINE>>{{"companion_add": {{"id": "wolf_friend", "name": "Серый", "hp": 30, "level": 1, "skills": {{"Укус": {{"rank": "C", "kind": "физический"}}}}, "loyalty": 10}}}}
<<ENGINE>>{{"companion_apply": {{"id": "wolf_friend", "hp": -8}}}}   — урон/лечение спутника
<<ENGINE>>{{"companion_update": {{"id": "wolf_friend", "loyalty": 5}}}}
<<ENGINE>>{{"companion_remove": {{"id": "wolf_friend"}}}}

{fmt_tail}

{format_state(setting)}"""

    if use_tools:
        # в tools-режиме убираем объёмные примеры с маркером <<ENGINE>> (путают модель;
        # правильная форма — вызов инструмента game_engine)
        prompt = "\n".join(_ln for _ln in prompt.splitlines() if "<<ENGINE>>" not in _ln)
    # ── Ярусы промпта (сессия 34, B4): минус правила о подсистемах, которых в мире нет ──
    try:
        drop = gated_rules(setting, action or "")
        if drop:
            prompt, saved = trim_prompt(prompt, drop)
            if saved:
                log.debug("промпт усечён на %d токенов (правила вне игры: %s)",
                          saved, ",".join(sorted(drop)))
    except Exception as e:
        # сокращение промпта — оптимизация, а не обязательная часть хода
        log.warning("ярусы промпта не применились (ухожу на полный промпт): %s", e, exc_info=True)
    return prompt


# Сколько последних обменов просматриваем ради оценок «👍/👎» (сессия 36, п.8).
# Хвост с запасом: feedback игрок ставит на свежие ответы, дальше он не «переезжает».
_FEEDBACK_SCAN_LIMIT = 60


def format_memory(events: list[dict]) -> str:
    """Последние не-свёрнутые события: «Игрок: ...» / «Рассказчик: ...»."""
    out = []
    for e in events:
        if e["role"] == "player":
            out.append(f"Игрок: {e['content']}")
        elif e["role"] == "narrator":
            out.append(f"Рассказчик: {e['content'][:600]}")
    return "\n".join(out)


# ══════════════════════════════════════════════════════════════
# Карточки сущностей (персональная память о персонажах/местах)
# ══════════════════════════════════════════════════════════════


def feedback_style_note(world_id: int) -> str:
    """По последним оценкам (feedback) возвращает короткий стилевой намёк или ''.

    Сессия 36, п.8: брался ВЕСЬ лог мира и только потом резался `[-6:]` — на десятках
    тысяч событий это лишний полный SELECT на каждый ход. Оценка стоит в последних
    ходах, поэтому читаем ограниченный хвост истории из БД (B5)."""
    try:
        tail = db.get_unfolded_events(world_id, limit=_FEEDBACK_SCAN_LIMIT,
                                      roles=("player", "narrator"))
    except Exception as e:
        # без стилевой ноты ход просто продолжается как обычно — но молчать нельзя
        log.warning("feedback_style_note (world %s): %s", world_id, e)
        return ""
    evs = [e for e in tail if e.get("feedback")]
    if not evs:
        return ""
    recent = evs[-6:]
    likes = sum(1 for e in recent if e["feedback"] == 1)
    dislikes = sum(1 for e in recent if e["feedback"] == -1)
    if likes == 0 and dislikes == 0:
        return ""
    if dislikes > likes:
        return "[Обратная связь игрока] Игроку недавно НЕ понравились твои ответы. Измени стиль: " \
               "короче и конкретнее, меньше общих слов и повторов, продвигай действие вперёд."
    if likes > 0:
        return "[Обратная связь игрока] Последние ответы игроку понравились — продолжай в том же духе " \
               "(живость и стиль как сейчас), но не повторяйся дословно."
    return ""


def drop_engine_examples(prompt: str) -> tuple[str, int]:
    """Убрать ВЫВОДНЫЙ блок примеров `<<ENGINE>>` (сессия 63, последний рычаг усечения).

    Примеров в промпте ~25 строк, и это самая избыточная его часть: сами директивы
    перечислены строкой «Доступные ключи…» и описаны в правилах 7–25, а формат требует
    ОДНУ строку в конце ответа. Когда жертвовать уже нечем (лор/RAG/карточки/сводки/история
    срезаны, а окно узкое — обычная локальная 4B-модель), лучше потерять демо-строки, чем
    потерять историю или состояние: молчаливый переполнение окна = обрезанные ответы (A2,
    сессия 34). Заголовки блоков и правила не трогаются: режутся ТОЛЬКО строки, начинающиеся
    с маркера, поэтому нумерация правил (_GATED_RULES/trim_prompt) не сдвигается.
    """
    kept = [ln for ln in prompt.splitlines() if not ln.startswith("<<ENGINE>>")]
    if len(kept) == len(prompt.splitlines()):
        return prompt, 0
    out = "\n".join(kept)
    return out, est_tokens(prompt) - est_tokens(out)


def build_messages(world: dict, setting: dict, action: str,
                   recent_events: list[dict], summaries: list[dict],
                   rag_chunks: list[str], entity_cards: list[dict] | None = None,
                   persona: str | None = None, use_tools: bool = False,
                   # D5 (аудит 38): аннотация врала — функция возвращает ПАРУ (messages, meta)
                   # с сессии 34 (A2), а signature осталась «list[dict]»: mypy не мог проверить
                   # вызывающий код и молча принимал dict там, где ждали список.
                   lore: list[str] | None = None) -> tuple[list[dict], dict]:
    """Собирает единственный system-message хода. Гарантирует, что промпт НЕ вылезет за
    контекст модели (A2, сессия 34): если после бюджета recent всё равно перебор —
    в порядке меньшей важности выкидываются лор-чанки → воспоминания RAG → сводки →
    карточки, а затем самая старая недавняя история. Каждый отказ — в лог (правило 14).
    Возвращает (messages, meta): meta содержит размеры секций и число выкинутого."""
    sys_prompt = build_system_prompt(world, setting, persona=persona, use_tools=use_tools,
                                     action=action)
    fb_note = feedback_style_note(world["id"])
    if fb_note:
        sys_prompt += "\n\n" + fb_note

    lore = list(lore or [])
    rag_chunks = list(rag_chunks or [])
    # Сводки в промпт — больше при большом контексте (жалоба: «всегда 9 фактов»).
    # A3 (сессия 34): окно считалось здесь, но routers/core._summaries() резал список до 3
    # раньше — масштабирование было мёртвым. Теперь количество задаёт ЭТА функция, а вызывающий
    # передаёт сводки с запасом.
    max_summaries = dynamic_memory_k(world, 3, 10)
    summaries = list(summaries or [])[-max_summaries:]
    recent_events = list(recent_events or [])

    def assemble():
        mem_blocks = []
        if lore:
            mem_blocks.append("[ЛОР МИРА — факты вселенной (из библиотеки мира, актуально)]\n"
                              + "\n\n".join(lore))
        if summaries:
            smry_text = "\n\n".join(f"Сводка {i + 1}: {s['content']}"
                                     for i, s in enumerate(summaries))
            mem_blocks.append(f"[ПРОШЛЫЕ СОБЫТИЯ (сводки)]\n{smry_text}")
        if rag_chunks:
            mem_blocks.append("[ВОСПОМИНАНИЯ (из долгосрочной памяти)]\n"
                              + "\n---\n".join(f"• {c[:400]}" for c in rag_chunks))
        if entity_cards:
            mem_blocks.append("[КАРТОЧКИ СУЩНОСТЕЙ — кто/что рядом и важно]\n"
                              + format_entity_cards(entity_cards))
        parts = [sys_prompt]
        if mem_blocks:
            parts.append("\n\n".join(mem_blocks))
        recent = format_memory(recent_events)
        if recent:
            parts.append(f"[НЕДАВНЯЯ ИСТОРИЯ]\n{recent}")
        parts.append(f"[ДЕЙСТВИЕ ИГРОКА]\n{action}")
        return "\n\n".join(parts)

    ctx = world_context_tokens(world)
    mtok = int(world_gen_settings(world).get("max_tokens") or get_config().max_tokens)
    hard = max(1500, ctx - mtok)          # что реально можно отправить модели
    meta: dict = {"trimmed": [], "overflow_tokens": 0}
    content = assemble()
    over = est_tokens(content) - hard

    # Жертвуем в порядке ОТ наименее важного к наиболее важному, и ПОСТЕПЕННО (отдать
    # половину лора лучше, чем отдать весь): промпт может быть больше плана всего на
    # пару сотен токенов, а «молча обрезать» — ровно то, от чего защищаемся.
    # Порядок: лор → RAG-воспоминания → карточки (кроме текущей локации/активных квестов)
    # → сводки → самая старая недавняя история (её уже помнят сводки и RAG).
    def half(lst: list) -> int:
        return max(1, len(lst) // 2)

    def _scene_cards(cards: list) -> list:
        """Карточки, которые не выкидываются НИКОГДА: текущая локация и активные квесты."""
        return [c for c in cards
                if c.get("kind") == "location" or c.get("entity_key") == setting.get("current_location")
                or (c.get("kind") == "quest" and
                    ((setting.get("quests") or {}).get(c.get("entity_key")) or {}).get("status") == "active")]

    def shrink_cards(keep: int) -> None:
        """Оставить `keep` карточек, но NEVER без текущей локации/активных квестов —
        без них модель не знает, кто в сцене (select_relevant_entities уже отсортировал
        по важности, поэтому режем с хвоста)."""
        nonlocal entity_cards
        cards = list(entity_cards or [])
        if len(cards) <= keep:
            return
        must = _scene_cards(cards)
        rest = [c for c in cards if c not in must]
        entity_cards = (must + rest)[:max(keep, len(must))]

    plan = (
        ("лор", lambda: len(lore) > 1, lambda: lore.__delitem__(slice(half(lore), None))),
        ("лор", lambda: bool(lore), lambda: lore.clear()),
        ("воспоминания", lambda: len(rag_chunks) > 1, lambda: rag_chunks.__delitem__(slice(half(rag_chunks), None))),
        ("воспоминания", lambda: bool(rag_chunks), lambda: rag_chunks.clear()),
        ("карточки", lambda: len(entity_cards or []) > 4,
         lambda: shrink_cards(max(2, len(entity_cards or []) // 2))),
        # Сессия 63: последний рычаг по карточкам — раньше каскад останавливался на «не хуже
        # 4 штук», а ниже 4 резать не хотел, и в мире с узким окном (локальная 4B) промпт
        # переполнял окно МОЛЧА: overflow-предупреждение есть, но играть невозможно. Карточки
        # НЕ-сцены (дальние NPC/завершённые квесты) — самое дешёвое, чем можно пожертвовать:
        # их уже покрывают RAG и сводки, а «кто рядом» (карточки сцены) остаётся всегда.
        ("карточки(не из сцены)", lambda: len(entity_cards or []) > len(_scene_cards(entity_cards or [])),
         lambda: shrink_cards(len(_scene_cards(list(entity_cards or []))))),
        ("сводки", lambda: len(summaries) > 1, lambda: summaries.__delitem__(slice(0, half(summaries)))),
        ("сводки", lambda: bool(summaries), lambda: summaries.clear()),
    )
    for label, has_it, shrink in plan:
        if over <= 0:
            break
        if has_it():
            before = est_tokens(content)
            shrink()
            content = assemble()
            after = est_tokens(content)
            over = after - hard
            if before != after:
                meta["trimmed"].append(f"{label}(-{before - after})")
    # крайний случай: самая старая недавняя история
    while over > 0 and len(recent_events) > 2:
        recent_events.pop(0)
        content = assemble()
        over = est_tokens(content) - hard
        meta["trimmed"].append("недавняя история")
    # Сессия 63, ПОСЛЕДНИЙ рычаг: когда память уже срезана до дня, а примеров механики в
    # промпте больше 20 строк — жертвуем ИМИ, а не игрой: директивы и без демо-строк
    # перечислены в блоке формата, а без истории/состояния мир теряет связность.
    if over > 0:
        sys_prompt, saved_eg = drop_engine_examples(sys_prompt)
        if saved_eg:
            content = assemble()
            over = est_tokens(content) - hard
            meta["trimmed"].append(f"примеры механики(-{saved_eg})")
    if over > 0:
        meta["overflow_tokens"] = over
        log.warning("промпт мира %s больше окна модели на %d токенов даже после усечения — "
                    "уменьши «Размер контекста»/«Max токенов» в настройках мира",
                    world.get("id"), over)
    if meta["trimmed"]:
        log.info("промпт усечён под окно (%s): −%d токенов сверх плана",
                 ", ".join(meta["trimmed"][:6]), sum(
                     int(x.split("-")[1].rstrip(")")) for x in meta["trimmed"]
                     if "-" in x and x.split("-")[1].rstrip(")").isdigit()))
    return [{"role": "system", "content": content}], meta


# ══════════════════════════════════════════════════════════════
# Директивы <<ENGINE>>: парсинг + применение
# ══════════════════════════════════════════════════════════════
# Модель в tools-режиме иногда «выпевает» вызов как текст вместо настоящего tool_call:
#   game_engine({"roll": ...})  /  game_engine { ... }  /  game_engine(...)
# Раньше здесь жили ENGINE_RE и GAME_ENGINE_TEXT_RE (жадный `\{.*\}`) — они удалены:
# ни одна не использовалась вне split_engine, а greedy-захват в новых форматах вреден
# (доезжал до последней `{` в ответе). Парсер теперь ходит по тексту балансирующим
# сканером скобок — см. _iter_json_objs/_cut_engine_blocks (сессия 36, п.4).

# Начало «служебной» части ответа: сюда уже не нужны прожимальные токены игроку
_ENGINE_START = re.compile(r"(<<ENGINE>>|game_engine\s*\()", re.IGNORECASE)
_ENGINE_MARKERS = ("<<ENGINE>>", "game_engine(")

# Каноны директив движка — из Цепочки обязанностей (mechanics.DIRECTIVE_CHAIN).
# По ним определяем «голые» JSON-блоки механики, вставленные моделью в текст.
try:  # осторожно: при циклическом импорте не ронять модуль — просто отключим эвристику
    from .mechanics import DIRECTIVE_CHAIN as _DIRECTIVE_CHAIN

    ENGINE_KEYS = frozenset(k for h in _DIRECTIVE_CHAIN for k in h.keys)
except Exception:  # pragma: no cover - фолбэк для раннего импорта
    ENGINE_KEYS = frozenset()
# Директивы вне Цепочки обязанностей: `roll` обрабатывается ядром хода отдельно (кубы),
# но в ответе модели это ровно такой же служебный блок, и он обязан вырезаться из текста.
ENGINE_KEYS = ENGINE_KEYS | {"roll", "dice"}


def _is_engine_obj(obj) -> bool:
    """Похож ли разобранный JSON на набор директив движка?

    Требование строгое: непустой dict, у которого ХОТЯ БЫ ОДИН ключ — известная директива,
    и НЕИЗВЕСТНЫХ ключей не больше одного. Иначе проза с фигурными скобками
    (`вырезал {рубиново} слово`) могла бы утащить в механику кусок текста."""
    if not isinstance(obj, dict) or not obj:
        return False
    known = [k for k in obj if str(k) in ENGINE_KEYS]
    unknown = [k for k in obj if str(k) not in ENGINE_KEYS]
    return bool(known) and len(unknown) <= 1


def _iter_json_objs(text: str):
    """Перебирает (start, end, dict) всех СБАЛАНСИРОВАННЫХ JSON-объектов в тексте.

    Балансировка скобок нужна вместо жадного регулярки на `{.*}`: при прозе после
    механики (`… game_engine({...}) и он ушёл.` + вторая строка с `{`) greedy-регэксп
    захватывал всё до последней `{…}` в ответе и склеивал текст. Строки/экранирование
    учитываются, чтобы `{` внутри строкового значения не ломало баланс скобок."""
    n = len(text)
    i = 0
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        j = i
        while j < n:
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        break
            j += 1
        if depth != 0 or j >= n:
            return  # обрывок без закрытия — дальше искать нечего
        try:
            obj = json.loads(text[i:j + 1])
        except Exception:
            obj = None
        if obj is not None:
            yield i, j + 1, obj
        i = j + 1


def _merge_directives(base: Optional[dict], extra: dict) -> dict:
    """Сливает несколько блоков механики из одного ответа (поздние дополняют ранние;
    списочные директивы инвентаря складываем, а не затираем)."""
    out = dict(base or {})
    for k, v in extra.items():
        if k in out and isinstance(v, list) and isinstance(out[k], list):
            out[k] = list(out[k]) + list(v)
        else:
            out[k] = v
    return out


def find_engine_start(reply: str) -> int:
    """Индекс, где начинается служебный блок механики (<<ENGINE>> или game_engine(...)),
    либо -1, если его нет. Используется в стриминге, чтобы не показывать механику игроку."""
    m = _ENGINE_START.search(reply)
    return m.start() if m else -1


def engine_tail_hold(text: str) -> int:
    """Сколько последних символов стрима нельзя отдавать игроку: они могут оказаться
    началом служебного маркера («…к <» / «…game_eng»). Без этого хвоста в чате на
    долю секунды мелькают обрывки `<<ENGINE>>`/`game_engine(` (сессия 36, п.4).
    В конце стрима буфер дописывается целиком, так что текст не теряется."""
    low = (text or "").lower()
    hold = 0
    for marker in _ENGINE_MARKERS:
        for k in range(min(len(marker) - 1, len(low)), 0, -1):
            if low.endswith(marker[:k]):
                hold = max(hold, k)
                break
    if re.search(r"[\"\u00ab<\[]\s*$", text or ""):
        hold = max(hold, 2)
    return hold


def split_engine(reply: str) -> tuple[str, Optional[dict]]:
    """Вырезает механику из ответа и возвращает (чистый текст, директивы JSON или None).

    Форматы, которые реально выдаёт модель:
      1) <<ENGINE>>{...}             — локальный prompt-формат
      2) game_engine({...}) текстом  — «выпетый» вызов вместо tool_call (tools-режим)
      3) голый JSON-блок {...}       — литрпг/аудит, в конце ответа ИЛИ в середине абзаца

    Реализация — ОДИН сканер (сессия 36, п.4) вместо трёх правил «от маркера до конца
    строки». Блок механики вырезается вместе с маркером и закрывающей скобкой вызова,
    а проза ДО и ПОСЛЕ остаётся у игрока. Прежняя версия: жадный `\\{.*\\}` захватывал
    текст до последней `{...}` в ответе и склеивал куски, а блок в середине абзаца
    вообще не распознавался и оставался в чате.
    """
    text = (reply or "").strip()
    if not text:
        return text, None

    # 1) где начинается служебная зона (первый из маркеров)
    starts = []
    m_eng = re.search(r"<<ENGINE>>", text)
    if m_eng:
        starts.append(m_eng.start())
    m_ge = re.search(r"game_engine\s*\(", text, re.IGNORECASE)
    if m_ge:
        starts.append(m_ge.start())
    zone = min(starts) if starts else None

    prose: list[str] = []
    directives: Optional[dict] = None

    def _add(obj: dict) -> None:
        nonlocal directives
        directives = _merge_directives(directives, obj)

    if zone is None:
        head, tail = text, ""
    else:
        head, tail = text[:zone], text[zone:]

    # 2) из «служебной зоны» вынимаем все directive-блоки; остальное — проза после вызова
    if tail:
        rest, objs = _cut_engine_blocks(tail)
        for o in objs:
            _add(o)
        if not objs:
            # JSON не разобрался (обрывок/кривые кавычки) — пробуем «ленивый» парсер,
            # но текст при этом не выбрасываем: пусть останется в чате
            parsed = _fallback_parse(rest)
            if parsed:
                _add(parsed)
        rest = _strip_engine_markers(rest)
        if rest.strip():
            prose.append(rest.strip())

    # 3) в «прологе» тоже мог стоять блок механики (маркер оказался позже или потерялся)
    if head:
        head_clean, objs = _cut_engine_blocks(head)
        for o in objs:
            _add(o)
        head_clean = _strip_engine_markers(head_clean)
        if head_clean.strip():
            prose.insert(0, head_clean.strip())

    clean = "\n\n".join(prose).strip()
    return clean, (directives or None)


def _cut_engine_blocks(segment: str) -> tuple[str, list[dict]]:
    """Вырезает из текста все JSON-объекты, похожие на директивы движка.

    Возвращает (остаток текста, список разобранных директив-словарей в порядке встречи).
    Скобки балансируются вручную (строки/экранирование учитываются): жадный регэксп
    не годится, он «доезжает» до последней `{` в ответе и уносит прозу между блоками.
    """
    objs: list[dict] = []
    out: list[str] = []
    pos = 0
    for s, e, obj in _iter_json_objs(segment):
        if not _is_engine_obj(obj):
            continue
        out.append(segment[pos:s])
        pos = e
        objs.append(obj)
        # закрывающая скобка вызова game_engine(...), приклеенная к блоку
        while pos < len(segment) and segment[pos] in " \t\n":
            pos += 1
        if pos < len(segment) and segment[pos] == ")":
            pos += 1
    out.append(segment[pos:])
    return "".join(out), objs


def _strip_engine_markers(text: str) -> str:
    """Убирает оставшиеся маркеры <<ENGINE>> / game_engine( (повторные, вложенные,
    недописанные), сохраняя текст вокруг них."""
    if not text:
        return text
    text = re.sub(r"<<ENGINE>>\s*", "", text)
    text = re.sub(r"game_engine\s*\(\s*", "", text, flags=re.IGNORECASE)
    return text


def _fallback_parse(text: str) -> Optional[dict]:
    """Ленивый парсер вида key:value; key:{...} — запасной вариант."""
    out: dict[str, Any] = {}
    for m in re.finditer(r'(\w+)\s*[:=]\s*("(?:[^"\\]|\\.)*"|[-\d\.]+|true|false|null|\{.*?\}|\[.*?\])', text):
        k, v = m.group(1), m.group(2)
        try:
            v = json.loads(v)
        except Exception:
            v = v.strip('"')
        if k not in out:
            out[k] = v
    return out or None


def parse_tool_args(args):
    """Нормализует JSON-аргументы инструмента game_engine.
    1) вложенные JSON-строки парсятся; 2) «голые» дельты игрока (gold/hp/...) оборачиваются в player;
    3) add_item/remove_item из строки/dict приводятся к [{name, qty}]."""
    def walk(x):
        if isinstance(x, str):
            s = x.strip()
            if s.startswith(("{", "[")):
                try:
                    return walk(json.loads(s))
                except Exception:
                    return x
            return x
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        return x
    if isinstance(args, str):
        try:
            d = json.loads(args)
        except Exception:
            return {}
    else:
        d = args
    d = walk(d) if isinstance(d, dict) else {}
    bare = {"gold", "hp", "mp", "xp", "stats", "actions"}
    if any(k in d for k in bare) and "player" not in d:
        d["player"] = {k: d.pop(k) for k in list(d) if k in bare}
    for k in ("add_item", "remove_item"):
        if k in d:
            v = d[k]
            if isinstance(v, str):
                d[k] = [{"name": v, "qty": 1}]
            elif isinstance(v, dict):
                item = dict(v)
                if "item" in item:
                    item["name"] = item.pop("item")
                if "count" in item:
                    item["qty"] = item.pop("count")
                d[k] = [item]
    # item_update: модель часто пишет {item: ..., desc: ...} — имя приводим к name,
    # одиночный dict оборачиваем в список (ожидание ItemHandler).
    if "item_update" in d:
        iu = d["item_update"]
        if isinstance(iu, dict):
            iu = [iu]
        if isinstance(iu, list):
            fixed = []
            for x in iu:
                if isinstance(x, dict):
                    x = dict(x)
                    if "item" in x and "name" not in x:
                        x["name"] = x.pop("item")
                    fixed.append(x)
            d["item_update"] = fixed
    return d


_MECH_WORDS = (
    "покуп", "плат", "отдам", "прода", "золот", "монет", "деньг", "купл", "цен",
    "удар", "атак", "бьёт", "бью", "стреля", "нанеси", "урон", "леч", "вылеч", "зель", "эликсир",
    "напад", "напада", "аттак", "убить", "ранить", "бой", "деру", "дуэль", "схват", "хват",
    "нож", "оружие", "меч", "кинжал", "пистол", "удари", "атаку", "замах", "выпад",
    "возьм", "беру", "подбер", "забрать", "карман", "кошелёк", "краду", "вору", "стащ", "сумк","мешок",
    "откро", "сундук", "дверь", "дверц", "люк", "замок", "взлом",
    "иду", "пойду", "направляюсь", "вхожу", "входим", "следу", "перехожу", "входит", "идти",
    "скрыт", "пряч", "проверк", "пыта", "убежда", "торг", "бартер", "обмен",
    "квест", "задани", "нанимаю", "эффект", "отрав", "благослов", "восстанов",
    "отдых", "сплю", "выпь", "ем", "съе", "куша", "хил", "мана", "выносливость",
)


def mech_trigger(text: str) -> bool:
    """Эвристика: стоит ли запускать аудит-проход директив (действие явно тянет механику)."""
    t = (text or "").lower()
    return any(w in t for w in _MECH_WORDS)


# D2 (аудит 38): отсюда удалён мёртвый хелпер has_effectful_directives() вместе с его
# _EFFECT_KEYS — аудит триггерится предикатом mech_trigger() по тексту действия, а не по
# «есть ли механика в ответе», и ни один вызов в проекте не обращался к этим именам
# (проверено grep'ом по backend/, tests/, scripts/). Возвращать их «на будущее» незачем:
# будущее — это директивы mechanics/apply_directives, они там и переписываются.

def _flag_line(flags: dict, titles: Any = None, limit: int = 14) -> str:
    """Строка флагов для промптов: `key=value` + (если мастер/сюжет дали) человекочитаемое
    название из `setting["flag_titles"]`. Машинный ключ СОХРАНЯЕТСЯ: по нему сверяет факты
    судья и на него ссылается директива `flag` (закон 2 — только показ, ничего не меняем).
    """
    ft = titles if isinstance(titles, dict) else {}
    parts = []
    for k, v in list(flags.items())[:limit]:
        t = str(ft.get(k) or "").strip()
        parts.append(f"{k}={v}" + (f" ({t})" if t else ""))
    return "; ".join(parts)


def audit_messages(setting: dict, text: str, reply: str = "") -> list[dict]:
    """Компактный промпт для аудит-прохода: без полного контекста — только базовое состояние
    (чтобы модель считала ДЕЛЬТЫ, а не абсолюты) + критичные факты (живые/мёртвые NPC, флаги) +
    последний обмен игрок/рассказчик. Помимо механики, судья проверяет, что директивы не
    противоречат установленным фактам (см. «Аудит логики.txt», Стратегия А/п.1)."""
    p = setting.get("player") or {}
    inv = ", ".join(i.get("name", "") for i in (p.get("inventory") or [])[:8]) or "пусто"
    cur = (
        f"Текущее состояние игрока: золото {p.get('gold', 0)}, HP {p.get('hp', 0)}/{p.get('max_hp', 0)}, "
        f"MP {p.get('mp', 0)}/{p.get('max_mp', 0)}, уровень {p.get('level', 1)}, "
        f"класс «{p.get('class') or 'нет'}», професс. «{p.get('profession') or 'нет'}», инвентарь: {inv}."
    )
    # Критичные факты-истины мира: жив/мёртв NPC, флаги. Судья должен учитывать их при механиках.
    npc = setting.get("npc") or {}
    npc_facts = []
    for k, v in list(npc.items())[:12]:
        if is_player_npc(k, v if isinstance(v, dict) else None):
            continue   # п.13: игрок — не персонаж (свои цифры судье даны строкой выше)
        name = (v.get("name") or k) if isinstance(v, dict) else k
        alive = v.get("alive", True) if isinstance(v, dict) else True
        npc_facts.append(f"{name} — {'мёртв' if not alive else 'жив'}")
    flags = setting.get("flags") or {}
    flag_line = _flag_line(flags, setting.get("flag_titles"), limit=14) if flags else "нет"
    facts_line = []
    if npc_facts:
        facts_line.append("NPC: " + "; ".join(npc_facts))
    facts_line.append("Флаги-истины: " + flag_line)
    # Уже существующие враги и их HP (чтобы аудит применял дельты к верным целям)
    enemies = setting.get("enemies") or {}
    enemy_line = ""
    if enemies:
        es = [f"{v.get('name', k)}({v.get('hp',0)}/{v.get('max_hp',0)} HP)" for k, v in list(enemies.items())[:8]]
        enemy_line = "\nВраги в бою: " + "; ".join(es)
    critical = "\n".join(facts_line) + enemy_line
    user = f"{text}"
    if reply:
        user += f"\n\nОтвет рассказчика (что случилось в сюжете): {reply[:900]}"
    return [
        {"role": "system", "content": (
            "Ты — движок RPG. По действию игрока и ответу рассказчика определи механические изменения. "
            "Верни ТОЛЬКО валидный JSON с директивами (как для game_engine). Все числа — ДЕЛЬТЫ относительно текущего состояния "
            "(например gold: -3 значит минус 3). Если механики нет — верни {}. "
            "Ключи: player ({gold, hp, mp, xp, stats, actions, level}), race_change, class, class_rank, class_evolve, secondary_class, secondary_rank, profession, "
            "skill_add, skill_rank, skill_remove, title, reputation, effect_add (name — человекочитаемо, desc — описание; периодическая убыль за ход: damage/heal — HP, mp_damage/mp_heal — MP; длительность — turns числом, у бессрочного turns нет), effect_remove, add_item/remove_item, item_update {{name, desc}}, "
            "add_item, remove_item, enemy_add, enemy_apply, enemy_remove, quest, quest_done, quest_advance, quest_choose, npc_set, npc_kill, "
            "location_add, location_update, move, flag, time, weather, game_over, "
            "shop_add, shop_remove, shop_update, trade_buy, trade_sell, gather, craft_learn, craft_remove, craft, "
            "companion_add, companion_remove, companion_update, companion_apply, "
            "ability_add, ability_remove, ability_update, ability_use, progress_add, achievement_add, "
            "roll ({expr, mod, dc, label}). "
            "БОЙ: если рядом с игроком появляется противник, которого ещё нет во «Враги в бою» — заведи его enemy_add {{id, name, hp, dmg}} "
            "(и может enemy_apply с уроном за этот ход). Задавай врагу БАЗОВУЮ HP/урон как если бы он был \"нормальный для этой локации\" с точки зрения сеттинга — "
            "движок автоматически отмасштабирует его силу под уровень игрока (см. ⚖️ Сложность в состоянии). Урон игрока по врагу — enemy_apply (ДЕЛЬТА, минус), урон по игроку — "
            "player {{hp: -N}}. Не удаляй врага (enemy_remove), пока его не добили\nили не убили в тексте. "
            "Если предмет/эффект игрока должен дать механическое последствие (урон, золото, бафф) по описанию — "
            "включи его (player {{gold/hp}}, effect_add). "
            "Учитывай КРИТИЧНЫЕ ФАКТЫ ниже: не выдавай директивы, противоречащие им "
            "(например не оживляй мёртвого NPC, не открывай уже сломанную дверь)."
        )},
        {"role": "user", "content": f"{cur}\n{critical}\n\nДействие игрока: {user}\nВерни JSON директив."},
    ]


# Судья логики (LLM-as-a-Judge, фоновая проверка противоречий)
# Стратегия Б из «Аудит логики.txt»: запускается асинхронно после хода, не блокирует ответ;
# найденную ошибку модели НЕ переписывает, а оформляет системным сообщением-«искажением
# реальности» (сюжетный поворот) — ошибка ИИ становится фичей мира (мистика/безумие/иллюзия).
# ══════════════════════════════════════════════════════════════
def judge_messages(setting: dict, action: str, reply: str, lang: str = "ru") -> list[dict]:
    """Компактный промпт судьи: критичные факты мира + биография/роль персонажа + последний обмен.
    Помимо фактов-противоречий (вердикт+twist) судья сверяет БИОГРАФИЮ с расой/классом/профессией/
    навыками и, если есть грубое несоответствие, предлагает корректировку. Возвращает сообщения LLM."""
    lang_instr = ("На русском языке." if lang == "ru" else "In English.")
    p = setting.get("player") or {}

    # Критичные факты для сверки: жив/мёртв NPC, флаги-истины, текущая локация, ключевые цифры
    npc = setting.get("npc") or {}
    npc_facts = []
    for k, v in list(npc.items())[:14]:
        if is_player_npc(k, v if isinstance(v, dict) else None):
            continue   # п.13: игрока сверяют по портрету, а не как персонажа окружения
        name = (v.get("name") or k) if isinstance(v, dict) else k
        alive = v.get("alive", True) if isinstance(v, dict) else True
        location = v.get("location") if isinstance(v, dict) else None
        npc_facts.append(f"{name} — {'мёртв' if not alive else 'жив'}" +
                         (f" (в локации: {location})" if location else ""))
    npc_line = "; ".join(npc_facts) if npc_facts else "нет"

    flags = setting.get("flags") or {}
    flag_line = _flag_line(flags, setting.get("flag_titles"), limit=14) if flags else "нет"

    loc = (setting.get("locations") or {}).get(setting.get("current_location", "start"), {})
    # Портрет персонажа (биография + роль) — судья сверяет их согласованность
    skills = p.get("skills") or {}
    skills_line = "; ".join(f"{n} ({sk.get('rank','F') if isinstance(sk,dict) else sk})"
                             for n, sk in list(skills.items())[:10]) if skills else "нет"
    inv = p.get("inventory") or []
    inv_line = "; ".join(str(it.get("name") if isinstance(it, dict) else it) for it in inv[:20]) if inv else "пусто"
    bio = (p.get("identity") or "").strip()[:1400]
    facts = (
        f"ПЕРСОНАЖ: {p.get('name','Путник')} | HP {p.get('hp',0)}/{p.get('max_hp',0)} | Золото {p.get('gold',0)}"
        f" | Локация: {loc.get('name','?')}\n"
        f"РОЛЬ: раса «{p.get('race') or '—'}», класс «{p.get('class') or '—'}», "
        f"профессия «{p.get('profession') or '—'}», уровень {p.get('level',1)}\n"
        f"НАВЫКИ: {skills_line}\n"
        f"ИНВЕНТАРЬ: {inv_line}\n"
        f"БИОГРАФИЯ: {bio or '—'}\n"
        f"ЖИВЫЕ/МЁРТВЫЕ NPC: {npc_line}\n"
        f"ФЛАГИ-ИСТИНЫ: {flag_line}"
    )
    system = (
        "Ты — строгий, но аккуратный судья логики текстовой RPG. Твои ДВЕ задачи (возврат — ОДИН валидный JSON):\n"
        "ЗАДАЧА 1 — противоречия фактам: сверь последний ответ рассказчика с фактами мира (кто жив/мёртв, "
        "флаги-истины, локация, ИНВЕНТАРЬ). Лови только ГРУБЫЕ противоречия: мёртвый NPC говорит, сломанная "
        "дверь открыта, персонаж в двух местах, персонаж достаёт/использует предмет, которого у него НЕТ в "
        "ИНВЕНТАРЕ (гранату, оружие, снадобье и т.п.). Не будь педантичным и не «души» креативность: "
        "необычные, но непротиворечивые повороты — норма; но нельзя позволять герою доставать из воздуха "
        "предметы (гранаты/реактор/бомбу), которых в ИНВЕНТАРЕ нет — это грубое противоречие.\n"
        "ЗАДАЧА 2 — согласованность персонажа: сверь БИОГРАФИЮ с РОЛЬЮ (раса/класс/профессия/уровень) и НАВЫКАМИ. "
        "Если роль/навыки явно противоречат биографии и миру (напр. в современном мире у персонажа фэнтезийная раса, "
        "или по биографии персонаж новичок, а уровень 99 / навыки ветерана) — предложи корректировку РОЛИ/навыков в "
        "духе биографии и мира. Если всё согласовано — corrections пустой.\n"
        "ВЕРНИ ТОЛЬКО валидный JSON, без пояснений:\n"
        "{\"verdict\": \"ok\", \"corrections\": {}} если противоречий фактам нет,\n"
        "{\"verdict\": \"issue\", \"issue\": \"<короткое описание противоречия>\", "
        "\"twist\": \"<1-2 предложения на языке мира, оборачивающие ошибку в атмосферный сюжетный поворот, "
        "не отрицая случившееся грубо>\", \"corrections\": {...}}\n"
        "corrections — опционально авто-правка согласованности: {\"race\": \"...\", \"class\": \"...\", \"profession\": \"...\", \"level\": <целое>, \"skills\": [...]} "
        "(skills — массив объектов {name, rank} плюс опциональные kind, desc, mp_cost; пустые/ненужные поля опускай; если нечего править — {}). "
        f"{lang_instr}"
    )
    user = f"ДАННЫЕ МИРА И ПЕРСОНАЖА:\n{facts}\n\nДЕЙСТВИЕ ИГРОКА: {action}\n\nОТВЕТ РАССКАЗЧИКА: {reply[:1400]}\n\nВынеси вердикт и, при необходимости, correction."
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _clamp_ratio(value, total, default: float = 1.0) -> float:
    """Доля value/total в [0,1] для восстановления пропорции при смене максимума.
    Если максимум не задан или нулевой — считаем «полная шкала» (default)."""
    try:
        v = float(value if value is not None else 0)
        t = float(total) if total else 0.0
    except (TypeError, ValueError):
        return default
    if t <= 0:
        return default
    return max(0.0, min(1.0, v / t))


def _apply_judge_corrections(setting: dict, corr: dict) -> list[str]:
    """Применяет корректировку согласованности от судьи к состоянию игрока (раса/класс/профессия/уровень/навыки).
    Возвращает читаемые системные сообщения о внесённых изменениях."""
    msgs: list[str] = []
    if not isinstance(corr, dict) or not corr:
        return msgs
    p = setting.get("player") or {}
    ensure_player_schema(p)
    race = str(corr.get("race") or "").strip()
    if race and (p.get("race") or "").strip() != race:
        msgs += apply_directives(setting, {"race_change": race})
    cls = str(corr.get("class") or "").strip()
    if cls and (p.get("class") or "").strip() != cls:
        msgs += apply_directives(setting, {"class": cls})
    prof = str(corr.get("profession") or "").strip()
    if prof and (p.get("profession") or "").strip() != prof:
        msgs += apply_directives(setting, {"profession": prof})
    try:
        lv = int(corr.get("level") or 0)
        if 1 <= lv <= 99 and lv != (p.get("level") or 1):
            # Сессия 36, п.26: раньше ставилось hp/max_hp, mp/max_mp — то есть
            # «бесплатное лечение» из служебной правки. Явная прокачка рассказчиком
            # (директива player.level) восстанавливает героя СОЗНАТЕЛЬНО: это сюжетный
            # ритуал/дар, он и сообщается как праздник. Коррекция судьи — не повод
            # лечить: иначе раненый герой дёргал бы судью правкой уровня.
            # Пропорция сохраняется: max_hp/max_mp пересчитает recalc_derived ниже, а
            # доля остаётся той же (плюс прежний баг — залечивание по СТАРЫМ максимумам).
            ratio_hp = _clamp_ratio(p.get("hp"), p.get("max_hp"))
            ratio_mp = _clamp_ratio(p.get("mp"), p.get("max_mp"))
            was_hp = int(p.get("hp", 0) or 0)
            p["level"] = lv
            recalc_derived(p, setting.get("_difficulty", "normal"))
            new_hp = int(round(p.get("max_hp", 100) * ratio_hp))
            # жив был — жив и остаётся (округление не должно убивать),
            # мёртв был — не воскрешаем (решает рассказчик, закон 3)
            if was_hp > 0 and new_hp < 1:
                new_hp = 1
            p["hp"] = max(0, min(new_hp, int(p.get("max_hp", 100))))
            p["mp"] = max(0, min(int(round(p.get("max_mp", 50) * ratio_mp)),
                                 int(p.get("max_mp", 50))))
            msgs.append(f"Уровень: {p.get('level',1)}")
    except Exception as e:
        # кривой уровень от судьи не должен ронять остальные корректировки
        log.warning("коррекция уровня судьей не применена: %s", e)
    sk = corr.get("skills")
    if isinstance(sk, list) and sk:
        for s in sk:
            if isinstance(s, dict) and str(s.get("name") or "").strip():
                sn = str(s["name"]).strip()[:60]
                p.setdefault("skills", {})[sn] = {"rank": norm_rank(s.get("rank") or "F"),
                                                    "kind": str(s.get("kind") or "спец")[:60],
                                                    "desc": str(s.get("desc") or ""),
                                                    "mp_cost": int(s.get("mp_cost", 0) or 0)}
                msgs.append(f"Навык: {sn}")
    recalc_derived(p)
    return msgs


# ══════════════════════════════════════════════════════════════
# Провидение (Божественный арбитр) — ручное воззвание игрока.
# Игрок считает, что рассказчик ошибся (не выдал предмет, не списал урон/золото и т.п.), и
# «воззывает к божеству». Та же модель, но ОТДЕЛЬНЫЙ строгий промпт: она проверяет, была ли
# ошибка на самом деле, и если да — придумывает сюжетное объяснение («искажение реальности»)
# и возвращает корректирующие директивы, которые применяются штатным движком.
# Это мета-уровень: не заменяет рассказчика, а чинит мир поверх, превращая отладку в игровую
# механику. Кулдаун ограничивается в роутере (worlds.py), чтобы не было «попросить у богов золото».
# ══════════════════════════════════════════════════════════════
def divine_messages(world: dict, setting: dict, complaint: str,
                    action: str = "", reply: str = "", lang: str = "ru") -> list[dict]:
    """Строгий промпт Провидения: текущее состояние мира + жалоба игрока + последний обмен
    (действие рассказа троковые и ответ) , чтобы судья понял, о какой ошибке речь.
    Возвращает сообщения LLM; ответ — JSON {"declined":bool, "twist":str, "directives":{...}}."""
    lang_instr = ("На русском языке." if lang == "ru" else "In English.")
    facts = format_state(setting)
    exchange = ""
    if action or reply:
        exchange = "Последний обмен (на что игрок указывает):\n"
        if action:
            exchange += f"[Игрок] {action[:600]}\n"
        if reply:
            exchange += f"[Рассказчик] {reply[:1000]} \n"
    system = (
        "Ты — Абсолютный Арбитр Реальности этого игрового мира. Игрок воззвал к тебе, подозревая "
        "разрыв в ткани мироздания (ошибку Рассказчика: не выдали предмет, не списали потраченный "
        "ресурс, применили урон/последствия, которых не было, и т.п.).\n"
        "Твоя задача — НЕ ругать и НЕ наказывать. Впиши исправление в сюжет, если ошибка реальна.\n"
        "1. Сверь жалобу с состоянием мира (ИНВЕНТАРЬ, HP/MP/золото, квесты, флаги) и последним обменом. "
        "Действительно ли есть противоречие? Будь строг и ПОСЛЕДОВАТЕЛЕН: не потакай игроку ради жалости, "
        "различай реальную ошибку Рассказчика и простое недовольство игрока."
        "2. Если ошибка РЕАЛЬНА (не выдали предмет, не списали ресурс, HP/золото не в балансе, или есть "
        "настоящее противоречие установленных фактов: расхождение курсов/титулов/идентичности/положения персонажа) — "
        "ТЫ КОРРЕКТОР МИРА: ИСПРАВЬ его, а не оставляй как есть. Придумай мистическое/техногенное/логичное объяснение "
        "(временная петля, глюк Системы, вмешательство высших сил, двойная реальность) и ОБЯЗАТЕЛЬНО верни исправляющие "
        "директивы (add_item/remove_item, player hp/mp/gold, effect_add/remove, quest, flag, title и т.п. — как в game_engine), "
        "чтобы после твоего ответа мир стал СОГЛАСОВАННЫМ. При противоречии курса/титула/статуса — приведи их в порядок "
        "директивой title (например «Студентка третьего курса…» вместо «первокурсница») или установи flag, фиксирующий "
        "верный факт; если в инвентаре/золоте явный разрыв — добавь/спиши предметы; если предмет выдан без описания, "
        "а свойства уже есть в тексте — допиши их item_update {{name, desc}}, не выдавая предмет заново. "
        "Объяснение — ТОЛЬКО в twist, а реальная "
        "правка — в directives. Не подстраивайся под каприз, но РЕАЛЬНОЕ противоречие обязано быть устранено."
        "3. Отказ (decline: true) применяй ТОЛЬКО если жалоба — каприз без фактической ошибки (просто «дай золото/предмет, "
        "потому что хочу») или если состояние уже корректно и игрок ошибается. Не списывай подлинное противоречие на «так задумано»: "
        "если есть расхождение — это повод ИСПРАВИТЬ, а не оправдать.\n"
        # Сессия 36, п.2: Провидение — КОРРЕКТОР, а не генератор награды. Без этой рамки
        # та же модель, что и у рассказчика, охотно «возвращала» утраченное по несколько раз
        # за вечер (жалоба → add_item/gold), и это был фарм, а не починка реальности.
        "4. Границы исправления (важно): ты ВОССТАНАВЛИВАЕШЬ то, что уже было названо в обмене "
        "(предмет, деньги, урон — ровно в том количестве, о котором речь), и НЕ выдаёшь ничего "
        "сверх упомянутой потери. Запрещено: новое золото «в подарок», лишние предметы, лечение "
        "сверх справедливой компенсации, прокачка уровня/статов, готовые квесты с наградой. "
        "Если для согласованности достаточно флага/титула/квеста — правь только их.\n"
        "5. Будь краток: twist — 1–2 предложения. Отвечай ЯЗЫКОМ МИРА (персонаж слышит «голос из пустоты»).\n"
        "ВЕРНИ ТОЛЬКО валидный JSON без пояснений:\n"
        "{\"decline\": false, \"twist\": \"<сюжетное объяснение>\", \"directives\": {..директивы..}}\n"
        "или {\"decline\": true, \"twist\": \"<голос, что игрок ошибся>\", \"directives\": {}}.\n"
        "directives — такой же формат, как game_engine: player {hp/mp/gold}, add_item/remove_item [{{name,qty,desc}}], "
        "item_update {{name, desc}} (правка уже выданного предмета), "
        "quest/quest_done, flag, effect_add/effect_remove, move и т.п. Все числа как у рассказчика (дельты). "
        f"{lang_instr}"
    )
    user = (f"СОСТОЯНИЕ МИРА:\n{facts}\n\n" +
            exchange +
            f"ЖАЛОБА ИГРОКА: {complaint}\n\nВынеси решение (decline / correction) в JSON.")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _divine_grants(directives: dict) -> str:
    """Сводка «тяжёлых» выдач Провидения для лога: положительно изменённые золото/HP/MP
    и добавленные предметы. Пустая строка, если правка ресурсам не касается."""
    parts: list[str] = []
    pl = directives.get("player")
    if isinstance(pl, dict):
        for key in ("gold", "hp", "mp"):
            try:
                v = int(pl.get(key) or 0)
            except (TypeError, ValueError):
                v = 0
            if v > 0:
                parts.append(f"{key}+{v}")
    items = directives.get("add_item")
    if isinstance(items, dict):
        items = [items]
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict) and it.get("name"):
                parts.append(f"item:{it['name']}\u00d7{it.get('qty', 1)}")
    return ", ".join(parts)


async def divine_intervene(world_id: int, world: dict, setting: dict, complaint: str,
                           action: str = "", reply: str = "", lang: str = "ru",
                           provider: dict | None = None) -> dict | None:
    """Провидение: проверяет жалобу, правит мир директивами (если ошибка реальна) и возвращает:
    {"decline": bool, "twist": str, "directives": dict, "sys_msgs": list[str], "state": setting}
    Возвращает None при сбое модели (2 неудачных парсинга) — вызывающий покажет ошибку.
    Меняет setting на месте (применяет корректирующие директивы).

    D2 (аудит 41, сессия 65): полем `state` вызывающий БОЛЬШЕ НЕ пользуется — записью в БД
    ведёт `routers/worlds.py::divine`, которое перечитывает свежее состояние и повторно
    применяет `directives` к нему (снимок начала запроса затирал правки фоновых агентов).
    `state` оставлен как снимок «что моделилось на входе» для тестов/отладки.
    """
    if not (complaint or "").strip():
        return None
    msgs = divine_messages(world, setting, complaint, action=action, reply=reply, lang=lang)
    # A6 (аудит 41): раньше здесь лежало замыкание `_parse()` с двумя `except Exception:
    # return None` — оно НЕ вызывалось ни разу (ответ разбирает `llm_json_tool`, который
    # сам пишет warning), т.е. было не «тихой веткой», а мёртвым кодом, маскирующим
    # несуществующие ошибки. Удалено; все реальные выходы отсюда логируются ниже.

    data = None
    try:
        data = await llm_json_tool(
            msgs, "divine_verdict",
            "Решение Провидения по жалобе игрока: decline (отклонить) или correction (исправить мир).",
            {
                "decline": {"type": "boolean", "description": "true если ошибки игрока нет и мир корректен"},
                "twist": {"type": "string", "description": "Системное сообщение-поворот на языке мира (1-2 предложения)"},
                "directives": {"type": "object", "description": "Корректирующие директивы движка (если decline=false)"},
            },
            ["decline"], provider=provider, temperature=0.3, max_tokens=600, max_attempts=2)
    except Exception as e:
        log.warning("Провидение: вызов LLM (world %s): %s", world_id, e)
        return None
    if data is None:
        log.warning("Провидение: невалидный JSON дважды (world %s)", world_id)
        return None

    decline = bool(data.get("decline"))
    twist = _cut_words(str(data.get("twist") or "").strip(), 420)
    if not twist:
        twist = ("Ты слышишь далёкий голос: \u201cХм... В этом мире всё как должно быть, путник.\u201d"
                 if decline else "Ты слышишь далёкий голос: \u201cПространство колеблется.\u201d")
    sys_msgs: list[str] = []
    directives = None
    if not decline:
        raw = data.get("directives")
        directives = normalize_directives(raw) if isinstance(raw, dict) else None
        if directives:
            # ПРАВИЛО 3/15 и сессия 36, п.2: Провидение может выдать ресурсы, и это
            # единственный «кододоступный» контроль над его щедростью — каждое «тяжёлое»
            # дарование (золото/лечение/предметы) оседает в логе, чтобы фарма не случилось
            # незаметно. Ограничение выдачи — на уровне промпта и кулдауна в роутере.
            heavy = _divine_grants(directives)
            if heavy:
                log.warning("Провидение (world %s) ВЫДАЛО ресурсы: %s — жалоба: %.120s",
                            world_id, heavy, complaint,
                            extra={"fields": {"divine_grants": heavy}})
            try:
                sys_msgs = apply_directives(setting, directives)
            except Exception as e:
                log.warning("Провидение: применение директив (world %s): %s", world_id, e)
                sys_msgs = []
    return {"decline": decline, "twist": twist, "directives": directives,
            "sys_msgs": sys_msgs, "state": setting}


async def logic_judge(world_id: int, setting: dict, action: str, reply: str,
                      lang: str = "ru", provider: dict | None = None) -> dict | None:
    """Фоновая проверка логики хода. Возвращает dict с полями:
    - twist: текст системного сообщения-«искажения» (или None, если противоречий фактам нет),
    - corrections: авто-правка согласованности {race/class/profession/level/skills} (или None),
    либо None, если судья не ответил/не нашёл ничего. При невалидном JSON — один повтор с
    пониженной температурой, затем фолбэк-пропуск с логированием (не тихий)."""
    if not (action or "").strip() or not (reply or "").strip():
        return None
    msgs = judge_messages(setting, action, reply, lang=lang)
    # A6 (аудит 41): мёртвое замыкание `_parse()` с тихими `return None` удалено —
    # разбор JSON делает `llm_json_tool` (он и пишет warning), оба отказа ниже логируются.

    def _to_result(data: dict) -> dict | None:
        result: dict = {"twist": None, "corrections": None}
        corr = data.get("corrections")
        if isinstance(corr, dict) and corr and any(corr.get(k) for k in ("race", "class", "profession", "level", "skills")):
            result["corrections"] = corr
        if str(data.get("verdict") or "").strip().lower() == "issue":
            twist = _cut_words(str(data.get("twist") or "").strip(), 400)
            if twist:
                issue = _cut_words(str(data.get("issue") or "").strip(), 180)
                body = twist + (f" (\u201c{issue}\u201d)" if issue else "")
                result["twist"] = f"🌫 Реальность искажается. {body}"
        if result["twist"] is None and result["corrections"] is None:
            return None
        return result

    data = None
    try:
        data = await llm_json_tool(
            msgs, "logic_judge_verdict",
            "Сверка хода с фактами мира: verdict issue (противоречие) или ok; при issue — twist и issue-описание, при рассинхроне роли — corrections.",
            {
                "verdict": {"type": "string", "description": "ok | issue"},
                "twist": {"type": "string", "description": "Системное сообщение-искажение реальности (если issue)"},
                "issue": {"type": "string", "description": "Кратко что за противоречие"},
                "corrections": {"type": "object", "description": "Авто-правка роли: race/class/profession/level/skills"},
            },
            ["verdict"], provider=provider, temperature=0.2, max_tokens=420, max_attempts=2,
            temperature_retry=0.05)
    except Exception as e:
        log.warning("судья логики: вызов LLM (world %s): %s", world_id, e)
        return None
    if data is None:
        log.warning("судья логики: невалидный JSON дважды (world %s)", world_id)
        return None
    return _to_result(data)


# Инструмент (function calling) для механики — вместо маркера <<ENGINE>>
# Используется для облачных openai_compat провайдеров; у локальных (llamacpp/ollama)
# остаётся промпт-формат <<ENGINE>> (слабые модели калечат tool_calls).
# ══════════════════════════════════════════════════════════════
GAME_ENGINE_TOOL = [{"type": "function", "function": {
    "name": "game_engine",
    "description": (
        "Изменяет механическое состояние мира по заключительной части ответа рассказчика: "
        "player (hp/mp/gold/xp/stats/actions/level), race_change, class, class_rank, class_evolve, "
        "secondary_class, secondary_rank, profession, skill_add/skill_rank/skill_remove, title, "
        "reputation, effect_add (name — человекочитаемо, desc — описание; урон/лечение за ход: damage/heal — HP, mp_damage/mp_heal — MP; срок — turns числом, без него эффект бессрочен)/effect_remove, add_item/remove_item (всегда с desc), item_update {{name, desc, weight, value, note}}, enemy_add/enemy_apply/enemy_remove, "
        "quest/quest_done (можно {id, next} — авто-цепочка), quest_advance (ступень), quest_choose (ветка), "
        "quest_success/quest_fail ({id, reason, next} — ИТОГ квеста: success/failed), quest {{id, timer: {{name, turns, desc}}}} (дедлайн квеста), "
        "npc_set (id,name,mood,alive,desc,faction,location,schedule,money,notes,voice)/npc_kill, location_add/location_update/location_remove (id — мир теряет место: разрушено/затоплено/закрыто навсегда; текущая локация игрока под удалением нельзя)/move, flag {name,value,title [человеческое имя]}, time, weather, "
        "enemy_effect_add/enemy_effect_remove (статусы НА врагах: {{id,name,turns,damage,desc}} — ХРАНИЛИЩЕ, сами не тикают: урон по врагам только твоим enemy_apply), "
        "enemy_mark ({{id, position, initiative, target, stance}} — тактические метки для порядка боя), "
        "timer_add/timer_remove (таймеры-дедлайны мира: {name, turns, desc}), equip/unequip (экипировка по слотам: у предмета должен быть slot), "
        "needs (потребности/рассудок: {голод: {value: -10}}), board_add (доска объявлений {title, text}), faction_rank (звание во фракции {faction, rank}), "
        "date (календарь {day, month, season}), vision_add (видение в очередь {text, hint}), trigger_vision (разыграть видение), "
        "shop_add/shop_remove/shop_update, trade_buy/trade_sell, gather, craft_learn/craft_remove/craft, "
        "companion_add/companion_remove/companion_update/companion_apply, "
        "ability_add/ability_remove/ability_update/ability_use (универсальные способности — заклинания/техно/псионика), "
        "progress_add ({убийства/квесты/локации…}), achievement_add {name, desc}, "
        "quest_remove (id или [id,...] — стереть квест из мира совсем: арка СТАЛА НЕВОЗМОЖНОЙ "
        "(участники мертвы, город уничтожен), а не «выполнена»/«провалена»), "
        "world_evolve ({what, why} — ты ЯВНО отходишь от канвы стартового сюжета: что берёшь/что "
        "бросаешь и какое действие игрока к этому привело; запись попадёт в состояние и в следующий промпт), "
        "roll (expr/mod/dc/label — бросок куба), game_over. "
        "Когда игрок взял/нашёл/подобрал/получил предмет — обязательно используй add_item; когда использовал/выбросил — remove_item. "
        "Аргументы — валидный JSON с любым набором этих ключей."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
}}]


def use_tools_for_provider(prov: dict | None) -> bool:
    """Включаем tool-calling только для надёжных OpenAI-совместимых провайдеров.

    D5: параметр допускает None — вызовы идут из мест, где снимок провайдера мог не
    заполниться (per-world настройки старых миров), и `bool(prov)` уже это обрабатывает."""
    if not prov:
        return False
    return bool(prov.get("enabled", True)) and prov.get("id") == "openai_compat"


async def llm_json_tool(messages: list[dict], tool_name: str, tool_desc: str,
                        properties: dict, required: list[str],
                        provider: dict | None = None,
                        temperature: float = 0.4, max_tokens: int = 900,
                        max_attempts: int = 2,
                        temperature_retry: float | None = None) -> dict | None:
    """Надёжный JSON-вызов LLM через function calling (tools): модель ОБЯЗАНА вернуть валидный
    JSON-аргумент инструмента, а не текст. Для reasoner-моделей (deepseek-v4-flash и т.п.) это
    радикально надёжнее, чем «верни JSON текстом» (иначе битый/пустой JSON, обрывы).

    На повторе температура может понижаться (temperature_retry) — для судьи/провидения,
    где второй проход должен быть «холоднее» и стабильнее.
    Возвращает dict аргументов (распарсенный) или None после max_attempts неудач.
    Если провайдер не поддерживает tools (не openai_compat) — фолбэк на обычный вызов с
    ретраем и поиском {...} в тексте."""
    from . import llm
    tool = [{"type": "function", "function": {
        "name": tool_name,
        "description": tool_desc,
        "parameters": {"type": "object", "properties": properties,
                        "required": required, "additionalProperties": False},
    }}]
    for attempt in range(max_attempts):
        temp = temperature if attempt == 0 else (temperature_retry if temperature_retry is not None else temperature)
        try:
            if use_tools_for_provider(provider):
                tc_out: list[dict] = []
                await llm.complete(messages, temperature=temp, max_tokens=max_tokens,
                                   provider=provider, tools=tool, tool_choice="required",
                                   tool_calls_out=tc_out)
                if tc_out:
                    args = (tc_out[0].get("arguments") or "").strip()
                    if args:
                        data = json.loads(args)
                        if isinstance(data, dict) and data:
                            return data
                # пустой tool_call — повторим
            else:
                # фолбэк без tools: просим JSON текстом, ищем {...}
                out = (await llm.complete(messages, temperature=temp,
                                          max_tokens=max_tokens, provider=provider) or "").strip()
                m = re.search(r"\{.*\}", out, re.DOTALL)
                if m:
                    try:
                        data = json.loads(m.group(0))
                        if isinstance(data, dict) and data:
                            return data
                    except Exception:
                        pass
        except Exception as e:
            log.warning("llm_json_tool(%s) попытка %d/%d: %s", tool_name, attempt + 1, max_attempts, e)
    return None


# ══════════════════════════════════════════════════════════════
# Кубы
# ══════════════════════════════════════════════════════════════
# A1 (аудит 41): границы кубика. Без них `d0` давал ValueError → HTTP 500 на ход,
# `0d6` — «бросок без броска» (total 0), а директива модели `99999999d20` — ~2 с
# и список на ~100 МБ (DoS хода + событие `dice` на миллионы символов).
DICE_COUNT_MAX = 100        # больше ста кубиков за один бросок никто не кидает
DICE_SIDES_MIN = 2          # d0/d1 бессмысленны
DICE_SIDES_MAX = 1000       # d1000 — уже фантастика
DICE_MOD_MAX = 1000         # |бонус| и |мод| сверх этого — ошибка или попытка сломать итог

# D7 (аудит 41): одна регулярка на «распознали куб» (roll_expr) и на «какой куб бросали»
# (roll_outcome). Иначе исход спорил с клампом: startswith("d20") не видел ни `1d20`,
# ни `D20+3`, ни `d20 +5`, и ветвь «крит-провал по dc−10» не срабатывала никогда.
_DICE_RE = re.compile(r"\s*(\d*)d(\d+)\s*(?:([+-])\s*(\d+))?\s*", re.IGNORECASE)


def _dice_shape(expr: Any) -> tuple[int, int] | None:
    """Форма кубика `(число кубиков, грани)` из любого написания: `d20`, `1d20`, `D20+3`.

    Мусор (`2d6zz`, `d`, пусто) → None. `d0`/`d1` дают форму (1, 0)/(1, 1) — но ниже матчится
    только одиночный d20, а `roll_expr` такой ввод и так меняет на фолбэк d20. Пробелы
    терпим — но только вокруг `d` и знака мода: полный совпад строки, а не префикс
    (иначе `re.match` съедал «хвост» выражения молча).
    """
    m = _DICE_RE.fullmatch(str(expr).strip())
    if not m:
        return None
    return int(m.group(1) or "1"), int(m.group(2))


def _dice_fallback(mod: int, why: str, expr: Any) -> dict:
    """Невалидный ввод кубика → детерминированный d20 (ход не роняем), но НЕ молча."""
    log.warning("roll_expr: %r — %s; бросаем фолбэк d20", expr, why)
    mod = _clamp_dice_int(mod)
    rolls = [random.randint(1, 20)]
    return {"rolls": rolls, "total": rolls[0] + mod, "expr": "d20", "mod": mod, "note": why}


def _clamp_dice_int(value: int, limit: int = DICE_MOD_MAX) -> int:
    return max(-limit, min(limit, int(value)))


def roll_expr(expr: str, mod: int = 0) -> dict:
    """Бросает куб вида '2d6+1', 'd20', 'd100'. Возвращает {rolls, total, expr, note}.

    `expr` в ответе — фактически брошенное выражение (после клампов), его и показываем;
    `note` — чем исходный ввод пришлось подрезать/заменить (пусто, если всё честно).
    """
    m = _DICE_RE.fullmatch(str(expr).strip())
    if not m:
        return _dice_fallback(mod, "выражение не распознано", expr)
    count = int(m.group(1) or "1")
    sides = int(m.group(2))
    bonus = int(m.group(3) + m.group(4)) if m.group(3) else 0
    notes: list[str] = []
    if count < 1:
        return _dice_fallback(mod, "в выражении ноль кубиков", expr)
    if sides < DICE_SIDES_MIN:
        return _dice_fallback(mod, f"у кубика меньше {DICE_SIDES_MIN} граней", expr)
    if count > DICE_COUNT_MAX:
        notes.append(f"число кубиков подрезано с {count} до {DICE_COUNT_MAX}")
        count = DICE_COUNT_MAX
    if sides > DICE_SIDES_MAX:
        notes.append(f"число граней подрезано с {sides} до {DICE_SIDES_MAX}")
        sides = DICE_SIDES_MAX
    bonus = _clamp_dice_int(bonus)
    mod = _clamp_dice_int(mod)
    rolls = [random.randint(1, sides) for _ in range(count)]
    eff = f"{count}d{sides}" + (f"{bonus:+d}" if bonus else "")
    return {"rolls": rolls, "total": sum(rolls) + bonus + mod, "expr": eff, "mod": mod,
            "note": "; ".join(notes)}


def roll_outcome(total: int, dc: int, expr: str, rolls: list[int] | None = None) -> str:
    """Оценка броска. `rolls` — выпавшие грани из `roll_expr` (источник истины о nat-1).

    D7 (аудит 41): «крит-провал при 1» раньше сравнивал с единицей ИТОГ, в который уже
    вошли мод и бонус куба — `d20+5` на честной 1 (total 6) провала не давал, а для `2d6`
    `total == 1` невозможен в принципе (мёртвая ветка). Натуральная единица считается по
    фактическому броску одного кубика (правило 8 промпта: «критический провал 1» — это
    грань, а не сумма). `rolls` не передан — остаётся прежний слабый признак total == 1.

    «Провал на 10+ хуже сложности» — только для одиночного d20: на 3d6 разброс шире и
    такая планка ссыпалась бы в крит-провалы на каждом среднем провале.
    """
    if total >= dc + 10:
        return "критический успех"
    if total >= dc:
        return "успех"
    if rolls is not None:
        nat_one = len(rolls) == 1 and rolls[0] == 1
    else:
        nat_one = total == 1
    if nat_one:
        return "критический провал"
    shape = _dice_shape(expr)
    if shape and shape[0] == 1 and shape[1] == 20 and total <= dc - 10:
        return "критический провал"
    return "провал"


# ══════════════════════════════════════════════════════════════
async def narrate_roll(world_id: int, label: str, expr: str, mod: int, dc: int,
                       total: int, outcome: str, lang: str = "ru",
                       persona: str | None = None, provider: dict | None = None,
                       action: str = "", situation: str = "") -> str:
    """Второй проход: рассказчик описывает результат броска."""
    lang_instr = ("на русском языке" if lang == "ru" else "in English")
    mod_str = f" с модификатором {mod}" if mod else ""
    # Контекст сцены: что игрок сделал и какой текст уже написан ДО броска.
    # Без этого LLM выдумывает отдельную сцену (напр. «словесный спор» вместо ножевой атаки).
    ctx = f"\nДействие игрока: {str(action)[:200]}" if action else ""
    ctx += f"\nТекст сцены, развернувшейся перед броском: {str(situation)[:1200]}" if situation else ""
    user_msg = (f"Проверка «{label}»: бросок {expr}{mod_str}, сложность {dc}. "
                f"Итог: {total} — {outcome}\n\n"
                f"Опиши, ЧТО ПРОИЗОШЛО в той же сцене — продолжай именно её (выше ты уже начал её описывать). "
                f"Не меняй действие (не превращай атаку/бросок в словесный спор или другую ситуацию), не "
                f"вводи новую сцену сбоку. Речь про ТУ ЖЕ дуэль/действие игрока.{ctx}")
    persona_line = (clip_persona(persona) or "").strip()
    sys_text = (f"{persona_line} " if persona_line else "") + \
        f"Ты — рассказчик RPG. Опиши результат броска {lang_instr}, 1–2 абзаца, " \
        "живо, без пересказа правил. Не упоминай числа (кроме общей оценки). Продолжай ровно ту сцену, " \
        "которая развернулась перед броском (это РЕЗУЛЬТАТ этого же действия), а не новую."
    messages = [
        {"role": "system", "content": sys_text},
        {"role": "user", "content": user_msg},
    ]
    try:
        return (await llm.complete(messages, temperature=0.9, max_tokens=350,
                                   provider=provider)).strip()
    except Exception as e:
        # A6: ход с кубиками не падает, но и не остаётся без объяснения в журнале.
        _agent_quiet("roll", f"описание результата броска не получено: {e}", e)
        return ""

def dynamic_event_messages(world: dict, setting: dict) -> list[dict]:
    """Компактный промпт: одно случайное событие мира в JSON (текст + директивы)."""
    cur = format_state(setting)
    return [
        {"role": "system", "content": (
            "Ты — мастер случайных событий текстовой RPG. По текущему состоянию мира придумай ОДНО короткое "
            "событие (2–4 предложения на языке мира): нападение, смена погоды, появление NPC, находка, "
            "слухи, происшествие в локации — естественное продолжение сеттинга и положения игрока. "
            "НЕ решай за игрока, не двигай его и не заканчивай его историю. "
            "Верни ТОЛЬКО валидный JSON без пояснений и без <<ENGINE>>:"
            " {\"event\": \"текст события\", \"directives\": {\"weather\": \"...\", ...}}"
            " Директивы — опциональные механические изменения: weather, time, enemy_add {id,name,hp,dmg}, "
            "npc_set {id,name,mood}, add_item [{name,qty}], flag {name,value,title}, player {{gold: -5, hp: -3}}, "
            "quest, effect_add. Если изменений не нужно — \"directives\": {}."
        )},
        {"role": "user", "content": "Текущее состояние:" + chr(10) + chr(10) + cur + chr(10) + chr(10) + "Сгенерируй одно событие."},
    ]


async def generate_dynamic_event(world: dict, setting: dict,
                                 provider: dict | None = None) -> tuple[str, dict] | None:
    """LLM генерирует случайное событие мира. Возвращает (текст события, директивы) или None."""
    try:
        data = await llm_json_tool(
            dynamic_event_messages(world, setting), "dynamic_event",
            "Одно короткое случайное событие мира: текст + опциональные механические директивы.",
            {
                "event": {"type": "string", "description": "Текст события на языке мира (2-4 предложения)"},
                "directives": {"type": "object", "description": "Опциональные директивы: weather, time, enemy_add, npc_set, add_item, flag, player, quest, effect_add"},
            },
            ["event"], provider=provider, temperature=0.9, max_tokens=350, max_attempts=2)
        if not data:
            _agent_quiet("event", "модель не вернула валидный JSON события дважды")
            return None
        ev = str(data.get("event") or "🌍 В мире что-то произошло…").strip()[:700]
        raw_dir = data.get("directives")
        if isinstance(raw_dir, list):
            raw_dir = raw_dir[0] if raw_dir else {}
        directives = normalize_directives(raw_dir) if isinstance(raw_dir, dict) else {}
        return ev, directives
    except Exception as e:
        _agent_quiet("event", f"случайное событие не сгенерировано: {e}", e)
        return None


# ══════════════════════════════════════════════════════════════
# Автономный «мастер» (сессия 25): выводит игрока из тупика.
# ══════════════════════════════════════════════════════════════
_STOPWORDS_RU = {"и", "в", "во", "на", "с", "за", "к", "от", "по", "о", "об", "у", "из",
                 "я", "он", "она", "оно", "мы", "вы", "они", "это", "тот", "что", "как",
                 "бы", "не", "а", "но", "его", "её", "их", "меня", "тебя", "мне", "тебе",
                 "себя", "чтобы", "или", "ну"}


def _norm_words(text: str) -> list[str]:
    """Нормализация действия игрока в список значимых слов (нижний регистр, без знаков)."""
    return [w for w in re.findall(r"[а-яa-zё]{3,}", (text or "").lower()) if w not in _STOPWORDS_RU]


def _action_similar(a: str, b: str, threshold: float = 0.72) -> bool:
    """Похожи ли два действия (доля общих значимых слов)."""
    wa, wb = set(_norm_words(a)), set(_norm_words(b))
    if not wa or not wb:
        return False
    inter = len(wa & wb)
    return inter / max(len(wa), len(wb)) >= threshold


def master_stuck_reason(actions: list[str], active_quests: int, turns: int) -> str | None:
    """Детерминированный признак «застревания» игрока. Возвращает причину или None.
    - repetition: в последних ходах игрок повторяет почти одно и то же действие;
    - drifting: сыграно много ходов без единого активного квеста (бродит без направления).
    Случайное/нормальное разнообразие НЕ даёт причину (чтобы «мастер» не вмешивался почём зря)."""
    acts = [(a or "").strip() for a in (actions or []) if (a or "").strip()]
    recent = acts[-6:]
    if len(recent) >= 3:
        reps = 0
        for i in range(len(recent)):
            for j in range(i + 1, len(recent)):
                if _action_similar(recent[i], recent[j]):
                    reps += 1
        if reps >= 2:
            return "repetition"
    if turns >= 15 and active_quests <= 0 and len(acts) >= 3:
        return "drifting"
    return None


def master_messages(setting: dict, reason: str, recent_text: str, lang: str = "ru") -> list[dict]:
    """Промпт автономного мастера: один сюжетный шаг, выводящий игрока из тупика."""
    cur = format_state(setting)
    lang_instr = "На русском языке." if lang == "ru" else "In English."
    reason_desc = {
        "repetition": "игрок уже несколько ходов повторяет одно и то же действие (топчется на месте) — нужен свежий зацеп или сюжетный поворот",
        "drifting": "игрок долго бродит без направления и без активных квестов — нужна новая цель/квест",
    }.get(reason, reason)
    return [
        {"role": "system", "content": (
            lang_instr + chr(10) +
            "Ты — автономный мастер (сюжетный режиссёр) текстовой RPG. Игрок «застрял»: " + reason_desc + ". "
            "Придумай ОДИН следующий шаг, который выводит его из тупика и даёт интересную цель: "
            "новый квест (через директиву quest), сюжетный поворот/зацеп, слух или NPC-подсказку, "
            "смену фокуса. НЕ решай за игрока, НЕ двигай его тело и НЕ заканчивай его историю. "
            "Впиши поворот в сеттинг, не ломая уже установленные факты и лор. "
            "Верни ОДИН валидный JSON без пояснений и без <<ENGINE>>, вида: "
            "mode: quest или narrative, title: название квеста, text: короткое системное сообщение "
            "на языке мира (2-4 предложения), directives: опциональные изменения "
            "(например quest, npc_set, add_item, flag)"
        )},
        {"role": "user", "content": recent_text + chr(10) + chr(10) + cur + chr(10) + chr(10) + "Сгенерируй шаг (JSON)."},
    ]


async def generate_master_nudge(setting: dict, reason: str, recent_text: str, lang: str = "ru",
                                provider: dict | None = None) -> tuple[str, dict] | None:
    """LLM-проход автономного мастера. Возвращает (сообщение, директивы) или None при сбое."""
    try:
        data = await llm_json_tool(
            master_messages(setting, reason, recent_text, lang), "master_step",
            "Один сюжетный шаг, выводящий игрока из тупика: текст + опциональные директивы.",
            {
                "text": {"type": "string", "description": "Системное сообщение на языке мира (2-4 предложения)"},
                "directives": {"type": "object", "description": "Опциональные директивы: quest, npc_set, add_item, flag"},
            },
            ["text"], provider=provider, temperature=0.9, max_tokens=300, max_attempts=2)
        if not data:
            _agent_quiet("master", "мастер не вернул валидный JSON шага дважды")
            return None
        txt = str(data.get("text") or "").strip()[:700]
        if not txt:
            _agent_quiet("master", "пустой текст шага мастера (JSON без `text`)")
            return None
        raw_d = data.get("directives")
        if isinstance(raw_d, list):
            raw_d = raw_d[0] if raw_d else {}
        directives = normalize_directives(raw_d) if isinstance(raw_d, dict) else {}
        return txt, directives
    except Exception as e:
        _agent_quiet("master", f"шаг автономного мастера не сгенерирован: {e}", e)
        return None


# ══════════════════════════════════════════════════════════════
# Боевой ИИ врагов (сессия 26): тактические решения для живых врагов.
# ══════════════════════════════════════════════════════════════
_ENEMY_AI_MODES = {
    "attack": "продолжает преследовать/рваться в бой (рассказчик ведёт урон в своём ходе)",
    "guard": "занимает оборону/держит дистанцию, выжидает момент",
    "retreat": "отступает/сбегает — движок уберёт врага (enemy_remove)",
    "negotiate": "пытается переговорить/сдаться/выторговать условия (флаг или NPC-сделка)",
    "trap": "устраивает ловушку / ловит из засады (флаг подсветится при следующем действии)",
}


def _living_enemies(setting: dict) -> list[tuple[str, dict]]:
    """Живые враги: [(id, enemy)] с hp>0, отсортированы по убыванию опасности (dmg)."""
    out = []
    for k, v in (setting.get("enemies") or {}).items():
        if isinstance(v, dict) and int(v.get("hp", 0) or 0) > 0:
            out.append((k, v))
    # sorted советский: по убыванию урона, сравниваем int
    out.sort(key=lambda kv: -int(kv[1].get("dmg", 0) or 0))
    return out


def enemy_ai_messages(setting: dict, recent_text: str, lang: str = "ru") -> list[dict]:
    """Промпт боевого ИИ врагов: по живым врагам и ситуации выбери тактический ход (ОДНО решение)."""
    enemies = _living_enemies(setting)
    en_lines = "; ".join(
        f"{v.get('name', k)} HP {v.get('hp',0)}/{v.get('max_hp',v.get('hp',0))} урон {v.get('dmg',0)}"
        +(f" (намерение: {v.get('ai')})" if v.get("ai") else "")
        for k, v in enemies)
    if not en_lines:
        en_lines = "—"
    p = setting.get("player") or {}
    st = effective_stats(p)
    cur = format_state(setting)
    lang_instr = "На русском языке." if lang == "ru" else "In English."
    modes_desc = "; ".join(f"{k} — {v}" for k, v in _ENEMY_AI_MODES.items())
    return [
        {"role": "system", "content": (
            lang_instr + "\nТы — боевой ИИ противника в текстовой RPG. В бою есть живые враги: " + en_lines + ". "
            "Игрок: HP " + str(p.get('hp',0)) + "/" + str(p.get('max_hp',0)) + ", уровень " + str(p.get('level',1))
            + ", статы " + ", ".join(f"{k} {v}" for k, v in st.items()) + ". "
            "В решительном ДЕЙСТВИИ врагов (их ответном ходе) выбери ОДНО разумное тактическое решение "
            "с учётом стиля игрока (агрессивен/осторожен/силён/ранен) и соотношения сил. Доступные режимы: "
            + modes_desc + ". "
            "Верни ОДИН валидный JSON без пояснений и без <<ENGINE>>: "
            "{enemy: id, mode: один из режимов, note: короткое описание хода врага (2-3 предложения на языке мира), "
            "directives: опциональные не-уроновые директивы (enemy_remove для отступления, flag, npc_set, quest, add_item)}"
        )},
        {"role": "user", "content": recent_text + chr(10) + chr(10) + cur + chr(10) + chr(10) + "Ход врагов (JSON):"},
    ]


async def generate_enemy_ai(setting: dict, recent_text: str, lang: str = "ru",
                            provider: dict | None = None) -> tuple[str, str, str, dict] | None:
    """LLM-проход боевого ИИ врагов. Возвращает (enemy_id, текстовое описание, mode, directives)
    или None при сбое/невалидном ответе."""
    try:
        data = await llm_json_tool(
            enemy_ai_messages(setting, recent_text, lang), "enemy_ai_move",
            "Тактический ход врагов в бою: выбор режима + описание + не-уроновые директивы.",
            {
                "enemy": {"type": "string", "description": "id врага"},
                "mode": {"type": "string", "description": "attack | guard | retreat | negotiate | trap"},
                "note": {"type": "string", "description": "Короткое описание хода врага на языке мира (2-3 предложения)"},
                "directives": {"type": "object", "description": "Опциональные не-уроновые директивы: enemy_remove, flag, npc_set, quest, add_item"},
            },
            ["enemy", "mode", "note"], provider=provider, temperature=0.85, max_tokens=260, max_attempts=2)
        if not data:
            _agent_quiet("enemy_ai", "боевой ИИ не вернул валидный JSON хода дважды")
            return None
        eid = str(data.get("enemy") or "").strip()
        if not eid:
            return None
        mode = str(data.get("mode") or "attack").strip().lower()
        if mode not in _ENEMY_AI_MODES:
            mode = "attack"
        txt = str(data.get("note") or data.get("text") or "").strip()[:700]
        if not txt:
            return None
        raw_d = data.get("directives")
        if isinstance(raw_d, list):
            raw_d = raw_d[0] if raw_d else {}
        directives = normalize_directives(raw_d) if isinstance(raw_d, dict) else {}
        return eid, txt, mode, directives
    except Exception as e:
        _agent_quiet("enemy_ai", f"ход боевого ИИ не получен: {e}", e)
        return None


# ══════════════════════════════════════════════════════════════
# Сны/видения (сессия 32): память как сюжетный приём.
# Рассказчик получает старые RAG-факты и оборачивает их в сон/галлюцинацию/сигнал.
# ══════════════════════════════════════════════════════════════
def vision_messages(setting: dict, vision: dict, lang: str = "ru", memories: list[str] | None = None) -> list[dict]:
    """Промпт видения: текст-заготовка мастера + текущее состояние + факты памяти (RAG)."""
    cur = format_state(setting)
    hint = (vision or {}).get("hint", "") if isinstance(vision, dict) else ""
    mem = "\n".join(f"— {m[:200]}" for m in (memories or [])[:6]) or "(память пуста)"
    lang_instr = "На русском языке." if lang == "ru" else "In English."
    return [
        {"role": "system", "content": (
            lang_instr + "\nТы — Рассказчик. Игрок впадает в видение/сон/галлюцинацию/перехваченный сигнал "
            "(память как сюжет: прошлое возвращается). Опиши его как живое сновидение: 2–4 абзаца, "
            "атмосферно, с намёками на прошлое и будущее. Это НЕ дамп фактов — это художественный образ, "
            "в котором звучат эхо прошлых событий (из фактов памяти ниже). Не решай за игрока, не давай "
            "готовых ответов, оставляй тайну. Текст пиши прозой, без механики и без <<ENGINE>>."
        )},
        {"role": "user", "content": (
            ("Подсказка мастера: " + str(hint)[:300] + "\n" if hint else "") +
            "Состояние мира:\n" + cur + "\n\nФакты памяти (эхо прошлого):\n" + mem +
            "\n\nВидение (пиши прозой):"
        )},
    ]


async def generate_vision(setting: dict, vision: dict, lang: str = "ru",
                          provider: dict | None = None,
                          memories: list[str] | None = None) -> str | None:
    """LLM-проход видения (отдельный вызов, не блокирует ход). Возвращает текст видения."""
    try:
        msgs = vision_messages(setting, vision, lang, memories=memories)
        txt = await llm.complete(msgs, provider=provider, temperature=0.9, max_tokens=600)
        txt = (txt or "").strip()
        # вырезаем возможные <<ENGINE>>-хвосты
        idx = find_engine_start(txt)
        if idx >= 0:
            txt = txt[:idx]
        return txt[:1600] or None
    except Exception as e:
        _agent_quiet("vision", f"видение не разыграно: {e}", e)
        return None


_DAMAGE_KEYS = ("player", "enemy_apply", "enemy_add", "companion_apply", "effect_add")


def _strip_damage(directives: dict) -> dict:
    """Боевой ИИ руководит тактикой и НЕ должен наносить урон игроку/врагам напрямую
    (это делает рассказчик в своём ходе, иначе урон задвоится). Убирает из директив
    любые уроновые/призывающие врагов эффекты, оставляя безопасные (flag/npc/quest/add_item)."""
    out = {}
    for k, v in (directives or {}).items():
        k = str(k)
        if k in _DAMAGE_KEYS:
            continue  # пропускаем всё, что может бить/лечить игрока или менять состав врагов
        out[k] = v
    return out


def _apply_enemy_ai(setting: dict, eid: str, mode: str, directives: dict) -> list[str]:
    """Применяет решение боевого ИИ (только тактическое/не-уроновые эффекты; урон игроку
    ведёт рассказчик в своём ходе, чтобы не задвоился). Возвращает сообщения хода."""
    msgs: list[str] = []
    e = (setting.get("enemies") or {}).get(eid)
    if not e:
        return msgs
    if mode == "retreat":
        setting["enemies"].pop(eid, None)
        msgs.append(f"🏃 {e.get('name', eid)} отступает с поля боя.")
        return msgs
    # как только враг попал в огонь — снимаем намерение (оно передано рассказчику через state)
    e["ai"] = mode
    if directives:
        try:
            msgs += apply_directives(setting, _strip_damage(directives))
        except Exception as e:
            # намерение врага записано, а его директивы (флаг/квест/предмет) не применены —
            # без лога это выглядит как «боевой ИИ ничего не делает»
            log.warning("боевой ИИ: директивы врага %s не применены: %s", eid, e, exc_info=True)
    return msgs


def event_chance(turns_since: int, every_turns: int, level: int = 1) -> float:
    """Шанс случайного события: растёт с каждым ходом и с уровнем игрока (динамическая
    сложность — чем выше уровень, тем чаще события), но НИКОГДА не гарантирован
    (потолок 0.5 — гарантии нет даже у порога).
    Базовый уровень 1 даёт прежнее поведение (для обратной совместимости)."""
    if turns_since <= 0 or every_turns <= 0:
        return 0.0
    lvl = max(1, int(level or 1))
    freq = 1.0 + (lvl - 1) * 0.05  # частота: +5% за каждый уровень сверх 1
    return min(0.5, turns_since / (every_turns / freq))


def suggestions_messages(setting: dict, action: str, reply: str) -> list[dict]:
    """Промпт для ИИ-вариантов действий (зависят от ситуации, «бытовые»)."""
    cur = format_state(setting)
    last = f"Действие игрока: {action[:200]}" + chr(10) + f"Ответ рассказчика: {reply[:500]}"
    return [
        {"role": "system", "content": (
            "Ты — соавтор игрока в текстовой RPG. По последнему обмену и текущему состоянию мира предложи "
            "3–4 коротких варианта действий (по 3–9 слов), уместных прямо сейчас: бытовые и поведенческие "
            "(развести костёр, проверить следы, расспросить старуху о севере), исследовательские, социальные, "
            "немного рискованные. Пиши от первого лица («я…»), 3–9 слов, без точки в конце. НЕ предлагай "
            "технические команды (открыть инвентарь, посмотреть карту) и НЕ "
            "дублируй прямые механики (атаковать, купить). Это живой текст действия от лица игрока, в стиле мира. "
            "Верни ТОЛЬКО JSON-массив строк, без пояснений."
        )},
        {"role": "user", "content": cur + chr(10) + chr(10) + last + chr(10) + chr(10) + "Варианты (JSON):"},
    ]


async def generate_suggestions(setting: dict, action: str, reply: str,
                               provider: dict | None = None) -> list[str]:
    """3–4 ситуационных варианта действий для кнопок; при сбое — пустой список.
    Генерация через tools (function calling): модель обязана вернуть JSON-массив строк.
    Для reasoner-моделей (deepseek-v4-flash и т.п.) это надёжнее, чем «верни JSON текстом»
    (иначе битый/пустой массив — фронт держал старые заглушки сюжета)."""
    try:
        data = await llm_json_tool(
            suggestions_messages(setting, action, reply), "suggest_actions",
            "3-4 коротких варианта действий для игрока (от первого лица, 3-9 слов, без точки).",
            {"suggestions": {"type": "array", "items": {"type": "string"},
                              "description": "3-4 варианта действий"}},
            ["suggestions"], provider=provider, temperature=0.9, max_tokens=600, max_attempts=2)
        if not data:
            return []
        items: list[str] = []
        def add(t: str) -> None:
            t = re.sub(r"^[\d\s.\-–—•*\"'«»]+", "", str(t)).strip().strip('"').strip("—–•").strip()
            if t and t not in items:
                items.append(t[:90])
        raw = data.get("suggestions") or data.get("actions") or []
        if isinstance(raw, list):
            for x in raw:
                add(x)
        return items[:5]
    except Exception:
        return []


# Создание мира: вступительная сцена
# ══════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════
# Декомпозиция (сессия 30): память / лор / генерация персонажа
# вынесены в отдельные модули. Ниже — РЕЭКСПОРТ: все внешние вызовы
# narrator.retrieve_memory(...), narrator.apply_character(...) и т.п.
# продолжают работать без изменений (тонкий фасад поверх новых модулей).
# ══════════════════════════════════════════════════════════════
from . import character_generator, lore_retriever, memory as memory_mod  # noqa: E402

# ── память (RAG, сводки, карточки) ──
memory_query_text = memory_mod.memory_query_text
cosine_threshold = memory_mod.cosine_threshold
# исторические приватные имена (совместимость с прежним narrator.py)
_memory_query_text = memory_mod.memory_query_text
_cosine_threshold = memory_mod.cosine_threshold
retrieve_memory = memory_mod.retrieve_memory
index_exchange = memory_mod.index_exchange
index_summary = memory_mod.index_summary
summarize_and_compress = memory_mod.summarize_and_compress
_make_summary = memory_mod._make_summary
_world_main_provider = memory_mod.world_main_provider
compact_entity = memory_mod.compact_entity
format_entity_cards = memory_mod.format_entity_cards
select_relevant_entities = memory_mod.select_relevant_entities
ensure_knowledge_cards = memory_mod.ensure_knowledge_cards
update_entity_cards = memory_mod.update_entity_cards
index_entities = memory_mod.index_entities

# ── лор мира (библия вселенной) ──
seed_lore_from_theme = lore_retriever.seed_lore_from_theme
seed_lore_from_custom = lore_retriever.seed_lore_from_custom
_chunk_lore = lore_retriever._chunk_lore
index_lore_entry = lore_retriever.index_lore_entry
index_all_lore = lore_retriever.index_all_lore
retrieve_lore = lore_retriever.retrieve_lore

# ── генерация персонажа и открытия ──
generate_identity = character_generator.generate_identity
generate_character = character_generator.generate_character
apply_character = character_generator.apply_character
generate_opening = character_generator.generate_opening
_is_modern_world = character_generator._is_modern_world
_cut_words = character_generator._cut_words
