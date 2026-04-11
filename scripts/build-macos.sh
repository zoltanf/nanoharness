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
if [[ -n "${NANOHARNESS_TARGET_ARCHES:-}" ]]; then
  TARGET_ARCH_INPUT="$NANOHARNESS_TARGET_ARCHES"
elif [[ -n "${NANOHARNESS_TARGET_ARCH:-}" ]]; then
  TARGET_ARCH_INPUT="$NANOHARNESS_TARGET_ARCH"
elif [[ "$(uname -m)" == "arm64" ]]; then
  TARGET_ARCH_INPUT="x86_64 arm64"
else
  TARGET_ARCH_INPUT="x86_64"
fi
TARGET_ARCH_INPUT="${TARGET_ARCH_INPUT//,/ }"
CODESIGN_IDENTITY="${NANOHARNESS_CODESIGN_IDENTITY:-}"
INSTALLER_IDENTITY="${NANOHARNESS_INSTALLER_IDENTITY:-}"
UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/uv-cache}"
DEFAULT_UV_BIN="${NANOHARNESS_UV_BIN:-$(command -v uv || true)}"
DEFAULT_X86_64_UV_BIN="${NANOHARNESS_UV_BIN_X86_64:-}"
MANAGED_PYTHON_VERSION="${NANOHARNESS_MANAGED_PYTHON_VERSION:-3.14}"

BUILD_ROOT="$ROOT/build/macos"
DIST_ROOT="$ROOT/dist/macos"
ASSETS_ROOT="$BUILD_ROOT/assets"
ICON_PATH="$ASSETS_ROOT/${APP_NAME}.icns"
ICONSET_DIR="$ASSETS_ROOT/${APP_NAME}.iconset"
ARTIFACTS_ENV="$BUILD_ROOT/artifacts.env"
ARTIFACTS_LIST="$BUILD_ROOT/artifacts.list"
SHA256SUMS_PATH="$DIST_ROOT/SHA256SUMS.txt"
MANAGED_PYTHON_ROOT="$BUILD_ROOT/managed-python"

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

remove_tree() {
  local path="$1"
  local attempt
  if [[ ! -e "$path" ]]; then
    return
  fi
  for attempt in 1 2 3 4 5; do
    rm -rf "$path" 2>/dev/null || true
    if [[ ! -e "$path" ]]; then
      return
    fi
    find "$path" -name .DS_Store -delete 2>/dev/null || true
    chmod -R u+w "$path" 2>/dev/null || true
    sleep 0.2
  done
  echo "Failed to remove existing path: $path" >&2
  find "$path" -maxdepth 4 -ls >&2 || true
  exit 1
}

x86_64_user_base() {
  arch -x86_64 /usr/bin/python3 -m site --user-base 2>/dev/null | tail -n 1
}

refresh_x86_64_uv_bin() {
  local user_base
  if [[ -n "${NANOHARNESS_UV_BIN_X86_64:-}" ]]; then
    DEFAULT_X86_64_UV_BIN="$NANOHARNESS_UV_BIN_X86_64"
    return
  fi
  if [[ -x "/usr/local/bin/uv" ]]; then
    DEFAULT_X86_64_UV_BIN="/usr/local/bin/uv"
    return
  fi
  user_base="$(x86_64_user_base || true)"
  if [[ -n "$user_base" && -x "$user_base/bin/uv" ]]; then
    DEFAULT_X86_64_UV_BIN="$user_base/bin/uv"
    return
  fi
  DEFAULT_X86_64_UV_BIN=""
}

uv_bin_for_arch() {
  local target_arch="$1"
  case "$target_arch" in
    arm64)
      printf '%s\n' "${NANOHARNESS_UV_BIN_ARM64:-$DEFAULT_UV_BIN}"
      ;;
    x86_64)
      if [[ -n "$DEFAULT_X86_64_UV_BIN" ]]; then
        printf '%s\n' "$DEFAULT_X86_64_UV_BIN"
      else
        printf '%s\n' "$DEFAULT_UV_BIN"
      fi
      ;;
    *)
      printf '%s\n' "$DEFAULT_UV_BIN"
      ;;
  esac
}

