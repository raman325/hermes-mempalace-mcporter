"""Tests for ``plugin/__init__.py`` — the MempalaceMcporterProvider class.

The ``McporterClient`` is replaced with ``unittest.mock.Mock`` for these
tests so we never shell out. Live MCP behavior is covered by the smoke
test script (``/tmp/phase2_smoke.py``).
"""

from __future__ import annotations

import json
import threading
import time
from unittest.mock import Mock

import pytest

import plugin
from plugin import MempalaceMcporterProvider

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def provider():
    return MempalaceMcporterProvider()


@pytest.fixture
def mock_client_status():
    """Default mempalace_status response — what initialize() hits first."""
    return {
        "total_drawers": 365,
        "wings": {"raman_homelab": 228, "raman_identity": 4},
        "rooms": {"diary": 39},
        "protocol": "5-step palace protocol",
        "aaak_dialect": "AAAK dialect spec",
    }


@pytest.fixture
def patched_init(monkeypatch, mock_client_status):
    """Run ``initialize`` with the McporterClient stubbed out.

    Returns ``(provider, mock_client_instance)`` so the test can assert on
    individual calls and tweak return values per-tool.
    """

    def _make(side_effects=None):
        mock_client = Mock()

        # Default: status call returns the canonical wake-up payload.
        # Anything else returns an empty dict so background wake-up threads
        # finish without blowing up.
        def _call(tool, args=None, timeout_seconds=None):
            if side_effects and tool in side_effects:
                value = side_effects[tool]
                if isinstance(value, Exception):
                    raise value
                return value
            if tool == "mempalace_status":
                return mock_client_status
            return {}

        mock_client.call.side_effect = _call

        # Patch the constructor so initialize() picks up our mock.
        monkeypatch.setattr(plugin, "McporterClient", lambda **kw: mock_client)

        p = MempalaceMcporterProvider()
        p.initialize("test-session", platform="cli")
        # Wait briefly so the two wake-up background threads complete.
        # They each call the mock client once; the mock returns immediately,
        # so 0.1s is plenty.
        time.sleep(0.1)
        return p, mock_client

    return _make


# ---------------------------------------------------------------------------
# Shape: name, availability, schemas, schema gating
# ---------------------------------------------------------------------------


def test_name(provider):
    assert provider.name == "mempalace-mcporter"


def test_is_available_returns_true_without_init(provider):
    # mcporter availability is checked at init time, not here.
    assert provider.is_available() is True


# Mirrors the 19-tool surface mempalace's reference openclaw skill exposes
# (``mempalace/integrations/openclaw/SKILL.md``). Same shape, so a Hermes
# session and a Claude Code session interact with the palace through the
# same vocabulary.
EXPECTED_TOOL_NAMES = {
    # Search + structure
    "mempalace_search",
    "mempalace_status",
    "mempalace_list_wings",
    "mempalace_list_rooms",
    "mempalace_get_taxonomy",
    "mempalace_get_aaak_spec",
    # Drawer add / remove / dedup (append-first; no update/list/get)
    "mempalace_add_drawer",
    "mempalace_delete_drawer",
    "mempalace_check_duplicate",
    # Knowledge graph
    "mempalace_kg_query",
    "mempalace_kg_add",
    "mempalace_kg_invalidate",
    "mempalace_kg_timeline",
    "mempalace_kg_stats",
    # Per-agent diary (agent_name auto-injected to ``hermes``)
    "mempalace_diary_write",
    "mempalace_diary_read",
    # Room-graph navigation (discover-only; tunnels are created by mining)
    "mempalace_traverse",
    "mempalace_graph_stats",
    "mempalace_find_tunnels",
}


def test_tool_schemas_visible_before_initialize(provider):
    # Regression for the discovery bug: Hermes' ``agent.memory_manager``
    # snapshots ``get_tool_schemas()`` at registration time to build its
    # ``tool_name → provider`` routing table. If we returned ``[]`` there,
    # the dispatcher would never learn our tool names and every later call
    # would hit ``"Unknown tool: <name>"`` without reaching ``handle_tool_call``.
    # Backend readiness gating belongs in ``handle_tool_call``, not here.
    schemas = provider.get_tool_schemas()
    names = {s["name"] for s in schemas}
    assert names == EXPECTED_TOOL_NAMES


