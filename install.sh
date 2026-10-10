#!/usr/bin/env bash
# DSN – Deep Space Network: Node installieren, beitreten oder aktualisieren.
#
# Erster Node (gründet den Cluster, im entpackten Quellverzeichnis ausführen):
#   sudo ./install.sh --name voyager01 --address 100.64.0.1 --public-ip 203.0.113.10
#
# Weitere Nodes (Befehl steht im Dashboard unter Nodes -> Server hinzufügen):
#   curl -fsSL http://100.64.0.1:7946/install.sh | sudo bash -s -- --seed 100.64.0.1:7946 --token '<secret>' \
#        --name pioneer01 --address 100.64.0.2 --public-ip 198.51.100.7
#
# Update eines bestehenden Nodes (Konfiguration bleibt erhalten):
#   curl -fsSL http://<node>:7946/install.sh | sudo bash -s -- --update --seed <node>:7946 --token '<secret>'
set -euo pipefail

NAME="" ADDRESS="" PUBLIC_IP="" SEED="" TOKEN="" ROLE="worker" PRIORITY=100 PORT=7946 UPDATE=0
LABELS=() DISKS=()
die() { echo -e "\e[31mFehler:\e[0m $*" >&2; exit 1; }
info() { echo -e "\e[36m==>\e[0m $*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --address) ADDRESS="$2"; shift 2 ;;
    --public-ip) PUBLIC_IP="$2"; shift 2 ;;
    --seed) SEED="$2"; shift 2 ;;
    --token) TOKEN="$2"; shift 2 ;;
    --role) ROLE="$2"; shift 2 ;;
    --priority) PRIORITY="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --label) LABELS+=("$2"); shift 2 ;;
    --disk) DISKS+=("$2"); shift 2 ;;
    --update) UPDATE=1; shift ;;
    -h|--help) sed -n '2,15p' "$0" 2>/dev/null || true; exit 0 ;;
    *) die "Unbekannte Option: $1" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "Bitte als root (sudo) ausführen"

# ------------------------------------------------------------------ Docker
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker ist nicht installiert."
  read -r -p "Jetzt über get.docker.com installieren? [j/N] " ans </dev/tty || ans=n
  [[ "$ans" =~ ^[jJyY]$ ]] || die "Docker wird benötigt"
  curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null 2>&1 || die "Docker-Compose-Plugin fehlt (apt install docker-compose-plugin)"
command -v rsync >/dev/null 2>&1 || true

mkdir -p /etc/dsn /var/lib/dsn/hostchecks /srv/dsn/volumes /opt/dsn
chmod 700 /etc/dsn

# ------------------------------------------------------------------ Quellcode holen
if [[ -n "$SEED" ]]; then
  [[ -n "$TOKEN" ]] || die "--token fehlt"
  info "Lade DSN-Quellcode von $SEED"
  tmp=$(mktemp -d)
  curl -fsSL -H "Authorization: Bearer $TOKEN" "http://$SEED/dist/dsn.tar.gz" | tar xz -C "$tmp" \
    || die "Download von $SEED fehlgeschlagen (Token falsch oder Node nicht erreichbar?)"
  rm -rf /opt/dsn/src && mv "$tmp/dsn" /opt/dsn/src && rm -rf "$tmp"
else
  SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  [[ -f "$SRC_DIR/Dockerfile" ]] || die "Ohne --seed muss install.sh aus dem DSN-Quellverzeichnis gestartet werden"
  if [[ "$SRC_DIR" != "/opt/dsn/src" ]]; then rm -rf /opt/dsn/src && cp -r "$SRC_DIR" /opt/dsn/src; fi
fi

info "Baue Image dsn:local (dauert beim ersten Mal 1–2 Minuten)"
docker build -q -t dsn:local /opt/dsn/src >/dev/null
cp /opt/dsn/src/docker-compose.yml /opt/dsn/docker-compose.yml
echo "DSN_IMAGE=dsn:local" > /opt/dsn/.env   # lokal gebautes Image statt ghcr.io

# ------------------------------------------------------------------ Konfiguration
if [[ $UPDATE -eq 1 ]]; then
  [[ -f /etc/dsn/config.yml ]] || die "Keine bestehende Installation gefunden (/etc/dsn/config.yml)"
else
  [[ -f /etc/dsn/config.yml ]] && die "/etc/dsn/config.yml existiert bereits – für ein Update --update verwenden"
  [[ -n "$NAME" && -n "$ADDRESS" ]] || die "--name und --address (Overlay-IP) sind Pflicht"
  [[ "$NAME" =~ ^[a-z0-9][a-z0-9-]{0,40}$ ]] || die "Name: nur a-z, 0-9 und '-'"
  if [[ -z "$TOKEN" ]]; then
    TOKEN=$(docker run --rm --entrypoint python dsn:local -m dsn gen-secret)
    NEW_CLUSTER=1
  fi
  {
    echo "node:"
    echo "  name: $NAME"
    echo "  address: $ADDRESS"
    echo "  public_ip: \"$PUBLIC_IP\""
    echo "  role: $ROLE"
    echo "  leader_priority: $PRIORITY"
    echo "  labels:"
    if [[ ${#LABELS[@]} -eq 0 ]]; then echo "    {}"; fi
    for l in "${LABELS[@]}"; do echo "    ${l%%=*}: \"${l#*=}\""; done
    echo "cluster:"
    echo "  secret: \"$TOKEN\""
    echo "  seeds: [${SEED:+\"$SEED\"}]"
    echo "port: $PORT"
    echo "bind: 0.0.0.0"
    echo "disks:"
    echo "  - { name: root, path: / }"
    for d in "${DISKS[@]}"; do echo "  - { name: \"${d%%:*}\", path: \"${d#*:}\" }"; done
  } > /etc/dsn/config.yml
  chmod 600 /etc/dsn/config.yml
fi

# ------------------------------------------------------------------ Host-Checks (RAID)
if [[ -d /run/systemd/system ]] && { command -v ssacli >/dev/null 2>&1 || grep -q '^md' /proc/mdstat 2>/dev/null; }; then
  info "Richte RAID-Check (alle 5 Minuten) ein"
  install -m 755 /opt/dsn/src/scripts/raid-check.sh /usr/local/sbin/dsn-raid-check
  cat > /etc/systemd/system/dsn-raid-check.service <<'EOF'
[Unit]
Description=DSN RAID-Check
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/dsn-raid-check
EOF
  cat > /etc/systemd/system/dsn-raid-check.timer <<'EOF'
[Unit]
Description=DSN RAID-Check alle 5 Minuten
[Timer]
OnBootSec=1min
OnUnitActiveSec=5min
[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  systemctl enable --now dsn-raid-check.timer >/dev/null
  /usr/local/sbin/dsn-raid-check || true
fi

# ------------------------------------------------------------------ Start
info "Starte DSN"
docker compose -f /opt/dsn/docker-compose.yml up -d --force-recreate

PORT_CFG=$(awk '/^port:/ {print $2}' /etc/dsn/config.yml)
ADDR_CFG=$(awk '/^  address:/ {print $2; exit}' /etc/dsn/config.yml)
echo
info "Fertig! Dashboard: http://${ADDR_CFG}:${PORT_CFG:-7946}"
if [[ "${NEW_CLUSTER:-0}" == 1 ]]; then
  echo
  echo "Neuer Cluster gegründet. Cluster-Secret (gut aufbewahren, z.B. im Passwortmanager):"
  echo "  $TOKEN"
  echo "Beim ersten Aufruf des Dashboards legst du damit das Admin-Konto an."
fi
echo "Logs: docker logs -f dsn"
