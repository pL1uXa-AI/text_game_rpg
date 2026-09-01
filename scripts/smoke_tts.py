# -*- coding: utf-8 -*-
"""Smoke-тест TTS: статус, настройки мира, синтез кириллицы, фоновый синтез события."""
import asyncio
import json

import httpx

BASE = "http://127.0.0.1:8002"
WORLD = 18


async def main():
    c = httpx.AsyncClient(timeout=120)
    # 1. статус
    r = await c.get(f"{BASE}/api/tts/status")
    st = r.json()
    print("[status] libs:", st["libs"], "| piper voices:", st["voices"]["piper"], "| kokoro:", st["kokoro_ready"])
    # 2. настройки мира
    r = await c.get(f"{BASE}/api/worlds/{WORLD}/tts/settings")
    print("[get-settings]", json.dumps(r.json(), ensure_ascii=False))
    # 3. сохранить настройки
    r = await c.post(f"{BASE}/api/worlds/{WORLD}/tts/settings",
                     json={"enabled": True, "provider": "piper", "voice": "ru_RU-ruslan-medium",
                           "rate": "+0%", "auto_play": False})
    print("[set-settings]", r.status_code, json.dumps(r.json(), ensure_ascii=False)[:300])
    # 4. синтез с кириллицей
    r = await c.post(f"{BASE}/api/tts/test",
                     json={"provider": "piper", "voice": "ru_RU-ruslan-medium", "rate": "+0%",
                           "text": "Мрак окутал руины старой крепости. Ты слышишь далёкий вой."})
    d = r.json()
    print("[test-piper]", d.get("ok"), d.get("mime"), "size_kb:", len(d.get("data", "")) * 3 // 4 // 1024,
          "err:", d.get("error"))
    # 5. фоновый синтез события: последнее narrator-событие мира
    h = await c.get(f"{BASE}/api/worlds/{WORLD}/history")
    evs = h.json()
    narrator_evs = [e for e in evs if e["role"] == "narrator" and e.get("content", "").strip()]
    if narrator_evs:
        ev = narrator_evs[-1]
        eid = ev["id"]
        print(f"[event {eid}] role={ev['role']} seq={ev['seq']} len={len(ev['content'])}")
        r = await c.post(f"{BASE}/api/worlds/{WORLD}/events/{eid}/tts/retry")
        print("[retry]", r.status_code, r.json())
        for i in range(60):
            await asyncio.sleep(1.5)
            r = await c.get(f"{BASE}/api/worlds/{WORLD}/events/{eid}/tts/status")
            s = r.json()
            if s["tts_status"] in (2, -1):
                print(f"[status] -> {s}")
                break
            if i % 10 == 0:
                print("[status] ...", s["tts_status"])
        if s["tts_status"] == 2:
            r = await c.get(f"{BASE}/api/worlds/{WORLD}/events/{eid}/audio")
            print("[audio]", r.status_code, "bytes:", len(r.content), "mime:", r.headers.get("content-type"))
    await c.aclose()


asyncio.run(main())