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
import math
import sys
import time
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
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read(), parse_constant=_reject_json_constant)
    latency_ms = (time.monotonic() - started) * 1000
    choice = body["choices"][0]["message"]
    text = (choice.get("content") or "").lower()
    raw_calls = choice.get("tool_calls") or []
    tool_calls = [tc["function"]["name"] for tc in raw_calls]
    tool_details = []
    for tc in raw_calls:
        raw_args = tc["function"].get("arguments")
        try:
            args = (json.loads(raw_args, object_pairs_hook=_unique_json_object,
                               parse_constant=_reject_json_constant) if isinstance(raw_args, str) else raw_args)
        except (ValueError, TypeError):
            args = None
        if not isinstance(args, dict):
            args = None
        tool_details.append({"name": tc["function"]["name"], "arguments": args})
    usage = body.get("usage") or {}
    return text, tool_calls, {
        "latency_ms": latency_ms,
        "response_model": body.get("model"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "tool_details": tool_details,
    }


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError(f"non-finite JSON value: {value}")


def _canonical_json(value):
    try:
        return json.dumps(value, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        return None


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


def check(case, text, tool_calls, tool_details=None):
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
    if "must_call_tool_args" in case:
        expected = case["must_call_tool_args"]
        matches = [call for call in (tool_details or []) if call["name"] == expected["name"]]
        if (len(tool_details or []) != 1 or len(matches) != 1 or
                not isinstance(matches[0]["arguments"], dict) or
                _canonical_json(matches[0]["arguments"]) is None or
                _canonical_json(matches[0]["arguments"]) != _canonical_json(expected["arguments"])):
            failures.append(f"expected only one {expected['name']!r} call with specified arguments")
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
        if not any(k in case for k in ("must_contain", "must_call_tool", "must_not_call_tool", "must_call_tool_args")):
            problems.append(f"case {cid!r}: no assertion — it can never fail, so it grades nothing")
        tools = case.get("tools", [])
        if not isinstance(tools, list) or any(
            not isinstance(tool, dict) or tool.get("type") != "function" or
            not isinstance(tool.get("function"), dict) or
            not isinstance(tool["function"].get("name"), str) or not tool["function"]["name"]
            for tool in tools
        ):
            problems.append(f"case {cid!r}: 'tools' must be a list of named function schemas")
            offered = set()
        else:
            offered = {tool["function"]["name"] for tool in tools}
        asserted_name = case.get("must_call_tool")
        if "must_call_tool" in case and (not isinstance(asserted_name, str) or asserted_name not in offered):
            problems.append(f"case {cid!r}: asserted tool {asserted_name!r} is not declared in 'tools'")
        args_assertion = case.get("must_call_tool_args")
        args_name = args_assertion.get("name") if isinstance(args_assertion, dict) else None
        forbidden = case.get("must_not_call_tool")
        if "must_not_call_tool" in case and forbidden is not None and forbidden in (asserted_name, args_name):
            problems.append(f"case {cid!r}: cannot require and forbid the same tool")
        if "must_call_tool_args" in case:
            expected = case["must_call_tool_args"]
            if not isinstance(expected, dict) or not isinstance(expected.get("name"), str) or not expected["name"] or not isinstance(expected.get("arguments"), dict):
                problems.append(f"case {cid!r}: 'must_call_tool_args' needs a tool name and argument object")
            elif expected["name"] not in offered:
                problems.append(f"case {cid!r}: asserted tool {expected['name']!r} is not declared in 'tools'")
            elif _canonical_json(expected["arguments"]) is None:
                problems.append(f"case {cid!r}: 'must_call_tool_args' contains a non-JSON value")
    return problems


def run_suite(base_url, model, cases, api_key=None):
    results = {}
    for case in cases:
        try:
            text, tool_calls, request_metrics = call_model(base_url, model, case, api_key=api_key)
            failures = check(case, text, tool_calls, request_metrics.get("tool_details"))
            metrics = {**trajectory_metrics(tool_calls), **request_metrics}
            results[case["id"]] = (len(failures) == 0, failures, metrics)
        except Exception as e:
            results[case["id"]] = (
                False,
                [f"request error: {e}"],
                {
                    "tool_call_count": 0,
                    "repeated_call_count": 0,
                    "latency_ms": None,
                    "response_model": None,
                    "prompt_tokens": None,
                    "completion_tokens": None,
                    "total_tokens": None,
                    "infra_error": True,
                },
            )
    return results


def percentile(values, percentile_value):
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil((percentile_value / 100) * len(ordered)) - 1)
    return ordered[index]


def suite_metrics(results):
    completed = [metrics for _, _, metrics in results.values() if metrics["latency_ms"] is not None]
    latencies = [metrics["latency_ms"] for metrics in completed]
    def total_or_unknown(field):
        values = [metrics.get(field) for metrics in completed]
        return sum(values) if values and all(value is not None for value in values) else None

    return {
        "completed": len(completed),
        "p50_latency_ms": percentile(latencies, 50),
        "p95_latency_ms": percentile(latencies, 95),
        "prompt_tokens": total_or_unknown("prompt_tokens"),
        "completion_tokens": total_or_unknown("completion_tokens"),
        "total_tokens": total_or_unknown("total_tokens"),
    }


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

    try:
        with open(args.prompts) as fh:
            cases = json.load(fh, parse_constant=_reject_json_constant,
                              object_pairs_hook=_unique_json_object)
    except ValueError as exc:
        print(f"suite rejected — invalid JSON: {exc}", file=sys.stderr)
        sys.exit(2)

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
        if b_pass and not c_pass and not c_metrics.get("infra_error"):
            flag = "  <-- REGRESSION"
            regressions.append(cid)
        total_calls += c_metrics["tool_call_count"]
        total_repeated += c_metrics["repeated_call_count"]
        repeat_flag = f"  <-- {c_metrics['repeated_call_count']} repeated call(s)" if c_metrics["repeated_call_count"] else ""
        print(
            f"{cid:28s} baseline={b_mark:4s} candidate={c_mark:4s} "
            f"calls={c_metrics['tool_call_count']} "
            f"latency_ms={c_metrics['latency_ms']:.1f} "
            f"tokens={c_metrics['total_tokens'] if c_metrics['total_tokens'] is not None else 'UNKNOWN'} "
            f"model={c_metrics['response_model']}{flag}{repeat_flag}"
            if c_metrics["latency_ms"] is not None
            else f"{cid:28s} baseline={b_mark:4s} candidate={c_mark:4s} request_failed{flag}"
        )
        if not c_pass:
            for f in c_fail:
                print(f"    candidate failure: {f}")

    # ABSOLUTE GATE. Relative-only comparison passes a suite where BOTH sides fail
    # every case, which is the vacuous-test failure mode: green CI, zero signal.
    shared_failures = [
        c["id"] for c in cases
        if not baseline_results[c["id"]][0] and not candidate_results[c["id"]][0]
        and not baseline_results[c["id"]][2].get("infra_error")
        and not candidate_results[c["id"]][2].get("infra_error")
    ]
    infra_errors = [c["id"] for c in cases if candidate_results[c["id"]][2].get("infra_error")
                    or baseline_results[c["id"]][2].get("infra_error")]

    def usage_total(results):
        known = [r[2].get("total_tokens") for r in results.values()
                 if r[2].get("total_tokens") is not None]
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
    for label, results in (("baseline", baseline_results), ("candidate", candidate_results)):
        metrics = suite_metrics(results)
        p50 = f"{metrics['p50_latency_ms']:.1f}" if metrics["p50_latency_ms"] is not None else "n/a"
        p95 = f"{metrics['p95_latency_ms']:.1f}" if metrics["p95_latency_ms"] is not None else "n/a"
        print(
            f"{label} inference: completed={metrics['completed']}/{len(cases)} "
            f"p50_ms={p50} p95_ms={p95} total_tokens={metrics['total_tokens'] if metrics['total_tokens'] is not None else 'UNKNOWN'} "
            f"prompt_tokens={metrics['prompt_tokens'] if metrics['prompt_tokens'] is not None else 'UNKNOWN'} "
            f"completion_tokens={metrics['completion_tokens'] if metrics['completion_tokens'] is not None else 'UNKNOWN'}"
        )
    sys.exit(1 if regressions or shared_failures or infra_errors else 0)


if __name__ == "__main__":
    main()
