#!/usr/bin/env bash
# Send a proactive message from Steve to Joe's fixed Buzz DM.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE_ROOT="${CRM_TEMPLATE_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
MESSAGE="${1:-}"

if [[ -z "${MESSAGE//[[:space:]]/}" ]]; then
    echo "Usage: send-buzz.sh '<message>'" >&2
    exit 1
fi

cd "${TEMPLATE_ROOT}"
exec python3.11 -m integrations.send_buzz "${MESSAGE}"
