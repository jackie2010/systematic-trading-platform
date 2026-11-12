# -*- coding: utf-8 -*-
"""
verify_env.py 的单元测试：全部通过 monkeypatch 打桩，不依赖真实数据/网络/服务。
"""
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

# 被测脚本位于 examples/ 下，需将其加入 sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../examples")))

import verify_env


def _fake_df(rows=3, duplicate_index=False):
    index = (
        pd.DatetimeIndex([pd.Timestamp("2024-01-01")] * rows, name="timestamp")
        if duplicate_index
        else pd.DatetimeIndex(pd.date_range("2024-01-01", periods=rows), name="timestamp")
    )
    return pd.DataFrame({"close": [1.0] * rows}, index=index)


# ---------------------------------------------------------------- check()
class TestCheckRunner:
    def test_pass_recorded(self):
        verify_env.results.clear()
        verify_env.check("demo", lambda: "ok")
        assert verify_env.results == [True]

    def test_fail_recorded_without_raising(self, capsys):
        def boom():
            raise RuntimeError("bad")

        verify_env.results.clear()
        verify_env.check("demo", boom)          # 不应抛出
        assert verify_env.results == [False]
        assert "[FAIL] demo: bad" in capsys.readouterr().out


# ---------------------------------------------------------------- check_env
class TestCheckEnv:
    def test_ok(self, monkeypatch):
        monkeypatch.setattr(verify_env, "zvt_env", {
            "zvt_home": "/tmp/zvt", "data_path": "/tmp/zvt/data", "log_path": "/tmp/zvt/logs",
        })
        monkeypatch.setattr(verify_env.os, "access", lambda *a, **k: True)
        assert "data_path=/tmp/zvt/data" in verify_env.check_env()

    def test_missing_key(self, monkeypatch):
        monkeypatch.setattr(verify_env, "zvt_env", {"zvt_home": "/tmp/zvt"})
        with pytest.raises(AssertionError, match="zvt_env\\[data_path\\]"):
            verify_env.check_env()

    def test_data_path_not_writable(self, monkeypatch):
        monkeypatch.setattr(verify_env, "zvt_env", {
            "zvt_home": "/tmp/zvt", "data_path": "/tmp/zvt/data", "log_path": "/tmp/zvt/logs",
        })
        monkeypatch.setattr(verify_env.os, "access", lambda *a, **k: False)
        with pytest.raises(AssertionError, match="不可写"):
            verify_env.check_env()


# ---------------------------------------------------------------- check_storage
class TestCheckStorage:
    def test_ok(self, monkeypatch, tmp_path):
        provider_dir = tmp_path / verify_env.PROVIDER
        provider_dir.mkdir()
        (provider_dir / f"{verify_env.PROVIDER}_stock_meta.db").write_text("")
        monkeypatch.setattr(verify_env, "zvt_env", {"data_path": str(tmp_path)})
        detail = verify_env.check_storage()
        assert "1 个 db" in detail

    def test_missing_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(verify_env, "zvt_env", {"data_path": str(tmp_path)})
        with pytest.raises(AssertionError, match="请先执行采集"):
            verify_env.check_storage()

    def test_empty_dir(self, monkeypatch, tmp_path):
        (tmp_path / verify_env.PROVIDER).mkdir()
        monkeypatch.setattr(verify_env, "zvt_env", {"data_path": str(tmp_path)})
        with pytest.raises(AssertionError, match="无 db 文件"):
            verify_env.check_storage()


# ---------------------------------------------------------------- check_meta
class TestCheckMeta:
    def test_ok(self, monkeypatch):
        monkeypatch.setattr(verify_env, "get_data_count", lambda *a, **k: 5200)
        assert "5200 只股票" in verify_env.check_meta()

    def test_empty(self, monkeypatch):
        monkeypatch.setattr(verify_env, "get_data_count", lambda *a, **k: 0)
        with pytest.raises(AssertionError, match="Stock 表为空"):
            verify_env.check_meta()


# ---------------------------------------------------------------- check_kdata
class _FakeTimestamp:
    @staticmethod
    def desc():
        return "timestamp desc"


