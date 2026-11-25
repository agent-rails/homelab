import base64
import time

import otlp

SENTINEL = "SENTINEL-7f3a9c41"
RESOURCE = {
    "service.name": "claude-code",
    "tenant.id": "personal",
    "user.email": f"{SENTINEL}@example.invalid",
    "host.name": SENTINEL,
    "custom.secret": SENTINEL,
}
STANDARD = {
    "user.email": f"{SENTINEL}@example.invalid",
    "user.id": SENTINEL,
    "user.account_uuid": SENTINEL,
    "user.account_id": SENTINEL,
    "organization.id": SENTINEL,
    "session.id": SENTINEL,
    "workspace.host_paths": f"/Users/{SENTINEL}",
    "vcs.repository.url.full": f"https://github.com/{SENTINEL}/private",
    "vcs.repository.name": "agent-bailiff",
    "prompt.id": SENTINEL,
    "tool_use_id": SENTINEL,
    "request_id": SENTINEL,
    "custom.secret": SENTINEL,
}
CONTENT = {
    "tool_parameters": f'{{"bash_command":"cat {SENTINEL}"}}',
    "tool_input": f'{{"command":"{SENTINEL}"}}',
    "error": f"Overloaded {SENTINEL}",
    "prompt": SENTINEL,
    "hook_name": f"PreToolUse:{SENTINEL}",
    "hook_definitions": SENTINEL,
    "_body": SENTINEL,
}


def record(event, **fields):
    return {**STANDARD, **CONTENT, "event.name": event, **fields}


def gates_on_batch():
    return otlp.logs_payload(
        RESOURCE,
        [
            record("api_request", model="claude-opus-5-5", duration_ms=1234),
            record("api_error", model="claude-opus-5-5", status_code=529, duration_ms=10),
            record("api_error", model="claude-opus-5-5", error=f"connect ECONNREFUSED {SENTINEL}"),
            record("api_retries_exhausted", model="claude-opus-5-5", status_code=529, total_attempts=10),
            record("tool_result", tool_name="Bash", success="false", duration_ms=50, error_type="Error:ENOENT"),
            record("tool_result", tool_name="mcp__server__tool", success="true", duration_ms=7),
            record("tool_decision", decision="reject", source="hook", tool_name="Bash", classifier_verdict="unavailable"),
            record(
                "hook_execution_complete",
                hook_event="PreToolUse",
                num_hooks=3,
                num_success=1,
                num_blocking=1,
                num_non_blocking_error=1,
                num_cancelled=0,
            ),
            record("user_prompt"),
            record("assistant_response"),
            record("tool_result_content"),
            {**STANDARD, **CONTENT},
        ],
    )


def forged_batch():
    resource = {**RESOURCE, "tenant.id": f"Evil Tenant {SENTINEL}"}
    forged = f"forged-{SENTINEL}"
    return otlp.logs_payload(
        resource,
        [
            record("api_error", model=f"x y {SENTINEL}", status_code=forged, duration_ms=forged, **{"vcs.repository.name": forged}),
            record("tool_result", tool_name=f"has space {SENTINEL}", success=forged, error_type=f"Error:/Users/{SENTINEL}/.ssh/id_rsa", duration_ms=5, **{"vcs.repository.name": forged}),
            record("tool_decision", decision=forged, source=forged, tool_name="Read", classifier_verdict=forged, tenant=forged, repo=forged),
            record("hook_execution_complete", hook_event=forged, num_blocking=forged, num_non_blocking_error=forged, num_success=forged, num_cancelled=forged),
        ],
    )


def api_requests(count, model, tenant="personal", repo="agent-bailiff"):
    return otlp.logs_payload(
        {"service.name": "claude-code", "tenant.id": tenant},
        [
            {"event.name": "api_request", "model": model, "duration_ms": 100, "vcs.repository.name": repo}
            for _ in range(count)
        ],
    )


def api_errors(count, model):
    return otlp.logs_payload(
        {"service.name": "claude-code", "tenant.id": "personal"},
        [
            {"event.name": "api_error", "model": model, "status_code": 500, "vcs.repository.name": "agent-bailiff"}
            for _ in range(count)
        ],
    )


METRIC_LEAK = {
    "user.email": f"{SENTINEL}@example.invalid",
    "user.id": SENTINEL,
    "user.account_uuid": SENTINEL,
    "user.account_id": SENTINEL,
    "organization.id": SENTINEL,
    "workspace.host_paths": f"/Users/{SENTINEL}",
    "vcs.repository.url.full": f"https://github.com/{SENTINEL}/private",
    "custom.secret": SENTINEL,
    "agent.name": SENTINEL,
    "skill.name": SENTINEL,
}
PASSTHROUGH_METRICS = {
    "claude_code.session.count": "",
    "claude_code.lines_of_code.count": "",
    "claude_code.pull_request.count": "",
    "claude_code.commit.count": "",
    "claude_code.cost.usage": "USD",
    "claude_code.token.usage": "tokens",
    "claude_code.code_edit_tool.decision": "",
    "claude_code.active_time.total": "s",
}


