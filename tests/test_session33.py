# -*- coding: utf-8 -*-
"""Сессия 33 — авто-детект окна модели, стартующие переживающие метрики и бэкап.

  * llm.probe_context: llamacpp `/props`, ollama `/api/show`, OpenAI `/models`
    (включая облачный формат `context_length`), кэш, тишина при недоступности;
  * core._context_guard: снижает context_tokens мира, если заявленное окно больше
    реального n_ctx (защита от молчаливых обрезов), и НЕ трогает ничего, когда лимит
    неизвестен; отдаёт лимит в gen_settings для UI;
  * metrics: журнал data/metrics.jsonl переживает «рестарт» (пустой in-memory буфер),
    битые строки не валят отчёт, выключенный персист ничего не пишет.

Async-проверки гоняются через asyncio.run (pytest-asyncio в проект не добавляем).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend import llm, metrics
from backend import config as config_mod


@pytest.fixture(autouse=True)
def _clean_state():
    """Кэш probe'ов и буферы метрик не протекают между тестами."""
    llm._ctx_cache.clear()
    metrics._samples.clear()
    yield
    llm._ctx_cache.clear()
    metrics._samples.clear()


@pytest.fixture
def fake_config(monkeypatch):
    """Подменить кэшированный Config, сохранив прочие поля реального конфига
    (config.get_config читает config._cache['cfg'] — меняем только нужное)."""
    def _set(**kw):
        real = config_mod.get_config()
        # наследуемся от РЕАЛЬНОГО класса: методы (resolve_world_providers/get_provider) должны жить
        class _C(type(real)):
            pass
        cfg = _C()
        vals = dict(getattr(real, "__dict__", {}) or {})
        vals.setdefault("detect_model_context", True)
        vals.setdefault("metrics_persist", True)
        vals.setdefault("metrics_tail", 500)
        vals.setdefault("metrics_file", "")
        vals.update(kw)
        for k, v in vals.items():
            setattr(cfg, k, v)
        monkeypatch.setattr(config_mod, "_cache", {"cfg": cfg})
        return cfg
    return _set


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload


class _FakeHTTP:
    """Заглушка httpx.AsyncClient: отвечает на /props, /api/show, /models."""

    def __init__(self, props=None, show=None, models=None, status=200):
        self.props, self.show, self.models, self.status = props, show, models, status
        self.closed = False
        self.calls: list[str] = []

    def is_closed(self):
        return self.closed

    async def get(self, url, **kw):
        self.calls.append(f"GET {url}")
        if url.endswith("/props"):
            return _Resp(self.props or {}, self.status)
        if url.endswith("/models"):
            return _Resp(self.models or {"data": []}, self.status)
        return _Resp({}, 404)

    async def post(self, url, json=None, **kw):
        self.calls.append(f"POST {url}")
        if url.endswith("/api/show"):
            return _Resp(self.show or {}, self.status)
        return _Resp({}, 404)


def _use(monkeypatch, fake):
    monkeypatch.setattr(llm, "_get_client", lambda: fake)


# ── probe_context: где какой провайдер прячет n_ctx ──────────────────────

def test_probe_llamacpp_props(monkeypatch):
    _use(monkeypatch, _FakeHTTP(props={"default_generation_settings": {"n_ctx": 8192}}))
    out = asyncio.run(llm.probe_context(
        {"id": "llamacpp", "base_url": "http://127.0.0.1:8080/v1"}))
    assert out == {"max_context": 8192, "source": "llamacpp:/props"}


def test_probe_llamacpp_props_top_level_n_ctx(monkeypatch):
    _use(monkeypatch, _FakeHTTP(props={"n_ctx": 4096}))
    out = asyncio.run(llm.probe_context(
        {"id": "llamacpp", "base_url": "http://127.0.0.1:8080/v1"}))
    assert out and out["max_context"] == 4096


def test_probe_ollama_api_show(monkeypatch):
    _use(monkeypatch, _FakeHTTP(show={"model_info": {"llama.general.context_length": 32768}}))
    out = asyncio.run(llm.probe_context(
        {"id": "ollama", "base_url": "http://127.0.0.1:11434/v1", "model": "qwen"}))
    assert out == {"max_context": 32768, "source": "ollama:/api/show"}


