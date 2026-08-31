@echo off
REM setup_env.bat — поднять окружение игры с нуля на новой машине/в новом клонов.
REM Делает три вещи:
REM   1) проверяет .env (создаёт из .env.example, если его нет — ключи впишешь сам)
REM   2) ставит зависимости сервера (requirements.txt)
REM   3) создаёт СВОЙ изолированный venv ChromaDB в data\chroma-venv (порт 8001)
REM Опциональные тяжёлые движки (озвучка/локальные эмбеддинги) — см. шаг 4 и README.
REM
REM Запускать из корня проекта. Ничего не перезаписывает без спроса.

setlocal
cd /d %~dp0..

set PY=D:\Development\Development_Tools\Runtimes\Python\3.12.10\python.exe
if not exist "%PY%" (
    echo [ERROR] Питон не найден: "%PY%"
    echo         Поправь переменную PY в этом файле на свой python.exe ^(3.11+^).
    pause
    exit /b 1
)
"%PY%" -V || (echo [ERROR] python не запускается & pause & exit /b 1)

echo.
echo [1/4] Конфигурация
if exist ".env" (
    echo       .env уже есть — не трогаю.
) else (
    copy /Y ".env.example" ".env" >nul
    echo       [!] создан .env из .env.example — ВПИШИ ключи ^(MAIN/EMBEDDING/RERANK^),
    echo           иначе игра поедет только на локальной llama.cpp и без RAG.
)

echo.
echo [2/4] Зависимости сервера
"%PY%" -X utf8 -m pip install -r requirements.txt
if errorlevel 1 ( echo [ERROR] pip install requirements.txt & pause & exit /b 1 )

echo.
echo [3/4] Своя ChromaDB (data\chroma-venv, порт 8001)
if not exist "data\chroma-venv\Scripts\python.exe" (
    "%PY%" -X utf8 -m venv "data\chroma-venv"
    if errorlevel 1 ( echo [ERROR] не создался venv & pause & exit /b 1 )
)
"data\chroma-venv\Scripts\python.exe" -X utf8 -m pip install --upgrade pip >nul
"data\chroma-venv\Scripts\python.exe" -X utf8 -m pip install "chromadb>=0.5.23,<0.7" "posthog==2.5.0"
if errorlevel 1 ( echo [ERROR] не поставилась chromadb в data\chroma-venv & pause & exit /b 1 )
if not exist "data\chroma" mkdir "data\chroma"

echo.
echo [4/4] Опционально
echo       Озвучка (Piper/Kokoro/Edge) + русский голос:  scripts\setup_tts.bat
echo       Локальные эмбеддинги офлайн:                  "%PY%" -m pip install fastembed
echo       (без них игра работает: TTS=none, эмбеддинги=облако или выкл.)

echo.
echo [OK] Готово. Запуск игры:  start_game.bat
pause
