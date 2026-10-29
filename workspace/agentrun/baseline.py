"""P0/P3 baseline: run the stock Cloud-OpsBench ReAct agent on the frozen 5-case set.

This is the *before* side of the comparison.  Nothing in the benchmark is
modified: we call ``run.run_case`` exactly like ``python run.py`` does, only with
a per-case config so the five cases (which live in five different categories)
can share one driver.

    python3 -m agentrun.baseline --max-iterations 20
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import benchbridge  # noqa: F401  (sys.path side effect: repo agent dir)
from pathlib import Path

from .env import TASKS_FILE, TRACES_BASELINE, agent_model, case_path

import run as bench_run  # repo/agents/cloudops_agent/run.py


def build_config(case: dict, max_iterations: int) -> dict:
    model = agent_model()
    return {
        "model": {
            "model": model.model,
            "provider": "openai_compatible",
            "api_base": model.api_base,
            "api_key": model.api_key,
            "temperature": 0.0,
            "max_tokens": model.max_tokens,
            "timeout": model.timeout,
        },
        "diagnosis": {
            "max_iterations": max_iterations,
            "system": case["system"],
            "fault_category": case["category"],
            "case_name": case["case"],
            "dataset_root": str(bench_run.PROJECT_ROOT.parent.parent),
            "save_root": str(TRACES_BASELINE),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-iterations", type=int, default=20)
    parser.add_argument("--only", default="", help="comma separated case ids")
    parser.add_argument("--tasks-file", default=str(TASKS_FILE))
    args = parser.parse_args(argv)

    cases = json.loads(Path(args.tasks_file).read_text())["cases"]
    if args.only:
        wanted = {item.strip() for item in args.only.split(",") if item.strip()}
        cases = [c for c in cases if c["id"] in wanted]

    model = agent_model()
    runner = bench_run.build_model_runner(build_config(cases[0], args.max_iterations)["model"])
    print(f"[BASELINE] n={len(cases)} model={model.model} save_root={TRACES_BASELINE}", flush=True)

    for index, case in enumerate(cases, 1):
        started = time.time()
        path = case_path(case["system"], case["category"], case["case"])
        print(f"[RUN] {index}/{len(cases)} {case['id']} ", end="", flush=True)
        try:
            bench_run.run_case(build_config(case, args.max_iterations), path, runner)
        except Exception as exc:  # keep going, the failure is itself data
            print(f"[ERROR] {case['id']} {type(exc).__name__}: {exc}", flush=True)
            continue
        print(f"    wall={time.time() - started:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
