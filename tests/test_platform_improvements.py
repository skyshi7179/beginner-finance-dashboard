"""Offline tests for dates, owned temporary files, strategies and explicit UI actions."""
from datetime import datetime, date
from pathlib import Path
from dataclasses import replace
import json

import numpy as np
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from src.date_controls import completed_end
from src.stock_data import session_cache as temp
from src.stock_data.cache import cache_paths,load_history_cache
from src.stock_data.management import archive_entry
from src.stock_data.errors import StockDataError
from src.backtest.engine import BacktestParams,Portfolio,execute_target,run_backtest
from src.backtest.strategies import StrategySpec,strategy_signals,parse_rules,DEFAULT_RULES,simple_rsi
from src.backtest.rule_assistant import draft_rules
from test_backtest_page import seed_cache, fixture_data

APP=Path(__file__).resolve().parents[1]/"app.py"
CALENDAR={"coverage_start":"2024-01-01","coverage_end":"2024-01-31",
          "eligible_session_dates":["2024-01-04","2024-01-05","2024-01-08","2024-01-09"]}


@pytest.mark.parametrize("when,expected",[("2024-01-08T10:00:00+08:00","2024-01-05"),
    ("2024-01-08T14:59:00+08:00","2024-01-05"),("2024-01-08T15:00:00+08:00","2024-01-08"),
    ("2024-01-07T16:00:00+08:00","2024-01-05"),("2024-01-10T16:00:00+08:00","2024-01-09"),
    ("2024-01-08T06:59:00+00:00","2024-01-05")])
def test_completed_date_handles_open_close_holidays_and_timezone(when,expected):
    result,note=completed_end(datetime.fromisoformat(when),CALENDAR)
    assert result.isoformat()==expected
    assert "日历" in note


def test_unverified_calendar_does_not_guess_weekdays():
    result,note=completed_end(datetime.fromisoformat("2030-01-08T16:00:00+08:00"),CALENDAR)
    assert result is None and "未覆盖" in note


def test_session_cleanup_expires_only_owned_inactive_directories(tmp_path):
    old,new="a"*32,"b"*32
    temp.touch(tmp_path,old,now=0,create=True)
    temp.touch(tmp_path,new,now=950,create=True)
    (tmp_path/old/"sample.csv").write_text("old")
    (tmp_path/"not-a-session").mkdir()
    (tmp_path/("c"*32)).mkdir()
    assert temp.sweep(tmp_path,now=1000,ttl=100)==[old]
    assert (tmp_path/new).exists() and (tmp_path/"not-a-session").exists() and (tmp_path/("c"*32)).exists()
    assert temp.clear_session(tmp_path,new)
    assert not temp.clear_session(tmp_path,"c"*32)


def test_session_rejects_traversal_symlinks_and_foreign_markers(tmp_path):
    with pytest.raises(ValueError):temp.session_directory(tmp_path,"../../stocks")
    outside=tmp_path/"permanent";outside.mkdir()
    (tmp_path/("a"*32)).symlink_to(outside,target_is_directory=True)
    assert not temp.clear_session(tmp_path,"a"*32)
    assert outside.exists()
    path=tmp_path/("b"*32);path.mkdir();(path/"lease.json").write_text('{}')
    assert not temp.clear_session(tmp_path,"b"*32)
    with pytest.raises(ValueError):temp.touch(tmp_path,"b"*32)


def test_lease_renewal_preserves_files_and_later_restart_sweep(tmp_path):
    sid="a"*32
    temp.touch(tmp_path,sid,now=1,create=True)
    (tmp_path/sid/"rows.csv").write_text("data")
    temp.touch(tmp_path,sid,now=590)
    assert temp.sweep(tmp_path,now=700)==[]
    assert temp.sweep(tmp_path,now=1200)==[sid]


