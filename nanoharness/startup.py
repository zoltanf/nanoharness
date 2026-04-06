"""Startup checks: Ollama availability and model presence."""

from __future__ import annotations

import sys
from typing import Callable

from .config import Config
from .ollama import OllamaClient


INSTALL_INSTRUCTIONS = """
Ollama is not running at {url}.

To install Ollama:
  macOS:   brew install ollama   (or download from https://ollama.com)
  Linux:   curl -fsSL https://ollama.com/install.sh | sh
  Windows: Download from https://ollama.com/download

After installing, start Ollama:
  ollama serve
"""


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
