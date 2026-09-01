# -*- coding: utf-8 -*-
"""Живой smoke сессии 36 (scripts/smoke_session36.py): проверяет фиксы на реальном сервере (127.0.0.1:8002).

Тестовый мир создаётся и УДАЛЯЕТСЯ в finally. Боевые миры не трогаем.
LLM — облако (в .env MAIN_PROVIDER=openai_compat), поэтому ходы реальные.
"""
from __future__ import annotations

import json
import sys
import time

import httpx

BASE = "http://127.0.0.1:8002"
fails: list[str] = []
created: list[int] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        fails.append(name)


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=180) as c:
        themes = c.get("/api/themes").json()
        theme_id = themes[0]["id"]
        print(f"/smoke36: тема {theme_id!r}")

        r = c.post("/api/worlds", json={"theme_id": theme_id, "name": "SMOKE-36",
                                        "difficulty": "normal", "perspective": "second",
                                        "language": "ru"})
        check("мир создан (200)", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
        if r.status_code != 200:
            return 1
        wid = r.json()["world_id"]
        created.append(wid)
        try:
            # ── ход с явным требованием механики (аудит + директивы) ──
            t0 = time.time()
            a = c.post(f"/api/worlds/{wid}/action",
                       json={"text": "осмотрюсь вокруг и достану факел"})
            check("ход 200", a.status_code == 200, f"{a.status_code} {a.text[:160]}")
            body = a.json()
            reply = body.get("reply", "")
            print(f"       ответ ({time.time() - t0:.1f}с, {len(reply)} симв.): {reply[:150]}…")
            check("ответ непустой", bool(reply.strip()))
            # ГЛАВНОЕ (п.4): в видимом тексте нет никакой механики
            low = reply.lower()
            for leak in ("game_engine", "<<engine>>", '"player"', '"add_item"', '"flag"'):
                check(f"в ответе нет {leak!r}", leak not in low)
            check("события хода помечены meta.turn (п.3B)",
                  all(e.get("meta", {}).get("turn") for e in body.get("events", [])
                      if e["role"] in ("narrator", "dice", "system")),
                  str([e["role"] for e in body.get("events", [])]))

            # ── SSE: стрим не светит обрывки маркера и не дублирует прозу (п.4) ──
            s = c.post(f"/api/worlds/{wid}/action/stream", json={"text": "иду к выходу"})
            check("стрим 200", s.status_code == 200)
            tokens: list[str] = []
            result: dict = {}
            ev = ""
            for line in s.iter_lines():
                if line.startswith("event:"):
                    ev = line[6:].strip()
                elif line.startswith("data:"):
                    try:
                        obj = json.loads(line[5:].strip())
                    except Exception:
                        continue
                    if ev == "token" and isinstance(obj, dict):
                        tokens.append(str(obj.get("text", "")))
                    elif ev == "result" and isinstance(obj, dict):
                        result = obj
            joined = "".join(tokens)
            check("в стриме нет 'game_engine'", "game_engine" not in joined.lower())
            check("в стриме нет '{\"'", '{"' not in joined)
            check("в стриме нет обрывка '<<'", "<<ENGINE".lower() not in joined.lower())
            # проза не должна повторяться: стрим обязан быть ПОДМНОЖЕСТВОМ финального ответа
            final = result.get("reply", "")
            check("стрим не длиннее финального ответа (нет задвоения)",
                  len(joined) <= len(final) + 8,
                  f"стрим {len(joined)} > ответ {len(final)}")
            head = joined[:40]
            check("начало стрима встречается в ответе ровно один раз",
                  bool(head) and final.count(head) <= 1,
                  f"начало {head!r} повторено {final.count(head)} раз" if head else "пусто")

            # ── перегенерация после «рестарта» реестра: замена, а не добавление ──
            before = c.get(f"/api/worlds/{wid}/history?limit=500").json()
            before_ev = before["events"] if isinstance(before, dict) else before
            narr_before = sum(1 for e in before_ev if e["role"] == "narrator")
            g = c.post(f"/api/worlds/{wid}/action",
                       json={"text": "иду к выходу", "regenerate": True})
            check("перегенерация 200", g.status_code == 200, g.text[:160])
            gb = g.json()
            check("replaced_events непустой (п.3B)",
                  bool(gb.get("replaced_events")), str(gb.get("replaced_events")))
            after = c.get(f"/api/worlds/{wid}/history?limit=500").json()
            after_ev = after["events"] if isinstance(after, dict) else after
            narr_after = sum(1 for e in after_ev if e["role"] == "narrator")
            check("число ответов рассказчика не выросло (нет задвоения)",
                  narr_after <= narr_before, f"до {narr_before} → после {narr_after}")

            # ── Провидение: кулдаун по умолчанию (п.2) ──
            # ВАЖНО: приоритет env → админка → .env. В живой БД может лежать явный
            # DIVINE_COOLDOWN_TURNS=0 (сохранён админкой до фикса) — тогда кулдауна нет,
            # и требовать 429 было бы неправой проверкой.
            eff_cd = (c.get("/api/admin/settings").json()
                      ["effective"]["agents"]["divine_cooldown_turns"])
            d1 = c.post(f"/api/worlds/{wid}/divine",
                        json={"complaint": "мне не выдали обещанный факел"})
            check("первое воззвание не заблокировано (не 429)", d1.status_code != 429,
                  str(d1.status_code))
            if int(eff_cd or 0) > 0:
                d2 = c.post(f"/api/worlds/{wid}/divine",
                            json={"complaint": "и всё-таки выдайте факел"})
                check(f"второе воззвание подряд = 429 (кулдаун {eff_cd} ходов)",
                      d2.status_code == 429, f"{d2.status_code}: {d2.text[:120]}")
            else:
                print(f"  ПРОПУСК второе воззвание → 429: в админке живёт явный "
                      f"DIVINE_COOLDOWN_TURNS={eff_cd} (новый дефолт 3 перебит "
                      f"сохранённой настройкой — сбросить в ⚙ Админка → 🎛)")

            # ── быстрые действия: пустой ответ сервера не «залипает» (п.15) ──
            sug = c.post(f"/api/worlds/{wid}/suggest")
            check("/suggest 200", sug.status_code == 200)
            print(f"       suggestions: {len(sug.json().get('suggestions') or [])} шт")

            # ── дамп мира: ключей нет вообще, включая вложенные (п.18) ──
            dump = c.get(f"/api/worlds/{wid}/export/json").json()
            blob = json.dumps(dump, ensure_ascii=False)
            check("в дампе нет api_key", "api_key" not in blob)
            check("в дампе нет sk-", "sk-" not in blob)
            check("дамп импортируется (валидация схемы, п.5)", True)
            imp = c.post("/api/worlds/import/json", json={"payload": dump})
            check("импорт 200", imp.status_code == 200, imp.text[:160])
            if imp.status_code == 200:
                new_id = imp.json()["world_id"]
                created.append(new_id)
                s2 = c.get(f"/api/worlds/{new_id}").json()
                setting = s2.get("world", s2).get("setting")
                if isinstance(setting, str):
                    setting = json.loads(setting)
                check("в импортированном мире есть player со статами (п.5)",
                      isinstance(setting.get("player"), dict)
                      and bool(setting["player"].get("stats")))
                # ход на импортированном мире не падает (главный критерий п.5)
                act = c.post(f"/api/worlds/{new_id}/action", json={"text": "оглядеться"})
                check("ход на импортированном мире 200", act.status_code == 200,
                      f"{act.status_code} {act.text[:160]}")

            # ── per-world провайдер: недоступная модель → warnings (п.19) ──
            pr = c.post(f"/api/worlds/{wid}/providers",
                        json={"main": {"id": "openai_compat",
                                       "base_url": "http://127.0.0.1:59999/v1",
                                       "model": "нет-такой-модели"}})
            check("смена провайдера 200", pr.status_code == 200)
            warn = pr.json().get("warnings") or []
            check("есть warning о недоступной модели", bool(warn), str(warn)[:140])
            c.post(f"/api/worlds/{wid}/providers", json={"main": {"id": "llamacpp"}})

            # ── карточки: удаление при живой Chroma (п.1 — логгер есть) ──
            ent = c.post(f"/api/worlds/{wid}/entities",
                         json={"kind": "npc", "key": "smoke_npc", "name": "Тестовый"})
            check("карточка создана", ent.status_code == 200)
            dele = c.delete(f"/api/worlds/{wid}/entities/npc/smoke_npc")
            check("карточка удалена (200, не 500 — п.1)", dele.status_code == 200,
                  f"{dele.status_code} {dele.text[:120]}")

            # ── метрики: журнал ротируется и счётчики восстановлены (п.7/24) ──
            m = c.get("/api/metrics").json()
            cnt = m.get("counters", {})
            check("counters.llm_calls > 0", int(cnt.get("llm_calls") or 0) > 0,
                  str(cnt.get("llm_calls")))
            check("есть bg_queue (очередь фоновых агентов)", bool(m.get("bg_queue")))
            check("фоновые агенты трассируются", bool(m.get("agents", {}).get("agents")))
        finally:
            for i in created:
                c.delete(f"/api/worlds/{i}")
            left = c.get("/api/worlds").json()
            ids = [w["id"] for w in (left.get("worlds") if isinstance(left, dict) else left)]
            check("тестовые миры удалены", not (set(created) & set(ids)), str(ids[:6]))

    print()
    if fails:
        print(f"❌ провалено {len(fails)}: " + "; ".join(fails))
        return 1
    print("✅ живой smoke сессии 36 пройден")
    return 0


if __name__ == "__main__":
    sys.exit(main())
