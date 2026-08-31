@echo off
REM start_chroma.bat — запуск СОБСТВЕННОЙ ChromaDB игры (порт 8001).
REM Данные — data/chroma, venv — data/chroma-venv. Отдельно от py_docs (8000).

setlocal

set CHROMA_EXE=D:\Development\Development_Environment\Workspaces\Dev_Data\Projects\Python\text_game\data\chroma-venv\Scripts\chroma.exe
set CHROMA_DATA=D:\Development\Development_Environment\Workspaces\Dev_Data\Projects\Python\text_game\data\chroma
set CHROMA_HOST=127.0.0.1
set CHROMA_PORT=8001
set CHROMA_LOG=D:\Development\Development_Environment\Workspaces\Dev_Data\Projects\Python\text_game\data\chroma.log

set ANONYMIZED_TELEMETRY=FALSE
set CHROMA_TELEMETRY_ENDPOINT=

if not exist "%CHROMA_EXE%" (
    echo [ERROR] chroma.exe not found. Run: data\chroma-venv\Scripts\python.exe -m pip install "chromadb>=0.5.23,<0.7" "posthog==2.5.0"
    pause
    exit /b 1
)

if not exist "%CHROMA_LOG%" (type nul > "%CHROMA_LOG%")

REM Проверка: уже запущен?
curl -s http://%CHROMA_HOST%:%CHROMA_PORT%/api/v1/heartbeat >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] Game ChromaDB already running on port %CHROMA_PORT%
    exit /b 0
)

echo [START] Game ChromaDB on %CHROMA_HOST%:%CHROMA_PORT%...
echo         Data: %CHROMA_DATA%
start "ChromaDB-Game" /MIN "%CHROMA_EXE%" run --path "%CHROMA_DATA%" --host %CHROMA_HOST% --port %CHROMA_PORT% --log-path "%CHROMA_LOG%"

set count=0
:wait_loop
timeout /t 1 /nobreak >nul
curl -s http://%CHROMA_HOST%:%CHROMA_PORT%/api/v1/heartbeat >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] Game ChromaDB ready on http://%CHROMA_HOST%:%CHROMA_PORT%
    exit /b 0
)
set /a count+=1
if %count% lss 30 goto wait_loop

echo [ERROR] Game ChromaDB failed to start after 30s (смотри data\chroma.log)
pause
exit /b 1