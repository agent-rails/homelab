# DESIGN.md — LiteLLM Proxy for Local Model Serving

## 1. Executive summary

Every consumer of local models in this stack (Hermes, OpenClaw, the eval harness, ad-hoc load-test scripts) hardcodes raw backend addresses (`localhost:8000` for vLLM, `localhost:11434` for Ollama), both of which are completely unauthenticated. There is no unified interface, no auth boundary, no cross-backend observability (Grafana only sees vLLM), and swapping or adding a backend means editing every consumer individually. The decision is to deploy LiteLLM Proxy, an open-source, self-hosted, OpenAI-compatible gateway, as a k8s Deployment in the `ai-infra` namespace, fronting both backends through the existing `vllm-host`/`ollama-host` ExternalName services and exposed to native-process consumers via `kubectl port-forward`, the same pattern already used for the Buzz relay. The one-sentence why: centralize routing, auth, and observability behind one interface so consumers stop talking to raw, unauthenticated backend ports directly, without adopting infrastructure (llm-d) built for a distributed-GPU problem this single-GPU homelab doesn't have.

## 2. Architecture

```
                          OrbStack k3s cluster
   ┌───────────────────────────────────────────────────────────────┐
   │  ai-infra ns                                                   │
   │                                                                 │
   │    ollama-host (ExternalName) ──► host.orb.internal:11434       │
   │    vllm-host   (ExternalName) ──► host.orb.internal:8000        │
   │                    ▲                    ▲                       │
   │                    │                    │                       │
   │    litellm-proxy (Deployment, 1 replica)                        │
   │      model_list: ollama-default  -> ollama-host:11434           │
   │                  vllm-default    -> vllm-host:8000/v1           │
   │      auth: virtual keys (per consumer) via master_key           │
   │      metrics: /metrics (prometheus callback)                    │
   │            │                                                    │
   │    litellm-postgres (Deployment + PVC)                          │
   │      LiteLLM_VerificationTokenTable (virtual key + spend store) │
   │                                                                 │
   │    open-webui (Deployment) ──► browser UI (unchanged)           │
   │    hermes (Deployment) ──► model: custom, base_url -> proxy     │
   │                                                                 │
   │  buzz ns (unchanged)                                            │
   │    buzz (relay: WS + REST + NIP-11)                             │
   │    buzz-postgresql / buzz-redis / buzz-minio                    │
   └───────────────────────────────────────────────────────────────┘
              ▲                                    ▲
              │ http://localhost:4000/v1           │ (native macOS processes,
              │ (kubectl port-forward)              │  unchanged, still directly
              │ + per-consumer virtual key           │  reachable — see §3, §5)
   ┌──────────┴───────────────┐          ┌──────────┴──────────┐
   │ Hermes gateway (CLI)      │          │ vllm-metal (MLX)     │
   │ OpenClaw gateway           │          │ Ollama                │
   │ eval_harness.py             │          └──────────────────────┘
   │ load-test scripts             │
   └────────────────────────────────┘
```

Nothing changes about how vLLM and Ollama run: still native macOS processes, still registered into cluster DNS via the existing ExternalName services. LiteLLM Proxy is the only new moving part inside the cluster; it consumes those same ExternalName services rather than bypassing them, so the `host.orb.internal` indirection stays centralized in one place (`k3s/ai-infra/*-service.yaml`) instead of being duplicated into a proxy-specific config.

## 3. DevSecOps threat model

### 3.1 Current auth boundary (baseline, before this design)

Both vLLM (`:8000`) and Ollama (`:11434`) are unauthenticated. Anything that can resolve `vllm-host`/`ollama-host` in-cluster, or reach `host.orb.internal` on those ports from the host Mac directly, gets full API access: any model, any endpoint, no rate limit, no request attribution. Ollama in particular exposes model-management endpoints (`/api/pull`, `/api/delete`) alongside its chat API — those are reachable too, not just inference.

### 3.2 New auth boundary this design creates

LiteLLM Proxy sits in front of both backends and requires a virtual API key (an `sk-...` token) on every request. Two auth-generation paths exist, and this design deliberately does not pick the database-backed one on day one (see §4 phasing):

- **Master key** (`general_settings.master_key`, set via `LITELLM_MASTER_KEY` env var from a k8s Secret): the proxy admin credential. It authenticates every request in Phase 1 and, in Phase 2, is retained solely to mint/revoke virtual keys via `/key/generate` — it stops being handed to any consumer.
- **Virtual keys** (Phase 2): per-consumer `sk-...` tokens, each scoped to a specific `models` list (e.g. the eval harness's key gets both `ollama-default` and `vllm-default`; Hermes's key gets only `ollama-default`). Virtual keys require a Postgres-backed `LiteLLM_VerificationTokenTable` — LiteLLM's own docs are explicit that without a database, keys cannot be dynamically generated or individually revoked, only the single master key works. This is a real, load-bearing constraint, not a config nuance: it's the reason this design proposes a Postgres instance at all.

