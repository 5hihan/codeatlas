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
(`task_context`, `find_symbols`, `symbol_source`, `references`, `impact`) are flagged `anthropic/alwaysLoad` so the
first useful call needs no search round-trip (`CODEATLAS_ALWAYS_LOAD` overrides the list; `symbol` and
`file_outline` were used once each in twenty measured sessions, so their schemas are deferred like the other 32),
every navigation tool carries `readOnlyHint`/`idempotentHint` so Claude Code runs them in parallel with other
read-only calls, and the server instructions stay inside the 4,096-character cap. Measured with a one-turn
`claude -p` with and without `--plugin-dir`, the plugin adds 3,636 prompt tokens to every request: 1,323 for the
five always-loaded schemas (plain string and integer defaults instead of `Optional` schemas), about 1,360 for the
39 tool names and the instructions, 644 for the skill, agent and command descriptions, 308 for the SessionStart
orientation; the previous build added 4,882. The SessionStart hook gives that orientation (index size, which tool
to reach for, and the rule not to Grep for an identifier or Read a whole indexed file) at startup, resume and
`/clear`, and a shorter reminder after every compaction, when models otherwise forget the index exists. Dropping
the rule to save about 110 tokens cost five Grep/Read calls after one `task_context` in a measured session, so it
stays. A PreToolUse hook auto-approves CodeAtlas's
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
tasks: the expected answer is in the top three results for 8/9 and in the top five for 8/9 (was 7/9), mean
reciprocal rank 0.54 to 0.72 (the one miss sits sixth: "how are dependencies resolved and injected into endpoints",
whose answer `solve_dependencies` shares no word with the question). Every
fold names the call that lists the rest (`full=True`, `file_outline`, `references`), so nothing is hidden, only
deferred. The hooks cost about 40 ms per tool call (20 ms of it Python start-up) and add nothing to the transcript
unless they deny or orient. (`task_context` has since been trimmed further to bodies for the strongest four items:
24,408 tokens on the same nine tasks, -38% against 0.1.2.)

### The plugin in real Claude Code sessions

Claude Code 2.1.325 (`claude -p`, Sonnet) answering ten questions about fastapi (Python, 135k lines) and hono
(TypeScript, 95k lines). Six are narrow relationship questions, three per repository: "where is X and which test
covers it", "what breaks if X changes its signature" and "how does a request flow from A to B". Four are task-shaped:
"I want to add a body-size check to body parsing / a constructor option that changes route registration: which
functions would I change, who calls them, which tests cover them", "how does an exception in a dependency become an
HTTP error response" and "where is the request path normalised before routing, and which tests cover that". Each
question ran with only the built-in tools and with the plugin loaded (`--plugin-dir`); the tables are means of the
warm-cache passes (the first run of each question is dropped), figures from Claude's own usage report, tool calls
counted from the stream. Both arms reached the right functions and lines on every question. The plugin arm is this
build as committed; the baseline was run once and reused, since it does not change. One caveat on method: the
retrieval changes in this build were made while looking at `task_context`'s output for these ten questions, so
they are not a held-out set; the nine-task precision benchmark above uses different questions and moved from a mean
reciprocal rank of 0.70 to 0.72 under the same changes.

| Mean per question, warm cache | without plugin (30 runs) | with plugin (20 runs) | Welch's t |
|---|---:|---:|---:|
| tool calls | 4.3 | 2.5 (-42%) | p = 0.0001 |
| Read calls | 1.4 | 0.15 (-90%) | p < 0.0001 |
| tool errors | 0 | 0 | |
| API time | 11.6 s | 8.7 s (-25%) | p = 0.007 |
| output tokens | 1,506 | 1,050 (-30%) | p = 0.002 |
| new input tokens | 3,034 | 3,431 (+13%) | p = 0.47 |
| input processed (new + cache reads) | 68,741 | 73,865 (+7%) | p = 0.39 |
| cost | $0.034 | $0.031 (-7%) | p = 0.52 |

The two kinds of question behave differently, so they are worth separating:

| Mean per question, warm cache | narrow six: without (18) | with (12) | task-shaped four: without (12) | with (8) |
|---|---:|---:|---:|---:|
| tool calls | 3.8 | 1.9 (-49%, p = 0.0002) | 5.1 | 3.4 (-34%, p = 0.019) |
| Read calls | 1.4 | 0.17 (p = 0.003) | 1.5 | 0.13 (-92%, p = 0.0002) |
| API time | 9.8 s | 6.9 s (-30%, p = 0.005) | 14.3 s | 11.3 s (-21%, p = 0.07) |
| output tokens | 1,274 | 837 (-34%, p = 0.006) | 1,854 | 1,370 (-26%, p = 0.03) |
| new input tokens | 2,086 | 2,951 (+41%, p = 0.26) | 4,457 | 4,152 (-7%, p = 0.59) |
| input processed | 66,000 | 65,788 (0%) | 72,853 | 85,981 (+18%, p = 0.15) |
| cost | $0.027 | $0.026 (-3%, p = 0.85) | $0.043 | $0.038 (-11%, p = 0.24) |

