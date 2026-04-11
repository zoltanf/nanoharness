#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This build script only runs on macOS." >&2
  exit 1
fi

APP_NAME="${NANOHARNESS_APP_NAME:-NanoHarness}"
CLI_NAME="${NANOHARNESS_CLI_NAME:-nanoh}"
BUNDLE_ID="${NANOHARNESS_BUNDLE_ID:-com.nanoharness.app}"
TARGET_ARCH_INPUT="${NANOHARNESS_TARGET_ARCHES:-${NANOHARNESS_TARGET_ARCH:-arm64 x86_64}}"
TARGET_ARCH_INPUT="${TARGET_ARCH_INPUT//,/ }"
CODESIGN_IDENTITY="${NANOHARNESS_CODESIGN_IDENTITY:-}"
INSTALLER_IDENTITY="${NANOHARNESS_INSTALLER_IDENTITY:-}"
UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/uv-cache}"
DEFAULT_UV_BIN="${NANOHARNESS_UV_BIN:-$(command -v uv || true)}"

BUILD_ROOT="$ROOT/build/macos"
DIST_ROOT="$ROOT/dist/macos"
ASSETS_ROOT="$BUILD_ROOT/assets"
ICON_PATH="$ASSETS_ROOT/${APP_NAME}.icns"
ICONSET_DIR="$ASSETS_ROOT/${APP_NAME}.iconset"
ARTIFACTS_ENV="$BUILD_ROOT/artifacts.env"
ARTIFACTS_LIST="$BUILD_ROOT/artifacts.list"
SHA256SUMS_PATH="$DIST_ROOT/SHA256SUMS.txt"

TARGET_ARCHES=()
for arch in $TARGET_ARCH_INPUT; do
  TARGET_ARCHES+=("$arch")
done

if [[ ${#TARGET_ARCHES[@]} -eq 0 ]]; then
  echo "Set NANOHARNESS_TARGET_ARCHES or NANOHARNESS_TARGET_ARCH to at least one architecture." >&2
  exit 1
fi

if [[ -z "$DEFAULT_UV_BIN" ]]; then
  echo "uv was not found in PATH." >&2
  exit 1
fi

uv_bin_for_arch() {
  local target_arch="$1"
  case "$target_arch" in
    arm64)
      printf '%s\n' "${NANOHARNESS_UV_BIN_ARM64:-$DEFAULT_UV_BIN}"
      ;;
    x86_64)
      printf '%s\n' "${NANOHARNESS_UV_BIN_X86_64:-$DEFAULT_UV_BIN}"
      ;;
    *)
      printf '%s\n' "$DEFAULT_UV_BIN"
      ;;
  esac
}

run_uv_host() {
  local uv_bin="$DEFAULT_UV_BIN"
  env \
    UV_CACHE_DIR="$UV_CACHE_DIR" \
    UV_PROJECT_ENVIRONMENT="$BUILD_ROOT/uv-env-host" \
    "$uv_bin" run "$@"
}

can_run_arch() {
  local target_arch="$1"
  local uv_bin
  uv_bin="$(uv_bin_for_arch "$target_arch")"
  if [[ ! -x "$uv_bin" ]]; then
    return 1
  fi
  arch "-$target_arch" "$uv_bin" --version >/dev/null 2>&1
}

run_uv_arch() {
  local target_arch="$1"
  local uv_bin
  uv_bin="$(uv_bin_for_arch "$target_arch")"
  shift
  env \
    UV_CACHE_DIR="$UV_CACHE_DIR" \
    UV_PROJECT_ENVIRONMENT="$BUILD_ROOT/uv-env-$target_arch" \
    arch "-$target_arch" "$uv_bin" run "$@"
}

SEEN_ARCHES=""
for arch in "${TARGET_ARCHES[@]}"; do
  case "$arch" in
    arm64|x86_64) ;;
    *)
      echo "Unsupported target architecture: $arch" >&2
      exit 1
      ;;
  esac
  if [[ " $SEEN_ARCHES " == *" $arch "* ]]; then
    echo "Duplicate target architecture requested: $arch" >&2
    exit 1
  fi
  if ! can_run_arch "$arch"; then
    if [[ "$arch" == "x86_64" && "$(uname -m)" == "arm64" ]]; then
      echo "Unable to run uv under x86_64. Install Rosetta, provide an x86_64-capable uv via NANOHARNESS_UV_BIN_X86_64, or set NANOHARNESS_TARGET_ARCHES=arm64." >&2
    else
      echo "Unable to run uv under $arch on this machine. Set NANOHARNESS_TARGET_ARCHES to a supported subset." >&2
    fi
    exit 1
  fi
  SEEN_ARCHES="$SEEN_ARCHES $arch"
