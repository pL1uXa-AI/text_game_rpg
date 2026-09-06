# -*- coding: utf-8 -*-
"""Аудит 41, B2 (сессия 55): rate-limit на «дорогих» эндпоинтах + доступ к админке.

Что проверяется (правило 19 — наблюдаем ПОВЕДЕНИЕ приложения, а не тексты исходников):

1. Каждый роут, который зовёт LLM / облачные эмбеддинги / синтез голоса / скачивание
   моделей / импорт дампа / правку глобальных настроек, стоит под лимитером. Сверяется по
   реальным зависимостям FastAPI-маршрутов (`route.dependant`), поэтому роутер без guard'а
   или scope, которого нет в реестре `SCOPES`, валит тест, — а реестр не может устареть,
   потому что имена scope'ов берутся из самого приложения.
2. Лимиты реестра валидны (положительные, целые пороги) и неизвестный scope не «бесконечный»,
   а падает в `default`.
3. Guard реально режет: после лимита — HTTPException(429), отказ виден в журнале с ключом
   (A6), а выключенный лимитер (`RATE_LIMIT_ENABLED=false`, тестовая среда) не режет НИЧЕГО.
4. Чужой адрес в сети (запуск с `GAME_BIND=0.0.0.0`) получает лимиты в `REMOTE_FACTOR` раз
   жесте — и это проверяется поведением, а не арифметикой в тесте.
5. `/api/admin/settings` (правка провайдеров, `MAIN_BASE_URL` и ключей) — только localhost:
   GET/POST с чужого адреса = 403, с localhost = 200, `ADMIN_ALLOW_LAN=true` = снова 200.
   Через саму админку этот ключ не включается (`hidden_admin_keys`).
6. `RATE_LIMIT_ENABLED` из конфига применяется стартом сервера (`_lifespan`), а не живёт
   только в docstring модуля (иначе ключ в .env был бы обещанием в никуда).

Запуск: "…\\3.12.10\\python.exe" -X utf8 -m pytest tests/test_session55_ratelimit_routes.py -q
"""
from __future__ import annotations

import logging

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import ratelimit as rl
from backend.app import app

# Роуты, которые стоят денег/ресурса: LLM-проход, облачный эмбеддинг, синтез речи,
# скачка моделей, полная переиндексация памяти, запись глобальных настроек.
# (method, path) → обязательный scope из `SCOPES`.
#
# `/hint` отдельной строки нет: это слэш-команда ВНУТРИ POST .../action (`_slash_hint`),
# то есть ходит под тем же лимитером, что и сам ход.
EXPENSIVE = {
    ("POST", "/api/worlds"): "create_world",
    ("POST", "/api/worlds/{world_id}/action"): "action",
    ("POST", "/api/worlds/{world_id}/action/stream"): "action_stream",
    ("POST", "/api/worlds/{world_id}/divine"): "divine",
    ("POST", "/api/worlds/{world_id}/suggest"): "suggest",
    ("POST", "/api/worlds/{world_id}/vision/trigger"): "vision",
    ("POST", "/api/worlds/{world_id}/providers"): "providers",
    ("POST", "/api/worlds/import/json"): "import_json",
    ("GET", "/api/worlds/{world_id}/memory/search"): "memory_search",
    ("GET", "/api/worlds/{world_id}/lore/search"): "lore_search",
    ("POST", "/api/worlds/{world_id}/lore"): "lore_write",
    ("PATCH", "/api/worlds/{world_id}/lore/{lore_id}"): "lore_write",
    ("POST", "/api/worlds/{world_id}/entities"): "entity_write",
    ("PATCH", "/api/worlds/{world_id}/entities/{kind}/{entity_key}"): "entity_write",
    ("POST", "/api/tts/test"): "tts_test",
    ("POST", "/api/tts/download"): "tts_download",
    ("POST", "/api/worlds/{world_id}/events/{event_id}/tts/retry"): "tts_retry",
    ("GET", "/api/admin/settings"): "admin",
    ("POST", "/api/admin/settings"): "admin",
}


# ────────────────────────── наблюдение за приложением ──────────────────────────
def _all_routes():
    """Все маршруты приложения, раскрывая include_router (FastAPI прячет их в _IncludedRouter)."""
    def walk(routes):
        for r in routes:
            sub = getattr(r, "original_router", None)
            if sub is not None:
                yield from walk(sub.routes)
                continue
            yield r
    yield from walk(app.routes)


