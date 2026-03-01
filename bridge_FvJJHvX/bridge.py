#!/usr/bin/env python3
"""
uds_bridge_multi.py

Player-side bridge that connects local vcan interface to the remote can_gateway_server_multi TCP server.

Behavior / assumptions:
 - Uses python-can to read/write socketcan vcan interface.
 - For every CAN frame read from vcan (Message.data), bridge sends newline-delimited JSON
   {"arbitration_id": <int>, "data":[<int>,...]} to the server over a persistent TCP connection.
 - Frames received from server (JSON objects or lists) are injected onto the vcan as CAN messages.
 - On start, if the vcan interface does not exist, bridge attempts to create it using
   `modprobe vcan` and `ip link add ...` (requires sudo). If those commands fail due to lack
   of privileges, an instructive error is printed and the bridge exits.
 - Legacy mode (--legacy): accepts textual lines from stdin in "ID DATA" form and will send them to the server.
 - The bridge will handle the initial {"type":"session"...} message from server (prints it).
 - Robust JSON parsing; invalid frames are logged but do not crash the bridge.

Usage:
  python3 uds_bridge_multi.py --host 127.0.0.1 --port 5000 --iface vcan0 --debug

Note: requires python-can installed: pip3 install python-can
"""

from __future__ import annotations

import argparse
import socket
import json
import time
import sys
import subprocess
import threading
from typing import Any, Dict, List, Optional

try:
    import can
except Exception as e:
    can = None  # We'll handle import failures gracefully

# ---------- Helpers ----------

