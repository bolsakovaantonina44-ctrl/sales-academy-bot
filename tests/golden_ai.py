"""Live golden-set quality gate for evaluation behavior.

Synthetic conversations only. This runner calls the configured evaluation model
and checks broad, human-approved quality bands. It is intentionally separate from
normal predeploy because live evaluations are slower and cost-bearing.
Run before/after prompt or rubric changes:
    python -u tests/golden_ai.py

Optionally limit a diagnostic rerun to selected case ids:
    GOLDEN_CASE_IDS=id_one,id_two python -u tests/golden_ai.py
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openai import OpenAI
from academy.ai import AI
from academy.domain import session_empty, SKILLS
from academy.reporting import total_score
from academy.scenarios import template

CASES_PATH = Path(__file__).with_name("golden_cases.json")
# Keep gate calibration separate from production scoring behavior.
MAXIMA = {key: maximum for key, _, maximum in SKILLS}


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


def _expected_statuses(values):
    """Accept the old fixture label `none` as the product-schema value `absent`."""
    return {"absent" if value == "none" else value for value in values}


def _score_for_gate(result):
    """Return a comparable 0..100 score without turning unobserved skills into zero.

    Product reports intentionally allow score=null when a skill was objectively not
    observable. Golden fixtures can be shorter than real sessions, so the gate
    normalizes the observed skills instead of treating valid nulls as a crash.
    """
    score = total_score(result)
    if score is not None:
        return score, []

    if not result or not result.get("simulation_valid") or result.get("technical_partial"):
        return None, ["evaluation_unavailable"]

    skills = result.get("skills") or []
    if len(skills) != len(MAXIMA):
        return None, [f"skills_count={len(skills)} expected={len(MAXIMA)}"]

    observed = [item for item in skills if item.get("score") is not None and item.get("id") in MAXIMA]
    missing = [str(item.get("id", "?")) for item in skills if item.get("score") is None]
    observed_max = sum(MAXIMA[item["id"]] for item in observed)
    if len(observed) < 4 or observed_max <= 0:
        return None, ["too_few_observed_skills=" + ",".join(missing)]

    earned = sum(item["score"] for item in observed)
    normalized = round(100 * earned / observed_max)
    notes = ["normalized_from_observed; null_skills=" + ",".join(missing)] if missing else []
    return normalized, notes


def _goal_severity(actual, expected):
    if actual in expected:
        return None
    # achieved vs partial is often a boundary judgement for synthetic dialogues.
    if actual == "partial" and "achieved" in expected:
        return "soft"
    if actual == "achieved" and "partial" in expected:
        return "soft"
    return "hard"


def _status_severity(actual, expected):
    if actual in expected:
        return None
    # proposed/agreed can vary when the dialogue has action but an incomplete
    # owner/time/condition detail. Absence vs presence remains a hard regression.
    if actual in {"proposed", "agreed"} and expected & {"proposed", "agreed"}:
        return "soft"
    return "hard"


def check(case, result):
    expected = case["expected"]
    score, notes = _score_for_gate(result)
    hard = []
    soft = list(notes)

    if score is None:
        hard.append("unscorable=" + ",".join(notes or ["unknown"]))
    elif not expected["score_min"] <= score <= expected["score_max"]:
        distance = expected["score_min"] - score if score < expected["score_min"] else score - expected["score_max"]
        problem = f"score={score} expected={expected['score_min']}..{expected['score_max']}"
        # Live LLM grading is non-deterministic. Small/medium drift is diagnostic;
        # large drift still blocks deployment.
        (soft if distance <= 20 else hard).append(problem)

    goal = result.get("goal")
    goal_severity = _goal_severity(goal, set(expected["goal"]))
    if goal_severity:
        problem = f"goal={goal} expected={expected['goal']}"
        (soft if goal_severity == "soft" else hard).append(problem)

    expected_statuses = _expected_statuses(expected["next_step_status"])
    status = result.get("next_step_status")
    status_severity = _status_severity(status, expected_statuses)
    if status_severity:
        problem = f"next_step_status={status} expected={sorted(expected_statuses)}"
        (soft if status_severity == "soft" else hard).append(problem)

    return score, hard, soft


def main():
    all_cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    if len(all_cases) < 20:
        raise RuntimeError(f"Golden set too small: {len(all_cases)}")

    selected = [x.strip() for x in os.getenv("GOLDEN_CASE_IDS", "").split(",") if x.strip()]
    cases = all_cases
    if selected:
        wanted = set(selected)
        cases = [case for case in all_cases if case["id"] in wanted]
        missing = sorted(wanted - {case["id"] for case in cases})
        if missing:
            raise RuntimeError("Unknown GOLDEN_CASE_IDS: " + ", ".join(missing))
        print(f"GOLDEN_FILTER selected={len(cases)} ids={','.join(case['id'] for case in cases)}", flush=True)

    model = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
    eval_model = os.getenv("OPENAI_EVAL_MODEL", model)
    failures = []
    warnings = []
    with OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=90, max_retries=1) as client:
        ai = AI(client, model, "unused", eval_model)
        for index, case in enumerate(cases, 1):
            session = build_session(case, index)
            try:
                result = ai.evaluate(session)
                score, hard, soft = check(case, result)
            except Exception as exc:
                score = None
                hard = [f"exception={type(exc).__name__}: {exc}"]
                soft = []
            status = "FAIL" if hard else ("WARN" if soft else "PASS")
            details = hard + soft
            print(
                f"GOLDEN {index:02d}/{len(cases)} {status} id={case['id']} "
                f"level={case['level']} score={score}"
                + ("" if not details else " problems=" + " | ".join(details)),
                flush=True,
            )
            if hard:
                failures.append((case["id"], hard))
            elif soft:
                warnings.append((case["id"], soft))

    passed = len(cases) - len(failures)
    print(
        f"GOLDEN_SUMMARY passed={passed} failed={len(failures)} warnings={len(warnings)} "
        f"total={len(cases)} full_set={len(all_cases)} model={eval_model}",
        flush=True,
    )
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
