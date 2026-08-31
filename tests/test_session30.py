# -*- coding: utf-8 -*-
"""Тесты новых возможностей сессии 30: rate-limit, трассинг фоновых агентов, LRU-кэш эмбеддингов."""
from __future__ import annotations

import asyncio


def test_ratelimit_allows_and_blocks():
    from backend import ratelimit as rl
    rl.reset()
    rl.configure(enabled=True)
    # маленький лимит: 5 в окне 60с, burst 3 в 5с
    ok = all(rl.allow("k1", limit=5, window=60, burst=3, burst_window=5) for _ in range(3))
    assert ok, "3 вызова под burst-лимитом проходят"
    assert not rl.allow("k1", limit=5, window=60, burst=3, burst_window=5), "4-й вызов блокирован (burst=3)"
    # другой ключ не задет
    assert rl.allow("k2", limit=5, window=60, burst=3, burst_window=5)
    # выключенный лимитер пропускает всё
    rl.configure(enabled=False)
    assert rl.allow("k1", limit=1, window=60, burst=1, burst_window=5)
    # восстанавливаем состояние тестовой среды: лимитер должен остаться ВЫКЛЮЧЕННЫМ
    # (conftest configure(enabled=False) для герметичности API-тестов)
    rl.configure(enabled=False)
    rl.reset()


def test_metrics_agent_tracing():
    from backend import metrics as mm
    mm.record_agent("judge", 123.4, world_id=7)
    mm.record_agent("judge", 50.0, world_id=7)
    mm.record_agent("master", 900.1, world_id=7, ok=False)
    report = mm.as_json(limit=5)
    agents = report["agents"]
    assert "judge" in agents["agents"], "судья в отчёте"
    assert agents["agents"]["judge"]["calls"] >= 2
    assert agents["agents"]["judge"]["avg_ms"] > 0
    assert agents["agents"]["master"]["errors"] >= 1, "ошибка мастера зафиксирована"
    assert agents["last"], "последние спаны есть"


def test_embeddings_lru_cache_hits():
    """LRU-кэш эмбеддингов: повторный embed_query того же текста не пересчитывает вектор."""
    from backend import embeddings as emb

    calls = {"n": 0}
    real_http = emb._http_json

    async def fake_http(method, url, headers=None, payload=None, timeout=120):
        calls["n"] += 1
        texts = payload["input"]
        out = []
        for i, t in enumerate(texts):
            base = sum(ord(ch) for ch in str(t)) % 97 + 1  # зависит от текста
            out.append({"index": i, "embedding": [float(base)] * 4096})
        return {"data": out}

    emb._http_json = fake_http
    try:
        prov = {"id": "test", "model": "m", "base_url": "http://x", "api_key": "k", "enabled": True}
        # провайдер test не проходит ветку local/ключей — используем прямой вызов внутреннего пути
        # (обход проверок: ставим dummy cfg-поля через monkeypatch-подобную замену embed_batch)
        async def run():
            v1 = await emb.embed_batch(["Привет мир"], is_query=True, provider=prov)
            v2 = await emb.embed_batch(["Привет мир"], is_query=True, provider=prov)
            v3 = await emb.embed_batch(["Другой текст"], is_query=True, provider=prov)
            return v1, v2, v3

        v1, v2, v3 = asyncio.run(run())
        assert v1 == v2, "кэш вернул тот же вектор"
        assert calls["n"] == 2, f"HTTP-вызовов 2 (первый текст+другой), а не 3: {calls['n']}"
        assert v3 != v1, "другой текст → другой вектор (или кэш не смешал)"
    finally:
        emb._http_json = real_http
        emb._cache_clear()  # не оставляем тестовые векторы в глобальном кэше


# ── Сессия 31: динамическая память от контекста + защита от обрывов/дублей ──

def test_dynamic_memory_k_scales_with_context():
    """RAG-фактов/лор-чанков становится больше при большом контексте мира (не фикс. 4/3)."""
    from backend import narrator
    # мир с контекстом 32k → база
    w32 = {"gen_settings": '{"context_tokens": 32768}'}
    assert narrator.dynamic_memory_k(w32, 4, 24) == 4
    assert narrator.dynamic_memory_k(w32, 3, 12) == 3
    # мир с контекстом 262k → пропорционально больше, но не выше потолка
    w256 = {"gen_settings": '{"context_tokens": 262144}'}
    k = narrator.dynamic_memory_k(w256, 4, 24)
    assert k > 4, f"при 256k RAG-фактов больше 4: {k}"
    assert k <= 24, f"не выше потолка: {k}"
    lk = narrator.dynamic_memory_k(w256, 3, 12)
    assert lk > 3 and lk <= 12
    # per-world переопределение базы (0 = авто)
    wo = {"gen_settings": '{"context_tokens": 32768, "rag_memory_k": 10}'}
    assert narrator.dynamic_memory_k(wo, 4, 24) == 10
    # лор-бюджет тоже растёт
    b = narrator.dynamic_lore_budget(w256, 900, 4500)
    assert b > 900 and b <= 4500


def test_dynamic_lore_budget_caps():
    from backend import narrator
    w = {"gen_settings": '{"context_tokens": 262144}'}
    assert narrator.dynamic_lore_budget(w, 900, 4500) == 4500  # потолок


def test_dedupe_repeats_removes_adjacent_duplicates():
    """Подряд идущие точные дубликаты абзацев/предложений удаляются."""
    from backend.routers.core import _dedupe_repeats
    t = ("Ты пригибаешься, вглядываясь в туман. "
         "Ты пригибаешься, вглядываясь в туман. Пальцы скользят по земле.")
    out = _dedupe_repeats(t)
    assert out.count("Ты пригибаешься") == 1, f"дубль убран: {out!r}"
    # короткие фразы не трогаются
    assert _dedupe_repeats("Хм. Хм.") == "Хм. Хм."


def test_looks_finished():
    from backend.routers.core import _looks_finished
    assert _looks_finished("Он идёт к груде камня.")
    assert _looks_finished("Туман густеет…")
    assert _looks_finished("Он говорит: «привет»")
    assert not _looks_finished("Ты идёшь к груде кам"), "обрыв на полуслове"
    assert _looks_finished("")
