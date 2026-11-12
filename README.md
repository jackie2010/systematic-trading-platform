# Systematic Trading Platform

基于 [zvt](https://github.com/zvtvz/zvt) 的系统化量化交易平台实践仓库：**数据采集 → 数据存储 → 因子计算 → 回测 → 实盘/模拟交易** 的完整链路，并附带一套分层环境校验工具。

> 本仓库源自 `zvt` 源码快照，在其之上补充了工程化文档与可运行的环境校验脚本。上游原始说明见 [README-upstream.md](README-upstream.md)（英文）与 [README-upstream-cn.md](README-upstream-cn.md)（中文）。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%20%7C%203.10%20%7C%203.11%20%7C%203.12-blue.svg)](https://www.python.org/)

## 核心模块

| 模块 | 职责 |
| --- | --- |
| `zvt.domain` | 定义数据长什么样：实体（股票/指数/ETF…）与行情、财务、股东等 Schema |
| `zvt.recorders` | 数据从哪来、怎么增量落库：`em`、`eastmoney`、`joinquant`、`sina`、`exchange`、`qmt` 等多数据源 |
| `zvt.contract` | 通用底座：Schema / 存储路由 / `query_data` / 因子基类 / 状态服务 |
| `zvt.factors` | 把行情变成可筛选的市场描述：均线、MACD、财务、技术指标 |
| `zvt.trader` | 回测与交易：下单、持仓、账户快照、绩效统计 |
| `zvt.rest` / `zvt.ui` | REST 服务与 Dash/Plotly 可视化界面 |

## 快速开始

要求 Python `>=3.9, <4`。

```bash
# 1. 创建虚拟环境并安装
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
# source .venv/bin/activate

pip install -r requirements.txt
pip install -e .
```

```python
# 2. 采集数据（默认数据目录为 ~/zvt-home/data）
from zvt.domain import Stock, Stock1dHfqKdata

Stock.record_data(provider="em")
Stock1dHfqKdata.record_data(provider="em", entity_ids=["stock_sz_000001"])

# 3. 查询
df = Stock1dHfqKdata.query_data(provider="em", entity_ids=["stock_sz_000001"], limit=5)
print(df)
```

```python
# 4. 因子 + 回测
from zvt.factors.ma.ma_factor import CrossMaFactor
from zvt.trader.trader import StockTrader

factor = CrossMaFactor(
    entity_ids=["stock_sz_000001"],
    provider="em",
    windows=[5, 10],
    start_timestamp="2024-01-01",
)
factor.compute()

trader = StockTrader(
    entity_ids=["stock_sz_000001"],
    provider="em",
    start_timestamp="2024-01-01",
)
trader.run()
```

## 环境校验：`examples/verify_env.py`

环境出问题时症状往往相似（"没数据""回测不成交"），但根因可能在任何一层。该脚本按**依赖方向**从底层往上层拆成独立检查项，**首个 FAIL 即问题所在层**，可直接接入 CI（失败退出码为 1）。

```bash
python examples/verify_env.py               # 基础 8 项：依赖/环境/存储/元数据/K线/质量/幂等/因子
python examples/verify_env.py --full        # 追加回测（基础 + 自定义钩子）与 REST 服务自启动，共 11 项
python examples/verify_env.py --grid        # 参数网格对比回测（3 组均线参数）
python examples/verify_env.py --cross       # 多因子类型交叉对比（CrossMa / Bull / GoldCross × 2 组参数）
python examples/verify_env.py --report      # 导出回测结果 CSV + 净值/回撤走势图
python examples/verify_env.py --external    # 外部数据源接入：CSV → 独立 provider → 双通道对照
python examples/verify_env.py --incremental # 外部源增量同步：分批入库 + 幂等 + 断点续采
```

输出示例：

```
[PASS] 依赖: Python 3.12.13, 19 个声明依赖全部满足
[PASS] 环境变量: version=0.13.5, data_path=...\zvt-home\data
[PASS] 存储目录: 3 个 db: ['em_stock_1d_hfq_kdata.db', ...]
[PASS] 元数据: 5641 只股票
[PASS] K 线: latest=2026-11-09, 抽样 5 条
[PASS] 数据质量: 242 条通过质量校验, 新鲜度 lag=0 天
[PASS] 采集幂等: 5 行在重复采集后保持不变
[PASS] 因子: result rows=242

结果：8 PASS / 0 FAIL
```

配套单测：

```bash
python -m pytest tests/test_verify_env.py -q
```

## 目录结构

```
├── src/zvt/               # 框架源码
│   ├── domain/            # Schema 定义（实体 + 行情 + 财务 + 板块…）
│   ├── recorders/         # 各数据源采集器
│   ├── contract/          # Schema / 存储 / 查询 / 因子基类
│   ├── factors/           # 因子实现
│   ├── trader/            # 交易与回测
│   ├── rest/              # REST 接口
│   └── ui/                # Dash 可视化
├── examples/              # 示例脚本（verify_env.py、数据/因子/交易 runner）
├── tests/                 # 单元测试
├── docs/
│   ├── architecture.md    # 架构与环境验证清单（含逐项代码示例）
│   └── storage_config.md  # 存储配置说明
├── sql/                   # 运维 SQL
└── requirements.txt
```

## 文档

- [架构说明与 14.11 环境验证清单](docs/architecture.md)：分层设计、数据流、存储路由、回测常见坑位与逐项校验示例
- [存储配置](docs/storage_config.md)
- 上游使用文档：<https://zvt.readthedocs.io/en/latest/>

## 开发

```bash
pip install -r requirements/dev.txt
pytest ./tests --ignore=tests/recorders/     # 单测（不跑联网采集）
python examples/verify_env.py --full         # 环境冒烟（真实库）
```

关于测试隔离：`tests/__init__.py` 会设置 `TESTING_ZVT=True`，把 zvt 重定向到样例库 `~/zvt-test-home`；因此在 CI 中调用 `verify_env.py` 需用 subprocess 并显式移除该环境变量。

## 许可证与致谢

本项目基于 [zvt](https://github.com/zvtvz/zvt)（作者 zvtvz）二次整理，遵循 [MIT License](LICENSE)，版权归原作者所有。上游 README 保留于 [README-upstream.md](README-upstream.md) 供对照参考。

投资有风险，本项目仅用于技术研究与学习，不构成任何投资建议。
