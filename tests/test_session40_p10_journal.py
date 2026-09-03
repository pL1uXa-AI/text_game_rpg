# -*- coding: utf-8 -*-
"""Сессия 40, п.10 (мир «Новый мир»): дневник перестал быть тупиком «…нажми ⬆ Показать ранние события».

Симптом: клик по ЛЮБОЙ записи вкладки «Дневник» выдавал
`Событие хода N не в загруженной части лога. Поднимись в начало и нажми «⬆ Показать ранние
события».` — то есть переход в прошлое работал только руками игрока, а в мире длиннее
страницы лога (в чат при открытии грузится хвост на 60 событий, `worlds.py::world_detail`)
— никогда. Плюс тот же тупик давали ходы, удалённые перемоткой/`↻`, и записи без привязки
к ходу (seq=0 → «хода 0»).

Правка чисто фронтовая (`scrollToSeq`): сам догружает ранние страницы (`loadEarlier`),
а если точного хода в логе нет — ведёт к ближайшему сохранившемуся и честно объясняет
причину. `loadEarlier` теперь возвращает, удалось ли догрузить (иначе циклу не понять,
что дальше пусто).

Законы: только отображение/навигация (закон 2), ничего не решаем за мастера (закон 3),
никаких жанровых слов (закон 1).
"""
from __future__ import annotations

import re as _re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
sys.path.insert(0, str(ROOT / "scripts"))


def _fn_src(name: str) -> str:
    """Тело JS-функции из app.js. `async` возвращаем вместе с телом: без него `await`
    внутри функции — синтаксическая ошибка, и Node падал бы на заглушке, а не на коде."""
    from check_frontend import _extract_fn
    body = _extract_fn(APP_JS, name)
    if f"async function {name}(" in APP_JS and not body.startswith("async"):
        body = "async " + body
    return body


