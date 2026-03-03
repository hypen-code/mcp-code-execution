"""Unit tests for the LLM enhancer module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from mce.compiler.llm_enhancer import enhance_with_llm
from mce.config import MCEConfig
from mce.errors import CompileError


def _make_config(api_key: str = "test-api-key", model: str = "gemini/gemini-2.0-flash") -> MCEConfig:
    return MCEConfig(
        llm_enhance=True,
        llm_api_key=api_key,
        llm_model=model,
    )


# ---------------------------------------------------------------------------
# Missing API key
# ---------------------------------------------------------------------------


async def test_enhance_raises_compile_error_when_api_key_missing() -> None:
    config = _make_config(api_key="")
    with pytest.raises(CompileError, match="MCE_LLM_API_KEY"):
        await enhance_with_llm("result = 1", "test_server", config)


# ---------------------------------------------------------------------------
# litellm not installed
# ---------------------------------------------------------------------------


async def test_enhance_raises_compile_error_when_litellm_not_installed() -> None:
    config = _make_config()
    with patch.dict("sys.modules", {"litellm": None}), pytest.raises((CompileError, ImportError)):
        await enhance_with_llm("result = 1", "test_server", config)


# ---------------------------------------------------------------------------
# Successful enhancement
# ---------------------------------------------------------------------------


async def test_enhance_returns_improved_code() -> None:
    config = _make_config()
    original_code = "def foo(x):\n    return x"
    enhanced_code = "def foo(x: int) -> int:\n    '''Enhanced function.'''\n    return x"

    mock_response = MagicMock()
    mock_response.choices[0].message.content = enhanced_code

    mock_litellm = MagicMock()
    mock_litellm.completion.return_value = mock_response

    with patch.dict("sys.modules", {"litellm": mock_litellm}):
        result = await enhance_with_llm(original_code, "test_server", config)

    assert result == enhanced_code
    mock_litellm.completion.assert_called_once()


async def test_enhance_passes_correct_model_and_key() -> None:
    config = _make_config(api_key="sk-or-test-key", model="openrouter/mistralai/mistral-7b")
    original_code = "result = 42"

    mock_response = MagicMock()
    mock_response.choices[0].message.content = original_code

    mock_litellm = MagicMock()
    mock_litellm.completion.return_value = mock_response

    with patch.dict("sys.modules", {"litellm": mock_litellm}):
        await enhance_with_llm(original_code, "my_server", config)

    call_kwargs = mock_litellm.completion.call_args
    assert call_kwargs.kwargs["model"] == "openrouter/mistralai/mistral-7b"
    assert call_kwargs.kwargs["api_key"] == "sk-or-test-key"


async def test_enhance_code_in_prompt_message() -> None:
    """The original code must appear in the LLM prompt."""
    config = _make_config()
    original_code = "def special_fn(): pass"

    mock_response = MagicMock()
    mock_response.choices[0].message.content = original_code

    mock_litellm = MagicMock()
    mock_litellm.completion.return_value = mock_response

    with patch.dict("sys.modules", {"litellm": mock_litellm}):
        await enhance_with_llm(original_code, "srv", config)

    call_kwargs = mock_litellm.completion.call_args
    messages = call_kwargs.kwargs["messages"]
    assert any("special_fn" in m.get("content", "") for m in messages)


# ---------------------------------------------------------------------------
# Fallback on LLM failure
# ---------------------------------------------------------------------------


async def test_enhance_falls_back_to_original_code_on_exception() -> None:
    config = _make_config()
    original_code = "result = 1"

    mock_litellm = MagicMock()
    mock_litellm.completion.side_effect = RuntimeError("API rate limit exceeded")

    with patch.dict("sys.modules", {"litellm": mock_litellm}):
        result = await enhance_with_llm(original_code, "test_server", config)

    # Falls back to original code on any exception
    assert result == original_code


async def test_enhance_strips_whitespace_from_response() -> None:
    config = _make_config()
    original_code = "def fn(): pass"
    enhanced_with_whitespace = "   def fn(): pass   \n"

    mock_response = MagicMock()
    mock_response.choices[0].message.content = enhanced_with_whitespace

    mock_litellm = MagicMock()
    mock_litellm.completion.return_value = mock_response

    with patch.dict("sys.modules", {"litellm": mock_litellm}):
        result = await enhance_with_llm(original_code, "srv", config)

    assert result == enhanced_with_whitespace.strip()
