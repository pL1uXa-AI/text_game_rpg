# -*- coding: utf-8 -*-
"""Аудит 41, B5 (сессия 58): мёртвых и лишних настроек не заводим — и не держим.

Было: `RERANK_TOP_N=20` в `.env` (ручку сняли ещё в A17, аудит 38 — строка пережила её на
год) плюс такая же строка осела в таблице `admin_settings`; `GAME_HOST`/`GAME_PORT` в
`.env.example` читались ТОЛЬКО самим `config.py`, а реальный адрес/порт задаёт
`start_game.bat` (`GAME_BIND` из окружения + жёсткий `--port 8002`). Итог — обещание в
шаблоне, которое ничего не делает, и `hidden_admin_keys()`, честно прячущая «настройку»,
которой нет. Следующая сессия тратила бы время на поиск «куда не применяется ключ».

Что проверяется (ничего не удаляет — `.env` и `data/game.db` файлы владельца, правило 15):

1. Ни одного мёртвого ПОЛЯ в `Config`: каждое либо читается кодом вне `config.py`, либо
   сознательная инфраструктура (`hidden_admin_keys()` — пути/порты/`LOG_FILE`, их применяет
   раннер или `logsetup`, читающий env напрямую). С degradation-пробой: подмешанное в
   dataclass фиктивное поле обязано быть поймано (иначе проверка пуста).
2. `hidden_admin_keys()` не содержит имён-призраков: всё объявленное спрятанным — реальное
   поле `Config` (иначе «в админке нет» врать про несуществующую настройку).
3. Ключей, снятых с производства, в репозитории не осталось: `GAME_HOST`/`GAME_PORT`/
   `RERANK_TOP_N` не в `.env.example` и не в коде (`config.py` с помянутыми в комментариях
   исключениями, .bat, фронте).
4. Привязка сервера — ОДНА: `start_game.bat` по-прежнему сам выбирает bind (`GAME_BIND`) и
   порт; в `.env.example` нет второй, конкурирующей пары ключей.
5. Мёртвый ключ больше не молчит: `_warn_dead_keys` пишет warning на неизвестное имя (из .env
   И из админки) и молчит на всех валидных; тот же путь ловит и реальный `Config.load` с
   кривым .env. Без этого пункт 3 был бы разовой уборкой, а не защитой.

Запуск: "…\\3.12.10\\python.exe" -X utf8 -m pytest tests/test_session58_dead_config.py -q
"""
from __future__ import annotations

import dataclasses
import logging
import re
from pathlib import Path

import pytest

from backend import config as cfg_mod
from backend import logsetup
from backend.config import (Config, hidden_admin_keys, overridable_env_keys,
                            strip_env_comment)

ROOT = Path(__file__).resolve().parent.parent

# Ключи, снятые с производства (аудит 38 A17 / аудит 41 B5). Держать их в шаблоне или в коде
# = снова обещание в никуда.
RETIRED = ("GAME_HOST", "GAME_PORT", "RERANK_TOP_N")


def _env_keys(path: Path) -> list[str]:
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        out.append(s.partition("=")[0].strip())
    return out


