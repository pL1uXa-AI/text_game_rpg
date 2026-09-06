# -*- coding: utf-8 -*-
"""Аудит 41, C3 (сессия 61): латиница, приклеенная к кириллице, больше не живёт молча.

Было: в текстах проекта встречались гибриды двух алфавитов в одном слове — «ф» + «antom» +
«ные» (комментарий `backend/db.py`), «conservat» + «ивный» (docstring `backend/memory.py`),
«и» + «closed» и «following» в фразе (`AGENT.md`). Ни словарь `TYPOS` (он знает конкретные
ошибки), ни `check_frontend.py` (CJK и омоглифы-латиницу внутри кириллицы по буквам) такой
класс не ловили, а глаз читает гибрид как нормальное русское слово.

Что проверяется:

1. `test_glue_hybrids_are_gone` — конкретные исправленные строки (тот же приём, что у
   `test_known_typos_are_gone` C10: цитата дефекта не должна вернуться).
2. `test_check_typos_covers_glue_class` — у сканера ЕСТЬ проверка этого класса (regex +
   белый список), и она работает в обе стороны: заведомую вставку находит, легальную
   техническую речь («через RAG», «в JSON») не трогает. Это и есть граница решения:
   буквальный regex из задания (`[а-яё]+\\s*[a-zA-Z]{2,}`, со пробелом) дал бы 376 попаданий
   по одному только `backend/` — не опечатки, а нормальные термины.
3. `test_check_typos_script_runs` — весь проект чист по обеим проверкам (код возврата 0).

Запуск: "…\\3.12.10\\python.exe" -X utf8 -m pytest tests/test_session61_latin_glue.py -q
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_checker():
    """Загрузить scripts/check_typos.py как модуль (в scripts/ нет __init__.py)."""
    spec = importlib.util.spec_from_file_location("check_typos", ROOT / "scripts" / "check_typos.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Исправленные места: (файл, фрагмент ИСПРАВЛЕННОЙ строки). Цитаты самих опечаток здесь
# собраны из кусков — иначе этот файл не прошёл бы проверку, которую сам же запускает.
# Два «AGENT.md» стали «AGENT_ARCHIVE.md»: с C4 (сессия 62) эссе сессии 40, п.2 — с исправленными
# формулировками — переехало в архив дословно; проверяется то, где текст живёт сейчас.
FIXED = [
    ("backend/db.py", "«фа" + "нтомные» сообщения"),
    ("backend/memory.py", "Критерий консерв" + "ативный"),
    ("AGENT_ARCHIVE.md", "пробел и закр" + "ыла его через `item_update`"),
    ("AGENT_ARCHIVE.md", "в тексте двух по" + "следующих ходов"),
    ("scripts/check_start_bat.py", "и assert" + "'ами сверяли подстроки"),
]


@pytest.mark.parametrize("rel,good", FIXED)
def test_glue_hybrids_are_gone(rel: str, good: str):
    """Латинская вставка убрана, русский вариант на месте и не «откатился»."""
    text = (ROOT / rel).read_text(encoding="utf-8")
    assert good in text, f"{rel}: исправленная формулировка не найдена: {good!r}"
    checker = _load_checker()
    for n, line in enumerate(text.splitlines(), 1):
        if checker.SKIP_MARK in line:
            continue
        hits = checker._glue_line(line)
        assert not hits, f"{rel}:{n}: латиница в кириллице {hits}"


def test_check_typos_covers_glue_class():
    """Сканер знает класс C3: regex, белый список и проба в обе стороны."""
    c = _load_checker()
    assert hasattr(c, "GLUE_RE") and hasattr(c, "GLUE_OK"), "в check_typos.py нет проверки C3"
    # заведомые вставки (собраны из кусков — иначе этот файл не пройдёт свою же проверку)
    for bad in ("«ф" + "antom" + "ные»", "Критерий " + "conservat" + "ивный",
                "пробел " + "и" + "closed" + " его", "не закр" + "onservat" + "ировано"):
        assert c._glue_line(bad), f"не поймана вставка: {bad!r}"
    # легальная техническая речь — не опечатка (именно ради этого границы слитные)
    for ok in ("через RAG и LLM", "в JSON-дампе мира", "`item_update` дописывает add_item",
               "локальная llama.cpp", "по id карточки", "флаг GAME_BIND"):
        assert not c._glue_line(ok), f"ложное срабатывание: {ok!r} → {c._glue_line(ok)}"
    assert c.probe() == 0, "самопроверка сканера (check_typos.py --probe) провалилась"


def test_check_typos_script_runs():
    """Весь проект чист: словарь опечаток + проверка латинских вставок (код возврата 0)."""
    r = subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "scripts" / "check_typos.py")],
                       cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"check_typos.py: {r.stdout[-800:]}{r.stderr[-400:]}"


def test_scan_reaches_backend_and_docs():
    """Проверка реально смотрит и код, и документацию (иначе «чисто» ничего не значит).

    Отдельно фиксирует осознанную границу метода: слово через пробел («… двух following
    ходов») сканер НЕ зовёт ошибкой — со пробелом он даёт 376 попаданий по одному `backend/`
    («через RAG», «в JSON»), то есть тонет в ложном. Под автоматикой — слитные гибриды
    двух алфавитов в одном слове; остальные правят глазами или словарём TYPOS.
    """
    c = _load_checker()
    files = {str(p.relative_to(ROOT)).replace("\\", "/") for p in c.collect([])}
    assert any(f.startswith("backend/") for f in files)
    assert any(f.endswith(".md") for f in files)
    assert "scripts/check_typos.py" in files, "сканер обязан проверять и себя"
    assert not c._glue_line("в тексте двух following ходов"), "граница слитности сломана"
