"""Offline acceptance for the second novice UX/rule revision."""
from copy import deepcopy
from dataclasses import replace
from datetime import date
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from src.backtest.engine import BacktestParams,Portfolio,execute_order,execute_target,run_backtest
from src.backtest.strategies import StrategySpec,DEFAULT_RULES,VOLUME_PROFIT_RULES,validate_rules,evaluate_rule,parse_rules,explain_rules
from src.stock_data.provider import fetch_stock_history
from src.stock_data.turnover import normalize_turnover
from src.stock_data.units import canonical_turnover
from src.stock_analysis.indicators import calculate_indicators
from src.stock_analysis.charts import build_price_chart,build_amount_chart
from test_backtest_page import seed_cache,fixture_data

APP=Path(__file__).resolve().parents[1]/"app.py"


def bars(prices):
    values=np.asarray(prices,dtype=float)
    return pd.DataFrame({"date":pd.bdate_range("2024-01-02",periods=len(values)).strftime("%Y-%m-%d"),
        "open":values,"high":values+1,"low":values-1,"close":values,"volume":1000.,
        "volume_unit":"股","amount":10000.,"amount_unit":"元","asset_type":"A股"})


def condition(field,op,value):
    return {"left":{"field":field},"op":op,"right":{"value":value}}


def test_positive_arithmetic_mean_can_have_zero_or_negative_total_return():
    for closes in ([100,110,100],[100,120,99]):
        data=bars(closes)
        result=calculate_indicators(data)
        assert result.metrics["mean_daily_return"]>0
        assert result.metrics["cumulative_return"]<=0


@pytest.mark.parametrize("asset,unit,expected",[("A股","股",100.),("A股","手",10000.),("ETF","份",100.),("ETF","手",10000.)])
def test_canonical_units_explicit_lots_and_currency(asset,unit,expected):
    raw=bars([10,11]).assign(asset_type=asset,volume=100.,volume_unit=unit,amount=2.,amount_unit="万元")
    before=raw.copy(deep=True)
    result,warnings=canonical_turnover(raw)
    assert result.volume.tolist()==[expected,expected]
    assert result.amount.tolist()==[20000.,20000.]
    assert result.volume_unit.eq("份" if asset=="ETF" else "股").all()
    pd.testing.assert_frame_equal(raw,before)
    assert not warnings


def test_partial_units_and_missing_values_do_not_hide_good_days():
    data=bars([10,11,12,13]);data.loc[1,"volume_unit"]="UNKNOWN";data.loc[2,"volume"]=np.nan
    data.loc[3,"amount"]=np.nan
    result,warnings=canonical_turnover(data)
    assert result.volume.notna().tolist()==[True,False,False,True]
    analysis=calculate_indicators(result)
    assert len(analysis.data)==4
    figure=build_price_chart(analysis.data,moving_averages=())
    assert len(figure.data[1].x)==4 and pd.isna(figure.data[1].y[1])
    assert len(figure.data[2].x)==2 and "缺失" in figure.data[2].name
    assert pd.isna(build_amount_chart(result).data[0].y[3])
    assert warnings


def test_partial_supplement_keeps_valid_other_dates_and_fields():
    raw=bars([10,11,12]);raw["amount"]=raw["amount"].astype(object);raw.loc[1,"amount"]="bad";raw.loc[2,"volume"]=np.nan
    result=normalize_turnover(raw,"2024-01-02","2024-01-04",allow_partial=True)
    assert result.volume.iloc[0]==1000 and result.volume.iloc[1]==1000
    assert pd.isna(result.amount.iloc[1]) and pd.isna(result.volume.iloc[2])
    source=bars([10,11,12]);source["volume"]=10
    fetched=fetch_stock_history("600519","2024-01-02","2024-01-04",fetcher=lambda **kw:source.drop(columns="amount"),turnover_fetcher=lambda *a,**kw:raw)
    assert fetched.turnover_status.iloc[0]=="COMPLETE"
    assert fetched.turnover_status.iloc[1]=="PARTIAL"
    assert fetched.volume.iloc[1]==1000 and pd.isna(fetched.amount.iloc[1])


