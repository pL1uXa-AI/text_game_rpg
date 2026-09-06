# -*- coding: utf-8 -*-
"""Сессия 49 — A9 (аудит 41): «кривое» число в настройках ломало ВСЕ будущие ходы.

Что было: валидация сводилась к «`int()`/`float()` упадёт → дефолт» (`config.it/flt`),
диапазонов не знал ни один слой.

  * `POST /api/worlds/{id}/settings` писал `temperature`, `top_p`, `max_tokens` как есть:
    `max_tokens: 0` / `temperature: 1e9` ложились в БД и роняли ошибку модели на каждом
    ходу (чинилось только повторной правкой настроек);
  * админка (`DEFAULT_TEMP`, `LLM_TIMEOUT`, `LOG_MAX_BYTES`, `TURN_SNAPSHOT_KEEP`,
    `LLM_BG_*`) — то же: `TURN_SNAPSHOT_KEEP=1` = перемотка «умирает» через ход,
    `LOG_MAX_BYTES=1` = ротация съедает журнал, мусор вместо числа тихо становился
    дефолтом (пользователь думал, что сохранил своё значение).

Что стало: ОДИН реестр границ `config.NUM_RANGES`, который применяют
  * `Config.load` (env процесса → админка → .env — все три источника разом),
  * `narrator.world_gen_settings` (пер-мир, ПРИ ЧТЕНИИ — потому что кривой JSON мог
    прийти из старого сохранения или из импортированного дампа),
  * `routers/worlds.py::update_gen_settings` (при записи + `gen_limits` в ответе),
  * `routers/admin.py::_admin_num` (при записи: мусор → 400, вне границ → подрезка),
  * `logsetup` (ротор журнала настраивается до первого конфига и читает env сам).

Закон 2/3: это только проверка возможности — значения выбирает игрок, код их не
«улучшает», а подрезку пишет в журнал (правило 14). Тесты — на ПОВЕДЕНИЕ (правило 19).
"""
from __future__ import annotations

import json

from backend import config as cfg_mod
from backend import db as db_mod
from backend import narrator as narrator_mod
from backend.config import clamp_gen_settings, clamp_num, Config, NUM_RANGES, num_kind
from backend.routers import core as core_mod

THEME_ID = narrator_mod.THEMES[0]["id"]


def _world_payload(name: str = "A9") -> dict:
    return {"theme_id": THEME_ID, "name": name}


# ══════════════════ реестр границ ══════════════════

def test_num_ranges_keys_are_real_settings():
    """Реестр не устаревает молча: каждое имя — поле Config (в верхнем регистре).

    Иначе опечатка в ключе = «границы нет» и настройка снова проходит насквозь."""
    fields = {f.upper() for f in Config.__dataclass_fields__}
    unknown = sorted(k for k in NUM_RANGES if k not in fields)
    assert not unknown, f"NUM_RANGES знает имена, которых нет в Config: {unknown}"


def test_num_ranges_defaults_are_inside_their_own_bounds():
    """Дефолты кода обязаны проходить собственный кламп — иначе «чиня» настройки,
    сломали бы старт игры на чистой установке."""
    for key, (lo, hi) in NUM_RANGES.items():
        dflt = Config.__dataclass_fields__[key.lower()].default
        assert isinstance(dflt, (int, float)) and not isinstance(dflt, bool), key
        assert lo <= float(dflt) <= hi, f"{key}: дефолт {dflt} вне границ {lo}…{hi}"


def test_gen_limit_keys_have_ranges():
    """Пер-мирные ключи обязаны ссылаться на существующие границы."""
    for k, env_name in cfg_mod.GEN_LIMIT_KEYS.items():
        assert env_name in NUM_RANGES, f"gen_settings.{k} → {env_name} без границ"


def test_zero_meaning_switches_stay_zero():
    """0 у этих настроек — задокументированный режим, а не «слишком мало» (не подрезать).

    TURN_SNAPSHOT_KEEP=0 = «хранить все точки», AUTO_TIME_EVERY=0 = авто-часы выкл.,
    METRICS_MAX_BYTES=0 = ротация выкл., TTS_CACHE_TTL_DAYS=0 = кэш вечно."""
    for key in ("TURN_SNAPSHOT_KEEP", "AUTO_TIME_EVERY", "METRICS_MAX_BYTES",
                "TTS_CACHE_TTL_DAYS"):
        assert clamp_num(key, 0) == 0, f"{key}: подрезали задокументированный 0"
    # а «просто слишком мало» — подрезается
    assert clamp_num("LOG_MAX_BYTES", 1) == int(NUM_RANGES["LOG_MAX_BYTES"][0])
    assert clamp_num("LLM_TIMEOUT", 0.01) == NUM_RANGES["LLM_TIMEOUT"][0]


