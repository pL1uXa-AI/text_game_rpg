# -*- coding: utf-8 -*-
"""Аудит 41, C1 (сессия 59): повторяющиеся числа в документах больше не сводят вручную.

Было: одно и то же число (тесты / рассказчики / сюжеты / подсказки жанров) живёт в README,
AGENT и ROADMAP в нескольких строках, и каждая сессия переписывала их глазами. Итог из
отчёта 41: реально 795, в ROADMAP — «534» (три места), в AGENT — «795» в одном месте и
«388» в другом (файл врал сам себе), README — «pytest (795 теста)» с неверным падежом.
Плюс цифра тестов вообще не имела ни одного сторожа — расхождение обнаруживалось только
очередной сессией «на глаз» (и она же приносила новую порцию расхождений).

Механизм (просил ROADMAP [docs-цифры] — не «ещё раз поправить», а перестать править):
`scripts/doc_figures.py` — реестр FIGURES (что мерить в коде/на диске/pytest ↔ где это
заявлено в доках) + `--sync`, который переписывает числа и падеж сам. Проверка:

1. `test_docs_figures_agree` — все зарегистрированные заявления сходятся с реальностью
   (реальные замеры: `pytest --collect-only`, `len(NARRATOR_PRESETS)`, файлы `plots/*`,
   `len(GENRE_HINTS)`). Это и есть обещанный тест.
2. CLI честный: `python scripts/doc_figures.py` (без флага) обязан выйти в 0 — иначе
   следующая сессия получит красное сразу, а не через неделю.
3. Реестр не протух: КАЖДОЕ заявление находится ровно один раз, и каждый из трёх доков
   участвует в сверке — иначе док переписали (или число переехало в историческую сводку),
   а реестр молчит.
4. Историю не трогаем: ни одно заявление не попадает в «Сводка сессии NN»/«Спринт NN»/
   «ЛОГ СПРИНТОВ» — устаревшая цифра в отчёте прошедшей сессии часть истории и правке
   не подлежит (раздел AGENT.md «📊 СОСТОЯНИЕ И ИСТОРИЯ»).
5. Деградационные пробы (иначе 1–3 ничего не проверяют): подменённое в памяти число красит
   `check`, «795 теста» при 795 красит падеж, потерянное/продублированное заявление красит
   реестр, а `sync` чинит поломку минимальной правкой и не трогает переносы строк файла.
   Пробы работают на КОПИЯХ в tmp_path/в памяти — живые доки тест НЕ пишет.

Замер OpenAPI (50 путей / 64 операций) с C2 (сессия 60) тоже под реестром: заявления
`openapi_paths`/`openapi_ops` живут в README «🗂 API», а полноту перечисления путей
сторожит `tests/test_session60_openapi_documented.py`.

Запуск: "…\\3.12.10\\python.exe" -X utf8 -m pytest tests/test_session59_docs_figures.py -q
"""
from __future__ import annotations

import functools
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import doc_figures as df  # noqa: E402

SCRIPT = ROOT / "scripts" / "doc_figures.py"
# Заголовки «замороженной» истории: цифры там устаревать ИМЕЮТ ПРАВО.
HISTORY_HEADS = ("Сводка сессии", "Спринт ", "ЛОГ СПРИНТОВ")


@functools.lru_cache(maxsize=1)
def real() -> dict[str, int]:
    """Замеры один раз на модуль: measure_tests поднимает вложенный pytest --collect-only."""
    return df.measure_all(ROOT)


def _heading_of(text: str, lineno: int) -> str:
    """Заголовок раздела, которому принадлежит строка lineno (1-базная)."""
    head = ""
    for i, line in enumerate(text.splitlines()[:lineno], 1):
        if re.match(r"^#{2,4} ", line):
            head = line
    return head


# ───────────────────────── 1–2. правдивость чисел ─────────────────────────

def test_docs_figures_agree():
    """Каждое зарегистрированное число в README/AGENT/ROADMAP = реальность (C1)."""
    values = real()
    assert values["tests"] > 700, f"замер теста сломался: {values['tests']}"
    problems = df.check(ROOT, values)
    assert not problems, "числа в документах расходятся:\n" + "\n".join(problems)


