# -*- coding: utf-8 -*-
"""Сессия 38 — продолжение: тесты на «слоистые» пункты аудита (A9, D7, D8, D9, D10).

Отдельный файл (а не хвост test_session38_bugfixes.py) — чтобы читались по блокам:
тут проверяются не баги игрока, а инварианты слоёв (атомарность группы записи,
гигиена фоновой очереди, семантика «постоянного эффекта», обработчик Chroma,
подписки шины). Каждый тест обязан был бы падать на «до».
"""
from __future__ import annotations

import asyncio
import inspect
import pathlib
import threading
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _plain_world(hp: int = 50) -> int:
    from backend import db
    return db.create_world(
        "тест", "custom", "фэнтези", "normal", "second", "ru", "",
        {"player": {"hp": hp, "max_hp": hp, "stats": {}, "inventory": []},
         "locations": {}, "npc": {}, "quests": {}, "flags": {}}, {})


# ══════════════════════ A9: атомарность группы записи ══════════════════════

def test_transaction_blocks_other_writers_until_group_exits():
    """Пока открыта группа, чужой поток обязан ЖДАТЬ, а не вписываться в чужой BEGIN.

    Дефект (аудит 38, A9): `transaction()` отпускал `_lock` сразу после `BEGIN IMMEDIATE`
    — а docstring утверждал обратное. Запись из другого потока в этом окне становилась
    частью ЧУЖОЙ транзакции: при её откате строка исчезала из БД, а уведомление живого
    чата выбрасывалось вместе с чужими событиями (при чужом COMMIT — наоборот, рассылалось
    с чужим контекстом). На старом коде этот тест падает (проверено: INTERLEAVED)."""
    from backend import db

    wid = _plain_world()
    order: list[str] = []

    def _outer() -> None:
        with db.transaction():
            db.add_event(wid, "system", "из группы")
            time.sleep(0.5)                         # синхронное тело открытой группы
            order.append("group:exit")

    def _other() -> None:
        time.sleep(0.2)                             # стартуем, пока группа открыта
        # сюда поток обязан заблокироваться на _lock (до фикса — вписывался в чужой BEGIN)
        db.add_event(wid, "system", "из чужого")
        order.append("other:wrote")

    t1 = threading.Thread(target=_outer)
    t2 = threading.Thread(target=_other)
    try:
        t1.start()
        t2.start()
        t1.join(30)
        t2.join(30)
        assert not (t1.is_alive() or t2.is_alive()), "поток завис (лок не отдаётся?)"
        assert order == ["group:exit", "other:wrote"], \
            f"запись вписалась в открытую группу: {order}"
        rows = [e["content"] for e in db.get_events(wid)]
        assert "из чужого" in rows, "чужая запись пропала вместе с откатом группы"
    finally:
        db.delete_world(wid)


def test_transaction_delivers_each_event_exactly_once():
    """Рассылка — ровно по одному уведомлению на событие группы (не ноль и не два).

    A9 лечится «локом на всё тело», и это же снимает двойную рассылку: pending-буфер
    сливается один раз на внешнем COMMIT."""
    from backend import db

    wid = _plain_world()
    seen: list[str] = []
    listener = lambda row: seen.append(row["content"])   # noqa: E731
    db.add_event_listener(listener)
    try:
        with db.transaction():
            db.add_event(wid, "system", "A")
            db.add_event(wid, "system", "B")
        db.add_event(wid, "system", "C")       # вне группы — доставляется сразу
        assert seen == ["A", "B", "C"], f"рассылка исказилась: {seen}"
    finally:
        with db._lock:
            if listener in db._event_listeners:
                db._event_listeners.remove(listener)
        db.delete_world(wid)


def test_no_await_inside_transaction_body():
    """Инвариант «тело db.transaction() синхронное» закреплено статически (A9).

    `_lock` удерживается на всё тело, поэтому `await` внутри блока = удержания лока
    через переключение корутины → другие задачи этого же цикла встают. Правило живёт
    в AGENT.md («СОГЛАШЕНИЯ ПО КОДУ»), тест не даёт ему разойтись с кодом."""
    bad = []
    for f in sorted((ROOT / "backend").rglob("*.py")):
        lines = f.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if "with db.transaction():" not in line:
                continue
            indent = len(line) - len(line.lstrip())
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= indent:
                    break
                if "await " in nxt and not nxt.strip().startswith("#"):
                    bad.append(f"{f.relative_to(ROOT)}:{j + 1}: {nxt.strip()[:70]}")
                j += 1
    assert not bad, "await внутри with db.transaction():\n" + "\n".join(bad)


# ══════════════════════ D7: очередь фоновых агентов ══════════════════════

