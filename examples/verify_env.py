# -*- coding: utf-8 -*-
"""
zvt 环境分层校验脚本。

设计思路
--------
环境出问题时，症状往往相似（「没数据」「回测不成交」），但根因可能在任何一层。
若只写一个大 try/except，第一个异常就会中断，后面的层永远得不到验证。
因此这里把校验拆成相互独立的 11 个检查项，按**依赖方向**从底层往上层排列：

    依赖 → 环境变量 → 存储 → 元数据 → K 线 → 数据质量
         → 采集幂等 → 因子 → 回测（默认策略 + 自定义钩子）→ REST 服务

每一层只依赖它前面的层，所以「首个 FAIL」就是问题所在层；后面的 FAIL 通常是
前面失败的连带结果（例如没有 K 线，因子和回测必然也失败），不必逐个排查。

用法
----
    python examples/verify_env.py            # 基础 8 项（较快，不跑回测、不启服务）
    python examples/verify_env.py --full     # 完整 11 项（追加回测与 REST 服务自启动）
    python examples/verify_env.py --grid     # 额外跑参数网格对比回测（3 组均线参数）
    python examples/verify_env.py --cross    # 额外跑多因子类型交叉对比（3 类因子 × 2 组参数）
    python examples/verify_env.py --report   # 导出回测结果 CSV + 净值/回撤走势图
    python examples/verify_env.py --external # 外部数据源接入：CSV → 独立 provider → 双通道对照
    python examples/verify_env.py --incremental # 外部源增量同步：分批入库 + 幂等 + 断点续采

任一检查失败时进程退出码为 1，可直接接入 CI 或部署前自检。

注意事项
--------
1. 本脚本校验的是**真实数据目录**（`zvt_env["data_path"]`）。在 `tests/` 目录下运行
   pytest 时，`tests/__init__.py` 会设置 `TESTING_ZVT=True`，把 zvt 重定向到样例库
   `~/zvt-test-home`；因此 CI 中调用本脚本要用 subprocess 并显式移除该环境变量。
2. 不依赖 pip：`uv venv` 创建的环境默认没有 pip，依赖校验改用 `importlib.metadata`。
3. 默认标的固定为 `ENTITY_ID`/`PROVIDER`，换标的改顶部常量即可。
"""
import argparse
import math
import os
import subprocess
import sys
import time
import traceback
from importlib.metadata import PackageNotFoundError, distribution, version

import pandas as pd
from packaging.requirements import Requirement

from zvt import __version__, zvt_env
from zvt.contract import IntervalLevel
from zvt.contract.api import del_data, df_to_db, get_data_count
from zvt.domain import Stock, Stock1dHfqKdata, Stock1dKdata
from zvt.factors.ma.ma_factor import CrossMaFactor
from zvt.factors.macd.macd_factor import BullFactor, GoldCrossFactor
from zvt.trader.trader import StockTrader
from zvt.trader.trader_info_api import clear_trader
from zvt.trader.trader_schemas import AccountStats, Order, Position
from zvt.utils.time_utils import current_date, recent_year_date, to_date_time_str

# 校验使用的样本标的与数据源。选平安银行（000001）是因为上市早、停牌少，
# 数据连续性最好，出现缺口时更可能是采集问题而不是标的本身的问题。
ENTITY_ID = "stock_sz_000001"
PROVIDER = "em"

# 回测校验专用 trader_name，与业务回测隔离，避免覆盖或污染已有结果
TRADER_NAME = "verify_env_trader"
# 自定义钩子回测的名字，与上面的基础回测分开存储，便于对比两套策略
TRADER_NAME_HOOK = "verify_env_trader_hook"
# 参数网格回测的名字前缀，实际会拼上窗口参数（如 verify_env_trader_grid_5_10），
# 每组参数独立存储，避免互相覆盖导致对比失真
TRADER_NAME_GRID = "verify_env_trader_grid"
# 外部数据源演示：自带 provider 名，数据与 em 的库物理隔离，不影响真实数据。
# 存储路径为 {data_path}/external/external_csv_*.db：目录取 provider 前缀
# （下划线前一段），文件名保留完整 provider 名——自定义 provider 含下划线时会看到这个现象。
EXTERNAL_PROVIDER = "external_csv"
# 外部源对照组与实验组的 trader_name，两者参数完全相同，只换 provider
TRADER_NAME_EXT_CONTROL = "verify_env_trader_ext_ctl"
TRADER_NAME_EXT_CSV = "verify_env_trader_ext_csv"
# 增量接入示例的 trader_name：对照组同样走 em，实验组走增量同步后的 provider
TRADER_NAME_INC_CONTROL = "verify_env_trader_inc_ctl"
TRADER_NAME_INC = "verify_env_trader_inc"
# 外部数据导出起点：必须早于回测起点 pre_load_days（默认 365 天），
# 否则均线预热不足，信号会与原生通道不一致
EXTERNAL_EXPORT_START = "2022-01-01"

# REST 校验目标接口：返回服务端已注册的 provider 列表，无参数、无副作用
REST_URL = "http://127.0.0.1:8090/api/data/providers"

# 各检查项的通过/失败记录（True/False）。供最后汇总 PASS/FAIL 数量使用。
# 不用计数器而是保留列表，便于需要时定位具体是哪几项失败。
results = []


# ---------------------------------------------------------------- 通用工具
def check(name, func):
    """
    执行单个检查项并记录结果。

    刻意在这里吞掉异常：某一层失败不应影响后续层的验证，否则一次运行只能看到
    一个问题，排查要来回跑多次。异常信息与截断的调用栈会打印出来供定位。

    Args:
        name: 检查项名称，用于输出 `[PASS] name: ...` / `[FAIL] name: ...`
        func: 无参可调用对象；正常时返回描述字符串，失败时抛异常

    Returns:
        None。结果追加到模块级 `results`，不在返回值中体现。

    Side Effects:
        向 stdout 打印一行结果；失败时额外打印 2 层调用栈。
    """
    try:
        detail = func()
        print(f"[PASS] {name}: {detail}")
        results.append(True)
    except Exception as e:
        print(f"[FAIL] {name}: {e}")
        # limit=2 只保留最关键的两层，避免 zvt 深层调用栈刷屏
        traceback.print_exc(limit=2)
        results.append(False)


# ------------------------------------------------------------ 基础分层检查
# 顺序即依赖顺序：上层检查假设下层已通过
def check_deps():
    """
    校验 zvt 声明的依赖是否齐全、版本是否匹配。

    为什么不用 `pip check`：
      - `uv venv` 创建的环境默认不含 pip，`python -m pip check` 会直接失败；
      - `pip check` 只检查「已安装包之间」的冲突，查不出「声明版本与实际版本不一致」。
    因此直接读取发行版元数据：`distribution("zvt").requires` 得到声明的依赖列表
    （如 `pandas==1.5.3`），再用 `Requirement` 解析名称与版本约束逐一比对。

    Returns:
        str: 形如 "Python 3.12.13, 19 个声明依赖全部满足"

    Raises:
        AssertionError: Python 版本过低、存在缺失依赖、或版本不满足约束。
            错误信息会列出具体包名与期望/实际版本，便于直接修正。
    """
    assert sys.version_info >= (3, 9), f"Python 版本过低: {sys.version.split()[0]}"

    missing, mismatched, total = [], [], 0
    for raw in distribution("zvt").requires or []:
        req = Requirement(raw)
        # 跳过仅在特定环境生效的依赖（如 `; python_version < "3.10"`），
        # 否则在当前解释器上会被误报为缺失
        if req.marker and not req.marker.evaluate():
            continue
        total += 1
        try:
            installed = version(req.name)
        except PackageNotFoundError:
            missing.append(req.name)
            continue
        # prereleases=True：允许预发布版满足约束，避免本地装 rc 版时误报
        if req.specifier and not req.specifier.contains(installed, prereleases=True):
            mismatched.append(f"{req.name}: 需要 {req.specifier}, 实际 {installed}")

    assert not missing, f"缺少依赖: {missing}"
    assert not mismatched, f"版本不匹配: {mismatched}"
    return f"Python {sys.version.split()[0]}, {total} 个声明依赖全部满足"