def _guards(route) -> tuple[list[str], bool]:
    """(список scope'ов rate-limit, требует ли роут localhost) по РЕАЛЬНЫМ зависимостям."""
    scopes: list[str] = []
    local = False
    dep = getattr(route, "dependant", None)
    for d in getattr(dep, "dependencies", []) or []:
        call = d.call
        scope = getattr(call, "_rl_scope", None)
        if scope:
            scopes.append(scope)
        if getattr(call, "_rl_local", False):
            local = True
    return scopes, local


def _find(method: str, path: str):
    for r in _all_routes():
        if getattr(r, "path", None) == path and method in (getattr(r, "methods", None) or ()):
            return r
    return None


# ───────────────────────────── 1. реестр роутов ─────────────────────────────
def test_expensive_routes_are_guarded():
    """Ни один «дорогой» роут не остался без лимитера (B2: раньше их было три из ~20)."""
    missing = []
    for (method, path), scope in EXPENSIVE.items():
        r = _find(method, path)
        if r is None:
            missing.append(f"{method} {path} — роут вообще пропал из приложения")
            continue
        scopes, _local = _guards(r)
        if scope not in scopes:
            missing.append(f"{method} {path} — нет guard'а «{scope}», есть только {scopes}")
    assert not missing, "роуты без rate-limit:\n  " + "\n  ".join(missing)


def test_guarded_routes_all_use_known_scopes():
    """Обратная сторона: в приложении нет лимитеров с scope, которого нет в `SCOPES`."""
    unknown = []
    for r in _all_routes():
        scopes, _ = _guards(r)
        for s in scopes:
            if s not in rl.SCOPES:
                unknown.append(f"{getattr(r, 'path', '?')}: scope {s!r}")
    assert not unknown, f"неизвестные scope'ы (guard взял бы дефолт молча): {unknown}"


def test_admin_routes_require_localhost():
    """Админка (провайдеры, base_url, ключи) закрыта до localhost — 403 снаружи не обходится."""
    for method in ("GET", "POST"):
        r = _find(method, "/api/admin/settings")
        assert r is not None
        _scopes, local = _guards(r)
        assert local, f"{method} /api/admin/settings — нет проверки адреса"


def test_loopback_guard_is_not_applied_to_public_api():
    """Замыкаем на localhost ТОЛЬКО настройки: игровой API должен остаться играбельным."""
    for (method, path) in (("POST", "/api/worlds/{world_id}/action"),
                           ("GET", "/api/worlds")):
        r = _find(method, path)
        _scopes, local = _guards(r)
        assert not local, f"{method} {path} — игровой роут лег бы под require_local"


# ───────────────────────────── 2. реестр лимитов ─────────────────────────────
def test_scope_registry_values_are_sane():
    """Лимиты щедрые, но конечные: ноль/отрицательное отключало бы эндпоинт целиком."""
    for scope, lim in rl.SCOPES.items():
        for key in ("limit", "window", "burst", "burst_window"):
            assert key in lim, f"{scope}: нет параметра {key}"
            assert lim[key] > 0, f"{scope}.{key} = {lim[key]} (0 = запрет, а не лимит)"
        assert int(lim["limit"]) == lim["limit"] and int(lim["burst"]) == lim["burst"], \
            f"{scope}: пороги обязаны быть целыми"
        # «дорогие» роуты не должны случайно получить дефолтные 120/мин: дороже — меньше
        if scope in ("create_world", "import_json", "tts_download", "vision", "divine"):
            assert lim["limit"] < rl.SCOPES["default"]["limit"], f"{scope} не «дороже» дефолта"


def test_unknown_scope_falls_back_to_default():
    """Опечатка в имени scope не означает «без лимита» — только дефолт."""
    assert rl.scope_limits("net-takogo-skopa") == dict(rl.SCOPES["default"])


# ───────────────────────────── 3. guard режет ─────────────────────────────
class _Req:
    """Минимальный дубль Request: у лимитера спрашивается только request.client.host."""

    def __init__(self, host: str = "127.0.0.1"):
        self.client = type("C", (), {"host": host})()


@pytest.fixture
def limiter_on(monkeypatch):
    """Включённый на время теста лимитер с чистыми счётчиками (среда по умолчанию — выкл.)."""
    rl.reset()
    monkeypatch.setattr(rl, "ENABLED", True)
    yield
    rl.reset()
    monkeypatch.setattr(rl, "ENABLED", False)