class TestCheckKdata:
    def _patch_schema(self, monkeypatch, df):
        class FakeKdata:
            timestamp = _FakeTimestamp()

            @staticmethod
            def query_data(**kwargs):
                return df

        monkeypatch.setattr(verify_env, "Stock1dHfqKdata", FakeKdata)

    def test_ok(self, monkeypatch):
        self._patch_schema(monkeypatch, _fake_df(rows=5))
        detail = verify_env.check_kdata()
        assert "latest=2024-01-05" in detail and "抽样 5 条" in detail

    def test_empty(self, monkeypatch):
        self._patch_schema(monkeypatch, pd.DataFrame())
        with pytest.raises(AssertionError, match="无 K 线"):
            verify_env.check_kdata()

    def test_duplicate_index(self, monkeypatch):
        self._patch_schema(monkeypatch, _fake_df(rows=3, duplicate_index=True))
        with pytest.raises(AssertionError, match="主键重复"):
            verify_env.check_kdata()


# ---------------------------------------------------------------- check_factor
class TestCheckFactor:
    def _patch_factor(self, monkeypatch, result_df):
        class FakeFactor:
            def __init__(self, **kwargs):
                self.result_df = None

            def compute(self):
                self.result_df = result_df

        monkeypatch.setattr(verify_env, "CrossMaFactor", FakeFactor)

    def test_ok(self, monkeypatch):
        self._patch_factor(monkeypatch, _fake_df(rows=10))
        assert "result rows=10" in verify_env.check_factor()

    def test_empty_result(self, monkeypatch):
        self._patch_factor(monkeypatch, pd.DataFrame())
        with pytest.raises(AssertionError, match="因子结果为空"):
            verify_env.check_factor()


# ---------------------------------------------------------------- check_backtest
class _FakeColumn:
    def __eq__(self, other):
        return self


class _FakeTimestamp:
    """order=...asc()/desc() 的占位实现，返回自身即可被 query_data 打桩忽略"""

    def asc(self):
        return self

    def desc(self):
        return self


class TestCheckBacktest:
    def _patch_trader(self, monkeypatch, acct_df, orders_df=None):
        class FakeTrader:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def run(self):
                pass

        class FakeAccountStats:
            trader_name = _FakeColumn()
            timestamp = _FakeTimestamp()

            @staticmethod
            def query_data(**kwargs):
                return acct_df

        class FakeOrder:
            trader_name = _FakeColumn()
            timestamp = _FakeTimestamp()

            @staticmethod
            def query_data(**kwargs):
                return orders_df if orders_df is not None else pd.DataFrame()

        monkeypatch.setattr(verify_env, "StockTrader", FakeTrader)
        monkeypatch.setattr(verify_env, "AccountStats", FakeAccountStats)
        monkeypatch.setattr(verify_env, "Order", FakeOrder)
        monkeypatch.setattr(verify_env, "clear_trader", lambda *a, **k: None)

    @staticmethod
    def _acct(navs=(1000.0, 1234.5), cash=1000.0):
        index = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=len(navs), name="timestamp"))
        # 保持恒等式 all_value == cash + value 成立
        return pd.DataFrame({
            "input_money": [1000.0] * len(navs),
            "cash": [cash] * len(navs),
            "value": [nav - cash for nav in navs],
            "all_value": list(navs),
        }, index=index)

    @staticmethod
    def _orders(buy=True, sell=True):
        rows = []
        if buy:
            rows.append({"entity_id": "stock_sz_000001", "order_type": "order_long",
                         "order_amount": 100, "order_price": 10.0})
        if sell:
            rows.append({"entity_id": "stock_sz_000001", "order_type": "order_close_long",
                         "order_amount": 100, "order_price": 11.0})
        return pd.DataFrame(rows)

    def test_ok(self, monkeypatch):
        self._patch_trader(monkeypatch, self._acct(), self._orders())
        detail = verify_env.check_backtest()
        assert "账户快照 2 条" in detail
        assert "成交 2 笔(买1/卖1)" in detail
        # 现金 1000 + 市值 0 = all_value 1000，恒等式成立
        assert "期末 1234.50" in detail

    def test_no_account_record(self, monkeypatch):
        self._patch_trader(monkeypatch, pd.DataFrame(), self._orders())
        with pytest.raises(AssertionError, match="未写入 AccountStats"):
            verify_env.check_backtest()

    def test_identity_broken(self, monkeypatch):
        acct = self._acct()
        acct.loc[acct.index[-1], "all_value"] = 5000.0   # 与 cash+value 不符
        self._patch_trader(monkeypatch, acct, self._orders())
        with pytest.raises(AssertionError, match="资产恒等式不成立"):
            verify_env.check_backtest()

    def test_no_orders(self, monkeypatch):
        self._patch_trader(monkeypatch, self._acct(), pd.DataFrame())
        with pytest.raises(AssertionError, match="回测无成交"):
            verify_env.check_backtest()

    def test_only_one_side(self, monkeypatch):
        self._patch_trader(monkeypatch, self._acct(), self._orders(sell=False))
        with pytest.raises(AssertionError, match="只有单边成交"):
            verify_env.check_backtest()


