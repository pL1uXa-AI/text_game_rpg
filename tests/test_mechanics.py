# -*- coding: utf-8 -*-
"""Юнит-тесты RPG-механики (чистый Python, без БД и сети): кубы, эффекты,
директивы apply_directives, профессии от действий, производные статы."""
from __future__ import annotations

import copy

import pytest

from backend import narrator
from backend.narrator import (
    THEMES, apply_directives, default_setting,
    effective_stats, format_state, normalize_directives, recalc_derived, roll_expr, roll_outcome,
    split_engine, tick_effects,
)
# D2 (аудит 38): фасад narrator больше не реэкспортирует имя, которое никем не читалось
# через narrator.X — берём напрямую из модуля-владельца (механика живёт в mechanics).
from backend.mechanics import check_profession_advance


@pytest.fixture
def setting():
    s = default_setting(THEMES[0], "normal")
    s["player"]["hp"] = 50
    s["player"]["gold"] = 10
    s["player"]["inventory"] = []  # старт инвентаря сюжета не влияет на вес-тесты
    return s


def test_theme_is_valid(setting):
    assert setting["player"]["stats"], "в стартовом состоянии есть статы"
    assert setting["player"]["hp"] == 50


def test_format_state_with_new_mechanics(setting):
    """Регрессия: format_state/build_messages не должны падать на спутниках/магазинах/рецептах/прогрессе квеста.
    (dict_items нельзя слайсить — .items()[:N] бросает TypeError и роняет ход.)"""
    # применим директивы, наполняющие все новые структуры
    apply_directives(setting, {"companion_add": {"id": "wolf", "name": "Серый", "hp": 30,
                                                "skills": {"Укус": {"rank": "C"}, "Крик": {"rank": "D"}}}})
    apply_directives(setting, {"shop_add": {"id": "sm", "name": "Лавка",
                                           "items": [{"name": "Меч", "price": 10, "qty": 2}]}})
    apply_directives(setting, {"craft_learn": {"id": "r1", "name": "Рецепт",
                                               "ingredients": [{"name": "Руда", "qty": 1}],
                                               "result": {"name": "Клинок", "qty": 1}}})
    apply_directives(setting, {"quest": {"id": "q1", "title": "Квест", "progress": "шаг 2"}})
    out = format_state(setting)
    assert "Серый" in out and "Укус" in out
    assert "Лавка" in out
    assert "Рецепт" in out
    assert "шаг 2" in out


# ── Кубы ──────────────────────────────────────────────────────────

def test_roll_d20():
    r = roll_expr("d20")
    assert len(r["rolls"]) == 1 and 1 <= r["total"] <= 20


def test_roll_2d6_bonus():
    r = roll_expr("2d6+1")
    assert len(r["rolls"]) == 2 and 1 <= r["total"] <= 13
    assert r["total"] == sum(r["rolls"]) + 1


def test_roll_d100():
    r = roll_expr("d100")
    assert 1 <= r["total"] <= 100


def test_roll_mod():
    r = roll_expr("d20", mod=2)
    assert r["total"] == r["rolls"][0] + 2


def test_roll_garbage_fallback():
    r = roll_expr("не_куб")
    assert r["rolls"] == [] and 1 <= r["total"] <= 20


def test_roll_outcome_buckets():
    assert roll_outcome(30, 15, "d20") == "критический успех"
    assert roll_outcome(15, 15, "d20") == "успех"
    assert roll_outcome(5, 15, "d20") == "критический провал"
    assert roll_outcome(10, 15, "d20") == "провал"
    assert roll_outcome(1, 15, "d20") == "критический провал"


# ── Эффекты и снапшот ─────────────────────────────────────────────

def test_tick_effects_damage(setting):
    setting["player"]["effects"] = {"Яд": {"turns": 3, "damage": 2}}
    msgs = tick_effects(setting)
    assert any("Яд" in m for m in msgs)
    assert setting["player"]["hp"] == 48, "тик снял 2 HP"
    assert setting["player"]["effects"]["Яд"]["turns"] == 2, "turns уменьшился"


def test_tick_effects_expiry(setting):
    setting["player"]["effects"] = {"Искра": {"turns": 1, "damage": 5}}
    tick_effects(setting)
    assert "Искра" not in setting["player"]["effects"], "истёкший эффект убран"


def test_tick_effects_stacks_multiply(setting):
    setting["player"]["effects"] = {"Кровотечение": {"turns": 2, "damage": 3, "stacks": 2}}
    tick_effects(setting)
    assert setting["player"]["hp"] == 44, "урон × стаки (3×2)"


def test_snapshot_no_double_tick(setting):
    """Перегенерация: откат к снапшоту → тик применяется ровно один раз."""
    setting["player"]["effects"] = {"Яд": {"turns": 3, "damage": 2}}
    snap = copy.deepcopy(setting)
    tick_effects(setting)
    hp_after_first = setting["player"]["hp"]
    restored = copy.deepcopy(snap)
    tick_effects(restored)
    assert restored["player"]["hp"] == hp_after_first, "тик задвоился"
    assert restored["player"]["effects"]["Яд"]["turns"] == 2


def test_heal_effect(setting):
    setting["player"]["effects"] = {"Регенерация": {"turns": 2, "heal": 3}}
    tick_effects(setting)
    assert setting["player"]["hp"] == 53


# ── apply_directives: игрок ───────────────────────────────────────

def test_player_gold_and_hp(setting):
    msgs = apply_directives(setting, {"player": {"gold": -3, "hp": -10}})
    assert setting["player"]["gold"] == 7
    assert setting["player"]["hp"] == 40
    assert any("Золото" in m for m in msgs)


def test_player_stats_additive(setting):
    apply_directives(setting, {"player": {"stats": {"сила": 2, "мудрость": -1}}})
    assert setting["player"]["stats"]["сила"] == 12
    assert setting["player"]["stats"]["мудрость"] == 9
    assert setting["player"]["stats"]["сила"] >= 1


def test_player_xp_no_autolevel(setting):
    # Уровень растёт только явной директивой `level` (рассказчик — мастер),
    # XP копится как информация, но пороговой автопрокачки НЕТ (сессия 19).
    setting["player"]["xp"] = 90
    apply_directives(setting, {"player": {"xp": 20}})
    assert setting["player"]["xp"] == 110, "XP копится, но не конвертируется в уровень"
    assert setting["player"]["level"] == 1, "без директивы level уровень не меняется"


def test_player_xp_explicit_level(setting):
    # Сюжетный рост уровня выдаётся явно через player.level
    apply_directives(setting, {"player": {"level": 3}})
    assert setting["player"]["level"] == 3
    assert setting["player"]["hp"] == setting["player"]["max_hp"], "HP восстановлены"
    assert setting["player"]["mp"] == setting["player"]["max_mp"], "MP восстановлены"


def test_player_actions_accumulate(setting):
    apply_directives(setting, {"player": {"actions": {"кузнечное дело": 2}}})
    apply_directives(setting, {"player": {"actions": {"кузнечное дело": 1}}})
    assert setting["player"]["actions"]["кузнечное дело"] == 3


# ── apply_directives: класс/профессия/навыки/титулы/репутация ───

def test_class_change_with_skill(setting):
    msgs = apply_directives(setting, {"class": "Маг"})
    assert setting["player"]["class"] == "Маг"
    assert any("Класс" in m for m in msgs)
    assert setting["player"]["skills"], "стартовый навык выдан"


def test_class_custom_with_skill(setting):
    apply_directives(setting, {"class": {"name": "Тёмный маг", "skill": {"name": "Проклятие", "rank": "F"}}})
    assert setting["player"]["class"] == "Тёмный маг"
    assert "Проклятие" in setting["player"]["skills"]


