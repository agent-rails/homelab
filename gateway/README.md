# Native inference gateway

This is the live gateway on the request path documented in
`../AI_LAB_SECURITY.md`:

```text
consumer -> LiteLLM -> Caddy (loopback-bound) -> Ollama or MLX on loopback
```

Caddy terminates TLS, requires a separate bearer token for each backend,
allowlists inference and health paths, removes the incoming `Authorization`
header before proxying, and returns `404` for every other authenticated path.
Access logging is not enabled. The two upstream credentials must be independent
of LiteLLM's master and consumer virtual keys.

Always start Caddy through `scripts/run-ai-host-gateway.sh`. The launcher reads
the credentials from separate owner-only, mode-600, non-symlink files and
refuses missing, malformed, identical, or weak values before Caddy can bind a
listener. It also verifies the reviewed Caddy version and executable digest.

## Shadow validation

Install the reviewed Homebrew Caddy build, but do not start its service:

```bash
brew install caddy
scripts/test-ai-host-gateway.sh
```

The script pins Caddy 2.11.4 and its Darwin/arm64 executable SHA-256, creates
throwaway credentials and a private Caddy data directory, and listens only on
high localhost ports. It proves missing, empty, malformed and identical token
files cannot create either listener. It then validates TLS, correct/missing/wrong
credentials on both independently authenticated listeners, Ollama health
forwarding, mutation-route denial, default denial, and that neither token is
present in Caddy's process log. It does not modify
Ollama, LiteLLM, Kubernetes, Homebrew services, or production ports.

## Persistent gateway

The live gateway runs from a private state directory, `~/.config/ai-gateway`
(mode 700). It holds both upstream tokens (mode 600), Caddy's data directory
with the internal CA, and `gateway.log`.

```bash
scripts/install-ai-host-gateway.sh       # creates tokens once, runs Caddy as a launchd user agent
scripts/sync-ai-gateway-credentials.sh   # copies tokens and CA into ai-infra Secret/ConfigMap
```

The install script creates missing tokens but never replaces existing ones, and
it reuses the existing CA, so re-running it does not invalidate the cluster copy.
It sets `AI_GATEWAY_HOST=host.orb.internal`, so the certificate name matches what
pods dial. From the Mac itself, use
`curl --resolve host.orb.internal:19443:127.0.0.1` because that name does not
resolve on the host.

Consumers:

- LiteLLM reads `OLLAMA_GATEWAY_TOKEN` and `MLX_GATEWAY_TOKEN` from the
  `ai-gateway-upstream` Secret, and trusts the CA from the `ai-gateway-ca`
  ConfigMap through `SSL_VERIFY`.
- Prometheus mounts the MLX token and CA as compose secrets.

Stop any session-started gateway before installing: the install script refuses
when 19443 is already listening, and Caddy's `SO_REUSEPORT` would otherwise let
two instances share the port.

Stop the gateway:

```bash
launchctl bootout gui/$(id -u)/dev.agent-rails.ai-gateway
```

## Cutover rollback

Restore the pre-cutover consumer path from commit `3fef1db`:

```bash
for f in litellm-configmap litellm-deployment ollama-service vllm-service; do
  git show 3fef1db:k3s/ai-infra/$f.yaml | kubectl apply -f -
done
kubectl -n ai-infra rollout status deploy/litellm-proxy
git show 3fef1db:k3s/ai-infra/open-webui-values.yaml > /tmp/open-webui-values.yaml
helm upgrade open-webui open-webui/open-webui --version 16.0.0 -n ai-infra -f /tmp/open-webui-values.yaml
git checkout 3fef1db -- observability/prometheus.yaml observability/docker-compose.yaml
(cd observability && docker compose up -d prometheus)
```

Then restart the `:4000` port-forward.

## Known limit

OrbStack forwards host loopback to every container and pod, and its
Kubernetes does not enforce NetworkPolicy, so raw Ollama and vLLM remain
reachable from local workloads. See `../AI_LAB_SECURITY.md`.
