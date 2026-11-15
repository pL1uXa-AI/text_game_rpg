@echo off
REM Сессия 83: файл сохранён в UTF-8, а консоль Windows — в cp866, поэтому русский текст в
REM окне выглядел «кракозябрами». Ставим кодовую страницу (влияет только на это окно).
chcp 65001 >nul
REM start_chroma.bat — запуск СОБСТВЕННОЙ ChromaDB игры (порт 8001).
REM Данные — data\chroma, venv — data\chroma-venv. Отдельно от py_docs (8000).
REM
REM D13 (аудит 38): раньше пути были прописаны АБСОЛЮТНО этого рабочего каталога, поэтому
REM клон/перенос проекта в другое место ломал запуск ChromaDB — а start_game.bat вызывает
REM именно этот скрипт. Все пути теперь считаются от %~dp0.. (корень проекта).

setlocal
set "ROOT=%~dp0.."
cd /d "%ROOT%"

set "CHROMA_EXE=%ROOT%\data\chroma-venv\Scripts\chroma.exe"
REM chroma.exe из venv (chromadb 0.6.3) на этой машине стартует и МГНОВЕННО выходит с кодом 1 без
REM единой строки в stdout/stderr/логе. Тот же сервер, запущенный как -m chromadb.cli.cli,
REM поднимается нормально. Поэтому запускаем через python venv, а .exe — только фолбэк.
set "CHROMA_PY=%ROOT%\data\chroma-venv\Scripts\python.exe"
set "CHROMA_DATA=%ROOT%\data\chroma"
set "CHROMA_HOST=127.0.0.1"
set "CHROMA_PORT=8001"
REM B3 (аудит 38): журнал Chroma писался в data\chroma.log и не ротировался (5.3 МБ на
REM момент аудита). Перенесён в data\logs\ — там же, где журнал игры; папка data/ целиком
REM вне git.
set "CHROMA_LOG=%ROOT%\data\logs\chroma.log"

set ANONYMIZED_TELEMETRY=FALSE
set CHROMA_TELEMETRY_ENDPOINT=

if exist "%CHROMA_PY%" goto have_py
if exist "%CHROMA_EXE%" goto have_exe
echo [ERROR] Neither python.exe nor chroma.exe in data\chroma-venv.
echo         Run scripts\setup_env.bat - it creates data\chroma-venv and installs chromadb
pause
exit /b 1
:have_py
set "CHROMA_CMD="%CHROMA_PY%" -X utf8 -m chromadb.cli.cli"
goto have_cmd
:have_exe
set "CHROMA_CMD="%CHROMA_EXE%""
:have_cmd
if not exist "%ROOT%\data\logs" mkdir "%ROOT%\data\logs"
if not exist "%CHROMA_DATA%" mkdir "%CHROMA_DATA%"

REM ── Проверка: уже запущена? ─────────────────────────────────────────────
REM D14 (аудит 38): health-check приводится к ТОЙ ЖЕ схеме, что у llama.cpp (сессия 36,
REM п.14): проверяется ФАКТ ответа и его HTTP-код, а не errorlevel curl.
REM   * эндпоинт — /api/v2/heartbeat, тот же, что спрашивает backend/chroma_client.py:
REM     ping(). В chromadb 1.x v1-эндпоинты удалены, и старая проверка врала бы в обе
REM     стороны: ложный «мёртв» → второй экземпляр сервиса, ложный «жива» → RuntimeError
REM     в игре («ChromaDB не отвечает»);
REM   * живым считается любой код меньше 500 (401/404 = сервис отвечает).
set "CHROMA_CODE=000"
for /f "usebackq delims=" %%c in (`curl -s -o nul --max-time 5 -w "%%{http_code}" http://%CHROMA_HOST%:%CHROMA_PORT%/api/v2/heartbeat`) do set "CHROMA_CODE=%%c"
if "%CHROMA_CODE%"=="000" goto start_it
if "%CHROMA_CODE:~0,1%"=="5" goto start_it
echo [OK] Game ChromaDB already running on port %CHROMA_PORT% (HTTP %CHROMA_CODE%)
exit /b 0

:start_it
echo [START] Game ChromaDB on %CHROMA_HOST%:%CHROMA_PORT%...
echo         Data: %CHROMA_DATA%
echo         Log:  %CHROMA_LOG%
REM ── Запуск в фоне ───────────────────────────────────────────────────────
REM ⚠ НЕ добавляй сюда `>> "%CHROMA_LOG%" 2>&1` и не оборачивай в `cmd /c ...`:
REM на этой машине ЛЮБОЙ вариант `start` с перенаправлением потока вешает сам bat
REM (он ждёт закрытия файла), и Chroma не поднимается — игрок видел «failed to
REM start after 30s, HTTP=000», а chroma.log оставался пустым. Проверено.
REM Chroma пишет свой лог сама — через --log-path (это её штатный параметр).
start "ChromaDB-Game" /MIN %CHROMA_CMD% run --path "%CHROMA_DATA%" --host %CHROMA_HOST% --port %CHROMA_PORT% --log-path "%CHROMA_LOG%"

set count=0
:wait_loop
timeout /t 1 /nobreak >nul
set "CHROMA_CODE=000"
for /f "usebackq delims=" %%c in (`curl -s -o nul --max-time 5 -w "%%{http_code}" http://%CHROMA_HOST%:%CHROMA_PORT%/api/v2/heartbeat`) do set "CHROMA_CODE=%%c"
if not "%CHROMA_CODE%"=="000" if not "%CHROMA_CODE:~0,1%"=="5" (
    echo [OK] Game ChromaDB ready on http://%CHROMA_HOST%:%CHROMA_PORT% ^(HTTP %CHROMA_CODE%^)
    exit /b 0
)
set /a count+=1
if %count% lss 30 goto wait_loop

echo [ERROR] Game ChromaDB failed to start after 30s (последний ответ HTTP=%CHROMA_CODE%; смотри data\logs\chroma.log)
pause
exit /b 1
