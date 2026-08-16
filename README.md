# homelab

Local k3s (OrbStack) cluster running a small agentic stack: Hermes (agent runtime),
OpenClaw (plugin-based agent gateway), Buzz (Nostr-based team chat relay used as an
inter-agent channel), vLLM-metal and Ollama (native macOS processes, registered into
the cluster via ExternalName services), and Open WebUI as a browser front end.

This is a learning/reference repo, not a deployable product. It exists to document
the real integration points and the real bugs hit while wiring these pieces together,
as groundwork for [agent-guard](https://github.com/agent-rails/agent-guard) and
broader Principal Architect / FDE-track experience with agent orchestration on k8s.

## Layout

- `k3s/hermes/` — Hermes deployment manifests (ConfigMap, PVC, Deployment)
- `k3s/ai-infra/` — ExternalName services registering native-host vLLM/Ollama into
  cluster DNS, plus Open WebUI helm values
- `k3s/buzz/` — Buzz relay helm values (chart: `buzz-0.1.7`)
- `hermes/` — Hermes agent config example (`config.yaml.example`, `.env.example`)
- `openclaw/` — OpenClaw's Buzz channel config example, plus
  `skills/llmfit-advisor.SKILL.md` (copy of the installed skill — real source:
  [AlexsJones/llmfit](https://github.com/AlexsJones/llmfit)) that lets the agent
  recommend hardware-fit local models via `llmfit recommend --json`
- `observability/` — Prometheus + Grafana (docker-compose) scraping vLLM's
  `/metrics`, using vLLM's own official dashboard (12 panels: TTFT, TPOT, throughput,
  KV cache usage, prefix cache hit rate, request queue depth)
- `eval/` — small regression harness: runs a fixed prompt set (arithmetic,
  instruction-following, tool-call triggering) against two OpenAI-compatible
  endpoints and diffs baseline vs candidate, nonzero exit on regression
- `DECISIONS.md` — real footguns hit and how they were resolved
- `DESIGN.md` — architecture, DevSecOps threat model, and rollout plan for a
  LiteLLM Proxy fronting vLLM/Ollama (Phase 1 implemented, see `k3s/ai-infra/litellm-*`;
  llm-d was considered and rejected as oversized for a single-GPU homelab)

## What's NOT here

Real secrets: Buzz nsec keys (`~/.buzz/keys.json`), the Hermes `.env`
(`BUZZ_PRIVATE_KEY`, `BUZZ_ALLOWED_USERS`), and the OpenClaw gateway auth token
(`~/.openclaw/openclaw.json` `gateway.auth.token`) all stay local. Every file here
that would normally hold one of those uses a placeholder — see `*.example` files.

## Architecture

```
                     OrbStack k3s cluster
   ┌─────────────────────────────────────────────────────┐
   │  ai-infra ns                                         │
   │    ollama-host (ExternalName) ──► host.orb.internal   │
   │    vllm-host   (ExternalName) ──► host.orb.internal   │
   │    open-webui  (Deployment)  ──► browser UI           │
   │                                                       │
   │  ai-infra ns                                         │
   │    hermes (Deployment) ──► model: custom/gpt-oss:20b  │
   │                                                       │
   │  buzz ns                                             │
   │    buzz (relay: WS + REST + NIP-11)                   │
   │    buzz-postgresql / buzz-redis / buzz-minio          │
   └─────────────────────────────────────────────────────┘
              ▲                        ▲
              │ ws://localhost:3939    │ (native macOS processes)
              │ (kubectl port-forward) │
   ┌──────────┴──────────┐   ┌─────────┴─────────┐
   │ Hermes gateway (CLI)│   │ vllm-metal (MLX)   │
   │ Buzz identity        │   │ Ollama              │
   └──────────────────────┘   └─────────────────────┘
              ▲
              │ ws://localhost:3939
   ┌──────────┴──────────┐
   │ OpenClaw gateway     │
   │ Buzz plugin (source- │
   │ linked, not npm)     │
   └──────────────────────┘
```

## Bring-up order

1. `k3s/ai-infra/` — ExternalName services, then Open WebUI (`helm install open-webui
   open-webui/open-webui -n ai-infra -f k3s/ai-infra/open-webui-values.yaml`)
2. `k3s/buzz/` — relay (`helm install buzz buzz/buzz -n buzz -f k3s/buzz/values.yaml`)
3. `k3s/hermes/` — agent runtime, once `ai-infra` DNS is resolvable
4. Hermes CLI + OpenClaw gateway run as native macOS processes, each holding its own
   Buzz identity (see `hermes/.env.example`, `openclaw/buzz-channel-config.example.json`)

`kubectl port-forward -n buzz svc/buzz 3939:3000` exposes the relay to both native
processes. **Port 3000 is the app port — the service also exposes 8080 as a health
probe only; forwarding to 8080 gets a 404 on every real API call.**

## Observability

```
cd observability && docker compose up -d
```

Prometheus (`localhost:9090`) scrapes vLLM's `/metrics` via `host.docker.internal:8000`
— works out of the box against a native-process vLLM, no adaptation needed from
vLLM's own upstream example. Grafana (`localhost:3000`, default `admin`/`admin`)
needs the Prometheus datasource and dashboard added once per fresh container:

```bash
curl -s -X POST http://admin:admin@localhost:3000/api/datasources \
  -H "Content-Type: application/json" \
  -d '{"name":"Prometheus","type":"prometheus","url":"http://prometheus:9090","access":"proxy","isDefault":true}'
```

then import `observability/grafana-vllm-dashboard.json` via Grafana's dashboard
import UI (or `/api/dashboards/import`), pointing its `DS_PROMETHEUS` input at the
datasource UID returned above.

## LiteLLM Proxy (Phase 1)

```
kubectl apply -f k3s/ai-infra/litellm-configmap.yaml -f k3s/ai-infra/litellm-deployment.yaml -f k3s/ai-infra/litellm-service.yaml
kubectl port-forward -n ai-infra svc/litellm-proxy 4000:4000
```

Needs a `litellm-secrets` Secret first (see `k3s/ai-infra/litellm-secret.yaml.example`) —
**both** `LITELLM_MASTER_KEY` and `LITELLM_SALT_KEY` must be `openssl rand -base64 32`
format specifically (not hex) or LiteLLM's own key-cache decryption fails with a
misleading `No connected db.` error. Single OpenAI-compatible endpoint fronting
`vllm-host` and `ollama-host`, model aliases `ollama-default`, `vllm-default`,
`gpt-oss-default`.

Hermes routes through it natively (`~/.hermes/config.yaml` `base_url` +
`key_env: LITELLM_MASTER_KEY`, real key in `.env`). OpenClaw routes through it too,
but **not** via a hand-rolled custom provider entry — OpenClaw's own bundled `vllm`
provider (`openai-completions` API, real `VLLM_API_KEY` auth wiring already built
in) works against any OpenAI-compatible backend, LiteLLM included. Point its
`baseUrl` at `http://127.0.0.1:4000/v1` and set `VLLM_API_KEY` to the LiteLLM
master key — see DECISIONS.md for the full story of why a custom `models.providers.litellm`
entry silently sends an empty API key and this is the real fix.

## LiteLLM Proxy (Phase 2 — Postgres + per-consumer virtual keys)

```
kubectl apply -f k3s/ai-infra/litellm-postgres-pvc.yaml \
  -f k3s/ai-infra/litellm-postgres-service.yaml \
  -f k3s/ai-infra/litellm-postgres-deployment.yaml
```

Needs a `litellm-postgres-secrets` Secret first (see
`k3s/ai-infra/litellm-postgres-secret.yaml.example`) — a dedicated single-replica
Postgres, deliberately NOT shared with `buzz-postgresql` (DESIGN.md §4 rejected
that coupling). Then add `DATABASE_URL` to the existing `litellm-secrets` Secret
(placeholder in `litellm-secret.yaml.example`) and re-apply
`litellm-deployment.yaml`. The proxy now runs `prisma migrate deploy` on boot; the
`startupProbe` + `2Gi` memory limit exist specifically so the migration completes
before liveness kills it and without OOM (see DECISIONS.md).

Per-consumer virtual keys are minted against the master key and scoped to only the
models each consumer needs:

```bash
MK=$(kubectl get secret litellm-secrets -n ai-infra -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
curl -s http://localhost:4000/key/generate -H "Authorization: Bearer $MK" \
  -H "Content-Type: application/json" \
  -d '{"key_alias":"openclaw","models":["ollama-default"]}'
```

Scoping: hermes-native and hermes-incluster -> `gpt-oss-default`; openclaw ->
`ollama-default`; eval-harness -> all three. Each consumer holds its own key, not
the master key: Hermes uses `key_env: LITELLM_VIRTUAL_KEY` (native: value in
`~/.hermes/.env`; in-cluster: `secretKeyRef` -> `litellm-secrets`
key `LITELLM_VIRTUAL_KEY_HERMES`), OpenClaw's bundled `vllm` provider reads
`VLLM_API_KEY` (export the openclaw virtual key before `openclaw gateway run`).
The master key is retained only as the proxy admin credential for
`/key/generate` and `/key/block`.

Rollback: if Postgres has an incident, point the proxy back at master-key auth
(proxy-side config only — every consumer already targets the proxy `base_url`).

## Eval harness (model/quant swap regression)

```
python3 eval/eval_harness.py \
  --baseline http://localhost:8000/v1 --baseline-model Qwen/Qwen3-0.6B \
  --candidate http://localhost:8001/v1 --candidate-model mlx-community/Qwen3-0.6B-4bit
```

Run this before "promoting" any model or quantization swap. Real result the first
time this ran against a 4-bit MLX quant of the same model: text quality was fine
(even slightly better on one case), but **tool-calling silently broke** — the
quantized model stopped emitting `tool_calls` entirely on two cases the base model
passed. Exit code is nonzero on any baseline-passed/candidate-failed case, so this
is CI-gateable.
