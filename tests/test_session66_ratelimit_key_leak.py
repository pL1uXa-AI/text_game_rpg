# -*- coding: utf-8 -*-
"""Сессия 66 — D3 (аудит 41): rate-limit не течёт по ключам.

Было (`backend/ratelimit.py`): `_calls`/`_burst` — `defaultdict(deque)` / `defaultdict(list)`.
Такое чтение на НЕизвестном ключе СОЗДАЁТ пустую запись (`_calls[key]` в `allow`), а чистки
не было вообще: карта ключей (`scope:IP`) росла на каждое встреченное имя и никогда не
сжималась. После B2 (аудит 41) scope'ов стало 19, а ключ — scope × адрес клиента с ЭФЕМЕРНЫМ
портом, то есть течь ускорилась. Тот же класс дефекта чинили в `bus.py` (D10, аудит 38):
там вылечили «чтение не создаёт», но амортизированной чистки старых ключей нет ни там, ни тут.

Стало: обычные словари + `_last` (когда ключ трогали), запись создаётся только в `allow()`,
мусор выметает `_prune()` (не чаще раза в `_PRUNE_AFTER` секунд, порог «забытья»
`_STALE_AFTER` — много длиннее обоих окон, поэтому лимиты не «оздоравливаются» чисткой).

Каждый тест обязан был бы падать до фикса.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from backend import ratelimit as rl


class _Clock:
    """Фейшие monotonic-часы: `t` можно двигать вручную (тесты чистки не спят)."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock(monkeypatch):
    """Лимитер включён, счётчики пусты, время под контролем теста."""
    rl.reset()
    mon = _Clock()
    monkeypatch.setattr(rl, "ENABLED", True)
    monkeypatch.setattr(rl, "_now", mon)
    yield mon
    rl.configure(enabled=False)   # как в conftest: API-тесты не должны ловить 429
    rl.reset()


def test_reading_creates_nothing(clock):
    """Чтение незнакомых ключей не плодит записей (главная засада `defaultdict`)."""
    # именно «чтение создаёт запись» и есть дефект: у обычной карты чужой ключ — KeyError
    with pytest.raises(KeyError):
        rl._calls["нет-такого"]
    with pytest.raises(KeyError):
        rl._burst["нет-такого"]
    assert rl.stats() == {"keys": 0, "windows": 0, "bursts": 0}
    rl.make_guard("scope-без-обращения", limit=5, window=60, burst=5, burst_window=5)
    assert rl.stats() == {"keys": 0, "windows": 0, "bursts": 0}, "фабрика лимитера не пишет счётчики"


def test_allow_creates_exactly_one_key(clock):
    """Запись появляется ровно одна на ключ и синхронно во всех трёх словарях."""
    assert rl.allow("x:1", limit=3, window=60, burst=3, burst_window=5)
    s = rl.stats()
    assert s["keys"] == 1 and s["windows"] == 1 and s["bursts"] == 1
    assert rl.allow("x:1", limit=3, window=60, burst=3, burst_window=5)
    assert rl.stats()["keys"] == 1, "повторный вызов того же ключа не плодит запись"


def test_stale_keys_are_pruned(clock):
    """Ключи, к которым не обращались дольше _STALE_AFTER, выметаются (главная претензия D3)."""
    for i in range(300):
        rl.allow(f"divine:10.0.0.{i}", limit=20, window=60, burst=6, burst_window=5)
    assert rl.stats()["keys"] == 300
    clock.t += rl._STALE_AFTER + 1
    rl.allow("action:127.0.0.1", limit=120, window=60, burst=30, burst_window=5)
    assert rl.stats() == {"keys": 1, "windows": 1, "bursts": 1}, "утекшие ключи выметены"
    assert set(rl._last) == {"action:127.0.0.1"}


def test_prune_is_amortized_not_per_call(clock):
    """Чистка амортизированная: между запусками проходит не меньше _PRUNE_AFTER секунд."""
    rl.allow("old:1", limit=5, window=60, burst=5, burst_window=5)
    first = rl._prune_at
    clock.t += rl._STALE_AFTER + 1
    rl.allow("new:1", limit=5, window=60, burst=5, burst_window=5)   # первый запуск чистки
    assert "old:1" not in rl._last
    assert rl._prune_at > first, "следующий запуск отложен на _PRUNE_AFTER"
    # ключ умер бы и раньше, но обход делается НЕ на каждый вызов:
    rl.allow("old:1", limit=5, window=60, burst=5, burst_window=5)
    clock.t += 1.0
    before = rl.stats()["keys"]
    for i in range(50):
        rl.allow(f"fresh:{i}", limit=5, window=60, burst=5, burst_window=5)
    assert rl.stats()["keys"] >= before, "внутри окна чистка не повторяется"


def test_fresh_keys_and_their_limits_survive_pruning(clock):
    """Живые ключи чистка не трогает: счётчик в окне не обнуляется задаром."""
    # широкие окна, чтобы к моменту чистки отметки ещё жили внутри лимита
    for _ in range(5):
        assert rl.allow("k:1", limit=5, window=600, burst=9, burst_window=300)
    clock.t += rl._PRUNE_AFTER + 1        # пришло время прохода чистки
    rl.allow("other:1", limit=5, window=600, burst=9, burst_window=300)
    assert "k:1" in rl._last, "ключ, живущий в пределах _STALE_AFTER, обязан остаться"
    assert not rl.allow("k:1", limit=5, window=600, burst=9, burst_window=300), \
        "5-й вызов того же ключа всё так же блокирован"


def test_guard_refusal_creates_only_its_own_key(clock, monkeypatch):
    """429 от guard_for: в картах ровно один ключ, и он принадлежит реальному клиенту."""
    from unittest.mock import MagicMock

    monkeypatch.setitem(rl.SCOPES, "divine", {"limit": 2, "window": 60, "burst": 99,
                                             "burst_window": 5})
    guard = rl.guard_for("divine")
    req = MagicMock()
    req.client.host = "127.0.0.1"
    guard(req)
    guard(req)
    with pytest.raises(HTTPException) as ei:
        guard(req)
    assert ei.value.status_code == 429
    assert rl.stats()["keys"] == 1
    assert list(rl._last) == ["divine:127.0.0.1"]


def test_reset_clears_everything(clock):
    """reset() убирает и карту «последнего обращения» (иначе prune жил бы мусором)."""
    rl.allow("z:1", limit=5, window=60, burst=5, burst_window=5)
    rl.reset()
    assert rl.stats() == {"keys": 0, "windows": 0, "bursts": 0}
    assert rl._prune_at == 0.0
