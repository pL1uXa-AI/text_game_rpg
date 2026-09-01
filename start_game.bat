@echo off
REM Text Game RPG — запуск сервера игры.
REM Требования: 1) llama.cpp на 8080 (уже запущен)  2) своя ChromaDB на 8001 (поднимется сама).
REM Консоль НЕ закрывается при ошибке: показываем сообщение и ждём Enter.

setlocal
cd /d %~dp0

REM 0. Если игра уже запущена — второй раз не запускаем
netstat -ano | findstr ":8002" | findstr "LISTENING" >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] Игра уже запущена: http://127.0.0.1:8002
    start "" http://127.0.0.1:8002
    echo      (открыл браузер. Закрой это окно.)
    pause
    exit /b 0
)

REM 1. Проверка llama.cpp
REM Сервер модели может быть запущен с --api-key: тогда /v1/models без ключа отвечает 401,
REM и «не нулевой errorlevel от curl» означал бы «llama.cpp мёртв» — игра отказывалась
REM стартовать при живом сервере (сессия 36, п.14). Поэтому проверяем сам ФАКТ ответа:
REM любой код меньше 500 (включая 401/404) = сервис отвечает, он живой.
set "LLAMA_CODE=000"
for /f "usebackq delims=" %%c in (`curl -s -o nul --max-time 5 -w "%%{http_code}" http://127.0.0.1:8080/v1/models`) do set "LLAMA_CODE=%%c"
if "%LLAMA_CODE:~0,1%"=="5" goto llama_dead
if "%LLAMA_CODE%"=="000" goto llama_dead
echo [OK] llama.cpp отвечает на 127.0.0.1:8080 (HTTP %LLAMA_CODE%)
goto llama_ok
:llama_dead
echo [WARN] llama.cpp не отвечает на 127.0.0.1:8080 — игра без рассказа не поедет!
echo        Запусти сервер модели и повтори запуск.
pause
exit /b 1
:llama_ok

REM 2. Проверка своей ChromaDB (порт 8001)
curl -s http://127.0.0.1:8001/api/v1/heartbeat >nul 2>&1
if %errorlevel% neq 0 (
    echo [START] Поднимаю собственную ChromaDB игры на 127.0.0.1:8001...
    call scripts\start_chroma.bat
    if %errorlevel% neq 0 (
        echo [ERROR] ChromaDB не поднялась. Смотри data\chroma.log
        pause
        exit /b 1
    )
)

REM 3. Сервер игры
REM ВАЖНО: слушаем ТОЛЬКО 127.0.0.1. В .env лежат живые API-ключи, у игры нет авторизации —
REM доступ с 0.0.0.0 отдаёт админку (/admin) и весь API всей локальной сети.
if "%GAME_BIND%"=="" set GAME_BIND=127.0.0.1
echo [START] Text Game RPG: http://127.0.0.1:8002
echo [INFO]  bind=%GAME_BIND% (чтобы открыть наружу — запусти вручную с GAME_BIND=0.0.0.0,
echo         помня: авторизации в игре нет, в .env живые ключи)
"D:\Development\Development_Tools\Runtimes\Python\3.12.10\python.exe" -X utf8 -m uvicorn backend.app:app --host %GAME_BIND% --port 8002
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Сервер не запустился или упал (код %errorlevel%).
    echo         Внимательно посмотри сообщения выше. Обычно это:
    echo           - порт 8002 занят другим процессом,
    echo           - ошибка в коде backend (тогда читай traceback выше).
    pause
)