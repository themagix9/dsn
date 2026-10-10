"""Docker-Compose-Stacks auf dem lokalen Host steuern.

Jeder DSN-Service ist ein Compose-Projekt mit dem Namen "dsn-<service>". Die Compose-Datei wird nach
<data_dir>/stacks/<service>/docker-compose.yml geschrieben (gleicher Pfad auf Host und im Container),
damit relative Pfade im Compose-File auf dem Host funktionieren.

Im Compose-File stehen diese Variablen zur Verfügung:
  ${DSN_DATA}  -> /srv/dsn/volumes/<service>   (wird zwischen den Nodes synchronisiert)
  ${DSN_NODE}  -> Name des aktuellen Nodes
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os

import yaml

from .config import Config

log = logging.getLogger("dsn.docker")
PREFIX = "dsn-"
OVERRIDE_VERSION = 2  # erhöhen, wenn sich das generierte Override ändert -> Stacks werden neu erstellt
MANAGED_LABEL = "dsn.managed"
SERVICE_LABEL = "dsn.service"


async def run(*args: str, env: dict | None = None, timeout: float = 600) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, **(env or {})},
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, f"Timeout nach {timeout}s"
    return proc.returncode, out.decode(errors="replace")


class DockerOps:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        os.makedirs(cfg.stacks_dir, exist_ok=True)

    def project(self, svc: str) -> str:
        return PREFIX + svc

    def data_path(self, svc: str) -> str:
        return os.path.join(self.cfg.volumes_dir, svc)

    def compose_file(self, svc: str) -> str:
        return os.path.join(self.cfg.stacks_dir, svc, "docker-compose.yml")

    def env_for(self, spec: dict) -> dict:
        return {**spec.get("env", {}), "DSN_DATA": self.data_path(spec["name"]), "DSN_NODE": self.cfg.node.name}

    @staticmethod
    def spec_hash(spec: dict) -> str:
        raw = json.dumps({"c": spec["compose"], "e": spec.get("env", {}), "o": OVERRIDE_VERSION}, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def override_file(self, svc: str) -> str:
        return os.path.join(self.cfg.stacks_dir, svc, "docker-compose.dsn.yml")

    def _write(self, spec: dict):
        path = self.compose_file(spec["name"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.makedirs(self.data_path(spec["name"]), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(spec["compose"])
        with open(os.path.join(os.path.dirname(path), ".env"), "w", encoding="utf-8") as f:
            for k, v in self.env_for(spec).items():
                f.write(f"{k}={v}\n")
        # Docker darf DSN-Container NICHT selbst (z.B. nach Reboot) starten – das entscheidet allein DSN,
        # sonst laufen Services nach einem Failover kurzzeitig doppelt. Abstürze fängt der Agent-Abgleich ab.
        services = (yaml.safe_load(spec["compose"]) or {}).get("services") or {}
        # Label "dsn.managed" = einzige Kennzeichnung, an der DSN seine Stacks erkennt (NIE am Namen –
        # sonst würde DSN fremde Projekte wie "dsn-lab" für seine halten und stoppen).
        with open(self.override_file(spec["name"]), "w", encoding="utf-8") as f:
            yaml.safe_dump({"services": {s: {"restart": "no", "labels": {MANAGED_LABEL: "true",
                                                                         SERVICE_LABEL: spec["name"]}}
                                         for s in services}}, f)

    def _base(self, svc: str) -> list[str]:
        files = ["-f", self.compose_file(svc)]
        if os.path.exists(self.override_file(svc)):
            files += ["-f", self.override_file(svc)]
        return ["docker", "compose", "-p", self.project(svc), *files,
                "--env-file", os.path.join(self.cfg.stacks_dir, svc, ".env")]

    async def up(self, spec: dict, pull: bool = False) -> tuple[bool, str]:
        self._write(spec)
        args = self._base(spec["name"]) + ["up", "-d", "--remove-orphans"]
        if pull:
            args += ["--pull", "always"]
        code, out = await run(*args, env=self.env_for(spec))
        return code == 0, out[-4000:]

    async def down(self, svc: str) -> tuple[bool, str]:
        # "-p" reicht für down, das Compose-File muss nicht mehr existieren
        code, out = await run("docker", "compose", "-p", self.project(svc), "down", "--remove-orphans")
        return code == 0, out[-4000:]

    async def restart(self, svc: str) -> tuple[bool, str]:
        code, out = await run(*self._base(svc), "restart")
        return code == 0, out[-4000:]

    async def exec(self, svc: str, service: str, command: str, timeout: float = 1800) -> tuple[bool, str]:
        code, out = await run(*self._base(svc), "exec", "-T", service, "sh", "-c", command, timeout=timeout)
        return code == 0, out[-4000:]

    async def logs(self, svc: str, tail: int = 200) -> str:
        _, out = await run(*self._base(svc), "logs", "--no-color", "--tail", str(tail), timeout=30)
        return out

    async def list_stacks(self) -> dict[str, dict]:
        """Status aller von DSN verwalteten Stacks. Erkennung NUR über das Label dsn.managed=true
        (plus passender Projektname) – fremde Projekte werden nie angefasst, egal wie sie heißen."""
        code, out = await run("docker", "ps", "-a", "--filter", f"label={MANAGED_LABEL}=true",
                              "--format", "{{json .}}", timeout=30)
        if code != 0:
            raise RuntimeError(out.strip())
        stacks: dict[str, dict] = {}
        for line in out.splitlines():
            try:
                c = json.loads(line)
            except ValueError:
                continue
            labels = dict(kv.split("=", 1) for kv in c.get("Labels", "").split(",") if "=" in kv)
            proj = labels.get("com.docker.compose.project", "")
            svc = labels.get(SERVICE_LABEL, "")
            if not svc or proj != PREFIX + svc:
                continue
            status = c.get("Status", "")
            health = "unhealthy" if "(unhealthy)" in status else "healthy" if "(healthy)" in status else \
                "starting" if "(health: starting)" in status else ""
            stacks.setdefault(svc, {"containers": []})["containers"].append({
                "name": c.get("Names"), "image": c.get("Image"), "state": c.get("State"),
                "status": status, "health": health,
                "service": labels.get("com.docker.compose.service", ""),
            })
        for st in stacks.values():
            cs = st["containers"]
            if all(x["state"] == "running" and x["health"] != "unhealthy" for x in cs):
                st["status"] = "running" if all(x["health"] != "starting" for x in cs) else "starting"
            elif any(x["state"] == "running" for x in cs):
                st["status"] = "degraded"
            else:
                st["status"] = "stopped"
        return stacks
