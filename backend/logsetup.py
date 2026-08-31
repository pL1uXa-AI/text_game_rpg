# -*- coding: utf-8 -*-
"""logsetup.py — структурированное логирование игры (сессия 34, завершение правила 14).

Раньше `logging` не настраивался вообще: сообщения уходили в конвук uvicorn плоскими
строками, а диагностику фоновых агентов (судья/мастер/карточки/события) нельзя было ни
найти, ни связать с конкретным ходом. Плюс в коде оставались тихие `except: pass`.

Что даёт модуль:
  * **JSON-строки** (одна запись = один объект) — логи ищутся инструментами, а не глазами;
  * **контекст хода** через `contextvars` (`world_id`, `turn_seq`, `agent`, `request_id`) —
    всё, что написано внутри `turn_context(...)`, помечено этим ходом автоматически;
  * **файл** `data/logs/game.log` c ротацией (`LOG_FILE`/`LOG_MAX_BYTES`/`LOG_BACKUP_COUNT`)
    плюс обычный человекочитаемый вывод в консоль (stderr), чтобы не ломать привычный запуск;
  * **тихие `except: pass` становятся видимыми** — хелпер `quiet()`/`log_debug_once()` для
    легальных фолбэков, которые не должны засорять warning-уровень, но остаются в debug.

Правила проекта: настройки LLM/ключей в лог не пишутся (секреты — правило 3); в JSON поля
`msg` обрезается до разумной длины, `exc` — traceback одной строкой.

Использование:
    from .logsetup import get_logger, turn_context
    log = get_logger(__name__)
    with turn_context(world_id=7, seq=142, agent="judge"):
        log.info("проверка фактов")      # → JSON с world_id=7, seq=142, agent="judge"
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import logging.handlers
import os
import sys

import time
from pathlib import Path
from typing import Any, Iterator, Optional

ROOT = Path(__file__).resolve().parent.parent

# ── Контекст хода (переживаёт await, не ломается на потоках) ─────────────────
_ctx_world: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar("tg_world", default=None)
_ctx_seq: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar("tg_seq", default=None)
_ctx_agent: contextvars.ContextVar[str] = contextvars.ContextVar("tg_agent", default="")
_ctx_rid: contextvars.ContextVar[str] = contextvars.ContextVar("tg_rid", default="")

_CONFIGURED = False
_file_handler: logging.Handler | None = None
_MAX_MSG = 1200

# Ключи, значения которых никогда не пишутся в лог (правило 3 — секреты).
_SECRET_HINTS = ("api_key", "apikey", "authorization", "token", "secret", "password")


def current_context() -> dict[str, Any]:
    """Текущий логический контекст (для явной передачи в метрики/исключения)."""
    out: dict[str, Any] = {}
    w, s, a, r = _ctx_world.get(), _ctx_seq.get(), _ctx_agent.get(), _ctx_rid.get()
    if w is not None:
        out["world_id"] = w
    if s is not None:
        out["turn_seq"] = s
    if a:
        out["agent"] = a
    if r:
        out["request_id"] = r
    return out


@contextlib.contextmanager
def turn_context(world_id: int | None = None, seq: int | None = None,
                 agent: str | None = None, request_id: str | None = None) -> Iterator[dict]:
    """Логический контекст: всё залогированное внутри получает метки хода/агента.

    Вложенность корректна (контекст восстанавливается на выходе). Возвращает словарь
    контекста — удобно для `metrics.record_agent(..., **ctx)`.
    """
    tokens: list[tuple] = []
    if world_id is not None:
        tokens.append((_ctx_world, _ctx_world.set(int(world_id))))
    if seq is not None:
        tokens.append((_ctx_seq, _ctx_seq.set(int(seq))))
    if agent is not None:
        tokens.append((_ctx_agent, _ctx_agent.set(str(agent))))
    if request_id is not None:
        tokens.append((_ctx_rid, _ctx_rid.set(str(request_id))))
    try:
        yield current_context()
    finally:
        for var, tok in reversed(tokens):
            var.reset(tok)


def scrub(d: Any) -> Any:
    """Рекурсивно вырезает секретные поля из структуруемых данных перед записью в лог."""
    if isinstance(d, dict):
        out = {}
        for k, v in d.items():
            if any(h in str(k).lower() for h in _SECRET_HINTS):
                out[k] = "***"
            else:
                out[k] = scrub(v)
        return out
    if isinstance(d, (list, tuple)):
        return [scrub(x) for x in d]
    return d


class JsonFormatter(logging.Formatter):
    """Один лог = один JSON-объект: msg + контекст хода + поля extra + exc."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        if len(msg) > _MAX_MSG:
            msg = msg[:_MAX_MSG] + "…"
        obj: dict[str, Any] = {
            "ts": round(record.created, 3),
            "level": record.levelname,
            "logger": record.name,
            "msg": msg,
        }
        obj.update(current_context())
        # произвольные структурированные поля, переданные через extra={"fields": {...}}
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            for k, v in scrub(fields).items():
                if k not in obj:
                    obj[k] = v
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info).replace("\n", " | ")[:900]
        return json.dumps(obj, ensure_ascii=False, default=str)


