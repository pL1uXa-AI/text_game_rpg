# -*- coding: utf-8 -*-
"""API-тесты (FastAPI TestClient + герметичные заглушки LLM/Chroma/памяти).

Проверяют полный контур роутеров после рефакторинга app.py → роутеры:
каталог, миры, действия (обычное + SSE), слоты, настройки, фидбек, мастер,
карточки, экспорт, память, админка, статус системы.
"""
from __future__ import annotations

import json

import pytest

from backend import narrator as narrator_mod

# Тема по умолчанию — первый сюжет из plots/ (system). Раньше был хардкод THEME_ID;
# встроенных тем больше нет — они живут файлами в plots/system и plots/user.
THEME_ID = narrator_mod.THEMES[0]["id"]


# ── Каталог ─────────────────────────────────────────────────────────

def test_themes(api_client):
    client, _ = api_client
    r = client.get("/api/themes")
    assert r.status_code == 200
    themes = r.json()
    # Сюжеты-миры живут в папке plots/ (system + user); минимум 1 комплектный сюжет.
    assert len(themes) >= 1
    assert all({"id", "name", "genre", "desc"} <= t.keys() for t in themes)


def test_genres(api_client):
    client, _ = api_client
    r = client.get("/api/genres")
    assert r.status_code == 200
    assert len(r.json()) >= 20


def test_narrators_seeded(api_client):
    client, _ = api_client
    r = client.get("/api/narrators")
    assert r.status_code == 200
    names = [n["name"] for n in r.json()]
    assert len(names) >= 7, "идемпотентный сид пресетов рассказчиков"


def test_providers_options(api_client):
    client, _ = api_client
    r = client.get("/api/providers")
    body = r.json()
    assert {"main", "embedding", "rerank", "tts"} <= body.keys()
    assert "rerank_enabled_global" in body


def test_plots_crud(api_client):
    client, _ = api_client
    r = client.post("/api/plots", json={"name": "Сюжет A", "plot": "Герой просыпается в порту."})
    assert r.status_code == 200
    pid = r.json()["plot"]["id"]
    r = client.patch(f"/api/plots/{pid}", json={"name": "Сюжет B", "plot": "Другая завязка."})
    assert r.status_code == 200
    assert r.json()["plot"]["name"] == "Сюжет B"
    assert len(client.get("/api/plots").json()) >= 1
    assert client.delete(f"/api/plots/{pid}").status_code == 200
    assert client.delete(f"/api/plots/{pid}").status_code == 404


def test_narrators_crud(api_client):
    client, _ = api_client
    r = client.post("/api/narrators", json={"name": "Шёпот сна", "prompt": "Ты — сонный голос."})
    assert r.status_code == 200
    nid = r.json()["narrator"]["id"]
    r = client.patch(f"/api/narrators/{nid}", json={"name": "Шёпот сна", "prompt": "Другое описание."})
    assert r.status_code == 200
    assert client.delete(f"/api/narrators/{nid}").status_code == 200


# ── Миры ───────────────────────────────────────────────────────────

def test_world_graph_endpoint(api_client):
    """GET /api/worlds/{id}/graph возвращает узлы/рёбра/слои и путь к цели."""
    client, _ = api_client
    r = client.post("/api/worlds", json={"theme_id": THEME_ID, "name": "Граф-мир"})
    wid = r.json()["world_id"]

    payload_r = client.get(f"/api/worlds/{wid}/graph")
    assert payload_r.status_code == 200
    data = payload_r.json()
    assert "nodes" in data and "edges" in data and "layers" in data
    # Текущая локация — стартовая локация сюжета (не обязательно "start")
    assert data["current"] in {n["id"] for n in data["nodes"]}
    assert len(data["nodes"]) >= 1

    # путь к цели: существующая локация → список узлов, иначе пусто
    cur = data["current"]
    r2 = client.get(f"/api/worlds/{wid}/graph", params={"target": cur})
    path = r2.json().get("path") or []
    assert path and path[0] == cur

    # неизвестный мир → 404
    assert client.get("/api/worlds/999999/graph").status_code == 404


