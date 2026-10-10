"""Benachrichtigungen: ntfy, Gotify, E-Mail (SMTP), Discord/Slack-Webhooks."""
from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage

import httpx

log = logging.getLogger("dsn.alerts")

PRIO = {"info": (3, 4), "warning": (4, 6), "critical": (5, 9)}  # (ntfy, gotify)
EMOJI = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}
COLOR = {"info": 0x3B82F6, "warning": 0xF59E0B, "critical": 0xEF4444}


async def send(cfg: dict, title: str, message: str, severity: str = "info") -> list[str]:
    """Sendet an alle konfigurierten Kanäle. Gibt eine Liste von Fehlern zurück (leer = alles ok)."""
    errors: list[str] = []
    tasks = []
    async with httpx.AsyncClient(timeout=15) as client:
        ntfy = cfg.get("ntfy", {})
        if ntfy.get("url") and ntfy.get("topic"):
            # JSON-Publishing, damit Umlaute im Titel funktionieren (HTTP-Header sind nur ASCII)
            headers = {"Authorization": f"Bearer {ntfy['token']}"} if ntfy.get("token") else {}
            tasks.append(("ntfy", client.post(ntfy["url"].rstrip("/"), headers=headers, json={
                "topic": ntfy["topic"], "title": title, "message": message,
                "priority": PRIO[severity][0], "tags": [severity]})))
        gotify = cfg.get("gotify", {})
        if gotify.get("url") and gotify.get("token"):
            tasks.append(("gotify", client.post(f"{gotify['url'].rstrip('/')}/message",
                                                params={"token": gotify["token"]},
                                                json={"title": title, "message": message,
                                                      "priority": PRIO[severity][1]})))
        for wh in cfg.get("webhooks", []):
            if not wh.get("url"):
                continue
            if wh.get("type") == "slack":
                body = {"text": f"{EMOJI[severity]} *{title}*\n{message}"}
            else:
                body = {"embeds": [{"title": f"{EMOJI[severity]} {title}", "description": message[:4000],
                                    "color": COLOR[severity]}]}
            tasks.append((wh.get("type", "webhook"), client.post(wh["url"], json=body)))

        for name, coro in tasks:
            try:
                r = await coro
                if r.status_code >= 300:
                    errors.append(f"{name}: HTTP {r.status_code} {r.text[:200]}")
            except httpx.HTTPError as e:
                errors.append(f"{name}: {e}")

    mail = cfg.get("email", {})
    if mail.get("host") and mail.get("recipients"):
        try:
            await asyncio.to_thread(_send_mail, mail, f"[DSN] {EMOJI[severity]} {title}", message)
        except Exception as e:  # noqa: BLE001 – jede SMTP-Ausnahme melden
            errors.append(f"email: {e}")

    for e in errors:
        log.warning("Alarm-Versand fehlgeschlagen: %s", e)
    return errors


def _send_mail(mail: dict, subject: str, body: str):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail.get("sender") or mail.get("username")
    msg["To"] = mail["recipients"]
    msg.set_content(body)
    port = int(mail.get("port", 587))
    if port == 465:
        smtp = smtplib.SMTP_SSL(mail["host"], port, timeout=20)
    else:
        smtp = smtplib.SMTP(mail["host"], port, timeout=20)
        if mail.get("starttls", True):
            smtp.starttls()
    with smtp:
        if mail.get("username"):
            smtp.login(mail["username"], mail.get("password", ""))
        smtp.send_message(msg)