class PlainFormatter(logging.Formatter):
    """Человекочитаемый вывод для консоли (запуск из start_game.bat): с контекстом хода."""

    def format(self, record: logging.LogRecord) -> str:
        base = f"{time.strftime('%H:%M:%S', time.localtime(record.created))} " \
               f"{record.levelname:<7} {record.name.replace('backend.', '')}: {record.getMessage()}"
        ctx = current_context()
        tags = []
        if "world_id" in ctx:
            tags.append(f"w{ctx['world_id']}")
        if "turn_seq" in ctx:
            tags.append(f"s{ctx['turn_seq']}")
        if ctx.get("agent"):
            tags.append(ctx["agent"])
        if tags:
            base = f"[{' '.join(tags)}] {base}"
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def _env(name: str, default: str) -> str:
    """Значение из окружения процесса → .env (админку тут не читаем: config→db→config рекурсия).
    Тот же приоритет, что у `Config.load` (пустое значение = дефолт), поэтому `LOG_FILE=off`
    отключает файл, а пустое поле — оставляет стандартный путь."""
    v = os.environ.get(name)
    if v is None:
        v = _dotenv_value(name)
    return (v or default).strip() if v is not None else default


_DOTENV: dict[str, str] | None = None


def _dotenv_value(name: str) -> str | None:
    """Одно чтение .env (кэшируется) — чтобы лог-настройки работали до импорта config.py."""
    global _DOTENV
    if _DOTENV is None:
        _DOTENV = {}
        try:
            env_file = ROOT / ".env"
            if env_file.exists():
                for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    if not line or line.lstrip().startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    _DOTENV[k.strip()] = v.strip().strip('"').strip("'")
        except Exception:
            _DOTENV = {}
    return _DOTENV.get(name)


def _cfg_value(name: str, default):
    """То же, но через полный приоритет конфига (env → админка → .env), если config уже собран."""
    try:
        from .config import get_config
        return getattr(get_config(), name.lower(), default)
    except Exception:
        return _env(name, str(default))


