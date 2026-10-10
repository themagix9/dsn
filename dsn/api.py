"""HTTP-API: interne Cluster-Endpunkte (/internal/*, Bearer-Secret) und Dashboard-API (/api/*, Login)."""
from __future__ import annotations

import asyncio
import hmac
import io
import json
import os
import tarfile
import time
from dataclasses import dataclass

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, alerts, auth
from .adopt import Adopter, AdoptError
from .manage import READ_OPS, WRITE_OPS, Manager, ManageError
from .agent import Agent
from .cluster import Cluster, NoLeader, RemoteError
from .config import Config
from .docker_ops import DockerOps
from .scheduler import Scheduler
from .state import OpError, StateStore, mask_secrets, strip_masked

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
SRC_DIR = os.environ.get("DSN_SRC", "/app/src")


@dataclass
class Ctx:
    cfg: Config
    store: StateStore
    cluster: Cluster
    agent: Agent
    scheduler: Scheduler
    docker: DockerOps
    adopter: Adopter
    manager: Manager


def create_app(ctx: Ctx) -> FastAPI:
    app = FastAPI(title="Deep Space Network", version=__version__, docs_url=None, redoc_url=None)
    cfg, store, cluster, agent, sched = ctx.cfg, ctx.store, ctx.cluster, ctx.agent, ctx.scheduler
    sessions = auth.Sessions(cfg.secret)
    limiter = auth.RateLimiter()
    pending_totp: dict[str, str] = {}

    # ================================================================== Auth-Helfer
    def internal(request: Request):
        h = request.headers.get("authorization", "")
        if not hmac.compare_digest(h, f"Bearer {cfg.secret}"):
            raise HTTPException(401, "Ungültiges Cluster-Secret")

    def current_user(request: Request) -> dict:
        name = sessions.read(request.cookies.get(auth.SESSION_COOKIE))
        user = store.data["users"].get(name) if name else None
        if not user:
            raise HTTPException(401, "Nicht angemeldet")
        return {"username": name, "role": user.get("role", "viewer"), "totp": bool(user.get("totp_secret"))}

    def admin(user: dict = Depends(current_user)) -> dict:
        if user["role"] != "admin":
            raise HTTPException(403, "Nur für Admins")
        return user

    async def submit(op: str, args: dict) -> int:
        try:
            return await cluster.submit(op, args)
        except OpError as e:
            raise HTTPException(400, str(e))
        except NoLeader as e:
            raise HTTPException(503, str(e))
        except RemoteError as e:
            raise HTTPException(400, str(e))

    # ================================================================== Interne Endpunkte
    @app.post("/internal/heartbeat", dependencies=[Depends(internal)])
    async def i_heartbeat(body: dict):
        cluster.record_status(body)
        return cluster.my_status()

    @app.get("/internal/state", dependencies=[Depends(internal)])
    async def i_state():
        return store.snapshot()

    @app.post("/internal/state", dependencies=[Depends(internal)])
    async def i_push_state(body: dict):
        return {"accepted": store.replace_if_newer(body)}

    @app.post("/internal/vote", dependencies=[Depends(internal)])
    async def i_vote(body: dict):
        granted, term = cluster.grant_vote(body["candidate"], int(body["term"]), int(body["version"]))
        return {"granted": granted, "term": term}

    @app.post("/internal/op", dependencies=[Depends(internal)])
    async def i_op(body: dict):
        if not cluster.is_leader:
            raise HTTPException(409, "Nicht Leader")
        try:
            v = store.apply(body["op"], body["args"])
        except OpError as e:
            raise HTTPException(400, str(e))
        asyncio.get_running_loop().create_task(cluster.broadcast_state())
        return {"version": v}

    @app.post("/internal/submit", dependencies=[Depends(internal)])
    async def i_submit(body: dict):
        """Für CLI-Befehle: Operation über den Leader ausführen (egal auf welchem Node aufgerufen)."""
        return {"version": await submit(body["op"], body["args"])}

    @app.post("/internal/join", dependencies=[Depends(internal)])
    async def i_join(body: dict):
        if body.get("name") in store.data["nodes"] and store.data["nodes"][body["name"]]["address"] != body.get("address"):
            existing = body["name"]
            if cluster.down_for(existing) == 0:
                raise HTTPException(409, f"Node-Name {existing} ist bereits mit anderer Adresse aktiv")
        v = await submit("node_upsert", body)
        cluster.emit("join", f"Node {body.get('name')} ({body.get('address')}) ist dem Cluster beigetreten",
                     node=body.get("name"), alert=True)
        return {"version": v}

    @app.post("/internal/event", dependencies=[Depends(internal)])
    async def i_event(body: dict):
        cluster.add_event(body)
        return {"ok": True}

    @app.post("/internal/sync", dependencies=[Depends(internal)])
    async def i_sync(body: dict):
        try:
            return await agent.sync_service(body["service"], body["targets"], dump=body.get("dump", True))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/internal/accept_sync", dependencies=[Depends(internal)])
    async def i_accept_sync(service: str):
        if not agent.accepts_sync(service):
            raise HTTPException(409, f"{service} läuft auf diesem Node – Sync abgelehnt")
        return {"ok": True}

    @app.get("/internal/logs", dependencies=[Depends(internal)])
    async def i_logs(service: str, tail: int = 200):
        return {"logs": await ctx.docker.logs(service, min(tail, 2000))}

    @app.post("/internal/stack_action", dependencies=[Depends(internal)])
    async def i_stack_action(body: dict):
        svc, action = body["service"], body["action"]
        if svc not in agent.desired():
            raise HTTPException(400, f"{svc} läuft nicht auf diesem Node")
        if action == "restart":
            ok, out = await ctx.docker.restart(svc)
        elif action == "pull":
            ok, out = await ctx.docker.up(store.data["services"][svc], pull=True)
        else:
            raise HTTPException(400, "Unbekannte Aktion")
        return {"ok": ok, "output": out}

    @app.get("/internal/history", dependencies=[Depends(internal)])
    async def i_history(node: str):
        return list(cluster.history.get(node, []))

    # ------------------------------------------------------------------ Container-Übernahme (lokal)
    @app.get("/internal/inventory", dependencies=[Depends(internal)])
    async def i_inventory():
        try:
            return await ctx.adopter.inventory()
        except AdoptError as e:
            raise HTTPException(500, str(e))

    @app.post("/internal/adopt/plan", dependencies=[Depends(internal)])
    async def i_adopt_plan(body: dict):
        try:
            return await ctx.adopter.plan(body["kind"], body["ref"], body.get("select"))
        except AdoptError as e:
            raise HTTPException(400, str(e))

    @app.post("/internal/adopt/apply", dependencies=[Depends(internal)])
    async def i_adopt_apply(body: dict):
        try:
            return await ctx.adopter.apply(body["service"], body["containers"], body["mounts"])
        except AdoptError as e:
            raise HTTPException(400, str(e))

    @app.post("/internal/adopt/restore", dependencies=[Depends(internal)])
    async def i_adopt_restore(body: dict):
        return await ctx.adopter.restore(body["containers"])

    # ------------------------------------------------------------------ Docker-Verwaltung (lokal)
    @app.post("/internal/self_update", dependencies=[Depends(internal)])
    async def i_self_update():
        try:
            return await ctx.manager.self_update()
        except ManageError as e:
            raise HTTPException(400, str(e))

    @app.post("/internal/docker", dependencies=[Depends(internal)])
    async def i_docker(body: dict):
        try:
            return await ctx.manager.handle(body["op"], body.get("args") or {})
        except (ManageError, TypeError) as e:
            raise HTTPException(400, str(e))

    @app.websocket("/internal/console")
    async def i_console(websocket: WebSocket, container: str):
        if not hmac.compare_digest(websocket.headers.get("authorization", ""), f"Bearer {cfg.secret}"):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        await run_console_local(websocket, container)

    async def run_console_local(websocket: WebSocket, container: str):
        async def send(data: bytes):
            await websocket.send_bytes(data)

        async def receive():
            try:
                return json.loads(await websocket.receive_text())
            except (WebSocketDisconnect, ValueError, RuntimeError):
                return None
        try:
            await ctx.manager.console(container, send, receive)
        except ManageError as e:
            await websocket.send_bytes(f"\r\n\x1b[31m{e}\x1b[0m\r\n".encode())
        except (WebSocketDisconnect, RuntimeError):
            pass
        try:
            await websocket.close()
        except RuntimeError:
            pass

    # ================================================================== Installation neuer Nodes
    @app.get("/install.sh")
    async def install_script():
        return FileResponse(os.path.join(SRC_DIR, "install.sh"), media_type="text/x-shellscript")

    @app.get("/install.ps1")
    async def install_script_windows():
        return FileResponse(os.path.join(SRC_DIR, "install.ps1"), media_type="text/plain; charset=utf-8")

    @app.get("/dist/dsn.tar.gz", dependencies=[Depends(internal)])
    async def dist():
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(SRC_DIR, arcname="dsn", filter=lambda ti: None if "__pycache__" in ti.name else ti)
        return Response(buf.getvalue(), media_type="application/gzip")

    # ================================================================== Dashboard: Login
    @app.get("/api/setup")
    async def setup_needed():
        return {"needed": not store.data["users"], "node": cluster.me}

    @app.post("/api/setup")
    async def setup(body: dict, request: Request):
        ip = request.client.host if request.client else "?"
        if limiter.blocked(ip):
            raise HTTPException(429, "Zu viele Versuche")
        if store.data["users"]:
            raise HTTPException(400, "Setup bereits erledigt")
        if not hmac.compare_digest(body.get("token", ""), cfg.secret):
            limiter.fail(ip)
            raise HTTPException(403, "Cluster-Secret falsch (steht in /etc/dsn/config.yml)")
        try:
            h = auth.hash_password(body.get("password", ""))
        except ValueError as e:
            raise HTTPException(400, str(e))
        await submit("user_upsert", {"username": body["username"], "password_hash": h, "role": "admin"})
        return {"ok": True}

    @app.post("/api/login")
    async def login(body: dict, request: Request, response: Response):
        ip = request.client.host if request.client else "?"
        if limiter.blocked(ip):
            raise HTTPException(429, "Zu viele Fehlversuche – bitte 10 Minuten warten")
        user = store.data["users"].get(body.get("username", ""))
        if not user or not auth.check_password(body.get("password", ""), user["password_hash"]):
            limiter.fail(ip)
            raise HTTPException(401, "Benutzername oder Passwort falsch")
        if user.get("totp_secret"):
            if not body.get("totp"):
                raise HTTPException(401, "TOTP_REQUIRED")
            if not auth.check_totp(user["totp_secret"], body["totp"]):
                limiter.fail(ip)
                raise HTTPException(401, "2FA-Code falsch")
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        response.set_cookie(auth.SESSION_COOKIE, sessions.issue(body["username"]), max_age=auth.SESSION_MAX_AGE,
                            httponly=True, samesite="strict", secure=secure)
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(response: Response):
        response.delete_cookie(auth.SESSION_COOKIE)
        return {"ok": True}

    @app.get("/api/me")
    async def me(user: dict = Depends(current_user)):
        return user

    @app.post("/api/me/totp/begin")
    async def totp_begin(user: dict = Depends(current_user)):
        secret = auth.new_totp_secret()
        pending_totp[user["username"]] = secret
        return {"secret": secret, "uri": auth.totp_uri(secret, user["username"])}

    @app.post("/api/me/totp/enable")
    async def totp_enable(body: dict, user: dict = Depends(current_user)):
        secret = pending_totp.get(user["username"])
        if not secret or not auth.check_totp(secret, body.get("code", "")):
            raise HTTPException(400, "Code passt nicht – Uhrzeit auf dem Handy prüfen")
        await submit("user_upsert", {"username": user["username"], "totp_secret": secret})
        pending_totp.pop(user["username"], None)
        return {"ok": True}

    @app.post("/api/me/password")
    async def change_password(body: dict, user: dict = Depends(current_user)):
        u = store.data["users"][user["username"]]
        if not auth.check_password(body.get("old", ""), u["password_hash"]):
            raise HTTPException(400, "Altes Passwort falsch")
        try:
            h = auth.hash_password(body.get("new", ""))
        except ValueError as e:
            raise HTTPException(400, str(e))
        await submit("user_upsert", {"username": user["username"], "password_hash": h})
        return {"ok": True}

    # ================================================================== Dashboard: Benutzer
    @app.get("/api/users")
    async def users(_: dict = Depends(admin)):
        return [{"username": n, "role": u.get("role"), "totp": bool(u.get("totp_secret")), "created": u.get("created")}
                for n, u in store.data["users"].items()]

    @app.post("/api/users")
    async def user_create(body: dict, _: dict = Depends(admin)):
        if body.get("role") not in ("admin", "viewer"):
            raise HTTPException(400, "Rolle muss admin oder viewer sein")
        args = {"username": body["username"], "role": body["role"]}
        if body.get("password"):
            try:
                args["password_hash"] = auth.hash_password(body["password"])
            except ValueError as e:
                raise HTTPException(400, str(e))
        elif body["username"] not in store.data["users"]:
            raise HTTPException(400, "Passwort fehlt")
        if body.get("reset_totp"):
            args["totp_secret"] = ""
        await submit("user_upsert", args)
        return {"ok": True}

    @app.delete("/api/users/{username}")
    async def user_delete(username: str, _: dict = Depends(admin)):
        await submit("user_remove", {"username": username})
        return {"ok": True}

    # ================================================================== Dashboard: Übersicht
    def overview() -> dict:
        st = store.data
        leader = cluster.current_leader()
        alive = cluster.alive_nodes()
        nodes = []
        for name, n in st["nodes"].items():
            s = cluster.node_status(name)
            p = cluster.peers.get(name)
            nodes.append({
                "name": name, "role": n.get("role"), "platform": n.get("platform", "linux"),
                "address": n.get("address"), "public_ip": n.get("public_ip"),
                "labels": n.get("labels", {}), "drain": n.get("drain", False),
                "leader_priority": n.get("leader_priority", 100), "version": n.get("version"),
                "online": name in alive, "down_for": round(cluster.down_for(name)),
                "stable_for": round(cluster.stable_for(name)),
                "rtt_ms": 0 if name == cluster.me else (round(p.rtt_ms, 1) if p and p.rtt_ms is not None else None),
                "is_leader": name == leader, "is_me": name == cluster.me,
                "has_quorum": s.get("has_quorum"), "fenced": s.get("fenced"),
                "metrics": s.get("metrics") or {}, "error": p.error if p else "",
                "services": [svc for svc, a in st["assignments"].items() if a.get("node") == name],
                "unprotected": s.get("unprotected") or [], "protect_all": n.get("protect_all", False),
                "tailscale": s.get("tailscale") or {}, "build": (s.get("build") or ""),
            })
        services = []
        for svc, spec in st["services"].items():
            a = st["assignments"].get(svc) or {}
            node = a.get("node")
            ns = cluster.node_status(node) if node else {}
            rt = (ns.get("stacks") or {}).get(svc) or {}
            sync = (ns.get("syncs") or {}).get(svc) or {}
            status = rt.get("status", "unknown") if node in alive else "node_offline"
            if not spec["enabled"]:
                status = "disabled"
            elif a.get("phase") == "migrating":
                status = "migrating"
            elif not node:
                status = "unplaced"
            services.append({
                "name": svc, "description": spec.get("description", ""), "enabled": spec["enabled"],
                "node": node, "phase": a.get("phase"), "target": a.get("target"), "reason": a.get("reason"),
                "since": a.get("since"), "previous": a.get("previous"), "status": status,
                "containers": rt.get("containers", []), "error": rt.get("error", ""),
                "sync": {**spec["sync"], "last_ok": sync.get("last_ok"), "last_error": sync.get("error", ""),
                         "fail_count": sync.get("fail_count", 0), "duration": sync.get("duration"),
                         "running": False},
                "dns": spec["dns"], "dns_error": sched.dns_errors.get(svc, ""),
                "placement": spec["placement"], "failback": spec["failback"],
            })
        return {
            "cluster": {
                "me": cluster.me, "leader": leader, "term": cluster.term, "version": store.version,
                "members": len(st["nodes"]), "alive": len(alive), "quorum": cluster.quorum_size(),
                "has_quorum": cluster.has_quorum, "fenced": cluster.fenced, "dsn_version": __version__,
                "migrations": list(sched.migrations), "docker_error": agent.docker_error,
            },
            "nodes": sorted(nodes, key=lambda n: n["name"]),
            "services": sorted(services, key=lambda s: s["name"]),
            "events": list(cluster.events)[-60:][::-1],
            "ts": time.time(),
        }

    @app.get("/api/overview")
    async def api_overview(_: dict = Depends(current_user)):
        return overview()

    @app.websocket("/api/ws")
    async def ws(websocket: WebSocket):
        name = sessions.read(websocket.cookies.get(auth.SESSION_COOKIE))
        if not name or name not in store.data["users"]:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        try:
            while True:
                await websocket.send_json(overview())
                await asyncio.sleep(2)
        except (WebSocketDisconnect, RuntimeError):
            pass

    @app.get("/api/events")
    async def api_events(limit: int = 500, _: dict = Depends(current_user)):
        return list(cluster.events)[-limit:][::-1]

    @app.get("/api/nodes/{name}/history")
    async def node_history(name: str, _: dict = Depends(current_user)):
        h = cluster.history.get(name)
        if not h and name != cluster.me:
            try:
                return await cluster.call(name, "GET", f"/internal/history?node={name}")
            except (httpx.HTTPError, RemoteError):
                return []
        return list(h or [])

    # ================================================================== Dashboard: Nodes
    @app.patch("/api/nodes/{name}")
    async def node_update(name: str, body: dict, _: dict = Depends(admin)):
        args = {k: body[k] for k in ("drain", "labels", "leader_priority", "public_ip", "protect_all") if k in body}
        await submit("node_update", {"name": name, **args})
        if "drain" in args:
            cluster.emit("drain", f"{name}: Drain {'aktiviert' if args['drain'] else 'aufgehoben'}", node=name)
        return {"ok": True}

    @app.post("/api/nodes/{name}/update")
    async def node_self_update(name: str, user: dict = Depends(admin)):
        if name == cluster.me:
            try:
                r = await ctx.manager.self_update()
            except ManageError as e:
                raise HTTPException(400, str(e))
        else:
            if name not in store.data["nodes"]:
                raise HTTPException(404, "Node unbekannt")
            try:
                r = await cluster.call(name, "POST", "/internal/self_update", timeout=60)
            except (httpx.HTTPError, RemoteError) as e:
                raise HTTPException(502, str(e))
        cluster.emit("update", f"{user['username']}: Update von {name} gestartet ({r.get('image')})", node=name)
        return r

    @app.delete("/api/nodes/{name}")
    async def node_remove(name: str, _: dict = Depends(admin)):
        await submit("node_remove", {"name": name})
        cluster.emit("leave", f"Node {name} aus dem Cluster entfernt", "warning", node=name)
        return {"ok": True}

    # ------------------------------------------------------------------ Pairing per Token
    async def pair_request(method: str, addr: str, port: int, otp: str, path: str, payload=None):
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.request(method, f"http://{addr}:{port}{path}", json=payload, headers={"X-DSN-Pair": otp})
        if r.status_code >= 400:
            try:
                raise HTTPException(400, f"{addr}: {r.json().get('detail', r.text)}")
            except ValueError:
                raise HTTPException(400, f"{addr}: HTTP {r.status_code}")
        return r.json()

    @app.post("/api/pair/check")
    async def pair_check(body: dict, _: dict = Depends(admin)):
        from .pairing import decode_token
        try:
            tok = decode_token(body.get("token", ""))
        except ValueError as e:
            raise HTTPException(400, str(e))
        manual = (body.get("address") or "").strip()
        candidates = ([manual] if manual else []) + [a for a in tok.get("a", []) if a != manual]
        if not candidates:
            raise HTTPException(400, "Der Token enthält keine Adresse (z.B. Windows) – bitte die IP des Servers eintragen")
        tried = []
        for addr in candidates:
            try:
                info = await pair_request("GET", addr, tok.get("p", 7946), tok["k"], "/pair/info")
                return {"ok": True, "address": addr, "info": info, "tried": tried, "hostname": tok.get("h"),
                        "platform": tok.get("o", "linux")}
            except HTTPException as e:
                if "ungültig" in str(e.detail):
                    raise
                tried.append({"address": addr, "error": str(e.detail)})
            except httpx.HTTPError as e:
                tried.append({"address": addr, "error": f"nicht erreichbar ({type(e).__name__})"})
        return {"ok": False, "tried": tried, "hostname": tok.get("h"), "platform": tok.get("o", "linux")}

    @app.post("/api/pair/complete")
    async def pair_complete(body: dict, user: dict = Depends(admin)):
        from .pairing import decode_token
        from .state import NAME_RE
        try:
            tok = decode_token(body.get("token", ""))
        except ValueError as e:
            raise HTTPException(400, str(e))
        name = (body.get("name") or "").strip()
        if not NAME_RE.match(name):
            raise HTTPException(400, "Name: nur a-z, 0-9 und '-'")
        if name in store.data["nodes"]:
            raise HTTPException(400, f"Es gibt bereits einen Node {name}")
        if body.get("role") not in ("worker", "witness"):
            raise HTTPException(400, "Rolle muss worker oder witness sein")
        port = int(tok.get("p", 7946))
        seeds = [store.data["nodes"][n]["address"] for n in cluster.alive_nodes() if n in store.data["nodes"]]
        cfg_new = {
            "node": {"name": name, "address": body["address"], "public_ip": body.get("public_ip", ""),
                     "role": body["role"], "leader_priority": int(body.get("leader_priority", 100)),
                     "labels": {str(k): str(v) for k, v in (body.get("labels") or {}).items()}},
            "cluster": {"secret": cfg.secret, "seeds": seeds},
            "port": port,
        }
        await pair_request("POST", body["address"], port, tok["k"], "/pair", cfg_new)
        cluster.emit("join", f"{user['username']} hat {name} ({body['address']}) per Token aufgenommen – "
                             f"Server startet neu und tritt bei", node=name)
        return {"ok": True}

    @app.get("/api/join")
    async def join_info(_: dict = Depends(admin)):
        ep = cfg.endpoint
        return {
            "seed": ep,
            "command": (f"curl -fsSL http://{ep}/install.sh | sudo bash -s -- "
                        f"--seed {ep} --token '{cfg.secret}' --name <NAME> --address <OVERLAY-IP> "
                        f"--public-ip <PUBLIC-IP>"),
            # Windows mit Docker Desktop: PowerShell als Administrator
            "command_windows": (f"& ([scriptblock]::Create((irm http://{ep}/install.ps1))) -Seed {ep} "
                                f"-Token '{cfg.secret}' -Name <NAME> -Address <IP-DIESES-PCS>"),
        }

    # ================================================================== Dashboard: Services
    @app.get("/api/services/{name}")
    async def service_get(name: str, _: dict = Depends(current_user)):
        spec = store.data["services"].get(name)
        if not spec:
            raise HTTPException(404, "Service unbekannt")
        return spec

    @app.put("/api/services/{name}")
    async def service_put(name: str, body: dict, _: dict = Depends(admin)):
        body["name"] = name
        new = name not in store.data["services"]
        if not new:
            body.setdefault("adopted_from", store.data["services"][name].get("adopted_from"))
        await submit("service_upsert", body)
        cluster.emit("service", f"Service {name} {'angelegt' if new else 'geändert'}", service=name)
        return {"ok": True}

    @app.delete("/api/services/{name}")
    async def service_delete(name: str, _: dict = Depends(admin)):
        await submit("service_remove", {"name": name})
        cluster.emit("service", f"Service {name} gelöscht (Daten bleiben unter {cfg.volumes_dir}/{name})",
                     "warning", service=name)
        return {"ok": True}

    @app.post("/api/services/{name}/move")
    async def service_move(name: str, body: dict, _: dict = Depends(admin)):
        await submit("service_move", {"name": name, "node": body["node"]})
        return {"ok": True}

    async def on_service_node(name: str, method: str, path: str, payload=None, timeout: float = 120):
        a = store.data["assignments"].get(name)
        if not a or not a.get("node"):
            raise HTTPException(400, "Service läuft auf keinem Node")
        try:
            return await cluster.call(a["node"], method, path, payload, timeout=timeout)
        except (httpx.HTTPError, RemoteError) as e:
            raise HTTPException(502, f"{a['node']}: {e}")

    @app.post("/api/services/{name}/action")
    async def service_action(name: str, body: dict, _: dict = Depends(admin)):
        if body.get("action") == "sync":
            spec = store.data["services"].get(name)
            if not spec or spec["sync"]["mode"] == "none":
                raise HTTPException(400, "Sync ist für diesen Service deaktiviert")
            node = store.data["assignments"].get(name, {}).get("node")
            targets = [n for n, x in store.data["nodes"].items()
                       if n != node and x.get("role") == "worker" and Scheduler.eligible(n, spec, store.data)
                       and n in cluster.alive_nodes()]
            return await on_service_node(name, "POST", "/internal/sync",
                                         {"service": name, "targets": targets}, timeout=3600)
        return await on_service_node(name, "POST", "/internal/stack_action",
                                     {"service": name, "action": body.get("action")}, timeout=600)

    @app.get("/api/services/{name}/logs")
    async def service_logs(name: str, tail: int = 300, _: dict = Depends(current_user)):
        return await on_service_node(name, "GET", f"/internal/logs?service={name}&tail={tail}", timeout=30)

    # ================================================================== Dashboard: Container übernehmen
    async def node_call(node: str, method: str, path: str, payload=None, timeout: float = 60):
        if node not in store.data["nodes"]:
            raise HTTPException(404, "Node unbekannt")
        try:
            return await cluster.call(node, method, path, payload, timeout=timeout)
        except (httpx.HTTPError, RemoteError) as e:
            raise HTTPException(502, str(e))

    @app.get("/api/nodes/{node}/inventory")
    async def node_inventory(node: str, _: dict = Depends(current_user)):
        inv = await node_call(node, "GET", "/internal/inventory")
        # Originale, die bereits von DSN übernommen wurden, markieren
        adopted = {}
        for svc, spec in store.data["services"].items():
            af = spec.get("adopted_from") or {}
            if af.get("node") == node:
                for c in af.get("containers", []):
                    adopted[c["id"]] = svc
        for c in inv["containers"] + [c for s in inv["stacks"] for c in s["containers"]]:
            c["adopted_by"] = adopted.get(c["id"])
        for s in inv["stacks"]:
            s["adopted_by"] = next((c["adopted_by"] for c in s["containers"] if c["adopted_by"]), None)
        return inv

    @app.post("/api/nodes/{node}/docker")
    async def node_docker(node: str, body: dict, user: dict = Depends(current_user)):
        op, args = body.get("op", ""), body.get("args") or {}
        if op in WRITE_OPS:
            if user["role"] != "admin":
                raise HTTPException(403, "Nur für Admins")
        elif op not in READ_OPS:
            raise HTTPException(400, "Unbekannte Operation")
        if node == cluster.me:
            try:
                result = await ctx.manager.handle(op, args)
            except (ManageError, TypeError) as e:
                raise HTTPException(400, str(e))
        else:
            result = await node_call(node, "POST", "/internal/docker", {"op": op, "args": args}, timeout=900)
        if op in WRITE_OPS:
            what = args.get("action", "") + " " + str(args.get("id") or args.get("name") or args.get("project") or "")
            cluster.emit("docker", f"{user['username']}: {op} {what.strip()} auf {node}", node=node)
        return result

    @app.websocket("/api/console")
    async def console(websocket: WebSocket, node: str, container: str):
        name = sessions.read(websocket.cookies.get(auth.SESSION_COOKIE))
        user = store.data["users"].get(name) if name else None
        if not user or user.get("role") != "admin":
            await websocket.close(code=4401)
            return
        await websocket.accept()
        cluster.emit("console", f"{name}: Konsole in {container} auf {node} geöffnet", node=node)
        if node == cluster.me:
            await run_console_local(websocket, container)
            return
        # an den Ziel-Node weiterreichen
        from urllib.parse import quote

        from websockets.asyncio.client import connect
        url = f"ws://{cluster.endpoint_of(node)}/internal/console?container={quote(container)}"
        try:
            async with connect(url, additional_headers={"Authorization": f"Bearer {cfg.secret}"},
                               max_size=None) as remote:
                async def up():
                    while True:
                        await remote.send(await websocket.receive_text())

                async def down():
                    async for msg in remote:
                        await websocket.send_bytes(msg if isinstance(msg, bytes) else msg.encode())
                tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in tasks:
                    t.cancel()
        except Exception as e:  # noqa: BLE001 – Verbindungsfehler an den Benutzer melden
            try:
                await websocket.send_bytes(f"\r\n\x1b[31mVerbindung zu {node} fehlgeschlagen: {e}\x1b[0m\r\n".encode())
            except RuntimeError:
                pass
        try:
            await websocket.close()
        except RuntimeError:
            pass

    @app.post("/api/nodes/{node}/adopt/plan")
    async def adopt_plan(node: str, body: dict, _: dict = Depends(admin)):
        return await node_call(node, "POST", "/internal/adopt/plan", body)

    @app.post("/api/nodes/{node}/adopt")
    async def adopt(node: str, body: dict, user: dict = Depends(admin)):
        name = body.get("name", "")
        if name in store.data["services"]:
            raise HTTPException(400, f"Service {name} existiert bereits")
        preferred = [node] + [n for n in body.get("preferred", []) if n != node]
        spec = {
            "name": name, "description": body.get("description") or f"Übernommen von {node}",
            "compose": body["compose"], "env": body.get("env") or {},
            "placement": {"preferred": preferred, "auto": True, "constraints": {}},
            "sync": body.get("sync") or {"mode": "periodic", "interval": 300},
            "enabled": True,
        }
        try:
            from .state import normalize_service
            normalize_service(spec)  # Fehler im Compose VOR dem Stoppen des Originals erkennen
        except OpError as e:
            raise HTTPException(400, str(e))
        await node_call(node, "POST", "/internal/adopt/apply",
                        {"service": name, "containers": body["containers"], "mounts": body["mounts"]}, timeout=7200)
        spec["adopted_from"] = {"node": node, "kind": body.get("kind"), "ref": body.get("ref"),
                                "containers": body["containers"]}
        try:
            await cluster.submit("service_upsert", spec)
        except Exception as e:  # noqa: BLE001 – Original wiederherstellen, egal was schiefging
            await node_call(node, "POST", "/internal/adopt/restore", {"containers": body["containers"]})
            raise HTTPException(500, f"Service konnte nicht angelegt werden, Original wurde wieder gestartet: {e}")
        cluster.emit("adopt", f"{body.get('ref')} auf {node} als Service {name} in HA übernommen "
                              f"(durch {user['username']})", service=name, node=node, alert=True)
        return {"ok": True}

    @app.post("/api/services/{name}/restore")
    async def restore_original(name: str, user: dict = Depends(admin)):
        spec = store.data["services"].get(name)
        af = (spec or {}).get("adopted_from")
        if not af:
            raise HTTPException(400, "Dieser Service wurde nicht übernommen – kein Original vorhanden")
        await submit("service_upsert", {**spec, "enabled": False})
        # warten, bis der DSN-Stack überall gestoppt ist (Ports müssen frei sein)
        for _ in range(60):
            running = [n for n in store.data["nodes"]
                       if ((cluster.node_status(n).get("stacks") or {}).get(name) or {}).get("status")
                       not in (None, "missing", "stopped")]
            if not running:
                break
            await asyncio.sleep(2)
        r = await node_call(af["node"], "POST", "/internal/adopt/restore", {"containers": af["containers"]})
        cluster.emit("adopt", f"{name}: Original auf {af['node']} wiederhergestellt, DSN-Service deaktiviert "
                              f"(durch {user['username']})", "warning", service=name, node=af["node"], alert=True)
        return r

    # ================================================================== Dashboard: Einstellungen
    @app.get("/api/settings")
    async def settings_get(_: dict = Depends(admin)):
        return mask_secrets(store.settings())

    @app.patch("/api/settings")
    async def settings_patch(body: dict, _: dict = Depends(admin)):
        patch = strip_masked(body, store.settings())
        await submit("settings_update", {"patch": patch})
        return mask_secrets(store.settings())

    @app.post("/api/settings/test_alert")
    async def test_alert(user: dict = Depends(admin)):
        errors = await alerts.send(store.settings()["alerts"], "Testalarm",
                                   f"Test von {cluster.me}, ausgelöst durch {user['username']}.", "info")
        return {"ok": not errors, "errors": errors}

    # ================================================================== Prometheus
    @app.get("/metrics", response_class=PlainTextResponse)
    async def prometheus(request: Request):
        token_ok = hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {cfg.secret}")
        if not token_ok:
            current_user(request)
        lines = []
        ov = overview()
        lines.append(f"dsn_cluster_quorum {int(ov['cluster']['has_quorum'])}")
        lines.append(f"dsn_cluster_nodes_alive {ov['cluster']['alive']}")
        for n in ov["nodes"]:
            lbl = f'node="{n["name"]}"'
            m = n["metrics"]
            lines.append(f"dsn_node_up{{{lbl}}} {int(n['online'])}")
            lines.append(f"dsn_node_leader{{{lbl}}} {int(n['is_leader'])}")
            if n["rtt_ms"] is not None:
                lines.append(f"dsn_node_rtt_ms{{{lbl}}} {n['rtt_ms']}")
            if m:
                lines.append(f"dsn_node_cpu_percent{{{lbl}}} {m.get('cpu', 0)}")
                lines.append(f"dsn_node_mem_percent{{{lbl}}} {m.get('mem_percent', 0)}")
                for d in m.get("disks", []):
                    if "percent" in d:
                        lines.append(f'dsn_node_disk_percent{{{lbl},disk="{d["name"]}"}} {d["percent"]}')
        for s in ov["services"]:
            lbl = f'service="{s["name"]}",node="{s["node"] or ""}"'
            lines.append(f"dsn_service_running{{{lbl}}} {int(s['status'] == 'running')}")
            if s["sync"]["last_ok"]:
                lines.append(f'dsn_service_last_sync_timestamp{{service="{s["name"]}"}} {s["sync"]["last_ok"]}')
        return "\n".join(lines) + "\n"

    # ================================================================== Statische UI
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app
