#!/usr/bin/env python3
import socket
import threading
import json
import time
import can
import requests
import os
import argparse
import subprocess


PRINT_PREFIX = "[bridge]"


def p(*msg):
    print(PRINT_PREFIX, *msg)


def ensure_vcan(iface):
    try:
        out = subprocess.check_output(["ip", "link"], text=True)
        if iface in out:
            p(f"{iface} already exists.")
            return True
    except Exception as e:
        p(f"Could not check interfaces: {e}")

    p(f"{iface} not found, creating...")

    try:
        subprocess.run(["sudo", "modprobe", "vcan"], check=True)
        subprocess.run(["sudo", "ip", "link", "add", iface, "type", "vcan"], check=True)
        subprocess.run(["sudo", "ip", "link", "set", iface, "up"], check=True)
        p(f"{iface} created and up!")
        return True
    except Exception as e:
        p(f"Failed to create {iface}. Run this script with sudo? Error: {e}")
        return False


class UDSBridge:
    def __init__(self, host, port, iface="vcan0"):
        self.host = host
        self.port = port
        self.iface = iface
        self.sock = None
        self.bus = None
        self.running = True


    def connect(self):
        p(f"Connecting to {self.host}:{self.port} ...")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        try:
            self.sock.connect((self.host, self.port))
        except Exception as e:
            p(f"ERROR: cannot connect → {e}")
            raise SystemExit

        p("Connected.")

  
    def _handle_session_json(self, j):
        if j.get("type") != "session":
            return

        url = j.get("download_url")


        if not url:
            return

        p(f"Downloading algorithm.zip from: {url}")

        try:
            r = requests.get(url, timeout=10)
            if r.status_code != 200:
                p(f"ERROR: HTTP status {r.status_code}")
                return
        except Exception as e:
            p(f"ERROR downloading ZIP → {e}")
            return

        with open("algorithm.zip", "wb") as f:
            f.write(r.content)

        p("Saved algorithm.zip successfully!")


    def tcp_listener(self):
        buf = b""
        while self.running:
            try:
                data = self.sock.recv(4096)
                if not data:
                    p("Server closed connection.")
                    self.running = False
                    break

                buf += data


                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        text = line.decode("utf-8", errors="replace").strip()
                    except Exception as e:
                        p(f"Decode error: {e}")
                        continue


                    try:
                        obj = json.loads(text)
                    except json.JSONDecodeError as e:
                        p(f"Invalid JSON from server: {text} ({e})")
                        continue


                    if isinstance(obj, dict) and obj.get("type") == "session":
                        self._handle_session_json(obj)
                        continue


                    frames = obj if isinstance(obj, list) else [obj]

                    for frame in frames:
                        if not isinstance(frame, dict):
                            continue
                        if "arbitration_id" not in frame or "data" not in frame:
                            continue

                        try:
                            arb_id = int(frame["arbitration_id"])
                            data_bytes = bytes(int(x) & 0xFF for x in frame["data"])
                        except Exception as e:
                            p(f"Bad frame from server: {frame} ({e})")
                            continue

                        msg = can.Message(
                            arbitration_id=arb_id,
                            data=data_bytes,
                            is_extended_id=False,
                        )
                        try:
                            self.bus.send(msg)
                        except Exception as e:
                            p(f"CAN send error: {e}")

            except Exception as e:
                p(f"TCP listener error: {e}")
                time.sleep(0.5)
                if not self.running:
                    break


    def can_listener(self):
        while self.running:
            try:
                msg = self.bus.recv()
                if msg is None:
                    continue

                if msg.is_error_frame:
                    continue


                frame_obj = {
                    "arbitration_id": msg.arbitration_id,
                    "data": list(msg.data),
                }

                try:
                    payload = json.dumps(frame_obj, separators=(",", ":")).encode("utf-8") + b"\n"
                    self.sock.sendall(payload)
                except Exception as e:
                    p(f"TCP send error: {e}")
                    break

            except Exception as e:
                p(f"CAN listener error: {e}")


    def run(self):     
        if not ensure_vcan(self.iface):
            p("Cannot continue without vcan interface.")
            return


        try:
            self.bus = can.interface.Bus(
                channel=self.iface,
                interface="socketcan",
            )
        except Exception as e:
            p(f"Cannot open CAN interface {self.iface} → {e}")
            return


        self.connect()


        threading.Thread(target=self.tcp_listener, daemon=True).start()
        threading.Thread(target=self.can_listener, daemon=True).start()

        p("Bridge is active. Start using CAN tools!")

        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            p("Stopping bridge...")

        self.running = False
        try:
            self.sock.close()
        except Exception:
            pass
        p("Bridge stopped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--iface", default="vcan0")
    args = parser.parse_args()

    br = UDSBridge(args.host, args.port, args.iface)
    br.run()
