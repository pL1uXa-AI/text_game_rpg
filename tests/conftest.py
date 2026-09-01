# -*- coding: utf-8 -*-
"""Общие фикстуры pytest: изолированная среда (отдельная БД, без сети) + тест-клиент."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── Изоляция: ДО импорта backend ────────────────────────────────────────
# Отдельная БД (боевую data/game.db НЕ трогаем), TTS/события/облако выключены.
_TMP = tempfile.mkdtemp(prefix="textgame-pytest-")
os.environ.setdefault("DB_PATH", str(Path(_TMP) / "test.db"))
# Не писать в рабочую папку проекта: бэкапы БД и журнал метрик в тестах выключены
# (иначе каждый прогон гоняет файлы в data/).
os.environ["BACKUP_DB_ON_START"] = "false"
os.environ["METRICS_PERSIST"] = "false"
os.environ["DETECT_MODEL_CONTEXT"] = "false"
# Лог тестов — в temp-папку прогона, а не в боевой data/logs/game.log (иначе pytest
# забивает рабочий журнал диагностики мусором заглушенных вызовов).
os.environ["LOG_FILE"] = str(Path(_TMP) / "test-game.log")
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["TTS_ENABLED"] = "false"
os.environ["DYNAMIC_EVENTS_ENABLED"] = "false"
os.environ["EMBEDDING_PROVIDER"] = "none"
os.environ["RERANK_PROVIDER"] = "none"
os.environ["RERANK_ENABLED"] = "false"
# Rate-limit в тестах выключен: тестовый клиент гоняет десятки действий с одного IP
# (127.0.0.1) за секунды — это легитимная нагрузка теста, а не «залипание» UI.
import backend.ratelimit as _rl_mod  # noqa: E402
_rl_mod.configure(enabled=False)

from fastapi.testclient import TestClient  # noqa: E402

from backend import chroma_client as chroma_mod  # noqa: E402
from backend import llm as llm_mod  # noqa: E402
from backend import narrator as narrator_mod  # noqa: E402
from backend.app import app  # noqa: E402


@pytest.fixture
def api_client(monkeypatch):
    """TestClient приложения с герметичными заглушками (LLM/Chroma/память — без сети).

    Возвращает (client, holder): holder["reply"] — текст, который «вернёт LLM».
    Если reply содержит <<ENGINE>>{...} — механика применится как в бою.
    """
    holder: dict = {"reply": "Ты осматриваешься. Вокруг тихо."}
    async def _noop(*a, **kw):
        return None
    async def _noop_list(*a, **kw):
        return []
    async def _available(*a, **kw):
        return True
    async def _opening(*a, **kw):
        return "Ты просыпаешься в старой таверне. Половицы скрипят."
    async def _complete(*a, **kw):
        return holder["reply"]

    async def _stream(*a, **kw):
        # стрим режет reply на куски — движок SSE должен их склеить и применить механики
        reply = holder["reply"]
        for _i in range(0, max(1, len(reply)), 37):
            yield reply[_i:_i + 37]
        if not reply:
            yield " "

    # LLM
    monkeypatch.setattr(llm_mod, "check_available", _available)
    monkeypatch.setattr(llm_mod, "complete", _complete)
    monkeypatch.setattr(llm_mod, "stream_chat", _stream)

    class _FakeResp:
        status_code = 200

    class _FakeClient:
        async def get(self, *a, **kw):
            return _FakeResp()

    monkeypatch.setattr(llm_mod, "_get_client", lambda: _FakeClient())
    # Память/карточки/события (без Chroma и без облака)
    monkeypatch.setattr(narrator_mod, "retrieve_memory", _noop_list)
    monkeypatch.setattr(narrator_mod, "generate_opening", _opening)
    monkeypatch.setattr(narrator_mod, "generate_suggestions", _noop_list)
    monkeypatch.setattr(narrator_mod, "index_exchange", _noop)
    monkeypatch.setattr(narrator_mod, "summarize_and_compress", _noop)
    monkeypatch.setattr(narrator_mod, "update_entity_cards", _noop)
    monkeypatch.setattr(narrator_mod, "ensure_knowledge_cards", lambda *a, **kw: [])
    monkeypatch.setattr(narrator_mod, "index_entities", _noop)
    monkeypatch.setattr(narrator_mod, "generate_dynamic_event", _noop)
    # Chroma-клиент
    for _name in ("ping", "count", "query", "add", "upsert", "delete_by_where", "delete_by_ids"):
        if hasattr(chroma_mod, _name):
            monkeypatch.setattr(chroma_mod, _name, _noop_list if _name in ("query", "count") else _noop)
    return TestClient(app), holder


def create_world_payload(theme: str = "") -> dict:
    """Минимальный payload создания мира. По умолчанию — первый сюжет из plots/ (system)."""
    if not theme:
        theme = narrator_mod.THEMES[0]["id"]
    return {"theme_id": theme, "name": "Тестовый мир", "difficulty": "normal",
            "perspective": "second", "language": "ru"}


@pytest.fixture
def fake_config(monkeypatch):
    """Подменить кэшированный Config, сохранив остальные поля реального конфига.

    config.get_config() читает config._cache["cfg"] — подменяем словарь кэша целиком
    и только на время теста (monkeypatch вернёт прежний кэш автоматически).
    Наследуемся от реального класса Config, чтобы методы (resolve_world_providers,
    get_provider) продолжали работать."""
    import backend.config as config_mod

    def _set(**kw):
        real = config_mod.get_config()

        class _C(type(real)):
            pass

        cfg = _C()
        vals = dict(getattr(real, "__dict__", {}) or {})
        vals.update(kw)
        for k, v in vals.items():
            setattr(cfg, k, v)
        monkeypatch.setattr(config_mod, "_cache", {"cfg": cfg})  # undo вернёт старый кэш
        return cfg

    return _set