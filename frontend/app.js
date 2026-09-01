/* Text Game RPG — frontend logic (vanilla JS) */
"use strict";

const $ = (id) => document.getElementById(id);
const state = { worlds: [], themes: [], genres: [], selectedGenres: [], plots: [], narrators: [], providers: null, providersEffective: null,
                currentWorld: null, setting: null, gen: {}, streaming: false, seenSeq: 0, pollTimer: null,
                tts: null, ttsStatus: null, ttsSettingsRaw: {}, playingTts: null, memoryDefaults: {}, eventSource: null };

const API = (path, opts = {}) =>
  fetch(path, { headers: { "Content-Type": "application/json" }, ...opts })
    .then(async (r) => {
      const data = await r.json().catch(() => null);
      if (!r.ok) throw new Error((data && (data.detail || data.error)) || r.statusText);
      return data;
    });

const DIFF_LABELS = { easy: "лёгкая", normal: "средняя", hardcore: "хардкор" };
const DIFF_SHORT = { easy: "Лёгкая", normal: "Средняя", hardcore: "Хардкор" };

/* ─────────────── Стартовый экран ─────────────── */
async function loadGenres() {
  state.genres = await API("/api/genres");
  const box = $("genre-chips");
  if (!box) return;
  box.innerHTML = "";
  state.genres.forEach((g) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "chip-toggle";
    b.dataset.id = g.id;
    b.innerHTML = esc(g.name);
    b.title = g.hint || "";
    b.onclick = () => {
      b.classList.toggle("on");
      state.selectedGenres = [...box.querySelectorAll(".chip-toggle.on")].map((x) => x.dataset.id);
    };
    box.appendChild(b);
  });
}

async function loadThemes() {
  state.themes = await API("/api/themes");
  // D15 (сессия 38): битые файлы сюжетов больше не исчезают молча — показываем причину
  // прямо в окне «Новый мир» (раньше: только log.warning, игрок видел «сюжетов меньше»).
  try {
    const pe = await API("/api/plots/errors");
    const box = $("plots-errors");
    if (box) {
      const errs = (pe && pe.errors) || [];
      box.style.display = errs.length ? "" : "none";
      box.innerHTML = errs.length
        ? "⚠ Сюжеты с ошибками (не показаны в списке): " + errs.map((e) =>
            `<div><b>${esc(e.file)}</b> — ${esc(e.error)}</div>`).join("")
        : "";
    }
  } catch (_) { /* best-effort: каталог важнее плашки */ }
  const grid = $("theme-grid");
  grid.innerHTML = "";
  // Первая карточка — свой (кастомный) сюжет
  const custom = document.createElement("div");
  custom.className = "theme-card custom-plot-card";
  custom.setAttribute("data-id", "custom");
  custom.innerHTML = `<h4>✍️ Свой сюжет</h4><div class="g">любой жанр</div><p>Придумай завязку сам — рассказчик развернёт её в живой мир.</p>`;
  custom.onclick = () => selectThemeCard(custom, "custom");
  grid.appendChild(custom);
  state.themes.forEach((t) => {
    const card = document.createElement("div");
    card.className = "theme-card";
    card.setAttribute("data-id", t.id);
    const badge = t.group ? `<span class="g-badge ${t.group}">${t.group === "system" ? "системный" : "мой"}</span>` : "";
    card.innerHTML = `<h4>${badge}${esc(t.name)}</h4><div class="g">${esc(t.genre)}</div><p>${esc(t.desc)}</p>`;
    card.onclick = () => selectThemeCard(card, t.id);
    grid.appendChild(card);
  });
}

async function reloadPlots() {
  const b = $("btn-reload-plots");
  if (b) b.disabled = true;
  try {
    const r = await API("/api/plots/reload", { method: "POST" });
    await loadThemes();
    if (b) {
      const orig = b.textContent;
      b.textContent = `✅ Обновлено (${r.total})`;
      setTimeout(() => { b.textContent = orig; b.disabled = false; }, 1400);
    }
  } catch (e) {
    if (b) { b.disabled = false; b.textContent = "⚠ " + e.message; }
  }
}

function selectThemeCard(card, id) {
  const grid = $("theme-grid");
  grid.querySelectorAll(".theme-card").forEach((c) => c.classList.remove("selected"));
  card.classList.add("selected");
  $("btn-create-world").disabled = false;
  state.selectedTheme = id;
  state.selectedPlotId = null;               // выбор готового сюжета снимается
  renderPlotsHighlight();
  $("custom-plot-box").style.display = id === "custom" ? "" : "none";
  if (id === "custom") $("custom-plot-text").focus();
  // Рекомендуемый рассказчик из сюжета: подставляем в селект, но пользователь может сменить.
  const theme = state.themes.find((t) => t.id === id);
  const sel = $("new-narrator");
  if (theme && theme.narrator_id && sel) {
    const exists = state.narrators.some((n) => n.id == theme.narrator_id);
    if (exists && sel.value != theme.narrator_id) {
      sel.value = theme.narrator_id;
      const prev = $("new-narrator-preview");
      if (prev) {
        const n = state.narrators.find((x) => x.id == theme.narrator_id);
        prev.textContent = n ? (n.desc || "") : "";
      }
    }
  }
}

async function loadPlots() {
  state.plots = await API("/api/plots");
  state.plotsShown = state.plotsShown || PLOT_PAGE;
  renderPlots();
}

const PLOT_PAGE = 10;
function renderPlots() {
  const list = $("plots-list");
  if (!list) return;
  const shown = state.plots.slice(0, state.plotsShown);
  list.innerHTML = shown.map((p) => `
    <div class="plot-item" data-pid="${p.id}">
      <div class="p-left">
        <div class="p-name">${esc(p.name)}</div>
        <div class="p-plot muted">${esc(p.plot.slice(0, 90))}${p.plot.length > 90 ? "…" : ""}</div>
      </div>
      <div class="e-actions">
        <button class="btn small" data-act="open" data-pid="${p.id}">Выбрать</button>
        <button class="btn small" data-act="edit" data-pid="${p.id}">✎</button>
        <button class="btn small danger" data-act="del" data-pid="${p.id}">🗑</button>
      </div>
    </div>`).join("") || `<p class="muted">Пока пусто — добавь свой сюжет, чтобы быстро начинать по нему миры.</p>`;
  if (state.plots.length > state.plotsShown) {
    list.insertAdjacentHTML("beforeend",
      `<div class="load-more"><button class="btn small" id="btn-plots-more">Показать ещё (${state.plots.length - state.plotsShown})</button></div>`);
    list.querySelector("#btn-plots-more").onclick = () => { state.plotsShown += PLOT_PAGE; loadPlots(); };
  }
  list.querySelectorAll("[data-act]").forEach((b) => {
    b.onclick = async () => {
      const pid = +b.dataset.pid;
      if (b.dataset.act === "del") {
        if (!confirm("Удалить сюжет?")) return;
        await API(`/api/plots/${pid}`, { method: "DELETE" });
        loadPlots();
      } else if (b.dataset.act === "edit") {
        const p = state.plots.find((x) => x.id === pid);
        plotModal(p);
      } else {
        state.selectedPlotId = pid;
        state.selectedTheme = "custom";
        $("btn-create-world").disabled = false;
        $("theme-grid").querySelectorAll(".theme-card").forEach((c) =>
          c.classList.remove("selected"));
        document.querySelector('.theme-card[data-id="custom"]').classList.add("selected");
        $("custom-plot-box").style.display = "none";   // сюжет задан заранее
        renderPlotsHighlight();
      }
    };
  });
  renderPlotsHighlight();
}

function renderPlotsHighlight() {
  document.querySelectorAll(".plot-item").forEach((el) => {
    el.classList.toggle("selected", state.selectedPlotId === +el.dataset.pid);
  });
}

function plotModal(p) {
  openModal(p ? "✎ Правка сюжета" : "➕ Новый сюжет", `
    <label>Название <input id="pl-name" value="${esc(p?.name || "")}" placeholder="Мой сюжет"></label>
    <label>Сюжет/завязка
      <textarea id="pl-plot" rows="6" placeholder="Опиши мир, завязку и интригу. Рассказчик превратит это в игру.">${esc(p?.plot || "")}</textarea></label>
    <label>Лор мира (необязательно)
      <textarea id="pl-lore" rows="6" placeholder="Статьи лора: строки «## Заголовок» начинают новую статью.\n## География\nКонтинент расколот на три королевства...">${esc(p?.lore || "")}</textarea></label>
    <p class="muted">Лор — «библия» мира (история, системы, фракции): подаётся рассказчику через RAG, дополняется в игре. Сохранённый сюжет появится в списке «Мои сюжеты».</p>`, async () => {
    const name = $("pl-name").value.trim();
    const plot = $("pl-plot").value.trim();
    if (!name || !plot) { alert("Название и текст сюжета обязательны"); throw new Error("empty"); }
    const path = p ? `/api/plots/${p.id}` : "/api/plots";
    await API(path, { method: p ? "PATCH" : "POST", body: JSON.stringify({ name, plot, lore: $("pl-lore").value.trim() }) });
    loadPlots();
  });
}

async function loadWorlds() {
  state.worlds = await API("/api/worlds");
  state.worldsShown = state.worldsShown || WORLD_PAGE;
  renderWorlds();
}

const WORLD_PAGE = 8;
function renderWorlds() {
  const list = $("world-list");
  list.innerHTML = "";
  if (!state.worlds.length) {
    list.innerHTML = `<p class="muted">Миров пока нет — создай первый справа.</p>`;
    return;
  }
  const shown = state.worlds.slice(0, state.worldsShown);
  shown.forEach((w) => {
    const card = document.createElement("div");
    card.className = "world-card";
    const tname = (state.themes.find((t) => t.id === w.theme) || {}).name || (w.theme === "custom" ? "Свой сюжет" : `Сюжет «${w.theme}» (удалён/правлен)`);
    card.innerHTML = `
      <div>
        <div class="w-name">${esc(w.name)}</div>
        <div class="w-meta">${esc(tname)} · ходов: ${w.events} · ${(DIFF_LABELS[w.difficulty] || w.difficulty)}</div>
      </div>
      <div class="w-btns">
        <button class="btn small" data-act="open">Играть</button>
        <button class="btn small" data-act="del">🗑</button>
      </div>`;
    card.onclick = (e) => {
      const act = e.target.closest("button")?.dataset.act;
      if (!act) openWorld(w.id);
      else if (act === "open") openWorld(w.id);
      else if (act === "del") {
        if (confirm(`Удалить мир «${w.name}» и всю его историю?`)) {
          API(`/api/worlds/${w.id}`, { method: "DELETE" }).then(loadWorlds);
        }
      }
    };
    list.appendChild(card);
  });
  // «Показать ещё» — если миров больше, чем показано
  if (state.worlds.length > state.worldsShown) {
    const more = document.createElement("div");
    more.className = "load-more";
    more.innerHTML = `<button class="btn small" id="btn-worlds-more">Показать ещё (${state.worlds.length - state.worldsShown})</button>`;
    more.querySelector("#btn-worlds-more").onclick = () => { state.worldsShown += WORLD_PAGE; renderWorlds(); };
    list.appendChild(more);
  }
}

async function loadNarrators() {
  state.narrators = await API("/api/narrators");
  const sel = $("new-narrator");
  if (!sel) return;
  sel.innerHTML = state.narrators.map((n) =>
    `<option value="${n.id}" title="${esc(n.desc || "")}" ${n.is_preset && n.id === 1 ? "selected" : ""}>${esc(n.name)}${n.is_preset ? " ⭐" : ""}</option>`
  ).join("");
  const prev = $("new-narrator-preview");
  if (prev) {
    const show = () => {
      const n = state.narrators.find((x) => x.id == sel.value);
      prev.textContent = n ? (n.desc || "Описание не задано.") : "";
    };
    sel.addEventListener("change", show);
    show();
  }
}

async function createWorld() {
  const b = $("btn-create-world");
  b.disabled = true;
  $("create-state").textContent = "🌱 Рассказчик открывает мир… это несколько секунд";
  try {
    const res = await API("/api/worlds", {
      method: "POST",
      body: JSON.stringify({
        theme_id: state.selectedTheme,
        difficulty: $("new-difficulty").value,
        perspective: $("new-perspective").value,
        language: $("new-language").value,
        name: $("new-name").value.trim(),
        custom_hook: $("new-hook").value.trim(),
        narrator_id: $("new-narrator").value || null,
        genres: state.selectedGenres && state.selectedGenres.length ? state.selectedGenres : null,
        custom_plot: state.selectedTheme === "custom" && !state.selectedPlotId
          ? $("custom-plot-text").value.trim() : null,
        plot_id: state.selectedTheme === "custom" && state.selectedPlotId ? state.selectedPlotId : null,
        custom_lore: state.selectedTheme === "custom" && !state.selectedPlotId
          ? $("custom-lore-text").value.trim() : null,
      }),
    });
    // Быстрые действия от ИИ уже сгенерированы для вступительной сцены сервером
    if (Array.isArray(res.suggestions) && res.suggestions.length) {
      state.suggestions = res.suggestions.filter(Boolean);
      try { localStorage.setItem(`textgame.suggestions.${res.world_id}`, JSON.stringify(state.suggestions)); } catch (_) {}
    }
    await loadWorlds();
    openWorld(res.world_id);
  } catch (e) {
    $("create-state").textContent = "Ошибка: " + e.message;
    b.disabled = false;
  }
}

/* ─────────────── Игровой экран ─────────────── */
async function openWorld(id) {
  state.currentWorld = id;
  $("screen-start").style.display = "none";
  $("screen-game").style.display = "flex";
  // Кнопка админки — только в меню, в игре не нужна
  const tb = document.querySelector(".topbar"); if (tb) tb.style.display = "none";
  const d = await API(`/api/worlds/${id}`);
  state.setting = d.setting || null;
  state.gen = d.gen_settings || {};
  state.providersEffective = d.providers_effective || null;
  // E5 (аудит 38): поле provider_settings в state больше не хранится — оно записывалось
  // из двух мест и НИКОГДА не читалось, а носителем в нём лежали per-world API-ключи
  // (см. A3). Форма настроек заполняется из providers_effective (замаскированного).
  state.rerankGlobal = !!d.rerank_enabled;
  state.currentNarrator = d.world.narrator_id != null ? +d.world.narrator_id : (state.narrators[0]?.id ?? null);
  state.tts = d.tts || null;
  state.ttsSettingsRaw = d.tts_settings_raw || {};
  state.memoryDefaults = d.memory_defaults || {};
  // Быстрые действия от ИИ не теряются при перезагрузке страницы (восстанавливаем ДО renderSetting)
  try {
    const saved = localStorage.getItem(`textgame.suggestions.${id}`);
    state.suggestions = saved ? JSON.parse(saved) : [];
  } catch (_) { state.suggestions = []; }
  // свежие варианты от ИИ при открытии — не оставляем устаревшие из localStorage навсегда
  loadSuggestionRefresh(id);
  loadTtsStatus().then(renderTtsUI);
  const w = d.world;
  $("world-name").textContent = w.name;
  $("world-name").title = w.name;   // полное имя видно ховером (сайдбар обрезает длинные)
  $("btn-menu").textContent = "← Меню";
  const tname = (state.themes.find((t) => t.id === w.theme) || {}).name || (w.theme === "custom" ? "Свой сюжет" : `Сюжет «${w.theme}» (удалён/правлен)`);
  $("world-meta").textContent = `${tname} · ${w.genre} · сложность ${DIFF_LABELS[w.difficulty] || w.difficulty} · язык ${w.language}`;
  renderSetting(state.setting);
  syncGenUI();
  $("log").innerHTML = "";
  logPagerBtn = null;               // кнопку пагинации создадим заново
  state.logMinSeq = 0;
  // A10: сервер уже отдал лог отфильтрованным по CHAT_LOG_ROLES, но предикат на фронте
  // один (shouldRenderEvent) — он же отсекает folded и служебные роли, если мир вернулся
  // с другим набором ролей (старый кеш ответа).
  const recent = (d.recent || []).filter(shouldRenderEvent);
  recent.forEach((e) => appendMsg(e, false));
  if (!recent.length) {
    appendMsg({ role: "system", content: "Мир создан, но пуст. Начни с первого действия." }, false);
  }
  // запоминаем самый ранний загруженный seq — для подгрузки более ранних событий
  const firstMsg = $("log").querySelector(".msg");
  if (firstMsg) state.logMinSeq = +(firstMsg.dataset.seq || 0);
  showLogPager(true);                // «⬆ Показать ранние события» (скроется, когда дальше пусто)
  // Варианты действий (включая ИИ-предложения к вступительной сцене) — гарантированно рисуем
  // при открытии мира, даже если renderSetting где-то споткнулся выше.
  try { renderSuggestionBar(); } catch (_) {}
  state.seenSeq = 0;
  (d.recent || []).forEach((e) => { if (e.seq) state.seenSeq = Math.max(state.seenSeq, +e.seq); });
  // Сразу листаем лог в конец (к последнему ответу рассказчика)
  requestAnimationFrame(() => { $("log").scrollTop = $("log").scrollHeight; });
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = setInterval(pollEvents, 15000);
  startLiveBus();          // D3 (сессия 34): SSE-лента мира; поллинг остаётся запасным
  loadEntities();
  loadLore();
  loadSaves();
  loadNarrators().then(syncNarratorUI);
  loadProviderOptions().then(syncProvidersUI);
  $("cmd").focus();
}

function goMenu() {
  state.currentWorld = null;
  stopLiveBus();
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
  $("screen-game").style.display = "none";
  $("screen-start").style.display = "";
  const tb = document.querySelector(".topbar"); if (tb) tb.style.display = "";
  loadWorlds();
  refreshStatus();
  window.scrollTo(0, 0);
}

/* ─────────────── Живой чат: подтягиваем фоновые события мира ─────────────── */
/* D3 (сессия 34): SSE-лента мира (bus.py + GET /events/stream). Поверх поллинга:
   если EventSource упал/не поддерживается — поллинг pollEvents остаётся запасным. */
function startLiveBus() {
  stopLiveBus();
  if (!state.currentWorld) return;
  try {
    const es = new EventSource(`/api/worlds/${state.currentWorld}/events/stream?after=${state.seenSeq || 0}`);
    state.eventSource = es;
    es.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        if (!data || data.type !== "event" || !data.event) return;
        const e = data.event;
        if (e.seq > (state.seenSeq || 0)) state.seenSeq = +e.seq;
        // A10/E3: единый предикат + дедюп по id события (см. appendMsg)
        if (!shouldRenderLive(e)) return;
        if (document.querySelector(`.msg[data-id="${CSS.escape(String(e.id))}"]`)) return;
        appendMsg(e, true);
      } catch (_) { /* битое сообщение шины — игнор, поллинг догонит */ }
    };
    es.onerror = () => {
      // EventSource сам переподключается; при недоступности — поллинг уже работает
    };
  } catch (_) { state.eventSource = null; }
}

function stopLiveBus() {
  if (state.eventSource) { try { state.eventSource.close(); } catch (_) {} state.eventSource = null; }
}

/* E4 (аудит 38): жива ли SSE-лента — по ней решаем, нужен ли запасной поллинг.
 * readyState: 0 = connecting, 1 = open, 2 = closed. При connecting тоже молчим:
 * EventSource переподключается сам, а долбить API в этот момент смысла нет. */
function liveBusOpen() {
  const es = state.eventSource;
  return !!es && es.readyState !== 2;
}

/* ─────────────── C4: часы мира + компас соседних локаций ─────────────── */
function renderWorldClock(s) {
  const el = $("world-clock");
  if (!el) return;
  const parts = [];
  if (s.time) parts.push(`🕐 ${s.time}`);
  if (s.weather) parts.push(`☁ ${s.weather}`);
  if (s.date && s.date.season) parts.push(`🍂 ${s.date.season}`);
  el.textContent = parts.join(" · ");
  el.title = parts.join(", ") || "время и погода мира";
}

