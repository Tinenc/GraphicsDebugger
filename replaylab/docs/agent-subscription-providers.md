# Agent subscription provider matrix

Verified against the vendors' official documentation on 2026-09-05. These are
subscription/CLI connections, not API-key adapters. ReplayLab invokes only the
fixed command in its built-in provider catalog, stores no vendor subscription
token in the profile JSON, and performs an ACP/app-server handshake without
sending a chat prompt. Every built-in vendor uses one durable, profile-scoped
state root: ReplayLab sets the vendor's documented root variable where one
exists and redirects the process home for CLIs which document only a home-based
store. Login, connection testing and task execution receive the same root;
ReplayLab neither parses nor copies credentials. A vendor may still use
operating-system credential storage independently of those paths.

| Provider | Official authentication path used by ReplayLab | Agent transport | Model selection |
| --- | --- | --- | --- |
| OpenAI Codex | Codex app-server account login; the app-server returns the browser/device authorization information. | `codex app-server` | app-server model catalog |
| GitHub Copilot CLI | `copilot login --web-flow` or `copilot login --device-code` | `copilot --acp` over stdio | Imported when the ACP session exposes a model config option; otherwise use the CLI default or an exact supported model. ACP itself cannot run `/login`. |
| Cursor CLI | `agent login` opens Cursor's official browser flow and uses the saved Cursor subscription. ReplayLab invokes the equivalent unambiguous `cursor-agent` alias before the handshake. Current Cursor builds can leave a redundant ACP `authenticate(cursor_login)` pending after that login, so ReplayLab deliberately skips only that duplicate call and lets `session/new` verify the stored account. | `cursor-agent acp` over stdio | Imported when the ACP session exposes model configuration; otherwise use Cursor's configured default. |
| Google Gemini CLI | Start `gemini`, choose **Sign in with Google**, and complete the browser flow in the contained terminal. | `gemini --acp` over stdio | Imported when exposed in ACP session configuration; otherwise use the configured default. |
| Qwen Code | Start `qwen`, run `/auth`, choose Alibaba ModelStudio Coding Plan or another supported API provider, then exit after verification. The former Qwen OAuth path was discontinued on 2026-04-15. | `qwen --acp` over stdio | Imported when exposed in ACP session configuration; otherwise use the configured default or an exact provider model. |
| Kimi Code CLI | `kimi login`; the CLI uses an RFC 8628 device-code flow and prints the verification URL/code. | `kimi acp` over stdio | Kimi ACP can return model/session configuration options, which ReplayLab imports when present. |
| Tencent CodeBuddy Code / Hunyuan | Start isolated `codebuddy`, choose **Log in via Chinese Site**, and complete the browser flow with the CodeBuddy account that owns the subscription. | `codebuddy --acp` over stdio, pinned to the standard `cli` agent with connection-scoped user settings, no ambient global/project/local MCP or settings, no transcript persistence and no built-in tools; only the exact injected `mcp__ReplayLab` server is pre-approved | Imported when the ACP session exposes model configuration; Hunyuan/model availability and quota remain controlled by the signed-in CodeBuddy plan. |
| Anthropic Claude Code | ReplayLab opens `claude auth login` with this connection's profile-isolated `CLAUDE_CONFIG_DIR`; the user completes Anthropic's official login in that terminal. | Built-in `claude-agent-acp` over stdio. This is the Agent Client Protocol reference adapter around the official Claude Code CLI, not an Anthropic-native ACP server. ReplayLab supplies one strict project MCP, the selected project Skills and `AskUserQuestion` elicitation. | A selected id is passed as `ANTHROPIC_MODEL`; blank follows the subscription default. Availability remains controlled by the signed-in plan. |
| Custom ACP | ReplayLab never guesses a login command. Authentication must already be configured by the user. | User-entered ACP stdio command | Handshake discovery when available, or manual exact model. The command is never automatically trusted. |

For a new built-in provider connection, qrenderdoc visibly checks trust only for
the read-only official catalog command and **Save and Connect** immediately runs
the relevant login and no-prompt handshake. Custom ACP continues to require an
explicit trust checkbox after the user reviews every argument. A missing CLI is
reported with its exact official installation command/link.

