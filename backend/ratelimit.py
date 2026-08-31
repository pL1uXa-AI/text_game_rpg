# -*- coding: utf-8 -*-
"""
ratelimit.py — лёгкий rate-limit без внешних зависимостей (сессия 30, «Безопасность»).

Локальная однопользовательская игра без аутентификации: жёсткий лимит на действия
(типа slowapi) не нужен, но защита от «залипания» кнопки / скрипта / случайного
цикла в UI полезна. Это in-memory sliding-window лимитер: key → (окно, счётчик).

- Без внешних библиотек (проект не хочет новых зависимостей ради этого).
- Персистентности нет — рестарт сервера сбрасывает счётчики (приемлемо).
- Не блокирует легитимную игру: лимиты щедрые (действие — не чаще ~6/с, и т.п.).
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Callable, Optional

from fastapi import HTTPException, Request

# История вызовов: key → deque(timestamps). Слайд-окно: старые отметки выпадают.
_calls: dict[str, deque[float]] = defaultdict(deque)
# Дополнительная «корзина» токенов на короткое окно (burst-защита).
_burst: dict[str, list[float]] = defaultdict(list)

_WINDOW = 60          # окно, секунды
_BURST_WINDOW = 5.0   # короткое окно для «взрывов»
_DEFAULT_LIMIT = 120  # вызовов за _WINDOW
_DEFAULT_BURST = 30   # вызовов за _BURST_WINDOW

# Глобальный выключатель (для тестов/отладки): RATE_LIMIT_ENABLED=false отключает лимитер.
ENABLED = True


def _now() -> float:
    return time.monotonic()


def allow(key: str, limit: int = _DEFAULT_LIMIT, window: float = _WINDOW,
          burst: int = _DEFAULT_BURST, burst_window: float = _BURST_WINDOW) -> bool:
    """Проверяет лимит для ключа. True = разрешено, False = превышен лимит.
    Чистит старые отметки (амортизированно, окна фиксированные)."""
    if not ENABLED:
        return True
    now = _now()

    # слайд-окно
    q = _calls[key]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)

    # burst-корзина (короткое окно)
    b = _burst[key]
    while b and now - b[0] > burst_window:
        b.pop(0)
    if len(b) >= burst:
        return False
    b.append(now)
    return True


def client_key(request: Request, scope: str = "") -> str:
    """Ключ лимитера: IP клиента (локальная игра — обычно 127.0.0.1) + область."""
    ip = request.client.host if request.client else "?"
    return f"{scope}:{ip}"


def make_guard(scope: str, limit: int = _DEFAULT_LIMIT, window: float = _WINDOW,
               burst: int = _DEFAULT_BURST, burst_window: float = _BURST_WINDOW,
               detail: str = "Слишком много запросов. Подожди немного.") -> Callable[[Request], None]:
    """Фабрика dependency-функции FastAPI для rate-limit эндпоинта."""
    def guard(request: Request) -> None:
        key = client_key(request, scope)
        if not allow(key, limit=limit, window=window, burst=burst, burst_window=burst_window):
            raise HTTPException(429, detail)
    return guard


def reset() -> None:
    """Сброс счётчиков (для тестов)."""
    _calls.clear()
    _burst.clear()


def configure(enabled: bool | None = None) -> None:
    """Программно включить/выключить лимитер (тесты, отладка)."""
    global ENABLED
    if enabled is not None:
        ENABLED = bool(enabled)
    if not ENABLED:
        reset()
