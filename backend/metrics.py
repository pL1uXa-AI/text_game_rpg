# -*- coding: utf-8 -*-
"""metrics.py — Профилирование и мониторинг хода.

Собирает по каждому ходу игрока метрики генерации (время LLM, оценка токенов в промпте/ответе,
размер памяти/контекста, провайдер, температура, качество — `repetition` (доля соседних
dословных повторов) и `lexical_dup_share` (доля повторных слов) — и агрегирует их.

Это лёгкий in-memory реестр (single-process uvicorn). Не критичен: при сбое любого шага
сбора просто логируется warning и метрика пропускается — ход игрока не замедляется и не падает.
"""
from __future__ import annotations

import asyncio
import json
import threading
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
    Принимает dict или kwargs. Обновляет кумулятивные счётчики и кладёт снапшот в очередь.

    D14 (аудит 41): на диск метрика уходит НЕ сразу, а пачкой (см. «Буфер журнала» ниже) —
    ход игрока больше не платит `open(...,'a')` за каждую метрику."""
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
    _queue_persist(entry)

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


def _append_lines(p: Path, items: list[dict]) -> None:
    """Один `open(...,'a')` на пачку записей. ФОРМАТ ЖУРНАЛА НЕ МЕНЯЕТСЯ: JSON-строка на метрику.

    Сбой — warning (метрики не должны ронять ни ход, ни фоновый сброс), строки теряются:
    журнал — диагностика, а не игровая память (см. bus.py: ему точность не нужна тем более)."""
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        _rotate_journal(p)
        with p.open("a", encoding="utf-8") as fh:
            for entry in items:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("metrics: не удалось дописать %s (%d записей): %s", p, len(items), e)


def _persist(entry: dict) -> None:
    """Записать ОДНУ метрику сразу, минуя буфер (низкоуровневая точка; её же дергает `flush`)."""
    p = _file_path()
    if not p:
        return
    _append_lines(p, [entry])


# ── Буфер журнала (D14, аудит 41) ─────────────────────────────────────────
# `record()` вызывается СИНХРОННО из async-хода (`routers/core.py`), и раньше каждая
# метрика значила `mkdir` + `open(...,'a')` + запись в data/metrics.jsonl на горячем пути
# (плюс проверка ротации). Теперь метрика ложится в память, а на диск уходит ПАЧКА:
#   * по объёму — накопилось `_FLUSH_BATCH` строк (`record` зовётся раз на ход, значит пачка
#     это ~20 ходов: раньше каждый из них стоил своего `open()`);
#   * по возрасту — первая ждущая строка старше `_FLUSH_AGE_S` (проверка на каждой новой
#     записи, «амортизированно», как `_prune` в ratelimit: отдельного таймера нет);
#   * при остановке сервера (`app._lifespan`) и при явном `flush()` — синхронно.
# Цикл событий не блокируется: сброс уходит в поток (`run_in_executor`), а если цикла нет
# (скрипты, pytest) — пишется сразу синхронно, чтобы «запустил скрипт → журнал на месте».
# Цена честности: последние ≤ `_FLUSH_BATCH` строк могут отставать от `/api/metrics` (он
# читает память, она свежее) и от диска, пока процесс жив; поле `journal_pending` в отчёте
# это видно. `METRICS_PERSIST=false` (conftest) — буфер даже не заводится: ноль I/O в tests.
_FLUSH_BATCH = 20        # сколько метрик ждём до записи одной пачкой (~20 ходов)
_FLUSH_AGE_S = 2.0
_MAX_PENDING = 500          # потолок памяти: переполнился — пишем сразу, копить не даём
_pending: list[dict] = []
_pending_lock = threading.Lock()
_pending_since: float = 0.0        # monotonic-момент, когда буфер стал непустым
_flush_fut: "Optional[asyncio.Future]" = None
_flush_loop: Optional[asyncio.AbstractEventLoop] = None   # цикл, которому принадлежит fut
_flush_writes = 0                  # сколько пачек ушло на диск (наблюдаемость)


def _queue_persist(entry: dict) -> None:
    """Положить метрику в буфер журнала и, при зрелости пачки, заказать сброс."""
    global _pending_since
    if _file_path() is None:
        return                      # персист выключен / нет конфига — буфер не нужен
    try:
        loop: "Optional[asyncio.AbstractEventLoop]" = asyncio.get_running_loop()
    except RuntimeError:
        loop = None                 # вне asyncio (скрипт/тест) — писать сразу, синхронно
    due = False
    first = False
    with _pending_lock:
        first = not _pending
        if first:
            _pending_since = time.monotonic()
        _pending.append(entry)
        if (len(_pending) >= _FLUSH_BATCH
                or len(_pending) > _MAX_PENDING
                or (time.monotonic() - _pending_since) >= _FLUSH_AGE_S):
            due = True
    if due or loop is None:
        _schedule_flush(loop)
        return
    if first:
        # «разрядка» по возрасту: если новых метрик больше не будет (игрок замолчал),
        # отложенный вызов сам донесёт пачку до диска — отдельного таймера не заводим.
        try:
            loop.call_later(_FLUSH_AGE_S, _schedule_flush, loop)
        except Exception as e:
            log.warning("metrics: отложенный сброс не поставлен, пишу синхронно: %s", e)
            flush()


def _schedule_flush(loop: "Optional[asyncio.AbstractEventLoop]" = None) -> None:
    """Сбросить буфер, не вставая в позу циклу событий.

    Один писатель за раз: если прошлый сброс ещё в работе в ЭТОМ ЖЕ цикле, новые строки
    догонят его пачку (буфер общий), второго `open()` на тот же файл не заводим. Future из
    чужого цикла (TestClient приносит новый на каждый тест, и он к этому моменту уже закрыт)
    не переживёт нас — сброс перепривязывается к живому циклу, иначе буфер завис бы навечно."""
    global _flush_fut, _flush_loop
    try:
        running: "Optional[asyncio.AbstractEventLoop]" = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if loop is None:
        loop = running
    if loop is None or loop is not running or loop.is_closed():
        _flush_fut = _flush_loop = None
        flush()                     # активного цикла нет (скрипт/тест) — пишем сразу
        return
    if _flush_fut is not None and _flush_loop is loop and not _flush_fut.done():
        return
    try:
        fut = loop.run_in_executor(None, flush)
    except Exception as e:          # нет пула / цикл закрывается — не терять метрики
        log.warning("metrics: фоновый сброс не запланирован, пишу синхронно: %s", e)
        _flush_fut = _flush_loop = None
        flush()
        return
    fut.add_done_callback(_on_flush_done)
    _flush_fut, _flush_loop = fut, loop


def _on_flush_done(fut: "asyncio.Future") -> None:
    """Правило 14: отказ фонового сброса виден в журнале, а не молчит."""
    global _flush_fut, _flush_loop
    try:
        fut.result()
    except Exception as e:
        log.warning("metrics: фоновый сброс журнала не удался: %s", e)
    finally:
        if _flush_fut is fut:
            _flush_fut = _flush_loop = None


def flush() -> int:
    """Записать всё, что накоплено в буфере, одной пачкой. Возвращает число строк.

    Синхронная и безопасная из любого места: вызывается при остановке сервера, из тестов
    и когда активного цикла событий нет."""
    global _pending_since, _flush_writes
    with _pending_lock:
        if not _pending:
            _pending_since = 0.0
            return 0
        items = _pending[:]
        _pending.clear()
        _pending_since = 0.0
    p = _file_path()
    if not p:
        return 0                    # персист выключен по дороге: строки просто забыты
    _append_lines(p, items)
    _flush_writes += 1
    return len(items)


def pending_count() -> int:
    """Сколько метрик ещё не дошло до журнала (наблюдаемость буфера)."""
    with _pending_lock:
        return len(_pending)


def buffer_stats() -> dict:
    """Снимок буфера для `/api/metrics`: сколько ждёт, сколько пачек ушло, возраст heads."""
    with _pending_lock:
        age = round(time.monotonic() - _pending_since, 2) if _pending else 0.0
        return {"pending": len(_pending), "age_s": age, "flushes": _flush_writes,
                "batch": _FLUSH_BATCH, "max_age_s": _FLUSH_AGE_S}


def reset_buffer() -> None:
    """Забыть буфер (только для тестов: состояние модуля не должно протекать между тестами)."""
    global _pending_since, _flush_fut, _flush_loop, _flush_writes
    with _pending_lock:
        _pending.clear()
        _pending_since = 0.0
    _flush_fut = _flush_loop = None
    _flush_writes = 0


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
    # Буфер намеренно НЕ сбрасываем: он синхронно писал бы I/O из async-роута, а память
    # всегда свежее журнала — отчёту недостающие на диске строки не нужны.
    all_samples = list(_samples)
    restored_from_journal = False
    if not all_samples:
        all_samples = read_journal()
        restored_from_journal = bool(all_samples)
    recent = all_samples[-60:]
    snapshot = {
        "samples_total": len(all_samples),
        "restored_from_journal": restored_from_journal,
        # D14: сколько метрик ещё в буфере и не дописано в data/metrics.jsonl
        "journal_pending": pending_count(),
        "journal_buffer": buffer_stats(),
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
    llm_ms = [float(s["llm_ms"]) for s in samples
              if isinstance(s.get("llm_ms"), (int, float))]
    ct = [s.get("completion_tokens") or 0 for s in samples]
    pt = [s.get("prompt_tokens") or 0 for s in samples]
    mem = [s.get("memory_tokens") for s in samples if s.get("memory_tokens") is not None]
    rep = [s.get("repetition") for s in samples if s.get("repetition") is not None]
    # A16 (аудит 38): «доля повторных слов» — отдельная, честно названная величина.
    # Её нельзя трактовать как зацикливание: у живой прозы она 0.4–0.55 всегда.
    lex = [s.get("lexical_dup_share") for s in samples
           if s.get("lexical_dup_share") is not None]
    providers: "defaultdict[str, int]" = defaultdict(int)
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
    scores = [float(s["rag_score_avg"]) for s in samples
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