def test_tool_schemas_count_after_init(patched_init):
    p, _ = patched_init()
    schemas = p.get_tool_schemas()
    names = {s["name"] for s in schemas}
    assert names == EXPECTED_TOOL_NAMES


# ---------------------------------------------------------------------------
# Cron context guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"agent_context": "cron"},
        {"agent_context": "flush"},
        {"platform": "cron"},
    ],
)
def test_initialize_skips_under_cron_context(provider, monkeypatch, kwargs):
    # McporterClient must not even be constructed under cron.
    sentinel = Mock(side_effect=AssertionError("client should not be built under cron"))
    monkeypatch.setattr(plugin, "McporterClient", sentinel)

    provider.initialize("s1", **kwargs)

    assert provider._cron_skipped is True
    assert provider._initialized is False
    assert provider.get_tool_schemas() == []
    assert provider.system_prompt_block() == ""
    sentinel.assert_not_called()


def test_subsequent_non_cron_init_clears_cron_flag(provider, monkeypatch, mock_client_status):
    # First call: cron context. Second call: real session on the same
    # provider instance must not stay inert.
    mock_client = Mock()
    mock_client.call.return_value = mock_client_status
    monkeypatch.setattr(plugin, "McporterClient", lambda **kw: mock_client)

    provider.initialize("s1", agent_context="cron")
    assert provider._cron_skipped is True

    provider.initialize("s2", platform="cli")
    assert provider._cron_skipped is False
    assert provider._initialized is True


# ---------------------------------------------------------------------------
# Initialize success / failure
# ---------------------------------------------------------------------------


def test_initialize_sets_initialized_on_success(patched_init):
    p, _ = patched_init()
    assert p._initialized is True


def test_initialize_leaves_unitialized_when_status_fails(monkeypatch):
    # ``mempalace_status`` raising must NOT yield _initialized=True so that
    # ``handle_tool_call`` short-circuits with a structured "not initialized"
    # error rather than passing a bad call through to the backend.
    #
    # Note: ``get_tool_schemas()`` intentionally still returns the 8 schemas
    # in this state — they describe the *interface*. Hermes' tool router
    # snapshots them at registration time before ``initialize()`` runs, so
    # hiding them on failure would leave the router with no mapping and
    # every later call would return "Unknown tool" from Hermes itself
    # (never reaching ``handle_tool_call``).
    from plugin.client import McporterError

    mock_client = Mock()
    mock_client.call.side_effect = McporterError("backend unreachable")
    monkeypatch.setattr(plugin, "McporterClient", lambda **kw: mock_client)

    p = MempalaceMcporterProvider()
    p.initialize("s1", platform="cli")
    assert p._initialized is False
    # Schemas still advertised — see comment above.
    assert len(p.get_tool_schemas()) == len(EXPECTED_TOOL_NAMES)
    # But calls fail fast with a clear error so the model knows.
    result = json.loads(p.handle_tool_call("mempalace_status", {}))
    assert "error" in result
    assert "not initialized" in result["error"].lower()


# ---------------------------------------------------------------------------
# Wake-up composition (3 layers)
# ---------------------------------------------------------------------------


def test_system_prompt_block_composes_three_layers(patched_init):
    p, _ = patched_init(
        side_effects={
            "mempalace_list_drawers": {
                "drawers": [
                    {
                        "wing": "raman_identity",
                        "room": "bio",
                        "content_preview": "Raman bio preview",
                    },
                    {
                        "wing": "raman_identity",
                        "room": "guardrails",
                        "content_preview": "HARD GUARDRAILS preview",
                    },
                ],
                "total": 2,
            },
            "mempalace_diary_read": {
                "entries": [
                    {"topic": "session-1", "content": "what I did yesterday"},
                ],
            },
        }
    )
    block = p.system_prompt_block()
    assert "## Palace overview" in block
    assert "365" in block
    assert "## Memory protocol" in block
    assert "5-step palace protocol" in block
    assert "## Identity" in block
    assert "Raman bio preview" in block
    assert "HARD GUARDRAILS preview" in block
    assert "## Recent agent context" in block
    assert "what I did yesterday" in block