def test_missing_price_provider_volume_retains_price_candles():
    raw=bars([10,11,12]);raw.loc[1,"volume"]=np.nan
    fetched=fetch_stock_history("600519","2024-01-02","2024-01-04",fetcher=lambda **kw:raw)
    assert len(fetched)==3 and pd.isna(fetched.volume.iloc[1])


def test_nested_logic_and_three_valued_missing():
    data=bars([10,11,12]).assign(can_trade=True)
    a=condition("close","gt",10);b=condition("close","lt",12);c=condition("volume","gt",2000)
    node={"logic":"all","conditions":[a,{"logic":"any","conditions":[b,c]}]}
    rules={"buy":node,"sell":DEFAULT_RULES["sell"]};validate_rules(rules)
    assert evaluate_rule(data,node).tolist()==[False,True,False]
    data.loc[1,"volume"]=np.nan
    unknown={"logic":"all","conditions":[a,c]}
    assert pd.isna(evaluate_rule(data,unknown).iloc[1])
    assert "（" in explain_rules(rules)["buy"] and " 或 " in explain_rules(rules)["buy"]


@pytest.mark.parametrize("change",[
    lambda r:r["buy"]["conditions"][0]["right"].update(lag=-1),
    lambda r:r["buy"]["conditions"][0]["right"].update(scale=float("inf")),
    lambda r:r["buy"]["conditions"][0].update(right={"field":"close"}),
    lambda r:r["sell"]["conditions"][0].update(op="cross_above"),
    lambda r:r["sell"]["conditions"][0]["left"].update(lag=1),
])
def test_nested_schema_rejects_future_units_and_account_history(change):
    rules=deepcopy(VOLUME_PROFIT_RULES);change(rules)
    with pytest.raises(ValueError):validate_rules(rules)


def test_nested_depth_limit_and_backward_compatibility():
    assert parse_rules(json.dumps(DEFAULT_RULES))==DEFAULT_RULES
    group=deepcopy(DEFAULT_RULES["buy"])
    for _ in range(4):group={"logic":"all","conditions":[group]}
    with pytest.raises(ValueError,match="层"):
        validate_rules({"buy":group,"sell":DEFAULT_RULES["sell"]})


def test_volume_twice_and_up_exact_boundary_zero_previous_and_candle_difference():
    data=bars([10,11,12,11]).assign(can_trade=True)
    data["volume"]=[100,200,399,800]
    # A negative candle can still close above the previous close.
    data.loc[1,"open"]=11.5
    assert evaluate_rule(data,VOLUME_PROFIT_RULES["buy"]).iloc[1] == True
    assert evaluate_rule(data,VOLUME_PROFIT_RULES["buy"]).iloc[2] == False
    assert evaluate_rule(data,VOLUME_PROFIT_RULES["buy"]).iloc[3] == False
    data.loc[0,"volume"]=0
    assert pd.isna(evaluate_rule(data,VOLUME_PROFIT_RULES["buy"]).iloc[1])


def test_cost_basis_actual_fills_fees_partial_sale_and_new_trade_reset():
    params=BacktestParams(commission_rate=.001,slippage_rate=.001)
    account=Portfolio(100000)
    fill=execute_order(account,"BUY",100,params)
    assert account.cost_basis==pytest.approx(fill["gross_value"]+fill["commission"])
    assert account.close_state(100,0)["position_return"]<0
    old_cost,old_shares=account.cost_basis,account.shares
    execute_target(account,.4,100,params)
    assert account.cost_basis==pytest.approx(old_cost*account.shares/old_shares)
    execute_order(account,"SELL",100,params)
    assert account.cost_basis==0 and account.close_state(100,1)["position_return"] is None
    fill=execute_order(account,"BUY",110,params)
    assert account.cost_basis==pytest.approx(fill["gross_value"]+fill["commission"])
    rejected=Portfolio(1);execute_order(rejected,"BUY",100,params)
    assert rejected.cost_basis==0 and rejected.shares==0


