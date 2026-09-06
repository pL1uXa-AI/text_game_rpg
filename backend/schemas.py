# -*- coding: utf-8 -*-
"""Pydantic-схемы запросов API (вынесены из app.py при рефакторинге на роутеры)."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

# ══════════════════════════════════════════════════════════════
# Границы размера пользовательских текстов (B3, аудит 41)
# ══════════════════════════════════════════════════════════════
# Раньше в схемах не было НИ ОДНОЙ границы: `POST /api/narrators` с 5 МБ персоны или
# `POST /api/worlds` с гигантским лором проходили насквозь, оседали в SQLite и затем
# уходили в промпт КАЖДОГО хода мира (персона в `build_system_prompt` бюджета не имела
# вовсе; лор хотя бы резался `lore_token_budget`). Один запрос = испорченный мир.
# ONE source of truth — константы ниже (как реестр границ чисел в `config.NUM_RANGES`, A9).
# Числа выбраны «запас над реальностью», а не впритык: самые длинные штатные тексты —
# персона пресета 2.0 КБ, лорная статья 1.2 КБ, лор системного сюжета ~17 КБ, действие
# игрока по умолчанию 640 симв. (`config.max_action_chars`, настраивается в админке).
# Отказ — 422 с читаемым `detail` (в `app.py` висит обработчик: pydantic-список ошибок он
# превращает в одну строку) и `log.warning` там же; молчаливого обрезания «до лимита» нет.
NAME_MAX = 120            # имена миров/рассказчиков/сюжетов/слотов
DESC_MAX = 500            # короткое «описание для списка» (в базе самые длинные ~140)
LORE_TITLE_MAX = 200      # заголовок статьи лора
HOOK_MAX = 4000           # личный зацеп игрока (фронт и так режет до 240)
PERSONA_MAX = 20_000      # промпт-персона рассказчика
PLOT_MAX = 100_000        # текст своего сюжета
LORE_TEXT_MAX = 200_000   # пачка статей лора (custom_lore / plots.lore) и одна статья
LORE_TAG_MAX = 500
COMPLAINT_MAX = 2000      # воззвание к Провидению
ACTION_HARD_MAX = 100_000  # жёсткий потолок действия: мягкий лимит (`max_action_chars`) — в админке
TTS_TEXT_MAX = 2000
CARD_KIND_MAX = 60
CARD_KEY_MAX = 200
CARD_NAME_MAX = 200
CARD_SUMMARY_MAX = 4000
CARD_BIO_MAX = 20_000
URL_MAX = 500             # base_url провайдеров
MODEL_MAX = 200
API_KEY_MAX = 500


class ProviderIn(BaseModel):
    """Правка одного провайдера (per-world или при создании мира).

    B3: границы только у текстов-идентификаторов (адрес/модель/ключ), значения чисел —
    не наш фильтр: их смысл знает только провайдер."""
    id: Optional[str] = Field(default=None, max_length=NAME_MAX)
    base_url: Optional[str] = Field(default=None, max_length=URL_MAX)
    api_key: Optional[str] = Field(default=None, max_length=API_KEY_MAX)
    model: Optional[str] = Field(default=None, max_length=MODEL_MAX)
    enabled: Optional[bool] = None


class WorldCreate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=NAME_MAX)
    theme_id: str = Field(max_length=NAME_MAX)
    genres: Optional[list[str]] = None
    difficulty: str = "normal"
    perspective: str = "second"
    language: str = "ru"
    custom_hook: Optional[str] = Field(default="", max_length=HOOK_MAX)
    narrator_id: Optional[int] = None
    # кастомный сюжет: либо текст сюжета напрямую, либо id сохранённого сюжета (тема "custom")
    custom_plot: Optional[str] = Field(default=None, max_length=PLOT_MAX)
    plot_id: Optional[int] = None
    # опциональный лор мира (для своего сюжета): текст статей (## Заголовок → отдельная статья)
    custom_lore: Optional[str] = Field(default=None, max_length=LORE_TEXT_MAX)
    # модель и провайдеры при создании мира (пер-мир, как в настройках)
    provider_main: Optional[ProviderIn] = None
    provider_embedding: Optional[ProviderIn] = None
    provider_rerank: Optional[ProviderIn] = None


class ActionIn(BaseModel):
    # B3: жёсткий потолок тела запроса. Мягкий лимит действия живёт в конфите и проверяется
    # в `routers/core._ensure_action_len` (400 с человеческим текстом); здесь — страховка,
    # чтобы мегабайт не доезжал ни до БД, ни до валидатора.
    text: str = Field(max_length=ACTION_HARD_MAX)
    regenerate: Optional[bool] = False  # перегенерировать ответ на тот же ход (без нового события игрока)


class SaveIn(BaseModel):
    name: str = Field(max_length=NAME_MAX)


class RewindIn(BaseModel):
    """Перемотка мира к началу хода `seq` (сессия 34, C1): состояние из снапшота этого хода,
    более новые ходы убираются (mode="delete") или сокрыты (mode="hide")."""
    seq: int
    mode: str = Field(default="delete", max_length=NAME_MAX)


class DivineIn(BaseModel):
    """Воззвание к Провидению (Божественный арбитр): жалоба игрока на ошибку Рассказчика.
    Фронт режет поле до 500 символов, `COMPLAINT_MAX` — серверный потолок (API честнее UI)."""
    complaint: str = Field(max_length=COMPLAINT_MAX)


class FeedbackIn(BaseModel):
    value: int  # 1 | -1 | 0


class LoreIn(BaseModel):
    """Статья лора мира (библия вселенной): заголовок + большой текст.

    B3: `content` был единственным полем, которое роутер («режет только whitespace»,
    `routers/lore.py`) принимал любого размера — 5 МБ статьи оседали в SQLite и индексились
    в Chroma чанками на каждый ход."""
    title: str = Field(max_length=LORE_TITLE_MAX)
    content: str = Field(max_length=LORE_TEXT_MAX)
    tags: Optional[str] = Field(default="", max_length=LORE_TAG_MAX)
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
    """Глобальные настройки LLM/провайдеров/генерации/TTS (админка).
    None — не трогать; пустая строка — сбросить к .env; значение — задать.

    B3: границы добавлены только текстовым полям (адреса/модели/ключи/голос); числовые
    строки не режутся здесь — для них есть реестр диапазонов `config.NUM_RANGES` (A9),
    он даёт и кламп, и 400 с именем ключа."""
    main_provider: Optional[str] = Field(default=None, max_length=NAME_MAX)
    main_base_url: Optional[str] = Field(default=None, max_length=URL_MAX)
    main_api_key: Optional[str] = Field(default=None, max_length=API_KEY_MAX)
    main_model: Optional[str] = Field(default=None, max_length=MODEL_MAX)
    embedding_provider: Optional[str] = Field(default=None, max_length=NAME_MAX)
    embedding_base_url: Optional[str] = Field(default=None, max_length=URL_MAX)
    embedding_api_key: Optional[str] = Field(default=None, max_length=API_KEY_MAX)
    embedding_model: Optional[str] = Field(default=None, max_length=MODEL_MAX)
    rerank_provider: Optional[str] = Field(default=None, max_length=NAME_MAX)
    rerank_base_url: Optional[str] = Field(default=None, max_length=URL_MAX)
    rerank_api_key: Optional[str] = Field(default=None, max_length=API_KEY_MAX)
    rerank_model: Optional[str] = Field(default=None, max_length=MODEL_MAX)
    rerank_enabled: Optional[bool] = None
    # rerank_top_n удалён (A17): размер выдачи = RAG_MEMORY_K/LORE_RAG_K, вторая ручка избыточна
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
    # п.14b/п.16 (сессия 40): авто-часы мира и число повторов дописывания механики
    auto_time_enabled: Optional[bool] = None
    auto_time_every: Optional[str] = None
    mech_narrate_retries: Optional[str] = None
    divine_cooldown_turns: Optional[str] = None
    max_action_chars: Optional[str] = None
    # наблюдаемость и устойчивость (сессия 38, B5): раньше README обещал, что админка
    # покрывает «все ключевые параметры», а эти задавались только .env. Все они влияют на
    # расход токенов или на наблюдаемость, поэтому имеют смысл в UI между запусками.
    log_level: Optional[str] = None
    log_max_bytes: Optional[str] = None
    log_backup_count: Optional[str] = None
    log_prompt_dump: Optional[bool] = None
    llm_retries: Optional[str] = None
    llm_retry_backoff: Optional[str] = None
    llm_timeout: Optional[str] = None
    chroma_retries: Optional[str] = None
    embedding_retries: Optional[str] = None
    llm_bg_concurrency: Optional[str] = None
    llm_bg_max_queue: Optional[str] = None
    llm_bg_yield_turn: Optional[bool] = None
    prompt_tiers_enabled: Optional[bool] = None
    turn_snapshot_keep: Optional[str] = None
    # переносимость и наблюдаемость (сессия 33)
    detect_model_context: Optional[bool] = None
    backup_db_on_start: Optional[bool] = None
    backup_keep: Optional[str] = None
    metrics_persist: Optional[bool] = None
    metrics_tail: Optional[str] = None
    metrics_max_bytes: Optional[str] = None   # порог ротации журнала (0 = выкл.), сессия 36 п.7
    # Явный сброс конкретных ключей админки (имена env в верхнем регистре) → значения
    # возвращаются к .env. Нужно потому, что булев тумблер не может выразить «не задано»
    # (пустая строка в Optional[bool] = 422), а без этого кнопка «сбросить к .env»
    # фактически ВЫКЛЮЧАЛА все фоновые агенты и озвучку.
    reset: Optional[list[str]] = None
    # TTS (озвучка)
    tts_enabled: Optional[bool] = None
    tts_provider: Optional[str] = Field(default=None, max_length=NAME_MAX)
    tts_voice: Optional[str] = Field(default=None, max_length=NAME_MAX)
    tts_rate: Optional[str] = None
    tts_auto_play: Optional[bool] = None
    tts_cache_enabled: Optional[bool] = None
    tts_cache_ttl_days: Optional[str] = None
    tts_max_chars: Optional[str] = None


class TtsSettingsIn(BaseModel):
    """Per-мир настройки озвучки: пустая строка — «по умолчанию (глобально)»."""
    enabled: Optional[bool] = None
    provider: Optional[str] = Field(default=None, max_length=NAME_MAX)
    voice: Optional[str] = Field(default=None, max_length=NAME_MAX)
    rate: Optional[str] = Field(default=None, max_length=NAME_MAX)
    auto_play: Optional[bool] = None


class TtsTestIn(BaseModel):
    provider: Optional[str] = Field(default=None, max_length=NAME_MAX)
    voice: Optional[str] = Field(default=None, max_length=NAME_MAX)
    rate: Optional[str] = Field(default=None, max_length=NAME_MAX)
    text: Optional[str] = Field(default=None, max_length=TTS_TEXT_MAX)


class TtsDownloadIn(BaseModel):
    provider: str = Field(default="piper", max_length=NAME_MAX)
    voice: str = Field(default="", max_length=NAME_MAX)


class NarratorIn(BaseModel):
    """Персона рассказчика. `prompt` уходит в `build_system_prompt` без бюджета ярусов
    (это шапка промпта), поэтому его лимит — единственная защита от раздутого промпта на
    КАЖДЫЙ ход; страховка размера есть и там (`narrator.PERSONA_TOKEN_BUDGET`)."""
    name: str = Field(max_length=NAME_MAX)
    prompt: str = Field(max_length=PERSONA_MAX)
    desc: Optional[str] = Field(default="", max_length=DESC_MAX)


class PlotIn(BaseModel):
    name: str = Field(max_length=NAME_MAX)
    plot: str = Field(max_length=PLOT_MAX)
    lore: Optional[str] = Field(default="", max_length=LORE_TEXT_MAX)  # опциональный лор сюжета (станет лором мира при создании)


class PatchIn(BaseModel):
    patch: dict


class ImportIn(BaseModel):
    """Тело импорта мира: JSON-дамп, полученный из GET /api/worlds/{id}/export/json.
    Создаёт новый мир, существующие не трогает."""
    payload: dict


class EntityIn(BaseModel):
    """Карточка сущности/знаний (создание и правка из UI и из API).

    B3: поля карточки попадают в промпт ([КАРТОЧКИ СУЩНОСТЕЙ]) и в память — без границы
    одна «ла_NRKA»-карточка съедала бюджет контекста."""
    kind: str = Field(max_length=CARD_KIND_MAX)
    key: str = Field(max_length=CARD_KEY_MAX)
    name: Optional[str] = Field(default=None, max_length=CARD_NAME_MAX)
    summary: Optional[str] = Field(default=None, max_length=CARD_SUMMARY_MAX)
    relationship: Optional[str] = Field(default=None, max_length=CARD_SUMMARY_MAX)
    bio_add: Optional[str] = Field(default=None, max_length=CARD_BIO_MAX)
    meta: Optional[dict] = None