# -*- coding: utf-8 -*-
"""Мобильная версия: игра с телефона в локальной сети.

Жалобы игрока (сессия 84):
  * «нет меню для просмотра карточек, персонажа и прочего» — кнопка-гамбургер жила
    ВНУТРИ боковой панели, а панель на узком экране скрыта за краем экрана: открыть
    её с телефона было НЕЧЕМ;
  * «быстрые действия не обновляются» — POST /suggest ждал ответа 12 с, а на локальной
    27B-модели один проход идёт 80–140 с → всегда таймаут и пустой список;
  * адаптив ломался: 100vh не учитывает адресную строку, быстрые действия выстраивались
    в столбик на пол-экрана.

Проверки поведенческие (по разметке/коду), без запуска браузера.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "frontend" / "style.css").read_text(encoding="utf-8")
APP = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")


# ── панель мира открывается с телефона ──────────────────────────────────

def test_sidebar_open_button_exists_outside_sidebar():
    """Кнопка открытия панели — в чате, а НЕ внутри самой панели (иначе на телефоне
    её не видно: панель уехала за край экрана)."""
    assert 'id="btn-sidebar-open"' in INDEX, "нет кнопки открытия панели на телефоне"
    # кнопка физически внутри <main class="chat">, а не в <aside class="sidebar">
    chat_part = INDEX.split('<main class="chat">', 1)[1].split("</main>", 1)[0]
    assert 'id="btn-sidebar-open"' in chat_part, "кнопка осталась вне чата"


def test_sidebar_open_button_wired():
    assert 'id="btn-sidebar-open"' in APP or '$("btn-sidebar-open")' in APP
    assert "sideOpen.onclick" in APP, "кнопка не подключена к открытию панели"


def test_sidebar_open_hidden_on_desktop():
    """На десктопе панель видна всегда — кнопка не должна мешать."""
    assert ".sidebar-open { display: none; }" in CSS


def test_sidebar_open_visible_on_mobile():
    mobile = CSS.split("@media (max-width: 900px)", 1)[1]
    assert ".sidebar-open" in mobile
    assert "display: inline-flex" in mobile.split(".sidebar-open", 1)[1][:200]


# ── подсказки не «залипают» ─────────────────────────────────────────────

def test_suggest_timeout_is_configurable_and_generous():
    """Жёсткие 12 с убивали подсказки на медленной модели — лимит должен быть
    настраиваемым и заведомо больше одного прохода модели."""
    from backend.config import get_config
    cfg = get_config()
    assert cfg.suggest_timeout >= 30, "слишком короткий таймаут подсказок"
    worlds = (ROOT / "backend" / "routers" / "worlds.py").read_text(encoding="utf-8")
    assert "suggest_timeout" in worlds
    assert "timeout=12)" not in worlds, "остался жёсткий таймаут 12 с"


def test_suggest_failure_is_logged_not_silent():
    """Правило 14: пустые подсказки из-за 401 больше не прячутся молчанием."""
    worlds = (ROOT / "backend" / "routers" / "worlds.py").read_text(encoding="utf-8")
    assert "подсказки не сгенерированы" in worlds


# ── адаптив ─────────────────────────────────────────────────────────────

def test_mobile_uses_dynamic_viewport_height():
    """100vh не учитывает адресную строку мобильного браузера — низ уезжает."""
    assert "100dvh" in CSS, "нет динамической высоты (dvh) для телефона"


def test_suggestion_and_compass_are_scrollable_rows():
    """Быстрые действия и «Рядом» — одна строка с горизонтальной прокруткой,
    а не столбик кнопок на пол-экрана."""
    mobile = CSS.split("@media (max-width: 900px)", 1)[1]
    block = mobile.split(".suggestion-bar, .compass-bar", 1)
    assert len(block) > 1, "нет мобильного правила для лент подсказок"
    rule = block[1].split("}", 1)[0]
    assert "flex-wrap: nowrap" in rule
    assert "overflow-x: auto" in rule


def test_input_is_tap_friendly_on_mobile():
    mobile = CSS.split("@media (max-width: 900px)", 1)[1]
    assert "font-size: 16px" in mobile, "iOS зумит страницу на полях мельче 16px"


def test_safe_area_insets_respected():
    """Кнопка «домой»/жесты на iPhone не должны перекрывать поле ввода."""
    assert "env(safe-area-inset-bottom" in CSS


# ── стартовый экран: одна колонка на телефоне ───────────────────────────

def test_start_layout_single_column_on_mobile():
    """«Мои миры» и «Новый мир» были жёстко в две колонки (320px + 1fr) — на 390px
    обе панели сжимались до нечитаемого, а карточки сюжетов шли по два столбика."""
    mobile = CSS.split("@media (max-width: 900px)", 1)[1]
    block = mobile.split(".start-layout", 1)[1].split("}", 1)[0]
    assert "flex-direction: column" in block
    assert "theme-grid { grid-template-columns: 1fr; }" in mobile


# ── открытие мира: без «пустого экрана» ─────────────────────────────────

def test_world_screen_shown_after_data_loaded():
    """Экран игры показывался ДО await API — на телефоне по Wi-Fi игрок видел
    пустое полотно с одной кнопкой «Провидение»."""
    body = APP.split("async function openWorld(", 1)[1].split("\n}", 1)[0]
    show_at = body.index('$("screen-game").style.display = "flex"')
    await_at = body.index("await API(`/api/worlds/${id}`)")
    assert await_at < show_at, "экран игры всё ещё показывается до загрузки данных"
    assert "Загрузка…" in body, "нет индикации ожидания на кнопке «Играть»"