def test_archive_moves_pair_without_touching_other_entries(tmp_path):
    data=tmp_path/"stocks";seed_cache(data)
    paths=cache_paths("600519",data_dir=data)
    before=[p.read_bytes() for p in paths]
    target=archive_entry(data,{"symbol":"600519","adjustment":"qfq","asset_type":"A股"},tmp_path/"recovery")
    assert not any(p.exists() for p in paths)
    assert [(target/p.name).read_bytes() for p in paths]==before
    with pytest.raises(StockDataError):archive_entry(data,{"symbol":"../../other","adjustment":"qfq","asset_type":"A股"},tmp_path/"recovery")


def test_grid_partial_rebalance_costs_and_no_negative_cash():
    params=BacktestParams(commission_rate=.001,slippage_rate=.001)
    p=Portfolio(100000)
    buy=execute_target(p,.5,100,params)
    assert buy["side"]=="BUY" and 0<p.shares<1000 and p.cash>0
    assert p.cash==pytest.approx(100000-500*100*1.001*1.001)
    previous=p.shares
    sell=execute_target(p,.2,100,params)
    assert sell["side"]=="SELL" and 0<p.shares<previous
    assert sell["commission"]>0
    execute_target(p,1,100,params)
    assert p.cash>=0
    execute_target(p,0,100,params)
    assert p.shares==0


@pytest.mark.parametrize("spec",[StrategySpec(),StrategySpec(kind="channel",period=5,exit_period=3),
    StrategySpec(kind="rsi",period=3),StrategySpec(kind="grid"),StrategySpec(kind="custom",rules=DEFAULT_RULES)])
def test_all_strategies_next_open_no_future_leak_and_reproducible(spec):
    data=fixture_data(90)
    result=run_backtest(data,strategy=spec)
    assert not result.equity.empty
    assert result.equity.strategy_cash.ge(0).all() and result.equity.strategy_shares.ge(0).all()
    assert (result.trades.execution_date>result.trades.signal_date).all()
    changed=data.copy();changed.loc[70:,["open","high","low","close"]]*=1.5
    future=run_backtest(changed,strategy=spec)
    pd.testing.assert_frame_equal(result.equity.iloc[:70],future.equity.iloc[:70])
    assert not set(data.columns)&{"signal","short_ma"}


def test_channel_uses_prior_prices_not_current_close():
    data=fixture_data(12);data["can_trade"]=True
    data["close"]=range(100,112)
    result=strategy_signals(data,StrategySpec(kind="channel",period=3,exit_period=2),BacktestParams())
    assert result.channel_high.iloc[3]==102
    assert result.signal.iloc[3]==1


def test_simple_rsi_handles_flat_up_down_and_missing():
    assert simple_rsi(pd.Series([1.,1.,1.,1.,1.]),3).iloc[-1]==50
    assert simple_rsi(pd.Series([1.,2.,3.,4.,5.]),3).iloc[-1]==100
    assert simple_rsi(pd.Series([5.,4.,3.,2.,1.]),3).iloc[-1]==0
    assert pd.isna(simple_rsi(pd.Series([1.,np.nan,3.,4.,5.]),3).iloc[-1])


@pytest.mark.parametrize("mutate",[lambda r:r.update(code="import os"),lambda r:r["buy"].update(logic="eval"),
    lambda r:r["buy"]["conditions"][0].update(op="exec"),
    lambda r:r["buy"]["conditions"][0].update(left={"field":"future_close"}),
    lambda r:r["buy"]["conditions"][0].update(left={"value":True}),
    lambda r:r["buy"]["conditions"][0].update(left={"value":10**1000}),
    lambda r:r["buy"]["conditions"][0].update(left={"field":"ma","window":-1})])
def test_custom_rules_reject_code_unknown_fields_and_invalid_values(mutate):
    rules=json.loads(json.dumps(DEFAULT_RULES));mutate(rules)
    with pytest.raises(ValueError):parse_rules(json.dumps(rules))


@pytest.mark.parametrize("text",['{"buy":{},"buy":{}}','{"buy":NaN}','```json\n{}\n```','[]','{"buy":Infinity}'])
def test_rule_json_rejects_unsafe_envelopes(text):
    with pytest.raises(ValueError):parse_rules(text)


