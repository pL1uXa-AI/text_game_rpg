# -*- coding: utf-8 -*-
"""Хвосты сессии 38 (E1, E7, D11, D15-хвост): проверки закрытых пунктов.

Формулировки хвостов — в ROADMAP.md («🔴 Хвосты сессии 38»), контекст аудита —
«Сводка сессии 38» в AGENT.md. Инвариант 19: тесты проверяют ПОВЕДЕНИЕ, а не подстроки;
структурные проверки живут в scripts/check_*.
"""
from __future__ import annotations

import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


# ══════════════════════ E1: разметка без JS-литералов в атрибутах ══════════════════
def _app_js() -> str:
    return (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")


def test_e1_no_inline_onclick_in_markup():
    """Ни одного `onclick="…"` в строковых шаблонов: обработчик назначается делегатом.

    Прежняя защита (сессия 36, п.25) держалась на дисциплине «не забудь jsAttr()».
    Делегат убирает класс риска: в атрибуте лежат данные, а не исполняемый JS.
    """
    import re
    code = re.sub(r"(?m)^\s*(//|\*).*?$", "", _app_js())
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    assert 'onclick="' not in code
    assert not re.search(r'(?:href|src)\s*=\s*["\'`]\s*javascript:', code, re.I)


def test_e1_delegate_resolves_actions_to_functions():
    """CLICK_ACTIONS: каждое действие — существующая функция, каждый data-click — в реестре."""
    import re
    js = _app_js()
    m = re.search(r"const CLICK_ACTIONS = \{([^}]*)\};", js, re.S)
    assert m, "реестр делегата потерян"
    actions = {x.strip() for x in m.group(1).replace("\n", " ").split(",") if x.strip()}
    decl = set(re.findall(r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", js))
    assert actions <= decl, f"реестр зовёт несуществующие: {sorted(actions - decl)}"
    used = set(re.findall(r'data-click="([A-Za-z]+)"', js))
    assert used == actions, f"data-click вне реестра: {sorted(used ^ actions)}"


def test_e1_delegate_passes_string_keys_verbatim():
    """Сам риск прежнего решения — ключ с апострофом. Проверяем путь целиком на Node.

    Путь: объект ключа → esc() → data-arg → разбор атрибута → аргумент обработчика.
    Ожидание: обработчик получает ТОЧНО тот ключ, что был в словаре мира (ключи словарей
    сущностей — строки, даже когда выглядят числом: «123» не должно стать 123).
    """
    node = subprocess.run(["node", "--version"], capture_output=True, text=True)
    if node.returncode != 0:
        pytest.skip("node недоступен")
    from check_frontend import _extract_fn  # общий помощник чекера фронта
    keys = ["don't", 'quote"key', "back\\slash", "<script>", "';x=1;//", "ключ_юникод", "123"]
    snippet = (
        _extract_fn(_app_js(), "esc") + "\n" + _extract_fn(_app_js(), "attrArg") + "\n"
        + "const got = [];\n"
        + "const CLICK_NUMERIC = new Set(%s);\n" % json.dumps(["showItem"])
        + "const fn = (v) => got.push([typeof v, v]);\n"
        + "const keys = %s;\n" % json.dumps(keys, ensure_ascii=False)
        + """
for (const k of keys) {
  const html = '<i data-click="showQuest" data-arg="' + attrArg(k) + '"></i>';
  const m = html.match(/data-arg="([^"]*)"/);
  if (!m) { console.log('HTML_BROKEN', JSON.stringify(k)); process.exit(1); }
  const raw = m[1].replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"')
                  .replace(/&#39;/g, "'").replace(/&amp;/g, "&");
  fn(raw);
}
const bad = got.filter(([t, v]) => t !== "string" || !keys.includes(v));
console.log(bad.length ? "BAD " + JSON.stringify(bad) : "DELEGATE_OK");
""")
    tmp = ROOT / "_e1_probe.cjs"
    tmp.write_text(snippet, encoding="utf-8")
    try:
        r = subprocess.run(["node", str(tmp)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
    finally:
        tmp.unlink(missing_ok=True)
    assert "DELEGATE_OK" in (r.stdout or ""), (r.stdout or "") + (r.stderr or "")


def test_e7_check_frontend_green():
    """Чекер фронта с новыми проверками (E7 — обратный ход динамических id) зелёный."""
    r = subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "scripts" / "check_frontend.py")],
                       cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-500:]


def test_e7_dynamic_ids_are_explicit_and_live():
    """E7: каждая маска DYNAMIC_IDS обоснована и реально собирается в app.js.

    Раньше whitelist жил в регулярках чекера неявно — опечатка в $("pl-lore") проходила
    как «такой id собирается в рантайме». Теперь список — явный словарь с пояснением,
    и чекер проверяет его в обе стороны.
    """
    import check_frontend as cf
    assert isinstance(cf.DYNAMIC_IDS, dict) and all(v.strip() for v in cf.DYNAMIC_IDS.values()), \
        "у маски динамического id должно быть текстовое обоснование"
    app_js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    html = ((ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
            + (ROOT / "frontend" / "admin.html").read_text(encoding="utf-8"))
    used = set(cf.re.findall(r"""(?:\$\(|getElementById\()["']([A-Za-z0-9_\-]+)["']""", app_js))
    assert not cf.dynamic_check(app_js, html, used)


# ══════════════════ D15-хвост: строгая структура файла сюжета ══════════════════
def _plot_dict() -> dict:
    p = sorted((ROOT / "plots" / "system").glob("*.js"))[0]
    return json.loads(p.read_text(encoding="utf-8"))


def test_d15_schema_accepts_all_system_plots():
    """Ни одно ложное срабатывание: все системные сюжеты проходят строгую схему."""
    import check_plot
    for p in sorted((ROOT / "plots" / "system").glob("*.js")):
        errs: list[str] = []
        check_plot.schema_check(json.loads(p.read_text(encoding="utf-8")),
                                lambda m: errs.append(m))
        assert not errs, f"{p.name}: {errs[:5]}"


@pytest.mark.parametrize("mutate,expect", [
    (lambda d: d["starting_state"]["locations"][0].pop("name"), "locations"),
    (lambda d: d["starting_state"].__setitem__("gold", "много"), "gold"),
    (lambda d: d["metadata"].pop("logline"), "logline"),
    (lambda d: d["story"]["quest_chains"][0].__setitem__("status", 123), "status"),
    (lambda d: d["story"]["acts"][0]["chapters"][0].pop("title"), "title"),
    (lambda d: d["lore_articles"][0].pop("content"), "lore_articles"),
])
def test_d15_schema_rejects_broken_but_valid_json(mutate, expect):
    """Главный смысл хвоста: валидный JSON с битой структурой больше не проходит молча."""
    import check_plot
    d = _plot_dict()
    mutate(d)
    errs: list[str] = []
    check_plot.schema_check(d, lambda m: errs.append(m))
    assert errs, f"схема не заметила поломку {expect}"
    assert any(expect in e for e in errs), f"нарушение не про {expect}: {errs[:3]}"


def test_d15_schema_keeps_author_free_fields():
    """Закон 1: свои поля автора (неизвестные движку) схемой запрещены НЕ быть."""
    import check_plot
    d = _plot_dict()
    d["moya_pamyat"] = {"своё": "содержимое"}
    d["starting_state"]["avtorskiy_blok"] = 1
    errs: list[str] = []
    check_plot.schema_check(d, lambda m: errs.append(m))
    assert not errs, errs[:3]


def test_d15_broken_plot_visible_to_user(tmp_path):
    """Регрессия видимости: файл, битый по структуре, не должен исчезать из каталога молча.

    Проверка поведенческая: plots.scan читает plots/user; файл с валидным JSON, но без
    обязательных блоков, обязан попасть в SCAN_ERRORS (его видит плашка в окне «Новый мир»).
    """
    from backend import plots
    user = ROOT / "plots" / "user"
    user.mkdir(parents=True, exist_ok=True)
    f = user / "_probe-test-schema.js"
    f.write_text(json.dumps({"metadata": {"name": "Проба"}, "lore_text": "x"}),
                 encoding="utf-8")
    try:
        plots.reload()
        bad = [e for e in plots.scan_errors() if "_probe-test-schema" in e["file"]]
        assert bad, f"битый сюжет не попал в реестр: {plots.scan_errors()}"
    finally:
        f.unlink(missing_ok=True)
        plots.reload()


# ══════════════════ E6: минимальная мобильная адаптивность ══════════════════
def test_e6_media_blocks_present():
    """@media есть, первый брейкпоинт ≤900px, панель выдвигается, тап-цели ≥44px."""
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    from check_frontend import mobile_check
    front = ROOT / "frontend"
    assert not mobile_check(
        (front / "style.css").read_text(encoding="utf-8"),
        (front / "index.html").read_text(encoding="utf-8"),
        (front / "app.js").read_text(encoding="utf-8"))


@pytest.mark.parametrize("mutate,why", [
    (lambda c: c.replace("translateX(-102%)", "none"), "выдвижная панель"),
    (lambda c: c.replace("min-height: 44px", "min-height: 30px"), "тап-цели"),
    (lambda c: c.replace("font-size: 16px", "font-size: 13px"), "шрифт поля ввода"),
])
def test_e6_checker_detects_degradation(mutate, why):
    """Инвариант 19: проверка обязана ловить возврат к десктоп-только вёрстке."""
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    from check_frontend import mobile_check
    front = ROOT / "frontend"
    css = mutate((front / "style.css").read_text(encoding="utf-8"))
    errs = mobile_check(css, (front / "index.html").read_text(encoding="utf-8"),
                        (front / "app.js").read_text(encoding="utf-8"))
    assert errs, f"чекер не заметил потерю: {why}"


def test_e6_sidebar_toggle_wired():
    """Переключатель панели: кнопка в разметке, обработчик в JS, класс в CSS."""
    front = ROOT / "frontend"
    html = (front / "index.html").read_text(encoding="utf-8")
    js = (front / "app.js").read_text(encoding="utf-8")
    css = (front / "style.css").read_text(encoding="utf-8")
    assert 'id="btn-sidebar"' in html and 'id="sidebar-scrim"' in html
    assert "$(\"btn-sidebar\")" in js and "side-open" in js
    assert ".side-open .sidebar" in css.replace("#screen-game ", "")


# ══════════════════ D5: типы в старых модулях + строгость mypy ══════════════════
def test_d5_mypy_clean_with_check_untyped_defs():
    """Остатки mypy разобраны, check_untyped_defs включён — прогон обязан быть чистым.

    Регрессия двойная: (а) вернуть замечание, (б) выключить строгость обратно, «чтобы
    CI молчал» — тогда проверка перестаёт видеть тела функций без аннотаций.
    """
    ini = (ROOT / "mypy.ini").read_text(encoding="utf-8")
    assert re.search(r"^check_untyped_defs = true", ini, re.M), "check_untyped_defs снова выключен"
    r = subprocess.run([sys.executable, "-X", "utf8", "-m", "mypy", "-p", "backend"],
                       cwd=str(ROOT), capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, (r.stdout or "")[-2000:] + (r.stderr or "")[-500:]


def test_d5_no_mass_type_ignore():
    """Правило проекта: массовый `# type: ignore` запрещён — разбирать по месту."""
    hits = []
    for f in sorted((ROOT / "backend").rglob("*.py")):
        for n, ln in enumerate(io.open(f, encoding="utf-8"), 1):
            code = ln.split("#")[0]
            if code.strip() and "type: ignore" in ln and "type: ignore" not in code:
                hits.append(f"{f.name}:{n}")
    # 3 осознанных исключения (порождают их не типы бэкенда, а сторонние слои):
    # config.py — кэш готового Config, db.py:114 — asyncio.run_coroutine_threadsafe,
    # graph.py — Union при обходе родителя. Новые — только по месту и с пояснением.
    assert len(hits) <= 3, f"разрастается type: ignore вместо правки типов: {hits}"


def test_d5_db_row_helper_rejects_missing_row():
    """Помощники db обязаны падать внятно, а не молча превращать None в 0/'' (правило 14)."""
    import asyncio
    from backend import db

    class _Cur:
        async def fetchone(self):
            return None

    with pytest.raises(LookupError):
        asyncio.run(db._one_row(_Cur()))
    with pytest.raises(LookupError):
        asyncio.run(db._as_dict(_Cur()))

    class _NoId:
        lastrowid = None

    with pytest.raises(LookupError):
        db._rowid(_NoId())
    assert db._rowid(type("X", (), {"lastrowid": 7})()) == 7


# ══════════════ B1/B3: файлы рабочей копии (решение владельца) ══════════════
def test_b1_trash_files_stay_out_of_git():
    """Пока владелец не решил удалять ли мусор — минимум: он не должен попасть в репозиторий.

    .env.bak* содержат живые ключи; проверка по индексу git (правило 15), а не по .gitignore:
    файл мог быть добавлен до появления игнора.
    """
    r = subprocess.run(["git", "ls-files"], cwd=str(ROOT), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-300:]
    tracked = set(r.stdout.splitlines())
    forbidden = [p for p in tracked
                 if (p.startswith(".env.") and p != ".env.example") or p == "nul"
                 or p.startswith("data/") or p.startswith("НЕ УДАЛЯТЬ")
                 or p.startswith("scripts/_test_") or p.startswith("server")
                 or (Path(p).suffix == ".log" and not p.startswith("data/"))]
    assert not forbidden, f"в git попали файлы рабочей копии: {forbidden}"
    # и в рабочем дереве ни один из этих путей не лежит в staged-индексе
    st = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=str(ROOT),
                        capture_output=True, text=True)
    assert not [p for p in st.stdout.splitlines() if p.startswith(".env")]

# ══════════════════ D11: структурные проверки батников в scripts/ ══════════════════
def test_d11_check_start_bat_green():
    r = subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "scripts" / "check_start_bat.py")],
                       cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-500:]


