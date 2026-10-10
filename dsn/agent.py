"""Node-Agent: bringt die lokalen Stacks auf den Soll-Zustand und synchronisiert Daten.

Level-basiert: Der Agent liest aus dem replizierten State, welche Services diesem Node zugewiesen sind,
und sorgt dafür, dass genau diese laufen. Alles andere mit dem Präfix "dsn-" wird gestoppt.
Hat der Node kein Quorum (fenced), werden alle DSN-Stacks gestoppt.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from .cluster import Cluster
from .config import Config
from .docker_ops import DockerOps, run

log = logging.getLogger("dsn.agent")
RETRY_BACKOFF = 30


class Agent:
    def __init__(self, cfg: Config, cluster: Cluster, docker: DockerOps):
        self.cfg = cfg
        self.cluster = cluster
        self.docker = docker
        self.applied: dict[str, str] = {}
        self.errors: dict[str, str] = {}
        self.last_attempt: dict[str, float] = {}
        self.failing_since: dict[str, float] = {}
        self.sync_running: set[str] = set()
        self.last_sync_start: dict[str, float] = {}
        self.docker_error = ""
        self._pw_file = os.path.join(cfg.data_dir, "rsync.pass")

    # ------------------------------------------------------------------ Soll-Zustand
    def desired(self) -> dict[str, dict]:
        if self.cfg.node.role != "worker":
            return {}
        st = self.cluster.store.data
        out = {}
        for svc, a in st["assignments"].items():
            spec = st["services"].get(svc)
            if spec and spec.get("enabled") and a.get("node") == self.cluster.me and a.get("phase") == "running":
                out[svc] = spec
        return out

    async def reconcile_once(self):
        try:
            stacks = await self.docker.list_stacks()
            self.docker_error = ""
        except Exception as e:  # noqa: BLE001
            self.docker_error = str(e)
            log.error("Docker nicht erreichbar: %s", e)
            return

        if self.cluster.fenced:
            for svc, st in stacks.items():
                log.warning("Fencing: stoppe %s", svc)
                await self.docker.down(svc)
                self.applied.pop(svc, None)
            stacks = {}
        elif self.cluster.can_run_services():
            desired = self.desired()
            now = time.time()
            for svc, spec in desired.items():
                h = DockerOps.spec_hash(spec)
                cur = stacks.get(svc)
                changed = self.applied.get(svc) != h or cur is None
                # abgestürzte Container (Docker-Restart-Policy ist für DSN-Stacks abgeschaltet) neu starten
                crashed = cur is not None and cur["status"] in ("stopped", "degraded")
                wait = RETRY_BACKOFF if self.errors.get(svc) else (0 if changed else 15)
                if (changed or crashed) and now - self.last_attempt.get(svc, 0) >= wait:
                    if crashed and not changed:
                        log.warning("%s ist abgestürzt (%s) – starte neu", svc, cur["status"])
                    self.last_attempt[svc] = now
                    log.info("Starte/aktualisiere %s", svc)
                    ok, out = await self.docker.up(spec)
                    if ok:
                        self.applied[svc] = h
                        self.errors.pop(svc, None)
                    else:
                        self.errors[svc] = out.strip()[-1500:]
                        log.error("Start von %s fehlgeschlagen: %s", svc, out[-500:])
            for svc in list(stacks):
                if svc not in desired:
                    log.info("Stoppe %s (nicht mehr diesem Node zugewiesen)", svc)
                    await self.docker.down(svc)
                    self.applied.pop(svc, None)
                    self.errors.pop(svc, None)
            try:
                stacks = await self.docker.list_stacks()
            except Exception:  # noqa: BLE001
                pass
        # sonst: Quorum noch unklar -> nichts anfassen

        desired = self.desired() if self.cluster.can_run_services() else {}
        report = {}
        now = time.time()
        for svc in set(stacks) | set(desired):
            st = stacks.get(svc, {"containers": [], "status": "missing"})
            status = st["status"]
            if svc in self.errors:
                status = "failed"
            healthy = status in ("running", "starting")
            if svc in desired and not healthy:
                self.failing_since.setdefault(svc, now)
            else:
                self.failing_since.pop(svc, None)
            report[svc] = {
                "status": status,
                "containers": st["containers"],
                "desired": svc in desired,
                "error": self.errors.get(svc, ""),
                "failing_since": self.failing_since.get(svc),
            }
        self.cluster.local_stacks = report

    async def run_reconcile(self):
        while True:
            try:
                await self.reconcile_once()
            except Exception:  # noqa: BLE001
                log.exception("Fehler beim Abgleich")
            await asyncio.sleep(5)

    # ------------------------------------------------------------------ Ungeschützte Container
    async def run_inventory(self, adopter):
        """Meldet jede Minute, welche laufenden Container auf diesem Server NICHT HA-geschützt sind."""
        while True:
            try:
                inv = await adopter.inventory()
                names = [c["name"] for s in inv["stacks"] for c in s["containers"]] + \
                        [c["name"] for c in inv["containers"]]
                running = [c for s in inv["stacks"] for c in s["containers"]] + inv["containers"]
                self.cluster.local_unprotected = sorted(
                    c["name"] for c in running
                    if c["state"] == "running" and not c["agent"] and not c["managed"] and c["name"] in names)
            except Exception as e:  # noqa: BLE001
                log.debug("Inventar fehlgeschlagen: %s", e)
            await asyncio.sleep(60)

    # ------------------------------------------------------------------ Datensync
    def sync_targets(self, spec: dict) -> list[str]:
        """Alle anderen Worker-Nodes, auf denen der Service laufen dürfte."""
        nodes = self.cluster.members()
        cons = spec["placement"]["constraints"]
        alive = self.cluster.alive_nodes()
        out = []
        for name, n in nodes.items():
            if name == self.cluster.me or n.get("role") != "worker" or name not in alive:
                continue
            labels = n.get("labels") or {}
            if all(labels.get(k) == v for k, v in cons.items()):
                out.append(name)
        return out

    async def sync_service(self, svc: str, targets: list[str], dump: bool = True) -> dict:
        spec = self.cluster.store.data["services"].get(svc)
        if not spec:
            raise ValueError("Service unbekannt")
        self.sync_running.add(svc)
        t0 = time.time()
        result = {"ts": t0, "ok": True, "error": "", "targets": {}}
        try:
            if dump and spec["sync"]["mode"] == "dump":
                ok, out = await self.docker.exec(svc, spec["sync"]["dump_service"], spec["sync"]["dump_command"])
                if not ok:
                    result.update(ok=False, error=f"Dump fehlgeschlagen: {out[-500:]}")
            src = self.docker.data_path(svc).rstrip("/") + "/"
            os.makedirs(src, exist_ok=True)
            for t in targets:
                host = self.cluster.endpoint_of(t).rsplit(":", 1)[0]
                dest = f"rsync://dsn@{host}:{self.cfg.rsync_port}/volumes/{svc}/"
                code, out = await run("rsync", "-a", "--delete", "--timeout=120", "--contimeout=10",
                                      f"--password-file={self._pw_file}", src, dest, timeout=3600)
                result["targets"][t] = {"ok": code == 0, "error": "" if code == 0 else out.strip()[-500:]}
                if code != 0:
                    result["ok"] = False
                    result["error"] = result["error"] or f"{t}: {out.strip()[-300:]}"
        finally:
            self.sync_running.discard(svc)
        result["duration"] = round(time.time() - t0, 1)
        prev = self.cluster.local_syncs.get(svc, {})
        self.cluster.local_syncs[svc] = {
            **result,
            "last_ok": t0 if result["ok"] else prev.get("last_ok"),
            "fail_count": 0 if result["ok"] else prev.get("fail_count", 0) + 1,
        }
        if not result["ok"]:
            log.warning("Sync %s fehlgeschlagen: %s", svc, result["error"])
        return result

    async def run_sync(self):
        while True:
            await asyncio.sleep(5)
            if not self.cluster.can_run_services():
                continue
            now = time.time()
            for svc, spec in self.desired().items():
                mode = spec["sync"]["mode"]
                if mode == "none" or svc in self.sync_running:
                    continue
                if self.cluster.local_stacks.get(svc, {}).get("status") not in ("running", "degraded"):
                    continue
                if now - self.last_sync_start.get(svc, 0) < spec["sync"]["interval"]:
                    continue
                targets = self.sync_targets(spec)
                if not targets:
                    continue
                self.last_sync_start[svc] = now
                asyncio.get_running_loop().create_task(self.sync_service(svc, targets))

    def accepts_sync(self, svc: str) -> bool:
        """Eingehenden Sync nur annehmen, wenn der Service hier NICHT läuft (Schutz vor Überschreiben)."""
        if svc in self.desired():
            return False
        st = self.cluster.local_stacks.get(svc)
        return not st or st["status"] in ("missing", "stopped")

    # ------------------------------------------------------------------ rsync-Daemon
    async def run_rsyncd(self):
        os.makedirs(self.cfg.volumes_dir, exist_ok=True)
        pw = self.cfg.rsync_password
        secrets_file = os.path.join(self.cfg.data_dir, "rsyncd.secrets")
        conf_file = os.path.join(self.cfg.data_dir, "rsyncd.conf")
        for path, content in ((self._pw_file, pw + "\n"), (secrets_file, f"dsn:{pw}\n")):
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            os.chmod(path, 0o600)
        api = f"{'127.0.0.1' if self.cfg.bind == '0.0.0.0' else self.cfg.bind}:{self.cfg.port}"
        env = {**os.environ, "DSN_API": api, "DSN_SECRET": self.cfg.secret}

        def write_conf() -> str:
            # Lauscht auf allen Adressen (LAN + Tailscale), nimmt aber nur Cluster-Mitglieder an.
            # rsync liest die Datei bei JEDER Verbindung neu -> neue Nodes/Adressen gelten sofort.
            # Docker Desktop: Verbindungen kommen per NAT, daher dort ohne IP-Filter (Port nur im LAN freigeben).
            allow = ""
            if self.cfg.node.platform == "linux":
                ips = {n.get("address", "").rsplit(":", 1)[0] for n in self.cluster.members().values()}
                ips |= {"127.0.0.1"}
                if (self.cluster.store.settings().get("tailscale") or {}).get("enabled"):
                    ips.add("100.64.0.0/10")
                allow = "    hosts allow = " + " ".join(sorted(i for i in ips if i)) + "\n"
            content = f"""pid file = {self.cfg.data_dir}/rsyncd.pid
