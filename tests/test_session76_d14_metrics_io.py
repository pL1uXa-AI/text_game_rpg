# -*- coding: utf-8 -*-
"""D14 (аудит 41): `metrics.record()` не делает дисковый I/O на горячем пути хода.

Было: `record()` вызывается СИНХРОННО из async-хода (`routers/core.py`), и каждая метрика
значила `mkdir` + `open(...,'a')` + запись строки в `data/metrics.jsonl` (плюс проверку
ротации) прямо в цикле событий — лишний I/O там, где игрок ждёт ответ.

Стало (`backend/metrics.py`, блок «Буфер журнала»): метрика ложится в память, а на диск
уходит ПАЧКОЙ — по объёму (`_FLUSH_BATCH`), по возрасту (`_FLUSH_AGE_S`, «разрядка» через
`call_later`) или по явному `flush()`; при остановке сервера буфер дочитывает `_lifespan`.
Сама запись выполняется в потоке (`run_in_executor`), цикл событий не блокируется.

Что сторожится: ФОРМАТ журнала не изменился (одна JSON-строка = одна метрика); ротация
жива; `METRICS_PERSIST=false` (conftest) по-прежнему не заводит буфер и не пишет в `data/`;
вне asyncio (скрипты) запись осталась немедленной; `open()` вызывается РЕДКО.
"""
from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from backend import app as app_mod
from backend import config as config_mod
from backend import metrics as mm


# ── инфраструктура ──────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolated():
    """Модульные глобалы metrics не протекают между тестами."""
    mm.reset_buffer()
    mm._samples.clear()
    yield
    mm.reset_buffer()
    mm._samples.clear()


@pytest.fixture
def journal(monkeypatch, tmp_path):
    """Включить персист в tmp-файл (конфиг наследуется от реального — методы не теряются)."""
    target = tmp_path / "metrics.jsonl"

    def _set(**kw):
        real = config_mod.get_config()

        class _C(type(real)):
            pass

        cfg = _C()
        vals = dict(getattr(real, "__dict__", {}) or {})
        vals.update(metrics_persist=True, metrics_file=str(target),
                    metrics_tail=500, metrics_max_bytes=0)
        vals.update(kw)
        for k, v in vals.items():
            setattr(cfg, k, v)
        monkeypatch.setattr(config_mod, "_cache", {"cfg": cfg})
        return target
    return _set


def _lines(target):
    return [ln for ln in target.read_text(encoding="utf-8").splitlines() if ln.strip()] \
        if target.exists() else []


def _writer(monkeypatch):
    """Счётчик реальных обращений к файлу (сколько было `_append_lines`, сколько строк в каждой)."""
    calls: list[int] = []
    orig = mm._append_lines

    def spy(p, items):
        calls.append(len(items))
        return orig(p, items)

    monkeypatch.setattr(mm, "_append_lines", spy)
    return calls


# ── главное: в цикле событий диска нет ──────────────────────────────────

def test_record_in_async_loop_touches_no_disk(journal):
    target = journal()

    async def main():
        mm.record(world_id=1, llm_ms=42.0, prompt_tokens=10)
        return mm.pending_count(), target.exists()

    pending, exists = asyncio.run(main())
    assert pending == 1, "запись должна остаться в буфере"
    assert not exists, "на горячем пути файла журнала быть не должно"
    rep = mm.as_json()                      # in-memory отчёт свежее диска
    assert rep["journal_pending"] == 1
    assert rep["last"][0]["llm_ms"] == 42.0


def test_batch_writes_once_per_pack(journal, monkeypatch):
    target = journal()
    calls = _writer(monkeypatch)

    async def main():
        for i in range(mm._FLUSH_BATCH + 5):
            mm.record(world_id=1, llm_ms=float(i))
        await asyncio.sleep(0.1)
        return mm.pending_count()

    assert asyncio.run(main()) == 5, "ушли полные пачки, остаток (5) дожидается"
    assert len(_lines(target)) == mm._FLUSH_BATCH
    assert len(calls) == 1, f"ожидался один заход на файл на пачку, а их {len(calls)}"
    assert calls[0] == mm._FLUSH_BATCH


