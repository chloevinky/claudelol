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
  const meSummary = $("#me-summary");
  const meItems = $("#me-items");
  const gameClock = $("#game-clock");
  const adviceSummary = $("#advice-summary");
  const adviceSpike = $("#advice-spike");
  const adviceMatchup = $("#advice-matchup");
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

  function formatClock(seconds) {
    if (!seconds) return "0:00";
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return `${m}:${s.toString().padStart(2, "0")}`;
  }

  function renderPlayer(p) {
    const div = document.createElement("div");
    div.className = "player";
    const scores = p.scores || {};
    div.innerHTML = `
      <span class="champ">${p.champion || "?"}</span>
      <span class="pos">${p.position || ""}</span>
      <span class="score">${scores.kills ?? 0}/${scores.deaths ?? 0}/${scores.assists ?? 0} · CS ${scores.creepScore ?? 0}</span>
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
      meItems.innerHTML = "";
      meSummary.innerHTML = "";
      mePosition.textContent = "";
      return;
    }
    meName.textContent = `${me.champion || "?"} — ${me.summoner || ""}`;
    mePosition.textContent = me.position || "";
    const sc = me.scores || {};
    meSummary.innerHTML = `
      <strong>${sc.kills ?? 0} / ${sc.deaths ?? 0} / ${sc.assists ?? 0}</strong>
      &nbsp;·&nbsp; CS ${sc.creepScore ?? 0}
      &nbsp;·&nbsp; lvl ${me.level ?? 0}
      &nbsp;·&nbsp; gold ${me.current_gold ?? 0}
      &nbsp;·&nbsp; keystone <strong>${me.keystone || "—"}</strong>
      &nbsp;·&nbsp; spells ${(me.summoner_spells || []).filter(Boolean).join(" / ") || "—"}
    `;
    meItems.innerHTML = "";
    (me.items || []).forEach((it) => {
      const span = document.createElement("span");
      span.className = "item";
      span.textContent = it.name || `#${it.id}`;
      meItems.appendChild(span);
    });
    if (!me.items || me.items.length === 0) {
      meItems.innerHTML = `<span class="muted small">No items yet</span>`;
    }
  }

  function renderAdvice(state) {
    const advice = state?.advice?.advice || null;
    if (!advice || Object.keys(advice).length === 0) {
      adviceSummary.textContent = state?.advice?.error ? `Error: ${state.advice.error}` : "Waiting for Claude…";
      adviceSpike.textContent = "";
      adviceMatchup.textContent = "";
      bestItems.innerHTML = `<li class="muted small">—</li>`;
      counterItems.innerHTML = `<li class="muted small">—</li>`;
      tipsList.innerHTML = `<li class="muted small">—</li>`;
      adviceMeta.textContent = "";
      return;
    }
    adviceSummary.textContent = advice.summary || "—";
    adviceSpike.textContent = advice.power_spike ? `Spike: ${advice.power_spike}` : "";
    adviceMatchup.textContent = advice.lane_matchup ? `Lane: ${advice.lane_matchup}` : "";

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
      gameClock.textContent = formatClock(snap.game?.game_time || 0);
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
      if (btn.dataset.tab === "config") loadConfig();
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
