# -*- coding: utf-8 -*-
"""Сессия 52 — A12 (аудит 41): `/api/system/status` не врёт о живости модели.

В проекте жили ДВА разных критерия «жива ли модель»:
  * `llm.check_available()` → `GET {base_url}/models` с заголовком `Authorization: Bearer …`
    и «жив» = ответ < 500 (специально: 401/404 от llama.cpp с `--api-key` — не «сервер мёртв»);
  * `routers/system.py` → голый `GET {base_url}/models` БЕЗ ключа и «жив» = ровно 200.

Симптом: на защищённом ключом сервере (llama.cpp `--api-key` или облачный шлюз) мир играется,
а плашка статуса и админская диагностика показывают `llm.up: false`.

Чиним: одна точка истины — `llm.probe()` (её же зовёт `check_available`), а «не отвечает» и
«требует ключ» разделены полем `needs_key`.

Правило 19: проверяется поведение (сеть подменена на `_get_client`, ответы — фиктивные),
а не тексты исходников.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from backend import llm as llm_mod


class _Resp:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _Client:
    """Фиктивный HTTP-клиент: помнит последний запрос, отвечает заранее заданное."""

    def __init__(self, status_code=None, exc=None):
        self.status_code = status_code
        self.exc = exc
        self.url = None
        self.headers = None

    async def get(self, url, **kw):
        self.url = url
        self.headers = kw.get("headers") or {}
        if self.exc:
            raise self.exc
        return _Resp(self.status_code)


def _probe(client):
    return asyncio.run(_call(client))


async def _call(client):
    orig = llm_mod._get_client
    llm_mod._get_client = lambda: client
    try:
        return await llm_mod.probe({"base_url": "http://127.0.0.1:8080/v1", "api_key": "sk-XXXX"})
    finally:
        llm_mod._get_client = orig


# ── юнит: критерий probe ─────────────────────────────────────────────────

def test_probe_treats_401_as_alive_and_needs_key():
    """401 на /models = сервер ЖИВ (ход работает), но ключ не принят — отдельное поле."""
    up, needs_key, detail = _probe(_Client(status_code=401))
    assert up is True, "401 больше не считается «модель мертва»"
    assert needs_key is True
    assert detail == "401"


def test_probe_sends_authorization():
    """Запрос к /models обязан нести ключ — иначе защищённый сервер отвечает 401 всегда."""
    c = _Client(status_code=200)
    _probe(c)
    assert c.headers.get("Authorization") == "Bearer sk-XXXX"


def test_probe_without_key_sends_no_header():
    c = _Client(status_code=200)
    orig = llm_mod._get_client
    llm_mod._get_client = lambda: c
    try:
        up, needs_key, _d = asyncio.run(llm_mod.probe({"base_url": "http://h/v1"}))
    finally:
        llm_mod._get_client = orig
    assert (up, needs_key) == (True, False)
    assert "Authorization" not in c.headers


def test_probe_connection_refused_is_down():
    """Сервиса нет (порт закрыт) — честно «внизу», без needs_key."""
    c = _Client(exc=httpx.ConnectError("Connection refused"))
    up, needs_key, detail = _probe(c)
    assert up is False and needs_key is False
    assert "ConnectError" in detail


def test_probe_5xx_is_down():
    up, needs_key, _d = _probe(_Client(status_code=502))
    assert up is False and needs_key is False


def test_probe_empty_base_url_is_down():
    orig = llm_mod._get_client
    llm_mod._get_client = lambda: pytest.fail("запрос при пустом base_url недопустим")
    try:
        up, needs_key, detail = asyncio.run(llm_mod.probe({"base_url": "  "}))
    finally:
        llm_mod._get_client = orig
    assert (up, needs_key) == (False, False)
    assert "base_url" in detail


def test_check_available_matches_probe():
    """`check_available` — обёртка над `probe` (второго критерия в проекте нет)."""
    orig = llm_mod._get_client
    llm_mod._get_client = lambda: _Client(status_code=404)
    try:
        assert asyncio.run(llm_mod.check_available({"base_url": "http://h/v1"})) is True
        llm_mod._get_client = lambda: _Client(exc=httpx.ConnectTimeout("t"))
        assert asyncio.run(llm_mod.check_available({"base_url": "http://h/v1"})) is False
    finally:
        llm_mod._get_client = orig


# ── HTTP-контур: статус-эндпоинт использует тот же критерий ──────────────

def test_status_reports_401_as_up(api_client, monkeypatch):
    """Раньше: голый GET без ключа + `== 200` → `llm.up: false` при живой модели.

    Ключ обязан УХОДИТЬ в probe статуса: без настройки в CI провайдер — llamacpp с
    пустым ключом (заголовка нет по праву), поэтому сценарий «ключ есть» задаём явно.
    """
    client, _ = api_client
    c = _Client(status_code=401)
    monkeypatch.setattr(llm_mod, "_get_client", lambda: c)
    from backend.config import get_config

    cfg = get_config()
    monkeypatch.setattr(cfg, "main_provider", "openai_compat")
    monkeypatch.setattr(cfg, "main_api_key", "sk-TESTKEY")
    body = client.get("/api/system/status").json()
    assert body["llm"]["up"] is True
    assert body["llm"]["needs_key"] is True
    assert c.headers.get("Authorization") == "Bearer sk-TESTKEY", \
        "статус обязан спрашивать модель с ключом"
    # ключ не утёк в ответ статуса (правило 3)
    assert "sk-TESTKEY" not in body["llm"]["detail"]


def test_status_reports_refused_as_down(api_client, monkeypatch):
    client, _ = api_client
    monkeypatch.setattr(llm_mod, "_get_client",
                        lambda: _Client(exc=httpx.ConnectError("Connection refused")))
    body = client.get("/api/system/status").json()
    assert body["llm"]["up"] is False
    assert body["llm"]["needs_key"] is False
    assert "ConnectError" in body["llm"]["detail"]


def test_status_never_leaks_api_key(api_client, monkeypatch):
    """Правило 3: наружу — маска; в `detail` — только код ответа/имя исключения."""
    client, _ = api_client
    monkeypatch.setattr(llm_mod, "_get_client", lambda: _Client(status_code=200))
    txt = client.get("/api/system/status").text
    from backend.config import get_config

    key = get_config().main_api_key
    if key:  # в тестовой среде ключ может быть пустым
        assert key not in txt, "живой ключ утёк в /api/system/status"
        assert "••••••" in txt, "провайдеры обязаны уходить замаскированными"
