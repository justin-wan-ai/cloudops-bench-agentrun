"""Environment, paths and model config for the AgentRun migration.

One rule: every credential comes from ``workspace/.env`` (never hardcoded, never
exported into a DSL file).  ``.env`` is loaded with ``setdefault`` so a real
environment variable always wins.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent
RSI_ROOT = WORKSPACE.parent
BENCH_ROOT = RSI_ROOT / "repo"          # Cloud-OpsBench checkout (read-only asset)
BENCHMARK = BENCH_ROOT / "benchmark"
PROCESS_LABEL = BENCH_ROOT / "process-label"

TASKS_FILE = WORKSPACE / "eval" / "tasks_5.json"
DSL_V01 = WORKSPACE / "dsl_versions" / "v0.1.dsl.txt"
TRACES_BASELINE = WORKSPACE / "traces" / "baseline"
TRACES_AGENTRUN = WORKSPACE / "traces" / "agentrun"
RUNS = WORKSPACE / "runs"
COVERAGE = RUNS / "coverage_754.json"
JEV_REQ = WORKSPACE / "jev" / "req"
JEV_ANS = WORKSPACE / "jev" / "ans"
JEV_AUDIT = WORKSPACE / "jev" / "audit"


def load_env(path: Path = WORKSPACE / ".env") -> None:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value:
            os.environ.setdefault(key, value)


load_env()


@dataclass(frozen=True)
class ModelConfig:
    model: str
    api_base: str
    api_key: str
    temperature: float = 0.0
    max_tokens: int = 4096
    timeout: int = 120


def agent_model() -> ModelConfig:
    """DeepSeek endpoint used by the free agent (baseline) and by escalate/report."""
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY is not set in workspace/.env")
    return ModelConfig(
        model=os.environ.get("LLM_MODEL", "deepseek-flash"),
        api_base=os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/"),
        api_key=key,
    )


def jev_ready() -> tuple[bool, str]:
    ok = bool(os.environ.get("DASHSCOPE_API_KEY")) and bool(os.environ.get("DASHSCOPE_OPENAI_BASE_URL"))
    return ok, ("ok" if ok else "DASHSCOPE_API_KEY / DASHSCOPE_OPENAI_BASE_URL missing")


def case_path(system: str, category: str, case: str) -> Path:
    return BENCHMARK / system / category / case