def _run_node(snippet: str, marker: str) -> None:
    if subprocess.run(["node", "--version"], capture_output=True, text=True).returncode != 0:
        pytest.skip("node недоступен")
    tmp = ROOT / "_s40_p10_probe.cjs"
    tmp.write_text(snippet, encoding="utf-8")
    try:
        r = subprocess.run(["node", str(tmp)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
    finally:
        tmp.unlink(missing_ok=True)
    out = (r.stdout or "") + (r.stderr or "")
    assert marker in out, out[:2000]


# ── структура: мёртвого тупика больше нет ─────────────────────────────

def test_scroll_to_seq_no_longer_sends_player_to_the_button():
    assert "не в загруженной части лога" not in APP_JS, \
        "в UI вернулся текст-тупик: переход обязан догружать сам"
    fn = _fn_src("scrollToSeq")
    assert "loadEarlier(" in fn, "scrollToSeq больше не догружает ранние события"
    assert "_nearestSeqMsg(" in fn, "нет перехода к ближайшему сохранившемуся ходу"
    assert "async function scrollToSeq" in APP_JS, "scrollToSeq обязан быть async (ждёт догрузку)"


def test_load_earlier_reports_whether_it_loaded_more():
    """loadEarlier возвращает true/false — иначе цикл догрузки не отличит «пусто» от «ок»."""
    fn = _fn_src("loadEarlier")
    assert "return false" in fn and "return true" in fn, \
        f"loadEarlier не сообщает результат:\n{fn[:400]}"


# ── поведение на НАСТОЯЩЕМ JS (Node) ─────────────────────────────────

_JS_HARNESS = """
/* заглушки DOM/сети: документ с сообщениями, loadEarlier догружает страницы */
let msgs = [];                       // [{seq}]
let alerts = [];
let pages = []; calls = 0;               // очереди «ранних» событий, что отдаёт сервер

function mkMsg(seq) { return { dataset: { seq: String(seq) }, classList: { add() {} },
                              scrollIntoView() {} }; }
global.document = {
  querySelector: (sel) => {
    const m = sel.match(/^\\.msg\\[data-seq="(-?\\d+)"\\]$/);
    if (!m) return null;
    return msgs.find((x) => x.dataset.seq === m[1]) || null;
  },
  querySelectorAll: (sel) => (sel === ".msg[data-seq]" ? msgs : []),
};
global.alert = (t) => alerts.push(t);
const state = { currentWorld: 1, logMinSeq: 0 };
async function loadEarlier() {
  calls++;
  if (pages === "stuck") { state.logMinSeq = 9999; return true; }   // сервер вечно отдаёт то же
  const page = pages.shift();
  if (!page || !page.length) return false;
  page.forEach((s) => msgs.push(mkMsg(s)));
  msgs.sort((a, b) => +a.dataset.seq - +b.dataset.seq);
  state.logMinSeq = page[0];
  return true;
}
"""

# лимит страниц догрузки живёт ОТДЕЛЬНЫМ объявлением в app.js — берём его оттуда, иначе
# тест проверял бы не тот код, что в проде.
_m = _re.search(r"^const _SEQ_JUMP_MAX_PAGES = \d+;$", APP_JS, _re.M)
# нет объявления — snippet соберётся с ReferenceError, тест упадёт (молча не зелёный)
_PAGES_CONST = _m.group(0) if _m else ""

_SNIPPET = (_fn_src("scrollToSeq") + "\n" + _fn_src("_focusSeqMsg") + "\n"
            + _fn_src("_nearestSeqMsg") + "\n" + _PAGES_CONST + "\n" + _JS_HARNESS + """
// порядок вызовов важен: функции объявлены выше, заглушки — до прогонов
let bad = [];
function reset(list, early, firstSeq) {
  msgs = list.map(mkMsg);
  pages = (early === "stuck") ? "stuck" : early.map((p) => p.slice());
  state.logMinSeq = firstSeq; alerts = []; calls = 0;
}

(async () => {
  // 1. ход уже на экране — скролл без единого запроса к истории
  reset([5, 6, 7], [], 5);
  await scrollToSeq(6);
  if (pages.length || alerts.length) bad.push("пункт 1: лишний догруз/алерт: " + JSON.stringify(alerts));

  // 2. живой случай мира №103: запись «ход 6», в чате только хвост от 20 — догружаем сами
  reset([20, 21, 22, 23], [[14, 15, 16, 17, 18, 19], [6, 7, 8, 9, 10, 11]], 20);
  await scrollToSeq(6);
  if (alerts.length) bad.push("пункт 2: догрузка не сработала, алерты: " + JSON.stringify(alerts));

  // 3. хода нет в истории вовсе (раньше это было «поднимись и нажми кнопку» в никуда) —
  //    показываем ближайший сохранившийся и объясняем
  reset([20, 21, 22], [[]], 20);
  await scrollToSeq(31);
  if (!alerts.length || !/ближайший/i.test(alerts[0])) {
    bad.push("пункт 3: нет честного сообщения о ближайшем ходе: " + JSON.stringify(alerts));
  }
  if (alerts.length && /Поднимись|ранние события/.test(alerts[0])) {
    bad.push("пункт 3: вернулся текст-тупик: " + alerts[0]);
  }

  // 4. запись без привязки к ходу (seq=0) — не «хода 0», а внятная причина
  reset([1, 2, 3], [], 1);
  await scrollToSeq(0);
  if (!alerts.length || /хода 0/.test(alerts[0])) {
    bad.push("пункт 4: плохая реакция на seq=0: " + JSON.stringify(alerts));
  }

  // 5. сервер, который вечно отдаёт «есть ещё раньше», не должен зациклить переход
  reset([100], "stuck", 100);
  await scrollToSeq(1);
  if (calls > 41) bad.push("пункт 5: догрузка не ограничена по числу страниц (вызовов " + calls + ")");
  if (!alerts.length) bad.push("пункт 5: при пустом результате должен быть ответ пользователю");

  console.log(bad.length ? "BAD " + bad.join(" | ") : "OK p10");
})();
""")


def test_scroll_to_seq_real_js():
    """Прогон НАСТОЯЩИХ scrollToSeq/_focusSeqMsg/_nearestSeqMsg на Node."""
    _run_node(_SNIPPET, "OK p10")


def test_journal_entries_carry_jumpable_seq():
    """Записи дневника обязаны нести seq того хода, который есть в чате (roles-фильтр тот же).

    Дневник ссылается на seq действия игрока; в чат события игрока приходят и при
    открытии мира (`CHAT_LOG_ROLES` включает `player`) — иначе переход в принципе невозможен.
    """
    src = (ROOT / "backend" / "routers" / "core.py").read_text(encoding="utf-8")
    assert 'CHAT_LOG_ROLES: tuple[str, ...] = ("player"' in src, \
        "из чата исчезла роль player — ссылки дневника на ходы игрока стали битыми"
