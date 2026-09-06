# -*- coding: utf-8 -*-
"""plots.py — загрузчик сюжетов-миров из папки plots/.

Сюжеты лежат как ОТДЕЛЬНЫЕ файлы (формат — чистый JSON по схеме PLOTS.md,
расширение `.js`) в двух папках:

    plots/system/   — комплектные «стандартные» сюжеты, идущие с игрой
    plots/user/     — сюжеты игрока (добавляются/правятся БЕЗ перезапуска)

Движок при старте (и по требованию кнопки «🔄 Обновить сюжеты») сканирует папки,
строит для каждого сюжета тему-обёртку (как записи THEMES) и отдаёт её в каталог
и в создание мира. Миры, уже созданные из сюжета, НЕ зависят от файла: при создании
в setting копируется снапшот темы (`_theme_snapshot`) и применяется стартовое
состояние (`starting_state`) — удаление/правка файла не трогает существующие миры.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .narrator_data import GENRE_HINTS

from .logsetup import get_logger

log = get_logger(__name__)

PLOTS_ROOT = Path(__file__).resolve().parent.parent / "plots"
PLOT_SEARCH = ("system", "user")  # порядок: system первая, user переопределяет

# Живой список тем-обёрток (на него ссылается narrator.THEMES, каталог, создание мира)
THEMES: list[dict] = []
# plot_id -> raw-сюжет (для применения starting_state при создании мира)
_PLOTS: dict[str, dict] = {}
# Кэш сигнатур файлов (путь -> (mtime_ns, size)) — дешёвая проверка «изменилось что-то»
_SIGS: dict[str, tuple[int, int]] = {}

# ── Грубая транслитерация для стабильного id из имени файла (кириллица → латиница) ──
_TRANS = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "_", "ы": "y", "ь": "_", "э": "e", "ю": "yu", "я": "ya",
}
for _c in list(_TRANS):
    _TRANS[_c.upper()] = _TRANS[_c].upper()


def _safe_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _safe_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _as_dict(v) -> dict:
    return v if isinstance(v, dict) else {}


def _slug_id(filename: str) -> str:
    """Стабильный id из имени файла: транслитерация → a-z0-9_-.
    Если ничего не вышло (вдруг) — 'plot-<sha1(head)>'."""
    base = Path(filename).stem.lower()
    out = []
    for ch in base:
        if ch in _TRANS:
            out.append(_TRANS[ch])
        elif ch.isalnum() or ch in "-_":
            out.append(ch)
    s = re.sub(r"[^a-z0-9_-]+", "-", "".join(out)).strip("-_")
    return s or ("plot-" + hashlib.sha1(base.encode("utf-8")).hexdigest()[:8])


def _parse_lore_text(text: str) -> list[dict]:
    """Разбор lore_text ('## Заголовок' + текст) в статьи (если нет lore_articles)."""
    entries: list[dict] = []
    cur: list[str] = []
    cur_title = "Лор мира"
    for ln in (text or "").splitlines():
        m = re.match(r"^#{1,4}\s+(.+?)\s*#*\s*$", ln.strip())
        if m:
            if "".join(cur).strip():
                entries.append({"title": cur_title, "content": "\n".join(cur).strip()})
            cur_title, cur = m.group(1).strip(), []
        else:
            cur.append(ln)
    if "".join(cur).strip():
        entries.append({"title": cur_title, "content": "\n".join(cur).strip()})
    return [e for e in entries if str(e.get("content") or "").strip()]


def _theme_from_plot(pid: str, plot: dict, group: str = "user") -> dict:
    """Строит тему-обёртку из сюжета (схема PLOTS.md) — как запись THEMES."""
    meta = _as_dict(plot.get("metadata"))
    start_state = _as_dict(plot.get("starting_state"))
    story = _as_dict(plot.get("story"))
    plot_text = str(plot.get("plot_text") or "").strip()
    # Жанры (lower, только известные движку оставляем в подсказках)
    gens = [str(g).strip().lower() for g in (meta.get("genres") or []) if str(g).strip()]
    genre = ", ".join(gens) or "приключение"
    hints = " ".join(GENRE_HINTS.get(g, "") for g in gens if GENRE_HINTS.get(g)).strip()
    # Стартовая локация — из starting_state.locations по start_location_id
    start_loc = None
    sid = str(meta.get("start_location_id") or "").strip()
    for _loc in (start_state.get("locations") or []):
        if isinstance(_loc, dict) and str(_loc.get("id") or "") == sid:
            start_loc = {"name": str(_loc.get("name") or sid), "desc": str(_loc.get("desc") or "")}
            break
    # Лор: структурированные статьи или разбор lore_text
    lore_art = [a for a in (plot.get("lore_articles") or []) if isinstance(a, dict)]
    if not lore_art and str(plot.get("lore_text") or "").strip():
        lore_art = _parse_lore_text(str(plot.get("lore_text") or ""))
    opening = str(story.get("opening") or plot_text or "").strip()
    name = str(meta.get("name") or "").strip() or base_name(pid)
    inv = []
    for it in (start_state.get("inventory") or []):
        if isinstance(it, str) and it.strip():
            inv.append({"name": it.strip()[:60], "qty": 1, "desc": ""})
        elif isinstance(it, dict) and str(it.get("name") or "").strip():
            inv.append({"name": str(it["name"]).strip()[:60], "qty": max(1, _safe_int(it.get("qty"), 1)),
                        "desc": str(it.get("desc") or "").strip()[:300],
                        "weight": _safe_float(it.get("weight"))})
    goal = str(meta.get("logline") or "").strip() or str(meta.get("global_goal") or "").strip()[:240] or name
    # Рекомендуемый рассказчик для сюжета (имя пресета из plots/narrators/*.js) — опционально.
    narrator = str(plot.get("narrator") or "").strip()
    return {
        "id": pid,
        "name": name,
        "genre": genre,
        "desc": goal,
        # Сессия 63 («живой мир»): раньше здесь стояло «строго в русле этого сюжета ... веди
        # к глобальной цели по канве актов/квестов» — и рассказчик тащил прописанную канву,
        # даже когда игрок её уже обессмыслил (убил ключевого NPC, сжёг город). Теперь канва
        # названа тем, чем она является по замыслу: точкой отсчёта, а не единственным путём.
        # ЛОР остаётся законом (сеттинг — правда мира), сюжет — материалом (закон 1:
        # формулировка жанронейтральна и одинакова для стандартных и своих сюжетов).
        "style": ("Пиши живо и атмосферно, в русле этого мира: держи ЛОР (не противоречь "
                   "фактам вселенной и уже установленным событиям игры), раскрывай завязку и "
                   "глобальную цель как НАПРЯЖЕНИЕ сюжета, а не как обязательный маршрут. "
                   "Канва актов и квестов — точка отсчёта: действия игрока вправе свернуть её, "
                   "и тогда ты дописываешь историю сам, опираясь на то, что уже случилось."
                   + (" " + hints if hints else "")).rstrip(),
        "starter": {"gold": _safe_int(start_state.get("gold")), "inventory": inv},
        "start_location": start_loc or {"name": name, "desc": ""},
        "opening": opening,
        "lore": [{"title": str(a.get("title") or f"Лор {i}"),
                   "core": bool(a.get("core") or a.get("is_core")),
                   "content": str(a.get("content") or ""),
                   "tags": str(a.get("tags") or "")}
                  for i, a in enumerate(lore_art, 1)],
        "plot_text": plot_text,
        "group": group,          # "system" | "user"
        "is_plot": True,         # тема из файла сюжета (для создания мира)
        "plot_id": pid,
        "narrator": narrator or None,  # рекомендуемый рассказчик (имя) — резолвится в narrator_id в каталоге
        "plot": plot,            # raw-сюжет (для apply_plot_start) — только при создании мира
    }


# D15 (аудит 38): ошибки сканирования файлов сюжетов, видимые ПОЛЬЗОВАТЕЛЮ.
# Формат: {"file": "plots/user/мой-сюжет.js", "error": "короткое пояснение"}.
# Прежняя реакция на битый файл была одна — log.warning: игрок видел лишь «сюжетов стало
# меньше» без объяснения (plots/*.js — это JSON с расширением .js, соглашение PLOTS.md,
# поэтому на них не навешивается ни редакторная валидация, ни node --check). Реестр
# отдаётся в GET /api/themes → plots_errors и показывается плашкой в окне «Новый мир».
# Соглашение про расширение принято осознанно (вариант B аудита): переименование файлов
# в .json тронуло бы снапшоты id уже созданных миров, а выгода была бы косметической.
SCAN_ERRORS: list[dict] = []


def scan_errors() -> list[dict]:
    """Копия реестра битых файлов сюжета (для каталога/UI)."""
    return list(SCAN_ERRORS)


def _bad(path, why: str) -> None:
    """Зарегистрировать пропущенный файл сюжета (D15) — чтобы он был виден в UI."""
    try:
        rel = str(Path(path).relative_to(PLOTS_ROOT.parent))
    except Exception:
        rel = str(path)
    SCAN_ERRORS.append({"file": rel, "error": why[:300]})


def base_name(pid: str) -> str:
    """Человеческое имя из id (pepel-kontrakta → Пепел Контракта) — фолбэк, если в сюжете нет metadata.name."""
    return str(pid).replace("-", " ").replace("_", " ").strip().title() or pid


def _scan() -> tuple[list[dict], dict[str, dict]]:
    """Сканирует plots/system и plots/user. user переопределяет system при совпадении id.
    Один битый файл не роняет остальные (пропуск с warning) и не роняет reload целиком;
    сам пропуск запоминается в SCAN_ERRORS — см. D15."""
    themes_by_id: dict[str, dict] = {}
    raw_by_id: dict[str, dict] = {}
    SCAN_ERRORS.clear()
    for sub in PLOT_SEARCH:
        folder = PLOTS_ROOT / sub
        if not folder.is_dir():
            continue
        for f in sorted(folder.glob("*.js")):
            fpath = str(f)
            try:
                _SIGS[fpath] = (f.stat().st_mtime_ns, f.stat().st_size)
            except OSError:
                pass
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning("plots: пропущен файл %s (не JSON): %s", f, e)
                _bad(f, f"не читается как JSON: {e}")
                continue
            if not isinstance(data, dict):
                log.warning("plots: пропущен %s (не JSON-объект)", f)
                _bad(f, "файл — не JSON-объект (ожидаются поля схемы PLOTS.md)")
                continue
            story = _as_dict(data.get("story"))
            opening = str(story.get("opening") or "").strip() or str(data.get("plot_text") or "").strip()
            if not opening:
                log.warning("plots: пропущен %s (нет story.opening / plot_text — мир не сможет стартовать)", f)
                _bad(f, "пусто story.opening и plot_text — миру нечем стартовать")
                continue
            pid = str(data.get("id") or "").strip() or _slug_id(f.name)
            pid = re.sub(r"[^a-z0-9_-]+", "_", pid.lower()).strip("_") or _slug_id(f.name)
            if pid in themes_by_id:
                log.info("plots: id '%s' уже есть — файл %s переопределяет (главный — %s)",
                         pid, f, "user" if sub == "user" else "system")
            try:
                themes_by_id[pid] = _theme_from_plot(pid, data, group=sub)
            except Exception as e:
                log.warning("plots: пропущен %s (ошибка сборки темы): %s", f, e)
                _bad(f, f"файл не собирается в тему: {e}")
                continue
            raw_by_id[pid] = data
    themes = sorted(themes_by_id.values(),
                    key=lambda t: (t.get("group") != "user", str(t.get("name") or "").lower()))
    return themes, raw_by_id


def reload() -> dict:
    """Полная перезагрузка сюжетов с диска. Вызывается при старте и по кнопке «Обновить»."""
    global _SIGS
    _SIGS = {}
    themes, plots = _scan()
    THEMES.clear()
    THEMES.extend(themes)
    _PLOTS.clear()
    _PLOTS.update(plots)
    sys_n = sum(1 for t in THEMES if t.get("group") == "system")
    usr_n = len(THEMES) - sys_n
    if SCAN_ERRORS:
        # правило 14: молча потерять сюжет нельзя — считаем и пишем явно
        log.warning("plots reload: %d сюжетов (system=%d, user=%d), ОТОБРАНО %d битых: %s",
                    len(THEMES), sys_n, usr_n, len(SCAN_ERRORS),
                    "; ".join(f"{e['file']}: {e['error']}" for e in SCAN_ERRORS))
    else:
        log.info("plots reload: %d сюжетов (system=%d, user=%d)", len(THEMES), sys_n, usr_n)
    return {"total": len(THEMES), "system": sys_n, "user": usr_n,
            "broken": len(SCAN_ERRORS), "errors": scan_errors(),
            "ids": [t["plot_id"] for t in THEMES]}


def _mtime_changed() -> bool:
    """True, если набор файлов сюжетов изменился (новый/удалён/правлен).
    Учитывает и факт «папок нет» (если они появились — надо перечитать)."""
    seen: dict[str, tuple[int, int]] = {}
    for sub in PLOT_SEARCH:
        folder = PLOTS_ROOT / sub
        if not folder.is_dir():
            continue
        for f in folder.glob("*.js"):
            try:
                seen[str(f)] = (f.stat().st_mtime_ns, f.stat().st_size)
            except OSError:
                pass
    # Фолбэк: если папок/файлов нет, но _SIGS пуст и тем нет — считаем НЕ изменённым
    # (иначе ensure_fresh на пустой папке вызывал бы reload() на каждый GET /api/themes).
    if not seen and not _SIGS:
        return False
    return seen != _SIGS


def ensure_fresh() -> None:
    """Дешёвая проверка mtime — подхватывает новые/изменённые сюжеты БЕЗ перезапуска.
    Вызывается на каждом GET /api/themes. Полный ресканинг — по кнопке (reload())."""
    try:
        if _mtime_changed():
            reload()
    except Exception as e:
        log.warning("plots.ensure_fresh: %s", e)


def get_theme(theme_id) -> dict | None:
    """Тема-обёртка сюжета по id (или None)."""
    if not theme_id:
        return None
    return next((t for t in THEMES if t.get("id") == theme_id), None)


def get_plot(plot_id) -> dict | None:
    """Сырой сюжет по id (для применения стартового состояния при создании мира)."""
    return _PLOTS.get(str(plot_id))


# Загрузка при импорте (движок стартует уже с сюжетами)
try:
    reload()
except Exception as e:
    log.warning("plots init: %s", e)
