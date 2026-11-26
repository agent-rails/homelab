import time

import pytest
import yaml

import conftest
import events

STACK = {
    "services": ["prometheus", "otel-collector"],
    "ports": {"prometheus": [9090], "otel-collector": [4318, 13133, 8888, 8889]},
    "prometheus_tmpfs_size": "24m",
}


def send(stack, payload):
    status, body = stack.send_otlp("otel-collector", 4318, "/v1/logs", payload)
    assert status == 200, body


def total(stack, selector, expected):
    rows = stack.query_prom(
        f"sum({selector})",
        until=lambda rows: bool(rows) and float(rows[0]["value"][1]) == expected,
        timeout=60,
    )
    return float(rows[0]["value"][1])


@pytest.mark.config
def test_prometheus_retention_is_size_bounded_and_self_scraped():
    compose = yaml.safe_load(conftest.REAL_COMPOSE.read_text())
    command = compose["services"]["prometheus"]["command"]
    sizes = [c for c in command if c.startswith("--storage.tsdb.retention.size=")]
    assert len(sizes) == 1 and sizes[0].split("=")[1].endswith("GB")
    jobs = {j["job_name"]: j for j in yaml.safe_load(conftest.REAL_PROMETHEUS.read_text())["scrape_configs"]}
    assert jobs["prometheus"]["static_configs"][0]["targets"] == ["localhost:9090"]


@pytest.mark.integration
def test_prometheus_paused_no_loss(stack):
    send(stack, events.api_requests(4, "claude-haiku-2-2"))
    assert total(stack, 'agent_api_requests_total{model="claude-haiku-2-2"}', 4) == 4
    stack.compose(["pause", "prometheus"])
    try:
        for _ in range(3):
            send(stack, events.api_requests(5, "claude-haiku-2-2"))
        time.sleep(12)
    finally:
        stack.compose(["unpause", "prometheus"])
    assert total(stack, 'agent_api_requests_total{model="claude-haiku-2-2"}', 19) == 19


@pytest.mark.integration
def test_collector_down_absence_alert_expr(stack):
    expr = 'max(up{job="otel-collector"}) == 0 or absent(up{job="otel-collector"})'
    assert stack.query_prom('max(up{job="otel-collector"}) == 1', timeout=60)
    stack.compose(["stop", "-t", "1", "otel-collector"])
    rows = stack.query_prom(expr, timeout=60)
    assert rows


@pytest.mark.integration
def test_tsdb_full_wal_failure_visible(stack):
    stack.compose(["start", "otel-collector"])
    fill = stack.compose(
        ["exec", "-T", "--user", "root", "prometheus", "sh", "-c", "dd if=/dev/zero of=/prometheus/fill bs=1M 2>&1 || true"],
        check=False,
    )
    assert "No space left" in fill.stdout + fill.stderr

    def failed_writes():
        text = stack.scrape("prometheus", 9090)
        values = [
            float(line.split()[-1])
            for line in text.splitlines()
            if line.startswith("prometheus_tsdb_wal_writes_failed_total")
        ]
        return max(values) if values else 0.0

    conftest.poll(failed_writes, lambda value: value > 0, timeout=90)
