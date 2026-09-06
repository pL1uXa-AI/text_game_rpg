# -*- coding: utf-8 -*-
"""Интеграционный тест «создать мир → 10 ходов → проверить память» (сессия 30).

Герметичный сквозной сценарий: без реальной сети/ChromaDB/облака (всё заглушено
как в conftest), но по ПОЛНОМУ контуру: create_world → N действий с директивами →
состояние мира меняется, события копятся, карточки знаний создаются, RAG-функции
(retrieve_memory/index_exchange через заглушки) корректно вызываются.

Дополнительно проверяет новый слой декомпозиции: память/лор/генерация персонажа
живут в новых модулях (backend/memory.py, lore_retriever.py, character_generator.py),
а narrator.py — тонкий фасад поверх них.
"""
from __future__ import annotations

from backend import narrator
from backend import character_generator, lore_retriever, memory as memory_mod


def test_decomposition_facade():
    """narrator.* — реэкспорт из новых модулей (тонкий фасад)."""
    assert narrator.retrieve_memory is memory_mod.retrieve_memory
    assert narrator.index_exchange is memory_mod.index_exchange
    assert narrator.summarize_and_compress is memory_mod.summarize_and_compress
    assert narrator.retrieve_lore is lore_retriever.retrieve_lore
    assert narrator.seed_lore_from_theme is lore_retriever.seed_lore_from_theme
    assert narrator.generate_character is character_generator.generate_character
    assert narrator.apply_character is character_generator.apply_character
    assert narrator.generate_opening is character_generator.generate_opening
    assert narrator.compact_entity is memory_mod.compact_entity


