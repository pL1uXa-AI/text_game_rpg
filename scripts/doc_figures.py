# -*- coding: utf-8 -*-
"""doc_figures.py — один источник правды для ПОВТОРЯЮЩИХСЯ чисел в документации (аудит 41, C1).

Было: число pytest-тестов / рассказчиков / сюжетов / подсказок жанров переписывалось в README,
AGENT и ROADMAP ВРУЧНУЮ каждой сессией — и расходилось: ROADMAP жил с «534», AGENT в одном месте
говорил «795», в другом — «388» (то есть файл врал сам себе), README — «app.openapi() — 49».
Правильное число знает только прогон (`pytest --collect-only`, `app.openapi()`,
`len(NARRATOR_PRESETS)`), а ручная синхронизация пяти строк — ровно то, что сессия забывает.

Механизм (вместо «ещё одной правки цифр»):
  * реестр FIGURES — что измерять (реальность: код/диск/pytest) и ГДЕ это заявлено в доках
    (якорные регулярки, у каждой ровно одна числовая группа);
  * по умолчанию — сверка: заявления с реальностью, код 1 + список расхождений;
  * `--sync`  — переписывает числа (и падеж слова «тест») само, во всех .md из DOCS, переносы строк
    файла сохраняются;
  * `--print` — только замеры (в том числе пути/операции OpenAPI).
То есть сессия, добавившая тесты, больше не ищет глазами «куда вписать число»: она гоняет
`python -X utf8 scripts/doc_figures.py --sync`. За согласованность отвечает
`tests/test_session59_docs_figures.py::test_docs_figures_agree` (тот же путь, что требовал
ROADMAP [docs-цифры]).

Честные границы (механизм не обещает лишнего):
  * под проверкой ТОЛЬКО заявления из реестра. Новое МЕСТО с числом надо зарегистрировать одной
    строкой, иначе оно останется неконтролируемым; исторические сводки сессий НЕ регистрируются
    сознательно — устаревшая цифра в отчёте «что было» часть истории (её не правят, см. AGENT.md
    «📊 СОСТОЯНИЕ И ИСТОРИЯ»); поэтому якоря берутся с ОБОРМЛЕНИЕМ строки (комментарий, ячейка
    таблицы, маркер markdown), а не просто «цифра + слово»: голый поиск «8 рассказчиков» красил бы
    сводку сессии 33, где он исторически верен;
  * каждое заявление обязано находиться РОВНО один раз — иначе претензия «найдено N раз, надо 1»
    (док переписали, реестр забыли, или наоборот);
  * сверяется и падеж («795 тестов», а не «795 теста») — слово правит тот же `--sync`;
  * что нельзя измерить (длительность прогона «63 с», размеры файлов в КБ) в реестр не заносим:
    проверяемое заявление обязано быть проверяемым.

Запуск:  python -X utf8 scripts/doc_figures.py [--check | --sync | --print]   (код 0 = всё честно)
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ("README.md", "AGENT.md", "ROADMAP.md")

# слово «тест» в разных падежах (числа в доках живут в этих трёх формах)
TEST_WORD = r"тест[аов]{0,2}"
TEST_WORD_AT = re.compile(r"\s*(" + TEST_WORD + r")")     # « 795 теста» → «теста» с пробелом
TEST_WORD_SUB = re.compile(TEST_WORD)                     # только слово, пробелы не трогает


# ────────────────────────── реальность (измерения) ──────────────────────────

def measure_tests(root: Path = ROOT) -> int:
    """Сколько тестов реально собирает pytest — тот же замер, что делает сессия руками."""
    r = subprocess.run([sys.executable, "-X", "utf8", "-m", "pytest", "--collect-only", "-q"],
                       cwd=str(root), capture_output=True, text=True, encoding="utf-8",
                       timeout=900)
    out = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"(\d+) tests? collected", out)
    if m:
        return int(m.group(1))
    per_file = re.findall(r"(?m)^tests/\S+\.py: (\d+)$", out)
    if per_file:
        return sum(int(x) for x in per_file)
    raise SystemExit(f"doc_figures: не удалось посчитать тесты (rc={r.returncode}):\n{out[-600:]}")


def measure_openapi(root: Path = ROOT) -> tuple[int, int]:
    """(пути, операции) из схемы FastAPI — тот самый замер, про который README врал «49» (C2)."""
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from backend.app import app  # noqa: PLC0415 — тяжёлый импорт нужен только этому замеру

    spec = app.openapi()
    methods = ("get", "post", "put", "patch", "delete")
    ops = sum(len([m for m in item if m in methods]) for item in spec["paths"].values())
    return len(spec["paths"]), ops


def measure_plots(root: Path = ROOT) -> tuple[int, int]:
    """(комплектных сюжетов, своих) на диске — plots/*.js по соглашению PLOTS.md."""
    return (len(sorted((root / "plots" / "system").glob("*.js"))),
            len(sorted((root / "plots" / "user").glob("*.js"))))


def measure_narrators(root: Path = ROOT) -> int:
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from backend.narrators_loader import NARRATOR_PRESETS  # noqa: PLC0415

    return len(NARRATOR_PRESETS)


def measure_genres(root: Path = ROOT) -> int:
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from backend.narrator_data import GENRE_HINTS  # noqa: PLC0415

    return len(GENRE_HINTS)


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    """Русское согласование: 799 тестов, 801 тест, 802 теста."""
    mod100, mod10 = n % 100, n % 10
    if 11 <= mod100 <= 14:
        return many
    if mod10 == 1:
        return one
    if 2 <= mod10 <= 4:
        return few
    return many


class Figure:
    """Одна измеримая величина: чем меряется + где заявлена в документах."""

    def __init__(self, label: str, measure, claims: tuple[str, ...],
                 plural: tuple[str, str, str] | None = None, word: str | None = None):
        self.label = label
        self.measure = measure
        self.claims = claims        # якорные регулярки с ОДНОЙ числовой группой
        self.plural = plural        # (тест/теста/тестов) — если падеж слова тоже правим
        # основа слова, к которому приклеено число: по умолчанию «тест» (исторически первый
        # случай), для путей/операций — «пут»/«операц» (окончание добирается [а-яё]{0,3})
        base = re.escape(word) if word else TEST_WORD
        self.word_at = re.compile(r"\s*(" + base + r"[а-яё]{0,3})")     # найти текущую форму
        self.word_sub = re.compile(base + r"[а-яё]{0,3}")               # заменить форму

    def word_for(self, n: int) -> str | None:
        return plural_ru(n, *self.plural) if self.plural else None


FIGURES: dict[str, Figure] = {
    # Число pytest-тестов: README (дерево проекта), AGENT («Как тестировать» + «Контроль
    # качества»), ROADMAP 📊. ИСТОРИЧЕСКИЕ сводки («Итоговые проверки: 534 теста», «итого 534»)
    # в реестре НЕ заявлены — и не должны: править отчёт прошедшей сессии = фальсификация.
    "tests": Figure(
        "pytest-тестов",
        measure_tests,
        (
            r"^├── tests/\s+# pytest \((\d{2,4})\s*" + TEST_WORD + r"\)",         # README: дерево проекта
            r"^#\s+(\d{2,4})\s*" + TEST_WORD + r":",                             # AGENT: как тестировать
            r"- `python -X utf8 -m pytest` → \*\*(\d{2,4})\s*" + TEST_WORD + r"\*\*",   # AGENT: КУК
            r"\*\*(\d{2,4})\s*" + TEST_WORD + r",\s*0 падений\*\*",              # ROADMAP: 📊
        ),
        plural=("тест", "теста", "тестов"),
    ),
    # предустановленные рассказчики = plots/narrators/*.js (len(NARRATOR_PRESETS))
    "narrators": Figure(
        "рассказчиков",
        measure_narrators,
        (
            r"\), (\d{1,3}) рассказчиков",                       # ROADMAP: 📊
            r"# (\d{1,3}) из plots/narrators/\*\.js",            # AGENT: как тестировать
            r"^- \*\*(\d{1,3}) предустановленных\*\*",            # README: раздел «Рассказчик»
            r"; (\d{1,3}) предустановленных сеются",             # AGENT: схема БД
            r"api/narrators\s+# (\d{1,3}) предустановленных",    # AGENT: smoke-команда
        ),
    ),
    # сюжеты: комплектные и свои (файлы на диске)
    "plots_system": Figure(
        "комплектных сюжетов",
        lambda root: measure_plots(root)[0],
        (r"\((\d{1,3}) комплектных", r"по-прежнему (\d{1,3}) готовых"),
    ),
    "plots_user": Figure(
        "своих сюжетов",
        lambda root: measure_plots(root)[1],
        (r"по-прежнему \d+ готовых \(`plots/system`\) и (\d{1,3}) своих",),
    ),
    # жанровые подсказки-справочник (GENRE_HINTS)
    "genres": Figure(
        "жанровых подсказок",
        measure_genres,
        (r"(\d{1,3}) жанровые подсказки", r"`GENRE_HINTS` \((\d{1,3}) шт"),
    ),
    # пути и операции OpenAPI (аудит 41, C2, сессия 60): README заявляет оба числа, и оба
    # меряются схемой приложения, а не глазами. Падеж правит тот же --sync:
    # «50 путей / 64 операции», при 61 — «61 операция».
    "openapi_paths": Figure(
        "путей OpenAPI",
        lambda root: measure_openapi(root)[0],
        (r"`app\.openapi\(\)` — \*\*(\d{1,3})\s*пут",),
        plural=("путь", "пути", "путей"), word="пут",
    ),
    "openapi_ops": Figure(
        "операций OpenAPI",
        lambda root: measure_openapi(root)[1],
        (r"`app\.openapi\(\)` — \*\*\d{1,3}\s*пут[а-яё]{0,3}\s*/\s*(\d{1,3})\s*операц",),
        plural=("операция", "операции", "операций"), word="операц",
    ),
}


