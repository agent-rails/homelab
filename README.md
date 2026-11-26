# homelab

A single-machine agentic lab on macOS: agents (Hermes, OpenClaw), a team chat relay
they talk over (Buzz), local models (Ollama, vLLM-metal) behind an authenticated
gateway, LiteLLM as the one model endpoint, Open WebUI as a browser front end, and
Prometheus and Grafana for model-serving and Claude Code usage metrics.

It is a reference repository, not a deployable product. It records the real
integration points, and the real bugs hit wiring them together, in `DECISIONS.md`.

## What runs where

| Component | Runs as | Reached at | Notes |
|---|---|---|---|
| Ollama | native macOS process | `http://localhost:11434` (raw) | Consumers go through the gateway |
| vLLM-metal | native macOS process | `http://localhost:8000` (raw) | OpenAI-compatible `/v1` |
| Caddy gateway | native macOS process | `https://host.orb.internal:19443` (Ollama), `:19444` (vLLM) | TLS only, bound to loopback, bearer-token auth; see `gateway/README.md` |
| LiteLLM proxy | k3s, `ai-infra` | `http://localhost:4000/v1` via port-forward | The single model endpoint; aliases `ollama-default`, `vllm-default`, `gpt-oss-default` |
| Open WebUI | k3s, `ai-infra` | `http://localhost:3080` via port-forward | Browser front end over LiteLLM |
| Hermes | k3s, `ai-infra`, and a native CLI | — | Agent runtime; model `gpt-oss-default` through LiteLLM |
| Buzz relay | k3s, `buzz` | `ws://localhost:3939` via port-forward to service port 3000 | Inter-agent chat channel |
| OpenClaw gateway | native macOS process | `http://localhost:18789` | Agent gateway with a Buzz plugin |
| Prometheus | Docker Compose | `http://127.0.0.1:9090` | Scrapes LiteLLM and vLLM; receives Claude Code metrics over OTLP |
| Grafana | Docker Compose | `http://127.0.0.1:3000` | Provisioned datasource and Claude Code dashboard |

`kubectl port-forward` processes do not survive a laptop sleep; restart them after
waking.

```
                     OrbStack k3s cluster
   ┌─────────────────────────────────────────────────────┐
   │  ai-infra ns                                         │
   │    open-webui ──► litellm-proxy                      │
   │    litellm-proxy ──► https://host.orb.internal:19443  │
   │                      https://host.orb.internal:19444  │
   │    hermes ──► litellm-proxy (gpt-oss-default)         │
   │                                                       │
   │  buzz ns                                             │
   │    buzz (relay: WS + REST + NIP-11)                   │
   │    buzz-postgresql / buzz-redis / buzz-minio          │
   └─────────────────────────────────────────────────────┘
              ▲                        ▲
              │ ws://localhost:3939    │ native macOS processes
   ┌──────────┴──────────┐   ┌─────────┴───────────┐
   │ Hermes CLI           │   │ Caddy gateway        │
   │ OpenClaw gateway     │   │  ──► Ollama :11434   │
   │ (each a Buzz identity)│  │  ──► vLLM-metal :8000│
   └──────────────────────┘   └──────────────────────┘
```

## Repository layout

| Path | Contents |
|---|---|
| `gateway/`, `scripts/` | Caddy gateway config, launcher, launchd install, cluster credential sync, gateway test |
| `k3s/ai-infra/` | LiteLLM, its Postgres, and Open WebUI values |
| `k3s/hermes/` | Hermes deployment, ConfigMap and PVC |
| `k3s/buzz/` | Buzz relay helm values (chart `buzz-0.1.7`) |
| `hermes/` | Hermes config and `.env` examples |
| `openclaw/` | OpenClaw Buzz channel config example, the `llmfit-advisor` skill, and the opt-in agent-guard gate (`openclaw/agent-guard/`) |
| `observability/` | Prometheus and Grafana (Docker Compose), the Claude Code usage dashboard, and vLLM and LiteLLM dashboards |
| `eval/` | Regression harness comparing two OpenAI-compatible endpoints |
| `DESIGN.md` | Architecture, threat model and rollout plan for LiteLLM fronting the local models |
| `AI_LAB_SECURITY.md` | The enforced-gateway target, cutover acceptance tests and evidence |
| `DECISIONS.md` | Footguns hit and how each was resolved |

## Bring-up order

1. Native gateway: `scripts/install-ai-host-gateway.sh`, then
   `scripts/sync-ai-gateway-credentials.sh` (see `gateway/README.md`).
2. LiteLLM (see below), then Open WebUI:
   `helm install open-webui open-webui/open-webui -n ai-infra -f k3s/ai-infra/open-webui-values.yaml`.
   Open WebUI needs the `open-webui-openai-keys` Secret holding
   `<pipelines key>;<open-webui LiteLLM virtual key>` under key `keys`.
