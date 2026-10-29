"""Bridge to the Cloud-OpsBench diagnostic backend (no re-implementation).

The benchmark ships the simulated-kubectl tools in
``repo/agents/cloudops_agent/{tools,runtime}``.  We import *those* modules so the
AgentRun runner and the stock ReAct baseline observe byte-identical tool output;
anything else would make the before/after comparison meaningless.
"""
from __future__ import annotations

import sys
from typing import Any, Dict

from .env import BENCH_ROOT

_AGENT_DIR = BENCH_ROOT / "agents" / "cloudops_agent"
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from tools.cloudops import build_tool_registry, create_k8s_tools, render_tools_description  # noqa: E402
from runtime.core import ToolExecutor  # noqa: E402

TOOL_SYSTEM = {"boutique": "boutique", "trainticket": "train-ticket"}


class SnapshotTools:
    """One case snapshot, exposed through the official ToolExecutor."""

    def __init__(self, case_path, system: str, category: str):
        self.case_path = str(case_path)
        self.system = system
        self.category = category
        registry = build_tool_registry(
            create_k8s_tools(self.case_path, system=TOOL_SYSTEM[system], fault_category=category)
        )
        self.registry = registry
        self.executor = ToolExecutor(registry)
        self.calls: list[Dict[str, Any]] = []

    def describe(self) -> str:
        return render_tools_description(self.registry)

    def tool_names(self) -> list[str]:
        return sorted(self.registry)

    def call(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        result = self.executor.execute(name, dict(arguments or {}))
        self.calls.append(
            {
                "tool_name": name,
                "arguments": dict(arguments or {}),
                "observation": result.get("observation") or "",
                "error": result.get("error"),
                "latency_ms": round(float(result.get("latency") or 0.0) * 1000, 1),
            }
        )
        return result

    def observe(self, name: str, arguments: Dict[str, Any]) -> str:
        """Call a tool and return its text observation ("" when the tool errored)."""
        result = self.call(name, arguments)
        if result.get("success"):
            return str(result.get("observation") or "")
        return ""
