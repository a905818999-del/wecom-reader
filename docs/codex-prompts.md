# Codex 接入提示词（Codex App 起步用）

> **使用场景**：把 wecom-reader 接入 ChatGPT Codex，让 Codex 读 issue 起 PR。
> **前提**：PR #1/#4/#5 已 merge，main 上有 CI（pytest + ruff + coverage）。本地没有 `.db` / `*.db-wal` / 真实聊天数据。
> **AGENTS.md 已在仓库根**：Codex 启动时会自动读，红线已声明。

---

## 1. Codex App 一次性接入 prompt（首次连仓库用）

> 复制这一段贴进 Codex App 的"项目说明 / custom instructions"或会话第一轮：

```
You're collaborating on https://github.com/a905818999-del/wecom-reader — a Python
library to read WeCom (企业微信) local encrypted SQLite chat records.

Workflow rules:
1. Read AGENTS.md first. 5 hard rules, especially:
   - No real data in PRs (no .db, no -wal, no actual messages)
   - Never decrypt or commit E:\WXWork\... content
   - 100% coverage on changed `wecom_reader/db/message.py` paths
   - mypy strict is the goal; pragmatic for now
   - Pragmatic error codes (ErrorCode enum) preferred over exceptions

2. Pick ONE issue at a time from
   https://github.com/a905818999-del/wecom-reader/issues
   - Issue #6 (BUG: messages incomplete) — already fixed in PR #2, just merge
   - Issue #8 (BUG: web UI image/mention) — TODO, this is your first real task
   - Issue #7 (BUG: WAL real-time) — research-heavy, do LAST

3. For each issue:
   a. Create branch:  fix/<issue-num>-<short-slug>
   b. Reference the issue in the PR description:  Closes #N
   c. Add tests under tests/ — every fix gets a regression test
   d. Run `pytest tests/ -v --cov=wecom_reader --cov-report=term-missing` locally
      (or just push and let CI run — same gates)
   e. PR title:  [<type>] <scope>: <one-line summary>
      types: fix | feat | chore | docs | refactor | test
      scope: db | crypto | reader | web | cli | docs | ci

4. Style:
   - 直给 comments in Chinese when explaining "why"; code identifiers in English
   - Prefer `# pragma: no cover` for unreachable defensive code over fake tests
   - Use pytest fixtures, not unittest.TestCase
   - Type hints everywhere; `from __future__ import annotations` in new modules

5. Don't:
   - Don't merge your own PRs — zhen reviews and merges
   - Don't push to main directly
   - Don't include any output that mentions E:\ paths or real WeCom data
   - Don't install heavy deps (no pandas, no pytorch, no playwright unless asked)
```

---

## 2. 派发 issue #8（web UI image/mention）的具体 prompt

> 复制贴进 Codex 对话（在新会话 / 续接 Codex 会话都行）：

```
Task: close issue #8 on a905818999-del/wecom-reader.
Title: [BUG] Web UI 渲染：图片消息不显示 / @ 提及不高亮

Context:
- wecom_reader/web.py:184-196 has a render loop that does
  `escapeHtml(content)` for every message regardless of content_type.