### 3.3 Secrets handling — generation, storage, rotation, revocation

- **Generation**: `master_key` generated once via `openssl rand -hex 32`, prefixed `sk-` per LiteLLM convention. Postgres credentials generated the same way. Both minted at deploy time, never checked into git — this repo's existing convention (Buzz nsec keys, Hermes `.env`, OpenClaw gateway token) is that every real secret has a placeholder `.example` file and the real value stays local; `litellm-secret.yaml` follows the same pattern the `hermes-claude-token` Secret already establishes in `k3s/hermes/deployment.yaml` (`valueFrom.secretKeyRef`).
- **Storage**: k8s Secret `litellm-secrets` in `ai-infra`, holding `master_key` and (Phase 2) `database_url`. Mounted as env vars, never as a file baked into the ConfigMap-mounted `models.yaml`.
- **Per-consumer virtual keys** (Phase 2): minted individually via `POST /key/generate` against the master key, one per consumer (Hermes, OpenClaw, eval harness, load-test tooling). Each key's value is stored the same way the repo already stores `BUZZ_PRIVATE_KEY` — in that consumer's local `.env`/config, with a placeholder committed.
- **Rotation**: LiteLLM's automatic scheduled rotation is an enterprise feature; this design does not depend on it. Manual rotation procedure: mint a replacement key scoped identically, roll it into the consumer's config, restart that consumer, then `/key/block` the old key. Rotation trigger is event-driven, not calendar-driven — rotate a consumer's key when its threat model changes (e.g. a new third-party OpenClaw skill is installed) or on suspected compromise. A fixed calendar cadence would be compliance theater at this scale; naming the actual trigger is more honest.
- **Revocation**: `/key/block` (immediate) vs `/key/unblock`. This is the concrete capability that raw backend access never had — today, "revoking" a misbehaving consumer means killing the consumer process or firewalling the whole backend, there's no scoped off-switch.

### 3.4 Blast radius: compromised consumer, scoped key vs raw backend access

Today (raw access), a compromised or buggy agent skill that discovers `ollama-host:11434` can call any model, hit Ollama's management endpoints, and generate traffic indistinguishable from any other consumer's — no log ties a request back to its origin.

With a scoped virtual key: the compromised consumer is limited to (a) whatever `models` its key was scoped to, and (b) the OpenAI-compatible surface LiteLLM proxies at all — LiteLLM's Ollama integration maps to `/api/chat`/`/api/generate` translated into the OpenAI schema, it does not forward arbitrary Ollama-native routes like `/api/pull` or `/api/delete`. So a consumer that only ever goes through the proxy gets both narrower model access and a narrower endpoint surface than raw access ever offered. This containment is real but conditional: it only holds for a consumer that actually goes through the proxy. See §3.5 for why that's not enforced.

### 3.5 What this design explicitly does NOT protect against

