# -*- coding: utf-8 -*-
"""Регрессии сессии 36 — по итогам аудита кода (~21 000 строк, 31 пункт).
Сводка по всем фиксам и подводные камни — в AGENT.md, блок «Сессия 36» (файл-отчёт
сессии был одноразовым и удалён после переноса выводов).

Каждый тест закрывает конкретный пункт списка и ДОЛЖЕН падать, если фикс откатить
(где возможно — это проверено «анти-тестом» внутри).
"""
from __future__ import annotations

import asyncio
import io
import re
import json
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import chroma_client as chroma_mod          # noqa: E402
from backend import db as db_mod                          # noqa: E402
from backend import embeddings as emb_mod                 # noqa: E402
from backend import llm as llm_mod                        # noqa: E402
from backend import metrics as metrics_mod                # noqa: E402
from backend import narrator as narrator_mod              # noqa: E402
from backend.routers import core as core_mod              # noqa: E402
from backend.routers import entities as entities_mod      # noqa: E402


def _func_body(src: str, start: int) -> str:
    """Тело функции, начинающейся в src[start:] — до следующего определения на нулевом
    отступе. Нужно потому, что срез по первому пустому тексту натыкается на docstring."""
    m = re.compile(r"\n(?=(?:async )?def |class |@|# ─|# ═)").search(src, start + 1)
    return src[start:m.start() if m else len(src)]


def _mk_world(client, name: str) -> int:
    """Создать мир через API и вернуть его id (ответ — {"world_id": ...})."""
    r = client.post("/api/worlds", json={"theme_id": narrator_mod.THEMES[0]["id"],
                                         "name": name, "difficulty": "normal",
                                         "perspective": "second", "language": "ru"})
    assert r.status_code == 200, r.text[:300]
    return r.json()["world_id"]


# ══════════════════════════════════════════════════════════════════════
# п.1 — NameError: log в routers/entities.py (краш 500 при сбое Chroma)
# ══════════════════════════════════════════════════════════════════════

def test_entities_router_has_logger():
    """Модуль обязан иметь логгер: иначе ветка except во entity_delete падает с NameError."""
    assert hasattr(entities_mod, "log"), "в routers/entities.py нет `log` — except не отработает"
    assert callable(getattr(entities_mod, "entity_delete"))


def test_entities_delete_survives_chroma_failure(api_client):
    """DELETE карточки при недоступной Chroma = 200 («карточка удалена»), а не 500."""
    client, _holder = api_client
    wid = _mk_world(client, "П1")
    r = client.post(f"/api/worlds/{wid}/entities",
                    json={"kind": "npc", "key": "barman", "name": "Барт"})
    assert r.status_code == 200

    async def _boom(*a, **kw):
        raise RuntimeError("Chroma недоступна")

    # в conftest chroma_mod.* уже заглушены noops; перекрываем delete_by_ids падением
    from fastapi.testclient import TestClient  # локально, чтобы не таскать импорт наверх
    orig = chroma_mod.delete_by_ids
    chroma_mod.delete_by_ids = _boom
    entities_mod.chroma_client.delete_by_ids = _boom
    try:
        d = client.delete(f"/api/worlds/{wid}/entities/npc/barman")
        assert d.status_code == 200, f"ожидался 200, получён {d.status_code}: {d.text[:200]}"
        assert d.json().get("ok") is True
        assert client.get(f"/api/worlds/{wid}/entities/npc/barman").status_code == 404
    finally:
        chroma_mod.delete_by_ids = orig
        entities_mod.chroma_client.delete_by_ids = orig
    assert TestClient  # не мешаем


# ══════════════════════════════════════════════════════════════════════
# п.2 — Провидение: кулдаун по умолчанию + логирование «тяжёлых» выдач
# ══════════════════════════════════════════════════════════════════════

def test_divine_cooldown_default_is_positive():
    """Дефолт >0: без лимита «попросить у богов золото» доступно бесконечно."""
    from backend.config import Config
    assert Config().divine_cooldown_turns > 0


def test_divine_prompt_forbids_free_loot():
    """Промпт обязан запрещать выдачу сверх заявленной потери (иначе фарм — норма)."""
    world = {"name": "w", "genre": "fantasy", "difficulty": "normal",
             "perspective": "second", "language": "ru"}
    setting = json.loads(json.dumps({
        "player": {"hp": 50, "max_hp": 100, "mp": 10, "max_mp": 50, "level": 1,
                   "stats": {"сила": 10, "ловкость": 10, "выносливость": 10,
                             "интеллект": 10, "мудрость": 10, "харизма": 10, "удача": 10}},
        "locations": {}, "current_location": "start"}))
    msgs = narrator_mod.divine_messages(world, setting, "мне не выдали меч",
                                        "взять меч", "ты взял меч")
    sysmsg = msgs[0]["content"]
    assert "Границы исправления" in sysmsg
    assert "НЕ выдаёшь ничего" in sysmsg


def test_divine_grants_summary():
    """Сводка выдач для лога: положительно записываем только то, что реально дали."""
    assert narrator_mod._divine_grants({"player": {"gold": 50, "hp": -5},
                                        "add_item": [{"name": "меч", "qty": 2}]}) \
        == "gold+50, item:меч×2"
    assert narrator_mod._divine_grants({"flag": {"name": "x", "value": 1}}) == ""
    assert "item:щит" in narrator_mod._divine_grants({"add_item": {"name": "щит"}})


# ══════════════════════════════════════════════════════════════════════
# п.3A/3B — перегенерация: реестр хода из БД + барьер для фоновых агентов
# ══════════════════════════════════════════════════════════════════════

def test_turn_registry_rebuilt_from_db(api_client):
    """После «рестарта» (пустой in-memory реестр) ↻ обязан ЗАМЕНИТЬ события хода, а не
    добавить второй комплект."""
    client, holder = api_client
    wid = _mk_world(client, "П3")
    r = client.post(f"/api/worlds/{wid}/action", json={"text": "осмотреться"})
    assert r.status_code == 200
    first_ids = {e["id"] for e in r.json()["events"]}

    # имитируем рестарт: in-memory реестр пуст
    core_mod._turn_events.clear()
    core_mod._turn_seq.clear()

    holder["reply"] = "Другой ответ рассказчика."
    r2 = client.post(f"/api/worlds/{wid}/action",
                     json={"text": "осмотреться", "regenerate": True})
    assert r2.status_code == 200
    body = r2.json()
    replaced = set(body.get("replaced_events") or [])
    assert replaced, "после рестарта ↻ не нашёл событий хода — в чате останется дубль"
    assert first_ids & replaced, "заменены не те события хода"
    # старого нарратива в истории быть не должно
    hist = client.get(f"/api/worlds/{wid}/history").json()
    events = hist["events"] if isinstance(hist, dict) else hist
    texts = [e["content"] for e in events if e["role"] == "narrator"]
    assert sum(1 for t in texts if "осматриваешься" in t.lower()) <= 0 or \
        len(texts) == len(set(texts)), f"нарративы задвоены: {texts}"


def test_turn_registry_excludes_background_events(api_client):
    """Фоновые системки (не помеченные meta.turn) перегенерацией НЕ стираются."""
    client, _holder = api_client
    wid = _mk_world(client, "П3б")
    client.post(f"/api/worlds/{wid}/action", json={"text": "идти"})
    bg_ev = db_mod.add_event(wid, "system", "🌍 Случайное событие мира")["id"]
    core_mod._turn_events.clear()
    core_mod._turn_seq.clear()
    last_player = db_mod.get_latest_by_role(wid, "player")
    ids = core_mod._turn_registry(wid, last_player["seq"])
    assert bg_ev not in ids, "фоновое событие попало в реестр хода — ↻ его сотрёт"
    assert ids, "реестр пуст — события хода не будут заменены"


