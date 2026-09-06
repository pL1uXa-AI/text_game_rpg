# -*- coding: utf-8 -*-
"""Конфигурация игры: читает .env из корня проекта (переопределяется env процесса)."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from . import admin_settings
from .logsetup import log_once

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

# ══════════════════════════════════════════════════════
# Границы числовых настроек (A9, аудит 41)
# ══════════════════════════════════════════════════════
# Раньше «кривое» число проходило насквозь: валидация была только «int()/float() упадёт
# → дефолт» (config.it/flt), а диапазонов не было ни в .env, ни в админке, ни в настройках
# мира. Итог: `MAX_TOKENS=0` или `LLM_TIMEOUT=0.01` валились уже МОДЕЛИ на КАЖДОМ ходу, а
# чинились только повторной правкой настроек.
# ONE source of truth — этот словарь; он применяется:
#   * в `Config.load` (env процесса → админка → .env — все три источника разом),
#   * в `logsetup` (ротор журнала настраивается до первого конфига и читает env напрямую),
#   * в per-world настройках (`routers/core.py::clamp_gen_settings`, там же и при ЧТЕНИИ —
#     старые/импортированные миры с кривым gen_settings перестают ломать ходы).
# Нижняя граница TURN_SNAPSHOT_KEEP = 10: точка перемотки, которой почти нет, бесполезна
# так же, как её отсутствие (1–9 снапшотов = «назад» работает на пару ходов и молча
# умирает дальше). «Хранить все» при этом остаётся законным 0 — см. NUM_ZERO_KEYS.
# Подрезка всегда пишется в журнал (правило 14) — тихой правки нет.
# Числа вне реестра (порты, размерности эмбеддингов) не трогаются: реестр пополняется
# осознанно, а не «на всякий».
NUM_RANGES: dict[str, tuple[float, float]] = {
    # генерация (те же границы у per-world значений — см. GEN_LIMIT_KEYS)
    "DEFAULT_TEMP": (0.0, 2.0),
    "DEFAULT_TOP_P": (0.0, 1.0),
    "MAX_TOKENS": (16.0, 32768.0),
    "CONTEXT_TOKENS": (512.0, 262144.0),
    "MAX_ACTION_CHARS": (16.0, 100000.0),
    # память/RAG
    "RECENT_TOKEN_BUDGET": (64.0, 200000.0),
    "SUMMARY_TOKEN_BUDGET": (64.0, 200000.0),
    "RAG_MEMORY_K": (0.0, 200.0),
    "RAG_MEMORY_MAX": (0.0, 200.0),
    "RAG_CANDIDATES": (1.0, 500.0),
    "LORE_TOKEN_BUDGET": (0.0, 200000.0),
    "LORE_TOKEN_BUDGET_MAX": (0.0, 200000.0),
    "LORE_RAG_K": (0.0, 200.0),
    "LORE_RAG_K_MAX": (0.0, 200.0),
    "RERANK_THRESHOLD": (0.0, 1.0),
    "HYBRID_WEIGHT_BM25": (0.0, 1.0),
    "COSINE_THRESHOLD": (0.0, 1.0),
    "COSINE_THRESHOLD_LOCAL": (0.0, 1.0),
    # частота фоновых агентов (0 у событий = «никогда», интервалы не бывают нулевыми)
    "EVENT_EVERY_TURNS": (1.0, 10000.0),
    "LOGIC_JUDGE_INTERVAL": (1.0, 10000.0),
    "AUTONOMOUS_MASTER_INTERVAL": (1.0, 10000.0),
    "ENEMY_AI_INTERVAL": (1.0, 10000.0),
    "DIVINE_COOLDOWN_TURNS": (0.0, 1000.0),
    "MECH_NARRATE_RETRIES": (0.0, 5.0),
    "AUTO_TIME_EVERY": (0.0, 1000.0),        # 0 = авто-часы выключены (сессия 40, п.14b)
    # устойчивость обращений
    "LLM_RETRIES": (0.0, 5.0),
    "LLM_RETRY_BACKOFF": (0.0, 10.0),
    "LLM_TIMEOUT": (5.0, 7200.0),            # ниже 5 с ход не уходит — модель не успевает
    "CHROMA_RETRIES": (0.0, 5.0),
    "EMBEDDING_RETRIES": (0.0, 5.0),
    "LLM_BG_CONCURRENCY": (1.0, 4.0),        # больше 4 фоновых проходов локальная модель не тянет
    "LLM_BG_MAX_QUEUE": (1.0, 512.0),
    # журнал/данные
    "LOG_MAX_BYTES": (1048576.0, 268435456.0),   # минимум 1 МБ: иначе ротация съедает журнал
    "LOG_BACKUP_COUNT": (0.0, 30.0),
    "METRICS_TAIL": (10.0, 100000.0),
    "METRICS_MAX_BYTES": (0.0, 268435456.0),     # 0 = ротация выключена (решение сессии 36)
    "TURN_SNAPSHOT_KEEP": (10.0, 100000.0),
    "BACKUP_KEEP": (1.0, 1000.0),
    "TTS_CACHE_TTL_DAYS": (0.0, 3650.0),         # 0 = вечно
    "TTS_MAX_CHARS": (100.0, 100000.0),
}

# Ключи, для которых 0 — НЕ «слишком мало», а осмысленный режим: ниже своего минимума не
# подрезаем (иначе сломали бы задокументированные выключатели):
#   TURN_SNAPSHOT_KEEP=0 — хранить все точки (`db._save_turn_snapshot`: `if keep and keep>0`);
#   AUTO_TIME_EVERY=0    — авто-часы выключены (сессия 40, п.14b);
#   EVENT_EVERY_TURNS=0  — «никогда» (`narrator.event_chance`); 0 у порогов RAG = «авто»;
#   METRICS_MAX_BYTES=0  — ротация журнала метрик выключена (решение сессии 36);
#   TTS_CACHE_TTL_DAYS=0 — кэш озвучки живёт вечно.
NUM_ZERO_KEYS: frozenset[str] = frozenset({
    "TURN_SNAPSHOT_KEEP", "AUTO_TIME_EVERY", "EVENT_EVERY_TURNS", "RAG_MEMORY_K",
    "RAG_MEMORY_MAX", "LORE_RAG_K", "LORE_RAG_K_MAX", "LORE_TOKEN_BUDGET",
    "LORE_TOKEN_BUDGET_MAX", "METRICS_MAX_BYTES", "TTS_CACHE_TTL_DAYS",
    "RERANK_THRESHOLD", "HYBRID_WEIGHT_BM25", "COSINE_THRESHOLD",
    "COSINE_THRESHOLD_LOCAL", "LLM_RETRY_BACKOFF",
})

_CLAMP_LOG = logging.getLogger("textgame.config")


def clamp_num(key: str, value, *, integer: bool = True, label: str | None = None):
    """Значение настройки в границах `NUM_RANGES[key]`; нет ключа — значение как есть.

    Числовую СТРОКУ (её приносят админка и JSON из БД) нормализуем к числу того же типа,
    что и границы: потребитель и так отправлял её в модель числом. Совсем не-число
    ("широко") остаётся как есть — изобретать значение за игрока нечем (закон 3), а
    тихая подмена дефолтом только прячет проблему.

    Пишет warning при подрезке (`label` — где именно лежит значение: для per-world
    настроек это не имя env-ключа). Неброско: вызывается на каждом чтении конфига.
    """
    rng = NUM_RANGES.get(key)
    if rng is None:
        return value
    lo, hi = rng
    try:
        num = int(value) if integer else float(value)
    except (TypeError, ValueError):
        return value
    if num == 0 and key in NUM_ZERO_KEYS:
        return num                      # 0 = задокументированный режим, а не «слишком мало»
    out = int(min(hi, max(lo, num))) if integer else float(min(hi, max(lo, num)))
    text = str(value).strip() if isinstance(value, str) else None
    if out != num or (text is not None and text != str(out)):
        # log_once: пер-мир настройки читаются КАЖДЫЙ ход (narrator.world_gen_settings),
        # и повторяющийся warning превратился бы в лог-шторм (правило 14 + A6, аудит 41):
        # факт видим ОДИН раз на (ключ, значение), а не молчит вовсе
        log_once(_CLAMP_LOG, f"clamp:{label or key}={value}", logging.WARNING,
                 "настройка %s=%s приведена/подрезана до %s (допустимо %s…%s)",
                 label or key, value, out, lo, hi)
    return out


def clamp_int(key: str, value, *, label: str | None = None) -> int:
    return clamp_num(key, value, integer=True, label=label)


def clamp_float(key: str, value, *, label: str | None = None) -> float:
    return clamp_num(key, value, integer=False, label=label)


def num_kind(key: str) -> str | None:
    """'int' | 'float' | None — тип числовой настройки по её env-имени (дефолт `Config`).

    Нужен админке, где все числа приходят СТРОКАМИ: по нему решаем, `int()` или `float()`
    и что отвечать в `400`. None — либо ключ нечисловой, либо границ не задавали."""
    fld = Config.__dataclass_fields__.get(key.lower())
    if fld is None:
        return None
    dflt = fld.default
    if isinstance(dflt, bool) or not isinstance(dflt, (int, float)):
        return None
    return "int" if isinstance(dflt, int) else "float"


# Per-world ключи gen_settings → имена глобальных настроек того же смысла. Одна таблица
# границ на игру: то, что запрещено в .env/админке, запрещено и в мире (A9, аудит 41).
GEN_LIMIT_KEYS: dict[str, str] = {
    "temperature": "DEFAULT_TEMP",
    "top_p": "DEFAULT_TOP_P",
    "max_tokens": "MAX_TOKENS",
    "context_tokens": "CONTEXT_TOKENS",
    "rag_memory_k": "RAG_MEMORY_K",
    "lore_rag_k": "LORE_RAG_K",
    "lore_token_budget": "LORE_TOKEN_BUDGET",
}


def clamp_gen_settings(gen: dict, *, where: str = "настройки мира") -> dict:
    """Пер-мирные `gen_settings` в общих границах (A9). Возвращает НОВЫЙ dict.

    Нечисловой мусор (строка/None) не трогает — его переводит в дефолт потребитель.
    Только проверка возможности (закон 2): значения выбирает игрок, код не «улучшает» их.
    """
    out = dict(gen or {})
    for k, env_name in GEN_LIMIT_KEYS.items():
        v = out.get(k)
        if v is None:
            continue
        out[k] = clamp_num(env_name, v,
                           integer=env_name not in ("DEFAULT_TEMP", "DEFAULT_TOP_P"),
                           label=f"{where}: {k}")
    return out


def gen_clamped_before(gen: dict, after: dict) -> dict:
    """Что именно подрезали/привели в `gen_settings` (для ответа API, A9).

    `{ключ: {sent, used, range}}` — пусто, если трогать было нечего. Числовые строки
    («"0.7"») считаются тем же числом: они доезжают до модели числом, а вот подмену
    диапазона игроку обязан показывать UI."""
    out: dict[str, dict] = {}
    for k, v in (after or {}).items():
        old = (gen or {}).get(k)
        if old is None:
            continue
        try:
            same = float(str(old)) == float(str(v))
        except (TypeError, ValueError):
            same = old == v
        if not same:
            rng = NUM_RANGES.get(GEN_LIMIT_KEYS.get(k, ""))
            out[k] = {"sent": old, "used": v,
                      "range": list(rng) if rng else None}
    return out


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


def _warn_dead_keys(env_vals: dict[str, str], admin_over: dict[str, str]) -> None:
    """B5 (аудит 41): ключ, которого конфиг не читает, обязан быть виден, а не молчать.

    Так в проекте и поселились «мёртвые» настройки: `RERANK_TOP_N` (ручку сняли в A17,
    а строка осталась в .env И в таблице admin_settings) и `GAME_HOST`/`GAME_PORT` (их
    читал только сам config.py, реальный bind/port задаёт start_game.bat). Следующая
    сессия честно искала бы, «куда не применяется настройка». Теперь при старте в журнал
    идёт список лишних имён — РАЗДЕЛЬНО по источникам: «убери строку в .env» и «такую
    строку записала админка» — это разные действия (у владельца файл, лезть в него самой
    игре нельзя).

    Опорный список — `overridable_env_keys()` (производен от dataclass, устареть не может).
    Пишем через `log_once`: конфиг перечитывается на каждый ход, а шторм в логе — это
    тот же тихий шум, только громче (правило 14). Вызов только из `Config.load` — на импорте
    ничего не происходит (A13).
    """
    known = set(overridable_env_keys())
    env_dead = sorted({k.upper() for k in env_vals if k.upper() not in known})
    admin_dead = sorted({k.upper() for k in admin_over if k.upper() not in known})
    if not env_dead and not admin_dead:
        return
    where = []
    if env_dead:
        where.append("в файле .env: " + ", ".join(env_dead))
    if admin_dead:
        where.append("в таблице admin_settings (записано админкой): " + ", ".join(admin_dead))
    log_once(_CLAMP_LOG, "dead-keys:" + ",".join(env_dead + admin_dead), logging.WARNING,
             "мёртвые ключи настроек — конфиг их НЕ читает, чини код или убирай строку — %s",
             "; ".join(where))


@dataclass
class Config:
    # API
    # B5 (аудит 41): полей `game_host`/`game_port` здесь больше нет — их читали из .env, но,
    # кроме самого config.py, НЕ ТРОГАЛ никто: адрес и порт задаёт `start_game.bat`
    # (`GAME_BIND` из окружения процесса + жёсткий `--port 8002`, завязанный на фронт и на
    # проверку портов в scripts/check_start_bat.py). Приложение не выбирает, на каком порту
    # его подняли (это делает uvicorn-раннер), поэтому «настройка» была обещанием в никуда,
    # а `hidden_admin_keys()` честно объявляла несуществующую настройку «не для админки».
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

    # ── Безопасность доступа (B2, аудит 41) ──
    # У игры НЕТ авторизации: любой, до кого дотянется порт, = владелец админки.
    # Поэтому /api/admin/settings (правка провайдеров, base_url и ключей) по умолчанию
    # отвечает только с localhost. Ставить true стоит лишь осознанно (нужен доступ с
    # телефона при GAME_BIND=0.0.0.0) — через саму админку ключ не включается
    # (см. hidden_admin_keys): иначе открывший админку мог бы разблокировать себе сеть.
    admin_allow_lan: bool = False

    # B2: глобальный выключатель rate-limit'а «дорогих» эндпоинтов. Применяется на старте
    # (`app._lifespan` → `ratelimit.configure`), вранье в .env без этого было бы обещанием
    # в никуда (ключ читался только docstring'ом модуля, а не кодом).
    rate_limit_enabled: bool = True

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
    # п.16 (сессия 40): сколько раз просить модель дописать прозу, если она вернула
    # только механику без текста (перед тем как показывать служебную строку).
    mech_narrate_retries: int = 2
    # п.14b (сессия 40): часы мира идут сами (ход × N → утро/день/вечер/ночь). Причина —
    # «вечер» из сюжета залипал навсегда: время меняет только директива time, а мастер
    # её не давал, и расписания NPC («ночью таверна закрыта») противоречили тексту.
    # Погоду авто-часы НЕ трогают — это творческое решение мастера (закон 3).
    auto_time_enabled: bool = True
    auto_time_every: int = 4           # ходов игрока на одну часть суток (сутки ≈ 16 ходов)

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
        _warn_dead_keys(vals, admin_over)
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
            # A9: после разбора — общий кламп по NUM_RANGES (кривой .env/админка больше
            # не уезжают в модель и не ломают каждый ход)
            try:
                val = float(get(*keys, default=str(default)))
            except (TypeError, ValueError):
                val = float(default)
            return clamp_float(keys[0], val) if keys else val

        def it(default, *keys):
            try:
                val = int(get(*keys, default=str(default)))
            except (TypeError, ValueError):
                val = int(default)
            return clamp_int(keys[0], val) if keys else val

        return cls(
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
            admin_allow_lan=get("ADMIN_ALLOW_LAN", default="false").lower() in ("1", "true", "yes", "on"),
            rate_limit_enabled=get("RATE_LIMIT_ENABLED", default="true").lower() in ("1", "true", "yes", "on"),
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
            mech_narrate_retries=it(2, "MECH_NARRATE_RETRIES"),
            auto_time_enabled=get("AUTO_TIME_ENABLED", default="true").lower() in ("1", "true", "yes", "on"),
            auto_time_every=it(4, "AUTO_TIME_EVERY"),
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


def overridable_env_keys() -> tuple[str, ...]:
    """Имена env/админки, которые МОЖНО переопределять: поля Config в верхнем регистре.

    B5/D1 (аудит 38): раньше такой реестр (`admin_settings.OVERRIDABLE_KEYS`, 26 имён)
    был только объявлением — ни роутер, ни документация его не читали, и он отстал от
    реальных настроек (в нём не было AUTONOMOUS_MASTER_*, ENEMY_AI_*, LOGIC_JUDGE_*,
    DIVINE_COOLDOWN_TURNS, RAG_*, LORE_*, COSINE_*, METRICS_*). Источник теперь выводится
    из dataclass и устареть не может: имя env-ключа проекта = ИМЯ_ПОЛЯ Config (закреплено
    тестом test_env_example_covers_all_config_keys).
    """
    return tuple(sorted(f.upper() for f in Config.__dataclass_fields__))


def hidden_admin_keys() -> tuple[str, ...]:
    """Ключи, которых в админке сознательно нет — их нельзя поменять «на живую».

    Это инфраструктура (путь к данным/порты: нужен перезапуск) и `LOG_FILE`: logsetup
    настраивается до первого чтения конфига и берёт путь из env/.env напрямую
    (config→db→config — рекурсия опасна), поэтому правка пути к журналу через админку
    молча не применилась бы. README и админка говорят об этом честно (B5).

    B5 (аудит 41): GAME_HOST/GAME_PORT из списка сняты вместе с самими полями — врать про
    «настройку, которую нельзя поменять на живую», когда настройки нет вообще, хуже, чем
    молчать. Список обязан состоять только из РЕАЛЬНЫХ полей Config (следит
    `tests/test_session58_dead_config.py`)."""
    return ("DB_PATH", "CHROMA_HOST", "CHROMA_PORT", "CHROMA_COLLECTION", "LOG_FILE",
            # B2: «открыть админку наружу» не может включаться самой админкой — иначе
            # доступ к форме превращается в самопроизвольный срыв замка).
            "ADMIN_ALLOW_LAN")


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