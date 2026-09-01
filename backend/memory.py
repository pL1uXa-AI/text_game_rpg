# -*- coding: utf-8 -*-
"""
memory.py — гибридная долгосрочная память мира (декомпозиция narrator.py, сессия 30).

Собрано из narrator.py то, что относится к ПАМЯТИ (verbatim-перенос, семантика не менялась):
- векторная память: retrieve_memory, index_exchange, index_summary (+ helpers);
- сводки: summarize_and_compress, _make_summary, world_main_provider;
- карточки сущностей: select_relevant_entities, update_entity_cards, index_entities;
- карточки знаний: ensure_knowledge_cards + helpers (_origin_for, _upsert_knowledge).

Слои памяти (никто и ничто не забывается):
1. краткосрочная — последние несвёрнутые обмены (SQLite events, роль player/narrator);
2. сводки — старые события сворачиваются LLM в 2–5 предложений (роль summary);
3. векторная (RAG) — ВСЕ обмены/сводки/карточки индексируются в свою ChromaDB;
4. карточки сущностей — NPC/локации/фракции/квесты/предметы/события (LLM-архивариус);
5. карточки знаний — расы/классы/профессии/навыки/эффекты/предметы (детерминированно);
6. лор мира — большие статьи вселенной (см. lore_retriever.py).

Публичное API сохраняет имена/сигнатуры narrator.py: narrator.retrieve_memory(...) и т.п.
продолжают работать через реэкспорт (см. конец narrator.py).
"""
from __future__ import annotations

import json
import re
from typing import Optional

from . import bg, chroma_client, db, embeddings, llm
from .config import est_tokens, get_config

from .logsetup import get_logger

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# Векторная память: RAG-поиск + индексация
# ══════════════════════════════════════════════════════════════

def memory_query_text(world_id: int, action: str, setting: dict) -> str:
    """Текст запроса к памяти: действие + текущая локация + активные квесты
    (чтобы RAG искал релевантное и по контексту места/сюжета)."""
    loc = setting.get("locations", {}).get(setting.get("current_location", ""), {})
    quests = " ".join(q.get("title", "") for q in setting.get("quests", {}).values() if q.get("status") == "active")
    return f"Действие: {action}. Локация: {loc.get('name', '')}. Квесты: {quests}"


def cosine_threshold(cfg, emb_prov) -> float:
    """Порог косинусной близости для отбора RAG-кандидатов.
    Для локального fastembed-провайдера порог ниже (эта модель даёт разрежённые/меньшие
    близости, чем облачный qwen3-embedding); иначе — глобальный cfg.cosine_threshold."""
    if emb_prov and emb_prov.get("id") == "local":
        return getattr(cfg, "cosine_threshold_local", 0.20)
    return cfg.cosine_threshold


async def retrieve_memory(world_id: int, action: str, setting: dict, k: Optional[int] = None,
                          providers: dict | None = None,
                          scores_out: list | None = None) -> list[str]:
    """RAG-память: релевантные фрагменты обменов/сводок/карточек по действию игрока.

    `scores_out` (если передан список) дополняется оценками релевантности в том же порядке,
    что и возвращённые куски: [{"similarity":..., "hybrid":..., "rerank":...}] — нужно для
    прозрачности RAG в UI (E3: «попало ли вспомненное в тему хода»), раньше был виден
    только счётчик «🧠 Память (N)». Паттерн `*_out` как у llm.stream_chat(tool_calls_out).
    """
    cfg = get_config()
    # B5: считаем события на стороне SQLite (было len(get_events(limit=4000)) — весь лог в Python)
    if db.count_events(world_id) < 3:
        return []
    # Динамический K: при большом контексте мира (128k/256k) вспоминаем больше фактов,
    # а не фиксированные RAG_MEMORY_K (жалоба: «рассказчик всегда вспоминает 9 фактов,
    # вне зависимости от контекста 262к»).
    if k is None:
        world = db.get_world(world_id)
        from .narrator import dynamic_memory_k
        k = dynamic_memory_k(world, cfg.rag_memory_k, cfg.rag_memory_max) if world else cfg.rag_memory_k
    emb_prov = (providers or {}).get("embedding")
    rerank_prov = (providers or {}).get("rerank")
    query = memory_query_text(world_id, action, setting)
    try:
        qv = await embeddings.embed_query(query, provider=emb_prov)
        if not qv:
            return []
        results = await chroma_client.query(qv, n_results=max(cfg.rag_candidates, k + 8),
                                            where={"world_id": world_id})
        if not results:
            return []
        out = []
        for r in results:
            cos = chroma_client.cosine_from_distance(r.get("distance", 2.0))
            if cos < cosine_threshold(cfg, emb_prov):
                continue
            out.append({"content": r.get("content", ""), "similarity": cos})
        if not out:
            return []
        out = embeddings.hybrid_rerank(query, out, weight_bm25=cfg.hybrid_weight_bm25)
        if cfg.rerank_enabled:
            out = await embeddings.rerank_results(query, out, top_n=k, provider=rerank_prov)
        out = out[:k]
        if scores_out is not None:
            # оценки, реально решившие отдачу: косинус, гибрид (BM25+косинус), реранкер
            for c in out:
                rr = c.get("_rerank")
                scores_out.append({
                    "similarity": round(float(c.get("similarity", 0) or 0), 3),
                    "hybrid": round(float(c.get("_hybrid", 0) or 0), 3),
                    "rerank": round(float(rr), 3) if rr is not None else None,
                })
        return [c["content"] for c in out]
    except Exception as e:
        # Память не критична — ход не роняем. НО молча глотать нельзя (AGENT.md, правило 14):
        # отказ Chroma/облака неотличим от «эмбеддинги выключены», и диагностика теряется.
        # Логируем с контекстом; легальный фолбэк (пустой вектор при выключенных эмбеддингах)
        # возвращается выше через `if not qv: return []` и сюда не доходит.
        log.warning("retrieve_memory (world %s): RAG недоступен — %s", world_id, e)
        return []


