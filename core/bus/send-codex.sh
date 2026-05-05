#!/usr/bin/env bash
# send-codex.sh - Hand a task off to OpenAI Codex via codex exec (headless mode)
#
# Usage:
#   send-codex.sh "<prompt>"                          # one-shot prompt
#   echo "<prompt>" | send-codex.sh -                 # prompt via stdin
#   send-codex.sh --cwd /path/to/repo "<prompt>"      # set working dir for codex
#   send-codex.sh --model o3 "<prompt>"               # override model
#   send-codex.sh --json "<prompt>"                   # emit JSONL events to stdout
#   send-codex.sh --review                            # `codex exec review` against current repo
#
# Auth: uses local codex login (ChatGPT or OPENAI_API_KEY). Run `codex login status` to verify.
# Sandbox: defaults to read-only. Pass --workspace-write or --full-auto to allow writes.
# Logs: appends invocation metadata to ~/.claude-remote/default/logs/<agent>/codex.log
#
# Returns: stdout = codex's final message (or full JSONL if --json). Exit 0 on success.

set -euo pipefail

# Defaults
PROMPT=""
CWD=""
MODEL=""
SANDBOX="read-only"
JSON=0
REVIEW=0
EXTRA_ARGS=()

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --cwd|-C)
            CWD="${2:-}"
            shift 2
            ;;
        --model|-m)
            MODEL="${2:-}"
            shift 2
            ;;
        --workspace-write)
            SANDBOX="workspace-write"
            shift
            ;;
        --full-auto)
            SANDBOX=""
            EXTRA_ARGS+=(--full-auto)
            shift
            ;;
        --json)
            JSON=1
            shift
            ;;
        --review)
            REVIEW=1
            shift
            ;;
        --skip-git-repo-check)
            EXTRA_ARGS+=(--skip-git-repo-check)
            shift
            ;;
        -)
            PROMPT=$(cat)
            shift
            ;;
        --help|-h)
            sed -n '2,17p' "$0"
            exit 0
            ;;
        *)
            if [[ -z "${PROMPT}" ]]; then
                PROMPT="$1"
            else
                PROMPT="${PROMPT}"$'\n'"$1"
            fi
            shift
            ;;
    esac
done

# Validate
if [[ ${REVIEW} -eq 0 && -z "${PROMPT}" ]]; then
    echo "ERROR: no prompt provided. Pass as arg, via stdin with -, or use --review." >&2
    exit 2
fi

if ! command -v codex >/dev/null 2>&1; then
    echo "ERROR: codex CLI not found. Install with: npm install -g @openai/codex" >&2
    exit 127
fi

# Build command
CMD=(codex exec)
[[ ${REVIEW} -eq 1 ]] && CMD+=(review)
[[ -n "${CWD}" ]] && CMD+=(--cd "${CWD}")
[[ -n "${MODEL}" ]] && CMD+=(--model "${MODEL}")
[[ -n "${SANDBOX}" ]] && CMD+=(--sandbox "${SANDBOX}")
[[ ${JSON} -eq 1 ]] && CMD+=(--json)
[[ ${#EXTRA_ARGS[@]} -gt 0 ]] && CMD+=("${EXTRA_ARGS[@]}")

# Capture last message to a temp file so we can return clean text even when codex
# emits progress noise to stdout. JSON mode bypasses this and returns raw stream.
LAST_MSG=""
if [[ ${JSON} -eq 0 ]]; then
    LAST_MSG=$(mktemp -t codex-last-msg.XXXXXX)
    trap 'rm -f "${LAST_MSG}"' EXIT
    CMD+=(-o "${LAST_MSG}")
fi

# Log invocation
AGENT="${CRM_AGENT_NAME:-$(basename "$(pwd)")}"
LOG_DIR="${HOME}/.claude-remote/default/logs/${AGENT}"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/codex.log"
{
    echo "---"
    echo "ts: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "agent: ${AGENT}"
    echo "cwd: ${CWD:-$(pwd)}"
    echo "model: ${MODEL:-default}"
    echo "sandbox: ${SANDBOX:-(full-auto)}"
    echo "review: ${REVIEW}"
    echo "json: ${JSON}"
    echo "prompt_first_120: ${PROMPT:0:120}"
} >> "${LOG_FILE}"

# Run codex. Pipe prompt via stdin when not in review mode.
if [[ ${REVIEW} -eq 1 ]]; then
    "${CMD[@]}" </dev/null
    EXIT_CODE=$?
elif [[ ${JSON} -eq 1 ]]; then
    printf '%s' "${PROMPT}" | "${CMD[@]}" -
    EXIT_CODE=$?
else
    printf '%s' "${PROMPT}" | "${CMD[@]}" - >/dev/null
    EXIT_CODE=$?
    if [[ ${EXIT_CODE} -eq 0 && -s "${LAST_MSG}" ]]; then
        cat "${LAST_MSG}"
    fi
fi

echo "exit: ${EXIT_CODE}" >> "${LOG_FILE}"
exit ${EXIT_CODE}
