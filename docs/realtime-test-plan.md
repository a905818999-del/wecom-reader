# 企业微信实时性测试方案

> **目标**：验证 `wecom_reader` 从"消息发出去"到"reader.get_messages() 看见"的端到端延迟。
> **前置**：企业微信连接器已连接，可以主动向你发消息。
> **关键约束**：测试必须用**自毁消息**（自收发 + 自动删除），避免污染真实会话数据。

---

## 架构

```
┌─────────────────┐                       ┌──────────────────┐
│  WorkBuddy 对话  │                       │  你（zhen）的     │
│  (本 session)   │                       │  真实企业微信      │
│                 │                       │                  │
│  MCP wecom 工具  │ ──发"ping-abc123"──→ │  收到，user 看到  │
│  ↓               │                       │                  │
│  bash 脚本       │                       │                  │
│  ↓ loop          │                       │                  │
│  reader.get_messages                      │                  │
│  找"ping-abc123" │ ←── Webhook ──────── │  你可以手动转发   │
│  ↓ 计算 delta_t  │                       │  (可选)           │
└─────────────────┘                       └──────────────────┘
```

---

## 三种测试模式（按自动化程度）

### 模式 A：纯人工观察（最简单，5 分钟）

适合：日常回归，**不需要**精确数据

```
1. 我通过 wecom MCP 给你发一条消息："ping-test-{时间戳}"
2. 你看看几秒后收到
3. 你回一条："收到，N 秒"
4. 我这边打开 wecom-reader，reader.init() 后调 get_messages()
   看能不能查到这条（含 "ping-test-" 的最新一条）
```

**优点**：零脚本，1 分钟能跑
**缺点**：user 体验主观，无法量化
**适用**：人手少的快速回归

---

### 模式 B：半自动（推荐，精度到秒）

适合：定期回归，**有量化指标**

**思路**：我**自己**用 wecom-reader 的 MCP 工具发消息，自己用 reader 查自己，全程不需要你手动配合。

**问题**：wecom-reader MCP 发出去的消息是「测试账号 → 你」，**仍要你确认收到**才能证明链路通了。
**解决**：用 **bot 账号**给自己发消息（self-message），完全自包含。

**步骤**：

```python
# tests/realtime/test_self_echo.py
import time
import uuid
import subprocess
from wecom_reader import WeComReader

# 1. 准备 reader（指向你本机的解密数据）
reader = WeComReader(
    db_dir=r"E:\WXWork\1688851235369380\Data",
    decrypted_dir="wxwork_decrypted",
)
reader.init()

# 2. 生成唯一 ping token（防止误匹配历史）
ping_token = f"ping-{uuid.uuid4().hex[:8]}"
self_session = "R:<你跟自己的会话ID>"

# 3. 通过企业微信 MCP 发消息（self-message）
# 注意：这是测试场景，wecom-reader 库本身不支持发消息，
# 需要走 wecom connector / 或者直接 CLI / 或者 WeCom UI
import subprocess
subprocess.run(["powershell", "-Command",
    f'Send-WXWorkMessage -To "self" -Content "{ping_token}"'
])

# 4. 启动 polling loop
start = time.monotonic()
deadline = start + 60.0  # 60s 截止
found = None

while time.monotonic() < deadline:
    msgs = reader.get_messages(self_session, limit=20)
    for m in msgs:
        if ping_token in (m.get("content") or ""):
            found = m
            break
    if found:
        break
    time.sleep(1.0)  # 1s polling interval

latency = time.monotonic() - start

# 5. 报告
if found:
    print(f"OK  latency={latency:.2f}s  msg_seq={found['sequence']}")
else:
    print(f"TIMEOUT after 60s")
```

**实测指标**：
- L1（消息从发送到 reader 可见）：30s 内 = OK，>60s = 警告，>120s = 失败

**适用**：每周跑一次，写入 log

---

### 模式 C：完整 CI 集成（最硬核）

适合：要做 **持续监控 / SLO 仪表盘**

**架构**：

```yaml
# .github/workflows/realtime-monitor.yml
name: Realtime Latency Monitor

on:
  schedule:
    - cron: '*/15 * * * *'  # 每 15 分钟
  workflow_dispatch:

jobs:
  measure:
    runs-on: self-hosted  # 需要一台常驻 Windows + 已登录的 WeCom
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.13'
      - run: pip install -e .
      - run: python tests/realtime/monitor.py
        env:
          WECOM_BOT_KEY: ${{ secrets.WECOM_BOT_KEY }}  # 群机器人 webhook

      - name: Report to dashboard
        run: |
          curl -X POST "$DASHBOARD_URL/api/ingest" \
            -H "Content-Type: application/json" \
            -d @latency.json
```

**问题**：
1. **GitHub Actions 没有 Windows + 登录的 WeCom** → 需要 self-hosted runner
2. **真实数据禁令**：CI 跑的 reader 不能解密你**真实**数据 → 需要**隔离测试账号**
3. **消息发不出** → 需要 bot 群 webhook 或者测试账号

**适用**：你已经买了企业微信 SaaS / 有专门测试账号

---

## 推荐路线：先模式 A → 模式 B

```
今天  ──→  模式 A：5 分钟跑一次，人工看延迟
                ↓ 数据够多了
下周二  ──→  模式 B：脚本化，写入 .workbuddy/memory/2026-07-0X.md
                ↓ 跑了 1 周，基线稳定
月底  ──→  模式 C：上 GH Actions / 加 SLO 仪表盘
```

---

## 现在马上能做的（5 分钟）

**模式 A，零代码**：

1. 我用 wecom connector 给你发："ping-test-{当前时间戳}"
2. 你看几秒后收到，告诉我"收到"
3. 我在自己这边打开 wecom-reader，验证消息能不能查到
4. 三件事一次性跑完

**或者更直接**：你说"发"，我立刻发，秒表开始；你说"我看到了"，秒表停。**这就是 L1 latency 的真值**。

---

## 注意事项

1. **避免真实数据污染**：
   - 测试消息用 `ping-{uuid}` 前缀，自包含可识别
   - 不要在测试会话里发日常内容（污染你和别人真实聊天）
   - 测试完**立即撤回**消息（wecom MCP 提供 recall API）

2. **WAL 时序假设**：
   - 测的是「reader 能多快看见新消息」
   - 当前 bug：**只能等下一次 init()** 才看见（PR #2 不解决这个）
   - 真正解决要靠 PR #7（WAL 实时合并）merge 之后

3. **测出来的延迟分解**：
   ```
   L_total = L_wecom_server + L_local_db_write + L_wal_flush + L_reader_poll
   ```
   - L_wecom_server：~1-3s（你看到通知的速度）
   - L_local_db_write：~1-5s（WeCom 写本地 db 的频率）
   - L_wal_flush：~10s-WAL 满了才合并（这是 PR #2/7 想消灭的延迟）
   - L_reader_poll：1s（脚本 sleep 间隔）

   **预期**：没修 WAL bug 前，L_total ≈ 60-600s
   **修了之后**：L_total ≈ 3-10s

---

## 要走哪条路？

- **A**：我现在发 ping-test，你告诉我"收到"
- **B**：我写 `tests/realtime/test_self_echo.py`，跑一次基线
- **C**：你已经有 self-hosted runner + 测试账号，我直接上 CI

你说 A 我就发，你说 B 我写脚本，你说 C 我先列任务清单。