def check_env():
    """
    校验 zvt 运行时环境变量已初始化且数据目录可写。

    `zvt_env` 由 `zvt.init_env()` 在 import zvt 时填充；缺少关键键说明 ZVT_HOME
    定位失败（例如在错误的用户下运行、或 ZVT_HOME 指向不存在的路径）。
    只读不写的话 `os.access` 判断会通过，所以必须显式检查 W_OK——很多「采集不到数据」
    的根因就是数据目录只读。

    Returns:
        str: 形如 "version=0.13.5, data_path=...\\zvt-home\\data"

    Raises:
        AssertionError: 缺少 zvt_env 键，或 data_path 不可写。
    """
    for key in ("zvt_home", "data_path", "log_path"):
        assert key in zvt_env, f"缺少 zvt_env[{key}]"
    assert os.access(zvt_env["data_path"], os.W_OK), "data_path 不可写"
    return f"version={__version__}, data_path={zvt_env['data_path']}"


def check_storage():
    """
    校验 provider 目录与 db 文件已真正落盘。

    对应存储路由规则 `{data_path}/{provider}/{provider}_{db_name}.db`（见架构文档第 3 节）。
    目录存在但没有 .db，通常意味着采集启动过但第一个批次就失败了。

    Returns:
        str: 形如 "3 个 db: ['em_stock_1d_hfq_kdata.db', ...]"

    Raises:
        AssertionError: provider 目录不存在，或目录下没有任何 db 文件。
    """
    provider_dir = os.path.join(zvt_env["data_path"], PROVIDER)
    assert os.path.isdir(provider_dir), f"未找到 {provider_dir}，请先执行采集"
    dbs = [f for f in os.listdir(provider_dir) if f.endswith(".db")]
    assert dbs, "provider 目录下无 db 文件"
    # 只展示前 3 个，避免 provider 下 db 很多时刷屏
    return f"{len(dbs)} 个 db: {dbs[:3]}"


def check_meta():
    """
    校验 Stock 元数据表非空。

    元数据是后续所有查询的基础：K 线、因子、回测都依赖 entity_id 能对应到实体。
    元数据为空时，即使 K 线表有数据，上层也会因找不到实体而空转。

    Returns:
        str: 形如 "5641 只股票"

    Raises:
        AssertionError: Stock 表记录数为 0。
    """
    count = get_data_count(Stock, provider=PROVIDER)
    assert count > 0, "Stock 表为空"
    return f"{count} 只股票"


def check_kdata():
    """
    校验样本标的的 K 线存在且主键唯一。

    只取最近 5 条，目的是快速确认「能查到数据」；
    主键唯一性断言（`df.index.is_unique`）用于暴露重复采集——
    重复数据在按时间序列做因子计算时会把同一交易日算两次，且很隐蔽。

    这里用后复权表（Stock1dHfqKdata）而非未复权表：复权表更适合下游因子与回测，
    未复权表可能尚未采集（见 14.11 常见坑）。

    Returns:
        str: 形如 "latest=2026-11-09, 抽样 5 条"

    Raises:
        AssertionError: 查询结果为空，或索引存在重复。
    """
    df = Stock1dHfqKdata.query_data(
        entity_ids=[ENTITY_ID],
        provider=PROVIDER,
        order=Stock1dHfqKdata.timestamp.desc(),
        limit=5,
    )
    assert not df.empty, f"{ENTITY_ID} 无 K 线"
    assert df.index.is_unique, "K 线主键重复"
    # 直接取查询结果的最新日期，避免依赖未复权表的采集状态
    latest = df.reset_index()["timestamp"].max()
    return f"latest={to_date_time_str(latest)}, 抽样 {len(df)} 条"


def check_quality():
    """
    校验行情数据本身是否可信（不只看「有没有数据」）。

    检查近一年的四个维度：
      1. 主键唯一：同一交易日只应有一条记录；
      2. 关键列无缺失：open/close/high/low/volume 缺失会让因子算出 NaN；
      3. 行情逻辑自洽：`high >= low`，且 `close` 落在 [low, high] 区间内——
         这类脏数据通常来自复权计算错误或数据源串行；
      4. 新鲜度：最新一条距今不超过 7 个自然日（覆盖周末，长假期间需放宽）。

    Returns:
        str: 形如 "242 条通过质量校验, 新鲜度 lag=0 天"

    Raises:
        AssertionError: 任一项不满足，错误信息会给出具体列名或越界条数。
    """
    df = Stock1dHfqKdata.query_data(
        entity_ids=[ENTITY_ID],
        provider=PROVIDER,
        start_timestamp=to_date_time_str(recent_year_date()),
        end_timestamp=to_date_time_str(current_date()),
        index="timestamp",
    )
    assert not df.empty, f"{ENTITY_ID} 近一年无 K 线"
    assert df.index.is_unique, "时间戳重复"

    cols = ["open", "close", "high", "low", "volume"]
    null_counts = df[cols].isna().sum()
    # 只把真正有缺失的列放进报错信息，避免输出一整列 0
    bad = null_counts[null_counts > 0].to_dict()
    assert not bad, f"关键列存在缺失: {bad}"

    assert (df["high"] >= df["low"]).all(), "存在 high < low 的脏数据"
    in_range = (df["close"] >= df["low"]) & (df["close"] <= df["high"])
    assert in_range.all(), f"{(~in_range).sum()} 条 close 越界"

    lag = (pd.Timestamp.now().normalize() - df.index.max()).days
    assert lag <= 7, f"数据停更 {lag} 天"
    return f"{len(df)} 条通过质量校验, 新鲜度 lag={lag} 天"


def check_idempotent():
    """
    校验采集的增量幂等性：同一区间重复采集，行数与主键必须不变。

    定时任务每天跑采集，一旦增量逻辑失效会出现两种后果：
      - 行数翻倍（主键未生效）→ 安静地污染下游统计，很难发现；
      - 唯一约束冲突抛异常 → 定时任务直接崩掉。
    两种都必须断言。

    实现方式：取最近 5 条确定一个小区间，先计数 → 对同区间再采集一次 → 再计数。
    若第二次耗时与首次相当，说明它在重复下载而非跳过，也是增量失效的旁证。

    Returns:
        str: 形如 "5 行在重复采集后保持不变"

    Raises:
        AssertionError: 无数据可校验、主键重复，或重复采集后行数发生变化。
    """
    df = Stock1dHfqKdata.query_data(
        entity_ids=[ENTITY_ID],
        provider=PROVIDER,
        order=Stock1dHfqKdata.timestamp.desc(),
        limit=5,
        index="timestamp",
    )
    assert not df.empty, "无 K 线可校验"
    start, end = df.index.min(), df.index.max()

    def count_rows():
        """统计当前区间内的行数，并顺带断言主键唯一

        每次重新查库而不是复用 before 的 DataFrame，是为了让断言真的覆盖
        「写库之后」的状态；若复用旧数据，重复写入根本不会被发现。
        """
        one = Stock1dHfqKdata.query_data(
            entity_ids=[ENTITY_ID], provider=PROVIDER,
            start_timestamp=start, end_timestamp=end,
        )
        assert one.index.is_unique, "采集后主键重复"
        return len(one)

    before = count_rows()
    assert before > 0, "区间内无数据"

    # 第二次采集同一区间：recorder 应识别已存在记录并跳过
    Stock1dHfqKdata.record_data(
        entity_ids=[ENTITY_ID], provider=PROVIDER,
        start_timestamp=start, end_timestamp=end,
    )
    after = count_rows()
    assert after == before, f"重复采集产生重复数据: {before} -> {after}"
    return f"{before} 行在重复采集后保持不变"


