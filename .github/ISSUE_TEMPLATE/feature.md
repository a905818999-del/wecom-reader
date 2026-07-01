---
name: Feature request
about: Propose a new feature for wecom-reader
title: "[FEAT] "
labels: ["enhancement"]
---

## Goal
One sentence: what does this feature let the user do?

## Why
What problem does it solve? Is there a workaround today?

## Acceptance criteria
- [ ] Behavior 1
- [ ] Behavior 2
- [ ] Tests added (target coverage ≥80% on changed lines)
- [ ] CHANGELOG / README updated (if user-facing)

## Out of scope
What should this PR NOT do? (helps avoid scope creep)

## Test plan
How will you verify it works? Mention any real data conversation IDs
(e.g. `R:2910032769` is the canonical 61K-message regression dataset).

## Codex reminder
- Read `AGENTS.md` first (architecture + 5 red lines).
- Use the PR template (`.github/pull_request_template.md`).
- Run `pytest --cov=wecom_reader --cov-report=term-missing` before pushing.
- If real-data verification is needed, do NOT commit data — only commit
  regression test cases with synthetic fixtures.
