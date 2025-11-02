# vesp/util.py
from __future__ import annotations
from pathlib import Path
from typing import Optional
import json
import re
import uuid

# ---------- app dirs / files ----------
APP_NAME = "vespulaa"
STATE_DIR = Path.home() / ".config" / APP_NAME
STATE_DIR.mkdir(parents=True, exist_ok=True)

TOKEN_FILE = STATE_DIR / "token.json"
LOG_DIR = STATE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

NODE_UUID_FILE = STATE_DIR / "node_uuid.json"
NODES_FILE = STATE_DIR / "nodes.json"

CONFIG_DIR = STATE_DIR
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

NODES_FILE = CONFIG_DIR / "nodes.json"

# ---------- token helpers ----------
def save_token(token: int) -> None:
    TOKEN_FILE.write_text(json.dumps({"token": int(token)}))


def load_token() -> Optional[int]:
    try:
        data = json.loads(TOKEN_FILE.read_text())
        t = int(data.get("token"))
        if t < 0:
            return None
        return t
    except Exception:
        return None


def clear_token() -> None:
    try:
        TOKEN_FILE.unlink()
    except FileNotFoundError:
        pass


# ---------- UUID helpers ----------
_HEX32 = re.compile(r"^[0-9a-f]{32}$")


def default_node_uuid_bytes() -> bytes:
    """
    Return a persistent 16B UUID for this app:
      - If ~/.config/vesp/node_uuid.json exists, return it.
      - Else return a fresh RFC-4122 v4 UUID (do NOT persist yet).
        Controller will persist it on JoinComplete.
    Set VESP_FORCE_NEW_UUID=1 to ignore any persisted UUID for this run.
    """
    import os, json
    if os.environ.get("VESP_FORCE_NEW_UUID") == "1":
        return uuid.uuid4().bytes  # RFC-4122 v4

    try:
        if NODE_UUID_FILE.exists():
            obj = json.loads(NODE_UUID_FILE.read_text())
            hx = (obj.get("uuid_hex","") or "").lower()
            if len(hx) == 32:
                return bytes.fromhex(hx)
    except Exception:
        pass
    return uuid.uuid4().bytes  # RFC-4122 v4

def persist_node_uuid(u: bytes) -> None:
    try:
        NODE_UUID_FILE.write_text(json.dumps({"uuid_hex": u.hex()}))
    except Exception:
        pass

def normalize_uuid_hex(s: str) -> str:
    """
    Accepts: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee' or 'aabb...'(32 hex)
    Returns lowercase 32 hex chars, raises ValueError if not 16 bytes.
    """
    x = s.strip().lower().replace("-", "")
    if not _HEX32.match(x):
        raise ValueError("UUID must be exactly 16 bytes (32 hex characters).")
    return x


def load_nodes_db():
    """
    Returns a dict:
    {
      "<remote_uuid_hex>": {
         "unicast": int,
         "elements": int,
         "last_onoff": 0|1|None,
         "last_seen": "ISO8601 or None",
         "provisioned_at": "ISO8601",
         "state": "active"|"reset_sent"|...
      },
      ...
    }
    """
    try:
        with NODES_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
            # normalize types just in case
            for k, v in list(data.items()):
                if not isinstance(v, dict):
                    data[k] = {}
            return data
    except Exception:
        return {}

def save_nodes_db(db: dict):
    """
    Safely persist nodes.json.
    """
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with NODES_FILE.open("w", encoding="utf-8") as f:
            json.dump(db, f, indent=2, sort_keys=True)
    except Exception:
        # non-fatal, just ignore
        pass
        
# ---------- Mesh address parsing ----------
def parse_unicast_addr(s: str) -> int:
    """
    Accept '0x1201', '1201' (hex without 0x), or decimal '4609'.
    Valid unicast range: 0x0001..0x7FFF.
    """
    ss = s.strip().lower()
    try:
        if ss.startswith("0x"):
            val = int(ss, 16)
        elif all(c in "0123456789abcdef" for c in ss) and len(ss) <= 4:
            val = int(ss, 16)  # hex without 0x
        else:
            val = int(ss, 10)  # decimal
    except ValueError:
        raise ValueError("Unicast address must be hex (e.g. 0x1201 or 1201) or decimal.")
    if not (0x0001 <= val <= 0x7FFF):
        raise ValueError("Unicast address out of range (0x0001..0x7FFF).")
    return val


# ---------- ADV parsing (PB-ADV only; no GATT) ----------
def parse_unprov_uuid(adv: bytes):
    """
    Try to extract a 16-byte Device UUID from an unprovisioned device beacon
    as delivered by bluetooth-meshd's Provisioner1.ScanResult.

    Observed format from BlueZ on your machine (len = 22 bytes):
      [0:16]  Device UUID
      [16:18] OOB Info (little-endian)
      [18:22] URI Hash (optional, often 0x00000000)

    We treat the first 16 bytes as the UUID and return it as lowercase hex,
    as long as it's not all 0x00 or all 0xff.
    """
    if not isinstance(adv, (bytes, bytearray)):
        return None

    # Need at least 16 bytes for UUID
    if len(adv) < 16:
        return None

    uuid_bytes = adv[0:16]

    # Filter out garbage like all zeros or all 0xff
    if all(b == 0x00 for b in uuid_bytes):
        return None
    if all(b == 0xFF for b in uuid_bytes):
        return None

    # Return nice clean hex string, 32 lowercase chars
    return uuid_bytes.hex()

