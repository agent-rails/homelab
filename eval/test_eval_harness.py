import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch


MODULE_PATH = pathlib.Path(__file__).with_name("eval_harness.py")
SPEC = importlib.util.spec_from_file_location("eval_harness", MODULE_PATH)
eval_harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(eval_harness)


class EvalHarnessMetricsTests(unittest.TestCase):
    @patch.object(eval_harness.urllib.request, "urlopen")
    def test_call_model_normalizes_null_usage(self, urlopen):
        response = MagicMock()
        response.read.return_value = json.dumps(
            {
                "model": "ollama-default",
                "choices": [{"message": {"content": "42", "tool_calls": []}}],
                "usage": {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None},
            }
        ).encode()
        urlopen.return_value.__enter__.return_value = response

        text, tool_calls, metrics = eval_harness.call_model(
            "http://localhost:4000/v1",
            "ollama-default",
            {"prompt": "What is 12 + 30?"},
            api_key="test-key",
        )

        self.assertEqual(text, "42")
        self.assertEqual(tool_calls, [])
        self.assertEqual(metrics["response_model"], "ollama-default")
        self.assertIsNone(metrics["prompt_tokens"])
        self.assertIsNone(metrics["completion_tokens"])
        self.assertIsNone(metrics["total_tokens"])

    def test_percentile_uses_nearest_rank(self):
        values = [10.0, 20.0, 30.0, 40.0]

        self.assertEqual(eval_harness.percentile(values, 50), 20.0)
        self.assertEqual(eval_harness.percentile(values, 95), 40.0)
        self.assertIsNone(eval_harness.percentile([], 95))

    def test_suite_metrics_excludes_failed_requests(self):
        results = {
            "passed": (
                True,
                [],
                {
                    "latency_ms": 25.0,
                    "prompt_tokens": 10,
                    "completion_tokens": 4,
                    "total_tokens": 14,
                },
            ),
            "failed_request": (
                False,
                ["request error"],
                {
                    "latency_ms": None,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            ),
        }

        self.assertEqual(
            eval_harness.suite_metrics(results),
            {
                "completed": 1,
                "p50_latency_ms": 25.0,
                "p95_latency_ms": 25.0,
                "prompt_tokens": 10,
                "completion_tokens": 4,
                "total_tokens": 14,
            },
        )

    @patch.object(eval_harness.urllib.request, "urlopen")
    def test_tool_arguments_are_checked_without_executing_tool(self, urlopen):
        response = MagicMock()
        response.read.return_value = json.dumps({
            "model": "fixture",
            "choices": [{"message": {"content": "", "tool_calls": [{
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"/wrong"}'},
            }]}}],
        }).encode()
        urlopen.return_value.__enter__.return_value = response
        case = {
            "id": "wrong-path", "prompt": "Read the allowed file",
            "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}],
            "must_call_tool_args": {"name": "read_file", "arguments": {"path": "/allowed"}},
        }
        self.assertEqual(eval_harness.validate_cases([case]), [])
        result = eval_harness.run_suite("http://localhost:4000/v1", "fixture", [case])
        self.assertFalse(result["wrong-path"][0])
        self.assertIn("specified arguments", result["wrong-path"][1][0])
        self.assertEqual(eval_harness.check(case, "", ["read_file", "bash"], [
            {"name": "read_file", "arguments": {"path": "/allowed"}},
            {"name": "bash", "arguments": {"command": "echo surprise"}},
        ]), ["expected only one 'read_file' call with specified arguments"])

    def test_malformed_tool_argument_assertion_is_rejected(self):
        case = {"id": "bad", "prompt": "Read", "must_call_tool_args": {"name": "read_file"}}
        self.assertIn("case 'bad': 'must_call_tool_args' needs a tool name and argument object",
                      eval_harness.validate_cases([case]))

    def test_exact_arguments_reject_wrong_json_types_and_missing_fields(self):
        case = {"must_call_tool_args": {"name": "update", "arguments": {"enabled": True, "count": 1}}}
        for arguments in ({"enabled": 1, "count": 1}, {"enabled": True, "count": 1.0}, None):
            with self.subTest(arguments=arguments):
                self.assertTrue(eval_harness.check(case, "", ["update"],
                    [{"name": "update", "arguments": arguments}]))
        self.assertEqual(eval_harness.check(case, "", ["update"],
            [{"name": "update", "arguments": {"count": 1, "enabled": True}}]), [])

    @patch.object(eval_harness.urllib.request, "urlopen")
    def test_missing_or_duplicate_tool_arguments_cannot_pass_empty_assertion(self, urlopen):
        case = {"prompt": "Call ping", "must_call_tool_args": {"name": "ping", "arguments": {}}}
        for function in ({"name": "ping"}, {"name": "ping", "arguments": '{"x":1,"x":2}'}):
            response = MagicMock()
            response.read.return_value = json.dumps({"choices": [{"message": {
                "tool_calls": [{"type": "function", "function": function}],
            }}]}).encode()
            urlopen.return_value.__enter__.return_value = response
            with self.subTest(function=function):
                with self.assertRaises(ValueError):
                    eval_harness.call_model("http://localhost:4000/v1", "fixture", case)

    def test_asserted_tool_must_be_offered(self):
        case = {"id": "missing", "prompt": "Call ping", "tools": [],
                "must_call_tool_args": {"name": "ping", "arguments": {}}}
        self.assertIn("case 'missing': asserted tool 'ping' is not declared in 'tools'",
                      eval_harness.validate_cases([case]))

    def test_suite_rejects_nonfinite_and_contradictory_assertions(self):
        tool = {"type": "function", "function": {"name": "ping"}}
        invalid = {"id": "invalid", "prompt": "Call ping", "tools": [tool],
                   "must_call_tool_args": {"name": "ping", "arguments": {"count": float("nan")}},
                   "must_not_call_tool": "ping"}
        problems = eval_harness.validate_cases([invalid])
        self.assertTrue(any("non-JSON value" in problem for problem in problems))
        self.assertTrue(any("cannot require and forbid" in problem for problem in problems))
        legacy = {"id": "legacy", "prompt": "Call ping", "must_call_tool": "ping"}
        self.assertTrue(any("not declared" in problem for problem in eval_harness.validate_cases([legacy])))
        conflict = {"id": "conflict", "prompt": "Call ping", "tools": [tool,
                    {"type": "function", "function": {"name": "pong"}}],
                    "must_call_tool": "ping", "must_call_tool_args": {"name": "pong", "arguments": {}}}
        self.assertTrue(any("different tools" in problem for problem in eval_harness.validate_cases([conflict])))
        self.assertTrue(eval_harness.validate_cases({"id": "not-a-list"}))
        self.assertTrue(eval_harness.validate_cases([None]))

    @patch.object(eval_harness.urllib.request, "urlopen")
    def test_non_string_arguments_and_duplicate_response_keys_fail(self, urlopen):
        case = {"prompt": "Call ping", "must_call_tool_args": {"name": "ping", "arguments": {"x": 1}}}
        response = MagicMock()
        response.read.return_value = json.dumps({"choices": [{"message": {
            "tool_calls": [{"type": "function", "function": {"name": "ping", "arguments": {"x": 1}}}],
        }}]}).encode()
        urlopen.return_value.__enter__.return_value = response
        with self.assertRaisesRegex(ValueError, "JSON arguments string"):
            eval_harness.call_model("http://localhost:4000/v1", "fixture", case)
        response.read.return_value = b'{"choices":[],"choices":[]}'
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            eval_harness.call_model("http://localhost:4000/v1", "fixture", case)
        response.read.return_value = b'{"choices":[{"message":{"content":"ok"}}],"usage":{"total_tokens":1e400}}'
        with self.assertRaisesRegex(ValueError, "non-finite JSON number"):
            eval_harness.call_model("http://localhost:4000/v1", "fixture", case)

    @patch.object(eval_harness.urllib.request, "urlopen")
    def test_nonfinite_tool_argument_is_not_exact_json(self, urlopen):
        response = MagicMock()
        response.read.return_value = json.dumps({"choices": [{"message": {
            "tool_calls": [{"type": "function", "function": {"name": "ping", "arguments": '{"count": NaN}'}}],
        }}]}).encode()
        urlopen.return_value.__enter__.return_value = response
        case = {"prompt": "Call ping", "must_call_tool_args": {"name": "ping", "arguments": {"count": 1}}}
        with self.assertRaisesRegex(ValueError, "invalid JSON arguments"):
            eval_harness.call_model("http://localhost:4000/v1", "fixture", case)

    @patch.object(eval_harness.urllib.request, "urlopen")
    def test_exact_numbers_do_not_round_into_expected_value(self, urlopen):
        response = MagicMock()
        urlopen.return_value.__enter__.return_value = response
        for raw_number, expected in (("1e-400", 0.0), ("9007199254740993.0", 9007199254740992.0),
                                     ("1.00000000000000000000000000001", 1.0), ("1e-1000030", 0.0)):
            response.read.return_value = json.dumps({"choices": [{"message": {
                "tool_calls": [{"type": "function", "function": {"name": "ping", "arguments": '{"x":' + raw_number + '}'}}],
            }}]}).encode()
            case = {"prompt": "Call ping", "must_call_tool_args": {"name": "ping", "arguments": {"x": expected}}}
            with self.subTest(raw_number=raw_number):
                result = eval_harness.call_model("http://localhost:4000/v1", "fixture", case)
                self.assertTrue(eval_harness.check(case, result[0], result[1], result[2]["tool_details"]))

        for raw_number in ("0.00", "0e5", "0.0e-3"):
            response.read.return_value = json.dumps({"choices": [{"message": {
                "tool_calls": [{"type": "function", "function": {"name": "ping", "arguments": '{"x":' + raw_number + '}'}}],
            }}]}).encode()
            case = {"prompt": "Call ping", "must_call_tool_args": {"name": "ping", "arguments": {"x": 0.0}}}
            with self.subTest(equal_zero=raw_number):
                result = eval_harness.call_model("http://localhost:4000/v1", "fixture", case)
                self.assertEqual(eval_harness.check(case, result[0], result[1], result[2]["tool_details"]), [])

    @patch.object(eval_harness.urllib.request, "urlopen")
    def test_fractional_tool_schema_is_serialized(self, urlopen):
        response = MagicMock()
        response.read.return_value = b'{"choices":[{"message":{"content":"ok"}}]}'
        urlopen.return_value.__enter__.return_value = response
        case = json.loads('{"prompt":"Call ping","tools":[{"type":"function","function":'
                          '{"name":"ping","parameters":{"type":"object","properties":'
                          '{"x":{"type":"number","minimum":0.5}}}}}]}',
                          parse_float=eval_harness._finite_json_float)
        self.assertEqual(eval_harness.validate_cases([{**case, "id": "fractional", "must_not_call_tool": "other"}]), [])
        eval_harness.call_model("http://localhost:4000/v1", "fixture", case)
        request = urlopen.call_args.args[0]
        self.assertIn(b'"minimum":0.5', request.data)

    @patch.object(eval_harness.urllib.request, "urlopen")
    def test_malformed_tool_calls_fail_even_for_name_only_assertions(self, urlopen):
        response = MagicMock()
        urlopen.return_value.__enter__.return_value = response
        case = {"id": "malformed", "prompt": "Call ping", "must_call_tool": "ping"}
        for tool_calls in ({}, 0, [{"type": "function", "function": {"name": "ping", "arguments": "not json"}}],
                           [{"type": "function", "function": {"name": 123, "arguments": "{}"}}],
                           [{"type": "other", "function": {"name": "ping", "arguments": "{}"}}]):
            response.read.return_value = json.dumps({"choices": [{"message": {"tool_calls": tool_calls}}]}).encode()
            with self.subTest(tool_calls=tool_calls):
                result = eval_harness.run_suite("http://localhost:4000/v1", "fixture", [case])["malformed"]
                self.assertFalse(result[0])
                self.assertTrue(result[2]["infra_error"])

    def test_main_fails_for_shared_failure_and_request_error(self):
        case = {"id": "c1", "prompt": "Answer", "must_contain": ["safe"]}
        with tempfile.TemporaryDirectory() as tmp:
            cases_path = pathlib.Path(tmp) / "cases.json"
            cases_path.write_text(json.dumps([case]))
            argv = ["eval_harness.py", "--baseline", "http://baseline/v1", "--baseline-model", "base",
                    "--candidate", "http://candidate/v1", "--candidate-model", "candidate",
                    "--prompts", str(cases_path)]
            for baseline, candidate in (
                ((False, ["wrong"], self._metrics()), (False, ["wrong"], self._metrics())),
                ((True, [], self._metrics()), (False, ["request error"], self._metrics(infra=True))),
                ((False, ["request error"], self._metrics(infra=True)), (True, [], self._metrics())),
            ):
                with self.subTest(candidate=candidate[1]):
                    with patch.object(sys, "argv", argv), patch.object(eval_harness, "run_suite", side_effect=[{"c1": baseline}, {"c1": candidate}]):
                        with self.assertRaises(SystemExit) as exit_result:
                            eval_harness.main()
                    self.assertEqual(exit_result.exception.code, 1)

    @staticmethod
    def _metrics(infra=False):
        return {"latency_ms": None if infra else 1.0, "response_model": "fixture",
                "prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
                "tool_call_count": 0, "repeated_call_count": 0, "infra_error": infra}


if __name__ == "__main__":
    unittest.main()
