import importlib.util
import json
import pathlib
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
        self.assertEqual(metrics["prompt_tokens"], 0)
        self.assertEqual(metrics["completion_tokens"], 0)
        self.assertEqual(metrics["total_tokens"], 0)

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


if __name__ == "__main__":
    unittest.main()
