"""Replizierter Cluster-State.

Nur der Leader verändert den State (über Operationen, siehe OPS). Jede Änderung erhöht die
Versionsnummer und wird sofort an alle Nodes verteilt. Nodes übernehmen immer die höchste Version.
Ein Kandidat bekommt nur Stimmen, wenn sein State mindestens so neu ist wie der des Wählers –
dadurch geht beim Leader-Wechsel keine bestätigte Änderung verloren.
"""
from __future__ import annotations

import copy
import json
import os
import re
import secrets
import time
import threading

import yaml

DEFAULT_SETTINGS = {
    "timeouts": {
        "heartbeat": 2,          # Sekunden zwischen Heartbeats
        "node_timeout": 10,      # ohne Heartbeat -> Node gilt als nicht erreichbar (für Quorum)
        "lease": 10,             # Gültigkeit der Leader-Lease
        "fence": 30,             # Node ohne Quorum stoppt nach X s seine Services (Self-Fencing)
        "failover": 60,          # Node ohne Heartbeat -> nach X s werden Services verschoben
        "failback_delay": 600,   # Node muss X s stabil laufen, bevor automatisch zurückverschoben wird
        "service_fail": 180,     # Stack startet X s nicht -> nächster Node
    },
    "failback_enabled": True,
    "dns": {"provider": "cloudflare", "api_token": "", "ttl": 60},
    "alerts": {
        "ntfy": {"url": "", "topic": "", "token": ""},
        "gotify": {"url": "", "token": ""},
        "email": {"host": "", "port": 587, "username": "", "password": "", "sender": "",
                  "recipients": "", "starttls": True},
        "webhooks": [],          # [{"type": "discord"|"slack", "url": "..."}]
    },
    "thresholds": {"disk": 90, "mem": 95, "cpu": 95, "rtt_ms": 250},
    "tailscale": {"enabled": False, "authkey": "", "use_for_cluster": True, "serve_dashboard": False},
}

SECRET_FIELDS = [
    ("dns", "api_token"),
    ("alerts", "ntfy", "token"),
    ("alerts", "gotify", "token"),
    ("alerts", "email", "password"),
    ("tailscale", "authkey"),
]

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


class OpError(Exception):
    pass


def default_state() -> dict:
    return {
        "version": 0,
        "cluster_id": secrets.token_hex(8),
        "nodes": {},
        "services": {},
        "assignments": {},
        "settings": copy.deepcopy(DEFAULT_SETTINGS),
        "users": {},
    }


def deep_merge(base: dict, patch: dict) -> dict:
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v
    return base


# --------------------------------------------------------------------------- Operationen

def op_node_upsert(s: dict, a: dict):
    name = a["name"]
    if not NAME_RE.match(name):
        raise OpError(f"Ungültiger Node-Name: {name}")
    node = s["nodes"].get(name, {"joined": time.time(), "drain": False})
    for k in ("address", "public_ip", "role", "labels", "leader_priority", "version", "platform"):
        if k in a:
            node[k] = a[k]
    s["nodes"][name] = node


def op_node_update(s: dict, a: dict):
    node = s["nodes"].get(a["name"])
    if not node:
        raise OpError("Node unbekannt")
    for k in ("drain", "labels", "leader_priority", "public_ip", "protect_all"):
        if k in a:
            node[k] = a[k]


def op_node_remove(s: dict, a: dict):
    name = a["name"]
    if any(x.get("node") == name for x in s["assignments"].values()):
        raise OpError("Auf dem Node laufen noch Services – erst 'drain' aktivieren und warten")
    s["nodes"].pop(name, None)


def normalize_service(spec: dict) -> dict:
    name = spec.get("name", "")
    if not NAME_RE.match(name):
        raise OpError("Service-Name: nur a-z, 0-9 und '-', max. 41 Zeichen")
    try:
        parsed = yaml.safe_load(spec.get("compose") or "")
    except yaml.YAMLError as e:
        raise OpError(f"Compose-YAML ungültig: {e}")
    if not isinstance(parsed, dict) or not isinstance(parsed.get("services"), dict):
        raise OpError("Compose-YAML braucht einen 'services:'-Block")
    sync = spec.get("sync") or {}
    mode = sync.get("mode", "none")
    if mode not in ("none", "periodic", "dump"):
        raise OpError("sync.mode muss none, periodic oder dump sein")
    if mode == "dump" and not (sync.get("dump_service") and sync.get("dump_command")):
        raise OpError("Für sync.mode=dump werden dump_service und dump_command benötigt")
    placement = spec.get("placement") or {}
    return {
        "name": name,
        "enabled": bool(spec.get("enabled", True)),
        "description": spec.get("description", ""),
        "compose": spec["compose"],
        "env": {str(k): str(v) for k, v in (spec.get("env") or {}).items()},
        "placement": {
            "preferred": list(placement.get("preferred") or []),
            "constraints": {str(k): str(v) for k, v in (placement.get("constraints") or {}).items()},
            "auto": bool(placement.get("auto", True)),
        },
        "sync": {
            "mode": mode,
            "interval": max(30, int(sync.get("interval", 300))),
            "dump_service": sync.get("dump_service", ""),
            "dump_command": sync.get("dump_command", ""),
        },
        "dns": [{"name": d["name"].strip().lower(), "proxied": bool(d.get("proxied", False))}
                for d in (spec.get("dns") or []) if d.get("name")],
        "failback": spec.get("failback"),  # None = globale Einstellung
        "adopted_from": spec.get("adopted_from"),  # Original-Container bei übernommenen Services
        "updated": time.time(),
    }