refresh_x86_64_uv_bin

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
  local -a extra_args=()
  local -a env_args=()
  uv_bin="$(uv_bin_for_arch "$target_arch")"
  shift
  env_args+=(
    UV_CACHE_DIR="$UV_CACHE_DIR"
    UV_PROJECT_ENVIRONMENT="$BUILD_ROOT/uv-env-$target_arch"
  )
  if [[ "$target_arch" == "x86_64" ]]; then
    env_args+=(
      UV_PYTHON_INSTALL_DIR="$MANAGED_PYTHON_ROOT/$target_arch"
    )
    extra_args+=(
      --managed-python
      --python "$MANAGED_PYTHON_VERSION"
    )
  fi
  if [[ "$target_arch" == "x86_64" ]]; then
    env "${env_args[@]}" \
      arch "-$target_arch" "$uv_bin" run "${extra_args[@]}" "$@"
  else
    env "${env_args[@]}" \
      arch "-$target_arch" "$uv_bin" run "$@"
  fi
}

ensure_x86_64_uv() {
  local user_base
  local uv_bin

  if [[ "$(uname -m)" != "arm64" ]]; then
    return
  fi
  refresh_x86_64_uv_bin
  uv_bin="$(uv_bin_for_arch x86_64)"
  if [[ -n "$uv_bin" && -x "$uv_bin" ]] && arch -x86_64 "$uv_bin" --version >/dev/null 2>&1; then
    return
  fi

  if ! arch -x86_64 /usr/bin/python3 -V >/dev/null 2>&1; then
    echo "Unable to start /usr/bin/python3 under x86_64. Install Rosetta first with: softwareupdate --install-rosetta" >&2
    exit 1
  fi

  echo "  bootstrapping x86_64 uv with Rosetta Python..."
  user_base="$(x86_64_user_base || true)"
  if [[ -z "$user_base" ]]; then
    echo "Unable to determine the x86_64 Python user base for bootstrapping uv." >&2
    exit 1
  fi

  mkdir -p "$BUILD_ROOT/pip-cache-x86_64"
  if ! env \
      PIP_CACHE_DIR="$BUILD_ROOT/pip-cache-x86_64" \
      PIP_DISABLE_PIP_VERSION_CHECK=1 \
      PIP_REQUIRE_VIRTUALENV=0 \
      arch -x86_64 /usr/bin/python3 -m pip install --user --upgrade uv; then
    echo "Unable to install an x86_64 uv automatically. Install an Intel-capable uv at /usr/local/bin/uv or set NANOHARNESS_UV_BIN_X86_64 manually." >&2
    exit 1
  fi

  refresh_x86_64_uv_bin
  uv_bin="$(uv_bin_for_arch x86_64)"
  if [[ -z "$uv_bin" || ! -x "$uv_bin" ]] || ! arch -x86_64 "$uv_bin" --version >/dev/null 2>&1; then
    echo "x86_64 uv bootstrap completed, but the resulting binary could not be executed. Set NANOHARNESS_UV_BIN_X86_64 manually." >&2
    exit 1
  fi
}

if [[ "$(uname -m)" == "arm64" && " ${TARGET_ARCHES[*]} " == *" x86_64 "* ]]; then
  ensure_x86_64_uv
fi

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
      if [[ -n "$DEFAULT_X86_64_UV_BIN" ]]; then
        echo "Unable to run uv under x86_64 with $DEFAULT_X86_64_UV_BIN. Install Rosetta, verify that binary is Intel-capable, or set NANOHARNESS_TARGET_ARCHES=arm64." >&2
      else
        echo "Unable to run uv under x86_64. Install Rosetta and an Intel-capable uv at /usr/local/bin/uv, provide one via NANOHARNESS_UV_BIN_X86_64, or set NANOHARNESS_TARGET_ARCHES=arm64." >&2
      fi
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
if [[ " ${TARGET_ARCHES[*]} " == *" x86_64 "* ]]; then
  echo "  x86_64 uv: $(uv_bin_for_arch x86_64)"
  echo "  x86_64 python: managed ${MANAGED_PYTHON_VERSION}"
fi
if [[ " ${TARGET_ARCHES[*]} " == *" arm64 "* ]]; then
  echo "  arm64 uv: $(uv_bin_for_arch arm64)"
fi

remove_tree "$BUILD_ROOT"
remove_tree "$DIST_ROOT"
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
