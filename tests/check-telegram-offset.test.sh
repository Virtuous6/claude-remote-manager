#!/usr/bin/env bash
# Regression tests for Telegram offset handling.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

TMP="$(mktemp -d "${TMPDIR:-/tmp}/crm-check-telegram.XXXXXX")"
cleanup() {
    rm -rf "${TMP}"
}
trap cleanup EXIT

mkdir -p "${TMP}/bin" "${TMP}/crm/state"

cat > "${TMP}/bin/curl" <<'SH'
#!/usr/bin/env bash
cat "${FAKE_TG_RESPONSE}"
SH
chmod +x "${TMP}/bin/curl"

export PATH="${TMP}/bin:${PATH}"
export CRM_ROOT="${TMP}/crm"
export CRM_TEMPLATE_ROOT="${TMP}/template"
export CRM_AGENT_NAME="test-agent"
export BOT_TOKEN="fake-token"
export ALLOWED_USER="123"

mkdir -p "${CRM_TEMPLATE_ROOT}/agents/${CRM_AGENT_NAME}"
cat > "${CRM_TEMPLATE_ROOT}/agents/${CRM_AGENT_NAME}/.env" <<'ENV'
BOT_TOKEN=fake-token
ALLOWED_USER=123
ENV

cat > "${TMP}/empty.json" <<'JSON'
{"ok":true,"result":[]}
JSON
export FAKE_TG_RESPONSE="${TMP}/empty.json"

EMPTY_OUTPUT=$(bash "${ROOT}/core/bus/check-telegram.sh")
[[ -z "${EMPTY_OUTPUT}" ]] || fail "empty result produced output"
[[ ! -f "${CRM_ROOT}/state/.telegram-offset-${CRM_AGENT_NAME}" ]] || fail "empty result wrote offset"

cat > "${TMP}/message.json" <<'JSON'
{
  "ok": true,
  "result": [
    {
      "update_id": 41,
      "message": {
        "chat": {"id": 999},
        "from": {"id": 123, "first_name": "Joe"},
        "text": "hi",
        "reply_to_message": {"text": "prev"},
        "date": 1778060000
      }
    }
  ]
}
JSON
export FAKE_TG_RESPONSE="${TMP}/message.json"

OFFSET_DEFER_FILE="${TMP}/offset-defer"
MESSAGE_OUTPUT=$(CRM_DEFER_TELEGRAM_OFFSET_FILE="${OFFSET_DEFER_FILE}" bash "${ROOT}/core/bus/check-telegram.sh")

printf '%s\n' "${MESSAGE_OUTPUT}" | jq -e 'select(.text == "hi" and .reply_to_text == "prev" and .type == "message")' >/dev/null || fail "message output missing"
grep -Fxq "__OFFSET__:42" "${OFFSET_DEFER_FILE}" || fail "deferred offset missing"
[[ ! -f "${CRM_ROOT}/state/.telegram-offset-${CRM_AGENT_NAME}" ]] || fail "deferred path wrote offset directly"

unset CRM_DEFER_TELEGRAM_OFFSET_FILE
STANDALONE_OUTPUT=$(bash "${ROOT}/core/bus/check-telegram.sh")
printf '%s\n' "${STANDALONE_OUTPUT}" | jq -e 'select(.text == "hi" and .reply_to_text == "prev" and .type == "message")' >/dev/null || fail "standalone output missing"
grep -Fxq "42" "${CRM_ROOT}/state/.telegram-offset-${CRM_AGENT_NAME}" || fail "standalone offset missing"

echo "PASS: check telegram offset"
