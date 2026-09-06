# -*- coding: utf-8 -*-
r"""Аудит 41, B4 (сессия 57): правда о мусоре в рабочем дереве.

Было: `nul` (0 байт) и `server_restart.log` вернулись в корень рабочей копии — их плодит любое
`>nul` в `.bat` (`start_game.bat:10,94`, `scripts/setup_env.bat`) и ручной перезапуск сервера с
перенаправлением вывода. В git они не попадают (`.gitignore`), но AGENT.md, правило 15,
утверждал «мусор удалён в сессии 39» — то есть врёт каждой следующей сессии (ровно тот класс
дефекта «док не врёт», что C1/C2).

Что проверяется (НИЧТО не удаляет файлы рабочей копии — это решение владельца, правило 15):

1. Канон мусора (список правила 15) покрыт `.gitignore`: `git check-ignore` говорит «игнорируется»
   по каждому шаблону — поэтому ни один тест не может покраснеть из-за того, что `nul` снова на диске.
2. Проба честна к себе: если вырезать из `.gitignore` ВСЕ строки, накрывающие путь, `check-ignore`
   обязан стать «не игнорируется» (иначе проверка 1 ничего не проверяла бы).
3. Реально лежащие в дереве мусорные файлы — игнорируемые (присутствие = не поломка, а норма).
4. Живая часть дока («ЖЁСТКИЕ ПРАВИЛА» + «Инварианты») не может заявлять «мусор удалён» без
   оговорки «может появляться локально» и без подсказки, что `nul` снимается только
   `del \\?\<абсолютный путь>` (обычный rm/del его не видит). Исторические сводки сессий под
   проверку НЕ попадают — они и должны оставаться как были (AGENT.md: «здесь только действующие
   инварианты», устаревшие цифры — часть истории).

Запуск: "…\\3.12.10\\python.exe" -X utf8 -m pytest tests/test_session57_worktree_trash.py -q
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

GIT = "git"

# Канон из правила 15 (шаблоны, а не конкретные файлы: `nul` и `server*.log` возвращаются).
TRASH_GLOBS = [
    ".env.bak",                 # .env.*
    "nul",
    "server_restart.log",       # server*.log + *.log
    "data/fastembed_test/model.onnx",
    "scripts/_test_repair.py",
]


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([GIT, *args], cwd=str(cwd or ROOT), capture_output=True,
                          text=True, timeout=60)


def _ignored(path: str, cwd: Path) -> bool:
    """True если git считает путь игнорируемым (--no-index: проверка без индекса и коммитов)."""
    r = _git("check-ignore", "-v", "--no-index", path, cwd=cwd)
    return r.returncode == 0


def test_trash_canon_is_covered_by_gitignore():
    """Ни один путь из канона мусора не может попасть в репозиторий (правило 15)."""
    bare = _git("check-ignore", "--no-index", *TRASH_GLOBS)
    assert bare.returncode == 0, (bare.stdout, bare.stderr)
    # и поштучно с указанием конкретной строки .gitignore — иначе «0» ничего не объясняет
    for p in TRASH_GLOBS:
        r = _git("check-ignore", "-v", "--no-index", p)
        assert r.returncode == 0, f"{p} НЕ игнорируется: .gitignore не кроет канон правила 15"
        assert r.stdout.startswith(".gitignore:"), f"{p}: непонятно, какое правило сработало"


def test_gitignore_cover_is_real_not_vacuous(tmp_path):
    """Вырезание накрывающих путь строк обязано сделать путь «видимым» для git.

    Без этой пробы проверка выше «зеленела» бы и на пустом .gitignore (класс «тест ничего не
    проверяет», тот же приём, что degradation-пробы в tests/test_session38_tails.py).
    """
    r = _git("init", "-q", cwd=tmp_path)
    assert r.returncode == 0, r.stderr[-200:]
    src = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for p in TRASH_GLOBS:
        lines = list(src)
        cut: list[str] = []
        for _ in range(6):
            work = tmp_path / ".gitignore"
            work.write_text("\n".join(lines) + "\n", encoding="utf-8")
            rr = _git("check-ignore", "-v", "--no-index", p, cwd=tmp_path)
            if rr.returncode != 0:
                assert rr.returncode == 1, f"{p}: check-ignore сломался: {rr.stderr[-200:]}"
                break
            m = re.match(r"^\.gitignore:(\d+):", rr.stdout)
            assert m, f"{p}: check-ignore не назвал строку правила: {rr.stdout!r}"
            n = int(m.group(1))
            assert lines[n - 1] not in cut, f"{p}: зацикливание на строке {n}"
            cut.append(lines[n - 1])
            del lines[n - 1]
        else:
            raise AssertionError(f"{p}: правила перебираются бесконечно")
        assert cut, f"{p}: в оригинале не игнорируется вовсе — проверка 1 пуста"
    # фальшивых срабатываний нет: обычный код проекта игнорируемым не считается
    (tmp_path / ".gitignore").write_text("\n".join(src) + "\n", encoding="utf-8")
    assert not _ignored("backend/app.py", tmp_path), "check-ignore врёт: исходник «игнорируется»"


def _worktree_trash() -> list[str]:
    """Мусорные файлы, которые реально лежат в рабочем дереве (только чтение, ничего не удаляем)."""
    found: list[str] = []
    for pat in ("nul", "server*.log", ".env.bak*"):
        found += [str(f.relative_to(ROOT)) for f in ROOT.glob(pat)]
    for d in ("data/fastembed_test",):
        if (ROOT / d).exists():
            found.append(d)
    if (ROOT / "scripts" / "_test_repair.py").exists():
        found.append("scripts/_test_repair.py")
    return found


def test_existing_trash_does_not_break_anything():
    """То, что `nul` и `server_restart.log` снова на диске, — не поломка: файлы игнорируемые.

    Именно поэтому док не имеет права утверждать «удалено» без оговорки, а тест не имеет права
    требовать чистоты рабочего дерева (иначе он краснел бы на следующем же `start_game.bat`).
    """
    trash = _worktree_trash()
    for t in trash:
        assert _ignored(t, ROOT), f"{t}: лежит в дереве и НЕ игнорируется — риск попасть в git"
    # в индексе их быть не может (это и есть настоящая граница — тест B1 из сессии 38/39)
    tracked = set(_git("ls-files").stdout.splitlines())
    assert not [t for t in trash if t in tracked], f"мусор в индексе git: {tracked & set(trash)}"


# ─────────────── правда дока о мусоре (живые разделы AGENT.md) ───────────────

LIVE_SECTIONS = ("## ⚠️ ЖЁСТКИЕ ПРАВИЛА", "### Инварианты (")
# Оговорка, без которой «удалён в сессии 39» = ложь следующей сессии.
HONEST_MARKS = ("могут появляться локально", "Рабочее дерево", "разрешени", "nul")


def _live_text() -> str:
    """Только живые разделы правил/инвариантов — исторические сводки сессий не правятся."""
    body = (ROOT / "AGENT.md").read_text(encoding="utf-8")
    out: list[str] = []
    for head in LIVE_SECTIONS:
        i = body.find(head)
        assert i >= 0, f"AGENT.md: раздел {head!r} не найден — обнови разметку проверки"
        j = re.search(r"(?m)^#{2,3} ", body[i + len(head):])
        out.append(body[i:i + len(head) + (j.start() if j else 0)])
    return "\n".join(out)


def _rule15_lies(text: str) -> list[str]:
    """Что тут врёт: возвращает список претензий (пусто = текст честен)."""
    problems: list[str] = []
    claims = [m for m in re.finditer(r"[Мм]усор[^\n]{0,400}?удал[её]н", text)]
    for m in claims:
        seg = text[m.start(): m.start() + 1600]
        if "могут появляться локально" not in seg:
            problems.append("«мусор … удалён» без оговорки, что файлы могут вернуться в дерево")
        if "nul" not in seg or "del " not in seg:
            problems.append("нет подсказки, как вообще снимать nul на Windows")
    return problems


def test_rule15_admits_trash_can_come_back():
    assert _rule15_lies(_live_text()) == [], _rule15_lies(_live_text())


def test_doc_lie_checker_detects_regression():
    """Возврат прежней формулировки («удалён в сессии 39» без оговорки) обязан быть пойман."""
    old = ("Мусор рабочей копии (`.env.bak*`, `nul`, `server*.log`) удалён в сессии 39 по "
           "решению владельца; тест не даст таким путям попасть в индекс.")
    lies = _rule15_lies(old)
    assert lies, "чекер ослеп: ложная «мусор удалён» проходит как честная"
    assert any("оговорк" in p for p in lies) and any("nul" in p for p in lies)
    # и на исправленном тексте — ни одной претензии (фальшивых срабатываний нет)
    assert _rule15_lies(
        "Мусор рабочей копии (`nul`) удалён в сессии 39 — но такие файлы могут появляться "
        "локально; удалять только с разрешения владельца, `nul` снимается `del \\\\?\\path\\nul`.") == []


def test_trash_hint_names_the_real_sources():
    """Правило 15 объясняет ПРИЧину возврата — иначе следующая сессия снова «чинит» симптом."""
    live = _live_text()
    i = live.find("могут появляться локально")
    assert i >= 0
    seg = live[i:i + 700]
    assert ">nul" in seg or "> nul" in seg, "не назван источник `nul` — перенаправление в .bat"
    assert "server" in seg, "не назван источник server*.log — ручной перезапуск сервера"
    # и эти источники не выдуманы: `>nul` правда стоит в bat-скриптах
    bat_src = "\n".join(p.read_text(encoding="utf-8", errors="replace").lower()
                        for p in [ROOT / "start_game.bat", *sorted((ROOT / "scripts").glob("*.bat"))])
    assert re.search(r">\s*nul\b", bat_src), "проверка ссылается на `>nul`, а в .bat его нет"


def test_honest_marks_present_in_rule15():
    live = _live_text()
    m = re.search(r"^15\.\s+\*\*Git и секреты.*$", live, flags=re.M)
    assert m, "правило 15 «Git и секреты» не найдено — обнови регулярку проверки"
    para = m.group(0)
    missing = [k for k in HONEST_MARKS if k not in para]
    assert not missing, f"в правиле 15 нет обязательных оговорок: {missing}"
    assert "test_b1_trash_files_stay_out_of_git" in para, "ссылка на тест индекса потеряна"
    assert "test_session57_worktree_trash.py" in para, "нет ссылки на проверку правдивости дока"