def ensure_vcan(iface: str, debug: bool = False) -> None:
    """
    Ensure vcan interface exists. Tries to auto-create using modprobe + ip link.
    If creation fails due to permissions, prints instructions and exits.
    """
    # Quick check via ip link show
    try:
        out = subprocess.check_output(["ip", "link", "show", iface], stderr=subprocess.STDOUT)
        if debug:
            print(f"[DEBUG] Found interface {iface}")
        return
    except subprocess.CalledProcessError:
        if debug:
            print(f"[DEBUG] Interface {iface} not found; attempting to create")
    except FileNotFoundError:
        print("[ERROR] 'ip' command not found. Make sure iproute2 is installed.", file=sys.stderr)
        sys.exit(1)

    # Try to create vcan
    try:
        subprocess.check_call(["sudo", "modprobe", "vcan"])
        subprocess.check_call(["sudo", "ip", "link", "add", "dev", iface, "type", "vcan"])
        subprocess.check_call(["sudo", "ip", "link", "set", "up", iface])
        print(f"[INFO] Created and brought up {iface} (via sudo).")
    except subprocess.CalledProcessError as e:
        print("[ERROR] Failed to auto-create vcan interface. You need to run these commands as root or with sudo:", file=sys.stderr)
        print("  sudo modprobe vcan", file=sys.stderr)
        print(f"  sudo ip link add dev {iface} type vcan || true", file=sys.stderr)
        print(f"  sudo ip link set up {iface}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print("[ERROR] 'sudo' or 'ip' not available on this system.", file=sys.stderr)
        sys.exit(1)


def parse_server_frame(obj: Any) -> List[Dict[str, Any]]:
    """
    Accept either a single object or a list of objects and normalize to a list of frames.
    Each frame is {"arbitration_id": int, "data": [ints...]}
    """
    if isinstance(obj, dict):
        return [obj]
    if isinstance(obj, list):
        out = []
        for it in obj:
            if isinstance(it, dict) and "arbitration_id" in it and "data" in it:
                out.append(it)
        return out
    return []


def parse_legacy_line(line: str) -> Optional[Dict[str, Any]]:
    """
    Same legacy parser as server. Accepts "7E3 022201" or "7E3 02 22 01" etc.
    """
    line = line.strip()
    if not line:
        return None
    parts = line.split()
    if len(parts) < 2:
        return None
    try:
        aid = int(parts[0], 16)
    except Exception:
        return None
    toks = parts[1:]
    if len(toks) == 1:
        s = toks[0]
        if s.startswith("0x") or s.startswith("0X"):
            s = s[2:]
        if len(s) % 2 == 0:
            try:
                data = [int(s[i:i+2], 16) for i in range(0, len(s), 2)]
                return {"arbitration_id": aid, "data": data}
            except Exception:
                return None
    data = []
    for tok in toks:
        try:
            data.append(int(tok, 16))
        except Exception:
            return None
    return {"arbitration_id": aid, "data": data}


# ---------- Bridge core ----------

class UdsBridge:
    def __init__(self, host: str, port: int, iface: str, debug: bool = False, legacy: bool = False):
        self.host = host
        self.port = port
        self.iface = iface
        self.debug = debug
        self.legacy = legacy
        self.sock: Optional[socket.socket] = None
        self.running = False
        self.can_bus: Optional[Any] = None
        self.recv_thread: Optional[threading.Thread] = None

    def connect_server(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(5.0)
        print(f"[INFO] Connecting to server {self.host}:{self.port} ...")
        try:
            self.sock.connect((self.host, self.port))
            print("[INFO] Connected to server.")
            self.sock.settimeout(None)
        except Exception as e:
            print("[ERROR] Failed to connect to server:", e)
            sys.exit(1)

    def start_can(self) -> None:
        if can is None:
            print("[ERROR] python-can is required. Install with: pip3 install python-can", file=sys.stderr)
            sys.exit(1)
        # Ensure interface exists
        ensure_vcan(self.iface, debug=self.debug)
        try:
            self.can_bus = can.interface.Bus(channel=self.iface, interface="socketcan")
        except Exception as e:
            print(f"[ERROR] Failed to open CAN interface {self.iface}: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"[INFO] Opened CAN interface {self.iface}")

    def run(self) -> None:
        self.connect_server()
        self.start_can()
        self.running = True

        # Start thread to read from server socket
        self.recv_thread = threading.Thread(target=self._server_reader_thread, daemon=True)
        self.recv_thread.start()

        # Main loop: read CAN frames and forward to server
        try:
            while self.running:
                msg = self.can_bus.recv(timeout=1.0)
                if msg is None:
                    continue
                # Build canonical JSON
                data_list = list(msg.data)
                obj = {"arbitration_id": msg.arbitration_id, "data": data_list}
                line = json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n"
                if self.debug:
                    print("[DEBUG][TO-SERVER]", line.strip())
                try:
                    self.sock.sendall(line.encode("utf-8"))
                except Exception as e:
                    print("[ERROR] Failed to send to server:", e)
                    self.running = False
                    break

                # Optionally accept legacy input from stdin in legacy mode
                if self.legacy and sys.stdin in select_readable(0.0):
                    raw = sys.stdin.readline()
                    if not raw:
                        continue
                    parsed = parse_legacy_line(raw)
                    if parsed:
                        try:
                            self.sock.sendall((json.dumps(parsed) + "\n").encode("utf-8"))
                        except Exception as e:
                            print("[ERROR] Failed to send legacy to server:", e)
        except KeyboardInterrupt:
            print("\n[INFO] Stopping bridge (user interrupt)")
        finally:
            self.running = False
            try:
                self.sock.close()
            except Exception:
                pass
            try:
                if self.can_bus is not None:
                    self.can_bus.shutdown()
            except Exception:
                pass

    def _server_reader_thread(self) -> None:
        """
        Read newline-delimited JSON messages from server and inject onto vcan.
        Handles both single object and list-of-objects.
        """
        buf = b""
        sock = self.sock
        while self.running:
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    print("[INFO] Server closed connection")
                    self.running = False
                    break
                buf += chunk
            except Exception as e:
                print("[ERROR] Error reading from server socket:", e)
                self.running = False
                break

            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line:
                    continue
                try:
                    s = line.decode("utf-8", errors="replace").strip()
                except Exception:
                    s = line.decode("latin1", errors="replace").strip()
                if self.debug:
                    print("[DEBUG][FROM-SERVER]", s)
                # Try parse JSON
                try:
                    obj = json.loads(s)
                except Exception:
                    print("[WARN] Failed to parse JSON from server:", s)
                    continue

                # Handle session message
                if isinstance(obj, dict) and obj.get("type") == "session":
                    #print("[INFO] Session from server:", obj)
                    print("[INFO] Connected. Ready to Play !!")
                    continue

                frames = parse_server_frame(obj)
                for f in frames:
                    # Validate
                    aid = f.get("arbitration_id")
                    data = f.get("data")
                    if not isinstance(aid, int) or not isinstance(data, list):
                        print("[WARN] Invalid frame shape from server:", f)
                        continue
                    # Truncate/pad to 8 bytes (python-can expects bytes length <= 8)
                    if len(data) > 8:
                        print("[WARN] Truncating frame data to 8 bytes")
                        data = data[:8]
                    try:
                        msg = can.Message(arbitration_id=aid, data=bytes(data), is_extended_id=False)
                        self.can_bus.send(msg)
                        if self.debug:
                            print(f"[DEBUG] Injected CAN {aid:03X} {data}")
                    except Exception as e:
                        print("[ERROR] Failed to send CAN frame:", e)


# ---------- Small helpers ----------

def select_readable(timeout: float) -> List[int]:
    """
    Simple select wrapper to know if stdin has data (non-blocking).
    Returns list of fds ready (just 0 if stdin ready).
    """
    import select
    r, _, _ = select.select([sys.stdin], [], [], timeout)
    return r


# ---------- CLI ----------

def main():
    p = argparse.ArgumentParser(description="UDS bridge: vcan <-> TCP server")
    p.add_argument("--host", default="127.0.0.1", help="Server host")
    p.add_argument("--port", type=int, default=5000, help="Server port")
    p.add_argument("--iface", default="vcan0", help="socketcan interface (vcan0)")
    p.add_argument("--debug", action="store_true", help="Enable debug logging")
    p.add_argument("--legacy", action="store_true", help="Enable legacy textual input parsing")
    args = p.parse_args()

    bridge = UdsBridge(args.host, args.port, args.iface, debug=args.debug, legacy=args.legacy)
    bridge.run()


if __name__ == "__main__":
    main()
