# -*- coding: utf-8 -*-
"""check_plot.py — автоматическая проверка файла сюжета (plots/*.js) по правилам PLOTS.md.

Запуск:
    D:/Development/Development_Tools/Runtimes/Python/3.12.10/python.exe -X utf8 scripts/check_plot.py plots/system/<file>.js

Проверяет (по чек-листу PLOTS.md):
  [1] валидность JSON и наличие обязательных ключей;
  [2] new_locations в главах — ТОЛЬКО новые (не из starting_state.locations);
  [3] key_quests — только id, определённые в story.quest_chains (нет призрачных ссылок);
  [4] key_npcs — только id, определённые в starting_state.npcs;
  [5] connections — только существующие локации (нет рёбер в никуда);
  [5b] в тексте steps/branches квестов нет несуществующих id (призрачных «ветка <id>»);
  [6] relations фракций — только «союз»/«враг», ссылки на существующие фракции;
  [7] статусы квестов — только active/done;
  [8] key_flags_to_set — только события со значением true (флаг «не произошло» = false запрещён);
  [9] next_quest_id — ссылается на существующий квест или null;
  [10] start_location_id — существует в starting_state.locations;
  [11] faction у NPC/магазинов — определена в factions;
  [12] lore_articles и lore_text согласованы по заголовкам (warning);
  [13] narrator (рекомендуемый рассказчик сюжета) — имя существует в plots/narrators/*.js (warning).

Возвращает код 0 при полном соответствии, 1 при нарушениях (выводит список),
2 при неверном вызове.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

VALID_STANCES = {"союз", "враг", "ally", "enemy"}
VALID_STATUSES = {"active", "done"}
REQUIRED_TOP = {"metadata", "lore_articles", "lore_text", "factions",
                "starting_state", "story", "plot_text", "handoff_directives"}


def _ids(items, key: str) -> set:
    return {str(x.get("id") or "").strip() for x in items
            if isinstance(x, dict) and str(x.get("id") or "").strip()}


def check(path: Path) -> int:
    if not path.exists():
        print(f"✗ файл не найден: {path}")
        return 1
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"✗ файл не является валидным JSON: {e}")
        return 1
    errs: list[str] = []
    warns: list[str] = []

    def bad(msg: str) -> None:
        errs.append(msg)

    # [1] обязательные ключи
    missing = REQUIRED_TOP - set(d.keys())
    if missing:
        bad(f"отсутствуют обязательные ключи: {', '.join(sorted(missing))}")

    ss = d.get("starting_state") or {}
    story = d.get("story") or {}
    meta = d.get("metadata") or {}

    locs = _ids(ss.get("locations") or [], "id")
    fids = _ids(d.get("factions") or [], "id")
    npcs = _ids(ss.get("npcs") or [], "id")
    qids = _ids(story.get("quest_chains") or [], "id")
    shop_ids = _ids(ss.get("shops") or [], "id")

    # [2] new_locations — только новые
    for act in story.get("acts") or []:
        for ch in act.get("chapters") or []:
            cid = ch.get("id")
            for nl in ch.get("new_locations") or []:
                if nl in locs:
                    bad(f"new_locations '{nl}' в главе {cid}: локация УЖЕ есть в starting_state.locations — "
                        f"убери из new_locations (п.4.1: только реально новые)")

    # [3] key_quests — только определённые
    for act in story.get("acts") or []:
        for ch in act.get("chapters") or []:
            cid = ch.get("id")
            for kq in ch.get("key_quests") or []:
                if kq not in qids:
                    bad(f"key_quests '{kq}' в главе {cid}: нет такого квеста в story.quest_chains — "
                        f"призрачная ссылка (п.5.3): определи квест или убери ссылку")

    # [4] key_npcs — только определённые
    for act in story.get("acts") or []:
        for ch in act.get("chapters") or []:
            cid = ch.get("id")
            for kn in ch.get("key_npcs") or []:
                if kn not in npcs:
                    bad(f"key_npcs '{kn}' в главе {cid}: нет такого NPC в starting_state.npcs — "
                        f"призрачная ссылка (п.5.3): определи NPC или убери ссылку")

    # [5] connections — только существующие локации
    for loc in ss.get("locations") or []:
        if not isinstance(loc, dict):
            continue
        lid = loc.get("id")
        for c in loc.get("connections") or []:
            if c not in locs:
                bad(f"connections '{lid}' -> '{c}': нет такой локации (ребро в никуда ломает карту)")

    # [5b] подозрительные токены-идентификаторы в ТЕКСТЕ steps/branches квестов.
    # Ссылка на НЕСУЩЕСТВУЮЩИЙ id (напр. "ветка arcadia_heist", а такого квеста нет) —
    # призрак: движок по тексту ничего не создаст, судья логики может «увидеть» несуществующее.
    # Проверка: токен в тексте, похожий на snake_case id, но отсутствующий в пуле известных id.
    id_pool = qids | locs | npcs | fids | shop_ids
    for q in story.get("quest_chains") or []:
        if not isinstance(q, dict):
            continue
        qid = q.get("id")
        texts = []
        texts += [str(s) for s in (q.get("steps") or []) if isinstance(s, str)]
        texts += [str(v) for v in (q.get("branches") or {}).values() if isinstance(v, str)]
        texts += [str(k) for k in (q.get("branches") or {}).keys()]
        for t in texts:
            for tok in re.findall(r"[a-z][a-z0-9_]{2,}", t.lower()):
                if tok in id_pool:
                    continue  # существующий id — нормальная осознанная ссылка
                if tok in ("взлом", "задание", "культ", "аркадия", "тени", "npc", "npcs", "cultists", "id"):
                    continue  # русские слова/термины, прошедшие транслитерацию — шум
                # эвристика: если токен похож на id (не слово русского текста, латиница+_) — призрак
                bad(f"quest '{qid}': текст steps/branches упоминает несуществующий id '{tok}' — "
                    f"призрачная ссылка (п.5.3): определи сущность или опиши ветку словами")

    # [6] relations фракций
    for f in d.get("factions") or []:
        if not isinstance(f, dict):
            continue
        fid = f.get("id")
        for other, stance in (f.get("relations") or {}).items():
            if other not in fids:
                bad(f"relations {fid} -> '{other}': нет такой фракции в factions (призрачная репутация)")
            if str(stance).strip().lower() not in VALID_STANCES:
                bad(f"relations {fid} -> '{other}': недопустимая связь '{stance}' — "
                    f"только «союз»/«враг» (нейтрально = отсутствие записи)")

    # [7] статусы квестов
    for q in story.get("quest_chains") or []:
        if isinstance(q, dict) and str(q.get("status") or "").strip() not in VALID_STATUSES:
            bad(f"quest '{q.get('id')}': статус '{q.get('status')}' — только active/done (п.4.2)")

    # [8] key_flags_to_set — только true (свершившиеся события)
    for act in story.get("acts") or []:
        for ch in act.get("chapters") or []:
            cid = ch.get("id")
            for k, v in (ch.get("key_flags_to_set") or {}).items():
                if not isinstance(v, bool) or v is not True:
                    bad(f"key_flags_to_set '{k}' в главе {cid}: значение должно быть true "
                        f"(флаг фиксирует свершившееся событие; false ничего не запоминает — п.4.4)")

    # [9] next_quest_id — существующий квест или null
    for q in story.get("quest_chains") or []:
        if not isinstance(q, dict):
            continue
        nxt = q.get("next_quest_id")
        if nxt not in (None, "") and nxt not in qids:
            bad(f"quest '{q.get('id')}': next_quest_id '{nxt}' — нет такого квеста в quest_chains")

    # [10] start_location_id
    sid = str(meta.get("start_location_id") or "").strip()
    if sid and sid not in locs:
        bad(f"metadata.start_location_id '{sid}' не найден в starting_state.locations")

    # [11] faction у NPC/магазинов
    for n in ss.get("npcs") or []:
        if isinstance(n, dict) and n.get("faction") and n["faction"] not in fids:
            bad(f"npc '{n.get('id')}': faction '{n['faction']}' не определена в factions")
    for s in ss.get("shops") or []:
        if isinstance(s, dict) and s.get("faction") and s["faction"] not in fids:
            bad(f"shop '{s.get('id')}': faction '{s['faction']}' не определена в factions")
    for s in ss.get("shops") or []:
        if isinstance(s, dict) and s.get("location") and s["location"] not in locs:
            bad(f"shop '{s.get('id')}': location '{s['location']}' не определена в starting_state.locations")

    # [12] согласованность lore_articles и lore_text (warning, не ошибка)
    art_titles = {str(a.get("title") or "").strip() for a in (d.get("lore_articles") or []) if isinstance(a, dict)}
    text_titles = {m.group(1).strip() for m in
                   re.finditer(r"^#{1,4}\s+(.+?)\s*#*\s*$", d.get("lore_text") or "", re.M)}
    if art_titles and text_titles and art_titles != text_titles:
        only_art = art_titles - text_titles
        only_text = text_titles - art_titles
        if only_art or only_text:
            warns.append(f"lore_articles и lore_text рассинхронизированы: "
                         f"только в articles: {sorted(only_art)[:3]}; только в text: {sorted(only_text)[:3]}")

    # [13] рекомендуемый рассказчик — имя существует среди пресетов (warning, не ошибка)
    rec_narr = str(d.get("narrator") or "").strip()
    if rec_narr:
        nar_dir = Path(__file__).resolve().parent.parent / "plots" / "narrators"
        known = set()
        if nar_dir.is_dir():
            for nf in nar_dir.glob("*.js"):
                try:
                    nd = json.loads(nf.read_text(encoding="utf-8"))
                    if isinstance(nd, dict) and str(nd.get("name") or "").strip():
                        known.add(str(nd["name"]).strip())
                except Exception:
                    pass
        if rec_narr not in known:
            warns.append(f"narrator '{rec_narr}' не найден среди пресетов plots/narrators/*.js — "
                         f"поле не подставится при выборе сюжета (известные: {sorted(known)[:8]})")

    # ── вывод ──
    if errs:
        print(f"✗ Найдено {len(errs)} нарушений в {path.name}:")
        for i, e in enumerate(errs, 1):
            print(f"  {i}. {e}")
        return 1
    print(f"✓ {path.name}: все проверки пройдены (целостность id, new_locations, флаги, квесты, фракции).")
    for w in warns:
        print(f"  ⚠ {w}")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print("Использование: python -X utf8 scripts/check_plot.py plots/<folder>/<file>.js [ещё файлы...]")
        return 2
    rc = 0
    for arg in sys.argv[1:]:
        rc |= check(Path(arg))
    return rc


if __name__ == "__main__":
    sys.exit(main())
