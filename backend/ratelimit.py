# -*- coding: utf-8 -*-
"""
ratelimit.py — лёгкий rate-limit без внешних зависимостей (сессия 30, «Безопасность»).

Локальная однопользовательская игра без аутентификации: жёсткий лимит на действия
(типа slowapi) не нужен, но защита от «залипания» кнопки / скрипта / случайного
цикла в UI полезна. Это in-memory sliding-window лимитер: key → (окно, счётчик).

- Без внешних библиотек (проект не хочет новых зависимостей ради этого).
- Персистентности нет — рестарт сервера сбрасывает счётчики (приемлемо).
- Не блокирует легитимную игру: лимиты щедрые (действие — не чаще ~6/с, и т.п.).

B2 (аудит 41): реестр «дорогих» (LLM/сеть/диск) эндпоинтов в `SCOPES` + `guard_for(scope)`;
чужому адресу в сети (запуск с `GAME_BIND=0.0.0.0`) лимиты урезаются в `REMOTE_FACTOR` раз,
а админка (правка провайдеров и ключей) закрывается до loopback — `require_local`.

D3 (аудит 41): словари счётчиков больше НЕ `defaultdict`. Запись создаётся только на
реальном вызове `allow()`, а ключи, к которым не обращались дольше `_STALE_AFTER`, выметаются
амортизированной чисткой `_prune()` (проверка — O(1) на каждый вызов). Иначе карта ключей
росла на каждое встреченное имя (scope × IP × порт эфемерных клиентов) и никогда не сжималась
— тот же класс дефекта, что чинили в `bus.py` (D10, аудит 38).
"""
from __future__ import annotations

import time
from collections import deque
from typing import Callable

from fastapi import HTTPException, Request

from .logsetup import get_logger

log = get_logger(__name__)

# История вызовов: key → deque(timestamps). Слайд-окно: старые отметки выпадают.
# D3 (аудит 41): обычный dict, а НЕ defaultdict(deque) — defaultdict создавал запись на
# ЛЮБОМ чтении (`_calls[key]` в allow), и ключ оставался навсегда: после B2 ключей стало
# 19 роутов × адрес клиента, и течь ускорилась. Читается через get(), запись — только в allow.
_calls: dict[str, deque[float]] = {}
# Дополнительная «корзина» токенов на короткое окно (burst-защита). Живёт ровно столько,
# сколько соответствующий ключ в _calls (удаляются вместе в _prune/reset).
_burst: dict[str, list[float]] = {}
# Когда ключ трогали последний раз — по нему и решается, что пора выметать.
_last: dict[str, float] = {}

_WINDOW = 60          # окно, секунды
_BURST_WINDOW = 5.0   # короткое окно для «взрывов»
_DEFAULT_LIMIT = 120  # вызовов за _WINDOW
_DEFAULT_BURST = 30   # вызовов за _BURST_WINDOW

# Порог «забытья» ключа и период амортизированной чистки (секунды). Ключ, к которому не
# обращались дольше _STALE_AFTER, гарантированно пуст по обоим окнам (они много короче),
# поэтому удаление не может «оздоровить» уже заблокированного клиента.
_STALE_AFTER = 300.0
_PRUNE_AFTER = 60.0
_prune_at = 0.0

# ─── B2 (аудит 41): реестр «дорогих» эндпоинтов ───────────────────────────────
# Раньше `make_guard` стоял на трёх роутах (action / action_stream / divine), а
# остальные точки, которые зовут LLM, облачные эмбеддинги, синтез голоса или
# скачивание моделей, не были ограничены НИЧЕМ. У игры нет авторизации, а
# `GAME_BIND=0.0.0.0` (запуск «для телефона») выставляет эти эндпоинты всей
# локальной сети — то есть любой сосед по Wi-Fi мог жечь платные токены провайдера.
# Здесь единый список scope'ов: лимиты ЩЕДРЫЕ (игрок упираться не должен), цель —
# скрипт/заломанная кнопка/чужой в сети, а не человек.
#
# Значения читаются В МОМЕНТ запроса (см. `guard_for`), поэтому тест может
# временно подрезать любой scope, а не стучаться в лимиты, зашитые в замыкание.
SCOPES: dict[str, dict[str, float]] = {
    "default":       {"limit": 120, "window": 60, "burst": 30, "burst_window": 5},
    # ход игрока — прежние лимиты (сессия 30), не ужали
    "action":        {"limit": 120, "window": 60, "burst": 30, "burst_window": 5},
    "action_stream": {"limit": 120, "window": 60, "burst": 30, "burst_window": 5},
    "divine":        {"limit": 20,  "window": 60, "burst": 6,  "burst_window": 5},
    # создание мира = 2–3 LLM-прохода + эмбеддинги лора
    "create_world":  {"limit": 12,  "window": 60, "burst": 4,  "burst_window": 10},
    "suggest":       {"limit": 30,  "window": 60, "burst": 8,  "burst_window": 5},
    "vision":        {"limit": 12,  "window": 60, "burst": 4,  "burst_window": 10},
    # смена провайдера = проверка доступности модели сетевым запросом
    "providers":     {"limit": 20,  "window": 60, "burst": 6,  "burst_window": 10},
    # RAG-поиск (эмбеддинг запроса + реранк)
    "memory_search": {"limit": 40,  "window": 60, "burst": 12, "burst_window": 5},
    "lore_search":   {"limit": 40,  "window": 60, "burst": 12, "burst_window": 5},
    # запись лора/карточки = индексация в Chroma (облачный эмбеддинг на каждую статью)
    "lore_write":    {"limit": 40,  "window": 60, "burst": 12, "burst_window": 5},
    "entity_write":  {"limit": 60,  "window": 60, "burst": 20, "burst_window": 5},
    # импорт дампа = полная переиндексация памяти в фоне
    "import_json":   {"limit": 6,   "window": 60, "burst": 3,  "burst_window": 30},
    # TTS: синтез фразы / скачка моделей (десятки МБ) / перезапуск синтеза
    "tts_test":      {"limit": 20,  "window": 60, "burst": 6,  "burst_window": 10},
    "tts_download":  {"limit": 4,   "window": 60, "burst": 2,  "burst_window": 30},
    "tts_retry":     {"limit": 30,  "window": 60, "burst": 10, "burst_window": 5},
    "admin":         {"limit": 30,  "window": 60, "burst": 10, "burst_window": 5},
}

