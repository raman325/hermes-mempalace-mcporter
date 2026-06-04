"""Thin subprocess wrapper around ``npx mcporter call <server>.<tool>``.

mcporter is the Node.js CLI that proxies MCP tool calls to configured servers.
Our plugin uses it to reach mempalace through the user's mcphub aggregator —
``mcphub.mempalace-mempalace_search``, ``mcphub.mempalace-mempalace_add_drawer``,
and so on.

Latency budget: ``npx mcporter`` cold-starts in ~2s; that cost is mostly
invisible at runtime because ``queue_prefetch`` runs prefetches in the
background between turns. Wake-up data fetches at ``initialize`` time also
run on background threads. The agent loop never blocks on this client.

Error shape:

* Success → mcporter unwraps the MCP envelope and prints the tool's raw
  result. JSON-decoded into a ``dict`` (or returned as a stripped string
  for non-JSON responses).
* Tool failure → mcporter returns the MCP envelope verbatim:
  ``{"content": [{"type": "text", "text": "Error: …"}], "isError": true}``.
  We detect ``isError: true`` and raise :class:`McporterError`.
* Subprocess failure (non-zero exit, timeout) → :class:`McporterError`.

Detection of the two output shapes happens entirely on this side; mcporter's
exit code is unreliable (it exits 0 for tool errors that show ``isError``).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional

logger = logging.getLogger("mempalace_mcporter.client")


# Default mcporter server name. Matches what you'd get if you ran
# ``mcporter config add mempalace ...`` to register mempalace's MCP server
# directly (no aggregator in between). Override when behind an aggregator
# like mcphub via the provider's ``mcporter_server`` config key.
DEFAULT_SERVER = "mempalace"

# Default tool prefix. Empty means tools are addressed by their bare name
# (``mempalace_status``, not ``mempalace-mempalace_status``) — the case when
# mcporter talks to mempalace directly. Aggregators that namespace tools by
# source server (mcphub uses ``<server>-`` prefixes) need this set to match.
TOOL_PREFIX = ""

# Hard ceiling on call duration. Anything longer than this is almost
# certainly a hung subprocess; better to fail fast than block the
# background worker indefinitely.
DEFAULT_TIMEOUT_SECONDS = 30


class McporterError(RuntimeError):
    """Raised when an mcporter invocation fails (subprocess or tool error)."""


def _resolve_command() -> List[str]:
    """Pick the mcporter entrypoint, preferring a global install over npx.

    ``mcporter`` directly on ``PATH`` skips the ``npx`` cold start (~2s
    saving per call). Most installs use ``npx`` though, so we fall back to
    that automatically.
    """
    if shutil.which("mcporter"):
        return ["mcporter"]
    return ["npx", "mcporter"]


def _detect_error(payload: Any) -> Optional[str]:
    """Return the MCP error text if ``payload`` is a wrapped error.

    mcporter prints ``{"content": [{"type":"text","text":"Error: …"}],
    "isError": true}`` on tool failures. Success cases print the raw
    result. Detection is duck-typed on the envelope shape.
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("isError") is not True:
        return None
    content = payload.get("content")
    if isinstance(content, list) and content and isinstance(content[0], dict):
        text = content[0].get("text")
        if isinstance(text, str):
            return text
    return "mcporter tool call failed (no error text)"


class McporterClient:
    """Stateless subprocess client for mcporter MCP calls.

    Parameters
    ----------
    server:
        mcporter server name (e.g. ``"mcphub"``). Overridable so the
        plugin works against alternative aggregator configs.
    tool_prefix:
        Prefix applied to short tool names — ``call("status")`` becomes
        ``mcphub.mempalace-mempalace_status``. Set ``""`` to pass
        fully-qualified names directly.
    timeout_seconds:
        Per-call timeout. Background prefetches can afford the full ceiling;
        synchronous code paths should pass something smaller.
    command:
        Override the resolved ``mcporter`` command. Tests inject a fake
        binary here; production callers leave this ``None`` so the client
        picks ``mcporter`` then ``npx mcporter``.
    """

    def __init__(
        self,
        server: str = DEFAULT_SERVER,
        tool_prefix: str = TOOL_PREFIX,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        command: Optional[List[str]] = None,
    ) -> None:
        self.server = server
        self.tool_prefix = tool_prefix
        self.timeout_seconds = timeout_seconds
        self._command = command or _resolve_command()

    def call(
        self,
        tool: str,
        args: Optional[Dict[str, Any]] = None,
        *,
        timeout_seconds: Optional[int] = None,
    ) -> Any:
        """Invoke ``tool`` on the configured server.

        Returns the decoded result on success (typically a dict). Raises
        :class:`McporterError` on subprocess failure, timeout, or
        tool-level ``isError: true``.
        """
        full_tool = f"{self.tool_prefix}{tool}" if self.tool_prefix else tool
        selector = f"{self.server}.{full_tool}"
        cmd = [*self._command, "call", selector, "--output", "json"]
        if args:
            cmd += ["--args", json.dumps(args)]

        timeout = timeout_seconds if timeout_seconds is not None else self.timeout_seconds

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ},
            )
        except subprocess.TimeoutExpired as exc:
            raise McporterError(f"mcporter call {selector} timed out after {timeout}s") from exc
        except FileNotFoundError as exc:
            raise McporterError(
                f"mcporter binary not found on PATH (tried {self._command[0]}); "
                "install via `npm install -g mcporter` or ensure npx is available"
            ) from exc

        if result.returncode != 0:
            raise McporterError(
                f"mcporter call {selector} exit={result.returncode}: "
                f"{(result.stderr or result.stdout or '').strip()[:500]}"
            )

        stdout = (result.stdout or "").strip()
        if not stdout:
            return None

        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            # Non-JSON output (some tools just print plain text). Hand it
            # back as a string so callers can decide what to do.
            return stdout

        # Tool-level error escapes the success envelope and uses the MCP
        # error shape. Exit code stays 0 in that case, so we must inspect.
        err = _detect_error(payload)
        if err:
            raise McporterError(f"{selector}: {err}")

        return payload

    def daemon_status(self) -> bool:
        """Return True if the mcporter daemon is currently running.

        mcphub is HTTP-only, so the daemon isn't strictly required (no
        stdio process to keep warm), but the method exists for setups
        that register additional stdio servers alongside mcphub.
        """
        try:
            result = subprocess.run(
                [*self._command, "daemon", "status"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
        # The daemon prints "Daemon is running" or "Daemon is not running"
        # depending on state. Anchor on the negative phrase to avoid
        # false positives.
        return "Daemon is not running" not in (result.stdout or "")