async def index_exchange(world_id: int, seq: int, action: str, reply: str,
                         provider: dict | None = None) -> None:
    """Индексирует обмен (действие → результат) в ChromaDB."""
    doc = f"Действие игрока: {action}\nРезультат: {reply[:1200]}"
    try:
        vec = await embeddings.embed_documents([doc], provider=provider)
        if not vec:
            return
        await chroma_client.add(
            ids=[f"ex_{world_id}_{seq}"],
            embeddings=vec,
            metadatas=[{"world_id": world_id, "seq": seq, "kind": "exchange"}],
            documents=[doc],
        )
    except Exception as e:
        if provider and provider.get("enabled"):
            log.warning("index_exchange error (world %s seq %s): %s", world_id, seq, e)


async def index_summary(world_id: int, seq: int, summary: str, provider: dict | None = None) -> None:
    try:
        vec = await embeddings.embed_documents([summary], provider=provider)
        if not vec:
            return
        await chroma_client.add(
            ids=[f"sum_{world_id}_{seq}"],
            embeddings=vec,
            metadatas=[{"world_id": world_id, "seq": seq, "kind": "summary"}],
            documents=[summary],
        )
    except Exception as e:
        if provider and provider.get("enabled"):
            log.warning("index_summary error (world %s seq %s): %s", world_id, seq, e)


# ══════════════════════════════════════════════════════════════
# Сводки (сжатие старых событий)
# ══════════════════════════════════════════════════════════════

def _provider_settings(world: dict) -> dict:
    try:
        ws = json.loads(world.get("provider_settings") or "{}")
        return ws if isinstance(ws, dict) else {}
    except Exception:
        return {}


def world_main_provider(world: dict) -> dict:
    """Основной провайдер LLM мира (учитывает per-world переопределения)."""
    return get_config().get_provider("main", _provider_settings(world).get("main"))


async def _make_summary(text: str, lang: str = "ru", provider: dict | None = None) -> str:
    lang_instr = ("Создай краткую сводку на русском языке" if lang == "ru"
                  else "Create a concise summary in English")
    messages = [
        {"role": "system", "content": "Ты — архивариус текстовой RPG. Ты конспектируешь произошедшее для будущих ходов."},
        {"role": "user", "content": f"{lang_instr} (2–5 предложений): сохрани ключевые факты: кто, где, что сделал, "
                                    f"какие результаты, кого встретил, что получил/потерял. Не добавляй от себя.\n\n{text}"},
    ]
    try:
        return (await llm.complete(messages, temperature=0.4, max_tokens=400, provider=provider)).strip()
    except Exception:
        return ""


