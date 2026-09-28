# M5 Known Gaps（未通过 / 未覆盖项）

> 基线：`master@b0853d88a02d29feb8d20ad4e2736fe373b16679`。
> 记录时点：第 1 阶段（离线）与第 2 阶段（一次真实 DeepSeek + 一次真实微信草稿）之后。
> 状态图例：**RESOLVED**=本次实证已覆盖；**PARTIAL**=部分覆盖；**OPEN**=仍未覆盖/未通过。

## A. 真实调用结果

1. **真实 DeepSeek 请求 — RESOLVED**：一次请求成功（EXECUTED/REAL，无降级，HTTP 200，内容非空），并记录了服务商签发的请求 ID `00315f61-ba01-498c-b26b-a157032d7659`（`kit.external`，source=external）。
2. **真实微信草稿 — OPEN（本次失败）**：调用发生但被微信拒绝，错误 `40164 invalid ip 117.185.153.57 ... not in whitelist`（token 获取成功，`draft/add` 出口 IP 未在白名单）。**未创建任何草稿**，隔离 `done.jsonl` 不存在，无 `media_id`。本轮不重试。
   - 修复方向（需新一次批准）：把当前出口 IP 加入微信 IP 白名单或固定出口 IP，然后用**新的 run id**再批准一次；绝不能靠重试掩盖。
3. **provider-issued 请求 ID — PARTIAL**：DeepSeek 取得响应体 `id` 并已记录；微信侧因草稿失败，只有失败调用的 rid `6aba6a3f-09f32081-0d8bcf70`（已记录为失败凭据，**未**通过 `kit.external`）。未合成任何 ID。

## B. 结构性限制

4. **微信永久封面 — OPEN（未验证）**：本次在通过封面校验前就被 API 的 IP 白名单拒绝，永久封面 media ID 是否有效**未验证**。若后续仍因封面失败，记录为失败分类，不伪造 `done` 或 `media_id`；已删除的 `assets/placeholder.jpg` 不恢复。
5. **图片与子进程路径 — OPEN**：隔离适配器直接调用 `WeChatPublisher` 的解析与 `create_draft()`，跳过动态封面、内联图片上传、正常 `wechat_publish.py` 子进程与 `publish_worker._publish_one` 的生产调用路径。本验收**不证明**生产图片生成与子进程完整链路。
6. **不是真实 queue 端到端 — OPEN**：微信验证全程使用隔离队列（`.verifykit/data/m5/<run-id>/wechat/`），证明草稿调用与 worker 状态迁移，但不等价于生产 `queue/pending.jsonl` 上的真实消费。真实队列 11 条记录未动。
7. **连续真实排程 — OPEN**：本轮只做文档与现状观察，未加载、启用或修改任何定时任务；05:00、06:00、10:00 与 worker 均无 content-hub 生产定时任务。在连续运行记录齐全前，**不宣称 M5 全部通过**，也不宣称线上恢复。
8. **外部告警渠道 — OPEN**：飞书/邮件等外部告警的接收人与凭据仍未提供（沿用 M3 结论）。隔离 health 显示 `publish-worker` 连续失败 1 次，但无外部投递。

## C. 关卡与策略状态（刻意保持）

9. **VerifyKit acceptance 仍 `approved:false` — 保持**：受保护的 acceptance 与 ground truth 未人工批准，Golden Path 预期 FAIL；本里程碑未修改 `approved`。
10. **受保护的 fallback 列表不变 — 保持**：`wechat.draft_live` 失败经 `kit.degrade(reason)` 记录为 unexpected fallback（`m5.wechat.publish_failed`），未自行标为 `expected`，未加入 `allowed_fallbacks`。

## D. 证据可读性限制

11. **DeepSeek 细节数值未落盘 — OPEN**：请求/响应模型 ID（响应侧）、认证分类、脚本请求级耗时、实际 usage 数字只存在于脚本 stdout，VerifyKit level-1 摘要仅保存 schema/大小/哈希，事后不可恢复。不推算费用。下一次改进：让适配器把 redacted 结果单独写入忽略目录工件。
12. **一次性预算已生效 — RESOLVED**：`deepseek/request.attempted` 与 `wechat/draft.attempted` 均存在，证明每个 probe 至多一次；失败后未重试、未重跑队列。
