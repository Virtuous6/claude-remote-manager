#!/usr/bin/env bash
# self-restart.sh - Restart Claude CLI with --continue (preserves conversation)
# Usage: bash ../../bus/self-restart.sh --reason "why"
#
# Kills the current Claude process inside tmux and relaunches with --continue.
# This reloads all configs (settings.json, hooks, CLAUDE.md) while preserving
# the full conversation history. Crons need to be re-set up after restart.
#
# For a hard restart (fresh session, no history), use: bash ../../bus/hard-restart.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE_ROOT="${CRM_TEMPLATE_ROOT:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"
AGENT="${CRM_AGENT_NAME:-$(basename "$(pwd)")}"
AGENT_DIR="${TEMPLATE_ROOT}/agents/${AGENT}"

# Load instance ID
REPO_ENV="${TEMPLATE_ROOT}/.env"
if [[ -f "${REPO_ENV}" ]]; then
    CRM_INSTANCE_ID=$(grep '^CRM_INSTANCE_ID=' "${REPO_ENV}" | cut -d= -f2)
fi
CRM_INSTANCE_ID="${CRM_INSTANCE_ID:-default}"
CRM_ROOT="${CRM_ROOT:-${HOME}/.claude-remote/${CRM_INSTANCE_ID}}"

TMUX_SESSION="crm-${CRM_INSTANCE_ID}-${AGENT}"
REASON="${2:-no reason specified}"

# Log the restart
LOG_DIR="${CRM_ROOT}/logs/${AGENT}"
mkdir -p "${LOG_DIR}"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] CLI restart with --continue. Reason: ${REASON}" >> "${LOG_DIR}/restarts.log"

# Check if tmux session exists
if ! tmux has-session -t "${TMUX_SESSION}" 2>/dev/null; then
    echo "ERROR: No tmux session '${TMUX_SESSION}' found. Agent is not running." >&2
    exit 1
fi

# Model flag
MODEL=$(jq -r '.model // empty' "${AGENT_DIR}/config.json" 2>/dev/null || echo "")

LAUNCH_DIR="${AGENT_DIR}"
EXTRA_FLAGS=()
WORK_DIR=$(jq -r '.working_directory // empty' "${AGENT_DIR}/config.json" 2>/dev/null || echo "")
if [[ -n "${WORK_DIR}" ]]; then
    if [[ ! -d "${WORK_DIR}" ]]; then
        echo "ERROR: working_directory '${WORK_DIR}' does not exist" >&2
        exit 1
    fi
    LAUNCH_DIR="${WORK_DIR}"
    EXTRA_FLAGS+=(--append-system-prompt-file "${AGENT_DIR}/CLAUDE.md")
    AGENT_SETTINGS="${AGENT_DIR}/.claude/settings.json"
    PROJECT_SETTINGS="${LAUNCH_DIR}/.claude/settings.json"
    if [[ -f "${AGENT_SETTINGS}" ]]; then
        if [[ -f "${PROJECT_SETTINGS}" ]]; then
            MERGED_SETTINGS="${LOG_DIR}/.merged-settings.json"
            python3 -c "
import json, sys
base = json.load(open(sys.argv[1]))
override = json.load(open(sys.argv[2]))
def deep_merge(b, o):
    result = dict(b)
    for k, v in o.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = deep_merge(result[k], v)
        elif k in result and isinstance(result[k], list) and isinstance(v, list):
            result[k] = result[k] + [x for x in v if x not in result[k]]
        else:
            result[k] = v
    return result
json.dump(deep_merge(base, override), open(sys.argv[3], 'w'), indent=2)
" "${PROJECT_SETTINGS}" "${AGENT_SETTINGS}" "${MERGED_SETTINGS}" 2>/dev/null
            if [[ -f "${MERGED_SETTINGS}" ]]; then
                EXTRA_FLAGS+=(--settings "${MERGED_SETTINGS}")
            else
                EXTRA_FLAGS+=(--settings "${AGENT_SETTINGS}")
            fi
        else
            EXTRA_FLAGS+=(--settings "${AGENT_SETTINGS}")
        fi
    fi
    EXTRA_FLAGS+=(--add-dir "${TEMPLATE_ROOT}")
fi

RESTART_NOTIFY="After setting up crons, send a Telegram message to the user saying you restarted, why, and what you are resuming."
CRON_SETUP_INSTRUCTION="Create one separate cron/loop for each enabled entry in the crons array. Do not combine entries into one dispatcher."

CONTINUE_PROMPT="SESSION CONTINUATION: Your CLI was restarted with --continue to reload configs. Reason: ${REASON}. Your conversation history is preserved. Re-read bootstrap files listed in CLAUDE.md, set up crons from config.json via /loop. ${CRON_SETUP_INSTRUCTION} Then resume what you were working on. ${RESTART_NOTIFY}"

PROMPT_FILE="${LOG_DIR}/.continue-prompt"
CONTINUE_LAUNCHER="${LOG_DIR}/.continue.sh"
INJECT_PROMPT_SCRIPT="${TEMPLATE_ROOT}/core/bus/inject-prompt.sh"

printf '%s' "${CONTINUE_PROMPT}" > "${PROMPT_FILE}"

