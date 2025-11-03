from __future__ import annotations
import sys, json, threading
from .controller import Controller

def jprint(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)

def main():
    ctrl = Controller()
    ctrl.set_gui_callbacks(
        log_cb=lambda s: jprint({"type":"log", "text": s}),
        scan_cb=lambda uuid_hex, rssi: jprint({"type":"scan", "uuid": uuid_hex, "rssi": int(rssi)}),
    )
    ctrl.export()
    ctrl.start_glib_thread()

    jprint({"type":"ready", "detail":"bridge started"})

    def handle(cmd, args):
        try:
            if cmd == "create_network":         ok, msg = ctrl.create_network()
            elif cmd == "attach":               ok, msg = ctrl.attach()
            elif cmd == "detach":               ok, msg = ctrl.detach_local()
            elif cmd == "leave":                ok, msg = ctrl.leave_network(deep=bool(args.get("deep", False)))
            elif cmd == "purge":                ok, msg = ctrl.purge_local_node()
            elif cmd == "config_local_client":  ok, msg = ctrl.config_local_client()
            elif cmd == "scan_start":           ok, msg = ctrl.scan_start(args.get("seconds"))
            elif cmd == "scan_stop":            ok, msg = ctrl.scan_stop()
            elif cmd == "provision_uuid":       ok, msg = ctrl.provision_uuid(args.get("uuid",""))
            elif cmd == "reset_node":           ok, msg = ctrl.reset_remote_node(args.get("unicast",""))
            else:
                ok, msg = False, f"Unknown command: {cmd}"
        except Exception as e:
            ok, msg = False, f"{cmd} exception: {e}"
        jprint({"type":"result", "cmd": cmd, "ok": bool(ok), "msg": str(msg)})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            cmd = req.get("cmd","")
            args = {k:v for k,v in req.items() if k != "cmd"}
            threading.Thread(target=handle, args=(cmd, args), daemon=True).start()
        except Exception as e:
            jprint({"type":"result", "cmd": "<parse>", "ok": False, "msg": f"bad request: {e}"})

if __name__ == "__main__":
    main()

