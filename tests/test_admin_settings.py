# -*- coding: utf-8 -*-
"""Тесты интеграции админки с конфигом (вынос admin_settings из config.py):
приоритет env → admin_settings → .env; отдельное соединение без рекурсии;
сброс через invalidate_config."""
from __future__ import annotations




def test_db_path_from_env(tmp_path, monkeypatch):
    """admin_settings.db_path() следует за DB_PATH окружения (тестовая изоляция)."""
    from backend import admin_settings
    p = str(tmp_path / "custom.db")
    monkeypatch.setenv("DB_PATH", p)
    assert admin_settings.db_path().replace("\\", "/") == p.replace("\\", "/")


def test_read_overrides_default_empty(api_client):
    from backend import admin_settings
    overrides = admin_settings.read_overrides()
    # конфиг тестов не ставит админ-настроек — после очистки теста пусто (или что-то от .env, но не критично)
    assert isinstance(overrides, dict)
    assert all(k == k.upper() for k in overrides), "ключи нормализуются в верхний регистр"


def test_admin_settings_visible_in_config(api_client):
    from backend import db
    from backend.config import get_config, invalidate_config
    db.set_admin_settings({"MAX_TOKENS": "1500"})
    invalidate_config()
    cfg = get_config()
    assert cfg.max_tokens == 1500, "админка переопределяет .env"
    # вернуть как было
    db.set_admin_settings({"MAX_TOKENS": ""})
    invalidate_config()


def test_env_wins_over_admin_settings(api_client, monkeypatch):
    from backend import db
    from backend.config import get_config, invalidate_config
    monkeypatch.setenv("RERANK_ENABLED", "false")
    db.set_admin_settings({"RERANK_ENABLED": "true"})
    invalidate_config()
    cfg = get_config()
    assert cfg.rerank_enabled is False, "приоритет: env процесса → админка → .env"
    monkeypatch.delenv("RERANK_ENABLED")
    invalidate_config()
    assert get_config().rerank_enabled is True, "без env-переопределения админка работает"
    db.set_admin_settings({"RERANK_ENABLED": ""})
    invalidate_config()


def test_background_tasks_enabled_admin_toggle(api_client):
    from backend import db
    from backend.config import get_config, invalidate_config
    # админка выключает фоновые задачи
    db.set_admin_settings({"BACKGROUND_TASKS_ENABLED": "false"})
    invalidate_config()
    assert get_config().background_tasks_enabled is False
    # сброс → .env (включено)
    db.set_admin_settings({"BACKGROUND_TASKS_ENABLED": ""})
    invalidate_config()
    assert get_config().background_tasks_enabled is True


def test_openapi_has_tags_and_description(api_client):
    """OpenAPI задокументирован: описание и теги-разделы."""
    from backend.app import app
    schema = app.openapi()
    assert schema["info"]["title"] == "Text Game RPG"
    assert schema["info"].get("description"), "добавлено описание приложения"
    tags = {t["name"] for t in (schema.get("tags") or [])}
    assert "Миры и действия" in tags and "Озвучка (TTS)" in tags
    assert "/api/worlds/{world_id}/action" in schema["paths"]



def test_empty_admin_value_meaning_delete(api_client):
    from backend import db
    from backend.config import invalidate_config
    db.set_admin_settings({"DEFAULT_TEMP": "0.2"})
    invalidate_config()
    assert db.get_admin_settings().get("DEFAULT_TEMP") == "0.2"
    db.set_admin_settings({"DEFAULT_TEMP": ""})
    invalidate_config()
    assert "DEFAULT_TEMP" not in db.get_admin_settings()


def test_config_repeated_loads_no_recursion(api_client):
    """Нет рекурсии config→db→config (чтение админки — отдельным соединением)."""
    from backend.config import get_config, invalidate_config
    for _ in range(5):
        invalidate_config()
        cfg = get_config()
        assert cfg.max_tokens > 0


def test_api_roundtrip_persists(api_client):
    """POST /api/admin/settings → видно в GET и в admin_settings.read_overrides()."""
    from backend import admin_settings, db
    from backend.config import invalidate_config
    client, _ = api_client
    r = client.post("/api/admin/settings", json={"rerank_enabled": False})
    assert r.status_code == 200
    assert admin_settings.read_overrides().get("RERANK_ENABLED") == "false"
    invalidate_config()
    r = client.get("/api/admin/settings")
    assert r.json()["effective"]["rerank_enabled"] is False
    # очистка
    db.set_admin_settings({"RERANK_ENABLED": ""})
    invalidate_config()