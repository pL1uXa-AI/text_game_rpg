# -*- coding: utf-8 -*-
"""narrators_loader.py — загрузчик предустановленных рассказчиков из файлов.

Рассказчики лежат как ОТДЕЛЬНЫЕ файлы (формат — чистый JSON по схеме
PLOTS.md/README, расширение `.js`) в папке:

    plots/narrators/   — предустановленные персоны рассказчиков

Каждый файл — JSON-объект с полями:
    { "name": "Имя", "desc": "Краткое описание", "prompt": "Персона/промпт" }

Движок при старте сканирует папку и отдаёт список пресетов в
`db.seed_narrators` (INSERT OR IGNORE по name — идемпотентно). Правка файла +
перезапуск (или вызов reload()) обновляет список пресетов, которые будут
засеяны в НОВЫХ базах; уже созданные записи в БД не трогаются (name UNIQUE).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .logsetup import get_logger

log = get_logger(__name__)

NARRATORS_ROOT = Path(__file__).resolve().parent.parent / "plots" / "narrators"

# Живой список пресетов (dict {name, desc, prompt}) — по нему сидится БД
NARRATOR_PRESETS: list[dict] = []
# Кэш сигнатур файлов (путь -> (mtime_ns, size)) — дешёвая проверка «изменилось что-то»
_SIGS: dict[str, tuple[int, int]] = {}


def _scan() -> list[dict]:
    """Сканирует plots/narrators/*.js и возвращает список пресетов.
    Один битый файл не роняет остальные (пропуск с warning)."""
    out: list[dict] = []
    if not NARRATORS_ROOT.is_dir():
        return out
    for f in sorted(NARRATORS_ROOT.glob("*.js")):
        fpath = str(f)
        try:
            _SIGS[fpath] = (f.stat().st_mtime_ns, f.stat().st_size)
        except OSError:
            pass
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("narrators: пропущен файл %s (не JSON): %s", f, e)
            continue
        if not isinstance(data, dict):
            log.warning("narrators: пропущен %s (не JSON-объект)", f)
            continue
        name = str(data.get("name") or "").strip()
        prompt = str(data.get("prompt") or "").strip()
        if not name or not prompt:
            log.warning("narrators: пропущен %s (нет name или prompt)", f)
            continue
        out.append({
            "name": name,
            "desc": str(data.get("desc") or "").strip(),
            "prompt": prompt,
        })
    return out


def reload() -> list[dict]:
    """Полная перезагрузка пресетов с диска. Вызывается при старте и вручную."""
    global _SIGS
    _SIGS = {}
    presets = _scan()
    NARRATOR_PRESETS.clear()
    NARRATOR_PRESETS.extend(presets)
    log.info("narrators reload: %d пресетов из plots/narrators", len(presets))
    return presets


def _mtime_changed() -> bool:
    """True, если набор файлов рассказчиков изменился (новый/удалён/правлен)."""
    seen: dict[str, tuple[int, int]] = {}
    if NARRATORS_ROOT.is_dir():
        for f in NARRATORS_ROOT.glob("*.js"):
            try:
                seen[str(f)] = (f.stat().st_mtime_ns, f.stat().st_size)
            except OSError:
                pass
    if not seen and not _SIGS:
        return False
    return seen != _SIGS


def ensure_fresh() -> bool:
    """Дешёвая проверка mtime — подхватывает новые/изменённые файлы БЕЗ перезапуска
    (список пресетов для будущих сидов; уже созданные в БД записи не трогает).
    Возвращает True, если файлы изменились и пресеты перечитаны."""
    try:
        if _mtime_changed():
            reload()
            return True
    except Exception as e:
        log.warning("narrators.ensure_fresh: %s", e)
    return False


# Загрузка при импорте (движок стартует уже с пресетами)
try:
    reload()
except Exception as e:
    log.warning("narrators init: %s", e)