Before every login, probe and task, ReplayLab regenerates the provider-specific
containment files and launch flags. Copilot receives exact
`--available-tools/--allow-tool` entries and no built-in MCPs; Gemini/Qwen
receive an exact MCP server/tool allow-list with built-ins disabled; Kimi gets a
launch-scoped Agent file; Claude gets strict project settings, the one task MCP
and copied Skills; Cursor and CodeBuddy receive their documented isolated
policy/configuration. These are automatic connection behavior, not settings a
user must add to a global vendor profile.

ACP standardizes session configuration options, including a `model` category,
but it does not guarantee that every agent will publish a model list. The UI
therefore leaves manual entry available and says when the handshake exposed no
standard list instead of treating an empty list as a failure.

Cursor runs with profile-scoped `CURSOR_CONFIG_DIR`, `HOME`, and `USERPROFILE`
values plus a fresh, ReplayLab-owned working directory for every Agent turn.
Both user-level and project-level `mcp.json` files are pinned empty so Cursor
does not discover the user's editor MCP servers. ReplayLab rewrites Cursor's CLI
policy before each launch so ambient shell, arbitrary file access, web fetch,
and user-configured MCP servers do not enter the RenderDoc task. ACP v1 does not
expose a host-owned dynamic-tool callback, so a capture-aware child Agent receives
exactly one first-party `ReplayLabMCP` server filtered to that Skill task. Every
ACP provider automatically approves only a session-bound, unique tool call whose
structured input names the exact `ReplayLab` server and one tool from that turn's
Host allow-list. It prefers `allow_once`; an `allow_always` fallback is confined to
the fresh process, session, and task-filtered proxy. Human-readable titles are
never authorization inputs. The per-task Host filter remains authoritative. The
live tool manifest, frozen capture scope, effect ceiling, concrete artifact
postconditions and rollback requirements are checked by Bread; ACP prose
cannot grant or revoke them. Capture-free conversation needs no MCP server. An
unrelated ACP permission request is denied as an ordinary tool result so the
native Agent can choose an admitted operation; malformed or replayed requests
fail closed.

### ACP extension compatibility

