"""DNS-Failover über die Cloudflare-API (A-Records auf die public_ip des aktiven Nodes)."""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger("dsn.dns")
API = "https://api.cloudflare.com/client/v4"


class DNSError(Exception):
    pass


class CloudflareDNS:
    def __init__(self):
        self._zones: dict[str, str] = {}

    async def _req(self, client: httpx.AsyncClient, method: str, url: str, **kw) -> dict:
        r = await client.request(method, API + url, **kw)
        data = r.json()
        if not data.get("success"):
            raise DNSError("; ".join(e.get("message", "?") for e in data.get("errors", [])) or r.text)
        return data

    async def _zone_id(self, client: httpx.AsyncClient, fqdn: str) -> str:
        parts = fqdn.split(".")
        for i in range(len(parts) - 1):
            zone = ".".join(parts[i:])
            if zone in self._zones:
                return self._zones[zone]
            data = await self._req(client, "GET", "/zones", params={"name": zone})
            if data["result"]:
                self._zones[zone] = data["result"][0]["id"]
                return self._zones[zone]
        raise DNSError(f"Keine Cloudflare-Zone für {fqdn} gefunden (Token-Rechte: Zone.DNS edit + Zone read)")

    async def ensure_a(self, token: str, fqdn: str, ip: str, ttl: int, proxied: bool) -> bool:
        """Setzt den A-Record. Gibt True zurück, wenn etwas geändert wurde."""
        if not token:
            raise DNSError("Kein Cloudflare-API-Token konfiguriert")
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(headers=headers, timeout=15) as client:
            zid = await self._zone_id(client, fqdn)
            data = await self._req(client, "GET", f"/zones/{zid}/dns_records", params={"type": "A", "name": fqdn})
            body = {"type": "A", "name": fqdn, "content": ip, "ttl": 1 if proxied else ttl, "proxied": proxied}
            recs = data["result"]
            if recs:
                rec = recs[0]
                if rec["content"] == ip and rec.get("proxied") == proxied:
                    return False
                await self._req(client, "PUT", f"/zones/{zid}/dns_records/{rec['id']}", json=body)
            else:
                await self._req(client, "POST", f"/zones/{zid}/dns_records", json=body)
            log.info("DNS %s -> %s", fqdn, ip)
            return True
