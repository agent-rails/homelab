import pytest

import conftest
import otlp

STACK = {"services": ["prometheus"], "ports": {"prometheus": [9090]}}


@pytest.mark.config
def test_docker_context_assertion_rejects_other_contexts(monkeypatch):
    monkeypatch.setenv("DOCKER_CONTEXT", "desktop-linux")
    with pytest.raises(AssertionError, match="DOCKER_CONTEXT must be 'orbstack'"):
        conftest.assert_docker_context()
    monkeypatch.delenv("DOCKER_CONTEXT")
    with pytest.raises(AssertionError):
        conftest.assert_docker_context()


@pytest.mark.config
def test_free_ports_are_distinct_and_unprivileged():
    ports = conftest.free_ports(5)
    assert len(set(ports)) == 5
    assert all(port > 1023 for port in ports)


@pytest.mark.config
def test_test_compose_never_publishes_live_ports_or_secrets():
    import yaml

    real = yaml.safe_load(conftest.REAL_COMPOSE.read_text())
    stack = conftest.Stack(["prometheus"], {"prometheus": [9090]})
    document = conftest.build_test_compose(
        real, ["prometheus"], stack.host_ports, stack.workdir, "50m"
    )
    service = document["services"]["prometheus"]
    assert "secrets" not in service
    assert service["ports"] == [f"127.0.0.1:{stack.port('prometheus', 9090)}:9090"]
    assert stack.port("prometheus", 9090) not in (9090, 3000)
    assert "name" not in document
    assert all(not volume.startswith("prometheus-data") for volume in service["volumes"])


@pytest.mark.config
def test_test_compose_injects_dummy_webhook_not_host_secret():
    import stat

    import yaml

    real = yaml.safe_load(conftest.REAL_COMPOSE.read_text())
    assert real["services"]["grafana"]["secrets"] == ["slack_webhook_url"]
    stack = conftest.Stack(["prometheus", "grafana"], {"prometheus": [9090], "grafana": [3000]})
    stack.render()
    document = yaml.safe_load(stack.compose_file.read_text())
    secret_file = conftest.dummy_secret_path(stack.workdir, "slack_webhook_url")
    assert document["secrets"] == {"slack_webhook_url": {"file": str(secret_file)}}
    assert document["services"]["grafana"]["secrets"] == ["slack_webhook_url"]
    assert "secrets" not in document["services"]["prometheus"]
    assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600
    assert secret_file.read_text().startswith("https://hooks.slack.invalid/")
    assert ".config/observability" not in stack.compose_file.read_text()


@pytest.mark.integration
def test_stack_accepts_otlp_and_query_polls(stack):
    status, body = stack.send_otlp(
        "prometheus",
        9090,
        "/api/v1/otlp/v1/metrics",
        otlp.sum_payload(
            {"service.name": "harness"},
            "harness.probe",
            [({"k": "v"}, 3.0)],
            otlp.CUMULATIVE,
        ),
    )
    assert status == 200, body
    result = stack.query_prom("harness_probe_total")
    assert result[0]["value"][1] == "3"


@pytest.mark.integration
def test_query_prom_times_out_with_last_result(stack):
    with pytest.raises(AssertionError, match="timed out after 2s"):
        stack.query_prom("harness_never_exists_total", timeout=2)
