<#
    setup_claude_code_acp.ps1

    Installs everything needed to connect a Claude subscription to ReplayLab over
    ACP without changing the user's global Claude login:
      - the official Claude Code CLI (does Anthropic's real subscription login), and
      - the ACP reference adapter `@agentclientprotocol/claude-agent-acp`
        (formerly @zed-industries/claude-code-acp), which reuses Claude Code's stored
        credentials. No API key, no reverse-engineered authentication.

    ReplayLab's Save and Connect flow runs `claude auth login` with the selected
    connection's isolated CLAUDE_CONFIG_DIR. Do not run that login globally here:
    otherwise the credential would be stored outside the profile ReplayLab launches.

    NOTE: the archived community package `acp-claude-code` (Xuanwo) is NOT supported
    any more -- it ignores MCP servers passed by the client, never asks for tool
    permissions and does not forward thinking, so ReplayLab's child Agents never
    received the RenderDoc tools with it.  Uninstall it if present.

    Using a Claude Pro/Max subscription through a third-party ACP client must follow
    Anthropic's third-party authentication and usage terms.

    Run in Windows PowerShell (NOT as Administrator):
        powershell -ExecutionPolicy Bypass -File .\setup_claude_code_acp.ps1

    If a step reports a command is not found right after installing it, open a NEW
    PowerShell window (so PATH refreshes) and re-run this script -- it skips steps
    that are already done.
#>

$ErrorActionPreference = "Stop"
function Have($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

Write-Host "== 1/3  Node.js 22+ (required by the claude-agent-acp adapter) ==" -ForegroundColor Cyan
if (Have node) {
    $nodeVersion = (node --version)
    Write-Host ("Node.js found: " + $nodeVersion)
    if ([int]($nodeVersion.TrimStart('v').Split('.')[0]) -lt 22) {
        Write-Host "claude-agent-acp requires Node.js >= 22. Upgrade with: winget upgrade OpenJS.NodeJS.LTS" -ForegroundColor Yellow
    }
} else {
    Write-Host "Installing Node.js LTS via winget..."
    winget install --id OpenJS.NodeJS.LTS --accept-source-agreements --accept-package-agreements
    Write-Host "Node.js installed. If 'node' is not found below, open a NEW terminal and re-run." -ForegroundColor Yellow
}

if (-not (Have npm)) {
    Write-Host "npm not on PATH yet. Open a NEW terminal (so Node's PATH applies) and re-run." -ForegroundColor Yellow
    exit 1
}

Write-Host "== 2/3  Claude Code CLI (provides the 'claude' login command) ==" -ForegroundColor Cyan
if (Have claude) {
    Write-Host "Claude Code CLI already available: " -NoNewline; claude --version
} else {
    npm install -g "@anthropic-ai/claude-code"
}

Write-Host "== 3/3  claude-agent-acp adapter (npm global) ==" -ForegroundColor Cyan
if (Have acp-claude-code) {
    Write-Host "Removing the archived 'acp-claude-code' package (unsupported)..." -ForegroundColor Yellow
    npm uninstall -g acp-claude-code | Out-Null
}
if (Have claude-agent-acp) {
    Write-Host "Adapter found: " -NoNewline; claude-agent-acp --version
    Write-Host "Updating to the latest release..."
    npm install -g "@agentclientprotocol/claude-agent-acp@latest"
} else {
    npm install -g "@agentclientprotocol/claude-agent-acp@latest"
}

Write-Host ""
Write-Host "Dependencies are ready. In qrenderdoc:" -ForegroundColor Green
Write-Host "  1) Provider list -> 'Claude Code (ACP subscription)'"
Write-Host "  2) Turn ON allow_unsandboxed_agent (required for any ACP agent)"
Write-Host "  3) Leave the model blank to use Claude Code's default"
Write-Host "  4) Click Save and Connect; ReplayLab opens the profile-isolated official login"
