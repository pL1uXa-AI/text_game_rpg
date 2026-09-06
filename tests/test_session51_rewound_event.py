# -*- coding: utf-8 -*-
"""Сессия 51 — A11 (аудит 41): документированное событие `rewound` существует.

`backend/rewind.py` (докстринг модуля, п.6) обещал: «мир помечает точку отсчёта, и все
открытые вкладки получают событие `rewound` — чтобы UI перерисовал лог, а не дорисовывал
к старому». Никто его не публиковал: в шину уходили только `{"type": "event", …}` от хука
БД, а во фронте ветки `rewound` не было. Практический симптом: вторая открытая вкладка
того же мира после перемотки (или загрузки сохранения) в первой держала удалённый лог до
ручного F5 — а новые ходы дорисовывались к прошлому, которого уже нет.

Чиним обе половины обещания:
  * сервер: `rewind_to` публикует в шину `{"type": "rewound", …}` и ставит метку
    `meta.rewound` на служебной строке отката (носитель сигнала для поллинга);
  * фронт: вкладка по сигналу ПЕРЕЗАГРУЖАЕТ мир (догонять удалившееся бессмысленно),
    а вкладка без живой SSE — по той же метке или по `latest_seq` сервера, который стал
    меньше её курсора.

Правило 19: проверяется поведение (шина, HTTP, настоящие JS-функции на Node).
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from backend import bus, db, rewind
from conftest import create_world_payload

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
APP_JS = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")

_S = {"player": {"hp": 50}, "locations": {}, "npc": {}, "quests": {}, "flags": {}}


def _mk_world(client) -> int:
    return client.post("/api/worlds", json=create_world_payload()).json()["world_id"]


def _play(client, wid: int, n: int, start: int = 0) -> None:
    for i in range(n):
        r = client.post(f"/api/worlds/{wid}/action", json={"text": f"шаг {start + i}"})
        assert r.status_code == 200, r.text


def _plain_world() -> int:
    return db.create_world("тест", "custom", "фэнтези", "normal", "second", "ru", "",
                           dict(_S), {})


def _turns(wid: int, n: int) -> list[int]:
    """n ходов «как в игре» (снимок ДО хода + действие + ответ); возвращает точки отката."""
    pts = []
    for i in range(1, n + 1):
        seq = 2 * i - 1
        db.save_turn_snapshot(wid, seq, dict(_S))
        pts.append(seq)
        db.add_event(wid, "player", f"действие {i}", seq=seq)
        db.add_event(wid, "narrator", f"ответ {i}", seq=seq + 1)
    return pts


def _rewound_rows(wid: int) -> list[dict]:
    return [e for e in db.get_events(wid) if (e.get("meta") or {}).get("rewound")]


# ═════════════════ 1. сервер: обещание шины исполнено ═════════════════

def test_rewind_to_publishes_rewound_on_the_bus():
    """`rewind_to` обязан отдать подписчикам мира событие типа `rewound`.

    Деградация (проверено): без публикации подписчик получает только строки БД — тест красный."""
    wid = _plain_world()
    pts = _turns(wid, 3)

    bus.reset()
    q = bus.subscribe(wid)
    res = asyncio.run(rewind.rewind_to(wid, pts[1]))
    payloads = []
    while not q.empty():
        payloads.append(q.get_nowait())
    bus.unsubscribe(wid, q)
    bus.reset()

    types = [p.get("type") for p in payloads]
    assert "rewound" in types, f"шина не видела отката, только {types}"
    p = next(x for x in payloads if x["type"] == "rewound")
    assert p["world_id"] == wid and p["seq"] == res["seq"], f"в сигнале не та точка: {p}"
    assert p["mode"] == "delete" and p["event_id"], "сигнал не связан со строкой отката"
    assert p["latest_seq"] == db.latest_seq(wid), "сигнал врёт о хвосте таймлайна"


def test_rewound_is_published_after_the_db_is_rewound():
    """Порядок важен: вкладка по `rewound` перечитывает мир, и к моменту сигнала БД уже
    обязана быть откатана — иначе перезагрузка вернула бы старый таймлайн."""
    wid = _plain_world()
    pts = _turns(wid, 3)
    seen: list[int] = []

    bus.reset()
    q = bus.subscribe(wid)
    real_put = q.put_nowait

    def _spy(payload):
        if payload.get("type") == "rewound":
            seen.append(db.latest_seq(wid))       # какой хвост виден в момент сигнала
        return real_put(payload)

    q.put_nowait = _spy
    asyncio.run(rewind.rewind_to(wid, pts[1]))
    bus.unsubscribe(wid, q)
    bus.reset()

    assert seen == [pts[1]], (f"в момент сигнала хвост = {seen}, точка = {pts[1]} — "
                              "публикация ушла до записей в БД")


def test_rewind_endpoint_marks_its_event(api_client):
    """HTTP ⏪: служебная строка отката несёт `meta.rewound` (носитель сигнала для вкладок
    без SSE), а ответ — её id, чтобы клиент сопоставил сигнал с событием."""
    client, _ = api_client
    wid = _mk_world(client)
    _play(client, wid, 3)
    pts = [p["seq"] for p in db.list_turn_snapshots(wid)]
    assert len(pts) >= 2, f"мало снапшотов для теста: {pts}"

    r = client.post(f"/api/worlds/{wid}/rewind", json={"seq": pts[1], "mode": "delete"})
    assert r.status_code == 200, r.text
    rows = _rewound_rows(wid)
    assert len(rows) == 1, f"строк отката с меткой: {len(rows)}"
    assert rows[0]["meta"]["rewound"] == {"to_seq": pts[1], "mode": "delete"}
    assert r.json()["event"]["id"] == rows[0]["id"], "роутер отдаёт другую строку отката"


def test_save_load_marks_its_rewind(api_client):
    """💾 «Загрузить сохранение» — тот же откат (режим hide) и обязан быть помечен:
    иначе вторая вкладка никогда не узнает, что её таймлайн сокрыли."""
    client, _ = api_client
    wid = _mk_world(client)
    _play(client, wid, 2)
    sid = client.post(f"/api/worlds/{wid}/saves", json={"name": "середина"}).json()["save"]["id"]
    _play(client, wid, 2, start=3)
    assert not _rewound_rows(wid), "метка отката появилась сама собой"

    r = client.post(f"/api/worlds/{wid}/saves/{sid}/load")
    assert r.status_code == 200, r.text
    rows = _rewound_rows(wid)
    assert len(rows) == 1 and rows[0]["meta"]["rewound"]["mode"] == "hide", rows


def test_failed_rewind_publishes_nothing(api_client):
    """Отказа (400/409) в шине быть не может: мир не меняли — перечитывать его незачем."""
    client, _ = api_client
    wid = _mk_world(client)
    _play(client, wid, 2)
    bus.reset()
    q = bus.subscribe(wid)

    assert client.post(f"/api/worlds/{wid}/rewind", json={"seq": 0}).status_code == 400
    got = []
    while not q.empty():
        got.append(q.get_nowait())
    assert not [g for g in got if g.get("type") == "rewound"], "на 400 всё равно опубликовали"

    # и на занятом мире (409, A5) — тоже тишина
    from backend import bg
    with bg.turn_block(wid):
        assert client.post(f"/api/worlds/{wid}/rewind",
                           json={"seq": 3, "mode": "delete"}).status_code == 409
    while not q.empty():
        got.append(q.get_nowait())
    assert not [g for g in got if g.get("type") == "rewound"], "409 разослал откат, которого не было"
    bus.unsubscribe(wid, q)
    bus.reset()


def test_events_poll_reports_latest_seq(api_client):
    """Запасной путь (поллинг) обязан нести `latest_seq`: иначе отсталая вкладка не
    отличит «новых событий нет» от «хвост удалён перемоткой» (строка отката ниже её `since`)."""
    client, _ = api_client
    wid = _mk_world(client)
    _play(client, wid, 3)
    latest = db.latest_seq(wid)
    body = client.get(f"/api/worlds/{wid}/events?since=0").json()
    assert body["latest_seq"] == latest, "поллинг не сообщает честный хвост таймлайна"

    pts = [p["seq"] for p in db.list_turn_snapshots(wid)]
    assert client.post(f"/api/worlds/{wid}/rewind",
                       json={"seq": pts[1], "mode": "delete"}).status_code == 200
    after = client.get(f"/api/worlds/{wid}/events?since=0").json()["latest_seq"]
    assert after == pts[1] < latest, f"после отката latest_seq={after}, точка={pts[1]}"
    # курсор вкладки, игравшей до отката, — больше серверного хвоста: сигнал «перезагружай»
    assert after < latest


# ═════════════════ 2. фронт: настоящие функции на Node ═════════════════

def _fn_src(name: str) -> str:
    from check_frontend import _extract_fn
    body = _extract_fn(APP_JS, name)
    if f"async function {name}(" in APP_JS and not body.startswith("async"):
        body = "async " + body
    return body


def _run_node(snippet: str, marker: str) -> None:
    if subprocess.run(["node", "--version"], capture_output=True, text=True).returncode != 0:
        pytest.skip("node недоступен")
    tmp = ROOT / "_s51_rewound_probe.cjs"
    tmp.write_text(snippet, encoding="utf-8")
    try:
        r = subprocess.run(["node", str(tmp)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
    finally:
        tmp.unlink(missing_ok=True)
    out = (r.stdout or "") + (r.stderr or "")
    assert marker in out, out[:3000]


_HARNESS = r"""
/* Заглушки DOM/сети: проверяем ПОВЕДЕНИЕ реальных функций — кто и когда перезагружает мир. */
let opens = [], msgs = [], responses = [];
const state = { currentWorld: 7, seenSeq: 20, reloading: false, streaming: false,
                pollTimer: null, eventSource: null };
