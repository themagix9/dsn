"""Tailscale-Integration: DSN betreibt auf jedem Linux-Node einen Tailscale-Container (Sidecar).

* Auth-Key wird EINMAL im Dashboard hinterlegt (Einstellungen -> Tailscale) und clusterweit repliziert.
* Ist "Cluster-Verkehr über Tailscale" aktiv, meldet sich der Node mit seiner Tailscale-IP im Cluster an.
* Optional: Dashboard per HTTPS im Tailnet (`tailscale serve`) -> https://<node>.<tailnet>.ts.net
* Windows/Docker Desktop: Dort übernimmt die normale Tailscale-App die Verbindung (kein Sidecar möglich).
"""
from __future__ import annotations

import asyncio
import json
import logging

from .cluster import Cluster
from .config import Config
from .docker_ops import run

log = logging.getLogger("dsn.tailscale")
CONTAINER = "dsn-tailscale"
IMAGE = "tailscale/tailscale:stable"


class TailscaleManager:
    def __init__(self, cfg: Config, cluster: Cluster):
        self.cfg = cfg
        self.cluster = cluster
        self.serving = False

    async def _exists(self) -> str | None:
        code, out = await run("docker", "inspect", "-f", "{{.State.Status}}", CONTAINER, timeout=15)
        return out.strip() if code == 0 else None

    async def _start(self, authkey: str):
        state = await self._exists()
        if state == "running":
            return
        if state:
            await run("docker", "start", CONTAINER, timeout=60)
            return
        if not authkey:
            raise RuntimeError("Kein Auth-Key hinterlegt")
        log.info("Starte Tailscale-Sidecar")
        code, out = await run(
            "docker", "run", "-d", "--name", CONTAINER, "--restart", "unless-stopped",
            "--label", "dsn.agent=true", "--label", "dsn.sidecar=tailscale",
            "--network", "host", "--cap-add", "NET_ADMIN", "--cap-add", "NET_RAW", "--device", "/dev/net/tun",
            "-v", "dsn-tailscale-state:/var/lib/tailscale",
            "-e", f"TS_AUTHKEY={authkey}", "-e", "TS_STATE_DIR=/var/lib/tailscale",
            "-e", f"TS_HOSTNAME={self.cfg.node.name}", "-e", "TS_USERSPACE=false", "-e", "TS_ACCEPT_DNS=false",
            IMAGE, timeout=300)
        if code != 0:
            raise RuntimeError(out.strip()[-500:])

    async def _remove(self):
        if await self._exists():
            log.info("Tailscale deaktiviert – entferne Sidecar (Anmeldung bleibt im Volume erhalten)")
            await run("docker", "rm", "-f", CONTAINER, timeout=60)

    async def _ts(self, *args: str, timeout: float = 20) -> tuple[int, str]:
        return await run("docker", "exec", CONTAINER, "tailscale", *args, timeout=timeout)

    async def status(self) -> dict:
        code, out = await self._ts("status", "--json")
        if code != 0:
            return {"state": "starting", "error": out.strip()[-300:]}
        d = json.loads(out)
        me = d.get("Self") or {}
        ips = [ip for ip in me.get("TailscaleIPs") or [] if "." in ip]
        return {"state": d.get("BackendState"), "ip": ips[0] if ips else "",
                "dns": (me.get("DNSName") or "").rstrip("."), "online": me.get("Online", False),
                "tailnet": (d.get("CurrentTailnet") or {}).get("Name", ""),
                "auth_url": d.get("AuthURL", "")}

    async def _serve(self, enable: bool):
        if enable and not self.serving:
            code, out = await self._ts("serve", "--bg", "--https=443", f"http://127.0.0.1:{self.cfg.port}")
            self.serving = code == 0
            if code != 0:
                raise RuntimeError(f"tailscale serve: {out.strip()[-300:]}")
        elif not enable and self.serving:
            await self._ts("serve", "reset")
            self.serving = False

    async def run(self):
        while True:
            ts = self.cluster.store.settings().get("tailscale") or {}
            info: dict = {"enabled": bool(ts.get("enabled"))}
            try:
                if self.cfg.node.platform != "linux":
                    info["mode"] = "app"  # Windows: Tailscale-App des Hosts
                elif not ts.get("enabled"):
                    await self._remove()
                    self.serving = False
                    self.cfg.advertise = ""
                else:
                    await self._start(ts.get("authkey", ""))
                    info.update(await self.status())
                    if info.get("state") == "Running":
                        await self._serve(bool(ts.get("serve_dashboard")))
                        info["serving"] = self.serving
                    use = ts.get("use_for_cluster", True) and info.get("state") == "Running" and info.get("ip")
                    new = info["ip"] if use else ""
                    if new != self.cfg.advertise:
                        log.info("Cluster-Adresse: %s", new or self.cfg.node.address)
                        self.cfg.advertise = new
            except Exception as e:  # noqa: BLE001 – Fehler anzeigen, Schleife läuft weiter
                info["error"] = str(e)
            self.cluster.local_tailscale = info
            await asyncio.sleep(20)