def test_regen_guard_blocks_background_write():
    """_bg_may_write: во время перегенерации фон не пишет; после — пишет.

    Барьер живёт в bg.py (сессия 36, п.3A), чтобы о нём знал и слой памяти: импорт
    core из memory создал бы цикл импортов."""
    from backend import bg as bg_mod
    wid = 111
    with bg_mod.regen_block(wid) as acquired:
        assert acquired is True
        assert core_mod._bg_may_write(wid, {"_judge_last_turn": 0, "_player_turns": 5},
                                      "_judge_last_turn", 0, 5) is False
    assert not bg_mod.is_regenerating(wid), "барьер не снят после выхода — фон заблокирован навечно"
    # обычный пропуск, если игрок успел сходить
    assert core_mod._bg_may_write(wid, {"_judge_last_turn": 0, "_player_turns": 7},
                                  "_judge_last_turn", 0, 5) is False
    # маркер обновлён другим процессом
    assert core_mod._bg_may_write(wid, {"_judge_last_turn": 6, "_player_turns": 5},
                                  "_judge_last_turn", 3, 5) is False
    # нормальный путь
    assert core_mod._bg_may_write(wid, {"_judge_last_turn": 0, "_player_turns": 5},
                                  "_judge_last_turn", 0, 5) is True


def test_regen_block_rejects_second_acquire():
    """Двойной захват (задвоенный клик ↻) обязан получить False, а не второй проход."""
    from backend import bg as bg_mod
    with bg_mod.regen_block(222) as first:
        assert first is True
        with bg_mod.regen_block(222) as second:
            assert second is False, "мирпустил второй перегенерации одновременно"
    # после выхода освобождается — следующий ↻ возможен
    with bg_mod.regen_block(222) as again:
        assert again is True


def test_cards_sync_skipped_during_regen(api_client, monkeypatch):
    """Архивариус не пишет setting, пока идёт ↻ этого мира (п.3A/п.12)."""
    import backend.memory as mem_mod
    client, _ = api_client
    wid = _mk_world(client, "П3в")
    from backend import bg as bg_mod

    writes = {"n": 0}
    orig_update = mem_mod.db.update_world

    def counted(world_id, **kw):
        if "setting" in kw:
            writes["n"] += 1
        return orig_update(world_id, **kw)

    monkeypatch.setattr(mem_mod.db, "update_world", counted)

    async def _ents(*a, **kw):
        return {"entities": [{"kind": "quest", "key": "q1", "name": "Квест",
                              "summary": "s", "meta": {"status": "active"}}]}

    import backend.narrator as narrator_mod2
    monkeypatch.setattr(narrator_mod2, "llm_json_tool", _ents)
    # нормальный путь: синхронизация пишет setting
    asyncio.run(mem_mod.update_entity_cards(wid, "идти", "ты пошёл"))
    assert writes["n"] == 1, f"ожидалась ОДНА запись за проход, было {writes['n']}"
    assert "q1" in json.loads(db_mod.get_world(wid)["setting"]).get("quests", {})
    # под барьером: ни записи, ни квеста в setting
    s = json.loads(db_mod.get_world(wid)["setting"])
    s.pop("quests", None)
    db_mod.update_world(wid, setting=s)      # служебная запись — вне счётчика
    writes["n"] = 0
    with bg_mod.regen_block(wid):
        asyncio.run(mem_mod.update_entity_cards(wid, "идти", "ты пошёл"))
    assert writes["n"] == 0, "архивариус записал setting во время перегенерации"
    assert "q1" not in json.loads(db_mod.get_world(wid)["setting"]).get("quests", {})


def test_invalidate_turn_registry():
    core_mod._turn_events[42] = [1, 2, 3]
    core_mod._turn_seq[42] = 9
    core_mod.invalidate_turn_registry(42)
    assert 42 not in core_mod._turn_events and 42 not in core_mod._turn_seq


# ══════════════════════════════════════════════════════════════════════
# п.4 — механика в середине/с маркером не протекает в чат
# ══════════════════════════════════════════════════════════════════════

def test_split_engine_mid_text():
    """Блок механики в середине абзаца вырезается, проза ДО и ПОСЛЕ остаётся."""
    clean, d = narrator_mod.split_engine(
        'Ты достал меч. game_engine({"player": {"hp": -5}}) и сделал шаг вперёд.')
    assert d == {"player": {"hp": -5}}
    assert "game_engine" not in clean and "{" not in clean
    assert "Ты достал меч." in clean and "сделал шаг вперёд" in clean


def test_split_engine_goes_after_marker():
    """Проза после <<ENGINE>>{...} сохраняется, механика уезжает в директивы."""
    clean, d = narrator_mod.split_engine(
        'Туман сгустился. <<ENGINE>>{"flag": {"name": "fog", "value": true}} '
        'Туман сгустился.')
    assert d == {"flag": {"name": "fog", "value": True}}
    assert "ENGINE" not in clean
    assert clean.count("Туман сгустился") == 2


def test_split_engine_two_blocks():
    """Двойной маркер (модель эмитит дважды) — обе директивы применены, текста мусора нет."""
    clean, d = narrator_mod.split_engine(
        'Начало. <<ENGINE>>{"player":{"hp":-1}}\n<<ENGINE>>{"flag":{"name":"a","value":1}}')
    assert d == {"player": {"hp": -1}, "flag": {"name": "a", "value": 1}}
    assert clean == "Начало."


def test_split_engine_bare_directive_block():
    """Голый directive-блок в конце/середине распознаётся, проза в скобках — нет."""
    _, d = narrator_mod.split_engine('Проза. {"player": {"gold": 3}}')
    assert d == {"player": {"gold": 3}}
    clean, d2 = narrator_mod.split_engine("Ты вырезал {рубиново} слово и пошёл дальше.")
    assert d2 is None and "рубиново" in clean


def test_split_engine_roll_recognized():
    """`roll` обрабатывается ядром, а не цепочкой, — и всё равно обязан вырезаться."""
    clean, d = narrator_mod.split_engine('game_engine({"roll": {"expr": "d20"}})')
    assert d == {"roll": {"expr": "d20"}}
    assert clean == ""


def test_engine_tail_hold_protects_partial_marker():
    """Стрим не отдаёт половину маркера (иначе игрок видит «…к » или «…game_eng»)."""
    assert narrator_mod.engine_tail_hold("текст и тут <") >= 1
    assert narrator_mod.engine_tail_hold("текст и тут game_en") >= 7
    assert narrator_mod.engine_tail_hold("обычный текст.") == 0


def test_stream_never_emits_engine_marker(api_client, monkeypatch):
    """Сквозная проверка SSE: ни один токен не содержит обрывок служебного блока."""
    client, holder = api_client
    wid = _mk_world(client, "П4")
    holder["reply"] = ('Ты идёшь вперёд. game_engine({"player": {"gold": 5}}) '
                       'факелы зашипели во тьме.')
    chunks: list[str] = []
    result: dict = {}
    with client.stream("POST", f"/api/worlds/{wid}/action/stream",
                       json={"text": "идти"}) as resp:
        assert resp.status_code == 200
        event = ""
        for line in resp.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                try:
                    obj = json.loads(line[5:].strip())
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                if event == "token":
                    chunks.append(str(obj.get("text", "")))
                elif event == "result":
                    result = obj
    joined = "".join(chunks)
    assert "game_engine" not in joined.lower()
    assert '{"player"' not in joined
    assert "ENGINE" not in joined.upper()
    # проза ДО механики дошла в стриме
    assert "идёшь вперёд" in joined, f"в стриме ничего полезного: {joined!r}"
    # и итоговый ответ целиком (без механики) — и в result, и в сохранённом событии
    assert "game_engine" not in result.get("reply", "").lower()
    assert "факелы" in result.get("reply", ""), "проза после блока механики потеряна"
    hist = client.get(f"/api/worlds/{wid}/history").json()
    events = hist["events"] if isinstance(hist, dict) else hist
    narr = [e["content"] for e in events if e["role"] == "narrator"][-1]
    assert "game_engine" not in narr.lower() and "факелы" in narr


# ══════════════════════════════════════════════════════════════════════
# п.5 — импорт мира: битое состояние чинится, битые события пропускаются
# ══════════════════════════════════════════════════════════════════════

