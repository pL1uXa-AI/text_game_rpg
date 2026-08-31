# -*- coding: utf-8 -*-
"""risk.py — справка «что у персонажа есть для этой идеи» (сессия 34, C7).

Чистый форматировщик состояния (закон 2): ничего не решает, не проверяет «разрешено ли»,
не трогает LLM. Игрок видит СВОИ ресурсы против выбранной затеи — и меньше раздражается
на отказы движка («🎒 Перегруз», «нет станции», «не хватает золота»), потому что причины
отказа становятся прозрачными до попытки.

/Zakon 3: вывод — это перечень фактов («есть/нет»), а не вердикт «нельзя». Решает мастер.

Формат: /risk [идея]  → «взломать замок», «переговоры с гильдией», «крафт меча», «побег»,
«выживание в дикой местности», «лечение», «торговля», «скрытность», «бой»…
Ключевые слова РУССКИЕ и АНГЛИЙСКИЕ, жанрово-нейтральные (закон 1): движок не знает,
что такое «взлом» — он знает, что это «ловкость + инструменты + время».
"""
from __future__ import annotations

from typing import Any, Optional

# Жанр-нейтральные «оси» проверки: как назвать её игроку, какие статы/навыки/предметы
# и внешние условия к ней обычно относятся. Это СПРАВОЧНИК-ПОДСКАЗКА, а не формула успеха:
# связность с миром и итог решает рассказчик (закон 3).
_AXES: list[dict[str, Any]] = [
    {"id": "fight", "label": "бой / грубая сила",
     "words": ("бой", "атак", "драк", "убить", "враг", "меч", "сила", "штурм", "fight", "attack", "combat"),
     "stats": ("сила", "выносливость", "ловкость"),
     "skills": ("оруж", "бой", "влад", "фехтов", "стрельб", "единоборств"),
     "need": "оружие (слот «оружие») и запас HP",
     "watch": "hp"},
    {"id": "stealth", "label": "скрытность / кража",
     "words": ("скрыт", "крад", "проник", "незамеч", "тихо", "тень", "взлом", "отмычк", "сейф",
               "stealth", "lockpick", "sneak"),
     "stats": ("ловкость", "интеллект", "удача"),
     "skills": ("скрыт", "краж", "взлом", "проникн", "диверс", "акробат"),
     "need": "инструменты взлома/отмычки, темнота или ночь (время суток)",
     "watch": "mp"},
    {"id": "talk", "label": "переговоры / обман",
     "words": ("переговор", "уговор", "убедить", "врать", "обман", "договор", "торг",
               "дипломат", "talk", "persuade", "lie", "negotiat"),
     "stats": ("харизма", "мудрость", "интеллект"),
     "skills": ("убежд", "переговор", "торг", "психолог", "блеф", "оратор", "запугиван"),
     "need": "репутация/звание у нужной фракции, аргумент (доказательство, услуга, деньги)",
     "watch": "reputation"},
    {"id": "craft", "label": "крафт / починка",
     "words": ("кова", "крафт", "создат", "смастер", "почин", "рецепт", "ремонт", "свар",
               "craft", "build", "repair", "forge"),
     "stats": ("интеллект", "ловкость", "выносливость"),
     "skills": ("кузнеч", "алхим", "портняж", "механик", "инженер", "электрон", "кожа"),
     "need": "станция (кузница/верстак/лаборатория) и ПРОФЕССИЯ, материалы в инвентаре",
     "watch": "station"},
    {"id": "trade", "label": "торговля / деньги",
     "words": ("куп", "прода", "цена", "лавк", "магат", "долг", "заплатить", "бизнес",
               "buy", "sell", "trade", "shop"),
     "stats": ("харизма", "интеллект", "удача"),
     "skills": ("торг", "оценк", "барахол", "финанс", "эконом"),
     "need": "золото и свободное место в рюкзаке (вес)",
     "watch": "gold"},
    {"id": "knowledge", "label": "знание / расследование",
     "words": ("узнать", "исслед", "изучить", "прочт", "найти след", "расслед", "догад",
               "логик", "знан", "investigat", "research", "read", "recall"),
     "stats": ("интеллект", "мудрость", "удача"),
     "skills": ("истор", "религ", "природовед", "аркан", "крип", "медик", "правовед", "язык"),
     "need": "доступ к источнику (книга, лор, свидетель, база данных)",
     "watch": "mp"},
    {"id": "survive", "label": "выживание / путь",
     "words": ("выжить", "идти через", "пережить", "дикая", "холод", "жажда", "голод",
               "буря", "ночлег", "привал", "survive", "travel", "camp"),
     "stats": ("выносливость", "мудрость", "сила"),
     "skills": ("выжив", "охот", " ориент", "привал", "медит", "альпинизм"),
     "need": "еда/вода/тёплая вещь; запас потребностей (голод/жажда/усталость)",
     "watch": "needs"},
    {"id": "magic", "label": "магия / сверхспособность / техно",
     "words": ("маг", "заклин", "способность", "пси", "техно", "каст", "устройств",
               "магия", "spell", "magic", "ability", "device", "hacking"),
     "stats": ("интеллект", "мудрость", "харизма"),
     "skills": (),
     "need": "выученная способность (abilities) и хватает ли энергии MP",
     "watch": "mp"},
    {"id": "escape", "label": "побег / погоня",
     "words": ("сбеж", "убежать", "погон", "спастись", "уйти от", "выбрал", "погоня",
               "escape", "chase", "flee"),
     "stats": ("ловкость", "выносливость", "удача"),
     "skills": ("акробат", "бег", "верх", "вожден", "паркур", "преследован"),
     "need": "известный путь наружу (соседние локации/карта) и запас сил",
     "watch": "hp"},
]