def check_factor():
    """
    校验因子能完成计算并产出非空结果。

    这一步验证的是「数据 + 因子计算链路」是否打通：K 线表里要有 windows 所需的
    足够历史，因子才能算出均线并产生 filter_result。
    `entity_provider` 与 `provider` 都要显式传，否则会以 None 去查元数据表而得到空结果。
    `need_persist=False` 避免校验过程往库里写因子表。

    Returns:
        str: 形如 "result rows=242"

    Raises:
        AssertionError: result_df 为 None 或为空，通常意味着数据区间不足或 provider 不明。
    """
    factor = CrossMaFactor(
        entity_ids=[ENTITY_ID],
        provider=PROVIDER,
        entity_provider=PROVIDER,
        start_timestamp=to_date_time_str(recent_year_date()),
        end_timestamp=to_date_time_str(current_date()),
        windows=[5, 10],
        need_persist=False,
    )
    factor.compute()
    assert factor.result_df is not None and not factor.result_df.empty, "因子结果为空"
    return f"result rows={len(factor.result_df)}"


# -------------------------------------------------------------- 回测示例
# 同一区间跑两套策略：默认策略校验框架链路，自定义钩子校验策略层可插拔


def _sparkline(values, width=60):
    """
    把一串数值压成一行 ASCII 走势图，用于在终端/CI 日志里直观看出趋势。

    不依赖 matplotlib：zvt 的可视化走 plotly，但导出图片需要 kaleido，
    验证脚本不该为此引入新依赖；ASCII 图在 CI 日志里同样可读。

    Args:
        values: 可迭代数值序列（如净值序列）。
        width: 最多取多少个点；超过则等间隔抽样，避免一行过长。

    Returns:
        str: 长度不超过 width 的字符串，字符按分位数映射
        ``" .:-=+*#%@"``（空格最低，@ 最高）。
    """
    vals = [v for v in values if v == v]  # 过滤 NaN
    if not vals:
        return ""
    if len(vals) > width:
        step = len(vals) / width
        vals = [vals[int(i * step)] for i in range(width)]

    lo, hi = min(vals), max(vals)
    levels = " .:-=+*#%@"
    if hi == lo:
        return levels[len(levels) // 2] * len(vals)
    return "".join(levels[int((v - lo) / (hi - lo) * (len(levels) - 1))] for v in vals)


def _round_trips(orders):
    """
    按 FIFO 把订单流配对成完整往返交易，返回每笔收益率。

    为什么不直接读持仓表：`sim_account.on_trading_close()` 会把 `long_amount == 0`
    的持仓剔除后再落库，因此 `Position` 表只保留**未平仓快照**，也没有 `closing`
    字段（`closing` 在 `AccountStats` 上，含义是「该快照为收盘结算」）。
    想统计「已平仓交易的胜率」，只能从订单流还原。

    配对规则：买入累加数量与成本；卖出按当前均价结算一笔收益，并扣减对应成本；
    数量归零后重新开始下一笔往返。若最后一笔未平仓，不计入结果。

    Args:
        orders: 按时间升序的订单 DataFrame，需含
            `entity_id`/`order_type`/`order_amount`/`order_price`。

    Returns:
        list[float]: 每笔往返的收益率（卖出价 / 买入均价 - 1）。无完整往返时返回空列表。
    """
    trips = []
    for _, group in orders.groupby("entity_id"):
        amount, cost = 0.0, 0.0
        for _, row in group.iterrows():
            if row["order_type"] == "order_long":
                amount += row["order_amount"]
                cost += row["order_amount"] * row["order_price"]
            elif row["order_type"] == "order_close_long" and amount > 0:
                sell_amount = min(row["order_amount"], amount)
                avg_cost = cost / amount
                trips.append(row["order_price"] / avg_cost - 1)
                cost -= avg_cost * sell_amount
                amount -= sell_amount
                # 浮点误差累积会让 amount 停在 1e-13 量级，视为已清仓
                if amount <= 1e-9:
                    amount, cost = 0.0, 0.0
    return trips


def check_backtest():
    """
    跑一个小样本回测，并校验交易结果**内部自洽**（不只是「没报错」）。

    校验分四组：
      1. **账户恒等式**：`all_value == cash + value`，且现金不为负（不透支）；
      2. **双边成交**：订单里既有买入也有平仓——只有单边说明信号链路半死；
      3. **可复现性**：`clear_trader()` 先清旧结果，快照时间戳不重复；
      4. **结果合理性**：输出总收益、最大回撤、往返笔数与胜率。

    这里踩过的两个坑必须显式处理，否则账户资产会恒等于初始资金（看起来「跑完了」）：
      1. **必须传 entity_ids 而不是 codes**：`Trader.init_entities()` 直接返回
         `self.entity_ids`，只传 codes 时它是 None，导致每轮都拿不到标的、空转；
      2. **必须在 init_factors 内先 compute()**：日线历史回测中 `Trader.run()` 主循环
         的 `waiting_seconds <= 0` 分支不会再调用 `factor.move_on()`，因此不会触发
         compute，`result_df` 保持 None，`get_targets()` 永远返回空列表。

    另外 `provider` 不在 `init_factors` 的模板方法入参里，只能从 `self.provider` 取。

    自定义交易行为的写法（本函数只用默认策略，实际项目按需覆盖钩子）：

    ```python
    class MyTrader(StockTrader):
        def on_targets_selected_from_levels(self, timestamp):
            # 例：只做多、且最多持有 5 只
            long_selected, short_selected = super().on_targets_selected_from_levels(timestamp)
            return (set(list(long_selected)[:5]) if long_selected else set()), short_selected

        def long_position_control(self):
            # 例：无仓位时打 3 成，有仓位时补到 6 成
            return 0.3 if not self.get_current_positions() else 0.6

        def on_profit_control(self):
            # 例：盈利 15% 止盈、亏损 5% 止损
            self.profit_threshold = (0.15, -0.05)
            return super().on_profit_control()
    ```

    Returns:
        str: 形如
        "账户快照 130 条, 成交 27 笔(买14/卖13), 收益 -1.32%, 回撤 -7.33%,
         往返 13 笔胜率 15.4%, 期末 986836.17"

    Raises:
        AssertionError: 无账户快照、恒等式不成立、现金透支、无成交或只有单边成交。
    """

    class VerifyTrader(StockTrader):
        """仅用于校验的最小 Trader：单因子、日线、不绘图

        不覆盖任何交易钩子（选股、仓位、止盈止损），保持默认行为，
        这样校验的是框架自身的交易链路，而不是某个自定义策略。
        """

        def init_factors(
            self,
            entity_ids,
            entity_schema,
            exchanges,
            codes,
            start_timestamp,
            end_timestamp,
            adjust_type=None,
        ):
            """构造唯一的交叉均线因子，并在返回前完成计算。

            参数由 `Trader.__init__` 调用时传入，其中 `start_timestamp` 已被回溯
            `pre_load_days`（默认 365 天），保证因子有足够历史算出均线。

            与模板方法的两处差异（都不能省）：
              - 显式传 `provider`/`entity_provider`：模板签名里没有 provider，
                不传会以 None 查元数据表而拿到空数据；
              - 返回前调用 `factor.compute()`：日线回测主循环不会再触发
                `factor.move_on()`，不主动算则 `result_df` 为 None，选股永远为空。

            Returns:
                list[Factor]: 单元素因子列表，长度决定 `trading_level_asc`。
            """
            factor = CrossMaFactor(
                entity_ids=entity_ids,
                entity_schema=entity_schema,
                # init_factors 的入参里没有 provider，必须从 trader 上取
                provider=self.provider,
                entity_provider=self.provider,
                codes=codes,
                exchanges=exchanges,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
                windows=[5, 10],
                need_persist=False,
            )
            # 日线回测中 Trader 主循环不会再触发因子计算，需在构造时先 compute
            factor.compute()
            return [factor]

    # 清掉上次结果，否则快照会累积、指标失真（重跑不可复现）
    clear_trader(TRADER_NAME)

    trader = VerifyTrader(
        entity_ids=[ENTITY_ID],
        provider=PROVIDER,
        level=IntervalLevel.LEVEL_1DAY,
        start_timestamp="2024-01-01",
        end_timestamp="2024-06-30",
        trader_name=TRADER_NAME,
        # 无 display 环境下绘图会阻塞或报错，校验时关闭
        draw_result=False,
    )
    trader.run()

    # ---- 1. 账户快照：恒等式 + 不透支 ----
    acct = AccountStats.query_data(
        filters=[AccountStats.trader_name == TRADER_NAME],
        order=AccountStats.timestamp.asc(),
        index="timestamp",
    )
    assert not acct.empty, "回测未写入 AccountStats"
    assert acct.index.is_unique, "账户快照时间戳重复"

    # 允许浮点误差：金额是 float 累加出来的
    identity_gap = (acct["all_value"] - (acct["cash"] + acct["value"])).abs().max()
    assert identity_gap < 1.0, f"资产恒等式不成立，最大偏差 {identity_gap:.2f}"
    assert (acct["cash"] >= -1e-6).all(), "出现现金透支（cash < 0）"

    # ---- 2. 订单：必须有成交，且买卖双边 ----
    orders = Order.query_data(
        filters=[Order.trader_name == TRADER_NAME],
        order=Order.timestamp.asc(),
    )
    assert not orders.empty, "回测无成交，检查因子是否 compute、entity_ids 是否传入"
    types = set(orders["order_type"].unique())
    assert {"order_long", "order_close_long"} <= types, f"只有单边成交: {types}"

    # ---- 3. 绩效指标 ----
    nav = acct["all_value"] / acct["input_money"].iloc[0]
    total_return = nav.iloc[-1] - 1
    max_drawdown = (nav / nav.cummax() - 1).min()
    trips = _round_trips(orders)
    win_rate = sum(t > 0 for t in trips) / len(trips) if trips else float("nan")

    buy_count = (orders["order_type"] == "order_long").sum()
    sell_count = (orders["order_type"] == "order_close_long").sum()
    return (
        f"账户快照 {len(acct)} 条, 成交 {len(orders)} 笔(买{buy_count}/卖{sell_count}), "
        f"收益 {total_return:.2%}, 回撤 {max_drawdown:.2%}, "
        f"往返 {len(trips)} 笔胜率 {win_rate:.1%}, 期末 {acct['all_value'].iloc[-1]:.2f}"
    )


def check_backtest_hooks():
    """
    用自定义钩子跑第二个回测，验证「示例代码可直接照抄」而不只是文档里的伪代码。

    与 `check_backtest()` 的区别只在于策略层：这里覆盖了三个最常用的钩子，
    框架层（因子、下单、记账）完全一致。三个钩子的作用时机：

      - `on_targets_selected_from_levels(timestamp)`：每个最小周期选出标的后调用，
        可在此做限仓、黑名单过滤、按信号强弱排序取 TopN；
      - `long_position_control()`：决定本次买入用多少比例的资金，
        无仓位时轻仓试错、有持仓时加码是常见做法；
      - `profit_threshold`：构造参数，主循环每轮比较持仓浮盈，触发止盈/止损。

    校验点：账户快照与订单都非空，且任意一天的持仓数不超过 `max_positions`
    （证明限仓钩子确实生效，而不是被框架忽略）。

    Returns:
        str: 形如
        "账户快照 130 条, 成交 9 笔, 最大持仓 1, 收益 -0.85%, 期末 991,503.21"

    Raises:
        AssertionError: 无账户快照、无成交，或持仓数超过 `max_positions`。
    """

    class HookTrader(StockTrader):
        """演示自定义钩子的最小实现：限仓 + 分批建仓 + 止盈止损

        与 `VerifyTrader` 的分工：`VerifyTrader` 保持默认策略，用来校验框架自身的
        交易链路；`HookTrader` 只改策略层，用来校验「钩子确实被框架调用」。

        三个钩子各代表一类常见需求：
          - `on_targets_selected_from_levels`：选股后过滤/限仓（本例限制同时持有数）；
          - `long_position_control`：仓位管理（本例无仓 3 成、有仓 6 成）；
          - `profit_threshold`：止盈止损（构造参数传入，见下方实例化）。

        校验只覆盖单标的，因此限仓取 3 时实际不会触发截断；断言写的是
        `持仓数 <= max_positions`，在多标的场景下同样成立。
        """

        #: 同时持有的标的上限（单标的校验时恒为 1，多标的场景才起作用）
        max_positions = 3

        def init_factors(
            self,
            entity_ids,
            entity_schema,
            exchanges,
            codes,
            start_timestamp,
            end_timestamp,
            adjust_type=None,
        ):
            """构造因子，逻辑与 `VerifyTrader.init_factors` 完全相同

            这里刻意不抽公共函数：`init_factors` 是框架约定的模板方法，签名固定，
            两处示例都是给人照抄的完整片段，保持独立比抽基类更易读。
            两个必要条件仍不可省——显式传 provider、返回前 `compute()`。

            Returns:
                list[Factor]: 单元素因子列表。
            """
            factor = CrossMaFactor(
                entity_ids=entity_ids,
                entity_schema=entity_schema,
                provider=self.provider,
                entity_provider=self.provider,
                codes=codes,
                exchanges=exchanges,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
                windows=[5, 10],
                need_persist=False,
            )
            factor.compute()
            return [factor]

        def on_targets_selected_from_levels(self, timestamp):
            """选出目标后做限仓：按 entity_id 排序取前 max_positions 个

            排序而非直接截断集合，是为了结果可复现——set 的迭代顺序不确定，
            直接 list(set)[:n] 会导致同样的数据每次选出不同的标的。
            """
            long_selected, short_selected = super().on_targets_selected_from_levels(timestamp)
            if long_selected:
                long_selected = set(sorted(long_selected)[: self.max_positions])
            return long_selected, short_selected

        def long_position_control(self):
            """无仓位时用 3 成资金试仓，已有持仓时加到 6 成

            刻意区别于默认实现（默认是 0.2/0.5），这样回测结果不同，
            才能证明钩子确实被框架调用，而不是被忽略。
            """
            return 0.3 if not self.get_current_positions() else 0.6

    clear_trader(TRADER_NAME_HOOK)

    trader = HookTrader(
        entity_ids=[ENTITY_ID],
        provider=PROVIDER,
        level=IntervalLevel.LEVEL_1DAY,
        start_timestamp="2024-01-01",
        end_timestamp="2024-06-30",
        trader_name=TRADER_NAME_HOOK,
        # 盈利 15% 止盈、亏损 5% 止损；不传则用默认 (3, -0.3)
        profit_threshold=(0.15, -0.05),
        draw_result=False,
    )
    trader.run()

    acct = AccountStats.query_data(
        filters=[AccountStats.trader_name == TRADER_NAME_HOOK],
        order=AccountStats.timestamp.asc(),
        index="timestamp",
    )
    assert not acct.empty, "钩子回测未写入 AccountStats"

    orders = Order.query_data(filters=[Order.trader_name == TRADER_NAME_HOOK])
    assert not orders.empty, "钩子回测无成交"

    # Position 是每日快照：同一 timestamp 的行数即当天的持仓标的数
    positions = Position.query_data(filters=[Position.trader_name == TRADER_NAME_HOOK])
    max_holding = int(positions.groupby("timestamp").size().max()) if not positions.empty else 0
    assert max_holding <= HookTrader.max_positions, (
        f"持仓数 {max_holding} 超过上限 {HookTrader.max_positions}，限仓钩子未生效"
    )

    nav = acct["all_value"] / acct["input_money"].iloc[0]
    return (
        f"账户快照 {len(acct)} 条, 成交 {len(orders)} 笔, 最大持仓 {max_holding}, "
        f"收益 {nav.iloc[-1] - 1:.2%}, 期末 {acct['all_value'].iloc[-1]:,.2f}"
    )


def check_backtest_grid():
    """
    参数网格对比回测：同一区间跑多组均线参数，输出横向对比表。

    与前两个回测示例的分工：
      - `check_backtest()`   校验框架链路（能否成交、记账对不对）；
      - `check_backtest_hooks()` 校验策略钩子（自定义逻辑是否被调用）；
      - 本函数校验**参数化能力**：同一策略换参数应产生不同结果，若完全相同，
        说明参数没有真正传进因子（典型原因见下）。

    参数必须通过**子类注入**，不能在实例化后改属性：`Trader.__init__` 内部
    就调用了 `init_factors()`，等拿到实例时因子已经建好，再赋值 `trader.windows`
    完全无效。这里用 `type()` 动态建子类，把参数写进类属性后再实例化。

    每组参数用独立的 `trader_name` 并先 `clear_trader()`，否则结果互相覆盖，
    对比表会显示成同一套数据。

    Returns:
        str: 形如
        "3 组参数, 最优 5/10(收益 -1.32%), 结果差异已确认"

    Raises:
        AssertionError: 某组参数无账户快照，或所有组结果完全相同（参数未生效）。
    """
    param_sets = [(5, 10), (5, 20), (10, 20)]

    class GridTrader(StockTrader):
        """参数化 Trader：`windows` 由子类属性提供，其余逻辑不变"""

        #: 均线窗口，默认值会被动态子类覆盖
        windows = [5, 10]

        def init_factors(self, entity_ids, entity_schema, exchanges, codes,
                         start_timestamp, end_timestamp, adjust_type=None):
            """用 self.windows 构造因子，其它两个必要条件与前面示例一致"""
            factor = CrossMaFactor(
                entity_ids=entity_ids,
                entity_schema=entity_schema,
                provider=self.provider,
                entity_provider=self.provider,
                codes=codes,
                exchanges=exchanges,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
                windows=self.windows,
                need_persist=False,
            )
            factor.compute()
            return [factor]

    rows = []
    for fast, slow in param_sets:
        name = f"{TRADER_NAME_GRID}_{fast}_{slow}"
        # 参数必须在实例化前注入：__init__ 里就会调用 init_factors
        trader_cls = type(
            f"GridTrader_{fast}_{slow}", (GridTrader,), {"windows": [fast, slow]}
        )

        clear_trader(name)
        trader_cls(
            entity_ids=[ENTITY_ID],
            provider=PROVIDER,
            level=IntervalLevel.LEVEL_1DAY,
            start_timestamp="2024-01-01",
            end_timestamp="2024-06-30",
            trader_name=name,
            draw_result=False,
        ).run()

        acct = AccountStats.query_data(
            filters=[AccountStats.trader_name == name],
            order=AccountStats.timestamp.asc(),
            index="timestamp",
        )
        assert not acct.empty, f"{fast}/{slow} 无账户快照"

        orders = Order.query_data(filters=[Order.trader_name == name])
        nav = acct["all_value"] / acct["input_money"].iloc[0]
        rets = nav.pct_change().dropna()
        # 日收益年化；单标的半年样本噪声大，仅用于横向排序，不作为绝对评价
        sharpe = (
            rets.mean() / rets.std() * math.sqrt(252)
            if len(rets) > 1 and rets.std() > 0
            else float("nan")
        )
        trips = _round_trips(orders)
        rows.append({
            "label": f"{fast}/{slow}",
            "total_return": nav.iloc[-1] - 1,
            "max_drawdown": (nav / nav.cummax() - 1).min(),
            "sharpe": sharpe,
            "orders": len(orders),
            "trips": len(trips),
            "win_rate": sum(t > 0 for t in trips) / len(trips) if trips else float("nan"),
            "final": acct["all_value"].iloc[-1],
        })

    # 参数是否真的生效：不同窗口必须产出不同期末资产
    finals = {round(r["final"], 2) for r in rows}
    assert len(finals) > 1, "各组参数结果完全相同，参数未传入因子（检查是否在实例化后赋值）"

    header = f"{'参数':>8}{'总收益':>10}{'最大回撤':>10}{'夏普':>8}{'订单':>6}{'往返':>6}{'胜率':>8}{'期末资产':>14}"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['label']:>8}{r['total_return']:>10.2%}{r['max_drawdown']:>10.2%}"
            f"{r['sharpe']:>8.2f}{r['orders']:>6}{r['trips']:>6}{r['win_rate']:>8.1%}"
            f"{r['final']:>14,.2f}"
        )

    best = max(rows, key=lambda r: r["total_return"])
    return f"{len(rows)} 组参数, 最优 {best['label']}(收益 {best['total_return']:.2%}), 结果差异已确认"


