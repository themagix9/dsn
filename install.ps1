<#
DSN - Deep Space Network: Node auf Windows mit Docker Desktop installieren, beitreten oder aktualisieren.
(Absichtlich ohne Umlaute: Windows PowerShell 5.1 liest Skripte sonst falsch ein.)

Voraussetzungen: Docker Desktop laeuft im Linux-Container-Modus. PowerShell "Als Administrator" starten
(fuer die Firewall-Regel).

Erster Node (gruendet den Cluster), im DSN-Quellordner:
  .\install.ps1 -Name win01 -Address 192.168.1.50

Weiteren Node beitreten (Befehl steht im Dashboard unter Nodes -> Server hinzufuegen):
  & ([scriptblock]::Create((irm http://<seed>:7946/install.ps1))) -Seed <seed>:7946 -Token '<secret>' -Name win01 -Address 192.168.1.50

Update (Konfiguration bleibt erhalten):
  & ([scriptblock]::Create((irm http://<seed>:7946/install.ps1))) -Update -Seed <seed>:7946 -Token '<secret>'
#>
param(
    [string]$Name = "",
    [string]$Address = "",          # IP dieses PCs, unter der die anderen Nodes ihn erreichen (LAN/Tailscale)
    [string]$PublicIp = "",
    [string]$Seed = "",
    [string]$Token = "",
    [ValidateSet("worker", "witness")][string]$Role = "worker",
    [int]$Priority = 100,
    [string[]]$Label = @(),
    [switch]$Update
)
# "Continue": Docker schreibt Fortschritt nach stderr – PowerShell 5.1 wuerde das sonst als Fehler werten.
# Fehler werden ueber $LASTEXITCODE bzw. try/catch erkannt.
$ErrorActionPreference = "Continue"
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
function Info($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Fail($m) { Write-Host "Fehler: $m" -ForegroundColor Red; exit 1 }
$Vol = "/var/lib/docker/volumes/dsn-data/_data"

# ------------------------------------------------------------------ Docker Desktop pruefen
docker info *> $null
if ($LASTEXITCODE -ne 0) { Fail "Docker Desktop laeuft nicht. Bitte starten und erneut versuchen." }
$os = docker info --format "{{.OSType}}"
if ($os -ne "linux") { Fail "Docker Desktop muss im Linux-Container-Modus laufen (aktuell: $os)." }

$Base = Join-Path $env:ProgramData "DSN"
New-Item -ItemType Directory -Force $Base | Out-Null
$Src = Join-Path $Base "src"

# ------------------------------------------------------------------ Quellcode holen
if ($Seed) {
    if (-not $Token) { Fail "-Token fehlt" }
    Info "Lade DSN-Quellcode von $Seed"
    $tmp = Join-Path $env:TEMP ("dsn-" + [guid]::NewGuid())
    New-Item -ItemType Directory $tmp | Out-Null
    $tgz = Join-Path $tmp "dsn.tar.gz"
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "http://$Seed/dist/dsn.tar.gz" -Headers @{ Authorization = "Bearer $Token" } -OutFile $tgz -ErrorAction Stop
    } catch { Fail "Download von $Seed fehlgeschlagen (Token falsch oder Node nicht erreichbar?): $_" }
    tar -xzf $tgz -C $tmp
    if (Test-Path $Src) { Remove-Item -Recurse -Force $Src }
    Move-Item (Join-Path $tmp "dsn") $Src
    Remove-Item -Recurse -Force $tmp
} else {
    if (-not (Test-Path (Join-Path $PSScriptRoot "Dockerfile"))) { Fail "Ohne -Seed muss install.ps1 aus dem DSN-Quellordner gestartet werden" }
    if ((Resolve-Path $PSScriptRoot).Path.TrimEnd("\") -ne $Src.TrimEnd("\")) {
        if (Test-Path $Src) { Remove-Item -Recurse -Force $Src }
        Copy-Item -Recurse $PSScriptRoot $Src
    }
}

Info "Baue Image dsn:local (dauert beim ersten Mal 1-2 Minuten)"
docker build -q -t dsn:local $Src | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "docker build fehlgeschlagen" }
docker volume create dsn-data | Out-Null

function InVolume([string]$cmd) {
    # Pipeline-Eingabe (z.B. die Config) an den Container weiterreichen
    $input | docker run --rm -i --entrypoint sh -v "dsn-data:$Vol" dsn:local -c $cmd
}

# ------------------------------------------------------------------ Konfiguration (liegt im Volume)
$hasConfig = (InVolume "test -f $Vol/config.yml && echo yes") -eq "yes"
$newCluster = $false
if ($Update) {
    if (-not $hasConfig) { Fail "Keine bestehende Installation gefunden" }
} else {
    if ($hasConfig) { Fail "DSN ist hier bereits installiert - fuer ein Update -Update verwenden" }
    if (-not $Name -or -not $Address) { Fail "-Name und -Address sind Pflicht" }
    if ($Name -notmatch '^[a-z0-9][a-z0-9-]{0,40}$') { Fail "Name: nur a-z, 0-9 und '-'" }
    if (-not $Token) { $Token = (docker run --rm --entrypoint python dsn:local -m dsn gen-secret).Trim(); $newCluster = $true }
    $labels = ($Label | ForEach-Object { $k, $v = $_ -split "=", 2; "    ${k}: `"$v`"" }) -join "`n"
    if (-not $labels) { $labels = "    {}" }
    $seeds = if ($Seed) { "[`"$Seed`"]" } else { "[]" }
    $yaml = @"
node:
  name: $Name
  address: "$Address"
  public_ip: "$PublicIp"
  role: $Role
  platform: windows
  leader_priority: $Priority
  labels:
$labels
cluster:
  secret: "$Token"
  seeds: $seeds
port: 7946
bind: 0.0.0.0
rsync_bind: 0.0.0.0
data_dir: $Vol/lib
volumes_dir: $Vol/volumes
host_root: /host
disks:
  - { name: Docker-Volumes, path: /var/lib/docker/volumes }
"@
    # sed entfernt ein evtl. von PowerShell vorangestelltes BOM
    $yaml.Replace("`r", "") | InVolume "sed '1s/^\xEF\xBB\xBF//' > $Vol/config.yml && chmod 600 $Vol/config.yml"
}

# ------------------------------------------------------------------ Start
Copy-Item -Force (Join-Path $Src "docker-compose.windows.yml") (Join-Path $Base "docker-compose.yml")
# lokal gebautes Image statt ghcr.io verwenden
[IO.File]::WriteAllText((Join-Path $Base ".env"), "DSN_IMAGE=dsn:local`n")
Info "Starte DSN"
docker compose -f (Join-Path $Base "docker-compose.yml") -p dsn up -d --force-recreate 2>&1 | ForEach-Object { "$_" }
if ($LASTEXITCODE -ne 0) { Fail "Start fehlgeschlagen" }

# ------------------------------------------------------------------ Firewall
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isAdmin) {
    if (-not (Get-NetFirewallRule -DisplayName "DSN Cluster" -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName "DSN Cluster" -Direction Inbound -Protocol TCP -LocalPort 7946, 8873 -Action Allow -Profile Private, Domain | Out-Null
        Info "Firewall-Regel 'DSN Cluster' (TCP 7946, 8873, nur private Netze) angelegt"
    }
} else {
    Write-Host "Hinweis: Nicht als Administrator gestartet - Firewall-Regel fuer TCP 7946 und 8873 bitte selbst anlegen." -ForegroundColor Yellow
}

Write-Host ""
$addr = if ($Address) { $Address } else { "<diese-IP>" }
Info "Fertig! Dashboard: http://${addr}:7946  (lokal: http://localhost:7946)"
if ($newCluster) {
    Write-Host ""
    Write-Host "Neuer Cluster gegruendet. Cluster-Secret (gut aufbewahren):"
    Write-Host "  $Token"
}
Write-Host "Logs: docker logs -f dsn"
