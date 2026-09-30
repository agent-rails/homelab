#!/usr/bin/env python3
"""Compare two OpenAI-compatible model endpoints against a fixed prompt set.

Usage:
    python3 eval_harness.py --baseline http://localhost:8000/v1 --baseline-model Qwen/Qwen3-0.6B \
                             --candidate http://localhost:8001/v1 --candidate-model mlx-community/Qwen3-0.6B-4bit

Exit code is nonzero if the candidate regresses, if any case fails on BOTH sides
(absolute gate), or if any request errored. Token usage is reported; absent usage is
reported as UNKNOWN and never counted as zero.
"""
import argparse
import json
import sys
import urllib.request


def call_model(base_url, model, case, api_key=None):
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": case["prompt"]}],
        "max_tokens": 128,
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if "tools" in case:
        payload["tools"] = case["tools"]
        payload["tool_choice"] = "auto"

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode(),
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read())
    choice = body["choices"][0]["message"]
    text = (choice.get("content") or "").lower()
    tool_calls = [tc["function"]["name"] for tc in (choice.get("tool_calls") or [])]
    # Absent usage stays None. Recording it as 0 would claim the inference was free,
    # which silently corrupts every cost comparison built on this harness.
    raw = body.get("usage") or {}
    usage = {
        "prompt_tokens": raw.get("prompt_tokens"),
        "completion_tokens": raw.get("completion_tokens"),
        "total_tokens": raw.get("total_tokens"),
    }
    return text, tool_calls, usage


def trajectory_metrics(tool_calls):
    """Per-response trajectory signals. This harness is single-turn (one call, one
    response), so these measure within-turn behavior, not a multi-step agent loop:
    a real cross-turn trajectory tracker is a separate, bigger thing this doesn't do.

    - tool_call_count: how many tool calls the model requested in this one response.
    - repeated_call_count: how many of those are the same tool name called more than
      once in the same response -- a within-turn thrash/redundancy signal.
    """
    tool_call_count = len(tool_calls)
    seen = set()
    repeated_call_count = 0
    for name in tool_calls:
        if name in seen:
            repeated_call_count += 1
        seen.add(name)
    return {"tool_call_count": tool_call_count, "repeated_call_count": repeated_call_count}


def check(case, text, tool_calls):
    failures = []
    if "must_contain" in case:
        if not any(s.lower() in text for s in case["must_contain"]):
            failures.append(f"expected one of {case['must_contain']!r} in response, got: {text[:120]!r}")
    if "must_call_tool" in case:
        expected = case["must_call_tool"]
        if expected not in tool_calls:
            failures.append(f"expected tool call {expected!r}, got calls: {tool_calls}")
        elif tool_calls != [expected]:
            failures.append(
                f"tool selection imprecise: expected exactly [{expected!r}], got {tool_calls} "
                f"(correct tool was called, but with extra/duplicate calls alongside it)"
            )
    if "must_not_call_tool" in case:
        if case["must_not_call_tool"] in tool_calls:
            failures.append(f"expected NO call to {case['must_not_call_tool']!r}, but it was called")
    return failures


def validate_cases(cases):
    """Reject a malformed suite before spending any inference on it.

    A duplicate id silently overwrites the earlier case in the results dict, so the
    suite would report fewer cases than it ran and the lost one could never regress.
    """
    problems, seen = [], set()
    if not cases:
        problems.append("suite is empty")
    for i, case in enumerate(cases):
        cid = case.get("id")
        if not isinstance(cid, str) or not cid.strip():
            problems.append(f"case #{i}: missing or non-string 'id'")
            continue
        if cid in seen:
            problems.append(f"case #{i}: duplicate id {cid!r} — would silently overwrite the earlier case")
        seen.add(cid)
        if not isinstance(case.get("prompt"), str) or not case["prompt"].strip():
            problems.append(f"case {cid!r}: missing or empty 'prompt'")
        if not any(k in case for k in ("must_contain", "must_call_tool", "must_not_call_tool")):
            problems.append(f"case {cid!r}: no assertion — it can never fail, so it grades nothing")
    return problems


