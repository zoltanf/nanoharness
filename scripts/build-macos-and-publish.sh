#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"$SCRIPT_DIR/build-macos.sh"
"$SCRIPT_DIR/publish-github-release.sh"
"$SCRIPT_DIR/publish-homebrew-tap.sh"
