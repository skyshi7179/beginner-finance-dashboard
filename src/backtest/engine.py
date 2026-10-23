"""Deterministic close-signal / next-open research backtest.

All OHLC are rebased to first open=100. Cash, quantities and costs are research
units, not executable real shares or CNY. Adjusted prices do not model actual
dividends, splits, price limits, liquidity or broker cash flows.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

from src.backtest.metrics import portfolio_statistics


class BacktestInputError(ValueError):
    """Invalid parameters or insufficient/ambiguous historical data."""


@dataclass(frozen=True)
class BacktestParams:
    short_window: int = 5
    long_window: int = 20
    initial_cash: float = 100_000.0
    commission_rate: float = 0.0003
    slippage_rate: float = 0.0005
    risk_free_rate: float = 0.0
    lot_size: int = 100
    max_calendar_gap: int = 7

    def validate(self) -> None:
        if any(not isinstance(value, int) or isinstance(value, bool) for value in (self.short_window, self.long_window)):
            raise BacktestInputError("均线窗口必须为整数。")
        if not (1 <= self.short_window < self.long_window <= 250):
            raise BacktestInputError("要求 1 ≤ 短期窗口 < 长期窗口 ≤ 250。")
        if not np.isfinite(self.initial_cash) or not 0 < self.initial_cash <= 1e12:
            raise BacktestInputError("初始研究资金必须大于0且不超过1e12。")
        for name, value, maximum in (("手续费", self.commission_rate, .01), ("滑点", self.slippage_rate, .05), ("无风险利率", self.risk_free_rate, .2)):
            if not np.isfinite(value) or not 0 <= value <= maximum:
                raise BacktestInputError(f"{name}参数不在支持范围内。")
        if self.lot_size != 100 or self.max_calendar_gap != 7:
            raise BacktestInputError("第一版固定使用100模拟单位买入；兼容参数max_calendar_gap保持为7，但连续性由交易日日历判定。")


@dataclass
class Portfolio:
    cash: float
    shares: int = 0
    cost_basis: float = 0.0
    entry_index: int | None = None

    def record_fill(self, side, quantity, gross, fee):
        if side == "BUY":
            self.cost_basis += gross + fee
        else:
            previous_shares = self.shares + quantity
            self.cost_basis *= self.shares / previous_shares if previous_shares else 0.
            if not self.shares:
                self.cost_basis = 0.
                self.entry_index = None

    def close_state(self, close, index):
        if not self.shares or self.cost_basis <= 0:
            return {"position_return": None, "holding_days": None}
        return {"position_return": self.shares * close / self.cost_basis - 1,
                "holding_days": index - self.entry_index + 1 if self.entry_index is not None else 1}


@dataclass
class BacktestResult:
    equity: pd.DataFrame
    orders: pd.DataFrame
    trades: pd.DataFrame
    benchmark_orders: pd.DataFrame
    metrics: pd.DataFrame
    params: BacktestParams
    warnings: list[str]
    metadata: dict


ORDER_COLUMNS = ["signal_date", "scheduled_date", "execution_date", "side", "status", "reason",
                 "quantity", "reference_open", "execution_price", "gross_value", "commission", "slippage_cost",
                 "cash_after", "shares_after"]


def execute_order(portfolio: Portfolio, side: str, opening: float, params: BacktestParams) -> dict:
    """All-in lot-rounded buy / full liquidation sell, including odd remainder."""
    if side not in {"BUY", "SELL"} or not np.isfinite(opening) or opening <= 0:
        raise BacktestInputError("订单方向或模拟开盘价无效。")
    price = opening * (1 + params.slippage_rate if side == "BUY" else 1 - params.slippage_rate)
    record = {"status": "FILLED", "reason": "", "quantity": 0, "reference_open": opening,
              "execution_price": price, "gross_value": 0.0, "commission": 0.0, "slippage_cost": 0.0}
    if side == "BUY":
        quantity = int(np.floor(portfolio.cash / (price * (1 + params.commission_rate)) / params.lot_size)) * params.lot_size
        if portfolio.shares:
            record.update(status="IGNORED", reason="ALREADY_LONG")
        elif quantity == 0:
            record.update(status="REJECTED", reason="INSUFFICIENT_CASH")
        else:
            gross = quantity * price
            fee = gross * params.commission_rate
            portfolio.cash -= gross + fee
            portfolio.shares += quantity
            record.update(quantity=quantity, gross_value=gross, commission=fee, slippage_cost=quantity * (price - opening))
    elif portfolio.shares == 0:
        record.update(status="IGNORED", reason="ALREADY_FLAT")
    else:
        quantity = portfolio.shares
        gross = quantity * price
        fee = gross * params.commission_rate
        portfolio.cash += gross - fee
        portfolio.shares = 0
        record.update(quantity=quantity, gross_value=gross, commission=fee, slippage_cost=quantity * (opening - price))
    if record["status"] == "FILLED" and record["quantity"]:
        portfolio.record_fill(side, record["quantity"], record["gross_value"], record["commission"])
    if portfolio.cash < -1e-7 or portfolio.shares < 0:
        raise BacktestInputError("模拟账户出现负现金或负持仓，已停止计算。")
    portfolio.cash = max(0.0, portfolio.cash)
    record.update(cash_after=portfolio.cash, shares_after=portfolio.shares)
    return record


def prepare_bars(history: pd.DataFrame, params: BacktestParams, min_observations: int | None = None) -> tuple[pd.DataFrame, list[str], float]:
    params.validate()
    if history.empty:
        raise BacktestInputError("没有历史行情，无法回测。")
    if not {"date", "open", "high", "low", "close", "volume"}.issubset(history):
        raise BacktestInputError("行情缺少日期或 OHLCV 字段。")
    data = history.copy(deep=True)
    warnings = ["归一化研究回测：首日开盘=100；资金、持仓及费用均为模拟研究单位，不代表实盘现金流。"]
    for column in ("symbol", "asset_type", "adjustment", "source", "provider"):
        if column in data and data[column].fillna("UNKNOWN").nunique() > 1:
            raise BacktestInputError(f"{column} 混用，不能进行同一证券、同一价格口径回测。")
    data["date"] = pd.to_datetime(data["date"], format="ISO8601", errors="coerce")
    if data["date"].isna().any():
        raise BacktestInputError("存在无效日期。")
    for column in ("open", "high", "low", "close", "volume"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    if not np.isfinite(data["close"]).all() or (data["close"] <= 0).any():
        raise BacktestInputError("收盘价缺失、非数值或非正，无法可靠估值；请刷新数据或调整区间。")
    if data.groupby("date")[["open", "high", "low", "close", "volume"]].nunique(dropna=False).gt(1).any().any():
        raise BacktestInputError("同一日期存在冲突的 OHLCV 记录。")
    duplicates = int(data["date"].duplicated().sum())
    if duplicates:
        warnings.append(f"去除 {duplicates} 条完全相同的重复日期记录。")
    data = data.drop_duplicates("date").sort_values("date").reset_index(drop=True)
    first_open = float(data["open"].iloc[0])
    if not np.isfinite(first_open) or first_open <= 0:
        raise BacktestInputError("区间首日开盘价无效，无法设置研究价格基准，请调整起点。")
    valid_price = np.isfinite(data[["open", "high", "low"]]).all(axis=1) & data[["open", "high", "low"]].gt(0).all(axis=1)
    valid_range = (data["high"] >= data[["open", "close", "low"]].max(axis=1)) & (data["low"] <= data[["open", "close", "high"]].min(axis=1))
    data["can_trade"] = valid_price & valid_range & np.isfinite(data["volume"]) & data["volume"].gt(0)
    count = int(data["can_trade"].sum())
    minimum = min_observations if min_observations is not None else params.long_window + 1
    if count < minimum:
        raise BacktestInputError(f"数据长度不足：至少需要 {minimum} 个可交易观测，当前仅 {count} 个。")
    if (~data["can_trade"]).any():
        warnings.append(f"{int((~data['can_trade']).sum())} 条日线不可交易：该日订单取消、信号暂停，持仓仍按有效收盘估值；均线不填充无效日。")
    data["calendar_gap"] = data["date"].diff().dt.days.fillna(0)
    from src.stock_data.calendar import session_gap
    previous_valid = []
    gap_statuses = []
    missing_dates = []
    unknown_count = 0
    for index, row in data.iterrows():
        if index == 0:
            previous_valid.append(False)
            gap_statuses.append("FIRST")
            continue
        evidence = session_gap(data.iloc[index - 1]["date"].to_pydatetime(), row["date"].to_pydatetime())
        gap_statuses.append(evidence.status)
        previous_valid.append(evidence.observation_is_next_session)
        missing_dates.extend(evidence.missing_sessions)
        unknown_count += evidence.status == "UNKNOWN_CALENDAR"
    data["previous_observation_valid"] = previous_valid
    data["session_gap_status"] = gap_statuses
    if missing_dates:
        preview = "、".join(missing_dates[:5]) + ("等" if len(missing_dates) > 5 else "")
        warnings.append(f"检测到 {len(missing_dates)} 个本地日历声明的开市日没有行情记录（{preview}）：跨缺口的历史引用、日涨跌条件和待执行订单不使用。")
    if unknown_count:
        warnings.append(f"有 {unknown_count} 处相邻记录超出本地交易日历覆盖，连续性标为未核验；不将其称为缺失交易日。")
    for column in ("open", "high", "low", "close"):
        data[f"input_{column}"] = data[column]
        data[column] = data[column] / first_open * 100
    return data, warnings, first_open


def generate_ma_signals(data: pd.DataFrame, params: BacktestParams) -> pd.DataFrame:
    """Separate, past-only signal layer; never reads next open or shifts prices back."""
    result = data.copy()
    close = result["close"].where(result["can_trade"])
    result["short_ma"] = close.rolling(params.short_window, min_periods=params.short_window).mean()
    result["long_ma"] = close.rolling(params.long_window, min_periods=params.long_window).mean()
    difference = result["short_ma"] - result["long_ma"]
    result["signal"] = np.select([(difference > 0) & (difference.shift(1) <= 0),
                                  (difference < 0) & (difference.shift(1) >= 0)], [1, -1], default=0)
    return result


def execute_target(portfolio: Portfolio, target: float, opening: float, params: BacktestParams) -> dict:
    """Grid rebalance at the next open, with cash limits and partial positions."""
    if not np.isfinite(target) or not 0 <= target <= 1 or not np.isfinite(opening) or opening <= 0:
        raise BacktestInputError("网格目标仓位或开盘价无效。")
    desired = int(np.floor((portfolio.cash + portfolio.shares * opening) * target / opening / params.lot_size)) * params.lot_size
    side = "BUY" if desired > portfolio.shares else "SELL"
    price = opening * (1 + params.slippage_rate if side == "BUY" else 1 - params.slippage_rate)
    difference = desired - portfolio.shares
    if side == "BUY":
        affordable = int(np.floor(portfolio.cash / (price * (1 + params.commission_rate)) / params.lot_size)) * params.lot_size
        quantity = min(difference, affordable)
    else:
        quantity = portfolio.shares if target == 0 else max(0, -difference)
    record = {"status":"FILLED" if quantity > 0 else "REJECTED" if side == "BUY" else "IGNORED",
              "reason":"" if quantity > 0 else "INSUFFICIENT_CASH" if side == "BUY" else "TARGET_UNCHANGED",
              "side":side, "quantity":quantity, "reference_open":opening, "execution_price":price,
              "gross_value":quantity * price, "commission":quantity * price * params.commission_rate,
              "slippage_cost":quantity * abs(price - opening)}
    if quantity:
        gross, fee = record["gross_value"], record["commission"]
        portfolio.cash += (-gross - fee) if side == "BUY" else (gross - fee)
        portfolio.shares += quantity if side == "BUY" else -quantity
    if quantity:
        portfolio.record_fill(side,quantity,record["gross_value"],record["commission"])
    if portfolio.cash < -1e-7 or portfolio.shares < 0:
        raise BacktestInputError("网格账户出现负现金或负持仓。")
    portfolio.cash = max(0, portfolio.cash)
    record.update(cash_after=portfolio.cash, shares_after=portfolio.shares)
    return record


def run_backtest(history: pd.DataFrame, params: BacktestParams | None = None, *, strategy=None) -> BacktestResult:
    params = params or BacktestParams()
    from src.backtest.strategies import StrategySpec, LABELS, minimum_bars, strategy_signals
    strategy = strategy or StrategySpec()
    try:
        strategy.validate()
    except ValueError as exc:
        raise BacktestInputError(str(exc)) from exc
    from src.backtest.strategies import rule_fields, market_operands, evaluate_at, RULE_SCHEMA_VERSION
    fields=rule_fields(strategy.rules) if strategy.kind == "custom" else set()
    unit_warnings=[]
    if fields & {"volume","volume_ma","amount"}:
        from src.stock_data.units import canonical_turnover
        history,unit_warnings=canonical_turnover(history)
        if fields & {"volume","volume_ma"} and history["volume"].isna().all():
            raise BacktestInputError("成交量单位缺失或全部数据不可用，请刷新缓存；不能计算成交量条件。")
    data, warnings, first_open = prepare_bars(history, params, minimum_bars(strategy, params))
    warnings.extend(unit_warnings)
    data = generate_ma_signals(data, params) if strategy.kind == "ma" else strategy_signals(data, strategy, params)
    if strategy.kind == "grid":
        warnings.append("日线网格仅按收盘档位在下一开盘调整目标仓位，不推断日内路径；跳空和整手/费用会产生偏离。边界使用首日开盘=100的研究价格。")
    spec = strategy
    operands=market_operands(data,spec.rules) if spec.kind=="custom" else None
    if spec.kind=="custom":
        warnings.append("持仓收益率=收盘持仓市值/剩余成本−1；成本含买入手续费与滑点，不预扣未来卖出费用。止盈止损是收盘触发阈值，下一开盘可能跳空，不保证实际收益/损失为该比例。")
    strategy, benchmark = Portfolio(params.initial_cash), Portfolio(params.initial_cash)
    orders, baseline_orders, valuations = [], [], []
    pending = None
    benchmark_attempted = False
    for index, row in data.iterrows():
        if pending is not None:
            if not row["can_trade"]:
                pending.update(status="CANCELLED", reason="UNTRADABLE_NEXT_BAR")
            elif row["session_gap_status"] == "MISSING_SESSION":
                pending.update(status="CANCELLED", reason="MISSING_TRADING_SESSION")
            elif row["session_gap_status"] != "CONTIGUOUS":
                pending.update(status="CANCELLED", reason="CALENDAR_UNVERIFIED")
            else:
                fill = (execute_target(strategy, pending["target_weight"], row["open"], params)
                        if "target_weight" in pending else execute_order(strategy, pending["side"], row["open"], params))
                pending.update(fill)
                if fill["status"] == "FILLED":
                    pending["execution_date"] = row["date"]
                    if pending["side"]=="BUY" and strategy.entry_index is None:
                        strategy.entry_index=index
            pending.update(cash_after=strategy.cash, shares_after=strategy.shares)
            pending = None
        if not benchmark_attempted and row["can_trade"]:
            fill = execute_order(benchmark, "BUY", row["open"], params)
            baseline_orders.append({"signal_date": pd.NaT, "scheduled_date": row["date"],
                                    "execution_date": row["date"] if fill["status"] == "FILLED" else pd.NaT,
                                    "side": "BUY", **fill})
            benchmark_attempted = True
        account=strategy.close_state(row["close"],index)
        rule_status=""
        if spec.kind=="custom":
            side_to_check="sell" if strategy.shares else "buy"
            answer=evaluate_at(spec.rules[side_to_check],index,operands,account) if row["can_trade"] else None
            signal=(-1 if strategy.shares else 1) if answer is True else 0
            rule_status="条件成立" if answer is True else "条件不成立" if answer is False else "条件不可判断：指标、量额或历史记录不足/当日不可交易"
            data.at[index,"signal"]=signal
            row=row.copy();row["signal"]=signal
        if row["signal"] != 0:
            side = "BUY" if row["signal"] == 1 else "SELL"
            order = {"signal_date": row["date"], "scheduled_date": data.iloc[index + 1]["date"] if index + 1 < len(data) else pd.NaT,
                     "execution_date": pd.NaT, "side": side, "status": "PENDING", "reason": "", "quantity": 0,
                     "reference_open": np.nan, "execution_price": np.nan, "gross_value": 0., "commission": 0., "slippage_cost": 0.,
                     "cash_after": strategy.cash, "shares_after": strategy.shares}
            if spec.kind == "grid":
                order["target_weight"] = float(row["target_weight"])
            if spec.kind != "grid" and ((side == "BUY" and strategy.shares) or (side == "SELL" and not strategy.shares)):
                order.update(status="IGNORED", reason="ALREADY_LONG" if strategy.shares else "ALREADY_FLAT")
            elif index + 1 == len(data):
                order.update(status="UNFILLED_END", reason="NO_NEXT_BAR")
            else:
                pending = order
            orders.append(order)
        valuations.append({"position_return": account["position_return"] if account["position_return"] is not None else np.nan,
                           "holding_days": account["holding_days"] if account["holding_days"] is not None else np.nan,
                           "strategy_cost_basis": strategy.cost_basis, "rule_status":rule_status,
                           "strategy_cash": strategy.cash, "strategy_shares": strategy.shares,
                           "strategy_equity": strategy.cash + strategy.shares * row["close"],
                           "benchmark_cash": benchmark.cash, "benchmark_shares": benchmark.shares,
                           "benchmark_equity": benchmark.cash + benchmark.shares * row["close"],
                           "pending_side": pending["side"] if pending else ""})
    equity = pd.concat([data, pd.DataFrame(valuations)], axis=1)
    order_frame = pd.DataFrame(orders, columns=ORDER_COLUMNS + (["target_weight"] if spec.kind == "grid" else []))
    baseline_frame = pd.DataFrame(baseline_orders, columns=ORDER_COLUMNS)
    trades = order_frame.loc[order_frame["status"].eq("FILLED")].copy()
    statistics = []
    for prefix, label, count in (("strategy", LABELS[spec.kind], len(trades)),
                                  ("benchmark", "买入持有", int(baseline_frame["status"].eq("FILLED").sum()))):
        metrics, curves = portfolio_statistics(equity[f"{prefix}_equity"], params.initial_cash,
                                                risk_free_rate=params.risk_free_rate, trade_count=count)
        statistics.append({"portfolio": label, **metrics})
        for column in curves:
            equity[f"{prefix}_{column}"] = curves[column]
    return BacktestResult(equity, order_frame, trades, baseline_frame, pd.DataFrame(statistics).set_index("portfolio"), params, warnings,
                          {"price_basis": "first_open_100", "input_first_open": first_open, "initial_cash": params.initial_cash,
                           "initial_shares": 0, "force_liquidation": False, "params": asdict(params),
                           "strategy": asdict(spec), "strategy_label": LABELS[spec.kind], "rule_schema_version":RULE_SCHEMA_VERSION,
                           "position_return_basis":"close_market_value / remaining_cost_including_entry_fee_slippage - 1; exit_fee excluded"})
