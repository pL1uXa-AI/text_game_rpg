# -*- coding: utf-8 -*-
"""Глобальные настройки админки (таблица `admin_settings`).

Вынесены ИЗ config.py (пункт рефакторинга «admin_settings из config»):
`Config.load()` больше не открывает SQLite сам — он получает переопределения
отсюда. Чтение идёт ОТДЕЛЬНЫМ соединением (только sqlite3, без db.py), поэтому
рекурсия config→db→config не возникает (db._db() вызывает get_config(),
а threading.Lock тут ни при чём — соединение полностью своё).

Приоритет значений: env процесса → admin_settings (админка) → .env
"""
from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"


def db_path() -> str:
    """Путь к БД: env процесса → .env → дефолт data/game.db (как в Config.load)."""
    from .config import strip_env_comment

    db_path = os.environ.get("DB_PATH", "")
    if not db_path and ENV_FILE.exists():
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("DB_PATH="):
                db_path = strip_env_comment(line.partition("=")[2])
                break
    if not db_path:
        db_path = str(ROOT / "data" / "game.db")
    if not Path(db_path).is_absolute():
        db_path = str(ROOT / db_path)
    return db_path


def read_overrides() -> dict:
    """{КЛЮЧ (верх. регистр): значение} из admin_settings — только непустые.

    Ошибки (БД недоступна/сломана) дают {} — игра живёт на .env. Но молчать нельзя
    (правило 14, сессия 36 п.23): вся админка при этом тихо перестаёт действовать, и
    «почему судья логики снова включён» приходится гадать. Пишем об этом ОДИН раз
    (log_once), чтобы не залить лог на каждом обращении к конфигу."""
    try:
        conn = sqlite3.connect(db_path())
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS admin_settings "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL)"
            )
            rows = conn.execute("SELECT key, value FROM admin_settings").fetchall()
        finally:
            conn.close()
        return {str(k).upper(): str(v).strip() for k, v in rows if str(v).strip()}
    except Exception as e:
        from .logsetup import get_logger, log_once
        log_once(get_logger(__name__), "admin-overrides-unavailable", logging.WARNING,
                 "admin_settings недоступна (%s) — переопределения админки игнорируются, "
                 "игра идёт по .env", e)
        return {}


# Реестр «что можно переопределять» (OVERRIDABLE_KEYS) раньше жил здесь и не читался
# НИОТКУДА — из-за чего отстал от реальных настроек админки (сессия 38, D1/B5).
# Единственный источник — config.overridable_env_keys(): производен от dataclass, устареть
# не может; его валидирует роутер и по нему фронт строит «сбросить всё к .env».
