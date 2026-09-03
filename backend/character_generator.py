# -*- coding: utf-8 -*-
"""
character_generator.py — генерация персонажа и вступительной сцены (декомпозиция narrator.py).

Перенесено из narrator.py (verbatim, семантика не менялась):
- generate_identity — биография «кто я» (зацеп игрока → LLM-генерация по теме/лору);
- generate_character — богатый старт (раса/класс/профессия/статы/уровень/навыки/инвентарь);
- apply_character — применение результата к состоянию (через движок директив);
- generate_opening — вступительная сцена (с инжектом лора и персонажа);
- вспомогательные: _STAT_LU, _MODERN_GENRES, _is_modern_world, _cut_words.

Публичное API сохраняет имена/сигнатуры narrator.py (narrator.generate_character(...) и т.п.
работают через реэкспорт в конце narrator.py).
"""
from __future__ import annotations

import json
import random

from . import db, llm
from .config import get_config
from .mechanics import (RACE_NAMES, CLASS_NAMES, PROFESSION_NAMES,
                        ensure_player_schema, norm_rank, recalc_derived, apply_directives)

from .logsetup import get_logger, log_once

log = get_logger(__name__)


# Маппинг ключей характеристик: модель может вернуть статы англоязычными ключами
# (strength/agility/intellect/endurance/...), а p["stats"] ждёт русские (сила/ловкость/...).
_STAT_LU = {
    "сила": "сила", "strength": "сила",
    "ловкость": "ловкость", "agility": "ловкость", "dexterity": "ловкость",
    "выносливость": "выносливость", "endurance": "выносливость", "constitution": "выносливость",
    "интеллект": "интеллект", "intelligence": "интеллект", "intellect": "интеллект",
    "мудрость": "мудрость", "wisdom": "мудрость",
    "харизма": "харизма", "charisma": "харизма",
    "удача": "удача", "luck": "удача",
}

_MODERN_GENRES = ("реалрпг", "литрпг", "киберпанк", "научная фантастика", "постапокалипсис",
                  "детектив", "вестерн", "зомби", "хроно", "марсиан",
                  "викторианский детектив", "шпион", "супергероика", "стимпанк", "космическая опера",
                  "сюрреалист", "триллер", "ужасы", "хоррор", "японская мистика", "современн")


def _is_modern_world(genre: str) -> bool:
    """Современные/техно/реал-миры (реалрпг, литрпг, киберпанк, постапокалипсис и т.п.),
    где фэнтезийный класс (Воин/Маг) выглядит неуместным."""
    g = (genre or "").lower()
    return any(k in g for k in _MODERN_GENRES)


