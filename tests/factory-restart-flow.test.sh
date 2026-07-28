#!/usr/bin/env bash
# Verify factory agents resume an exact Claude session from an external agent dir.

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

TMP="$(mktemp -d "${TMPDIR:-/tmp}/crm-factory-restart.XXXXXX")"
AGENT="buzz-test-$$"
INSTANCE="test-$$"
SESSION_ID="12345678-1234-4234-8234-123456789abc"
TEMPLATE_ROOT="${TMP}/template"
CRM_ROOT="${TMP}/crm-root"
AGENT_DIR="${CRM_ROOT}/factory/agents/${AGENT}"
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

mkdir -p "${TEMPLATE_ROOT}" "${AGENT_DIR}" "${LOG_DIR}" "${TMP}/bin"
AGENT_DIR="$(cd "${AGENT_DIR}" && pwd -P)"
printf 'CRM_INSTANCE_ID=%s\n' "${INSTANCE}" > "${TEMPLATE_ROOT}/.env"
printf '# Factory test\n' > "${AGENT_DIR}/CLAUDE.md"
printf '{"claude_session_id":"%s"}\n' "${SESSION_ID}" > "${AGENT_DIR}/config.json"
printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >> "%s"\n' \
    "${TMP}/claude-args.log" > "${TMP}/bin/claude"
chmod +x "${TMP}/bin/claude"

tmux new-session -d -s "${TMUX_SESSION}" "PATH=${TMP}/bin:\$PATH bash"

PATH="${TMP}/bin:${PATH}" \
CRM_TEMPLATE_ROOT="${TEMPLATE_ROOT}" \
CRM_AGENT_NAME="${AGENT}" \
CRM_AGENT_DIR="${AGENT_DIR}" \
CRM_ROOT="${CRM_ROOT}" \
    bash "${ROOT}/core/bus/self-restart.sh" --reason "factory test"

CONTINUE_LAUNCHER="${LOG_DIR}/.continue.sh"
[[ -f "${CONTINUE_LAUNCHER}" ]] || fail "continue launcher not generated"
assert_contains "${CONTINUE_LAUNCHER}" "export CRM_AGENT_DIR=${AGENT_DIR}"
assert_contains "${CONTINUE_LAUNCHER}" "--resume ${SESSION_ID} --dangerously-skip-permissions"

for _ in 1 2 3 4 5 6 7 8 9 10; do
    [[ -f "${TMP}/claude-args.log" ]] && break
    sleep 1
done
[[ -f "${TMP}/claude-args.log" ]] || fail "restart runner did not relaunch claude"
assert_contains "${TMP}/claude-args.log" "--resume ${SESSION_ID} --dangerously-skip-permissions"

echo "PASS: factory restart flow"
