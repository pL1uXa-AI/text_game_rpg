# -*- coding: utf-8 -*-
"""Конфигурация игры: читает .env из корня проекта (переопределяется env процесса)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import admin_settings

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
_cache: dict = {}

# ══════════════════════════════════════════════════════
# Провайдеры: основная модель (main), эмбеддинги, реранкер
# ══════════════════════════════════════════════════════
PROVIDER_OPTIONS: dict[str, list[dict]] = {
    "main": [
        {"id": "llamacpp", "name": "llama.cpp — локальный (127.0.0.1:8080)", "needs_key": False, "needs_model": False},
        {"id": "ollama", "name": "Ollama — локальный (по умолчанию http://127.0.0.1:11434/v1)", "needs_key": False, "needs_model": True},
        {"id": "openai_compat", "name": "OpenAI-совместимый API (RouterAI и др.)", "needs_key": True, "needs_model": True},
    ],
    "embedding": [
        {"id": "routerai", "name": "RouterAI — qwen/qwen3-embedding-8b", "needs_key": True, "needs_model": False},
        {"id": "openai_compat", "name": "OpenAI-совместимый API (любой /embeddings)", "needs_key": True, "needs_model": True},
        {"id": "local", "name": "Локальный FastEmbed — оффлайн (multilingual-miniLM, ru+en)", "needs_key": False, "needs_model": False},
        {"id": "none", "name": "Выключено (RAG-память отключается)", "needs_key": False, "needs_model": False},
    ],
    "rerank": [
        {"id": "routerai", "name": "RouterAI — voyageai/rerank-2.5", "needs_key": True, "needs_model": False},
        {"id": "openai_compat", "name": "OpenAI-совместимый /rerank", "needs_key": True, "needs_model": True},
        {"id": "none", "name": "Выключено (реранкер отключён)", "needs_key": False, "needs_model": False},
    ],
    "tts": [
        {"id": "piper", "name": "Piper — локальный (sherpa-onnx, русские голоса)", "needs_key": False, "needs_model": False},
        {"id": "kokoro", "name": "Kokoro-82M — локальный (без русского, для en-миров)", "needs_key": False, "needs_model": False},
        {"id": "edge", "name": "Edge — облачный Microsoft TTS (лучшие рус. голоса, бесплатно)", "needs_key": False, "needs_model": False},
        {"id": "none", "name": "Выключено (озвучка отключена)", "needs_key": False, "needs_model": False},
    ],
}

DEFAULT_PROVIDER_ID: dict[str, str] = {"main": "llamacpp", "embedding": "routerai", "rerank": "routerai", "tts": "edge"}

# маска для секретов при выводе в UI/логи
KEY_MASK = "••••••••"


@dataclass
class Config:
    # API
    game_host: str = "127.0.0.1"
    game_port: int = 8002
    db_path: str = str(ROOT / "data" / "game.db")

    # ── Провайдер основной модели ──
    main_provider: str = "llamacpp"          # llamacpp | ollama | openai_compat
    main_base_url: str = ""                  # переопределение (ollama/openai_compat)
    main_api_key: str = ""                   # для openai_compat
    main_model: str = ""                     # для ollama/openai_compat

    # llama.cpp (порт 8080) — базовый адрес по умолчанию для main
    llama_base_url: str = "http://127.0.0.1:8080/v1"
    llama_model: str = ""                    # пусто — сервер сам выбирает загруженную модель

    # ── Провайдер эмбеддингов ──
    embedding_provider: str = "routerai"     # routerai | openai_compat | local | none
    embedding_base_url: str = "https://routerai.ru/api/v1"
    embedding_api_key: str = ""
    embedding_model: str = "qwen/qwen3-embedding-8b"
    embedding_dim: int = 4096
    prefix_doc: str = "Passage: "
    prefix_query: str = "Query: "

    # Локальный эмбеддинг (fastembed) — оффлайн-фолбэк "local"
    local_embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    local_embedding_dim: int = 384
    # Папка кэша fastembed (модели скачиваются один раз; стабильный путь под корнем игры)
    local_embedding_cache: str = "data/fastembed"

    # ── Провайдер реранкера ──
    rerank_provider: str = "routerai"        # routerai | openai_compat | none
    rerank_base_url: str = ""
    rerank_api_key: str = ""
    rerank_model: str = "voyageai/rerank-2.5"
    rerank_enabled: bool = True
    rerank_top_n: int = 20
    rerank_threshold: float = 0.2

    # Гибрид BM25 + косинус
    hybrid_weight_bm25: float = 0.4
    cosine_threshold: float = 0.35
    # Порог для локальных эмбеддингов (fastembed даёт разрежённые близости) — ниже
    cosine_threshold_local: float = 0.20

    # Генерация
    default_temp: float = 0.8
    default_top_p: float = 0.95
    max_tokens: int = 2000
    context_tokens: int = 32768  # стандарт 32k: размер контекста нового мира (память считается от него)

    # Память
    recent_token_budget: int = 2600
    summary_token_budget: int = 700
    rag_memory_k: int = 4
    rag_memory_max: int = 24          # потолок RAG-фактов при большом контексте (динамика от context_tokens)
    rag_candidates: int = 20

    # Лор мира (библия вселенной): сколько токенов лора попадает в промпт
    lore_token_budget: int = 900      # суммарный бюджет блока [ЛОР МИРА] в токенах
    lore_token_budget_max: int = 4500 # потолок бюджета лора при большом контексте (динамика)
    lore_rag_k: int = 3               # релевантных статей/чанков из RAG-поиска по лору
    lore_rag_k_max: int = 12          # потолок лор-чанков при большом контексте (динамика)

    # Динамические события мира (фоновая генерация без действия игрока)
    dynamic_events_enabled: bool = True
    event_every_turns: int = 10       # каждые N действий игрока (таймера по времени НЕТ)
    max_action_chars: int = 640       # макс. длина действия игрока (сил. ≈ 200 токенов при len/3.2)

    # Глобальный выключатель фоновых задач (память/RAG, карточки сущностей). Фоновые задачи
    # не блокируют ответ игроку, но тратят токены облака. Выключение = быстрее/дешевле, но
    # память деградирует (никаких сводок/карточек/индексации). Динамические события и судья
    # логики управляются своими флагами (dynamic_events_enabled / logic_judge_enabled).
    background_tasks_enabled: bool = True

    # Судья логики (фоновая проверка противоречий): after each turn, асинхронный LLM-проход
    # сверяет ответ рассказчика с критичными фактами мира (живые NPC, флаги). Ошибку не
    # переписывает, а оформляет системным сообщением-«искажением реальности» (сюжетный поворот).
    logic_judge_enabled: bool = True  # глобальный для судьи логики (per-world поверх, gen_settings.logic_judge)
    logic_judge_interval: int = 3     # проверять не чаще чем раз в N ходов (экономия токенов)

    # Провидение (Божественный арбитр) — ручное воззвание игроком, когда он считает, что
    # рассказчик ошибся (не выдал предмет, не списал урон и т.п.). Модель проверяет логику
    # и правит мир директивой. Это «корректор мира»: НЕ ограничен ходами — игрок может
    # воззвать при любой неточности, а не ждать N ходов (защита от злоупотребления
    # лежит в самих ответах Провидения — decline без настоящей ошибки, а не в ожидании).
    divine_cooldown_turns: int = 0    # минимум ходов между воззваниями (0 = без кулдауна)
    tick_effects_enabled: bool = True  # авто-тик статус-эффектов в начале хода («физический движок»)
    tick_needs_enabled: bool = True    # авто-тик потребностей/рассудка (голод/усталость/жажда/рассудок/стресс/мораль)

    # ── Автономный «мастер» (сессия 25) ──
    # Фоновый сюжетный агент: когда игрок «застрял» (повторяет одно действие / без активных
    # квестов), генерирует квест или сюжетный поворот, чтобы вывести его из тупика. Работает
    # по правилу фоновых задач — не блокирует ответ игроку, глобально выключается этим флагом.
    autonomous_master_enabled: bool = True
    autonomous_master_interval: int = 6   # не чаще раза в N ходов игрока (экономия токенов)

    # ── Боевой ИИ врагов (сессия 26) ──
    # Фоновый тактический агент: пока в бою есть живые враги, решает их ход — преследование/охрана,
    # отступление (enemy_remove), переговоры (flag/npc), ловушка (flag). НЕ наносит урон игроку напрямую
    # (это делает рассказчик в своём ходе) — выбирает стратегию. Редко, по интервалу ходов, не блокирует ответ.
    enemy_ai_enabled: bool = True
    enemy_ai_interval: int = 2           # раз в N ходов игрока, пока есть живые враги (экономия токенов)

    # ── Переносимость и наблюдаемость (сессия 33) ──
    # Бэкап файла БД при старте: data/game.db — единственное место, где живут миры,
    # события, карточки и лор (в git он не лежит). Снимок делается SQLite Backup API
    # в data/backups/ и старые срезаются по количеству.
    backup_db_on_start: bool = True
    backup_keep: int = 10
    # Метрики ходов переживают перезапуск: каждый ход дописывается в data/metrics.jsonl
    # (in-memory кольцевой буфер остаётся быстрым окном для дашборда).
    metrics_persist: bool = True
    metrics_file: str = "data/metrics.jsonl"
    metrics_tail: int = 500     # сколько последних строк читать из файла для отчёта
    # Авто-подгонка размера контекста мира под реальный n_ctx модели (защита от обрезов
    # на локальной llama.cpp 8192 при стандарте .env 32768).
    detect_model_context: bool = True

    # ── Озвучка (TTS): piper | kokoro | edge | none ──
    tts_enabled: bool = True          # глобальный включатель озвучки (per-world поверх)
    tts_provider: str = "edge"
    tts_voice: str = "ru-RU-DmitryNeural"  # Edge, мужской голос (стандарт)
    tts_rate: str = "+0%"             # скорость речи: +20% / -10% и т.п.
    tts_auto_play: bool = False       # автоматически проигрывать новый ответ
    tts_cache_enabled: bool = True
    tts_cache_ttl_days: int = 60      # срок жизни кэша аудио (дни, 0 = вечно)
    tts_max_chars: int = 2400         # максимум символов для синтеза за один ответ

    # ChromaDB
    chroma_host: str = "127.0.0.1"
    chroma_port: int = 8001
    chroma_collection: str = "text_game_memory"

    # ──────────────────────────────────────────────
    # Провайдеры: резолв настроек
    # ──────────────────────────────────────────────
    def provider_default(self, kind: str) -> str:
        return DEFAULT_PROVIDER_ID.get(kind, "none")

    def _option(self, kind: str, pid: str) -> dict:
        return next((o for o in PROVIDER_OPTIONS.get(kind, []) if o["id"] == pid), {})

    def get_provider(self, kind: str, overrides: dict | None = None) -> dict:
        """Эффективные настройки провайдера для категории kind.
        overrides — per-world переопределения {"id", "base_url", "api_key", "model"}.
        Возвращает готовый dict для клиентов (llm/embeddings)."""
        overrides = overrides or {}
        pid = overrides.get("id") or ({
            "main": self.main_provider,
            "embedding": self.embedding_provider,
            "rerank": self.rerank_provider,
        }.get(kind) or self.provider_default(kind))
        opt = self._option(kind, pid)
        if not opt:  # неизвестный id — откат на дефолт
            pid = self.provider_default(kind)
            opt = self._option(kind, pid)

        base, key, model = "", "", ""
        if kind == "main":
            if pid == "llamacpp":
                base, key, model = self.llama_base_url, "", self.llama_model
            elif pid == "ollama":
                base, key, model = (self.main_base_url or "http://127.0.0.1:11434/v1"), self.main_api_key, self.main_model
            else:  # openai_compat
                base, key, model = self.main_base_url, self.main_api_key, self.main_model
        elif kind == "embedding":
            if pid == "local":
                # локальный fastembed-фолбэк: без сети/ключа/base_url/модели в облаке
                base, key, model = "", "", self.local_embedding_model
            else:
                base, key, model = self.embedding_base_url, self.embedding_api_key, self.embedding_model
        elif kind == "rerank":
            base = self.rerank_base_url or self.embedding_base_url
            key = self.rerank_api_key or self.embedding_api_key
            model = self.rerank_model
        elif kind == "tts":
            base, key, model = "", "", self.tts_voice  # model = голос (для UI)

        # per-world переопределения
        if overrides.get("base_url"):
            base = overrides["base_url"]
        if overrides.get("api_key"):
            key = overrides["api_key"]
        if overrides.get("model"):
            model = overrides["model"]

        enabled = pid != "none"
        if kind == "tts":
            enabled = pid != "none" and self.tts_enabled
        if overrides.get("enabled") is not None:
            enabled = bool(overrides["enabled"])
        return {
            "id": pid,
            "name": opt.get("name", pid),
            "needs_key": bool(opt.get("needs_key", False)),
            "needs_model": bool(opt.get("needs_model", False)),
            "base_url": (base or "").rstrip("/"),
            "api_key": key or "",
            "model": model or "",
            "enabled": enabled,
        }

    def resolve_world_providers(self, world_provider_settings: dict) -> dict:
        """Все провайдеры мира: {"main": {...}, "embedding": {...}, "rerank": {...}}."""
        ws = world_provider_settings or {}
        return {kind: self.get_provider(kind, ws.get(kind)) for kind in ("main", "embedding", "rerank")}

    @classmethod
    def load(cls, env_file: Path = ENV_FILE, env=None) -> "Config":
        env = env or os.environ
        vals: dict[str, str] = {}
        if env_file.exists():
            for raw in env_file.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                vals[k.strip()] = v.strip().strip('"').strip("'")
        admin_over = admin_settings.read_overrides()
        def get(*keys: str, default=None):
            for k in keys:
                if k in env and env[k] != "":
                    return env[k]
                if k in admin_over and admin_over[k] != "":
                    return admin_over[k]
                if k in vals and vals[k] != "":
                    return vals[k]
            return default

        def flt(default, *keys):
            try:
                return float(get(*keys, default=str(default)))
            except (TypeError, ValueError):
                return default

        def it(default, *keys):
            try:
                return int(get(*keys, default=str(default)))
            except (TypeError, ValueError):
                return default

        return cls(
            game_host=get("GAME_HOST", default="127.0.0.1"),
            game_port=it(8002, "GAME_PORT"),
            db_path=get("DB_PATH", default=str(ROOT / "data" / "game.db")),
            main_provider=get("MAIN_PROVIDER", default="llamacpp"),
            main_base_url=get("MAIN_BASE_URL", default=""),
            main_api_key=get("MAIN_API_KEY", default=""),
            main_model=get("MAIN_MODEL", default=""),
            llama_base_url=get("LLAMA_BASE_URL", default="http://127.0.0.1:8080/v1").rstrip("/"),
            llama_model=get("LLAMA_MODEL", default=""),
            embedding_provider=get("EMBEDDING_PROVIDER", default="routerai"),
            embedding_base_url=get("EMBEDDING_BASE_URL", default="https://routerai.ru/api/v1").rstrip("/"),
            embedding_api_key=get("EMBEDDING_API_KEY", default=""),
            embedding_model=get("EMBEDDING_MODEL", default="qwen/qwen3-embedding-8b"),
            embedding_dim=it(4096, "EMBEDDING_DIM"),
            prefix_doc=get("PREFIX_DOC", default="Passage: "),
            prefix_query=get("PREFIX_QUERY", default="Query: "),
            local_embedding_model=get("LOCAL_EMBEDDING_MODEL", default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"),
            local_embedding_dim=it(384, "LOCAL_EMBEDDING_DIM"),
            local_embedding_cache=get("LOCAL_EMBEDDING_CACHE", default="data/fastembed"),
            rerank_provider=get("RERANK_PROVIDER", default="routerai"),
            rerank_base_url=get("RERANK_BASE_URL", default="").rstrip("/"),
            rerank_api_key=get("RERANK_API_KEY", default=""),
            rerank_model=get("RERANK_MODEL", default="voyageai/rerank-2.5"),
            rerank_enabled=get("RERANK_ENABLED", default="true").lower() in ("1", "true", "yes", "on"),
            rerank_top_n=it(20, "RERANK_TOP_N"),
            rerank_threshold=flt(0.2, "RERANK_THRESHOLD"),
            hybrid_weight_bm25=flt(0.4, "HYBRID_WEIGHT_BM25"),
            cosine_threshold=flt(0.35, "COSINE_THRESHOLD"),
            cosine_threshold_local=flt(0.20, "COSINE_THRESHOLD_LOCAL"),
            default_temp=flt(0.8, "DEFAULT_TEMP"),
            default_top_p=flt(0.95, "DEFAULT_TOP_P"),
            max_tokens=it(2000, "MAX_TOKENS"),
            context_tokens=it(32768, "CONTEXT_TOKENS"),
            recent_token_budget=it(2600, "RECENT_TOKEN_BUDGET"),
            summary_token_budget=it(700, "SUMMARY_TOKEN_BUDGET"),
            rag_memory_k=it(4, "RAG_MEMORY_K"),
            rag_memory_max=it(24, "RAG_MEMORY_MAX"),
            rag_candidates=it(20, "RAG_CANDIDATES"),
            lore_token_budget=it(900, "LORE_TOKEN_BUDGET"),
            lore_token_budget_max=it(4500, "LORE_TOKEN_BUDGET_MAX"),
            lore_rag_k=it(3, "LORE_RAG_K"),
            lore_rag_k_max=it(12, "LORE_RAG_K_MAX"),
            dynamic_events_enabled=get("DYNAMIC_EVENTS_ENABLED", default="true").lower() in ("1", "true", "yes", "on"),
            event_every_turns=it(10, "EVENT_EVERY_TURNS"),
            max_action_chars=it(640, "MAX_ACTION_CHARS"),
            background_tasks_enabled=get("BACKGROUND_TASKS_ENABLED", default="true").lower() in ("1", "true", "yes", "on"),
            logic_judge_enabled=get("LOGIC_JUDGE_ENABLED", default="true").lower() in ("1", "true", "yes", "on"),
            logic_judge_interval=it(3, "LOGIC_JUDGE_INTERVAL"),
            divine_cooldown_turns=it(0, "DIVINE_COOLDOWN_TURNS"),
            tick_effects_enabled=get("TICK_EFFECTS_ENABLED", default="true").lower() in ("1", "true", "yes", "on"),
            tick_needs_enabled=get("TICK_NEEDS_ENABLED", default="true").lower() in ("1", "true", "yes", "on"),
            autonomous_master_enabled=get("AUTONOMOUS_MASTER_ENABLED", default="true").lower() in ("1", "true", "yes", "on"),
            autonomous_master_interval=it(6, "AUTONOMOUS_MASTER_INTERVAL"),
            enemy_ai_enabled=get("ENEMY_AI_ENABLED", default="true").lower() in ("1", "true", "yes", "on"),
            enemy_ai_interval=it(2, "ENEMY_AI_INTERVAL"),
            backup_db_on_start=get("BACKUP_DB_ON_START", default="true").lower() in ("1", "true", "yes", "on"),
            backup_keep=it(10, "BACKUP_KEEP"),
            metrics_persist=get("METRICS_PERSIST", default="true").lower() in ("1", "true", "yes", "on"),
            metrics_file=get("METRICS_FILE", default="data/metrics.jsonl"),
            metrics_tail=it(500, "METRICS_TAIL"),
            detect_model_context=get("DETECT_MODEL_CONTEXT", default="true").lower() in ("1", "true", "yes", "on"),
            tts_enabled=get("TTS_ENABLED", default="true").lower() in ("1", "true", "yes", "on"),
            tts_provider=get("TTS_PROVIDER", default="edge").lower(),
            tts_voice=get("TTS_VOICE", default="ru-RU-DmitryNeural"),
            tts_rate=get("TTS_RATE", default="+0%"),
            tts_auto_play=get("TTS_AUTO_PLAY", default="false").lower() in ("1", "true", "yes", "on"),
            tts_cache_enabled=get("TTS_CACHE_ENABLED", default="true").lower() in ("1", "true", "yes", "on"),
            tts_cache_ttl_days=it(60, "TTS_CACHE_TTL_DAYS"),
            tts_max_chars=it(2400, "TTS_MAX_CHARS"),
            chroma_host=get("CHROMA_HOST", default="127.0.0.1"),
            chroma_port=it(8001, "CHROMA_PORT"),
            chroma_collection=get("CHROMA_COLLECTION", default="text_game_memory"),
        )


def get_config() -> Config:
    if "cfg" not in _cache:
        _cache["cfg"] = Config.load()
    return _cache["cfg"]  # type: ignore


def _admin_overrides() -> dict:
    """Настройки из админки (таблица admin_settings): поверх .env, но ниже переменных окружения.
    Реализация вынесена в admin_settings.read_overrides() (отдельное sqlite-соединение без db.py —
    иначе config→db→config рекурсия вешает сервер).
    Оставлен тонкий wrapper для обратной совместимости."""
    return admin_settings.read_overrides()


def invalidate_config() -> None:
    """Сбрасывает кэш конфига (после сохранения настроек админки)."""
    _cache.clear()


def est_tokens(text: str) -> int:
    """Грубая оценка токенов (для кириллицы и латиницы)."""
    if not text:
        return 0
    # ~4 символа на токен для смешанного текста, но с запасом
    return max(1, int(len(text) / 3.2))