import subprocess

import pytest
import yaml

import conftest

pytestmark = pytest.mark.config

ALERTING_FILE = conftest.OBSERVABILITY_DIR / "grafana/provisioning/alerting/agent-telemetry.yaml"
OPERATORS = {"gt": ">", "lt": "<"}

SCENARIOS = {
    "agent-collector-down": [
        {
            "series": {'up{job="otel-collector"}': "1 1 0x20"},
            "eval_time": "10m",
            "fires": [{"job": "otel-collector"}],
        },
        {"series": {'up{job="otel-collector"}': "1x20"}, "eval_time": "10m", "fires": []},
    ],
    "agent-derived-events-missing": [
        {
            "series": {
                'claude_code_active_time_seconds_total{session_id="s1"}': "0+10x60",
                "agent_api_requests_total": "5x60",
            },
            "eval_time": "50m",
            "fires": [{}],
        },
        {
            "series": {'claude_code_active_time_seconds_total{session_id="s1"}': "0+10x60"},
            "eval_time": "50m",
            "fires": [{}],
        },
        {
            "series": {
                'claude_code_active_time_seconds_total{session_id="s1"}': "0+10x60",
                "agent_api_requests_total": "0+1x60",
            },
            "eval_time": "50m",
            "fires": [],
        },
        {
            "series": {
                'claude_code_active_time_seconds_total{session_id="s1"}': "0+10x60",
                "agent_api_requests_total": "_x39 1x21",
            },
            "eval_time": "50m",
            "fires": [],
        },
        {
            "series": {
                'claude_code_active_time_seconds_total{session_id="s1"}': "100x60",
                "agent_api_requests_total": "5x60",
            },
            "eval_time": "50m",
            "fires": [],
        },
    ],
    "agent-prometheus-unreachable": [
        {"series": {'up{job="prometheus"}': "0x20"}, "eval_time": "10m", "fires": [{"job": "prometheus"}]},
        {"series": {'up{job="prometheus"}': "1x20"}, "eval_time": "10m", "fires": []},
    ],
    "agent-d2c-datapoint-errors": [
        {
            "series": {
                "otelcol_deltatocumulative_datapoints": "0+100x40",
                'otelcol_deltatocumulative_datapoints{error="delta.ErrOutOfOrder"}': "_x19 1x21",
            },
            "eval_time": "26m",
            "fires": [{"error": "delta.ErrOutOfOrder"}],
        },
        {
            "series": {
                "otelcol_deltatocumulative_datapoints": "0+100x40",
                'otelcol_deltatocumulative_datapoints{error="limit"}': "_x19 1x21",
            },
            "eval_time": "26m",
            "fires": [],
        },
    ],
    "agent-d2c-streams-dropped": [
        {
            "series": {'otelcol_deltatocumulative_datapoints{error="limit"}': "_x19 1x21"},
            "eval_time": "26m",
            "fires": [{}],
        },
        {
            "series": {'otelcol_deltatocumulative_datapoints{error="limit"}': "1x40"},
            "eval_time": "26m",
            "fires": [],
        },
    ],
    "agent-prometheus-wal-write-failures": [
        {
            "series": {"prometheus_tsdb_wal_writes_failed_total": "0x19 1x21"},
            "eval_time": "26m",
            "fires": [{}],
        },
        {"series": {"prometheus_tsdb_wal_writes_failed_total": "0x40"}, "eval_time": "26m", "fires": []},
    ],
    "agent-prometheus-out-of-order-samples": [
        {
            "series": {"prometheus_target_scrapes_sample_out_of_order_total": "0x19 3x21"},
            "eval_time": "26m",
            "fires": [{}],
        },
        {"series": {"prometheus_target_scrapes_sample_out_of_order_total": "0x40"}, "eval_time": "26m", "fires": []},
    ],
    "agent-api-error-rate": [
        {
            "series": {
                'agent_api_requests_total{model="m"}': "0+10x40",
                'agent_api_errors_total{error_class="http_529",model="m"}': "_x19 1+2x21",
            },
            "eval_time": "30m",
            "fires": [{"error_class": "http_529"}],
        },
        {
            "series": {'agent_api_errors_total{error_class="connection",model="m"}': "_x19 1x21"},
            "eval_time": "26m",
            "fires": [{"error_class": "connection"}],
        },
        {
            "series": {
                'agent_api_requests_total{model="m"}': "0+10x40",
                'agent_api_errors_total{error_class="http_529",model="m"}': "_x19 1x21",
            },
            "eval_time": "26m",
            "fires": [],
        },
    ],
    "agent-tool-errors": [
        {
            "series": {
                'agent_tool_results_total{tool_name="Bash",success="false",error_type="ENOENT"}': "0+2x40",
                'agent_tool_results_total{tool_name="Bash",success="true",error_type="none"}': "0+50x40",
                'agent_tool_results_total{tool_name="Read",success="false",error_type="EACCES"}': "0+0.5x40",
            },
            "eval_time": "30m",
            "fires": [{"error_type": "ENOENT"}],
        },
        {
            "series": {'agent_tool_results_total{tool_name="Bash",success="false",error_type="Exit"}': "_x19 12x21"},
            "eval_time": "26m",
            "fires": [{"error_type": "Exit"}],
        },
        {
            "series": {'agent_tool_results_total{tool_name="Read",success="false",error_type="EACCES"}': "0+0.5x40"},
            "eval_time": "30m",
            "fires": [],
        },
    ],
    "agent-classifier-outage": [
        {
            "series": {
                'agent_tool_decisions_total{decision="reject",source="classifier",classifier_verdict="unavailable"}': "_x19 1x21",
                'agent_tool_decisions_total{decision="accept",source="classifier",classifier_verdict="allow"}': "0+5x40",
            },
            "eval_time": "26m",
            "fires": [{"classifier_verdict": "unavailable"}],
        },
        {
            "series": {'agent_tool_decisions_total{decision="accept",source="classifier",classifier_verdict="allow"}': "0+5x40"},
            "eval_time": "26m",
            "fires": [],
        },
    ],
    "agent-hook-non-blocking-errors": [
        {
            "series": {
                'agent_hook_executions_total{hook_event="PreToolUse",outcome="non_blocking_error"}': "_x19 1x21",
                'agent_hook_executions_total{hook_event="PreToolUse",outcome="success"}': "0+9x40",
            },
            "eval_time": "26m",
            "fires": [{"hook_event": "PreToolUse"}],
        },
        {
            "series": {'agent_hook_executions_total{hook_event="PreToolUse",outcome="non_blocking_error"}': "1x40"},
            "eval_time": "26m",
            "fires": [],
        },
    ],
}


