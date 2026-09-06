# -*- coding: utf-8 -*-
"""Сессия 68 — D6 (аудит 41): док про фоновых агентов и реальные сигнатуры.

Было: `AGENT.md` описывал фоновых агентов как задачи «с состоянием хода» (в потоке хода
перечислены судья/мастер/боевой ИИ с `…` в сигнатурах), а реальная задача динамических
событий — `routers/core.py::_maybe_dynamic_event(world_id)` — мира НЕ получает: перечитывает
его сама. Строкой выше она в потоке хода вообще не значилась, поэтому читатель не находил её
нигде и достраивал картину по аналогии с судьёй («значит и ей шлют setting»). Расхождение
безобидное только на вид: оно ровно то, из-за чего вырос A4 (агент, судящий по чужому
снимку), — следующая сессия могла «починить» событие под выдуманную сигнатуру.

Стало (решение D6 — правкой ДОКА, логика не тронута, перезапуск сервера не нужен):
  * в потоке хода добавлена строка «🌍 динамическое событие» с явной сигнатурой;
  * снята неоднозначность `…` в `_maybe_autonomous_master(world_id, …)` /
    `_maybe_enemy_ai(world_id, …)`: там `…` = `action, reply`, а параметр `setting` — мёртвый
    остаток до-A4 эпохи («не передаём мир» — про событие, «не читаем из снимка» — про всех).

Тест деградационный: ловит и возврат подписи события к «с состоянием», и то, что пометка про
`…` осталась, когда мёртвый параметр изъят (или наоборот — снова участвует в расчёте).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "backend" / "routers" / "core.py"
AGENT = ROOT / "AGENT.md"

# агенты, которые получают снимок хода, но в расчёте его не используют (остаток до-A4)
SNAPSHOT_AGENTS = ("_maybe_logic_judge", "_maybe_autonomous_master", "_maybe_enemy_ai",
                   "_maybe_trigger_vision")


@pytest.fixture(scope="module")
def funcs() -> dict:
    tree = ast.parse(CORE.read_text(encoding="utf-8"))
    return {n.name: n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


@pytest.fixture(scope="module")
def agent_md() -> str:
    return AGENT.read_text(encoding="utf-8")


def _params(fn) -> list[str]:
    return [a.arg for a in fn.args.args]


def _used(fn) -> set[str]:
    return {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}


def _flow(agent_md: str) -> str:
    return agent_md.split("### Поток одного хода игрока", 1)[1].split("### Директивы", 1)[0]


def test_dynamic_event_signature_is_world_only(funcs, agent_md):
    """Событие — единственная задача фона без снимка хода: подпись `(world_id)` + перечитывание БД."""
    fn = funcs["_maybe_dynamic_event"]
    assert _params(fn) == ["world_id"], (
        "у _maybe_dynamic_event появился аргумент состояния — строка потока хода в AGENT.md "
        "обязана быть синхронна с подписью (D6): правь док вместе с кодом")
    assert "db.get_world(" in ast.unparse(fn), \
        "_maybe_dynamic_event больше не перечитывает мир — «читаем сами» в доке стало ложью"
    assert "_maybe_dynamic_event(world_id)" in agent_md, (
        "в AGENT.md нет явной сигнатуры события: читатель достраивает её по аналогии с судьёй")


def test_flow_lists_dynamic_event(funcs, agent_md):
    """Динамическое событие названо в потоке хода (раньше в списке фоновых задач его не было)."""
    flow = _flow(agent_md)
    assert "_maybe_dynamic_event" in flow, "поток хода не упоминает задачу события"
    assert "🌍" in flow, "событие в потоке хода не помечено своей эмблемой (как судья/мастер/ИИ)"


def test_dead_snapshot_note_matches_code(funcs, agent_md):
    """Пометка «`…` = action, reply, а setting — остаток» живёт ровно пока жив мёртвый параметр."""
    dead = {a for a in SNAPSHOT_AGENTS
            if "setting" in _params(funcs[a]) and "setting" not in _used(funcs[a])}
    assert dead == set(SNAPSHOT_AGENTS), (
        f"снимок хода снова участвует в расчёте (или параметр изъят) не у всех: мёртвый "
        f"только {sorted(dead)} — поправь пометку про `…` в AGENT.md")
    assert "остаток до-А4 эпохи" in _flow(agent_md), (
        "AGENT.md не объясняет, что значит `…` в сигнатурах агентов: читатель ждёт там "
        "состояние мира, а его передают лишь как неиспользуемый остаток (D6)")


def test_doc_does_not_claim_event_gets_state(funcs, agent_md):
    """Обратная претензия D6: док не приписывает событию чтение из снимка хода."""
    block = [ln for ln in _flow(agent_md).splitlines() if "_maybe_dynamic_event" in ln]
    assert block and all("agents_setting" not in ln for ln in block), (
        "строка потока про событие снова обещает снимок хода — его как раз не передают")
