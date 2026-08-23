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

## 架构概览

```
cron 05:00 → run_all.sh              ← 唤醒 collect-daily + cleanup
cron 06:00 → run_daily_full.sh       ← article-daily + Medium
cron */5  → run_publish_worker.sh    ← 独立消费公众号草稿队列
cron 10:00 → run_daily_case.sh       ← 只运行 case-daily
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
├── platform_cli.py               # 内容中台CLI入口（run collect-daily / article-daily / case-daily / cleanup）
├── content_platform/             # 内容中台（当前主链路）
│   ├── cli.py                    # argparse CLI 定义
│   ├── runtime.py                # job 编排：collect-daily（采集→打标→聚类→打分→素材池）
│   ├── ingest/                   # 采集层 loader（每个来源一个模块，读 sources.yaml）
│   │   ├── rss.py                # RSS源采集
│   │   ├── reddit.py             # Reddit热门讨论采集
│   │   ├── blogs.py              # AI公司博客采集
│   │   ├── consulting.py         # 咨询报告 + 智库采集
│   │   └── cases.py              # 案例/播客素材
│   ├── normalize/                # URL规范化 + 素材记录构建
│   ├── curate/                   # 聚类、LLM打分
│   ├── datasets/                 # article_pool / case_pool
│   ├── business/                 # 文章/案例管线编排
│   └── paths.py / storage/       # 路径与JSON存取
│
├── run_all.sh                    # 总采集入口（cron 05:00 → collect-daily + cleanup）
├── run_daily_full.sh             # 每日文章 + Medium（cron 06:00）
├── run_daily_article.sh          # 仅文章生成入口（cron 06:00）
├── run_daily_case.sh             # 仅案例拆解入口（cron 10:00）
├── run_publish_worker.sh         # 独立草稿发布 worker（cron 每5分钟）
├── run_daily_article_publish.sh  # 发布 worker（手动/备用）
├── run_daily_article_writeonly.sh# 仅写作不入队（手动）
├── run_medium_article.sh         # Medium 文章入口
├── run_wechat_stats.sh           # 微信数据分析管线（手动触发）
│
├── write_article.py              # 公众号文章写作（选题→正文→配图→入队）
├── write_medium_article.py       # Medium 英文文章写作
├── decompose_case_study.py       # 每日案例拆解管线
├── synthesize_weekly.py          # 周报合成
├── framework_article.py          # 框架/方法论文章（骨架版，未启用cron）
├── polish_article.py             # 文章润色
├── embed_images.py               # IMAGE占位符替换（fallback）
├── generate_cover.py             # 文章封面图生成（Pexels搜索+裁剪）
├── image_search.py               # Pexels/Pixabay图片搜索下载
├── wechat_publish.py             # 微信公众号发布器（含Markdown→HTML转码）
├── publish_worker.py             # 发布 worker（消费 queue/）
├── publish_queue.py              # 发布队列（文件锁）
│
├── research_cards.py             # 飞书研究卡片生成+推送
├── stats_daily.py                # 每日采集统计
├── hot_recommend.py              # 公众号底部热门文章推荐
├── fetch_wechat_stats.py         # 公众号数据分析（阅读量等）
├── analyze_wechat_data.py        # 公众号数据深度分析
│
├── library_system_prompts.py     # 系统提示词库
├── tag_schema.py                 # 打标维度schema
│
├── sources.yaml                  # 采集源配置（rss/reddit/blogs/consulting/thinktank/podcast）
├── lib/
│   ├── __init__.py               # 共享工具（路径、URL注册表、去重、quick-filter）
│   ├── env_loader.py             # .env自动加载
│   ├── llm.py                    # LLM调用封装（OpenRouter）
│   ├── materials.py              # 素材管理（读取/过滤/去重）
│   ├── models.py                 # 模型配置（集中管理）
│   └── page_utils.py             # 页面正文/链接提取工具（extract_page_summary 等）
│
└── .env.example                  # 环境变量模板
```

## 详细脚本说明

