# Computer

The computer branch is the only real device branch in V1. Its capabilities are registered through `ToolRegistry`:

- `filesystem`: read, write, list, exists, info, search (recursive, limited to 500 results)
- `shell`: exec
- `git`: status, diff, log, branch, checkout, add, commit
- `project`: analyze
- `system`: info (read-only local diagnostics)
- `codegraph`: build (bounded Python module, symbol and import graph)
- `web`: search, fetch (public HTTP(S) only)

Future computer capabilities should be added as focused tools or capabilities here, not inside the Task Engine.

The `search` and `scrape_url` ideas from the `ai_testing` prototype are intentionally
implemented as a lightweight HTTP adapter, without browser automation or
persistent browser profiles. Search results are limited and public/local-network
boundaries are checked. Browser automation and scraping still need a separate
adapter with explicit permissions if they become necessary.
