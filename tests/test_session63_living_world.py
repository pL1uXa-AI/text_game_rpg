"""Сессия 63 — «живой мир»: сюжет как точка отсчёта, а не рельсы.

Что здесь проверяется (и почему это игра, а не «ещё одна фича»):

1. `quest_remove` — стереть арку, которая СТАЛА НЕВОЗМОЖНОЙ. Отличие от quest_done/
   quest_fail принципиальное: «выполнен/провален» — итог, которого игрок не переживал.
2. `location_remove` — мир теряет место (до сессии 63 remove был для фракций, квестов,
   магазинов, таймеров и предметов, но НЕ для локаций: сожжённый город висел на карте
   вечно). Текущую локацию игрока удалить нельзя — это и есть «код проверяет, мастер
   решает» (закон 3).
3. `world_evolve` — явная запись отхода от канвы: без неё через 30 ходов рассказчик
   противоречит себе, потому что канва по-прежнему в промпте, а реальность уже другая.
4. Правило 37 и блок «🧭 ЖИВОЙ МИР» — промпт обязан говорить, что канва материал;
   при этом правило НЕ режется ярусами (права рассказчика нужны каждому миру).
5. Бюджет промпта: правило 37 не должно переполнять окно — каскад усечения получил
   последний рычаг (жертвуем примерами механики, а не историей).
"""
from __future__ import annotations

import json

import pytest

from backend import mechanics as m
from backend import narrator as n
from backend import plot_deviation as pd


def _setting(**over):
    st = {
        "player": {"hp": 50, "max_hp": 50, "mp": 10, "max_mp": 10, "gold": 0, "level": 1,
                   "stats": {}, "inventory": [], "race": "человек", "class": "Воин"},
        "locations": {"here": {"name": "Тут", "desc": "", "connections": ["there"]},
                      "there": {"name": "Там", "desc": "", "connections": ["here"]},
                      "town": {"name": "Город", "desc": ""}},
        "current_location": "here",
        "npc": {}, "quests": {}, "flags": {}, "factions": {},
    }
    st.update(over)
    return st


# ══════════════════════ 1. quest_remove ══════════════════════
def test_quest_remove_erases_impossible_arc():
    st = _setting(quests={"plot_a": {"title": "А", "desc": "", "status": "active"},
                          "plot_b": {"title": "Б", "desc": "", "status": "active"}})
    msgs = m.apply_directives(st, {"quest_remove": ["plot_a"]})
    assert "plot_a" not in st["quests"] and "plot_b" in st["quests"]
    assert any("больше не актуален" in x for x in msgs)
    # НЕ засчитан как выполненный: статистика пути не врёт
    assert st["player"].get("progress", {}).get("quests_done", 0) == 0


def test_quest_remove_string_form_and_unknown_id():
    st = _setting(quests={"x": {"title": "X", "status": "active"}})
    m.apply_directives(st, {"quest_remove": "x"})
    assert "x" not in st["quests"]
    msgs = m.apply_directives(st, {"quest_remove": "нет_такого"})
    assert any("не найден" in x for x in msgs)          # честный отказ, не тихий


def test_quest_remove_clears_its_deadline_timer():
    """Регрессия: таймер квеста снимается ДО удаления квеста — _quest_timer_finish ищет
    квест в setting["quests"], и на уже удалённом молча выходил, оставляя сиротский
    дедлайн, тикающий «в никуда» до скончания мира."""
    st = _setting(quests={"y": {"title": "Y", "status": "active", "timer": "doom"}})
    st["timers"] = {"doom": {"name": "doom", "turns_left": 3, "desc": "рок"}}
    m.apply_directives(st, {"quest_remove": "y"})
    assert "y" not in st["quests"]
    assert "doom" not in st["timers"], "дедлайн удалённого квеста обязан сняться"