def measure_all(root: Path = ROOT) -> dict[str, int]:
    return {name: fig.measure(root) for name, fig in FIGURES.items()}


def read_docs(root: Path = ROOT) -> dict[str, str]:
    return {n: (root / n).read_text(encoding="utf-8") for n in DOCS if (root / n).exists()}


class Claim:
    """Найденное в документе заявление о числе из реестра."""

    def __init__(self, doc: str, lineno: int, figure: str, pattern: str,
                 value: int, word: str | None, line: str):
        self.doc, self.lineno, self.figure, self.pattern = doc, lineno, figure, pattern
        self.value, self.word, self.line = value, word, line

    def where(self) -> str:
        return f"{self.doc}:{self.lineno}"

    def snippet(self) -> str:
        return self.line.strip()[:90]


def collect_claims(root: Path = ROOT, docs: dict[str, str] | None = None) -> list[Claim]:
    """Все зарегистрированные заявления (в том числе расходящиеся с реальностью)."""
    docs = read_docs(root) if docs is None else docs
    out: list[Claim] = []
    for doc, text in docs.items():
        for lineno, line in enumerate(text.splitlines(), 1):
            for fig_name, fig in FIGURES.items():
                for pat in fig.claims:
                    m = re.search(pat, line)
                    if not m:
                        continue
                    word = None
                    if fig.plural:
                        wm = fig.word_at.match(line, m.end(1))
                        word = wm.group(1) if wm else None
                    out.append(Claim(doc, lineno, fig_name, pat, int(m.group(1)), word, line))
    return out


