import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest
import yaml

import conftest
import events
from test_grafana_alerting_config import GOLDEN_METRICS

pytestmark = pytest.mark.integration

STACK = {
    "services": ["prometheus", "otel-collector", "grafana"],
    "ports": {"prometheus": [9090], "otel-collector": [4318, 13133], "grafana": [3000]},
}
ALERTING_FILE = conftest.OBSERVABILITY_DIR / "grafana/provisioning/alerting/agent-telemetry.yaml"
DUMMY_WEBHOOK = conftest.DUMMY_SECRETS["slack_webhook_url"].strip()
GRAFANA_READY_TIMEOUT_S = 90
EVALUATION_TIMEOUT_S = 150
ERROR_RATE_RULE = "agent-api-error-rate"


class GrafanaClient:
    def __init__(self, base):
        self.base = base
        token = base64.b64encode(b"admin:admin").decode()
        self.headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

    def request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=self.headers, method=method)
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read())

    def get(self, path):
        return self.request("GET", path)


def grafana_logs(stack):
    result = stack.compose(["logs", "--no-color", "--no-log-prefix", "grafana"], check=False)
    return result.stdout + result.stderr


@pytest.fixture(scope="module")
def grafana(stack):
    client = GrafanaClient(stack.url("grafana", 3000))

    def fetch():
        if client.get("/api/health").get("database") != "ok":
            return False
        return isinstance(client.get("/api/v1/provisioning/alert-rules"), list)

    try:
        conftest.poll(fetch, bool, GRAFANA_READY_TIMEOUT_S)
    except AssertionError as error:
        pytest.fail(f"grafana not ready: {error}\n{grafana_logs(stack)[-4000:]}")
    return client


def provisioned_file():
    return yaml.safe_load(ALERTING_FILE.read_text())


def file_rules():
    return {rule["uid"]: rule for group in provisioned_file()["groups"] for rule in group["rules"]}


def rule_query_and_threshold(rule):
    query = next(node for node in rule["data"] if node["datasourceUid"] == "prometheus")
    condition = next(node for node in rule["data"] if node["refId"] == rule["condition"])
    evaluator = condition["model"]["conditions"][0]["evaluator"]
    return query["model"]["expr"], evaluator["params"][0]


def send_logs(stack, payload):
    status, body = stack.send_otlp("otel-collector", 4318, "/v1/logs", payload)
    assert status == 200, body


def grafana_rule_states(grafana):
    groups = grafana.get("/api/prometheus/grafana/api/v1/rules")["data"]["groups"]
    return {r["uid"]: r for g in groups for r in g["rules"] if r.get("uid")}


def test_alert_rules_provisioned_as_in_file(grafana):
    loaded = {rule["uid"]: rule for rule in grafana.get("/api/v1/provisioning/alert-rules")}
    expected = file_rules()
    assert set(loaded) == set(expected)
    for uid, rule in expected.items():
        assert loaded[uid]["noDataState"] == rule["noDataState"], uid
        assert loaded[uid]["execErrState"] == rule["execErrState"], uid
        assert loaded[uid]["for"] == rule["for"], uid
        assert loaded[uid]["provenance"] == "file", uid


def test_contact_point_reads_webhook_from_secret_file(grafana):
    export = grafana.get("/api/v1/provisioning/contact-points/export?decrypt=true&format=json")
    receivers = [r for cp in export["contactPoints"] for r in cp["receivers"] if r["uid"] == "agent-slack-dm"]
    assert len(receivers) == 1
    assert receivers[0]["settings"]["url"] == DUMMY_WEBHOOK


def test_notification_policy_routes_to_slack(grafana):
    policy = grafana.get("/api/v1/provisioning/policies")
    assert policy["receiver"] == "slack-dm"
    assert policy["group_by"] == ["grafana_folder", "alertname"]


def test_template_escapes_hostile_label_values(grafana):
    template = provisioned_file()["templates"][0]
    alert = {
        "status": "firing",
        "labels": {"alertname": "Tool errors <b>", "error_type": "<!channel>&<https://evil.example|click>"},
        "annotations": {"summary": "summary <@U123>"},
        "startsAt": "2026-11-24T00:00:00Z",
    }
    rendered = grafana.request(
        "POST",
        "/api/alertmanager/grafana/config/api/v1/templates/test",
        {"name": template["name"], "template": template["template"], "alerts": [alert]},
    )
    assert not rendered.get("errors"), rendered
    outputs = {r["name"]: r["text"] for r in rendered["results"]}
    text = outputs["agent.slack.text"]
    title = outputs["agent.slack.title"]
    assert "error_type=&lt;!channel&gt;&amp;&lt;https://evil.example|click&gt;" in text
    assert "summary &lt;@U123&gt;" in text
    assert title == "[FIRING] Tool errors &lt;b&gt;"
    for output in (text, title):
        assert "<" not in output and ">" not in output


