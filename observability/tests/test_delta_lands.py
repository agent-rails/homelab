import concurrent.futures
import json
import re
import time

import pytest

import events
import otlp

STACK = {
    "services": ["prometheus", "otel-collector"],
    "ports": {"prometheus": [9090], "otel-collector": [4318, 13133, 8888, 8889]},
}
SESSION_A = "0a000000-0000-4000-8000-000000000001"
SESSION_B = "0a000000-0000-4000-8000-000000000002"
SESSION_MIXED_CUMULATIVE = "0a000000-0000-4000-8000-000000000003"
SESSION_MIXED_DELTA = "0a000000-0000-4000-8000-000000000004"
SESSION_MIXED_COUNT = "0a000000-0000-4000-8000-000000000005"
SESSION_OUT_OF_ORDER = "0a000000-0000-4000-8000-000000000006"
ALERT_NEW_SERIES = (
    "increase({selector}[15m]) > 0 or ({selector} unless {selector} offset 15m)"
)


def send(stack, payload):
    status, body = stack.send_otlp("otel-collector", 4318, "/v1/logs", payload)
    assert status == 200, body


def total(stack, selector, expected):
    result = stack.query_prom(
        f"sum({selector})",
        until=lambda rows: bool(rows) and float(rows[0]["value"][1]) == expected,
    )
    return float(result[0]["value"][1])


@pytest.mark.integration
def test_derived_counter_lands(stack):
    send(stack, events.api_requests(2, "claude-haiku-1-1"))
    assert total(stack, 'agent_api_requests_total{model="claude-haiku-1-1"}', 2) == 2
    send(stack, events.api_requests(3, "claude-haiku-1-1"))
    assert total(stack, 'agent_api_requests_total{model="claude-haiku-1-1"}', 5) == 5


@pytest.mark.integration
def test_four_producers_lossless(stack):
    per_producer = 40

    def producer(_):
        for _ in range(per_producer):
            send(stack, events.api_requests(5, "claude-haiku-1-2"))

    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        list(pool.map(producer, range(4)))
    expected = 4 * per_producer * 5
    assert total(stack, 'agent_api_requests_total{model="claude-haiku-1-2"}', expected) == expected
    converted = stack.query_prom(
        'sum(otelcol_deltatocumulative_datapoints{error=""})',
        until=lambda rows: bool(rows) and float(rows[0]["value"][1]) > 0,
    )
    assert float(converted[0]["value"][1]) > 0
    errors = stack.query_prom_now('sum(otelcol_deltatocumulative_datapoints{error!=""})')
    assert errors == [] or float(errors[0]["value"][1]) == 0


@pytest.mark.integration
def test_single_error_fires_alert_expr(stack):
    send(stack, events.api_errors(1, "claude-haiku-1-3"))
    selector = 'agent_api_errors_total{model="claude-haiku-1-3"}'
    expr = ALERT_NEW_SERIES.format(selector=selector)
    result = stack.query_prom(expr)
    assert len(result) == 1


@pytest.mark.integration
def test_restart_one_reset_rate_recovers(stack):
    selector = 'agent_api_requests_total{model="claude-haiku-1-4"}'
    send(stack, events.api_requests(10, "claude-haiku-1-4"))
    assert total(stack, selector, 10) == 10
    stack.compose(["restart", "-t", "1", "otel-collector"])
    stack.wait_ready()
    send(stack, events.api_requests(3, "claude-haiku-1-4"))
    assert total(stack, selector, 3) == 3
    resets = stack.query_prom(f"resets({selector}[10m])", until=lambda rows: bool(rows) and rows[0]["value"][1] == "1")
    assert resets[0]["value"][1] == "1"
    for _ in range(4):
        send(stack, events.api_requests(5, "claude-haiku-1-4"))
        import time

        time.sleep(3)
    rate = stack.query_prom(f"rate({selector}[30s])", until=lambda rows: bool(rows) and float(rows[0]["value"][1]) > 0)
    assert float(rate[0]["value"][1]) > 0


def send_metrics(stack, payload):
    status, body = stack.send_otlp("otel-collector", 4318, "/v1/metrics", payload)
    assert status == 200, body