def check(root: Path = ROOT, values: dict[str, int] | None = None,
          docs: dict[str, str] | None = None) -> list[str]:
    """Список претензий (пусто = документы честные)."""
    values = measure_all(root) if values is None else values
    claims = collect_claims(root, docs)
    problems: list[str] = []
    seen: dict[tuple[str, str], int] = {}
    for c in claims:
        seen[(c.figure, c.pattern)] = seen.get((c.figure, c.pattern), 0) + 1
        fig = FIGURES[c.figure]
        real = values[c.figure]
        if c.value != real:
            problems.append(f"{c.where()}: {fig.label} заявлено {c.value}, реально {real} "
                            f"→ «{c.snippet()}»")
        want = fig.word_for(real)
        if want and c.word and c.word != want:
            problems.append(f"{c.where()}: {fig.label} — падеж «{c.word}» при числе {real}, "
                            f"надо «{want}» → «{c.snippet()}»")
    for fig_name, fig in FIGURES.items():
        for pat in fig.claims:
            n = seen.get((fig_name, pat), 0)
            if n != 1:
                problems.append(f"реестр: заявление «{fig_name}» ({pat!r}) найдено {n} раз, надо 1 "
                                f"— док переписали, а реестр забыли (или наоборот)")
    return problems


def _fix_line(line: str, fig: Figure, pat: str, real: int) -> str:
    """Заменить заявление о числе (и падеж слова следом) на реальное значение."""
    m = re.search(pat, line)
    if not m:
        return line
    s, e = m.span(1)
    digits = str(real)
    new = line[:s] + digits + line[e:]
    after = s + len(digits)                     # позиция сразу за новым числом
    if fig.plural:
        new = new[:after] + fig.word_sub.sub(fig.word_for(real) or "", new[after:], count=1)
    return new