class TestRoundTrips:
    def test_pairing(self):
        orders = pd.DataFrame([
            {"entity_id": "e", "order_type": "order_long", "order_amount": 100, "order_price": 10.0},
            {"entity_id": "e", "order_type": "order_close_long", "order_amount": 100, "order_price": 12.0},
        ])
        trips = verify_env._round_trips(orders)
        assert len(trips) == 1 and abs(trips[0] - 0.2) < 1e-9

    def test_open_position_excluded(self):
        orders = pd.DataFrame([
            {"entity_id": "e", "order_type": "order_long", "order_amount": 100, "order_price": 10.0},
        ])
        assert verify_env._round_trips(orders) == []


# ---------------------------------------------------------------- check_rest
class TestCheckBacktestHooks:
    def _patch(self, monkeypatch, acct_df, orders_df, pos_df):
        class FakeTrader:
            def __init__(self, **kwargs):
                pass

            def run(self):
                pass

        class _Queryable:
            trader_name = _FakeColumn()
            timestamp = _FakeTimestamp()

            def __init__(self, df):
                self._df = df

            def query_data(self, **kwargs):
                return self._df

        monkeypatch.setattr(verify_env, "StockTrader", FakeTrader)
        monkeypatch.setattr(verify_env, "AccountStats", _Queryable(acct_df))
        monkeypatch.setattr(verify_env, "Order", _Queryable(orders_df))
        monkeypatch.setattr(verify_env, "Position", _Queryable(pos_df))
        monkeypatch.setattr(verify_env, "clear_trader", lambda *a, **k: None)

    @staticmethod
    def _acct():
        index = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=2, name="timestamp"))
        return pd.DataFrame({
            "input_money": [1000.0, 1000.0],
            "all_value": [1000.0, 990.0],
        }, index=index)

    def test_ok(self, monkeypatch):
        pos = pd.DataFrame({"timestamp": pd.to_datetime(["2024-01-02", "2024-01-02"])})
        self._patch(monkeypatch, self._acct(), pd.DataFrame({"order_type": ["order_long"]}), pos)
        detail = verify_env.check_backtest_hooks()
        assert "最大持仓 2" in detail
        assert "收益 -1.00%" in detail

    def test_position_limit_exceeded(self, monkeypatch):
        pos = pd.DataFrame({"timestamp": pd.to_datetime(["2024-01-02"] * 4)})
        self._patch(monkeypatch, self._acct(), pd.DataFrame({"order_type": ["order_long"]}), pos)
        with pytest.raises(AssertionError, match="限仓钩子未生效"):
            verify_env.check_backtest_hooks()

    def test_no_orders(self, monkeypatch):
        self._patch(monkeypatch, self._acct(), pd.DataFrame(), pd.DataFrame())
        with pytest.raises(AssertionError, match="无成交"):
            verify_env.check_backtest_hooks()


