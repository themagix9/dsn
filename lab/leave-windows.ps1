# Entfernt den DSN-Agenten von diesem Windows-PC wieder (Container + Daten-Volume "dsn-data").
# Deine anderen Container werden nicht angefasst. Danach den Node im Dashboard unter Nodes entfernen.
docker compose -p dsn -f "$env:ProgramData\DSN\docker-compose.yml" down 2>&1 | Out-Null
docker volume rm dsn-data 2>&1 | Out-Null
Write-Host "DSN-Agent von diesem PC entfernt."