3. Buzz relay: `helm install buzz buzz/buzz -n buzz -f k3s/buzz/values.yaml`.
4. Hermes in the cluster, once LiteLLM is up (`k3s/hermes/`).
5. The Hermes CLI and the OpenClaw gateway as native processes, each with its own
   Buzz identity (`hermes/.env.example`, `openclaw/buzz-channel-config.example.json`).

Expose the relay to both native processes with
`kubectl port-forward -n buzz svc/buzz 3939:3000`. **Use port 3000: the service's
8080 is a health probe only and returns 404 for every real API call.**

## Model endpoint: LiteLLM

### Phase 1: one endpoint

```
kubectl apply -f k3s/ai-infra/litellm-configmap.yaml -f k3s/ai-infra/litellm-deployment.yaml -f k3s/ai-infra/litellm-service.yaml
kubectl port-forward -n ai-infra svc/litellm-proxy 4000:4000
```

Create the `litellm-secrets` Secret first (`k3s/ai-infra/litellm-secret.yaml.example`).
**Both `LITELLM_MASTER_KEY` and `LITELLM_SALT_KEY` must be `openssl rand -base64 32`
values, not hex**, or LiteLLM's key-cache decryption fails with a misleading
`No connected db.` error. The `ai-gateway-upstream` Secret and `ai-gateway-ca`
ConfigMap must also exist (created by the credential sync script).

The config is mounted with `subPath`, so a running pod never sees ConfigMap changes.
After editing it:

```
kubectl apply -f k3s/ai-infra/litellm-configmap.yaml
kubectl -n ai-infra rollout restart deploy/litellm-proxy
kubectl -n ai-infra rollout status deploy/litellm-proxy
kubectl port-forward -n ai-infra svc/litellm-proxy 4000:4000
```

Hermes uses it natively (`~/.hermes/config.yaml` `base_url` plus a `key_env`).
OpenClaw uses its bundled `vllm` provider (OpenAI-compatible, with real
`VLLM_API_KEY` wiring) pointed at `http://127.0.0.1:4000/v1`, not a hand-written
custom provider, which silently sends an empty API key (see `DECISIONS.md`).

### Phase 2: Postgres and per-consumer keys

```
kubectl apply -f k3s/ai-infra/litellm-postgres-pvc.yaml \
  -f k3s/ai-infra/litellm-postgres-service.yaml \
  -f k3s/ai-infra/litellm-postgres-deployment.yaml
```

Create `litellm-postgres-secrets` first (`k3s/ai-infra/litellm-postgres-secret.yaml.example`).
It is a dedicated Postgres, deliberately not shared with `buzz-postgresql`
(`DESIGN.md` §4). Add `DATABASE_URL` to `litellm-secrets` and re-apply
`litellm-deployment.yaml`. The proxy runs `prisma migrate deploy` on boot; the
`startupProbe` and the 2Gi memory limit let the migration finish before liveness
kills it and without running out of memory.

Mint a virtual key per consumer, scoped to the models it needs:

```bash
MK=$(kubectl get secret litellm-secrets -n ai-infra -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
curl -s http://localhost:4000/key/generate -H "Authorization: Bearer $MK" \
  -H "Content-Type: application/json" \
  -d '{"key_alias":"openclaw","models":["ollama-default"]}'
```

| Consumer | Models | Where its key lives |
|---|---|---|
| Hermes (native and in-cluster) | `gpt-oss-default` | `~/.hermes/.env`; in-cluster `secretKeyRef` to `litellm-secrets` key `LITELLM_VIRTUAL_KEY_HERMES` |
| OpenClaw | `ollama-default` | `~/.openclaw/.env` as `VLLM_API_KEY` (loaded on every `openclaw gateway run`) |
| Eval harness | all three | passed as `--baseline-api-key` and `--candidate-api-key` |

The master key is kept only for `/key/generate` and `/key/block` and never sits in a
consumer's config. If Postgres fails, point the proxy back at master-key auth; every
consumer already targets the proxy.

## agent-guard tool gate (opt-in)

```
openclaw config set plugins.load.paths '["<path to this repo>/openclaw/agent-guard"]' --json
openclaw config set plugins.entries.agent-guard-gate.enabled true
openclaw gateway restart
openclaw plugins inspect agent-guard-gate --runtime --json   # expect typedHooks[].name = before_tool_call
```

