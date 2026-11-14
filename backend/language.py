# -*- coding: utf-8 -*-
"""Язык ответа рассказчика: детект ухода с языка мира и переписывание.

Проблема (сессия 80, найдено живьём на Qwen3): локальная модель на длинном промпте иногда
продолжает повествование по-английски посреди русской сцены — «There is no dragon.».
Инструкция в промпте есть, но одной инструкции мало, и игрок видит текст не на своём языке.

Слой честный и дешёвый: считаем долю латиницы и кириллицы в БУКВАХ текста. Формулы, имена,
термины и артефакты — норма, поэтому порог не 0; но если латиницы больше четверти букв,
это уже не «остатки», а другой язык. Тогда — ОДИН повторный проход с просьбой переписать,
и решение принимается по результату: не вышло — остаётся исходный текст (лучше ответ на
чужом языке, чем пустота).
"""
from __future__ import annotations

import re

from .logsetup import get_logger

log = get_logger(__name__)

_LATIN = re.compile(r"[A-Za-z]")
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")

# Порог: доля одного алфавита среди всех букв. Обычный русский текст с парой латинских
# вставок проходит; но доля — грубая мера: три-четыре английских слова посреди русского
# абзаца дают всего 0.82 кириллицы и «проскакивают», хотя игрок видит в тексте чужой язык.
_DOMINANT = 0.85
_MIN_LETTERS = 40

# Второй, более точный критерий: иноземная ФРАЗА внутри текста. Имена, названия моделей и
# термины — это одно-два слова; три и более латинских слова подряд — уже предложение не
# на языке мира. Ловит именно то, что жаловал игрок: «Руки знают жест: мять the hook
# straight, flick the wrist, let the sinker drop».
_LATIN_PHRASE = re.compile(r"[A-Za-z][A-Za-z'’-]*(?:[ \t]+[A-Za-z][A-Za-z'’-]*){2,}")
_CYR_PHRASE = re.compile(r"[А-Яа-яЁё][А-Яа-яЁё'’-]*(?:[ \t]+[А-Яа-яЁё][А-Яа-яЁё'’-]*){3,}")


def _has_foreign_phrase(text: str, lang: str) -> bool:
    """Есть ли внутри текста фраза (≥3 слов подряд) на чужом алфавите."""
    foreign = _CYR_PHRASE if lang == "en" else _LATIN_PHRASE
    return bool(foreign.search(text or ""))


def wrong_language(text: str, lang: str = "ru") -> bool:
    """Ответ написан не на языке мира (заметно по алфавиту)?

    Для ru: кириллица должна доминировать. Для en: наоборот. Другие языковые коды не
    проверяем — эвристика по алфавиту работала бы неверно.
    """
    if lang not in ("ru", "en") or not text:
        return False
    if _has_foreign_phrase(text, lang):
        return True
    latin = len(_LATIN.findall(text))
    cyr = len(_CYRILLIC.findall(text))
    total = latin + cyr
    if total < _MIN_LETTERS:
        return False
    if lang == "ru":
        return cyr / total < _DOMINANT
    return latin / total < _DOMINANT


async def retell_in_language(world: dict, text: str, lang: str, provider: dict | None = None,
                             max_chars: int = 6000) -> str:
    """Просит модель'переписать этот ответ на языке мира, сохранив всё остальное.

    Возвращает переписанный текст или "" (тогда вызывающий оставляет исходный).
    """
    from . import llm

    if lang == "ru":
        sys_msg = ("Ты — редактор-переводчик. Перепиши текст рассказчика ПОЛНОСТЬЮ на РУССКОМ "
                   "языке. ВАЖНО: в тексте встречаются отдельные английские слова и целые "
                   "английские фразы — их тоже переведи на русский (кроме имён собственных и "
                   "названий). Сохрани в точности все события, факты, реплики, детали и порядок "
                   "сцен. Ничего не добавляй и не убирай. Верни только переписанный текст.")
    else:
        sys_msg = ("You are an editor-translator. Rewrite the narrator's text entirely in ENGLISH. "
                   "IMPORTANT: foreign words and whole foreign phrases inside it must be translated "
                   "too (proper names may stay). Preserve exactly every event, fact, line of dialogue, "
                   "detail and scene order. Add or remove nothing. Return only the rewritten text.")
    try:
        out = await llm.complete(
            [{"role": "system", "content": sys_msg},
             {"role": "user", "content": text[:max_chars]}],
            temperature=0.2, provider=provider,
            # Перевод — не творческая задача: «мыслям» здесь нечего улучшать, а у локальной
            # Qwen3 они съедают всю выдачу (модель уходит рассуждать и возвращает пустоту).
            effort="off",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("переписывание на языке мира не удалось (world %s): %s", world.get("id"), e)
        return ""
    out = (out or "").strip()
    if not out:
        log.warning("переписывание на языке мира вернуло пустой текст (world %s)", world.get("id"))
        return ""
    if wrong_language(out, lang):
        # Модель повторила ту же ошибку (часто: «переписала» структуру, но оставила английский
        # текст). Второй заход с куда более прямым указанием — дешевле, чем оставить игроку
        # текст не на его языке.
        log.info("первое переписывание не помогло (world %s) — пробую жёстче", world.get("id"))
        hard = ("ПРЕДЫДУЩИЙ ОТВЕТ БЫЛ НЕ НА РУССКОМ. Переведи его на русский целиком: "
                "каждое слово, каждое предложение. Это перевод, а не пересказ — сохрани все "
                "события и детали. Верни только русский текст."
                if lang == "ru" else
                "THE PREVIOUS REPLY WAS NOT IN ENGLISH. Translate it entirely into English. "
                "Preserve every event and detail. Return only English text.")
        try:
            out = await llm.complete(
                [{"role": "system", "content": hard},
                 {"role": "user", "content": text[:max_chars]}],
                temperature=0.1, provider=provider, effort="off",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("повторное переписывание не удалось (world %s): %s", world.get("id"), e)
            return ""
        out = (out or "").strip()
        if not out or wrong_language(out, lang):
            log.warning("язык ответа не удалось исправить (world %s) — оставляю как есть",
                        world.get("id"))
            return ""
    return out