async def summarize_and_compress(world_id: int, provider: dict | None = None) -> None:
    """Сворачивает старые события в сводку при переполнении недавнего окна."""
    cfg = get_config()
    world = db.get_world(world_id)
    # локальный импорт: world_recent_budget живёт в narrator (бюджеты контекста)
    from .narrator import world_recent_budget
    budget = world_recent_budget(world) if world else cfg.recent_token_budget
    # Б5: окно недавней истории умещается в токен-бюджет, поэтому целиком мир из БД тянуть
    # незачем — берём с запасом последние события (×6 на случай коротких реплик).
    unfolded = db.get_unfolded_events(world_id, limit=max(60, int(budget / 60) * 6))
    if not unfolded:
        return

    used = 0
    keep: list[dict] = []
    to_fold: list[dict] = []
    for e in unfolded:
        t = est_tokens(e["content"])
        if used + t > budget and keep:
            to_fold.append(e)
        else:
            used += t
            keep.append(e)
    # Сворачиваем только если сэкономим заметно (иначе дёргаем LLM впустую)
    if not to_fold or len(to_fold) < 2:
        return
    folded_tokens = sum(est_tokens(e["content"]) for e in to_fold)
    if folded_tokens < 500:
        return

    fold_text = "\n".join(
        f"Игрок: {e['content']}" if e["role"] == "player" else f"Рассказчик: {e['content'][:400]}"
        for e in to_fold
    )
    world = db.get_world(world_id)
    lang = world["language"] if world else "ru"
    wprov = get_config().resolve_world_providers(_provider_settings(world))
    summary = await _make_summary(fold_text, lang, provider=provider or wprov["main"])
    if not summary:
        return
    seq = db.latest_seq(world_id) + 1
    last_fold_seq = to_fold[-1]["seq"]
    # A1 (сессия 34): сводка ПОКРЫВАЕТ конкретный диапазон seq. Без этой отметки нельзя
    # отличить «свёрнуто в сводку (разворачивать не надо)» от «сокрыто перемоткой
    # (обязательно вернуть)» — именно из-за этого загрузка сохранения когда-то съела всю
    # недавнюю историю. Пишем в meta события-сводки (роль summary = служебная, не художка).
    db.add_event(world_id, "summary", summary, seq,
                 meta={"covers": {"from": to_fold[0]["seq"], "to": last_fold_seq,
                                  "tokens": folded_tokens}})
    db.mark_folded(world_id, last_fold_seq)
    await index_summary(world_id, seq, summary, provider=wprov["embedding"])


# ═══════════════════════════════════════════════════════════
# Карточки сущностей (LLM-архивариус) + знания (детерминированно)
# ═══════════════════════════════════════════════════════════

def compact_entity(e: dict) -> str:
    """Компактная строка карточки для подачи в контекст модели."""
    meta = e.get("meta") or {}
    if isinstance(e.get("meta"), str):
        try:
            meta = json.loads(e["meta"])
        except Exception:
            meta = {}
    kind_icon = {"npc": "Персонаж", "location": "Локация", "faction": "Фракция",
                 "quest": "Квест", "item": "Предмет", "event": "Событие",
                 "enemy": "Враг", "shop": "Магазин",
                 "companion": "Компаньон", "craft": "Рецепт крафта",
                 "race": "Раса", "class": "Класс", "profession": "Профессия",
                 "skill": "Навык", "effect": "Эффект", "magic": "Магия"}.get(e.get("kind", ""), e.get("kind", ""))
    parts = [f"[{kind_icon} «{e.get('name', e.get('entity_key'))}»]"]
    if e.get("summary"):
        parts.append(f"Статус: {e['summary'][:200]}")
    if e.get("relationship"):
        parts.append(f"Отношения с игроком: {e['relationship'][:120]}")
    if e.get("bio"):
        parts.append(f"История: {e['bio'][:420]}")
    extra = []
    if meta.get("alive") is False:
        extra.append("МЁРТВ")
    if meta.get("location"):
        extra.append(f"где: {meta['location']}")
    if meta.get("faction"):
        extra.append(f"фракция: {meta['faction']}")
    if meta.get("rank") and e.get("kind") in ("class", "skill"):
        extra.append(f"ранг: {meta['rank']}")
    if e.get("kind") == "race":
        if meta.get("bonus"):
            extra.append("бонусы: " + ", ".join(f"{k}{v:+}" for k, v in meta["bonus"].items()))
        if meta.get("passive"):
            extra.append(f"пассивка: {meta['passive']}")
    if e.get("kind") == "class" and meta.get("stat"):
        extra.append(f"осн. стат: {meta['stat']}")
    if e.get("kind") == "effect":
        dmg = int(meta.get("damage", 0) or 0)
        if dmg:
            extra.append(f"урон {dmg} HP/ход")
        heal = int(meta.get("heal", 0) or 0)
        if heal:
            extra.append(f"лечение {heal} HP/ход")
        stk = int(meta.get("stacks", 1) or 1)
        if stk > 1:
            extra.append(f"стаков {stk}")
        if meta.get("turns") and meta.get("turns") not in (-1, None):
            extra.append(f"{meta['turns']} ход.")
        if meta.get("mods"):
            extra.append("моды: " + ", ".join(f"{k}{v:+}" for k, v in meta["mods"].items()))
    if e.get("kind") == "profession" and meta.get("buff"):
        extra.append(f"бафф: {meta['buff']}")
    if meta.get("origin"):
        o = meta["origin"]
        if isinstance(o, list):
            o = "; ".join(str(x) for x in o)
        extra.append(f"получено: {str(o)[:140]}")
    if extra:
        parts.append("; ".join(extra))
    return "\n".join(parts)


