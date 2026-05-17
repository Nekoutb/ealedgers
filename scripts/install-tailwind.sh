#!/usr/bin/env bash
# scripts/install-tailwind.sh — download the Tailwind v4 standalone CLI.
#
# Idempotent: only downloads if the binary isn't already present.
# Detects Windows (Git Bash / MSYS) vs Linux vs macOS automatically.

set -euo pipefail

VERSION="v4.3.0"
RELEASE_URL_BASE="https://github.com/tailwindlabs/tailwindcss/releases/download/${VERSION}"

case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*)
        ASSET="tailwindcss-windows-x64.exe"
        OUTPUT="scripts/tailwindcss.exe"
        ;;
    Linux*)
        case "$(uname -m)" in
            x86_64) ASSET="tailwindcss-linux-x64" ;;
            aarch64|arm64) ASSET="tailwindcss-linux-arm64" ;;
            *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
        esac
        OUTPUT="scripts/tailwindcss"
        ;;
    Darwin*)
        case "$(uname -m)" in
            x86_64) ASSET="tailwindcss-macos-x64" ;;
            arm64) ASSET="tailwindcss-macos-arm64" ;;
            *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
        esac
        OUTPUT="scripts/tailwindcss"
        ;;
    *)
        echo "Unsupported OS: $(uname -s)" >&2
        exit 1
        ;;
esac

if [ -x "$OUTPUT" ]; then
    echo "Tailwind already installed at $OUTPUT"
    "$OUTPUT" --help 2>&1 | head -1
    exit 0
fi

mkdir -p scripts
URL="${RELEASE_URL_BASE}/${ASSET}"
echo "Downloading $URL ..."
curl -sL --max-time 180 -o "$OUTPUT" "$URL"
chmod +x "$OUTPUT"
echo "Installed $OUTPUT"
"$OUTPUT" --help 2>&1 | head -1
