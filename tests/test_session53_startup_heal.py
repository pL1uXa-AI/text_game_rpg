# -*- coding: utf-8 -*-
"""Аудит 41, A13 — стартовое само-исцеление больше НЕ живёт на импорте модуля.

Было: `import backend.app` сидил рассказчиков, бэкапил БД, чистил сирот и обходил ВСЕ миры
(ремимена карточек, флаги, игрок-как-NPC, ремсинхронизация карт). Цена: на занятой базе
второй экземпляр сервера ловил `database is locked` с полным таймаутом `db._run` на каждой
группе (замерено: 12.6 с импорта против 0.3 с на свободной базе), а pytest/CI платили за
весь проход при каждом запуске. Стало: весь проход — функция `app.self_heal()`, которую
`_lifespan` ставит фоновой задачей (`bg.spawn`) после поднятия сервера, с одной строкой
журнала.

Инвариант 19: проверяется ПОВЕДЕНИЕ (реальный импорт в подпроцессе, реальный вызов, реальный
журнал), а не подстроки в `app.py`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Скрипт для подпроцесса: чистый импорт → потом явный self_heal().
_IMPORT_SCRIPT = """
import json, os, sqlite3, sys
sys.path.insert(0, {root!r})
DB = os.environ["DB_PATH"]


def narrators():
    # файла мог создать config/admin_settings (своя таблица, данных не пишет) —
    # важно не «есть ли файл», а «посеяны ли рассказчики»
    if not os.path.exists(DB):
        return 0
    con = sqlite3.connect(DB)
    try:
        return con.execute("SELECT COUNT(*) FROM narrators").fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        con.close()


