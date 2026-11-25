import json
import urllib.parse

import pytest

import conftest
import events
import otlp

STACK = {
    "services": ["otel-collector", "tempo", "loki"],
    "ports": {"otel-collector": [4318, 13133, 8888, 8889], "tempo": [3200], "loki": [3100]},
    "env": {"OTELCOL_DEBUG_CONFIG": "/etc/otelcol/config.debug.yaml"},
}


LINK = {
    "traceId": "0af7651916cd43dd8448eb211c80319c",
    "spanId": "b7ad6b7169203331",
    "traceState": f"k={events.SENTINEL}",
    "attributes": otlp.attrs({"secret": events.SENTINEL}),
}


def span_batch():
    resource = {**events.RESOURCE}
    leak = {
        "user.email": f"{events.SENTINEL}@example.invalid",
        "tool_input": events.SENTINEL,
        "tool_name": "Bash",
        "custom.secret": events.SENTINEL,
        "message": events.SENTINEL,
    }
    return otlp.traces_payload(
        resource,
        [
            ("claude_code.tool", leak, {"secret": events.SENTINEL, "message": events.SENTINEL}),
            ("claude_code.user_prompt_dump", leak, {"secret": events.SENTINEL}),
            ("claude_code.hook", {"hook_event": "Stop"}, {}, [LINK]),
        ],
        scope=sentinel_scope(),
    )


def sentinel_scope():
    return {"name": events.SENTINEL, "version": events.SENTINEL, "attributes": otlp.attrs({"secret": events.SENTINEL})}


def scoped_log_batch():
    return otlp.logs_payload(
        {"service.name": "claude-code", "tenant.id": "personal"},
        [{"event.name": "api_request", "model": "claude-haiku-4-1", "duration_ms": 1, "_severity": events.SENTINEL}],
        scope=sentinel_scope(),
    )


@pytest.fixture(scope="module")
def sent(stack):
    status, body = stack.send_otlp("otel-collector", 4318, "/v1/logs", events.gates_on_batch())
    assert status == 200, body
    status, body = stack.send_otlp("otel-collector", 4318, "/v1/logs", scoped_log_batch())
    assert status == 200, body
    status, body = stack.send_otlp("otel-collector", 4318, "/v1/traces", span_batch())
    assert status == 200, body
    return stack


def loki_entries(stack):
    query = urllib.parse.urlencode({"query": '{service_name=~".+"}', "limit": "100", "since": "10m"})
    body = json.loads(stack.get(stack.url("loki", 3100, f"/loki/api/v1/query_range?{query}")))
    return body["data"]["result"]


def tempo_traces(stack):
    body = json.loads(stack.get(stack.url("tempo", 3200, "/api/search?limit=20")))
    return body.get("traces", [])


@pytest.mark.integration
def test_loki_receives_sanitized_events_only(sent):
    wanted = ("api_request", "api_error", "api_retries_exhausted", "tool_result", "tool_decision", "hook_execution_complete", "claude-haiku-4-1")
    result = conftest.poll(
        lambda: loki_entries(sent),
        lambda rows: all(name in json.dumps(rows) for name in wanted),
        90,
        interval=2.0,
    )
    text = json.dumps(result)
    assert events.SENTINEL.lower() not in text.lower()
    assert "api_request" in text and "api_error" in text
    assert "user_prompt" not in text and "assistant_response" not in text
    assert "mcp__" not in text


@pytest.mark.integration
def test_tempo_receives_sanitized_spans_only(sent):
    traces = conftest.poll(lambda: tempo_traces(sent), lambda rows: len(rows) > 0, 90, interval=2.0)
    trace_id = traces[0]["traceID"]
    raw = sent.get(sent.url("tempo", 3200, f"/api/traces/{trace_id}"))
    assert events.SENTINEL.lower() not in raw.lower()
    assert "claude_code.tool" in raw
    assert "user_prompt_dump" not in raw
    assert "claude_code.hook" not in raw
    assert "secret-event" not in raw
    assert "0af7651916cd43dd8448eb211c80319c" not in raw.lower() and "CvdlGRbNQ92ESOshHIAxnA==" not in raw


@pytest.mark.integration
def test_collector_sanitizes_before_debug_exporters(sent):
    assert events.SENTINEL.lower() not in sent.scrape("otel-collector", 8889).lower()