def test_guard_blocks_after_limit_of_its_scope(limiter_on, monkeypatch):
    """Дорогой роут отдаёт 429 ровно после своего лимита (значения читаются из реестра)."""
    monkeypatch.setitem(rl.SCOPES, "tts_test",
                        {"limit": 3, "window": 60, "burst": 3, "burst_window": 5})
    guard = rl.guard_for("tts_test")
    for _ in range(3):
        guard(_Req())          # молча проходит
    with pytest.raises(HTTPException) as ei:
        guard(_Req())
    assert ei.value.status_code == 429
    # другой scope и другой адрес — независимые счётчики (общий ключ «съел» бы игру)
    guard2 = rl.guard_for("tts_download")
    guard2(_Req())
    guard(_Req(host="10.9.8.7"))   # чужой адрес — свой ключ, не блокирован чужим лимитом


def test_guard_refusal_is_logged_with_key(limiter_on, monkeypatch, caplog):
    """A6: отказ обязан быть в журнале с scope и ключом (голый 429 в логе ни о чём)."""
    monkeypatch.setitem(rl.SCOPES, "divine",
                        {"limit": 1, "window": 60, "burst": 1, "burst_window": 5})
    guard = rl.guard_for("divine")
    guard(_Req())
    with caplog.at_level(logging.WARNING, logger="textgame"):
        with pytest.raises(HTTPException):
            guard(_Req())
    hits = [r.getMessage() for r in caplog.records if "rate-limit" in r.getMessage()]
    assert hits, f"отказ 429 не попал в журнал: {[r.getMessage() for r in caplog.records]}"
    assert "divine" in hits[0] and "127.0.0.1" in hits[0], hits[0]


def test_disabled_limiter_blocks_nothing(monkeypatch):
    """Тестовая среда / отладка: ENABLED=False — хоть миллион запросов под минимальным лимитом."""
    rl.reset()
    monkeypatch.setattr(rl, "ENABLED", False)
    guard = rl.guard_for("import_json")
    for _ in range(50):
        guard(_Req())     # без исключения
    rl.reset()