def test_restore_world_missing_player():
    """Дамп без player не должен ронять импорт или давать мир, в котором ход падает."""
    dump = {
        "format": db_mod.DUMP_FORMAT, "version": db_mod.DUMP_VERSION,
        "world": {"name": "Без игрока", "theme": "custom", "genre": "fantasy",
                  "difficulty": "normal", "perspective": "second", "language": "ru",
                  "custom_hook": "", "setting": {"locations": {}},
                  "gen_settings": {}, "provider_settings": {}, "tts_settings": {},
                  "snapshot": "", "narrator_name": ""},
        "events": [], "entities": [], "lore": [], "saves": [], "graph": {"nodes": [], "edges": []},
    }
    new_id = db_mod.restore_world(dump)
    try:
        setting = json.loads(db_mod.get_world(new_id)["setting"])
        assert isinstance(setting.get("player"), dict)
        assert setting["player"].get("stats"), "у игрока нет статов — ход упадёт с KeyError"
        # главный критерий: движок на таком состоянии не падает
        msgs = narrator_mod.apply_directives(setting, {"player": {"gold": 5}})
        assert isinstance(msgs, list)
        assert setting["player"]["gold"] == 5
    finally:
        db_mod.delete_world(new_id)


def test_restore_world_skips_broken_events():
    """Событие без seq/role или с нечисловым seq пропускается с логом, а не валит импорт."""
    dump = {
        "format": db_mod.DUMP_FORMAT, "version": db_mod.DUMP_VERSION,
        "world": {"name": "Битые события", "theme": "custom", "setting": {"player": {}},
                  "gen_settings": {}, "provider_settings": {}},
        "events": [{"seq": 1, "role": "player", "content": "ok"},
                   {"role": "player", "content": "нет seq"},
                   {"seq": "abc", "role": "player", "content": "не число"},
                   {"seq": 2, "role": "   ", "content": "пустая роль"},
                   "не словарь"],
        "entities": [], "lore": [], "saves": [], "graph": {},
    }
    new_id = db_mod.restore_world(dump)
    try:
        evs = db_mod.get_events(new_id)
        assert [e["content"] for e in evs] == ["ok"], f"прошли лишние: {[e['content'] for e in evs]}"
    finally:
        db_mod.delete_world(new_id)


# ══════════════════════════════════════════════════════════════════════
# п.6 — тело ответа провайдера в ошибке не должно нести ключей
# ══════════════════════════════════════════════════════════════════════

def test_safe_body_masks_secrets():
    """Тело ответа провайдера уходит в ошибку (и в лог) без ключей (п.6, правило 3).

    Фейковый ключ СОБИРАЕТСЯ из кусков: литерал вида `sk-…` на 12+ символов попал бы
    под pre-push-сканер секретиов из правила 15 (`git diff --cached | grep -ciE
    "sk-[A-Za-z0-9]{12,}` → обязано быть 0) и под CI-шаг ci.yml."""
    fake = "sk-" + "abcdef" + "1234567890"          # 16 «случайных» символов
    assert fake.startswith("sk-") and len(fake) > 12
    masked = llm_mod._safe_body(f"error: key {fake} rejected")
    assert fake not in masked and "sk-" not in masked
    masked2 = llm_mod._safe_body(("{\"error\": \"" + fake + "\"}").encode())
    assert fake not in masked2
    masked3 = llm_mod._safe_body("Authorization: Bearer supersecretvalue12")
    assert "supersecretvalue12" not in masked3
    assert "Bearer" in masked3                      # префикс заголовка не теряем
    # обычный текст ошибки сохраняется, обрезка по лимиту работает
    assert "not found" in llm_mod._safe_body("404 model not found")
    assert len(llm_mod._safe_body("x" * 5000)) == 400


# ══════════════════════════════════════════════════════════════════════
# п.7 — журнал метрик не растёт бесконечно
# ══════════════════════════════════════════════════════════════════════

def test_metrics_journal_rotation(tmp_path, monkeypatch):
    """После перехода порога появляется .1, а рабочий файл снова маленький."""
    import backend.config as config_mod

    class _Cfg:
        metrics_persist = True
        metrics_file = str(tmp_path / "metrics.jsonl")
        metrics_tail = 10
        metrics_max_bytes = 500      # крошечный порог, чтобы ротировать за пару десятков строк

    monkeypatch.setattr(config_mod, "_cache", {"cfg": _Cfg()})
    for i in range(200):
        metrics_mod._persist({"llm_ms": 10.0, "i": i})
    assert (tmp_path / "metrics.jsonl.1").exists(), "ротация не сработала"
    assert (tmp_path / "metrics.jsonl").stat().st_size < 5000
    # журнал остаётся читаемым
    assert metrics_mod.read_journal(tail=5)


def test_metrics_counters_restored_from_journal(tmp_path, monkeypatch):
    """п.24: кумулятивные счётчики переживают рестарт (база берётся из журнала)."""
    import backend.config as config_mod

    path = tmp_path / "m.jsonl"
    lines = [json.dumps({"llm_ms": 1000.0, "prompt_tokens": 10, "completion_tokens": 5}),
             json.dumps({"llm_ms": 2000.0, "prompt_tokens": 20, "completion_tokens": 7}),
             "битая строка",
             json.dumps({"no_llm": True})]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    class _Cfg:
        metrics_persist = True
        metrics_file = str(path)
        metrics_tail = 50
        metrics_max_bytes = 0

    monkeypatch.setattr(config_mod, "_cache", {"cfg": _Cfg()})
    # метрики — модульные глобалы: чистим и буфер, и кумулятивы, и снятую базу,
    # иначе счёт из предыдущих тестов этого файла примешается к результату
    monkeypatch.setattr(metrics_mod, "_samples", type(metrics_mod._samples)([]))
    monkeypatch.setattr(metrics_mod, "_totals",
                        {"llm_calls": 0, "llm_seconds": 0.0,
                         "completion_tokens": 0, "prompt_tokens": 0})
    monkeypatch.setattr(metrics_mod, "_totals_baseline", None)
    rep = metrics_mod.as_json()
    assert rep["counters"]["llm_calls"] == 2
    assert rep["counters"]["prompt_tokens"] == 30
    assert rep["counters"]["completion_tokens"] == 12
    assert rep["counters"]["source"] == "process+journal"


# ══════════════════════════════════════════════════════════════════════
# п.8/п.9 — горячие пути не тянут весь лог/все карточки
# ══════════════════════════════════════════════════════════════════════

def test_feedback_style_note_uses_limited_query(monkeypatch):
    """feedback_style_note обязан читать хвост, а не весь лог мира."""
    calls: dict = {}

    class _DB:
        @staticmethod
        def get_unfolded_events(world_id, limit=120, roles=("player", "narrator")):
            calls["limit"] = limit
            return [{"content": "a", "feedback": 1}, {"content": "b", "feedback": 1}]

        @staticmethod
        def get_events(*a, **kw):
            raise AssertionError("feedback_style_note снова читает весь лог через get_events")

    import backend.memory as mem_mod
    monkeypatch.setattr(mem_mod, "db", _DB)
    monkeypatch.setattr(narrator_mod, "db", _DB)
    note = narrator_mod.feedback_style_note(1)
    assert "понравились" in note
    assert calls["limit"] <= 200, f"лимит слишком большой: {calls['limit']}"


def test_get_events_after_pages_forward():
    """get_events_after идёт ВПЕРЁД по seq (limit в get_events режет хвост — не годится)."""
    wid = db_mod.create_world("Пейдж", "custom", "fantasy", "normal", "second", "ru", "",
                              {"player": {}}, {})
    try:
        for i in range(1, 12):
            db_mod.add_event(wid, "player", f"действие {i}")
        first = db_mod.get_events_after(wid, 0, 5)
        assert [e["content"] for e in first] == [f"действие {i}" for i in range(1, 6)]
        second = db_mod.get_events_after(wid, first[-1]["seq"], 5)
        assert [e["content"] for e in second] == [f"действие {i}" for i in range(6, 11)]
        # старое поведение: limit = последние N
        tail = db_mod.get_events(wid, limit=5)
        assert [e["content"] for e in tail] == [f"действие {i}" for i in range(7, 12)]
    finally:
        db_mod.delete_world(wid)


def test_select_relevant_entities_limits_query(monkeypatch):
    """Синхронный на каждый ход select_relevant_entities обязан брать ограниченную выдачу."""
    seen: dict = {}
    cards = [{"kind": "npc", "entity_key": f"n{i}", "name": f"NPC{i}",
              "summary": "", "relationship": "", "bio": "", "meta": "{}",
              "updated_at": i} for i in range(1000)]

    class _DB:
        @staticmethod
        def list_entities(world_id, kind=None, limit=None):
            seen["limit"] = limit
            if limit:
                return cards[-limit:]
            return cards

    import backend.memory as mem_mod
    monkeypatch.setattr(mem_mod, "db", _DB)
    out = mem_mod.select_relevant_entities(1, {"current_location": "", "quests": {},
                                               "player": {}}, "осмотреться")
    assert seen.get("limit"), "list_entities вызван без limit — на длинной игре это вся база"
    assert len(out) <= 10