uid = 0
gid = 0
use chroot = no
munge symlinks = yes
max connections = 8
reverse lookup = no
[volumes]
    path = {self.cfg.volumes_dir}
    read only = false
    auth users = dsn
    secrets file = {secrets_file}
    pre-xfer exec = /usr/local/bin/dsn-presync
{allow}"""
            try:
                with open(conf_file, encoding="utf-8") as f:
                    if f.read() == content:
                        return content
            except OSError:
                pass
            with open(conf_file, "w", encoding="utf-8") as f:
                f.write(content)
            return content

        async def keep_conf_fresh():
            while True:
                await asyncio.sleep(10)
                try:
                    write_conf()
                except Exception:  # noqa: BLE001
                    log.exception("rsyncd.conf konnte nicht aktualisiert werden")

        write_conf()
        asyncio.get_running_loop().create_task(keep_conf_fresh())
        while True:
            try:
                os.remove(f"{self.cfg.data_dir}/rsyncd.pid")
            except OSError:
                pass
            proc = await asyncio.create_subprocess_exec(
                "rsync", "--daemon", "--no-detach", f"--config={conf_file}", f"--port={self.cfg.rsync_port}",
                f"--address={self.cfg.rsync_bind or '0.0.0.0'}", env=env)
            code = await proc.wait()
            log.error("rsync-Daemon beendet (Code %s) – Neustart in 5 s", code)
            await asyncio.sleep(5)
