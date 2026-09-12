"""Live golden-set quality gate for evaluation behavior.

Synthetic conversations only. This runner calls the configured evaluation model
and checks broad, human-approved quality bands. It is intentionally separate from
normal predeploy because live evaluations are slower and cost-bearing.
Run before/after prompt or rubric changes:
    python -u tests/golden_ai.py
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openai import OpenAI
from academy.ai import AI
from academy.domain import session_empty
from academy.reporting import total_score
from academy.scenarios import template

CASES_PATH = Path(__file__).with_name("golden_cases.json")


def build_session(case, session_id):
    s = session_empty()
    s["id"] = session_id
    s["fields"].update(case["fields"])
    s["training_focus"] = case.get("focus")
    # Evaluation expects the same valid client-card contract as a real completed
    # training session. Golden cases test scoring/attribution, not card generation,
    # so use a deterministic synthetic fixture and keep case-specific dialogue/fields.
    s["card"] = template("2")["card"]
    s["history"] = [dict(role=role, content=text) for role, text in case["dialogue"]]
    s["phase"] = "closed"
    return s


def check(case, result):
    expected = case["expected"]
    score = total_score(result)
    failures = []
    if not expected["score_min"] <= score <= expected["score_max"]:
        failures.append(f"score={score} expected={expected['score_min']}..{expected['score_max']}")
    if result.get("goal") not in expected["goal"]:
        failures.append(f"goal={result.get('goal')} expected={expected['goal']}")
    if result.get("next_step_status") not in expected["next_step_status"]:
        failures.append(
            f"next_step_status={result.get('next_step_status')} expected={expected['next_step_status']}"
        )
    return score, failures


def main():
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    if len(cases) < 20:
        raise RuntimeError(f"Golden set too small: {len(cases)}")
    model = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
    eval_model = os.getenv("OPENAI_EVAL_MODEL", model)
    failures = []
    with OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=90, max_retries=1) as client:
        ai = AI(client, model, "unused", eval_model)
        for index, case in enumerate(cases, 1):
            session = build_session(case, index)
            try:
                result = ai.evaluate(session)
                score, problems = check(case, result)
            except Exception as exc:
                score = None
                problems = [f"exception={type(exc).__name__}: {exc}"]
            status = "PASS" if not problems else "FAIL"
            print(
                f"GOLDEN {index:02d}/{len(cases)} {status} id={case['id']} "
                f"level={case['level']} score={score}"
                + ("" if not problems else " problems=" + " | ".join(problems)),
                flush=True,
            )
            if problems:
                failures.append((case["id"], problems))
    passed = len(cases) - len(failures)
    print(f"GOLDEN_SUMMARY passed={passed} failed={len(failures)} total={len(cases)} model={eval_model}", flush=True)
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