### 采集层 (Collection)

采集统一由 `platform_cli.py run collect-daily` 调度，各来源 loader 在 `content_platform/ingest/` 下，配置在 `sources.yaml`：

| 来源 | 模块 | 说明 |
|------|------|------|
| RSS | `content_platform/ingest/rss.py` | 从配置的 RSS feed 抓取最新文章 |
| Reddit | `content_platform/ingest/reddit.py` | 抓取配置子版块 top/hot 热门讨论，quick-filter 筛企业AI落地相关 |
| 公司博客 | `content_platform/ingest/blogs.py` | Anthropic/OpenAI/Google/Microsoft 等官方博客 |
| 咨询报告 | `content_platform/ingest/consulting.py` | McKinsey/BCG/Deloitte 等（Serper 搜索）+ 智库列表页 |
| 案例/播客 | `content_platform/ingest/cases.py` | 案例拆解与播客素材 |

### 过滤/打标层 (Filtering & Tagging)

过滤与打标内嵌在 collect-daily 的 curate 阶段（`content_platform/runtime.py`）：
- **LLM 打标**：对素材打 7 维度标签（形式、主题、视角、证据来源、证据深度、语气、实体），schema 见 `tag_schema.py`
- **聚类去重**：按标签+内容 hash 聚类，跨天内容级去重
- **LLM 打分**：`editorial_fit_score` 打分，高于阈值进入素材池

### 写作层 (Writing)

| 脚本 | 功能 |
|------|------|
| `write_article.py` | 公众号文章全流程：选题判断 → 推理链 → 正文写作 → 配图匹配 → 输出到 wechat-articles/ |
| `embed_images.py` | IMAGE占位符替换：将 `<!-- IMAGE: N -->` 替换为 `![alt](./images/image-NNN.jpg)` |
| `image_search.py` | 图片搜索：Pexels（主）+ Pixabay（备选）搜索并下载配图 |
| `lib/pexels_images.py` | 正文配图「Pexels-first」可复用模块：`resolve_image()` 按 已有图→Pexels→(仅显式允许时)生成式 解析并做尺寸/格式归一化 |
| `generate_cover.py` | 封面图生成：搜索Pexels → 下载 → 裁剪为2.35:1（文章顶部）和1:1（列表缩略图） |
| `polish_article.py` | 文章润色：LLM驱动的语言优化 |
| `synthesize_weekly.py` | 周报合成：读取素材 → 主题聚类 → 写全文 → 配图 → 发布 |
| `framework_article.py` | 框架/方法论文章（骨架版，待启用） |

### 发布层 (Publishing)

| 脚本 | 功能 |
|------|------|
| `wechat_publish.py` | 微信公众号完整发布器：上传图片（永久素材/临时素材） → Markdown转WeChat HTML → 创建草稿 → 可选发布。支持永久封面图复用 |

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
└── platform_cli.py run collect-daily
    ├── Phase 1: 采集（content_platform/ingest/）
    │   ├── load_rss_materials       ← RSS feed抓取
    │   ├── load_reddit_materials    ← Reddit热门讨论（sources.yaml → reddit）
    │   ├── load_blog_materials      ← AI公司博客
    │   ├── load_consulting_materials← 咨询报告 + 智库
    │   └── load_case_materials      ← 案例/播客
    │
    ├── Phase 2: 规范化 + LLM打标（7维度）
    ├── Phase 3: 聚类 + 内容级去重
    ├── Phase 4: LLM打分（editorial_fit_score ≥ 0.65 进素材池）
    ├── Phase 5: 构建素材池
    │   ├── datasets/article_pool.json  ← 公众号文章候选
    │   └── datasets/case_pool.json     ← 案例拆解候选
    └── Phase 6: cleanup（清理30天前中间产物）
