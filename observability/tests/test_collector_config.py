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
    assert conditions[:2] == ['log.attributes["event.name"] == nil', 'IsDouble(log.attributes["event.name"])']
    match = re.fullmatch(
        r'not IsMatch\(String\(log\.attributes\["event\.name"\]\), "\^\(([a-z_|]+)\)\$"\)',
        conditions[2],
    )
    assert match
    assert set(match.group(1).split("|")) == EXPECTED_EVENTS
    assert len(conditions) == 3


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
    assert conditions[:2] == ["type != METRIC_DATA_TYPE_SUM", "type == METRIC_DATA_TYPE_SUM and is_monotonic == false"]
    assert len(conditions) == 3
    datapoint = config["processors"]["filter/metrics"]["metrics"]["datapoint"]
    assert datapoint == ["not (datapoint.value_double > -1.0e308 and datapoint.value_double < 1.0e308)"]
    match = re.fullmatch(r'not IsMatch\(name, "\^claude_code\[\.\]\(([a-z_|\[\].]+)\)\$"\)', conditions[2])
    assert match
    assert re.search(r'(?<!\[)\.|\.(?!\])', match.group(1)) is None
    names = match.group(1).replace("[.]", ".").split("|")
    assert {"claude_code." + n for n in names} == PASSTHROUGH_METRICS
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


@pytest.mark.config
def test_passthrough_metric_description_cleared_and_unit_pinned():
    groups = load()["processors"]["transform/metric_attrs"]["metric_statements"]
    metric = [g for g in groups if g["context"] == "metric"]
    assert len(metric) == 1
    statements = metric[0]["statements"]
    assert statements[:2] == ['set(metric.description, "")', 'set(metric.unit, "")']
    pinned = {
        re.fullmatch(r'set\(metric\.unit, "(\w+)"\) where metric\.name == "([a-z_.]+)"', s).groups()
        for s in statements[2:]
    }
    assert pinned == {
        ("USD", "claude_code.cost.usage"),
        ("tokens", "claude_code.token.usage"),
        ("s", "claude_code.active_time.total"),
    }


@pytest.mark.config
def test_sanitize_processors_fail_closed():
    config = load_debug()
    sanitizers = [p for p in config["processors"] if component_type(p) in ("filter", "transform")]
    assert len(sanitizers) == 10
    for name in sanitizers:
        assert config["processors"][name]["error_mode"] == "propagate", name


def enumerated(statements, key):
    pattern = r'set\(\w+\.attributes\["%s"\], "other"\) where .* "\^\(([^"]+)\)\$"\)\)?' % re.escape(key)
    matches = [re.fullmatch(pattern, s) for s in statements]
    found = [m.group(1) for m in matches if m]
    assert len(found) == 1, key
    return found[0]


@pytest.mark.config
def test_low_cardinality_labels_are_enumerated():
    log = load()["processors"]["transform/allowlist"]["log_statements"][0]["statements"]
    datapoint = load()["processors"]["transform/metric_attrs"]["metric_statements"][0]["statements"]
    sources = "config|hook|user_permanent|user_temporary|user_abort|user_reject"
    assert enumerated(log, "source") == sources
    assert enumerated(datapoint, "source") == sources
    assert len(enumerated(log, "hook_event").split("|")) == 33
    assert enumerated(log, "error_type") == "Error|ShellError|Error:(ENOENT|EACCES|EPERM|EISDIR|ENOTDIR|EEXIST|ENOTEMPTY|ETIMEDOUT)"
    assert enumerated(datapoint, "type") == "added|removed|input|output|cacheRead|cacheCreation|user|cli"
    assert enumerated(datapoint, "query_source") == "main|subagent|auxiliary|plugin_model_complete|plugin_model_classify"


@pytest.mark.config
def test_free_text_labels_require_string_values():
    log = load()["processors"]["transform/allowlist"]["log_statements"][0]["statements"]
    datapoint = load()["processors"]["transform/metric_attrs"]["metric_statements"][0]["statements"]
    traces = load_debug()["processors"]["transform/traces"]["trace_statements"][0]["statements"]
    checks = [(log, "log", k) for k in ("model", "tool_name")]
    checks += [(traces, "span", k) for k in ("model", "tool_name")]
    checks += [(datapoint, "datapoint", k) for k in ("session.id", "terminal.type", "model", "tool_name", "language")]
    for statements, context, key in checks:
        guard = f'not IsString({context}.attributes["{key}"]) or not IsMatch({context}.attributes["{key}"], '
        assert any(s.startswith(f'set({context}.attributes["{key}"], "other")') and guard in s for s in statements), key


