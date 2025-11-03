// ---------------- app code (define first to avoid TDZ) ----------------
function $(s){ return document.querySelector(s); }
function $all(s){ return Array.from(document.querySelectorAll(s)); }

const panes = {};
let currentTab = "app";
const stickToBottom = { nodes: true, devkey: true, app: true };
const scanMap = new Map(); // uuid -> best RSSI

// Names are now sourced from nodes.json; this is a UI cache.
const nodeNames = new Map(); // uuid -> friendly name
let nodesDb = {};            // full Python-maintained DB (uuid_hex -> object)

// ---------- utils ----------
function toBottom(el){ el.scrollTop = el.scrollHeight; }
function nearBottom(el, pad=12){ return el.scrollHeight - el.clientHeight - el.scrollTop < pad; }

function setTab(tab){
  currentTab = tab;
  $all(".tab").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
  const nodes = $("#nodes-dash");
  const dev   = $("#log-devkey");
  const app   = $("#log-app");
  if (nodes) nodes.classList.toggle("hidden", tab !== "nodes");
  if (dev)   dev.classList.toggle("hidden", tab !== "devkey");
  if (app)   app.classList.toggle("hidden", tab !== "app");
}

function push(tab, text){
  const el = panes[tab]; if(!el) return;
  const stick = stickToBottom[tab] && nearBottom(el);
  el.textContent += (el.textContent ? "\n" : "") + text;
  if (stick) toBottom(el);
}

function setStatus(t){ const s=$("#status"); if(s) s.textContent = `Status: ${t}`; }

function cleanUuid(u) { return String(u).replace(/[^0-9a-fA-F]/g, "").toLowerCase(); }
function getNodeName(uuid) {
  const u = cleanUuid(uuid);
  return nodeNames.get(u) || u;
}

// Normalize unicast to hex string suitable for reset_node
function toHexUnicast(u) {
  if (u == null) return "";
  if (typeof u === "string") {
    const s = u.trim().toLowerCase();
    if (/^0x[0-9a-f]+$/.test(s)) return s.slice(2);
    if (/^[0-9a-f]+$/.test(s)) return s;
    if (/^\d+$/.test(s)) { // decimal string
      const n = BigInt(s);
      let hx = n.toString(16);
      if (hx.length % 2) hx = "0" + hx;
      if (hx.length < 4) hx = hx.padStart(4, "0");
      return hx;
    }
    return "";
  }
  try {
    const n = BigInt(u);
    let hx = n.toString(16);
    if (hx.length % 2) hx = "0" + hx;
    if (hx.length < 4) hx = hx.padStart(4, "0");
    return hx;
  } catch {
    return "";
  }
}

async function refreshNodesDb() {
  const invoke = window.__VESPU_INVOKE__;
  try {
    const db = await invoke?.("read_nodes_db");
    if (db && typeof db === "object") {
      nodesDb = db;
      nodeNames.clear();
      for (const [uuid, entry] of Object.entries(nodesDb)) {
        const nm = (entry && typeof entry === "object" && entry.name) ? String(entry.name) : "";
        if (nm.trim()) nodeNames.set(cleanUuid(uuid), nm.trim());
      }
    } else {
      nodesDb = {};
      nodeNames.clear();
    }
  } catch (e) {
    nodesDb = {};
    push("app", `[WARN] read_nodes_db failed: ${e}`);
  }
}

async function saveNodeName(uuid, name) {
  const u = cleanUuid(uuid);
  const invoke = window.__VESPU_INVOKE__;
  try {
    await invoke?.("set_node_name", { uuid: u, name: String(name) });
    if (String(name).trim()) nodeNames.set(u, String(name).trim());
    else nodeNames.delete(u);
    if (!nodesDb[u] || typeof nodesDb[u] !== "object") nodesDb[u] = {};
    nodesDb[u].name = String(name);
  } catch (e) {
    push("app", `[ERR] set_node_name(${u}) failed: ${e}`);
  }
}

function renderScanList(){
  const ul = $("#scanList"); if (!ul) return;
  const prevSel = getSelectedUuid();
  ul.innerHTML = "";
  [...scanMap.entries()].sort((a,b)=> b[1] - a[1]).forEach(([uuid, rssi]) => {
    const li = document.createElement("li");
    li.textContent = `${uuid} | ${rssi}`;
    li.dataset.uuid = uuid;
    li.dataset.rssi = String(rssi);
    if (uuid === prevSel) li.classList.add("sel");
    ul.appendChild(li);
  });
}

