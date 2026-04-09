"""Tests for nanoharness/config.py — layered configuration loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from nanoharness.config import (
    Config,
    ModelConfig,
    AgentConfig,
    OllamaConfig,
    SafetyConfig,
    WebConfig,
    ToolsConfig,
    TOOL_NAMES,
    _apply_toml,
    _apply_env,
    _apply_args,
    parse_args,
    load_config,
    write_config_toml,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Remove all NANO_* env vars to prevent leakage between tests."""
    import os
    for key in list(os.environ):
        if key.startswith("NANO_"):
            monkeypatch.delenv(key)


class TestDefaults:
    def test_default_config(self):
        cfg = Config()
        assert cfg.model.name == "gemma4:26b"
        assert cfg.model.thinking is False
        assert cfg.agent.max_steps == 25
        assert cfg.agent.max_output_chars == 8000
        assert cfg.agent.timeout_seconds == 30
        assert cfg.ollama.base_url == "http://localhost:11434"
        assert cfg.safety.level == "workspace"
        assert cfg.web.port == 8321
        assert cfg.web.host == "127.0.0.1"
        assert cfg.web.open_browser is True
        assert cfg.debug is False


class TestParseArgs:
    def test_no_args(self):
        args = parse_args([])
        assert args.model is None
        assert args.think is None
        assert args.web is False

    def test_model(self):
        args = parse_args(["--model", "llama3:8b"])
        assert args.model == "llama3:8b"

    def test_think(self):
        args = parse_args(["--think"])
        assert args.think is True

    def test_no_think(self):
        args = parse_args(["--no-think"])
        assert args.no_think is True

    def test_web_options(self):
        args = parse_args(["--web", "--port", "9999", "--no-open"])
        assert args.web is True
        assert args.port == 9999
        assert args.no_open is True

    def test_workspace_positional(self):
        args = parse_args(["/tmp"])
        assert args.workspace == "/tmp"


class TestApplyToml:
    def test_empty(self):
        cfg = Config()
        _apply_toml(cfg, {})
        assert cfg.model.name == "gemma4:26b"

    def test_model_section(self):
        cfg = Config()
        _apply_toml(cfg, {"model": {"name": "llama3", "thinking": True}})
        assert cfg.model.name == "llama3"
        assert cfg.model.thinking is True

    def test_agent_section(self):
        cfg = Config()
        _apply_toml(cfg, {"agent": {"max_steps": 10, "timeout_seconds": 60}})
        assert cfg.agent.max_steps == 10
        assert cfg.agent.timeout_seconds == 60

    def test_all_sections(self):
        cfg = Config()
        data = {
            "model": {"name": "test"},
            "agent": {"max_steps": 5},
            "ollama": {"base_url": "http://other:1234"},
            "safety": {"level": "unrestricted"},
            "web": {"port": 9000, "host": "0.0.0.0"},
        }
        _apply_toml(cfg, data)
        assert cfg.model.name == "test"
        assert cfg.agent.max_steps == 5
        assert cfg.ollama.base_url == "http://other:1234"
        assert cfg.safety.level == "unrestricted"
        assert cfg.web.port == 9000


class TestApplyEnv:
    def test_model(self, monkeypatch):
        cfg = Config()
        monkeypatch.setenv("NANO_MODEL", "phi3")
        _apply_env(cfg)
        assert cfg.model.name == "phi3"

    def test_thinking(self, monkeypatch):
        cfg = Config()
        monkeypatch.setenv("NANO_THINKING", "true")
        _apply_env(cfg)
        assert cfg.model.thinking is True

    def test_max_steps(self, monkeypatch):
        cfg = Config()
        monkeypatch.setenv("NANO_MAX_STEPS", "10")
        _apply_env(cfg)
        assert cfg.agent.max_steps == 10

    def test_safety(self, monkeypatch):
        cfg = Config()
        monkeypatch.setenv("NANO_SAFETY", "unrestricted")
        _apply_env(cfg)
        assert cfg.safety.level == "unrestricted"

    def test_debug(self, monkeypatch):
        cfg = Config()
        monkeypatch.setenv("NANO_DEBUG", "1")
        _apply_env(cfg)
        assert cfg.debug is True


class TestApplyArgs:
    def test_none_values_no_override(self):
        cfg = Config()
        args = parse_args([])
        _apply_args(cfg, args)
        assert cfg.model.name == "gemma4:26b"

    def test_set_values_override(self):
        cfg = Config()
        args = parse_args(["--model", "test", "--max-steps", "5"])
        _apply_args(cfg, args)
        assert cfg.model.name == "test"
        assert cfg.agent.max_steps == 5

    def test_workspace_resolves(self, tmp_path: Path):
        cfg = Config()
        args = parse_args([str(tmp_path)])
        _apply_args(cfg, args)
        assert cfg.workspace == tmp_path.resolve()


