# -*- coding: utf-8 -*-
"""Сессия 69 — D7 (аудит 41): исход броска считается по факту броска, а не по итогу.

Было в `backend/narrator.py::roll_outcome`:

    if total == 1 or (expr.startswith("d20") and total <= dc - 10):
        return "критический провал"

Две мёртвые/спорные ветки:

* `total == 1` сравнивает с единицей ИТОГ, в который уже вошли `mod` и бонус куба. Модель
  пишет `roll: {expr: "d20", mod: 5}` и кидает честную 1 → total = 6 → никакого крит-провала,
  хотя правило 8 промпта («критический провал 1») про ГРАНЬ. Обратно: на `2d6` сумма равна 1
  невозможно — ветка никогда не срабатывала для многокубиковых бросков;
* `expr.startswith("d20")` зависел от НАПИСАНИЯ: `1d20`, `D20`, `d20+3`, `d20 +5` — все они
  `roll_expr` распознаёт и бросает как d20, но startswith их не видел, и планка «провал на
  10+ хуже сложности» не включалась никогда (а директура модели именно такие строки и пишет).

Стало: натуральная единица считается по `res["rolls"]` (переданный 4-й аргумент `rolls`),
форма куба — по той же регулярке, что и распознавание (`_DICE_RE`, общий источник истины с
клампом A1), регистр и пробелы не важны. Роут хода передаёт и `rolls`, и фактический куб из
`res["expr"]` (после клампов), а не исходную строку модели.

Поведение многокубиковых бросков НЕ изменили: крит-провал по dc−10 остался только для
одиночного d20 (на 3d6 разброс шире, такая планка ссыпала бы в 💥 каждый средний провал).
"""
from __future__ import annotations

import inspect
import io
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NARRATOR = ROOT / "backend" / "narrator.py"
CORE = ROOT / "backend" / "routers" / "core.py"

from backend.narrator import _dice_shape, roll_expr, roll_outcome  # noqa: E402


# ══════════════════ 1. Нормализация выражения (было: startswith) ══════════════════
@pytest.mark.parametrize("expr", ["d20", "1d20", "D20", "d20+3", "D20+5", "1d20-2",
                                  "d20 + 5", "  d20  ", "d20+0"])
def test_d20_shapes_all_recognized(expr):
    """Все написания d20, которые реально пишет модель, опознаются как одиночный d20."""
    assert _dice_shape(expr) == (1, 20), expr


@pytest.mark.parametrize("expr", ["2d6", "d6", "3d6+1", "d100", "1d20zz", "d20x", "d", "",
                                  "  ", "d0", "d1", "7"])
def test_non_d20_shapes_not_matched(expr):
    """Ни один из этих кубов не обязан вести себя как «проверка d20».

    `d0`/`d1` формально матчатся как (1, 0)/(1, 1) — но это никогда не 20-гранник, а мусор
    `roll_expr` и так заменяет фолбэком d20. `1d20zz`/`d20x` — не куб: прежний `re.match`
    съедал префикс молча, полный совпад (A1) такого не делает.
    """
    shape = _dice_shape(expr)
    assert not (shape and shape == (1, 20)), expr


def test_single_regex_shared_with_roll_expr():
    """Один источник истины: то, что бросает `roll_expr`, и то, что считает исход."""
    for expr in ("d20", "1d20", "D20+3", "2d6", "d100", "3d6+2"):
        res = roll_expr(expr)
        assert _dice_shape(res["expr"]) == _dice_shape(expr), expr
    # Мусор → фолбэк d20: исход должен считаться по фактическому (d20), а не по вводу модели.
    for bad in ("2d6zz", "d0", "", "может d20?"):
        assert _dice_shape(bad) != (1, 20), bad
        assert roll_expr(bad)["expr"] == "d20", bad


# ══════════════════ 2. Натуральная единица по кубу, а не по итогу ══════════════════
def test_nat_one_with_bonus_is_critical_fail():
    """d20+5 на честной 1 (total=6) — крит-провал. Раньше это был просто «провал»."""
    assert roll_outcome(6, 15, "d20+5", [1]) == "критический провал"
    # тот же итог без единицы на кубе (2d6: 1+5? нет, суммы 1 не бывает) — не крит
    assert roll_outcome(6, 15, "2d6", [2, 4]) == "провал"


def test_multi_dice_total_one_branch_is_not_silently_dead():
    """`total == 1` больше не единственный признак: на 2d6 он невозможен, на d20 — работает."""
    assert roll_outcome(1, 15, "d20", [1]) == "критический провал"
    # Rolls не передан (внешний вызов) — прежний слабый признак сохраняется, а не падает.
    assert roll_outcome(1, 15, "d20") == "критический провал"
    assert roll_outcome(1, 15, "1d20") == "критический провал"


def test_high_roll_with_bonus_not_downgraded():
    """Никаких новых крит-провалов там, где их не было: хороший итог — хороший результат."""
    assert roll_outcome(25, 15, "1d20+5", [20]) == "критический успех"
    assert roll_outcome(16, 15, "d20+3", [13]) == "успех"
    assert roll_outcome(9, 15, "d20+3", [6]) == "провал"


