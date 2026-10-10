#!/usr/bin/env bash
# Spielt den aktuellen Code aus C:\dsn auf alle Labor-Server ein – nacheinander, wie bei einem echten Rolling-Update.
set -euo pipefail
export MSYS_NO_PATHCONV=1
cd "$(dirname "$0")"
ROOT="$(cd .. && pwd)"
SECRET=$(cat secret.txt)
info() { echo -e "\e[36m==>\e[0m $*"; }

info "voyager01: neuen Quellcode einspielen"
tar -C "$ROOT" --exclude=./lab --exclude=./test --exclude=__pycache__ -cf - . \
  | docker exec -i lab-voyager01 sh -c 'rm -rf /root/dsn && mkdir -p /root/dsn && tar -xf - -C /root/dsn'
docker exec lab-voyager01 bash /root/dsn/install.sh --update | tail -2
until curl -fs http://localhost:18941/api/setup >/dev/null; do sleep 2; done

for s in pioneer01 server3; do
  docker exec lab-$s test -f /etc/dsn/config.yml 2>/dev/null || continue
  info "$s: Update vom Seed voyager01"
  docker exec lab-$s bash -c "curl -fsSL http://172.31.0.11:7946/install.sh | bash -s -- --update \
    --seed 172.31.0.11:7946 --token '$SECRET'" | tail -2
  sleep 5
done
info "Update fertig"
