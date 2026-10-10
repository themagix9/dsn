"""Lokale Node-Konfiguration (/etc/dsn/config.yml).

Alles, was clusterweit gilt (Services, Timeouts, DNS, Alarme, Benutzer), liegt NICHT hier,
sondern im replizierten Cluster-State (siehe state.py).
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field

import yaml


@dataclass
class DiskConfig:
    name: str
    path: str  # Pfad auf dem Host, z.B. "/" oder "/srv/raid600"


@dataclass
class NodeConfig:
    name: str
    address: str                 # Overlay-IP (WireGuard/Tailscale) – darüber läuft der Cluster-Traffic
    public_ip: str = ""          # Öffentliche IP – wird bei Failover in Cloudflare-DNS eingetragen
    role: str = "worker"         # worker = führt Container aus, witness = nur Quorum-Stimme
    labels: dict = field(default_factory=dict)
    leader_priority: int = 100   # kleiner = bevorzugter Leader
    platform: str = "linux"      # linux | windows (Docker Desktop) – nur zur Anzeige/Pfadlogik


@dataclass
class Config:
    node: NodeConfig
    secret: str
    seeds: list[str]
    port: int = 7946
    bind: str = "0.0.0.0"
    rsync_port: int = 8873
    rsync_bind: str = ""         # leer = auf node.address binden (Linux, Host-Netz); Docker Desktop: 0.0.0.0
    data_dir: str = "/var/lib/dsn"
    volumes_dir: str = "/srv/dsn/volumes"
    host_root: str = "/host"     # Host-Root-Mount für Disk-Metriken
    disks: list[DiskConfig] = field(default_factory=list)
    path: str = ""               # woher die Konfiguration geladen wurde
    advertise: str = ""          # zur Laufzeit gesetzt, z.B. Tailscale-IP (überschreibt node.address)

    @property
    def stacks_dir(self) -> str:
        return os.path.join(self.data_dir, "stacks")

    @property
    def hostchecks_dir(self) -> str:
        return os.path.join(self.data_dir, "hostchecks")

    @property
    def rsync_password(self) -> str:
        return hashlib.sha256((self.secret + ":rsync").encode()).hexdigest()[:32]

    @property
    def endpoint(self) -> str:
        return f"{self.advertise or self.node.address}:{self.port}"


class NotConfigured(Exception):
    """Noch keine Konfiguration vorhanden -> DSN startet im Pairing-Modus."""


def default_data_dir() -> str:
    return os.environ.get("DSN_DATA_DIR", "/var/lib/dsn")


def config_candidates() -> list[str]:
    if os.environ.get("DSN_CONFIG"):
        return [os.environ["DSN_CONFIG"]]
    # /etc/dsn: klassische Installation (install.sh); data_dir: per Pairing erzeugt
    return ["/etc/dsn/config.yml", os.path.join(default_data_dir(), "config.yml")]


def load(path: str | None = None) -> Config:
    if not path:
        path = next((p for p in config_candidates() if os.path.isfile(p)), None)
        if not path:
            raise NotConfigured()
    with open(path, encoding="utf-8-sig") as f:  # -sig: BOM von Windows-Editoren/PowerShell tolerieren
        raw = yaml.safe_load(f) or {}

    n = raw.get("node", {})
    node = NodeConfig(
        name=n["name"],
        address=n["address"],
        public_ip=n.get("public_ip", ""),
        role=n.get("role", "worker"),
        platform=n.get("platform", "linux"),
        labels={str(k): str(v) for k, v in (n.get("labels") or {}).items()},
        leader_priority=int(n.get("leader_priority", 100)),
    )
    if node.role not in ("worker", "witness"):
        raise ValueError("node.role muss 'worker' oder 'witness' sein")

    c = raw.get("cluster", {})
    secret = os.environ.get("DSN_SECRET") or c.get("secret", "")
    if len(secret) < 24:
        raise ValueError("cluster.secret fehlt oder ist zu kurz (mind. 24 Zeichen)")

    return Config(
        node=node,
        secret=secret,
        seeds=list(c.get("seeds") or []),
        port=int(raw.get("port", 7946)),
        bind=raw.get("bind", "0.0.0.0"),
        rsync_port=int(raw.get("rsync_port", 8873)),
        rsync_bind=raw.get("rsync_bind", ""),
        data_dir=raw.get("data_dir", default_data_dir()),
        volumes_dir=raw.get("volumes_dir", "/srv/dsn/volumes"),
        host_root=raw.get("host_root", "/host"),
        disks=[DiskConfig(**d) for d in (raw.get("disks") or [{"name": "root", "path": "/"}])],
        path=path,
    )
