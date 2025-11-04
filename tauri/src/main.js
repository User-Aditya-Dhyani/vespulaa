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

// UI state (in-memory only)
let nodesFilter = "";
let nodesSort = { key: "name", dir: "asc" }; // name|state|elements|model|last_onoff|uuid|unicast

// ---------- utils ----------
function toBottom(el){ el.scrollTop = el.scrollHeight; }
function nearBottom(el, pad=12){ return el.scrollHeight - el.clientHeight - el.scrollTop < pad; }
function clamp(n,min,max){ return Math.max(min, Math.min(max, n)); }

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
    if (/^0x[0-9a-f]+$/.test(s)) return s.slice(2).padStart(4,"0");
    if (/^[0-9a-f]+$/.test(s))   return s.padStart(4,"0");
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

function formatModelId(v){
  if (v == null) return "";
  if (typeof v === "string" && v.trim() === "") return "";
  let n;
  if (typeof v === "string") {
    const s = v.trim().toLowerCase();
    if (/^0x[0-9a-f]+$/.test(s)) n = parseInt(s, 16);
    else if (/^[0-9a-f]+$/.test(s)) n = parseInt(s, 16);
    else if (/^\d+$/.test(s)) n = parseInt(s, 10);
  } else if (typeof v === "number") {
    n = v;
  }
  if (Number.isFinite(n)) {
    let hx = n.toString(16).toUpperCase();
    hx = hx.padStart(4,"0");
    return "0x" + hx;
  }
  return String(v);
}

function formatElements(v){
  if (v == null) return "";
  if (Array.isArray(v)) return String(v.length);
  if (typeof v === "object") return Object.keys(v).length ? String(Object.keys(v).length) : "";
  if (typeof v === "number") return String(clamp(v,0,9999));
  return String(v);
}

// last_onoff: show On/Off (model 1000 semantics): 1 -> On, 0 -> Off
function formatLastOnOffBool(v){
  if (v == null) return "";
  if (typeof v === "string" && /^\d+$/.test(v)) v = parseInt(v,10);
  if (v === 1) return "On";
  if (v === 0) return "Off";
  return "";
}

// --- Extract 'state' robustly from nodes.json ---
function extractState(info){
  if (!info || typeof info !== "object") return undefined;
  let s = info.state;
  if (s && typeof s === "object") {
    s = s.value ?? s.name ?? s.state ?? s.status;
  }
  if (typeof s !== "string") {
    s = info.status ?? info.node_state;
  }
  if (typeof s === "number") s = String(s);
  if (typeof s === "string") return s.trim();
  return undefined;
}

/*
  State vocabulary sourced from controller.py:

  Provisioning pipeline:
    provisioning -> appkey_sent -> appkey_ok -> bind_sent -> bind_ok -> pub_sent -> pub_ok

  Other / runtime / admin:
    reset_sent (we hide these rows), error/fail/timeout, attached/attach, scanning/seen/discovered/pending
*/
const STATE_INFO = {
  // terminal + admin
  "pub_ok":        { label: "Provisioned",      prio: 90, cls: "ok" },       // final success
  "reset_sent":    { label: "Reset Sent",       prio: 10, cls: "muted" },
  "reset_ok":      { label: "Reset",            prio: 0,  cls: "muted", hide: true }, // UI: hidden

  // provisioning (descending importance)
  "pub_sent":      { label: "Publish Sent",     prio: 85, cls: "info" },
  "bind_ok":       { label: "Bind OK",          prio: 80, cls: "info" },
  "bind_sent":     { label: "Bind Sent",        prio: 75, cls: "info" },
  "appkey_ok":     { label: "AppKey OK",        prio: 70, cls: "info" },
  "appkey_sent":   { label: "AppKey Sent",      prio: 65, cls: "info" },
  "provisioning":  { label: "Provisioning…",    prio: 60, cls: "warn" },

  // discovery / runtime (optional convenience states)
  "attached":      { label: "Attached",         prio: 50, cls: "ok" },
  "attach":        { label: "Attaching…",       prio: 48, cls: "info" },
  "scanning":      { label: "Scanning",         prio: 45, cls: "info" },
  "seen":          { label: "Seen",             prio: 44, cls: "info" },
  "discovered":    { label: "Discovered",       prio: 43, cls: "info" },
  "pending":       { label: "Pending",          prio: 40, cls: "warn" },

  // failure
  "error":         { label: "Error",            prio: 5,  cls: "bad" },
  "fail":          { label: "Error",            prio: 5,  cls: "bad" },
  "timeout":       { label: "Timeout",          prio: 5,  cls: "bad" },

  // fallback
  "unknown":       { label: "Unknown",          prio: 1,  cls: "muted" },
};

