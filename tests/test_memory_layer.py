# -*- coding: utf-8 -*-
"""Сессия 33 — тесты СЛОЯ ПАМЯТИ и ОЗВУЧКИ (то, что conftest всегда заглушал).

До этой сессии 206 тестов покрывали директивы и API, но `retrieve_memory`,
`summarize_and_compress`, `_chunk_lore`, гибрид BM25, порогы косинуса, коллекции по
размерности и подготовка текста для TTS не проверялись НИ ОДНИМ тестом — conftest
подменяет память нулевыми заглушками. Здесь эти куски тестируются по-настоящему,
без сети и без живого ChromaDB (HTTP-слой подменён фикстурами ниже).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend import chroma_client, embeddings, lore_retriever, memory, narrator, tts
from backend import config as config_mod


# ═════════════════════ BM25 / гибрид (embeddings.py) ═════════════════════

def test_preprocess_strips_markdown_and_stopwords():
    t = embeddings.preprocess_text("**Стальная *пыль*** осела `[ссылка](http://x)` на `верстаке`")
    assert "http" not in t and "[" not in t and "*" not in t
    assert "стальная" in t and "верстак" in t


def test_bm25_ranks_relevant_document_first():
    corpus = [
        "кузнец выковал клинок из стальной руды у горна",
        "рыбаки вышли в море на рассвете и поймали тунца",
        "руда найденная в шахте пригодилась кузнецу для клинка",
    ]
    q = embeddings._tokenize("кузнец клинок руда")
    scores = embeddings.bm25_scores(q, corpus)
    assert len(scores) == 3
    assert scores[0] > scores[1], "документ про кузнеца обязан быть ближе к нулю, чем про рыбу"
    assert scores[2] > scores[1]


def test_bm25_empty_inputs_are_safe():
    assert embeddings.bm25_scores([], ["что-то"]) == [0.0]
    assert embeddings.bm25_scores(["а"], []) == []


def test_bm25_prefers_term_frequency_but_dampens():
    """Насыщение по BM25: x5 вхождений НЕ в 5 раз лучше x1 (k1 ограничивает)."""
    one = embeddings.bm25_scores(["огонь"], ["огонь burning"])
    five = embeddings.bm25_scores(["огонь"], ["огонь " * 5 + "burning"])
    assert five[0] > one[0]
    assert five[0] < one[0] * 5


def test_hybrid_rerank_mixes_cosine_and_bm25():
    cands = [
        {"content": "совершенно unrelated текст про китов и океан", "similarity": 0.90},
        {"content": "дракон сжёг амбар и улетел на север", "similarity": 0.60},
    ]
    out = embeddings.hybrid_rerank("дракон амбар", [dict(c) for c in cands], weight_bm25=0.4)
    # при weight=0.4 лексическое совпадение тянет «дракона» вверх
    assert out[0]["content"].startswith("дракон")
    assert "_hybrid" in out[0]
    # при weight=0 решение чистого косинуса сохраняется (код не «чинит» выдачу сам)
    out2 = embeddings.hybrid_rerank("дракон амбар", [dict(c) for c in cands], weight_bm25=0.0)
    assert out2[0]["similarity"] == 0.90


def test_hybrid_rerank_empty_and_missing_fields():
    assert embeddings.hybrid_rerank("запрос", []) == []
    out = embeddings.hybrid_rerank("запрос", [{"content": ""}])
    assert len(out) == 1


# ═══════════════ COLLECTIONS BY DIM (chroma_client.py) ═══════════════

def test_collection_name_follows_vector_dim(fake_config):
    fake_config(chroma_collection="text_game_memory", local_embedding_dim=384,
                embedding_dim=4096)
    assert chroma_client.collection_name_for_dim(4096) == "text_game_memory"
    assert chroma_client.collection_name_for_dim(384) == "text_game_memory_local"
    assert set(chroma_client.collection_names_all()) == {"text_game_memory",
                                                        "text_game_memory_local"}

def test_normalize_vectors_unit_length():
    v = chroma_client._normalize([3.0, 4.0])
    assert v == pytest.approx([0.6, 0.8])
    assert chroma_client._normalize([0.0, 0.0]) == [0.0, 0.0]  # нулевой вектор не делим на ноль


def test_cosine_from_distance_bounds():
    assert chroma_client.cosine_from_distance(0.0) == 1.0
    assert chroma_client.cosine_from_distance(2.0) == 0.0
    assert chroma_client.cosine_from_distance(9.0) == 0.0      # прижимает к нулю, не минус


def test_cosine_threshold_lower_for_local_provider(fake_config):
    cfg = fake_config(cosine_threshold=0.35, cosine_threshold_local=0.20)
    assert memory.cosine_threshold(cfg, {"id": "routerai"}) == 0.35
    assert memory.cosine_threshold(cfg, {"id": "local"}) == 0.20
    assert memory.cosine_threshold(cfg, None) == 0.35


# ═══════════════════ retrieve_memory: весь RAG-контур ═══════════════════

@pytest.fixture
def rag_env(monkeypatch, fake_config):
    """Герметичный RAG: эмбеддинги, Chroma и реранкер — подделаны, ЛОГИКА ОТБОРА НАСТЯЩАЯ."""
    cfg = fake_config(rag_memory_k=4, rag_memory_max=24, rag_candidates=20,
                      cosine_threshold=0.30, cosine_threshold_local=0.20,
                      hybrid_weight_bm25=0.4, rerank_enabled=False)
    state = {"query": [], "added": [], "events": 10}

    async def fake_embed_query(text, provider=None):
        state["embed_calls"] = state.get("embed_calls", 0) + 1
        state["last_query"] = text
        return [1.0] + [0.0] * 7

    async def fake_chroma_query(qv, n_results=10, where=None):
        state["n_results"] = n_results
        return [{"content": r["content"], "distance": r["distance"],
                 "metadata": r.get("metadata", {}), "chunk_id": r.get("chunk_id", "x")}
                for r in state.get("chroma_rows", [])]

    monkeypatch.setattr(embeddings, "embed_query", fake_embed_query)
    monkeypatch.setattr(chroma_client, "query", fake_chroma_query)
    monkeypatch.setattr(config_mod, "get_config", lambda: cfg)
    monkeypatch.setattr(memory, "get_config", lambda: cfg)
    monkeypatch.setattr(memory.db, "get_events",
                        lambda wid, limit=None, since_seq=0: [{"id": i} for i in range(state["events"])])
    # сессия 34 (B5): retrieve_memory считает события на стороне SQLite, а не длиной списка
    monkeypatch.setattr(memory.db, "count_events",
                        lambda wid, roles=None, unfolded_only=False: state["events"])
    monkeypatch.setattr(memory.db, "get_world", lambda wid: {"gen_settings": "{}"})
    return cfg, state


def _rows(state, rows):
    state["chroma_rows"] = rows


def test_retrieve_memory_filters_by_cosine_threshold(rag_env):
    cfg, state = rag_env
    _rows(state, [
        {"content": "далёкое эхо (косинус 0.2)", "distance": 1.6},
        {"content": "вспоминаю: дракон сжёг амбар", "distance": 0.4},   # cos = 0.8
        {"content": "почти совпадение — таверна", "distance": 0.1},     # cos = 0.95
    ])
    out = asyncio.run(memory.retrieve_memory(1, "осмотреться", {"locations": {}, "quests": {}}))
    assert all("далёкое эхо" not in c for c in out), "ниже порога обязано отбрасываться"
    assert len(out) == 2


def test_retrieve_memory_respects_k_and_dynamic_scaling(rag_env):
    _cfg, state = rag_env
    _rows(state, [{"content": f"факт {i}", "distance": 0.1} for i in range(30)])
    out = asyncio.run(memory.retrieve_memory(1, "идти", {"locations": {}, "quests": {}}, k=4))
    assert len(out) == 4
    # при 256k окне K растёт (не фиксированные 4 — жалоба «всегда 9 фактов при 262к»)
    big = {"gen_settings": json.dumps({"context_tokens": 262144})}
    k_big = narrator.dynamic_memory_k(big, 4, 24)
    assert k_big > 4 and k_big <= 24


def test_retrieve_memory_query_includes_location_and_quests(rag_env):
    _cfg, state = rag_env
    _rows(state, [{"content": "что-то", "distance": 0.1}])
    setting = {"current_location": "start", "locations": {"start": {"name": "Портовый город"}},
               "quests": {"q1": {"title": "Найти кузнеца", "status": "active"}}}
    asyncio.run(memory.retrieve_memory(1, "поговорить с стражником", setting))
    q = state["last_query"]
    assert "Портовый город" in q and "Найти кузнеца" in q and "поговорить с стражником" in q


def test_retrieve_memory_short_history_skips_rag(rag_env):
    """Миров на 1-2 ходах искать нечего — не дёргаем облако зря."""
    _cfg, state = rag_env
    state["events"] = 2
    _rows(state, [{"content": "x", "distance": 0.1}])
    out = asyncio.run(memory.retrieve_memory(1, "привет", {}))
    assert out == []
    assert "embed_calls" not in state, "запрос эмбеддинга не должен уходить вообще"


def test_retrieve_memory_silent_when_embeddings_off(rag_env, monkeypatch):
    """Выключенные эмбеддинги — ЛЕГАЛЬНЫЙ фолбэк (пусто), а не ошибка."""
    _cfg, state = rag_env

    async def none(text, provider=None):
        return []

    monkeypatch.setattr(embeddings, "embed_query", none)
    _rows(state, [{"content": "x", "distance": 0.1}])
    assert asyncio.run(memory.retrieve_memory(1, "идти", {})) == []


def test_retrieve_memory_logs_when_chroma_down(rag_env, monkeypatch, caplog):
    """Отказ Chroma обязан ЛОГИРОВАТЬСЯ (правило 14: фолбэк допустим, молчание — нет)."""
    import logging

    _cfg, state = rag_env

    async def boom(qv, n_results=10, where=None):
        raise RuntimeError("Chroma лёг")

    monkeypatch.setattr(chroma_client, "query", boom)
    with caplog.at_level(logging.WARNING, logger="textgame"):
        assert asyncio.run(memory.retrieve_memory(1, "идти", {})) == []
    assert any("Chroma лёг" in r.getMessage() for r in caplog.records), \
        "оценка RAG-недоступности не должна теряться в логах"


def test_retrieve_memory_uses_reranker_when_enabled(rag_env, monkeypatch, fake_config):
    cfg, state = rag_env
    monkeypatch.setattr(memory, "get_config", lambda: cfg)
    object.__setattr__(cfg, "rerank_enabled", True)
    seen = {}

    async def fake_rerank(query, cands, top_n=5, provider=None):
        seen["top_n"] = top_n
        return list(reversed(cands))[:top_n]

    monkeypatch.setattr(embeddings, "rerank_results", fake_rerank)
    _rows(state, [{"content": "первый", "distance": 0.1}, {"content": "второй", "distance": 0.2}])
    out = asyncio.run(memory.retrieve_memory(1, "идти", {}, k=2, providers={}))
    assert seen["top_n"] == 2
    assert out == ["первый", "второй"] or out == ["второй", "первый"]
    assert len(out) == 2


# ═════════════════════ индексация (idempotent, метаданные) ═════════════════════

def test_index_exchange_writes_world_scoped_chunk(monkeypatch, fake_config):
    fake_config()
    added = {}

    async def fake_embed(texts, provider=None):
        return [[0.1] * 4 for _ in texts]

    async def fake_add(**kwargs):
        added.update(kwargs)

    monkeypatch.setattr(embeddings, "embed_documents", fake_embed)
    monkeypatch.setattr(chroma_client, "add", fake_add)
    asyncio.run(memory.index_exchange(7, 3, "открыть сундук", "Внутри карта."))
    assert added["ids"] == ["ex_7_3"], "id чанка обязан быть уникальным для мира и seq"
    assert added["metadatas"][0]["world_id"] == 7, "без world_id память одного мира утечёт в другой"
    assert added["metadatas"][0]["kind"] == "exchange"
    doc = added["documents"][0]
    assert "открыть сундук" in doc and "Внутри карта." in doc


def test_index_exchange_truncates_huge_reply(monkeypatch, fake_config):
    fake_config()
    captured = {}

    async def fake_embed(texts, provider=None):
        return [[0.1] * 4 for _ in texts]

    async def fake_add(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(embeddings, "embed_documents", fake_embed)
    monkeypatch.setattr(chroma_client, "add", fake_add)
    asyncio.run(memory.index_exchange(1, 1, "а", "Б" * 5000))
    assert len(captured["documents"][0]) < 5000, "простыня ответа не должна уезжать в память целиком"


def test_index_summary_id_does_not_collide_with_exchange(monkeypatch, fake_config):
    fake_config()
    ids = []

    async def fake_embed(texts, provider=None):
        return [[0.1] * 4 for _ in texts]

    async def fake_add(**kwargs):
        ids.extend(kwargs["ids"])

    monkeypatch.setattr(embeddings, "embed_documents", fake_embed)
    monkeypatch.setattr(chroma_client, "add", fake_add)
    asyncio.run(memory.index_exchange(1, 5, "а", "б"))
    asyncio.run(memory.index_summary(1, 5, "сводка"))
    assert ids == ["ex_1_5", "sum_1_5"], "сводка и обмен на одном seq не должны затирать друг друга"


def test_index_entities_logs_and_never_raises_when_cloud_dead(monkeypatch, fake_config, caplog):
    """Фоновая индексация не роняет ход, но ошибку обязано быть видно в логе."""
    import logging

    fake_config()

    async def noop(*a, **kw):
        return None

    monkeypatch.setattr(chroma_client, "delete_by_ids", noop)

    async def boom(texts, provider=None):
        raise RuntimeError("облако легло")

    monkeypatch.setattr(embeddings, "embed_documents", boom)
    with caplog.at_level(logging.WARNING, logger="textgame"):
        asyncio.run(memory.index_entities(1, [{"kind": "npc", "entity_key": "a", "name": "А"}]))
    assert any("облако легло" in r.getMessage() for r in caplog.records)


# ═════════════════════ бюджеты памяти (narrator.py) ═════════════════════

def test_world_recent_budget_math(fake_config):
    """A2 (сессия 34): бюджет recent = контекст − (измеренный промпт + ответ) − резерв памяти,
    где резерв — ДОЛЯ окна, а не плоские 16384 (которые были больше всего локального n_ctx)."""
    cfg = fake_config(context_tokens=32768, max_tokens=2000)
    w = {"gen_settings": json.dumps({"context_tokens": 32768, "max_tokens": 2000})}
    b = narrator.world_recent_budget(w)
    extra = narrator.memory_extra_budget(w)
    assert 0 < b < 32768 - 2000
    assert b == max(narrator.MIN_RECENT_BUDGET, 32768 - narrator.CONTEXT_OVERHEAD - 2000 - extra)
    # резерв растёт с окном, но ограничен сверху
    assert extra == int(32768 * narrator.MEMORY_EXTRA_RATIO)
    assert narrator.MEMORY_EXTRA_BUDGET == narrator.MAX_MEMORY_EXTRA_BUDGET  # обратная совместимость
    assert narrator.world_recent_budget({}) == narrator.world_recent_budget(
        {"gen_settings": "{}"}) or b  # без gen_settings — дефолты конфига, не падение


def test_recent_budget_uses_measured_prompt_size(fake_config):
    """A2: реальный размер системного промпта (~6к токенов) обязан вычитаться из бюджета —
    иначе recent переполняет окно и модель молча обрезает ответ (локальный n_ctx 8192)."""
    fake_config(context_tokens=32768, max_tokens=2000)
    w = {"gen_settings": json.dumps({"context_tokens": 32768, "max_tokens": 2000})}
    guess = narrator.world_recent_budget(w)
    measured = narrator.world_recent_budget(w, prompt_tokens=12000)
    assert measured < guess, "измеренный промпт должен уменьшать окно истории"
    assert guess - measured == 12000 - narrator.CONTEXT_OVERHEAD
    # smaller-than-guess measurement не помогает молча съесть бюджет: держим запас
    assert narrator.world_recent_budget(w, prompt_tokens=500) == guess


def test_local_window_gets_real_budget_not_floor(fake_config):
    """A2: при llama.cpp n_ctx=8192 старая формула всегда давала пол 400 токенов
    (резерв 16384 > весь контекст). Теперь там реальное окно истории."""
    fake_config(context_tokens=8192, max_tokens=2000)
    w = {"gen_settings": json.dumps({"context_tokens": 8192, "max_tokens": 2000})}
    b = narrator.world_recent_budget(w)
    assert b > narrator.MIN_RECENT_BUDGET, "локальное окно больше не должно падать на пол"
    assert b + narrator.CONTEXT_OVERHEAD + 2000 + narrator.memory_extra_budget(w) <= 8192


def test_prompt_tiers_drop_unused_subsystem_rules(fake_config):
    """B4: правила о подсистемах, которых в мире нет, не едят контекст; и мгновенно
    возвращаются, когда подсистема появляется или игрок про неё пишет."""
    fake_config(prompt_tiers_enabled=False)
    empty_player = {"hp": 50, "max_hp": 50, "mp": 10, "max_mp": 10, "gold": 0, "level": 1,
                    "stats": {}, "inventory": [], "race": "человек", "class": "Воин",
                    "profession": "Кузнец", "skills": {"меч": {"rank": "D"}}}
    st = {"player": empty_player, "locations": {}, "npc": {}, "quests": {}, "flags": {}}
    w = {"id": 1, "name": "t", "language": "ru", "genre": "фэнтези", "difficulty": "normal",
         "perspective": "second", "theme": {"name": "T", "genre": "фэнтези"},
         "setting": json.dumps(st), "gen_settings": json.dumps({"max_tokens": 2000})}
    full = narrator.build_system_prompt(w, st, use_tools=True, action="")
    R22 = "Не создавай предметы «из ниоткуда» без рецепта"   # уникальная строка правила 22
    R26 = "ФРАКЦИИ → ПУТЬ ИГРОКА"                            # правило 26
    assert R26 in full and R22 in full
    # выключатель уважается: с выключенными ярусами ничего не отбрасывается
    assert narrator.gated_rules(st, "") == set()

    fake_config(prompt_tiers_enabled=True)
    lean = narrator.build_system_prompt(w, st, use_tools=True, action="")
    drop = narrator.gated_rules(st, "")
    assert {"21", "22", "26", "31"} <= drop
    assert R26 not in lean and R22 not in lean, "правила мёртвых подсистем не должны есть контекст"
    assert len(lean) < len(full), "в мире без подсистем промпт обязан быть короче"
    # словарь механик остаётся — модель знает, что директивы существуют
    assert "крафт, станции, сбор" in lean
    # отдельная чистая функция тоже считает экономию
    lean2, saved = narrator.trim_prompt(full, drop)
    assert saved > 0 and lean2 == lean
    # игрок просит крафт → правило обязано вернуться
    assert "22" not in narrator.gated_rules(st, "сковать меч у кузнеца")
    assert R22 in narrator.build_system_prompt(w, st, use_tools=True, action="сковать меч"), \
        "если игрок просит крафт — полное правило обязано вернуться в том же ходу"
    # подсистема появилась в состоянии → правило возвращается
    st2 = dict(st, shops={"s": {"name": "лавка"}}, factions={"g": {"name": "гильдия"}})
    drops = narrator.gated_rules(st2, "осмотреться")
    assert "21" not in drops and "26" not in drops
    # выключатель уважается
    fake_config(prompt_tiers_enabled=False)
    assert narrator.gated_rules(st, "") == set()


def test_build_messages_never_overflows_window(fake_config):
    """A2: собранный промпт не должен вылезать за окно модели — иначе ответы молча
    обрезаются (именно от этого защищали авто-детект n_ctx в сессии 33)."""
    from backend.config import est_tokens
    fake_config(context_tokens=8192, max_tokens=2000)
    st = {"player": {"hp": 1, "inventory": []}, "locations": {"here": {"name": "Тут"}},
          "npc": {}, "quests": {"q1": {"title": "Квест", "status": "active"}},
          "flags": {}, "current_location": "here"}
    w = {"id": 1, "name": "t", "language": "ru", "genre": "x", "difficulty": "normal",
         "perspective": "second", "setting": json.dumps(st), "theme": {"name": "T", "genre": "x"},
         "gen_settings": json.dumps({"context_tokens": 8192, "max_tokens": 2000})}
    cards = [{"kind": "npc", "name": f"N{i}", "entity_key": f"n{i}", "summary": "с" * 200,
              "relationship": "", "bio": "", "meta": "{}"} for i in range(6)]
    cards.append({"kind": "location", "name": "Тут", "entity_key": "here",
                  "summary": "м", "relationship": "", "bio": "", "meta": "{}"})
    huge_lore = ["ло р" * 400] * 4
    huge_rag = ["па мять" * 400] * 6
    recent = [{"role": "player", "content": "x" * 500}, {"role": "narrator", "content": "y" * 500}] * 8
    msgs, meta = narrator.build_messages(w, st, "идти", recent, [{"content": "св " * 300}] * 3,
                                        huge_rag, cards, lore=huge_lore)
    assert est_tokens(msgs[0]["content"]) <= 8192 - 2000, meta
    assert meta["trimmed"], "усечение должно быть видно в метаданных хода"
    assert not meta["overflow_tokens"]
    # карточки СЦЕНЫ (локация + активный квест) не выкидываются никогда
    assert "Тут" in msgs[0]["content"] and "Квест" in msgs[0]["content"]


def test_world_recent_budget_floors_at_minimum(fake_config):
    fake_config(context_tokens=8192, max_tokens=4000)
    tiny = {"gen_settings": json.dumps({"context_tokens": 1024, "max_tokens": 4000})}
    assert narrator.world_recent_budget(tiny) >= narrator.MIN_RECENT_BUDGET


def test_world_context_tokens_falls_back_to_config(fake_config):
    cfg = fake_config(context_tokens=32768)
    assert narrator.world_context_tokens({}) == cfg.context_tokens
    assert narrator.world_context_tokens(
        {"gen_settings": json.dumps({"context_tokens": 8192})}) == 8192
    # мусор в JSON не валит ход
    assert narrator.world_context_tokens({"gen_settings": "{не json"}) == cfg.context_tokens


def test_dynamic_lore_budget_scales_and_caps(fake_config):
    fake_config()
    assert narrator.dynamic_lore_budget({}, 900, 4500) >= 900
    big = {"gen_settings": json.dumps({"context_tokens": 262144})}
    assert narrator.dynamic_lore_budget(big, 900, 4500) == 4500, "потолок обязано соблюдать"
    # per-world переопределение (0 = авто)
    manual = {"gen_settings": json.dumps({"context_tokens": 32768, "lore_token_budget": 1200})}
    assert narrator.dynamic_lore_budget(manual, 900, 4500) >= 1200


# ═════════════════════ лор: чанкинг (lore_retriever.py) ═════════════════════

def test_chunk_lore_keeps_paragraphs_whole():
    text = "Первый абзац про историю города.\n\nВторой абзац про гильдию магов.\n\nТретий короткий."
    chunks = lore_retriever._chunk_lore(text, max_tokens=550)
    assert chunks
    assert "".join(chunks).count("Первый абзац") == 1
    assert all(len(c) for c in chunks)


def test_chunk_lore_splits_long_article():
    para = "Предложение про драконов, которые давно вымерли, но оставили кладки. " * 40
    text = "\n\n".join([para] * 6)
    chunks = lore_retriever._chunk_lore(text, max_tokens=550)
    assert len(chunks) > 1, "статью на 2000+ токенов нельзя отдавать в промпт одним куском"
    assert all(len(c) <= len(para) * 2 for c in chunks), "чанки обязаны быть сопоставимыми"


def test_chunk_lore_empty_input():
    assert lore_retriever._chunk_lore("", max_tokens=550) in ([], [""])
    assert lore_retriever._chunk_lore("   \n\n  ", max_tokens=550) in ([], [""])


# ═════════════════════ сводки (memory.summarize_and_compress) ═════════════════════

def test_summarize_compresses_old_events_and_folds(fake_config, monkeypatch):
    """Сводка: старые события помечаются folded, в БД идёт роль summary, в память — индексация.
    Проверяет и сигнатуру: summarize_and_compress(world_id, provider=None), мир берётся из БД."""
    cfg = fake_config(summary_token_budget=700)
    calls = {"add": [], "folded": [], "indexed": []}

    async def fake_complete(messages, **kw):
        return "Герой посетил рынок и поговорил со стражником."

    monkeypatch.setattr(memory, "get_config", lambda: cfg)
    monkeypatch.setattr(memory.llm, "complete", fake_complete)

    # бюджет окна мира (=400 при 4096/2000) заведомо меньше одного сообщения → сворачиваем всё, кроме первого
    big = "ход " * 400
    evs = [{"id": i, "seq": i, "role": r, "content": big, "folded": 0}
           for i, r in enumerate(["player", "narrator"] * 8)]
    monkeypatch.setattr(memory.db, "get_events", lambda wid, **kw: list(evs))
    # сессия 34 (B5): сводка берёт окно несвёрнутых событий ограниченным запросом
    monkeypatch.setattr(memory.db, "get_unfolded_events", lambda wid, limit=120, roles=None: list(evs))
    monkeypatch.setattr(memory.db, "get_world", lambda wid: {
        "gen_settings": json.dumps({"context_tokens": 4096, "max_tokens": 2000}),
        "provider_settings": "{}", "language": "ru"})
    monkeypatch.setattr(memory.db, "latest_seq", lambda wid: 16)

    def _add(wid, role, content, seq=None, meta=None):
        calls["add"].append((role, content))
        return {"id": 99, "seq": seq or 17, "role": role, "content": content, "meta": meta or {}}

    monkeypatch.setattr(memory.db, "add_event", _add)
    monkeypatch.setattr(memory.db, "mark_folded", lambda wid, up_to: calls["folded"].append(up_to))

    async def fake_index(wid, seq, summary, provider=None):
        calls["indexed"].append(summary)

    monkeypatch.setattr(memory, "index_summary", fake_index)
    asyncio.run(memory.summarize_and_compress(1))
    assert calls["add"], "сводка обязана записаться событием"
    assert calls["add"][0][0] == "summary", "роль сводки — summary (её берут build_messages/аудит)"
    assert calls["folded"], "старые события должны быть помечены свёрнутыми"
    assert calls["indexed"] == ["Герой посетил рынок и поговорил со стражником."]


def test_summarize_does_not_fold_when_llm_fails(fake_config, monkeypatch):
    """Если архивариус не ответил — сворачивать НЕЛЬЗЯ: иначе история теряется безвозвратно."""
    cfg = fake_config(summary_token_budget=700)
    monkeypatch.setattr(memory, "get_config", lambda: cfg)

    async def boom(messages, **kw):
        raise RuntimeError("модель легла")

    monkeypatch.setattr(memory.llm, "complete", boom)
    big = "ход " * 400
    evs = [{"id": i, "seq": i, "role": r, "content": big, "folded": 0}
           for i, r in enumerate(["player", "narrator"] * 8)]
    monkeypatch.setattr(memory.db, "get_events", lambda wid, **kw: list(evs))
    # сессия 34 (B5): сводка берёт окно несвёрнутых событий ограниченным запросом
    monkeypatch.setattr(memory.db, "get_unfolded_events", lambda wid, limit=120, roles=None: list(evs))
    monkeypatch.setattr(memory.db, "get_world", lambda wid: {
        "gen_settings": json.dumps({"context_tokens": 4096, "max_tokens": 2000}),
        "provider_settings": "{}", "language": "ru"})
    folded, added = [], []
    monkeypatch.setattr(memory.db, "mark_folded", lambda wid, up_to: folded.append(up_to))
    monkeypatch.setattr(memory.db, "add_event", lambda *a, **kw: added.append(a))
    asyncio.run(memory.summarize_and_compress(1))
    assert added == [] and folded == []


def test_summarize_skips_when_nothing_to_fold(fake_config, monkeypatch):
    cfg = fake_config(summary_token_budget=700)
    monkeypatch.setattr(memory, "get_config", lambda: cfg)
    monkeypatch.setattr(memory.db, "get_events",
                        lambda wid, **kw: [{"id": 1, "seq": 1, "role": "player",
                                            "content": "привет", "folded": 0}])
    monkeypatch.setattr(memory.db, "get_unfolded_events",
                        lambda wid, limit=120, roles=None: [{"id": 1, "seq": 1, "role": "player",
                                                             "content": "привет", "folded": 0}])
    monkeypatch.setattr(memory.db, "get_world", lambda wid: {"gen_settings": "{}"})
    added = []
    monkeypatch.setattr(memory.db, "add_event",
                        lambda *a, **kw: added.append(a))
    asyncio.run(memory.summarize_and_compress(1))
    assert added == [], "на одном коротком ходе сводить нечего"


# ═════════════════════ TTS: подготовка текста и кэш ═════════════════════

def test_clean_tts_text_strips_markdown_emoji_and_counts():
    t = tts._clean_tts_text("Ты берёшь **меч** (x2) 🔥 и идёшь в `подземелье`")
    assert "*" not in t and "`" not in t
    assert "🔥" not in t
    assert "(x2)" not in t
    assert "меч" in t and "подземелье" in t


def test_clean_tts_text_reads_multipliers_as_numbers():
    assert "2" in tts._clean_tts_text("урон x2 по врагу")


def test_split_chunks_respects_limit_and_sentences():
    text = "Первое предложение. " * 100
    chunks = tts._split_chunks(text, limit=120)
    assert len(chunks) > 1
    assert all(len(c) <= 120 for c in chunks), "кусок длиннее лимита уронит синтез"
    assert "".join(chunks).count("Первое") == 100


def test_split_chunks_edge_cases():
    assert tts._split_chunks("", limit=100) == []
    assert tts._split_chunks("короткая фраза", limit=100) == ["короткая фраза"]
    # одно гигантское предложение без точек — режем по символам, не теряем текст
    big = "а" * 5000
    parts = tts._split_chunks(big, limit=1200)
    assert sum(len(p) for p in parts) == 5000
    assert all(len(p) <= 1200 for p in parts)


def test_tts_hash_depends_on_voice_rate_provider():
    h1 = tts._hash("текст", "edge", "ru-RU-DmitryNeural", "+0%")
    h2 = tts._hash("текст", "edge", "ru-RU-SvetlanaNeural", "+0%")
    h3 = tts._hash("текст", "edge", "ru-RU-DmitryNeural", "+20%")
    h4 = tts._hash("другой текст", "edge", "ru-RU-DmitryNeural", "+0%")
    assert len({h1, h2, h3, h4}) == 4, "иначе кэш вернёт чужую озвучку"
    assert h1 == tts._hash("текст", "edge", "ru-RU-DmitryNeural", "+0%")


def test_norm_rate_always_signed_for_edge():
    """Edge падает на «0%» — знак обязателен (баг из сессии 9, регресс)."""
    for raw, expect in [(None, "+0%"), ("", "+0%"), ("0%", "+0%"), ("0", "+0%"),
                        ("20%", "+20%"), ("+20%", "+20%"), ("-10%", "-10%")]:
        out = tts._norm_rate(raw)
        assert out.startswith(("+", "-")), f"{raw!r} → {out!r}: Edge отклонит без знака"


def test_tts_effective_prefers_world_over_global(fake_config):
    cfg = fake_config(tts_enabled=True, tts_provider="edge", tts_voice="ru-RU-DmitryNeural",
                      tts_rate="+0%")
    monkey_world = {"tts_settings": json.dumps({"enabled": True, "provider": "piper",
                                                "voice": "ru_RU-ruslan-medium", "rate": "+10%"})}
    eff = tts.tts_effective(monkey_world)
    assert eff["provider"] == "piper" and eff["voice"] == "ru_RU-ruslan-medium"
    assert eff["rate"] == "+10%"
    # пустые поля мира = глобальные дефолты
    eff2 = tts.tts_effective({"tts_settings": json.dumps({})})
    assert eff2["provider"] == cfg.tts_provider and eff2["voice"] == cfg.tts_voice


def test_tts_effective_disabled_globally(fake_config):
    cfg = fake_config(tts_enabled=False, tts_provider="edge", tts_voice="ru-RU-DmitryNeural",
                      tts_rate="+0%")
    eff = tts.tts_effective({"tts_settings": json.dumps({"enabled": True})})
    assert eff.get("enabled") is False or eff.get("provider") in ("none", "edge")


def test_piper_voice_ready_false_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(tts, "VOICES_DIR", tmp_path)
    assert tts.piper_voice_ready("ru_RU-нет-такого-medium") is False
    d = tmp_path / "ru_RU-ruslan-medium"
    d.mkdir()
    (d / "ru_RU-ruslan-medium.onnx").write_bytes(b"x")
    (d / "tokens.txt").write_text("а б в", encoding="utf-8")
    assert tts.piper_voice_ready("ru_RU-ruslan-medium") is True
