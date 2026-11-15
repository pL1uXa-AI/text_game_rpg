# -*- coding: utf-8 -*-
"""Версионирование статики: `?v=<mtime>` в ссылках на CSS/JS.

Зачем (сессия 85, реальная жалоба «адаптация не поменялась»). Браузер телефона держал
старые `style.css`/`app.js`: ссылки в HTML были без версии, а эвристический кэш мобилок
не перепроверяет файл, пока ссылка та же. Заголовки `Cache-Control` помогают только
после того, как браузер дойдёт до сервера, — то есть не помогают вовсе в этом случае.

Решение: при отдаче `index.html`/`admin.html` подставляем к `/static/<файл>` параметр
`?v=<mtime в секундах>`. Изменился файл — изменился URL — браузер обязан скачать заново.
Кэширование при этом не выключается: неизменённый файл отдаётся из кэша как раньше.

Без зависимостей и без правки разметки руками: теги `<link rel="stylesheet">` и
`<script src>` переписываются на лету.
"""
from __future__ import annotations

import re
from pathlib import Path

# Ловим ровно статические ссылки: /static/<file> без параметров запроса.
_STATIC_REF = re.compile(r'(/static/[A-Za-z0-9_.\-]+)(?!\?)')


def _mtime(path: Path) -> int:
    try:
        return int(path.stat().st_mtime)
    except OSError:
        return 0


def version_static(html: str, frontend_dir: str | Path) -> str:
    """Возвращает HTML, где каждая ссылка на /static/* имеет `?v=<mtime>`."""
    root = Path(frontend_dir)
    cache: dict[str, int] = {}

    def repl(m: re.Match) -> str:
        ref = m.group(1)
        if ref not in cache:
            cache[ref] = _mtime(root / ref.rsplit("/", 1)[-1])
        return f"{ref}?v={cache[ref]}"

    return _STATIC_REF.sub(repl, html)
