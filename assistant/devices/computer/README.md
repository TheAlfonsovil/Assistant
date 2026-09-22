# Computer

The computer branch is the only real device branch in V1. Its capabilities are registered through `ToolRegistry`:

- `filesystem`: read, write, list, exists, info, search (recursive, limited to 500 results)
- `shell`: exec
- `git`: status, diff, log, branch, checkout, add, commit
- `project`: analyze, read, audit, validate, create, initialize, scaffold, modify, edit
- `system`: info (read-only local diagnostics)
- `codegraph`: build (bounded Python module, symbol and import graph)
- `web`: search, fetch (public HTTP(S) only)
- `browser`: inspect, open, log, close_tab, close_site, close_browser

Future computer capabilities should be added as focused tools or capabilities here, not inside the Task Engine.

`project.analyze` is a bounded structural inventory. `project.audit` is a
read-only inventory of safe manifests, detected tests and limitations; it
accepts `run_tests` (boolean, default `false`) and only executes a detected
test command when that value is `true`. `project.create` still only creates a
direct child directory. `project.initialize` creates a stack-neutral workspace with a manifest and
artifact directories. `project.validate` runs stack-aware build, test, Python
syntax and Docker Compose configuration checks after a change. `project.scaffold` supports Vue + Java 25 /
Spring Boot 4 as well as Python FastAPI or Flask projects with a static HTML
frontend. `project.modify` applies explicit, bounded file changes for a named
feature and can run explicitly supplied validation commands.

The `search` and `scrape_url` ideas from the `ai_testing` prototype are intentionally
implemented as a lightweight HTTP adapter, without browser automation or
persistent browser profiles. Search results are limited and public/local-network
boundaries are checked. Chromium tabs and URLs are observable when a DevTools
port is enabled; otherwise the Windows fallback reports browser windows and an
explicit limitation. Browser interactions are appended to
`data/browser-interactions.jsonl` with action, origin, browser identity, tab
identity, URL and result. Closing a tab requires an observed `browser_id` and
`tab_id`; closing a complete browser requires its observed `window:<pid>` identity.
