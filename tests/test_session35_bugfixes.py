# -*- coding: utf-8 -*-
"""Сессия 35 — исправления багов, найденных баг-хантом сессии 34 (AGENT.md).

Проверяет ровно то, что было сломано:
  1. `_master_busy` не очищался при успешном/раннем выходе из `_maybe_autonomous_master`
     (discard стоял только в except) — автономный мастер НАВСЕГДА блокировался до рестарта.
  2. Сводки «будущего» утекали в промпт при hide-перемотке/загрузке сохранения:
     `get_summary_events` не фильтровал по folded, а stale-сводки с seq < точки отката
     не помечались.
  3. Ключи дневника строились через `abs(hash(...))` — рандомизированный хеш ломал
     дедуп между процессами.
  4. Подсказка бюджета в UI считалась по старым константам (2600/16384).
  5. Дублированный мёртвый `except` в core.py.
"""
from __future__ import annotations

import asyncio
import json
import re

import pytest

from backend import db, journal, rewind

# ─────────────────────────── баг 2: сводки «будущего» ───────────────────────────


def _mk_world(hp0: int = 50) -> int:
    return db.create_world(
        "тест", "custom", "фэнтези", "normal", "second", "ru", "",
        {"player": {"hp": hp0}, "locations": {}, "npc": {}, "quests": {}, "flags": {}},
        {})


def _play(wid: int, turns: int, snap_from: int = 1) -> None:
    for i in range(1, turns + 1):
        if i >= snap_from:
            db.save_turn_snapshot(wid, 2 * i - 1,
                                  {"player": {"hp": 50 + i}, "locations": {}, "npc": {},
                                   "quests": {}, "flags": {}})
        db.add_event(wid, "player", f"действие {i}", seq=2 * i - 1)
        db.add_event(wid, "narrator", f"ответ {i}", seq=2 * i)


def _fold_into_summary(wid: int, first_seq: int, last_seq: int, sum_seq: int) -> None:
    db.add_event(wid, "summary", f"сводка {first_seq}-{last_seq}", sum_seq,
                 meta={"covers": {"from": first_seq, "to": last_seq, "tokens": 600}})
    db.mark_folded(wid, last_seq)


def test_summary_events_filters_hidden_by_default():
    """get_summary_events по умолчанию отдаёт только живые сводки (folded=0)."""
    wid = _mk_world()
    _play(wid, 3)
    _fold_into_summary(wid, 1, 2, 7)                 # сводка на seq 7
    assert [e["seq"] for e in db.get_summary_events(wid)] == [7]
    # сокрыли её (как это делает hide-перемотка)
    db.fold_state_range(wid, db.FOLD_HIDDEN, 7, 7, ("summary",))
    assert db.get_summary_events(wid) == [], \
        "сокрытая сводка не должна попадать в промпт"
    assert [e["seq"] for e in db.get_summary_events(wid, unfolded_only=False)] == [7], \
        "для анализа/чистки нужны все строки"


def test_rewind_hide_marks_stale_summary_below_rewind_point():
    """Сводка с seq < точки отката, но покрывающая откатываемый диапазон, при hide
    должна быть помечена folded=2 — иначе утечёт в промпт."""
    wid = _mk_world()
    _play(wid, 6)
    # сводка на seq 13 покрывает ходы 1..3 (seq 1..6); ходы 4..6 (seq 7..12) не свёрнуты
    _fold_into_summary(wid, 1, 6, 13)
    asyncio.run(rewind.rewind_to(wid, 7, mode="hide"))   # откат к началу хода 4
    summaries = db.get_summary_events(wid, unfolded_only=False)
    assert summaries, "строка сводки остаётся в БД (hide не удаляет)"
    assert all(e["folded"] == db.FOLD_HIDDEN for e in summaries), \
        f"stale-сводка должна быть сокрыта: {[(e['seq'], e['folded']) for e in summaries]}"
    assert db.get_summary_events(wid) == [], "в промпт сокрытая сводка не попадает"


def test_rewind_delete_removes_stale_summary_below_rewind_point():
    """В delete-режиме stale-сводка с seq < точки отката должна быть УДАЛЕНА."""
    wid = _mk_world()
    _play(wid, 6)
    _fold_into_summary(wid, 1, 6, 13)
    res = asyncio.run(rewind.rewind_to(wid, 7))          # delete, к началу хода 4
    assert res["removed_summaries"] == 1, "сводка про откатываемый диапазон удаляется"
    assert db.get_summary_events(wid, unfolded_only=False) == [], \
        "в delete-режиме строк сводки не остаётся"
    assert db.get_summary_events(wid) == []


def test_rewind_hide_summary_after_point_hidden_and_filtered():
    """Сводка с seq >= точки отката прячется fold_state_range(FOLD_HIDDEN, seq) и не
    утекает в get_summary_events."""
    wid = _mk_world()
    _play(wid, 6)
    db.add_event(wid, "summary", "сводка о будущем", seq=14,
                 meta={"covers": {"from": 13, "to": 14, "tokens": 300}})   # после точки
    asyncio.run(rewind.rewind_to(wid, 7, mode="hide"))
    assert db.get_summary_events(wid) == [], \
        "сводка о сокрытом будущем не попадает в промпт"


# ─────────────────────────────── баг 3: дневник ───────────────────────────────


