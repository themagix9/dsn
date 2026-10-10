# DSN – Deep Space Network

Hochverfügbarkeit für Docker-Compose-Stacks über mehrere Standorte (Rechenzentrum + Zuhause),
mit Quorum, Self-Fencing, Datensync, Cloudflare-DNS-Failover, Monitoring, Alarmen und Live-Dashboard.

```
            WireGuard / Tailscale (Overlay-Netz)
 ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
 │  voyager01   │◄───►│  pioneer01   │◄───►│  galileo01   │   ← je ein DSN-Container
 │  Berlin DC   │     │  HP t630     │     │  DL360 Gen10 │     (Agent + Controller + Dashboard)
 └──────┬───────┘     └──────┬───────┘     └──────┬───────┘
        │ Heartbeats, Leader-Wahl (Mehrheit), rsync-Datensync │
        └──────────────── Cloudflare-API (DNS-Failover) ──────┘
```

## Wie es funktioniert

| Baustein | Funktion |
|---|---|
| **Heartbeats** | Jeder Node meldet sich alle 2 s bei allen anderen (inkl. Metriken). |
| **Quorum + Leader** | Nur ein Node mit Sicht auf die **Mehrheit** kann Leader werden. Stimmen gelten jeweils für eine Lease, deshalb gibt es nie zwei Leader. Der Leader trifft alle Entscheidungen. |
| **Self-Fencing** | Ein Node ohne Quorum stoppt nach 30 s **selbst** alle DSN-Services. Failover passiert erst nach 60 s. Damit kann kein Service doppelt laufen (kein Split-Brain). |
| **Failover** | Ein Node ist > 60 s weg: Der Service startet auf dem nächsten Node, DNS wird umgestellt, ein Alarm geht raus. |
| **Failback** | Der bevorzugte Node ist 10 min stabil: kontrollierte Migration zurück. |
| **Migration** | Stack auf der Quelle stoppen, finaler rsync, Start am Ziel, DNS umstellen. Es gehen dabei **keine Daten verloren**. |
| **Datensync** | Pro Service wählbar: `none`, `periodic` (rsync) oder `dump` (DB-Dump + rsync). Ein Node, auf dem der Service gerade läuft, lehnt eingehende Syncs ab. |
| **Monitoring** | CPU, RAM, Disks, Netz, Temperatur, Latenz zwischen den Nodes, RAID (HPE Smart Array / mdadm), Container-Status, Sync-Alter. Dazu ein Prometheus-Endpunkt unter `/metrics`. |
| **Alarme** | ntfy, Gotify, E-Mail, Discord/Slack, jeweils bei Problem **und** bei Behebung. |

> **Wichtig: Mindestens 3 Nodes.** Bei 2 Nodes hat nach einem Ausfall der verbleibende nur 1/2 Stimmen,
> also kein Quorum und kein automatisches Failover. Der dritte Node kann ein vollwertiger Worker sein
> (DL360) oder nur ein **Witness** (`--role witness`, z.B. ein Raspberry Pi oder ein 3-€-VPS). Er führt dann
> keine Container aus und stimmt nur mit ab.

## Installation

Auf jedem Server wird nur **ein Container** gestartet. Der ist Agent, Dashboard und Monitoring zugleich.
Voraussetzung: Docker mit Compose-Plugin.

### 1. DSN auf dem Server starten
```bash
git clone https://github.com/themagix9/dsn.git && cd dsn
docker compose up -d
docker logs dsn          # zeigt den Pairing-Token (dsn1_…)
```
Windows mit Docker Desktop (PowerShell): `docker compose -f docker-compose.windows.yml up -d` und dann
`docker logs dsn`. TCP 7946 und 8873 in der Windows-Firewall für private Netze freigeben.

Ohne Konfiguration startet DSN im **Pairing-Modus** und wartet. Der Token steht absichtlich **nur im Log**,
denn wer ihn kennt, kann den Server in einen Cluster holen.

### 2a. Erster Server: neuen Cluster gründen
`http://<server>:7946` öffnen → „Neuen Cluster gründen“ → Token, Name und Adresse eintragen.
Das angezeigte **Cluster-Secret sicher speichern**. Nach dem Neustart legst du damit das Admin-Konto an und
aktivierst unter **Konto** die 2FA.

### 2b. Weitere Server: per Token aufnehmen
Im Dashboard **Nodes → Server hinzufügen → Per Token**: Token einfügen → „Server suchen“ → Name, Rolle
(Worker/Witness), Labels wählen → „In den Cluster aufnehmen“. Der Server startet neu und tritt bei.

