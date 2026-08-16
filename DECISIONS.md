# Decisions and footguns

Real problems hit while building this stack, and how they were actually resolved.
No hypotheticals — every entry below was reproduced live.

## Prefix caching shows no benefit under low, sequential load

Ran an A/B (`--enable-prefix-caching` vs `--no-enable-prefix-caching`) with 5
sequential requests sharing a 512-token system prompt. Cache hits were real and
confirmed via `/metrics` (1920/2543 tokens hit-cached), but steady-state latency
was statistically indistinguishable between the two runs (~288ms vs ~304ms avg).
At this scale — small model, short prompt, one request at a time, no batching
contention — prefill is already cheap enough that skipping it doesn't move the
needle. The real win only showed up conceptually once concurrency entered the
picture (`vllm bench serve` load test): that's where continuous batching and
prefix caching actually earn their keep. Left caching enabled anyway (no downside)
rather than concluding "it doesn't work" from a benchmark shape too small to show it.

## `vllm bench serve`'s `prefix_repetition` dataset doesn't reliably produce cache hits

Ran the built-in load-test tool with `--dataset-name prefix_repetition` (5 shared
prefixes across 50 requests, concurrency 10) — real throughput/latency numbers came
back clean, but `vllm:prefix_cache_queries_total` stayed at exactly 0 for the whole
run, despite a hand-rolled script (identical literal system prompts) proving the
caching mechanism itself works minutes earlier. Root cause not fully chased, but the
dataset generator (`PrefixRepetitionRandomDataset` in
`vllm/benchmarks/datasets/datasets.py`) builds prefixes from random token IDs via a
decode→re-encode round trip that the code itself tracks as lossy
(`token_mismatch_total`) — combined with `openai-chat` backend re-tokenizing after
chat-template wrapping, the "shared" prefix likely isn't landing as byte-identical
tokens server-side. Not a vLLM caching bug — a benchmark-tool quirk worth knowing
before trusting its cache-hit numbers specifically (its throughput/latency numbers
are still real and trustworthy).

## Qwen3's reasoning mode silently eats an eval harness's token budget

First eval harness run against Qwen3-0.6B (and its 4-bit MLX quant) showed both
baseline and candidate failing identically on arithmetic and tool-calling cases —
looked like a broken harness. Real cause: Qwen3 emits `<think>...</think>` reasoning
tokens by default, and a 64-token budget was entirely consumed by the reasoning
trace before any real answer or tool call. Fixed by passing
`chat_template_kwargs: {"enable_thinking": false}` and raising `max_tokens` to 128.
After the fix, the harness produced a genuinely informative result: the 4-bit quant
stopped emitting tool calls entirely on two cases the base model passed — a real,
silent capability regression from quantization that text-only quality checks would
have missed entirely.

## k8s `command` vs Docker Compose `command`

k8s pod spec `command` overrides the image's Docker `ENTRYPOINT` entirely (unlike
Compose's `command`, which only overrides `CMD`). Caught by cross-referencing a
prior art repo's chart-lessons doc before applying — `deployment.yaml` here uses
`args`, not `command`.

## ExternalName services for native-host processes

vLLM (Metal/MLX) and Ollama run natively on the Mac, not in the cluster — GPU
passthrough into OrbStack's VM isn't a thing. Registered them into cluster DNS via
`ExternalName` services pointing at `host.orb.internal` (`k3s/ai-infra/*-service.yaml`)
so in-cluster workloads (Hermes, Open WebUI) address them by normal service name
instead of hardcoding the host bridge address everywhere.

## vLLM tool-choice rejection

Open WebUI sends `tool_choice: "auto"` by default; vLLM rejects that without
`--enable-auto-tool-choice --tool-call-parser hermes`. The parser name was confirmed
against the real Qwen3 `tokenizer_config.json` chat template, not guessed — wrong
parser choice fails silently with malformed tool calls instead of an error.

## Open WebUI port-forward dies on pod rollout

`kubectl port-forward` binds to a specific pod identity, not the Service. Any pod
restart (including embedding-model cold-start re-downloads, since
`persistence.enabled: false`) silently kills the forward. No persistent volume was
set up on purpose — this is a throwaway dev instance; if that changes, flip
`persistence.enabled: true` in `open-webui-values.yaml` first.

## Buzz relay port: 3000 vs 8080

