# -*- coding: utf-8 -*-
"""Роутер админки: глобальные настройки LLM/провайдеров/генерации/TTS.

Хранение — таблица admin_settings (db.py); переопределения читает config через
backend/admin_settings.py (отдельное соединение, без рекурсии config↔db).
Здесь только HTTP-слой: валидация, маскировка ключей, сброс кэша конфига."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import db
from ..config import invalidate_config, get_config, KEY_MASK, PROVIDER_OPTIONS
from ..schemas import AdminSettingsIn
from .core import _mask_provider

router = APIRouter(tags=["Админка"])


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
        "effective": {
            "providers": {k: _mask_provider(dict(v)) for k, v in prov.items()},
            "rerank_enabled": cfg.rerank_enabled,
            "rerank_top_n": cfg.rerank_top_n,
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
                       "divine_cooldown_turns": cfg.divine_cooldown_turns,
                       "max_action_chars": cfg.max_action_chars},
            # переносимость и наблюдаемость (сессия 33)
            "persist": {"detect_model_context": bool(cfg.detect_model_context),
                        "backup_db_on_start": bool(cfg.backup_db_on_start),
                        "backup_keep": cfg.backup_keep,
                        "metrics_persist": bool(cfg.metrics_persist),
                        "metrics_tail": cfg.metrics_tail,
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
        payload["HYBRID_WEIGHT_BM25"] = "" if v == "" else v
    # выключатели фоновых агентов и мира
    for f, env in (("logic_judge_enabled", "LOGIC_JUDGE_ENABLED"),
                   ("dynamic_events_enabled", "DYNAMIC_EVENTS_ENABLED"),
                   ("autonomous_master_enabled", "AUTONOMOUS_MASTER_ENABLED"),
                   ("enemy_ai_enabled", "ENEMY_AI_ENABLED"),
                   ("tick_effects_enabled", "TICK_EFFECTS_ENABLED"),
                   ("tick_needs_enabled", "TICK_NEEDS_ENABLED"),
                   ("detect_model_context", "DETECT_MODEL_CONTEXT"),
                   ("backup_db_on_start", "BACKUP_DB_ON_START"),
                   ("metrics_persist", "METRICS_PERSIST"),
                   ("tts_cache_enabled", "TTS_CACHE_ENABLED")):
        v = getattr(body, f, None)
        if v is not None:
            payload[env] = "true" if v else "false"
    # числовые параметры памяти/агентов/мир (пустая строка = сброс к .env)
    for f in ("rerank_top_n", "rerank_threshold", "rag_memory_k", "rag_memory_max",
              "rag_candidates", "recent_token_budget", "summary_token_budget",
              "lore_token_budget", "lore_token_budget_max", "lore_rag_k", "lore_rag_k_max",
              "cosine_threshold", "cosine_threshold_local", "logic_judge_interval",
              "event_every_turns", "autonomous_master_interval", "enemy_ai_interval",
              "divine_cooldown_turns", "max_action_chars", "tts_cache_ttl_days", "tts_max_chars",
              "backup_keep", "metrics_tail"):
        v = getattr(body, f, None)
        if v is None:
            continue
        v = str(v).strip()
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
    for k in (body.reset or []):
        key = str(k).strip().upper()
        if key:
            payload[key] = ""
    db.set_admin_settings(payload)
    invalidate_config()
    cfg = get_config()
    return {"ok": True,
            "effective": {"providers": {k: _mask_provider(dict(v)) for k, v in
                                          cfg.resolve_world_providers({}).items()},
                           "rerank_enabled": cfg.rerank_enabled,
                           "defaults": {"temperature": cfg.default_temp, "top_p": cfg.default_top_p,
                                        "max_tokens": cfg.max_tokens, "context_tokens": cfg.context_tokens}}}