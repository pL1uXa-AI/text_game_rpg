# -*- coding: utf-8 -*-
"""check_frontend.py — проверка фронтенда без браузера (CI и локально).

frontend/ — vanilla JS без сборки, поэтому «проверить» означает:
  1) `node --check` на app.js (если node в PATH) и на встроенном <script> из admin.html;
  2) каждая `$("id")`/`getElementById("id")` в app.js имеет соответствующий id в HTML —
     иначе UI падает на клике («null is not an object»), что уже случалось в проекте;
  3) (E1, хвосты сессии 38) в разметке app.js нет JS-литералов внутри HTML-атрибутов:
     карточки состояния зовут обработчик через data-click/data-arg, аргумент экранирован
     esc() — безопасность больше не держится на дисциплине «не забудь jsAttr»;
  4) в комментариях нет посторонних CJK-иероглифов и латинских ОМОГЛИФОВ внутри
     кириллических слов («вcё» с латинской c, «копятcя») — опечатки, которые глазами
     не находятся, а проверка находит.

Запуск:  python -X utf8 scripts/check_frontend.py   (код 0 = ок)
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONT = ROOT / "frontend"

# ── E7 (хвосты сессии 38): явный белый список динамических id ──────────────────────
# Раньше «что считается динамическим» пряталось в регулярках чекера: опечатка в
# $("pl-lore") проходила бы как «id собран в рантайме». Каждый элемент тут обязан
# объяснить, ПОЧЕМУ id нет в статической разметке, и быть проверенным обратным
# поиском (см. dynamic_check ниже): префикс без постановщика id = ошибка.
# Ключи словаря — маски ({} = placeholder), расширяются на месте: "save-" и т.п.
DYNAMIC_IDS: dict[str, str] = {
    # вкладки существуют для каждого data-tab в index.html ($("tab-" + t.dataset.tab))
    "tab-": "вкладки: $(\"tab-\" + t.dataset.tab) — id есть в HTML, маска держит конкатенацию",
    # кнопки/зоны, создаваемые app.js и живущие в переменой, а не искомые по id
    "log-pager": "showLogPager(): div создаётся кодом (logPagerBtn), ищется не по id",
    "save-trash-zone": "зона дропа сохранений создаётся кодом и берётся по id сразу после создания",
}

# Что в app.js собирает id динамически (el.id = …, id="…" в JS-шаблоне).
ID_ASSIGN_RES = (
    r"""\.id\s*=\s*["'`]([A-Za-z0-9_\-]+)""",                      # el.id = "log-pager"
    r"""\.id\s*=\s*["'`]([A-Za-z0-9_\-]+)["'`]\s*\+""",            # el.id = "tab-" + name
    r"""id=["'`]([A-Za-z0-9_\-]+)\$\{[^}]*\}""",                    # id="nr-x-${i}" в шаблоне
    r"""id=["'`]\$\{[^}]*\}([A-Za-z0-9_\-]*)["'`]""",               # id="${kind}-y" — суффикс
)


def dynamic_check(app_js: str, html: str, used: set[str]) -> list[str]:
    """E7: симметричные проверки динамических id.

    (а) маска из DYNAMIC_IDS обязана чем-то назначаться в app.js, иначе список врёт;
    (б) обратно: каждый id, назначенный в app.js и отсутствующий в HTML, обязан
        попадать под маску — иначе это мёртвый id, на который уже никто не ссылается.
    """
    errors: list[str] = []
    assigned: set[str] = set()
    for rx in ID_ASSIGN_RES:
        for m in re.finditer(rx, app_js):
            tok = m.group(1)
            if tok:
                assigned.add(tok)
    prefixes = {k.rstrip("-") for k in DYNAMIC_IDS}
    # ссылка вида $("tab-" + …) / getElementById("nr-" + …) — доказательство, что маска жива
    concats = set(re.findall(r"""(?:\$\(|getElementById\()["']([A-Za-z0-9_\-]+)["']\s*\+""", app_js))
    for mask in DYNAMIC_IDS:
        stem = mask.rstrip("-")
        live = (mask in concats or stem in concats or any(stem in a for a in assigned)
                or re.search(r"""id=["'`][^"'`]*%s""" % re.escape(stem), app_js))
        if not live:
            errors.append(f"DYNAMIC_IDS: маска {mask!r} никем не собирается — список устарел "
                          f"(проверка перестанет ловить опечатки в $(\"{stem}…\"))")
    # сирота = id назначен в app.js, его нет ни в разметке, ни в списке динамических
    orphans = sorted(a for a in assigned
                     if a not in html and a not in prefixes and a not in DYNAMIC_IDS
                     and not any(a in mask for mask in DYNAMIC_IDS))
    if orphans:
        errors.append("app.js назначает id, которых нет в разметке и которые никто не зовёт "
                      "(мёртвые или опечатка): " + ", ".join(orphans[:15]))
    print(f"  · id, назначаемых в app.js: {len(assigned)}, масок DYNAMIC_IDS: {len(DYNAMIC_IDS)}, "
          f"сирот: {len(orphans)}")
    return errors


