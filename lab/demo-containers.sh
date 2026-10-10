#!/usr/bin/env bash
# Legt auf lab-pioneer01 ein paar "bestehende" Container an – so wie sie heute auf deinen Servern laufen –
# damit du die Übernahme ins HA im Dashboard (Tab "Container") ausprobieren kannst.
set -euo pipefail
export MSYS_NO_PATHCONV=1
X() { docker exec -i lab-pioneer01 "$@"; }

# 1) Einzelner Container per "docker run" mit benanntem Volume
X docker rm -f notizen >/dev/null 2>&1 || true
X docker run -d --name notizen --restart unless-stopped -p 8081:80 -v notizen-daten:/usr/share/nginx/html nginx:alpine >/dev/null
X docker exec notizen sh -c 'echo "<h1>Notizen von pioneer01</h1>" > /usr/share/nginx/html/index.html'

# 2) Compose-Stack mit Datenbank + Bind-Mount-Ordner (wie ein selbst verwalteter Stack in /opt/stacks)
X sh -c 'mkdir -p /opt/stacks/wiki/html && echo "<h1>Wiki</h1>" > /opt/stacks/wiki/html/index.html && cat > /opt/stacks/wiki/docker-compose.yml' <<'EOF'
services:
  web:
    image: nginx:alpine
    container_name: wiki-web
    restart: unless-stopped
    ports:
      - "8082:80"
    volumes:
      - ./html:/usr/share/nginx/html:ro
      - /etc/localtime:/etc/localtime:ro
  db:
    image: postgres:16-alpine
    restart: unless-stopped
    env_file: .env.db
    volumes:
      - dbdata:/var/lib/postgresql/data
volumes:
  dbdata:
EOF
X sh -c 'printf "POSTGRES_PASSWORD=wiki-test-pw\nPOSTGRES_DB=wiki\n" > /opt/stacks/wiki/.env.db'
X docker compose -f /opt/stacks/wiki/docker-compose.yml -p wiki up -d >/dev/null 2>&1

# 3) Noch ein Einzel-Container ohne Daten (zustandslos)
X docker rm -f echo-api >/dev/null 2>&1 || true
X docker run -d --name echo-api --restart unless-stopped -p 8083:80 traefik/whoami >/dev/null

echo "Demo-Container auf pioneer01 angelegt: notizen (docker run + Volume), wiki (Compose: nginx + Postgres), echo-api"