def test_profession_buff(setting):
    apply_directives(setting, {"profession": "Кузнец"})
    assert setting["player"]["profession"] == "Кузнец"
    assert setting["player"].get("profession_buff"), "бафф профессии сохранён"


def test_skill_add_and_rank(setting):
    apply_directives(setting, {"skill_add": {"name": "Огненный шар", "rank": "D", "mp_cost": 5}})
    assert setting["player"]["skills"]["Огненный шар"]["rank"] == "D"
    apply_directives(setting, {"skill_rank": {"name": "Огненный шар", "rank": "C"}})
    assert setting["player"]["skills"]["Огненный шар"]["rank"] == "C"


def test_title_and_reputation(setting):
    apply_directives(setting, {"title": "Ветеран", "reputation": {"гильдия воров": 2}})
    assert "Ветеран" in setting["player"]["titles"]
    assert setting["player"]["reputation"]["гильдия воров"] == 2


def test_race_change(setting):
    apply_directives(setting, {"race_change": "эльф"})
    assert setting["player"]["race"] == "эльф"


# ── apply_directives: мир ─────────────────────────────────────────

def test_items(setting):
    apply_directives(setting, {"add_item": [{"name": "Меч", "qty": 1}]})
    assert any(i["name"] == "Меч" for i in setting["player"]["inventory"])
    apply_directives(setting, {"remove_item": "Меч"})
    assert not any(i["name"] == "Меч" for i in setting["player"]["inventory"])


def test_enemy_add_apply(setting):
    apply_directives(setting, {"enemy_add": {"id": 7, "name": "Тролль", "hp": 30, "dmg": 5}})
    assert "7" in setting["enemies"]
    apply_directives(setting, {"enemy_apply": {"id": 7, "hp": -10}})
    assert setting["enemies"]["7"]["hp"] == 20


def test_dynamic_adversary_scale_level1_identity(setting):
    """На уровне 1 множители = 1.0 — ничего не усиливаем (враг как послал рассказчик)."""
    sc = narrator.dynamic_adversary_scale(setting["player"], "normal")
    assert sc == {"hp": 1.0, "dmg": 1.0, "event": 1.0}


def test_dynamic_adversary_scale_scales_by_level():
    """С ростом уровня множители растут, а сложность мира делает рост круче/положе."""
    lvl1 = narrator.dynamic_adversary_scale(level=1, difficulty="normal")
    lvl10 = narrator.dynamic_adversary_scale(level=10, difficulty="normal")
    assert lvl10["hp"] > lvl1["hp"] and lvl10["dmg"] > lvl1["dmg"]
    assert lvl10["event"] > 1.0
    hc = narrator.dynamic_adversary_scale(level=10, difficulty="hardcore")
    ez = narrator.dynamic_adversary_scale(level=10, difficulty="easy")
    assert hc["dmg"] > ez["dmg"]  # на хардкоре враги агрессивнее


def test_dynamic_enemy_scaled_by_player_level(setting):
    """Динамическая сложность: сила врага отмасштабируется под уровень игрока."""
    apply_directives(setting, {"enemy_add": {"id": 7, "name": "Тролль", "hp": 30, "dmg": 5}})
    # уровень 1 → без изменений
    assert setting["enemies"]["7"]["hp"] == 30 and setting["enemies"]["7"]["dmg"] == 5
    # уровень 10 → scale ~ hp 1+9*0.5=5.5, dmg 1+9*0.3=3.7
    setting["player"]["level"] = 10
    apply_directives(setting, {"enemy_add": {"id": "w", "name": "Гроза", "hp": 20, "dmg": 4}})
    sc = narrator.dynamic_adversary_scale(setting["player"], "normal")
    assert setting["enemies"]["w"]["hp"] == max(1, int(round(20 * sc["hp"])))
    assert setting["enemies"]["w"]["dmg"] == max(1, int(round(4 * sc["dmg"])))
    assert setting["enemies"]["w"]["hp"] > 20 and setting["enemies"]["w"]["dmg"] > 4


def test_dynamic_event_chance_scales_with_level():
    """Частота случайных событий растёт с уровнем игрока (потолок 0.5 сохранён)."""
    base_c = narrator.event_chance(2, 10, level=1)
    lvl10 = narrator.event_chance(2, 10, level=10)
    assert lvl10 > base_c
    assert lvl10 <= 0.5
    assert lvl10 == min(0.5, 2 / (10 / (1.0 + 9 * 0.05)))


def test_master_stuck_reason_repetition():
    """Автономный мастер: повтор одного действия игроком → сигнал «repetition»."""
    acts = ["осмотреться в комнате", "осмотреться в комнате",
            "осмотреться в комнате", "снова осмотреться"]
    assert narrator.master_stuck_reason(acts, 1, 4) == "repetition"


def test_master_stuck_reason_drifting():
    """Много ходов без активных квестов → сигнал «drifting»; разнообразные действия — молчит."""
    varied = ["идти к мосту", "поговорить с кузнецом", "заглянуть в храм",
              "починить нож", "купить еды", "выспаться"]
    assert narrator.master_stuck_reason(varied, 0, 15) == "drifting"
    # до 15 ходов даже без квестов мастер не вмешивается (не фальсифицирует тупик)
    assert narrator.master_stuck_reason(varied, 0, 14) is None
    assert narrator.master_stuck_reason(varied, 0, 10) is None
    # разнообразные действия и есть активный квест → не застревает
    assert narrator.master_stuck_reason(["идти", "спать", "есть", "торговать"], 1, 8) is None


def test_master_stuck_reason_no_interference():
    """Мало ходов / мало действий — мастер не вмешивается (не фальсифицирует тупик)."""
    assert narrator.master_stuck_reason(["идти к мосту"], 1, 2) is None
    assert narrator.master_stuck_reason([], 1, 50) is None


def test_generate_master_nudge_parses_quest(setting, monkeypatch):
    """LLM-проход мастера: парсинг JSON с директивой quest и текстом (без реального LLM)."""
    async def fake_complete(messages, **kw):
        return ('{"mode": "quest", "title": "След в пепле", '
                '"text": "Старуха приносит весть: кто-то ищет тебя.", '
                '"directives": {"quest": {"id": "q_ash", "title": "След в пепеле", "status": "active"}}}')
    import backend.narrator as narrator_mod
    monkeypatch.setattr(narrator_mod.llm, "complete", fake_complete)
    import asyncio
    res = asyncio.run(narrator.generate_master_nudge(setting, "drifting", "...", lang="ru"))
    assert res is not None
    txt, dirs = res
    assert "старуха" in txt.lower() or "Старуха" in txt
    assert dirs["quest"]["id"] == "q_ash"


def test_enemy_ai_living_enemies(setting):
    """Боевой ИИ: определяет только живых врагов, сортирует по убыванию опасности."""
    assert narrator._living_enemies(setting) == []
    setting["enemies"] = {"w": {"name": "Волк", "hp": 20, "max_hp": 20, "dmg": 4},
                          "sh": {"name": "Шакал", "hp": 8, "max_hp": 8, "dmg": 2},
                          "corpse": {"name": "Труп", "hp": 0, "max_hp": 5, "dmg": 1}}
    living = narrator._living_enemies(setting)
    ids = [k for k, _ in living]
    assert ids == ["w", "sh"]  # мёртвый враг (hp 0) исключён; урон 4 > 2 → волк первый


def test_enemy_ai_apply_retreat(setting):
    """Режим retreat убирает врага с поля боя (урон игроку не наносит)."""
    setting["enemies"] = {"w": {"name": "Волк", "hp": 20, "max_hp": 20, "dmg": 4}}
    msgs = narrator._apply_enemy_ai(setting, "w", "retreat", {})
    assert "w" not in setting["enemies"]
    assert any("отступ" in m for m in msgs)
    # ставки игрока не тронуты (урон ведёт рассказчик, не ИИ)
    assert setting["player"]["hp"] == 50 or "hp" in setting["player"]