The Buzz Helm chart exposes three ports: `app` (3000), `health` (8080), `metrics`
(9102). Port-forwarding to 8080 looks like it works — TCP connects fine — but every
real API call returns `relay error 404`. Only port 3000 is the actual application.

## `kubectl port-forward` does not survive laptop sleep

Every port-forward in this stack is a foreground process pinned to the terminal
session; macOS sleep kills the underlying TCP connection and the process doesn't
recover. Both Hermes's and OpenClaw's Buzz plugins have their own reconnect/backoff
logic and recover cleanly once the forward is restarted — but the forward itself
needs restarting manually (or wrapped in a `launchd` job / `kubectl port-forward`
supervisor, not done here since this is a manually-started dev environment).

## Buzz `groupPolicy: allowlist` silently drops everyone without `groupAllowFrom`

The single most expensive bug in this repo's history. OpenClaw's Buzz plugin
(`extensions/buzz/src/inbound.ts`) computes `resolveStableChannelMessageIngress(...)`
and if `access.ingress.admission !== "dispatch"`, returns **with zero logging** —
no warning, no rejected-sender log line, nothing. `groupPolicy: "allowlist"` is the
schema default, and without an explicit `groupAllowFrom` list every sender —
including the channel owner — is silently ignored. The WS connection log
(`Buzz connected to ws://localhost:3939 for 1 channel(s)`) looks completely healthy
the whole time; the only way to find this was reading the plugin's own TypeScript
source. Fix: always pair `groupPolicy: "allowlist"` with an explicit
`groupAllowFrom` list of hex pubkeys (see `openclaw/buzz-channel-config.example.json`).

## LiteLLM Proxy (Phase 1) shipped and validated; two real integration bugs found and fixed, one deferred

Implemented DESIGN.md's Phase 1: `litellm-proxy` Deployment + Service in `ai-infra`,
fronting both `vllm-host` and `ollama-host` behind one OpenAI-compatible endpoint
with master-key auth. Two real bugs surfaced and were fixed during rollout, one
real OpenClaw-specific limitation was found and deferred rather than chased:

**Resource limits underestimated.** The design doc's own estimate (`128Mi`/`256Mi`,
"small FastAPI process") was wrong in practice -- the pod was immediately
OOMKilled (`exitCode: 137`, `reason: OOMKilled`). Bumped to `512Mi`/`1Gi`. A design
document's resource estimate is a guess until it's actually run once.

**Ollama rejects the ExternalName-mapped Host header with a silent 403.** This is
separate from `OLLAMA_ORIGINS`/CORS (which controls the browser `Origin` header) --
Ollama has an independent, hardcoded check that only accepts `Host: localhost`,
and it fails closed with an empty-body 403 and zero server-side log line. Routing
through the `ollama-host` ExternalName service means the proxy's outbound request
carries `Host: ollama-host`, which gets silently rejected. `OLLAMA_ORIGINS=*` does
**not** fix this -- confirmed by setting it via `launchctl setenv` (which also
required discovering the running Ollama was actually a `brew services`-managed
launchd job, not the manually-`nohup`'d process I thought I'd restarted -- launchd
won the race for the port every time). The real fix is `extra_headers: {Host:
"localhost"}` in LiteLLM's `litellm_params` for the Ollama model entries, which
LiteLLM does support and forwards correctly.

**vLLM's single-worker "distributed" init can hang indefinitely on network state
change.** After being idle across a laptop-sleep cycle, restarting vLLM produced
a real, non-transient hang: `[c10d] The server socket on [::ffff:10.5.0.2]:PORT
has timed out, will retry` repeating for 25+ minutes with no progress. vLLM's
engine core always goes through `torch.distributed`'s rendezvous even at
`tensor_parallel_size=1`, and it was binding to whatever IP
`socket.gethostbyname(socket.gethostname())` resolved to (a `10.5.x.x`
VPN-adjacent address) rather than loopback -- an address that had gone stale.
Fixed with `VLLM_HOST_IP=127.0.0.1` to force the rendezvous onto loopback. Also
found and killed stray orphaned child processes (a `resource_tracker` and a
`socket`-holding worker) still bound to the old rendezvous port after the parent
was killed -- `pkill -f "vllm serve"` alone did not clean these up.

