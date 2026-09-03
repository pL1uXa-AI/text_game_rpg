"""Регрессии сессии 40 (баг-хант мира «Новый мир»).

П.4 — «цвет текста рассказчика сливается с фоном, читать трудно; сначала печатается
нормально, а потом сообщение удаляется и появляется в другом виде».

Причина: аудит 38 (E2) поменял в `frontend/app.js::msgClass` дефолт «рисовать как
рассказчика» на новую роль `unknown`, но явную ветку `narrator` не добавил. В итоге
живой стрим (`.msg.narrator typing`) печатался нормальным стилем, а по окончании хода
`handleActionResult` перерисовывал то же сообщение классом `.msg.unknown` — серый
(#9aa), 13px, пунктирная рамка. Отсюда и «сливается с фоном», и «исчез/появился другим».

П.5 — «„Вы #“ пишется без номера, пока не обновишь страницу».

Причина: сообщение игрока рисуется оптимистично (сразу, до ответа сервера) и не знает
ни id, ни seq события; при этом `<span>#</span>` рисовался ВСЕГДА — даже с пустым seq.
Ответ сервера содержал настоящее player-событие, но `handleActionResult` с `skipPlayer`
его отбрасывал, а дедюп в `appendMsg` не помог бы: локальному пузырю выдан временный
id `local-N`. Номер появлялся только после перезагрузки страницы. Лечится «усыновлением»
пузыря: `adoptPlayer()` подставляет в уже нарисованный элемент реальные id/seq и номер.

П.6 — «кнопка „🧭 рядом:“ выдаёт системное название („Идти в path_to_architects“)».

Причина: `renderCompass` шёл в обход общей формулировки перехода — в текст действия
игрока попадал Внутренний id локации (`data-loc`), тогда как карта и быстрые действия
слали человекочитаемое имя («Я иду в «Тропа к Форпосту».»). Модель описывала переход по
машинному имени, и оно оставалось в чате навсегда. Лечится единым хелпером
`travelActionText(name)` + `data-name` на кнопке; висячие рёбра (нет имени ни в
`setting.locations`, ни в подписях графа) компасом больше не показываются.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "frontend" / "style.css").read_text(encoding="utf-8")

sys.path.insert(0, str(ROOT / "scripts"))


def _fn_src(name: str) -> str:
    """Тело JS-функции из app.js (по балансу скобок) — общий помощник чекера фронта."""
    from check_frontend import _extract_fn
    return _extract_fn(APP_JS, name)


def _run_node(snippet: str, marker: str) -> None:
    if subprocess.run(["node", "--version"], capture_output=True, text=True).returncode != 0:
        pytest.skip("node недоступен")
    tmp = ROOT / "_s40_probe.cjs"
    tmp.write_text(snippet, encoding="utf-8")
    try:
        r = subprocess.run(["node", str(tmp)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
    finally:
        tmp.unlink(missing_ok=True)
    out = (r.stdout or "") + (r.stderr or "")
    assert marker in out, out[:1500]


# ════════════════ П.4: стиль рассказчика ════════════════

def _msg_class_body() -> str:
    fn = APP_JS[APP_JS.index("function msgClass("):]
    return fn[: fn.index("\n}\n")]


def test_msgclass_maps_narrator_explicitly():
    """Роль narrator обязана возвращать класс narrator (а не проваливаться в unknown)."""
    body = _msg_class_body()
    assert re.search(r'role === "narrator"\)\s*return "narrator"', body), (
        f"msgClass потерял ветку narrator — рассказчик снова рисуется как unknown:\n{body}"
    )
    assert 'return "unknown"' in body, "unknown-фолбэк для незнакомых ролей (E2) должен сохраниться"


def test_narrator_style_is_readable_not_muted():
    """Стиль рассказчика — обычный текст, сплошная рамка (не приглушённый unknown)."""
    rule = STYLE_CSS[STYLE_CSS.index(".msg.narrator .body"):].split("\n")[0]
    assert "var(--bg3)" in rule, f"фон сообщения рассказчика потерян: {rule}"
    for bad in ("italic", "dashed", "--muted"):
        assert bad not in rule, f"стиль рассказчика снова «нечитаемый» ({bad}): {rule}"
    # незнакомые роли обязаны визуально отличаться от рассказчика (решение E2, аудит 38)
    unknown = STYLE_CSS[STYLE_CSS.index(".msg.unknown .body"):].split("\n")[0]
    assert "--muted" in unknown, "unknown-роль должна отличаться от рассказчика (E2)"


# ════════════════ П.5: номер «#» у хода игрока ════════════════

def test_buildmsg_hides_empty_seq():
    """Пустой seq больше не рисуется как голый «#» (был мусор до ответа сервера)."""
    fn = APP_JS[APP_JS.index("function buildMsg("):]
    fn = fn[: fn.index("\n}\n")]
    assert re.search(r"e\.seq === 0 \|\| e\.seq", fn), (
        f"buildMsg снова печатает «#» при пустом seq:\n{fn[:600]}"
    )