def test_fifty_metrics_are_not_fifty_opens(journal, monkeypatch):
    """Регресс на суть D14: раньше `open(...,'a')` был на КАЖДУЮ метрику."""
    journal()
    calls = _writer(monkeypatch)

    async def main():
        for i in range(50):
            mm.record(world_id=1, llm_ms=float(i))
        await asyncio.sleep(0.1)
        return mm.pending_count()

    pending = asyncio.run(main())
    assert len(calls) <= 3, f"слишком много открытий файла: {len(calls)} (метрик 50)"
    assert pending + mm._FLUSH_BATCH * len(calls) >= 50, "часть метрик потерялась"


def test_age_discharges_buffer(journal, monkeypatch):
    """«Разрядка»: новых метрик нет — отложенный сброс сам доносит пачку до диска."""
    target = journal()
    monkeypatch.setattr(mm, "_FLUSH_AGE_S", 0.05)

    async def main():
        mm.record(world_id=1, llm_ms=7.0)
        await asyncio.sleep(0.5)
        return mm.pending_count()

    assert asyncio.run(main()) == 0
    assert [json.loads(x)["llm_ms"] for x in _lines(target)] == [7.0]


def test_sync_caller_still_writes_immediately(journal):
    """Вне asyncio (скрипт/pytest) — пишем сразу: «запустил скрипт → журнал на месте»."""
    target = journal()
    mm.record(world_id=3, llm_ms=1.5)
    assert [json.loads(x)["world_id"] for x in _lines(target)] == [3]


def test_flush_returns_count_and_is_idempotent(journal):
    target = journal()

    async def main():
        for i in range(4):
            mm.record(world_id=1, llm_ms=float(i))
        return mm.flush()

    assert asyncio.run(main()) == 4
    assert len(_lines(target)) == 4
    assert mm.flush() == 0, "пустой буфер ничего не пишет"
    assert mm.buffer_stats()["flushes"] == 1


def test_format_unchanged_one_json_line_per_metric(journal):
    target = journal()

    async def main():
        mm.record(world_id=9, llm_ms=1.0, provider="ллама", game_over=False)
        mm.record(world_id=9, llm_ms=2.0, cut_mid=True)
        return mm.flush()

    assert asyncio.run(main()) == 2
    d0, d1 = (json.loads(x) for x in _lines(target))
    assert d0["provider"] == "ллама" and d0["world_id"] == 9 and d0["game_over"] is False
    assert "ts" in d0 and d1["cut_mid"] is True


def test_persist_disabled_never_buffers(journal):
    """METRICS_PERSIST=false (conftest) — буфер не заводится, файл не появляется."""
    target = journal(metrics_persist=False)

    async def main():
        for i in range(mm._FLUSH_BATCH * 2):
            mm.record(world_id=1, llm_ms=float(i))
        await asyncio.sleep(0.1)
        return mm.pending_count()

    assert asyncio.run(main()) == 0
    assert not target.exists()
    assert len(mm._samples) == mm._FLUSH_BATCH * 2, "in-memory метрики на месте"


def test_read_journal_and_counters_after_buffer(journal):
    """`read_journal` и восстановление счётчиков (сессии 33/36) не сломались."""
    journal()

    async def main():
        for _ in range(3):
            mm.record(world_id=1, llm_ms=100.0, prompt_tokens=10, completion_tokens=5)
        return mm.flush()

    asyncio.run(main())
    assert len(mm.read_journal()) == 3
    mm._samples.clear()
    rep = mm.as_json()
    assert rep["restored_from_journal"] is True
    assert rep["windows"]["all"]["llm_ms_avg"] == 100.0


def test_rotation_still_applies(journal, tmp_path):
    journal(metrics_max_bytes=200, metrics_tail=5)

    async def main():
        for i in range(mm._FLUSH_BATCH * 3):
            mm.record(world_id=1, llm_ms=float(i))
        await asyncio.sleep(0.2)
        return mm.flush()
    asyncio.run(main())
    assert (tmp_path / "metrics.jsonl.1").exists(), "ротация не сработала"
    assert mm.read_journal(tail=3)