def test_bg_reset_is_called_by_fixture():
    """D7: `bg.reset()` жил без единого вызова. Планировщик глобален, а цикл у каждого
    TestClient свой — без сброса воркеры/heap прошлого прогона доедают следующий."""
    src = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert "_bg.reset()" in src, "bg.reset() по-прежнему не вызывается нигде"


def test_bg_dead_knobs_removed():
    """D7: await_result / PRIO_SUMMARY / PRIO_SUGGEST удалены (потребителей не было)."""
    from backend import bg
    assert not hasattr(bg, "PRIO_SUMMARY"), "PRIO_SUMMARY вернулся: сводки ставятся не сюда"
    assert not hasattr(bg, "PRIO_SUGGEST"), "PRIO_SUGGEST вернулся: подсказки не в очереди"
    assert "await_result" not in inspect.signature(bg.submit).parameters, \
        "await_result вернулся без потребителя (фон обязан НЕ блокировать ответ)"
    # все оставшиеся приоритеты реально используются в ядре хода
    core = (ROOT / "backend" / "routers" / "core.py").read_text(encoding="utf-8")
    for name in ("PRIO_MEMORY", "PRIO_CARDS", "PRIO_JUDGE", "PRIO_ENEMY_AI",
                 "PRIO_VISION", "PRIO_MASTER", "PRIO_EVENT"):
        assert name in core, f"приоритет {name} объявлен, но ядром хода не ставится"


# ══════════════════════ D8: движок — мёртвые ветки ══════════════════════

def _min_setting() -> dict:
    return {"player": {"hp": 50, "max_hp": 50, "mp": 0, "max_mp": 0, "gold": 0, "level": 1,
                       "stats": {}, "inventory": []},
            "locations": {}, "npc": {}, "quests": {}, "flags": {}}


@pytest.mark.parametrize("raw", ["permanent", "∞", "бессрочно", "постоянно", "какая-то строка"])
def test_non_numeric_turns_is_permanent(raw):
    """D8: ЛЮБОЕ нечисловое turns = постоянный эффект (-1).

    Раньше это оформлялось тернарником `A if <список слов> else A` — обе ветки давали -1,
    разбор слов не делал НИЧЕГО и лишь вводил читателя в заблуждение (ruff RUF034)."""
    from backend import mechanics
    st = _min_setting()
    mechanics.apply_directives(st, {"effect_add": {"name": "Яд", "turns": raw, "damage": 1}})
    assert st["player"]["effects"]["Яд"]["turns"] == -1, raw


def test_numeric_turns_still_counts_down():
    from backend import mechanics
    st = _min_setting()
    mechanics.apply_directives(st, {"effect_add": {"name": "Яд", "turns": "3", "damage": 1}})
    assert st["player"]["effects"]["Яд"]["turns"] == 3
    mechanics.tick_effects(st)
    assert st["player"]["effects"]["Яд"]["turns"] == 2


def test_dead_ternary_not_restored():
    """Мёртвые тернарники D8 не должны вернуться (текст закрепляет намерение)."""
    src = (ROOT / "backend" / "mechanics.py").read_text(encoding="utf-8")
    assert "turns = -1 if isinstance(_turns_raw" not in src, "тернарник с двумя -1 вернулся"
    assert '"turns": turns if turns != -1 else -1' not in src


def test_normalize_setting_ranks_still_mutates_in_place():
    """D8.3: циклы переведены на .values() — list() обязан остаться (мутация того же dict,
    иначе RuntimeError: dictionary changed size during iteration)."""
    from backend import mechanics
    st = _min_setting()
    st["player"]["skills"] = {"меч": {"rank": "1"}, "щит": {"rank": "2"}}
    st["companions"] = {"волк": {"name": "Волк", "skills": {"укус": {"rank": "3"}}}}
    assert mechanics.normalize_setting_ranks(st) is True
    assert st["player"]["skills"]["меч"]["rank"] == "F"
    assert st["player"]["skills"]["щит"]["rank"] == "E"
    assert st["companions"]["волк"]["skills"]["укус"]["rank"] == "D"
    src = (ROOT / "backend" / "mechanics.py").read_text(encoding="utf-8")
    assert "for sk in list(skills.values())" in src, "list() убрали — будет мутация при итерации"


# ══════════════════════ D9: обработчик add() в Chroma ══════════════════════

def test_chroma_add_renames_collection_once(monkeypatch):
    """D9: раньше dimension-ветка имела свой `continue` без потолка attempt → при
    повторяющемся конфликте размерностей имя удлинялось каждый круг
    (`base_local_4096_4096_…`), плодя мусорные коллекции. Теперь — одна переименовка
    и наружу (с записью в лог)."""
    from backend import chroma_client as cc

    urls: list[str] = []

    async def _boom(method, url, payload=None):
        urls.append(url)
        raise RuntimeError("Error adding: dimension mismatch")

    async def _ensure(name: str) -> str:
        return "cid:" + name

    monkeypatch.setattr(cc, "_raw", _boom)
    monkeypatch.setattr(cc, "ensure_collection", _ensure)
    with pytest.raises(RuntimeError):
        asyncio.run(cc.add(["id1"], [[0.1, 0.2, 0.3]], [{"k": 1}], ["doc"]))
    assert len(urls) <= 2, f"переименование повторяется, а не ограничено одной попыткой: {urls}"


