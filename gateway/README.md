# Native inference gateway

This is the shadow configuration for the enforced request path documented in
`../AI_LAB_SECURITY.md`:

```text
consumer -> LiteLLM -> Caddy -> loopback-only Ollama or MLX
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

## Production cutover prerequisites

Do not run this configuration as a persistent service yet. The cutover still
requires all of the following in one reviewed change:

- prove the OrbStack-facing host address and its stability;
- issue a gateway certificate whose SAN matches the in-cluster hostname;
- store the private key and upstream tokens in owner-only host files;
- mount only the CA certificate and Secret-backed upstream tokens into LiteLLM;
- confirm the current LiteLLM build can inject the bearer tokens and verify the
  private CA without disabling TLS verification;
- bind Ollama and MLX to loopback and prove raw ports are unreachable from an
  unrelated pod and containers on multiple OrbStack bridges;
- migrate Open WebUI and all stable consumers to LiteLLM virtual keys;
- stage tested NetworkPolicies for DNS, LiteLLM, Postgres and gateway egress;
- capture exact pre-cutover state and rollback commands.

Never put a real token in this repository, a ConfigMap, a process argument, or a
log. Never use `tls_insecure_skip_verify` or plaintext HTTP as a fallback.