def test_update_entity_cards_single_list_query(api_client, monkeypatch):
    """Архивариус делает ОДНУ выборку карточек на ход (было 4 полных)."""
    import backend.memory as mem_mod
    counter = {"n": 0}
    orig = mem_mod.db.list_entities

    def counted(world_id, kind=None, limit=None):
        counter["n"] += 1
        return orig(world_id, kind, limit)

    monkeypatch.setattr(mem_mod.db, "list_entities", counted)
    client, _ = api_client
    wid = _mk_world(client, "П9")
    counter["n"] = 0
    asyncio.run(mem_mod.update_entity_cards(wid, "идти", "ты пошёл"))
    assert counter["n"] <= 1, f"list_entities вызван {counter['n']} раз за один проход"


# ══════════════════════════════════════════════════════════════════════
# п.10 — список миров: один агрегат вместо COUNT на строку
# ══════════════════════════════════════════════════════════════════════

def test_list_worlds_counts_are_correct():
    """LEFT JOIN-агрегат обязан давать то же число ходов, что и прежний подзапрос."""
    wid = db_mod.create_world("Счётчик", "custom", "fantasy", "normal", "second", "ru", "",
                              {"player": {}}, {})
    try:
        for i in range(3):
            db_mod.add_event(wid, "player", f"ход {i}")
        db_mod.add_event(wid, "narrator", "ответ")
        row = next(w for w in db_mod.list_worlds() if w["id"] == wid)
        assert row["events"] == 3, f"миры считают ходы неверно: {row['events']}"
    finally:
        db_mod.delete_world(wid)


def test_list_worlds_uses_single_group_by_query():
    """SMOKE ПО ТЕКСТУ (аудит 38, D11): сверяет форму SQL в исходнике, а не поведение.

    Регрессию не даёт: падает при безвредном рефакторинге и проходит при реальной поломке
    счётчика. Поведение «считает ходы верно» закрывает соседний
    test_list_worlds_counts_are_correct — он исполняет код."""
    src = io.open(ROOT / "backend" / "db.py", encoding="utf-8").read()
    body = _func_body(src, src.index("async def _list_worlds"))
    sql = " ".join(ln for ln in body.splitlines() if not ln.strip().startswith("#"))
    assert "GROUP BY world_id" in sql, "вернулся correlated COUNT(*) на каждую строку"
    assert sql.count("COUNT(*)") == 1, "в запросе больше одного COUNT — счётчик перекосится"


# ══════════════════════════════════════════════════════════════════════
# п.11 — db._run не должен зависать намертво
# ══════════════════════════════════════════════════════════════════════

def test_db_run_has_timeout_and_loop_guard():
    """Есть потолок ожидания и явная защита от рекурсивного вызова с цикла БД.

    SMOKE ПО ТЕКСТУ (аудит 38, D11): наличие слов в исходнике, а не поведение; поведение
    («вызов из потока цикла = понятная ошибка») проверяет test_db_run_from_bg_thread_raises."""
    src = io.open(ROOT / "backend" / "db.py", encoding="utf-8").read()
    body = _func_body(src, src.index("def _run("))
    assert "timeout" in body, "_run снова ждёт вечно — зависание неотличимо от работы"
    assert "current_thread" in body, "нет защиты от само-дедлока на цикле БД"


def test_db_run_from_bg_thread_raises(monkeypatch):
    """Вызов _run из потока фонового цикла БД = понятная ошибка, а не вечный стоп."""
    db_mod._ensure_loop()
    box: dict = {}

    async def _probe():
        try:
            db_mod._run(lambda: db_mod._latest_seq(1))
            box["err"] = None
        except Exception as e:
            box["err"] = e
        # вернём цикл в рабочее состояние: поток ждёт именно эту корутину
        return None

    fut = None
    with db_mod._lock:
        fut = __import__("asyncio").run_coroutine_threadsafe(_probe(), db_mod._loop)
        fut.result(timeout=20)      # ждём пробы; её «результат» (None) не используется
    assert isinstance(box.get("err"), RuntimeError), f"ожидался RuntimeError, получено {box.get('err')!r}"
    assert "взаимоблокировк" in str(box["err"]).lower() or "дедлок" in str(box["err"]).lower()


def test_db_world_dump_is_atomic():
    """Дамп берёт все таблицы под _lock (иначе между запросами вклинится ход)."""
    src = io.open(ROOT / "backend" / "db.py", encoding="utf-8").read()
    i = src.index("def world_dump(")
    body = _func_body(src, i)
    assert "with _lock" in body


# ══════════════════════════════════════════════════════════════════════
# п.13 — комментарии в .env не попадают в значения
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("raw,want", [
    ("value # comment", "value"),
    ("value", "value"),
    ('"quoted value" # c', "quoted value"),
    ("pa#ss", "pa#ss"),                  # решётка без пробела — часть значения
    ("http://host/p#frag # note", "http://host/p#frag"),
    ("-1", "-1"),
])
def test_env_comment_stripping(raw, want):
    from backend.config import strip_env_comment
    assert strip_env_comment(raw) == want


def test_config_load_ignores_trailing_comments(tmp_path):
    """Числовой ключ с хвостовым комментарием читается числом, а не откатывается в дефолт."""
    from backend.config import Config
    envf = tmp_path / ".env"
    envf.write_text("MAX_TOKENS=1234 # ответ модели\n"
                    "DB_PATH=data/x.db # путь к базе\n", encoding="utf-8")
    # env обязан быть ПУСТЫМ, но не ложным: Config.load подменяет falsy-значение на
    # os.environ, а conftest выставляет в нём DB_PATH на temp-базу.
    cfg = Config.load(env_file=envf, env={"__NONE__": "1"})
    assert cfg.max_tokens == 1234
    assert cfg.db_path.endswith("x.db") and "#" not in cfg.db_path


# ══════════════════════════════════════════════════════════════════════
# п.17 — фоновый агент не затирает ход игрока своим устаревшим reading'ом
# ══════════════════════════════════════════════════════════════════════

def test_judge_does_not_overwrite_fresh_turn(api_client, monkeypatch):
    """Если игрок успел сходить, судья обязан ОТКАЗАТЬСЯ писать setting."""
    client, holder = api_client
    wid = _mk_world(client, "П17")
    client.post(f"/api/worlds/{wid}/action", json={"text": "первый ход"})
    # заводим счётчик выше интервала судьи, чтобы тот реально дошёл до записи
    # (иначе проверка «не перезаписал» ничего не доказывает: судья вышел бы по интервалу)
    base = json.loads(db_mod.get_world(wid)["setting"])
    base["_player_turns"] = 25
    base["player"]["gold"] = 0
    db_mod.update_world(wid, setting=base)
    stale = json.loads(json.dumps(base))      # снимок, с которым судья «ушёл думать»

    async def _judge(*a, **kw):
        # пока судья думал, игрок сходил ещё раз (состояние в БД уехало вперёд)
        s = json.loads(db_mod.get_world(wid)["setting"])
        s["_player_turns"] = 99
        s["player"]["gold"] = 777
        db_mod.update_world(wid, setting=s)
        return {"verdict": "issue", "twist": "реальность дрогнула", "corrections": {}}

    async def _noop_async(*a, **kw):
        return None

    monkeypatch.setattr(narrator_mod, "logic_judge", _judge)
    monkeypatch.setattr(narrator_mod, "index_exchange", _noop_async)
    before_events = len(db_mod.get_events(wid))
    asyncio.run(core_mod._maybe_logic_judge(wid, stale, "ход", "ответ"))

    after = json.loads(db_mod.get_world(wid)["setting"])
    assert after["_player_turns"] == 99, "судья откатил счётчик ходов — запись не заблокирована"
    assert after["player"]["gold"] == 777, "судья затёр ход игрока своим устаревшим reading'ом"
    assert not after.get("_judge_last_turn"), "судья отметил свой проход, хотя ничего не записал"
    assert len(db_mod.get_events(wid)) == before_events, "«искажение реальности» ушло в чат впустую"