# Сколько последних карточек мира загрушает в память «окна сцены» на один ход
# (select_relevant_entities / update_entity_cards). Значение с запасом: столько живых
# сущностей в одном мире практически не бывает, но на многомесячных играх оно
# ограничивает выборку БД (сессия 36, п.9).
_SCENE_CARDS_WINDOW = 400


def _merge_cards(cards: list[dict], fresh: list[dict]) -> list[dict]:
    """Обновляет окно карточек мира свежезаписанными (по уникальному ключу
    kind+entity_key) вместо второго запроса к БД (сессия 36, п.12)."""
    if not fresh:
        return cards
    idx = {(c.get("kind"), c.get("entity_key")): i for i, c in enumerate(cards)}
    out = list(cards)
    for c in fresh:
        key = (c.get("kind"), c.get("entity_key"))
        if key in idx:
            out[idx[key]] = c
        else:
            out.append(c)
    return out


def format_entity_cards(cards: list[dict]) -> str:
    return "\n\n".join(compact_entity(c) for c in cards)


def select_relevant_entities(world_id: int, setting: dict, action: str, limit: int = 10) -> list[dict]:
    """Выбирает карточки, важные для текущего хода:
    живые NPC и фракции, текущая локация, активные квесты, знания (раса/класс/навыки/эффекты)
    + явные упоминания в действии.

    Сессия 36, п.9: вызывается СИНХРОННО на каждый ход, поэтому карточки берутся
    ограниченной выдачей (последние по updated_at), а не все до единой: на длинной игре
    с тысячами карточек полная выборка жрёт память и держит БД. Приоритетная сортировка
    работает по этому окну (самые свежие карточки и есть самые вероятные релевантные).
    """
    # выдача идёт от самых свежих (updated_at DESC) — при равных приоритетах
    # sorted() сохраняет именно этот порядок: в контекст попадают недавние карточки
    all_cards = db.list_entities(world_id, limit=_SCENE_CARDS_WINDOW)
    if not all_cards:
        return []
    cur_loc = setting.get("current_location", "")
    active_quests = [k for k, v in setting.get("quests", {}).items() if v.get("status") == "active"]
    p = setting.get("player", {})
    known = set()
    for k in ("race", "class", "secondary_class", "profession"):
        if p.get(k):
            known.add(str(p.get(k)).lower())
    for k in (p.get("skills") or {}):
        known.add(str(k).lower())
    for k in (p.get("effects") or {}):
        known.add(str(k).lower())

    def priority(c: dict) -> float:
        pv = 0.0
        kind = c.get("kind")
        # Дневник (kind=journal, сессия 34 / C2) — отдельный слой для ИГРОКА, а не для
        # модели: хроника не должна попадать в контекст и вытеснять живые карточки NPC.
        if kind == "journal":
            return -100.0
        try:
            meta = json.loads(c.get("meta") or "{}")
        except Exception:
            meta = {}
        if kind == "location" and c.get("entity_key") == cur_loc:
            pv += 5.0
        if kind == "npc" and meta.get("location") == cur_loc:
            pv += 3.0
        if kind == "npc" and meta.get("alive", True) is not False:
            pv += 1.5
        if kind == "quest" and c.get("entity_key") in active_quests:
            pv += 4.0  # активные квесты — приоритетнее знаний (сессия 32: не теряются в топе)
        if kind in ("race", "class", "profession", "skill", "effect"):
            pv += 1.5
            if str(c.get("entity_key", "")).lower() in known:
                pv += 3.0
        elif kind == "quest" and c.get("entity_key") not in active_quests:
            pv += 1.0  # выполненные/неактивные квесты — ниже активных, но выше «мусора»
        if kind == "faction":
            pv += 2.0
        if kind == "event":
            pv += 0.3
        # явное упоминание имени в действии (стемминг: «гримуаром» матчит «гримуар»)
        hay = f"{c.get('name','')} {c.get('entity_key','')}".lower()
        for w in re.findall(r"[а-яА-ЯёЁa-zA-Z]{3,}", action.lower()):
            stem = w[:max(3, len(w) - 2)]  # отбрасываем окончание
            if len(w) > 2 and (w in hay or stem in hay):
                pv += 4.0
                break
        return pv

    scored = sorted(all_cards, key=priority, reverse=True)
    # Карточки дневника (kind=journal) — слой для ИГРОКА (вкладка «Дневник»), их НЕ
    # должно быть в контексте модели: хроника вытеснила бы живые карточки NPC из окна.
    return [c for c in scored[:limit] if c.get("kind") != "journal"]


