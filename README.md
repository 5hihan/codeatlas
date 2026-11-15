<h1 align="center">CodeAtlas</h1>

<p align="center">
  <strong>A code map, fix loop and live dashboard for coding agents. Tree-sitter, no LLM in the loop, updates on every save.</strong><br>
  <em>Works with Claude Code, Codex and Cursor (any MCP client).</em>
</p>

<p align="center">
  <a href="#install"><img alt="install" src="https://img.shields.io/badge/install-one%20line-2ea44f"></a>
  <a href="#what-it-does"><img alt="tools" src="https://img.shields.io/badge/MCP%20tools-39-7aa2ff"></a>
  <a href="#languages"><img alt="languages" src="https://img.shields.io/badge/languages-17-c792ea"></a>
  <a href="#snipe-the-fix-loop"><img alt="snipe" src="https://img.shields.io/badge/fix%20loop-SNIPE-ff6b6b"></a>
  <a href="LICENSE"><img alt="license" src="https://img.shields.io/badge/license-MIT-lightgrey"></a>
</p>

> *Your agent just opened a 216,000-line monorepo. It needs one function, its callers and the test that covers it.
> It should not read 30,000 tokens of files to find them.*

CodeAtlas indexes a repository with tree-sitter into a symbol graph (functions, classes, references, imports,
env keys, SQL tables, patterns) and exposes it over MCP as compact, `path:line`-anchored answers. An agent asks
`task_context("fix expired refresh tokens")` and gets the five symbols that matter, their callers, the test that
exercises them and the config they read, in ~3k tokens instead of ~30k. It also remembers what the agent has
already looked at and shows how each result connects to it, hop by hop
(`login --calls@134--> rotateRefreshToken`). The index updates itself within a second of
every save, and a localhost React dashboard shows the map, the SNIPE board and agent activity live.

```
backend/src/services/auth.ts  [typescript, 764 lines, 25 symbols]
f async function signup(db, input)                     :49-119  ←2f {async_await,closures,error_handling}
f async function login(db, input)                      :121-146 ←3f {async_await,error_handling}
f async function rotateRefreshToken(db, token, ...)    :216-254 ←2f {async_await,error_handling}
imports (repo): services/errors.ts[unauthorized], security/tokens.ts[sha256, randomToken]
imported by (6): routes/auth.ts, routes/appleWeb.ts, tests/auth-lifecycle.test.ts, ...
```

## Install

**Windows (PowerShell)**
```powershell
iwr -useb https://raw.githubusercontent.com/5hihan/codeatlas/main/install.ps1 | iex
```

**macOS / Linux**
```bash
curl -fsSL https://raw.githubusercontent.com/5hihan/codeatlas/main/install.sh | sh
```