@pytest.mark.parametrize("close,gap,op,threshold",[(11.1,10.8,"gte",.1),(8.9,8.7,"lte",-.1)])
def test_close_threshold_next_open_gap_and_profit_is_position_based(close,gap,op,threshold):
    data=bars([10,10,close,gap,10,10])
    rules={"buy":{"logic":"all","conditions":[condition("close","gt",0)]},
           "sell":{"logic":"any","conditions":[condition("position_return",op,threshold)]}}
    result=run_backtest(data,BacktestParams(short_window=1,long_window=2,commission_rate=0,slippage_rate=0),strategy=StrategySpec(kind="custom",rules=rules))
    assert result.trades.side.iloc[:2].tolist()==["BUY","SELL"]
    assert result.trades.signal_date.iloc[1]==pd.Timestamp(data.date.iloc[2])
    assert result.trades.execution_date.iloc[1]==pd.Timestamp(data.date.iloc[3])
    assert result.trades.execution_price.iloc[1]==pytest.approx(gap/10*100)
    assert result.equity.strategy_cost_basis.iloc[3]==0
    assert pd.isna(result.equity.position_return.iloc[3])


def test_complete_example_no_future_leak_and_missing_volume_gap():
    raw=fixture_data(90).assign(volume_unit="股",amount_unit="元",asset_type="A股")
    raw.loc[25,"volume"]=np.nan
    spec=StrategySpec(kind="custom",rules=VOLUME_PROFIT_RULES)
    before=run_backtest(raw,strategy=spec)
    assert "不可判断" in before.equity.rule_status.iloc[25]
    changed=raw.copy();changed.loc[70:,["open","high","low","close"]]*=1.8;changed.loc[70:,"volume"]*=3
    after=run_backtest(changed,strategy=spec)
    pd.testing.assert_frame_equal(before.equity.iloc[:70],after.equity.iloc[:70])
    assert (before.trades.execution_date>before.trades.signal_date).all()


def open_page(page,tmp_path,monkeypatch):
    import src.backtest.page as backtest
    import src.stock_analysis.page as visual
    seed_cache(tmp_path)
    monkeypatch.setattr(backtest,"DATA_DIR",tmp_path);monkeypatch.setattr(visual,"DATA_DIR",tmp_path)
    import requests
    monkeypatch.setattr(requests,"get",lambda *a,**k:pytest.fail("UI changes must not download"))
    app=AppTest.from_file(APP,default_timeout=30).run()
    app.button(key=f"home_enter_{page}").click().run()
    return app


def test_independent_chart_zoom_and_reset_do_not_run_data_or_backtest(tmp_path,monkeypatch):
    app=open_page("visualization",tmp_path,monkeypatch)
    assert any("平均日收益为正，区间最终收益仍可能为零或负数" in item.value for item in app.info)
    app.button(key="zoom_visualization_price_chart").click().run()
    assert [json.loads(c.proto.config)["scrollZoom"] for c in app.get("plotly_chart")]==[True,False,False,False]
    app.button(key="zoom_visualization_daily_return").click().run()
    assert [json.loads(c.proto.config)["scrollZoom"] for c in app.get("plotly_chart")]==[False,True,False,False]
    before=app.get("plotly_chart")[1].proto.spec
    app.button(key="reset_visualization_daily_return").click().run()
    assert before!=app.get("plotly_chart")[1].proto.spec and not app.exception


def test_all_cached_range_and_all_history_only_change_dates(tmp_path,monkeypatch):
    app=open_page("visualization",tmp_path,monkeypatch)
    app.selectbox(key="visualization_dates_quick").select("全部已缓存区间").run()
    app.button(key="visualization_dates_apply").click().run()
    assert not app.exception
    app.button(key="return_home_visualization").click().run()
    app.button(key="home_enter_data_acquisition").click().run()
    control=app.selectbox(key="stock_history_dates_quick")
    all_label=next(v for v in control.options if v.startswith("全部历史"))
    control.select(all_label).run();app.button(key="stock_history_dates_apply").click().run()
    assert app.session_state["stock_history_dates"][0]==date(1990,1,1)
    assert not app.exception