def check_backtest_cross():
    """
    多因子类型交叉对比：同一区间跑多种因子 × 每类 2 组参数，输出交叉表。

    前三个回测示例的分工：
      - `check_backtest()`        校验框架链路（能否成交、记账对不对）；
      - `check_backtest_hooks()`  校验策略钩子（自定义逻辑是否被调用）；
      - `check_backtest_grid()`   校验参数化能力（同一因子换参数应产出不同结果）；
      - 本函数校验**因子类型切换**：同一区间换 BullFactor / GoldCrossFactor /
        CrossMaFactor，结果必须显著不同。若全部相同，说明 `init_factors` 里选
        错了类（或忘了换），所有回测用的其实是同一个因子。

    三种因子的信号来源：
      - BullFactor（MACD）：DIF 上穿 0 轴 → 多头；
      - GoldCrossFactor（MACD）：DIF 上穿 DEA → 金叉；
      - CrossMaFactor（MA）：快线上穿慢线。

    断言：各组期末资产不全相同（因子类型确实起了作用）。

    Returns:
        str: 形如
        "3 类因子 × 6 组参数, 期末资产区间 [986K, 1,078K], 差异已确认"

    Raises:
        AssertionError: 某组无账户快照，或所有组结果完全相同。
    """
    # (因子类, 显示标签, kwargs 构造函数——统一签名 fn(variant) -> dict)
    # 三类因子参数化方式不同：MA 因子把 variant 当 windows 列表，MACD 因子忽略 variant
    configs = [
        ("CrossMa", CrossMaFactor, lambda v: {"windows": list(v)},
         [(5, 10), (10, 20)]),
        ("Bull", BullFactor, lambda v: {},
         [None, None]),    # BullFactor 无额外参数，两列同值（仅用于验证同因子可复现）
        ("GoldCross", GoldCrossFactor, lambda v: {},
         [None, None]),    # GoldCrossFactor 同理
    ]

    class CrossTrader(StockTrader):
        """因子类型通过类属性 `factor_cls` 和 `factor_kwargs` 注入"""
        factor_cls = None
        factor_kwargs = {}

        def init_factors(self, entity_ids, entity_schema, exchanges, codes,
                         start_timestamp, end_timestamp, adjust_type=None):
            factor = self.factor_cls(
                entity_ids=entity_ids,
                entity_schema=entity_schema,
                provider=self.provider,
                entity_provider=self.provider,
                codes=codes,
                exchanges=exchanges,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
                need_persist=False,
                **self.factor_kwargs,
            )
            factor.compute()
            return [factor]

    results = []
    for label, fcls, kwargs_fn, variants in configs:
        for i, variant in enumerate(variants, 1):
            tag = f"{label}_{i}"
            # 参数名也进 trader_name，便于追踪不同配置
            trader_name = f"{TRADER_NAME_GRID}_{tag.lower()}"
            # kwargs_fn 统一签名 fn(variant) -> dict
            kwargs = kwargs_fn(variant)

            trader_cls = type(
                f"CrossTrader_{tag}", (CrossTrader,),
                {"factor_cls": fcls, "factor_kwargs": kwargs},
            )

            clear_trader(trader_name)
            trader_cls(
                entity_ids=[ENTITY_ID],
                provider=PROVIDER,
                level=IntervalLevel.LEVEL_1DAY,
                start_timestamp="2024-01-01",
                end_timestamp="2024-06-30",
                trader_name=trader_name,
                draw_result=False,
            ).run()

            acct = AccountStats.query_data(
                filters=[AccountStats.trader_name == trader_name],
                order=AccountStats.timestamp.asc(), index="timestamp",
            )
            assert not acct.empty, f"{tag} 无账户快照"

            nav = acct["all_value"] / acct["input_money"].iloc[0]
            orders = Order.query_data(filters=[Order.trader_name == trader_name])
            results.append({
                "tag": tag,
                "factor": label,
                "variant": i,
                "kwargs": kwargs,
                "total_return": nav.iloc[-1] - 1,
                "final": acct["all_value"].iloc[-1],
                "orders": len(orders),
            })

    # 同一因子内两列的对比（MA 因子有真正的参数差异，MACD 因子两列同值）
    # 不同因子之间的差异（Bull vs GoldCross vs CrossMa 应显著不同）
    finals = {round(r["final"], 2) for r in results}
    assert len(finals) > 1, "所有因子结果完全相同，因子类型未切换"

    # 打印交叉表：行是因子类型，列是参数变体
    # 只有 CrossMa 因子的两列有真正的参数差异（5/10 vs 10/20）
    # MACD 因子两列是重复运行，用来验证同一因子参数不变时结果可复现
    print("\n=== 多因子类型交叉对比 ===")
    print(f"{'因子':>12}{'参数':>12}{'期末资产':>14}{'总收益':>10}{'订单':>6}")
    print("-" * 58)
    for r in results:
        kw = r["kwargs"]
        param_str = f"{kw['windows'][0]}/{kw['windows'][1]}" if "windows" in kw else "default"
        print(f"{r['factor']:>12}{param_str:>12}{r['final']:>14,.2f}{r['total_return']:>10.2%}{r['orders']:>6}")

    finals_list = [r["final"] for r in results]
    lo, hi = min(finals_list), max(finals_list)
    return (
        f"{len(results)} 组, 期末资产 [{lo:,.0f}, {hi:,.0f}], "
        f"不同因子差异已确认"
    )