function renderCompass(s) {
  const el = $("compass-bar");
  if (!el) return;
  const locs = s.locations || {};
  const cur = s.current_location;
  const here = cur && locs[cur];
  const conns = (here && Array.isArray(here.connections)) ? here.connections : [];
  // подстраховка: если рёбер в состоянии нет — возьмём из кэша карты (граф) при его наличии
  let items = conns.map((id) => ({ id, name: (locs[id] && locs[id].name) || id }))
    .filter((x) => x.id !== cur);
  if (!items.length && state.mapGraph && state.mapGraph.edges) {
    items = state.mapGraph.edges
      .filter((e) => e[0] === cur || e[1] === cur)
      .map((e) => { const id = e[0] === cur ? e[1] : e[0]; return { id, name: (locs[id] && locs[id].name) || id }; });
  }
  if (!items.length) { el.style.display = "none"; return; }
  el.style.display = "";
  el.innerHTML = `<span class="compass-label">🧭 рядом:</span> ` + items.slice(0, 6).map((it) =>
    `<button class="btn small compass-btn" data-loc="${esc(it.id)}" title="Идти в ${esc(it.name)}">→ ${esc(it.name)}</button>`
  ).join("");
  el.querySelectorAll(".compass-btn").forEach((b) => {
    b.onclick = () => sendAsAction(`Идти в ${b.dataset.loc}`);
  });
}

/* Отправить текст как действие игрока (общий путь для компас/кнопок). */
async function sendAsAction(text) {
  if (!text || !text.trim() || state.streaming) return;
  const input = $("cmd");
  if (input) input.value = "";
  appendMsg({ role: "player", content: text.trim(), seq: "" });
  await streamTurn(text.trim(), false);
}

/* ─────────────── C2: дневник приключений (вкладка) ─────────────── */
async function loadJournal() {
  if (!state.currentWorld) return;
  const list = $("journal-list");
  if (!list) return;
  try {
    const cat = ($("jr-cat") && $("jr-cat").value) || "";
    const d = await API(`/api/worlds/${state.currentWorld}/journal?limit=150&cat=${encodeURIComponent(cat)}`);
    const sel = $("jr-cat");
    if (sel && sel.options.length <= 1 && d.categories && d.categories.length) {
      sel.innerHTML = `<option value="">все записи</option>` +
        d.categories.map((c) => `<option value="${esc(c.cat)}">${esc(c.icon)} ${esc(c.cat)} (${c.count})</option>`).join("");
    }
    // «на горизонте» (ружья Чехова, C9)
    const chk = $("jr-chekhov");
    const guns = (state.setting && state.setting._chekhov) || [];
    if (chk && guns.length) {
      chk.style.display = "";
      chk.innerHTML = `🏹 На горизонте: ` + guns.map((g) => esc(g.subject || "")).join(" · ") +
        ` <small>(верни в сюжет, когда уместно)</small>`;
    } else if (chk) { chk.style.display = "none"; chk.innerHTML = ""; }
    list.innerHTML = d.entries.length ? d.entries.map((e) =>
      `<li class="item-clickable" data-click="scrollToSeq" data-arg="${numAttr(e.seq)}" title="к ходу ${e.seq}">` +
      `<small class="right">ход ${e.seq}</small>${esc(e.icon)} <b>${esc(e.title)}</b>` +
      (e.text ? `<small>${esc(e.text)}</small>` : ``) + `</li>`
    ).join("") : `<li class="muted">Пока пусто — дневник заполняется значимыми событиями (встречи, квесты, находки, смена роли).</li>`;
  } catch (_) {
    list.innerHTML = `<li class="muted">Дневник недоступен.</li>`;
  }
}

function scrollToSeq(seq) {
  const msg = document.querySelector(`.msg[data-seq="${seq}"]`);
  if (msg) { msg.scrollIntoView({ block: "center" }); msg.classList.add("flash-seq"); return; }
  // если события ещё не загружены (пагинация) — открываем историю
  alert(`Событие хода ${seq} не в загруженной части лога. Поднимись в начало и нажми «⬆ Показать ранние события».`);
}

/* ─────────────── C1: перемотка к ходу ─────────────── */
async function openRewindModal() {
  if (!state.currentWorld) return;
  let pts;
  try { pts = (await API(`/api/worlds/${state.currentWorld}/rewind/points`)).points || []; }
  catch (_) { alert("Точки перемотки недоступны."); return; }
  if (!pts.length) { alert("Точек перемотки пока нет — дойди до пары ходов, и появятся."); return; }
  const body = `<p class="muted">Перемотка возвращает мир к началу выбранного хода: состояние, память, дневник. Ходы после точки будут удалены (сохрани заранее, если дорого).</p>` +
    `<ul class="list rewind-list">` + pts.map((p) =>
      `<li><button class="btn small rewind-btn" data-seq="${p.seq}">⏪ к ходу ${p.seq}</button>` +
      ` <small class="muted">${esc(p.preview || "—")}</small></li>`
    ).join("") + `</ul>`;
  openModal("⏪ Вернуть мир к ходу", body, async () => {
    const btn = document.querySelector(".rewind-btn:focus");
    if (!btn) { alert("Выбери ход из списка."); throw new Error("no-seq"); }
    const seq = btn.dataset.seq;
    if (!confirm(`Точно вернуть мир к ходу ${seq}? Ходы после него будут удалены.`)) throw new Error("cancel");
    const res = await API(`/api/worlds/${state.currentWorld}/rewind`, {
      method: "POST", body: JSON.stringify({ seq: +seq, mode: "delete" }),
    });
    await openWorld(state.currentWorld);   // перезагрузка лога/состояния/памяти
  });
}

/* ─────────────── C7: /risk (мои средства) ─────────────── */
async function openRiskModal() {
  if (!state.currentWorld) return;
  const idea = ($("cmd") && $("cmd").value.trim()) || "";
  let reply = "";
  try {
    const d = await API(`/api/worlds/${state.currentWorld}/risk?idea=${encodeURIComponent(idea)}`);
    reply = d.reply || "";
  } catch (_) { reply = "Справка /risk недоступна."; }
  openModal("🧭 Мои средства", `<pre class="risk-pre">${esc(reply)}</pre>`,
    async () => { $("cmd").focus(); });
}

async function pollEvents() {
  if (!state.currentWorld || state.streaming) return;
  // E4 (аудит 38): поллинг — ЗАПАСНОЙ путь. Пока живая SSE-лента открыта, не долбить API
  // двумя тяжёлыми запросами каждые 15 с (выборка событий + полный detail мира). Но и
  // полную сверку состояния совсем убирать нельзя: фоновые агенты (архивариус карточек,
  // синхронизация квестов) правят setting БЕЗ новых сообщений, и сайдбар «запаздывал» бы
  // до перезагрузки. Поэтому при живой шине оставляем редкую (раз в 4 тика ≈ 60 с)
  // сверку только СОСТОЯНИЯ, а выборку событий пропускаем.
  if (liveBusOpen()) {
    if ((++_pollTick % 4) !== 0) return;
    await syncStateFromServer();
    return;
  }
  try {
    // A14 (аудит 38): сервер отдаёт объект {events, truncated} и страницу с потолком.
    // truncated = догон не влез целиком → пересоединяемся, а не молча теряем сообщения.
    const body = await API(`/api/worlds/${state.currentWorld}/events?since=${state.seenSeq || 0}`);
    const evs = (body && body.events) || [];
    let maxSeq = state.seenSeq || 0;
    evs.forEach((e) => { if (+e.seq > maxSeq) maxSeq = +e.seq; });
    if (maxSeq > (state.seenSeq || 0)) state.seenSeq = maxSeq;
    // A10: единый предикат; E3: дедюп по id события
    const fresh = evs.filter((e) => shouldRenderLive(e)
      && !document.querySelector(`.msg[data-id="${CSS.escape(String(e.id))}"]`));
    fresh.forEach((e) => appendMsg(e, true));
    if (body && body.truncated) {
      // часть событий осталась за страницей — просим серверную ленту пересоединиться
      startLiveBus();
    }
    const changed = await syncStateFromServer();
    // только что добавленные свежие сообщения тоже могли сдвинуть чат
    if (fresh.length && changed) requestAnimationFrame(() => { $("log").scrollTop = $("log").scrollHeight; });
  } catch (_) { /* тихий опрос */ }
}

/* Сверка СОСТОЯНИЯ мира с сервером (фон мог править setting без новых сообщений). */
let _pollTick = 0;
async function syncStateFromServer() {
  const d = await API(`/api/worlds/${state.currentWorld}`);
  const next = d.setting || null;
  const changed = JSON.stringify(state.setting || null) !== JSON.stringify(next);
  if (next) state.setting = next;
  if (changed) {
    renderSetting(state.setting);
    loadEntities();
    loadSaves();
  }
  return changed;
}

/* ─────────────── Рассказчик (настройки) ─────────────── */
function syncNarratorUI() {
  const sel = $("set-narrator");
  if (!sel || !state.narrators.length) return;
  const cur = (state.currentNarrator == null && state.narrators.length) ? 1 : state.currentNarrator;
  sel.innerHTML = state.narrators.map((n) =>
    `<option value="${n.id}">${esc(n.name)}${n.is_preset ? " ⭐" : ""}</option>`
  ).join("");
  sel.value = cur && state.narrators.some((n) => n.id == cur) ? cur : state.narrators[0].id;
  $(`set-narrator`).onchange = async () => {
    const id = +sel.value;
    try {
      await API(`/api/worlds/${state.currentWorld}/settings`, {
        method: "POST", body: JSON.stringify({ narrator_id: id }),
      });
      const nr = state.narrators.find((n) => n.id === id);
      $(`narrator-preview`).textContent = nr ? (nr.desc || "") : "";
    } catch (e) { alert("Ошибка смены рассказчика: " + e.message); }
  };
  const nr = state.narrators.find((n) => n.id == sel.value);
  $(`narrator-preview`).textContent = nr ? (nr.desc || "") : "";
}

async function narratorsModal() {
  const list = state.narrators || [];
  openModal("📖 Рассказчики", `
    <div class="nr-list">
      ${list.map((n) => `<div class="entity-card">
        <div class="e-head"><span class="e-name">${esc(n.name)}${n.is_preset ? " ⭐" : ""}</span>
          <span class="e-kind">#${n.id}</span></div>
        ${n.desc ? `<div class="e-sum">${esc(n.desc)}</div>` : ""}
        <div class="e-actions">
          <button class="btn small" data-act="edit" data-id="${n.id}">✎</button>
          <button class="btn small danger" data-act="del" data-id="${n.id}">🗑</button>
        </div>
      </div>`).join("") || `<p class="muted">Рассказчиков пока нет.</p>`}
    </div>
    <hr>
    <h3>Новый рассказчик</h3>
    <label>Имя <input id="nr-name" placeholder="Мой ведущий"></label>
    <label>Описание (для списка) <input id="nr-desc" placeholder="Коротко о стиле"></label>
    <label>Промпт (персона — как вести игру)
      <textarea id="nr-prompt" rows="5"
        placeholder="Ты — седой бард… (опиши характер и стиль повествования)"></textarea></label>
    <div class="toolbar">
      <button id="btn-nr-save" class="btn small primary">💾 Сохранить</button>
      <button id="btn-nr-clear" class="btn small">Сбросить / новый</button>
    </div>`, async () => {
    const name = $(`nr-name`).value.trim();
    const prompt = $(`nr-prompt`).value.trim();
    if (!name || !prompt) { alert("Имя и промпт обязательны"); throw new Error("empty"); }
    const saveBtn = $(`btn-nr-save`);
    const editId = saveBtn.dataset.editId || null;
    const path = editId ? `/api/narrators/${editId}` : `/api/narrators`;
    await API(path, { method: editId ? "PATCH" : "POST", body: JSON.stringify({
      name, prompt, desc: $(`nr-desc`).value.trim() }) });
    state.narrators = await API("/api/narrators");
    syncNarratorUI();
  });
  document.querySelectorAll(".nr-list [data-act]").forEach((b) => {
    b.onclick = async () => {
      const id = +b.dataset.id;
      if (b.dataset.act === "del") {
        if (!confirm("Удалить рассказчика?")) return;
        const r = await API(`/api/narrators/${id}`, { method: "DELETE" }).catch((e) => error(e));
        if (r !== undefined) { state.narrators = await API("/api/narrators"); narratorsModal(); }
      } else {
        const n = state.narrators.find((x) => x.id === id);
        $(`nr-name`).value = n.name; $(`nr-desc`).value = n.desc || ""; $(`nr-prompt`).value = n.prompt;
        const save = $(`btn-nr-save`); save.textContent = "💾 Сохранить изменения";
        save.dataset.editId = n.id;
        document.querySelectorAll(".nr-list [data-act='edit']").forEach((x) => x.classList.remove("liked"));
        b.classList.add("liked");
      }
    };
  });
  $(`btn-nr-clear`).onclick = () => {
    $(`nr-name`).value = ""; $(`nr-desc`).value = ""; $(`nr-prompt`).value = "";
    const save = $(`btn-nr-save`); save.textContent = "💾 Сохранить"; delete save.dataset.editId;
  };
}

function error(e) { alert((e && e.message) || String(e)); }

/* ─────────────── Провайдеры (настройки) ─────────────── */
async function loadProviderOptions() {
  state.providers = await API("/api/providers");
}

const PROVIDER_KINDS = ["main", "embedding", "rerank"];

function syncProvidersUI() {
  if (!state.providers || !state.providersEffective) return;
  PROVIDER_KINDS.forEach((kind) => {
    const sel = $(`set-prov-${kind}`);
    const eff = state.providersEffective[kind] || {};
    sel.innerHTML = state.providers[kind].map((o) =>
      `<option value="${o.id}">${esc(o.name)}${o.id === "none" ? " (выкл.)" : ""}</option>`
    ).join("");
    sel.value = eff.id || state.providers[kind][0].id;
    sel.onchange = () => renderProviderFields(kind);
    renderProviderFields(kind);
  });
  const rerankEff = state.providersEffective.rerank || {};
  $(`set-rerank-enabled`).checked = !!(state.rerankGlobal && rerankEff.enabled !== false && rerankEff.id !== "none");
  $(`set-rerank-enabled`).disabled = !state.rerankGlobal;
  if (!state.rerankGlobal) $(`set-rerank-enabled`).title = "RERANK_ENABLED=false в .env — глобальный выключатель";
}

function renderProviderFields(kind) {
  const box = $(`prov-fields-${kind}`);
  const sel = $(`set-prov-${kind}`);
  const opt = (state.providers || {})[kind].find((o) => o.id === sel.value);
  const eff = state.providersEffective[kind] || {};
  const needsKey = opt?.needs_key;
  const needsModel = opt?.needs_model;
  box.innerHTML = `
    <label>Base URL
      <input data-pf="base_url" placeholder="https://.../v1" value="${esc(eff.base_url || "")}"></label>
    ${needsKey ? `<label>API Key
      <input data-pf="api_key" type="password" placeholder="${eff.api_key ? "оставь пустым — ключ уже задан" : "sk-..."}" value="">
      ${eff.api_key ? `<p class="muted">Ключ задан (скрыт). Пустое поле = не менять.</p>` : `<p class="muted">Ключ не задан.</p>`}</label>`
    : `<input data-pf="api_key" type="hidden" value="">`}
    ${needsModel ? `<label>Модель
      <input data-pf="model" placeholder="имя модели" value="${esc(eff.model || "")}"></label>`
    : `<input data-pf="model" type="hidden" value="">`}
  `;
}

function collectProviderPayload(kind) {
  const sel = $(`set-prov-${kind}`);
  const data = { id: sel.value };
  const box = $(`prov-fields-${kind}`);
  box.querySelectorAll("[data-pf]").forEach((inp) => {
    const v = inp.value.trim();
    if (inp.dataset.pf === "api_key") {
      if (v && v !== "••••••••" && v !== inp.dataset.masked) data.api_key = v;
    } else if (v) {
      data[inp.dataset.pf] = v;
    }
  });
  return data;
}

