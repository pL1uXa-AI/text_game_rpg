# -*- coding: utf-8 -*-
"""rewind.py — перемотка таймлайна мира к ходу N (сессия 34: A1, A4, C1).

Один честный механизм отката вместо трёх кривых. Его вызывают:
  * «назад к ходу N» в UI (новое, C1);
  * загрузка слота сохранения (было: `mark_folded(≤seq)` + цикл `mark_folded(seq)` для
    каждого будущего события → свёрнутыми оказывались ВСЕ обмены, недавняя история мира
    умирала безвозвратно — баг A1);
  * будущие правки (перегенерация остаётся отдельной, более лёгкой механикой — она
    заменяет последний ход, а не откатывает таймлайн).

Что делает перемотка и почему именно так:
  1. состояние мира берётся из `turn_snapshots` (снимок ДО хода N) — без угадывания;
  2. события хода N и «после» него удаляются (их больше нет в таймлайне);
  3. сводки, покрывающие удалённый диапазон, удаляются — иначе рассказчик «помнит»
     события, которых уже не было;
  4. всё, что было свёрнуто в эти удалённые сводки, РАЗВОРАЧИВАЕТСЯ (folded=0) — история
     возвращается в недавнее окно (обратимость — суть фикса A1);
  5. векторы удалённых обменов/сводок вычищаются из ChromaDB (A4: память не должна помнить
     отменённое; ошибки чистки логируются, а не глотаются);
  6. мир помечает точку отсчёта и рассылает по шине событие `rewound` (+ метку
     `meta.rewound` на системной строке отката) — все открытые вкладки перерисовывают лог,
     а не дорисовывают его к устаревшему. Запасной путь (поллинг без SSE) доносит тот же
     сигнал через метку события — иначе вкладка без живого соединения показывала бы удалённые
     ходы до F5 (A11, аудит 41).

Законы архитектуры: перемотка — служебная операция пользователя над собственным
прохождением (как загрузка сохранения); решений за мастера движок не принимает.
"""
from __future__ import annotations

import json
from typing import Optional

from . import bg, bus, chroma_client, db
from .logsetup import get_logger, turn_context

log = get_logger(__name__)


class RewindBusy(RuntimeError):
    """Мир сейчас недосягаем для отката: идёт ход игрока, ↻ или другой откат (A5).

    Поднимается ДО любой записи в БД — роутер превращает его в честный 409, а мир
    остаётся ровно таким, каким был (ни событий, ни состояния не тронуто).
    """


def _folded_ranges_covering(world_id: int, from_seq: int) -> list[dict]:
    """Сводки, чей покрытый диапазон заходит за `from_seq` (их данные устаревают при откате).

    Сводки без meta.covers (миры, созданные до сессии 34) считаем покрывающими всё, что
    раньше их seq: консервативно — такую сводку не разворачиваем, но и не удаляем вслепую.
    """
    out = []
    for s in db.get_summary_events(world_id, limit=0, unfolded_only=False):
        meta = s.get("meta") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        covers = (meta or {}).get("covers") or {}
        out.append({"seq": s["seq"], "id": s["id"],
                    "from": int(covers.get("from") or 0) or None,
                    "to": int(covers.get("to") or 0) or None})
    return out


async def _purge_vectors(world_id: int, seqs: list[int], summary_seqs: list[int]) -> None:
    """Вычистить векторы удалённых обменов и сводок из обеих коллекций ChromaDB (A4).

    Ошибку НЕ глотаем: логгируем (правило 14) и продолжаем — удалённые события в SQLite
    уже не вернуть, а недоступная ChromaDB не должна отменять перемотку мира.
    """
    ids = [f"ex_{world_id}_{s}" for s in seqs] + [f"sum_{world_id}_{s}" for s in summary_seqs]
    if not ids:
        return
    try:
        await chroma_client.delete_by_ids(ids)
    except Exception as e:
        log.warning("перемотка (world %s): векторы %d удалённых событий остались в Chroma: %s",
                    world_id, len(ids), e)


