# -*- coding: utf-8 -*-
"""Сессия 40, п.11 (мир «Новый мир»): в статистике пути больше нет системных ярлыков.

Симптом: в карточке игрока — «📊 Статистика: Moves 1 · Discoveries 1», в `/stats` —
«Пройдено: moves 1; discoveries 1». Игрок читает машинные ключи движка как текст.
Причина НЕ в данных: `_bump` (`backend/mechanics.py`) заводит счётчики именно этими
ключами (`kills`/`quests_done`/`moves`/`discoveries`), а UI печатал ключ «как есть,
только с заглавной» (`ucfirst(k)`), и `/stats` — тоже.

Правка — только отображение (закон 2): движок НЕ меняет ключи (`progress_add` в
промпте, `format_state` у модели и старые сохранения завязаны на машинные имена) и
ничего не выдумывает (закон 3). Есть точный словарь человекочитаемых ярлыков для
ЧЕТЫРЁХ счётчиков, которые ведёт сам код; ключи, придуманные мастером
(`progress_add {победы: 1}`), выводятся как есть — перефразировать чужое имя права нет.

Тот же класс дефекта, что п.9 (машинные имена в карточках), но слой — статистика.
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

# Машинные счётчики, которые ведёт ДВИЖОК (см. _bump в mechanics.py) — обязаны иметь ярлык.
ENGINE_COUNTERS = {"kills", "quests_done", "moves", "discoveries"}


def test_bump_keys_are_the_ones_we_label():
    """Словарь ярлыков не должен устареть: все keys, которые пишет _bump, в нём есть."""
    from backend.mechanics import PROGRESS_LABELS
    src = (ROOT / "backend" / "mechanics.py").read_text(encoding="utf-8")
    written = set(re.findall(r'_bump\([^\)]*,\s*"([a-z_]+)"\)', src))
    assert written == ENGINE_COUNTERS, f"движок пишет новые счётчики: {written - ENGINE_COUNTERS}"
    assert written <= set(PROGRESS_LABELS), f"без ярлыка: {written - set(PROGRESS_LABELS)}"


def test_progress_label_readable_and_conservative():
    from backend.mechanics import progress_label
    for k in ENGINE_COUNTERS:
        lab = progress_label(k)
        assert lab and lab != k, f"{k} → {lab!r}: ярлык обязан отличаться от машинного ключа"
        assert not re.fullmatch(r"[A-Za-z0-9_\- ]+", lab), f"не человекочитаемо: {lab!r}"
    # ключ мастера (на языке мира) — не трогаем (закон 3)
    assert progress_label("победы") == "победы"
    assert progress_label("Дуэлей выиграно") == "Дуэлей выиграно"
    assert progress_label("") and progress_label(None)


def test_stats_slash_shows_labels_not_keys(api_client):
    """/stats: игрок видит «Переходов 1», а не «moves 1»."""
    from backend import db
    client, _ = api_client
    wid = client.post("/api/worlds", json={"theme_id": _theme(), "name": "P11"}).json()["world_id"]
    st = db.get_world(wid)["setting"]
    if isinstance(st, str):
        st = json.loads(st)
    st["player"]["progress"] = {"moves": 3, "discoveries": 2, "победы": 1}
    db.update_world(wid, setting=st)
    reply = client.post(f"/api/worlds/{wid}/action", json={"text": "/stats"}).json()["reply"]
    low = reply.lower()
    for k in ENGINE_COUNTERS:
        assert k not in low, f"машинный ключ {k!r} вернулся в /stats: {reply!r}"
    assert "Переходов 3" in reply and "Открыто локаций 2" in reply, reply
    assert "победы 1" in reply, f"ключ мастера обязан остаться как есть: {reply!r}"


def test_ui_statistics_row_uses_label_helper():
    """В карточке игрока ярлык счётчика берётся из progressLabel, а не из ucfirst(k)."""
    row = next(ln for ln in APP_JS.splitlines() if "📊 Статистика" in ln)
    assert "progressLabel(" in row, row
    assert "ucfirst(k)" not in row, row
    fn = _fn_src("progressLabel")
    assert "PROGRESS_LABELS" in fn


def test_progress_label_real_js():
    """НАСТОЯЩИЙ progressLabel из app.js на Node: машинные ключи → игровые слова."""
    snippet = _fn_src("progressLabel")
    # словарь объявлен отдельным `const` вне функции — берём его из исходника как есть
    m = re.search(r"^const PROGRESS_LABELS = \{.*?^\};$", APP_JS, re.S | re.M)
    assert m, "в app.js нет ожидаемого словаря PROGRESS_LABELS"
    snippet = m.group(0) + "\n" + snippet + """
const got = ["moves", "discoveries", "kills", "quests_done", "победы", "Дуэлей выиграно", ""]
  .map((k) => `${k}=>${progressLabel(k)}`);
const machine = ["moves", "discoveries", "kills", "quests_done"]
  .filter((k) => progressLabel(k) === k);
console.log(JSON.stringify({ got, machine }));
"""
    out = _run_node(snippet)
    data = json.loads(out.strip().splitlines()[-1])
    assert not data["machine"], f"машинные ключи без ярлыка: {data['machine']}"
    joined = " | ".join(data["got"])
    assert "moves=>Переходов" in joined and "discoveries=>Открыто локаций" in joined, joined
    assert "победы=>победы" in joined, "ключ мастера перефразирован (закон 3)"
    assert "=>?" in joined, "пустой ключ обязан дать прочный ярлык, а не undefined"


def test_frontend_and_backend_labels_agree():
    """Ярлыки фрон и бэк говорят одинаково (иначе /stats и картка разойдутся)."""
    from backend.mechanics import PROGRESS_LABELS
    m = re.search(r"^const PROGRESS_LABELS = \{(.*?)\n\};$", APP_JS, re.S | re.M)
    assert m, "в app.js не найден словарь PROGRESS_LABELS"
    js = dict(re.findall(r'(\w+)\s*:\s*"([^"]+)"', m.group(1)))
    assert js == PROGRESS_LABELS, f"расхождение фронта и бэкенда:\n{js}\n{PROGRESS_LABELS}"


def test_keys_untouched_in_state():
    """Ярлык — только вывод: в setting ключи остаются машинными (промпт и старые сохранения)."""
    from backend.mechanics import _bump
    p: dict = {}
    _bump(p, "moves")
    _bump(p, "discoveries")
    assert list(p["progress"]) == ["moves", "discoveries"], p
    # format_state (промпт мастера) тоже остаётся машинным — мастер зовёт progress_add по id
    from backend.narrator import format_state
    st = json.loads(json.dumps(_bare_setting()))
    st["player"]["progress"] = {"moves": 2}
    assert "moves 2" in format_state(st)


# ── помощники ─────────────────────────────────────────────────────────

def _theme() -> str:
    from backend import narrator as narrator_mod
    return narrator_mod.THEMES[0]["id"]


def _bare_setting() -> dict:
    from backend import narrator as narrator_mod
    return narrator_mod.default_setting(narrator_mod.THEMES[0], "normal")


def _fn_src(name: str) -> str:
    from check_frontend import _extract_fn
    return _extract_fn(APP_JS, name)


def _run_node(snippet: str) -> str:
    if subprocess.run(["node", "--version"], capture_output=True, text=True).returncode != 0:
        pytest.skip("node недоступен")
    tmp = ROOT / "_s40_p11_probe.cjs"
    tmp.write_text(snippet, encoding="utf-8")
    try:
        r = subprocess.run(["node", str(tmp)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
    finally:
        tmp.unlink(missing_ok=True)
    assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    return (r.stdout or "") + (r.stderr or "")
