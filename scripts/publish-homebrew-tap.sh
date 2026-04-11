#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! gh auth status >/dev/null 2>&1; then
  echo "gh is not authenticated. Run: gh auth login -h github.com" >&2
  exit 1
fi

ORIGIN_URL="$(git remote get-url origin)"
if [[ "$ORIGIN_URL" =~ github\.com[:/]([^/]+)/([^.]+)(\.git)?$ ]]; then
  SOURCE_OWNER="${BASH_REMATCH[1]}"
  SOURCE_NAME="${BASH_REMATCH[2]}"
  SOURCE_REPO="${SOURCE_OWNER}/${SOURCE_NAME}"
else
  echo "Could not infer GitHub source repo from origin: $ORIGIN_URL" >&2
  exit 1
fi

TAP_REPO="${NANOHARNESS_TAP_REPO:-${SOURCE_OWNER}/homebrew-nanoharness}"
TAP_DIR="$ROOT/build/homebrew-tap"

ARTIFACT_FILES=()

collect_artifacts_envs() {
  local list_path="$ROOT/build/macos/artifacts.list"
  local candidate
  if [[ $# -gt 0 ]]; then
    for candidate in "$@"; do
      if [[ ! -f "$candidate" ]]; then
        echo "Artifacts metadata not found: $candidate" >&2
        exit 1
      fi
      ARTIFACT_FILES+=("$candidate")
    done
    return
  fi

  if [[ ! -f "$list_path" ]]; then
    echo "Artifacts metadata list not found: $list_path" >&2
    exit 1
  fi

  while IFS= read -r candidate; do
    if [[ -z "$candidate" ]]; then
      continue
    fi
    if [[ ! -f "$candidate" ]]; then
      echo "Artifacts metadata not found: $candidate" >&2
      exit 1
    fi
    ARTIFACT_FILES+=("$candidate")
  done < "$list_path"

  if [[ ${#ARTIFACT_FILES[@]} -eq 0 ]]; then
    echo "No per-architecture metadata files were found in: $list_path" >&2
    exit 1
  fi
}

collect_artifacts_envs "$@"

rm -rf "$TAP_DIR"

if gh repo view "$TAP_REPO" >/dev/null 2>&1; then
  gh repo clone "$TAP_REPO" "$TAP_DIR"
else
  gh repo create "$TAP_REPO" --public --description "Homebrew tap for NanoHarness" >/dev/null
  gh repo clone "$TAP_REPO" "$TAP_DIR"
fi

python3 "$ROOT/scripts/render_homebrew_tap.py" \
  --tap-dir "$TAP_DIR" \
  --source-repo "$SOURCE_REPO" \
  --tap-repo "$TAP_REPO" \
  --artifacts-env "${ARTIFACT_FILES[@]}"

git -C "$TAP_DIR" add Casks Formula README.md
if git -C "$TAP_DIR" diff --cached --quiet; then
  echo "Homebrew tap already up to date."
  exit 0
fi

VERSION_LINE="$(sed -n "s/^NANOHARNESS_BUILD_VERSION='\(.*\)'$/\1/p" "${ARTIFACT_FILES[0]}")"
git -C "$TAP_DIR" commit -m "Update NanoHarness Homebrew tap to ${VERSION_LINE}"
git -C "$TAP_DIR" push

echo "Published Homebrew tap updates to $TAP_REPO"
