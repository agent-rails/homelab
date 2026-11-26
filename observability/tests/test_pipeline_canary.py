import json
import re

import pytest

import events

STACK = {
    "services": ["prometheus", "otel-collector"],
    "ports": {"prometheus": [9090], "otel-collector": [4318, 13133, 8888, 8889]},
}

DERIVED = {
    "agent_api_requests_total": {"model", "repo", "tenant"},
    "agent_api_errors_total": {"model", "error_class", "status_code", "repo", "tenant"},
    "agent_api_retries_exhausted_total": {"model", "error_class", "repo", "tenant"},
    "agent_api_duration_milliseconds_count": {"model"},
    "agent_api_duration_milliseconds_sum": {"model"},
    "agent_api_duration_milliseconds_bucket": {"model", "le"},
    "agent_tool_results_total": {"tool_name", "success", "error_type", "repo", "tenant"},
    "agent_tool_duration_milliseconds_count": {"tool_name", "success"},
    "agent_tool_duration_milliseconds_sum": {"tool_name", "success"},
    "agent_tool_duration_milliseconds_bucket": {"tool_name", "success", "le"},
    "agent_tool_decisions_total": {"decision", "source", "classifier_verdict", "tool_name", "repo", "tenant"},
    "agent_hook_executions_total": {"hook_event", "outcome", "repo", "tenant"},
}
PASSTHROUGH_NAMES = [
    "claude_code_session_count_total",
    "claude_code_lines_of_code_count_total",
    "claude_code_pull_request_count_total",
    "claude_code_commit_count_total",
    "claude_code_cost_usage_USD_total",
    "claude_code_token_usage_tokens_total",
    "claude_code_code_edit_tool_decision_total",
    "claude_code_active_time_seconds_total",
]
PASSTHROUGH_LABELS = {
    "session_id", "terminal_type", "type", "model", "tool_name", "decision", "source",
    "language", "start_type", "query_source", "speed", "effort", "tenant", "repo",
}
SCRAPE_LABELS = {"__name__", "job", "instance", "exported_job"}


@pytest.fixture(scope="module")
def gates_on(stack):
    status, body = stack.send_otlp("otel-collector", 4318, "/v1/logs", events.gates_on_batch())
    assert status == 200, body
    stack.query_prom('agent_hook_executions_total{outcome="blocking"}')
    stack.query_prom('agent_tool_results_total{tool_name="mcp_tool"}')
    return stack


@pytest.fixture(scope="module")
def forged(stack, gates_on):
    status, body = stack.send_otlp("otel-collector", 4318, "/v1/logs", events.forged_batch())
    assert status == 200, body
    stack.query_prom('agent_api_errors_total{error_class="other"}')
    stack.query_prom('agent_tool_decisions_total{tool_name="Read"}')
    stack.query_prom('agent_hook_executions_total{hook_event="other"}')
    return stack


def labels_by_metric(stack):
    by_name = {}
    for labels in stack.series('{__name__=~"agent_.*"}'):
        by_name.setdefault(labels["__name__"], []).append(set(labels) - SCRAPE_LABELS)
    return by_name


@pytest.mark.integration
def test_exact_label_set(forged):
    by_name = labels_by_metric(forged)
    assert set(by_name) == set(DERIVED)
    for name, expected in DERIVED.items():
        for label_set in by_name[name]:
            assert label_set == expected, name


@pytest.mark.integration
def test_sentinel_string_absent(forged):
    everything = json.dumps(forged.series('{__name__=~".+"}'))
    label_names = json.loads(forged.get(forged.url("prometheus", 9090, "/api/v1/labels")))["data"]
    values = []
    for label in label_names:
        values += json.loads(
            forged.get(forged.url("prometheus", 9090, f"/api/v1/label/{label}/values"))
        )["data"]
    corpus = [everything, json.dumps(values), forged.scrape("otel-collector", 8889), forged.scrape("otel-collector", 8888)]
    leaked = [i for i, text in enumerate(corpus) if events.SENTINEL.lower() in text.lower()]
    assert leaked == []


@pytest.mark.integration
def test_unknown_keys_dropped(forged):
    forbidden = re.compile(r"user|session|email|account|organization|workspace|host|custom|prompt|vcs|tool_use|request_id")
    names = set()
    for labels in forged.series('{__name__=~".+"}'):
        if labels["__name__"].startswith(("agent_", "target_info")):
            names |= set(labels)
    assert [name for name in names if forbidden.search(name)] == []
    info = [s for s in forged.series("target_info") if s.get("job") != "otel-collector"]
    assert info == []


