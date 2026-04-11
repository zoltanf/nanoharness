# macOS Build and Release

NanoHarness now ships with a macOS-oriented build pipeline that produces:

- `NanoHarness.app`
- `nanoh`
- `NanoHarness-<version>-arm64.pkg`
- `NanoHarness-<version>-x86_64.pkg`
- `NanoHarness-homebrew-<version>-<arch>.tar.gz`
- `nanoh-<version>-<arch>.tar.gz`

The user-facing build version is timestamp based and looks like:

`2026.04.11.1229`

## Outputs

Running the build writes artifacts to:

- `dist/macos/arm64/NanoHarness.app`
- `dist/macos/arm64/nanoh`
- `dist/macos/arm64/NanoHarness-<version>-arm64.pkg`
- `dist/macos/x86_64/NanoHarness.app`
- `dist/macos/x86_64/nanoh`
- `dist/macos/x86_64/NanoHarness-<version>-x86_64.pkg`
- `dist/macos/NanoHarness-homebrew-<version>-<arch>.tar.gz`
- `dist/macos/nanoh-<version>-<arch>.tar.gz`
- `dist/macos/SHA256SUMS.txt`
- `build/macos/artifacts.env`
- `build/macos/artifacts.list`
- `build/macos/arm64/artifacts.env`
- `build/macos/x86_64/artifacts.env`

`build/macos/artifacts.env` is a shell-friendly manifest for the whole build. `build/macos/artifacts.list` enumerates the per-architecture metadata files used by the publish scripts.

## Versioning

The build version is generated automatically in local time by:

```bash
python3 scripts/version_info.py --write
```

You can force a specific build version:

```bash
export NANOHARNESS_BUILD_VERSION=2026.04.11.1229
```

## Build

Unsigned local build:

```bash
./scripts/build-macos.sh
```

By default this attempts both `arm64` and `x86_64` so one run can prepare the release assets for the app bundle, the installer, and Homebrew.

This uses `uv run --extra app --extra build ...` under the hood, so the Python runtime and app dependencies are bundled into the frozen outputs. End users do not need Python or uv installed.

The script drives separate per-architecture uv environments automatically. On Apple Silicon, the `x86_64` pass requires Rosetta plus an `x86_64`-capable `uv` binary. If your default `uv` is arm64-only, point the script at an Intel build of `uv`:

```bash
export NANOHARNESS_UV_BIN_X86_64="/usr/local/bin/uv"
./scripts/build-macos.sh
```

If you only want a single-architecture build:

```bash
export NANOHARNESS_TARGET_ARCHES="arm64"
./scripts/build-macos.sh
```

The Homebrew artifacts contain:

- the `.app` bundle plus `nanoh` for the cask
- the standalone `nanoh` binary for the formula

## Sign

Set your Developer ID identities before building:

```bash
export NANOHARNESS_CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
export NANOHARNESS_INSTALLER_IDENTITY="Developer ID Installer: Your Name (TEAMID)"
```

Then run:

```bash
./scripts/build-macos.sh
```

## Notarize

Create a notarytool keychain profile first, then set:

```bash
export NANOHARNESS_NOTARY_PROFILE="nanoharness-notary"
```

Notarize an existing build:

```bash
./scripts/notarize-macos.sh dist/macos/arm64/NanoHarness-<version>-arm64.pkg dist/macos/arm64/NanoHarness.app
./scripts/notarize-macos.sh dist/macos/x86_64/NanoHarness-<version>-x86_64.pkg dist/macos/x86_64/NanoHarness.app
```

Or build + notarize in one step:

```bash
./scripts/release-macos.sh
```

`release-macos.sh` walks the generated `build/macos/artifacts.list` file and notarizes every built architecture.

## GitHub release assets

After building, upload the Homebrew assets to the source repository release with:

```bash
./scripts/publish-github-release.sh
```

This uses `gh` and expects:

- a valid `gh auth login`
- `origin` to point at the source GitHub repo

The release tag is `v<version>`.

The release uploader reads `build/macos/artifacts.list` and uploads all generated per-architecture `.pkg` installers, both Homebrew archives, and `dist/macos/SHA256SUMS.txt`.

## Homebrew tap

Create or update the Homebrew tap repository with:

```bash
./scripts/publish-homebrew-tap.sh
```

By default this uses:

- source repo: inferred from `git remote get-url origin`
- tap repo: `<owner>/homebrew-nanoharness`

You can override the tap repo with:

```bash
export NANOHARNESS_TAP_REPO="your-user/homebrew-nanoharness"
```

When you use the default dual-arch build, the tap publisher consumes both generated metadata files automatically and renders a dual-arch cask and formula. You can still pass explicit metadata files if needed:

```bash
./scripts/publish-homebrew-tap.sh build/macos/arm64/artifacts.env build/macos/x86_64/artifacts.env
```

## macOS security prompt workaround

If you install an unsigned or not-yet-notarized build and macOS blocks the first launch, you can remove the quarantine flag from Terminal:

```bash
sudo xattr -r -d com.apple.quarantine "/Applications/NanoHarness.app"
```

Or open `System Settings` -> `Privacy & Security`, scroll to the bottom, and click `Open Anyway` for `NanoHarness.app`.

## Useful environment variables

- `NANOHARNESS_BUILD_VERSION`: Override the generated timestamp version.
- `NANOHARNESS_APP_NAME`: Defaults to `NanoHarness`.
- `NANOHARNESS_CLI_NAME`: Defaults to `nanoh`.
- `NANOHARNESS_BUNDLE_ID`: Defaults to `com.nanoharness.app`.
- `NANOHARNESS_TARGET_ARCHES`: Defaults to `arm64 x86_64`.
- `NANOHARNESS_TARGET_ARCH`: Single-architecture override used for compatibility with older local flows.
- `NANOHARNESS_UV_BIN`: Override the default `uv` binary path.
- `NANOHARNESS_UV_BIN_ARM64`: Override the `uv` binary used for the `arm64` build.
- `NANOHARNESS_UV_BIN_X86_64`: Override the `uv` binary used for the `x86_64` build.
- `NANOHARNESS_CODESIGN_IDENTITY`: Developer ID Application identity.
- `NANOHARNESS_INSTALLER_IDENTITY`: Developer ID Installer identity.
- `NANOHARNESS_NOTARY_PROFILE`: notarytool keychain profile name.
- `NANOHARNESS_TAP_REPO`: Override the default tap repo name.

## Notes

- The `.pkg` is the recommended end-user artifact because it can install both the app bundle and the `nanoh` terminal command in one step.
- `NanoHarness.app` launches the desktop mode directly.
- `nanoh` is packaged as a standalone terminal executable.
- Homebrew installation is driven by the generated cask and formula in the tap repo.
