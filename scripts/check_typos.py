# -*- coding: utf-8 -*-
"""check_typos.py — проверка известных русских опечаток (сессия 38, пункт C10)
и латиницы, ПРИКЛЕЕННОЙ к кириллице (сессия 61, пункт C3).

Два существующих сканера ловят «иностранное» (CJK-вставки) и латинские омоглифы внутри
кириллицы (`check_frontend.py`), но русскую орфографию — нет: «ИТОГ КВЕТА» TYPO-OK, «промпт уощён» TYPO-OK,
«нормилзация директив» TYPO-OK, «персонаж останается» TYPO-OK — всё
это выглядит почти чисто, глаз соскальзывает. Аудит сессии 38 нашёл шесть таких мест, и
одно из них (`narrator.py`, правило 33) попало прямо в СИСТЕМНЫЙ ПРОМПТ, то есть каждый
ход читает модель.

Дешёвое закрытие класса: список «ошибка → правильно» + проход по файлам проекта.
Список пополняется каждой найденной опечаткой (регрессия, а не «разовая правка»).

Второй класс (C3, аудит 41) словарю недоступен: в русское слово въедает латиница, и
получается гибрид двух алфавитов в одном слове — «ф» + «antom» + «ные», «conservat» + «ивный»,
«и» + «closed». Ни на одну запись TYPOS такое не похоже, а глаз вставку не видит: мозг
достреливает русское слово. Ловим структурно — `GLUE_RE` (стык кириллицы и 2+ латинских
букв) + белый список `GLUE_OK`.

⚠ Пробел в паттерн НЕ вставлен, хотя в задании C3 regex был `[а-яё]+\\s*[a-zA-Z]{2,}`:
со пробелом проверка находит 376 мест в `backend/` и ещё ~180 по документам — и это не
опечатки, а нормальная техническая речь: «через RAG», «в JSON», «локальная llama», «по id».
Сканер, у которого ложных срабатываний больше, чем настоящих, выключают в первую же неделю.
Под проверкой только СЛИТНАЯ склейка: на ней ложных срабатываний ноль.

Запуск:  python -X utf8 scripts/check_typos.py [путь …]      (код 0 = ок)
         python -X utf8 scripts/check_typos.py --probe       (самопроверка сканера)
Исключение строки: маркер `TYPO-OK` в той же строке — так в доках цитируются сами
исправленные опечатки (иначе документация о баге не смогла бы существовать).
"""
from __future__ import annotations

import re
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

# ── C3 (аудит 41): латиница, приклеенная к кириллице слитно ────────────────────────────
# Граница обязана быть слитной: со пробелом паттерн срыгивает всю техническую речь проекта
# (376 мест в backend/), см. шапку модуля.
GLUE_RE = re.compile(r"[а-яёА-ЯЁ][A-Za-z]{2,}|[A-Za-z]{2,}[а-яёА-ЯЁ]")
# Белый список: легальные латинские термины, которым позволено стоять в слитном контакте с
# кириллицей (название ключа, директивы, модели — термин, дописанный к русскому слову без
# разрыва). Таких написаний в проекте сейчас ноль; список нужен, чтобы проверка не выдумывала опечатку там,
# где термин присоединён намеренно, и растёт по мере НАХОДОК, а не по фантазии.
GLUE_OK: frozenset[str] = frozenset({
    "llm", "rag", "tts", "sse", "json", "api", "ui", "npc", "hp", "mp", "xp", "id",
    "game_engine", "n_ctx", "item_update", "check_plot", "add_item", "remove_item",
    "ok", "py", "js", "bat", "md", "ci", "db", "sql", "html", "css", "url", "utf",
})

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
# «ошибка» внутри исправленной формулировки legitimately выглядит как подстрока TYPO-OK
# (например «сохранить» содержит «сохронить» только при опечатке — не конфликтует)  TYPO-OK


def _hits_line(line: str) -> list[tuple[str, str]]:
    low = line.lower()
    out = []
    for bad, good in TYPOS.items():
        if bad in low:
            out.append((bad, good))
    return out


def _glue_line(line: str) -> list[str]:
    """Латиница, приклеенная к кириллице (C3). Пусто = строка чиста."""
    out = []
    for m in GLUE_RE.finditer(line):
        tok = re.sub(r"[а-яёА-ЯЁ]", "", m.group(0)).lower().rstrip("_.")
        if tok in GLUE_OK:
            continue
        out.append(m.group(0))
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
        for tok in _glue_line(line):
            errors.append(f"{path.relative_to(ROOT)}:{n}: латиница в кириллице «{tok}» "
                          f"(C3): {line.strip()[:90]}")
    return errors


def collect(paths: list[str]) -> list[Path]:
    if paths:
        return [Path(p) for p in paths]
    out: list[Path] = []
    for pat in SCAN_GLOBS:
        out.extend(sorted(ROOT.glob(pat)))
    # архив аудита — это ЦИТАТЫ опечаток, а не код: помечать каждую строку бессмысленно
    skip = ("ISSUES_AUDIT_",)
    return [p for p in out if p.is_file() and "__pycache__" not in str(p)
            and not any(s in str(p).replace("\\", "/") for s in skip)]


# Пробы самого сканера: заведомую вставку обязан найти, легальную — пропустить. Без них
# «зелёный» сканер мог бы означать лишь то, что regex перестал что-либо находить.
# Гибриды собраны из кусков, чтобы этот файл проходил собственную проверку.
GLUE_SHOULD_CATCH = {
    "латиница в слове": "«ф" + "antom" + "ные» сообщения",
    "английский корень + русский хвост": "Критерий " + "conservat" + "ивный",
    "слитный союз и глагол": "пробел " + "и" + "closed" + " его",
}
GLUE_SHOULD_PASS = (
    "через RAG и LLM", "в JSON-дампе мира", "`item_update` дописывает add_item",
    "локальная llama.cpp", "по id карточки", "нет world.setting", "флаг GAME_BIND",
)


def probe() -> int:
    """Сканер работает: ловит заведомые вставки и не трогает заведомо легальные."""
    bad = 0
    for name, s in GLUE_SHOULD_CATCH.items():
        if not _glue_line(s):
            print(f"  ❌ не поймано ({name}): {s!r}")
            bad += 1
    for s in GLUE_SHOULD_PASS:
        if _glue_line(s):
            print(f"  ❌ ложное срабатывание: {s!r} → {_glue_line(s)}")
            bad += 1
    total = len(GLUE_SHOULD_CATCH) + len(GLUE_SHOULD_PASS)
    if bad:
        print(f"probe: ПРОВАЛЕНО {bad} из {total}")
        return 1
    print(f"✅ probe: {len(GLUE_SHOULD_CATCH)} вставок поймано, "
          f"{len(GLUE_SHOULD_PASS)} легальных фраз не тронуто")
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--probe":
        return probe()
    files = collect(argv)
    errors: list[str] = []
    for p in files:
        errors.extend(check_file(p))
    print(f"check_typos: файлов {len(files)}, известных опечаток в словаре {len(TYPOS)}, "
          f"легальных сращений в белом списке {len(GLUE_OK)}")
    if errors:
        print("\n❌ НАЙДЕНЫ ОПЕЧАТКИ:")
        for e in errors:
            print("  -", e)
        print("\n(строку можно выключить маркером TYPO-OK — если «ошибка» цитируется намеренно)")
        return 1
    print("✅ опечаток не найдено")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
