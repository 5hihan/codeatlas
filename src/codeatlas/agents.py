"""Claude Code agents, skill and commands that ship with CodeAtlas.

The same templates produce two layouts:

* project files (``.claude/agents/*.md``) written by ``codeatlas install claude``, where the MCP server is the
  project's ``.mcp.json`` entry ``codeatlas`` (tools are ``mcp__codeatlas__*``);
* a plugin directory (``codeatlas plugin export DIR``) with ``.claude-plugin/plugin.json``, ``.mcp.json``,
  ``agents/``, ``skills/`` and ``commands/``, where the bundled server is ``atlas`` inside plugin ``codeatlas``
  (tools are ``mcp__plugin_codeatlas_atlas__*``).
"""
from __future__ import annotations

import json
from pathlib import Path

from . import __version__

PROJECT_PREFIX = "mcp__codeatlas"
PLUGIN_PREFIX = "mcp__plugin_codeatlas_atlas"

NAVIGATOR = """You are CodeAtlas Navigator: a read-only code-intelligence agent for this repository. You answer questions
such as "where is X implemented", "what calls Y", "what breaks if Z changes", "which env keys / tables / config does
this touch", "how does this feature flow" using the CodeAtlas MCP tools instead of reading whole files.

Method
1. Start with `task_context(question, scope=<dir if known>)`; for a named symbol go straight to `find_symbols` /
   `symbol`. Use `repo_overview` / `repo_map` only for broad orientation questions. When the question names code
   the user already knows ("from the login handler, how is the token rotated"), pass it as `known=[...]`: results
   are ranked by how they connect to it and the route is shown hop by hop.
2. Drill down with `symbol` (callers, callees, pattern tags), `file_outline`, `references`, `impact`, `triggers`,
   `dependencies`, `relationships` (config/schema/env), `programming_patterns`. Read code only through
   `symbol_source`, and only the symbols you actually need. For "how does A reach B" / "how is X wired to Y", or
   to place a symbol you just found relative to code you already understand, use `connections(target, known)`
   (known defaults to what you looked at in this session); quote its `A --calls@line--> B` hops as evidence.
3. Use Grep/Read for what the index cannot see (string literals, generated code, config values). Never read a whole
   large file when an outline answers the question. Never re-run the same tool with the same arguments.
4. Stop as soon as the answer is grounded in evidence. You do not audit, refactor or edit anything.

Answer format (keep it under ~400 words unless more was requested)
- One or two sentences that answer the question directly.
- "Evidence": bullets of `path:line` anchors, each with a short phrase (symbol, what it does, who calls it).
- "Related" (optional): up to five symbols or files worth opening next, one reason each.
- Say what is uncertain: medium-confidence callers, dynamic dispatch, unresolved imports, truncated results.
"""

SNIPER = """You are CodeAtlas Sniper. You fix one concrete issue at a time with the SNIPE loop (Scan, Narrow, Infer, Patch,
Evaluate) and the smallest possible context and patch surface. You never widen a fix into a refactor.

Loop
1. `next_snipe(agent="sniper")` (or the issue id / failure you were given; paste failure output into
   `snipe_report` first). If nothing is ready: `snipe_scan` (optionally `run="tests"`), then stop and report.
2. `claim_snipe(id, agent="sniper")` -> read level-1 context. Climb `get_snipe_context(id, level)` one level at a
   time (2 signatures, 3 bodies, 4 callers/deps, 5 full file, 6 subsystem) only while a question remains open.
3. Infer the most likely cause from the ranked candidates, their evidence and counter-evidence. Say which hypothesis
   you are testing and why.
4. Patch the fewest symbols that invalidate that hypothesis, with `replace_symbol` / `insert_code` (parse-checked,
   returns old text). Do not touch unrelated code; log anything else you notice with `snipe_add`.
5. `verify_snipe(id)`: PASS -> DONE and go to step 1 for the next item (at most 3 items per run unless told
   otherwise). FAIL -> re-read the failure evidence, revise the hypothesis, patch again (max 3 attempts).
   ABORTED (blast radius exceeded) -> leave a note with `snipe_update` and stop.
6. If verification cannot run (no test command), configure it with `snipe_config(test_cmd=...)` when the repo makes
   the command obvious, otherwise run the relevant test with Bash and report the result honestly.

Report at the end: for each issue, the id, the hypothesis, the symbols changed (path:line), the verification result,
and anything left open. Never claim a fix is verified when it was not.
"""

