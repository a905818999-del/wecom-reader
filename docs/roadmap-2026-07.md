# 2026-07 月计划：wecom-reader 主力消化季

> **目标**：本月聚焦消化现有债（PR #3 收尾 + WAL 加密研究）和顺势扩张（HTTP facade 给 agent 集成入口）。
> **来源**：2026-07-03 对 seatalk-info-capture 借鉴项的"读+管/解密"二分评估，结论是 4 借鉴项里 2 项落地、2 项挂起，详见 `../.workbuddy/memory/2026-07-03.md`。
> **关键原则**：先消化已有债，再扩大面；任何"加 resolver / 加 order 参数"等"为借鉴而借鉴"的工作**严禁先于 image_resolver 测试补完启动**。

---

## 一、状态看板（一眼读懂）

| 标识 | 任务 | 优先级 | 阻塞 | 状态 |
|---|---|---|---|---|
| **T1** | WAL 加密格式研究 | P0 | 无（独立研究）| pending |
| **T2** | HTTP facade `/api/v1/*`（含 `/health` 暴露 wal_present）| P0 | 无 | pending |
| **T3** | Windows 多壳入口（.bat / .ps1）| P1 | 挂车在 T2 或 T4 PR | pending |
| **T4** | image_resolver 单元测试（Codex 派发候选）| P1 | 无（Codex 接入后可启动）| pending |
| **T5** | info-pipeline 分支清理 + docs PR | P2 | 无 | pending |
| **T6** | Resolver 工厂化（所有 content_type）| P2 | **阻塞 T4** | blocked |
| **T7** | `order=asc/desc` 参数 | P3 | **等 watch 场景** | deferred |

**已完成（不许回退）**：
- PR #1 `initial-release`（merged）
- PR #2 `fix/multi-table-pagination`（UNION ALL 修复，merged）
- PR #3 `feat/image-resolver`（merged，无单测，触发 T4 技术债）
- PR #4 `chore/add-agents-md`（merged）
- PR #5 `chore/ci-templates`（merged）

---

## 二、详细任务说明 + 验收标准

### T1 — WAL 加密格式研究（P0，最高）

**为什么 P0**：message.db-wal 7.9MB 永久不可读，意味着 wxwork 当前**最近 7.9MB 的交易全部丢失**。每多放一天多丢一天数据。详见 `2026-06-26.md` "WAL 加密格式研究" 章节。

**做什么**：
1. 拿 wxsqlite3 源码（GitHub `utelle/wxsqlite3`）读 `Cipher.cpp` / `Cipher.h`，定位：
   - AES key 派生函数
   - Page encrypt/decrypt 的 hook 点
   - WAL 的 page_no 到 main db 的 page_no 映射是否一致
2. 用 `message.db` 已解密 page + `message.db-wal` 同 page 字节差，做 oracle 反推密钥派生
3. 跑 `PRAGMA wal_checkpoint(FULL)`（前提：能解锁或绕过密钥）
4. 在 `wecom_reader/crypto/` 加 `decrypt_wal.py` 真正实现

**怎么验收**：
- [ ] `message.db-wal` 解出来 ≤ 5% 数据有 SQLite magic（首页定位）
- [ ] 解出至少一个 page，跟 main db 同 page_no 内容能对得上或能解释差异
- [ ] `wal_warning` 字段移除（不再需要），替换为实际可读状态
- [ ] 测试：合成 WAL 字节 → 走 decrypt → 验证输出

**关联 PR**：独立 PR `feat/wal-decrypt`，**不动 reader.py 现有 public API**

**工时估算**：4–8 小时（取决于 wxsqlite3 源码阅读速度）

---

### T2 — HTTP facade `/api/v1/*`（P0，顺势）

**为什么 P0 顺势**：HTTP facade 复用现有 Flask web + reader 实例，**改 web.py 一个文件加 blueprint 即可**，几乎免费的扩张。同时承载"暴露 wal_present / wal_warning"的双重收益（解决 T1 可见性）。

