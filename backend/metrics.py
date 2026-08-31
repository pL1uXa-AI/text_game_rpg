# -*- coding: utf-8 -*-
"""metrics.py — Профилирование и мониторинг хода.

Собирает по каждому ходу игрока метрики генерации (время LLM, оценка токенов в промпте/ответе,
размер памяти/контекста, провайдер, температура, качество — повторы) и агрегирует их.

Это лёгкий in-memory реестр (single-process uvicorn). Не критичен: при сбое любого шага
сбора просто логируется warning и метрика пропускается — ход игрока не замедляется и не падает.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Optional

import logging

log = logging.getLogger("textgame")

ROOT = Path(__file__).resolve().parent.parent

# Кольцевой буфер недавних метрик (защита от бесконечного роста памяти).
_MAX_SAMPLES = 500
_samples: deque[dict] = deque(maxlen=_MAX_SAMPLES)
# Кумулятивные счётчики LLM-вызовов и суммарного времени (для среднего за всю жизнь процесса).
_totals: dict[str, Any] = {
    "llm_calls": 0,
    "llm_seconds": 0.0,
    "completion_tokens": 0,
    "prompt_tokens": 0,
}
# ── Трейсинг фоновых агентов (сессия 30): сколько занял каждый фоновый проход ──
# (судья логики, автономный мастер, боевой ИИ, динамические события, карточки, TTS).
_agent_samples: deque[dict] = deque(maxlen=_MAX_SAMPLES)
_agent_totals: dict[str, float] = {}
_agent_counts: dict[str, int] = {}


def _est_tokens(text: str) -> int:
    return max(1, int(len(text or "") / 3.2))


def record(turn_metrics: dict | None = None, **kw) -> None:
    """Сохранить метрику одного события (хода/LLM-вызова).
    Принимает dict или kwargs. Обновляет кумулятивные счётчики и кладёт снапшот в очередь."""
    if not turn_metrics:
        turn_metrics = {}
    if kw:
        merged = dict(turn_metrics)
        merged.update(kw)
        turn_metrics = merged
    if not turn_metrics:
        return
    entry = dict(turn_metrics)
    entry.setdefault("ts", time.time())
    _samples.append(entry)
    _persist(entry)

    # кумулятивы (безопасно, малые типы)
    ct = entry.get("completion_tokens") or 0
    pt = entry.get("prompt_tokens") or 0
    _totals["completion_tokens"] += int(ct)
    _totals["prompt_tokens"] += int(pt)
    dur = entry.get("llm_ms")
    if dur is not None:
        _totals["llm_calls"] += 1
        _totals["llm_seconds"] += float(dur) / 1000.0


def record_agent(agent: str, ms: float, world_id: int | None = None, ok: bool = True) -> None:
    """Записать время фонового агента (трассинг: судья/мастер/боевой ИИ/события/карточки).
    `ms` — миллисекунды выполнения прохода; `ok` — завершился ли успешно.
    Хранится отдельным кольцевым буфером; агрегируется в as_json()."""
    agent = str(agent or "?")[:40]
    entry = {"agent": agent, "ms": round(float(ms), 1), "ts": time.time(), "ok": bool(ok)}
    if world_id is not None:
        entry["world_id"] = int(world_id)
    _agent_samples.append(entry)
    _agent_totals[agent] = _agent_totals.get(agent, 0.0) + float(ms)
    _agent_counts[agent] = _agent_counts.get(agent, 0) + 1


def _avg(nums) -> float:
    n = [x for x in nums if x is not None]
    return round(sum(n) / len(n), 1) if n else 0.0


# ── Переживание перезапуска (сессия 33) ───────────────────────────────────
# In-memory буфер (deque 500) был «амнезией»: рестарт сервера стирал историю времени
# генерации, расходов токенов и кривую качества ответов — а именно по ним видно,
# что модель деградирует. Каждый ход дописывается строкой в data/metrics.jsonl;
# отчёт при пустом буфере (свежий старт) достраивается из хвоста файла.


def _file_path():
    """Путь к журналу метрик или None (персист выключен / нет конфига)."""
    try:
        from .config import get_config
        cfg = get_config()
        if not getattr(cfg, "metrics_persist", True):
            return None
        p = Path(cfg.metrics_file)
        return p if p.is_absolute() else ROOT / p
    except Exception as e:  # метрики не должны ронять ход
        log.warning("metrics: путь к журналу недоступен: %s", e)
        return None


def _persist(entry: dict) -> None:
    p = _file_path()
    if not p:
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("metrics: не удалось дописать %s: %s", p, e)


def read_journal(tail: int | None = None) -> list[dict]:
    """Последние `tail` записей из журнала метрик (повреждённые строки пропускаются)."""
    p = _file_path()
    if not p or not p.exists():
        return []
    try:
        if tail is None:
            cfg_tail = 500
            try:
                from .config import get_config
                cfg_tail = int(get_config().metrics_tail or 500)
            except Exception:
                pass
            tail = cfg_tail
        with p.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()[-max(1, int(tail)) * 4:]  # с запасом на битые строки
    except Exception as e:
        log.warning("metrics: журнал %s не читается: %s", p, e)
        return []
    out: list[dict] = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out[-max(1, int(tail)):] if tail else out


def as_json(limit: int = 20) -> dict:
    """Агрегированный отчёт для дашборда/лога: общее, за окно, средние + последние N снапшотов.

    In-memory буфер пуст после рестарта — тогда окно достраивается из журнала
    data/metrics.jsonl, чтобы история времени/токенов/качества не терялась."""
    all_samples = list(_samples)
    restored_from_journal = False
    if not all_samples:
        all_samples = read_journal()
        restored_from_journal = bool(all_samples)
    recent = all_samples[-60:]
    snapshot = {
        "samples_total": len(all_samples),
        "restored_from_journal": restored_from_journal,
        "windows": {
            "all": _aggregate(all_samples),
            "recent_60": _aggregate(recent),
        },
        "counters": dict(_totals),
        "last": all_samples[-limit:],
        # ── Трейсинг фоновых агентов: среднее/суммарное время, счётчики, последние вызовы ──
        "agents": _agents_report(limit),
    }
    return snapshot


def _agents_report(limit: int = 20) -> dict:
    recent = list(_agent_samples)[-limit:]
    per = {}
    for a in set(_agent_totals):
        n = _agent_counts.get(a, 0)
        per[a] = {
            "calls": n,
            "total_ms": round(_agent_totals.get(a, 0.0), 1),
            "avg_ms": round(_agent_totals.get(a, 0.0) / n, 1) if n else 0.0,
            "errors": sum(1 for s in _agent_samples if s["agent"] == a and not s.get("ok")),
        }
    return {"agents": per, "last": recent}


def _aggregate(samples: list[dict]) -> dict:
    llm_ms = [s.get("llm_ms") for s in samples if s.get("llm_ms") is not None]
    ct = [s.get("completion_tokens") or 0 for s in samples]
    pt = [s.get("prompt_tokens") or 0 for s in samples]
    mem = [s.get("memory_tokens") for s in samples if s.get("memory_tokens") is not None]
    rep = [s.get("repetition") for s in samples if s.get("repetition") is not None]
    providers = defaultdict(int)
    for s in samples:
        providers[str(s.get("provider") or "?")] += 1
    return {
        "llm_ms_avg": _avg(llm_ms),
        "llm_ms_max": round(max(llm_ms), 1) if llm_ms else 0,
        "completion_tokens_avg": round(_avg(ct)),
        "prompt_tokens_avg": round(_avg(pt)),
        "memory_tokens_avg": _avg(mem),
        "repetition_avg": _avg(rep),
        "repetition_high_rate": round((sum(1 for x in rep if x and x >= 0.4) / len(rep)) if rep else 0, 3),
        "provider_dist": {k: v for k, v in providers.items()},
        "n": len(samples),
    }


def snapshot_latest() -> Optional[dict]:
    return _samples[-1] if _samples else None