Das Dashboard muss den neuen Server dafür einmal erreichen: im LAN, über Tailscale oder über eine öffentliche
IP mit (vorübergehend) offenem Port 7946. Liegt er woanders, trägst du im Dialog seine erreichbare IP ein.

### 3. Tailscale (empfohlen)
In der Tailscale-Admin-Konsole einen **wiederverwendbaren Auth-Key** erzeugen (Settings → Keys → Generate auth
key → *Reusable*). Ihn im Dashboard unter **Einstellungen → Tailscale** eintragen und aktivieren. Danach gilt:
- Jeder Linux-Node startet automatisch einen Tailscale-Container (`dsn-tailscale`), neue Nodes ebenso.
- Mit „Cluster-Verkehr über Tailscale“ laufen Heartbeats und Sync verschlüsselt über das Tailnet.
- Mit „Dashboard per HTTPS“ ist das Dashboard unter `https://<node>.<tailnet>.ts.net` erreichbar, mit
  gültigem Zertifikat. Dafür in der Tailscale-Konsole MagicDNS und HTTPS-Zertifikate aktivieren.
- Windows-PCs nutzen die normale Tailscale-App.

Ports: `7946/tcp` (API/Dashboard) und `8873/tcp` (rsync, nimmt nur Cluster-Mitglieder an). Mit Tailscale
müssen sie im Internet **nicht** offen sein.

### 4. Updates
Im Dashboard unter **Nodes → Update**: DSN lädt das neueste Image und startet den Agenten neu. Die Services
laufen dabei weiter. Bitte immer einen Node nach dem anderen aktualisieren.
Das Image wird bei jedem Push auf `main` von GitHub Actions gebaut (`ghcr.io/themagix9/dsn`, amd64 + arm64).

### Alternative: Installation per Skript (ohne GitHub-Image)
`install.sh` (Linux) bzw. `install.ps1` (Windows) bauen das Image lokal und richten alles per Parameter ein.
Die Befehle stehen im Dashboard unter Nodes → Server hinzufügen → Linux-/Windows-Befehl.
## Services anlegen

Ein Service ist ein normales Compose-File. Im Dashboard unter **Services → Neuer Service**. Regeln:

* Persistente Daten **immer unter `${DSN_DATA}`** ablegen, denn nur dieses Verzeichnis wird synchronisiert:
  ```yaml
  services:
    db:
      image: postgres:16
      environment: { POSTGRES_PASSWORD: geheim }
      volumes:
        - ${DSN_DATA}/pg:/var/lib/postgresql/data
        - ${DSN_DATA}/dump:/backup
    app:
      image: nextcloud:29
      ports: ["8080:80"]
      volumes: ["${DSN_DATA}/html:/var/www/html"]
  ```
* Keine benannten Docker-Volumes und kein `container_name` verwenden.
* **Datenbanken**: Sync-Modus `dump` wählen, z.B. Service `db`, Befehl
  `pg_dump -U postgres postgres > /backup/dump.sql`. Ein periodischer rsync der laufenden DB-Dateien ist
  nicht garantiert konsistent. Der Dump ist es. Bei geplanten Migrationen wird ohnehin gestoppt und final
  synchronisiert, dann sind auch die Rohdateien konsistent.
* **Platzierung**: Liste bevorzugter Nodes (Reihenfolge = Priorität), optional Constraints über Labels
  (z.B. `storage=raid`), Rest automatisch nach freien Ressourcen.
* **DNS**: Hostnamen eintragen. Der Leader setzt die A-Records auf die `public_ip` des aktiven Nodes.
  Den Cloudflare-Token (Rechte: *Zone.DNS:Edit* + *Zone:Read*) unter **Einstellungen** hinterlegen.

### Docker-Verwaltung (wie Portainer)
Der Tab **Docker** zeigt pro Server alle Stacks und Container. Dort gibt es:
- Start, Stop, Neustart, Löschen, Logs (auch live), Details und eine **Konsole im Browser**
- **Images**, **Volumes** und **Netzwerke**: anzeigen, aufräumen, anlegen
- **Neuer Stack**: Compose-Stack deployen, wahlweise nur auf diesem Server oder als HA-Service

Jede Änderung wird im Ereignis-Log protokolliert (wer, was, wo). Der DSN-Agent selbst ist gegen Stoppen, Löschen
und Konsole gesperrt. HA-Services steuert man unter „Services“.

**Ganzen Server schützen** übernimmt alle laufenden Stacks und Container eines Servers in einem Durchgang ins HA
und markiert den Server als „vollständig geschützt“. Läuft danach ein neuer Container länger als 2 Minuten
außerhalb von DSN, kommt eine Warnung (Dashboard + Alarm-Kanäle).

