# 港股 ETF 量化研究工作流 — PLAN v1

> 版本: v1 (2026-05-17)
> 后续计划迭代到 v2 时，在文件顶部维护更新日志

---

## 1. 项目定位

不是高频交易系统，不是复杂 AI 策略。

**核心目标：**

1. 建立稳定的数据获取层
2. 建立可扩展的策略研究框架
3. 建立标准化回测结构
4. 建立风控与持仓管理逻辑
5. 输出每日 ETF 轮动建议
6. 后续支持 AI 分析模块接入

**红线：** 不接券商 API，不自动交易，不让 LLM 直接生成买卖信号。

---

## 2. 标的池

| 代码 | 名称 | 类型 | 防守标记 |
|------|------|------|----------|
| 02800.HK | 恒生指数 ETF | equity | false |
| 03033.HK | 恒生科技 ETF | equity | false |
| 02828.HK | 国企 ETF | equity | false |
| 02840.HK | SPDR 金 ETF | defensive | true |
| CASH | 现金占位 | defensive | true |

**决策说明：**

- 防守资产第一版使用 `CASH`（现金/不持仓），不强行选择货币 ETF
- 02840.HK（SPDR 金 ETF）作为黄金防守资产候选，流动性充足
- 防守资产通过 `asset_type: "defensive"` 标记，由 strategy 层按规则切换

---

## 3. 仓位逻辑（第一版）

### 3.1 轮动简仓

```
若 top ETF 价格 > MA60（确认上升趋势）:
  - top1 ETF: 50%
  - CASH: 50%

若 top ETF 价格 ≤ MA60（跌破趋势线）:
  - CASH: 100%
```

### 3.2 候选增强版（预留，不实现）

```
若 top ETF 价格 > MA60:
  - top1 ETF: 50%
  - top2 ETF: 30%
  - CASH: 20%
```

增强版在 Phase 2 或根据回测结果决定是否启用。

---

## 4. 系统架构

### 4.1 架构图

```
                    workflows/
  ┌─────────┐  ┌──────────┐  ┌────────────────┐
  │ 日报运行  │  │ 回测运行  │  │ AI分析(预留)   │
  └────┬────┘  └────┬─────┘  └───────┬────────┘
       │            │                 │
       ▼            ▼                 ▼
  ┌────────────────────────────────────────────┐
  │            run_daily.py / run_*.py          │
  └──────┬──────────┬──────────┬─────────┬─────┘
         │          │          │         │
    ┌────▼──┐ ┌───▼────┐ ┌───▼───┐ ┌───▼────┐
    │ data/ │ │signals/│ │strategy│ │reports/│
    └───┬───┘ └───┬────┘ └───┬───┘ └───┬────┘
        │         │          │         │
        ▼         │          │         ▼
   ┌────────┐     │          │   本地文件输出
   │本地缓存  │     │          │
   │CSV/     │     │          │
   │Parquet  │     │          │
   └────────┘     │          │
                  ▼          ▼
             ┌──────────────────────┐
             │      config/        │
             │  (参数配置化，无硬编码)│
             └──────────────────────┘

backtest/ (独立流程):
   run_backtest.py → backtest/engine  → 回测结果
                          ↑
                   strategy/ (复用相同接口)
```

### 4.2 数据流

```
Yahoo Finance → data/loader.py → 标准化 DataFrame
                                      │
                                      ▼
                              signals/ 因子计算
                                      │
                                      ▼
                              strategy/ 策略引擎
                                      │
                                      ▼
                              reports/ 日报生成
                                      │
                           ┌──────────┴──────────┐
                           ▼                     ▼
                     本地Markdown日报      飞书/邮件通知(预留)

回测数据流:
  data_cache/hist/ → backtest/engine(读入)
                          ↑
                   strategy/ 作为可调用模块注入
```

### 4.3 模块职责矩阵

| 模块 | 职责 | 外部依赖 | 是否可独立测试 |
|------|------|----------|--------------|
| `data/` | 获取、缓存、标准化行情数据 | yfinance, pandas | 是 |
| `signals/` | 因子计算（动量、趋势、波动率） | pandas, numpy | 是 |
| `strategy/` | 轮动逻辑、仓位管理、规则引擎 | pandas, signals | 是 |
| `backtest/` | 向量化回测、指标计算 | pandas, signals, strategy, vectorbt | 是 |
| `reports/` | Markdown 日报生成 | pandas, strategy | 是 |
| `config/` | 配置加载与校验 | pyyaml, pydantic | 是 |
| `workflow/` | 通知接口、工作流编排 | (无硬依赖) | — |

---

## 5. Score 公式

### 5.1 第一版

