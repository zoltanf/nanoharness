"""Tests for nanoharness/startup.py — startup checks with mocked client."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nanoharness.config import Config
from nanoharness.startup import check_ollama, check_model


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
