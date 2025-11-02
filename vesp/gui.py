# vesp/gui.py
from __future__ import annotations
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from tkinter.scrolledtext import ScrolledText
from typing import Dict, Optional

from .controller import Controller
from .util import LOG_DIR, load_token

class VespGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("VESP Mesh (ADV-only)")

        # Controller
        self.ctrl = Controller()
        self.ctrl.set_gui_callbacks(log_cb=self._post_log, scan_cb=self._post_scan)
        self.ctrl.export()
        self.ctrl.start_glib_thread()

        # UI State
        self.scan_items: Dict[str, int] = {}

        # Layout
        self._build_ui()

        self._log(f"Logs directory: {LOG_DIR}")
        self._log("Ready. If this is your first time, click 'Create Network' to make your local node.")

        tok = load_token()
        if tok is not None:
            self._log(f"Token found ({tok}); attempting auto-attach…")
            self._do_threaded(lambda: self.ctrl.attach(tok), "Attach")

    # ---------- UI construction ----------
    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        # Row 0: Control buttons
        row0 = ttk.Frame(outer)
        row0.pack(fill="x", pady=(0, 8))

        self.btn_create = ttk.Button(row0, text="Create Network", command=self._do_create)
        self.btn_attach = ttk.Button(row0, text="Attach", command=self._do_attach)
        self.btn_detach = ttk.Button(row0, text="Detach (local)", command=self._do_detach)
        self.btn_leave  = ttk.Button(row0, text="Leave…", command=self._do_leave)
        self.btn_purge  = ttk.Button(row0, text="Purge (deep)", command=self._do_purge)
        self.btn_local_config = ttk.Button(row0, text="Config Local Client", command=self._do_local_config)

        for w in (self.btn_create, self.btn_attach, self.btn_detach, self.btn_leave, self.btn_purge, self.btn_local_config):
            w.pack(side="left", padx=(0, 6))

        # Row 1: Scan controls
        row1 = ttk.Frame(outer)
        row1.pack(fill="x", pady=(0, 8))

        ttk.Label(row1, text="Scan (s):").pack(side="left")
        self.ent_secs = ttk.Entry(row1, width=6)
        self.ent_secs.insert(0, "15")
        self.ent_secs.pack(side="left", padx=(4, 8))

        self.btn_scan_start = ttk.Button(row1, text="Start Scan", command=self._do_scan_start)
        self.btn_scan_stop = ttk.Button(row1, text="Stop Scan", command=self._do_scan_stop)
        self.btn_scan_start.pack(side="left", padx=(0, 6))
        self.btn_scan_stop.pack(side="left", padx=(0, 6))

        # Row 2: Unprovisioned list + actions
        row2 = ttk.Frame(outer)
        row2.pack(fill="both", expand=True)

        left = ttk.Frame(row2)
        left.pack(side="left", fill="both", expand=True)

        ttk.Label(left, text="Unprovisioned (UUID | RSSI)").pack(anchor="w")
        self.list_scan = tk.Listbox(left, height=12, activestyle="dotbox")
        self.list_scan.pack(fill="both", expand=True, pady=(4, 8))

        actions = ttk.Frame(left)
        actions.pack(fill="x")

        self.btn_prov_sel = ttk.Button(actions, text="Provision Selected", command=self._do_provision_selected)
        self.btn_prov_sel.pack(side="left", padx=(0, 6))

        ttk.Label(actions, text="or UUID:").pack(side="left")
        self.ent_uuid = ttk.Entry(actions, width=64)  # widened to show full UUID comfortably
        self.ent_uuid.pack(side="left", padx=(4, 6))
        self.btn_prov_uuid = ttk.Button(actions, text="Provision UUID", command=self._do_provision_uuid)
        self.btn_prov_uuid.pack(side="left", padx=(0, 6))

        # Row 3: Node Reset
        row3 = ttk.Frame(outer)
        row3.pack(fill="x", pady=(8, 8))
        ttk.Label(row3, text="Node Reset unicast:").pack(side="left")
        self.ent_unicast = ttk.Entry(row3, width=10)
        self.ent_unicast.insert(0, "00aa")
        self.ent_unicast.pack(side="left", padx=(4, 6))
        ttk.Button(row3, text="Reset", command=self._do_reset).pack(side="left")

        # Row 4: Log pane
        row4 = ttk.Frame(outer)
        row4.pack(fill="both", expand=True)

        ttk.Label(row4, text="Log").pack(anchor="w")
        self.txt_log = ScrolledText(row4, height=16, wrap="none", state="disabled")
        self.txt_log.pack(fill="both", expand=True, pady=(4, 0))

        # Status bar
        self.var_status = tk.StringVar(value="Status: idle")
        bar = ttk.Label(outer, textvariable=self.var_status, anchor="w")
        bar.pack(fill="x", pady=(8, 0))

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- Thread-safe GUI posting ----------
    def _post_log(self, s: str):
        self.root.after(0, self._log, s)

    def _post_scan(self, uuid_hex: str, rssi: int):
        # Normalize to exactly 32 hex chars (16 bytes) before adding to the list.
        uh = self._sanitize_uuid_text(uuid_hex)
        if len(uh) != 32:
            # Show a hint once in the log but don't pollute the list
            self.root.after(0, self._log, f"[GUI] Ignored scan UUID (len={len(uh)}): {uh}")
            return
        self.root.after(0, self._scan_add_or_update, uh, rssi)


    # ---------- Logging / list helpers ----------
    def _log(self, s: str):
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", s + "\n")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def _scan_add_or_update(self, uuid_hex: str, rssi: int):
        prev = self.scan_items.get(uuid_hex)
        if prev is None or rssi > prev:
            self.scan_items[uuid_hex] = rssi
        self.list_scan.delete(0, "end")
        for u, r in sorted(self.scan_items.items(), key=lambda kv: kv[1], reverse=True):
            self.list_scan.insert("end", f"{u} | {r}")
        # Make sure the list visually updates right away
        self.list_scan.update_idletasks()

    def _get_selected_uuid(self) -> Optional[str]:
        try:
            idx = self.list_scan.curselection()
            if not idx:
                return None
            line = self.list_scan.get(idx[0])
            raw = line.split("|", 1)[0]
            uh = self._sanitize_uuid_text(raw)
            return uh if len(uh) == 32 else None
        except Exception:
            return None
            
    def _sanitize_uuid_text(self, s: str) -> str:
        """Keep only hex chars, lowercase, strip 0x/dashes/spaces/newlines etc."""
        if not s:
            return ""
        import re
        h = re.sub(r'[^0-9a-fA-F]', '', s)
        return h.lower()


    # ---------- Actions ----------
    def _do_threaded(self, func, label: str):
        def run():
            ok, msg = False, f"{label}: (no result)"
            try:
                ok, msg = func()
            except Exception as e:
                ok, msg = False, f"{label} exception: {e}"
            self._post_log(msg)
            self.var_status.set(f"Status: {msg}")
        threading.Thread(target=run, daemon=True).start()

    def _do_create(self):  self._do_threaded(self.ctrl.create_network, "CreateNetwork")
    def _do_attach(self):  self._do_threaded(self.ctrl.attach, "Attach")
    def _do_detach(self):  self._do_threaded(self.ctrl.detach_local, "Detach")
    def _do_purge(self):   self._do_threaded(self.ctrl.purge_local_node, "Purge")

    def _do_local_config(self):
        if not self.ctrl.is_attached:
            messagebox.showwarning("Local Config", "Attach to network first.")
            return
        self._do_threaded(lambda: self.ctrl.config_local_client(), "LocalConfig")

    def _do_leave(self):
        if not messagebox.askyesno("Confirm Leave",
                                   "This will delete your local mesh node from the daemon.\n\nProceed?"):
            return
        deep = messagebox.askyesno(
            "Restart daemon?",
            "Also restart bluetooth-meshd after leaving? (Recommended on Ubuntu 24.04)\n"
            "You may be prompted for your password."
        )
        self._do_threaded(lambda: self.ctrl.leave_network(deep=deep), "Leave")

    def _do_scan_start(self):
        try:
            secs = int(self.ent_secs.get().strip())
        except Exception:
            secs = None
        self.scan_items.clear()
        self.list_scan.delete(0, "end")
        self._do_threaded(lambda: self.ctrl.scan_start(secs), "UnprovisionedScan")

    def _do_scan_stop(self):
        self._do_threaded(self.ctrl.scan_stop, "UnprovisionedScanCancel")

    def _do_provision_selected(self):
        uuid_hex = self._get_selected_uuid()
        if not uuid_hex:
            messagebox.showwarning("Provision", "Select a device from the list first.")
            return
        self._do_threaded(lambda: self.ctrl.provision_uuid(uuid_hex), "AddNode")

    def _do_provision_uuid(self):
        raw = self.ent_uuid.get()
        uh = self._sanitize_uuid_text(raw)
        if len(uh) != 32:
            messagebox.showwarning(
                "Provision",
                f"UUID must be 16 bytes (32 hex chars).\n\nYou entered ({len(uh)} chars after cleanup):\n{uh or '(empty)'}"
            )
            return
        self._do_threaded(lambda: self.ctrl.provision_uuid(uh), "AddNode")

    def _do_bind(self):
        try:
            tgt  = int(self.ent_target.get().strip(), 16)
            elem = int(self.ent_elem.get().strip(), 16)
            mid  = int(self.ent_model.get().strip(), 16)
            self._do_threaded(lambda: self.ctrl.bind_model_sig(tgt, elem, 0, mid), "Bind")
        except Exception as e:
            self._log(f"Bind error: {e}")

    def _do_sub(self):
        try:
            tgt  = int(self.ent_target.get().strip(), 16)
            elem = int(self.ent_elem.get().strip(), 16)
            mid  = int(self.ent_model.get().strip(), 16)
            grp  = int(self.ent_group.get().strip(), 16)
            self._do_threaded(lambda: self.ctrl.sub_add_sig(tgt, elem, grp, mid), "SubAdd")
        except Exception as e:
            self._log(f"Subscribe error: {e}")

    def _do_reset(self):
        dst = self.ent_unicast.get().strip()
        if not dst:
            messagebox.showwarning("Reset", "Enter a unicast address, e.g. 00aa or 0x00aa")
            return
        self._do_threaded(lambda: self.ctrl.reset_remote_node(dst), "ConfigNodeReset")

    def _on_close(self):
        self.root.destroy()


def main():
        root = tk.Tk()
        try:
            style = ttk.Style(root)
            if "clam" in style.theme_names():
                style.theme_use("clam")
        except Exception:
            pass
        app = VespGUI(root)
        root.minsize(860, 700)
        root.mainloop()


if __name__ == "__main__":
    main()

