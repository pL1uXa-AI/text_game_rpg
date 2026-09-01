# -*- coding: utf-8 -*-
"""bg.py — очередь фоновых LLM-агентов (сессия 34, B3).

Фоновых проходов к ОДНОЙ модели набралось шесть: карточки сущностей (архивариус),
судья логики, автономный «мастер», боевой ИИ врагов, динамические события, видения,
 плюс индексация памяти и озвучка. Все они стартовали `create_task(...)` сразу после
ответа игроку и **битесь** в один слот локального llama.cpp (n_ctx 8192, одна очередь).
Практический эффект: следующий ход игрока вставал в очередь за 3–5 фоновыми запросами —
«игра думает» именно из-за этого, а не из-за самого ответа.

Что делает модуль (и не делает лишнего):
  * **семафор** `llm_bg_concurrency` — сколько фоновых LLM-проходов идёт одновременно;
  * **приоритет** — память/карточки (важно для качества будущих ходов) важнее событий/мастера;
  * **пауза на время хода игрока** (`llm_bg_yield_turn`): пока `_process_action` не отдал
    ответ, фоновые агенты не стартуют — игрок всегда первый в очереди к модели;
  * **потолок очереди** (`llm_bg_max_queue`): фоновые задачи не копятся бесконечно, если
    модель медленная — лишнее отбрасывается с записью в лог/метрики (это «дешёвый режим»,
    а не ошибка);
  * **наблюдаемость**: глубина очереди, время ожидания, число отказов — в `/api/metrics`.

Законы архитектуры не затрагивает: это диспетчеризация обращений к модели, решений за
мастера движок не принимает.

Использование:
    from .. import bg
    await bg.submit("cards", coro_factory, priority=bg.PRIO_MEMORY)   # из create_task
    with bg.player_turn():                                            # в ядре хода
        ...
"""
from __future__ import annotations

import asyncio
import contextlib
import heapq
import itertools
import time
from collections import defaultdict
from typing import Any, Awaitable, Callable, Optional

from .logsetup import get_logger, turn_context

log = get_logger(__name__)

# Приоритеты (меньше — идёт раньше). Память и карточки важнее для будущих ходов,
# «украшательства» мира — ниже хода игрока, который вообще вне очереди.
PRIO_MEMORY = 10     # индексация обменов/сводок в Chroma
PRIO_CARDS = 20      # архивариус карточек сущностей
PRIO_JUDGE = 30      # судья логики (правки мира ценнее атмосферных вставок)
PRIO_SUMMARY = 35    # свёртка истории в сводку
PRIO_ENEMY_AI = 45   # боевой ИИ врагов
PRIO_VISION = 55     # сны/видения
PRIO_MASTER = 60     # автономный «мастер»
PRIO_EVENT = 70      # динамическое событие мира
PRIO_SUGGEST = 80    # подсказки действий (самое дешевое — можно потерять)


class _Item:
    __slots__ = ("prio", "seq", "name", "factory", "future", "enqueued", "world_id", "agent")

    def __init__(self, prio: int, seq: int, name: str, factory: Callable[[], Awaitable[Any]],
                 world_id: int | None, agent: str):
        self.prio = prio
        self.seq = seq
        self.name = name
        self.factory = factory
        self.future: asyncio.Future | None = None
        self.enqueued = time.monotonic()
        self.world_id = world_id
        self.agent = agent

    def __lt__(self, other: "_Item") -> bool:
        # приоритет, затем FIFO (heapq не умеет сравнивать корутины)
        return (self.prio, self.seq) < (other.prio, other.seq)


