# -*- coding: utf-8 -*-
"""Сессия 48 — A8 (аудит 41): «GET /history?limit=0 = весь лог мира».

Было: `db._get_history_page` на «нет лимита» отправлял в SQL `LIMIT 100000`, т.е.
`GET /history` (он же `?limit=0`) вытягивал ВСЁ прохождение в один JSON-ответ и в память —
тот же класс A14/B5, что уже закрыли для `/events` (`_EVENTS_PAGE_LIMIT = 500`).

Стало: единый потолок страницы в слое данных (`db.HISTORY_PAGE_DEFAULT` / `HISTORY_PAGE_MAX`)
и честный флаг `truncated` («раньше показанного ещё есть»). Форма ответа —
`{"events": [...], "truncated": bool}` (как у `/events`); фронт (`loadEarlier`) синхронизирован
и по-прежнему принимает голый список, чтобы ответ старой формы не ронял пагинацию.

Лишняя строка вычитается одним `LIMIT n+1` в SQL — без COUNT по всему логу.

Тесты — на ПОВЕДЕНИЕ (правило 19): живая тестовая БД, реальные HTTP-ответы, настоящий JS на
Node. Текст исходников не читаем; сверка константы размера страницы фронта и серверного
потолка — тоже поведение, а не grep.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

import backend.db as db_mod

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
sys.path.insert(0, str(ROOT / "scripts"))

# размер страницы, о котором договорились фронт и бэкенд (см. frontend/app.js::LOG_PAGE_SIZE)
_m = re.search(r"^const LOG_PAGE_SIZE = (\d+);$", APP_JS, re.M)
assert _m, "в app.js пропало объявление LOG_PAGE_SIZE — фронтовая страница не находится"
FRONT_PAGE_SIZE = int(_m.group(1))
_PAGE_DECL = _m.group(0)          # берём ИЗ app.js, иначе тест гонял бы свою заглушку


def _plain_world() -> int:
    """Мир прямо в БД (без API/LLM): ни одного лишнего события, считать страницу честно."""
    return db_mod.create_world(
        "тест", "custom", "фэнтези", "normal", "second", "ru", "",
        {"player": {"hp": 50, "max_hp": 50, "stats": {}, "inventory": []},
         "locations": {"start": {"name": "Привал"}}, "npc": {}, "quests": {}, "flags": {}},
        {})


def _fill(wid: int, n: int, role: str = "system", start: int = 1) -> None:
    for i in range(n):
        db_mod.add_event(wid, role, f"событие {start + i}", seq=start + i)


def _mk_world(client, name="Лог"):
    r = client.post("/api/worlds", json={"theme_id": "custom", "name": name,
                                         "custom_plot": "сюжет", "genres": []})
    assert r.status_code == 200, r.text
    return r.json()["world_id"]


@pytest.fixture
def wid(api_client):
    return _plain_world()


# ══════════════════ слой данных: потолок + флаг ══════════════════

def test_history_page_is_capped_without_limit(wid):
    """«Нет лимита» ≠ «весь лог»: без limit приходит страница дефолтного размера."""
    total = db_mod.HISTORY_PAGE_DEFAULT + 40
    _fill(wid, total)
    evs, truncated = db_mod.get_history_page(wid)
    assert len(evs) == db_mod.HISTORY_PAGE_DEFAULT, "страница не ограничена дефолтом"
    assert truncated is True, "при остатке лога обязан быть честный truncated"
    # режем ХВОСТ (самые поздние), порядок внутри страницы — хронологический
    assert [e["seq"] for e in evs[:2]] == [41, 42] and evs[-1]["seq"] == total


def test_history_page_limit_above_ceiling_is_clamped(wid):
    """Гигантский limit (бывший «весь лог») резуется потолком — и это видно по флагу."""
    _fill(wid, db_mod.HISTORY_PAGE_MAX + 5)
    evs, truncated = db_mod.get_history_page(wid, limit=10 ** 9)
    assert len(evs) == db_mod.HISTORY_PAGE_MAX
    assert truncated is True


def test_history_page_flag_only_when_something_is_left(wid):
    """Смысл флага — «дальше есть что листать», а не «ответ непустой»."""
    _fill(wid, 5)
    evs, truncated = db_mod.get_history_page(wid, limit=50)
    assert len(evs) == 5 and truncated is False
    # limit ровно по остатку — лишней строки в выборке нет, значит не подрезано
    assert db_mod.get_history_page(wid, limit=5) == (evs, False)
    # limit меньше остатка → страница режется, и клиент это узнаёт
    evs3, trunc3 = db_mod.get_history_page(wid, limit=2)
    assert [e["seq"] for e in evs3] == [4, 5] and trunc3 is True
    # долистываем до начала: последних двух нет, ранние — все, флаг гаснет
    evs4, trunc4 = db_mod.get_history_page(wid, before_seq=3, limit=50)
    assert [e["seq"] for e in evs4] == [1, 2] and trunc4 is False


def test_history_page_roles_counted_in_sql(wid):
    """Служебные роли не «весят» в странице и не создают ложного truncated (A10 + A8)."""
    _fill(wid, 10, role="summary")                      # невидимые чату строки
    _fill(wid, 3, role="player", start=11)              # видимые
    evs, truncated = db_mod.get_history_page(wid, limit=50, roles=("player",))
    assert [e["seq"] for e in evs] == [11, 12, 13]
    assert truncated is False, "подрезка служебными ролями — ложный флаг"
    # но если видимых БОЛЬШЕ страницы — флаг обязан появиться (считается после фильтра в SQL)
    _fill(wid, 5, role="player", start=14)
    assert db_mod.get_history_page(wid, limit=2, roles=("player",))[1] is True


# ══════════════════════ HTTP: форма ответа ══════════════════════

def test_history_endpoint_shape(api_client):
    """GET /history отдаёт {events, truncated} — фронт читает поля, а не голый список."""
    client, _ = api_client
    wid = _mk_world(client)
    _fill(wid, 3, start=100)
    r = client.get(f"/api/worlds/{wid}/history")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, dict) and {"events", "truncated"} <= set(body), \
        "ответ сменил форму: фронт не найдёт события"
    assert isinstance(body["events"], list) and body["truncated"] is False


def test_history_endpoint_is_bounded_and_reports_truncation(api_client):
    """Образец test_events_poll_is_bounded_and_reports_truncation — теперь для /history.

    `?limit=0` (он же дефолт) не имеет права возвращать весь лог мира."""
    client, _ = api_client
    wid = _mk_world(client)
    total = db_mod.HISTORY_PAGE_MAX + 30
    _fill(wid, total, start=100)
    for q in ("", "?limit=0", f"?limit={total}", "?limit=999999"):
        body = client.get(f"/api/worlds/{wid}/history{q}").json()
        assert len(body["events"]) <= db_mod.HISTORY_PAGE_MAX, f"{q}: страница не ограничена"
        assert body["truncated"] is True, f"{q}: лог обрезан, а флаг молчит"
        # страница — САМЫЕ поздние события (иначе лог не читается снизу вверх)
        assert body["events"][-1]["seq"] == 99 + total, f"{q}: отдан не хвост лога"
    # пагинация назад: первая полная страница и шаг до начала — флаг честно гаснет
    head = client.get(f"/api/worlds/{wid}/history?limit=0").json()["events"][0]["seq"]
    page = client.get(f"/api/worlds/{wid}/history",
                      params={"before": head, "limit": 500}).json()
    assert page["events"] and page["truncated"] is False, "ранних событий осталось меньше страницы"


def test_history_roles_filter_kept(api_client):
    """A10 не поехал: сводки по-прежнему не попадают в /history (и не портят флаг)."""
    client, _ = api_client
    wid = _mk_world(client)
    _fill(wid, 5, role="summary", start=200)
    _fill(wid, 2, role="player", start=206)
    body = client.get(f"/api/worlds/{wid}/history").json()
    assert all(e["role"] != "summary" for e in body["events"]), "сводки вернулись в лог игрока"
    assert body["truncated"] is False, "невидимые роли снова считаются в лимите"


# ══════════════ фронт: настоящий JS loadEarlier на Node ══════════════

def _fn_src(name: str) -> str:
    from check_frontend import _extract_fn
    body = _extract_fn(APP_JS, name)
    if f"async function {name}(" in APP_JS and not body.startswith("async"):
        body = "async " + body
    return body


def _run_node(snippet: str, marker: str) -> None:
    if subprocess.run(["node", "--version"], capture_output=True, text=True).returncode != 0:
        pytest.skip("node недоступен")
    tmp = ROOT / "_s48_history_probe.cjs"
    tmp.write_text(snippet, encoding="utf-8")
    try:
        r = subprocess.run(["node", str(tmp)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
    finally:
        tmp.unlink(missing_ok=True)
    out = (r.stdout or "") + (r.stderr or "")
    assert marker in out, out[:2000]


def test_frontend_page_size_fits_server_ceiling():
    """Фронт не просит больше серверного потолка: иначе truncated=True на каждой кнопке."""
    assert 0 < FRONT_PAGE_SIZE <= db_mod.HISTORY_PAGE_MAX, \
        f"LOG_PAGE_SIZE={FRONT_PAGE_SIZE}, потолок = {db_mod.HISTORY_PAGE_MAX}"
    assert FRONT_PAGE_SIZE <= 100, f"страница {FRONT_PAGE_SIZE} раздувает ответ кнопки «раньше»"


_HARNESS = r"""
/* заглушки DOM/сети: сервер отдаёт страницу разной формы, считаем что вставлено */
function mkBtn() { return { disabled: false, textContent: "", parentNode: { removeChild() {} } }; }
let btn = mkBtn();
let logPagerBtn = null;
let responses = [], calls = 0, inserted = [], pagerShown = [], queries = [];
const state = { currentWorld: 1, logMinSeq: 20, loadingEarlier: false };
function API(url) { calls++; queries.push(url); return Promise.resolve(responses.shift()); }
function buildMsg(e) { inserted.push(e.seq); return {}; }
function showLogPager(show) { pagerShown.push(show); }
const _log = { firstChild: null, nextSibling: null, scrollHeight: 100, scrollTop: 0,
               insertBefore() {}, prepend() {} };