def passthrough_forged_batch():
    resource = {**RESOURCE, "tenant.id": f"Evil Tenant {SENTINEL}"}
    forged = f"forged!{SENTINEL}"
    point = {
        **METRIC_LEAK,
        "session.id": "forged!session" + SENTINEL,
        "terminal.type": forged,
        "type": forged,
        "model": f"x y {SENTINEL}",
        "tool_name": forged,
        "decision": forged,
        "source": forged,
        "language": forged,
        "start_type": forged,
        "query_source": forged,
        "speed": forged,
        "effort": forged,
        "tenant": forged,
        "repo": forged,
        "vcs.repository.name": forged,
    }
    batches = [
        otlp.sum_payload(resource, name, [(point, 1.0)], otlp.CUMULATIVE, unit=unit)
        for name, unit in PASSTHROUGH_METRICS.items()
    ]
    batches.append(otlp.sum_payload(resource, "custom.metric.secret", [(point, 1.0)], otlp.CUMULATIVE))
    return otlp.merge(*batches)


def session_tokens(session_id, value, start_ns, temporality=otlp.CUMULATIVE, name="claude_code.token.usage", unit="tokens", attributes=None, time_ns=None):
    attributes = attributes or {"type": "input", "model": "claude-opus-5-5"}
    return otlp.sum_payload(
        {"service.name": "claude-code", "tenant.id": "personal"},
        name,
        [({"session.id": session_id, **attributes}, value)],
        temporality,
        start_ns=start_ns,
        unit=unit,
        time_ns=time_ns,
    )


TYPED_VALUES = {
    "bytes": {"bytesValue": base64.b64encode(SENTINEL.encode()).decode()},
    "array": {"arrayValue": {"values": [{"stringValue": SENTINEL}]}},
    "kvlist": {"kvlistValue": {"values": [{"key": SENTINEL, "value": {"stringValue": SENTINEL}}]}},
    "double": {"doubleValue": 7.25},
}


def typed_value_logs():
    return otlp.logs_payload(
        {"service.name": "claude-code", "tenant.id": "personal"},
        [
            {"event.name": "api_request", "model": value, "duration_ms": 100, "vcs.repository.name": "agent-bailiff"}
            for value in TYPED_VALUES.values()
        ]
        + [
            {"event.name": "tool_result", "tool_name": value, "success": "true", "duration_ms": 5, "vcs.repository.name": "agent-bailiff"}
            for value in TYPED_VALUES.values()
        ],
    )


def typed_value_metrics():
    points = [
        ({"session.id": value, "model": value, "tool_name": value, "terminal.type": value, "language": value, "type": "input"}, 1.0)
        for value in TYPED_VALUES.values()
    ]
    return otlp.sum_payload(
        {"service.name": "claude-code", "tenant.id": "personal"},
        "claude_code.token.usage",
        points,
        otlp.CUMULATIVE,
        unit="tokens",
    )


def forged_metric_metadata():
    point = {"session.id": "0b000000-0000-4000-8000-000000000001", "type": "input", "model": "claude-haiku-3-3"}
    resource = {"service.name": "claude-code", "tenant.id": "personal"}
    return otlp.merge(
        otlp.sum_payload(resource, "claude_code.sessionXcount", [(point, 1.0)], otlp.CUMULATIVE),
        otlp.sum_payload(resource, "claude_code.token.usage", [(point, 1.0)], otlp.CUMULATIVE, unit="evilunit", description=SENTINEL),
    )


def hook_without_counts(hook_event, num_success):
    return otlp.logs_payload(
        {"service.name": "claude-code", "tenant.id": "personal"},
        [{"event.name": "hook_execution_complete", "hook_event": hook_event, "num_success": num_success, "vcs.repository.name": "agent-bailiff"}],
    )


def api_request_bad_duration(model):
    return otlp.logs_payload(
        {"service.name": "claude-code", "tenant.id": "personal"},
        [{"event.name": "api_request", "model": model, "duration_ms": "not-a-number", "vcs.repository.name": "agent-bailiff"}],
    )


def empty_value_logs():
    return otlp.logs_payload(
        {"service.name": "claude-code", "tenant.id": "personal"},
        [
            {"event.name": "api_request", "model": {}, "duration_ms": {}, "vcs.repository.name": "agent-bailiff"},
            {"event.name": "tool_result", "tool_name": {}, "success": {}, "error_type": {}, "vcs.repository.name": "agent-bailiff"},
            {"event.name": "hook_execution_complete", "hook_event": {}, "num_success": 1, "vcs.repository.name": "agent-bailiff"},
        ],
    )