REVIEWER = """You are CodeAtlas Reviewer: a read-only reviewer of the current change set (working tree, staged, or a
branch versus its base). You use the index's blast radius rather than reading the whole diff first.

Method
1. `change_context(base_ref, comparison=...)` for the changed symbols, risk, consumers to re-check, config/schema
   files and tests to run.
2. For each changed symbol that has consumers or no tests: `symbol` and `impact` to see who depends on it, then
   `symbol_source` for the new body and, when needed, `symbol_source` of the most important callers.
3. Check config/schema/env relationships (`relationships`) when configuration, migrations or schemas changed.
4. Use `git diff` through Bash only for the exact hunks of symbols you have decided to look at.

Report: a short verdict (safe / needs attention / risky) with the reasons; a bullet list of concrete findings, each
with `path:line`, what could break and which test or caller proves it; the tests that should run before merging;
open questions. Prefer three precise findings over ten vague ones. Do not edit code.
"""

SKILL = """---
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
"""

COMMANDS = {
    "find": ("""---
description: Find where something is implemented or how it works, using the CodeAtlas index (read-only)
argument-hint: <question or symbol>
agent: codeatlas-navigator
---

Answer this question about the repository using CodeAtlas tools, with path:line evidence:

$ARGUMENTS
"""),
    "snipe": ("""---
description: Fix the next concrete issue (or the given issue id / failure) with the SNIPE loop
argument-hint: [issue id, failure text, or empty for next_snipe]
agent: codeatlas-sniper
disable-model-invocation: true
---

Run the SNIPE loop. Target: $ARGUMENTS
(If empty, take `next_snipe`. If it looks like test or error output, feed it to `snipe_report` first.)
"""),
    "review-changes": ("""---
description: Review the current change set with CodeAtlas blast radius (consumers, tests, config/schema)
argument-hint: [base ref, default HEAD]
agent: codeatlas-reviewer
disable-model-invocation: true
---

Review the changes versus base ref: $ARGUMENTS
(Default base ref is HEAD, comparison=working. Use merge_base=True for a feature branch versus main.)
"""),
}


def agent_files(prefix: str) -> dict[str, str]:
    """name -> markdown for the three agents, with tool allowlists bound to an MCP server prefix."""
    nav_tools = f"Read, Grep, Glob, {prefix}__*"
    fix_tools = f"Read, Grep, Glob, Edit, Write, Bash, {prefix}__*"
    return {
        "codeatlas-navigator": f"""---
name: codeatlas-navigator
description: >-
  Read-only code research with the CodeAtlas index. Use for "where is X", "what calls Y", "what breaks if Z changes", "which config/env/schema does this use", "how does this flow" questions; returns path:line evidence without reading whole files.
tools: "{nav_tools}"
model: sonnet
color: cyan
---

{NAVIGATOR}""",
        "codeatlas-sniper": f"""---
name: codeatlas-sniper
description: >-
  Fixes one concrete issue end to end with the SNIPE loop (next_snipe, claim, minimal context, smallest patch, verify). Use to fix a failing test, a reported bug, or the next ready issue on the SNIPE board.
tools: "{fix_tools}"
model: inherit
color: red
---

{SNIPER}""",
        "codeatlas-reviewer": f"""---
name: codeatlas-reviewer
description: >-
  Reviews the current diff or a branch using CodeAtlas blast radius: changed symbols, consumers to re-check, config/schema impact, tests to run. Read-only; use before committing or merging.
tools: "{nav_tools}, Bash(git diff*), Bash(git log*), Bash(git status*)"
model: sonnet
color: yellow
---

{REVIEWER}""",
    }


def write_project_agents(repo: Path) -> list[str]:
    out = []
    d = repo / ".claude" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    for name, text in agent_files(PROJECT_PREFIX).items():
        p = d / f"{name}.md"
        p.write_text(text, encoding="utf-8")
        out.append(str(p))
    return out


