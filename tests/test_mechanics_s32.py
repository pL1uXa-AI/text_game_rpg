# -*- coding: utf-8 -*-
"""Тесты механик сессии 32: таймеры мира, экипировка, потребности/рассудок,
календарь/сезоны, локации-зоны, доска объявлений, звания во фракциях, ставки на бросок."""
from __future__ import annotations


from backend import mechanics
from backend.mechanics import apply_directives, normalize_directives


def _world_setting(**kw):
    s = {"player": {"hp": 100, "max_hp": 100, "mp": 50, "max_mp": 50, "gold": 10,
                    "xp": 0, "level": 1, "inventory": []},
         "locations": {"start": {"name": "Старт", "desc": "", "connections": []}},
         "current_location": "start", "time": "день", "weather": "ясно",
         "flags": {}}
    s["player"].update(kw)
    mechanics.ensure_player_schema(s["player"])
    return s


# ── Таймеры мира ──
def test_timer_add_tick_expire():
    s = _world_setting()
    msgs = apply_directives(s, {"timer_add": {"name": "Бомба", "turns": 2, "desc": "тикает"}})
    assert any("Таймер" in m for m in msgs)
    assert s["timers"]["Бомба"]["turns_left"] == 2
    # 1-й ход
    msgs = mechanics.tick_world_timers(s)
    assert s["timers"]["Бомба"]["turns_left"] == 1
    # 2-й ход — истёк
    msgs = mechanics.tick_world_timers(s)
    assert "Бомба" not in s["timers"]
    assert any("истёк" in m for m in msgs)


def test_timer_remove_and_eternal():
    s = _world_setting()
    apply_directives(s, {"timer_add": {"name": "Вечный", "turns": -1}})
    apply_directives(s, {"timer_remove": {"name": "Вечный"}})
    assert "Вечный" not in s["timers"]
    apply_directives(s, {"timer_add": {"name": "Без срока", "turns": -1}})
    msgs = mechanics.tick_world_timers(s)
    # D12 (аудит 38): сверяем и СИСТЕМНЫЕ СООБЩЕНИЯ тика, а не только состояние:
    # бессрочный таймер обязан остаться и не выдать «истёк».
    assert "Без срока" in s["timers"]  # бессрочный не тикает
    assert not any("истёк" in m for m in msgs), f"бессрочный таймер объявлен истёкшим: {msgs}"
    assert s["timers"]["Без срока"].get("turns_left") in (-1, None)


# ── Экипировка и слоты ──
def test_equip_unequip():
    s = _world_setting()
    s["player"]["inventory"] = [
        {"name": "Шлем", "qty": 1, "slot": "голова", "bonus": {"выносливость": 2}},
        {"name": "Меч", "qty": 1, "slot": "оружие", "bonus": {"сила": 3}},
    ]
    ok, msg = mechanics.equip_item(s, "Шлем")
    assert ok
    assert s["player"]["equipped"]["голова"] == "Шлем"
    # второй шлем в тот же слот — замена
    s["player"]["inventory"].append({"name": "Шлем2", "qty": 1, "slot": "голова"})
    ok, msg = mechanics.equip_item(s, "Шлем2")
    assert ok and "Шлем2" in s["player"]["equipped"]["голова"]
    # бонусы складываются: Шлем снят при замене, его бонус ушёл
    bonuses = mechanics.equipped_bonuses(s["player"])
    assert bonuses.get("выносливость") in (None, 0)
    ok, msg = mechanics.unequip_item(s, "Шлем2")
    assert ok
    assert "голова" not in s["player"]["equipped"]
    # нет предмета
    ok, msg = mechanics.equip_item(s, "Нет такого")
    assert not ok


