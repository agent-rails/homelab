#!/usr/bin/env python3
"""Fixtures for the eval harness's own gate. No network.

These exist because the gate itself was the bug: a relative-only comparison exits 0
on a suite where every case fails on both sides.
"""
import io, json, subprocess, sys, tempfile, unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import eval_harness as eh

CASES = [
    {"id": "c1", "prompt": "p1", "must_contain": ["yes"]},
    {"id": "c2", "prompt": "p2", "must_contain": ["yes"]},
]


def fake(text, usage=None, tools=None):
    return text, tools or [], usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


def run_main(baseline_text, candidate_text, cases=CASES, usage=None, raise_on=None):
    """Run main() with call_model stubbed. Returns (exit_code, stdout)."""
    def stub(base_url, model, case, api_key=None):
        if raise_on and case["id"] in raise_on and "cand" in base_url:
            raise RuntimeError("connection refused")
        text = candidate_text if "cand" in base_url else baseline_text
        return fake(text, usage)

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(cases, f); path = f.name
    argv = ["eval_harness.py", "--baseline", "http://base/v1", "--baseline-model", "b",
            "--candidate", "http://cand/v1", "--candidate-model", "c", "--prompts", path]
    orig_call, orig_argv = eh.call_model, sys.argv
    eh.call_model, sys.argv = stub, argv
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            eh.main()
        code = 0
    except SystemExit as e:
        code = e.code or 0
    finally:
        eh.call_model, sys.argv = orig_call, orig_argv
    return code, buf.getvalue()


class GateTests(unittest.TestCase):
    def test_both_fail_is_not_a_pass(self):
        """The original bug: everything failing exited 0."""
        code, out = run_main("no", "no")
        self.assertEqual(code, 1, "a suite failing on both sides must NOT exit 0")
        self.assertIn("FAILED ON BOTH", out)

    def test_all_pass_exits_zero(self):
        code, out = run_main("yes", "yes")
        self.assertEqual(code, 0)

    def test_regression_still_caught(self):
        code, out = run_main("yes", "no")
        self.assertEqual(code, 1)
        self.assertIn("REGRESSION", out)

    def test_missing_usage_is_unknown_not_zero(self):
        code, out = run_main("yes", "yes",
                             usage={"prompt_tokens": None, "completion_tokens": None, "total_tokens": None})
        self.assertIn("UNKNOWN", out)
        self.assertIn("INCOMPLETE", out)

    def test_usage_is_reported(self):
        code, out = run_main("yes", "yes")
        self.assertIn("candidate tokens: 30", out)

    def test_infra_error_fails_and_is_separated(self):
        code, out = run_main("yes", "yes", raise_on={"c1"})
        self.assertEqual(code, 1)
        self.assertIn("infrastructure error", out)


class ValidationTests(unittest.TestCase):
    def test_duplicate_id(self):
        p = eh.validate_cases([{"id": "a", "prompt": "x", "must_contain": ["y"]},
                               {"id": "a", "prompt": "z", "must_contain": ["y"]}])
        self.assertTrue(any("duplicate" in x for x in p))

    def test_assertionless_case(self):
        self.assertTrue(any("no assertion" in x for x in eh.validate_cases([{"id": "a", "prompt": "x"}])))

    def test_empty_suite(self):
        self.assertTrue(any("empty" in x for x in eh.validate_cases([])))

    def test_valid_suite_clean(self):
        self.assertEqual(eh.validate_cases(CASES), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
