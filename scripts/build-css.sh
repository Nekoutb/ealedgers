#!/usr/bin/env bash
# scripts/build-css.sh — build the Tailwind output CSS.
#
# Usage:
#   scripts/build-css.sh           # one-shot minified build (for commits / deploys)
#   scripts/build-css.sh --watch   # watch mode for local dev (rebuild on save)

set -euo pipefail

BIN=""
if [ -x "scripts/tailwindcss" ]; then
    BIN="scripts/tailwindcss"
elif [ -x "scripts/tailwindcss.exe" ]; then
    BIN="scripts/tailwindcss.exe"
fi

if [ -z "$BIN" ]; then
    echo "Tailwind binary not found. Run: scripts/install-tailwind.sh" >&2
    exit 1
fi

INPUT="static_src/main.css"
OUTPUT="accounting/static/accounting/main.css"
mkdir -p "$(dirname "$OUTPUT")"

EXTRA_ARGS=()
case "${1:-}" in
    --watch|-w)
        EXTRA_ARGS+=("--watch")
        ;;
    *)
        EXTRA_ARGS+=("--minify")
        ;;
esac

echo "Building $INPUT -> $OUTPUT ${EXTRA_ARGS[*]}"
"$BIN" -i "$INPUT" -o "$OUTPUT" "${EXTRA_ARGS[@]}"