def test_equip_directive_and_stats():
    s = _world_setting()
    s["player"]["inventory"] = [{"name": "Броня", "qty": 1, "slot": "торс", "bonus": {"выносливость": 5}}]
    msgs = apply_directives(s, {"equip": {"item": "Броня"}})
    # D12: сообщение экипировки — часть контракта (игрок видит «🛡 Надето»)
    assert any("Броня" in m for m in msgs), f"нет системного сообщения об экипировке: {msgs}"
    assert s["player"]["equipped"]["торс"] == "Броня"
    eff = mechanics.effective_stats(s["player"])
    assert eff["выносливость"] == 10 + 5
    msgs = apply_directives(s, {"unequip": {"item": "Броня"}})
    assert any("Броня" in m for m in msgs), f"нет системного сообщения о снятии: {msgs}"
    assert "торс" not in s["player"]["equipped"]


# ── Потребности и рассудок ──
def test_needs_tick_and_directive():
    s = _world_setting()
    mechanics._ensure_axis(s["player"], "needs")
    before = s["player"]["needs"]["голод"]["value"]
    msgs = mechanics.tick_needs_mental(s)
    assert s["player"]["needs"]["голод"]["value"] < before
    assert isinstance(msgs, list), "тик потребностей обязан возвращать список сообщений"
    # директива дельты
    apply_directives(s, {"needs": {"голод": {"value": -50}}})
    assert s["player"]["needs"]["голод"]["value"] <= 100 - 50
    # предупреждение при критике (убыль идёт, сообщение о критике появляется)
    s2 = _world_setting()
    s2["player"]["needs"]["голод"] = {"value": 5, "max": 100, "decay": 0.8}
    msgs = mechanics.tick_needs_mental(s2)
    assert any("критическом" in m for m in msgs)


def test_mental_stress_grows():
    s = _world_setting()
    mechanics._ensure_axis(s["player"], "mental")
    before = s["player"]["mental"]["стресс"]["value"]
    mechanics.tick_needs_mental(s)
    assert s["player"]["mental"]["стресс"]["value"] > before


# ── Календарь/сезоны ──
def test_date_directive_and_season_mods():
    s = _world_setting()
    apply_directives(s, {"date": {"day": "15", "month": "ноябрь", "season": "зима"}})
    assert s["date"]["season"] == "зима"
    from backend.narrator import environment_mods
    mods = environment_mods("ночь", "ясно", season="зима")
    assert any("зим" in m.lower() or "холод" in m.lower() for m in mods)


# ── Локации-зоны ──
def test_location_zone_effects():
    s = _world_setting()
    s["locations"]["ruins"] = {"name": "Руины", "desc": "", "connections": [],
                               "effects": [{"name": "Радиация", "damage": 2, "desc": "опасно"}]}
    msgs = mechanics.apply_location_effects(s, "ruins", apply=True)
    assert "Радиация" in s["player"]["effects"]
    assert s["player"]["effects"]["Радиация"]["damage"] == 2
    # при выходе снимается
    msgs = mechanics.apply_location_effects(s, "ruins", apply=False)
    # D12/A2: именно это сообщение терялось в чате при move — проверяем его наличие
    assert any("Радиация" in m for m in msgs), f"нет сообщения о снятии эффекта зоны: {msgs}"
    assert "Радиация" not in s["player"]["effects"]


# ── Доска объявлений ──
def test_board_add_text():
    s = _world_setting()
    apply_directives(s, {"board_add": {"title": "Ищу героя", "text": "Награда 50 золота"}})
    assert len(s["board"]) == 1
    assert "Ищу героя" in mechanics.board_text(s)


# ── Звания во фракциях ──
def test_faction_rank_directive():
    s = _world_setting()
    apply_directives(s, {"faction_rank": {"faction": "guild", "rank": "Мастер-кузнец"}})
    assert s["player"]["faction_ranks"]["guild"] == "Мастер-кузнец"
    apply_directives(s, {"faction_rank": {"faction": "guild", "rank": ""}})
    assert "guild" not in s["player"]["faction_ranks"]