- wecom_reader/image_resolver.py (PR #3, branch feat/image-resolver)
  is the resolver API. It's MERGED on main already.
- Content types you need to handle:
  4  = image (PR #3 already covers this — use it)
  15 = image/file
  (don't worry about voice / video / card yet, leave them as text fallback)

Plan:
1. Create branch:  feat/web-image-rendering-and-mentions
2. Backend — wecom_reader/web.py:
   - Add route GET /api/image/<msg_id>
     → reader.image_resolver.resolve_image(msg_id) → send_file(path)
     → 404 if not resolved
   - Add test tests/test_web.py with at least:
     - 200 happy path (use a tiny fixture PNG)
     - 404 when resolve_image returns None
     - 404 when resolved file missing
3. Backend — wecom_reader/db/message.py:_parse_content:
   - Parse protobuf content for @mentions, return list[str] under `mentions` key
   - Add test for: text with no mention → []; "@nickname hello" → ["nickname"]
4. Frontend — wecom_reader/web.py (the inline JS in render_messages()):
   - Branch on m.content_type === 4 || 15 → render <img> with src=/api/image/<msg_id>
   - For text containing @nickname → wrap in <span class="mention">@nickname</span>
   - Add .mention CSS to the existing <style> block
5. Don't break existing tests. `pytest -v` should be green.
6. Don't add any deps (use stdlib send_file, no Pillow).
7. Don't expose real local paths in API responses.
   The /api/image/<msg_id> endpoint should NOT echo file paths.
8. Coverage on web.py ≥ 80%.

Acceptance:
- [ ] pytest -v green
- [ ] pytest --cov=wecom_reader.web ≥ 80%
- [ ] No E:\ paths in any test or fixture
- [ ] PR references "Closes #8"
- [ ] PR description includes: issue #8 link + before/after rendering example

When done, push branch and open PR. Don't merge.
```

---

## 3. 派发 issue #7（WAL 实时合并）的 prompt（**分两轮**）

> 第一轮：研究 task，没有 PR 产出

```
Research task (no PR expected, write findings to docs/wal-format-notes.md):

Issue #7: message.db-wal is not merged into reader snapshots. The current init()
in wecom_reader/reader.py skips -wal files. WeCom 4.x writes to WAL constantly,
so the most recent N hours of messages are invisible until user clicks "refresh"
in WeCom UI (= full re-init).

Background you can build on:
- wecom_reader/crypto/decrypt.py has decrypt_wal_pages() / decrypt_wal_file()
  helpers (already implemented, just not wired up). Read them first.
- WAL file header first 4 bytes are `0x377f0682` — NOT standard SQLite WAL magic.
- Main db uses AES-128-CBC with a 16-byte global key, page size 4096.
- Read tests/smoke_message.py and the "WAL 未合并" section in
  .workbuddy/memory/2026-06-26.md for prior investigation.

Deliverable (docs/wal-format-notes.md):
1. Confirm: is the 0x377f0682 magic a custom WCDB/wxSQLite3 magic? Find references.
2. For each frame in the WAL:
   - Header layout (24 bytes): what are salt, checksum, page_no fields?
   - Is the page_no stored the same way as main db?
   - Is encryption identical to main db (same key + same IV derivation)?
3. Try decrypting one frame with main db key, page_no from header, AES-128-CBC,
   and document the failure mode (garbage? partial magic? wrong page_size?).
4. Compare against wxSQLite3 source / WCDB source if findable on GitHub.
   Look for branch: Tencent/wcdb, askdaddy/wxsqlite3
5. Suggest a concrete next step: is it feasible to merge WAL automatically?

Don't write production code yet. Just the notes file + commit + push.
PR title: [docs] wal-format-research-notes (no Closes #X reference)
```

> 第二轮（拿到研究笔记后）：工程 task

```
Implement issue #7 based on docs/wal-format-notes.md (read it first!).

Pick approach A/B/C from the issue body based on what the notes recommend.
Most likely: approach A (decrypt WAL + write merged db) or C (shadow db).

Whatever approach:
- Add tests with synthetic WAL content (you can hand-craft a 24-byte header +
  a 4096-byte page with known plaintext, encrypt it with the same key, then
  feed it through your decoder)
- Don't depend on real WeCom data in any test
- Add a `wal_present` and `wal_warning` field to init() result so zhen knows
  if WAL is being processed
- Coverage on wecom_reader/crypto/decrypt.py new paths ≥ 90%
- pytest -v green
- Don't break the WAL detection that PR #2 already added

Branch: feat/wal-merge
PR: Closes #7
```

---

## 4. Codex 跑出 PR 之后，zhen 的 review checklist

> 这是 zhen review Codex PR 时用的（贴进 PR 评论里或自己用）

```
[ ] PR title uses [<type>] <scope>: format
[ ] PR description references "Closes #N"
[ ] Branch name matches: <type>/<short-slug>
[ ] No file in wecom-reader/tests/ references E:\WXWork or real data
[ ] No .db / -wal / -shm files in diff
[ ] git diff --stat shows tests/ lines ≈ code lines (≥ 0.5x ratio)
[ ] CI green on push
[ ] Coverage on changed modules ≥ 80% (100% for db/message.py)
[ ] No new dep without discussion
[ ] Commit messages have issue ref: "(#N)" or "Closes #N"
```

---

## 5. 失败回滚 / 越界处理

如果 Codex：
- 提了 PR 但覆盖 < 80% → comment "覆盖率不达标，参考 #6 修复思路补 test"
- 引入了新依赖 → comment "为什么需要？能不能用 stdlib 替代？"
- commit 里包含了真实数据 → **立刻 revert**，评论让 Codex 重新提交
- 越权 merge 了自己的 PR → 撤销 merge + 写一条 "以后 Codex 不要 merge 自己的 PR"
- 跑了一晚上没产出 → 不催，第二天再看；Codex 不需要 vibe pressure

---

## 6. 第一次连 Codex 的实际操作步骤

```
1. 打开 ChatGPT Codex App（或网页 code.openai.com）
2. 连 GitHub: Settings → Integrations → 选 a905818999-del
3. 开新 session，仓库选 wecom-reader
4. 粘 #1 那一段"接入 prompt"作为 system context
5. 起第一个 task：粘 #2 那一段（issue #8）
6. 等 Codex 提 PR
7. 你（zhen）review + merge
```

---

## 7. 节奏建议

- 第一周：每周 1-2 个 issue 派给 Codex
- 周末：review Codex 一周的 PR，合并该合的，关掉该关的
- 每月：把 Codex 跑出来的"值得沉淀"经验写进 AGENTS.md 或 .workbuddy/memory/
- 别贪多：Codex 一次跑一个 task，parallel PR 会让 review 质量下降

---

## 附录：派发 issue #6 的 prompt（其实不需要）

> #6 已经被 PR #2 解决了，**不要派给 Codex**。zhen 自己 merge PR #2 就行。
> 留这个 prompt 在这里是为了完整参考：

```
This issue is already fixed in PR #2 (branch fix/multi-table-pagination).
Just merge PR #2 — no Codex task needed. Closing as resolved.
```