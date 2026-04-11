#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 <pkg-path> <app-path>" >&2
  exit 1
fi

PKG_PATH="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
APP_PATH="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"
NOTARY_PROFILE="${NANOHARNESS_NOTARY_PROFILE:-}"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Notarization requires macOS." >&2
  exit 1
fi
if [[ -z "$NOTARY_PROFILE" ]]; then
  echo "Set NANOHARNESS_NOTARY_PROFILE to a notarytool keychain profile name." >&2
  exit 1
fi
if [[ ! -f "$PKG_PATH" ]]; then
  echo "Package not found: $PKG_PATH" >&2
  exit 1
fi
if [[ ! -d "$APP_PATH" ]]; then
  echo "App bundle not found: $APP_PATH" >&2
  exit 1
fi

xcrun notarytool submit "$PKG_PATH" --wait --keychain-profile "$NOTARY_PROFILE"
xcrun stapler staple "$APP_PATH"
xcrun stapler staple "$PKG_PATH"

echo "Notarization complete."
echo "  app: $APP_PATH"
echo "  pkg: $PKG_PATH"
