"""Layered configuration: CLI flags > env vars > TOML config > defaults."""

from __future__ import annotations

import argparse
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path.home() / ".nanoharness"
CONFIG_FILE = CONFIG_DIR / "config.toml"

WARN_SAFETY_NONE = (
    "WARNING: safety=none — workspace containment and environment scrubbing are "
    "disabled. The agent can read/write any file and run unrestricted commands."
)
WARN_DEBUG_ON = (
    "Debug logging is ON — tool arguments, file contents, and conversation messages "
    "are written to ~/.nanoharness/debug/"
)


@dataclass
class ModelConfig:
    name: str = "gemma4:26b"
    thinking: bool = False
    num_ctx: int = 0  # 0 = use Ollama's default for the model


@dataclass
class AgentConfig:
    max_steps: int = 25
    max_output_chars: int = 8000
    timeout_seconds: int = 30


@dataclass
class OllamaConfig:
    base_url: str = "http://localhost:11434"


@dataclass
class SafetyConfig:
    level: str = "workspace"  # "confirm" | "workspace" | "none"


@dataclass
class WebConfig:
    port: int = 8321
    host: str = "127.0.0.1"
    open_browser: bool = True


@dataclass
class Config:
    model: ModelConfig = field(default_factory=ModelConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    web: WebConfig = field(default_factory=WebConfig)
    workspace: Path = field(default_factory=Path.cwd)
    debug: bool = False


def _load_toml(path: Path) -> dict:
    if not path.is_file():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def _apply_toml(cfg: Config, data: dict) -> None:
    m = data.get("model", {})
    if "name" in m:
        cfg.model.name = m["name"]
    if "thinking" in m:
        cfg.model.thinking = bool(m["thinking"])
    if "num_ctx" in m:
        cfg.model.num_ctx = int(m["num_ctx"])

    a = data.get("agent", {})
    if "max_steps" in a:
        cfg.agent.max_steps = int(a["max_steps"])
    if "max_output_chars" in a:
        cfg.agent.max_output_chars = int(a["max_output_chars"])
    if "timeout_seconds" in a:
        cfg.agent.timeout_seconds = int(a["timeout_seconds"])

    o = data.get("ollama", {})
    if "base_url" in o:
        cfg.ollama.base_url = o["base_url"]

    s = data.get("safety", {})
    if "level" in s:
        cfg.safety.level = s["level"]

    w = data.get("web", {})
    if "port" in w:
        cfg.web.port = int(w["port"])
    if "host" in w:
        cfg.web.host = w["host"]


def _apply_env(cfg: Config) -> None:
    if v := os.environ.get("NANO_MODEL"):
        cfg.model.name = v
    if v := os.environ.get("NANO_THINKING"):
        cfg.model.thinking = v.lower() in ("1", "true", "yes")
    if v := os.environ.get("NANO_NUM_CTX"):
        cfg.model.num_ctx = int(v)
    if v := os.environ.get("NANO_MAX_STEPS"):
        cfg.agent.max_steps = int(v)
    if v := os.environ.get("NANO_TIMEOUT"):
        cfg.agent.timeout_seconds = int(v)
    if v := os.environ.get("NANO_OLLAMA_URL"):
        cfg.ollama.base_url = v
    if v := os.environ.get("NANO_SAFETY"):
        cfg.safety.level = v
    if v := os.environ.get("NANO_DEBUG"):
        cfg.debug = v.lower() in ("1", "true", "yes")
    if v := os.environ.get("NANO_WEB_PORT"):
        cfg.web.port = int(v)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="nanoharness",
        description="Lightweight AI coding agent for local LLMs",
    )
    p.add_argument("workspace", nargs="?", default=None, help="Working directory")
    p.add_argument("--model", default=None, help="Ollama model name")
    p.add_argument("--think", action="store_true", default=None, help="Enable thinking mode")
    p.add_argument("--no-think", action="store_true", default=None, help="Disable thinking mode")
    p.add_argument("--max-steps", type=int, default=None, help="Max agent steps per turn")
    p.add_argument("--safety", choices=["confirm", "workspace", "none"], default=None)
    p.add_argument("--config", type=Path, default=None, help="Config file path")
    p.add_argument("--debug", action="store_true", default=False, help="Enable debug logging to ~/.nanoharness/debug/")
    p.add_argument("--tui", action="store_true", default=True, help="Launch Textual TUI (default)")
    p.add_argument("--repl", action="store_true", default=False, help="Launch basic REPL instead of TUI")
    p.add_argument("--web", action="store_true", default=False, help="Launch web UI in browser")
    p.add_argument("--port", type=int, default=None, help="Web UI port (default: 8321)")
    p.add_argument("--app", action="store_true", default=False, help="Launch as desktop app (native webview window)")
    p.add_argument("--no-open", action="store_true", default=False, help="Don't auto-open browser for web UI")
    p.add_argument("--num-ctx", type=int, default=None, help="Context window size (tokens); 0 = model default")
    return p.parse_args(argv)


def _apply_args(cfg: Config, args: argparse.Namespace) -> None:
    if args.model is not None:
        cfg.model.name = args.model
    if args.think:
        cfg.model.thinking = True
    if args.no_think:
        cfg.model.thinking = False
    if args.max_steps is not None:
        cfg.agent.max_steps = args.max_steps
    if args.safety is not None:
        cfg.safety.level = args.safety
    if args.workspace is not None:
        cfg.workspace = Path(args.workspace).resolve()
    if args.debug:
        cfg.debug = True
    if args.port is not None:
        cfg.web.port = args.port
    if args.no_open:
        cfg.web.open_browser = False
    if args.num_ctx is not None:
        cfg.model.num_ctx = args.num_ctx


def write_config_toml(cfg: Config, path: Path = CONFIG_FILE) -> None:
    """Serialize Config to TOML and write to path (creates parent dirs as needed)."""
    lines = [
        "[model]",
        f'name = "{cfg.model.name}"',
        f"thinking = {str(cfg.model.thinking).lower()}",
        f"num_ctx = {cfg.model.num_ctx}",
        "",
        "[agent]",
        f"max_steps = {cfg.agent.max_steps}",
        f"max_output_chars = {cfg.agent.max_output_chars}",
        f"timeout_seconds = {cfg.agent.timeout_seconds}",
        "",
        "[ollama]",
        f'base_url = "{cfg.ollama.base_url}"',
        "",
        "[safety]",
        f'level = "{cfg.safety.level}"',
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))


def load_config(argv: list[str] | None = None) -> tuple[Config, argparse.Namespace]:
    """Load config with precedence: CLI > env > TOML > defaults."""
    args = parse_args(argv)
    cfg = Config()

    config_path = args.config or CONFIG_FILE
    toml_data = _load_toml(config_path)
    _apply_toml(cfg, toml_data)
    _apply_env(cfg)
    _apply_args(cfg, args)

    return cfg, args
