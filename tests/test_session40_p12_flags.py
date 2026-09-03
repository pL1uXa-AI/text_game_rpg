# -*- coding: utf-8 -*-
"""Сессия 40, п.12 (мир «Новый мир»): у флагов есть человекочитаемое название.

Симптом: во вкладке «Состояние» и в дневнике — «Player awakened: да», «Core sealed: да»,
в модалке ключа `player_awakened`. Флаг — факт мира, а игрок читает машинный id:
тот же класс дефекта, что п.9 (карточки) и п.11 (статистика), но слой — флаги.

Причина НЕ во фронте: `flagLabel` честно перебивает известные ключи словарём
`FLAG_LABELS`, но он разово захардкод под один сюжет, а рассказчик и файл сюжета
имена не задают вовсе — у нового флага всегда остаётся «id со звёздочками».

Правка (законы 2/3): движок НЕ переименовывает ключи (на машинные id опираются
судья логики, `flag`-директивы, `station:`-флаги крафта и старые сохранения) и НЕ
выдумывает имена — название даёт мастер: директива `flag {name, value, title}`
хранится в `setting["flag_titles"]` (тот же приём, что `desc` у эффектов), оттуда его
читают UI, дневник и промпт. Сюжет может задать его тем же полем.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
sys.path.insert(0, str(ROOT / "scripts"))

from backend.narrator import (  # noqa: E402
    THEMES, apply_directives, default_setting, format_state,
)


def _st(**flags) -> dict:
    s = default_setting(THEMES[0], "normal")
    for k, v in flags.items():
        s["flags"][k] = v
    return s


# ── движок: title хранится отдельно от значения ───────────────────────

def test_flag_directive_keeps_value_and_stores_title():
    s = _st()
    msgs = apply_directives(s, {"flag": {"name": "door_open", "value": True,
                                         "title": "Дверь в склепах открыта"}})
    assert s["flags"]["door_open"] is True, "значение флага — как и раньше (судья/station:)"
    assert s["flag_titles"]["door_open"] == "Дверь в склепах открыта"
    assert msgs == [] or all(isinstance(m, str) for m in msgs)


def test_flag_title_re_written_but_blank_keeps_previous():
    s = _st()
    apply_directives(s, {"flag": {"name": "f1", "value": True, "title": "Первое"}})
    apply_directives(s, {"flag": {"name": "f1", "value": False, "title": "Второе"}})
    assert s["flag_titles"]["f1"] == "Второе"
    apply_directives(s, {"flag": {"name": "f1", "value": True}})
    assert s["flag_titles"]["f1"] == "Второе", "флаг без title не обязан терять прежнее имя"
    assert s["flags"]["f1"] is True


def test_flag_title_is_capped():
    s = _st()
    apply_directives(s, {"flag": {"name": "f1", "value": True, "title": "м" * 900}})
    assert len(s["flag_titles"]["f1"]) <= 120


def test_flag_without_title_untouched():
    s = _st()
    apply_directives(s, {"flag": {"name": "bare", "value": True}})
    assert s["flag_titles"] == {}
    assert "bare=True" in format_state(s)


# ── промпт: мастер видит и ключ, и имя (иначе не узнает, что называть) ──

def test_format_state_shows_flag_key_and_title():
    s = _st(player_awakened=True)
    s["flag_titles"] = {"player_awakened": "Пробуждение случилось"}
    out = format_state(s)
    assert "player_awakened" in out, "машинный ключ обязан остаться: по нему флаг правится"
    assert "Пробуждение случилось" in out


def test_judge_and_audit_see_titles_too():
    """Судья/аудит читают флаги строкой `_flag_line` — там же и имя, и ключ."""
    from backend.narrator import _flag_line
    line = _flag_line({"door_open": True}, {"door_open": "Дверь открыта"})
    assert line == "door_open=True (Дверь открыта)"
    assert _flag_line({"x": False}, {}) == "x=False"
    assert _flag_line({"x": True}, "муср") == "x=True"   # не dict → просто не мешаем


def test_plot_flag_may_be_object_with_title():
    """Сюжет (PLOTS.md) задаёт флаг и так: {"value": true, "title": "…"}."""
    from backend.narrator import apply_plot_start
    s = _st()
    apply_plot_start(s, {"starting_state": {"flags": {"core_sealed":
                                                      {"value": True, "title": "Сердце запечатано"}}},
                         "story": {}})
    assert s["flags"]["core_sealed"] is True
    assert s["flag_titles"]["core_sealed"] == "Сердце запечатано"
    # прежняя форма (ключ: true) обязана работать как раньше
    s2 = _st()
    apply_plot_start(s2, {"starting_state": {"flags": {"old_style": True}}, "story": {}})
    assert s2["flags"]["old_style"] is True and s2["flag_titles"] == {}


def test_journal_entry_uses_flag_title():
    """Дневник (он не LLM) больше не печатает «Так в мире и осталось: player_awakened»."""
    from backend.journal import notable_diff
    prev, now = _st(), _st(player_awakened=True)
    now["flag_titles"] = {"player_awakened": "Первое пробуждение"}
    got = [e for e in notable_diff(prev, now) if e["cat"] == "flag"]
    assert got and "Первое пробуждение" in got[0]["title"], got


# ── промпт/инструмент: title описан (иначе модель его не даст) ─────────

def test_prompt_and_tool_advertise_flag_title():
    src = (ROOT / "backend" / "narrator.py").read_text(encoding="utf-8")
    rule = re.search(r"^17\. Флаги.*$", src, re.M)
    assert rule, "правило 17 о флагах исчезло из промпта"
    assert "title" in rule.group(0), f"правило 17 не просит человекочитаемое имя:\n{rule.group(0)}"
    assert "flag {name,value,title" in src, "описание инструмента/аудита не знает про title"


# ── само-исцеление уже созданных миров (п.9 той же сессии — тот же приём) ──

def test_repair_flag_titles_from_source_plot():
    """Мир, созданный из сюжета ДО правки, получает имена флагов при старте сервера."""
    from backend.flag_titles import repair_flag_titles
    st = {"flags": {"player_awakened": True, "core_sealed": True, "custom": True},
          "_theme_snapshot": {"id": "raskolotye-nebesa"}}
    assert repair_flag_titles(st) == 2, "подписываются только флаги, имя которых сюжет знает"
    assert st["flag_titles"]["player_awakened"]
    assert "custom" not in st["flag_titles"], "выдумывать имя чужому флагу нечем — нечего и менять"
    assert st["flags"]["player_awakened"] is True, "значения флагов не трогаются"
    # идемпотентность
    assert repair_flag_titles(st) == 0


def test_repair_flag_titles_never_breaks():
    from backend.flag_titles import repair_flag_titles
    assert repair_flag_titles(None) == 0
    assert repair_flag_titles({}) == 0
    assert repair_flag_titles({"flags": {"a": True}}) == 0        # нет снапшота сюжета
    assert repair_flag_titles({"flags": {"a": True},
                               "_theme_snapshot": {"id": "нет-такого"}}) == 0
    # имя, данное мастером, сюжетом не перетирается
    st = {"flags": {"player_awakened": True}, "flag_titles": {"player_awakened": "Моё имя"},
          "_theme_snapshot": {"id": "raskolotye-nebesa"}}
    assert repair_flag_titles(st) == 0 and st["flag_titles"]["player_awakened"] == "Моё имя"


def test_flag_titles_editable_in_master_mode():
    """flag_titles в белом списке режима «Мастер» (правка состояния руками)."""
    src = (ROOT / "backend" / "routers" / "worlds.py").read_text(encoding="utf-8")
    block = src[src.index('async def patch_state'):src.index('async def patch_state') + 900]
    assert '"flag_titles"' in block, "режим мастера не может поправить название флага"


# ── фронтенд: имя берётся из состояния мира ───────────────────────────

def _fn_src(name: str) -> str:
    from check_frontend import _extract_fn
    body = _extract_fn(APP_JS, name)
    if f"function {name}(" not in body:
        raise AssertionError(f"{name} не извлечена")
    return body


def test_ui_falls_back_to_state_titles_first():
    fn = _fn_src("flagLabel")
    assert "flag_titles" in fn, "flagLabel не смотрит в состояние мира (мастерские имена)"
    assert "FLAG_LABELS" in fn


def test_flag_label_real_js():
    if subprocess.run(["node", "--version"], capture_output=True, text=True).returncode != 0:
        pytest.skip("node недоступен")
    labels = re.search(r"^const FLAG_LABELS = \{.*?^\};$", APP_JS, re.S | re.M)
    assert labels, "в app.js не найден словарь FLAG_LABELS"
    snippet = (labels.group(0) + "\n" + _fn_src("flagLabel") + "\n" + _fn_src("ucfirst") + """
