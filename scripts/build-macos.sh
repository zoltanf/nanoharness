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
TARGET_ARCH="${NANOHARNESS_TARGET_ARCH:-$(uname -m)}"
CODESIGN_IDENTITY="${NANOHARNESS_CODESIGN_IDENTITY:-}"
INSTALLER_IDENTITY="${NANOHARNESS_INSTALLER_IDENTITY:-}"
UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/uv-cache}"

BUILD_ROOT="$ROOT/build/macos"
DIST_ROOT="$ROOT/dist/macos"
ICON_PATH="$BUILD_ROOT/assets/${APP_NAME}.icns"
ICONSET_DIR="$BUILD_ROOT/assets/${APP_NAME}.iconset"
PKGROOT="$BUILD_ROOT/pkgroot"
ARTIFACTS_ENV="$BUILD_ROOT/artifacts.env"

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
export NANOHARNESS_TARGET_ARCH="$TARGET_ARCH"
export NANOHARNESS_CODESIGN_IDENTITY="$CODESIGN_IDENTITY"
export NANOHARNESS_ICON="$ICON_PATH"
export UV_CACHE_DIR

echo "Building NanoHarness ${NANOHARNESS_BUILD_VERSION}"
echo "  app: ${APP_NAME}.app"
echo "  cli: ${CLI_NAME}"
echo "  arch: ${TARGET_ARCH}"

rm -rf "$BUILD_ROOT" "$DIST_ROOT"
mkdir -p "$BUILD_ROOT" "$DIST_ROOT"

uv run --extra app --extra build python "$ROOT/scripts/render_macos_icon.py" \
  --output "$ICON_PATH" \
  --workdir "$ICONSET_DIR"

PYI_ARGS=(
  --noconfirm
  --clean
  --distpath "$DIST_ROOT"
  --workpath "$BUILD_ROOT/pyinstaller/work"
)

uv run --extra app --extra build pyinstaller "${PYI_ARGS[@]}" "$ROOT/packaging/NanoHarness-app.spec"
uv run --extra app --extra build pyinstaller "${PYI_ARGS[@]}" "$ROOT/packaging/nanoh.spec"

APP_PATH="$DIST_ROOT/${APP_NAME}.app"
CLI_PATH="$DIST_ROOT/${CLI_NAME}"
PKG_PATH="$DIST_ROOT/${APP_NAME}-${NANOHARNESS_BUILD_VERSION}.pkg"

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

PKGBUILD_ARGS=(
  --root "$PKGROOT"
  --identifier "${BUNDLE_ID}.pkg"
  --version "$NANOHARNESS_BUNDLE_BUILD_VERSION"
  --install-location /
)
if [[ -n "$INSTALLER_IDENTITY" ]]; then
  PKGBUILD_ARGS+=(--sign "$INSTALLER_IDENTITY")
fi
pkgbuild "${PKGBUILD_ARGS[@]}" "$PKG_PATH"

cat > "$ARTIFACTS_ENV" <<EOF
NANOHARNESS_BUILD_VERSION='${NANOHARNESS_BUILD_VERSION}'
NANOHARNESS_PACKAGE_VERSION='${NANOHARNESS_PACKAGE_VERSION}'
NANOHARNESS_BUNDLE_SHORT_VERSION='${NANOHARNESS_BUNDLE_SHORT_VERSION}'
NANOHARNESS_BUNDLE_BUILD_VERSION='${NANOHARNESS_BUNDLE_BUILD_VERSION}'
NANOHARNESS_APP_PATH='${APP_PATH}'
NANOHARNESS_CLI_PATH='${CLI_PATH}'
NANOHARNESS_PKG_PATH='${PKG_PATH}'
EOF

echo
echo "Build complete."
echo "  app: $APP_PATH"
echo "  cli: $CLI_PATH"
echo "  pkg: $PKG_PATH"
echo "  metadata: $ARTIFACTS_ENV"

if [[ -z "$CODESIGN_IDENTITY" ]]; then
  echo "WARNING: NANOHARNESS_CODESIGN_IDENTITY is unset. The app and CLI are unsigned." >&2
fi
if [[ -z "$INSTALLER_IDENTITY" ]]; then
  echo "WARNING: NANOHARNESS_INSTALLER_IDENTITY is unset. The installer package is unsigned." >&2
fi
