"""DSN – Deep Space Network.

  python -m dsn run                       Agent + Controller + Dashboard starten
  python -m dsn gen-secret                Neues Cluster-Secret erzeugen
  python -m dsn passwd <user> [--admin]   Passwort setzen / Benutzer anlegen (z.B. Notfallzugang)
  python -m dsn reset-2fa <user>          2FA eines Benutzers entfernen
  python -m dsn remove-user <user> [--force]  Benutzer löschen (--force: auch den letzten Admin)
"""
from __future__ import annotations

import asyncio
import getpass
import json
import logging
import os
import secrets
import sys
import urllib.request

from . import config


def cmd_run():
    import uvicorn

    from .adopt import Adopter
    from .manage import Manager
    from .tailscale import TailscaleManager
    from .agent import Agent
    from .api import Ctx, create_app
    from .cluster import Cluster
    from .docker_ops import DockerOps
    from .metrics import MetricsCollector
    from .scheduler import Scheduler
    from .state import StateStore

    logging.basicConfig(level=os.environ.get("DSN_LOG", "INFO"),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        cfg = config.load()
    except config.NotConfigured:
        from .pairing import run_pairing
        logging.getLogger("dsn").info("Keine Konfiguration gefunden – starte im Pairing-Modus")
        run_pairing()
        return
    os.makedirs(cfg.data_dir, exist_ok=True)
    os.makedirs(cfg.hostchecks_dir, exist_ok=True)
    store = StateStore(os.path.join(cfg.data_dir, "state.json"))
    cluster = Cluster(cfg, store, MetricsCollector(cfg))
    docker = DockerOps(cfg)
    agent = Agent(cfg, cluster, docker)
    sched = Scheduler(cluster)
    adopter = Adopter(cfg)
    app = create_app(Ctx(cfg, store, cluster, agent, sched, docker, adopter, Manager(cfg)))

    async def main():
        server = uvicorn.Server(uvicorn.Config(app, host=cfg.bind, port=cfg.port, log_level="warning"))
        loops = [cluster.run(), sched.run(), sched.run_dns(), sched.run_monitor(), TailscaleManager(cfg, cluster).run()]
        if cfg.node.role == "worker":
            loops += [agent.run_reconcile(), agent.run_sync(), agent.run_rsyncd(), agent.run_inventory(adopter)]
        logging.getLogger("dsn").info("DSN %s startet als %s (%s) auf %s", __import__("dsn").__version__,
                                      cfg.node.name, cfg.node.role, cfg.endpoint)
        await asyncio.gather(server.serve(), *loops)

    asyncio.run(main())


def _local_submit(op: str, args: dict):
    cfg = config.load()
    host = "127.0.0.1" if cfg.bind == "0.0.0.0" else cfg.bind
    req = urllib.request.Request(f"http://{host}:{cfg.port}/internal/submit", method="POST",
                                 data=json.dumps({"op": op, "args": args}).encode(),
                                 headers={"Authorization": f"Bearer {cfg.secret}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        print("OK, State-Version", json.load(r)["version"])


def main():
    args = sys.argv[1:] or ["run"]
    if args[0] == "run":
        cmd_run()
    elif args[0] == "gen-secret":
        print(secrets.token_urlsafe(32))
    elif args[0] == "passwd" and len(args) >= 2:
        from .auth import hash_password
        pw = getpass.getpass("Neues Passwort: ")
        if pw != getpass.getpass("Wiederholen: "):
            sys.exit("Passwörter stimmen nicht überein")
        a = {"username": args[1], "password_hash": hash_password(pw)}
        if "--admin" in args:
            a["role"] = "admin"
        _local_submit("user_upsert", a)
    elif args[0] == "remove-user" and len(args) >= 2:
        # --force erlaubt auch das Löschen des letzten Admins (danach ist die Setup-Seite wieder aktiv)
        _local_submit("user_remove", {"username": args[1], "force": "--force" in args})
    elif args[0] == "reset-2fa" and len(args) == 2:
        _local_submit("user_upsert", {"username": args[1], "totp_secret": ""})
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
