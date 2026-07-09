# 微信公众号内容流水线 (WeChat Content Pipeline)

自动化内容采集 → 清洗聚合 → 选题 → 写作 → 配图 → 发布至微信公众号的完整流水线。

## 安装

先准备 Python 虚拟环境，然后安装运行时依赖：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 当前主链路

仓库已经切到新的文件制内容中台。当前推荐入口不是直接调用旧脚本，而是通过 `platform_cli.py`：

```bash
python3 platform_cli.py run collect-daily --date 2026-06-03
python3 platform_cli.py run article-daily --date 2026-06-03
python3 platform_cli.py run case-daily --date 2026-06-03
python3 platform_cli.py run cleanup --date 2026-06-03
```

其中：

- `collect-daily` 负责写 `platform/ingest`、`platform/normalize`、`platform/curate`、`platform/datasets`
- `article-daily` 从同日 `platform/datasets/article_pool.json` 读取候选，再调用旧 `write_article.py` 负责写作
- `case-daily` 从同日 `platform/datasets/case_pool.json` 读取候选，再调用旧 `decompose_case_study.py` 负责写作
- `cleanup` 清理 30 天前的中间产物

旧脚本仍在仓库里，但它们不再是推荐的主调度入口。

## 架构概览

```
cron 05:00 → run_all.sh              ← 唤醒 collect-daily + cleanup
cron 06:00 → run_daily_article.sh    ← 唤醒 article-daily + case-daily
```

## 平台目录

```text
platform/
├── ingest/raw/YYYY-MM-DD/        # 原始采集输入
├── normalize/YYYY-MM-DD/         # 规范化素材
├── curate/YYYY-MM-DD/            # 聚类、打分、过滤后的素材
├── datasets/YYYY-MM-DD/          # article_pool / case_pool / selection / materials bridge
├── jobs/YYYY-MM-DD/              # 每个 job 的 job.json、步骤状态、artifact 指针
└── state/                        # 长生命周期状态（后续扩展）
```

## 目录结构

```
collector/
├── run_all.sh                  # 总管线入口（cron 05:00）
├── run_daily_article.sh        # 每日文章生成管线入口（cron 06:00）
├── run_wechat_stats.sh         # 数据分析管线（手动触发）
│
├── collect.py                  # 统一CLI（可替代各collect_*.py独立调用）
├── collect_rss.py              # RSS源采集（Phase 1 part 1）
├── collect_blogs.py            # AI公司博客采集（Phase 1 part 2）
├── collect_consulting.py       # 咨询报告采集（Phase 1 part 3，仅周一）
├── collect_podcasts.py         # 播客节目采集（Phase 1 part 4，仅周一）
│
├── filter_items.py             # LLM过滤素材（Phase 2）
├── poll_and_save.py            # 轮询过滤结果并保存（Phase 3）
│
├── tag_materials.py            # 增量素材打标（Phase 0，打标7维度）
├── write_article.py            # 公众号文章写作（Phase 1）
├── embed_images.py             # IMAGE占位符替换（Phase 2 fallback）
├── generate_cover.py           # 文章封面图生成（Pexels搜索+裁剪）
├── image_search.py             # Pexels/Pixabay图片搜索下载
├── wechat_publish.py           # 微信公众号发布器（含Markdown→HTML转码）
├── publish_wechat.py           # 旧的发布脚本（已迁移到wechat_publish.py）
│
├── decompose_case_study.py     # 每日案例拆解管线
├── synthesize_weekly.py        # 周报合成（替代weekly-synthesis skill）
├── framework_article.py        # 框架/方法论文章（骨架版，未启用cron）
├── polish_article.py           # 文章润色
│
├── research_cards.py           # 飞书研究卡片生成+推送
├── stats_daily.py              # 每日采集统计
├── hot_recommend.py            # 公众号底部热门文章推荐
├── fetch_wechat_stats.py       # 公众号数据分析（阅读量等）
├── analyze_wechat_data.py      # 公众号数据深度分析
│
├── library_system_prompts.py   # 系统提示词库
├── tag_schema.py               # 打标维度schema
│
├── lib/
│   ├── __init__.py             # 共享工具（路径、URL注册表）
│   ├── env_loader.py           # .env自动加载
│   ├── llm.py                  # LLM调用封装（OpenRouter）
│   ├── materials.py            # 素材管理（读取/过滤/去重）
│   └── models.py               # 模型配置（集中管理）
│
└── .env.example               # 环境变量模板
```

