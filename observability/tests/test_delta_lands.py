import concurrent.futures

import pytest

import events

STACK = {
    "services": ["prometheus", "otel-collector"],
    "ports": {"prometheus": [9090], "otel-collector": [4318, 13133, 8888, 8889]},
}
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
    send(stack, events.api_requests(2, "derived-lands"))
    assert total(stack, 'agent_api_requests_total{model="derived-lands"}', 2) == 2
    send(stack, events.api_requests(3, "derived-lands"))
    assert total(stack, 'agent_api_requests_total{model="derived-lands"}', 5) == 5


@pytest.mark.integration
def test_four_producers_lossless(stack):
    per_producer = 40

    def producer(_):
        for _ in range(per_producer):
            send(stack, events.api_requests(5, "lossless"))

    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        list(pool.map(producer, range(4)))
    expected = 4 * per_producer * 5
    assert total(stack, 'agent_api_requests_total{model="lossless"}', expected) == expected
    errors = stack.query_prom_now('sum(otelcol_deltatocumulative_datapoints_total{error!=""})')
    assert errors == [] or float(errors[0]["value"][1]) == 0


@pytest.mark.integration
def test_single_error_fires_alert_expr(stack):
    send(stack, events.api_errors(1, "single-error"))
    selector = 'agent_api_errors_total{model="single-error"}'
    expr = ALERT_NEW_SERIES.format(selector=selector)
    result = stack.query_prom(expr)
    assert len(result) == 1


@pytest.mark.integration
def test_restart_one_reset_rate_recovers(stack):
    selector = 'agent_api_requests_total{model="restart"}'
    send(stack, events.api_requests(10, "restart"))
    assert total(stack, selector, 10) == 10
    stack.compose(["restart", "-t", "1", "otel-collector"])
    stack.wait_ready()
    send(stack, events.api_requests(3, "restart"))
    assert total(stack, selector, 3) == 3
    resets = stack.query_prom(f"resets({selector}[10m])", until=lambda rows: bool(rows) and rows[0]["value"][1] == "1")
    assert resets[0]["value"][1] == "1"
    for _ in range(4):
        send(stack, events.api_requests(5, "restart"))
        import time

        time.sleep(3)
    rate = stack.query_prom(f"rate({selector}[30s])", until=lambda rows: bool(rows) and float(rows[0]["value"][1]) > 0)
    assert float(rate[0]["value"][1]) > 0