@pytest.mark.config
def test_hook_counts_default_to_zero_and_bad_duration_dropped():
    log = load()["processors"]["transform/allowlist"]["log_statements"][0]["statements"]
    for key in ("num_success", "num_blocking", "num_non_blocking_error", "num_cancelled"):
        hook = f'set(log.attributes["{key}"], "0") where log.attributes["event.name"] == "hook_execution_complete" and (log.attributes["{key}"] == nil or '
        assert any(s.startswith(hook) and s.endswith('"^[0-9]{1,3}$"))') for s in log), key
        assert f'delete_key(log.attributes, "{key}") where log.attributes["event.name"] != "hook_execution_complete"' in log, key
    assert any(s.startswith('delete_key(log.attributes, "duration_ms") where ') for s in log)
    definitions = {d["name"]: d for d in load()["connectors"]["signal_to_metrics"]["logs"]}
    for name in ("agent.api.duration", "agent.tool.duration"):
        assert len(definitions[name]["conditions"]) == 1
        assert definitions[name]["conditions"][0].endswith(' and log.attributes["duration_ms"] != nil'), name


@pytest.mark.config
def test_debug_traces_drop_links_and_scope_metadata():
    groups = {g["context"]: g["statements"] for g in load_debug()["processors"]["transform/traces"]["trace_statements"]}
    assert not any("links" in s for s in groups["span"])
    assert "Len(span.links) > 0" in load_debug()["processors"]["filter/spans"]["traces"]["span"]
    assert groups["scope"] == ['keep_keys(scope.attributes, [])', 'set(scope.name, "claude_code")', 'set(scope.version, "")']
    logs = {g["context"]: g["statements"] for g in load()["processors"]["transform/resource"]["log_statements"]}
    assert logs["scope"] == groups["scope"]


@pytest.mark.config
def test_ingest_path_containers_hardened():
    services = yaml.safe_load(conftest.REAL_COMPOSE.read_text())["services"]
    for name in ("otel-collector", "tempo", "loki"):
        assert services[name]["cap_drop"] == ["ALL"], name
        assert services[name]["security_opt"] == ["no-new-privileges:true"], name
        assert services[name]["mem_limit"] == "512m", name
    assert services["otel-collector"]["read_only"] is True


@pytest.mark.config
def test_empty_typed_values_deleted_before_keep_keys():
    contexts = [
        ("log", load()["processors"]["transform/allowlist"]["log_statements"][0]["statements"]),
        ("datapoint", load()["processors"]["transform/metric_attrs"]["metric_statements"][0]["statements"]),
        ("span", load_debug()["processors"]["transform/traces"]["trace_statements"][0]["statements"]),
    ]
    for context, statements in contexts:
        sanitized = {
            s.split('"')[1]
            for s in statements
            if s.startswith(f"set({context}.attributes[") and '"other"' in s
        }
        deleted = {
            s.split('"')[1]
            for s in statements
            if s.startswith(f"delete_key({context}.attributes, ") and s.endswith("] == nil")
        }
        assert sanitized - {"tenant", "repo", "vcs.repository.name"} <= deleted, (context, sorted(sanitized - deleted))


def ordered_expressions(config, processors):
    for name in processors:
        processor = config["processors"][name]
        if component_type(name) == "filter":
            for signal in processor.values():
                if isinstance(signal, dict):
                    for conditions in signal.values():
                        yield from conditions
        for key, groups in processor.items():
            if key.endswith("_statements"):
                for group in groups:
                    yield from group.get("conditions", [])
                    yield from group["statements"]


@pytest.mark.config
@pytest.mark.parametrize("loader", [load, load_debug], ids=["default", "debug"])
def test_string_reads_guarded_against_non_finite_doubles(loader):
    config = loader()
    checked = 0
    for name, pipeline in config["service"]["pipelines"].items():
        guarded = set()
        for expression in ordered_expressions(config, [p for p in pipeline["processors"] if p in config["processors"]]):
            for path in re.findall(r'(?<!Is)String\((\w+\.attributes\["[^"]+"\])\)', expression):
                assert path in guarded, (name, path)
                checked += 1
            guarded.update(re.findall(r'IsDouble\((\w+\.attributes\["[^"]+"\])\)(?:$| and not \(\1 > -1\.0e308 and \1 < 1\.0e308\))', expression))
    assert checked >= 20


@pytest.mark.config
def test_service_name_enumerated_and_severity_cleared():
    expected = 'set(resource.attributes["service.name"], "other") where resource.attributes["service.name"] == nil or not IsString(resource.attributes["service.name"]) or not IsMatch(resource.attributes["service.name"], "^(claude-code)$")'
    groups = [
        load()["processors"]["transform/resource"]["log_statements"][0]["statements"],
        load()["processors"]["transform/metric_resource"]["metric_statements"][0]["statements"],
        load_debug()["processors"]["transform/trace_resource"]["trace_statements"][0]["statements"],
    ]
    for statements in groups:
        assert statements[1:] == [expected]
    log = load()["processors"]["transform/allowlist"]["log_statements"][0]["statements"]
    assert "set(log.severity_number, 0)" in log
