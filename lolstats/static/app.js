(() => {
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  const statusDot = $("#status-dot");
  const connection = $("#connection");
  const patchLabel = $("#patch-label");
  const notInGame = $("#not-in-game");
  const gameGrid = $("#game-grid");
  const meName = $("#me-name");
  const mePosition = $("#me-position");
  const meSpells = $("#me-spells");
  const meKeystone = $("#me-keystone");
  const adviceSummary = $("#advice-summary");
  const adviceSpike = $("#advice-spike");
  const adviceOpponent = $("#advice-opponent");
  const adviceMeta = $("#advice-meta");
  const bestItems = $("#best-items");
  const counterItems = $("#counter-items");
  const tipsList = $("#tips");
  const enemiesEl = $("#enemies");
  const alliesEl = $("#allies");
  const patchInfoEl = $("#patch-info");
  const keyStatus = $("#key-status");
  const configForm = $("#config-form");
  const configSaved = $("#config-saved");

  function renderPlayer(p) {
    const div = document.createElement("div");
    div.className = "player";
    const spells = (p.summoner_spells || []).filter(Boolean).join(" / ") || "—";
    div.innerHTML = `
      <span class="champ">${p.champion || "?"}</span>
      <span class="pos">${p.position || ""}</span>
      <span class="score">${spells}${p.keystone ? ` · ${p.keystone}` : ""}</span>
    `;
    return div;
  }

  function renderTeam(container, players) {
    container.innerHTML = "";
    (players || []).forEach((p) => container.appendChild(renderPlayer(p)));
    if (!players || players.length === 0) {
      container.innerHTML = `<div class="muted small">No data</div>`;
    }
  }

  function renderMe(snap) {
    const me = snap.me;
    if (!me) {
      meName.textContent = "Spectator / unknown";
      mePosition.textContent = "";
      meSpells.textContent = "";
      meKeystone.textContent = "";
      return;
    }
    meName.textContent = `${me.champion || "?"} — ${me.summoner || ""}`;
    mePosition.textContent = me.position || "";
    meSpells.textContent = (me.summoner_spells || []).filter(Boolean).join(" / ") || "";
    meKeystone.textContent = me.keystone || "";
  }

  function renderAdvice(state) {
    const advice = state?.advice?.advice || null;
    if (!advice || Object.keys(advice).length === 0) {
      adviceSummary.textContent = state?.advice?.error ? `Error: ${state.advice.error}` : "Waiting for Claude…";
      adviceSpike.textContent = "";
      adviceOpponent.textContent = "";
      bestItems.innerHTML = `<li class="muted small">—</li>`;
      counterItems.innerHTML = `<li class="muted small">—</li>`;
      tipsList.innerHTML = `<li class="muted small">—</li>`;
      adviceMeta.textContent = "";
      return;
    }
    adviceSummary.textContent = advice.summary || "—";
    adviceOpponent.textContent = advice.lane_opponent ? `Lane: ${advice.lane_opponent}` : "";
    adviceSpike.textContent = advice.power_spike ? `Spike: ${advice.power_spike}` : "";

    bestItems.innerHTML = "";
    (advice.best_items || []).forEach((it) => {
      const li = document.createElement("li");
      li.innerHTML = `<span class="name">${it.name}</span><span class="reason">${it.reason || ""}</span>`;
      bestItems.appendChild(li);
    });
    if (bestItems.children.length === 0) bestItems.innerHTML = `<li class="muted small">—</li>`;

    counterItems.innerHTML = "";
    (advice.counter_items || []).forEach((it) => {
      const li = document.createElement("li");
      li.classList.add("counter");
      li.innerHTML = `<span class="name">${it.name}</span><span class="against">vs ${it.against}</span><span class="reason">${it.reason || ""}</span>`;
      counterItems.appendChild(li);
    });
    if (counterItems.children.length === 0) counterItems.innerHTML = `<li class="muted small">—</li>`;

    tipsList.innerHTML = "";
    (advice.tips || []).forEach((tip) => {
      const li = document.createElement("li");
      li.textContent = tip;
      tipsList.appendChild(li);
    });
    if (tipsList.children.length === 0) tipsList.innerHTML = `<li class="muted small">—</li>`;

    const ts = state.advice.requested_at ? new Date(state.advice.requested_at * 1000) : null;
    const tsStr = ts ? ts.toLocaleTimeString() : "—";
    adviceMeta.textContent = `${state.advice.model || ""} · patch ${state.advice.patch_version || "?"} · ${tsStr}`;
  }

  let lastSnapshot = null;

  function applyState(state) {
    if (state.snapshot) lastSnapshot = state.snapshot;
    if (state.in_game && state.snapshot) {
      notInGame.classList.add("hidden");
      gameGrid.classList.remove("hidden");
      const snap = state.snapshot;
      renderMe(snap);
      renderTeam(enemiesEl, snap.enemies);
      renderTeam(alliesEl, snap.allies);
      renderAdvice(state);
      statusDot.classList.remove("warn");
      statusDot.classList.add("online");
    } else {
      notInGame.classList.remove("hidden");
      gameGrid.classList.add("hidden");
      statusDot.classList.remove("online");
      statusDot.classList.add("warn");
    }
  }

  // -- websocket --

  let ws = null;
  let reconnectDelay = 1000;

  function connect() {
    const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`;
    ws = new WebSocket(url);
    ws.onopen = () => {
      connection.textContent = "live";
      connection.classList.add("connected");
      reconnectDelay = 1000;
    };
    ws.onclose = () => {
      connection.textContent = "disconnected";
      connection.classList.remove("connected");
      setTimeout(connect, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 15000);
    };
    ws.onerror = () => { try { ws.close(); } catch (_) {} };
    ws.onmessage = (e) => {
      let msg;
      try { msg = JSON.parse(e.data); } catch { return; }
      if (msg.type === "state") {
        applyState(msg.data);
      } else if (msg.type === "advice") {
        applyState({
          in_game: true,
          snapshot: lastSnapshot,
          advice: msg.data,
        });
      }
    };
  }

  // -- tabs --

  $$(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$(".tab").forEach((b) => b.classList.remove("active"));
      $$(".tab-panel").forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      const tabId = `tab-${btn.dataset.tab}`;
      const panel = document.getElementById(tabId);
      if (panel) panel.classList.add("active");
      if (btn.dataset.tab === "config") {
        loadConfig();
      } else if (distillPoll) {
        clearInterval(distillPoll);
        distillPoll = null;
      }
      if (btn.dataset.tab === "pregame") loadChampions();
    });
  });

  // -- config --

  async function loadConfig() {
    const res = await fetch("/api/config");
    const cfg = await res.json();
    keyStatus.textContent = cfg.anthropic_api_key_set ? `set (${cfg.anthropic_api_key})` : "not set";
    for (const [k, v] of Object.entries(cfg)) {
      const el = configForm.elements[k];
      if (!el) continue;
      if (el.type === "checkbox") el.checked = !!v;
      else if (el.type === "password") el.value = "";
      else el.value = v ?? "";
    }
    const patchRes = await fetch("/api/patch");
    const patch = await patchRes.json();
    if (patch.version) {
      patchLabel.textContent = `Patch ${patch.version}`;
      const counts = patch.counts || {};
      patchInfoEl.textContent = `Cached patch ${patch.version} (${counts.champions ?? 0} champs, ${counts.items ?? 0} items, ${counts.runes ?? 0} runes, ${counts.summoner_spells ?? 0} spells), locale ${patch.locale || "?"}.`;
    } else {
      patchInfoEl.textContent = "Patch data not loaded yet. Click below to download it now.";
    }

    loadLogs();
    loadDistill();
  }

  function progressBar(pct) {
    const clamped = Math.max(0, Math.min(100, pct || 0));
    let cls = "fill";
    if (clamped >= 100) cls += " done";
    else if (clamped < 100) cls += " partial";
    return `<div class="progress"><div class="${cls}" style="width:${clamped}%"></div></div>`;
  }

  function renderDistill(d) {
    const summary = $("#distill-summary");
    const overall = $("#distill-overall");
    const kinds = $("#distill-kinds");
    if (!summary || !overall || !kinds) return;

    if (!d || !d.version) {
      summary.textContent = "No patch cached yet — download the patch first.";
      overall.innerHTML = "";
      kinds.innerHTML = "";
      return;
    }

    const bits = [`Model: ${d.model}`];
    bits.push(d.enabled ? "enabled" : "disabled");
    if (!d.has_api_key) bits.push("no API key");
    if (d.running) bits.push("running…");
    else if (d.complete) bits.push("complete");
    summary.textContent = bits.join(" · ");

    overall.innerHTML = `
      <div class="progress-label"><span>Overall</span><span>${d.done} / ${d.eligible} (${d.pct}%)</span></div>
      ${progressBar(d.pct)}
    `;

    const order = ["item", "champion", "rune", "spell"];
    kinds.innerHTML = "";
    order.forEach((k) => {
      const row = d.kinds && d.kinds[k];
      if (!row) return;
      const div = document.createElement("div");
      div.className = "progress-row";
      div.innerHTML = `
        <div class="progress-label"><span>${row.label}</span><span>${row.done} / ${row.eligible} (${row.pct}%)</span></div>
        ${progressBar(row.pct)}
      `;
      kinds.appendChild(div);
    });
  }

  let distillPoll = null;

  async function loadDistill() {
    const summary = $("#distill-summary");
    try {
      const r = await fetch("/api/distill");
      if (!r.ok) {
        if (summary) summary.textContent = `Couldn't load status (HTTP ${r.status}).`;
        return;
      }
      const d = await r.json();
      renderDistill(d);
      // Auto-poll while a run is in progress; stop once it settles.
      const onConfigTab = document.getElementById("tab-config")?.classList.contains("active");
      if (d.running && onConfigTab && !distillPoll) {
        distillPoll = setInterval(loadDistill, 2500);
      } else if (!d.running && distillPoll) {
        clearInterval(distillPoll);
        distillPoll = null;
      }
    } catch (e) {
      // ignore
    }
  }

  function fmtBytes(n) {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1024 / 1024).toFixed(1)} MB`;
  }

  async function loadLogs() {
    try {
      const r = await fetch("/api/logs");
      const data = await r.json();
      const dirEl = document.getElementById("logs-dir");
      const filesEl = document.getElementById("logs-files");
      if (dirEl) dirEl.innerHTML = `Logs folder: <code>${data.logs_dir}</code>`;
      if (filesEl) {
        filesEl.innerHTML = "";
        const list = document.createElement("div");
        list.className = "log-files";
        (data.files || []).forEach((f) => {
          const a = document.createElement("a");
          a.href = `/api/logs/file/${encodeURIComponent(f.name)}`;
          a.download = f.name;
          a.innerHTML = `${f.name}<span class="size">${fmtBytes(f.size_bytes)}</span>`;
          list.appendChild(a);
        });
        if (!list.children.length) {
          list.innerHTML = `<span class="muted small">No log files yet — they'll appear after the server runs for a bit.</span>`;
        }
        filesEl.appendChild(list);
      }
      const tailEl = document.getElementById("logs-tail");
      if (tailEl) {
        const tr = await fetch("/api/logs/tail?lines=200");
        const td = await tr.json();
        tailEl.textContent = (td.lines || []).join("\n");
      }
    } catch (e) {
      if (summary) summary.textContent = "Couldn't load distillation status.";
    }
  }

  // -- pre-game (runes / spells / starting items) --

  let championsLoaded = false;

  async function loadChampions() {
    if (championsLoaded) return;
    const sel = $("#pregame-champion");
    if (!sel) return;
    try {
      const r = await fetch("/api/champions");
      const data = await r.json();
      const names = data.champions || [];
      if (!names.length) {
        sel.innerHTML = `<option value="">No champions cached yet</option>`;
        return;
      }
      sel.innerHTML = names.map((n) => `<option value="${n}">${n}</option>`).join("");
      championsLoaded = true;
    } catch (e) {
      sel.innerHTML = `<option value="">Failed to load champions</option>`;
    }
  }

  function renderPregame(data) {
    const title = $("#pregame-title");
    const meta = $("#pregame-meta");
    const summary = $("#pregame-summary");
    const groups = $("#pregame-groups");
    const notes = $("#pregame-notes");
    const advice = data && data.advice;
    if (!advice || !Object.keys(advice).length) {
      summary.textContent = data && data.error ? `Error: ${data.error}` : "No answer returned.";
      groups.innerHTML = "";
      notes.innerHTML = "";
      meta.textContent = "";
      return;
    }
    const topicLabel = { runes: "Runes", summoner_spells: "Summoner spells", starting_items: "Starting items" }[data.topic] || "Setup";
    title.textContent = `${topicLabel}: ${data.champion}${data.role ? " · " + data.role : ""}`;
    summary.textContent = advice.summary || "—";

    groups.innerHTML = "";
    (advice.groups || []).forEach((g) => {
      const div = document.createElement("div");
      div.className = "li-block";
      const picks = (g.picks || []).join(" · ");
      div.innerHTML = `<span class="name">${g.label || ""}</span>` +
        `<span class="picks">${picks}</span>` +
        (g.reason ? `<span class="reason">${g.reason}</span>` : "");
      groups.appendChild(div);
    });
    if (!groups.children.length) groups.innerHTML = `<div class="muted small">—</div>`;

    notes.innerHTML = "";
    (advice.notes || []).forEach((n) => {
      const li = document.createElement("li");
      li.textContent = n;
      notes.appendChild(li);
    });

    const cachedStr = data.cached ? "cached" : "fresh";
    meta.textContent = `${data.model || ""} · patch ${data.patch_version || "?"} · ${cachedStr}`;
  }

  const pregameForm = $("#pregame-form");
  if (pregameForm) {
    pregameForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const champion = pregameForm.elements["champion"].value;
      const role = pregameForm.elements["role"].value;
      const topic = pregameForm.elements["topic"].value;
      const summary = $("#pregame-summary");
      const btn = $("#pregame-ask");
      if (!champion) {
        summary.textContent = "Pick a champion first.";
        return;
      }
      $("#pregame-groups").innerHTML = "";
      $("#pregame-notes").innerHTML = "";
      $("#pregame-meta").textContent = "";
      summary.textContent = "Asking Claude…";
      btn.disabled = true;
      try {
        const r = await fetch("/api/pregame", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ champion, role, topic }),
        });
        const body = await r.json().catch(() => ({}));
        if (!r.ok) {
          summary.textContent = body.detail || `Failed: ${r.status}`;
          return;
        }
        renderPregame(body);
      } catch (err) {
        summary.textContent = `Failed: ${err.message}`;
      } finally {
        btn.disabled = false;
      }
    });
  }

  configForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const data = {};
    for (const el of configForm.elements) {
      if (!el.name) continue;
      if (el.type === "checkbox") data[el.name] = el.checked;
      else if (el.type === "number") data[el.name] = el.value === "" ? null : Number(el.value);
      else data[el.name] = el.value;
    }
    if (data.anthropic_api_key === "") delete data.anthropic_api_key;
    Object.keys(data).forEach((k) => data[k] === null && delete data[k]);

    const res = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    if (res.ok) {
      configSaved.textContent = "Saved.";
      setTimeout(() => (configSaved.textContent = ""), 2000);
      loadConfig();
    } else {
      configSaved.textContent = "Save failed.";
    }
  });

  $("#refresh-patch").addEventListener("click", async () => {
    patchInfoEl.textContent = "Downloading…";
    try {
      const r = await fetch("/api/patch/refresh", { method: "POST" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
    } catch (e) {
      patchInfoEl.textContent = `Failed: ${e.message}`;
      return;
    }
    loadConfig();
  });

  $("#refresh-distill").addEventListener("click", async () => {
    const summary = $("#distill-summary");
    try {
      const r = await fetch("/api/distill/run", { method: "POST" });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        if (summary) summary.textContent = body.detail || `Failed: ${r.status}`;
        return;
      }
    } catch (e) {
      if (summary) summary.textContent = `Failed: ${e.message}`;
      return;
    }
    loadDistill();
  });

  $("#refresh-advice").addEventListener("click", async () => {
    try {
      const r = await fetch("/api/advice/refresh", { method: "POST" });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        adviceSummary.textContent = body.detail || `Failed: ${r.status}`;
      }
    } catch (e) {
      adviceSummary.textContent = `Failed: ${e.message}`;
    }
  });

  // -- bootstrap --

  fetch("/api/patch").then((r) => r.json()).then((p) => {
    if (p && p.version) patchLabel.textContent = `Patch ${p.version}`;
  });
  connect();
})();
