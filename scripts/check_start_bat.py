# -*- coding: utf-8 -*-
"""check_start_bat.py — структурные проверки батников запуска (D11, хвосты сессии 38).

Зачем отдельный скрипт (инвариант 19 в AGENT.md): прежние тесты читали текст start_game.bat
иAssert'ами сверяли подстроки — это «smoke по тексту»: регрессий в поведении cmd такой тест
не даёт, а при безвредном рефакторинге падает. Полноценный харнесс (`cmd /c` с подменённым
портом и фейковыми curl) оставлен как отдельная задача; сюда перенесены те проверки,
которые ЧЕСТНО являются структурными инвариантами файла и должны жить в scripts/check_*:

  [1] ни одного зашитого абсолютного пути к python.exe (D13) — иначе на другой машине
      bat падает без внятного текста;
  [2] интерпретатор берётся из переменной и проверяется запуском `-V` (а не `if exist`);
  [3] сервер игры слушает ТОЛЬКО 127.0.0.1 (правило 5: наружу нельзя — нет авторизации);
  [4] порт игры 8002 во всех местах согласован (bat, backend, frontend);
  [5] живость сервисов определяется HTTP-кодом (<500 = жив), а не errorlevel от curl (п.14);
  [6] файл в кодировке UTF-8 и без CJK-опечаток (cp1251 ломает кириллицу в echo).

Запуск:  python -X utf8 scripts/check_start_bat.py   (код 0 = ок)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BATS = [ROOT / "start_game.bat"] + sorted((ROOT / "scripts").glob("*.bat"))

# bat'ы, которые обязаны уметь находить интерпретатор без жёсткого пути
PY_BATS = {"start_game.bat", "setup_env.bat", "setup_tts.bat"}
ABS_PY = re.compile(r"(?:[A-Za-z]:[\\/][^\"'\s]*|[\\\\/]{2}[^\"'\s]*)python(?:\.exe)?", re.I)


def check_bat(path: Path) -> list[str]:
    errs: list[str] = []
    name = path.name
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        return [f"{name}: не UTF-8 ({e}) — cmd печатает кашу и правила AGENT.md (п.10) нарушены"]
    if "﻿" in text:
        errs.append(f"{name}: BOM в начале файла (.cmd с BOM местами не исполняется)")
    # инвариант 15: .bat обязаны оставаться CRLF (правка файла скриптом легко оставляет LF,
    # и тогда cmd.exe спотыкается о многострочные блоки if (...) — молча)
    if "\r\n" not in text:
        errs.append(f"{name}: переводы строк не CRLF (инвариант 15: cmd.exe + LF в if-блоке)")

    bad_cjk = {c for c in text if 0x3000 <= ord(c) <= 0x9fff or 0xac00 <= ord(c) <= 0xd7af}
    if bad_cjk:
        errs.append(f"{name}: посторонние CJK-символы {sorted(bad_cjk)}")

    if name in PY_BATS:
        # абсолютные пути ищем только в ИСПОЛНЯЕМЫХ строках: в echo/REM они законны —
        # там это подсказка пользователю «как задать PY вручную»
        body = "\n".join(ln for ln in text.splitlines()
                         if not ln.strip().upper().startswith(("REM", "ECHO", "@ECHO", ":")))
        hits = sorted({m.group(0) for m in ABS_PY.finditer(body)})
        if hits:
            errs.append(f"{name}: зашит абсолютный путь к python {hits} — на другой машине "
                        f"нужен PY/GAME_PYTHON из окружения, иначе 'python' из PATH (D13)")
        if not re.search(r'if\s+"?%\w+%"?\s*==\s*""', text):
            errs.append(f"{name}: нет значения по умолчанию для интерпретатора "
                        f'(ожидаем `if "%PY%"=="" set "PY=python"`)')
        if not re.search(r'"?%(?:PY|GAME_PYTHON)%"?\s+-V', text):
            errs.append(f"{name}: интерпретатор не проверяется запуском `-V` "
                        f"(`if exist` путь в PATH не ловит)")
    return errs


def check_main_bat(text: str) -> list[str]:
    """Стартовый bat игры: безопасность запуска и согласованность портов."""
    errs: list[str] = []
    if not re.search(r"--host\s+%GAME_BIND%", text):
        errs.append("start_game.bat: uvicorn запускается не из %GAME_BIND% — адрес перестал настраиваться")
    if re.search(r"--host\s+0\.0\.0\.0", text):
        errs.append("start_game.bat: жёсткий 0.0.0.0 — игра без авторизации уходит в локальную сеть (правило 5)")
    if "127.0.0.1" not in text:
        errs.append("start_game.bat: нет 127.0.0.1 — дефолт bind перестал быть локальным (правило 5)")
    ports = set(re.findall(r"[:\-](8002)\b", text))
    if "8002" not in ports:
        errs.append("start_game.bat: порт 8002 не найден — он разошёлся с README/frontend")
    # п.14: живость сервиса по HTTP-коду, а не по errorlevel
    seg = text[text.index("curl"):] if "curl" in text else ""
    if "http_code" not in text:
        errs.append("start_game.bat: проверка сервисов снова без `-w %{http_code}` (401 = «мёртв» — ложный отказ)")
    code_lines = [ln.strip() for ln in seg.splitlines()
                  if ln.strip() and not ln.strip().upper().startswith("REM")
                  and not ln.strip().startswith(":")]
    if any("errorlevel" in ln.lower() for ln in code_lines if "curl" in ln.lower()):
        errs.append("start_game.bat: вывод curl классифицируется через errorlevel (любая 4xx = ложная смерть)")
    if "pause" not in text:
        errs.append("start_game.bat: нет pause — окно закроется вместе с текстом ошибки")
    return errs


def check_ports_agree() -> list[str]:
    """Порт игры обязан совпадать в bat, бэкенде и фронтенде (иначе UI просто не дойдёт до API)."""
    errs: list[str] = []
    bat = (ROOT / "start_game.bat").read_text(encoding="utf-8")
    ports = set(re.findall(r":(80\d\d)", bat)) | set(re.findall(r"--port\s+(\d+)", bat))
    js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    js_ports = set(re.findall(r"127\.0\.0\.1:(\d{4})", js))
    if js_ports and not js_ports <= {"8002"}:
        errs.append(f"app.js ссылается на порты {sorted(js_ports)}, отличные от 8002")
    if "8002" not in ports:
        errs.append(f"start_game.bat: среди портов нет 8002 (найдено {sorted(ports)})")
    return errs


def block_echo_check(path: Path) -> list[str]:
    """D11: `echo`/`set` со «ногой» скобкой внутри многострочного if(...) ломает весь блок.

    cmd.exe закрывает блок по первой неэкранированной `)`: строки после неё (в
    start_game.bat там был `pause` и `exit /b 0`) начинают выполняться БЕЗУСЛОВНО. Живая
    поломка, найденная харнессом tests/test_start_bat_harness.py: launcher не доходил до
    запуска сервера НИКОГДА. Экранирование — `^(` и `^)`. Харнесс исполняет bat только на
    Windows, поэтому на Linux-CI этот инвариант сторожит отсюда.
    """
    errs: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return []
    depth = 0
    for n, line in enumerate(text.splitlines(), 1):
        t = line.strip()
        skip = t.upper().startswith("REM") or t.startswith(":")
        if depth and not skip and re.match(r"(?i)^(echo|set)\b", t):
            outside_quotes = re.sub(r'"[^"]*"', '""', t)   # в кавычках скобки безвредны
            if re.search(r"(?<!\^)[()]", outside_quotes):
                errs.append(f"{path.name}:{n}: echo/set со скобкой внутри if-блока — cmd "
                            f"закроет блок досрочно и строки ниже утекут в безусловное "
                            f"выполнение (экранируй ^( и ^)): {t[:70]}")
        if not skip and re.match(r"(?i)^(if|for|call)\b", t) and t.endswith("("):
            depth += 1
        if depth and t == ")":
            depth -= 1
    return errs


def main() -> int:
    errors: list[str] = []
    print("bat checks:")
    for b in BATS:
        if not b.exists():
            errors.append(f"нет файла {b.name}")
            continue
        errors += check_bat(b)
        errors += block_echo_check(b)
    n_blocks = sum(1 for b in BATS if b.exists() and block_echo_check(b))
    print(f"  · echo/set с неэкранированными скобками в if-блоках: {n_blocks} файлов")
    game = (ROOT / "start_game.bat")
    if game.exists():
        errors += check_main_bat(game.read_text(encoding="utf-8"))
    errors += check_ports_agree()
    print(f"  · батников проверено: {len(BATS)}, обязательных с поиском python: {len(PY_BATS)}")
    if errors:
        print("\n❌ ОШИБКИ:")
        for e in errors:
            print("  -", e)
        return 1
    print("\n✅ батники проверены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
