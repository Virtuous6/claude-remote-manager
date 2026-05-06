#!/usr/bin/env bash
# hard-restart.sh - Kill and relaunch an agent (new session, no conversation history)
# Usage: bash ../../bus/hard-restart.sh --reason "why"
#
# Use this when the session is corrupted, context is exhausted, or you
# need a truly fresh start. For normal restarts, use self-restart.sh instead.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE_ROOT="${CRM_TEMPLATE_ROOT:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"
AGENT="${CRM_AGENT_NAME:-$(basename "$(pwd)")}"

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

# Write force-fresh marker so agent-wrapper.sh uses STARTUP_PROMPT (no --continue)
mkdir -p "${CRM_ROOT}/state"
touch "${CRM_ROOT}/state/${AGENT}.force-fresh"

# Detach so the current Claude/tool call can exit before launchd kills it.
USER_ID=$(id -u)
LABEL="com.claude-remote.${CRM_INSTANCE_ID}.${AGENT}"
RESTART_CMD="sleep 5; if launchctl print 'gui/${USER_ID}/${LABEL}' >/dev/null 2>&1; then launchctl kickstart -k 'gui/${USER_ID}/${LABEL}'; else launchctl unload '${PLIST}' 2>/dev/null; sleep 1; launchctl load '${PLIST}'; fi"
nohup bash -c "${RESTART_CMD}" \
    >> "${LOG_DIR}/restarts.log" 2>&1 &
disown

echo "Hard-restart scheduled for ${AGENT} in ~5 seconds. New session will start fresh."