**做什么**：
1. `wecom_reader/web.py` 新增 `api_bp` blueprint，挂在 `/api/v1/`
2. 端点设计：
   ```
   GET  /api/v1/health          → {"status": "ok", "wal_present": [...], "wal_warning": "...", "reader_status": "..."}
   GET  /api/v1/sessions        → list of session summaries
   GET  /api/v1/sessions/<id>   → single session metadata
   GET  /api/v1/sessions/<id>/messages?limit=N&offset=M  → paged messages
   GET  /api/v1/messages/<id>/image → resolve single image, return base64 or redirect
   GET  /api/v1/search?q=...    → search results
   ```
3. 鉴权：Bearer Token（从环境变量读 `WECOM_READER_TOKEN`）
4. 复用现有 `reader.py`，**不重复实现任何业务逻辑**
5. OpenAPI 3.1 描述文件附在 `wecom_reader/web_api_schema.json`

**怎么验收**：
- [ ] `/api/v1/health` 返回 200，body 含 wal_present 数组
- [ ] `/api/v1/sessions/<id>/messages?limit=50` 与 `python -m wecom_reader.cli messages` 返回**sequence 集合完全一致**（用 `R:2910032769` 对比）
- [ ] 无 token 时返回 401
- [ ] curl 测试脚本：`tests/api_smoke.sh`（或 `.ps1`）跨平台 5 条用例全过
- [ ] OpenAPI 描述完整（端点、参数、返回类型、错误码）

**关联 PR**：`feat/http-facade`，base main，**不依赖 T1 进展**（wal_present 已有 reader 字段）

**工时估算**：3–4 小时

---

### T3 — Windows 多壳入口（P1，挂车）

**为什么 P1 挂车**：低风险低收益，但用户 100% Windows → 留 entry barrier 给后续接手的人。**任何 P0 PR 顺带加即可**，不要单独开 PR 占 review 资源。

**做什么**：
1. `wecom-reader/wecom-reader.bat`（ASCII 编码，避免路径乱码）：
   ```bat
   @echo off
   if "%WECOM_READER_VENV%"=="" (
       "%~dp0.venv\Scripts\python.exe" -m wecom_reader.cli %*
   ) else (
       "%WECOM_READER_VENV%\Scripts\python.exe" -m wecom_reader.cli %*
   )
   ```
2. `wecom-reader/wecom-reader.ps1`（PowerShell 版）：
   - 优先用 `pythonw.exe` 避免弹窗
   - 路径用 `[System.IO.Path]` API 而非字符串拼接（避免 \ vs / 错乱）
3. README 加一段"Windows 用户可直接双击运行"说明
4. `.gitignore` 不动（壳入口必须 commit）

**怎么验收**：
- [ ] 在 `wecom-reader/` 目录双击 `.bat` 能跑 `python -m wecom_reader.cli --help`
- [ ] 在 Git Bash 里执行 `./wecom-reader.bat` 输出与 `python -m wecom_reader.cli --help` 一致
- [ ] 路径含空格的 directory 测试（如 `C:\Program Files\Projects\wecom-reader`）

**关联**：合进 T2 或 T4 任一 PR，不要单开

**工时估算**：30 分钟

---

### T4 — image_resolver 单元测试（P1，Codex 派发）

**为什么是 Codex 候选**：816 行代码无单测是已知技术债。代码结构简单（70% 是 SQLite 查询 + 路径处理），适合 Codex App 派发。**user 自己持有 Codex 接入**，我不动手但要把 prompt 备好。

**候选 prompt 模板**：见 `codex-prompts.md`（待扩展）

**验收（Codex PR 合入前我做的）**：
- [ ] `tests/test_image_resolver.py` 覆盖 ≥ 80%（行 + 分支）
- [ ] 含真实数据回归（`R:2910032769` 一个 image 消息解析往返）
- [ ] mock fixture 包含：file.db / CacheMapping.db schema / `Cache/Image/` 子目录树
- [ ] 不动 `image_resolver.py` 现有 public API

**手写 fallback（Codex 不可用时我做的）**：8 个 test case，约 200 行

**工时估算**：1–2 小时

---

### T5 — info-pipeline 分支清理 + docs PR（P2，清理债）

**做什么**：
1. 删 `info-pipeline` 的 `main2` + `feat/initial-release` 分支（保留 `main`）
2. `__init__.py` 改动 + `examples/` 目录补 3 个 demo：`basic_pipeline.py` / `agent_with_memory.py` / `hook_usage.py`
3. AGENTS.md（仿 wecom-reader 格式）

