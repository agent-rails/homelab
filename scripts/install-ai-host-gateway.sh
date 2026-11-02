#!/bin/sh
set -eu

label=dev.agent-rails.ai-gateway
repo_dir=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
state_dir=${AI_GATEWAY_STATE_DIR:-$HOME/.config/ai-gateway}
plist=$HOME/Library/LaunchAgents/$label.plist
domain=gui/$(id -u)

umask 077
mkdir -p "$state_dir" "$state_dir/data" "$state_dir/config"
chmod 700 "$state_dir"

for name in ollama mlx; do
	token_file=$state_dir/$name.token
	if [ ! -e "$token_file" ]; then
		openssl rand -hex 32 >"$token_file"
	fi
	chmod 600 "$token_file"
done

cat >"$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>$label</string>
	<key>ProgramArguments</key>
	<array>
		<string>/bin/sh</string>
		<string>$repo_dir/scripts/run-ai-host-gateway.sh</string>
		<string>$repo_dir/gateway/Caddyfile</string>
		<string>$state_dir/ollama.token</string>
		<string>$state_dir/mlx.token</string>
	</array>
	<key>EnvironmentVariables</key>
	<dict>
		<key>PATH</key>
		<string>/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
		<key>AI_GATEWAY_HOST</key>
		<string>host.orb.internal</string>
		<key>XDG_DATA_HOME</key>
		<string>$state_dir/data</string>
		<key>XDG_CONFIG_HOME</key>
		<string>$state_dir/config</string>
	</dict>
	<key>RunAtLoad</key>
	<true/>
	<key>KeepAlive</key>
	<true/>
	<key>StandardOutPath</key>
	<string>$state_dir/gateway.log</string>
	<key>StandardErrorPath</key>
	<string>$state_dir/gateway.log</string>
</dict>
</plist>
EOF
chmod 600 "$plist"

launchctl bootout "$domain/$label" 2>/dev/null || true
launchctl bootstrap "$domain" "$plist"

ca_cert=$state_dir/data/caddy/pki/authorities/local/root.crt
attempt=0
until [ -s "$ca_cert" ] && curl -fsS --cacert "$ca_cert" --max-time 2 -o /dev/null \
	--resolve host.orb.internal:19443:127.0.0.1 \
	-H "Authorization: Bearer $(cat "$state_dir/ollama.token")" \
	https://host.orb.internal:19443/api/version 2>/dev/null; do
	attempt=$((attempt + 1))
	if [ "$attempt" -ge 30 ]; then
		echo "gateway did not become ready; see $state_dir/gateway.log" >&2
		exit 1
	fi
	sleep 1
done

echo "gateway running as $domain/$label"
echo "CA certificate: $ca_cert"
