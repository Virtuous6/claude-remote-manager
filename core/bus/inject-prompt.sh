#!/usr/bin/env bash
# inject-prompt.sh - Paste and submit a prompt into a Claude Code tmux pane.

set -euo pipefail

TMUX_SESSION="${1:?tmux session required}"
PROMPT_FILE="${2:?prompt file required}"
LOG_FILE="${3:-/dev/null}"
LABEL="${4:-prompt}"
TIMEOUT="${5:-60}"
PANE="${TMUX_SESSION}:0.0"

log() {
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) inject-prompt ${LABEL}: $1" >> "${LOG_FILE}"
}

if [[ ! -s "${PROMPT_FILE}" ]]; then
    log "ERROR prompt file missing or empty: ${PROMPT_FILE}"
    exit 1
fi

if ! tmux has-session -t "${TMUX_SESSION}" 2>/dev/null; then
    log "ERROR tmux session missing: ${TMUX_SESSION}"
    exit 1
fi

prompt_ready() {
    local pane_text chevron
    pane_text=$(tmux capture-pane -t "${PANE}" -p 2>/dev/null || true)
    chevron=$(printf '\342\235\257')

    printf '%s\n' "${pane_text}" | grep -q "Quick safety check" && return 1
    printf '%s\n' "${pane_text}" | grep -q "Enter to confirm" && return 1
    printf '%s\n' "${pane_text}" | grep -q "${chevron}" && return 0
    printf '%s\n' "${pane_text}" | grep -q "Try 'fix lint errors'" && return 0
    printf '%s\n' "${pane_text}" | grep -q "bypass permissions" && return 0
    return 1
}

elapsed=0
while ! prompt_ready; do
    if (( elapsed >= TIMEOUT )); then
        log "ERROR prompt not ready after ${TIMEOUT}s"
        exit 1
    fi
    sleep 1
    elapsed=$((elapsed + 1))
done

safe_label=$(printf '%s' "${LABEL}" | tr -cs 'A-Za-z0-9_-' '_')
buffer_name="crm-${safe_label}-$$"
byte_count=$(wc -c < "${PROMPT_FILE}" | tr -d '[:space:]')

tmux load-buffer -b "${buffer_name}" "${PROMPT_FILE}"
tmux paste-buffer -t "${PANE}" -b "${buffer_name}"
sleep 0.5
tmux send-keys -t "${PANE}" Enter
tmux delete-buffer -b "${buffer_name}" 2>/dev/null || true

log "injected ${byte_count} bytes after ${elapsed}s"
