#!/usr/bin/env python3
"""Compare two OpenAI-compatible model endpoints against a fixed prompt set.

Usage:
    python3 eval_harness.py --baseline http://localhost:8000/v1 --baseline-model Qwen/Qwen3-0.6B \
                             --candidate http://localhost:8001/v1 --candidate-model mlx-community/Qwen3-0.6B-4bit

Exit code is nonzero if the candidate regresses on any case the baseline passed.
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
        body = json.loads(resp.read())
    latency_ms = (time.monotonic() - started) * 1000
    choice = body["choices"][0]["message"]
    text = (choice.get("content") or "").lower()
    tool_calls = [tc["function"]["name"] for tc in (choice.get("tool_calls") or [])]
    usage = body.get("usage") or {}
    return text, tool_calls, {
        "latency_ms": latency_ms,
        "response_model": body.get("model"),
        "prompt_tokens": usage.get("prompt_tokens") or 0,
        "completion_tokens": usage.get("completion_tokens") or 0,
        "total_tokens": usage.get("total_tokens") or 0,
    }


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


def run_suite(base_url, model, cases, api_key=None):
    results = {}
    for case in cases:
        try:
            text, tool_calls, request_metrics = call_model(base_url, model, case, api_key=api_key)
            failures = check(case, text, tool_calls)
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
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
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
    return {
        "completed": len(completed),
        "p50_latency_ms": percentile(latencies, 50),
        "p95_latency_ms": percentile(latencies, 95),
        "prompt_tokens": sum(metrics["prompt_tokens"] for metrics in completed),
        "completion_tokens": sum(metrics["completion_tokens"] for metrics in completed),
        "total_tokens": sum(metrics["total_tokens"] for metrics in completed),
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

    cases = json.load(open(args.prompts))

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
            f"calls={c_metrics['tool_call_count']} "
            f"latency_ms={c_metrics['latency_ms']:.1f} "
            f"tokens={c_metrics['total_tokens']} "
            f"model={c_metrics['response_model']}{flag}{repeat_flag}"
            if c_metrics["latency_ms"] is not None
            else f"{cid:28s} baseline={b_mark:4s} candidate={c_mark:4s} request_failed{flag}"
        )
        if not c_pass:
            for f in c_fail:
                print(f"    candidate failure: {f}")

    print(f"\n{len(regressions)} regression(s) found: {regressions}")
    print(f"candidate trajectory: {total_calls} total tool call(s), {total_repeated} repeated call(s) across the suite")
    for label, results in (("baseline", baseline_results), ("candidate", candidate_results)):
        metrics = suite_metrics(results)
        p50 = f"{metrics['p50_latency_ms']:.1f}" if metrics["p50_latency_ms"] is not None else "n/a"
        p95 = f"{metrics['p95_latency_ms']:.1f}" if metrics["p95_latency_ms"] is not None else "n/a"
        print(
            f"{label} inference: completed={metrics['completed']}/{len(cases)} "
            f"p50_ms={p50} p95_ms={p95} total_tokens={metrics['total_tokens']} "
            f"prompt_tokens={metrics['prompt_tokens']} completion_tokens={metrics['completion_tokens']}"
        )
    sys.exit(1 if regressions else 0)


if __name__ == "__main__":
    main()
