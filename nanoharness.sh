#!/usr/bin/env bash
# Run NanoHarness — auto-selects the right uv extras based on mode flag.
#
#   ./nanoharness.sh              → TUI (default)
#   ./nanoharness.sh --app        → desktop webview
#   ./nanoharness.sh --web        → browser web UI
#   ./nanoharness.sh --repl       → plain REPL
#   ./nanoharness.sh --model llama3.2 --think
#                                 → any extra flags passed through
set -eo pipefail

cd "$(dirname "$0")"

EXTRAS=()
for arg in "$@"; do
  case "$arg" in
    --app)  EXTRAS=(--extra app);  break ;;
    --web)  EXTRAS=(--extra web);  break ;;
  esac
done

exec uv run "${EXTRAS[@]}" python -m nanoharness "$@"