async def rewind_to(world_id: int, before_seq: int, setting: Optional[dict] = None,
                    mode: str = "delete", note: str = "⏪ Перемотка") -> dict:
    """Вернуть мир к состоянию ПЕРЕД ходом `before_seq` (события с seq >= before_seq уходят).

    `setting` — состояние для записи (по умолчанию — снапшот этого хода из БД).
    `mode`:
      * "delete" — ходы после точки УДАЛЯЮТСЯ (чистая перемотка: таймлайн не врёт);
      * "hide"   — остаются в БД, но сокрыты (folded=FOLD_HIDDEN) и исключены из промпта;
        так работает загрузка сохранения — строки журнала не гибнут от промаха по кнопке.
    В обоих режимах всё, что было свёрнуто в ставшие недостоверными сводки, разворачивается
    обратно в недавнее окно (обратимость — суть фикса A1), а векторы удалённого вычищаются
    из ChromaDB (A4).

    A5 (аудит 41): весь откат идёт под барьером `bg.rewind_block` — тот же `_regen`, что и у ↻.
    Без него перемотка «догоняла» идущий ход и фоновые агенты: откат случался раньше, чем ход
    дописал события/setting, и мир сразу после «перемотки» уезжал в будущее. Теперь такой
    откат невозможен: занятый мир отдаёт `RewindBusy` (роутер превращает его в 409) БЕЗ
    единой записи в БД.
    """
    with bg.rewind_block(world_id) as ok:
        if not ok:
            raise RewindBusy(
                "По этому миру сейчас идёт ход или перегенерация — перемотка/загрузка не "
                "могла бы честно догнать таймлайн. Дождись ответа и повтори.")
        return await _rewind_to(world_id, before_seq, setting=setting, mode=mode, note=note)