_WATCH_LABEL = {
    "hp": "здоровье", "mp": "энергия (MP)", "gold": "золото", "needs": "потребности",
    "reputation": "репутация/фракции", "station": "станция для крафта",
}


def _pct(value: float, best: float) -> str:
    """Не «шанс», а грубая шкала «насколько прокачано» (0–100% от ориентира best=20)."""
    return f"{max(0, min(100, int(round(value / best * 100))))}%"


def match_axes(text: str) -> list[dict]:
    """Какие оси ресурсов относятся к идее (пустая идея = все оси обзором)."""
    low = (text or "").lower()
    if not low.strip():
        return []
    hits = [ax for ax in _AXES if any(w in low for w in ax["words"])]
    return hits


def _inventory_names(p: dict) -> list[str]:
    return [str(i.get("name", "")).lower() for i in (p.get("inventory") or [])
            if isinstance(i, dict)]


def _skill_names(p: dict) -> list[str]:
    return [str(k).lower() for k in (p.get("skills") or {})]


def _ability_names(p: dict) -> list[str]:
    return [str(k).lower() for k in (p.get("abilities") or {})]


def _env_note(setting: dict) -> str:
    t = str(setting.get("time") or "").strip()
    w = str(setting.get("weather") or "").strip()
    parts = [x for x in (t, w) if x]
    return " | ".join(parts) if parts else "без особых условий"


