#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

"$ROOT/scripts/build-macos.sh"

ARTIFACTS_LIST="$ROOT/build/macos/artifacts.list"
if [[ ! -f "$ARTIFACTS_LIST" ]]; then
  echo "Build metadata list missing: $ARTIFACTS_LIST" >&2
  exit 1
fi

NOTARIZED=0
while IFS= read -r artifacts_env; do
  if [[ -z "$artifacts_env" ]]; then
    continue
  fi
  if [[ ! -f "$artifacts_env" ]]; then
    echo "Per-architecture metadata file missing: $artifacts_env" >&2
    exit 1
  fi
  # shellcheck disable=SC1090
  source "$artifacts_env"
  "$ROOT/scripts/notarize-macos.sh" "$NANOHARNESS_PKG_PATH" "$NANOHARNESS_APP_PATH"
  NOTARIZED=1
done < "$ARTIFACTS_LIST"

if [[ "$NOTARIZED" -eq 0 ]]; then
  echo "No per-architecture build metadata entries were found in: $ARTIFACTS_LIST" >&2
  exit 1
fi
