# Computer

The computer branch is the only real device branch in V1. Its capabilities are registered through `ToolRegistry`:

- `filesystem`: read, write, create, delete, list, exists, info, search, search_text
- `shell`: exec
- `git`: status, diff, log, branch, checkout, add, commit
- `process`: start, status, log, stop
- `deployment`: build, test, deploy, verify, rollback
- `project`: analyze, read, validate, create, edit
- `audit`: run
- `system`: info (read-only local diagnostics)
- `codegraph`: build (bounded project module, symbol and dependency graph)
- `types`: probe, hover, definition, references, rename, diagnostics, symbols (semantic answers from pyright)
- `debug`: probe, trace (run a program under debugpy and read real locals)
- `window`: probe, list, snapshot (desktop windows as a control tree, via UI Automation)
- `web`: search, fetch (public HTTP(S) only)
- `browser`: inspect, open, log, close_tab, close_site, close_browser

Future computer capabilities should be added as focused tools or capabilities here, not inside the Task Engine.

Tool implementations live in `actions/audit.py`, `actions/filesystem.py`,
`actions/shell.py`, `actions/process.py`, `actions/git.py`,
`actions/deployment.py`, and `actions/project_tool.py`. Project handlers are
split across `project_create.py`, `project_read.py`, `project_edit.py`, and
`project_validate.py`. `actions/registry.py` only composes and registers them;
browser, codegraph, semantics, system, and web each have their own sibling module.

`types` is the semantic layer: `lsp.py` is a small LSP client (Content-Length
framed JSON-RPC over stdio) and `semantics.py` turns its answers into bounded
tool output. It reports `available: false` with the reason when node or pyright
is missing, and it locates the symbol by name so a caller does not need to know
the exact column. `rename` is the semantic operation text search cannot do: the
server resolves the symbol, so the edit set is complete, and applying it is
opt-in (a truncated plan is never half-applied). `diagnostics` runs the batch
checker (`index.js` with `node` directly: 1.1 s against 18.5 s through `npx`)
and returns type errors as evidence.

`debug` is runtime truth: `dap.py` is a DAP client (same framing) for
`debugpy`, and `debugger.py` runs a program with breakpoints and reports each
stop with its frame and local values. Three protocol facts are encoded there
because they are not obvious: the `launch` response arrives only after
`configurationDone`, the program's output travels as `output` events, and the
session must be torn down inside the call that started it.

`window` is the desktop equivalent of `browser.snapshot`: `window.py` uses the
Windows accessibility tree (UI Automation) to list top-level windows and to walk
one window's controls with name, type, state and screen rectangle. It is
observation only — acting stays with the opt-in `input.*` — and it is pruned
because anonymous panes are noise: a control is kept when it has a name, is
actionable, or its type says something structural (a menu bar, a tab strip, a
document). Two host facts are handled explicitly: COM must be initialised **in
the thread that uses it** (hence the initialiser wrapper around every call) and
the API is blocking, so every call runs off the event loop.

The optional extras are declared in `pyproject.toml`: `debug` for `debugpy` and
`uia` for `uiautomation`. Neither is required to run the assistant; each tool
reports what is missing (`missing-debugpy`, `missing-uiautomation`).

`project.analyze` is a bounded structural inventory. `audit.run` is a separate,
structured evidence report; it does not modify project files directly, and
detected tests run by default. Set `run_tests=false` to skip them.
Audit contracts, profiles, report evaluation, and test discovery live in
`assistant/project_audit/`.
`project.create` makes a direct child of
the projects root whose whole content is the requested directories and
artifacts plus a runtime-owned manifest. No stack is scaffolded, no `README` or
configuration is invented, and no template id is accepted; a missing file
stays missing until a later `project.edit` writes it. `project.validate` runs
only the explicit commands supplied by the planner or worker. It does not
infer language stacks, tests, deployment tools, or Docker checks.
`project.edit`
applies explicit, bounded file changes for a named feature and can run
explicitly supplied validation commands.

The `search` and `scrape_url` ideas from the `ai_testing` prototype are intentionally
implemented as a lightweight HTTP adapter, without browser automation or
persistent browser profiles. Search results are limited and public/local-network
boundaries are checked. Chromium tabs and URLs are observable when a DevTools
port is enabled; otherwise the Windows fallback reports browser windows and an
explicit limitation. Browser interactions are appended to
`data/browser-interactions.jsonl` with action, origin, browser identity, tab
identity, URL and result. Closing a tab requires an observed `browser_id` and
`tab_id`; closing a complete browser requires its observed `window:<pid>` identity.
