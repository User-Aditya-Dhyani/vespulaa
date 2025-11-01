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

        def _get_elem_proxy(self):
            if self.elem_proxy is not None:
                return self.elem_proxy
            if not self.node_path:
                raise RuntimeError("Not attached yet")
            elem_path = f"{self.node_path}/element0"  # Standard BlueZ Element1 path
            self.elem_proxy = self.bus.get(MESH_BUS, elem_path)
            return self.elem_proxy

        # GUI callbacks for logging + scan table
        self._log_cb: Callable[[str], None] = lambda s: print(s)
        self._scan_cb: Callable[[str, int], None] = lambda uuid_hex, rssi: None

        # ---------- persistent unicast allocator ----------
        # read nodes.json and find the highest "end" address we've used;
        # bump from there so we don't reuse 0x0005 every run
        db = load_nodes_db()
        max_used = 0x0004
        for info in db.values():
            try:
                base = int(info.get("unicast", 0))
                elems = int(info.get("elements", 1))
                end = base + elems  # first free addr after that node's block
                if end > max_used:
                    max_used = end
            except Exception:
                pass

        # propose a safe floor well above our own element address.
        # 0x0010 worked for you. You could even pick 0x0100 if you want to be extra.
        floor_start = 0x0010

        nxt = max_used + 1
        if nxt < floor_start:
            nxt = floor_start

        self._next_unicast = nxt
        # now alloc_unicast() will hand out >= 0x0010 every time


        # GLib main loop for async callbacks from meshd
        self._glib_loop: Optional[GLib.MainLoop] = None

        # our provisioning FSM state for each fresh node
        # key = unicast int, val = dict:
        # {
        #    "uuid": "<hex uuid>",
        #    "stage": "provisioning" | "appkey_sent" | "appkey_ok"
        #             | "bind_sent" | "bind_ok"
        #             | "pub_sent"  | "done",
        #    "model_id": 0x1000,     # Generic OnOff Server
        #    "app_idx": 0,           # AppKey index we use
        #    "elem_addr": <unicast>, # primary element address
        # }
        self._provision_jobs: Dict[int, Dict[str, Any]] = {}
        try:
            (LOG_DIR / "mesh_app.log").touch(exist_ok=True)
            (LOG_DIR / "mesh_devkey.log").touch(exist_ok=True)
        except Exception:
            pass
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
                self.log(f"Attached. Node path: {self.node_path}")
            except Exception as e:
                emsg = e.args[0] if e.args else str(e)
                if ("org.bluez.mesh.Error.AlreadyExists" in emsg or
                    "org.bluez.mesh.Error.Busy" in emsg):
                    # already attached -> just grab proxy
                    if self.node_path:
                        self.mgmt = self.bus.get(MESH_BUS, self.node_path)
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

    def detach_local(self):
        """
        Local-only detach: drop mgmt proxy but don't tell daemon to forget us.
        """
        if not self.is_attached:
            return False, "Detach skipped: not attached."
        def do_detach():
            self.log("Detaching locally (closing mgmt proxy; keeping node_path)")
            self.mgmt = None
        return self._safe_call(do_detach, "Detach(local)")

    def leave_network(self, deep: bool = False):
        """
        Tell bluetooth-meshd to delete our node (Leave(token)),
        clear local token, and optionally restart the daemon.
        """
        tok = load_token()
        if tok is None:
            return False, "Leave skipped: no token found (nothing to forget)"

        def do_leave():
            self.log(f"Leave({tok})")
            self._get_mesh().Leave(int(tok))
            clear_token()
            self.mgmt = None
            self.node_path = None
            self.log("Leave: OK (daemon node removed; local token cleared)")

        ok, msg = self._safe_call(do_leave, "Leave")
        if not ok or not deep:
            return ok, msg

        ok2, msg2 = self._systemd_restart_meshd()
        if ok2:
            return True, "Leave: OK (daemon restarted)"
        else:
            self.log("Run manually:\n  sudo systemctl restart bluetooth-meshd")
            return True, "Leave: node removed; restart daemon manually (see log)"

    def purge_local_node(self):
        """
        Nuclear option:
          - stop meshd via systemd DBus
          - pkexec rm -rf /var/lib/bluetooth/mesh/<uuid>
          - start meshd
          - wipe local token / node_path cache
        """
        uuid_hex = self._current_uuid_hex()
        if not uuid_hex:
            return False, "Purge: no local UUID found (nothing to delete)"

        self.log(f"Purge: target UUID={uuid_hex}")

        ok, msg = self._systemd_stop_meshd()
        if not ok:
            return False, f"Purge: failed to stop meshd ({msg})"

        ok, msg = self._pkexec_run(["rm", "-rf", f"/var/lib/bluetooth/mesh/{uuid_hex}"])
        self.log(f"Purge: rm dir -> {msg}")
        if not ok:
            # try to restart anyway so we don't leave daemon down
            self._systemd_start_meshd()
            return False, f"Purge: failed to delete node dir ({msg})"

        ok, msg = self._systemd_start_meshd()
        if not ok:
            return False, f"Purge: meshd failed to start ({msg})"

        # local cleanup
        try:
            clear_token()
            from .util import NODE_UUID_FILE
            try:
                NODE_UUID_FILE.unlink(missing_ok=True)
            except TypeError:
                import os
                try:
                    os.remove(NODE_UUID_FILE)
                except Exception:
                    pass
            self.mgmt = None
            self.node_path = None
        except Exception as e:
            self.log(f"Purge: local cleanup warning: {e}")

        return True, "Purge: OK (node dir removed; meshd restarted)"

    # ---------------- scan / provision ----------------

    def scan_start(self, seconds: Optional[int] = None):
        if not self.mgmt:
            raise RuntimeError("Not attached yet")
        opts: Dict[str, Any] = {}
        if seconds is not None:
            s = max(1, min(int(seconds), 600))
            opts["Seconds"] = GLib.Variant('q', s)
            self.log(f"UnprovisionedScan({s}s)")
        else:
            self.log("UnprovisionedScan({})")
        return self._safe_call(lambda: self.mgmt.UnprovisionedScan(opts), "UnprovisionedScan")

    def scan_stop(self):
        if self.mgmt:
            self.log("UnprovisionedScanCancel()")
            return self._safe_call(lambda: self.mgmt.UnprovisionedScanCancel(), "UnprovisionedScanCancel")

    def provision_uuid(self, uuid_hex: str):
        """
        Start PB-ADV provisioning for a beaconing (unprovisioned) UUID.
        We no longer cancel the scan before calling AddNode.
        """
        if not self.mgmt:
            raise RuntimeError("Not attached yet")

        uh = uuid_hex.replace("-", "").strip().lower()
        if len(uh) != 32 or any(c not in "0123456789abcdef" for c in uh):
            raise ValueError("UUID must be 16 bytes (32 hex chars)")

        self.log(f"AddNode({uh})")
        return self._safe_call(
            lambda: self.mgmt.AddNode(bytes.fromhex(uh), {}),
            "AddNode"
        )

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
        Under the hood this is Config AppKey Add.
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

    def _encode_model_app_bind(self, elem_addr: int, app_index: int, model_id: int) -> bytes:
        """
        Config Model App Bind (opcode 0x803D) for SIG models.
        """
        buf = bytearray()
        buf += bytes([0x80, 0x3D])  # opcode: Config Model App Bind
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

        Layout per spec:
          0:      0x03
          1-2:    element_addr   (LE)
          3-4:    publish_addr   (LE)
          5-6:    AppKeyIndex (12 bits) | CredFlag (1 bit)  (LE)
          7:      ttl
          8:      period byte (pub period: (res<<6)|steps)
          9:      retransmit
          10-11:  model_id (SIG model -> 2 bytes LE)
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
        period: int = 0x45,         # <-- 0x45 means ~5s period (RES=1s, steps=5)
        retransmit: int = 0x00,
        cred_flag: bool = False,
    ):
        """
        Ask the remote node to publish GenericOnOffStatus periodically to pub_addr
        using AppKey app_index.

        dest_unicast: address of the *remote* node we just provisioned
        elem_addr:    its element address (usually same as dest_unicast)
        pub_addr:     who it should publish to (we use our provisioner addr 0x0001)
        model_id:     SIG model ID (0x1000 = Generic OnOff Server)
        period:       publish period byte. 0x45 => every ~5 seconds.
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
                "model_id": "0x1000",   # we're managing Generic OnOff Server
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
        Update node's 'state' field in nodes.json to reflect where we are
        (appkey_ok, bind_ok, pub_ok, etc) and bump last_seen timestamp.
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
        Step 3 of provisioning FSM:
        tell the node to periodically publish GenericOnOffStatus back to us.
        """
        job = self._provision_jobs.get(unicast)
        if not job or not self.mgmt:
            return

        elem_addr = job["elem_addr"]
        model_id  = job["model_id"]
        app_idx   = job["app_idx"]

        # our own primary element is 0x0001, so tell the new node
        # "publish your GenericOnOffStatus to 0x0001"
        pub_addr    = 0x0001

        ok_pub, msg_pub = self.cfg_pub_set_sig(
            dest_unicast=unicast,
            elem_addr=elem_addr,
            pub_addr=pub_addr,
            app_index=app_idx,
            model_id=model_id,
            ttl=7,
            period=0x45,        # <-- IMPORTANT: nonzero, ~5s period
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

        return f"unknown opcode {data[0:2].hex()}"

    def _handle_config_status_from_node(self, src_unicast: int, data: bytes):
        """
        Advance the provisioning FSM based on config status PDUs from the node.

        We care about three opcodes:

        - 0x8003: Config AppKey Status
            -> means the node accepted AppKey(0)
            -> next step: Bind AppKey(0) to Generic OnOff Server (0x1000)

        - 0x803E: Config Model App Status
            -> means the Bind succeeded
            -> next step: set Publication so it publishes GenericOnOffStatus to us

        - 0x8019: Config Model Publication Status
            -> means PubSet succeeded
            -> provisioning DONE 🎉
            -> ask node for its current GenericOnOff state immediately
        """
        job = self._provision_jobs.get(src_unicast)
        if not job:
            return  # not a node we're actively provisioning

        if len(data) < 2:
            return

        op0, op1 = data[0], data[1]

        # 0x8003 = Config AppKey Status
        if op0 == 0x80 and op1 == 0x03:
            if len(data) >= 3:
                status = data[2]
                if status == 0x00:
                    # AppKey Add accepted
                    self.log(
                        f"[FSM] got AppKeyStatus OK from 0x{src_unicast:04x} -> send Bind next"
                    )
                    job["stage"] = "appkey_ok"

                    # reflect in nodes.json
                    self._mark_node_stage(job["uuid"], "appkey_ok")

                    # kick off Bind
                    self._fsm_send_bind(src_unicast)
                else:
                    self.log(
                        f"[FSM] AppKeyStatus FAIL (0x{status:02x}) from 0x{src_unicast:04x}"
                    )
            return

        # 0x803E = Config Model App Status (Bind result for SIG model)
        if op0 == 0x80 and op1 == 0x3E:
            if len(data) >= 3:
                status = data[2]
                if status == 0x00:
                    self.log(
                        f"[FSM] got Bind OK from 0x{src_unicast:04x} -> send PubSet next"
                    )
                    job["stage"] = "bind_ok"

                    # reflect in nodes.json
                    self._mark_node_stage(job["uuid"], "bind_ok")

                    # tell node to publish GenericOnOffStatus to us
                    self._fsm_send_pubset(src_unicast)
                else:
                    self.log(
                        f"[FSM] Bind FAIL (0x{status:02x}) from 0x{src_unicast:04x}"
                    )
            return

        # 0x8019 = Config Model Publication Status (PubSet result)
        if op0 == 0x80 and op1 == 0x19:
            if len(data) >= 3:
                status = data[2]
                if status == 0x00:
                    self.log(
                        f"[FSM] got PubSet OK from 0x{src_unicast:04x} -> provisioning DONE 🎉"
                    )
                    job["stage"] = "done"

                    # mark node fully active / publishing
                    self._mark_node_stage(job["uuid"], "pub_ok")

                    # actively poll the node right now for its state,
                    # so we don't have to sit around waiting for its periodic publish.
                    ok_get, msg_get = self.send_onoff_get(src_unicast, app_idx=0)
                    if ok_get:
                        self.log(
                            f"[FSM] sent GenericOnOffGet to 0x{src_unicast:04x}, waiting for Status…"
                        )
                    else:
                        self.log(
                            f"[FSM] failed to send GenericOnOffGet to 0x{src_unicast:04x}: {msg_get}"
                        )
                else:
                    self.log(
                        f"[FSM] PubSet FAIL (0x{status:02x}) from 0x{src_unicast:04x}"
                    )
            return

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

    def on_add_node_complete(self, uuid_bytes: bytes, unicast: int, count: int):
        """
        bluetooth-meshd Provisioner1.AddNodeComplete callback.
        Node has just been provisioned via PB-ADV.
        We'll:
          - stash FSM job
          - save nodes.json state='provisioning'
          - create (or confirm) AppKey(0)
          - send Config AppKey Add to the node
        """
        uuid_hex = uuid_bytes.hex()
        self.log(
            f"AddNodeComplete: uuid={uuid_hex} "
            f"unicast=0x{unicast:04x} elements={count}"
        )

        # create/refresh FSM job
        self._provision_jobs[unicast] = {
            "uuid": uuid_hex,
            "stage": "provisioning",
            "model_id": 0x1000,    # Generic OnOff Server
            "app_idx": 0,          # AppKey(0)
            "elem_addr": unicast,  # element 0
        }

        # make/update nodes.json
        self._persist_node_basic(uuid_hex, unicast, count)

        def _kickoff_cfg():
            # Ensure we have AppKey(0) locally
            self._ensure_appkey(app_index=0, net_index=0)

            # Push that AppKey down to the node (Config AppKey Add)
            ok, msg = self.add_appkey_to_node(
                unicast,
                app_index=0,
                net_index=0,
                update=False
            )
            if ok:
                self.log(f"[FSM] AppKey Add sent to 0x{unicast:04x}")
                self._provision_jobs[unicast]["stage"] = "appkey_sent"
                # After this, we expect a DevKey message:
                #   opcode 0x8003 (AppKey Status)
                # which will trigger _fsm_send_bind.
            else:
                self.log(f"[FSM] AppKey Add FAILED to 0x{unicast:04x}: {msg}")
        try:
            self.log("[FSM] stopping scan (UnprovisionedScanCancel)")
            self.mgmt.UnprovisionedScanCancel()
        except Exception as e:
            self.log(f"[FSM] scan-cancel failed (non-fatal): {e}")
        threading.Thread(target=_kickoff_cfg, daemon=True).start()

    def on_add_node_failed(self, uuid_bytes: bytes, reason: str):
        self.log(f"AddNodeFailed: uuid={uuid_bytes.hex()} reason={reason}")

    def on_element_message(self, source: int, key_index: int, destination, data: bytes):
        """
        Element1.MessageReceived: application-layer (AppKey-encrypted) msg.
        We'll specifically interpret GenericOnOffStatus (0x82 0x04).
        """
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
        This is where nodes answer AppKey Add / Bind / PubSet.
        """
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

        # drive the provisioning FSM forward
        try:
            self._handle_config_status_from_node(source, data)
        except Exception as e:
            self.log(f"[FSM] error while handling config status: {e}")
                
    def send_onoff_get(self, dest_unicast: int, app_idx: int = 0):
        """
        Ask a node for its Generic OnOff state (Generic OnOff Get, opcode 0x8201).
        We send this over AppKey 'app_idx' (we always use 0 so far).

        That node should answer with GenericOnOffStatus (opcode 0x8204),
        which will arrive via on_element_message(), which writes mesh_app.log
        and updates nodes.json last_onoff/last_seen.
        """
        if not self.is_attached:
            raise RuntimeError("Not attached yet")

        # Generic OnOff Get opcode = 0x82 0x01 (2-byte SIG opcode)
        payload = bytes([0x82, 0x01])

        self.log(f"[debug] sending GenericOnOff Get -> 0x{dest_unicast:04x}")

        # FIXED: Use Element0 proxy + explicit path
        try:
            elem = self._get_elem_proxy()
            return self._safe_call(
                lambda: elem.AppKeySend(
                    ELEM0_PATH,  # Our app's Element0 path
                    int(dest_unicast),
                    int(app_idx),
                    {},           # options dict: we'll keep empty for now
                    payload,
                ),
                f"OnOffGet(0x{dest_unicast:04x})"
            )
        except Exception as e:
            self.log(f"[OnOffGet] Proxy error: {e}")
            return False, f"OnOffGet proxy failed: {e}"

    # ---------------- utilities ----------------

    def _safe_call(self, fn, label: str):
        """
        Wrap any D-Bus call in try/except and return (ok, msg)
        so the GUI doesn't explode.
        """
        try:
            fn()
            return True, f"{label}: OK"
        except Exception as e:
            emsg = e.args[0] if e.args else str(e)
            self.log(f"{label} failed: {emsg}")
            return False, f"{label} failed: {emsg}"

    @property
    def is_attached(self) -> bool:
        return self.node_path is not None and self.mgmt is not None

