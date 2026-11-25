import base64
import json

import pytest

import events

STACK = {
    "services": ["prometheus", "otel-collector"],
    "ports": {"prometheus": [9090], "otel-collector": [4318, 13133, 8888, 8889]},
}
ENCODED_SENTINEL = base64.b64encode(events.SENTINEL.encode()).decode()


def send(stack, path, payload):
    status, body = stack.send_otlp("otel-collector", 4318, path, payload)
    assert status == 200, body


def total(stack, selector, expected):
    rows = stack.query_prom(
        f"sum({selector})",
        until=lambda result: bool(result) and float(result[0]["value"][1]) == expected,
    )
    return float(rows[0]["value"][1])


def leaked(stack):
    corpus = [
        json.dumps(stack.series('{__name__=~".+"}')),
        stack.scrape("otel-collector", 8889),
        stack.scrape("otel-collector", 8888),
    ]
    return [i for i, text in enumerate(corpus) if events.SENTINEL.lower() in text.lower() or ENCODED_SENTINEL in text]


@pytest.mark.integration
def test_non_string_log_values_become_other(stack):
    send(stack, "/v1/logs", events.typed_value_logs())
    count = len(events.TYPED_VALUES)
    assert total(stack, 'agent_api_requests_total{model="other"}', count) == count
    assert total(stack, 'agent_tool_results_total{tool_name="other"}', count) == count
    assert leaked(stack) == []


@pytest.mark.integration
def test_non_string_datapoint_values_become_other(stack):
    send(stack, "/v1/metrics", events.typed_value_metrics())
    series = stack.query_prom("claude_code_token_usage_tokens_total")
    for row in series:
        for label in ("session_id", "model", "tool_name", "terminal_type", "language"):
            assert row["metric"][label] == "other", (label, row["metric"])
    assert leaked(stack) == []


@pytest.mark.integration
def test_forged_metric_name_unit_and_description_dropped(stack):
    send(stack, "/v1/metrics", events.forged_metric_metadata())
    assert total(stack, 'claude_code_token_usage_tokens_total{model="claude-haiku-3-3"}', 1) == 1
    names = json.loads(stack.get(stack.url("prometheus", 9090, "/api/v1/label/__name__/values")))["data"]
    assert [n for n in names if "sessionx" in n.lower() or "evilunit" in n.lower()] == []
    assert "evilunit" not in stack.scrape("otel-collector", 8889).lower()
    assert leaked(stack) == []


@pytest.mark.integration
def test_hook_with_missing_counts_still_counted(stack):
    send(stack, "/v1/logs", events.hook_without_counts("Stop", 2))
    assert total(stack, 'agent_hook_executions_total{hook_event="Stop",outcome="success"}', 2) == 2
    outcomes = stack.query_prom('agent_hook_executions_total{hook_event="Stop"}', until=lambda rows: len(rows) == 4)
    assert sorted((r["metric"]["outcome"], r["value"][1]) for r in outcomes) == [
        ("blocking", "0"),
        ("cancelled", "0"),
        ("non_blocking_error", "0"),
        ("success", "2"),
    ]


@pytest.mark.integration
def test_invalid_duration_counts_request_but_not_histogram(stack):
    send(stack, "/v1/logs", events.api_request_bad_duration("claude-haiku-3-1"))
    send(stack, "/v1/logs", events.api_requests(1, "claude-haiku-3-2"))
    assert total(stack, 'agent_api_requests_total{model="claude-haiku-3-1"}', 1) == 1
    assert total(stack, 'agent_api_duration_milliseconds_count{model="claude-haiku-3-2"}', 1) == 1
    assert stack.query_prom_now('agent_api_duration_milliseconds_count{model="claude-haiku-3-1"}') == []


@pytest.mark.integration
def test_empty_typed_values_fall_back_to_defaults(stack):
    send(stack, "/v1/logs", events.empty_value_logs())
    assert total(stack, 'agent_api_requests_total{model="unknown"}', 1) == 1
    assert total(stack, 'agent_tool_results_total{tool_name="unknown",success="unknown",error_type="none"}', 1) == 1
    assert total(stack, 'agent_hook_executions_total{hook_event="unknown",outcome="success"}', 1) == 1
    assert stack.query_prom_now('agent_api_duration_milliseconds_count{model="unknown"}') == []
    assert '=""' not in stack.scrape("otel-collector", 8889)


@pytest.mark.integration
def test_empty_typed_datapoint_labels_dropped(stack):
    send(stack, "/v1/metrics", events.empty_value_metrics())
    rows = stack.query_prom("claude_code_commit_count_total", until=lambda result: bool(result))
    assert [r["value"][1] for r in rows] == ["3"]
    assert not {"session_id", "model", "terminal_type", "type", "speed"} & set(rows[0]["metric"])
    assert '=""' not in stack.scrape("otel-collector", 8889)


@pytest.mark.integration
def test_allowlisted_names_with_wrong_type_dropped(stack):
    send(stack, "/v1/metrics", events.forged_metric_types())
    send(stack, "/v1/metrics", events.forged_metric_metadata())
    assert total(stack, 'claude_code_token_usage_tokens_total{model="claude-haiku-3-3"}', 1) == 1
    names = json.loads(stack.get(stack.url("prometheus", 9090, "/api/v1/label/__name__/values")))["data"]
    assert "claude_code_token_usage_tokens_total" in names
    assert [n for n in names if n.startswith("claude_code_") and not n.endswith("_total")] == []