def test_dc_minus_ten_only_for_single_d20():
    """Планка dc−10 — у одиночного d20; на 2d6/d100 она не срабатывает (как и задумывалось)."""
    assert roll_outcome(5, 15, "D20", [5]) == "критический провал"      # startswith не видел «D20»
    assert roll_outcome(5, 15, "1d20", [5]) == "критический провал"     # …и «1d20»
    assert roll_outcome(5, 15, "d20-5", [10]) == "критический провал"
    assert roll_outcome(5, 15, "2d6", [2, 3]) == "провал"
    assert roll_outcome(5, 15, "d100", [5]) == "провал"
    assert roll_outcome(5, 15, "3d6", [1, 2, 2]) == "провал"


def test_dc_minus_ten_does_not_swallow_normal_fails():
    """dc−10 не превращает каждый средний провал d20 в крит: ровно на границе и ниже."""
    assert roll_outcome(4, 15, "d20", [4]) == "критический провал"
    assert roll_outcome(5, 15, "d20", [5]) == "критический провал"
    assert roll_outcome(6, 15, "d20", [6]) == "провал"


# ══════════════════ 3. Роут передаёт факт броска ══════════════════
def test_router_passes_rolls_and_actual_die():
    """`roll_outcome` в роуте обязан получать rolls и фактический куб, а не строку модели."""
    src = io.open(CORE, encoding="utf-8").read()
    i = src.index("outcome = narrator.roll_outcome(")
    call = src[i:src.index("\n", i)]
    assert 'res.get("rolls")' in call, f"rolls не передаются: {call}"
    assert not re.search(r"roll_outcome\(\s*total,\s*dc,\s*expr\s*\)", src), \
        "вызов вернулся к виду (total, dc, expr) — исход снова по написанию"
    # Старого «startswith» больше нет в теле функции (в комментарии она осталась — там она и нужна).
    body = inspect.getsource(roll_outcome)
    assert "startswith" not in body.split(chr(34)*3)[-1], "в теле roll_outcome вернулся startswith"


def test_actual_die_used_for_shape_not_model_string():
    """Куб для оценки берём из res["expr"] (после клампов), иначе `d0` судился как не-d20."""
    src = io.open(CORE, encoding="utf-8").read()
    i = src.index("outcome = narrator.roll_outcome(")
    assert 'res.get("expr")' in src[i:i + 200], "в оценку идёт исходная строка модели"


# ══════════════════ 4. Интеграция: ход с директивой roll ══════════════════
def _turn_with_roll(api_client, monkeypatch, engine, rolls):
    """Прогон хода, где модель просит бросок; бросок фиксирован, LLM-проходы заглушены."""
    from backend import narrator as nar

    client, holder = api_client
    wid = client.post("/api/worlds", json={"theme_id": "raskolotye-nebesa", "name": "D7"}
                      ).json()["world_id"]

    def fake_roll(expr, mod=0):
        return {"rolls": list(rolls), "total": sum(rolls) + mod,
                "expr": f"1d20+{mod}" if mod else "1d20", "mod": mod, "note": ""}

    monkeypatch.setattr(nar, "roll_expr", fake_roll)
    holder["reply"] = "Ты бьёшь первым, клинок идёт мимо рёбер. <<ENGINE>>" + engine
    r = client.post(f"/api/worlds/{wid}/action", json={"text": "атаковать стражника"})
    assert r.status_code == 200, r.text
    dice = [e for e in r.json()["events"] if e["role"] == "dice"]
    assert dice, f"нет события куба: {r.json()['events']}"
    return dice[0]["content"]


def test_nat_one_with_mod_is_critical_in_turn(api_client, monkeypatch):
    """d20+5, выпала 1 (итог 6) → игрок видит 💥 крит-провал, а не ❌ провал."""
    text = _turn_with_roll(api_client, monkeypatch,
                           '{"roll":{"expr":"d20","mod":5,"dc":15,"label":"атака"}}', [1])
    assert "критический провал" in text, text
    assert "💥" in text, text
    assert "= 6" in text, text


def test_mid_roll_stays_plain_fail_in_turn(api_client, monkeypatch):
    """Тот же куб, но 12 (итог 17) — успех: правка не раздула крит-провалы."""
    text = _turn_with_roll(api_client, monkeypatch,
                           '{"roll":{"expr":"d20","mod":5,"dc":15,"label":"атака"}}', [12])
    assert "успех" in text and "критический" not in text, text


def test_multi_dice_low_total_not_critical_in_turn(api_client, monkeypatch):
    """2d6 без единицы-на-кубике остаётся «провалом» (мёртвая ветка total==1 не ожила)."""
    from backend import narrator as nar

    client, holder = api_client
    wid = client.post("/api/worlds", json={"theme_id": "raskolotye-nebesa", "name": "D7b"}
                      ).json()["world_id"]

    def fake_roll(expr, mod=0):
        return {"rolls": [2, 3], "total": 5, "expr": "2d6", "mod": 0, "note": ""}

    monkeypatch.setattr(nar, "roll_expr", fake_roll)
    holder["reply"] = "Пол под тобой трещит. <<ENGINE>>" + \
        '{"roll":{"expr":"2d6","dc":15,"label":"ловушка"}}'
    r = client.post(f"/api/worlds/{wid}/action", json={"text": "бежать"})
    text = [e for e in r.json()["events"] if e["role"] == "dice"][0]["content"]
    assert "провал" in text and "критический" not in text, text
