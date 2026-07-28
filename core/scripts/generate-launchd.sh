#!/usr/bin/env bash
# generate-launchd.sh - Generate and load a launchd plist for an agent
# Usage: generate-launchd.sh <agent_name> [<agent_dir>]

set -euo pipefail

AGENT="$1"
if [[ ! "${AGENT}" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]]; then
    echo "ERROR: invalid agent name" >&2
    exit 1
fi
TEMPLATE_ROOT="$(cd "$(dirname "$0")/../.." && pwd -P)"
AGENT_DIR="${2:-${TEMPLATE_ROOT}/agents/${AGENT}}"
AGENT_DIR="$(cd "${AGENT_DIR}" && pwd -P)"
CONFIG_FILE="${AGENT_DIR}/config.json"

# Load instance ID from repo .env
ENV_FILE="${TEMPLATE_ROOT}/.env"
if [[ -f "${ENV_FILE}" ]]; then
    CRM_INSTANCE_ID=$(grep '^CRM_INSTANCE_ID=' "${ENV_FILE}" | cut -d= -f2)
fi
CRM_INSTANCE_ID="${CRM_INSTANCE_ID:-default}"

PLIST_DIR="${HOME}/Library/LaunchAgents"
PLIST_NAME="com.claude-remote.${CRM_INSTANCE_ID}.${AGENT}"
PLIST_FILE="${PLIST_DIR}/${PLIST_NAME}.plist"
CRM_ROOT="${HOME}/.claude-remote/${CRM_INSTANCE_ID}"
LOG_DIR="${CRM_ROOT}/logs/${AGENT}"
WRAPPER="${TEMPLATE_ROOT}/core/scripts/agent-wrapper.sh"

mkdir -p "${PLIST_DIR}" "${LOG_DIR}"
chmod 700 "${LOG_DIR}"

# Auto-detect PATH: find where claude, jq, and python3 live
CLAUDE_BIN=$(which claude 2>/dev/null || echo "")
if [[ -z "${CLAUDE_BIN}" ]]; then
    echo "ERROR: 'claude' not found in PATH. Install Claude Code CLI first." >&2
    exit 1
fi
CLAUDE_DIR=$(dirname "${CLAUDE_BIN}")

# Detect the active Node.js version (the one that will actually work with claude)
NODE_BIN=$(which node 2>/dev/null || echo "")
if [[ -z "${NODE_BIN}" ]]; then
    echo "ERROR: 'node' not found in PATH. Install Node.js first." >&2
    exit 1
fi
NODE_DIR=$(dirname "${NODE_BIN}")

# Build PATH with only the active node version + detected dirs + standard system dirs
# IMPORTANT: Do NOT glob all nvm/fnm versions — only the currently active one.
# Including multiple node versions causes unpredictable resolution and crashes
# when an incompatible version is picked up first.
LAUNCHD_PATH="${NODE_DIR}:${CLAUDE_DIR}:/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin"

# Include pyenv shims if present
[[ -d "${HOME}/.pyenv/shims" ]] && LAUNCHD_PATH="${HOME}/.pyenv/shims:${LAUNCHD_PATH}"

# Generate plist
cat > "${PLIST_FILE}" <<ENDPLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_NAME}</string>

    <key>ProgramArguments</key>
    <array>
        <string>${WRAPPER}</string>
        <string>${AGENT}</string>
        <string>${TEMPLATE_ROOT}</string>
        <string>${AGENT_DIR}</string>
    </array>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>${LOG_DIR}/stdout.log</string>

    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/stderr.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${LAUNCHD_PATH}</string>
        <key>HOME</key>
        <string>${HOME}</string>
        <key>CRM_AGENT_NAME</key>
        <string>${AGENT}</string>
        <key>CRM_INSTANCE_ID</key>
        <string>${CRM_INSTANCE_ID}</string>
        <key>CRM_ROOT</key>
        <string>${CRM_ROOT}</string>
        <key>CRM_TEMPLATE_ROOT</key>
        <string>${TEMPLATE_ROOT}</string>
        <key>CRM_AGENT_DIR</key>
        <string>${AGENT_DIR}</string>
    </dict>

    <key>WorkingDirectory</key>
    <string>${AGENT_DIR}</string>

    <key>ThrottleInterval</key>
    <integer>10</integer>
</dict>
</plist>
ENDPLIST

echo "Generated: ${PLIST_FILE}"

# Load the plist
launchctl unload "${PLIST_FILE}" 2>/dev/null || true
launchctl load "${PLIST_FILE}"

echo "Loaded: ${PLIST_NAME}"
