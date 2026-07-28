#!/usr/bin/env bash
# Send a correlated CRM reply with optional core and typed Buzz workflow changes.
# Usage: send-acp-reply.sh <adapter> <reply_to> '<text>' [core_file]
#        send-acp-reply.sh <adapter> <reply_to> '<text>' [--core file] [--workflow file]

set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE_ROOT="${CRM_TEMPLATE_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

if [[ -z "${CRM_ROOT:-}" ]]; then
    REPO_ENV="${TEMPLATE_ROOT}/.env"
    if [[ -f "${REPO_ENV}" ]]; then
        CRM_INSTANCE_ID=$(grep '^CRM_INSTANCE_ID=' "${REPO_ENV}" | cut -d= -f2)
    fi
    CRM_INSTANCE_ID="${CRM_INSTANCE_ID:-default}"
    CRM_ROOT="${HOME}/.claude-remote/${CRM_INSTANCE_ID}"
fi

FROM="${CRM_AGENT_NAME:-$(basename "$(pwd)")}"
TO="${1:-}"
REPLY_TO="${2:-}"
TEXT="${3:-}"
CORE_FILE=""
WORKFLOW_FILE=""
shift "$(( $# < 3 ? $# : 3 ))"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --core)
            [[ $# -ge 2 && -z "${CORE_FILE}" ]] || {
                echo "ERROR: Invalid core option" >&2
                exit 1
            }
            CORE_FILE="$2"
            shift 2
            ;;
        --workflow)
            [[ $# -ge 2 && -z "${WORKFLOW_FILE}" ]] || {
                echo "ERROR: Invalid workflow option" >&2
                exit 1
            }
            WORKFLOW_FILE="$2"
            shift 2
            ;;
        *)
            if [[ -z "${CORE_FILE}" ]]; then
                CORE_FILE="$1"
                shift
            else
                echo "ERROR: Unexpected ACP reply argument" >&2
                exit 1
            fi
            ;;
    esac
done

if [[ ! "${FROM}" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]]; then
    echo "ERROR: Invalid CRM agent name" >&2
    exit 1
fi
if [[ ! "${TO}" =~ ^buzz-acp(-[a-z0-9][a-z0-9-]{0,62})?$ ]]; then
    echo "ERROR: Invalid ACP adapter" >&2
    exit 1
fi
if [[ ! "${REPLY_TO}" =~ ^acp-[0-9a-f]{32}$ ]]; then
    echo "ERROR: Invalid ACP turn ID" >&2
    exit 1
fi
if [[ -z "${TEXT//[[:space:]]/}" ]]; then
    echo "ERROR: Empty ACP reply" >&2
    exit 1
fi

EPOCH_MS=$(python3 -c 'import time; print(int(time.time() * 1000))')
RAND=$(head -c 32 /dev/urandom | LC_ALL=C tr -dc 'a-z0-9' | head -c 5)
MSG_ID="${EPOCH_MS}-${FROM}-${RAND}"
FILENAME="1-${EPOCH_MS}-from-${FROM}-${RAND}.json"
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%S.000Z")

JQ_ARGS=(
    -n -c
    --arg id "${MSG_ID}"
    --arg from "${FROM}"
    --arg to "${TO}"
    --arg ts "${TIMESTAMP}"
    --arg text "${TEXT}"
    --arg reply_to "${REPLY_TO}"
)
JQ_FILTER='{id:$id,from:$from,to:$to,priority:"high",timestamp:$ts,text:$text,reply_to:$reply_to}'

if [[ -n "${CORE_FILE}" ]]; then
    EXPECTED_CORE_FILE="${CRM_ROOT}/state/${FROM}-core.md"
    if [[ "${CORE_FILE}" != "${EXPECTED_CORE_FILE}" ]]; then
        echo "ERROR: Core file must use the isolated agent state path" >&2
        exit 1
    fi
    if [[ ! -f "${CORE_FILE}" || -L "${CORE_FILE}" ]]; then
        echo "ERROR: Core path must be a regular non-symlink file" >&2
        exit 1
    fi
    CORE_BYTES=$(wc -c < "${CORE_FILE}" | tr -d ' ')
    if [[ "${CORE_BYTES}" -eq 0 || "${CORE_BYTES}" -gt 16384 ]]; then
        echo "ERROR: Core file must be 1-16384 bytes" >&2
        exit 1
    fi
    JQ_ARGS+=(--rawfile core "${CORE_FILE}")
    JQ_FILTER+=' | .buzz_memory_updates=[{slug:"core",value:$core}]'
fi

if [[ -n "${WORKFLOW_FILE}" ]]; then
    EXPECTED_WORKFLOW_FILE="${CRM_ROOT}/state/${FROM}-workflow-op.json"
    if [[ "${WORKFLOW_FILE}" != "${EXPECTED_WORKFLOW_FILE}" ]]; then
        echo "ERROR: Workflow file must use the isolated agent state path" >&2
        exit 1
    fi
    if [[ ! -f "${WORKFLOW_FILE}" || -L "${WORKFLOW_FILE}" ]]; then
        echo "ERROR: Workflow path must be a regular non-symlink file" >&2
        exit 1
    fi
    WORKFLOW_BYTES=$(wc -c < "${WORKFLOW_FILE}" | tr -d ' ')
    if [[ "${WORKFLOW_BYTES}" -eq 0 || "${WORKFLOW_BYTES}" -gt 16384 ]]; then
        echo "ERROR: Workflow file must be 1-16384 bytes" >&2
        exit 1
    fi
    if ! jq -e 'type == "object"' "${WORKFLOW_FILE}" >/dev/null; then
        echo "ERROR: Workflow operation must be one JSON object" >&2
        exit 1
    fi
    JQ_ARGS+=(--slurpfile workflow "${WORKFLOW_FILE}")
    JQ_FILTER+=' | .buzz_workflow_operation=$workflow[0]'
fi

INBOX_DIR="${CRM_ROOT}/inbox/${TO}"
mkdir -p "${INBOX_DIR}"
chmod 700 "${INBOX_DIR}"

JSON=$(jq "${JQ_ARGS[@]}" "${JQ_FILTER}")
TMP="${INBOX_DIR}/.tmp.${FILENAME}"
FINAL="${INBOX_DIR}/${FILENAME}"
trap 'rm -f "${TMP}"' EXIT
printf '%s\n' "${JSON}" > "${TMP}"
mv "${TMP}" "${FINAL}"
if [[ -n "${WORKFLOW_FILE}" ]]; then
    rm -f "${WORKFLOW_FILE}"
fi

bash "${SCRIPT_DIR}/ack-inbox.sh" "${REPLY_TO}" 2>/dev/null || true
echo "${MSG_ID}"
