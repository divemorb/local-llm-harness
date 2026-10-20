#!/usr/bin/env bash
# Stop a running watchdog unit as soon as a step's commit ("Step ID: ...") appears.
# The watchdog reads its plan only at start, so a pause_after added later has no effect.
# Usage: stop-after.sh UNIT WORKTREE STEP_ID      (e.g. local-llm-harness-myplan ~/coding/myrepo-myplan S3)
set -euo pipefail
unit=$1 wt=$2 step=$3
while systemctl --user is-active -q "$unit"; do
  if git -C "$wt" log -20 --format=%s | grep -q "^Step $step:"; then
    systemctl --user stop "$unit"
    notify-send "local-llm-harness" "stopped after step $step (as asked)" 2>/dev/null || true
    echo "stopped $unit after step $step"
    exit 0
  fi
  sleep 5
done
echo "$unit is no longer running; step $step not committed"