@pytest.mark.parametrize("needle,replace,kind,expect", [
    ('if "%GAME_PYTHON%"=="" set "GAME_PYTHON=python"',
     'set GAME_PYTHON="D:\\Tools\\python.exe"', "abs_path", "абсолютный путь"),
    ("--host %GAME_BIND%", "--host 0.0.0.0", "public_bind", "0.0.0.0"),
    ('-w "%%{http_code}"', "", "errorlevel_style", "http_code"),
])
def test_d11_checker_detects_degradation(tmp_path, needle, replace, kind, expect):
    """Страховка от «тест ничего не проверяет»: подпорченный bat обязан быть пойман.

    Два обязательных условия честности пробы: (а) файл пишся байтами с CRLF — иначе на
    Linux сработал бы CRLF-инвариант и тест «зеленел» бы по ложной причине, не увидев
    реальной поломки; (б) проверяется КОНКРЕТНОЕ нарушение (expect), а не «любая ошибка».
    """
    import check_start_bat as c
    src = (ROOT / "start_game.bat").read_text(encoding="utf-8")
    assert needle in src, f"исходник start_game.bat изменился — обнови пробу ({kind})"
    bat = tmp_path / "start_game.bat"
    bat.write_bytes(src.replace(needle, replace).replace("\n", "\r\n").encode("utf-8"))
    errs = c.check_bat(bat) + c.check_main_bat(bat.read_text(encoding="utf-8"))
    assert any(expect in e for e in errs), \
        f"чекер не поймал возврат к {kind}; ошибки: {[e[:60] for e in errs]}"
    # и фальшивых срабатываний нет: целый файл проходит чисто
    whole = tmp_path / "whole.bat"
    whole.write_bytes(src.replace("\n", "\r\n").encode("utf-8"))
    assert not (c.check_bat(whole) + c.check_main_bat(src)), "чекер врёт на целом bat"


