# Labor: diesen Windows-PC (Docker Desktop) als Node "win-desktop" in den Labor-Cluster aufnehmen.
# Labor-Besonderheit: Windows erreicht voyager01 über localhost:18941, die Labor-Server erreichen
# Windows über host.docker.internal. Im echten Netz nimmt man einfach die LAN-/Tailscale-IPs.
$tok = (Get-Content "$PSScriptRoot\secret.txt" -Raw).Trim()
& "$PSScriptRoot\..\install.ps1" -Seed localhost:18941 -Token $tok -Name win-desktop -Address host.docker.internal -Label location=buero
docker run --rm --entrypoint sh -v dsn-data:/v dsn:local -c "sed -i 's/localhost:18941/host.docker.internal:18941/' /v/config.yml"
docker restart dsn | Out-Null
Write-Host "win-desktop tritt dem Labor bei. Entfernen: lab\leave-windows.ps1"
