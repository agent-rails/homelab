import json
import re
import subprocess

import pytest
import yaml

import conftest

pytestmark = pytest.mark.config

ALERTING_FILE = conftest.OBSERVABILITY_DIR / "grafana/provisioning/alerting/agent-telemetry.yaml"
DASHBOARD_FILE = conftest.OBSERVABILITY_DIR / "grafana/dashboards/agent-failures.json"
RECIPIENT_SECRET = "/run/secrets/alert_email_to"
FORBIDDEN_LITERALS = (
    re.compile(r"hooks\.slack", re.IGNORECASE),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"),
)

GOLDEN_METRICS = {
    "agent_api_requests_total",
    "agent_api_errors_total",
    "agent_api_duration_milliseconds_bucket",
    "agent_api_duration_milliseconds_sum",
    "agent_api_duration_milliseconds_count",
    "agent_tool_results_total",
    "agent_tool_duration_milliseconds_bucket",
    "agent_tool_duration_milliseconds_sum",
    "agent_tool_duration_milliseconds_count",
    "agent_tool_decisions_total",
    "agent_hook_executions_total",
    "claude_code_active_time_seconds_total",
    "up",
    "otelcol_deltatocumulative_datapoints",
    "prometheus_tsdb_wal_writes_failed_total",
    "prometheus_target_scrapes_sample_out_of_order_total",
}

GOLDEN_LABELS = {
    "model",
    "error_class",
    "status_code",
    "tool_name",
    "success",
    "error_type",
    "decision",
    "source",
    "classifier_verdict",
    "hook_event",
    "outcome",
    "repo",
    "tenant",
    "le",
    "job",
    "error",
    "session_id",
}

PROMQL_KEYWORDS = {
    "and",
    "or",
    "unless",
    "bool",
    "offset",
    "by",
    "without",
    "on",
    "ignoring",
    "group_left",
    "group_right",
}

EXPECTED_STATES = {
    "agent-collector-down": ("Alerting", "Alerting"),
    "agent-derived-events-missing": ("Alerting", "Alerting"),
    "agent-prometheus-unreachable": ("Alerting", "Alerting"),
    "agent-d2c-datapoint-errors": ("OK", "Error"),
    "agent-d2c-streams-dropped": ("OK", "Error"),
    "agent-prometheus-wal-write-failures": ("OK", "Error"),
    "agent-prometheus-out-of-order-samples": ("OK", "Error"),
    "agent-api-error-rate": ("OK", "Error"),
    "agent-tool-errors": ("OK", "Error"),
    "agent-classifier-outage": ("OK", "Error"),
    "agent-hook-non-blocking-errors": ("OK", "Error"),
}

PAUSED_UNTIL_CUTOVER = {"agent-derived-events-missing"}

RARE_EVENT_RULES = {
    "agent-d2c-datapoint-errors",
    "agent-d2c-streams-dropped",
    "agent-api-error-rate",
    "agent-tool-errors",
    "agent-classifier-outage",
    "agent-hook-non-blocking-errors",
}


def load_alerting():
    return yaml.safe_load(ALERTING_FILE.read_text())


def all_rules(config):
    return [rule for group in config["groups"] for rule in group["rules"]]


def query_node(rule):
    nodes = [node for node in rule["data"] if node["datasourceUid"] == "prometheus"]
    assert len(nodes) == 1, rule["uid"]
    return nodes[0]


def threshold_node(rule):
    nodes = [node for node in rule["data"] if node["model"].get("type") == "threshold"]
    assert len(nodes) == 1, rule["uid"]
    return nodes[0]


