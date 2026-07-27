# MessageRecord v1 审计合同

WeCom2 是 MessageRecord v1 规范、validator 语义、canonical acceptance fixture
和正反例向量的唯一权威。合同变更顺序固定为：

1. WeCom2 规范、fixture 和正反例向量；
2. wecom-reader producer conformance；
3. 跨仓验证。

两仓 fixture 的 SHA-256 一致只证明副本字节一致，不能替代正反例测试。

## JSONL 格式

- UTF-8，每行一个 JSON object，无空行或 envelope。
- 只允许 LF；每行（包括最后一行）以一个 LF 结束。
- 使用 compact JSON：`ensure_ascii=False`、`sort_keys=False`、
  `separators=(",", ":")`。
- object 必须按下列顺序精确包含 14 个字段；缺失、未知或乱序字段均拒绝。

1. `account_hash`
2. `conversation_hash`
3. `message_id`
4. `sequence`
5. `timestamp`
6. `direction`
7. `sender_hash`
8. `conversation_type`
9. `message_type`
10. `status`
11. `content_hash`
12. `resource_refs`
13. `source`
14. `parse_status`

## 字段边界

- `account_hash`、`conversation_hash` 必填且为
  `sha256:<64 lowercase hex>`。
- `message_id`、`sender_hash`、`content_hash` 可为空；非空时使用相同完整
  SHA-256 格式。
- `sequence`、`timestamp` 为整数或 `null`，不接受布尔值。
- `resource_refs` 为数组，元素全部为完整 SHA-256。
- producer `source` 只允许 `db`、`wal`、`lookup`、`index`。
- producer `parse_status` 只允许 `OK`、`UNSUPPORTED`、`UNVERIFIABLE`、
  `ERROR`；`DRIFT` 是 verifier-only 状态。
- 绝对路径、原始 WeCom ID 和 secret-like 内容一律拒绝。

## Stable key 与重复

stable key 优先级为：

1. account + conversation + message_id；
2. account + conversation + sequence；
3. account + conversation + timestamp + direction + sender + message type +
   content hash。

`source_duplicate_count` 统计 reader export 内第二条及之后的相同 stable key，
不删除或折叠原始记录。`truth_duplicate_count` 是独立的 truth 匹配指标，两者不得
互相覆盖。

## 兼容

字段、hash 规则、stable-key 优先级、parse-status 语义或隐私规则的变更需要 v2。
文档澄清、测试加强和不改变语义的 validator 修复可保持 v1。
