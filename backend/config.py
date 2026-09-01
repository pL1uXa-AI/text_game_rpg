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


def strip_env_comment(value: str) -> str:
    """Отрезает хвостовой комментарий строки `.env`: `KEY=value # пояснение`.

    Сессия 36, п.13: раньше комментарий попадал в значение (`"value # пояснение"`), и
    числовые/буевые настройки молча ломались (it()/flt() откатывались на дефолт, а строка
    пути уходила в значение с мусором). Срезаем только `#` после пробела и только вне
    кавычек — иначе сломали бы `SECRET="pa#ssword"` и URL с якорем.
    """
    v = (value or "").strip()
    if not v:
        return v
    quote = ""
    for i, ch in enumerate(v):
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in ('"', "'"):
            quote = ch
            continue
        if ch == "#" and i > 0 and v[i - 1] in " \t":
            v = v[:i]
            break
    return v.strip().strip('"').strip("'")


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
    # A17 (аудит 38): ручки RERANK_TOP_N в проекте больше нет — размер выдачи реранкера
    # задаётся размером RAG-выдачи (RAG_MEMORY_K / LORE_RAG_K), а не отдельной настройкой
    # (две ручки на одно = источник путаницы). Порог релевантности ниже — РЕАЛЬНО применяется
    # в embeddings.rerank_results (раньше и он был декоративным).
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
    # и правит мир директивой. Это «корректор мира».
    # Сессия 36, п.2: раньше кулдаун был 0, и «попросить у богов золото» можно было в
    # неограниченном количестве — ответы Провидения защитой от фарма не являются (они
    # генерирует та же модель, что и директивы выдачи). Поэтому по умолчанию включён
    # минимальный интервал: жаловаться чаще, чем раз в N ходов, не имеет смысла, а 429
    # в роутере уже был — просто недостижимый при cd=0. 0 по-прежнему отключает лимит.
    divine_cooldown_turns: int = 3    # минимум ходов между воззваниями (0 = без кулдауна)
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
    # Ротация журнала метрик (сессия 36, п.7): METRICS_TAIL ограничивал только чтение, а
    # сам файл рос бесконечно. При переходе порога metrics.jsonl сдвигается в .1/.2/.3
    # (как лог-файлы logsetup). 0 = ротация выключена (журнал растёт как раньше).
    metrics_max_bytes: int = 4_194_304   # 4 МБ на файл журнала
    # Авто-подгонка размера контекста мира под реальный n_ctx модели (защита от обрезов
    # на локальной llama.cpp 8192 при стандарте .env 32768).
    detect_model_context: bool = True

    # ── Наблюдаемость (сессия 34, логирование) ──
    # Уровень логгера `textgame` (DEBUG/INFO/WARNING/ERROR). DEBUG показывает и легальные
    # фолбэки (log_once-повторы, тихие деградации RAG).
    log_level: str = "INFO"
    # JSON-лог с контекстом хода (world_id/turn_seq/agent). Пусто/off = только консоль.
    # Относительный путь считается от корня игры.
    log_file: str = "data/logs/game.log"
    log_max_bytes: int = 5_242_880     # размер файла до ротации (~5 МБ)
    log_backup_count: int = 3          # сколько архивных файлов держать
    # Отладочный дамп собранного промпта хода в лог (DEBUG) — видно, что реально ушло в модель.
    log_prompt_dump: bool = False

    # ── Устойчивость обращений к LLM/Chroma/облаку (сессия 34) ──
    # Фоновые агенты и ход игрока переживают временные сбои (429/5xx/таймаут/обрыв
    # соединения): повтор с экспоненциальной задержкой. 0 = отключить повторы.
    llm_retries: int = 2              # повторов ПОСЛЕ первой попытки (итого до 3 запросов)
    llm_retry_backoff: float = 0.8    # базовая задержка, удваивается (0.8с → 1.6с)
    llm_timeout: float = 300.0        # общий таймаут запроса к модели (connect — жёстче)
    chroma_retries: int = 1           # ChromaDB — локальный сервис: один быстрый повтор
    embedding_retries: int = 1        # облачные эмбеддинги/реранкер

    # ── Очередь фоновых агентов (сессия 34) ──
    # Все LLM-проходы (ход игрока + судья/мастер/боевой ИИ/события/карточки/видения) делят
    # ОДН очередь к модели. Без этого 5–6 фоновых задач ставятся в тот же слот сразу после
    # ответа и «съедают» следующий ход игрока. Семафор ограничивает параллельность,
    # приоритет решает, кто пойдёт первым, а очередь видна в /api/metrics.
    llm_bg_concurrency: int = 2       # сколько фоновых LLM-проходов одновременно
    llm_bg_max_queue: int = 32        # потолок очереди (переполнение = отказ с логом)
    llm_bg_yield_turn: bool = True    # фоновые агенты ждут окончания хода игрока

    # Сколько точек перемотки (состояний перед ходом) хранить на мир. Каждая ~1-5 КБ JSON.
    # 0 = хранить все. Нужно для «назад к ходу N» (C1) и честной загрузки сохранений (A1).
    turn_snapshot_keep: int = 120

    # ── Ярусы системного промпта (сессия 34) ──
    # Промпт рассказчика ≈ 6к токенов каждый ход. «Полный» промпт нужен, когда модель обязана
    # сама эмитить механику (локальные провайдеры, формат <<ENGINE>>). В tools-режиме список
    # директив уже отдан схемой инструмента game_engine — многостраничные примеры во
    # промпте избыточны и только съедают контекст, поэтому промпт собирается тоньше.
    # (Замер сессии 34: полный ≈ 6.2к токенов против ≈ 4.2к без дубля schemas-примеров.)
    prompt_tiers_enabled: bool = True

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
                vals[k.strip()] = strip_env_comment(v.strip())
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
            # A6 (аудит 38): дефолт обязан совпадать с dataclass (`divine_cooldown_turns: int = 3`).
            # Раньше здесь стоял литерал 0, и на ЛЮБОЙ чистой установке (копия .env.example без
            # ключа, CI, свежий клон) кулдаун Провидения был 0 — анти-фарм «попросить у богов
            # золото» не работал ровно там, где его и включали. Регресс-страховка:
            # tests/test_session38_bugfixes.py::test_config_load_defaults_match_dataclass
            divine_cooldown_turns=it(3, "DIVINE_COOLDOWN_TURNS"),
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
            metrics_max_bytes=it(4194304, "METRICS_MAX_BYTES"),
            detect_model_context=get("DETECT_MODEL_CONTEXT", default="true").lower() in ("1", "true", "yes", "on"),
            log_level=get("LOG_LEVEL", default="INFO"),
            log_file=get("LOG_FILE", default="data/logs/game.log"),
            log_max_bytes=it(5242880, "LOG_MAX_BYTES"),
            log_backup_count=it(3, "LOG_BACKUP_COUNT"),
            log_prompt_dump=get("LOG_PROMPT_DUMP", default="false").lower() in ("1", "true", "yes", "on"),
            llm_retries=it(2, "LLM_RETRIES"),
            llm_retry_backoff=flt(0.8, "LLM_RETRY_BACKOFF"),
            llm_timeout=flt(300.0, "LLM_TIMEOUT"),
            chroma_retries=it(1, "CHROMA_RETRIES"),
            embedding_retries=it(1, "EMBEDDING_RETRIES"),
            llm_bg_concurrency=it(2, "LLM_BG_CONCURRENCY"),
            llm_bg_max_queue=it(32, "LLM_BG_MAX_QUEUE"),
            llm_bg_yield_turn=get("LLM_BG_YIELD_TURN", default="true").lower() in ("1", "true", "yes", "on"),
            prompt_tiers_enabled=get("PROMPT_TIERS_ENABLED", default="true").lower() in ("1", "true", "yes", "on"),
            turn_snapshot_keep=it(120, "TURN_SNAPSHOT_KEEP"),
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


def invalidate_config() -> None:
    """Сбрасывает кэш конфига (после сохранения настроек админки)."""
    _cache.clear()


def est_tokens(text: str) -> int:
    """Грубая оценка токенов (для кириллицы и латиницы)."""
    if not text:
        return 0
    # ~4 символа на токен для смешанного текста, но с запасом
    return max(1, int(len(text) / 3.2))