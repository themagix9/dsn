"""Host-Metriken: CPU, RAM, Disks, Netzwerk, Temperaturen und Host-Checks (z.B. RAID)."""
from __future__ import annotations

import glob
import json
import os
import time

import psutil

from .config import Config


class MetricsCollector:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._last_net = None
        psutil.cpu_percent(None)  # erste Messung initialisieren

    def _disk_path(self, path: str) -> str:
        root = self.cfg.host_root
        if root and os.path.isdir(root):
            return os.path.join(root, path.lstrip("/"))
        return path

    def collect(self) -> dict:
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()
        disks = []
        for d in self.cfg.disks:
            try:
                u = psutil.disk_usage(self._disk_path(d.path))
                disks.append({"name": d.name, "path": d.path, "total": u.total, "used": u.used,
                              "percent": u.percent})
            except OSError as e:
                disks.append({"name": d.name, "path": d.path, "error": str(e)})

        net = psutil.net_io_counters()
        now = time.time()
        rx_rate = tx_rate = 0.0
        if self._last_net:
            t0, rx0, tx0 = self._last_net
            dt = max(now - t0, 0.001)
            rx_rate = max(net.bytes_recv - rx0, 0) / dt
            tx_rate = max(net.bytes_sent - tx0, 0) / dt
        self._last_net = (now, net.bytes_recv, net.bytes_sent)

        temps = {}
        try:
            for chip, entries in (psutil.sensors_temperatures() or {}).items():
                vals = [e.current for e in entries if e.current]
                if vals:
                    temps[chip] = round(max(vals), 1)
        except (AttributeError, OSError):
            pass

        return {
            "ts": now,
            "cpu": psutil.cpu_percent(None),
            "cpu_count": psutil.cpu_count(),
            "load": list(os.getloadavg()) if hasattr(os, "getloadavg") else [0, 0, 0],
            "mem_total": vm.total,
            "mem_used": vm.total - vm.available,
            "mem_percent": vm.percent,
            "swap_percent": sw.percent,
            "disks": disks,
            "net_rx": rx_rate,
            "net_tx": tx_rate,
            "temps": temps,
            "boot_time": psutil.boot_time(),
            "hostchecks": self.hostchecks(),
        }

    def hostchecks(self) -> list[dict]:
        """Liest JSON-Ergebnisse von Host-Skripten (z.B. scripts/raid-check.sh).

        Format: {"name": "RAID", "status": "ok|warning|critical", "summary": "...", "details": [...], "ts": 123}
        """
        out = []
        for path in sorted(glob.glob(os.path.join(self.cfg.hostchecks_dir, "*.json"))):
            try:
                with open(path, encoding="utf-8") as f:
                    chk = json.load(f)
                age = time.time() - float(chk.get("ts", 0))
                if age > 900:
                    chk["status"] = "warning"
                    chk["summary"] = f"Check veraltet ({int(age / 60)} min) – läuft der Timer? " + chk.get("summary", "")
                out.append(chk)
            except (OSError, ValueError) as e:
                out.append({"name": os.path.basename(path), "status": "warning", "summary": f"Lesefehler: {e}"})
        return out
