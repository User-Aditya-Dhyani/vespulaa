# vesp/logger_node.py
from __future__ import annotations
import argparse
import signal
import sys
import time
from typing import Optional

from gi.repository import GLib

from .controller import Controller
from .util import load_token, LOG_DIR


class HeadlessLogger:
    def __init__(self, auto_create: bool):
        self.ctrl = Controller()
        # Simple stdout logger; Controller also appends to files.
        self.ctrl.set_gui_callbacks(log_cb=self._log, scan_cb=self._on_scan)
        # Export our app/agent/provisioner/element & spin GLib in background
        self.ctrl.export()
        self.ctrl.start_glib_thread()
        self._want_quit = False
        self._auto_create = auto_create

    # --- logging hooks given to Controller ---
    def _log(self, s: str):
        sys.stdout.write(s + "\n")
        sys.stdout.flush()

    def _on_scan(self, uuid_hex: str, rssi: int):
        self._log(f"[SCAN] {uuid_hex} RSSI={rssi}")

    # --- lifecycle helpers ---
    def ensure_attached(self):
        token = load_token()
        if token is None:
            if not self._auto_create:
                raise RuntimeError(
                    "No local mesh token found. Run with --create (first time) "
                    "to create your laptop node."
                )
            ok, msg = self.ctrl.create_network()
            self._log(msg)
            if not ok:
                raise RuntimeError("CreateNetwork failed; cannot attach.")
            # on_join_complete will auto-attach
            # Give the daemon a moment to call back.
            time.sleep(0.5)

        if not self.ctrl.is_attached:
            ok, msg = self.ctrl.attach()
            self._log(msg)
            if not ok:
                raise RuntimeError("Attach failed.")

    def run(self):
        self._log(f"[VESP] Logs dir: {LOG_DIR}")
        self._log("[VESP] Headless logger up. Press Ctrl+C to exit.")

        # Keep process alive; GLib loop runs in the background thread.
        # We just idle here and respond to SIGINT/SIGTERM.
        while not self._want_quit:
            time.sleep(0.25)

    def stop(self, *_):
        self._want_quit = True
        self._log("[VESP] Shutting down...")


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="vesp-logger",
        description="Headless ADV-only Bluetooth Mesh logger (create/attach/scan/provision/reset).",
    )
    p.add_argument("--create", action="store_true",
                   help="If no token exists, create the local node first (Join & auto-attach).")
    p.add_argument("--scan", type=int, default=0,
                   help="Scan for unprovisioned devices for N seconds (PB-ADV).")
    p.add_argument("--provision", metavar="UUID_HEX", default=None,
                   help="Provision a device by 16-byte UUID (32 hex chars).")
    p.add_argument("--reset", metavar="UNICAST",
                   help="Send Config Node Reset to a unicast (e.g., 00aa or 0x00aa).")
    p.add_argument("--no-attach", action="store_true",
                   help="Skip auto-attach (useful for quick checks of CreateNetwork only).")

    args = p.parse_args(argv or sys.argv[1:])

    app = HeadlessLogger(auto_create=args.create)
    signal.signal(signal.SIGINT, app.stop)
    signal.signal(signal.SIGTERM, app.stop)

    # Attach unless told not to
    if not args.no_attach:
        app.ensure_attached()

    # Optional actions
    if args.scan and not args.no_attach:
        ok, msg = app.ctrl.scan_start(args.scan)
        app._log(msg)

    if args.provision and not args.no_attach:
        ok, msg = app.ctrl.provision_uuid(args.provision)
        app._log(msg)

    if args.reset and not args.no_attach:
        ok, msg = app.ctrl.reset_remote_node(args.reset)
        app._log(msg)

    # Idle until Ctrl+C
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