- **This is still one trust boundary.** Everything here runs on one Mac. This does not defend against network-level attackers, other hosts, or anything beyond what "unauthenticated localhost service" already implied. Framing this as closing a network security gap would be overselling it.
- **The proxy is opt-in, not enforced.** vLLM and Ollama keep listening on their existing ports, unauthenticated, reachable by anything that could reach them before. Nothing in this design stops a consumer (or a compromised skill) from bypassing the proxy and hitting `vllm-host:8000` or `ollama-host:11434` directly. Closing that would mean binding the backends to loopback-only and routing everything through the proxy's egress, which would break the ExternalName pattern this stack already depends on (nothing else could reach them either) — out of scope for this rollout, named here so it isn't silently assumed solved.
- **The proxy itself is a new, higher-value target.** Centralizing routing through one process also centralizes the blast radius of compromising *that* process: a compromised proxy sees and can manipulate all model traffic from all consumers, where before an attacker who'd compromised one consumer only saw that consumer's traffic. This is the standard gateway tradeoff (centralize visibility, also centralize attack surface) and it should be named explicitly rather than only marketing the visibility half.
- **Supply chain on the proxy image itself is real, not hypothetical.** LiteLLM had a genuine PyPI supply-chain compromise in March 2026 (versions 1.82.7/1.82.8, malicious `.pth` file with credential-stealing and Kubernetes-cluster-spreading capability, traced to a compromised Trivy dependency in LiteLLM's own CI). The official Docker image path (`ghcr.io/berriai/litellm`) was confirmed unaffected — it pins `requirements.txt` and never touched the compromised PyPI releases — which is why this design mandates deploying via that image, pinned to an exact digest of a post-incident release, never `:latest`. This is a concrete argument for image-digest pinning discipline, not abstract advice.
- **No compliance-grade audit trail.** Proxy request logs are useful for debugging and attribution, not a tamper-evident audit log.
- **No rate limiting configured in this rollout.** LiteLLM supports it; this design doesn't turn it on. Named as a deferred follow-up, not implemented.
- **`/metrics` is not locked down beyond "any valid key can read it."** LiteLLM has an open upstream issue (BerriAI/litellm#13644) that any existing API key, not just an admin key, can read `/metrics` without further restriction. Consistent with today's posture (vLLM's own `/metrics` is already unauthenticated within the trust boundary), so this isn't a regression, but it's not a hardening win either — worth stating plainly rather than letting "we added auth" imply metrics got locked down too.
- **Single replica, no HA.** Appropriate for a single-GPU homelab; this pattern would need real redundancy analysis before generalizing to a shared/team environment.

## 4. Rollout plan

### Phase 0 — prep
- Generate `master_key` (`openssl rand -hex 32`, `sk-` prefix).
- Write `k3s/ai-infra/litellm-configmap.yaml` (`models.yaml`):
  ```yaml
  model_list:
    - model_name: ollama-default
      litellm_params:
        model: ollama/gpt-oss:20b
        api_base: http://ollama-host:11434
    - model_name: vllm-default
      litellm_params:
        model: openai/Qwen/Qwen3-0.6B
        api_base: http://vllm-host:8000/v1
  litellm_settings:
    callbacks: ["prometheus"]
  general_settings:
    master_key: os.environ/LITELLM_MASTER_KEY
  ```
- `k3s/ai-infra/litellm-secret.yaml`: placeholder `.example` committed, real Secret applied locally, matching the `hermes-claude-token` pattern exactly.

### Phase 1 — ship the proxy, master-key-only, prove parity
- `k3s/ai-infra/litellm-deployment.yaml`: single-replica Deployment, image `ghcr.io/berriai/litellm:<pinned-digest>`, `args: ["--config", "/app/config.yaml", "--port", "4000"]`, config mounted read-only from the ConfigMap (no PVC/init-container seeding — unlike Hermes's config, this one has no runtime-mutation use case, so a plain ConfigMap mount is the simpler correct choice), `livenessProbe`/`readinessProbe` on `/health/liveliness` and `/health/readiness` at port 4000, resource requests/limits sized like a small FastAPI process (`cpu: 100m/500m`, `memory: 128Mi/256Mi`).
- `k3s/ai-infra/litellm-service.yaml`: ClusterIP, port 4000.
- `kubectl port-forward -n ai-infra svc/litellm-proxy 4000:4000` — same exposure pattern as Buzz's `kubectl port-forward -n buzz svc/buzz 3939:3000`. Note up front: this stack's own DECISIONS.md already documents that `kubectl port-forward` doesn't survive laptop sleep for the Buzz forward; this new forward inherits the identical failure mode and needs the same manual-restart (or future `launchd`-supervisor) treatment.
- Point every consumer at the proxy, still using the single master key as the API key:
  - `hermes/config.yaml` (and `k3s/hermes/configmap.yaml`): `base_url: http://localhost:4000/v1` (native Hermes CLI) or `http://litellm-proxy.ai-infra.svc:4000/v1` (in-cluster Hermes Deployment), `default: ollama-default`, `api_key: <master_key>`.
  - OpenClaw `models.providers.ollama.base_url` → `http://localhost:4000/v1`, `api_key: <master_key>`.
  - `eval_harness.py --baseline http://localhost:4000/v1 --baseline-model ollama-default` (or `vllm-default`) — no code change needed, it's already a `--baseline`/`--candidate` base-URL flag.
  - Load-test scripts: same base-URL swap.
- **Validation**: re-run the existing eval harness with `--baseline` pointed at the direct backend and `--candidate` pointed at the proxy in front of the *same* backend/model. A clean run (zero regressions) is the proof that the proxy path is behaviorally transparent — same contract the harness already applies to quant swaps, just applied to a routing-layer swap instead of a model swap.
- **Rollback**: point the 4 consumer configs back at the raw backend ports. ExternalName services are untouched throughout, so this is a config-only revert with no cluster-object cleanup required.

### Phase 2 — Postgres, per-consumer virtual keys, revoke master-key-as-bearer-token
- `k3s/ai-infra/litellm-postgres-*.yaml`: dedicated, minimal single-replica Postgres (Deployment + small PVC + its own Secret), **not** a shared database inside `buzz-postgresql`. Considered and rejected: reusing the existing Buzz Postgres instance would save a few hundred MB of RAM, but it would couple two unrelated failure domains — a Buzz-relay database incident would take down LLM proxy auth, and vice versa. On a 32GB Mac already carrying two large native model processes, a second small Postgres (~150-250Mi) is noise; the coupling it avoids is not.
- Wire `DATABASE_URL` into the LiteLLM Secret, restart the proxy.
- Mint 4 virtual keys via `/key/generate` against the master key, one per consumer, each scoped to only the `model_list` entries that consumer actually needs (e.g. eval harness gets both `ollama-default` and `vllm-default`; Hermes and OpenClaw get only `ollama-default`).
- Roll each consumer's config from the master key to its own virtual key, one at a time, validating with the eval harness after each cutover.
- Once all four are cut over, the master key is no longer handed to any consumer — retained only as the admin credential for future `/key/generate`/`/key/block` calls.
- **Rollback**: if Postgres has an incident, fall back to master-key auth temporarily (proxy-side config change only — no consumer config touches required, since every consumer already points at the proxy's `base_url`, they just resume presenting the master key). This is the concrete payoff of centralizing in Phase 1 first: by Phase 2, rollback is a one-place change instead of four.

### Phase 3 — observability
- Add `litellm-proxy:4000/metrics` as a second Prometheus scrape target alongside the existing `host.docker.internal:8000` vLLM target in `observability/prometheus.yaml`.
- Add a minimal Grafana panel set (request count/latency by `model` and by key/consumer label) next to the existing vLLM dashboard — this is what actually closes the "Ollama traffic is invisible" gap named in the problem statement; today's dashboard only sees vLLM's own `/metrics`.

## 5. What this design explicitly does not solve

- Does not add network-level authentication or isolation beyond this one Mac's trust boundary.
- Does not stop bypass of the proxy: vLLM and Ollama remain reachable on their existing ports unless separately firewalled, which is out of scope here (see §3.5).
- Does not implement rate limiting, even though LiteLLM supports it — deferred, not configured in this rollout.
- Does not provide HA or multi-replica resilience for the proxy; single point of failure by design, appropriate at this scale, not appropriate to generalize without redesign.
- Does not solve the OpenClaw `openclaw.sqlite` schema-7-vs-6 bug or any other existing DECISIONS.md issue — explicitly out of scope, unrelated failure domain.
- Does not give the eval harness's ad hoc quant-swap candidate backends (e.g. a throwaway vLLM process on `:8001`, seen in the existing `eval_harness.py --candidate` usage) a stable proxy alias. Those are inherently transient, non-DNS-registered experiments; forcing every one-off quant test through a checked-in `model_list` entry would add process overhead disproportionate to the value. The harness keeps the ability to point `--candidate` directly at an ad hoc backend; only the *stable* baseline path is expected to routinely go through the proxy.
- Does not replace or reduce the value of llm-d for this environment — that evaluation stands. llm-d exists to do KV-cache-aware routing and disaggregated prefill/decode across many vLLM replicas on a multi-GPU Kubernetes cluster, via the Gateway API Inference Extension's External Processing Pod scoring decode pods by prefix-cache locality. This homelab has one GPU and no replica-routing problem to solve; adopting llm-d here would be infrastructure sized for someone else's cluster, not this one.

## Sources

- [Getting Started | liteLLM](https://docs.litellm.ai/docs/)
- [Virtual Keys | liteLLM](https://docs.litellm.ai/docs/proxy/virtual_keys)
- [Health Checks | liteLLM](https://docs.litellm.ai/docs/proxy/health)
- [Kubernetes and Helm | BerriAI/litellm | DeepWiki](https://deepwiki.com/BerriAI/litellm/4.2.2-kubernetes-and-helm)
- [Prometheus metrics | liteLLM](https://docs.litellm.ai/docs/proxy/prometheus)
- [Ollama | liteLLM](https://docs.litellm.ai/docs/providers/ollama)
- [OpenAI-Compatible Endpoints | liteLLM](https://docs.litellm.ai/docs/providers/openai_compatible)
- [Security Update: Suspected Supply Chain Incident | liteLLM](https://docs.litellm.ai/blog/security-update-march-2026)
- [Datadog Security Labs: LiteLLM and Telnyx compromised on PyPI](https://securitylabs.datadoghq.com/articles/litellm-compromised-pypi-teampcp-supply-chain-campaign/)
- [Master KV cache aware routing with llm-d | Red Hat Developer](https://developers.redhat.com/articles/2025/10/07/master-kv-cache-aware-routing-llm-d-efficient-ai-inference)
- [Announcing the llm-d community! | llm-d](https://llm-d.ai/blog/llm-d-announce)
- [BerriAI/litellm#13644 — Prometheus /metrics access control issue](https://github.com/BerriAI/litellm/issues/13644)
