import json
import time
import urllib.error
import urllib.request


def attrs(values):
    out = []
    for key, value in values.items():
        if isinstance(value, bool):
            out.append({"key": key, "value": {"boolValue": value}})
        elif isinstance(value, int):
            out.append({"key": key, "value": {"intValue": str(value)}})
        else:
            out.append({"key": key, "value": {"stringValue": str(value)}})
    return out


def logs_payload(resource, records):
    now = str(time.time_ns())
    log_records = []
    for record in records:
        record = dict(record)
        body = record.pop("_body", "")
        log_records.append(
            {
                "timeUnixNano": now,
                "observedTimeUnixNano": now,
                "body": {"stringValue": body},
                "attributes": attrs(record),
            }
        )
    return {
        "resourceLogs": [
            {
                "resource": {"attributes": attrs(resource)},
                "scopeLogs": [
                    {
                        "scope": {"name": "com.anthropic.claude_code.events"},
                        "logRecords": log_records,
                    }
                ],
            }
        ]
    }


def sum_payload(resource, name, datapoints, temporality, start_ns=None, monotonic=True, unit=""):
    now = str(time.time_ns())
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
