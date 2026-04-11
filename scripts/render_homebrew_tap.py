#!/usr/bin/env python3
"""Render Homebrew tap files from one or more NanoHarness build metadata files."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import shlex
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


ENV_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
ARCH_ORDER = {"arm64": 0, "x86_64": 1}


@dataclass(frozen=True)
class BuildArtifact:
    arch: str
    version: str
    cask_filename: str
    cask_sha256: str
    cli_filename: str
    cli_sha256: str


def _load_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = ENV_LINE_RE.match(line)
        if match is None:
            continue
        key, value = match.groups()
        data[key] = shlex.split(value)[0] if value else ""
    return data


def _build_artifact(path: Path) -> BuildArtifact:
    env = _load_env_file(path)
    try:
        arch = env["NANOHARNESS_TARGET_ARCH"]
        version = env["NANOHARNESS_BUILD_VERSION"]
        cask_filename = Path(env["NANOHARNESS_HOMEBREW_CASK_PATH"]).name
        cask_sha256 = env["NANOHARNESS_HOMEBREW_CASK_SHA256"]
        cli_filename = Path(env["NANOHARNESS_HOMEBREW_CLI_PATH"]).name
        cli_sha256 = env["NANOHARNESS_HOMEBREW_CLI_SHA256"]
    except KeyError as exc:
        raise SystemExit(f"{path} is missing expected build metadata: {exc}") from exc
    return BuildArtifact(
        arch=arch,
        version=version,
        cask_filename=cask_filename,
        cask_sha256=cask_sha256,
        cli_filename=cli_filename,
        cli_sha256=cli_sha256,
    )


def _cask_body(
    artifacts: list[BuildArtifact],
    *,
    source_repo: str,
    cask_token: str,
    app_name: str,
    cli_name: str,
    desc: str,
    homepage: str,
) -> str:
    version = artifacts[0].version
    by_arch = {artifact.arch: artifact for artifact in artifacts}

    lines = [
        f'cask "{cask_token}" do',
        f'  version "{version}"',
    ]
    if len(artifacts) == 2:
        lines += [
            '  arch arm: "arm64", intel: "x86_64"',
            f'  sha256 arm: "{by_arch["arm64"].cask_sha256}", intel: "{by_arch["x86_64"].cask_sha256}"',
            f'  url "https://github.com/{source_repo}/releases/download/v#{{version}}/{app_name}-homebrew-#{{version}}-#{{arch}}.tar.gz"',
        ]
    else:
        artifact = artifacts[0]
        lines += [
            f'  sha256 "{artifact.cask_sha256}"',
            f'  url "https://github.com/{source_repo}/releases/download/v#{{version}}/{artifact.cask_filename}"',
            f'  depends_on arch: :{artifact.arch}',
        ]

    lines += [
        f'  name "{app_name}"',
        f'  desc "{desc}"',
        f'  homepage "{homepage}"',
        "",
        f'  app "{app_name}.app"',
        f'  binary "{cli_name}", target: "{cli_name}"',
        "",
        '  caveats do',
        '    <<~EOS',
        "      NanoHarness requires Ollama to be installed separately.",
        "    EOS",
        "  end",
        "",
        '  zap trash: "~/.nanoharness"',
        "end",
        "",
    ]
    return "\n".join(lines)


def _formula_body(
    artifacts: list[BuildArtifact],
    *,
    source_repo: str,
    formula_name: str,
    desc: str,
    homepage: str,
) -> str:
    version = artifacts[0].version
    class_name = "".join(part.capitalize() for part in formula_name.split("-"))
    lines = [
        f"class {class_name} < Formula",
        f'  desc "{desc}"',
        f'  homepage "{homepage}"',
        f'  version "{version}"',
        "",
    ]
    if len(artifacts) == 2:
        by_arch = {artifact.arch: artifact for artifact in artifacts}
        lines += [
            "  on_arm do",
            f'    url "https://github.com/{source_repo}/releases/download/v#{{version}}/{by_arch["arm64"].cli_filename}"',
            f'    sha256 "{by_arch["arm64"].cli_sha256}"',
            "  end",
            "",
            "  on_intel do",
            f'    url "https://github.com/{source_repo}/releases/download/v#{{version}}/{by_arch["x86_64"].cli_filename}"',
            f'    sha256 "{by_arch["x86_64"].cli_sha256}"',
            "  end",
            "",
        ]
    else:
        artifact = artifacts[0]
        lines += [
            f'  depends_on arch: :{artifact.arch}',
            f'  url "https://github.com/{source_repo}/releases/download/v#{{version}}/{artifact.cli_filename}"',
            f'  sha256 "{artifact.cli_sha256}"',
            "",
        ]

    lines += [
        "  def install",
        f'    bin.install "{formula_name}"',
        "  end",
        "",
        "  test do",
        f'    system "#{{bin}}/{formula_name}", "--help"',
        "  end",
        "end",
        "",
    ]
    return "\n".join(lines)


def _readme_body(*, source_repo: str, tap_repo: str, cask_token: str, formula_name: str) -> str:
    return "\n".join(
        [
            "# NanoHarness Homebrew Tap",
            "",
            "Install the full app bundle plus CLI:",
            "",
            "```bash",
            f"brew tap {tap_repo}",
            f"brew install --cask {cask_token}",
            "```",
            "",
            "Install just the CLI:",
            "",
            "```bash",
            f"brew tap {tap_repo}",
            f"brew install {formula_name}",
            "```",
            "",
            f"Artifacts are published from https://github.com/{source_repo}.",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tap-dir", required=True, help="Destination Homebrew tap checkout.")
    parser.add_argument("--source-repo", required=True, help="GitHub repo hosting release assets, e.g. zoltanf/nanoharness.")
    parser.add_argument("--tap-repo", required=True, help="GitHub tap repo, e.g. zoltanf/homebrew-nanoharness.")
    parser.add_argument("--artifacts-env", nargs="+", required=True, help="One or more build/macos/artifacts.env files.")
    parser.add_argument("--cask-token", default="nanoharness")
    parser.add_argument("--formula-name", default="nanoh")
    parser.add_argument("--app-name", default="NanoHarness")
    parser.add_argument("--desc", default="Local LLM agent harness for macOS")
    parser.add_argument("--homepage", default="https://github.com/zoltanf/nanoharness")
    args = parser.parse_args()

    artifacts = sorted(
        [_build_artifact(Path(path).resolve()) for path in args.artifacts_env],
        key=lambda artifact: ARCH_ORDER.get(artifact.arch, 99),
    )
    if not artifacts:
        raise SystemExit("No build artifacts were provided.")

    versions = {artifact.version for artifact in artifacts}
    if len(versions) != 1:
        raise SystemExit(f"All artifacts must have the same version, got: {sorted(versions)}")

    arches = [artifact.arch for artifact in artifacts]
    if len(set(arches)) != len(arches):
        raise SystemExit(f"Duplicate architectures provided: {arches}")

    tap_dir = Path(args.tap_dir).resolve()
    casks_dir = tap_dir / "Casks"
    formula_dir = tap_dir / "Formula"
    casks_dir.mkdir(parents=True, exist_ok=True)
    formula_dir.mkdir(parents=True, exist_ok=True)

    (casks_dir / f"{args.cask_token}.rb").write_text(
        _cask_body(
            artifacts,
            source_repo=args.source_repo,
            cask_token=args.cask_token,
            app_name=args.app_name,
            cli_name=args.formula_name,
            desc=args.desc,
            homepage=args.homepage,
        )
    )
    (formula_dir / f"{args.formula_name}.rb").write_text(
        _formula_body(
            artifacts,
            source_repo=args.source_repo,
            formula_name=args.formula_name,
            desc=args.desc,
            homepage=args.homepage,
        )
    )
    (tap_dir / "README.md").write_text(
        _readme_body(
            source_repo=args.source_repo,
            tap_repo=args.tap_repo,
            cask_token=args.cask_token,
            formula_name=args.formula_name,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