def test_action_result_adopts_player_event():
    """handleActionResult обязан усыновить оптимистичный пузырь, а не выбросить событие."""
    fn = APP_JS[APP_JS.index("function handleActionResult("):]
    fn = fn[: fn.index("\n}\n")]
    assert "adoptPlayer(" in fn, "handleActionResult не проставляет id/seq пузырю игрока"
    # усыновление — ДО фильтра skipPlayer, иначе событие игрока уже отброшено
    assert fn.index("adoptPlayer(") < fn.index("if (skipPlayer)"), \
        "adoptPlayer вызывается после skipPlayer-фильтра — номера снова не будет"


def test_adoptplayer_real_js():
    """Прогон НАСТОЯЩЕГО adoptPlayer() на Node: пузырь получает id/seq и номер «#N».

    Функция берётся из исходника (не копия), чтобы тест ловил регрессии в коде фронта.
    """
    snippet = _fn_src("adoptPlayer") + """
const state = { seenSeq: 0 };
function el(id, text, local) {
  const who = { children: [], querySelector: () => null, appendChild(c) { this.children.push(c); } };
  const node = { dataset: id ? { id } : {}, _who: who,
    querySelector: (s) => s === ".body" ? { textContent: text } : (s === ".who" ? who : null) };
  if (local) node.dataset.local = "1";
  return node;
}
function makeDoc(nodes) {
  return { querySelectorAll: (sel) => sel.includes('data-id^="local-"')
      ? nodes.filter((n) => String(n.dataset.id || "").startsWith("local-")
                         && !(sel.includes(":not([data-local])") && n.dataset.local)) : [],
      createElement: () => ({ className: "", textContent: "" }) };
}
let bad = [];
// основной путь: номер и id берутся из серверного события
const bubbles = [el("local-1", "Идти в лес"), el("local-2", "Осмотреться")];
global.document = makeDoc(bubbles);
if (!adoptPlayer({ id: 771, seq: 43, role: "player", content: "Осмотреться" })) bad.push("вернул false");
const got = bubbles[1], span = got._who.children[0];
if (got.dataset.id !== "771") bad.push("id не подставлен: " + got.dataset.id);
if (got.dataset.seq !== "43") bad.push("seq не подставлен: " + got.dataset.seq);
if (!span || span.textContent !== "#43") bad.push("номер «#43» не нарисован");
if (state.seenSeq !== 43) bad.push("seenSeq не обновлён: " + state.seenSeq);
// совпадение по тексту устойчиво к нормализации пробелов
global.document = makeDoc([el("local-3", "Прыгнуть    в     воду")]);
if (!adoptPlayer({ id: 6, seq: 6, content: "Прыгнуть в воду" })) bad.push("не нормализует пробелы");
// пузыри слэш-команд (data-local) не усыновляются
global.document = makeDoc([el("local-4", "Прыгнуть", true)]);
if (adoptPlayer({ id: 8, seq: 8, content: "Прыгнуть" })) bad.push("усыновил пузырь слэш-команды");
// единственный пузырь без точного совпадения — страховка (сервер срезал <<ENGINE>>)
global.document = makeDoc([el("local-6", "Съесть кристалл, для эксперимента")]);
if (!adoptPlayer({ id: 9, seq: 9, content: "Съесть кристалл" })) bad.push("страховка не сработала");
// а из нескольких неугаданных — не тянем никого
global.document = makeDoc([el("local-7", "A"), el("local-8", "B")]);
if (adoptPlayer({ id: 10, seq: 10, content: "C" })) bad.push("угадал пузырь из двух");
if (adoptPlayer({ id: 11, seq: 11, content: "" })) bad.push("пустой content усыновил");
console.log(bad.length ? "BAD " + JSON.stringify(bad) : "ADOPT_OK");
"""
    _run_node(snippet, "ADOPT_OK")