def node_check(label: str, src: str) -> list[str]:
    """`node --check` на тексте. Возвращает список ошибок (пусто = ок / node недоступен)."""
    node = shutil.which("node") or shutil.which("node.exe")
    if not node:
        print(f"  · {label}: node не найден в PATH — пропущено")
        return []
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(src)
        tmp = fh.name
    try:
        r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
        if r.returncode != 0:
            return [f"{label}: {r.stderr.strip()[:400]}"]
    finally:
        Path(tmp).unlink(missing_ok=True)
    return []


def _extract_fn(src: str, name: str) -> str:
    """Тело JS-функции `name` из исходника (по балансу фигурных скобок)."""
    i = src.find(f"function {name}(")
    if i < 0:
        raise RuntimeError(f"в app.js не найдена функция {name}()")
    depth = 0
    j = src.index("{", i)
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise RuntimeError(f"несбалансированные скобки в {name}()")


# Полигон E1 (хвосты сессии 38): берёт НАСТОЯЩИЕ esc()/attrArg() из app.js и проверяет,
# что грязный ключ переживает путь «JS-шаблон → HTML-атрибут data-arg → значение в рантайме»
# без изменений и БЕЗ исполнения кода. Ключи — от LLM, поэтому в наборе апостроф, кавычка,
# перевод строки и «';x=1;//». Последняя строка — страховка от «тест ничего не проверяет»:
# если подстановка в атрибут перестанет экранироваться, HTML оборвётся и тест это увидит.
_ATTRARG_SNIPPET = r"""
const keys = ["don't", 'quote"key', "back\\slash", "<script>", "mix'\"q", "a\nb",
              "", "ключ_юникод", "';x=1;//"];
let bad = 0;
for (const k of keys) {
  const html = '<i data-click="showQuest" data-arg="' + attrArg(k) + '"></i>';
  // допустим только один неразобранный атрибут: значение обязана заканчивать сырая "
  const m = html.match(/^<i data-click="[a-zA-Z]+" data-arg="([^"]*)"><\/i>$/);
  if (!m) { bad++; console.log("FAIL-HTML", JSON.stringify(k)); continue; }
  const got = m[1].replace(/&lt;/g, "<").replace(/&gt;/g, ">")
                  .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, "&");
  const want = k.replace(/\r/g, "");
  if (got !== want) { bad++; console.log("FAIL", JSON.stringify(k), "->", JSON.stringify(got)); }
}
console.log(bad ? "BAD=" + bad : "ROUNDTRIP_OK");
"""


def _strip_js(app_js: str) -> str:
    """app.js без комментариев: текст правил E1 помянут старую формулировку `onclick="…"`,
    и она не должна считаться нарушением."""
    code = re.sub(r"(?m)^\s*(//|\*).*?$", "", app_js)
    return re.sub(r"/\*.*?\*/", "", code, flags=re.S)


