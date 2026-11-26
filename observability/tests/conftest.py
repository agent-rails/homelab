import copy
import json
import os
import pathlib
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import pytest
import yaml

import otlp

OBSERVABILITY_DIR = pathlib.Path(__file__).resolve().parents[1]
REAL_COMPOSE = OBSERVABILITY_DIR / "docker-compose.yaml"
REAL_PROMETHEUS = OBSERVABILITY_DIR / "prometheus.yaml"
REQUIRED_DOCKER_CONTEXT = "orbstack"
READY_TIMEOUT_S = 60
SELF_CONTAINED_JOBS = ("otel-collector", "prometheus")
DUMMY_SECRETS = {
    "smtp_host": "smtp.example.invalid:587\n",
    "smtp_user": "login@example.invalid\n",
    "smtp_from_address": "sender@example.invalid\n",
    "smtp_password": "dummy-smtp-password\n",
    "alert_email_to": "alerts@example.invalid\n",
}


def assert_docker_context():
    actual = os.environ.get("DOCKER_CONTEXT")
    assert actual == REQUIRED_DOCKER_CONTEXT, (
        f"DOCKER_CONTEXT must be {REQUIRED_DOCKER_CONTEXT!r}, got {actual!r}"
    )


@pytest.fixture(scope="session", autouse=True)
def docker_context():
    assert_docker_context()


def free_ports(count):
    sockets = []
    try:
        for _ in range(count):
            probe = socket.socket()
            probe.bind(("127.0.0.1", 0))
            sockets.append(probe)
        return [probe.getsockname()[1] for probe in sockets]
    finally:
        for probe in sockets:
            probe.close()


def compose_run(args, project=None, compose_file=None, env=None, check=True, timeout=120):
    command = ["docker", "compose"]
    if project:
        command += ["-p", project]
    if compose_file:
        command += ["-f", str(compose_file)]
    command += args
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, **(env or {})},
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"docker compose timed out after {timeout}s, pid {process.pid} left running: {' '.join(command)}")
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"{' '.join(command)} failed ({result.returncode})\n{result.stdout}\n{result.stderr}"
        )
    return result


def collector_image():
    compose = yaml.safe_load(REAL_COMPOSE.read_text())
    return compose["services"]["otel-collector"]["image"]


def validate_collector(config_files):
    command = ["docker", "run", "--rm", "-v", f"{OBSERVABILITY_DIR / 'otel-collector'}:/cfg:ro", collector_image(), "validate"]
    command += [f"--config=/cfg/{name}" for name in config_files]
    return subprocess.run(command, capture_output=True, text=True, timeout=120)


def poll(fetch, accept, timeout, interval=1.0):
    deadline = time.monotonic() + timeout
    last = None
    while True:
        try:
            last = fetch()
            if accept(last):
                return last
        except (urllib.error.URLError, ConnectionError, OSError, ValueError) as error:
            last = repr(error)
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s; last result: {last!r}")
        time.sleep(interval)


def dummy_secret_path(workdir, name):
    return workdir / f"{name}.secret"


def write_dummy_secrets(workdir, secrets):
    for name, content in secrets.items():
        path = dummy_secret_path(workdir, name)
        path.touch(mode=0o600, exist_ok=False)
        path.write_text(content)


def build_test_compose(real, services, host_ports, workdir, prometheus_tmpfs_size, secrets=DUMMY_SECRETS, patches=None):
    selected = {}
    used_secrets = set()
    for name in services:
        service = copy.deepcopy(real["services"][name])
        assert "env_file" not in service, f"{name} reads an env file the harness cannot replace"
        patch = (patches or {}).get(name, {})
        kept = [secret for secret in service.pop("secrets", []) if secret in secrets] + patch.get("secrets", [])
        if kept:
            service["secrets"] = kept
            used_secrets.update(kept)
        if "environment" in patch:
            service["environment"] = {**service.get("environment", {}), **patch["environment"]}
        if "extra_hosts" in patch:
            service["extra_hosts"] = patch["extra_hosts"]
        service.pop("depends_on", None)
        service.pop("container_name", None)
        service.pop("profiles", None)
        service["ports"] = [
            f"127.0.0.1:{host_ports[name][container]}:{container}"
            for container in host_ports.get(name, {})
        ]
        volumes = []
        for volume in service.get("volumes", []):
            source, _, rest = volume.partition(":")
            if source.startswith("${PWD}"):
                source = str(OBSERVABILITY_DIR) + source[len("${PWD}"):]
                if name == "prometheus" and source == str(REAL_PROMETHEUS):
                    source = str(workdir / "prometheus.yml")
                volumes.append(f"{source}:{rest}")
        service["volumes"] = volumes
        if name == "prometheus":
            service["tmpfs"] = [f"/prometheus:size={prometheus_tmpfs_size},uid=65534,gid=65534"]
        selected[name] = service
    document = {"services": selected}
    if used_secrets:
        document["secrets"] = {
            secret: {"file": str(dummy_secret_path(workdir, secret))} for secret in sorted(used_secrets)
        }
    return document


