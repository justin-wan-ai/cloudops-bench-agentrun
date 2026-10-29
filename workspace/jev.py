"""Jev decision node for DSL v0.2, backed by Aliyun Model Studio "decision model".

The decision model is a *non-generative* forward pass: it returns
``choice`` / ``noul`` / ``score`` answers with probability distributions, which is
exactly the AgentRun jev contract (route / pick / verify, mechanical output,
explicit abstain, auditable probabilities).

Endpoint (see https://help.aliyun.com/zh/model-studio/decision-model-api):
    POST {DASHSCOPE_OPENAI_BASE_URL}/systemone
    model = "decision-model-preview"
    body  = {"model": ..., "state": ..., "questions": {...}}

Mapping used here:
    jev(route)  -> choice  : which plan computes this question ("other" = abstain)
    jev(verify) -> noul    : does the chosen plan's output contract match the question?
    jev(score)  -> score   : (optional) confidence-ish complexity nudge, recorded only

No gold, no result tables and no answers ever enter ``state``.

Hard rule (project standing rule, see README §10): jev **always** calls the real
Aliyun decision model.  There is no mock/stub/local-surrogate path; a transport or
auth failure raises :class:`JevError` and is reported as ``status="error"``, never
silently downgraded to a deterministic answer.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from . import config as _config  # noqa: F401  (import side effect: loads .env)

MODEL = "decision-model-preview"
ABSTAIN = "other"


class JevError(RuntimeError):
    pass


@dataclass
class JevConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = MODEL
    timeout: int = 60
    max_retries: int = 2

    def __post_init__(self) -> None:
        self.base_url = (self.base_url or os.environ.get("DASHSCOPE_OPENAI_BASE_URL", "")).rstrip("/")
        self.api_key = self.api_key or os.environ.get("DASHSCOPE_API_KEY", "")
        if not self.base_url:
            raise JevError("DASHSCOPE_OPENAI_BASE_URL is not set")
        if not self.api_key:
            raise JevError("DASHSCOPE_API_KEY is not set")

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/systemone"


@dataclass
class JevClient:
    config: JevConfig = field(default_factory=JevConfig)
    calls: int = 0
    input_tokens: int = 0
    latency_ms: float = 0.0
    trace: list[dict] = field(default_factory=list)

    def system_one(self, state: Any, questions: dict[str, dict]) -> dict:
        if len(questions) > 16:
            raise JevError(f"decision model accepts at most 16 questions, got {len(questions)}")
        payload = {"model": self.config.model, "state": state, "questions": questions}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_error = ""
        for _ in range(self.config.max_retries + 1):
            request = urllib.request.Request(
                self.config.endpoint,
                data=data,
                method="POST",
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
                self.calls += 1
                self.input_tokens += int((body.get("usage") or {}).get("input_tokens") or 0)
                self.latency_ms += float(body.get("latency_ms") or 0.0)
                digest = hashlib.sha256(
                    json.dumps(state, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest()[:12]
                self.trace.append({
                    "request_id": body.get("request_id"),
                    "endpoint": self.config.endpoint,
                    "model": self.config.model,
                    "state_sha256_12": digest,
                    "state_keys": sorted(state) if isinstance(state, dict) else "raw",
                    "questions": sorted(questions),
                    "answers": body.get("answers"),
                    "usage": body.get("usage"),
                    "latency_ms": body.get("latency_ms"),
                })
                return body
            except urllib.error.HTTPError as exc:  # 4xx = do not retry blindly
                detail = exc.read().decode("utf-8", "replace")[:400]
                last_error = f"HTTP {exc.code}: {detail}"
                if 400 <= exc.code < 500 and exc.code != 429:
                    break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
        raise JevError(last_error or "decision model call failed")

    def usage(self) -> dict:
        return {"calls": self.calls, "input_tokens": self.input_tokens,
                "model_latency_ms": round(self.latency_ms, 1)}


def _cond_label(cond) -> str:
    if not cond:
        return "default"
    if isinstance(cond, list):
        return " and ".join(_cond_label(c) for c in cond)
    if "param" in cond:
        if "equals" in cond:
            return f"{cond['param']}={cond['equals']}"
        if "in" in cond:
            return f"{cond['param']} in {cond['in']}"
        if "not_equals" in cond:
            return f"{cond['param']}!={cond['not_equals']}"
    return "condition"


def _output_description(plan) -> str:
    """Render the output contract, including per-shape variants of a unified spec."""
    project = plan.project or {}
    def render(columns: list) -> str:
        names = []
        for column in columns:
            if "as" in column:
                names.append(column["as"])
            elif isinstance(column.get("expand"), dict):
                names.append(f"<one column per value of {column['expand'].get('param')}>")
        return ", ".join(names)

    if project.get("variants"):
        parts = []
        for variant in project["variants"]:
            parts.append(f"{_cond_label(variant.get('when'))} -> [{render(variant.get('columns', []))}]")
        return "Output columns by shape: " + "; ".join(parts)
    return "Output columns: " + render(project.get("columns", []))


def _catalog_criteria(plans: list) -> dict[str, str]:
    """Describe each plan by what it computes *and* what it needs from the question.

    The required-parameter list is what lets the model separate plans that share an
    entity (e.g. the count vs rate retention plans) instead of guessing from prose.
    """
    criteria = {}
    for plan in plans:
        ops = " -> ".join(next(iter(op)) for op in plan.ops)
        required = sorted(name for name, spec in plan.params.items() if spec.get("required"))
        criteria[plan.id] = (
            f"{plan.description} Entity={plan.entity}. "
            f"Question-supplied parameters it cannot run without: {required or 'none'}. "
            f"Operator chain: {ops}. {_output_description(plan)}"
        )
    criteria[ABSTAIN] = (
        "none of these plans fits: the question needs a mechanism, entity or output shape that is "
        "not in this catalog, or its expected answer shape is ambiguous between several plans."
    )
    return criteria


def route_plan(dsl, task: dict, slots: dict, client: JevClient,
               restrict: list[str] | None = None) -> dict:
    """jev(route): choose one plan id (or abstain) for this question.

    ``restrict`` is the deterministic candidate set from the slot router (AgentRun
    pattern B: deterministic first, model second).  Passing ``None`` asks over the
    full catalog, which is the fallback when no plan passes the hard requirements.
    """
    plans = dsl.plans if not restrict else [p for p in dsl.plans if p.id in restrict]
    state = {
        "database": task.get("db"),
        "question": task["question"],
        "extracted_slots": {k: v for k, v in slots.items() if not k.startswith("_")},
    }
    questions = {
        "plan": {
            "type": "choice",
            "instructions": (
                "Which plan computes exactly this question? Match the aggregation/mechanism, the "
                "entity and the requested output columns. Prefer 'other' over a near miss."
            ),
            "criteria": _catalog_criteria(plans),
        }
    }
    body = client.system_one(state, questions)
    answer = (body.get("answers") or {}).get("plan") or {}
    return {
        "choice": answer.get("choice"),
        "confidence": answer.get("confidence"),
        "probabilities": answer.get("probabilities") or {},
        "request_id": body.get("request_id"),
        "usage": body.get("usage"),
    }


def verify_plan(plan, task: dict, slots: dict, client: JevClient) -> dict:
    """jev(verify): does the chosen plan's output contract match the question?"""
    state = {
        "database": task.get("db"),
        "question": task["question"],
        "chosen_plan": {
            "id": plan.id,
            "description": plan.description,
            "entity": plan.entity,
            "output_contract": _output_description(plan),
            "operator_chain": [next(iter(op)) for op in plan.ops],
        },
        "extracted_slots": {k: v for k, v in slots.items() if not k.startswith("_")},
    }
    questions = {
        "fits": {
            "type": "noul",
            "instructions": (
                "Does this plan compute the quantity the question asks for (same entity, same filters, "
                "same aggregation)? Extra helper columns are harmless, and a narrower output is fine "
                "when the question only asks for those fields. Answer false only if a requested value "
                "is missing, computed differently, or the grain is wrong."
            ),
            "criteria": {"true": "output contract matches the question",
                         "false": "missing/extra columns, wrong grain, or wrong aggregation"},
        }
    }
    body = client.system_one(state, questions)
    answer = (body.get("answers") or {}).get("fits") or {}
    probability = answer.get("noul")
    return {
        "fits": probability,
        "request_id": body.get("request_id"),
        "usage": body.get("usage"),
    }


