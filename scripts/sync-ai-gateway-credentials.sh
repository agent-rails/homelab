#!/bin/sh
set -eu

namespace=ai-infra
state_dir=${AI_GATEWAY_STATE_DIR:-$HOME/.config/ai-gateway}
ca_cert=$state_dir/data/caddy/pki/authorities/local/root.crt

for required in "$state_dir/ollama.token" "$state_dir/mlx.token" "$ca_cert"; do
	if [ ! -s "$required" ]; then
		echo "missing $required; run scripts/install-ai-host-gateway.sh first" >&2
		exit 1
	fi
done

work_dir=$(mktemp -d "${TMPDIR:-/tmp}/ai-gateway-sync.XXXXXX")
trap 'rm -rf "$work_dir"' 0
trap 'exit 130' 1 2 15
umask 077
tr -d '\n' <"$state_dir/ollama.token" >"$work_dir/OLLAMA_GATEWAY_TOKEN"
tr -d '\n' <"$state_dir/mlx.token" >"$work_dir/MLX_GATEWAY_TOKEN"

kubectl -n "$namespace" create secret generic ai-gateway-upstream \
	--from-file=OLLAMA_GATEWAY_TOKEN="$work_dir/OLLAMA_GATEWAY_TOKEN" \
	--from-file=MLX_GATEWAY_TOKEN="$work_dir/MLX_GATEWAY_TOKEN" \
	--dry-run=client -o yaml | kubectl apply --server-side --field-manager=ai-gateway-sync -f -
kubectl -n "$namespace" annotate secret ai-gateway-upstream kubectl.kubernetes.io/last-applied-configuration- >/dev/null
kubectl -n "$namespace" create configmap ai-gateway-ca \
	--from-file=ca.crt="$ca_cert" \
	--dry-run=client -o yaml | kubectl apply --server-side --field-manager=ai-gateway-sync -f -
