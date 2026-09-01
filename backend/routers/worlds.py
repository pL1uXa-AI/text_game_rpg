# -*- coding: utf-8 -*-
"""Роутер миров: CRUD, действия (в т.ч. SSE-стриминг), слоты, настройки,
провайдеры per-world, обратная связь, режим мастера, экспорт, поиск по памяти."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from .. import bus, chroma_client, db, graph, llm, narrator, rewind, tts
from ..config import get_config, PROVIDER_OPTIONS
from ..ratelimit import make_guard
from ..schemas import (ActionIn, DivineIn, FeedbackIn, GenSettingsIn, ImportIn, PatchIn,
                       ProvidersIn, RewindIn, SaveIn, WorldCreate)
from .core import (
    _apply_provider_override,
    _background_memory,
    _context_guard,
    _ensure_action_len,
    _index_world_lore,
    _mask_provider,
    _masked_world_providers,
    _process_action,
    invalidate_turn_registry,
    _world_persona,
    _world_provider_settings,
    _world_providers,
)

router = APIRouter(tags=["Миры и действия"])

from ..logsetup import get_logger

log = get_logger(__name__)


@router.get("/api/worlds")
async def worlds():
    return db.list_worlds()


@router.post("/api/worlds")
async def create_world(body: WorldCreate):
    difficulty = body.difficulty if body.difficulty in ("easy", "normal", "hardcore") else "normal"
    perspective = body.perspective if body.perspective in ("first", "second", "third") else "second"
    language = body.language if body.language in ("ru", "en") else "ru"
    genres_sel = []
    if body.genres:
        genres_sel = [g.strip().lower() for g in body.genres if g and g.strip().lower() in narrator.GENRE_HINTS]

    theme: dict
    hook: str
    if body.theme_id == "custom":
        # Свой сюжет: текст берём из поля custom_plot или из сохранённого сюжета plot_id
        if body.custom_plot and body.custom_plot.strip():
            plot_text = body.custom_plot.strip()
            plot_name = (body.name or "").strip() or "Свой сюжет"
        elif body.plot_id:
            pl = db.get_plot(body.plot_id)
            if not pl:
                raise HTTPException(400, "Сюжет не найден")
            plot_text = pl["plot"]
            plot_name = (body.name or "").strip() or pl["name"]
        else:
            raise HTTPException(400, "Опиши свой сюжет или выбери сохранённый")
        theme = narrator.theme_from_custom(plot_name, plot_text, genres_sel)
        hook = plot_text
        is_plot = False
    else:
        theme = narrator.get_theme(body.theme_id)
        if not theme:
            raise HTTPException(400, f"Неизвестная тема: {body.theme_id}")
        genre = theme["genre"]
        if genres_sel:
            genre = ", ".join(genres_sel)
        theme = {**theme, "genre": genre}
        # Сюжет из файла plots/: зацеп по умолчанию — plot_text (завязка), если игрок не задал свой
        is_plot = bool(theme.get("is_plot"))
        hook = (body.custom_hook or "").strip()
        if is_plot and not hook:
            hook = (theme.get("plot_text") or "").strip()

    name = body.name and body.name.strip() or f"{theme['name']} — {narrator.diff_label(difficulty)}"
    setting = narrator.default_setting(theme, difficulty)
    # Снапшот темы для мира из сюжета: если файл сюжета позже удалён/отредактирован — мир
    # продолжает жить по данным, которые были на момент создания (правило «не зависит от файлов»).
    # Храним только то, что нужно в рантайме (стиль/жанр/имя/завязка), без сырого сюжета/лора.
    if is_plot:
        setting["_theme_snapshot"] = {k: theme.get(k) for k in
                                       ("id", "name", "genre", "desc", "style", "opening")}
    gen_settings = {"temperature": get_config().default_temp, "top_p": get_config().default_top_p,
                    "max_tokens": get_config().max_tokens,
                    "context_tokens": get_config().context_tokens,
                    "logic_judge": True}
    # Пер-мирные провайдеры из формы создания
    ps: dict = {}
    for k, p in (("main", body.provider_main), ("embedding", body.provider_embedding),
                 ("rerank", body.provider_rerank)):
        if p is not None:
            ps[k] = _apply_provider_override(ps.get(k, {}), p)
    # Если выбрана недоступная модель — мир не создаётся (чёткая ошибка)
    providers = get_config().resolve_world_providers(ps)
    if not await llm.check_available(providers["main"]):
        m = providers["main"]
        raise HTTPException(503, "Выбранная модель недоступна: «" + m["name"] + "» (" + (m["base_url"] or "адрес не задан") + "). "
                             "Проверь, что сервер запущен, адрес и API-ключ верны — и повтори создание мира.")
    # Авто-подгонка окна мира под реальный n_ctx модели (защита от молчаливых обрезов).
    gen_settings = await _context_guard(0, providers, gen_settings)
    narrator_id = body.narrator_id or db.default_narrator_id()
    world_id = db.create_world(name, body.theme_id, theme["genre"], difficulty, perspective, language,
                               hook, setting, gen_settings, narrator_id=narrator_id,
                               provider_settings=ps or None)
    # Персонаж: богатый старт «кто я» — раса/класс/профессия/статы/уровень/навыки/инвентарь/биография.
    # Приоритет: зацеп игрока (identity) → сгенерированный обогащённый персонаж → фолбэк-биография по теме/лору.
    lore_texts: list[str] = []
    if body.theme_id == "custom":
        custom_lore = (body.custom_lore or "").strip()
        if not custom_lore and body.plot_id:
            pl = db.get_plot(body.plot_id)
            custom_lore = ((pl or {}).get("lore", "") or "").strip()
        if custom_lore:
            lore_texts = [custom_lore]
    else:
        lore = theme.get("lore") or []
        if isinstance(lore, dict):
            lore_texts = [str(v) for v in lore.values() if v]
        elif isinstance(lore, list):
            # темы из сюжетов — список статей {title, content, ...}: берём текст статьи
            lore_texts = [v.get("content") if isinstance(v, dict) and v.get("content") else str(v)
                          for v in lore if v]
    # Лор передаём в генератор, чтобы персонаж не противоречил «библии» вселенной.
    try:
        hook_text = hook
        gen = await narrator.generate_character(theme, setting, hook=hook_text,
                                                lang=language, provider=providers["main"],
                                                lore=lore_texts or None)
        if not gen:
            log.warning("create_world (world %s): generate_character вернул None — персонаж останается минимальным", world_id)
            # Фолбэк БЕЗ LLM. ВАЖНО: для миров из файлов сюжетов hook — это plot_text (завязка-лор),
            # а не личность игрока. Писать его в identity нельзя (в биографии окажется лор).
            # Личный зацеп (body.custom_hook) — можно, это осознанное пожелание игрока.
            is_plot_default = (theme.get("plot_text") or "").strip()
            personal_hook = hook_text if (hook_text and (not is_plot or hook_text != is_plot_default)) else ""
            if personal_hook:
                setting["player"]["identity"] = personal_hook[:900]
            else:
                setting["player"]["identity"] = f"{setting['player'].get('name', 'Путник')} — житель этого мира."
            narrator.apply_character(setting, {}, hook=personal_hook, genre=theme.get("genre", ""))
            if not (setting.get("player") or {}).get("name") or setting["player"]["name"] == "Путник":
                try:
                    first = next((w for w in (setting["player"]["identity"] or "").split()
                                  if any(ch.isalpha() for ch in w)), "").strip(",.")
                    setting["player"]["name"] = first[:40] or "Путник"
                except Exception:
                    setting["player"]["name"] = "Путник"
        else:
            narrator.apply_character(setting, gen, hook=hook_text, genre=theme.get("genre", ""))
    except Exception as e:
        log.warning("create_world персонаж (world %s): %s", world_id, e)
    # Сюжет из файла plots/: применяем стартовое состояние мира (starting_state) —
    # локации/NPC/магазины/фракции/квесты/флаги/время/погода/золото/инвентарь.
    # Роль игрока (раса/класс/статы) сюжет НЕ задаёт — её ведёт generate_character выше.
    # Внешний try: старт применяется ДАЖЕ если генерация персонажа упала (мир без «голой» завязки).
    if is_plot and theme.get("plot"):
        try:
            narrator.apply_plot_start(setting, theme["plot"])
        except Exception as e:
            log.warning("create_world apply_plot_start (world %s): %s", world_id, e)
    # Карточки знаний на старте: раса/класс/профессия/навыки/эффекты/предметы из стартового
    # состояния создаются СРАЗУ (иначе вкладка «Карточки» пустая до первого хода).
    try:
        kcards = narrator.ensure_knowledge_cards(world_id, setting, seq=0)
        if kcards:
            asyncio.get_event_loop().create_task(narrator.index_entities(world_id, kcards))
    except Exception as e:
        log.warning("create_world карточки знаний (world %s): %s", world_id, e)
    try:
        db.update_world(world_id, setting=setting)
    except Exception as e:
        log.warning("create_world update_world (world %s): %s", world_id, e)
    world = db.get_world(world_id)
    providers = _world_providers(world)
    persona = _world_persona(world)

    # ── Лор мира (библия вселенной): из темы или из своего сюжета (опционально) ──
    try:
        if body.theme_id == "custom":
            custom_lore = (body.custom_lore or "").strip()
            if not custom_lore and body.plot_id:
                pl = db.get_plot(body.plot_id)
                custom_lore = (pl or {}).get("lore", "") or ""
            if custom_lore:
                narrator.seed_lore_from_custom(world_id, custom_lore)
        else:
            narrator.seed_lore_from_theme(world_id, theme)
        asyncio.get_event_loop().create_task(_index_world_lore(world_id, providers))
    except Exception as e:
        # лор не критичен для создания мира, но без него рассказчик не знает канон —
        # молча потерять сидинг нельзя (правило 14)
        log.warning("сидинг лора (мир %s) не удался — мир создан без «библии» вселенной: %s",
                    world_id, e)

    # 🌐 Графовая база данных: первичная синхронизация карты мира на старте.
    # Источник истины рёбер — setting.locations; здесь граф строится из начального мира.
    try:
        graph.sync_from_setting(world_id, setting)
    except Exception as e:
        log.warning("граф (создание мира %s): %s", world_id, e)

    try:
        opening = await narrator.generate_opening(world, setting, persona=persona,
                                                  provider=providers["main"])
    except Exception:
        opening = theme.get("opening", "Мир пробуждается. Что ты делаешь?")
    db.add_event(world_id, "narrator", opening)
    ev = db.add_event(world_id, "system", f"🌍 Мир «{name}» создан. Пиши свои действия в поле ввода.")
    asyncio.get_event_loop().create_task(_background_memory(world_id, 1, "Открытие мира", opening))

    # Быстрые действия ИИ сразу на первом шаге (по вступительной сцене), а не только после хода.
    suggestions: list[str] = []
    try:
        suggestions = await asyncio.wait_for(
            narrator.generate_suggestions(setting, "Начало истории", opening, provider=providers["main"]),
            timeout=12)
    except Exception:
        suggestions = []

    return {"world_id": world_id, "world": db.get_world(world_id),
            "opening": opening, "opening_event_id": ev["id"],
            "suggestions": suggestions, "state": setting}


@router.get("/api/worlds/{world_id}")
async def world_detail(world_id: int):
    world = db.get_world(world_id)
    if not world:
        raise HTTPException(404, "Мир не найден")
    recent = db.get_events(world_id, limit=60)
    cfg = get_config()
    return {"world": world, "setting": json.loads(world["setting"]),
            "recent": recent, "gen_settings": json.loads(world.get("gen_settings") or "{}"),
            "persona": _world_persona(world),
            "providers_effective": _masked_world_providers(world),
            "provider_settings": _world_provider_settings(world),
            "rerank_enabled": cfg.rerank_enabled,
            "memory_defaults": {"rag_memory_k": cfg.rag_memory_k, "rag_memory_max": cfg.rag_memory_max,
                                "lore_rag_k": cfg.lore_rag_k, "lore_rag_k_max": cfg.lore_rag_k_max,
                                "lore_token_budget": cfg.lore_token_budget,
                                "lore_token_budget_max": cfg.lore_token_budget_max},
            "tts": tts.tts_effective(world),
            "tts_settings_raw": json.loads(world.get("tts_settings") or "{}")}


@router.get("/api/worlds/{world_id}/graph")
async def world_graph(world_id: int, target: str | None = None):
    """🌐 Граф мира: узлы (локации + kind), связи, слои BFS от текущей локации
    и кратчайший путь к `target` (если достижим). Источник истины — графовая БД.
    Перед ответом синхронизируется с актуальным setting (best-effort)."""
    world = db.get_world(world_id)
    if not world:
        raise HTTPException(404, "Мир не найден")
    setting = json.loads(world["setting"])
    try:
        graph.sync_from_setting(world_id, setting)
    except Exception as e:
        log.warning("граф (world %s): %s", world_id, e)
    return graph.payload(world_id, setting, target=target)


@router.get("/api/worlds/{world_id}/history")
async def history(world_id: int, before: int = 0, limit: int = 60):
    """Вся история событий мира. Для постраничного просмотра лога:
    `before` — брать события строго раньше этого seq; `limit` — сколько штук (по умолчанию всё)."""
    # B5 (сессия 34): страница вырезается в SQL — раньше тянули ВЕСЬ лог мира и срезали в
    # Python, из-за чего просмотр истории длинного прохождения тормозил линейно.
    return db.get_history_page(world_id, before_seq=before or 0, limit=limit or 0)


@router.get("/api/worlds/{world_id}/events")
async def events_since(world_id: int, since: int = 0):
    """Новые события после seq — ЗАПАСНЫЙ путь живого чата (поллинг), если EventSource
    недоступен (см. /events/stream)."""
    evs = db.get_events(world_id, since_seq=since)
    return [{"id": e["id"], "seq": e["seq"], "role": e["role"], "content": e["content"],
             "feedback": e["feedback"], "folded": e["folded"], "meta": e.get("meta") or {}} for e in evs]


@router.get("/api/worlds/{world_id}/events/stream")
async def events_stream(world_id: int, after: int = 0):
    """🔌 Живая лента мира (сессия 34, D3): SSE-подписка на шину событий (backend/bus.py).

    Фоновые системки — ⚔️ ход врага, 🤖 подсказка мастера, ⏰ истёкший таймер, ⚖️ искажение
    реальности, 💾 автосохранение — приходят вкладке мгновенно, а не следующим поллингом
    (раньше до 15 секунд). Перед подпиской догружаем всё, что произошло с `after`, чтобы
    переподключение после обрыва не потеряло сообщения.
    """
    if not db.get_world(world_id):
        raise HTTPException(404, "Мир не найден")
    q = bus.subscribe(world_id)

    async def gen():
        try:
            # 1) догоняем пропущенное (после обрыва/перезагрузки страницы)
            for e in db.get_events(world_id, since_seq=after):
                yield bus.sse_format({"type": "event", "event": {
                    "id": e["id"], "seq": e["seq"], "role": e["role"], "content": e["content"],
                    "feedback": e["feedback"], "folded": e["folded"], "meta": e.get("meta") or {}}})
            yield bus.sse_format({"type": "ready", "world_id": world_id})
            # 2) живая рассылка + heartbeat, чтобы промежуточный прокси не убил соединение
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=25.0)
                    yield bus.sse_format(payload)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            bus.unsubscribe(world_id, q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ───────────────────────────── Провайдеры per-world ─────────────────────────────
@router.post("/api/worlds/{world_id}/providers")
async def world_providers(world_id: int, body: ProvidersIn):
    world = db.get_world(world_id)
    if not world:
        raise HTTPException(404, "Мир не найден")
    current = _world_provider_settings(world)
    cfg = get_config()
    # валидируем id
    for kind in ("main", "embedding", "rerank"):
        item = getattr(body, kind)
        if item is not None and item.id is not None:
            ids = {o["id"] for o in PROVIDER_OPTIONS[kind]}
            if item.id not in ids:
                raise HTTPException(400, f"Неизвестный провайдер «{item.id}» для {kind}")
        if item is not None:
            current[kind] = _apply_provider_override(current.get(kind, {}), item)
    # Сессия 36, п.19: раньше пер-мирная смена провайдера не проверялась нигде,
    # и сохранение нерабочей модели превращало будущие ходы в лавину ошибок (мир
    # продолжал думать, что всё в порядке). При создании мира такая проверка есть
    # (503) — теперь она есть и при правке, но как предупреждение, а не блокировка:
    # отказать в сохранении — значит запереть игрока, у которого модель поднялась
    # через минуту, и лишить его возможности пересохранить остальные настройки.
    warns: list[str] = []
    eff = cfg.resolve_world_providers(current)
    try:
        main = eff.get("main") or {}
        if main.get("enabled") and not await llm.check_available(main):
            warns.append("Основная модель «" + (main.get("name") or main.get("id") or "?") +
                         "» сейчас не отвечает (" + (main.get("base_url") or "адрес не задан") +
                         "). Ходы будут падать, пока сервер модели не поднимется.")
    except Exception as e:
        log.warning("проверка доступности модели (world %s): %s", world_id, e)
    db.update_world(world_id, provider_settings=current)
    if warns:
        log.warning("сохранены пер-мирные провайдеры с предупреждением (world %s): %s",
                    world_id, " | ".join(warns))
    return {"ok": True, "provider_settings": current,
            "warnings": warns,
            "providers_effective": {k: _mask_provider(dict(v)) for k, v in eff.items()}}


# ───────────────────────────── Действия ─────────────────────────────
@router.post("/api/worlds/{world_id}/action")
async def action(world_id: int, body: ActionIn, _rl: None = Depends(make_guard("action"))):
    text = _ensure_action_len(body.text)
    # Слэш-команды
    low = text.lower()
    if low.startswith("/roll "):
        return await _slash_roll(world_id, text[6:].strip())
    if low.startswith("/hint"):
        return await _slash_hint(world_id)
    if low.startswith("/memory "):
        return await _slash_memory(world_id, text[8:].strip())
    if low == "/where":
        return _slash_where(world_id)
    if low == "/status":
        return _slash_status(world_id)
    if low == "/quests":
        return _slash_quests(world_id)
    if low == "/stats":
        return _slash_stats(world_id)
    if low == "/map":
        return _slash_map(world_id)
    if low == "/inventory":
        return _slash_inventory(world_id)
    if low in ("/shops", "/economy", "/craft"):
        return _slash_economy(world_id)
    if low == "/story":
        return _slash_story(world_id)
    if low == "/board":
        return _slash_board(world_id)
    if low == "/risk" or low.startswith("/risk "):
        return _slash_risk(world_id, text[5:].strip())
    if low == "/journal" or low.startswith(("/journal ", "/хроника ", "/дневник ")) or low in ("/хроника", "/дневник"):
        return _slash_journal(world_id, text)
    if low in ("/help", "/помощь"):
        return {"reply": ("Команды: /roll <куб>, /hint, /memory <запрос>, /where, /status, /quests, /stats, "
                          "/map, /inventory, /shops (или /economy, /craft), /board, /story, /journal, "
                          "/risk <идея>, /help. Во всём остальном просто описывай действия."),
                "events": [], "state": json.loads(db.get_world(world_id)["setting"]), "game_over": False}
    return await _process_action(world_id, text, regenerate=bool(body.regenerate))


@router.post("/api/worlds/{world_id}/suggest")
async def suggest_actions(world_id: int):
    """Свежие варианты действий по текущей сцене (кнопка «Обновить сюжеты»/при открытии мира).
    Перегенерирует подсказки ИИ по последнему ответу рассказчика — чтобы кнопки не «залипали»
    на одних и тех же. Best-effort: при сбое возвращает пусто (фронт покажет фолбэк от состояния)."""
    world = db.get_world(world_id)
    if not world:
        raise HTTPException(404, "Мир не найден")
    setting = json.loads(world["setting"])
    providers = _world_providers(world)
    # Рассказчик-персона (persona) здесь НЕ нужна: narrator.generate_suggestions /
    # suggestions_messages её не принимают — подсказки это «бытовые действия игрока»,
    # а не голос NPC. Мёртвый локальный вызов _world_persona() (ruff F841) удалён; если
    # захотим подсказки в персоне — это отдельная доработка с параметром в narrator.
    # последний ответ рассказчика (сессия 36, п.8: хвост истории из БД, а не весь лог)
    last_reply = ""
    for e in reversed(db.get_unfolded_events(world_id, limit=12, roles=("narrator",))):
        if e["role"] == "narrator":
            last_reply = e["content"]
            break
    suggestions: list[str] = []
    if last_reply.strip():
        try:
            suggestions = await asyncio.wait_for(
                narrator.generate_suggestions(setting, "Осмотреться в текущей ситуации", last_reply,
                                              provider=providers["main"]),
                timeout=12)
        except Exception:
            suggestions = []
    return {"suggestions": suggestions}


@router.post("/api/worlds/{world_id}/divine")
async def divine(world_id: int, body: DivineIn, _rl: None = Depends(make_guard("divine", limit=20, window=60, burst=6, burst_window=5))):
    """Воззвание к Провидению (Божественный арбитр). Игрок жалуется на ошибку Рассказчика;
    та же модель со строгим отдельным промптом проверяет логику и, если ошибка реальна,
    правит мир директивами (add_item, hp, gold, квесты...) с сюжетным объяснением
    («искажение реальности»). Провидение — корректор мира, а не генератор награды:
    промпт запрещает выдачу сверх заявленной потери, а интервал между воззваниями
    ограничивает DIVINE_COOLDOWN_TURNS (сессия 36, п.2; 0 = без лимита). Каждая ресурсная
    выдача пишется в лог (`narrator._divine_grants`). Без реальной ошибки Провидение
    мягко отказывает (decline) и мир не трогает."""
    complaint = (body.complaint or "").strip()
    if not complaint:
        raise HTTPException(400, "Опишите, в чём искажение реальности.")
    world = db.get_world(world_id)
    if not world:
        raise HTTPException(404, "Мир не найден")
    setting = json.loads(world["setting"])
    if setting.get("game_over"):
        raise HTTPException(409, "Игра окончена в этом мире.")

    # Кулдаун DIVINE_COOLDOWN_TURNS (сессия 36, п.2): по умолчанию 3 хода. Раньше был 0,
    # и «попросить у богов золото» можно было неограниченно — ответы модели защитой от
    # фарма не являются. 0 по-прежнему отключает лимит (админка/.env).
    cd = max(0, int(get_config().divine_cooldown_turns))
    if cd > 0:
        turns = setting.get("_player_turns", 0) or 0
        # None = «ещё ни разу не воззывался» — первое воззвание ДОСТУПНО всегда.
        # (Раньше читалось как `... , 0) or 0`, и на старте мира получалось «боги молчат
        # первые cd ходов»: игрок не мог пожаловаться на ошибку рассказчика в самой
        # первой сцене — а это как раз тот момент, где Провидение нужнее всего.)
        last_raw = setting.get("_divine_last_turn")
        if last_raw is not None:
            try:
                last = int(last_raw)
            except (TypeError, ValueError):
                last = 0
            if turns - last < cd:
                left = cd - (turns - last)
                raise HTTPException(429, f"Провидение ещё не готово ответить. "
                                         f"Воззвать можно через {left} ход(ов).")

    providers = _world_providers(world)
    # Последний обмен (действие → ответ), на который указывает игрок — Провидение сверяет с ним
    # B5: последний обмен двумя короткими запросами вместо всего лога
    action_last, reply_last = db.get_last_exchange(world_id)

    res = await narrator.divine_intervene(world_id, world, setting, complaint,
                                          action=action_last, reply=reply_last,
                                          lang=world.get("language", "ru"),
                                          provider=providers["main"])
    if res is None:
        raise HTTPException(502, "Провидение молчит. Попробуйте ещё раз.")

    twist = res["twist"]
    sys_msgs = res.get("sys_msgs") or []
    decline = bool(res.get("decline"))
    divine_content = twist
    if sys_msgs:
        divine_content += "\n" + "\n".join(sys_msgs)
    with db.transaction():
        plea_ev = db.add_event(world_id, "system", f"\U0001f64f Воззвание: \u201c{complaint}\u201d")
        div_ev = db.add_event(world_id, "divine", divine_content)
        # фиксируемся и время последнего воззвания (для кулдауна)
        new_state = res.get("state") or setting
        new_state["_divine_last_turn"] = new_state.get("_player_turns", 0) or 0
        db.update_world(world_id, setting=new_state)
    # индексируем воззвание и ответ в память мира
    try:
        seq = db.latest_seq(world_id)
        cur = db.get_world(world_id)
        embed = _world_providers(cur).get("embedding") if cur else None
        await narrator.index_exchange(world_id, seq, "\u2642\ufe0f Воззвание к Провидению", divine_content,
                                      provider=embed)
    except Exception as e:
        log.warning("index за Провидение (world %s): %s", world_id, e)

    return {"world_id": world_id, "events": [
        {"id": plea_ev["id"], "seq": plea_ev["seq"], "role": "system", "content": plea_ev["content"]},
        {"id": div_ev["id"], "seq": div_ev["seq"], "role": "divine", "content": divine_content},
    ], "state": new_state, "game_over": new_state.get("game_over", False), "decline": decline}


@router.post("/api/worlds/{world_id}/vision/trigger")
async def trigger_vision(world_id: int):
    """🌙 Разыграть видение/сон из очереди (pending_visions): отдельный LLM-проход, который
    оборачивает старые факты памяти в художественное сновидение. Рассказчик-мастер вызывает
    при отдыхе/сне/медитации (vision_add — положить в очередь). Не блокирует ход."""
    world = db.get_world(world_id)
    if not world:
        raise HTTPException(404, "Мир не найден")
    setting = json.loads(world["setting"])
    pv = setting.get("pending_visions") or []
    if not isinstance(pv, list) or not pv:
        raise HTTPException(404, "Очередь видений пуста. Сначала положите видение (vision_add).")
    vision = pv.pop(0)
    providers = _world_providers(world)
    # Память как сюжет: подмешиваем RAG-факты в видение (эхо прошлого)
    memories: list[str] = []
    try:
        mem = await narrator.retrieve_memory(world_id, "видение, сон, прошлое", setting,
                                             providers=providers)
        memories = [str(m) for m in (mem or [])[:6]]
    except Exception as e:
        log.warning("RAG для видения (world %s): %s", world_id, e)
    txt = await narrator.generate_vision(setting, vision, lang=world.get("language", "ru"),
                                         provider=providers["main"], memories=memories)
    if not txt:
        # возвращаем видение обратно (не теряем) и сообщаем о сбое
        pv.insert(0, vision)
        raise HTTPException(502, "Видение не пришло. Попробуйте ещё раз.")
    content = f"🌙 Видение:\n{txt}"
    with db.transaction():
        vis_ev = db.add_event(world_id, "narrator", content)
        db.update_world(world_id, setting=setting)
    # индексируем видение в память (усиливает «тёмные воспоминания»)
    try:
        seq = db.latest_seq(world_id)
        embed = _world_providers(db.get_world(world_id)).get("embedding")
        await narrator.index_exchange(world_id, seq, "🌙 Видение", content, provider=embed)
    except Exception as e:
        log.warning("index за видение (world %s): %s", world_id, e)
    return {"world_id": world_id, "events": [
        {"id": vis_ev["id"], "seq": vis_ev["seq"], "role": "narrator", "content": content}],
        "state": setting, "game_over": setting.get("game_over", False)}


@router.post("/api/worlds/{world_id}/action/stream")
async def action_stream(world_id: int, body: ActionIn, _rl: None = Depends(make_guard("action_stream"))):
    text = _ensure_action_len(body.text)
    low = text.lower()
    if (low.startswith("/roll ") or low.startswith("/hint") or low.startswith("/memory ") or low == "/where"
            or low == "/status" or low == "/quests" or low == "/stats" or low == "/map" or low == "/inventory" or low == "/story"
            or low == "/board"
            or low in ("/help", "/помощь")):
        # слэш-команды — без стриминга
        result = await action(world_id, body)
        async def gen():
            yield f'event: done\ndata: {json.dumps(result, ensure_ascii=False)}\n\n'
        return StreamingResponse(gen(), media_type="text/event-stream")

    queue: asyncio.Queue = asyncio.Queue()

    async def emit(tok: str):
        await queue.put(tok)

    async def worker():
        try:
            result = await _process_action(world_id, text, stream_emit=emit, regenerate=bool(body.regenerate))
            await queue.put(("RESULT", result))
        except Exception as e:
            await queue.put(("ERROR", str(e)))
        finally:
            await queue.put(("DONE", None))

    task = asyncio.get_event_loop().create_task(worker())

    async def gen():
        try:
            while True:
                item = await queue.get()
                if isinstance(item, str):
                    yield f"event: token\ndata: {json.dumps({'text': item}, ensure_ascii=False)}\n\n"
                elif item[0] == "RESULT":
                    yield f"event: result\ndata: {json.dumps(item[1], ensure_ascii=False)}\n\n"
                elif item[0] == "ERROR":
                    yield f"event: error\ndata: {json.dumps({'error': item[1]}, ensure_ascii=False)}\n\n"
                elif item[0] == "DONE":
                    break
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(gen(), media_type="text/event-stream")


async def _slash_roll(world_id: int, expr: str):
    m = re.match(r"^(\d*)d(\d+)([+-]\d+)?$", expr.strip().lower())
    if not m:
        return {"reply": "Формат: /roll d20|2d6+1|d100 и т.п.", "events": [], "state": None, "game_over": False}
    res = narrator.roll_expr(expr.strip())
    ev = db.add_event(world_id, "dice", f"🎲 Бросок {expr}: {res['rolls']} = {res['total']}")
    return {"reply": f"🎲 {expr}: {' '.join(map(str, res['rolls']))} = {res['total']}",
            "reply_event_id": ev["id"], "events": [ev], "state": None, "game_over": False}


async def _slash_hint(world_id: int):
    world = db.get_world(world_id)
    setting = json.loads(world["setting"])
    providers = _world_providers(world)
    persona = _world_persona(world)
    msgs = [{"role": "system", "content": narrator.build_system_prompt(world, setting, persona=persona)},
            {"role": "user", "content": "Игрок просит подсказку. Дай короткий намёк (1–2 предложения): что стоит "
                                        "сделать дальше в текущей ситуации, не раскрывая тайн."}]
    try:
        hint = (await llm.complete(msgs, temperature=0.7, max_tokens=150,
                                   provider=providers["main"])).strip()
    except Exception:
        hint = "Осмотрись внимательнее: в деталях окружения часто кроется следующий шаг."
    ev = db.add_event(world_id, "system", f"💡 Подсказка: {hint}")
    return {"reply": hint, "reply_event_id": ev["id"], "events": [ev], "state": setting, "game_over": False}


async def _slash_memory(world_id: int, query: str):
    setting = json.loads(db.get_world(world_id)["setting"])
    chunks = await narrator.retrieve_memory(world_id, query, setting, k=5)
    out = "🔎 Воспоминания:\n\n" + "\n\n---\n\n".join(f"• {c[:500]}" for c in chunks) if chunks else "Ничего не вспомнилось."
    return {"reply": out, "events": [], "state": setting, "game_over": False}


def _slash_where(world_id: int):
    setting = json.loads(db.get_world(world_id)["setting"])
    loc = setting.get("locations", {}).get(setting.get("current_location", "start"), {})
    name = loc.get("name") or setting.get("current_location", "неизвестно")
    desc = loc.get("desc") or ""
    conns = loc.get("connections") or []
    known = [setting.get("locations", {}).get(c, {}).get("name", c) for c in conns]
    reply = f"📍 Ты находишься: {name}"
    if desc:
        reply += f"\n{desc}"
    if known:
        reply += "\nРядом: " + ", ".join(known)
    return {"reply": reply, "events": [], "state": setting, "game_over": False}


def _slash_status(world_id: int):
    setting = json.loads(db.get_world(world_id)["setting"])
    p = setting["player"]
    name = p.get("name") or "Путник"
    L = [f"🧙 {name} — ур. {p.get('level', 1)}"]
    L.append(f"❤️ HP {p.get('hp', 0)}/{p.get('max_hp', 0)} | 💧 MP {p.get('mp', 0)}/{p.get('max_mp', 0)} | 🪙 {p.get('gold', 0)} | ✨ XP {p.get('xp', 0)}")
    role = []
    if p.get("race"):
        role.append(f"раса: {p['race']}")
    if p.get("class"):
        role.append(f"класс: {p['class']} (ранг {p.get('class_rank', 'F')})")
    if p.get("secondary_class"):
        role.append(f"мультикласс: {p['secondary_class']} (ранг {p.get('secondary_rank', 'F')})")
    if p.get("profession"):
        role.append(f"профессия: {p['profession']}")
    if role:
        L.append("🎭 " + "; ".join(role))
    st = p.get("stats", {})
    if st:
        L.append("📊 " + ", ".join(f"{k} {v}" for k, v in st.items()))
    skills = p.get("skills") or {}
    if skills:
        s = []
        for k, sk in list(skills.items())[:14]:
            r = sk.get("rank", "F") if isinstance(sk, dict) else sk
            s.append(f"{k} ({r})")
        L.append("⚔ Навыки: " + "; ".join(s))
    titles = p.get("titles") or []
    if titles:
        L.append("🏅 Титулы: " + ", ".join(str(t) for t in titles[:8]))
    rep = p.get("reputation") or {}
    if rep:
        L.append("💗 Репутация: " + "; ".join(f"{k}: {v}" for k, v in list(rep.items())[:8]))
    actions = p.get("actions") or {}
    if actions:
        L.append("🔨 Дела: " + "; ".join(f"{k} ×{v}" for k, v in list(actions.items())[:10]))
    effects = p.get("effects") or {}
    if effects:
        L.append("✨ Эффекты: " + "; ".join(str(k) for k in list(effects.keys())[:10]))
    identity = (p.get("identity") or "").strip()
    if identity:
        L.append(f"\n📜 {identity[:800]}")
    return {"reply": "\n".join(L), "events": [], "state": setting, "game_over": False}


def _slash_quests(world_id: int):
    setting = json.loads(db.get_world(world_id)["setting"])
    quests = setting.get("quests", {})
    if not quests:
        return {"reply": "📜 Активных квестов нет.", "events": [], "state": setting, "game_over": False}
    L = ["📜 Квесты:"]
    for qid, q in quests.items():
        title = q.get("title", qid)
        status = q.get("status", "active")
        icon = "✔" if status == "done" else "🟡"
        prog = q.get("progress")
        L.append(f"{icon} {title}" + (f" — {q.get('desc', '')}" if q.get("desc") else "")
                + (f" [шаг: {prog}]" if prog else ""))
    return {"reply": "\n".join(L), "events": [], "state": setting, "game_over": False}


def _slash_stats(world_id: int):
    setting = json.loads(db.get_world(world_id)["setting"])
    p = setting["player"]
    name = p.get("name") or "Путник"
    L = [f"📊 {name} — статистика пути"]
    prog = p.get("progress") or {}
    if prog:
        L.append("Пройдено: " + "; ".join(f"{k} {v}" for k, v in list(prog.items())[:16]))
    else:
        L.append("Прозрачный путь ещё не отмечен — отметка появится по событиям.")
    ach = p.get("achievements") or []
    if ach:
        L.append("\n🏆 Достижения:")
        for a in ach:
            if isinstance(a, dict):
                L.append(f"  • {a.get('name')}" + (f" — {a.get('desc')}" if a.get("desc") else ""))
            else:
                L.append(f"  • {a}")
    else:
        L.append("\n🏆 Достижений пока нет.")
    return {"reply": "\n".join(L), "events": [], "state": setting, "game_over": False}


def _slash_map(world_id: int):
    setting = json.loads(db.get_world(world_id)["setting"])
    locs = setting.get("locations", {})
    cur = setting.get("current_location", "start")
    if not locs:
        return {"reply": "🗺 Карта пуста.", "events": [], "state": setting, "game_over": False}
    # источник истины — графовая БД (nodes/edges/current), фолбэк — setting.locations
    g = None
    try:
        graph.sync_from_setting(world_id, setting)
        g = graph.payload(world_id, setting)
    except Exception as e:
        log.warning("граф (/map, world %s): %s", world_id, e)
    L = ["🗺 Известные локации:"]
    if g and g["nodes"]:
        adj = {}
        for e in g["edges"]:
            adj.setdefault(e["source"], set()).add(e["target"])
            adj.setdefault(e["target"], set()).add(e["source"])
        # локации (kind=location); соседи из рёбер графа
        for n in g["nodes"]:
            if n.get("kind") != "location":
                continue
            node_id = n["id"]
            marker = "📍" if node_id == cur else "•"
            conns = [c for c in (adj.get(node_id) or ()) if c in locs]
            conn_names = [locs.get(c, {}).get("name", c) for c in conns]
            line = f"{marker} {n.get('label') or node_id}"
            if conn_names:
                line += " -> " + ", ".join(conn_names)
            L.append(line)
        # связанные сущности (магазины/фракции/живые NPC)
        shops_n = [n["label"] for n in g["nodes"] if n.get("kind") == "shop" and n["id"].startswith("shop:")]
        fac_n = [n["label"] for n in g["nodes"] if n.get("kind") == "faction" and n["id"].startswith("faction:")]
        npc_n = [n["label"] for n in g["nodes"] if n.get("kind") == "npc" and n["id"].startswith("npc:")]
        if shops_n:
            L.append("🏪 Магазины: " + ", ".join(shops_n))
        if fac_n:
            L.append("🏴 Фракции: " + ", ".join(fac_n))
        if npc_n:
            L.append("🗣 Живые NPC: " + ", ".join(npc_n))
        if not L[1:]:
            L.append("Карта пуста (локаций пока нет).")
    else:
        # фолбэк на setting.locations
        for lid, loc in locs.items():
            marker = "📍" if lid == cur else "•"
            name = loc.get("name", lid)
            conns = [locs.get(c, {}).get("name", c) for c in (loc.get("connections") or [])]
            line = f"{marker} {name}"
            if conns:
                line += " -> " + ", ".join(conns)
            L.append(line)
    return {"reply": "\n".join(L), "events": [], "state": setting, "game_over": False}


def _slash_inventory(world_id: int):
    setting = json.loads(db.get_world(world_id)["setting"])
    inv = setting["player"].get("inventory", []) or []
    if not inv:
        return {"reply": "🎒 Инвентарь пуст.", "events": [], "state": setting, "game_over": False}
    L = ["🎒 Инвентарь:"]
    for it in inv:
        name = it.get("name", "?")
        qty = it.get("qty", 1)
        desc = it.get("desc", "")
        L.append(f"• {name} ×{qty}" + (f" — {desc}" if desc else ""))
    return {"reply": "\n".join(L), "events": [], "state": setting, "game_over": False}


def _slash_economy(world_id: int):
    from backend.mechanics import (inventory_weight, carry_capacity, total_sell_value,
                                   location_stations, can_craft, has_station)
    setting = json.loads(db.get_world(world_id)["setting"])
    p = setting["player"]
    L = ["💰 Экономика:"]
    L.append(f"🪙 Золото: {p.get('gold', 0)}")
    w = inventory_weight(p)
    cap = carry_capacity(p)
    if w:
        L.append(f"🎒 Загрузка: {w:.0f}/{cap} кг" + (" ⚠ ПЕРЕГРУЗ" if w > cap else ""))
    sell = total_sell_value(p)
    L.append(f"💰 Продав весь инвентарь, можно выручить ≈ {sell} 🪙")
    _st = location_stations(setting)
    if _st:
        L.append("🔧 Станции здесь: " + ", ".join(_st))
    shops = setting.get("shops") or {}
    if shops:
        L.append("\n🏪 Магазины:")
        for sid, sh in list(shops.items())[:10]:
            _items = (sh.get("items") or [])[:8]
            if not _items:
                L.append(f"  • {sh.get('name', sid)} — пусто")
                continue
            _s = "; ".join(f"{i.get('name')} {i.get('price')}🪙 ×{i.get('qty')}" for i in _items)
            L.append(f"  • {sh.get('name', sid)}{f" ({sh.get('faction')})" if sh.get('faction') else ''} — {_s}")
    crafts = setting.get("crafts") or {}
    avail = []
    for rid, rc in list(crafts.items())[:12]:
        _miss, _st0 = can_craft(setting, rc)
        if _st0 == "ok":
            avail.append(rc.get("name", rid))
    if crafts:
        L.append(f"\n📘 Рецепты крафта: {len(crafts)} (сейчас можно создать {len(avail)}: {', '.join(avail) or '—'})")
    else:
        L.append("\nРецептов крафта пока не изучено.")
    return {"reply": "\n".join(L), "events": [], "state": setting, "game_over": False}


def _slash_story(world_id: int):
    setting = json.loads(db.get_world(world_id)["setting"])
    evs = db.get_events(world_id, limit=40)
    turns = [e for e in evs if e["role"] in ("player", "narrator")][-20:]
    if not turns:
        return {"reply": "История пока пуста.", "events": [], "state": setting, "game_over": False}
    L = ["📖 Последние события:"]
    for e in turns:
        content = e["content"].replace("\n", " ").strip()
        if len(content) > 180:
            content = content[:180] + "…"
        if e["role"] == "player":
            L.append(f"🗣 {content}")
        else:
            L.append(f"🌿 {content}")
    return {"reply": "\n".join(L), "events": [], "state": setting, "game_over": False}


def _slash_board(world_id: int):
    """Доска объявлений мира (таверна/форум/рация) — чистое отображение (закон 2)."""
    from backend.mechanics import board_text
    setting = json.loads(db.get_world(world_id)["setting"])
    board = board_text(setting, limit=20)
    if not board:
        return {"reply": "📜 Доска объявлений пуста.", "events": [], "state": setting, "game_over": False}
    return {"reply": "📜 Доска объявлений:\n" + board, "events": [], "state": setting, "game_over": False}


def _slash_risk(world_id: int, idea: str = ""):
    """/risk <идея> — «чем я могу это закрыть» (сессия 34, C7). Чистый форматировщик
    состояния без LLM: перечень фактов, а не вердикт (законы 2/3)."""
    from ..risk import describe_risk
    setting = json.loads(db.get_world(world_id)["setting"])
    return {"reply": describe_risk(setting, idea), "events": [], "state": setting,
            "game_over": False}


def _slash_journal(world_id: int, text: str = ""):
    """/journal — дневник приключений (сессия 34, C2). Хроника значимого, детерминированная.

    `/journal note <текст>` — заметка ИГРОКА (закон 2: код лишь хранит и показывает,
    смысл записи решает игрок). /journal <слово> — поиск по хронике.
    """
    from .. import journal as _jr
    setting = json.loads(db.get_world(world_id)["setting"])
    arg = (text or "").strip()
    # в chat может прийти полный ввод («/journal note …») — срезаем префикс команды
    for pfx in ("/journal", "/хроника", "/дневник"):
        if arg.lower().startswith(pfx):
            arg = arg[len(pfx):].strip()
            break
    low = arg.lower()
    if low.startswith("note ") or low.startswith("заметка "):
        note = arg.split(" ", 1)[1].strip()[:400]
        if note:
            seq = db.latest_seq(world_id)
            db.upsert_entity(world_id, _jr.KIND, f"t{seq}-note-{_jr._stable_key(note)}",
                             name=note, summary="", meta={"seq": seq, "cat": "note",
                                                           "icon": "✍️"}, seq=seq)
            return {"reply": "✍️ Записано в дневник.", "events": [], "state": setting,
                    "game_over": False}
    body = _jr.render(world_id, limit=40)
    if arg and not low.startswith(("note ", "заметка ")):
        q = arg.lower()
        hits = [it for it in _jr.entries(world_id, limit=500)
                if q in it["title"].lower() or q in (it["text"] or "").lower()]
        if not hits:
            body = f"📔 По запросу «{arg}» в дневнике ничего нет."
        else:
            body = f"📔 Дневник — «{arg}» ({len(hits)}):\n" + "\n".join(
                f"  {h['icon']} [ход {h['seq']}] {h['title']}" for h in hits[:25])
    return {"reply": body, "events": [], "state": setting, "game_over": False}


# ───────────────────────────── Дневник / ресурсы ─────────────────────────────
@router.get("/api/worlds/{world_id}/journal")
async def journal_list(world_id: int, limit: int = 100, cat: str = ""):
    """📔 Дневник приключений (сессия 34, C2) — хроника значимого для UI-вкладки.

    `cat` — фильтр категории (quest/npc/item/flag/combat/role/place/world/note).
    Закон 2: только чтение того, что движок уже зафиксировал по диффу состояний.
    """
    from .. import journal as _jr
    if not db.get_world(world_id):
        raise HTTPException(404, "Мир не найден")
    return {"entries": _jr.entries(world_id, limit=limit, cat=cat),
            "categories": _jr.entry_categories(world_id)}


@router.get("/api/worlds/{world_id}/risk")
async def risk_report(world_id: int, idea: str = ""):
    """🧭 /risk как REST (сессия 34, C7) — чем персонаж может закрыть идею.

    Чистый форматировщик состояния (без LLM, без вердиктов): перечень фактов «есть/нет».
    """
    from ..risk import describe_risk
    world = db.get_world(world_id)
    if not world:
        raise HTTPException(404, "Мир не найден")
    setting = json.loads(world["setting"])
    return {"reply": describe_risk(setting, idea), "state": setting}


# ───────────────────────────── Сохранения ─────────────────────────────
@router.get("/api/worlds/{world_id}/rewind/points")
async def rewind_points(world_id: int):
    """Доступные точки перемотки (сессия 34, C1): [{seq, ts, preview}].

    preview — начало действия игрока на этом ходу, чтобы список в UI был понятен
    («идти в таверну», «напасть на стражника»). Дамп state не отдаётся — только указатели.
    """
    if not db.get_world(world_id):
        raise HTTPException(404, "Мир не найден")
    pts = []
    for s in db.list_turn_snapshots(world_id):
        cur = db.get_latest_by_role(world_id, "player", before_seq=s["seq"] + 1)
        text = (cur or {}).get("content") or ""
        pts.append({"seq": s["seq"], "ts": s["ts"],
                    "preview": text.replace("\n", " ").strip()[:70] or "—"})
    return {"points": pts, "enabled": True}


@router.post("/api/worlds/{world_id}/rewind")
async def rewind_world(world_id: int, body: RewindIn):
    """⏪ Вернуть мир к началу хода seq (сессия 34, C1 + фиксы A1/A4).

    Состояние берётся из снапшота этого хода, более новые события убираются (delete) или
    сокрыляются (hide), ставшие недостоверными сводки удаляются, а развёрнутая история
    возвращается в недавнее окно. Векторы отменённых ходов вычищаются из ChromaDB —
    рассказчик не будет помнить то, чего уже не было.
    """
    if body.mode not in ("delete", "hide"):
        raise HTTPException(400, "mode: 'delete' или 'hide'")
    try:
        res = await rewind.rewind_to(world_id, body.seq, mode=body.mode, note="⏪ Перемотка")
    except LookupError:
        raise HTTPException(404, "Мир не найден")
    except ValueError as e:
        raise HTTPException(400, str(e))
    # реестр событий «последнего хода» устарел: часть id удалена/сокрыта, и следующий ↻
    # не должен опираться на них (сессия 36, п.3B)
    invalidate_turn_registry(world_id)
    res["world"] = db.get_world(world_id)
    return res


@router.post("/api/worlds/{world_id}/saves")
async def save_game(world_id: int, body: SaveIn):
    world = db.get_world(world_id)
    if not world:
        raise HTTPException(404, "Мир не найден")
    setting = json.loads(world["setting"])
    seq = db.latest_seq(world_id)
    save_id = db.create_save(world_id, body.name or f"Сохранение {seq}", setting, seq)
    return {"ok": True, "save": db.get_save(save_id)}


@router.get("/api/worlds/{world_id}/saves")
async def saves(world_id: int):
    return db.list_saves(world_id)


@router.post("/api/worlds/{world_id}/saves/{save_id}/load")
async def load_save(world_id: int, save_id: int):
    """Загрузить слот. Раньше это делалось `mark_folded()`’ом всего подряд (прошлое ≤ точки
    и будущее > точки) — после загрузки в недавнюю историю не попадало НИЧЕГО, и
    развернуть это было нельзя (баг A1, проверено вживую: 9 из 9 обменов свёрнуты).

    Теперь загрузка — тот же честный механизм, что и перемотка: состояние из слота,
    более новые события сокрыты (FOLD_HIDDEN — строки журнала не уничтожаются, историю
    можно вернуть), ставшие недостоверными сводки сняты с выдачи, а их покрытие возвращено
    в недавнее окно. Векторы ушедших ходов убираются из ChromaDB (A4).
    """
    world = db.get_world(world_id)
    save = db.get_save(save_id)
    if not world or not save or save["world_id"] != world_id:
        raise HTTPException(404, "Сохранение/мир не найден")
    setting = json.loads(save["setting"])
    target_seq = int(save["seq"] or 1) or 1
    try:
        res = await rewind.rewind_to(world_id, target_seq, setting=setting, mode="hide",
                                     note=f"💾 Загружено сохранение «{save['name']}» (ход {target_seq})")
    except ValueError as e:
        # состояние слота применимо, но таймлайн не поддаётся честному откату —
        # не делаем вид, что всё удалось: говорим, что именно не так
        raise HTTPException(400, f"Загрузка сохранения не удалась: {e}")
    invalidate_turn_registry(world_id)   # тот же смысл, что при перемотке (п.3B)
    # системное сообщение уже записал rewind_to (с пометкой о сохранении) — не дублируем
    return {"ok": True, "world": db.get_world(world_id), "setting": res["setting"],
            "event": res["event"], "unfolded": res["unfolded"],
            "removed_events": res["removed_events"]}


@router.delete("/api/worlds/{world_id}/saves/{save_id}")
async def delete_save(world_id: int, save_id: int):
    save = db.get_save(save_id)
    if not save or save["world_id"] != world_id:
        raise HTTPException(404, "Сохранение не найдено")
    db.delete_save(save_id)
    return {"ok": True}


# ───────────────────────────── Настройки / обратная связь ─────────────────────────────
@router.post("/api/worlds/{world_id}/settings")
async def update_gen_settings(world_id: int, body: GenSettingsIn):
    world = db.get_world(world_id)
    if not world:
        raise HTTPException(404, "Мир не найден")
    g = json.loads(world.get("gen_settings") or "{}")
    for k, v in [("temperature", body.temperature), ("top_p", body.top_p),
                 ("max_tokens", body.max_tokens)]:
        if v is not None:
            g[k] = v
    if body.context_tokens is not None:
        # до 262144 (256k) для облачных моделей; 512 — нижний предел окна памяти
        g["context_tokens"] = max(512, min(262144, int(body.context_tokens)))
    if body.logic_judge is not None:
        g["logic_judge"] = bool(body.logic_judge)
    # per-world параметры памяти: 0/пусто = авто (динамика от размера контекста)
    for k in ("rag_memory_k", "lore_rag_k", "lore_token_budget"):
        v = getattr(body, k, None)
        if v is not None:
            g[k] = max(0, int(v))
    # если пользователь поднял контекст выше реального n_ctx модели — снизим и покажем лимит
    g = await _context_guard(world_id, _world_providers(world), g)
    db.update_world(world_id, gen_settings=g)
    if body.narrator_id is not None:
        if not db.get_narrator(body.narrator_id):
            raise HTTPException(404, "Рассказчик не найден")
        db.update_world(world_id, narrator_id=body.narrator_id)
    return {"ok": True, "gen_settings": g, "narrator_id": body.narrator_id or world.get("narrator_id")}


@router.post("/api/worlds/{world_id}/events/{event_id}/feedback")
async def feedback(world_id: int, event_id: int, body: FeedbackIn):
    db.set_feedback(event_id, max(-1, min(1, body.value)))
    return {"ok": True}


@router.post("/api/worlds/{world_id}/state/patch")
async def patch_state(world_id: int, body: PatchIn):
    """Режим «Мастер»: точечные правки состояния (игрок/NPC/квесты/флаги)."""
    world = db.get_world(world_id)
    if not world:
        raise HTTPException(404, "Мир не найден")
    setting = json.loads(world["setting"])
    allowed = {"player", "enemies", "npc", "locations", "current_location", "quests",
               "flags", "weather", "time", "game_over", "style_notes"}
    def _merge(dst: dict, src: dict) -> None:
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                _merge(dst[k], v)
            else:
                dst[k] = v
    for key, val in body.patch.items():
        if key in allowed:
            if isinstance(val, dict) and isinstance(setting.get(key), dict):
                _merge(setting[key], val)
            else:
                setting[key] = val
    db.update_world(world_id, setting=setting)
    db.add_event(world_id, "system", "🛠 Мастер изменил состояние мира.")
    return {"ok": True, "setting": setting}


# ───────────────────────────── Экспорт / память ─────────────────────────────
# Роль-имя → человекочитаемая подпись в экспорте (бизнес-форматирование, не данные).
_EXPORT_ROLE_LABELS = {
    "narrator": "Рассказчик", "player": "Игрок", "system": "СИСТЕМА",
    "dice": "Бросок", "summary": "Сводка",
}


def render_export_history(world: dict, events: list[dict]) -> str:
    """Собирает текстовый экспорт истории из данных мира и событий.
    Чистая бизнес/презентационная логика — не зависит от хранилища."""
    header = f"=== {world['name']} | тема: {world['theme']} | сложность: {world['difficulty']} ===\n"
    out = [header]
    for e in events:
        tag = _EXPORT_ROLE_LABELS.get(e["role"], e["role"])
        out.append(f"[{e['seq']}] {tag}: {e['content']}")
    return "\n\n".join(out) + "\n"


@router.get("/api/worlds/{world_id}/export")
async def export(world_id: int):
    world, events = db.export_data(world_id)
    if world is None:
        raise HTTPException(status_code=404, detail="Мир не найден")
    return PlainTextResponse(render_export_history(world, events), media_type="text/plain; charset=utf-8")


# ──────────────────── JSON-дамп мира (бэкап/перенос, сессия 33) ────────────────────

@router.get("/api/worlds/{world_id}/export/json")
async def export_json(world_id: int, download: bool = Query(False, description="заголовок Content-Disposition для скачивания файла")):
    """Полный JSON-дамп мира: состояние + ВСЕ события + карточки + лор + слоты + граф.

    В отличие от /export (человекочитаемый текст истории) это машиночитаемый снимок
    для бэкапа и переноса между машинами. Ключи API из provider_settings вырезаются.
    `?download=1` — отдать с заголовком Content-Disposition (сохранение файла браузером).
    """
    data = db.world_dump(world_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Мир не найден")
    slug = re.sub(r"[^\w\-]+", "_", (data["world"]["name"] or "world"))[:40]
    fname = f"{slug}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    headers = None
    if download:
        # HTTP-заголовки — latin-1: русское имя мира в filename= ломает ответ (UnicodeEncodeError).
        # Даём ASCII-фолбэк плюс RFC 5987 filename* с URL-кодированием — его понимают браузеры.
        ascii_name = re.sub(r"[^A-Za-z0-9._\-]", "_", fname)
        quoted = quote(fname, encoding="utf-8")
        headers = {"Content-Disposition": f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quoted}'}
    return JSONResponse(data, headers=headers)


@router.post("/api/worlds/import/json")
async def import_json(body: ImportIn):
    """Создаёт НОВЫЙ мир из дампа /export/json (тело — объект дампа).

    Ничего не затирает: всегда заводится отдельный мир. После импорта память (Chroma)
    перестраивается фоново, провайдеры/эмбеддинги резолвятся из текущего .env/админки
    (ключи в дампе не хранятся)."""
    try:
        new_id = db.restore_world(body.payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Некорректный дамп: {e}")
    asyncio.get_event_loop().create_task(_reindex_imported_world(new_id))
    w = db.get_world(new_id) or {}
    return {"ok": True, "world_id": new_id, "world": w,
            "note": "мир создан; память (RAG) перестраивается в фоне"}


async def _reindex_imported_world(world_id: int) -> None:
    """После импорта: заново индексируем в Chroma лор, карточки и обмены (в дампе их нет)."""
    try:
        world = db.get_world(world_id)
        if not world:
            return
        providers = _world_providers(world)
        await _index_world_lore(world_id, providers)
        cards = db.list_entities(world_id)
        if cards:
            await narrator.index_entities(world_id, cards)
        # Сессия 36, п.8: обмены читаются СТРАНИЦАМИ (по seq вперёд), а не все разом:
        # на перенесённом мире с десятками тысяч событий один SELECT держал бы в памяти
        # весь лог. Индексация и так идёт по одному обмену, пагинация её не меняет.
        # `pending` — незакрытое действие игрока с прошлой страницы (его ответ мог
        # уехать за границу окна: раньше пара «действие → ответ» просто терялась).
        page = 500
        after = 0
        pending: tuple[int, str] | None = None
        indexed = 0
        while True:
            rows = db.get_events_after(world_id, after, page)
            if not rows:
                break
            for e in rows:
                role = e["role"]
                if role == "player":
                    # действие без ответа остаётся неиндексированным (как и раньше)
                    pending = (e["seq"], e["content"])
                elif role == "narrator" and pending:
                    await narrator.index_exchange(world_id, pending[0], pending[1],
                                                  e["content"], provider=providers.get("embedding"))
                    pending = None
                    indexed += 1
            after = rows[-1]["seq"]
            if len(rows) < page:
                break
        log.info("reindex после импорта (world %s): обменов проиндексировано %d", world_id, indexed)
    except Exception as e:
        log.warning("reindex после импорта (world %s): %s", world_id, e)


@router.delete("/api/worlds/{world_id}")
async def delete_world(world_id: int):
    db.delete_world(world_id)
    try:
        await chroma_client.delete_by_where({"world_id": world_id})
    except Exception as e:
        # векторы удалённого мира останутся в Chroma (сожрут место и могут всплыть в поиске)
        log.warning("удаление мира %s: чистка векторов в Chroma не удалась: %s", world_id, e)
    return {"ok": True}


@router.get("/api/worlds/{world_id}/memory/search")
async def memory_search(world_id: int, q: str):
    setting = json.loads(db.get_world(world_id)["setting"])
    chunks = await narrator.retrieve_memory(world_id, q, setting, k=6)
    return {"results": chunks}