class TestCheckBacktestGrid:
    def _patch(self, monkeypatch, final_map):
        """final_map: {trader_name: 期末资产}，按 filter 中的名字返回不同快照"""

        class FakeTrader:
            windows = [5, 10]

            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def run(self):
                pass

        class _NamedColumn:
            def __init__(self, name):
                self._name = name

            def __eq__(self, other):
                # 把条件编码成元组，query_data 才能知道是哪一组参数
                return ("filter", self._name, other)

        class _Asc:
            def asc(self):
                return None

        class FakeAccountStats:
            trader_name = _NamedColumn("trader_name")
            # 代码里用了 order=AccountStats.timestamp.asc()，假对象需支持 .asc()
            timestamp = _Asc()

            @staticmethod
            def query_data(**kwargs):
                name = kwargs["filters"][0][2]
                if name not in final_map:
                    return pd.DataFrame()
                index = pd.DatetimeIndex(
                    pd.date_range("2024-01-01", periods=2, name="timestamp")
                )
                return pd.DataFrame({
                    "input_money": [1000.0, 1000.0],
                    "all_value": [1000.0, final_map[name]],
                }, index=index)

        class FakeOrder:
            trader_name = _NamedColumn("trader_name")

            @staticmethod
            def query_data(**kwargs):
                return pd.DataFrame({
                    "entity_id": ["e", "e"],
                    "order_type": ["order_long", "order_close_long"],
                    "order_amount": [100, 100],
                    "order_price": [10.0, 11.0],
                })

        monkeypatch.setattr(verify_env, "StockTrader", FakeTrader)
        monkeypatch.setattr(verify_env, "AccountStats", FakeAccountStats)
        monkeypatch.setattr(verify_env, "Order", FakeOrder)
        monkeypatch.setattr(verify_env, "clear_trader", lambda *a, **k: None)

    def test_ok(self, monkeypatch):
        self._patch(monkeypatch, {
            f"{verify_env.TRADER_NAME_GRID}_5_10": 990.0,
            f"{verify_env.TRADER_NAME_GRID}_5_20": 1010.0,
            f"{verify_env.TRADER_NAME_GRID}_10_20": 1020.0,
        })
        detail = verify_env.check_backtest_grid()
        assert "3 组参数" in detail
        assert "10/20" in detail   # 收益最高者应被选为最优

    def test_same_result_means_params_ignored(self, monkeypatch):
        same = {f"{verify_env.TRADER_NAME_GRID}_{a}_{b}": 1000.0
                for a, b in [(5, 10), (5, 20), (10, 20)]}
        self._patch(monkeypatch, same)
        with pytest.raises(AssertionError, match="参数未传入因子"):
            verify_env.check_backtest_grid()

    def test_missing_account(self, monkeypatch):
        self._patch(monkeypatch, {})
        with pytest.raises(AssertionError, match="无账户快照"):
            verify_env.check_backtest_grid()


class TestCheckBacktestCross:
    def _patch(self, monkeypatch, finals_by_name):
        """finals_by_name: {trader_name: 期末资产}"""

        class FakeTrader:
            factor_cls = None
            factor_kwargs = {}

            def __init__(self, **kwargs):
                pass

            def run(self):
                pass

        class _NamedColumn:
            def __init__(self, name):
                self._name = name

            def __eq__(self, other):
                return ("filter", self._name, other)

        class _Asc:
            def asc(self):
                return None

        class FakeAccountStats:
            trader_name = _NamedColumn("trader_name")
            timestamp = _Asc()

            @staticmethod
            def query_data(**kwargs):
                name = kwargs["filters"][0][2]
                if name not in finals_by_name:
                    return pd.DataFrame()
                index = pd.DatetimeIndex(
                    pd.date_range("2024-01-01", periods=2, name="timestamp")
                )
                return pd.DataFrame({
                    "input_money": [1000.0, 1000.0],
                    "all_value": [1000.0, finals_by_name[name]],
                }, index=index)

        class FakeOrder:
            trader_name = _NamedColumn("trader_name")

            @staticmethod
            def query_data(**kwargs):
                return pd.DataFrame({"order_type": ["order_long"]})

        monkeypatch.setattr(verify_env, "StockTrader", FakeTrader)
        monkeypatch.setattr(verify_env, "AccountStats", FakeAccountStats)
        monkeypatch.setattr(verify_env, "Order", FakeOrder)
        monkeypatch.setattr(verify_env, "clear_trader", lambda *a, **k: None)

    def test_ok(self, monkeypatch):
        names = {
            f"{verify_env.TRADER_NAME_GRID}_{tag}": v
            for tag, v in [
                ("crossma_1", 990.0), ("crossma_2", 1010.0),
                ("bull_1", 1020.0), ("bull_2", 1020.0),
                ("goldcross_1", 1050.0), ("goldcross_2", 1050.0),
            ]
        }
        self._patch(monkeypatch, names)
        detail = verify_env.check_backtest_cross()
        assert "6 组" in detail
        assert "不同因子差异已确认" in detail

    def test_all_same_result(self, monkeypatch):
        same = 1000.0
        names = {
            f"{verify_env.TRADER_NAME_GRID}_{tag}": same
            for tag in ["crossma_1", "crossma_2", "bull_1", "bull_2",
                        "goldcross_1", "goldcross_2"]
        }
        self._patch(monkeypatch, names)
        with pytest.raises(AssertionError, match="因子类型未切换"):
            verify_env.check_backtest_cross()