**Validated with the real eval harness, not just manual curl.** Added
`--baseline-api-key`/`--candidate-api-key` flags to `eval_harness.py` (it never
had auth support before, since every backend so far had been unauthenticated) and
ran the harness with `--baseline` on the direct backend and `--candidate` through
the proxy, for both Ollama and vLLM routes. Zero regressions either way --
concrete proof the proxy path is behaviorally transparent, exactly the check
DESIGN.md's Phase 1 rollout plan specified.

**OpenClaw's `--model <provider>/<id>` CLI override has a real, separate bug from
config-driven model selection.** Added a `litellm` entry under `models.providers`
in `openclaw.json` (same shape Hermes uses successfully via its `custom` provider
type). Direct HTTP calls to the proxy work fine, verified with real curl.
Invoking it via `openclaw agent --model litellm/ollama-default` fails with
`Error: Persisted plugin install records are invalid` -- traced to
`requireLoadablePluginInstallRecordState` in
`src/plugins/installed-plugin-index-record-reader.ts`, called from
`loadInstalledPluginIndexInstallRecords`, itself invoked (per
`missing-configured-plugin-install.candidates.ts` and
`provider-install-catalog.ts`) as part of a plugin-install-catalog lookup that
`--model`'s override path triggers but normal config-driven resolution does not.
Confirmed by isolation: setting the exact same `litellm/ollama-default` value as
`agents.defaults.model.primary` instead of passing `--model` bypasses this
error entirely and reaches the real LLM call. Root cause not fully chased inside
that catalog-lookup path (the raw `installed_plugin_index` SQLite row is `{}`,
valid; `PRAGMA integrity_check` passes) -- confirmed fresh-process-safe (not an
in-memory cache issue) but not further isolated.

