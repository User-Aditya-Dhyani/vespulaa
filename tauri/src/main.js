// tauri/src/main.js
const { core, event } = window.__TAURI__ || {};
const invoke = core?.invoke;
const listen = event?.listen;

function $(s){ return document.querySelector(s); }
function $all(s){ return Array.from(document.querySelectorAll(s)); }

const panes = {};
let currentTab = "app";
const stickToBottom = { nodes: true, devkey: true, app: true };
const scanMap = new Map(); // uuid -> best RSSI

function toBottom(el){ el.scrollTop = el.scrollHeight; }
function nearBottom(el, pad=12){ return el.scrollHeight - el.clientHeight - el.scrollTop < pad; }

function setTab(tab){
  currentTab = tab;
  $all(".tab").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
  Object.entries(panes).forEach(([k, el]) => el.classList.toggle("hidden", k !== tab));
}

function push(tab, text){
  const el = panes[tab]; if(!el) return;
  const stick = stickToBottom[tab] && nearBottom(el);
  el.textContent += (el.textContent ? "\n" : "") + text;
  if (stick) toBottom(el);
}

function setStatus(t){ const s=$("#status"); if(s) s.textContent = `Status: ${t}`; }

function renderScanList(){
  const ul = $("#scanList");
  if (!ul) return;

  // keep currently selected (if any)
  const prevSel = getSelectedUuid();

  ul.innerHTML = "";
  // sort by RSSI desc and render
  [...scanMap.entries()]
    .sort((a,b)=> b[1] - a[1])
    .forEach(([uuid, rssi]) => {
      const li = document.createElement("li");
      li.textContent = `${uuid} | ${rssi}`;
      li.dataset.uuid = uuid;
      li.dataset.rssi = String(rssi);
      if (uuid === prevSel) li.classList.add("sel");
      ul.appendChild(li);
    });
}

function send(name, args){
  push("app", `[UI] ${name}${args ? " " + JSON.stringify(args) : ""}`);
  setStatus(`${name}…`);
  if (!invoke) { push("app", "[ERR] Tauri invoke missing"); return; }
  // pass args directly (no nested {args:{}}), matches Rust signatures
  invoke(name, args || {}).catch(e => {
    push("app", `[ERR] invoke ${name}: ${String(e)}`);
    setStatus(`${name} → error`);
  });
}

function getSelectedUuid() {
  const sel = document.querySelector("#scanList li.sel");
  return sel ? sel.dataset.uuid : null;
}

document.addEventListener("DOMContentLoaded", async () => {
  const bm = $("#boot-marker"); if (bm) bm.textContent = "JS loaded ✔";

  panes.nodes = $("#log-nodes");
  panes.devkey = $("#log-devkey");
  panes.app   = $("#log-app");

  push("app", "[UI] Frontend ready");
  setStatus("idle");
  setTab("app");

  Object.entries(panes).forEach(([tab, el]) => {
    stickToBottom[tab] = true;
    
    el.addEventListener("scroll", () => { 
      const atBottom = nearBottom(el);
      if (!atBottom) stickToBottom[tab] = false;
      else stickToBottom[tab] = true;
    });
    el.addEventListener("wheel", () => { stickToBottom[tab] = nearBottom(el); }, { passive: true });
    el.addEventListener("mousedown", () => { stickToBottom[tab] = nearBottom(el); });    
  });
  
  // single-select behavior on the UUID list
  const ul = $("#scanList");
  if (ul) {
    // click to select
    ul.addEventListener("click", (e) => {
      const li = e.target.closest("li");
      if (!li || !ul.contains(li)) return;
      // clear previous selection
      ul.querySelectorAll("li.sel").forEach(x => x.classList.remove("sel"));
      li.classList.add("sel");
    });

    // Up/Down enter selection (optional but handy)
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
        // trigger provision via keyboard
        const uuid = getSelectedUuid();
        if (uuid) send("provision_uuid", { uuid });
      }
    });
  }


  $("#logTabs")?.addEventListener("click", (e) => {
    const tab = e.target.closest(".tab")?.dataset.tab;
    if (tab) setTab(tab);
  });

  // Top bar buttons, exact Controller verbs
  $all("button[data-cmd]").forEach(btn => {
    btn.addEventListener("click", () => {
      const name = btn.dataset.cmd;
      if (name === "leave" || name === "purge") {
        if (!confirm(`Are you sure you want to ${name.toUpperCase()}?`)) return;
      }
      send(name);
    });
  });

  // Scan controls
  $("#scanStart")?.addEventListener("click", () => {
    const secs = parseInt($("#scanSecs")?.value || "15", 10);
    send("scan_start", { seconds: Number.isFinite(secs) ? secs : 15 });
  });
  $("#scanStop")?.addEventListener("click", () => send("scan_stop"));

  // provision by UUID
  $("#provUuidBtn")?.addEventListener("click", () => {
    const uuid = ($("#provUuid")?.value || "").trim();
    if (!uuid) return push("app", "[UI] provision_uuid: no UUID");
    send("provision_uuid", { uuid });
  });

  // provision selected
  $("#provSelected")?.addEventListener("click", () => {
    const uuid = getSelectedUuid();
    if (!uuid) return push("app", "[UI] provision_selected: nothing selected");
    // Send the selected UUID to the backend
    send("provision_uuid", { uuid });
  });

  // reset
  $("#resetBtn")?.addEventListener("click", () => {
    const unicast = ($("#resetUnicast")?.value || "").trim();
    if (!unicast) return push("app", "[UI] reset_node: no unicast");
    send("reset_node", { unicast });
  });

  // Back-end signals
  if (listen) {
    await listen("log:app",    (e) => push("app", String(e.payload)));
    await listen("log:python", (e) => push("app", String(e.payload)));
    await listen("log:nodes",  (e) => push("nodes",  String(e.payload)));
    await listen("log:devkey", (e) => push("devkey", String(e.payload)));
    await listen("resp:cmd",   (e) => {
      const { cmd, ok, msg } = e.payload || {};
      setStatus(`${cmd} → ${ok ? "ok" : "error"}`);
      push("app", `[${ok?"OK":"ERR"}] ${cmd}: ${msg}`);
    });
    await listen("scan:add",   (e) => {
      const { uuid, rssi } = e.payload || {};
      if (!uuid) return;
      const clean = String(uuid).replace(/[^0-9a-fA-F]/g, "").toLowerCase();
      if (clean.length !== 32) {
        push("app", `[GUI] Ignored scan UUID (len=${clean.length}): ${uuid}`);
        return;
      }
      const prev = scanMap.get(clean);
      if (prev === undefined || rssi > prev) scanMap.set(clean, rssi|0);
      renderScanList();
    });
  } else {
    push("app", "[WARN] Tauri event API not available");
  }
});