def test_dashboard_provisioned_with_variables(grafana):
    dashboard = grafana.get("/api/dashboards/uid/agent-failures")["dashboard"]
    assert dashboard["title"] == "Agent failures"
    assert {v["name"] for v in dashboard["templating"]["list"]} == {"repo", "tenant"}


def test_dashboard_queries_parse_in_prometheus(grafana):
    dashboard = grafana.get("/api/dashboards/uid/agent-failures")["dashboard"]
    exprs = [t["expr"] for p in dashboard["panels"] for t in p.get("targets", [])]
    assert exprs
    for expr in exprs:
        concrete = expr.replace("$__rate_interval", "1m").replace("$repo", ".*").replace("$tenant", ".*")
        query = urllib.parse.urlencode({"query": concrete})
        try:
            result = grafana.get(f"/api/datasources/proxy/uid/prometheus/api/v1/query?{query}")
        except urllib.error.HTTPError as error:
            pytest.fail(f"{expr}\n{error.read().decode()}")
        assert result["status"] == "success", (expr, result)


def test_single_api_error_through_collector_drives_error_rate_rule(stack, grafana):
    expr, threshold = rule_query_and_threshold(file_rules()[ERROR_RATE_RULE])
    send_logs(stack, events.api_errors(1, "grafana-rule"))
    rows = stack.query_prom(
        expr,
        until=lambda result: bool(result) and float(result[0]["value"][1]) > threshold,
        timeout=60,
    )
    assert len(rows) == 1
    assert rows[0]["metric"] == {"error_class": "http_500"}
    assert float(rows[0]["value"][1]) == 1.0
    deadline = time.monotonic() + EVALUATION_TIMEOUT_S
    while True:
        rule = grafana_rule_states(grafana)[ERROR_RATE_RULE]
        if rule["state"].lower() in ("pending", "firing") or time.monotonic() > deadline:
            break
        time.sleep(2)
    assert rule["state"].lower() in ("pending", "firing"), rule
    assert [a["labels"]["error_class"] for a in rule["alerts"]] == ["http_500"]


def test_every_golden_metric_exists_in_real_pipeline(stack):
    send_logs(stack, events.gates_on_batch())
    status, body = stack.send_otlp(
        "otel-collector",
        4318,
        "/v1/metrics",
        events.session_tokens(
            "golden", 5.0, time.time_ns(), name="claude_code.active_time.total", unit="s", attributes={"type": "cli"}
        ),
    )
    assert status == 200, body
    missing = sorted(GOLDEN_METRICS)
    deadline = time.monotonic() + 60
    while missing and time.monotonic() < deadline:
        missing = [name for name in missing if not stack.series(name)]
        if missing:
            time.sleep(2)
    assert missing == []


def test_rules_evaluate_without_query_errors(grafana):
    expected = set(file_rules())
    deadline = time.monotonic() + EVALUATION_TIMEOUT_S
    while True:
        rules = {uid: r for uid, r in grafana_rule_states(grafana).items() if uid in expected}
        evaluated = {uid: r for uid, r in rules.items() if not r["lastEvaluation"].startswith("0001-")}
        if set(evaluated) == expected or time.monotonic() > deadline:
            break
        time.sleep(2)
    assert set(evaluated) == expected, sorted(expected - set(evaluated))
    for uid, rule in evaluated.items():
        assert rule["health"] != "error", (uid, rule.get("lastError"))
        assert not rule.get("lastError"), (uid, rule.get("lastError"))


def test_no_provisioning_errors_in_logs(stack, grafana):
    lines = grafana_logs(stack).splitlines()
    provisioning = [l for l in lines if "provisioning" in l]
    assert any("alerting" in l for l in provisioning), "\n".join(lines[-50:])
    absent_plugins_dir = 'error="open /etc/grafana/provisioning/plugins: no such file or directory"'
    errors = [
        l for l in lines
        if "level=error" in l
        and ("provisioning" in l or "ngalert" in l or "alerting" in l)
        and not (l.startswith("logger=provisioning.plugins ") and l.endswith(absent_plugins_dir))
    ]
    assert not errors, "\n".join(errors)