class TestSparkline:
    def test_rising(self):
        line = verify_env._sparkline([1, 2, 3, 4])
        assert len(line) == 4
        assert line[0] < line[-1]  # ASCII 字符序随数值上升

    def test_flat_uses_middle_level(self):
        # levels 共 10 档，中间档为索引 5，即 "+"
        assert verify_env._sparkline([5, 5, 5]) == "+" * 3

    def test_width_limit_and_nan(self):
        line = verify_env._sparkline([float("nan")] + list(range(100)), width=10)
        assert len(line) == 10

    def test_empty(self):
        assert verify_env._sparkline([]) == ""


class TestCheckBacktestReport:
    @staticmethod
    def _acct(n=2):
        index = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=n, name="timestamp"))
        return pd.DataFrame({
            "input_money": [1000.0] * n,
            "all_value": [1000.0 + i * 10 for i in range(n)],
        }, index=index)

    def _patch(self, monkeypatch, acct_df, orders_df=None):
        class _Asc:
            def asc(self):
                return None

        class FakeAccountStats:
            trader_name = _FakeColumn()
            timestamp = _Asc()

            @staticmethod
            def query_data(**kwargs):
                return acct_df

        class FakeOrder:
            trader_name = _FakeColumn()

            @staticmethod
            def query_data(**kwargs):
                return orders_df if orders_df is not None else pd.DataFrame(
                    {"order_type": ["order_long"]}
                )

        monkeypatch.setattr(verify_env, "AccountStats", FakeAccountStats)
        monkeypatch.setattr(verify_env, "Order", FakeOrder)
        return FakeAccountStats

    def test_ok(self, monkeypatch, tmp_path):
        monkeypatch.setitem(verify_env.zvt_env, "tmp_path", str(tmp_path))
        self._patch(monkeypatch, self._acct())

        detail = verify_env.check_backtest_report()

        out = tmp_path / "backtest_report"
        assert (out / "account.csv").exists()
        assert (out / "orders.csv").exists()
        assert "快照 2 行" in detail

    def test_rerun_when_empty(self, monkeypatch, tmp_path):
        monkeypatch.setitem(verify_env.zvt_env, "tmp_path", str(tmp_path))
        state = {"calls": 0, "acct": self._acct()}

        class _Asc:
            def asc(self):
                return None

        class FakeAccountStats:
            trader_name = _FakeColumn()
            timestamp = _Asc()

            @staticmethod
            def query_data(**kwargs):
                # 第一次返回空，触发补跑；补跑（calls 变为 1）之后返回真实快照
                return state["acct"] if state["calls"] > 0 else pd.DataFrame()

        class FakeOrder:
            trader_name = _FakeColumn()

            @staticmethod
            def query_data(**kwargs):
                return pd.DataFrame({"order_type": ["order_long"]})

        def fake_backtest():
            state["calls"] += 1
            return "ok"

        monkeypatch.setattr(verify_env, "AccountStats", FakeAccountStats)
        monkeypatch.setattr(verify_env, "Order", FakeOrder)
        monkeypatch.setattr(verify_env, "check_backtest", fake_backtest)

        detail = verify_env.check_backtest_report()
        assert state["calls"] == 1      # 确实补跑了一次
        assert "快照 2 行" in detail


class TestPrepareExternalDf:
    def _raw(self):
        # 模拟 CSV 读回：timestamp 是字符串，provider 还是源库的值
        return pd.DataFrame({
            "id": ["stock_sz_000001_2024-01-02"],
            "timestamp": ["2024-01-02"],
            "close": [9.8],
            "provider": ["em"],
        })

    def test_timestamp_becomes_datetime(self):
        out = verify_env._prepare_external_df(self._raw(), "external_csv")
        assert pd.api.types.is_datetime64_any_dtype(out["timestamp"])

    def test_provider_overridden(self):
        out = verify_env._prepare_external_df(self._raw(), "external_csv")
        assert set(out["provider"]) == {"external_csv"}

    def test_id_preserved_and_input_untouched(self):
        raw = self._raw()
        out = verify_env._prepare_external_df(raw, "external_csv")
        assert "id" in out.columns
        assert raw.loc[0, "provider"] == "em"      # 原 df 不被修改
        assert isinstance(raw.loc[0, "timestamp"], str)


