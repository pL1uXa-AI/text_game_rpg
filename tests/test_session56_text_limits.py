# -*- coding: utf-8 -*-
"""Аудит 41, B3 (сессия 56): границы размера пользовательских текстов.

Было: в `backend/schemas.py` не было НИ ОДНОЙ границы (`NarratorIn.prompt`, `PlotIn.plot/lore`,
`LoreIn.content/title`, `WorldCreate.custom_*`, `DivineIn.complaint`, `EntityIn.*`, `ActionIn.text`).
Один POST с мегабайтным текстом проходил насквозь: строка в SQLite + (для персоны/лора)
раздутый system-промпт на КАЖДЫЙ ход мира. Персона вообще не имела бюджета: лор режет
`lore_token_budget`, историю — `recent_token_budget`, персона шла первой строкой промпта как есть.

Что проверяется (правило 19 — наблюдаем поведение приложения, а не тексты исходников):

1. Тексты длиннее границ (`backend/schemas.py`, константы `*_MAX`) отклоняются ВСЕМИ
   «создающими» эндпоинтами — 422, до записи в БД и до единственного LLM/эмбеддинг-вызова.
2. 422 человекочитаем: `detail` — строка (или список строк) с именем поля и лимитом, а не
   сырой pydantic-список объектов `loc`/`type`/`ctx` (фронт показывает именно `detail`),
   и отказ видим в журнале (правило 14).
3. Легальные тексты (сотни раз короче лимита) проходят как раньше — границы не впритык.
4. Персона, которая уже лежит в БД (старый мир, импорт дампа, правка файла пресета), не
   может раздуть промпт: `narrator.build_system_prompt` подрезает её по токен-бюджету и
   пишет об этом в журнал; короткую персону не трогает.
5. Порядок жертв в промпте: при переборе окна первым урезается ЛОР (`build_messages`),
   а персона/состояние/действие остаются — раздутый лор не съедает остальное.
6. Прочие ветки обработчика 422 («не задано», «неверный тип») не испорчены, а `maxlength`
   во фронте совпадает с серверными границами (UI не должен обещать больше).

Запуск: "…\\3.12.10\\python.exe" -X utf8 -m pytest tests/test_session56_text_limits.py -q
"""
from __future__ import annotations

import logging

import pytest

from backend import narrator
from backend import schemas as S


# ── 1. Отказ по границе, до записи в БД и до обращения к модели ──────────────
@pytest.mark.parametrize("path,body,field,limit", [
    ("/api/narrators", {"name": "Гигант", "prompt": "я" * (S.PERSONA_MAX + 1)}, "prompt", S.PERSONA_MAX),
    ("/api/narrators", {"name": "я" * (S.NAME_MAX + 1), "prompt": "normal"}, "name", S.NAME_MAX),
    ("/api/plots", {"name": "Сюжет", "plot": "п" * (S.PLOT_MAX + 1)}, "plot", S.PLOT_MAX),
    ("/api/plots", {"name": "Сюжет", "plot": "норм", "lore": "л" * (S.LORE_TEXT_MAX + 1)},
     "lore", S.LORE_TEXT_MAX),
])
def test_oversized_texts_rejected(api_client, path, body, field, limit):
    client, _ = api_client
    r = client.post(path, json=body)
    assert r.status_code == 422, f"{path}: гигантский текст принят ({r.status_code})"
    assert str(limit) in r.text, f"в ответе нет числа лимита {limit}: {r.text[:200]}"
    assert field in r.text, f"в ответе нет имени поля {field}: {r.text[:200]}"
    # каталог не пополвился отказавшей записью
    assert all(b.get(field) is None or len(str(b.get(field))) <= limit
               for b in client.get(path).json())


def test_oversized_lore_rejected_before_db(api_client):
    client, _ = api_client
    wid = client.post("/api/worlds", json={"theme_id": narrator.THEMES[0]["id"], "name": "L"}
                      ).json()["world_id"]
    # мир из системного сюжета приносит свои статьи лора — сравниваем с «до»
    before = client.get(f"/api/worlds/{wid}/lore").json()
    r = client.post(f"/api/worlds/{wid}/lore",
                    json={"title": "Статья", "content": "т" * (S.LORE_TEXT_MAX + 1)})
    assert r.status_code == 422
    assert client.get(f"/api/worlds/{wid}/lore").json() == before, "статьи записаны несмотря на отказ"
    # и заголовок тоже не бесконечный
    assert client.post(f"/api/worlds/{wid}/lore",
                       json={"title": "з" * (S.LORE_TITLE_MAX + 1), "content": "текст"}
                       ).status_code == 422