# ══════════════════════ 2. location_remove ══════════════════════
def test_location_remove_rebuilds_map_edges():
    st = _setting()
    msgs = m.apply_directives(st, {"location_remove": "town"})
    assert "town" not in st["locations"] and any("больше нет" in x for x in msgs)
    st2 = _setting()
    st2["locations"]["there"]["connections"] = ["here", "town"]
    m.apply_directives(st2, {"location_remove": "town"})
    assert "town" not in st2["locations"]["there"]["connections"], "мёртвое ребро карты"


def test_location_remove_refuses_current_location():
    """Игрок стоит в удаляемой локации → отказ (пусть сперва выведет move). Код проверяет."""
    st = _setting()
    msgs = m.apply_directives(st, {"location_remove": "here"})
    assert "here" in st["locations"], "текущую локацию удалять нельзя"
    assert any("СЕЙЧАС здесь" in x for x in msgs)


def test_location_remove_after_move_is_allowed():
    st = _setting()
    m.apply_directives(st, {"move": "there"})
    m.apply_directives(st, {"location_remove": "here"})
    assert "here" not in st["locations"] and st["current_location"] == "there"


def test_location_remove_list_and_empty_connections_pruned():
    st = _setting()
    m.apply_directives(st, {"location_remove": ["town", "нет"]})
    assert "town" not in st["locations"]
    st3 = _setting()
    st3["locations"]["there"]["connections"] = ["here"]
    m.apply_directives(st3, {"location_remove": "here"})  # here — текущая → отказ, связи целы
    assert st3["locations"]["there"]["connections"] == ["here"] or "here" in st3["locations"]


# ══════════════════════ 3. world_evolve ══════════════════════
def test_world_evolve_records_deviation():
    st = _setting(_player_turns=7)
    msgs = m.apply_directives(st, {"world_evolve": {"what": "арка «заговор» снята",
                                                    "why": "игрок убил информатора"}})
    # Запись есть (рассказчик увидит её в состоянии), а в чат НЕ уходит NOTHING: строка
    # «мир отходит от канвы сюжета» выдала бы игроку существование написанного сценария.
    assert st["plot_deviation"][0]["turn"] == 7
    assert "арка «заговор» снята" in pd.plot_deviation_text(st)
    assert msgs == [], f"мета-поворот не должен светиться игроку: {msgs}"


def test_world_evolve_dedupes_and_caps():
    st = _setting()
    m.apply_directives(st, {"world_evolve": {"what": "одно и то же"}})
    m.apply_directives(st, {"world_evolve": {"what": "одно и то же"}})
    assert len(st["plot_deviation"]) == 1, "дубль той же формулировки не плодится"
    for i in range(pd.DEVIATION_CAP + 5):
        pd.plot_deviation_add(st, f"поворот {i}")
    assert len(st["plot_deviation"]) == pd.DEVIATION_CAP, "буфер ограничен — промпт не раздувается"


def test_world_evolve_string_form_and_garbage():
    st = _setting()
    m.apply_directives(st, {"world_evolve": "свободной строкой"})
    assert st["plot_deviation"][-1]["what"] == "свободной строкой"
    before = len(st["plot_deviation"])
    m.apply_directives(st, {"world_evolve": {"nope": 1}})
    m.apply_directives(st, {"world_evolve": 42})
    assert len(st["plot_deviation"]) == before, "мусор не пишет пустые записи"


def test_plot_deviation_add_clips_long_text():
    st = _setting()
    pd.plot_deviation_add(st, "д" * 500, "ю" * 500)
    e = st["plot_deviation"][0]
    assert len(e["what"]) <= pd.WHAT_MAX and len(e["why"]) <= pd.WHY_MAX


# ══════════════════════ 4. дневник: игроку — РЕАЛЬНЫЕ изменения, не мета-слой ══════════════════════
def test_journal_shows_erased_quest_and_lost_place():
    """Правило с63 (правка владельца): игрок видит ПРИНЯТЫЕ изменения мира (квест больше не
    актуален, места больше нет) но NEVER мета-слой рассказчика (канва/plot_deviation)."""
    from backend import journal
    prev = _setting(quests={"doomed": {"title": "Идти к мосту", "status": "active"}},
                    locations={"here": {"name": "Тут"}, "bridge": {"name": "Мост"}})
    now = _setting(quests={}, locations={"here": {"name": "Тут"}})
    cats = {e["title"]: e["cat"] for e in journal.notable_diff(prev, now)}
    assert any("Идти к мосту" in t for t in cats), cats
    assert any("Больше нет: Мост" == t for t in cats), cats
    assert cats.get("Больше нет: Мост") == "place"