def sync(root: Path = ROOT, values: dict[str, int] | None = None) -> list[str]:
    """Переписать числа (и падеж) по реальным значениям. Возвращает изменённые файлы."""
    values = measure_all(root) if values is None else values
    changed: list[str] = []
    for name in DOCS:
        path = root / name
        if not path.exists():
            continue
        raw = path.read_bytes()
        # переносы файла сохраняем: доки проекта LF, но правка «всухую» не обязана их менять
        crlf = raw.count(b"\r\n") > 0
        text = raw.decode("utf-8")
        lines = text.splitlines(keepends=True)
        for fig_name, fig in FIGURES.items():
            real = values[fig_name]
            for pat in fig.claims:
                for i, line in enumerate(lines):
                    fixed = _fix_line(line, fig, pat, real)
                    if fixed != line:
                        lines[i] = fixed
        new_text = "".join(lines)
        if new_text != text:
            body = new_text.replace("\r\n", "\n")
            path.write_bytes((body.replace("\n", "\r\n") if crlf else body).encode("utf-8"))
            changed.append(name)
    return changed


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Сверка/синхронизация чисел в README/AGENT/ROADMAP")
    ap.add_argument("--sync", action="store_true", help="переписать числа по реальности")
    ap.add_argument("--check", action="store_true", help="только сверка (по умолчанию)")
    ap.add_argument("--print", dest="show", action="store_true", help="только замеры")
    args = ap.parse_args(argv)

    if args.show:
        paths, ops = measure_openapi(ROOT)
        vals = measure_all(ROOT)
        print(f"openapi_paths={paths} openapi_ops={ops} "
              + " ".join(f"{k}={v}" for k, v in vals.items()))
        return 0

    values = measure_all(ROOT)
    if args.sync:
        changed = sync(ROOT, values)
        print("doc_figures: синхронизировано:", ", ".join(changed) or "ничего (уже честно)")
    bad = check(ROOT, values)
    if bad:
        print("❌ ЧИСЛА В ДОКАХ РАСХОДЯТСЯ С РЕАЛЬНОСТЬЮ:")
        for b in bad:
            print("  -", b)
        print("\nЧинится одной командой: python -X utf8 scripts/doc_figures.py --sync")
        return 1
    print("✅ doc_figures: все заявления совпадают с реальностью:",
          ", ".join(f"{FIGURES[k].label}={v}" for k, v in values.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
