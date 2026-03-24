#!/usr/bin/env bash
# send-message.sh - Send a message to another agent's inbox
# Usage: send-message.sh <to_agent> <priority> '<message text>' [reply_to]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE_ROOT="${CRM_TEMPLATE_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# Resolve CRM_ROOT with instance ID
if [[ -z "${CRM_ROOT:-}" ]]; then
    REPO_ENV="${TEMPLATE_ROOT}/.env"
    if [[ -f "${REPO_ENV}" ]]; then
        CRM_INSTANCE_ID=$(grep '^CRM_INSTANCE_ID=' "${REPO_ENV}" | cut -d= -f2)
    fi
    CRM_INSTANCE_ID="${CRM_INSTANCE_ID:-default}"
    CRM_ROOT="${HOME}/.claude-remote/${CRM_INSTANCE_ID}"
fi

CRM_AGENT_NAME="${CRM_AGENT_NAME:-$(basename "$(pwd)")}"
FROM="${CRM_AGENT_NAME}"

# Validate agent name to prevent injection
if [[ ! "${FROM}" =~ ^[a-z0-9_-]+$ ]]; then
    echo "ERROR: Invalid agent name '${FROM}'" >&2
    exit 1
fi

TO="$1"
PRIORITY="${2:-normal}"
TEXT="${3:-}"
REPLY_TO="${4:-null}"

# Auto-create target inbox if it doesn't exist (same instance)
INBOX_DIR="${CRM_ROOT}/inbox/${TO}"
if [[ ! -d "${INBOX_DIR}" ]]; then
    mkdir -p "${INBOX_DIR}"
fi

# Map priority to sort number
case "${PRIORITY}" in
    urgent) PNUM=0 ;;
    high)   PNUM=1 ;;
    normal) PNUM=2 ;;
    low)    PNUM=3 ;;
    *)      echo "ERROR: Invalid priority '${PRIORITY}'" >&2; exit 1 ;;
esac

# Generate unique filename components
EPOCH_MS=$(python3 -c 'import time; print(int(time.time() * 1000))')
RAND=$(head -c 32 /dev/urandom | LC_ALL=C tr -dc 'a-z0-9' | head -c 5)
MSG_ID="${EPOCH_MS}-${FROM}-${RAND}"
FILENAME="${PNUM}-${EPOCH_MS}-from-${FROM}-${RAND}.json"
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%S.000Z")

# Quote reply_to properly for JSON
[[ "${REPLY_TO}" == "null" ]] && RT_JSON="null" || RT_JSON="\"${REPLY_TO}\""

# Build JSON message
JSON=$(jq -n -c \
    --arg id "${MSG_ID}" \
    --arg from "${FROM}" \
    --arg to "${TO}" \
    --arg priority "${PRIORITY}" \
    --arg ts "${TIMESTAMP}" \
    --arg text "${TEXT}" \
    --argjson reply_to "${RT_JSON}" \
    '{id:$id, from:$from, to:$to, priority:$priority, timestamp:$ts, text:$text, reply_to:$reply_to}')

# Atomic write: temp file then rename
TMP="${INBOX_DIR}/.tmp.${FILENAME}"
FINAL="${INBOX_DIR}/${FILENAME}"

trap 'rm -f "${TMP}"' EXIT

printf '%s\n' "${JSON}" > "${TMP}"
mv "${TMP}" "${FINAL}"

# Auto-ACK the original message when replying
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ "${REPLY_TO}" != "null" ]]; then
    bash "${SCRIPT_DIR}/ack-inbox.sh" "${REPLY_TO}" 2>/dev/null || true
fi

echo "${MSG_ID}"