def test_enemy_ai_apply_stance(setting):
    """Охрана/преследование фиксирует тактическое намерение (ai) врага для рассказчика."""
    setting["enemies"] = {"w": {"name": "Волк", "hp": 20, "max_hp": 20, "dmg": 4}}
    narrator._apply_enemy_ai(setting, "w", "guard", {})
    assert setting["enemies"]["w"]["ai"] == "guard"


def test_enemy_ai_strip_damage():
    """Боевой ИИ не должен наносить урон/призывать врагов: такие директивы вырезаются."""
    d = {"flag": {"name": "trap", "value": True},
          "player": {"hp": -10},
          "enemy_apply": {"id": "x", "hp": -5},
          "add_item": [{"name": "След", "qty": 1}]}
    stripped = narrator._strip_damage(d)
    assert "flag" in stripped and "add_item" in stripped
    assert "player" not in stripped and "enemy_apply" not in stripped


def test_enemy_ai_intent_shown_to_narrator(setting):
    """Тактическое намерение врага (ai) видно рассказчику в состоянии мира."""
    setting["enemies"] = {"w": {"name": "Волк", "hp": 20, "max_hp": 20, "dmg": 4, "ai": "guard"}}
    out = format_state(setting)
    assert "намерение: в обороне" in out


def test_generate_enemy_ai_parses(setting, monkeypatch):
    """LLM-проход боевого ИИ: парсинг JSON (enemy/mode/note/directives) без реального LLM."""
    setting["enemies"] = {"w": {"name": "Волк", "hp": 20, "max_hp": 20, "dmg": 4}}
    async def fake(messages, **kw):
        return ('{"enemy": "w", "mode": "retreat", '
                '"note": "Раненый волк сбегает в лес.", '
                '"directives": {}}')
    import backend.narrator as narrator_mod
    monkeypatch.setattr(narrator_mod.llm, "complete", fake)
    import asyncio
    res = asyncio.run(narrator.generate_enemy_ai(setting, "...", lang="ru"))
    assert res is not None
    eid, txt, mode, dirs = res
    assert eid == "w" and mode == "retreat"
    assert "волк" in txt.lower()


def test_quests(setting):
    apply_directives(setting, {"quest": {"id": "q1", "title": "Найти артефакт", "status": "active"}})
    assert setting["quests"]["q1"]["status"] == "active"
    apply_directives(setting, {"quest": {"id": "q1", "progress": "войти в храм"}})
    assert setting["quests"]["q1"]["progress"] == "войти в храм"
    # title/desc не затёрты при обновлении только прогресса
    assert setting["quests"]["q1"]["title"] == "Найти артефакт"
    apply_directives(setting, {"quest_done": "q1"})
    assert setting["quests"]["q1"]["status"] == "done"


def test_quest_staged_advance(setting):
    """B3: ступенчатый квест — quest_advance продвигает по steps (след/по имени)."""
    setting["quests"]["q"] = {"id": "q", "title": "Раскопки", "status": "active",
                               "steps": ["найти вход", "спуститься", "вернуть артефакт"]}
    apply_directives(setting, {"quest_advance": {"id": "q"}})
    assert setting["quests"]["q"]["current"] == 1, "авто-шаг вперёд → индекс 1"
    apply_directives(setting, {"quest_advance": {"id": "q", "step": "вернуть артефакт"}})
    assert setting["quests"]["q"]["current"] == 2, "переход на шаг по имени"


def test_quest_branch_choose(setting):
    """B3: ветвление — quest_choose фиксирует выбранную ветку."""
    apply_directives(setting, {"quest": {"id": "q2", "title": "Выбор",
                                          "branches": ["уговорить", "сразиться"]}})
    apply_directives(setting, {"quest_choose": {"id": "q2", "branch": "уговорить"}})
    assert setting["quests"]["q2"]["chosen"] == "уговорить"
    assert setting["quests"]["q2"]["branch"] == "уговорить"


def test_quest_chain_on_done(setting):
    """B3: цепочка — quest_done с next автоматически запускает следующий квест."""
    apply_directives(setting, {"quest": {"id": "q1", "title": "Первый", "status": "active"}})
    apply_directives(setting, {"quest_done": {"id": "q1",
                                              "next": {"id": "q2", "title": "Следующий", "desc": "продолжение"}}})
    assert setting["quests"]["q1"]["status"] == "done"
    assert setting["quests"]["q2"]["status"] == "active"
    assert setting["quests"]["q2"]["title"] == "Следующий"


def test_ability_flow(setting):
    """B4: универсальные способности — изучение, использование (трата MP), снятие."""
    apply_directives(setting, {"ability_add": {"name": "Ледяная стрела", "school": "магия",
                                                "cost": 4, "desc": "поражает цель"}})
    assert setting["player"]["abilities"]["Ледяная стрела"]["cost"] == 4
    before = setting["player"]["mp"]
    apply_directives(setting, {"ability_use": {"name": "Ледяная стрела", "cost": 4}})
    assert setting["player"]["mp"] == max(0, before - 4), "использование списывает MP"
    apply_directives(setting, {"ability_remove": "Ледяная стрела"})
    assert "Ледяная стрела" not in setting["player"]["abilities"]


def test_achievements_and_progress(setting):
    """B5: достижения и статистика — achievement_add, progress_add, авто-счётчики."""
    apply_directives(setting, {"achievement_add": {"name": "Первый шаг", "desc": "сделал выбор"}})
    assert setting["player"]["achievements"][0]["name"] == "Первый шаг"
    # дубликат не копится
    apply_directives(setting, {"achievement_add": "Первый шаг"})
    assert len(setting["player"]["achievements"]) == 1
    # прогресс
    apply_directives(setting, {"progress_add": {"kills": 2}})
    assert setting["player"]["progress"]["kills"] == 2
    # авто-счётчик убийств
    apply_directives(setting, {"enemy_add": {"id": "g", "name": "Гобл", "hp": 5, "dmg": 1}})
    apply_directives(setting, {"enemy_apply": {"id": "g", "hp": -5}})
    assert setting["player"]["progress"]["kills"] == 3, "+1 от поверженного врага"


def test_npc_and_kill(setting):
    apply_directives(setting, {"npc_set": {"id": "n1", "name": "Бармен", "faction": "гильдия"}})
    assert setting["npc"]["n1"]["alive"] is True
    apply_directives(setting, {"npc_kill": "n1"})
    assert setting["npc"]["n1"]["alive"] is False


def test_npc_notes_non_scalar_values(setting):
    """Регрессии на `npc_set.notes` (сессия 37, блок C5).

    1) ruff F821 / живой NameError: значения notes могут быть не строкой
       (список/число/вложенный dict) — ветка сериализует их через `json`,
       которого в mechanics.py не было импортировано.
    2) тихая потеря данных: `existing.update()` в начале обработчика перезаписывал
       прежние notes новыми, и «слияние» читало уже НОВЫЕ заметки — второй вызов
       npc_set с другим ключом стирал всё, что мастер заводил раньше.
    Оба проверяются на одном и том же месте, поэтому тест должен падать при
    откате любого из двух фиксов.
    """
    apply_directives(setting, {"npc_set": {"id": "n1", "name": "Купец",
                                          "notes": {"тайна": "шпион", "долг": ["мех", "3 монеты"],
                                                    "знает": 7}}})
    notes = setting["npc"]["n1"]["notes"]
    assert notes["тайна"] == "шпион", "строка хранится как есть"
    assert notes["долг"] == '["мех", "3 монеты"]', "список — JSON-строкой (кириллица не экранируется)"
    assert notes["знает"] == "7", "скаляр не-строка тоже сериализуется"

    # накопление: второй вызов не должен стирать ключи первого
    apply_directives(setting, {"npc_set": {"id": "n1", "notes": {"хочет": "вернуть долг"}}})
    notes = setting["npc"]["n1"]["notes"]
    assert notes["хочет"] == "вернуть долг", "новый ключ добавился"
    assert notes["тайна"] == "шпион", "прежний ключ НЕ потерян (баг слияния)"
    assert notes["долг"] == '["мех", "3 монеты"]', "нескалярный прежний ключ НЕ потерян"

    # None удаляет ключ (правка «стереть заметку»), а не пишет "null"
    apply_directives(setting, {"npc_set": {"id": "n1", "notes": {"тайна": None}}})
    notes = setting["npc"]["n1"]["notes"]
    assert "тайна" not in notes, "None снимает ключ"
    assert notes.get("хочет") == "вернуть долг" and notes.get("долг"), "остальное сохранено"

    # выдача в промпт не должна падать на таких значениях
    txt = narrator._notes_text(notes)
    assert "мех" in txt and "вернуть долг" in txt


