# -*- coding: utf-8 -*-
"""Роутер админки: глобальные настройки LLM/провайдеров/генерации/TTS.

Хранение — таблица admin_settings (db.py); переопределения читает config через
backend/admin_settings.py (отдельное соединение, без рекурсии config↔db).
Здесь только HTTP-слой: валидация, маскировка ключей, сброс кэша конфига."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import db
from ..config import (get_config, hidden_admin_keys, invalidate_config, KEY_MASK,
                    PROVIDER_OPTIONS, overridable_env_keys)
from ..logsetup import get_logger
from ..schemas import AdminSettingsIn
from .core import _mask_provider

router = APIRouter(tags=["Админка"])

log = get_logger(__name__)


@router.get("/api/admin/settings")
async def admin_settings_get():
    cfg = get_config()
    admin = db.get_admin_settings()
    stored = {k: v for k, v in admin.items() if v.strip()}
    # ключи нормализуем в верхний регистр (как имена env) и маскируем api-ключи
    masked_stored = {k.upper(): (KEY_MASK if k.upper().endswith("API_KEY") and v.strip() else v)
                     for k, v in stored.items()}
    prov = cfg.resolve_world_providers({})
    return {
        "stored": masked_stored,
        # Единый список ключей, которые админка умеет переопределять. Фронт строит по нему
        # «сбросить всё к .env» — раньше список был продублирован ТРЕТЬИМ местом (RESET_KEYS
        # в admin.html) и мог разойтись (B5.2).
        "overridable": [k for k in overridable_env_keys() if k not in hidden_admin_keys()],
        "not_overridable": list(hidden_admin_keys()),
        "effective": {
            "providers": {k: _mask_provider(dict(v)) for k, v in prov.items()},
            "rerank_enabled": cfg.rerank_enabled,
            "rerank_threshold": cfg.rerank_threshold,
            "background_tasks_enabled": bool(cfg.background_tasks_enabled),
            "hybrid_weight_bm25": cfg.hybrid_weight_bm25,
            "defaults": {"temperature": cfg.default_temp, "top_p": cfg.default_top_p,
                         "max_tokens": cfg.max_tokens, "context_tokens": cfg.context_tokens},
            "memory": {"rag_memory_k": cfg.rag_memory_k, "rag_memory_max": cfg.rag_memory_max,
                       "rag_candidates": cfg.rag_candidates,
                       "recent_token_budget": cfg.recent_token_budget,
                       "summary_token_budget": cfg.summary_token_budget,
                       "lore_token_budget": cfg.lore_token_budget,
                       "lore_token_budget_max": cfg.lore_token_budget_max,
                       "lore_rag_k": cfg.lore_rag_k, "lore_rag_k_max": cfg.lore_rag_k_max,
                       "cosine_threshold": cfg.cosine_threshold,
                       "cosine_threshold_local": cfg.cosine_threshold_local},
            "agents": {"logic_judge_enabled": bool(cfg.logic_judge_enabled),
                       "logic_judge_interval": cfg.logic_judge_interval,
                       "dynamic_events_enabled": bool(cfg.dynamic_events_enabled),
                       "event_every_turns": cfg.event_every_turns,
                       "autonomous_master_enabled": bool(cfg.autonomous_master_enabled),
                       "autonomous_master_interval": cfg.autonomous_master_interval,
                       "enemy_ai_enabled": bool(cfg.enemy_ai_enabled),
                       "enemy_ai_interval": cfg.enemy_ai_interval,
                       "tick_effects_enabled": bool(cfg.tick_effects_enabled),
                       "tick_needs_enabled": bool(cfg.tick_needs_enabled),
                       # п.14b/п.16 (сессия 40): авто-часы мира и дописывание механики
                       "auto_time_enabled": bool(cfg.auto_time_enabled),
                       "auto_time_every": cfg.auto_time_every,
                       "mech_narrate_retries": cfg.mech_narrate_retries,
                       "divine_cooldown_turns": cfg.divine_cooldown_turns,
                       "max_action_chars": cfg.max_action_chars},
            # переносимость и наблюдаемость (сессия 33)
            # наблюдаемость и устойчивость (B5, сессия 38) — то, чего в админке не было
            "runtime": {"log_level": cfg.log_level,
                        "log_max_bytes": cfg.log_max_bytes,
                        "log_backup_count": cfg.log_backup_count,
                        "log_prompt_dump": bool(cfg.log_prompt_dump),
                        "llm_retries": cfg.llm_retries,
                        "llm_retry_backoff": cfg.llm_retry_backoff,
                        "llm_timeout": cfg.llm_timeout,
                        "chroma_retries": cfg.chroma_retries,
                        "embedding_retries": cfg.embedding_retries,
                        "llm_bg_concurrency": cfg.llm_bg_concurrency,
                        "llm_bg_max_queue": cfg.llm_bg_max_queue,
                        "llm_bg_yield_turn": bool(cfg.llm_bg_yield_turn),
                        "prompt_tiers_enabled": bool(cfg.prompt_tiers_enabled),
                        "turn_snapshot_keep": cfg.turn_snapshot_keep},
            "persist": {"detect_model_context": bool(cfg.detect_model_context),
                        "backup_db_on_start": bool(cfg.backup_db_on_start),
                        "backup_keep": cfg.backup_keep,
                        "metrics_persist": bool(cfg.metrics_persist),
                        "metrics_tail": cfg.metrics_tail,
                        "metrics_max_bytes": cfg.metrics_max_bytes,
                        "metrics_file": cfg.metrics_file},
            "tts": {"provider": cfg.tts_provider, "enabled": bool(cfg.tts_enabled),
                     "voice": cfg.tts_voice, "rate": cfg.tts_rate,
                     "auto_play": bool(cfg.tts_auto_play),
                     "cache_enabled": bool(cfg.tts_cache_enabled),
                     "cache_ttl_days": cfg.tts_cache_ttl_days,
                     "max_chars": cfg.tts_max_chars},
        },
    }


@router.post("/api/admin/settings")
async def admin_settings_save(body: AdminSettingsIn):
    # валидация провайдеров
    for kind in ("main", "embedding", "rerank"):
        pid = getattr(body, f"{kind}_provider")
        if pid is not None:
            ids = {o["id"] for o in PROVIDER_OPTIONS[kind]}
            if pid not in ids:
                raise HTTPException(400, f"Неизвестный провайдер «{pid}» для {kind}")
    payload: dict = {}
    for kind in ("main", "embedding", "rerank"):
        pid = getattr(body, f"{kind}_provider")
        for f in ("provider", "base_url", "api_key", "model"):
            v = getattr(body, f"{kind}_{f}", None)
            if f == "provider":
                v = pid
            if v is None:
                continue
            v = str(v).strip()
            if v == "":
                # пусто = сброс к .env
                payload[f"{kind}_{f}".upper()] = ""
            elif not (f == "api_key" and v == KEY_MASK):
                payload[f"{kind}_{f}".upper()] = v
    if body.rerank_enabled is not None:
        payload["RERANK_ENABLED"] = "true" if body.rerank_enabled else "false"
    if body.background_tasks_enabled is not None:
        payload["BACKGROUND_TASKS_ENABLED"] = "true" if body.background_tasks_enabled else "false"
    if body.hybrid_weight_bm25 is not None:
        v = str(body.hybrid_weight_bm25).strip()
        # Сессия 36, п.29: вес BM25 обязан попасть в [0,1] — иначе гибрид выдаёт score
        # вне диапазона и RAG начинает сортировать память наперевос (косинус с
        # отрицательным весом при w>1). Не валидировалось нигде, кроме клампа в
        # embeddings.hybrid_rerank; здесь — понятный отказ вместо тихой правки.
        if v:
            try:
                w = float(v)
            except ValueError:
                raise HTTPException(400, "Вес BM25 должен быть числом от 0 до 1")
            if not (0.0 <= w <= 1.0):
                raise HTTPException(400, f"Вес BM25 вне диапазона [0, 1]: {v}")
        payload["HYBRID_WEIGHT_BM25"] = v
    # переключатели наблюдаемости/устойчивости (B5): булевы — своим списком
    for f, env in (("llm_bg_yield_turn", "LLM_BG_YIELD_TURN"),
                   ("prompt_tiers_enabled", "PROMPT_TIERS_ENABLED"),
                   ("log_prompt_dump", "LOG_PROMPT_DUMP")):
        v = getattr(body, f, None)
        if v is not None:
            payload[env] = "true" if v else "false"
    # числовые/строковые параметры наблюдаемости (пустая строка = сброс к .env)
    for f in ("log_level", "log_max_bytes", "log_backup_count",
              "llm_retries", "llm_retry_backoff", "llm_timeout",
              "chroma_retries", "embedding_retries",
              "llm_bg_concurrency", "llm_bg_max_queue", "turn_snapshot_keep"):
        v = getattr(body, f, None)
        if v is None:
            continue
        v = str(v).strip()
        payload[f.upper()] = v
    # выключатели фоновых агентов и мира
    for f, env in (("logic_judge_enabled", "LOGIC_JUDGE_ENABLED"),
                   ("dynamic_events_enabled", "DYNAMIC_EVENTS_ENABLED"),
                   ("autonomous_master_enabled", "AUTONOMOUS_MASTER_ENABLED"),
                   ("enemy_ai_enabled", "ENEMY_AI_ENABLED"),
                   ("tick_effects_enabled", "TICK_EFFECTS_ENABLED"),
                   ("tick_needs_enabled", "TICK_NEEDS_ENABLED"),
                   ("auto_time_enabled", "AUTO_TIME_ENABLED"),
                   ("detect_model_context", "DETECT_MODEL_CONTEXT"),
                   ("backup_db_on_start", "BACKUP_DB_ON_START"),
                   ("metrics_persist", "METRICS_PERSIST"),
                   ("tts_cache_enabled", "TTS_CACHE_ENABLED")):
        v = getattr(body, f, None)
        if v is not None:
            payload[env] = "true" if v else "false"
    # числовые параметры памяти/агентов/мир (пустая строка = сброс к .env)
    for f in ("rerank_threshold", "rag_memory_k", "rag_memory_max",
              "rag_candidates", "recent_token_budget", "summary_token_budget",
              "lore_token_budget", "lore_token_budget_max", "lore_rag_k", "lore_rag_k_max",
              "cosine_threshold", "cosine_threshold_local", "logic_judge_interval",
              "event_every_turns", "autonomous_master_interval", "enemy_ai_interval",
              "divine_cooldown_turns", "max_action_chars", "tts_cache_ttl_days", "tts_max_chars",
              "auto_time_every", "mech_narrate_retries",
              "backup_keep", "metrics_tail", "metrics_max_bytes"):
        v = getattr(body, f, None)
        if v is None:
            continue
        v = str(v).strip()
        # размер журнала обязан оставаться неотрицательным (0 = ротация выключена);
        # отрицательный порог молча ломал бы ротацию (файл не ротируется никогда)
        if f == "metrics_max_bytes" and v:
            try:
                if int(v) < 0:
                    raise HTTPException(400, "METRICS_MAX_BYTES не может быть отрицательным")
            except ValueError:
                raise HTTPException(400, "METRICS_MAX_BYTES должен быть целым числом байт")
        payload[f.upper()] = "" if v == "" else v
    # TTS: булевы + строки
    if body.tts_enabled is not None:
        payload["TTS_ENABLED"] = "true" if body.tts_enabled else "false"
    if body.tts_auto_play is not None:
        payload["TTS_AUTO_PLAY"] = "true" if body.tts_auto_play else "false"
    for f in ("tts_provider", "tts_voice", "tts_rate"):
        v = getattr(body, f, None)
        if v is None:
            continue
        v = str(v).strip()
        payload[f.upper()] = "" if v == "" else v
    for f in ("default_temp", "default_top_p", "max_tokens", "context_tokens"):
        v = getattr(body, f, None)
        if v is None:
            continue
        v = str(v).strip()
        payload[f.upper()] = "" if v == "" else v
    # Явный сброс ключей к .env (пустое значение = удалить строку из admin_settings).
    # Позволяет кнопке «сбросить к .env» НЕ трогать булевы тумблеры как false.
    allowed = set(overridable_env_keys())
    unknown: list[str] = []
    for k in (body.reset or []):
        key = str(k).strip().upper()
        if not key:
            continue
        if key not in allowed:
            unknown.append(key)
            continue
        payload[key] = ""
    if unknown:
        # B5: раньше любое значение в `reset` молча уходило в таблицу пустой строкой, и
        # опечатка в имени ключа означала «сброс не сработал» без единого признака
        raise HTTPException(400, "Неизвестные ключи админки (опечатка в reset?): "
                                 + ", ".join(sorted(unknown)))
    db.set_admin_settings(payload)
    invalidate_config()
    # LOG_* применяются на лету: logsetup.reconfigure() перепривязывает уровень и ротацию
    # (механизм написан в сессии 34, но из админки не вызывался — настройка «уровень
    # логгера» ждала перезапуска, B5). Путь к файлу не переключается: он из env/.env
    # (см. config.hidden_admin_keys), иначе была бы рекурсия config→db→config.
    try:
        from .. import logsetup
        logsetup.reconfigure()
    except Exception as e:
        # правило 14: админка сохранена, а уровень журнала не применился — это видно в логе
        log.warning("перенастройка логов после сохранения админки не удалась: %s", e)
    cfg = get_config()
    return {"ok": True,
            "effective": {"providers": {k: _mask_provider(dict(v)) for k, v in
                                          cfg.resolve_world_providers({}).items()},
                           "rerank_enabled": cfg.rerank_enabled,
                           "defaults": {"temperature": cfg.default_temp, "top_p": cfg.default_top_p,
                                        "max_tokens": cfg.max_tokens, "context_tokens": cfg.context_tokens}}}