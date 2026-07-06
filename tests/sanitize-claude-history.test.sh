#!/usr/bin/env bash
# Regression test for Claude history surrogate sanitizer.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/crm-sanitize-history.XXXXXX")"

cleanup() {
    rm -rf "${TMP}"
}
trap cleanup EXIT

HISTORY="${TMP}/session.jsonl"
cat > "${HISTORY}" <<'JSONL'
{"message":{"content":"bad high \ud83d... tail"}}
{"message":{"content":"valid pair \uD83E\uDEC2 stays"}}
{"message":{"content":"bad low \uDE00 tail"}}
JSONL

bash "${ROOT}/core/scripts/sanitize-claude-history.sh" "${TMP}" > "${TMP}/out"

grep -Fq "[unicode omitted]... tail" "${HISTORY}"
grep -Fq "valid pair \uD83E\uDEC2 stays" "${HISTORY}"
grep -Fq "bad low [unicode omitted] tail" "${HISTORY}"
grep -Fq "Sanitized 2 escaped surrogate(s)" "${TMP}/out"
ls "${HISTORY}".surrogate-bak-* >/dev/null

echo "PASS: sanitize claude history"
