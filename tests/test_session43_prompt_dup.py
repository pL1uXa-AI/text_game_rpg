# -*- coding: utf-8 -*-
"""A3 (аудит 41): действие игрока попадало в промпт ДВАЖДЫ.

`_process_action_inner` сначала писал ход в БД (`db.add_event(..., "player", text)`), а
затем собирал `[НЕДАВНЯЯ ИСТОРИЯ]` из этой же БД — уже включая только что записанное
действие, которое параллельно лежало в `[ДЕЙСТВИЕ ИГРОКА]`. Следствия: бюджет
`recent_token_budget` тратился на дубль, а модель, видя повтор, начинала копировать
собственную формулировку.

Лечится фильтром `_recent_block(..., exclude_ids=...)`: из недавней истории вырезается
(1) событие действия текущего хода, (2) при ↻ — само прежнее действие и события
заменяемого хода (старый ответ модель не должна перечитывать как «уже случившийся»).

Проверка — ПОВЕДЕНЧЕСКАЯ: реальный контур роутеров + реальный `build_messages`,
собирается настоящий текст промпта и считается число вхождений текста действия.
"""
from __future__ import annotations

import json

from backend import narrator as narrator_mod
from backend import db
from backend.routers import core as core_mod

THEME_ID = narrator_mod.THEMES[0]["id"]


def _mk_world(client) -> int:
    r = client.post("/api/worlds", json={
        "theme_id": THEME_ID, "name": "A3", "difficulty": "normal",
        "perspective": "second", "language": "ru"})
    assert r.status_code == 200
    return r.json()["world_id"]


def _prompt_of(calls: list) -> str:
    """Текст сообщений ОСНОВНОГО LLM-прохода хода (того, где собран `[ДЕЙСТВИЕ ИГРОКА]`).

    За ход к модели обращаются не один раз (аудит механики, судья логики — свои промпты),
    поэтому берём последний промпт с этим маркером, а не `calls[-1]`.
    """
    for messages, _kw in reversed(calls):
        text = "\n".join(str(m.get("content") or "") for m in (messages or []))
        if "[ДЕЙСТВИЕ ИГРОКА]" in text:
            return text
    raise AssertionError("ни один промпт хода не содержит [ДЕЙСТВИЕ ИГРОКА]")


def _capture_complete(monkeypatch, api_client):
    """Перехватить messages, уходит в `llm.complete` (нестримный путь хода).

    Текст ответа читается из `holder["reply"]` ДИНАМИЧЕСКИ — тест может сменить его
    между ходами (`client, holder, calls = ...`).
    """
    client, holder = api_client
    calls: list = []

    async def _complete(messages, *a, **kw):
        calls.append((messages, dict(kw)))
        return holder["reply"]

    monkeypatch.setattr(core_mod.llm, "complete", _complete)
    return client, holder, calls


def test_action_text_appears_once_in_prompt(monkeypatch, api_client):
    """Текст действия встречается в собранном промпте РОВНО ОДИН раз."""
    client, holder, calls = _capture_complete(monkeypatch, api_client)
    wid = _mk_world(client)
    action = "я поднимаю медный фонарь и иду к причалу"

    r = client.post(f"/api/worlds/{wid}/action", json={"text": action})
    assert r.status_code == 200
    prompt = _prompt_of(calls)
    assert prompt.count(action) == 1, (
        f"действие игрока в промпте {prompt.count(action)} раз — дубль в [НЕДАВНЯЯ ИСТОРИЯ]")
    assert f"[ДЕЙСТВИЕ ИГРОКА]\n{action}" in prompt, "блок действия на месте"

    # исторические ходы при этом НЕ теряются: вступление мира осталось в промпте
    assert "[НЕДАВНЯЯ ИСТОРИЯ]" in prompt or "Рассказчик:" in prompt


