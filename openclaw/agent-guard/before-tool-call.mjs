// agent-guard <-> OpenClaw integration: a `before_tool_call` hook that routes every
// OpenClaw tool call through agent-guard's real policy engine before dispatch.
//
// Why this integration point (not the MCP wrapper): OpenClaw dispatches its builtin
// tools (exec, write, edit, apply_patch, delete, sessions_spawn, ...) internally,
// NOT through `mcp.servers`. agent-guard's zero-code `guard mcp` wrapper only sits in
// front of external MCP servers, so it structurally cannot see a builtin `exec`. The
// `before_tool_call` hook fires on EVERY tool call including builtins, can block /
// require-approval / rewrite, and fails closed on timeout -- so it is the only seam
// that reaches the exact tool surface the documented tool-narration bug fabricates
// (see homelab DECISIONS.md).
//
// The hook is a thin adapter: it shells one `guard check` per call and maps the
// verdict onto OpenClaw's hook result. All policy + audit logic stays in agent-guard;
// nothing about the decision is reimplemented here.
//
// Load it (opt-in) via plugins.load.paths -- see README.md in this directory.

// No `openclaw/plugin-sdk/*` import on purpose: a standalone file loaded via
// plugins.load.paths lives outside OpenClaw's node_modules, so Node ESM cannot resolve
// the bare `openclaw` specifier from here. definePluginEntry is just a plain-object
// factory (verified in the installed dist: id/name/description/configSchema/register,
// no brand symbol), so the default export below is the exact shape it produces.

import { spawnSync } from "node:child_process";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));

// All three are env-overridable so the live config can point at the venv-installed
// CLI and a durable audit path without editing this file.
const GUARD_BIN = process.env.AGENT_GUARD_BIN || "guard";
const POLICY_PATH = process.env.AGENT_GUARD_POLICY || join(HERE, "policy.yaml");
// Audit defaults to the OpenClaw state dir (never the repo working tree).
const AUDIT_PATH =
  process.env.AGENT_GUARD_AUDIT ||
  join(process.env.OPENCLAW_STATE_DIR || join(homedir(), ".openclaw"), "agent-guard-audit.jsonl");
const CHECK_TIMEOUT_MS = 10_000;

// Map agent-guard's exit-code convention (0 allow, 3 deny, 4 require_human) onto an
// OpenClaw before_tool_call result. Any other outcome -- spawn failure, timeout,
// non-JSON output, unexpected code -- returns a block: fail closed, never a silent
// allow. This mirrors agent-guard's own default: deny posture on the OpenClaw side.
function decide(toolName, params, agentId) {
  const payload = JSON.stringify({ tool: toolName, args: params ?? {} });
  const result = spawnSync(
    GUARD_BIN,
    [
      "check",
      "--policy",
      POLICY_PATH,
      "--audit",
      AUDIT_PATH,
      "--agent-id",
      `openclaw:${agentId ?? "unknown"}`,
      "--json",
    ],
    { input: payload, encoding: "utf8", timeout: CHECK_TIMEOUT_MS },
  );

  if (result.error || result.status === null) {
    return {
      block: true,
      blockReason: `agent-guard unavailable (${result.error?.message ?? "timeout"}); denied fail-closed`,
    };
  }

  let reason = "policy decision";
  try {
    const parsed = JSON.parse(result.stdout);
    if (parsed?.reason) reason = parsed.reason;
  } catch {
    return { block: true, blockReason: "agent-guard returned unparseable output; denied fail-closed" };
  }

  if (result.status === 0) return undefined; // allow -> no decision, tool proceeds
  if (result.status === 3) return { block: true, blockReason: `agent-guard: ${reason}` };
  if (result.status === 4) {
    return {
      requireApproval: {
        title: `agent-guard gate: ${toolName}`,
        description: reason,
        severity: "warning",
        timeoutMs: 120_000,
      },
    };
  }
  return { block: true, blockReason: `agent-guard unexpected exit ${result.status}; denied fail-closed` };
}

export default {
  id: "agent-guard-gate",
  name: "agent-guard tool gate",
  description: "Route every OpenClaw tool call through agent-guard's policy engine before dispatch.",
  configSchema: { type: "object", additionalProperties: false, properties: {} },
  register(api) {
    api.on(
      "before_tool_call",
      (event, ctx) => decide(event.toolName, event.params, ctx?.agentId),
      { priority: 100 },
    );
  },
};
