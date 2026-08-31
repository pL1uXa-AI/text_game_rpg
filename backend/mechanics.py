# -*- coding: utf-8 -*-
"""
mechanics.py — RPG-движок директив (GameEngine): применение <<ENGINE>>{...} / tool_calls
`game_engine` к состоянию мира.

Вынесено из narrator.py (раздел «Сложность кода»): `apply_directives` была огромной
(сотни строк, много разрозненной логики по директивам). Здесь собраны:
  * справочники рангов/рас/классов/профессий (RANK_ORDER, RACES, CLASSES, PROFESSIONS);
  * низкоуровневые статовые помощники (ensure_player_schema, effective_stats, recalc_derived…);
  * нормализация директив (normalize_directives → backend/directives.py);
  * применение механики — apply_directives и её приватные помощники.

narrator.py импортирует нужное и публично пробрасывает apply_directives /
normalize_directives / tick_effects / check_profession_advance, чтобы роутеры и тесты
продолжали работать через `narrator.<имя>` без изменений.
"""
from __future__ import annotations

import logging

import re
from typing import Any, Optional

from .directives import normalize as _normalize_directives

log = logging.getLogger("textgame")
log.setLevel(logging.WARNING)

# ══════════════════════════════════════════════════════════════
# Механики сессии 32: потребности/рассудок, слоты экипировки
# ══════════════════════════════════════════════════════════════
# Слоты экипировки: universal — без ограничений. Два предмета в одном слоте — отказ
# («судья возможностей»), что именно надеть решает рассказчик.
EQUIP_SLOTS = ("голова", "торс", "руки", "ноги", "оружие", "вторая рука", "аксессуар", "аксессуары")

# Потребности и рассудок: {имя: (стартовое значение, максимум, убыль за ход)}
# Убыль — «физический движок»: движок тикает и предупреждает, последствия ведёт мастер.
NEEDS_SPECS = {
    "голод": (80.0, 100.0, 0.8),
    "жажда": (80.0, 100.0, 1.2),
    "усталость": (80.0, 100.0, 0.6),
}
MENTAL_SPECS = {
    "рассудок": (80.0, 100.0, 0.3),
    "стресс": (20.0, 100.0, 0.2),  # растёт, не убывает (decay < 0)
    "мораль": (80.0, 100.0, 0.2),
}
NEED_CRITICAL = 25.0   # ниже — предупреждение мастеру
MENTAL_CRITICAL = 25.0  # ниже (для стресса — выше)
NEED_FULL = 85.0        # выше — «сыт/бодр» (подсказка мастеру)


# ══════════════════════════════════════════════════════════════
# RPG-система: ранги, расы, классы, профессии
# ══════════════════════════════════════════════════════════════
RANK_ORDER = ["F", "E", "D", "C", "B", "A", "S", "SS", "SSS", "Z", "ZZ", "ZZZ", "G"]
RANK_TEXT = "F < E < D < C < B < A < S < SS < SSS < Z < ZZ < ZZZ < G"

DEFAULT_STATS = {"сила": 10, "ловкость": 10, "выносливость": 10,
                 "интеллект": 10, "мудрость": 10, "харизма": 10, "удача": 10}
STAT_HINTS = {
    "сила": "физ. урон, груз, силовые проверки",
    "ловкость": "точность, скорость, уклонение, взлом, скрытность",
    "выносливость": "HP, стойкость к ядам/болезням, выносливость в пути",
    "интеллект": "магия, загадки, скорость изучения",
    "мудрость": "воля/дух, маг. защита, сопротивление эффектам, лечение, интуиция",
    "харизма": "убеждение, торговля, лидерство, NPC",
    "удача": "криты, находки, случайные события",
}

RACES = [  # вшитые расы; мастер/рассказчик может придумать свою (race_change с bonus/passive)
    {"name": "человек", "bonus": {"удача": 1, "харизма": 1}, "desc": "универсал без слабостей",
     "passive": {"name": "Адаптивность", "desc": "+10% к опыту за квесты"}},
    {"name": "эльф", "bonus": {"ловкость": 3, "мудрость": 2, "выносливость": -1},
     "desc": "стремительный, острый глаз",
     "passive": {"name": "Острый глаз", "desc": "+20% к восприятию, +5% к крит. урону из лука"}},
    {"name": "дварф", "bonus": {"выносливость": 3, "сила": 2, "ловкость": -2},
     "desc": "крепкий, упёртый",
     "passive": {"name": "Каменная воля", "desc": "сопротивление магии +15%, бонус защиты в тяжёлой броне"}},
    {"name": "орк", "bonus": {"сила": 4, "выносливость": 1, "интеллект": -2},
     "desc": "мощный, горячий",
     "passive": {"name": "Ярость крови", "desc": "при HP<30% урон +25%, защита −10%"}},
    {"name": "зверолюд", "bonus": {"ловкость": 2, "мудрость": 2, "харизма": -1},
     "desc": "кошачьи/волчьи черты",
     "passive": {"name": "Ночное зрение", "desc": "+30% к скрытности и поиску ловушек в темноте"}},
    {"name": "полудемон", "bonus": {"интеллект": 2, "харизма": 2, "мудрость": -2},
     "desc": "тёмное наследие",
     "passive": {"name": "Тёмное наследие", "desc": "+10% маг. урона, −10% к лечению"}},
    {"name": "драконид", "bonus": {"сила": 2, "выносливость": 2, "ловкость": -2},
     "desc": "чешуя и когти",
     "passive": {"name": "Чешуя дракона", "desc": "+5% защиты от физ. урона, +15% к огню"}},
    {"name": "нежить", "bonus": {"интеллект": 2, "харизма": 2, "выносливость": -3},
     "desc": "вампир/скелет",
     "passive": {"name": "Бессмертный", "desc": "без еды, +10% к магии тьмы, свет наносит +15% урона"}},
]
RACE_NAMES = [r["name"] for r in RACES]

CLASSES = [  # вшитые классы; при смене класса выдаётся стартовый навык
    {"name": "Воин", "stat": "сила", "desc": "танк, физ. урон, тяжёлая броня",
     "starter_skill": {"name": "Сильный удар", "rank": "F", "kind": "боевой", "desc": "мощный удар (+урон от силы)"}},
    {"name": "Лучник", "stat": "ловкость", "desc": "дальний бой, криты, скрытность",
     "starter_skill": {"name": "Прицельный выстрел", "rank": "F", "kind": "боевой", "desc": "точный выстрел с бонусом ловкости"}},
    {"name": "Маг", "stat": "интеллект", "desc": "магический урон и контроль",
     "starter_skill": {"name": "Огненный шар", "rank": "F", "kind": "магический", "desc": "огненный снаряд, урон от интеллекта", "mp_cost": 5}},
    {"name": "Вор", "stat": "ловкость", "desc": "скрытность, удар из тени, взлом",
     "starter_skill": {"name": "Удар из тени", "rank": "F", "kind": "боевой", "desc": "огромный урон из скрытности"}},
    {"name": "Жрец", "stat": "мудрость", "desc": "лечение, баффы, защита от нежити",
     "starter_skill": {"name": "Малая молитва", "rank": "F", "kind": "магический", "desc": "исцеляет союзника (+мудрость)", "mp_cost": 4}},
    {"name": "Бард", "stat": "харизма", "desc": "поддержка, дебаффы, дипломатия",
     "starter_skill": {"name": "Песнь вдохновения", "rank": "F", "kind": "социальный", "desc": "бафф союзникам на атаку/защиту", "mp_cost": 3}},
]
CLASS_NAMES = [c["name"] for c in CLASSES]

PROFESSIONS = [  # ремёсла; смена профессии даёт постоянный бафф
    {"name": "Кузнец", "desc": "ковать и чинить оружие/броню", "buff": {"сила": 1}},
    {"name": "Алхимик", "desc": "зелья и эликсиры", "buff": {"интеллект": 1}},
    {"name": "Травник", "desc": "лечебные снадобья, знание ядов", "buff": {"мудрость": 1}},
    {"name": "Охотник", "desc": "следопытство, капканы, дичь", "buff": {"ловкость": 1}},
    {"name": "Шахтёр", "desc": "добыча руды и камней", "buff": {"выносливость": 1}},
    {"name": "Повар", "desc": "сытная еда с эффектами", "buff": {"выносливость": 1}},
    {"name": "Портной", "desc": "шитьё и починка одежды", "buff": {"удача": 1}},
    {"name": "Моряк", "desc": "корабли и навигация", "buff": {"ловкость": 1}},
    {"name": "Книжник", "desc": "языки, свитки, знания", "buff": {"интеллект": 1}},
]
PROFESSION_NAMES = [pr["name"] for pr in PROFESSIONS]

# Действия → профессия (накопление: действия игрока копятся в player.actions,
# при достижении порога профессия меняется сама). Рассказчик увеличивает счётчики
# директивой {"player": {"actions": {"кузнечное дело": 1}}}.
PROF_ACTION_MAP = {  # действие (нижний регистр) → (профессия, порог)
    "кузнечное дело": ("Кузнец", 12), "ковка": ("Кузнец", 8), "ремонт": ("Кузнец", 8),
    "алхимия": ("Алхимик", 12), "зельеварение": ("Алхимик", 8),
    "травничество": ("Травник", 12), "сбор трав": ("Травник", 8),
    "охота": ("Охотник", 12), "следопытство": ("Охотник", 8), "капканы": ("Охотник", 8),
    "горное дело": ("Шахтёр", 12), "кулинария": ("Повар", 12), "готовка": ("Повар", 8),
    "портняжное дело": ("Портной", 12), "шитьё": ("Портной", 8),
    "мореплавание": ("Моряк", 12), "навигация": ("Моряк", 8),
    "книжное дело": ("Книжник", 12), "чтение свитков": ("Книжник", 8), "изучение языков": ("Книжник", 8),
}


def rank_index(rank: str) -> int:
    try:
        return RANK_ORDER.index(str(rank).upper().strip())
    except ValueError:
        return -1


def norm_rank(raw: str = "F") -> str:
    """Приводит ранг к каноническому (буквенному) виду F..G.
    Модель/генератор может вернуть ранг цифрой (1, 2, 3…) — например в навыках
    (первый навык даёт честное "F", а дальнейшие иногда "3", "4").
    Цифра N мапится в ступень RANK_ORDER[N-1] (1→F, 2→E, 3→D, 4→C, 5→B, 6→A, 7→S…)."""
    try:
        s = str(raw or "").strip().upper()
    except Exception:
        return "F"
    if not s:
        return "F"
    if s in RANK_ORDER:
        return s
    if s.isdigit():
        n = int(s)
        if 1 <= n <= len(RANK_ORDER):
            return RANK_ORDER[n - 1]
        return "G" if n > len(RANK_ORDER) else "F"
    # Смешанный/неизвестный ранг — берём первый буквенный символ, если это известная ступень
    return s[0] if s[0] in RANK_ORDER else "F"


def normalize_setting_ranks(setting: dict) -> bool:
    """Нормализует буквенные ранги во всём состоянии мира (после бага, когда
    генератор отдавал цифровые ранги в навыках: 1/2/3… вместо F/E/D…).
    Чинит player.skills / class_rank / secondary_rank и навыки спутников.
    Возвращает True, если что-то изменилось."""
    changed = False
    p = setting.get("player") or {}
    skills = p.get("skills")
    if isinstance(skills, dict):
        for n, sk in list(skills.items()):
            if isinstance(sk, dict):
                nr = norm_rank(sk.get("rank", "F"))
                if nr != str(sk.get("rank", "F")).upper():
                    sk["rank"] = nr
                    changed = True
    for key in ("class_rank", "secondary_rank"):
        cur = p.get(key, "F")
        nr = norm_rank(cur)
        if nr != str(cur).upper():
            p[key] = nr
            changed = True
    for cid, comp in (setting.get("companions") or {}).items():
        if not isinstance(comp, dict):
            continue
        csk = comp.get("skills")
        if isinstance(csk, dict):
            for n, sk in list(csk.items()):
                if isinstance(sk, dict):
                    nr = norm_rank(sk.get("rank", "F"))
                    if nr != str(sk.get("rank", "F")).upper():
                        sk["rank"] = nr
                        changed = True
    return changed


def ensure_player_schema(p: dict) -> None:
    """Достраивает новые RPG-поля игрока в старых сохранениях."""
    st = p.get("stats")
    if not isinstance(st, dict):
        st = {}
    for k, v in DEFAULT_STATS.items():
        st.setdefault(k, int(v))
    p["stats"] = st
    p.setdefault("race", "")
    p.setdefault("class", "")
    p.setdefault("class_rank", "F")
    p.setdefault("secondary_class", "")
    p.setdefault("secondary_rank", "F")
    p.setdefault("profession", "")
    p.setdefault("skills", {})
    p.setdefault("titles", [])
    p.setdefault("reputation", {})
    p.setdefault("effects", {})
    p.setdefault("actions", {})  # накопленные действия → смена профессий (PROF_ACTION_MAP)
    p.setdefault("abilities", {})  # универсальные сверхспособности/умения (магия/техника/псионика)
    p.setdefault("progress", {})    # статистика пути (ходы/убийства/квесты/локации)
    p.setdefault("achievements", list())  # 🏆 достижения {name, desc}
    # Сессия 32: потребности/рассудок и экипировка (универсальные оси ресурсов)
    p.setdefault("needs", {})        # {голод: {value, max, decay}, ...}
    p.setdefault("mental", {})       # {рассудок/стресс/мораль: {value, max, decay}}
    p.setdefault("faction_ranks", {})  # {фракция: звание/должность}
    p.setdefault("equipped", {})     # {слот: имя предмета} — что надето сейчас
    # старый формат навыков {имя: число} → {имя: {rank, kind, desc, mp_cost}}
    new_sk = {}
    for k, v in p["skills"].items():
        if isinstance(v, dict):
            new_sk[k] = {"rank": norm_rank(v.get("rank", "F")), "kind": v.get("kind", "универсальное"),
                         "desc": v.get("desc", ""), "mp_cost": int(v.get("mp_cost", 0) or 0)}
        else:
            lvl = int(v or 1)
            new_sk[k] = {"rank": "F", "level": max(1, lvl), "kind": "универсальное", "desc": "", "mp_cost": 0}
    p["skills"] = new_sk


