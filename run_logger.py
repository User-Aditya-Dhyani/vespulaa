#!/usr/bin/env python3
"""
Headless ADV-only Bluetooth Mesh logger.

Examples:
  # First time on a fresh machine:
  python3 run_logger.py --create

  # Just attach to existing local node and idle-log:
  python3 run_logger.py

  # Scan 20s for unprovisioned ESP32 and auto-log results:
  python3 run_logger.py --scan 20

  # Provision a found UUID (32 hex chars, no dashes):
  python3 run_logger.py --provision DDDD441D64BD1F0E0000000000000000

  # Reset a node so it becomes unprovisioned again:
  python3 run_logger.py --reset 00aa
"""
from __future__ import annotations

import sys

def main():
    try:
        from vesp.logger_node import main as run
    except Exception as e:
        sys.stderr.write(
            "[VESP] Failed to import headless logger.\n"
            f"        Error: {e}\n"
            "        Make sure system and Python dependencies are installed.\n"
            "        Try: ./scripts/install.sh\n"
        )
        raise SystemExit(1)
    raise SystemExit(run())


if __name__ == "__main__":
    main()

