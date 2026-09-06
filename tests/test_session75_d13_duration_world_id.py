# -*- coding: utf-8 -*-
"""D13 (аудит 41): строка длительности API-запроса обязана нести `world_id`.

Было: `_log_request_context` (backend/app.py) сбрасывал контекст-переменные
`_ctx_rid`/`_ctx_world` в `finally`, а строка «GET /api/worlds/3/action → 200 за 12 мс»
писалась НИЖЕ, уже после сброса. `request_id` и так передавался явно через
`extra={"fields": ...}`, а мир брался только из контекста — значит в единственной
строке «сколько висел запрос» не было ни мира, ни хода: в `data/logs/game.log` её
нельзя было связать с тем самым миром (правило 14 — диагностика не теряется).

Стало: мир передаётся ЯВНЫМ полем `world_id` в `extra["fields"]`. Логику middleware
(расстановка/снятие контекста, уровень, фильтр «не шумим на статике») не перестраивали
— добавлено ровно одно поле.
"""
from __future__ import annotations

import inspect
import json
import logging

import pytest

from backend import app as app_mod
from backend import logsetup
from backend import narrator as narrator_mod


def _mk_world(client, name):
    return client.post("/api/worlds",
                       json={"theme_id": narrator_mod.THEMES[0]["id"], "name": name}).json()["world_id"]


def _dur_records(caplog, path_part: str = "/api/"):
    """Записи журнала именно от строки длительности (msg: «METHOD /path → NNN за X мс»)."""
    out = []
    for rec in caplog.records:
        msg = rec.getMessage()
        if "→" in msg and " за " in msg and "мс" in msg and path_part in msg:
            out.append(rec)
    return out


@pytest.fixture
def info_logs(caplog):
    """conftest выставляет LOG_LEVEL=WARNING, а успешный запрос пишется на INFO."""
    with caplog.at_level(logging.INFO, logger="textgame"):
        yield caplog


# ── 1. мир в строке длительности ────────────────────────────────────────────

def test_world_id_present_in_duration_line(api_client, info_logs):
    client, _ = api_client
    wid = _mk_world(client, "D13-1")
    assert client.get(f"/api/worlds/{wid}").status_code == 200
    recs = _dur_records(info_logs, f"/api/worlds/{wid}")
    assert recs, "нет строки длительности для API-запроса по миру"
    fields = getattr(recs[-1], "fields", None) or {}
    assert fields.get("world_id") == wid, \
        f"в «GET /api/worlds/{wid} → 200 за N мс» нет world_id: {fields}"
    assert fields.get("request_id"), "request_id обязан остаться как был"
    assert fields.get("status") == 200


def test_error_line_carries_world_id(api_client, info_logs):
    """4xx пишутся на WARNING и тоже обязаны быть с миром — иначе сбой не найти."""
    client, _ = api_client
    r = client.get("/api/worlds/999999/memory")
    assert r.status_code == 404
    recs = _dur_records(info_logs, "/api/worlds/999999/memory")
    assert recs, "нет строки длительности для 404"
    rec = recs[-1]
    assert rec.levelno == logging.WARNING
    assert getattr(rec, "fields", {}).get("world_id") == 999999


def test_json_log_line_carries_world(api_client, tmp_path, info_logs):
    """То же видит читатель `data/logs/game.log`: JsonFormatter отдаёт поле наружу."""
    client, _ = api_client
    wid = _mk_world(client, "D13-3")
    assert client.get(f"/api/worlds/{wid}").status_code == 200
    rec = _dur_records(info_logs, f"/api/worlds/{wid}")[0]
    obj = json.loads(logsetup.JsonFormatter().format(rec))
    assert obj["world_id"] == wid
    assert obj["request_id"] == getattr(rec, "fields")["request_id"]


# ── 2. поведение не изменилось там, где мира нет ────────────────────────────

def test_non_world_api_line_has_no_world_id(api_client, info_logs):
    client, _ = api_client
    assert client.get("/api/system/status").status_code == 200
    recs = _dur_records(info_logs, "/api/system/status")
    assert recs, "строка длительности для статуса системы пропала"
    fields = getattr(recs[-1], "fields", {})
    # мира в пути нет — поля просто не должно быть (не `world_id: null`)
    assert "world_id" not in fields, f"выдуман мир для запроса без мира: {fields}"
    assert fields.get("request_id"), "request_id обязан остаться"


def test_static_and_pages_stay_silent(api_client, info_logs):
    """Фильтр «не шумим на статике/страницах» трогать нельзя: пишутся только /api/."""
    client, _ = api_client
    client.get("/")
    client.get("/static/app.js")
    assert not _dur_records(info_logs, ""), "строка длительности появилась не-API-запросу"


def test_context_still_reset_after_request(api_client):
    """`finally` на месте: контекст запроса не течёт в следующий вызов."""
    client, _ = api_client
    wid = _mk_world(client, "D13-4")
    client.get(f"/api/worlds/{wid}")
    ctx = logsetup.current_context()
    assert ctx.get("world_id") is None, "контекст мира не снят после запроса"
    assert not ctx.get("request_id"), "контекст request_id не снят после запроса"


def test_context_live_inside_request(api_client):
    """Внутри запроса контекст работает как работал: ставится ДО `call_next` (не снят раньше)."""
    client, _ = api_client
    wid = _mk_world(client, "D13-5")
    seen: dict = {}

    async def _probe():
        seen.update(logsetup.current_context())
        return {"ok": True}

    app_mod.app.add_api_route("/api/worlds/{world_id}/_probe", _probe, methods=["GET"])
    try:
        assert client.get(f"/api/worlds/{wid}/_probe").status_code == 200
    finally:
        app_mod.app.router.routes = [
            r for r in app_mod.app.router.routes
            if getattr(r, "path", "") != "/api/worlds/{world_id}/_probe"]
    assert seen.get("world_id") == wid, "внутри запроса мир в контексте пропал"
    assert seen.get("request_id"), "внутри запроса нет request_id — цепочка хода распалась"


# ── 3. сторож исходника ────────────────────────────────────────────────────

def test_source_guards_explicit_world_field():
    src = inspect.getsource(app_mod._log_request_context)
    assert '"world_id"' in src, \
        "мир снова берётся только из контекста, который к моменту лога уже сброшен"
    assert "finally" in src, "снятие контекста в finally обязательно (иначе течёт в следующий запрос)"
    assert 'path.startswith("/api/")' in src, "фильтр «не шумим на статике» трогать нельзя"
