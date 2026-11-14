---
name: codeatlas
description: Navigate, understand and fix code in repositories indexed by CodeAtlas (tree-sitter code map, config/schema relationships, SNIPE fix loop). Use whenever the user asks where something is implemented, what calls what, how one piece of code reaches another, what a change affects, which config/env/schema a symbol uses, or to fix a failing test or bug, instead of reading whole files or grepping.
when_to_use: where is X, what calls Y, how does A reach B, what breaks if Z changes, which env var / table / config does this use, explain how this feature flows, fix this failing test, find the bug
allowed-tools: mcp__plugin_codeatlas_atlas__*
---

# CodeAtlas

The repository is indexed by the `codeatlas` MCP server (auto-updating on every save). Prefer its tools to reading
files; every answer carries `path:line` anchors you can open.

| Need | Tool |
|---|---|
| start a task, get the relevant symbols with source, tests, config | `task_context(task, scope, token_budget, known=[what you already understand])` |
| how does what I know reach what I want (route, hop by hop) | `connections(target, known)`; known defaults to what you looked at |
| where is X / what is X | `find_symbols`, `symbol`, `symbol_source` |
| structure of a file | `file_outline` (with reference counts and pattern tags) |
| who uses it, what breaks | `references`, `impact`, `triggers` |
| imports, config, env keys, SQL tables | `dependencies`, `relationships` |
| what changed and who is affected | `change_context(base_ref)` |
| edit precisely | `replace_symbol`, `insert_code`, `rename_symbol` (parse-checked) |
| fix the next concrete issue | `next_snipe` -> `claim_snipe` -> `get_snipe_context` -> patch -> `verify_snipe` |
| report a failure | `snipe_report(text)` |

Rules: outline before reading; read symbols, not files; change declarations with `replace_symbol` / `insert_code`
(never rewrite a whole file); check `impact` before changing a signature; keep patches to the smallest surface; log
side findings with `snipe_add` instead of fixing them on the spot. The plugin's PreToolUse guard enforces this for
indexed files: a denied Grep/Read/Edit/Write names the CodeAtlas call to make instead.

Delegate: use the `codeatlas-navigator` agent for research questions, `codeatlas-sniper` to fix an issue end to
end, `codeatlas-reviewer` to review a change set.
