# Local AI lab security and acceptance

## Decision

Use OrbStack and k3s for the local control plane, agents, routing, state and
observability. Run Ollama or MLX natively on macOS to use Metal. Treat every
result from this environment as **development evidence**, never Linux/NVIDIA
release evidence.

The intended request path is:

```text
consumer --per-consumer key--> LiteLLM --dedicated host gateway--> native inference
```

The current stack does not yet enforce the second boundary. The
`ollama-host` and `vllm-host` ExternalName services expose the native backends
to every pod that can reach `host.orb.internal`; Open WebUI also points to those
services directly. Ollama has unauthenticated model-management endpoints. Until
the acceptance checks below pass, describe LiteLLM as the preferred route, not
as a security boundary.

## Required hardening before enforced-gateway status

1. Put a dedicated authenticated host-side gateway in front of each native
   backend. Bind the backend to the narrowest address that still permits the
   gateway to reach it.
2. Allow only inference and health routes. For Ollama, explicitly deny model
   mutation routes including `/api/pull`, `/api/push`, `/api/delete`,
   `/api/create`, `/api/copy`, and their versioned equivalents.
3. Configure LiteLLM as the only client of the host gateway. Store its upstream
   credential in a Kubernetes Secret, not a ConfigMap or command line.
4. Move Open WebUI and every other stable consumer to LiteLLM with a distinct,
   model-scoped virtual key. Do not give consumers the LiteLLM master key.
5. Remove the direct backend configuration from consumers. Removing DNS aliases
   alone is not a control because pods can still resolve `host.orb.internal`.
6. Apply default-deny ingress and egress policies only after proving that the
   active CNI enforces them. Explicitly allow DNS, LiteLLM, and each service's
   required state dependencies.
7. Keep gateway logs metadata-only. Do not log prompts, responses, bearer
   tokens, request bodies, or model inputs.

## Cutover acceptance

Capture a baseline and a rollback command before applying any live change. The
cutover is accepted only when all of these tests pass:

- Existing consumers complete the fixed eval suite through LiteLLM.
- Each consumer key can reach only its approved model aliases.
- A revoked consumer key fails.
- An unrelated pod cannot connect to raw Ollama or vLLM at all, with or without
  a credential. The host gateway returns 401 or 403 without its upstream
  credential.
- A container on a different OrbStack bridge cannot call the host gateway
  without the upstream credential. Network separation alone is not expected on
  OrbStack and is not counted as a pass.
- Ollama inference succeeds through LiteLLM while pull, push, delete, create and
  copy fail through every consumer path.
- Prometheus health and inference metrics remain available without recording
  prompt or response content.
- Rolling back restores the exact pre-cutover consumer path.

Do not commit or print credentials while testing. Use throwaway tokens and
delete them after the negative tests.

## Model selection pre-check

`llm-fit` is an advisory planning input. It never approves a deployment and
must never override a failed or unknown BlockOps preflight.

The repository pins the expected CLI version in
`scripts/capture-llmfit-advisory.sh`. Capture the recommendation before manifest
approval:

```bash
scripts/capture-llmfit-advisory.sh ./evidence/llmfit-candidate

# Include a model-specific plan when the catalog selector and approved context
# are known.
scripts/capture-llmfit-advisory.sh \
  ./evidence/llmfit-qwen \
  Qwen/Qwen3-8B 8192 mlx-8bit
```

The script refuses an unexpected tool version, platform or reviewed executable
SHA-256 and an existing output directory. It disables the optional dashboard,
requires `jq` to validate every JSON document, captures system and recommendation
JSON, optionally captures a model plan, and writes hashes plus executable
provenance. It invokes no download, deployment, benchmark, or sharing operation.
Preserve the directory as asserted planning evidence, then run the BlockOps
manifest preflight against the actual deployment target.

Responsibility boundary: **llm-fit recommends; BlockOps validates the approved
deployment envelope.**

## Release acceptance lane

The BlockOps vLLM profile remains unreleased until it passes on an Ubuntu host
with Docker Engine, NVIDIA Container Toolkit and a real NVIDIA GPU. The run must
cover GPU discovery, device requests, image and Hugging Face revision pins,
unauthenticated-request rejection, synthetic inference, rollback, interrupted
recovery, required Docker network isolation, signed receipt generation and
independent verification.

Podman/libkrun experiments on macOS are compatibility experiments only. Use a
disposable machine/profile with separate caches and no production credentials;
they do not satisfy the NVIDIA or Docker release gate.
