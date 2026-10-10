"""Pairing-Modus: DSN läuft ohne Konfiguration und wartet darauf, über das Dashboard aufgenommen zu werden.

Ablauf:
  1. Server: `docker compose up -d` -> keine Config -> Pairing-Modus, Token im Log (`docker logs dsn`)
  2. Dashboard: Token einfügen -> Cluster ruft /pair/info ab (Vorschau) und schickt per /pair die Config
  3. Server schreibt die Config, beendet sich, Docker startet ihn neu -> normaler Betrieb, tritt dem Cluster bei

Sicherheit: Der Token wird bewusst NUR im Log ausgegeben, nie auf einer Webseite. Wer ihn kennt, könnte den
Server in einen fremden Cluster holen (DSN hat Zugriff auf docker.sock = root auf dem Host).
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import time

import psutil
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from . import __version__
from .config import config_candidates, default_data_dir

log = logging.getLogger("dsn.pairing")
TOKEN_PREFIX = "dsn1_"
WIN_VOL = "/var/lib/docker/volumes/dsn-data/_data"


def encode_token(data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return TOKEN_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_token(token: str) -> dict:
    token = re.sub(r"\s+", "", token or "")
    if not token.startswith(TOKEN_PREFIX):
        raise ValueError("Kein DSN-Token (muss mit dsn1_ beginnen)")
    body = token[len(TOKEN_PREFIX):]
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, json.JSONDecodeError):
        raise ValueError("Token ist beschädigt – bitte vollständig kopieren")
    if not data.get("k"):
        raise ValueError("Token unvollständig")
    return data


def local_addresses() -> list[str]:
    """IPv4-Adressen des Hosts, sinnvoll sortiert: Tailscale, privat (LAN), öffentlich. Docker-Bridges raus."""
    ts, private, public = [], [], []
    for ifname, addrs in psutil.net_if_addrs().items():
        if ifname == "lo" or ifname.startswith(("docker", "br-", "veth", "virbr")):
            continue
        for a in addrs:
            if a.family != socket.AF_INET:
                continue
            ip = ipaddress.ip_address(a.address)
            if ip.is_loopback or ip.is_link_local:
                continue
            if ip in ipaddress.ip_network("100.64.0.0/10"):
                ts.append(a.address)
            elif ip.is_private:
                private.append(a.address)
            else:
                public.append(a.address)
    return ts + private + public


def _os_name() -> str:
    for path in ("/host/etc/os-release", "/etc/os-release"):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if line.startswith("PRETTY_NAME="):
                        return line.split("=", 1)[1].strip().strip('"')
        except OSError:
            continue
    return "unbekannt"


class Pairing:
    def __init__(self):
        self.data_dir = default_data_dir()
        self.platform = os.environ.get("DSN_PLATFORM", "linux")
        self.port = int(os.environ.get("DSN_PORT", "7946"))
        self.target = config_candidates()[-1]
        self.state_file = os.path.join(self.data_dir, "pairing.json")
        os.makedirs(self.data_dir, exist_ok=True)
        try:
            with open(self.state_file, encoding="utf-8") as f:
                self.otp = json.load(f)["otp"]
        except (OSError, ValueError, KeyError):
            self.otp = secrets.token_urlsafe(18)
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump({"otp": self.otp, "created": time.time()}, f)
            os.chmod(self.state_file, 0o600)
        self.fails = 0
        self.used = False  # nach erfolgreichem Pairing sofort gesperrt (nicht erst nach dem Neustart)

    @property
    def hostname(self) -> str:
        name = os.environ.get("DSN_HOSTNAME") or socket.gethostname()
        return re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")[:41] or "server"

    def token(self) -> str:
        return encode_token({"a": local_addresses() if self.platform == "linux" else [],
                             "p": self.port, "k": self.otp, "h": self.hostname, "o": self.platform})

    def banner(self) -> str:
        line = "=" * 78
        hint = ("" if self.platform == "linux" else
                "\n  Windows: Im Dashboard die IP dieses PCs (LAN/Tailscale) als Adresse eintragen.")
        return (f"\n{line}\n  DSN wartet auf Aufnahme in einen Cluster (Pairing-Modus)\n\n"
                f"  Diesen Token im Dashboard unter  Nodes -> Server hinzufügen  einfügen:\n\n"
                f"  {self.token()}\n\n"
                f"  Gefundene Adressen: {', '.join(local_addresses()) or '-'} (Port {self.port}){hint}\n{line}\n")

    def check(self, otp: str | None) -> bool:
        if self.used or self.fails > 20:
            return False  # nach vielen Fehlversuchen nur noch per Neustart
        ok = bool(otp) and hmac.compare_digest(otp, self.otp)
        if not ok:
            self.fails += 1
        return ok

    async def info(self) -> dict:
        proc = await asyncio.create_subprocess_exec(
            "docker", "info", "--format", "{{json .}}", stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        docker = {}
        try:
            d = json.loads(out)
            docker = {"version": d.get("ServerVersion"), "containers": d.get("Containers"),
                      "running": d.get("ContainersRunning"), "images": d.get("Images"),
                      "os": d.get("OperatingSystem")}
        except ValueError:
            docker = {"error": out.decode(errors="replace")[:300]}
        vm = psutil.virtual_memory()
        return {"hostname": self.hostname, "platform": self.platform, "os": _os_name(),
                "addresses": local_addresses(), "port": self.port, "dsn_version": __version__,
                "cpus": psutil.cpu_count(), "mem_total": vm.total, "docker": docker}

    def apply(self, cfg: dict) -> str:
        """Config vom Cluster übernehmen (plattformspezifische Pfade ergänzen) und speichern."""
        self.used = True
        cfg = dict(cfg)
        cfg.setdefault("port", self.port)
        cfg.setdefault("bind", "0.0.0.0")
        cfg["node"]["platform"] = self.platform
        if self.platform == "windows":
            cfg.update({"rsync_bind": "0.0.0.0", "data_dir": f"{WIN_VOL}/lib", "volumes_dir": f"{WIN_VOL}/volumes",
                        "host_root": "/host", "disks": [{"name": "Docker-Volumes", "path": "/var/lib/docker/volumes"}]})
        else:
            cfg.setdefault("disks", [{"name": "root", "path": "/"}])
        os.makedirs(os.path.dirname(self.target), exist_ok=True)
        tmp = self.target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.target)
        try:
            os.remove(self.state_file)  # Token ist verbraucht
        except OSError:
            pass
        return self.target


def run_pairing():
    import uvicorn

    p = Pairing()
    app = FastAPI(docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    async def page():
        addr = (local_addresses() or [""])[0]
        return f"""<!doctype html><html lang="de"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>DSN – Pairing</title>