def strip_promql(expr):
    labels = set()
    text = re.sub(r'"(?:[^"\\]|\\.)*"', '""', expr)
    for body in re.findall(r"\{([^}]*)\}", text):
        labels.update(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:=~|!~|!=|=)", body))
    text = re.sub(r"\{[^}]*\}", " ", text)
    grouping = r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\(([^)]*)\)"
    for body in re.findall(grouping, text):
        labels.update(name.strip() for name in body.split(",") if name.strip())
    text = re.sub(grouping, " ", text)
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"\boffset\s+\d+[smhdwy]", " ", text)
    text = re.sub(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?", " ", text)
    names = set()
    for match in re.finditer(r"(?<![A-Za-z0-9_.])([A-Za-z_:][A-Za-z0-9_:]*)(\s*\()?", text):
        if match.group(2) or match.group(1) in PROMQL_KEYWORDS:
            continue
        names.add(match.group(1))
    return names, labels


def dashboard_exprs(dashboard):
    return [
        target["expr"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
    ]


def test_strip_promql_finds_names_and_labels():
    names, labels = strip_promql(
        'sum by (error_class) (increase(a_total{repo=~"$repo"}[15m]) > 0 or (a_total unless a_total offset 15m)) / on() group_left() vector(0)'
    )
    assert names == {"a_total"}
    assert labels == {"error_class", "repo"}


def test_alerting_file_parses_with_expected_sections():
    config = load_alerting()
    assert config["apiVersion"] == 1
    assert set(config) == {"apiVersion", "groups", "contactPoints", "policies", "templates"}


def test_rule_uids_match_expected_set():
    uids = [rule["uid"] for rule in all_rules(load_alerting())]
    assert len(uids) == len(set(uids))
    assert set(uids) == set(EXPECTED_STATES)


def test_every_rule_queries_prometheus_datasource():
    for rule in all_rules(load_alerting()):
        node = query_node(rule)
        assert node["model"]["instant"] is True, rule["uid"]
        assert node["model"]["expr"].strip(), rule["uid"]


def test_every_rule_references_only_golden_metrics_and_labels():
    for rule in all_rules(load_alerting()):
        names, labels = strip_promql(query_node(rule)["model"]["expr"])
        assert names, rule["uid"]
        assert names <= GOLDEN_METRICS, (rule["uid"], names - GOLDEN_METRICS)
        assert labels <= GOLDEN_LABELS, (rule["uid"], labels - GOLDEN_LABELS)


def test_every_rule_has_separate_numeric_threshold_as_condition():
    for rule in all_rules(load_alerting()):
        node = threshold_node(rule)
        assert rule["condition"] == node["refId"], rule["uid"]
        assert node["datasourceUid"] == "__expr__", rule["uid"]
        assert node["model"]["expression"] == query_node(rule)["refId"], rule["uid"]
        evaluator = node["model"]["conditions"][0]["evaluator"]
        assert evaluator["type"] in {"gt", "lt"}, rule["uid"]
        assert len(evaluator["params"]) == 1, rule["uid"]
        assert isinstance(evaluator["params"][0], (int, float)), rule["uid"]


def test_query_expressions_carry_no_inline_threshold_comparison():
    for rule in all_rules(load_alerting()):
        expr = query_node(rule)["model"]["expr"]
        trailing = re.sub(r"\s+", " ", expr).strip()
        assert not re.search(r"(>|<|==|!=)\s*[0-9.]+\s*$", trailing), rule["uid"]


def test_every_rule_has_specified_nodata_error_and_pending():
    for rule in all_rules(load_alerting()):
        expected = EXPECTED_STATES[rule["uid"]]
        assert (rule["noDataState"], rule["execErrState"]) == expected, rule["uid"]
        assert rule["for"] == "5m", rule["uid"]
        assert rule["isPaused"] is (rule["uid"] in PAUSED_UNTIL_CUTOVER), rule["uid"]


def test_rare_event_rules_use_new_series_form():
    for rule in all_rules(load_alerting()):
        if rule["uid"] not in RARE_EVENT_RULES:
            continue
        expr = re.sub(r"\s+", " ", query_node(rule)["model"]["expr"])
        found = re.search(r"increase\((\w+)(\{[^}]*\})?\[15m\]\)\s*>\s*0 or \((\w+)(\{[^}]*\})? unless (\w+)(\{[^}]*\})? offset 15m\)", expr)
        assert found, rule["uid"]
        assert found.group(1) == found.group(3) == found.group(5), rule["uid"]
        assert found.group(2) == found.group(4) == found.group(6), rule["uid"]


def test_alert_windows_outlast_pending_period():
    for rule in all_rules(load_alerting()):
        for window in re.findall(r"\[(\d+)m\]", query_node(rule)["model"]["expr"]):
            assert int(window) >= 15, rule["uid"]


def test_contact_point_reads_email_recipient_from_secret_file():
    config = load_alerting()
    receivers = [r for cp in config["contactPoints"] for r in cp["receivers"]]
    assert len(receivers) == 1
    receiver = receivers[0]
    assert receiver["uid"] == "agent-email"
    assert receiver["type"] == "email"
    assert receiver["settings"]["addresses"] == "$__file{%s}" % RECIPIENT_SECRET
    assert set(receiver["settings"]) == {"addresses", "subject", "message"}


SMTP_SECRETS = {
    "smtp_host": "smtp-host",
    "smtp_user": "smtp-user",
    "smtp_from_address": "smtp-from-address",
    "smtp_password": "smtp-password",
    "alert_email_to": "alert-email-to",
}


def test_compose_reads_smtp_settings_only_from_secret_files():
    compose = yaml.safe_load(conftest.REAL_COMPOSE.read_text())
    grafana = compose["services"]["grafana"]
    assert "env_file" not in grafana
    assert grafana["secrets"] == list(SMTP_SECRETS)
    for name, filename in SMTP_SECRETS.items():
        assert compose["secrets"][name] == {"file": "${HOME}/.config/observability/" + filename}, name
    assert grafana["environment"] == {
        "GF_SMTP_ENABLED": "true",
        "GF_SMTP_STARTTLS_POLICY": "MandatoryStartTLS",
        "GF_SMTP_SKIP_VERIFY": "false",
        "GF_SMTP_HOST__FILE": "/run/secrets/smtp_host",
        "GF_SMTP_USER__FILE": "/run/secrets/smtp_user",
        "GF_SMTP_FROM_ADDRESS__FILE": "/run/secrets/smtp_from_address",
        "GF_SMTP_PASSWORD__FILE": "/run/secrets/smtp_password",
    }
    assert RECIPIENT_SECRET == "/run/secrets/alert_email_to"


def committed_config_files():
    listed = subprocess.run(
        ["git", "-C", str(conftest.OBSERVABILITY_DIR), "ls-files", "-z", "--full-name", "."],
        capture_output=True, text=True, check=True,
    ).stdout
    root = conftest.OBSERVABILITY_DIR.parent
    return [root / name for name in listed.split("\0") if name and not name.startswith("observability/tests/")]


def forbidden_literals(text):
    return [match.group(0) for pattern in FORBIDDEN_LITERALS for match in pattern.finditer(text)]


def test_forbidden_literal_scan_catches_webhooks_and_addresses():
    assert forbidden_literals("url: https://hooks.slack.com/services/T0/B0/x") == ["hooks.slack"]
    assert forbidden_literals("addresses: owner@example.com") == ["owner@example.com"]
    assert forbidden_literals("image: grafana/grafana:13.1.3@sha256:ab5cb380") == []


def test_committed_config_has_no_slack_webhook_or_email_address():
    files = committed_config_files()
    assert conftest.REAL_COMPOSE in files and ALERTING_FILE in files
    found = {str(path): hits for path in files if (hits := forbidden_literals(path.read_text()))}
    assert found == {}


def test_policy_routes_everything_to_the_email_contact_point():
    config = load_alerting()
    contact_point = config["contactPoints"][0]["name"]
    assert len(config["policies"]) == 1
    assert config["policies"][0]["receiver"] == contact_point
    assert "routes" not in config["policies"][0]


def test_dollar_appears_only_in_file_interpolation():
    text = ALERTING_FILE.read_text()
    assert re.findall(r"\$[^\n]*", text) == ["$__file{%s}" % RECIPIENT_SECRET]


def test_templates_render_label_values_only_through_escape():
    config = load_alerting()
    contact_settings = config["contactPoints"][0]["receivers"][0]["settings"]
    texts = [t["template"] for t in config["templates"]]
    texts += [contact_settings["subject"], contact_settings["message"]]
    escape_body = re.search(r'\{\{ define "agent\.esc" \}\}(.*?)\{\{ end \}\}', config["templates"][0]["template"])
    assert escape_body
    for char, entity in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;")):
        assert f'reReplaceAll "{char}" "{entity}"' in escape_body.group(1)
    assert escape_body.group(1).index('"&"') < escape_body.group(1).index('"<"')
    for text in texts:
        for action in re.findall(r"\{\{-?\s*(.*?)\s*-?\}\}", text):
            if re.search(r"\.(Labels|Annotations|CommonLabels|CommonAnnotations|GroupLabels|Name|Value)\b", action):
                assert action.startswith('template "agent.esc"') or action.startswith("range") or action.startswith("if ne .Name"), action
            assert "ValueString" not in action, action


def test_dashboard_parses_with_variables_and_sections():
    dashboard = json.loads(DASHBOARD_FILE.read_text())
    assert dashboard["uid"] == "agent-failures"
    variables = {v["name"] for v in dashboard["templating"]["list"]}
    assert variables == {"repo", "tenant"}
    rows = [p["title"] for p in dashboard["panels"] if p["type"] == "row"]
    assert rows[:4] == ["Model API", "Tools", "Permissions", "Hooks"]


def test_dashboard_queries_use_golden_metrics_and_prometheus_uid():
    dashboard = json.loads(DASHBOARD_FILE.read_text())
    exprs = dashboard_exprs(dashboard)
    assert exprs
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            assert target["datasource"]["uid"] == "prometheus", panel["title"]
    for expr in exprs:
        names, labels = strip_promql(expr)
        assert names and names <= GOLDEN_METRICS, (expr, names - GOLDEN_METRICS)
        assert labels <= GOLDEN_LABELS, (expr, labels - GOLDEN_LABELS)


def test_dashboard_filters_repo_and_tenant_on_labelled_counters():
    dashboard = json.loads(DASHBOARD_FILE.read_text())
    labelled = ("agent_api_requests_total", "agent_api_errors_total", "agent_tool_results_total", "agent_tool_decisions_total", "agent_hook_executions_total")
    rows = [p["title"] for p in dashboard["panels"] if p["type"] == "row"]
    assert rows[-1] == "Pipeline health"
    health_row = next(i for i, p in enumerate(dashboard["panels"]) if p["title"] == "Pipeline health")
    failure_panels = {"panels": dashboard["panels"][:health_row]}
    for expr in dashboard_exprs(failure_panels):
        for selector in re.findall(r"(\w+)(\{[^}]*\})?", expr):
            if selector[0] in labelled:
                assert 'repo=~"$repo"' in selector[1] and 'tenant=~"$tenant"' in selector[1], expr


def test_dashboard_aggregates_passthrough_without_session_id():
    dashboard = json.loads(DASHBOARD_FILE.read_text())
    passthrough = [e for e in dashboard_exprs(dashboard) if "claude_code_" in e]
    assert passthrough
    for expr in passthrough:
        assert "sum without(session_id)" in expr, expr


def test_dashboard_panel_ids_unique():
    dashboard = json.loads(DASHBOARD_FILE.read_text())
    ids = [p["id"] for p in dashboard["panels"]]
    assert len(ids) == len(set(ids))


def test_dashboard_shows_labels_collapsed_to_other():
    dashboard = json.loads(DASHBOARD_FILE.read_text())
    panel = next(p for p in dashboard["panels"] if p["title"] == "Labels collapsed to other")
    labels = {re.search(r"\{(\w+)=\"other\"\}", t["expr"]).group(1) for t in panel["targets"]}
    assert labels == {"model", "error_type", "tool_name", "hook_event", "repo", "tenant"}


def test_collector_down_summary_names_instance():
    rule = next(r for r in all_rules(load_alerting()) if r["uid"] == "agent-collector-down")
    assert "{{ with .Labels.instance }} at {{ . }}{{ end }}" in rule["annotations"]["summary"]
