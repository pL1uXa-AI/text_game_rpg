# -*- coding: utf-8 -*-
"""Само-исцеление человекочитаемых названий флагов (сессия 40, п.12).

Симптом: «Player awakened: да», «Core sealed: да» — игрок читает машинные ключи флагов.
Флаги уже заведённых миров пришли из `starting_state.flags` сюжета, где названия тогда
не задавались (движок научился их читать только в этом пункте). Как и п.9 про карточки,
движок НИЧЕГО не выдумывает: имя берётся у того же сюжета, из которого мир был создан
(`_theme_snapshot.id` → `plots.get_plot(...) → starting_state.flags`), и правится ТОЛЬКО
флаг, у которого своего `flag_titles[key]` ещё нет. Идемпотентно, без LLM.

Законы: 2 — подстановка того, что мир уже знает; 3 — имя флага по-прежнему выбирает
мастер/сюжет, код не переименовывает и не трогает значения.
"""
from __future__ import annotations

from typing import Any, Optional

from . import plots
from .logsetup import get_logger

log = get_logger(__name__)


def _flag_titles_of_snapshot(theme_id: Any) -> dict:
    """Имена флагов, которые задал сюжет (файл plots/) по своему id. {} — сюжет недоступен
    (мир свой/старый/файл удалён) — значит чинить нечем, и это нормально."""
    pid = str(theme_id or "").strip()
    if not pid:
        return {}
    try:
        plot = plots.get_plot(pid)
    except Exception as e:                     # сюжет бьётся — не роняем старт сервера
        log.warning("repair_flag_titles: сюжет %s не прочитан: %s", pid, e)
        return {}
    if not isinstance(plot, dict):
        return {}
    flags = (plot.get("starting_state") or {}).get("flags")
    if not isinstance(flags, dict):
        return {}
    out = {}
    for k, v in flags.items():
        if isinstance(v, dict) and str(v.get("title") or "").strip():
            out[str(k).strip()] = str(v["title"]).strip()[:120]
    return out


def repair_flag_titles(setting: Optional[dict]) -> int:
    """Дополнить `flag_titles` из сюжета-источника. Возвращает число исправленных имён."""
    if not isinstance(setting, dict):
        return 0
    flags = setting.get("flags")
    if not isinstance(flags, dict) or not flags:
        return 0
    snap = setting.get("_theme_snapshot")
    theme_id = snap.get("id") if isinstance(snap, dict) else None
    titles = _flag_titles_of_snapshot(theme_id)
    if not titles:
        return 0
    cur = setting.get("flag_titles")
    cur = dict(cur) if isinstance(cur, dict) else {}
    fixed = 0
    for key in flags:
        k = str(key)
        if not str(cur.get(k) or "").strip() and k in titles:
            cur[k] = titles[k]
            fixed += 1
    if fixed:
        setting["flag_titles"] = cur
    return fixed