function renderSetting(s) {
  if (!s) return;
  const p = s.player || {};
  $("hp-bar").style.width = p.max_hp ? (p.hp / p.max_hp * 100) + "%" : "0%";
  $("mp-bar").style.width = p.max_mp ? (p.mp / p.max_mp * 100) + "%" : "0%";
  $("hp-num").textContent = `${p.hp}/${p.max_hp}`;
  $("mp-num").textContent = `${p.mp}/${p.max_mp}`;
  // Персонаж «кто я» — вынесен в отдельный блок ниже, здесь только сводка
  const playerRows = [];
  playerRows.push(["Уровень", p.level], ["Опыт", p.xp], ["Золото", p.gold],
    ["Время", s.time], ["Погода", s.weather]);
  // 🍖 Потребности и 🧠 рассудок (сессия 32)
  const needs = Object.entries(p.needs || {});
  const mental = Object.entries(p.mental || {});
  const crit = (v, mx) => Number(v) <= 25 ? " ⚠" : "";
  if (needs.length) playerRows.push(["🍖 Потребности", needs.map(([k, v]) => `${esc(ucfirst(k))} ${Math.round(Number(v && v.value) || 0)}${crit(v && v.value, v && v.max)}`).join(" · ")]);
  if (mental.length) playerRows.push(["🧠 Рассудок", mental.map(([k, v]) => `${esc(ucfirst(k))} ${Math.round(Number(v && v.value) || 0)}${crit(v && v.value, v && v.max)}`).join(" · ")]);
  // вместимость рюкзака (вес предметов × qty), если есть предметы с весом
  const carryUsed = (p.inventory || []).reduce((a, i) => a + (Number(i.weight) || 0) * (i.qty || 1), 0);
  if (carryUsed > 0) {
    const stats = p.stats || {};
    const carryCap = 20 + (Number(stats["сила"]) || 10) * 2 + (Number(stats["выносливость"]) || 10) * 2;
    const loadCls = carryUsed > carryCap ? ' title="Рюкзак перегружен!"' : "";
    playerRows.push(["🎒 Загрузка", `${carryUsed}/${carryCap} кг` + (carryUsed > carryCap ? " ⚠" : "")]);
  }
  $("player-kv").innerHTML = kv(playerRows);
  // Блок «Персонаж»: имя + полная биография + раса/класс/профессия/навыки как явные параметры с описаниями
  (function renderCharacter() {
    const bio = (p.identity || "").trim();
    const H = [];
    const raceDesc = CHARACTER_RACE_DESC[(p.race || "").toLowerCase()];
    if (p.race) {
      const racePas = Object.entries(p.effects || {}).filter(([n, ef]) => ef && ef.tag === "race")
        .map(([n, ef]) => `${esc(ucfirst(n))} — ${esc(ef.desc || "")}`).join("; ");
      H.push(charRow("🧝 Раса", `${esc(ucfirst(p.race))}${raceDesc ? ` <span class="muted">— ${raceDesc}</span>` : ""}` + (racePas ? `<br><small class="char-desc">${racePas}</small>` : "")));
    } else {
      H.push(charRow("🧝 Раса", `<span class="muted">не назначена</span>`));
    }
    if (p.class) {
      const clsDesc = CHARACTER_CLASS_DESC[(p.class || "").toLowerCase()];
      H.push(charRow("🎭 Класс", `${esc(ucfirst(p.class))} (ранг ${esc(p.class_rank || "F")})` + (clsDesc ? ` <span class="muted">— ${clsDesc}</span>` : "")));
      if (p.secondary_class) H.push(charRow("🔄 Мультикласс", `${esc(ucfirst(p.secondary_class))} (ранг ${esc(p.secondary_rank || "F")})`));
    } else {
      H.push(charRow("🎭 Класс", `<span class="muted">не назначен</span>`));
    }
    if (p.profession) {
      const profDesc = CHARACTER_PROF_DESC[(p.profession || "").toLowerCase()];
      H.push(charRow("⚒ Профессия", `${esc(ucfirst(p.profession))}` + (profDesc ? ` <span class="muted">— ${profDesc}</span>` : "")));
    } else {
      H.push(charRow("⚒ Профессия", `<span class="muted">не назначена</span>`));
    }
    const titles = p.titles || [];
    if (titles.length) H.push(charRow("🏅 Титулы", titles.map((t) => esc(ucfirst(t))).join(", ")));
    // 🏅 Звания во фракциях (должность — не репутация)
    const fraRanks = Object.entries(p.faction_ranks || {});
    if (fraRanks.length) H.push(charRow("🎖 Звания", fraRanks.map(([fid, r]) => `${esc(ucfirst(factionName(fid)))}: ${esc(r)}`).join(", ")));
    const rep = Object.entries(p.reputation || {});
    if (rep.length) H.push(charRow("🤝 Репутация", rep.map(([k, v]) => `${esc(ucfirst(k))} — ${esc(repStanding(v))} (${v >= 0 ? "+" : ""}${v})`).join(", ")));
    // Фракции и их связи (relations: союз/враг), заведённые рассказчиком директивами faction_add
    const factions = Object.entries(s.factions || {});
    if (factions.length) {
      const fHtml = factions.map(([id, f], fi) => {
        const n = f.name || id;
        const st = repStanding((p.reputation || {})[id] || 0);
        const rel = Object.entries(f.relations || {}).map(([oid, st2]) => {
          const oname = (s.factions || {})[oid]?.name || oid;
          return `${esc(oname)}: ${esc(st2)}`;
        });
        const relTxt = rel.length ? ` <span class="frac-rel">связи: ${rel.join(", ")}</span>` : "";
        return `<li class="item-clickable" data-click="showFaction" data-arg="${numAttr(fi)}" title="${esc(f.desc || "")}">🏴 ${esc(n)} <span class="muted">— ${esc(st)}</span>${relTxt}</li>`;
      }).join("");
      H.push(`<div class="char-row"><span class="char-label">🏴 Фракции</span><ul class="list">${fHtml}</ul></div>`);
    }
    const acts = Object.entries(p.actions || {});
    if (acts.length) H.push(charRow("📈 Действия", acts.map(([a, c]) => actionNameWithProg(a, c, p.profession)).join(", ")));
    const skills = Object.entries(p.skills || {});
    if (skills.length) {
      const skHtml = skills.map(([n, sk], si) => {
        if (sk && typeof sk === "object") {
          const d = sk.desc ? trunc(sk.desc, 100) : "";
          return `<li class="item-clickable" data-click="showSkill" data-arg="${numAttr(si)}" title="${esc(sk.desc || "")}">${esc(ucfirst(n))} (ранг ${esc(sk.rank || "F")}${sk.kind ? ", " + esc(ucfirst(sk.kind)) : ""})${d ? `<small>${esc(d)}</small>` : ""}</li>`;
        }
        return `<li class="item-clickable" data-click="showSkill" data-arg="${numAttr(si)}">${esc(ucfirst(n))} ур.${esc(sk)}</li>`;
      }).join("");
      H.push(`<div class="char-row"><span class="char-label">🎖 Навыки / Способности</span><ul class="list">${skHtml}</ul></div>`);
    } else {
      H.push(`<div class="char-row"><span class="char-label">🎖 Навыки / Способности</span><span class="muted">нет навыков</span></div>`);
    }
    // Универсальные сверхспособности (магия/техника/псионика — в духе жанра мира)
    const abil = Object.entries(p.abilities || {});
    if (abil.length) {
      const abHtml = abil.map(([n, ab], ai) => {
        const school = ab.school ? ` [${esc(ab.school)}]` : "";
        const cost = ab.cost ? ` <small>энергия ${ab.cost}</small>` : "";
        const d = ab.desc ? ` — ${trunc(ab.desc, 100)}` : "";
        return `<li class="item-clickable" data-click="showAbility" data-arg="${numAttr(ai)}" title="${esc(ab.desc || "")}">${esc(ucfirst(n))}${school}${cost}<small>${d}</small></li>`;
      }).join("");
      H.push(`<div class="char-row"><span class="char-label">⚡ Способности</span><ul class="list">${abHtml}</ul></div>`);
    }
    // Статистика пути и достижения
    const progress = Object.entries(p.progress || {});
    const achievements = (p.achievements || []);
    if (progress.length) {
      H.push(`<div class="char-row"><span class="char-label">📊 Статистика</span><span>${progress.map(([k, v]) => `${esc(ucfirst(k))} ${v}`).join(" · ")}</span></div>`);
    }
    if (achievements.length) {
      const achHtml = achievements.map((a) => {
        const nm = typeof a === "string" ? a : a.name;
        const d = typeof a === "object" && a ? a.desc : "";
        return `<li>🏆 ${esc(ucfirst(nm))}${d ? ` <small>— ${esc(d)}</small>` : ""}</li>`;
      }).join("");
      H.push(`<div class="char-row"><span class="char-label">🏆 Достижения</span><ul class="list">${achHtml}</ul></div>`);
    }
    // Имя + биография — внизу, после роли (раса/класс/профессия/навыки)
    if (bio) H.push(`<div class="char-name">${esc(p.name || "Путник")}</div><div class="char-bio">${esc(bio).replace(/\n/g, "<br>")}</div>`);
    else if (p.name || true) H.push(`<div class="char-name">${esc(p.name || "Путник")}</div>`);
    if (!H.length) H.push(`<div class="muted">Персонаж ещё не оформлен.</div>`);
    $("character").innerHTML = `<div class="char-card">${H.join("")}</div>`;
  })();
  // Эффективные статы = база + моды активных эффектов
  const baseStats = p.stats || {};
  const statMods = {};
  Object.values(p.effects || {}).forEach((ef) => {
    ef = ef || {};
    Object.entries(ef.mods || {}).forEach(([k, v]) => { statMods[k] = (statMods[k] || 0) + (Number(v) || 0); });
  });
  const effStats = {};
  let anyMod = false;
  Object.entries(baseStats).forEach(([k, v]) => { effStats[k] = Number(v) + (statMods[k] || 0); if (statMods[k]) anyMod = true; });
  // ⚔️ Бонусы от экипировки — тоже моды к статам
  (function equipMods() {
    Object.entries(p.equipped || {}).forEach(([slot, itemName]) => {
      const it = (p.inventory || []).find((x) => x.name === itemName);
      if (it && it.bonus) Object.entries(it.bonus || {}).forEach(([k, v]) => {
        statMods[k] = (statMods[k] || 0) + (Number(v) || 0);
        anyMod = true;
        effStats[k] = Number(baseStats[k] || 0) + statMods[k];
      });
    });
  })();
  $("stats-kv").innerHTML = kv(Object.entries(effStats).map(([k, v]) => [ucfirst(k), statMods[k] ? `${v}<em class="mod">(${statMods[k] > 0 ? "+" : ""}${statMods[k]})</em>` : v]))
    + (anyMod ? `<p class="muted">Характеристики — с учётом модов эффектов.</p>` : ``) || `<div>—</div><b>?</b>`;
  $("effects-list").innerHTML = Object.entries(p.effects || {}).map(([name, ef], ei) => {
    ef = ef || {};
    // id-подобные имена (chill_resonance) → читабельно
    let label = String(name || "");
    if (/^[A-Za-z0-9_\-]+$/.test(label)) label = (label.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()) || label).slice(0, 60);
    const t = (ef.turns === undefined || ef.turns === null || ef.turns === -1) ? "постоянно" : `${ef.turns} ход.`;
    const kind = ef.kind ? `, ${esc(ef.kind)}` : "";
    let tick = "";
    if (ef.damage) tick += ` −${ef.damage} HP/ход`;
    if (ef.heal) tick += ` +${ef.heal} HP/ход`;
    if ((ef.stacks || 1) > 1) tick += ` ×${ef.stacks}`;
    const mods = ef.mods || {};
    if (Object.keys(mods).length) tick += ` (моды: ${Object.entries(mods).map(([k, v]) => `${esc(k)}${v > 0 ? "+" : ""}${v}`).join(", ")})`;
    const desc = ef.desc ? trunc(ef.desc, 120) : "";
    const cls = (ef.damage || 0) > 0 ? " bad" : (ef.heal || 0) > 0 ? " good" : "";
    return `<li class="eff${cls} item-clickable" data-click="showEffect" data-arg="${numAttr(ei)}" title="${esc(ef.desc || "")}">${esc(label)}<small>${desc ? esc(desc) + " " : ""}(${t}${kind}${tick})</small></li>`;
  }).join("") || `<li>нет</li>`;
  $("inventory").innerHTML = (p.inventory || []).map((i, idx) =>
    `<li class="item-clickable" data-click="showItem" data-arg="${numAttr(idx)}" title="${esc(i.desc || "нет описания")}">${esc(i.name)} ×${i.qty || 1}` + (i.desc ? `<small>${trunc(i.desc, 60)}</small>` : ``)
    + (i.value ? ` <em class="val">🪙${i.value}</em>` : ``)
    + (i.weight ? ` <em class="val">${Number(i.weight)}кг</em>` : ``) + `</li>`).join("") || `<li>пусто</li>`;
  const invSell = (p.inventory || []).reduce((a, i) => a + (Number(i.value) || 0) * (i.qty || 1), 0);
  if (invSell > 0) $("inventory-title").innerHTML = "🧰 Инвентарь <small class=\"muted\">(продать ≈ " + invSell + "🪙)</small>";
  // 🛡 Экипировка (сессия 32): что надето по слотам
  const equipped = Object.entries(p.equipped || {});
  if (equipped.length) {
    const eqHtml = equipped.map(([slot, itemName]) => {
      const it = (p.inventory || []).find((x) => x.name === itemName);
      const bn = (it && it.bonus) ? ` <small>${Object.entries(it.bonus).map(([k, v]) => `${esc(ucfirst(k))}${v > 0 ? "+" : ""}${v}`).join(", ")}</small>` : "";
      return `<li>🛡 ${esc(ucfirst(slot))}: ${esc(itemName)}${bn}</li>`;
    }).join("");
    $("inventory-title").innerHTML += ` <small class="muted">| 🛡 ${equipped.length} слота</small>`;
    $("equipped-list").innerHTML = eqHtml;
  } else {
    $("equipped-list").innerHTML = `<li class="muted">ничего не надето</li>`;
  }
  const loc = (s.locations || {})[s.current_location] || {};
  // Локация в стиле «биографии»: название крупным + описание блоками (как char-card)
  const locName = loc.name || "?";
  const locDesc = (loc.desc || "").trim();
  const locConn = (loc.connections || []).map((c) => {
    const l = (s.locations || {})[c];
    return l ? l.name || c : c;
  });
  const locStations = (loc.stations || []);
  $("location").innerHTML = `<div class="char-card loc-card">`
    + `<div class="loc-name">📍 ${esc(locName)}</div>`
    + (locDesc ? `<div class="loc-desc">${esc(locDesc).replace(/\n/g, "<br>")}</div>` : `<div class="muted">Описание отсутствует.</div>`)
    + (locConn.length ? `<div class="loc-links"><span class="muted">Рядом:</span> ${locConn.map((c) => esc(c)).join(", ")}</div>` : "")
    + (locStations.length ? `<div class="loc-links"><span class="muted">🔧 Станции:</span> ${locStations.map((x) => esc(x)).join(", ")}</div>` : "")
    + (loc.effects && loc.effects.length ? `<div class="loc-links zone"><span class="muted">🌫 Зоны:</span> ${loc.effects.map((fx) => esc(fx.name || "") + (fx.damage ? ` −${fx.damage} HP/ход` : "")).join(", ")}</div>` : "")
    + `</div>`;
  // ⏳ Таймеры мира и 📜 доска (сессия 32)
  const timers = Object.entries(s.timers || {});
  if (timers.length) {
    $("timers-list").innerHTML = timers.map(([name, t]) => {
      const tl = t && t.turns_left;
      const dur = (tl === undefined || tl === null || tl === -1 || tl === "∞") ? "без срока" : `${tl} ход.`;
      return `<li>⏳ ${esc(name)} <small>(${dur})</small>${t && t.desc ? ` <small class="desc">— ${esc(t.desc)}</small>` : ""}</li>`;
    }).join("") || `<li>нет</li>`;
  } else {
    $("timers-list").innerHTML = `<li class="muted">нет</li>`;
  }
  const board = (s.board || []);
  if (board.length) {
    $("board-list").innerHTML = board.slice(-8).map((b) => `<li>📜 <b>${esc(b.title || "")}</b>${b.text ? ` — ${esc(trunc(b.text, 140))}` : ""}</li>`).join("");
  } else {
    $("board-list").innerHTML = `<li class="muted">пусто</li>`;
  }
  // 🌙 Очередь видений (сыграют при отдыхе/сне — рассказчик разыграет)
  const visions = (s.pending_visions || []);
  if (Array.isArray(visions) && visions.length) {
    $("visions-list").innerHTML = visions.slice(-5).map((v) => {
      const hint = (v && v.hint) || (v && v.text) || "";
      return `<li>🌙 ${esc(trunc(String(hint), 100))}</li>`;
    }).join("");
  } else {
    $("visions-list").innerHTML = `<li class="muted">нет</li>`;
  }
  $("quests").innerHTML = Object.entries(s.quests || {}).map(([k, q]) => {
    let tag = `${esc(q.title)}`;
    if (q.progress) tag += ` — <b>${esc(q.progress)}</b>`;
    if (q.chosen) tag += ` <small title="ветка">⎇ ${esc(q.chosen)}</small>`;
    return `<li class="item-clickable ${q.status === "done" ? "done" : ""}" data-click="showQuest" data-arg="${attrArg(k)}">${q.status === "done" ? "✔ " : ""}${tag}${q.desc ? ` — ${trunc(q.desc, 90)}` : ""}</li>`;
  }).join("") || `<li>нет</li>`;
  $("shops-list").innerHTML = Object.entries(s.shops || {}).map(([k, sh]) => {
    const items = (sh.items || []).length;
    return `<li class="item-clickable" data-click="showShop" data-arg="${attrArg(k)}">${sh.desc ? `<small class="right">${trunc(sh.desc, 80)}</small>` : ``}<b>${esc(sh.name || k)}</b>${sh.owner ? ` · ${esc(sh.owner)}` : ""}${sh.faction ? ` <small>${esc(factionName(sh.faction))}</small>` : ``}<small>товаров: ${items}</small></li>`;
  }).join("") || `<li>нет</li>`;
  // крафт: помечаем «гот needs» и вовсе недоступные (нет станции/профессии/ингредиентов)
  $("crafts-list").innerHTML = Object.entries(s.crafts || {}).map(([k, c]) => {
    const res = c.result || {};
    const ing = (c.ingredients || []).map(i => `${esc(i.name)} ×${i.qty}`).join(", ");
    let req = "";
    if (c.station) req += ` <small>🔧 ${esc(Array.isArray(c.station) ? c.station.join(", ") : c.station)}</small>`;
    if (c.profession) req += ` <small>⚒ ${esc(c.profession)}</small>`;
    const ready = craftReady(s, c);
    const icon = ready ? `<em class="val">✓</em>` : `<em class="val bad">✗</em>`;
    return `<li class="item-clickable" data-click="showCraft" data-arg="${attrArg(k)}">${c.desc ? `<small class="desc">${trunc(c.desc, 80)}</small>` : ``}<b>${esc(c.name || k)}</b> → ${esc(res.name || "?")} ×${res.qty || 1} ${icon}${req}<small>${esc(ing || "без вложений")}</small></li>`;
  }).join("") || `<li>нет</li>`;
  $("enemies").innerHTML = Object.entries(s.enemies || {}).map(([k, e]) =>
    `<li class="item-clickable" data-click="showEnemy" data-arg="${attrArg(k)}">${e.desc ? `<small class="desc">${trunc(e.desc, 80)}</small>` : ``}⚔ ${esc(e.name)} — HP ${e.hp}/${e.max_hp}${e.money ? ` <em class="val">🪙${e.money}</em>` : ``}</li>`).join("") || `<li>нет</li>`;
  $("npc-list").innerHTML = Object.entries(s.npc || {}).map(([k, n]) => {
    let sch = "";
    const schedule = n.schedule;
    if (schedule && typeof schedule === "object" && n.alive !== false && s.time) {
      const t = String(s.time).toLowerCase();
      for (const [k2, act] of Object.entries(schedule)) {
        const kk = String(k2).toLowerCase();
        if (t.includes(kk) || kk.includes(t)) { sch = ` <small class="sch">⏰ ${esc(act)}</small>`; break; }
      }
    }
    return `<li class="item-clickable" data-click="showNpc" data-arg="${attrArg(k)}">${n.alive === false ? "🪦 " : "🗣 "}${esc(n.name)} (${trunc(n.mood || n.desc || "", 40)}${n.faction ? ", " + esc(factionName(n.faction)) : ""}${n.money ? ", 🪙" + esc(n.money) : ""})${sch}</li>`;
  }).join("") || `<li>нет</li>`;
  $("companions-list").innerHTML = Object.entries(s.companions || {}).map(([k, c]) =>
    `<li class="item-clickable" data-click="showCompanion" data-arg="${attrArg(k)}">${c.hp <= 0 ? "💀 " : "🤝 "}<b>${esc(c.name || k)}</b> ⚔ ${c.hp}/${c.max_hp || c.hp} Lv${c.level || 1}${c.loyalty ? ` · верность ${esc(c.loyalty)}` : ""}<small>${trunc(c.desc, 80)}</small></li>`).join("") || `<li>нет</li>`;
  $("flags").innerHTML = Object.entries(s.flags || {}).map(([k, v], fi) =>
    `<span class="item-clickable flag-pill" data-click="showFlag" data-arg="${numAttr(fi)}" title="${esc(k)}=${esc(JSON.stringify(v))}">🚩 ${esc(flagLabel(k))}: ${flagWord(v)}</span>`).join("") || `<span class="flags-none">нет</span>`;
  // E8 (аудит 38): у подсказки про флаги теперь есть смысл — она прячется, когда флагов
  // нет (раньше id был объявлен в разметке и не упоминался ни в JS, ни в CSS).
  const fh = $("flags-hint");
  if (fh) fh.style.display = Object.keys(s.flags || {}).length ? "" : "none";
  renderMap();
  renderSuggestionBar();
  // C4 (сессия 34): часы мира (время/погода/сезон) и компас соседних локаций
  renderWorldClock(s);
  renderCompass(s);
}

/* ─────────────── Карта мира (SVG-граф локаций) ─────────────── */
/* ─────────────── Карта мира (SVG-граф: зум/панорама, автоподгон) ─────────────── */
let mapView = null;      // {x, y, w, h} в кооординатах раскладки
let mapWorld = null;     // для какого мира построена (сброс при смене)
let mapBounds = null;    // текущие границы раскладки (для подгона)
let _mapSuppressClick = false;
let _mapPath = null;     // id-путь по выбранной цели (подсветка кратчайшего пути)
let _mapLabels = false;  // показывать подписи узлов
let _mapReachOnly = true;// затемнять недостижимые (самые дальние/изолированные)
// 🌐 граф как источник карты (данные с /api/worlds/{id}/graph)
let mapGraph = null;          // {nodes, edges, current, layers, path}
let mapGraphWorld = null;     // для какого мира загружен граф
let mapGraphLoading = false;  // защита от повтора запросов

async function refreshMapGraph() {
  // Подгрузить граф мира с эндпоинта и перерисовать карту (без дублей).
  if (!state.currentWorld || mapGraphLoading) return;
  mapGraphLoading = true;
  try {
    const g = await API(`/api/worlds/${state.currentWorld}/graph`);
    if (state.currentWorld) {
      mapGraph = g;
      mapGraphWorld = state.currentWorld;
      renderMap();
    }
  } catch (e) {
    // граф недоступен — остаёмся на прежних данных / фолбэк setting.locations
  } finally {
    mapGraphLoading = false;
  }
}

function _mapPathIds(from, to, adj, ids) {
  // BFS с родителями → кратчайший путь from..to (или null, если to недостижим)
  if (from === to) return [from];
  const seen = new Set([from]);
  const par = {};
  const q = [from];
  while (q.length) {
    const u = q.shift();
    for (const v of (adj[u] || [])) {
      if (seen.has(v)) continue;
      seen.add(v); par[v] = u;
      if (v === to) {
        const path = [to]; let x = to;
        while (x !== from) { x = par[x]; if (x === undefined) return null; path.unshift(x); }
        return path;
      }
      q.push(v);
    }
  }
  return null;
}

function _mapWireFilters(svg) {
  const bar = $("map-filters");
  if (!bar || bar.dataset.wired) return;
  bar.dataset.wired = "1";
  const re = $("mf-reach"), lb = $("mf-labels"), cl = $("mf-clear");
  if (re) { re.checked = _mapReachOnly; re.onchange = () => { _mapReachOnly = re.checked; renderMap(); }; }
  if (lb) { lb.checked = _mapLabels; lb.onchange = () => { _mapLabels = lb.checked; renderMap(); }; }
  if (cl) cl.onclick = () => { _mapPath = null; renderMap(); };
}
// Кнопка «Сбросить путь» видна, только когда на карте подсвечен путь (клик по дальней локации).
function syncMapFilters() {
  const cl = $("mf-clear");
  if (cl) cl.style.display = (_mapPath && _mapPath.length > 1) ? "" : "none";
}

