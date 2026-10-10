"""Bestehende Container/Stacks eines Hosts erkennen und in DSN übernehmen ("In HA aufnehmen").

Ablauf einer Übernahme (auf dem Node, auf dem das Original läuft):
  1. Compose-File beschaffen: aus dem Compose-Projekt bzw. Portainer-Stack lesen, sonst aus
     `docker inspect` erzeugen (z.B. für per `docker run` gestartete Container).
  2. Ausgewählte Volumes/Bind-Mounts auf ${DSN_DATA}/<key> umschreiben.
  3. Original-Container stoppen und Docker-Autostart abschalten (sie bleiben erhalten -> Rückweg möglich).
  4. Daten nach /srv/dsn/volumes/<service>/<key> KOPIEREN (Originaldaten bleiben unangetastet).
  5. DSN-Service anlegen (bevorzugter Node = dieser Node) -> DSN startet ihn als "dsn-<service>".
"""
from __future__ import annotations

import json
import logging
import os
import re

import yaml

from .config import Config
from .docker_ops import MANAGED_LABEL, SERVICE_LABEL as DSN_SERVICE_LABEL, run

log = logging.getLogger("dsn.adopt")

PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
CONFIG_LABEL = "com.docker.compose.project.config_files"
WORKDIR_LABEL = "com.docker.compose.project.working_dir"

# Diese Pfade werden standardmäßig NICHT übernommen (hostspezifisch)
SYSTEM_PATHS = ("/var/run", "/run", "/proc", "/sys", "/dev", "/etc/localtime", "/etc/timezone",
                "/etc/hostname", "/etc/hosts", "/var/lib/docker/containers", "/lib/modules")


class AdoptError(Exception):
    pass


def is_agent(c: dict) -> bool:
    """Der DSN-Agent selbst (Label, oder ältere Installationen: Image dsn:* mit 'python -m dsn')."""
    labels = c["Config"].get("Labels") or {}
    return labels.get("dsn.agent") == "true" or (
        c["Config"]["Image"].startswith("dsn:") and "dsn" in (c["Config"].get("Entrypoint") or []))


def win_to_vm(path: str) -> str:
    """Docker Desktop: 'C:\\Users\\x\\app\\docker-compose.yml' -> '/run/desktop/mnt/host/c/Users/x/app/docker-compose.yml'."""
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", path)
    if not m:
        return path
    return f"/run/desktop/mnt/host/{m.group(1).lower()}/" + m.group(2).replace("\\", "/")


def _key(service: str, dest: str) -> str:
    k = re.sub(r"[^a-zA-Z0-9_.-]+", "-", f"{service}-{dest.strip('/')}").strip("-").lower()
    return k or service


def _is_system(src: str) -> bool:
    return any(src == p or src.startswith(p + "/") for p in SYSTEM_PATHS) or src.endswith("docker.sock")