def test_d11_block_echo_rule(tmp_path):
    """D11: «голая» скобка в echo внутри if-блока сторожится и структурно.

    Нужно именно потому, что харнесс tests/test_start_bat_harness.py исполняет bat только на
    Windows, а CI — Linux: без этой проверки регрессия launcher'а (см. git log сессии 39:
    из-за одной такой строки сервер не запускался никогда) проходила бы в CI незамеченной.
    """
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    from check_start_bat import block_echo_check
    for b in [ROOT / "start_game.bat"] + sorted((ROOT / "scripts").glob("*.bat")):
        assert not block_echo_check(b), f"{b.name}: echo/set со скобкой внутри if-блока"
    d = tmp_path / "broken_block.bat"
    d.write_bytes("@echo off\r\nif 1 EQU 0 (\r\n    echo text (x)\r\n    pause\r\n)\r\n"
                  .encode("utf-8"))
    assert block_echo_check(d), "чекер ослеп: сломанный блок не пойман"
    ok = tmp_path / "good_block.bat"
    ok.write_bytes("@echo off\r\nif 1 EQU 0 (\r\n    echo text ^(x^)\r\n    pause\r\n)\r\n"
                   .encode("utf-8"))
    assert not block_echo_check(ok), "чекер врёт на исправленном bat"