def test_chroma_add_retries_only_transient(monkeypatch):
    """D9: условие `"ECONNRESET" not in str(e) and "reset" not in str(e).lower()` —
    «reset» ловит и ECONNRESET, и «…reset…»; проверка регистронезависимая и не дублирует
    dimension-ветку."""
    from backend import chroma_client as cc
    calls = {"n": 0}

    async def _flaky(method, url, payload=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("Server disconnected (ECONNRESET)")
        return {}

    async def _ensure(name: str) -> str:
        return "cid"
    monkeypatch.setattr(cc, "_raw", _flaky)
    monkeypatch.setattr(cc, "ensure_collection", _ensure)
    asyncio.run(cc.add(["id1"], [[0.1, 0.2]], [{"k": 1}], ["doc"]))
    assert calls["n"] == 3


def test_chroma_add_reraises_unknown_error(monkeypatch):
    from backend import chroma_client as cc

    async def _bad(method, url, payload=None):
        raise RuntimeError("плохой запрос без размера и сброса")

    async def _ensure(name: str) -> str:
        return "cid"
    monkeypatch.setattr(cc, "_raw", _bad)
    monkeypatch.setattr(cc, "ensure_collection", _ensure)
    with pytest.raises(RuntimeError, match="плохой запрос"):
        asyncio.run(cc.add(["id1"], [[0.1, 0.2]], [{"k": 1}], ["doc"]))


# ══════════════════════ D10: шина событий ══════════════════════

def test_bus_does_not_leak_empty_subscriber_sets():
    """D10: `_subs` — defaultdict(set); чтение на отсутствующем ключе СОЗДАВАЛО пустое
    множество (протечка ключей и врущий счётчик «миров» в stats())."""
    from backend import bus
    bus.reset()
    q = bus.subscribe(4242)
    bus.publish(4242, {"type": "x"})
    assert 9999 not in bus._subs, "publish на пустом мире создал запись (defaultdict)"
    assert bus.stats()["worlds"] == 1
    bus.unsubscribe(4242, q)
    assert 4242 not in bus._subs, "последняя очередь не почистила ключ мира"
    bus.unsubscribe(4242, q)                       # повторный — без падения
    assert 4242 not in bus._subs, "unsubscribe на несуществующем мире создал запись"
    bus.publish(4242, {"type": "y"})              # мира нет — тихо, без создания записи
    assert 4242 not in bus._subs
    bus.reset()


def test_bus_unsubscribe_keeps_other_subscribers():
    from backend import bus
    bus.reset()
    q1, q2 = bus.subscribe(77), bus.subscribe(77)
    bus.unsubscribe(77, q1)
    assert bus._queues(77) == {q2}, "ушёл и чужой подписчик"
    bus.unsubscribe(77, q2)
    assert 77 not in bus._subs
    bus.reset()


def test_bus_publish_no_import_in_hot_path():
    """D10: импорт `log_once` был ВНУТРИ обработчика QueueFull (на «горячем» пути)."""
    src = (ROOT / "backend" / "bus.py").read_text(encoding="utf-8")
    assert "from .logsetup import" not in src.split("def publish")[1].split("def publish_threadsafe")[0], \
        "импорт вернулся внутрь publish()"


# ══════════════════════ D6: нигде нет «огонь-и-забыл» ══════════════════════

def test_bg_spawn_survives_garbage_collection():
    """bg.spawn держит СВОЮ ссылку (в bg) — задача доживает до результата, даже когда
    вызывающий её «забыл» (asyncio хранит задачи слабыми ссылками)."""
    from backend import bg

    async def scenario():
        done: list[int] = []

        async def work(i: int):
            await asyncio.sleep(0.02)
            done.append(i)

        for i in range(25):
            bg.spawn(work(i), name=f"probe{i}")
        import gc
        gc.collect()
        await asyncio.sleep(0.4)
        assert sorted(done) == list(range(25)), f"задачи собраны мусорщиком: {len(done)}/25"
    asyncio.run(scenario())


def test_bg_spawn_without_loop_closes_coroutine():
    """Вне цикла событий задача не заведётся — корутина обязана быть закрыта (иначе
    RuntimeWarning «coroutine was never awaited» на каждом синхронном вызове)."""
    from backend import bg

    async def work():
        return 1
    coro = work()
    assert bg.spawn(coro, name="no-loop") is None
    assert coro.cr_frame is None, "корутина не закрыта — будет предупреждение в логе"