def selftest() -> int:
    """One-command proof that the real decision model is reachable (no mock)."""
    client = JevClient(JevConfig())
    body = client.system_one(
        {"probe": "spider2-lite jev selftest"},
        {
            "route": {
                "type": "choice",
                "instructions": "Which handler should process this probe?",
                "criteria": {"decision_model": "a structured decision request",
                             "generative_llm": "free-text generation is required"},
            },
            "fits": {"type": "noul", "instructions": "Is this a structured decision probe?"},
        },
    )
    answers = body.get("answers") or {}
    print(f"endpoint      : {client.config.endpoint}")
    print(f"model         : {client.config.model}")
    print(f"request_id    : {body.get('request_id')}")
    print(f"answers       : {json.dumps(answers, ensure_ascii=False)}")
    print(f"usage         : {body.get('usage')}  latency_ms={body.get('latency_ms')}")
    # Connectivity/schema test only: a synthetic probe carries no real semantics, so
    # we assert the contract (choice in criteria, probabilities normalised, noul numeric),
    # not the semantic answer.
    route = answers.get("route") or {}
    fits = answers.get("fits") or {}
    probabilities = route.get("probabilities") or {}
    checks = {
        "choice_in_criteria": route.get("choice") in {"decision_model", "generative_llm"},
        "probabilities_normalised": abs(sum(probabilities.values()) - 1.0) < 0.05,
        "noul_numeric": isinstance(fits.get("noul"), (int, float)) and 0.0 <= float(fits["noul"]) <= 1.0,
        "usage_reported": bool(body.get("usage")),
    }
    print("contract      :", checks)
    ok = all(checks.values())
    print("selftest      :", "OK (real decision model, contract satisfied)" if ok else "CONTRACT VIOLATION")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(selftest())
