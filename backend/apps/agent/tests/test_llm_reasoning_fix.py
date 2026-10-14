"""Regression test for DeepSeek reasoning model routing bug.

Bug: DeepSeek reasoning models return empty content field, causing
Supervisor to fail intent parsing and return _free_chat instead of routing.

Fix: LLMClient now fallback to reasoning_content when content is empty.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from apps.agent.llm_client import LLMClient


@pytest.mark.asyncio
async def test_deepseek_reasoning_model_empty_content():
    """Test that LLMClient handles DeepSeek reasoning model response with empty content"""

    # Mock OpenAI API response (DeepSeek R1 style)
    mock_response_data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",  # Empty content (bug trigger)
                    "reasoning_content": '{"agent": "analyst", "message": "test"}',
                }
            }
        ]
    }

    client = LLMClient.get_instance()

    # Mock httpx.AsyncClient context manager
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = mock_response_data

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await client.chat(
            system="test system",
            user="test user",
            max_tokens=256,
            temperature=0.1,
        )

        # Should return reasoning_content, not empty content
        assert result == '{"agent": "analyst", "message": "test"}'
        assert result != ""


@pytest.mark.asyncio
async def test_normal_response_with_content():
    """Test that LLMClient still works with normal responses having content"""

    mock_response_data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": '{"agent": "quant"}',
                }
            }
        ]
    }

    client = LLMClient.get_instance()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = mock_response_data

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await client.chat(
            system="test",
            user="test",
            max_tokens=256,
            temperature=0.1,
        )

        # Should use content when available
        assert result == '{"agent": "quant"}'