def test_unwritable_path_warns_and_keeps_turn_alive(journal, tmp_path, caplog):
    blocker = tmp_path / "blocker"
    blocker.write_text("не папка", encoding="utf-8")
    journal(metrics_file=str(blocker / "m.jsonl"))

    async def main():
        mm.record(world_id=1, llm_ms=1.0)
        return mm.flush()

    with caplog.at_level("WARNING"):
        assert asyncio.run(main()) == 1        # не бросила
    assert "не удалось дописать" in caplog.text
    assert mm.pending_count() == 0


def test_background_flush_failure_is_logged(journal, monkeypatch, caplog):
    """Правило 14: отказ фонового сброса виден в журнале, а не молчит."""
    journal()

    def boom(p, items):
        raise RuntimeError("диск умер")

    monkeypatch.setattr(mm, "_append_lines", boom)

    async def main():
        for i in range(mm._FLUSH_BATCH):
            mm.record(world_id=1, llm_ms=float(i))
        await asyncio.sleep(0.2)

    with caplog.at_level("WARNING"):
        asyncio.run(main())
    assert "фоновый сброс" in caplog.text


def test_pending_rows_survive_a_closed_loop(journal):
    """Буфер не «зависает» из-за Future из закрытого цикла: следующий цикл дописывает."""
    target = journal()

    async def round1():
        for i in range(3):
            mm.record(world_id=1, llm_ms=float(i))     # недобор пачки — ждёт
    asyncio.run(round1())
    assert not target.exists()

    async def round2():
        for i in range(mm._FLUSH_BATCH):
            mm.record(world_id=1, llm_ms=float(i))
        await asyncio.sleep(0.1)
    asyncio.run(round2())
    assert len(_lines(target)) >= mm._FLUSH_BATCH, "старые строки обязаны уехать с новой пачкой"


# ── интеграция: ход мира и остановка сервера ────────────────────────────

def test_turn_does_not_write_journal_synchronously(api_client, journal):
    from backend import narrator as narrator_mod
    client, holder = api_client
    holder["reply"] = "Ты стоишь в тишине."
    journal()

    wid = client.post("/api/worlds", json={
        "theme_id": narrator_mod.THEMES[0]["id"], "name": "D14"}).json()["world_id"]
    r = client.post(f"/api/worlds/{wid}/action", json={"text": "осмотреться"})
    assert r.status_code == 200, r.text
    assert mm.pending_count() >= 1, "метрика хода должна лечь в буфер"
    assert client.get("/api/metrics").json()["journal_pending"] >= 1, "отчёт враньём не мёртв"
    assert mm.flush() >= 1
    assert _lines(mm._file_path()), "сброс дописал метрику хода"


def test_shutdown_flushes_buffer():
    src = inspect.getsource(app_mod._lifespan)
    assert "metrics.flush()" in src
    assert src.index("metrics.flush()") > src.index("bg.shutdown_nowait"), \
        "буфер дочитывают ПОСЛЕ остановки фоновых задач — они тоже пишут метрики"


def test_hot_path_source_guards():
    src = inspect.getsource(mm.record)
    assert "_queue_persist(entry)" in src
    assert "_persist(entry)" not in src.replace("_queue_persist(entry)", ""), \
        "record не должен звать немедленную запись"
    assert "await" not in src, "record остаётся синхронной (её зовут из async-хода)"
    q = inspect.getsource(mm._queue_persist)
    assert "_pending_lock" in q and "asyncio.sleep" not in q
    assert "run_in_executor" in inspect.getsource(mm._schedule_flush), "диск — в потоке"
    assert "call_later" in q, "возрастная разрядка без отдельного таймера"


def test_conftest_still_disables_persist():
    """Страховка «тесты не пишут в data/»: env-выключатель на месте и работает."""
    import os
    assert os.environ.get("METRICS_PERSIST") == "false"
    assert mm._file_path() is None
