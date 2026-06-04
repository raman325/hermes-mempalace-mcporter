"""Tests for ``plugin/client.py`` — the subprocess wrapper around mcporter.

The actual subprocess is replaced with a stub via ``monkeypatch`` so tests
don't need ``mcporter`` (or even ``npx``) on the host.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from plugin.client import (
    DEFAULT_SERVER,
    TOOL_PREFIX,
    McporterClient,
    McporterError,
    _detect_error,
)

# ---------------------------------------------------------------------------
# _detect_error
# ---------------------------------------------------------------------------


def test_detect_error_returns_none_for_success_payload():
    # Tools return raw dicts on success — no ``isError`` envelope.
    assert _detect_error({"total_drawers": 4}) is None


def test_detect_error_returns_none_for_non_dict():
    assert _detect_error("plain string") is None
    assert _detect_error([1, 2, 3]) is None
    assert _detect_error(None) is None


def test_detect_error_extracts_envelope_text():
    payload = {
        "content": [{"type": "text", "text": "Error: thing went wrong"}],
        "isError": True,
    }
    assert _detect_error(payload) == "Error: thing went wrong"


def test_detect_error_ignores_isError_falsy_envelope():
    # isError must be ``True`` specifically — ``False`` / missing / "true"
    # (string) all mean success.
    assert _detect_error({"content": [], "isError": False}) is None
    assert _detect_error({"content": [], "isError": "true"}) is None
    assert _detect_error({"isError": True}) == "mcporter tool call failed (no error text)"


# ---------------------------------------------------------------------------
# McporterClient.call — success path
# ---------------------------------------------------------------------------


def _fake_run(stdout="", stderr="", returncode=0):
    """Build a ``subprocess.CompletedProcess``-like return for monkeypatch."""

    def _run(cmd, **kwargs):
        _run.last_cmd = cmd
        _run.last_kwargs = kwargs
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    _run.last_cmd = None
    _run.last_kwargs = None
    return _run


def test_call_invokes_correct_selector_and_args(monkeypatch):
    fake = _fake_run(stdout=json.dumps({"total_drawers": 365}))
    monkeypatch.setattr(subprocess, "run", fake)

    client = McporterClient(command=["mcporter"])
    result = client.call("mempalace_status")

    assert result == {"total_drawers": 365}
    # Selector composition: server.tool_prefix + short_name
    assert fake.last_cmd[:5] == [
        "mcporter",
        "call",
        "mcphub.mempalace-mempalace_status",
        "--output",
        "json",
    ]


def test_call_serialises_args_as_json(monkeypatch):
    fake = _fake_run(stdout=json.dumps({"drawers": []}))
    monkeypatch.setattr(subprocess, "run", fake)

    client = McporterClient(command=["mcporter"])
    client.call("mempalace_list_drawers", {"wing": "raman_identity", "limit": 5})

    # ``--args`` carries a JSON-encoded dict.
    args_idx = fake.last_cmd.index("--args")
    parsed = json.loads(fake.last_cmd[args_idx + 1])
    assert parsed == {"wing": "raman_identity", "limit": 5}


def test_call_omits_args_flag_when_no_kwargs(monkeypatch):
    fake = _fake_run(stdout=json.dumps({}))
    monkeypatch.setattr(subprocess, "run", fake)

    client = McporterClient(command=["mcporter"])
    client.call("mempalace_status")
    assert "--args" not in fake.last_cmd


def test_call_returns_string_when_output_is_not_json(monkeypatch):
    fake = _fake_run(stdout="plain text response")
    monkeypatch.setattr(subprocess, "run", fake)

    client = McporterClient(command=["mcporter"])
    assert client.call("some_tool") == "plain text response"


def test_call_returns_none_for_empty_stdout(monkeypatch):
    fake = _fake_run(stdout="")
    monkeypatch.setattr(subprocess, "run", fake)
    assert McporterClient(command=["mcporter"]).call("noop") is None


def test_call_respects_custom_server_and_prefix(monkeypatch):
    fake = _fake_run(stdout=json.dumps({}))
    monkeypatch.setattr(subprocess, "run", fake)

    client = McporterClient(
        server="my-hub",
        tool_prefix="",
        command=["mcporter"],
    )
    client.call("some_tool")
    assert fake.last_cmd[2] == "my-hub.some_tool"


# ---------------------------------------------------------------------------
# McporterClient.call — error paths
# ---------------------------------------------------------------------------


def test_call_raises_on_isError_envelope(monkeypatch):
    fake = _fake_run(
        stdout=json.dumps(
            {
                "content": [{"type": "text", "text": "Error: bad args"}],
                "isError": True,
            }
        )
    )
    monkeypatch.setattr(subprocess, "run", fake)

    with pytest.raises(McporterError) as ei:
        McporterClient(command=["mcporter"]).call("bad_tool")
    assert "bad args" in str(ei.value)


def test_call_raises_on_nonzero_exit(monkeypatch):
    fake = _fake_run(stdout="", stderr="some error from cli", returncode=3)
    monkeypatch.setattr(subprocess, "run", fake)

    with pytest.raises(McporterError) as ei:
        McporterClient(command=["mcporter"]).call("any_tool")
    assert "exit=3" in str(ei.value)
    assert "some error from cli" in str(ei.value)


def test_call_raises_on_timeout(monkeypatch):
    def _raise_timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 30))

    monkeypatch.setattr(subprocess, "run", _raise_timeout)

    with pytest.raises(McporterError) as ei:
        McporterClient(command=["mcporter"], timeout_seconds=2).call("slow_tool")
    assert "timed out" in str(ei.value)


def test_call_raises_on_missing_binary(monkeypatch):
    def _raise_fnf(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(subprocess, "run", _raise_fnf)

    with pytest.raises(McporterError) as ei:
        McporterClient(command=["mcporter"]).call("any")
    assert "not found" in str(ei.value).lower()


def test_call_per_call_timeout_overrides_default(monkeypatch):
    fake = _fake_run(stdout=json.dumps({}))
    monkeypatch.setattr(subprocess, "run", fake)

    McporterClient(command=["mcporter"], timeout_seconds=30).call("tool", timeout_seconds=5)
    assert fake.last_kwargs.get("timeout") == 5


# ---------------------------------------------------------------------------
# Constants are not silently changed
# ---------------------------------------------------------------------------


def test_default_server_and_prefix_match_expected_setup():
    # If these values change, the live install on hermes breaks. Pinning
    # them in tests makes the contract explicit.
    assert DEFAULT_SERVER == "mcphub"
    assert TOOL_PREFIX == "mempalace-"
