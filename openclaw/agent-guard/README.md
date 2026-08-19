# agent-guard tool gate for OpenClaw

First real integration of [agent-guard](https://github.com/agent-rails/agent-guard)'s
policy gate in front of OpenClaw's actual tool dispatch in this homelab. Opt-in,
fail-closed, every decision audited.

## Why this exists

OpenClaw's documented tool-narration bug (see `../../DECISIONS.md`) is a small local
model (`llama3.1:8b`) fabricating tool calls. The dangerous case is not the pure
hallucination that OpenClaw only displays as text — that executes nothing — but the
one OpenClaw's own `tool-call-repair` layer promotes into a *real* executed `exec` /
`write`. This gate sits at the real tool-dispatch boundary and denies or human-gates
those before they run.

## The integration point (investigated from source, not assumed)

Two candidate seams exist; only one reaches the tool surface the bug fabricates:

- `guard mcp --policy ... -- <server>` (agent-guard's zero-code MCP wrapper) only sits
  in front of **external** MCP servers configured under `mcp.servers`. OpenClaw
  dispatches its **builtin** tools (`exec`, `write`, `edit`, `apply_patch`, `delete`,
  `sessions_spawn`, ...) internally, so the MCP wrapper structurally **cannot** see a
  builtin `exec`. Not usable for this target.
- OpenClaw's **`before_tool_call` plugin hook** fires on *every* tool call including
  builtins, can return `block` / `requireApproval` / `params`-rewrite, and **fails
  closed on timeout** (15s → reject). This is the seam used here.

## What's in this directory

| File                    | Purpose                                                                       |
| ----------------------- | ----------------------------------------------------------------------------- |
| `before-tool-call.mjs`  | The plugin: a `before_tool_call` hook that routes each call through agent-guard |
| `openclaw.plugin.json`  | Plugin manifest (required — OpenClaw's discovery is manifest-driven)           |
| `package.json`          | Declares `openclaw.extensions` → the hook entry                               |
| `policy.yaml`           | agent-guard policy written for OpenClaw's real builtin tool names              |

The hook is a thin adapter: it shells one `guard check --policy policy.yaml --audit ...
--json` per tool call and maps agent-guard's verdict onto OpenClaw's hook result. All
policy and audit logic stays in agent-guard — nothing is reimplemented in JS. It
imports no `openclaw/*` package (a standalone file can't resolve that bare specifier),
so it loads regardless of module-resolution context.

## Decision mapping

| agent-guard (`guard check` exit) | OpenClaw hook result                          | Effect                          |
| -------------------------------- | --------------------------------------------- | ------------------------------- |
| `allow` (0)                      | `undefined`                                   | tool proceeds                   |
| `require_human` (4)              | `{ requireApproval: {...} }`                  | operator prompted (`/approve`)  |
| `deny` (3)                       | `{ block: true, blockReason }`                | blocked before execution        |
| spawn error / timeout / other    | `{ block: true }`                             | **fail closed** — never allowed |

## Policy posture (conservative first cut)

`default: deny`. Read-only tools (`read`, `ls`, `grep`, `find`, `tree`, `web_fetch`,
`web_search`) allow. Destructive shell shapes (`rm -rf`, `mkfs`, `dd`, fork bomb, ...)
and credential paths (`.ssh/`, `.openclaw/`, `id_rsa`, `/etc/shadow`, `.env`) hard-deny.
Everything mutating (`exec`, `write`, `edit`, `apply_patch`, `move`, `delete`,
`sessions_spawn`, `message`, `browser`) is `require_human`. Tighten `gate-exec` to a
bin allowlist once real usage is observed.

## How to opt your live OpenClaw into it

Not wired into `~/.openclaw/openclaw.json` — that holds real secrets and stays local
(see repo README "What's NOT here"). To enable it on your live instance, a human does:

```bash
# 1. Make agent-guard's `guard` CLI resolvable (pipx, or point AGENT_GUARD_BIN at a venv):
#    export AGENT_GUARD_BIN=/path/to/agent-guard/.venv/bin/guard   (in the gateway's env)

# 2. Register this directory as a load-path plugin and enable it:
openclaw config set plugins.load.paths '["/Users/fo/dev/agent-rails/homelab/openclaw/agent-guard"]' --json
openclaw config set plugins.entries.agent-guard-gate.enabled true

# 3. Restart the gateway, then prove the hook registered:
openclaw gateway restart
openclaw plugins inspect agent-guard-gate --runtime --json   # expect typedHooks[].name = before_tool_call
```

Optional env overrides (set in the gateway process env): `AGENT_GUARD_BIN`,
`AGENT_GUARD_POLICY`, `AGENT_GUARD_AUDIT` (defaults to
`$OPENCLAW_STATE_DIR/agent-guard-audit.jsonl`).

Because the hook shells `guard check` per call, the `guard` CLI must resolve in the
gateway's environment. If it does not, every tool call fails closed (denied) — loud,
not silent.

## What this proves, and what it does not

Verified live against a throwaway OpenClaw instance (isolated `OPENCLAW_STATE_DIR`,
separate port — the live daily-use gateway was never touched):

- OpenClaw's runtime **loads the plugin and registers the hook**: `plugins inspect
  --runtime` reports `typedHooks[0].name = before_tool_call`, `hookCount = 1`.
- The **decision + audit path the hook delegates to** is deterministic and real: across
  `read` / `exec rm -rf` / `write` / `write .env` / `sessions_spawn` / unknown-tool the
  verdicts are allow / deny / require_human / deny / require_human / deny, with one
  structured audit record written per decision.

Not closed in-session: a **model-driven** tool call firing the registered hook
end-to-end. `llama3.1:8b` hard-fails any tool-enabled request through OpenClaw's agent
loop (plain inference works; add tools and the LLM request errors out) — the same
small-local-model tool-calling wall documented in `DECISIONS.md`. The model never emits
a tool call, so the hook correctly has nothing to fire on. This is a model-reliability
gap, not an integration gap — and it is precisely the failure mode the gate backstops.

Structural coverage limit, stated plainly: the hook fires on tool calls that reach
dispatch — including a fabricated call OpenClaw's `tool-call-repair` promotes into a
real executed one. It does **not** fire on a pure hallucination that OpenClaw only
prints as text and never dispatches. That path executes nothing; its harm is misleading
output, an output-integrity problem upstream of any tool-authz boundary, not something
this or any dispatch gate can catch.

## Not wired: per-agent velocity limits

agent-guard ships `VelocityLimiter` (cap N calls/window/agent — the right tool against
a hallucinating model spamming calls). It does **not** fit this integration as built:
the hook shells a fresh `guard check` per call, and the limiter is in-memory only, so
counters reset every call and never accumulate. Using it meaningfully needs a persistent
`guard` sidecar the hook talks to (a durable counter backend). Deferred as the honest
next step, not forced into a shape it doesn't fit.