def effective_stats(p: dict) -> dict:
    """Базовые статы + моды активных эффектов + бонусы экипировки (моды не меняют базу,
    поэтому откат снапшота при перегенерации не ломается)."""
    st = {k: int(v) for k, v in (p.get("stats") or {}).items()}
    for _n, ef in (p.get("effects") or {}).items():
        if not isinstance(ef, dict):
            continue
        mods = ef.get("mods") or {}
        if isinstance(mods, dict):
            for k, v in mods.items():
                if k in st:
                    st[k] = st[k] + int(v)
    # Сессия 32: бонусы экипировки (слоты) — моды к статам
    for _slot, item_name in (p.get("equipped") or {}).items():
        if not item_name:
            continue
        it = next((x for x in (p.get("inventory") or []) if x.get("name") == item_name), None)
        if not isinstance(it, dict):
            continue
        b = it.get("bonus")
        if isinstance(b, dict):
            for k, v in b.items():
                if k in st:
                    st[k] = st[k] + int(v)
    return st


def recalc_derived(p: dict, difficulty: str = "normal") -> None:
    """Производные: max_hp от выносливости, max_mp от интеллекта/мудрости.
    Не понижает максимумы старых сохранений ниже их текущего уровня."""
    hp_map = {"easy": 120, "normal": 100, "hardcore": 80}
    con = int(p["stats"].get("выносливость", 10) or 10)
    it = int(p["stats"].get("интеллект", 10) or 10)
    ws = int(p["stats"].get("мудрость", 10) or 10)
    lvl = int(p.get("level", 1) or 1)
    base_hp = hp_map.get(difficulty, 100) + (con - 10) * 2 + (lvl - 1) * 10
    base_mp = 50 + (it - 10) * 2 + (ws - 10) + (lvl - 1) * 5
    old_hp = p.get("max_hp") or base_hp
    old_mp = p.get("max_mp") or base_mp
    p["max_hp"] = max(old_hp, base_hp)
    p["max_mp"] = max(old_mp, base_mp)
    p["hp"] = min(p.get("hp", p["max_hp"]), p["max_hp"])
    p["mp"] = min(p.get("mp", p["max_mp"]), p["max_mp"])