def test_world_ten_turns_memory_and_state(api_client, monkeypatch):
    """Создать мир → 10 ходов → проверить, что состояние развивается, события копятся,
    а карточки знаний фиксируют механику (память не противоречит)."""
    client, holder = api_client
    # conftest подменяет ensure_knowledge_cards на noop (герметичность) — для этого теста
    # включаем РЕАЛЬНУЮ детерминированную логику карточек знаний.
    monkeypatch.setattr(narrator, "ensure_knowledge_cards", memory_mod.ensure_knowledge_cards)
    wid = client.post("/api/worlds", json={"theme_id": narrator.THEMES[0]["id"], "name": "E2E"}).json()["world_id"]
    assert wid

    # 10 ходов с разными директивами: золото, предмет, навык, эффект, квест, враг, локация
    turns = [
        ("Поднимаю кошель.", '<<ENGINE>>{"player": {"gold": 15}}'),
        ("Покупаю зелье.", '<<ENGINE>>{"add_item": [{"name": "Зелье лечения", "qty": 1, "desc": "лечит"}]}'),
        ("Учу навык.", '<<ENGINE>>{"skill_add": {"name": "Огненный шар", "rank": "B", "kind": "магический", "mp_cost": 5}}'),
        ("Меняю класс.", '<<ENGINE>>{"class": "Маг"}'),
        ("Накладываю эффект.", '<<ENGINE>>{"effect_add": {"name": "отравлен", "turns": 3, "damage": 2, "desc": "яд"}}'),
        ("Беру квест.", '<<ENGINE>>{"quest": {"id": "find_book", "title": "Найти гримуар", "desc": "…", "status": "active"}}'),
        ("Встречаю врага.", '<<ENGINE>>{"enemy_add": {"id": "wolf", "name": "Волк", "hp": 25}}'),
        ("Перехожу в подвал.", '<<ENGINE>>{"location_add": {"id": "cellar", "name": "Подвал", "desc": "тёмный"}, "move": "cellar"}'),
        ("Получаю титул.", '<<ENGINE>>{"title": "Ветеран", "reputation": {"гильдия": 2}}'),
        ("Осматриваюсь.", "Вокруг тихо. Ничего примечательного."),
    ]
    for i, (text, reply) in enumerate(turns):
        holder["reply"] = reply
        r = client.post(f"/api/worlds/{wid}/action", json={"text": text})
        assert r.status_code == 200, f"ход {i}: {r.text}"
        assert r.json()["state"]["player"]["hp"] > 0, f"ход {i}: игрок жив"

    # ── История: 10 ходов накопились ──
    # A8 (аудит 41): /history отдаёт страницу {events, truncated}
    hist = client.get(f"/api/worlds/{wid}/history").json()["events"]
    players = [e for e in hist if e["role"] == "player"]
    assert len(players) == 10, f"10 действий игрока, получили {len(players)}"

    # ── Состояние: механика применилась ──
    s = client.get(f"/api/worlds/{wid}").json()["setting"]
    assert s["player"]["gold"] >= 15, "золото за кошель"
    assert any(i["name"] == "Зелье лечения" for i in s["player"]["inventory"]), "предмет в инвентаре"
    assert "Огненный шар" in s["player"]["skills"], "навык выучен"
    assert s["player"]["class"] == "Маг", "класс сменился"
    # эффект был наложен на 5-м ходу и (при авто-тике) истёк к 10-му — проверяем в событиях
    eff_seen = any("отравлен" in (e.get("content") or "") for e in hist)
    assert eff_seen, "эффект наложен (виден в событиях)"

    # ── Карточки знаний: память зафиксировала механику (детерминированно, без LLM) ──
    from backend import db as db_mod
    cards = db_mod.list_entities(wid)
    kinds = {c["kind"] for c in cards}
    assert "skill" in kinds and "class" in kinds and "item" in kinds and "quest" in kinds, \
        f"карточки знаний созданы: {sorted(kinds)}"
    # навык в карточке с рангом
    skill_card = next((c for c in cards if c["kind"] == "skill" and c["entity_key"] == "Огненный шар"), None)
    assert skill_card, "карточка навыка «Огненный шар»"
    import json as _json
    smeta = skill_card["meta"]
    if isinstance(smeta, str):
        smeta = _json.loads(smeta)
    assert smeta.get("rank") == "B", "ранг навыка в карточке"
    # предмет с описанием
    item_card = next((c for c in cards if c["kind"] == "item" and c["entity_key"] == "Зелье лечения"), None)
    assert item_card, "карточка предмета"

    # ── select_relevant_entities: активный квест/знания приоритетны ──
    # (карточка локации создаётся архивариусом — он заглушен в conftest, поэтому явно заводим)
    from backend import db as db_mod2
    db_mod2.upsert_entity(wid, "location", "cellar", name="Подвал", summary="тёмный подвал", meta={"location": "cellar"}, seq=1)
    rel = narrator.select_relevant_entities(wid, s, "иду в подвал за гримуаром")
    rel_keys = {(c["kind"], c["entity_key"]) for c in rel}
    assert ("quest", "find_book") in rel_keys, "активный квест в релевантных карточках"
    assert ("location", "cellar") in rel_keys, "текущая локация в релевантных"
    assert ("skill", "Огненный шар") in rel_keys, "знание (навык) в релевантных"

    # ── compact_entity/format_entity_cards: карточки читаемы для модели ──
    txt = narrator.format_entity_cards(rel[:3])
    assert "«" in txt and "Статус:" in txt, "формат карточек для промпта"
    qcard = next(c for c in cards if c["kind"] == "quest" and c["entity_key"] == "find_book")
    qtxt = narrator.compact_entity(qcard)
    assert "Квест" in qtxt and "Найти гримуар" in qtxt, "карточка квеста читаема"


def test_lore_pipeline_hermetic(api_client, monkeypatch):
    """Лор-контур: сидинг → чанкинг → индексация (заглушка) → retrieve_lore (фолбэк без эмбеддингов)."""
    client, _ = api_client
    wid = client.post("/api/worlds", json={"theme_id": narrator.THEMES[0]["id"], "name": "Lore"}).json()["world_id"]

    # Сидинг из темы (сюжет-файл) создаёт статьи
    from backend import db as db_mod
    theme = narrator.get_theme(narrator.THEMES[0]["id"])
    entries = narrator.seed_lore_from_theme(wid, theme)
    if not entries:  # у сюжета может не быть лора — пропускаем проверку без сбоя
        return
    assert db_mod.list_lore(wid), "статьи лора созданы"

    # Чанкинг: большая статья режется по абзацам/предложениям
    big = "Абзац первый.\n\n" + "Предложение. " * 300
    chunks = narrator._chunk_lore(big, max_tokens=120)
    assert len(chunks) > 1, "большая статья разбивается на чанки"
    assert all(c.strip() for c in chunks)

    # retrieve_lore с выключенными эмбеддингами (conftest: EMBEDDING_PROVIDER=none) — фолбэк свежими статьями
    setting = client.get(f"/api/worlds/{wid}").json()["setting"]
    out = await_narrator_retrieve_lore(wid, "осматриваюсь", setting)
    assert isinstance(out, list), "retrieve_lore возвращает список"
    if out:
        assert out[0].startswith("📖"), "строки блока [ЛОР МИРА] в формате «📖 Заголовок: …»"