function $(id) { return id === "log" ? _log : (id === "btn-load-earlier" ? btn : null); }
global.document = { createElement: () => ({ innerHTML: "", nextSibling: null,
                           querySelector: () => btn,
                           set className(v) {}, set id(v) {} }) };
const PAGE = LOG_PAGE_SIZE;   // объявление — строкой ниже, ОДНА величина с продом

(async () => {
  let bad = [];

  // 1. новая форма ответа {events, truncated}: страница вставлена, курсор сдвинут,
  //    кнопка не спрятана (выше ещё есть что листать)
  inserted = []; pagerShown = []; state.logMinSeq = 20;
  responses = [{ events: [{ seq: 17 }, { seq: 18 }, { seq: 19 }], truncated: true }];
  if (!await loadEarlier()) bad.push("1: false при непустой странице");
  if (state.logMinSeq !== 17) bad.push("1: курсор не сдвинут: " + state.logMinSeq);
  if (inserted.filter((x) => typeof x === "number").join() !== "17,18,19")
    bad.push("1: вставка неверна: " + inserted.join());
  if (pagerShown.length) bad.push("1: кнопку прячут раньше времени");

  // 2. пустая страница — кнопка прячется, результат false
  pagerShown = []; state.logMinSeq = 5;
  responses = [{ events: [], truncated: false }];
  if (await loadEarlier()) bad.push("2: true при пустой странице");
  if (pagerShown[0] !== false) bad.push("2: кнопка не спрятана: " + JSON.stringify(pagerShown));

  // 3. ответ старой формы (голый список) обязан читаться — фронт не падает
  inserted = []; state.logMinSeq = 10;
  responses = [[{ seq: 8 }, { seq: 9 }]];
  if (!await loadEarlier()) bad.push("3: старый формат не читается");
  if (inserted.filter((x) => typeof x === "number").join() !== "8,9" || state.logMinSeq !== 8)
    bad.push("3: " + inserted.join() + " min=" + state.logMinSeq);

  // 4. мусор вместо ответа (null) — false, а не исключение наружу
  state.logMinSeq = 30;
  responses = [null];
  if (await loadEarlier()) bad.push("4: null-ответ дал true");
  if (calls !== 4) bad.push("4: запросов должно быть 4, есть " + calls);

  // 5. в запросе — ограниченный limit и курсор before (не «весь лог»)
  for (const q of queries) {
    if (!new RegExp("history\\?before=\\d+&limit=" + PAGE + "$").test(q))
      bad.push("5: запрос без ограниченной страницы: " + q);
  }

  console.log(bad.length ? "BAD " + bad.join(" | ") : "OK s48");
})();
"""

_SNIPPET = (_fn_src("loadEarlier") + "\n" + _PAGE_DECL + "\n" + _HARNESS)


def test_load_earlier_real_js():
    """Прогон НАСТОЯЩЕЙ loadEarlier на Node: новая форма /history не ломает пагинацию."""
    _run_node(_SNIPPET, "OK s48")
