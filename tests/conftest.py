"""Shared fixtures for the Phase 2 plugin test suite.

The plugin imports ``from agent.memory_provider import MemoryProvider`` and
falls back to a stub when Hermes isn't on the path. For tests we install a
stub ourselves before the plugin module is first imported so the ABC binds
cleanly in either order.
"""

from __future__ import annotations

import sys
import types


def _install_stub_memory_provider() -> None:
    if "agent.memory_provider" in sys.modules:
        return

    agent_mod = types.ModuleType("agent")
    mp_mod = types.ModuleType("agent.memory_provider")

    class MemoryProvider:  # mirrors what the plugin's class inherits
        pass

    mp_mod.MemoryProvider = MemoryProvider  # type: ignore[attr-defined]
    agent_mod.memory_provider = mp_mod  # type: ignore[attr-defined]
    sys.modules["agent"] = agent_mod
    sys.modules["agent.memory_provider"] = mp_mod


_install_stub_memory_provider()
