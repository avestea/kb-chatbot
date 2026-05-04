"""Tests for Slice 13 — Query Rewriting (rewrite_query)."""
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from src.rag.rewrite import rewrite_query


# ---------------------------------------------------------------------------
# No history — short-circuit path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_history_returns_original_without_api_call():
    """Empty history: returns original message immediately, no LLM call."""
    with patch("src.rag.rewrite._client.messages.create") as mock_create:
        result = await rewrite_query("What is the refund policy?", [])

    assert result == "What is the refund policy?"
    mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_none_like_empty_history_returns_original():
    """Explicitly empty list is the only falsy history value; message returned unchanged."""
    result = await rewrite_query("hello", [])
    assert result == "hello"


# ---------------------------------------------------------------------------
# Happy path — LLM rewrites the query
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_with_history_rewrites_via_anthropic():
    """Non-empty history triggers LLM call; rewritten text is returned."""
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="What is the refund policy for digital goods?")]

    with patch("src.rag.rewrite._client.messages.create", new=AsyncMock(return_value=mock_response)):
        result = await rewrite_query(
            "And for digital goods?",
            [
                {"role": "user", "content": "What is the refund policy?"},
                {"role": "assistant", "content": "Our refund policy allows returns within 30 days."},
            ],
        )

    assert result == "What is the refund policy for digital goods?"


@pytest.mark.asyncio
async def test_rewrite_strips_whitespace():
    """Trailing/leading whitespace in LLM output is stripped."""
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="  What is the subscription refund policy?  \n")]

    with patch("src.rag.rewrite._client.messages.create", new=AsyncMock(return_value=mock_response)):
        result = await rewrite_query(
            "And subscriptions?",
            [{"role": "user", "content": "Tell me about refunds."}],
        )

    assert result == "What is the subscription refund policy?"


# ---------------------------------------------------------------------------
# Fallback behaviour
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_exception_falls_back_to_original():
    """Any exception from the Anthropic API returns the original message."""
    with patch("src.rag.rewrite._client.messages.create", side_effect=Exception("API error")):
        result = await rewrite_query(
            "And what about that?",
            [{"role": "user", "content": "Tell me about refunds."}],
        )

    assert result == "And what about that?"


@pytest.mark.asyncio
async def test_empty_rewrite_response_falls_back_to_original():
    """Empty string from LLM falls back to the original message."""
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="")]

    with patch("src.rag.rewrite._client.messages.create", new=AsyncMock(return_value=mock_response)):
        result = await rewrite_query(
            "And for subscriptions?",
            [{"role": "user", "content": "Refund policy?"}],
        )

    assert result == "And for subscriptions?"


# ---------------------------------------------------------------------------
# History truncation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_history_truncated_to_last_6_turns():
    """Only the last 6 turns are included in the transcript sent to the LLM."""
    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"Turn {i}"}
        for i in range(10)
    ]

    captured: list[dict] = []

    async def mock_create(**kwargs):
        captured.append(kwargs)
        resp = MagicMock()
        resp.content = [MagicMock(text="rewritten query")]
        return resp

    with patch("src.rag.rewrite._client.messages.create", new=mock_create):
        await rewrite_query("new message", history)

    assert len(captured) == 1
    prompt_text = captured[0]["messages"][0]["content"]
    # Last 6 turns are indices 4–9
    assert "Turn 4" in prompt_text
    assert "Turn 9" in prompt_text
    # Early turns (0–3) must not appear
    assert "Turn 0" not in prompt_text
    assert "Turn 3" not in prompt_text


@pytest.mark.asyncio
async def test_history_under_6_uses_all_turns():
    """When history has fewer than 6 turns, all are used."""
    history = [
        {"role": "user", "content": "Question one"},
        {"role": "assistant", "content": "Answer one"},
    ]

    captured: list[dict] = []

    async def mock_create(**kwargs):
        captured.append(kwargs)
        resp = MagicMock()
        resp.content = [MagicMock(text="rewritten")]
        return resp

    with patch("src.rag.rewrite._client.messages.create", new=mock_create):
        await rewrite_query("Follow up", history)

    prompt_text = captured[0]["messages"][0]["content"]
    assert "Question one" in prompt_text
    assert "Answer one" in prompt_text


# ---------------------------------------------------------------------------
# Prompt structure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prompt_contains_transcript_and_new_message():
    """The prompt sent to the LLM contains both the history transcript and the new message."""
    captured: list[dict] = []

    async def mock_create(**kwargs):
        captured.append(kwargs)
        resp = MagicMock()
        resp.content = [MagicMock(text="rewritten")]
        return resp

    history = [{"role": "user", "content": "What is the warranty?"}]
    with patch("src.rag.rewrite._client.messages.create", new=mock_create):
        await rewrite_query("And for repairs?", history)

    prompt_text = captured[0]["messages"][0]["content"]
    assert "What is the warranty?" in prompt_text
    assert "And for repairs?" in prompt_text
    assert captured[0]["model"] == "claude-haiku-4-5-20251001"
    assert captured[0]["max_tokens"] == 128
