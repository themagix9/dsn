"""Leader-Logik: Platzierung, Failover, Failback, Migration, DNS und Monitoring-Alarme.

Läuft auf allen Nodes, handelt aber nur, solange der Node Leader mit Quorum ist.

Ablauf einer geplanten Migration (manuell, Failback, Drain):
  1. Assignment -> phase=migrating   => Quell-Node stoppt den Stack (konsistente Daten auf Platte)
  2. Quell-Node synct final per rsync zum Ziel
  3. Assignment -> node=Ziel, phase=running => Ziel startet den Stack, DNS wird umgestellt
Schlägt Schritt 1 oder 2 fehl, wird zurückgerollt (Service läuft wieder auf der Quelle).

Failover (Node tot): Der Service startet sofort auf dem nächsten Node mit dem Datenstand des letzten
periodischen Syncs. Der tote Node hat sich zu diesem Zeitpunkt bereits selbst eingezäunt.
"""
from __future__ import annotations

import asyncio
import logging
import time

from . import alerts
from .cluster import Cluster
from .dns import CloudflareDNS, DNSError

log = logging.getLogger("dsn.scheduler")


def fmt_age(ts) -> str:
    if not ts:
        return "nie"
    s = int(time.time() - ts)
    return f"vor {s} s" if s < 120 else f"vor {s // 60} min" if s < 7200 else f"vor {s // 3600} h"