def test_oversized_world_create_and_divine_and_entity(api_client):
    client, _ = api_client
    before = len(client.get("/api/worlds").json())
    # создание мира — самый дорогой путь (2–3 LLM-прохода + эмбеддинги лора)
    assert client.post("/api/worlds", json={
        "theme_id": "custom", "custom_plot": "с" * (S.PLOT_MAX + 1)}).status_code == 422
    assert client.post("/api/worlds", json={
        "theme_id": "custom", "custom_plot": "норм", "custom_lore": "л" * (S.LORE_TEXT_MAX + 1),
    }).status_code == 422
    assert client.post("/api/worlds", json={
        "theme_id": "custom", "custom_plot": "норм", "custom_hook": "х" * (S.HOOK_MAX + 1),
    }).status_code == 422
    assert len(client.get("/api/worlds").json()) == before, "отказавший мир всё-таки создан"

    wid = client.post("/api/worlds", json={"theme_id": narrator.THEMES[0]["id"],
                                           "name": "D"}).json()["world_id"]
    assert client.post(f"/api/worlds/{wid}/divine",
                       json={"complaint": "ж" * (S.COMPLAINT_MAX + 1)}).status_code == 422
    assert client.post(f"/api/worlds/{wid}/entities",
                       json={"kind": "npc", "key": "k", "summary": "с" * (S.CARD_SUMMARY_MAX + 1)}
                       ).status_code == 422
    assert client.post(f"/api/worlds/{wid}/saves",
                       json={"name": "и" * (S.NAME_MAX + 1)}).status_code == 422


def test_action_hard_cap_is_ahead_of_soft_limit(api_client):
    """Мягкий лимит действия настраивается в админке (`max_action_chars`, 400 с текстом),
    поэтому схема обязана пускать больше: иначе админка потеряла бы смысл. 422 — только
    за жёстким потолком тела запроса."""
    from backend.config import get_config

    client, _ = api_client
    soft = int(get_config().max_action_chars)
    assert S.ACTION_HARD_MAX > soft, "жёсткий потолок схемы не должен отрезать настройку админки"
    wid = client.post("/api/worlds", json={"theme_id": narrator.THEMES[0]["id"],
                                           "name": "A"}).json()["world_id"]
    # чуть больше мягкого лимита — по-прежнему 400 «разбей на шаги», а не 422 от схемы
    r = client.post(f"/api/worlds/{wid}/action", json={"text": "д" * (soft + 5)})
    assert r.status_code == 400, r.text
    assert client.post(f"/api/worlds/{wid}/action",
                       json={"text": "д" * (S.ACTION_HARD_MAX + 1)}).status_code == 422


# ── 2. Ответ человекочитаем и виден в журнале ─────────────────────────────────
def test_422_detail_is_human_readable_and_logged(api_client, caplog):
    client, _ = api_client
    with caplog.at_level(logging.WARNING, logger="textgame"):
        r = client.post("/api/narrators", json={"name": "Х", "prompt": "я" * (S.PERSONA_MAX + 1)})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert isinstance(detail, str), f"фронт читает detail как текст, пришло: {type(detail)}"
    assert "prompt" in detail and str(S.PERSONA_MAX) in detail, detail
    assert any("422" in m and "/api/narrators" in m for m in (x.getMessage() for x in caplog.records)), \
        "отказ по границе не видно в журнале (правило 14)"


def test_422_still_reports_other_kinds_of_bad_body(api_client):
    """Обработчик не должен молча портить прочие ошибки валидации: «не задано поле» и
    «неверный тип» — те же две ветки, что и граница длины."""
    client, _ = api_client
    r = client.post("/api/narrators", json={"prompt": "без имени"})
    assert r.status_code == 422
    assert "name" in r.json()["detail"], r.text
    assert "обязательное" in r.json()["detail"], r.text
    r = client.post("/api/worlds/1/rewind", json={"seq": "не число"})
    assert r.status_code == 422, r.text
    assert "seq" in r.json()["detail"] and "тип" in r.json()["detail"], r.text


# ── 3. Границы не впритык к реальному ────────────────────────────────────────
def test_limits_have_headroom_over_real_texts():
    """Каждая граница ≥ N × самого длинного ШТАТНОГО текста, иначе следующий же сюжет/
    пресет упрётся в 422 (это ровно то, о чём предупреждал отчёт)."""
    from backend import narrators_loader

    longest_persona = max((len(p["prompt"]) for p in narrators_loader.NARRATOR_PRESETS), default=0)
    assert S.PERSONA_MAX > 4 * max(longest_persona, 1), \
        f"PERSONA_MAX впритык к пресетам ({longest_persona})"
    # лорные статьи системных сюжетов (~17 КБ) и обычные статьи из UI (~1.2 КБ)
    assert S.LORE_TEXT_MAX > 5 * 18_000
    assert S.LORE_TITLE_MAX > 4 * 40
    assert S.PLOT_MAX > 5 * 1_000
    assert S.HOOK_MAX >= 10 * 240, "фронт режет зацеп до 240 — серверный лимит не должен быть уже"
    assert S.COMPLAINT_MAX >= 4 * 500, "textarea жалобы в UI — 500 символов"