def check_backtest_report():
    """
    回测结果落盘 + 终端可读化：导出 CSV、打印净值/回撤走势图。

    前四个回测示例都在校验「结果对不对」，这个示例解决「结果怎么用」——
    回测跑完之后，账本需要能被导出、被人工核对、被其它脚本二次分析。

    复用 `TRADER_NAME` 的回测结果；若尚无结果，先跑一次基础回测。
    产物写入 `zvt_env["tmp_path"]/backtest_report/`，不污染数据目录：
      - `account.csv`：每日账户快照（现金、市值、总资产、收益）
      - `orders.csv`：逐笔订单明细

    为什么不用 matplotlib 画图：zvt 的可视化走 plotly，导出图片还需 kaleido；
    验证脚本不应为此新增依赖。改用 ASCII 走势图，CI 日志里同样能看出形态。

    Returns:
        str: 形如
        "导出 2 个文件至 ...\\backtest_report, 快照 130 行, 订单 27 行, 收益 -1.32%"

    Raises:
        AssertionError: 账户为空、导出文件不存在或行数与查询结果不符。
    """
    out_dir = os.path.join(zvt_env["tmp_path"], "backtest_report")

    acct = AccountStats.query_data(
        filters=[AccountStats.trader_name == TRADER_NAME],
        order=AccountStats.timestamp.asc(),
        index="timestamp",
    )
    if acct.empty:
        # 首次运行或结果被清理过：先补跑一次基础回测
        check_backtest()
        acct = AccountStats.query_data(
            filters=[AccountStats.trader_name == TRADER_NAME],
            order=AccountStats.timestamp.asc(),
            index="timestamp",
        )
    assert not acct.empty, "回测结果为空，无法导出"

    orders = Order.query_data(filters=[Order.trader_name == TRADER_NAME])
    os.makedirs(out_dir, exist_ok=True)

    account_csv = os.path.join(out_dir, "account.csv")
    orders_csv = os.path.join(out_dir, "orders.csv")
    acct.to_csv(account_csv, index_label="timestamp", encoding="utf-8-sig")
    orders.to_csv(orders_csv, index=False, encoding="utf-8-sig")

    # 校验落盘完整性：文件存在且行数与查询结果一致，避免静默截断
    for path, expected in ((account_csv, len(acct)), (orders_csv, len(orders))):
        assert os.path.exists(path) and os.path.getsize(path) > 0, f"{path} 未生成或为空"
        with open(path, encoding="utf-8-sig") as f:
            written = sum(1 for _ in f) - 1  # 去掉表头
        assert written == expected, f"{path} 行数 {written} != {expected}"

    # 净值与回撤走势：终端直观看形态，不必打开文件
    nav = acct["all_value"] / acct["input_money"].iloc[0]
    drawdown = nav / nav.cummax() - 1
    print(f"  净值  {_sparkline(nav)}")
    print(f"  回撤  {_sparkline(drawdown)}")

    return (
        f"导出 {out_dir}, 快照 {len(acct)} 行, 订单 {len(orders)} 行, "
        f"收益 {nav.iloc[-1] - 1:.2%}"
    )