```

### 每日文章管线 (cron 06:00)

> **架构说明（2026-08-03 修订）**：写作与发布是两个独立调度阶段。
> `write_article.py` 与 `decompose_case_study.py` 只负责产出并写入
> `queue/pending.jsonl`，绝不自行启动 sender。cron 每 5 分钟独立运行
> `publish_worker.py` 创建草稿，不自动群发，保留人工审稿闸门。

```
run_daily_article.sh
├── Phase 0: 读取素材池（打标/打分已在 collect-daily 完成）
│   └── datasets/article_pool.json → 选材
│
├── Phase 1: AI写作 + 入队（Stage A：内容生成）
│   └── write_article.py --date today
│       ├── 选题判断 (LLM, 超时/重试见 lib/llm.py LLM_CALL_TIMEOUT)
│       ├── 推理链构建
│       ├── 正文写作
│       ├── 配图匹配下载（image_search.py，无图时回退 assets/placeholder.jpg）
│       ├── 图片嵌入 (embed_images.py)
│       └── 入队 queue/pending.jsonl 后退出
│
└── Stage B：发布（独立 cron，每 5 分钟）
    └── publish_worker.py --once
        └── wechat_publish.py --article article.md --images-dir images/   (无 --publish ⇒ 仅建草稿)
            ├── 并发上传正文图片（单图失败跳过，不崩溃）
            ├── 封面图生成&上传（不计入正文内联图）
            ├── Markdown → WeChat HTML转码
            └── 创建草稿 (draft/add)
```

> 队列写入和 worker 迁移记录都受文件锁保护；失败会按 5/10 分钟退避重试，
> 三次失败后进入 `failed.jsonl`。队列状态见 `pending.jsonl`、`processing.jsonl`、
> `done.jsonl` 与 `failed.jsonl`。

### 每日案例拆解 (cron 10:00)

```
decompose_case_study.py --external
├── 信源发现 ← 外部源池
├── 多信源交叉验证
├── 写作（CASE_STUDY_PROMPT）
├── 配图（image_search.py）
├── 入队（publish_queue.py）
└── 独立 worker 创建微信草稿
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
reports/                    # URL注册表 + 历史素材库
├── _index/
│   ├── all_urls.tsv       # 所有URL注册表（collected/used/passed/skipped）
│   └── content_hashes.tsv # 内容hash注册表（跨天转载去重）
├── newsletters/            # RSS素材存档
├── blog/                   # AI公司博客存档
├── consulting-reports/     # 咨询报告存档
└── reddit/                 # Reddit讨论存档

platform/                   # 内容中台中间产物（collect-daily 产出）
├── ingest/raw/YYYY-MM-DD/  # 原始采集素材
├── normalize/YYYY-MM-DD/   # 规范化素材
├── curate/YYYY-MM-DD/      # 聚类打分后素材
├── datasets/YYYY-MM-DD/    # article_pool / case_pool
└── jobs/YYYY-MM-DD/        # job 状态与日志

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
0 5 * * * /path/to/content-hub/run_all.sh

# 每日公众号文章 — 每天06:00
0 6 * * * cd /path/to/content-hub && bash run_daily_article.sh

# 独立发布阶段 — 每5分钟消费队列，空队列静默
*/5 * * * * cd /path/to/content-hub && bash run_publish_worker.sh

# 每日案例拆解 — 每天10:00，只运行 case-daily
0 10 * * * cd /path/to/content-hub && bash run_daily_case.sh
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
- **reddit**: Reddit热门讨论子版块（artificial、AI_Agents、MachineLearning等，走公开JSON API，配REDDIT_CLIENT_ID/SECRET时自动用OAuth）
- **blogs**: AI公司博客（OpenAI、Anthropic、Google、Microsoft等20+）
- **consulting**: 咨询报告搜索词（McKinsey、BCG等10家）
- **thinktank**: 智库页面（MIT Sloan、HBR、Wharton、RAND）
- **podcast**: 播客频道/Feed（案例拆解用）
- **filtering**: 相关度过滤规则配置（quick-filter 关键词见 `lib/__init__.py` TOPIC_KW_MAP）

> ⚠️ `sources.yaml` 中的 API key 已脱敏为占位符，使用前需替换为真实值。
