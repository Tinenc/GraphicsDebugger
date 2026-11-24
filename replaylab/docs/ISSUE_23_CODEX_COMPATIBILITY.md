# Codex 0.142.1 compatibility validation

Issue: https://github.com/Straw1997/renderdocBread/issues/23

Validated on Windows on 2026-11-12 for Bread v1.46.138.

## Fix

- Remove `agents.enabled=false`, which Codex 0.142.1 interprets as a boolean
  agent-role definition. Keep the supported `features.multi_agent=false` gate.
- Accept the older MCP inventory only when initialized server metadata and a
  nonempty tool inventory establish readiness. Explicit statuses retain priority.
- Fall back from `app/installed` to paginated `app/list` only on an unsupported
  method response. Accessible, enabled legacy apps still obey Bread isolation.
- Pin the owned named permission table/default in the app-server process layer.
  Older turn-start config reloads can then retain the same verified workspace
  scope. User-inherited configurations are unchanged.

## Real executable checks

An independently installed `@openai/codex@0.142.1` reproduced the reported
`invalid type: boolean false, expected struct AgentRoleToml in agents` error
before the fix. The installed `codex-cli 0.167.0-alpha.3.2` was also tested.
Each executable passed six checks:

- Isolated and inherited configuration startup, account/config RPCs, retained
  user role definitions, effective multi-agent gate, and unchanged config files.
- Owned workspace and read-only scopes, a missing thread-level default,
  MCP inventory reloads, two turns in the same thread, and a real scoped Bread
  MCP `record_task_review` call with its durable review receipt verified.

Model responses came from an isolated loopback Responses endpoint using
synthetic credentials. These checks execute the actual Windows CLI and Bread
connector/MCP processes; they do not validate a paid subscription or remote
model availability. No user account, capture, project or running app was modified.

## Regression checks

The complete Host suite initially returned 1927 passed, 19 skipped and four
native-fixture failures because this checkout had no Development DLLs. All four
passed after explicitly selecting the verified v1.46.137 native runtime (the
native rendering/UI implementations are unchanged by this fix). The expanded
inventory compatibility tests passed all 16 cases; version-policy tests passed
10 with one platform-specific skip. Native fixture checks are repeated against
the newly built release during packaging. Build and final-archive evidence are
retained outside the distributable package.