Routes every OpenClaw tool call through [agent-guard](https://github.com/agent-rails/agent-guard)
before dispatch, through the `before_tool_call` hook, the only seam that reaches
OpenClaw's builtin tools (`exec`, `write`, ...); the `guard mcp` wrapper covers only
external MCP servers. Default deny, read-only tools allowed, mutating tools gated by a
person, destructive shapes refused, every decision audited. Needs agent-guard's
`guard` CLI on the gateway's path (or `AGENT_GUARD_BIN`). It is not wired into the live
OpenClaw config; see `openclaw/agent-guard/README.md` for coverage limits.

## Observability

```
cd observability && docker compose up -d
```

Prometheus and Grafana listen on `127.0.0.1` only and keep their data in named
volumes. Grafana (default `admin`/`admin`) provisions the Prometheus datasource
(uid `prometheus`) and the Claude Code dashboard itself.

### Alert email

Grafana emails alerts over SMTP with mandatory STARTTLS and certificate checks.
`docker compose up` fails to create Grafana until these files exist in
`~/.config/observability/`, each with mode `0600` and none committed:

- `smtp.env`: `GF_SMTP_HOST=<host>:587`, `GF_SMTP_USER=<login>` and
  `GF_SMTP_FROM_ADDRESS=<sender>`, one per line. Do not put the password or TLS
  settings here; Compose sets those and Grafana exits if the password is set twice.
- `smtp-password`: the SMTP password only (for Gmail, an app password).
- `alert-email-to`: the address that receives alerts.

### Claude Code usage

Prometheus accepts OpenTelemetry metrics directly (`--web.enable-otlp-receiver`), so
Claude Code sends its own usage metrics with no collector. Add to the `env` block of
`~/.claude/settings.json`:

```json
"CLAUDE_CODE_ENABLE_TELEMETRY": "1",
"OTEL_METRICS_EXPORTER": "otlp",
"OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "http://127.0.0.1:9090/api/v1/otlp/v1/metrics",
"OTEL_EXPORTER_OTLP_METRICS_PROTOCOL": "http/protobuf",
"OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "cumulative",
"OTEL_METRICS_INCLUDE_ACCOUNT_UUID": "false",
"OTEL_METRIC_EXPORT_INTERVAL": "5000"
```

The "Claude Code usage" dashboard shows cost, sessions, active time, the cache-read
share, cost and output tokens by model, and tokens by type. Each session is its own
cumulative counter, so panels sum each session's last value in the time range. Cost is
Claude Code's estimate, not billing.

- The export interval is 5 seconds because, at the default 60, a session that ended
  sooner was not exported in testing.
- Only metrics are sent; prompt and response logging stay off (their defaults).
- Claude Code always attaches `user.email`, which is why the stack is bound to
  localhost. Do not expose ports 9090 or 3000.
- If the stack is down, Claude Code keeps working and the metrics are dropped
  (checked: a session with the endpoint stopped exited 0).

### Model-serving metrics

Prometheus scrapes vLLM through the gateway at `https://host.orb.internal:19444`
(using the MLX upstream token and gateway CA from `~/.config/ai-gateway`, mounted as
Compose secrets) and LiteLLM at `host.docker.internal:4000`. The LiteLLM target needs
the proxy port-forward and `LITELLM_METRICS_KEY` in the environment before
`docker compose up`. The pinned LiteLLM build rejects a normal virtual key on
`/metrics`, so use the master key for this local stack and never commit it.

Import `observability/grafana-vllm-dashboard.json` (vLLM's own dashboard: TTFT, TPOT,
throughput, KV cache, prefix cache hit rate, queue depth) and
`observability/grafana-litellm-dashboard.json` once through Grafana's import page,
choosing the provisioned Prometheus datasource.

## Eval harness

Compare two OpenAI-compatible endpoints before promoting a model or quantization swap:

```
python3 eval/eval_harness.py \
  --baseline http://localhost:8000/v1 --baseline-model Qwen/Qwen3-0.6B \
  --candidate http://localhost:8001/v1 --candidate-model mlx-community/Qwen3-0.6B-4bit
```

The first real run, against a 4-bit MLX quant of the same model, found text quality
unchanged but **tool calling silently broken**: the quantized model stopped emitting
`tool_calls` on two cases the base model passed.

- A case can assert text (`must_contain`), a tool call (`must_call_tool`), a forbidden
  tool (`must_not_call_tool`), or one exact call with its arguments
  (`must_call_tool_args`, whose tool must appear in the case's `tools`).
- Unknown case keys are rejected, so a misspelled assertion cannot be silently ignored.
- Argument comparison keeps JSON types (`true`, `1` and `1.0` differ) and compares
  decimals exactly, sign included.
- The process exits nonzero on a candidate regression, a case that fails on both
  models, or a request error. Missing token usage is reported as unknown.
- The report includes response model, token usage and p50/p95 latency.

It checks the model's proposed call and never executes tools. It is a single-response
comparison, not a multi-step agent replay or a production safety gate.

## Security notes

- Every consumer's default route reaches the models through LiteLLM and the
  authenticated gateway. That is not an isolation boundary: OrbStack forwards host
  loopback to every container and pod and does not enforce NetworkPolicy, so local
  workloads can still call raw Ollama and vLLM. See `AI_LAB_SECURITY.md`.
- No secrets are in this repository. Buzz keys (`~/.buzz/keys.json`), the Hermes `.env`,
  the OpenClaw gateway token, LiteLLM keys and gateway tokens stay local; files that
  would hold them are `*.example` placeholders.
