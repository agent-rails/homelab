import re

import pytest
import yaml

import conftest

COLLECTOR_DIR = conftest.OBSERVABILITY_DIR / "otel-collector"
CONFIG_SETS = {"default": ["config.yaml", "empty.yaml"], "debug": ["config.yaml", "config.debug.yaml"]}
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
    "metrics": [
        "memory_limiter",
        "filter/metrics",
        "transform/metric_attrs",
        "transform/metric_resource",
    ],
    "traces": [
        "memory_limiter",
        "filter/spans",
        "transform/traces",
        "transform/trace_resource",
    ],
}
PUSH_EXPORTER_TYPES = {"otlp", "otlp_http", "otlp_grpc", "otlphttp", "prometheus_remote_write"}


def load(name="config.yaml"):
    return yaml.safe_load((COLLECTOR_DIR / name).read_text())


def deep_merge(base, extra):
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_debug():
    return deep_merge(load(), load("config.debug.yaml"))


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
@pytest.mark.parametrize("loader", [load, load_debug], ids=["default", "debug"])
def test_every_pipeline_has_sanitize_chain(loader):
    config = loader()
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


PASSTHROUGH_METRICS = {
    "claude_code.session.count",
    "claude_code.lines_of_code.count",
    "claude_code.pull_request.count",
    "claude_code.commit.count",
    "claude_code.cost.usage",
    "claude_code.token.usage",
    "claude_code.code_edit_tool.decision",
    "claude_code.active_time.total",
}


@pytest.mark.config
def test_passthrough_metric_allowlist_golden():
    config = load()
    conditions = config["processors"]["filter/metrics"]["metrics"]["metric"]
    assert len(conditions) == 1
    match = re.fullmatch(r'not IsMatch\(name, "\^claude_code\[\.\]\(([a-z_.|]+)\)\$"\)', conditions[0])
    assert match
    assert {"claude_code." + n for n in match.group(1).split("|")} == PASSTHROUGH_METRICS
    statements = config["processors"]["transform/metric_attrs"]["metric_statements"][0]["statements"]
    keep = [s for s in statements if s.startswith("keep_keys(datapoint.attributes")]
    assert len(keep) == 1 and statements[-1] == keep[0]
    for forbidden in ("user.", "organization", "workspace", "vcs.repository.url", "email"):
        assert forbidden not in keep[0]
    assert "session.id" in keep[0]
    resource = config["processors"]["transform/metric_resource"]["metric_statements"][0]["statements"]
    assert resource[0] == 'keep_keys(resource.attributes, ["service.name"])'


@pytest.mark.config
def test_prometheus_otlp_receiver_still_enabled_in_this_phase():
    compose = yaml.safe_load(conftest.REAL_COMPOSE.read_text())
    assert "--web.enable-otlp-receiver" in compose["services"]["prometheus"]["command"]


@pytest.mark.config
def test_metric_unit_contract():
    definitions = {d["name"]: d for d in load()["connectors"]["signal_to_metrics"]["logs"]}
    for name in ("agent.api.duration", "agent.tool.duration"):
        assert definitions[name]["unit"] == "ms"
    for name, definition in definitions.items():
        if "sum" in definition:
            assert "unit" not in definition, name
            assert definition["sum"]["value"] == "1" or name == "agent.hook.executions"


@pytest.mark.config
def test_debug_config_adds_only_loki_and_tempo_pipelines():
    default = load()
    merged = load_debug()
    assert set(merged["service"]["pipelines"]) - set(default["service"]["pipelines"]) == {"logs/raw", "traces"}
    for name in default["service"]["pipelines"]:
        assert merged["service"]["pipelines"][name] == default["service"]["pipelines"][name]
    added = set(merged["exporters"]) - set(default["exporters"])
    assert added == {"otlp_http/loki", "otlp_http/tempo"}
    assert merged["exporters"]["otlp_http/loki"]["endpoint"].startswith("http://loki:")
    assert merged["exporters"]["otlp_http/tempo"]["endpoint"].startswith("http://tempo:")
    for exporter in added:
        assert merged["exporters"][exporter]["sending_queue"]["queue_size"] > 0


@pytest.mark.config
def test_debug_backends_are_profiled_pinned_and_retention_bounded():
    compose = yaml.safe_load(conftest.REAL_COMPOSE.read_text())
    assert compose["services"]["tempo"]["profiles"] == ["traces"]
    assert compose["services"]["loki"]["profiles"] == ["logs"]
    for service in ("tempo", "loki"):
        assert "ports" not in compose["services"][service]
        assert "@sha256:" in compose["services"][service]["image"]
    tempo = yaml.safe_load((conftest.OBSERVABILITY_DIR / "tempo" / "config.yaml").read_text())
    loki = yaml.safe_load((conftest.OBSERVABILITY_DIR / "loki" / "config.yaml").read_text())
    assert tempo["compactor"]["compaction"]["block_retention"] == "72h"
    assert loki["limits_config"]["retention_period"] == "168h"
    assert loki["compactor"]["retention_enabled"] is True
    command = " ".join(compose["services"]["otel-collector"]["command"])
    assert "${OTELCOL_DEBUG_CONFIG:-/etc/otelcol/empty.yaml}" in command


@pytest.mark.config
def test_grafana_has_tempo_and_loki_datasources():
    root = conftest.OBSERVABILITY_DIR / "grafana" / "provisioning" / "datasources"
    urls = {}
    for path in root.glob("*.yaml"):
        for source in yaml.safe_load(path.read_text())["datasources"]:
            urls[source["uid"]] = source["url"]
    assert urls["tempo"] == "http://tempo:3200"
    assert urls["loki"] == "http://loki:3100"


@pytest.mark.config
def test_collector_merge_keeps_default_pipelines_and_adds_debug():
    command = [
        "docker", "run", "--rm", "-v", f"{COLLECTOR_DIR}:/cfg:ro", conftest.collector_image(),
        "print-config", "--config=/cfg/config.yaml", "--config=/cfg/config.debug.yaml",
    ]
    result = conftest.subprocess.run(command, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    printed = yaml.safe_load(result.stdout)
    default = load()["service"]["pipelines"]
    pipelines = printed["service"]["pipelines"]
    assert set(pipelines) == set(default) | {"logs/raw", "traces"}
    for name, pipeline in default.items():
        assert pipelines[name]["processors"] == pipeline["processors"]
        assert pipelines[name]["exporters"] == pipeline["exporters"]
    assert pipelines["logs/raw"]["processors"] == SANITIZE_CHAIN["logs"] + ["batch/events"]
