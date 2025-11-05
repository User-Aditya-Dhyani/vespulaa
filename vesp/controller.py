from __future__ import annotations
from typing import Callable, Optional, Any, Dict
from datetime import datetime, timezone
import threading

from pydbus import SystemBus
from gi.repository import GLib

from .dbus_mesh import (
    APP_ROOT, AGENT_PATH, ELEM0_PATH,
    AppRoot, ProvisionAgent, Element0,
    build_object_manager_map,
)
from .util import (
    save_token, load_token, clear_token,
    default_node_uuid_bytes, parse_unprov_uuid, LOG_DIR,
    persist_node_uuid,
    load_nodes_db, save_nodes_db,
)

MESH_BUS = "org.bluez.mesh"
MESH_PATH = "/org/bluez/mesh"


class Controller:
    """
    Main brain:
      - create/attach local provisioner node
      - scan + PB-ADV provision ESP32 nodes
      - auto-config new nodes: AppKey -> Bind -> PubSet
      - keep nodes.json in sync and log traffic
    """

    def __init__(self):

        self._dbus_lock = threading.RLock()
        
        # the UUID we just tried to create/join with (for JoinComplete)
        self._pending_uuid: bytes | None = None

        # DBus plumbing
        self.bus = SystemBus()
        self.mesh = None
        self._attach_in_progress = False

        # objects we export to bluetooth-meshd
        self.app = AppRoot()
        self.agent = ProvisionAgent()
        self.elem0 = Element0()
        for obj in (self.app, self.agent, self.elem0):
            obj.controller = self

        # after Attach(), bluetooth-meshd gives us a node path like
        # /org/bluez/mesh/node<uuid>; we bind that to self.mgmt
        self.node_path: Optional[str] = None
        self.mgmt = None

        # NEW: Lazy Element0 proxy for AppKeySend etc.
        self.elem_proxy = None

        # GUI callbacks for logging + scan table
        self._log_cb: Callable[[str], None] = lambda s: print(s)
        self._scan_cb: Callable[[str, int], None] = lambda uuid_hex, rssi: None

        # ---------- persistent unicast allocator ----------
        db = load_nodes_db()
        max_used = 0x0004
        for info in db.values():
            try:
                base = int(info.get("unicast", 0))
                elems = int(info.get("elements", 1))
                end = base + elems
                if end > max_used:
                    max_used = end
            except Exception:
                pass

        floor_start = 0x0010
        nxt = max_used + 1
        if nxt < floor_start:
            nxt = floor_start
        self._next_unicast = nxt

        # GLib main loop for async callbacks from meshd
        self._glib_loop: Optional[GLib.MainLoop] = None

        # provisioning FSM state
        self._provision_jobs: Dict[int, Dict[str, Any]] = {}
        try:
            (LOG_DIR / "mesh_app.log").touch(exist_ok=True)
            (LOG_DIR / "mesh_devkey.log").touch(exist_ok=True)
        except Exception:
            pass

        # ---- receive-gating to make "detach" actually silent ----
        self._rx_enabled: bool = True  # enabled after attach, disabled on detach

        # ---- runtime state guards ----
        self._scan_active: bool = False              # track UnprovisionedScan state
        self._prov_active_uuid: Optional[str] = None # UUID currently being provisioned (single-flight)
        self._prov_timeout_src: Optional[int] = None # GLib timeout source id for AddNode

        # ---- runtime state guards ----
        self._scan_active: bool = False                  # track UnprovisionedScan state
        self._prov_active_uuid: Optional[str] = None     # UUID currently being provisioned (single-flight)
        self._prov_timeout_src: Optional[int] = None     # kept for compatibility, but unused by no-timeout flow

        # NEW: queueing + scan resume + action guards
        self._prov_queue: list[str] = []                 # FIFO queue of UUIDs
        self._scan_should_resume: bool = False           # remember scan state while we provision

        self._leave_in_progress: bool = False            # UI guard
        self._purge_in_progress: bool = False            # UI guard

        self._pkexec_warmed: bool = False

    # ---------------- internal helpers ----------------

    def _get_mesh(self):
        """
        Lazily get /org/bluez/mesh from bluetooth-meshd.
        """
        if self.mesh is not None:
            return self.mesh
        try:
            self.mesh = self.bus.get(MESH_BUS, MESH_PATH)
            return self.mesh
        except KeyError:
            raise RuntimeError("bluetooth-meshd is not on D-Bus yet. Is it running?")

    def _systemd(self):
        # org.freedesktop.systemd1.Manager (polkit prompt via desktop auth agent)
        return self.bus.get("org.freedesktop.systemd1", "/org/freedesktop/systemd1")

    def _systemd_restart_meshd(self):
        def do():
            self._systemd().RestartUnit("bluetooth-meshd.service", "replace")
        return self._safe_call(do, "systemd RestartUnit(bluetooth-meshd)")

    def _systemd_stop_meshd(self):
        def do():
            self._systemd().StopUnit("bluetooth-meshd.service", "replace")
        return self._safe_call(do, "systemd StopUnit(bluetooth-meshd)")

    def _systemd_start_meshd(self):
        def do():
            self._systemd().StartUnit("bluetooth-meshd.service", "replace")
        return self._safe_call(do, "systemd StartUnit(bluetooth-meshd)")

    def _pkexec_warmup(self) -> None:
        """
        Trigger a single polkit authorization prompt so subsequent pkexec calls
        in this session are allowed without re-prompting (polkit caches auth).
        We do NOT store any password ourselves.
        """
        if self._pkexec_warmed:
            return
        ok, msg = self._pkexec_run(["/bin/sh", "-c", "true"], timeout=30)
        if ok:
            self._pkexec_warmed = True
            self.log("[pkexec] authorization warmed (polkit may cache for this session)")
        else:
            # Not fatal; purge/other calls will prompt again as needed
            self.log(f"[pkexec] warmup did not complete: {msg}")

    def _pkexec_run(self, argv: list[str], timeout: int = 60) -> tuple[bool, str]:
        """
        We still need root for rm -rf /var/lib/bluetooth/mesh/<uuid>.
        We do that with pkexec. Systemd control stays on DBus.
        """
        import subprocess
        try:
            mapped = []
            for a in argv:
                if a == "rm":
                    mapped.append("/usr/bin/rm")
                elif a in ("sh", "/bin/sh"):
                    mapped.append("/bin/sh")
                else:
                    mapped.append(a)
            out = subprocess.run(["/usr/bin/pkexec", *mapped],
                                 capture_output=True, text=True, timeout=timeout,
                                 env={'SHELL':'/bin/sh'})
            if out.returncode == 0:
                return True, (out.stdout.strip() or "OK")
            return False, (out.stderr.strip() or out.stdout.strip() or f"rc={out.returncode}")
        except Exception as e:
            return False, f"pkexec exception: {e}"

    def _current_uuid_hex(self) -> Optional[str]:
        """
        Best guess of *our* own node UUID, for purge() etc.
        """
        try:
            from .util import NODE_UUID_FILE
            if NODE_UUID_FILE.exists():
                import json
                j = json.loads(NODE_UUID_FILE.read_text())
                hx = (j.get("uuid_hex") or "").strip().lower()
                if len(hx) == 32:
                    return hx
        except Exception:
            pass

        if self.node_path and self.node_path.startswith("/org/bluez/mesh/node"):
            cand = self.node_path.rsplit("node", 1)[-1].lower()
            if len(cand) == 32 and all(c in "0123456789abcdef" for c in cand):
                return cand

        return None

    # ---------------- GUI hooks ----------------

    def set_gui_callbacks(self, log_cb=None, scan_cb=None):
        if log_cb:
            self._log_cb = log_cb
        if scan_cb:
            self._scan_cb = scan_cb

    def log(self, msg: str):
        try:
            self._log_cb(msg)
        finally:
            pass

    # ---------------- export objects ----------------

    def export(self):
        self.bus.register_object(APP_ROOT,   self.app,   type(self.app).__dbus_xml__)
        self.bus.register_object(AGENT_PATH, self.agent, type(self.agent).__dbus_xml__)
        self.bus.register_object(ELEM0_PATH, self.elem0, type(self.elem0).__dbus_xml__)
        self.app._children = build_object_manager_map(self.app, self.agent, self.elem0)
        self.log(f"Exported objects under {APP_ROOT}")

    # ---------------- GLib loop ----------------

    def start_glib_thread(self):
        if self._glib_loop:
            return
        self._glib_loop = GLib.MainLoop()
        threading.Thread(target=self._glib_loop.run, daemon=True).start()
        self.log("GLib main loop started (background thread)")

    # ---------------- network lifecycle ----------------

    def create_network(self):
        """
        Create our provisioner node in bluetooth-meshd.
        If AlreadyExists, recover the token and attach.
        """
        existing = load_token()
        if existing is not None:
            self.log(f"CreateNetwork: token already present ({existing}); doing Attach instead")
            return self.attach(existing)

        from gi.repository import GLib
        import os, json, subprocess

        dev_uuid = default_node_uuid_bytes()
        self._pending_uuid = dev_uuid
        uhex = dev_uuid.hex()
        self.log(f"CreateNetwork UUID={uhex} (len={len(dev_uuid)})")
        if len(dev_uuid) != 16:
            return False, "CreateNetwork: bad UUID length"

        # helper: read node.json via pkexec to grab token if node already exists
        def _recover_token(uuid_hex: str):
            try:
                out = subprocess.run(
                    ["/usr/bin/pkexec", "/bin/sh", "-c",
                     f"cat /var/lib/bluetooth/mesh/{uuid_hex}/node.json 2>/dev/null"],
                    capture_output=True, text=True, timeout=20
                )
                if out.returncode != 0:
                    self.log(f"pkexec cat node.json failed: {out.stderr.strip() or out.stdout.strip()}")
                    return None
                j = json.loads(out.stdout)
                tok_hex = (j.get("token") or "").strip().lower().removeprefix("0x")
                if len(tok_hex) != 16:
                    return None
                return int(tok_hex, 16)
            except Exception as e:
                self.log(f"token recovery exception: {e}")
                return None

        mesh = self._get_mesh()

        # fast path: CreateNetwork()
        try:
            try:
                mesh.CreateNetwork(APP_ROOT, GLib.Variant('ay', dev_uuid))
                return True, "CreateNetwork: OK"
            except Exception as e1:
                try:
                    mesh.CreateNetwork(APP_ROOT, list(dev_uuid))
                    return True, "CreateNetwork: OK"
                except Exception as e2:
                    s1, s2 = f"{e1}", f"{e2}"
                    if "AlreadyExists" in s1 or "AlreadyExists" in s2:
                        self.log("CreateNetwork: AlreadyExists — attempting token recovery…")
                        try:
                            persist_node_uuid(dev_uuid)
                            self.log(f"Persisted node UUID: {uhex}")
                        except Exception as pe:
                            self.log(f"Persist UUID failed: {pe}")
                        tok = _recover_token(uhex)
                        if tok is None:
                            return False, "CreateNetwork: node exists; token recovery failed"
                        save_token(tok)
                        self.log(f"Recovered token=0x{tok:016x}; attempting Attach…")
                        return self.attach(tok)
                    self.log(f"CreateNetwork fast path failed: ay='{e1}', list[int]='{e2}'")
        except Exception as e:
            self.log(f"CreateNetwork failed: {e}")

        # fallback: Import()
        dev_key = os.urandom(16)
        net_key = os.urandom(16)
        flags = {"IvUpdate": GLib.Variant('b', False), "KeyRefresh": GLib.Variant('b', False)}
        try:
            mesh.Import(APP_ROOT, bytes(dev_uuid), bytes(dev_key), bytes(net_key),
                        0, flags, 0, 0x0001)
            return True, "Import: OK"
        except Exception as e:
            es = f"{e}"
            if "AlreadyExists" in es:
                self.log("Import: AlreadyExists — attempting token recovery…")
                try:
                    persist_node_uuid(dev_uuid)
                    self.log(f"Persisted node UUID: {uhex}")
                except Exception as pe:
                    self.log(f"Persist UUID failed: {pe}")
                tok = _recover_token(uhex)
                if tok is None:
                    return False, "Import: node exists; token recovery failed"
                save_token(tok)
                self.log(f"Recovered token=0x{tok:016x}; attempting Attach…")
                return self.attach(tok)
            msg = f"Import failed: {e}"
            self.log(msg)
            return False, msg

    def attach(self, token: Optional[int] = None):
        """
        Attach to our existing provisioner node using the mesh token.
        """
        if self.is_attached:
            return True, "Attach: already attached"
        if self._attach_in_progress:
            return False, "Attach: already in progress"

        tok = token if token is not None else load_token()
        if tok is None:
            raise RuntimeError("No token found. Run create_network() first.")

        self.log(f"Attach(APP_ROOT, token={tok})")
        self._attach_in_progress = True

        def do_attach():
            try:
                node, _cfg = self._get_mesh().Attach(APP_ROOT, int(tok))
                self.node_path = str(node)
                self.mgmt = self.bus.get(MESH_BUS, self.node_path)
                self._rx_enabled = True  # <--- enable RX on successful attach
                self.log(f"Attached. Node path: {self.node_path}")
            except Exception as e:
                emsg = e.args[0] if e.args else str(e)
                if ("org.bluez.mesh.Error.AlreadyExists" in emsg or
                    "org.bluez.mesh.Error.Busy" in emsg):
                    if self.node_path:
                        self.mgmt = self.bus.get(MESH_BUS, self.node_path)
                        self._rx_enabled = True
                        self.log("Attach: daemon reports already attached; rebound mgmt proxy.")
                    else:
                        raise
                else:
                    raise
            finally:
                self._attach_in_progress = False

        ok, msg = self._safe_call(do_attach, "Attach")
        if not ok:
            self._attach_in_progress = False
        return ok, msg

    def config_local_client(self) -> tuple[bool, str]:
        """
        One-time setup: Add AppKey(0) + Bind to local Generic OnOff Client (0x1001) on primary elem.
        Idempotent: Skips if already in nodes.json as 'local_provisioner'.
        """

        self._ensure_appkey(app_index=0, net_index=0)

        local_unicast = 0x0001  # Or self.get_local_unicast() if added
        local_elem = 0x0001
        app_idx = 0
        model_id = 0x1001

        db = load_nodes_db()
        if db.get("local_provisioner", {}).get("appkey_bound", False):
            return True, "Local client already configured (skipped)"

        self.log(f"[LocalConfig] Adding AppKey({app_idx}) to local node 0x{local_unicast:04x}")
        ok_add, msg_add = self.add_appkey_to_node(local_unicast, app_index=app_idx, net_index=0, update=False)
        if not ok_add:
            return False, f"Local AppKey add FAILED: {msg_add}"

        self.log(f"[LocalConfig] Binding local Client (0x{model_id:04x}) to AppKey({app_idx})")
        ok_bind, msg_bind = self.cfg_bind_model_sig(local_unicast, local_elem, app_idx, model_id)
        if not ok_bind:
            return False, f"Local Bind FAILED: {msg_bind}"

        db["local_provisioner"] = {"appkey_bound": True, "configured_at": datetime.now(timezone.utc).isoformat()}
        save_nodes_db(db)
        self.log("[LocalConfig] Local client configured! OnOff Sends now work.")
        return True, "Local client configured successfully"

    def detach_local(self):
        """
        Local-only detach: drop mgmt proxy but don't tell daemon to forget us.
        Also disable RX so callbacks become no-ops while detached.
        """
        if not self.is_attached:
            return False, "Detach skipped: not attached."
        def do_detach():
            self.log("Detaching locally (closing mgmt proxy; keeping node_path)")
            self._provisioning_cancel("detached")
            self.mgmt = None
            self._rx_enabled = False   # <--- gate callbacks while 'detached'
        return self._safe_call(do_detach, "Detach(local)")


    def leave_network(self, deep: bool = False):
        """
        Tell bluetooth-meshd to delete our node (Leave(token)),
        clear local token, clear node_uuid.json, and drop local proxies.
        NOTE: We do NOT restart bluetooth-meshd here. `deep` is ignored on purpose
        to keep signature compatibility with callers.
        """
        if getattr(self, "_leave_in_progress", False):
            return False, "Leave already in progress"
        self._leave_in_progress = True
        try:
            tok = load_token()
            if tok is None:
                return False, "Leave skipped: no token found (nothing to forget)"

            def do_leave():
                self.log(f"Leave({tok})")
                self._get_mesh().Leave(int(tok))
                clear_token()
                # also clear persisted node UUID file if present
                try:
                    from .util import NODE_UUID_FILE
                    try:
                        NODE_UUID_FILE.unlink(missing_ok=True)
                    except TypeError:
                        import os
                        try:
                            os.remove(NODE_UUID_FILE)
                        except Exception:
                            pass
                except Exception as e:
                    self.log(f"Leave: failed to clear node_uuid.json: {e}")

                self.mgmt = None
                self.node_path = None
                self._rx_enabled = False
                self.log("Leave: OK (daemon node removed; local token and node_uuid cleared)")

            return self._safe_call(do_leave, "Leave")
        finally:
            self._leave_in_progress = False

    def purge_local_node(self):
        """
        Nuclear option.
        - Does NOT store any password. We 'warm up' polkit once so subsequent pkexec
          calls are usually non-interactive during this app session (polkit caches).
        - Stops meshd, removes node dir, starts meshd, clears local artifacts.
        """
        if getattr(self, "_purge_in_progress", False):
            return False, "Purge already in progress"
        self._purge_in_progress = True
        try:
            uuid_hex = self._current_uuid_hex()
            if not uuid_hex:
                return False, "Purge: no local UUID found (nothing to delete)"

            self.log(f"Purge: target UUID={uuid_hex}")

            # Warm up polkit so subsequent pkexec calls don't re-prompt this session.
            try:
                self._pkexec_warmup()
            except Exception:
                pass  # non-fatal

            ok, msg = self._systemd_stop_meshd()
            if not ok:
                return False, f"Purge: failed to stop meshd ({msg})"

            ok, msg = self._pkexec_run(["rm", "-rf", f"/var/lib/bluetooth/mesh/{uuid_hex}"])
            self.log(f"Purge: rm dir -> {msg}")
            if not ok:
                self._systemd_start_meshd()
                return False, f"Purge: failed to delete node dir ({msg})"

            ok, msg = self._systemd_start_meshd()
            if not ok:
                return False, f"Purge: meshd failed to start ({msg})"

            # local cleanup
            try:
                clear_token()
                from .util import NODE_UUID_FILE, NODES_FILE
                try:
                    NODES_FILE.unlink(missing_ok=True)
                except TypeError:
                    import os
                    try:
                        os.remove(NODES_FILE)
                    except Exception:
                        pass
                try:
                    NODE_UUID_FILE.unlink(missing_ok=True)
                except TypeError:
                    import os
                    try:
                        os.remove(NODE_UUID_FILE)
                    except Exception:
                        pass
                try:
                    (LOG_DIR / "mesh_app.log").unlink(missing_ok=True)
                except TypeError:
                    import os
                    try:
                        os.remove(str(LOG_DIR / "mesh_app.log"))
                    except Exception:
                        pass
                try:
                    (LOG_DIR / "mesh_devkey.log").unlink(missing_ok=True)
                except TypeError:
                    import os
                    try:
                        os.remove(str(LOG_DIR / "mesh_devkey.log"))
                    except Exception:
                        pass
                self.mgmt = None
                self.node_path = None
                self._rx_enabled = False
            except Exception as e:
                self.log(f"Purge: local cleanup warning: {e}")

            return True, "Purge: OK (node dir removed; meshd restarted)"
        finally:
            self._purge_in_progress = False

    # ---------------- scan / provision ----------------
    def scan_start(self, seconds: Optional[int] = None):
        """
        Start UnprovisionedScan only if not currently provisioning.
        Never auto-restart; UI decides when to start again.
        """
        if not self.mgmt:
            raise RuntimeError("Not attached yet")

        # Hard guard: don't scan while AddNode is in-flight.
        if self._prov_active_uuid:
            return False, f"UnprovisionedScan: blocked (provisioning {self._prov_active_uuid} is in progress)"

        # If a scan is already active, say so (idempotent UX).
        if self._scan_active:
            return True, "UnprovisionedScan: already running"

        # Always stop any stray scan first (defensive; no-op if not started).
        try:
            self.scan_stop()
        except Exception:
            pass

        opts: Dict[str, Any] = {}
        if seconds is not None:
            s = max(1, min(int(seconds), 600))
            opts["Seconds"] = GLib.Variant('q', s)
            self.log(f"UnprovisionedScan({s}s)")
        else:
            self.log("UnprovisionedScan({})")

        ok, msg = self._safe_call(lambda: self.mgmt.UnprovisionedScan(opts), "UnprovisionedScan")
        if ok:
            self._scan_active = True
        return ok, msg


    def scan_stop(self):
        """
        Stop scan if running; idempotent.
        """
        if self.mgmt and self._scan_active:
            self.log("UnprovisionedScanCancel()")
            ok, msg = self._safe_call(lambda: self.mgmt.UnprovisionedScanCancel(), "UnprovisionedScanCancel")
            if ok:
                self._scan_active = False
            return ok, msg
        return True, "UnprovisionedScanCancel: already stopped"


    def provision_uuid(self, uuid_hex: str):
        """
        PB-ADV provisioning for a beaconing (unprovisioned) UUID.
        - Strict single-flight (one AddNode at a time).
        - Dedupe exact same UUID requests while in-flight.
        - No app-side timeout; rely on meshd producing AddNode{Complete,Failed}.
        - Do NOT auto-restart scanning afterwards (UI controls scanning).
        """
        if not self.mgmt:
            raise RuntimeError("Not attached yet")

        uh = uuid_hex.replace("-", "").strip().lower()
        if len(uh) != 32 or any(c not in "0123456789abcdef" for c in uh):
            raise ValueError("UUID must be 16 bytes (32 hex chars)")

        # If nodes.json already says the device is done, refuse.
        if self._db_is_final_pub_ok(uh):
            self.log(f"[Provision] UUID {uh} already provisioned (pub_ok); skipping AddNode")
            return False, "Already provisioned (pub_ok)"

        # Single-flight guard
        if self._prov_active_uuid:
            if self._prov_active_uuid == uh:
                self.log(f"[Provision] {uh} already in progress; ignoring duplicate request")
                return False, f"Provision already in progress: {uh}"
            else:
                self.log(f"[Provision] busy with {self._prov_active_uuid}; ignoring request for {uh}")
                return False, f"Provision busy: {self._prov_active_uuid}"

        # Stop scan if running — provisioning and scanning must not overlap.
        if self._scan_active:
            try:
                self.scan_stop()
            except Exception as e:
                self.log(f"Scan-cancel (pre-AddNode) non-fatal: {e}")

        # Mark active (no timer; let meshd drive completion/failure)
        self._prov_active_uuid = uh

        self.log(f"AddNode({uh})")
        ok, msg = self._safe_call(lambda: self.mgmt.AddNode(bytes.fromhex(uh), {}), "AddNode")
        if not ok:
            # Submission failed synchronously -> clear busy flag.
            if self._prov_active_uuid == uh:
                self._prov_active_uuid = None
            return ok, msg
        return ok, msg




    # ---------------- Config Client helpers ----------------

    def _ensure_appkey(self, app_index: int = 0, net_index: int = 0):
        """
        Make sure our local provisioner node has AppKey(app_index).
        """
        def do():
            try:
                self.mgmt.CreateAppKey(int(net_index), int(app_index))
                self.log(f"CreateAppKey: net={net_index}, app={app_index}")
            except Exception as e:
                emsg = e.args[0] if e.args else str(e)
                if "AlreadyExists" in emsg:
                    self.log(f"CreateAppKey: app {app_index} already exists (local)")
                else:
                    raise
        return self._safe_call(do, "CreateAppKey")

    def add_appkey_to_node(self, unicast: int, app_index: int = 0, net_index: int = 0, update: bool = False):
        """
        Tell the newly provisioned node:
        "Install AppKey(app_index) of NetKey(net_index)."
        """
        def do():
            self.mgmt.AddAppKey(
                ELEM0_PATH,
                int(unicast),
                int(app_index),
                int(net_index),
                bool(update),
            )
            self.log(
                f"AddAppKey -> node 0x{unicast:04x} "
                f"(app={app_index}, net={net_index}, update={update})"
            )
        return self._safe_call(do, f"AddAppKey(0x{unicast:04x})")

    def _cfg_send(self, dest_unicast: int, payload: bytes, label: str):
        """
        Low-level: send a DevKey-encrypted Config Client PDU to dest_unicast.
        """
        return self._safe_call(
            lambda: self.mgmt.DevKeySend(
                ELEM0_PATH,
                int(dest_unicast),
                True,          # remote=True (use remote node's DevKey)
                0x000,         # net index 0
                {},            # no options
                payload,
            ),
            label
        )

    def _bind_local_client_sig(self, local_elem_addr: int, app_index: int, model_id: int):
        # Bind LOCAL Generic OnOff Client (0x1001) to AppKey(0) on local primary elem 0x0001
        local_unicast = 0x0001
        try:
            ok, msg = self.cfg_bind_model_sig(local_unicast, local_elem_addr, app_index, model_id)
            return ok, msg
        except Exception as e:
            return False, f"Local bind exception: {e}"

    def _encode_model_app_bind(self, elem_addr: int, app_index: int, model_id: int) -> bytes:
        """
        Config Model App Bind (opcode 0x803D) for SIG models.
        """
        buf = bytearray()
        buf += bytes([0x80, 0x3D])  # opcode
        buf += int(elem_addr).to_bytes(2, "little")
        buf += int(app_index).to_bytes(2, "little")  # AppKeyIndex (12-bit real index)
        buf += int(model_id).to_bytes(2, "little")   # SIG model id (0x1000)
        return bytes(buf)

    def _encode_model_pub_set(
        self,
        elem_addr: int,
        pub_addr: int,
        app_index: int,
        ttl: int,
        period: int,
        retransmit: int,
        cred_flag: bool,
        model_id: int,
    ) -> bytes:
        """
        Build Config Model Publication Set (opcode 0x03) payload.
        """
        field = ((app_index & 0x0FFF) | ((1 if cred_flag else 0) << 12))

        buf = bytearray()
        buf.append(0x03)  # Config Model Publication Set
        buf += int(elem_addr).to_bytes(2, "little")
        buf += int(pub_addr).to_bytes(2, "little")
        buf += int(field).to_bytes(2, "little")
        buf.append(int(ttl) & 0xFF)
        buf.append(int(period) & 0xFF)
        buf.append(int(retransmit) & 0xFF)
        buf += int(model_id).to_bytes(2, "little")
        return bytes(buf)

    def cfg_bind_model_sig(self, dest_unicast: int, elem_addr: int, app_index: int, model_id: int):
        """
        High-level wrapper for sending Config Model App Bind via DevKeySend.
        """
        payload = self._encode_model_app_bind(elem_addr, app_index, model_id)
        self.log(f"[cfg_bind_model_sig] sending Bind payload to 0x{dest_unicast:04x}: {payload.hex()}")
        return self._cfg_send(
            dest_unicast,
            payload,
            f"CfgModelAppBind(0x{dest_unicast:04x})"
        )

    def cfg_pub_set_sig(
        self,
        dest_unicast: int,
        elem_addr: int,
        pub_addr: int,
        app_index: int,
        model_id: int,
        ttl: int = 7,
        period: int = 0x45,         # ~5s
        retransmit: int = 0x00,
        cred_flag: bool = False,
    ):
        """
        Ask the remote node to publish GenericOnOffStatus periodically to pub_addr.
        """
        payload = self._encode_model_pub_set(
            elem_addr,
            pub_addr,
            app_index,
            ttl,
            period,
            retransmit,
            cred_flag,
            model_id,
        )

        self.log(
            f"[cfg_pub_set_sig] sending PubSet payload to 0x{dest_unicast:04x}: {payload.hex()}"
        )

        return self._cfg_send(
            dest_unicast,
            payload,
            f"CfgModelPubSet(0x{dest_unicast:04x})"
        )

    def reset_remote_node(self, unicast_str: str):
        """
        Send Config Node Reset (0x8049) to wipe a node from the mesh.
        We'll also mark nodes.json as reset_sent.
        """
        if not self.mgmt:
            raise RuntimeError("Not attached yet")

        s = unicast_str.strip().lower()
        try:
            if s.startswith("0x"):
                dest = int(s, 16)
            elif all(c in "0123456789abcdef" for c in s) and len(s) <= 4:
                dest = int(s, 16)
            else:
                dest = int(s, 10)
        except ValueError:
            raise ValueError("Unicast must be hex (0x1201 / 1201) or decimal.")
        if not (0x0001 <= dest <= 0x7FFF):
            raise ValueError("Unicast out of range (0x0001..0x7FFF).")

        opcode = bytes([0x80, 0x49])  # Config Node Reset

        ok, msg = self._safe_call(
            lambda: self.mgmt.DevKeySend(ELEM0_PATH, dest, True, 0x000, {}, opcode),
            "ConfigNodeReset"
        )

        # update db state -> reset_sent
        try:
            db = load_nodes_db()
            now_iso = datetime.now(timezone.utc).isoformat()
            for uuid_hex, entry in db.items():
                if int(entry.get("unicast", -1)) == dest:
                    entry["state"] = "reset_sent"
                    entry["last_seen"] = now_iso
            save_nodes_db(db)
        except Exception as e:
            self.log(f"reset_remote_node: couldn't update nodes.json: {e}")

        return ok, msg

    # ---------------- provisioning FSM + nodes.json helpers ----------------

    def _provisioning_cancel(self, reason: str = "cancelled"):
        """
        Best-effort cancel for explicit user actions (e.g., detach).
        We do NOT call this for duplicate provision_uuid anymore.
        """
        uuid = self._prov_active_uuid
        self._prov_active_uuid = None

        try:
            if self._prov_timeout_src is not None:
                GLib.source_remove(self._prov_timeout_src)
        except Exception:
            pass
        self._prov_timeout_src = None

        try:
            if self._scan_active:
                self.scan_stop()
        except Exception:
            pass
        try:
            if self.mgmt and hasattr(self.mgmt, "Cancel"):
                self.mgmt.Cancel({})
        except Exception:
            pass

        if uuid:
            self.log(f"[Provision] canceled in-flight job for {uuid} ({reason})")


    def _persist_node_basic(self, uuid_hex: str, unicast: int, elements: int):
        """
        Make sure nodes.json has the basic info for this node.
        Mark 'state' as 'provisioning'.
        """
        try:
            db = load_nodes_db()
            now_iso = datetime.now(timezone.utc).isoformat()

            entry = db.get(uuid_hex, {})
            entry.update({
                "unicast": int(unicast),
                "elements": int(elements),
                "primary_elem": int(unicast),
                "model_id": "0x1000",
                "app_idx": 0,
                "provisioned_at": entry.get("provisioned_at", now_iso),
                "last_seen": entry.get("last_seen", None),
                "last_onoff": entry.get("last_onoff", None),
                "state": "provisioning",
            })
            db[uuid_hex] = entry
            save_nodes_db(db)
            self.log(f"[persist] wrote/updated node {uuid_hex} -> nodes.json")
        except Exception as e:
            self.log(f"[persist] failed to save nodes.json: {e}")

    def _mark_node_stage(self, uuid_hex: str, stage: str, extra: Dict[str, Any] | None = None):
        """
        Update node's 'state' and bump last_seen.
        """
        try:
            db = load_nodes_db()
            entry = db.get(uuid_hex, {})
            entry["state"] = stage
            entry["last_seen"] = datetime.now(timezone.utc).isoformat()
            if extra:
                entry.update(extra)
            db[uuid_hex] = entry
            save_nodes_db(db)
            self.log(f"[persist] node {uuid_hex} -> state={stage}")
        except Exception as e:
            self.log(f"[persist] failed to update node stage: {e}")

    def _fsm_send_bind(self, unicast: int):
        """
        Send Config Model App Bind to bind AppKey(0) to GenericOnOff Server (0x1000).
        """
        job = self._provision_jobs.get(unicast)
        if not job or not self.mgmt:
            return

        ok, msg = self.cfg_bind_model_sig(
            dest_unicast=unicast,
            elem_addr=job["elem_addr"],
            app_index=job["app_idx"],
            model_id=job["model_id"],
        )

        if ok:
            self.log(f"[FSM] Bind sent to 0x{unicast:04x}")
            job["stage"] = "bind_sent"
        else:
            self.log(f"[FSM] Bind send failed to 0x{unicast:04x}: {msg}")

    def _fsm_send_pubset(self, unicast: int):
        """
        Step 3 of provisioning FSM.
        """
        job = self._provision_jobs.get(unicast)
        if not job or not self.mgmt:
            return

        elem_addr = job["elem_addr"]
        model_id  = job["model_id"]
        app_idx   = job["app_idx"]
        pub_addr  = 0x0001

        ok_pub, msg_pub = self.cfg_pub_set_sig(
            dest_unicast=unicast,
            elem_addr=elem_addr,
            pub_addr=pub_addr,
            app_index=app_idx,
            model_id=model_id,
            ttl=7,
            period=0x45,
            retransmit=0x00,
            cred_flag=False,
        )

        if ok_pub:
            self.log(f"[FSM] PubSet sent to 0x{unicast:04x}")
            job["stage"] = "pub_sent"
        else:
            self.log(f"[FSM] PubSet send failed to 0x{unicast:04x}: {msg_pub}")

    def _decode_config_status(self, data: bytes) -> str:
        """
        Decode common Config Client status PDUs for nicer logs.
        """
        if len(data) < 2:
            return "too-short"

        op0 = data[0]
        op1 = data[1]

        # 0x8003 AppKey Status
        if op0 == 0x80 and op1 == 0x03:
            status = data[2] if len(data) > 2 else None
            return f"AppKeyStatus status=0x{status:02x}" if status is not None else "AppKeyStatus <short>"

        # 0x803E Model App Status (Bind result)
        if op0 == 0x80 and op1 == 0x3E:
            status = data[2] if len(data) > 2 else None
            return f"ModelAppStatus(Bind) status=0x{status:02x}" if status is not None else "ModelAppStatus(Bind) <short>"

        # 0x8019 Model Publication Status
        if op0 == 0x80 and op1 == 0x19:
            status = data[2] if len(data) > 2 else None
            return f"ModelPubStatus status=0x{status:02x}" if status is not None else "ModelPubStatus <short>"

        # ---- NEW: 0x804A Node Reset Status
        if op0 == 0x80 and op1 == 0x4A:
            return "NodeResetStatus"

        return f"unknown opcode {data[0:2].hex()}"

    def _handle_config_status_from_node(self, src_unicast: int, data: bytes):
        """
        Advance the provisioning FSM based on config status PDUs from the node.
        """
        job = self._provision_jobs.get(src_unicast)

        if len(data) < 2:
            return

        op0, op1 = data[0], data[1]

        # 0x8003 = Config AppKey Status
        if op0 == 0x80 and op1 == 0x03:
            if job and len(data) >= 3:
                status = data[2]
                if status == 0x00:
                    self.log(f"[FSM] got AppKeyStatus OK from 0x{src_unicast:04x} -> send Bind next")
                    job["stage"] = "appkey_ok"
                    self._mark_node_stage(job["uuid"], "appkey_ok")
                    self._fsm_send_bind(src_unicast)
            return

        # 0x803E = Config Model App Status (Bind)
        if op0 == 0x80 and op1 == 0x3E:
            if job and len(data) >= 3:
                status = data[2]
                if status == 0x00:
                    self.log(f"[FSM] got Bind OK from 0x{src_unicast:04x} -> send PubSet next")
                    job["stage"] = "bind_ok"
                    self._mark_node_stage(job["uuid"], "bind_ok")
                    self._fsm_send_pubset(src_unicast)
            return

        # 0x8019 = Config Model Publication Status (PubSet)
        if op0 == 0x80 and op1 == 0x19:
            if job and len(data) >= 3:
                status = data[2]
                if status == 0x00:
                    self.log(f"[FSM] got PubSet OK from 0x{src_unicast:04x} -> provisioning DONE 🎉")
                    job["stage"] = "done"
                    self._mark_node_stage(job["uuid"], "pub_ok")
                    ok_get, msg_get = self.send_onoff_get(src_unicast, app_idx=0)
                    if ok_get:
                        self.log(f"[FSM] sent GenericOnOffGet to 0x{src_unicast:04x}, waiting for Status…")
                    else:
                        self.log(f"[FSM] failed to send GenericOnOffGet to 0x{src_unicast:04x}: {msg_get}")
            return

        # ---- NEW: 0x804A = Node Reset Status (ack for 0x8049)
        if op0 == 0x80 and op1 == 0x4A:
            self.log(f"[Reset] Node 0x{src_unicast:04x} acknowledged reset (0x804A). Marking reset_ok.")
            try:
                db = load_nodes_db()
                now_iso = datetime.now(timezone.utc).isoformat()
                for uuid_hex, entry in list(db.items()):
                    if int(entry.get("unicast", -1)) == src_unicast:
                        entry["state"] = "reset_ok"
                        entry["last_seen"] = now_iso
                        # optional: clear last_onoff after reset
                        entry.pop("last_onoff", None)
                        db[uuid_hex] = entry
                        break
                save_nodes_db(db)
            except Exception as e:
                self.log(f"[Reset] failed to update nodes.json: {e}")

            # clean up any pending FSM job for this node
            try:
                self._provision_jobs.pop(src_unicast, None)
            except Exception:
                pass
            return

    def _db_state_for_uuid(self, uuid_hex: str) -> Optional[str]:
        try:
            db = load_nodes_db()
            s = db.get(uuid_hex, {}).get("state")
            if isinstance(s, dict):
                s = s.get("value") or s.get("name") or s.get("state") or s.get("status")
            return (str(s).strip().lower() if s else None)
        except Exception:
            return None

    def _db_is_final_pub_ok(self, uuid_hex: str) -> bool:
        return (self._db_state_for_uuid(uuid_hex) in ("pub_ok", "done"))

    def _provisioning_busy(self) -> bool:
        return self._prov_active_uuid is not None

    def _provisioning_begin(self, uuid_hex: str, timeout_sec: int = 0):
        # kept for compatibility with callers; we just set the active UUID now
        self._prov_active_uuid = uuid_hex
        # no timers in no-timeout mode
        try:
            if self._prov_timeout_src is not None:
                GLib.source_remove(self._prov_timeout_src)
        except Exception:
            pass
        self._prov_timeout_src = None

    def _provisioning_end_if(self, uuid_hex: str):
        if self._prov_active_uuid == uuid_hex:
            self._prov_active_uuid = None
        try:
            if self._prov_timeout_src is not None:
                GLib.source_remove(self._prov_timeout_src)
        except Exception:
            pass
        self._prov_timeout_src = None


    def _provisioning_start_next_if_any(self):
        if self._prov_active_uuid is None and self._prov_queue:
            nxt = self._prov_queue.pop(0)
            self.log(f"[Provision] starting queued UUID {nxt}")
            # go through public path for the same checks
            try:
                self.provision_uuid(nxt)
            except Exception as e:
                self.log(f"[Provision] failed to start queued {nxt}: {e}")
                # try the next one if this entry was bad
                self._provisioning_start_next_if_any()

    def _resume_scan_if_needed(self):
        # resume only if we paused it for provisioning
        if self._scan_should_resume:
            self._scan_should_resume = False
            try:
                ok, msg = self.scan_start()
                if not ok:
                    self.log(f"[Scan] resume failed: {msg}")
            except Exception as e:
                self.log(f"[Scan] resume exception: {e}")


    # ---------------- callbacks from bluetooth-meshd ----------------

    def on_join_complete(self, token: int):
        """
        bluetooth-meshd: our own node was created successfully.
        Save token, persist our UUID, and auto-attach.
        """
        self.log(f"JoinComplete token=0x{token:016x}")
        save_token(token)

        if self._pending_uuid is not None:
            try:
                persist_node_uuid(self._pending_uuid)
                self.log(f"Persisted node UUID: {self._pending_uuid.hex()}")
            except Exception as e:
                self.log(f"Persist UUID failed: {e}")
            finally:
                self._pending_uuid = None

        try:
            self.attach(token)
        except Exception as e:
            self.log(f"Auto-attach failed: {e}")

    def on_join_failed(self, reason: str):
        self.log(f"JoinFailed: {reason}")

    def on_scan_result(self, rssi: int, adv: bytes, options: Optional[Dict[str, Any]]):
        """
        bluetooth-meshd Provisioner1.ScanResult callback.
        """
        self.log(f"[Scan] rssi={rssi} len={len(adv)} adv={adv.hex()[:64]}...")
        uuid_hex = parse_unprov_uuid(adv)
        if uuid_hex:
            self.log(f"[Scan] unprov UUID={uuid_hex}")
            self._scan_cb(uuid_hex, rssi)
        else:
            self.log(f"[Scan] no UUID parsed (rssi={rssi}) adv={adv.hex()}")

    def alloc_unicast(self, count: int) -> int:
        """
        bluetooth-meshd asks us for a unicast block for a new device.
        """
        start = self._next_unicast
        self._next_unicast += int(count)
        self.log(f"Alloc unicast: 0x{start:04x}..+{count-1}")
        return start

    # ---------------- callbacks from bluetooth-meshd ----------------

    def on_add_node_complete(self, uuid_bytes: bytes, unicast: int, count: int):
        """
        Provisioner1.AddNodeComplete callback.
        - Clears single-flight busy flag.
        - Does NOT auto-restart scanning.
        """
        if not self._rx_enabled:
            self.log("[FSM] ignoring AddNodeComplete while locally detached")
            return

        uuid_hex = uuid_bytes.hex()
        self.log(f"AddNodeComplete: uuid={uuid_hex} unicast=0x{unicast:04x} elements={count}")

        # Clear busy flag if this was our in-flight UUID.
        if self._prov_active_uuid == uuid_hex:
            self._prov_active_uuid = None

        # Create/refresh FSM job
        self._provision_jobs[unicast] = {
            "uuid": uuid_hex,
            "stage": "provisioning",
            "model_id": 0x1000,   # Generic OnOff Server
            "app_idx": 0,         # AppKey(0)
            "elem_addr": unicast, # element 0
        }

        # Persist basics
        self._persist_node_basic(uuid_hex, unicast, count)

        # Kick off config steps (AppKey Add) in a thread
        def _kickoff_cfg():
            self._ensure_appkey(app_index=0, net_index=0)
            ok, msg = self.add_appkey_to_node(unicast, app_index=0, net_index=0, update=False)
            if ok:
                self.log(f"[FSM] AppKey Add sent to 0x{unicast:04x}")
                self._provision_jobs[unicast]["stage"] = "appkey_sent"
            else:
                self.log(f"[FSM] AppKey Add FAILED to 0x{unicast:04x}: {msg}")

        # Best-effort: ensure scanning is stopped (may already be)
        try:
            if self._scan_active:
                self.mgmt.UnprovisionedScanCancel()
                self._scan_active = False
                self.log("[FSM] ensured scan stopped after AddNodeComplete")
        except Exception as e:
            self.log(f"[FSM] scan-cancel post-complete non-fatal: {e}")

        threading.Thread(target=_kickoff_cfg, daemon=True).start()


    def on_add_node_failed(self, uuid_bytes: bytes, reason: str):
        """
        Provisioner1.AddNodeFailed callback.
        - Clears single-flight busy flag.
        - Does NOT auto-restart scanning.
        """
        if not self._rx_enabled:
            self.log("[FSM] ignoring AddNodeFailed while locally detached")
            return

        uuid_hex = uuid_bytes.hex()
        self.log(f"AddNodeFailed: uuid={uuid_hex} reason={reason}")

        if self._prov_active_uuid == uuid_hex:
            self._prov_active_uuid = None

        # Leave scan state unchanged — UI decides whether to scan again.


    def on_element_message(self, source: int, key_index: int, destination, data: bytes):
        """
        Element1.MessageReceived: application-layer (AppKey-encrypted) msg.
        We'll specifically interpret GenericOnOffStatus (0x82 0x04).
        """
        # ---- NEW: gate messages while "detached" locally ----
        if not self._rx_enabled:
            return

        try:
            info = "unknown"
            last_onoff_val = None

            # Generic OnOff Status = 0x82 0x04 <presentOnOff>
            if len(data) >= 3 and data[0] == 0x82 and data[1] == 0x04:
                last_onoff_val = data[2]
                info = f"GenericOnOffStatus(on={last_onoff_val})"

            line = (
                f"APPMSG src=0x{source:04x} app_idx={key_index} "
                f"len={len(data)} data={data.hex()} dec={info}"
            )
            self.log(line)
            (LOG_DIR / "mesh_app.log").open("a", encoding="utf-8").write(line + "\n")

            # persist telemetry in nodes.json
            if last_onoff_val is not None:
                now_iso = datetime.now(timezone.utc).isoformat()
                try:
                    db = load_nodes_db()
                    for uuid_hex, entry in db.items():
                        if int(entry.get("unicast", -1)) == int(source):
                            entry["last_onoff"] = int(last_onoff_val)
                            entry["last_seen"] = now_iso
                            db[uuid_hex] = entry
                            break
                    save_nodes_db(db)
                except Exception as e:
                    self.log(f"[Element0] update nodes.json failed: {e}")

        except Exception as e:
            self.log(f"[Element0] MessageReceived error: {e}")

    def on_element_devkey_message(self, source: int, remote: bool, net_index: int, data: bytes):
        """
        Element1.DevKeyMessageReceived: config/status messages encrypted with DevKey.
        This is where nodes answer AppKey Add / Bind / PubSet / Reset.
        """
        # ---- NEW: gate messages while "detached" locally ----
        if not self._rx_enabled:
            return

        try:
            decoded = self._decode_config_status(data)
            line = (
                f"DEVKEY src=0x{source:04x} remote={remote} "
                f"net_idx=0x{net_index:03x} len={len(data)} data={data.hex()} dec={decoded}"
            )
            self.log(line)
            (LOG_DIR / "mesh_devkey.log").open("a", encoding="utf-8").write(line + "\n")
        except Exception as e:
            self.log(f"[Element0] DevKeyMessageReceived error: {e}")

        # drive the provisioning FSM forward (+ reset ack)
        try:
            self._handle_config_status_from_node(source, data)
        except Exception as e:
            self.log(f"[FSM] error while handling config status: {e}")
                
    def send_onoff_get(self, dest_unicast: int, app_idx: int = 0):
        """
        Ask a node for its Generic OnOff state (Generic OnOff Get, opcode 0x8201).
        We send this over AppKey 'app_idx' (we always use 0 so far).
        """
        if not self.is_attached:
            raise RuntimeError("Not attached yet")

        payload = bytes([0x82, 0x01])

        self.log(f"[debug] sending GenericOnOff Get -> 0x{dest_unicast:04x}")

        try:
            return self._safe_call(
                lambda: self.mgmt.Send(
                    ELEM0_PATH,
                    int(dest_unicast),
                    int(app_idx),
                    {},
                    payload,
                ),
                f"OnOffGet(0x{dest_unicast:04x})"
            )
        except Exception as e:
            self.log(f"[OnOffGet] Proxy error: {e}")
            return False, f"OnOffGet proxy failed: {e}"

    # ---------------- utilities ----------------
        
    def _safe_call(self, fn, label: str):
        try:
            with self._dbus_lock:
                fn()
            return True, f"{label}: OK"
        except Exception as e:
            emsg = e.args[0] if e.args else str(e)
            self.log(f"{label} failed: {emsg}")
            return False, f"{label} failed: {emsg}"

    @property
    def is_attached(self) -> bool:
        return self.node_path is not None and self.mgmt is not None
