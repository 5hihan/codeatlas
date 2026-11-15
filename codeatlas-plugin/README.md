# CodeAtlas plugin for Claude Code

Requires the `codeatlas` command on PATH (`uv tool install codeatlas`, or the one-line installer in the repository README). Install in one step:

```
/plugin install codeatlas --marketplace 5hihan/codeatlas
```

or load it with `claude --plugin-dir <this dir>`, then use
`/codeatlas:find <question>`, `/codeatlas:snipe`, `/codeatlas:review-changes`, or let Claude delegate to
the `codeatlas-navigator`, `codeatlas-sniper` and `codeatlas-reviewer` agents.

`hooks/hooks.json` installs three hooks. A SessionStart hook (startup, resume, /clear and after every compaction) injects a few
lines of orientation: the index size and which tool to reach for first. A PreToolUse hook auto-approves CodeAtlas's own
read-only tools (queries never prompt; edits, SNIPE state changes and test runs still ask; `CODEATLAS_AUTO_APPROVE=off`
turns it off). A PreToolUse guard, in a repository that has a
`.codeatlas/index.db`, for files the index
knows, denies with a message naming the CodeAtlas tool to use instead:

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

`evals/` holds `claude plugin eval` cases (run with `--scaffold --mocks off --allow-tools "mcp__plugin_codeatlas_atlas__*"`): a
navigation question that must be answered with path:line evidence through CodeAtlas tools, and a non-code question that
must not call them. The with/without-plugin delta is the number to beat when changing tools, descriptions or the guard.

`hooks/register.js` is a mod (Claude Code 2.1.287+): it answers a whole-file Read of an indexed source file with the file's
outline and a Grep for an identifier with its definitions and uses, in place, as the tool's own result, so the model
needs no retry and sees no denial. Bounded reads, text regexes and unindexed files run unchanged; the PreToolUse
guard still covers what the mod lets through (whole-file `cat`, edits).

Without the `codeatlas` command on PATH the plugin is inert, not noisy: the server fails to start, the hooks exit
without output and the mod lets every call through. It does not install itself: a self-install hook would need a
shell on every platform (Windows without Git Bash has none) and would show an error line at every session start
where it cannot run, so installation stays with the one-line installer or `uv tool install codeatlas`.
