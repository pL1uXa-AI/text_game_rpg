/**
 * screenshot.mjs — снимок интерфейса игры для README (docs/images/showcase.png).
 *
 * Запуск: node scripts/screenshot.mjs [--url http://127.0.0.1:8002] [--world 37] [--out docs/images/showcase.png]
 *
 * Как работает: поднимает Chrome/Edge в headless-режиме с открытым DevTools-портом (без
 * сторонних зависимостей — CDP по WebSocket есть в Node 22+), ждёт, пока SPA отрисует мир,
 * и снимает кадр. Ничего не пишет в игру: только читает уже созданный мир.
 */
import { spawn } from "node:child_process";
import { mkdirSync, existsSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

const args = process.argv.slice(2);
const arg = (name, def) => {
  const i = args.indexOf(name);
  return i >= 0 && args[i + 1] ? args[i + 1] : def;
};

const BASE = arg("--url", "http://127.0.0.1:8002");
const OUT = resolve(arg("--out", "docs/images/showcase.png"));
const PORT = Number(arg("--port", "9222"));
const WIDTH = Number(arg("--width", "1600"));
const HEIGHT = Number(arg("--height", "1000"));
const SCALE = Number(arg("--scale", "1"));

const BROWSERS = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function httpJson(path) {
  const r = await fetch(`http://127.0.0.1:${PORT}${path}`);
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
}

/** Ждём, пока CDP-порт браузера ответит. */
async function waitForBrowser(timeoutMs = 20000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    try {
      await httpJson("/json/version");
      return true;
    } catch {
      await sleep(250);
    }
  }
  return false;
}

/** Минимальный CDP-клиент поверх WebSocket. */
class CDP {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    this.events = [];
    ws.addEventListener("message", (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
      } else if (msg.method) {
        this.events.push(msg);
      }
    });
  }

  static async connect(url) {
    const ws = new WebSocket(url);
    await new Promise((res, rej) => {
      ws.addEventListener("open", res, { once: true });
      ws.addEventListener("error", () => rej(new Error(`WS не открылся: ${url}`)), { once: true });
    });
    return new CDP(ws);
  }

  send(method, params = {}) {
    const id = ++this.id;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }));
  }

  /** Выполнить выражение в странице и вернуть значение. */
  async eval(expression) {
    const r = await this.send("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    if (r.exceptionDetails) {
      throw new Error(r.exceptionDetails.exception?.description || "ошибка в странице");
    }
    return r.result?.value;
  }
}

/** Ждём выполнения условия в странице. */
async function waitFor(cdp, expression, { timeout = 90000, label = "условие" } = {}) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    if (await cdp.eval(expression)) return true;
    await sleep(500);
  }
  throw new Error(`таймаут ожидания: ${label}`);
}

function launchBrowser(exe, userDataDir) {
  const child = spawn(
    exe,
    [
      "--headless=new",
      `--remote-debugging-port=${PORT}`,
      `--user-data-dir=${userDataDir}`,
      `--window-size=${WIDTH},${HEIGHT}`,
      `--force-device-scale-factor=${SCALE}`,
      "--hide-scrollbars",
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-extensions",
      "about:blank",
    ],
    { stdio: "ignore" },
  );
  return child;
}

async function main() {
  const exe = BROWSERS.find((p) => existsSync(p));
  if (!exe) throw new Error("не найден Chrome/Edge; укажи путь в BROWSERS");

  // профиль браузера — во временной папке, в репозиторий не попадает
  const userDataDir = resolve(".screenshot-profile");
  mkdirSync(userDataDir, { recursive: true });

  console.log(`[screenshot] браузер: ${exe}`);
  const child = launchBrowser(exe, userDataDir);
  let cdp;
  try {
    if (!(await waitForBrowser())) throw new Error("CDP браузера не поднялся");

    const target = await httpJson("/json/new?about:blank").catch(async () => {
      const list = await httpJson("/json/list");
      return list.find((t) => t.type === "page");
    });
    cdp = await CDP.connect(target.webSocketDebuggerUrl);
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width: WIDTH, height: HEIGHT, deviceScaleFactor: SCALE, mobile: false,
    });

    console.log(`[screenshot] открываю ${BASE} …`);
    await cdp.send("Page.navigate", { url: BASE });
    await waitFor(cdp, "document.readyState === 'complete'", { label: "загрузка страницы" });

    // Открываем мир: SPA кладёт выбранный мир в localStorage, но надёжнее — API + клик.
    const worldId = await cdp.eval(`(async () => {
      const r = await fetch('/api/worlds');
      const d = await r.json();
      const worlds = d.worlds || d;
      const w = worlds.find(x => (x.name||'').length) || worlds[0];
      return w ? w.id : null;
    })()`);
    if (!worldId) throw new Error("в игре нет ни одного мира — сначала создай мир");

    console.log(`[screenshot] мир id=${worldId}, жду отрисовку чата…`);
    await cdp.eval(`openWorld(${worldId})`);
    await waitFor(cdp, "document.querySelectorAll('#log .msg').length > 0",
      { label: "сообщения в чате" });
    // даём догрузиться побочным панелям (карточки/лор/состояние) и скроллим чат вниз
    await sleep(3500);
    await cdp.eval(`(() => { const l = document.getElementById('log'); l.scrollTop = l.scrollHeight; return true; })()`);
    await sleep(800);

    const shot = await cdp.send("Page.captureScreenshot", {
      format: "png",
      captureBeyondViewport: false,
    });
    mkdirSync(dirname(OUT), { recursive: true });
    writeFileSync(OUT, Buffer.from(shot.data, "base64"));
    console.log(`[screenshot] готово: ${OUT}`);
  } finally {
    try { if (cdp) cdp.ws.close(); } catch {}
    child.kill();
  }
}

main().catch((e) => {
  console.error("[screenshot] ошибка:", e.message);
  process.exit(1);
});
