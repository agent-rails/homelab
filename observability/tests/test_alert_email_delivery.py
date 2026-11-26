import email
import email.policy
import json
import re
import tempfile
import time
import urllib.error

import pytest

import conftest
import smtp_sink
from test_grafana_provisioning_loads import GrafanaClient, grafana_logs

pytestmark = pytest.mark.integration

GRAFANA_READY_TIMEOUT_S = 90
ALERTMANAGER_SYNC_TIMEOUT_S = 120
TEMPLATES_LOADED = re.compile(r'msg="template definitions loaded" mimir=0 grafana=[1-9]')
DELIVERY_TIMEOUT_S = 30
RECIPIENT = "owner@example.invalid"
SENDER = "grafana@example.invalid"
LOGIN = "grafana-login"
PASSWORD = "sink-password"
CA_SECRET = "test_smtp_ca"
HOSTILE_ALERTNAME = "Tool errors <img src=x onerror=alert(1)>"
HOSTILE_ERROR_TYPE = "<script>alert(1)</script>"
HOSTILE_SUMMARY = 'summary " & <a href="javascript:alert(1)">x</a>'


@pytest.fixture(scope="module")
def sink():
    workdir = tempfile.mkdtemp(prefix="obstest-smtp-")
    certfile, keyfile = smtp_sink.write_self_signed_cert(workdir)
    instance = smtp_sink.SmtpSink(certfile, keyfile)
    instance.start()
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture(scope="module")
def stack(sink):
    instance = conftest.Stack(
        services=["grafana"],
        container_ports={"grafana": [3000]},
        secrets={
            "smtp_host": f"{smtp_sink.SINK_HOSTNAME}:{sink.port}\n",
            "smtp_user": f"{LOGIN}\n",
            "smtp_from_address": f"{SENDER}\n",
            "smtp_password": f"{PASSWORD}\n",
            "alert_email_to": f"{RECIPIENT}\n",
            CA_SECRET: sink.certfile.read_text(),
        },
        patches={
            "grafana": {
                "secrets": [CA_SECRET],
                "environment": {"SSL_CERT_FILE": f"/run/secrets/{CA_SECRET}"},
                "extra_hosts": [f"{smtp_sink.SINK_HOSTNAME}:host-gateway"],
            }
        },
    )
    try:
        instance.up()
        yield instance
    finally:
        instance.down()


@pytest.fixture(scope="module")
def grafana(stack):
    client = GrafanaClient(stack.url("grafana", 3000))

    def fetch():
        if client.get("/api/health").get("database") != "ok":
            return False
        return bool(email_receiver(client))

    def templates_loaded():
        return TEMPLATES_LOADED.search(grafana_logs(stack))

    try:
        conftest.poll(fetch, bool, GRAFANA_READY_TIMEOUT_S)
        conftest.poll(templates_loaded, bool, ALERTMANAGER_SYNC_TIMEOUT_S)
    except AssertionError as error:
        pytest.fail(f"grafana not ready: {error}\n{grafana_logs(stack)[-4000:]}")
    return client


RECEIVERS_API = "/apis/notifications.alerting.grafana.app/v1beta1/namespaces/default/receivers"


def email_receiver(grafana):
    items = grafana.get(RECEIVERS_API)["items"]
    return next((r for r in items if r["spec"]["title"] == "email-owner"), None)


def send_test_notification(grafana):
    receiver = email_receiver(grafana)
    integrations = [i for i in receiver["spec"]["integrations"] if i.get("uid") == "agent-email"]
    assert len(integrations) == 1, receiver
    body = {
        "integration": integrations[0],
        "alert": {
            "labels": {"alertname": HOSTILE_ALERTNAME, "error_type": HOSTILE_ERROR_TYPE},
            "annotations": {"summary": HOSTILE_SUMMARY},
        },
    }
    path = f"{RECEIVERS_API}/{receiver['metadata']['name']}/test"
    try:
        result = grafana.request("POST", path, body)
    except urllib.error.HTTPError as error:
        result = {"http_status": error.code, **json.loads(error.read())}
    assert "status" in result, result
    return result


def wait_for_message(sink):
    deadline = time.monotonic() + DELIVERY_TIMEOUT_S
    while not sink.messages and time.monotonic() < deadline:
        time.sleep(0.5)
    assert sink.messages, [s.commands for s in sink.sessions]
    return sink.messages[0]


def test_alert_email_delivered_over_starttls_with_values_escaped_once(sink, grafana):
    sink.reset(starttls=True)
    result = send_test_notification(grafana)
    assert result["status"] == "success", result
    session = wait_for_message(sink)
    assert session.tls is True
    assert session.auth == [b"", LOGIN.encode(), PASSWORD.encode()]
    assert session.mail_from == SENDER
    assert session.rcpt_to == [RECIPIENT]
    message = email.message_from_bytes(session.data, policy=email.policy.default)
    assert message["To"] == RECIPIENT
    assert message["Subject"] == f"[FIRING] {HOSTILE_ALERTNAME}"
    html = message.get_body(("html",)).get_content()
    assert "<script" not in html and "<img src=x" not in html and 'href="javascript' not in html
    body = html[html.index("</head>"):]
    assert "firing: error_type=&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "summary &#34; &amp; &lt;a href=&#34;javascript:alert(1)&#34;&gt;x&lt;/a&gt;" in body
    assert "&amp;lt;" not in body and "&amp;amp;" not in body and "&amp;#34;" not in body


def test_alert_email_refused_without_starttls(sink, grafana):
    sink.reset(starttls=False)
    result = send_test_notification(grafana)
    assert result["status"] == "failure", result
    assert "STARTTLS" in result["error"], result
    assert sink.sessions, "grafana never connected to the sink"
    assert all(not s.tls and s.auth is None and s.mail_from is None for s in sink.sessions)
    assert sink.messages == []
