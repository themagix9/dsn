#!/usr/bin/env bash
# End-to-End-Test im lokalen 3-Node-Cluster
cd "$(dirname "$0")"
B=http://localhost:17941; JAR=jar.txt
X() { MSYS_NO_PATHCONV=1 docker exec "$@"; }
ov() { curl -s -b $JAR ${1:-$B}/api/overview > ov.json; docker run -i --rm --entrypoint python dsn:local -c "$(cat ov.py)" < ov.json; }
step() { echo; echo "================ $(date +%T) $*"; }

step "Cluster frisch starten"
docker compose down -v >/dev/null 2>&1
docker compose up -d >/dev/null 2>&1; sleep 20
curl -s -X POST $B/api/setup -H 'Content-Type: application/json' -d '{"token":"test-secret-0123456789-abcdefghijkl","username":"admin","password":"supergeheim123"}' >/dev/null; sleep 2
curl -s -c $JAR -X POST $B/api/login -H 'Content-Type: application/json' -d '{"username":"admin","password":"supergeheim123"}'; echo
curl -s -b $JAR -X PATCH $B/api/settings -H 'Content-Type: application/json' -d '{"timeouts":{"node_timeout":6,"fence":12,"failover":20,"failback_delay":30,"lease":6}}' >/dev/null
COMPOSE='services:\n  web:\n    image: traefik/whoami:latest\n    restart: unless-stopped\n    volumes:\n      - ${DSN_DATA}/data:/data\n'
curl -s -b $JAR -X PUT $B/api/services/whoami -H 'Content-Type: application/json' -d "{\"description\":\"Testservice\",\"compose\":\"$COMPOSE\",\"placement\":{\"preferred\":[\"node2\",\"node3\"],\"auto\":true},\"sync\":{\"mode\":\"periodic\",\"interval\":30}}"; echo
sleep 35; ov

step "Daten auf node2 schreiben, Sync abwarten"
X dsn-test-node2-1 sh -c 'mkdir -p /srv/dsn/volumes/whoami/data; echo "v1 von node2" > /srv/dsn/volumes/whoami/data/marker.txt'
sleep 35
for n in 1 3; do echo -n "node$n: "; X dsn-test-node$n-1 cat /srv/dsn/volumes/whoami/data/marker.txt; done

step "AUSFALL node2 (Agent + Docker gestoppt)"
docker compose stop node2 d2 >/dev/null 2>&1
sleep 40; ov
echo "Leader-Wechsel-Events auf node1: $(docker logs dsn-test-node1-1 2>&1 | grep -c 'neuer Leader')"
echo -n "Container auf d3: "; X dsn-test-d3-1 docker ps --format '{{.Names}} {{.Status}}'

step "Neue Daten auf node3 (Ersatz) schreiben"
X dsn-test-node3-1 sh -c 'echo "v2 geschrieben auf node3 waehrend Ausfall" > /srv/dsn/volumes/whoami/data/marker.txt'

step "node2 kommt zurück -> Failback nach 30 s Stabilität erwartet"
docker compose start d2 node2 >/dev/null 2>&1
sleep 75; ov
echo -n "marker auf node2 nach Failback: "; X dsn-test-node2-1 cat /srv/dsn/volumes/whoami/data/marker.txt
echo -n "Container auf d2: "; X dsn-test-d2-1 docker ps --format '{{.Names}} {{.Status}}'
echo -n "Container auf d3: "; X dsn-test-d3-1 docker ps --format '{{.Names}} {{.Status}}'; echo

step "FENCING: node1 + node3 weg -> node2 allein ohne Quorum muss whoami stoppen"
docker compose stop node1 node3 >/dev/null 2>&1
sleep 25
ov http://localhost:17942 2>/dev/null || true
echo -n "Container auf d2 (sollte leer sein): "; X dsn-test-d2-1 docker ps --format '{{.Names}} {{.Status}}'; echo
docker logs dsn-test-node2-1 2>&1 | grep -E "fence|Fencing|Quorum" | tail -3

step "node1 + node3 zurück -> Quorum, whoami wieder auf node2"
docker compose start node1 node3 >/dev/null 2>&1
sleep 30; ov
echo -n "Container auf d2: "; X dsn-test-d2-1 docker ps --format '{{.Names}} {{.Status}}'
