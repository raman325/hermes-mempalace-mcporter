# hermes-mempalace-mcporter

Hermes [`MemoryProvider`](https://github.com/NousResearch/hermes-agent) plugin
that routes through [mempalace](https://github.com/MemPalace/mempalace)'s MCP
server via [`mcporter`](https://www.npmjs.com/package/mcporter) + your existing
MCP aggregator (`mcphub`).

This is **Phase 2** of the MemPalace + Hermes integration. Phase 1 is the
in-tree Python plugin at `mempalace/integrations/hermes/` in
[MemPalace/mempalace#1684](https://github.com/MemPalace/mempalace/pull/1684):
same `MemoryProvider` ABC, but Phase 1 imports mempalace as a Python module
while Phase 2 reaches mempalace over MCP. Use Phase 2 when mempalace runs on
a different host than Hermes and you already have an MCP aggregator wired up.

## When to use this

- **Phase 1** — you `pip install mempalace` on the same machine as Hermes;
  the plugin imports mempalace directly. Simple, local-only.
- **Phase 2 (this)** — mempalace runs on a remote host (e.g. `docker-server`);
  Hermes reaches it through mcporter + mcphub. Network-attached palace.

## Architecture

```
Hermes (Python)
    │  imports & calls
    ▼
MempalaceMcporterProvider  (this plugin)
    │  subprocess
    ▼
mcporter call mcphub.mempalace-<tool>  (Node.js CLI)
    │  HTTPS
    ▼
mcphub aggregator
    │  MCP
    ▼
mempalace MCP server (on docker-server)
```

Latency per call: ~1s warm (global `mcporter` install) or ~2s cold (via
`npx`). The plugin runs all writes through a background queue and prefetches
via `queue_prefetch`, so the agent loop never blocks on this round-trip
except for the first turn's synchronous prefetch (bounded at 3s).

## Design

| What | Default | Notes |
|---|---|---|
| Default wing for Hermes writes | `hermes` | Override via `MEMPALACE_WING` env var or `default_wing` in `$HERMES_HOME/mempalace-mcporter.json` |
| Identity wing (wake-up source) | `identity` | Override via `MEMPALACE_IDENTITY_WING` or `identity_wing` config key |
| Stable room name | `conversations` | Matches Phase 1's backfill behavior; `session_id` lives in metadata |
| Diary identity | single `agent_name = "hermes"` | All Hermes diary in one diary regardless of profile |
| Wake-up | composed from 3 MCP calls cached at init | No dedicated `mempalace_wake_up` tool exists |
| Prefetch | `queue_prefetch` background thread | ~1s/call MCP latency makes synchronous prefetch unacceptable |

The wake-up is the most interesting piece — see
[`plugin/__init__.py`](plugin/__init__.py) docstring for the three cached
layers (`mempalace_status` for protocol + AAAK + structure;
`mempalace_list_drawers(wing=<identity_wing>)` for identity;
`mempalace_diary_read(agent_name=hermes)` for recent agent context).

## Install

mcporter must be on the host's PATH. Global install avoids the ~2s `npx`
cold start per call:

```bash
ssh hermes 'sudo npm install -g mcporter'
```

Then deploy the plugin:

```bash
git clone https://github.com/raman325/hermes-mempalace-mcporter.git   # or wherever
cd hermes-mempalace-mcporter
./deploy.sh   # rsyncs plugin/ to hermes:~/.hermes/plugins/mempalace-mcporter/
```

In `~/.hermes/config.yaml`:

```yaml
memory:
  provider: mempalace-mcporter
```

Restart Hermes; the plugin self-tests against mempalace at `initialize`
time via a `mempalace_status` call. If it can't reach the backend the
provider stays inactive (tools hidden, hooks no-op) rather than advertising
behavior it can't deliver.

## Configuration

Read from `$HERMES_HOME/mempalace-mcporter.json` first, then env vars override:

| Key | Env var | Default |
|---|---|---|
| `default_wing` | `MEMPALACE_WING` | `hermes` |
| `identity_wing` | `MEMPALACE_IDENTITY_WING` | `identity` |
| `mcporter_server` | `MEMPALACE_MCPORTER_SERVER` | `mcphub` |
| `tool_prefix` | `MEMPALACE_TOOL_PREFIX` | `mempalace-` |
| `n_prefetch` | — | `3` (clamped 1–20) |

`tool_prefix` is what mcphub prepends to tools from each upstream server. If
your aggregator uses a different prefix (or none), set it here. Empty string
is valid — set `{"tool_prefix": ""}` in the JSON when mcporter is configured
to talk to mempalace directly (no aggregator).

Empty-string env var values are treated as **unset** (a deliberate guard
against accidental clobbering from deactivation scripts). To force a value
to empty, use the JSON config file.

Example `~/.hermes/mempalace-mcporter.json` for a palace whose wings use a
custom prefix like `myorg_`:

```json
{
  "default_wing": "myorg_hermes",
  "identity_wing": "myorg_identity"
}
```

## Development

```bash
uv venv --python 3.13
uv pip install -e ".[dev]"
uv run pytest                # unit tests, no SSH/network
uv run ruff check plugin/
./deploy.sh                  # deploy to hermes for smoke testing
```

Tests mock the `McporterClient` so they don't need a live mcphub.

## Relationship to Phase 1

This plugin and the Phase 1 plugin can both be installed in
`~/.hermes/plugins/` on different hosts (or even the same host with
different `memory.provider` settings). They expose the same 8 tool
schemas to the model, so a Hermes session can switch backends without
the model noticing — the tools just route to different places.

Both backends ultimately write to the same wing/room/drawer/KG primitives;
mempalace's data model is the single source of truth.
