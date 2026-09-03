"""Сессия 40, п.14 (мир «Новый мир»): «весь текст про туман, читать трудно».

Диагноз (по логу мира №103): дело НЕ в CSS (это починил п.4 этой же сессии) и НЕ в
персоне рассказчика («Летописец Системы» адекватен). Причина — ЗАЛИПШАЯ СРЕДА:

  * сюжет `raskolotye-nebesa` задаёт `starting_state.start_weather = "туман"`, и
    `apply_plot_start` ставит `setting["weather"]` ОДИН РАЗ при создании мира;
  * дальше погода двигается ТОЛЬКО директивой `weather` (`mechanics.WorldHandler`), а
    рассказчик за 40 ходов не дал её ни разу (`time` тоже не менял);
  * при этом `format_state` каждый ход совал в промпт «Погода: туман | …» + «Влияние
    среды: −2 к видимости, легко заблудиться» (правило 24 прямо велит «учитывать это в
    описаниях»), а стартовое вступление мира — «Туман стелется над руинами Аэтерны».

Итог: модель честно описывала туман в каждом ответе (7…9 упоминаний на 11 ответов
рассказчика, почти каждый ответ начинается с «Туман …»), и текст стал однотонным.

Лечение — сигналом, а не решением (закон 3: погоду не меняет код):
  * `WorldHandler` запоминает ход последней смены среды (`setting["_env_last_turn"]`);
  * `format_state`, если среда не двигалась ≥ 3 ходов, помечает это мастеру и напоминает
    про директивы weather/time. Правило 24 промпта при этом НЕ удлинялось — тест бюджета
    окна (`test_build_messages_never_overflows_window`) ловит любое пополнение промпта,
    поэтому сигнал живёт в строке состояния, которую модель читает и так каждый ход.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import mechanics, narrator  # noqa: E402


def _setting(**over) -> dict:
    st = copy.deepcopy(narrator.default_setting({"name": "t", "genre": "приключение"}))
    st["player"] = {"name": "Тест", "hp": 50, "max_hp": 50, "mp": 10, "max_mp": 10,
                    "level": 1, "xp": 0, "gold": 0, "race": "человек", "class": "Маг",
                    "profession": "", "stats": {}, "skills": {}, "inventory": [],
                    "effects": {}, "titles": [], "reputation": {}}
    st["current_location"] = "here"
    st["locations"] = {"here": {"name": "Здесь", "desc": "тут"}}
    st["weather"] = "туман"
    st["time"] = "вечер"
    st.update(over)
    return st


# ── 1. директивы среды запоминают ход ─────────────────────────────────
def test_weather_directive_records_last_env_turn():
    st = _setting(_player_turns=7)
    msgs = mechanics.apply_directives(st, {"weather": "ясно"})
    assert st["weather"] == "ясно"
    assert st["_env_last_turn"] == 7, "ход смены погоды обязан запомниться"
    assert st["_weather_last_turn"] == 7
    assert msgs == [] or isinstance(msgs, list)


def test_time_directive_also_counts_as_env_change():
    st = _setting(_player_turns=4)
    mechanics.apply_directives(st, {"time": "ночь"})
    assert st["_env_last_turn"] == 4
    assert st["_time_last_turn"] == 4, "п.14b: у времени свой счётчик (для авто-часов)"


def test_code_never_changes_weather_itself():
    """Закон 3: движок не решает за мастера — без директивы погода остаётся какой была
    (никакого авто-«рассеял туман» по счётчику ходов)."""
    st = _setting(_player_turns=99)
    mechanics.apply_directives(st, {"flag": {"name": "x", "value": True}})
    assert st["weather"] == "туман"
    assert "_env_last_turn" not in st, "без смены среды метку ставить нечем"


# ── 2. format_state предупреждает о залипшей среде ────────────────────
def test_format_state_warns_when_env_stale():
    st = _setting(_player_turns=10, _env_last_turn=2)
    txt = narrator.format_state(st)
    assert "Погода не менялась 8 ходов" in txt, txt
    assert "weather" in txt, "подсказка обязана назвать директиву"


def test_format_state_silent_while_env_fresh():
    st = _setting(_player_turns=10, _env_last_turn=9)
    assert "Погода не менялась" not in narrator.format_state(st)
    st2 = _setting(_player_turns=12, _env_last_turn=10)   # ровно 2 хода — ещё терпимо
    assert "Погода не менялась" not in narrator.format_state(st2)


def test_format_state_prefers_weather_marker_over_generic_env():
    """п.14b: время суток с авто-часами не должно «съедать» возраст погоды — счётчик
    погоды отдельный (`_weather_last_turn`), общий `_env_last_turn` остаётся фолбэком."""
    st = _setting(_player_turns=10, _env_last_turn=9, _time_last_turn=9, _weather_last_turn=1)
    assert "Погода не менялась 9 ходов" in narrator.format_state(st)


def test_format_state_silent_for_fresh_worlds():
    st = _setting()
    txt = narrator.format_state(st)
    assert "Погода: туман" in txt
    assert "Погода не менялась" not in txt


def test_format_state_survives_junk_markers():
    st = _setting(_player_turns="много", _env_last_turn=None)
    assert "Погода не менялась" not in narrator.format_state(st)


# ── 3. сигнал не ломает бюджет окна промпта ───────────────────────────
def test_env_warning_still_fits_prompt_window():
    import json as _json
    from backend.config import est_tokens

    st = _setting(_player_turns=40, _env_last_turn=1)   # ровно случай мира №103
    w = {"id": 1, "name": "t", "language": "ru", "genre": "тёмное фэнтези, литрпг",
         "difficulty": "normal", "perspective": "second", "theme": "",
         "setting": _json.dumps(st),
         "gen_settings": _json.dumps({"context_tokens": 8192, "max_tokens": 2000})}
    msgs, meta = narrator.build_messages(w, st, "идти дальше", [], [], [], None)
    assert "Погода не менялась" in msgs[0]["content"]
    assert est_tokens(msgs[0]["content"]) <= 8192 - 2000, meta


# ── 4. режим «Мастер»: правка погоды гасит предупреждение ────────────
def test_master_patch_resets_env_age(api_client):
    client, _ = api_client
    from backend import db as _db
    wid = client.post("/api/worlds", json={"theme_id": "raskolotye-nebesa",
                                          "name": "L"}).json()["world_id"]
    st = _db.get_world(wid)["setting"]
    import json as _json
    st = _json.loads(st) if isinstance(st, str) else st
    st["_player_turns"] = 20
    st["_env_last_turn"] = 1
    _db.update_world(wid, setting=st)
    r = client.post(f"/api/worlds/{wid}/state/patch", json={"patch": {"weather": "ясно"}})
    assert r.status_code == 200
    new = r.json()["setting"]
    assert new["weather"] == "ясно"
    assert new["_env_last_turn"] == 20, "правка мастером — тоже смена среды"
    assert new["_weather_last_turn"] == 20


# ── 5. п.14b: авто-часы мира (время суток больше не «вечер навсегда») ────
def test_auto_time_advances_cycle():
    st = _setting(time="вечер", _player_turns=4, _time_anchor_turn=0)
    msgs = mechanics.tick_time(st, every=4)
    assert st["time"] == "ночь", msgs
    assert msgs and "Часы мира" in msgs[0] and "вечер → ночь" in msgs[0]


def test_auto_time_wraps_to_morning():
    st = _setting(time="ночь", _player_turns=8, _time_anchor_turn=4)
    mechanics.tick_time(st, every=4)
    assert st["time"] == "утро"
    # полный цикл возвращается в исходную точку
    for _ in range(3):
        st["_player_turns"] += 4
        mechanics.tick_time(st, every=4)
    assert st["time"] == "ночь"


def test_auto_time_first_call_anchors_silently():
    """Свежий мир не должен терять вечер на первом же ходу (и молча менять время)."""
    st = _setting(time="вечер", _player_turns=1)
    assert mechanics.tick_time(st, every=4) == []
    assert st["time"] == "вечер" and st["_time_anchor_turn"] == 1


def test_auto_time_respects_step():
    st = _setting(time="вечер", _player_turns=3, _time_anchor_turn=0)
    assert mechanics.tick_time(st, every=4) == []
    assert st["time"] == "вечер"


def test_auto_time_never_touches_weather():
    """закон 3: авто-часы ведут КАЛЕНДАРЬ, а не погоду — «рассеять туман» решает мастер."""
    st = _setting(time="день", weather="туман", _player_turns=40, _time_anchor_turn=0)
    mechanics.tick_time(st, every=4)
    assert st["weather"] == "туман"


def test_auto_time_leaves_custom_times_alone():
    """Жанр не вшит (закон 1): своё/авторское время суток («три часа после полудня»,
    «time of gods») авто-часы не переписывают и не ломят."""
    for custom in ("три склянки", "полнолуние", ""):
        st = _setting(time=custom, _player_turns=50, _time_anchor_turn=0)
        assert mechanics.tick_time(st, every=4) == []
        assert st["time"] == custom


def test_auto_time_switch_and_junk_step_are_noops():
    st = _setting(time="вечер", _player_turns=99, _time_anchor_turn=0)
    assert mechanics.tick_time(st, every=0) == []
    assert st["time"] == "вечер", "every=0 = не двигать время вовсе"
    # невалидный шаг = база (4), а 99 ходов с якоря 0 — срок давно пришёл
    assert mechanics.tick_time(st, every="нет") != []
    assert st["time"] == "ночь"


def test_master_time_patch_reseeds_auto_clock(api_client):
    """Мастер поставил «ночь» — авто-часы обязаны стартовать от этой правки, а не
    догонять старые счётчики (иначе «ночь» сменилась бы сразу на «утро»)."""
    client, _ = api_client
    from backend import db as _db
    import json as _json
    wid = client.post("/api/worlds", json={"theme_id": "raskolotye-nebesa",
                                          "name": "L"}).json()["world_id"]
    st = _db.get_world(wid)["setting"]
    st = _json.loads(st) if isinstance(st, str) else st
    st["_player_turns"] = 30
    st["_time_anchor_turn"] = 0
    _db.update_world(wid, setting=st)
    r = client.post(f"/api/worlds/{wid}/state/patch", json={"patch": {"time": "ночь"}})
    new = r.json()["setting"]
    assert new["time"] == "ночь"
    assert new["_time_anchor_turn"] == 30
    assert mechanics.tick_time(new, every=4) == [] and new["time"] == "ночь"


# ── 6. п.15: тик эффектов виден каждый ход, а не только когда льётся урон ──
def test_timed_effect_reports_remaining_turns():
    """Симптом: «Стабилизация узора (3 хода)» висит, а игрок не видит ни убыли, ни
    обратного отсчёта — «эффекты не применяются» неотличимо от «эффекты тикают молча».
    Теперь остаток показывается системкой каждый ход."""
    st = _setting()
    st["player"]["effects"] = {"Дурман": {"turns": 3, "kind": "состояние",
                                          "damage": 0, "desc": "голова тяжёлая"}}
    msgs = mechanics.tick_effects(st)
    assert any("Дурман" in m and "осталось 2" in m for m in msgs), msgs
    # счётчик реально идёт (движок и раньше снимал эффект по turns — это не менялось)
    mechanics.tick_effects(st)
    msgs3 = mechanics.tick_effects(st)
    assert any("закончился" in m for m in msgs3), msgs3
    assert "Дурман" not in st["player"]["effects"]


def test_permanent_effect_stays_silent():
    """Постоянные эффекты (turns=-1) не должны плодить пустые системки каждый ход."""
    st = _setting()
    st["player"]["effects"] = {"Резонанс Порядка": {"turns": -1, "kind": "особый",
                                                  "desc": "+5 к MP"}}
    assert mechanics.tick_effects(st) == []


# ── 7. п.16: «Механика применена.» — последний рубеж, а не дефолт ──────
def test_mech_fallback_prose_is_not_a_stub():
    """Если модель так и не вернула текст — игрок получает пересказ хода по системкам,
    а не служебную заглушку (иначе переход/находка остались несказанными)."""
    from backend.routers.core import _mech_fallback_prose
    out = _mech_fallback_prose(["🧭 Переход: Тропа к Форпосту",
                               "✨ Эффект «Яд» (3 хода) наложен."],
                               ["⏳ Эффект «Лихорадка»: −30 HP → 86/156"])
    assert "Механика применена" not in out
    assert "Переход: Тропа к Форпосту" in out and "Яд»" in out and "Лихорадка" in out
    assert _mech_fallback_prose([], []) == "", "нечего пересказывать — не выдумываем"


def test_mech_prose_retries_and_falls_back(monkeypatch):
    """Пустой ответ модели → повтор (не сразу заглушка), а когда не помогло — текст по фактам."""
    import asyncio
    from backend import llm
    from backend.routers import core
    calls = {"n": 0}

    async def fake_complete(msgs, **kw):
        calls["n"] += 1
        return "" if calls["n"] < 2 else "Ты выходишь на тропу."

    monkeypatch.setattr(llm, "complete", fake_complete)
    monkeypatch.setattr(core.narrator, "build_system_prompt", lambda *a, **k: "система")
    world = {"id": 1, "name": "t", "language": "ru", "genre": "литрпг", "difficulty": "normal",
             "perspective": "second", "theme": "", "setting": "{}",
             "gen_settings": json.dumps({"context_tokens": 8192, "max_tokens": 900})}
    st = _setting()
    out = asyncio.run(core._narrate_mech_outcome(world, st, None, {"main": {}},
                                                ["Переход: Тропа"], "идти"))
    assert out == "Ты выходишь на тропу." and calls["n"] == 2

    # модель упрямо молчит → детерминированный пересказ, а не «Механика применена»
    async def mute(msgs, **kw):
        return ""
    monkeypatch.setattr(llm, "complete", mute)
    out2 = asyncio.run(core._narrate_mech_outcome(world, st, None, {"main": {}},
                                                 ["Переход: Тропа"], "идти"))
    assert "Переход: Тропа" in out2 and "Механика применена" not in out2


def test_engine_marker_stripped_from_mech_prose(monkeypatch):
    """Дописывающий проход не имеет права вернуть новую механику (директивы уже применены)."""
    import asyncio
    from backend import llm
    from backend.routers import core

    async def leaky(msgs, **kw):
        return "Ты идёшь. <<ENGINE>>{\"player\": {\"gold\": 10}}"
    monkeypatch.setattr(llm, "complete", leaky)
    monkeypatch.setattr(core.narrator, "build_system_prompt", lambda *a, **k: "система")
    world = {"id": 1, "name": "t", "language": "ru", "genre": "литрпг", "difficulty": "normal",
             "perspective": "second", "theme": "", "setting": "{}",
             "gen_settings": json.dumps({"context_tokens": 8192, "max_tokens": 900})}
    out = asyncio.run(core._narrate_mech_outcome(world, _setting(), None, {"main": {}},
                                                ["Переход"], "идти"))
    assert "ENGINE" not in out and "gold" not in out and out.startswith("Ты идёшь")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
