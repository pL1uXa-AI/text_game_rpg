# -*- coding: utf-8 -*-
"""Золотой тест Pydantic-схем директив (backend/directives.py).

Гарантия: новый `directives.normalize()` выдаёт ПОБУКВЕННО тот же результат,
что прежний ручной `narrator.normalize_directives` (эталон воспроизведён ниже
как _legacy_normalize) на корпусе из 70+ директив (мусор + валидные + края).
Плюс: схемы принимают документированные формы, а безнадёжный «мусор» больше
не роняет normalize (улучшение против прежнего кода — 7.get() крашился).
"""
from __future__ import annotations

from typing import Any

import pytest

from backend import directives
from backend import narrator


# ── Эталон: прежняя ручная нормализация (повтор из истории narrator.py) ──

def _legacy_norm_item(item: Any) -> dict:
    if isinstance(item, str):
        return {"name": item, "qty": 1}
    return {"name": item.get("name", "Предмет"), "qty": int(item.get("qty", 1)),
            "desc": item.get("desc", "")}


def _legacy_normalize(d):
    if not isinstance(d, dict):
        return {}
    out: dict = {}
    for k, v in d.items():
        if k == "player":
            pl = dict(v) if isinstance(v, dict) else {}
            for sub in ("stats", "actions"):
                if sub in pl and not isinstance(pl[sub], dict):
                    pl[sub] = {}
            out[k] = pl
        elif k == "roll":
            if isinstance(v, dict):
                out[k] = v
            elif isinstance(v, str) and v.strip():
                out[k] = {"expr": str(v).strip()[:32]}
        elif k == "effect_add":
            if isinstance(v, dict):
                out[k] = v
            elif isinstance(v, str) and v.strip():
                out[k] = {"name": str(v).strip()[:80]}
        elif k in ("quest", "npc_set", "location_add", "location_update",
                   "enemy_add", "enemy_apply", "flag"):
            if isinstance(v, dict):
                w = dict(v)
                if "id" in w and not isinstance(w["id"], str):
                    w["id"] = str(w["id"]).strip()
                out[k] = w
        elif k == "reputation":
            out[k] = v if isinstance(v, dict) else {}
        elif k in ("add_item", "remove_item"):
            if isinstance(v, list):
                out[k] = [it if isinstance(it, dict) else _legacy_norm_item(it) for it in v]
            elif isinstance(v, dict):
                out[k] = [dict(v)]
            elif isinstance(v, str) and v.strip():
                out[k] = [_legacy_norm_item(v)]
        else:
            out[k] = v
    return out


# ── Корпус ──

