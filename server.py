#!/usr/bin/env python3
"""gnvtop — lightweight web UI for GPU monitoring (nvtop-style).

Serves a single static page plus a JSON API reading the same data sources
nvtop uses: nvidia-smi (-q -x XML: metrics + ALL process types) for NVIDIA
GPUs, amdgpu sysfs for AMD GPUs. Standard library only. Binds to 127.0.0.1
by design (local tool, no auth).

Usage: gnvtop | python3 server.py [port]   (default 8399)
"""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8399
HOST = "127.0.0.1"


def run(cmd, timeout=2):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout if p.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def first_num(text):
    """'8 %' / '31.86 W' / '1414 MiB' -> 8 / 31.86 / 1414 ; 'N/A' -> None."""
    s = (text or "").split()
    if not s:
        return None
    try:
        f = float(s[0])
        return int(f) if f == int(f) else f
    except ValueError:
        return None


def read_sys(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def read_nvidia():
    """One nvidia-smi XML call: full metrics + every process type (C/G/C+G)."""
    out = run(["nvidia-smi", "-q", "-x"])
    has_tool = os.path.exists("/usr/bin/nvidia-smi") or os.path.exists("/usr/local/bin/nvidia-smi")
    if out is None:
        return [], [], ("error" if has_tool else "absent")
    try:
        root = ET.fromstring(out)
    except ET.ParseError:
        return [], [], "error"
    driver = (root.findtext("driver_version") or "").strip()
    gpus, procs = [], []
    for i, g in enumerate(root.findall("gpu")):
        t = lambda p: (g.findtext(p) or "").strip()
        n = lambda p: first_num(t(p))
        gpu_id = f"nvidia-{i}"
        gpus.append({
            "id": gpu_id,
            "vendor": "NVIDIA",
            "name": t("product_name"),
            "driver": driver,
            "util": n("utilization/gpu_util"),
            "mem_util": n("utilization/memory_util"),
            "mem_used_mib": n("fb_memory_usage/used"),
            "mem_total_mib": n("fb_memory_usage/total"),
            "temp_c": n("temperature/gpu_temp"),
            "power_w": n("power_readings/power_draw"),
            "power_limit_w": n("power_readings/power_limit"),
            "fan_pct": n("fan_speed"),
            "clock_sm_mhz": n("clocks/sm_clock"),
            "clock_mem_mhz": n("clocks/mem_clock"),
        })
        for p in g.findall("./processes/process_info"):
            mem = first_num(p.findtext("used_memory"))
            procs.append({
                "pid": first_num(p.findtext("pid")),
                "type": (p.findtext("type") or "?").strip(),
                "name": (p.findtext("process_name") or "?").strip(),
                "mem_mib": mem if mem is not None else 0,
                "gpu_id": gpu_id,
            })
    return gpus, procs, "ok"


def amd_name(dev, card):
    """Marketing name: product_name sysfs if present, else the lspci description."""
    n = read_sys(dev / "product_name")
    if n:
        return n
    addr = os.path.basename(os.path.realpath(dev))
    out = run(["lspci", "-s", addr])
    if out and ": " in out:
        # "…controller: Advanced Micro Devices, Inc. [AMD/ATI] Granite Ridge [Radeon Graphics] (rev c1)"
        desc = out.split(": ", 1)[1].split(" (rev")[0]
        if "]" in desc:
            desc = desc.split("]", 1)[1].strip()
        return desc or f"AMD GPU ({card.name})"
    return f"AMD GPU ({card.name})"


def read_amd():
    """Scan DRM devices for amdgpu (vendor 0x1002) and read plain sysfs metrics."""
    gpus = []
    for card in sorted(Path("/sys/class/drm").glob("card[0-9]")):
        dev = card / "device"
        if read_sys(dev / "vendor") != "0x1002":
            continue
        used = first_num(read_sys(dev / "mem_info_vram_used"))
        total = first_num(read_sys(dev / "mem_info_vram_total"))
        mib = 1048576
        temp = power = power_cap = None
        for h in sorted(dev.glob("hwmon*")):
            if temp is None:
                v = first_num(read_sys(h / "temp1_input"))
                if v is not None:
                    temp = round(v / 1000)
            if power is None:
                v = first_num(read_sys(h / "power1_average"))
                if v is not None:
                    power = round(v / 1e6, 1)
            if power_cap is None:
                v = first_num(read_sys(h / "power1_cap"))
                if v is not None:
                    power_cap = round(v / 1e6, 1)
        gpus.append({
            "id": f"amd-{card.name}",
            "vendor": "AMD",
            "name": amd_name(dev, card),
            "driver": None,
            "util": first_num(read_sys(dev / "gpu_busy_percent")),
            "mem_util": None,
            "mem_used_mib": round(used / mib) if used is not None else None,
            "mem_total_mib": round(total / mib) if total is not None else None,
            "temp_c": temp,
            "power_w": power,
            "power_limit_w": power_cap,
            "fan_pct": None,
            "clock_sm_mhz": None,
            "clock_mem_mhz": None,
        })
    return gpus


def snapshot():
    nv_gpus, procs, nv_status = read_nvidia()
    amd_gpus = read_amd()
    return {
        "ts": time.time(),
        "hostname": socket.gethostname(),
        "port": PORT,
        "sources": {"nvidia": nv_status, "amd": "ok" if amd_gpus else "absent"},
        "gpus": nv_gpus + amd_gpus,
        "processes": procs,
    }


class Handler(BaseHTTPRequestHandler):
    restart_pending = None  # set by /api/restart; executed after the response is flushed

    def log_message(self, fmt, *args):  # quiet: 1 req/s would flood the terminal
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/api/gpus":
            self._json(200, snapshot())
        elif path == "/":
            try:
                body = (ROOT / "index.html").read_bytes()
            except OSError:
                self.send_error(500, "index.html missing")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in ("/api/restart", "/api/kill", "/api/shutdown"):
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            req = {}

        if path == "/api/kill":
            pids = req.get("pids")
            sig = {"TERM": signal.SIGTERM, "KILL": signal.SIGKILL}.get(req.get("sig"))
            if not isinstance(pids, list) or not pids or len(pids) > 100 or sig is None \
               or not all(isinstance(p, int) and p > 1 for p in pids):
                self._json(400, {"error": "pids must be a non-empty list of ints (max 100); sig must be TERM or KILL"})
                return
            results = []
            for pid in pids:
                try:
                    os.kill(pid, sig)
                    results.append({"pid": pid, "ok": True})
                except ProcessLookupError:
                    results.append({"pid": pid, "ok": False, "err": "no such process"})
                except PermissionError:
                    results.append({"pid": pid, "ok": False, "err": "permission denied"})
                except OSError as e:
                    results.append({"pid": pid, "ok": False, "err": str(e)})
            self._json(200, {"results": results})
            return

        if path == "/api/shutdown":
            self._json(200, {"ok": True})
            threading.Timer(0.3, os._exit, args=(0,)).start()  # after response is flushed
            return

        port = req.get("port")
        if not isinstance(port, int) or not (1024 <= port <= 65535):
            self._json(400, {"error": "port must be an integer in 1024-65535"})
            return
        self._json(200, {"restarting": True, "port": port})
        # re-exec after the response is on the wire; keeps one process, so the
        # `gnvtop` wrapper's signals still control the server
        threading.Timer(0.3, lambda: os.execv(
            sys.executable, [sys.executable, os.path.abspath(__file__), str(port)])).start()


def already_running(port):
    """True if whatever holds the port answers as a gnvtop API."""
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/gpus", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def main():
    try:
        srv = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        if already_running(PORT):
            print(f"gnvtop already running at http://{HOST}:{PORT}")
            sys.exit(0)
        print(f"gnvtop: cannot bind {HOST}:{PORT} — port in use by another program. Try: gnvtop <other-port>")
        sys.exit(1)
    print(f"gnvtop serving on http://{HOST}:{PORT} (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
