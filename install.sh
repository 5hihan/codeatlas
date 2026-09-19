#!/usr/bin/env sh
# CodeAtlas installer for macOS / Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/5hihan/codeatlas/main/install.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/5hihan/codeatlas/main/install.sh | sh -s -- codex   # pick a platform
#   ./install.sh [claude|codex|cursor|all|auto] [--update] [--uninstall]
#
# Installs uv if missing, installs the `codeatlas` command with uv, then runs `codeatlas init --global <platform>`
# which registers the MCP server + agents for Claude Code (plugin), Codex (~/.codex/config.toml +
# ~/.agents/skills/codeatlas) and Cursor. Re-run with --update to upgrade.
set -e
PLATFORM="auto"; MODE="install"
for a in "$@"; do
  case "$a" in
    --update) MODE="update" ;;
    --uninstall) MODE="uninstall" ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    claude|codex|cursor|all|auto) PLATFORM="$a" ;;
  esac
done
REPO="https://github.com/5hihan/codeatlas"
HERE="$(cd "$(dirname "$0")" 2>/dev/null && pwd || echo "")"
if [ -n "$CODEATLAS_SOURCE" ]; then SOURCE="$CODEATLAS_SOURCE"
elif [ -n "$HERE" ] && [ -f "$HERE/pyproject.toml" ]; then SOURCE="$HERE"
else SOURCE="git+$REPO"; fi

echo ""
echo "  CodeAtlas  -  code map, SNIPE fix loop and live dashboard for coding agents"
echo ""
if [ "$MODE" = "uninstall" ]; then
  uv tool uninstall codeatlas >/dev/null 2>&1 || true
  command -v claude >/dev/null 2>&1 && claude plugin uninstall codeatlas@codeatlas >/dev/null 2>&1 || true
  rm -rf "$HOME/.agents/skills/codeatlas"
  echo "  removed. Project files (.codeatlas/, SNIPE.md, .mcp.json entries) were left in place."
  exit 0
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "  > installing uv (https://docs.astral.sh/uv/)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
echo "  > ${MODE}ing codeatlas from $SOURCE"
uv tool install --force --reinstall --python ">=3.10" "$SOURCE"
export PATH="$HOME/.local/bin:$PATH"
uv tool update-shell >/dev/null 2>&1 || true
codeatlas doctor
echo "  > registering CodeAtlas with your coding agents ($PLATFORM)"
codeatlas init --global "$PLATFORM"
cat <<'EOF'

  Done.
  Claude Code : /codeatlas:find <question>, /codeatlas:snipe, or just ask; agents codeatlas-navigator/sniper/reviewer
  Codex       : type $codeatlas (Codex uses $, not /); the codeatlas MCP server starts automatically
  Any repo    : cd into it and run `codeatlas init` once (seeds the index, SNIPE.md, agent guidance)
  Dashboard   : codeatlas open   (http://127.0.0.1:8765 while an agent session runs)
  Open a new shell if `codeatlas` is not found (PATH was just updated).
EOF
