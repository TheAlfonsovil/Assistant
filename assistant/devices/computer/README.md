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
- `web`: search, fetch (public HTTP(S) only)
- `browser`: inspect, open, log, close_tab, close_site, close_browser

Future computer capabilities should be added as focused tools or capabilities here, not inside the Task Engine.

Tool implementations live in `actions/audit.py`, `actions/filesystem.py`,
`actions/shell.py`, `actions/process.py`, `actions/git.py`,
`actions/deployment.py`, and `actions/project_tool.py`. Project handlers are
split across `project_create.py`, `project_read.py`, `project_edit.py`, and
`project_validate.py`. `actions/registry.py` only composes and registers them;
browser, codegraph, system, and web each have their own sibling module.

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
