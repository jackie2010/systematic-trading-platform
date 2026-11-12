# -*- coding: utf-8 -*-
import importlib.util
import logging
import sys
import types
from pathlib import Path

import pandas as pd


def load_em_stock_meta_module():
    exchange = types.SimpleNamespace(sh="sh", sz="sz", bj="bj")

    zvt_module = types.ModuleType("zvt")
    contract_module = types.ModuleType("zvt.contract")
    contract_module.Exchange = exchange

    contract_api_module = types.ModuleType("zvt.contract.api")
    contract_api_module.df_to_db = lambda **kwargs: None

    contract_recorder_module = types.ModuleType("zvt.contract.recorder")

    class Recorder:
        pass

    contract_recorder_module.Recorder = Recorder

    domain_module = types.ModuleType("zvt.domain")

    class Stock:
        @staticmethod
        def query_data(**kwargs):
            return []

    domain_module.Stock = Stock

    recorders_module = types.ModuleType("zvt.recorders")
    em_module = types.ModuleType("zvt.recorders.em")
    em_api_module = types.ModuleType("zvt.recorders.em.em_api")
    em_api_module.get_tradable_list = lambda **kwargs: pd.DataFrame()
    em_api_module.get_basic_info = lambda **kwargs: None
    em_module.em_api = em_api_module

    pd_utils_module = types.ModuleType("zvt.utils.pd_utils")
    pd_utils_module.pd_is_not_null = lambda df: df is not None and not df.empty

    time_utils_module = types.ModuleType("zvt.utils.time_utils")
    time_utils_module.to_pd_timestamp = lambda value: value

    sys.modules["zvt"] = zvt_module
    sys.modules["zvt.contract"] = contract_module
    sys.modules["zvt.contract.api"] = contract_api_module
    sys.modules["zvt.contract.recorder"] = contract_recorder_module
    sys.modules["zvt.domain"] = domain_module
    sys.modules["zvt.recorders"] = recorders_module
    sys.modules["zvt.recorders.em"] = em_module
    sys.modules["zvt.recorders.em.em_api"] = em_api_module
    sys.modules["zvt.utils.pd_utils"] = pd_utils_module
    sys.modules["zvt.utils.time_utils"] = time_utils_module

    module_path = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "zvt"
        / "recorders"
        / "em"
        / "meta"
        / "em_stock_meta_recorder.py"
    )
    spec = importlib.util.spec_from_file_location("test_em_stock_meta_module", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_em_stock_recorder_uses_default_pagination(monkeypatch):
    module = load_em_stock_meta_module()
    calls = []

    def fake_get_tradable_list(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame(
            [
                {
                    "id": "stock_sh_600000",
                    "name": "PF BANK",
                    "total_cap": None,
                    "float_cap": None,
                }
            ]
        )

    recorder = object.__new__(module.EMStockRecorder)
    recorder.logger = logging.getLogger("test_em_stock_recorder")
    recorder.force_update = False
    recorder.session = object()
    recorder.http_session = object()

    monkeypatch.setattr(module.em_api, "get_tradable_list", fake_get_tradable_list)
    monkeypatch.setattr(module, "df_to_db", lambda **kwargs: None)
    monkeypatch.setattr(module.Stock, "query_data", lambda **kwargs: [])

    recorder.run()

    assert len(calls) == 3
    assert all(call["entity_type"] == "stock" for call in calls)
    assert all("limit" not in call for call in calls)