That installs [uv](https://docs.astral.sh/uv/) if needed, the `codeatlas` command, and registers CodeAtlas with
every coding agent it finds on the machine (`codeatlas init --global`). Pass a platform to pick one:
`... | sh -s -- codex`, or `install.ps1 codex`. Re-run with `--update` / `-Update` to upgrade.

Then, in any repository (new or existing, empty is fine):

```bash
codeatlas init        # writes agents + guidance, idempotent; the index builds itself when your agent starts here
```

<details>
<summary><b>Per platform</b></summary>

| Platform | How | Then |
|---|---|---|
| **Claude Code** | plugin `codeatlas@codeatlas` installed at user scope by the installer (or `claude plugin marketplace add 5hihan/codeatlas` + `claude plugin install codeatlas@codeatlas`) | `/codeatlas:find <question>`, `/codeatlas:snipe`, `/codeatlas:review-changes`, or let Claude delegate to the `codeatlas-navigator`, `codeatlas-sniper`, `codeatlas-reviewer` agents. Two hooks make Claude actually use the index: a SessionStart hook injects a few lines of orientation at startup, resume and after every compaction, and a PreToolUse guard denies built-in `Grep`/`Glob`, whole-file `Read` of indexed files and in-declaration edits with the CodeAtlas tool to call instead (`CODEATLAS_GUARD=off` disables, `=search` keeps only the search redirects) |
| **Codex** | `mcp_servers.codeatlas` in `~/.codex/config.toml` + skill in `~/.agents/skills/codeatlas` | type `$codeatlas` (Codex uses `$`, not `/`); the MCP tools are available in every project |
| **Cursor** | `codeatlas init cursor` writes `.cursor/mcp.json` in the repo | the MCP tools appear in the agent |
| **Anything else (MCP)** | `codeatlas serve --repo <path>` over stdio | 39 tools, see below |

Manual install from a checkout: `uv tool install .` (or `pipx install .`), then `codeatlas doctor`.
</details>

## Measured: the same agent, with and without CodeAtlas

Claude Code (`claude -p`, Sonnet) answering the same research questions on a 785-file, 216k-line TypeScript +
Kotlin + Swift monorepo, once with only Read/Grep/Glob and once with the CodeAtlas MCP server added. Numbers are
Claude's own usage report, summed over every turn, averaged over two runs each.

| Question | | Tool calls | Files read | New tokens into context | API time | Cost |
|---|---|---:|---:|---:|---:|---:|
| where is the refresh token rotated on resume, which test covers it | without | 17.5 | 3.0 | 65,678 | 61 s | $0.187 |
| | **with CodeAtlas** | **5.0** | **0.5** | **27,566** | **26 s** | **$0.125** |
| what breaks if a session-revocation function changes signature; callers + tests | without | 26.0 | 7.5 | 90,835 | 90 s | $0.267 |
| | **with CodeAtlas** | **15.0** | **4.5** | 91,632 | **70 s** | **$0.220** |
| which env vars the relay/media-budget code reads, and where they are declared | without | 8.5 | 3.5 | 64,672 | 51 s | $0.174 |
| | **with CodeAtlas** | **7.5** | **0.0** | **57,334** | **24 s** | $0.177 |

Answers were checked against the source; both arms reached the right functions and lines. With CodeAtlas the agent
made 2-3× fewer tool calls, opened almost no whole files, and answered roughly twice as fast; new context dropped
by 58% on the first task and was flat on the impact task, where the agent still opened the four files it wanted to
quote. Three further questions on a 101k-line web app were grep-shaped ("list every place X is called"); there the
agent answered with Grep alone in both arms and nothing changed. CodeAtlas pays off when a question needs
relationships (callers, impact, config, "what triggers what"), not when a single grep suffices.

### Context cost per task, estimated over six codebases

Six projects, one representative task each. "Without" is the tokens of the whole files an agent would open to
answer it; "with" is the `task_context` answer (symbols, source of the strongest ones, callers, tests, config).

| Codebase | Files | Lines | Symbols | Full index | Save → index | Task | Without | With CodeAtlas | Saved |
|---|---:|---:|---:|---:|---:|---|---:|---:|---:|
| TS + Kotlin + Swift monorepo | 785 | 216,013 | 8,520 | 13.8 s | 23 ms | fix refresh-token rotation | 28,508 | 2,899 | **9.8×** |
| TS/TSX web app + API | 432 | 101,146 | 3,049 | 5.7 s | 22 ms | fix async error handling in checkout | 55,377 | 2,955 | **18.7×** |
| Python + TS client library | 52 | 13,972 | 889 | 0.7 s | 18 ms | how the client fetches and caches | 26,197 | 2,915 | **9.0×** |
| TSX admin app | 119 | 16,643 | 697 | 0.8 s | 12 ms | where orders are created | 5,953 | 2,882 | 2.1× |
| JavaScript service | 94 | 8,261 | 256 | 0.4 s | 17 ms | how cards render, GET route | 6,241 | 3,579 | 1.7× |
| Python library | 29 | 3,138 | 115 | 0.3 s | 17 ms | how the mesh is built | 3,379 | 2,275 | 1.5× |

The saving grows with the codebase: small projects fit in context anyway, large ones do not. Query latency is
below 40 ms at p95 for `symbol`, `references` and `impact`, and a no-op freshness check costs under 10 ms on the
small codebases and ~100 ms on the 216k-line monorepo. Measured on a Windows 11 desktop (Intel Core Ultra 7, 32 GB RAM, SSD).

Accuracy checks that shaped the resolver: `this.x()` callers stay inside their class in 97% of synthetic cases,
callers never cross language or package boundaries, and generic member names (`.size`, `.push`) are never
attributed to an unrelated repository symbol.

## What it does

**Navigation without reading files**

| Tool | Gives the agent |
|---|---|
| `task_context` | **start here**: the symbols relevant to a task (stemmed BM25 over names, signatures, docs and module names + PageRank + graph proximity + pattern hints; tests, examples and private helpers weighed down, results spread across files), source for the strongest ones, tests, config/docs, inside an adaptive token budget; each item says why it is there. `known=[...]` (or, by default, what the agent looked at in this session) re-ranks results by how they connect to that code and renders the route to each one |
| `connections` | **how what you know reaches what you want**: shortest labelled routes from known symbols/files to a target through calls, callback registrations, class membership, file containment, imports and env/table/config relationships, every hop a `path:line`; alternative routes, the symbols on the way, and an honest "no route within N hops" |
| `repo_overview`, `repo_map` | languages, packages, hubs, entry points; the complete symbol map, files ordered by PageRank (pass `token_budget` to get only the top-ranked part) |
| `file_outline`, `find_symbols`, `symbol`, `symbol_source` | outlines with reference counts and pattern tags (files over 60 symbols are summarised to their most referenced members, `full=True` lists all); signature, doc, callers, callees; just the lines of one symbol |
| `references`, `impact`, `triggers`, `dependencies`, `module_graph` | who uses it, what breaks, what makes it run, import graphs |
| `relationships` | code ↔ config ↔ schema: env keys read and declared, SQL tables declared and used, literal config paths |
| `programming_patterns` | 12 patterns (generics, closures, decorators, async, generators, error handling, ...) at real locations with evidence |
| `change_context` | git blast radius: changed symbols, consumers to re-check, config/schema touched, tests to run, risk |
| `hotspots`, `unreferenced`, `findings`, `grep` | complexity, dead code, comment tags, regex search |

**Connecting what the agent knows to what it needs.** Agents rarely start from nothing: they have a handler open, a
failing test, a symbol from the last answer. `task_context(task, known=["login", "routes/auth.ts"])` runs a weighted
search from those anchors over the resolved graph, so the `process` reachable from `main` outranks a same-named
`process` in a legacy package, the anchors themselves are not repeated, the symbols that bridge an anchor to a result
are added, and the answer shows the route as a tree:

```
## How what you know connects to these
  main (app/main.py:3)
    --calls@4--> handle (app/handlers.py:3)
      --calls@5--> process (app/service.py:4)
        --calls@7--> Store.save (app/store.py:2)
  no route within ~6 hops: process (legacy/process.py:1)
```

The server remembers what each agent session looked at (`symbol`, `symbol_source`, `file_outline`, `references`,
`impact`, edits, `task_context` results), so this happens by default; `known=[]` starts fresh. The explicit question
is `connections(target, known)`: `main --calls@4--> handle --calls@5--> process --calls@7--> Store.save`, with
`A <--calls@12-- B` meaning B calls A, alternative routes (through imports, a shared test, a config file), and the
symbols on the way with `symbol_source` handles. Routes prefer tight evidence (a resolved call costs 1 hop, "same
file" 1.6, an import 1.5, medium-confidence edges more), pay extra to pass through hubs such as loggers, and pay a
little to reverse direction, so straight call chains win over "they share a helper" when both exist.

**Precise editing.** `replace_symbol`, `insert_code`, `rename_symbol` work on syntax-tree spans: neighbours on the
same line survive, the new file is parsed before it is written, a content hash guards against concurrent edits,
and rename touches identifiers only, never strings or comments.

**Resolution you can trust.** Every graph tool shares one resolver: import aliases are followed, `this.x()` binds
to the enclosing class, `Foo.x()` to `Foo`, then same file → imported files → unique definition in the same package
and language. Edges carry a confidence; ambiguous ones are reported, not guessed. Results say when they were
truncated and which index version they came from.

## SNIPE: the fix loop

No sprints, no tickets, no audits. Every save and every reported failure becomes a tiny scored issue; the agent
takes the highest-confidence, lowest-context one and fixes it with the smallest patch that can be verified.

```
next_snipe  →  claim_snipe(id)  →  get_snipe_context(id, level)  →  patch  →  verify_snipe(id)  →  next
```

- **Scan** is event-driven: removed-but-still-referenced symbols, broken imports, syntax errors and new `FIXME`s
  become issues on the next save; `snipe_report` turns test/typecheck/lint output (node, jest, vitest, pytest, go,
  cargo) into issues; static findings stay dormant as *opportunities*.
- **Narrow** reduces an issue to a few symbols: seeds from stack traces, named symbols and the failing test,
  callee chains, their intersection, bounded to the evidence's package and language.
- **Infer** ranks candidates with confidence, evidence and counter-evidence.
- **Patch** is the agent's job, on the smallest surface. Context is lazy: level 1 names, 2 signatures, 3 bodies,
  4 callers, 5 file, 6 subsystem.
- **Evaluate** is a cone: parse + dead-reference check + targeted tests, then the callers' tests, the full suite
  only when the risk justifies it. Over 6 symbols or 3 files changed → the snipe is aborted and promoted to
  INVESTIGATE. Test commands are auto-detected per package.

`SNIPE.md` in the repo mirrors the board; the dashboard shows READY TO SNIPE, INVESTIGATING and CURRENT (per agent)
live. Score = √(confidence × impact × reproducibility × isolation) ÷ √(context cost × blast radius).

## Dashboard

`codeatlas serve` (what agents run) also starts a React dashboard on `http://127.0.0.1:8765` (`codeatlas open`):
overview, repo map with budget slider, symbol explorer, file outlines, hotspots, module graph, relationships,
patterns, findings, the SNIPE board and a live feed of every tool call and index update, pushed over
Server-Sent Events. One dashboard per repo is shared by concurrent agent sessions.

## Languages

Python, JavaScript, TypeScript, TSX, Go, Rust, Java, Kotlin, Swift, C, C++, C#, Ruby, PHP, Scala, Lua, Bash.
Grammars ship with `tree-sitter-language-pack`; nothing to compile. Config and schema files: `.env`, JSON, YAML,
TOML, INI, SQL, Prisma, GraphQL, protobuf.

## CLI

```
codeatlas init [claude|codex|cursor|all|auto] [--global]   set up a repo, or the whole machine once
codeatlas serve [--repo PATH] [--profile nav|edit|snipe|all]  MCP server + watcher + dashboard
codeatlas context "<task>" [--known X] | connections TARGET --known X | changes [--base REF] | map | overview
codeatlas outline PATH | symbol NAME | impact NAME
codeatlas snipe [board|scan [--run tests]|next|report FILE]  the loop from the shell
codeatlas plugin export DIR | dashboard --open | open | doctor
codeatlas hook pretool [--prefix P] [--mode default|search|off]   Claude Code PreToolUse guard (shipped in the plugin)
```

## Built for how Claude Code loads tools

Claude Code defers MCP tool schemas behind a tool search and shows the model only tool names plus the server's
instructions at session start. CodeAtlas works with that: the five tools an agent reaches for first
(`task_context`, `find_symbols`, `symbol`, `file_outline`, `symbol_source`) are flagged `anthropic/alwaysLoad` so the
first useful call needs no search round-trip, at about 1,100 tokens of schemas upfront (`CODEATLAS_ALWAYS_LOAD`
overrides the list; the other 34 tools cost only their names, about 640 tokens with the instructions), every
navigation tool carries `readOnlyHint`/`idempotentHint` so Claude Code runs them in parallel with other read-only
calls, and the server instructions stay inside the 4,096-character cap. A SessionStart hook adds about 270
tokens of orientation (index size, which tool to reach for) at startup, resume and `/clear`, and a 125-token reminder
after every compaction, when models otherwise forget the index exists. A PreToolUse hook auto-approves CodeAtlas's
own read-only tools, so a query of the index never shows a permission prompt; edits, SNIPE state changes and anything
that runs tests still ask (`CODEATLAS_AUTO_APPROVE=off` turns the approval off). `codeatlas-plugin/evals/` holds
`claude plugin eval` cases (a navigation question that must be answered with path:line evidence through CodeAtlas
tools, and a non-code question that must not call them) so the with/without-plugin delta can be measured on every
change.

### Tool outputs, 0.1.2 to 0.1.3, on identical inputs

Default output of each tool on fixed targets in three repositories (this one, fastapi at 135k lines, hono at 95k
lines), estimated at 3.6 characters per token with the same estimator for both versions; the "before" column is
the 0.1.2 code run against the same index.

| Default output | 0.1.2 | 0.1.3 | Change |
|---|---:|---:|---:|
| `task_context`, 9 tasks | 39,130 | 28,323 | **-28%** |
| `symbol`, 12 symbols (hubs folded: 12 members, callers grouped by file, 20 callees) | 8,496 | 6,364 | **-25%** |
| `file_outline`, 9 files (over 60 symbols summarised) | 19,867 | 15,851 | **-20%** |
| `impact`, 9 symbols (crowded levels grouped by file) | 6,322 | 5,391 | **-15%** |
| `references`, 9 names (default limit 100, total and next offset still reported) | 3,593 | 3,305 | **-8%** |
| `repo_overview`, `repo_map(2000)` | 7,928 | 7,928 | 0% |
| all 36 measurements | 85,336 | 67,162 | **-21%** |

No single measurement got larger. The biggest single savings are hubs: `fastapi/routing.py` outline 6,131 to 2,885,
`FastAPI` symbol 1,371 to 744, `Context` (hono) 1,252 to 810. Precision moved the other way on the same nine
tasks: the expected answer is in the top five results for 9/9 (was 7/9), mean reciprocal rank 0.54 to 0.72. Every
fold names the call that lists the rest (`full=True`, `file_outline`, `references`), so nothing is hidden, only
deferred. The hooks cost about 40 ms per tool call (20 ms of it Python start-up) and add nothing to the transcript
unless they deny or orient. (`task_context` has since been trimmed further to bodies for the strongest four items:
24,408 tokens on the same nine tasks, -38% against 0.1.2.)

### The plugin in real Claude Code sessions

Claude Code 2.1.325 (`claude -p`, Sonnet) answering six relationship questions, three about fastapi (Python,
135k lines) and three about hono (TypeScript, 95k lines): a "where is X and which test covers it" question, a "what
breaks if X changes its signature" question and a "how does a request flow from A to B" question per repository.
Each question ran four times per arm, once with only the built-in tools and once with the plugin loaded
(`--plugin-dir`); the table is the mean of the three warm-cache passes (18 runs per arm), figures from Claude's own
usage report, tool calls counted from the stream. Both arms reached the right functions and lines.

| Mean per question, warm cache, 18 runs per arm | without plugin | with plugin |
|---|---:|---:|
| tool calls | 3.8 | 2.3 (-40%) |
| whole files read | 1.4 | 0 |
| API time | 9.8 s | 6.9 s (-30%) |
| output tokens | 1,274 | 814 (-36%) |
| new input tokens | 2,086 | 2,036 (-2%) |
| cost | $0.027 | $0.023 (-17%) |

Per repository: fastapi 3.0 vs 4.8 calls and $0.024 vs $0.030, hono 1.6 vs 2.8 calls and $0.022 vs $0.025. Five of
the six questions cost less with the plugin; the hono "how does a request flow" question cost more ($0.037 vs
$0.027) because the route answer plus the bodies the model then fetched outweighed reading two files. Of the 24
plugin-arm runs, 23 had no tool error of any kind and none showed a permission prompt; the one error was a shell
grep the guard refused, answered inside the refusal. The plugin arm never read a whole file: `task_context` 12
times, `symbol_source` 22, `references` 9, `impact` 8, `find_symbols` 7 against 56 Greps and 32 Reads without it.

What moved these numbers during 0.1.3: `task_context` includes bodies only for tasks and "how does it work"
questions (a "where is X" answer dropped from about 3,000 to 1,300-2,200 tokens); for "how does X reach Y"
questions it follows the route (calls made inside closures count as the enclosing method's, constructing a class
runs its constructor, route nodes are kept even when they are weak lexical matches); the orientation routes "who
calls / what breaks" to `references` and `impact`, which are always-loaded; and a denied identifier grep carries the
callers in its reason. Before those changes the first three questions cost 13% more with the plugin than without
and used 56% more new input tokens. The always-load decision is measured, not assumed: with
`CODEATLAS_ALWAYS_LOAD=none` the same questions took 6-7 calls instead of 2-4 (two `ToolSearch` round trips, then a
fallback to Grep/Read) and cost 50-100% more. Auto-approve is measured too: with no permission rule for the server
at all, five CodeAtlas calls ran without a prompt while an unlisted Bash call was refused. The plugin's always-on
cost is +3,627 prompt tokens per request (tool names and instructions, the always-loaded schemas, the orientation,
the skill and agent descriptions); 23 instead of 39 tools would save only about 390 of those, which confirms the
other schemas are deferred.

`claude plugin eval codeatlas-plugin --scaffold --mocks off --allow-tools "mcp__plugin_codeatlas_atlas__*"` runs the
two bundled cases with and without the plugin. At the default three runs per case every run passed in both arms:
the navigation question is answered with path:line evidence either way (through CodeAtlas tools and no whole-file
read with the plugin, through Grep and two Reads without it; 6 turns against 5 in that set, 3 against 5 in an
earlier single run), and the non-code question calls no CodeAtlas tool in any run, so the plugin does not trigger
on general questions. About $9 of API calls paid for every
real-session measurement in this section. The spread between runs is large, so the differences were tested
(Welch's t, 18 warm runs per arm): fewer tool calls (p = 0.002), no whole-file reads (p = 0.0005), less API time
(p = 0.0015) and fewer output tokens (p = 0.0017) are established; the 17% lower cost (p = 0.23) and the 2% fewer new
input tokens (p = 0.93) are not distinguishable from noise at this sample size. In words: the plugin reliably answers
with fewer, cheaper-to-produce steps and never reads whole files; it does not reliably put fewer input tokens into
context than a careful grep-and-read baseline on narrow questions.

## The guard: advice becomes a rule

Telling the model to "use CodeAtlas" in CLAUDE.md is not enough: the built-in Grep, Glob, Read and Edit schemas
are always loaded while MCP tools are deferred, so the model reaches for them by default. The plugin ships a
PreToolUse hook that, in a repository with a `.codeatlas/index.db` and only for files the index knows, denies the
built-in call and names the CodeAtlas call to make instead, so the model recovers in the same turn:

| Built-in call | What happens |
|---|---|
| whole-file Read of an indexed source file | **answered in place** by the plugin's mod: the file's top-level declarations with line ranges, as the Read's own result (no denial, no retry) |
| Grep for an identifier | **answered in place** by the mod: its definitions and its uses grouped by file, as the Grep's own result |
| Glob, text-regex Grep, `grep`/`rg`/`find` in any Bash statement | denied, redirected to `grep`, `find_symbols`, `references`, `list_files` |
| `cat` of an indexed file through Bash | denied, redirected to `file_outline`, then `symbol_source` for the declarations needed |
| Edit whose old_string lies inside a declaration (up to 150 lines) | `replace_symbol("<the enclosing symbol>", ...)`, `insert_code` |
| Write over an existing source file, `sed -i`, `>`, `tee`, inline scripts that write | `replace_symbol` / `insert_code` (Write is for new files) |
| any command that sets `CODEATLAS_GUARD` | denied: the switch is the user's, not the model's |

Two details decide whether the guard holds. **Every statement is inspected**, so `export X=1; cd src; A=$(find . -name
y); sed -n 1,50p $A | grep -v '^//'` does not hide its search behind a chain, and a `cd` carries into `$(...)`.
**No deny message names the off switch**, so the model cannot talk itself out of the guard.

The mod (`codeatlas-plugin/hooks/register.js`, Claude Code 2.1.287 or later) runs before the guard: when it can
answer a Read or Grep from the index it does, and the guard never fires; what it lets through, the guard still
judges. What the index does not cover is never gated or answered: unindexed files, docs and config, bounded partial
reads (an offset with a limit of 250 lines or fewer), text regexes, edits outside declarations, and anything in a
directory outside the indexed repository, such as a reference checkout you are reading upstream logic from. That last one matters: CodeAtlas has
no index there, so a denial would leave no working tool. `CODEATLAS_GUARD=search` keeps only the search redirects
and `CODEATLAS_GUARD=off` disables the guard, both set by you in your own environment.

## Only project folders get indexed

CodeAtlas refuses to index anything that is not a project: your home directory, the user folders inside it
(Desktop, Documents, Downloads, AppData, OneDrive, ...), drive / filesystem roots and system directories
(Windows, Program Files, `/usr`, `/Applications`, ...). Projects *inside* those folders are fine
(`~/Desktop/my-app`). `codeatlas init`, `codeatlas serve` and the library all enforce it, and the Claude Code
guard ignores a stray index at such a location. `CODEATLAS_ALLOW_ANY_ROOT=1` overrides it deliberately.

## How it stays fast on large repos

- One tree-sitter walk per changed file collects definitions, calls, identifier uses, tokens, complexity and
  patterns; references to new names are found through a token index, never by re-reading files.
- Incremental everything: a save re-parses one file; PageRank, FTS and relationship inventories refresh lazily
  and are throttled on no-op checks.
- Budgets adapt to repository size, scope, candidate count and risk terms; every response is bounded and says so.

## Development

```bash
uv sync && uv run pytest          # 200+ tests, fixture repos in several languages
cd dashboard && npm install && npm run dev   # React dashboard against a running `codeatlas serve`
```

MIT License.
