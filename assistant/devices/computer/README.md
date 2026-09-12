# Computer

The computer branch is the only real device branch in V1. Its capabilities are registered through `ToolRegistry`:

- `filesystem`: read, write, list, exists
- `shell`: exec
- `git`: status, diff, log, branch, checkout, add, commit
- `project`: analyze

Future computer capabilities should be added as focused tools or capabilities here, not inside the Task Engine.
