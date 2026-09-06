# -*- coding: utf-8 -*-
"""Сессия 70 — D8 (аудит 41): директивы мира `flag` / `time` / `weather` показываются игроку.

Было: `mechanics.WorldHandler.apply` возвращал `[]` — то есть ход, в котором рассказчик
передвинул часы («ночь»), поднял грозу или зафиксировал сюжетный факт флагом, не давал в
чате НИЧЕГО. Изменённое состояние игрок мог заметить только по сайдбару, а системки у
остальных звеньев цепочки (урон, предметы, квесты, эпохи, фракции) — есть.

Почему это баг, а не осознанное молчание (ср. с63, где «🧭 ЖИВОЙ МИР» убрали из чата
намеренно): показ мета-знания выдаёт заранее написанную канву, а факт «наступила ночь» —
это ровно то, что игрок и так читает в описании. Закон 2 прямо разрешает инфраструктуру
отображения, закон 3 не нарушен: движок ничего не оценивает и не отказывает — он озвучивает
записанное мастером значение (значение в состоянии остаётся дословно его).

Что НЕ дублируем:
* авто-часы `tick_time` пишут «🕐 Часы мира: вечер → ночь» в начале хода — директива с тем
  же значением молчит (сравнение в lower/strip), иначе один ход давал бы две строки о ночи;
* флаг, значение которого не поменялось (модель страхуется и повторяет директиву ход за
  ходом), и служебные `station:`-флаги крафта (машинный ключ игроку показывать нельзя —
  тот же дефект, что п.12 сессии 40 чинил в названиях флагов).
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MECHANICS = ROOT / "backend" / "mechanics.py"

from backend import mechanics  # noqa: E402
from backend.narrator import apply_directives, default_setting  # noqa: E402


def _st(**over) -> dict:
    s = default_setting({"name": "t", "genre": "приключение"}, "normal")
    s["time"] = "вечер"
    s["weather"] = "туман"
    s.update(over)
    return s


# ═════════════ 1. сам показ (то, чего не было) ═════════════

def test_time_directive_announces_itself():
    msgs = apply_directives(_st(), {"time": "ночь"})
    assert msgs == ["🕐 Время: ночь."], msgs


def test_weather_directive_announces_itself():
    msgs = apply_directives(_st(), {"weather": "гроза"})
    assert msgs == ["🌦 Погода: гроза."], msgs


def test_flag_directive_announces_with_human_title():
    """Название из п.12 (flag_titles) идёт в чат, машинный ключ — только если имени нет."""
    msgs = apply_directives(_st(), {"flag": {"name": "door_open", "value": True,
                                             "title": "Дверь в склепах открыта"}})
    assert msgs == ["🚩 Дверь в склепах открыта — да."], msgs
    msgs2 = apply_directives(_st(), {"flag": {"name": "ритуал_кончен", "value": False}})
    assert msgs2 == ["🚩 ритуал_кончен — нет."], msgs2


def test_flag_string_value_is_shown_as_a_fact():
    """Флаг-строка («ключ у мельника») — легальная форма; в чат уходит она, а не «да»."""
    msgs = apply_directives(_st(), {"flag": {"name": "note", "value": "ключ у мельника"}})
    assert msgs == ["🚩 note — ключ у мельника."], msgs


def test_value_false_words_read_as_no():
    s = _st()
    msgs = apply_directives(s, {"flag": {"name": "f", "value": "false"}})
    assert msgs == ["🚩 f — нет."], msgs
    assert s["flags"]["f"] == "false", "значение в состоянии не переписывается (его читает судья)"


def test_order_flags_then_time_then_weather():
    msgs = apply_directives(_st(), {"flag": {"name": "ф", "value": True},
                                    "time": "ночь", "weather": "ливень"})
    assert [m[0] for m in msgs] == ["🚩", "🕐", "🌦"], msgs


# ═════════════ 2. закон 3: показ ≠ решение ═════════════

def test_state_values_stay_verbatim_while_shown_lowercased():
    """Сравнение — в lower/strip, запись — дословно: мастер пишет как хочет."""
    s = _st()
    apply_directives(s, {"time": "  НОЧЬ ", "weather": "Кровавый туман"})
    assert s["time"] == "  НОЧЬ ", s["time"]
    assert s["weather"] == "Кровавый туман"
    msgs = apply_directives(_st(), {"weather": "Кровавый туман"})
    assert msgs == ["🌦 Погода: Кровавый туман."], msgs


def test_directive_never_refused_because_of_the_new_showing():
    """Ни одной отказной ветки не появилось: любое значение проходит в состояние."""
    s = _st()
    apply_directives(s, {"time": "Между собака лапами и петух не прокрил",
                         "weather": "пепел"})
    assert s["time"].startswith("Между") and s["weather"] == "пепел"


def test_env_turn_markers_still_written():
    """п.14 (с40): счётчики возраста среды нужны format_state — показ их не отменил."""
    s = _st(_player_turns=6)
    apply_directives(s, {"time": "ночь", "weather": "ясно"})
    assert s["_env_last_turn"] == 6 and s["_time_last_turn"] == 6 and s["_weather_last_turn"] == 6


def test_game_over_unchanged_and_silent_here():
    """Финал озвучивают фронт и финальный чек apply_directives — звено мира молчит."""
    s = _st()
    assert apply_directives(s, {"game_over": True}) == []
    assert s["game_over"] is True


# ═════════════ 3. молчание, когда показывать нечего ═════════════

@pytest.mark.parametrize("same", ["ночь", "Ночь", " ночь "])
def test_repeated_time_is_silent(same):
    s = _st()
    apply_directives(s, {"time": "ночь"})
    assert apply_directives(s, {"time": same}) == [], "тот же час повторно — без дубля"


def test_repeated_weather_is_silent():
    s = _st()
    apply_directives(s, {"weather": "гроза"})
    assert apply_directives(s, {"weather": "Гроза"}) == []


def test_empty_time_or_weather_values_are_silent():
    """`{"time": ""}`/`{"time": null}` — нормализованный мусор: пишем как раньше, не кричим."""
    s = _st()
    assert apply_directives(s, {"time": ""}) == []
    assert apply_directives(s, {"weather": None}) == []


def test_no_duplicate_of_autoclock_messages():
    """Авто-часы (начало хода) уже сказали «ночь» — директива того же часа молчит:
    в ходе игрока будет ОДНА строка про время, а не две."""
    s = _st(time="вечер", _player_turns=8, _time_anchor_turn=4)
    ticked = mechanics.tick_time(s)               # ровно то, что делает роут в начале хода
    assert ticked and s["time"] == "ночь", ticked
    assert apply_directives(s, {"time": "ночь"}) == []
    # а если мастер ОТМЕНИЛ автомат — смену видим
    assert apply_directives(s, {"time": "утро"}) == ["🕐 Время: утро."]


def test_repeated_flag_with_equivalent_value_is_silent():
    """True и "true" — один факт: модель повторяет директиву из хода в ход, спам не нужен."""
    s = _st()
    assert apply_directives(s, {"flag": {"name": "х", "value": True}}) == ["🚩 х — да."]
    assert apply_directives(s, {"flag": {"name": "х", "value": "true"}}) == []
    assert apply_directives(s, {"flag": {"name": "х", "value": 1}}) == []
    # а смена факта — видна
    assert apply_directives(s, {"flag": {"name": "х", "value": False}}) == ["🚩 х — нет."]


def test_station_flag_stays_out_of_chat():
    """`station:<имя>` — служебный флаг крафта (машинный ключ): его игрок читает как
    «📕 Станции здесь», а не как сюжетный факт."""
    s = _st()
    assert apply_directives(s, {"flag": {"name": "station:кузня", "value": True}}) == []
    assert mechanics.has_station(s, "кузня"), "флаг по-прежнему работает как станция"


def test_blank_flag_name_still_ignored():
    s = _st()
    assert apply_directives(s, {"flag": {"name": "  ", "value": True}}) == []
    assert s["flags"] == {}


# ═════════════ 4. через API: сообщение доходит до чата ═════════════

def _turn(api_client, engine: str, text: str = "осмотреться", wid: int | None = None):
    """Один ход с ENGINE-директивами; возвращает (world_id, системки хода, ответ)."""
    client, holder = api_client
    if wid is None:
        wid = client.post("/api/worlds", json={"theme_id": "raskolotye-nebesa", "name": "D8"}
                          ).json()["world_id"]
    holder["reply"] = f"Ты озираешься. <<ENGINE>>{engine}"
    r = client.post(f"/api/worlds/{wid}/action", json={"text": text})
    assert r.status_code == 200, r.text
    body = r.json()
    return wid, [e["content"] for e in body["events"] if e["role"] == "system"], body


def test_turn_with_time_directive_reaches_chat(api_client):
    _, sysmsgs, body = _turn(api_client, '{"time": "ночь", "weather": "гроза"}')
    assert any(m.startswith("🕐 Время: ночь") for m in sysmsgs), sysmsgs
    assert any(m.startswith("🌦 Погода: гроза") for m in sysmsgs), sysmsgs
    st = body["state"]
    assert st["time"] == "ночь" and st["weather"] == "гроза"


def test_second_turn_with_same_time_does_not_repeat(api_client):
    wid, first, _ = _turn(api_client, '{"time": "ночь"}')
    assert sum(m.startswith("🕐") for m in first) == 1, first
    # тот же ход вторично: авто-часы ещё не сдвинулись (4 хода на часть суток),
    # директива с тем же значением — обязана молчать
    _, again, _ = _turn(api_client, '{"time": "ночь"}', text="идти дальше", wid=wid)
    assert not any(m.startswith("🕐") for m in again), again
    _, third, _ = _turn(api_client, '{"time": "утро"}', text="ждать рассвета", wid=wid)
    assert any(m == "🕐 Время: утро." for m in third), third

def test_flag_and_station_mixed_in_one_turn(api_client):
    _, sysmsgs, body = _turn(api_client,
                             '{"flag": {"name": "station:кузня", "value": true},'
                             ' "time": "ночь"}')
    assert any(m.startswith("🕐") for m in sysmsgs), sysmsgs
    assert not any("station:" in m for m in sysmsgs), sysmsgs
    assert body["state"]["flags"]["station:кузня"] is True


def test_junk_directives_do_not_crash_the_turn(api_client):
    """Пустая/нулевая среда в директивах — ход не падает и не кричит в чат."""
    _, sysmsgs, _ = _turn(api_client, '{"time": null, "weather": "", "flag": {"name": ""}}')
    assert not any(m.startswith(("🕐", "🌦", "🚩")) for m in sysmsgs), sysmsgs


# ═════════════ 5. сторожа исходника ═════════════

def test_world_handler_no_longer_returns_empty_list():
    src = io.open(MECHANICS, encoding="utf-8").read()
    body = src[src.index("class WorldHandler"):]
    body = body[:body.index("class FactionHandler")]
    assert "return []" not in body, "звено мира снова молчит (D8)"
    assert "🕐" in body and "🌦" in body and "🚩" in body


def test_doc_lists_env_showing():
    """AGENT.md обязан описывать новые системки (иначе следующая сессия решит, что их нет)."""
    doc = (ROOT / "AGENT.md").read_text(encoding="utf-8")
    seg = [ln for ln in doc.splitlines() if "`flag: {name, value}`" in ln]
    assert seg, "строка директив мира исчезла из AGENT.md"
    assert "🕐" in seg[0] and "🌦" in seg[0] and "🚩" in seg[0], seg[0]
    assert "tick_time" in seg[0], "не сказано, что с авто-часами показ не дублируется"


def test_no_stray_words_in_new_text():
    """Комментарий правки — без опечаток/латинских склеек (проверка на конкретном фрагменте)."""
    src = io.open(MECHANICS, encoding="utf-8").read()
    i = src.index("class WorldHandler")
    frag = src[i:i + 2000]
    assert not re.search(r"\b[а-яё][a-z]{2,}|\b[a-z]{2,}[а-яё]\b",
                         " ".join(re.findall(r"#\s*(.+)", frag))), "латинь в кириллице"