def test_visual_editor_example_nested_groups_percent_and_home_persistence(tmp_path,monkeypatch):
    app=open_page("backtest",tmp_path,monkeypatch)
    app.selectbox(key="strategy_kind").select("custom").run()
    app.button(key="rules_load_volume_example").click().run()
    assert not app.exception
    assert app.number_input(key="rule_edit_v1_buy_0_right_lag").value==1
    assert app.number_input(key="rule_edit_v1_buy_0_right_scale").value==2
    assert app.number_input(key="rule_edit_v1_sell_0_right_value").value==10
    assert app.number_input(key="rule_edit_v1_sell_1_right_value").value==-10
    assert any("10.00%" in v.value for v in app.markdown)
    next(c for c in app.checkbox if str(c.key).startswith("rules_confirm_")).check().run()
    next(b for b in app.button if b.label=="运行回测").click().run()
    assert app.session_state["backtest_run_result"].metadata["strategy"]["rules"]==VOLUME_PROFIT_RULES
    app.selectbox(key="rule_edit_v1_buy_1_type").select("子条件组").run()
    assert app.selectbox(key="rule_edit_v1_buy_1_logic")
    assert next(b for b in app.button if b.label=="运行回测").disabled
    app.button(key="return_home_backtest").click().run();app.button(key="home_enter_backtest").click().run()
    assert app.selectbox(key="rule_edit_v1_buy_1_type").value=="子条件组" and not app.exception


def test_ai_assistant_accepts_new_schema_offline():
    from src.backtest.rule_assistant import draft_rules
    calls=[]
    def model(config,messages):
        calls.append(messages)
        return json.dumps(VOLUME_PROFIT_RULES)
    assert draft_rules("放量上涨，盈利或亏损达到10%或MA死叉退出",None,model_call=model)==VOLUME_PROFIT_RULES
    assert "版本2" in calls[0][0]["content"]


@pytest.mark.parametrize("price,op,threshold",[(11.,"gte",.1),(9.,"lte",-.1)])
def test_exact_ten_percent_threshold_tolerates_float_rounding(price,op,threshold):
    from src.backtest.strategies import evaluate_at,market_operands
    data=bars([10,10,price,10]).assign(can_trade=True)
    rules={"buy":DEFAULT_RULES["buy"],"sell":{"logic":"all","conditions":[condition("position_return",op,threshold)]}}
    operands=market_operands(data,rules)
    assert evaluate_at(rules["sell"],2,operands,{"position_return":price/10-1}) is True


def test_comparisons_and_undefined_holdings_use_three_valued_logic():
    from src.backtest.strategies import evaluate_at,market_operands
    data=bars([10,11,12]).assign(can_trade=True)
    node={"logic":"any","conditions":[condition("position_return","gte",.1),condition("close","eq",11)]}
    rules={"buy":node,"sell":DEFAULT_RULES["sell"]}
    values=market_operands(data,rules)
    assert evaluate_at(node,0,values,{"position_return":None}) is None
    assert evaluate_at(node,1,values,{"position_return":None}) is True
    assert evaluate_rule(data,node).iloc[1]==True


def test_known_missing_session_excluded_from_daily_stats_but_total_preserved():
    data=bars([100,110,100]);data["date"]=["2024-01-02","2024-01-04","2024-01-05"]
    result=calculate_indicators(data)
    assert pd.isna(result.data.daily_return.iloc[1])
    assert result.data.observation_return.iloc[1]==pytest.approx(.1)
    assert result.metrics["mean_daily_return"]==pytest.approx(100/110-1)
    assert result.metrics["cumulative_return"]==0
    assert any("缺失交易日" in w for w in result.warnings)


def test_lag_does_not_jump_across_a_known_missing_session():
    data=bars([10,11,12,13]).assign(volume=[100,300,1000,4000])
    data["date"]=["2024-01-02","2024-01-04","2024-01-05","2024-01-08"]
    rules={"buy":VOLUME_PROFIT_RULES["buy"],"sell":{"logic":"any","conditions":[condition("position_return","gte",1)]}}
    result=run_backtest(data,BacktestParams(short_window=1,long_window=2),strategy=StrategySpec(kind="custom",rules=rules))
    assert "不可判断" in result.equity.rule_status.iloc[1]
    assert result.equity.signal.iloc[1]==0