def _safe_int(v, default=0):
    """Безопасный int(): кривое от LLM (строка/None/мусор) → default, иначе падение хода."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ══════════════════════════════════════════════════════════════
# Динамическая сложность (сессия 24): сила врагов и частота событий
# подстраиваются под уровень игрока.
# ══════════════════════════════════════════════════════════════
_DYNAMIC_SLOPES = {  # прирост множителей за КАЖДЫЙ уровень сверх 1 (по сложности мира)
    "easy":     {"hp": 0.45, "dmg": 0.25},
    "normal":   {"hp": 0.50, "dmg": 0.30},
    "hardcore": {"hp": 0.55, "dmg": 0.40},
}


def dynamic_adversary_scale(player: dict | None = None, difficulty: str = "normal",
                            level: int | None = None) -> dict:
    """Множители силы врагов под текущий уровень игрока (динамическая сложность).

    Игрок растёт с уровнем (+HP/+MP в recalc_derived), поэтому поле может перерасти базу
    «уровня 1», и топор по-прежнему 4 урона станет несерьёзным. Движок сам масштабирует
    предлагаемую рассказчиком HP/урон врага, чтобы угроза не отставала от прокачки.
    На уровне 1 возвращает 1.0 (ничего не меняет). Сложность мира меняет крутизну роста:
    хардкор — враги растут быстрее, лёгкая — медленнее.

    Возвращает: {"hp": float, "dmg": float, "event": float}."""
    lvl = 1
    if player is not None and isinstance(player, dict):
        lvl = int(player.get("level") or 1)
    elif level is not None:
        lvl = int(level or 1)
    lvl = max(1, lvl or 1)
    if lvl <= 1:
        return {"hp": 1.0, "dmg": 1.0, "event": 1.0}
    s = _DYNAMIC_SLOPES.get(str(difficulty), _DYNAMIC_SLOPES["normal"])
    return {
        "hp": round(1.0 + (lvl - 1) * s["hp"], 3),
        "dmg": round(1.0 + (lvl - 1) * s["dmg"], 3),
        "event": round(1.0 + (lvl - 1) * 0.05, 3),  # частота событий растёт слабее
    }


# ══════════════════════════════════════════════════════════════
# Фракции и репутация → геймплей (ступени, связи, реакция)
# ══════════════════════════════════════════════════════════════
_STANDING_RU = [(-100, "Изгой"), (-12, "Заклятый враг"), (-5, "Враг"), (-1, "Недоверие"),
              (1, "Нейтрально"), (5, "Доверие"), (12, "Друг"), (1 << 30, "Союзник")]
_STANDING_EN = [(-100, "Outcast"), (-12, "Sworn enemy"), (-5, "Enemy"), (-1, "Wary"),
              (1, "Neutral"), (5, "Trusted"), (12, "Friend"), (1 << 30, "Ally")]


def reputation_standing(rep, lang="ru") -> str:
    """Текстовая ступень репутации игрока у фракции: число → ярлык.
    Ступени одинаковы для «хороших» и «злых» фракций — это мера доверия фракции,
    а не нравственная оценка. (Новый порог — верхняя граница ступени.)"""
    try:
        rep = int(rep or 0)
    except (TypeError, ValueError):
        rep = 0
    table = _STANDING_EN if lang and lang != "ru" else _STANDING_RU
    for max_rep, label in table:
        if rep <= max_rep:
            return label
    return table[-1][1]


def faction_rep_value(player: dict, faction_id: str) -> int:
    """Числовое значение репутации игрока у фракции (безопасно)."""
    try:
        return int((player.get("reputation") or {}).get(faction_id, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _apply_reputation_delta(player: dict, factions: dict | None, dimension: str, delta: int) -> list[str]:
    """«Разлив» изменения репутации по связям фракций.
    Первичную фракцию (`dimension`) прибавляет вызывающий; здесь только связи:
    союзники делят часть выгоды, враги — часть издержек (по «симпатии/антипатии»)."""
    rep = player.setdefault("reputation", {})
    fac = (factions or {}).get(dimension)
    rels = (fac or {}).get("relations") or {}
    if not rels or not int(delta):
        return []
    shared = max(1, abs(int(delta)) // 4)   # союзники делят долю выгоды/потерь
    msgs = []
    for other_id, stance in rels.items():
        stance = str(stance or "").strip().lower()
        other_id = str(other_id)
        if stance in ("союз", "союзник", "ally", "friend", "друг"):
            if int(delta) > 0:
                rep[other_id] = (rep.get(other_id, 0) or 0) + shared
                msgs.append(f"({other_id} +{shared})")
        elif stance in ("враг", "hostile", "enemy"):
            penal = -abs(int(delta))
            rep[other_id] = (rep.get(other_id, 0) or 0) + penal
            msgs.append(f"({other_id} {penal})")
    return msgs

def _bump(p: dict, key: str, delta: int = 1) -> None:
    """Приращивает счётчик статистики пути игрока (убийства/квесты/локации)."""
    prog = p.setdefault("progress", {})
    prog[key] = prog.get(key, 0) + max(0, delta)


def _norm_item(item: Any) -> dict:
    if isinstance(item, str):
        return {"name": item, "qty": 1}
    r = {"name": item.get("name", "Предмет"), "qty": int(item.get("qty", 1)),
         "desc": item.get("desc", "")}
    val = item.get("value")
    if val not in (None, "", 0):
        try:
            r["value"] = int(val)
        except (TypeError, ValueError):
            pass  # кривой value от LLM не должен ронять ход
    w = item.get("weight")
    if w not in (None, "", 0):
        try:
            r["weight"] = float(w)
        except (TypeError, ValueError):
            pass  # кривой вес от LLM не должен ронять ход
    return r


def _give_item(setting: dict, name: str, qty: int, desc: str = "",
               weight: float | None = None) -> None:
    """Добавить предмет в инвентарь игрока (склеивает по имени).
    weight (кг / условные единицы) — опциональный вес предмета для системы
    вместимости; если не задан, вес сохраняется как есть (0 = невесомый)."""
    inv = setting["player"].setdefault("inventory", [])
    found = next((x for x in inv if x.get("name") == name), None)
    if found:
        found["qty"] = found.get("qty", 1) + qty
        if desc and not found.get("desc"):
            found["desc"] = desc
        if weight is not None:
            found["weight"] = float(weight)
    else:
        entry = {"name": name, "qty": qty}
        if desc:
            entry["desc"] = desc
        if weight is not None:
            entry["weight"] = float(weight)
        inv.append(entry)


def inventory_weight(player: dict) -> float:
    """Суммарный вес инвентаря игрока (weight × qty по каждому предмету)."""
    tot = 0.0
    for i in (player.get("inventory") or []):
        if not isinstance(i, dict):
            continue
        tot += float(i.get("weight", 0) or 0) * max(1, int(i.get("qty", 1) or 1))
    return round(tot, 1)


def carry_capacity(player: dict) -> float:
    """Вместимость рюкзака игрока (базовая 20 + сила×2 + выносливость×2).
    Всегда вычисляется от статов — значит работает и для старых сохранений
    (у них просто нет предметов с весом, поэтому ничего не ломает)."""
    st = player.get("stats") or {}
    try:
        str_ = int(st.get("сила", 10) or 10)
        con = int(st.get("выносливость", 10) or 10)
    except (TypeError, ValueError):
        str_, con = 10, 10
    return round(20 + str_ * 2 + con * 2, 1)


def _overweight_left(setting: dict, extra_weight: float) -> float:
    """Сколько веса НЕ помещается при попытке добавить extra_weight (≥0, 0 = влезает).
    Всегда ≥0; >0 означает перегруз на эту величину."""
    extra_weight = float(extra_weight or 0)
    if extra_weight <= 0:
        return 0.0
    p = setting["player"]
    free = carry_capacity(p) - inventory_weight(p)
    return round(max(0.0, extra_weight - max(0.0, free)), 1)


def _carry_block(noun: str, setting: dict, extra_weight: float) -> str | None:
    """Если предмет не помещается — вернуть сообщение перегруза, иначе None."""
    left = _overweight_left(setting, extra_weight)
    if left <= 0:
        return None
    p = setting["player"]
    return (f"{noun} Перегруз: не хватает {left:.0f} кг в рюкзаке "
            f"({inventory_weight(p):.0f}/{carry_capacity(p):.0f}).")


def _stock_qty(shop: dict, item_name: str) -> int:
    st = next((x for x in (shop.get("items") or []) if x.get("name") == item_name), None)
    return int(st.get("qty", 0) or 0) if st else 0


def total_sell_value(player: dict) -> int:
    """Сумма, которую можно выручить, продав весь инвентарь (по `value` каждого).
    Универсальная справка для игрока/рассказчика — никакой логики, только отображение."""
    tot = 0
    for i in (player.get("inventory") or []):
        if not isinstance(i, dict):
            continue
        tot += int(i.get("value", 0) or 0) * max(1, int(i.get("qty", 1) or 1))
    return tot


def location_stations(setting: dict) -> list[str]:
    """Станции/мастерские текущей локации (для крафта). Задаются мастером через
    location_add/update: `stations: [\"кузница\", \"алхимический стол\"]`. Универсально
    для любого жанра (кузница/верстак/матрица/лаборатория/кухня…)."""
    loc = setting.get("locations", {}).get(setting.get("current_location", "start"), {})
    st = loc.get("stations") or []
    st = [s for s in st if str(s or "").strip()]
    # флаги вида station:{name}=true также считаются доступными
    flags = setting.get("flags") or {}
    for fname, fval in flags.items():
        fk = str(fname).lower()
        if fk.startswith("station:") and fval:
            st.append(fk[len("station:"):])
    seen = set()
    out = []
    for s in st:
        k = str(s).strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(str(s).strip())
    return out


def has_station(setting: dict, recipe_station: str) -> bool:
    """Есть ли у игрока в текущем месте нужная станция (по требованию рецепта).
    Сравнение без регистра и без лишних пробелов."""
    want = str(recipe_station or "").strip().lower()
    if not want:
        return True
    return any(want == str(s).strip().lower() for s in location_stations(setting))


def can_craft(setting: dict, recipe: dict) -> tuple[list[str], str]:
    """Проверка возможности создать по рецепту: (список причин-препятствий, статус).
    Пустой список = можно. Статус: 'io' (нечго нет), 'ingredients', 'station', 'profession'.
    Чисто информационно — применить или нет решает мастер."""
    if not recipe:
        return ["Рецепт не известен"], "node"
    inv = setting["player"].get("inventory") or []
    missing = []
    for it in (recipe.get("ingredients") or []):
        need = int(it.get("qty", 1) or 1)
        have = sum(i.get("qty", 0) for i in inv if i.get("name") == it.get("name"))
        if have < need:
            missing.append(f"{it.get('name')} ({have}/{need})")
    if missing:
        return missing, "ingredients"
    want_st = recipe.get("station") or []
    if not isinstance(want_st, list):
        want_st = [want_st]
    want_st = [str(x).strip() for x in want_st if str(x or "").strip()]
    if want_st and not any(has_station(setting, x) for x in want_st):
        return [f"нужна станция «{', '.join(want_st)}»"], "station"
    prof = (recipe.get("profession") or "").strip()
    cur_prof = str(setting["player"].get("profession") or "").strip().lower()
    if prof and cur_prof != prof.lower():
        return [f"требуется профессия «{prof}»"], "profession"
    return [], "ok"


def _shop_price(setting: dict, shop: dict, item_name: str, mode: str):
    """Цена покупки (buy) или продажи (sell) предмета в магазине с учётом репутации фракции.
    Репутация фракции магазина `player.reputation[faction]`:
    каждая единица ≈ 5% скидки/наценки (потолок ±50%)."""
    base = None
    if mode == "buy":
        it = next((x for x in (shop.get("items") or []) if x.get("name") == item_name), None)
        if it is not None:
            base = it.get("price")
    else:
        it = next((x for x in setting["player"].get("inventory", []) if x.get("name") == item_name), None)
        if it is not None:
            base = it.get("value")
    if base in (None, ""):
        return None
    base = int(base)
    faction = str(shop.get("faction") or "").strip()
    rep = 0
    if faction:
        try:
            rep = int((setting["player"].get("reputation") or {}).get(faction, 0) or 0)
        except (TypeError, ValueError):
            rep = 0
    factor = min(1.5, max(0.5, 1.0 - 0.05 * rep))
    if mode == "buy":
        return max(0, int(round(base * factor)))      # высок. репутация → дешевле купить
    return max(0, int(round(base * (2.0 - factor))))   # высок. репутация → дороже продать


def normalize_directives(d) -> dict:
    """Защита от структурного «мусора» в JSON директив LLM.

    Модели иногда возвращают строку вместо объекта: roll: "d20", enemy_apply: "волк",
    player: "hp -5" и т.п. Без нормализации такой ход падает с
    'str' object has no attribute 'get'. Здесь кривые значения выправляются,
    а безнадёжные — отбрасываются (игра продолжается, механика не применяется).

    Реализация вынесена в backend/directives.py (Pydantic-схемы директив):
    семантика идентична прежней ручной — tests/test_directives_schemas.py следит.
    """
    return _normalize_directives(d)


def tick_effects(setting: dict) -> list[str]:
    """Начало хода: применяет периодические эффекты (урон/лечение ×стаки), уменьшает
    длительность и убирает истёкшие. Возвращает системные сообщения."""
    msgs: list[str] = []
    p = setting.get("player", {})
    effects = p.get("effects") or {}
    if not effects:
        return msgs
    expired: list[str] = []
    for name, ef in list(effects.items()):
        if not isinstance(ef, dict):
            effects.pop(name, None)
            continue
        dmg = int(ef.get("damage", 0) or 0) * max(1, int(ef.get("stacks", 1) or 1))
        heal = int(ef.get("heal", 0) or 0) * max(1, int(ef.get("stacks", 1) or 1))
        if dmg:
            hp0 = max(0, p.get("hp", 0) - dmg)
            p["hp"] = hp0
            msgs.append(f"⏳ Эффект «{name}»: −{dmg} HP → {hp0}/{p.get('max_hp', 0)}")
        if heal:
            hp1 = min(p.get("max_hp", 100), p.get("hp", 0) + heal)
            p["hp"] = hp1
            msgs.append(f"⏳ Эффект «{name}»: +{heal} HP → {hp1}/{p.get('max_hp', 0)}")
        if "turns" in ef and ef.get("turns") not in (-1, None):
            ef["turns"] = max(0, int(ef["turns"]) - 1)
            if ef["turns"] <= 0:
                expired.append(name)
    for name in expired:
        effects.pop(name, None)
        msgs.append(f"⌛ Эффект «{name}» закончился.")
    return msgs


def tick_world_timers(setting: dict) -> list[str]:
    """Начало хода: тикают таймеры мира (дедлайны, осада, бомба…).
    Движок лишь уменьшает счётчик и сообщает «истёк» — ЧТО случится, решает мастер."""
    msgs: list[str] = []
    timers = setting.get("timers") or {}
    if not isinstance(timers, dict) or not timers:
        return msgs
    expired: list[str] = []
    for name, t in list(timers.items()):
        if not isinstance(t, dict):
            continue
        t_left = t.get("turns_left")
        if t_left is None or t_left in (-1, "∞", "inf", "permanent"):
            continue  # бессрочный таймер не тикает
        try:
            t_left = int(t_left)
        except (TypeError, ValueError):
            continue
        t_left = max(0, t_left - 1)
        t["turns_left"] = t_left
        if t_left <= 0:
            expired.append(name)
    for name in expired:
        d = timers.pop(name, None) or {}
        tail = f" — {d.get('desc')}" if isinstance(d, dict) and d.get("desc") else ""
        msgs.append(f"⏰ Таймер «{name}» истёк.{tail}")
    return msgs


def _need_specs() -> dict:
    return dict(NEEDS_SPECS)


def _mental_specs() -> dict:
    return dict(MENTAL_SPECS)


def _ensure_axis(p: dict, kind: str) -> dict:
    """Достраивает шкалу потребностей (kind='needs') или рассудка (kind='mental')."""
    ensure_player_schema(p)
    d = p.setdefault(kind, {})
    specs = NEEDS_SPECS if kind == "needs" else MENTAL_SPECS
    for k, (val, mx, decay) in specs.items():
        cur = d.get(k)
        if not isinstance(cur, dict):
            cur = {}
        cur.setdefault("value", float(val))
        cur.setdefault("max", float(mx))
        cur.setdefault("decay", float(decay))
        d[k] = cur
    return d


def tick_needs_mental(setting: dict) -> list[str]:
    """Начало хода: потребности убывают/растут по decay (физика), рассудок/стресс/мораль —
    то же. Движок только тикает и предупреждает — последствия (эффекты, штрафы, сюжет)
    ведёт мастер директивами (закон 3). Возвращает предупреждения при критике."""
    msgs: list[str] = []
    p = setting.get("player", {})
    if not isinstance(p, dict):
        return msgs
    needs = _ensure_axis(p, "needs")
    mental = _ensure_axis(p, "mental")
    for name, spec in list(needs.items()):
        if not isinstance(spec, dict):
            continue
        mx = float(spec.get("max", 100) or 100)
        decay = float(spec.get("decay", 0) or 0)
        if decay == 0:
            continue
        cur = float(spec.get("value", mx) or mx)
        spec["value"] = max(0.0, min(mx, cur - decay))
        if spec["value"] <= NEED_CRITICAL:
            msgs.append(f"⚠️ {name} на критическом уровне ({spec['value']:.0f}/{mx:.0f}) — игрок нуждается в еде/питье/отдыхе.")
    for name, spec in list(mental.items()):
        if not isinstance(spec, dict):
            continue
        mx = float(spec.get("max", 100) or 100)
        decay = float(spec.get("decay", 0) or 0)
        if decay == 0:
            continue
        cur = float(spec.get("value", mx) or mx)
        # стресс растёт (decay>0 — прибавляем)
        if name == "стресс":
            spec["value"] = max(0.0, min(mx, cur + decay))
            if spec["value"] >= 100 - MENTAL_CRITICAL:
                msgs.append(f"⚠️ Стресс на критическом уровне ({spec['value']:.0f}/{mx:.0f}) — нужен отдых/разрядка.")
        else:
            spec["value"] = max(0.0, min(mx, cur - decay))
            if spec["value"] <= MENTAL_CRITICAL:
                msgs.append(f"⚠️ {name} на критическом уровне ({spec['value']:.0f}/{mx:.0f}) — игрок на пределе.")
    return msgs


def equipped_bonuses(p: dict) -> dict:
    """Суммарные бонусы от экипировки {стат: +N} (моды, базу не меняют)."""
    out: dict = {}
    for slot, item_name in (p.get("equipped") or {}).items():
        if not item_name:
            continue
        it = next((x for x in (p.get("inventory") or []) if x.get("name") == item_name), None)
        if not isinstance(it, dict):
            continue
        b = it.get("bonus")
        if isinstance(b, dict):
            for k, v in b.items():
                out[k] = out.get(k, 0) + int(v)
    return out


def _slot_of(item: dict) -> str:
    s = str(item.get("slot") or "").strip().lower()
    if s in ("аксессуары", "аксессуар"):
        return "аксессуар"
    return s if s else "универсальный"


def equip_item(setting: dict, item_name: str) -> tuple[bool, str]:
    """Надеть предмет: есть в инвентаре, слот свободен (или снять прежний). Возвращает (ok, msg)."""
    p = setting["player"]
    ensure_player_schema(p)
    name = str(item_name or "").strip()
    if not name:
        return False, "Не указан предмет."
    it = next((x for x in p.get("inventory", []) if x.get("name") == name), None)
    if not isinstance(it, dict):
        return False, f"Нет предмета «{name}» в инвентаре."
    slot = _slot_of(it)
    equipped = p.setdefault("equipped", {})
    if slot == "универсальный":
        return False, f"«{name}» не имеет слота (нельзя надеть)."
    old = equipped.get(slot)
    if old == name:
        return True, f"«{name}» уже надето (слот: {slot})."
    if old:
        equipped[slot] = name
        return True, f"🛡 {old} → {name} (слот: {slot})."
    equipped[slot] = name
    return True, f"🛡 Надето: {name} (слот: {slot})."


def unequip_item(setting: dict, item_name: str) -> tuple[bool, str]:
    """Снять предмет (по имени или слоту). Возвращает (ok, msg)."""
    p = setting["player"]
    ensure_player_schema(p)
    name = str(item_name or "").strip()
    equipped = p.setdefault("equipped", {})
    if not name:
        return False, "Не указан предмет."
    slot = None
    if name in equipped.values():
        slot = next((s for s, v in equipped.items() if v == name), None)
    elif name.lower() in (_slot_of(x) for x in p.get("inventory", [])):
        # по имени слота (например «шлем»)
        for s, v in equipped.items():
            if s == name.lower():
                slot = s
                name = v
                break
    if slot is None:
        return False, f"«{name}» не надето."
    equipped.pop(slot, None)
    return True, f"🛡 Снято: {name} (слот: {slot})."


def board_add(setting: dict, title: str, text: str) -> str:
    """Добавить объявление на доску мира (таверна/форум/рация). Возвращает системное сообщение."""
    board = setting.setdefault("board", [])
    if not isinstance(board, list):
        board = []
        setting["board"] = board
    board.append({"title": str(title or "").strip()[:80], "text": str(text or "").strip()[:500]})
    if len(board) > 30:
        board.pop(0)
    return f"📜 Объявление: {title or '(без названия)'}"


def board_text(setting: dict, limit: int = 12) -> str:
    """Человекочитаемый текст доски объявлений (для состояния/UI)."""
    board = setting.get("board") or []
    if not isinstance(board, list):
        return ""
    if not board:
        return ""
    out = []
    for b in board[-limit:]:
        t = b.get("title") if isinstance(b, dict) else b
        tx = b.get("text") if isinstance(b, dict) else ""
        out.append(f"• {t}" + (f": {tx[:150]}" if tx else ""))
    return "\n".join(out)


def location_effects_for(setting: dict, loc_id: str) -> list[dict]:
    """Эффекты локации-зоны (радиация/туман/проклятие). Возвращает список dict-эффектов."""
    loc = setting.get("locations", {}).get(loc_id or "", {})
    fx = loc.get("effects") if isinstance(loc, dict) else None
    return list(fx) if isinstance(fx, list) else []


def apply_location_effects(setting: dict, loc_id: str, apply: bool = True) -> list[str]:
    """Наложить/снять эффекты текущей локации-зоны (универсально: радиация, ядовитый
    туман, проклятый лес, зона невесомости). Применение через эффекты — «физика»,
    защита/последствия — рассказчик (закон 3)."""
    msgs: list[str] = []
    p = setting.get("player", {})
    if not isinstance(p, dict):
        return msgs
    ensure_player_schema(p)
    effects = location_effects_for(setting, loc_id)
    if not effects:
        return msgs
    for fx in effects:
        if not isinstance(fx, dict) or not fx.get("name"):
            continue
        name = str(fx["name"])[:80]
        if apply:
            new_ef = {
                "turns": -1,  # постоянно, пока игрок в зоне
                "damage": int(fx.get("damage", 0) or 0),
                "heal": int(fx.get("heal", 0) or 0),
                "kind": str(fx.get("kind", "зона"))[:30],
                "stacks": max(1, int(fx.get("stacks", 1) or 1)),
                "desc": str(fx.get("desc", ""))[:200],
                "tag": "zone",
            }
            mods = fx.get("mods")
            if isinstance(mods, dict) and mods:
                new_ef["mods"] = mods
            cur = p["effects"].get(name)
            if not cur:
                p["effects"][name] = new_ef
                msgs.append(f"🌫 Влияние места: «{name}».")
        else:
            if p["effects"].pop(name, None) is not None:
                msgs.append(f"🌫 Влияние места «{name}» спало.")
    return msgs



def _race_spec_from(name_or_dict) -> Optional[dict]:
    """Нормализует расу из директивы: строка (имя из справочника) или dict {name, bonus, passive}."""
    if isinstance(name_or_dict, dict):
        spec = dict(name_or_dict)
        if spec.get("name"):
            built = next((r for r in RACES if r["name"].lower() == str(spec["name"]).strip().lower()), None)
            if built:
                spec.setdefault("bonus", built["bonus"])
                spec.setdefault("passive", built["passive"])
                spec.setdefault("desc", built["desc"])
        return spec if spec.get("name") else None
    name = str(name_or_dict or "").strip()
    built = next((r for r in RACES if r["name"].lower() == name.lower()), None)
    return dict(built) if built else ({"name": name} if name else None)


def _prof_spec_from(name_or_dict) -> Optional[dict]:
    if isinstance(name_or_dict, dict):
        spec = dict(name_or_dict)
        if spec.get("name"):
            built = next((pf for pf in PROFESSIONS if pf["name"].lower() == str(spec["name"]).strip().lower()), None)
            if built:
                spec.setdefault("buff", built["buff"])
                spec.setdefault("desc", built["desc"])
        return spec if spec.get("name") else None
    name = str(name_or_dict or "").strip()
    built = next((pf for pf in PROFESSIONS if pf["name"].lower() == name.lower()), None)
    return dict(built) if built else ({"name": name} if name else None)


def _apply_race_change(p: dict, spec: dict) -> str | None:
    """Смена расы: откат старого бонуса/пассива, применение новых. Для творческих рас рассказчик передаёт bonus/passive."""
    ensure_player_schema(p)
    new_name = str(spec.get("name", "")).strip()[:60]
    if not new_name:
        return None
    old = (p.get("race") or "").strip()
    # снять старый расовый бонус (если есть)
    old_bonus = p.pop("race_bonus", None) or {}
    for k, v in old_bonus.items():
        if k in p["stats"]:
            p["stats"][k] = max(1, p["stats"].get(k, 10) - int(v))
    for name in list(p["effects"].keys()):
        if isinstance(p["effects"][name], dict) and p["effects"][name].get("tag") == "race":
            p["effects"].pop(name)
    # применить новую расу
    bonus = spec.get("bonus") or {}
    if isinstance(bonus, dict):
        for k, v in bonus.items():
            if k in p["stats"]:
                p["stats"][k] = max(1, p["stats"].get(k, 10) + int(v))
        p["race_bonus"] = {k: int(v) for k, v in bonus.items() if k in p["stats"]}
    passive = spec.get("passive")
    if isinstance(passive, dict) and passive.get("name"):
        p["effects"][str(passive["name"])[:80]] = {
            "turns": -1, "kind": "особый", "tag": "race",
            "desc": str(passive.get("desc", ""))[:200]}
    p["race"] = new_name
    recalc_derived(p)
    tail = ""
    if bonus:
        tail += " (бонусы: " + ", ".join(f"{k}{v:+}" for k, v in bonus.items()) + ")"
    return f"🧝 Раса: {old or '—'} → {new_name}{tail}"


def _apply_profession_change(p: dict, spec: dict) -> str | None:
    """Смена профессии: снимает бафф старой, применяет постоянный бафф новой (стат)."""
    ensure_player_schema(p)
    new_name = str(spec.get("name", "")).strip()[:60]
    if not new_name:
        return None
    old = (p.get("profession") or "").strip()
    # снять бафф старой профессии
    old_buff = p.pop("profession_buff", None) or {}
    for k, v in old_buff.items():
        if k in p["stats"]:
            p["stats"][k] = max(1, p["stats"].get(k, 10) - int(v))
    for name in list(p["effects"].keys()):
        if isinstance(p["effects"][name], dict) and p["effects"][name].get("tag") == "profession":
            p["effects"].pop(name)
    # применить новую
    buff = spec.get("buff") or {}
    if isinstance(buff, dict):
        for k, v in buff.items():
            if k in p["stats"]:
                p["stats"][k] = max(1, p["stats"].get(k, 10) + int(v))
        p["profession_buff"] = {k: int(v) for k, v in buff.items() if k in p["stats"]}
    p["effects"][f"Ремесло: {new_name}"] = {
        "turns": -1, "kind": "особый", "tag": "profession",
        "desc": str(spec.get("desc", ""))[:200]}
    p["profession"] = new_name
    recalc_derived(p)
    tail = ""
    if buff:
        tail += " (бафф: " + ", ".join(f"{k}{v:+}" for k, v in buff.items()) + ")"
    return f"⚒ Профессия: {old or '—'} → {new_name}{tail}"


def check_profession_advance(setting: dict) -> list[str]:
    """Авто-смена профессии от накопленных действий. Вызывается после apply_directives.
    Выбирает ветку действия с максимальным перевыполнением порога (PROF_ACTION_MAP),
    применяет профессию и возвращает системные сообщения."""
    p = setting.get("player") or {}
    acts = p.get("actions") or {}
    if not acts:
        return []
    cur_prof = (p.get("profession") or "").strip()
    best: tuple = None  # (перевыполнение, действие, счётчик, порог, имя_профессии)
    for act, count in acts.items():
        prof = PROF_ACTION_MAP.get(str(act).strip().lower())
        if not prof:
            continue
        pname, thr = prof
        if pname == cur_prof and cur_prof:
            continue  # профессия уже эта — не дёргаемся
        c = int(count or 0)
        if c >= thr and (best is None or (c - thr) > best[0]):
            best = (c - thr, act, c, thr, pname)
    if not best:
        return []
    _over, act, c, thr, pname = best
    spec = _prof_spec_from(pname) or {"name": pname}
    m = _apply_profession_change(p, spec)
    msgs = [f"📈 Накопленные действия («{act}» {c}/{thr}) → смена профессии."]
    if m:
        msgs.append(m)
    return msgs


def _apply_skill(p: dict, sk: dict, source: str = "skill_add") -> str | None:
    """Изучение/улучшение навыка с рангами. sk: {name, rank?, kind?, desc?, mp_cost?}."""
    ensure_player_schema(p)
    name = str((sk.get("name") if isinstance(sk, dict) else sk) or "").strip()[:60]
    if not name:
        return None
    r = norm_rank(sk.get("rank") if isinstance(sk, dict) else "F")
    kind = (sk.get("kind") if isinstance(sk, dict) else "") or "универсальное"
    desc = (sk.get("desc") if isinstance(sk, dict) else "") or ""
    mp = int(sk.get("mp_cost", 0)) if isinstance(sk, dict) else 0
    cur = p["skills"].get(name)
    if cur and isinstance(cur, dict):
        if rank_index(r) > rank_index(cur.get("rank", "F")):
            cur["rank"] = r
            cur.update({"kind": kind, "desc": desc or cur.get("desc", ""), "mp_cost": mp})
            return f"🎖 Навык «{name}» улучшен до ранга {r}."
        return None
    p["skills"][name] = {"rank": r, "kind": kind, "desc": desc, "mp_cost": mp}
    return f"🎖 Изучен навык «{name}» (ранг {r}, {kind})."


# ══════════════════════════════════════════════════════════════
# Движок директив — Цепочка обязанностей (Chain of Responsibility)
#
# Раньше apply_directives() была гигантской функцией-«монада», где вся логика
# по всем директивам сидела в одном теле. Теперь каждая группа директив —
# это отдельный обработчик (DirectiveHandler), владеющий НЕПЕРЕСЕКАЮЩИМСЯ
# набором ключей (self.keys). Запрос (dict директив) проходит по цепочке
# DIRECTIVE_CHAIN, и каждое «звено» обрабатывает только свою группу, не трогая
# чужие ключи — это классическая «цепочка обязанностей»: ответственный
# обработчик применяет директиву, остальные её не трогают.
# Преимущества: новая директива = новый обработчик (без правки монолита),
# каждая группа тестируется изолированно, код читается по категориям.
# ══════════════════════════════════════════════════════════════
class DirectiveHandler:
    """Базовый обработчик директив. keys — ключи директив, за которые он отвечает."""

    keys: frozenset[str] = frozenset()

    def handles(self, d: dict) -> bool:
        """Есть ли в запросе директивы, за которые отвечает обработчик."""
        return bool(self.keys & d.keys())

    def apply(self, setting: dict, d: dict) -> list[str]:
        """Применяет свои директивы к setting. Возвращает список системных сообщений."""
        return []


class PlayerHandler(DirectiveHandler):
    """player: hp / mp / gold / xp / stats / actions.
    Уровень растёт ТОЛЬКО явной директивой `player.level` (творческий рост на
    рассказчике); XP копится как информация, но авто-прокачки по порогу НЕТ —
    код не решает уровень за мастера («код — судья, рассказчик — мастер»)."""
    keys = frozenset({"player"})

    def apply(self, setting: dict, d: dict) -> list[str]:
        msgs: list[str] = []
        p = setting["player"]
        difficulty = setting.get("_difficulty", "normal")
        pl = d.get("player") or {}
        if isinstance(pl, dict):
            for k in ("hp", "mp", "gold", "xp", "level"):
                if k in pl:
                    v = int(pl[k])
                    if k == "hp":
                        p["hp"] = max(0, min(p.get("max_hp", 100), p.get("hp", 0) + v))
                        msgs.append(f"Здоровье: {p['hp']}/{p['max_hp']}" + (f" (−{abs(v)})" if v < 0 else f" (+{v})"))
                    elif k == "mp":
                        p["mp"] = max(0, min(p.get("max_mp", 50), p.get("mp", 0) + v))
                    elif k == "gold":
                        p["gold"] = max(0, p.get("gold", 0) + v)
                        msgs.append(f"Золото: {p['gold']}" + (f" (+{v})" if v >= 0 else f" (−{abs(v)})"))
                    elif k == "xp":
                        p["xp"] = p.get("xp", 0) + v
                        msgs.append(f"Опыт +{v}")
                    elif k == "level":
                        # Явное задание уровня — творческий рост по сюжету (ритуал, дар, испытание),
                        # а не только формулой XP (вариант А: цифры — внутренняя форма).
                        if 1 <= v <= 99 and v != (p.get("level") or 1):
                            p["level"] = v
                            recalc_derived(p, difficulty)
                            p["hp"] = p["max_hp"]
                            p["mp"] = p["max_mp"]
                            msgs.append(f"🎉 Уровень изменён до {v}! HP/MP восстановлены.")
            # Уровень повышается ТОЛЬКО директивой `player.level` (сюжетный рост,
            # на усмотрение рассказчика). XP копится как информация, но авто-прокачки
            # 100 XP → уровень НЕТ — уровень решает мастер директивами, а не формула.
            # статы (аддитивно): {"player": {"stats": {"сила": 2}}}
            st = pl.get("stats")
            if isinstance(st, dict) and st:
                st_msgs = []
                for k, v in st.items():
                    key = str(k).strip().lower()
                    if key in p["stats"]:
                        p["stats"][key] = max(1, p["stats"].get(key, 10) + int(v))
                        st_msgs.append(f"{key} {p['stats'][key]}")
                if st_msgs:
                    msgs.append("Статы: " + ", ".join(st_msgs))
                recalc_derived(p, difficulty)
            # действия (накопление профессий): {"player": {"actions": {"кузнечное дело": 1}}}
            acts = pl.get("actions")
            if isinstance(acts, dict) and acts:
                p.setdefault("actions", {})
                for k, v in acts.items():
                    key = str(k).strip()[:40]
                    if key:
                        p["actions"][key] = p["actions"].get(key, 0) + int(v)
        return msgs


class IdentityHandler(DirectiveHandler):
    """Раса / класс / ранг / эволюция / мультикласс / профессия."""
    keys = frozenset({"race_change", "class", "class_rank", "class_evolve",
                      "secondary_class", "secondary_rank", "profession"})

    def apply(self, setting: dict, d: dict) -> list[str]:
        msgs: list[str] = []
        p = setting["player"]
        if "race_change" in d:
            spec = _race_spec_from(d["race_change"])
            if spec:
                m = _apply_race_change(p, spec)
                if m:
                    msgs.append(m)
        if "class" in d:
            cls_in = d["class"]
            cl_skill = None
            if isinstance(cls_in, dict):
                cls = str(cls_in.get("name", "")).strip()[:60]
                cl_skill = cls_in.get("skill")
            else:
                cls = str(cls_in).strip()[:60]
            if cls:
                old = p.get("class", "") or "—"
                p["class"] = cls
                built = next((c for c in CLASSES if c["name"].lower() == cls.lower()), None)
                note = ""
                if built and not any(k.lower() == built["starter_skill"]["name"].lower() for k in p.get("skills", {})):
                    m = _apply_skill(p, built["starter_skill"])
                    if m:
                        note = " " + m
                elif cl_skill and isinstance(cl_skill, dict):
                    m = _apply_skill(p, cl_skill)
                    if m:
                        note = " " + m
                msgs.append(f"🎭 Класс: {old} → {cls} (ранг {p.get('class_rank','F')}).{note}")
        if "class_rank" in d:
            cr = d["class_rank"]
            rank = norm_rank((cr.get("rank") if isinstance(cr, dict) else cr) or "F")
            cur_r = p.get("class_rank", "F")
            if rank_index(rank) > rank_index(cur_r):
                p["class_rank"] = rank
                msgs.append(f"⬆ Ранг класса: {cur_r} → {rank}")
        if "class_evolve" in d:
            ev = str(d["class_evolve"]).strip()[:60]
            if ev:
                old = p.get("class", "") or "—"
                p["class"] = ev
                msgs.append(f"🔮 Эволюция класса: {old} → {ev}")
        if "secondary_class" in d:
            sc = str(d["secondary_class"]).strip()[:60]
            if sc:
                p["secondary_class"] = sc
                msgs.append(f"🔄 Мультикласс: {sc} (ранг {p.get('secondary_rank','F')})")
        if "secondary_rank" in d:
            sr = d["secondary_rank"]
            rank = norm_rank((sr.get("rank") if isinstance(sr, dict) else sr) or "F")
            if rank_index(rank) > rank_index(p.get("secondary_rank", "F")):
                p["secondary_rank"] = rank
                msgs.append(f"⬆ Ранг мультикласса: {rank}")
        if "profession" in d:
            prof = _prof_spec_from(d["profession"])
            if prof:
                m = _apply_profession_change(p, prof)
                if m:
                    msgs.append(m)
        return msgs


class SkillHandler(DirectiveHandler):
    """Навыки (изучение/ранг/забывание), титулы, репутация."""
    keys = frozenset({"skill_add", "skill", "skill_rank", "skill_remove", "title", "reputation"})

    def apply(self, setting: dict, d: dict) -> list[str]:
        msgs: list[str] = []
        p = setting["player"]
        if "skill_add" in d:
            m = _apply_skill(p, d["skill_add"])
            if m:
                msgs.append(m)
        if "skill" in d:  # старый формат {"skill": {"name":..., "value": N}}
            sk = d["skill"]
            if isinstance(sk, dict) and "value" in sk and "rank" not in sk:
                sk = {**sk, "rank": "F"}
            m = _apply_skill(p, sk, source="skill")
            if m:
                msgs.append(m)
        if "skill_rank" in d:
            sr2 = d["skill_rank"]
            sname = str((sr2.get("name") if isinstance(sr2, dict) else sr2) or "").strip()[:60]
            srank = norm_rank((sr2.get("rank") if isinstance(sr2, dict) else "F") or "F")
            cur = p["skills"].get(sname)
            if sname and isinstance(cur, dict) and rank_index(srank) > rank_index(cur.get("rank", "F")):
                cur["rank"] = srank
                msgs.append(f"🎖 Навык «{sname}»: ранг {srank}")
        if "skill_remove" in d:
            skr = d["skill_remove"]
            skr = skr.get("name") if isinstance(skr, dict) else skr
            if str(skr) in p.get("skills", {}):
                p["skills"].pop(str(skr))
                msgs.append(f"Навык «{skr}» забыт.")
        if "title" in d:
            t = str(d["title"]).strip()[:60]
            if t:
                titles = p.setdefault("titles", [])
                if t not in titles:
                    titles.append(t)
                    msgs.append(f"🏅 Титул: «{t}»")
        if "reputation" in d:
            rp = d["reputation"]
            if isinstance(rp, dict) and rp:
                rep = p.setdefault("reputation", {})
                out = []
                spill: list[str] = []
                for k, v in rp.items():
                    val = int(v)
                    name = str(k)
                    rep[name] = rep.get(name, 0) + val
                    out.append(f"{name}: {rep[name]}")
                    # «В разлив» по связям фракций: союзники/враги реагируют на изменения
                    spill += _apply_reputation_delta(p, setting.get("factions"), name, val)
                msgs.append("Репутация: " + ", ".join(out) + (" (сдвиг по фракциям: " + " ".join(spill) + ")" if spill else ""))
        return msgs


class AbilityHandler(DirectiveHandler):
    """Универсальные сверхспособности/умения (заклинания, техно-устройства, психо-импульсы)
    — одна абстракция под все жанры. Применением энергии (MP) управляет рассказчик."""
    keys = frozenset({"ability_add", "ability_remove", "ability_update", "ability_use"})

    def apply(self, setting: dict, d: dict) -> list[str]:
        msgs: list[str] = []
        p = setting["player"]
        ab = p.setdefault("abilities", {})
        if "ability_add" in d:
            aa = d["ability_add"]
            if isinstance(aa, dict) and aa.get("name"):
                nm = str(aa["name"]).strip()[:80]
                ab[nm] = {
                    "school": str(aa.get("school") or "").strip()[:40],
                    "source": str(aa.get("source") or "").strip()[:40],
                    "cost": _safe_int(aa.get("cost", aa.get("mp_cost", 0)), 0),
                    "cooldown": _safe_int(aa.get("cooldown"), 0),
                    "desc": str(aa.get("desc") or "")[:200],
                }
                tag = f" [{ab[nm]['school']}]" if ab[nm]["school"] else ""
                msgs.append(f"⚡ Освоена способность: «{nm}»{tag}")
        if "ability_update" in d:
            au = d["ability_update"]
            if isinstance(au, dict) and au.get("name"):
                nm = str(au["name"]).strip()[:80]
                if nm in ab:
                    for k in ("school", "source", "cost", "cooldown", "desc"):
                        if k in au:
                            ab[nm][k] = _safe_int(au[k], 0) if k in ("cost", "cooldown") else str(au[k])[:200]
                    msgs.append(f"⚡ Способность «{nm}» обновлена")
        if "ability_remove" in d:
            ar = d["ability_remove"]
            nm = str((ar.get("name") if isinstance(ar, dict) else ar) or "").strip()[:80]
            if nm in ab:
                ab.pop(nm)
                msgs.append(f"Способность «{nm}» утрачена")
        if "ability_use" in d:
            au = d["ability_use"]
            if isinstance(au, dict) and au.get("name"):
                nm = str(au["name"]).strip()[:80]
                if nm in ab:
                    cost = _safe_int(au.get("cost", ab[nm].get("cost", 0)), 0)
                    if cost:
                        m0 = p.get("mp", 0)
                        p["mp"] = max(0, m0 - cost)
                        msgs.append(f"⚡ Использована «{nm}» (энергия −{cost})")
                    else:
                        msgs.append(f"⚡ Использована «{nm}»")
                else:
                    msgs.append(f"⚠ Нет способности «{nm}» — сначала ability_add")
        return msgs


class EffectHandler(DirectiveHandler):
    """Эффекты: наложение / снятие."""
    keys = frozenset({"effect_add", "effect_remove"})

    def apply(self, setting: dict, d: dict) -> list[str]:
        msgs: list[str] = []
        p = setting["player"]
        if "effect_add" in d:
            ea = d["effect_add"]
            if isinstance(ea, dict) and ea.get("name"):
                name = str(ea["name"])[:80]
                # turns может прийти строкой ("permanent", "∞", "forever") — держимся
                _turns_raw = ea.get("turns", ea.get("duration", -1))
                try:
                    turns = int(_turns_raw)
                except (TypeError, ValueError):
                    turns = -1 if isinstance(_turns_raw, str) and _turns_raw.strip().lower() in ("permanent", "forever", "∞", "бессрочно", "постоянно", "-1") else -1
                new_ef = {
                    "turns": turns if turns != -1 else -1,
                    "damage": int(ea.get("damage", 0) or 0),
                    "heal": int(ea.get("heal", 0) or 0),
                    "kind": str(ea.get("kind", "особый"))[:30],
                    "stacks": max(1, int(ea.get("stacks", 1) or 1)),
                    "desc": str(ea.get("desc", ""))[:200],
                }
                if ea.get("tag"):
                    new_ef["tag"] = str(ea["tag"])[:30]
                mods = ea.get("mods")
                if isinstance(mods, dict) and mods:
                    new_ef["mods"] = mods
                cur = p["effects"].get(name)
                if cur:
                    if turns != -1 and cur.get("turns", -1) != -1:
                        cur["turns"] = max(cur["turns"], turns)
                    for k, v in new_ef.items():
                        if k == "turns" and (cur.get("turns", -1) == -1 or turns == -1):
                            cur[k] = v
                        elif k != "turns":
                            cur[k] = v
                else:
                    p["effects"][name] = new_ef
                ef_final = p["effects"][name]
                dur = "постоянно" if ef_final.get("turns", -1) in (-1, None) else f"{ef_final['turns']} ход."
                label = str(name).strip()
                if re.fullmatch(r"[A-Za-z0-9_\-]+", label):
                    # id-подобное имя (chill_resonance) — показываем читабельно
                    label = (label.replace("_", " ").strip().title() or label)[:60]
                desc = str(ef_final.get("desc") or "").strip()
                extra = f" — {desc[:150]}" if desc else ""
                msgs.append(f"✨ Эффект «{label}» ({ef_final.get('kind','особый')}, {dur}){extra}{' обновлён' if cur else ' наложен'}.")
        if "effect_remove" in d:
            er = d["effect_remove"]
            er_name = er.get("name") if isinstance(er, dict) else er
            if er_name:
                rm = p["effects"].pop(str(er_name), None)
                if rm is not None:
                    _d = str(rm.get("desc") or "").strip() if isinstance(rm, dict) else ""
                    msgs.append(f"Эффект «{er_name}» снят." + (f" — {_d[:120]}" if _d else ""))
        return msgs


class ItemHandler(DirectiveHandler):
    """Инвентарь: добавление / удаление предметов."""
    keys = frozenset({"add_item", "remove_item"})

    def apply(self, setting: dict, d: dict) -> list[str]:
        msgs: list[str] = []
        p = setting["player"]
        if "add_item" in d:
            for it in d["add_item"]:
                ni = _norm_item(it)
                _give_item(setting, ni["name"], ni["qty"], ni.get("desc", ""), float(ni.get("weight", 0) or 0))
                if ni.get("value") is not None:
                    found = next((x for x in setting["player"]["inventory"] if x["name"] == ni["name"]), None)
                    if found:
                        found["value"] = ni["value"]
                msgs.append(f"Получено: {ni['name']} ×{ni['qty']}")
        if "remove_item" in d:
            for it in d["remove_item"]:
                ri = _norm_item(it)
                found = next((x for x in p["inventory"] if x["name"] == ri["name"]), None)
                if found:
                    found["qty"] = max(0, found.get("qty", 1) - ri["qty"])
                    if found["qty"] == 0:
                        p["inventory"].remove(found)
        return msgs


class EconomyHandler(DirectiveHandler):
    """Экономика: магазины (add/remove/update) и торговля (buy/sell)."""
    keys = frozenset({"shop_add", "shop_remove", "shop_update", "trade_buy", "trade_sell"})

    def apply(self, setting: dict, d: dict) -> list[str]:
        msgs: list[str] = []
        shops = setting.setdefault("shops", {})
        if "shop_add" in d and isinstance(d["shop_add"], dict) and (d["shop_add"].get("id") or "").strip():
            s = d["shop_add"]
            sid = str(s["id"])
            items = []
            for it in (s.get("items") or []):
                if isinstance(it, dict) and (it.get("name") or "").strip():
                    items.append({"name": str(it["name"]),
                                  "price": _safe_int(it.get("price", 0)),
                                  "qty": _safe_int(it.get("qty", 1), 1) or 1,
                                  "value": _safe_int(it.get("value"), 0),
                                  "weight": float(it.get("weight", 0) or 0),
                                  "desc": it.get("desc", "")})
            shops[sid] = {"id": sid, "name": s.get("name", sid), "owner": s.get("owner", ""),
                          "faction": s.get("faction", ""), "location": s.get("location", ""), "items": items}
            msgs.append(f"🏪 Магазин «{s.get('name', sid)}» открыт лавкой {s.get('owner', '') or 'хозяином'}.")
        if "shop_remove" in d:
            sr = d["shop_remove"]
            sid = sr.get("id") if isinstance(sr, dict) else sr
            if sid and str(sid) in shops:
                msgs.append(f"🏪 Магазин «{shops.pop(str(sid)).get('name', sid)}» закрыл двери.")
        if "shop_update" in d and isinstance(d["shop_update"], dict) and (d["shop_update"].get("id") or "").strip():
            su = d["shop_update"]
            sh = shops.get(str(su["id"]))
            if sh:
                for k in ("name", "owner", "faction", "location"):
                    if su.get(k):
                        sh[k] = su[k]
                if su.get("items") is not None:
                    nitems = []
                    for it in su["items"]:
                        if isinstance(it, dict) and (it.get("name") or "").strip():
                            nitems.append({"name": str(it["name"]),
                                           "price": _safe_int(it.get("price", 0)),
                                           "qty": _safe_int(it.get("qty", 1), 1) or 1,
                                           "value": _safe_int(it.get("value"), 0),
                                           "weight": float(it.get("weight", 0) or 0),
                                           "desc": it.get("desc", "")})
                    sh["items"] = nitems
                msgs.append(f"🏪 Магазин «{sh.get('name', su['id'])}» обновлён.")
        if "trade_buy" in d and isinstance(d["trade_buy"], dict):
            tb = d["trade_buy"]
            item = str(tb.get("item", "")).strip()
            qty = max(1, _safe_int(tb.get("qty", 1), 1))
            sh = shops.get(str(tb.get("shop", "")))
            if item and qty and sh:
                stock = next((x for x in sh["items"] if x.get("name") == item), None)
                if stock is None:
                    msgs.append(f"🏪 В «{sh.get('name', tb.get('shop'))}» нет товара «{item}».")
                elif _stock_qty(sh, item) < qty:
                    msgs.append(f"🏪 Недостаточно товара «{item}» (есть {_stock_qty(sh, item)}).")
                else:
                    price = _shop_price(setting, sh, item, "buy")
                    total = price * qty
                    gold = setting["player"].get("gold", 0)
                    # вес покупаемого товара: не помещается в рюкзак → отказ
                    w = float(stock.get("weight", 0) or 0) * qty
                    if gold < total:
                        msgs.append(f"🏪 Не хватает золота: нужно {total} 🪙, у тебя {gold}.")
                    elif blk := _carry_block("🎒", setting, w):
                        msgs.append(f"🏪 {blk}")
                    else:
                        setting["player"]["gold"] = gold - total
                        stock["qty"] -= qty
                        if stock["qty"] <= 0:
                            sh["items"].remove(stock)
                        _give_item(setting, item, qty, stock.get("desc", ""), float(stock.get("weight", 0) or 0))
                        # пересh: предмет в руках наследует ценность покупки (для продажи)
                        _bought = next((x for x in setting["player"]["inventory"] if x.get("name") == item), None)
                        if _bought is not None and stock.get("value"):
                            _bought["value"] = _safe_int(stock.get("value"), 0)
                        msgs.append(f"🛒 Куплено: {item} ×{qty} за {total} 🪙.")
        if "trade_sell" in d and isinstance(d["trade_sell"], dict):
            ts = d["trade_sell"]
            item = str(ts.get("item", "")).strip()
            qty = max(1, _safe_int(ts.get("qty", 1), 1))
            if item and qty:
                have = next((x for x in setting["player"]["inventory"] if x.get("name") == item), None)
                if have is None or have.get("qty", 0) < qty:
                    msgs.append(f"🏪 Нет предмета «{item}» для продажи.")
                else:
                    sh = shops.get(str(ts.get("shop", ""))) or {"name": ts.get("shop", "торговец"), "faction": "", "location": ""}
                    price = _shop_price(setting, sh, item, "sell")
                    if price is None:
                        price = 1
                    total = price * qty
                    have["qty"] -= qty
                    if have["qty"] <= 0:
                        setting["player"]["inventory"].remove(have)
                    setting["player"]["gold"] = setting["player"].get("gold", 0) + total
                    msgs.append(f"💰 Продано: {item} ×{qty} за {total} 🪙.")
        return msgs


class CraftHandler(DirectiveHandler):
    """Крафт: рецепты (learn/remove), сбор ресурсов, создание."""
    keys = frozenset({"craft_learn", "craft_remove", "gather", "craft"})

    def apply(self, setting: dict, d: dict) -> list[str]:
        msgs: list[str] = []
        p = setting["player"]
        crafts = setting.setdefault("crafts", {})
        if "craft_learn" in d:
            cr = d["craft_learn"]
            if not isinstance(cr, dict):
                cr = {"id": str(cr)}
            rid = str(cr.get("id", "") or "").strip() or (cr.get("name") or "").strip()
            if rid:
                ing = {}
                for it in (cr.get("ingredients") or []):
                    if isinstance(it, dict) and (it.get("name") or "").strip():
                        nm = str(it["name"]).strip()
                        ing[nm] = ing.get(nm, 0) + max(1, _safe_int(it.get("qty", 1), 1))
                ing = [{"name": n, "qty": q} for n, q in ing.items()]
                res = cr.get("result") or cr.get("name")
                if isinstance(res, dict):
                    res = {"name": str(res.get("name", rid)).strip(), "qty": max(1, _safe_int(res.get("qty", 1), 1)),
                           "value": _safe_int(res.get("value"), 0),
                           "weight": float(res.get("weight", 0) or 0),
                           "desc": res.get("desc", "")}
                else:
                    res = {"name": str(res or rid).strip(), "qty": 1, "value": 0, "weight": 0.0, "desc": ""}
                crafts[rid] = {"id": rid, "name": cr.get("name", rid),
                               "result": res, "ingredients": ing,
                               "profession": cr.get("profession", ""), "desc": cr.get("desc", "")}
                # опциональная станция/мастерская (универсально для всех жанров)
                _st = cr.get("station")
                if _st:
                    st_list = _st if isinstance(_st, list) else [str(_st)]
                    st_list = [str(s).strip() for s in st_list if str(s or "").strip()]
                    if st_list:
                        crafts[rid]["station"] = st_list
                msgs.append(f"📘 Изучен рецепт крафта: {cr.get('name', rid)} → {res['name']} ×{res['qty']}")
        if "craft_remove" in d:
            cr = d["craft_remove"]
            rid = cr.get("id") if isinstance(cr, dict) else cr
            if rid and str(rid) in crafts:
                msgs.append(f"📕 Рецепт «{crafts.pop(str(rid)).get('name', rid)}» забыт.")
        if "gather" in d:
            g = d["gather"]
            if not isinstance(g, dict):
                g = {"item": str(g), "qty": 1}
            item = str(g.get("item", "")).strip()
            qty = max(1, int(g.get("qty", 1) or 1))
            if item:
                w = float(g.get("weight", 0) or 0) * qty
                blk = _carry_block("🎒", setting, w)
                if blk:
                    msgs.append(f"🪓 {blk}")
                else:
                    _give_item(setting, item, qty, g.get("desc", ""), float(g.get("weight", 0) or 0))
                    msgs.append(f"🪓 Собрано: {item} ×{qty}")
        if "craft" in d:
            c = d["craft"]
            if isinstance(c, dict):
                rid = str(c.get("recipe", c.get("id", "")) or "").strip() or c.get("name", "") or ""
                qty = max(1, int(c.get("qty", 1) or 1))
            else:
                rid, qty = str(c).strip(), 1
            recipe = crafts.get(rid)
            if recipe is None:
                # найти по имени/результату
                rid2 = next((k for k, v in crafts.items() if v.get("name") == rid or (v.get("result") or {}).get("name") == rid), None)
                recipe = crafts.get(rid2)
            if recipe is None:
                msgs.append(f"🛠 Рецепт «{rid}» не известен персонажу.")
            elif not (recipe.get("ingredients") or []):
                msgs.append(f"🛠 Рецепт «{recipe['name']}» не задаёт материалов — невозможно создать.")
            else:
                # станция/мастерская: рецепт может требовать место (кузница, верстак — для всех жанров)
                _st_req = recipe.get("station") or []
                if _st_req and not any(has_station(setting, x) for x in _st_req):
                    msgs.append(f"🛠 Для «{recipe['name']}» нужна станция: {', '.join(_st_req)} (здесь её нет).")
                    return msgs
                # ремесло: рецепт может требовать профессию (базовую логику проверяет код, профессию меняет мастер)
                _prof_req = str(recipe.get("profession") or "").strip()
                if _prof_req and _prof_req.lower() != str(p.get("profession") or "").strip().lower():
                    msgs.append(f"🛠 Для «{recipe['name']}» нужна профессия «{_prof_req}» (у тебя «{p.get('profession') or 'нет'}»).")
                    return msgs
                inv = p["inventory"]
                need = {it["name"]: it["qty"] * qty for it in recipe["ingredients"]}
                have = {i["name"]: i.get("qty", 0) for i in inv}
                missing = {n: q for n, q in need.items() if have.get(n, 0) < q}
                if missing:
                    msgs.append(f"🛠 Не хватает материалов для «{recipe['name']}»: "
                                + "; ".join(f"{n} ({have.get(n,0)}/{q})" for n, q in missing.items()))
                else:
                    # результат может не поместиться в рюкзак — проверяем до списания ингредиентов
                    res = recipe["result"]
                    w = float(res.get("weight", 0) or 0) * res["qty"] * qty
                    blk = _carry_block("🎒", setting, w)
                    if blk:
                        msgs.append(f"🛠 {blk}")
                        return msgs
                    # списать ингредиенты
                    for n, q in need.items():
                        it = next(i for i in inv if i["name"] == n)
                        it["qty"] -= q
                        if it["qty"] <= 0:
                            inv.remove(it)
                    _give_item(setting, res["name"], res["qty"] * qty, res.get("desc", ""), float(res.get("weight", 0) or 0))
                    msgs.append(f"🛠 Создано: {res['name']} ×{res['qty']*qty} ({recipe['name']})")
        return msgs


class CompanionHandler(DirectiveHandler):
    """Компаньоны: спутники NPC (add/remove/update/apply)."""
    keys = frozenset({"companion_add", "companion_remove", "companion_update", "companion_apply"})

    def apply(self, setting: dict, d: dict) -> list[str]:
        msgs: list[str] = []
        companions = setting.setdefault("companions", {})
        if "companion_add" in d and isinstance(d["companion_add"], dict) and (d["companion_add"].get("id") or "").strip():
            ca = d["companion_add"]
            cid = str(ca["id"]).strip()
            hp = _safe_int(ca.get("hp", 30), 30)
            skills = {}
            if isinstance(ca.get("skills"), dict):
                skills = ca["skills"]
            elif isinstance(ca.get("skills"), list):
                for sk in ca["skills"]:
                    if isinstance(sk, dict) and (sk.get("name") or "").strip():
                        skills[sk["name"]] = {"rank": norm_rank(sk.get("rank", "F")), "kind": sk.get("kind", ""), "desc": sk.get("desc", "")}
            companions[cid] = {"id": cid, "name": ca.get("name", cid), "hp": hp, "max_hp": max(hp, _safe_int(ca.get("max_hp", hp), hp)),
                               "level": max(1, _safe_int(ca.get("level", 1), 1)), "desc": ca.get("desc", ""),
                               "faction": ca.get("faction", ""), "loyalty": _safe_int(ca.get("loyalty", 0)),
                               "skills": skills}
            msgs.append(f"🤝 К отряду присоединился спутник: {ca.get('name', cid)} (⚔ HP {hp})")
        if "companion_remove" in d:
            cr = d["companion_remove"]
            cid = cr.get("id") if isinstance(cr, dict) else cr
            if cid and str(cid) in companions:
                msgs.append(f"💔 Спутник {companions.pop(str(cid)).get('name', cid)} покинул отряд.")
        if "companion_update" in d and isinstance(d["companion_update"], dict) and (d["companion_update"].get("id") or "").strip():
            cu = d["companion_update"]
            comp = companions.get(str(cu["id"]))
            if comp:
                for k in ("name", "desc", "faction"):
                    if cu.get(k) is not None:
                        comp[k] = cu[k]
                if cu.get("level"):
                    comp["level"] = max(1, _safe_int(cu["level"], 1))
                if cu.get("loyalty") is not None:
                    comp["loyalty"] = _safe_int(cu["loyalty"])
                if isinstance(cu.get("skills"), dict):
                    for sname, sval in cu["skills"].items():
                        if isinstance(sval, dict):
                            comp.setdefault("skills", {})[sname] = sval
                msgs.append(f"🤝 Спутник {comp.get('name', cu['id'])} обновлён.")
        if "companion_apply" in d and isinstance(d["companion_apply"], dict):
            cpa = d["companion_apply"]
            comp = companions.get(str(cpa.get("id", "")))
            if comp:
                hp = int(cpa.get("hp", 0))
                comp["hp"] = max(0, min(comp.get("max_hp", comp["hp"]), comp.get("hp", 0) + hp))
                msgs.append(f"{comp.get('name', cpa.get('id'))}: {comp['hp']}/{comp.get('max_hp', comp['hp'])} HP"
                            + (f" (−{abs(hp)})" if hp < 0 else f" (+{hp})"))
                if comp["hp"] <= 0:
                    msgs.append(f"💀 Спутник {comp.get('name', cpa.get('id'))} пал в бою.")
        return msgs


class EnemyHandler(DirectiveHandler):
    """Враги: нанесение урона, добавление, удаление."""
    keys = frozenset({"enemy_apply", "enemy_add", "enemy_remove"})

    def apply(self, setting: dict, d: dict) -> list[str]:
        msgs: list[str] = []
        if "enemy_apply" in d and isinstance(d["enemy_apply"], dict):
            ea = d["enemy_apply"]
            e = setting["enemies"].get(ea.get("id"))
            if e:
                hp = int(ea.get("hp", 0))
                e["hp"] = max(0, e.get("hp", 0) + hp)
                msgs.append(f"{e.get('name', ea.get('id'))}: {e['hp']}/{e.get('max_hp', e['hp'])} HP"
                            + (f" (−{abs(hp)})" if hp < 0 else f" (+{hp})"))
                if e["hp"] <= 0:
                    _bump(setting["player"], "kills")
                    _loot = int(e.get("money", 0) or 0)
                    _txt = f"☠ {e.get('name', ea.get('id'))} повержен!"
                    if _loot > 0:
                        setting["player"]["gold"] = setting["player"].get("gold", 0) + _loot
                        _txt += f" (в кошельке {_loot} 🪙)"
                    msgs.append(_txt)
                    setting["enemies"].pop(ea.get("id"))
        if "enemy_add" in d and isinstance(d["enemy_add"], dict) and (d["enemy_add"].get("id") or "").strip():
            ea = d["enemy_add"]
            # Динамическая сложность: сила врага масштабируется под уровень игрока.
            # Рассказчик задаёт БАЗОВУЮ силу (как для окрестностей 1-го уровня), движок
            # сам усиливает HP/урон, чтобы угроза не отставала от прокачки персонажа.
            _sc = dynamic_adversary_scale(setting.get("player"),
                                          setting.get("_difficulty", "normal"))
            _hp = max(1, int(round(_safe_int(ea.get("hp"), 20) * _sc["hp"])))
            _dmg = max(1, int(round(_safe_int(ea.get("dmg"), 4) * _sc["dmg"])))
            setting["enemies"][str(ea["id"]).strip()] = {"name": ea.get("name", ea["id"]),
                                            "hp": _hp,
                                            "max_hp": _hp,
                                            "dmg": _dmg,
                                            "desc": ea.get("desc", ""),
                                            "money": max(0, _safe_int(ea.get("money"), 0))}
        if "enemy_remove" in d:
            eid = d["enemy_remove"]
            if isinstance(eid, dict):
                eid = eid.get("id")
            setting["enemies"].pop(eid, None)
        return msgs


class QuestHandler(DirectiveHandler):
    """Квесты: создание/обновление (с прогрессом и ступенями), ветвление, цепочки завершения.
    Дополнительные директивы: quest_advance (перейти к следующей стадии), quest_choose (выбрать ветку),
    а quest_done может принимать {id, next} → автоматически запустить следующий квест (цепочка)."""
    keys = frozenset({"quest", "quest_done", "quest_advance", "quest_choose"})

    def apply(self, setting: dict, d: dict) -> list[str]:
        msgs: list[str] = []
        if "quest" in d and isinstance(d["quest"], dict) and (d["quest"].get("id") or "").strip():
            q = d["quest"]
            qid = q["id"]
            existing = setting["quests"].get(qid)
            is_new = existing is None
            if existing:
                existing.update({k: v for k, v in q.items() if k != "id"})
            else:
                setting["quests"][qid] = {
                    "title": q.get("title", qid), "desc": q.get("desc", ""),
                    "status": q.get("status", "active")}
                for k, v in q.items():          # прогресс/ступени/ветки и прочие поля при создании не теряем
                    if k not in ("id", "title", "desc", "status"):
                        setting["quests"][qid][k] = v
            entry = setting["quests"][qid]
            if is_new:
                if entry.get("status") == "active":
                    msgs.append(f"📜 Новый квест: {entry.get('title', qid)}")
            elif "progress" in q and q["progress"] != existing.get("progress"):
                prog = q["progress"]
                msgs.append(f"📜 Прогресс квеста «{entry.get('title', qid)}»: {prog}")
        if "quest_advance" in d and isinstance(d["quest_advance"], dict) and (d["quest_advance"].get("id") or "").strip():
            ad = d["quest_advance"]
            qid = ad["id"]
            quest = setting["quests"].get(qid)
            if quest:
                cur = quest.get("current") or 0
                nm = _quest_next_step(quest, cur, ad.get("step"))
                if nm is not None:
                    quest["current"] = nm
                    quest["progress"] = quest.get("steps_text", {}).get(str(nm)) or \
                        _step_name(quest, nm)
                    msgs.append(f"📜 Квест «{quest.get('title', qid)}» → {quest['progress']}")
                else:
                    msgs.append(f"📜 Квест «{quest.get('title', qid)}»: неизвестный шаг")
        if "quest_choose" in d and isinstance(d["quest_choose"], dict) and (d["quest_choose"].get("id") or "").strip():
            ch = d["quest_choose"]
            qid = ch["id"]
            quest = setting["quests"].get(qid)
            if quest:
                branch = str(ch.get("branch") or "").strip()
                if branch:
                    quest["chosen"] = branch
                    quest["branch"] = branch
                    msgs.append(f"🔀 Квест «{quest.get('title', qid)}»: ветка — {branch}")
        if "quest_done" in d:
            qd = d["quest_done"]
            qid = qd if isinstance(qd, str) else qd.get("id")
            if qid in setting["quests"]:
                setting["quests"][qid]["status"] = "done"
                _bump(setting["player"], "quests_done")
                msgs.append(f"✔ Квест выполнен: {setting['quests'][qid].get('title', qid)}")
            # цепочка: после выполнения автоматически запускаем следующий квест (next)
            if isinstance(qd, dict):
                nxt = qd.get("next")
                if isinstance(nxt, dict) and (nxt.get("id") or "").strip():
                    nid = nxt["id"]
                    if nid not in setting["quests"]:
                        setting["quests"][nid] = {
                            "title": nxt.get("title", nid),
                            "desc": nxt.get("desc", ""), "status": "active"}
                        for k, v in nxt.items():
                            if k not in ("id", "title", "desc", "status"):
                                setting["quests"][nid][k] = v
                        msgs.append(f"📜 Цепочка продолжена — новый квест: {setting['quests'][nid].get('title', nid)}")
                elif isinstance(nxt, str) and nxt.strip() and nxt.strip() in setting["quests"]:
                    if setting["quests"][nxt.strip()].get("status") != "active":
                        setting["quests"][nxt.strip()]["status"] = "active"
                        msgs.append(f"📜 Продолжение: {setting['quests'][nxt.strip()].get('title', nxt)}")
        return msgs


def _step_name(quest: dict, idx) -> str:
    """Название шага по индексу: из steps (список строк или {id/name}) или дефолт."""
    steps = quest.get("steps")
    if isinstance(steps, list) and 0 <= idx < len(steps):
        s = steps[idx]
        return (s.get("name") if isinstance(s, dict) else str(s)) or f"шаг {idx + 1}"
    if isinstance(steps, dict):
        keys = list(steps)
        if 0 <= idx < len(keys):
            return steps[keys[idx]] if isinstance(steps[keys[idx]], str) else keys[idx]
    return f"шаг {idx + 1}"


def _quest_next_step(quest: dict, cur, desire) -> int | None:
    """Следующая ступень: если задан желаемый step — на него; иначе +1."""
    steps = quest.get("steps")
    if desire is not None:
        # по имени/id среди ступеней
        if isinstance(steps, list):
            for i, s in enumerate(steps):
                if isinstance(s, dict) and (str(s.get("id") or "") == str(desire) or str(s.get("name") or "") == str(desire)):
                    return i
                if not isinstance(s, dict) and str(s) == str(desire):
                    return i
        if isinstance(steps, dict):
            keys = list(steps)
            for i, k in enumerate(keys):
                if str(k) == str(desire):
                    return i
        return None
    # авто-вперёд: значение cur — либо индекс (int), либо ищем его позиция в списке
    if isinstance(steps, list):
        sidx = cur if isinstance(cur, int) else next((i for i, s in enumerate(steps) if (s.get('id') if isinstance(s, dict) else s) == cur), -1)
        nxt = (sidx + 1) if (isinstance(sidx, int) and 0 <= sidx < len(steps)) else 0
        return nxt if nxt < len(steps) else len(steps) - 1
    if isinstance(steps, dict):
        keys = list(steps)
        sidx = cur if isinstance(cur, int) else next((i for i, k in enumerate(keys) if str(k) == str(cur)), -1)
        nxt = (sidx + 1) if (isinstance(sidx, int) and 0 <= sidx < len(keys)) else 0
        return nxt if nxt < len(keys) else len(keys) - 1
    # без ступен — простой счётчик стадий
    base = cur if isinstance(cur, int) else 0
    return int(base) + 1


class NpcHandler(DirectiveHandler):
    """NPC: установка характеристик, убийство."""
    keys = frozenset({"npc_set", "npc_kill"})

    def apply(self, setting: dict, d: dict) -> list[str]:
        msgs: list[str] = []
        if "npc_set" in d and isinstance(d["npc_set"], dict) and (d["npc_set"].get("id") or "").strip():
            ns = d["npc_set"]
            existing = setting["npc"].get(ns["id"])
            if existing:
                existing.update({k: v for k, v in ns.items() if k != "id"})
            else:
                setting["npc"][ns["id"]] = {"name": ns.get("name", ns["id"]),
                                            "mood": ns.get("mood", ""),
                                            "alive": ns.get("alive", True),
                                            "desc": ns.get("desc", ""),
                                            "faction": ns.get("faction", "")}
            # деньги-у-НПЦ: у персонажа может быть свой кошелёк (bounty/жалованье/долг)
            _npc = setting["npc"][ns["id"]]
            if ns.get("money") is not None:
                _npc["money"] = max(0, _safe_int(ns.get("money"), 0))
            # Расписание NPC: {время: "что делает/доступен ли"}. Напр. {"ночь": "таверна закрыта", "день": "рынок"}.
            # В format_state показывается активная запись под текущее время — рассказчик учитывает её.
            sch = ns.get("schedule")
            if isinstance(sch, dict):
                setting["npc"][ns["id"]]["schedule"] = {str(k): str(v) for k, v in sch.items() if str(v).strip()}
        if "npc_kill" in d:
            nid = d["npc_kill"]
            nid = nid if isinstance(nid, str) else nid.get("id")
            if nid in setting["npc"]:
                _npc = setting["npc"][nid]
                _npc["alive"] = False
                _loot = int(_npc.get("money", 0) or 0)
                _txt = f"☠ {_npc.get('name', nid)} мёртв."
                if _loot > 0:
                    setting["player"]["gold"] = setting["player"].get("gold", 0) + _loot
                    _txt += f" (найдено {_loot} 🪙)"
                    _npc["money"] = 0  # кошелёк забран
                msgs.append(_txt)
        return msgs


class LocationHandler(DirectiveHandler):
    """Локации: создание/обновление/портал (move) + граф карты мира."""
    keys = frozenset({"location_add", "location_update", "move"})

    def apply(self, setting: dict, d: dict) -> list[str]:
        msgs: list[str] = []
        if "location_add" in d and isinstance(d["location_add"], dict) and (d["location_add"].get("id") or "").strip():
            la = d["location_add"]
            lid = str(la["id"]).strip()
            setting["locations"][lid] = {"name": la.get("name", la["id"]),
                                              "desc": la.get("desc", "")}
            # Сессия 32: эффекты локации-зоны (радиация/туман/проклятие) — сохраняем
            _fx = la.get("effects")
            if isinstance(_fx, list) and _fx:
                setting["locations"][lid]["effects"] = [
                    fx if isinstance(fx, dict) else {"name": str(fx)} for fx in _fx
                ]
            # станции/мастерские локации (для крафта): места, где можно создавать
            _st = la.get("stations")
            if isinstance(_st, list):
                _clean = [str(s).strip() for s in _st if str(s or "").strip()]
                if _clean:
                    setting["locations"][lid]["stations"] = _clean
            # связи для карты мира: добавить опциональные connections
            cons_in = la.get("connections")
            if isinstance(cons_in, list):
                cons = []
                for c in cons_in:
                    if isinstance(c, dict):
                        c = c.get("id") or c.get("name")
                    cs = str(c or "").strip()
                    if cs and cs != lid and cs not in cons:
                        cons.append(cs)
                if cons:
                    setting["locations"][lid]["connections"] = cons
                    # отвечаем двунаправленным ребром на известных локациях (карта мира)
                    for cs in cons:
                        if cs in setting["locations"]:
                            back = setting["locations"][cs].setdefault("connections", [])
                            if lid not in back:
                                back.append(lid)
        if "location_update" in d and isinstance(d["location_update"], dict) and (d["location_update"].get("id") or "").strip():
            lu = d["location_update"]
            lid = str(lu["id"]).strip()
            if lid in setting["locations"]:
                loc = setting["locations"][lid]
                if "name" in lu:
                    loc["name"] = lu["name"]
                if "desc" in lu:
                    loc["desc"] = lu["desc"]
                if "stations" in lu:
                    _st = lu["stations"]
                    if isinstance(_st, list):
                        _clean = [str(s).strip() for s in _st if str(s or "").strip()]
                        loc["stations"] = _clean
                    else:
                        loc.pop("stations", None)
                # Сессия 32: эффекты зоны можно править через location_update
                if "effects" in lu:
                    _fx = lu["effects"]
                    if isinstance(_fx, list) and _fx:
                        loc["effects"] = [fx if isinstance(fx, dict) else {"name": str(fx)} for fx in _fx]
                    else:
                        loc.pop("effects", None)
        if "move" in d:
            dst = d["move"]
            if isinstance(dst, dict):
                dst = dst.get("location")
            if dst in setting["locations"]:
                old = setting.get("current_location")
                if dst != old:
                    setting["current_location"] = dst
                    _bump(setting["player"], "moves")
                    if (setting.get("location_history") or {}).get(dst) is None:
                        _bump(setting["player"], "discoveries")   # впервые в новой локации
                    hist = setting.setdefault("location_history", {})
                    hist[dst] = hist.get(dst, 0) + 1
                    # граф локаций: запоминаем переход (для визуальной карты мира)
                    for a, b in ((old, dst), (dst, old)):
                        if a in setting["locations"]:
                            cons = setting["locations"][a].setdefault("connections", [])
                            if b not in cons:
                                cons.append(b)
                    msgs.append(f"Переход: {setting['locations'][dst].get('name', dst)}")
        return msgs


class ProgressHandler(DirectiveHandler):
    """Статистика/достижения: счётчики пути (progress) и награды (achievement_add).
    Счётчики и достижения копит рассказчик по событиям — код лишь хранит и приращивает."""
    keys = frozenset({"progress_add", "achievement_add"})

    def apply(self, setting: dict, d: dict) -> list[str]:
        msgs: list[str] = []
        p = setting["player"]
        prog = p.setdefault("progress", {})
        ach = p.setdefault("achievements", [])
        if "progress_add" in d and isinstance(d["progress_add"], dict):
            for k, v in d["progress_add"].items():
                key = str(k).strip()[:40]
                prog[key] = prog.get(key, 0) + max(0, _safe_int(v, 0))
        if "achievement_add" in d:
            aa = d["achievement_add"]
            if isinstance(aa, dict) and aa.get("name"):
                nm = str(aa["name"]).strip()[:80]
                if not any(a.get("name") == nm for a in ach if isinstance(a, dict)):
                    ach.append({"name": nm, "desc": str(aa.get("desc") or "")[:200]})
                    msgs.append(f"🏆 Достижение: «{nm}»")
            elif isinstance(aa, str) and aa.strip():
                nm = aa.strip()[:80]
                if not any(a.get("name") == nm for a in ach if isinstance(a, dict)):
                    ach.append({"name": nm, "desc": ""})
                    msgs.append(f"🏆 Достижение: «{nm}»")
        return msgs


class WorldHandler(DirectiveHandler):
    """Мир: флаги, время суток, погода, game_over."""
    keys = frozenset({"flag", "time", "weather", "game_over"})

    def apply(self, setting: dict, d: dict) -> list[str]:
        if "flag" in d and isinstance(d["flag"], dict) and str(d["flag"].get("name", "")).strip():
            f = d["flag"]
            setting["flags"][str(f["name"]).strip()] = f.get("value", True)
        if "time" in d:
            setting["time"] = str(d["time"])
        if "weather" in d:
            setting["weather"] = str(d["weather"])
        if d.get("game_over"):
            setting["game_over"] = True
        return []


class FactionHandler(DirectiveHandler):
    """Фракции: создание/правка/удаление структур данных о фракциях и их связях.
    Фракция keyed по id (как npc.faction / reputation key). Предполагается, что
    рассказчик вводит фракции по мере появления в сюжете; relations. """
    keys = frozenset({"faction_add", "faction_update", "faction_remove", "faction_set"})

    def apply(self, setting: dict, d: dict) -> list[str]:
        msgs: list[str] = []
        factions = setting.setdefault("factions", {})
        if "faction_add" in d:
            fa = d["faction_add"]
            if isinstance(fa, dict):
                fid = str(fa.get("id") or fa.get("name") or "").strip()
                if fid:
                    cur = factions.get(fid, {})
                    cur.setdefault("name", str(fa.get("name") or fid))
                    if fa.get("desc") is not None:
                        cur["desc"] = str(fa["desc"])
                    if fa.get("alignment") is not None:
                        cur["alignment"] = str(fa["alignment"])
                    rels = fa.get("relations")
                    if isinstance(rels, dict):
                        rels_out = cur.setdefault("relations", {})
                        rels_out.update({str(k): str(v) for k, v in rels.items()})
                    factions[fid] = cur
                    msgs.append(f"🏷 Фракция «{cur['name']}»: основана/обновлена.")
        if "faction_set" in d:
            s = d["faction_set"]
            if isinstance(s, dict) and str(s.get("id") or s.get("name") or "").strip():
                fid = str(s.get("id") or s.get("name")).strip()
                cur = factions.get(fid, {"name": str(s.get("name") or fid)})
                for k in ("name", "desc", "alignment"):
                    if s.get(k) is not None:
                        cur[k] = str(s[k])
                rels = s.get("relations")
                if isinstance(rels, dict):
                    rels_out = cur.setdefault("relations", {})
                    rels_out.update({str(k): str(v) for k, v in rels.items()})
                factions[fid] = cur
                msgs.append(f"🏷 Фракция «{cur.get('name', fid)}» записана.")
        if "faction_update" in d:
            fu = d["faction_update"]
            if isinstance(fu, dict) and str(fu.get("id") or "").strip():
                fid = str(fu["id"]).strip()
                if fid in factions:
                    for k in ("name", "desc", "alignment"):
                        if fu.get(k) is not None:
                            factions[fid][k] = str(fu[k])
                    rels = fu.get("relations")
                    if isinstance(rels, dict):
                        rels_out = factions[fid].setdefault("relations", {})
                        rels_out.update({str(k): str(v) for k, v in rels.items()})
                    msgs.append(f"🏷 Фракция «{factions[fid].get('name', fid)}» обновлена.")
                else:
                    msgs.append(f"Фракция «{fid}» не найдена.")
        if "faction_remove" in d:
            fr = d["faction_remove"]
            fid = str((fr.get("id") if isinstance(fr, dict) else fr) or "").strip()
            if fid and fid in factions:
                factions.pop(fid)
                msgs.append(f"🏷 Фракция «{fid}» убрана (репутация/флаги сохранены).")
        return msgs


class TimerHandler(DirectiveHandler):
    """Таймеры мира (дедлайны): timer_add / timer_remove. Тик — tick_world_timers."""
    keys = frozenset({"timer_add", "timer_remove"})

    def apply(self, setting: dict, d: dict) -> list[str]:
        msgs: list[str] = []
        timers = setting.setdefault("timers", {})
        if not isinstance(timers, dict):
            timers = {}
            setting["timers"] = timers
        if "timer_add" in d:
            ta = d["timer_add"]
            if isinstance(ta, dict) and ta.get("name"):
                name = str(ta["name"])[:60]
                _turns_raw = ta.get("turns", -1)
                try:
                    turns = int(_turns_raw)
                except (TypeError, ValueError):
                    turns = -1
                timers[name] = {"turns_left": turns, "desc": str(ta.get("desc", ""))[:200]}
                dur = "без срока" if turns in (-1, None) else f"{turns} ход."
                msgs.append(f"⏳ Таймер «{name}» запущен ({dur})" + (f" — {ta.get('desc')}" if ta.get("desc") else ""))
        if "timer_remove" in d:
            tr = d["timer_remove"]
            name = str((tr.get("name") if isinstance(tr, dict) else tr) or "").strip()
            if name and name in timers:
                timers.pop(name, None)
                msgs.append(f"⏰ Таймер «{name}» убран.")
        return msgs


class EquipHandler(DirectiveHandler):
    """Экипировка: equip {item} / unequip {item} — надевание/снятие снаряжения по слотам."""
    keys = frozenset({"equip", "unequip"})

    def apply(self, setting: dict, d: dict) -> list[str]:
        msgs: list[str] = []
        if "equip" in d:
            eq = d["equip"]
            item_name = eq.get("item") if isinstance(eq, dict) else eq
            if isinstance(item_name, dict):
                item_name = item_name.get("name") or item_name.get("item")
            ok, msg = equip_item(setting, str(item_name or ""))
            msgs.append(msg if ok else f"❌ {msg}")
        if "unequip" in d:
            ue = d["unequip"]
            item_name = ue.get("item") if isinstance(ue, dict) else ue
            if isinstance(item_name, dict):
                item_name = item_name.get("name") or item_name.get("item")
            ok, msg = unequip_item(setting, str(item_name or ""))
            msgs.append(msg if ok else f"❌ {msg}")
        return msgs


class NeedsHandler(DirectiveHandler):
    """Потребности/рассудок: needs {голод: {value, max, decay}} — дельты или перезапись."""
    keys = frozenset({"needs"})

    def apply(self, setting: dict, d: dict) -> list[str]:
        msgs: list[str] = []
        p = setting["player"]
        needs = _ensure_axis(p, "needs")
        mental = _ensure_axis(p, "mental")
        nd = d["needs"]
        if not isinstance(nd, dict):
            return msgs
        for k, v in nd.items():
            key = str(k).strip().lower()
            if key in needs:
                _axis_update(needs[key], v)
            elif key in mental:
                _axis_update(mental[key], v)
            else:
                continue
            val = needs[key]["value"] if key in needs else mental[key]["value"]
            msgs.append(f"📊 {k}: {val:.0f}/{needs[key]['max'] if key in needs else mental[key]['max']:.0f}.")
        return msgs


def _axis_update(cur: dict, v: Any) -> None:
    """Обновить шкалу {value,max,decay}.
    Число = дельта (как player.hp); dict: {value: дельта, max, decay, set: абсолют}."""
    mx = float(cur.get("max", 100) or 100)
    if isinstance(v, (int, float)):
        cur["value"] = max(0.0, min(mx, float(cur.get("value", mx)) + float(v)))
    elif isinstance(v, dict):
        if v.get("max") is not None:
            mx = float(v["max"])
            cur["max"] = mx
        if v.get("decay") is not None:
            cur["decay"] = float(v["decay"])
        if v.get("set") is not None:
            cur["value"] = max(0.0, min(mx, float(v["set"])))
        elif v.get("value") is not None:
            cur["value"] = max(0.0, min(mx, float(cur.get("value", mx)) + float(v["value"])))
        else:
            cur["value"] = max(0.0, min(mx, float(cur.get("value", mx))))


class BoardHandler(DirectiveHandler):
    """Доска объявлений мира: board_add {title, text} (чистое отображение, закон 2)."""
    keys = frozenset({"board_add"})

    def apply(self, setting: dict, d: dict) -> list[str]:
        msgs: list[str] = []
        ba = d["board_add"]
        if isinstance(ba, dict):
            msgs.append(board_add(setting, str(ba.get("title") or ""), str(ba.get("text") or "")))
        elif isinstance(ba, str) and ba.strip():
            msgs.append(board_add(setting, ba, ""))
        return msgs


class FactionRankHandler(DirectiveHandler):
    """Звания во фракциях: faction_rank {faction, rank} — должность игрока (не репутация)."""
    keys = frozenset({"faction_rank"})

    def apply(self, setting: dict, d: dict) -> list[str]:
        msgs: list[str] = []
        p = setting["player"]
        fr = d["faction_rank"]
        if isinstance(fr, dict) and str(fr.get("faction") or "").strip():
            fid = str(fr["faction"]).strip()
            rank = str(fr.get("rank") or "").strip()
            if rank:
                p.setdefault("faction_ranks", {})[fid] = rank
                msgs.append(f"🏅 Звание во фракции: {fid} — «{rank}».")
            else:
                p.setdefault("faction_ranks", {}).pop(fid, None)
                msgs.append(f"🏅 Звание во фракции {fid} снято.")
        return msgs


class CalendarHandler(DirectiveHandler):
    """Календарь мира: date {day, month, season} — сезоны дают моды (см. environment_mods)."""
    keys = frozenset({"date"})

    def apply(self, setting: dict, d: dict) -> list[str]:
        msgs: list[str] = []
        dt = d["date"]
        if isinstance(dt, dict):
            cur = setting.setdefault("date", {})
            if isinstance(cur, str):
                cur = {}
                setting["date"] = cur
            for k in ("day", "month", "season"):
                if dt.get(k) is not None:
                    cur[k] = str(dt[k])
            msgs.append("📅 Дата обновлена: " + ", ".join(f"{k}={v}" for k, v in cur.items() if v))
        return msgs


class VisionHandler(DirectiveHandler):
    """Сны/видения: vision_add {text, hint} — в очередь; trigger_vision {} — разыграть."""
    keys = frozenset({"vision_add", "trigger_vision"})

    def apply(self, setting: dict, d: dict) -> list[str]:
        msgs: list[str] = []
        pv = setting.setdefault("pending_visions", [])
        if not isinstance(pv, list):
            pv = []
            setting["pending_visions"] = pv
        if "vision_add" in d:
            va = d["vision_add"]
            if isinstance(va, dict) and (va.get("text") or va.get("hint")):
                pv.append({"text": str(va.get("text") or "")[:600],
                           "hint": str(va.get("hint") or "")[:300]})
                msgs.append("🌙 Видение добавлено в очередь (сыграет при отдыхе/сне).")
        if "trigger_vision" in d:
            msgs.append("🌙 Мастер призывает видение…")
        return msgs


# Цепочка обработчиков: порядок сохраняет прежний порядок сообщений в выводе.
DIRECTIVE_CHAIN: list[DirectiveHandler] = [
    PlayerHandler(),
    IdentityHandler(),
    SkillHandler(),
    AbilityHandler(),
    EffectHandler(),
    ItemHandler(),
    EconomyHandler(),
    CraftHandler(),
    CompanionHandler(),
    EnemyHandler(),
    QuestHandler(),
    ProgressHandler(),
    NpcHandler(),
    FactionHandler(),
    TimerHandler(),
    EquipHandler(),
    NeedsHandler(),
    BoardHandler(),
    FactionRankHandler(),
    CalendarHandler(),
    VisionHandler(),
    LocationHandler(),
    WorldHandler(),
]


def apply_directives(setting: dict, d: dict) -> list[str]:
    """Применяет директивы к состоянию мира через ЦЕПОЧКУ ОБЯЗАННОСТЕЙ.

    Запрос (dict директив, прогоняемый через normalize_directives) проходит по
    DIRECTIVE_CHAIN; каждое звено обрабатывает только свою группу ключей и
    возвращает свои системные сообщения. На входе достраивается схема игрока,
    на выходе — общий финальный чек смерти. Возвращает список системных сообщений.
    """
    p = setting["player"]
    ensure_player_schema(p)
    d = normalize_directives(d)  # str вместо dict → правильные типы (иначе ход падает)

    msgs: list[str] = []
    for handler in DIRECTIVE_CHAIN:
        if handler.handles(d):
            # директивы НЕ должны ронять ход: сбой одного обработчика изолирован,
            # не ломает остальные (правило 14 — логируем с контекстом, не глотаем молча)
            try:
                msgs.extend(handler.apply(setting, d))
            except Exception as e:
                log.warning("DirectiveHandler %r упал на %s: %s",
                            type(handler).__name__, sorted(handler.keys & d.keys()), e,
                            exc_info=True)
    # смерть игрока
    if p["hp"] <= 0 and not setting.get("game_over"):
        setting["game_over"] = True
        msgs.append("💀 Игрок погиб. Мир замирает…")
    return msgs
