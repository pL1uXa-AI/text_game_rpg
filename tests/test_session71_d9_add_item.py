# -*- coding: utf-8 -*-
"""Сессия 71 — D9 (аудит 41): `add_item` — одна точка записи `value` и ОДНО сообщение на ход.

Было две претензии к одному звену цепочки (`mechanics.ItemHandler`):

(а) **Два (фактически три) пути для `value`.** `_give_item` создавал запись инвентаря, а
    `add_item` потом ВТОРЫМ поиском по `player["inventory"]` дописывал найденному предмету
    ценность — то есть у нового предмета `value` оказывался записан «в обход» функции выдачи,
    а у кривой записи инвентаря (без ключа `name`, наследие старых сейвов) этот второй поиск
    падал в `KeyError` и весь `add_item` тихо съедался try/except звена.
    Третий путь был у `trade_buy`: «наследует ценность покупки» делался тем же вторым
    поиском. Теперь ценность (как и вес/описание) пишет ОДНА функция — `_give_item`.

(б) **1000 системных сообщений.** Модель на «выдай 1000 мечей» часто шлёт `add_item`
    длинным списком (или повторяет один предмет несколько раз), и каждый пункт рождал свою
    строку «Получено: …» → чат тонул, истории хода раздувались, а карточке предмета в
    `origins` (`memory.ensure_knowledge_cards`) доставала случайная из сотен копий.
    Теперь показ склеен: одно сообщение «🎒 Получено: Меч ×1000; Щит ×200», больше 6 имён
    сворачиваются в «… и ещё N».

Закон 3 не нарушен: движок по-прежнему ничего не решает — он складывает в инвентарь ровно
то, что дал мастер, и озвучивает это одной строкой.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MECHANICS = ROOT / "backend" / "mechanics.py"

from backend import mechanics as mec  # noqa: E402
from backend.memory import _origin_for  # noqa: E402
from backend.narrator import apply_directives, default_setting  # noqa: E402


def _st(**over) -> dict:
    s = default_setting({"name": "t", "genre": "приключение"}, "normal")
    s["player"]["inventory"] = []
    s.update(over)
    return s


def _inv(setting, name):
    return next((x for x in setting["player"]["inventory"] if x.get("name") == name), None)


# ═════════ 1. (а) один путь для value ═════════

def test_new_item_gets_value_from_give_item():
    """Ценность у НОВОГО предмета появляется самой выдачей, а не вторичной правкой."""
    src = io.open(MECHANICS, encoding="utf-8").read()
    body = src[src.index("def _give_item"):src.index("def inventory_weight")]
    assert "value" in body, "_give_item снова не умеет ценность — у add_item два пути (D9)"
    s = _st()
    msgs = apply_directives(s, {"add_item": [{"name": "Трофей", "qty": 2, "value": 10}]})
    assert _inv(s, "Трофей")["value"] == 10
    assert mec.total_sell_value(s["player"]) == 20, "справка продажи считает value × qty"
    assert msgs == ["🎒 Получено: Трофей ×2"], msgs


def test_existing_item_value_updated_and_merge():
    s = _st()
    apply_directives(s, {"add_item": [{"name": "Меч", "qty": 1}]})
    assert "value" not in _inv(s, "Меч"), "без value в директиве ключ не появляется"
    apply_directives(s, {"add_item": [{"name": "Меч", "qty": 1, "value": 7}]})
    it = _inv(s, "Меч")
    assert it["qty"] == 2 and it["value"] == 7, "склеивание по имени + ценность записаны"


def test_missing_value_does_not_clobber_stored_one():
    """`value` в директиве нет (или он 0/кривой) — ранее записанная цена НЕ затирается."""
    s = _st()
    apply_directives(s, {"add_item": [{"name": "Амулет", "qty": 1, "value": 25}]})
    apply_directives(s, {"add_item": [{"name": "Амулет", "qty": 2}]})
    it = _inv(s, "Амулет")
    assert it["qty"] == 3 and it["value"] == 25, it
    apply_directives(s, {"add_item": [{"name": "Амулет", "qty": 1, "value": 0}]})
    assert _inv(s, "Амулет")["value"] == 25, "value:0 — не «обнулить», а «не задано»"


def test_junk_inventory_entry_without_name_does_not_break_add_item():
    """Регрессия на «второй поиск»: запись инвентаря без `name` (старый сейв) больше не
    роняет `add_item` (было KeyError → всё звено молча съедалось except-ом)."""
    s = _st()
    s["player"]["inventory"].append({"qty": 1, "desc": "безымянный хлам"})
    msgs = apply_directives(s, {"add_item": [{"name": "Фонарь", "qty": 1, "value": 4}]})
    assert _inv(s, "Фонарь")["value"] == 4
    assert msgs == ["🎒 Получено: Фонарь ×1"], msgs


def test_trade_buy_inherits_value_through_same_helper():
    """Покупка наследует цену из ценника той же выдачей (третий путь убран из исходника)."""
    src = io.open(MECHANICS, encoding="utf-8").read()
    seg = src[src.index('"trade_buy" in d'):src.index('"trade_sell" in d')]
    assert "_bought" not in seg, "trade_buy по-прежнему правит инвентарь вторым поиском"
    s = _st()
    s["player"]["gold"] = 1000
    apply_directives(s, {"shop_add": {"id": "gb", "name": "Лавка", "items": [
        {"name": "Слиток", "price": 5, "qty": 5, "weight": 1, "value": 3}]}})
    apply_directives(s, {"shop_add": {"id": "nb", "name": "Без цен", "items": [
        {"name": "Верёвка", "price": 2, "qty": 5}]}})
    apply_directives(s, {"trade_buy": {"shop": "gb", "item": "Слиток", "qty": 2}})
    it = _inv(s, "Слиток")
    assert it["qty"] == 2 and it["value"] == 3 and it["weight"] == 1.0, it
    apply_directives(s, {"trade_buy": {"shop": "nb", "item": "Верёвка", "qty": 1}})
    assert "value" not in _inv(s, "Верёвка"), "нет цены в ценнике — не пишем нулевой value"


def test_give_item_value_signature_optional():
    """Старые вызовы (gather/craft) работают без нового параметра и НЕ заводят value."""
    s = _st()
    apply_directives(s, {"gather": {"item": "Руда", "qty": 3, "weight": 1}})
    it = _inv(s, "Руда")
    assert it["qty"] == 3 and it["weight"] == 1.0 and "value" not in it, it


# ═════════ 2. (б) одно сообщение вместо тысячи ═════════

def test_thousand_swords_is_one_message():
    s = _st()
    items = [{"name": "Меч", "qty": 1} for _ in range(1000)]
    msgs = apply_directives(s, {"add_item": items})
    assert msgs == ["🎒 Получено: Меч ×1000"], msgs
    assert _inv(s, "Меч")["qty"] == 1000, "в инвентаре — тоже одна склеенная строка"


def test_many_different_items_fold_to_one_line_with_counter():
    s = _st()
    items = [{"name": f"Хлам{i}", "qty": 1} for i in range(20)]
    msgs = apply_directives(s, {"add_item": items})
    assert len(msgs) == 1, msgs
    txt = msgs[0]
    assert txt.startswith("🎒 Получено: ") and "… и ещё 14" in txt, txt
    assert txt.count("×") == 6, txt[:200]
    assert len(s["player"]["inventory"]) == 20, "показ свёрнут, предметы — все на месте"


def test_names_and_qty_summed_in_one_message():
    s = _st()
    msgs = apply_directives(s, {"add_item": [{"name": "Меч", "qty": 2},
                                             {"name": "Щит", "qty": 1},
                                             {"name": "Меч", "qty": 3}]})
    assert msgs == ["🎒 Получено: Меч ×5; Щит ×1"], msgs


def test_empty_add_item_stays_silent():
    """`add_item: []` — ничего не выдано, пустой строки «🎒 Получено: » в чате нет."""
    s = _st()
    msgs = apply_directives(s, {"add_item": []})
    assert msgs == [], msgs


def test_origin_for_item_card_still_matches():
    """Карточке предмета нужно «условие получения» — склейка его не ломает (память).
    Здесь же сторожится, что комментарий в `memory.py` ссылается на живую формулировку."""
    s = _st()
    msgs = apply_directives(s, {"add_item": [{"name": "Клинок", "qty": 4}]})
    assert _origin_for("Клинок", msgs) == msgs[0], "карточка не должна остаться без origin"
    mem = io.open(ROOT / "backend" / "memory.py", encoding="utf-8").read()
    assert "«🎒 Получено" in mem, "память ссылается на удалённую формулировку системки"


def test_overweight_and_other_links_keep_own_messages():
    """Ничего кроме add_item не переозвучено: gather/craft/купля — свои строки, свои эмодзи."""
    s = _st()
    assert mec.inventory_weight(s["player"]) == 0
    msgs = apply_directives(s, {"gather": {"item": "Ветка", "qty": 1, "weight": 1}})
    assert msgs == ["🪓 Собрано: Ветка ×1"], msgs


# ═════════ 4. через API: системка доходит до чата одна ═════════

def _turn(api_client, engine: str, text: str = "подобрать всё", wid: int | None = None):
    client, holder = api_client
    if wid is None:
        wid = client.post("/api/worlds", json={"theme_id": "raskolotye-nebesa", "name": "D9"}
                          ).json()["world_id"]
    holder["reply"] = f"Ты собираешь трофеи. <<ENGINE>>{engine}"
    r = client.post(f"/api/worlds/{wid}/action", json={"text": text})
    assert r.status_code == 200, r.text
    body = r.json()
    return wid, [e["content"] for e in body["events"] if e["role"] == "system"], body


def test_turn_with_hundred_items_yields_one_chat_line(api_client):
    engine = '{"add_item": [' + ",".join(
        '{"name": "Клык %d", "qty": 3}' % i for i in range(120)) + ']}'
    wid, sysmsgs, body = _turn(api_client, engine)
    got = [m for m in sysmsgs if "Получено" in m]
    assert len(got) == 1, got
    assert got[0].startswith("🎒 Получено: ") and "… и ещё 114" in got[0], got[0]
    st = body["state"]
    fangs = [i for i in st["player"]["inventory"] if str(i["name"]).startswith("Клык ")]
    assert len(fangs) == 120 and all(i["qty"] == 3 for i in fangs), \
        "предметы при этом выданы все (сворачивается только показ)"


def test_value_survives_the_turn_and_reaches_sidebar_state(api_client):
    _, sysmsgs, body = _turn(api_client,
                             '{"add_item": [{"name": "Перстень", "qty": 2, "value": 40,'
                             ' "weight": 0.1, "desc": "печатка Ковена"}]}')
    assert sysmsgs == ["🎒 Получено: Перстень ×2"] or \
        any("Перстень ×2" in m for m in sysmsgs), sysmsgs
    it = next(i for i in body["state"]["player"]["inventory"] if i["name"] == "Перстень")
    assert it["value"] == 40 and it["weight"] == 0.1 and it["desc"] == "печатка Ковена", it


def test_stray_value_text_not_invisible_to_junk(api_client):
    """Кривой `value` от модели не роняет ход и не даёт лишней системки."""
    _, sysmsgs, _ = _turn(api_client, '{"add_item": [{"name": "Монета", "qty": 1, "value": "много"}]}')
    assert any("Монета ×1" in m for m in sysmsgs), sysmsgs


# ═════════ 5. сторожа исходника ═════════

def test_item_handler_no_secondary_search_for_value():
    src = io.open(MECHANICS, encoding="utf-8").read()
    body = src[src.index("class ItemHandler"):src.index("class EconomyHandler")]
    assert 'if x["name"] == ni["name"]' not in body, "второй поиск по инвентарю вернулся (D9а)"
    assert body.count('msgs.append(f"🎒 Получено') <= 1, "показ снова плодится на каждый пункт"
    # старая формулировка без эмодзи больше не возвращается (её ловит и тест дока, и память)
    assert '"Получено:' not in src and "f\"Получено" not in src, "в чате снова «Получено:» без 🎒"


def test_doc_describes_single_value_path_and_one_message():
    doc = (ROOT / "AGENT.md").read_text(encoding="utf-8")
    seg = [ln for ln in doc.splitlines() if "`add_item / remove_item:" in ln]
    assert seg, "строка директив инвентаря исчезла из AGENT.md"
    i = doc.index("`add_item / remove_item:")
    block = doc[i:i + 1200]
    assert "_give_item" in block and "×1000" in block, block[:600]


@pytest.mark.parametrize("qty", [1, 5, 1000])
def test_qty_is_never_lost_in_message(qty):
    s = _st()
    msgs = apply_directives(s, {"add_item": [{"name": "Стрела", "qty": qty}]})
    assert msgs == [f"🎒 Получено: Стрела ×{qty}"], msgs
    assert _inv(s, "Стрела")["qty"] == qty
