#!/usr/bin/env python3
"""
VESP GUI launcher.

- Verifies that a display is available (so Tkinter won’t crash in headless shells).
- Delegates to vesp.gui.main() which wires up the D-Bus/GLib loop and the UI.

Usage:
  python3 run_gui.py
"""

from __future__ import annotations
import os
import sys

def _ensure_display():
    # Common cases:
    #  - Local desktop: DISPLAY is set
    #  - Wayland: WAYLAND_DISPLAY is set (Tk uses XWayland typically; still need DISPLAY)
    #  - Headless SSH: neither is set -> suggest using run_logger.py
    if not os.environ.get("DISPLAY"):
        sys.stderr.write(
            "[VESP] No DISPLAY detected. The GUI needs an X/Wayland session.\n"
            "       If you are on a headless machine, run the ADV logger instead:\n"
            "         python3 run_logger.py\n"
        )
        sys.exit(2)

def main():
    _ensure_display()
    try:
        # Import here so missing deps produce a clean, actionable error.
        from vesp.gui import main as gui_main
    except Exception as e:
        sys.stderr.write(
            "[VESP] Failed to import GUI components.\n"
            f"        Error: {e}\n"
            "        Make sure system deps (python3-gi, gir packages) and Python deps are installed.\n"
            "        You can run:  ./scripts/install.sh\n"
        )
        sys.exit(1)

    # Hand off to the real GUI entrypoint
    gui_main()


if __name__ == "__main__":
    main()

