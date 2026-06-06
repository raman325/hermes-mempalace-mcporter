"""MemPalace MCP-bridge memory provider for Hermes.

Routes Hermes' ``MemoryProvider`` ABC through the mempalace MCP server via
``mcporter`` + ``mcphub`` instead of ``import mempalace``. Targeted at setups
where mempalace lives on a separate host (e.g. docker-server) and Hermes
reaches it through the user's existing MCP aggregator.

Phase 2 of the MemPalace + Hermes integration. Phase 1 is the in-tree Python
plugin at ``mempalace/integrations/hermes/`` in MemPalace/mempalace#1684 —
same ABC, different backend.

Locked architectural decisions (see ``docs/decisions.md`` if it exists, or
the project README):

* **Default wing for Hermes-originated writes:** ``hermes``. Override per
  user via ``MEMPALACE_WING`` env var or ``default_wing`` in
  ``$HERMES_HOME/mempalace-mcporter.json``.
* **Stable room name:** ``conversations`` (same as Phase 1; session ids
  live in metadata, not as the room).
* **Diary identity:** single ``agent_name = "hermes"``. All diary entries
  land in ``wing_hermes`` (mempalace's default for per-agent diaries).
* **Wake-up:** composed client-side from three MCP calls cached at init —
  ``mempalace_status`` (palace overview + ``PALACE_PROTOCOL`` + ``AAAK_SPEC``),
  ``mempalace_list_drawers(wing=identity, limit=5)`` (identity layer; wing
  is configurable via ``MEMPALACE_IDENTITY_WING``),
  ``mempalace_diary_read(agent_name=hermes, last_n=3)`` (recent agent context).
* **Prefetch:** asynchronous via ``queue_prefetch``. The ~1s/call MCP
  latency makes synchronous prefetch unacceptable on the hot path; the
  background worker fills a cache that ``prefetch`` drains.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .client import McporterClient, McporterError

# Plug into Hermes' MemoryProvider ABC at runtime; fall back to a stub when
# this module is imported outside Hermes (tests, IDE inspection).
try:
    from agent.memory_provider import MemoryProvider  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover

    class MemoryProvider:  # type: ignore[no-redef]
        """Stub used when ``agent.memory_provider`` cannot be imported."""


logger = logging.getLogger("mempalace_mcporter")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_TRIVIAL_PROMPT_RE = re.compile(
    r"^(yes|no|ok|okay|sure|thanks|thank you|y|n|yep|nope|yeah|nah|"
    r"continue|go ahead|do it|proceed|got it|cool|nice|great|done|next|lgtm|k)$",
    re.IGNORECASE,
)


def _is_trivial_prompt(text: str) -> bool:
    """Return True for prompts not worth firing a 1s MCP call against.

    Slash commands, empty strings, and single-word acknowledgements skip
    the background prefetch — the cache from the previous turn is more
    relevant than a fresh search on ``"ok"``.
    """
    if not text:
        return True
    stripped = text.strip()
    if not stripped or stripped.startswith("/"):
        return True
    return bool(_TRIVIAL_PROMPT_RE.match(stripped))


def _normalize_content(content: Any) -> str:
    """Flatten Anthropic/OpenAI list-shaped ``content`` to plain text.

    Mirrors the helper in the Phase 1 plugin so behaviour stays identical
    across the two backends. Without it, ``f"User: {content}"`` persists
    the literal Python ``repr`` of the list and corrupts semantic search.
    """
    if not content:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            btype = block.get("type", "")
            if btype == "text":
                text = block.get("text", "")
                if text:
                    parts.append(text)
            elif btype == "tool_use":
                parts.append(f"[tool_use: {block.get('name', '?')}]")
            elif btype == "tool_result":
                parts.append(f"[tool_result] {_normalize_content(block.get('content'))}")
            else:
                text = block.get("text", "")
                if text:
                    parts.append(text)
        return "\n".join(parts)
    return str(content)


# ---------------------------------------------------------------------------
# Tool schemas (same shape as Phase 1 so a session can swap providers cleanly)
# ---------------------------------------------------------------------------


TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "mempalace_search",
        "description": (
            "Semantic search across the palace. Returns verbatim drawers ranked by relevance."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language search query."},
                "wing": {"type": "string", "description": "Limit results to one wing (optional)."},
                "room": {"type": "string", "description": "Limit results to one room (optional)."},
                "limit": {
                    "type": "integer",
                    "description": "Number of results (default 5).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "mempalace_status",
        "description": (
            "Palace overview: total drawers, per-wing counts, protocol, and AAAK dialect spec."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "mempalace_list_wings",
        "description": "List all wings with their drawer counts.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "mempalace_list_rooms",
        "description": "List rooms (and counts) within a wing.",
        "parameters": {
            "type": "object",
            "properties": {
                "wing": {
                    "type": "string",
                    "description": "Wing name (optional — all wings if omitted).",
                },
            },
        },
    },
    {
        "name": "mempalace_kg_query",
        "description": (
            "Query the knowledge graph for relationships involving an entity, "
            "with optional time filtering."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "Entity name."},
                "as_of": {
                    "type": "string",
                    "description": "ISO date for historical view (optional).",
                },
            },
            "required": ["entity"],
        },
    },
    {
        "name": "mempalace_kg_add",
        "description": "Add a (subject, predicate, object) fact to the knowledge graph.",
        "parameters": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "predicate": {"type": "string"},
                "object": {"type": "string"},
                "valid_from": {"type": "string", "description": "ISO date (optional)."},
            },
            "required": ["subject", "predicate", "object"],
        },
    },
    {
        "name": "mempalace_diary_write",
        "description": (
            "Append an entry to this Hermes session's diary "
            "(agent_name is auto-injected — leave it out)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "entry": {"type": "string", "description": "Diary entry text."},
                "topic": {"type": "string", "description": "Optional topic tag."},
            },
            "required": ["entry"],
        },
    },
    {
        "name": "mempalace_diary_read",
        "description": "Read recent diary entries for this Hermes session.",
        "parameters": {
            "type": "object",
            "properties": {
                "last_n": {
                    "type": "integer",
                    "description": "Number of entries to return (default 10).",
                },
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class MempalaceMcporterProvider(MemoryProvider):  # type: ignore[misc]
    """MemPalace memory provider routed through mcporter + mcphub."""

    # Defaults — generic so the plugin works on any mempalace install.
    # Override per-user via ``$HERMES_HOME/mempalace-mcporter.json`` or the
    # ``MEMPALACE_WING`` / ``MEMPALACE_IDENTITY_WING`` env vars.
    DEFAULT_WING = "hermes"
    DEFAULT_ROOM = "conversations"
    DIARY_AGENT_NAME = "hermes"
    IDENTITY_WING = "identity"
    IDENTITY_DRAWERS_LIMIT = 5
    DIARY_READ_LAST_N = 3

    # Threading discipline -----------------------------------------------
    WORKER_QUEUE_MAX = 500
    PREFETCH_TIMEOUT_SECONDS = 3  # first-turn sync fallback ceiling

    def __init__(self) -> None:
        # Lifecycle
        self._config: Dict[str, Any] = {}
        self._initialized = False
        self._cron_skipped = False
        self._session_id: str = ""
        self._hermes_home: str = ""
        self._turn_count = 0

        # MCP client (built in initialize once config is loaded)
        self._client: Optional[McporterClient] = None

        # Wake-up composition (3 cached layers, refreshed on background threads)
        self._wakeup_protocol: str = ""
        self._wakeup_identity: str = ""
        self._wakeup_recent: str = ""
        self._wakeup_lock = threading.Lock()

        # Background worker for writes
        self._worker_queue: queue.Queue = queue.Queue(maxsize=self.WORKER_QUEUE_MAX)
        self._worker_thread: Optional[threading.Thread] = None
        self._worker_stop = threading.Event()

        # Background prefetch cache (filled by queue_prefetch, drained by prefetch)
        self._prefetch_result: str = ""
        self._prefetch_lock = threading.Lock()
        self._prefetch_thread: Optional[threading.Thread] = None

        # Serialise re-entrant initialize so we never spawn duplicate workers
        self._init_lock = threading.Lock()

    # ----- Required ABC --------------------------------------------------

    @property
    def name(self) -> str:
        return "mempalace-mcporter"

    def is_available(self) -> bool:
        # mcporter availability is checked at initialize time so the
        # provider can register cleanly even if the binary is missing.
        return True

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        agent_context = kwargs.get("agent_context", "")
        platform = kwargs.get("platform", "cli")
        if agent_context in {"cron", "flush"} or platform == "cron":
            with self._init_lock:
                self._cron_skipped = True
            return

        with self._init_lock:
            # Clear the cron flag — a previous cron init must not poison us.
            self._cron_skipped = False
            self._session_id = session_id or ""
            self._hermes_home = str(kwargs.get("hermes_home", "") or "")
            self._config = self._load_config()

            self._client = McporterClient(
                server=self._config.get("mcporter_server", "mempalace"),
                tool_prefix=self._config.get("tool_prefix", ""),
            )

            # Smoke-test the client with the canonical wake-up call. If it
            # fails the provider stays inactive — better a uniformly silent
            # provider than half-broken state advertising tools that error.
            backend_ready = False
            try:
                status = self._client.call("mempalace_status", timeout_seconds=10)
                if isinstance(status, dict):
                    backend_ready = True
                    self._wakeup_protocol = self._format_status(status)
            except McporterError as exc:
                logger.warning("MemPalace MCP unreachable on initialize: %s", exc)

            if backend_ready and (
                self._worker_thread is None or not self._worker_thread.is_alive()
            ):
                self._worker_stop.clear()
                self._worker_thread = threading.Thread(
                    target=self._background_worker,
                    daemon=True,
                    name="mempalace-mcporter-worker",
                )
                self._worker_thread.start()

            if backend_ready:
                # Fire the remaining two wake-up layers in parallel; both land
                # in the cache under the lock, ready for the first
                # ``system_prompt_block`` call.
                threading.Thread(
                    target=self._refresh_wakeup_identity,
                    daemon=True,
                    name="mempalace-mcporter-wakeup-id",
                ).start()
                threading.Thread(
                    target=self._refresh_wakeup_recent,
                    daemon=True,
                    name="mempalace-mcporter-wakeup-diary",
                ).start()

            self._initialized = backend_ready

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        # Schemas describe the *interface*, not runtime readiness. Hermes'
        # ``agent.memory_manager`` snapshots schemas at registration time
        # (BEFORE ``initialize()`` runs) to build its tool→provider routing
        # table; if we returned ``[]`` there, the dispatcher would never learn
        # our tool names and every later call would hit
        # ``"Unknown tool: <name>"`` without ever reaching ``handle_tool_call``.
        # Backend readiness is checked at call time in ``handle_tool_call``.
        if self._cron_skipped:
            return []
        return list(TOOL_SCHEMAS)

    # ----- Recall: prompt block + prefetch + queue_prefetch -------------

    def system_prompt_block(self) -> str:
        if self._cron_skipped or not self._initialized:
            return ""
        with self._wakeup_lock:
            parts = [
                self._wakeup_protocol,
                self._wakeup_identity,
                self._wakeup_recent,
            ]
        non_empty = [p for p in parts if p]
        if not non_empty:
            return ""
        return "# MemPalace context\n\n" + "\n\n".join(non_empty)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._cron_skipped or not self._initialized or not query:
            return ""

        # Drain whatever queue_prefetch produced on the prior turn.
        with self._prefetch_lock:
            cached = self._prefetch_result
            self._prefetch_result = ""
        if cached:
            return cached

        if _is_trivial_prompt(query):
            return ""

        # First-turn / cache-miss path: run synchronously with a bounded
        # ceiling. If we time out, leave the thread running so its result
        # lands in the cache for the next turn.
        result_box = {"value": ""}

        def _run() -> None:
            try:
                result_box["value"] = self._compute_prefetch(query)
            except Exception as exc:
                logger.debug("sync prefetch error: %s", exc)

        thread = threading.Thread(target=_run, daemon=True, name="mempalace-mcporter-prefetch-sync")
        thread.start()
        thread.join(timeout=self.PREFETCH_TIMEOUT_SECONDS)
        if thread.is_alive():
            logger.debug(
                "first-turn prefetch exceeded %ds — deferring to background",
                self.PREFETCH_TIMEOUT_SECONDS,
            )
            return ""
        return result_box["value"]

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if self._cron_skipped or not self._initialized or not query:
            return
        if _is_trivial_prompt(query):
            return
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            # Prior fetch still in flight — let it land rather than racing.
            return

        def _run() -> None:
            try:
                result = self._compute_prefetch(query)
                with self._prefetch_lock:
                    self._prefetch_result = result
            except Exception as exc:
                logger.debug("queue_prefetch error: %s", exc)

        self._prefetch_thread = threading.Thread(
            target=_run, daemon=True, name="mempalace-mcporter-prefetch-bg"
        )
        self._prefetch_thread.start()

    def _compute_prefetch(self, query: str) -> str:
        if self._client is None:
            return ""
        n = max(1, min(int(self._config.get("n_prefetch", 3)), 20))
        try:
            result = self._client.call("mempalace_search", {"query": query, "limit": n})
        except McporterError as exc:
            logger.debug("search failed: %s", exc)
            return ""
        results = result.get("results", []) if isinstance(result, dict) else []
        if not results:
            return ""
        lines = ["## MemPalace — relevant context"]
        for r in results:
            if not isinstance(r, dict):
                continue
            wing = r.get("wing", "")
            room = r.get("room", "")
            tag = f"[{wing}/{room}] " if wing else ""
            content = (r.get("content") or r.get("text") or "").strip()
            if content:
                lines.append(f"{tag}{content}")
        return "\n\n".join(lines)

    # ----- Writes (all routed through the background worker) -------------

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        if self._cron_skipped or not self._initialized:
            return
        user_text = _normalize_content(user_content)
        assistant_text = _normalize_content(assistant_content)
        if not user_text and not assistant_text:
            return
        try:
            self._worker_queue.put_nowait(
                (
                    "file_turn",
                    {
                        "user": user_text,
                        "assistant": assistant_text,
                        "session_id": session_id or self._session_id,
                    },
                )
            )
        except queue.Full:
            logger.warning(
                "MemPalace-MCP queue full (maxsize=%d) — turn dropped",
                self.WORKER_QUEUE_MAX,
            )

    def on_turn_start(self, turn_number: int, message: str, **kwargs: Any) -> None:
        self._turn_count = turn_number

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if self._cron_skipped or not self._initialized:
            return
        try:
            self._worker_queue.put_nowait(("session_end", {"messages": list(messages or [])}))
        except queue.Full:
            logger.warning("queue full at session_end — messages will not be filed")
        # Refresh the recent-diary wake-up cache so the next session sees
        # the entry that on_session_end is about to write.
        threading.Thread(
            target=self._refresh_wakeup_recent,
            daemon=True,
            name="mempalace-mcporter-wakeup-diary",
        ).start()

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs: Any,
    ) -> None:
        self._session_id = new_session_id or ""
        if reset:
            self._turn_count = 0
            # Stale-cache invalidation: the prior query was about a
            # different conversation.
            with self._prefetch_lock:
                self._prefetch_result = ""

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        if self._cron_skipped or not self._initialized:
            return ""
        try:
            self._worker_queue.put_nowait(("pre_compress", {"messages": list(messages or [])}))
        except queue.Full:
            logger.warning(
                "queue full at pre_compress — %d messages will not be filed",
                len(messages or []),
            )
            return ""
        return (
            "MemPalace has filed every message in this window verbatim. "
            "Compressed content remains searchable via the `mempalace_search` tool."
        )

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if (
            self._cron_skipped
            or not self._initialized
            or action != "add"
            or target != "user"
            or not content
        ):
            return
        try:
            self._worker_queue.put_nowait(("mem_write", {"content": content}))
        except queue.Full:
            logger.warning("queue full at memory_write — entry dropped")

    def on_delegation(
        self,
        task: str,
        result: str,
        *,
        child_session_id: str = "",
        **kwargs: Any,
    ) -> None:
        self.sync_turn(
            f"[delegated task]\n{task}",
            f"[subagent {child_session_id} returned]\n{result}",
            session_id=self._session_id,
        )

    # ----- Tool dispatch (thin pass-throughs; agent_name auto-injected) -

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        if self._cron_skipped:
            return json.dumps({"error": "MemPalace not active (cron context)."})
        if not self._initialized or self._client is None:
            return json.dumps({"error": "MemPalace MCP not initialized."})
        args = dict(args or {})
        try:
            if tool_name == "mempalace_search":
                return json.dumps(self._client.call("mempalace_search", args))
            if tool_name == "mempalace_status":
                return json.dumps(self._client.call("mempalace_status"))
            if tool_name == "mempalace_list_wings":
                return json.dumps(self._client.call("mempalace_list_wings"))
            if tool_name == "mempalace_list_rooms":
                return json.dumps(self._client.call("mempalace_list_rooms", args))
            if tool_name == "mempalace_kg_query":
                return json.dumps(self._client.call("mempalace_kg_query", args))
            if tool_name == "mempalace_kg_add":
                return json.dumps(self._client.call("mempalace_kg_add", args))
            if tool_name == "mempalace_diary_write":
                args["agent_name"] = self.DIARY_AGENT_NAME
                return json.dumps(self._client.call("mempalace_diary_write", args))
            if tool_name == "mempalace_diary_read":
                args["agent_name"] = self.DIARY_AGENT_NAME
                return json.dumps(self._client.call("mempalace_diary_read", args))
            return json.dumps({"error": f"unknown tool: {tool_name}"})
        except McporterError as exc:
            return json.dumps({"error": str(exc)})

    # ----- Setup-wizard plumbing ----------------------------------------

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "default_wing",
                "env_var": "MEMPALACE_WING",
                "description": "Default wing for Hermes-originated drawer writes.",
                "default": self.DEFAULT_WING,
            },
            {
                "key": "identity_wing",
                "env_var": "MEMPALACE_IDENTITY_WING",
                "description": (
                    "Wing whose drawers compose the identity layer of "
                    "system_prompt_block."
                ),
                "default": self.IDENTITY_WING,
            },
            {
                "key": "mcporter_server",
                "env_var": "MEMPALACE_MCPORTER_SERVER",
                "description": (
                    "mcporter server name in ~/.mcporter/mcporter.json. "
                    "Default 'mempalace' assumes mcporter talks to mempalace "
                    "directly; set to 'mcphub' (or your aggregator's name) "
                    "when going through an aggregator."
                ),
                "default": "mempalace",
            },
            {
                "key": "tool_prefix",
                "env_var": "MEMPALACE_TOOL_PREFIX",
                "description": (
                    "Tool name prefix. Empty default matches direct mempalace "
                    "registration. Aggregators that namespace tools by source "
                    "server (mcphub uses 'mempalace-') need this set to match."
                ),
                "default": "",
            },
            {
                "key": "n_prefetch",
                "description": "Number of search results injected per turn.",
                "default": 3,
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        config_path = Path(hermes_home) / "mempalace-mcporter.json"
        existing: Dict[str, Any] = {}
        if config_path.exists():
            try:
                existing = json.loads(config_path.read_text())
            except Exception as exc:
                logger.debug("config read failed: %s", exc)
        existing.update(values)
        config_path.write_text(json.dumps(existing, indent=2) + "\n")

    # ----- Shutdown ------------------------------------------------------

    def shutdown(self) -> None:
        self._worker_stop.set()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=10.0)
            if self._worker_thread.is_alive():
                logger.warning("MemPalace-MCP worker did not drain within shutdown timeout")
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=5.0)

    # ----- Internals -----------------------------------------------------

    def _load_config(self) -> Dict[str, Any]:
        config: Dict[str, Any] = {}
        if self._hermes_home:
            config_path = Path(self._hermes_home) / "mempalace-mcporter.json"
            if config_path.exists():
                try:
                    config.update(json.loads(config_path.read_text()))
                except Exception as exc:
                    logger.debug("config load failed: %s", exc)
        for env_key, conf_key in (
            ("MEMPALACE_WING", "default_wing"),
            ("MEMPALACE_IDENTITY_WING", "identity_wing"),
            ("MEMPALACE_MCPORTER_SERVER", "mcporter_server"),
            ("MEMPALACE_TOOL_PREFIX", "tool_prefix"),
        ):
            value = os.environ.get(env_key)
            if value:
                config[conf_key] = value
        return config

    def _format_status(self, status: Dict[str, Any]) -> str:
        """Render mempalace_status into the wake-up protocol layer."""
        lines = ["## Palace overview"]
        if "total_drawers" in status:
            lines.append(f"- Total drawers: {status['total_drawers']}")
        wings = status.get("wings") or {}
        if isinstance(wings, dict) and wings:
            wing_summary = ", ".join(f"{w} ({n})" for w, n in wings.items())
            lines.append(f"- Wings: {wing_summary}")
        protocol = status.get("protocol", "")
        if protocol:
            lines.append("")
            lines.append("## Memory protocol")
            lines.append(protocol)
        return "\n".join(lines)

    def _refresh_wakeup_identity(self) -> None:
        if self._client is None:
            return
        identity_wing = self._config.get("identity_wing") or self.IDENTITY_WING
        try:
            result = self._client.call(
                "mempalace_list_drawers",
                {"wing": identity_wing, "limit": self.IDENTITY_DRAWERS_LIMIT},
            )
        except McporterError as exc:
            logger.debug("identity wake-up fetch failed: %s", exc)
            return
        drawers = result.get("drawers", []) if isinstance(result, dict) else []
        if not drawers:
            return
        lines = ["## Identity"]
        for d in drawers:
            if not isinstance(d, dict):
                continue
            # ``list_drawers`` returns ``content_preview`` (truncated). Older
            # mempalace versions may also include ``content``; check both.
            text = (d.get("content_preview") or d.get("content") or "").strip()
            if text:
                room = d.get("room", "")
                tag = f"[{room}] " if room else ""
                lines.append(f"{tag}{text}")
        with self._wakeup_lock:
            self._wakeup_identity = "\n\n".join(lines) if len(lines) > 1 else ""

    def _refresh_wakeup_recent(self) -> None:
        if self._client is None:
            return
        try:
            result = self._client.call(
                "mempalace_diary_read",
                {
                    "agent_name": self.DIARY_AGENT_NAME,
                    "last_n": self.DIARY_READ_LAST_N,
                },
            )
        except McporterError as exc:
            logger.debug("diary wake-up fetch failed: %s", exc)
            return
        entries = result.get("entries", []) if isinstance(result, dict) else []
        if not entries:
            return
        lines = ["## Recent agent context"]
        for e in entries:
            if not isinstance(e, dict):
                continue
            # Diary entries carry their text under ``content`` (not ``entry``,
            # which is the *input* arg name to ``mempalace_diary_write``).
            # Tolerate ``entry`` too so older shapes still render.
            text = (e.get("content") or e.get("entry") or "").strip()
            if text:
                topic = e.get("topic", "")
                tag = f"[{topic}] " if topic and topic != "general" else ""
                lines.append(f"{tag}{text}")
        with self._wakeup_lock:
            self._wakeup_recent = "\n\n".join(lines) if len(lines) > 1 else ""

    def _background_worker(self) -> None:
        # Same drain-on-stop pattern as Phase 1: don't exit while items remain.
        while not self._worker_stop.is_set() or not self._worker_queue.empty():
            try:
                task, payload = self._worker_queue.get(timeout=1.0)
            except queue.Empty:
                if self._worker_stop.is_set():
                    break
                continue
            try:
                if task == "file_turn":
                    self._file_turn(payload)
                elif task == "session_end":
                    self._mine_session(payload)
                elif task == "pre_compress":
                    self._file_pre_compress(payload)
                elif task == "mem_write":
                    self._mirror_memory_write(payload)
            except Exception as exc:
                logger.debug("worker task %s error: %s", task, exc)
            finally:
                try:
                    self._worker_queue.task_done()
                except ValueError:
                    pass

    def _file_turn(self, payload: Dict[str, Any]) -> None:
        if self._client is None:
            return
        user_msg = payload.get("user", "") or ""
        assistant_msg = payload.get("assistant", "") or ""
        if not user_msg and not assistant_msg:
            return
        text = f"User: {user_msg}\n\nAssistant: {assistant_msg}".strip()
        wing = self._config.get("default_wing") or self.DEFAULT_WING
        try:
            self._client.call(
                "mempalace_add_drawer",
                {"wing": wing, "room": self.DEFAULT_ROOM, "content": text},
            )
        except McporterError as exc:
            logger.debug("add_drawer failed: %s", exc)

    def _mine_session(self, payload: Dict[str, Any]) -> None:
        messages = payload.get("messages", []) or []
        for idx, msg in enumerate(messages):
            if msg.get("role") != "user":
                continue
            content = _normalize_content(msg.get("content"))
            if not content:
                continue
            assistant_content = ""
            if idx + 1 < len(messages) and messages[idx + 1].get("role") == "assistant":
                assistant_content = _normalize_content(messages[idx + 1].get("content"))
            self._file_turn({"user": content, "assistant": assistant_content})

    def _file_pre_compress(self, payload: Dict[str, Any]) -> None:
        msgs = payload.get("messages", []) or []
        i = 0
        while i < len(msgs):
            msg = msgs[i]
            if msg.get("role") != "user":
                # Lone non-user message — file under empty user so it isn't dropped.
                self._file_turn(
                    {
                        "user": "",
                        "assistant": _normalize_content(msg.get("content")),
                    }
                )
                i += 1
                continue
            user_content = _normalize_content(msg.get("content"))
            assistant_content = ""
            if i + 1 < len(msgs) and msgs[i + 1].get("role") == "assistant":
                assistant_content = _normalize_content(msgs[i + 1].get("content"))
                i += 2
            else:
                i += 1
            self._file_turn({"user": user_content, "assistant": assistant_content})

    def _mirror_memory_write(self, payload: Dict[str, Any]) -> None:
        if self._client is None:
            return
        content = payload.get("content", "") or ""
        if not content:
            return
        try:
            self._client.call(
                "mempalace_kg_add",
                {"subject": "user", "predicate": "asserted", "object": content},
            )
        except McporterError as exc:
            logger.debug("kg_add failed: %s", exc)


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(ctx: Any) -> None:
    """Register the MempalaceMcp provider with Hermes."""
    ctx.register_memory_provider(MempalaceMcporterProvider())
