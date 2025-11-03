// tauri/src/main.js

// ---------------- app code (define first to avoid TDZ) ----------------
function $(s){ return document.querySelector(s); }
function $all(s){ return Array.from(document.querySelectorAll(s)); }

const panes = {};
let currentTab = "app";
const stickToBottom = { nodes: true, devkey: true, app: true };
const scanMap = new Map(); // uuid -> best RSSI
const nodeNames = new Map(); // uuid -> friendly name

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
function getNodeName(uuid) { const u = cleanUuid(uuid); return nodeNames.get(u) || u; }

async function loadNodeNames() {
  const invoke = window.__VESPU_INVOKE__;
  try {
    const map = await invoke?.("get_node_names");
    if (map && typeof map === "object") {
      Object.entries(map).forEach(([u, n]) => nodeNames.set(cleanUuid(u), String(n)));
    }
  } catch (e) {
    push("app", `[WARN] get_node_names failed: ${e}`);
  }
}
async function saveNodeName(uuid, name) {
  const u = cleanUuid(uuid);
  const invoke = window.__VESPU_INVOKE__;
  try {
    await invoke?.("set_node_name", { uuid: u, name: String(name) });
    if (String(name).trim()) nodeNames.set(u, String(name).trim());
    else nodeNames.delete(u);
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

function renderNodesDash() {
  const body = $("#nodes-body"); if (!body) return;
  const entries = [...scanMap.entries()].sort((a,b)=> b[1]-a[1]);
  const stick = nearBottom(body);
  body.innerHTML = "";
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

      const commit = async () => {
        const val = edit.value;
        await saveNodeName(uuid, val);
        view.textContent = getNodeName(uuid);
        nameCol.replaceChild(view, edit);
        push("app", `[UI] name saved for ${uuid}: "${val || uuid}"`);
      };
      const cancel = () => { nameCol.replaceChild(view, edit); };
      edit.addEventListener("keydown", (e) => { if (e.key === "Enter") commit(); else if (e.key === "Escape") cancel(); });
      edit.addEventListener("blur", commit);
    });

    const uuidCol = document.createElement("div"); uuidCol.className = "col uuid"; uuidCol.textContent = cleanUuid(uuid);
    const rssiCol = document.createElement("div"); rssiCol.className = "col rssi"; rssiCol.textContent = String(rssi);

    const actionsCol = document.createElement("div"); actionsCol.className = "col actions";
    const btnMsg = document.createElement("button");
    btnMsg.className = "btn"; btnMsg.textContent = "Message"; btnMsg.title = "Future: send model cmd";
    btnMsg.addEventListener("click", () => { push("app", `[UI] (future) send message → ${getNodeName(uuid)}`); });
    actionsCol.appendChild(btnMsg);

    row.appendChild(nameCol); row.appendChild(uuidCol); row.appendChild(rssiCol); row.appendChild(actionsCol);
    body.appendChild(row);
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

function startApp() {
  const listen = window.__VESPU_LISTEN__;

  const bm = document.getElementById("boot-marker");
  if (bm) bm.textContent = "JS loaded ✔";

  panes.devkey = $("#log-devkey");
  panes.app   = $("#log-app");

  push("app", "[UI] Frontend ready");
  loadNodeNames().then(() => renderNodesDash());
  setStatus("idle");
  setTab("app");

  Object.entries(panes).forEach(([tab, el]) => {
    if (!el) return;
    stickToBottom[tab] = true;
    el.addEventListener("scroll", () => { stickToBottom[tab] = nearBottom(el); });
    el.addEventListener("wheel",  () => { stickToBottom[tab] = nearBottom(el); }, { passive: true });
    el.addEventListener("mousedown", () => { stickToBottom[tab] = nearBottom(el); });
  });

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

  $("#logTabs")?.addEventListener("click", (e) => {
    const tab = e.target.closest(".tab")?.dataset.tab;
    if (tab) setTab(tab);
  });

  $all("button[data-cmd]").forEach(btn => {
    btn.addEventListener("click", () => {
      const name = btn.dataset.cmd;
      if (name === "leave" || name === "purge") {
        if (!confirm(`Are you sure you want to ${name.toUpperCase()}?`)) return;
        }
      send(name);
    });
  });

  $("#scanStart")?.addEventListener("click", () => {
    const secs = parseInt($("#scanSecs")?.value || "15", 10);
    send("scan_start", { seconds: Number.isFinite(secs) ? secs : 15 });
  });
  $("#scanStop")?.addEventListener("click", () => send("scan_stop"));

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

  $("#resetBtn")?.addEventListener("click", () => {
    const unicast = ($("#resetUnicast")?.value || "").trim();
    if (!unicast) return push("app", "[UI] reset_node: no unicast");
    send("reset_node", { unicast });
  });

  if (listen) {
    (async () => {
      await listen("log:app",    (e) => push("app", String(e.payload)));
      await listen("log:python", (e) => push("app", String(e.payload)));
      await listen("log:nodes",  (e) => push("app", String(e.payload)));
      await listen("log:devkey", (e) => push("devkey", String(e.payload)));
      await listen("resp:cmd",   (e) => {
        const { cmd, ok, msg } = e.payload || {};
        setStatus(`${cmd} → ${ok ? "ok" : "error"}`);
        push("app", `[${ok?"OK":"ERR"}] ${cmd}: ${msg}`);
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
        renderNodesDash();
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
    if (t && t.core && t.core.invoke && t.event && t.event.listen) {
      window.__VESPU_INVOKE__ = t.core.invoke;
      window.__VESPU_LISTEN__ = t.event.listen;
      startApp();
    } else if (Date.now() < deadline) {
      setTimeout(poll, 20);
    } else {
      console.error("[FATAL] Tauri globals not ready; UI will show 'invoke missing'.");
      startApp(); // still start so you see logs/warnings
    }
  })();
})();

