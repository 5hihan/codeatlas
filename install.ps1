<#
.SYNOPSIS
  CodeAtlas installer for Windows.

  iwr -useb https://raw.githubusercontent.com/5hihan/codeatlas/main/install.ps1 | iex

  With a platform:   & ([scriptblock]::Create((iwr -useb https://raw.githubusercontent.com/5hihan/codeatlas/main/install.ps1))) codex
  From a checkout:   .\install.ps1 [claude|codex|cursor|all|auto] [-Update] [-Uninstall]

  What it does: installs uv if missing, installs the `codeatlas` command with uv, then runs
  `codeatlas init --global <platform>` which registers the MCP server + agents for Claude Code (plugin),
  Codex (~/.codex/config.toml + ~/.agents/skills/codeatlas) and Cursor. Re-run with -Update to upgrade.
#>
param(
    [Parameter(Position = 0)][ValidateSet("auto", "claude", "codex", "cursor", "all")][string]$Platform = "auto",
    [switch]$Update,
    [switch]$Uninstall,
    [switch]$Help
)
$ErrorActionPreference = "Stop"
$Repo = "https://github.com/5hihan/codeatlas"
$Source = if ($env:CODEATLAS_SOURCE) { $env:CODEATLAS_SOURCE }
          elseif ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot "pyproject.toml"))) { $PSScriptRoot }
          else { "git+$Repo" }

function Write-Step($msg) { Write-Host "  > $msg" -ForegroundColor Cyan }

if ($Help) { Get-Help $MyInvocation.MyCommand.Path -Detailed; return }

Write-Host ""
Write-Host "  CodeAtlas  -  code map, SNIPE fix loop and live dashboard for coding agents" -ForegroundColor White
Write-Host ""

if ($Uninstall) {
    Write-Step "removing codeatlas"
    uv tool uninstall codeatlas 2>$null | Out-Null
    if (Get-Command claude -ErrorAction SilentlyContinue) { claude plugin uninstall codeatlas@codeatlas 2>$null | Out-Null }
    Remove-Item -Recurse -Force "$HOME\.agents\skills\codeatlas" -ErrorAction SilentlyContinue
    Write-Host "  removed. Project files (.codeatlas/, SNIPE.md, .mcp.json entries) were left in place." -ForegroundColor Green
    return
}

# 1. uv (fast Python package manager; installs Python itself if needed)
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Step "installing uv (https://docs.astral.sh/uv/)"
    irm https://astral.sh/uv/install.ps1 | iex
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

# 2. codeatlas
Write-Step ("{0} codeatlas from {1}" -f ($(if ($Update) { "updating" } else { "installing" }), $Source))
uv tool install --force --reinstall --python ">=3.10" $Source
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
uv tool update-shell 2>$null | Out-Null
codeatlas doctor

# 3. agents: Claude Code plugin, Codex, Cursor (auto-detects what is installed)
Write-Step "registering CodeAtlas with your coding agents ($Platform)"
codeatlas init --global $Platform

Write-Host ""
Write-Host "  Done." -ForegroundColor Green
Write-Host "  Claude Code : /codeatlas:find <question>, /codeatlas:snipe, or just ask; agents codeatlas-navigator/sniper/reviewer"
Write-Host "  Codex       : type `$codeatlas (Codex uses `$, not /); the codeatlas MCP server starts automatically"
Write-Host "  Any repo    : cd into it and run `codeatlas init` once (agents + guidance; the index builds when your agent starts there)"
Write-Host "  Dashboard   : codeatlas open   (http://127.0.0.1:8765 while an agent session runs)"
Write-Host "  Open a new terminal if 'codeatlas' is not found (PATH was just updated)." -ForegroundColor DarkGray
