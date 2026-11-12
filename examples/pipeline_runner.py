# -*- coding: utf-8 -*-
"""
端到端一体化编排示例：采集 → 选股 → 股票池 → 次日交易计划 → 通知。

运行方式：
    python examples/pipeline_runner.py               # 立即执行一次流水线
    python examples/pipeline_runner.py --schedule    # 常驻，按 cron 每日调度
"""
import argparse
import logging
import traceback

from apscheduler.schedulers.background import BackgroundScheduler

from zvt import init_log, zvt_config
from zvt.api.kdata import get_latest_kdata_date, get_trade_dates
from zvt.api.selector import get_entity_ids_by_filter
from zvt.contract import IntervalLevel
from zvt.domain import Stock, Stock1dHfqKdata, LimitUpInfo
from zvt.factors.ma.ma_factor import CrossMaFactor
from zvt.factors.target_selector import TargetSelector
from zvt.informer import EmailInformer
from zvt.tag.common import StockPoolType
from zvt.tag.tag_models import CreateStockPoolInfoModel, CreateStockPoolsModel
from zvt.tag.tag_service import build_stock_pool, build_stock_pool_info
from zvt.trading.trading_models import BuildTradingPlanModel
from zvt.trading.trading_service import build_trading_plan
from zvt.trader import TradingSignalType
from zvt.utils.recorder_utils import run_data_recorder
from zvt.utils.time_utils import current_date, recent_year_date, to_date_time_str, to_pd_timestamp

logger = logging.getLogger(__name__)
sched = BackgroundScheduler()
email = EmailInformer()

LEVEL = IntervalLevel.LEVEL_1DAY
PROVIDER = "em"
POOL_NAME = "双均线池"
DEMO_LIMIT = 200


def date_range():
    """因子计算区间：近一年至今"""
    return to_date_time_str(recent_year_date()), to_date_time_str(current_date())


def next_trading_date():
    """取下一个交易日（交易日历来自已入库的指数日线）"""
    today = to_date_time_str(current_date())
    for d in get_trade_dates(entity_type="stock", start=today):
        d_str = to_date_time_str(d)
        if d_str > today:
            return d_str
    return today


def notify(title, body):
    try:
        email.send_message(zvt_config["email_username"], title, body)
    except Exception:
        # 未配置邮箱时不影响主流程
        logger.warning(f"notify failed: {title}")


def run_step(name, func):
    """统一异常捕获：单步失败只告警，不抛出"""
    logger.info(f"===== {name} start =====")
    try:
        result = func()
        logger.info(f"===== {name} done =====")
        return result
    except Exception:
        logger.error(f"===== {name} failed =====\n{traceback.format_exc()}")
        notify(f"{name} 失败", traceback.format_exc())
        return None


def step_collect():
    """STEP 1：采集元数据、事件与 K 线，返回有效标的"""
    run_data_recorder(domain=Stock, data_provider=PROVIDER)
    run_data_recorder(domain=LimitUpInfo, data_provider=PROVIDER)

    entity_ids = get_entity_ids_by_filter(
        provider=PROVIDER,
        ignore_delist=True,
        ignore_st=True,
        ignore_new_stock=False,
        limit=DEMO_LIMIT,
    )
    unfinished = run_data_recorder(
        domain=Stock1dHfqKdata,
        entity_ids=entity_ids,
        data_provider=PROVIDER,
        entity_provider=PROVIDER,
        day_data=True,
        sleeping_time=2,
        retry_times=5,
        return_unfinished=True,
    )
    if unfinished:
        # 数据不完整时不生成交易计划，避免脏信号
        raise RuntimeError(f"K 线未完成实体 {len(unfinished)} 个，中断后续选股")
    return entity_ids


def step_select():
    """STEP 2：因子计算 + 选股 + 股票池落库"""
    start_timestamp, end_timestamp = date_range()
    data_date = get_latest_kdata_date(entity_type="stock", provider=PROVIDER)

    entity_ids = get_entity_ids_by_filter(
        provider=PROVIDER,
        ignore_delist=True,
        ignore_st=True,
        ignore_new_stock=False,
        limit=DEMO_LIMIT,
    )

    factor = CrossMaFactor(
        entity_schema=Stock,
        entity_ids=entity_ids,
        provider=PROVIDER,
        entity_provider=PROVIDER,
        level=LEVEL,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        windows=[5, 10],
        need_persist=False,
    )
    factor.compute()

    selector = TargetSelector(
        entity_schema=Stock,
        provider=PROVIDER,
        level=LEVEL,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
    )
    selector.add_factor(factor)
    selector.run()

    selected = selector.get_open_long_targets(to_pd_timestamp(data_date)) or []
    logger.info(f"{data_date} 选股 {len(selected)} 只: {selected}")

    # 选股结果落为股票池，供盘中、前端、交易计划复用
    if selected:
        build_stock_pool_info(
            create_stock_pool_info_model=CreateStockPoolInfoModel(
                stock_pool_type=StockPoolType.custom, stock_pool_name=POOL_NAME,
            ),
            timestamp=to_date_time_str(data_date),
        )
        build_stock_pool(
            create_stock_pools_model=CreateStockPoolsModel(
                stock_pool_name=POOL_NAME, entity_ids=selected,
            ),
            target_date=to_date_time_str(data_date),
        )
    return selected


def step_plan(selected_ids):
    """STEP 3：为选股结果生成下一交易日的买入计划"""
    plan_date = next_trading_date()
    plans = []
    for entity_id in selected_ids:
        plans.append(
            build_trading_plan(
                BuildTradingPlanModel(
                    stock_id=entity_id,
                    trading_date=plan_date,
                    expected_open_pct=0.0,
                    trading_reason=f"{POOL_NAME}选股",
                    trading_signal_type=TradingSignalType.open_long,
                )
            )
        )
    logger.info(f"{plan_date} 生成交易计划 {len(plans)} 条")
    return plans


def step_report(selected_ids, plans):
    """STEP 4：汇总通知"""
    today = to_date_time_str(current_date())
    content = (
        f"{today} 一体化流水线完成\n"
        f"选股数量：{len(selected_ids or [])}\n"
        f"选股明细：{', '.join((selected_ids or [])[:20])}\n"
        f"交易计划：{len(plans or [])} 条（{next_trading_date()} 生效）"
    )
    logger.info(content)
    notify(f"{today} 一体化流水线完成", content)


def daily_pipeline():
    """主流水线：前序步骤失败即中断"""
    entity_ids = run_step("1-采集", step_collect)
    if not entity_ids:
        return

    selected = run_step("2-选股", step_select)
    if selected is None:
        return

    plans = run_step("3-交易计划", lambda: step_plan(selected))
    run_step("4-报告", lambda: step_report(selected, plans))


@sched.scheduled_job("cron", hour=16, minute=0, day_of_week="mon-fri")
def scheduled_pipeline():
    daily_pipeline()


@sched.scheduled_job("cron", hour=9, minute=0, day_of_week="sat")
def scheduled_catch_up():
    """周末兜底补采：增量机制保证重复执行无副作用"""
    run_step(
        "周末补采",
        lambda: run_data_recorder(
            domain=Stock1dHfqKdata,
            data_provider=PROVIDER,
            entity_provider=PROVIDER,
            day_data=True,
            sleeping_time=2,
            retry_times=10,
        ),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="zvt 端到端一体化流水线")
    parser.add_argument("--schedule", action="store_true", help="常驻并按 cron 调度")
    args = parser.parse_args()

    init_log("pipeline_runner.log")

    if args.schedule:
        sched.start()
        sched._thread.join()
    else:
        daily_pipeline()