def run_suite(base_url, model, cases, api_key=None):
    results = {}
    for case in cases:
        try:
            text, tool_calls, usage = call_model(base_url, model, case, api_key=api_key)
            failures = check(case, text, tool_calls)
            metrics = trajectory_metrics(tool_calls)
            metrics["usage"] = usage
            metrics["infra_error"] = False
            results[case["id"]] = (len(failures) == 0, failures, metrics)
        except Exception as e:
            # An infrastructure error is not a quality signal. Kept separate so a
            # broken endpoint cannot be read as "the model failed the task".
            results[case["id"]] = (False, [f"request error: {e}"], {
                "tool_call_count": 0, "repeated_call_count": 0,
                "usage": {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None},
                "infra_error": True,
            })
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True, help="baseline OpenAI-compatible base_url")
    ap.add_argument("--baseline-model", required=True)
    ap.add_argument("--candidate", required=True, help="candidate OpenAI-compatible base_url")
    ap.add_argument("--candidate-model", required=True)
    ap.add_argument("--prompts", default="prompts.json")
    ap.add_argument("--baseline-api-key", default=None, help="Bearer token for the baseline endpoint")
    ap.add_argument("--candidate-api-key", default=None, help="Bearer token for the candidate endpoint")
    args = ap.parse_args()

    with open(args.prompts) as fh:
        cases = json.load(fh)

    problems = validate_cases(cases)
    if problems:
        print("suite rejected — fix these before spending inference on it:", file=sys.stderr)
        for pr in problems:
            print(f"  - {pr}", file=sys.stderr)
        sys.exit(2)

    print(f"=== baseline: {args.baseline_model} @ {args.baseline} ===")
    baseline_results = run_suite(args.baseline, args.baseline_model, cases, api_key=args.baseline_api_key)

    print(f"=== candidate: {args.candidate_model} @ {args.candidate} ===")
    candidate_results = run_suite(args.candidate, args.candidate_model, cases, api_key=args.candidate_api_key)

    print("\n=== results ===")
    regressions = []
    total_calls = 0
    total_repeated = 0
    for case in cases:
        cid = case["id"]
        b_pass, b_fail, b_metrics = baseline_results[cid]
        c_pass, c_fail, c_metrics = candidate_results[cid]
        b_mark = "PASS" if b_pass else "FAIL"
        c_mark = "PASS" if c_pass else "FAIL"
        flag = ""
        if b_pass and not c_pass:
            flag = "  <-- REGRESSION"
            regressions.append(cid)
        total_calls += c_metrics["tool_call_count"]
        total_repeated += c_metrics["repeated_call_count"]
        repeat_flag = f"  <-- {c_metrics['repeated_call_count']} repeated call(s)" if c_metrics["repeated_call_count"] else ""
        print(
            f"{cid:28s} baseline={b_mark:4s} candidate={c_mark:4s} "
            f"calls={c_metrics['tool_call_count']}{flag}{repeat_flag}"
        )
        if not c_pass:
            for f in c_fail:
                print(f"    candidate failure: {f}")

    # ABSOLUTE GATE. Relative-only comparison passes a suite where BOTH sides fail
    # every case, which is the vacuous-test failure mode: green CI, zero signal.
    shared_failures = [
        c["id"] for c in cases
        if not baseline_results[c["id"]][0] and not candidate_results[c["id"]][0]
        and not candidate_results[c["id"]][2].get("infra_error")
    ]
    infra_errors = [c["id"] for c in cases if candidate_results[c["id"]][2].get("infra_error")
                    or baseline_results[c["id"]][2].get("infra_error")]

    def usage_total(results):
        known = [r[2]["usage"].get("total_tokens") for r in results.values()
                 if r[2].get("usage") and r[2]["usage"].get("total_tokens") is not None]
        missing = len(results) - len(known)
        return sum(known), missing

    b_tok, b_missing = usage_total(baseline_results)
    c_tok, c_missing = usage_total(candidate_results)
    print(f"\nbaseline tokens:  {b_tok}" + (f"  (UNKNOWN for {b_missing} case(s))" if b_missing else ""))
    print(f"candidate tokens: {c_tok}" + (f"  (UNKNOWN for {c_missing} case(s))" if c_missing else ""))
    if b_missing or c_missing:
        print("  cost comparison is INCOMPLETE — unknown usage is not zero usage")

    if infra_errors:
        print(f"\n{len(infra_errors)} infrastructure error(s) (not a quality signal): {infra_errors}")
    if shared_failures:
        print(f"{len(shared_failures)} case(s) FAILED ON BOTH sides: {shared_failures}")
        print("  the suite is not passing in absolute terms — a relative-only gate would hide this")

    print(f"\n{len(regressions)} regression(s) found: {regressions}")
    print(f"candidate trajectory: {total_calls} total tool call(s), {total_repeated} repeated call(s) across the suite")
    sys.exit(1 if (regressions or shared_failures or infra_errors) else 0)


if __name__ == "__main__":
    main()