def empty_value_metrics():
    point = {"session.id": {}, "model": {}, "terminal.type": {}, "type": {}, "speed": {}}
    return otlp.sum_payload(
        {"service.name": "claude-code", "tenant.id": "personal"},
        "claude_code.commit.count",
        [(point, 3.0)],
        otlp.CUMULATIVE,
    )


def forged_metric_types():
    point = {"session.id": "0c000000-0000-4000-8000-000000000001", "model": "claude-haiku-5-1"}
    now = str(time.time_ns())
    attributes = otlp.attrs(point)
    metrics = [
        {"name": "claude_code.token.usage", "gauge": {"dataPoints": [{"timeUnixNano": now, "asDouble": 1.0, "attributes": attributes}]}},
        {
            "name": "claude_code.cost.usage",
            "histogram": {
                "aggregationTemporality": otlp.CUMULATIVE,
                "dataPoints": [{"timeUnixNano": now, "startTimeUnixNano": now, "count": "1", "sum": 1.0, "bucketCounts": ["1"], "explicitBounds": [], "attributes": attributes}],
            },
        },
        {
            "name": "claude_code.commit.count",
            "sum": {
                "aggregationTemporality": otlp.CUMULATIVE,
                "isMonotonic": False,
                "dataPoints": [{"timeUnixNano": now, "startTimeUnixNano": now, "asDouble": 1.0, "attributes": attributes}],
            },
        },
    ]
    resource = {"attributes": otlp.attrs({"service.name": "claude-code", "tenant.id": "personal"})}
    return {"resourceMetrics": [{"resource": resource, "scopeMetrics": [{"scope": {"name": "x"}, "metrics": metrics}]}]}


NAN = {"doubleValue": "NaN"}
INF = {"doubleValue": "Infinity"}
NEG_INF = {"doubleValue": "-Infinity"}


def non_finite_logs():
    repo = {"vcs.repository.name": "provider-router"}
    claude = {"service.name": "claude-code", "tenant.id": "personal"}
    main = otlp.logs_payload(
        claude,
        [
            {"event.name": "api_request", "model": "claude-sonnet-5-4", "duration_ms": 10, **repo},
            {"event.name": "api_request", "model": "claude-sonnet-5-4", "duration_ms": NAN, **repo},
            {"event.name": "api_request", "model": "claude-opus-4-1-20250805", **repo},
            {"event.name": "api_error", "model": "claude-sonnet-5-4", "status_code": NAN, "error": INF, **repo},
            {"event.name": "tool_result", "tool_name": "Bash", "success": NAN, "error_type": INF, "duration_ms": NEG_INF, **repo},
            {"event.name": "tool_decision", "tool_name": "Bash", "decision": NAN, "source": INF, "classifier_verdict": NEG_INF, **repo},
            {"event.name": "hook_execution_complete", "hook_event": NAN, "num_success": 1, "num_blocking": INF, "num_non_blocking_error": NAN, "num_cancelled": NEG_INF, **repo},
            {"event.name": "api_request", "model": "claude-sonnet-5-4", "vcs.repository.name": NAN},
            {"event.name": NAN, "model": "claude-sonnet-5-4", **repo},
        ],
    )
    forged_tenant = otlp.logs_payload(
        {"service.name": "evil-svc-1234", "tenant.id": NAN},
        [{"event.name": "api_request", "model": "claude-sonnet-5-4", "vcs.repository.name": "memkit"}],
    )
    forged_service = otlp.logs_payload(
        {"service.name": INF, "tenant.id": "personal"},
        [{"event.name": "api_request", "model": "claude-sonnet-5-4", "vcs.repository.name": "model-router"}],
    )
    return {"resourceLogs": main["resourceLogs"] + forged_tenant["resourceLogs"] + forged_service["resourceLogs"]}


def non_finite_metrics():
    resource = {"service.name": "claude-code", "tenant.id": NAN}
    session = "0e000000-0000-4000-8000-0000000000%02d"
    return otlp.sum_payload(
        resource,
        "claude_code.cost.usage",
        [
            ({"session.id": session % 1, "model": "claude-sonnet-5-4", "type": NAN, "speed": INF}, 2.0),
            ({"session.id": session % 2, "model": "claude-sonnet-5-4"}, "NaN"),
            ({"session.id": session % 3, "model": "claude-sonnet-5-4"}, "Infinity"),
            ({"session.id": session % 4, "model": "claude-sonnet-5-4"}, "-Infinity"),
        ],
        otlp.CUMULATIVE,
    )