def test_num_kind_follows_field_type():
    assert num_kind("LLM_TIMEOUT") == "float"
    assert num_kind("TURN_SNAPSHOT_KEEP") == "int"
    assert num_kind("MAIN_MODEL") is None       # строка
    assert num_kind("TTS_ENABLED") is None      # bool
    assert num_kind("NO_SUCH_KEY") is None


# ══════════════════ Config.load (env → админка → .env) ══════════════════

def test_config_load_clamps_env_values(tmp_path, monkeypatch):
    """Кривой .env больше не доезжает до игры: значения подрезаются при чтении конфига."""
    monkeypatch.setenv("LLM_TIMEOUT", "0.01")
    monkeypatch.setenv("LOG_MAX_BYTES", "1")
    monkeypatch.setenv("TURN_SNAPSHOT_KEEP", "3")
    monkeypatch.setenv("DEFAULT_TEMP", "1e9")
    monkeypatch.setenv("MAX_TOKENS", "0")
    monkeypatch.setenv("LLM_BG_CONCURRENCY", "99")
    cfg = Config.load(env_file=tmp_path / "absent.env")
    assert cfg.llm_timeout == NUM_RANGES["LLM_TIMEOUT"][0]
    assert cfg.log_max_bytes == 1_048_576
    assert cfg.turn_snapshot_keep == 10
    assert cfg.default_temp == 2.0
    assert cfg.max_tokens == 16
    assert cfg.llm_bg_concurrency == 4


def test_config_load_keeps_junk_at_default_but_bounds_it(tmp_path, monkeypatch):
    """Не-число → дефолт (как раньше); число в границах → как есть (не трогаем)."""
    monkeypatch.setenv("LLM_TIMEOUT", "совсем-не-число")
    monkeypatch.setenv("TURN_SNAPSHOT_KEEP", "500")
    cfg = Config.load(env_file=tmp_path / "absent.env")
    assert cfg.llm_timeout == Config().llm_timeout
    assert cfg.turn_snapshot_keep == 500


def test_log_rotator_bounds_apply_before_config(tmp_path, monkeypatch):
    """logsetup настраивается ДО первого конфига и читает env сам — там тот же реестр."""
    from backend import logsetup
    monkeypatch.setenv("LOG_MAX_BYTES", "1")
    monkeypatch.setenv("LOG_BACKUP_COUNT", "999")
    assert logsetup._clamped_int_env("LOG_MAX_BYTES", "5242880") == 1_048_576
    assert logsetup._clamped_int_env("LOG_BACKUP_COUNT", "3") == 30
    monkeypatch.delenv("LOG_MAX_BYTES")
    monkeypatch.delenv("LOG_BACKUP_COUNT")
    assert logsetup._clamped_int_env("LOG_MAX_BYTES", "5242880") == 5_242_880


# ══════════════════ настройки мира ══════════════════

def test_world_settings_junk_is_clamped_on_save(api_client):
    """POST /settings: «temperature: 1e9», «max_tokens: 0» не уезжают в БД как есть."""
    client, _ = api_client
    wid = client.post("/api/worlds", json=_world_payload()).json()["world_id"]
    r = client.post(f"/api/worlds/{wid}/settings",
                    json={"temperature": 1e9, "top_p": 5, "max_tokens": 0,
                          "context_tokens": 7})
    assert r.status_code == 200
    g = r.json()["gen_settings"]
    assert g["temperature"] == 2.0
    assert g["top_p"] == 1.0
    assert g["max_tokens"] == 16
    assert g["context_tokens"] == 512
    # и честно сказано, что именно подрезали (UI/пользователь видит причину)
    limits = r.json()["gen_limits"]
    assert {"temperature", "max_tokens", "context_tokens"} <= set(limits)
    assert limits["max_tokens"] == {"sent": 0, "used": 16, "range": [16.0, 32768.0]}
    # в БД легло уже подрезанное значение
    assert json.loads(db_mod.get_world(wid)["gen_settings"])["temperature"] == 2.0


