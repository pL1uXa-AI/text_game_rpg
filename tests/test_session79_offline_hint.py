# -*- coding: utf-8 -*-
"""Жалоба: «не могу запустить без локального сервера, хотя локальный сервер использовать и не собираюсь».

Разбор: приложение — это Python-сервер (`uvicorn backend.app:app`), он же и есть игра. Если открыть
`frontend/index.html` напрямую из папки (`file://`), ни относительные `/api/*`, ни `/static/app.js`,
ни `/static/style.css` не загрузятся: игрок видит пустой экран и красную консоль.

Стало (сценарий «файл вместо сервера»): оболочки `/` (`index.html`) и `/admin` ещё до разбора
`<body>` проверяют `location.protocol === "file:"` и уводят на `frontend/offline.html` — страницу-
подсказку «запустите start_game.bat», без всякого сервера и без ошибок в консоли.

Что сторожится: подсказка реально отдаётся сервером, редирект стоит в ОБОИХ HTML и срабатывает
только для `file://`, а `start_game.bat` — тот прибор, на который подсказка ссылается.
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.testclient import TestClient

from backend.routers import system as sys_mod

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"

FILE_REDIRECT = re.compile(r"""location\.protocol\s*(?:===|!==|==)\s*["']file:["']""")


def _client():
    app = FastAPI()
    app.include_router(sys_mod.router)
    app.mount("/static", StaticFiles(directory=sys_mod.FRONTEND_DIR), name="static")
    return TestClient(app)


# ── страница-подсказка ────────────────────────────────────────────────────

def test_offline_page_exists():
    p = FRONTEND / "offline.html"
    assert p.is_file(), "нет frontend/offline.html — подсказка при открытии файлом пропала"


def test_offline_page_served_by_static():
    """Подсказка — обычный файл в /static, сервер отдаёт её без правок."""
    r = _client().get("/static/offline.html")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_offline_page_tells_how_to_start():
    html = (FRONTEND / "offline.html").read_text(encoding="utf-8")
    assert "start_game.bat" in html
    assert "127.0.0.1:8002" in html


# ── редирект в обеих оболочках ────────────────────────────────────────────

def test_index_redirects_on_file():
    """index.html: guard стоит в <head>, до загрузки app.js и любого запроса к /api."""
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    head = html.split("<body", 1)[0]
    assert FILE_REDIRECT.search(head), "index.html не проверяет file:// до загрузки SPA"
    assert 'location.replace("offline.html")' in head


def test_admin_redirects_on_file():
    """admin.html: guard живёт в СВОЁМ inline-скрипте (первая строка, до любых запросов).

    Отдельный <script> в <head> ломал scripts/check_frontend.py: чекер вытаскивает
    скрипт жадной регуляркой `<script>(.*)</script>` и получал разметку между тегами.
    Поэтому проверка живёт внутри уже существующего скрипта админки (тоже до всех fetch).
    """
    html = (FRONTEND / "admin.html").read_text(encoding="utf-8")
    assert FILE_REDIRECT.search(html), "admin.html не проверяет file://"
    # guard — до первого сетевого вызова
    guard_at = html.index("location.replace(\"offline.html\")")
    assert guard_at < html.index("fetch("), "редирект стоит после первого запроса к API"


def test_redirect_only_for_file_protocol():
    """Через сервер страницы не должны себя никуда уводить: редирект живёт под условием file:."""
    for name in ("index.html", "admin.html"):
        html = (FRONTEND / name).read_text(encoding="utf-8")
        # единственное вхождение replace — сразу после guard'а про file:
        assert html.count("location.replace(") == 1, name
        assert re.search(r'if \(location\.protocol !== "file:"\) return;\s*\n\s*'
                         r'location\.replace\("offline\.html"\);', html) \
            or re.search(r'if \(location\.protocol === "file:"\) \{?\s*'
                         r'location\.replace\("offline\.html"\);', html), name
    # gameplay-скрипты на месте: страницы не превращены в заглушки
    assert '<script src="/static/app.js"></script>' in (FRONTEND / "index.html").read_text(encoding="utf-8")


def test_served_pages_keep_body():
    """Правка не превратила отдаваемые сервером страницы в заглушки."""
    c = _client()
    assert "<div id=\"app\">" in c.get("/").text
    assert "Админка" in c.get("/admin").text


# ── ссылка на launcher настоящая ──────────────────────────────────────────

def test_start_bat_is_the_launcher():
    bat = (ROOT / "start_game.bat").read_text(encoding="utf-8", errors="replace")
    assert "uvicorn backend.app:app" in bat
    assert "--port 8002" in bat


def test_routes_unchanged():
    paths = {r.path for r in sys_mod.router.routes}
    assert paths == {"/", "/admin", "/api/metrics", "/api/system/status"}
