#!/usr/bin/env bash
# Send an approved file from Steve to Joe's fixed Buzz DM.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE_ROOT="${CRM_TEMPLATE_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
FILE="${1:-}"
MESSAGE="${2:-File from Steve}"

if [[ -z "${FILE}" ]]; then
    echo "Usage: send-buzz-file.sh <approved-file> ['message']" >&2
    exit 1
fi

cd "${TEMPLATE_ROOT}"
exec python3.11 -m integrations.send_buzz_file "${FILE}" "${MESSAGE}"