# ════════════ П.6: «рядом:» слал системный id локации ════════════

def test_travel_action_text_helper_exists_and_is_human():
    """Единый хелпер формулировки перехода: только имя, никаких id."""
    fn = _fn_src("travelActionText")
    assert "Я иду в «" in fn, f"хелпер не строит человекочитаемую фразу:\n{fn}"
    assert "data-loc" not in fn and "dataset" not in fn, "хелпер не должен знать про id"
    # все пути перехода пользуются им, а не строят фразу на месте
    assert APP_JS.count("travelActionText(") >= 4, \
        "компас/карта/быстрые действия снова расходятся в формулировках"
    assert not re.search(r"(sendAsAction|quickAction)\([^)]*Идти в \$\{", APP_JS), \
        "в код вернулась отправка действия «Идти в <id>» (системное имя в чате)"
    # шаблон фразы обязан остаться единственным — внутри хелпера
    assert APP_JS.count("Я иду в «") == 1, (
        "где-то кроме travelActionText снова появился свой шаблон перехода — пути разойдутся"
    )


def test_compass_button_carries_name_and_sends_it():
    """Кнопка компаса несёт data-name и шлёт имя, а не data-loc."""
    fn = APP_JS[APP_JS.index("function renderCompass("):]
    fn = fn[: fn.index("\n}\n")]
    assert 'data-name="${esc(it.name)}"' in fn, "на кнопке компаса нет человекочитаемого имени"
    assert "travelActionText(b.dataset.name)" in fn, "компас шлёт не имя локации"
    assert "sendAsAction(`Идти в" not in fn, "компас снова отправляет системный id"
    # имя ищется по подписи графа, а висячие рёбра (без имени) не показываются
    assert "labels[id]" in fn and "known(" in fn, "компас может показать сырой id локации"


