# -*- coding: utf-8 -*-
"""Pydantic-схемы директив механики (<<ENGINE>>{...} / tool_calls game_engine) + нормализация.

Директивы приходят от LLM в виде свободного JSON — модель часто присылает
строку вместо объекта (roll: "d20"), число вместо dict (player: 7), числовой id
(quest: {id: 7}) и т.п. Модели ниже описывают каноническую форму каждой директивы,
а `normalize()` приводит произвольный JSON к виду, который ждёт `apply_directives`
(narrator.py), и отбрасывает безнадёжное.

ВАЖНО: семантика normalize() обязана совпадать с прежним ручным
`narrator.normalize_directives` — за этим следит tests/test_directives_schemas.py
(золотой корпус: 44 мусорных + 30 валидных директив, побуквенное сравнение вывода).
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ══════════════════════════════════════════════════════════════════════
# Схемы директив (канонические формы)
# ══════════════════════════════════════════════════════════════════════


class PlayerDirective(BaseModel):
    """`player`: дельты состояния игрока {hp, mp, gold, xp, stats, actions}."""
    model_config = ConfigDict(extra="allow")  # неизвестные поля не выкидываем (старое поведение)

    hp: Any = None
    mp: Any = None
    gold: Any = None
    xp: Any = None
    stats: dict[str, Any] = Field(default_factory=dict)
    actions: dict[str, Any] = Field(default_factory=dict)

    @field_validator("stats", "actions", mode="before")
    @classmethod
    def _force_dict(cls, v):
        # player: {stats: "грязь"} → stats: {} (применение не падает, статы не трогаются)
        return v if isinstance(v, dict) else {}


class RollDirective(BaseModel):
    """`roll`: бросок куба {expr, mod, dc, label}."""
    model_config = ConfigDict(extra="allow")

    expr: Any = None
    mod: Any = None
    dc: Any = None
    label: Any = None


class EffectAddDirective(BaseModel):
    """`effect_add`: статус-эффект {name, turns|duration, damage, heal, kind, stacks, mods, desc, tag}."""
    model_config = ConfigDict(extra="allow")

    name: Any = None
    turns: Any = None
    duration: Any = None  # алиас turns
    damage: Any = None
    heal: Any = None
    kind: Any = None
    stacks: Any = None
    mods: Any = None
    desc: Any = None
    tag: Any = None


class DictWithIdDirective(BaseModel):
    """quest / npc_set / location_add / location_update / enemy_add / enemy_apply / flag —
    dict-директивы с (возможно) числовым id. Числовой id LLM приводится к строке."""
    model_config = ConfigDict(extra="allow")

    id: Any = None

    @field_validator("id")
    @classmethod
    def _id_to_str(cls, v):
        # id: 7 → "7"; id: "q1" → "q1" (прежний код: non-str → str(...).strip())
        return str(v).strip() if not isinstance(v, str) else v


class SkillAddDirective(BaseModel):
    """`skill_add`: {name, rank, kind, desc, mp_cost}."""
    model_config = ConfigDict(extra="allow")

    name: Any = None
    rank: Any = None
    kind: Any = None
    desc: Any = None
    mp_cost: Any = None


class SkillRankDirective(BaseModel):
    """`skill_rank`: {name, rank}."""
    model_config = ConfigDict(extra="allow")

    name: Any = None
    rank: Any = None


class SkillDirective(BaseModel):
    """`skill` (старый формат): {name, value} — конвертируется в ранг F."""
    model_config = ConfigDict(extra="allow")

    name: Any = None
    value: Any = None


class ClassDirective(BaseModel):
    """`class`: "Воин" или {name, skill}."""
    model_config = ConfigDict(extra="allow")

    name: Any = None
    skill: Any = None


class RankDirective(BaseModel):
    """`class_rank` / `secondary_rank`: "D" или {rank: "D"}."""
    model_config = ConfigDict(extra="allow")

    rank: Any = None


class RaceChangeDirective(BaseModel):
    """`race_change`: "эльф" или {name, bonus, passive, desc}."""
    model_config = ConfigDict(extra="allow")

    name: Any = None
    bonus: Any = None
    passive: Any = None
    desc: Any = None


class ProfessionDirective(BaseModel):
    """`profession`: "Кузнец" или {name, buff, desc}."""
    model_config = ConfigDict(extra="allow")

    name: Any = None
    buff: Any = None
    desc: Any = None


class ReputationDirective(BaseModel):
    """`reputation`: {фракция: дельта} — произвольные ключи."""
    model_config = ConfigDict(extra="allow")


class ItemSpec(BaseModel):
    """Предмет в add_item/remove_item: {name, qty, desc}."""
    model_config = ConfigDict(extra="ignore")

    name: str = "Предмет"
    qty: Any = 1
    desc: Any = ""


class TimerDirective(BaseModel):
    """`timer_add`: {name, turns, desc} — дедлайн мира (тикает в начале хода)."""
    model_config = ConfigDict(extra="allow")

    name: Any = None
    turns: Any = None
    desc: Any = None


class EquipDirective(BaseModel):
    """`equip` / `unequip`: название предмета (строка) или {item: имя}."""
    model_config = ConfigDict(extra="allow")

    item: Any = None


class NeedsDirective(BaseModel):
    """`needs`: {голод: {value, max, decay}} / {рассудок: {value, max, decay}} — дельты/перезапись."""
    model_config = ConfigDict(extra="allow")


class BoardDirective(BaseModel):
    """`board_add`: {title, text} — объявление на доске мира."""
    model_config = ConfigDict(extra="allow")

    title: Any = None
    text: Any = None


class FactionRankDirective(BaseModel):
    """`faction_rank`: {faction, rank} — звание/должность игрока во фракции."""
    model_config = ConfigDict(extra="allow")

    faction: Any = None
    rank: Any = None


class CalendarDirective(BaseModel):
    """`date`: {day, month, season} — календарь мира."""
    model_config = ConfigDict(extra="allow")

    day: Any = None
    month: Any = None
    season: Any = None


class VisionDirective(BaseModel):
    """`vision_add`: {text, hint} — видение/сон в очереди; `trigger_vision`: {} — разыграть."""
    model_config = ConfigDict(extra="allow")

    text: Any = None
    hint: Any = None


# Паспорт ключей → модели (для dict-формы: валидация = verbatim-копия, т.к. extra=allow
# и Any-поля не меняют значения; идемпотентно и безопасно).
DICT_SPECS: dict[str, type[BaseModel]] = {
    "player": PlayerDirective,
    "roll": RollDirective,
    "effect_add": EffectAddDirective,
    "quest": DictWithIdDirective,
    "npc_set": DictWithIdDirective,
    "location_add": DictWithIdDirective,
    "location_update": DictWithIdDirective,
    "enemy_add": DictWithIdDirective,
    "enemy_apply": DictWithIdDirective,
    "flag": DictWithIdDirective,
    "skill_add": SkillAddDirective,
    "skill_rank": SkillRankDirective,
    "skill": SkillDirective,
    "class": ClassDirective,
    "class_rank": RankDirective,
    "secondary_rank": RankDirective,
    "race_change": RaceChangeDirective,
    "profession": ProfessionDirective,
    "reputation": ReputationDirective,
    "timer_add": TimerDirective,
    "equip": EquipDirective,
    "unequip": EquipDirective,
    "needs": NeedsDirective,
    "board_add": BoardDirective,
    "faction_rank": FactionRankDirective,
    "date": CalendarDirective,
    "vision_add": VisionDirective,
    "trigger_vision": VisionDirective,
}


def _model_dump(model: BaseModel) -> dict:
    """Канонический вывод: только реально заданные поля (+ extra как есть)."""
    return model.model_dump(exclude_unset=True)


# ══════════════════════════════════════════════════════════════════════
# Нормализация (канонизация произвольного JSON директив LLM)
# ══════════════════════════════════════════════════════════════════════

_DICT_WITH_ID_KEYS = ("quest", "npc_set", "location_add", "location_update",
                      "enemy_add", "enemy_apply", "flag",
                      "companion_add", "companion_update", "companion_apply")
_ITEM_KEYS = ("add_item", "remove_item")


def _norm_item(item: Any) -> dict:
    """Предмет из строки ("меч") или кривого dict → {name, qty, desc}."""
    if isinstance(item, str):
        return {"name": item, "qty": 1}
    if isinstance(item, dict):
        try:
            spec = ItemSpec.model_validate(item)
            out = _model_dump(spec)  # exclude_unset: только заданные поля
            out.setdefault("qty", 1)
            out.setdefault("name", "Предмет")
            out.setdefault("desc", "")
            return out
        except Exception:
            return dict(item)
    # прочее (число/None/...): раньше тут был краш (item.get AttributeError) — теперь терпим
    return {"name": "Предмет", "qty": 1}


def _norm_player(v: Any) -> dict:
    """player: dict → {hp/mp/gold/xp/stats/actions}; всё прочее → {}."""
    if isinstance(v, dict):
        try:
            return _model_dump(PlayerDirective.model_validate(v))
        except Exception:
            return dict(v)
    return {}


def _norm_roll(v: Any) -> dict | None:
    """roll: "d20" → {expr}; {expr, mod, dc} → как есть; прочее → отбросить."""
    if isinstance(v, dict):
        try:
            return _model_dump(RollDirective.model_validate(v))
        except Exception:
            return dict(v)
    if isinstance(v, str) and v.strip():
        return {"expr": str(v).strip()[:32]}
    return None


def _norm_effect_add(v: Any) -> dict | None:
    """effect_add: "отравлен" → {name}; dict → как есть; прочее → отбросить."""
    if isinstance(v, dict):
        try:
            return _model_dump(EffectAddDirective.model_validate(v))
        except Exception:
            return dict(v)
    if isinstance(v, str) and v.strip():
        return {"name": str(v).strip()[:80]}
    return None


def _norm_items(v: Any) -> list | None:
    """add_item/remove_item: список/dict/строка → список предметов; прочее → отбросить."""
    if isinstance(v, list):
        return [it if isinstance(it, dict) else _norm_item(it) for it in v]
    if isinstance(v, dict):
        return [dict(v)]
    if isinstance(v, str) and v.strip():
        return [_norm_item(v)]
    return None


def _norm_passthrough(key: str, v: Any) -> Any:
    """Остальные ключи — «как есть»; dict-формы дополнительно прогоняем через Pydantic
    (ничего не меняет: extra=allow + Any, но даёт валидацию и самодокументацию)."""
    if isinstance(v, dict):
        spec = DICT_SPECS.get(key)
        if spec is not None:
            try:
                return _model_dump(spec.model_validate(v))
            except Exception:
                return dict(v)
    return v


def normalize(d: Any) -> dict:
    """Защита от структурного «мусора» в JSON директив LLM (drop-in для
    narrator.normalize_directives): кривые значения выправляются, безнадёжные
    отбрасываются — ход никогда не падает на 'str' object has no attribute 'get'."""
    if not isinstance(d, dict):
        return {}
    out: dict = {}
    for k, v in d.items():
        if k == "player":
            out[k] = _norm_player(v)
        elif k == "roll":
            r = _norm_roll(v)
            if r is not None:
                out[k] = r
        elif k == "effect_add":
            e = _norm_effect_add(v)
            if e is not None:
                out[k] = e
        elif k in _DICT_WITH_ID_KEYS:
            if isinstance(v, dict):
                try:
                    out[k] = _model_dump(DictWithIdDirective.model_validate(v))
                except Exception:
                    out[k] = dict(v)
        elif k == "reputation":
            out[k] = v if isinstance(v, dict) else {}
        elif k in _ITEM_KEYS:
            items = _norm_items(v)
            if items is not None:
                out[k] = items
        else:
            out[k] = _norm_passthrough(k, v)
    return out