# CodeAtlas plugin for Claude Code

Requires the `codeatlas` command on PATH (`uv tool install codeatlas`). Load with `claude --plugin-dir <this dir>` or install it, then use
`/codeatlas:find <question>`, `/codeatlas:snipe`, `/codeatlas:review-changes`, or let Claude delegate to
the `codeatlas-navigator`, `codeatlas-sniper` and `codeatlas-reviewer` agents.

`hooks/hooks.json` installs a PreToolUse guard: in a repository that has a `.codeatlas/index.db`, the built-in
Grep and Glob tools and `grep`/`rg`/`find` run through Bash are denied with a message naming the CodeAtlas tool to
use instead (`grep`, `find_symbols`, `references`, `list_files`). Set `CODEATLAS_GUARD=off` to disable it, or
`CODEATLAS_GUARD=strict` to also redirect whole-file Read of indexed source files to `file_outline` / `symbol_source`.