def test_remote_host_gets_tighter_limits():
    """B2: сосед по сети (GAME_BIND=0.0.0.0) — в REMOTE_FACTOR раз жесте, игрок не затронут."""
    base = rl.scope_limits("action", "127.0.0.1")
    assert base == dict(rl.SCOPES["action"]), "своему хосту лимиты не меняются"
    far = rl.scope_limits("action", "10.0.0.5")
    assert far["limit"] == max(1, base["limit"] // rl.REMOTE_FACTOR)
    assert far["burst"] == max(1, base["burst"] // rl.REMOTE_FACTOR)
    assert far["window"] == base["window"], "окно — не масштабируется, только пороги"
    # и никогда не ноль (0 = «запретить эндпоинт совсем»)
    for host in ("10.1.2.3", "192.168.0.47", "2001:db8::1"):
        assert rl.scope_limits("tts_download", host)["limit"] >= 1


def test_remote_guard_actually_blocks_earlier(limiter_on, monkeypatch):
    """Не арифметика, а поведение: тому же скрипту с чужого адреса хватит меньше запросов."""
    monkeypatch.setitem(rl.SCOPES, "memory_search",
                        {"limit": 8, "window": 60, "burst": 4, "burst_window": 5})
    guard = rl.guard_for("memory_search")
    # чужой: limit 2, burst 1 → второй запрос уже отказ
    guard(_Req(host="10.0.0.5"))
    with pytest.raises(HTTPException):
        guard(_Req(host="10.0.0.5"))
    # localhost под тем же guard'ом ещё далёк от своего лимита
    for _ in range(4):
        guard(_Req())


# ───────────────────────────── 4. require_local ─────────────────────────────
def test_require_local_allows_loopback_and_refuses_other():
    """Свой адрес — пропуск, чужой — 403 (в т.ч. пустой/неизвестный клиент)."""
    rl.reset()
    for host in ("127.0.0.1", "127.0.0.9", "::1", "localhost"):
        rl.require_local(_Req(host=host))      # не бросает
    for host in ("10.0.0.5", "192.168.1.7", "8.8.8.8"):
        with pytest.raises(HTTPException) as ei:
            rl.require_local(_Req(host=host))
        assert ei.value.status_code == 403
    assert not rl.is_loopback(""), "пустой адрес = не свой (не открываем доступ «в никуда»)"
    assert not rl.is_loopback(None)


def test_admin_settings_http_403_from_remote():
    """Реальный HTTP-контур: чужой хост не читает и не пишет настройки, localhost — может."""
    from backend import bg as _bg
    _bg.reset()
    try:
        remote = TestClient(app, client=("10.11.12.13", 40000))
        r = remote.get("/api/admin/settings")
        assert r.status_code == 403, f"админка открылась чужому: {r.status_code}"
        r = remote.post("/api/admin/settings", json={"log_level": "DEBUG"})
        assert r.status_code == 403, f"чужой записал настройки: {r.status_code}"
        assert "localhost" in r.text.lower() or "ADMIN_ALLOW_LAN" in r.text
        # фикстура api_client ходит с 127.0.0.1 — там тот же запрос проходит (см. ниже)
    finally:
        _bg.reset()


def test_admin_settings_ok_from_local(api_client):
    """Тот же роут с localhost работает — 403 не должен сломать ни игру, ни админку."""
    client, _holder = api_client
    assert client.get("/api/admin/settings").status_code == 200


def test_admin_allow_lan_switch_reopens_and_is_not_self_serviceable(api_client, fake_config):
    """ADMIN_ALLOW_LAN=true — доступ из сети включается ЯВНО, и самой админкой не меняется."""
    from backend import bg as _bg
    from backend.config import hidden_admin_keys
    assert "ADMIN_ALLOW_LAN" in hidden_admin_keys(), \
        "ключ не должен попадать в overridable: открывший админку не вправе сам себя разблокировать"
    client, _holder = api_client
    remote = TestClient(app, client=("172.16.0.9", 40000))
    assert remote.get("/api/admin/settings").status_code == 403
    fake_config(admin_allow_lan=True)
    _bg.reset()
    try:
        assert remote.get("/api/admin/settings").status_code == 200, \
            "ADMIN_ALLOW_LAN=true не открыл админку"
    finally:
        _bg.reset()


# ───────────────────── 5. RATE_LIMIT_ENABLED из конфига ─────────────────────
def test_rate_limit_config_reads_env(tmp_path):
    """Ключ .env существует не для красоты: он разворачивает выключатель (тот же путь, что у других флагов)."""
    from backend.config import Config
    p = tmp_path / ".env"
    p.write_text("RATE_LIMIT_ENABLED=false\n", encoding="utf-8")
    assert Config.load(env_file=p, env={"SENTINEL": "1"}).rate_limit_enabled is False
    assert Config.load(env_file=tmp_path / "нет.env", env={"SENTINEL": "1"}).rate_limit_enabled is True, \
        "по умолчанию лимитер ВКЛЮЧЁН (выключенный rate-limit — неожиданность, а не безопасность)"


def test_lifespan_applies_rate_limit_flag(monkeypatch):
    """Старт сервера применяет конфиг: иначе `RATE_LIMIT_ENABLED=false` в .env ничего бы не делал.

    Проверяем факт вызова (шпион над `ratelimit.configure`), а не итоговый глобал:
    `configure(enabled=True)` поднял бы лимитер посреди прогона и следующие API-тесты
    ловили бы 429. Тот же приём, что у `test_session53_startup_heal` (lifespan без
    TestClient, `__aenter__`/`__aexit__`; тяжёлый само-исцеление и прогрев озвучки — заглушки,
    чтобы старт не лез в БД, которую этот же выход закрывает)."""
    import asyncio

    from backend import app as app_mod
    from backend import config as config_mod

    seen: list[bool] = []
    real_configure = rl.configure

    def _spy(enabled=None):
        seen.append(enabled)
        return real_configure(enabled=None)     # среду НЕ трогаем: пусть остаётся выкл.

    monkeypatch.setattr(app_mod, "self_heal", lambda: {"worlds": 0})
    monkeypatch.setattr(app_mod, "_startup_heal", lambda: asyncio.sleep(0))
    monkeypatch.setattr(app_mod, "_startup_tts_preload", lambda: asyncio.sleep(0))
    monkeypatch.setattr(rl, "configure", _spy)
    async def _scenario():
        real = config_mod.get_config()

        class _C(type(real)):
            pass

        cfg = _C()
        for k, v in dict(getattr(real, "__dict__", {}) or {}).items():
            setattr(cfg, k, v)
        cfg.rate_limit_enabled = False
        orig_app, orig_cfg = app_mod.get_config, config_mod.get_config
        app_mod.get_config = config_mod.get_config = lambda: cfg
        try:
            ctx = app_mod._lifespan(app_mod.app)
            await ctx.__aenter__()
            await ctx.__aexit__(None, None, None)
        finally:
            app_mod.get_config, config_mod.get_config = orig_app, orig_cfg
    asyncio.run(_scenario())
    assert False in seen, f"старт не применил RATE_LIMIT_ENABLED: {seen}"
