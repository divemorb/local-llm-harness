#!/usr/bin/env bash
# llm.sh – run a local llama.cpp server (Qwen3.8-27B by default) in rootless Podman.
# Usage: llm.sh [start|stop|restart|recreate|status|logs]   (default: start)
# Settings can be overridden by environment variables of the same name.
set -euo pipefail

# --- Settings ---------------------------------------------------------------
NAME="${NAME:-llm}"
IMAGE="${IMAGE:-ghcr.io/ggml-org/llama.cpp:server-cuda}"
MODEL_DIR="${MODEL_DIR:-$HOME/llm-models}"
HF_MODEL="${HF_MODEL:-unsloth/Qwen3.8-27B-GGUF:Q5_K_M}"  # only used when no local file matches
MODEL_PATTERN="${MODEL_PATTERN:-*Q5_K_M*.gguf}"
ALIAS="${ALIAS:-qwen3.8-27b}"                            # the model name in the API (and in opencode.json)
PORT="${PORT:-8080}"
KEY_FILE="${KEY_FILE:-$HOME/.config/llm/api-key}"        # API key; mounted as a file, never on the command line
CTX="${CTX:-131072}"                                     # context size in tokens (the plan's context_tokens)
read -ra CONFLICTS <<< "${CONFLICTS:-}"                  # containers that also use the GPU's memory (space-separated)
READY_TIMEOUT="${READY_TIMEOUT:-600}"                    # seconds (the first start includes the download)

# --- Helpers ----------------------------------------------------------------
is_running() { [[ "$(podman inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" == "true" ]]; }

check_conflicts() {
  local c a
  for c in "${CONFLICTS[@]}"; do
    if is_running "$c"; then
      if [[ ! -t 0 ]]; then echo "Container '$c' is running and uses GPU memory; stop it first."; exit 1; fi
      read -rp "Container '$c' is running and uses GPU memory. Stop it? [y/N] " a
      if [[ "$a" == [yY] ]]; then
        podman stop "$c" >/dev/null
      else
        echo "Aborted."; exit 1
      fi
    fi
  done
}

set_model_args() {
  local f
  f=$(find "$MODEL_DIR" \( -type f -o -type l \) -name "$MODEL_PATTERN" ! -name 'mmproj*' -print -quit)
  if [[ -n "$f" ]]; then
    MODEL_ARGS=(-m "/models/${f#"$MODEL_DIR"/}")
    echo "Local model: ${f#"$MODEL_DIR"/}"
  else
    MODEL_ARGS=(-hf "$HF_MODEL")
    echo "No local model found: it is downloaded on the first start."
  fi
}

create() {
  mkdir -p "$MODEL_DIR"
  set_model_args
  podman run -d --name "$NAME" \
    --device nvidia.com/gpu=all \
    -p "127.0.0.1:$PORT:8080" \
    -v "$MODEL_DIR:/models" \
    -e LLAMA_CACHE=/models \
    -v "$KEY_FILE:/run/secrets/llama-api-key:ro" \
    "$IMAGE" \
    "${MODEL_ARGS[@]}" \
    --alias "$ALIAS" --no-mmproj -ngl 99 -c "$CTX" --jinja \
    --api-key-file /run/secrets/llama-api-key \
    --host 0.0.0.0 --port 8080 >/dev/null
}

wait_ready() {
  local i
  echo -n "Waiting for the server"
  for ((i = 0; i < READY_TIMEOUT; i += 2)); do
    if curl -sf "http://127.0.0.1:$PORT/health" >/dev/null; then
      echo " – ready: http://127.0.0.1:$PORT"
      return 0
    fi
    if ! is_running "$NAME"; then
      echo; echo "The container stopped. Last log lines:"
      podman logs --tail 30 "$NAME"
      exit 1
    fi
    echo -n "."; sleep 2
  done
  echo; echo "Timed out. Check with: $0 logs"; exit 1
}

start() {
  if is_running "$NAME"; then echo "Already running: http://127.0.0.1:$PORT"; return; fi
  check_conflicts
  if podman container exists "$NAME"; then
    podman start "$NAME" >/dev/null
  else
    create
  fi
  wait_ready
}

# --- Main -------------------------------------------------------------------
case "${1:-start}" in
  start)    start ;;
  stop)     podman stop "$NAME" >/dev/null && echo "Stopped." ;;
  restart)  podman stop "$NAME" >/dev/null 2>&1 || true; start ;;
  recreate) podman rm -f "$NAME" >/dev/null 2>&1 || true; start ;;
  status)   if is_running "$NAME"; then echo "Running: http://127.0.0.1:$PORT"; else echo "Stopped."; fi ;;
  logs)     podman logs -f "$NAME" ;;
  *)        echo "Usage: $0 [start|stop|restart|recreate|status|logs]"; exit 1 ;;
esac