function mapFit(svg, bounds) {
  const LW = 600, LH = 420;
  let { minX, minY, maxX, maxY } = bounds;
  // минимум — вся плоскость раскладки
  const x0 = Math.min(minX, 0), y0 = Math.min(minY, 0);
  const x1 = Math.max(maxX, LW), y1 = Math.max(maxY, LH);
  mapView = { x: x0 - 10, y: y0 - 10, w: (x1 - x0) + 20, h: (y1 - y0) + 20 };
  svg.setAttribute("viewBox", `${mapView.x} ${mapView.y} ${mapView.w} ${mapView.h}`);
}

function renderMap() {
  const svg = $("world-map");
  if (!svg || !state.setting) return;
  const s = state.setting;
  const locs = s.locations || {};
  const cur = s.current_location || "start";
  const ids = Object.keys(locs);
  if (!ids.length) { svg.innerHTML = ""; return; }

  // 🌐 источник истины — граф-эндпоинт (если загружен), иначе фолбэк на setting.locations
  const g = (mapGraphWorld === state.currentWorld) ? mapGraph : null;
  const mapVisible = () => { const p = $("tab-map"); return p && p.style.display !== "none"; };
  if (!g && mapVisible()) { refreshMapGraph(); }
  const nameOf = {};                             // id -> имя локации (из графа, если есть)
  if (g && g.nodes) g.nodes.forEach((n) => { if (n.kind === "location") nameOf[n.id] = n.label || n.id; });

  // ── смежность (идю location-узлов; рёбра из графа, иначе connections) ──
  const adj = {};
  ids.forEach((id) => { adj[id] = new Set(); });
  if (g && g.edges) {
    g.edges.forEach((e) => {
      if (adj[e.source] && adj[e.target]) { adj[e.source].add(e.target); adj[e.target].add(e.source); }
    });
  } else {
    ids.forEach((id) => {
      (locs[id].connections || []).forEach((c) => { if (adj[c]) { adj[id].add(c); adj[c].add(id); } });
    });
  }

  // ── BFS от текущей локации → уровни колец (радиус без жёсткой крышки — подгоняется) ──
  const level = {}; let maxLevel = 0;
  const queue = [[cur, 0]]; const seen = new Set();
  while (queue.length) {
    const [id, lv] = queue.shift();
    if (seen.has(id)) continue;
    seen.add(id); level[id] = lv; maxLevel = Math.max(maxLevel, lv);
    [...adj[id]].forEach((n) => { if (!seen.has(n)) queue.push([n, lv + 1]); });
  }
  ids.filter((id) => !seen.has(id)).forEach((id) => { level[id] = level[id] ?? 0; });
  const lonely = maxLevel + 1;
  ids.forEach((id) => { if (!seen.has(id) && level[id] === 0) level[id] = lonely; });

  const LW = 600, LH = 420, CX = LW / 2, CY = LH / 2;
  const radiusFor = (lv) => (lv === 0 ? 0 : 62 + lv * 64);   // кольца шире: много уровней не слипаются
  const pos = {}; const counts = {}; const idx = {};
  ids.forEach((id) => { counts[level[id]] = (counts[level[id]] || 0) + 1; });
  ids.forEach((id) => {
    const lv = level[id];
    idx[lv] = (idx[lv] || 0) + 1;
    const angle = (idx[lv] - 1) * (2 * Math.PI / counts[lv]) - Math.PI / 2;
    const r = lv === 0 ? 0 : radiusFor(lv);
    pos[id] = { x: CX + r * Math.cos(angle), y: CY + r * Math.sin(angle) };
  });

  // границы для подгона (с учётом подписи и радиуса узла)
  const bounds = { minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity };
  ids.forEach((id) => {
    bounds.minX = Math.min(bounds.minX, pos[id].x - 95);
    bounds.maxX = Math.max(bounds.maxX, pos[id].x + 95);
    bounds.minY = Math.min(bounds.minY, pos[id].y - 20);
    bounds.maxY = Math.max(bounds.maxY, pos[id].y + 40);
  });

  // сброс вида при смене мира; иначе сохраняем текущий зум/сдвиг пользователя
  if (mapWorld !== state.currentWorld || !mapView) { mapFit(svg, bounds); mapWorld = state.currentWorld; _mapPath = null; }
  mapBounds = bounds;
  _mapWireFilters(svg);
  syncMapFilters();

  // ── рёбра ──
  const connected = new Set(adj[cur] || []);
  const reachable = new Set(seen);            // все достижимые из текущей (BFS-множество)
  const pathSet = _mapPath ? new Set(_mapPath) : null;
  const pathEdges = new Set();
  if (pathSet) {
    for (let i = 0; i < _mapPath.length - 1; i++) {
      pathEdges.add([_mapPath[i], _mapPath[i + 1]].sort().join("|"));
    }
  }
  const lines = []; const seenPair = new Set();
  ids.forEach((id) => {
    (locs[id].connections || []).forEach((c) => {
      const key = [id, c].sort().join("|");
      if (pos[c] && !seenPair.has(key)) {
        seenPair.add(key);
        const onPath = pathEdges.has(key);
        lines.push(`<line${onPath ? " class=\"path\"" : ""} x1="${pos[id].x}" y1="${pos[id].y}" x2="${pos[c].x}" y2="${pos[c].y}"/>`);
      }
    });
  });

  // ── узлы: крупные подписи с обводкой (читаемо поверх линий), клик = пойти/подсветить путь ──
  const nodes = ids.map((id) => {
    const l = locs[id] || {};
    const fname = nameOf[id] || l.name || id;
    const name = fname.slice(0, 22);
    const isCur = id === cur;
    const isNear = connected.has(id);
    const onPath = pathSet && pathSet.has(id);
    const dimmed = _mapReachOnly && !reachable.has(id);
    const cls = [
      isCur ? "cur" : (isNear ? "near" : (onPath ? "path" : "far")),
      dimmed ? "dim" : "",
      onPath ? "ispath" : "",
      "clickable",
    ].filter(Boolean).join(" ");
    const showLabel = isCur || isNear || onPath || _mapLabels;
    const lbl = showLabel ? `<text class="nlabel" x="${pos[id].x}" y="${pos[id].y + 36}">${esc(name)}</text>` : "";
    const tip = isCur ? "Осмотреться" : (onPath ? "\n— это путь до выбранной цели" : "\n— кликни, чтобы подсветить путь");
    return `<g class="node ${cls}" data-id="${esc(id)}" data-name="${esc(fname)}">
      <title>${esc(fname)}${tip}</title>
      <circle cx="${pos[id].x}" cy="${pos[id].y}" r="15"/>
      ${lbl}
    </g>`;
  }).join("");

    // ── маркеры не-локаций (магазины/фракции/живые NPC), из графа ──
  let markers = "";
  if (g && g.nodes && g.edges) {
    const nodeKind = {}; const anchor = {};
    g.nodes.forEach((n) => { nodeKind[n.id] = n.kind; });
    g.edges.forEach((e) => {
      const ka = nodeKind[e.source], kb = nodeKind[e.target];
      if (ka && ka !== "location" && kb === "location") anchor[e.source] = e.target;
      else if (ka === "location" && kb && kb !== "location") anchor[e.target] = e.source;
    });
    const mkIcon = {
      shop: String.fromCodePoint(0x1F3EA),
      faction: String.fromCodePoint(0x1F3F4),
      npc: String.fromCodePoint(0x1F5E3)
    };
    g.nodes.forEach((n) => {
      if (n.kind === "location") return;
      const anc = anchor[n.id]; if (!anc || !pos[anc]) return;
      const x = pos[anc].x + 18, y = pos[anc].y + 10;
      const icon = mkIcon[n.kind] || n.kind.slice(0, 1);
      const desc = (n.data && n.data.desc) ? (" " + n.data.desc) : "";
      markers += `<g class="node marker m-${n.kind}" data-id="${esc(n.id)}" data-name="${esc(n.label || n.id)}">`;
      markers += `<title>${esc(n.label || n.id)}${esc(desc)}</title>`;
      markers += `<circle cx="${x}" cy="${y}" r="6"/>`;
      markers += `<text class="mkin" x="${x}" y="${y+3}">${icon}</text>`;
      markers += `</g>`;
    });
  }
  svg.innerHTML = lines.join("") + nodes + markers;

  svg.querySelectorAll(".node.clickable").forEach((g) => {
    g.addEventListener("click", (e) => {
      e.stopPropagation();
      if (state.streaming || _mapSuppressClick) return;
      const id0 = g.dataset.id;
      const name = g.dataset.name;
      if (id0 === cur) { quickAction("Осмотреться вокруг."); return; }
      const isNear = connected.has(id0);
      if (isNear) { quickAction(`Я иду в «${name}».`); return; }
      // дальняя локация → подсветить кратчайший путь из текущей
      _mapPath = _mapPathIds(cur, id0, adj, ids);
      renderMap();
    });
  });

  // ── зум/панорама (один раз на svg-элемент) ──
  if (!svg.dataset.mapReady) {
    svg.dataset.mapReady = "1";
    const pt = (ev) => {
      const r = svg.getBoundingClientRect();
      return {
        x: mapView.x + (ev.clientX - r.left) / r.width * mapView.w,
        y: mapView.y + (ev.clientY - r.top) / r.height * mapView.h,
      };
    };
    const apply = () => svg.setAttribute("viewBox", `${mapView.x} ${mapView.y} ${mapView.w} ${mapView.h}`);
    svg.addEventListener("wheel", (e) => {
      e.preventDefault();
      const p = pt(e); const f = e.deltaY < 0 ? 1.2 : 1 / 1.2;
      const nw = Math.max(120, Math.min(4000, mapView.w * f));
      const nh = nw * (mapView.h / mapView.w);       // сохраняем пропорции
      mapView.x = p.x - (p.x - mapView.x) * (nw / mapView.w);
      mapView.y = p.y - (p.y - mapView.y) * (nh / mapView.h);
      mapView.w = nw; mapView.h = nh;
      apply();
    }, { passive: false });
    let drag = null;
    svg.addEventListener("pointerdown", (e) => {
      drag = { sx: e.clientX, sy: e.clientY, vx: mapView.x, vy: mapView.y, moved: false, id: e.pointerId };
      svg.setPointerCapture(e.pointerId);
      svg.classList.add("dragging");
    });
    svg.addEventListener("pointermove", (e) => {
      if (!drag || drag.id !== e.pointerId) return;
      const dx = e.clientX - drag.sx, dy = e.clientY - drag.sy;
      if (Math.abs(dx) + Math.abs(dy) > 5) drag.moved = true;
      mapView.x = drag.vx - dx * (mapView.w / svg.clientWidth);
      mapView.y = drag.vy - dy * (mapView.h / svg.clientHeight);
      apply();
    });
    svg.addEventListener("pointerup", (e) => {
      if (!drag || drag.id !== e.pointerId) return;
      _mapSuppressClick = drag.moved;               // после перетаскивания клик по узлу не срабатывает
      drag = null; svg.classList.remove("dragging");
      setTimeout(() => { _mapSuppressClick = false; }, 40);
    });
    svg.addEventListener("dblclick", () => { if (mapBounds) mapFit(svg, mapBounds); });
    // кнопки зума (поверх карты) — вешаем один раз у враппера
    const wrap = $("map-wrap");
    if (wrap) {
      const zoom = (f) => {
        const cx = mapView.x + mapView.w / 2, cy = mapView.y + mapView.h / 2;
        const nw = Math.max(120, Math.min(4000, mapView.w * f));
        const nh = nw * (mapView.h / mapView.w);
        mapView = { x: cx - nw / 2, y: cy - nh / 2, w: nw, h: nh };
        apply();
      };
      const zi = document.createElement("button");
      zi.className = "map-btn"; zi.textContent = "+"; zi.title = "Приблизить";
      zi.onclick = () => zoom(1.3);
      const zo = document.createElement("button");
      zo.className = "map-btn"; zo.textContent = "−"; zo.title = "Отдалить";
      zo.onclick = () => zoom(1 / 1.3);
      const zr = document.createElement("button");
      zr.className = "map-btn"; zr.textContent = "⤢"; zr.title = "Подогнать всё (двойной клик по карте)";
      zr.onclick = () => { if (mapBounds) mapFit(svg, mapBounds); };
      wrap.appendChild(zi); wrap.appendChild(zo); wrap.appendChild(zr);
    }
  }
}

/* ─────────────── Варианты действий (кнопки) ─────────────── */
let _sugReqInFlight = false;      // один запрос пересчёта подсказок за раз
let _sugReqAt = 0;                // когда был последний (защита от шторма при ↻ подряд)
const _SUG_MIN_INTERVAL_MS = 4000;

async function loadSuggestionRefresh(worldId) {
  // Свежие варианты действий по текущей сцене (вместо устаревших из localStorage).
  // Сервер перегенерит подсказки по последнему ответу рассказчика (best-effort, не блокирует).
  // Ограничение по времени (сессия 36, п.15): теперь этот вызов идёт и при пустом ответе
  // сервера на каждом ходу, поэтому не допускаем параллельных/частых повторок — /suggest
  // сам по себе дорогой LLM-проход.
  if (!worldId || _sugReqInFlight) return;
  const now = Date.now();
  if (now - _sugReqAt < _SUG_MIN_INTERVAL_MS) return;
  _sugReqInFlight = true;
  _sugReqAt = now;
  try {
    const r = await API(`/api/worlds/${worldId}/suggest`, { method: "POST" });
    if (Array.isArray(r.suggestions) && r.suggestions.length) {
      state.suggestions = r.suggestions.filter(Boolean);
      try { localStorage.setItem(`textgame.suggestions.${worldId}`, JSON.stringify(state.suggestions)); } catch (_) {}
      renderSuggestionBar();
    }
  } catch (_) { /* best-effort: остаются текущие варианты */ }
  finally { _sugReqInFlight = false; }
}

function renderSuggestionBar() {
  const bar = $("suggestion-bar");
  if (!bar || !state.setting) return;
  const s = state.setting;
  if (s.game_over) { bar.style.display = "none"; return; }
  const sug = [];
  const add = (t, a) => {
    if (sug.length >= 5) return;
    if (!sug.some((x) => x.a === a && x.t === t)) sug.push({ t, a });
  };
  const render = (label) => {
    bar.innerHTML = `<span class="sug-label">${label}</span>` +
      sug.map((x) => `<button class="btn small sug" data-a="${esc(x.a)}">${esc(x.t)}</button>`).join("");
    bar.style.display = "";
    bar.querySelectorAll(".sug").forEach((b) => {
      b.onclick = () => { if (!state.streaming) quickAction(b.dataset.a); };
    });
  };

  // 1) ИИ-варианты (рассказчик предлагает 3–4 действия под ситуацию) — приоритет
  const ai = (Array.isArray(state.suggestions) ? state.suggestions : [])
    .map((t) => String(t).trim().slice(0, 90)).filter(Boolean);
  if (ai.length) {
    ai.slice(0, 5).forEach((t) => add(t, t));
    render("Рассказчик предлагает:");
    return;
  }

  // 2) фолбэк: бытовые заготовки от состояния (когда ИИ не ответил)
  const p = s.player || {};
  const loc = (s.locations || {})[s.current_location] || {};
  // соседние локации (карта)
  (loc.connections || []).forEach((cid) => {
    const l = (s.locations || {})[cid];
    if (l && cid !== s.current_location) add(`🚶 Идти в ${l.name}`, `Я иду в «${l.name}».`);
  });
  // живые NPC
  Object.values(s.npc || {}).forEach((n) => {
    if (n.alive !== false) add(`🗣 Поговорить с ${n.name}`, `Поговорить с ${n.name}.`);
  });
  // враги рядом
  Object.values(s.enemies || {}).forEach((e) => {
    add(`⚔ Атаковать ${e.name}`, `Атаковать ${e.name}!`);
  });
  // активные квесты
  Object.values(s.quests || {}).forEach((q) => {
    if (q.status === "active") add(`📜 ${q.title}`, `Продолжить задание: ${q.title}.`);
  });
  // рецепты крафта
  Object.values(s.crafts || {}).forEach((c) => {
    add(`🛠 Создать: ${(c.result || {}).name || c.name}`, `Я хочу создать ${(c.result || {}).name || c.name} (крафт по рецепту).`);
  });
  // живые компаньоны — можно отдать команду
  Object.values(s.companions || {}).forEach((c) => {
    if (c.hp > 0) add(`🤝 Приказ: ${c.name}`, `Отдать приказ спутнику ${c.name}.`);
  });
  // стартовые вопросы
  if (!p.race) add("🧝 Определиться с расой", "Какая у меня раса? Расскажи о моём происхождении.");
  if (!p.class) add("🎭 Определиться с классом", "Чем я владею лучше всего? Выбери мой класс.");
  // универсальные
  add("👀 Осмотреться", "Осмотреться вокруг.");
  add("🌙 Отдохнуть", "Отдохнуть и перевести дух.");
  add("🧰 Заняться инвентарём", "Осмотреть и перебрать свои вещи.");

  render("Быстрые действия:");
}

function quickAction(text) {
  appendMsg({ role: "player", content: text, seq: "" });
  streamTurn(text, false);
}

/* ─────────────── Воззвание к Провидению (Божественный арбитр) ───────────────
 * Игрок считает, что рассказчик ошибся (не выдал предмет / не списал ресурс и т.п.).
 * Открывает окно ввода; жалоба уходит отдельной строгой ролью-модели; если ошибка реальна —
 * Провидение правит мир и даёт сюжетное объяснение (искажение реальности). */
function divineModal() {
  if (state.streaming) return;
  const draft = "Мне не выдали предмет, о котором написал рассказчик.";
  openModal("👁 Воззвание к Провидению",
    `<p class="muted">Опишите разрыв реальности: что произошло не так, как должно было — "не выдали предмет", "не списали потраченное зелье/золото", "награда не пришла", «урон не применился» и т.п. Провидение проверит логику и, если ошибка реальна, исправит мир сюжетным поворотом.</p>` +
    `<textarea id="divine-complaint" rows="4" maxlength="500" placeholder="Например: произошла ошибка, мне не дали предмет, о котором написано">${esc(draft)}</textarea>`,
    () => {
      const t = ($("divine-complaint")?.value || "").trim();
      if (!t) return;
      closeModal();
      doDivine(t);
    });
}

function addDivineTyper() {
  const div = document.createElement("div");
  div.className = "msg divine typing";
  div.innerHTML = `<div class="who">\ud83d\udd1f Провидение</div><div class="body">Вглядывается в ткань реальности…</div>`;
  $("log").appendChild(div);
  $("log").scrollTop = $("log").scrollHeight;
  return div;
}

async function doDivine(complaint) {
  const typer = addDivineTyper();
  try {
    const res = await API(`/api/worlds/${state.currentWorld}/divine`,
      { method: "POST", body: JSON.stringify({ complaint }) });
    handleDivine(res, typer);
  } catch (err) {
    if (typer) typer.remove();
    appendMsg({ role: "system", content: "⚠ " + (err.message || err) });
  }

}

function handleDivine(res, typer) {
  if (typer) typer.remove();
  (res.events || []).forEach((e) => appendMsg(e));
  if (res.state) { state.setting = res.state; renderSetting(res.state); }
  if (res.game_over) appendMsg({ role: "system", content: "💀 Мир завершён." });
  loadEntities();
}

// Человекочитаемая передача значения флага для обычного игрока
function flagWord(v) {
  if (v === true) return "да";
  if (v === false) return "нет";
  return JSON.stringify(v);
}

// Человекочитаемое имя фракции по id (если фракция есть в состоянии) — иначе сам id
function factionName(id) {
  const f = (state.setting && state.setting.factions) || {};
  const fr = String(id || "");
  return (f[fr] && f[fr].name) ? f[fr].name : (fr || "?");
}

// Читаемый ярлык флага: известные ключи → фразы, иначе id → слова с заглавной
const FLAG_LABELS = {
  first_contract_broken: "Первый контракт сорван",
  known_heretic: "Известен как еретик",
  door_open: "Дверь открыта",
  trap_triggered: "Ловушка сработала",
  sealed_vault: "Хранилище запечатано",
  player_initiated: "Игрок начал разговор",
  met_the_librarian: "Встреча с хранителем",
  observer_summoned: "Наблюдатель призван",
  world_shift: "Мир изменился",
  allied_with_cult: "Союз с Культом",
  allied_with_blades: "Союз с Клинками",
};
function flagLabel(k) {
  if (FLAG_LABELS[k]) return FLAG_LABELS[k];
  const s = String(k || "").replace(/_/g, " ");
  return s ? ucfirst(s) : k;
}