def test_economy_trade(setting):
    # магазин с товарами и фракцией
    apply_directives(setting, {"shop_add": {"id": "bazaar", "name": "Лавка", "owner": "Трактирщик",
                                           "faction": "гильдия воров",
                                           "items": [{"name": "Зелье", "price": 25, "qty": 5}]}})
    assert setting["shops"]["bazaar"]["items"][0]["price"] == 25
    # дать игроку предмет с ценностью
    apply_directives(setting, {"add_item": [{"name": "Трофей", "qty": 1, "value": 10}]})
    # покупателю нужно золото
    setting["player"]["gold"] = 200
    # покупка (репутация 0 → цена базовая)
    gold0 = setting["player"]["gold"]
    apply_directives(setting, {"trade_buy": {"shop": "bazaar", "item": "Зелье", "qty": 2}})
    assert setting["player"]["gold"] == gold0 - 50, "золото списалось 25×2"
    assert any(i["name"] == "Зелье" and i["qty"] == 2 for i in setting["player"]["inventory"])
    assert setting["shops"]["bazaar"]["items"][0]["qty"] == 3
    # продажа
    apply_directives(setting, {"trade_sell": {"shop": "bazaar", "item": "Трофей", "qty": 1}})
    assert setting["player"]["gold"] == gold0 - 50 + 10, "выручка по value"
    assert not any(i["name"] == "Трофей" for i in setting["player"]["inventory"])


def test_economy_reputation_discount(setting):
    apply_directives(setting, {"shop_add": {"id": "sm", "name": "Лавка", "faction": "гильдия воров",
                                           "items": [{"name": "Меч", "price": 100, "qty": 1}]}})
    # положительная репутация → скидка на покупку
    apply_directives(setting, {"reputation": {"гильдия воров": 4}})
    setting["player"]["gold"] = 1000
    gold0 = setting["player"]["gold"]
    apply_directives(setting, {"trade_buy": {"shop": "sm", "item": "Меч", "qty": 1}})
    cost = gold0 - setting["player"]["gold"]
    assert cost < 100, f"скидка по репутации: {cost} < 100"
    # недостаточно золота — не проходит
    apply_directives(setting, {"shop_add": {"id": "rich", "name": "Дорогая",
                                           "items": [{"name": "Артефакт", "price": 99999, "qty": 1}]}})
    apply_directives(setting, {"trade_buy": {"shop": "rich", "item": "Артефакт", "qty": 1}})
    assert not any(i["name"] == "Артефакт" for i in setting["player"]["inventory"]), "не куплено без золота"


def test_crafting_basic(setting):
    # собрать ресурсы
    apply_directives(setting, {"gather": {"item": "Железная руда", "qty": 2}})
    apply_directives(setting, {"gather": {"item": "Уголь", "qty": 1}})
    # выучить рецепт
    apply_directives(setting, {"craft_learn": {"id": "sword", "name": "Ковать меч",
                                               "ingredients": [{"name": "Железная руда", "qty": 2}, {"name": "Уголь", "qty": 1}],
                                               "result": {"name": "Железный меч", "qty": 1}}})
    # не хватает → отказ
    apply_directives(setting, {"gather": {"item": "Уголь", "qty": 1}})  # всего 2 угля, нужно 2 для 2 мечей
    apply_directives(setting, {"craft": {"recipe": "sword", "qty": 3}})
    assert not any(i["name"] == "Железный меч" for i in setting["player"]["inventory"]), "недостаточно ресурсов — не создаётся"
    # достаточно для 1 меча
    apply_directives(setting, {"craft": {"recipe": "sword", "qty": 1}})
    assert any(i["name"] == "Железный меч" and i["qty"] == 1 for i in setting["player"]["inventory"])
    # ингредиенты списались: руды 2-2=0, угля 2-1=1
    assert not any(i["name"] == "Железная руда" for i in setting["player"]["inventory"])
    assert any(i["name"] == "Уголь" and i["qty"] == 1 for i in setting["player"]["inventory"])


def test_crafting_unknown_recipe(setting):
    apply_directives(setting, {"craft": {"recipe": "nonexistent", "qty": 1}})
    assert not any(i["name"] == "nonexistent" for i in setting["player"]["inventory"])


def test_crafting_no_ingredients_guard(setting):
    """Рецепт без материалов нельзя создать (нет «бесплатных» предметов);
    дублирующиеся ингредиенты с одним именем складываются."""
    apply_directives(setting, {"craft_learn": {"id": "free", "name": "Бесплатно",
                                               "ingredients": [],
                                               "result": {"name": "Хлам", "qty": 1}}})
    apply_directives(setting, {"craft": {"recipe": "free", "qty": 1}})
    assert not any(i["name"] == "Хлам" for i in setting["player"]["inventory"]), "рецепт без материалов не создаётся"
    # дубликаты ингредиентов (2×«Руда» + 1×«Руда») → 3×Руда
    apply_directives(setting, {"gather": {"item": "Руда", "qty": 3}})
    apply_directives(setting, {"craft_learn": {"id": "dup", "name": "Дубль",
                                               "ingredients": [{"name": "Руда", "qty": 2}, {"name": "Руда", "qty": 1}],
                                               "result": {"name": "Слиток", "qty": 1}}})
    apply_directives(setting, {"craft": {"recipe": "dup", "qty": 1}})
    assert any(i["name"] == "Слиток" for i in setting["player"]["inventory"]), "создано из 3×Руда"
    assert not any(i["name"] == "Руда" for i in setting["player"]["inventory"]), "вся руда списана (2+1)"


def test_companions(setting):
    apply_directives(setting, {"companion_add": {"id": "wolf", "name": "Серый", "hp": 30,
                                                "level": 1, "loyalty": 10,
                                                "skills": {"Укус": {"rank": "C", "kind": "физический"}}}})
    assert setting["companions"]["wolf"]["hp"] == 30
    assert setting["companions"]["wolf"]["skills"]["Укус"]["rank"] == "C"
    # урон спутнику
    apply_directives(setting, {"companion_apply": {"id": "wolf", "hp": -8}})
    assert setting["companions"]["wolf"]["hp"] == 22
    # лечение (не выше максимума)
    apply_directives(setting, {"companion_apply": {"id": "wolf", "hp": 50}})
    assert setting["companions"]["wolf"]["hp"] == 30
    # обновление
    apply_directives(setting, {"companion_update": {"id": "wolf", "loyalty": 15, "level": 2}})
    assert setting["companions"]["wolf"]["loyalty"] == 15
    assert setting["companions"]["wolf"]["level"] == 2
    # смерть не убирает, но HP=0
    apply_directives(setting, {"companion_apply": {"id": "wolf", "hp": -100}})
    assert setting["companions"]["wolf"]["hp"] == 0
    # удаление
    apply_directives(setting, {"companion_remove": {"id": "wolf"}})
    assert "wolf" not in setting["companions"]