def _origin_for(name: str, origins: list[str] | None) -> str | None:
    """Ищет среди системных сообщений хода то, где упомянуто имя (условие получения)."""
    if not origins or not name:
        return None
    low = name.lower()
    for m in origins:
        if m and low in m.lower():
            return m[:160]
    return None


def _upsert_knowledge(world_id: int, kind: str, key: str, origins: list[str] | None, seq: int, *,
                      name: str, summary: str = "", meta: dict | None = None,
                      bio_add: str | None = None) -> dict:
    """Создаёт/обновляет карточку знаний; на новую записывает условие получения (origin)."""
    was = db.get_entity(world_id, kind, key)
    meta_out = dict(meta or {})
    origin = _origin_for(key, origins)
    if origin:
        cur = meta_out.get("origin")
        cur = cur if isinstance(cur, list) else ([] if not cur else [str(cur)])
        if origin not in cur:
            cur = (cur + [origin])[-3:]
            meta_out["origin"] = cur
    elif was and not meta_out.get("origin") and (was.get("meta") or "{}"):
        try:
            old_meta = json.loads(was["meta"])
            if old_meta.get("origin") and "origin" not in meta_out:
                meta_out["origin"] = old_meta["origin"]
        except Exception as e:
            log.warning("чтение meta карточки (world %s, %s/%s): %s", world_id, kind, key, e)
    return db.upsert_entity(world_id, kind, key, name=name, summary=summary,
                            bio_add=bio_add, meta=meta_out, seq=seq)


def ensure_knowledge_cards(world_id: int, setting: dict, seq: int = 0,
                           origins: list[str] | None = None) -> list[dict]:
    """Держит в памяти карточки созданных рас/классов/профессий/навыков/эффектов/предметов,
    чтобы рассказчик не противоречил сам себе в будущем. Возвращает созданные/обновлённые
    карточки (для индексации в Chroma). Творческие расы/профессии читаются из полей игрока
    (race_bonus / profession_buff / эффект с tag). origins — системные сообщения хода:
    на новых карточках фиксируется условие получения."""
    from .mechanics import RACES, CLASSES, PROFESSIONS  # справочники (локальный импорт)

    out: list[dict] = []
    try:
        p = setting.get("player") or {}
        race = (p.get("race") or "").strip()
        cls = (p.get("class") or "").strip()
        sc = (p.get("secondary_class") or "").strip()
        prof = (p.get("profession") or "").strip()
        all_effects = p.get("effects") or {}
        if race:
            built = next((r for r in RACES if r["name"].lower() == race.lower()), None)
            bonus = p.get("race_bonus") or (built.get("bonus") if built else None) or {}
            passive = None
            for _n, _ef in all_effects.items():
                if isinstance(_ef, dict) and _ef.get("tag") == "race":
                    passive = {"name": _n, "desc": _ef.get("desc", "")}
                    break
            if not passive and built:
                passive = built["passive"]
            meta = {"passive": (passive or {}).get("name", "") or "", "bonus": bonus or {}}
            summary = ((passive or {}).get("desc") or (built.get("desc") if built else "") or "")[:200]
            out.append(_upsert_knowledge(world_id, "race", race, origins, seq, name=race, summary=summary,
                                         bio_add=built["desc"] if built else None, meta=meta))
        for key, rank in ((cls, p.get("class_rank", "F")), (sc, p.get("secondary_rank", "F"))):
            if not key:
                continue
            built_c = next((c for c in CLASSES if c["name"].lower() == key.lower()), None)
            out.append(_upsert_knowledge(world_id, "class", key, origins, seq, name=key,
                                         summary=(built_c.get("desc") if built_c else "")[:200],
                                         meta={"rank": rank, "stat": built_c.get("stat") if built_c else ""}))
        if prof:
            built_p = next((pf for pf in PROFESSIONS if pf["name"].lower() == prof.lower()), None)
            buff = p.get("profession_buff") or (built_p.get("buff") if built_p else None) or {}
            out.append(_upsert_knowledge(world_id, "profession", prof, origins, seq, name=prof,
                                         summary=(built_p.get("desc") if built_p else "")[:200],
                                         meta={"buff": buff or {}}))
        for name, sk in (p.get("skills") or {}).items():
            if isinstance(sk, dict):
                out.append(_upsert_knowledge(world_id, "skill", name, origins, seq, name=name,
                                             summary=(sk.get("desc") or "")[:200],
                                             meta={"rank": sk.get("rank", "F"), "kind": sk.get("kind", ""),
                                                   "mp_cost": sk.get("mp_cost", 0)}))
        for name, ab in (p.get("abilities") or {}).items():
            if isinstance(ab, dict):
                out.append(_upsert_knowledge(world_id, "skill", name, origins, seq, name=name,
                                             summary=(ab.get("desc") or "")[:200],
                                             meta={"kind": f"способность/{ab.get('school') or ''}",
                                                   "rank": "", "mp_cost": ab.get("cost", 0)}))
        for name, ef in all_effects.items():
            if isinstance(ef, dict):
                out.append(_upsert_knowledge(world_id, "effect", name, origins, seq, name=name,
                                             summary=(ef.get("desc") or "")[:200],
                                             meta={"kind": ef.get("kind", "особый"), "turns": ef.get("turns", -1),
                                                   "damage": ef.get("damage", 0), "heal": ef.get("heal", 0),
                                                   "stacks": ef.get("stacks", 1), "mods": ef.get("mods") or {}}))
        # предметы инвентаря — тоже в карточки знаний (условие получения из «Получено: …»)
        for item in (p.get("inventory") or []):
            iname = str(item.get("name") or "").strip()[:120]
            if not iname:
                continue
            desc = str(item.get("desc") or "")[:200]
            out.append(_upsert_knowledge(world_id, "item", iname, origins, seq, name=iname,
                                         summary=desc or "в инвентаре игрока"))
        # Квесты из механического состояния — в карточки (чтобы вкладка «Карточки» видела
        # активные квесты на старте/в любой момент, а не только после фонового архивариуса)
        for qid, q in (setting.get("quests") or {}).items():
            qid = str(qid or "").strip()
            if not qid or not isinstance(q, dict):
                continue
            status = str(q.get("status") or "active").lower()
            meta = {"status": "done" if status in ("done", "completed", "выполнен", "завершён") else "active"}
            cur = q.get("current")
            if cur is not None:
                meta["step"] = cur
            if q.get("chosen"):
                meta["branch"] = str(q["chosen"])
            out.append(_upsert_knowledge(world_id, "quest", qid, origins, seq,
                                         name=str(q.get("title") or qid)[:200],
                                         summary=str(q.get("desc") or "")[:200],
                                         bio_add=None,
                                         meta=meta))
    except Exception as e:
        log.warning("карточки знаний предметов (world %s): %s", world_id, e)
    return out