def test_probe_openai_models_matches_our_model(monkeypatch):
    """Облачный формат (RouterAI и др.): context_length у нужной записи каталога."""
    _use(monkeypatch, _FakeHTTP(models={"data": [
        {"id": "other/model", "context_length": 4096},
        {"id": "upstage/solar-pro4", "context_length": 262144},
    ]}))
    out = asyncio.run(llm.probe_context(
        {"id": "openai_compat", "base_url": "https://x/api/v1", "model": "upstage/solar-pro4"}))
    assert out == {"max_context": 262144, "source": "openai:/models"}


def test_probe_single_entry_catalog(monkeypatch):
    _use(monkeypatch, _FakeHTTP(models={"data": [{"id": "anything", "context_length": 16384}]}))
    out = asyncio.run(llm.probe_context({"id": "openai_compat", "base_url": "https://x/api/v1"}))
    assert out and out["max_context"] == 16384


def test_probe_silent_when_unreachable_or_disabled(monkeypatch):
    _use(monkeypatch, _FakeHTTP(models={}, status=500))
    run = asyncio.run
    assert run(llm.probe_context({"id": "openai_compat", "base_url": "https://x/api/v1"})) is None
    assert run(llm.probe_context({"id": "none", "base_url": ""})) is None
    assert run(llm.probe_context(None)) is None


def test_probe_is_cached(monkeypatch):
    fake = _FakeHTTP(models={"data": [{"id": "m", "context_length": 8192}]})
    _use(monkeypatch, fake)
    prov = {"id": "openai_compat", "base_url": "https://x/api/v1", "model": "m"}
    asyncio.run(llm.probe_context(prov))
    asyncio.run(llm.probe_context(prov))
    assert len(fake.calls) == 1, "второй вызов обязан взяться из кэша"
    assert llm.probe_context_cached(prov) == {"max_context": 8192, "source": "openai:/models"}


# ── _context_guard: не даём миру заявить окно больше модели ──────────────

def _guard(providers, gen, probe_result):
    from backend.routers import core

    async def fake_probe(p):
        return probe_result

    orig = llm.probe_context
    llm.probe_context = fake_probe
    try:
        return asyncio.run(core._context_guard(1, providers, gen))
    finally:
        llm.probe_context = orig


def test_context_guard_lowers_context_to_model_limit(fake_config):
    fake_config(detect_model_context=True)
    g = _guard({"main": {"id": "llamacpp"}}, {"context_tokens": 32768, "max_tokens": 2000},
               {"max_context": 8192, "source": "llamacpp:/props"})
    assert 0 < g["context_tokens"] < 8192
    assert g["context_limit"] == 8192
    assert g["context_limit_source"] == "llamacpp:/props"


def test_context_guard_keeps_small_context(fake_config):
    fake_config(detect_model_context=True)
    g = _guard({"main": {}}, {"context_tokens": 8192},
               {"max_context": 32768, "source": "openai:/models"})
    assert g["context_tokens"] == 8192          # не занижаем без нужды
    assert g["context_limit"] == 32768


def test_context_guard_noop_when_limit_unknown(fake_config):
    """Лимит не известен → НЕ додумываем за пользователя (не режем окно втайне)."""
    fake_config(detect_model_context=True)
    g = _guard({"main": {}}, {"context_tokens": 262144}, None)
    assert g["context_tokens"] == 262144
    assert "context_limit" not in g


def test_context_guard_survives_probe_exception(fake_config):
    from backend.routers import core

    async def boom(p):
        raise RuntimeError("сеть легла")

    fake_config(detect_model_context=True)
    orig = llm.probe_context
    llm.probe_context = boom
    try:
        g = asyncio.run(core._context_guard(1, {"main": {}}, {"context_tokens": 32768}))
    finally:
        llm.probe_context = orig
    assert g == {"context_tokens": 32768}


def test_context_guard_respects_kill_switch(fake_config):
    from backend.routers import core

    async def never(p):
        raise AssertionError("не должен вызываться при DETECT_MODEL_CONTEXT=false")

    fake_config(detect_model_context=False)
    orig = llm.probe_context
    llm.probe_context = never
    try:
        g = asyncio.run(core._context_guard(1, {"main": {}}, {"context_tokens": 32768}))
    finally:
        llm.probe_context = orig
    assert g == {"context_tokens": 32768}


