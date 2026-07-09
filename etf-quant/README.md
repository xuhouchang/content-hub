# 港股 ETF 量化研究工作流

基于 OpenClaw / Python 的港股 ETF 量化轮动系统。

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 运行日报（生成 reports/output/etf_daily_YYYY-MM-DD.md）
python run_daily.py

# 运行回测
python run_backtest.py
```

## 架构

详见 [PLAN.md](PLAN.md)

关键模块：

| 模块 | 职责 |
|------|------|
| `config/` | YAML 配置 + ETF 元信息 |
| `data/` | 行情获取、缓存、标准化 |
| `signals/` | 动量、趋势、波动率因子 |
| `strategy/` | ETF 轮动 + 仓位管理 |
| `backtest/` | 向量化回测 |
| `reports/` | Markdown 日报 |
| `workflow/` | 通知接口（预留） |

## 策略说明

**第一版轮动规则：**
1. 综合得分 = 0.4 × 20日收益 + 0.6 × 60日收益 − 0.2 × 20日波动率
2. Top 1 ETF: 50%，现金: 50%
3. 若 Top ETF 跌破 MA60 → 全仓现金

所有参数在 config.yaml 中配置，无需修改代码。

## 数据源

- **Primary:** Yahoo Finance (yfinance)
- **Fallback:** 预留 Stooq / Alpha Vantage 接口

## 阶段状态

- [x] Phase 1: MVP 日报闭环 (代码就绪)
- [ ] Phase 2: 完整回测框架
- [ ] Phase 3: 数据源增强
- [ ] Phase 4: 自动化部署
- [ ] Phase 5: AI 分析接入

## 红线

- ❌ 不接券商 API
- ❌ 不自动交易
- ❌ LLM 不直接生成买卖信号
