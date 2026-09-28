# WORKFLOW-ITERATIONS — content-hub 公众号文章链路迭代

Workflow: `codex-opencode-codex`（Orchestrator: Paseo Orchestrator profile；Planner: Codex GPT-6-Sol；Executor: OpenCode DeepSeek V4.1 Flash；Reviewer: Codex GPT-5.6-Terra）。

## Original task

实施 `docs/2026-09-28-wechat-article-pipeline-iteration-plan.md`。用户明确：修复全部问题（M0–M5）；允许连接真实外部服务（真实 DeepSeek、真实微信草稿）；允许提交（git commit）。

用户随后把**第 1 轮范围收窄为 M0 + M1**，M2–M5 留待第 1 轮验收通过后另行执行。第 2 轮（拆分执行）为 M2–M4，第 3 轮为 M5。

## 环境事实（本机验证）

- `cron` 无法写入 `~/Documents`（macOS TCC 拦截，实测：写主目录成功、写项目 logs 失败）。排程必须用 **launchd**。
- 已安装并验证自检用 LaunchAgent：`com.contenthub.schedulertest`（每 600s 写 `logs/scheduler_test.log`）。
- 微信凭据已写入 `.env`（600，gitignored）；真实 `cgi-bin/token` 调用成功，IP `211.95.99.82` 已加白。
- `queue/pending.jsonl` 原有 **11 条**（2026-08-22/23）历史记录，必须保持不动。

## 里程碑与提交

| 提交 | 内容 |
| --- | --- |
| `bd44147` | 基线存档：归档迭代前的既有未提交改动（17 文件）。不含 `assets/placeholder.jpg` 删除、缓存、`.env` |
| `f1c44f8` | M0：离线基线、漏斗盘点、VerifyKit Stage 0、测试隔离修复 |
| `87143c2` | M1：终稿验收门与幂等提交时序 |

`assets/placeholder.jpg` 删除保持未提交（`.gitignore` 标注该文件为"committed on purpose"），待人工确认。

## Round 1 — Executor (M0+M1)

Executor 报告要点（详见其最终报告）：

- **M0**：新增 `content_platform/audit/funnel.py` 与 `docs/2026-09-28-baseline-funnel.md`（采集→评分→候选→选中→合格→入队→草稿漏斗，含原始证据路径与显式 `unknown`，并分类 material_shortage / model_failure / quality_rejection / enqueue_failure / wechat_failure）；新增 `tests/conftest.py` 全局隔离；修复 8 个既有失败/联网测试；新增 `tests/platform/test_funnel.py`（5 条）；引入 VerifyKit（`.verifykit/`，acceptance 与 ground-truth 均 `approved:false`，Node ESM 桥接 Python）；重写 CI 模板为 Python；`requirements.txt` 增补 `Pillow`。
- **M0 缺陷与修复**：既有测试 `test_legacy_writer_isolation` 等会写入**真实** `queue/pending.jsonl`（11→19 条）及 `wechat-articles/`、`platform/state/`。修复方式为 `tests/conftest.py` autouse 把 live 路径重定向到 per-test 临时目录，并桩掉 LLM 标注/评分边界（未在生产代码加测试分支）。队列已恢复为原始 11 条（备份 `/tmp/content-hub-queue-pending-polluted-20260928.bak`）。
- **M1**：润色纳入写作流程（非零退出/超时/缺脚本/缺终稿 ⇒ 写作失败，保留草稿与 `polish_error.txt`）；质量与"正文内可定位来源"检查改在**终稿** `article.md` 上执行（仅元数据里有 URL 不再算通过）；`QUALITY_LLM_JUDGE=1` 为可选语义复核并设每日预算，启用但不可用 ⇒ 显式拒绝；`derive_article_id` 稳定 ID；`commit_final_article` 使主题/已用 URL/入队三写入幂等可重试并记录每步状态，未完成 ⇒ 退出码 3（绝不算成功）；`enqueue_for_publish` 跨 pending/processing/done/failed 按 ID 去重。
- **测试**：全套 `pytest -q tests` = 131 passed；计划指定的两条命令 = 37 / 90 passed；M1 四类离线场景 = 10 passed。
- **ruff**：计划指定文件 = 99 个**既有**错误（历史风格债），新增文件干净。
- **VerifyKit**：`check` exit 0；Golden Path `wechat-article-pipeline` Run ID `run_20260928090949_174ef423`，结果 FAIL（exit 1），各步 EXECUTED/REAL，mock 0，失败原因为 ground truth 未人工批准（符合"未批准不得变绿"要求）。
- **偏离计划**：新增 Pillow 依赖；修复既有测试；重写 CI 模板；2 处静默 `except: pass` 改为记录日志；写作退出码新增 3（提交未完成）。
- **范围变更记录**：Executor 在收到收窄指令前已实现 M2（提交 `c7f1c47`）与部分 M3（未提交），随后按要求 reset 到 M1 提交并丢弃，备份于 `/tmp/content-hub-m2-commit.patch` 与 `/tmp/content-hub-m3-uncommitted.patch`（第 2 轮可复用）。

## Round 1 — Reviewer（独立验收，read-only）

