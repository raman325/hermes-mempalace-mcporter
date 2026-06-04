"""Tests for the two pure helpers in ``plugin/__init__.py``.

``_is_trivial_prompt`` controls whether ``queue_prefetch`` fires a 1s MCP
call against a one-word ack; ``_normalize_content`` decides whether
Anthropic-format ``content`` lists become the literal ``repr`` of the list
(corrupting search) or get flattened to plain text.
"""

import pytest

from plugin import _is_trivial_prompt, _normalize_content

# ---------------------------------------------------------------------------
# _is_trivial_prompt
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "ok",
        "yes",
        "no",
        "thanks",
        "thank you",
        "lgtm",
        "/help",
        "/reset",
        "  /save  ",
    ],
)
def test_trivial_prompts_skip_prefetch(text):
    assert _is_trivial_prompt(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "what's the auth flow?",
        "tell me about Max",
        "why did we switch to GraphQL last month",
        "OK so what is the deploy command",  # starts with ok but is a real question
    ],
)
def test_substantive_prompts_trigger_prefetch(text):
    assert _is_trivial_prompt(text) is False


# ---------------------------------------------------------------------------
# _normalize_content
# ---------------------------------------------------------------------------


def test_normalize_passes_strings_through():
    assert _normalize_content("hello") == "hello"


def test_normalize_handles_empty():
    assert _normalize_content("") == ""
    assert _normalize_content(None) == ""
    assert _normalize_content([]) == ""


def test_normalize_flattens_anthropic_list():
    blocks = [
        {"type": "text", "text": "what is the auth flow?"},
        {"type": "tool_use", "name": "grep", "input": {"q": "JWT"}},
        {"type": "text", "text": "(short clarifier)"},
    ]
    out = _normalize_content(blocks)
    assert "what is the auth flow?" in out
    assert "[tool_use: grep]" in out
    assert "(short clarifier)" in out
    # Must not be the literal Python repr — that's the bug this prevents.
    assert "{'type'" not in out


def test_normalize_handles_tool_result_blocks():
    blocks = [{"type": "tool_result", "content": "found 3 matches"}]
    out = _normalize_content(blocks)
    assert "[tool_result]" in out
    assert "found 3 matches" in out


def test_normalize_handles_nested_tool_result_list():
    # Anthropic occasionally nests content lists inside tool_result blocks.
    blocks = [
        {
            "type": "tool_result",
            "content": [{"type": "text", "text": "nested OK"}],
        }
    ]
    out = _normalize_content(blocks)
    assert "nested OK" in out


def test_normalize_falls_back_for_unknown_block_with_text():
    blocks = [{"type": "weird_future_block", "text": "still has text"}]
    assert "still has text" in _normalize_content(blocks)


def test_normalize_skips_unknown_block_without_text():
    blocks = [{"type": "weird_future_block", "data": {"x": 1}}]
    # No ``text`` field to fall back on — should produce empty output, NOT
    # the literal repr.
    assert _normalize_content(blocks) == ""