def plugin_hooks(prefix: str) -> dict:
    """Claude Code hooks: a PreToolUse guard that denies built-in search, whole-file reads and in-declaration edits
    when the repo is indexed, and a SessionStart hook that injects a few lines of orientation.

    Advice in CLAUDE.md is not enough: Grep/Glob/Read/Edit schemas are always loaded while MCP tools are deferred,
    so the model reaches for them by default. The guard (``codeatlas hook pretool``) turns the advice into a rule
    and its deny reason names the CodeAtlas tool (and arguments) to call instead, so the model recovers in the same
    turn. See :mod:`codeatlas.hook` for exactly what is gated. The SessionStart hook (``codeatlas hook
    session-start``) runs at startup, resume, /clear and after every compaction: the index size and which tool to
    reach for first, so the first call of a session is a CodeAtlas call and the habit survives compaction.
    """
    # exec form (command + args): each argument is passed as-is, no shell quoting, same on every platform.
    # `modules` registers the mod (hooks/register.js): it answers a whole-file Read or an identifier Grep of an
    # indexed file from the index, in place, before the guard would have to deny it.
    return {"modules": ["./register.js"], "hooks": {
        "SessionStart": [{
            "matcher": "startup|resume|clear|compact",
            "hooks": [{"type": "command", "command": "codeatlas",
                       "args": ["hook", "session-start", "--prefix", prefix], "timeout": 5}],
        }],
        "PreToolUse": [
            {   # the guard: built-in search / whole-file reads / in-declaration edits of indexed files
                "matcher": "Grep|Glob|Bash|Read|Edit|MultiEdit|Write",
                "hooks": [{"type": "command", "command": "codeatlas",
                           "args": ["hook", "pretool", "--prefix", prefix], "timeout": 10}],
            },
            {   # the same handler auto-approves CodeAtlas's own read-only tools: no permission prompt for a query
                "matcher": f"{prefix}__.*",
                "hooks": [{"type": "command", "command": "codeatlas",
                           "args": ["hook", "pretool", "--prefix", prefix], "timeout": 5}],
            },
        ],
    }}


MOD_JS = """// CodeAtlas mod: answer a built-in Read or Grep of an indexed source file from the index, in place.
//
// A whole-file Read becomes the file's top-level declarations with line ranges; a Grep for an identifier (through
// the Grep tool or a shell grep/rg in Bash) becomes its definitions and name matches grouped by file. The model gets
// the answer as the tool's result: no denial, no retry, no hook message. Anything the index cannot answer (bounded
// reads, text regexes, other shell commands, unindexed files, no `codeatlas` on PATH) goes ahead unchanged, and the
// PreToolUse guard still applies to it.
const PREFIX = '%(prefix)s'

export function register(on, options) {
  on('tool.call', { tool: ['Read', 'Grep', 'Bash'] }, async ($, e, next) => {
    try {
      const { tool, tool_use_id, consent, ...args } = e
      const env = {}
      if (options && options.guard_mode !== undefined) env.CLAUDE_PLUGIN_OPTION_GUARD_MODE = String(options.guard_mode)
      const run = await $.process.run(['codeatlas', 'hook', 'answer', '--prefix', PREFIX],
                                      { stdin: JSON.stringify({ tool_name: tool, tool_input: args }), timeoutMs: 8000, env })
      if (run.exitCode === 0 && run.stdout.trim()) {
        const answer = JSON.parse(run.stdout)
        if (answer && typeof answer.result === 'string' && answer.result) {
          // the engine checks `result` against the tool's own output shape
          const text = answer.result
          const numLines = text.split('\\n').length
          if (tool === 'Read') {
            return { result: { type: 'text', file: { filePath: String(args.file_path || ''), content: text, numLines, startLine: 1, totalLines: numLines } } }
          }
          if (tool === 'Bash') {
            return { result: { stdout: text, stderr: '', interrupted: false, isImage: false } }
          }
          const filenames = Array.isArray(answer.files) ? answer.files : []
          return { result: { mode: 'content', content: text, numLines, numMatches: numLines, numFiles: filenames.length, filenames } }
        }
      }
    } catch (err) {
      // fall through: the tool runs as usual
    }
    return next(e)
  })
}
"""