done

if [[ -n "${NANOHARNESS_BUILD_VERSION:-}" ]]; then
  VERSION_SHELL_OUTPUT="$(
    python3 "$ROOT/scripts/version_info.py" \
      --version "$NANOHARNESS_BUILD_VERSION" \
      --write \
      --format shell
  )"
else
  VERSION_SHELL_OUTPUT="$(
    python3 "$ROOT/scripts/version_info.py" \
      --write \
      --format shell
  )"
fi

eval "$VERSION_SHELL_OUTPUT"

: "${NANOHARNESS_BUILD_VERSION:?failed to derive build version}"
: "${NANOHARNESS_PACKAGE_VERSION:?failed to derive package version}"
: "${NANOHARNESS_BUNDLE_SHORT_VERSION:?failed to derive bundle short version}"
: "${NANOHARNESS_BUNDLE_BUILD_VERSION:?failed to derive bundle build version}"

export NANOHARNESS_BUILD_VERSION
export NANOHARNESS_PACKAGE_VERSION
export NANOHARNESS_BUNDLE_SHORT_VERSION
export NANOHARNESS_BUNDLE_BUILD_VERSION
export NANOHARNESS_PROJECT_ROOT="$ROOT"
export NANOHARNESS_APP_NAME="$APP_NAME"
export NANOHARNESS_CLI_NAME="$CLI_NAME"
export NANOHARNESS_BUNDLE_ID="$BUNDLE_ID"
export NANOHARNESS_CODESIGN_IDENTITY="$CODESIGN_IDENTITY"
export NANOHARNESS_ICON="$ICON_PATH"
export UV_CACHE_DIR

echo "Building NanoHarness ${NANOHARNESS_BUILD_VERSION}"
echo "  app: ${APP_NAME}.app"
echo "  cli: ${CLI_NAME}"
echo "  archs: ${TARGET_ARCHES[*]}"

rm -rf "$BUILD_ROOT" "$DIST_ROOT"
mkdir -p "$ASSETS_ROOT" "$DIST_ROOT"
: > "$SHA256SUMS_PATH"
: > "$ARTIFACTS_LIST"

run_uv_host --extra build python "$ROOT/scripts/render_macos_icon.py" \
  --output "$ICON_PATH" \
  --workdir "$ICONSET_DIR"

