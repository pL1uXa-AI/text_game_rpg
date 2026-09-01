# -*- coding: utf-8 -*-
"""journal.py — Дневник приключений (сессия 34, C2).

Авто-хроника ЗНАЧИМОГО: новый квест / его итог, первая встреча с персонажем, уникальная
находка, поворотный флаг, победа над врагом, повышение уровня/класса/профессии, открытие
локации, завершение игры. Записывается ПОСЛЕ хода детерминированно (без LLM — дёшево и
не выдумывает), хранится карточками kind="journal" (та же таблица entities, что и карточки
знаний) и отдаётся в UI с переходом к ходу по seq.

Законы архитектуры:
  * закон 2 — это отображение/хроника, движок ничего не решает;
  * закон 3 — формулировки записей нейтральны («квест принят», а не «ты обязан»); смысл
    и продолжение остаются за рассказчиком;
  * закон 1 — никаких жанровых слов: только общие категории (квест/персонаж/предмет/
    флаг/бой/роль/место/финал).

Почему не LLM-архивариус: он и так вызывается каждый ход; добавлять ему вторую роль —
значит плодить токены и галлюцинации в Chronicle, где нужна точность фактов.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from . import db
from .logsetup import get_logger

log = get_logger(__name__)


def _stable_key(*parts: str) -> str:
    """Детерминированный ключ из частей (сессия 35, баг 3).

    Раньше здесь был `abs(hash(title))` — а `hash()` рандомизирован (PYTHONHASHSEED),
    поэтому после рестарта сервера тот же ход/заголовок давал ДРУГОЙ ключ: дубликаты
    записей дневника и сломанный дедуп перегенерации. md5 от строки стабилен между
    процессами и машинами.
    """
    return hashlib.md5("\x1f".join(parts).encode("utf-8")).hexdigest()[:12]

KIND = "journal"
# категории (для иконки/фильтра в UI). note — заметка самого игрока (/journal note …)
CAT_QUEST = "quest"
CAT_NPC = "npc"
CAT_ITEM = "item"
CAT_FLAG = "flag"
CAT_COMBAT = "combat"
CAT_ROLE = "role"
CAT_PLACE = "place"
CAT_WORLD = "world"
CAT_NOTE = "note"

_ICON = {CAT_QUEST: "📜", CAT_NPC: "👤", CAT_ITEM: "🎒", CAT_FLAG: "🚩",
         CAT_COMBAT: "⚔️", CAT_ROLE: "⭐", CAT_PLACE: "🗺", CAT_WORLD: "🌍",
         CAT_NOTE: "✍️"}


def _as_dict(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def _keys(d: Any) -> set:
    return set((d or {}).keys()) if isinstance(d, dict) else set()


def notable_diff(prev: dict, now: dict, action: str = "", sys_msgs: Optional[list[str]] = None
                 ) -> list[dict]:
    """Что из этого хода достойно дневника. Чистая функция над «до/после».

    Возвращает [{cat, title, text, seq?}] — порядок = порядок записи.
    """
    prev, now = _as_dict(prev), _as_dict(now)
    out: list[dict] = []
    if not now:
        return out

    # ── квесты: принятие / шаг / итог ──
    pq, nq = _as_dict(prev.get("quests")), _as_dict(now.get("quests"))
    for qid in nq:
        q = _as_dict(nq[qid])
        old = _as_dict(pq.get(qid))
        title = str(q.get("title") or qid)[:80]
        if not old:
            out.append({"cat": CAT_QUEST, "title": f"Новое задание: {title}",
                        "text": str(q.get("desc") or "")[:220]})
        elif q.get("status") != old.get("status"):
            st = str(q.get("status") or "")
            if st in ("done", "success"):
                tail = f" — {q['outcome_reason']}" if q.get("outcome_reason") else ""
                out.append({"cat": CAT_QUEST, "title": f"Завершено: {title}{tail}",
                            "text": "Ты довёл дело до конца."})
            elif st == "failed":
                tail = f" — {q['outcome_reason']}" if q.get("outcome_reason") else ""
                out.append({"cat": CAT_QUEST, "title": f"Провалено: {title}{tail}",
                            "text": "Так повернулось, что не вышло."})
            elif st == "active" and str(old.get("status") or "") in ("done", "failed", "success"):
                out.append({"cat": CAT_QUEST, "title": f"Снова в деле: {title}", "text": ""})
        elif q.get("progress") and q.get("progress") != old.get("progress"):
            out.append({"cat": CAT_QUEST, "title": f"{title}: {str(q['progress'])[:90]}",
                        "text": "Шаг пройден."})

    # ── персонажи: первая встреча и смерть ──
    pn, nn = _as_dict(prev.get("npc")), _as_dict(now.get("npc"))
    for nid in nn:
        v = _as_dict(nn[nid])
        name = str(v.get("name") or nid)[:60]
        old = pn.get(nid)
        if old is None:
            note = f" ({v['desc'][:90]})" if v.get("desc") else ""
            out.append({"cat": CAT_NPC, "title": f"Новое знакомство: {name}{note}",
                        "text": str(v.get("mood") or "")[:80], "subject": name})
        elif _as_dict(old).get("alive", True) and v.get("alive") is False:
            out.append({"cat": CAT_NPC, "title": f"{name} мёртв", "text": "",
                        "subject": name})
        elif (v.get("notes") or {}) and isinstance(v.get("notes"), dict) and \
                (v["notes"] != _as_dict(old).get("notes")):
            # заметки мастера изменились — значит NPC что-то знает/скрыл/раскрыл
            changed = [k for k, val in v["notes"].items()
                       if _as_dict(old).get("notes", {}).get(k) != val]
            if changed:
                out.append({"cat": CAT_NPC, "title": f"{name}: открылось новое",
                            "text": ", ".join(str(c) for c in changed[:3])[:120]})

    # ── вещи: первые уникальные находки (не считаем повторы) ──
    def inv_names(st: dict) -> dict:
        p = _as_dict(st.get("player"))
        res: dict[str, dict] = {}
        for i in (p.get("inventory") or []):
            if isinstance(i, dict) and i.get("name"):
                res[str(i["name"])] = i
        return res

    pi, ni = inv_names(prev), inv_names(now)
    for name in ni:
        if name not in pi:
            it = _as_dict(ni[name])
            desc = f" — {it['desc'][:90]}" if it.get("desc") else ""
            out.append({"cat": CAT_ITEM, "title": f"Впервые в руках: {name}{desc}",
                        "text": "", "subject": str(name)[:60]})

    # ── флаги-истины: только новые (поворотные по определению) ──
    pf, nf = _as_dict(prev.get("flags")), _as_dict(now.get("flags"))
    for k in nf:
        if k not in pf and nf[k] is not False:
            out.append({"cat": CAT_FLAG, "title": f"Так в мире и осталось: {k}",
                        "text": "", "subject": str(k)[:60]})

    # ── бой: кто повержен (счётчик убийств + исчезновение врага) ──
    pe, ne = _as_dict(prev.get("enemies")), _as_dict(now.get("enemies"))
    killed = [str(_as_dict(pe[eid]).get("name") or eid) for eid in pe if eid not in ne]
    if killed:
        out.append({"cat": CAT_COMBAT,
                    "title": "Поле боя: " + ", ".join(killed[:4]) + " повержен"
                            + ("ы" if len(killed) > 1 else ""), "text": ""})

    # ── роль: уровень/класс/профессия/звание/титул/достижение ──
    pp, np_ = _as_dict(prev.get("player")), _as_dict(now.get("player"))
    for field, label in (("level", "Уровень"), ("class", "Класс"), ("race", "Раса"),
                         ("profession", "Ремесло"), ("secondary_class", "Второй путь")):
        if np_.get(field) and np_.get(field) != pp.get(field):
            out.append({"cat": CAT_ROLE, "title": f"{label}: {np_[field]}", "text": ""})
    for t in (_as_list(np_.get("titles")) or []):
        if t not in (_as_list(pp.get("titles")) or []):
            out.append({"cat": CAT_ROLE, "title": f"Титул: {t}", "text": ""})
    for a in (_as_list(np_.get("achievements")) or []):
        name = a.get("name") if isinstance(a, dict) else str(a)
        old_names = [(x.get("name") if isinstance(x, dict) else str(x))
                     for x in (_as_list(pp.get("achievements")) or [])]
        if name and name not in old_names:
            out.append({"cat": CAT_ROLE, "title": f"Достижение: {name}",
                        "text": (a.get("desc") or "")[:160] if isinstance(a, dict) else ""})
    # способности
    pa, na = _as_dict(pp.get("abilities")), _as_dict(np_.get("abilities"))
    for name in na:
        if name not in pa:
            out.append({"cat": CAT_ROLE, "title": f"Освоен приём: {name}",
                        "text": str(_as_dict(na[name]).get("desc") or "")[:160]})

    # ── места: первая локация на карте и приход в неё ──
    pl, nl = _as_dict(prev.get("locations")), _as_dict(now.get("locations"))
    for lid in nl:
        if lid not in pl:
            _lname = str(_as_dict(nl[lid]).get("name") or lid)[:60]
            out.append({"cat": CAT_PLACE, "title": f"Открыто новое место: {_lname}",
                        "text": "", "subject": _lname})
    cur_now = str(now.get("current_location") or "")
    if cur_now and cur_now != str(prev.get("current_location") or "") and _as_dict(nl.get(cur_now)):
        nm = str(_as_dict(nl[cur_now]).get("name") or cur_now)[:60]
        out.append({"cat": CAT_PLACE, "title": f"Ты добрался: {nm}", "text": ""})

    # ── мир: смерть/финал, сезон, доска, таймер ──
    if now.get("game_over") and not prev.get("game_over"):
        out.append({"cat": CAT_WORLD, "title": "Путь оборвался", "text": ""})
    pd_, nd_ = _as_dict(prev.get("date")), _as_dict(now.get("date"))
    if nd_.get("season") and nd_["season"] != pd_.get("season"):
        out.append({"cat": CAT_WORLD, "title": f"Сменился сезон: {nd_['season']}", "text": ""})
    pt, nt = _as_dict(prev.get("timers")), _as_dict(now.get("timers"))
    for name in pt:
        if name not in nt:
            out.append({"cat": CAT_WORLD, "title": f"Срок вышел: {name}", "text": ""})
    return out


def _as_list(v: Any) -> list:
    return v if isinstance(v, list) else []


def record_turn(world_id: int, prev: dict, now: dict, seq: int, action: str = "",
                sys_msgs: Optional[list[str]] = None) -> list[dict]:
    """Записать значимое этого хода в дневник (карточки kind="journal").

    Возвращает сохранённые карточки. Детерминированно и best-effort: дневник не должен
    ронять ход (ошибка — в лог, правило 14).
    """
    saved: list[dict] = []
    try:
        diff = notable_diff(prev, now, action, sys_msgs)
        log.debug("дневник (world %s, seq %s): значимых событий %d", world_id, seq, len(diff))
        for item in diff:
            cat = str(item.get("cat") or CAT_WORLD)
            title = str(item.get("title") or "").strip()[:120]
            if not title:
                continue
            # ключ стабилен по (ход, категория, заголовок): перегенерация того же хода
            # перезапишет свою запись, а не заведёт вторую
            key = f"t{seq}-{cat}-{_stable_key(title)}"
            ent = db.upsert_entity(world_id, KIND, key, name=title,
                                   summary=str(item.get("text") or "")[:220],
                                   meta={"seq": seq, "cat": cat, "icon": _ICON.get(cat, "•"),
                                         "subject": str(item.get("subject") or "")[:60]},
                                   seq=seq)
            if ent:
                saved.append(ent)
    except Exception as e:
        log.warning("дневник (world %s, seq %s): запись не удалась: %s", world_id, seq, e)
    return saved


def add_player_note(world_id: int, note: str) -> Optional[dict]:
    """Заметка игрока (/journal note …). Закон 2: код хранит и показывает, смысл за ним."""
    note = (note or "").strip()[:400]
    if not note:
        return None
    seq = db.latest_seq(world_id)
    return db.upsert_entity(world_id, KIND, f"t{seq}-note-{_stable_key(note)}",
                            name=note, summary="", seq=seq,
                            meta={"seq": seq, "cat": CAT_NOTE, "icon": _ICON[CAT_NOTE]})


def entries(world_id: int, limit: int = 100, cat: str = "") -> list[dict]:
    """Записи дневника хронологически: [{seq, cat, icon, title, text}]."""
    rows = db.list_entities(world_id, kind=KIND)
    out: list[dict] = []
    for r in rows:
        try:
            meta = json.loads(r.get("meta") or "{}")
        except Exception:
            meta = {}
        c = str(meta.get("cat") or CAT_WORLD)
        if cat and c != cat:
            continue
        out.append({"seq": int(meta.get("seq") or r.get("seq") or 0), "cat": c,
                    "icon": str(meta.get("icon") or _ICON.get(c, "•")),
                    "title": r.get("name") or "", "text": r.get("summary") or "",
                    "subject": str(meta.get("subject") or ""),
                    "key": r.get("entity_key")})
    out.sort(key=lambda x: (x["seq"], x["cat"]))
    return out[-max(1, int(limit)):] if limit else out


def entry_categories(world_id: int) -> list[dict]:
    """Категории дневника с числом записей (для фильтров во вкладке)."""
    counts: dict[str, int] = {}
    for it in entries(world_id, limit=0):
        c = str(it.get("cat") or CAT_WORLD)
        counts[c] = counts.get(c, 0) + 1
    return [{"cat": c, "icon": _ICON.get(c, "•"), "count": n}
            for c, n in sorted(counts.items(), key=lambda kv: -kv[1])]


def render(world_id: int, limit: int = 40) -> str:
    """Текстовая хроника для /journal (чистое отображение — закон 2)."""
    items = entries(world_id, limit=limit)
    if not items:
        return ("📔 Дневник пока пуст — в него попадают знаковые события: первые встречи, "
                "квесты и их итог, находки, смены роли, новые места.")
    L = ["📔 Дневник приключений:"]
    last_turn = None
    for it in items:
        head = f"— ход {it['seq']} —" if it["seq"] != last_turn else None
        if head:
            L.append(head)
            last_turn = it["seq"]
        L.append(f"  {it['icon']} {it['title']}" + (f"\n      {it['text']}" if it["text"] else ""))
    return "\n".join(L)


# ── «Чеховские ружья» (сессия 34, C9) ───────────────────────────────────────
# Заряженные, но ещё НЕ прозвучавшие намёки: знакомство / предмет / флаг / место, о
# которых мир с тех пор не заикался. Движок лишь ДЕРЖИТ список и показывает его в
# состоянии («на горизонте…») — выстрелит ружьё, когда и как, решает мастер
# (законы 2/3: отображение + подсказка, никаких сюжетных решений за рассказчиком).
_CHEKHOV_CATS = (CAT_NPC, CAT_ITEM, CAT_FLAG, CAT_PLACE)
_CHEKHOV_MAX = 6           # сколько ружей висит одновременно
_CHEKHOV_TTL = 12          # ходов, после которых ружьё гаснет само (не вечно)


def _mentioned(low: str, name: str) -> bool:
    """Упомянуто ли имя в тексте (без регистра; значимая часть имени тоже считается).

    «Старый компас» ищется и как целиком, и по самому длинному слову («компас») — иначе
    ружьё никогда не снималось бы со стены: персонажей и вещи называют коротко.
    Имена флагов (snake_case) сравниваются ещё и со словами после «_».
    """
    n = str(name or "").lower().strip()
    if len(n) < 3:
        return False
    if n in low:
        return True
    words = [w for w in n.replace("_", " ").split() if len(w) >= 4]
    return any(w in low for w in words)


def chekhov_update(setting: dict, prev: dict, now: dict, reply: str = "",
                   action: str = "", seq: int = 0) -> list[dict]:
    """Продвинуть список ружей после хода: прозвучавшие — снять, новые значимые — зарядить.

    Возвращает актуальный список (он же кладётся в setting["_chekhov"]).
    """
    guns = [dict(g) for g in (setting.get("_chekhov") or [])
            if isinstance(g, dict) and g.get("subject")]
    low = ((reply or "") + "\n" + (action or "")).lower()

    alive: list[dict] = []
    for g in guns:
        if _mentioned(low, g.get("subject")):
            continue                        # ружьё прозвучало — снимаем со «стены»
        g["age"] = int(g.get("age") or 0) + 1
        if g["age"] <= _CHEKHOV_TTL:
            alive.append(g)

    known = {str(g.get("subject", "")).lower() for g in alive}
    for item in notable_diff(prev, now, action):
        cat = str(item.get("cat") or "")
        subj = str(item.get("subject") or "").strip()
        if cat not in _CHEKHOV_CATS or not subj or subj.lower() in known:
            continue
        if _mentioned(low, subj):
            continue                        # уже прозвучало в этом же ходу
        alive.append({"subject": subj[:60], "cat": cat,
                      "title": str(item.get("title") or subj)[:100],
                      "seq": int(seq or 0), "age": 0})
        known.add(subj.lower())

    setting["_chekhov"] = alive[-_CHEKHOV_MAX:]
    return setting["_chekhov"]


def chekhov_text(setting: dict) -> str:
    """Строка для format_state: что «на горизонте» (пусто — ничего не висит)."""
    guns = [g for g in (setting.get("_chekhov") or []) if isinstance(g, dict) and g.get("subject")]
    if not guns:
        return ""
    parts = []
    for g in guns[-_CHEKHOV_MAX:]:
        icon = _ICON.get(str(g.get("cat") or ""), "•")
        age = int(g.get("age") or 0)
        parts.append(f"{icon} {g['subject']}" + (f" (висит {age} х.)" if age else ""))
    return "; ".join(parts)


__all__ = ["KIND", "record_turn", "notable_diff", "entries", "render",
           "add_player_note", "chekhov_update", "chekhov_text", "entry_categories",
           "CAT_NOTE", "CAT_QUEST", "CAT_NPC", "CAT_ITEM", "CAT_FLAG",
           "CAT_COMBAT", "CAT_ROLE", "CAT_PLACE", "CAT_WORLD"]