const kv = (rows) => rows.map(([k, v]) => `<div>${k}</div><b>${v}</b>`).join("");

/* ─────────────── Блок «Персонаж» (справочники и помощники) ─────────────── */
const CHARACTER_RACE_DESC = {
  "человек": "универсал без слабостей", "эльф": "стремительный, острый глаз",
  "дварф": "крепкий, упёртый", "орк": "мощный, горячий", "зверолюд": "кошачьи/волчьи черты",
  "полудемон": "тёмное наследие", "драконид": "чешуя и когти", "нежить": "вампир/скелет"
};
const CHARACTER_CLASS_DESC = {
  "воин": "танк, физ. урон, тяжёлая броня", "лучник": "дальний бой, криты, скрытность",
  "маг": "магический урон и контроль", "вор": "скрытность, удар из тени, взлом",
  "жрец": "лечение, баффы, защита от нежити", "бард": "поддержка, дебаффы, дипломатия"
};
const CHARACTER_PROF_DESC = {
  "кузнец": "ковать и чинить оружие/броню", "алхимик": "зелья и эликсиры",
  "травник": "лечебные снадобья, знание ядов", "охотник": "следопытство, капканы, дичь",
  "шахтёр": "добыча руды и камней", "повар": "сытная еда с эффектами",
  "портной": "шитьё и починка одежды", "моряк": "корабли и навигация",
  "книжник": "языки, свитки, знания"
};
// Действия → профессия: {дело: {prof, thr, desc}} — прогресс профессий из повторяющихся дел
const CHARACTER_ACTION_DESC = {
  "кузнечное дело": {"prof": "Кузнец", "thr": 12, "desc": "ковать и чинить металл"},
  "ковка": {"prof": "Кузнец", "thr": 8, "desc": "ковать металл"},
  "ремонт": {"prof": "Кузнец", "thr": 8, "desc": "чинить вещи"},
  "алхимия": {"prof": "Алхимик", "thr": 12, "desc": "смешивать реагенты"},
  "зельеварение": {"prof": "Алхимик", "thr": 8, "desc": "варить зелья"},
  "травничество": {"prof": "Травник", "thr": 12, "desc": "знать и собирать травы"},
  "сбор трав": {"prof": "Травник", "thr": 8, "desc": "собирать травы"},
  "охота": {"prof": "Охотник", "thr": 12, "desc": "выслеживать и добывать дичь"},
  "следопытство": {"prof": "Охотник", "thr": 8, "desc": "читать следы"},
  "капканы": {"prof": "Охотник", "thr": 8, "desc": "ставить ловушки"},
  "горное дело": {"prof": "Шахтёр", "thr": 12, "desc": "добывать руду и камень"},
  "кулинария": {"prof": "Повар", "thr": 12, "desc": "готовить еду"},
  "готовка": {"prof": "Повар", "thr": 8, "desc": "готовить еду"},
  "портняжное дело": {"prof": "Портной", "thr": 12, "desc": "шить и чинить одежду"},
  "шитьё": {"prof": "Портной", "thr": 8, "desc": "шить"},
  "мореплавание": {"prof": "Моряк", "thr": 12, "desc": "управлять кораблём"},
  "навигация": {"prof": "Моряк", "thr": 8, "desc": "вести по курсу"},
  "книжное дело": {"prof": "Книжник", "thr": 12, "desc": "работать с книгами"},
  "чтение свитков": {"prof": "Книжник", "thr": 8, "desc": "читать свитки"},
  "изучение языков": {"prof": "Книжник", "thr": 8, "desc": "учить языки"},
};
function actionNameWithProg(a, c, curProf) {
  const map = CHARACTER_ACTION_DESC[String(a || "").toLowerCase()];
  const cur = String(curProf || "").toLowerCase();
  if (!map) return esc(ucfirst(a));
  const target = cur && String(map.prof).toLowerCase() === cur ? cur : map.prof;
  const actDesc = map.desc ? ` — ${map.desc}` : "";
  const prog = (+c >= +map.thr)
    ? ` (порог взят → ${ucfirst(target)})`
    : ` (→ ${ucfirst(target)} ${c}/${map.thr})`;
  return `${esc(ucfirst(a))}${actDesc}${prog}`;
}
function ucfirst(s) {
  s = String(s == null ? "" : s);
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}
// Ступень репутации игрока у фракции (число → ярлык), синхронна с backend.mechanics.reputation_standing
function repStanding(v) {
  v = Number(v) || 0;
  if (v <= -100) return "Изгой";
  if (v <= -12) return "Заклятый враг";
  if (v <= -5) return "Враг";
  if (v <= -1) return "Недоверие";
  if (v <= 1) return "Нейтрально";
  if (v <= 5) return "Доверие";
  if (v <= 12) return "Друг";
  return "Союзник";
}
function charRow(label, value) {
  return value ? `<div class="char-row"><span class="char-label">${label}</span><span class="char-value">${value}</span></div>` : "";
}
// Обрезка длинного описания по границе слова с многоточием (не режем на полуслове).
function trunc(s, n) {
  s = String(s == null ? "" : s);
  if (s.length <= n) return s;
  let cut = s.slice(0, n - 1);
  const sp = cut.lastIndexOf(" ");
  if (sp > Math.floor(n / 2)) cut = cut.slice(0, sp);
  return cut.replace(/\s+$/, "") + "…";
}
// Универсальные справки крафта (только отображение — решает мастер)
function stationList() {
  const s = state.setting || {};
  const loc = (s.locations || {})[s.current_location || "start"] || {};
  const stl = (loc.stations || []).slice();
  for (const [f, v] of Object.entries(s.flags || {})) {
    if (String(f).toLowerCase().startsWith("station:") && v) stl.push(String(f).slice("station:".length));
  }
  const seen = {}; return stl.filter((x) => { const k = String(x).toLowerCase(); if (seen[k]) return false; seen[k] = 1; return true; });
}
function stationHere(s, station) {
  const loc = (s.locations || {})[s.current_location || "start"] || {};
  const have = (loc.stations || []).map((x) => String(x).toLowerCase());
  const want = String(station || "").toLowerCase();
  if (have.includes(want)) return true;
  const flags = s.flags || {};
  return Object.keys(flags).some((f) => String(f).toLowerCase() === "station:" + want && flags[f]);
}
function craftReady(s, c) {
  // проверка «можно ли создать сейчас» (ингредиенты + станция + профессия)
  const inv = (s.player && s.player.inventory) || [];
  const need = {};
  (c.ingredients || []).forEach((i) => { need[i.name] = (need[i.name] || 0) + (i.qty || 1); });
  for (const [n, q] of Object.entries(need)) {
    const have = inv.filter((x) => x.name === n).reduce((a, x) => a + (x.qty || 0), 0);
    if (have < q) return false;
  }
  const st = Array.isArray(c.station) ? c.station : c.station ? [c.station] : [];
  if (st.length && !st.some((s2) => stationHere(s, s2))) return false;
  if (c.profession && String(c.profession).toLowerCase() !== String(s.player.profession || "").toLowerCase()) return false;
  return true;
}
function showItem(idx) {
  const inv = (state.setting && state.setting.player && state.setting.player.inventory) || [];
  const it = inv[idx];
  if (!it) return;
  const slotTxt = it.slot ? ` · Слот: ${esc(ucfirst(it.slot))}` : "";
  const bonusTxt = (it.bonus && typeof it.bonus === "object" && Object.keys(it.bonus).length)
    ? ` · Бонусы: ${Object.entries(it.bonus).map(([k, v]) => `${esc(ucfirst(k))}${v > 0 ? "+" : ""}${v}`).join(", ")}`
    : "";
  openModal("🧰 " + (it.name || "Предмет"),
    `<p class="item-big">${esc(it.name)} ×${it.qty || 1}</p>` +
    `<p class="muted">${it.weight ? `Вес ${Number(it.weight)} кг` : "Невесомое"}${it.value ? ` · Продажа 🪙${it.value}` : ""}${slotTxt}${bonusTxt}</p>` +
    (it.desc ? `<p class="item-full-desc">${esc(it.desc)}</p>` : `<p class="muted">Описание отсутствует.</p>`),
    () => {});
}
function showSkill(idx) {
  const skills = (state.setting && state.setting.player && state.setting.player.skills) || {};
  const entries = Object.entries(skills);
  const sk = entries[idx];
  if (!sk) return;
  const [n, v] = sk;
  if (v && typeof v === "object") {
    openModal("🎖 " + ucfirst(n),
      `<p class="item-big">${esc(ucfirst(n))} <span class="muted">ранг ${esc(v.rank || "F")}${v.kind ? " · " + esc(ucfirst(v.kind)) : ""}${v.mp_cost ? " · энергия " + v.mp_cost : ""}</span></p>` +
      (v.desc ? `<p class="item-full-desc">${esc(v.desc)}</p>` : `<p class="muted">Описание отсутствует.</p>`),
      () => {});
  } else {
    openModal("🎖 " + ucfirst(n), `<p class="item-big">${esc(ucfirst(n))} ур.${esc(v)}</p>`, () => {});
  }
}
function showAbility(idx) {
  const abil = (state.setting && state.setting.player && state.setting.player.abilities) || {};
  const entries = Object.entries(abil);
  const en = entries[idx];
  if (!en) return;
  const [n, v] = en;
  openModal("⚡ " + ucfirst(n),
    `<p class="item-big">${esc(ucfirst(n))}${v.school ? ` <span class="muted">[${esc(ucfirst(v.school))}]</span>` : ""}${v.cost ? ` <span class="muted">энергия ${esc(v.cost)}</span>` : ""}</p>` +
    (v.desc ? `<p class="item-full-desc">${esc(v.desc)}</p>` : `<p class="muted">Описание отсутствует.</p>`),
    () => {});
}
function showQuest(key) {
  const quests = (state.setting && state.setting.quests) || {};
  const q = quests[key];
  if (!q) return;
  const statusName = q.status === "done" ? "✔ выполнено" : (q.status === "active" ? "📜 активно" : (q.status || ""));
  let rows = `<p class="item-big">${esc(q.title || key)} <span class="muted">${esc(statusName)}</span></p>`;
  if (q.progress) rows += `<p><b>${esc(q.progress)}</b></p>`;
  if (q.chosen) rows += `<p><span class="muted">Ветка: ${esc(q.chosen)}</span></p>`;
  rows += (q.desc ? `<p class="item-full-desc">${esc(q.desc)}</p>` : `<p class="muted">Описание отсутствует.</p>`);
  if (q.steps && q.steps.length) {
    rows += `<p class="muted">Шаги:</p><ul class="list">` + q.steps.map((st, i) =>
      `<li>${i + 1}. ${esc(typeof st === "string" ? st : (st.desc || JSON.stringify(st)))}${st.done ? " ✔" : ""}</li>`
    ).join("") + `</ul>`;
  }
  openModal("📜 " + (q.title || "Квест"), rows, () => {});
}
function showEffect(idx) {
  const effects = (state.setting && state.setting.player && state.setting.player.effects) || {};
  const en = Object.entries(effects)[idx];
  if (!en) return;
  const [name, ef] = en;
  let label = String(name || "");
  if (/^[A-Za-z0-9_\-]+$/.test(label)) label = (label.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()) || label);
  const t = (ef.turns === undefined || ef.turns === null || ef.turns === -1) ? "постоянно" : `${ef.turns} ход.`;
  let meta = `${esc(ef.kind ? ucfirst(ef.kind) : "особый")} · ${t}`;
  if (ef.damage) meta += ` · −${ef.damage} HP/ход`;
  if (ef.heal) meta += ` · +${ef.heal} HP/ход`;
  if ((ef.stacks || 1) > 1) meta += ` · ×${ef.stacks}`;
  const mods = ef.mods || {};
  if (Object.keys(mods).length) meta += ` · моды: ${Object.entries(mods).map(([k, v]) => `${esc(k)}${v > 0 ? "+" : ""}${v}`).join(", ")}`;
  openModal("⏳ " + label,
    `<p class="item-big">${esc(label)} <span class="muted">${esc(t)}</span></p>` +
    `<p class="muted">${esc(meta)}</p>` +
    (ef.desc ? `<p class="item-full-desc">${esc(ef.desc)}</p>` : `<p class="muted">Описание эффекта отсутствует.</p>`),
    () => {});
}
function showFaction(idx) {
  const factions = (state.setting && state.setting.factions) || {};
  const entries = Object.entries(factions);
  const en = entries[idx];
  if (!en) return;
  const [id, f] = en;
  const rep = (state.setting.player?.reputation || {})[id] || 0;
  const rel = Object.entries(f.relations || {}).map(([oid, st2]) => {
    const oname = (state.setting.factions || {})[oid]?.name || oid;
    return `${esc(oname)} — ${esc(st2)}`;
  });
  let rows = `<p class="item-big">🏴 ${esc(f.name || id)} <span class="muted">${esc(repStanding(rep))} (${rep >= 0 ? "+" : ""}${rep})</span></p>`;
  if (f.alignment) rows += `<p><span class="muted">Мировоззрение:</span> ${esc(ucfirst(f.alignment))}</p>`;
  if (rel.length) rows += `<p><span class="muted">Связи:</span> ${rel.join("; ")}</p>`;
  rows += (f.desc ? `<p class="item-full-desc">${esc(f.desc)}</p>` : `<p class="muted">Описание отсутствует.</p>`);
  openModal("🏴 " + (f.name || "Фракция"), rows, () => {});
}

function showFlag(idx) {
  const flags = (state.setting && state.setting.flags) || {};
  const entries = Object.entries(flags);
  const en = entries[idx];
  if (!en) return;
  const [k, v] = en;
  openModal("🚩 " + flagLabel(k),
    `<p class="item-big">${esc(flagLabel(k))} <span class="muted">(${flagWord(v)})</span></p>` +
    `<p class="muted">Ключ: ${esc(k)}</p>` +
    `<p class="muted">Флаг — факт мира, который помнит движок (открытые двери, выборы, события).</p>`,
    () => {});
}

function showNpc(key) {
  const npcs = (state.setting && state.setting.npc) || {};
  const n = npcs[key];
  if (!n) return;
  let rows = `<p class="item-big">${esc(n.name || key)} ${n.alive === false ? "🪦" : "🗣"}</p>`;
  if (n.mood) rows += `<p><span class="muted">Настроение:</span> ${esc(n.mood)}</p>`;
  if (n.money) rows += `<p><span class="muted">Кошелёк:</span> 🪙${esc(n.money)} ${n.alive === false ? "(забран при смерти)" : ""}</p>`;
  if (n.faction) {
    const fr = String(n.faction);
    const st = repStanding((state.setting.player?.reputation || {})[fr] || 0);
    const fname = factionName(fr);
    rows += `<p><span class="muted">Фракция:</span> ${esc(fname)} <span class="muted">→ ${esc(st)}</span></p>`;
  }
  if (n.schedule && typeof n.schedule === "object") rows += `<p><span class="muted">Расписание:</span> ${Object.entries(n.schedule).map(([k2, v]) => `${esc(k2)} → ${esc(v)}`).join("; ")}</p>`;
  rows += (n.desc ? `<p class="item-full-desc">${esc(n.desc)}</p>` : `<p class="muted">Описание отсутствует.</p>`);
  openModal("🗣 " + (n.name || "Персонаж"), rows, () => {});
}
function showCompanion(key) {
  const comps = (state.setting && state.setting.companions) || {};
  const c = comps[key];
  if (!c) return;
  let rows = `<p class="item-big">${esc(c.name || key)} ${c.hp <= 0 ? "💀" : "🤝"}</p>`;
  rows += `<p class="muted">⚔ HP ${c.hp}/${c.max_hp || c.hp} · Lv${c.level || 1}${c.loyalty ? ` · верность ${esc(c.loyalty)}` : ""}${c.faction ? ` · ${esc(c.faction)}` : ""}</p>`;
  const skills = c.skills || {};
  if (Object.keys(skills).length) rows += `<p class="muted">Навыки:</p><ul class="list">` + Object.entries(skills).map(([sn, sv]) =>
    `<li>${esc(sn)}<small>${sv && typeof sv === "object" ? ` (ранг ${esc(sv.rank || "F")})` : ""}</small></li>`).join("") + `</ul>`;
  rows += (c.desc ? `<p class="item-full-desc">${esc(c.desc)}</p>` : `<p class="muted">Описание отсутствует.</p>`);
  openModal("🤝 " + (c.name || "Спутник"), rows, () => {});
}
function showShop(key) {
  const shops = (state.setting && state.setting.shops) || {};
  const sh = shops[key];
  if (!sh) return;
  let rows = `<p class="item-big">${esc(sh.name || key)}</p>`;
  if (sh.owner) rows += `<p><span class="muted">Владелец:</span> ${esc(sh.owner)}</p>`;
  if (sh.faction) rows += `<p><span class="muted">Фракция:</span> ${esc(factionName(sh.faction))}</p>`;
  rows += (sh.desc ? `<p class="item-full-desc">${esc(sh.desc)}</p>` : ``);
  rows += `<p class="muted">Товары:</p><ul class="list">` + (sh.items || []).map((i) =>
    `<li>${esc(i.name)} — ${i.price}🪙 ×${i.qty || 1}`
    + (i.value ? ` <small title="продажа">🪙${i.value}</small>` : ``)
    + (i.weight ? ` <small>${Number(i.weight)}кг</small>` : ``)
    + (i.desc ? `<small> ${esc(i.desc)}</small>` : ``) + `</li>`
  ).join("") + `</ul>`;
  // станции локации (если есть у текущего места)
  const stl = stationList();
  if (stl.length) rows += `<p class="muted">🔧 Станции здесь: ${esc(stl.join(", "))}</p>`;
  openModal("🏪 " + (sh.name || "Магазин"), rows, () => {});
}
function showCraft(key) {
  const crafts = (state.setting && state.setting.crafts) || {};
  const c = crafts[key];
  if (!c) return;
  const res = c.result || {};
  let rows = `<p class="item-big">${esc(c.name || key)}</p>`;
  rows += `<p class="muted">Ингредиенты:</p><ul class="list">` + (c.ingredients || []).map((i) =>
    `<li>${esc(i.name)} ×${i.qty}</li>`).join("") + `</ul>`;
  rows += `<p><b>Результат:</b> ${esc(res.name || "?")} ×${res.qty || 1}`
    + (res.weight ? ` <small>${Number(res.weight)}кг</small>` : ``)
    + (res.value ? ` <small>🪙${res.value}</small>` : ``)
    + (res.desc ? `<small> — ${esc(res.desc)}</small>` : ``) + `</p>`;
  let req = "";
  if (c.station) {
    const stl = Array.isArray(c.station) ? c.station.join(", ") : c.station;
    req += `<p class="muted">🔧 Станция: ${esc(stl)}</p>`;
  }
  if (c.profession) req += `<p class="muted">⚒ Профессия: ${esc(c.profession)}</p>`;
  if (req) rows += req;
  const ready = craftReady(state.setting, c);
  rows += `<p${ready ? ` class="good"` : ` class="bad"`}>${ready ? "✓ Сейчас можно создать" : "✗ Не хватает условий"}</p>`;
  rows += (c.desc ? `<p class="item-full-desc">${esc(c.desc)}</p>` : ``);
  openModal("🛠 " + (c.name || "Крафт"), rows, () => {});
}
function showEnemy(key) {
  const enemies = (state.setting && state.setting.enemies) || {};
  const e = enemies[key];
  if (!e) return;
  let rows = `<p class="item-big">⚔ ${esc(e.name || key)}</p>`;
  rows += `<p class="muted">HP ${e.hp}/${e.max_hp || e.hp}${e.dmg ? ` · урон ${e.dmg}` : ""}${e.money ? ` · 🪙${e.money}` : ""}</p>`;
  rows += (e.desc ? `<p class="item-full-desc">${esc(e.desc)}</p>` : `<p class="muted">Описание врага отсутствует.</p>`);
  openModal("⚔ " + (e.name || "Враг"), rows, () => {});
}