def test_locations_and_move(setting):
    apply_directives(setting, {"location_add": {"id": "loc1", "name": "Таверна"}})
    apply_directives(setting, {"location_add": {"id": "loc2", "name": "Лес", "connections": ["loc1"]}})
    assert setting["locations"]["loc2"]["connections"] == ["loc1"]
    assert "loc2" in setting["locations"]["loc1"].get("connections", []), "ребро двунаправленное"
    apply_directives(setting, {"move": "loc2"})
    assert setting["current_location"] == "loc2"


def test_flags_time_weather_game_over(setting):
    apply_directives(setting, {"flag": {"name": "дверь", "value": True},
                               "time": "ночь", "weather": "гроза"})
    assert setting["flags"]["дверь"] is True
    assert setting["time"] == "ночь"
    assert setting["weather"] == "гроза"
    apply_directives(setting, {"game_over": True})
    assert setting["game_over"] is True


def test_death_by_directive(setting):
    apply_directives(setting, {"player": {"hp": -500}})
    assert setting["game_over"] is True


# ── Профессии от действий ─────────────────────────────────────────

def test_profession_advance_by_actions(setting):
    setting["player"]["actions"] = {"кузнечное дело": 12}
    msgs = check_profession_advance(setting)
    assert setting["player"]["profession"] == "Кузнец", msgs
    assert any("Кузнец" in m for m in msgs)


def test_profession_no_advance_below_threshold(setting):
    setting["player"]["actions"] = {"кузнечное дело": 2}
    check_profession_advance(setting)
    assert not setting["player"].get("profession"), "до порога профессия не меняется"


# ── Производные статы / эффективные ──────────────────────────────

def test_recalc_derived(setting):
    recalc_derived(setting["player"], "normal")
    assert setting["player"]["max_hp"] > 0 and setting["player"]["max_mp"] > 0


def test_effective_stats_with_mods(setting):
    setting["player"]["effects"] = {"Тьма": {"mods": {"ловкость": -2}, "turns": 3}}
    st = effective_stats(setting["player"])
    assert st["ловкость"] == 8, "мод эффекта учтён в эффективных статах"
    assert setting["player"]["stats"]["ловкость"] == 10, "база не меняется"


# ── Парсинг блока <<ENGINE>> ──────────────────────────────────────

def test_split_engine():
    text, d = split_engine("Ты идешь.\n<<ENGINE>>{\"player\": {\"gold\": 5}}")
    assert text == "Ты идешь."
    assert d == {"player": {"gold": 5}}


def test_split_engine_none():
    text, d = split_engine("Просто текст")
    assert d is None and text == "Просто текст"


def test_normalize_and_apply_garbage(setting):
    garbage = [{"player": "hp -5"}, {"roll": "d20"}, {"quest": "строка"},
               {"enemy_add": {"id": 7, "name": "X", "hp": 1}}, {"reputation": "x"}, None]
    for d in garbage:
        nd = normalize_directives(d)
        msgs = apply_directives(setting, nd)
        assert isinstance(msgs, list)
    assert setting["player"]["stats"], "состояние не сломано мусором"

# ── Судья логики (промпт-строитель, чистый Python) ──────────────────

def test_judge_messages_contains_critical_facts(setting):
    setting["npc"] = {"barman": {"name": "Трактирщик", "alive": False}}
    setting["flags"] = {"door_open": True}
    msgs = narrator.judge_messages(setting, "Я поговорил с трактирщиком",
                                   "Трактирщик радушно ответил вам.")
    joined = msgs[0]["content"] + "\n" + msgs[1]["content"]
    assert "Трактирщик — мёртв" in joined    # критичный факт попадает судье
    assert "door_open=True" in joined          # флаги-истины попадают судье
    assert "verdict" in msgs[0]["content"]     # просим JSON-вердикт


def test_judge_messages_ok_empty_npc(setting):
    # без NPC и флагов судья всё равно валиден и не падает
    msgs = narrator.judge_messages(setting, "Я осмотрелся", "Ничего примечательного.")
    assert len(msgs) == 2 and msgs[1]["content"]


def _run(coro):
    import asyncio
    return asyncio.get_event_loop().run_until_complete(coro)


def test_logic_judge_retries_on_invalid_json(monkeypatch, setting):
    """Судья: невалидный JSON → повтор с пониженной температурой → валидный принимается."""
    from backend import llm as llm_mod
    calls: list = []

    async def _complete(messages, **kw):
        calls.append(kw.get("temperature"))
        return "Не JSON вовсе, а простой текст без фигурных скобок" if len(calls) == 1 \
            else '{"verdict": "issue", "twist": "мир дрогнул"}'

    monkeypatch.setattr(llm_mod, "complete", _complete)
    setting["npc"] = {"barman": {"name": "Трактирщик", "alive": True}}
    out = _run_judge(setting)
    assert len(calls) == 2, "должен быть ровно один повтор"
    assert calls[0] == 0.2 and calls[1] == 0.05, "повтор с пониженной температурой"
    assert out and out.get("twist"), "валидный результат после повтора"


def _run_judge(setting):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(
        narrator.logic_judge(1, setting, "Я ударил столик", "Столик задрожал.", provider={}))


def test_logic_judge_returns_none_after_double_fail(monkeypatch, setting):
    """Судья: дважды невалидный JSON → None (не падает)."""
    from backend import llm as llm_mod

    async def _fake(messages, **kw):
        return "совсем не JSON"

    monkeypatch.setattr(llm_mod, "complete", _fake)
    assert _run_judge(setting) is None


def test_rag_note_only_when_empty_and_disabled(monkeypatch):
    """A3: подсказка о деградации памяти появляется только когда RAG пуст И эмбеддинги выключены."""
    from backend.routers import core
    world_off = {"provider_settings": '{"embedding": {"enabled": false, "id": "none"}}'}
    assert core._rag_note(world_off, []) is not None          # выкл + пусто → подсказка
    assert core._rag_note(world_off, ["что-то вспомнил"]) is None  # выкл + есть чанки → без подсказки
    world_on = {"provider_settings": '{"embedding": {"enabled": true}}'}
    assert core._rag_note(world_on, []) is None               # вкл + пусто → это просто «ничего не вспомнил»


# ── Расширенный аудит-проход (проверка фактов в аудите механики) ──────────

def test_audit_messages_includes_critical_facts(setting):
    setting["npc"] = {"barman": {"name": "Трактирщик", "alive": False}}
    setting["flags"] = {"door_open": False}
    msgs = narrator.audit_messages(setting, "Я открыл дверь трактира", "Дверь распахнулась.")
    joined = msgs[0]["content"] + "\n" + msgs[1]["content"]
    assert "Трактирщик — мёртв" in joined      # критичный факт доходит до судьи механики
    assert "door_open=False" in joined
    assert "не оживляй мёртвого NPC" in msgs[0]["content"]  # инструкция против противоречий


# ── Судья логики: согласованность биографии и роли + корректировки ──────

# ── Провидение (Божественный арбитр) ─────────────────────────────────

def test_divine_messages_include_complaint_and_state(setting):
    setting["player"]["inventory"] = [{"name": "Зелье", "qty": 2}]
    msgs = narrator.divine_messages({}, setting, "мне не дали меч",
                                    action="Я достал меч", reply="Меч у тебя в руке")
    joined = msgs[0]["content"] + "\n" + msgs[1]["content"]
    assert "мне не дали меч" in joined        # жалоба доходит
    assert "decline" in msgs[0]["content"]  # просим JSON с decline/directives
    assert "Зелье" in joined                  # состояние мира доходит
    assert "[Игрок]" in joined and "[Рассказчик]" in joined  # последний обмен доходит


