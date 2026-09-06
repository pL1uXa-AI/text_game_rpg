// XSS-полигон A2 (аудит 41): гоняет НАСТОЯЩИЙ renderSetting() из frontend/app.js на DOM-заглушке
// и ищет «живой» HTML в том, что фронт записал в innerHTML. Запуск: node scripts/xss_probe.js state.json
const fs = require("fs"), vm = require("vm"), path = require("path");

function el(id) {
  return {
    id, style: {}, dataset: {}, value: "", textContent: "", innerHTML: "", offsetWidth: 0,
    children: [], addEventListener() {}, removeEventListener() {}, querySelectorAll: () => [],
    querySelector: () => null, appendChild(c) { return c; }, removeChild() {}, remove() {},
    insertBefore() {}, setAttribute(k, v) { this[k] = v; }, getAttribute() { return null; },
    focus() {}, click() {}, closest() { return null; }, scrollIntoView() {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    scrollTop: 0, scrollHeight: 0, clientHeight: 0,
  };
}
const store = {};
const doc = {
  getElementById: (id) => (store[id] = store[id] || el(id)),
  addEventListener() {}, querySelectorAll: () => [], querySelector: () => el("q"),
  createElement: (t) => el(t), body: el("body"), documentElement: el("html"), head: el("head"),
  readyState: "complete", title: "",
};
function ES() { this.close = () => {}; this.addEventListener = () => {}; }
const win = {
  document: doc,
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  addEventListener() {},
  location: { hash: "", origin: "http://x", pathname: "/", search: "" },
  fetch: async () => ({ ok: true, status: 200, json: async () => ({}), text: async () => "", headers: { get: () => null } }),
  setInterval: () => 0, setTimeout: () => 0, clearInterval() {}, clearTimeout() {},
  EventSource: ES, requestAnimationFrame() {},
  navigator: { userAgent: "node", clipboard: { writeText: async () => {} } },
  confirm: () => false, alert: () => {},
  matchMedia: () => ({ matches: false, addEventListener() {} }),
  scrollTo() {}, URL, AbortController, Blob,
  Audio: function () { this.play = async () => {}; },
};
win.window = win; win.self = win; win.globalThis = win;
const ctx = vm.createContext(win);
vm.runInContext(
  "this.console = console; this.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, c => '%' + c.charCodeAt(0).toString(16)) };",
  ctx);
const appJs = fs.readFileSync(path.join(__dirname, "..", "frontend", "app.js"), "utf8");
vm.runInContext(appJs, ctx, { filename: "app.js", timeout: 8000 });

const setting = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
ctx.__s = setting;
vm.runInContext("state.setting = __s; renderSetting(__s);", ctx, { filename: "probe.js" });

const dirty = "<img src=x onerror=alert(1)>";
const parts = [];
for (const k of Object.keys(store)) parts.push(k + "=" + (store[k].innerHTML || ""));
const html = parts.join("\n");
const live = html.match(/<img\s+src=x\s+onerror/gi) || [];
const rawTag = html.match(/<(script|iframe|svg)\b/gi) || [];
const escaped = (html.match(/&lt;img src=x onerror/gi) || []).length;
// «= 0»-подобных проверок тут нет: важно только, что грязный тег не стал разметкой.
const broken = (html.match(/"<\/|'><\/b>/g) || []).length;
console.log("escaped_hits=" + escaped, "live_tags=" + (live.length + rawTag.length), "markup_broken=" + broken);
process.stdout.write(escaped > 0 && live.length + rawTag.length === 0 && broken === 0 ? "XSS_PROBE_OK\n" : "XSS_PROBE_FAIL\n");
process.exit(escaped > 0 && live.length + rawTag.length === 0 && broken === 0 ? 0 : 1);