class TestPrecedence:
    def test_cli_overrides_env(self, monkeypatch):
        monkeypatch.setenv("NANO_MODEL", "from_env")
        cfg, _ = load_config(["--model", "from_cli"])
        assert cfg.model.name == "from_cli"

    def test_env_overrides_default(self, monkeypatch):
        monkeypatch.setenv("NANO_SAFETY", "unrestricted")
        cfg, _ = load_config([])
        assert cfg.safety.level == "unrestricted"

    def test_toml_overrides_default(self, tmp_path: Path):
        toml_file = tmp_path / "config.toml"
        toml_file.write_text('[model]\nname = "from_toml"\n')
        cfg, _ = load_config(["--config", str(toml_file)])
        assert cfg.model.name == "from_toml"

    def test_full_stack(self, tmp_path: Path, monkeypatch):
        toml_file = tmp_path / "config.toml"
        toml_file.write_text('[model]\nname = "from_toml"\n')
        monkeypatch.setenv("NANO_SAFETY", "confirm")
        cfg, _ = load_config([
            "--config", str(toml_file),
            "--max-steps", "3",
        ])
        assert cfg.model.name == "from_toml"  # from TOML
        assert cfg.safety.level == "confirm"  # from env
        assert cfg.agent.max_steps == 3  # from CLI


class TestToolsConfig:
    def test_defaults_all_enabled(self):
        cfg = ToolsConfig()
        for name in TOOL_NAMES:
            assert getattr(cfg, name) is True

    def test_tool_names_matches_fields(self):
        cfg = ToolsConfig()
        for name in TOOL_NAMES:
            assert hasattr(cfg, name), f"ToolsConfig missing field: {name}"

    def test_config_has_tools(self):
        cfg = Config()
        assert isinstance(cfg.tools, ToolsConfig)
        assert cfg.tools.bash is True


class TestApplyTomlTools:
    def test_disable_one_tool(self):
        cfg = Config()
        _apply_toml(cfg, {"tools": {"bash": False}})
        assert cfg.tools.bash is False
        assert cfg.tools.python_exec is True

    def test_enable_already_enabled(self):
        cfg = Config()
        _apply_toml(cfg, {"tools": {"read_file": True}})
        assert cfg.tools.read_file is True

    def test_disable_multiple(self):
        cfg = Config()
        _apply_toml(cfg, {"tools": {"bash": False, "python_exec": False, "fetch_webpage": False}})
        assert cfg.tools.bash is False
        assert cfg.tools.python_exec is False
        assert cfg.tools.fetch_webpage is False
        assert cfg.tools.todo is True

    def test_unknown_tool_ignored(self):
        cfg = Config()
        _apply_toml(cfg, {"tools": {"nonexistent": False}})
        for name in TOOL_NAMES:
            assert getattr(cfg.tools, name) is True

    def test_empty_tools_section(self):
        cfg = Config()
        _apply_toml(cfg, {"tools": {}})
        for name in TOOL_NAMES:
            assert getattr(cfg.tools, name) is True


class TestWriteConfigTomlTools:
    def test_tools_section_written(self, tmp_path: Path):
        cfg = Config()
        path = tmp_path / "config.toml"
        write_config_toml(cfg, path)
        content = path.read_text()
        assert "[tools]" in content
        for name in TOOL_NAMES:
            assert f"{name} = true" in content

    def test_disabled_tool_written(self, tmp_path: Path):
        cfg = Config()
        cfg.tools.bash = False
        cfg.tools.python_exec = False
        path = tmp_path / "config.toml"
        write_config_toml(cfg, path)
        content = path.read_text()
        assert "bash = false" in content
        assert "python_exec = false" in content
        assert "read_file = true" in content

    def test_roundtrip(self, tmp_path: Path):
        cfg = Config()
        cfg.tools.bash = False
        cfg.tools.fetch_webpage = False
        path = tmp_path / "config.toml"
        write_config_toml(cfg, path)

        import tomllib
        with open(path, "rb") as f:
            data = tomllib.load(f)
        cfg2 = Config()
        _apply_toml(cfg2, data)
        assert cfg2.tools.bash is False
        assert cfg2.tools.fetch_webpage is False
        assert cfg2.tools.read_file is True