async def _rewind_to(world_id: int, before_seq: int, setting: Optional[dict] = None,
                     mode: str = "delete", note: str = "⏪ Перемотка") -> dict:
    """Тело отката — вызывается только из `rewind_to`, под барьером."""
    hide = mode == "hide"
    seq = int(before_seq)
    with turn_context(world_id=world_id, seq=seq, agent="rewind"):
        world = db.get_world(world_id)
        if not world:
            raise LookupError("Мир не найден")
        latest = db.latest_seq(world_id)
        if seq <= 0:
            raise ValueError("Нужен положительный номер хода")
        if seq > latest + 1:
            raise ValueError(f"Хода {seq} ещё нет (последний — {latest})")

        if setting is None:
            setting = db.get_turn_snapshot(world_id, seq)
            if setting is None:
                # Точка перемотки не сохранена (мир создан до сессии 34 либо снапшоты
                # срезаны политикой хранения). Честно говорим пользователю, вместо того
                # чтобы молча откатить таймлайн и оставить рассинхрон с состоянием.
                raise ValueError(
                    "Нет снимка состояния для этого хода — перемотка недоступна. "
                    "Используй загрузку сохранения или начни новый проход.")

        # 1) сводки, которые перестают быть достоверными, и их диапазоны.
        #    Берём ВСЕ сводки (включая уже сокрытые): stale — те, чьё покрытие заходит
        #    за точку отката или чей seq ≥ точки (сводка о «будущем»).
        summaries = _folded_ranges_covering(world_id, seq)
        stale = [s for s in summaries if (s["to"] is not None and s["to"] >= seq) or s["seq"] >= seq]
        # 2) какие seq были свёрнуты этими сводками → их надо развернуть
        unfold_from = min([s["from"] for s in stale if s["from"]] or [seq])
        # 3) убираем события начиная с точки отката
        doomed = db.get_turn_events(world_id, seq, None, roles=("player", "narrator", "summary"),
                                    unfolded_only=False)
        exchange_seqs = sorted({e["seq"] for e in doomed if e["role"] in ("player", "narrator")})
        if hide:
            # будущее — сокрыть (folded=2), включая сводки с seq ≥ точки.
            db.fold_state_range(world_id, db.FOLD_HIDDEN, seq)
            # сводки, покрывающие откатываемый диапазон, недостоверны, даже если их seq < точки:
            # помечаем FOLD_HIDDEN, чтобы они не попадали в промпт (get_summary_events
            # фильтрует folded=0) и их можно было вернуть при повторной перемотке.
            for s in stale:
                if s["seq"] < seq:
                    db.fold_state_range(world_id, db.FOLD_HIDDEN, s["seq"], s["seq"],
                                        ("summary",))
            removed_n = len(doomed)
            removed_summaries = [s["seq"] for s in stale]
        else:
            # сначала сводки, потом остальное — иначе delete_events_after смел бы и их,
            # и отчёт «сколько сводок убрано» стал бы нулём при реально удалённых сводках.
            # Сводки с seq < точки, но покрывающие откат, тоже удаляем (иначе они
            # переживут перемотку и попадут в промпт как «будущее»).
            below = [s["seq"] for s in stale if s["seq"] < seq]
            removed_summaries = db.delete_summaries_after(world_id, seq - 1)
            if below:
                db.delete_events_by_seq(world_id, below)
                removed_summaries = sorted(set(removed_summaries) | set(below))
            removed_seqs = db.delete_events_after(world_id, seq - 1)
            removed_n = len(removed_seqs)
            exchange_seqs = removed_seqs
        # 4) разворачиваем покрытое (обратимость — главное в фиксе A1).
        # ВАЖНО: диапазон строго [unfold_from ; seq-1] — иначе в режиме hide развернулись
        # бы события, которые мы только что сокрыли выше (они имеют seq >= точки отката).
        unfolded_n = 0
        if unfold_from < seq:
            unfolded_n = db.fold_state_range(world_id, db.FOLD_VISIBLE, unfold_from, seq - 1,
                                             ("player", "narrator"))

        # 5) состояние мира (game_over снимается: откат = «жизнь продолжается»)
        setting = dict(setting)
        setting.pop("game_over", None)
        db.update_world(world_id, setting=setting)
        db.save_turn_snapshot(world_id, seq, setting)   # точка остаётся доступной
        verb = "сокрыто" if hide else "убрано"
        ev = db.add_event(world_id, "system",
                          f"{note}: возврат к ходу {seq} — {verb} событий {removed_n}, "
                          f"сводок {len(removed_summaries)}, возвращено в недавнюю память "
                          f"{unfolded_n}.",
                          # A11 (аудит 41): метка на строке отката — носитель сигнала для
                          # вкладок БЕЗ живой SSE-ленты (запасной поллинг читает события).
                          meta={"rewound": {"to_seq": seq, "mode": mode}})
        # 6) память не должна помнить отменённое (A4) — в обоих режимах
        await _purge_vectors(world_id, exchange_seqs,
                             [s["seq"] for s in stale] if hide else removed_summaries)
        log.info("перемотка (world %s, mode=%s): %d событий, %d сводок, развёрнуто %d",
                 world_id, mode, removed_n, len(removed_summaries), unfolded_n)
        # A11 (аудит 41): обещание из докстринга модуля — в жизни. Раньше `rewound` не
        # публиковался НИГДЕ, и вторая открытая вкладка после отката в первой держала
        # удалённый лог (дорисовывая к нему новые ходы) до ручного F5.
        # Публикуем ПОСЛЕ всех записей в БД: вкладка по этому сигналу перезагружает мир,
        # и перезагрузка обязана увидеть уже откатанный таймлайн.
        # `bus.publish` (не publish_threadsafe) — мы и так в цикле событий приложения
        # (async-роут), а глобальный `_loop` шины может указывать на другой/мёртвый цикл.
        bus.publish(world_id, {"type": "rewound", "world_id": world_id, "seq": seq,
                               "mode": mode, "event_id": ev["id"],
                               "latest_seq": db.latest_seq(world_id)})
        return {"ok": True, "world_id": world_id, "seq": seq, "mode": mode,
                "removed_events": removed_n, "removed_summaries": len(removed_summaries),
                "unfolded": unfolded_n, "setting": setting, "event": ev,
                "latest_seq": db.latest_seq(world_id)}