def build_test_prometheus(real_path, workdir):
    config = yaml.safe_load(real_path.read_text())
    config["scrape_configs"] = [
        job for job in config["scrape_configs"] if job["job_name"] in SELF_CONTAINED_JOBS
    ]
    target = workdir / "prometheus.yml"
    target.write_text(yaml.safe_dump(config))
    return target


class Stack:
    def __init__(self, services, container_ports, prometheus_tmpfs_size="200m", env=None, secrets=None, patches=None):
        self.services = services
        self.secrets = {**DUMMY_SECRETS, **(secrets or {})}
        self.patches = patches or {}
        self.project = f"obstest-{uuid.uuid4().hex[:10]}"
        self.env = env or {}
        self.prometheus_tmpfs_size = prometheus_tmpfs_size
        self.container_ports = container_ports
        allocated = free_ports(sum(len(ports) for ports in container_ports.values()))
        self.host_ports = {}
        for service, ports in container_ports.items():
            self.host_ports[service] = {port: allocated.pop() for port in ports}
        self.workdir = pathlib.Path(tempfile.mkdtemp(prefix=self.project))
        self.compose_file = self.workdir / "compose.yaml"

    def render(self):
        real = yaml.safe_load(REAL_COMPOSE.read_text())
        build_test_prometheus(REAL_PROMETHEUS, self.workdir)
        write_dummy_secrets(self.workdir, self.secrets)
        document = build_test_compose(
            real, self.services, self.host_ports, self.workdir, self.prometheus_tmpfs_size, self.secrets, self.patches
        )
        self.compose_file.write_text(yaml.safe_dump(document))

    def compose(self, args, **kwargs):
        return compose_run(
            args, project=self.project, compose_file=self.compose_file, env=self.env, **kwargs
        )

    def up(self):
        self.render()
        self.compose(["up", "-d", "--wait", "--wait-timeout", str(READY_TIMEOUT_S), *self.services])

    def down(self):
        if self.compose_file.exists():
            self.compose(["down", "-v", "--remove-orphans", "-t", "1"], check=False)

    def port(self, service, container_port):
        return self.host_ports[service][container_port]

    def url(self, service, container_port, path=""):
        return f"http://127.0.0.1:{self.port(service, container_port)}{path}"

    def get(self, url, timeout=10):
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read().decode()

    def wait_ready(self):
        if "otel-collector" in self.services:
            poll(
                lambda: self.get(self.url("otel-collector", 13133, "/")),
                lambda body: "Server available" in body,
                READY_TIMEOUT_S,
            )
        for service, port in (("tempo", 3200), ("loki", 3100)):
            if service in self.services:
                poll(
                    lambda: self.get(self.url(service, port, "/ready")),
                    lambda body: "ready" in body.lower(),
                    READY_TIMEOUT_S * 2,
                    interval=2.0,
                )
        if "prometheus" in self.services:
            poll(
                lambda: self.get(self.url("prometheus", 9090, "/-/ready")),
                lambda body: "Ready" in body,
                READY_TIMEOUT_S,
            )

    def send_otlp(self, service, container_port, path, payload):
        return otlp.post_json(self.url(service, container_port, path), payload)

    def query_prom(self, expr, until=None, timeout=30):
        accept = until if until is not None else (lambda result: len(result) > 0)

        def fetch():
            query = urllib.parse.urlencode({"query": expr})
            body = json.loads(self.get(self.url("prometheus", 9090, f"/api/v1/query?{query}")))
            assert body["status"] == "success", body
            return body["data"]["result"]

        return poll(fetch, accept, timeout)

    def series(self, match):
        query = urllib.parse.urlencode({"match[]": match})
        body = json.loads(self.get(self.url("prometheus", 9090, f"/api/v1/series?{query}")))
        assert body["status"] == "success", body
        return body["data"]

    def scrape(self, service, container_port):
        return self.get(self.url(service, container_port, "/metrics"))

    def query_prom_now(self, expr):
        query = urllib.parse.urlencode({"query": expr})
        body = json.loads(self.get(self.url("prometheus", 9090, f"/api/v1/query?{query}")))
        assert body["status"] == "success", body
        return body["data"]["result"]


@pytest.fixture(scope="module")
def stack(request):
    spec = getattr(request.module, "STACK")
    instance = Stack(
        services=spec["services"],
        container_ports=spec["ports"],
        prometheus_tmpfs_size=spec.get("prometheus_tmpfs_size", "200m"),
        env=spec.get("env"),
    )
    try:
        instance.up()
        instance.wait_ready()
        yield instance
    finally:
        instance.down()