### Bestehende Container übernehmen („In HA aufnehmen“)
Im Dashboard unter **Container** zeigt jeder Node alle Stacks (Compose und Portainer) und einzelnen Container.
Mit **In HA aufnehmen** startet ein Assistent:

1. Er liest das Compose-File (aus Portainer bzw. vom Host) oder erzeugt es aus `docker inspect`. `container_name`
   wird entfernt, `env_file` direkt übernommen.
2. Er zeigt alle Volumes und Bind-Mounts. Du wählst, welche Daten repliziert werden. Systempfade
   (`docker.sock`, `/etc/localtime` …) sind abgewählt.
3. Danach wird das Original **gestoppt** (Autostart aus, es wird nicht gelöscht), die Daten werden nach
   `/srv/dsn/volumes/<service>/` **kopiert**, und der Service startet als DSN-Service auf demselben Node.

Unter Services → Bearbeiten → **Original wiederherstellen** geht es jederzeit zurück. Die alten HA-Skripte
auf pioneer01 müssen deaktiviert werden, damit sie nicht gegen DSN arbeiten.

DSN schaltet für seine Container die Docker-Restart-Policy ab (`restart: "no"` per Override) und startet
abgestürzte Container selbst neu. So kann Docker nach einem Reboot keinen Service starten, der inzwischen
auf einem anderen Node läuft.

Portainer kann zum Anschauen und für Logs weiterlaufen. DSN-Stacks (`dsn-*`) dort aber **nicht bearbeiten**,
denn DSN setzt sie sonst auf den definierten Stand zurück.

## HPE DL360 Gen10: Hinweise

* **RAID-Überwachung**: `ssacli` von HPE installieren (Repository *mcp* auf downloads.linux.hpe.com).
  `install.sh` richtet dann automatisch einen Timer ein (`dsn-raid-check.timer`, alle 5 min). Ein Degraded
  Array oder eine ausgefallene Disk löst einen kritischen Alarm aus.
* **Arrays nutzen**: z.B. RAID1-600 als `/srv` mounten, dann liegen alle Service-Daten (`/srv/dsn/volumes`)
  auf dem gespiegelten Array. Mit `--disk` beide Arrays überwachen und den Node mit `--label storage=raid`
  markieren. Datenintensive Services bekommen dann den Constraint `storage=raid`.
* Die 16 GB RAM reichen für den Anfang. DSN selbst braucht ca. 80 MB. Die RAM-Auslastung pro Node
  zeigt das Dashboard, die Alarmschwelle ist einstellbar.

## Notfall-Befehle (auf einem Node)

```bash
docker logs -f dsn                                  # Logs
docker exec -it dsn python -m dsn passwd admin --admin  # Passwort zurücksetzen
docker exec -it dsn python -m dsn reset-2fa admin       # 2FA entfernen
docker exec -it dsn python -m dsn remove-user admin --force  # auch letzten Admin löschen -> Setup-Seite
```

## Labor zum Ausprobieren (virtuelle Server auf Docker Desktop)
```bash
bash lab/start.sh            # voyager01 + pioneer01 (Testnodes!) installieren, server3 bleibt leer
bash lab/demo-containers.sh  # Beispiel-Container auf pioneer01 zum Üben von "In HA aufnehmen"
bash lab/update.sh           # aktuellen Code als Rolling-Update auf alle Labor-Server
```
Dashboard: http://localhost:18941. Auf einen Labor-Server: `docker exec -it lab-server3 bash`.
Ausfall simulieren: `docker stop lab-voyager01`. Löschen: `docker compose -f lab/compose.yml down -v`.
Diesen Windows-PC dazunehmen: `lab\join-windows.ps1`, wieder entfernen: `lab\leave-windows.ps1`.

## Automatischer Test (3 Nodes per Docker-in-Docker)
```bash
docker build -t dsn:local . && docker build -t dsn:test test
bash test/scenario.sh      # Failover, Failback, Fencing – vollautomatisch
```
Dashboard: http://localhost:17941 (Test-Login wird vom Szenario angelegt, siehe `test/scenario.sh`).
Aufräumen: `docker compose -f test/compose.yml down -v`

## Grenzen / Roadmap
* Bei einem echten Ausfall gilt der Datenstand des letzten Syncs (RPO = Intervall). Für RPO ≈ 0 käme
  später Echtzeit-Replikation (z.B. DRBD) für einzelne Services in Frage.
* Metrik-Verlauf liegt 6 h im Speicher. Für Langzeit-Statistik `/metrics` in Prometheus/Grafana einbinden
  (Bearer-Token = Cluster-Secret).
* Das Dashboard läuft per HTTP im Overlay-Netz. Wer es öffentlich erreichbar machen will, sollte einen Reverse-Proxy
  mit TLS davorsetzen.
