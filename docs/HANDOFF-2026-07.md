# wecom-reader / info-pipeline 交接文档（2026-07）

> 目标：让 Codex 接盘后无需追问背景即可继续。本文件是进度总览，细节见同目录
> `roadmap-2026-07.md`（月计划）、`realtime-test-plan.md`（实时性测试）、
> `codex-prompts.md`（Codex 起步提示词）。AGENTS.md 已在仓库根声明红线。

## 0. 仓库地图

| 仓库 | remote | 默认/当前分支 | 状态 |
|---|---|---|---|
| **wecom-reader** | `a905818999-del/wecom-reader` | 默认 `feat/initial-release`；本机当前 `fix/8-web-image-mention`（已推） | 活跃主项目 |
| **info-pipeline** | `a905818999-del/info-pipeline` | `main`（已推，含 smoke 测试） | 完成初版，待扩展 |
| **wechat-decrypt** | `ylytdeng/wechat-decrypt`（**第三方**） | `main` | 只读参考，请勿推送 |

> 根目录另有 `weflow-asar/`（WeFlow 拆包产物，381M，逆向参考）、
> `wxwork_decrypted/`（1.2G 解密聊天库，**敏感，绝不提交**）、`info-pipeline-data/`
> （staging .db，数据）。这些都不进 git。

## 1. wecom-reader 进度

### 已落地
- **多表分页 bug 修复（核心）**：`db/message.py` 的 `get_messages` / `search_messages`
  旧实现 3 张表各自 `LIMIT/OFFSET` 再二次截断，`offset>0` 时全局位置错乱、丢消息。
  已改为单条 `UNION ALL` + 最外层 `LIMIT/OFFSET`。`db/message.py` 覆盖 100%。
- **image_resolver**：`file.db(server_id)` → `CacheMapping(key→file_name)` → `Cache/Image/` 实际文件，
  精确匹配率 ~80%。CLI：`image stats / resolve <msg_id> / export <conv_id>`。
- **Web UI（Flask）**：`/api/image/<id>` 安全渲染（防目录穿越）、消息 JSON API、HTML 渲染。
- **质量门禁**：CI（pytest + ruff + coverage）、AGENTS.md、issue/PR 模板已就位。
- **测试**：`tests/` 共 32 passed（image_resolver / message_mentions / web_image /
  integration_session 全部 self-contained，用 tmp_path 合成数据，不依赖真库）。

### 已知未解（P0）
- **WAL 加密格式未研究（T1）**：`reader.init()` 跳过 `-wal/-shm`，未 checkpoint 的最新事务
  **永久丢失**。加密格式与 main db 不兼容；WAL 文件头 magic 固定 `0x377f0682`（非 SQLite 标准）。
  当前 `init()` 会输出 `wal_present` 列表 + `wal_warning` 提示，但无修复。
- **HTTP facade（T2）**：`/api/v1/*`（含 `/health` 暴露 `wal_present`）、
  `get_messages(include_wal=...)` 未做（与 WAL 研究交集，见 `roadmap-2026-07.md`）。

### 分支
`feat/image-resolver`、`fix/multi-table-pagination`、`fix/8-web-image-mention`（当前）、
`feat/initial-release`、`chore/*`。**提交走特性分支，全局 pre-commit 禁止直推 main/master。**

## 2. info-pipeline 进度
- 智能信息处理系统：状态机 `raw→queued→processing→processed→verified/failed`，
  `MiniMaxAgent`（MiniMax-M3，OpenAI 兼容端点）、`MemoryStore` 短期/长期记忆。
- 初版完成，已补 `tests/test_smoke.py`（4 passed，MockAgent 离线端到端），ruff 全库清理（27→0）。
- 注意：本仓库**此前无测试**，钩子会因 pytest 收 0 测试而 block——提交前务必保留测试。

## 3. Codex 行动清单（按优先级，详见 roadmap）
1. **T1 WAL 研究（P0）**——先搞清 `0x377f0682` magic 的页结构，才能做 T2。
2. **T2 HTTP facade（P0）**——`/api/v1/*` + `include_wal`，建议独立 PR，不动 `reader.py` public API。
3. **T4 image_resolver 单测（P1，Codex 派发）**——**必须早于 T6**。
4. **T6 Resolver 工厂化（P2，被 T4 阻塞）**；T3 Windows 多壳入口（P1，挂车）；T5 info-pipeline 分支清理 + docs PR（P2）。
5. **红线**：绝不提交 `.db` / 真实聊天数据 / `.workbuddy` / secret；借鉴项先过"5 问"评估。

## 4. 如何在本地跑（Codex 起步）
```bash
# 安装（Windows + uv / pip）
pip install -e ".[dev]"          # wecom-reader
pytest                           # 应 32 passed
ruff check .                     # 应全过

# 解密库放 wxwork_decrypted/（已被 gitignore），不进 git
python -m wecom_reader.cli <command>
```
注意：全局 pre-commit（`~/.git-hooks/pre-commit`）要求 **ruff + pytest 通过** 且 **代码改动必须跑出测试**，
否则 commit 被 block。本机环境需保证 `ruff` 与可用的 `pytest` 都在 PATH 上。