def configure(level: str | None = None) -> logging.Logger:
    """Настраивает логгер `textgame` (идемпотентно — повторные вызовы не плодят хендлеры).

    Уровень — LOG_LEVEL (.env/окружение) или аргумент; файл — LOG_FILE (пусто = без файла).
    Вызывается один раз при импорте приложения (app.py) и защищено от двойного вызова
    в тестах/скриптах.
    """
    global _CONFIGURED
    root = logging.getLogger("textgame")
    lvl_name = str(level or _cfg_value("LOG_LEVEL", "INFO")).upper()
    root.setLevel(getattr(logging, lvl_name, logging.INFO))

    if _CONFIGURED:
        return root

    plain = PlainFormatter()

    if not root.handlers:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(plain)
        root.addHandler(sh)
    # Записи идут И В НАШИ хендлеры, И наверх (propagate=True): наверху висит handler pytest
    # (caplog / --log-cli), которым проверяется правило 14. Чтобы в бою не появился дубль
    # от lastResort (он печатает в stderr, если в иерархии вообще нет хендлеров), вешаем
    # на корень NullHandler, когда он пуст — сам он ничего не выводит.
    root.propagate = True
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        root_logger.addHandler(logging.NullHandler())

    # Файл JSON. Путь берём из .env/окружения напрямую (config→db→config — рекурсия опасна,
    # см. AGENT.md про admin_settings), админ-оверраид применяется повторным configure().
    file_env = str(_env("LOG_FILE", "data/logs/game.log"))
    if file_env and file_env.lower() not in ("off", "none", ""):
        try:
            path = Path(file_env)
            if not path.is_absolute():
                path = ROOT / path
            path.parent.mkdir(parents=True, exist_ok=True)
            max_bytes = int(_env("LOG_MAX_BYTES", "5242880") or 0) or 5_242_880
            backups = int(_env("LOG_BACKUP_COUNT", "3") or 0) or 3
            fh = logging.handlers.RotatingFileHandler(
                path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
            fh.setFormatter(JsonFormatter())
            _file_handler = fh
            root.addHandler(fh)
        except Exception as e:      # лог-файл не должен валить запуск игры
            root.warning("лог-файл недоступен (%s): пишу только в консоль", e)

    # Приглушаем болтливые библиотеки, но оставляем ошибки.
    for noisy in ("httpx", "httpcore", "aiosqlite", "edge_tts", "fastembed", "onnxruntime"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))

    _CONFIGURED = True
    root.debug("логирование настроено: level=%s file=%s", lvl_name, file_env or "—")
    return root


def reset_for_tests() -> None:
    """Снять хендлеры и отметки log_once (тесты: изолировать лог от боевого data/logs)."""
    global _CONFIGURED, _file_handler
    root = logging.getLogger("textgame")
    for h in list(root.handlers):
        root.removeHandler(h)
        with contextlib.suppress(Exception):
            h.close()
    _file_handler = None
    _CONFIGURED = False
    reset_once()


def reconfigure() -> logging.Logger:
    """Переприменить уровень/файл после сохранения админ-настроек (invalidate_config)."""
    global _CONFIGURED, _file_handler
    root = logging.getLogger("textgame")
    if _file_handler is not None:
        with contextlib.suppress(Exception):
            root.removeHandler(_file_handler)
            _file_handler.close()
        _file_handler = None
    _CONFIGURED = False
    return configure()


def get_logger(name: str | None = None) -> logging.Logger:
    """Логгер игры. Имя потомок `textgame`, чтобы общий уровень/хендлеры были одни,
    но в логе было видно, какой модуль писал (textgame.routers.core и т.п.)."""
    configure()
    if not name:
        return logging.getLogger("textgame")
    short = name[len("backend."):] if name.startswith("backend") else name
    short = short.replace("backend", "").strip(".") or "core"
    return logging.getLogger(f"textgame.{short}")


# ── Помощники против тихих исключений (правило 14) ──────────────────────────
_seen: set[str] = set()


def log_once(logger: logging.Logger, key: str, level: int, fmt: str, *args: Any) -> None:
    """Логирует конкретную повторяющуюся проблему ОДИН раз за процесс.

    Нужен для легальных фолбэков, которые иначе либо молча глотаются (`except: pass` —
    запрещёно правилом 14), либо заливают лог на каждом ходе (например «Chroma не отвечает»
    при выключенной базе). Ключ — короткая метка места.
    """
    if key in _seen:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("[once:%s] повтор", key)
        return
    _seen.add(key)
    logger.log(level, fmt + "  (дальше это не повторяется в логе)", *args)


def reset_once() -> None:
    """Забыть отметки log_once (тесты/длительный процесс после починки сервиса)."""
    _seen.clear()
