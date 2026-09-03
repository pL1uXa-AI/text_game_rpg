# -*- coding: utf-8 -*-
"""Сессия 40, п.9 (мир «Новый мир»): в карточках больше нет системных (машинных) имён.

Симптом: во вкладке «Карточки» — `player` и `trail_to_outpost` вместо «Игрок» и
«Тропа к Форпосту». Причина: фоновый архивариус (`memory.update_entity_cards`) вернул
`{"kind","key"}` без `name`, а код подставил ключ: `name = item.get("name") or key`.
Машинный id попадал и в контекст модели (`compact_entity`), так что дефект не только
косметический.

Правка — только отображение/подстановка того, что мир уже знает (законы 2 и 3):
движок НЕ выдумывает названия, а берёт их из состояния мира и НЕ даёт машинному id
перетереть человекочитаемое имя.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_looks_machine_name_only_catches_ids():
    from backend.memory import looks_machine_name as m
    assert m("player") and m("trail_to_outpost") and m("orin_shop") and m("first-quest")
    # человекочитаемые названия (в т.ч. технические обозначения) — НЕ машинные id
    for s in ("Игрок", "Тропа к Форпосту", "Кайден «Узорник» Велари", "V-28",
              "S.K.Y.", "Форпост Кристалл", ""):
        assert not m(s), f"ложное срабатывание на: {s!r}"


def test_card_name_from_setting_reads_world_state():
    from backend.memory import card_name_from_setting as cn
    st = {"npc": {"player": {"name": "Игрок"}},
          "locations": {"trail_to_outpost": {"name": "Тропа к Форпосту"}},
          "quests": {"q1": {"title": "Голос из Колыбели"}},
          "shops": {"s1": {"id": "orin_shop", "name": "Лавка Орину"}}}
    assert cn("npc", "player", st) == "Игрок"
    assert cn("location", "trail_to_outpost", st) == "Тропа к Форпосту"
    assert cn("quest", "q1", st) == "Голос из Колыбели"
    assert cn("shop", "orin_shop", st) == "Лавка Орину"   # поиск и по полю id
    assert cn("item", "player", st) == ""                  # у предметов своей секции нет
    assert cn("npc", "ghost", st) == ""                    # нет в состоянии — не выдумываем
    assert cn("npc", "player", {}) == ""


def test_resolve_card_name_prefers_human_and_blocks_machine_overwrite():
    from backend.memory import resolve_card_name as rn
    st = {"npc": {"player": {"name": "Игрок"}},
          "locations": {"trail_to_outpost": {"name": "Тропа к Форпосту"}}}
    # живой случай: модель прислала только kind+key
    assert rn("npc", "player", "", "", st) == "Игрок"
    assert rn("location", "trail_to_outpost", "", "", st) == "Тропа к Форпосту"
    # имя модели — приоритетнее состояния (мастер мог переименовать в тексте)
    assert rn("npc", "player", "Странник", "Игрок", st) == "Странник"
    # машинное НЕ затирает уже человекочитаемое (поздний проход без name)
    assert rn("npc", "player", "player", "Игрок", st) == "Игрок"
    # ключ не найден в состоянии — берём прежнее имя карточки
    assert rn("event", "e1", "", "Первый выход к Форпосту", st) == "Первый выход к Форпосту"
    # совсем нечего брать — остаётся ключ (лучше, чем пустое имя)
    assert rn("event", "e1", "", "", st) == "e1"


def test_archivist_prompt_requires_human_readable_name():
    """В промпте архивариуса есть прямое требование заполнять `name` человекочитаемо."""
    src = (ROOT / "backend" / "memory.py").read_text(encoding="utf-8")
    assert re.search(r"ОБЯЗАТЕЛЬНО заполняй name", src), "требование к name пропало из промпта"


def test_upsert_entity_still_keeps_key_fallback():
    """Нижний слой БД остаётся как есть: `name or entity_key` — страховка, а не источник."""
    src = (ROOT / "backend" / "db.py").read_text(encoding="utf-8")
    assert '"name": name or entity_key' in src


def test_repair_machine_card_names(api_client):
    """Само-исцеление уже записанных карточек: машинное имя -> имя из состояния мира.
    Правит только те, чьё настоящее имя мир ЗНАЕТ, и только машинные (идемпотентно)."""
    from backend import db, memory
    client, _ = api_client
    wid = client.post("/api/worlds", json={"theme_id": "custom", "name": "т",
                                           "custom_plot": "Ты идёшь по тропе."}).json()["world_id"]
    setting = {"player": {"hp": 10}, "locations": {"trail_to_outpost": {"name": "Тропа к Форпосту"}},
               "npc": {"player": {"name": "Игрок"}}, "quests": {}, "flags": {}}
    db.update_world(wid, setting=setting)
    db.upsert_entity(wid, "location", "trail_to_outpost", name="trail_to_outpost", summary="туман")
    db.upsert_entity(wid, "npc", "player", name="player")
    # карточка, имени которой в состоянии нет, — чинить нечем, остаётся как есть
    db.upsert_entity(wid, "event", "ritual", name="ritual")
    # человекочитаемое имя трогать нельзя
    db.upsert_entity(wid, "item", "crystal", name="Синий кристалл Порядка")
    assert memory.repair_machine_card_names(wid, setting) == 2
    by_key = {(e["kind"], e["entity_key"]): e["name"] for e in db.list_entities(wid)}
    assert by_key[("location", "trail_to_outpost")] == "Тропа к Форпосту"
    assert by_key[("npc", "player")] == "Игрок"
    assert by_key[("event", "ritual")] == "ritual", "имя выдумано там, где мир его не знает"
    assert by_key[("item", "crystal")] == "Синий кристалл Порядка"
    # идемпотентно: второй проход не находит чего править
    assert memory.repair_machine_card_names(wid, setting) == 0
    assert memory.repair_machine_card_names(wid, {}) == 0