Per question the plugin cost less on seven of the ten (`h_compose`, "what breaks if compose changes", went from 3.0
calls and $0.030 to one `impact` call and $0.012; the fastapi dependency-exception question from 6.3 calls and
$0.048 to 2.0 calls and $0.036; the WebSocket route question from 6.7 calls and four Reads to 2.5 calls and one),
the same on the body-size task ($0.048 either way, 5.3 against 5.5 calls: `task_context` names
`request_body_to_args` and the closure that calls `request.body()`, and the model still grepped for the exact read
sites), and more on two: the hono match question ($0.018 to $0.021) and the hono request-flow question ($0.027 to
$0.043), where one `task_context` answer with route and bodies is larger than the two targeted greps the baseline
happened to make. What the arms did: without the plugin, 75 Greps, 43 Reads, 9 shell commands and 2 Globs over 30
runs; with it, 15 `task_context`, 7 `references`, 5 `symbol_source`, 4 `impact`, 2 `find_symbols`, one index
`grep`, and 8 Greps, 4 shell commands, 3 Reads and one Glob over 20 runs.

The honest reading: fewer tool calls, fewer output tokens, less API time and almost no whole-file reads are
established across all ten questions and within each group; the cost difference points the right way (-7% overall,
-11% on task-shaped questions, where the previous build was +12%) but is within noise at this sample size, because
cache reads are billed at a tenth of new input and the plugin puts somewhat more into context per request (its
always-on prompt cost, below, and larger first answers) while it saves output tokens and turns. Spend on every
real-session measurement in this section: about $14 of API calls.

#### Seamless, measured

| What a user would notice | Measured |
|---|---|
| permission prompts for the plugin's tools | none: the read-only tools are auto-approved by a hook (with no permission rule at all, five CodeAtlas calls ran without a prompt while an unlisted Bash call was refused) |
| tool errors or denials in the transcript | 0 in 30 plugin-arm runs (0 in 30 baseline runs) |
| a refused Grep or Read | none reaches the model: a forced whole-file `Read src/compose.ts` came back as "src/compose.ts: 73 lines, 2 symbols; the index answered this Read with the file's outline. Top-level declarations: compose f:15-73; dispatch f:32-71. Read one declaration with symbol_source(...)", and the model answered from it |
| answers that talk about the tooling | 2 of 30 say "using the CodeAtlas index" as their source; none mention a hook, a refusal or a denied call |
| session start | +1.2 to +1.7 s on a one-turn `claude -p` session (MCP server start, index freshness check, SessionStart hook), 2.2-5.5 s without the plugin against 3.8-6.8 s with it, four pairs |
| per tool call | about 40 ms of PreToolUse hook (20 ms of it Python start-up) |
| files outside the index | docs, config and unindexed files are never gated |

What moved these numbers during 0.1.3: `task_context` includes bodies only for tasks and "how does it work"
questions (a "where is X" answer dropped from about 3,000 to 1,300-2,200 tokens); for "how does X reach Y"
questions it follows the route (calls made inside closures count as the enclosing method's, constructing a class
runs its constructor, route nodes are kept even when they are weak lexical matches); the orientation routes "who
calls / what breaks" to `references` and `impact`, which are always-loaded; a denied identifier grep carries the
callers in its reason, and the mod answers whole-file Reads and identifier greps in place. For task-shaped
questions: words that describe the answer wanted ("who calls them, which tests cover them, give file:line anchors")
and the project's own name ("in this FastAPI repository") no longer select symbols; `scripts/` and `tools/` rank
like examples; a bare "constructor" boosts only the constructors of classes the task names; items of a change task
carry their callers (`callers: solve_dependencies fastapi/dependencies/utils.py:800`); a symbol that calls a task
term counts ("body parsing" happens where `request.body()` is called); TypeScript private members (`#addRoute`) are
indexed as identifiers, so their six call sites are references where there were none (one measured answer had
said "CodeAtlas reports no internal callers for #addRoute"). Before those changes the task-shaped four cost 12% more
with the plugin than without and processed 45% more input; the first three narrow questions had cost 13% more and
used 56% more new input tokens. The always-load decision is measured, not assumed: with `CODEATLAS_ALWAYS_LOAD=none`
the same questions took 6-7 calls instead of 2-4 (two `ToolSearch` round trips, then a fallback to Grep/Read) and
cost 50-100% more. Auto-approve is measured too: with no permission rule for the server at all, five CodeAtlas calls
ran without a prompt while an unlisted Bash call was refused.

`claude plugin eval codeatlas-plugin --scaffold --mocks off --allow-tools "mcp__plugin_codeatlas_atlas__*"` runs the
two bundled cases with and without the plugin. At the default three runs per case every run passed in both arms:
the navigation question is answered with path:line evidence either way (through CodeAtlas tools and no whole-file
read with the plugin, through Grep and two Reads without it), and the non-code question calls no CodeAtlas tool in
any run, so the plugin does not trigger on general questions.

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
