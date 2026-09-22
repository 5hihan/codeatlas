# CodeAtlas plugin for Claude Code

Requires the `codeatlas` command on PATH (`uv tool install codeatlas`). Load with `claude --plugin-dir <this dir>` or install it, then use
`/codeatlas:find <question>`, `/codeatlas:snipe`, `/codeatlas:review-changes`, or let Claude delegate to
the `codeatlas-navigator`, `codeatlas-sniper` and `codeatlas-reviewer` agents.

`hooks/hooks.json` installs a PreToolUse guard. In a repository that has a `.codeatlas/index.db`, for files the index
knows, it denies with a message naming the CodeAtlas tool to use instead:

- Grep/Glob and `grep`/`rg`/`find` in any Bash statement -> `grep`, `find_symbols`, `references`, `list_files`
- whole-file Read and `cat` through Bash -> `file_outline`, `symbol_source`
- Edit whose old_string lies inside a declaration (up to 150 lines) -> `replace_symbol`, `insert_code`
- Write over an existing source file, `sed -i`, `>`, `tee`, inline scripts that write -> `replace_symbol`, `insert_code`
- any command that sets `CODEATLAS_GUARD`: the switch belongs to the user, not to the model

Every statement of a command is inspected (`;`, `&&`, `|`, newlines, `$(...)`), so chaining does not hide a search,
and no deny message names the off switch. Never gated: unindexed files, docs and config, bounded partial reads,
edits outside declarations, and any directory outside the indexed repository (a reference checkout, another
project), where CodeAtlas has no index and the shell is the only tool. Set `CODEATLAS_GUARD=search` in your own
environment for search-only redirects, or `off` to disable the guard.
