# -*- coding: utf-8 -*-
"""
lore_retriever.py — лор мира (библия вселенной): статьи → чанки → RAG → сжатие в промпт.

Декомпозиция narrator.py (сессия 30): сюда перенесены все функции лора
(verbatim, семантика не менялась):
- seed_lore_from_theme / seed_lore_from_custom — сидинг статей при создании мира;
- _chunk_lore — чанкинг больших статей (по абзацам/предложениям, ~550 токенов);
- index_lore_entry / index_all_lore — индексация чанков в Chroma (kind='lore');
- retrieve_lore — отдача в промпт: якорные статьи (всегда) + RAG-чанки по действию
  игрока + фолбэк свежими статьями при выключенном RAG; бюджет LORE_TOKEN_BUDGET.

Публичное API сохраняет имена/сигнатуры narrator.py (narrator.retrieve_lore(...) и т.п.
работают через реэкспорт в конце narrator.py).
"""
from __future__ import annotations

import json
import logging
import re

from . import chroma_client, db, embeddings
from .config import est_tokens, get_config
from .memory import memory_query_text, cosine_threshold  # общие с памятью хелперы

from .logsetup import get_logger

log = get_logger(__name__)


def _provider_settings(world: dict) -> dict:
    try:
        ws = json.loads(world.get("provider_settings") or "{}")
        return ws if isinstance(ws, dict) else {}
    except Exception:
        return {}


def seed_lore_from_theme(world_id: int, theme: dict) -> list[dict]:
    """Создаёт статьи лора из темы (поле theme['lore']) при создании мира.
    Идемпотентно по source='theme' — повторный вызов не дублирует статьи."""
    entries = theme.get("lore") or []
    if not entries:
        return []
    existing = db.list_lore(world_id)
    if any(e.get("source") == "theme" for e in existing):
        return []
    out = []
    for i, e in enumerate(entries, 1):
        title = str(e.get("title") or f"Лор {i}").strip()
        content = str(e.get("content") or "").strip()
        if not content:
            continue
        out.append(db.create_lore(world_id, title, content,
                                  tags=str(e.get("tags") or ""),
                                  is_core=bool(e.get("core") or e.get("is_core")),
                                  source="theme"))
    return out


def seed_lore_from_custom(world_id: int, text: str) -> list[dict]:
    """Разбирает пользовательский текст лора на статьи: строки «## Заголовок» —
    новая статья, остальное — её содержание. Без заголовков — одна статья «Лор мира»."""
    text = (text or "").strip()
    if not text:
        return []
    lines = text.splitlines()
    blocks: list[tuple[str, str]] = []
    cur_title = "Лор мира"
    cur: list[str] = []
    for ln in lines:
        m = re.match(r"^#{1,4}\s+(.+?)\s*#*\s*$", ln.strip())
        if m:
            if "".join(cur).strip():
                blocks.append((cur_title, "\n".join(cur).strip()))
            cur_title = m.group(1).strip()
            cur = []
        else:
            cur.append(ln)
    if "".join(cur).strip():
        blocks.append((cur_title, "\n".join(cur).strip()))
    return [db.create_lore(world_id, t, c, tags="", is_core=False, source="custom")
            for t, c in blocks if c]


