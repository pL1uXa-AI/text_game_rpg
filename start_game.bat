@echo off
REM Text Game RPG — запуск сервера игры.
REM Требования: 1) провайдер модели из MAIN_BASE_URL в .env (локальная модель или облако)
REM             2) своя ChromaDB на 8001 (поднимется сама).
REM Консоль НЕ закрывается при ошибке: показываем сообщение и ждём Enter.

setlocal
cd /d %~dp0

REM 0. Если игра уже запущена — второй раз не запускаем
netstat -ano | findstr ":8002" | findstr "LISTENING" >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] Игра уже запущена: http://127.0.0.1:8002
    REM GAME_NO_BROWSER задан — вкладку НЕ открывать: нужно автотестам (харнесс
    REM tests/test_start_bat_harness.py) и автозапуску, где окно браузера — вредный
    REM побочный эффект. Игрок по умолчанию видит прежнее поведение.
    if "%GAME_NO_BROWSER%"=="" start "" http://127.0.0.1:8002
    if "%GAME_NO_BROWSER%"=="" echo      открыл браузер. Закрой это окно.
    if not "%GAME_NO_BROWSER%"=="" echo      браузер не открыт: GAME_NO_BROWSER задан
    pause
    exit /b 0
)

REM 1. Проверка ПРОВАЙДЕРА МОДЕЛИ — того, что реально настроен в .env (MAIN_BASE_URL).
REM Было: жёстко http://127.0.0.1:8080/v1/models, т.е. лаунчер с марша бросал запуск у игрока
REM с облачным провайдером (routerai.ru и т.п.): «llama.cpp не отвечает» — хотя llama.cpp ему
REM не нужен вовсе. Теперь адрес берётся из того же .env, что читает backend/config.py, и
REM проверяется тем же критерием, что llm.probe(): любой код < 500 (401/403/404 тоже) = живой.
REM Таймаут больше 8-секундного в probe(): облачный шлюз под нагрузкой медленнее локалки.
REM GAME_PROBE_URL=<url> — явный override (осознанно, как GAME_BIND); GAME_SKIP_LLM_CHECK=1 —
REM пропустить проверку (критерий GAME_* из харнесса test_start_bat_harness.py).
REM
REM ⚠ Про стиль: здесь НЕТ блоков `if ( ... )` вокруг set+echo. cmd раскрывает %VAR% во всём
REM блоке при разборе, до выполнения, поэтому `set "X=..."` и `echo %X%` в одном блоке печатают
REM СТАРОЕ (пустое) значение — ровно так адрес из .env терялся, и проверка молча уходила на
REM http://127.0.0.1:8080/v1. Ветки — только через goto, переменные — вне скобок.
set "PROVIDER_BASE="
if not exist ".env" goto provider_default
for /f "usebackq tokens=1,* delims==" %%a in (`findstr /b /c:"MAIN_BASE_URL=" .env`) do set "PROVIDER_BASE=%%b"
set "PROVIDER_BASE=%PROVIDER_BASE:"=%"
set "PROVIDER_BASE=%PROVIDER_BASE: =%"
:provider_default
if "%PROVIDER_BASE%"=="" set "PROVIDER_BASE=http://127.0.0.1:8080/v1"
if not "%GAME_PROBE_URL%"=="" goto probe_override
set "PROBE_URL=%PROVIDER_BASE%/models"
goto provider_probe
:probe_override
echo [INFO]  GAME_PROBE_URL задан — проверяю %GAME_PROBE_URL%
set "PROBE_URL=%GAME_PROBE_URL%"
:provider_probe
if "%GAME_SKIP_LLM_CHECK%"=="1" goto llm_skip
set "LLM_CODE=000"
for /f "usebackq delims=" %%c in (`curl -s -o nul --max-time 15 -w "%%{http_code}" "%PROBE_URL%"`) do set "LLM_CODE=%%c"
if "%LLM_CODE%"=="000" goto llm_dead
if "%LLM_CODE:~0,1%"=="5" goto llm_dead
echo [OK] провайдер модели отвечает: %PROBE_URL% (HTTP %LLM_CODE%)
goto llm_ok
:llm_skip
echo [INFO]  проверка провайдера пропущена: GAME_SKIP_LLM_CHECK=1
goto llm_ok
:llm_dead
echo [WARN] провайдер модели НЕ отвечает: %PROBE_URL% (код %LLM_CODE%)
echo        Проверь MAIN_BASE_URL/MAIN_API_KEY в .env; если модель держится локально —
echo        запусти её (например, llama.cpp на 127.0.0.1:8080) и повтори запуск.
echo        Игра без рассказа не поедет. (Осознанно пропустить: set GAME_SKIP_LLM_CHECK=1)
pause
exit /b 1
:llm_ok

