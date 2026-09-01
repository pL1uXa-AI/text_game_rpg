# -*- coding: utf-8 -*-
"""check_frontend.py — проверка фронтенда без браузера (CI и локально).

frontend/ — vanilla JS без сборки, поэтому «проверить» означает:
  1) `node --check` на app.js (если node в PATH) и на встроенном <script> из admin.html;
  2) каждая `$("id")`/`getElementById("id")` в app.js имеет соответствующий id в HTML —
     иначе UI падает на клике («null is not an object»), что уже случалось в проекте;
  3) ключи сущностей подставляются в onclick только через jsAttr()/numAttr() (сессия 36,
     п.25): esc() не экранирует апостроф, и id вида don't обрывал JS-литерал в атрибуте;
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

# Имена, которых заведомо нет в статическом HTML (создаются динамически).
DYNAMIC_IDS = {
    "tab-",  # склейка $("tab-" + t.dataset.tab) — вкладки существуют для каждого data-tab
}


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


# Полигон: берёт НАСТОЯЩИЕ esc()/jsAttr() из app.js и проверяет, что грязный ключ
# переживает путь «JS-шаблон → HTML-атрибут → JS-литерал» неизменным. Последняя
# строка — страховка от «тест ничего не проверяет»: старое экранирование (только esc)
# на апострофе ломается, и тест обязан это видеть.
_JSATTR_SNIPPET = r"""
const keys = ["don't", 'quote"key', "back\\slash", "<script>", "mix'\"q", "a\nb",
              "", "ключ_юникод", "';x=1;//"];
function attrVal(html) {
  // браузер заканчивает значение атрибута на ПЕРВОЙ сырой кавычке (\" в HTML не спасает)
  const m = html.match(/onclick="([^"]*)"/);
  if (!m) return { ok: false, why: "атрибут оборвался — HTML сломан" };
  const a = m[1].replace(/&lt;/g, "<").replace(/&gt;/g, ">")
                .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, "&");
  const q = a.match(/^f\((['"])([\s\S]*)\1\)$/);
  if (!q) return { ok: false, why: "литерал не читается: " + a };
  try { return { ok: true, value: new Function("return " + q[1] + q[2] + q[1] + ";")() }; }
  catch (e) { return { ok: false, why: "битый JS в атрибуте: " + e.message }; }
}
let bad = 0;
for (const k of keys) {
  const r = attrVal("<i onclick=\"f('" + jsAttr(k) + "')\"></i>");
  if (!r.ok || r.value !== k.replace(/\r/g, "")) {
    bad++;
    console.log("FAIL", JSON.stringify(k), r.why || JSON.stringify(r.value));
  }
}
const legacy = attrVal("<i onclick=\"f('" + esc("don't") + "')\"></i>");
console.log("LEGACY_BROKEN=" + (legacy.ok ? 0 : 1));
console.log(bad ? "BAD=" + bad : "ROUNDTRIP_OK");
"""


def jsattr_check(app_js: str) -> list[str]:
    """Экранирование ключей внутри onclick="fn('…')" (сессия 36, п.25)."""
    errors: list[str] = []
    if "function jsAttr" not in app_js:
        return ["app.js: нет функции jsAttr() — ключи в onclick неэкранированы (п.25)"]

    # статически: каждая подстановка в JS-литерале атрибута — через jsAttr/numAttr
    subs = re.findall(r"""onclick="[A-Za-z]+\([^)"]*'\$\{([^}]+)\}'""", app_js)
    bad = sorted({s.strip() for s in subs
                  if not re.match(r"^(jsAttr|numAttr)\(", s.strip())})
    if bad:
        errors.append("app.js: в onclick-литерал подставляется НЕ через jsAttr/numAttr: "
                      + ", ".join(bad[:10]))
    print(f"  · подстановок в onclick-литералах: {len(subs)}, вне jsAttr/numAttr: {len(bad)}")

    node = shutil.which("node") or shutil.which("node.exe")
    if not node:
        print("  · node не найден — roundtrip-тест jsAttr пропущен")
        return errors
    snippet = (_extract_fn(app_js, "esc") + "\n" + _extract_fn(app_js, "jsAttr")
               + "\n" + _JSATTR_SNIPPET)
    with tempfile.NamedTemporaryFile("w", suffix=".cjs", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(snippet)
        tmp = fh.name
    try:
        r = subprocess.run([node, tmp], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        out = (r.stdout or "") + (r.stderr or "")
        if "ROUNDTRIP_OK" not in out:
            errors.append("app.js: roundtrip jsAttr не пройден: " + out.strip()[:400])
        elif "LEGACY_BROKEN=0" in out:
            errors.append("app.js: проверка jsAttr деградировала — старое esc() проходит")
        else:
            print("  · jsAttr: roundtrip грязных ключей ок (старое esc ломается, как надо)")
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


def main() -> int:
    errors: list[str] = []
    app_js = (FRONT / "app.js").read_text(encoding="utf-8")
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
    missing = sorted(i for i in used if i not in known and i not in DYNAMIC_IDS)
    if missing:
        errors.append("app.js ссылается на отсутствующие id: " + ", ".join(missing[:20]) +
                      ("" if len(missing) <= 20 else f" … и ещё {len(missing) - 20}"))
    print(f"  · id-ссылок в app.js: {len(used)}, из них не найдено в HTML: {len(missing)}")

    errors += jsattr_check(app_js)
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