/* ─────────────── Лог сообщений ─────────────── */
function msgClass(role) {
  if (role === "player") return "player";
  if (role === "dice") return "dice";
  if (role === "system") return "system";
  if (role === "summary") return "summary";
  if (role === "divine") return "divine";
  // E2 (аудит 38): неизвестную роль раньше рисовали как рассказчика — визуально врёт
  // (роль может прийти из импортированного дампа или режима мастера)
  return "unknown";
}
const WHO = { player: "Вы", narrator: "Рассказчик", dice: "Кубы", system: "СИСТЕМА", summary: "Сводка памяти", divine: "\u2764\ufe0f Провидение" };

/* A10 (аудит 38): ЕДИНЫЙ предикат «показывать ли это событие в логе игрока».
 * Раньше openWorld рисовал всё, что прислал сервер (включая сводки памяти — спойлеры
 * свёрнутого прошлого), а SSE/поллинг фильтровали по-своему: картина «до F5» и «после F5»
 * отличалась. Теперь фильтр один, а набор ролей серверного журнала приходит с миром
 * (d.log_roles = backend/routers/core.py::CHAT_LOG_ROLES — единый источник правды).
 * Сводки (summary) игрок не видит НИГДЕ: это материал промпта, а не часть истории. */
function shouldRenderEvent(e) {
  if (!e || !e.role) return false;
  if (e.role === "summary") return false;      // сводка памяти — служебный слой
  if (e.folded) return false;                  // сокрыто перемоткой/загрузкой
  return true;
}
/* Для живых доставок (SSE/поллинг) действия игрока не догружаются: они уже нарисованы
 * оптимистично в sendAction/quickAction. При ОТКРЫТИИ мира, наоборот, нужны. */
function shouldRenderLive(e) {
  return shouldRenderEvent(e) && e.role !== "player";
}

/* Плашка «🧠 Память» у ответа рассказчика: показывает подхваченные фрагменты памяти/лора.
 * Данные берём из meta события (хранятся в БД — плашка переживает перезагрузку страницы). */
function attachMemoryPill(msgEl, mem) {
  if (!mem || !mem.length) return;
  if (msgEl.querySelector(".mem-pill")) return;
  const pill = document.createElement("button");
  pill.className = "mem-pill";
  pill.textContent = `🧠 Память (${mem.length})`;
  pill.title = "Какие фрагменты памяти были подхвачены";
  pill.onclick = () => openModal("🧠 Память в этом ответе",
    `<p class="muted">Подхвачено фрагментов: ${mem.length}. Это не редактируется — просто показывает, что рассказчик «вспомнил» при ответе.</p>` +
    mem.map((m) =>
      `<div class="mem-item"><div class="mem-kind">${m.kind === "Лор" ? "📜 Лор" : m.kind === "⚠" ? "⚠ Память" : "💭 Память"}</div>` +
      `<div class="mem-text">${esc(m.text)}</div></div>`
    ).join(""),
    () => {});
  // ставим перед кнопкой «Озвучить» (если она есть в блоке действий)
  const actionsEl = msgEl.querySelector(".actions");
  const ttsEl = actionsEl && actionsEl.querySelector("[data-tts]");
  if (actionsEl && ttsEl) actionsEl.insertBefore(pill, ttsEl);
  else if (actionsEl) actionsEl.appendChild(pill);
  else msgEl.appendChild(pill);
}

function buildMsg(e) {
  const div = document.createElement("div");
  div.className = `msg ${msgClass(e.role)}`;
  div.dataset.id = e.id || "";
  div.dataset.seq = e.seq || "";
  const ttsBtn = ttsButtonHTML(e);
  const actions = (e.role === "narrator" || e.role === "dice") && e.id
    ? `<div class="actions">
        <button data-fb="1" class="${e.feedback === 1 ? "liked" : ""}">👍</button>
        <button data-fb="-1" class="${e.feedback === -1 ? "disliked" : ""}">👎</button>
        ${e.role === "narrator" ? `<button data-regen title="Перегенерировать ответ">↻</button>` : ""}
        ${ttsBtn}
      </div>`
    : "";
  div.innerHTML = `<div class="who">${esc(WHO[e.role] || e.role || "?")}
    <span class="muted">#${esc(String(e.seq || ""))}</span></div>
    <div class="body">${esc(e.content)}</div>${actions}`;
  // Прозрачность RAG: плашка «🧠 Память» у ответа рассказчика. Данные подхваченных
  // фрагментов хранятся в meta события (переживают перезагрузку/пагинацию/опрос).
  const mem = (e.meta && Array.isArray(e.meta.memory_used) && e.meta.memory_used.length)
    ? e.meta.memory_used : null;
  if (mem && e.role === "narrator") attachMemoryPill(div, mem);
  const ttsEl = div.querySelector("[data-tts]");
  if (ttsEl) {
    const status = +e.tts_status || 0;
    if (status === 1) pollTtsStatus(e.id, ttsEl, false);
    else if (status === 2 || status === 0) ttsEl.onclick = () => playTtsAudio(e.id, ttsEl);
    else ttsEl.onclick = () => retryTts(e.id, ttsEl);
  }
  div.querySelector("[data-regen]")?.addEventListener("click", () => {
    if (state.streaming) return;
    const text = playerTextBefore(div);
    if (text) streamTurn(text, true);
    else alert("Не нашёл действие игрока для перегенерации.");
  });
  div.querySelectorAll("[data-fb]").forEach((btn) => {
    btn.onclick = () => {
      const v = parseInt(btn.dataset.fb);
      API(`/api/worlds/${state.currentWorld}/events/${e.id}/feedback`,
        { method: "POST", body: JSON.stringify({ value: v }) });
      div.querySelectorAll("[data-fb]").forEach((b) =>
        b.className = (b.dataset.fb == v) ? `liked${v == -1 ? " disliked" : ""}` : "");
    };
  });
  return div;
}

function appendMsg(e, scroll = true) {
  // E3 (аудит 38): дедюплицируем ПО ID события, а не по seq. Серверные id уникальны и
  // есть всегда, а seq у локальных (оптимистичных) сообщений пустой — раньше второе
  // идентичное служебное сообщение («Ошибка: …», «Мир создан, но пуст…») считалось
  // «уже нарисованным» и молча терялось, а селектор `.msg[data-seq=""]` был хрупким.
  const key = e.id != null && e.id !== "" ? String(e.id) : `local-${++_localMsgId}`;
  if (e.id != null && e.id !== "") {
    if (document.querySelector(`.msg[data-id="${CSS.escape(key)}"]`)) {
      if (+e.seq > (state.seenSeq || 0)) state.seenSeq = +e.seq;
      return;
    }
  } else {
    e = Object.assign({}, e, { id: key });   // локальному сообщению даём временный id
  }
  const div = buildMsg(e);
  if (+e.seq > (state.seenSeq || 0)) state.seenSeq = +e.seq;
  $("log").appendChild(div);
  if (scroll) $("log").scrollTop = $("log").scrollHeight;
  return div;
}

/* ─── Пагинация лога: подгрузка более ранних событий ─── */
let logPagerBtn = null;
// E3: счётчик id для локально нарисованных (оптимистичных) сообщений
let _localMsgId = 0;

function showLogPager(show) {
  if (!logPagerBtn) {
    logPagerBtn = document.createElement("div");
    logPagerBtn.id = "log-pager";
    logPagerBtn.className = "log-pager";
    logPagerBtn.innerHTML = `<button class="btn small" id="btn-load-earlier">⬆ Показать ранние события</button>`;
    logPagerBtn.querySelector("#btn-load-earlier").onclick = loadEarlier;
  }
  if (show) {
    if (!logPagerBtn.parentNode) $("log").prepend(logPagerBtn);
  } else if (logPagerBtn.parentNode) {
    logPagerBtn.parentNode.removeChild(logPagerBtn);
  }
}

async function loadEarlier() {
  if (!state.currentWorld || state.loadingEarlier) return;
  state.loadingEarlier = true;
  const btn = $("btn-load-earlier");
  if (btn) { btn.disabled = true; btn.textContent = "⏳ Загрузка…"; }
  try {
    const evs = await API(`/api/worlds/${state.currentWorld}/history?before=${state.logMinSeq || 0}&limit=50`);
    const older = evs.filter((e) => e.seq < (state.logMinSeq || Infinity));
    if (!older.length) {
      showLogPager(false);   // больше ранних событий нет
      if (btn) btn.textContent = "⬆ Показать ранние события";
      return;
    }
    const insertBefore = logPagerBtn ? logPagerBtn.nextSibling : $("log").firstChild;
    const prevHeight = $("log").scrollHeight;
    older.forEach((e) => $("log").insertBefore(buildMsg(e), insertBefore));
    state.logMinSeq = older[0].seq;
    // сохраняем пропорциональное положение прокрутки (виден тот же абзац, что и раньше)
    const newHeight = $("log").scrollHeight;
    $("log").scrollTop += newHeight - prevHeight;
    if (btn) btn.textContent = "⬆ Показать ранние события";
  } catch (_) {
    if (btn) btn.textContent = "⬆ Показать ранние события";
  } finally {
    state.loadingEarlier = false;
  }
}

function playerTextBefore(el) {
  /* Последнее действие игрока выше этого сообщения (для кнопки перегенерации). */
  let node = el.previousElementSibling;
  while (node) {
    if (node.classList.contains("msg") && node.classList.contains("player")) {
      const body = node.querySelector(".body");
      const t = body && body.textContent.trim();
      return t || "";
    }
    node = node.previousElementSibling;
  }
  return "";
}

function addTyper() {
  const div = document.createElement("div");
  div.className = "msg narrator typing";
  div.innerHTML = `<div class="who">Рассказчик</div><div class="body"></div>`;
  $("log").appendChild(div);
  $("log").scrollTop = $("log").scrollHeight;
  return div;
}

/* ─────────────── Озвучка (TTS) ─────────────── */
function ttsButtonHTML(e) {
  // Кнопка озвучки у ответов рассказчика (если озвучка включена в мире)
  if (e.role !== "narrator" || !e.id || !state.tts || !state.tts.enabled) return "";
  const s = +e.tts_status || 0;
  if (s === 1) return `<button class="tts tts-pending" data-tts="${e.id}" title="Готовится озвучка…">⟳</button>`;
  if (s === 2) return `<button class="tts" data-tts="${e.id}" title="Озвучить">🔊</button>`;
  if (s === -1) return `<button class="tts tts-err" data-tts="${e.id}" title="Ошибка синтеза — повторить">⚠</button>`;
  return `<button class="tts" data-tts="${e.id}" title="Озвучить">🔊</button>`;
}

function pollTtsStatus(eid, btn, autoPlay) {
  // Опрос фонового синтеза: ⟳ → 🔊 / ⚠  (статус события 1 → 2 | -1)
  let tries = 0;
  btn.classList.add("tts-pending");
  // Один таймер на кнопку (сессия 36, п.16): интервал создавался на каждый вызов, а
  // снимался только внутри колбэка на терминальном статусе. При повторном открытии мира,
  // перезагрузке страницы или автопроигрывании (buildMsg + sendAction зовут pollTtsStatus
  // по одному событию дважды) старые интервалы оставались висеть и долбили /tts/status
  // вечно. Храним id на самой кнопке и снимаем предыдущий перед новым.
  if (btn._ttsTimer) { clearInterval(btn._ttsTimer); btn._ttsTimer = null; }
  const stop = () => { if (btn._ttsTimer) { clearInterval(btn._ttsTimer); btn._ttsTimer = null; } };
  const timer = setInterval(async () => {
    tries++;
    try {
      const r = await API(`/api/worlds/${state.currentWorld}/events/${eid}/tts/status`);
      const s = +r.tts_status;
      if (s === 2) {
        stop();
        btn.classList.remove("tts-pending");
        btn.innerHTML = "🔊";
        btn.title = "Озвучить";
        btn.onclick = () => playTtsAudio(eid, btn);
        if (autoPlay || btn.dataset.autoPlay === "1") setTimeout(() => { btn.onclick && btn.onclick(); }, 300);
      } else if (s === -1) {
        stop();
        btn.classList.remove("tts-pending");
        btn.classList.add("tts-err");
        btn.innerHTML = "⚠";
        btn.title = "Ошибка синтеза — повторить";
        btn.onclick = () => retryTts(eid, btn);
      } else if (tries > 90) {  // ~3 минуты — сдаёмся
        stop();
        btn.classList.remove("tts-pending");
        btn.innerHTML = "🔊";
        btn.title = "Озвучить";
        btn.onclick = () => playTtsAudio(eid, btn);
      }
    } catch (_) {
      btn.classList.remove("tts-pending");
      // мир закрыт/переключён — опрашивать смысла нет: иначе таймер остался бы висеть
      if (state.currentWorld == null || !document.body.contains(btn)) stop();
    }
  }, 2000);
  btn._ttsTimer = timer;
}

async function playTtsAudio(eid, btn) {
  if (state.streaming) return;
  const cur = state.playingTts;
  if (cur) {
    if (cur.eid === eid && !cur.audio.paused) {
      cur.audio.pause();
      document.querySelectorAll(".tts.playing").forEach((b) => b.classList.remove("playing"));
      return;
    }
    try { cur.audio.pause(); } catch (_) {}
    state.playingTts = null;
  }
  btn.classList.add("busy");
  btn.textContent = "…";
  try {
    const resp = await fetch(`/api/worlds/${state.currentWorld}/events/${eid}/audio`);
    if (!resp.ok) throw new Error("аудио ещё не готово");
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    state.playingTts = { eid, audio, url };
    btn.classList.remove("busy");
    btn.innerHTML = "⏸";
    btn.classList.add("playing");
    audio.onended = () => {
      document.querySelectorAll(".tts.playing").forEach((b) => { b.innerHTML = "🔊"; b.classList.remove("playing"); });
      if (state.playingTts && state.playingTts.eid === eid) state.playingTts = null;
      URL.revokeObjectURL(url);
    };
    await audio.play();
  } catch (err) {
    btn.classList.remove("busy");
    btn.innerHTML = "🔊";
    // аудио ещё нет (старое событие / сразу после хода) — запускаем фоновый синтез тихо
    retryTts(eid, btn);
  }
}

async function retryTts(eid, btn) {
  try {
    await API(`/api/worlds/${state.currentWorld}/events/${eid}/tts/retry`, { method: "POST" });
    btn.innerHTML = "⟳";
    btn.classList.add("tts-pending");
    btn.classList.remove("tts-err");
    btn.onclick = null;
    pollTtsStatus(eid, btn, false);
  } catch (err) { alert("Не удалось запустить синтез: " + err.message); }
}

async function loadTtsStatus() {
  try { state.ttsStatus = await API("/api/tts/status"); } catch (_) { state.ttsStatus = null; }
}

function ttsProviderSel() { return $("tts-provider").value; }

function ttsSignedRate() {
  // Edge-сервис Microsoft требует явный знак в скорости («+0%», а не «0%») — иначе «Invalid rate»
  const v = parseInt($("tts-rate").value) || 0;
  return (v >= 0 ? "+" : "") + v + "%";
}

function ttsVoicesFor(provider) {
  // голоса локальные + знакомые; для edge — из реестра
  const st = state.ttsStatus || {};
  const voices = (st.voices || {})[provider === "" ? (state.tts || {}).provider : provider] || [];
  const labels = (st.voice_labels || {})[provider] || {};
  return voices.map((v) => ({ v, label: labels[v] || v }));
}

// Текущий выбранный голос: select (известные) ИЛИ свой голос из доп. поля
function ttsSelectedVoice() {
  if (($("tts-voice") || {}).value === "!custom") return ($("tts-voice-custom") || {}).value?.trim() || "";
  return ($("tts-voice") || {}).value || "";
}

// Показывать ли кнопку «Скачать голос»: только когда есть что скачивать
function updateTtsVoiceToolbar() {
  const btn = $("btn-tts-download");
  const provider = ttsProviderSel() || (state.tts || {}).provider || "edge";
  const st = state.ttsStatus || {};
  const voice = ttsSelectedVoice();
  // «Выключено в этом мире» — настройки голоса не нужны
  const none = provider === "none";
  const vr = $("tts-voice-row"), rr = $("tts-rate-row");
  if (vr) vr.style.display = none ? "none" : "";
  if (rr) rr.style.display = none ? "none" : "";
  let need = false;
  if (provider === "piper") {
    const downloaded = (st.voices || {}).piper || [];
    // кнопка нужна, пока конкретный выбранный голос не скачан (пустой выбор = дефолт, его и скачиваем)
    need = !(voice && downloaded.includes(voice));
  } else if (provider === "kokoro") {
    need = !st.kokoro_ready;
  }
  if (btn) btn.style.display = need ? "" : "none";
}

// Наполняет select голосов: «по умолчанию» + известные голоса провайдера + «свой голос…»
function fillTtsVoiceOptions(provider, saved) {
  const sel = $("tts-voice");
  const custom = $("tts-voice-custom");
  if (!sel) return;
  const opts = ttsVoicesFor(provider);
  sel.innerHTML = `<option value="">По умолчанию (голос провайдера)</option>` +
    opts.map((o) => `<option value="${esc(o.v)}">${esc(o.label)}</option>`).join("") +
    `<option value="!custom">✍️ Свой голос…</option>`;
  const showCustom = saved && saved !== "" && !opts.some((o) => o.v === saved);
  if (showCustom) {
    sel.value = "!custom";
    if (custom) { custom.value = saved; custom.style.display = ""; }
  } else {
    sel.value = saved || "";
    if (custom) custom.style.display = "none";
  }
  // даталист для поля «свой голос» (чтобы не печатать руками)
  const dl = $("tts-voice-list");
  if (dl) dl.innerHTML = opts.map((o) => `<option value="${esc(o.v)}">`).join("");
}

function renderTtsUI() {
  if (!state.tts || !state.ttsStatus) return;
  const t = state.tts;
  $("tts-enabled").checked = !!t.enabled;
  $("tts-provider").value = state.ttsSettingsRaw && state.ttsSettingsRaw.provider !== undefined ? state.ttsSettingsRaw.provider : (t.provider && t.provider !== (state.ttsStatus.global || {}).provider ? t.provider : "");
  const rateNum = parseInt(t.rate) || 0;
  $("tts-rate").value = Math.max(-50, Math.min(50, rateNum));
  $("tts-rate-val").textContent = `${rateNum >= 0 ? "+" : ""}${rateNum}%`;
  $("tts-auto-play").checked = !!t.auto_play;
  // select голосов выбранного провайдера (обновляется сразу, без ручного стирания)
  const prov = ttsProviderSel() || t.provider || "edge";
  const saved = (state.ttsSettingsRaw && state.ttsSettingsRaw.voice) || "";
  fillTtsVoiceOptions(prov, saved);
  updateTtsVoiceToolbar();
  updateTtsStateText();
}

function updateTtsStateText() {
  const st = state.ttsStatus || {};
  const t = state.tts || {};
  const parts = [];
  if (st.libs) {
    if (!st.libs.sherpa_onnx) parts.push("⚠ нет пакета sherpa-onnx (локальные движки)");
    if (!st.libs.edge_tts) parts.push("⚠ нет пакета edge-tts (облачный движок)");
  }
  const pv = (st.voices || {}).piper || [];
  if (t.provider === "piper") {
    if (!pv.length) parts.push("Голос Piper не скачан — нажми «Скачать голос»");
    else parts.push(`Локальные голоса: ${pv.join(", ")} (качество среднее — попробуй ⭐ Edge)`);
  }
  if (t.provider === "kokoro") {
    parts.push(st.kokoro_ready ? "Kokoro установлен" : "Kokoro не установлен (~350 МБ) — нажми «Скачать голос»");
  }
  if (t.provider === "edge") parts.push("Требуется интернет (сервис Microsoft, бесплатно)");
  const el = $("tts-state");
  if (el) el.textContent = parts.join(" · ");
}

async function saveTtsSettings() {
  const payload = {
    provider: $("tts-provider").value,
    voice: ttsSelectedVoice(),
    rate: ttsSignedRate(),
    enabled: $("tts-enabled").checked,
    auto_play: $("tts-auto-play").checked,
  };
  try {
    const res = await API(`/api/worlds/${state.currentWorld}/tts/settings`, {
      method: "POST", body: JSON.stringify(payload),
    });
    state.tts = res.effective;
    state.ttsSettingsRaw = res.overrides;
    alert("Настройки озвучки сохранены");
    loadTtsStatus();
  } catch (e) { alert("Ошибка: " + e.message); }
}