class _Scheduler:
    """Очередь + воркеры. Живёт в раннере события (создаётся лениво под текущий цикл)."""

    def __init__(self) -> None:
        self.heap: list[_Item] = []
        self.counter = itertools.count()
        self.running = 0
        self.player_turns = 0          # глубина: сколько ходов игрока в работе
        self.unfulfilled = 0           # сколько задач отброшено из-за потолка очереди
        self.wait_ms_total = 0.0
        self.wait_n = 0
        self.errors: dict[str, int] = defaultdict(int)
        self.done: dict[str, int] = defaultdict(int)
        self._cond: asyncio.Condition | None = None
        self._loop_id: int | None = None
        self._workers = 0
        self._worker_tasks: set[asyncio.Task] = set()

    def _ensure(self) -> bool:
        """Лениво поднимает условие/воркеров под активный цикл; False — цикла нет (тесты без loop)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        if self._loop_id != id(loop) or self._cond is None:
            # новый цикл (например, тест) — пересоздаём примитивы, старых воркеров не плодим
            for t in list(self._worker_tasks):
                t.cancel()
            self._worker_tasks.clear()
            self._cond = asyncio.Condition()
            self._loop_id = id(loop)
            self._workers = 0
            self.heap.clear()
            self.running = 0
        while self._workers < self._target_workers():
            self._workers += 1
            # asyncio хранит задачи слабыми ссылками — держим свою ссылку, иначе воркера
            # может собрать мусор посреди игры.
            self._worker_tasks.add(loop.create_task(self._worker(self._workers)))
        return True

    def _target_workers(self) -> int:
        try:
            from .config import get_config
            n = int(getattr(get_config(), "llm_bg_concurrency", 2) or 2)
        except Exception:
            n = 2
        return max(1, min(4, n))

    async def _worker(self, idx: int) -> None:
        assert self._cond is not None
        while True:
            async with self._cond:
                # ждём, пока есть работа ИЛИ пока ход игрока не отпустит модель
                while True:
                    if not self.heap:
                        await self._cond.wait()
                        continue
                    if self.player_turns > 0 and self._yield_turn():
                        await self._cond.wait()
                        continue
                    break
                item = heapq.heappop(self.heap)
                self.running += 1
            ok = True
            try:
                wait = (time.monotonic() - item.enqueued) * 1000
                self.wait_ms_total += wait
                self.wait_n += 1
                if item.world_id is not None:
                    with turn_context(world_id=item.world_id, agent=item.agent):
                        res = await item.factory()
                else:
                    res = await item.factory()
                if item.future and not item.future.done():
                    item.future.set_result(res)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                ok = False
                self.errors[item.name] += 1
                log.warning("фоновая задача %r упала: %s", item.name, e, exc_info=True)
                if item.future and not item.future.done():
                    item.future.set_exception(e)
            finally:
                self.running -= 1
                self.done[item.name if ok else f"{item.name}!"] += 1
                async with self._cond:
                    self._cond.notify()

    @staticmethod
    def _yield_turn() -> bool:
        try:
            from .config import get_config
            return bool(getattr(get_config(), "llm_bg_yield_turn", True))
        except Exception:
            return True

    async def submit(self, name: str, factory, *, priority: int = PRIO_EVENT,
                     world_id: int | None = None, agent: str = "",
                     await_result: bool = False) -> Any:
        factory = _as_factory(factory)
        if not self._ensure():
            # нет цикла событий — выполняем напрямую (скрипты/синхронные тесты)
            return await factory()
        assert self._cond is not None
        cap = self._max_queue()
        if len(self.heap) >= cap:
            self.unfulfilled += 1
            log.warning("очередь фоновых задач переполнена (%d) — задача %r отброшена", cap, name)
            with contextlib.suppress(Exception):
                await _close_coro(factory)
            return None
        item = _Item(priority, next(self.counter), name, factory, world_id, agent or name)
        item.future = asyncio.get_running_loop().create_future() if await_result else None
        async with self._cond:
            heapq.heappush(self.heap, item)
            self._cond.notify()
        if await_result and item.future is not None:
            return await item.future
        return None

    @staticmethod
    def _max_queue() -> int:
        try:
            from .config import get_config
            return max(1, int(getattr(get_config(), "llm_bg_max_queue", 32) or 32))
        except Exception:
            return 32

    @contextlib.contextmanager
    def player_turn_cm(self):
        """Пока внутри — фоновые агенты не стартуют (игрок первый в очереди к модели)."""
        self.player_turns += 1
        try:
            yield
        finally:
            self.player_turns -= 1
            self._wake_all()

    def _wake_all(self) -> None:
        """Разбудить воркеров после окончания хода игрока (из синхронного finally)."""
        cond = self._cond
        if cond is None:
            return

        async def _wake() -> None:
            async with cond:
                cond.notify_all()

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        t = loop.create_task(_wake())
        t.add_done_callback(self._worker_tasks.discard)

    def stats(self) -> dict:
        avg = round(self.wait_ms_total / self.wait_n) if self.wait_n else 0
        return {"queued": len(self.heap), "running": self.running,
                "worker_target": self._target_workers(),
                "player_turns_active": self.player_turns,
                "avg_wait_ms": avg, "dropped": self.unfulfilled,
                "done": dict(self.done), "errors": dict(self.errors)}


_sched = _Scheduler()

# ── Барьер перегенерации (сессия 36, п.3A) ────────────────────────────
# Пока для мира выполняется ↻, его setting ОТКАТЫВАЕТСЯ к снапшоту и пересчитывается
# заново. Фоновые агенты (судья/мастер/боевой ИИ/события/видения/архивариус карточек),
# начатые по состоянию ДО отката, в это окно писать в setting не должны — иначе их
# директивы либо потеряются (их сотрёт откат), либо применятся дважды (откат + новый
# ответ). Живёт здесь, а не в routers/core: про этот барьер должен помнить и слой
# памяти (memory.py), а импорт core создал бы цикл импортов.
_regen: set[int] = set()


def is_regenerating(world_id: int) -> bool:
    """Идёт ли сейчас перегенерация мира (фону писать в setting нельзя)."""
    return int(world_id) in _regen


@contextlib.contextmanager
def regen_block(world_id: int):
    """Занять барьер перегенерации. yield False, если мир уже перегенерируется
    (двойной клик/ретрай) — вызывающий обязан выйти, ничего не трогая."""
    wid = int(world_id)
    if wid in _regen:
        yield False
        return
    _regen.add(wid)
    try:
        yield True
    finally:
        _regen.discard(wid)


def _as_factory(op):
    """Приводит аргумент к фабрике корутины.

    Терпимость осознанная: существующие места вызова пишут `create_task(coro)` и передают
    уже готовую корутину, а не лямбду. Корутина, созданная заранее, хранится как есть и
    будет запущена воркером (создание корутины не выполняет код, так что отложенный старт
    корректен).
    """
    if asyncio.iscoroutine(op):
        return _CoroutineAdapter(op)
    return op


class _CoroutineAdapter:
    """Обёртка над готовой корутиной: вызывается один раз (иначе «never awaited»/реюз)."""
    __slots__ = ("_coro",)

    def __init__(self, coro) -> None:
        self._coro = coro

    def __call__(self):
        coro, self._coro = self._coro, None
        if coro is None:
            raise RuntimeError("фоновая корутина уже была выполнена")
        return coro


async def _close_coro(factory) -> None:
    coro = getattr(factory, "_coro", None) if isinstance(factory, _CoroutineAdapter) else None
    if coro is not None:
        coro.close()


async def submit(name: str, factory, *, priority: int = PRIO_EVENT,
                 world_id: int | None = None, agent: str = "", await_result: bool = False) -> Any:
    """Поставить фоновый LLM-проход в очередь (см. модуль). await_result=True — ждать результат.

    `factory` — либо функция без аргументов, возвращающая корутину (`lambda: narrator.foo(...)`),
    либо уже готовая корутина (для совместимости со старым стилем `create_task(coro)`).
    """
    return await _sched.submit(name, factory, priority=priority, world_id=world_id,
                               agent=agent, await_result=await_result)


@contextlib.contextmanager
def player_turn() -> Any:
    """Синхронный контекст-менеджер: обозначает «идёт ход игрока» (фон ждёт).

    Работает и в async-коде: `with bg.player_turn(): ...await...` — await внутри блока
    не переключает поток, счётчик корректен.
    """
    with _sched.player_turn_cm():
        yield


def stats() -> dict:
    return _sched.stats()


def shutdown_nowait() -> None:
    """Остановить воркеров фоновой очереди (штатное завершение сервера, сессия 36, п.30).

    Незавершённые задачи отменяются: после закрытия БД/HTTP-клиентов они всё равно
    упали бы, а так завершаются сразу и без «Task was destroyed but it is pending».
    """
    sched = _sched
    for t in list(sched._worker_tasks):
        t.cancel()
    sched._worker_tasks.clear()
    sched._workers = 0
    sched.heap.clear()


def reset() -> None:
    """Полностью сбросить планировщик (тесты: изолировать очередь и воркеров)."""
    global _sched
    _sched = _Scheduler()