```python
score = 0.4 * ret_20d + 0.6 * ret_60d - 0.2 * vol_20d
```

- `ret_20d`: 20 日收益率
- `ret_60d`: 60 日收益率
- `vol_20d`: 20 日年化滚动波动率（归一化后在公式中与收益率同量级）
- 系数全部在 `config.yaml` 中配置，可调

### 5.2 预留扩展

- 成交额过滤（避免流动性不足的 ETF）
- 波动率分级惩罚（非线性）
- 多因子加权（动量+趋势强度+低波）

---

## 6. 回测方案

### 6.1 Phase 1 — 极简回测验证

MVP 内置 `backtest/quick.py`，功能：

- 输入：最近 1 年日线数据
- 频率：每周/每日调仓
- 输出指标：
  - 年化收益率
  - 最大回撤
  - 夏普比率
  - 胜率（上涨天数比例）
  - 总收益
- 方法：vectorbt 向量化，单文件可运行

### 6.2 Phase 2 — 完整回测框架

- 多参数网格搜索
- 滚动窗口验证
- 超额收益分解
- 与基准（恒指持有）对比
- 交易成本模拟

### 6.3 核心原则

- 回测逻辑与实盘逻辑解耦（`strategy/` 模块复用，`backtest/engine.py` 独立）
- 不允许策略逻辑写死在回测器里
- 不允许 future bias

---

## 7. 数据源策略

### 7.1 分层数据源

| 层级 | 数据源 | 用途 | 阶段 |
|------|--------|------|------|
| Primary | Yahoo Finance (yfinance) | MVP 原型数据 | Phase 1 |
| Fallback 1 | Stooq | 冗余备选 | Phase 2 |
| Fallback 2 | Alpha Vantage（免费 tier） | 第二备选 | Phase 2 |
| 付费 | 待定（wind / 同花顺 / 券商数据） | 生产环境 | Phase 3+ |

### 7.2 data 层接口设计原则

```python
# 抽象接口: 所有数据源实现同一接口
class DataSource(ABC):
    def fetch(self, tickers: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
        ...

# 使用方式
source = YahooSource()
# 后续 fallback: source = FailoverSource([YahooSource(), StooqSource()])
data = source.fetch(...)
```

---

## 8. 参数配置化设计

所有硬编码参数必须放入 `config.yaml`，通过 `config/settings.py` 加载。

**核心配置项分类：**

| 分类 | 配置项 | 默认值 | 说明 |
|------|--------|--------|------|
| 标的 | tickers | 02800.HK, 03033.HK, 02828.HK, 02840.HK | |
| 标的 | defensive_tickers | CASH, 02840.HK | 防守资产列表 |
| 动量 | ret_periods | [20, 60] | 收益率计算周期 |
| 动量 | ret_weights | [0.4, 0.6] | 动量权重 |
| 波动率 | vol_penalty | 0.2 | 波动率惩罚系数 |
| 波动率 | vol_window | 20 | 波动率窗口 |
| 趋势 | ma_periods | [20, 60] | MA 窗口 |
| 仓位 | max_position | 0.5 | 单标的仓位上限 |
| 仓位 | defensive_on_break_ma | 60 | 跌破该 MA 则防守 |
| 回测 | backtest_years | 1 | 回测年数 |
| 数据 | cache_dir | data_cache/hist | 缓存目录 |

---

## 9. 日报输出格式

```markdown
# 港股 ETF 日报 — 2026-05-17

## 当前建议
- 标的：03033.HK
- 仓位：50%
- 现金：50%

## 信号明细
| ETF | 20日收益 | 60日收益 | 波动率 | 得分 | 价格/MA60 |
|-----|----------|----------|--------|------|-----------|
| 03033.HK | +3.2% | +8.5% | 25.3% | 0.052 | +2.1% |
| 02800.HK | +1.8% | +5.1% | 18.2% | 0.028 | +1.0% |
| ...

## 趋势状态
- 03033.HK：上升趋势（价格 > MA20 > MA60）
- 02800.HK：上升趋势
- 02828.HK：横盘震荡
- 02840.HK：防守资产

## 风险提示
- 恒生科技波动率处于近期高位
- 若 03033.HK 跌破 MA60（当前 $20.50），建议切换到现金
```

---

## 10. 项目目录结构

