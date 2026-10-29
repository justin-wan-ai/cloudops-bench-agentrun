"""escalate -> judge agent (the 5% path).

Only reached when the fixed flow cannot pin a cause: no candidate, the jev
verification disabled the hypothesis, or the family is inherently open
(code defects).  The judge is a budgeted, read-only ReAct loop over the same
snapshot tools; every tool call it makes is written into the same trace so the
official process scorer still sees it.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

from openai import OpenAI

from .env import ModelConfig
from .rules import VALID_ROOT_CAUSES
from .state import CaseState

SYSTEM = """You are the escalation judge in an SRE root-cause-analysis pipeline.

A deterministic workflow already collected evidence but could not pin the root cause.
You may call read-only diagnostic tools on the incident snapshot, then submit.

Reply with exactly one JSON object per turn, nothing else:
  {"thought": "...", "action": "<ToolName>", "arguments": {...}}
or, when you are confident:
  {"thought": "...", "final": {"fault_object": "app|node|namespace/name", "root_cause": "snake_case_cause", "key_evidence_summary": "one sentence"}}

Rules:
- fault_object must be `app/<business service>` (or `node/<node>` / `namespace/<ns>` when the cause is node/namespace scoped).
- root_cause must be one of the snake_case causes implied by the tool descriptions; do not invent names.
- Prefer 1-3 targeted tool calls over broad exploration. Evidence beats speculation.
"""


def _digest(state: CaseState, limit_per_probe: int = 1200) -> str:
    lines = [
        f"incident: system={state.system} namespace={state.anchors.namespace} query={state.anchors.query!r}",
        f"family shortlist: {state.family}",
        "collected evidence:",
    ]
    seen = set()
    for key, text in state.obs.items():
        if key.startswith("sig:") or key in seen or not text:
            continue
        seen.add(key)
        lines.append(f"--- [{key}] ({len(text)} chars)\n{text[:limit_per_probe]}")
    return "\n".join(lines)[:14000]


def _parse_json(text: str) -> Optional[Dict[str, Any]]:
    body = str(text or "").strip()
    body = re.sub(r"^```(?:json)?|```$", "", body, flags=re.MULTILINE).strip()
    try:
        return json.loads(body)
    except Exception:
        match = re.search(r"\{.*\}", body, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                return None
    return None


def escalate(state: CaseState, model_cfg: ModelConfig, budget: int = 8) -> Optional[Dict[str, Any]]:
    client = OpenAI(api_key=model_cfg.api_key, base_url=model_cfg.api_base, timeout=model_cfg.timeout)
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": _digest(state) + "\n\nAvailable tools:\n" + state.tools.describe()},
    ]
    for step in range(max(1, budget)):
        try:
            response = client.chat.completions.create(
                model=model_cfg.model, messages=messages, temperature=0.0, max_tokens=900
            )
        except Exception as exc:  # network/API problem: report, never fake an answer
            state.notes.append(f"escalate llm error: {type(exc).__name__}: {exc}")
            return None
        state.model_calls += 1
        usage = getattr(response, "usage", None)
        if usage is not None:
            state.tokens["in"] += int(getattr(usage, "prompt_tokens", 0) or 0)
            state.tokens["out"] += int(getattr(usage, "completion_tokens", 0) or 0)
        content = (response.choices[0].message.content or "").strip()
        payload = _parse_json(content)
        if payload is None:
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": "That was not valid JSON. Reply with exactly one JSON object."})
            continue

        if isinstance(payload.get("final"), dict):
            final = payload["final"]
            root_cause = str(final.get("root_cause") or "").strip()
            fault_object = str(final.get("fault_object") or "").strip()
            if root_cause in VALID_ROOT_CAUSES and "/" in fault_object:
                state.escalate_steps = step + 1
                return {
                    "fault_object": fault_object,
                    "root_cause": root_cause,
                    "key_evidence_summary": str(final.get("key_evidence_summary") or "")[:400],
                    "source": "escalate",
                }
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content":
                             f"Rejected: root_cause {root_cause!r} is not in the taxonomy or fault_object {fault_object!r} "
                             "is not of the form kind/name. Try again with a legal value."})
            continue

        action = str(payload.get("action") or "").strip()
        arguments = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
        if not action or action not in state.tools.tool_names():
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content":
                             f"Unknown action {action!r}. Choose one of: {', '.join(state.tools.tool_names())}."})
            continue

        text = state.observe(f"judge:{step}", action, arguments, purpose="escalate judge probe")
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": f"observation:\n{text[:4000] or '(empty)'}"})
        state.escalate_steps = step + 1
    state.notes.append("escalate exhausted budget")
    return None