## 详细脚本说明

### 采集层 (Collection)

| 脚本 | 功能 | 触发方式 |
|------|------|---------|
| `collect_rss.py` | RSS源采集：从配置的RSS feed抓取最新文章 | cron 05:00 Phase 1 |
| `collect_blogs.py` | AI公司博客采集：Anthropic/OpenAI/DeepMind等官方博客 | cron 05:00 Phase 1 |
| `collect_consulting.py` | 咨询报告采集：McKinsey/BCG/Deloitte等，仅周一运行 | cron 05:00 周一 Phase 1 |
| `collect_podcasts.py` | 播客节目发现：仅周一 | cron 05:00 周一 Phase 1 |
| `collect.py` | 统一CLI层，可替代上面所有采集脚本独立调用 | 手动 |

### 过滤层 (Filtering)

| 脚本 | 功能 |
|------|------|
| `filter_items.py` | LLM驱动的素材过滤：调用DeepSeek API判断每条素材是否与企业AI落地相关，打上pass/skip标签 |
| `poll_and_save.py` | 轮询过滤结果并保存到 reports/ 目录 |

### 标签层 (Tagging)

| 脚本 | 功能 |
|------|------|
| `tag_materials.py` | 增量打标：对未标记的新素材，用LLM打7维度标签（形式、主题、视角、证据来源、证据深度、语气、实体） |
| `tag_schema.py` | 7维度标签的Schema定义 |

### 写作层 (Writing)

| 脚本 | 功能 |
|------|------|
| `write_article.py` | 公众号文章全流程：选题判断 → 推理链 → 正文写作 → 配图匹配 → 输出到 wechat-articles/ |
| `embed_images.py` | IMAGE占位符替换：将 `<!-- IMAGE: N -->` 替换为 `![alt](./images/image-NNN.jpg)` |
| `image_search.py` | 图片搜索：Pexels（主）+ Pixabay（备选）搜索并下载配图 |
| `generate_cover.py` | 封面图生成：搜索Pexels → 下载 → 裁剪为2.35:1（文章顶部）和1:1（列表缩略图） |
| `polish_article.py` | 文章润色：LLM驱动的语言优化 |
| `synthesize_weekly.py` | 周报合成：读取7天素材 → 主题聚类 → 写全文 → 配图 → 发布 |
| `framework_article.py` | 框架/方法论文章（骨架版，待启用） |

### 发布层 (Publishing)

| 脚本 | 功能 |
|------|------|
| `wechat_publish.py` | 微信公众号完整发布器：上传图片（永久素材/临时素材） → Markdown转WeChat HTML → 创建草稿 → 可选发布。支持永久封面图复用 |
| `publish_wechat.py` | 早期发布脚本（简化版，逐步迁移中） |

### 案例拆解

| 脚本 | 功能 |
|------|------|
| `decompose_case_study.py` | 每日案例拆解管线：从外部信源发现案例 → 多信源交叉验证 → 写作 → 配图 → 发布。区别于公众号文章（重叙事），案例拆解重信息密度和可复盘细节 |

### 研究卡

| 脚本 | 功能 |
|------|------|
| `research_cards.py` | 研究卡片生成+飞书文档推送：从 reports/ 读取素材 → LLM提炼 → 保存本地 → 按模块推送到飞书文档 |

### 数据分析