Bread follows the [ACP extensibility contract](https://agentclientprotocol.com/protocol/v1/extensibility):
unhandled extension notifications are ignored, and unsupported extension requests
receive JSON-RPC `-32601` (Method not found), allowing the provider to fall back.
Neither case cancels a turn or grants a capability. This applies to every ACP
provider, including CodeBuddy's `_codebuddy.ai/command`, artifact, checkpoint and
initialization notifications. The Python ACP SDK removes the wire method's leading
underscore before invoking Bread's extension callbacks.

Notification payloads are opaque: Bread does not execute commands, open artifacts,
approve permissions, or treat them as assistant text or task-completion evidence.
Native file/terminal callbacks, task-scoped MCP permissions, malformed/replayed
permission checks and provider stop/error handling remain authoritative. Supported
Cursor question/plan requests keep their provider-specific handling.

The connection test checks the handshake without spending model quota. A successful
connection alone therefore does not prove a chat turn can complete; the separate
model test sends a short prompt. Extension regressions are covered on both the model
test and the main chat session through the real ACP SDK and a local stdio fixture,
including multiple turns, unknown requests and a refused turn. These tests need no
vendor account and do not claim account-level CodeBuddy verification.

Subscription connections do not enter a Bread planner or mandatory child-Agent
router. ACP and Codex keep their vendor-native Agent loop and call the same
task-scoped Bread MCP used by the API path. The Agent chooses tools and optional
Skills; the Host checks concrete write and replay effects. No terminal report
form, forced verification follow-up, or Host-selected next step is required.

Codex app-server turns use the server-advertised built-in `:read-only`
permission profile plus the exact ReplayLab runtime roots on both `thread/start`
and `turn/start`. ReplayLab sets `project_doc_max_bytes=0`, clears project
document fallbacks, disables shell/Apps/multi-agent features, injects exactly one
task-scoped ReplayLab MCP server, and requires an empty ambient instruction-source
list. The active permission profile, roots, network setting, approval policy,
MCP inventory and App isolation are verified before a turn is accepted. Protocol
incompatibility is a non-retryable capability error.

## 2026-09-05 no-prompt verification snapshot

- Codex completed the deployed qrenderdoc → app-server → task MCP →
  ReplayLab bridge chain and a real capture task.
- Installed Cursor CLI completed `initialize` + `session/new` from its isolated
  profile and returned its model configuration without a prompt. A real probe
  also reproduced the duplicate `authenticate(cursor_login)` hang that the
  connection-specific skip fixes.
- Installed `claude-agent-acp` completed ACP v1 initialization from an isolated
  `CLAUDE_CONFIG_DIR` and advertised MCP/session capabilities without a prompt.
- Copilot, Gemini, Qwen, Kimi and CodeBuddy were not installed on this machine.
  Their official configuration contracts and launch shapes are covered by
  automated tests, but account-level end-to-end success is intentionally not
  claimed until each CLI and account is available.

## Official references

- [Codex CLI](https://developers.openai.com/codex/cli/)
- [Codex configuration reference](https://developers.openai.com/codex/config-reference)
- [GitHub Copilot CLI command reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference)
- [GitHub Copilot ACP server](https://docs.github.com/en/copilot/reference/copilot-cli-reference/acp-server)
- [Install GitHub Copilot CLI](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli)
- [Cursor CLI ACP](https://cursor.com/docs/cli/acp)
- [Cursor CLI MCP discovery](https://cursor.com/docs/cli/mcp)
- [Cursor CLI authentication](https://cursor.com/docs/cli/reference/authentication)
- [Cursor CLI installation](https://cursor.com/docs/cli/installation)
- [Cursor CLI configuration](https://cursor.com/docs/cli/reference/configuration)
- [Cursor CLI permissions](https://cursor.com/docs/cli/reference/permissions)
- [Gemini CLI installation](https://github.com/google-gemini/gemini-cli/blob/main/docs/get-started/installation.mdx)
- [Gemini CLI authentication](https://github.com/google-gemini/gemini-cli/blob/main/docs/get-started/authentication.mdx)
- [Gemini CLI ACP mode](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/acp-mode.md)
- [Qwen Code quickstart](https://qwenlm.github.io/qwen-code-docs/en/users/quickstart/)
- [Qwen Code authentication](https://qwenlm.github.io/qwen-code-docs/zh/users/configuration/auth/)
- [Kimi CLI command reference](https://www.kimi.com/code/docs/en/kimi-code-cli/reference/kimi-command)
- [Kimi ACP reference](https://www.kimi.com/code/docs/en/kimi-code-cli/reference/kimi-acp)
- [CodeBuddy Code quickstart and Chinese-site login](https://www.codebuddy.cn/docs/cli/quickstart)
- [CodeBuddy Code ACP mode](https://www.codebuddy.cn/docs/cli/acp)
- [CodeBuddy plan management](https://www.codebuddy.cn/docs/ide/Account/plan)
- [CodeBuddy CLI isolation flags](https://www.codebuddy.cn/docs/cli/cli-reference)
- [CodeBuddy custom configuration directory](https://www.codebuddy.cn/docs/cli/installation)
- [CodeBuddy permission rules](https://www.codebuddy.cn/docs/cli/permissions)
- [Claude Code authentication](https://code.claude.com/docs/en/authentication)
- [Claude Code environment variables](https://code.claude.com/docs/en/env-vars)
- [Claude Code ACP reference adapter](https://github.com/agentclientprotocol/claude-agent-acp)
- [Claude ACP permission extension](https://github.com/agentclientprotocol/claude-agent-acp/blob/main/docs/permission-extension.md)
- [Gemini CLI configuration](https://github.com/google-gemini/gemini-cli/blob/main/docs/reference/configuration.md)
- [Qwen Code MCP configuration](https://qwenlm.github.io/qwen-code-docs/en/users/features/mcp/)
- [Kimi configuration files](https://www.kimi.com/code/docs/en/kimi-code-cli/configuration/config-files)
- [Kimi data locations](https://www.kimi.com/code/docs/en/kimi-code-cli/configuration/data-locations.html)
- [Kimi custom Agents](https://www.kimi.com/code/docs/en/kimi-code-cli/customization/agents)
- [ACP session configuration options](https://agentclientprotocol.com/announcements/session-config-options-stabilized)
- [ACP authentication methods](https://agentclientprotocol.com/rfds/auth-methods)

Claude connection validation also runs `claude auth status --json` with the same isolated `CLAUDE_CONFIG_DIR` as the actual task. ACP initialization and model discovery alone are not account authentication. `setup-token` generates a token without storing CLI login state and is not used by Save and Connect. These checks send no model prompt and do not establish model entitlement or request-time access.