def describe_risk(setting: dict, idea: str = "") -> str:
    """Человекочитаемая справка: чем персонаж СЕЙЧАС может закрыть идею, а чего не хватает.

    Возвращает текст. Никаких решений: только перечень фактов из состояния (закон 2/3).
    """
    from .mechanics import (carry_capacity, effective_stats, inventory_weight,
                            location_stations, reputation_standing, can_craft)

    p = setting.get("player") or {}
    stats = effective_stats(p)
    lines: list[str] = []
    axes = match_axes(idea) or _AXES
    head = f"🧭 Что есть у тебя для «{idea.strip()}»:" if idea.strip() else "🧭 Твои ресурсы по всем осям:"
    lines.append(head)
    inv = _inventory_names(p)
    skills = _skill_names(p)
    abilities = _ability_names(p)
    rep = p.get("reputation") or {}
    ranks = p.get("faction_ranks") or {}
    stations = location_stations(setting)
    weight, cap = inventory_weight(p), carry_capacity(p)
    need_axes = axes[:4] if len(axes) > 4 else axes
    for ax in need_axes:
        parts: list[str] = []
        sv = [(s, int(stats.get(s, 0) or 0)) for s in ax["stats"]]
        strong = [f"{s} {v} ({_pct(v, 20)})" for s, v in sv if v >= 12]
        mid = [f"{s} {v}" for s, v in sv if 8 <= v < 12]
        if strong:
            parts.append("сильно: " + ", ".join(strong))
        if mid:
            parts.append("терпимо: " + ", ".join(mid))
        if not strong and not mid and sv:
            parts.append("слабо: " + ", ".join(f"{s} {v}" for s, v in sv))
        # навыки (по названию оси)
        rel_skills = [k for k in skills if any(w in k for w in ax["skills"])] if ax["skills"] else []
        rel_skills += [k for k in abilities if any(w in k for w in ("маг", "пси", "техно"))] \
            if ax["id"] == "magic" else []
        if rel_skills:
            got = []
            for k in rel_skills[:4]:
                sk = (p.get("skills") or {}).get(k) or (p.get("abilities") or {}).get(k) or {}
                rank = sk.get("rank") or sk.get("school") or ""
                got.append(f"{k}{f' [{rank}]' if rank else ''}")
            parts.append("навыки: " + ", ".join(got))
        else:
            parts.append("профильных навыков нет")
        # вещи
        want = {"fight": ("меч", "оруж", "нож", "копь", "пистолет", "щит", "брон"),
                "stealth": ("отмычк", "нож", "плащ", "перчатк", "канат", "факел"),
                "craft": (), "trade": (), "talk": ("письмо", "доказательств", "подарок", "взятк"),
                "survive": ("еда", "вода", "хлеб", "веревк", "спальник", "огнив", "тёпл"),
                "knowledge": ("книг", "гримуар", "записк", "карта", "свидетел", "терминал"),
                "magic": ("кристалл", "скрижал", "ампул", "батаре", "зелье"),
                "escape": ("лошад", "конь", "машин", "лодка", "канат", "плащ"),
                }
        names = want.get(ax["id"], ())
        have = [i for i in inv if names and any(w in i for w in names)]
        if names:
            parts.append("вещи: " + (", ".join(sorted(set(have))[:4]) if have else "нет подходящих"))
        if ax["id"] == "craft":
            parts.append(f"станция здесь: {', '.join(stations) if stations else '— (нужна мастерская)'}")
            crafts = setting.get("crafts") or {}
            ready = [rc.get("name", rid) for rid, rc in list(crafts.items())[:12]
                     if can_craft(setting, rc)[1] == "ok"]
            parts.append(f"рецептов: {len(crafts)}, готов сейчас: {', '.join(ready[:4]) or '—'}")
        if ax["id"] == "trade":
            parts.append(f"золото: {p.get('gold', 0)} 🪙 | продашь инвентарь ≈ "
                         f"{sum(int(i.get('value', 0) or 0) * int(i.get('qty', 1) or 1) for i in (p.get('inventory') or []) if isinstance(i, dict))} 🪙")
        if ax["id"] == "talk":
            if rep:
                top = sorted(rep.items(), key=lambda kv: -abs(int(kv[1] or 0)))[:3]
                parts.append("репутация: " + ", ".join(
                    f"{k} {v} ({reputation_standing(int(v or 0))})" for k, v in top))
            else:
                parts.append("репутации ни у одной фракции нет")
            if ranks:
                parts.append("звания: " + ", ".join(f"{k} — {v}" for k, v in list(ranks.items())[:3]))
        lines.append(f"• {ax['label']}: " + "; ".join(parts))
        lines.append(f"    нужно по сути: {ax['need']}")
    # общие ограничения хода
    tail = [f"🎒 {weight:.0f}/{cap} кг" + (" — ПЕРЕГРУЗ (часть покупок/крафта откажет)" if weight > cap else ""),
            f"❤ HP {p.get('hp', 0)}/{p.get('max_hp', p.get('hp', 0))}",
            f"🔷 MP {p.get('mp', 0)}/{p.get('max_mp', p.get('mp', 0))}",
            f"🕐 {_env_note(setting)}"]
    needs = p.get("needs") or {}
    crit = [k for k, v in needs.items() if isinstance(v, dict)
            and float(v.get("value", 100)) <= float(v.get("max", 100)) * 0.2]
    if crit:
        tail.append("⚠ критические потребности: " + ", ".join(crit))
    lines.append("")
    lines.append("Итого по карману: " + " | ".join(tail))
    if len(axes) > len(need_axes):
        lines.append(f"(показаны 4 из {len(axes)} подходящих осей — уточни идею формулировкой)")
    lines.append("Это перечень твоих средств, а не вердикт: решает Рассказчик.")
    return "\n".join(lines)


def risk_axes_ids() -> list[str]:
    """Для тестов/справки: какие оси вообще знает форматировщик."""
    return [a["id"] for a in _AXES]


__all__ = ["describe_risk", "match_axes", "risk_axes_ids"]