def _field_sources() -> str:
    """Весь код/фронт/батники, КРОМЕ config.py (там поля объявляются и заполняются)."""
    parts: list[str] = []
    for p in list((ROOT / "backend").rglob("*.py")) + list((ROOT / "frontend").glob("*.js")) \
            + list((ROOT / "frontend").glob("*.html")) + list((ROOT / "scripts").glob("*.py")) \
            + [ROOT / "start_game.bat"]:
        if "__pycache__" in str(p) or p.name == "config.py":
            continue
        parts.append(p.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def _code_only(path: Path) -> str:
    """Текст Python-файла без комментариев и строковых литералов (tokenize).

    Нужно потому, что снятые ключи обязано быть видно в комментариях и докстрингах
    («ручки RERANK_TOP_N больше нет») — проверять по сырому тексту означало бы воевать
    с честной документацией.
    """
    import tokenize

    out: list[str] = []
    with tokenize.open(str(path)) as f:
        for tok in tokenize.generate_tokens(f.readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out.append(tok.string)
    return " ".join(out)


def _used_inside_config() -> set[str]:
    """Поля, которые config.py читает сам через `self.<поле>`.

    Провайдерские поля (`main_base_url`, `llama_model`, …) наружу по имени не читаются —
    их расходует `get_provider`, отдавая готовый dict. Мёртвое поле — то, которое не нужно
    ни снаружи, ни внутри (т.е. только заполняется из env и лежит мёртвым грузом).
    Граница — `def overridable_env_keys`: до неё dataclass и резолверы (там `self.X` =
    живое чтение), после — только реестры.
    """
    src = (ROOT / "backend" / "config.py").read_text(encoding="utf-8")
    cut = src.index("def overridable_env_keys")
    # до реестров — dataclass, провайдеры, клампы; `self.<поле>` оттуда = живое поле
    return {m.group(1) for m in re.finditer(r"self\.([a-z_][a-z0-9_]*)\b", src[:cut])}


def _dead_fields(sources: str, fields: list[str], inside: set[str] | None = None) -> list[str]:
    """Поля, до которых никто не достал: ни внешним корпусом, ни `self.<поле>` в config.py."""
    infra = {k.lower() for k in hidden_admin_keys()}
    inside = _used_inside_config() if inside is None else inside
    dead = []
    for name in fields:
        if name in infra or name in inside:
            continue                                    # пути/порты/LOG_FILE и резолв провайдеров
        if re.search(r"[.\b\"]" + name + r"\b", sources):
            continue
        dead.append(name)
    return dead


def test_no_dead_config_fields():
    """Ни одного поля Config, которое код больше не читает (B5)."""
    dead = _dead_fields(_field_sources(), [f.name for f in dataclasses.fields(Config)])
    assert not dead, f"мёртвые поля Config (никто не читает): {dead}"


def test_dead_field_scan_is_not_vacuous():
    """Подмешанное фиктивное поле обязано быть поймано — иначе проверка 1 пуста."""
    fake = "yet_another_unused_knob"
    assert _dead_fields("", [fake], inside=set()) == [fake], "чекер ослеп: изолированное поле не красится"
    assert _dead_fields(f"cfg.{fake} = 1", [fake], inside=set()) == [], \
        "чекер бьёт мимо реального обращения"
    assert _dead_fields("", [fake], inside={fake}) == [], "self.<поле> внутри config.py — тоже использование"
    # реальные поля не могут «случайно» попасть в тот же список, что и фиктивное
    assert _dead_fields(_field_sources(), [fake]) == [fake]


def test_hidden_admin_keys_are_real_settings():
    """«Спрятано от админки» обязано быть реальной настройкой, а не памятью о снятой."""
    phantom = [k for k in hidden_admin_keys() if k not in set(overridable_env_keys())]
    assert not phantom, f"hidden_admin_keys() перечисляет то, чего в Config нет: {phantom}"
    for k in ("DB_PATH", "LOG_FILE", "ADMIN_ALLOW_LAN"):
        assert k in hidden_admin_keys(), f"инфраструктурный ключ {k} пропал из скрытых"


@pytest.mark.parametrize("key", RETIRED)
def test_retired_keys_are_gone(key):
    """Снятый ключ не живёт ни в шаблоне, ни в коде, ни в батниках (только в комментариях/доках)."""
    assert key not in _env_keys(ROOT / ".env.example"), f"{key} вернулся в .env.example"
    assert key not in _code_only(ROOT / "backend" / "config.py"), f"{key} снова читается кодом"
    assert key not in (ROOT / "start_game.bat").read_text(encoding="utf-8")
    assert f'"{key}"' not in "".join(Path(p).read_text(encoding="utf-8", errors="ignore")
                                    for p in [ROOT / "frontend" / "admin.html",
                                              ROOT / "frontend" / "app.js"])


def test_bind_and_port_have_one_owner():
    """Сервер поднимает start_game.bat: bind из GAME_BIND, порт жёсткий — второй пары ключей нет."""
    bat = (ROOT / "start_game.bat").read_text(encoding="utf-8")
    assert "--host %GAME_BIND%" in bat, "bind перестал браться из GAME_BIND"
    assert "--port 8002" in bat, "порт игры уехал из launcher'а (сверь check_start_bat.py)"
    example = "\n".join(_env_keys(ROOT / ".env.example"))
    assert not re.search(r"\bGAME_(HOST|PORT|BIND)\b", example), \
        "в шаблоне снова две конкурирующие записи про адрес слушания (bind — окружение процесса)"


# ─────────────── мёртвый ключ обязан быть видимым ───────────────

def test_warn_dead_keys_reports_and_stays_quiet(caplog):
    """Неизвестное имя из .env ИЛИ из админки — warning; валидные имена — тишина."""
    known = overridable_env_keys()[0]
    logsetup.reset_once()
    with caplog.at_level(logging.WARNING, logger="textgame.config"):
        cfg_mod._warn_dead_keys({known: "1", "RERANK_TOP_N": "20"}, {})
    msgs = [r.getMessage() for r in caplog.records]
    assert any("RERANK_TOP_N" in m and "читает" in m for m in msgs), \
        f"мёртвый ключ промолчал: {msgs}"
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="textgame.config"):
        cfg_mod._warn_dead_keys({}, {"GAME_HOST": "0.0.0.0"})       # только админка
    assert any("GAME_HOST" in r.getMessage() for r in caplog.records), \
        "осевшая в admin_settings строка не видна — её ведь никто не ищет"
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="textgame.config"):
        cfg_mod._warn_dead_keys({k: "1" for k in overridable_env_keys()}, {})
    assert not [r for r in caplog.records if "мёртвые" in r.getMessage()], \
        "валидные ключи объявлены мёртвыми (чекер врёт → правки начнут удалять нужное)"