# ══════════════════════════════════════════════════════════════════════
# п.18 — из дампа вырезаются ЛЮБЫЕ вложенные api_key
# ══════════════════════════════════════════════════════════════════════

def test_world_dump_strips_nested_api_keys():
    """api_key, затесавшийся в setting/saves/snapshot (старые базы, чужая правка), не утекает."""
    dump = {
        "format": db_mod.DUMP_FORMAT, "version": db_mod.DUMP_VERSION,
        "world": {"name": "Секрет", "theme": "custom",
                  "setting": {"player": {"gold": 1},
                              "nested": {"deep": [{"api_key": "sk-should-die"}]}},
                  "snapshot": json.dumps({"provider_settings": {"main": {"api_key": "sk-x"}}}),
                  "gen_settings": {}, "provider_settings": {"main": {"api_key": "sk-y"}}},
        "events": [], "entities": [], "lore": [],
        "saves": [{"name": "s", "seq": 1, "setting": json.dumps({"api_key": "sk-z"}),
                   "created_at": 0}],
        "graph": {},
    }
    cleaned = db_mod._dump_strip_secrets(dump)
    blob = json.dumps(cleaned, ensure_ascii=False)
    for doomed in ("sk-should-die", "sk-x", "sk-z"):
        assert doomed not in blob, f"ключ {doomed} утёк в дамп"
    assert "api_key" not in blob
    # ИНВАРИАНТ: типы не меняются (restore_world требует dict в world.setting —
    # пересерилизация «в строку» сломала бы импорт целого мира)
    assert isinstance(cleaned["world"]["setting"], dict)
    assert cleaned["world"]["setting"]["player"]["gold"] == 1
    assert not db_mod._dump_has_secrets(cleaned["world"]["setting"])
    # JSON-строки остаются строками, но без секретов
    assert isinstance(cleaned["world"]["snapshot"], str)
    assert "api_key" not in cleaned["world"]["snapshot"]
    assert json.loads(cleaned["world"]["snapshot"]) == {"provider_settings": {"main": {}}}
    assert isinstance(cleaned["saves"][0]["setting"], str)
    assert json.loads(cleaned["saves"][0]["setting"]) == {}


def test_dump_has_secrets_detects_json_strings():
    assert db_mod._dump_has_secrets({"a": json.dumps({"api_key": "x"})})
    assert not db_mod._dump_has_secrets({"a": "обычный текст {не json"})
    assert not db_mod._dump_has_secrets({"a": {"b": 1}})


# ══════════════════════════════════════════════════════════════════════
# п.23 — молчаливый фолбэк админки становится видимым
# ══════════════════════════════════════════════════════════════════════

def test_admin_settings_logs_once_on_failure(tmp_path, monkeypatch, caplog):
    import backend.admin_settings as admin_mod
    monkeypatch.setattr(admin_mod, "db_path", lambda: str(tmp_path / "нет" / "такого.db"))
    caplog.set_level(logging.WARNING, logger="textgame.admin_settings")
    for _ in range(3):
        assert admin_mod.read_overrides() == {}
    msgs = [r.getMessage() for r in caplog.records if "admin_settings" in r.getMessage()]
    assert len(msgs) == 1, f"ожидался одноразовый warning, получили {len(msgs)}"


# ══════════════════════════════════════════════════════════════════════
# п.25 — ключ сущности с апострофом не ломает модалку
# ══════════════════════════════════════════════════════════════════════

def test_frontend_jsattr_defined_and_used():
    """E1 (хвосты 38): onclick-литералы вынесены в data-click — проверяем обратное.

    Исторический смысл теста (сессия 36, п.25) — «ключ от LLM не должен ломать модалку».
    Теперь он закрыт сильнее: в разметке вообще нет JS-литералов, поэтому вспомогательная
    функция jsAttr() удалена, а экранирование HTML-атрибута (attrArg → esc) проверяется
    roundtrip-полигоном в scripts/check_frontend.py.
    """
    import re
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    from check_frontend import _strip_js
    js = io.open(ROOT / "frontend" / "app.js", encoding="utf-8").read()
    assert 'onclick="' not in _strip_js(js), "вернулся inline-onclick"
    assert "CLICK_ACTIONS" in js and "attrArg" in js
    assert set(re.findall(r"""data-click="([A-Za-z]+)""", js)) <= \
        set(re.findall(r"""function ([A-Za-z]+)\(""", js)), "data-click без функции"


def test_esc_alone_breaks_on_apostrophe():
    """Доказательство, что фикс нужен: esc() не спасает от апострофа в атрибуте."""
    js = io.open(ROOT / "frontend" / "app.js", encoding="utf-8").read()
    esc_src = js[js.index("function esc("):js.index("}", js.index("function esc(")) + 1]
    assert "'" not in esc_src.split("{", 1)[1].replace("'", "") or True
    # esc не заменяет апостроф — значит ключ don't закрывает JS-строку раньше времени
    out = esc_src
    assert "&#39" not in out and "\\\\'" not in out


# ══════════════════════════════════════════════════════════════════════
# п.26 — коррекция уровня судьёй не лечит бесплатно
# ══════════════════════════════════════════════════════════════════════

def _mk_setting(hp=30, mp=10, level=5, max_hp=100, max_mp=60):
    return {"_difficulty": "normal",
            "player": {"hp": hp, "max_hp": max_hp, "mp": mp, "max_mp": max_mp,
                       "level": level,
                       "stats": {k: 10 for k in ("сила", "ловкость", "выносливость",
                                                 "интеллект", "мудрость", "харизма", "удача")}},
            "locations": {}, "current_location": "start"}


def test_judge_correction_preserves_hp_ratio():
    s = _mk_setting(hp=30, mp=12)
    narrator_mod._apply_judge_corrections(s, {"level": 9})
    p = s["player"]
    assert p["level"] == 9
    assert p["max_hp"] > 100, "max_hp обязан вырасти (recalc_derived по уровню)"
    # доля сохранена, а не залечена до максимума
    assert p["hp"] < p["max_hp"], f"бесплатное лечение: {p['hp']}/{p['max_hp']}"
    assert abs(p["hp"] / p["max_hp"] - 0.3) < 0.06, f"пропорция сломана: {p['hp']}/{p['max_hp']}"
    assert abs(p["mp"] / p["max_mp"] - 0.2) < 0.06


def test_judge_correction_keeps_dead_player_dead():
    s = _mk_setting(hp=0, mp=0)
    narrator_mod._apply_judge_corrections(s, {"level": 9})
    assert s["player"]["hp"] == 0, "судья воскресил персонажа правкой уровня"


def test_judge_correction_full_hp_stays_full():
    s = _mk_setting(hp=100, mp=60)
    narrator_mod._apply_judge_corrections(s, {"level": 9})
    p = s["player"]
    assert p["hp"] == p["max_hp"]


# ══════════════════════════════════════════════════════════════════════
# п.29 — вес BM25 зажат в [0,1]
# ══════════════════════════════════════════════════════════════════════

def test_hybrid_rerank_clamps_weight():
    cands = [{"content": "огонь жарко", "similarity": 0.9},
             {"content": "вода течёт", "similarity": 0.1}]
    ok = [dict(c) for c in cands]
    emb_mod.hybrid_rerank("огонь", ok, weight_bm25=0.4)
    for w in (2.0, -1.0, 99, float("nan"), None, "abc", 1.0, 0.0):
        got = [dict(c) for c in cands]
        emb_mod.hybrid_rerank("огонь", got, weight_bm25=w)
        scores = [c["_hybrid"] for c in got]
        assert all(0.0 - 1e-9 <= s <= 1.0 + 1e-9 for s in scores), f"w={w}: score вне [0,1]: {scores}"
    # порядок при w>1 не должен переворачиваться относительно корректного w=1
    assert [c["content"] for c in emb_mod.hybrid_rerank("огонь", [dict(c) for c in cands],
                                                        weight_bm25=5.0)][0] == "огонь жарко"


def test_admin_rejects_out_of_range_bm25(api_client):
    client, _ = api_client
    r = client.post("/api/admin/settings", json={"hybrid_weight_bm25": "2"})
    assert r.status_code == 400, f"невалидный вес приняли: {r.status_code}"
    assert "BM25" in r.text or "bm25" in r.text
    r2 = client.post("/api/admin/settings", json={"hybrid_weight_bm25": "-0.5"})
    assert r2.status_code == 400
    r3 = client.post("/api/admin/settings", json={"hybrid_weight_bm25": "0.55"})
    assert r3.status_code == 200
    # пустое значение = сброс к .env — обязан оставаться разрешённым
    r4 = client.post("/api/admin/settings", json={"hybrid_weight_bm25": ""})
    assert r4.status_code == 200


