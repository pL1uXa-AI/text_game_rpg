# -*- coding: utf-8 -*-
"""Pydantic-схемы запросов API (вынесены из app.py при рефакторинге на роутеры)."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class ProviderIn(BaseModel):
    """Правка одного провайдера (per-world или при создании мира)."""
    id: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    enabled: Optional[bool] = None


class WorldCreate(BaseModel):
    name: Optional[str] = None
    theme_id: str
    genres: Optional[list[str]] = None
    difficulty: str = "normal"
    perspective: str = "second"
    language: str = "ru"
    custom_hook: Optional[str] = ""
    narrator_id: Optional[int] = None
    # кастомный сюжет: либо текст сюжета напрямую, либо id сохранённого сюжета (тема "custom")
    custom_plot: Optional[str] = None
    plot_id: Optional[int] = None
    # опциональный лор мира (для своего сюжета): текст статей (## Заголовок → отдельная статья)
    custom_lore: Optional[str] = None
    # модель и провайдеры при создании мира (пер-мир, как в настройках)
    provider_main: Optional[ProviderIn] = None
    provider_embedding: Optional[ProviderIn] = None
    provider_rerank: Optional[ProviderIn] = None


class ActionIn(BaseModel):
    text: str
    regenerate: Optional[bool] = False  # перегенерировать ответ на тот же ход (без нового события игрока)


class SaveIn(BaseModel):
    name: str


class RewindIn(BaseModel):
    """Перемотка мира к началу хода `seq` (сессия 34, C1): состояние из снапшота этого хода,
    более новые ходы убираются (mode="delete") или сокрыты (mode="hide")."""
    seq: int
    mode: str = "delete"


class DivineIn(BaseModel):
    """Воззвание к Провидению (Божественный арбитр): жалоба игрока на ошибку Рассказчика"""
    complaint: str


class FeedbackIn(BaseModel):
    value: int  # 1 | -1 | 0


class LoreIn(BaseModel):
    """Статья лора мира (библия вселенной): заголовок + большой текст."""
    title: str
    content: str
    tags: Optional[str] = ""
    is_core: Optional[bool] = False


class GenSettingsIn(BaseModel):
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    context_tokens: Optional[int] = None
    logic_judge: Optional[bool] = None
    narrator_id: Optional[int] = None
    # per-world память (0/пусто = авто от размера контекста)
    rag_memory_k: Optional[int] = None       # сколько фактов памяти вспоминать (0 = авто)
    lore_rag_k: Optional[int] = None         # сколько чанков лора (0 = авто)
    lore_token_budget: Optional[int] = None  # бюджет лора в токенах (0 = авто)


class ProvidersIn(BaseModel):
    main: Optional[ProviderIn] = None
    embedding: Optional[ProviderIn] = None
    rerank: Optional[ProviderIn] = None


class AdminSettingsIn(BaseModel):
    """Глобальные настройки LLM/провайдеров/генерации (админка).
    None — не трогать; пустая строка — сбросить к .env; значение — задать."""
    main_provider: Optional[str] = None
    main_base_url: Optional[str] = None
    main_api_key: Optional[str] = None
    main_model: Optional[str] = None
    embedding_provider: Optional[str] = None
    embedding_base_url: Optional[str] = None
    embedding_api_key: Optional[str] = None
    embedding_model: Optional[str] = None
    rerank_provider: Optional[str] = None
    rerank_base_url: Optional[str] = None
    rerank_api_key: Optional[str] = None
    rerank_model: Optional[str] = None
    rerank_enabled: Optional[bool] = None
    rerank_top_n: Optional[str] = None
    rerank_threshold: Optional[str] = None
    # фоновые задачи памяти (индексация RAG, карточки, сводки)
    background_tasks_enabled: Optional[bool] = None
    # гибридный поиск: вес BM25 в {0..1} (0 = только косинус Chroma, 1 = только BM25)
    hybrid_weight_bm25: Optional[str] = None
    default_temp: Optional[str] = None
    default_top_p: Optional[str] = None
    max_tokens: Optional[str] = None
    context_tokens: Optional[str] = None
    # память (сколько фактов/лора вспоминать)
    rag_memory_k: Optional[str] = None
    rag_memory_max: Optional[str] = None
    rag_candidates: Optional[str] = None
    recent_token_budget: Optional[str] = None
    summary_token_budget: Optional[str] = None
    lore_token_budget: Optional[str] = None
    lore_token_budget_max: Optional[str] = None
    lore_rag_k: Optional[str] = None
    lore_rag_k_max: Optional[str] = None
    cosine_threshold: Optional[str] = None
    cosine_threshold_local: Optional[str] = None
    # фоновые агенты и мир
    logic_judge_enabled: Optional[bool] = None
    logic_judge_interval: Optional[str] = None
    dynamic_events_enabled: Optional[bool] = None
    event_every_turns: Optional[str] = None
    autonomous_master_enabled: Optional[bool] = None
    autonomous_master_interval: Optional[str] = None
    enemy_ai_enabled: Optional[bool] = None
    enemy_ai_interval: Optional[str] = None
    tick_effects_enabled: Optional[bool] = None
    tick_needs_enabled: Optional[bool] = None
    divine_cooldown_turns: Optional[str] = None
    max_action_chars: Optional[str] = None
    # переносимость и наблюдаемость (сессия 33)
    detect_model_context: Optional[bool] = None
    backup_db_on_start: Optional[bool] = None
    backup_keep: Optional[str] = None
    metrics_persist: Optional[bool] = None
    metrics_tail: Optional[str] = None
    # Явный сброс конкретных ключей админки (имена env в верхнем регистре) → значения
    # возвращаются к .env. Нужно потому, что булев тумблер не может выразить «не задано»
    # (пустая строка в Optional[bool] = 422), а без этого кнопка «сбросить к .env»
    # фактически ВЫКЛЮЧАЛА все фоновые агенты и озвучку.
    reset: Optional[list[str]] = None
    # TTS (озвучка)
    tts_enabled: Optional[bool] = None
    tts_provider: Optional[str] = None
    tts_voice: Optional[str] = None
    tts_rate: Optional[str] = None
    tts_auto_play: Optional[bool] = None
    tts_cache_enabled: Optional[bool] = None
    tts_cache_ttl_days: Optional[str] = None
    tts_max_chars: Optional[str] = None


class TtsSettingsIn(BaseModel):
    """Per-мир настройки озвучки: пустая строка — «по умолчанию (глобально)»."""
    enabled: Optional[bool] = None
    provider: Optional[str] = None
    voice: Optional[str] = None
    rate: Optional[str] = None
    auto_play: Optional[bool] = None


class TtsTestIn(BaseModel):
    provider: Optional[str] = None
    voice: Optional[str] = None
    rate: Optional[str] = None
    text: Optional[str] = None


class TtsDownloadIn(BaseModel):
    provider: str = "piper"
    voice: str = ""


class NarratorIn(BaseModel):
    name: str
    prompt: str
    desc: Optional[str] = ""


class PlotIn(BaseModel):
    name: str
    plot: str
    lore: Optional[str] = ""  # опциональный лор сюжета (станет лором мира при создании)


class PatchIn(BaseModel):
    patch: dict


class ImportIn(BaseModel):
    """Тело импорта мира: JSON-дамп, полученный из GET /api/worlds/{id}/export/json.
    Создаёт новый мир, существующие не трогает."""
    payload: dict


class EntityIn(BaseModel):
    kind: str
    key: str
    name: Optional[str] = None
    summary: Optional[str] = None
    relationship: Optional[str] = None
    bio_add: Optional[str] = None
    meta: Optional[dict] = None