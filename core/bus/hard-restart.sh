#!/usr/bin/env bash
# hard-restart.sh - Kill and relaunch an agent (new session, no conversation history)
# Usage: bash ../../bus/hard-restart.sh --reason "why"
#
# Use this when the session is corrupted, context is exhausted, or you
# need a truly fresh start. For normal restarts, use self-restart.sh instead.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
TEMPLATE_ROOT="${CRM_TEMPLATE_ROOT:-$(cd "${SCRIPT_DIR}/../.." && pwd -P)}"
AGENT="${CRM_AGENT_NAME:-$(basename "$(pwd)")}"
AGENT_DIR="${CRM_AGENT_DIR:-${TEMPLATE_ROOT}/agents/${AGENT}}"
AGENT_DIR="$(cd "${AGENT_DIR}" && pwd -P)"

# Load instance ID
REPO_ENV="${TEMPLATE_ROOT}/.env"
if [[ -f "${REPO_ENV}" ]]; then
    CRM_INSTANCE_ID=$(grep '^CRM_INSTANCE_ID=' "${REPO_ENV}" | cut -d= -f2)
fi
CRM_INSTANCE_ID="${CRM_INSTANCE_ID:-default}"
CRM_ROOT="${CRM_ROOT:-${HOME}/.claude-remote/${CRM_INSTANCE_ID}}"

PLIST="${HOME}/Library/LaunchAgents/com.claude-remote.${CRM_INSTANCE_ID}.${AGENT}.plist"
REASON="${2:-no reason specified}"

if [[ ! -f "${PLIST}" ]]; then
    echo "ERROR: No launchd plist found for ${AGENT} at ${PLIST}" >&2
    exit 1
fi

# Log the restart
LOG_DIR="${CRM_ROOT}/logs/${AGENT}"
mkdir -p "${LOG_DIR}"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Hard-restart triggered. Reason: ${REASON}" >> "${LOG_DIR}/restarts.log"

# Reset crash counter so launchd doesn't throttle
rm -f "${LOG_DIR}/.crash_count_today"

# Factory agents own an exact Claude session ID. A fresh launch cannot reuse an
# ID that already has history, so rotate it atomically before setting the marker.
CONFIG_FILE="${AGENT_DIR}/config.json"
CLAUDE_SESSION_ID=$(jq -r '.claude_session_id // empty' "${CONFIG_FILE}" 2>/dev/null || echo "")
if [[ -n "${CLAUDE_SESSION_ID}" ]]; then
    if [[ ! "${CLAUDE_SESSION_ID}" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
        echo "ERROR: invalid claude_session_id" >&2
        exit 1
    fi
    NEW_SESSION_ID=$(uuidgen | tr 'A-F' 'a-f')
    CONFIG_TMP=$(mktemp "${CONFIG_FILE}.XXXXXX")
    jq --arg session "${NEW_SESSION_ID}" '.claude_session_id = $session' \
        "${CONFIG_FILE}" > "${CONFIG_TMP}"
    chmod 600 "${CONFIG_TMP}"
    mv "${CONFIG_TMP}" "${CONFIG_FILE}"
fi

# Write force-fresh marker so agent-wrapper.sh uses STARTUP_PROMPT (no --continue)
mkdir -p "${CRM_ROOT}/state"
touch "${CRM_ROOT}/state/${AGENT}.force-fresh"

# Clear context tracking state so new session starts fresh
rm -f "${CRM_ROOT}/state/${AGENT}.session-start"

# Detach so the current Claude/tool call can exit before launchd kills it.
USER_ID=$(id -u)
LABEL="com.claude-remote.${CRM_INSTANCE_ID}.${AGENT}"
RESTART_CMD="sleep 5; if launchctl print 'gui/${USER_ID}/${LABEL}' >/dev/null 2>&1; then launchctl kickstart -k 'gui/${USER_ID}/${LABEL}'; else launchctl unload '${PLIST}' 2>/dev/null; sleep 1; launchctl load '${PLIST}'; fi"
nohup bash -c "${RESTART_CMD}" \
    >> "${LOG_DIR}/restarts.log" 2>&1 &
disown

echo "Hard-restart scheduled for ${AGENT} in ~5 seconds. New session will start fresh."
