#!/bin/sh
set -eu

expected_version=v2.11.4
expected_platform=Darwin/arm64
expected_sha256=07765bfa5cc2b60d3b481c787c2e9d003d5e05e688dffc23ababe5330db18c1b

repo_dir=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
caddyfile=$repo_dir/gateway/Caddyfile
gateway_runner=$repo_dir/scripts/run-ai-host-gateway.sh
caddy_bin=$(command -v caddy 2>/dev/null || true)

if [ -z "$caddy_bin" ]; then
	echo "caddy is required" >&2
	exit 1
fi
platform=$(uname -s)/$(uname -m)
if [ "$platform" != "$expected_platform" ]; then
	echo "no reviewed Caddy pin for $platform" >&2
	exit 1
fi
actual_version=$("$caddy_bin" version | awk '{print $1}')
if [ "$actual_version" != "$expected_version" ]; then
	echo "refusing Caddy $actual_version; expected $expected_version" >&2
	exit 1
fi
actual_sha256=$(shasum -a 256 "$caddy_bin" | awk '{print $1}')
if [ "$actual_sha256" != "$expected_sha256" ]; then
	echo "refusing unreviewed Caddy binary SHA-256 $actual_sha256" >&2
	exit 1
fi
if ! curl -fsS --max-time 3 http://127.0.0.1:11434/api/version >/dev/null; then
	echo "Ollama must be healthy on 127.0.0.1:11434" >&2
	exit 1
fi
if lsof -nP -iTCP:19443 -sTCP:LISTEN >/dev/null 2>&1; then
	echo "shadow port 19443 is already in use" >&2
	exit 1
fi

test_dir=$(mktemp -d "${TMPDIR:-/tmp}/blockops-ai-gateway.XXXXXX")
caddy_pid=
cleanup() {
	if [ -n "$caddy_pid" ]; then
		kill "$caddy_pid" 2>/dev/null || true
		wait "$caddy_pid" 2>/dev/null || true
	fi
	rm -rf "$test_dir"
}
trap cleanup 0 1 2 15

umask 077
ollama_token=$(openssl rand -hex 32)
mlx_token=$(openssl rand -hex 32)
ollama_token_file=$test_dir/ollama.token
mlx_token_file=$test_dir/mlx.token
printf '%s\n' "$ollama_token" >"$ollama_token_file"
printf '%s\n' "$mlx_token" >"$mlx_token_file"
chmod 600 "$ollama_token_file" "$mlx_token_file"
export XDG_DATA_HOME=$test_dir/data
export XDG_CONFIG_HOME=$test_dir/config

assert_no_listeners() {
	! lsof -nP -iTCP:19443 -sTCP:LISTEN >/dev/null 2>&1
	! lsof -nP -iTCP:19444 -sTCP:LISTEN >/dev/null 2>&1
}
expect_refusal() {
	if "$gateway_runner" "$@" >"$test_dir/refusal.log" 2>&1; then
		echo "invalid gateway credentials were accepted" >&2
		exit 1
	fi
	assert_no_listeners
}

empty_token_file=$test_dir/empty.token
malformed_token_file=$test_dir/malformed.token
identical_token_file=$test_dir/identical.token
: >"$empty_token_file"
printf '%064d\n' 0 | tr '0' 'G' >"$malformed_token_file"
cp "$ollama_token_file" "$identical_token_file"
chmod 600 "$empty_token_file" "$malformed_token_file" "$identical_token_file"
expect_refusal "$caddyfile" "$test_dir/missing.token" "$mlx_token_file"
expect_refusal "$caddyfile" "$empty_token_file" "$mlx_token_file"
expect_refusal "$caddyfile" "$malformed_token_file" "$mlx_token_file"
expect_refusal "$caddyfile" "$ollama_token_file" "$identical_token_file"

"$gateway_runner" "$caddyfile" "$ollama_token_file" "$mlx_token_file" >"$test_dir/caddy.log" 2>&1 &
caddy_pid=$!

ca_cert=$XDG_DATA_HOME/caddy/pki/authorities/local/root.crt
attempt=0
while [ "$attempt" -lt 30 ]; do
	if [ -s "$ca_cert" ] && curl -fsS --cacert "$ca_cert" --max-time 1 \
		-H "Authorization: Bearer $ollama_token" \
		https://localhost:19443/api/version >/dev/null 2>&1; then
		break
	fi
	attempt=$((attempt + 1))
	sleep 1
done
if [ "$attempt" -eq 30 ]; then
	echo "shadow gateway did not become ready" >&2
	tail -50 "$test_dir/caddy.log" >&2
	exit 1
fi

status=$(curl -sS --cacert "$ca_cert" -o /dev/null -w '%{http_code}' https://localhost:19443/api/version)
test "$status" = 401
status=$(curl -sS --cacert "$ca_cert" -o /dev/null -w '%{http_code}' \
	-H 'Authorization: Bearer wrong' https://localhost:19443/api/version)
test "$status" = 401
status=$(curl -sS --cacert "$ca_cert" -o /dev/null -w '%{http_code}' \
	-H "Authorization: Bearer $ollama_token" https://localhost:19443/api/version)
test "$status" = 200

for forbidden_path in /api/pull /api/push /api/delete /api/create /api/copy /unknown; do
	status=$(curl -sS --cacert "$ca_cert" -o /dev/null -w '%{http_code}' \
		-H "Authorization: Bearer $ollama_token" \
		-X POST "https://localhost:19443$forbidden_path")
	test "$status" = 404
done

status=$(curl -sS --cacert "$ca_cert" -o /dev/null -w '%{http_code}' https://localhost:19444/health)
test "$status" = 401
status=$(curl -sS --cacert "$ca_cert" -o /dev/null -w '%{http_code}' \
	-H 'Authorization: Bearer wrong' https://localhost:19444/health)
test "$status" = 401
status=$(curl -sS --cacert "$ca_cert" -o /dev/null -w '%{http_code}' \
	-H "Authorization: Bearer $mlx_token" https://localhost:19444/health)
test "$status" != 401
test "$status" != 404

if grep -F "$ollama_token" "$test_dir/caddy.log" >/dev/null || \
	grep -F "$mlx_token" "$test_dir/caddy.log" >/dev/null; then
	echo "gateway token appeared in Caddy logs" >&2
	exit 1
fi

echo "PASS: credential fail-closed, TLS, separate auth, Ollama allowlist, mutation denial, and log redaction"
