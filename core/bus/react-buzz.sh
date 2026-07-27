#!/usr/bin/env bash
# React as Steve to an accessible Buzz event.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE_ROOT="${CRM_TEMPLATE_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
EVENT="${1:-}"
EMOJI="${2:-}"

if [[ ! "${EVENT}" =~ ^[0-9a-f]{64}$ || -z "${EMOJI}" ]]; then
    echo "Usage: react-buzz.sh <64-char-event-id> '<emoji>'" >&2
    exit 1
fi

cd "${TEMPLATE_ROOT}"
exec python3.11 -m integrations.react_buzz "${EVENT}" "${EMOJI}"