class Scheduler:
    def __init__(self, cluster: Cluster):
        self.cluster = cluster
        self.store = cluster.store
        self.migrations: dict[str, asyncio.Task] = {}
        self.dns = CloudflareDNS()
        self.dns_applied: dict[str, tuple[str, float]] = {}
        self.dns_errors: dict[str, str] = {}
        self.alert_active: dict[str, bool] = {}
        self.unprot_since: dict[str, float] = {}
        self._noplace_warned: dict[str, float] = {}

    @property
    def active(self) -> bool:
        return self.cluster.is_leader and self.cluster.has_quorum

    # ------------------------------------------------------------------ Platzierung
    @staticmethod
    def eligible(name: str, spec: dict, st: dict) -> bool:
        n = st["nodes"].get(name)
        if not n or n.get("role") != "worker":
            return False
        labels = n.get("labels") or {}
        return all(labels.get(k) == v for k, v in spec["placement"]["constraints"].items())

    def usable(self, name: str, spec: dict, st: dict) -> bool:
        return self.eligible(name, spec, st) and not st["nodes"][name].get("drain") \
            and self.cluster.down_for(name) == 0

    def choose(self, spec: dict, st: dict, exclude: set[str]) -> str | None:
        cands = [n for n in st["nodes"] if n not in exclude and self.usable(n, spec, st)]
        if not cands:
            return None
        for n in spec["placement"]["preferred"]:
            if n in cands:
                return n
        if spec["placement"]["preferred"] and not spec["placement"]["auto"]:
            return None
        load = {n: 0 for n in cands}
        for a in st["assignments"].values():
            if a.get("node") in load:
                load[a["node"]] += 1

        def score(n):
            m = self.cluster.node_status(n).get("metrics") or {}
            free_mem = 1 - m.get("mem_percent", 50) / 100
            return free_mem - m.get("cpu", 50) / 200 - 0.05 * load[n]
        return max(cands, key=score)

    # ------------------------------------------------------------------ Hauptschleife
    async def run(self):
        while True:
            await asyncio.sleep(3)
            if not self.active:
                continue
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                log.exception("Fehler im Scheduler")

    async def _assign(self, svc: str, fields: dict):
        await self.cluster.submit("assignment_set", {"service": svc, "fields": fields})

    async def tick(self):
        st = self.store.snapshot()
        t = st["settings"]["timeouts"]
        # Gerade gewählter Leader wartet einen Heartbeat-Zyklus, damit er alle Peers kennt
        if time.monotonic() - self.cluster.leader_since < t["heartbeat"] * 2:
            return
        for svc in list(st["assignments"]):
            if svc not in st["services"]:
                await self.cluster.submit("assignment_set", {"service": svc, "delete": True})

        for svc, spec in st["services"].items():
            if svc in self.migrations:
                continue
            a = st["assignments"].get(svc)
            if not spec["enabled"]:
                if a:
                    await self.cluster.submit("assignment_set", {"service": svc, "delete": True})
                    self.cluster.emit("service", f"{svc} deaktiviert", service=svc)
                continue

            if not a or a.get("node") not in st["nodes"]:
                node = self.choose(spec, st, set())
                if node:
                    await self._assign(svc, {"node": node, "phase": "running", "since": time.time(),
                                             "generation": (a or {}).get("generation", 0) + 1,
                                             "reason": "Erstplatzierung", "target": None})
                    self.cluster.emit("placement", f"{svc} wird auf {node} gestartet", service=svc, node=node)
                elif time.time() - self._noplace_warned.get(svc, 0) > 600:
                    self._noplace_warned[svc] = time.time()
                    self.cluster.emit("placement", f"Kein passender Node für {svc} verfügbar", "warning",
                                      service=svc, alert=True)
                continue

            node = a["node"]
            if a.get("phase") == "migrating":
                self._start_migration(svc, a.get("target") or node, a.get("reason", "Fortsetzung"), "resume")
                continue

            # 1) Manuelle Verschiebung angefordert
            target = a.get("requested_target")
            if target:
                await self._assign(svc, {"requested_target": None})
                if target != node and self.usable(target, spec, st):
                    self._start_migration(svc, target, "manuell verschoben", "move")
                elif target != node:
                    self.cluster.emit("move", f"{target} ist für {svc} nicht nutzbar (offline, drain oder "
                                              f"Constraints)", "warning", service=svc)
                continue

            # 2) Node ausgefallen -> Failover
            down = self.cluster.down_for(node)
            if down > t["failover"]:
                new = self.choose(spec, st, {node})
                if new:
                    await self._failover(svc, spec, node, new, f"{node} seit {int(down)} s nicht erreichbar")
                continue

            # 3) Drain / Constraints passen nicht mehr
            if st["nodes"][node].get("drain") or not self.eligible(node, spec, st):
                new = self.choose(spec, st, {node})
                if new:
                    reason = "Drain" if st["nodes"][node].get("drain") else "Constraints geändert"
                    self._start_migration(svc, new, reason, "move")
                continue

            # 4) Service startet auf dem Node nicht
            rt = (self.cluster.node_status(node).get("stacks") or {}).get(svc) or {}
            if rt.get("failing_since") and time.time() - rt["failing_since"] > t["service_fail"]:
                new = self.choose(spec, st, {node})
                if new:
                    self.cluster.emit("service_fail", f"{svc} läuft auf {node} seit "
                                      f"{int(time.time() - rt['failing_since'])} s nicht ({rt.get('status')}) "
                                      f"– verschiebe auf {new}", "warning", service=svc, node=node, alert=True)
                    if down > 0:
                        await self._failover(svc, spec, node, new, "Service-Fehler")
                    else:
                        self._start_migration(svc, new, "Service-Fehler", "move")
                continue

            # 5) Automatischer Failback auf bevorzugten Node
            failback = spec["failback"] if spec["failback"] is not None else st["settings"]["failback_enabled"]
            pref = spec["placement"]["preferred"]
            if failback and pref:
                best = next((n for n in pref if self.usable(n, spec, st)), None)
                better = best and best != node and (node not in pref or pref.index(best) < pref.index(node))
                if better and self.cluster.stable_for(best) >= t["failback_delay"]:
                    stable = fmt_age(time.time() - self.cluster.stable_for(best)).replace("vor ", "")
                    self._start_migration(svc, best, f"Failback, {best} seit {stable} stabil", "failback")

    async def _failover(self, svc: str, spec: dict, old: str, new: str, reason: str):
        a = self.store.data["assignments"].get(svc, {})
        sync_info = ""
        if spec["sync"]["mode"] != "none":
            last = ((self.cluster.node_status(old).get("syncs") or {}).get(svc) or {}).get("last_ok")
            sync_info = f"\nDatenstand: letzter erfolgreicher Sync {fmt_age(last)}"
        await self._assign(svc, {"node": new, "phase": "running", "since": time.time(), "previous": old,
                                 "generation": a.get("generation", 0) + 1, "reason": f"Failover: {reason}",
                                 "target": None})
        self.cluster.emit("failover", f"{svc}: {old} → {new} ({reason}){sync_info}", "critical",
                          service=svc, node=new, alert=True)

    # ------------------------------------------------------------------ Migration
    def _start_migration(self, svc: str, target: str, reason: str, kind: str):
        if svc in self.migrations:
            return
        task = asyncio.get_running_loop().create_task(self._migrate(svc, target, reason, kind))
        self.migrations[svc] = task
        task.add_done_callback(lambda _: self.migrations.pop(svc, None))

    async def _wait_stopped(self, source: str, svc: str, min_version: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.active:
                return False
            status = self.cluster.node_status(source)
            if self.cluster.down_for(source) > 0:
                return True  # Quelle weg -> stoppt sich per Fencing selbst
            if status.get("version", 0) >= min_version:
                rt = (status.get("stacks") or {}).get(svc)
                if not rt or rt["status"] in ("missing", "stopped"):
                    return True
            await asyncio.sleep(2)
        return False

    async def _migrate(self, svc: str, target: str, reason: str, kind: str):
        a = self.store.data["assignments"].get(svc, {})
        spec = self.store.data["services"].get(svc)
        source = a.get("node")
        if not spec or not source:
            return
        try:
            if a.get("phase") != "migrating":
                v = await self.cluster.submit("assignment_set", {"service": svc, "fields": {
                    "phase": "migrating", "target": target, "reason": reason}})
            else:
                v = self.store.version
            self.cluster.emit("migration", f"{svc}: {source} → {target} gestartet ({reason})", service=svc)

            if source != target and self.cluster.down_for(source) == 0:
                if not await self._wait_stopped(source, svc, v, 180):
                    raise RuntimeError(f"{source} hat {svc} nicht rechtzeitig gestoppt")
                if spec["sync"]["mode"] != "none" and self.cluster.down_for(source) == 0:
                    r = await self.cluster.call(source, "POST", "/internal/sync",
                                                {"service": svc, "targets": [target], "dump": False},
                                                timeout=3600)
                    if not r.get("ok"):
                        raise RuntimeError(f"Finaler Sync fehlgeschlagen: {r.get('error')}")

            await self._assign(svc, {"node": target, "phase": "running", "since": time.time(),
                                     "previous": source, "generation": a.get("generation", 0) + 1,
                                     "reason": reason, "target": None})
            sev = "warning" if kind == "failback" else "info"
            self.cluster.emit(kind if kind != "resume" else "migration",
                              f"{svc}: {source} → {target} abgeschlossen ({reason})", sev, service=svc,
                              node=target, alert=kind in ("failback", "move"))
        except Exception as e:  # noqa: BLE001
            log.exception("Migration %s fehlgeschlagen", svc)
            self.cluster.emit("migration", f"{svc}: Migration {source} → {target} fehlgeschlagen: {e} – "
                                           f"läuft weiter auf {source}", "warning", service=svc, alert=True)
            try:
                await self._assign(svc, {"node": source, "phase": "running", "target": None})
            except Exception:  # noqa: BLE001
                log.exception("Rollback von %s fehlgeschlagen", svc)

    # ------------------------------------------------------------------ DNS
    async def run_dns(self):
        while True:
            await asyncio.sleep(10)
            if not self.active:
                continue
            st = self.store.data
            dns_cfg = st["settings"]["dns"]
            for svc, spec in list(st["services"].items()):
                a = st["assignments"].get(svc) or {}
                if not spec["dns"] or a.get("phase") != "running":
                    continue
                ip = (st["nodes"].get(a["node"]) or {}).get("public_ip")
                if not ip:
                    self.dns_errors[svc] = f"Node {a['node']} hat keine public_ip"
                    continue
                for rec in spec["dns"]:
                    fqdn = rec["name"]
                    last = self.dns_applied.get(fqdn)
                    if last and last[0] == ip and time.time() - last[1] < 600:
                        continue
                    try:
                        changed = await self.dns.ensure_a(dns_cfg["api_token"], fqdn, ip, int(dns_cfg["ttl"]),
                                                          rec["proxied"])
                        self.dns_applied[fqdn] = (ip, time.time())
                        self.dns_errors.pop(svc, None)
                        if changed:
                            self.cluster.emit("dns", f"DNS {fqdn} → {ip} ({a['node']})", service=svc)
                    except (DNSError, Exception) as e:  # noqa: BLE001
                        msg = f"DNS-Update {fqdn} fehlgeschlagen: {e}"
                        if self.dns_errors.get(svc) != msg:
                            self.cluster.emit("dns", msg, "warning", service=svc, alert=True)
                        self.dns_errors[svc] = msg

    # ------------------------------------------------------------------ Monitoring-Alarme
    def _alert(self, key: str, active: bool, title: str, msg: str, severity: str, node=None, service=None):
        was = self.alert_active.get(key, False)
        if active and not was:
            self.cluster.emit("alert", f"{title}: {msg}", severity, node=node, service=service)
            asyncio.get_running_loop().create_task(
                alerts.send(self.store.settings()["alerts"], title, msg, severity))
        elif was and not active:
            self.cluster.emit("alert", f"Behoben – {title}", "info", node=node, service=service)
            asyncio.get_running_loop().create_task(
                alerts.send(self.store.settings()["alerts"], f"Behoben: {title}", msg, "info"))
        self.alert_active[key] = active

    async def run_monitor(self):
        while True:
            await asyncio.sleep(15)
            if not self.active or time.monotonic() - self.cluster.leader_since < 30:
                continue
            try:
                self.check_alerts()
            except Exception:  # noqa: BLE001
                log.exception("Fehler im Monitoring")

    def check_alerts(self):
        st = self.store.data
        thr = st["settings"]["thresholds"]
        t = st["settings"]["timeouts"]
        for name in st["nodes"]:
            down = self.cluster.down_for(name)
            self._alert(f"down:{name}", down > max(t["node_timeout"] * 3, 30), f"Node {name} nicht erreichbar",
                        f"{name} sendet seit {int(down)} s keine Heartbeats.", "critical", node=name)
            if down > 0:
                continue
            s = self.cluster.node_status(name)
            m = s.get("metrics") or {}
            for d in m.get("disks", []):
                pct = d.get("percent", 0)
                self._alert(f"disk:{name}:{d['name']}", pct >= thr["disk"], f"{name}: Disk {d['name']} fast voll",
                            f"{d['name']} ({d['path']}) ist zu {pct:.0f} % belegt.", "warning", node=name)
            self._alert(f"mem:{name}", m.get("mem_percent", 0) >= thr["mem"], f"{name}: RAM knapp",
                        f"RAM-Auslastung {m.get('mem_percent', 0):.0f} %.", "warning", node=name)
            for chk in m.get("hostchecks", []):
                bad = chk.get("status") not in ("ok", None)
                self._alert(f"check:{name}:{chk.get('name')}", bad, f"{name}: {chk.get('name')} {chk.get('status')}",
                            chk.get("summary", ""), "critical" if chk.get("status") == "critical" else "warning",
                            node=name)
            p = self.cluster.peers.get(name)
            if p and p.rtt_ms is not None:
                self._alert(f"rtt:{name}", p.rtt_ms > thr["rtt_ms"], f"Hohe Latenz zu {name}",
                            f"RTT {p.rtt_ms:.0f} ms (Schwelle {thr['rtt_ms']} ms).", "warning", node=name)
            # Server als "vollständig geschützt" markiert -> neue, ungeschützte Container melden
            current = set(s.get("unprotected") or []) if st["nodes"][name].get("protect_all") else set()
            prefix = f"unprot:{name}:"
            for key in [k for k, v in self.alert_active.items() if k.startswith(prefix) and v]:
                if key[len(prefix):] not in current:
                    self._alert(key, False, f"{name}: Container {key[len(prefix):]} nicht HA-geschützt", "", "info",
                                node=name)
            for key in [k for k in self.unprot_since if k.startswith(prefix) and k[len(prefix):] not in current]:
                self.unprot_since.pop(key)
            for cname in current:
                # erst nach 2 min melden – das Inventar der Agents ist bis zu 60 s alt (z.B. direkt nach Übernahme)
                if time.time() - self.unprot_since.setdefault(prefix + cname, time.time()) < 120:
                    continue
                self._alert(prefix + cname, True, f"{name}: Container {cname} ist nicht HA-geschützt",
                            f"Auf {name} läuft der neue Container „{cname}“ außerhalb von DSN. Bei einem Ausfall "
                            f"wird er nicht verschoben. Im Dashboard unter Docker → In HA aufnehmen.",
                            "warning", node=name)
            for svc, sy in (s.get("syncs") or {}).items():
                self._alert(f"sync:{svc}", sy.get("fail_count", 0) >= 3, f"Sync von {svc} schlägt fehl",
                            f"{sy.get('fail_count')} Fehlversuche in Folge auf {name}: {sy.get('error', '')}",
                            "warning", service=svc)