// New: render from nodes.json DB if present; otherwise fall back to scans.
function renderNodesDash() {
  const body = $("#nodes-body"); if (!body) return;
  const stick = nearBottom(body);
  body.innerHTML = "";

  const dbEntries = Object.entries(nodesDb || {}); // [uuid, info]
  const useDb = dbEntries.length > 0;

  if (useDb) {
    for (const [uuid, info] of dbEntries) {
      const row = document.createElement("div");
      row.className = "nodes-row";

      // ---- Name (inline editable) ----
      const nameCol = document.createElement("div");
      nameCol.className = "col name";
      const view = document.createElement("div");
      view.className = "name-view";
      view.textContent = getNodeName(uuid);
      nameCol.appendChild(view);

      view.addEventListener("click", () => {
        const edit = document.createElement("input");
        edit.className = "name-edit";
        edit.value = getNodeName(uuid);
        nameCol.replaceChild(edit, view);
        edit.focus(); edit.select();

        let done = false;
        const commit = async () => {
          if (done) return;
          done = true;
          const val = edit.value;
          await saveNodeName(uuid, val);
          view.textContent = getNodeName(uuid);
          nameCol.replaceChild(view, edit);
          push("app", `[UI] name saved for ${uuid}: "${val || uuid}"`);
        };
        const cancel = () => {
          if (done) return;
          done = true;
          nameCol.replaceChild(view, edit);
        };
        edit.addEventListener("keydown", (e) => {
          if (e.key === "Enter") { e.preventDefault(); commit(); }
          else if (e.key === "Escape") { e.preventDefault(); cancel(); }
        });
        edit.addEventListener("blur", commit);
      });

      // ---- UUID ----
      const uuidCol = document.createElement("div");
      uuidCol.className = "col uuid";
      const uuidClean = cleanUuid(uuid);
      uuidCol.textContent = uuidClean;

      // ---- Unicast (from nodes.json) ----
      const unicastCol = document.createElement("div");
      unicastCol.className = "col unicast";

      const isLocal = uuidClean === "cae";
      let rawUnicast = (info && typeof info === "object" && info.unicast != null) ? info.unicast : "";
      let unicastHex = toHexUnicast(rawUnicast);
      if (isLocal) unicastHex = "0001"; // rule: local 'cae' always 0001
      unicastCol.textContent = unicastHex;

      // ---- Actions: Reset using row's unicast ----
      const actionsCol = document.createElement("div");
      actionsCol.className = "col actions";

      if (!isLocal) {
        const btnReset = document.createElement("button");
        btnReset.className = "btn warn";
        btnReset.textContent = "Reset";
        btnReset.title = "Reset this node via unicast from nodes.json";
        if (!unicastHex) {
          btnReset.disabled = true;
          btnReset.title = "No valid unicast in nodes.json for this node";
        }
        btnReset.addEventListener("click", () => {
          if (!unicastHex) {
            push("app", `[UI] reset_node: missing/invalid unicast for ${getNodeName(uuid)}`);
            return;
          }
          push("app", `[UI] reset_node → ${getNodeName(uuid)} (${unicastHex})`);
          send("reset_node", { unicast: unicastHex });
        });
        actionsCol.appendChild(btnReset);
      } else {
        const dot = document.createElement("span");
        dot.textContent = "—";
        dot.style.opacity = "0.6";
        actionsCol.appendChild(dot);
      }

      row.appendChild(nameCol);
      row.appendChild(uuidCol);
      row.appendChild(unicastCol);
      row.appendChild(actionsCol);
      body.appendChild(row);
    }
  } else {
    // Fallback: legacy “scan-only” rows
    const entries = [...scanMap.entries()].sort((a,b)=> b[1]-a[1]);
    for (const [uuid, rssi] of entries) {
      const row = document.createElement("div");
      row.className = "nodes-row";

      const nameCol = document.createElement("div");
      nameCol.className = "col name";
      const view = document.createElement("div");
      view.className = "name-view";
      view.textContent = getNodeName(uuid);
      nameCol.appendChild(view);

      view.addEventListener("click", () => {
        const edit = document.createElement("input");
        edit.className = "name-edit";
        edit.value = getNodeName(uuid);
        nameCol.replaceChild(edit, view);
        edit.focus(); edit.select();

        let done = false;
        const commit = async () => {
          if (done) return;
          done = true;
          const val = edit.value;
          await saveNodeName(uuid, val);
          view.textContent = getNodeName(uuid);
          nameCol.replaceChild(view, edit);
          push("app", `[UI] name saved for ${uuid}: "${val || uuid}"`);
        };
        const cancel = () => {
          if (done) return;
          done = true;
          nameCol.replaceChild(view, edit);
        };
        edit.addEventListener("keydown", (e) => {
          if (e.key === "Enter") { e.preventDefault(); commit(); }
          else if (e.key === "Escape") { e.preventDefault(); cancel(); }
        });
        edit.addEventListener("blur", commit);
      });

      const uuidCol = document.createElement("div"); uuidCol.className = "col uuid"; uuidCol.textContent = cleanUuid(uuid);

      const unicastCol = document.createElement("div");
      unicastCol.className = "col unicast";
      unicastCol.textContent = `RSSI ${String(rssi)}`;

      const actionsCol = document.createElement("div");
      actionsCol.className = "col actions";
      const btnReset = document.createElement("button");
      btnReset.className = "btn warn";
      btnReset.textContent = "Reset";
      btnReset.disabled = true;
      btnReset.title = "Unavailable (no nodes.json entry)";
      actionsCol.appendChild(btnReset);

      row.appendChild(nameCol); row.appendChild(uuidCol); row.appendChild(unicastCol); row.appendChild(actionsCol);
      body.appendChild(row);
    }
  }

  if (stick) toBottom(body);
}

