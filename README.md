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
- `openclaw/` — OpenClaw's Buzz channel config example
- `observability/` — Prometheus + Grafana (docker-compose) scraping vLLM's
  `/metrics`, using vLLM's own official dashboard (12 panels: TTFT, TPOT, throughput,
  KV cache usage, prefix cache hit rate, request queue depth)
- `DECISIONS.md` — real footguns hit and how they were resolved

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