async def update_entity_cards(world_id: int, action: str, reply: str,
                              provider: dict | None = None) -> list[dict]:
    """Фоновое обновление карточек сущностей по последнему обмену (LLM → JSON).
    Возвращает сохранённые/обновлённые карточки."""
    world = db.get_world(world_id)
    if not world:
        return []
    provider = provider or world_main_provider(world)
    # Сессия 36, п.9/п.12: ОДНА ограниченная выборка карточек на вызов (было 4 полных
    # db.list_entities — каждая тянула все карточки мира в память на каждый ход).
    all_cards = db.list_entities(world_id, limit=_SCENE_CARDS_WINDOW)
    current = all_cards[-25:]
    lang = world.get("language", "ru")
    current_text = "\n\n".join(compact_entity(c) for c in current) or "(карточек пока нет)"
    lang_instr = (
        "Используй ключи и язык: русский. " if lang == "ru"
        else "Use keys and language: English. "
    )
    messages = [
        {"role": "system", "content": (
            "Ты — архивариус живого мира RPG. Ты ведёшь карточки сущностей: персонажей (npc), локаций (location), "
            "фракций (faction), квестов (quest), предметов (item), врагов (enemy), магазинов (shop), "
            "компаньонов (companion), рецептов крафта (craft) и важных событий (event). "
            "Ответь ТОЛЬКО валидным JSON без пояснений и markdown: "
            '{"entities":[{"kind":"npc","key":"barman","name":"Трактирщик Барт",'
            '"summary":"сейчас в таверне, устал","relationship":"друг, доверяет 7/10","bio_add":"новый факт",'
            '"meta":{"alive":true,"location":"tavern"}}]} '
            "kind: npc|location|faction|quest|item|event|enemy|shop|companion|craft. key — короткий латинский идентификатор. "
            "bio_add — один новый факт, которого ещё нет в истории, иначе пропусти. "
            "Если ничего не изменилось — верни {\"entities\":[]}. Обновляй ТОЛЬКО затронутые сущности."
        )},
        {"role": "user", "content": (
            f"{lang_instr}Обновление карточек после нового хода:\n\n"
            f"Текущие карточки:\n{current_text}\n\n"
            f"Действие игрока: {action}\n"
            f"Событие мира/ответ: {reply[:1500]}\n\n"
            "Верни JSON."
        )},
    ]
    try:
        from .narrator import llm_json_tool
        data = await llm_json_tool(
            messages, "update_entities",
            "Обновление карточек сущностей мира после хода.",
            {
                "entities": {"type": "array", "description": "Список затронутых сущностей",
                    "items": {"type": "object", "properties": {
                        "kind": {"type": "string", "description": "npc|location|faction|quest|item|event|enemy|shop|companion|craft"},
                        "key": {"type": "string", "description": "Короткий латинский id"},
                        "name": {"type": "string"},
                        "summary": {"type": "string", "description": "Текущий статус"},
                        "relationship": {"type": "string", "description": "Отношения с игроком"},
                        "bio_add": {"type": "string", "description": "Один новый факт"},
                        "meta": {"type": "object"},
                    }, "required": ["kind", "key"]}},
            },
            ["entities"], provider=provider, temperature=0.2, max_tokens=900, max_attempts=2)
        if not data:
            return []
    except Exception:
        return []
    ents_list = data.get("entities") or []
    if not isinstance(ents_list, list):
        return []

    saved = []
    for item in ents_list:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "")).strip().lower()
        key = str(item.get("key", "")).strip()
        if kind not in ("npc", "location", "faction", "quest", "item", "event",
                        "enemy", "shop", "companion", "craft") or not key:
            continue
        name = str(item.get("name") or key)[:120]
        summary = str(item.get("summary") or "")[:400]
        relationship = str(item.get("relationship") or "")[:200]
        bio_add = str(item.get("bio_add") or "").strip()[:500]
        meta = item.get("meta") or {}
        if not isinstance(meta, dict):
            meta = {}
        seq = db.latest_seq(world_id)
        ent = db.upsert_entity(world_id, kind, key, name=name, summary=summary,
                               relationship=relationship, bio_add=bio_add, meta=meta, seq=seq)
        saved.append(ent)
    # переиндексация изменённых карточек в Chroma (память поиска)
    if saved:
        try:
            await index_entities(world_id, saved)
        except Exception as e:
            log.warning("индексация карточек (world %s): %s", world_id, e)
    # ── Синхронизация карточек архивариуса в механическое состояние setting ──
    # Карточки, заведённые архивариусом (квест/NPC/магазин/враг/спутник/рецепт), должны
    # быть видны и в своих разделах вкладки «Состояние», а не только во вкладке
    # «Карточки» — иначе сущность «теряется» между вкладками.
    #
    # Сессия 36, п.12 (и найденный при проверке РЕАЛЬНЫЙ дефект): раньше это были три
    # независимых блока, каждый из которых заново парсил ОДНУ И ТУ ЖЕ строку
    # world["setting"] и в конце писал db.update_world(setting=...). Поздний блок затирая
    # правки раннего: квест, синхронизированным первым, исчезал, стоило второму блоку
    # записать свой «свежераспарсенный» setting. Теперь состояние парсится ОДИН раз,
    # все правки накапливаются в одном объекте, и запись в БД — одна в конце.
    #
    # Окно карточек = снятое в начале вызова + обновлённые этим прогоном (один список,
    # без повторных выборок из БД).
    all_cards = _merge_cards(all_cards, saved)
    # Пока архивариус думал (секунды LLM-прохода), игрок мог сходить: world снят в начале
    # вызова, и синхронизация поверх него откатила бы setting к началу хода — берём свежак.
    # А если для мира идёт перегенерация (↻ откатывает состояние к снапшоту и пересчитывает
    # его), синхронизацию пропускаем вовсе: карточки уже записаны в entities, а setting
    # пересоберёт новый ответ — наша правка здесь означала бы потерю/двоение механики
    # (п.3A).
    if bg.is_regenerating(world_id):
        log.debug("архивариус (world %s): идёт перегенерация — синхронизация в setting "
                  "пропущена", world_id)
        return saved
    fresh = db.get_world(world_id) or world
    try:
        raw = fresh.get("setting") or "{}"
        setting = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(setting, dict):
            raise ValueError("setting не объект")
    except Exception as e:
        log.warning("синхронизация карточек (world %s): состояние не читается: %s",
                    world_id, e)
        return saved

    def _card_meta(e):
        m = e.get("meta") or {}
        if isinstance(m, str):
            try:
                m = json.loads(m)
            except Exception:
                m = {}
        return m if isinstance(m, dict) else {}

    changed = False
    try:
        # Квесты: не затираем прогресс/описание ручного квеста — только заводим новые
        # и завершаем те, что архивариус пометил выполненными.
        qs = setting.setdefault("quests", {})
        if not isinstance(qs, dict):
            setting["quests"] = qs = {}
        for e in (c for c in all_cards if c.get("kind") == "quest"):
            key = e.get("entity_key") or ""
            if not key:
                continue
            meta = _card_meta(e)
            status = ("done" if str(meta.get("status", "")).strip().lower()
                      in ("done", "completed", "выполнен", "завершён") else "active")
            existing = qs.get(key)
            if existing:
                if not isinstance(existing, dict):
                    continue
                # прежняя семантика: НЕ трогаем прогресс/описание ручного квеста;
                # завершаем только тот, что ещё активен в мире и помечен выполненным
                # в карточке (иначе «done» архивариуса пересилил бы сюжетный статус)
                if existing.get("status") == "active" and status == "done":
                    existing["status"] = "done"
                    changed = True
                continue
            qs[key] = {"id": key, "title": e.get("name") or key,
                       "desc": e.get("summary") or "", "status": status}
            changed = True
    except Exception as e:
        log.warning("синхронизация квестов (world %s): %s", world_id, e)

    try:
        # NPC: поля (mood/alive/faction/location), заданные директивами/вручную, не трогает
        npc = setting.setdefault("npc", {})
        if not isinstance(npc, dict):
            setting["npc"] = npc = {}
        for e in (c for c in all_cards if c.get("kind") == "npc"):
            key = e.get("entity_key") or ""
            if not key or key in npc:
                continue
            meta = _card_meta(e)
            npc[key] = {
                "name": e.get("name") or key,
                "mood": e.get("summary") or "",
                "desc": e.get("bio") or e.get("summary") or "",
                "alive": meta.get("alive", True),
                "faction": meta.get("faction", ""),
            }
            changed = True
    except Exception as e:
        log.warning("синхронизация NPC (world %s): %s", world_id, e)

    try:
        # Магазины/враги/спутники/рецепты — только отсутствующие (без перезаписи полей,
        # выставленных директивами или мастером).
        kind_to_setting = {"shop": "shops", "enemy": "enemies",
                           "companion": "companions", "craft": "crafts"}
        for kind, sec in kind_to_setting.items():
            target = setting.setdefault(sec, {})
            if not isinstance(target, dict):
                setting[sec] = target = {}
            for e in (c for c in all_cards if c.get("kind") == kind):
                key = e.get("entity_key") or ""
                if not key or key in target:
                    continue
                meta = _card_meta(e)
                name = e.get("name") or key
                if kind == "shop":
                    target[key] = {"id": key, "name": name, "owner": meta.get("owner", ""),
                                   "faction": meta.get("faction", ""), "location": meta.get("location", ""),
                                   "items": [], "desc": e.get("summary") or ""}
                elif kind == "enemy":
                    hp = meta.get("hp") or meta.get("max_hp") or 20
                    target[key] = {"name": name, "hp": hp, "max_hp": hp,
                                   "dmg": meta.get("dmg", 4), "desc": e.get("bio") or e.get("summary") or ""}
                elif kind == "companion":
                    hp = meta.get("hp") or meta.get("max_hp") or 30
                    target[key] = {"id": key, "name": name, "hp": hp, "max_hp": max(hp, meta.get("max_hp") or hp),
                                   "level": meta.get("level", 1), "loyalty": meta.get("loyalty", 0),
                                   "faction": meta.get("faction", ""), "skills": meta.get("skills") or {},
                                   "desc": e.get("bio") or e.get("summary") or ""}
                elif kind == "craft":
                    target[key] = {"id": key, "name": name, "ingredients": [], "result": {},
                                   "desc": e.get("bio") or e.get("summary") or ""}
                changed = True
    except Exception as e:
        log.warning("синхронизация магазинов/врагов/компаньонов/крафтов (world %s): %s", world_id, e)

    # ЕДИНСТВЕННАЯ запись за проход: накопленные правки всех секций сразу.
    if changed:
        try:
            db.update_world(world_id, setting=setting)
        except Exception as e:
            log.warning("запись синхронизированного состояния (world %s): %s", world_id, e)
    return saved
async def index_entities(world_id: int, cards: list[dict]) -> None:
    """Индексирует/обновляет карточки в ChromaDB (перезапись по id)."""
    for e in cards:
        cid = f"ent_{world_id}_{e.get('kind','')}_{e.get('entity_key','')}"
        try:
            await chroma_client.delete_by_ids([cid.replace(' ', '_')])
        except Exception as e:
            log.warning("удаление-преиндекс карточки (world %s, %s): %s", world_id, cid, e)
    texts = [compact_entity(e) for e in cards]
    try:
        vecs = await embeddings.embed_documents(texts)
        ids = [f"ent_{world_id}_{e.get('kind','')}_{e.get('entity_key','')}".replace(' ', '_') for e in cards]
        metas = [{"world_id": world_id, "kind": "entity",
                  "entity_kind": e.get("kind", ""), "entity_key": e.get("entity_key", ""),
                  "entity_name": e.get("name", "")} for e in cards]
        await chroma_client.add(ids, vecs, metas, texts)
    except Exception as e:
        log.warning("индексация карточек (world %s): %s", world_id, e)