ARCH_ARTIFACTS_ENVS=()
for TARGET_ARCH in "${TARGET_ARCHES[@]}"; do
  export NANOHARNESS_TARGET_ARCH="$TARGET_ARCH"

  ARCH_BUILD_ROOT="$BUILD_ROOT/$TARGET_ARCH"
  ARCH_DIST_ROOT="$DIST_ROOT/$TARGET_ARCH"
  PKGROOT="$ARCH_BUILD_ROOT/pkgroot"
  ARCH_ARTIFACTS_ENV="$ARCH_BUILD_ROOT/artifacts.env"
  HOMEBREW_BUNDLE_STAGE="$ARCH_BUILD_ROOT/homebrew-bundle"
  HOMEBREW_CLI_STAGE="$ARCH_BUILD_ROOT/homebrew-cli"

  APP_PATH="$ARCH_DIST_ROOT/${APP_NAME}.app"
  CLI_PATH="$ARCH_DIST_ROOT/${CLI_NAME}"
  PKG_PATH="$ARCH_DIST_ROOT/${APP_NAME}-${NANOHARNESS_BUILD_VERSION}-${TARGET_ARCH}.pkg"
  HOMEBREW_CASK_PATH="$DIST_ROOT/${APP_NAME}-homebrew-${NANOHARNESS_BUILD_VERSION}-${TARGET_ARCH}.tar.gz"
  HOMEBREW_CLI_PATH="$DIST_ROOT/${CLI_NAME}-${NANOHARNESS_BUILD_VERSION}-${TARGET_ARCH}.tar.gz"

  mkdir -p "$ARCH_BUILD_ROOT" "$ARCH_DIST_ROOT"

  echo "  building arch: ${TARGET_ARCH}"

  PYI_ARGS=(
    --noconfirm
    --clean
    --distpath "$ARCH_DIST_ROOT"
    --workpath "$ARCH_BUILD_ROOT/pyinstaller/work"
    --target-architecture "$TARGET_ARCH"
  )

  run_uv_arch "$TARGET_ARCH" --extra app --extra build pyinstaller "${PYI_ARGS[@]}" "$ROOT/packaging/NanoHarness-app.spec"
  run_uv_arch "$TARGET_ARCH" --extra app --extra build pyinstaller "${PYI_ARGS[@]}" "$ROOT/packaging/nanoh.spec"

  if [[ ! -d "$APP_PATH" ]]; then
    echo "Expected app bundle was not produced: $APP_PATH" >&2
    exit 1
  fi
  if [[ ! -x "$CLI_PATH" ]]; then
    echo "Expected CLI binary was not produced: $CLI_PATH" >&2
    exit 1
  fi

  mkdir -p "$PKGROOT/Applications" "$PKGROOT/usr/local/bin"
  ditto "$APP_PATH" "$PKGROOT/Applications/${APP_NAME}.app"
  install -m 755 "$CLI_PATH" "$PKGROOT/usr/local/bin/${CLI_NAME}"

  rm -rf "$HOMEBREW_BUNDLE_STAGE" "$HOMEBREW_CLI_STAGE"
  mkdir -p "$HOMEBREW_BUNDLE_STAGE" "$HOMEBREW_CLI_STAGE"
  ditto "$APP_PATH" "$HOMEBREW_BUNDLE_STAGE/${APP_NAME}.app"
  install -m 755 "$CLI_PATH" "$HOMEBREW_BUNDLE_STAGE/${CLI_NAME}"
  install -m 755 "$CLI_PATH" "$HOMEBREW_CLI_STAGE/${CLI_NAME}"
  tar -C "$HOMEBREW_BUNDLE_STAGE" -czf "$HOMEBREW_CASK_PATH" "${APP_NAME}.app" "${CLI_NAME}"
  tar -C "$HOMEBREW_CLI_STAGE" -czf "$HOMEBREW_CLI_PATH" "${CLI_NAME}"

  PKGBUILD_ARGS=(
    --root "$PKGROOT"
    --identifier "${BUNDLE_ID}.pkg.${TARGET_ARCH}"
    --version "$NANOHARNESS_BUNDLE_BUILD_VERSION"
    --install-location /
  )
  if [[ -n "$INSTALLER_IDENTITY" ]]; then
    PKGBUILD_ARGS+=(--sign "$INSTALLER_IDENTITY")
  fi
  pkgbuild "${PKGBUILD_ARGS[@]}" "$PKG_PATH"

  PKG_SHA256="$(shasum -a 256 "$PKG_PATH" | awk '{print $1}')"
  HOMEBREW_CASK_SHA256="$(shasum -a 256 "$HOMEBREW_CASK_PATH" | awk '{print $1}')"
  HOMEBREW_CLI_SHA256="$(shasum -a 256 "$HOMEBREW_CLI_PATH" | awk '{print $1}')"

  cat >> "$SHA256SUMS_PATH" <<EOF
${PKG_SHA256}  $(basename "$PKG_PATH")
${HOMEBREW_CASK_SHA256}  $(basename "$HOMEBREW_CASK_PATH")
${HOMEBREW_CLI_SHA256}  $(basename "$HOMEBREW_CLI_PATH")
EOF

  cat > "$ARCH_ARTIFACTS_ENV" <<EOF
NANOHARNESS_BUILD_VERSION='${NANOHARNESS_BUILD_VERSION}'
NANOHARNESS_PACKAGE_VERSION='${NANOHARNESS_PACKAGE_VERSION}'
NANOHARNESS_BUNDLE_SHORT_VERSION='${NANOHARNESS_BUNDLE_SHORT_VERSION}'
NANOHARNESS_BUNDLE_BUILD_VERSION='${NANOHARNESS_BUNDLE_BUILD_VERSION}'
NANOHARNESS_TARGET_ARCH='${TARGET_ARCH}'
NANOHARNESS_APP_PATH='${APP_PATH}'
NANOHARNESS_CLI_PATH='${CLI_PATH}'
NANOHARNESS_PKG_PATH='${PKG_PATH}'
NANOHARNESS_PKG_SHA256='${PKG_SHA256}'
NANOHARNESS_HOMEBREW_CASK_PATH='${HOMEBREW_CASK_PATH}'
NANOHARNESS_HOMEBREW_CASK_SHA256='${HOMEBREW_CASK_SHA256}'
NANOHARNESS_HOMEBREW_CLI_PATH='${HOMEBREW_CLI_PATH}'
NANOHARNESS_HOMEBREW_CLI_SHA256='${HOMEBREW_CLI_SHA256}'
NANOHARNESS_SHA256SUMS_PATH='${SHA256SUMS_PATH}'
EOF

  ARCH_ARTIFACTS_ENVS+=("$ARCH_ARTIFACTS_ENV")
