"""Cluster-Mitgliedschaft, Heartbeats, Leader-Wahl (Quorum-Lease) und Self-Fencing.

Funktionsweise in Kurzform:
* Jeder Node schickt alle `heartbeat` Sekunden einen Heartbeat an jeden anderen Node. Die Antwort ist
  der Status des Gegenübers – ein Round-Trip tauscht also beide Richtungen aus (inkl. Metriken).
* Quorum = Mehrheit der Cluster-Mitglieder (Witness-Nodes zählen mit). Nur wer die Mehrheit sieht,
  darf Leader werden – und der Leader braucht für jede Lease-Verlängerung die Stimmen einer Mehrheit.
  Jeder Node vergibt seine Stimme pro Lease-Zeitraum nur an einen Kandidaten -> nie zwei Leader.
* Ein Node, der länger als `fence` Sekunden kein Quorum sieht, stoppt seine Services selbst
  (Self-Fencing). Der Leader verschiebt Services erst nach `failover` Sekunden (> fence). Damit ist der
  ausgefallene Node garantiert schon "eingezäunt", bevor die Services woanders starten -> kein Split-Brain.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from collections import deque
from dataclasses import dataclass, field

import httpx

from . import __version__, alerts
from .config import Config
from .metrics import MetricsCollector
from .state import StateStore

log = logging.getLogger("dsn.cluster")
HISTORY_INTERVAL = 10       # Sekunden zwischen History-Punkten
HISTORY_POINTS = 2160       # 6 Stunden


class NoLeader(Exception):
    pass


class RemoteError(Exception):
    pass


@dataclass
class Peer:
    name: str
    endpoint: str
    last_seen: float = 0.0       # monotonic
    up_since: float = 0.0
    rtt_ms: float | None = None
    status: dict = field(default_factory=dict)
    error: str = ""


class Cluster:
    def __init__(self, cfg: Config, store: StateStore, metrics: MetricsCollector):
        self.cfg = cfg
        self.me = cfg.node.name
        self.store = store
        self.metrics = metrics
        self.started = time.monotonic()
        self.peers: dict[str, Peer] = {}

        # Leader-Wahl
        self.term = 0
        self.voted_for: str | None = None
        self.vote_expires = 0.0
        self._leader_until = 0.0
        self.leader_since = 0.0
        self.last_leader_seen = time.monotonic()

        # Quorum / Fencing
        self.has_quorum = False
        self.no_quorum_since: float | None = time.monotonic()
        self.fenced = False

        # Lokaler Laufzeitstatus (vom Agent befüllt)
        self.local_metrics: dict = {}
        self.local_stacks: dict = {}
        self.local_syncs: dict = {}
        self.local_unprotected: list = []
        self.local_tailscale: dict = {}

        self.history: dict[str, deque] = {}
        self.events: deque = deque(maxlen=500)
        self._event_ids: set[str] = set()
        self._events_path = os.path.join(cfg.data_dir, "events.jsonl")
        self._load_events()

        self._fetching = False
        self._last_join = 0.0
        self._hb_tasks: dict[str, asyncio.Task] = {}
        self.client = httpx.AsyncClient(timeout=5, headers={"Authorization": f"Bearer {cfg.secret}"})

    # ------------------------------------------------------------------ Hilfen
    @property
    def is_leader(self) -> bool:
        return time.monotonic() < self._leader_until

    def current_leader(self) -> str | None:
        if self.is_leader:
            return self.me
        if self.voted_for and self.voted_for != self.me and time.monotonic() < self.vote_expires:
            return self.voted_for
        return None

    def members(self) -> dict:
        return self.store.data["nodes"]

    def quorum_size(self) -> int:
        return len(self.members()) // 2 + 1

    def alive_nodes(self) -> set[str]:
        t = self.store.timeouts()
        now = time.monotonic()
        alive = {self.me} if self.me in self.members() else set()
        for name, p in self.peers.items():
            if name in self.members() and now - p.last_seen < t["node_timeout"]:
                alive.add(name)
        return alive

    def down_for(self, name: str) -> float:
        """Wie lange ein Node schon keinen Heartbeat geschickt hat (0 = lebt)."""
        if name == self.me:
            return 0.0
        p = self.peers.get(name)
        now = time.monotonic()
        last = max(p.last_seen if p else 0.0, self.started)
        if p and now - p.last_seen < self.store.timeouts()["node_timeout"]:
            return 0.0
        return now - last

    def stable_for(self, name: str) -> float:
        if name == self.me:
            return time.monotonic() - self.started
        p = self.peers.get(name)
        if not p or self.down_for(name) > 0:
            return 0.0
        return time.monotonic() - p.up_since

    def node_status(self, name: str) -> dict:
        if name == self.me:
            return self.my_status()
        p = self.peers.get(name)
        return p.status if p else {}

    def endpoint_of(self, name: str) -> str:
        if name == self.me:
            return self.cfg.endpoint
        node = self.members().get(name)
        if node:
            return node["address"]
        p = self.peers.get(name)
        if p:
            return p.endpoint
        raise RemoteError(f"Node {name} unbekannt")

    def can_run_services(self) -> bool:
        return self.has_quorum and not self.fenced and self.current_leader() is not None \
            and self.me in self.members()

    def my_status(self) -> dict:
        return {
            "name": self.me,
            "endpoint": self.cfg.endpoint,
            "version": self.store.version,
            "term": self.term,
            "leader": self.current_leader(),
            "has_quorum": self.has_quorum,
            "fenced": self.fenced,
            "role": self.cfg.node.role,
            "metrics": self.local_metrics,
            "stacks": self.local_stacks,
            "syncs": self.local_syncs,
            "unprotected": self.local_unprotected,
            "tailscale": self.local_tailscale,
            "dsn_version": __version__,
            "build": os.environ.get("DSN_BUILD", "lokal"),
            "ts": time.time(),
        }

    # ------------------------------------------------------------------ HTTP
    async def call(self, node: str, method: str, path: str, json_body=None, timeout: float = 5,
                   endpoint: str | None = None):
        url = f"http://{endpoint or self.endpoint_of(node)}{path}"
        r = await self.client.request(method, url, json=json_body, timeout=timeout)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise RemoteError(f"{node}: {detail}")
        return r.json()

    # ------------------------------------------------------------------ Heartbeats
    def record_status(self, status: dict, rtt_ms: float | None = None):
        name = status.get("name")
        if not name or name == self.me:
            return
        now = time.monotonic()
        p = self.peers.get(name)
        if not p:
            p = self.peers[name] = Peer(name=name, endpoint=status.get("endpoint", ""))
        was_down = now - p.last_seen >= self.store.timeouts()["node_timeout"]
        if was_down:
            p.up_since = now
        p.last_seen = now
        p.endpoint = status.get("endpoint", p.endpoint)
        p.status = status
        p.error = ""
        if rtt_ms is not None:
            p.rtt_ms = rtt_ms
        self._history(name, status.get("metrics") or {}, p.rtt_ms)
        if status.get("version", 0) > self.store.version:
            asyncio.get_running_loop().create_task(self.fetch_state(name))

    def _history(self, name: str, m: dict, rtt):
        if not m:
            return
        h = self.history.setdefault(name, deque(maxlen=HISTORY_POINTS))
        if h and m.get("ts", 0) - h[-1][0] < HISTORY_INTERVAL:
            return
        h.append((m.get("ts", time.time()), m.get("cpu", 0), m.get("mem_percent", 0),
                  rtt, m.get("net_rx", 0), m.get("net_tx", 0)))

    async def _heartbeat(self, name: str | None, endpoint: str, payload: dict):
        t0 = time.monotonic()
        try:
            r = await self.client.post(f"http://{endpoint}/internal/heartbeat", json=payload, timeout=3)
            r.raise_for_status()
            self.record_status(r.json(), (time.monotonic() - t0) * 1000)
        except (httpx.HTTPError, ValueError) as e:
            if name and name in self.peers:
                self.peers[name].error = str(e) or type(e).__name__

    async def fetch_state(self, name: str):
        if self._fetching:
            return
        self._fetching = True
        try:
            st = await self.call(name, "GET", "/internal/state", timeout=10)
            if self.store.replace_if_newer(st):
                log.info("State v%s von %s übernommen", st["version"], name)
        except (httpx.HTTPError, RemoteError) as e:
            log.debug("State-Abruf von %s fehlgeschlagen: %s", name, e)
        finally:
            self._fetching = False

    async def broadcast_state(self):
        snap = self.store.snapshot()

        async def push(name):
            try:
                await self.call(name, "POST", "/internal/state", snap, timeout=5)
            except (httpx.HTTPError, RemoteError):
                pass
        await asyncio.gather(*(push(n) for n in self.members() if n != self.me))

    # ------------------------------------------------------------------ Leader-Wahl
    def grant_vote(self, candidate: str, term: int, version: int) -> tuple[bool, int]:
        now = time.monotonic()
        if version < self.store.version or term < self.term:
            return False, self.term
        if self.voted_for not in (None, candidate) and now < self.vote_expires:
            return False, self.term
        if candidate != self.me and self.is_leader:
            return False, self.term
        self.voted_for = candidate
        self.vote_expires = now + self.store.timeouts()["lease"]
        self.term = term
        return True, self.term

    async def _campaign(self, alive: set[str]) -> bool:
        lease = self.store.timeouts()["lease"]
        start = time.monotonic()
        # Auch nach knapp verpasster Verlängerung gilt der Leader als "weiterhin im Amt", solange seine
        # eigene Stimme noch gültig ist -> kein neuer Term, kein falsches "neuer Leader"-Event
        was_leader = self.is_leader or (self.leader_since > 0 and self.voted_for == self.me
                                        and start < self.vote_expires)
        term = self.term if was_leader else self.term + 1
        ok, _ = self.grant_vote(self.me, term, self.store.version)
        if not ok:
            return False
        req = {"candidate": self.me, "term": term, "version": self.store.version}

        async def ask(name):
            try:
                return await self.call(name, "POST", "/internal/vote", req, timeout=1.5)
            except (httpx.HTTPError, RemoteError):
                return None
        results = await asyncio.gather(*(ask(n) for n in alive if n != self.me))
        votes = 1
        for r in results:
            if not r:
                continue
            if r.get("granted"):
                votes += 1
            self.term = max(self.term, r.get("term", 0))
        if votes >= self.quorum_size() and self.term == term:
            self._leader_until = start + lease * 0.8
            if not was_leader:
                self.leader_since = time.monotonic()
                log.info("Bin jetzt Leader (Term %s, %s/%s Stimmen)", term, votes, len(self.members()))
                self.emit("leader", f"{self.me} ist neuer Leader (Term {term})")
            return True
        # Niederlage: eigene Stimme freigeben, damit andere gewählt werden können
        self._leader_until = 0
        if self.voted_for == self.me:
            self.voted_for = None
        if was_leader:
            log.warning("Leader-Lease verloren (%s Stimmen)", votes)
        return False

    async def _election(self, alive: set[str]):
        if self.me not in self.members():
            return
        now = time.monotonic()
        lease = self.store.timeouts()["lease"]
        if self.is_leader or (self.voted_for == self.me and now < self.vote_expires):
            await self._campaign(alive)
            return
        leader = self.current_leader()
        if leader and leader in alive:
            self.last_leader_seen = now
            return
        if not self.has_quorum:
            return
        nodes = self.members()
        ranked = sorted(alive, key=lambda n: (nodes[n].get("leader_priority", 100), n))
        # Bevorzugter Kandidat tritt an; ist nach 3 Leases noch kein Leader da, darf jeder (mit Zufallsverzögerung)
        if ranked and ranked[0] == self.me:
            await self._campaign(alive)
        elif now - self.last_leader_seen > 3 * lease:
            await asyncio.sleep(secrets.randbelow(1000) / 1000)
            if self.current_leader() is None:
                await self._campaign(alive)

    # ------------------------------------------------------------------ Fencing
    def _update_quorum(self, alive: set[str]):
        now = time.monotonic()
        members = self.members()
        self.has_quorum = self.me in members and len(alive) >= self.quorum_size()
        if self.has_quorum:
            self.no_quorum_since = None
            if self.fenced:
                self.fenced = False
                self.emit("quorum", f"{self.me} hat wieder Quorum – Fencing aufgehoben", "info", node=self.me,
                          alert=True)
            return
        if self.no_quorum_since is None:
            self.no_quorum_since = now
        if not self.fenced and members and now - self.no_quorum_since > self.store.timeouts()["fence"]:
            self.fenced = True
            self.emit("fence", f"{self.me} hat kein Quorum (sieht {len(alive)}/{len(members)} Nodes) – "
                               f"lokale Services werden gestoppt (Self-Fencing)", "critical", node=self.me,
                      alert=True)

    # ------------------------------------------------------------------ Join
    def _my_node_entry(self) -> dict:
        n = self.cfg.node
        return {"name": n.name, "address": self.cfg.endpoint, "public_ip": n.public_ip, "role": n.role, "platform": n.platform,
                "labels": n.labels, "leader_priority": n.leader_priority, "version": __version__}

    async def _ensure_membership(self):
        entry = self._my_node_entry()
        members = self.members()
        if self.me in members:
            cur = members[self.me]
            changed = any(cur.get(k) != entry[k] for k in ("address", "role", "version", "platform"))
            if changed and self.current_leader():
                try:
                    await self.submit("node_upsert", entry)
                except Exception as e:  # noqa: BLE001
                    log.debug("Node-Update fehlgeschlagen: %s", e)
            return
        if not members and not self.cfg.seeds:
            self.store.apply("node_upsert", entry)
            log.info("Neuer Cluster gebootstrapped mit %s", self.me)
            self.emit("join", f"Cluster gegründet von {self.me}")
            return
        if time.monotonic() - self._last_join < 10:
            return
        self._last_join = time.monotonic()
        for seed in self.cfg.seeds:
            try:
                r = await self.client.post(f"http://{seed}/internal/join", json=entry, timeout=10)
                if r.status_code < 300:
                    log.info("Join-Anfrage über %s erfolgreich", seed)
                    return
                log.warning("Join über %s abgelehnt: %s", seed, r.text)
            except httpx.HTTPError as e:
                log.warning("Seed %s nicht erreichbar: %s", seed, e)

    # ------------------------------------------------------------------ Hauptschleife
    async def run(self):
        loop = asyncio.get_running_loop()
        while True:
            t = self.store.timeouts()
            started = loop.time()
            try:
                self.local_metrics = self.metrics.collect()
                self._history(self.me, self.local_metrics, 0)
                await self._ensure_membership()
                payload = self.my_status()
                targets = {n: node["address"] for n, node in self.members().items() if n != self.me}
                if self.me not in self.members():
                    for seed in self.cfg.seeds:
                        if seed != self.cfg.endpoint:
                            targets[f"seed:{seed}"] = seed
                # Heartbeats laufen unabhängig: ein toter Node (Timeout) darf den Takt nicht bremsen
                for n, ep in targets.items():
                    task = self._hb_tasks.get(n)
                    if not task or task.done():
                        self._hb_tasks[n] = loop.create_task(self._heartbeat(n, ep, payload))
                await asyncio.sleep(0.3)  # schnellen Antworten Zeit geben
                alive = self.alive_nodes()
                self._update_quorum(alive)
                await self._election(alive)
            except Exception:  # noqa: BLE001 – Schleife darf nie sterben
                log.exception("Fehler in der Cluster-Schleife")
            await asyncio.sleep(max(0.2, t["heartbeat"] - (loop.time() - started)))

    # ------------------------------------------------------------------ Operationen
    async def submit(self, op: str, args: dict) -> int:
        """Änderung am Cluster-State – wird immer vom Leader ausgeführt."""
        if self.is_leader:
            v = self.store.apply(op, args)
            asyncio.get_running_loop().create_task(self.broadcast_state())
            return v
        leader = self.current_leader()
        if not leader:
            raise NoLeader("Kein Leader verfügbar (Quorum fehlt?)")
        r = await self.call(leader, "POST", "/internal/op", {"op": op, "args": args}, timeout=10)
        if self.store.version < r["version"]:
            await self.fetch_state(leader)
        return r["version"]

    # ------------------------------------------------------------------ Events
    def _load_events(self):
        try:
            with open(self._events_path, encoding="utf-8") as f:
                lines = f.readlines()[-500:]
            for line in lines:
                ev = json.loads(line)
                self.events.append(ev)
                self._event_ids.add(ev["id"])
            with open(self._events_path, "w", encoding="utf-8") as f:
                f.writelines(lines)
        except (OSError, ValueError):
            pass

    def add_event(self, ev: dict) -> bool:
        if ev["id"] in self._event_ids:
            return False
        if len(self._event_ids) > 5000:
            self._event_ids = {e["id"] for e in self.events}
        self._event_ids.add(ev["id"])
        self.events.append(ev)
        try:
            with open(self._events_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev) + "\n")
        except OSError:
            pass
        return True

    def emit(self, kind: str, message: str, severity: str = "info", node: str | None = None,
             service: str | None = None, alert: bool = False):
        ev = {"id": secrets.token_hex(8), "ts": time.time(), "kind": kind, "severity": severity,
              "message": message, "node": node, "service": service, "origin": self.me}
        log.log(logging.WARNING if severity != "info" else logging.INFO, "[%s] %s", kind, message)
        self.add_event(ev)
        loop = asyncio.get_running_loop()
        loop.create_task(self._broadcast_event(ev))
        if alert:
            title = {"fence": "Node eingezäunt", "failover": "Failover", "failback": "Failback",
                     "node_down": "Node ausgefallen", "node_up": "Node wieder da"}.get(kind, kind.capitalize())
            loop.create_task(alerts.send(self.store.settings()["alerts"], f"{title}: {node or service or ''}",
                                         message, severity))

    async def _broadcast_event(self, ev: dict):
        async def push(name):
            try:
                await self.call(name, "POST", "/internal/event", ev, timeout=3)
            except (httpx.HTTPError, RemoteError):
                pass
        await asyncio.gather(*(push(n) for n in self.members() if n != self.me))
