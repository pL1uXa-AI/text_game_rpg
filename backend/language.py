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

# Порог: доля одного алфавита среди всех букв. Порог щедрый к «остаткам», но не к смешению:
# модель любит начинать по-русски и скатываться в английский посреди сцены (0.2–0.4
# кириллицы в живом прогоне). 0.85 отсекает такие ответы, оставляя законные латинские
# вставки — имена, названия, термины — которые в русском тексте редки.
_DOMINANT = 0.85
# Минимум букв, чтобы судить о языке (короткие реплики/цифры не считаем).
_MIN_LETTERS = 40


def wrong_language(text: str, lang: str = "ru") -> bool:
    """Ответ написан не на языке мира (заметно по алфавиту)?

    Для ru: кириллица должна доминировать. Для en: наоборот. Другие языковые коды не
    проверяем — эвристика по алфавиту работала бы неверно.
    """
    if lang not in ("ru", "en") or not text:
        return False
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
        sys_msg = ("Ты — редактор. Перепиши текст рассказчика на РУССКОМ языке полностью, "
                   "сохранив в точности все события, факты, реплики, детали и порядок "
                   "сцен. Ничего не добавляй и не убирай. Верни только переписанный текст.")
    else:
        sys_msg = ("You are an editor. Rewrite the narrator's text in ENGLISH, preserving exactly "
                   "every event, fact, line of dialogue, detail and scene order. Add or remove "
                   "nothing. Return only the rewritten text.")
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
