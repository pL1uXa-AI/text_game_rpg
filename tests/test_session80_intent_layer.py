# -*- coding: utf-8 -*-
"""Действие игрока: он заявляет СВОЁ ДЕЙСТВИЕ, а не состояние мира.

Задумка игрока (жалоба дословно): «игрок пишет "я нахожу меч" — это уже предустановка,
максимум что можно тут — "я осматриваюсь в поиске меча"». То есть «пытаюсь найти меч» тоже
не годится: оно всё ещё обещает, что меч существует. Игроку принадлежит только действие.

Слои (`backend/intent.py`, подключён в `routers/core._process_action_inner`):
  * ДЕЙСТВИЕ («осматриваюсь», «убиваю») — не трогаем, он действует сейчас;
  * ИТОГ глагола ПРИОБРЕТЕНИЯ («нашёл», «взял», «украл») → действие: «разглядываю меч»;
  * ИТОГ глагола УСИЛИЯ («убил», «взломал», «убедил») → «пытаюсь убить/взломать/убедить».

Проверки поведенческие (правило 19): разбор, сохранение смысла, идемпотентность,
и что в промпт уходят и переписанное действие, и дословная цитата игрока.
"""
from __future__ import annotations

import pytest

from backend.intent import normalize_intent


# ── приобретение: результат → ДЕЙСТВИЕ, без обещания объекта ────────────

@pytest.mark.parametrize("raw,expected", [
    ("я нашёл меч", "я разглядываю меч"),
    ("я нашёл отпечаток", "я разглядываю отпечаток"),
    ("я взял ключ со стола", "я разглядываю ключ со стола"),
    ("я украл кольцо у купца", "я разглядываю кольцо у купца"),
    ("я нашёл мешок золота на полу.", "я разглядываю мешок золота на полу."),
    ("Нашёл отпечаток!", "разглядываю отпечаток!"),
    ("нашёл", "осматриваюсь"),
])
def test_acquisition_becomes_action_not_claim(raw, expected):
    r = normalize_intent(raw)
    assert r["changed"] and r["kind"] == "acquire"
    assert r["text"] == expected


def test_acquire_does_not_imply_the_object_exists():
    """Ключевое: в результате НЕТ «нашёл»/«пытаюсь найти» — объект не подтверждается."""
    r = normalize_intent("я нашёл меч")
    assert "нашёл" not in r["text"].lower()
    assert "найти" not in r["text"].lower(), "«пытаюсь найти» всё ещё обещает объект"
    assert "меч" in r["text"], "объект обязан сохраниться — игрок сказал, что ищет меч"


def test_acquire_keeps_object_and_secondary_clause():
    r = normalize_intent("я нашёл меч в сундуке и забираю его")
    assert r["text"] == "я разглядываю меч в сундуке и забираю его"


# ── усилие/угроза: результат → попытка ──────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("я убил орка", "я пытаюсь убить орка"),
    ("я взломал замок", "я пытаюсь взломать замок"),
    ("я убедил стражника", "я пытаюсь убедить стражника"),
    ("я взобрался на стену", "я пытаюсь взобраться на стену"),
])
def test_effort_becomes_attempt(raw, expected):
    r = normalize_intent(raw)
    assert r["changed"] and r["kind"] == "attempt"
    assert r["text"] == expected


# ── действие в настоящем времени не трогаем ─────────────────────────────

@pytest.mark.parametrize("raw", [
    "я нахожу меч",
    "осматриваюсь",
    "убиваю орка",
    "убеждаю стражника",
    "я иду на рынок",
])
def test_present_tense_untouched(raw):
    r = normalize_intent(raw)
    assert r["text"] == raw and not r["changed"] and r["kind"] == "process"


# ── крайние случаи ──────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    "пытаюсь взломать замок",
    "хочу найти сундук",
    "Пробую договориться с лешим",
    "осматриваюсь в поисках меча",
])
def test_existing_attempts_not_rewritten(raw):
    r = normalize_intent(raw)
    assert r["text"] == raw
    assert "пытаюсь пытаюсь" not in r["text"]


def test_question_is_not_a_claim():
    raw = "Кто-нибудь видел здесь торговца?"
    r = normalize_intent(raw)
    assert r["text"] == raw and r["kind"] == "question"


def test_original_phrase_preserved():
    raw = "я нашёл меч"
    assert normalize_intent(raw)["original"] == raw


def test_normalize_is_idempotent():
    once = normalize_intent("я нашёл отпечаток")
    twice = normalize_intent(once["text"])
    assert twice["text"] == once["text"]


def test_empty_action_safe():
    r = normalize_intent("   ")
    assert r["text"] == "" and not r["changed"]


# ── интеграция: что видит модель ────────────────────────────────────────

def test_prompt_gets_action_and_verbatim_quote(fake_config):
    from backend import narrator

    world = {"id": 1, "name": "Тест", "language": "ru", "gen_settings": "{}"}
    setting = {"player": {"name": "Тест", "hp": 10, "max_hp": 10, "mp": 5, "max_mp": 5,
                          "stats": {}}, "location": "start"}
    action = "я нашёл меч"
    intent = normalize_intent(action)
    action_ctx = (f"{intent['text']}\n(дословно игрок написал: «{intent['original']}»)")
    messages, _meta = narrator.build_messages(world, setting, action_ctx, [], [], [])
    prompt = "\n".join(str(m.get("content") or "") for m in messages)
    assert "разглядываю меч" in prompt, "в промпте нет переведённого действия"
    assert "«я нашёл меч»" in prompt, "в промпте нет дословной цитаты игрока"
    assert "ПЫТАЕТСЯ СДЕЛАТЬ ИГРОК" in prompt
