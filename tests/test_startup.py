"""Tests for nanoharness/startup.py — startup checks with mocked client."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanoharness.config import Config
from nanoharness.startup import check_ollama, check_model, try_start_ollama


class TestCheckOllama:
    @pytest.mark.asyncio
    async def test_healthy(self, config: Config, mock_client: AsyncMock):
        mock_client.check_health = AsyncMock(return_value=True)
        assert await check_ollama(config, mock_client) is True

    @pytest.mark.asyncio
    async def test_unhealthy(self, config: Config, mock_client: AsyncMock):
        mock_client.check_health = AsyncMock(return_value=False)
        assert await check_ollama(config, mock_client) is False


class TestCheckModel:
    @pytest.mark.asyncio
    async def test_model_present(self, config: Config, mock_client: AsyncMock):
        mock_client.has_model = AsyncMock(return_value=(True, [{"name": "gemma4:26b"}]))
        assert await check_model(config, mock_client) is True

    @pytest.mark.asyncio
    async def test_model_absent_user_declines(self, config: Config, mock_client: AsyncMock):
        mock_client.has_model = AsyncMock(return_value=(False, []))
        prompt_fn = lambda msg: "n"
        assert await check_model(config, mock_client, prompt_fn=prompt_fn) is False

    @pytest.mark.asyncio
    async def test_model_absent_user_accepts(self, config: Config, mock_client: AsyncMock):
        mock_client.has_model = AsyncMock(return_value=(False, []))
        mock_client.pull_model = AsyncMock(return_value=True)
        prompt_fn = lambda msg: "y"
        assert await check_model(config, mock_client, prompt_fn=prompt_fn) is True

    @pytest.mark.asyncio
    async def test_pull_fails(self, config: Config, mock_client: AsyncMock):
        mock_client.has_model = AsyncMock(return_value=(False, []))
        mock_client.pull_model = AsyncMock(return_value=False)
        prompt_fn = lambda msg: "y"
        assert await check_model(config, mock_client, prompt_fn=prompt_fn) is False


class TestTryStartOllama:
    """Tests for the 'offer to start Ollama' prompt+launch flow."""

    async def _run(self, config, mock_client, *, brew_bin, ollama_bin,
                   brew_services_output: bytes, user_answer: str,
                   health_side_effect=None):
        """Helper: patch shutil.which, subprocess calls, and input(), then call try_start_ollama."""
        import asyncio

        async def fake_brew_services(*args, **kwargs):
            proc = MagicMock()
            proc.communicate = AsyncMock(return_value=(brew_services_output, b""))
            proc.wait = AsyncMock(return_value=0)
            return proc

        with (
            patch("nanoharness.startup.shutil") as mock_shutil,
            patch("nanoharness.startup.asyncio.create_subprocess_exec", side_effect=fake_brew_services),
            patch("nanoharness.startup.subprocess.Popen") as mock_popen,
            patch("builtins.input", return_value=user_answer),
        ):
            mock_shutil.which = lambda name: brew_bin if name == "brew" else ollama_bin
            if health_side_effect is not None:
                mock_client.check_health = AsyncMock(side_effect=health_side_effect)

            result = await try_start_ollama(config, mock_client)

        return result, mock_popen

    async def test_not_installed_shows_instructions(self, config: Config, mock_client: AsyncMock, capsys):
        """When ollama is absent from PATH and not brew-managed, prints install instructions."""
        result, _ = await self._run(
            config, mock_client,
            brew_bin=None,
            ollama_bin=None,
            brew_services_output=b"",
            user_answer="y",
        )
        assert result is False
        captured = capsys.readouterr()
        assert "install" in captured.out.lower() or "ollama.com" in captured.out

    async def test_user_declines_returns_false(self, config: Config, mock_client: AsyncMock):
        """When user answers 'n', returns False without starting anything."""
        result, mock_popen = await self._run(
            config, mock_client,
            brew_bin="/usr/local/bin/brew",
            ollama_bin="/usr/local/bin/ollama",
            brew_services_output=b"Name    Status\nollama  stopped\n",
            user_answer="n",
        )
        assert result is False
        mock_popen.assert_not_called()

    async def test_brew_managed_starts_brew_service(self, config: Config, mock_client: AsyncMock):
        """brew-managed install: starts via brew services and returns True when healthy."""
        mock_client.check_health = AsyncMock(return_value=True)
        result, mock_popen = await self._run(
            config, mock_client,
            brew_bin="/usr/local/bin/brew",
            ollama_bin="/usr/local/bin/ollama",
            brew_services_output=b"Name    Status\nollama  stopped\n",
            user_answer="y",
        )
        # brew services start is via create_subprocess_exec, NOT Popen
        mock_popen.assert_not_called()
        assert result is True

    async def test_path_only_starts_popen(self, config: Config, mock_client: AsyncMock):
        """Non-brew install: starts via Popen (detached ollama serve) and returns True."""
        mock_client.check_health = AsyncMock(return_value=True)
        result, mock_popen = await self._run(
            config, mock_client,
            brew_bin=None,
            ollama_bin="/usr/local/bin/ollama",
            brew_services_output=b"",
            user_answer="y",
        )
        mock_popen.assert_called_once()
        # Confirm start_new_session=True is set (detached process)
        _, kwargs = mock_popen.call_args
        assert kwargs.get("start_new_session") is True
        assert result is True

    async def test_start_timeout_returns_false(self, config: Config, mock_client: AsyncMock, capsys):
        """If Ollama never becomes healthy within the deadline, returns False."""
        import time

        call_count = 0
        start = time.monotonic()

        async def never_healthy():
            nonlocal call_count
            call_count += 1
            return False

        with (
            patch("nanoharness.startup.shutil") as mock_shutil,
            patch("nanoharness.startup.asyncio.create_subprocess_exec") as mock_exec,
            patch("nanoharness.startup.subprocess.Popen"),
            patch("builtins.input", return_value="y"),
            patch("nanoharness.startup.time") as mock_time,
        ):
            mock_shutil.which = lambda name: None if name == "brew" else "/usr/bin/ollama"
            mock_client.check_health = AsyncMock(return_value=False)

            # Make time.monotonic advance past the 15 s deadline immediately on 2nd call
            _real_time = time.monotonic()
            calls = [0]

            def fake_monotonic():
                calls[0] += 1
                # First call (deadline = X + 15): return base time
                # Subsequent calls (loop condition): return past deadline
                return _real_time if calls[0] <= 1 else _real_time + 20.0

            mock_time.monotonic = fake_monotonic

            result = await try_start_ollama(config, mock_client)

        assert result is False
        captured = capsys.readouterr()
        assert "timed out" in captured.out.lower()
