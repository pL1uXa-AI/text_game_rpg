# -*- coding: utf-8 -*-
"""Аудит 41, C2 (сессия 60): таблица API больше не может врать молча.

Было: README объявлял «Все пути (`app.openapi()` — 49; сверяется тестом
`test_openapi_paths_documented`)» — и врал обеими половинами предложения. Путей в схеме
приложения давно **50** (64 операции), а тест, на который стояла ссылка, в `tests/` НИКОГДА
не существовал: ссылка была декоративной, и расхождение пережило несколько сессий (ROADMAP
помечал его, но закрыть не мог). Класс тот же, что у C1, только объект другой: не «сколько»,
а «перечислено ли вообще всё».

Что проверяется (реальность — `app.openapi()`, а не тексты доков):

1. `test_openapi_paths_documented` — каждый путь схемы назван в таблице API README «🗂 API»
   (пользовательский список обязан быть полным: читатель решает по нему, что умеет игра). Там же
   `test_no_phantom_paths_in_readme_table` — в таблице нет путей, которых в приложении уже нет.
   Служебный список AGENT «🌐 ОСНОВНЫЕ ЭНДПОИНТЫ» полноты не обещает (это в названии), но и там
   названные пути обязаны существовать — `test_agent_section_lists_only_real_paths`.
   Сличение по нормализованным сегментам: `{world_id}` ≡ `{id}` ≡ `<seq>` (параметр — это `*`),
   а хвосты через эллипсис (`POST .../saves/{id}/load`) домысливаются от полного пути той же
   строки — иначе таблица вечно «недосказывала» бы из-за форматирования, а не содержания.
3. `test_operation_count_matches_readme` — README честно называет и число операций; сами числа
   (пути/операции) стоят под сверкой `scripts/doc_figures.py`, то есть переписываются `--sync`,
   а не глазами (граница C1/C2 снята: заявление внесено в реестр).
4. Пробы деградации (иначе 1–3 декоративны): удаление строки таблицы замечается, придуманная
   строка — тоже. Живые доки тест не пишет: портим текст в памяти.

Запуск: "…\\3.12.10\\python.exe" -X utf8 -m pytest tests/test_session60_openapi_documented.py -q
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from backend.app import app  # noqa: E402

README_API_HEAD = "## 🗂 API"      # README обязан покрывать СХЕМУ ЦЕЛИКОМ
AGENT_API_HEAD = "## 🌐 ОСНОВНЫЕ ЭНДПОИНТЫ"   # AGENT — «основные», полноты от него не требуют
SECTION_HEADS = (README_API_HEAD, AGENT_API_HEAD)
HTTP_METHODS = ("get", "post", "put", "patch", "delete")

# путь внутри markdown-строки таблицы: всегда с «api» (хвосты через «.../» — отдельный разбор)
PATH_TOKEN = re.compile(r"(?:/api|api)/[A-Za-z0-9_/{}<>.\-]*")
ELLIPSIS = re.compile(r"\.\.\./([A-Za-z0-9_/{}<>.\-]*)")
PARAM = re.compile(r"\{[^}]*\}|<[^<>]*>")


def norm(tok: str) -> tuple[str, ...]:
    """Путь → кортеж сегментов, где любой параметр свёрнут в «*» (?q=… отбрасывается)."""
    tok = tok.split("?")[0]
    return tuple("*" if PARAM.fullmatch(s) else s for s in tok.split("/") if s)


def section_rows(text: str, head: str) -> list[str]:
    """Строки таблиц (`|…`) раздела, который начинается заголовком `head`."""
    assert head in text, f"раздел «{head}» пропал из документа — правь константу теста, не доки"
    i = text.index(head)
    j = text.find("\n## ", i + len(head))
    return [ln for ln in text[i:j if j > 0 else len(text)].splitlines() if ln.strip().startswith("|")]


def documented_sigs(text: str, heads: tuple[str, ...] = SECTION_HEADS) -> set[tuple[str, ...]]:
    """Множество путей, которые таблицы API документа реально называют."""
    out: set[tuple[str, ...]] = set()
    for head in heads:
        if head not in text:
            continue
        for line in section_rows(text, head):
            fulls: list[str] = []
            for m in PATH_TOKEN.finditer(line):
                tok = m.group(0)
                if not tok.startswith("/"):
                    tok = "/" + tok
                fulls.append(tok)
                out.add(norm(tok))
            for m in ELLIPSIS.finditer(line):
                rest = m.group(1)
                for f in fulls:
                    segs = [s for s in f.split("/") if s]
                    for k in range(len(segs), 0, -1):
                        out.add(norm("/".join(segs[:k]) + "/" + rest))
    return out


def openapi_paths() -> dict[str, dict]:
    return app.openapi()["paths"]


def read_doc(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def readme_table_sigs() -> set[tuple[str, ...]]:
    return documented_sigs(read_doc("README.md"), (README_API_HEAD,))


# ─────────────────────────── 1. покрытие схемы ───────────────────────────

def test_openapi_paths_documented():
    """Каждый путь `app.openapi()` назван в таблице API README (обещанный тест, C2)."""
    sigs = readme_table_sigs()
    missing = [p for p in openapi_paths() if norm(p) not in sigs]
    assert not missing, ("недокументированные эндпоинты — добавь строку в README «🗂 API» "
                         "(ссылка в самом README обязана вести на живое покрытие):\n"
                         + "\n".join(f"  - {p}" for p in missing))


def test_agent_section_lists_only_real_paths():
    """Служебный список AGENT («основные») не обещает несуществующего: каждый НАЗВАННЫЙ там
    полным путём путь есть в схеме. Полноты от AGENT не требуют (заголовок говорит «основные»);
    проверяются только прямые упоминания, без раскрытия эллипсисов («.../tts/retry»), которые
    разбор домысляет щедро и по которым судить о вранье нельзя.
    """
    real = {norm(p) for p in openapi_paths()}
    ghosts = []
    for line in section_rows(read_doc("AGENT.md"), AGENT_API_HEAD):
        for m in PATH_TOKEN.finditer(line):
            tok = m.group(0)
            tok = tok if tok.startswith("/") else "/" + tok
            if norm(tok) not in real:
                ghosts.append(tok)
    assert not ghosts, f"AGENT называет пути, которых нет в схеме: {sorted(set(ghosts))}"


def test_doc_tables_are_not_empty_theatre():
    """Покрытие есть, и схемой живут операции (не только пути) — иначе «перечислено» пусто."""
    paths = openapi_paths()
    assert len(paths) >= 40, f"схема ужата до {len(paths)} путей — замер сломался?"
    ops = sum(len([m for m in item if m in HTTP_METHODS]) for item in paths.values())
    assert ops >= len(paths), f"замер операций бессмысленен: {ops} операций на {len(paths)} путей"


# ─────────────────────────── 2. обещаний-призраков нет ───────────────────────────

def test_no_phantom_paths_in_readme_table():
    """В таблице README нет путей, которых уже нет в приложении (обещание-призрак).

    Отдельно от проверки 1 и осознанно строже её для README: «строка удалена» и «строка
    осталась, но путь из приложения убрали» — разные поломки, и у них разные сообщения.
    (AGENT — список «основных» эндпоинтов: полноты от него не требуют, он проверен выше.)
    """
    real = {norm(p) for p in openapi_paths()}
    ghosts: list[str] = []
    for head in (README_API_HEAD,):
        for line in section_rows(read_doc("README.md"), head):
            for m in PATH_TOKEN.finditer(line):
                tok = m.group(0)
                tok = tok if tok.startswith("/") else "/" + tok
                if norm(tok) not in real:
                    ghosts.append(tok)
    assert not ghosts, "в таблице README перечислены эндпоинты, которых нет в схеме: " + "; ".join(sorted(set(ghosts)))


# ─────────────────────────── 3. правдивое число в README ───────────────────────────

def test_operation_count_matches_readme():
    """README не врёт ни в число путей, ни в число операций (синхронизируется реестром)."""
    import doc_figures as df  # noqa: PLC0415 — реестр живёт в scripts/

    paths, ops = df.measure_openapi(ROOT)
    m = re.search(r"`app\.openapi\(\)` — \*\*(\d{1,3})\s*пут[а-яё]*\s*/\s*(\d{1,3})\s*операц", read_doc("README.md"))
    assert m, ("README: заявление «`app.openapi()` — **N путей / M операций**» пропало из раздела "
               "«🗂 API» (или переехало — обнови якорь в FIGURES)")
    assert (int(m.group(1)), int(m.group(2))) == (paths, ops), (
        f"README: {m.group(1)} путей / {m.group(2)} операций, реально {paths}/{ops} "
        "→ python -X utf8 scripts/doc_figures.py --sync")
    assert "test_openapi_paths_documented" in read_doc("README.md"), \
        "README обязан ссылаться на ЖИВОЙ тест покрытия (C2: раньше ссылка вела в никуда)"


def test_registry_measures_openapi_numbers():
    """C2 закрыт (сессия 60): пути/операции OpenAPI ВЕДЬ в реестре, а не только в `--print`.

    Прежняя проба сессии 59 («заявлений сознательно нет») сторожила границу между C1 и C2;
    после закрытия C2 она покраснела бы, поэтому ПЕРЕВЕРНУТА: реестр ОБЯЗАН держать оба
    openapi-заявления, иначе цифра снова уйдёт в ручную правку (так она и совралась). Общую
    сверку всех чисел не дублируем — за этим следит test_session59::test_docs_figures_agree.
    """
    import doc_figures as df  # noqa: PLC0415

    assert "openapi_paths" in df.FIGURES and "openapi_ops" in df.FIGURES, \
        "числа OpenAPI вернулись в ручную правку — регистрируй их в FIGURES"
    paths, ops = df.measure_openapi(ROOT)
    assert df.FIGURES["openapi_paths"].measure(ROOT) == paths
    assert df.FIGURES["openapi_ops"].measure(ROOT) == ops
    claims = [c for c in df.collect_claims(ROOT) if c.figure.startswith("openapi")]
    assert len(claims) == 2, ("openapi-заявлений в доках должно быть ровно 2 (пути и операции), "
                              f"найдено {len(claims)}")
    got = {(c.figure, c.value) for c in claims}
    assert got == {("openapi_paths", paths), ("openapi_ops", ops)}, \
        f"README врёт про схему ({got} против {paths}/{ops}) → python -X utf8 scripts/doc_figures.py --sync"


# ─────────────────────────── 4. пробы деградации ───────────────────────────

def test_missing_row_is_caught():
    """Удалённая строка таблицы = красный тест (иначе проверка 1 ничего не стоит)."""
    readme = read_doc("README.md")
    gone = "/api/worlds/{world_id}/export/json"
    broken = "\n".join(ln for ln in readme.splitlines() if "export/json" not in ln)
    assert broken != readme, "проба не нашла строку экспорта"
    sigs = documented_sigs(broken, (README_API_HEAD,))
    assert norm(gone) not in sigs, "путь назван ещё где-то — проба обезврежена"


def test_phantom_row_is_caught():
    """Придуманная строка (такого пути в приложении нет) тоже обязана быть найдена разбором."""
    readme = read_doc("README.md")
    made_up = "| `GET /api/worlds/{id}/made-up-endpoint` | ✅ | 🤥 |\n"
    anchor = "| `GET /api/themes` |"
    i = next(ln for ln in readme.splitlines() if ln.startswith(anchor))
    broken = readme.replace(i + "\n", i + "\n" + made_up, 1)
    assert made_up in broken, "проба не вставила строку в таблицу README"
    assert norm("/api/worlds/{id}/made-up-endpoint") not in {norm(p) for p in openapi_paths()}
    assert norm("/api/worlds/{id}/made-up-endpoint") in documented_sigs(broken), \
        "разбор таблиц не видит новых строк — проверка призраков пуста"


@pytest.mark.parametrize("doc,head", (("README.md", "## 🗂 API"), ("AGENT.md", "## 🌐 ОСНОВНЫЕ ЭНДПОИНТЫ")))
def test_api_sections_still_exist(doc: str, head: str):
    """Оба раздела API на месте: исчезновение таблицы не должно делать тест «зелёным»."""
    assert head in read_doc(doc), f"раздел «{head}» пропал из {doc} — сверка покрытия пуста"