def test_compass_name_resolution_real_js():
    """Разрешение имени цели — на СТРОКАХ из renderCompass и данных мира №103.

    Строки `niceName`/`known` вырезаются из исходника (а не переписываются в тесте),
    иначе тест проверки не проверяет. Дальше — реальные id/имена из мира «Новый мир»."""
    fn = APP_JS[APP_JS.index("function renderCompass("):]
    fn = fn[: fn.index("\n}\n")]
    lines = [ln.strip() for ln in fn.splitlines() if ln.strip().startswith("const niceName =")
             or ln.strip().startswith("const known =")]
    assert len(lines) == 2, f"в renderCompass не нашлись niceName/known: {lines}"
    snippet = _fn_src("esc") + "\n" + _fn_src("travelActionText") + "\n" + "\n".join(lines) + """
const locs = { path_to_architects: { name: "Тропа к Форпосту" },
               architects_outpost: { name: "Форпост Кристалл" },
               nameless_room: {} };
const labels = { aeterna_ruins: "Руины Аэтерны (Второй Ярус)" };
const isLoc = new Set(["path_to_architects", "aeterna_ruins", "architects_outpost", "nameless_room"]);
let bad = [];
if (niceName("path_to_architects") !== "Тропа к Форпосту") bad.push("имя из setting не взято");
if (niceName("aeterna_ruins") !== "Руины Аэтерны (Второй Ярус)") bad.push("имя из графа не взято");
// висячее ребро (локации нет ни в setting, ни локацией в графе) — не кнопка
if (known("ghost_id")) bad.push("висячий id прошёл как известная локация");
// безымянная, но СУЩЕСТВУЮЩАЯ локация остаётся видимой (move валидируется по id)
if (!known("nameless_room")) bad.push("безымянная локация пропала из компаса");
const phrase = travelActionText(niceName("path_to_architects"));
if (phrase !== "Я иду в «Тропа к Форпосту».") bad.push("формулировка: " + phrase);
if (travelActionText("  Тропа   к   Форпосту ") !== "Я иду в «Тропа к Форпосту».") bad.push("пробелы");
if (travelActionText("") !== "" || travelActionText(null) !== "") bad.push("пустое имя дало фразу");
console.log(bad.length ? "BAD " + JSON.stringify(bad) : "COMPASS_OK");
"""
    _run_node(snippet, "COMPASS_OK")


# ════════ П.7: бессрочный эффект со сроком в описании ════════

EFF_DESC = ("Энергия кристалла вплетается в родовой чертёж, снижая риск разрыва. "
            "Требуется ещё 1-2 сессии медитации для полного закрепления.")


def test_duration_hint_detects_spoken_deadline():
    """`_duration_hint` замечает срок/условие снятия в desc — но не выдумывает их."""
    from backend.mechanics import _duration_hint
    assert _duration_hint(EFF_DESC), "«Требуется ещё 1-2 сессии медитации» не замечено"
    for txt in ("нужно ещё 2 хода, чтобы ритуал завершился", "спадёт через 3 дня",
                "держится 1–2 часа", "для завершения требуется вторая медитация",
                "после 5 шагов утихнет"):
        assert _duration_hint(txt), f"срок не замечен в: {txt}"
    # обычные (бессрочные по смыслу) описания триггерить НЕ должны
    for txt in ("", "яд в крови", "+10% к опыту за квесты",
                "энергия Порядок рвёт узор изнутри: -30 HP, -20 MP за ход",
                "ты восстановил древний канал, возможно это откроет новые пути"):
        assert not _duration_hint(txt), f"ложный срок в: {txt}"


def test_effect_add_warns_when_permanent_but_timed():
    """effect_add без turns при сроке в desc -> системка-подсказка (сам движок срок
    не назначает: закон 3, решает мастер)."""
    from backend.mechanics import apply_directives
    setting = {"player": {"effects": {}}}
    msgs = apply_directives(setting, {"effect_add": {"name": "Стабилизация родового канала",
                                                     "desc": EFF_DESC}})
    assert any("бессрочен" in m and "turns" in m for m in msgs), \
        f"нет предупреждения о бессрочности:\n{msgs}"
    assert setting["player"]["effects"]["Стабилизация родового канала"]["turns"] == -1, \
        "движок самовольно поставил turns (закон 3 нарушен)"
    # с явным turns предупреждения нет
    msgs2 = apply_directives({"player": {"effects": {}}},
                             {"effect_add": {"name": "Стаб", "turns": 2, "desc": EFF_DESC}})
    assert not any("бессрочен" in m for m in msgs2), f"ложная подсказка при turns=2: {msgs2}"


def test_readd_with_turns_fixes_permanent_effect():
    """Подсказка исполнима: повторный effect_add с turns переводит бессрочный эффект
    в срок (иначе системка звала бы в пустоту)."""
    from backend.mechanics import apply_directives
    setting = {"player": {"effects": {}}}
    apply_directives(setting, {"effect_add": {"name": "Стаб", "desc": EFF_DESC}})
    assert setting["player"]["effects"]["Стаб"]["turns"] == -1
    apply_directives(setting, {"effect_add": {"name": "Стаб", "turns": 2, "desc": EFF_DESC}})
    assert setting["player"]["effects"]["Стаб"]["turns"] == 2, "срок не применился"