done

ARTIFACTS_ENV_LIST_VALUE=""
TARGET_ARCHES_VALUE=""
for i in "${!TARGET_ARCHES[@]}"; do
  arch="${TARGET_ARCHES[$i]}"
  env_file="${ARCH_ARTIFACTS_ENVS[$i]}"
  printf '%s\n' "$env_file" >> "$ARTIFACTS_LIST"
  if [[ -n "$ARTIFACTS_ENV_LIST_VALUE" ]]; then
    ARTIFACTS_ENV_LIST_VALUE="${ARTIFACTS_ENV_LIST_VALUE}:"
    TARGET_ARCHES_VALUE="${TARGET_ARCHES_VALUE} "
  fi
  ARTIFACTS_ENV_LIST_VALUE="${ARTIFACTS_ENV_LIST_VALUE}${env_file}"
  TARGET_ARCHES_VALUE="${TARGET_ARCHES_VALUE}${arch}"
done

{
  echo "NANOHARNESS_BUILD_VERSION='${NANOHARNESS_BUILD_VERSION}'"
  echo "NANOHARNESS_PACKAGE_VERSION='${NANOHARNESS_PACKAGE_VERSION}'"
  echo "NANOHARNESS_BUNDLE_SHORT_VERSION='${NANOHARNESS_BUNDLE_SHORT_VERSION}'"
  echo "NANOHARNESS_BUNDLE_BUILD_VERSION='${NANOHARNESS_BUNDLE_BUILD_VERSION}'"
  echo "NANOHARNESS_TARGET_ARCHES='${TARGET_ARCHES_VALUE}'"
  echo "NANOHARNESS_ARTIFACTS_LIST_PATH='${ARTIFACTS_LIST}'"
  echo "NANOHARNESS_ARTIFACTS_ENV_LIST='${ARTIFACTS_ENV_LIST_VALUE}'"
  echo "NANOHARNESS_SHA256SUMS_PATH='${SHA256SUMS_PATH}'"
  for i in "${!TARGET_ARCHES[@]}"; do
    echo "NANOHARNESS_ARTIFACTS_ENV_${TARGET_ARCHES[$i]}='${ARCH_ARTIFACTS_ENVS[$i]}'"
  done
} > "$ARTIFACTS_ENV"

echo
echo "Build complete."
for arch in "${TARGET_ARCHES[@]}"; do
  echo "  ${arch} app: $DIST_ROOT/$arch/${APP_NAME}.app"
  echo "  ${arch} cli: $DIST_ROOT/$arch/${CLI_NAME}"
  echo "  ${arch} pkg: $DIST_ROOT/$arch/${APP_NAME}-${NANOHARNESS_BUILD_VERSION}-${arch}.pkg"
  echo "  ${arch} homebrew cask asset: $DIST_ROOT/${APP_NAME}-homebrew-${NANOHARNESS_BUILD_VERSION}-${arch}.tar.gz"
  echo "  ${arch} homebrew cli asset: $DIST_ROOT/${CLI_NAME}-${NANOHARNESS_BUILD_VERSION}-${arch}.tar.gz"
done
echo "  shas: $SHA256SUMS_PATH"
echo "  metadata manifest: $ARTIFACTS_ENV"
echo "  metadata list: $ARTIFACTS_LIST"

if [[ -z "$CODESIGN_IDENTITY" ]]; then
  echo "WARNING: NANOHARNESS_CODESIGN_IDENTITY is unset. The app and CLI are unsigned." >&2
fi
if [[ -z "$INSTALLER_IDENTITY" ]]; then
  echo "WARNING: NANOHARNESS_INSTALLER_IDENTITY is unset. The installer package is unsigned." >&2
fi