def test_cli_check_exits_zero():
    """Тот же контроль из командной строки (и из CI через pytest) — код 0."""
    r = subprocess.run([sys.executable, "-X", "utf8", str(SCRIPT)], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8", timeout=900)
    assert r.returncode == 0, f"doc_figures --check: rc={r.returncode}\n{r.stdout[-1200:]}\n{r.stderr[-400:]}"
    assert "совпадают с реальностью" in r.stdout


# ───────────────────────── 3–4. реестр не протух ─────────────────────────

def test_every_claim_is_found_exactly_once():
    """Ни одного «потерянного» заявления: док переписали, а реестр забыли — находим сразу."""
    docs = df.read_docs(ROOT)
    seen: dict[tuple[str, str], int] = {}
    for c in df.collect_claims(ROOT, docs):
        seen[(c.figure, c.pattern)] = seen.get((c.figure, c.pattern), 0) + 1
    missing = [f"{f}: {p!r}" for f, fig in df.FIGURES.items() for p in fig.claims
               if seen.get((f, p), 0) != 1]
    assert not missing, "заявление в доках не найдено (или найдено дважды):\n" + "\n".join(missing)


def test_figures_are_claimed_in_all_three_docs():
    """Каждый из трёх доков УЧАСТВУЕТ в сверке — иначе «согласованность трёх документов» пуста."""
    docs = df.read_docs(ROOT)
    assert set(docs) == set(df.DOCS), f"док из реестра пропал с диска: {set(df.DOCS) - set(docs)}"
    claimed = {c.doc for c in df.collect_claims(ROOT, docs)}
    assert claimed == set(df.DOCS), f"в сверку чисел не попадает ни одного заявления из: {set(df.DOCS) - claimed}"
    # а число тестов — обязательный общий знаменатель (именно оно и расходилось всегда)
    tests_docs = {c.doc for c in df.collect_claims(ROOT, docs) if c.figure == "tests"}
    assert tests_docs == set(df.DOCS), f"число тестов заявлено не во всех документах: {tests_docs}"


def test_no_claim_lives_in_frozen_history():
    """Заявления не должны залезать в исторические сводки — их правка = фальсификация."""
    docs = df.read_docs(ROOT)
    bad = []
    for c in df.collect_claims(ROOT, docs):
        head = _heading_of(docs[c.doc], c.lineno)
        if any(h in head for h in HISTORY_HEADS):
            bad.append(f"{c.where()} [{c.figure}] в разделе «{head.strip()}»")
    assert not bad, ("число из истории попало под сверку (нужен другой якорь или снять заявление):\n"
                     + "\n".join(bad))


# ───────────────────────── 5. деградационные пробы ─────────────────────────

def test_wrong_number_and_wrong_case_are_caught():
    """Враньё в число и в падеж обязано краситься — иначе проверка 1 декоративная."""
    docs = df.read_docs(ROOT)
    vals = real()
    # портим README: число тестов «как в ROADMAP до C1» (534) — ловится расхождение
    broken = dict(docs)
    broken["README.md"] = re.sub(r"(# pytest \()\d+", r"\g<1>534", broken["README.md"])
    problems = df.check(ROOT, vals, broken)
    assert any("README.md" in p and "pytest-тестов" in p for p in problems), \
        f"расхождение 534 vs {vals['tests']} не поймано: {problems}"
    # падеж: при текущем числе ставим заведомо неверную форму (795 тест**а** / 801 тест**ов**)
    bad_case = dict(docs)
    want = df.plural_ru(vals["tests"], "тест", "теста", "тестов")
    wrong = "тестов" if want != "тестов" else "теста"
    bad_case["README.md"] = re.sub(r"(# pytest \(%d )%s" % (vals["tests"], want),
                                   r"\g<1>" + wrong, bad_case["README.md"])
    assert bad_case["README.md"] != docs["README.md"], "проба падежа не сработала"
    problems = df.check(ROOT, vals, bad_case)
    assert any("падеж" in p for p in problems), \
        f"неверный падеж проходит как честный: {problems}"


def test_lost_claim_is_reported():
    """Если строку с числом удалили/переписали, реестр обязан об этом сказать (а не «ок»)."""
    docs = df.read_docs(ROOT)
    # форма слова зависит от числа (824 «теста», 816 «тестов»), поэтому пробуем не подстрокой,
    # а регуляркой: иначе тест краснел бы не из-за сломанного реестра, а из-за падежа (C3)
    docs["ROADMAP.md"] = re.sub(r"тест[аов]{0,2},\s*0 падений", "тестов", docs["ROADMAP.md"])
    problems = df.check(ROOT, real(), docs)
    assert any("найдено 0 раз" in p for p in problems), \
        f"потерянное заявление не замечено: {problems}"


def test_sync_fixes_minimally_and_keeps_line_endings(tmp_path):
    """`--sync` чинит только строку с числом, форматирование и переносы не трогает."""
    for name in df.DOCS:
        (tmp_path / name).write_bytes((ROOT / name).read_bytes())
    road = tmp_path / "ROADMAP.md"
    # ломаем ROADMAP (число тестов) и AGENT (число рассказчиков) относительно РЕАЛЬНОСТИ,
    # а не захардкоженной цифры: иначе проба краснеет/зеленеет в зависимости от дня запуска
    values = real()
    wrong_tests, wrong_narr = values["tests"] - 261, values["narrators"] - 1
    broken = re.sub(r"\*\*\d+\s*" + df.TEST_WORD + r",\s*0 падений\*\*",
                    f"**{wrong_tests} теста, 0 падений**", road.read_text(encoding="utf-8"))
    assert broken != road.read_text(encoding="utf-8"), "проба не сломала ROADMAP"
    agent_broken_src = re.sub(rf"# {values['narrators']} из plots/narrators",
                              f"# {wrong_narr} из plots/narrators",
                              (ROOT / "AGENT.md").read_text(encoding="utf-8"))
    (tmp_path / "AGENT.md").write_text(agent_broken_src, encoding="utf-8", newline="\n")
    road.write_text(broken, encoding="utf-8", newline="\r\n")      # «файл с CRLF» — проба переносов

    assert df.check(ROOT, values, df.read_docs(tmp_path)), \
        "сломанные копии выглядят честными — проверка пуста"
    changed = df.sync(tmp_path, values)
    assert {"AGENT.md", "ROADMAP.md"} <= set(changed), f"починено не то: {changed}"
    assert not df.check(ROOT, values, df.read_docs(tmp_path)), "после --sync остались расхождения"
    # минимум правки: в каждой починенной копии изменена ровно строка с числом
    for name, broken_src in (("AGENT.md", agent_broken_src), ("ROADMAP.md", broken)):
        old_lines = broken_src.splitlines()
        new_lines = (tmp_path / name).read_bytes().decode("utf-8").splitlines()
        assert len(old_lines) == len(new_lines), f"{name}: sync меняет число строк"
        diff = [i for i, (a, b) in enumerate(zip(old_lines, new_lines)) if a != b]
        assert len(diff) == 1, f"{name}: изменено больше одной строки: {diff}"
    row = next(x for x in road.read_bytes().decode("utf-8").splitlines() if "0 падений" in x)
    assert str(wrong_tests) not in row and f"{values['tests']}" in row, f"строка 📊 не почищена: {row[:120]}"
    assert road.read_bytes().count(b"\r\n") == road.read_bytes().count(b"\n"), \
        "sync перебил переносы строк файла"
    # и идемпотентен: второй прогон не пишет ничего
    assert df.sync(tmp_path, values) == [], "sync не идемпотентен (пилит файлы каждый раз)"


def test_plural_ru():
    """Согласование, на котором уже врал README: 795 тестов, 801 тест, 802 теста."""
    assert df.plural_ru(795, "тест", "теста", "тестов") == "тестов"
    assert df.plural_ru(801, "тест", "теста", "тестов") == "тест"
    assert df.plural_ru(802, "тест", "теста", "тестов") == "теста"
    assert df.plural_ru(811, "тест", "теста", "тестов") == "тестов"


def test_measures_match_the_code_they_claim():
    """Замеры обязаны мерить, а не возвращать константу (иначе «согласие» куплено дёшево)."""
    from backend.narrator_data import GENRE_HINTS
    from backend.narrators_loader import NARRATOR_PRESETS

    assert df.measure_narrators(ROOT) == len(NARRATOR_PRESETS) == real()['narrators']
    assert df.measure_genres(ROOT) == len(GENRE_HINTS) == real()['genres']
    system, user = df.measure_plots(ROOT)
    assert system == len(list((ROOT / "plots" / "system").glob("*.js")))
    assert user == len(list((ROOT / "plots" / "user").glob("*.js")))
    paths, ops = df.measure_openapi(ROOT)
    assert paths > 0 and ops >= paths, f"замер OpenAPI сломался: {paths}/{ops}"


def test_openapi_numbers_are_a_registered_figure():
    """C2 закрыт (сессия 60): пути/операции OpenAPI ВЕДЬ в реестре, а не только в `--print`.

    Прежняя проба («заявлений сознательно нет») сторожит границу между пунктами C1 и C2;
    когда C2 закрыли, она покраснела бы — поэтому перевёрнута: теперь реестр ОБЯЗАН держать
    оба openapi-заявления, иначе цифра снова уйдёт в ручную правку (а именно так она и совралась).
    """
    assert df.FIGURES["openapi_paths"].claims and df.FIGURES["openapi_ops"].claims, \
        "заявления app.openapi() сняты с реестра — см. C2 (README врал про «49»)"
    assert df.measure_openapi(ROOT)[0] > 0
