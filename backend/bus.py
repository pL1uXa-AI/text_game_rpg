# -*- coding: utf-8 -*-
"""bus.py — шина событий мира для живого чата (сессия 34, D3).

Раньше «живой мир» (фоновые системки: ⚔️ враг отступил, 🤖 Мастер подсказал, ⏰ таймер
истёк, ⚖️ искажение реальности, 💾 автосохранение) доходил до игрока поллингом
`GET /events?since=` раз в 15 секунд — то есть мир «оживал» с задержкой до четверти минуты,
а сам интервал ещё и долбил API/SQLite даже когда ничего не произошло.

Здесь — фан-аут по Server-Sent Events: каждое записанное событие мира мгновенно рассылается
всем открытым вкладкам этого мира. Поллинг остаётся ЗАПАСНЫМ путём (обрыв соединения,
прокси, старые браузеры) — см. frontend: EventSource + fallback на setInterval.

Как подключено без переписывания двадцати мест записи: db.py отдаёт крючок
`add_event_listener()` (чистое уведомление «записано событие», без какой-либо презентационной
логики — слои не нарушаются), а слушателем назначаем эту шину в app.py при старте.

Безопасность памяти: у каждого подписчика очередь ограничена (переполнение = самая старая
событие выбрасывается с записью в лог), мёртвые подписчики собираются сами.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections import defaultdict
from typing import Callable, Optional

from .logsetup import get_logger, log_once

log = get_logger(__name__)

_MAX_QUEUE = 200                 # на подписчика: больше = клиент завис, режем старое
_loop: Optional[asyncio.AbstractEventLoop] = None
# defaultdict(set) ОПАСЕН: чтение на отсутствующем ключе СОЗДАЁТ пустое множество
# (протечка ключей → растёт _subs и счётчик «миров» в stats()). Поэтому читается через
# _queues() (get, а не []), а пустое множество удаляется явно (см. unsubscribe/publish).
_subs: dict[int, set[asyncio.Queue]] = defaultdict(set)
# Счётчики ПРИБЛИЗИТЕЛЬНЫЕ (аудит 38, D10): publish() вызывается и из потока цикла БД
# (publish_threadsafe → call_soon_threadsafe), и из цикла приложения; инкременты идут без
# блокировки. Для наблюдаемости («есть ли потери рассылки») это допустимо, но читать их
# как точный журнал нельзя — и нигде они так и не читаются (только отдаются в /api/metrics).
_sent = 0
_dropped = 0


def attach_loop(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Запомнить цикл приложения (из startup) — чтобы синхронный код БД мог публиковать."""
    global _loop
    try:
        _loop = loop or asyncio.get_running_loop()
    except RuntimeError:
        _loop = None
        log.warning("bus: нет активного цикла событий — живые рассылки отключены")


def subscribe(world_id: int) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=_MAX_QUEUE)
    _subs[int(world_id)].add(q)
    return q


def _queues(world_id: int) -> set[asyncio.Queue] | None:
    """Очереди мира БЕЗ создания записи (defaultdict[...] создал бы пустое множество)."""
    return _subs.get(int(world_id))


def unsubscribe(world_id: int, q: asyncio.Queue) -> None:
    # D10 (аудит 38): три прежних дефекта в четырёх строках —
    #   (1) `_subs[int(world_id)]` на отсутствующем ключе СОЗДАВАЛ множество (протечка ключей);
    #   (2) `contextlib.suppress(KeyError)` был мёртв: defaultdict KeyError не бросает;
    #   (3) `del` при параллельном subscribe() того же мира мог выбросить ЧУЖУЮ очередь.
    # Теперь: читаем через get() (без создания записи) и удаляем ключ только если
    # множество опустело именно от нашего discard — между discard и проверкой вклиниться
    # чужому subscribe нельзя, т.к. весь publish/unsubscribe живёт в одном цикле событий.
    wid = int(world_id)
    qset = _subs.get(wid)
    if qset is None:
        return
    qset.discard(q)
    if not qset:
        del _subs[wid]


def publish(world_id: int, payload: dict) -> None:
    """Асинхронная публикация (вызываем из async-кода)."""
    global _sent, _dropped
    qset = _queues(world_id)
    if not qset:
        return
    for q in list(qset):
        try:
            q.put_nowait(payload)
            _sent += 1
        except asyncio.QueueFull:
            # медленный/зависший клиент: выкидываем самое старое, свежее дойдёт
            with contextlib.suppress(asyncio.QueueEmpty):
                q.get_nowait()
            _dropped += 1
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # D10 (аудит 38): импорт и log_once были ВНУТРИ обработчика на «горячем»
                # пути рассылки (import в except — лишняя работа в момент, когда клиент
                # и так отстал). Импорт — наверху модуля, ключ — инлайном.
                log_once(log, f"bus-full-{world_id}", 30,
                         "bus: очередь подписчика мира %s переполнена — клиент отстанет "
                         "и догрузит по поллингу", world_id)


def publish_threadsafe(world_id: int, payload: dict) -> None:
    """Публикация из синхронного кода (хук записи события в БД).

    Вызов может прийти из любого потока (в т.ч. фонового цикла aiosqlite) — поэтому
    идём через call_soon_threadsafe, а не трогаем очереди напрямую.
    """
    if not _queues(world_id):
        return
    loop = _loop
    if loop is None:
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        publish(world_id, payload)
        return
    with contextlib.suppress(RuntimeError):
        loop.call_soon_threadsafe(publish, int(world_id), payload)


def listener() -> Callable[[dict], None]:
    """Хук для db.add_event_listener(): публикует каждое записанное событие его мира."""
    def _on_event(row: dict) -> None:
        try:
            wid = int(row.get("world_id") or 0)
        except (TypeError, ValueError):
            return
        if not wid:
            return
        publish_threadsafe(wid, {"type": "event", "event": row})
    return _on_event


def sse_format(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def stats() -> dict:
    return {"subscribers": sum(len(v) for v in _subs.values()),
            "worlds": len(_subs), "sent": _sent, "dropped": _dropped}


def reset() -> None:
    """Полный сброс (тесты)."""
    global _loop, _sent, _dropped
    _subs.clear()
    _loop = None
    _sent = _dropped = 0
