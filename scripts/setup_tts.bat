@echo off
REM ─────────────────────────────────────────────────────────────
REM setup_tts.bat — установка TTS (озвучка рассказчика)
REM   1) ставит пакеты: sherpa-onnx (локальные Piper/Kokoro), edge-tts (облако Edge)
REM   2) скачивает русский голос Piper по умолчанию (ru_RU-ruslan-medium, ~70 МБ)
REM Запускать из корня проекта. Интернет обязателен.
REM ─────────────────────────────────────────────────────────────
chcp 65001 >nul
setlocal
cd /d "%~dp0.."

REM D13-хвост (аудит 38): как в setup_env.bat — интерпретатор берётся из PY (можно задать
REM руками: set PY=C:\путь\python.exe), иначе «python» из PATH. Абсолютный путь зашит быть
REM не может: на другой машине bat падал с «система не может найти указанный файл».
if "%PY%"=="" set "PY=python"
"%PY%" -V 1>nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python 3.11+ не найден "(PY=%PY%)".
  echo         Запусти так:  set PY=C:\путь\python.exe ^&^& scripts\setup_tts.bat
  pause
  exit /b 1
)

echo [1/2] Установка пакетов: sherpa-onnx, edge-tts ...
"%PY%" -X utf8 -m pip install --quiet sherpa-onnx edge-tts
if errorlevel 1 (
  echo [ERROR] Не удалось установить пакеты. Проверь интернет и pip.
  pause
  exit /b 1
)
echo [OK] Пакеты установлены.

echo [2/2] Проверка/скачивание русского голоса Piper ...
"%PY%" -X utf8 -c "import asyncio; from backend import tts; res = asyncio.run(tts.download_piper_voice('ru_RU-ruslan-medium')); print('Voice:', res)"
if errorlevel 1 (
  echo [ERROR] Не удалось скачать голос.
  pause
  exit /b 1
)
echo [OK] Голос готов. Перезапусти сервер: start_game.bat

pause
