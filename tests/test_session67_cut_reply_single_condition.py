# -*- coding: utf-8 -*-
"""Сессия 67 — D5 (аудит 41): решение «ответ оборван» живёт в ОДНОМ условии.

Было в роуте хода (`backend/routers/core.py`, хвост блока «защита от деградации ответа»):

    if not _looks_finished(final_text) and not final_text.strip().endswith((".", "!", "?", "…")):

Второе условие избыточно: `_looks_finished` отвечает «закончен» для любого текста, последний
символ которого входит в `_FINISH_CHARS = (".", "!", "?", "…", '"', "»", "'", "”", "\\n")`,
а `(".","!","?","…")` — её строгое подмножество. Значит «не закончен» ⇒ «не кончается точкой,
восклицательным, вопросительным или многоточием» всегда верно, и `and ...` никогда не меняло
результат. Опасность не в «лишней скобке», а в том, что решение об одном LLM-проходе
дописывания (`_finish_cut_reply`) висело на ДВУХ разных эвристиках: правка `_FINISH_CHARS`
(например добавить `:`) разъезжала бы их молча.

Стало: одно условие `not _looks_finished(final_text)` и комментарий, что именно оно проверяет
(обрыв ответа без финального знака препинания), с поблажками самой эвристики (пустой текст и
короче 20 символов — не обрыв).

ДО фикса падали два теста: `test_no_duplicated_punctuation_check_left` (в исходнике стояло
второе endswith-условие) и `test_cut_decision_documented_in_router` (пояснения не было).
Остальные — страховка от будущих правок: `test_old_second_condition_disagreed_on_quotes`
фиксирует «спорную» зону (ответ, закрытый кавычкой), где снятое условие молчало вместе с
первым, а в одиночку кричало «обрыв».
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "backend" / "routers" / "core.py"

# Знаки, которые считались «финалом» снятым вторым условием.
OLD_ALPHABET = (".", "!", "?", "…")


@pytest.fixture(scope="module")
def core():
    from backend.routers import core as m
    return m


# (текст, обязан ли считаться оборванным). Длинные — ≥20 символов после strip,
# чтобы поблажка «короткие не трогаем» не маскировала проверку.
CASES: list[tuple[str, bool]] = [
    ("", False),                                       # пусто — не обрезок (свой путь)
    ("Он идёт к груде камня.", False),                 # точка
    ("Ты смотришь на карту, и туман сгущается…", False),  # многоточие
    ("Берегись, там кто-то есть!", False),             # восклицательный
    ("Ты идёшь? Ну так отвечай же.", False),           # вопрос + точка
    ("Он сказал: «привет»", False),                    # закрытая кавычка = законченная реплика
    ("И только ветер в щелях — тот самый ветер из дома", True),   # обрыв на полуслове
    ("Ты идёшь к груде камня у обочины, где когда", True),
    ("«Привет», — сказал стражник и замолчал", True),  # реплика закрыта, мысль — нет
    ("Ты смотришь на карту (она покроежена", True),     # незакрытая скобка
    ("Первая строка\nвторая строка без конца", True),
]


def test_finish_alphabet_contains_old_alphabet(core):
    """Инвариант, на котором держится снятие: «. ! ? …» ⊂ _FINISH_CHARS.

    Если кто-то уберёт знак из `_FINISH_CHARS` — правка D5 перестаёт быть эквивалентной,
    и тест кричит раньше, чем дописывание обрезков молча пропадёт.
    """
    for ch in OLD_ALPHABET:
        assert ch in core._FINISH_CHARS, f"{ch!r} пропал из _FINISH_CHARS — D5-аргумент невалиден"
    tail = "Это длинный ответ рассказчика, который кончается вот т"
    for ch in OLD_ALPHABET:
        assert core._looks_finished(tail + ch), f"ответ на «{ch}» не признан законченным"


@pytest.mark.parametrize("text,cut", CASES,
                         ids=[re.sub(r"\W+", "_", t)[:24] or "empty" for t, _ in CASES])
def test_single_condition_equals_old_conjunction(core, text, cut):
    """Одно условие даёт ровно то же решение, что и прежнее `A and B`."""
    new = not core._looks_finished(text)
    stripped = text.strip()
    old = new and bool(stripped) and not stripped.endswith(OLD_ALPHABET)
    assert new == cut, f"обрыв опознан неверно на {text!r}"
    assert new == old, f"одиночное условие разошлось с прежним `A and B` на {text!r}"


def test_old_second_condition_disagreed_on_quotes(core):
    """Снятое условие СПОРИЛО с первым: ответ в кавычках оно звало обрывом, `_looks_finished` — нет.

    Это не «обе одинаковые», а «одна из них никогда не включалась»: на тех же текстах
    `endswith` кричало «обрыв», а конъюнкция молчала. Правка убирает ложную альтернативу,
    а не поведение.
    """
    text = "Он сказал: «иди за мной, но молчи»"
    assert core._looks_finished(text), "кавычка не считается финалом — правка D5 не нужна?"
    assert not text.endswith(OLD_ALPHABET), "пример должен быть из «спорной» зоны"


def test_no_duplicated_punctuation_check_left(core):
    """Второго, «своего» алфавита концовок в роуте не осталось."""
    src = io.open(CORE, encoding="utf-8").read()
    for pat in (r'endswith\(\("\.",\s*"!"', r'endswith\(OLD_ALPHABET'):
        assert not re.search(pat, src), "D5: endswith-условие вернулось — решение снова в двух местах"
    assert src.count("_looks_finished(final_text)") >= 2, \
        "D5: проверка обрыва осталась одна — и для дописывания, и для метрики cut_mid"


def test_cut_decision_documented_in_router(core):
    """Комментарий «почему одно условие» обязан быть рядом (иначе вернут второе)."""
    src = io.open(CORE, encoding="utf-8").read()
    i = src.index("if not _looks_finished(final_text):")
    window = src[max(0, i - 1600):i]
    assert "D5" in window and "_FINISH_CHARS" in window, \
        "нет пояснения, почему проверка ровно одна"


# ══════════════════════ Интеграция: поведение хода не изменилось ══════════════════════
CUT = "Ты идёшь к груде камня у обочины, где когда-то стоял указатель, а теперь только"
QUOTED = "Стражник медленно повернулся и сказал: «иди за мной, но молчи»"
TAIL = "Теперь там лежит ржавый жетон с нацарапанной меткой."
FINISH_MARK = "Твой ответ оборвался на полуслове"


def _run_turn(api_client, monkeypatch, reply):
    """Прогон хода с фиксированным ответом модели; возвращает (текст, промпты всех проходов).

    Заглушку навешиваем ПОСЛЕ создания мира: иначе она перехватывает и проходы генерации
    персонажа (create_world зовёт llm.complete), а это уже другой тест.
    """
    from backend import llm as llm_mod
    client, _holder = api_client
    wid = client.post("/api/worlds", json={"theme_id": "raskolotye-nebesa", "name": "D5"}
                      ).json()["world_id"]
    prompts: list[str] = []

    async def fake(msgs, **kw):
        prompts.append(chr(10).join(str(m.get("content", "")) for m in msgs))
        return reply if len(prompts) == 1 else " " + TAIL

    monkeypatch.setattr(llm_mod, "complete", fake)
    r = client.post(f"/api/worlds/{wid}/action", json={"text": "осмотреться"})
    assert r.status_code == 200, r.text
    return r.json()["reply"], prompts


def test_cut_reply_gets_one_finish_pass(api_client, monkeypatch):
    """Оборванный ответ — ровно один дописывающий проход (как и раньше)."""
    reply, prompts = _run_turn(api_client, monkeypatch, CUT)
    assert sum(FINISH_MARK in p for p in prompts) == 1, \
        f"дописывание отработало иначе: {len(prompts)} проходов"
    assert TAIL in reply, f"хвост не дописан: {reply!r}"
    assert not reply.rstrip().endswith("только"), "игрок увидел обрыв"


def test_quoted_ending_is_not_treated_as_cut(api_client, monkeypatch):
    """Ответ, закрытый кавычкой, НЕ считается обрывом (поблажка _looks_finished)."""
    reply, prompts = _run_turn(api_client, monkeypatch, QUOTED)
    assert not any(FINISH_MARK in p for p in prompts), \
        "кавычка на конце превратилась в обрыв — дописывание лишнее"
    assert reply.strip() == QUOTED