EVALS = {
    # A realistic question in a tiny scaffolded repository; measured with and without the plugin by
    # `claude plugin eval codeatlas-plugin` (needs --scaffold for the workspace). Outcome: the answer names the
    # right definition with a path:line anchor. Process: the plugin's tools were used (with-plugin arm only).
    "evals/where-is-it/case.yaml": """schema_version: "1.1"
name: where-is-it
description: A navigation question in a small indexed repository; the answer must carry path:line evidence.
tags: [navigation, task_context]
plugins: ["../.."]
runs: 3
expected_outcome: Names apply_discount in pricing.py with its line, and checkout as the caller, without reading whole files.
execution:
  max_turns: 12
  timeout_seconds: 300
context:
  scaffold_script: scaffold.sh
""",
    "evals/where-is-it/scaffold.sh": """#!/usr/bin/env bash
# Builds the tiny repository the where-is-it case asks about (run by `claude plugin eval --scaffold`).
set -euo pipefail
mkdir -p shop tests
cat > shop/__init__.py <<'PY'
PY
cat > shop/pricing.py <<'PY'
def base_total(items):
    return sum(i["price"] * i["qty"] for i in items)


def apply_discount(total, code):
    if code == "VIP":
        return total * 0.8
    return total
PY
cat > shop/checkout.py <<'PY'
from .pricing import base_total, apply_discount


def checkout(items, code=None):
    total = base_total(items)
    return apply_discount(total, code)
PY
cat > tests/test_checkout.py <<'PY'
from shop.checkout import checkout


def test_vip():
    assert checkout([{"price": 10, "qty": 1}], "VIP") == 8
PY
""",
    "evals/where-is-it/prompt.md": """---
schema_version: "1.1"
name: where-is-it
---
Where is the discount applied when an order total is computed, and what calls it? Answer with file:line anchors.
""",
    "evals/where-is-it/graders/answer-anchored.md": """---
type: regex
pattern: 'shop/pricing\\.py:\\d+'
target: last_message
---
The answer cites the discount function's location in shop/pricing.py with a line number.
""",
    "evals/where-is-it/graders/names-caller.md": """---
type: regex
pattern: 'checkout'
target: last_message
---
The answer names checkout as the caller of apply_discount.
""",
    "evals/where-is-it/graders/used-task-context.md": """---
type: tool_used
tool: mcp__plugin_codeatlas_atlas__task_context
arm: with-only
---
The question was answered through CodeAtlas (indicator only: the with-plugin arm).
""",
    "evals/where-is-it/graders/no-whole-file-read.md": """---
type: tool_used
tool: Read
min: 0
max: 0
arm: with-only
---
No whole-file Read was needed with the plugin (indicator only).
""",
    "evals/no-code-question/prompt.md": """---
schema_version: "1.1"
name: no-code-question
description: A question that has nothing to do with the repository must not trigger CodeAtlas tools.
tags: [negative]
plugins: ["../.."]
runs: 2
expected_outcome: A direct answer with no CodeAtlas tool call.
max_turns: 3
---
In two sentences, what is the difference between a mutex and a semaphore?
""",
    # `tool_used ... max: 0` per entry tool: a trace regex would also match the SessionStart orientation text
    "evals/no-code-question/graders/no-task-context.md": """---
type: tool_used
tool: mcp__plugin_codeatlas_atlas__task_context
min: 0
max: 0
arm: both
---
task_context was not called for a question that is not about the code.
""",
    "evals/no-code-question/graders/no-find-symbols.md": """---
type: tool_used
tool: mcp__plugin_codeatlas_atlas__find_symbols
min: 0
max: 0
arm: both
---
find_symbols was not called for a question that is not about the code.
""",
}


