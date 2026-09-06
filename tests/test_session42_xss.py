# -*- coding: utf-8 -*-
"""A2 (аудит 41): XSS во фронте — модельные тексты не должны становиться разметкой.

Два слоя защиты:
  1) структурный — `scripts/check_frontend.py::xss_check` (в HTML-шаблоне нет сырых
     интерполяций полей .desc/.name/.time/.weather/…), вызывается и из CI, и отсюда;
  2) поведенческий — реальные `esc/trunc/kv/charRow/flagWord` на Node-полигоне чекера и
     НАСТОЯЩИЙ `renderSetting()` из app.js на DOM-заглушке (`scripts/xss_probe.js`).

Почему нельзя «просто перевести всё на textContent»: карточки состояния — это разметка
(`<small>`, `<em class="val">`, вложенные списки), её правка убила бы UI. Законы 2/3 не
затронуты: меняется только показ.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from backend import narrator as narrator_mod  # noqa: E402  (нужен id темы для api-теста)

THEME_ID = narrator_mod.THEMES[0]["id"]

APP_JS = ROOT / "frontend" / "app.js"
PROBE = ROOT / "scripts" / "xss_probe.js"

# Грязь, которую writes LLM (описание предмета, `time` директивой мастера, mood NPC)
# или которая приходит из импортированного дампа мира.
DIRTY = '<img src=x onerror=alert(1)>'


def _node():
    node = shutil.which("node") or shutil.which("node.exe")
    if not node:
        pytest.skip("node недоступен")
    return node


# ── 1. помощники экранируют ────────────────────────────────────────────

def test_check_frontend_a2_rules_green():
    """Структурные проверки A2 в чекере фронта зелёные (и сырых вставок = 0)."""
    import check_frontend as cf
    js = APP_JS.read_text(encoding="utf-8")
    assert cf.xss_check(js) == []
    assert cf.xss_behavior_check(js) == []
    # не сломать инвариант E1 (атрибуты без JS-литералов)
    assert cf.dataclick_check(js) == []


def test_trunc_escapes_and_keeps_word_boundary():
    """trunc() обязан экранировать (был главным дырявым помощником) и не менять обрезку."""
    _node()
    import check_frontend as cf
    js = APP_JS.read_text(encoding="utf-8")
    snippet = (cf._extract_fn(js, "esc") + "\n" + cf._extract_fn(js, "trunc") + "\n"
               + "console.log(JSON.stringify([trunc(%s, 200), trunc(%s, 8), trunc(%s, 6)]));\n"
               % (json.dumps(DIRTY, ensure_ascii=False), json.dumps(DIRTY, ensure_ascii=False),
                  json.dumps("абвгдежз ик лм", ensure_ascii=False)))
    with tempfile.NamedTemporaryFile("w", suffix=".cjs", delete=False, encoding="utf-8") as fh:
        fh.write(snippet)
        tmp = fh.name
    try:
        r = subprocess.run([_node(), tmp], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
    finally:
        Path(tmp).unlink(missing_ok=True)
    assert r.returncode == 0, r.stderr
    long_, short_, words = json.loads(r.stdout.strip())
    assert "<img" not in long_ and "&lt;img" in long_, long_
    assert "onerror" not in short_.lower() or "&lt;" in short_, short_
    assert words == "абвгд…", f"обрезка по слову поехала: {words}"


# ── 2. сам рендер сайдбара ─────────────────────────────────────────────

def _dirty_setting() -> dict:
    d = DIRTY
    return {
        # время/погоду пишет мастер директивой time/weather — ровно тот вектор, что в отчёте
        "time": f'ночь">{d}', "weather": d, "current_location": "start",
        "locations": {"start": {"name": "Таверна", "desc": d, "connections": [], "stations": []}},
        "player": {
            "hp": 10, "max_hp": 20, "mp": 1, "max_mp": 2, "level": 1, "xp": 0, "gold": 5,
            "name": "Я", "identity": "био", "profession": "вор", "stats": {"сила": 10},
            "inventory": [{"name": "Кинжал", "qty": 1, "desc": d, "value": 9, "weight": 0.5}],
            "skills": {"скрытность": {"rank": "A", "desc": d}},
            "abilities": {"огнешар": {"school": "огонь", "cost": 3, "desc": d}},
            "effects": {"яд_острый": {"desc": d, "turns": 2, "damage": 3}},
            "progress": {"moves": 1},
        },
        "quests": {"q1": {"title": "Т", "desc": d, "status": "active", "progress": d}},
        "shops": {"sh1": {"name": "Лавка", "desc": d,
                          "items": [{"name": "х", "price": 1, "qty": 1, "desc": d}]}},
        "crafts": {"cr1": {"name": "Р", "desc": d, "ingredients": [{"name": "й", "qty": 2}],
                           "result": {"name": "К", "qty": 1, "value": 3, "desc": d}}},
        "enemies": {"e1": {"name": "Гоблин", "desc": d, "hp": 5, "max_hp": 9, "money": 2}},
        "npc": {"n1": {"name": "Бармен", "mood": d, "faction": "f1", "money": 1}},
        "companions": {"c1": {"name": "Спутник", "hp": 5, "max_hp": 9, "level": 2, "desc": d}},
        "flags": {"флаг_один": d},
        "factions": {"f1": {"name": "Гильдия", "desc": d, "relations": {"f2": d}}},
        "board": [{"title": "Объявление", "text": d}],
        "pending_visions": [{"hint": d}],
        "timers": {"т1": {"desc": d, "turns_left": 3}},
    }


def test_render_setting_does_not_emit_live_html():
    """На грязном мире «живых» тегов в innerHTML нет, а текст читается полностью."""
    _node()
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(_dirty_setting(), fh, ensure_ascii=False)
        state_path = fh.name
    try:
        r = subprocess.run([_node(), str(PROBE), state_path], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
    finally:
        Path(state_path).unlink(missing_ok=True)
    out = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 0, f"renderSetting протекает XSS:\n{out[-1500:]}"
    assert re.search(r"escaped_hits=(?:[5-9]|\d\d)", out), \
        f"грязь не дошла до DOM (экранированных вставок почти нет):\n{out}"


def test_probe_itself_catches_regression():
    """Страховка от «тест ничего не проверяет»: если trunc перестанет экранировать,
    полигон обязан покраснеть (проверяется на копии app.js в temp, рабочий файл не трогаем)."""
    _node()
    js = APP_JS.read_text(encoding="utf-8")
    assert "return esc(cut);" in js, "trunc больше не экранирует — сам факт A2 сломан"
    broken = js.replace("return esc(cut);", "return cut;", 1)
    tmpdir = Path(tempfile.mkdtemp(prefix="xss-probe-"))
    (tmpdir / "frontend").mkdir()
    (tmpdir / "frontend" / "app.js").write_text(broken, encoding="utf-8")
    (tmpdir / "scripts").mkdir()
    (tmpdir / "scripts" / "xss_probe.js").write_text(PROBE.read_text(encoding="utf-8"),
                                                     encoding="utf-8")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as fh:
        json.dump(_dirty_setting(), fh, ensure_ascii=False)
        state_path = fh.name
    try:
        r = subprocess.run([_node(), str(tmpdir / "scripts" / "xss_probe.js"), state_path],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
    finally:
        Path(state_path).unlink(missing_ok=True)
    assert r.returncode != 0, "полигон не заметил, что экранирование убрали — он бессилен"


# ── 3. атрибутов без экранирования не осталось ─────────────────────────

def test_entity_kind_and_key_are_escaped_and_url_encoded():
    """`data-kind` карточки (из PATCH/дампа) — экранирован, а в URL идёт кодированным."""
    js = APP_JS.read_text(encoding="utf-8")
    assert 'data-kind="${e.kind}"' not in js, "неэкранированный data-kind вернулся"
    assert 'data-kind="${attrArg(e.kind)}' in js
    assert 'data-eid="${attrArg(e.entity_key)}' in js, "data-eid обязан экранироваться так же"
    assert "encodeURIComponent(e.kind)" in js and "encodeURIComponent(e.entity_key)" in js, \
        "kind/key уходят в путь запроса сырыми"
    # класс маркера на карте — из graph-узлов, его тоже не вставляем как есть
    assert 'm-${n.kind}"' not in js


def test_kv_and_flag_word_escape_values():
    """kv()/flagWord() — значения пишет мастер/модель, экранирование внутри помощника."""
    js = APP_JS.read_text(encoding="utf-8")
    kv = re.search(r"const kv = [^\n]*", js).group(0)
    assert "<div>${esc(k)}</div><b>${esc(v)}</b>" in kv, kv
    assert "kvHtml" in js, "места с намеренным HTML должны уходить в kvHtml, а не в kv"
    body = js[js.index("function flagWord("):js.index("// Человекочитаемое имя фракции")]
    assert "esc(JSON.stringify(v))" in body


def test_no_double_escaping_in_markup():
    """Двойное экранирование (esc(trunc(…))) — игрок видит «&amp;lt;». Его быть не должно."""
    js = APP_JS.read_text(encoding="utf-8")
    assert not re.search(r"\besc\(\s*trunc\(", js), "вернулось двойное экранирование"


# ── 4. серверная половина: PATCH карточки не принимает любой kind ────────

def test_entity_patch_rejects_unknown_kind(api_client):
    """A2 (аудит 41): вид карточки валидировал только POST, PATCH — нет."""
    client, _ = api_client
    wid = client.post("/api/worlds", json={"theme_id": THEME_ID, "name": "XSS"}).json()["world_id"]
    assert client.post(f"/api/worlds/{wid}/entities",
                       json={"kind": "npc", "key": "barman", "name": "Бармен"}).status_code == 200
    body = {"kind": "npc", "key": "barman", "name": "Бармен"}
    r = client.patch(f"/api/worlds/{wid}/entities/<img%20onerror=1>/barman", json=body)
    assert r.status_code == 400, r.text
    assert client.post(f"/api/worlds/{wid}/entities",
                       json=dict(body, kind="<b>bad</b>")).status_code == 400

