"""Hand-checkable fixtures; no external data providers are used here."""

import numpy as np
import pandas as pd
import pytest

from src.backtest.engine import (BacktestInputError, BacktestParams, Portfolio, execute_order,
                                  prepare_bars, generate_ma_signals, run_backtest)
from src.backtest.metrics import portfolio_statistics


def bars(closes=(10, 8, 12, 9, 6, 8, 10), opens=None):
    close = np.asarray(closes, dtype=float)
    opening = np.asarray(opens, dtype=float) if opens is not None else close.copy()
    return pd.DataFrame({"date": pd.bdate_range("2024-01-02", periods=len(close)),
                         "open": opening, "high": np.maximum(opening, close) + 1,
                         "low": np.minimum(opening, close) - 1, "close": close,
                         "volume": 1000, "symbol": "600519", "adjustment": "qfq", "source": "FIXTURE"})


def config(**kwargs):
    return BacktestParams(short_window=1, long_window=2, commission_rate=0, slippage_rate=0, **kwargs)


def test_golden_and_death_cross_and_next_open_execution():
    result = run_backtest(bars(opens=[10, 8, 12, 20, 5, 8, 10]), config())
    assert result.equity["signal"].tolist() == [0, 0, 1, -1, 0, 1, 0]
    first_buy, first_sell = result.trades.iloc[0], result.trades.iloc[1]
    assert first_buy["signal_date"] == result.equity.loc[2, "date"]
    assert first_buy["execution_date"] == result.equity.loc[3, "date"]
    assert first_buy["execution_price"] == 200  # Next OPEN 20, not signal CLOSE 12.
    assert first_sell["execution_date"] == result.equity.loc[4, "date"]
    assert first_sell["execution_price"] == 50
    assert (result.trades["execution_date"] > result.trades["signal_date"]).all()
    assert result.equity.loc[0, "strategy_shares"] == 0
    assert result.equity.loc[0, "strategy_cash"] == 100000


def test_commission_slippage_cash_and_odd_lot_full_sale():
    params = BacktestParams(commission_rate=.001, slippage_rate=.01)
    portfolio = Portfolio(100000)
    buy = execute_order(portfolio, "BUY", 100, params)
    assert buy["quantity"] == 900
    assert buy["execution_price"] == pytest.approx(101)
    assert buy["commission"] == pytest.approx(90.9)
    assert buy["slippage_cost"] == pytest.approx(900)
    assert portfolio.cash == pytest.approx(9009.1)
    sale = execute_order(portfolio, "SELL", 110, params)
    assert sale["commission"] == pytest.approx(98.01)
    assert portfolio.cash == pytest.approx(106921.09)
    assert portfolio.shares == 0
    odd = Portfolio(0, shares=99)
    assert execute_order(odd, "SELL", 100, params)["quantity"] == 99
    assert odd.shares == 0


def test_no_initial_above_ma_buy_and_no_trades_for_flat_series():
    for fixture in [bars([10, 10, 10, 10]), bars([10, 11, 12, 13])]:
        result = run_backtest(fixture, config())
        assert result.trades.empty
        assert result.metrics.loc["双均线策略", "cumulative_return"] == 0
        assert result.metrics.loc["双均线策略", "trade_count"] == 0
        assert pd.isna(result.metrics.loc["双均线策略", "sharpe_ratio"])


def test_insufficient_cash_rejects_without_negative_account():
    result = run_backtest(bars(), config(initial_cash=1000))
    assert result.trades.empty
    assert "INSUFFICIENT_CASH" in result.orders["reason"].tolist()
    assert result.equity["strategy_cash"].eq(1000).all()
    assert result.equity["strategy_shares"].eq(0).all()
    assert result.benchmark_orders.iloc[0]["reason"] == "INSUFFICIENT_CASH"


def test_last_day_signal_unfilled_and_final_position_is_not_force_sold():
    last = run_backtest(bars([10, 8, 12]), config())
    assert last.orders.iloc[-1]["status"] == "UNFILLED_END"
    assert pd.isna(last.orders.iloc[-1]["execution_date"])
    assert last.trades.empty
    held = run_backtest(bars([10, 8, 12, 13]), config())
    assert len(held.trades) == 1
    assert held.equity.iloc[-1]["strategy_shares"] > 0
    assert held.metadata["force_liquidation"] is False


@pytest.mark.parametrize("change", [lambda df: df.assign(volume=[1000, 1000, 1000, 0, 1000, 1000, 1000]),
                                     lambda df: df.assign(open=[10, 8, 12, np.nan, 6, 8, 10])])
def test_untradable_next_bar_cancels_order_without_deferred_fill(change):
    result = run_backtest(change(bars()), config())
    assert result.orders.iloc[0]["status"] == "CANCELLED"
    assert result.orders.iloc[0]["reason"] == "UNTRADABLE_NEXT_BAR"
    assert pd.isna(result.orders.iloc[0]["execution_date"])
    assert result.equity.loc[3, "strategy_shares"] == 0
    assert result.equity.loc[4, "strategy_shares"] == 0