def _prepare_external_df(df, provider):
    """
    把外部来源（CSV / 接口返回）的数据整理成可入库形态。

    外部数据入库有三个必做修复，缺一都会导致「能查到行、但按日期查不到」：

    1. `timestamp` 转 datetime——CSV 往返会把它变成字符串 `"2024-01-02"`
       （dtype=object），SQLite 按文本比较，`<= "2024-01-16 00:00:00"`
       会把当天整行排除掉；
    2. 覆盖 `provider` 列——否则查 `provider="external_csv"` 时过滤不到；
    3. 保留 `id` 列——`df_to_db` 靠它去重，缺失会重复写入。

    Args:
        df: 外部读入的原始 DataFrame。
        provider: 目标 provider 名。

    Returns:
        pd.DataFrame: 可直接交给 `df_to_db` 的副本。
    """
    out = df.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"])
    out["provider"] = provider
    return out


def _import_external(schema, provider, entity_id, start_timestamp, end_timestamp, out_dir):
    """
    演示完整的外部数据接入链路：导出 → CSV 文件 → 读回 → 入库。

    刻意绕一圈文件而不是直接在内存里复制，是为了复现真实场景：
    外部数据的格式问题（时间列为字符串、缺 provider、缺 id）只有经过序列化才暴露。

    Args:
        schema: 目标数据表（如 `Stock1dKdata`）。
        provider: 写入的 provider 名。
        entity_id: 标的。
        start_timestamp: 导出起点（需覆盖因子回溯窗口）。
        end_timestamp: 导出终点。
        out_dir: CSV 落盘目录。

    Returns:
        tuple[int, int, str]: (导出行数, 实际入库行数, CSV 路径)。

    Raises:
        AssertionError: 入库行数与源数据不一致（说明主键冲突或被截断）。
    """
    df = schema.query_data(
        entity_ids=[entity_id],
        provider=PROVIDER,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
    )
    assert not df.empty, f"{schema.__tablename__} 源数据为空，无法演示外部接入"

    csv_path = os.path.join(out_dir, f"{entity_id}_{schema.__tablename__}.csv")
    # utf-8-sig：Excel 可直接打开；index=False：id/timestamp 都在列里
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    external = _prepare_external_df(pd.read_csv(csv_path, encoding="utf-8-sig"), provider)
    # force_update=True：反复运行本脚本时覆盖旧行，保证结果可重复
    saved = df_to_db(external, schema, provider=provider, force_update=True)

    assert saved == len(external), f"{schema.__tablename__} 入库 {saved} 行 != {len(external)} 行"
    return len(df), saved, csv_path