let _pollTick = 0;

function API() { return Promise.resolve(responses.shift()); }
let openWorld = (id) => { opens.push(id); return Promise.resolve(); };
function appendMsg(e) { msgs.push(e && e.id); }
function syncStateFromServer() { return Promise.resolve(false); }
function renderSetting() {}
function loadEntities() {}
function requestAnimationFrame(fn) { fn(); }
const _log = { scrollTop: 0, scrollHeight: 10 };
function $(id) { return id === "log" ? _log : null; }
global.document = { querySelector: () => null, querySelectorAll: () => [] };
global.CSS = { escape: (s) => String(s) };
global.EventSource = function (url) { this.url = url; this.readyState = 1; this.close = () => {}; };

(async () => {
  let bad = [];

  // 1. мета отката (hide-режим — строка доходит как новая) -> полная перезагрузка лога
  opens = []; msgs = []; state.seenSeq = 20; state.reloading = false;
  responses = [{ events: [{ id: 91, seq: 25, role: "system", folded: 0,
                            meta: { rewound: { to_seq: 12, mode: "hide" } } }],
                 truncated: false, latest_seq: 25 }];
  await pollEvents();
  if (!opens.length) bad.push("1: meta.rewound не перезагрузил мир");
  if (msgs.length) bad.push("1: к устаревшему логу дорисовано сообщение: " + msgs.join());

  // 2. хвост меньше курсора: delete-перемотка из чужой вкладки (её строка — ниже since)
  opens = []; msgs = []; state.seenSeq = 30; state.reloading = false;
  responses = [{ events: [], truncated: false, latest_seq: 12 }];
  await pollEvents();
  if (!opens.length) bad.push("2: latest_seq < seenSeq не перезагрузил лог");
  if (msgs.length) bad.push("2: что-то дорисовано: " + msgs.join());

  // 3. нормальный догон: перезагрузки НЕТ, сообщение дорисовано, курсор сдвинут
  opens = []; msgs = []; state.seenSeq = 20; state.reloading = false;
  responses = [{ events: [{ id: 5, seq: 21, role: "narrator", folded: 0, meta: {} }],
                 truncated: false, latest_seq: 21 }];
  await pollEvents();
  if (opens.length) bad.push("3: обычные события вызвали перезагрузку");
  if (msgs.join() !== "5") bad.push("3: сообщение не дорисовано: " + msgs.join());
  if (state.seenSeq !== 21) bad.push("3: курсор не сдвинут: " + state.seenSeq);

  // 4. сигнал чужого мира — текущую вкладку не трогаем
  opens = []; state.reloading = false;
  await applyRewound({ world_id: 999, seq: 3 });
  if (opens.length) bad.push("4: перезагрузили чужой мир");

  // 5. applyRewound своего мира — ровно одна перезагрузка, «замок» снят
  opens = []; state.reloading = false;
  await applyRewound({ world_id: 7, seq: 3 });
  if (opens.join() !== "7") bad.push("5: openWorld не вызван: " + opens.join());
  if (state.reloading) bad.push("5: флаг reloading залип — вкладка оглохла навечно");

  // 6. «замок»: параллельные сигналы не плодят двойную перезагрузку
  opens = []; state.reloading = false;
  await Promise.all([reloadWorld(), reloadWorld()]);
  if (opens.length !== 1) bad.push("6: перезагрузок: " + opens.length);
  state.reloading = false;

  // 6b. отказ сети во время перезагрузки не оставляет залипший замок
  opens = []; state.reloading = false;
  openWorld = () => Promise.reject(new Error("сеть легла"));
  await reloadWorld();
  if (state.reloading) bad.push("6b: после отказа замок не снят");
  openWorld = (id) => { opens.push(id); return Promise.resolve(); };
  state.reloading = false;

  // 7. ветка SSE: реальный onmessage из startLiveBus на посланном шине `rewound`
  opens = []; state.reloading = false; state.eventSource = null;
  startLiveBus();
  const es = state.eventSource;
  if (!es || typeof es.onmessage !== "function") bad.push("7: startLiveBus не повесил onmessage");
  else {
    es.onmessage({ data: JSON.stringify({ type: "rewound", world_id: 7, seq: 4 }) });
    await new Promise((r) => setTimeout(r, 5));
    if (opens.join() !== "7") bad.push("7: шина не перезагрузила лог: " + opens.join());
    // «ready» и мусор не должны ни рисовать, ни ронять обработчик
    opens = []; msgs = []; state.reloading = false;
    es.onmessage({ data: JSON.stringify({ type: "ready", world_id: 7 }) });
    es.onmessage({ data: "{битый json" });
    await new Promise((r) => setTimeout(r, 5));
    if (opens.length || msgs.length) bad.push("7b: ready/мусор что-то сделали");
  }

  // 8. 💾 loadSaveSlot: одна перезагрузка общим путём, «замок» держится всё её время
  //    (иначе ответный `rewound` из шины грузил бы мир вторично), состояние слота применено.
  opens = []; state.reloading = false; let lockDuring = null;
  responses = [{ setting: { player: { hp: 42 } }, event: { id: 3 }, unfolded: 2,
                 removed_events: 4 }];
  openWorld = (id) => { lockDuring = state.reloading; opens.push(id); return Promise.resolve(); };
  const saved = await loadSaveSlot(5);
  if (!saved || !saved.setting) bad.push("8: loadSaveSlot не вернул ответ сервера");
  if (opens.join() !== "7") bad.push("8: загрузку не перезагрузили: " + opens.join());
  if (lockDuring !== true) bad.push("8: «замок» не поднят — свой rewound грузит мир вторично");
  if (state.reloading) bad.push("8: замок залип");
  if (JSON.stringify(state.setting) !== '{"player":{"hp":42}}')
    bad.push("8: состояние слота не применено: " + JSON.stringify(state.setting));

  // 9. отказ (409 «мир занят», A5) — замок обязан быть снят, ошибка уходит наружу
  let rejected = false;
  responses = [];                       // API на пустой очереди -> undefined, как при сетевой ошибке
  try { await loadSaveSlot(6); } catch (_) { rejected = true; }
  if (state.reloading) bad.push("9: после отказа замок залип — вкладка оглохла");
  if (!rejected) bad.push("9: loadSaveSlot проглотил отказ сервера");
  openWorld = (id) => { opens.push(id); return Promise.resolve(); };

  console.log(bad.length ? "BAD " + bad.join(" | ") : "OK s51");
})();
"""

_SNIPPET = "\n".join([
    _fn_src("isRewoundEvent"),
    _fn_src("applyRewound"),
    _fn_src("reloadWorld"),
    _fn_src("liveBusOpen"),
    _fn_src("shouldRenderEvent"),
    _fn_src("shouldRenderLive"),
    _fn_src("loadSaveSlot"),
    _fn_src("pollEvents"),
    _fn_src("startLiveBus"),
    _fn_src("stopLiveBus"),
    _HARNESS,
])


def test_frontend_rewound_real_js():
    """Настоящие `pollEvents`/`applyRewound`/`startLiveBus` на Node: вкладка без удачи с SSE
    обязана ПЕРЕЗАГРУЗИТЬ лог по метке отката или по укоротившемуся хвосту, а не дорисовывать
    к удалённому прошлому; обычные события при этом живут по-старому."""
    _run_node(_SNIPPET, "OK s51")


def test_docs_promise_the_signal_they_deliver():
    """Док не врёт (тот же класс, что `test_logsetup_docstring_promises_only_real_helpers`
    из A6): обещание про `rewound` обязано описывать и то, ЧЕМ его видит вторая вкладка —
    иначе следующий читатель снова примет док за фантазию, а аудитор — за незакрытый пункт.

    Живой протокол «кто какие типы шлёт» сторожит не этот тест, а `check_frontend.bus_types_check`
    (правило 19: тексты исходников — к чекеру, поведение — в тест)."""
    doc = (ROOT / "backend" / "rewind.py").read_text(encoding="utf-8").split('"""')[1]
    assert "rewound" in doc
    assert "meta.rewound" in doc or "поллинг" in doc, \
        "док обещает rewound, но не говорит про запасной путь без SSE"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "rewound" in readme, "живая лента в README не описывает сигнал перемотки"