def test_previous_turns_stay_in_recent_block(monkeypatch, api_client):
    """Фильтр режет только ТЕКУЩЕЕ действие — прошлые ходы по-прежнему в истории."""
    client, holder, calls = _capture_complete(monkeypatch, api_client)
    wid = _mk_world(client)
    first = "первый шаг в туман"
    second = "второй шаг к маяку"

    assert client.post(f"/api/worlds/{wid}/action", json={"text": first}).status_code == 200
    assert client.post(f"/api/worlds/{wid}/action", json={"text": second}).status_code == 200
    prompt = _prompt_of(calls)

    assert prompt.count(second) == 1, "текущее действие — один раз"
    assert f"Игрок: {first}" in prompt, "прошлое действие игрока обязано остаться в истории"
    assert prompt.count(first) == 1


def test_regen_excludes_old_action_and_replaced_answer(monkeypatch, api_client):
    """↻: ни прежнее действие, ни старый ответ рассказчика не едут в недавнюю историю."""
    client, holder, calls = _capture_complete(monkeypatch, api_client)
    wid = _mk_world(client)
    action = "я толкаю дубовую дверь"

    # первый ход — со «старым» ответом
    holder["reply"] = "СТАРЫЙ_ОТВЕТ_РАССКАЗЧИКА. Дверь не поддаётся."
    calls.clear()
    old = client.post(f"/api/worlds/{wid}/action", json={"text": action})
    assert old.status_code == 200
    assert "СТАРЫЙ_ОТВЕТ_РАССКАЗЧИКА" in old.json()["reply"], "заглушка ответа применилась"
    assert calls, "первый ход дошёл до модели"

    # перегенерируем ТОТ ЖЕ ход новым ответом
    async def _complete2(messages, *a, **kw):
        calls.append((messages, {}))
        return "НОВЫЙ_ОТВЕТ. Дверь распахнулась."
    monkeypatch.setattr(core_mod.llm, "complete", _complete2)

    r = client.post(f"/api/worlds/{wid}/action",
                    json={"text": action, "regenerate": True})
    assert r.status_code == 200
    prompt = _prompt_of(calls)
    assert prompt.count(action) == 1, "при ↻ действие тоже не должно дублироваться"
    assert "СТАРЫЙ_ОТВЕТ_РАССКАЗЧИКА" not in prompt, (
        "заменяемый ответ не должен попасть в [НЕДАВНЯЯ ИСТОРИЯ]")

    # и в БД не задвоено: одно player-событие и один ответ хода (плюс вступление мира)
    evs = db.get_unfolded_events(wid)
    assert [e["role"] for e in evs].count("player") == 1
    narr = [e["content"] for e in evs if e["role"] == "narrator"]
    assert sum(1 for t in narr if t.startswith("НОВЫЙ_ОТВЕТ")) == 1
    assert not any(t.startswith("СТАРЫЙ_ОТВЕТ") for t in narr), "старый ответ удалён из истории"
    assert json.loads(db.get_world(wid)["setting"])  # мир живой


def test_recent_block_signature_filters_by_id() -> None:
    """Юнит `_recent_block`: exclude_ids вырезает событие, бюджет/порядок не ломаются."""
    evs = [{"id": 1, "seq": 1, "role": "narrator", "content": "раз " * 20, "folded": 0,
            "meta": {}},
           {"id": 2, "seq": 2, "role": "player", "content": "иду к морю", "folded": 0,
            "meta": {}}]
    orig = core_mod.db.get_unfolded_events
    core_mod.db.get_unfolded_events = lambda wid, limit=120, roles=None: list(evs)
    try:
        got = core_mod._recent_block(None, 1)
        assert [e["id"] for e in got] == [1, 2], "без фильтра — всё окно"
        got = core_mod._recent_block(None, 1, exclude_ids={2})
        assert [e["id"] for e in got] == [1], "исключённое событие вырезано"
    finally:
        core_mod.db.get_unfolded_events = orig