@pytest.mark.integration
def test_two_sessions_exact_counters(stack):
    start = time.time_ns()
    totals = {SESSION_A: 0, SESSION_B: 0}
    for step in range(1, 6):
        for session, factor in ((SESSION_A, 7), (SESSION_B, 11)):
            totals[session] = step * factor
            send_metrics(stack, events.session_tokens(session, float(totals[session]), start, attributes={"type": "input", "model": "claude-haiku-1-5"}))
        time.sleep(1)
    for session, expected in totals.items():
        selector = f'claude_code_token_usage_tokens_total{{session_id="{session}",model="claude-haiku-1-5"}}'
        assert total(stack, selector, expected) == expected
    assert total(stack, 'claude_code_token_usage_tokens_total{model="claude-haiku-1-5"}', sum(totals.values())) == sum(totals.values())
    resets = stack.query_prom_now('sum(resets(claude_code_token_usage_tokens_total{model="claude-haiku-1-5"}[10m]))')
    assert resets == [] or float(resets[0]["value"][1]) == 0
    sessions = stack.query_prom_now('count(max_over_time(claude_code_token_usage_tokens_total{model="claude-haiku-1-5"}[10m]))')
    assert float(sessions[0]["value"][1]) == 2


@pytest.mark.integration
def test_mixed_delta_cumulative_batch_survives(stack):
    start = time.time_ns()
    cumulative = events.session_tokens(SESSION_MIXED_CUMULATIVE, 100.0, start, attributes={"type": "input", "model": "claude-haiku-1-6"})
    delta = events.session_tokens(SESSION_MIXED_DELTA, 40.0, start, temporality=otlp.DELTA, attributes={"type": "output", "model": "claude-haiku-1-6"})
    other = events.session_tokens(
        SESSION_MIXED_COUNT, 5.0, start, name="claude_code.session.count", unit="", attributes={"start_type": "fresh"}
    )
    send_metrics(stack, otlp.merge(cumulative, delta, other))
    assert total(stack, f'claude_code_token_usage_tokens_total{{model="claude-haiku-1-6",session_id="{SESSION_MIXED_CUMULATIVE}"}}', 100) == 100
    assert total(stack, f'claude_code_token_usage_tokens_total{{model="claude-haiku-1-6",session_id="{SESSION_MIXED_DELTA}"}}', 40) == 40
    assert total(stack, f'claude_code_session_count_total{{session_id="{SESSION_MIXED_COUNT}"}}', 5) == 5
    send_metrics(stack, events.session_tokens(SESSION_MIXED_DELTA, 2.0, start, temporality=otlp.DELTA, attributes={"type": "output", "model": "claude-haiku-1-6"}))
    assert total(stack, f'claude_code_token_usage_tokens_total{{model="claude-haiku-1-6",session_id="{SESSION_MIXED_DELTA}"}}', 42) == 42


@pytest.mark.integration
def test_passthrough_metric_names_match_dashboard(stack):
    path = conftest_dashboard()
    names = set(re.findall(r"claude_code_[A-Za-z_]+", json.dumps(json.load(open(path)))))
    assert names
    start = time.time_ns()
    for name, unit in events.PASSTHROUGH_METRICS.items():
        send_metrics(stack, events.session_tokens("dash", 1.0, start, name=name, unit=unit, attributes={}))
    for metric in sorted(names):
        assert stack.query_prom(metric), metric


def conftest_dashboard():
    import conftest

    return conftest.OBSERVABILITY_DIR / "grafana" / "dashboards" / "claude-code-usage.json"


@pytest.mark.integration
def test_older_start_delta_counts_as_conversion_error(stack):
    newer = time.time_ns()
    older = newer - 10_000_000_000
    attributes = {"type": "input", "model": "claude-haiku-1-7"}
    first = events.session_tokens(SESSION_OUT_OF_ORDER, 1.0, newer, temporality=otlp.DELTA, attributes=attributes)
    second = events.session_tokens(SESSION_OUT_OF_ORDER, 1.0, older, temporality=otlp.DELTA, attributes=attributes, time_ns=older + 1_000_000_000)
    send_metrics(stack, first)
    assert total(stack, f'claude_code_token_usage_tokens_total{{session_id="{SESSION_OUT_OF_ORDER}"}}', 1) == 1
    send_metrics(stack, second)
    errors = stack.query_prom(
        'sum(otelcol_deltatocumulative_datapoints{error!=""})',
        until=lambda rows: bool(rows) and float(rows[0]["value"][1]) > 0,
    )
    assert float(errors[0]["value"][1]) > 0
