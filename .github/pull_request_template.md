## What
One sentence: what does this PR do?

## Why
Link the issue it closes (if any), or describe the problem.

## How
Brief technical summary — files changed, key design choices.

## Tests
- [ ] New tests added (specify which files)
- [ ] `pytest --cov=wecom_reader --cov-report=term-missing` runs clean
- [ ] Coverage on changed lines ≥ 80% (or justification)

## Real-data verification
For any pagination / query / decoding change, also run the regression
script against the canonical conversation `R:2910032769`
(61,343 main + 408 small + 0 kf messages). 11 offsets, see
`tests/smoke_message.py` for the pattern.

## Checklist
- [ ] Read `AGENTS.md` (5 red lines)
- [ ] Did NOT commit real chat data, decryption keys, or `wxwork_decrypted/`
- [ ] Did NOT commit `.coverage` / `api_at_*.txt` / `parse_output.txt`
- [ ] `mypy wecom_reader` is clean (or exceptions noted)
- [ ] `ruff check` and `ruff format --check` pass
- [ ] Commit message follows `type: short summary` format

## Out of scope
What this PR does NOT include (link follow-up issues if any).
