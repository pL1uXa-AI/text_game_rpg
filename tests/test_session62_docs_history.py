# -*- coding: utf-8 -*-
"""Аудит 41, C4 (сессия 62): «СОСТОЯНИЕ И ИСТОРИЯ» и эссе сессий ушли из живого AGENT.md.

Было: `AGENT.md` весил 241 КБ (1041 строка) при объявленном правиле «читай первым перед ЛЮБОЙ
работой» — то есть правило физически не исполнимо, и именно это FINDINGS называет главным
источником «сессия не дочитала и наступила на граблю». Внутри живых разделов («АРХИТЕКТУРА →
Директивы / Автогенерация контента», «СОГЛАШЕНИЯ ПО КОДУ → Известные фиксы», «СОСТОЯНИЕ И
ИСТОРИЯ») лежали отчёты прошедших сессий: эссе баг-ханта 40 (п.1–п.16), рассказы сессий 6–10
и 22–30, поимённая история тестового набора и «Сводка сессии 38». Сессии 1–40 дописывали их
в тот же файл, и он рос как эссеистика.

Сделано (без реформ, как просил пункт): перенесены в `AGENT_ARCHIVE.md` дословно; от каждого
эссе в `AGENT.md` осталась одна живая строка правила + ссылка, куда делось обоснование.
Что осталось — то, что пункт и просил оставить: три закона, жёсткие правила, архитектура,
схема БД, эндпоинты, соглашения, как тестировать, действующие инварианты.

Заодно закрыт риск, из-за которого перенос обычно отваливается: «факт в двух местах». Проверки
(правило 19 — поведение документа, а не цитаты из него):

1. вес/объём живого `AGENT.md` в бюджете — иначе C4 вернулся;
2. ни один абзац-«история» не продублирован в архиве (перенос, а не копирование);
3. ссылки из `AGENT.md` в архив ведут на разделы, которые там реально есть;
4. живой файл больше не держит эссе сессий (длинные строки «Сессия NN» — только в архиве);
5. историческая сводка не попала под сверку чисел: `AGENT_ARCHIVE.md` вне реестра
   `doc_figures` (иначе устаревшую цифру из отчёта «что было» начали бы «чинить»).

Запуск: "…\\3.12.10\\python.exe" -X utf8 -m pytest tests/test_session62_docs_history.py -q
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT = ROOT / "AGENT.md"
ARCHIVE = ROOT / "AGENT_ARCHIVE.md"
README = ROOT / "README.md"

# Бюджет живого файла: было 241 КБ, после переноса истории 158 КБ. 175 КБ — «C4 вернулся»:
# начинают дописывать отчёты вместо архива (прирост на новую сессию — 2–6 КБ правил).
AGENT_BUDGET_KB = 175
# Строка длиннее порога в живом разделе — почти всегда эссе («симптом → причина → замеры»),
# а не правило. Порог завышен: жёсткие правила 3/14/15 и слой памяти — легально длинные.
ESSAY_LINE_BYTES = 1500
# README — витрина: отчёт «сессия NN исправила N дефектов» ему не нужен вовсе.
README_BUDGET_KB = 90
RE_HEAD_SESSION = re.compile(r"(?m)^#{2,4} .*сессия \d")

RE_SESSION_HEAD = re.compile(r"\*\*Сессия \d")


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _essays(text: str) -> list[str]:
    """Строки-эссе: длинные и с шапкой «Сессия NN» (порог — ESSAY_LINE_BYTES)."""
    return [f"{n}: {line.strip()[:70]}" for n, line in enumerate(text.splitlines(), 1)
            if len(line.encode()) > ESSAY_LINE_BYTES and RE_SESSION_HEAD.search(line)]


def _shared_long_lines(agent_text: str, archive_text: str) -> set[str]:
    """Один и тот же длинный текст в двух файлах = копия, а не перенос."""
    def longs(text: str) -> set[str]:
        return {line.strip() for line in text.splitlines() if len(line.strip()) > 160}

    return longs(agent_text) & longs(archive_text)


def test_readme_keeps_no_session_reports():
    """README — про игру, а не про то, какую сессию что чинило (C4 тот же класс, что AGENT).

    Заголовок вида «## ✨ Что добавилось (сессия 34)» — это журнал: он устаревает молча и
    дублирует живые разделы (память/API/механики), где поведение описано честно. """
    text = README.read_text(encoding="utf-8")
    heads = RE_HEAD_SESSION.findall(text)
    assert not heads, ("README снова отчитывается сессией (перенеси отчёт в AGENT_ARCHIVE.md,"
                       " а поведение оставь по разделам): " + "; ".join(h.strip() for h in heads))
    kb = README.stat().st_size / 1024
    assert kb <= README_BUDGET_KB, f"README разросся до {kb:.0f} КБ (бюджет {README_BUDGET_KB})"


def test_agent_md_fits_the_reading_budget():
    """Живой AGENT.md обязан читаться за одну сессию — это и есть смысл C4."""
    kb = AGENT.stat().st_size / 1024
    assert kb <= AGENT_BUDGET_KB, (
        f"AGENT.md разросся до {kb:.0f} КБ (бюджет {AGENT_BUDGET_KB}). Правило «читай первым»"
        " снова неисполнимо: отчёты сессий — в AGENT_ARCHIVE.md, здесь только действующие"
        " правила/архитектура.")


def test_no_essays_left_in_live_doc():
    """Ни одного эссе «Сессия NN» длиннее строки правила в AGENT.md (они в архиве)."""
    bad = _essays(AGENT.read_text(encoding="utf-8"))
    assert not bad, ("живые разделы AGENT.md снова держат отчёт сессии вместо правила "
                     "(перенеси обоснование в AGENT_ARCHIVE.md):\n" + "\n".join(bad))


def test_archive_holds_what_agent_parcels():
    """Перенесённое в архив на месте и дословно (иначе «перенос» = потеря истории)."""
    arc = ARCHIVE.read_text(encoding="utf-8")
    for marker in ("Сессия 40, п.13", "Сессия 40, п.3", "Известные фиксы", "Сессии 6–10",
                   "Тестовый набор: состав и история", "Сводка сессии 38", "Сессия 40, п.12",
                   "Что добавилось (сессия 34)", "Стабильность (сессия 36", "Ружья Чехова"):
        assert marker in arc, f"архив потерял перенесённый раздел/эссе: {marker!r}"
    # «Цепочка обязанностей» — ЖИВОЕ правило, оно осталось в AGENT.md (не история)
    assert "Цепочку обязанностей" in AGENT.read_text(encoding="utf-8")
    # перенесённые эссе обязаны быть длинными строками именно там (иначе их не перенесли)
    assert any(len(line.encode()) > ESSAY_LINE_BYTES and "Сессия 40, п.13" in line
               for line in _lines(ARCHIVE)), "эссе п.13 в архиве не выглядит перенесённым"


def test_agent_links_point_to_real_archive_sections():
    """Ссылка «см. в архиве» не бывает пустой: раздел, куда шлёт AGENT.md, существует."""
    agent = AGENT.read_text(encoding="utf-8")
    arc = ARCHIVE.read_text(encoding="utf-8")
    heads = {h.strip(" #") for h in re.findall(r"(?m)^#{1,4} .*", arc)}
    mentioned = re.findall(r"`AGENT_ARCHIVE\.md`[^\n]{0,120}", agent)
    assert mentioned, "AGENT.md разучился слать читателя в архив — ссылки потерялись"
    for sent in mentioned:
        wanted = re.findall(r"«([^»]+)»", sent)
        for w in wanted:
            key = w.rstrip("….").split("(")[0].strip()
            if key in ("Сессия 36",):     # ссылка на старую сводку, не на заголовок
                continue
            hit = any(key.split(",")[0].strip() in h for h in heads) or key in arc
            assert hit, f"AGENT.md шлёт в несуществующий раздел архива: «{w}» из «{sent.strip()[:90]}»"


def test_history_is_not_duplicated_across_docs():
    """«Ни одного факта в двух местах»: длинные строки живых документов не кочуют копиями."""
    live = [AGENT.read_text(encoding="utf-8"), README.read_text(encoding="utf-8")]
    arc = ARCHIVE.read_text(encoding="utf-8")
    shared = set()
    for text in live:
        shared |= _shared_long_lines(text, arc)
    assert not shared, ("один и тот же текст живёт в двух файлах (копия вместо переноса):\n"
                        + "\n".join(sorted(s[:90] for s in shared)[:5]))
    # и между собой живые файлы не обмениваются абзацами (AGENT и README)
    both = _shared_long_lines(live[0], live[1])
    allowed = ("ТОЛЬКО универсальные механики", "Общие базовые возможности",
               "Код только ПРОВЕРЯЕТ возможность")   # канон трёх законов — осознанное исключение
    bad = [s[:90] for s in both if not any(k in s for k in allowed)]
    assert not bad, "факт продублирован между README и AGENT (кроме канона трёх законов):\n" \
        + "\n".join(bad[:5])


def test_archive_is_out_of_numbers_registry():
    """Устаревшая цифра в истории не «чинится»: архив вне реестра doc_figures (граница C1/C4)."""
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import doc_figures as df

    assert "AGENT_ARCHIVE.md" not in df.DOCS, (
        "архив попал под сверку чисел — --sync начнёт переписывать отчёты прошедших сессий")
    docs = df.read_docs(ROOT)
    assert "AGENT_ARCHIVE.md" not in docs


# ─────────────────── пробы: проверки не декоративные ───────────────────

def test_probe_checks_react_to_a_returned_essay_and_to_a_copy():
    """Без проб C4 закрывается «на слово»: вернули эссе / задвоили текст — ловим.

    Пробы на строках в памяти: живые документы тест НЕ пишет и НЕ удаляет."""
    essay = "- **Сессия 99 (мир «Тестовый»): симптом.** " + "причина " * 400
    assert _essays("## Раздел\n" + essay), "эссе «Сессия 99» не поймано — проверка декоративная"
    assert _essays("## Раздел\n- **Сессия 99:** короткое живое правило.") == [], \
        "короткая строка правила считается эссе"
    long_line = ("- Правило: " + "содержание " * 60).strip()
    assert _shared_long_lines(long_line + "\nx", long_line + "\ny") == {long_line}, \
        "дублирующийся текст между файлами не пойман"
    assert _shared_long_lines(long_line, "тот же смысл, но другими словами") == set()
    assert RE_HEAD_SESSION.search("## ✨ Что добавилось (сессия 34)"), \
        "проверка заголовков-отчётов README не ловит то, ради чего написана"