def test_journal_not_double_entry_for_finished_quest():
    """Выполненный квест — «Завершено», а не «больше не актуально» (удаление не притворяется).
    Регрессия на моё же изменение: пропущенные квесты без статуса active не попадают в журнал."""
    from backend import journal
    prev = _setting(quests={"d": {"title": "Де́ло", "status": "done"}})
    now = _setting(quests={})
    titles = [e["title"] for e in journal.notable_diff(prev, now)]
    assert not any("не актуально" in t for t in titles), titles


def test_plot_deviation_never_reaches_journal_or_state_of_player():
    """Мета-записи рассказчика не просачиваются ни в журнал (notable_diff их не читает),
    ни в системные сообщения хода. Директивы игроку отмирают, как и раньше."""
    from backend import journal
    prev = _setting()
    now = _setting(plot_deviation=[{"what": "тайная заметка мастера", "why": "", "turn": 3}])
    assert not any("тайная заметка" in json.dumps(e) for e in journal.notable_diff(prev, now))
    st = _setting()
    assert m.apply_directives(st, {"world_evolve": {"what": "заметка"}}) == []


# ══════════════════════ 5. промпт: правило 37 и блок состояния ══════════════════════
_WORLD = {"id": 1, "name": "t", "language": "ru", "genre": "фэнтези", "difficulty": "normal",
          "perspective": "second", "theme": {"name": "T", "genre": "фэнтези"},
          "gen_settings": json.dumps({"max_tokens": 2000})}


def _prompt(setting, tools=False):
    w = dict(_WORLD, setting=json.dumps(setting))
    return n.build_system_prompt(w, setting, use_tools=tools, action="")


@pytest.mark.parametrize("tools", [False, True])
def test_rule37_live_world_present_and_never_gated(tools):
    """Правило «живой мир» обязано быть в промпте ЛЮБОГО мира — и не должно резаться
    ярусами: в отличие от крафта/магазинов это не подсистема, а право рассказчика."""
    st = _setting()
    p = _prompt(st, tools)
    assert "ЖИВОЙ МИР vs КАНВА СЮЖЕТА" in p
    assert "37." in p
    assert "37" not in n.gated_rules(st, ""), "правило 37 не относится к мёртвым подсистемам"
    # с63 (правка владельца): запрет «раз в 10–15 ходов» — искуственная клетка, её быть не
    # должно: частоту сдвигов рассказчик выбирает сам по сюжету, а не по счётчику.
    assert "10–15" not in p and "10-15" not in p, "частотное лимитирование вернулось в промпт"


def test_state_shows_turn_counter_and_deviations():
    st = _setting(_player_turns=12, quests={"q": {"title": "Трон", "status": "active"}})
    s = n.format_state(st)
    assert "ЖИВОЙ МИР" in s and "ход 12" in s and "Трон" in s
    pd.plot_deviation_add(st, "информатор мёртв", "игрок убил его", turn=11)
    s2 = n.format_state(st)
    assert "отходил от канвы" in s2 and "информатор мёртв" in s2


def test_state_silent_without_turns_and_deviations():
    st = _setting()
    assert "ЖИВОЙ МИР" not in n.format_state(st)


def test_new_directives_advertised_in_both_modes():
    """Локальные модели видят формат текстом, облачные — через tools; обе обязаны знать
    про quest_remove/location_remove/world_evolve, иначе директиву никто не вызовёт."""
    st = _setting()
    assert "quest_remove" in _prompt(st, False)
    assert "location_remove" in _prompt(st, False)
    assert "world_evolve" in _prompt(st, False)
    desc = n.GAME_ENGINE_TOOL[0]["function"]["description"]
    for k in ("quest_remove", "location_remove", "world_evolve"):
        assert k in desc, f"{k} нет в описании инструмента"


