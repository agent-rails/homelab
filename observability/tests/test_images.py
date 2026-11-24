import base64
import json
import re

import pytest
import yaml

import conftest

STACK = {
    "services": ["prometheus", "grafana"],
    "ports": {"prometheus": [9090], "grafana": [3000]},
}
DIGEST_PINNED = re.compile(r"^[a-z0-9./_-]+:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$")


@pytest.mark.config
def test_every_image_is_pinned_by_tag_and_digest():
    compose = yaml.safe_load(conftest.REAL_COMPOSE.read_text())
    unpinned = {
        name: service["image"]
        for name, service in compose["services"].items()
        if not DIGEST_PINNED.match(service["image"])
    }
    assert unpinned == {}


@pytest.mark.integration
def test_existing_dashboard_provisions_on_pinned_images(stack):
    token = base64.b64encode(b"admin:admin").decode()

    def fetch():
        import urllib.request

        request = urllib.request.Request(
            stack.url("grafana", 3000, "/api/search?query=Claude"),
            headers={"Authorization": f"Basic {token}"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    found = conftest.poll(fetch, lambda result: len(result) > 0, 60)
    assert [item["uid"] for item in found] == ["claude-code-usage"]