async function testTtsVoice() {
  const btn = $("btn-tts-test");
  btn.disabled = true;
  btn.textContent = "Синтез…";
  try {
    const provider = ttsProviderSel() || (state.tts || {}).provider || "edge";
    const res = await API("/api/tts/test", { method: "POST", body: JSON.stringify({
      provider, voice: ttsSelectedVoice(), rate: ttsSignedRate(),
    }) });
    if (!res.ok) throw new Error(res.error);
    const blob = b64ToBlob(res.data, res.mime);
    const url = URL.createObjectURL(blob);
    const a = new Audio(url);
    a.onended = () => URL.revokeObjectURL(url);
    await a.play();
  } catch (e) { alert("Проверка голоса: " + e.message); }
  btn.disabled = false;
  btn.textContent = "🔊 Проверить голос";
}

function b64ToBlob(b64, mime) {
  const bin = atob(b64);
  const arr = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
  return new Blob([arr], { type: mime });
}

async function downloadTtsVoice() {
  const provider = ttsProviderSel() || (state.tts || {}).provider || "edge";
  if (provider === "none") { alert("Сначала выбери провайдера"); return; }
  if (!confirm(provider === "kokoro"
    ? "Скачать Kokoro-82M (~350 МБ) для локальной озвучки? (без русского — для англ. миров)"
    : "Скачать русский голос Piper (ru_RU-…-medium, ~30–70 МБ)?")) return;
  const btn = $("btn-tts-download");
  btn.disabled = true;
  btn.textContent = "⬇ Скачивание…";
  const st = $("tts-state"); if (st) st.textContent = "Скачивание модели… это может занять пару минут";
  try {
    let voice = ttsSelectedVoice();
    if (provider === "piper" && !voice) voice = state.ttsStatus.default_voices.piper || "ru_RU-ruslan-medium";
    const res = await API("/api/tts/download", { method: "POST", body: JSON.stringify({ provider, voice }) });
    if (!res.ok) throw new Error(res.error);
    if (res.error) throw new Error(res.error);
    const st2 = $("tts-state"); if (st2) st2.textContent = `✅ Голос готов (${res.size_mb} МБ). Сохрани настройки и нажми «Проверить голос».`;
    loadTtsStatus().then(() => { renderTtsUI(); updateTtsVoiceToolbar(); });
  } catch (e) {
    const st2 = $("tts-state"); if (st2) st2.textContent = "❌ " + e.message;
  }
  btn.disabled = false;
  btn.textContent = "⬇ Скачать голос";
}

/* ─────────────── Действие (со стримингом) ─────────────── */
async function streamTurn(text, regenerate) {
  state.streaming = true;
  $("btn-send").disabled = true;
  const typer = addTyper();
  try {
    const resp = await fetch(`/api/worlds/${state.currentWorld}/action/stream`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, regenerate: !!regenerate }),
    });
    if (!resp.ok) throw new Error((await resp.json().catch(() => null))?.detail || resp.statusText);
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    let result = null;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split("\n\n");
      buf = parts.pop();
      for (const part of parts) {
        const lines = part.split("\n");
        const ev = lines.find((l) => l.startsWith("event: "))?.slice(7);
        const data = lines.find((l) => l.startsWith("data: "))?.slice(6);
        if (!data) continue;
        const payload = JSON.parse(data);
        if (ev === "token") {
          typer.querySelector(".body").textContent += payload.text;
          $("log").scrollTop = $("log").scrollHeight;
        } else if (ev === "result") {
          result = payload;
        } else if (ev === "error") {
          throw new Error(payload.error);
        }
      }
    }
    // regenerate=True → сервер не вернул событие игрока (его не дублируем)
    mapGraph = null; mapGraphWorld = null;   // граф изменился за ход — кеш сбросить, карта обновится из эндпоинта
    if (result) handleActionResult(result, typer, !regenerate);
    else typer.remove();
  } catch (err) {
    typer.querySelector(".body").textContent = "⚠ Ошибка: " + err.message;
  }
  state.streaming = false;
  $("btn-send").disabled = false;
  loadSaves();
}

async function sendAction() {
  const input = $("cmd");
  const text = input.value.trim();
  if (!text || state.streaming) return;
  input.value = "";
  appendMsg({ role: "player", content: text, seq: "" });

  const isSlash = text.startsWith("/");
  if (isSlash) {
    try {
      const res = await API(`/api/worlds/${state.currentWorld}/action`,
        { method: "POST", body: JSON.stringify({ text }) });
      handleActionResult(res);
    } catch (err) { appendMsg({ role: "system", content: "Ошибка: " + err.message }); }
    return;
  }
  await streamTurn(text, false);
}

function handleActionResult(res, typerDiv, skipPlayer = false) {
  // Перегенерация: убираем старые сообщения заменённого хода (не копим «копии ответа»)
  if (Array.isArray(res.replaced_events) && res.replaced_events.length) {
    const gone = new Set(res.replaced_events.map(String));
    const log = $("log");
    log.querySelectorAll(".msg").forEach((el) => {
      if (el.dataset.id && gone.has(String(el.dataset.id))) el.remove();
    });
  }
  // синхронизируем состояние и события
  let events = res.events || [];
  if (skipPlayer) events = events.filter((e) => e.role !== "player");  // сообщение игрока уже нарисовано оптимистично
  let rendered = false;
  if (events.length) {
    if (typerDiv) {
      const last = events[events.length - 1];
      if (last && last.role === "narrator") {
        // обновляем типеры: заменяем на полное сообщение
        typerDiv.outerHTML = "";
        events.forEach((e) => appendMsg(e));
        rendered = true;
      }
    }
    if (!rendered) {
      events.forEach((e) => appendMsg(e));
      if (typerDiv) typerDiv.remove();
      rendered = true;
    }
  } else if (res.reply) {
    if (typerDiv) { typerDiv.querySelector(".body").textContent = res.reply; typerDiv.classList.remove("typing"); }
    else appendMsg({ role: "narrator", content: res.reply });
    rendered = true;
  }
  // ── Быстрые действия ──
  // Пустой список от сервера (сбой/таймаут генератора) НЕ стирает прошлые ИИ-предложения —
  // иначе после 2-го ответа кнопки «сбрасываются» на бытовые заготовки. Но и оставлять
  // старые без обновления нельзя: после смены сцены они устаревают и «залипают» (сессия 36,
  // п.15) — раньше асинхронный запрос свежих уходил только если старых предложений тоже не
  // было, что противоречило комментарию выше. Теперь при пустом ответе ВСЕГДА просим
  // сервер пересчитать подсказки по новой сцене; сами кнопки остаются до прихода новых.
  // Страховка от шторма запросов (например, при перегенерации подряд) — within
  // Страховка от шторма запросов (например, при перегенерации подряд) — внутри
  // loadSuggestionRefresh по таймеру последнего запроса к этой сцене.
  if (Array.isArray(res.suggestions) && res.suggestions.length) {
    state.suggestions = res.suggestions.filter(Boolean);
    try { localStorage.setItem(`textgame.suggestions.${state.currentWorld}`, JSON.stringify(state.suggestions)); } catch (_) {}
  } else if (state.currentWorld) {
    loadSuggestionRefresh(state.currentWorld);
  }
  if (res.state) {
    state.setting = res.state;
    renderSetting(res.state);
  }
  // Прозрачность RAG: кнопка «🧠 Память» у последнего ответа рассказчика показывает,
  // какие фрагменты памяти/лора были подхвачены в этом ответе. Сами данные уже сохранены
  // в meta события (buildMsg рисует плашку после перезагрузки тоже), здесь — лишь страховка
  // для живого ответа, если meta ещё не дошла.
  if (Array.isArray(res.memory_used) && res.memory_used.length) {
    const lastNarr = $("log").querySelector(".msg.narrator:last-of-type");
    if (lastNarr && !lastNarr.querySelector(".mem-pill")) attachMemoryPill(lastNarr, res.memory_used);
  }
  if (res.game_over) appendMsg({ role: "system", content: "💀 Мир завершён. Можно загрузить сохранение." });
  // Автопроигрывание озвучки нового ответа рассказчика (если включено в мире)
  if (state.tts && state.tts.auto_play && events.length) {
    const ne = events.filter((e) => e.role === "narrator" && e.id).pop();
    if (ne) {
      const btn = document.querySelector(`[data-tts="${ne.id}"]`);
      if (btn) {
        if (+ne.tts_status === 2) setTimeout(() => { btn.onclick && btn.onclick(); }, 400);
        else if (+ne.tts_status === 1) btn.dataset.autoPlay = "1";   // pollTtsStatus сам проиграет по готовности
      }
    }
  }
  loadEntities();
  return rendered;
}

/* ─────────────── Карточки сущностей ─────────────── */
async function loadEntities() {
  const kind = $("ent-kind-filter")?.value || "";
  const list = $("entities-list");
  if (!list) return;
  const ents = await API(`/api/worlds/${state.currentWorld}/entities?kind=${kind}`);
  const KIND = { npc: "Персонаж", location: "Локация", faction: "Фракция", quest: "Квест", item: "Предмет", event: "Событие",
                 enemy: "Враг", shop: "Магазин", companion: "Компаньон", craft: "Рецепт крафта",
                 race: "Раса", class: "Класс", profession: "Профессия", skill: "Навык", effect: "Эффект" };
  list.innerHTML = ents.map((e) => {
    let meta = {};
    try { meta = JSON.parse(e.meta || "{}"); } catch (_) {}
    return `<div class="entity-card">
      <div class="e-head"><span class="e-name">${esc(e.name)}</span>
        <span class="e-kind">${KIND[e.kind] || e.kind}${meta.alive === false ? " · 🪦" : ""}</span></div>
      ${e.summary ? `<div class="e-sum">${esc(e.summary)}</div>` : ""}
      ${e.relationship ? `<div class="e-rel">💗 ${esc(e.relationship)}</div>` : ""}
      ${e.bio ? `<div class="e-bio">${esc(e.bio)}</div>` : ""}
      <div class="e-actions">
        <button class="btn small" data-eid="${esc(e.entity_key)}" data-kind="${e.kind}" data-act="edit">✎ Править</button>
        <button class="btn small" data-eid="${esc(e.entity_key)}" data-kind="${e.kind}" data-act="del">Удалить</button>
      </div>
    </div>`;
  }).join("") || `<p class="muted">Карточек пока нет — они появятся автоматически по мере игры.</p>`;
  list.querySelectorAll("[data-act]").forEach((b) => {
    b.onclick = () => {
      const k = b.dataset.kind, id = b.dataset.eid;
      if (b.dataset.act === "del" && confirm("Удалить карточку?")) {
        API(`/api/worlds/${state.currentWorld}/entities/${k}/${id}`, { method: "DELETE" }).then(loadEntities);
      } else if (b.dataset.act === "edit") {
        API(`/api/worlds/${state.currentWorld}/entities/${k}/${id}`).then(editEntityModal);
      }
    };
  });
}

function editEntityModal(e) {
  const body = $(e) && e.id
    ? `
    <label>Имя <input id="ee-name" value="${esc(e.name)}"></label>
    <label>Статус (кратко) <textarea id="ee-summary" rows="2">${esc(e.summary || "")}</textarea></label>
    <label>Отношения с игроком <input id="ee-rel" value="${esc(e.relationship || "")}"></label>
    <label>Добавить факт в историю <textarea id="ee-bio" rows="2" placeholder="новый факт…"></textarea></label>
    <label>Meta (JSON) <textarea id="ee-meta" rows="2">${esc(e.meta || "{}")}</textarea></label>`
    : `
    <label>Вид
      <select id="ee-kind">
        <option value="npc">Персонаж</option><option value="location">Локация</option>
        <option value="faction">Фракция</option><option value="quest">Квест</option>
        <option value="item">Предмет</option><option value="event">Событие</option>
        <option value="race">Раса</option><option value="class">Класс</option>
        <option value="profession">Профессия</option><option value="skill">Навык</option>
        <option value="effect">Эффект</option>
      </select></label>
    <label>Ключ (латиницей) <input id="ee-key" placeholder="barman"></label>
    <label>Имя <input id="ee-name"></label>
    <label>Статус <textarea id="ee-summary" rows="2"></textarea></label>
    <label>Отношения <input id="ee-rel"></label>
    <label>Факт/описание <textarea id="ee-bio" rows="3"></textarea></label>
    <label>Meta (JSON) <textarea id="ee-meta" rows="2">{}</textarea></label>`;
  openModal(e.id ? "Правка карточки: " + e.name : "Новая карточка", body, async () => {
    let meta = {};
    try { meta = JSON.parse($("ee-meta").value || "{}"); } catch (_) { alert("meta: не JSON"); throw new Error("meta"); }
    const payload = {
      kind: e.id ? e.kind : $("ee-kind").value,
      key: e.id ? e.entity_key : $("ee-key").value.trim(),
      name: $("ee-name").value.trim(),
      summary: $("ee-summary").value.trim(),
      relationship: $("ee-rel").value.trim(),
      bio_add: $("ee-bio").value.trim() || null,
      meta,
    };
    const path = e.id
      ? `/api/worlds/${state.currentWorld}/entities/${e.kind}/${e.entity_key}`
      : `/api/worlds/${state.currentWorld}/entities`;
    await API(path, { method: e.id ? "PATCH" : "POST", body: JSON.stringify(payload) });
    loadEntities();
  });
}

/* ─────────────── Лор мира (библия) ─────────────── */
async function loadLore() {
  if (!state.currentWorld) return;
  const list = $("lore-list");
  if (!list) return;
  let lore = [];
  try { lore = await API(`/api/worlds/${state.currentWorld}/lore`); } catch (_) {}
  list.innerHTML = lore.map((e) => {
    const size = Math.max(0.1, Math.round(String(e.content || "").length / 100) / 10);
    const srcMeta = [];
    if (e.is_core) srcMeta.push("⭐ ядро");
    if (e.source === "theme") srcMeta.push("тема");
    else if (e.source === "custom") srcMeta.push("свой сюжет");
    else srcMeta.push("своё");
    return `<div class="entity-card">
      <div class="e-head"><span class="e-name" title="${esc(e.title)}">${esc(e.title)}</span>
        <span class="e-meta"><span class="lore-size">${size}k</span><span class="e-kind">${srcMeta.join(" · ")}</span></span></div>
      ${e.tags ? `<div class="e-rel">🏷 ${esc(e.tags)}</div>` : ""}
      <div class="e-bio">${esc(String(e.content || "").slice(0, 260))}${(e.content || "").length > 260 ? "…" : ""}</div>
      <div class="e-actions">
        <button class="btn small" data-lid="${e.id}" data-act="edit">✎ Править</button>
        <button class="btn small danger" data-lid="${e.id}" data-act="del">🗑</button>
      </div>
    </div>`;
  }).join("") || `<p class="muted">Лора пока нет. Он нужен, чтобы рассказчик не выдумывал канон мира. Добавь статью или дай лор при создании мира.</p>`;
  list.querySelectorAll("[data-act]").forEach((b) => {
    b.onclick = async () => {
      const id = +b.dataset.lid;
      if (b.dataset.act === "del") {
        if (!confirm("Удалить статью лора?")) return;
        await API(`/api/worlds/${state.currentWorld}/lore/${id}`, { method: "DELETE" });
        loadLore();
      } else if (b.dataset.act === "edit") {
        editLoreModal(lore.find((x) => x.id === id) || null);
      }
    };
  });
}

function editLoreModal(e) {
  const body = e
    ? `<label>Заголовок <input id="le-title" value="${esc(e.title)}"></label>
       <label>Теги (через запятую) <input id="le-tags" value="${esc(e.tags || "")}" placeholder="география, магия, фракции"></label>
       <label class="check"><input type="checkbox" id="le-core" ${e.is_core ? "checked" : ""}> ⭐ Якорная статья — всегда даётся рассказчику (сжато)</label>
       <label>Текст лора <textarea id="le-content" rows="10">${esc(e.content || "")}</textarea></label>`
    : `<label>Заголовок <input id="le-title" placeholder="География мира"></label>
       <label>Теги (через запятую) <input id="le-tags" placeholder="география, магия, фракции"></label>
       <label class="check"><input type="checkbox" id="le-core"> ⭐ Якорная статья — всегда даётся рассказчику (сжато)</label>
       <label>Текст лора <textarea id="le-content" rows="10" placeholder="История, системы, имена, правила мира… Может быть очень длинным — в промпт попадёт релевантное через RAG."></textarea></label>`;
  openModal(e ? "Правка статьи лора" : "Новая статья лора", body, async () => {
    const title = $("le-title").value.trim();
    const content = $("le-content").value.trim();
    if (!title || !content) { alert("Заголовок и текст обязательны"); throw new Error("empty"); }
    const payload = { title, content, tags: $("le-tags").value.trim(), is_core: $("le-core").checked };
    const path = e
      ? `/api/worlds/${state.currentWorld}/lore/${e.id}`
      : `/api/worlds/${state.currentWorld}/lore`;
    await API(path, { method: e ? "PATCH" : "POST", body: JSON.stringify(payload) });
    loadLore();
  });
}

async function searchLore() {
  const q = ($("lore-search").value || "").trim();
  if (!q) return loadLore();
  const list = $("lore-list");
  const res = await API(`/api/worlds/${state.currentWorld}/lore/search?q=${encodeURIComponent(q)}`);
  list.innerHTML = (res.chunks || []).map((c) =>
    `<div class="entity-card"><div class="e-bio">${esc(c)}</div></div>`
  ).join("") || `<p class="muted">Ничего не найдено. Проверь, что лор добавлен и эмбеддинги включены.</p>`;
}

/* ─────────────── Сохранения ─────────────── */
async function loadSaves() {
  if (!state.currentWorld) return;
  const list = $("saves-list");
  const saves = await API(`/api/worlds/${state.currentWorld}/saves`);
  const auto = saves.find((s) => s.name === "auto");
  const rest = saves.filter((s) => s.name !== "auto");
  let html = "";
  if (auto) {
    html += `<li class="save-item auto-save">
      <div>⏱ <b>Автосохранение</b> <span class="muted">ход #${auto.seq} (после каждого хода)</span></div>
      <div class="e-actions">
        <button class="btn small" data-id="${auto.id}" data-act="load">Восстановить</button>
      </div>
    </li>`;
  }
  html += rest.map((s) =>
    `<li class="save-item" draggable="true" data-save-id="${s.id}">
      <div>💾 <b>${esc(s.name)}</b> <span class="muted">ход #${s.seq}</span></div>
      <div class="e-actions">
        <button class="btn small" data-id="${s.id}" data-act="load">Загрузить</button>
        <button class="btn small danger" data-id="${s.id}" data-act="del">🗑</button>
      </div>
    </li>`).join("");
  list.innerHTML = html || "<li>Сохранений нет</li>";
  // ── Drag-and-Drop сохранений (сессия 30): перетащи слот на 🗑 (или в область-корзину) — удалить;
  // перетащи слот на другой слот — предложить загрузить его (наложение = перезапись не нужна).
  bindSaveDnD(list);
  list.querySelectorAll("[data-act]").forEach((b) => {
    b.onclick = async () => {
      const id = b.dataset.id;
      if (b.dataset.act === "del") {
        await API(`/api/worlds/${state.currentWorld}/saves/${id}`, { method: "DELETE" });
        loadSaves();
      } else if (b.dataset.act === "load") {
        await loadSaveSlot(id);
      }
    };
  });
}

/* Drag-and-Drop слотов сохранений: перетащить слот на 🗑/область-корзину = удалить.
   Чистый DOM, без backend-изменений; dragstart/dragover/drop на элементе списка. */
