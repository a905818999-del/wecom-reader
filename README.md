# wecom-reader

企业微信（WeCom）本地聊天记录读取工具。从 WXWork.exe 进程内存提取密钥，解密本地 SQLite 数据库，提供 CLI / Python 库 / Web UI 三种访问方式。

## 原理

企业微信使用 wxSQLite3 AES-128-CBC 加密本地数据库：

- 密钥长度：16 字节（128 bit）
- 密钥数量：**一个全局密钥**解密所有数据库（比个人微信简单得多）
- 加密方式：每页独立派生 key/IV，无 HMAC，无 PBKDF2
- 密钥提取：通过 Windows API 读取 WXWork.exe 进程内存

## 安装

```bash
pip install pycryptodomex click flask
```

## 使用

### CLI

```bash
# 提取密钥 + 解密所有数据库（需要管理员权限，WXWork.exe 运行中）
python -m wecom_reader.cli init --db-dir "E:\WXWork\<account_id>\Data"

# 查看状态
python -m wecom_reader.cli status

# 列出会话
python -m wecom_reader.cli sessions --limit 20

# 查看消息
python -m wecom_reader.cli messages R:12345 --limit 50

# 搜索消息
python -m wecom_reader.cli search "关键词"

# 查看联系人
python -m wecom_reader.cli contacts --keyword "张三"
```

### 脱敏审计导出

先运行 `init` 生成稳定的解密快照，再把全部消息导出为不含正文、姓名和本地路径的
JSONL。账号、会话、消息、发送者、正文和资源引用均使用确定性的 SHA-256 哈希。

```bash
python -m wecom_reader.cli \
  --db-dir "E:\WXWork\<account_id>\Data" \
  export-audit --output output/reader-export.jsonl
```

导出采用同目录临时文件并在成功后原子替换目标；读取失败、空导出或写入失败会返回
非零退出码，已有的成功文件不会被半截结果覆盖。`init` 会生成不含原始路径或账号的
来源清单，导出前会核对账号、来源目录和解密 `message.db` 指纹，防止账号与快照错配。
JSONL 行顺序固定为消息表顺序和各表 `rowid` 顺序；下游应使用稳定键与 `sequence`
判断消息身份和时间顺序，不应依赖文件行号。

MessageRecord v1 审计合同见 `docs/audit-contract-v1.md`。Reader 的
`tests/fixtures/audit_contract_v1.jsonl` 是 WeCom2 权威 fixture 的字节级副本；
CI 会校验 producer conformance、fixture SHA-256、source/parse_status 白名单和
fixture 隐私扫描。

### Python 库

```python
from wecom_reader import WeComReader

reader = WeComReader(db_dir="E:/WXWork/<account_id>/Data")
reader.init()  # 提取密钥 + 解密

sessions = reader.list_sessions(limit=10)
msgs = reader.get_messages("R:12345", limit=50)
results = reader.search_messages("关键词")
contacts = reader.contacts(keyword="张三")
```

### Web UI

```bash
python -m wecom_reader.web --db-dir "E:\WXWork\<account_id>\Data"
# 浏览器打开 http://127.0.0.1:8765
```

## 数据库结构

| 数据库 | 内容 | 大小 |
|--------|------|------|
| message.db | 聊天消息（130万+条） | ~770MB |
| session.db | 会话列表（5800+个） | ~22MB |
| user.db | 联系人/用户信息 | ~8MB |
| file.db | 文件传输记录 | ~158MB |
| calendar_r7.db | 日程信息 | ~2.5MB |
| crm.db | CRM 数据 | ~424KB |

## 会话 ID 格式

| 前缀 | 类型 |
|------|------|
| `R:<数字>` | 群聊 |
| `S:<数字>_<数字>` | 单聊 |
| `M:<数字>` | 微信联系人 |
| `O:<数字>` | 应用/公众号 |
| `Y:<数字>` | 系统会话 |

## 依赖

- Python >= 3.10
- pycryptodomex >= 3.20（AES 解密）
- click >= 8.0（CLI 框架）
- flask >= 3.0（Web UI，可选）

## 致谢

- [wechat-decrypt](https://github.com/ylytdeng/wechat-decrypt) — 企微解密参考实现
- [CipherTalk (密语)](https://github.com/ILoveBingLu/miyu) — WCDB 架构参考

## 免责声明

本工具仅供个人数据备份和学习研究使用。请遵守企业微信服务条款和当地法律法规。