function stateDisplay(stateRaw){
  const s = String(stateRaw || "unknown").toLowerCase();
  if (STATE_INFO[s]) return { key: s, ...STATE_INFO[s] };
  return { key: s, ...STATE_INFO["unknown"] };
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

// --- toasts ---
function toast(msg, kind="info"){
  let wrap = $("#toastWrap");
  if (!wrap){
    wrap = document.createElement("div");
    wrap.id = "toastWrap";
    document.body.appendChild(wrap);
  }
  const t = document.createElement("div");
  t.className = `toast ${kind}`;
  t.textContent = msg;
  wrap.appendChild(t);
  setTimeout(()=>{ t.classList.add("show"); }, 10);
  setTimeout(()=>{ t.classList.remove("show"); setTimeout(()=>t.remove(), 200); }, 2200);
}

// --- copy helpers ---
async function copyText(txt){
  try {
    await navigator.clipboard.writeText(txt);
    toast("Copied", "ok");
  } catch {
    toast("Copy failed", "err");
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

// New: render from nodes.json DB with filter & sort; otherwise fall back to scans.
function renderNodesDash() {
  const body = $("#nodes-body"); if (!body) return;
  const stick = nearBottom(body);
  body.innerHTML = "";

  const dbEntries = Object.entries(nodesDb || {}); // [uuid, info]
  const useDb = dbEntries.length > 0;

  if (useDb) {
    // Build normalized rows for sorting/filtering
    let rows = dbEntries.map(([uuid, info]) => {
      const uuidClean = cleanUuid(uuid);
      const isLocal = uuidClean === "cae";
      const name = getNodeName(uuidClean);

      // read 'state' directly (includes provisioning states from controller.py)
      const rawState = extractState(info);
      const st = stateDisplay(rawState);

      // hide if reset_sent
      const hide = st.hide === true;

      // unicast
      const unicastRaw = (info && typeof info === "object" && info.unicast != null) ? info.unicast : "";
      let unicastHex = toHexUnicast(unicastRaw);
      if (isLocal) unicastHex = "0001";

      // more fields
      const elements = formatElements(info?.elements);
      const model = formatModelId(info?.model_id);
      const last_onoff_txt = formatLastOnOffBool(info?.last_onoff);

      return {
        uuid: uuidClean,
        isLocal,
        name,
        elements,
        model,
        last_onoff_txt,
        unicastHex,
        state: st,  // {label, prio, cls, key, hide?}
        _hide: hide,
        _raw: info
      };
    });

    // Filter out hidden (e.g., reset_sent)
    rows = rows.filter(r => !r._hide);

    // Free-text Filter
    if (nodesFilter.trim()) {
      const q = nodesFilter.trim().toLowerCase();
      rows = rows.filter(r =>
        r.name.toLowerCase().includes(q) ||
        r.uuid.toLowerCase().includes(q) ||
        (r.unicastHex || "").toLowerCase().includes(q) ||
        (r.model || "").toLowerCase().includes(q) ||
        r.state.label.toLowerCase().includes(q)
      );
    }

    // Sort
    const keyMap = {
      name:        r => r.name,
      state:       r => r.state ? (1000 - r.state.prio) + "_" + r.state.label : 999,
      elements:    r => parseInt(r.elements || "0", 10) || 0,
      model:       r => r.model || "",
      last_onoff:  r => r.last_onoff_txt || "",
      uuid:        r => r.uuid,
      unicast:     r => r.unicastHex || ""
    };
    const key = nodesSort.key in keyMap ? nodesSort.key : "name";
    rows.sort((a,b)=>{
      const av = keyMap[key](a);
      const bv = keyMap[key](b);
      if (typeof av === "number" && typeof bv === "number") return (nodesSort.dir==="asc"?1:-1)*(av-bv);
      return (nodesSort.dir==="asc"?1:-1) * String(av).localeCompare(String(bv));
    });

    // Render rows
    for (const r of rows) {
      const row = document.createElement("div");
      row.className = "nodes-row";

      // ---- Name (inline editable) ----
      const nameCol = document.createElement("div");
      nameCol.className = "col name cell";
      const view = document.createElement("div");
      view.className = "name-view";
      view.textContent = r.name;
      nameCol.appendChild(view);

      view.addEventListener("click", () => {
        const edit = document.createElement("input");
        edit.className = "name-edit";
        edit.value = r.name;
        nameCol.replaceChild(edit, view);
        edit.focus(); edit.select();

        let done = false;
        const commit = async () => {
          if (done) return;
          done = true;
          const val = edit.value;
          await saveNodeName(r.uuid, val);
          view.textContent = getNodeName(r.uuid);
          nameCol.replaceChild(view, edit);
          push("app", `[UI] name saved for ${r.uuid}: "${val || r.uuid}"`);
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

      // ---- State (badge) ----
      const stateCol = document.createElement("div");
      stateCol.className = "col state cell";
      const badge = document.createElement("span");
      badge.className = `badge ${r.state.cls}`;
      badge.textContent = r.state.label;
      stateCol.appendChild(badge);

      // ---- Elements ----
      const elemCol = document.createElement("div");
      elemCol.className = "col elements cell right";
      elemCol.textContent = r.elements;

      // ---- Model ----
      const modelCol = document.createElement("div");
      modelCol.className = "col model cell";
      modelCol.textContent = r.model;

      // ---- Last On/Off ----
      const lastCol = document.createElement("div");
      lastCol.className = "col last_onoff cell";
      lastCol.textContent = r.last_onoff_txt;

      // ---- UUID (with copy) ----
      const uuidCol = document.createElement("div");
      uuidCol.className = "col uuid cell mono";
      const uuidSpan = document.createElement("span");
      uuidSpan.textContent = r.uuid;
      const uuidCopy = document.createElement("button");
      uuidCopy.className = "mini";
      uuidCopy.title = "Copy UUID";
      uuidCopy.textContent = "⧉";
      uuidCopy.addEventListener("click", ()=>copyText(r.uuid));
      uuidCol.appendChild(uuidSpan); uuidCol.appendChild(uuidCopy);

      // ---- Unicast (with copy) ----
      const unicastCol = document.createElement("div");
      unicastCol.className = "col unicast cell mono";
      const uniSpan = document.createElement("span");
      uniSpan.textContent = r.unicastHex;
      const uniCopy = document.createElement("button");
      uniCopy.className = "mini";
      uniCopy.title = "Copy Unicast";
      uniCopy.textContent = "⧉";
      uniCopy.disabled = !r.unicastHex;
      uniCopy.addEventListener("click", ()=>copyText(r.unicastHex));
      unicastCol.appendChild(uniSpan); unicastCol.appendChild(uniCopy);

      // ---- Actions: Reset using row's unicast ----
      const actionsCol = document.createElement("div");
      actionsCol.className = "col actions cell right";

      if (!r.isLocal) {
        const btnReset = document.createElement("button");
        btnReset.className = "btn warn";
        btnReset.textContent = "Reset";
        btnReset.title = "Reset this node via unicast from nodes.json";
        if (!r.unicastHex) {
          btnReset.disabled = true;
          btnReset.title = "No valid unicast in nodes.json for this node";
        }
        btnReset.addEventListener("click", () => {
          if (!r.unicastHex) {
            push("app", `[UI] reset_node: missing/invalid unicast for ${r.name}`);
            return;
          }
          push("app", `[UI] reset_node → ${r.name} (${r.unicastHex})`);
          toast(`Reset sent to ${r.name}`, "ok");
          send("reset_node", { unicast: r.unicastHex });
        });
        actionsCol.appendChild(btnReset);
      } else {
        const dash = document.createElement("span");
        dash.textContent = "—";
        dash.style.opacity = "0.6";
        actionsCol.appendChild(dash);
      }

      row.appendChild(nameCol);
      row.appendChild(stateCol);
      row.appendChild(elemCol);
      row.appendChild(modelCol);
      row.appendChild(lastCol);
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
      nameCol.className = "col name cell";
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

      const stateCol = document.createElement("div"); stateCol.className = "col state cell"; stateCol.textContent = "";
      const elemCol  = document.createElement("div"); elemCol.className  = "col elements cell right"; elemCol.textContent  = "";
      const modelCol = document.createElement("div"); modelCol.className = "col model cell"; modelCol.textContent = "";
      const lastCol  = document.createElement("div"); lastCol.className  = "col last_onoff cell"; lastCol.textContent  = "";
      const uuidCol  = document.createElement("div"); uuidCol.className  = "col uuid cell mono"; uuidCol.textContent  = cleanUuid(uuid);
      const uniCol   = document.createElement("div"); uniCol.className   = "col unicast cell mono"; uniCol.textContent   = `RSSI ${String(rssi)}`;
      const actCol   = document.createElement("div"); actCol.className   = "col actions cell right";
      const btnReset = document.createElement("button"); btnReset.className = "btn warn"; btnReset.textContent = "Reset"; btnReset.disabled = true;
      actCol.appendChild(btnReset);

      row.appendChild(nameCol); row.appendChild(stateCol); row.appendChild(elemCol);
      row.appendChild(modelCol); row.appendChild(lastCol); row.appendChild(uuidCol);
      row.appendChild(uniCol);   row.appendChild(actCol);
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

  // --- Column sorting handlers ---
  $all(".nodes-head .sortable").forEach(h => {
    h.addEventListener("click", () => {
      const k = h.dataset.key;
      if (!k) return;
      if (nodesSort.key === k) {
        nodesSort.dir = (nodesSort.dir === "asc") ? "desc" : "asc";
      } else {
        nodesSort.key = k; nodesSort.dir = "asc";
      }
      $all(".nodes-head .sortable").forEach(x => x.dataset.dir = (x.dataset.key === nodesSort.key ? nodesSort.dir : ""));
      renderNodesDash();
    });
  });

  // --- Filter input ---
  $("#nodesFilter")?.addEventListener("input", (e) => {
    nodesFilter = e.target.value || "";
    renderNodesDash();
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

