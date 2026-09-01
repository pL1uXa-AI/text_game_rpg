# -*- coding: utf-8 -*-
"""check_typos.py — проверка известных русских опечаток (сессия 38, пункт C10).

Два существующих сканера ловят «иностранное» (CJK-вставки) и латинские омоглифы внутри
кириллицы (`check_frontend.py`), но русскую орфографию — нет: «ИТОГ КВЕТА» TYPO-OK, «промпт уощён» TYPO-OK,
«нормилзация директив» TYPO-OK, «персонаж останается» TYPO-OK — всё
это выглядит почти чисто, глаз соскальзывает. Аудит сессии 38 нашёл шесть таких мест, и
одно из них (`narrator.py`, правило 33) попало прямо в СИСТЕМНЫЙ ПРОМПТ, то есть каждый
ход читает модель.

Дешёвое закрытие класса: список «ошибка → правильно» + проход по файлам проекта.
Список пополняется каждой найденной опечаткой (регрессия, а не «разовая правка»).

Запуск:  python -X utf8 scripts/check_typos.py [путь …]      (код 0 = ок)
Исключение строки: маркер `TYPO-OK` в той же строке — так в доках цитируются сами
исправленные опечатки (иначе документация о баге не смогла бы существовать).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# известная опечатка → как правильно (подстроки, регистр не важен)
# Строки словаря помечены TYPO-OK: иначе сканер находил «опечатки» в собственном словаре.
TYPOS: dict[str, str] = {
    # найдены аудитом сессии 38 и исправлены
    "итог квета": "итог квеста",              # в тексте системного промпта (правило 33) TYPO-OK
    "уощён": "усечён",                        # лог ярусов промпта TYPO-OK
    "нормилзация": "нормализация",            # лог аудита директив TYPO-OK
    "останается": "останется",                # лог создания мира TYPO-OK
    "битесь в один слот": "конкурируя за один слот",   # docstring bg.py TYPO-OK
    "очередь фонова": "очередь фоновых агентов",        # ROADMAP TYPO-OK
    "отдельная стужа": "отдельная ступень",             # ROADMAP TYPO-OK
    "каждые npc": "каждый NPC",                          # README, первая строка TYPO-OK
    # типичные «мягкие» ошибки, которые встречаются в этом проекте повторно
    "выполняеться": "выполняется",            # TYPO-OK
    "используеться": "используется",          # TYPO-OK
    "копитсяя": "копится",                    # TYPO-OK
    "необхидимо": "необходимо",               # TYPO-OK
    "сохронить": "сохранить",                 # TYPO-OK
}

# где ищем (тексты проекта; data/ и кэши — не трогаем)
SCAN_GLOBS = [
    "backend/**/*.py",
    "tests/**/*.py",
    "scripts/**/*.py",
    "frontend/*.js",
    "frontend/*.html",
    "plots/**/*.js",
    "*.md",
    "*.bat",
    "*.toml",
    "*.ini",
    ".github/workflows/*.yml",
    ".env.example",
]
SKIP_MARK = "TYPO-OK"
# «ошибка» внутри исправленной формулировки legitimately выглядит как подстрока
# (например «сохранить» содержит «сохронить» только при опечатке — не конфликтует)


def _hits_line(line: str) -> list[tuple[str, str]]:
    low = line.lower()
    out = []
    for bad, good in TYPOS.items():
        if bad in low:
            out.append((bad, good))
    return out


def check_file(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    errors = []
    for n, line in enumerate(text.splitlines(), 1):
        if SKIP_MARK in line:
            continue
        for bad, good in _hits_line(line):
            errors.append(f"{path.relative_to(ROOT)}:{n}: «{bad}» → {good}: "
                          f"{line.strip()[:90]}")
    return errors


def collect(paths: list[str]) -> list[Path]:
    if paths:
        return [Path(p) for p in paths]
    out: list[Path] = []
    for pat in SCAN_GLOBS:
        out.extend(sorted(ROOT.glob(pat)))
    # архив аудита — это ЦИТАТЫ опечаток, а не код: помечать каждую строку бессмысленно
    skip = ("scripts/check_typos.py", "ISSUES_AUDIT_")
    return [p for p in out if p.is_file() and "__pycache__" not in str(p)
            and not any(s in str(p).replace("\\", "/") for s in skip)]


def main(argv: list[str]) -> int:
    files = collect(argv[1:])
    errors: list[str] = []
    for p in files:
        errors.extend(check_file(p))
    print(f"check_typos: файлов {len(files)}, известных опечаток в словаре {len(TYPOS)}")
    if errors:
        print("\n❌ НАЙДЕНЫ ОПЕЧАТКИ:")
        for e in errors:
            print("  -", e)
        print("\n(строку можно выключить маркером TYPO-OK — если «ошибка» цитируется намеренно)")
        return 1
    print("✅ опечаток не найдено")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