# Чужой адрес в той же сети (запуск с не-loopback GAME_BIND) — лимиты жёстче в N раз:
# легитимный игрок сидит с 127.0.0.1, и умножение его не касается.
REMOTE_FACTOR = 4


# loopback: «это сам хост игры» (правило 5 — сервер обязан слушать только его)
_LOOPBACK_PREFIXES = ("127.",)
_LOOPBACK_NAMES = ("::1", "localhost")


def is_loopback(host: str | None) -> bool:
    """True для адресов самого хоста (127.0.0.x и IPv6 ::1)."""
    if not host:
        return False
    h = host.strip()
    if h in _LOOPBACK_NAMES:
        return True
    return any(h.startswith(p) for p in _LOOPBACK_PREFIXES)


def scope_limits(scope: str, host: str | None = None) -> dict[str, float]:
    """Лимиты scope'а для конкретного адреса (свой словарь — вызывающий волен менять).

    `host=None` — адрес неизвестен: лимиты своего хоста (не ужесточаем наугад).
    Пороги чужому адресу режутся в `REMOTE_FACTOR`, но никогда не до нуля (0 означало бы
    «эндпоинт запрещён», а не «лимит» — границы реестра проверяет тест).
    """
    base = dict(SCOPES.get(scope) or SCOPES["default"])
    if host is not None and not is_loopback(host):
        base["limit"] = max(1, int(base["limit"]) // REMOTE_FACTOR)
        base["burst"] = max(1, int(base["burst"]) // REMOTE_FACTOR)
    return base


def _mark(obj, **attrs):
    """Повесить на зависимость метку для НАБЛЮДЕНИЯ (B2).

    Тест сверяет «стоит ли на роуте лимитер» по реальным зависимостям приложения
    (инвариант 19: поведение, а не тексты исходников), а замыкание/функция иначе
    неотличимы от любой другой зависимости. Метки служебные, на логику не влияют.
    """
    for name, val in attrs.items():
        setattr(obj, name, val)
    return obj


def guard_for(scope: str) -> Callable[[Request], None]:
    """Зависимость-лимитер по имени scope'а из `SCOPES` (B2).

    В отличие от `make_guard`, лимиты НЕ зашиваются в замыкание, а читаются из
    реестра на каждый запрос: их можно поправить в одном месте (и подрезать в
    тесте), не трогая ни одного роутера.
    """
    def guard(request: Request) -> None:
        host = request.client.host if request.client else None
        lim = scope_limits(scope, host)
        key = client_key(request, scope)
        if not allow(key, limit=int(lim["limit"]), window=float(lim["window"]),
                     burst=int(lim["burst"]), burst_window=float(lim["burst_window"])):
            # A6 (аудит 41): в журнале обязан быть и ключ, и причина отказа.
            dropped = "окно" if (len(_calls.get(key) or ()) >= int(lim["limit"])) else "burst"
            log.warning("rate-limit: отказ 429 scope=%s ключ=%s (%s: лимит %s/%s c, "
                        "burst %s/%s c)", scope, key, dropped, lim["limit"], lim["window"],
                        lim["burst"], lim["burst_window"])
            raise HTTPException(429, "Слишком много запросов. Подожди немного.")
    # Метка «этот роут под лимитером scope» — для реестра-теста приложения.
    return _mark(guard, _rl_scope=scope,
                 _rl_limits=dict(SCOPES.get(scope) or SCOPES["default"]))


def require_local(request: Request, what: str = "админка") -> None:
    """B2: «только с localhost» — для endpoint'ов, которые меняют провайдеры и ключи.

    Rate-limit от чужого хоста в сети не спасает: админка может ПЕРЕПИСАТЬ
    `MAIN_BASE_URL` на адрес атакующего (перехват промптов и ключей). Поэтому
    доступ к настройке — loopback; явно наружу её выносят только
    `ADMIN_ALLOW_LAN=true` в `.env` (не через саму админку).
    """
    from .config import get_config   # локально: config → db → config, на импорте опасно
    if get_config().admin_allow_lan:
        return
    host = request.client.host if request.client else ""
    if not is_loopback(host):
        log.warning("rate-limit: отказ 403 %s — запрос не с localhost (ключ=%s)", what, host or "?")
        raise HTTPException(403, f"{what.capitalize()} доступна только с localhost "
                                f"(осознанно наружу — ADMIN_ALLOW_LAN=true в .env)")


# Та же метка, что у `guard_for`: «этот роут закрыт до localhost» (для теста реестра).
_mark(require_local, _rl_local=True)

# Глобальный выключатель (для тестов/отладки): RATE_LIMIT_ENABLED=false отключает лимитер.
ENABLED = True


def _now() -> float:
    return time.monotonic()


def _prune(now: float) -> None:
    """D3 (аудит 41): выметать ключи, к которым не обращались дольше _STALE_AFTER.

    Вызывается из `allow` не чаще раза в _PRUNE_AFTER секунд — «амортизированно»: цена
    чистки распределена по вызовам, а на горячем пути остаётся одно сравнение времени.
    Окна (60 с и 5 с) много короче порога, так что сбрасывается только то, что и так пусто.
    """
    global _prune_at
    if now < _prune_at:
        return
    _prune_at = now + _PRUNE_AFTER
    stale = [k for k, t in _last.items() if now - t > _STALE_AFTER]
    for key in stale:
        _calls.pop(key, None)
        _burst.pop(key, None)
        _last.pop(key, None)


def stats() -> dict[str, int]:
    """Наблюдаемость (D3): сколько ключей сейчас держим. Тест мерит по нему отсутствие течи."""
    return {"keys": len(_last), "windows": len(_calls), "bursts": len(_burst)}


def allow(key: str, limit: int = _DEFAULT_LIMIT, window: float = _WINDOW,
          burst: int = _DEFAULT_BURST, burst_window: float = _BURST_WINDOW) -> bool:
    """Проверяет лимит для ключа. True = разрешено, False = превышен лимит.
    Чистит старые отметки (амортизированно, окна фиксированные)."""
    if not ENABLED:
        return True
    now = _now()
    _prune(now)

    # слайд-окно
    q = _calls.get(key)
    if q is None:
        q = _calls[key] = deque()
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        _last[key] = now
        return False
    q.append(now)

    # burst-корзина (короткое окно)
    b = _burst.get(key)
    if b is None:
        b = _burst[key] = []
    while b and now - b[0] > burst_window:
        b.pop(0)
    if len(b) >= burst:
        _last[key] = now
        return False
    b.append(now)
    _last[key] = now
    return True


def client_key(request: Request, scope: str = "") -> str:
    """Ключ лимитера: IP клиента (локальная игра — обычно 127.0.0.1) + область."""
    ip = request.client.host if request.client else "?"
    return f"{scope}:{ip}"


def make_guard(scope: str, limit: int = _DEFAULT_LIMIT, window: float = _WINDOW,
               burst: int = _DEFAULT_BURST, burst_window: float = _BURST_WINDOW,
               detail: str = "Слишком много запросов. Подожди немного.") -> Callable[[Request], None]:
    """Фабрика dependency-функции FastAPI для rate-limit эндпоинта.

    B2 (аудит 41): для НОВЫХ роутов предпочтителен `guard_for(scope)` — лимиты берутся из
    реестра `SCOPES` (одно место правки + удалённое ужесточение). `make_guard` оставлен:
    им пользуются тесты «своего» лимита вне реестра, и он ничего не знает про адрес.
    """
    def guard(request: Request) -> None:
        key = client_key(request, scope)
        if not allow(key, limit=limit, window=window, burst=burst, burst_window=burst_window):
            # A6 (аудит 41, «по желанию» пункта): в журнале был только голый 429 — без
            # причины и без ключа, по которому сработал лимит. Теперь видно, что именно
            # и где ограничили (IP + scope + какой из двух лимитов), — диагностика
            # «почему у игрока перестали приниматься действия» без гадания.
            dropped = "окно" if (len(_calls.get(key) or ()) >= limit) else "burst"
            log.warning("rate-limit: отказ 429 scope=%s ключ=%s (%s: лимит %d/%s c, "
                        "burst %d/%s c)", scope, key, dropped, limit, window, burst,
                        burst_window)
            raise HTTPException(429, detail)
    return guard


__all__ = ["allow", "client_key", "configure", "guard_for", "is_loopback", "make_guard",
           "REMOTE_FACTOR", "require_local", "reset", "SCOPES", "scope_limits", "stats"]


def reset() -> None:
    """Сброс счётчиков (для тестов)."""
    global _prune_at
    _calls.clear()
    _burst.clear()
    _last.clear()
    _prune_at = 0.0


def configure(enabled: bool | None = None) -> None:
    """Программно включить/выключить лимитер (тесты, отладка)."""
    global ENABLED
    if enabled is not None:
        ENABLED = bool(enabled)
    if not ENABLED:
        reset()
