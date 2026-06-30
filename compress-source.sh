#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
  printf 'Usage: %s <output-directory>\n' "$0" >&2
  exit 2
fi

OUTPUT_DIR="$1"
if [ ! -d "$OUTPUT_DIR" ]; then
  printf 'Output directory does not exist: %s\n' "$OUTPUT_DIR" >&2
  exit 2
fi

if ! command -v zip >/dev/null 2>&1; then
  printf 'zip is required. Install it with: sudo apt install zip\n' >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
ZIP_PATH="$(cd "$OUTPUT_DIR" && pwd)/local-ai-assistant-${TIMESTAMP}.zip"

cd "$SCRIPT_DIR"

zip -r "$ZIP_PATH" . \
  -x 'node_modules/*' \
  -x 'dist/*' \
  -x 'build/*' \
  -x '.git/*' \
  -x 'coverage/*' \
  -x '*.log' \
  -x '*.tmp' \
  -x '*.swp' \
  -x '.DS_Store' \
  -x 'config/*.json' \
  -x '*.zip'

printf '%s\n' "$ZIP_PATH"
