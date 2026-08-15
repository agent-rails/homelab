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
