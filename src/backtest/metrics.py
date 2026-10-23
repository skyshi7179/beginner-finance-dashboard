"""Portfolio statistics include initial capital and first-day execution costs."""

from __future__ import annotations

import numpy as np
import pandas as pd


def portfolio_statistics(equity: pd.Series, initial_cash: float, *, risk_free_rate: float = 0.0,
                         trading_days: int = 252, trade_count: int = 0) -> tuple[dict, pd.DataFrame]:
    if equity.empty or initial_cash <= 0 or not np.isfinite(equity).all() or (equity <= 0).any():
        raise ValueError("净值统计需要有效的正资产和初始资金。")
    nav = equity.astype(float) / initial_cash
    returns = nav.pct_change(fill_method=None)
    returns.iloc[0] = nav.iloc[0] - 1  # Include first-open execution and first-close valuation.
    drawdown = nav / nav.cummax().clip(lower=1.0) - 1
    std = float(returns.std(ddof=1)) if len(returns) > 1 else None
    rf_daily = (1 + risk_free_rate) ** (1 / trading_days) - 1
    metrics = {"cumulative_return": float(nav.iloc[-1] - 1),
               "annualized_return": float(nav.iloc[-1] ** (trading_days / len(nav)) - 1),
               "annualized_volatility": std * np.sqrt(trading_days) if std is not None else None,
               "max_drawdown": float(-drawdown.min()),
               "sharpe_ratio": float((returns.mean() - rf_daily) / std * np.sqrt(trading_days)) if std is not None and std > 1e-12 else None,
               "trade_count": int(trade_count), "observation_count": len(nav),
               "risk_free_rate": risk_free_rate, "trading_days_per_year": trading_days}
    return metrics, pd.DataFrame({"nav": nav, "daily_return": returns, "drawdown": drawdown})