def test_ai_rule_draft_validated_and_never_executes():
    calls=[]
    def model(config,messages):
        calls.append(messages);return json.dumps(DEFAULT_RULES)
    assert draft_rules("均线上穿进入、下穿退出",None,model_call=model)==DEFAULT_RULES
    assert len(calls)==1
    with pytest.raises(ValueError):draft_rules("执行python",None,model_call=lambda *a:'{"code":"print(1)"}')


def test_date_controls_separate_inputs_quick_range_and_no_fetch(monkeypatch):
    import src.stock_data.page as page
    monkeypatch.setattr(page,"get_stock_history",lambda *a,**k:pytest.fail("date changes must not download"))
    app=AppTest.from_file(APP,default_timeout=30).run()
    app.button(key="home_enter_data_acquisition").click().run()
    assert app.date_input(key="stock_history_dates_start") and app.date_input(key="stock_history_dates_end")
    app.checkbox(key="stock_history_dates_until").check().run()
    assert app.date_input(key="stock_history_dates_end").disabled
    app.selectbox(key="stock_history_dates_quick").select("近3个月").run()
    app.button(key="stock_history_dates_apply").click().run()
    begin,end=app.session_state["stock_history_dates"]
    assert (end-begin).days==90 and not app.exception


def test_management_empty_and_return(tmp_path,monkeypatch):
    import src.stock_data.management as page
    monkeypatch.setattr(page,"DATA_DIR",tmp_path/"stocks")
    app=AppTest.from_file(APP,default_timeout=30).run()
    app.button(key="home_enter_data_management").click().run()
    assert app.button(key="cache_end_session") and not app.exception
    app.button(key="cache_end_session").click().run()
    assert any("清理" in v.value for v in app.success)
    app.button(key="return_home_data_management").click().run()
    assert app.session_state["app_page"]=="home"


class State(dict):
    __getattr__=dict.__getitem__
    __setattr__=dict.__setitem__


