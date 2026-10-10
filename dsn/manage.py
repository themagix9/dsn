"""Portainer-ähnliche Verwaltung des lokalen Docker: Container, Stacks, Images, Volumes, Netzwerke, Konsole.

Schutzregeln:
* Der DSN-Agent selbst kann über das Dashboard weder gestoppt noch gelöscht werden.
* HA-Services (Label dsn.managed) werden über "Services" gesteuert, nicht hier – sonst würde DSN
  einen hier gestoppten Container sofort wieder starten.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import pty
import re
import signal
import struct
import termios

import yaml

from .config import Config
from .docker_ops import MANAGED_LABEL, run

log = logging.getLogger("dsn.manage")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
SHELL = "if command -v bash >/dev/null 2>&1; then exec bash; else exec sh; fi"

READ_OPS = {"containers.logs", "containers.inspect", "images.list", "volumes.list", "networks.list",
            "stacks.get"}
WRITE_OPS = {"containers.action", "images.remove", "images.prune", "volumes.remove", "volumes.prune",
             "networks.create", "networks.remove", "networks.prune", "stacks.action", "stacks.deploy"}


class ManageError(Exception):
    pass


def _jsonlines(out: str) -> list[dict]:
    res = []
    for line in out.splitlines():
        try:
            res.append(json.loads(line))
        except ValueError:
            pass
    return res


class Manager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.local_dir = os.path.join(cfg.data_dir, "local-stacks")

    async def _docker(self, *args: str, timeout: float = 120) -> str:
        code, out = await run("docker", *args, timeout=timeout)
        if code != 0:
            raise ManageError(out.strip()[-1500:] or f"docker {args[0]} fehlgeschlagen")
        return out

    async def _labels(self, ref: str) -> dict:
        out = await self._docker("inspect", "--format", "{{json .Config.Labels}}", ref)
        return json.loads(out.strip() or "{}") or {}

    async def _guard(self, ref: str, action: str):
        labels = await self._labels(ref)
        if labels.get("dsn.agent") == "true" and action not in ("restart",):
            raise ManageError("Das ist der DSN-Agent dieses Servers – er kann hier nicht gestoppt/gelöscht werden")
        if labels.get(MANAGED_LABEL) == "true" and action in ("stop", "kill", "remove", "pause"):
            raise ManageError(f"Dieser Container gehört zum HA-Service „{labels.get('dsn.service')}“ – bitte unter "
                              f"Services steuern (sonst startet DSN ihn sofort wieder)")

    # ------------------------------------------------------------------ Dispatcher
    async def handle(self, op: str, a: dict):
        fn = {
            "containers.action": self.container_action, "containers.logs": self.container_logs,
            "containers.inspect": self.container_inspect,
            "images.list": self.images_list, "images.remove": self.image_remove, "images.prune": self.images_prune,
            "volumes.list": self.volumes_list, "volumes.remove": self.volume_remove, "volumes.prune": self.volumes_prune,
            "networks.list": self.networks_list, "networks.create": self.network_create,
            "networks.remove": self.network_remove, "networks.prune": self.networks_prune,
            "stacks.action": self.stack_action, "stacks.deploy": self.stack_deploy, "stacks.get": self.stack_get,
        }.get(op)
        if not fn:
            raise ManageError(f"Unbekannte Operation {op}")
        return await fn(**a)

    # ------------------------------------------------------------------ Container
    async def container_action(self, id: str, action: str):
        if action not in ("start", "stop", "restart", "kill", "pause", "unpause", "remove"):
            raise ManageError("Unbekannte Aktion")
        await self._guard(id, action)
        if action == "remove":
            out = await self._docker("rm", "-f", id)
        else:
            out = await self._docker(action, id, timeout=180)
        return {"ok": True, "output": out.strip()}

    async def container_logs(self, id: str, tail: int = 300):
        code, out = await run("docker", "logs", "--tail", str(min(int(tail), 5000)), "--timestamps", id, timeout=30)
        return {"logs": out}

    async def container_inspect(self, id: str):
        return json.loads(await self._docker("inspect", id))[0]

    # ------------------------------------------------------------------ Images
    async def images_list(self):
        images = _jsonlines(await self._docker("image", "ls", "--no-trunc", "--format", "{{json .}}"))
        used = set((await self._docker("ps", "-a", "--format", "{{.Image}}")).split())
        used_ids = set()
        if used:
            code, out = await run("docker", "image", "inspect", "--format", "{{.Id}}", *used, timeout=60)
            used_ids = set(out.split()) if code == 0 else set()
        for im in images:
            ref = f"{im.get('Repository')}:{im.get('Tag')}"
            im["in_use"] = im.get("ID") in used_ids or ref in used
        return images

    async def image_remove(self, id: str, force: bool = False):
        args = ["image", "rm"] + (["-f"] if force else []) + [id]
        return {"ok": True, "output": (await self._docker(*args)).strip()}

    async def images_prune(self, all: bool = False):
        args = ["image", "prune", "-f"] + (["-a"] if all else [])
        return {"ok": True, "output": (await self._docker(*args, timeout=600)).strip()}

    # ------------------------------------------------------------------ Volumes
    async def volumes_list(self):
        vols = _jsonlines(await self._docker("volume", "ls", "--format", "{{json .}}"))
        dangling = set((await self._docker("volume", "ls", "-q", "-f", "dangling=true")).split())
        for v in vols:
            v["in_use"] = v.get("Name") not in dangling
            v["protected"] = v.get("Name") == "dsn-data"
        return vols

    async def volume_remove(self, name: str):
        if name == "dsn-data":
            raise ManageError("dsn-data enthält die DSN-Konfiguration und Daten dieses Nodes")
        return {"ok": True, "output": (await self._docker("volume", "rm", name)).strip()}

    async def volumes_prune(self):
        # nur anonyme, ungenutzte Volumes (Docker-Standard ab 23); benannte bleiben erhalten
        return {"ok": True, "output": (await self._docker("volume", "prune", "-f", timeout=600)).strip()}

    # ------------------------------------------------------------------ Netzwerke
    async def networks_list(self):
        nets = _jsonlines(await self._docker("network", "ls", "--format", "{{json .}}"))
        for n in nets:
            n["builtin"] = n.get("Name") in ("bridge", "host", "none")
        return nets

    async def network_create(self, name: str, driver: str = "bridge"):
        if not NAME_RE.match(name):
            raise ManageError("Netzwerkname: a-z, 0-9, '-' und '_'")
        return {"ok": True, "output": (await self._docker("network", "create", "--driver", driver, name)).strip()}

    async def network_remove(self, name: str):
        return {"ok": True, "output": (await self._docker("network", "rm", name)).strip()}

    async def networks_prune(self):
        return {"ok": True, "output": (await self._docker("network", "prune", "-f")).strip()}

    # ------------------------------------------------------------------ Stacks (ohne HA)
    async def stack_action(self, project: str, action: str):
        if action not in ("start", "stop", "restart", "down", "pull"):
            raise ManageError("Unbekannte Aktion")
        out = await self._docker("ps", "-a", "--filter", f"label=com.docker.compose.project={project}",
                                 "--format", "{{.Labels}}")
        if "dsn.agent=true" in out:
            raise ManageError("Das ist der DSN-Agent dieses Servers")
        if f"{MANAGED_LABEL}=true" in out:
            raise ManageError("Das ist ein HA-Service – bitte unter Services steuern")
        if action == "pull":
            f = os.path.join(self.local_dir, project, "docker-compose.yml")
            if not os.path.exists(f):
                raise ManageError("Update (pull) geht nur für Stacks, die über DSN angelegt wurden")
            out = await self._docker("compose", "-p", project, "-f", f, "up", "-d", "--pull", "always", timeout=900)
        else:
            out = await self._docker("compose", "-p", project, action, timeout=300)
        if action == "down":
            # bei DSN-angelegten lokalen Stacks auch die gespeicherte Definition entfernen
            d = os.path.join(self.local_dir, project)
            if os.path.isdir(d):
                for fn in os.listdir(d):
                    os.remove(os.path.join(d, fn))
                os.rmdir(d)
        return {"ok": True, "output": out.strip()[-2000:]}

    async def stack_get(self, project: str):
        d = os.path.join(self.local_dir, project)
        f = os.path.join(d, "docker-compose.yml")
        if not os.path.exists(f):
            raise ManageError("Nur Stacks, die über DSN angelegt wurden, können hier bearbeitet werden")
        with open(f, encoding="utf-8") as fh:
            compose = fh.read()
        env = ""
        if os.path.exists(os.path.join(d, ".env")):
            with open(os.path.join(d, ".env"), encoding="utf-8") as fh:
                env = fh.read()
        return {"name": project, "compose": compose, "env": env}

    async def stack_deploy(self, name: str, compose: str, env: str = ""):
        if not NAME_RE.match(name) or name.startswith("dsn"):
            raise ManageError("Stack-Name: a-z, 0-9, '-', '_' (nicht mit 'dsn' beginnend)")
        try:
            doc = yaml.safe_load(compose)
        except yaml.YAMLError as e:
            raise ManageError(f"Compose-YAML ungültig: {e}")
        if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict):
            raise ManageError("Compose-YAML braucht einen 'services:'-Block")
        d = os.path.join(self.local_dir, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "docker-compose.yml"), "w", encoding="utf-8") as fh:
            fh.write(compose)
        with open(os.path.join(d, ".env"), "w", encoding="utf-8") as fh:
            fh.write(env.replace("\r", ""))
        out = await self._docker("compose", "-p", name, "-f", os.path.join(d, "docker-compose.yml"),
                                 "--env-file", os.path.join(d, ".env"), "up", "-d", "--remove-orphans", timeout=900)
        return {"ok": True, "output": out.strip()[-2000:]}

    # ------------------------------------------------------------------ Selbst-Update
    async def self_update(self) -> dict:
        """Neuestes DSN-Image ziehen und den Agenten neu erstellen – über einen Helfer-Container,
        denn ein Container kann sich nicht selbst ersetzen."""
        from .adopt import win_to_vm
        out = await self._docker("ps", "--filter", "label=dsn.agent=true", "--filter",
                                 "label=com.docker.compose.service=dsn", "--format", "{{.ID}}")
        ids = out.split()
        if not ids:
            raise ManageError("DSN-Container nicht gefunden (nicht per Docker Compose gestartet?)")
        info = json.loads(await self._docker("inspect", ids[0]))[0]
        labels, image = info["Config"]["Labels"] or {}, info["Config"]["Image"]
        if "/" not in image:
            raise ManageError(f"Diese Installation nutzt das lokal gebaute Image {image} – "
                              f"Update per install.sh/install.ps1 --update")
        project = labels.get("com.docker.compose.project", "dsn")
        cfile = win_to_vm(labels.get("com.docker.compose.project.config_files", "").split(",")[0])
        wdir = win_to_vm(labels.get("com.docker.compose.project.working_dir", ""))
        if not cfile or not wdir:
            raise ManageError("Compose-Datei des Agenten unbekannt")
        await run("docker", "rm", "-f", "dsn-updater", timeout=30)
        script = (f"sleep 2; docker compose -p {project} -f '{cfile}' pull && "
                  f"docker compose -p {project} -f '{cfile}' up -d")
        await self._docker("run", "-d", "--rm", "--name", "dsn-updater", "--label", "dsn.agent=true",
                           "-v", "/var/run/docker.sock:/var/run/docker.sock", "-v", f"{wdir}:{wdir}",
                           "--entrypoint", "sh", image, "-c", script)
        return {"ok": True, "image": image}

    # ------------------------------------------------------------------ Konsole
    async def console(self, container: str, send, receive):
        """Interaktive Shell im Container über ein PTY. send(bytes)/receive()->dict sind WebSocket-Adapter."""
        labels = await self._labels(container)
        if labels.get("dsn.agent") == "true":
            raise ManageError("Konsole im DSN-Agenten ist aus Sicherheitsgründen gesperrt")
        master, slave = pty.openpty()
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-it", "-e", "TERM=xterm-256color", container, "sh", "-c", SHELL,
            stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def on_read():
            try:
                data = os.read(master, 65536)
            except OSError:
                data = b""
            queue.put_nowait(data)
            if not data:
                loop.remove_reader(master)
        loop.add_reader(master, on_read)

        async def pump_out():
            while True:
                data = await queue.get()
                if not data:
                    return
                await send(data)

        async def pump_in():
            while True:
                msg = await receive()
                if msg is None:
                    return
                if msg.get("t") == "i":
                    os.write(master, msg["d"].encode())
                elif msg.get("t") == "r":
                    fcntl.ioctl(master, termios.TIOCSWINSZ,
                                struct.pack("HHHH", int(msg["rows"]), int(msg["cols"]), 0, 0))
                    try:
                        proc.send_signal(signal.SIGWINCH)
                    except ProcessLookupError:
                        pass

        tasks = [asyncio.create_task(pump_out()), asyncio.create_task(pump_in()),
                 asyncio.create_task(proc.wait())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            try:
                loop.remove_reader(master)
            except (ValueError, OSError):
                pass
            os.close(master)
            if proc.returncode is None:
                proc.terminate()