def test_identity_layer_reads_content_preview_field(patched_init):
    # Regression: mempalace_list_drawers returns ``content_preview``, not
    # ``content``. The smoke test caught this against the live palace.
    p, _ = patched_init(
        side_effects={
            "mempalace_list_drawers": {
                "drawers": [{"content_preview": "preview text"}],
            }
        }
    )
    assert "preview text" in p.system_prompt_block()


def test_recent_layer_reads_diary_content_field(patched_init):
    # Regression: diary entries store the text under ``content`` (not ``entry``).
    p, _ = patched_init(
        side_effects={
            "mempalace_diary_read": {
                "entries": [{"content": "what I did"}],
            }
        }
    )
    assert "what I did" in p.system_prompt_block()


def test_recent_layer_falls_back_to_legacy_entry_field(patched_init):
    # Defensive: if an older mempalace shape returns ``entry`` instead, we
    # still render it. Keeps the plugin compatible across versions.
    p, _ = patched_init(
        side_effects={
            "mempalace_diary_read": {
                "entries": [{"entry": "legacy shape text"}],
            }
        }
    )
    assert "legacy shape text" in p.system_prompt_block()


def test_system_prompt_block_empty_when_uninitialized(provider):
    assert provider.system_prompt_block() == ""


# ---------------------------------------------------------------------------
# Prefetch: cache drain + first-turn sync fallback + trivial-prompt skip
# ---------------------------------------------------------------------------


def test_prefetch_returns_empty_for_trivial_prompt(patched_init):
    p, _ = patched_init()
    assert p.prefetch("ok") == ""
    assert p.prefetch("/help") == ""
    assert p.prefetch("") == ""


def test_prefetch_returns_empty_when_not_initialized(provider):
    assert provider.prefetch("real query") == ""


def test_prefetch_drains_cached_result(patched_init):
    p, _ = patched_init()
    # Simulate ``queue_prefetch`` having stored a result on the prior turn.
    with p._prefetch_lock:
        p._prefetch_result = "cached context block"

    result = p.prefetch("any query")
    assert result == "cached context block"

    # Cache should be cleared after drain so a follow-up doesn't double-inject.
    assert p._prefetch_result == ""


def test_prefetch_runs_search_when_cache_empty(patched_init):
    p, _ = patched_init(
        side_effects={
            "mempalace_search": {
                "results": [
                    {
                        "wing": "raman_projects",
                        "room": "mempalace",
                        "text": "the search result text",
                    },
                ],
            },
        }
    )
    result = p.prefetch("tell me about mempalace")
    assert "the search result text" in result
    assert "[raman_projects/mempalace]" in result


# ---------------------------------------------------------------------------
# queue_prefetch (background)
# ---------------------------------------------------------------------------


def test_queue_prefetch_skips_trivial(patched_init):
    p, mock_client = patched_init()
    p.queue_prefetch("ok")
    # Give any (unwanted) thread a moment to fire.
    time.sleep(0.05)
    # No mempalace_search call should have been issued.
    assert all(c.args[0] != "mempalace_search" for c in mock_client.call.call_args_list)


def test_queue_prefetch_no_op_when_not_initialized(provider):
    # No exceptions, no threads started.
    provider.queue_prefetch("real query")
    assert provider._prefetch_thread is None


def test_queue_prefetch_fills_cache(patched_init):
    p, _ = patched_init(
        side_effects={
            "mempalace_search": {"results": [{"text": "background hit", "wing": "w"}]},
        }
    )
    p.queue_prefetch("background fetch this")
    # Wait for the daemon thread to populate the cache.
    p._prefetch_thread.join(timeout=2.0)
    with p._prefetch_lock:
        cached = p._prefetch_result
    assert "background hit" in cached