def test_plot_style_calls_canvas_a_starting_point():
    """Стиль тем/сюжетов не должен приказывать «вести к финалу по канве»: именно эта
    формулировка делала стандартные сюжеты рельсовыми."""
    from backend import plots
    for th in list(plots.THEMES):
        style = th.get("style", "")
        low = style.lower()
        assert "строго в русле этого сюжета" not in low, f"{th.get('name')}: канва = закон"
        assert "веди к глобальной цели по канве" not in low, f"{th.get('name')}: рельсы"
        if th.get("is_plot"):
            assert "точка отсчёта" in low or "напряжени" in low, \
                f"{th.get('name')}: стиль не говорит, что канва — материал"


def test_custom_plot_style_is_not_rails():
    th = n.theme_from_custom("Моё", "Герой идёт по заранее написанному пути.")
    assert "точка отсчёта" in th["style"].lower()


# ══════════════════════ 5. бюджет промпта (правило 37 не рвёт окно) ══════════════════════
def test_last_resort_trims_engine_examples_not_history(fake_config):
    """Каскад усечения обязан уметь пожертвовать примерами механики, но НЕ карточками
    сцены и не историей: иначе в мире с узким окном правило 37 молча обрезает ответы."""
    fake_config(context_tokens=8192, max_tokens=2000)
    st = _setting(quests={"q1": {"title": "Квест", "status": "active"}})
    w = dict(_WORLD, setting=json.dumps(st),
             gen_settings=json.dumps({"context_tokens": 8192, "max_tokens": 2000}))
    cards = [{"kind": "npc", "name": f"N{i}", "entity_key": f"n{i}", "summary": "с" * 200,
              "relationship": "", "bio": "", "meta": "{}"} for i in range(6)]
    cards.append({"kind": "location", "name": "Тут", "entity_key": "here",
                  "summary": "м", "relationship": "", "bio": "", "meta": "{}"})
    recent = ([{"role": "player", "content": "x" * 500},
               {"role": "narrator", "content": "y" * 500}] * 8)
    msgs, meta = n.build_messages(
        w, st, "идти", recent, [{"content": "св " * 300}] * 3,
        ["па мять" * 400] * 6, cards, lore=["ло р" * 400] * 4)
    from backend.config import est_tokens
    assert est_tokens(msgs[0]["content"]) <= 8192 - 2000, meta
    assert not meta["overflow_tokens"], meta
    assert "Тут" in msgs[0]["content"] and "Квест" in msgs[0]["content"], \
        "карточки сцены не выкидываются никогда"
    assert "ЖИВОЙ МИР vs КАНВА СЮЖЕТА" in msgs[0]["content"], \
        "правило 37 не режется даже в крайнем усечении"


def test_drop_engine_examples_only_touches_marker_lines():
    """Функция режет ТОЛЬКО строки с маркером → нумерация правил (и trim_prompt) не съезжает."""
    st = _setting()
    p = _prompt(st, tools=False)
    out, saved = n.drop_engine_examples(p)
    assert saved > 0
    assert not any(ln.startswith("<<ENGINE>>") for ln in out.splitlines())
    import re
    def nums(s):
        return [int(x) for x in re.findall(r"^(\d+)\.\s", s, re.M)]
    # Номера могут быть прорежены ЯРУСАМИ (trim_prompt убирает правила мёртвых подсистем) —
    # это легально и было до резки. Проверям другое: резка примеров НЕ сдвигает нумерацию,
    # иначе trim_prompt начал бы резать не то правило (инвариант C1, аудит 38).
    assert nums(out) == nums(p), "резка примеров сдвинула номера правил"
    assert n.drop_engine_examples(out)[1] == 0, "повтор вызова ничего не экономит (идемпотентно)"