def grafana_rules():
    config = yaml.safe_load(ALERTING_FILE.read_text())
    return [rule for group in config["groups"] for rule in group["rules"]]


def prometheus_rule(rule):
    query = next(node for node in rule["data"] if node["datasourceUid"] == "prometheus")
    threshold = next(node for node in rule["data"] if node["refId"] == rule["condition"])
    evaluator = threshold["model"]["conditions"][0]["evaluator"]
    expr = "(%s) %s %s" % (query["model"]["expr"], OPERATORS[evaluator["type"]], evaluator["params"][0])
    return {"alert": rule["uid"], "expr": expr, "for": rule["for"]}


def promtool_tests():
    tests = []
    for uid, scenarios in SCENARIOS.items():
        for scenario in scenarios:
            tests.append(
                {
                    "interval": "1m",
                    "input_series": [{"series": s, "values": v} for s, v in scenario["series"].items()],
                    "alert_rule_test": [
                        {
                            "eval_time": scenario["eval_time"],
                            "alertname": uid,
                            "exp_alerts": [{"exp_labels": labels} for labels in scenario["fires"]],
                        }
                    ],
                }
            )
    return {"rule_files": ["rules.yaml"], "evaluation_interval": "1m", "tests": tests}


def test_every_rule_has_firing_and_quiet_scenarios():
    assert set(SCENARIOS) == {rule["uid"] for rule in grafana_rules()}
    for uid, scenarios in SCENARIOS.items():
        assert any(s["fires"] for s in scenarios), uid
        assert any(not s["fires"] for s in scenarios), uid


def prometheus_image():
    return yaml.safe_load(conftest.REAL_COMPOSE.read_text())["services"]["prometheus"]["image"]


def test_alert_expressions_fire_only_in_failure_scenarios(tmp_path):
    rules = {"groups": [{"name": "agent", "rules": [prometheus_rule(rule) for rule in grafana_rules()]}]}
    (tmp_path / "rules.yaml").write_text(yaml.safe_dump(rules, sort_keys=False))
    (tmp_path / "tests.yaml").write_text(yaml.safe_dump(promtool_tests(), sort_keys=False))
    result = subprocess.run(
        [
            "docker", "run", "--rm", "--network", "none",
            "-v", f"{tmp_path}:/work:ro",
            "--entrypoint", "promtool",
            prometheus_image(),
            "test", "rules", "/work/tests.yaml",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SUCCESS" in result.stdout