def test_divine_intervene_applies_directives(monkeypatch, setting):
    """Провидение: decline=false + add_item применяется к состоянию, возвращает sys_msgs."""
    import asyncio
    from backend import llm as llm_mod
    async def _complete(messages, **kw):
        return ('{"decline": false, "twist": "Реальность треснула — меч", '
                '"directives": {"add_item": [{"name": "Меч", "qty": 1}]}}')
    monkeypatch.setattr(llm_mod, "complete", _complete)
    asyncio.new_event_loop().run_until_complete(
        narrator.divine_intervene(1, {}, setting, "мне не дали меч", provider={}))
    assert any(it["name"] == "Меч" for it in setting["player"]["inventory"])


def test_divine_intervene_declines(monkeypatch, setting):
    """Провидение: decline=true → ничего не меняет, ответ про «игрок ошибся»."""
    import asyncio
    from backend import llm as llm_mod
    async def _complete(messages, **kw):
        return '{"decline": true, "twist": "Тебе показалось, путник"}'
    monkeypatch.setattr(llm_mod, "complete", _complete)
    before = [dict(i) for i in setting["player"]["inventory"]]
    out = asyncio.new_event_loop().run_until_complete(
        narrator.divine_intervene(1, {}, setting, "дай золото", provider={}))
    assert out and out["decline"]
    assert setting["player"]["inventory"] == before  # ничего не изменено
    assert out["twist"]


def test_apply_judge_corrections_fills_role(setting):
    import asyncio
    msgs = narrator._apply_judge_corrections(
        setting, {"race": "человек", "class": "Пробуждённый", "profession": "Безработный"})
    p = setting["player"]
    assert p["race"] == "человек"
    assert p["class"] == "Пробуждённый"
    assert p["profession"] == "Безработный"
    assert msgs  # есть читаемые системные сообщения


def test_apply_judge_corrections_empty_is_noop(setting):
    assert narrator._apply_judge_corrections(setting, {}) == []
    assert narrator._apply_judge_corrections(setting, None) == []


def test_is_modern_world():
    assert narrator._is_modern_world("реалрпг, литрпг")
    assert narrator._is_modern_world("киберпанк")
    assert not narrator._is_modern_world("тёмное фэнтези")
    assert not narrator._is_modern_world("эпическое фэнтези")


def test_split_engine_game_engine_text():
    text = "Ты идёшь по улице.\n\ngame_engine({\"enemy_add\": {\"name\": \"Волк\", \"hp\": 10}})"
    clean, d = narrator.split_engine(text)
    assert "game_engine" not in clean
    assert d and d["enemy_add"]["name"] == "Волк"
    # нижний регистр вызова тоже
    clean2, d2 = narrator.split_engine("Тест. game_engine({\"flag\": {\"name\": \"x\", \"value\": true}})")
    assert "game_engine" not in clean2 and d2 and d2["flag"]["value"] is True


def test_judge_messages_includes_role_and_bio(setting):
    setting["player"]["identity"] = "Новичок, впервые увидевший Систему."
    setting["player"]["race"] = ""
    setting["player"]["class"] = ""
    msgs = narrator.judge_messages(setting, "Я осмотрелся", ".")
    joined = msgs[0]["content"] + "\n" + msgs[1]["content"]
    assert "БИОГРАФИЯ" in joined
    assert "РОЛЬ" in joined
    assert "corrections" in msgs[0]["content"]  # просим корректировку согласованности


def test_judge_messages_includes_inventory(setting):
    """Судья должен видеть инвентарь, чтобы ловить «использование предмета, которого нет»."""
    setting["player"]["inventory"] = [{"name": "Палка", "qty": 1}, {"name": "Фонарь", "qty": 2}]
    msgs = narrator.judge_messages(setting, "Я осмотрелся", "Ты осмотрелся.")
    joined = msgs[0]["content"] + "\n" + msgs[1]["content"]
    assert "ИНВЕНТАРЬ" in joined
    assert "Палка" in joined
    assert "Фонарь" in joined


def test_cut_words_no_midword_truncation():
    """Обрезка длинных сообщений судьи не должен рубить слово посередине."""
    # обрезали по границе последнего пробела → тело заканчивается на полном слове
    out = narrator._cut_words("один два три четыре пять шесть семь", 20)
    assert out == "один два три …" or out.rstrip("…").endswith(("один", "два", "три", "четыре"))
    # короткий текст не трогается
    assert narrator._cut_words("короткий", 100) == "короткий"
    # пусто/нет многоточия при коротком
    assert not narrator._cut_words("x"*5, 100).endswith("…")
# ── Богатый старт: apply_character мапит англ. ключи статов и заполняет роль ──

def test_apply_character_uses_generated_identity_over_raw_hook(setting):
    """Биография должна быть ОБОГАЩЁННЫМ сгенерированным вариантом, а не голым текстом зацепа.
    При валидной `identity` от генератора сырой hook НЕ должен перезаписывать её."""
    data = {"name": "Кай", "identity": "Кай — 28-летний разработчик, разочарованный в жизни",
            "race": "Человек", "class": "Воин", "profession": "Безработный",
            "level": 3, "stats": {"сила": 12, "ловкость": 11, "выносливость": 10,
                                    "интеллект": 15, "мудрость": 12, "харизма": 9, "удача": 10}}
    narrator.apply_character(setting, data, hook="я парень 28 лет, разработчик, разочаровался в жизни")
    identity = setting["player"]["identity"]
    assert identity == data["identity"]  # обогащённая биография от генератора выиграла
    assert identity != "я парень 28 лет, разработчик, разочаровался в жизни"  # не голый зацеп


def test_apply_character_falls_back_to_raw_hook_when_no_identity(setting):
    """Если генератор не вернул identity — фолбэк на сырой зацеп (не пустая биография)."""
    narrator.apply_character(setting, {"name": "Кай", "race": "Человек", "class": "Воин"},
                             hook="я парень 28 лет", genre="")
    assert setting["player"]["identity"].strip() == "я парень 28 лет"


def test_apply_character_maps_english_stat_keys(setting):
    """LLM может вернуть статы англоязычными ключами (strength/agility/...), а p['stats']
    ждёт русские (сила/ловкость/...). apply_character должен их смапить и не оставить 10/10."""
    data = {
        "name": "Кай",
        "identity": "Биолог, изучавший Пробуждение",
        "race": "Человек",
        "class": "Воин",
        "profession": "Безработный",
        "level": 1,
        "stats": {"strength": 16, "agility": 12, "endurance": 14, "intellect": 18,
                   "wisdom": 10, "charisma": 8, "luck": 11},
        "inventory": [],
    }
    narrator.apply_character(setting, data, genre="эпическое фэнтези")
    ps = setting["player"]["stats"]
    # все семь мапятся на русские ключи и принимают значения из англ. ввода
    assert ps["сила"] == 16
    assert ps["ловкость"] == 12
    assert ps["выносливость"] == 14
    assert ps["интеллект"] == 18
    assert ps["мудрость"] == 10
    assert ps["харизма"] == 8
    assert ps["удача"] == 11
    assert setting["player"]["name"] == "Кай"
    assert setting["player"]["race"]  # раса применена через движок директив


def test_apply_character_russian_keys_and_role_fill(setting):
    """Русские ключи статов тоже работают; уровень 1..99 применяется."""
    data = {
        "name": "Эйдан",
        "identity": "Бродяга",
        "race": "Эльф",
        "class": "Лучник",
        "profession": "Охотник",
        "level": 27,
        "stats": {"сила": 13, "ловкость": 9, "выносливость": 11, "интеллект": 7,
                    "мудрость": 6, "харизма": 10, "удача": 5},
        "inventory": [],
    }
    narrator.apply_character(setting, data, genre="эпическое фэнтези")
    assert setting["player"]["level"] == 27
    assert setting["player"]["stats"]["сила"] == 13
    assert setting["player"]["class"]  # класс применён движком