function bindSaveDnD(list) {
  if (!list) return;
  let dragId = null;
  const trashZone = (() => {
    // создаём видимую зону-корзину под списком (если ещё нет)
    let z = document.getElementById("save-trash-zone");
    if (!z) {
      z = document.createElement("div");
      z.id = "save-trash-zone";
      z.className = "save-trash-zone";
      z.textContent = "🗑 Перетащи сюда, чтобы удалить сохранение";
      list.parentNode.appendChild(z);
    }
    return z;
  })();

  list.querySelectorAll(".save-item").forEach((li) => {
    li.addEventListener("dragstart", (e) => {
      dragId = li.dataset.saveId;
      li.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      try { e.dataTransfer.setData("text/plain", String(dragId)); } catch (_) {}
    });
    li.addEventListener("dragend", () => {
      dragId = null;
      li.classList.remove("dragging");
      trashZone.classList.remove("over");
    });
  });

  trashZone.addEventListener("dragover", (e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    trashZone.classList.add("over");
  });
  trashZone.addEventListener("dragleave", () => trashZone.classList.remove("over"));
  trashZone.addEventListener("drop", async (e) => {
    e.preventDefault();
    trashZone.classList.remove("over");
    const id = dragId || e.dataTransfer.getData("text/plain");
    if (!id || !state.currentWorld) return;
    if (confirm("Удалить это сохранение?")) {
      await API(`/api/worlds/${state.currentWorld}/saves/${id}`, { method: "DELETE" });
      loadSaves();
    }
  });
}

async function loadSaveSlot(id) {
  const res = await API(`/api/worlds/${state.currentWorld}/saves/${id}/load`, { method: "POST" });
  state.setting = res.setting;
  renderSetting(res.setting);
  const d = await API(`/api/worlds/${state.currentWorld}`);
  $("log").innerHTML = "";
  d.recent.forEach((e) => appendMsg(e, false));
  state.seenSeq = 0;
  d.recent.forEach((e) => { if (e.seq) state.seenSeq = Math.max(state.seenSeq, +e.seq); });
  requestAnimationFrame(() => { $("log").scrollTop = $("log").scrollHeight; });
  loadEntities();
}

/* ─────────────── Настройки ─────────────── */
function syncGenUI() {
  if (!state.gen) return;
  $("set-temp").value = state.gen.temperature ?? 0.8;
  $("set-topp").value = state.gen.top_p ?? 0.95;
  $("set-mtok").value = state.gen.max_tokens ?? 2000;
  $("set-ctx").value = state.gen.context_tokens ?? 32768;
  $("set-logic-judge").checked = state.gen.logic_judge !== false; // по умолчанию вкл
  const md = state.memoryDefaults || {};
  const ragk = state.gen.rag_memory_k ?? 0;
  $("set-ragk").value = ragk;
  $("set-ragk").max = md.rag_memory_max || 64;
  $("set-lorek").value = state.gen.lore_rag_k ?? 0;
  $("set-lorek").max = md.lore_rag_k_max || 30;
  updateGenUI();
}

function updateGenUI() {
  $("temp-val").textContent = (+$("set-temp").value).toFixed(2);
  $("topp-val").textContent = (+$("set-topp").value).toFixed(2);
  const ctx = +$("set-ctx").value;
  const mtok = +$("set-mtok").value;
  $("ctx-val").textContent = ctx.toLocaleString("ru-RU");
  $("mtok-val").textContent = mtok;
  $("ctx-budget-val").textContent = Math.max(400, ctx - (2600 + mtok) - Math.max(512, Math.min(16384, Math.floor(ctx * 0.2)))).toLocaleString("ru-RU");
  // Реальный лимит контекста модели (авто-детект). Если окно мира выше — сервер сам
  // снизит его при сохранении, поэтому предупреждаем ещё до «применить».
  const note = $("ctx-limit-note");
  if (note) {
    const lim = +(state.gen && state.gen.context_limit) || 0;
    if (lim > 0) {
      const cap = Math.floor(lim * 0.95);
      note.hidden = false;
      note.innerHTML = ctx > cap
        ? `⚠ Модель отдаёт не больше <b>${lim.toLocaleString("ru-RU")}</b> токенов — при сохранении окно
           мира снизится до <b>${cap.toLocaleString("ru-RU")}</b>, иначе ответы будут обрезаться.`
        : `Реальный лимит модели: <b>${lim.toLocaleString("ru-RU")}</b> токенов
           (${(state.gen.context_limit_source || "авто-детект")}). Окно мира в него вписывается.`;
      $("set-ctx").max = Math.max(1024, lim);
    } else {
      note.hidden = true;
      note.textContent = "";
    }
  }
  const ragk = +$("set-ragk").value;
  $("ragk-val").textContent = ragk === 0 ? "авто" : ragk;
  const lorek = +$("set-lorek").value;
  $("lorek-val").textContent = lorek === 0 ? "авто" : lorek;
}

async function saveProviders() {
  const payload = {};
  PROVIDER_KINDS.forEach((kind) => {
    payload[kind] = collectProviderPayload(kind);
  });
  const rerankId = payload.rerank.id;
  payload.rerank.enabled = rerankId === "none"
    ? false
    : $("set-rerank-enabled").checked && state.rerankGlobal;
  try {
    const res = await API(`/api/worlds/${state.currentWorld}/providers`, {
      method: "POST", body: JSON.stringify(payload),
    });
    // E5: сырой снимок provider_settings не храним (там были живые ключи); для UI хватает
    // замаскированного providers_effective, который сервер вернул в том же ответе.
    state.providersEffective = res.providers_effective || null;
    syncProvidersUI();
    // сессия 36, п.19: сервер может принять настройки и предупредить, что модель не
    // отвечает. Молча сохранить «ok» — значит оставить игрока один на один с будущей
    // лавиной ошибок хода, поэтому предупреждение показываем сразу.
    const warns = Array.isArray(res.warnings) ? res.warnings.filter(Boolean) : [];
    alert(warns.length
      ? "Провайдеры сохранены, но:\n\n" + warns.join("\n\n")
      : "Провайдеры сохранены");
  } catch (e) { alert("Ошибка: " + e.message); }
}

function bindRange(id, outId, fmt) {
  const set = () => { $(outId).textContent = fmt($(id).value); };
  set(); // показать текущее значение сразу, а не только после движения ползунка
  $(id).addEventListener("input", set);
}

/* ─────────────── Модалка ─────────────── */
function openModal(title, bodyHTML, onOk) {
  $("modal-title").textContent = title;
  $("modal-body").innerHTML = bodyHTML;
  $("modal").style.display = "flex";
  $("modal-ok").onclick = async () => {
    try { await onOk(); closeModal(); } catch (e) { /* ошибка уже показана */ }
  };
  $("modal-cancel").onclick = closeModal;
}
function closeModal() { $("modal").style.display = "none"; }

/* ─────────────── Утилиты ─────────────── */
function esc(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// Значение для HTML-атрибута data-arg="…" (E1, хвосты сессии 38). Ключ сущности приходит
// от LLM и может содержать апостроф («don't»), кавычку, перевод строки. Раньше такие ключи
// вставлялись прямо в JS-литерал внутри onclick="showQuest('…')" и требовали двойного
// экранирования (jsAttr, сессия 36, п.25). Теперь JS-литералов в разметке нет: обработчик
// вешается делегатом ниже, а из атрибута читается ГОТАЯ СТРОКА — значит достаточно
// экранирования самого HTML-атрибута (esc). Браузер сам раскодировит сущности обратно.
function attrArg(s) {
  return esc(String(s ?? "").replace(/\r/g, ""));
}

// Число для JS-аргумента в атрибуте: гарантируем, что туда не попадёт не-число.
function numAttr(v) {
  const n = Number(v);
  return Number.isFinite(n) ? String(n) : "0";
}

/* ─────────── E1: единый делегат кликов по карточкам состояния ───────────
   Вместо 15 `onclick="fn('${jsAttr(k)}')"`: разметка несёт только имя действия и аргумент,
   а вызов живёт здесь. Новый обработчик больше не может забыть про экранирование —
   ему нечего экранировать, рисковый класс снят целиком (законы нейтральны: только UI).
   Реестр замыкается на function-декларации (они всплывают), поэтому объявлен здесь. */
const CLICK_ACTIONS = {
  scrollToSeq, showFaction, showSkill, showAbility, showEffect, showItem, showFlag,
  showQuest, showShop, showCraft, showEnemy, showNpc, showCompanion,
};
// Ключи словарей мира (квест/магазин/рецепт/враг/NPC/спутник) — СТРОКИ, даже если LLM
// дала id «123»; индексы списков и номер хода — числа. Разделяем явно, вместо прежнего
// «на глаз» в шаблоне (numAttr для чисел, jsAttr для строк).
const CLICK_NUMERIC = new Set(["scrollToSeq", "showFaction", "showSkill", "showAbility",
                               "showEffect", "showItem", "showFlag"]);
document.addEventListener("click", (e) => {
  const t = e.target;
  const el = t && t.closest ? t.closest("[data-click]") : null;
  if (!el) return;
  const name = el.dataset.click || "";
  const fn = CLICK_ACTIONS[name];
  if (!fn) { console.warn("неизвестный data-click:", name); return; }
  const raw = el.dataset.arg;
  if (raw === undefined) { fn(); return; }
  fn(CLICK_NUMERIC.has(name) ? Number(raw) : raw);
});

async function refreshStatus() {
  const el = $("sys-status");
  try {
    const s = await API("/api/system/status");
    const parts = [];
    if (s.llm.up) parts.push(`🧠 LLM ✓ (${s.llm.base_url.replace("http://", "")})`);
    else parts.push("🧠 LLM ✗");
    if (s.chroma.up) parts.push(`🗄 Память ✓ (${s.chroma.chunks} чанков)`);
    else parts.push("🗄 Память ✗ (запусти start_chroma.bat)");
    el.textContent = parts.join(" · ");
    el.className = "status-pill " + (s.llm.up && s.chroma.up ? "ok" : "warn");
  } catch (_) { el.textContent = "сервер недоступен"; el.className = "status-pill warn"; }
}

/* ─────────────── События ─────────────── */
document.addEventListener("DOMContentLoaded", () => {
  refreshStatus();
  loadThemes().catch((e) => console.error(e));
  loadGenres().catch((e) => console.error(e));
  loadPlots().catch((e) => console.error(e));
  loadWorlds().catch((e) => console.error(e));
  loadNarrators().catch((e) => console.error(e));

  $("btn-create-world").onclick = createWorld;
  const rbtn = $("btn-reload-plots"); if (rbtn) rbtn.onclick = reloadPlots;
  $("btn-add-plot").onclick = () => plotModal(null);
  $("btn-menu").onclick = goMenu;
  // E6 (хвосты 38): на телефоне панель мира — выдвижная; переключатель и закрытие по фону.
  const sg = $("screen-game");
  const sideToggle = $("btn-sidebar");
  const setSidebar = (open) => {
    sg.classList.toggle("side-open", open);
    if (sideToggle) sideToggle.setAttribute("aria-expanded", open ? "true" : "false");
  };
  if (sideToggle) sideToggle.onclick = () => setSidebar(!sg.classList.contains("side-open"));
  const scrim = $("sidebar-scrim");
  if (scrim) scrim.onclick = () => setSidebar(false);
  // клик по карточке состояния (открытая модалка/панель) на узком экране закрывает панель
  document.addEventListener("click", (e) => {
    if (window.innerWidth > 900) return;
    if (!sg.classList.contains("side-open")) return;
    const t = e.target;
    if (t && t.closest && (t.closest(".sidebar") || t.closest("#btn-sidebar"))) return;
    setSidebar(false);
  }, true);
  //Esc всегда убирает панель — на телефоне это единственный способ вернуться в чат,
  //когда панель открылась случайно (например после перехода на вкладку «Состояние»)
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && sg.classList.contains("side-open")) setSidebar(false);
  });

  $("btn-send").onclick = sendAction;
  $("cmd").addEventListener("keydown", (e) => { if (e.key === "Enter") sendAction(); });
  $("btn-divine").onclick = divineModal;

  $("btn-hint").onclick = () => { $("cmd").value = "/hint"; sendAction(); };
  // C1: перемотка к ходу (сессия 34)
  $("btn-rewind").onclick = openRewindModal;
  // C7: справка «мои средства» (сессия 34)
  $("btn-risk").onclick = openRiskModal;
  // C2: заметка игрока в дневник
  $("btn-jr-note").onclick = async () => {
    const note = ($("jr-note") || {}).value;
    if (!note || !note.trim()) return;
    $("cmd").value = `/journal note ${note.trim()}`;
    sendAction();
    if ($("jr-note")) $("jr-note").value = "";
  };
  if ($("jr-cat")) $("jr-cat").onchange = loadJournal;
  if ($("jr-note")) $("jr-note").addEventListener("keydown", (e) => { if (e.key === "Enter") $("btn-jr-note").click(); });
  $("btn-export").onclick = async () => {
    const txt = await fetch(`/api/worlds/${state.currentWorld}/export`).then((r) => r.text());
    const blob = new Blob([txt], { type: "text/plain;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `world-${state.currentWorld}-history.txt`;
    a.click();
  };
  // Полный JSON-дамп мира (состояние+события+карточки+лор+слоты+граф) — бэкап/перенос
  $("btn-export-json").onclick = async () => {
    try {
      const r = await fetch(`/api/worlds/${state.currentWorld}/export/json?download=1`);
      if (!r.ok) throw new Error((await r.json().catch(() => null))?.detail || r.statusText);
      const blob = await r.blob();
      const cd = r.headers.get("content-disposition") || "";
      const m = cd.match(/filename=\"([^\"]+)\"/);
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = m ? m[1] : `world-${state.currentWorld}-dump.json`;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) { alert("Не удалось выгрузить дамп: " + e.message); }
  };
  // Импорт: создаёт НОВЫЙ мир из JSON-дампа (ничего не затирает)
  $("btn-import-json").onclick = () => $("import-json-file").click();
  $("import-json-file").onchange = async (ev) => {
    const file = ev.target.files && ev.target.files[0];
    if (!file) return;
    try {
      const payload = JSON.parse(await file.text());
      if (!payload || payload.format !== "textgame.world.dump") {
        throw new Error("файл не похож на дамп мира (нет поля format)");
      }
      const res = await API("/api/worlds/import/json", { method: "POST", body: JSON.stringify({ payload }) });
      alert(`Мир «${res.world.name}» создан (id ${res.world_id}). Память перестраивается в фоне.`);
      await loadWorlds();
      openWorld(res.world_id);
    } catch (e) { alert("Импорт не удался: " + e.message); }
    ev.target.value = "";
  };
  $("btn-delete-world").onclick = async () => {
    if (confirm("Удалить мир и всю его историю?")) {
      await API(`/api/worlds/${state.currentWorld}`, { method: "DELETE" });
      location.reload();
    }
  };

  // вкладки
  document.querySelectorAll(".tab").forEach((t) => {
    t.onclick = () => {
      document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
      t.classList.add("active");
      document.querySelectorAll(".tab-panels > .panel-body").forEach((p) => p.style.display = "none");
      $("tab-" + t.dataset.tab).style.display = "";
      if (t.dataset.tab === "map") renderMap();
      if (t.dataset.tab === "entities") loadEntities();
      if (t.dataset.tab === "lore") loadLore();
      if (t.dataset.tab === "saves") loadSaves();
      if (t.dataset.tab === "journal") loadJournal();
      // вкладки могут гулять по горизонтали (скролл) — подсвеченную вкладку делаем видимой
      t.scrollIntoView({ inline: "start", block: "nearest" });
    };
  });

  // настройки
  bindRange("set-temp", "temp-val", (v) => (+v).toFixed(2));
  bindRange("set-topp", "topp-val", (v) => (+v).toFixed(2));
  bindRange("set-mtok", "mtok-val", (v) => v);
  bindRange("set-ctx", "ctx-val", (v) => (+v).toLocaleString("ru-RU"));
  bindRange("set-ragk", "ragk-val", (v) => (+v === 0 ? "авто" : v));
  bindRange("set-lorek", "lorek-val", (v) => (+v === 0 ? "авто" : v));
  ["set-mtok", "set-ctx", "set-ragk", "set-lorek"].forEach((id) => $(id).addEventListener("input", updateGenUI));
  // сворачиваемые секции настроек: состояние открытости запоминается между визитами
  document.querySelectorAll("#tab-settings details.settings-section").forEach((d) => {
    const key = "textgame.sect." + (d.querySelector(".settings-head")?.textContent || "").trim();
    try {
      const saved = localStorage.getItem(key);
      if (saved !== null) d.open = saved === "1";
    } catch (_) {}
    d.addEventListener("toggle", () => {
      try { localStorage.setItem(key, d.open ? "1" : "0"); } catch (_) {}
    });
  });
  $("btn-save-settings").onclick = async () => {
    await API(`/api/worlds/${state.currentWorld}/settings`, {
      method: "POST",
      body: JSON.stringify({ temperature: +$("set-temp").value, top_p: +$("set-topp").value,
                            max_tokens: +$("set-mtok").value, context_tokens: +$("set-ctx").value,
                            logic_judge: $("set-logic-judge").checked,
                            rag_memory_k: +$("set-ragk").value,
                            lore_rag_k: +$("set-lorek").value }),
    });
    // обновляем локально сохранённые gen_settings (без перезагрузки)
    state.gen = Object.assign({}, state.gen, {
      temperature: +$("set-temp").value, top_p: +$("set-topp").value,
      max_tokens: +$("set-mtok").value, context_tokens: +$("set-ctx").value,
      logic_judge: $("set-logic-judge").checked,
      rag_memory_k: +$("set-ragk").value, lore_rag_k: +$("set-lorek").value,
    });
    alert("Настройки сохранены");
  };

  // рассказчик
  $("btn-narrators").onclick = narratorsModal;

  // провайдеры
  $("btn-save-providers").onclick = saveProviders;

  // озвучка (TTS)
  $("btn-save-tts").onclick = saveTtsSettings;
  $("btn-tts-test").onclick = testTtsVoice;
  $("btn-tts-download").onclick = downloadTtsVoice;
  $("tts-provider").addEventListener("change", () => {
    const provider = ttsProviderSel() || (state.tts || {}).provider || "edge";
    const saved = (state.ttsSettingsRaw && state.ttsSettingsRaw.voice) || "";
    // сохраняем выбор, только если голос ЕСТЬ в списке нового провайдера; иначе — «По умолчанию»
    // (не подставляем «свой» с прошлого провайдера и не оставляем чужой голос)
    const keep = saved && ttsVoicesFor(provider).some((o) => o.v === saved) ? saved : "";
    fillTtsVoiceOptions(provider, keep);
    updateTtsVoiceToolbar();
    updateTtsStateText();
  });
  $("tts-voice").addEventListener("change", () => {
    // «Свой голос…» → показать поле ввода, остальное — спрятать
    const custom = $("tts-voice-custom");
    if (custom) custom.style.display = $("tts-voice").value === "!custom" ? "" : "none";
    updateTtsVoiceToolbar();
    updateTtsStateText();
  });
  $("tts-voice-custom").addEventListener("input", updateTtsStateText);
  $("tts-rate").addEventListener("input", () => {
    $("tts-rate-val").textContent = `${$("tts-rate").value >= 0 ? "+" : ""}${$("tts-rate").value}%`;
  });

  // сохранение
  $("btn-save").onclick = async () => {
    const name = $("save-name").value.trim() || `Ход ${new Date().toLocaleTimeString()}`;
    await API(`/api/worlds/${state.currentWorld}/saves`, { method: "POST", body: JSON.stringify({ name }) });
    loadSaves();
  };

  // карточки: фильтр + новая
  $("ent-kind-filter").addEventListener("change", loadEntities);
  $("btn-new-entity").onclick = () => editEntityModal({ id: null });

  // лор: добавление + поиск
  $("btn-lore-add").onclick = () => editLoreModal(null);
  $("lore-search").addEventListener("keydown", (e) => { if (e.key === "Enter") searchLore(); });

  // память
  $("btn-mem-search").onclick = async () => {
    const q = $("mem-search").value.trim();
    if (!q) return;
    const res = await API(`/api/worlds/${state.currentWorld}/memory/search?q=${encodeURIComponent(q)}`);
    $("mem-results").innerHTML = res.results.length
      ? res.results.map((c) => `<div class="entity-card"><div class="e-bio">${esc(c)}</div></div>`).join("")
      : `<p class="muted">Ничего не найдено.</p>`;
  };

  // мастер-патч
  $("btn-master-patch").onclick = async () => {
    try {
      const patch = JSON.parse($("master-patch").value || "{}");
      const res = await API(`/api/worlds/${state.currentWorld}/state/patch`,
        { method: "POST", body: JSON.stringify({ patch }) });
      state.setting = res.setting;
      renderSetting(res.setting);
      appendMsg({ role: "system", content: "🛠 Состояние обновлено мастером." });
    } catch (e) { alert("Ошибка патча: " + e.message); }
  };

  setInterval(refreshStatus, 30000);
});