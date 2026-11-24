import re

import pytest
import yaml

import conftest

COLLECTOR_DIR = conftest.OBSERVABILITY_DIR / "otel-collector"
CONFIG_SETS = {"default": ["config.yaml", "empty.yaml"]}
EXPECTED_EVENTS = {
    "api_request",
    "api_error",
    "api_retries_exhausted",
    "tool_result",
    "tool_decision",
    "hook_execution_complete",
}
SANITIZE_CHAIN = {
    "logs": [
        "memory_limiter",
        "filter/events",
        "transform/error_class",
        "transform/allowlist",
        "transform/resource",
    ],
}
PUSH_EXPORTER_TYPES = {"otlp", "otlp_http", "otlp_grpc", "otlphttp", "prometheus_remote_write"}


def load(name="config.yaml"):
    return yaml.safe_load((COLLECTOR_DIR / name).read_text())


def component_type(component_id):
    return component_id.split("/")[0]


@pytest.fixture(scope="module", params=sorted(CONFIG_SETS))
def validation(request):
    return conftest.validate_collector(CONFIG_SETS[request.param])


@pytest.mark.config
def test_config_validates_default_and_debug(validation):
    assert validation.returncode == 0, validation.stderr


@pytest.mark.config
def test_no_deprecation_warnings(validation):
    assert "deprecat" not in (validation.stdout + validation.stderr).lower()


@pytest.mark.config
def test_event_allowlist_golden():
    conditions = load()["processors"]["filter/events"]["logs"]["log_record"]
    assert conditions[0] == 'log.attributes["event.name"] == nil'
    match = re.fullmatch(
        r'not IsMatch\(String\(log\.attributes\["event\.name"\]\), "\^\(([a-z_|]+)\)\$"\)',
        conditions[1],
    )
    assert match
    assert set(match.group(1).split("|")) == EXPECTED_EVENTS
    assert len(conditions) == 2


@pytest.mark.config
def test_every_pipeline_has_sanitize_chain():
    config = load()
    pipelines = config["service"]["pipelines"]
    checked = 0
    for name, pipeline in pipelines.items():
        signal = name.split("/")[0]
        if "otlp" not in pipeline["receivers"]:
            continue
        chain = [p for p in pipeline["processors"] if p in SANITIZE_CHAIN[signal]]
        assert chain == SANITIZE_CHAIN[signal], name
        checked += 1
    assert checked >= 1


@pytest.mark.config
def test_no_push_exporters():
    config = load()
    used = {
        exporter
        for pipeline in config["service"]["pipelines"].values()
        for exporter in pipeline["exporters"]
        if exporter in config.get("exporters", {})
    }
    assert {component_type(e) for e in used} & PUSH_EXPORTER_TYPES == set()
    assert {component_type(e) for e in config["exporters"]} == {"prometheus"}


@pytest.mark.config
def test_every_stm_metric_has_condition_monotonic_defaults():
    definitions = load()["connectors"]["signal_to_metrics"]["logs"]
    assert definitions
    for definition in definitions:
        name = definition["name"]
        assert any('log.attributes["event.name"] == ' in c for c in definition["conditions"]), name
        if "sum" in definition:
            assert definition["sum"]["monotonic"] is True, name
        else:
            assert definition["histogram"]["buckets"], name
        assert definition["attributes"], name
        for attribute in definition["attributes"]:
            assert "default_value" in attribute, (name, attribute)


@pytest.mark.config
def test_no_hook_name_dimension():
    config = load()
    for definition in config["connectors"]["signal_to_metrics"]["logs"]:
        assert "hook_name" not in [a["key"] for a in definition["attributes"]]
    keep = [
        s
        for s in config["processors"]["transform/allowlist"]["log_statements"][0]["statements"]
        if s.startswith("keep_keys(")
    ]
    assert len(keep) == 1 and "hook_name" not in keep[0]


@pytest.mark.config
def test_hooks_have_four_constant_outcomes():
    hooks = [d for d in load()["connectors"]["signal_to_metrics"]["logs"] if d["name"] == "agent.hook.executions"]
    outcomes = sorted(
        a["default_value"] for d in hooks for a in d["attributes"] if a["key"] == "outcome"
    )
    assert outcomes == ["blocking", "cancelled", "non_blocking_error", "success"]


@pytest.mark.config
def test_container_memory_above_memory_limiter_and_body_limited():
    compose = yaml.safe_load(conftest.REAL_COMPOSE.read_text())
    mem_limit = compose["services"]["otel-collector"]["mem_limit"]
    assert mem_limit.endswith("m")
    config = load()
    assert int(mem_limit[:-1]) > config["processors"]["memory_limiter"]["limit_mib"]
    assert config["receivers"]["otlp"]["protocols"]["http"]["max_request_body_size"] > 0
    assert config["receivers"]["otlp"]["protocols"]["http"]["endpoint"] == "0.0.0.0:4318"


@pytest.mark.config
def test_collector_published_on_loopback_only_and_prometheus_scrapes_it():
    compose = yaml.safe_load(conftest.REAL_COMPOSE.read_text())
    assert compose["services"]["otel-collector"]["ports"] == ["127.0.0.1:4318:4318"]
    jobs = {j["job_name"]: j for j in yaml.safe_load(conftest.REAL_PROMETHEUS.read_text())["scrape_configs"]}
    targets = jobs["otel-collector"]["static_configs"][0]["targets"]
    assert sorted(targets) == ["otel-collector:8888", "otel-collector:8889"]


@pytest.mark.config
def test_resource_attributes_reduced_to_service_name():
    statements = load()["processors"]["transform/resource"]["log_statements"][0]["statements"]
    assert statements[0] == 'keep_keys(resource.attributes, ["service.name"])'