def write_plugin(target: Path) -> list[str]:
    """Materialise the plugin directory (load it with `claude --plugin-dir DIR` or install from git)."""
    out = []
    (target / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": "codeatlas",
        "displayName": "CodeAtlas",
        "description": "Tree-sitter code map, config/schema relationships and the SNIPE fix loop for large codebases, "
                       "with navigator, sniper and reviewer agents.",
        "version": __version__,
        "author": {"name": "CodeAtlas"},
        "license": "MIT",
        "homepage": "https://github.com/5hihan/codeatlas",
        "repository": "https://github.com/5hihan/codeatlas",
        "keywords": ["code-intelligence", "tree-sitter", "code-map", "symbols", "call-graph", "mcp", "snipe"],
        # `/plugin configure codeatlas@codeatlas` or `claude plugin install ... --config KEY=VALUE`; hooks read
        # CLAUDE_PLUGIN_OPTION_<KEY>, the server reads the same values through its environment (.mcp.json env)
        "userConfig": {
            "guard_mode": {"type": "string", "title": "Guard mode",
                           "description": "default: deny built-in Grep/Glob, whole-file Read and in-declaration edits of indexed "
                                          "files and name the CodeAtlas call instead; search: only the search redirects; "
                                          "off: no guard.", "default": "default"},
            "auto_approve": {"type": "boolean", "title": "Auto-approve read-only tools",
                             "description": "Approve CodeAtlas's read-only tools without a permission prompt (edits, SNIPE state "
                                            "changes and test runs always ask).", "default": True},
            "dashboard": {"type": "boolean", "title": "Dashboard",
                          "description": "Serve the live dashboard on 127.0.0.1:8765 while the server runs.", "default": True},
        },
    }
    files = {
        ".claude-plugin/plugin.json": json.dumps(manifest, indent=2) + "\n",
        ".mcp.json": json.dumps({"mcpServers": {"atlas": {
            "command": "codeatlas", "args": ["serve", "--repo", "${CLAUDE_PROJECT_DIR}"],
            "env": {"CODEATLAS_DASHBOARD": "${user_config.dashboard}"}}}}, indent=2) + "\n",
        "skills/codeatlas/SKILL.md": SKILL,
        "hooks/hooks.json": json.dumps(plugin_hooks(PLUGIN_PREFIX), indent=2) + "\n",
        "README.md": ("# CodeAtlas plugin for Claude Code\n\nRequires the `codeatlas` command on PATH "
                      "(`uv tool install codeatlas`, or the one-line installer in the repository README). Install in one step:\n\n"
                      "```\n/plugin install codeatlas --marketplace 5hihan/codeatlas\n```\n\n"
                      "or load it with `claude --plugin-dir <this dir>`, then use\n"
                      "`/codeatlas:find <question>`, `/codeatlas:snipe`, `/codeatlas:review-changes`, or let Claude delegate to\n"
                      "the `codeatlas-navigator`, `codeatlas-sniper` and `codeatlas-reviewer` agents.\n\n"
                      "`hooks/hooks.json` installs three hooks. A SessionStart hook (startup, resume, /clear and after every "
                      "compaction) injects a few\nlines of orientation: the index size and which tool to reach for first. "
                      "A PreToolUse hook auto-approves CodeAtlas's own\nread-only tools (queries never prompt; edits, SNIPE "
                      "state changes and test runs still ask; `CODEATLAS_AUTO_APPROVE=off`\nturns it off). "
                      "A PreToolUse guard, in a repository that has a\n`.codeatlas/index.db`, "
                      "for files the index\nknows, denies with a message naming the CodeAtlas tool to use instead:\n\n"
                      "- Grep/Glob and `grep`/`rg`/`find` in any Bash statement -> `grep`, `find_symbols`, `references`, "
                      "`list_files`\n"
                      "- whole-file Read and `cat` through Bash -> `file_outline`, `symbol_source`\n"
                      "- Edit whose old_string lies inside a declaration (up to 150 lines) -> `replace_symbol`, "
                      "`insert_code`\n"
                      "- Write over an existing source file, `sed -i`, `>`, `tee`, inline scripts that write -> "
                      "`replace_symbol`, `insert_code`\n"
                      "- any command that sets `CODEATLAS_GUARD`: the switch belongs to the user, not to the model\n\n"
                      "Every statement of a command is inspected (`;`, `&&`, `|`, newlines, `$(...)`), so chaining does "
                      "not hide a search,\nand no deny message names the off switch. Never gated: unindexed files, docs "
                      "and config, bounded partial reads,\nedits outside declarations, and any directory outside the "
                      "indexed repository (a reference checkout, another\nproject), where CodeAtlas has no index and the "
                      "shell is the only tool. Set `CODEATLAS_GUARD=search` in your own\nenvironment for search-only "
                      "redirects, or `off` to disable the guard.\n\n"
                      "`evals/` holds `claude plugin eval` cases (run with `--scaffold --mocks off --allow-tools "
                      "\"mcp__plugin_codeatlas_atlas__*\"`): a\nnavigation question that must be answered with path:line "
                      "evidence through CodeAtlas tools, and a non-code question that\nmust not call them. The "
                      "with/without-plugin delta is the number to beat when changing tools, descriptions or the guard.\n\n"
                      "`hooks/register.js` is a mod (Claude Code 2.1.287+): it answers a whole-file Read of an indexed source file "
                      "with the file's\noutline and a Grep for an identifier with its definitions and uses, in place, as the tool's "
                      "own result, so the model\nneeds no retry and sees no denial. Bounded reads, text regexes and unindexed files "
                      "run unchanged; the PreToolUse\nguard still covers what the mod lets through (whole-file `cat`, edits).\n\n"
                      "Without the `codeatlas` command on PATH the plugin is inert, not noisy: the server fails to start, the hooks "
                      "exit\nwithout output and the mod lets every call through. It does not install itself: a self-install hook "
                      "would need a\nshell on every platform (Windows without Git Bash has none) and would show an error line at "
                      "every session start\nwhere it cannot run, so installation stays with the one-line installer or "
                      "`uv tool install codeatlas`.\n"),
    }
    for name, text in agent_files(PLUGIN_PREFIX).items():
        files[f"agents/{name}.md"] = text
    for name, text in COMMANDS.items():
        files[f"commands/{name}.md"] = text
    files.update(EVALS)
    files["evals/.gitignore"] = "results/\n"
    files["hooks/register.js"] = MOD_JS % {"prefix": PLUGIN_PREFIX}
    for rel, text in files.items():
        p = target / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        out.append(str(p))
    return out
