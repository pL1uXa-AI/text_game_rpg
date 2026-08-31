# -*- coding: utf-8 -*-
"""check_frontend.py — проверка фронтенда без браузера (CI и локально).

frontend/ — vanilla JS без сборки, поэтому «проверить» означает:
  1) `node --check` на app.js (если node в PATH) и на встроенном <script> из admin.html;
  2) каждая `$("id")`/`getElementById("id")` в app.js имеет соответствующий id в HTML —
     иначе UI падает на клике («null is not an object»), что уже случалось в проекте.

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


def main() -> int:
    errors: list[str] = []
    app_js = (FRONT / "app.js").read_text(encoding="utf-8")
    html = "".join((FRONT / n).read_text(encoding="utf-8")
                   for n in ("index.html", "admin.html"))
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
    m = re.search(r"<script>(.*)</script>", (FRONT / "admin.html").read_text(encoding="utf-8"),
                  re.S)
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

    if errors:
        print("\n❌ ОШИБКИ:")
        for e in errors:
            print("  -", e)
        return 1
    print("\n✅ фронтенд проверен")
    return 0


if __name__ == "__main__":
    sys.exit(main())
