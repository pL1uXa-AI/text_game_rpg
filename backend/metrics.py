# -*- coding: utf-8 -*-
"""metrics.py — Профилирование и мониторинг хода.

Собирает по каждому ходу игрока метрики генерации (время LLM, оценка токенов в промпте/ответе,
размер памяти/контекста, провайдер, температура, качество — `repetition` (доля соседних
dословных повторов) и `lexical_dup_share` (доля повторных слов) — и агрегирует их.

Это лёгкий in-memory реестр (single-process uvicorn). Не критичен: при сбое любого шага
сбора просто логируется warning и метрика пропускается — ход игрока не замедляется и не падает.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Optional


from .logsetup import get_logger

log = get_logger(__name__)

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
    _ensure_totals_baseline()   # база должна быть снята ДО первой собственной записи
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


# ── Восстановление счётчиков после рестарта (сессия 36, п.24) ─────────────
# Кумулятивные `_totals` жили только в памяти: после перезапуска «counters» в
# /api/metrics показывали нули, хотя история ходов осталась в журнале.
#
# Схема: при ПЕРВОМ обращении (до записи своего хода) снимается БАЗА — сумма из
# журнала на этот момент. Дальше отчёт = база + то, что насчитал этот процесс.
# База снимается ровно один раз, иначе собственные записи процесса попали бы в
# счёт дважды (их же пишет и `_persist`). Честно помечаем источник: база — не
# «вся жизнь сервера», а лишь то, что пережило ротацию журнала.
_totals_baseline: dict[str, float] | None = None


def _totals_from_journal() -> dict[str, float]:
    """(llm_calls, llm_seconds, completion_tokens, prompt_tokens) из журнала метрик."""
    base = {"llm_calls": 0, "llm_seconds": 0.0, "completion_tokens": 0, "prompt_tokens": 0}
    try:
        for s in read_journal():
            try:
                base["completion_tokens"] += int(s.get("completion_tokens") or 0)
                base["prompt_tokens"] += int(s.get("prompt_tokens") or 0)
                if s.get("llm_ms") is not None:
                    base["llm_calls"] += 1
                    base["llm_seconds"] += float(s["llm_ms"]) / 1000.0
            except (TypeError, ValueError):
                continue    # битая строка журнала не обрывает агрегирование
    except Exception as e:
        log.warning("metrics: счётчики из журнала не восстановлены: %s", e)
    return base


def _ensure_totals_baseline() -> None:
    """Снять базу один раз — до того, как процесс сам чего-нибудь дописал в журнал."""
    global _totals_baseline
    if _totals_baseline is None:
        _totals_baseline = _totals_from_journal()


def _totals_report() -> dict:
    """Кумулятивы для отчёта: память процесса + база журнала, снятая на старте."""
    _ensure_totals_baseline()
    base = _totals_baseline or {}
    out = dict(_totals)
    if any(base.values()):
        out["llm_calls"] = int(_totals["llm_calls"] + base.get("llm_calls", 0))
        out["llm_seconds"] = round(_totals["llm_seconds"] + base.get("llm_seconds", 0.0), 2)
        out["completion_tokens"] = int(_totals["completion_tokens"] + base.get("completion_tokens", 0))
        out["prompt_tokens"] = int(_totals["prompt_tokens"] + base.get("prompt_tokens", 0))
        out["source"] = "process+journal"
        out["journal_baseline"] = dict(base)
    else:
        out["source"] = "process"
    return out


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


# ── Ротация журнала (сессия 36, п.7) ──────────────────────────────────
# METRICS_TAIL ограничивал только ЧТЕНИЕ: файл рос весь срок жизни игры, и на
# долгом прохождении в data/ скапливались сотни мегабайт JSON-строк.
# Теперь при переходе порога журнал сдвигается в .1/.2/… (тот же подход, что у
# RotatingFileHandler в logsetup). METRICS_MAX_BYTES = 0 — ротация выключена.
_JOURNAL_MAX_BYTES = 4 * 1024 * 1024
_JOURNAL_BACKUPS = 3


def _journal_max_bytes() -> int:
    try:
        from .config import get_config
        return max(0, int(getattr(get_config(), "metrics_max_bytes", _JOURNAL_MAX_BYTES) or 0))
    except Exception:
        return _JOURNAL_MAX_BYTES


def _rotate_journal(p: Path) -> None:
    """Сдвинуть metrics.jsonl → .1 → .2 → … при переполнении (best-effort).

    Ротация — гигиена диска, а не функция игры: её сбой не должен стоить метрики хода."""
    try:
        cap = _journal_max_bytes()
        if not cap or not p.exists() or p.stat().st_size < cap:
            return
        oldest = p.with_name(p.name + f".{_JOURNAL_BACKUPS}")
        if oldest.exists():
            oldest.unlink()
        for i in range(_JOURNAL_BACKUPS - 1, 0, -1):
            src = p.with_name(p.name + f".{i}")
            if src.exists():
                src.rename(p.with_name(p.name + f".{i + 1}"))
        p.rename(p.with_name(p.name + ".1"))
        log.info("metrics: журнал ротирован (%s, порог %d байт)", p.name, cap)
    except Exception as e:
        log.warning("metrics: ротация журнала не удалась: %s", e)


def _persist(entry: dict) -> None:
    p = _file_path()
    if not p:
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        _rotate_journal(p)
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
                pass  # легальный фолбэк: нет конфига — читаем стандартный хвост
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
        "counters": _totals_report(),
        "last": all_samples[-limit:],
        # ── Трейсинг фоновых агентов: среднее/суммарное время, счётчики, последние вызовы ──
        "agents": _agents_report(limit),
    }
    # ── Очередь фоновых агентов (сессия 34, B3): видно, не копится ли фон за ходом ──
    try:
        from . import bg as _bg
        snapshot["bg_queue"] = _bg.stats()
    except Exception as e:
        log.debug("метрики: статистика фоновой очереди недоступна: %s", e)
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
    # A16 (аудит 38): «доля повторных слов» — отдельная, честно названная величина.
    # Её нельзя трактовать как зацикливание: у живой прозы она 0.4–0.55 всегда.
    lex = [s.get("lexical_dup_share") for s in samples
           if s.get("lexical_dup_share") is not None]
    providers = defaultdict(int)
    for s in samples:
        providers[str(s.get("provider") or "?")] += 1
    # ── E3 (сессия 34): качество ответа как измеримые сигналы ──
    # cut_by_limit — ответ упёрся в max_tokens (лечится настройкой «Max токенов»);
    # cut_mid    — модель сама бросила мысль на полуслове (finish_reason=stop, но текст
    #              не закончен). Раньше дописывание и дедуп были, а частоты — нет.
    cuts = [s for s in samples if s.get("cut_by_limit")]
    mids = [s for s in samples if s.get("cut_mid")]
    n = len(samples)
    trimmed = [s for s in samples if s.get("prompt_trimmed")]
    scores = [s.get("rag_score_avg") for s in samples
              if isinstance(s.get("rag_score_avg"), (int, float))]
    return {
        "llm_ms_avg": _avg(llm_ms),
        "llm_ms_max": round(max(llm_ms), 1) if llm_ms else 0,
        "completion_tokens_avg": round(_avg(ct)),
        "prompt_tokens_avg": round(_avg(pt)),
        "memory_tokens_avg": _avg(mem),
        # A16: `repetition` — доля СОСЕДНИХ дословных повторов блоков (реальный цикл модели).
        # «Заметный цикл» — ≥0.15; прежний порог 0.4 belonged лексической мере и ловил «stilist-норму».
        "repetition_avg": _avg(rep),
        "repetition_high_rate": round((sum(1 for x in rep if x and x >= 0.15) / len(rep)) if rep else 0, 3),
        # лексическое разнообразие (1 - уникальные/все слова): ориентир стиля, НЕ диагноз цикла
        "lexical_dup_share_avg": _avg(lex),
        # доля ответов, обрезанных лимитом токенов / оборванных моделью (0..1)
        "cut_by_limit_rate": round(len(cuts) / n, 3) if n else 0,
        "cut_mid_rate": round(len(mids) / n, 3) if n else 0,
        "cut_by_limit_n": len(cuts),
        "cut_mid_n": len(mids),
        # среднее число вспоминаемых фактов и их средняя оценка релевантности (E3)
        "memory_k_avg": _avg([s.get("memory_k") for s in samples
                              if s.get("memory_k") is not None]),
        # средняя оценка релевантности вспомненных фактов (E3): видно, не мажет ли RAG мимо темы
        "rag_score_avg": (round(sum(scores) / len(scores), 3) if scores else None),
        # как часто промпт приходилось усекать под окно модели (A2)
        "prompt_trimmed_rate": round(len(trimmed) / n, 3) if n else 0,
        "provider_dist": {k: v for k, v in providers.items()},
        "n": n,
    }


def snapshot_latest() -> Optional[dict]:
    return _samples[-1] if _samples else None