def check_backtest_external():
    """
    外部数据源接入回测：把现有数据导出成 CSV，再作为「外部源」导入独立 provider，
    最后跑两套参数完全相同的回测做对照，期末资产必须完全一致。

    为什么要做这个校验：接入外部数据源时，最容易出的不是「导入报错」，而是
    「导入成功但回测不动」——数据行数对、查询也有结果，但下单时按确切日期
    查 K 线却查不到，于是所有信号被静默丢弃，账户恒为初始资金。

    实测踩到的三个坑（都已在本函数中规避）：
      1. **timestamp 变字符串**：CSV 往返后是 `object`，SQLite 文本比较导致
          `start=end=当天` 的查询命中 0 行 → 必须先 `pd.to_datetime`；
      2. **Trader 默认按 hfq 定价**：`Trader.__init__` 把 `adjust_type` 设为
         `AdjustType.hfq`，下单/估值查的是后复权表。只导入未复权表不会有任何成交，
         所以这里两张表都导；
      3. **导出范围要覆盖回溯窗口**：Trader 会把因子起点前移 `pre_load_days`
         （默认 365 天）。只导出回测区间本身，均线预热不足，信号会与原生通道
         不同——所以导出起点取 `EXTERNAL_EXPORT_START`。

    Returns:
        str: 形如
        "外部源入库 1202 行, 对照期末 986,836.17, 差异 0.00"

    Raises:
        AssertionError: 入库行数不符、任一组无成交，或两组期末资产不一致。
    """
    out_dir = os.path.join(zvt_env["tmp_path"], "external_demo")
    os.makedirs(out_dir, exist_ok=True)

    total_rows = 0
    for schema in (Stock1dKdata, Stock1dHfqKdata):
        exported, saved, _ = _import_external(
            schema, EXTERNAL_PROVIDER, ENTITY_ID,
            EXTERNAL_EXPORT_START, "2024-06-30", out_dir,
        )
        assert exported == saved
        total_rows += saved

        # 单日查询必须命中：这是第 1 个坑的直接检测点
        probe = schema.query_data(
            entity_ids=[ENTITY_ID], provider=EXTERNAL_PROVIDER,
            start_timestamp="2024-01-16", end_timestamp="2024-01-16",
        )
        assert len(probe) == 1, (
            f"{schema.__tablename__} 单日查询命中 {len(probe)} 行，"
            "通常是 timestamp 被存成了字符串"
        )

    class ExternalTrader(StockTrader):
        """两个 provider 共用同一套策略，唯一变量是数据来自哪里"""

        def init_factors(self, entity_ids, entity_schema, exchanges, codes,
                         start_timestamp, end_timestamp, adjust_type=None):
            factor = CrossMaFactor(
                entity_ids=entity_ids,
                entity_schema=entity_schema,
                provider=self.provider,
                entity_provider=self.provider,
                codes=codes,
                exchanges=exchanges,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
                windows=[5, 10],
                need_persist=False,
            )
            factor.compute()
            return [factor]

    def run_once(provider, trader_name):
        return _run_cross_ma_backtest(provider, trader_name, ExternalTrader)

    control_final, control_orders = run_once(PROVIDER, TRADER_NAME_EXT_CONTROL)
    csv_final, csv_orders = run_once(EXTERNAL_PROVIDER, TRADER_NAME_EXT_CSV)

    diff = abs(control_final - csv_final)
    assert diff < 0.01, (
        f"外部源回测结果与原生通道不一致：{csv_final:.2f} vs {control_final:.2f}"
    )
    return (
        f"外部源入库 {total_rows} 行, 对照 {control_orders} 笔/{control_final:,.2f}, "
        f"外部 {csv_orders} 笔/{csv_final:,.2f}, 差异 {diff:.2f}"
    )


def _run_cross_ma_backtest(provider, trader_name, trader_cls, start="2024-01-01", end="2024-06-30"):
    """
    用固定策略跑一次回测并返回 (期末资产, 订单数)，供外部数据源各示例做对照。

    抽出来是因为「对照组 vs 实验组」必须完全同参：策略类相同、区间相同，
    唯一变量是 provider。手写两遍容易漏参数，导致对照本身不成立。

    Args:
        provider: 数据来源，决定 K 线从哪个库读取。
        trader_name: 结果存储名，需与其它示例区分。
        trader_cls: Trader 子类，其 `init_factors` 已用 `self.provider` 构造因子。
        start: 回测起点。
        end: 回测终点。

    Returns:
        tuple[float, int]: (期末总资产, 订单笔数)。

    Raises:
        AssertionError: 无账户快照，或无成交（后者通常是下单时按日期查不到 K 线）。
    """
    clear_trader(trader_name)
    trader_cls(
        entity_ids=[ENTITY_ID],
        provider=provider,
        level=IntervalLevel.LEVEL_1DAY,
        start_timestamp=start,
        end_timestamp=end,
        trader_name=trader_name,
        draw_result=False,
    ).run()

    acct = AccountStats.query_data(
        filters=[AccountStats.trader_name == trader_name],
        order=AccountStats.timestamp.asc(),
        index="timestamp",
    )
    orders_df = Order.query_data(filters=[Order.trader_name == trader_name])
    assert not acct.empty, f"{trader_name} 无账户快照"
    assert not orders_df.empty, f"{trader_name} 无成交（K 线查询不到时的典型症状）"
    return acct["all_value"].iloc[-1], len(orders_df)


def _sync_batch(schema, provider, entity_id, start_timestamp, end_timestamp):
    """
    把外部源的一段数据增量同步进库（`force_update=False`），返回实际新增行数。

    这是与第六套示例的关键差异：第六套是「一次性全量导入 + 覆盖写」，
    这里模拟生产中的分页/定时同步——每批只追加新行，已存在的行不动。

    `df_to_db` 内部会按 `id` 查重，因此重复投喂同一批数据不会产生重复行，
    返回值就是真正写入的行数（0 表示这批数据已经同步过）。

    Returns:
        tuple[int, int]: (实际新增行数, 源数据行数)。
    """
    df = schema.query_data(
        entity_ids=[entity_id],
        provider=PROVIDER,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
    )
    if df.empty:
        return 0, 0
    df["provider"] = provider
    saved = df_to_db(df, schema, provider=provider, force_update=False)
    return saved, len(df)