def test_create_world_and_settings_apply_limit(api_client, monkeypatch, fake_config):
    """UI получает context_limit, а ползунок «Размер контекста» не может выйти за n_ctx."""
    client, _holder = api_client
    from backend import narrator as narrator_mod
    from backend.routers import core

    fake_config(detect_model_context=True)

    async def fake_probe(p):
        return {"max_context": 8192, "source": "llamacpp:/props"}

    monkeypatch.setattr(llm, "probe_context", fake_probe)
    monkeypatch.setattr(core, "llm", llm)

    r = client.post("/api/worlds", json={"theme_id": narrator_mod.THEMES[0]["id"],
                                         "name": "Контекст-мир"})
    assert r.status_code == 200, r.text
    wid = r.json()["world_id"]
    gs = client.get(f"/api/worlds/{wid}").json()["gen_settings"]
    assert gs["context_limit"] == 8192
    assert gs["context_tokens"] == int(8192 * 0.95)

    # поднимаем контекст в настройках — снова снижается до лимита модели (не 500)
    r2 = client.post(f"/api/worlds/{wid}/settings", json={"context_tokens": 200000})
    assert r2.status_code == 200, r2.text
    assert r2.json()["gen_settings"]["context_tokens"] == int(8192 * 0.95)


# ── метрики: журнал переживает рестарт ───────────────────────────────────

def test_metrics_journal_roundtrip(tmp_path, monkeypatch, fake_config):
    target = tmp_path / "m.jsonl"
    fake_config(metrics_persist=True, metrics_file=str(target))
    metrics.record(llm_ms=100, completion_tokens=5, prompt_tokens=10, provider="p", world_id=1)
    metrics.record(llm_ms=300, completion_tokens=7, prompt_tokens=12, provider="p", world_id=1)
    lines = target.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["llm_ms"] == 100

    # «рестарт»: in-memory буфер пуст — отчёт достраивается из журнала
    metrics._samples.clear()
    rep = metrics.as_json()
    assert rep["restored_from_journal"] is True
    assert rep["samples_total"] == 2
    assert rep["windows"]["all"]["n"] == 2
    assert rep["windows"]["all"]["llm_ms_max"] == 300.0


def test_metrics_journal_ignores_corrupt_lines(tmp_path, fake_config):
    p = tmp_path / "m.jsonl"
    p.write_text('{"llm_ms": 1}\nНЕ-JSON\n\n{"llm_ms": 2}\n', encoding="utf-8")
    fake_config(metrics_persist=True, metrics_file=str(p), metrics_tail=100)
    assert [r["llm_ms"] for r in metrics.read_journal()] == [1, 2]


def test_metrics_persist_disabled_writes_nothing(tmp_path, fake_config):
    """METRICS_PERSIST=false — журнал не создаётся (так стоят тесты: не гадили в data/)."""
    target = tmp_path / "m.jsonl"
    fake_config(metrics_persist=False, metrics_file=str(target))
    metrics.record(llm_ms=1)
    assert not target.exists()
    assert len(metrics._samples) == 1


def test_metrics_journal_unwritable_does_not_raise(tmp_path, monkeypatch, fake_config):
    """Недоступный путь к журналу не роняет ход — только warning в лог.
    Родителем делаем ОБЫКНОВЕННЫЙ файл: mkdir гарантированно не проходит."""
    blocker = tmp_path / "blocker"
    blocker.write_text("не папка", encoding="utf-8")
    bad = blocker / "m.jsonl"
    fake_config(metrics_persist=True, metrics_file=str(bad))
    metrics.record(llm_ms=1)                      # не бросает
    assert len(metrics._samples) == 1             # in-memory метрика всё равно записана
    monkeypatch.setattr(metrics, "_file_path", lambda: bad)
    assert metrics.read_journal() == []


def test_metrics_env_kill_switch_in_tests():
    """conftest обязан держать персист/бэкап выключенными — иначе тесты пишут в data/."""
    import os
    assert os.environ.get("METRICS_PERSIST") == "false"
    assert os.environ.get("BACKUP_DB_ON_START") == "false"
    assert os.environ.get("DETECT_MODEL_CONTEXT") == "false"