def _chunk_lore(content: str, max_tokens: int = 550) -> list[str]:
    """Разбивает очень большую статью лора на чанки по границам абзацев/предложений."""
    content = (content or "").strip()
    if not content:
        return []
    if est_tokens(content) <= max_tokens:
        return [content]
    paras = [p.strip() for p in re.split(r"\n\s*\n", content) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        if buf and est_tokens(buf) + est_tokens(p) > max_tokens:
            # всё ещё большой абзац — режем по предложениям
            while p and est_tokens(p) > max_tokens:
                cut = 0
                acc = ""
                for sent in re.split(r"(?<=[.!?…])\s+", p):
                    if acc and est_tokens(acc) + est_tokens(sent) > max_tokens:
                        break
                    acc += (" " if acc else "") + sent
                    cut = len(acc)
                if not cut:
                    cut = max_tokens * 3
                if buf:
                    chunks.append(buf.strip())
                    buf = ""
                chunks.append(p[:cut].strip())
                p = p[cut:].strip()
            if p:
                buf = p
            continue
        buf += ("\n\n" if buf else "") + p
    if buf.strip():
        chunks.append(buf.strip())
    return chunks or [content]


async def index_lore_entry(world_id: int, entry: dict, provider: dict | None = None) -> None:
    """Индексирует статью лора в Chroma (чанки, kind='lore', meta с lore_id/title).
    Сначала удаляет старые чанки статьи — при правке не накапливается мусор."""
    try:
        chunks = _chunk_lore(entry.get("content") or "")
        if not chunks:
            return
        try:
            await chroma_client.delete_by_where({"$and": [{"world_id": world_id}, {"kind": "lore"},
                                                           {"lore_id": entry["id"]}]})
        except Exception as e:
            # старые чанки могут остаться в поиске — переиндексация их перетрёт, но факт виден
            log.debug("лор: чистка старых чанков статьи %s (world %s): %s",
                      entry.get("id"), world_id, e)
        docs = [f"[ЛОР: {entry.get('title','')}] {c}" for c in chunks]
        vecs = await embeddings.embed_documents(docs, provider=provider)
        if not vecs:
            return
        await chroma_client.add(
            ids=[f"lore_{world_id}_{entry['id']}_{i}" for i in range(len(chunks))],
            embeddings=vecs,
            metadatas=[{"world_id": world_id, "kind": "lore", "lore_id": entry["id"],
                        "title": entry.get("title", ""), "chunk": i} for i in range(len(chunks))],
            documents=docs,
        )
    except Exception as e:
        if provider and provider.get("enabled"):
            log.warning("index_lore_entry error (world %s lore %s): %s", world_id, entry.get("id"), e)


async def index_all_lore(world_id: int, provider: dict | None = None) -> None:
    """Переиндексирует весь лор мира (сидинг после создания / пересборка)."""
    for e in db.list_lore(world_id):
        await index_lore_entry(world_id, e, provider)


async def retrieve_lore(world_id: int, action: str, setting: dict,
                        providers: dict | None = None,
                        budget: int | None = None) -> list[str]:
    """Собирает блок [ЛОР МИРА] для промпта:
    1. якорные (is_core) статьи — всегда, сжато по бюджету;
    2. RAG-поиск по лору (чанки под действие игрока) — если эмбеддинги доступны;
    3. фолбэк без эмбеддингов — свежие статьи по порядку в пределах бюджета.
    Возвращает список строк «📖 Заголовок: текст…» (готовый блок)."""
    entries = db.list_lore(world_id)
    if not entries:
        return []
    cfg = get_config()
    # Динамический бюджет/чанки от размера контекста мира: при 128k/256k окне лор не
    # упирается в фиксированные 900 токенов/3 чанка (жалоба: «всегда 9 фактов»).
    world = db.get_world(world_id)
    if budget is None and world:
        from .narrator import dynamic_lore_budget, dynamic_memory_k
        budget = dynamic_lore_budget(world, cfg.lore_token_budget, cfg.lore_token_budget_max)
        lore_k = dynamic_memory_k(world, cfg.lore_rag_k, cfg.lore_rag_k_max)
    else:
        budget = max(200, budget or cfg.lore_token_budget)
        lore_k = cfg.lore_rag_k
    out: list[str] = []
    used = 0

    def _push(title: str, text: str) -> None:
        nonlocal used
        t = est_tokens(text)
        if used + t > budget:
            allow = max(80, budget - used)
            text = text[:allow * 3]
            t = est_tokens(text)
        if t <= 0:
            return
        out.append(f"📖 {title}: {text}")
        used += t

    # 1) Якорные статьи (всегда, даже без RAG)
    for e in entries:
        if e.get("is_core") and used < budget:
            _push(e["title"], e["content"])

    # 2) RAG по лору
    rag_added = 0
    emb_prov = (providers or {}).get("embedding")
    query = memory_query_text(world_id, action, setting)
    try:
        qv = await embeddings.embed_query(query, provider=emb_prov)
        if qv:
            results = await chroma_client.query(
                qv, n_results=max(cfg.rag_candidates, lore_k + 6),
                where={"$and": [{"world_id": world_id}, {"kind": "lore"}]})
            cands = []
            for r in results:
                cos = chroma_client.cosine_from_distance(r.get("distance", 2.0))
                if cos < cosine_threshold(cfg, emb_prov):
                    continue
                cands.append({"content": r.get("content", ""), "similarity": cos,
                              "title": (r.get("metadata") or {}).get("title", "")})
            if cands:
                cands = embeddings.hybrid_rerank(query, cands, weight_bm25=cfg.hybrid_weight_bm25)
                if cfg.rerank_enabled:
                    cands = await embeddings.rerank_results(query, cands,
                                                            top_n=lore_k,
                                                            provider=(providers or {}).get("rerank"))
                for c in cands[:lore_k]:
                    if used >= budget:
                        break
                    _push(c.get("title") or "Лор", c.get("content", ""))
                    rag_added += 1
    except Exception:
        pass  # RAG по лору не критичен — фолбэк ниже

    # 3) Фолбэк без эмбеддингов (или если RAG вернул пусто): свежие статьи по порядку
    if rag_added == 0:
        for e in entries:
            if used >= budget:
                break
            if e.get("is_core"):
                continue  # уже добавлены
            _push(e["title"], e["content"])
    return out