def test_format_state_shows_full_effect_deadline():
    """`format_state` отдаёт desc эффекта ЦЕЛИКОМ: срок доходит до модели."""
    from backend.mechanics import desc_compact
    # лимита по умолчанию нет — полный текст без «…»
    assert desc_compact(EFF_DESC) == EFF_DESC, "по умолчанию desc снова ужимается"
    assert "…" not in desc_compact(EFF_DESC), "в полном описании появился многоточие"
    line = desc_compact(EFF_DESC, 120)
    assert "1-2 сессии медитации" in line, f"срок отрезан: {line}"
    assert desc_compact("", 90) == ""
    assert desc_compact("короткое", 90) == "короткое"
    long = "x" * 200
    cut = desc_compact(long, 90)
    assert len(cut) <= 91 and "…" in cut, f"сжатие не работает: {len(cut)}"
    assert cut.startswith("x" * 50) and cut.endswith("x" * 20), "остаются начало И конец"


def test_format_state_marks_permanent_timed_effect():
    """Живой мир: форматирование состояния подсказывает мастеру про бессрочный срок."""
    from backend import narrator as n
    setting = {"player": {"name": "Т", "hp": 50, "max_hp": 50, "mp": 10, "max_mp": 10,
                          "effects": {"Стабилизация родового канала":
                                      {"turns": -1, "kind": "особый", "desc": EFF_DESC}}}}
    out = n.format_state(setting)
    line = next((ln for ln in out.splitlines() if ln.startswith("Эффекты:")), "")
    assert EFF_DESC in line, f"описание дошло не полностью:\n{line}"
    assert "⚠" in line, f"нет пометки о бессрочности:\n{line}"


def test_prompt_rule_requires_turns_for_timed_effects():
    """В промпте рассказчика есть прямое правило: срок в тексте = turns числом.

    Формулировку правила может сократить любой будущий аудит бюджета промпта
    (`test_build_messages_never_overflows_window`), поэтому проверяем смысл, а не цитату."""
    src = (ROOT / "backend" / "narrator.py").read_text(encoding="utf-8")
    m = re.search(r"^16\. Эффекты.*$", src, re.M)
    assert m, "правило 16 про эффекты исчезло из промпта"
    rule = m.group(0)
    assert "desc" in rule and "turns" in rule, f"правило 16 разучилось требовать turns:\n{rule}"
    assert re.search(r"бессмертен|бессрочно|задавай|задал", rule), \
        f"из правила 16 пропала связь «срок в desc → turns»:\n{rule}"


# ── п.8 (мир «Новый мир»): дубль эффекта из-за имени-синонима ──────────


def test_same_effect_name_pairs_dupes_but_not_distinct_states():
    """`_same_effect_name` ловит синонимы мира №103 и НЕ склеивает разные состояния."""
    from backend.mechanics import _same_effect_name as same
    # живой случай: три имени одного процесса стабилизации узора
    assert same("Стабилизация фрактала", "Стабилизация родового канала")
    assert same("Стабилизация фрактала", "Стабилизация узора")
    assert same("Стабилизация родового канала", "Стабилизация родового канала")
    # разные состояния, у которых просто похожее имя, склеивать нельзя
    for a, b in (("Ледяная броня", "Ледяной щит"),
                 ("Ярость берсерка", "Покой берсерка"),
                 ("Благословение луны", "Проклятие луны"),
                 ("Голод", "Жажда"),
                 ("Отравлен", "Ослеплён"),
                 ("Кристальная лихорадка", "Резонанс Порядка"),
                 ("Стабилизация фрактала", "Связь с жилой Аэтерны"),
                 ("Второе дыхание", "Дыхание зимы")):
        assert not same(a, b), f"ложный дубль: {a!r} vs {b!r}"