class Adopter:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def host_path(self, path: str) -> str:
        root = self.cfg.host_root
        if root and os.path.isdir(root):
            return os.path.join(root, path.lstrip("/"))
        return path

    # ------------------------------------------------------------------ Inventar
    async def inspect_all(self) -> list[dict]:
        code, out = await run("docker", "ps", "-aq", timeout=30)
        if code != 0:
            raise AdoptError(out.strip())
        ids = out.split()
        if not ids:
            return []
        code, out = await run("docker", "inspect", *ids, timeout=60)
        if code != 0:
            raise AdoptError(out.strip())
        return json.loads(out)

    async def inventory(self) -> dict:
        """Alle Container des Hosts, gruppiert nach Compose-Projekt (Portainer-Stacks sind auch Compose-Projekte)."""
        stacks: dict[str, dict] = {}
        standalone = []
        for c in await self.inspect_all():
            labels = c["Config"].get("Labels") or {}
            info = {
                "id": c["Id"][:12],
                "name": c["Name"].lstrip("/"),
                "image": c["Config"]["Image"],
                "state": c["State"]["Status"],
                "health": (c["State"].get("Health") or {}).get("Status", ""),
                "restart": (c["HostConfig"].get("RestartPolicy") or {}).get("Name", ""),
                "ports": sorted({f"{b.get('HostPort')}->{p}" for p, bs in (c["HostConfig"].get("PortBindings") or {}).items()
                                 for b in (bs or []) if b.get("HostPort")}),
                "mounts": [{"type": m["Type"], "source": m.get("Source", ""), "destination": m["Destination"],
                            "name": m.get("Name", "")} for m in c.get("Mounts", [])],
                "service": labels.get(SERVICE_LABEL, ""),
                "agent": is_agent(c),
                "managed": labels.get(MANAGED_LABEL) == "true",
                "dsn_service": labels.get(DSN_SERVICE_LABEL, ""),
            }
            proj = labels.get(PROJECT_LABEL)
            if proj:
                st = stacks.setdefault(proj, {
                    "name": proj, "containers": [],
                    "config_files": labels.get(CONFIG_LABEL, ""), "working_dir": labels.get(WORKDIR_LABEL, ""),
                })
                st["containers"].append(info)
            else:
                standalone.append(info)
        for st in stacks.values():
            st["managed"] = any(c["managed"] for c in st["containers"])
            st["dsn_service"] = next((c["dsn_service"] for c in st["containers"] if c["managed"]), "")
            st["agent"] = any(c["agent"] for c in st["containers"])
            st["portainer"] = st["config_files"].startswith("/data/compose/")
            states = {c["state"] for c in st["containers"]}
            st["state"] = "running" if states == {"running"} else "stopped" if "running" not in states else "partial"
        return {"stacks": sorted(stacks.values(), key=lambda s: s["name"]),
                "containers": sorted(standalone, key=lambda c: c["name"])}

    # ------------------------------------------------------------------ Compose beschaffen
    def _portainer_data_dir(self, containers: list[dict]) -> str | None:
        for c in containers:
            if "portainer/portainer" in c["Config"]["Image"]:
                for m in c.get("Mounts", []):
                    if m["Destination"] == "/data":
                        return m["Source"]
        return None

    def _read_compose_files(self, config_files: str, all_containers: list[dict]) -> tuple[str | None, str]:
        """Liest das Compose-File eines Projekts vom Host. Gibt (Text, Verzeichnis) zurück."""
        for path in [p for p in config_files.split(",") if p]:
            path = win_to_vm(path)
            candidates = [path]
            if path.startswith("/data/compose/"):
                pdata = self._portainer_data_dir(all_containers)
                if pdata:
                    candidates.insert(0, pdata.rstrip("/") + path[len("/data"):])
            for cand in candidates:
                hp = self.host_path(cand)
                if os.path.isfile(hp):
                    with open(hp, encoding="utf-8") as f:
                        return f.read(), os.path.dirname(hp)
        return None, ""

    @staticmethod
    def _read_env_file(path: str) -> dict:
        env = {}
        if not os.path.isfile(path):
            return env
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
        return env

    async def _generate(self, containers: list[dict], warnings: list[str]) -> dict:
        """Compose-Definition aus `docker inspect` erzeugen (für docker-run-Container oder fehlende Dateien)."""
        images = {c["Config"]["Image"] for c in containers}
        code, out = await run("docker", "image", "inspect", *images, timeout=60)
        img_cfg = {}
        if code == 0:
            for im in json.loads(out):
                for tag in im.get("RepoTags") or []:
                    img_cfg[tag] = im["Config"]
                img_cfg[im["Id"]] = im["Config"]
        services = {}
        for c in containers:
            cfg, hc = c["Config"], c["HostConfig"]
            labels = cfg.get("Labels") or {}
            name = labels.get(SERVICE_LABEL) or re.sub(r"[^a-z0-9_-]", "-", c["Name"].lstrip("/").lower())
            icfg = img_cfg.get(cfg["Image"]) or img_cfg.get(c["Image"]) or {}
            svc: dict = {"image": cfg["Image"]}
            img_env = set(icfg.get("Env") or [])
            env = [e for e in cfg.get("Env") or [] if e not in img_env]
            if env:
                svc["environment"] = dict(e.split("=", 1) if "=" in e else (e, "") for e in env)
            if cfg.get("Cmd") and cfg.get("Cmd") != icfg.get("Cmd"):
                svc["command"] = cfg["Cmd"]
            if cfg.get("Entrypoint") and cfg.get("Entrypoint") != icfg.get("Entrypoint"):
                svc["entrypoint"] = cfg["Entrypoint"]
            ports = []
            for p, binds in (hc.get("PortBindings") or {}).items():
                cport, proto = p.split("/")
                for b in binds or []:
                    hp = b.get("HostPort")
                    if hp:
                        ip = f"{b['HostIp']}:" if b.get("HostIp") not in ("", "0.0.0.0", "::") else ""
                        ports.append(f"{ip}{hp}:{cport}" + ("/udp" if proto == "udp" else ""))
            if ports:
                svc["ports"] = ports
            vols = []
            for m in c.get("Mounts", []):
                if m["Type"] == "volume":
                    vols.append(f"{m['Name']}:{m['Destination']}" + ("" if m.get("RW", True) else ":ro"))
                elif m["Type"] == "bind":
                    vols.append(f"{m['Source']}:{m['Destination']}" + ("" if m.get("RW", True) else ":ro"))
            if vols:
                svc["volumes"] = vols
            rp = (hc.get("RestartPolicy") or {}).get("Name")
            svc["restart"] = rp if rp and rp != "no" else "unless-stopped"
            mode = hc.get("NetworkMode", "")
            if mode == "host":
                svc["network_mode"] = "host"
            elif mode and mode not in ("default", "bridge") and not mode.endswith("_default") \
                    and not mode.startswith("container:"):
                svc["networks"] = [mode]
                warnings.append(f"Netzwerk '{mode}' muss auf allen Nodes existieren (docker network create {mode})")
            for k in ("Privileged",):
                if hc.get(k):
                    svc["privileged"] = True
            if hc.get("CapAdd"):
                svc["cap_add"] = hc["CapAdd"]
            if hc.get("Devices"):
                svc["devices"] = [f"{d['PathOnHost']}:{d['PathInContainer']}" for d in hc["Devices"]]
                warnings.append(f"{name}: nutzt Geräte ({', '.join(svc['devices'])}) – müssen auf allen Nodes vorhanden sein")
            if hc.get("ExtraHosts"):
                svc["extra_hosts"] = hc["ExtraHosts"]
            user_labels = {k: v for k, v in labels.items() if not k.startswith(("com.docker.compose", "org.opencontainers"))
                           and (icfg.get("Labels") or {}).get(k) != v}
            if user_labels:
                svc["labels"] = user_labels
            services[name] = svc
        doc: dict = {"services": services}
        named = sorted({v.split(":")[0] for s in services.values() for v in s.get("volumes", [])
                        if not v.startswith("/")})
        if named:
            doc["volumes"] = {n: {"external": True} for n in named}
        nets = sorted({n for s in services.values() for n in s.get("networks", [])})
        if nets:
            doc["networks"] = {n: {"external": True} for n in nets}
        return doc

    # ------------------------------------------------------------------ Vorschau
    async def plan(self, kind: str, ref: str, select: dict[str, bool] | None = None) -> dict:
        """Erzeugt die Übernahme-Vorschau. `select` = {mount_key: True/False} überschreibt die Vorauswahl."""
        all_c = await self.inspect_all()
        if kind == "stack":
            containers = [c for c in all_c if (c["Config"].get("Labels") or {}).get(PROJECT_LABEL) == ref]
        else:
            containers = [c for c in all_c if c["Id"].startswith(ref) or c["Name"].lstrip("/") == ref]
        if not containers:
            raise AdoptError(f"{ref} nicht gefunden")
        if any((c["Config"].get("Labels") or {}).get(MANAGED_LABEL) == "true" for c in containers):
            raise AdoptError("Dieser Stack wird bereits von DSN verwaltet")
        if any(is_agent(c) for c in containers):
            raise AdoptError("Das ist der DSN-Agent selbst – er kann nicht ins HA aufgenommen werden")

        warnings: list[str] = []
        env: dict = {}
        source = "generated"
        labels = containers[0]["Config"].get("Labels") or {}
        doc = None
        if kind == "stack":
            text, cdir = self._read_compose_files(labels.get(CONFIG_LABEL, ""), all_c)
            if text:
                doc = yaml.safe_load(text)
                source = "portainer" if labels.get(CONFIG_LABEL, "").startswith("/data/compose/") else "compose"
                for f in (".env", "stack.env"):
                    env.update(self._read_env_file(os.path.join(cdir, f)))
                workdir = cdir
            else:
                warnings.append("Compose-File auf dem Host nicht gefunden – wurde aus den laufenden Containern "
                                "erzeugt. Bitte prüfen.")
        if doc is None:
            doc = await self._generate(containers, warnings)
            workdir = ""

        # Mounts der laufenden Container: Ziel -> Quelle (für das Kopieren)
        by_service: dict[str, dict] = {}
        for c in containers:
            sname = (c["Config"].get("Labels") or {}).get(SERVICE_LABEL) or \
                re.sub(r"[^a-z0-9_-]", "-", c["Name"].lstrip("/").lower())
            by_service.setdefault(sname, {m["Destination"]: m for m in c.get("Mounts", [])})

        mounts = []
        services = doc.get("services") or {}
        for sname, svc in services.items():
            if not isinstance(svc, dict):
                continue
            # Hostspezifisches entfernen bzw. inline machen
            if svc.pop("container_name", None):
                warnings.append(f"{sname}: container_name entfernt (DSN benennt Container selbst)")
            if "build" in svc:
                if svc.get("image"):
                    svc.pop("build")
                    warnings.append(f"{sname}: 'build' entfernt – es wird das Image {svc['image']} verwendet")
                else:
                    warnings.append(f"{sname}: nutzt 'build' ohne Image – das Image muss in eine Registry")
            if "env_file" in svc:
                files = svc.pop("env_file")
                files = files if isinstance(files, list) else [files]
                merged = {}
                for f in files:
                    p = f["path"] if isinstance(f, dict) else f
                    merged.update(self._read_env_file(os.path.join(workdir, p) if workdir else p))
                envd = svc.get("environment") or {}
                if isinstance(envd, list):
                    envd = dict(e.split("=", 1) if "=" in e else (e, "") for e in envd)
                svc["environment"] = {**merged, **envd}
                warnings.append(f"{sname}: env_file-Inhalt direkt ins Compose übernommen")

            new_vols = []
            for v in svc.get("volumes") or []:
                if isinstance(v, dict):
                    vtype, src, dest = v.get("type", "volume"), v.get("source", ""), v.get("target", "")
                else:
                    parts = str(v).split(":")
                    if len(parts) == 1:
                        new_vols.append(v)
                        continue
                    src, dest = parts[0], parts[1]
                    vtype = "bind" if src.startswith((".", "/", "~", "$")) else "volume"
                if vtype not in ("bind", "volume") or not dest:
                    new_vols.append(v)
                    continue
                live = by_service.get(sname, {}).get(dest)
                host_src = win_to_vm((live or {}).get("Source", ""))  # Docker Desktop meldet C:\...
                key = _key(sname, dest)
                is_dir = os.path.isdir(self.host_path(host_src)) if host_src else True
                default = bool(host_src) and not _is_system(host_src)
                migrate = select.get(key, default) if select else default
                if not host_src:
                    warnings.append(f"{sname}: Quelle von {dest} nicht ermittelbar (Container läuft nicht?) – bleibt unverändert")
                    migrate = False
                mounts.append({"service": sname, "destination": dest, "source": host_src, "type": vtype,
                               "key": key, "is_dir": is_dir, "migrate": migrate, "default": default,
                               "original": src})
                if migrate:
                    if isinstance(v, dict):
                        new_vols.append({**v, "type": "bind", "source": f"${{DSN_DATA}}/{key}"})
                    else:
                        new_vols.append(":".join([f"${{DSN_DATA}}/{key}", *str(v).split(":")[1:]]))
                else:
                    new_vols.append(v)
                    if vtype == "bind" and not _is_system(host_src):
                        warnings.append(f"{sname}: {src} wird nicht synchronisiert – muss auf allen Nodes existieren")
            if new_vols:
                svc["volumes"] = new_vols

        # Nicht mehr genutzte benannte Volumes entfernen
        used = set()
        for svc in services.values():
            for v in (svc or {}).get("volumes") or []:
                s = v.get("source", "") if isinstance(v, dict) else str(v).split(":")[0]
                used.add(s)
        if isinstance(doc.get("volumes"), dict):
            doc["volumes"] = {k: val for k, val in doc["volumes"].items() if k in used}
            if not doc["volumes"]:
                doc.pop("volumes")
        for n, ncfg in (doc.get("networks") or {}).items():
            if isinstance(ncfg, dict) and ncfg.get("external"):
                msg = f"Netzwerk '{n}' muss auf allen Nodes existieren (docker network create {n})"
                if msg not in warnings:
                    warnings.append(msg)

        name = re.sub(r"[^a-z0-9-]+", "-", ref.lower()).strip("-")[:41] or "service"
        return {
            "kind": kind, "ref": ref, "suggested_name": name, "source": source,
            "compose": yaml.safe_dump(doc, sort_keys=False, allow_unicode=True),
            "env": env, "mounts": mounts, "warnings": warnings,
            "containers": [{"id": c["Id"][:12], "name": c["Name"].lstrip("/"),
                            "restart": (c["HostConfig"].get("RestartPolicy") or {}).get("Name", "no")}
                           for c in containers],
        }

    # ------------------------------------------------------------------ Ausführen
    async def apply(self, service: str, containers: list[dict], mounts: list[dict]) -> dict:
        """Original stoppen, Autostart abschalten, Daten kopieren. Bei Fehler wird das Original wieder gestartet."""
        target_root = os.path.join(self.cfg.volumes_dir, service)
        todo = [m for m in mounts if m.get("migrate") and m.get("source")]
        for m in todo:
            dest = os.path.join(target_root, m["key"])
            if os.path.exists(dest) and (not os.path.isdir(dest) or os.listdir(dest)):
                raise AdoptError(f"Ziel {dest} existiert bereits und ist nicht leer – anderen Namen wählen")
        ids = [c["id"] for c in containers]
        log.info("Übernahme %s: stoppe %s", service, ", ".join(c["name"] for c in containers))
        code, out = await run("docker", "update", "--restart=no", *ids)
        if code != 0:
            raise AdoptError(f"docker update fehlgeschlagen: {out.strip()}")
        code, out = await run("docker", "stop", *ids, timeout=300)
        try:
            if code != 0:
                raise AdoptError(f"docker stop fehlgeschlagen: {out.strip()}")
            os.makedirs(target_root, exist_ok=True)
            copied = []
            for m in todo:
                src = self.host_path(m["source"])
                dest = os.path.join(target_root, m["key"])
                if m.get("is_dir", True):
                    os.makedirs(dest, exist_ok=True)
                    code, out = await run("cp", "-a", src.rstrip("/") + "/.", dest + "/", timeout=7200)
                else:
                    code, out = await run("cp", "-a", src, dest, timeout=7200)
                if code != 0:
                    raise AdoptError(f"Kopieren von {m['source']} fehlgeschlagen: {out.strip()[-300:]}")
                copied.append(m["key"])
            return {"ok": True, "copied": copied}
        except Exception:
            await self.restore(containers)
            raise

    async def restore(self, containers: list[dict]) -> dict:
        """Original-Container wieder aktivieren (Restart-Policy zurück + starten)."""
        errors = []
        for c in containers:
            code, out = await run("docker", "update", f"--restart={c.get('restart') or 'no'}", c["id"])
            if code != 0:
                errors.append(out.strip())
            code, out = await run("docker", "start", c["id"], timeout=120)
            if code != 0:
                errors.append(out.strip())
        return {"ok": not errors, "errors": errors}
