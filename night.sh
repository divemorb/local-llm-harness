#!/usr/bin/env bash
# Run watchdog plans one after another, unattended: the LLM server up first
# (llm.sh), stopped at the end; the next plan starts also when one stops early.
# A plan with "skip_failed": true goes on after a failed step: skip_step.py
# drops that step (never one listed in the plan's "keep": later steps depend
# on those) and the plan starts again at the next one. A run that ends within
# 2 minutes blames no step (e.g. a lock or a broken base): that plan stops.
# Log: runs/night.log.
#   systemd-run --user --unit=qwen-night ~/local-llm-harness/night.sh PLAN [PLAN ...]
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[ $# -gt 0 ] || { echo "usage: $0 PLAN [PLAN ...]   (plans/PLAN.json)"; exit 2; }
mkdir -p runs
log() { echo "[$(date '+%F %T')] $*" | tee -a runs/night.log; }

run_plan() {  # PLAN: runs it to its end; 0 success, 1 a step failed, 2 ended too early to blame a step
  local unit="local-llm-harness-$1" result start=$SECONDS
  systemctl --user reset-failed "$unit" 2>/dev/null || true
  log "start plan $1"
  systemd-run --user --unit="$unit" --working-directory="$PWD" "$PWD/watchdog.py" "plans/$1.json"
  while systemctl --user is-active -q "$unit"; do sleep 30; done
  result=$(systemctl --user show -p Result --value "$unit")
  log "plan $1 ended: $result after $(( (SECONDS - start) / 60 )) min"
  [ "$result" = success ] && return 0
  [ $((SECONDS - start)) -lt 120 ] && return 2
  return 1
}

skip_failed() {  # PLAN: whether the plan asks to go on after a failed step
  python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("skip_failed") else 1)' "plans/$1.json"
}

./llm.sh start >/dev/null || { log "llm server did not start"; exit 1; }
log "llm ready"
for plan in "$@"; do
  while true; do
    run_plan "$plan"; rc=$?
    [ $rc -eq 0 ] && break
    [ $rc -eq 2 ] && { log "$plan: ended too early to blame a step; stopped"; break; }
    skip_failed "$plan" || break
    step=$(python3 skip_step.py "plans/$plan.json") || { log "$plan: stopped (a 'keep' step failed or no step left)"; break; }
    log "$plan: step $step failed, skipped; going on with the next step"
  done
done
./llm.sh stop >/dev/null
log "night done, llm stopped"
