---
name: Bug report
about: Report a bug in wecom-reader
title: "[BUG] "
labels: ["bug"]
---

## What happened
What did you do? What did you expect? What actually happened?

## Repro
Minimal code/command to reproduce:

```python
from wecom_reader import WeComReader
reader = WeComReader()
reader.init()
# ...
```

## Environment
- OS: (Windows 11 / macOS / Linux)
- Python: (3.10 / 3.11 / 3.12)
- wecom-reader version: (`pip show wecom-reader`)
- WeCom data version: (3.x / 4.x)

## Logs / traceback
Paste relevant output here.

## Suggested fix
Optional — if you have an idea.

## Codex reminder
- Reproduce first, then propose a fix in a PR.
- Add a failing test that reproduces the bug, then make it pass.
- Read `AGENTS.md` for the 5 red lines (esp. "no commit of real chat data").
