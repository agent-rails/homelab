#!/bin/sh
set -eu

expected_version=v2.11.4
expected_platform=Darwin/arm64
expected_sha256=07765bfa5cc2b60d3b481c787c2e9d003d5e05e688dffc23ababe5330db18c1b

if [ "$#" -ne 3 ]; then
	echo "usage: $0 CADDYFILE OLLAMA_TOKEN_FILE MLX_TOKEN_FILE" >&2
	exit 2
fi

caddyfile=$1
ollama_token_file=$2
mlx_token_file=$3
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
if [ ! -f "$caddyfile" ] || [ -L "$caddyfile" ]; then
	echo "Caddyfile must be a regular, non-symlink file" >&2
	exit 1
fi

read_token() {
	token_file=$1
	if [ ! -f "$token_file" ] || [ -L "$token_file" ]; then
		echo "token file must be a regular, non-symlink file: $token_file" >&2
		return 1
	fi
	owner=$(stat -f '%Su' "$token_file")
	mode=$(stat -f '%Lp' "$token_file")
	if [ "$owner" != "$(id -un)" ] || [ "$mode" != 600 ]; then
		echo "token file must be owned by $(id -un) with mode 600: $token_file" >&2
		return 1
	fi
	bytes=$(wc -c <"$token_file" | tr -d ' ')
	if [ "$bytes" != 65 ]; then
		echo "token file must contain exactly 64 lowercase hex characters and a newline: $token_file" >&2
		return 1
	fi
	IFS= read -r token <"$token_file"
	case "$token" in
		????????????????????????????????????????????????????????????????)
			case "$token" in *[!0-9a-f]*) return 1 ;; esac
			;;
		*) return 1 ;;
	esac
	printf '%s' "$token"
}

OLLAMA_GATEWAY_TOKEN=$(read_token "$ollama_token_file") || {
	echo "invalid Ollama gateway token" >&2
	exit 1
}
MLX_GATEWAY_TOKEN=$(read_token "$mlx_token_file") || {
	echo "invalid MLX gateway token" >&2
	exit 1
}
if [ "$OLLAMA_GATEWAY_TOKEN" = "$MLX_GATEWAY_TOKEN" ]; then
	echo "Ollama and MLX gateway tokens must be different" >&2
	exit 1
fi
export OLLAMA_GATEWAY_TOKEN MLX_GATEWAY_TOKEN

"$caddy_bin" validate --config "$caddyfile" --adapter caddyfile >/dev/null
exec "$caddy_bin" run --config "$caddyfile" --adapter caddyfile
