#!/usr/bin/env bash
# RAID-Check für DSN: schreibt /var/lib/dsn/hostchecks/raid.json
# Unterstützt HPE Smart Array (ssacli, z.B. P408i-a im DL360 Gen10) und Linux-Software-RAID (mdadm).
# ssacli installieren: https://downloads.linux.hpe.com/SDR/project/mcp/ (Paket "ssacli")
set -uo pipefail
OUT=/var/lib/dsn/hostchecks/raid.json
mkdir -p "$(dirname "$OUT")"
status="ok"; details=(); summary=""

esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
worse() { # $1 = neuer Status
  case "$1" in critical) status=critical ;; warning) [[ $status == ok ]] && status=warning ;; esac
}

if command -v ssacli >/dev/null 2>&1; then
  cfg=$(ssacli ctrl all show config 2>&1)
  st=$(ssacli ctrl all show status 2>&1)
  ld_total=0; ld_bad=0; pd_total=0; pd_bad=0
  while IFS= read -r line; do
    line=$(echo "$line" | sed 's/^ *//')
    case "$line" in
      logicaldrive*)
        ld_total=$((ld_total+1))
        state=$(echo "$line" | sed -E 's/.*, ([^,]+)\)$/\1/')
        if [[ "$state" != "OK" ]]; then
          ld_bad=$((ld_bad+1))
          [[ "$state" =~ Failed ]] && worse critical || worse warning
          details+=("$line")
        fi ;;
      physicaldrive*)
        pd_total=$((pd_total+1))
        state=$(echo "$line" | sed -E 's/.*, ([^,]+)\)$/\1/')
        if [[ "$state" != "OK" ]]; then
          pd_bad=$((pd_bad+1))
          [[ "$state" =~ Failed ]] && worse critical || worse warning
          details+=("$line")
        fi ;;
    esac
  done <<< "$cfg"
  while IFS= read -r line; do
    line=$(echo "$line" | sed 's/^ *//')
    if [[ "$line" == *"Status:"* && "$line" != *"Status: OK"* && "$line" != *"Not Configured"* ]]; then
      worse warning; details+=("Controller: $line")
    fi
  done <<< "$st"
  summary="Smart Array: $ld_total Logical Drives ($ld_bad Problem), $pd_total Disks ($pd_bad Problem)"
  if [[ $ld_total -eq 0 ]]; then worse warning; summary="ssacli lieferte keine Logical Drives: $(echo "$cfg" | head -c 200)"; fi
elif [[ -e /proc/mdstat ]]; then
  arrays=$(grep -c '^md' /proc/mdstat || true)
  while IFS= read -r line; do
    if [[ "$line" =~ \[([U_]+)\] ]] && [[ "${BASH_REMATCH[1]}" == *_* ]]; then
      worse critical; details+=("Degraded: $line")
    fi
    [[ "$line" == *recovery* || "$line" == *resync* ]] && { worse warning; details+=("$(echo "$line" | sed 's/^ *//')"); }
  done < /proc/mdstat
  summary="mdadm: $arrays Arrays"
else
  status="warning"; summary="Weder ssacli noch /proc/mdstat gefunden"
fi

[[ $status == ok ]] && summary="$summary – alles OK"
{
  printf '{"name":"RAID","status":"%s","summary":"%s","ts":%s,"details":[' "$status" "$(esc "$summary")" "$(date +%s)"
  sep=""
  for d in "${details[@]}"; do printf '%s"%s"' "$sep" "$(esc "$d")"; sep=","; done
  printf ']}\n'
} > "$OUT.tmp" && mv "$OUT.tmp" "$OUT"