CORPUS = [
    # — регрессионный набор scripts/test_directives.py —
    {"player": "hp -5"},
    {"player": 7},
    {"player": None},
    {"roll": "d20"},
    {"roll": 42},
    {"roll": {"expr": "d20", "mod": "2", "dc": "15"}},
    {"enemy_apply": "волк"},
    {"enemy_apply": {"id": 3, "hp": 10}},
    {"enemy_add": "тролль"},
    {"enemy_add": {"id": 7, "name": "Тролль", "hp": 30, "dmg": 5}},
    {"enemy_remove": "x"},
    {"quest": "найди артефакт"},
    {"quest": {"id": "q1", "title": "Квест", "status": "active"}},
    {"quest_done": "q1"},
    {"npc_set": "трактирщик"},
    {"npc_set": {"id": "n1", "name": "Бармен"}},
    {"npc_kill": "n1"},
    {"location_add": "таверна"},
    {"location_add": {"id": "loc1", "name": "Таверна"}},
    {"location_update": "поле"},
    {"move": "loc1"},
    {"flag": "дверь открыта"},
    {"flag": {"name": "дверь", "value": True}},
    {"time": "ночь"},
    {"weather": "гроза"},
    {"game_over": True},
    {"effect_add": "отравлен"},
    {"effect_add": {"name": "Яд", "turns": 3, "damage": 2}},
    {"effect_remove": "Яд"},
    {"skill_add": {"name": "Огненный шар", "rank": "D", "mp_cost": 5}},
    {"skill_rank": {"name": "Огненный шар", "rank": "C"}},
    {"skill_remove": "Огненный шар"},
    {"class": 123},
    {"class": {"name": "Маг", "skill": {"name": "Волшба", "rank": "F"}}},
    {"profession": "Кузнец"},
    {"title": "Ветеран"},
    {"reputation": {"гильдия воров": 2}},
    {"reputation": "строка"},
    {"player": {"stats": {"сила": "2"}}},
    {"player": {"actions": {"кузнечное дело": 1}}},
    {"add_item": "меч"},
    {"add_item": [{"name": "Меч", "qty": 1}]},
    {"remove_item": "меч"},
    {"keyword_unknown": {"a": 1}},
    None,
    "просто строка",
    [],
    # — края и валидные формы —
    {},
    {"player": {}},
    {"player": {"stats": None, "gold": -3}},
    {"player": {"hp": -5, "gold": 10, "xp": "50", "mp": 0}},
    {"roll": {}},
    {"roll": "  "},
    {"roll": "d100"},
    {"roll": {"expr": "2d6+1", "mod": 3, "dc": 12, "label": "Проверка силы"}},
    {"effect_add": {}},
    {"effect_add": ""},
    {"effect_add": {"name": "chill_resonance", "mods": {"ловкость": 2}, "tag": "race",
                    "turns": "permanent", "stacks": "3"}},
    {"effect_remove": {"name": "Яд"}},
    {"quest": {"id": None, "title": "X"}},
    {"quest": {"id": 9, "title": "q", "desc": "описание", "status": "done"}},
    {"npc_set": {"id": 12.5, "name": "Y", "mood": "злой", "alive": True}},
    {"location_add": {"id": "l1", "connections": ["l2", 3], "desc": "лес"}},
    {"location_update": {"id": 4, "desc": "туманно"}},
    {"enemy_apply": {"id": " 7 ", "hp": -5}},
    {"enemy_add": {"id": 0, "name": "", "hp": 1, "dmg": 0}},
    {"flag": {"name": "x", "value": "true"}},
    {"skill_add": "меч"},
    {"skill_rank": "D"},
    {"skill": {"name": "Старый навык", "value": 3}},
    {"class_rank": {"rank": "C"}},
    {"class_rank": "B"},
    {"secondary_class": "Вор", "secondary_rank": {"rank": "D"}},
    {"class_evolve": "Маг высший"},
    {"race_change": "эльф"},
    {"race_change": {"name": "Драконид", "bonus": {"сила": 2}, "passive": "Чешуя"}},
    {"profession": {"name": "Кузнец", "buff": {"сила": 2}}},
    {"add_item": [{"name": "Зелье", "qty": 3, "desc": "лечит"}, {"name": "меч"}]},
    {"add_item": [{"name": "Меч", "qty": 1}]},
    {"remove_item": []},
    {"remove_item": {"name": "Меч"}},
    {"move": 2},
    {"time": None},
    {"weather": ""},
]


def test_normalize_equals_legacy_on_corpus():
    for d in CORPUS:
        got = directives.normalize(d)
        exp = _legacy_normalize(d)
        assert got == exp, f"расхождение на {d!r}:\n  new={got!r}\n  old={exp!r}"


def test_normalize_never_crashes():
    # плюс «экстремальный» мусор, на котором прежний код ПАДАЛ (item.get AttributeError)
    for d in CORPUS + [{"add_item": [7]}, {"add_item": [None]}, {"add_item": 7},
                       {"add_item": [True]},
                       {"player": [1, 2]}, {"roll": [1]}, {"effect_add": [1]}]:
        assert isinstance(directives.normalize(d), dict)
        assert isinstance(narrator.normalize_directives(d), dict)


