# -*- coding: utf-8 -*-
"""Аудит 41, B1 — CI-шаг mypy снова запускает проверку, а не падает на первом файле.

Было: `.github/workflows/ci.yml` запускал `mypy backend`. В проекте НЕТ
`backend/__init__.py` (implicit namespace packages), поэтому mypy падал
«No parent module -- cannot perform relative import» на `routers/admin.py` и НЕ
анализировал остальные 37 файлов. Из-за `continue-on-error: true` красным это не
делалось никогда, т.е. типизация в CI не проверялась ВООБЩЕ — и молча деградировала
(сломанный шаг чинили в сессии 38 (D4), но тогда исправили только комментарий в
`mypy.ini`, строку CI забыли: отсюда «второй раз»).

Почему этот тест законен при инварианте 19 («тесты, сверяющие ТЕКСТ исходников/.bat —
smoke, а не регрессия»): here проверяется не текст, а ИСПОЛНЯЕМАЯ КОМАНДА шага. Мы
достаём из ci.yml реальную командную строку и ЗАПУСКАЕМ её, требуя код 0. Это ровно то
поведение, которое выполняет CI; регрессия («снова positional backend») ловится не
подстрокой, а ненулевым кодом падения mypy. Файл — конфиг CI, не код и не `.bat`, поэтому
структурная часть (какой флаг у шага) здесь — обоснованное исключение: по-другому
содержимое YAML-конфига из теста не наблюдается.

Дополнительно тест честен к себе: он проверяет, что «плохая» форма (`mypy backend`)
действительно падает — иначе его утверждение о хорошей форме было бы пустым.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

# Инвариант 18: mypy — опциональная зависимость (есть в CI, нет в requirements.txt)
# → importorskip, а не жёсткий импорт.
pytest.importorskip("mypy", reason="mypy не установлен локально (в CI ставится шагом)")

ROOT = Path(__file__).resolve().parent.parent
CI = ROOT / ".github" / "workflows" / "ci.yml"


def _step_run(step_name_part: str) -> str:
    """Взять тело `run:` шага CI по фрагменту его названия (без внешних yaml-зависимостей)."""
    text = CI.read_text(encoding="utf-8")
    # блок шага: - name: "...Mypy..." (далее идёт до следующего «- name:» / конца steps)
    pat = re.compile(r'-\s*name:\s*"?([^"\n]*' + re.escape(step_name_part) + r'[^"\n]*)"?\n'
                     r'((?:[ \t]+.*\n)+)')
    m = pat.search(text)
    assert m, f"в {CI.name} нет шага с названием, содержащим {step_name_part!r}"
    body = m.group(2)
    run = re.search(r'^[ \t]+run:[ \t]*(.*\n(?:[ \t]{8,}.*\n)*)', body, re.M)
    assert run, f"шаг {step_name_part!r} без команды run"
    return run.group(1).strip()


def _argv(run_cmd: str) -> list[str]:
    """Команда шага → argv для запуска. `mypy` подменяется интерпретатором теста
    (`sys.executable -m mypy`) — иначе тест зависел бы от PATH/venv прогонщика, а не от mypy."""
    first = run_cmd.split("||")[0].strip()          # «|| echo ...» — не часть проверки
    parts = first.split()
    assert parts and parts[0] in ("mypy", "python", "python3"), f"непонятная команда: {first!r}"
    assert parts[0] == "mypy", "шаг CI обязан звать mypy напрямую (как и остальные шаги)"
    return [sys.executable, "-m", "mypy", *parts[1:]]


def test_b1_ci_mypy_step_command_actually_passes():
    """Команда из CI-шага mypy запускается РЕАЛЬНО и обязана дать код 0.

    Это и есть регрессия B1: с positional `backend` mypy падает («No parent module»),
    поэтому тест стал бы красным в ту же секунду, как строку CI откатят.
    """
    cmd = _step_run("Mypy")
    assert "-p backend" in cmd or "-p=backend" in cmd, f"шаг mypy не берёт пакетом: {cmd!r}"
    argv = _argv(cmd)
    r = subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, f"шаг CI {argv!r} падает:\n{(r.stdout or '')[-1500:]}\n{(r.stderr or '')[-500:]}"
    assert "Success" in (r.stdout or ""), f"mypy молчит но и не проверяет: {r.stdout!r}"


def test_b1_positional_backend_is_broken_so_the_fix_matters():
    """Самопроверка теста: сломанная форма обязана падать, иначе утверждение выше пусто."""
    r = subprocess.run([sys.executable, "-m", "mypy", "backend"],
                       cwd=str(ROOT), capture_output=True, text=True, timeout=600)
    assert r.returncode != 0, "ожидаемое «No parent module» исчезло: форма снова рабочая"
    assert "No parent module" in (r.stdout or "")
