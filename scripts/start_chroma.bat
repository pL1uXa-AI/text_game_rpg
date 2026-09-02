@echo off
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
set "CHROMA_DATA=%ROOT%\data\chroma"
set "CHROMA_HOST=127.0.0.1"
set "CHROMA_PORT=8001"
REM B3 (аудит 38): журнал Chroma писался в data\chroma.log и не ротировался (5.3 МБ на
REM момент аудита). Перенесён в data\logs\ — там же, где журнал игры; папка data/ целиком
REM вне git.
set "CHROMA_LOG=%ROOT%\data\logs\chroma.log"

set ANONYMIZED_TELEMETRY=FALSE
set CHROMA_TELEMETRY_ENDPOINT=

if not exist "%CHROMA_EXE%" (
    echo [ERROR] chroma.exe not found: "%CHROMA_EXE%"
    echo         Run scripts\setup_env.bat - it creates data\chroma-venv and installs chromadb
    pause
    exit /b 1
)

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
start "ChromaDB-Game" /MIN "%CHROMA_EXE%" run --path "%CHROMA_DATA%" --host %CHROMA_HOST% --port %CHROMA_PORT% --log-path "%CHROMA_LOG%"

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