def test_config_load_flags_dead_key_in_env(tmp_path, caplog):
    """Тот же путь действует для живого старта: кривой .env виден в журнале (правило 14)."""
    p = tmp_path / ".env"
    p.write_text("MAX_TOKENS=1200\nRERANK_TOP_N=20\n", encoding="utf-8")
    logsetup.reset_once()
    with caplog.at_level(logging.WARNING, logger="textgame.config"):
        cfg = Config.load(env_file=p, env={"SENTINEL": "1"})
    assert cfg.max_tokens == 1200, "кламп/разбор не должны пострадать от проверки мусора"
    assert any("RERANK_TOP_N" in r.getMessage() for r in caplog.records), \
        "Config.load молчит про лишний ключ — ровно как молчал раньше (т.е. B5 жив)"


def test_strip_env_comment_still_used_by_owner_files():
    """Служебная мелочь, за которую цепляется предыдущий тест: разбор .env не сломан."""
    assert strip_env_comment("1200 # комментарий") == "1200"
    assert strip_env_comment('"0.0.0.0"') == "0.0.0.0"


# ─────────── осколок в таблице админки: видим и снимаем ───────────

def test_stale_admin_row_is_visible_and_resettable(api_client):
    """B5: строка без поля в Config обязана быть видна в GET и сниматься «сбросом к .env».

    Раньше её не было видно НИКАК и снять было нельзя: `reset` принимал только живые имена,
    а неизвестный ключ отбивался как опечатка. Так `RERANK_TOP_N` и пережил снятие ручки.
    """
    from backend import db as db_mod
    from backend.config import invalidate_config

    client, _ = api_client
    db_mod.set_admin_settings({"RERANK_TOP_N": "20"})
    invalidate_config()
    try:
        d = client.get("/api/admin/settings").json()
        assert "RERANK_TOP_N" in d["stale"], f"осколок не виден: {d.get('stale')}"
        assert d["stored"]["RERANK_TOP_N"] == "20"
        # живая настройка в stale не лезет (иначе список — шум)
        assert "RAG_MEMORY_K" not in d["stale"]
        r = client.post("/api/admin/settings", json={"reset": ["RERANK_TOP_N"]})
        assert r.status_code == 200, r.text
        assert "RERANK_TOP_N" not in db_mod.get_admin_settings(), "сброс не снял мёртвую строку"
        # а мусор, которого в таблице нет, по-прежнему 400 (опечатка не проглатывается)
        assert client.post("/api/admin/settings",
                           json={"reset": ["TOTLY_NOT_A_KEY"]}).status_code == 400
    finally:
        db_mod.set_admin_settings({"RERANK_TOP_N": ""})
        invalidate_config()
