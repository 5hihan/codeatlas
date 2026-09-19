<h1 align="center">CodeAtlas</h1>

<p align="center">
  <strong>A code map, fix loop and live dashboard for coding agents. Tree-sitter, no LLM in the loop, updates on every save.</strong><br>
  <em>Works with Claude Code, Codex and Cursor (any MCP client).</em>
</p>

<p align="center">
  <a href="#install"><img alt="install" src="https://img.shields.io/badge/install-one%20line-2ea44f"></a>
  <a href="#what-it-does"><img alt="tools" src="https://img.shields.io/badge/MCP%20tools-36-7aa2ff"></a>
  <a href="#languages"><img alt="languages" src="https://img.shields.io/badge/languages-17-c792ea"></a>
  <a href="#snipe-the-fix-loop"><img alt="snipe" src="https://img.shields.io/badge/fix%20loop-SNIPE-ff6b6b"></a>
  <a href="LICENSE"><img alt="license" src="https://img.shields.io/badge/license-MIT-lightgrey"></a>
</p>

> *Your agent just opened a 216,000-line monorepo. It needs one function, its callers and the test that covers it.
> It should not read 30,000 tokens of files to find them.*

CodeAtlas indexes a repository with tree-sitter into a symbol graph (functions, classes, references, imports,
env keys, SQL tables, patterns) and exposes it over MCP as compact, `path:line`-anchored answers. An agent asks
`task_context("fix expired refresh tokens")` and gets the five symbols that matter, their callers, the test that
exercises them and the config they read, in ~3k tokens instead of ~30k. The index updates itself within a second of
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
codeatlas init        # seeds the index and SNIPE.md, writes agents + guidance, idempotent
```

<details>
<summary><b>Per platform</b></summary>

| Platform | How | Then |
|---|---|---|
| **Claude Code** | plugin `codeatlas@codeatlas` installed at user scope by the installer (or `claude plugin marketplace add 5hihan/codeatlas` + `claude plugin install codeatlas@codeatlas`) | `/codeatlas:find <question>`, `/codeatlas:snipe`, `/codeatlas:review-changes`, or let Claude delegate to the `codeatlas-navigator`, `codeatlas-sniper`, `codeatlas-reviewer` agents |
| **Codex** | `mcp_servers.codeatlas` in `~/.codex/config.toml` + skill in `~/.agents/skills/codeatlas` | type `$codeatlas` (Codex uses `$`, not `/`); the MCP tools are available in every project |
| **Cursor** | `codeatlas init cursor` writes `.cursor/mcp.json` in the repo | the MCP tools appear in the agent |
| **Anything else (MCP)** | `codeatlas serve --repo <path>` over stdio | 36 tools, see below |

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
| `task_context` | **start here**: the symbols relevant to a task (BM25 + PageRank + graph proximity + pattern hints), source for the strongest ones, tests, config/docs, inside an adaptive token budget; each item says why it is there and how to expand it |
| `repo_overview`, `repo_map` | languages, packages, hubs, entry points; a PageRank-ranked map that fits a budget |
| `file_outline`, `find_symbols`, `symbol`, `symbol_source` | outlines with reference counts and pattern tags; signature, doc, callers, callees; just the lines of one symbol |
| `references`, `impact`, `triggers`, `dependencies`, `module_graph` | who uses it, what breaks, what makes it run, import graphs |
| `relationships` | code ↔ config ↔ schema: env keys read and declared, SQL tables declared and used, literal config paths |
| `programming_patterns` | 12 patterns (generics, closures, decorators, async, generators, error handling, ...) at real locations with evidence |
| `change_context` | git blast radius: changed symbols, consumers to re-check, config/schema touched, tests to run, risk |
| `hotspots`, `unreferenced`, `findings`, `grep` | complexity, dead code, comment tags, regex search |

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
codeatlas context "<task>" | changes [--base REF] | map | overview | outline PATH | symbol NAME | impact NAME
codeatlas snipe [board|scan [--run tests]|next|report FILE]  the loop from the shell
codeatlas plugin export DIR | dashboard --open | open | doctor
```

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