```
workspace/etf-quant/
├── README.md                     # 项目说明
├── PLAN.md                       # 本计划文档
├── requirements.txt              # 依赖清单
├── config.yaml                   # 全局参数配置（无硬编码）
├── .gitignore
│
├── run_daily.py                  # 每日运行入口
├── run_backtest.py               # 回测入口
│
├── config/
│   ├── __init__.py
│   ├── settings.py               # pydantic 配置加载
│   └── universe.py               # ETF 标的元信息
│
├── data/
│   ├── __init__.py
│   ├── loader.py                 # 数据获取统一入口
│   ├── cache.py                  # 本地缓存管理
│   ├── standardizer.py           # 数据标准化
│   └── sources/
│       ├── __init__.py
│       ├── base.py               # 数据源抽象接口
│       └── yahoo.py              # Yahoo Finance 实现
│
├── signals/
│   ├── __init__.py
│   ├── momentum.py               # 多周期动量
│   ├── trend.py                  # MA 趋势判断
│   ├── volatility.py             # ATR / 历史波动率
│   └── composite.py              # 综合因子得分
│
├── strategy/
│   ├── __init__.py
│   ├── rotation.py               # ETF 轮动逻辑
│   ├── position.py               # 仓位管理
│   └── rules.py                  # 规则引擎
│
├── backtest/
│   ├── __init__.py
│   ├── quick.py                  # Phase 1 极简回测
│   └── metrics.py                # 表现指标计算
│
├── reports/
│   ├── __init__.py
│   └── daily.py                  # 日报生成
│
├── workflow/
│   ├── __init__.py
│   └── notify.py                 # 通知接口（飞书预留）
│
└── data_cache/                   # gitignored
    └── hist/
```

---

## 11. Phase 开发计划

### Phase 1 — MVP 可运行闭环

1. config/ 层（config.yaml + universe + settings）
2. data/ 层（抽象接口 + yahoo 实现 + 缓存 + 标准化）
3. signals/ 层（动量 + 趋势 + 波动率 + 综合得分）
4. strategy/ 层（轮动 + 仓位 + 规则引擎）
5. backtest/quick.py（极简回测验证）
6. reports/daily.py（日报输出）
7. run_daily.py（串联全流程）
8. run_backtest.py（回测执行入口）

**输出验证：** 跑通 `run_daily.py`，能在本地生成一份完整的 Markdown 日报。

### Phase 2 — 回测框架完善

- backtest/engine + 多参数搜索
- 交易成本模拟
- 基准对比
- 参数优化

### Phase 3 — 数据源增强

- Stooq fallback 实现
- 成交额过滤
- 数据质量监控

### Phase 4 — 自动化部署

- OpenClaw cron job 每日收盘后自动运行（16:30 HKT）
- 日报输出到飞书/群聊
- 异常告警

### Phase 5 — AI 分析接入（预留）

- LLM 解读日报 + 叠加判断
- 新闻情绪信号
- 异常模式识别

---

## 12. OpenClaw 工作流接入方案

### 12.1 日报自动运行

**cron job 配置：**

```yaml
schedule:
  kind: cron
  expr: "30 4 * * 1-5"    # UTC 04:30 = HKT 12:30（收盘后）
tz: Asia/Shanghai
payload:
  kind: "agentTurn"
  message: "运行 ETF 量化日报：cd ~/.openclaw/workspace/etf-quant && python run_daily.py"
```

### 12.2 通知通道

- 第一版：Python 脚本写入本地 Markdown 文件
- 第二版：飞书 webhook → 群卡片消息（预留 `workflow/notify.py` 接口）

### 12.3 LLM 介入方式（红线）

- LLM 不直接生成买卖信号
- LLM 仅分析日报、识别异常、提供额外 context
- 信号源始终是 strategy 模块的数学规则

### 12.4 回测触发

- 手动触发：`python run_backtest.py`
- 参数变更后自动回测（Phase 3+）

---

## 13. 代码质量原则

1. **可迭代** — 不产出"对称屎山"，每个模块只做一件事
2. **低耦合** — 模块之间通过函数接口交互，不互相引用全局变量
3. **高内聚** — 信号计算在 signals/，仓位逻辑在 strategy/，不混层
4. **内容清晰** — 每个函数不超过 30 行，每层不超过 200 行
5. **配置驱动** — 不允许任何硬编码参数
6. **测试友好** — 核心函数接收 DataFrame，返回 DataFrame，纯函数风格
7. **类型提示** — 所有公共函数有 type hints

---

## 14. 风险评估

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|----------|
| yfinance 港股数据不稳定 | 中 | 中 | data 层预留 fallback 接口 |
| 复权数据偏差 | 低 | 中 | 回测时使用调整收盘价，与实盘信号同一数据源 |
| MA60 过于频繁触发防守 | 中 | 中 | 系数可配置，回测验证后调整 |
| 港股休市日期影响 | 低 | 低 | use_cache = True，跳过缺失日 |
| 策略过拟合 | 中 | 高 | 极简公式、少量参数、Out-of-sample 验证 |