def test_journal_key_stable_across_processes():
    """Ключи дневника детерминированы: тот же (seq, cat, title) даёт тот же entity_key,
    даже после «рестарта» (пересоздания модуля с другим PYTHONHASHSEED)."""
    from backend import journal as j1
    wid = _mk_world()
    _play(wid, 1)
    ev = {"cat": "quest", "title": "Новое задание: Сделка с демонами", "text": "desc"}
    k1 = j1._stable_key(ev["title"])
    # эмулируем другой процесс: другой PYTHONHASHSEED не влияет на md5
    import hashlib
    k2 = hashlib.md5(ev["title"].encode("utf-8")).hexdigest()[:12]
    assert k1 == k2
    # и запись в БД использует стабильный ключ
    saved = journal.record_turn(wid, {}, {"quests": {"q1": {"title": "Сделка с демонами",
                                                           "status": "active"}}}, 2)
    key1 = saved[0]["entity_key"]
    # повторная запись того же хода должна перезаписать, а не создать дубль
    saved2 = journal.record_turn(wid, {}, {"quests": {"q1": {"title": "Сделка с демонами",
                                                             "status": "active"}}}, 2)
    assert saved2 and saved2[0]["entity_key"] == key1


def test_journal_note_key_stable():
    """Заметка игрока тоже использует стабильный ключ (не abs(hash))."""
    wid = _mk_world()
    _play(wid, 1)
    n1 = journal.add_player_note(wid, "помнить про мост")
    n2 = journal.add_player_note(wid, "помнить про мост")
    assert n1 and n2 and n1["entity_key"] == n2["entity_key"], \
        "та же заметка в том же ходе перезаписывается, а не дублируется"


# ─────────────────────────────── баг 4: UI-бюджет ───────────────────────────────


def _budget_ui(ctx: int, mtok: int) -> int:
    """Формула из frontend/app.js::updateGenUI (сессия 35) — floor как у серверного int()."""
    return max(400, ctx - (2600 + mtok) - max(512, min(16384, int(ctx * 0.2))))


def test_ui_budget_matches_server_formula():
    """UI-подсказка бюджета памяти совпадает с серверным world_recent_budget для
    стандартных случаев (новый мир, оверхед = CONTEXT_OVERHEAD)."""
    from backend import narrator as n
    # новый мир: измеренного промпта нет → world_prompt_overhead = 2600 + max_tokens
    for ctx, mtok in ((32768, 2000), (8192, 2000), (262144, 2000), (16384, 1024)):
        world = {"gen_settings": {"context_tokens": ctx, "max_tokens": mtok},
                 "setting": "{}"}
        server = n.world_recent_budget(world)
        ui = _budget_ui(ctx, mtok)
        assert ui == server, f"ctx={ctx} mtok={mtok}: UI {ui} != server {server}"
    # прежняя формула (2600+mtok+16384) на 32k давала ~11.8k — новая даёт ~21.6k
    assert _budget_ui(32768, 2000) > 20000, "резерв памяти теперь доля окна, а не 16k"


# ─────────────────────────────── баг 5: core.py ───────────────────────────────


def test_no_duplicate_except_metrics():
    """В core.py не должно быть двух подряд идущих одинаковых except-блоков метрик."""
    src = open("backend/routers/core.py", encoding="utf-8").read()
    n = len(re.findall(r'log\.warning\("метрики хода', src))
    assert n == 1, f"дублированный except убран (найдено {n})"


# ─────────────────────────────── баг 1: _master_busy ───────────────────────────────


def test_master_busy_released_on_early_return():
    """Ранний выход из _maybe_autonomous_master (мастер выключен / интервал не накоплен /
    игрок не застрял) обязан освобождать _master_busy — иначе мастер мёртв до рестарта."""
    from backend.routers import core as core_mod

    calls: list[str] = []

    async def _fake_sleep(*a, **kw):
        calls.append("sleep")
    async def _fake_gen(*a, **kw):
        calls.append("gen")
        return ("текст", {"flag": {"name": "x", "value": True}})

    original = core_mod.asyncio.sleep
    try:
        # случай 1: мастер выключен → ранний return → busy должен освободиться
        wid = _mk_world()
        core_mod._master_busy.discard(wid)
        async def _go_disabled():
            old_cfg = core_mod.get_config
            class _Cfg:
                autonomous_master_enabled = False
            core_mod.get_config = lambda: _Cfg()
            try:
                await core_mod._maybe_autonomous_master(wid, {}, "действие", "ответ")
            finally:
                core_mod.get_config = old_cfg
        core_mod.asyncio.sleep = _fake_sleep
        try:
            asyncio.run(_go_disabled())
        finally:
            core_mod.asyncio.sleep = original
        assert wid not in core_mod._master_busy, "busy должен освободиться при выключенном мастере"

        # случай 2: игрок не застрял (нет повторов/квестов мало) → ранний return
        wid2 = _mk_world()
        core_mod._master_busy.discard(wid2)
        for i in range(1, 5):
            db.add_event(wid2, "player", f"действие {i}", seq=2 * i - 1)
            db.add_event(wid2, "narrator", f"ответ {i}", seq=2 * i)
        s2 = json.loads(db.get_world(wid2)["setting"])
        s2["_player_turns"] = 4
        db.update_world(wid2, setting=s2)

        async def _go_stuck_reason_none():
            old_cfg = core_mod.get_config
            class _Cfg:
                autonomous_master_enabled = True
                autonomous_master_interval = 1
            core_mod.get_config = lambda: _Cfg()
            old_reason = core_mod.narrator.master_stuck_reason
            core_mod.narrator.master_stuck_reason = lambda *a, **kw: None
            try:
                await core_mod._maybe_autonomous_master(wid2, s2, "действие 4", "ответ 4")
            finally:
                core_mod.narrator.master_stuck_reason = old_reason
                core_mod.get_config = old_cfg
        core_mod.asyncio.sleep = _fake_sleep
        try:
            asyncio.run(_go_stuck_reason_none())
        finally:
            core_mod.asyncio.sleep = original
        assert wid2 not in core_mod._master_busy, \
            "busy должен освободиться, если игрок не застрял"
    finally:
        core_mod.asyncio.sleep = original