def test_queue_prefetch_skips_when_prior_in_flight(patched_init):
    p, mock_client = patched_init()
    # Pretend a prefetch thread is currently running.
    p._prefetch_thread = threading.Thread(target=lambda: time.sleep(0.5), daemon=True)
    p._prefetch_thread.start()
    p.queue_prefetch("would-be second query")
    # No new search call should have been initiated.
    p._prefetch_thread.join()
    assert all(c.args[0] != "mempalace_search" for c in mock_client.call.call_args_list)


# ---------------------------------------------------------------------------
# sync_turn → background worker → mempalace_add_drawer
# ---------------------------------------------------------------------------


def _wait_for_call(mock_client, tool_name, timeout=2.0):
    """Block until ``mock_client.call`` has been invoked with ``tool_name``."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(c.args[0] == tool_name for c in mock_client.call.call_args_list):
            return True
        time.sleep(0.02)
    return False


def test_sync_turn_files_drawer_via_worker(patched_init):
    p, mock_client = patched_init()
    p.sync_turn("what's up", "not much")
    assert _wait_for_call(mock_client, "mempalace_add_drawer"), (
        "add_drawer was never invoked by the worker"
    )
    p.shutdown()

    # Pull the add_drawer call to verify args.
    add = next(c for c in mock_client.call.call_args_list if c.args[0] == "mempalace_add_drawer")
    args = add.args[1]
    assert args["wing"] == "hermes"  # default; ``default_wing`` overrides per user
    assert args["room"] == "conversations"  # stable across all sessions
    assert "what's up" in args["content"]
    assert "not much" in args["content"]


def test_sync_turn_skips_both_empty(patched_init):
    p, mock_client = patched_init()
    pre = mock_client.call.call_count
    p.sync_turn("", "")
    time.sleep(0.05)
    p.shutdown()
    # No new calls were issued.
    assert mock_client.call.call_count == pre


def test_sync_turn_normalizes_list_content(patched_init):
    p, mock_client = patched_init()
    p.sync_turn(
        [{"type": "text", "text": "list-shape user"}],
        [{"type": "text", "text": "list-shape assistant"}],
    )
    assert _wait_for_call(mock_client, "mempalace_add_drawer")
    p.shutdown()

    add = next(c for c in mock_client.call.call_args_list if c.args[0] == "mempalace_add_drawer")
    content = add.args[1]["content"]
    # The content must contain the flattened text, not the literal repr.
    assert "list-shape user" in content
    assert "list-shape assistant" in content
    assert "{'type'" not in content


def test_sync_turn_honors_default_wing_env_override(patched_init):
    p, mock_client = patched_init()
    # Override the wing after init via config — exercises the same path env
    # vars hit at load time.
    p._config["default_wing"] = "raman_overridden"
    p.sync_turn("hi", "hello")
    assert _wait_for_call(mock_client, "mempalace_add_drawer")
    p.shutdown()

    add = next(c for c in mock_client.call.call_args_list if c.args[0] == "mempalace_add_drawer")
    assert add.args[1]["wing"] == "raman_overridden"


# ---------------------------------------------------------------------------
# Lifecycle hooks gated on readiness
# ---------------------------------------------------------------------------


def test_on_session_end_no_op_when_not_initialized(provider):
    pre = provider._worker_queue.qsize()
    provider.on_session_end([{"role": "user", "content": "hi"}])
    assert provider._worker_queue.qsize() == pre


def test_on_memory_write_no_op_when_not_initialized(provider):
    pre = provider._worker_queue.qsize()
    provider.on_memory_write("add", "user", "fact")
    assert provider._worker_queue.qsize() == pre


def test_on_memory_write_filters_non_user_targets(patched_init):
    p, mock_client = patched_init()
    pre = mock_client.call.call_count
    p.on_memory_write("add", "ai", "fact")  # target != user
    p.on_memory_write("replace", "user", "fact")  # action != add
    p.on_memory_write("add", "user", "")  # empty content
    time.sleep(0.05)
    p.shutdown()
    # No kg_add fired.
    assert all(c.args[0] != "mempalace_kg_add" for c in mock_client.call.call_args_list[pre:])


def test_on_memory_write_mirrors_to_kg_add(patched_init):
    p, mock_client = patched_init()
    p.on_memory_write("add", "user", "Raman likes coffee")
    assert _wait_for_call(mock_client, "mempalace_kg_add")
    p.shutdown()

    kg = next(c for c in mock_client.call.call_args_list if c.args[0] == "mempalace_kg_add")
    args = kg.args[1]
    assert args["subject"] == "user"
    assert args["predicate"] == "asserted"
    assert args["object"] == "Raman likes coffee"


def test_on_pre_compress_returns_hint_when_ready(patched_init):
    p, _ = patched_init()
    hint = p.on_pre_compress([{"role": "user", "content": "hi"}])
    assert isinstance(hint, str)
    assert "mempalace_search" in hint


def test_on_pre_compress_returns_empty_when_uninitialized(provider):
    assert provider.on_pre_compress([{"role": "user", "content": "hi"}]) == ""


def test_on_session_switch_invalidates_cache_on_reset(patched_init):
    p, _ = patched_init()
    with p._prefetch_lock:
        p._prefetch_result = "stale from prior session"
    p._turn_count = 5

    p.on_session_switch("new-id", reset=True)

    assert p._session_id == "new-id"
    assert p._turn_count == 0
    with p._prefetch_lock:
        assert p._prefetch_result == ""


def test_on_session_switch_preserves_cache_without_reset(patched_init):
    p, _ = patched_init()
    with p._prefetch_lock:
        p._prefetch_result = "still relevant"
    p._turn_count = 5

    p.on_session_switch("new-id", reset=False)

    assert p._session_id == "new-id"
    assert p._turn_count == 5  # not reset
    with p._prefetch_lock:
        assert p._prefetch_result == "still relevant"


# ---------------------------------------------------------------------------
# handle_tool_call dispatch
# ---------------------------------------------------------------------------


def test_tool_call_under_cron_returns_error(provider):
    provider._cron_skipped = True
    result = json.loads(provider.handle_tool_call("mempalace_search", {"query": "x"}))
    assert "error" in result


def test_tool_call_without_initialize_returns_error(provider):
    result = json.loads(provider.handle_tool_call("mempalace_status", {}))
    assert "error" in result


def test_tool_call_unknown_returns_error(patched_init):
    p, _ = patched_init()
    result = json.loads(p.handle_tool_call("mempalace_made_up_tool", {}))
    assert "error" in result


def test_diary_write_auto_injects_agent_name(patched_init):
    p, mock_client = patched_init(
        side_effects={"mempalace_diary_write": {"success": True}},
    )
    p.handle_tool_call("mempalace_diary_write", {"entry": "did things"})

    write_call = next(
        c for c in mock_client.call.call_args_list if c.args[0] == "mempalace_diary_write"
    )
    args = write_call.args[1]
    assert args["agent_name"] == "hermes"  # locked decision
    assert args["entry"] == "did things"


def test_diary_read_auto_injects_agent_name(patched_init):
    p, mock_client = patched_init(
        side_effects={"mempalace_diary_read": {"entries": []}},
    )
    p.handle_tool_call("mempalace_diary_read", {"last_n": 5})

    # ``_refresh_wakeup_recent`` also calls diary_read during init (with
    # last_n=3 for the wake-up cache); we want the call our handle_tool_call
    # made, which is the most recent one.
    read_calls = [c for c in mock_client.call.call_args_list if c.args[0] == "mempalace_diary_read"]
    args = read_calls[-1].args[1]
    assert args["agent_name"] == "hermes"
    assert args["last_n"] == 5


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


def test_shutdown_is_safe_without_initialize(provider):
    provider.shutdown()  # must not raise


def test_shutdown_drains_pending_writes(patched_init):
    p, mock_client = patched_init()
    p.sync_turn("u", "a")
    p.shutdown()
    # add_drawer was called during the drain.
    assert any(c.args[0] == "mempalace_add_drawer" for c in mock_client.call.call_args_list)
