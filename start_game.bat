@echo off
REM Text Game RPG — запуск сервера игры.
REM Сессия 83: файлы .bat сохранены в UTF-8, а консоль Windows по умолчанию в cp866 —
REM поэтому кириллица в окне лаунчера выглядела «кракозябрами» (Г—Р°РїСѓСЃРє). Ставим
REM кодовую страницу UTF-8 и шрифт консоли; chcp влияет только на это окно.
chcp 65001 >nul
REM Требования: 1) провайдер модели из MAIN_BASE_URL в .env (локальная модель или облако)
REM             2) своя ChromaDB на 8001 (поднимется сама).
REM Консоль НЕ закрывается при ошибке: показываем сообщение и ждём Enter.

setlocal
cd /d %~dp0

REM ── Адрес, по которому игра ПУБЛИКУЕТСЯ и открывается ──────────────────────
REM Сессия 85 (жалоба: «запускается 127.0.0.1, а не 192.168.1.101»): лаунчер печатал
REM и открывал в браузере жёстко http://127.0.0.1:8002, даже когда сервер слушал
REM сетевой адрес. Игрок видел localhost и не понимал, что с телефона надо идти по IP.
REM
REM Теперь адрес считается из GAME_BIND: для «все интерфейсы» (0.0.0.0) и loopback
REM подставляется LAN-адрес машины, а если сети нет — откатываемся на 127.0.0.1.
REM GAME_PUBLIC_HOST можно задать явно, если адрес нужно опубликовать не тот, что в bind.
if "%GAME_BIND%"=="" set "GAME_BIND=127.0.0.1"
set "GAME_URL_HOST=%GAME_PUBLIC_HOST%"
if not "%GAME_URL_HOST%"=="" goto url_ready
echo %GAME_BIND% | findstr /b /c:"127.0.0.1" /c:"localhost" /c:"::1" >nul 2>&1
if errorlevel 1 set "GAME_URL_HOST=%GAME_BIND%"
if not "%GAME_URL_HOST%"=="" goto url_ready
echo %GAME_BIND% | findstr /b /c:"0.0.0.0" /c:"::" >nul 2>&1
if errorlevel 1 goto url_loopback
REM «все интерфейсы»: берём LAN-адрес этой машины через scripts\lanip.ps1 (он отдаёт
REM только 192.168.* / 10.* — VPN-туннель 198.18 и Hyper-V 172.27 с телефона не годятся).
REM Отдельный .ps1, а не однострочник: внутри .bat экранирование кавычек ломалось, и в
REM переменную попадал мусор либо пусто (проверено).
set "GAME_URL_HOST="
for /f "usebackq delims=" %%I in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\lanip.ps1"`) do set "GAME_URL_HOST=%%I"
if not "%GAME_URL_HOST%"=="" goto url_ready
:url_loopback
set "GAME_URL_HOST=127.0.0.1"
:url_ready
REM ЖИВОЙ адрес игры: к нему идёт игрок (и в браузере, и с телефона).
set "GAME_URL=http://%GAME_URL_HOST%:8002"

REM 0. Если игра уже запущена — второй раз не запускаем
netstat -ano | findstr ":8002" | findstr "LISTENING" >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] Игра уже запущена: %GAME_URL%
    REM Показываем и loopback: на самом ПК всегда работает короткий адрес.
    echo      локально: http://127.0.0.1:8002
    REM GAME_NO_BROWSER задан — вкладку НЕ открывать: нужно автотестам (харнесс
    REM tests/test_start_bat_harness.py) и автозапуску, где окно браузера — вредный
    REM побочный эффект. Игрок по умолчанию видит прежнее поведение.
    if "%GAME_NO_BROWSER%"=="" start "" %GAME_URL%
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
REM ВАЖНО: адрес по умолчанию — 127.0.0.1 (GAME_BIND уже задан в начале скрипта, см.
REM блок «Адрес, по которому игра ПУБЛИКУЕТСЯ»). В .env лежат живые API-ключи, у игры
REM нет авторизации — доступ с чужого адреса открывает весь API локальной сети,
REM поэтому не-loopback адрес требует осознанного GAME_BIND_CONFIRM=1.

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
REM из GAME_PYTHON (можно задать в .env-стиле/окружении), иначе ищется сам.
REM
REM Сессия 83 (реальный сбой у игрока): в PATH первым стоит `python.exe` из
REM %LOCALAPPDATA%\Microsoft\WindowsApps — это ЗАГЛУШКА Microsoft Store: она не запускает
REM Python, а печатает «Python was not found; run without arguments to install from the
REM Store». Проверка `python -V` при этом «успешна» (процесс-заглушка возвращает 0), и
REM лаунчер уходил запускать uvicorn «успешным» python, который ничего не делает.
REM Поэтому кандидаты перебираются по порядку и проверяются НАСТОЯЩИМ запуском Python.
if not "%GAME_PYTHON%"=="" goto py_check
REM Поиск в порядке надёжности: обычный python → py launcher → типовые места установки.
REM Список путей собирается из %LOCALAPPDATA% (без зашитых букв диска), поэтому лаунчер
REM работает на любой машине; GAME_PYTHON по-прежнему перекрывает всё.
REM 1) обычный python (может быть заглушкой Store — отсеется проверкой ниже)
set "GAME_PYTHON=python"
%GAME_PYTHON% -c "import sys" >nul 2>&1
if %errorlevel% equ 0 goto py_check
REM 2) py launcher (штатный установщик Python с python.org)
set "GAME_PYTHON=py -3"
%GAME_PYTHON% -c "import sys" >nul 2>&1
if %errorlevel% equ 0 goto py_check
REM 3) типовые места установки для текущего пользователя
set "GAME_PYTHON="
for %%V in (314 313 312 311) do if not defined GAME_PYTHON if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" set "GAME_PYTHON=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
if not defined GAME_PYTHON goto py_missing
:py_check
REM ⚠ БЕЗ кавычек вокруг "%GAME_PYTHON%": при значении «py -3» кавычки склеивают «py -3» в
REM ОДНО имя файла, и запуск проваливается (проверено). Пути с пробелами сюда не попадают —
REM лаунчер сам берёт только существующие python.exe и py.exe.
%GAME_PYTHON% -c "import sys" >nul 2>&1
if %errorlevel% neq 0 goto py_missing
goto py_ok
:py_missing
echo [ERROR] Python не найден или в PATH стоит заглушка Microsoft Store.
echo         Поставь Python 3.11+ с python.org ^(галочка "Add python.exe to PATH"^)
echo         или укажи свой: set GAME_PYTHON=C:\\путь\\python.exe
echo         Установка зависимостей игры: scripts\\setup_env.bat
pause
exit /b 1
:py_ok
echo [START] Text Game RPG: %GAME_URL%
echo         локально: http://127.0.0.1:8002  ^(если слушаем все интерфейсы^)
echo [INFO]  bind=%GAME_BIND%  ^(правило 5 в AGENT.md: наружу — только осознанно^)
echo [INFO]  интерпретатор: %GAME_PYTHON%
%GAME_PYTHON% -X utf8 -m uvicorn backend.app:app --host %GAME_BIND% --port 8002
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Сервер не запустился или упал ^(код %errorlevel%^).
    echo         Внимательно посмотри сообщения выше. Обычно это:
    echo           - порт 8002 занят другим процессом,
    echo           - ошибка в коде backend ^(тогда читай traceback выше^).
    pause
)