def test_effect_add_warns_on_duplicate_name():
    """effect_add под именем-синонимом даёт системку-подсказку, но НЕ сливает и не
    переименовывает эффекты сам (закон 3 — решает мастер)."""
    from backend.mechanics import apply_directives
    setting = {"player": {"effects": {}}}
    apply_directives(setting, {"effect_add": {"name": "Стабилизация фрактала",
                                              "desc": "Энергия вплетается в родовой узор."}})
    msgs = apply_directives(setting, {"effect_add": {"name": "Стабилизация родового канала",
                                                     "desc": "Энергия вплетается в чертёж."}})
    assert any("близко" in m and "дубль" in m for m in msgs), f"нет подсказки о дубле:\n{msgs}"
    assert any("Стабилизация фрактала" in m for m in msgs), "не названо прежнее имя"
    effs = setting["player"]["effects"]
    assert len(effs) == 2, "движок самовольно слил эффекты (закон 3 нарушен)"
    # точное совпадение имени — обычное обновление, ложной подсказки нет
    msgs2 = apply_directives(setting, {"effect_add": {"name": "Стабилизация фрактала",
                                                      "turns": 2, "desc": "Процесс завершён."}})
    assert not any("близко" in m for m in msgs2), f"ложная подсказка при точном имени: {msgs2}"
    assert any("обновлён" in m for m in msgs2)


def test_format_state_hints_duplicate_effect_names():
    """Разовая системка о дубле могла уйти в историю — пометку мастер видит каждый ход."""
    from backend import narrator as n
    setting = {"player": {"name": "Т", "hp": 50, "max_hp": 50, "mp": 10, "max_mp": 10,
                          "effects": {
                              "Стабилизация фрактала": {"turns": -1, "kind": "особый",
                                                        "desc": "Энергия вплетается в узор."},
                              "Стабилизация родового канала": {"turns": -1, "kind": "особый",
                                                               "desc": "Энергия вплетается в чертёж."}}}}
    line = next((ln for ln in n.format_state(setting).splitlines()
                 if ln.startswith("Эффекты:")), "")
    assert line.count("дубль") == 2, f"пометка не у обоих имён:\n{line}"
    # одинокий эффект пометки не получает
    setting["player"]["effects"].pop("Стабилизация родового канала")
    line1 = next((ln for ln in n.format_state(setting).splitlines()
                  if ln.startswith("Эффекты:")), "")
    assert "дубль" not in line1, f"ложная пометка у единственного эффекта:\n{line1}"


def test_prompt_rule_forbids_synonym_effect_names():
    """Правило 20 промпта: одно состояние = одно имя (проверяем смысл, а не цитату)."""
    src = (ROOT / "backend" / "narrator.py").read_text(encoding="utf-8")
    m = re.search(r"^20\. Имена эффектов.*$", src, re.M)
    assert m, "правило 20 про имена эффектов исчезло из промпта"
    rule = m.group(0)
    assert "effect_remove" in rule and re.search(r"синоним|дубл|то же состояние", rule), \
        f"правило 20 разучилось запрещать имена-синонимы:\n{rule}"


def test_readme_opens_with_ai_warning():
    """README обязан сразу предупреждать: проект ведёт ИИ-агент, возможны ошибки.

    Пометку специально проверяет тест — её нельзя выкинуть «за ненадобностью» и
    нельзя унести ниже быстрого старта: читатель должен увидеть её ДО первого запуска."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    head = readme[:3000]
    assert "написан ИИ-агентом" in head, "предупреждение об ИИ-авторстве пропало из шапки README"
    assert "возможны" in head and ("ошибк" in head), "исчезло предупреждение о возможных ошибках"
    assert head.index("написан ИИ-агентом") < head.index("## 🚀 Быстрый старт"), \
        "предупреждение ушло ниже быстрого старта — его перестанут видеть"
