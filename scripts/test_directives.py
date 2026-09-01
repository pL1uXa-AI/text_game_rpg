# -*- coding: utf-8 -*-
"""
Регрессионный тест директив механики (по AGENT.md «Известные фиксы»).

Запуск:  python -X utf8 scripts/test_directives.py
(чистый Python, pytest не требуется; xUnit-соглашения, exit code 0 = всё ок)

Проверяет:
1. 35 «мусорных» директив — normalize_directives + apply_directives не роняют ход
   (раньше: 'str' object has no attribute 'get').
2. Снапшот при перегенерации: тик эффектов не задваивается (сессия 5).
3. roll_expr (d20 / 2d6+1 / d100) и roll_outcome.
4. db.transaction(): группа записей атомарна — при исключении внутри блока
   ничего не коммитится (иначе «события есть, а состояние не обновилось»).
"""
from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import db as dbmod  # noqa: E402
from backend import narrator  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name: str, fn):
    global _PASS, _FAIL
    try:
        fn()
        _PASS += 1
        print(f"  ✅ {name}")
    except AssertionError as e:
        _FAIL += 1
        print(f"  ❌ {name}: {e}")
    except Exception as e:
        _FAIL += 1
        print(f"  ❌ {name}: {type(e).__name__}: {e}")


def _player(**kw) -> dict:
    p = {
        "hp": 50, "max_hp": 100, "mp": 20, "max_mp": 50, "gold": 10,
        "xp": 0, "level": 1, "stats": {"сила": 10, "ловкость": 10, "выносливость": 10,
                                        "интеллект": 10, "мудрость": 10, "харизма": 10, "удача": 10},
        "inventory": [], "effects": {}, "skills": {},
    }
    p.update(kw)
    return p


def _setting(**kw) -> dict:
    s = narrator.default_setting(narrator.THEMES[0], "normal")
    s["player"]["hp"] = 50
    s.update(kw)
    return s


# ── 1. Мусорные директивы ──────────────────────────────────────────────

GARBAGE_DIRECTIVES = [
    {"player": "hp -5"},                          # str вместо dict
    {"player": 7},                                 # число
    {"player": None},                              # None
    {"roll": "d20"},                               # roll строкой
    {"roll": 42},                                  # roll числом
    {"roll": {"expr": "d20", "mod": "2", "dc": "15"}},  # строковые числа
    {"enemy_apply": "волк"},                       # строка
    {"enemy_apply": {"id": 3, "hp": 10}},
    {"enemy_add": "тролль"},
    {"enemy_add": {"id": 7, "name": "Тролль", "hp": 30, "dmg": 5}},
    {"enemy_remove": "x"},
    {"quest": "найди артефакт"},                   # строка вместо dict
    {"quest": {"id": "q1", "title": "Квест", "status": "active"}},
    {"quest_done": "q1"},
    {"npc_set": "трактирщик"},
    {"npc_set": {"id": "n1", "name": "Бармен"}},
    {"npc_kill": "n1"},
    {"location_add": "таверна"},
    {"location_add": {"id": "loc1", "name": "Таверна"}},
    {"location_update": "поле"},
    {"move": "loc1"},
    {"flag": "дверь открыта"},                     # строка
    {"flag": {"name": "дверь", "value": True}},
    {"time": "ночь"},
    {"weather": "гроза"},
    {"game_over": True},
    {"effect_add": "отравлен"},                    # строка
    {"effect_add": {"name": "Яд", "turns": 3, "damage": 2}},
    {"effect_remove": "Яд"},
    {"skill_add": {"name": "Огненный шар", "rank": "D", "mp_cost": 5}},
    {"skill_rank": {"name": "Огненный шар", "rank": "C"}},
    {"skill_remove": "Огненный шар"},
    {"class": 123},                                # не str/dict
    {"class": {"name": "Маг", "skill": {"name": "Волшба", "rank": "F"}}},
    {"profession": "Кузнец"},
    {"title": "Ветеран"},
    {"reputation": {"гильдия воров": 2}},
    {"reputation": "строка"},                      # строка вместо dict
    {"player": {"stats": {"сила": "2"}}},          # строковый стат
    {"player": {"actions": {"кузнечное дело": 1}}},
    {"add_item": "меч"},                           # строка
    {"add_item": [{"name": "Меч", "qty": 1}]},
    {"remove_item": "меч"},
    {"keyword_unknown": {"a": 1}},                 # незнакомый ключ
    None,                                          # вообще не dict
    "просто строка",
    [],
]


def t_garbage():
    for d in GARBAGE_DIRECTIVES:
        setting = _setting()
        nd = narrator.normalize_directives(d) or {}
        assert isinstance(nd, dict), f"normalize вернул не dict: {d!r}"
        msgs = narrator.apply_directives(setting, nd)
        assert isinstance(msgs, list), f"apply вернул не list: {d!r}"
        # состояние не должно сломаться
        assert setting["player"]["stats"], "stats пустые после грязи"
    # «грязный» player.stats не должен падать
    s2 = _setting()
    narrator.apply_directives(s2, {"player": {"stats": {"сила": "2"}}})
    assert s2["player"]["stats"]["сила"] == 12, "стат не применился аддитивно"