def _cut_words(text: str, limit: int) -> str:
    """Обрезает текст по границе слова, чтобы системные сообщения судьи/искажения
    не обрывались на пол-слове при превышении лимита."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    sp = cut.rfind(" ")
    if sp > limit // 2:
        cut = cut[:sp]
    return cut.rstrip() + "…"


async def generate_identity(theme: dict, setting: dict, hook: str = "",
                            lang: str = "ru", provider: dict | None = None) -> str:
    """Биография персонажа (кто он, откуда, чем занимается, текущее положение).
    Если игрок дал свой зацеп (hook) — используется он (уже сохранён в player.identity).
    Иначе LLM генерирует биографию по теме/лору, чтобы персонаж не был безымянным «Путником».
    Возвращает компактный текст (1–3 предложения)."""
    ident = (setting.get("player", {}) or {}).get("identity", "").strip()
    if ident:
        return ident
    if hook.strip():
        return hook.strip()[:400]
    lang_instr = ("Опиши персонажа на русском языке" if lang == "ru"
                  else "Describe the character in English")
    style = theme.get("style", "")[:400]
    desc = theme.get("desc", "")[:300]
    messages = [
        {"role": "system", "content": (
            "Ты — мастер текстовой RPG. Создай компактную биографию-заготовку игрока (1–3 предложения): "
            "имя, возраст, происхождение, занятие/прошлое и текущее положение в мире. Без воды, без разметки. "
            "Персонаж — обычный житель этого мира, у которого пробудилась/появилась способность или завязка приключения."
        )},
        {"role": "user", "content": f"{lang_instr}.\nМир: {desc}\nСтиль: {style}\nВерни только биографию персонажа."},
    ]
    try:
        out = (await llm.complete(messages, temperature=get_config().default_temp,
                                  max_tokens=500, provider=provider)).strip()
        return out[:1600] if out else ""
    except Exception:
        return ""


async def generate_character(theme: dict, setting: dict, hook: str = "",
                             lang: str = "ru", provider: dict | None = None, lore=None) -> dict | None:
    """Обогащённая первая генерация персонажа (доп. LLM-вызов при создании мира).

    `lore` — список статей лора (dict с `content` или строка), чтобы персонаж
    не противоречил «библии» вселенной. Возвращает dict полей или None при неудаче."""
    lang_instr = ("на русском языке" if lang == "ru" else "in English")
    style = theme.get("style", "")[:400]
    desc = theme.get("desc", "")[:300]
    hook_txt = hook.strip()
    lore_block = ""
    if lore:
        items = [x.get("content") if isinstance(x, dict) else str(x) for x in lore]
        items = [s for s in items if s and str(s).strip()]
        if items:
            lore_block = "\nЛор мира (учитывай его для согласованности персонажа с каноном):\n" + "\n".join(str(s).strip()[:800] for s in items)[:2500]
    # Порядок вариантов перемешиваем на каждом вызове — локальные/облачные LLM часто
    # «залипают» на позиционную предвзятость (первый/последний в списке -> стабильно
    # человек/вор/книжник). Случайный порядок раскоррелирует выбор между мирами.
    races = ", ".join(random.sample(RACE_NAMES, len(RACE_NAMES)))
    classes = ", ".join(random.sample(CLASS_NAMES, len(CLASS_NAMES)))
    profs = ", ".join(random.sample(PROFESSION_NAMES, len(PROFESSION_NAMES)))
    system = (
        "Ты — мастер текстовой RPG и генератор персонажей. Придумай игроку ПОЛНОГО персонажа, "
        "согласованного с миром, лором и его биографией. " + (lore_block + " " if lore_block else "") +
        "Если по биографии/лору персонаж стар и опытен "
        "(бессмертный маг 900 лет, ветеран, лорд) — статы, уровень, навыки и инвентарь должны отражать такую "
        "жизнь, а не старт новичка (уровень выше 1, характеристики не все 10, уместные навыки и предметы). "
        "Выбирай расу/класс/профессию ОСМЫСЛЕННО под биографию и мир, а не по шаблону (НЕ своди всё к "
        "человек/вор/книжник): для разных миров и биографий персонажи должны отличаться (эльф/дварф/орк, "
        "воин/маг/жрец, кузнец/алхимик/охотник и т.п.), если это уместно, особенно в фэнтезийном мире. "
        "Верни ТОЛЬКО валидный JSON без пояснений и без разметки со следующими ключами:\n"
        "name (строка, имя персонажа), "
        "identity (строка, биография 1–3 предложения: имя, возраст, происхождение, занятие, текущее положение; "
        "если игрок дал зацеп — РАЗВЕРНИ его в полновесную биографию, сохранив все его детали (имя/возраст/характер/прошлое), "
        "не просто повторяй зацеп дословно), "
        "race (одна из: " + races + " или творческая), "
        "class (одна из: " + classes + " или творческая), "
        "profession (одна из: " + profs + " или творческая, либо пустая строка ''), "
        "level (целое 1..99, согласуй с биографией), "
        "stats (объект с 7 ключами: сила, ловкость, выносливость, интеллект, мудрость, харизма, удача; "
        "числа 3..20, распредели по характеру и роли, НЕ все 10), "
        "skills (массив объектов {name, rank, kind, desc, mp_cost} — 0..5 навыков под роль), "
        "inventory (массив объектов {name, qty, desc} — 2..6 предметов с описанием, персонализированные под "
        "биографию и роль, не стандартный стартовый набор), "
        "titles (массив строк — титулы, опционально)."
    )
    user = (
        f"Создай персонажа {lang_instr}. Мир: {desc}. Стиль: {style}."
        + (f" Зацеп игрока (используй его как ОСНОВУ биографии identity — сохранив все детали и развив в полные 1–3 предложения): {hook_txt}" if hook_txt else "")
        + "\nЗаполни инструмент create_character."
    )
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    # ── Генерация через function calling (tools): модель ОБЯЗАНА вернуть валидный JSON-аргумент
    # инструмента со ВСЕМИ обязательными полями. Это надёжнее, чем просить «верни JSON текстом»:
    # reasoner-модели (deepseek-v4-flash) часто выдают битый/оборванный JSON — а tools-режим
    # возвращает строго типизированные аргументы.
    char_tool = [{"type": "function", "function": {
        "name": "create_character",
        "description": "Создаёт полного игрового персонажа: личность, роль, статы, навыки, инвентарь.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Имя персонажа"},
                "identity": {"type": "string", "description": "Биография 1–3 предложения: имя, возраст, происхождение, занятие, текущее положение. Если игрок дал зацеп — разверни его в полновесную биографию, сохранив все детали."},
                "race": {"type": "string", "description": "Раса: " + races + " или творческая"},
                "class": {"type": "string", "description": "Класс: " + classes + " или творческий"},
                "profession": {"type": "string", "description": "Профессия: " + profs + " или творческая, либо пустая строка"},
                "level": {"type": "integer", "minimum": 1, "maximum": 99, "description": "Уровень, согласуй с биографией"},
                "stats": {"type": "object", "description": "7 ключей: сила, ловкость, выносливость, интеллект, мудрость, харизма, удача; числа 3..20, НЕ все 10",
                    "properties": {"сила": {"type": "integer"}, "ловкость": {"type": "integer"}, "выносливость": {"type": "integer"}, "интеллект": {"type": "integer"}, "мудрость": {"type": "integer"}, "харизма": {"type": "integer"}, "удача": {"type": "integer"}}},
                "skills": {"type": "array", "description": "0..5 навыков под роль",
                    "items": {"type": "object", "properties": {"name": {"type": "string"}, "rank": {"type": "string", "description": "F/E/D/C/B/A/S/SS/SSS/Z/ZZ/ZZZ/G или цифра"}, "kind": {"type": "string"}, "desc": {"type": "string"}, "mp_cost": {"type": "integer"}}}},
                "inventory": {"type": "array", "description": "2..6 предметов с описанием, персонализированные",
                    "items": {"type": "object", "properties": {"name": {"type": "string"}, "qty": {"type": "integer"}, "desc": {"type": "string"}}}},
                "titles": {"type": "array", "description": "Титулы (опционально)", "items": {"type": "string"}},
            },
            "required": ["name", "identity", "race", "class", "level", "stats", "skills", "inventory"],
            "additionalProperties": False,
        },
    }}]
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            tool_calls_out: list[dict] = []
            await llm.complete(messages, temperature=max(0.4, get_config().default_temp),
                               max_tokens=2400, provider=provider,
                               tools=char_tool, tool_choice="required",
                               tool_calls_out=tool_calls_out)
        except Exception as e:
            log.warning("generate_character (попытка %d/%d): ошибка LLM: %s",
                        attempt + 1, max_attempts, e)
            if attempt == max_attempts - 1:
                return None
            continue
        if not tool_calls_out:
            log.warning("generate_character (попытка %d/%d): модель не вернула tool_call — повторяем",
                        attempt + 1, max_attempts)
            if attempt == max_attempts - 1:
                return None
            continue
        args = (tool_calls_out[0].get("arguments") or "").strip()
        if not args:
            log.warning("generate_character (попытка %d/%d): пустые аргументы tool_call — повторяем",
                        attempt + 1, max_attempts)
            if attempt == max_attempts - 1:
                return None
            continue
        try:
            data = json.loads(args)
            if not isinstance(data, dict) or not data:
                raise ValueError("пустой dict")
            # Нормализация через движковый парсер (вложенные JSON-строки и т.п.)
            from .narrator import parse_tool_args
            data = parse_tool_args(data) or data
            return data if isinstance(data, dict) and data else None
        except Exception as e:
            log.warning("generate_character (попытка %d/%d): аргументы не JSON (%s) — повторяем",
                        attempt + 1, max_attempts, str(e)[:80])
            if attempt == max_attempts - 1:
                return None
            continue
    return None


def _fallback_stats(cls: str = "", prof: str = "") -> dict:
    """Распределяет статы по роли, если модель не вернула валидный stats (фолбэк).
    Профильный стат — выше, остальные варьируются вокруг 10, но не все 10."""
    base = {"сила": 10, "ловкость": 10, "выносливость": 10, "интеллект": 10,
            "мудрость": 10, "харизма": 10, "удача": 10}
    cl = (cls or "").lower()
    pr = (prof or "").lower()
    # профильный стат по классу
    prime = {"воин": "сила", "лучник": "ловкость", "маг": "интеллект", "вор": "ловкость",
             "жрец": "мудрость", "бард": "харизма"}.get(cl, "")
    if not prime:
        # по профессии
        prime = {"кузнец": "сила", "алхимик": "интеллект", "травник": "мудрость",
                 "охотник": "ловкость", "шахтёр": "сила", "повар": "удача",
                 "портной": "ловкость", "моряк": "выносливость", "книжник": "интеллект"}.get(pr, "")
    if prime:
        base[prime] = 16
        # вторичный стат чуть выше
        second = {"воин": "выносливость", "лучник": "ловкость", "маг": "мудрость",
                  "вор": "удача", "жрец": "интеллект", "бард": "харизма"}.get(cl, "удача")
        if second in base:
            base[second] = 13
    # небольшие вариации для остальных (не все 10)
    import random as _r
    for k in base:
        if base[k] == 10:
            base[k] = max(8, min(13, 10 + _r.randint(-2, 3)))
    return base


def apply_character(setting: dict, data: dict, hook: str = "", genre: str = "") -> list[str]:
    """Применяет результат generate_character к состоянию мира (заполненная роль на старте).
    Расу/класс/профессию/навыки применяет через движок директив (бонусы, пассивки, стартовые навыки).
    Вызывается и при пустом data (фолбэк): заполняет схему игрока и даёт гарантию минимальной роли,
    чтобы персонаж не оставался «нулевым» (статы 10, пустые раса/класс/профессия)."""
    msgs: list[str] = []
    p = setting["player"]
    ensure_player_schema(p)
    if not isinstance(data, dict) or not data:
        # Фолбэк: роль не сгенерирована — гарантируем минимальную человеческую роль,
        # чтобы мир не стартовал с пустым персонажем. Класс оставляем рассказчику (правило 19).
        if _is_modern_world(genre):
            apply_directives(setting, {"race_change": "Человек"})
            if not (p.get("profession") or "").strip():
                apply_directives(setting, {"profession": {"name": "Безработный", "buff": {}}})
        else:
            apply_directives(setting, {"race_change": "Человек"})
            if not (p.get("class") or "").strip():
                best = max(p["stats"], key=lambda k: p["stats"][k]) if p["stats"] else "сила"
                cls_by_stat = {
                    "сила": "Воин", "ловкость": "Вор", "выносливость": "Воин",
                    "интеллект": "Маг", "мудрость": "Жрец", "харизма": "Бард", "удача": "Вор",
                }
                apply_directives(setting, {"class": cls_by_stat.get(best, "Воин")})
        if not (p.get("identity") or "").strip():
            p["identity"] = f"{p.get('name','Путник')} — житель этого мира."
        recalc_derived(p, setting.get("_difficulty", "normal"))
        return msgs
    # Имя
    nm = str(data.get("name") or "").strip()
    if nm:
        p["name"] = nm[:60]
    # Биография «кто я»: приоритет у ОБОГАЩЁННОГО генератором варианта (он получает зацеп игрока
    # как основу и разворачивает его в полные 1–3 предложения). Сырой зацеп — только если LLM
    # не вернул identity (фолбэк), чтобы в биографию не попадал голый текст игрока.
    ident = str(data.get("identity") or "").strip()
    if ident:
        p["identity"] = ident[:1600]
    elif hook and hook.strip():
        p["identity"] = hook.strip()[:1600]
    elif not (p.get("identity") or "").strip():
        p["identity"] = f"{p.get('name','Путник')} — житель этого мира."
    # Раса (движок: бонус + пассивка)
    race = data.get("race")
    if race:
        msgs += apply_directives(setting, {"race_change": race})
    # Класс (движок: стартовый навык)
    cls = data.get("class")
    if cls:
        if isinstance(cls, dict):
            msgs += apply_directives(setting, {"class": {"name": cls.get("name", ""), "skill": cls.get("skill")}})
        else:
            msgs += apply_directives(setting, {"class": str(cls)})
    # Профессия (движок: буфф)
    prof = data.get("profession")
    if prof and str(prof).strip():
        msgs += apply_directives(setting, {"profession": str(prof)})
    # Уровень
    try:
        lv = int(data.get("level") or 0)
        if 1 <= lv <= 99:
            p["level"] = lv
    except Exception:
        # легальный фолбэк: модель могла не вернуть level — но если вернула мусор, это видно в debug
        log.debug("generate/apply character: уровень не разобран из %r", data.get("level"))
    # Статы (абсолютные значения). Если модель не вернула валидные статы (None/не dict) —
    # распределяем сами по роли, чтобы персонаж не был «все 10».
    st = data.get("stats")
    if not isinstance(st, dict) or not st:
        st = _fallback_stats(data.get("class") or "", data.get("profession") or "")
        data["stats"] = st
    for k, v in st.items():
        ru = _STAT_LU.get(str(k).strip().lower())
        if ru and ru in p["stats"]:
            try:
                p["stats"][ru] = max(1, min(99, int(v)))
            except Exception:
                log.debug("персонаж: стат %s не разобран из %r", k, v)
    # Навыки
    for sk in (data.get("skills") or []):
        if isinstance(sk, str) and str(sk).strip():
            p["skills"].setdefault(str(sk).strip()[:60], {"rank": "F", "kind": "универсальное", "desc": "", "mp_cost": 0})
        elif isinstance(sk, dict) and str(sk.get("name") or "").strip():
            sn = str(sk["name"]).strip()[:60]
            p["skills"][sn] = {"rank": norm_rank(sk.get("rank") or "F"),
                                "kind": str(sk.get("kind") or "универсальное")[:60],
                                "desc": str(sk.get("desc") or ""),
                                "mp_cost": int(sk.get("mp_cost", 0) or 0)}
    # Инвентарь (персонализированный, если валиден) — с описаниями
    inv = data.get("inventory")
    if isinstance(inv, list) and inv:
        clean = []
        for it in inv:
            if isinstance(it, str) and it.strip():
                clean.append({"name": it.strip()[:60], "qty": 1, "desc": ""})
            elif isinstance(it, dict) and str(it.get("name") or "").strip():
                clean.append({"name": str(it["name"]).strip()[:60], "qty": max(1, int(it.get("qty", 1) or 1)),
                              "desc": str(it.get("desc") or "").strip()[:300]})
        if clean:
            p["inventory"] = clean
    # Титулы
    tits = data.get("titles")
    if isinstance(tits, list):
        p["titles"] = [str(t).strip()[:60] for t in tits if str(t).strip()][:10]
    # Производные HP/MP от статов с учётом сложности
    recalc_derived(p, setting.get("_difficulty", "normal"))
    # Персонаж только что создан — HP/MP должны быть полными по новым максимумам (не стартовыми 100/50)
    p["hp"] = p["max_hp"]
    p["mp"] = p["max_mp"]
    # ── Гарантия роли: даже если LLM не заполнил расу/класс/профессию — блок не пустой. ──
    # Фолбэк контекстно-зависимый: для «современных»/реал-миров (реалрпг, литрпг, техно и т.п.)
    # не пихаем фэнтезийный класс со стартовым навыком — назначаем нейтральную человеческую роль,
    # класс оставляем мастеру/рассказчику, а профессию — «безработный». Для фэнтези — класс по статам.
    if not (data.get("race") or "").strip():
        apply_directives(setting, {"race_change": "Человек"})
    if _is_modern_world(genre):
        # раса уже «Человек»; класс не форсируем (в реал/литрпг его даёт рассказчик директивой class)
        if not (p.get("profession") or "").strip() and not (data.get("profession") or "").strip():
            apply_directives(setting, {"profession": {"name": "Безработный", "buff": {}}})
    else:
        if not (data.get("class") or "").strip():
            best = max(p["stats"], key=lambda k: p["stats"][k]) if p["stats"] else "сила"
            cls_by_stat = {
                "сила": "Воин", "ловкость": "Вор", "выносливость": "Воин",
                "интеллект": "Маг", "мудрость": "Жрец", "харизма": "Бард", "удача": "Вор",
            }
            apply_directives(setting, {"class": cls_by_stat.get(best, "Воин")})
    return msgs


async def generate_opening(world: dict, setting: dict, persona: str | None = None,
                           provider: dict | None = None) -> str:
    theme = _world_theme(world, setting)
    hook = world.get("custom_hook", "").strip()
    lang = world.get("language", "ru")
    lang_instr = ("напиши вступление на русском языке" if lang == "ru" else "write the opening in English")

    # ── Лор в открытие: чтобы вступительная сцена сразу отражала библию мира ──
    lore_block = ""
    try:
        entries = db.list_lore(world["id"])
        if entries:
            core = [f"• {e['title']}: {e['content'][:500]}" for e in entries if e.get("is_core")]
            extra = [f"• {e['title']}: {e['content'][:350]}" for e in entries if not e.get("is_core")]
            lore_block = "\n\n[ЛОР МИРА — факты вселенной, по ним строй сцену]\n" + "\n".join((core + extra)[:6])
    except Exception as e:
        log_once(log, "opening-lore", 30,
                 "открытие мира: лор мира не подан в промпт (world %s): %s", world["id"], e)

    # Персонаж: из зацепа игрока или из уже заданной identity в состоянии
    ident = setting.get("player", {}).get("identity", "").strip()
    if hook:
        ident = ident or hook
    ident_instr = (
        f"\nПерсонаж игрока (кто он, откуда, чем занимается): {ident}"
        if ident else "\nОпиши персонажа игрока: кто он, откуда, чем занимается, что привело его к этому моменту."
    )

    # Лимит открытия: явно больше, чтобы не обрезалось на полу/слове.
    # Число символов ≈ токены × 3.2. Для reasoner-моделей (deepseek-v4-flash и т.п.) часть
    # max_tokens уходит на reasoning — поэтому берём с запасом (2400), а не 1600.
    OPENING_MAX_TOKENS = 2400
    opening_chars = int(round(OPENING_MAX_TOKENS * 3.2))
    from .narrator import build_system_prompt  # локально: избегаем цикла импортов
    messages = [
        {"role": "system", "content": build_system_prompt(world, setting, persona=persona) + lore_block},
        {"role": "user", "content":
            f"Открой игру: {lang_instr}. Создай вступительную сцену (3–5 абзацев): "
            f"СНАЧАЛА чётко представь персонажа игрока — кто он (имя, возраст, происхождение, занятие, как оказался здесь, цель/мотив). "
            f"Эта часть должна сразу дать игроку понять, кем он является, без догадок. Затем опиши место (согласно состоянию ниже) "
            f"и ближайшую зацепку/интригу. Вплети персонажа в сцену, а не просто назови. "
            f"Объём вступления — не более примерно {opening_chars} символов (~{OPENING_MAX_TOKENS} токенов): "
            f"укладывайся в лимит законченными предложениями, НЕ обрывай текст на полуслове/полуфразе "
            f"— обязательно доведи каждую мысль до конца."
            f"{ident_instr} Заверши 2–3 намёками, что можно сделать. Не используй блок <<ENGINE>> в этот раз.\n"
            f"Заготовка сюжета: {theme.get('opening', hook or 'Начало приключения')}"
            f"\nВАЖНО: заготовка сюжета выше — лишь сюжетная канва для вдохновения, а не обязательные факты. "
            f"Согласуй её с реальным положением персонажа из состояния выше (курс/статус/роль/история, identity). "
            f"Если заготовка противоречит состоянию (например, там «зачислены на первый курс», а персонаж уже учится на старших "
            f"курсах и давно живёт в общежитии) — НЕ пиши это буквально: либо преподнеси как странность/подделку, которую персонаж "
            f"сразу распознаёт как нелепую, либо опусти противоречащие детали."
            + (f"\nДобавочный вводный хук игрока: {hook}" if hook else "")},
    ]
    # ── Ретрай при пустом ответе: reasoner-модели (deepseek-v4-flash и т.п.) иногда тратят
    # весь max_tokens на reasoning и не успевают выдать content → opening пустой, в чате
    # пустое событие рассказчика. Повторяем до 3 раз; при полном провале — завязка сюжета
    # (theme.opening / hook), чтобы вступительное сообщение НИКОГДА не было пустым.
    opening_fallback = (theme.get("opening") or hook or "Мир пробуждается. Что ты делаешь?").strip()
    best = ""
    for attempt in range(3):
        fin: dict = {}
        try:
            out = (await llm.complete(messages,
                                      temperature=get_config().default_temp + 0.05,
                                      max_tokens=OPENING_MAX_TOKENS, provider=provider,
                                      finish_out=fin)).strip()
        except Exception as e:
            log.warning("generate_opening (попытка %d/3): ошибка LLM: %s", attempt + 1, e)
            out = ""
        if not out:
            log.warning("generate_opening (попытка %d/3): пустой ответ модели — повторяем", attempt + 1)
            continue
        from .narrator import split_engine  # локально: избегаем цикла импортов
        clean, _ = split_engine(out)  # noqa
        res = (clean or out).strip()
        # «Вступление дописано» = мысль кончается знаком конца предложения (хвостовые
        # кавычки/скобки не в счёт — см. _ends_sentence) И текст не упёрся в лимит токенов.
        # РАНЬШЕ проверка была `res[-1] not in ".!?…»"`: закрывающая кавычка обрывка
        # «…в свободной колонии «Осколок Рассвета»» считалась концом мысли, и оборванное
        # на середине фразы вступление (409 симв. вместо сцены) уходило в чат как есть —
        # игрок не видел ни персонажа, ни сцены и считал, что вступления не было (п.3).
        if _ends_sentence(res) and str(fin.get("finish_reason") or "") != "length":
            return res
        log.warning("generate_opening (попытка %d/3): вступление оборвано на полуслове "
                    "(finish=%s, %d симв.) — повторяю генерацию", attempt + 1,
                    fin.get("finish_reason") or "?", len(res))
        if len(res) > len(best):
            best = res
    # Ни одна попытка не дала законченной сцены: берём длиннейшую, но режем до последнего
    # ПОЛНОГО предложения (короткая целая сцена лучше оборванной), а нечего резать —
    # завязка сюжета. Молча отдавать обрывок нельзя.
    if best:
        cut = _cut_sentence(best)
        if len(cut) >= 120:
            log.warning("generate_opening: законченного вступления не дождались — обрезаю "
                        "обрывок до последнего полного предложения (%d → %d симв.)",
                        len(best), len(cut))
            return cut
    log.warning("generate_opening: все попытки пусты/оборваны — использую завязку сюжета")
    return opening_fallback


# Знак конца мысли и «хвосты», которые могут стоять ПОСЛЕ него (кавычки, скобки, пробелы).
_SENT_END = ".!?…"
# многоточие НЕ хвост: это знак конца мысли (…), его не срезаем
_TRAILERS = " »”’)]}"


def _ends_sentence(text: str) -> bool:
    """Закончена ли мысль: срезав хвостовые кавычки/скобки/пробелы, видим . ! ? …
    Для русского «…Осколок Рассвета» (закрывающая кавычка БЕЗ точки) — False: кавычка
    концом предложения не является, это обрыв."""
    t = (text or "").strip()
    while t and t[-1] in _TRAILERS:
        t = t[:-1].rstrip()
    return bool(t) and t[-1] in _SENT_END


def _cut_sentence(text: str) -> str:
    """Обрезает текст до последнего полного предложения (по .!?…) — защита от обрыва на полуслове.
    Режем по самому позднему знаку конца предложения (даже если он раньше середины —
    лучше короткое полное сообщение, чем оборванное). `»` знаком конца мысли НЕ считается
    (в русском он обычно закрывает кавычки), поэтому из списка символов убран."""
    idx = max((text.rfind(c) for c in _SENT_END), default=-1)
    if idx > 0:
        return text[:idx + 1].strip()
    return text


def _world_theme(world: dict, setting: dict) -> dict:
    """Тема мира (для генераторов). Делегирует в narrator._world_theme — там полный
    резолв: get_theme(plots/) → снапшот при создании → синтез для старых миров.
    Локальный импорт: избегаем цикла импортов (narrator → character_generator)."""
    from .narrator import _world_theme as _wt
    return _wt(world, setting)