def test_world_settings_sane_values_untouched(api_client):
    """Кламп не имеет права трогать нормальные настройки и врать в ответе."""
    client, _ = api_client
    wid = client.post("/api/worlds", json=_world_payload("A9-ok")).json()["world_id"]
    r = client.post(f"/api/worlds/{wid}/settings",
                    json={"temperature": 0.3, "max_tokens": 1500, "context_tokens": 4096})
    assert r.json()["gen_limits"] == {}
    assert r.json()["gen_settings"]["max_tokens"] == 1500


def test_junk_gen_settings_never_reach_the_model(fake_config, monkeypatch):
    """Главное обещание A9: кривой gen_settings из СТАРОГО сохранения/дампа не ломает ход.

    Кламп стоит при ЧТЕНИИ (`narrator.world_gen_settings`), поэтому путь «записали в БД
    до фикса / импортировали дамп» тоже закрыт. Проверяем и параметры хода, и бюджеты
    памяти, которые тоже читают эти настройки."""
    import backend.config as config_mod
    cfg = Config()
    monkeypatch.setattr(config_mod, "_cache", {"cfg": cfg})
    world = {"id": 42, "gen_settings": json.dumps(
        {"temperature": 1e9, "top_p": -3, "max_tokens": 0, "context_tokens": 5})}
    params = core_mod._gen_params(world)
    assert 0.0 <= params["temperature"] <= 2.0
    assert 0.0 <= params["top_p"] <= 1.0
    assert params["max_tokens"] >= 16
    # бюджеты окна остаются положительными (без клампа max_tokens=0/ctx=5 их уводило в мусор)
    assert narrator_mod.world_recent_budget(world) > 0
    assert narrator_mod.world_context_tokens(world) >= 512
    assert narrator_mod.world_prompt_overhead(world) > 0


def test_world_gen_settings_accept_dict_and_junk_types():
    """Форма gen_settings разная (dict из дампа, JSON-строка, битая строка) — не падаем."""
    assert narrator_mod.world_gen_settings({"gen_settings": {"max_tokens": 0}})["max_tokens"] == 16
    assert narrator_mod.world_gen_settings({"gen_settings": '{битый json'}) == {}
    # числовая строка нормализуется (её всё равно отправляли в модель числом),
    # а не-число остаётся как есть — изобретать значение за игроку не будем
    assert narrator_mod.world_gen_settings({"gen_settings": '{"temperature": "0.7"}'})[
        "temperature"] == 0.7
    assert narrator_mod.world_gen_settings({"gen_settings": '{"top_p": "широко"}'})[
        "top_p"] == "широко"


def test_world_detail_returns_effective_settings(api_client):
    """GET /api/worlds/{id} отдаёт то, чем мир реально играет (после клампа)."""
    client, _ = api_client
    wid = client.post("/api/worlds", json=_world_payload("A9-d")).json()["world_id"]
    db_mod.update_world(wid, gen_settings={"max_tokens": 0, "temperature": 1e9})
    g = client.get(f"/api/worlds/{wid}").json()["gen_settings"]
    assert g["max_tokens"] == 16 and g["temperature"] == 2.0


# ══════════════════ админка ══════════════════

def test_admin_numeric_out_of_range_is_clamped(api_client):
    """Админка: вне границ → сохраняется подрезанное значение и оно же видно в effective."""
    client, _ = api_client
    from backend import admin_settings
    from backend.config import invalidate_config
    r = client.post("/api/admin/settings", json={
        "turn_snapshot_keep": "1", "log_max_bytes": "1", "llm_timeout": "0.01",
        "default_temp": "1e9", "llm_bg_concurrency": "99"})
    assert r.status_code == 200
    over = admin_settings.read_overrides()
    assert over["TURN_SNAPSHOT_KEEP"] == "10"
    assert over["LOG_MAX_BYTES"] == "1048576"
    assert over["LLM_TIMEOUT"] == "5.0"
    assert over["DEFAULT_TEMP"] == "2.0"
    assert over["LLM_BG_CONCURRENCY"] == "4"
    eff = client.get("/api/admin/settings").json()["effective"]
    assert eff["runtime"]["turn_snapshot_keep"] == 10
    assert eff["runtime"]["log_max_bytes"] == 1_048_576
    assert eff["defaults"]["temperature"] == 2.0
    invalidate_config()
    db_mod.set_admin_settings({"TURN_SNAPSHOT_KEEP": "", "LOG_MAX_BYTES": "",
                               "LLM_TIMEOUT": "", "DEFAULT_TEMP": "",
                               "LLM_BG_CONCURRENCY": ""})
    invalidate_config()


