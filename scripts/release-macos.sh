#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

"$ROOT/scripts/build-macos.sh"

ARTIFACTS_ENV="$ROOT/build/macos/artifacts.env"
if [[ ! -f "$ARTIFACTS_ENV" ]]; then
  echo "Build metadata file missing: $ARTIFACTS_ENV" >&2
  exit 1
fi

# shellcheck disable=SC1090
source "$ARTIFACTS_ENV"
"$ROOT/scripts/notarize-macos.sh" "$NANOHARNESS_PKG_PATH" "$NANOHARNESS_APP_PATH"