def test_create_and_detail_world(api_client):
    client, _ = api_client
    r = client.post("/api/worlds", json={"theme_id": THEME_ID, "name": "Тест", "difficulty": "normal"})
    assert r.status_code == 200, r.text
    wid = r.json()["world_id"]
    detail = client.get(f"/api/worlds/{wid}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["world"]["id"] == wid
    assert "setting" in body and "providers_effective" in body
    assert any(e["role"] == "narrator" for e in body["recent"]), "открытие мира сгенерировано"


def test_create_world_unknown_theme(api_client):
    client, _ = api_client
    r = client.post("/api/worlds", json={"theme_id": "no_such_theme"})
    assert r.status_code == 400


def test_create_world_custom_plot(api_client):
    client, _ = api_client
    r = client.post("/api/worlds", json={"theme_id": "custom", "name": "Свой мир",
                                         "custom_plot": "Ты просыпаешься в библиотеке."})
    assert r.status_code == 200, r.text
    assert r.json()["opening"]


def test_worlds_list(api_client):
    client, _ = api_client
    assert client.get("/api/worlds").status_code == 200


# ── Действия ───────────────────────────────────────────────────────

def test_action_applies_directives(api_client):
    client, holder = api_client
    wid = client.post("/api/worlds", json={"theme_id": THEME_ID, "name": "A"}).json()["world_id"]
    holder["reply"] = ("Ты нашёл кошель на полу.\n"
                       "<<ENGINE>>{\"player\": {\"gold\": 15}, \"flag\": {\"name\": \"кошелёк\", \"value\": true}}")
    r = client.post(f"/api/worlds/{wid}/action", json={"text": "осматриваюсь"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"]["player"]["gold"] >= 15, "директивы применились"
    assert body["state"]["flags"].get("кошелёк") is True
    assert "reply_event_id" in body
    assert any(e["role"] == "player" for e in body["events"])


def test_death_by_effect_message_once(api_client):
    """Регрессия: смерть от эффекта в тике хода не должна дублировать сообщение о гибели."""
    client, holder = api_client
    wid = client.post("/api/worlds", json={"theme_id": THEME_ID, "name": "A"}).json()["world_id"]
    holder["reply"] = "Ты чувствуешь яд во всём теле."
    # ставим игрока при смерти и накладываем смертельный эффект (тик сработает в начале хода)
    patch = {"player": {"hp": 2, "max_hp": 100,
                        "effects": {"яд": {"turns": 5, "damage": 5}}}}
    pr = client.post(f"/api/worlds/{wid}/state/patch", json={"patch": patch})
    assert pr.status_code == 200, pr.text
    r = client.post(f"/api/worlds/{wid}/action", json={"text": "делаю шаг"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["game_over"] is True, "игрок погиб от эффекта"
    deaths = [e for e in body["events"]
              if e["role"] == "system" and "погиб от эффекта" in e["content"]]
    assert len(deaths) == 1, f"сообщение о гибели не должно дублироваться: {len(deaths)}"


def test_action_help_command(api_client):
    client, _ = api_client
    wid = client.post("/api/worlds", json={"theme_id": THEME_ID, "name": "B"}).json()["world_id"]
    r = client.post(f"/api/worlds/{wid}/action", json={"text": "/help"})
    assert r.status_code == 200
    assert "Команды" in r.json()["reply"]


def test_action_slash_roll(api_client):
    client, _ = api_client
    wid = client.post("/api/worlds", json={"theme_id": THEME_ID, "name": "C"}).json()["world_id"]
    r = client.post(f"/api/worlds/{wid}/action", json={"text": "/roll d20"})
    assert r.status_code == 200
    assert "🎲" in r.json()["reply"]


def test_action_slash_info_commands(api_client):
    client, _ = api_client
    wid = client.post("/api/worlds", json={"theme_id": THEME_ID, "name": "C1"}).json()["world_id"]
    for cmd in ("/where", "/status", "/quests", "/map", "/inventory", "/story"):
        r = client.post(f"/api/worlds/{wid}/action", json={"text": cmd})
        assert r.status_code == 200, r.text
        assert r.json()["reply"], f"{cmd} вернул ответ"


def test_auto_save_after_turn(api_client):
    client, holder = api_client
    wid = client.post("/api/worlds", json={"theme_id": THEME_ID, "name": "C2"}).json()["world_id"]
    # обычный ход → создаётся авто-слот "auto"
    holder["reply"] = "Ты осмотрелся.\n<<ENGINE>>{\"player\": {\"gold\": 5}}"
    r = client.post(f"/api/worlds/{wid}/action", json={"text": "осмотреться"})
    assert r.status_code == 200, r.text
    saves = client.get(f"/api/worlds/{wid}/saves").json()
    auto = [s for s in saves if s["name"] == "auto"]
    assert len(auto) == 1, "один авто-слот после хода"
    auto_id = auto[0]["id"]
    # ещё один ход → слот перезаписывается, а не дублируется
    holder["reply"] = "Второй осмотр.\n<<ENGINE>>{\"player\": {\"gold\": 10}}"
    client.post(f"/api/worlds/{wid}/action", json={"text": "снова осмотреться"})
    saves2 = client.get(f"/api/worlds/{wid}/saves").json()
    auto2 = [s for s in saves2 if s["name"] == "auto"]
    assert len(auto2) == 1, "слот перезаписан, не дублирован"
    assert auto2[0]["id"] != auto_id, "новый id после перезаписи"
    # авто-слот загружается
    assert client.post(f"/api/worlds/{wid}/saves/{auto2[0]['id']}/load").status_code == 200


def test_action_too_long(api_client):
    client, _ = api_client
    wid = client.post("/api/worlds", json={"theme_id": THEME_ID, "name": "D"}).json()["world_id"]
    r = client.post(f"/api/worlds/{wid}/action", json={"text": "д" * 700})
    assert r.status_code == 400
    assert "слишком длинное" in r.text.lower() or "Разбей" in r.json().get("detail", "")


def test_regenerate_replaces_events(api_client):
    """Регрессия: перегенерация ↻ заменяет события прошлого хода, а не накапливает копии."""
    client, holder = api_client
    wid = client.post("/api/worlds", json={"theme_id": THEME_ID, "name": "R"}).json()["world_id"]
    holder["reply"] = "Первый ответ рассказчика."
    r1 = client.post(f"/api/worlds/{wid}/action", json={"text": "первый шаг"})
    assert r1.status_code == 200
    holder["reply"] = "Второй ответ рассказчика."
    r2 = client.post(f"/api/worlds/{wid}/action", json={"text": "второй шаг"})
    assert r2.status_code == 200
    hist = client.get(f"/api/worlds/{wid}/history").json()
    narr_before = [e for e in hist if e["role"] == "narrator"]
    # регенерируем второй (последний) ход
    holder["reply"] = "Второй ответ — перегенерирован."
    rr = client.post(f"/api/worlds/{wid}/action", json={"text": "второй шаг", "regenerate": True})
    assert rr.status_code == 200, rr.text
    body = rr.json()
    assert "replaced_events" in body and len(body["replaced_events"]) >= 1
    hist2 = client.get(f"/api/worlds/{wid}/history").json()
    narr_after = [e for e in hist2 if e["role"] == "narrator"]
    assert len(narr_after) == len(narr_before), \
        f"перегенерация должна заменять (не добавлять): {len(narr_before)} → {len(narr_after)}"


def test_action_game_over_conflict(api_client):
    client, _ = api_client
    wid = client.post("/api/worlds", json={"theme_id": THEME_ID, "name": "E"}).json()["world_id"]
    # ставим game_over через мастера
    client.post(f"/api/worlds/{wid}/state/patch", json={"patch": {"game_over": True}})
    r = client.post(f"/api/worlds/{wid}/action", json={"text": "осмотреться"})
    assert r.status_code == 409


def test_action_stream_sse(api_client):
    client, holder = api_client
    wid = client.post("/api/worlds", json={"theme_id": THEME_ID, "name": "F"}).json()["world_id"]
    holder["reply"] = "Ты слышишь шаги за спиной.\n<<ENGINE>>{\"player\": {\"hp\": -5}}"
    pairs: list[tuple[str, str]] = []
    with client.stream("POST", f"/api/worlds/{wid}/action/stream", json={"text": "иду вглубь"}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        name, data = None, ""
        for line in r.iter_lines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
                if name is not None:
                    pairs.append((name, data))
    names = [n for n, _ in pairs]
    assert "token" in names
    assert "result" in names
    result = json.loads(dict(pairs)["result"])
    assert result["state"]["player"]["hp"] == 95, "SSE-ход применил механику (100 − 5)"
    # токены собраны в итоговый ответ (стриминг жив)
    tokens = "".join(json.loads(d)["text"] for n, d in pairs if n == "token")
    assert tokens



def test_entity_cards_crud(api_client):
    client, _ = api_client
    wid = client.post("/api/worlds", json={"theme_id": THEME_ID, "name": "G"}).json()["world_id"]
    r = client.post(f"/api/worlds/{wid}/entities",
                    json={"kind": "npc", "key": "barmen", "name": "Бармен",
                          "summary": "Молчалив", "relationship": "должник"})
    assert r.status_code == 200, r.text
    r = client.get(f"/api/worlds/{wid}/entities")
    assert any(e["entity_key"] == "barmen" for e in r.json())
    r = client.patch(f"/api/worlds/{wid}/entities/npc/barmen",
                     json={"kind": "npc", "key": "barmen", "bio_add": "Рассказал о гоблинах"})
    assert r.status_code == 200
    r = client.get(f"/api/worlds/{wid}/entities/npc/barmen")
    assert "гоблин" in r.json().get("bio", "").lower()
    assert client.delete(f"/api/worlds/{wid}/entities/npc/barmen").status_code == 200


def test_entity_bad_kind(api_client):
    client, _ = api_client
    wid = client.post("/api/worlds", json={"theme_id": THEME_ID, "name": "H"}).json()["world_id"]
    r = client.post(f"/api/worlds/{wid}/entities", json={"kind": "машина", "key": "x"})
    assert r.status_code == 400


# ── Слоты, настройки, фидбек, мастер, экспорт ─────────────────────

def test_saves_roundtrip(api_client):
    client, _ = api_client
    wid = client.post("/api/worlds", json={"theme_id": THEME_ID, "name": "I"}).json()["world_id"]
    r = client.post(f"/api/worlds/{wid}/saves", json={"name": "Точка 1"})
    assert r.status_code == 200
    sid = r.json()["save"]["id"]
    assert len(client.get(f"/api/worlds/{wid}/saves").json()) == 1
    # загружаем и удаляем
    assert client.post(f"/api/worlds/{wid}/saves/{sid}/load").status_code == 200
    assert client.delete(f"/api/worlds/{wid}/saves/{sid}").status_code == 200


def test_gen_settings_update(api_client):
    client, _ = api_client
    wid = client.post("/api/worlds", json={"theme_id": THEME_ID, "name": "J"}).json()["world_id"]
    r = client.post(f"/api/worlds/{wid}/settings",
                    json={"temperature": 0.3, "max_tokens": 1500, "context_tokens": 4096})
    assert r.status_code == 200
    assert r.json()["gen_settings"]["max_tokens"] == 1500
    detail = client.get(f"/api/worlds/{wid}").json()
    assert detail["gen_settings"]["context_tokens"] == 4096


def test_feedback_clamped(api_client):
    client, _ = api_client
    wid = client.post("/api/worlds", json={"theme_id": THEME_ID, "name": "K"}).json()["world_id"]
    ev = client.get(f"/api/worlds/{wid}/history").json()[-1]
    r = client.post(f"/api/worlds/{wid}/events/{ev['id']}/feedback", json={"value": 99})
    assert r.status_code == 200


def test_state_patch_master(api_client):
    client, _ = api_client
    wid = client.post("/api/worlds", json={"theme_id": THEME_ID, "name": "L"}).json()["world_id"]
    r = client.post(f"/api/worlds/{wid}/state/patch",
                    json={"patch": {"player": {"gold": 999}, "weather": "гроза"}})
    assert r.status_code == 200
    assert r.json()["setting"]["player"]["gold"] == 999
    assert client.post(f"/api/worlds/{wid}/state/patch",
                       json={"patch": {"secret_key": 1}}).status_code == 200  # неразрешённый ключ молча игнорируется


def test_history_pagination(api_client):
    client, holder = api_client
    wid = client.post("/api/worlds", json={"theme_id": THEME_ID, "name": "P1"}).json()["world_id"]
    holder["reply"] = "Ответ."
    # несколько ходов, чтобы набрать события
    for i in range(5):
        client.post(f"/api/worlds/{wid}/action", json={"text": f"действие {i}"})
    full = client.get(f"/api/worlds/{wid}/history").json()
    assert len(full) > 3
    # страница до некой точки — строго раньше этого seq
    page = client.get(f"/api/worlds/{wid}/history", params={"before": full[3]["seq"], "limit": 2}).json()
    assert page, "нашлись более ранние события"
    assert all(e["seq"] < full[3]["seq"] for e in page), "все события страницы строго раньше before"
    assert len(page) <= 2, "limit соблюдён"
    # без before — вся история (обратная совместимость)
    assert len(client.get(f"/api/worlds/{wid}/history").json()) == len(full)


def test_export_history(api_client):
    client, _ = api_client
    wid = client.post("/api/worlds", json={"theme_id": THEME_ID, "name": "M"}).json()["world_id"]
    r = client.get(f"/api/worlds/{wid}/export")
    assert r.status_code == 200
    assert "text/plain" in r.headers.get("content-type", "")


def test_memory_search(api_client):
    client, _ = api_client
    wid = client.post("/api/worlds", json={"theme_id": THEME_ID, "name": "N"}).json()["world_id"]
    r = client.get(f"/api/worlds/{wid}/memory/search", params={"q": "гоблины"})
    assert r.status_code == 200
    assert r.json() == {"results": []}


def test_delete_world(api_client):
    client, _ = api_client
    wid = client.post("/api/worlds", json={"theme_id": THEME_ID, "name": "O"}).json()["world_id"]
    assert client.delete(f"/api/worlds/{wid}").status_code == 200
    assert client.get(f"/api/worlds/{wid}").status_code == 404


# ── Админка и статус ───────────────────────────────────────────────

def test_admin_settings_get(api_client):
    client, _ = api_client
    r = client.get("/api/admin/settings")
    assert r.status_code == 200
    body = r.json()
    assert "stored" in body and "effective" in body
    eff = body["effective"]
    assert "providers" in eff and "defaults" in eff and "tts" in eff


def test_admin_settings_save_and_reset(api_client, monkeypatch):
    from backend import db

    client, _ = api_client
    payload = {"main_provider": "openai_compat", "main_base_url": "https://routerai.ru/api/v1",
               "main_model": "upstage/solar-pro4", "max_tokens": "1500"}
    r = client.post("/api/admin/settings", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert db.get_admin_settings().get("MAIN_MODEL") == "upstage/solar-pro4"
    assert db.get_admin_settings().get("MAX_TOKENS") == "1500"
    # GET показывает маскированный ключ, если есть API_KEY
    r = client.post("/api/admin/settings", json={"main_api_key": "secret-123"})
    assert r.status_code == 200
    got = client.get("/api/admin/settings").json()
    assert got["stored"].get("MAIN_API_KEY", "").startswith("••")
    # сброс к .env: пустая строка удаляет ключ
    client.post("/api/admin/settings", json={"main_api_key": "", "main_model": "", "max_tokens": ""})
    admin = db.get_admin_settings()
    assert "MAIN_API_KEY" not in admin
    assert "MAIN_MODEL" not in admin
    assert "MAX_TOKENS" not in admin


def test_admin_settings_bad_provider(api_client):
    client, _ = api_client
    r = client.post("/api/admin/settings", json={"main_provider": "нет_такого"})
    assert r.status_code == 400


def test_system_status(api_client):
    client, _ = api_client
    r = client.get("/api/system/status")
    assert r.status_code == 200
    body = r.json()
    assert body["llm"]["up"] is True  # заглушка check_available/_get_client
    assert "chroma" in body and "providers" in body and "tts" in body


def test_index_and_admin_pages(api_client):
    client, _ = api_client
    assert client.get("/").status_code == 200
    r = client.get("/admin")
    assert r.status_code == 200
    assert "Cache-Control" in r.headers and r.headers["Cache-Control"] == "no-store"