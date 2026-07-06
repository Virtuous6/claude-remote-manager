#!/usr/bin/env bash
# sanitize-claude-history.sh - remove escaped lone UTF-16 surrogates from Claude JSONL history.

set -euo pipefail

if [[ $# -eq 0 ]]; then
    echo "Usage: sanitize-claude-history.sh <jsonl-file-or-dir> [...]" >&2
    exit 2
fi

count_bad_surrogates() {
    perl -0ne '
        $n += () = /\\u[dD][89aAbB][0-9a-fA-F]{2}(?!\\u[dD][c-fC-F][0-9a-fA-F]{2})/g;
        $n += () = /(?<!\\u[dD][89aAbB][0-9a-fA-F]{2})\\u[dD][c-fC-F][0-9a-fA-F]{2}/g;
        END { print $n + 0 }
    ' "$1"
}

sanitize_file() {
    perl -0pi -e '
        s/\\u[dD][89aAbB][0-9a-fA-F]{2}(?!\\u[dD][c-fC-F][0-9a-fA-F]{2})/[unicode omitted]/g;
        s/(?<!\\u[dD][89aAbB][0-9a-fA-F]{2})\\u[dD][c-fC-F][0-9a-fA-F]{2}/[unicode omitted]/g;
    ' "$1"
}

TOTAL=0

while IFS= read -r -d '' file; do
    [[ -f "${file}" ]] || continue
    COUNT=$(count_bad_surrogates "${file}")
    [[ "${COUNT}" =~ ^[0-9]+$ ]] || COUNT=0
    if (( COUNT > 0 )); then
        BACKUP="${file}.surrogate-bak-$(date +%Y%m%d%H%M%S)"
        cp -p "${file}" "${BACKUP}"
        sanitize_file "${file}"
        TOTAL=$((TOTAL + COUNT))
        echo "Sanitized ${COUNT} escaped surrogate(s): ${file} (backup: ${BACKUP})"
    fi
done < <(
    for target in "$@"; do
        if [[ -d "${target}" ]]; then
            find "${target}" -type f -name '*.jsonl' -print0
        elif [[ -f "${target}" ]]; then
            printf '%s\0' "${target}"
        fi
    done
)

if (( TOTAL > 0 )); then
    echo "Sanitized ${TOTAL} escaped surrogate(s) total"
fi