def test_temp_download_reuses_service_and_does_not_modify_permanent(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import src.stock_data.workspace as work
    state=State()
    monkeypatch.setattr(work,"st",SimpleNamespace(session_state=state))
    monkeypatch.setattr(work,"start_cleaner",lambda root:None)
    directory=tmp_path/"stocks";seed_cache(directory)
    pair=cache_paths("600519",data_dir=directory)
    before=[p.read_bytes() for p in pair]
    calls=[]
    raw=fixture_data(100)
    def fetch(**kwargs):calls.append(kwargs);return raw
    result=work.get_stock_history("600519",raw.date.iloc[0],raw.date.iloc[-1],data_dir=directory,
        storage_mode=work.TEMPORARY,name="离线测试证券",market="上海",fetcher=fetch)
    assert result.status=="LIVE_FETCHED" and result.metadata["storage_scope"]=="本次临时"
    assert len(calls)==1 and [p.read_bytes() for p in pair]==before
    again=work.get_stock_history("600519",raw.date.iloc[0],raw.date.iloc[-1],data_dir=directory,
        storage_mode=work.TEMPORARY,name="离线测试证券",market="上海",fetcher=fetch)
    assert again.status=="CACHE" and len(calls)==1
    read=work.read_stock_history_cache("600519",raw.date.iloc[0],raw.date.iloc[-1],data_dir=directory)
    assert len(read.data)==100 and read.metadata["storage_scope"]=="本次临时"
    assert temp.clear_session(work.temporary_root(directory),state.temporary_cache_id)
    assert all(p.exists() for p in pair)


def test_failed_temp_download_keeps_existing_permanent_and_isolated_copy(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import src.stock_data.workspace as work
    monkeypatch.setattr(work,"st",SimpleNamespace(session_state=State()))
    monkeypatch.setattr(work,"start_cleaner",lambda root:None)
    directory=tmp_path/"stocks";seed_cache(directory)
    raw=fixture_data(100)
    result=work.get_stock_history("600519",raw.date.iloc[0],raw.date.iloc[-1],data_dir=directory,
        storage_mode=work.TEMPORARY,fetcher=lambda **kwargs:(_ for _ in ()).throw(RuntimeError("offline")))
    assert result.status=="STALE_CACHE" and len(result.data)==90
    assert len(load_history_cache("600519",data_dir=directory)[0])==90


def test_inline_dialog_requires_confirmation_and_runs_only_once(tmp_path,monkeypatch):
    import src.stock_data.inline_fetch as inline
    import src.backtest.page as page
    from src.stock_data.service import StockHistoryResult
    monkeypatch.setattr(page,"DATA_DIR",tmp_path/"stocks")
    calls=[]
    def fetch(*args,**kwargs):
        calls.append(kwargs)
        seed_cache(tmp_path/"stocks")
        return StockHistoryResult(pd.DataFrame(),"LIVE_FETCHED",{},"saved")
    monkeypatch.setattr(inline,"get_stock_history",fetch)
    app=AppTest.from_file(APP,default_timeout=30).run()
    app.session_state["research_context"]={"selected":{"symbol":"600519","name":"离线测试证券","market":"上海"},"asset_type":"A股","start":"2024-01-02","end":"2024-06-01","adjustment":"qfq"}
    app.button(key="home_enter_backtest").click().run()
    assert calls==[]
    app.button(key="backtest_inline_fetch").click().run()
    assert calls==[] and app.button(key="backtest_confirm_fetch")
    app.button(key="backtest_confirm_fetch").click().run()
    assert len(calls)==1 and not app.exception
    app.run()
    assert len(calls)==1


def test_strategy_change_invalidates_old_run_and_custom_requires_confirmation(tmp_path,monkeypatch):
    import src.backtest.page as page
    directory=tmp_path/"stocks";seed_cache(directory)
    monkeypatch.setattr(page,"DATA_DIR",directory)
    app=AppTest.from_file(APP,default_timeout=30).run()
    app.button(key="home_enter_backtest").click().run()
    next(b for b in app.button if b.label=="运行回测").click().run()
    app.selectbox(key="strategy_kind").select("channel").run()
    assert not app.get("plotly_chart")
    next(b for b in app.button if b.label=="运行回测").click().run()
    assert app.session_state["backtest_run_result"].metadata["strategy"]["kind"]=="channel"
    app.selectbox(key="strategy_kind").select("custom").run()
    assert next(b for b in app.button if b.label=="运行回测").disabled
    confirmation=next(c for c in app.checkbox if str(c.key).startswith("rules_confirm_"))
    confirmation.check().run()
    next(b for b in app.button if b.label=="运行回测").click().run()
    assert app.session_state["backtest_run_result"].metadata["strategy"]["kind"]=="custom"
    assert not app.exception


def test_preclose_cached_candle_is_refetched_after_close(tmp_path,monkeypatch):
    import src.stock_data.service as service
    from src.stock_data.cache import save_history_cache
    data=seed_cache(tmp_path)
    last=data.date.iloc[-1]
    now=datetime.fromisoformat(last+"T16:00:00+08:00")
    monkeypatch.setattr(service,"get_china_now",lambda:now)
    data.loc[data.date.eq(last),"fetched_at"]=last+"T10:00:00+08:00"
    save_history_cache(data,{"coverage_ranges":[{"start":data.date.iloc[0],"end":last}]},"600519",data_dir=tmp_path)
    assert service.read_stock_history_cache("600519",data.date.iloc[0],last,data_dir=tmp_path).status=="STALE_CACHE"
    calls=[]
    def fetch(**kwargs):
        calls.append(kwargs);return data[data.date.eq(last)].copy()
    result=service.get_stock_history("600519",data.date.iloc[0],last,data_dir=tmp_path,fetcher=fetch)
    assert result.status=="LIVE_FETCHED" and len(calls)==1
    assert calls[0]["start_date"]==last and calls[0]["end_date"]==last


def test_afterclose_empty_trading_day_not_permanently_cached(tmp_path,monkeypatch):
    import src.stock_data.service as service
    now=datetime.fromisoformat("2024-06-03T16:00:00+08:00")
    monkeypatch.setattr(service,"get_china_now",lambda:now)
    monkeypatch.setattr(service,"is_declared_trade_day",lambda d:True)
    calls=[]
    def fetch(**kwargs):calls.append(kwargs);return pd.DataFrame()
    for _ in range(2):
        result=service.get_stock_history("600519","2024-06-03","2024-06-03",data_dir=tmp_path,fetcher=fetch)
        assert result.status=="LIVE_FETCHED" and "尚未返回" in result.message
    assert len(calls)==2


def test_management_archive_restore_and_result_invalidation(tmp_path,monkeypatch):
    import src.stock_data.management as page
    directory=tmp_path/"stocks";seed_cache(directory)
    monkeypatch.setattr(page,"DATA_DIR",directory)
    app=AppTest.from_file(APP,default_timeout=30).run()
    app.session_state["ai_report_result"]="old"
    app.button(key="home_enter_data_management").click().run()
    next(c for c in app.checkbox if str(c.key).startswith("cache_manage_confirm_")).check().run()
    app.button(key="cache_archive").click().run()
    assert not cache_paths("600519",data_dir=directory)[0].exists()
    assert not app.exception
    with pytest.raises(KeyError):
        app.session_state["ai_report_result"]
    next(c for c in app.checkbox if str(c.key).startswith("cache_restore_confirm_")).check().run()
    app.button(key="cache_restore").click().run()
    assert cache_paths("600519",data_dir=directory)[0].exists()
    assert any("恢复" in s.value for s in app.success) and not app.exception


def test_promote_temp_cache_requires_confirmation_and_preserves_temp(tmp_path,monkeypatch):
    import src.stock_data.management as page
    directory=tmp_path/"stocks";sid="e"*32
    with temp.protect(tmp_path/"session_cache",sid) as session:
        seed_cache(session/"stocks")
    monkeypatch.setattr(page,"DATA_DIR",directory)
    app=AppTest.from_file(APP,default_timeout=30).run()
    app.session_state["temporary_cache_id"]=sid
    app.button(key="home_enter_data_management").click().run()
    assert app.button(key="cache_promote").disabled
    next(c for c in app.checkbox if str(c.key).startswith("cache_manage_confirm_")).check().run()
    app.button(key="cache_promote").click().run()
    assert not app.exception
    assert len(load_history_cache("600519",data_dir=directory)[0])==90
    assert (session/"stocks").exists()


def test_archive_rolls_back_if_second_move_fails(tmp_path,monkeypatch):
    import src.stock_data.management as page
    directory=tmp_path/"stocks";seed_cache(directory)
    pair=cache_paths("600519",data_dir=directory)
    original=[p.read_bytes() for p in pair]
    move=page.shutil.move;calls=[]
    def fail_second(*args,**kwargs):
        calls.append(args)
        if len(calls)==2:raise OSError("disk failure")
        return move(*args,**kwargs)
    monkeypatch.setattr(page.shutil,"move",fail_second)
    with pytest.raises(OSError):archive_entry(directory,{"symbol":"600519","adjustment":"qfq","asset_type":"A股"},tmp_path/"recovery")
    assert [p.read_bytes() for p in pair]==original


def test_chart_scroll_is_explicit_and_grid_selection_survives_home(tmp_path,monkeypatch):
    import src.backtest.page as page
    directory=tmp_path/"stocks";seed_cache(directory)
    monkeypatch.setattr(page,"DATA_DIR",directory)
    app=AppTest.from_file(APP,default_timeout=30).run()
    app.button(key="home_enter_backtest").click().run()
    app.selectbox(key="strategy_kind").select("grid").run()
    next(b for b in app.button if b.label=="运行回测").click().run()
    assert json.loads(app.get("plotly_chart")[0].proto.config)["scrollZoom"] is False
    app.button(key="zoom_backtest_nav_chart").click().run()
    assert json.loads(app.get("plotly_chart")[0].proto.config)["scrollZoom"] is True
    app.button(key="return_home_backtest").click().run()
    app.button(key="home_enter_backtest").click().run()
    assert app.selectbox(key="strategy_kind").value=="grid"
    assert len(app.get("plotly_chart"))==3 and not app.exception


def test_ai_rule_ui_only_calls_on_generate_and_hides_changed_description(tmp_path,monkeypatch):
    import src.backtest.page as page
    import src.backtest.strategy_controls as controls
    import src.ai_report.page as report_page
    from src.ai_report.models import ModelConfig
    directory=tmp_path/"stocks";seed_cache(directory)
    monkeypatch.setattr(page,"DATA_DIR",directory)
    monkeypatch.setattr(report_page,"load_models",lambda:([ModelConfig("mock","https://example.invalid/v1","not-a-real-key")],[]))
    calls=[]
    monkeypatch.setattr(controls,"draft_rules",lambda *args:(calls.append(args) or DEFAULT_RULES))
    app=AppTest.from_file(APP,default_timeout=30).run()
    app.button(key="home_enter_backtest").click().run()
    app.selectbox(key="strategy_kind").select("custom").run()
    app.text_area(key="rule_ai_description").set_value("MA5上穿MA20进入，下穿退出").run()
    assert calls==[]
    app.button(key="rule_ai_generate").click().run()
    assert len(calls)==1 and app.button(key="rule_ai_apply")
    app.run(); assert len(calls)==1
    app.text_area(key="rule_ai_description").set_value("改为其他条件").run()
    assert "rule_ai_apply" not in [b.key for b in app.button]
    assert len(calls)==1 and not app.exception


def test_custom_volume_rule_requires_units():
    from src.backtest.engine import BacktestInputError
    rules=json.loads(json.dumps(DEFAULT_RULES))
    rules["buy"]["conditions"][0]={"left":{"field":"volume"},"op":"gt","right":{"value":5}}
    with pytest.raises(BacktestInputError,match="成交量单位"):
        run_backtest(fixture_data(),strategy=StrategySpec(kind="custom",rules=rules))


def test_temp_copy_of_covered_cache_preserves_timestamp_and_does_not_fetch(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import src.stock_data.workspace as work
    state=State();monkeypatch.setattr(work,"st",SimpleNamespace(session_state=state))
    monkeypatch.setattr(work,"start_cleaner",lambda root:None)
    directory=tmp_path/"stocks";frame=seed_cache(directory)
    _,original=load_history_cache("600519",data_dir=directory)
    result=work.get_stock_history("600519",frame.date.iloc[0],frame.date.iloc[-1],data_dir=directory,
        storage_mode=work.TEMPORARY,fetcher=lambda **kwargs:pytest.fail("covered cache must not fetch"))
    assert result.status=="CACHE"
    assert result.metadata["cache_updated_at"]==original["cache_updated_at"]
    copied,_=load_history_cache("600519",data_dir=work.temporary_directory(directory))
    source,_=load_history_cache("600519",data_dir=directory)
    pd.testing.assert_frame_equal(copied,source)


def test_temp_write_failure_returns_old_permanent_without_network(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import src.stock_data.workspace as work
    monkeypatch.setattr(work,"st",SimpleNamespace(session_state=State()))
    monkeypatch.setattr(work,"start_cleaner",lambda root:None)
    monkeypatch.setattr(work,"protect",lambda *a,**k:(_ for _ in ()).throw(OSError("read only disk")))
    directory=tmp_path/"stocks";frame=seed_cache(directory)
    pair=cache_paths("600519",data_dir=directory);before=[p.read_bytes() for p in pair]
    result=work.get_stock_history("600519",frame.date.iloc[0],frame.date.iloc[-1],data_dir=directory,
        storage_mode=work.TEMPORARY,fetcher=lambda **kwargs:pytest.fail("must not fetch after failed storage setup"))
    assert result.status=="STALE_CACHE" and "未完成" in result.message
    assert len(result.data)==90 and [p.read_bytes() for p in pair]==before


def test_theme_css_preserves_primary_button_text_contrast(monkeypatch):
    import src.ui_experience as ui
    captured=[]
    monkeypatch.setattr(ui.st,"markdown",lambda html,**kwargs:captured.append(html))
    ui.inject_platform_css()
    assert "color: #172033 !important" in captured[0]
    assert "var(--background-color)" not in captured[0]
    assert "padding-top: 4.5rem" in captured[0]