function send(name, args){
  push("app", `[UI] ${name}${args ? " " + JSON.stringify(args) : ""}`);
  setStatus(`${name}…`);
  const invoke = window.__VESPU_INVOKE__;
  if (!invoke) { push("app", "[ERR] Tauri invoke missing"); console.error("[ERR] Tauri invoke missing"); return; }
  invoke(name, args || {}).catch(e => { push("app", `[ERR] invoke ${name}: ${String(e)}`); setStatus(`${name} → error`); });
}

function getSelectedUuid() {
  const sel = document.querySelector("#scanList li.sel");
  return sel ? sel.dataset.uuid : null;
}

async function bootAndRender() {
  await refreshNodesDb();
  renderNodesDash();
}

function startApp() {
  const listen = window.__VESPU_LISTEN__;

  const bm = document.getElementById("boot-marker");
  if (bm) bm.textContent = "JS loaded ✔";

  panes.devkey = $("#log-devkey");
  panes.app   = $("#log-app");

  // set terminal-style stickiness on log panes
  Object.entries({devkey: panes.devkey, app: panes.app}).forEach(([tab, el]) => {
    if (!el) return;
    stickToBottom[tab] = true;
    el.addEventListener("scroll", () => { stickToBottom[tab] = nearBottom(el); });
    el.addEventListener("wheel",  () => { stickToBottom[tab] = nearBottom(el); }, { passive: true });
    el.addEventListener("mousedown", () => { stickToBottom[tab] = nearBottom(el); });
  });

  push("app", "[UI] Frontend ready");
  setStatus("idle");
  setTab("nodes");
  bootAndRender();

  // --- header quick-menu (overlay) ---
  const grid = $(".grid");
  const menuBtn = $("#menuBtn");
  const menu = $("#quickMenu");
  function closeMenu(){ menu?.classList.remove("open"); }
  function openScanPanel(){ grid?.classList.add("scan-open"); }
  function closeScanPanel(){ grid?.classList.remove("scan-open"); }

  menuBtn?.addEventListener("click", () => {
    menu?.classList.toggle("open");
  });
  menu?.addEventListener("click", (e) => {
    const act = e.target.closest("[data-action]")?.dataset.action;
    if (!act) return;
    if (act === "open-scan") {
      openScanPanel();
    }
    closeMenu();
  });
  document.addEventListener("click", (e) => {
    if (!menu || !menuBtn) return;
    if (menu.contains(e.target) || menuBtn.contains(e.target)) return;
    closeMenu();
  });

  // --- Scan panel controls (panel has its own close button) ---
  $("#scanClose")?.addEventListener("click", closeScanPanel);

  $("#scanStart")?.addEventListener("click", () => {
    const secs = parseInt($("#scanSecs")?.value || "15", 10);
    scanMap.clear();
    renderScanList();
    openScanPanel(); // ensure visible
    send("scan_start", { seconds: Number.isFinite(secs) ? secs : 15 });
  });

  $("#scanStop")?.addEventListener("click", () => send("scan_stop"));

  // --- Scan list interactions ---
  const ul = $("#scanList");
  if (ul) {
    ul.addEventListener("click", (e) => {
      const li = e.target.closest("li");
      if (!li || !ul.contains(li)) return;
      ul.querySelectorAll("li.sel").forEach(x => x.classList.remove("sel"));
      li.classList.add("sel");
    });
    ul.addEventListener("keydown", (e) => {
      const items = Array.from(ul.querySelectorAll("li"));
      if (!items.length) return;
      const idx = items.findIndex(li => li.classList.contains("sel"));
      if (e.key === "ArrowDown") {
        const next = items[Math.min((idx < 0 ? 0 : idx + 1), items.length - 1)];
        items.forEach(x => x.classList.remove("sel"));
        next.classList.add("sel");
        next.scrollIntoView({ block: "nearest" });
        e.preventDefault();
      } else if (e.key === "ArrowUp") {
        const prev = items[Math.max((idx < 0 ? 0 : idx - 1), 0)];
        items.forEach(x => x.classList.remove("sel"));
        prev.classList.add("sel");
        prev.scrollIntoView({ block: "nearest" });
        e.preventDefault();
      } else if (e.key === "Enter") {
        const uuid = getSelectedUuid();
        if (uuid) send("provision_uuid", { uuid });
      }
    });
  }

  // --- Tabs ---
  $("#logTabs")?.addEventListener("click", (e) => {
    const tab = e.target.closest(".tab")?.dataset.tab;
    if (tab) setTab(tab);
  });

  // --- Top passthrough buttons ---
  $all("button[data-cmd]").forEach(btn => {
    btn.addEventListener("click", () => {
      const name = btn.dataset.cmd;
      if (name === "leave" || name === "purge") {
        if (!confirm(`Are you sure you want to ${name.toUpperCase()}?`)) return;
      }
      send(name);
    });
  });

  // --- Provision controls ---
  $("#provUuidBtn")?.addEventListener("click", () => {
    const uuid = ($("#provUuid")?.value || "").trim();
    if (!uuid) return push("app", "[UI] provision_uuid: no UUID");
    send("provision_uuid", { uuid });
  });

  $("#provSelected")?.addEventListener("click", () => {
    const uuid = getSelectedUuid();
    if (!uuid) return push("app", "[UI] provision_selected: nothing selected");
    send("provision_uuid", { uuid });
  });

  // --- Events from backend ---
  if (listen) {
    (async () => {
      await listen("log:app",    (e) => push("app", String(e.payload)));
      await listen("log:python", (e) => push("app", String(e.payload)));
      await listen("log:nodes",  (e) => push("app", String(e.payload)));
      await listen("log:devkey", (e) => push("devkey", String(e.payload)));

      // Debounced one-shot refresh after *only* the relevant state-changing commands.
      let refreshTimer = null;
      function scheduleRefresh(delay = 150) {
        if (refreshTimer) clearTimeout(refreshTimer);
        refreshTimer = setTimeout(async () => {
          await refreshNodesDb();
          renderNodesDash();
        }, delay);
      }

      await listen("resp:cmd",   async (e) => {
        const { cmd, ok, msg } = e.payload || {};
        setStatus(`${cmd} → ${ok ? "ok" : "error"}`);
        push("app", `[${ok?"OK":"ERR"}] ${cmd}: ${msg}`);

        if (ok && (
          cmd === "attach" ||
          cmd === "create_network" ||
          cmd === "provision_uuid" ||
          cmd === "reset_node" ||
          cmd === "config_local_client"
        )) {
          scheduleRefresh(150);
        }
      });

      await listen("nodes:changed", async () => {
        await refreshNodesDb();
        renderNodesDash();
      });

      await listen("scan:add",   (e) => {
        const { uuid, rssi } = e.payload || {};
        if (!uuid) return;
        const clean = cleanUuid(uuid);
        if (clean.length !== 32) {
          push("app", `[GUI] Ignored scan UUID (len=${clean.length}): ${uuid}`);
          return;
        }
        const prev = scanMap.get(clean);
        if (prev === undefined || rssi > prev) scanMap.set(clean, rssi|0);
        renderScanList();
      });
    })();
  } else {
    push("app", "[WARN] Tauri event API not available");
  }
}

// ---- ensure Tauri APIs are available AFTER startApp is defined ----
(function bootstrapTauriGlobals() {
  const deadline = Date.now() + 5000; // wait up to 5s
  (function poll() {
    const t = window.__TAURI__;
    if (t && t.core && t.event && t.event.listen && t.core.invoke) {
      window.__VESPU_INVOKE__ = t.core.invoke;
      window.__VESPU_LISTEN__ = t.event.listen;
      startApp();
    } else if (Date.now() < deadline) {
      setTimeout(poll, 20);
    } else {
      console.error("[FATAL] Tauri globals not ready; UI will show 'invoke missing'.");
      startApp();
    }
  })();
})();

