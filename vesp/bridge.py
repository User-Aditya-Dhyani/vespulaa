#!/usr/bin/env python3
import sys, json, traceback

# import from your existing codebase
from controller import Controller  # adjust if your module path differs

ctrl = Controller()

def jprint(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)

while True:
    line = sys.stdin.readline()
    if not line:
        break
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
        cmd = msg.get("cmd")
        args = msg.get("args", {}) or {}
    except Exception as e:
        jprint({"ok": False, "error": f"bad json: {e}"})
        continue

    try:
        if cmd == "create_network":
            # adapt this call/return to match your Controller API
            result = ctrl.create_network()
            # normalize result for the UI
            jprint({"ok": True, "data": result})
        else:
            jprint({"ok": False, "error": f"unknown cmd: {cmd}"})
    except Exception as e:
        traceback.print_exc()
        jprint({"ok": False, "error": str(e)})