def test_missing_declared_session_cancels_and_exchange_holiday_gap_is_allowed():
    fixture = bars()
    fixture.loc[3:, "date"] += pd.Timedelta(days=20)
    result = run_backtest(fixture, config())
    assert result.orders.iloc[0]["reason"] == "MISSING_TRADING_SESSION"
    ordinary = run_backtest(bars(), config())
    assert ordinary.orders.iloc[0]["status"] == "FILLED"
    holiday = bars([10, 8, 12, 13], opens=[10, 8, 12, 20])
    holiday["date"] = pd.to_datetime(["2024-09-26", "2024-09-27", "2024-09-30", "2024-10-08"])
    holiday_result = run_backtest(holiday, config())
    assert holiday_result.orders.iloc[0]["status"] == "FILLED"
    assert holiday_result.orders.iloc[0]["execution_date"] == pd.Timestamp("2024-10-08")
    assert holiday_result.equity.loc[3, "session_gap_status"] == "CONTIGUOUS"


def test_portfolio_metrics_include_initial_capital_and_first_day_cost():
    metrics, curve = portfolio_statistics(pd.Series([99., 120., 90., 60.]), 100, trade_count=2)
    assert curve["daily_return"].iloc[0] == pytest.approx(-.01)
    assert metrics["cumulative_return"] == pytest.approx(-.4)
    assert metrics["max_drawdown"] == pytest.approx(.5)
    assert metrics["annualized_return"] == pytest.approx(.6 ** (252 / 4) - 1)
    assert metrics["annualized_volatility"] == pytest.approx(curve["daily_return"].std(ddof=1) * np.sqrt(252))
    assert metrics["sharpe_ratio"] == pytest.approx(curve["daily_return"].mean() / curve["daily_return"].std(ddof=1) * np.sqrt(252))
    assert metrics["trade_count"] == 2
    loss, _ = portfolio_statistics(pd.Series([99., 90.]), 100)
    assert loss["max_drawdown"] == pytest.approx(.1)


def test_buy_and_hold_reference_first_open_same_costs_and_unrealized_end():
    result = run_backtest(bars([10, 11, 12, 13]), config())
    assert result.metrics.loc["买入持有", "cumulative_return"] == pytest.approx(.3)
    assert result.metrics.loc["买入持有", "trade_count"] == 1
    assert result.benchmark_orders.iloc[0]["execution_date"] == result.equity.loc[0, "date"]
    assert result.equity.loc[3, "benchmark_shares"] == 1000
    with_cost = run_backtest(bars([10, 10, 10, 10]), BacktestParams(short_window=1, long_window=2))
    assert with_cost.metrics.loc["买入持有", "cumulative_return"] < 0
    assert with_cost.equity.loc[0, "benchmark_drawdown"] < 0


def test_no_lookahead_signals_and_account_prefix_are_unchanged():
    raw = bars()
    before = raw.copy(deep=True)
    prefix = run_backtest(raw.iloc[:5], config())
    extended = run_backtest(pd.concat([raw, bars([10]).assign(date=pd.Timestamp("2024-01-20"), close=10000, high=10001)]), config())
    columns = ["short_ma", "long_ma", "signal", "strategy_cash", "strategy_shares", "strategy_equity", "strategy_nav"]
    pd.testing.assert_frame_equal(prefix.equity[columns], extended.equity[columns].iloc[:5].reset_index(drop=True))
    pd.testing.assert_frame_equal(raw, before)
    future_open = raw.copy()
    future_open.loc[3, ["open", "high"]] = [10000, 10001]
    changed = run_backtest(future_open, config())
    pd.testing.assert_series_equal(changed.equity["signal"], run_backtest(raw, config()).equity["signal"])


@pytest.mark.parametrize("fixture", [pd.DataFrame(), bars([10, 9]), bars().assign(close=np.nan),
                                      bars().assign(open=[np.nan, 8, 12, 9, 6, 8, 10]),
                                      bars().assign(adjustment=["qfq", "none", "qfq", "qfq", "qfq", "qfq", "qfq"])])
def test_empty_short_invalid_and_mixed_inputs_report_clear_errors(fixture):
    with pytest.raises(BacktestInputError):
        run_backtest(fixture, config())


@pytest.mark.parametrize("params", [BacktestParams(short_window=20, long_window=5), BacktestParams(short_window=2.5),
                                    BacktestParams(initial_cash=-1), BacktestParams(commission_rate=np.nan),
                                    BacktestParams(slippage_rate=.5)])
def test_invalid_parameters(params):
    with pytest.raises(BacktestInputError):
        params.validate()