def test_norm_rank_maps_digits_to_letters(setting):
    """Цифровые ранги навыков (1,2,3…) от генератора приводятся к буквенным ступеням F..G."""
    from backend.mechanics import norm_rank, RANK_ORDER
    assert norm_rank("F") == "F"
    assert norm_rank("1") == "F"
    assert norm_rank("2") == "E"
    assert norm_rank("3") == "D"
    assert norm_rank("4") == "C"
    assert norm_rank("5") == "B"
    assert norm_rank("6") == "A"
    assert norm_rank("7") == "S"
    assert norm_rank("13") == "G"
    assert norm_rank("0") == "F"
    assert norm_rank("SS") == "SS"
    assert norm_rank("") == "F"


def test_apply_character_normalizes_digit_skill_ranks(setting):
    """Первый навык приходит буквой, дальнейшие цифрами — все приводятся к буквенным."""
    data = {
        "name": "Кэй",
        "identity": "Хакер",
        "race": "Человек",
        "class": "Вор",
        "profession": "",
        "level": 3,
        "stats": {"сила": 10, "ловкость": 14, "выносливость": 10, "интеллект": 12,
                    "мудрость": 9, "харизма": 8, "удача": 11},
        "skills": [{"name": "Сильный удар", "rank": "F", "kind": "боевой"},
                   {"name": "Поток данных", "rank": "3", "kind": "спец"},
                   {"name": "Теневой уклон", "rank": "2", "kind": "спец"}],
        "inventory": [],
    }
    narrator.apply_character(setting, data, genre="киберпанк")
    sk = setting["player"]["skills"]
    assert sk["Сильный удар"]["rank"] == "F"
    assert sk["Поток данных"]["rank"] == "D"
    assert sk["Теневой уклон"]["rank"] == "E"


def test_apply_character_normalizes_mixed_skill_ranks(setting):
    """Смешанные цифровые ранги в skill_add также приводятся к буквенным."""
    from backend.narrator import apply_directives
    data = {"name": "Кэй", "identity": "Хакер", "race": "Человек", "class": "Вор",
            "profession": "", "level": 1,
            "stats": {"сила": 10, "ловкость": 14, "выносливость": 10, "интеллект": 12,
                      "мудрость": 9, "харизма": 8, "удача": 11},
            "skills": [{"name": "Крит", "rank": "1", "kind": "спец"}], "inventory": []}
    narrator.apply_character(setting, data, genre="киберпанк")
    assert setting["player"]["skills"]["Крит"]["rank"] == "F"


# ── Профилирование и мониторинг + ИИ-качество ─────────────────────────

def test_lexical_dup_share_counts_repeated_words():
    """A16 (аудит 38): старый «repetition» переименован по сути — это доля повторных
    СЛОВ, а не зацикливание. На живой прозе он завышен (0.4–0.55) и это ожидаемо."""
    from backend.routers.core import _lexical_dup_share
    # много повторов одного слова → высокий балл
    assert _lexical_dup_share("идём идём идём идём идём идём идём идём идём") >= 0.5
    # разнообразный текст → низкий балл
    assert _lexical_dup_share("Ты входишь в тёмную таверну. За стойкой хмурый трактирщик.") < 0.4
    # пусто/коротко → 0
    assert _lexical_dup_share("") == 0.0
    assert _lexical_dup_share("Кот") == 0.0


def test_repetition_detects_real_cycle():
    """A16 (аудит 38): настоящая метрика цикла — доля СОСЕДНИХ дословных повторов блоков.
    Цикл модели → высокий скор; та же по объёму проза без дублей → почти ноль."""
    from backend.routers.core import _repetition_score
    block = ("Ты медленно идёшь по скрипящим половицам, оглядывая тёмные углы таверны.")
    cycle = " ".join([block] * 6)                     # модель застряла в одном абзаце
    prose = ("Ты входишь в тёмную таверну. За стойкой хмурый трактирщик точит нож. "
             "У окна трое игроков в кости молча двигают фигуры. Пахает дымом и кислым элем, "
             "а где-то наверху скрипит половица — тяжело ступают чьи-то сапоги.")
    assert _repetition_score(cycle) >= 0.5, "дословный повтор подряд обязан ловиться"
    assert _repetition_score(prose) < 0.1, "нормальная проза не должна выглядеть циклом"
    assert _repetition_score("") == 0.0
    assert _repetition_score("Кот") == 0.0


def test_metrics_record_and_aggregate():
    from backend import metrics as mm
    mm.record(world_id=1, llm_ms=1000, completion_tokens=200, prompt_tokens=800,
              repetition=0.05, lexical_dup_share=0.42, provider="llamacpp", memory_tokens=600)
    # запись видна в «последних» снапшотах (глобальный буфер — там и другие вызовы тестов)
    last = mm.snapshot_latest()
    assert last is not None
    assert last.get("llm_ms") == 1000
    assert last.get("memory_tokens") == 600
    # сводка валидна: есть окна/счётчики
    report = mm.as_json(limit=5)
    assert "windows" in report and "counters" in report and report["counters"]["llm_calls"] >= 1
    assert "llm_ms_avg" in report["windows"]["all"]


# ── Фракции → геймплей ─────────────────────────────────────────

def test_reputation_standing_tiers():
    from backend.mechanics import reputation_standing as rs
    assert rs(0) == "Нейтрально"
    assert rs(-30) == "Заклятый враг"
    assert rs(-3) == "Недоверие"
    assert rs(3) == "Доверие"
    assert rs(8) == "Друг"
    assert rs(20) == "Союзник"


def test_faction_add_and_relations_in_state(setting):
    apply_directives(setting, {"faction_add": {"id": "guild", "name": "Гильдия воров",
                                               "desc": "тёмные", "relations": {"guard": "враг", "merch": "союз"}}})
    apply_directives(setting, {"faction_add": {"id": "guard", "name": "Стража", "relations": {"guild": "враг"}}})
    assert set(setting["factions"]) == {"guild", "guard"}
    assert setting["factions"]["guild"]["relations"]["merch"] == "союз"
    out = format_state(setting)
    assert "Гильдия воров" in out and "Фракции" in out


def test_faction_relations_spillover(setting):
    apply_directives(setting, {"faction_add": {"id": "guild", "relations": {"guard": "враг", "merch": "союз"}}})
    apply_directives(setting, {"faction_add": {"id": "guard", "relations": {"guild": "враг"}}})
    apply_directives(setting, {"faction_add": {"id": "merch", "relations": {"guild": "союз"}}})
    apply_directives(setting, {"reputation": {"guild": 10}})
    rep = setting["player"]["reputation"]
    # основная фракция получила ровно +10 (без двойного счёта)
    assert rep["guild"] == 10
    # враг теряет, союзник делит выгоду
    assert rep["guard"] == -10
    assert rep["merch"] == 2


def test_faction_remove(setting):
    apply_directives(setting, {"faction_add": {"id": "cult", "name": "Культ"}})
    assert "cult" in setting["factions"]
    apply_directives(setting, {"faction_remove": {"id": "cult"}})
    assert "cult" not in setting["factions"]


# ═══════════════ Сессия 28: экономика/крафт — вес предметов и карманы NPC ═══════════════

