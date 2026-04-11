from __future__ import annotations

from pathlib import Path

from scripts.render_homebrew_tap import main as render_homebrew_tap_main


def _write_artifacts_env(
    path: Path,
    *,
    version: str,
    arch: str,
    cask_filename: str,
    cask_sha: str,
    cli_filename: str,
    cli_sha: str,
) -> Path:
    path.write_text(
        "\n".join(
            [
                f"NANOHARNESS_BUILD_VERSION='{version}'",
                f"NANOHARNESS_TARGET_ARCH='{arch}'",
                f"NANOHARNESS_HOMEBREW_CASK_PATH='/tmp/{cask_filename}'",
                f"NANOHARNESS_HOMEBREW_CASK_SHA256='{cask_sha}'",
                f"NANOHARNESS_HOMEBREW_CLI_PATH='/tmp/{cli_filename}'",
                f"NANOHARNESS_HOMEBREW_CLI_SHA256='{cli_sha}'",
                "",
            ]
        )
    )
    return path


def test_render_homebrew_tap_dual_arch(tmp_path: Path, monkeypatch):
    arm = _write_artifacts_env(
        tmp_path / "arm.env",
        version="2026.04.11.1229",
        arch="arm64",
        cask_filename="NanoHarness-homebrew-2026.04.11.1229-arm64.tar.gz",
        cask_sha="arm-cask-sha",
        cli_filename="nanoh-2026.04.11.1229-arm64.tar.gz",
        cli_sha="arm-cli-sha",
    )
    intel = _write_artifacts_env(
        tmp_path / "intel.env",
        version="2026.04.11.1229",
        arch="x86_64",
        cask_filename="NanoHarness-homebrew-2026.04.11.1229-x86_64.tar.gz",
        cask_sha="intel-cask-sha",
        cli_filename="nanoh-2026.04.11.1229-x86_64.tar.gz",
        cli_sha="intel-cli-sha",
    )
    tap_dir = tmp_path / "tap"

    monkeypatch.setattr(
        "sys.argv",
        [
            "render_homebrew_tap.py",
            "--tap-dir",
            str(tap_dir),
            "--source-repo",
            "zoltanf/nanoharness",
            "--tap-repo",
            "zoltanf/homebrew-nanoharness",
            "--artifacts-env",
            str(arm),
            str(intel),
        ],
    )

    assert render_homebrew_tap_main() == 0

    cask = (tap_dir / "Casks" / "nanoharness.rb").read_text()
    formula = (tap_dir / "Formula" / "nanoh.rb").read_text()
    assert 'arch arm: "arm64", intel: "x86_64"' in cask
    assert 'sha256 arm: "arm-cask-sha", intel: "intel-cask-sha"' in cask
    assert 'on_arm do' in formula
    assert 'on_intel do' in formula


def test_render_homebrew_tap_single_arch(tmp_path: Path, monkeypatch):
    arm = _write_artifacts_env(
        tmp_path / "arm.env",
        version="2026.04.11.1229",
        arch="arm64",
        cask_filename="NanoHarness-homebrew-2026.04.11.1229-arm64.tar.gz",
        cask_sha="arm-cask-sha",
        cli_filename="nanoh-2026.04.11.1229-arm64.tar.gz",
        cli_sha="arm-cli-sha",
    )
    tap_dir = tmp_path / "tap"

    monkeypatch.setattr(
        "sys.argv",
        [
            "render_homebrew_tap.py",
            "--tap-dir",
            str(tap_dir),
            "--source-repo",
            "zoltanf/nanoharness",
            "--tap-repo",
            "zoltanf/homebrew-nanoharness",
            "--artifacts-env",
            str(arm),
        ],
    )

    assert render_homebrew_tap_main() == 0

    cask = (tap_dir / "Casks" / "nanoharness.rb").read_text()
    formula = (tap_dir / "Formula" / "nanoh.rb").read_text()
    assert 'depends_on arch: :arm64' in cask
    assert 'depends_on arch: :arm64' in formula