def await_narrator_retrieve_lore(wid: int, action: str, setting: dict) -> list[str]:
    import asyncio
    return asyncio.run(narrator.retrieve_lore(wid, action, setting))


def test_quest_cards_no_bio_duplication_and_human_name(api_client, monkeypatch):
    """Регрессия (баг «карточки квестов»): ensure_knowledge_cards не должен копить desc
    в bio (повторный вызов = дубликат) и должен использовать человекочитаемый title,
    а не латинский id в поле name."""
    client, holder = api_client
    monkeypatch.setattr(narrator, "ensure_knowledge_cards", memory_mod.ensure_knowledge_cards)
    from backend import db as db_mod

    wid = client.post("/api/worlds", json={"theme_id": narrator.THEMES[0]["id"], "name": "E2E-q"}).json()["world_id"]
    # заводим квест в состоянии
    patch = {"quests": {"faction_alignment": {
        "id": "faction_alignment", "title": "Сделка с демонами",
        "desc": "Выбрать, кому помочь: Культу Хаоса или Альянсу Клинков.", "status": "active"}}}
    r = client.post(f"/api/worlds/{wid}/state/patch", json={"patch": patch})
    assert r.status_code == 200, r.text

    setting = client.get(f"/api/worlds/{wid}").json()["setting"]
    # дважды вызываем ensure_knowledge_cards (как это делает движок каждый ход)
    narrator.ensure_knowledge_cards(wid, setting, seq=1)
    narrator.ensure_knowledge_cards(wid, setting, seq=2)

    cards = db_mod.list_entities(wid)
    qcard = next((c for c in cards if c["kind"] == "quest" and c["entity_key"] == "faction_alignment"), None)
    assert qcard, "карточка квеста создана"
    assert qcard["name"] == "Сделка с демонами", f"человекочитаемый title, а не id: {qcard['name']!r}"
    bio = qcard.get("bio") or ""
    desc = patch["quests"]["faction_alignment"]["desc"]
    assert bio.count(desc) <= 1, f"desc не должен дублироваться в bio: {bio!r}"
    # summary содержит описание (один раз), а bio — пустой/без повторов
    assert desc in (qcard.get("summary") or "")
    assert bio.count("•") <= 1, "bio не должен накапливать маркеры-дубликаты"


def test_item_card_gets_description_later(api_client, monkeypatch):
    """Сессия 40, п.2: предмет выдан без описания → рассказчик закрепил его item_update →
    карточка знания «предмет» обязана получить это описание и НЕ терять его на следующих
    ходах (раньше при пустом desc карточке вписывалась заглушка «в инвентаре игрока»)."""
    client, holder = api_client
    monkeypatch.setattr(narrator, "ensure_knowledge_cards", memory_mod.ensure_knowledge_cards)
    from backend import db as db_mod

    wid = client.post("/api/worlds", json={"theme_id": narrator.THEMES[0]["id"], "name": "E2E-item"}).json()["world_id"]

    holder["reply"] = 'Ты поднимаешь кристалл. <<ENGINE>>{"add_item": [{"name": "Синий кристалл", "qty": 1}]}'
    client.post(f"/api/worlds/{wid}/action", json={"text": "подобрать кристалл"})

    holder["reply"] = ('Внутри — серебристые нити. <<ENGINE>>{"item_update": '
                       '{"name": "Синий кристалл", "desc": "чистая энергия Порядка"}}}')
    r = client.post(f"/api/worlds/{wid}/action", json={"text": "изучить кристалл"})
    assert r.status_code == 200
    inv = r.json()["state"]["player"]["inventory"]
    mine = next(x for x in inv if x["name"] == "Синий кристалл")
    assert mine["desc"] == "чистая энергия Порядка", inv

    card = next((c for c in db_mod.list_entities(wid)
                 if c["kind"] == "item" and c["entity_key"] == "Синий кристалл"), None)
    assert card and card["summary"] == "чистая энергия Порядка", card

    # ещё ход с пустым desc в состоянии не должен затереть наведённое описание
    holder["reply"] = "Ты идёшь дальше."
    client.post(f"/api/worlds/{wid}/action", json={"text": "идти дальше"})
    card = next((c for c in db_mod.list_entities(wid)
                 if c["kind"] == "item" and c["entity_key"] == "Синий кристалл"), None)
    assert card["summary"] == "чистая энергия Порядка", "описание карточки не должно перезаписываться заглушкой"