# ── Ставки на бросок (roll) ──
def test_roll_stakes_passthrough():
    d = normalize_directives({"roll": {"expr": "d20", "dc": 15, "stakes": {"success": "замок откроется", "fail": "сигнализация"}}})
    assert d["roll"]["stakes"]["success"] == "замок откроется"


# ── normalize новых директив ──
def test_normalize_new_directives():
    d = normalize_directives({"timer_add": "бомба", "equip": {"item": "меч"}, "vision_add": {"text": "сон"}})
    # timer_add строкой → dict {name}
    assert d.get("timer_add") == "бомба" or (isinstance(d.get("timer_add"), dict))
    assert d.get("equip", {}).get("item") == "меч"
    assert d.get("vision_add", {}).get("text") == "сон"


# ── регрессионные тесты найденных багов (ревизия) ──
def test_timer_zero_turns_expires_immediately():
    """turns: 0 не должен превращаться в бессрочный (баг `or -1`)."""
    s = _world_setting()
    apply_directives(s, {"timer_add": {"name": "Мгновенный", "turns": 0}})
    assert s["timers"]["Мгновенный"]["turns_left"] == 0
    msgs = mechanics.tick_world_timers(s)
    assert "Мгновенный" not in s["timers"]
    assert any("истёк" in m for m in msgs)


def test_timer_string_turns():
    s = _world_setting()
    apply_directives(s, {"timer_add": {"name": "Стр", "turns": "3"}})
    assert s["timers"]["Стр"]["turns_left"] == 3
    mechanics.tick_world_timers(s)
    assert s["timers"]["Стр"]["turns_left"] == 2


def test_board_dict_guard():
    """board_text не должен падать при board-не-списке (кривое состояние)."""
    s = _world_setting()
    s["board"] = {"x": 1}
    assert mechanics.board_text(s) == ""
    apply_directives(s, {"board_add": {"title": "Новое", "text": "объявление"}})
    assert s["board"][0]["title"] == "Новое"


def test_location_add_keeps_effects():
    """location_add с effects должен сохранять зоны (радиация/туман)."""
    s = _world_setting()
    apply_directives(s, {"location_add": {"id": "ruins", "name": "Руины",
                                           "effects": [{"name": "Радиация", "damage": 2}]}})
    assert s["locations"]["ruins"]["effects"][0]["name"] == "Радиация"
    apply_directives(s, {"location_update": {"id": "ruins", "effects": []}})
    assert "effects" not in s["locations"]["ruins"]


def test_needs_delta_not_override():
    """needs {value: -30} — дельта (50), а НЕ абсолют (-30)."""
    s = _world_setting()
    apply_directives(s, {"needs": {"голод": {"value": -30}}})
    assert s["player"]["needs"]["голод"]["value"] == 50.0
    # абсолют через set:
    apply_directives(s, {"needs": {"голод": {"set": 95}}})
    assert s["player"]["needs"]["голод"]["value"] == 95.0


def test_equipped_item_without_bonus():
    """Предмет без bonus при экипировке не ломает effective_stats."""
    s = _world_setting()
    s["player"]["inventory"] = [{"name": "Лук", "qty": 1, "slot": "оружие"}]
    apply_directives(s, {"equip": {"item": "Лук"}})
    assert s["player"]["equipped"]["оружие"] == "Лук"
    eff = mechanics.effective_stats(s["player"])
    assert eff["сила"] == 10


def test_format_state_corrupt_new_fields():
    """format_state не падает при timers-строке/board-dict/date-строке/pending_visions=None."""
    from backend.narrator import format_state
    s = _world_setting()
    s.update({"timers": "бомба", "board": {"x": 1}, "date": "зима", "pending_visions": None})
    out = format_state(s)
    assert "ТЕКУЩЕЕ СОСТОЯНИЕ" in out


def test_pending_visions_shown_in_state():
    from backend.narrator import format_state
    s = _world_setting()
    apply_directives(s, {"vision_add": {"text": "сон", "hint": "вспомни"}})
    out = format_state(s)
    assert "Очередь видений" in out
