# -*- coding: utf-8 -*-
"""D15 (аудит 41): обе HTML-страницы отдаются с ОДИНАКОВЫМ `Cache-Control`.

Было: `/admin` нёс `Cache-Control: no-store`, а `/` — ничего. Без явного заголовка браузер
кешировал index.html по эвристике (у `FileResponse` есть `last-modified`/`etag`), поэтому после
правки фронта обычный F5 приносил старый HTML — отсюда и жалоба правила 6 («index.html
кешируется — hard refresh»).

Стало: единая константа `_PAGE_HEADERS` в `routers/system.py`, `no-store` на обоих роутах.

Что сторожится: заголовок есть и совпадает на `/` и `/admin`; текст HTML не изменился;
фолбэк-пути живы (несуществующий файл → не 200, а 500-я не превращается в тихий успех);
`/static/*` (другой механизм — `StaticFiles`) НЕ затронут правкой; исходник не разъехался.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.testclient import TestClient

from backend.routers import system as sys_mod

ROOT = Path(__file__).resolve().parent.parent

EXPECTED = "no-store"


def _client():
    """Мини-приложение ровно с теми же роутами страниц (без lifespan и LLM-заглушек)."""
    app = FastAPI()
    app.include_router(sys_mod.router)
    app.mount("/static", StaticFiles(directory=sys_mod.FRONTEND_DIR), name="static")
    return TestClient(app)


# ── заголовки обоих роутов ───────────────────────────────────────────────

def test_index_has_cache_control():
    r = _client().get("/")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == EXPECTED


def test_admin_has_cache_control():
    r = _client().get("/admin")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == EXPECTED


def test_both_pages_identical_header():
    """Собственно претензия D15: страницы не должны спорить о кеше."""
    c = _client()
    a, b = c.get("/"), c.get("/admin")
    assert a.status_code == b.status_code == 200
    assert a.headers["cache-control"] == b.headers["cache-control"] == EXPECTED


def test_pages_still_real_html():
    """no-store не сломал выдачу: это по-прежнему те самые файлы."""
    c = _client()
    assert "Text Game" in c.get("/").text
    assert "Админка" in c.get("/admin").text
    assert (ROOT / "frontend" / "index.html").read_text(encoding="utf-8") == c.get("/").text


def test_single_header_constant_used():
    """Единая точка истины в роуте — обе функции берут один и тот же словарь."""
    src = inspect.getsource(sys_mod)
    assert "_PAGE_HEADERS" in src
    body_index = inspect.getsource(sys_mod.index)
    body_admin = inspect.getsource(sys_mod.admin_page)
    assert "_PAGE_HEADERS" in body_index and "_PAGE_HEADERS" in body_admin
    assert sys_mod._PAGE_HEADERS == {"Cache-Control": EXPECTED}


# ── фолбэк-пути ───────────────────────────────────────────────────────────

def test_missing_file_is_not_silently_ok(tmp_path, monkeypatch):
    """Если HTML пропал — честная ошибка, а не пустой 200 с no-store."""
    monkeypatch.setattr(sys_mod, "FRONTEND_DIR", str(tmp_path))
    app = FastAPI()
    app.include_router(sys_mod.router)
    with pytest.raises(RuntimeError):
        TestClient(app, raise_server_exceptions=True).get("/")


def test_fileresponse_carries_headers_on_error_path():
    """404-ветка (отсутствующий файл) не «наследуем» страницу — заголовок живёт в ответе."""
    resp = FileResponse("nope.html", headers=sys_mod._PAGE_HEADERS)
    assert resp.headers["cache-control"] == EXPECTED
    assert resp.path.endswith("nope.html")


# ── границы правки ────────────────────────────────────────────────────────

def test_static_mount_untouched():
    """StaticFiles (app.js/style.css) — другой механизм, он НЕ получил no-store:
    иначе каждый F5 тянул бы 190 КБ JS заново. Правка касалась только HTML-роутов."""
    r = _client().get("/static/app.js")
    assert r.status_code == 200
    assert "etag" in r.headers
    assert "cache-control" not in r.headers


def test_api_routes_not_rewritten():
    """Список роутов системы не изменился (только заголовки, новых путей нет)."""
    paths = {r.path for r in sys_mod.router.routes}
    assert paths == {"/", "/admin", "/api/metrics", "/api/system/status"}
