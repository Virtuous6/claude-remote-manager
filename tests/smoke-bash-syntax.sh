#!/usr/bin/env bash
# Smoke-check core scripts against macOS bash 3.2.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS=(
    "${ROOT}/core/bus/"*.sh
    "${ROOT}/core/scripts/"*.sh
    "${ROOT}/scripts/"*.sh
)

FAIL=0
for script in "${SCRIPTS[@]}"; do
    if /bin/bash -n "${script}" 2>/dev/null; then
        echo "OK bash3 parse: $(basename "${script}")"
    else
        echo "FAIL bash3 parse: ${script}" >&2
        /bin/bash -n "${script}" || true
        FAIL=1
    fi

    BAD=$(grep -nE '\$\{[A-Za-z_0-9]+\^\^?\}|\$\{[A-Za-z_0-9]+,,?\}|^[[:space:]]*mapfile\b|^[[:space:]]*readarray\b|^[[:space:]]*declare[[:space:]]+-[a-zA-Z]*[Agn][a-zA-Z]*\b|^[[:space:]]*local[[:space:]]+-n\b|^[[:space:]]*typeset[[:space:]]+-n\b' "${script}" || true)
    if [[ -n "${BAD}" ]]; then
        echo "FAIL bash4 idiom: ${script}" >&2
        echo "${BAD}" >&2
        FAIL=1
    else
        echo "OK bash4 grep: $(basename "${script}")"
    fi
done

if (( FAIL )); then
    echo "FAIL: bash syntax smoke"
    exit 1
fi

echo "PASS: bash syntax smoke"