const state = { setting: { flags: { player_awakened: true, core_sealed: true, met_x: true },
                           flag_titles: { player_awakened: "Пробуждение случилось" } } };
const out = [
  flagLabel("player_awakened"),        // имя от мастера
  flagLabel("met_the_librarian"),      // имя из словаря известных ключей
  flagLabel("core_sealed"),            // нет имени → читабельный fallback
  flagLabel("door_open"),              // словарь известных ключей
];
console.log(JSON.stringify(out));
""")
    tmp = ROOT / "_s40_p12_probe.cjs"
    tmp.write_text(snippet, encoding="utf-8")
    try:
        r = subprocess.run(["node", str(tmp)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
    finally:
        tmp.unlink(missing_ok=True)
    out = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 0, out[:800]
    got = json.loads(out.strip().splitlines()[-1])
    assert got[0] == "Пробуждение случилось", f"имя мастера не победить: {got}"
    assert got[1] == "Встреча с хранителем", got
    assert got[3] == "Дверь открыта", got
    assert "core_sealed" != got[2] and "_" not in got[2], f"машинный id вернулся игроку: {got[2]!r}"


def test_no_raw_key_in_state_pill_when_titled():
    """Пилюля в «Состоянии» рисует flagLabel, а сырой ключ остаётся только в title/модалке."""
    row = next(ln for ln in APP_JS.splitlines() if "flag-pill" in ln)
    assert "flagLabel(k)" in row, row
