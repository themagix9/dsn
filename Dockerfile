FROM docker:27.5-cli AS dockercli

FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends rsync ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

# Docker CLI + Compose-Plugin: der Agent steuert den Host-Docker über /var/run/docker.sock
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Frontend-Bibliotheken beim Build einbinden -> Dashboard braucht zur Laufzeit kein Internet/CDN
ADD https://unpkg.com/vue@3.4.38/dist/vue.global.prod.js /app/vendor/vue.global.prod.js
ADD https://cdn.jsdelivr.net/npm/qrcode-generator@1.4.4/qrcode.js /app/vendor/qrcode.js
ADD https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.js /app/vendor/xterm.js
ADD https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.css /app/vendor/xterm.css
ADD https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.js /app/vendor/addon-fit.js

# Kompletter Quellcode: wird neuen Nodes über /dist/dsn.tar.gz ausgeliefert (kein Registry nötig)
COPY . /app/src
RUN cp -r /app/src/dsn /app/dsn \
 && mkdir -p /app/dsn/web/vendor && cp /app/vendor/* /app/dsn/web/vendor/ && chmod 644 /app/dsn/web/vendor/* \
 && install -m 755 /app/src/scripts/dsn-presync /usr/local/bin/dsn-presync

# Build-Kennung (von der GitHub-Action gesetzt: Commit + Datum), wird im Dashboard angezeigt
ARG DSN_BUILD=lokal
ENV PYTHONUNBUFFERED=1 DSN_SRC=/app/src DSN_BUILD=${DSN_BUILD}
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "dsn"]
CMD ["run"]
