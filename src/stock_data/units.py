"""Canonical units for analysis; never guess an undeclared legacy unit."""
import numpy as np
import pandas as pd


def canonical_turnover(frame):
    """Return an isolated view, retaining unknown raw fields for audit/export.

    Explicit lots are converted only for supported A-share/ETF records.
    An unavailable field never invalidates the other field or price candles.
    """
    result = frame.copy(deep=True)
    asset = result.get("asset_type", pd.Series("A股", index=result.index))
    target_volume = asset.map({"A股": "股", "ETF": "份"})
    issues = []
    for field, target in (("volume", target_volume), ("amount", pd.Series("元", index=result.index))):
        values = pd.to_numeric(result.get(field, pd.Series(np.nan, index=result.index)), errors="coerce")
        units = result.get(f"{field}_unit", pd.Series("UNKNOWN", index=result.index)).fillna("UNKNOWN")
        factor = pd.Series(np.nan, index=result.index)
        if field == "volume":
            factor.loc[units.eq(target)] = 1.
            factor.loc[units.eq("手") & target.notna()] = 100.
        else:
            factor = units.map({"元": 1., "万元": 10000., "亿元": 100000000.})
        good = factor.notna() & np.isfinite(values) & values.ge(0)
        if field == "volume":
            good &= ((values * factor) % 1).eq(0)
        result[f"input_{field}"] = result.get(f"input_{field}", values)
        result[f"input_{field}_unit"] = result.get(f"input_{field}_unit", units)
        result[field] = (values * factor).where(good)
        result[f"{field}_unit"] = target.where(good, "UNKNOWN")
        if (~good).any():
            label = "成交量" if field == "volume" else "成交额"
            issues.append(f"{int((~good).sum())} 条{label}缺失、无效或单位未确认：对应日期留空，不补零、不隐藏其他有效日期。")
    return result, issues


def turnover_gaps(frame):
    """Human-readable row/field issues, also used by chart markers."""
    dates = pd.to_datetime(frame["date"]).dt.strftime("%Y-%m-%d")
    status = frame.get("turnover_status", pd.Series("UNKNOWN", index=frame.index)).fillna("UNKNOWN")
    message = frame.get("turnover_message", pd.Series("", index=frame.index)).fillna("")
    classifications = status.map({
        "CONFLICT": "来源冲突", "PARTIAL": "字段待补充", "ERROR": "补充源请求失败",
        "STALE": "沿用旧值", "LEGACY": "旧缓存单位未确认", "COMPLETE": "单位或数值异常",
        "UNKNOWN": "状态未知",
    }).fillna(status)
    issue = status.ne("COMPLETE") | frame["volume"].isna() | frame["amount"].isna()
    return pd.DataFrame({
        "日期": dates,
        "价格日线": "存在",
        "成交量": np.where(frame["volume"].isna(), "待核验/留空", "可用"),
        "成交额": np.where(frame["amount"].isna(), "待核验/留空", "可用"),
        "问题分类": classifications,
        "核验说明": message,
    }).loc[issue].reset_index(drop=True)