REM 2. Проверка своей ChromaDB (порт 8001)
REM D14 (аудит 38): /api/v1/heartbeat + errorlevel curl → та же схема, что у llama.cpp в
REM п.14: спрашиваем v2 (его же зовёт chroma_client.ping()) и считаем сервис живым при любом
REM коде < 500. Разнобой эндпоинтов/критериев в трёх местах (этот bat, start_chroma.bat,
REM ping()) означал бы, что игра либо отказывается стартовать при живой базе, либо стартует
REM с мёртвой и падает на первом же ходе.
set "CHROMA_CODE=000"
for /f "usebackq delims=" %%c in (`curl -s -o nul --max-time 5 -w "%%{http_code}" http://127.0.0.1:8001/api/v2/heartbeat`) do set "CHROMA_CODE=%%c"
if "%CHROMA_CODE%"=="000" goto chroma_start
if not "%CHROMA_CODE:~0,1%"=="5" (
    echo [OK] Game ChromaDB отвечает на 127.0.0.1:8001 ^(HTTP %CHROMA_CODE%^)
    goto chroma_ok
)
:chroma_start
echo [START] Поднимаю собственную ChromaDB игры на 127.0.0.1:8001...
call scripts\start_chroma.bat
if %errorlevel% neq 0 (
    echo [ERROR] ChromaDB не поднялась. Смотри data\logs\chroma.log
    pause
    exit /b 1
)
:chroma_ok

REM 3. Сервер игры
REM ВАЖНО: слушаем ТОЛЬКО 127.0.0.1. В .env лежат живые API-ключи, у игры нет авторизации —
REM доступ с чужого адреса отдаёт админку (/admin) и весь API всей локальной сети.
if "%GAME_BIND%"=="" set GAME_BIND=127.0.0.1

REM B2 (аудит 41): этот launcher больше НЕ приглашает игрока «открыть наружу». Любой
REM адрес, кроме localhost, теперь требует явного подтверждения GAME_BIND_CONFIRM=1:
REM /api/admin/settings переписывает провайдеров и MAIN_BASE_URL (перехват промптов и
REM ключей), а авторизации в игре нет. Подсказка «как это сделать» из вывода убрана —
REM кто действительно хочет доступ с телефона, тот прочитает комментарий здесь.
REM (При выходе наружу админка всё равно остаётся только с 127.0.0.1 — см. ADMIN_ALLOW_LAN,
REM а лимиты запросов для чужих адресов урезаны в 4 раза — backend/ratelimit.py.)
if "%GAME_BIND%"=="127.0.0.1" goto bind_ok
if "%GAME_BIND%"=="localhost" goto bind_ok
if "%GAME_BIND%"=="::1" goto bind_ok
if "%GAME_BIND_CONFIRM%"=="1" goto bind_warn
echo [ERROR] GAME_BIND=%GAME_BIND% — игра без авторизации ушла бы всей сетью наружу.
echo         Нужен доступ с другого устройства — запусти с GAME_BIND_CONFIRM=1,
echo         помня: в .env лежат живые API-ключи.
pause
exit /b 1
:bind_warn
echo [WARN]  GAME_BIND=%GAME_BIND% — сервер доступен НЕ только на этом ПК.
echo         Админка /api/admin/settings при этом остаётся закрытой на localhost.
:bind_ok

REM D13 (аудит 38): вместо жёсткого абсолютного пути к python.exe — интерпретатор берётся
REM из GAME_PYTHON (можно задать в .env-стиле/окружении), иначе из PATH. Раньше на любой
REM другой машине bat падал с «система не может найти указанный файл» без внятного текста.
if "%GAME_PYTHON%"=="" set "GAME_PYTHON=python"
"%GAME_PYTHON%" -V >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Интерпретатор "%GAME_PYTHON%" не найден в PATH.
    echo         Запусти с GAME_PYTHON=C:\\путь\\python.exe ^(нужен Python 3.11+^),
    echo         либо добавь python в PATH. Установка зависимостей: scripts\\setup_env.bat
    pause
    exit /b 1
)
echo [START] Text Game RPG: http://127.0.0.1:8002
echo [INFO]  bind=%GAME_BIND%  ^(правило 5 в AGENT.md: наружу — только осознанно^)
"%GAME_PYTHON%" -X utf8 -m uvicorn backend.app:app --host %GAME_BIND% --port 8002
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Сервер не запустился или упал ^(код %errorlevel%^).
    echo         Внимательно посмотри сообщения выше. Обычно это:
    echo           - порт 8002 занят другим процессом,
    echo           - ошибка в коде backend ^(тогда читай traceback выше^).
    pause
)