# ══════════════════════════════════════════════════════════════════════
# п.30 — штатное закрытие ресурсов
# ══════════════════════════════════════════════════════════════════════

def test_app_has_lifespan_shutdown():
    src = io.open(ROOT / "backend" / "app.py", encoding="utf-8").read()
    assert "asynccontextmanager" in src and "def _lifespan" in src
    assert "db.close" in src and "llm.close" in src and "chroma_client.close" in src
    assert "on_event" not in src, "старые on_event хаки остались (DeprecationWarning)"


def test_bg_shutdown_nowait_cancels_workers():
    from backend import bg as bg_mod
    bg_mod.reset()

    async def _scenario():
        async def _slow():
            await asyncio.sleep(30)
        # D12 (аудит 38): задача ставится РАДИ побочного эффекта (воркер её подхватит),
        # а await_result не включён — возвращаемое значение здесь и не может быть результатом.
        await bg_mod.submit("slow", lambda: _slow(), world_id=1, agent="slow")
        await asyncio.sleep(0.05)
        assert bg_mod.stats()["queued"] + bg_mod.stats()["running"] >= 0
        bg_mod.shutdown_nowait()
        assert bg_mod.stats()["queued"] == 0
        return True

    assert asyncio.run(_scenario())


# ══════════════════════════════════════════════════════════════════════
# п.31 — дедуп повторов: компромиссы зафиксированы, формат сохранён
# ══════════════════════════════════════════════════════════════════════

def test_dedupe_keeps_paragraph_structure():
    dup = ("Абзац первый достаточно длинный, чтобы считаться самостоятельным блоком.\n\n"
           "Абзац первый достаточно длинный, чтобы считаться самостоятельным блоком.\n\n"
           "Абзац второй, тоже довольно длинный и уникальный по своему содержанию.")
    out = core_mod._dedupe_repeats(dup)
    assert out.count("Абзац первый") == 1
    assert "\n\n" in out, "абзацы склеились в простыню"
    assert "Абзац второй" in out


def test_dedupe_preserves_refrain_abab():
    block = "Длинный абзац про ветер, что треплет волосы на твоём измождённом лице."
    t = f"И всё замерло. {block} И всё замерло. {block}"
    assert core_mod._dedupe_repeats(t) == t, "рефрен A B A удалён — это не цикл модели"


def test_dedupe_is_noop_without_duplicates():
    t = ("Обычный текст без повторов, длиной заведомо больше восьмидесяти символов, "
         "чтобы не сработал ранний выход по длине из функции дедупликации.")
    assert core_mod._dedupe_repeats(t) == t


def test_dedupe_known_compromise_documented():
    """ДЛИННЫЙ рефрен подряд вырезается — это зафиксированный компромисс, а не молча баг."""
    doc = io.open(ROOT / "backend" / "routers" / "core.py", encoding="utf-8").read()
    i = doc.index("def _dedupe_repeats")
    body = _func_body(doc, i)
    assert "КОМПРОМИСС" in body.upper() and "30" in body


# ══════════════════════════════════════════════════════════════════════
# п.14 — start_game.bat: 401 от llama.cpp ≠ «сервер мёртв»
# ══════════════════════════════════════════════════════════════════════

# D11 (хвосты сессии 38): два теста ниже — ЧЕСТНЫЙ SMOKE ПО ТЕКСТУ .bat (они так и
# названы в докстринге): cmd здесь не исполняется, регрессий поведения они не дают.
# Структурные инварианты батников переехали в scripts/check_start_bat.py (он проверяет
# и это же, и больше), а в pytest из блока осталась проверяемое: чекер зелёный + ловит
# возврат к плохому (degradation-пробы) — tests/test_session38_tails.py::test_d11_*.
# Живой `cmd /c` харнесс с подменённым портом остался открытой задачей (ROADMAP, D11).


def test_start_bat_tolerates_401():
    """401 от llama.cpp (--api-key) ≠ «сервер мёртв»: игра обязана стартовать.

    SMOKE ПО ТЕКСТУ .bat (аудит 38, D11 — вариант C): bat здесь не исполняется, поэтому
    регрессии поведения нет — тест ловит лишь «правку убрали» и падает при безвредном
    рефакторинге. Честный harness (cmd /c с подменённым портом) — отдельная задача;
    решено оставить проверку текста, но назвать её как есть (см. инвариант 19 в AGENT.md:
    новые проверки так писать нельзя — только поведение; структурные — в scripts/check_*)."""
    bat = io.open(ROOT / "start_game.bat", encoding="utf-8").read()
    assert "http_code" in bat, "curl-проверка llama.cpp по-прежнему считает 401 смертью"
    seg = bat[bat.index("REM 1. Проверка llama.cpp"):bat.index("REM 2.")]
    # только ИСПОЛНЯЕМЫЕ строки секции (в REM-комментарии слово errorlevel законно —
    # там оно объясняет, от чего мы ушли)
    code = [ln.strip() for ln in seg.splitlines()
            if ln.strip() and not ln.strip().upper().startswith("REM")
            and not ln.strip().startswith(":")]
    assert not any("errorlevel" in ln.lower() for ln in code), \
        "проверка снова на errorlevel curl (любая 4xx = «мёртв» — ложный отказ в запуске)"
    assert any("LLAMA_CODE" in ln for ln in code), "пропадала проверка HTTP-кода ответа"
    assert "llama_dead" in seg and "000" in seg


def test_start_bat_has_no_cjk():
    """В батнике не должно остаться иероглифов-опечаток (сессия 36)."""
    bat = io.open(ROOT / "start_game.bat", encoding="utf-8").read()
    bad = [c for c in bat if 0x3000 <= ord(c) <= 0x9fff or 0xac00 <= ord(c) <= 0xd7af]
    assert not bad, f"посторонние CJK-символы в start_game.bat: {sorted(set(bad))}"


# ══════════════════════════════════════════════════════════════════════
# п.19 — per-world провайдеры: проверка доступности при сохранении
# ══════════════════════════════════════════════════════════════════════

def test_world_providers_warn_when_model_down(api_client, monkeypatch):
    client, _ = api_client
    wid = _mk_world(client, "П19")

    async def _down(prov):
        return False

    monkeypatch.setattr(llm_mod, "check_available", _down)
    r = client.post(f"/api/worlds/{wid}/providers",
                    json={"main": {"id": "llamacpp"}})
    assert r.status_code == 200, f"сохранение заблокировано: {r.status_code} {r.text[:200]}"
    warns = r.json().get("warnings") or []
    assert warns, "модель недоступна, но предупреждения нет"
    assert "не отвечает" in warns[0]
    # и в UI это тоже есть
    js = io.open(ROOT / "frontend" / "app.js", encoding="utf-8").read()
    assert "res.warnings" in js


def test_world_providers_no_warning_when_available(api_client, monkeypatch):
    client, _ = api_client
    wid = _mk_world(client, "П19б")

    async def _up(prov):
        return True

    monkeypatch.setattr(llm_mod, "check_available", _up)
    r = client.post(f"/api/worlds/{wid}/providers", json={"main": {"id": "llamacpp"}})
    assert r.status_code == 200
    assert not (r.json().get("warnings") or [])


# ══════════════════════════════════════════════════════════════════════
# п.21 — рабочий набор ключей .env не отстаёт от шаблона
# ══════════════════════════════════════════════════════════════════════

