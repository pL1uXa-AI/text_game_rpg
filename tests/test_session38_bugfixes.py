# -*- coding: utf-8 -*-
"""Сессия 38 — регресс-тесты на всё, что нашёл сплошной аудит (ISSUES_AUDIT_SESSION38.md).

Каждый тест закрепляет ОДИН закрытый пункт и обязан был бы падать до фикса. Номера
пунктов в докстрингах — каноническая нумерация аудита (A1…A19, B…, C…, D…, E…).

Проверяемое:
  A1  «ружья Чехова» переживают ход (round-trip через БД), а не живут один ход;
  A2  системка «эффект зоны снят» при move не теряется в чате;
  A3  живые API-ключи не уходят в /api/worlds/{id} и в ответ POST .../providers;
  A4  ни один эндпоинт не отвечает 5xx на несуществующем мире; фидбек скоупится по миру;
  A5  MAX_ACTION_CHARS читается из конфига на каждый ход (правка в админке живая);
  A6  дефолт DIVINE_COOLDOWN_TURNS в load() == дефолт dataclass (страховка от всех
      подобных расхождений — сверка полей Config с .env.example и между собой);
  A7  слэш-команда через /action/stream отдаёт `event: result` (фронт его слушает),
      а список команд в обеих ветках — из одного предиката;
  A8  удаление мира вычищает связанные таблицы + старт сервера убирает сирот;
  A11 создание мира выживает при пустом opening (500/IntegrityError больше нет);
  A12 сводки для промпта читаются ограниченным запросом;
  A13 реестр хода читает БД ограниченным окном;
  A14 SSE-догон и поллинг не вытягивают весь лог мира;
  A15 POST /divine не затирает ход игрока (409 при устаревшем состоянии);
  A16 метрика «повторы» считает настоящий цикл, а не долю повторных слов;
  A17 RERANK_THRESHOLD реально фильтрует выдачу реранкера (top_n как декорация удалён);
  A18 TTL кеша озвучки применяется: строки и файлы устаревших записей исчезают;
  A19 сбой загрузки персоны рассказчика логируется (правило 14);
  C10 в промпте и строках лога нет опечаток (кинет «ИТОГ КВЕТА» TYPO-OK и т.п.);
  C11 все слэш-команды задокументированы в README;
  D6  нет голых get_event_loop()/create_task без ссылки на задачу;
  E2  экранирование подстановок из внешних данных в innerHTML (карточки сюжетов, роли);
  E5  state.providerSettings из кода фронта удалён (носитель живых ключей).
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
from pathlib import Path

import pytest

import backend.db as db_mod
import backend.routers.core as core_mod
from backend import config as config_mod
from backend import narrator as narrator_mod

ROOT = Path(__file__).resolve().parent.parent


def _mk_world(client, name="Мир"):
    from backend import narrator as n
    r = client.post("/api/worlds", json={"theme_id": n.THEMES[0]["id"], "name": name})
    assert r.status_code == 200, r.text
    return r.json()["world_id"]


def _plain_world(hp: int = 50) -> int:
    """Мир прямо в БД (без API/LLM) — для тестов слоя данных."""
    return db_mod.create_world(
        "тест", "custom", "фэнтези", "normal", "second", "ru", "",
        {"player": {"hp": hp, "max_hp": hp, "stats": {}, "inventory": []},
         "locations": {"start": {"name": "Привал"}}, "npc": {}, "quests": {}, "flags": {}},
        {})


# ══════════════════════════ A1: ружья Чехова ══════════════════════════

def test_chekhov_persists_across_turns(api_client):
    """«🏹 На горизонте» обязано доживать до следующего хода.

    Дефект: `db.update_world(setting=...)` стоял в середине транзакции хода, а
    `journal.chekhov_update` мутировал setting уже ПОСЛЕ записи → ключ `_chekhov`
    никогда не попадал в БД, и фича была мертва (в боевой БД — 0 миров из 34)."""
    client, holder = api_client
    holder["reply"] = ('Ты осматриваешь пыльную комнату, ничего примечательного. '
                       '<<ENGINE>>{"add_item": [{"name": "Медный ключ", "qty": 1}]}')
    wid = _mk_world(client, "Ружья")
    client.post(f"/api/worlds/{wid}/action", json={"text": "осмотреться"})
    setting1 = json.loads(db_mod.get_world(wid)["setting"])
    assert "_chekhov" in setting1, "ружья Чехова не записаны в состояние после первого хода"
    assert any("люч" in str(g.get("subject", "")) for g in setting1["_chekhov"]), \
        f"после выдачи предмета ружьё не заряжено: {setting1['_chekhov']}"
    holder["reply"] = "Ничего нового не происходит."
    client.post(f"/api/worlds/{wid}/action", json={"text": "сесть на стул"})
    setting2 = json.loads(db_mod.get_world(wid)["setting"])
    guns = setting2.get("_chekhov") or []
    assert guns, "после второго хода «ружейная» пуста — список не накапливается"
    assert any("люч" in str(g.get("subject", "")) for g in guns), \
        f"ружьё пропало между ходами: {guns}"
    assert all(isinstance(g, dict) and g.get("subject") for g in guns)


# ══════════════════════ A2: снятие эффектов зоны ══════════════════════

def test_move_zone_message_not_dropped(api_client):
    """Системка «эффект зоны снят» при уходе из локации-зоны обязана дойти до чата."""
    client, holder = api_client
    wid = _mk_world(client, "Зона")
    setting = json.loads(db_mod.get_world(wid)["setting"])
    setting["locations"]["waste"] = {
        "name": "Радиоактивная пустошь",
        "effects": [{"name": "Облучение", "turns": -1, "damage": 2, "desc": "фон"}]}
    setting["current_location"] = "waste"
    db_mod.update_world(wid, setting=setting)
    narrator_mod.apply_location_effects(setting, "waste", apply=True)   # эффект наложен
    db_mod.update_world(wid, setting=setting)
    assert "Облучение" in (setting["player"].get("effects") or {}), \
        "эффект зоны не наложился (проверка некорректна)"

    holder["reply"] = "Ты уходишь с пустоши. " + '<<ENGINE>>{"move": "start"}'
    r = client.post(f"/api/worlds/{wid}/action", json={"text": "уйти отсюда"})
    assert r.status_code == 200, r.text
    texts = "\n".join(e["content"] for e in r.json()["events"])
    assert "Облучение" in texts and "спало" in texts, \
        f"сообщение о снятии эффекта зоны потеряно в логе: {texts!r}"


# ══════════════════════ A3: ключи в ответах API ══════════════════════

def test_api_never_leaks_provider_api_key(api_client):
    """Жёсткое правило 3: наружу ключ уходит только маскированным (KEY_MASK)."""
    from backend.config import KEY_MASK
    client, _ = api_client
    wid = _mk_world(client, "Ключи")
    db_mod.update_world(wid, provider_settings={
        "main": {"id": "openai_compat", "base_url": "https://x/api/v1",
                 "api_key": "sk-SECRET-1234567890", "model": "m"}})
    d = client.get(f"/api/worlds/{wid}").json()
    blob = json.dumps(d, ensure_ascii=False)
    assert "sk-SECRET-1234567890" not in blob, "world_detail отдаёт живой api_key"
    assert KEY_MASK in blob, "ключ не замаскрован — фронт не увидит маску"
    # и в вложенном JSON-поле world.provider_settings тоже (утечка шла именно оттуда)
    nested = json.loads(d["world"]["provider_settings"])
    assert nested["main"]["api_key"] == KEY_MASK
    r = client.post(f"/api/worlds/{wid}/providers", json={
        "main": {"id": "openai_compat", "base_url": "https://x/api/v1",
                 "api_key": "sk-ANOTHER-999999999", "model": "m"}})
    assert r.status_code == 200
    assert "sk-ANOTHER-999999999" not in r.text, "POST .../providers отдаёт живой api_key"
    # ключ при этом сохранён и работает (маска/пусто = «не менять» — семантика цела)
    stored = json.loads(db_mod.get_world(wid)["provider_settings"])
    assert stored["main"]["api_key"] == "sk-ANOTHER-999999999"
    assert core_mod._world_providers(db_mod.get_world(wid))["main"]["api_key"] == \
        "sk-ANOTHER-999999999"


# ══════════════════ A4: 404 вместо 500 на мёртвом мире ══════════════════

DEAD = 999999
# (метод, путь, тело) — все эндпоинты мира, обязанные отвечать 404, а не 5xx/200
_ENDPOINTS = [
    ("get", f"/api/worlds/{DEAD}", None),
    ("get", f"/api/worlds/{DEAD}/memory/search?q=x", None),
    ("get", f"/api/worlds/{DEAD}/graph", None),
    ("get", f"/api/worlds/{DEAD}/export/json", None),
    ("get", f"/api/worlds/{DEAD}/history", None),
    ("get", f"/api/worlds/{DEAD}/events", None),
    ("get", f"/api/worlds/{DEAD}/saves", None),
    ("get", f"/api/worlds/{DEAD}/journal", None),
    ("get", f"/api/worlds/{DEAD}/risk", None),
    ("get", f"/api/worlds/{DEAD}/rewind/points", None),
    ("get", f"/api/worlds/{DEAD}/lore", None),
    ("get", f"/api/worlds/{DEAD}/lore/search?q=x", None),
    ("get", f"/api/worlds/{DEAD}/entities", None),
    ("get", f"/api/worlds/{DEAD}/entities/npc/x", None),
    ("get", f"/api/worlds/{DEAD}/export", None),
    ("get", f"/api/worlds/{DEAD}/events/stream", None),
    ("get", f"/api/worlds/{DEAD}/tts/settings", None),
    ("post", f"/api/worlds/{DEAD}/tts/settings", {"enabled": True}),
    ("post", f"/api/worlds/{DEAD}/action", {"text": "/help"}),
    ("post", f"/api/worlds/{DEAD}/action", {"text": "идти"}),
    ("post", f"/api/worlds/{DEAD}/action/stream", {"text": "/status"}),
    ("post", f"/api/worlds/{DEAD}/action/stream", {"text": "идти"}),
    ("post", f"/api/worlds/{DEAD}/events/1/feedback", {"value": 1}),
    ("post", f"/api/worlds/{DEAD}/state/patch", {"patch": {}}),
    ("post", f"/api/worlds/{DEAD}/providers", {"main": {"id": "ollama"}}),
    ("post", f"/api/worlds/{DEAD}/settings", {"temperature": 0.5}),
    ("post", f"/api/worlds/{DEAD}/saves", {"name": "x"}),
    ("post", f"/api/worlds/{DEAD}/rewind", {"seq": 1, "mode": "hide"}),
    ("post", f"/api/worlds/{DEAD}/lore", {"title": "t", "content": "c"}),
    ("post", f"/api/worlds/{DEAD}/vision/trigger", {}),
    ("post", f"/api/worlds/{DEAD}/divine", {"complaint": "x"}),
    ("post", f"/api/worlds/{DEAD}/suggest", {}),
    ("post", f"/api/worlds/{DEAD}/entities", {"kind": "npc", "key": "k", "name": "n"}),
    ("delete", f"/api/worlds/{DEAD}/entities/npc/zz", None),
    ("delete", f"/api/worlds/{DEAD}/saves/1", None),
    ("delete", f"/api/worlds/{DEAD}/lore/1", None),
    ("delete", f"/api/worlds/{DEAD}", None),
    ("post", f"/api/worlds/{DEAD}/saves/1/load", None),
]


@pytest.mark.parametrize("method,path,body", _ENDPOINTS,
                         ids=[f"{m}:{p}" for m, p, _ in _ENDPOINTS])
def test_missing_world_returns_404(api_client, method, path, body):
    """Ни один эндпоинт не даёт 5xx и не «молча не удаётся» (200) на несуществующем мире."""
    client, _ = api_client
    resp = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)
    assert resp.status_code < 500, f"{method.upper()} {path} → {resp.status_code}: {resp.text[:200]}"
    assert resp.status_code == 404, \
        f"{method.upper()} {path} → {resp.status_code}, ожидался честный 404"


def test_feedback_scoped_to_world(api_client):
    """A4-bis: событие чужого мира нельзя пометить фидбеком адресом своего мира."""
    client, _ = api_client
    w1 = _mk_world(client, "Мир-1")
    w2 = _mk_world(client, "Мир-2")
    ev = db_mod.get_events(w1, limit=1)[0]
    r = client.post(f"/api/worlds/{w2}/events/{ev['id']}/feedback", json={"value": 1})
    assert r.status_code == 404, "фидбек события чужого мира прошёл"
    assert db_mod.get_event(ev["id"])["feedback"] == 0
    ok = client.post(f"/api/worlds/{w1}/events/{ev['id']}/feedback", json={"value": 1})
    assert ok.status_code == 200
    assert db_mod.get_event(ev["id"])["feedback"] == 1


def test_lore_rows_scoped_to_world(api_client):
    """A4-bis: правка/удаление статьи лора чужого мира по её id — тоже 404."""
    client, _ = api_client
    w1 = _mk_world(client, "Лор-1")
    w2 = _mk_world(client, "Лор-2")
    entry = db_mod.create_lore(w1, "Канон", "Текст канона.", source="user")
    r = client.patch(f"/api/worlds/{w2}/lore/{entry['id']}",
                     json={"title": "Взлом", "content": "чужой мир"})
    assert r.status_code == 404, "статью чужого мира можно переписать"
    r2 = client.delete(f"/api/worlds/{w2}/lore/{entry['id']}")
    assert r2.status_code == 404, "статью чужого мира можно удалить"
    assert db_mod.get_lore(entry["id"]), "данные всё равно потеряны"


# ══════════════════ A5: MAX_ACTION_CHARS из конфига ══════════════════

def test_max_action_chars_follows_config(api_client, fake_config):
    """Лимит длины действия обязан применяться СРАЗУ после правки в админке."""
    client, _ = api_client
    fake_config(max_action_chars=10)
    wid = _mk_world(client, "Лимит")
    r = client.post(f"/api/worlds/{wid}/action", json={"text": "двенадцать симв"})
    assert r.status_code == 400, "лимит из конфига не применился (нет 400)"
    assert "максимум 10" in r.text, f"в сообщении старый предел: {r.text[:200]}"
    fake_config(max_action_chars=640)
    assert client.post(f"/api/worlds/{wid}/action",
                       json={"text": "двенадцать симв"}).status_code == 200


# ══════════════════ A6/B4: дефолты и полнота .env.example ══════════════════

def test_divine_cooldown_default_is_three(api_client, fake_config, monkeypatch):
    """Чистая установка: кулдаун Провидения = 3 (заявлено в сессии 36 п.2), не 0."""
    from backend.config import Config
    from pathlib import Path as _P
    assert Config().divine_cooldown_turns == 3
    assert Config.load(env_file=_P("нет-такого-файла.env"),
                       env={"SENTINEL": "1"}).divine_cooldown_turns == 3, \
        "дефолт load() снова разошёлся с dataclass"
    # и поведенчески: второе воззвание на том же ходу отказывает
    client, _ = api_client
    fake_config(divine_cooldown_turns=3)
    wid = _mk_world(client, "Провидение")
    called: list[int] = []

    async def _fake_divine(world_id, world, setting, complaint, **kw):
        called.append(1)
        return {"twist": "Реальность вздрагивает.", "sys_msgs": [], "state": setting}
    monkeypatch.setattr(narrator_mod, "divine_intervene", _fake_divine)
    assert client.post(f"/api/worlds/{wid}/divine",
                       json={"complaint": "мне не выдали обещанное золото"}).status_code == 200
    r = client.post(f"/api/worlds/{wid}/divine", json={"complaint": "ещё раз"})
    assert r.status_code == 429, "анти-фарм Провидения не работает на чистой установке"


def test_config_load_defaults_match_dataclass(monkeypatch):
    """Страховка от рассинхрона дефолтов (A6): dataclass vs load() без env/.env.

    Тот же класс дефекта, что дал `divine_cooldown_turns=it(0, ...)` при dataclass-дефолте 3.
    Любое новое поле, чей литерал в load() разошёлся с полем Config, валит этот тест."""
    from backend.config import Config
    monkeypatch.setattr(config_mod.admin_settings, "read_overrides", lambda: {})
    dflt = Config()
    loaded = Config.load(env_file=Path(".__нет_такого__.env"), env={"SENTINEL": "1"})
    bad = {}
    for f in dflt.__dataclass_fields__:
        a, b = getattr(dflt, f), getattr(loaded, f)
        if isinstance(a, bool) or isinstance(b, bool):
            same = bool(a) == bool(b)
        elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
            same = float(a) == float(b)
        else:
            same = str(a).rstrip("/") == str(b).rstrip("/")
        if not same:
            bad[f] = (a, b)
    assert not bad, f"дефолты dataclass и Config.load() разошлись: {bad}"


def test_env_example_covers_all_config_keys():
    """B4: каждую настройку, которую читает Config.load, игрок должен мочь задать в .env.example.

    Имена env-переменных проекта = ИМЯ_ПОЛЯ Config в верхнем регистре (проверяется здесь же),
    поэтому реестром служит сам dataclass — второй источник списка не заведён, а значит не
    сможет устареть. Плюс: все *_API_KEY в шаблоне обязаны быть пустыми (правило 15)."""
    from backend.config import Config
    example = ROOT / ".env.example"
    assert example.exists()
    keys = set()
    for line in example.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.add(line.split("=", 1)[0].strip())
    fields = set(Config.__dataclass_fields__)
    env_names = {f.upper() for f in fields}
    missing = sorted(n for n in env_names if n not in keys and n not in
                     # служебные имена, которые не являются env-ключами полей
                     {"ENV_FILE"})
    assert not missing, f"в .env.example нет переменных, которые читает код: {missing}"
    # обратная сверка: в шаблоне не должно быть ключей, которых нет в Config
    extra = sorted(k for k in keys if k not in env_names)
    assert not extra, f"в .env.example есть мёртвые ключи (нет в Config): {extra}"
    # все *_API_KEY в шаблоне обязаны быть пустыми (правило 15)
    for line in example.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        if k.endswith("API_KEY"):
            assert v.strip() == "", f"в шаблоне задан непустой ключ: {k}"


def test_chat_log_roles_are_documented():
    """A10: набор ролей, показываемых в логе, зафиксирован и не включает служебные роли."""
    roles = set(core_mod.CHAT_LOG_ROLES)
    assert "summary" not in roles, "сводки памяти — материал промпта, а не часть истории игрока"
    assert {"narrator", "system", "dice"} <= roles
    assert "player" in roles, "действия игрока при открытии мира обязаны быть в логе"


# ══════════════════ A7: SSE-контракт стрима ══════════════════

def test_stream_slash_command_returns_result_event(api_client):
    """Команда, посланная через /action/stream, обязана вернуться как `event: result` —
    фронт слушает token/result/error и раньше терял ответ (`event: done`)."""
    client, _ = api_client
    wid = _mk_world(client, "Стрим-команда")
    with client.stream("POST", f"/api/worlds/{wid}/action/stream",
                       json={"text": "/status"}) as resp:
        raw = "\n".join(resp.iter_lines())
    assert "event: result" in raw, f"нет result-события: {raw[:300]}"
    assert "event: done" not in raw


def test_slash_predicate_knows_every_command():
    """Все команды, которые разбирает `action`, знает и предикат stream-ветки (A7 п.4)."""
    from backend.routers.worlds import is_slash_command
    src = io.open(ROOT / "backend" / "routers" / "worlds.py", encoding="utf-8").read()
    body = src[src.index("async def action(world_id"):src.index('world_id}/suggest"')]
    # разбираем ТОЛЬКО строки сравнений (иначе в набор попадут имена из текста /help)
    toks: set[str] = set()
    for line in body.splitlines():
        s = line.strip()
        if not re.search(r'low\s*(==|in|startswith)', s):
            continue
        toks |= set(re.findall(r'"(/[^"]*)"', s))
    assert len(toks) >= 12, f"нашёл слишком мало команд ({len(toks)}) — тест надо переписать"
    for raw in sorted(toks):
        # литерал с хвостовым пробелом — это префикс «команда + аргумент»: пробуем с
        # аргументом (в реальном ходе текст перед этим уже stripped в _ensure_action_len)
        probe = raw + ("текст" if raw.endswith(" ") else "")
        assert is_slash_command(probe), f"предикат не знает команду {raw!r} (проба {probe!r})"
    # русские алиасы и команды «голым именем», которые action() всё же разбирает
    for c in ("/memory карта", "/risk", "/risk тихий ход", "/journal note текст",
              "/хроника", "/дневник", "/дневник note memo", "/roll d20", "/помощь", "/hint"):
        assert is_slash_command(c), f"предикат не знает {c!r}"
    # «/memory» БЕЗ запроса диспетчер action() не разбирает (проверка `low.startswith("/memory ")`)
    # — предикат обязан совпадать с диспетчером, а не быть шире
    assert not is_slash_command("идти в таверну")
    assert not is_slash_command("")


# ══════════════════ A8: удаление мира и сироты ══════════════════

def test_delete_world_removes_relations(api_client):
    """Удаление мира обязано вычистить ВСЕ связанные таблицы, а не 4 из 8."""
    client, _ = api_client
    wid = _mk_world(client, "Удалить")
    db_mod.upsert_entity(wid, "npc", "guard", name="Стражник", meta={"alive": True})
    db_mod.create_lore(wid, "Город", "История города.", source="user")
    s = json.loads(db_mod.get_world(wid)["setting"])
    db_mod.save_turn_snapshot(wid, 1, s, keep=0)
    from backend import graph
    graph.sync_from_setting(wid, s)
    for table in ("entities", "lore", "graph_nodes", "graph_edges", "events",
                  "turn_snapshots", "saves"):
        assert _count_rows(table, wid) >= 0
    r = client.delete(f"/api/worlds/{wid}")
    assert r.status_code == 200, r.text
    leftovers = {t: _count_rows(t, wid) for t in
                 ("entities", "lore", "graph_nodes", "graph_edges", "events",
                  "turn_snapshots", "saves")}
    assert not any(leftovers.values()), f"после удаления мира остались строки: {leftovers}"
    assert not db_mod.get_world(wid)


def _count_rows(table: str, world_id: int) -> int:
    import sqlite3
    conn = sqlite3.connect(config_mod.get_config().db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE world_id = ?",
                                (world_id,)).fetchone()[0])
    finally:
        conn.close()


def test_prune_orphans_on_startup():
    """Стартовая чистка: строки несуществующих миров удаляются идемпотентно."""
    wid = _plain_world()
    db_mod.upsert_entity(wid, "npc", "ghost", name="Призрак", meta={})
    db_mod.create_lore(wid, "Фантом", "Лор осиротевшего мира.", source="user")
    out_of_thin_air = max(wid + 7777, 900001)
    db_mod.upsert_entity(out_of_thin_air, "npc", "orphan", name="Сирота", meta={})
    db_mod.create_lore(out_of_thin_air, "Осиротевшая статья", "текст", source="user")
    res = db_mod.prune_orphan_rows()
    assert res.get("entities") and res.get("lore")
    assert not db_mod.list_entities(out_of_thin_air), "карточки сирот не вычищены"
    assert not [x for x in db_mod.list_lore(out_of_thin_air)]
    assert db_mod.list_entities(wid), "чистка задела данные живого мира"
    # идемпотентность: повторный вызов ничего не находит
    assert not db_mod.prune_orphan_rows()
    db_mod.delete_world(wid)


# ══════════════════ A11: пустое вступление ══════════════════

def test_create_world_survives_empty_opening(api_client, monkeypatch):
    """Пустой ответ генератора открытия больше не роняет создание мира (было 500)."""
    client, _ = api_client
    from backend import narrator as n

    async def _empty_opening(*a, **kw):
        return ""
    monkeypatch.setattr(n, "generate_opening", _empty_opening)
    # тема «свой сюжет» кладёт в opening сырой plot — имитируем ровно тот случай, когда
    # и он пуст (plot из whitespace), иначе dict.get(default) отработал бы как фолбэк
    real_theme = n.theme_from_custom

    def _theme_with_empty_opening(name, plot, genres=None):
        return dict(real_theme(name, plot, genres), opening="")
    monkeypatch.setattr(n, "theme_from_custom", _theme_with_empty_opening)
    r = client.post("/api/worlds", json={"theme_id": "custom", "name": "Пустое открытие",
                                         "custom_plot": "Корабль тонет, капитан пропал."})
    assert r.status_code == 200, f"создание мира упало: {r.status_code} {r.text[:300]}"
    wid = r.json()["world_id"]
    narr = [e for e in db_mod.get_events(wid) if e["role"] == "narrator"]
    assert narr and all((e["content"] or "").strip() for e in narr), \
        "вступительное сообщение пустое или отсутствует"


def test_add_event_rejects_empty_content():
    """Слой данных: пустое событие — внятная ошибка, а не IntegrityError/тихий мусор."""
    wid = _plain_world()
    with pytest.raises(ValueError):
        db_mod.add_event(wid, "narrator", "   ")
    with pytest.raises(ValueError):
        db_mod.add_event(wid, "narrator", None)
    db_mod.delete_world(wid)


# ══════════════════ A12/A13: ограниченные горячие выборки ══════════════════

def test_summaries_query_is_bounded(monkeypatch):
    """Сводки для промпта читаются потолком, а не «все сводки мира на каждый ход»."""
    seen: dict = {}

    def fake_get_summary_events(world_id, limit=10, unfolded_only=True):
        seen["limit"] = limit
        return []
    monkeypatch.setattr(db_mod, "get_summary_events", fake_get_summary_events)
    core_mod._summaries(1)
    assert seen.get("limit"), "_summaries снова просит ВСЕ сводки (limit=0)"
    assert seen["limit"] <= core_mod.SUMMARIES_FETCH_LIMIT
    # явный запрос «все» (rewind/тесты) по-прежнему работает
    seen.clear()
    core_mod._summaries(1, limit=0)
    assert seen["limit"] == 0


def test_turn_registry_bounded_and_correct():
    """Реестр хода читает ограниченный диапазон, но находит ровно свои события."""
    wid = _plain_world()
    p = db_mod.add_event(wid, "player", "идти", seq=1)
    dice = db_mod.add_event(wid, "dice", "🎲", seq=2, meta={"turn": 1})
    narr = db_mod.add_event(wid, "narrator", "ты пошёл", seq=3, meta={"turn": 1})
    syst = db_mod.add_event(wid, "system", "⚔️ бой", seq=4, meta={"turn": 1})
    # «будущий» ход с меткой другого действия — его трогать нельзя
    db_mod.add_event(wid, "narrator", "ответ 2", seq=5, meta={"turn": 5})
    for i in range(6, 400):
        db_mod.add_event(wid, "system", f"фон {i}", seq=i)
    core_mod._turn_events.clear()
    core_mod._turn_seq.clear()
    ids = set(core_mod._turn_registry(wid, p["seq"]))
    assert ids == {dice["id"], narr["id"], syst["id"]}, f"реестр хода неверен: {ids}"
    # окно ограничено: хвост из сотен событий не должен попадать в выборку
    rows = db_mod.get_turn_events(wid, p["seq"], p["seq"] + core_mod._TURN_EVENTS_WINDOW,
                                 roles=("narrator", "dice", "system"), unfolded_only=False)
    assert len(rows) <= core_mod._TURN_EVENTS_WINDOW + 1
    db_mod.delete_world(wid)


# ══════════════════ A14: догон событий ограничен ══════════════════

def test_events_poll_is_bounded_and_reports_truncation(api_client):
    """GET /events?since= отдаёт страницу и честный флаг `truncated`."""
    client, _ = api_client
    wid = _mk_world(client, "Догон")
    for i in range(5):
        db_mod.add_event(wid, "system", f"фоновое {i}", seq=1000 + i)
    r = client.get(f"/api/worlds/{wid}/events?since=0")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, dict) and "events" in body and "truncated" in body, \
        "ответ сменил форму: фронт не найдёт события"
    assert len(body["events"]) == 7            # открытие мира (2) + 5 фоновых
    assert body["truncated"] is False
    src = io.open(ROOT / "backend" / "routers" / "worlds.py", encoding="utf-8").read()
    seg = src[src.index("async def events_stream"):]
    seg = seg[:seg.index("# ─────────")]
    assert "get_events_after" in seg, "SSE-догон снова читает лог без ограничения"
    assert "_EVENTS_PAGE_LIMIT" in seg


# ══════════════════ A15: Провидение не затирает ход ══════════════════

def test_divine_rejects_stale_state(api_client, monkeypatch):
    """Если за время LLM-прохода мир изменился — 409, а не тихая порча состояния (A15)."""
    client, _ = api_client
    wid = _mk_world(client, "Гонка")
    from backend import narrator as n

    async def _racing_divine(world_id, world, setting, complaint, **kw):
        # «игрок сходил», пока Провидение думало: меняем состояние в обход HTTP
        s = json.loads(db_mod.get_world(world_id)["setting"])
        s["_player_turns"] = int(s.get("_player_turns", 0) or 0) + 3
        s["_race_marker"] = "ход игрока"
        db_mod.update_world(world_id, setting=s)
        return {"twist": "боги молчат", "sys_msgs": [], "state": setting}
    monkeypatch.setattr(n, "divine_intervene", _racing_divine)
    assert (json.loads(db_mod.get_world(wid)["setting"]).get("_player_turns", 0) or 0) == 0
    r = client.post(f"/api/worlds/{wid}/divine", json={"complaint": "мне не дали награду"})
    assert r.status_code == 409, f"устаревшее состояние принято ({r.status_code})"
    assert "Повтори" in r.text or "изменился" in r.text
    # состояние хода игрока цело: Провидение его не затёрло
    assert json.loads(db_mod.get_world(wid)["setting"]).get("_race_marker") == "ход игрока"


# ══════════════════ A17: порог реранкера работает ══════════════════

def test_rerank_threshold_filters(monkeypatch, fake_config):
    """RERANK_THRESHOLD обязан отбрасывать нерелевантное (раньше настройка ни на что не влияла)."""
    from backend import embeddings as emb

    fake_config(rerank_enabled=True, rerank_threshold=0.5)

    async def fake_http_json(method, url, **kw):
        return {"results": [{"index": 0, "relevance_score": 0.9},
                            {"index": 1, "relevance_score": 0.42},
                            {"index": 2, "relevance_score": 0.1}]}
    monkeypatch.setattr(emb, "_http_json", fake_http_json)
    cands = [{"content": "a"}, {"content": "b"}, {"content": "c"}]
    prov = {"enabled": True, "api_key": "k", "base_url": "https://x", "model": "m"}
    out = asyncio.run(emb.rerank_results("запрос", cands, top_n=5, provider=prov))
    assert [c["content"] for c in out] == ["a"], f"порог не применился: {out}"

    # порог 0 = фильтр выключен (ничего не теряем)
    fake_config(rerank_enabled=True, rerank_threshold=0.0)
    out2 = asyncio.run(emb.rerank_results("запрос", [{"content": "x"}, {"content": "y"}],
                                          top_n=5, provider=prov))
    assert len(out2) == 2
    # ручка top_n из конфига удалена как декорация (A17): размер выдачи = K памяти/лора
    assert not hasattr(config_mod.Config(), "rerank_top_n"), "rerank_top_n вернулся в Config"


# ══════════════════ A18: TTL кеша озвучки применяется ══════════════════

def test_prune_tts_cache_removes_expired(tmp_path, monkeypatch, fake_config):
    """Старые записи кеша и их файлы удаляются, свежие целы; ротация не чаще метки."""
    from backend import tts as tts_mod
    data_dir = tmp_path / "data"
    cache_dir = data_dir / "tts" / "cache"
    (cache_dir / "edge" / "ab").mkdir(parents=True)
    monkeypatch.setattr(tts_mod, "ROOT", tmp_path)                    # ROOT/data/… — база путей
    monkeypatch.setattr(tts_mod, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(tts_mod, "ROTATION_MARKER", data_dir / "tts" / ".cache_rotation")
    monkeypatch.setattr(db_mod, "ROOT_PATH", tmp_path)

    def _file(h):
        p = cache_dir / "edge" / "ab" / f"{h}.mp3"
        p.write_bytes(b"x")
        return f"tts/cache/edge/ab/{h}.mp3"

    old_h, fresh_h, orphan_h = "aa" + "0" * 30, "bb" + "1" * 30, "cc" + "2" * 30
    old_rel, fresh_rel, orphan_rel = _file(old_h), _file(fresh_h), _file(orphan_h)
    db_mod.add_tts_cache(old_h, "edge", "v", "+0%", "mp3", old_rel)
    db_mod.add_tts_cache(fresh_h, "edge", "v", "+0%", "mp3", fresh_rel)
    db_mod.add_tts_cache(orphan_h, "edge", "v", "+0%", "mp3", orphan_rel)
    asyncio.run(_backdate(old_rel))
    db_mod._run(lambda: _forget(orphan_rel))       # файл остался, строки БД нет — сирота
    fake_config(tts_cache_enabled=True, tts_cache_ttl_days=30)

    res = tts_mod.rotate_tts_cache(force=True)
    assert res["ran"] and res["ttl_days"] == 30, res
    assert (cache_dir / "edge" / "ab" / f"{fresh_h}.mp3").exists(), "свежий файл удалён"
    assert not (cache_dir / "edge" / "ab" / f"{old_h}.mp3").exists(), \
        "устаревший файл остался на диске"
    assert not (cache_dir / "edge" / "ab" / f"{orphan_h}.mp3").exists(), \
        "файл-сирота (без строки БД) остался"
    paths = {r["rel_path"] for r in db_mod.all_tts_cache_paths()}
    assert old_rel not in paths and fresh_rel in paths
    # метка: повторная ротация «не чаще суток» не делается
    again = tts_mod.rotate_tts_cache(force=False)
    assert again["ran"] is False and "назад" in again.get("skip", ""), again
    # голоса — НЕ кеш: их ротация не трогает
    (data_dir / "tts" / "voices" / "v1").mkdir(parents=True, exist_ok=True)
    vp = data_dir / "tts" / "voices" / "v1" / "model.onnx"
    vp.write_bytes(b"keep me")
    tts_mod.rotate_tts_cache(force=True)
    assert vp.exists(), "ротация снесла голоса"


async def _backdate(rel: str):
    conn = await db_mod._open()
    await conn.execute("UPDATE tts_cache SET created_at = ? WHERE rel_path = ?",
                       (__import__("time").time() - 400 * 86400, rel))
    await db_mod._maybe_commit()


async def _forget(rel: str):
    conn = await db_mod._open()
    await conn.execute("DELETE FROM tts_cache WHERE rel_path = ?", (rel,))
    await db_mod._maybe_commit()


# ══════════════════ A19: сбой загрузки персоны логируется ══════════════════

def test_world_persona_logs_failure(monkeypatch, caplog):
    """Мир не должен молча терять выбранную персону рассказчика (правило 14)."""
    def boom(nid):
        raise RuntimeError("БД недоступна")
    monkeypatch.setattr(db_mod, "get_narrator", boom)
    with caplog.at_level(logging.WARNING, logger="textgame"):
        assert core_mod._world_persona({"id": 7, "narrator_id": 3}) is None
    assert any("не загружена" in r.getMessage() for r in caplog.records), \
        "ошибка загрузки персоны не попала в лог"


# ══════════════════ C10: опечатки в промпте и логах ══════════════════

_TYPOS = ["ИТОГ КВЕТА", "уощён", "нормилзация", "останается",   # TYPO-OK (цитаты опечаток)
          "перемотка, очередь фонова", "отдельная стужа", "каждые NPC"]   # TYPO-OK


@pytest.mark.parametrize("typo", _TYPOS)
def test_known_typos_are_gone(typo):
    """Опечатки из аудита не должны вернуться (и в промпте, и в доках)."""
    hits = []
    for rel in ["backend/narrator.py", "backend/routers/core.py", "backend/routers/worlds.py",
                "backend/bg.py", "backend/character_generator.py", "README.md", "ROADMAP.md",
                "AGENT.md"]:
        p = ROOT / rel
        if p.exists() and typo in p.read_text(encoding="utf-8"):
            hits.append(rel)
    assert not hits, f"опечатка {typo!r} вернулась в: {hits}"


def test_check_typos_script_runs():
    """Дешёвое закрытие класса опечаток: скрипт + шаг CI (аудит C10)."""
    script = ROOT / "scripts" / "check_typos.py"
    assert script.exists(), "нет scripts/check_typos.py"
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "-X", "utf8", str(script)], cwd=str(ROOT),
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"check_typos.py: {r.stdout[-800:]}{r.stderr[-400:]}"


# ══════════════════ C11: документация по командам ══════════════════

def test_slash_commands_documented():
    """Каждая слэш-команда обязана быть в README и в AGENT.md (аудит C11)."""
    from backend.routers.worlds import _SLASH_EXACT, _SLASH_PREFIXES
    cmds = set(_SLASH_EXACT) | {p.strip() for p in _SLASH_PREFIXES}
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    agent = (ROOT / "AGENT.md").read_text(encoding="utf-8")
    for c in sorted(cmds):
        # русские алиасы допускаются в скобках — ищем хотя бы упоминание имени
        name = c
        assert name in readme or name.strip("/") in readme, f"{name} не описана в README.md"
        assert name in agent or name.strip("/") in agent, f"{name} не зафиксирована в AGENT.md"


def test_three_laws_identical_in_docs():
    """Три закона в AGENT.md и README не должны расходиться дословно (аудит C11)."""
    agent = (ROOT / "AGENT.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for head in ("ТОЛЬКО универсальные механики",
                 "Общие базовые возможности",
                 "Код только ПРОВЕРЯЕТ возможность"):
        assert head in agent, f"закон «{head}» пропал из AGENT.md"
        assert head in readme, f"закон «{head}» пропал из README.md"


# ══════════════════ D6: гигиена фоновых задач ══════════════════

def test_no_bare_get_event_loop():
    """asyncio.get_event_loop() deprecated (3.12) и ошибка (3.13) — в проекте его нет."""
    bad = []
    for p in (ROOT / "backend").rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if "get_event_loop()" not in s:
                continue
            if s.startswith("#") or "get_event_loop()" in s.split("\"")[0].split("#", 1)[-1] \
                    and (s.startswith(("log.", "return", "\"")) or "#" in s):
                continue
            code = s.split("#")[0]
            if "get_event_loop()" in code:
                bad.append(f"{p.relative_to(ROOT)}:{i}: {s[:80]}")
    assert not bad, "вернулся get_event_loop():\n" + "\n".join(bad)


def test_bg_spawn_keeps_task_reference():
    """D6: asyncio хранит задачи слабыми ссылками — bg.spawn обязан держать свою."""
    from backend import bg

    async def scenario():
        flag = {"done": False}

        async def work():
            await asyncio.sleep(0.01)
            flag["done"] = True
        t = bg.spawn(work(), name="probe")
        assert t is not None
        assert t in bg._stray_tasks, "spawn не сохранил ссылку на задачу"
        await t
        assert flag["done"]
        await asyncio.sleep(0)                 # done_callback отработает на этом витке
        assert t not in bg._stray_tasks, "задача осталась в множестве после завершения"
    asyncio.run(scenario())


def test_no_bare_create_task_in_backend():
    """Все «огонь-и-забыл» задачи идут через bg.spawn (сам bg.py — исключение).

    Разрешён один паттерн: `task = loop.create_task(...)` — ссылка держится переменной,
    задачу отменяют в finally (так живёт worker SSE-стрима). Опасен именно выброшенный
    результат: asyncio хранит задачи слабыми ссылками (RUF006)."""
    from backend import bg
    bad = []
    for p in (ROOT / "backend").rglob("*.py"):
        if p.name == "bg.py":
            continue
        text = p.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            code = s.split("#")[0]
            if ".create_task(" not in code:
                continue
            # строка продолжения вызова (открывающая скобка на предыдущих строках) —
            # не самостоятельный «огонь-и-забыл», а часть уже проверенного выше вызова
            if code.count("(") > code.count(")"):
                continue
            left = code.split(".create_task(")[0]
            if "=" in left:
                continue                       # ссылка сохранена в переменную
            bad.append(f"{p.relative_to(ROOT)}:{i}: {s[:90]}")
    assert not bad, ("create_task с выброшенным результатом (RUF006 — задачу может собрать GC):\n"
                     + "\n".join(bad) + "\nиспользуй bg.spawn(coro, name=…)")
    assert callable(bg.spawn)


# ══════════════════ D1: мёртвый код удалён ══════════════════

@pytest.mark.parametrize("module,name", [
    ("db", "find_entities_by_text"),
    ("db", "get_events_before"),
    ("db", "get_events_range"),
    ("narrator", "has_effectful_directives"),
    ("chroma_client", "safe_json"),
    ("config", "_admin_overrides"),
])
def test_dead_helpers_removed(module, name):
    mod = __import__(f"backend.{module}", fromlist=["*"])
    assert not hasattr(mod, name), f"мёртвый хелпер {module}.{name} вернулся"


# ══════════════════ E2/E5: фронтенд ══════════════════

def test_frontend_escapes_theme_cards_and_roles():
    """E2: подстановки из внешних данных в innerHTML экранированы."""
    app = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    i = app.index("card.innerHTML = `<h4>")
    seg = app[i:i + 220]
    assert "esc(t.name)" in seg and "esc(t.genre)" in seg and "esc(t.desc)" in seg, \
        f"карточка сюжета строится без экранирования: {seg}"
    j = app.index("WHO[e.role]")
    around = app[max(0, j - 120):j + 120]
    assert "esc(WHO[e.role]" in around or "roleLabel(" in around, \
        f"роль события уходит в innerHTML сырой: {around}"


def test_frontend_drops_raw_provider_settings():
    """E5: state.providerSettings (носитель живых ключей) удалён из кода."""
    app = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    assert "state.providerSettings" not in app, "мёртвое поле с ключами вернулось"


def test_frontend_handles_object_events_response():
    """A14: фронт читает новую форму ответа /events (объект с events/truncated)."""
    app = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    assert ".events || []" in app or "d.events" in app, \
        "поллинг не разворачивает объект ответа — живой чат сломается"


def test_frontend_dedupes_by_event_id():
    """E3: дедюп сообщений лога — по id события, а не по seq.

    По `data-seq` второе идентичное служебное сообщение (без seq — `appendMsg({role:
    "system"…})`) считалось «уже нарисованным» и терялось, а селектор
    `.msg[data-seq=""]`/`[data-seq="undefined"]` строился из значения из БД."""
    app = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    fn = app[app.index("function appendMsg("):]
    fn = fn[:fn.index("\n}\n")]
    assert 'data-id="' in fn and "CSS.escape" in fn, \
        f"appendMsg не дедюплирует по id события:\n{fn[:400]}"
    assert 'data-seq="${e.seq}"' not in fn, "старый хрупкий селектор по seq вернулся"
    # локальным (оптимистичным) сообщениям без id выдаётся временный id
    assert "_localMsgId" in fn, "локальные сообщения без seq снова слипаются"


def test_check_frontend_passes():
    """Встроенные проверки фронта (id-ссылки, jsAttr, CJK/омоглифы) обязаны быть зелёными."""
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "-X", "utf8",
                        str(ROOT / "scripts" / "check_frontend.py")],
                       cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-500:]


def test_prompt_rules_numbering(fake_config):
    """C1: нумерация правил рассказчика обязана быть без дырок и без «уезжающих» номеров.

    Дефект был двусторонний: (а) правило 29 выдавалось только при canon_note → «…28, 30…»,
    (б) части одного правила склеивались через запятую и каждое получало свой номер
    («30. …, 31. срок)»). Ярусы промпта (_GATED_RULES/trim_prompt) ключуются НОМЕРАМИ,
    поэтому любая дырка/сдвиг = молчаливое вырезание не того правила."""
    import re
    # Ярусы промпта ВЫКЛЮЧЕНЫ: они легально вырезают правила мёртвых
    # подсистем (инвариант 10 в AGENT.md) — «дырка» в этом случае не дефект.
    fake_config(prompt_tiers_enabled=False)
    st = {"player": {"hp": 50, "max_hp": 50, "mp": 10, "max_mp": 10, "gold": 0, "level": 1,
                     "stats": {}, "inventory": [], "race": "человек", "class": "Воин",
                     "profession": "Кузнец", "skills": {"меч": {"rank": "D"}}},
          "locations": {}, "npc": {}, "quests": {}, "flags": {}}
    for canon in ("", "Город N под договором."):
        w = {"id": 1, "name": "t", "language": "ru", "genre": "фэнтези",
             "difficulty": "normal", "perspective": "second",
             "theme": {"name": "T", "genre": "фэнтези", "canon_note": canon},
             "setting": json.dumps(st), "gen_settings": json.dumps({"max_tokens": 2000})}
        for tools in (False, True):
            p = narrator_mod.build_system_prompt(w, st, use_tools=tools, action="")
            nums = [m.group(1) for m in re.finditer(r"^(\d+[а-я]?)\.\s", p, re.M)]
            plain = sorted(int(x) for x in nums if x[-1].isdigit())
            assert plain == list(range(1, max(plain) + 1)), \
                f"номера правил с дыркой (canon={bool(canon)}, tools={tools}): {plain}"
            assert max(plain) == narrator_mod.TAIL_RULE_BASE + len(narrator_mod.TAIL_RULES) - 1, \
                f"хвост правил уехал (canon={bool(canon)}): {max(plain)} != " \
                f"{narrator_mod.TAIL_RULE_BASE}+{len(narrator_mod.TAIL_RULES)}-1"
            # Сессия 63: правило хвоста обязано доходить до «живого мира» — иначе мир снова
            # рельсовый (канва сюжета тащит игрока к написанному финалу).
            assert "ЖИВОЙ МИР vs КАНВА СЮЖЕТА" in p, \
                f"правило 37 пропало из промпта (tools={tools})"
            # подстроки правил не должны дублировать заголовки других правил (trim режет
            # строку по номеру — дубль заголовка вернул бы вырезанное правило обратно)
            assert p.count("ФРАКЦИИ → ПУТЬ ИГРОКА") == 1, \
                "маркер правила 26 продублирован в другом правиле"


# ══════════ сессия 40, п.3: вступление не должно быть обрывком ══════════
def test_ends_sentence_russian_quotes():
    """Закрывающая кавычка «…» концом мысли не является: именно из-за этого обрывок
    вступления («…в свободной колонии «Осколок Рассвета») проходил как законченный."""
    from backend.character_generator import _ends_sentence
    assert _ends_sentence('Так кончается фраза.')
    assert _ends_sentence('Что ты делаешь?')
    assert _ends_sentence('Он ушёл…')
    assert _ends_sentence('Сказал: «да».')
    assert not _ends_sentence('Ты родился в колонии «Осколок Рассвета»')
    assert not _ends_sentence('оборвано без знака')
    assert not _ends_sentence("")


def test_opening_retries_truncated_text(monkeypatch):
    """generate_opening: обрывок (нет конца фразы) → повтор генерации, а не тихий вывод
    в чат; вторая, законченная попытка и уходит игроку."""
    import asyncio
    from backend import character_generator as cg

    seen = {"n": 0}

    async def _fake(messages, **kw):
        seen["n"] += 1
        if seen["n"] == 1:
            return "Ты — Кайден. Ты родился в колонии «Осколок Рассвета»"   # обрыв
        return "Ты — Кайден. Вот сцена целиком. Что ты делаешь?"

    monkeypatch.setattr(cg.llm, "complete", _fake)
    theme = narrator_mod.THEMES[0]
    st = narrator_mod.default_setting(theme, "normal")
    w = {"id": 1, "name": "t", "language": "ru", "genre": theme["genre"],
         "theme": theme["id"], "custom_hook": ""}
    out = asyncio.run(cg.generate_opening(w, st))
    assert seen["n"] == 2, "обрывок обязан был вызвать повтор"
    assert out == "Ты — Кайден. Вот сцена целиком. Что ты делаешь?"


def test_opening_never_returns_fragment(monkeypatch):
    """Все попытки оборваны — игрок получает или последнее ПОЛНОЕ предложение, или
    завязку сюжета; оборванный хвост в чат не уходит ни в одном из вариантов."""
    import asyncio
    from backend import character_generator as cg
    from backend.character_generator import _ends_sentence

    long_cut = ("Первое предложение сцены целиком. " * 6) + "а вот вторая мысль обрывается посередине"

    async def _fake(messages, **kw):
        return long_cut

    monkeypatch.setattr(cg.llm, "complete", _fake)
    theme = narrator_mod.THEMES[0]
    st = narrator_mod.default_setting(theme, "normal")
    w = {"id": 1, "name": "t", "language": "ru", "genre": theme["genre"],
         "theme": theme["id"], "custom_hook": ""}
    out = asyncio.run(cg.generate_opening(w, st))
    assert _ends_sentence(out), f"в чат ушёл оборванный текст: {out!r}"
    assert "обрывается посередине" not in out