# ── 2. Снапшот при перегенерации (тик не задваивается) ─────────────────

def t_snapshot_tick():
    s = _setting()
    s["player"]["effects"] = {"Яд": {"turns": 3, "damage": 2}}
    snap = copy.deepcopy(s)                       # = worlds.snapshot до хода

    # ход N: тик прошёл один раз
    msgs1 = narrator.tick_effects(s)
    assert any("Яд" in m for m in msgs1), "тик не сработал"
    hp_after_first = s["player"]["hp"]

    # перегенерация: состояние откатывается к снапшоту → тик ещё раз = тот же результат
    restored = copy.deepcopy(snap) if isinstance(snap, dict) else json.loads(snap)
    # D12 (аудит 38): вызов нужен РАДИ побочного эффекта (откатанный тик обязан дать тот
    # же урон) — имя результату не нужно, проверяется само состояние.
    narrator.tick_effects(restored)
    assert restored["player"]["hp"] == hp_after_first, \
        f"тик задвоился: {restored['player']['hp']} != {hp_after_first}"
    assert restored["player"]["effects"]["Яд"]["turns"] == 2, "turns не уменьшился"
    # и при повторной перегенерации снова тот же снапшот → урон стабилен
    restored3 = copy.deepcopy(snap)
    narrator.tick_effects(restored3)
    assert restored3["player"]["hp"] == hp_after_first, "повторная регенерация задваивает"


# ── 3. Кубы ────────────────────────────────────────────────────────────

def t_roll_expr():
    r = narrator.roll_expr("d20")
    assert len(r["rolls"]) == 1 and 1 <= r["total"] <= 20, f"d20: {r}"
    r2 = narrator.roll_expr("2d6+1")
    assert len(r2["rolls"]) == 2, f"2d6+1: {r2}"
    assert r2["total"] == sum(r2["rolls"]) + 1, "бонус +1 не учтён"
    r100 = narrator.roll_expr("d100")
    assert 1 <= r100["total"] <= 100, f"d100: {r100}"
    rmod = narrator.roll_expr("d20", mod=2)
    assert rmod["total"] == rmod["rolls"][0] + 2, "mod не учтён"
    # «мусорное» выражение → фолбэк d20
    bad = narrator.roll_expr("не_куб")
    assert 1 <= bad["total"] <= 20, f"фолбэк: {bad}"
    outcomes = narrator.roll_outcome(25, 15, "d20")
    assert outcomes in ("критический успех", "успех", "провал", "критический провал")


# ── 4. db.transaction(): атомарность группы записи ─────────────────────

def _patch_db_path(conn):
    """Переключаем db.py на временный файл (чтобы не трогать боевую game.db)."""
    global _db_path_backup
    _db_path_backup = None
    # dbmod._db() уже мог создать _conn — сбрасываем кэш после подмены пути
    dbmod._conn = None


def t_transaction_atomic():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "test_tx.db"
        # подменяем путь БД: перехватываем get_config → локальный конфиг сложно,
        # проще временно переопределить db_path через monkeypatch get_config
        orig_get_config = dbmod.get_config
        orig_conn = dbmod._conn
        dbmod._conn = None

        class _FakeCfg:
            db_path = str(tmp)

        dbmod.get_config = lambda: _FakeCfg()  # type: ignore

        def _restore():
            # закрываем временное соединение (иначе TemporaryDirectory не удалит файл)
            try:
                dbmod.close()
            except Exception:
                pass
            dbmod._conn = orig_conn
            dbmod.get_config = orig_get_config

        try:
            wid = dbmod.create_world("Тест", "tavern", "", "normal", "second", "ru",
                                     "", _setting(), {})
            # группа из двух записей — атомарно
            with dbmod.transaction():
                dbmod.add_event(wid, "player", "действие")
                dbmod.add_event(wid, "narrator", "ответ")
                dbmod.update_world(wid, setting=_setting())
            evs = dbmod.get_events(wid)
            assert len(evs) == 2, f"ожидали 2 события после COMMIT, получили {len(evs)}"
            w = dbmod.get_world(wid)
            assert w and "player" in w.get("setting", ""), "setting не сохранён"

            # исключение внутри блока → роллбэк всей группы
            try:
                with dbmod.transaction():
                    dbmod.add_event(wid, "player", "bug")
                    dbmod.add_event(wid, "system", "secondary")
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
            evs2 = dbmod.get_events(wid)
            assert len(evs2) == 2, \
                f"роллбэк не сработал: после аварии стало {len(evs2)} событий (было 2)"
        finally:
            _restore()
            # закрываем соединение, чтобы TemporaryDirectory мог удалить файл
            try:
                if dbmod._conn is not None:
                    dbmod._conn.close()
            except Exception:
                pass
            dbmod._conn = None


# ── main ──────────────────────────────────────────────────────────────

def main():
    print("test_directives.py — регрессия механики\n")
    check("35+ «мусорных» директив не роняют ход", t_garbage)
    check("снапшот при перегенерации: тик не задваивается", t_snapshot_tick)
    check("roll_expr / roll_outcome", t_roll_expr)
    check("db.transaction(): атомарная группа записи + роллбэк", t_transaction_atomic)
    print(f"\nИтог: ✅ {_PASS} / ❌ {_FAIL}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())