# ── новые переключатели доехали до админки (README обещает «все параметры в UI») ──

def test_admin_reset_uses_key_list_not_bool_false(api_client, monkeypatch):
    """Регресс на баг сессии 33: кнопка «Сбросить к .env» раньше писала `false` во все
    тумблеры и вместо возврата к .env ВЫКЛЮЧАЛА судью логики, автономного мастера,
    боевой ИИ, события и озвучку. Правильный механизм — список `reset`."""
    from backend import db
    from backend.config import get_config, invalidate_config

    client, _holder = api_client
    for k in ("LOGIC_JUDGE_ENABLED", "AUTONOMOUS_MASTER_ENABLED", "ENEMY_AI_ENABLED",
              "DYNAMIC_EVENTS_ENABLED", "TTS_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    try:
        assert client.post("/api/admin/settings", json={
            "logic_judge_enabled": True, "autonomous_master_enabled": True,
            "enemy_ai_enabled": True, "dynamic_events_enabled": True,
            "tts_enabled": True}).status_code == 200
        invalidate_config()
        assert client.post("/api/admin/settings", json={"reset": [
            "LOGIC_JUDGE_ENABLED", "AUTONOMOUS_MASTER_ENABLED", "ENEMY_AI_ENABLED",
            "DYNAMIC_EVENTS_ENABLED", "TTS_ENABLED"]}).status_code == 200
        invalidate_config()
        st = db.get_admin_settings()
        for key in ("LOGIC_JUDGE_ENABLED", "AUTONOMOUS_MASTER_ENABLED", "ENEMY_AI_ENABLED",
                    "DYNAMIC_EVENTS_ENABLED", "TTS_ENABLED"):
            assert st.get(key, "").strip() == "", f"{key} обязан удалиться, а не стать false"
        cfg = get_config()
        assert cfg.logic_judge_enabled and cfg.autonomous_master_enabled
        assert cfg.enemy_ai_enabled and cfg.dynamic_events_enabled and cfg.tts_enabled
    finally:
        invalidate_config()


def test_admin_settings_expose_persist_section(api_client):
    client, _holder = api_client
    r = client.get("/api/admin/settings")
    assert r.status_code == 200
    eff = r.json()["effective"]
    assert "persist" in eff, "секция переносимости/наблюдаемости должна быть в отдаче админки"
    for k in ("detect_model_context", "backup_db_on_start", "backup_keep",
              "metrics_persist", "metrics_tail"):
        assert k in eff["persist"], k


def test_admin_persist_toggles_roundtrip(api_client, monkeypatch):
    """Новые переключатели реально применяются через админку и сбрасываются к .env.
    env процесса имеет высший приоритет (conftest выключает персист/бэкап), поэтому
    на время теста снимаем эти три переменные."""
    from backend import db
    from backend import config as config_mod
    from backend.config import get_config, invalidate_config

    client, _holder = api_client
    for k in ("METRICS_PERSIST", "BACKUP_DB_ON_START", "DETECT_MODEL_CONTEXT"):
        monkeypatch.delenv(k, raising=False)
    try:
        r = client.post("/api/admin/settings", json={"metrics_persist": False,
                                                    "detect_model_context": False,
                                                    "backup_db_on_start": False,
                                                    "backup_keep": "5"})
        assert r.status_code == 200, r.text
        invalidate_config()
        cfg = get_config()
        assert cfg.metrics_persist is False and cfg.detect_model_context is False
        assert cfg.backup_db_on_start is False and cfg.backup_keep == 5
        stored = db.get_admin_settings()
        assert stored.get("BACKUP_KEEP") == "5"
        # сброс через явный `reset` (буле не умеет пустое значение) → возврат к .env/дефолтам кода
        assert client.post("/api/admin/settings",
                           json={"reset": ["METRICS_PERSIST", "BACKUP_DB_ON_START",
                                           "DETECT_MODEL_CONTEXT", "BACKUP_KEEP"]}).status_code == 200
        invalidate_config()
        cfg2 = get_config()
        assert cfg2.metrics_persist is True and cfg2.detect_model_context is True
        assert cfg2.backup_db_on_start is True and cfg2.backup_keep == 10
    finally:
        invalidate_config()