def op_service_upsert(s: dict, a: dict):
    spec = normalize_service(a)
    s["services"][spec["name"]] = spec


def op_service_remove(s: dict, a: dict):
    s["services"].pop(a["name"], None)
    s["assignments"].pop(a["name"], None)


def op_service_move(s: dict, a: dict):
    asg = s["assignments"].get(a["name"])
    if not asg:
        raise OpError("Service ist noch keinem Node zugewiesen")
    if a["node"] not in s["nodes"]:
        raise OpError("Ziel-Node unbekannt")
    asg["requested_target"] = a["node"]


def op_assignment_set(s: dict, a: dict):
    name = a["service"]
    if a.get("delete"):
        s["assignments"].pop(name, None)
        return
    asg = s["assignments"].setdefault(name, {"generation": 0})
    for k, v in a["fields"].items():
        if v is None:
            asg.pop(k, None)
        else:
            asg[k] = v


def op_settings_update(s: dict, a: dict):
    new = deep_merge(copy.deepcopy(s["settings"]), a["patch"])
    t = new["timeouts"]
    if not (t["node_timeout"] < t["fence"] < t["failover"]):
        raise OpError("Es muss gelten: node_timeout < fence < failover (sonst Split-Brain-Gefahr)")
    if t["lease"] > t["fence"]:
        raise OpError("lease darf nicht größer als fence sein")
    s["settings"] = new


def op_user_upsert(s: dict, a: dict):
    u = s["users"].get(a["username"], {"created": time.time()})
    for k in ("password_hash", "totp_secret", "role"):
        if k in a:
            u[k] = a[k]
    s["users"][a["username"]] = u


def op_user_remove(s: dict, a: dict):
    admins = [n for n, u in s["users"].items() if u.get("role") == "admin" and n != a["username"]]
    if not admins and not a.get("force"):  # force nur per CLI (Cluster-Secret) -> danach Setup-Seite
        raise OpError("Der letzte Admin kann nicht gelöscht werden")
    s["users"].pop(a["username"], None)


OPS = {
    "node_upsert": op_node_upsert,
    "node_update": op_node_update,
    "node_remove": op_node_remove,
    "service_upsert": op_service_upsert,
    "service_remove": op_service_remove,
    "service_move": op_service_move,
    "assignment_set": op_assignment_set,
    "settings_update": op_settings_update,
    "user_upsert": op_user_upsert,
    "user_remove": op_user_remove,
}


class StateStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)
            self.data["settings"] = deep_merge(copy.deepcopy(DEFAULT_SETTINGS), self.data.get("settings", {}))
        else:
            self.data = default_state()

    @property
    def version(self) -> int:
        return self.data["version"]

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self.data)

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f)
        os.replace(tmp, self.path)

    def replace_if_newer(self, new: dict) -> bool:
        with self._lock:
            if new.get("version", -1) <= self.data["version"]:
                return False
            self.data = new
            self._save()
            return True

    def apply(self, op: str, args: dict) -> int:
        if op not in OPS:
            raise OpError(f"Unbekannte Operation {op}")
        with self._lock:
            work = copy.deepcopy(self.data)
            OPS[op](work, args)
            work["version"] += 1
            self.data = work
            self._save()
            return work["version"]

    # Hilfsfunktionen für Lesezugriffe
    def settings(self) -> dict:
        return self.data["settings"]

    def timeouts(self) -> dict:
        return self.data["settings"]["timeouts"]


def mask_secrets(settings: dict) -> dict:
    out = copy.deepcopy(settings)
    for path in SECRET_FIELDS:
        d = out
        for k in path[:-1]:
            d = d.get(k, {})
        if d.get(path[-1]):
            d[path[-1]] = "********"
    for wh in out.get("alerts", {}).get("webhooks", []):
        if wh.get("url"):
            wh["url"] = wh["url"][:40] + "…"
    return out


def strip_masked(patch: dict, current: dict) -> dict:
    """Maskierte Werte ('********') aus einem Settings-Patch entfernen, damit sie nicht überschrieben werden."""
    patch = copy.deepcopy(patch)
    for path in SECRET_FIELDS:
        d = patch
        for k in path[:-1]:
            d = d.get(k) if isinstance(d, dict) else None
            if d is None:
                break
        if isinstance(d, dict) and d.get(path[-1]) == "********":
            d.pop(path[-1])
    whs = patch.get("alerts", {}).get("webhooks")
    if isinstance(whs, list):
        cur = current.get("alerts", {}).get("webhooks", [])
        for i, wh in enumerate(whs):
            if wh.get("url", "").endswith("…") and i < len(cur):
                wh["url"] = cur[i]["url"]
    return patch
