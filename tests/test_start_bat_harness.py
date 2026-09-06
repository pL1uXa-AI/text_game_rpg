# -*- coding: utf-8 -*-
"""D11 (хвосты сессии 38): ЧЕСТНЫЙ харнесс для start_game.bat — bat исполняется cmd'ом.

Прежние «тесты батника» читали текст файла и сверяли подстроки — инвариант 19 в AGENT.md
такое запрещает: регрессий поведения такие проверки не дают и падают при безвредном
рефакторинге. Здесь bat ДЕЙСТВИТЕЛЬНО исполняется `cmd /c` в песочнице, где внешние
зависимы подменены, так что проверяется реальная логика веток запуска.

Что подделано и почему именно так:
  · `curl.cmd`      — код ответа сервисов задаёт тест (8080 = llama.cpp, 8001 = Chroma);
                      NONE = «сервис молчит», как при живом connection refused;
  · `netstat.cmd`   — «игра уже слушает 8002 / не слушает»;
  · python — НАСТОЯЩИЙ (sys.executable): подменить его .bat-заглушкой нельзя, cmd.exe
                      не возвращает управление после вызова батча без `call` — bat
                      обрывался на проверке `-V` и до запуска не доходил (проверено);
  · фейковый пакет `uvicorn` в cwd песочницы — пишет полученную командную строку в маркер
                      и выходит 0. Именно поэтому сервер не стартует: порты не занимаються,
                      llama/Chroma не дёргаются, боевая БД не трогается;
  · `scripts\\start_chroma.bat` — заглушка с задаваемым errorlevel (bat зовёт её через
                      `call`, поэтому возврат управления корректен).

`pause` не блокирует тест (stdin = DEVNULL), браузер не открывается (ветка «уже запущена»
проверяется только на «второй uvicorn не лезет»: `start` — внутренняя команда cmd, её не
подменить, а реальный браузер из теста был бы вредным побочным эффектом).

Прогон: pytest на Windows. На Linux/macOS skip — там cmd.exe нет, bat прикрывает
структурный scripts/check_start_bat.py (он в CI).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BAT = ROOT / "start_game.bat"

# cmd.exe есть только на Windows. На Linux/macOS (там живёт CI) модуль пропускается ЦЕЛИКОМ:
# исполнять .bat там нечем, а фейковый «успех» был бы хуже пропуска. Структурную часть тех же
# инвариантов на любой платформе сторожит scripts/check_start_bat.py (у него отдельный CI-шаг).
pytestmark = pytest.mark.skipif(os.name != "nt",
                                reason="cmd.exe только на Windows; на Linux bat прикрывает "
                                       "scripts/check_start_bat.py (шаг CI)")

_CURL = """@echo off
REM Заглушка curl: какой код вернуть, решает тест (FAKE_LLAMA / FAKE_CHROMA).
REM Порт ищем во ВСЕЙ командной строке: последний аргумент bat-вызова — -w "%{http_code}",
REM а не URL. NONE = сервис молчит: ничего не печатаем, уходим с ненулевым кодом.
set "ALL=%*"
set "CODE=000"
if "%ALL:8080=%" NEQ "%ALL%" set "CODE=%FAKE_LLAMA%"
if "%ALL:8001=%" NEQ "%ALL%" set "CODE=%FAKE_CHROMA%"
if "%CODE%"=="NONE" exit /b 7
echo %CODE%
"""

_NETSTAT = """@echo off
if "%FAKE_LISTEN%"=="1" echo   TCP    127.0.0.1:8002    0.0.0.0:0    LISTENING    4242
exit /b 0
"""

# фейковый модуль uvicorn: регистрирует запуск и выходит 0 (реальный сервер не поднимается).
# utf8_mode пишется в запись: `-X utf8` — аргумент САМОГО python, до argv модуля он не доживает,
# так что иначе проверка правила 10 была бы ложной.
_UVICORN_MAIN = '''"""Фейковый uvicorn для харнесса start_game.bat (D11): пишет argv в маркер, rc 0."""
import os
import pathlib
import sys

rec = os.environ.get("RUNMARK")
if rec:
    with pathlib.Path(rec).open("a", encoding="utf-8") as fh:
        fh.write("UVICORN: utf8_mode=%d | %s\\n"
                 % (int(bool(sys.flags.utf8_mode)), " ".join(sys.argv[1:])))
sys.exit(0)
'''

_CHROMA_STUB = """@echo off
>>"%RUNMARK%" echo CHROMA_STUB_CALLED
exit /b %CHROMA_STUB_RC%
"""


def _write_bat(path: Path, text: str) -> None:
    """cmd.exe требует CRLF (инвариант 15): LF в многострочных if-блоках ломает bat молча."""
    path.write_bytes(text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"))


@pytest.fixture()
def sandbox(tmp_path: Path) -> dict:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_bat(bin_dir / "curl.cmd", _CURL)
    _write_bat(bin_dir / "netstat.cmd", _NETSTAT)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    _write_bat(scripts / "start_chroma.bat", _CHROMA_STUB)
    pkg = tmp_path / "uvicorn"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__main__.py").write_text(_UVICORN_MAIN, encoding="utf-8")
    _write_bat(tmp_path / "start_game.bat", BAT.read_text(encoding="utf-8"))
    return {"root": tmp_path, "marker": tmp_path / "calls.txt"}


def run_bat(sandbox: dict, *, llama: str = "200", chroma: str = "200",
            listening: str = "0", stub_rc: str = "0",
            extra_env: dict | None = None):
    """Исполняет bat через cmd и возвращает (proc, записанные аргументы uvicorn, флаг вызова Chroma)."""
    marker = sandbox["marker"]
    if marker.exists():
        marker.unlink()
    env = dict(os.environ)
    env["PATH"] = str(sandbox["root"] / "bin") + os.pathsep + env.get("PATH", "")
    env.update({
        "FAKE_LLAMA": llama, "FAKE_CHROMA": chroma, "FAKE_LISTEN": listening,
        "CHROMA_STUB_RC": stub_rc, "RUNMARK": str(marker),
        "GAME_PYTHON": sys.executable,
        # ветка «игра уже запущена» зовёт `start "" http://127.0.0.1:8002` — без этого
        # флага каждый прогон теста открывал бы реальный браузер на рабочем столе
        "GAME_NO_BROWSER": "1",
    })
    env.pop("GAME_BIND", None)
    env.pop("GAME_BIND_CONFIRM", None)   # B2: иначе унаследованный из shells флаг «разрешить»
    env.pop("PY", None)
    env.update(extra_env or {})
    p = subprocess.run(["cmd", "/c", "start_game.bat"], cwd=str(sandbox["root"]), env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       stdin=subprocess.DEVNULL, timeout=180)
    calls = marker.read_text(encoding="utf-8", errors="replace").splitlines() if marker.exists() else []
    return type("Res", (), {
        "proc": p, "calls": calls,
        "uvicorn": [c.split("| ", 1)[1] for c in calls if c.startswith("UVICORN:")],
        "utf8_mode": any("utf8_mode=1" in c for c in calls if c.startswith("UVICORN:")),
        "chroma_stub_called": any("CHROMA_STUB_CALLED" in c for c in calls),
    })()


def test_harness_actually_runs_cmd():
    """Страховка «харнесс ничего не проверяет»: cmd.exe и сам bat обязаны быть на месте."""
    assert os.name == "nt", "модуль должен был пропуститься"
    assert BAT.exists()
    r = subprocess.run(["cmd", "/c", "echo ok"], capture_output=True, text=True, timeout=30)
    assert "ok" in r.stdout, "cmd.exe не исполняет команды — харнесс слеп"


def test_bat_launches_server_on_loopback(sandbox):
    """Здоровые сервисы → uvicorn запущен ровно один раз, на 127.0.0.1:8002, приложением игры."""
    r = run_bat(sandbox)
    assert len(r.uvicorn) == 1, f"сервер не запущен: {r.calls} | {r.proc.stdout[-700:]}"
    line = r.uvicorn[0]
    assert "--host 127.0.0.1" in line, f"слушает не loopback (правило 5): {line}"
    assert "--port 8002" in line, f"порт не 8002: {line}"
    assert "backend.app:app" in line
    assert r.utf8_mode, "без -X utf8 кириллица в логах/чате ломается (правило 10)"
    assert not r.chroma_stub_called, "Chroma живая — поднимать её не требовалось"


def test_bat_starts_when_llama_answers_401(sandbox):
    """РЕАЛЬНОЕ поведение (п.14, сессия 36): 401/404 от llama.cpp с --api-key ≠ «сервер мёртв»."""
    for code in ("401", "404", "400", "200"):
        r = run_bat(sandbox, llama=code)
        assert r.uvicorn, f"llama отвечает {code} — bat отказался стартовать (ложная смерть)"


def test_bat_refuses_when_llama_really_dead(sandbox):
    """Нет ответа и 5xx — старт запрещён, bat объясняет причину и не поднимает процесс."""
    r = run_bat(sandbox, llama="NONE")
    assert not r.uvicorn, "llama.cpp мертва — игра всё равно попыталась стартовать"
    assert r.proc.returncode == 1
    assert "llama" in (r.proc.stdout + r.proc.stderr).lower()
    r = run_bat(sandbox, llama="503")
    assert not r.uvicorn, "5xx от llama.cpp должен считаться смертью"


def test_bat_starts_own_chroma_when_down(sandbox):
    """Chroma не отвечает → bat зовёт start_chroma.bat; та упала → игра НЕ стартует."""
    r = run_bat(sandbox, chroma="NONE", stub_rc="0")
    assert r.chroma_stub_called, "базы нет — start_chroma.bat не вызвана"
    assert r.uvicorn, "Chroma поднялась (rc 0) — игра обязана стартовать"

    r2 = run_bat(sandbox, chroma="500", stub_rc="1")
    assert r2.chroma_stub_called
    assert not r2.uvicorn, "Chroma не поднялась, а игра стартует (упадёт на первом же ходе)"
    assert r2.proc.returncode == 1


def test_bat_reports_missing_interpreter(sandbox):
    """Нерабочий GAME_PYTHON → внятная ошибка и код 1, а не «окно закрылось без объяснений»."""
    r = run_bat(sandbox, extra_env={"GAME_PYTHON": str(sandbox["root"] / "python-net-net.exe")})
    assert not r.uvicorn
    assert r.proc.returncode == 1
    out = (r.proc.stdout + r.proc.stderr).lower()
    assert "python" in out or "не найден" in out, f"текст ошибки не по делу: {out[-300:]}"


def test_bat_honors_game_bind_env(sandbox):
    """GAME_BIND — осознанный лаз (правило 5): по умолчанию loopback, явно (с подтверждением) — любой адрес."""
    for bind in ("0.0.0.0", "10.0.0.5"):
        r = run_bat(sandbox, extra_env={"GAME_BIND": bind, "GAME_BIND_CONFIRM": "1"})
        assert r.uvicorn and f"--host {bind}" in r.uvicorn[0], f"GAME_BIND={bind} проигнорирован"


def test_bat_refuses_public_bind_without_confirm(sandbox):
    """B2 (аудит 41): чужой адрес без GAME_BIND_CONFIRM — сервер НЕ поднимается.

    Раньше launcher сам приглашал «запусти с GAME_BIND=0.0.0.0», выдавая игру без
    авторизации (и админку с записью ключей) всей локальной сети."""
    r = run_bat(sandbox, extra_env={"GAME_BIND": "0.0.0.0"})
    assert not r.uvicorn, "без подтверждения bat всё равно вывел сервер наружу"
    assert r.proc.returncode == 1
    out = (r.proc.stdout + r.proc.stderr)
    assert "GAME_BIND_CONFIRM" in out, f"нет внятного объяснения отказа: {out[-300:]}"
    # localhost-варианты подтверждения НЕ требуют
    for bind in ("127.0.0.1", "localhost"):
        r = run_bat(sandbox, extra_env={"GAME_BIND": bind})
        assert r.uvicorn, f"{bind} — ложный отказ (игрок не должен подтверждать своё же ПК)"


def test_bat_does_not_launch_second_server(sandbox):
    """Игра уже слушает 8002 → повторный запуск НЕ поднимает второй uvicorn."""
    r = run_bat(sandbox, listening="1")
    assert not r.uvicorn, "порт занят — bat полез за вторым процессом"
    assert not r.chroma_stub_called
    # и не открыл браузер: вкладка на каждый прогон теста была бы вредным побочным эффектом
    assert "GAME_NO_BROWSER" in (r.proc.stdout + r.proc.stderr), \
        f"браузер-флаг не соблюдается: {r.proc.stdout[-300:]}"


def test_bat_browser_path_is_guarded(sandbox):
    """Ветка «открыть браузер» обязана остаться ЗА ГАРДом GAME_NO_BROWSER.

    Поведенчески эту ветку не проверяем намеренно: единственный способ — реально выполнить
    `start "" http://127.0.0.1:8002`, т.е. открыть браузер на рабочем столе при каждом прогоне
    (а `start` — внутренняя команда cmd, её не подменить из PATH). Поэтому проверка
    структурная (инвариант 19 честно помечен): guard есть, голых `start` в bat нет.
    """
    src = BAT.read_text(encoding="utf-8")
    starts = [ln.strip() for ln in src.splitlines()
              if re.match(r'(?i)^start\b', ln.strip())
              or re.search(r'""\s*start\s', ln) or 'start "" http' in ln]
    assert starts, "ветка открытия вкладки пропала из launcher'а entirely?"
    for ln in starts:
        guarded = re.search(r'if\s+"%GAME_NO_BROWSER%"\s*==\s*""\s+start', ln)
        assert guarded, f"start без гарда GAME_NO_BROWSER (откроет браузер в тестах): {ln}"
    # и поведение под гардом не сломано: с флагом браузер не открывается, сервер не дублируется
    r = run_bat(sandbox, listening="1")
    assert not r.uvicorn and "GAME_NO_BROWSER" in r.proc.stdout


def test_harness_detects_broken_block_echo(sandbox):
    """Доказательство, что харнесс ловит РЕАЛЬНЫЕ поломки cmd-блоков, а не подстроки.

    При первом же прогоне харнесс нашёл живую поломку: `echo` с НЕЭКРАНИРОВАННОЙ ( или )
    внутри многострочного `if (...)` завершает блок по первой `)` — cmd не считает её
    текстом. В первой ветке start_game.bat («игра уже запущена») стояло
    `echo      (открыл браузер. Закрой это окно.)`, из-за чего `pause` и `exit /b 0`
    утекали в БЕЗУСЛОВНОЕ выполнение: launcher не запускал игровой сервер НИКОГДА,
    даже при свободном порте и живых сервисах (молча выходил с кодом 0). Строка жила
    в файле с сессии 33; текст bat выглядел корректно и smoke-по-тексту этого не видел.

    Проба ВОСПРОИЗВОДИТ исторический вид ветки и требует ровно одного: харнесс обязан
    отличить сломанный launcher от целого (целой запускает, сломанный — нет).
    """
    src = BAT.read_text(encoding="utf-8")
    guarded = ('    if "%GAME_NO_BROWSER%"=="" start "" http://127.0.0.1:8002\r\n'
               '    if "%GAME_NO_BROWSER%"=="" echo      открыл браузер. Закрой это окно.\r\n'
               '    if not "%GAME_NO_BROWSER%"=="" echo      браузер не открыт: '
               'GAME_NO_BROWSER задан\r\n')
    needle = guarded.replace("\r\n", "\n")
    assert needle in src.replace("\r\n", "\n"), \
        "ветка «игра уже запущена» переписана — обнови пробу (исторический баг: инвариант 15а)"
    historical = ('    start "" http://127.0.0.1:8002\n'
                  '    echo      (открыл браузер. Закрой это окно.)\n')
    broken = src.replace("\r\n", "\n").replace(needle, historical)
    (sandbox["root"] / "start_game_broken.bat").write_bytes(
        broken.replace("\n", "\r\n").encode("utf-8"))

    env = dict(os.environ)
    env["PATH"] = str(sandbox["root"] / "bin") + os.pathsep + env.get("PATH", "")
    env.update({"FAKE_LLAMA": "200", "FAKE_CHROMA": "200", "FAKE_LISTEN": "0",
                "CHROMA_STUB_RC": "0", "GAME_NO_BROWSER": "1",
                "RUNMARK": str(sandbox["root"] / "broken.txt"),
                "GAME_PYTHON": sys.executable})
    subprocess.run(["cmd", "/c", "start_game_broken.bat"], cwd=str(sandbox["root"]),
                   env=env, capture_output=True, text=True, encoding="utf-8",
                   errors="replace", stdin=subprocess.DEVNULL, timeout=120)
    broke = sandbox["root"] / "broken.txt"
    broke_text = broke.read_text(encoding="utf-8", errors="replace") if broke.exists() else ""

    good = run_bat(sandbox, listening="0")
    assert good.uvicorn, "целой launcher не запускает сервер — сравнение теряет смысл"
    assert "UVICORN:" not in broke_text, (
        "харнесс ослеп: bat с исторической поломкой дошёл до запуска сервера, значит "
        "проверка перестала исполнять настоящие ветки cmd")