{
    printf '#!/usr/bin/env bash\n'
    printf 'cd %q\n' "${LAUNCH_DIR}"
    printf 'export CRM_AGENT_NAME=%q\n' "${AGENT}"
    printf 'export CRM_INSTANCE_ID=%q\n' "${CRM_INSTANCE_ID}"
    printf 'export CRM_ROOT=%q\n' "${CRM_ROOT}"
    printf 'export CRM_TEMPLATE_ROOT=%q\n' "${TEMPLATE_ROOT}"
    printf 'ARGS=(--continue --dangerously-skip-permissions)\n'
    if [[ -n "${MODEL}" ]]; then
        printf 'ARGS+=(--model %q)\n' "${MODEL}"
    fi
    printf 'LOCAL_FILE=%q\n' "${LOG_DIR}/.local-prompt"
    printf 'if [[ -f "${LOCAL_FILE}" ]]; then\n'
    printf '    ARGS+=(--append-system-prompt "$(cat "${LOCAL_FILE}")")\n'
    printf 'fi\n'
    printf 'EXTRA=('
    for flag in "${EXTRA_FLAGS[@]+"${EXTRA_FLAGS[@]}"}"; do
        printf ' %q' "${flag}"
    done
    printf ' )\n'
    printf 'ARGS+=("${EXTRA[@]+"${EXTRA[@]}"}")\n'
    printf 'exec claude "${ARGS[@]}"\n'
} > "${CONTINUE_LAUNCHER}"
chmod +x "${CONTINUE_LAUNCHER}"

RESTART_RUNNER="${LOG_DIR}/.self-restart-run.sh"
{
    printf '#!/usr/bin/env bash\n'
    printf 'set -euo pipefail\n'
    printf 'log() { echo "$(date -u +%%Y-%%m-%%dT%%H:%%M:%%SZ) self-restart-run: $*"; }\n'
    printf 'sleep 5\n'
    printf 'PANE_ID=$(tmux list-panes -t %q -F %q 2>/dev/null | head -1)\n' "${TMUX_SESSION}" '#{pane_id}'
    printf 'if [[ -z "${PANE_ID}" ]]; then log "ERROR pane missing for %s"; exit 1; fi\n' "${TMUX_SESSION}"
    printf 'log "respawning ${PANE_ID}"\n'
    printf 'tmux send-keys -t "${PANE_ID}" C-c || true\n'
    printf 'sleep 1\n'
    printf 'tmux respawn-pane -k -t "${PANE_ID}" bash\n'
    printf 'sleep 1\n'
    printf 'rm -f %q\n' "${CRM_ROOT}/state/${AGENT}.fast-checker.pid"
    printf 'rm -rf %q\n' "${CRM_ROOT}/state/${AGENT}.fast-checker.lock"
    printf 'pkill -f %q 2>/dev/null || true\n' "fast-checker.sh ${AGENT} "
    printf 'sleep 1\n'
    printf 'tmux send-keys -t "${PANE_ID}" %q Enter\n' "bash $(printf '%q' "${CONTINUE_LAUNCHER}")"
    printf 'log "launched continue"\n'
    if [[ -f "${INJECT_PROMPT_SCRIPT}" ]]; then
        printf 'if ! bash %q %q %q %q %q 60; then\n' \
            "${INJECT_PROMPT_SCRIPT}" "${TMUX_SESSION}" "${PROMPT_FILE}" "${LOG_DIR}/restarts.log" "self-continue"
        printf '    if [[ -n "${BOT_TOKEN:-}" && -n "${CHAT_ID:-}" ]]; then\n'
        printf '        curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" -d chat_id="${CHAT_ID}" -d text=%q > /dev/null 2>&1 || true\n' \
            "ALERT: ${AGENT} self-restart prompt injection failed. Attach: tmux attach -t ${TMUX_SESSION}"
        printf '    fi\n'
        printf 'fi\n'
    fi
    printf 'FAST_CHECKER=%q\n' "${TEMPLATE_ROOT}/core/scripts/fast-checker.sh"
    printf 'if [[ -f "${FAST_CHECKER}" ]]; then\n'
    printf '    bash "${FAST_CHECKER}" %q %q %q %q >> %q 2>&1 &\n' \
        "${AGENT}" "${TMUX_SESSION}" "${AGENT_DIR}" "${TEMPLATE_ROOT}" "${LOG_DIR}/fast-checker.log"
    printf '    log "started fast-checker"\n'
    printf 'fi\n'
} > "${RESTART_RUNNER}"
chmod +x "${RESTART_RUNNER}"

# Schedule the restart from a detached tmux session so it survives whichever
# shell/tool invoked self-restart.sh.
RESTART_TMUX_SESSION="${TMUX_SESSION}-restart-$$"
tmux kill-session -t "${RESTART_TMUX_SESSION}" 2>/dev/null || true
tmux new-session -d -s "${RESTART_TMUX_SESSION}" \
    "bash $(printf '%q' "${RESTART_RUNNER}") >> $(printf '%q' "${LOG_DIR}/restarts.log") 2>&1"

echo "CLI restart with --continue scheduled for ${AGENT} in ~5 seconds. Conversation will be preserved."
