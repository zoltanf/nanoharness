#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

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

# shellcheck disable=SC1090
source "${ARTIFACT_FILES[0]}"

if ! gh auth status >/dev/null 2>&1; then
  echo "gh is not authenticated. Run: gh auth login -h github.com" >&2
  exit 1
fi

ORIGIN_URL="$(git remote get-url origin)"
if [[ "$ORIGIN_URL" =~ github\.com[:/]([^/]+)/([^.]+)(\.git)?$ ]]; then
  SOURCE_REPO="${BASH_REMATCH[1]}/${BASH_REMATCH[2]}"
else
  echo "Could not infer GitHub source repo from origin: $ORIGIN_URL" >&2
  exit 1
fi

TAG="v${NANOHARNESS_BUILD_VERSION}"
TITLE="NanoHarness ${NANOHARNESS_BUILD_VERSION}"

if ! gh release view "$TAG" --repo "$SOURCE_REPO" >/dev/null 2>&1; then
  gh release create "$TAG" \
    --repo "$SOURCE_REPO" \
    --title "$TITLE" \
    --notes "Automated NanoHarness release for ${NANOHARNESS_BUILD_VERSION}."
fi

UPLOADS=()
for file in "${ARTIFACT_FILES[@]}"; do
  # shellcheck disable=SC1090
  source "$file"
  if [[ ${#UPLOADS[@]} -eq 0 ]]; then
    UPLOADS+=("$NANOHARNESS_SHA256SUMS_PATH")
  fi
  UPLOADS+=(
    "$NANOHARNESS_PKG_PATH"
    "$NANOHARNESS_HOMEBREW_CASK_PATH"
    "$NANOHARNESS_HOMEBREW_CLI_PATH"
  )
done

gh release upload "$TAG" \
  --repo "$SOURCE_REPO" \
  --clobber \
  "${UPLOADS[@]}"

echo "Published release assets to $SOURCE_REPO@$TAG"