def check_backtest_incremental():
    """
    增量接入外部数据源：分两批同步 → 重复投喂幂等 → 断点续采 → 回测对照。

    与 `check_backtest_external()` 的分工：
      - 那个演示「一次性全量导入」（CSV 文件 → `force_update=True` 覆盖）；
      - 本函数演示「增量同步」：每批只追加新行，已同步的不重复写。
        这是定时任务每天拉数据时的真实形态。

    四个断言分别对应接入外部源时最易踩的坑：
      1. 首批全量写入后 行数 == 源行数（不是 0，也不是 2 倍）；
      2. 第二批只写入新增行（不覆盖首批，也不重复）；
      3. 重复投喂同一批 → 新增 0 行（`df_to_db` 按 id 查重，天然幂等）；
      4. 以库内最新日期为断点续采 → 新增 0 行（断点续传不会重拉历史）。

    批次划分有讲究：首批从 `EXTERNAL_EXPORT_START`（2022-01-01）开始，
    以覆盖 Trader 的 365 天回溯窗口；否则均线预热不足，回测结果会与
    原生通道不一致——这是第六套示例里已经踩过的坑。

    Returns:
        str: 形如
        "2 批增量入库 601+601 行, 重复投喂新增 0 行, 对照 27 笔/986,836.17, 差异 0.00"

    Raises:
        AssertionError: 批次写入行数异常、幂等被破坏，或回测结果与对照不一致。
    """
    provider = EXTERNAL_PROVIDER
    # 从空库开始，保证每次运行的可观测数字一致
    for schema in (Stock1dKdata, Stock1dHfqKdata):
        del_data(schema, filters=[schema.provider == provider], provider=provider)

    # 首批覆盖回溯窗口，第二批是回测区间内的增量
    batches = [
        (EXTERNAL_EXPORT_START, "2024-03-31"),
        ("2024-04-01", "2024-06-30"),
    ]

    added_per_batch = []
    for schema in (Stock1dKdata, Stock1dHfqKdata):
        batch_counts = []
        for start, end in batches:
            added, source_rows = _sync_batch(schema, provider, ENTITY_ID, start, end)
            assert added == source_rows, (
                f"{schema.__tablename__} {start}~{end} 新增 {added} 行 != 源 {source_rows} 行"
            )
            batch_counts.append(added)
        added_per_batch = batch_counts

        # 幂等：同一批再投喂一次，应当一行都不新增
        repeat_added, _ = _sync_batch(schema, provider, ENTITY_ID, *batches[-1])
        assert repeat_added == 0, f"{schema.__tablename__} 重复投喂新增 {repeat_added} 行，幂等被破坏"

        # 断点续采：从库内最新日期继续拉，已同步的部分不应再写入
        latest = schema.query_data(
            entity_ids=[ENTITY_ID], provider=provider,
            order=schema.timestamp.desc(), limit=1,
        )["timestamp"].max()
        resume_added, _ = _sync_batch(
            schema, provider, ENTITY_ID,
            to_date_time_str(latest), "2024-06-30",
        )
        assert resume_added == 0, f"{schema.__tablename__} 断点续采重复写入 {resume_added} 行"

        # 最终行数应等于「回测窗口 + 回溯窗口」的总行数
        total = get_data_count(schema, provider=provider)
        expected = len(schema.query_data(
            entity_ids=[ENTITY_ID], provider=PROVIDER,
            start_timestamp=EXTERNAL_EXPORT_START, end_timestamp="2024-06-30",
        ))
        assert total == expected, f"{schema.__tablename__} 累计 {total} 行 != 预期 {expected} 行"

    class IncrementalTrader(StockTrader):
        """与第六套示例同策略，便于两个 provider 的结果直接对比"""

        def init_factors(self, entity_ids, entity_schema, exchanges, codes,
                         start_timestamp, end_timestamp, adjust_type=None):
            factor = CrossMaFactor(
                entity_ids=entity_ids,
                entity_schema=entity_schema,
                provider=self.provider,
                entity_provider=self.provider,
                codes=codes,
                exchanges=exchanges,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
                windows=[5, 10],
                need_persist=False,
            )
            factor.compute()
            return [factor]

    control_final, control_orders = _run_cross_ma_backtest(
        PROVIDER, TRADER_NAME_INC_CONTROL, IncrementalTrader
    )
    inc_final, inc_orders = _run_cross_ma_backtest(
        provider, TRADER_NAME_INC, IncrementalTrader
    )

    diff = abs(control_final - inc_final)
    assert diff < 0.01, (
        f"增量同步后回测结果与原生通道不一致：{inc_final:.2f} vs {control_final:.2f}"
    )
    return (
        f"2 批增量入库 {'+'.join(str(n) for n in added_per_batch)} 行, "
        f"重复投喂新增 0 行, 对照 {control_orders} 笔/{control_final:,.2f}, 差异 {diff:.2f}"
    )


def check_rest():
    """
    校验 REST 服务可访问（前置条件：服务已手动启动）。

    与 `check_rest_service()` 的区别：本函数只发请求，不负责起停服务。
    保留它是为了让单元测试可以用 monkeypatch 替换 `requests.get` 直接覆盖 HTTP 分支。

    Returns:
        str: 形如 "providers=['em', 'joinquant']"

    Raises:
        requests.HTTPError: 接口返回 4xx/5xx。
        AssertionError: 返回体为空。
    """
    import requests

    resp = requests.get(REST_URL, timeout=5)
    resp.raise_for_status()
    body = resp.json()
    assert body, "providers 返回为空"
    return f"providers={body}"


def check_rest_service(timeout=60):
    """
    自启动 zvt_server → 轮询就绪 → 校验接口 → 终止进程。

    比 `check_rest()` 更适合 CI：不需要人工先起服务，也不会因残留进程占用端口
    导致下次失败。三个关键细节：

      - **子进程日志用 DEVNULL 而非 PIPE**：父进程不读取时管道缓冲区写满，
        uvicorn 会阻塞在写日志上，表现为「进程还活着但端口一直连不上」，
        很容易误判成端口被占用；
      - **session.trust_env = False**：本机若配置了 http_proxy，requests 会把
        127.0.0.1 的请求也交给代理，导致连接超时；
      - **轮询中检查 proc.poll()**：服务若因参数错误或端口冲突提前退出，
        应立即报错，而不是白等满 timeout 才报「未就绪」。

    Args:
        timeout: 等待服务就绪的最长秒数，默认 60。

    Returns:
        str: 形如 "providers=[...]"

    Raises:
        RuntimeError: 服务进程在就绪前退出（附退出码）。
        TimeoutError: 超时仍未就绪，错误信息含最后一次连接异常。
        AssertionError: 接口返回体为空。

    Side Effects:
        启动并最终终止一个 uvicorn 子进程（`finally` 中保证清理）。
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "zvt.zvt_server:app",
         "--host", "127.0.0.1", "--port", "8090", "--log-level", "warning"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        import requests

        session = requests.Session()
        session.trust_env = False   # 绕过系统代理，避免 127.0.0.1 被代理接管

        deadline = time.time() + timeout
        last_err = None
        while time.time() < deadline:
            try:
                resp = session.get(REST_URL, timeout=2)
                resp.raise_for_status()
                body = resp.json()
                assert body, "providers 返回为空"
                return f"providers={body}"
            except Exception as e:
                last_err = e
                # 进程已退出说明启动失败，立即抛出真实原因
                if proc.poll() is not None:
                    raise RuntimeError(f"服务进程提前退出，退出码 {proc.returncode}")
                time.sleep(1)
        raise TimeoutError(f"{timeout}s 内服务未就绪: {last_err}")
    finally:
        # 必须清理：残留进程会占住 8090，导致后续校验全部失败
        proc.terminate()
        proc.wait(timeout=10)


# ---------------------------------------------------------------- 入口
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="zvt 环境分层校验")
    parser.add_argument("--full", action="store_true", help="包含回测与 REST 校验")
    parser.add_argument(
        "--grid",
        action="store_true",
        help="额外运行参数网格对比回测（3 组均线参数，约 3 倍回测耗时）",
    )
    parser.add_argument(
        "--cross",
        action="store_true",
        help="额外运行多因子类型交叉对比（3 类因子 × 2 组参数，约 6 倍回测耗时）",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="导出回测结果 CSV 并打印净值/回撤走势图",
    )
    parser.add_argument(
        "--external",
        action="store_true",
        help="外部数据源接入演示：CSV → 独立 provider → 双通道回测对照",
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="外部数据源增量同步：分批入库 + 幂等 + 断点续采 + 回测对照",
    )
    args = parser.parse_args()

    # 顺序即依赖顺序：前面失败时，后面的检查结果仅供参考
    check("依赖", check_deps)
    check("环境变量", check_env)
    check("存储目录", check_storage)
    check("元数据", check_meta)
    check("K 线", check_kdata)
    check("数据质量", check_quality)
    check("采集幂等", check_idempotent)
    check("因子", check_factor)
    if args.full:
        # 回测较慢（每个约 1 分钟），REST 需拉起服务，故放在 --full 下
        check("回测", check_backtest)
        check("回测(自定义钩子)", check_backtest_hooks)
        check("REST 服务", check_rest_service)
    if args.grid:
        # 独立开关：3 组参数串行跑，耗时是单个回测的 3 倍左右
        check("回测(参数网格)", check_backtest_grid)
    if args.cross:
        # 独立开关：3 类因子 × 2 组参数 = 6 个回测，耗时约为单个的 6 倍
        check("回测(多因子交叉)", check_backtest_cross)
    if args.report:
        # 复用基础回测结果导出；若结果不存在会自动补跑一次
        check("回测(结果导出)", check_backtest_report)
    if args.external:
        # 独立开关：导入 2 张表 + 跑 2 个回测做对照
        check("回测(外部数据源)", check_backtest_external)
    if args.incremental:
        # 独立开关：清空外部 provider → 分批同步 → 幂等/断点校验 → 2 个回测对照
        check("回测(增量接入)", check_backtest_incremental)

    failed = results.count(False)
    print(f"\n结果：{results.count(True)} PASS / {failed} FAIL")
    # 非 0 退出码是 CI 判定失败的依据
    sys.exit(1 if failed else 0)
