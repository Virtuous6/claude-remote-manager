#!/usr/bin/env bash
# Run an allowlisted, read-only Buzz query as Steve.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE_ROOT="${CRM_TEMPLATE_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

cd "${TEMPLATE_ROOT}"
exec python3.11 -m integrations.read_buzz "$@"
