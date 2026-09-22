# CodeAtlas plugin for Claude Code

Requires the `codeatlas` command on PATH (`uv tool install codeatlas`). Load with `claude --plugin-dir <this dir>` or install it, then use
`/codeatlas:find <question>`, `/codeatlas:snipe`, `/codeatlas:review-changes`, or let Claude delegate to
the `codeatlas-navigator`, `codeatlas-sniper` and `codeatlas-reviewer` agents.

`hooks/hooks.json` installs a PreToolUse guard. In a repository that has a `.codeatlas/index.db`, for files the index
knows, it denies with a message naming the CodeAtlas tool to use instead:

- built-in Grep/Glob and `grep`/`rg`/`find` through Bash -> `grep`, `find_symbols`, `references`, `list_files`
- whole-file Read (no offset/limit) and `cat` through Bash -> `file_outline`, `symbol_source`
- Edit whose old_string lies inside a declaration (up to 150 lines) -> `replace_symbol`, `insert_code`
- Write over an existing source file -> `replace_symbol`, `insert_code` (Write is for new files)

Unindexed files, docs and config, partial reads and edits outside declarations pass through. `CODEATLAS_GUARD=search`
keeps only the search redirects; `CODEATLAS_GUARD=off` disables the guard.
