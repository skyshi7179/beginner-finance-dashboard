"""Pure Pandas indicators, calculated only within the selected sample."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


class AnalysisInputError(ValueError):
    """Cached observations cannot support a consistent price analysis."""


@dataclass
class IndicatorResult:
    data: pd.DataFrame
    metrics: dict
    warnings: list[str]


INDICATOR_COLUMNS = ["ma5", "ma20", "daily_return", "net_value", "cumulative_return", "drawdown"]


def calculate_indicators(history: pd.DataFrame, trading_days: int = 252) -> IndicatorResult:
    """Return simple returns, sample volatility and a positive max drawdown.

    First return is NaN; NAV begins at 1 at the first selected close. Missing
    prices are rejected rather than filled or silently bridged. Known zero-
    volume records are excluded as non-trading observations. No trading
    calendar is inferred, and neither holidays nor suspensions are backfilled.
    """
    if not isinstance(trading_days, int) or isinstance(trading_days, bool) or trading_days <= 0:
        raise AnalysisInputError("交易日年化参数必须是正整数。")
    frame = history.copy(deep=True)
    warnings: list[str] = []
    metrics = {"cumulative_return": None, "annualized_volatility": None, "max_drawdown": None,
               "mean_daily_return": None, "valid_trading_days": 0, "return_observations": 0,
               "trading_days_per_year": trading_days}
    if frame.empty:
        for column in ["date", "open", "high", "low", "close", "volume", *INDICATOR_COLUMNS]:
            if column not in frame:
                frame[column] = pd.Series(dtype="float64" if column != "date" else "datetime64[ns]")
        return IndicatorResult(frame, metrics, ["所选区间没有可分析的行情记录。"])
    required = {"date", "open", "high", "low", "close", "volume"}
    if not required.issubset(frame):
        raise AnalysisInputError("行情缺少日期、OHLC 或成交量字段，请前往数据获取检查缓存。")
    frame["date"] = pd.to_datetime(frame["date"], format="ISO8601", errors="coerce")
    if frame["date"].isna().any():
        raise AnalysisInputError("缓存包含无效日期，请重新获取该区间行情。")
    for column in ("symbol", "market", "asset_type", "adjustment", "source", "provider"):
        if column in frame and frame[column].fillna("UNKNOWN").nunique() > 1:
            raise AnalysisInputError(f"所选区间的 {column} 不一致，不能混合计算；请缩短区间或重新获取统一口径的数据。")
    for column in ("open", "high", "low", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
        if not np.isfinite(frame[column]).all() or (frame[column] <= 0).any():
            raise AnalysisInputError(f"{column} 存在缺失、非数值或非正价格；请缩短区间、更换复权方式或重新获取数据。")
    if ((frame["high"] < frame[["open", "close", "low"]].max(axis=1))
            | (frame["low"] > frame[["open", "close", "high"]].min(axis=1))).any():
        raise AnalysisInputError("OHLC 高低价格关系异常，请检查行情来源。")
    frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce")
    invalid_volume = ~np.isfinite(frame["volume"]) | frame["volume"].lt(0)
    if invalid_volume.any():
        frame.loc[invalid_volume, "volume"] = np.nan
        warnings.append(f"{int(invalid_volume.sum())} 条成交量无效：保留有效价格，但不推断其是否停牌，成交量图显示缺口。")
    duplicate_fields = ["open", "high", "low", "close", "volume"]
    if frame.groupby("date")[duplicate_fields].nunique(dropna=False).gt(1).any().any():
        raise AnalysisInputError("同一交易日期存在相互冲突的 OHLCV 记录。")
    duplicates = frame["date"].duplicated().sum()
    if duplicates:
        warnings.append(f"已去除 {int(duplicates)} 条完全一致的重复日期记录。")
    frame = frame.drop_duplicates("date").sort_values("date").reset_index(drop=True)
    no_trade = frame["volume"].eq(0)
    if no_trade.any():
        warnings.append(f"已排除 {int(no_trade.sum())} 条零成交记录（可能停牌或无成交），不填充价格。")
        frame = frame.loc[~no_trade].reset_index(drop=True)
    if frame.empty:
        empty = calculate_indicators(frame, trading_days)
        return IndicatorResult(empty.data, empty.metrics, warnings + empty.warnings)
    if "adjustment" in frame and frame["adjustment"].eq("none").all():
        warnings.append("实际输入为不复权价格；权益事件可能影响价格连续性和收益解释。")
    for window in (5, 20):
        frame[f"ma{window}"] = frame["close"].rolling(window, min_periods=window).mean()
    frame["observation_return"] = frame["close"].pct_change(fill_method=None)
    from src.stock_data.calendar import is_declared_trade_day
    gap = pd.Series(False,index=frame.index)
    for index in range(1,len(frame)):
        previous, current=frame["date"].iloc[index-1],frame["date"].iloc[index]
        gap.iloc[index]=any(is_declared_trade_day(d.date().isoformat())
            for d in pd.date_range(previous+pd.Timedelta(days=1), current-pd.Timedelta(days=1)))
    frame["daily_return"] = frame["observation_return"].where(~gap)
    frame["return_interval_days"] = frame["date"].diff().dt.days
    frame["return_has_known_gap"] = gap
    if gap.any():
        warnings.append(f"{int(gap.sum())} 个收益区间跨过本地日历声明的缺失交易日：保留区间价格表现，但不计入日涨跌幅平均值和年化波动率。日历未覆盖时不能保证已识别全部缺口。")
    frame["net_value"] = frame["close"] / frame["close"].iloc[0]
    frame["cumulative_return"] = frame["net_value"] - 1
    frame["drawdown"] = frame["net_value"] / frame["net_value"].cummax() - 1
    returns = frame["daily_return"].dropna()
    metrics.update({"cumulative_return": float(frame["cumulative_return"].iloc[-1]),
                    "annualized_volatility": float(returns.std(ddof=1) * np.sqrt(trading_days)) if len(returns) >= 2 else None,
                    "max_drawdown": float(-frame["drawdown"].min()),
                    "mean_daily_return": float(returns.mean()) if len(returns) else None,
                    "valid_trading_days": len(frame), "return_observations": len(returns)})
    if len(frame) < 20:
        warnings.append(f"仅 {len(frame)} 个有效交易日：均线不足窗口处保留空值；年化波动率至少需要 2 个收益观测。")
    return IndicatorResult(frame, metrics, warnings)