import backend.app as A                                # вот ЭТО раньше сидило и лечило
after_import = narrators()
r = A.self_heal()                                       # а теперь это делает старт сервера
print(json.dumps({{"after_import": after_import, "heal": r, "after_heal": narrators()}}))
"""


@pytest.fixture
def probe_env(tmp_path) -> dict:
    """Окружение дочернего процесса: СВОЯ пустая база, без озвучки/бэкапов/сети.

    Наследуется от `os.environ` целиком: выкинуть из него SYSTEMROOT и пр. — получить
    «не удаётся инициализировать поставщика услуг» (Winsock) вместо теста.
    """
    env = dict(os.environ)
    env.update({
        "PYTHONUTF8": "1",
        "DB_PATH": str(tmp_path / "startup.db"),
        "LOG_FILE": str(tmp_path / "startup.log"),
        "BACKUP_DB_ON_START": "false",
        "TTS_ENABLED": "false",
        "EMBEDDING_PROVIDER": "none",
        "RERANK_PROVIDER": "none",
        "METRICS_PERSIST": "false",
        "DETECT_MODEL_CONTEXT": "false",
    })
    return env


def _run_probe(tmp_path, env: dict) -> dict:
    script = tmp_path / "probe_import.py"
    script.write_text(_IMPORT_SCRIPT.format(root=str(ROOT)), encoding="utf-8")
    r = subprocess.run([sys.executable, "-X", "utf8", str(script)], capture_output=True,
                       text=True, encoding="utf-8", cwd=str(ROOT), env=env, timeout=180)
    assert r.returncode == 0, f"подпроцесс упал: {r.stderr[-1500:]}"
    line = [ln for ln in r.stdout.splitlines() if ln.startswith("{")][-1]
    return json.loads(line)


def test_import_backend_app_does_not_write_preset_data(tmp_path, probe_env):
    """A13 (главное): `import backend.app` не сидит и не лечит данные.

    На свежей базе после импорта таблица рассказчиков пустая (ни одной строки), а
    первый же осмысленный вызов — явный `self_heal()`, и только он сеет пресеты.
    """
    out = _run_probe(tmp_path, probe_env)
    assert out["after_import"] == 0, (
        "import backend.app снова сеет рассказчиков: само-исцеление вернулось на импорт")
    assert out["after_heal"] >= 7, "self_heal() обязан сидеть пресеты рассказчиков"
    assert out["heal"]["worlds"] == 0, "на пустой базе нечего исцелять в мирах"


def test_self_heal_is_callable_and_reports_counters(api_client):
    """`self_heal()` — чистая функция прохода: возвращает счётчики, идемпотентна.

    Второй проход на тех же данных не находит ЧЕГО править (единственное исключение —
    счётчик миров: их просто считаем).
    """
    from backend import app as app_mod
    r1 = app_mod.self_heal()
    for key in ("worlds", "orphans", "ranks", "cards", "flags", "npc"):
        assert key in r1, f"счётчик {key} пропал из отчёта само-исцеления"
        assert isinstance(r1[key], int) and r1[key] >= 0
    r2 = app_mod.self_heal()
    assert r2["cards"] == 0 and r2["flags"] == 0 and r2["npc"] == 0 and r2["orphans"] == 0, \
        f"само-исцеление не идемпотентно: {r2}"


def test_self_heal_fixes_real_defects(api_client):
    """Перенесённый проход чинит то же, что чинил: цифровые ранги, игрока в списке NPC
    и сирот в БД — по всем мирам разом."""
    from backend import app as app_mod, db
    client, _ = api_client
    wid = client.post("/api/worlds", json={"theme_id": "custom", "name": "А13",
                                          "custom_plot": "Ты идёшь по тропе."}).json()["world_id"]
    st = json.loads(db.get_world(wid)["setting"])
    st["player"]["skills"] = {"меч": {"rank": "1"}}
    st["npc"] = {"player": {"name": "Игрок", "mood": "спокоен"}}
    db.update_world(wid, setting=st)
    db.upsert_entity(wid, "location", "trail", name="trail")   # машинное, но имени мир не знает
    orphan = wid + 9911
    db.upsert_entity(orphan, "npc", "ghost", name="Призрак", meta={})

    r = app_mod.self_heal()
    assert r["worlds"] >= 1
    s2 = json.loads(db.get_world(wid)["setting"])
    assert s2["player"]["skills"]["меч"]["rank"] == "F", "цифровой ранг не вылечен"
    assert "player" not in (s2.get("npc") or {}), "игрок остался в персонажах окружения"
    assert not db.list_entities(orphan), "сирота не вычищен"
    names = {(e["kind"], e["entity_key"]): e["name"] for e in db.list_entities(wid)}
    assert names.get(("location", "trail")) == "trail", "имени в состоянии нет — выдумывать нечем"


def test_self_heal_runs_at_server_startup(monkeypatch):
    """lifespan обязан запускать проход (фоновой задачей), а не терять его."""
    from backend import app as app_mod

    calls: list[int] = []
    monkeypatch.setattr(app_mod, "self_heal",
                        lambda: (calls.append(1), {"worlds": 3})[1])

    async def _scenario():
        cm = app_mod._lifespan(app_mod.app)
        await cm.__aenter__()
        try:
            for _ in range(100):                 # ждём фонового старта, но не вечно
                if calls:
                    break
                await asyncio.sleep(0.02)
        finally:
            await cm.__aexit__(None, None, None)
        return len(calls)

    n = asyncio.run(_scenario())
    assert n == 1, "само-исцеление не запускается стартом приложения"


def test_startup_heal_logs_one_line(api_client, caplog):
    """Требование аудита: одна строка лога «само-исцеление: N миров за X мс»."""
    from backend import app as app_mod

    async def _scenario():
        with caplog.at_level(logging.INFO, logger="textgame"):
            await app_mod._startup_heal()

    asyncio.run(_scenario())
    lines = [rec.getMessage() for rec in caplog.records if "само-исцеление:" in rec.getMessage()]
    assert len(lines) == 1, f"нет единственной строки отчёта (было: {lines})"
    msg = lines[0]
    assert "миров" in msg and "мс" in msg, f"отчёт неполный: {msg}"


def test_startup_heal_failure_is_logged_not_swallowed(api_client, caplog, monkeypatch):
    """Правило 14: упавшее само-исцеление видно в журнале (ход игры при этом не роняется)."""
    from backend import app as app_mod

    def _boom():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(app_mod, "self_heal", _boom)

    async def _scenario():
        with caplog.at_level(logging.WARNING, logger="textgame"):
            await app_mod._startup_heal()       # не бросает

    asyncio.run(_scenario())
    assert any("само-исцеление при старте не удалось" in rec.getMessage()
               for rec in caplog.records), "отказ стартового прохода ушёл в тишину"


def test_pending_vectors_flushed_even_on_failure(api_client, monkeypatch):
    """Ключи удалённых векторов не должны переживать неудачу прохода (иначе «память
    помнит удалённое» до следующего перезапуска, правило 14)."""
    from backend import app as app_mod, chroma_client as cc, memory

    deleted: list[list[str]] = []

    async def _capture(ids):
        deleted.append(list(ids))

    def _boom():
        memory.PENDING_VECTOR_KEYS.append("ent_1_npc_player")
        raise RuntimeError("database is locked")

    monkeypatch.setattr(cc, "delete_by_ids", _capture)
    monkeypatch.setattr(app_mod, "self_heal", _boom)
    memory.PENDING_VECTOR_KEYS.clear()

    asyncio.run(app_mod._startup_heal())
    assert deleted and deleted[0] == ["ent_1_npc_player"], "векторы не убраны после сбоя"
    assert not memory.PENDING_VECTOR_KEYS, "очередь pending не очищена"


def _wipe_narrators() -> None:
    from backend import db
    for i in [n["id"] for n in db.list_narrators()]:
        db.delete_narrator(i)
    assert not db.list_narrators()


def test_narrators_endpoint_seeds_when_table_empty(api_client):
    """Боковая дверь закрыта: GET /api/narrators на ПУСТОЙ таблице всё равно отдаёт пресеты
    (раньше это гарантировал импорт app.py; теперь — сам роут, пока фоновый старт не дошёл)."""
    client, _ = api_client
    _wipe_narrators()
    try:
        r = client.get("/api/narrators")
        assert r.status_code == 200
        assert len(r.json()) >= 7, "рассказчики не досеяны при первом обращении"
    finally:
        from backend import app as app_mod
        app_mod.self_heal()                     # возвращаем базе нормальное состояние


def test_create_world_without_startup_seed_still_gets_narrator(api_client):
    """Гонка стартового фона закрыта: мир, созданный ДО прихода `self_heal()`, обязан
    получить рассказчика по умолчанию, а не остаться без персоны навсегда."""
    from backend import app as app_mod, db
    client, _ = api_client
    _wipe_narrators()
    try:
        from backend import narrator as nr
        wid = client.post("/api/worlds", json={
            "theme_id": nr.THEMES[0]["id"], "name": "Без сида"}).json()["world_id"]
        assert db.get_world(wid)["narrator_id"], "мир создан без рассказчика"
        assert len(db.list_narrators()) >= 7, "сид не нагнан на создании мира"
    finally:
        app_mod.self_heal()
