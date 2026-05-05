#!/usr/bin/env bash
# Regression tests for launch/restart prompt and settings handling.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

assert_contains() {
    local file="$1"
    local pattern="$2"
    grep -Fq -- "$pattern" "$file" || fail "${file} missing: ${pattern}"
}

if ! command -v tmux >/dev/null 2>&1; then
    echo "SKIP: tmux not installed"
    exit 0
fi

TMP="$(mktemp -d "${TMPDIR:-/tmp}/crm-restart-flow.XXXXXX")"
AGENT="test-agent-$$"
INSTANCE="test-$$"
TEMPLATE_ROOT="${TMP}/template"
CRM_ROOT="${TMP}/crm-root"
PROJECT_DIR="${TMP}/project"
LOG_DIR="${CRM_ROOT}/logs/${AGENT}"
TMUX_SESSION="crm-${INSTANCE}-${AGENT}"

cleanup() {
    tmux kill-session -t "${TMUX_SESSION}" 2>/dev/null || true
    while IFS= read -r session_name; do
        [[ "${session_name}" == "${TMUX_SESSION}-restart-"* ]] || continue
        tmux kill-session -t "${session_name}" 2>/dev/null || true
    done < <(tmux list-sessions -F '#S' 2>/dev/null || true)
    rm -rf "${TMP}"
}
trap cleanup EXIT

mkdir -p \
    "${TEMPLATE_ROOT}/agents/${AGENT}/.claude" \
    "${PROJECT_DIR}/.claude" \
    "${CRM_ROOT}/logs/${AGENT}" \
    "${TMP}/bin"

printf 'CRM_INSTANCE_ID=%s\n' "${INSTANCE}" > "${TEMPLATE_ROOT}/.env"
printf '# Test agent\n' > "${TEMPLATE_ROOT}/agents/${AGENT}/CLAUDE.md"
printf '{"working_directory": "%s"}\n' "${PROJECT_DIR}" > "${TEMPLATE_ROOT}/agents/${AGENT}/config.json"

cat > "${PROJECT_DIR}/.claude/settings.json" <<'JSON'
{
  "permissions": {
    "allow": ["Bash(date)"],
    "deny": ["Read(/private)"]
  },
  "hooks": {
    "PreToolUse": [
      {"matcher": "Bash", "hooks": [{"type": "command", "command": "project-hook"}]}
    ]
  },
  "env": {
    "PROJECT": "yes"
  }
}
JSON

cat > "${TEMPLATE_ROOT}/agents/${AGENT}/.claude/settings.json" <<'JSON'
{
  "permissions": {
    "allow": ["Bash(date)", "Bash(git status)"]
  },
  "hooks": {
    "PreToolUse": [
      {"matcher": "Edit", "hooks": [{"type": "command", "command": "agent-hook"}]}
    ]
  },
  "env": {
    "AGENT": "yes"
  }
}
JSON

cat > "${TMP}/bin/claude" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "${TMP}/claude-args.log"
EOF
chmod +x "${TMP}/bin/claude"

tmux new-session -d -s "${TMUX_SESSION}" "PATH=${TMP}/bin:\$PATH bash"

PATH="${TMP}/bin:${PATH}" \
CRM_TEMPLATE_ROOT="${TEMPLATE_ROOT}" \
CRM_AGENT_NAME="${AGENT}" \
CRM_ROOT="${CRM_ROOT}" \
    bash "${ROOT}/core/bus/self-restart.sh" --reason "Joe's restart"

CONTINUE_LAUNCHER="${LOG_DIR}/.continue.sh"
PROMPT_FILE="${LOG_DIR}/.continue-prompt"
MERGED_SETTINGS="${LOG_DIR}/.merged-settings.json"
RESTART_RUNNER="${LOG_DIR}/.self-restart-run.sh"

[[ -f "${CONTINUE_LAUNCHER}" ]] || fail "continue launcher not generated"
[[ -f "${PROMPT_FILE}" ]] || fail "continue prompt not generated"
[[ -f "${MERGED_SETTINGS}" ]] || fail "merged settings not generated"
[[ -f "${RESTART_RUNNER}" ]] || fail "restart runner not generated"

assert_contains "${CONTINUE_LAUNCHER}" "export CRM_AGENT_NAME=${AGENT}"
assert_contains "${CONTINUE_LAUNCHER}" "export CRM_TEMPLATE_ROOT=${TEMPLATE_ROOT}"
assert_contains "${CONTINUE_LAUNCHER}" "--settings ${MERGED_SETTINGS}"
assert_contains "${CONTINUE_LAUNCHER}" "exec claude \"\${ARGS[@]}\""
assert_contains "${RESTART_RUNNER}" "tmux respawn-pane -k"

if grep -Fq "SESSION CONTINUATION:" "${CONTINUE_LAUNCHER}"; then
    fail "launcher contains positional prompt"
fi
assert_contains "${PROMPT_FILE}" "Joe's restart"

python3 - "${MERGED_SETTINGS}" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1]))
allow = data["permissions"]["allow"]
assert allow.count("Bash(date)") == 1, allow
assert "Bash(git status)" in allow, allow
assert data["permissions"]["deny"] == ["Read(/private)"]
assert data["env"]["PROJECT"] == "yes"
assert data["env"]["AGENT"] == "yes"
commands = [
    hook["command"]
    for entry in data["hooks"]["PreToolUse"]
    for hook in entry["hooks"]
]
assert "project-hook" in commands, commands
assert "agent-hook" in commands, commands
PY

for _ in 1 2 3 4 5 6 7 8 9 10; do
    [[ -f "${TMP}/claude-args.log" ]] && break
    sleep 1
done
[[ -f "${TMP}/claude-args.log" ]] || fail "restart runner did not relaunch claude"
assert_contains "${TMP}/claude-args.log" "--continue --dangerously-skip-permissions"
assert_contains "${TMP}/claude-args.log" "--settings ${MERGED_SETTINGS}"

echo "PASS: restart flow"