def test_narrator_delegates_to_directives():
    # публичная точка narrator.normalize_directives теперь работает через Pydantic-схемы
    for d in CORPUS[:30]:
        assert narrator.normalize_directives(d) == directives.normalize(d)


def test_apply_directives_tolerates_corpus():
    """После нормализации apply_directives не роняет ход (регрессия 'str has no get')."""
    from backend.narrator import default_setting, THEMES, apply_directives
    for d in CORPUS:
        setting = default_setting(THEMES[0], "normal")
        nd = directives.normalize(d)
        msgs = apply_directives(setting, nd)
        assert isinstance(msgs, list)
        assert setting["player"]["stats"], "статы сломались"


def test_id_coercion():
    nd = directives.normalize({"quest": {"id": 7, "title": "q"}})
    assert nd["quest"]["id"] == "7", "числовой id должен стать строкой"
    nd = directives.normalize({"enemy_apply": {"id": 3.0, "hp": 1}})
    assert nd["enemy_apply"]["id"] == "3.0"


def test_player_stats_crushed_to_empty():
    nd = directives.normalize({"player": {"stats": "crash", "gold": -5}})
    assert nd["player"] == {"gold": -5, "stats": {}}


def test_roll_string_and_int():
    assert directives.normalize({"roll": "d20"}) == {"roll": {"expr": "d20"}}
    assert directives.normalize({"roll": 42}) == {}
    assert directives.normalize({"roll": {"expr": "2d6+1"}}) == {"roll": {"expr": "2d6+1"}}


def test_item_normalization():
    assert directives.normalize({"add_item": "меч"}) == {"add_item": [{"name": "меч", "qty": 1}]}
    assert directives.normalize({"remove_item": {"name": "Меч"}}) == {"remove_item": [{"name": "Меч"}]}


def test_unknown_keys_passthrough():
    d = {"custom_flag_х": {"a": 1}, "anything": "value"}
    assert directives.normalize(d) == d


def test_models_accept_documented_shapes():
    """Схемы описывают канонические формы — прямые проверки model_validate."""
    from backend.directives import (
        EffectAddDirective, PlayerDirective, RollDirective, SkillAddDirective,
    )
    pl = PlayerDirective.model_validate({"hp": -5, "stats": {"сила": 2}})
    dump = pl.model_dump(exclude_unset=True)
    assert dump["hp"] == -5 and dump["stats"] == {"сила": 2}
    assert "actions" not in dump, "незаданные поля не должны выводиться"
    r = RollDirective.model_validate({"expr": "d20", "mod": 2, "dc": 15, "label": "x"})
    assert r.model_dump(exclude_unset=True)["mod"] == 2
    e = EffectAddDirective.model_validate(
        {"name": "Яд", "turns": 3, "damage": 2, "stacks": 1, "desc": "яд"})
    assert e.model_dump(exclude_unset=True)["damage"] == 2
    s = SkillAddDirective.model_validate({"name": "Огненный шар", "rank": "D", "mp_cost": 5})
    assert s.model_dump(exclude_unset=True) == {"name": "Огненный шар", "rank": "D", "mp_cost": 5}


def test_schemas_are_stable_docs(capsys):
    """Схемы — это живая документация: в backend/directives.py есть модели по всем директивам."""
    from backend import directives as dm
    assert hasattr(dm, "PlayerDirective")
    assert hasattr(dm, "RollDirective")
    assert hasattr(dm, "EffectAddDirective")
    assert hasattr(dm, "DictWithIdDirective")
    assert hasattr(dm, "SkillAddDirective")
    assert hasattr(dm, "ClassDirective")
    assert hasattr(dm, "RaceChangeDirective")
    assert hasattr(dm, "ProfessionDirective")
    assert hasattr(dm, "ReputationDirective")
    assert hasattr(dm, "ItemSpec")
    assert dm.normalize({"enemy_apply": {"id": 7}})["enemy_apply"]["id"] == "7"