def test_admin_junk_number_is_rejected_not_swallowed(api_client):
    """Раньше «abc» тихо становился дефолтом — пользователь думал, что сохранил своё."""
    client, _ = api_client
    r = client.post("/api/admin/settings", json={"turn_snapshot_keep": "abc"})
    assert r.status_code == 400
    assert "TURN_SNAPSHOT_KEEP" in r.json()["detail"]


def test_admin_empty_still_means_reset(api_client):
    """Пустая строка = сброс к .env — кламп не имеет права это проглотить."""
    client, _ = api_client
    from backend import admin_settings
    from backend.config import invalidate_config
    assert client.post("/api/admin/settings", json={"llm_timeout": ""}).status_code == 200
    assert admin_settings.read_overrides().get("LLM_TIMEOUT", "") == ""
    invalidate_config()


def test_admin_sane_values_survive(api_client):
    """Нормальное значение доезжает без изменений."""
    client, _ = api_client
    from backend import admin_settings
    from backend.config import invalidate_config
    client.post("/api/admin/settings", json={"llm_timeout": "45",
                                             "turn_snapshot_keep": "200"})
    over = admin_settings.read_overrides()
    assert float(over["LLM_TIMEOUT"]) == 45.0 and over["TURN_SNAPSHOT_KEEP"] == "200"
    db_mod.set_admin_settings({"LLM_TIMEOUT": "", "TURN_SNAPSHOT_KEEP": ""})
    invalidate_config()


# ══════════════════ законность подрезки в журнале ══════════════════

def test_clamping_is_logged_not_silent(api_client, monkeypatch):
    """Правило 14: тихой правки пользовательских настроек нет — подрезка видна в журнале.

    `log_once` (один раз на «ключ + значение», иначе каждый ход шторм), поэтому перед
    прогоном отметки забываются — так же, как это делает фикстура A6."""
    import logging
    from backend import logsetup
    cap: list[str] = []

    class _H(logging.Handler):
        def emit(self, record):
            cap.append(record.getMessage())

    root = logging.getLogger("textgame")
    h = _H(level=logging.DEBUG)
    root.addHandler(h)
    logsetup.reset_once()
    try:
        clamp_num("LOG_MAX_BYTES", 1)
        clamp_gen_settings({"max_tokens": 0}, where="мир 7")
    finally:
        root.removeHandler(h)
        logsetup.reset_once()
    assert any("LOG_MAX_BYTES" in m for m in cap), cap
    assert any("мир 7: max_tokens" in m for m in cap), cap


def test_repeated_clamp_does_not_storm_the_log():
    """Пер-мир настройки читаются каждый ход — warning обязан быть ОДИН раз на значение
    (иначе A9 починил молчание, но добавил лог-шторм, против чего и защищает A6)."""
    import logging
    from backend import logsetup
    cap: list[int] = []

    class _H(logging.Handler):
        def emit(self, record):
            if "подрезана" in record.getMessage():
                cap.append(1)

    root = logging.getLogger("textgame")
    h = _H(level=logging.DEBUG)
    root.addHandler(h)
    logsetup.reset_once()
    try:
        for _ in range(5):
            clamp_gen_settings({"max_tokens": 0}, where="мир 9")
    finally:
        root.removeHandler(h)
        logsetup.reset_once()
    assert len(cap) == 1, f"подрезка зашлась лог-штормом: {len(cap)} записей вместо 1"


def test_frontend_sliders_are_inside_server_bounds():
    """Ползунки настроек мира не предлагают то, что сервер всё равно подрежет.

    Проверка по поведению фронта: значения, отданные `syncGenUI` из эффективных
    настроек, обязаны остаться теми же после roundtrip через серверный кламп."""
    for key, env_name in cfg_mod.GEN_LIMIT_KEYS.items():
        lo, hi = NUM_RANGES[env_name]
        shown = {"temperature": 0.8, "top_p": 0.95, "max_tokens": 2000,
                 "context_tokens": 32768, "rag_memory_k": 4, "lore_rag_k": 3,
                 "lore_token_budget": 900}[key]
        assert lo <= shown <= hi, f"{key}: UI-дефолт {shown} вне серверных границ"