@pytest.mark.integration
def test_forged_values_become_other(forged):
    def values(metric, label):
        return {series[label] for series in forged.series(metric)}

    assert values("agent_api_errors_total", "status_code") >= {"other"}
    assert "other" in values("agent_api_errors_total", "repo")
    assert "other" in values("agent_api_errors_total", "model")
    assert "other" in values("agent_tool_results_total", "error_type")
    assert "other" in values("agent_tool_results_total", "success")
    assert "other" in values("agent_tool_results_total", "tool_name")
    assert "other" in values("agent_tool_decisions_total", "decision")
    assert "other" in values("agent_tool_decisions_total", "source")
    assert "other" in values("agent_tool_decisions_total", "classifier_verdict")
    assert "other" in values("agent_hook_executions_total", "hook_event")
    assert "other" in values("agent_tool_decisions_total", "tenant")
    assert "other" in values("agent_tool_results_total", "repo")
    assert values("agent_tool_decisions_total", "repo") == {"agent-bailiff"}
    assert values("agent_tool_results_total", "tool_name") >= {"mcp_tool", "Bash"}


@pytest.mark.integration
def test_tenant_on_every_record(stack):
    status, body = stack.send_otlp(
        "otel-collector", 4318, "/v1/logs", events.api_requests(3, "claude-haiku-2-1")
    )
    assert status == 200, body
    result = stack.query_prom(
        'sum(agent_api_requests_total{model="claude-haiku-2-1",tenant="personal"})',
        until=lambda rows: rows and rows[0]["value"][1] == "3",
    )
    assert result[0]["value"][1] == "3"
    assert stack.query_prom_now('agent_api_requests_total{model="claude-haiku-2-1",tenant!="personal"}') == []


@pytest.mark.integration
def test_error_text_never_stored(gates_on):
    classes = {series["error_class"] for series in gates_on.series("agent_api_errors_total")}
    assert classes <= {"http_529", "connection", "timeout", "other"} | {"http_500"}
    assert "connection" in classes
    assert "error" not in {name for series in gates_on.series('{__name__=~"agent_.*"}') for name in series}
    assert events.SENTINEL.lower() not in gates_on.scrape("otel-collector", 8889).lower()


@pytest.fixture(scope="module")
def passthrough_forged(stack):
    status, body = stack.send_otlp("otel-collector", 4318, "/v1/metrics", events.passthrough_forged_batch())
    assert status == 200, body
    for name in PASSTHROUGH_NAMES:
        stack.query_prom(name)
    return stack


@pytest.mark.integration
def test_passthrough_exact_label_set(passthrough_forged):
    stack = passthrough_forged
    found = {}
    for labels in stack.series('{__name__=~"claude_code_.*"}'):
        found.setdefault(labels["__name__"], []).append(set(labels) - SCRAPE_LABELS - {"__name__"})
    assert sorted(found) == sorted(PASSTHROUGH_NAMES)
    for name, label_sets in found.items():
        for label_set in label_sets:
            assert label_set <= PASSTHROUGH_LABELS, (name, label_set - PASSTHROUGH_LABELS)
    assert stack.series('{__name__=~"custom.*"}') == []
    assert [s for s in stack.series("target_info") if s.get("job") not in ("otel-collector", "prometheus")] == []


@pytest.mark.integration
def test_passthrough_forged_values_become_other_and_sentinel_absent(passthrough_forged):
    stack = passthrough_forged
    series = stack.series('{__name__=~"claude_code_.*"}')
    for label in ("terminal_type", "type", "tool_name", "decision", "source", "language", "start_type", "query_source", "speed", "effort", "tenant", "repo", "model"):
        values = {s[label] for s in series if label in s}
        assert values == {"other"}, (label, values)
    assert {s["session_id"] for s in series} == {"other"}
    label_names = json.loads(stack.get(stack.url("prometheus", 9090, "/api/v1/labels")))["data"]
    corpus = [json.dumps(stack.series('{__name__=~".+"}')), stack.scrape("otel-collector", 8889), stack.scrape("otel-collector", 8888)]
    for label in label_names:
        corpus.append(stack.get(stack.url("prometheus", 9090, f"/api/v1/label/{label}/values")))
    assert [i for i, text in enumerate(corpus) if events.SENTINEL.lower() in text.lower()] == []
