#!/usr/bin/env python3
"""Compare two OpenAI-compatible model endpoints against a fixed prompt set.

Usage:
    python3 eval_harness.py --baseline http://localhost:8000/v1 --baseline-model Qwen/Qwen3-0.6B \
                             --candidate http://localhost:8001/v1 --candidate-model mlx-community/Qwen3-0.6B-4bit

Exit code is nonzero if the candidate regresses on any case the baseline passed.
"""
import argparse
import json
import sys
import urllib.request


def call_model(base_url, model, case):
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

    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read())
    choice = body["choices"][0]["message"]
    text = (choice.get("content") or "").lower()
    tool_calls = [tc["function"]["name"] for tc in (choice.get("tool_calls") or [])]
    return text, tool_calls


def check(case, text, tool_calls):
    failures = []
    if "must_contain" in case:
        if not any(s.lower() in text for s in case["must_contain"]):
            failures.append(f"expected one of {case['must_contain']!r} in response, got: {text[:120]!r}")
    if "must_call_tool" in case:
        if case["must_call_tool"] not in tool_calls:
            failures.append(f"expected tool call {case['must_call_tool']!r}, got calls: {tool_calls}")
    if "must_not_call_tool" in case:
        if case["must_not_call_tool"] in tool_calls:
            failures.append(f"expected NO call to {case['must_not_call_tool']!r}, but it was called")
    return failures


def run_suite(base_url, model, cases):
    results = {}
    for case in cases:
        try:
            text, tool_calls = call_model(base_url, model, case)
            failures = check(case, text, tool_calls)
            results[case["id"]] = (len(failures) == 0, failures)
        except Exception as e:
            results[case["id"]] = (False, [f"request error: {e}"])
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True, help="baseline OpenAI-compatible base_url")
    ap.add_argument("--baseline-model", required=True)
    ap.add_argument("--candidate", required=True, help="candidate OpenAI-compatible base_url")
    ap.add_argument("--candidate-model", required=True)
    ap.add_argument("--prompts", default="prompts.json")
    args = ap.parse_args()

    cases = json.load(open(args.prompts))

    print(f"=== baseline: {args.baseline_model} @ {args.baseline} ===")
    baseline_results = run_suite(args.baseline, args.baseline_model, cases)

    print(f"=== candidate: {args.candidate_model} @ {args.candidate} ===")
    candidate_results = run_suite(args.candidate, args.candidate_model, cases)

    print("\n=== results ===")
    regressions = []
    for case in cases:
        cid = case["id"]
        b_pass, b_fail = baseline_results[cid]
        c_pass, c_fail = candidate_results[cid]
        b_mark = "PASS" if b_pass else "FAIL"
        c_mark = "PASS" if c_pass else "FAIL"
        flag = ""
        if b_pass and not c_pass:
            flag = "  <-- REGRESSION"
            regressions.append(cid)
        print(f"{cid:28s} baseline={b_mark:4s} candidate={c_mark:4s}{flag}")
        if not c_pass:
            for f in c_fail:
                print(f"    candidate failure: {f}")

    print(f"\n{len(regressions)} regression(s) found: {regressions}")
    sys.exit(1 if regressions else 0)


if __name__ == "__main__":
    main()