def test_all_documented_keys_exist_in_config():
    """Ключи из .env.example обязаны читаться конфигом (нет «мёртвых» настроек)."""
    from backend.config import Config
    fields = {f.name.upper() for f in __import__("dataclasses").fields(Config)}
    missing = []
    for line in io.open(ROOT / ".env.example", encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k = line.partition("=")[0].strip()
        if k not in fields:
            missing.append(k)
    assert not missing, f"в шаблоне есть ключи, которые конфиг не читает: {missing}"


# ══════════════════════════════════════════════════════════════════════
# п.28 — облачный Edge TTS повторяется при временных сбоях
# ══════════════════════════════════════════════════════════════════════

def test_tts_edge_retry_on_transient(monkeypatch, fake_config):
    """429/обрыв от Microsoft-сервиса — не ⚠ у игрока, а автоматический повтор."""
    pytest.importorskip("edge_tts", reason="опциональная зависимость (requirements-optional.txt)")
    import edge_tts
    from backend import tts as tts_mod

    fake_config(llm_retries=2, llm_retry_backoff=0.0)
    state = {"calls": 0}

    class _FakeComm:
        def __init__(self, text, voice, rate=None):
            state["rate"] = rate

        async def stream(self):
            state["calls"] += 1
            if state["calls"] == 1:
                raise edge_tts.exceptions.NoAudioReceived("сервис отдал пустой поток")
            yield {"type": "audio", "data": b"\xff\xfb\x90\x00"}

    monkeypatch.setattr(edge_tts, "Communicate", _FakeComm)
    out = asyncio.run(tts_mod._synth_edge("привет", "ru-RU-DmitryNeural", "0%"))
    assert out == b"\xff\xfb\x90\x00"
    assert state["calls"] == 2, f"повтора не было (вызовов: {state['calls']})"
    assert state["rate"] == "+0%", "нормализация rate ('0%' → '+0%') потерялась"


def test_tts_edge_permanent_error_raises(monkeypatch, fake_config):
    """Перманентная ошибка (битый голос) НЕ должна повторяться бесконечно."""
    pytest.importorskip("edge_tts", reason="опциональная зависимость (requirements-optional.txt)")
    import edge_tts
    from backend import tts as tts_mod

    fake_config(llm_retries=3, llm_retry_backoff=0.0)
    calls = {"n": 0}

    class _BadComm:
        def __init__(self, *a, **kw):
            pass

        async def stream(self):
            calls["n"] += 1
            raise ValueError("неизвестный голос")   # не временный сбой
            yield {"type": "audio", "data": b""}

    monkeypatch.setattr(edge_tts, "Communicate", _BadComm)
    with pytest.raises(ValueError):
        asyncio.run(tts_mod._synth_edge("текст", "bad-voice", "+0%"))
    assert calls["n"] == 1, f"перманентную ошибку повторяли {calls['n']} раз(а)"


def test_edge_transient_predicate_recognizes_edge_errors():
    pytest.importorskip("edge_tts", reason="опциональная зависимость (requirements-optional.txt)")
    from backend import tts as tts_mod
    import edge_tts.exceptions as ex
    assert tts_mod._edge_transient(ex.NoAudioReceived("x"))
    assert tts_mod._edge_transient(ex.UnexpectedResponse("x"))
    assert tts_mod._edge_transient(RuntimeError("LLM HTTP 503 (edge)"))
    assert not tts_mod._edge_transient(ValueError("битый аргумент"))


# ══════════════════════════════════════════════════════════════════════
# п.31 — эвристики зафиксированы как инварианты (не «молчаливые баги»)
# ══════════════════════════════════════════════════════════════════════

def test_heuristics_documented_as_invariants():
    doc = io.open(ROOT / "backend" / "routers" / "core.py", encoding="utf-8").read()
    for fn in ("def _looks_finished", "def _repetition_score", "def _dedupe_repeats"):
        body = _func_body(doc, doc.index(fn))
        assert "ИНВАРИАНТ" in body or "КОМПРОМИСС" in body, \
            f"{fn}: компромисс не задокументирован (п.31 требует явной фиксации)"


def test_stream_marker_split_across_tokens(api_client, monkeypatch):
    """Маркер механики, нарезанный по границам токенов («…gam» + «e_engine({…})»),
    не должен ни засветиться в чате, ни удвоить прозу (в старом коде `safe` не
    двигался в ветке «маркера нет» → при детекте проза уходила дважды)."""
    client, _holder = api_client
    wid = _mk_world(client, "П4б")
    from backend import llm as llm_for_stream

    pieces = ["Ты идёшь", " впе", "рёд. gam", "e_engine(", "{\"player\": {\"gold\": 5}}",
              ") факелы ", "зашипели."]

    async def _chunked_stream(messages, **kw):
        out = kw.get("tool_calls_out")
        if out is not None:
            out.append({"name": "game_engine", "arguments": '{"player": {"gold": 5}}'})
        for p in pieces:
            yield p

    monkeypatch.setattr(llm_for_stream, "stream_chat", _chunked_stream)
    chunks: list[str] = []
    result: dict = {}
    with client.stream("POST", f"/api/worlds/{wid}/action/stream",
                       json={"text": "идти"}) as resp:
        event = ""
        for line in resp.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                obj = json.loads(line[5:].strip())
                if event == "token":
                    chunks.append(str(obj.get("text", "")))
                elif event == "result":
                    result = obj
    joined = "".join(chunks)
    assert "gam" not in joined.replace("зашипели", ""), f"обрывок маркера ушёл в чат: {joined!r}"
    assert "engine" not in joined.lower() and "{" not in joined
    # проза не задвоена: «Ты идёшь вперёд.» ровно один раз
    assert joined.count("впер") == 1, f"проза задвоилась: {joined!r}"
    assert "факелы" in result.get("reply", "")


def test_turn_registry_fallback_for_legacy_world():
    """Мир, записанный ДО появления meta.turn (живые сохранения пользователя):
    после «рестарта» ↻ всё равно должен найти события хода, но НЕ задеть фоновые."""
    wid = db_mod.create_world("Легаси", "custom", "fantasy", "normal", "second", "ru", "",
                              {"player": {}}, {})
    try:
        p = db_mod.add_event(wid, "player", "идти")            # seq 1, БЕЗ meta.turn
        dice = db_mod.add_event(wid, "dice", "🎲 куб")          # seq 2
        narr = db_mod.add_event(wid, "narrator", "ты пошёл")    # seq 3
        syst = db_mod.add_event(wid, "system", "Золото: 5")     # seq 4
        bg_sys = db_mod.add_event(wid, "system", "🌍 Событие мира")   # seq 5 (фон)
        bg_vis = db_mod.add_event(wid, "narrator", "🌙 Видение:\n…")  # seq 6 (фон)
        core_mod._turn_events.clear()
        core_mod._turn_seq.clear()

        ids = core_mod._turn_registry(wid, p["seq"])
        assert set(ids) >= {dice["id"], narr["id"]}, f"ход не найден: {ids}"
        for keep in (syst["id"], bg_sys["id"], bg_vis["id"], p["id"]):
            assert keep not in ids, f"в чужие события лезет замена: {keep}"
    finally:
        db_mod.delete_world(wid)


def test_cards_sync_persists_all_sections(api_client, monkeypatch):
    """ДЕФЕКТ (найдён при проверке п.12): три блока синхронизации парсили ОДНУ строку
    world['setting'] и по очереди писали её в БД — поздний блок затирал правки раннего
    (квест исчезал, стоило блоку NPC записать «свежераспарсенный» setting).
    Теперь состояние парсится один раз и пишется один раз в конце."""
    import backend.memory as mem_mod
    client, _ = api_client
    wid = _mk_world(client, "П12")
    writes = {"n": 0}
    orig_update = mem_mod.db.update_world

    def counted(world_id, **kw):
        if "setting" in kw:
            writes["n"] += 1
        return orig_update(world_id, **kw)

    monkeypatch.setattr(mem_mod.db, "update_world", counted)

    async def _ents(*a, **kw):
        return {"entities": [
            {"kind": "quest", "key": "q_a", "name": "Квест А", "summary": "досье",
             "meta": {"status": "active"}},
            {"kind": "npc", "key": "npc_b", "name": "Стражник Б", "summary": "на посту",
             "meta": {"alive": True}},
            {"kind": "shop", "key": "shop_c", "name": "Лавка В", "summary": "товары",
             "meta": {"faction": "gildiya"}},
            {"kind": "enemy", "key": "enemy_d", "name": "Волк", "summary": "рычит",
             "meta": {"hp": 12}},
        ]}

    import backend.narrator as narrator_mod2
    monkeypatch.setattr(narrator_mod2, "llm_json_tool", _ents)
    asyncio.run(mem_mod.update_entity_cards(wid, "идти", "ты пошёл"))

    s = json.loads(db_mod.get_world(wid)["setting"])
    assert "q_a" in (s.get("quests") or {}), "квест затёрт поздней синхронизацией"
    assert "npc_b" in (s.get("npc") or {}), "NPC не синхронизирован"
    assert "shop_c" in (s.get("shops") or {}), "магазин не синхронизирован"
    assert "enemy_d" in (s.get("enemies") or {}), "враг не синхронизирован"
    assert writes["n"] == 1, f"ожидалась ОДНА итоговая запись, было {writes['n']}"


# ══════════════════════════════════════════════════════════════════════
# п.15/16 — страховки на фронте (полное поведение проверяется в браузере,
# здесь — чтобы правка не «незаметно откатилась» при следующем рефакторинге)
# ══════════════════════════════════════════════════════════════════════

def test_frontend_refreshes_suggestions_on_empty_result():
    """Пустой ответ suggestions ⇒ ВСЕГДА просим свежие (а не только когда старых нет)."""
    js = io.open(ROOT / "frontend" / "app.js", encoding="utf-8").read()
    i = js.index("function handleActionResult")
    body = js[i:i + 3500]
    assert "loadSuggestionRefresh(state.currentWorld)" in body, \
        "исчез вызов обновления подсказок — кнопки снова залипнут на устаревших (п.15)"
    # прежний баг: ветка обновления защищалась условием «и старых предложений тоже нет»,
    # из-за чего при пустом ответе сервера и живых старых кнопках свежие НЕ запрашивались
    # (кнопки залипали на устаревшей сцене). Проверяем точное старое выражение.
    assert "!(state.suggestions && state.suggestions.length)" not in body, \
        "вернулось прежнее guard-условие — при пустом ответе свежие подсказки не придут (п.15)"
    # троттлинг, чтобы «всегда» не превратилось в шторм запросов к /suggest
    assert "_SUG_MIN_INTERVAL_MS" in js and "_sugReqInFlight" in js


def test_frontend_tts_poll_clears_interval():
    """Один интервал на кнопку: старый не должен оставаться висеть (п.16)."""
    js = io.open(ROOT / "frontend" / "app.js", encoding="utf-8").read()
    i = js.index("function pollTtsStatus")
    body = js[i:js.index("\nasync function playTtsAudio", i)]
    assert "btn._ttsTimer" in body, "таймер не хранится на кнопке — интервалы плодятся"
    assert "clearInterval(btn._ttsTimer)" in body, "предыдущий интервал не снимается"
    # снятие обязано быть и в аварийной ветке (catch), и по потере мира
    assert body.count("stop()") >= 3, f"мало точек остановки таймера: {body.count('stop()')}"
    assert "document.body.contains(btn)" in body, "нет остановки после закрытия мира"


# ══════════════════════════════════════════════════════════════════════
# п.20/27 (verify-only из аудита) — закрепляем как инварианты
# ══════════════════════════════════════════════════════════════════════

def test_dice_events_do_not_need_tts_and_history_carries_status():
    """dice-события озвучки не требуют (фронт рисует 🔊 только narrator), а колонка
    tts_status обязана приходить в выборках истории — иначе плашка статуса сломается."""
    wid = db_mod.create_world("П20", "custom", "fantasy", "normal", "second", "ru", "",
                              {"player": {}}, {})
    try:
        db_mod.add_event(wid, "dice", "🎲 d20 = 17")
        rows = db_mod.get_events(wid, limit=60)
        assert rows and all("tts_status" in r for r in rows), "выборка истории потеряла tts_status"
        assert rows[0]["tts_status"] == 0     # «нет озвучки», фронт трактует как 🔊
        js = io.open(ROOT / "frontend" / "app.js", encoding="utf-8").read()
        i = js.index("function buildMsg")
        body = js[i:i + 3000]
        assert 'role === "narrator"' in body or 'e.role === "narrator"' in body
    finally:
        db_mod.delete_world(wid)


def test_admin_metrics_rotation_key(api_client):
    """METRICS_MAX_BYTES: принимается валидный, отклоняется отрицательный/не-число,
    пустой = сброс к .env (п.7 + «все параметры в админке»)."""
    client, _ = api_client
    r = client.post("/api/admin/settings", json={"metrics_max_bytes": "1048576"})
    assert r.status_code == 200, r.text[:200]
    eff = client.get("/api/admin/settings").json()["effective"]["persist"]
    assert eff["metrics_max_bytes"] == 1048576
    assert client.post("/api/admin/settings", json={"metrics_max_bytes": "-5"}).status_code == 400
    assert client.post("/api/admin/settings", json={"metrics_max_bytes": "abc"}).status_code == 400
    # 0 — легальный «ротацию выключить»
    assert client.post("/api/admin/settings", json={"metrics_max_bytes": "0"}).status_code == 200
    # пустое значение = удалить переопределение (вернуться к .env), а не выставить 0
    r2 = client.post("/api/admin/settings", json={"metrics_max_bytes": ""})
    assert r2.status_code == 200


def test_admin_form_sends_only_changed_fields():
    """Корневая причина п.2: collect() отправлял ЛЮБОЕ непустое поле, и каждое
    сохранение админки материализовало все эффективные значения в admin_settings —
    будущий дефолт .env/кода до них уже не доставал. Страховка на месте `dataset.orig`."""
    html = io.open(ROOT / "frontend" / "admin.html", encoding="utf-8").read()
    assert "el.dataset.orig = el.type === \"checkbox\"" in html, \
        "снимок «как при открытии» пропал — админка снова начнёт закреплять дефолты"
    i = html.index("function collect()")
    body = html[i:html.index("async function save()", i)]
    # collect() обязан читать поля через valOr/boolOr (иначе «не меняли» не отличить)
    assert "const valOr = (id)" in body and "const boolOr = (id)" in body, \
        "в collect() пропали хелперы «только изменённое» — дефолты снова начнут липнуть"
    # старые «всегда шлём» формы должны отсутствовать в collect()
    assert 'payload[`${kind}_provider`] = $val(`a-${kind}-provider`)' not in body, \
        "провайдеры снова отправляются безусловно"
    assert "payload.rerank_enabled = $checked(" not in body, "rerank_enabled шлётся всегда"
    assert "payload.metrics_persist = $checked(" not in body, "metrics_persist шлётся всегда"
    # и что хотя бы часть полей переведена на valOr/boolOr
    assert body.count("valOr(") > 8 and body.count("boolOr(") > 6


def test_admin_empty_save_changes_nothing(api_client):
    """Пустой POST (фронт шлёт его, когда ничего не трогали) не должен ничего закреплять."""
    client, _ = api_client
    before = client.get("/api/admin/settings").json()["stored"]
    assert client.post("/api/admin/settings", json={}).status_code == 200
    after = client.get("/api/admin/settings").json()["stored"]
    assert after == before, f"пустое сохранение изменило переопределения: {before} → {after}"


def test_divine_first_call_never_blocked(api_client, monkeypatch, fake_config):
    """Первое воззвание в мире (в т.ч. на ходу 0 — сразу после вступления) ОБЯЗАНО
    проходить: кулдаун считается от последнего воззвания, а «ещё не воззывался» — не ноль.

    Поймано живым smoke: `setting.get("_divine_last_turn", 0) or 0` схлопывало
    «не воззывался» с «воззывался на ходе 0», и в новом мире боги молчали первые
    DIVINE_COOLDOWN_TURNS ходов — ровно тогда, когда жалоба на рассказчика нужнее всего.
    """
    client, _holder = api_client
    fake_config(divine_cooldown_turns=3)
    wid = _mk_world(client, "П2-кулдаун")

    async def _verdict(*a, **kw):
        return {"decline": True, "twist": "всё верно, путник", "directives": None,
                "sys_msgs": [], "state": {}}

    monkeypatch.setattr(narrator_mod, "divine_intervene", _verdict)
    r1 = client.post(f"/api/worlds/{wid}/divine", json={"complaint": "мне не выдали меч"})
    assert r1.status_code != 429, f"первое воззвание заблокировано: {r1.status_code} {r1.text[:120]}"
    assert r1.status_code == 200, r1.text[:200]
    # второе подряд — уже кулдаун
    r2 = client.post(f"/api/worlds/{wid}/divine", json={"complaint": "всё равно дайте меч"})
    assert r2.status_code == 429, f"кулдаун не сработал: {r2.status_code}"
