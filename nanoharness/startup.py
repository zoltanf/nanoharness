"""Startup checks: Ollama availability and model presence."""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import sys
import time
from typing import Callable

from .config import Config
from .ollama import OllamaClient

# Minimum Ollama server version required for reliable tool-call support.
# Versions below this have known bugs (e.g. tool-call parsing, thinking field).
MIN_OLLAMA_VERSION = (0, 7, 0)


INSTALL_INSTRUCTIONS = """
Ollama is not running at {url}.

To install Ollama:
  macOS:   brew install ollama   (or download from https://ollama.com)
  Linux:   curl -fsSL https://ollama.com/install.sh | sh
  Windows: Download from https://ollama.com/download

After installing, start Ollama:
  ollama serve
"""


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse 'X.Y.Z' into (X, Y, Z). Returns (0, 0, 0) on failure."""
    m = re.search(r'(\d+)\.(\d+)\.(\d+)', v)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return (0, 0, 0)


async def check_version(client: OllamaClient) -> list[str]:
    """Fetch Ollama server version and return a list of warning strings (empty = OK)."""
    warnings: list[str] = []
    server_ver_str = await client.get_version()
    server_ver = _parse_version(server_ver_str)
    min_str = ".".join(str(x) for x in MIN_OLLAMA_VERSION)

    if server_ver == (0, 0, 0):
        warnings.append(f"Could not determine Ollama server version.")
    elif server_ver < MIN_OLLAMA_VERSION:
        warnings.append(
            f"Ollama server version {server_ver_str} is below the recommended minimum {min_str}. "
            f"Known bugs: tool-call parsing, thinking field support. Run: ollama --version && ollama serve"
        )

    # Also check for CLI/server mismatch
    try:
        result = await asyncio.create_subprocess_exec(
            "ollama", "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await result.communicate()
        cli_output = (out or err or b"").decode().strip()
        m = re.search(r'(\d+\.\d+\.\d+)', cli_output)
        if m:
            cli_ver_str = m.group(1)
            cli_ver = _parse_version(cli_ver_str)
            if cli_ver != server_ver and server_ver != (0, 0, 0):
                warnings.append(
                    f"Ollama CLI version ({cli_ver_str}) differs from server version ({server_ver_str}). "
                    f"Restart 'ollama serve' to pick up the updated binary."
                )
    except FileNotFoundError:
        pass  # ollama CLI not on PATH

    return warnings


async def check_ollama(config: Config, client: OllamaClient) -> bool:
    """Check if Ollama is running. Returns True if healthy."""
    return await client.check_health()


async def check_model(
    config: Config,
    client: OllamaClient,
    prompt_fn: Callable[[str], str] | None = None,
    progress_fn: Callable[[str, int, int], None] | None = None,
) -> bool:
    """Check if the configured model is available. Optionally pull it.

    prompt_fn: called with a question string, should return user input (e.g., "y"/"n")
    progress_fn: called with (status, completed, total) during pull
    """
    model_name = config.model.name

    found, models = await client.has_model(model_name)
    if found:
        return True

    # Model not found
    available = [m["name"] for m in models]

    msg = f"Model '{model_name}' not found locally."
    if available:
        msg += f"\nAvailable models: {', '.join(available)}"
    msg += f"\n\nWould you like to pull '{model_name}'? [y/N] "

    if prompt_fn:
        answer = prompt_fn(msg)
    else:
        print(msg, end="", flush=True)
        try:
            answer = input()
        except EOFError:
            answer = "n"

    if answer.strip().lower() not in ("y", "yes"):
        return False

    # Pull the model
    print(f"Pulling {model_name}...", flush=True)

    def _default_progress(status: str, completed: int, total: int) -> None:
        if total > 0:
            pct = completed * 100 // total
            mb_done = completed // (1024 * 1024)
            mb_total = total // (1024 * 1024)
            print(f"\r  {status}: {mb_done}/{mb_total} MB ({pct}%)", end="", flush=True)
        else:
            print(f"\r  {status}", end="", flush=True)

    cb = progress_fn or _default_progress
    success = await client.pull_model(model_name, callback=cb)
    print()  # newline after progress

    if success:
        print(f"Successfully pulled {model_name}.")
    else:
        print(f"Failed to pull {model_name}.")

    return success


def print_install_instructions(config: Config) -> None:
    print(INSTALL_INSTRUCTIONS.format(url=config.ollama.base_url))


async def try_start_ollama(config: Config, client: OllamaClient) -> bool:
    """Detect how Ollama is installed, offer to start it, and poll until healthy.

    Decision tree:
      • brew-managed formula  → offer ``brew services start ollama``
      • ollama on PATH only   → offer ``ollama serve`` (detached background process)
      • not installed at all  → print install instructions and return False

    Returns True if Ollama is responding after the start attempt.
    """
    url = config.ollama.base_url
    ollama_bin = shutil.which("ollama")
    brew_bin = shutil.which("brew")
    brew_managed = False

    # Check whether ollama appears in `brew services list` (formula installs only).
    # This works even when Ollama is stopped — brew always lists registered services.
    if brew_bin and ollama_bin:
        try:
            r = await asyncio.create_subprocess_exec(
                brew_bin, "services", "list",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await r.communicate()
            brew_managed = any(
                line.split()[:1] == [b"ollama"]
                for line in out.splitlines()
            )
        except Exception:
            pass

    if not ollama_bin and not brew_managed:
        print_install_instructions(config)
        return False

    # --- Prompt user ---
    print(f"Ollama is not running at {url}.")
    if brew_managed:
        print("Start Ollama via Homebrew? (brew services start ollama) [Y/n] ", end="", flush=True)
    else:
        print("Start Ollama in the background? (ollama serve) [Y/n] ", end="", flush=True)

    try:
        answer = input()
    except EOFError:
        answer = "n"

    if answer.strip().lower() not in ("", "y", "yes"):
        return False

    # --- Start Ollama ---
    if brew_managed:
        proc = await asyncio.create_subprocess_exec(
            brew_bin, "services", "start", "ollama",  # type: ignore[arg-type]
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    else:
        # Detach so the serve process outlives this prompt and keeps running
        # after the parent process moves on.
        subprocess.Popen(
            [ollama_bin, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    # --- Poll until healthy (up to 15 s) ---
    print("Starting Ollama", end="", flush=True)
    deadline = time.monotonic() + 15.0
    delay = 0.5
    while time.monotonic() < deadline:
        if await client.check_health():
            print(" ✓")
            return True
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, 2.0)
        print(".", end="", flush=True)

    print()
    print("Timed out waiting for Ollama to start. Try running it manually.")
    return False