**Real, separate, second bug found past that, and fully fixed.** Once routed
through `agents.defaults.model.primary` (bypassing the `--model` bug above), the
request reached LiteLLM and got a real `400` from LiteLLM's own
`user_api_key_auth()`: `No connected db.` -- a documented, known-misleading
LiteLLM message (BerriAI/litellm#2532, #4880) that in LiteLLM's own auth code
means "no valid credential was recognized," not an actual database requirement.
Confirmed via LiteLLM's own `--detailed_debug` request log: the failing request
arrived with `'api_key': ''` -- OpenClaw was sending an empty key, regardless of
whether `apiKey` in `openclaw.json` was set to a literal `sk-...` value or an
env-var-name marker. Traced the real cause: a hand-rolled custom provider entry
(`models.providers.litellm`, `api: "openai-completions"`) never gets real
credential wiring in OpenClaw -- that's not a bug so much as an unsupported
shape. OpenClaw's own bundled `vllm` extension
(`extensions/vllm/index.ts`, real source, already npm-shipped in
`2026.8.1-beta.2`) exists specifically for "local/self-hosted
OpenAI-compatible server," built on the plugin-SDK's
`defineSelfHostedOpenAICompatibleProvider()` helper, which *does* wire
`VLLM_API_KEY` through to the outbound request correctly. Since LiteLLM is
genuinely OpenAI-compatible, pointing that already-bundled `vllm` provider's
`baseUrl` at `http://127.0.0.1:4000/v1` (instead of a raw vLLM instance) and
setting `VLLM_API_KEY` to the LiteLLM master key works with zero new code --
no plugin needed to be written. Verified with a real agent turn: clean,
structured, valid tool calls, zero auth errors, full round trip through
OpenClaw -> `vllm` provider -> LiteLLM proxy -> Ollama -> `llama3.1:8b`. Lesson:
when a provider needs real credentialed auth (not just an unauthenticated local
endpoint like the `ollama` provider), reuse OpenClaw's `vllm` provider type
rather than hand-rolling a custom one -- the custom-provider auth path is a real
config-schema gap for this exact shape.

## OpenClaw beta.2 (npm, real release) resolves the schema bug; small local models narrate tool calls instead of executing them

Upgrading from the hand-built source checkout to the real published `openclaw@2026.8.1-beta.2` npm release (one patch beyond the broken `beta.1`) resolved the schema self-inconsistency below cleanly: `[gateway] ready` on first try, zero schema errors across multiple restarts, Buzz connected on first attempt instead of needing a reconnect loop. This is the real, sustainable fix — track the published release channel, not a hand-built source tree. Confirmed via `npm view openclaw dist-tags`: OpenClaw has a genuine stable/production channel (`latest: 2026.7.1-2`) separate from `beta`; this stack is on beta only because the Buzz plugin requires `>=2026.7.2`, which has not shipped as a stable release yet. The instability documented below is a consequence of that specific version-floor requirement, not evidence OpenClaw itself is broadly unstable software.

Separately: tried three different local models (`deepseek-r1:8b`, `llama3.1:8b`, and implicitly `muse-glimmer:30b-mlx` via the earlier stall) as the primary agent model through OpenClaw's Ollama integration. All three, when asked to use the llmfit-advisor skill, narrated an intended tool call as plaintext (`[[toolname(args)]]`, referencing OpenClaw's own AGENTS.md convention) instead of completing a real structured tool call or actually executing it. No server-side error in the gateway log — the request completes successfully from OpenClaw's perspective, the model just doesn't follow through. This is distinct from (and didn't reproduce) the `eval_harness.py` finding that a 4-bit MLX quant lost tool-calling via vLLM's dedicated `--tool-call-parser hermes` — that was through vLLM's OpenAI-compatible endpoint with a purpose-built parser; this is through Ollama's native tool support via OpenClaw's own agent loop, a different code path entirely, and evidently a less reliable one for small local models. Not chased to root cause — real, disclosed limitation of running small local models as OpenClaw's primary agent model today, not a config mistake we found and fixed.

## OpenClaw 2026.8.1-beta.1: DB schema self-inconsistency (confirmed unconditional)

Follow-up to the entry below: re-tested by fully wiping `openclaw.sqlite` and doing
a clean process restart (not `SIGUSR1` in-place). Gateway came up fine and stayed
healthy for hours — but on the *next* restart, `openclaw doctor --fix` and a fresh
`openclaw gateway run` both refused to start, reporting schema 7 on a database that
had been empty **minutes** earlier. This rules out `doctor --fix` as the cause (it
wasn't run in between) — something in the gateway's own ordinary runtime writes
schema-7-shaped state within minutes of a cold start, while every startup path
(`gateway run`, `doctor`) enforces schema ≤6. This is not an occasional/timing bug;
treat any process restart on this build as having a real chance of permanently
wedging the local state DB. Worked around each time the same way (wipe
`openclaw.sqlite`, accept the lost session/task-flow state), but stopped chasing
root cause — genuinely unfixable without an upstream release. The llmfit-advisor
skill itself (see below) was proven correct independent of this: ran
`llmfit recommend --json` directly and got the exact structured output the skill's
own `SKILL.md` documents the agent as consuming.

## OpenClaw 2026.8.1-beta.1: DB schema self-inconsistency

Installed build writes/expects a `openclaw.sqlite` state DB at schema version 7 in
some code paths (`openclaw doctor --fix`'s migration logic) but the gateway runtime's
own reader only supports schema 6, even on a freshly created, empty DB file from the
same install. This is a genuine bug in the currently-published beta
(`2026.8.1-beta.1`, no newer version exists on npm as of this writing) — not
something fixable locally. It surfaces as repeated
`device-pair: notify poll failed: ... uses newer schema version 7; this build
supports 6` warnings and a `heartbeat failed: Legacy workspace setup state requires
migration` error, and appears to degrade the agent-dispatch pipeline independently
of the Buzz channel connection (which stays healthy throughout). Documented here
rather than chased further — OpenClaw's Buzz *connection* is proven working
end-to-end (config schema, identity, allowlist); the reply-generation path is
blocked on this upstream issue, not on anything in this repo.

## `@openclaw/buzz` npm package is broken upstream

Published as a `0.0.0` stub with no `openclaw.extensions` field. The plugin only
works installed from source (`extensions/buzz` inside the full monorepo, `pnpm
install`'d) — sparse checkout fails because the plugin imports
`openclaw/plugin-sdk/*` as monorepo-sibling packages that don't exist outside a full
workspace checkout.

## `pkill -f "hermes gateway run"` / `"openclaw gateway run"` unreliable

The real running process names don't reliably substring-match those patterns
(`openclaw-gateway`, not a literal `openclaw gateway run` argv). Always confirm the
exact PID via `ps aux` before killing, rather than trusting `pkill -f` silently
matched (or didn't).