def test_ui_maxlength_never_promises_more_than_server():
    """UI-поля обязаны обещать НЕ больше, чем разрешает сервер: иначе «ввёл — получил 422».
    Сверяются константы двух слоёв (так же, как `LOG_PAGE_SIZE` против серверного потолка
    в сессии 48): из фронта вытаскиваются все `maxlength="N"`, и каждое обязано быть
    одной из серверных границ (или осознанным более мягким ограничением UI)."""
    import re
    from pathlib import Path

    from backend.config import get_config

    root = Path(__file__).resolve().parent.parent
    limits = {v for k, v in vars(S).items() if k.endswith("_MAX") and isinstance(v, int)}
    assert S.PERSONA_MAX in limits and S.LORE_TEXT_MAX in limits
    # мягкие UI-ограничения (зацеп 240, жалоба 500) и настраиваемый лимит действия
    ui_soft = {240, 500, int(get_config().max_action_chars)}
    found: set[int] = set()
    for rel in ("frontend/app.js", "frontend/index.html"):
        found |= {int(m) for m in re.findall(r'maxlength="(\d+)"',
                                             (root / rel).read_text(encoding="utf-8"))}
    assert found, "во фронте пропали все maxlength — поле-обещание не найти"
    for n in sorted(found):
        assert n in limits | ui_soft, (
            f"maxlength={n} во фронте не совпадает ни с одной границей schemas.py "
            f"(серверные: {sorted(limits)})")
        assert n <= max(limits), f"maxlength={n} щедрее самого большого серверного лимита"

def test_persona_clip_budget_covers_presets():
    """Кламп персоны в промпте не должен задевать ни один штатный рассказчик."""
    from backend import narrators_loader
    from backend.config import est_tokens

    for p in narrators_loader.NARRATOR_PRESETS:
        assert est_tokens(p["prompt"]) <= narrator.PERSONA_TOKEN_BUDGET, \
            f"пресет «{p['name']}» урезался бы в промпте"


# ── 4. Персона, уже лежащая в БД, промпт не раздувает ────────────────────────
def _world():
    return {"id": 1, "name": "Мир", "theme": narrator.THEMES[0]["id"], "genre": "фэнтези",
            "difficulty": "normal", "perspective": "second", "language": "ru",
            "custom_hook": "", "gen_settings": {}}


def _setting():
    return narrator.default_setting({"name": "Мир", "genre": "фэнтези", "style": "",
                                     "desc": "", "opening": ""}, "normal")


def test_short_persona_untouched():
    base = narrator.build_system_prompt(_world(), _setting(), persona="Ты — сонный голос.")
    assert base.startswith("Ты — сонный голос.")
    for preset in narrator.NARRATOR_PRESETS:
        assert narrator.build_system_prompt(_world(), _setting(),
                                           persona=preset["prompt"]).startswith(preset["prompt"])


def test_giant_persona_is_clipped_and_logged(caplog):
    from backend.config import est_tokens

    monster = "Запомни: " + "слово " * 200_000          # ≈400 КБ, «в БД уже лежит»
    assert est_tokens(monster) > 50 * narrator.PERSONA_TOKEN_BUDGET
    with caplog.at_level(logging.WARNING, logger="textgame"):
        prompt = narrator.build_system_prompt(_world(), _setting(), persona=monster)
    assert est_tokens(prompt) < est_tokens(monster) / 10, "персона не подрезана в промпте"
    assert "Запомни:" in prompt, "обрезка должна оставлять начало персоны, а не стирать её"
    assert any("обрезана в промпте" in x.getMessage() for x in caplog.records), \
        "тихое обрезание персоны запрещено правилом 14"
    # тот же текст в compact-проходе кубика не уезжает за бюджет
    assert est_tokens(narrator.clip_persona(monster) or "") <= narrator.PERSONA_TOKEN_BUDGET


def test_lore_is_the_first_sacrifice_in_build_messages(fake_config):
    """Из пункта: лор попадает в ЖЕРТВЕННУЮ секцию — при переборе окна он урезается
    первым, а состояние/персона/действие остаются."""
    import json

    from backend.config import est_tokens

    fake_config(context_tokens=8192, max_tokens=2000)
    st = _setting()
    st.update({"player": {"hp": 1, "inventory": []}, "locations": {"here": {"name": "Тут"}},
               "npc": {}, "quests": {}, "flags": {}, "current_location": "here"})
    w = _world()
    w["gen_settings"] = json.dumps({"context_tokens": 8192, "max_tokens": 2000})
    huge_lore = ["ло р" * 3000] * 3
    msgs, meta = narrator.build_messages(w, st, "идти", [], [], [], None, lore=huge_lore)
    assert est_tokens(msgs[0]["content"]) <= 8192 - 2000, meta
    assert meta["trimmed"] and meta["trimmed"][0].startswith("лор"), meta["trimmed"]