**验收**：
- [ ] `git branch -a | grep info-pipeline` 只剩 main
- [ ] `examples/` 三个 demo 都能 `uv run python examples/X.py` 跑通
- [ ] AGENTS.md 含 5 条红线 + 接手必答 checklist

**工时估算**：2–3 小时

---

### T6 — Resolver 工厂化（P2，**阻塞 T4**）

**为什么阻塞**：先把 T4 跑通（标准测试集建好），再做工厂化。否则**测试标准会随架构变化，导致 T4 测试集要返工**。

**启动条件（须满足 ALL）**：
- [ ] T4 完成且合并
- [ ] 至少出现 1 个新的 content_type 真实需求（如用户要解析文件/语音/位置/名片）
- [ ] 至少 1 个非 image 的 content_type 已验证数据存在

**否则就挂起，不要先开 PR**。

**预留设计**（如未来启动）：
```python
# wecom_reader/resolvers/__init__.py
REGISTRY = {
    4: ImageResolver,
    # 5: VoiceResolver,   # 等用户催
    # 6: FileResolver,
    # 7: LocationResolver,
    # 8: ContactResolver,
}

def get_resolver(content_type: int) -> Resolver | None:
    return REGISTRY.get(content_type)
```

---

### T7 — `order=asc/desc` 参数（P3，**deferred**）

**挂起理由**：当前所有消费场景都是 agent/API 调用，全部需要 DESC（最新优先）。**没有"人类会话时间线浏览"的需求 = 没人会调 asc = 死代码**。

**重启条件（满足任一）**：
- 用户提"我要看历史聊天按时间顺序"
- 接入了 watch 流式场景
- 启用了 GUI 浏览器（人用界面）

**否则不要加**。

---

## 三、本月决策记录（不许漂移）

### D1 — seatalk 借鉴 4 项中 2 项挂起
- ✅ 借鉴：HTTP facade（T2） + Windows 多壳入口（T3）
- ❌ 不借鉴：Resolver 工厂化（转 T6 阻塞启动） + order 参数（转 T7 deferred）
- **理由**：借鉴落地的唯一判据是"真实用户痛点 + 不挤压主线消化"

### D2 — WAL 研究 T1 不挂车，独立 PR
- WAL 是技术债，不是 feature
- 必须能跑通完整可读化才合并
- 不与 T2 / T4 合并 PR（避免 review 复杂度）

### D3 — Codex 接入由 user 自己接
- T4 是 Codex 候选 task，但**接入、派发、review Codex 的 PR 由 user 持有**
- 我能做的：写好 `codex-prompts.md` 模板、备好 prompt、等 user 触发

### D4 — image_resolver 必须先补完单测，再扩面（T4 → T6 顺序硬约束）
- 防止"工厂化 + 新加 resolver"挤占测试基线
- 这条**写进 MEMORY.md 长期持久**

---

## 四、月底验收（7/31 检查项）

- [ ] T1 完成度：WAL 解密可用，wal_warning 移除
- [ ] T2 完成度：HTTP facade 上线，有跨平台 smoke test
- [ ] T3 完成度：双击 .bat 能跑，README 有说明
- [ ] T4 完成度：image_resolver 覆盖率 ≥ 80%，真实数据回归通过
- [ ] T5 完成度：info-pipeline 分支干净，3 个 examples demo
- [ ] T6 / T7：满足启动条件前**不动**

---

## 五、关联引用

- 决策来源：`.workbuddy/memory/2026-07-03.md`（借鉴分析）
- 历史债：`.workbuddy/memory/2026-06-26.md`（UNION ALL 修复 + WAL 待研究）
- PR pipeline：`.workbuddy/memory/2026-07-01.md`（已推 5 个 PR）
- 实时性测试蓝本：`docs/realtime-test-plan.md`（如需验证 T2 端到端）
- Codex 接入：`docs/codex-prompts.md`（T4 prompt 来源）
- 测试基线：`wecom_reader/db/message.py` 100% 覆盖（42 tests）作为本月的下限
