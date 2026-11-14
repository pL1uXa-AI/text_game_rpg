# -*- coding: utf-8 -*-
"""Язык ответа рассказчика не должен съезжать.

Найдено живьём (сессия 80, Qwen3 на llama.cpp): на длинном русском промпте модель
продолжала сцену по-английски — «There is no dragon. There is a coil of rope…». Игрок
видит текст не на языке мира, и сцена разваливается, хотя модель «сделала всё правильно».

Два слоя: жёсткая инструкция в промпте (правило языка) + проверка готового текста с
одним переписыванием. Проверяем поведенчески: детектор и переписывание.
"""
from __future__ import annotations

from backend import language as lang_mod


RUSSIAN_OK = ("Ты стоишь у кромки причала, руки в карманах, и просто слушаешь. Утро здесь "
              "имеет свой темп: где-то за складом лязгает молот по клёпкам, и каждый удар "
              "идёт через дерево, через воздух, через твою грудную клетку.")

ENGLISH_SLIP = ("Ты приседаешь на корточки, and your eyes drag across the stretch of "
                "cobblestone in front of you the way they always do. Three generations of "
                "Solyen hands know this posture. Morning light still cuts low and sideways.")


def test_russian_text_passes():
    assert not lang_mod.wrong_language(RUSSIAN_OK, "ru")


def test_english_slip_is_detected():
    assert lang_mod.wrong_language(ENGLISH_SLIP, "ru")


def test_mixed_phrase_inside_russian_is_caught():
    """Реальный случай с Qwen3: русский абзац, а внутри — английская фраза.

    Такая вставка даёт 0.82 кириллицы и по «доле букв» проскакивала (порог 0.85), хотя
    игрок в тексте видит чужой язык. Поэтому есть второй критерий — фраза из ≥3 латинских
    слов подряд.
    """
    mixed = ("Каменный Мост держит реку без единой опоры. Он закидывает линию, и руки "
             "знают жест: мять the hook straight, flick the wrist, let the sinker drop.")
    assert lang_mod.wrong_language(mixed, "ru")


def test_latin_nouns_in_russian_are_fine():
    """Имена собственные и отдельные термины латиницей — не смешение языков."""
    t = "Марта Смазчица и Тиль Аркан говорили о Школе Камня, а сержант Дозора Верн молчал."
    assert not lang_mod.wrong_language(t, "ru")


def test_long_russian_text_passes():
    t = ("Каменный Мост держит реку без единой опоры. Он закидывает линию и ждёт поклёвки, "
         "слушая, как вода шлёпает о камень. Марта Смазчица вела учёт на причале, а утро "
         "шло своим чередом, не торопясь и не требуя ничего.")
    assert not lang_mod.wrong_language(t, "ru")


def test_short_text_not_judged():
    """Короткая реплика/цифры — не повод для перегенерации."""
    assert not lang_mod.wrong_language("ОК", "ru")
    assert not lang_mod.wrong_language("", "ru")


def test_english_world_judged_by_latin():
    en = "The rope lies on the wet cobblestones, and the fog does not lift until noon."
    assert not lang_mod.wrong_language(en, "en")
    assert lang_mod.wrong_language(RUSSIAN_OK, "en")


def test_language_note_is_strict():
    """Инструкция в промпте обязана быть жёсткой: одной строки «отвечай по-русски» не хватало."""
    from backend import narrator

    world = {"id": 77, "name": "Мир", "language": "ru", "gen_settings": "{}"}
    setting = {"player": {"name": "Тест", "hp": 10, "max_hp": 10, "mp": 5, "max_mp": 5,
                          "stats": {}}, "location": "start"}
    prompt = narrator.build_system_prompt(world, setting)
    assert "ЯЗЫК ОТВЕТА" in prompt and "РУССКИЙ" in prompt
    assert "не смешивай языки" in prompt.lower() or "не смешивай" in prompt.lower()
