# -*- coding: utf-8 -*-
"""Сессия 40, п.13 (мир «Новый мир»): игрок — не NPC.

Симптом мира №103: в «Состоянии» и сайдбаре в разделе NPC стоял `player = Игрок` рядом с
торговцем, а его «настроение» было биографией самого игрока («Проглотил кристалл…»); ту же
карточку `npc/player` заводил архивариус, и карта мира рисовала игрока «живым NPC».
Состояние героя живёт в `setting.player` — раздел NPC про ОКРУЖЕНИЕ. Закон 3: движок не
решает за мастера, он только не даёт смешивать сущности и объясняет отказ системкой.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
import sys  # noqa: E402
sys.path.insert(0, str(ROOT))

from backend.narrator import apply_directives, default_setting, format_state  # noqa: E402

THEME = {"name": "T", "genre": "фэнтези"}


def _st() -> dict:
    return default_setting(THEME, "normal")


# ── опознание ────────────────────────────────────────────────────────

def test_is_player_npc_by_key_and_name():
    from backend.mechanics import is_player_npc as me
    assert me("player") and me("PC") and me("protagonist") and me("игрок")
    assert me("npc1", {"name": "Игрок"}) and me("npc1", {"name": " игрок "})
    # обычные персонажи — не «игрок»
    assert not me("old_merchant", {"name": "Торговец Орин"})
    assert not me("player_two", {"name": "Питеец"})
    assert not me("npc1", {"name": "Игрок в маске"})
    assert not me("npc1", {}) and not me(None) and not me("")


# ── директивы: отказ вместо тихого заведения ─────────────────────────

def test_npc_set_refuses_player_and_still_works_for_npc():
    s = _st()
    msgs = apply_directives(s, {"npc_set": {"id": "player", "name": "Игрок",
                                            "mood": "Проглотил кристалл"}})
    assert "player" not in s["npc"], "игрок снова завёлся как персонаж окружения"
    assert any("не NPC" in m for m in msgs), f"мастер не получил объяснения: {msgs}"
    apply_directives(s, {"npc_set": {"id": "orin", "name": "Торговец Орин",
                                     "location": "ruins"}})
    assert s["npc"]["orin"]["name"] == "Торговец Орин"
    # п.13: «где стоит» — поле карточки NPC (без него «рядом» нельзя показать)
    assert s["npc"]["orin"]["location"] == "ruins"
    # имя по-прежнему за мастером: переименование существующего NPC работает
    apply_directives(s, {"npc_set": {"id": "orin", "mood": "насторожен"}})
    assert s["npc"]["orin"]["mood"] == "насторожен"


def test_npc_kill_refuses_player():
    s = _st()
    apply_directives(s, {"npc_set": {"id": "n1", "name": "Купец"}})
    s["npc"]["player"] = {"name": "Игрок", "alive": True}   # старый мир: заведён до правки
    msgs = apply_directives(s, {"npc_kill": "player"})
    assert s["npc"]["player"]["alive"] is True, "игрок «убит» как NPC"
    assert any("game_over" in m for m in msgs)
    assert any("мёртв" in m for m in apply_directives(s, {"npc_kill": "n1"}))


# ── показ рассказчику ────────────────────────────────────────────────

def _scene() -> dict:
    s = _st()
    s["current_location"] = "ruins"
    s["locations"] = {"ruins": {"name": "Руины", "desc": ""},
                      "square": {"name": "Площадь", "desc": ""}}
    s["npc"] = {"player": {"name": "Игрок", "mood": "Проглотил кристалл", "alive": True},
                "orin": {"name": "Торговец Орин", "mood": "нейтрален", "alive": True,
                         "location": "ruins"},
                "lyra": {"name": "Разведчица Лира", "mood": "дружелюбна", "alive": True,
                         "location": "square", "notes": {"тайна": "знает про ядро"}}}
    return s


def test_format_state_hides_player_and_marks_distance():
    txt = format_state(_scene())
    line = next(ln for ln in txt.splitlines() if ln.startswith("NPC"))
    assert "Игрок" not in line, f"игрок в блоке NPC: {line}"
    assert "Торговец Орин" in line
    assert "не рядом: Площадь" in line, "далёкий NPC не помечен — модель сочтёт его рядом"
    assert "Проглотил кристалл" not in txt, "биография игрока утекла в тайны NPC"
    assert "Разведчица Лира" in txt


def test_judge_and_audit_facts_skip_player():
    """Судья/аудит сверяют живых/мёртвых персонажей ОКРУЖЕНИЯ, а не игрока
    (у него свой портрет — иначе «Игрок — жив» вводил бы в заблуждение)."""
    from backend import narrator as N
    s = _st()
    s["npc"] = {"player": {"name": "Игрок", "alive": True},
                "orin": {"name": "Торговец Орин", "alive": False}}
    s["locations"] = {"ruins": {"name": "Руины", "desc": "", "connections": []}}
    s["current_location"] = "ruins"
    for msgs in (N.audit_messages(s, "поговорить с Орину"),
                 N.judge_messages(s, "поговорить с Орину", "Орин молчит.")):
        blob = json.dumps(msgs, ensure_ascii=False)
        line = next(ln for ln in blob.split("\n") if "NPC:" in ln)
        assert "Торговец Орин" in line
        assert "Игрок" not in line, f"игрок среди фактов-истин NPC: {line[:120]}"


# ── архивариус: карточку npc/player не заводит и в setting не льёт ───

def test_archivist_never_creates_player_card():
    """Путь записи карточки: npc-карточка с ключом `player` не проходит (проверяется
    поведением, а не текстом: мокаем LLM ответом архивариуса)."""
    pytest.importorskip("httpx")
    from backend import memory as M

    # llm.complete отдаёт текст строкой
    reply = json.dumps({"entities": [{"kind": "npc", "key": "player", "name": "Игрок",
                                      "summary": "Проглотил кристалл"},
                                     {"kind": "npc", "key": "orin", "name": "Торговец Орин",
                                      "summary": "торгует"}]}, ensure_ascii=False)

    async def _fake(*a, **k):
        return reply

    wid = M.db.create_world("п13", "custom", "фэнтези", "normal", "second", "ru", "",
                            _st(), {})
    monkey = pytest.MonkeyPatch()
    monkey.setattr(M.llm, "complete", _fake)
    monkey.setattr(M, "world_main_provider", lambda w: None)
    monkey.setattr(M, "index_entities", lambda w, c: None)   # хрому в тесте не трогаем
    try:
        asyncio.run(M.update_entity_cards(wid, "", ""))
        keys = [c["entity_key"] for c in M.db.list_entities(wid, kind="npc")]
        assert keys == ["orin"], f"архивариус завёл игрока как NPC: {keys}"
        st = json.loads(M.db.get_world(wid)["setting"])
        assert "player" not in st["npc"], "игрок протёк в setting.npc из карточек"
        assert "orin" in st["npc"]
    finally:
        monkey.undo()
        M.db.delete_world(wid)


def test_heal_player_as_npc(tmp_path, monkeypatch):
    """Само-исцеление: убирает дубль из setting и карточку из БД, идемпотентно."""
    pytest.importorskip("fastapi")
    from backend import memory as M
    st = _st()
    st["npc"] = {"player": {"name": "Игрок", "mood": "Проглотил кристалл", "alive": True},
                 "orin": {"name": "Торговец Орин", "alive": True}}
    wid = M.db.create_world("п13", "custom", "фэнтези", "normal", "second", "ru", "", st, {})
    try:
        M.db.upsert_entity(wid, "npc", "player", name="Игрок")
        M.db.upsert_entity(wid, "npc", "orin", name="Торговец Орин")
        assert M.heal_player_as_npc(wid, st) == 2, "запись в setting + карточка"
        assert "player" not in st["npc"] and "orin" in st["npc"]
        assert [c["entity_key"] for c in M.db.list_entities(wid, kind="npc")] == ["orin"]
        assert M.heal_player_as_npc(wid, st) == 0, "идемпотентность"
        assert any(k.endswith("_npc_player") for k in M.PENDING_VECTOR_KEYS), \
            "вектор удалённой карточки не поставлен на вычистку"
        M.PENDING_VECTOR_KEYS.clear()
    finally:
        M.db.delete_world(wid)


# ── карта мира: игрока среди «живых NPC» нет ─────────────────────────

def test_graph_omits_player_npc():
    from backend import graph as G
    st = _st()
    st["locations"] = {"ruins": {"name": "Руины", "desc": "", "connections": []}}
    st["npc"] = {"player": {"name": "Игрок", "alive": True},
                 "orin": {"name": "Торговец Орин", "alive": True}}
    nodes, _edges = G.build_graph(st)
    assert [n["label"] for n in nodes if n["kind"] == "npc"] == ["Торговец Орин"]


# ── фронтенд: реальный JS сайдбара (инвариант 19 — поведение, не текст) ──

def test_frontend_npc_row_filter_real_js():
    if subprocess.run(["node", "--version"], capture_output=True).returncode != 0:
        pytest.skip("node недоступен")
    app = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    guard = app[app.index("const _self"):app.index('$("npc-list")')]
    assert "_self(k, n)" in app and "📍" in app, "сайдбар не фильтрует и не помечает место"
    npc_state = {"player": {"name": "Игрок", "mood": "Проглотил кристалл"},
                 "orin": {"name": "Торговец Орин", "mood": "нейтрален", "alive": True},
                 "lyra": {"name": "Разведчица Лира", "mood": "дружелюбна",
                          "alive": True, "location": "square"}}
    snippet = ("const s = " + json.dumps({"current_location": "ruins", "npc": npc_state},
                                         ensure_ascii=False) + ";\n" + guard +
               'const out = Object.entries(s.npc).filter(([k, n]) => !_self(k, n))\n'
               '  .sort((a, b) => _far(a[1]) - _far(b[1]))\n'
               '  .map(([k, n]) => k + ":" + (_far(n) ? "далеко" : "рядом"));\n'
               'console.log(JSON.stringify(out));\n')
    tmp = ROOT / "_s40_p13_probe.cjs"
    tmp.write_text(snippet, encoding="utf-8")
    try:
        r = subprocess.run(["node", str(tmp)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
    finally:
        tmp.unlink(missing_ok=True)
    assert r.returncode == 0, (r.stdout + r.stderr)[:600]
    got = json.loads((r.stdout or "").strip().splitlines()[-1])
    assert got == ["orin:рядом", "lyra:далеко"], f"игрок/дальние в списке NPC: {got}"


def test_frontend_quick_actions_do_not_offer_far_npc():
    """Быстрые подсказки («Поговорить с …») — только по тем, кто рядом; поведение —
    тот же фильтр, что и в сайдбаре (проверяется реальным JS на Node)."""
    if subprocess.run(["node", "--version"], capture_output=True).returncode != 0:
        pytest.skip("node недоступен")
    app = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    blk = app[app.index("// живые NPC"):]
    blk = blk[:blk.index("// враги рядом")]
    tmp = ROOT / "_s40_p13_actions.cjs"
    tmp.write_text("const s = " + json.dumps(
        {"current_location": "ruins", "npc": {
            "player": {"name": "Игрок", "alive": True},
            "orin": {"name": "Торговец Орин", "alive": True},
            "lyra": {"name": "Лира", "alive": True, "location": "square"}}},
        ensure_ascii=False)
        + ";\nconst add = (l) => OUT.push(l); const OUT = [];\n" + blk +
        "\nconsole.log(JSON.stringify(OUT));\n", encoding="utf-8")
    try:
        r = subprocess.run(["node", str(tmp)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
    finally:
        tmp.unlink(missing_ok=True)
    assert r.returncode == 0, (r.stdout + r.stderr)[:500]
    got = json.loads(r.stdout.strip().splitlines()[-1])
    assert got == ["🗣 Поговорить с Торговец Орин"], f"подсказки рядом: {got}"
