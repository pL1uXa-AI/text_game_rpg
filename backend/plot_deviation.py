"""Сессия 63: «живой мир» — отход рассказчика от заранее написанного сюжета.

Идея: стандартный/свой сюжет — это ТОЧКА ОТСЧЁТА (стартовые локации, фракции, квесты,
канва `quest_chains`), а не единственно верный путь. Мир обязан перестраиваться под
действия игрока: убил «нужного» NPC, предал фракцию, похоронил арку — сюжет должен
свернуть, а не тащить мёртвую цепочку квестов к развилке, которой больше нет.

Проблема, которую это решает, чисто промптная: канва сюжета уходит в системный промпт
КАЖДЫМ ходом (`theme.desc`, `style`, якорный лор, `quest.desc`). Если мастер решил «мы
пошли своей дорогой», а в промпте по-прежнему лежит прежняя канва, он неминуемо начнёт
противоречить себе: правило 3 («не нарушай установленные факты») вступит в конфликт с
мёртвым сценарием, и через 20–30 ходов мир «починится» обратно в написанный сюжет.
Значит отход надо ЗАПИСАТЬ — и показать мастеру в следующий раз.

Здесь — только хранение правды (закон 2). Никаких триггеров, детекторов и автосверток:
код сам решать «сюжет умер» не может (закон 3). Мастер ЯВНО говорит «я сворачиваю»
директивой `world_evolve {what, why}`; обработчик — в `mechanics.PlotDeviationHandler`.
"""
from __future__ import annotations

from typing import Any, Optional

# Сколько записей отхода держим (как доска объявлений: старые уходят, промпт не раздувается).
DEVIATION_CAP = 20
# Максимальная длина формулировки (чтобы N отходов не съели бюджет состояния).
WHAT_MAX = 200
WHY_MAX = 200


def plot_deviation_add(setting: dict, what: str, why: str = "",
                       turn: Optional[int] = None) -> str:
    """Записать отход от канвы сюжета. Возвращает системное сообщение ('' — дубль).

    `what` — что именно мастер берёт из канвы / от чего отказывается; `why` — какое
    действие игрока к этому привело (без причины отход превращается в произвол).
    """
    dev = setting.setdefault("plot_deviation", [])
    if not isinstance(dev, list):
        dev = []
        setting["plot_deviation"] = dev
    entry: dict[str, Any] = {"what": str(what or "").strip()[:WHAT_MAX],
                             "why": str(why or "").strip()[:WHY_MAX]}
    if isinstance(turn, int):
        entry["turn"] = turn
    if not entry["what"]:
        return ""
    # Дубль той же формулировки не плодим: модель могла повторить отход на следующем ходу.
    if any(isinstance(x, dict) and x.get("what") == entry["what"] for x in dev):
        return ""
    dev.append(entry)
    if len(dev) > DEVIATION_CAP:
        dev.pop(0)
    # СООБЩЕНИЕ ПУСТОЕ — и это не забывчивость. world_evolve запись о том, что мастер
    # свернул с НАПИСАННОЙ канвы; в чате игрока такая строка («мир отходит от сюжета»)
    # выдавала бы существование заранее написанного сценария и ближайший поворот, т.е.
    # ломала бы иллюзию живого мира изнутри. Это заметки мастера, как «🗝 Тайны» у NPC:
    # рассказчик видит их в состоянии (plot_deviation_text), игрок — нет.
    return ""


def plot_deviation_text(setting: dict, limit: int = 8) -> str:
    """Человекочитаемый список отходов (для блока «🧭 ЖИВОЙ МИР» в состоянии)."""
    dev = setting.get("plot_deviation") or []
    if not isinstance(dev, list) or not dev:
        return ""
    out: list[str] = []
    for x in dev[-limit:]:
        if not isinstance(x, dict):
            continue
        w = str(x.get("what") or "").strip()
        if not w:
            continue
        why = str(x.get("why") or "").strip()
        t = x.get("turn")
        head = f"• ход {t}: {w}" if isinstance(t, int) else f"• {w}"
        out.append(head + (f" (из-за: {why[:120]})" if why else ""))
    return "\n".join(out)


def has_plot_deviation(setting: dict) -> bool:
    """Есть ли записанные отходы (для UI/тестов: «мир сворачивал с канвы N раз»)."""
    dev = setting.get("plot_deviation") or []
    return isinstance(dev, list) and any(isinstance(x, dict) and (x.get("what") or "")
                                         for x in dev)