def test_all_cached_does_not_keep_today_override_and_manual_change_leaves_all(tmp_path,monkeypatch):
    app=open_page("visualization",tmp_path,monkeypatch)
    app.checkbox(key="visualization_dates_until").check().run()
    app.selectbox(key="visualization_dates_quick").select("全部已缓存区间").run()
    app.button(key="visualization_dates_apply").click().run()
    assert not app.checkbox(key="visualization_dates_until").value
    assert app.session_state["visualization_dates"][1].year==2024
    app.date_input(key="visualization_dates_start").set_value(date(2024,1,9)).run()
    assert app.selectbox(key="visualization_dates_quick").value=="自定义"
    assert not app.exception


def test_provider_full_history_paging_and_nonprogress_error(monkeypatch):
    import src.stock_data.provider as provider
    from src.stock_data.errors import StockProviderError
    monkeypatch.setattr(provider,"PAGE_SIZE",2)
    pages=[[["2024-01-04","1","1","2",".5","1"],["2024-01-05","1","1","2",".5","1"]],
           [["2024-01-02","1","1","2",".5","1"],["2024-01-03","1","1","2",".5","1"]],[]]
    calls=[]
    class Response:
        def __init__(self,rows):self.rows=rows
        def raise_for_status(self):pass
        def json(self):return {"data":{"sh600519":{"qfqday":self.rows}}}
    def get(*args,**kwargs):
        calls.append(kwargs);return Response(pages.pop(0))
    result=provider.fetch_tencent_history("600519","1990-01-01","2024-01-05",adjustment="qfq",market="上海",http_get=get)
    assert result.date.tolist()==["2024-01-02","2024-01-03","2024-01-04","2024-01-05"]
    assert len(calls)==3 and ",2024-01-03,2,qfq" in calls[1]["params"]["param"]
    repeated=[["2024-01-04","1","1","2",".5","1"],["2024-01-05","1","1","2",".5","1"]]
    with pytest.raises(StockProviderError,match="未向过去推进"):
        provider.fetch_tencent_history("600519","1990-01-01","2024-01-05",adjustment="qfq",market="上海",http_get=lambda *a,**k:Response(repeated))


def test_volume_unit_mixture_only_excludes_unknown_rows_in_backtest():
    raw=bars([10,11,12,13,14,15]);raw.loc[2,"volume_unit"]="UNKNOWN"
    rules={"buy":{"logic":"all","conditions":[condition("volume","gt",5)]},"sell":{"logic":"all","conditions":[condition("position_return","gte",.1)]}}
    result=run_backtest(raw,BacktestParams(short_window=1,long_window=2),strategy=StrategySpec(kind="custom",rules=rules))
    assert result.equity.can_trade.tolist()==[True,True,False,True,True,True]
    assert not result.trades.empty


def test_chart_chinese_scaled_units_do_not_change_storage():
    raw=bars([10,11]).assign(volume=100000.,amount=500000000.)
    price=build_price_chart(calculate_indicators(raw).data,moving_averages=())
    assert price.data[1].name=="成交量（万股）"
    assert price.data[1].y[0]==10
    amount=build_amount_chart(raw)
    assert amount.data[0].name=="成交额（亿元）" and amount.data[0].y[0]==5
    assert raw.volume.iloc[0]==100000


def test_custom_form_does_not_require_irrelevant_ma_and_json_survives_home(tmp_path,monkeypatch):
    app=open_page("backtest",tmp_path,monkeypatch)
    app.selectbox(key="strategy_kind").select("custom").run()
    assert "backtest_short" not in [n.key for n in app.number_input]
    app.radio(key="custom_rule_mode").set_value("JSON规则").run()
    text=json.dumps(VOLUME_PROFIT_RULES,ensure_ascii=False)
    app.text_area(key="custom_rules_json").set_value(text).run()
    app.button(key="return_home_backtest").click().run();app.button(key="home_enter_backtest").click().run()
    assert json.loads(app.text_area(key="custom_rules_json").value)==VOLUME_PROFIT_RULES
    assert not app.exception
