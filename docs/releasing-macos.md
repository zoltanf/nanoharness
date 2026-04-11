# macOS Build and Release

NanoHarness now ships with a macOS-oriented build pipeline that produces:

- `NanoHarness.app`
- `nanoh`
- `NanoHarness-<version>.pkg`

The user-facing build version is timestamp based and looks like:

`2026.04.11.1229`

## Outputs

Running the build writes artifacts to:

- `dist/macos/NanoHarness.app`
- `dist/macos/nanoh`
- `dist/macos/NanoHarness-<version>.pkg`
- `build/macos/artifacts.env`

`build/macos/artifacts.env` is a shell-friendly summary of the generated paths and version values.

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

This uses `uv run --extra app --extra build ...` under the hood, so the Python runtime and app dependencies are bundled into the frozen outputs. End users do not need Python or uv installed.

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
./scripts/notarize-macos.sh dist/macos/NanoHarness-<version>.pkg dist/macos/NanoHarness.app
```

Or build + notarize in one step:

```bash
./scripts/release-macos.sh
```

## Useful environment variables

- `NANOHARNESS_BUILD_VERSION`: Override the generated timestamp version.
- `NANOHARNESS_APP_NAME`: Defaults to `NanoHarness`.
- `NANOHARNESS_CLI_NAME`: Defaults to `nanoh`.
- `NANOHARNESS_BUNDLE_ID`: Defaults to `com.nanoharness.app`.
- `NANOHARNESS_TARGET_ARCH`: Defaults to the current machine architecture.
- `NANOHARNESS_CODESIGN_IDENTITY`: Developer ID Application identity.
- `NANOHARNESS_INSTALLER_IDENTITY`: Developer ID Installer identity.
- `NANOHARNESS_NOTARY_PROFILE`: notarytool keychain profile name.

## Notes

- The `.pkg` is the recommended end-user artifact because it can install both the app bundle and the `nanoh` terminal command in one step.
- `NanoHarness.app` launches the desktop mode directly.
- `nanoh` is packaged as a standalone terminal executable.