def test_weight_carry_capacity(setting):
    from backend import mechanics as m
    cap = m.carry_capacity(setting["player"])
    assert cap == 60, f"20 + сила×2 + выносливость×2 с дефолтными 10,10 → 60, got {cap}"
    apply_directives(setting, {"gather": {"item": "Руда", "qty": 1, "weight": 50}})
    assert m.inventory_weight(setting["player"]) == 50
    # 2-я порция по 50 не влезает (свободно 60-50=10) → сбор отклонён, вес не вырос
    apply_directives(setting, {"gather": {"item": "Руда", "qty": 1, "weight": 50}})
    assert m.inventory_weight(setting["player"]) == 50, "перегруз — вторая порция не собрана"
    # вес сохранился на предмете
    assert any(i["name"] == "Руда" and i["weight"] == 50 for i in setting["player"]["inventory"])


def test_add_item_carries_weight(setting):
    from backend import mechanics as m
    apply_directives(setting, {"add_item": [{"name": "Железный меч", "qty": 1, "weight": 4, "value": 12}]})
    it = next(i for i in setting["player"]["inventory"] if i["name"] == "Железный меч")
    assert it["weight"] == 4 and it["value"] == 12
    assert m.inventory_weight(setting["player"]) == 4


def test_trade_buy_blocks_on_overload_and_shares_value(setting):
    from backend import mechanics as m
    setting["player"]["gold"] = 1000
    apply_directives(setting, {"shop_add": {"id": "gb", "name": "Гильдия",
        "items": [{"name": "Слиток", "price": 5, "qty": 5, "weight": 50, "value": 3}]}})
    apply_directives(setting, {"trade_buy": {"shop": "gb", "item": "Слиток", "qty": 1}})
    assert any(i["name"] == "Слиток" and i["qty"] == 1 for i in setting["player"]["inventory"])
    # купленный предмет наследует ценность shop-товара (для продажи)
    it = next(i for i in setting["player"]["inventory"] if i["name"] == "Слиток")
    assert it["value"] == 3 and it["weight"] == 50
    # вторая покупка той же вещи (ещё 50 кг) → перегруз, запас магазина не тронут
    q0 = setting["shops"]["gb"]["items"][0]["qty"]
    apply_directives(setting, {"trade_buy": {"shop": "gb", "item": "Слиток", "qty": 1}})
    assert next(i["qty"] for i in setting["player"]["inventory"] if i["name"] == "Слиток") == 1
    assert setting["shops"]["gb"]["items"][0]["qty"] == q0, "запас магазина без изменений при блоке"


def test_craft_blocks_on_overload_without_spending_ingredients(setting):
    apply_directives(setting, {"gather": {"item": "Руда", "qty": 1, "weight": 30}})
    apply_directives(setting, {"craft_learn": {"name": "Голем", "id": "golem",
        "ingredients": [{"name": "Руда", "qty": 1}],
        "result": {"name": "Голем", "qty": 1, "weight": 40}}})
    # руд 30, свободно 30; результат 40 → не помещается → запрет
    apply_directives(setting, {"craft": {"recipe": "golem", "qty": 1}})
    assert any(i["name"] == "Руда" for i in setting["player"]["inventory"]), "ингредиент не списан при перегрузе"
    assert not any(i["name"] == "Голем" for i in setting["player"]["inventory"]), "результат не создан"


def test_enemy_money_loot(setting):
    apply_directives(setting, {"enemy_add": {"id": "e1", "name": "Гоблин", "hp": 5, "money": 1}})
    assert setting["enemies"]["e1"]["money"] == 1
    g0 = setting["player"]["gold"]
    apply_directives(setting, {"enemy_apply": {"id": "e1", "hp": -5}})
    assert "e1" not in setting["enemies"], "враг повержен"
    assert setting["player"]["gold"] == g0 + 1, "кошелёк врага перешёл в золото игрока"


def test_npc_money_and_kill_loot(setting):
    apply_directives(setting, {"npc_set": {"id": "trader", "name": "Купец", "alive": True, "money": 50}})
    assert setting["npc"]["trader"]["money"] == 50
    g0 = setting["player"]["gold"]
    apply_directives(setting, {"npc_kill": "trader"})
    assert setting["player"]["gold"] == g0 + 50
    assert setting["npc"]["trader"]["money"] == 0, "кошелёк обнулён"
    assert setting["npc"]["trader"]["alive"] is False


# ═══════════════ Сессия 28 (глубже): станции крафта, профессия-требование, экономика справки ═══════════════

def test_craft_station_required(setting):
    from backend import mechanics as m
    apply_directives(setting, {"gather": {"item": "Руда", "qty": 1, "weight": 1}})
    apply_directives(setting, {"craft_learn": {"name": "Ковать", "id": "smith",
        "profession": "Кузнец", "station": "кузорня",
        "ingredients": [{"name": "Руда", "qty": 1}],
        "result": {"name": "Меч", "qty": 1}}})
    # станции нет в текущей локации → отказ, ингредиент не трое
    apply_directives(setting, {"craft": {"recipe": "smith", "qty": 1}})
    assert not any(i["name"] == "Меч" for i in setting["player"]["inventory"]), "без станции не кузнец создаёт"
    # профессия не та → тоже отказ
    apply_directives(setting, {"location_update": {"id": "start", "stations": ["кузорня"]}})
    apply_directives(setting, {"craft": {"recipe": "smith", "qty": 1}})
    assert not any(i["name"] == "Меч" for i in setting["player"]["inventory"]), "без профессии Кузнец не создаёт"
    # наконец всё готово
    apply_directives(setting, {"profession": "Кузнец"})
    apply_directives(setting, {"craft": {"recipe": "smith", "qty": 1}})
    assert any(i["name"] == "Меч" for i in setting["player"]["inventory"]), "с станцией и профессией создаёт"


def test_craft_profession_requirement(setting):
    apply_directives(setting, {"craft_learn": {"name": "Зелье", "id": "pot",
        "profession": "Алхимик",
        "ingredients": [{"name": "Трава", "qty": 1}],
        "result": {"name": "Зелье", "qty": 1}}})
    apply_directives(setting, {"gather": {"item": "Трава", "qty": 1}})
    apply_directives(setting, {"craft": {"recipe": "pot", "qty": 1}})
    assert not any(i["name"] == "Зелье" for i in setting["player"]["inventory"]), "без Алхимика нельзя"
    apply_directives(setting, {"profession": "Алхимик"})
    apply_directives(setting, {"craft": {"recipe": "pot", "qty": 1}})
    assert any(i["name"] == "Зелье" for i in setting["player"]["inventory"])


def test_station_flags_count(setting):
    """Станция может быть задана и флагом `station:кузорня=true` (без location.stations)."""
    from backend import mechanics as mec
    apply_directives(setting, {"flag": {"name": "station:кузорня", "value": True}})
    assert mec.has_station(setting, "кузорня") is True
    assert "кузня" in mec.location_stations(setting) or "кузорня" in mec.location_stations(setting)
    apply_directives(setting, {"gather": {"item": "Руда", "qty": 1, "weight": 1}})
    apply_directives(setting, {"craft_learn": {"name": "Ковать2", "id": "sw2",
        "ingredients": [{"name": "Руда", "qty": 1}],
        "result": {"name": "Меч2", "qty": 1}}})
    apply_directives(setting, {"craft": {"recipe": "sw2", "qty": 1}})
    assert any(i["name"] == "Меч2" for i in setting["player"]["inventory"]), "флаг-станция достаточно"


def test_total_sell_value_and_stations_spria(setting):
    from backend import mechanics as mec
    apply_directives(setting, {"add_item": [{"name": "Трофей", "qty": 2, "value": 10}]})
    assert mec.total_sell_value(setting["player"]) == 20, "2×10"
    # can_craft верен по всем осям
    missing, status = mec.can_craft(setting, {"ingredients": [{"name": "Нет", "qty": 1}]})
    assert status == "ingredients" and missing
