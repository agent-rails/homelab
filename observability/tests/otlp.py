import json
import time
import urllib.error
import urllib.request


def attrs(values):
    out = []
    for key, value in values.items():
        if isinstance(value, dict):
            out.append({"key": key, "value": value})
        elif isinstance(value, bool):
            out.append({"key": key, "value": {"boolValue": value}})
        elif isinstance(value, int):
            out.append({"key": key, "value": {"intValue": str(value)}})
        else:
            out.append({"key": key, "value": {"stringValue": str(value)}})
    return out


def logs_payload(resource, records, scope=None):
    base = time.time_ns()
    log_records = []
    for index, record in enumerate(records):
        now = str(base + index)
        record = dict(record)
        body = record.pop("_body", "")
        severity = record.pop("_severity", "")
        log_records.append(
            {
                "timeUnixNano": now,
                "observedTimeUnixNano": now,
                "body": {"stringValue": body},
                "severityText": severity,
                "attributes": attrs(record),
            }
        )
    return {
        "resourceLogs": [
            {
                "resource": {"attributes": attrs(resource)},
                "scopeLogs": [
                    {
                        "scope": scope or {"name": "com.anthropic.claude_code.events"},
                        "logRecords": log_records,
                    }
                ],
            }
        ]
    }


def sum_payload(resource, name, datapoints, temporality, start_ns=None, monotonic=True, unit="", time_ns=None, description=""):
    now = str(time_ns if time_ns is not None else time.time_ns())
    start = str(start_ns if start_ns is not None else time.time_ns())
    points = [
        {
            "attributes": attrs(point_attrs),
            "startTimeUnixNano": start,
            "timeUnixNano": now,
            "asDouble": value,
        }
        for point_attrs, value in datapoints
    ]
    return {
        "resourceMetrics": [
            {
                "resource": {"attributes": attrs(resource)},
                "scopeMetrics": [
                    {
                        "scope": {"name": "com.anthropic.claude_code"},
                        "metrics": [
                            {
                                "name": name,
                                "unit": unit,
                                "description": description,
                                "sum": {
                                    "aggregationTemporality": temporality,
                                    "isMonotonic": monotonic,
                                    "dataPoints": points,
                                },
                            }
                        ],
                    }
                ],
            }
        ]
    }


def traces_payload(resource, spans, trace_id="5b8efff798038103d269b633813fc60c", scope=None):
    now = time.time_ns()
    out = []
    for index, (name, span_attrs, event_attrs, *links) in enumerate(spans):
        out.append(
            {
                "traceId": trace_id,
                "spanId": f"{index + 1:016x}",
                "name": name,
                "kind": 1,
                "startTimeUnixNano": str(now - 1_000_000),
                "endTimeUnixNano": str(now),
                "attributes": attrs(span_attrs),
                "events": [{"timeUnixNano": str(now), "name": "secret-event", "attributes": attrs(event_attrs)}],
                "status": {"code": 2, "message": event_attrs.get("message", "")},
                "links": links[0] if links else [],
            }
        )
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": attrs(resource)},
                "scopeSpans": [{"scope": scope or {"name": "com.anthropic.claude_code"}, "spans": out}],
            }
        ]
    }


def merge(*payloads):
    return {"resourceMetrics": [rm for payload in payloads for rm in payload["resourceMetrics"]]}


DELTA = 1
CUMULATIVE = 2


def post_json(url, payload, timeout=10):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()
