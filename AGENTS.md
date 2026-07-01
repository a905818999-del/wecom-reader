# AGENTS.md

> Codex / 其他 AI Agent 接手 wecom-reader 必读的项目说明。
> 接手前先读完整个文件，再开始任何改动。

## 项目是什么

`wecom-reader` 是企业微信（WeCom）本地聊天记录读取工具。

**数据流**：Windows 上挂着企微进程 → 用 ctypes `ReadProcessMemory` 抓 16 字节全局密钥 → AES-128-CBC 解密 `message.db` / `session.db` 等 20 个 db → CLI 查询消息/会话/图片。

**当前状态**：
- PR #1 ✅ merged：初始 release（reader + db 适配器 + crypto + 4 层 verify 框架）
- PR #2 🟡 open：fix 多表分页消息丢失（`UNION ALL` 修复，54 tests，100% 覆盖 `db/message.py`）
- PR #3 🟡 open：feat 图片解析（`image_resolver.py` 816 行，**暂无单元测试**）

## 核心约束（**红线**）

### 1. 加密代码不可擅改
- 路径：`wecom_reader/crypto/`（`decrypt.py`, `wx_key.py`, `reader_key.py`）
- 任何改动 → 提 PR 时必须 @zhen-qian 复核，否则不 merge
- 单元测试可以加，但不动现有实现
- **理由**：密钥提取依赖 Windows 进程内存结构（ctypes ReadProcessMemory），改 1 字节就破

### 2. 绝不允许 commit 真实数据
项目根 `.gitignore` 已覆盖以下路径，**不要再开新口子**：
- `wxwork_decrypted/`（已解密的真实 db，2GB+）
- `Cache/Image/`（真实聊天图片）
- `CacheMapping/`（图片索引）
- `.workbuddy/`（WorkBuddy 会话数据）
- 任何 `.db` / `.db-wal` / `.db-shm` / `*.db`
- `api_at_*.txt` / `parse_output.txt`（调试残留）
- `.coverage` / `htmlcov/` / `.pytest_cache/`

### 3. 不改公开 API 签名
- `reader.get_messages()` / `search_messages()` / `get_sessions()`
- 返回类型稳定（`list[dict]` / `dict` / `int`）
- 改签名 = breaking change → 需要 major version bump + migration guide

### 4. 测试要求
- 新代码单测覆盖率 **>= 80%**
- 改 bug 必须配 regression test
- 真实数据回归脚本（如 `tests/smoke_*.py`）可以保留，但 fixture 不能 commit
- pytest config 在 `pyproject.toml`

## 工作流程

```
1. 读     AGENTS.md（这个文件）+ wecom_reader/db/message.py（看现有多表查询模式）
2. 分支   feat/xxx 或 fix/xxx，base 永远是 main
3. 开发
   - 单元测试先行（TDD）
   - pytest tests/ -v --cov=wecom_reader.xxx --cov-report=term-missing
   - 真实数据回归用 tests/smoke_*.py（不 commit fixture）
4. PR
   - Title 写清楚做什么
   - Body 写：测试结果 + 覆盖率报告 + 已知限制 + 相关 issue
   - 不自动 merge，等 zhen-qian 复核
```

## 真实数据基准（**不写入 PR**）

- 测试会话：`R:2910032769`（61,343 main + 408 small + 0 kf messages）
- 真实 db 路径（仅本地）：`E:\WXWork\1688851235369380\Data\`
- 解密输出路径（gitignored）：`wxwork_decrypted/`

## 关键文件

| 路径 | 用途 | 改前问 |
|---|---|---|
| `wecom_reader/reader.py` | 高层 facade | API 签名稳定 |
| `wecom_reader/db/message.py` | message 适配器 | 已用 UNION ALL，**禁走多表分页** |
| `wecom_reader/db/session.py` | 会话适配器 | — |
| `wecom_reader/crypto/` | 加密 + 密钥提取 | **禁改**（红线 1） |
| `wecom_reader/image_resolver.py` | 图片解析 | **当前无单测**（TODO） |
| `tests/` | pytest 测试 | — |

## 编码风格

- Python 3.13，类型注解要全（mypy strict）
- 错误码走契约式枚举（`ErrorCode`），不抛裸 `Exception`
- 不可达代码用 `# pragma: no cover`，**不硬造测试**
- 5 层 verify：static / units / imports / integration-mock / e2e
- 公开函数必须有 docstring（中文 OK，混合英文术语）

## PR 模板（**所有 AI 提的 PR 必填**）

```markdown
## 改动
- [一句话]

## 验证
- [ ] pytest tests/ -v 全绿
- [ ] 新代码覆盖率 >= 80%（粘 --cov 报告）
- [ ] 真实数据复现脚本（如果适用）

## 已知限制
- [列出]

## 相关
- 关联 issue / PR #
- 是否依赖未合并的 PR（如 #2 / #3）

## 复核请求
- [ ] @zhen-qian 复核（加密代码改动时强制）
```

## 沟通

- 卡住 > 30 分钟 → 在 issue / PR 下评论 @zhen-qian
- 超出当前 PR 范围的发现 → **单独开 issue**，不在 PR 里混改
- 不确定的事 → 评论问，不擅自决定
- 怀疑现有 bug → 写最小 reproduction → 开 issue，**不要在 PR 里 "顺手修"**

## 常见陷阱（**先看这个再动手**）

1. **多表查询**：永远用 `UNION ALL` + 外层 `LIMIT/OFFSET`，不要每表独立查再合并（PR #2 修复的就是这个 bug）
2. **WAL 文件**：`init()` 显式跳过 `*.db-wal` / `*.db-shm`，未 checkpoint 的事务丢失；reader 暴露 `wal_present` 字段
3. **decrypt helper**：`decrypt_wal_pages` / `decrypt_wal_file` 存在但**默认不调用**（WAL 加密格式未研究清楚）
4. **测试运行**：项目用 venv 隔离，跑测试用 `.venv/Scripts/python.exe -m pytest`，别用系统 Python
5. **pre-commit hook**：global hook 跑 `uv run ruff/pytest` 失败（项目没装 dev deps），用 `--no-verify` 跳过

## 接手任务前必答

- [ ] 我读了 AGENTS.md 全部
- [ ] 我知道加密代码在哪个路径、为什么不能改
- [ ] 我知道真实数据在哪个路径、为什么不能 commit
- [ ] 我知道公开 API 在哪个文件
- [ ] 我知道测试怎么跑、覆盖要求是什么
- [ ] 我知道 PR 模板必填项

答不出来 → 评论问，不要猜。