class TestImportExternal:
    def _patch(self, monkeypatch, df, saved=None):
        class FakeSchema:
            __tablename__ = "stock_1d_kdata"

            @staticmethod
            def query_data(**kwargs):
                return df

        calls = {}

        def fake_df_to_db(data, schema, provider, force_update=False):
            calls["provider"] = provider
            calls["rows"] = len(data)
            calls["force_update"] = force_update
            return calls["rows"] if saved is None else saved

        monkeypatch.setattr(verify_env, "df_to_db", fake_df_to_db)
        return FakeSchema, calls

    def test_ok(self, monkeypatch, tmp_path):
        df = pd.DataFrame({
            "id": ["a", "b"],
            "timestamp": ["2024-01-02", "2024-01-03"],
            "close": [1.0, 2.0],
            "provider": ["em", "em"],
        })
        schema, calls = self._patch(monkeypatch, df)
        exported, saved, csv_path = verify_env._import_external(
            schema, "external_csv", "stock_sz_000001",
            "2022-01-01", "2024-06-30", str(tmp_path),
        )
        assert (exported, saved) == (2, 2)
        assert calls["provider"] == "external_csv"
        assert calls["force_update"] is True
        assert Path(csv_path).exists()

    def test_saved_mismatch_raises(self, monkeypatch, tmp_path):
        df = pd.DataFrame({
            "id": ["a", "b"], "timestamp": ["2024-01-02", "2024-01-03"],
            "provider": ["em", "em"],
        })
        schema, _ = self._patch(monkeypatch, df, saved=1)
        with pytest.raises(AssertionError, match="入库 1 行"):
            verify_env._import_external(
                schema, "external_csv", "stock_sz_000001",
                "2022-01-01", "2024-06-30", str(tmp_path),
            )


class TestSyncBatch:
    def _schema(self, df):
        class FakeSchema:
            __tablename__ = "stock_1d_kdata"

            @staticmethod
            def query_data(**kwargs):
                return df

        return FakeSchema

    def test_new_rows_reported(self, monkeypatch):
        df = pd.DataFrame({
            "id": ["a", "b"], "timestamp": ["2024-01-02", "2024-01-03"],
            "provider": ["em", "em"],
        })
        seen = {}

        def fake_df_to_db(data, schema, provider, force_update=False):
            seen["provider"] = provider
            seen["force_update"] = force_update
            return 2

        monkeypatch.setattr(verify_env, "df_to_db", fake_df_to_db)
        added, source = verify_env._sync_batch(
            self._schema(df), "external_csv", "stock_sz_000001", "2024-01-01", "2024-01-31"
        )
        assert (added, source) == (2, 2)
        assert seen["provider"] == "external_csv"
        assert seen["force_update"] is False  # 增量模式：不覆盖已有行

    def test_empty_source_returns_zero(self, monkeypatch):
        monkeypatch.setattr(
            verify_env, "df_to_db", lambda *a, **k: pytest.fail("空数据不应调用 df_to_db")
        )
        added, source = verify_env._sync_batch(
            self._schema(pd.DataFrame()), "external_csv", "stock_sz_000001",
            "2024-01-01", "2024-01-31",
        )
        assert (added, source) == (0, 0)


class TestCheckRest:
    def test_ok(self, monkeypatch):
        class FakeResp:
            def raise_for_status(self):
                pass

            @staticmethod
            def json():
                return ["em", "joinquant"]

        monkeypatch.setattr("requests.get", lambda *a, **k: FakeResp())
        assert "providers=['em', 'joinquant']" in verify_env.check_rest()

    def test_empty_body(self, monkeypatch):
        class FakeResp:
            def raise_for_status(self):
                pass

            @staticmethod
            def json():
                return []

        monkeypatch.setattr("requests.get", lambda *a, **k: FakeResp())
        with pytest.raises(AssertionError, match="providers 返回为空"):
            verify_env.check_rest()

    def test_http_error(self, monkeypatch):
        class FakeResp:
            def raise_for_status(self):
                raise RuntimeError("500 Server Error")

        monkeypatch.setattr("requests.get", lambda *a, **k: FakeResp())
        with pytest.raises(RuntimeError, match="500"):
            verify_env.check_rest()