| 脚本 | 功能 |
|------|------|
| `stats_daily.py` | 每日采集统计：各种来源的数量、新增/过滤比例 |
| `fetch_wechat_stats.py` | 公众号阅读数据获取（微信公众平台数据接口）|
| `analyze_wechat_data.py` | 公众号数据深度分析 |
| `hot_recommend.py` | 公众号底部热门文章推荐：获取近期高阅读量文章生成推荐HTML |

### 共享库 (lib/)

| 模块 | 功能 |
|------|------|
| `lib/__init__.py` | 路径常量、URL注册表管理、去重逻辑、素材读取 |
| `lib/env_loader.py` | 自动从 .env 文件加载环境变量 |
| `lib/llm.py` | LLM调用封装：通过OpenRouter调用DeepSeek/OpenAI等模型，含重试逻辑 |
| `lib/materials.py` | 素材管理：读取/过滤/去重/采样 |
| `lib/models.py` | 模型配置：集中管理各场景默认模型，支持环境变量覆盖 |

## 管线流程

### 每日采集管线 (cron 05:00)

```
run_all.sh
├── Phase 1: 采集
│   ├── collect_rss.py       ← RSS feed抓取
│   └── collect_blogs.py     ← AI公司博客
│   ├── collect_consulting.py  (仅周一)
│   └── collect_podcasts.py    (仅周一)
│
├── Phase 2: LLM过滤
│   └── filter_items.py <source>  ← DeepSeek API判断相关度
│
├── Phase 3: 保存
│   └── poll_and_save.py <source> ← 保存到 reports/
│
├── Phase 4: 咨询报告策略 (仅周一)
│   └── consulting-report-strategist agent
│
├── Phase 5: 研究卡片 → 飞书
│   └── research_cards.py --max-cards 5
│
└── Phase 6: 每日统计
    └── stats_daily.py --save
```

### 每日文章管线 (cron 06:00)

> **架构说明（2026-07-09 重构）**：写作与发布已**解耦为两个异步阶段**。
> `write_article.py` 只负责产出文章并入队（`queue/pending.jsonl`），随后在后台
> 触发发布 worker；发布由独立的 `publish_worker.py` 进程消费队列完成（仅建草稿，
> 不自动群发，保留人工审稿闸门）。两者互不阻塞，发布超时不会再拖垮写作。

```
run_daily_article.sh
├── Phase 0: 增量打标
│   └── tag_materials.py --max-batches 10
│
├── Phase 1: AI写作 + 入队（Stage A：内容生成）
│   └── write_article.py --date today
│       ├── 选题判断 (LLM, 超时/重试见 lib/llm.py LLM_CALL_TIMEOUT)
│       ├── 推理链构建
│       ├── 正文写作
│       ├── 配图匹配下载（image_search.py，无图时回退 assets/placeholder.jpg）
│       ├── 图片嵌入 (embed_images.py)
│       └── 入队 queue/pending.jsonl + 后台触发 publish_worker.py
│
└── Stage B：发布（独立进程 publish_worker.py，可后台触发或 cron 安全网）
    └── publish_worker.py --once
        └── wechat_publish.py --article article.md --images-dir images/   (无 --publish ⇒ 仅建草稿)
            ├── 并发上传正文图片（单图失败跳过，不崩溃）
            ├── 封面图生成&上传（不计入正文内联图）
            ├── Markdown → WeChat HTML转码
            └── 创建草稿 (draft/add)
```

> cron 07:30 另有 `run_publish_worker.sh`（调用 `publish_worker.py --once`）作为安全网，
> 即使后台触发进程异常退出也能补发。队列状态见 `queue/pending.jsonl` / `done.jsonl` / `failed.jsonl`。

### 每日案例拆解 (cron 10:00)

```
decompose_case_study.py --external
├── 信源发现 ← 外部源池
├── 多信源交叉验证
├── 写作（CASE_STUDY_PROMPT）
├── 配图（image_search.py）
├── 发布（wechat_publish.py）
└── 保存到 wechat-articles/
```

