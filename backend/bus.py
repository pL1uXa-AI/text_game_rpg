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

from .logsetup import get_logger

log = get_logger(__name__)

_MAX_QUEUE = 200                 # на подписчика: больше = клиент завис, режем старое
_loop: Optional[asyncio.AbstractEventLoop] = None
_subs: dict[int, set[asyncio.Queue]] = defaultdict(set)
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


def unsubscribe(world_id: int, q: asyncio.Queue) -> None:
    with contextlib.suppress(KeyError):
        _subs[int(world_id)].discard(q)
        if not _subs[int(world_id)]:
            del _subs[int(world_id)]


def publish(world_id: int, payload: dict) -> None:
    """Асинхронная публикация (вызываем из async-кода)."""
    global _sent, _dropped
    qset = _subs.get(int(world_id))
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
                log_once_id = f"bus-full-{world_id}"
                from .logsetup import log_once
                log_once(log, log_once_id, 30,
                         "bus: очередь подписчика мира %s переполнена — клиент отстанет "
                         "и догрузит по поллингу", world_id)


def publish_threadsafe(world_id: int, payload: dict) -> None:
    """Публикация из синхронного кода (хук записи события в БД).

    Вызов может прийти из любого потока (в т.ч. фонового цикла aiosqlite) — поэтому
    идём через call_soon_threadsafe, а не трогаем очереди напрямую.
    """
    if not _subs.get(int(world_id)):
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