Reviewer 自行重跑证据：`pytest -q tests` = **131 passed**；`pytest -q tests/test_m1_final_draft.py` = **10 passed**；对 `bd44147` 归档树跑同一条 ruff 命令同为 **99 errors**（确认无新增 lint 债）；真实数据 md5 未变（`pending.jsonl` md5 `0900ce0ab0fed4ee2e003a797e760a1a`，材料缓存 md5 未变，未新建 `platform/state/` 或 `wechat-articles/` 目录）。VerifyKit：`check` PASS；`run wechat-article-pipeline` FAIL（Run ID `run_20260928091314_f2c0d5f2`），各步 EXECUTED/REAL，mock 0，unexpected fallback 0，失败原因为 ground truth 未人工批准。

判定通过：测试隔离有效；终稿通过后才提交；润色失败保留草稿；稳定 ID / 队列去重跨四态有效；无新增密钥。

**BLOCKING（必须在通过前修复）**

1. 漏斗日期发现不完整：`content_platform/audit/funnel.py:_discover_dates` 只扫描 `platform/jobs` 与 `platform/datasets`，未纳入队列（pending/processing/done/failed）等记录中的日期。当前 `queue/pending.jsonl` 有 3 条 `2026-08-23` 记录，但漏斗只报出 `2026-08-22`。
2. 失败分类错误：`funnel.py:_classify_article_failure` 只要 `enqueued and drafts_done == 0` 就返回 `wechat_failure`，把仍在 pending/processing 的记录也误判为微信失败；且与同处注释（"still-pending record is simply not yet published"）矛盾。微信失败必须要求 `failed.jsonl` 等真实失败证据。
3. 未跟踪文件 `WORKFLOW-ITERATIONS.md` 出现在 `git status`，导致工作区快照不只有允许的 `assets/placeholder.jpg` 删除。

**optional（本轮不实施）**：为"队列提交中断后重试恢复"补显式测试；为四种队列状态补参数化测试；把计划中提到的聚焦 pytest 命令写入文档以便复现。

**说明（非缺陷）**：Reviewer 指出计划文档未包含"37 / 90 passed"这两条聚焦命令，故无法从**文档**复现。这两条命令来自 Planner 的计划（§6），等价的完整证据是全量 `pytest -q tests` = 131 passed。两条命令已记录于本文件 Round 1 Executor 小节。

**方向确认**：Reviewer 指出 `tests/conftest.py` 的隔离、M1 提交门、稳定 ID 与队列去重均正确；VerifyKit 的 FAIL 属于"未人工批准"，不是代码缺陷。

## Round 1 — 修复轮（Executor）

提交 `74ca2f8`：`fix(M0): funnel date discovery and failure classification`（改 4 个文件）：
`content_platform/audit/funnel.py`、`tests/platform/test_funnel.py`、`docs/2026-09-28-baseline-funnel.md`（重新生成）、`WORKFLOW-ITERATIONS.md`（纳入版本管理）。

- 新增 `_record_date()`（读 `enqueued_at`/`finished_at`/`date`，校验 `YYYY-MM-DD`）；`_discover_dates` 现扫描 `platform/jobs`、`platform/datasets`、**四个队列文件**与文章/案例/旧版主题账本。仅存在于队列的日期以 `unknown` 上游环节输出。
- 新增 `_queue_state()`（优先级 failed > processing > pending > done），作为每运行 `queue_state` 字段；`wechat_failure` 现在**必须**有 `failed.jsonl` 证据；pending/processing 归为"未发布"。注释与行为一致。
- 更新原先断言错误行为的测试；新增 pending-only / processing-only / done（含 `media_id`）/ failed（含 `attempts`/`error`）覆盖；新增队列独有日期回归测试。
- 证据：`pytest -q tests` = **136 passed**；`ruff check content_platform/audit/funnel.py tests/platform/test_funnel.py` = clean；`queue/pending.jsonl` 前后 md5 均 `0900ce0ab0fed4ee2e003a797e760a1a`（仍 11 条）；`git status --short` 仅 ` D assets/placeholder.jpg`。

## Round 1 — Reviewer 第 2 轮（全新 Reviewer，修复复验）

**结论：PASS，零阻断问题。**

- 三项 Round-1 阻断判定均为 **FIXED**（附 file:line）。新漏斗测试非空断言（在旧实现上会失败）。
- 未发现 M2–M5 越界或密钥暴露；修复提交只动了声明的 4 个文件。
- 测试：`pytest -q tests` = 136 passed；`tests/platform/test_funnel.py` = 10 passed；ruff clean；重新生成的漏斗输出与已提交表格一致。
- 真实数据：全套测试前后 `queue/pending.jsonl` md5 均为 `0900ce0ab0fed4ee2e003a797e760a1a`（11 条），材料缓存 md5 未变，无 `platform/state/` 新文件。
- VerifyKit：`check` 静态策略 PASS；Golden Path Run #5 `run_20260928092440_ba4d7cef` = FAIL，**唯一原因**为 ground truth 仍 `approved=false`；3 个必需步全部 executed、0 skipped、全 REAL、mock 0、unexpected fallback 0。
- optional（未实施）：`_record_date()` 目前只校验年份与连字符位置，`2026-AB-CD` 这类畸形值可能被当作日期；当前真实数据不受影响，属防御性改进。

## Round 1 状态

M0 + M1 **通过独立复验，无阻断项**。仍需人工处理：VerifyKit acceptance/ground truth 的 `approved:true` 由人工决定；`assets/placeholder.jpg` 删除待人工确认；M2–M5 未实施（第 2、3 轮）。