<style>body{{font-family:system-ui;max-width:640px;margin:50px auto;padding:0 16px;background:#0d0d0d;color:#eee}}
input,button{{font:inherit;padding:8px 10px;border-radius:8px;border:1px solid #444;background:#1a1a19;color:#eee;width:100%;
box-sizing:border-box;margin:4px 0 10px}}button{{background:#3987e5;border:0;cursor:pointer;width:auto}}
code{{background:#232321;padding:2px 6px;border-radius:4px}}.box{{background:#1a1a19;padding:16px;border-radius:10px;margin:18px 0}}
.err{{color:#e66767}}.ok{{color:#0ca30c}}</style>
<h1>📡 DSN wartet auf Aufnahme</h1>
<p>Dieser Server (<b>{p.hostname}</b>) ist noch keinem Cluster zugeordnet. Den Token zeigt <code>docker logs dsn</code> auf dem Server.
Er wird aus Sicherheitsgründen hier nicht angezeigt.</p>
<div class="box"><b>Variante A: Einem bestehenden Cluster beitreten</b>
<p>Im Dashboard des Clusters: <b>Nodes → Server hinzufügen → Per Token</b> und den Token dort einfügen.</p></div>
<div class="box"><b>Variante B: Neuen Cluster gründen</b> (dies ist der erste Server)
<form id="f"><label>Token aus dem Log<input name="token" required placeholder="dsn1_…"></label>
<label>Name dieses Servers<input name="name" value="{p.hostname}" required pattern="[a-z0-9][a-z0-9-]{{0,40}}"></label>
<label>Adresse, unter der andere Server diesen erreichen (LAN- oder Tailscale-IP)<input name="address" value="{addr}" required></label>
<button>Cluster gründen</button></form><p id="msg"></p></div>
<script>
document.getElementById('f').onsubmit = async ev => {{
  ev.preventDefault(); const f = new FormData(ev.target), msg = document.getElementById('msg');
  msg.className = ''; msg.textContent = 'Gründe Cluster …';
  let tok = f.get('token').trim(), otp = '';
  try {{ const b = tok.replace(/^dsn1_/, '').replace(/-/g, '+').replace(/_/g, '/'); otp = JSON.parse(atob(b + '='.repeat((4 - b.length % 4) % 4))).k; }}
  catch (e) {{ msg.className = 'err'; msg.textContent = 'Token ungültig – bitte vollständig kopieren.'; return; }}
  const r = await fetch('/pair/bootstrap', {{method: 'POST', headers: {{'Content-Type': 'application/json', 'X-DSN-Pair': otp}},
    body: JSON.stringify({{name: f.get('name'), address: f.get('address')}})}});
  const d = await r.json();
  if (!r.ok) {{ msg.className = 'err'; msg.textContent = d.detail || 'Fehler'; return; }}
  msg.className = 'ok';
  msg.innerHTML = 'Cluster gegründet! <b>Cluster-Secret (jetzt sicher speichern, z.B. im Passwortmanager):</b><br><code>' + d.secret +
    '</code><br><br>Du brauchst es gleich, um das Admin-Konto anzulegen. Der Server startet neu …';
  setTimeout(() => location.reload(), 12000);
}};
</script></html>"""

    @app.post("/pair/bootstrap")
    async def bootstrap(request: Request, body: dict):
        """Ersten Node eines neuen Clusters einrichten (nur mit Token aus dem Log)."""
        from .state import NAME_RE
        if not p.check(request.headers.get("x-dsn-pair")):
            raise HTTPException(403, "Token ungültig oder abgelaufen")
        name, address = (body.get("name") or "").strip(), (body.get("address") or "").strip()
        if not NAME_RE.match(name) or not address:
            raise HTTPException(400, "Name (a-z, 0-9, -) und Adresse angeben")
        secret = secrets.token_urlsafe(32)
        p.apply({"node": {"name": name, "address": address, "role": "worker", "leader_priority": 10, "labels": {}},
                 "cluster": {"secret": secret, "seeds": []}})
        log.info("Neuer Cluster gegründet – starte neu")
        asyncio.get_running_loop().call_later(1.5, os._exit, 0)
        return {"ok": True, "secret": secret}

    @app.get("/api/setup")
    async def setup_state():
        return {"needed": False, "pairing": True, "node": p.hostname}

    @app.get("/pair/info")
    async def info(request: Request):
        if not p.check(request.headers.get("x-dsn-pair")):
            raise HTTPException(403, "Token ungültig oder abgelaufen")
        return await p.info()

    @app.post("/pair")
    async def pair(request: Request, body: dict):
        if not p.check(request.headers.get("x-dsn-pair")):
            raise HTTPException(403, "Token ungültig oder abgelaufen")
        try:
            assert body["node"]["name"] and body["node"]["address"] and len(body["cluster"]["secret"]) >= 24
        except (KeyError, TypeError, AssertionError):
            raise HTTPException(400, "Unvollständige Konfiguration")
        target = p.apply(body)
        log.info("Pairing erfolgreich – Konfiguration in %s gespeichert, starte neu", target)
        # kurz warten, damit die Antwort rausgeht; Docker startet den Container neu (restart: unless-stopped)
        asyncio.get_running_loop().call_later(1.0, os._exit, 0)
        return {"ok": True}

    async def main():
        server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=p.port, log_level="warning"))

        async def remind():
            while True:
                log.warning(p.banner())
                await asyncio.sleep(600)
        await asyncio.gather(server.serve(), remind())

    asyncio.run(main())