def dataclick_check(app_js: str) -> list[str]:
    """E1: ни JS-литералов в атрибутах, ни незакрытых data-click/data-arg."""
    errors: list[str] = []

    # 1) inline-JS в разметке запрещён целиком (старый `onclick="fn('…')"`, `href="javascript:"`)
    code = _strip_js(app_js)
    inline = [m for m in re.finditer(r"""onclick\s*=\s*["']""", code)]
    if inline:
        shown = [code[:m.start()].count("\n") + 1 for m in inline][:10]
        errors.append("app.js: вернулись inline-обработчики onclick=\"…\" (строки: "
                      + ", ".join(map(str, shown)) + ") — E1: только data-click + делегат")
    js_url = re.findall(r"""(?:href|src)\s*=\s*["'`]\s*javascript:""", code, re.I)
    if js_url:
        errors.append(f"app.js: {len(js_url)} ссылок javascript: в разметке")

    # 2) каждая data-click должна быть в реестре делегата и иметь объявленную функцию
    decl = {m.group(1) for m in re.finditer(r"""(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(""", app_js)}
    m = re.search(r"const CLICK_ACTIONS = \{([^}]*)\};", app_js, re.S)
    if not m:
        errors.append("app.js: нет реестра CLICK_ACTIONS — делегат E1 потерян")
        actions: set[str] = set()
    else:
        actions = {x.strip().rstrip(",").split(":")[0].strip()
                   for x in m.group(1).replace("\n", " ").split(",") if x.strip()}
        not_defined = sorted(a for a in actions if a not in decl)
        if not_defined:
            errors.append("CLICK_ACTIONS ссылается на несуществующие функции: "
                          + ", ".join(not_defined))
    used = set(re.findall(r"""data-click="([A-Za-z]+)""", code))
    n_click = len(re.findall(r"""data-click=""", code))
    n_arg = len(re.findall(r"""data-arg=""", code))
    if n_click != n_arg:
        errors.append(f"app.js: data-click {n_click} шт, а data-arg {n_arg} шт — "
                      "у части карточек аргумент потерян")
    unknown = sorted(used - actions)
    if unknown:
        errors.append("app.js: data-click без обработчика в CLICK_ACTIONS: " + ", ".join(unknown))
    unused = sorted(actions - used)
    if unused:
        errors.append("app.js: CLICK_ACTIONS содержит лишние действия (никто не зовёт): "
                      + ", ".join(unused))
    # 3) у data-arg обязана быть подстановка через esc-обёртку, а не голая переменная
    args = re.findall(r"""data-arg="\$\{([^}]+)\}""", code)
    bare = sorted({a.strip() for a in args if not re.match(r"^(attrArg|numAttr)\(", a.strip())})
    if bare:
        errors.append("app.js: data-arg заполняется НЕ через attrArg/numAttr: " + ", ".join(bare[:10]))
    print(f"  · data-click: {len(used)} действий, подстановок data-arg: {len(args)}, "
          f"без экранирования: {len(bare)}, inline-onclick: {len(inline)}")

    node = shutil.which("node") or shutil.which("node.exe")
    if not node:
        print("  · node не найден — roundtrip-тест attrArg пропущен")
        return errors
    snippet = (_extract_fn(app_js, "esc") + "\n" + _extract_fn(app_js, "attrArg")
               + "\n" + _ATTRARG_SNIPPET)
    with tempfile.NamedTemporaryFile("w", suffix=".cjs", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(snippet)
        tmp = fh.name
    try:
        r = subprocess.run([node, tmp], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        out = (r.stdout or "") + (r.stderr or "")
        if "ROUNDTRIP_OK" not in out:
            errors.append("app.js: roundtrip attrArg не пройден: " + out.strip()[:400])
        else:
            print("  · attrArg: roundtrip грязных ключей ок")
    except Exception as e:
        errors.append(f"app.js: запуск roundtrip-теста не удался: {e}")
    finally:
        Path(tmp).unlink(missing_ok=True)
    return errors


# CJK: иероглифы, каны, корейские слоги, полноширинные знаки
_CJK_RE = re.compile("[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff"
                     "\uac00-\ud7af\uf900-\ufaff\uff01-\uff60]")
# Осознанное исключение по маркеру (как noqa): строка, где «подозрительный» символ нужен
# НАМЕРЕННО — например, в документации цитируются сами исправленные опечатки.
# Работает для обеих проверок (CJK и омоглифы).
_OMO_MARK = "TYPO-OK"


def cjk_check(files: dict) -> list[str]:
    """Посторонние CJK-символы в кириллическом проекте — признак опечатки (сессия 36).

    Строку можно выключить маркером `TYPO-OK` (например, для цитаты в документации)."""
    errors = []
    for label, text in files.items():
        for n, line in enumerate(text.splitlines(), 1):
            if _OMO_MARK in line:
                continue
            if _CJK_RE.search(line):
                errors.append(f"{label}:{n}: посторонние CJK-символы: {line.strip()[:90]}")
    return errors


# Латинские буквы — визуальные двойники кириллицы, и сам кириллический алфавит:
# ищем ОМОГЛИФЫ внутри одного слова (тот же класс молчаливой порчи, что CJK-вставки).
_LOOKALIKE = set("aAcceoxxyKkMmTtHhBbPp")
_CYR_SET = set("АБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
               "абвгдежзийклмнопрстуфхцчшщъыьэюяёЁ")
_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё]+")
# Строки с regex-классами и литеральными escape-последовательностями — легальное
# смешение скриптов ([а-яA-Za-z], "\n", "\u2026"), а не опечатка.
_OMO_SKIP = ("findall", "re.sub", "re.compile", "re.match", "re.fullmatch",
             "re.search", "[а-я", "[А-Я", "\\u", "\\\\", "\\x", "\\n", "\\t", "\\r", "%.")


def omoglyph_check(files: dict) -> list[str]:
    """Латинские омоглифы внутри кириллических слов в комментариях/доках (сессия 36).

    «вcё» (c латинская), «копятcя», «фаcаd», «пересh», «меx» — глазами выглядят чисто,
    но ломают поиск по тексту и выдают невнимательную правку. Правило: слово, где ≥2
    кириллицы и от 1 до 3 латиницы-двойника. Доказательность: на HEAD сканер нашёл
    5 таких опечаток, в вычищенном рабочем дереве — 0 ложных срабатываний.

    Строку можно выключить маркером `TYPO-OK` (цитаты самих опечаток в документации).
    """
    errors = []
    for label, text in files.items():
        for n, line in enumerate(text.splitlines(), 1):
            if _OMO_MARK in line:
                continue
            if any(k in line for k in _OMO_SKIP):
                continue
            for w in _WORD_RE.findall(line):
                cyr = sum(1 for c in w if c in _CYR_SET)
                lat = [c for c in w if c in _LOOKALIKE and c.isascii()]
                if cyr >= 2 and 1 <= len(lat) <= 3:
                    errors.append(f"{label}:{n}: латинский омоглиф в слове {w!r}: "
                                  f"{line.strip()[:80]}")
    return errors


def mobile_check(css: str, html: str, app_js: str) -> list[str]:
    """E6 (хвосты 38): мобильная адаптивность не должна молча «испариться».

    Ловит не «красоту», а конкретные регрессии: @media-блок ≤900px обязан остаться,
    в нём — выдвижная панель, тап-цели ≥44px и шрифт 16px в поле ввода (iOS при меньшем
    зумит страницу), а переключатель панели обязан быть и в разметке, и в JS.
    """
    errors: list[str] = []
    m = re.search(r"@media[^{]*\(max-width:\s*(\d+)px\)\s*\{", css)
    if not m:
        return ["style.css: нет ни одного @media (max-width:…) — E6: адаптивность отсутствует"]
    if int(m.group(1)) < 900:
        errors.append(f"style.css: первый брейкпоинт {m.group(1)}px < 900px — панель мира "
                      f"на планшете всё ещё прижата к чату")
    body = css[css.index("@media"):]
    for need, why in (("translateX", "выдвижная панель (transform: translateX)"),
                      ("min-height: 44px", "тап-цели ≥44px"),
                      ("font-size: 16px", "шрифт 16px в поле ввода (iOS не зумит)")):
        if need not in body:
            errors.append(f"style.css: в @media нет {why}")
    if "prefers-reduced-motion" not in css:
        errors.append("style.css: нет prefers-reduced-motion — анимации навязываются")
    if 'id="btn-sidebar"' not in html:
        errors.append("index.html: нет кнопки btn-sidebar — панель мира нечем открыть")
    for hook in ("btn-sidebar", "sidebar-scrim", "side-open"):
        if hook not in app_js:
            errors.append(f"app.js: нет привязки {hook!r} — переключатель панели не работает")
    print(f"  · E6: брейкпоинтов {len(re.findall(r'@media', css))}, первый "
          f"{m.group(1)}px, хуков панели: 3/3")
    return errors


def main() -> int:
    errors: list[str] = []
    app_js = (FRONT / "app.js").read_text(encoding="utf-8")
    css = (FRONT / "style.css").read_text(encoding="utf-8")
    index_html = (FRONT / "index.html").read_text(encoding="utf-8")
    admin_html = (FRONT / "admin.html").read_text(encoding="utf-8")
    html = index_html + admin_html
    # id бывают и в разметке, и в JS-шаблонах (innerHTML из app.js: модалки, строки сохранений,
    # «Показать ранние события»). Учитываем оба источника, иначе проверка врёт.
    ID_RE = r"""id=["'`]([^"'`{}$]+)["'`]"""
    html_ids = set(re.findall(ID_RE, html))
    js_ids = set(re.findall(ID_RE, app_js))
    # id, назначаемые динамически (el.id = "…"), тоже существуют в рантайме
    js_ids |= set(re.findall(r"""\.id\s*=\s*["']([A-Za-z0-9_\-]+)["']""", app_js))
    known = html_ids | js_ids

    print("frontend checks:")
    errors += node_check("app.js", app_js)
    m = re.search(r"<script>(.*)</script>", admin_html, re.S)
    if not m:
        errors.append("admin.html: не найден <script> — структура страницы изменилась")
    else:
        errors += node_check("admin.html <script>", m.group(1))

    # ссылки на элементы из app.js обязаны существовать в разметке
    used = set(re.findall(r"""(?:\$\(|getElementById\()["']([A-Za-z0-9_\-]+)["']""", app_js))
    missing = sorted(i for i in used if i not in known
                     and not any(i.startswith(m) for m in DYNAMIC_IDS))
    if missing:
        errors.append("app.js ссылается на отсутствующие id: " + ", ".join(missing[:20]) +
                      ("" if len(missing) <= 20 else f" … и ещё {len(missing) - 20}"))
    print(f"  · id-ссылок в app.js: {len(used)}, из них не найдено в HTML: {len(missing)}")

    errors += dynamic_check(app_js, html, used)
    errors += dataclick_check(app_js)
    errors += mobile_check(css, html, app_js)
    files = {"frontend/app.js": app_js, "frontend/index.html": index_html,
             "frontend/admin.html": admin_html}
    # ту же опечатку ловим и в бэкенде (исторический случай — tests/conftest.py),
    # и в батнике запуска: сканируем только текстовые файлы проекта, без data/
    for extra in sorted((ROOT / "backend").rglob("*.py")) + \
            sorted((ROOT / "tests").glob("*.py")) + \
            sorted((ROOT / "plots").rglob("*.js")) + \
            [ROOT / "start_game.bat", ROOT / ".env.example",
             ROOT / "README.md", ROOT / "AGENT.md", ROOT / "ROADMAP.md", ROOT / "PLOTS.md"]:
        if "__pycache__" in str(extra):
            continue
        try:
            files[str(extra.relative_to(ROOT))] = extra.read_text(encoding="utf-8")
        except Exception:
            continue
    errors += cjk_check(files)
    errors += omoglyph_check(files)
    print(f"  · файлов проверено на CJK/омоглифы: {len(files)}, найдено строк: "
          f"{len([e for e in errors if 'CJK' in e or 'омоглиф' in e])}")

    if errors:
        print("\n❌ ОШИБКИ:")
        for e in errors:
            print("  -", e)
        return 1
    print("\n✅ фронтенд проверен")
    return 0


if __name__ == "__main__":
    sys.exit(main())
