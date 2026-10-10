#!/usr/bin/env bash
# Startet das DSN-Labor und installiert DSN auf voyager01 (Cluster gründen) und pioneer01 (beitreten).
# server3 bleibt leer – den fügst du selbst über das Dashboard hinzu.
set -euo pipefail
export MSYS_NO_PATHCONV=1
cd "$(dirname "$0")"
ROOT="$(cd .. && pwd)"
X() { docker exec "$@"; }
info() { echo -e "\e[36m==>\e[0m $*"; }

info "Starte virtuelle Server"
docker compose up -d --build

for s in voyager01 pioneer01 server3; do
  until X lab-$s docker info >/dev/null 2>&1; do sleep 1; done
done

if ! X lab-voyager01 test -f /etc/dsn/config.yml; then
  info "Kopiere Quellcode nach voyager01 und gründe den Cluster (Image-Build dauert 1–2 min)"
  tar -C "$ROOT" --exclude=./lab --exclude=./test --exclude=__pycache__ -cf - . \
    | X -i lab-voyager01 sh -c 'rm -rf /root/dsn && mkdir -p /root/dsn && tar -xf - -C /root/dsn'
  X lab-voyager01 bash /root/dsn/install.sh --name voyager01 --address 172.31.0.11 \
    --public-ip 203.0.113.11 --priority 10 --label location=berlin
fi
SECRET=$(X lab-voyager01 sh -c "awk -F'\"' '/secret:/ {print \$2}' /etc/dsn/config.yml")
echo "$SECRET" > secret.txt

info "Warte, bis voyager01 antwortet"
until curl -fs http://localhost:18941/api/setup >/dev/null; do sleep 2; done

if ! X lab-pioneer01 test -f /etc/dsn/config.yml; then
  info "pioneer01 tritt dem Cluster bei (genau der Befehl aus dem Dashboard)"
  X lab-pioneer01 bash -c "curl -fsSL http://172.31.0.11:7946/install.sh | bash -s -- \
    --seed 172.31.0.11:7946 --token '$SECRET' --name pioneer01 --address 172.31.0.12 \
    --public-ip 203.0.113.12 --priority 20 --label location=home"
fi

echo
info "Labor läuft!"
echo "  Dashboard:      http://localhost:18941  (voyager01)  oder  http://localhost:18942  (pioneer01)"
echo "  Cluster-Secret: $SECRET   (auch in lab/secret.txt)"
echo "  Server 3 öffnen: docker exec -it lab-server3 bash"
echo "  Ausfall simulieren:  docker stop lab-voyager01      zurückholen: docker start lab-voyager01"
echo "  Alles löschen:       docker compose -f lab/compose.yml down -v"