## 环境变量

参考 `.env.example` 配置以下变量：

| 变量名 | 说明 | 必填 |
|--------|------|------|
| `WECHAT_APP_ID` | 微信公众号AppID | ✅ 发布时 |
| `WECHAT_APP_SECRET` | 微信公众号AppSecret | ✅ 发布时 |
| `SERPER_API_KEY` | Serper.dev搜索API密钥 | ✅ 采集时 |
| `ANYSEARCH_API_KEY` | AnySearch搜索API密钥 | ✅ 采集时 |
| `DEEPSEEK_API_KEY` | DeepSeek API密钥 | ✅ 写作/过滤时 |
| `LLM_API_KEY` | LLM API密钥（兼容DeepSeek） | ✅ 写作时 |

## 数据目录

管线运行后会生成以下目录：

```
reports/                    # 采集素材库
├── _index/
│   ├── all_urls.tsv       # 所有URL注册表（collected/used/passed/skipped）
│   └── url_dates.tsv      # URL日期索引
├── rss/                    # RSS素材HTML/JSON
├── blogs/                  # AI公司博客
├── consulting/             # 咨询报告
└── thinktank/              # 智库报告

wechat-articles/            # 公众号文章输出目录
├── (YYYY-MM-DD)-title/
│   ├── article.md          # 文章Markdown
│   ├── images/             # 配图
│   │   ├── image-001-pexels.jpeg
│   │   ├── cover-1x1.jpg
│   │   └── metadata.json
│   ├── meta.json           # 标题/摘要/来源
│   └── wechat_result.json  # 发布记录

research/enterprise-ai-book/cards/  # 研究卡片
```

## 依赖

```bash
pip install Pillow  # 图片裁剪
# 其余为Python标准库（urllib, json, re, subprocess等）
```

## 快速开始

```bash
# 1. 复制并配置环境变量
cp .env.example .env
# 编辑 .env, 填入你的API keys

# 2. 手动跑一次采集管线
bash run_all.sh

# 3. 跑一次文章生成（不含发布）
python3 write_article.py --dry-run

# 4. 配置cron（参考Crontab配置章节）
```

## Crontab配置参考

```cron
# 采集管线 — 每天05:00
0 5 * * * /path/to/collector/run_all.sh

# 每日公众号文章 — 每天06:00
0 6 * * * cd /path/to/collector && bash run_daily_article.sh

# 发布安全网（异步解耦）— 每天07:30 消费发布队列，补发后台触发未完成的草稿
30 7 * * * cd /path/to/collector && bash run_publish_worker.sh

# 每日案例拆解 — 每天10:00（工作日）
0 10 * * 1-5 cd /path/to/collector && python3 decompose_case_study.py --external
```

## 注意事项

1. **发布前请检查标题长度**：微信API限制标题≤12个中文字符（或24个英文字符），脚本会截断过长标题
2. **封面图**：建议尺寸 900×900px（1:1缩略图）和 1200×627px（2.35:1文章顶部）
3. **API限频**：WeChat API有调用频率限制，发布间隔至少1分钟
4. **图片**：公众号正文图片必须通过 `cgi-bin/media/uploadimg` 上传获取CDN URL

## 配置补充说明

### sources.yaml

所有采集源的配置文件，包含：
- **reader**: 内容获取方式（direct HTTP / Jina Reader API）
- **rss**: 30+ RSS源（One Useful Thing、Import AI、TLDR AI、TechCrunch等科技媒体）
- **blogs**: AI公司博客（OpenAI、Anthropic、Google、Microsoft等20+）
- **consulting**: 咨询报告搜索词（McKinsey、BCG等10家）
- **thinktank**: 智库页面（MIT Sloan、HBR、Wharton、RAND）
- **filtering**: LLM过滤规则配置（6个相关度维度及权重）

> ⚠️ `sources.yaml` 中的 API key 已脱敏为占位符，使用前需替换为真实值。
