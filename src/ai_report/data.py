"""Build JSON facts by reusing the existing indicators and backtest engine."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import re

import pandas as pd

from src.backtest.engine import BacktestInputError, BacktestParams, run_backtest
from src.stock_analysis.indicators import AnalysisInputError, calculate_indicators
from src.stock_data.service import StockHistoryResult, validate_request
from src.time_utils import get_china_now


REPORT_TYPES = {"history": "股票历史表现分析", "technical": "技术指标分析",
                "backtest": "双均线策略回测分析", "comprehensive": "综合分析报告"}
SECTION_TITLES = {"A": "股票与数据概况", "B": "历史收益与风险分析", "C": "技术指标观察",
                  "D": "策略回测分析", "E": "分析限制与风险说明"}
DATA_VERSION = "research_facts_v1"


class ReportInputError(ValueError):
    pass


@dataclass
class AnalysisPacket:
    payload: dict
    metrics_csv: bytes


def input_signature(history: StockHistoryResult, selected: dict, start: str, end: str,
                    adjustment: str, report_type: str, params: BacktestParams,
                    model_identity: str) -> str:
    digest = hashlib.sha256(pd.util.hash_pandas_object(history.data, index=False).values.tobytes()).hexdigest()
    material = {"data_version": DATA_VERSION, "data_hash": digest, "cache_metadata": history.metadata,
                "cache_status": history.status, "selected": selected, "start": str(start), "end": str(end),
                "adjustment": adjustment, "report_type": report_type, "model": model_identity,
                "backtest": asdict(params) if report_type in {"backtest", "comprehensive"} else None}
    return hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def build_analysis(history: StockHistoryResult, selected: dict, *, start: str, end: str,
                   adjustment: str, report_type: str, params: BacktestParams | None = None) -> AnalysisPacket:
    """Called only on generation; no network, file writes or hidden computation by LLM."""
    params = params or BacktestParams()
    if report_type not in REPORT_TYPES:
        raise ReportInputError("未知报告类型。")
    code, start, end = validate_request(selected.get("symbol", ""), start, end, adjustment)
    if selected.get("asset_type", "A股") not in {"A股", "ETF"}:
        raise ReportInputError("证券类别仅支持A股和ETF。")
    if report_type in {"backtest", "comprehensive"}:
        try:
            params.validate()
        except BacktestInputError as exc:
            raise ReportInputError(str(exc)) from exc
    if history.status not in {"CACHE", "STALE_CACHE"} or history.data.empty:
        raise ReportInputError("没有可分析的本地缓存，请先前往股票数据获取。")
    frame = history.data
    if not {"date", "open", "high", "low", "close", "volume"}.issubset(frame.columns):
        raise ReportInputError("缓存缺少日期或OHLCV字段。")
    for field in ("symbol", "name", "asset_type", "source", "provider", "adjustment"):
        if field not in frame or frame[field].isna().any() or frame[field].astype(str).str.strip().eq("").any():
            raise ReportInputError(f"缓存缺少可核验的{field}，请检查或刷新缓存。")
        if frame[field].astype(str).nunique() != 1:
            raise ReportInputError(f"缓存{field}混用，不能生成同一证券报告。")
        if not frame[field].map(lambda value: isinstance(value, str)).all():
            raise ReportInputError(f"缓存{field}类型无效，身份与来源必须为文本。")
    if not frame["symbol"].astype(str).eq(code).all() or not frame["asset_type"].eq(selected.get("asset_type", "A股")).all():
        raise ReportInputError("证券身份与所选缓存不匹配。")
    name = str(frame["name"].iloc[0])
    if selected.get("name") and selected["name"] != name:
        raise ReportInputError("证券名称与缓存不匹配。")
    if len(name) > 100 or any(ord(character) < 32 for character in name):
        raise ReportInputError("证券名称包含异常长度或控制字符。")
    for field in ("source", "provider", "data_mode", "mode"):
        if field in frame and frame[field].astype(str).str.upper().str.contains("SAMPLE|DEMO", regex=True).any():
            raise ReportInputError("股票研究报告不将SAMPLE/DEMO合成样例当作真实股票行情。")
    dates = pd.to_datetime(frame["date"], errors="coerce")
    if dates.isna().any() or not dates.between(pd.Timestamp(start), pd.Timestamp(end)).all():
        raise ReportInputError("缓存日期无效或超出请求区间。")
    if (dates.dt.date > get_china_now().date()).any():
        raise ReportInputError("缓存含未来日期，不能作为已发生历史生成报告。")
    if str(frame["adjustment"].iloc[0]) not in {"qfq", "hfq", "none"}:
        raise ReportInputError("缓存实际复权口径不明，不能生成可核验报告。")
    try:
        analysis = calculate_indicators(frame)
    except AnalysisInputError as exc:
        raise ReportInputError(str(exc)) from exc
    if analysis.data.empty:
        raise ReportInputError("区间内没有有效交易观测。")
    warnings = list(analysis.warnings)
    if history.status != "CACHE":
        warnings.append("请求区间未完全覆盖；仅分析现有缓存的实际范围，不推断缺失交易日。")
    if history.metadata.get("turnover_missing_rows", 0):
        warnings.append("部分成交数据未完整核验；报告不据此推断流动性，也不分析成交额趋势。")
    facts: dict = {}
    candidates: dict = {}
    required: dict = {section: [] for section in SECTION_TITLES}

    def fact(key, value, label, unit="", origin="缓存元数据", kind="text"):
        if value is not None and kind in {"number", "percent", "points", "count"}:
            value = int(value) if kind == "count" else float(value)
            if not math.isfinite(value):
                value = None
        if value is None:
            display = "未声明" if kind == "text" else "数据不足（未计算）"
        elif kind == "percent":
            display = f"{value:.2%}"
        elif kind == "points":
            display = f"{value:.2f}个百分点"
        elif kind == "number":
            display = f"{value:,.4f}".rstrip("0").rstrip(".")
        else:
            value = str(value) if kind == "text" else value
            display = str(value)
        facts[key] = {"value": value, "display": display, "label": label, "unit": unit, "origin": origin}

    def statement(key, section, template, mandatory=False):
        refs = re.findall(r"\{\{([\w.]+)\}\}", template)
        if not all(ref in facts for ref in refs):
            raise ReportInputError("报告模板引用了不存在的统计字段。")
        candidates[key] = {"section": section, "template": template, "refs": sorted(set(refs))}
        if mandatory:
            required[section].append(key)

    identity = {"symbol": code, "name": name, "asset_type": str(frame["asset_type"].iloc[0])}
    actual_period = {"start": dates.min().date().isoformat(), "end": dates.max().date().isoformat()}
    for field, value in identity.items():
        fact(f"instrument.{field}", value, field)
    for field, value in actual_period.items():
        fact(f"data.{field}", value, f"缓存实际{field}")
    fact("request.start", start, "请求开始日期")
    fact("request.end", end, "请求结束日期")
    fact("request.adjustment", adjustment or "none", "请求复权")
    fact("data.adjustment", frame["adjustment"].iloc[0], "实际复权")
    fact("data.source", frame["source"].iloc[0], "价格来源")
    fact("data.status", history.status, "缓存状态")
    fact("data.updated", history.metadata.get("cache_updated_at") or history.metadata.get("updated_at"), "缓存更新时刻")
    fact("data.version", " / ".join(sorted(frame.get("provider_version", pd.Series(dtype=str)).dropna().astype(str).unique())) or "未声明", "接口版本")
    fact("data.rows", len(frame), "缓存日线数", "条", kind="count")
    statement("overview", "A", "证券：{{instrument.name}}（{{instrument.symbol}}，{{instrument.asset_type}}）。实际缓存范围{{data.start}}至{{data.end}}，共{{data.rows}}条。来源{{data.source}}，状态{{data.status}}，更新时刻{{data.updated}}，接口版本{{data.version}}。请求区间{{request.start}}至{{request.end}}；请求复权{{request.adjustment}}，实际复权{{data.adjustment}}。", True)
    for key, value in analysis.metrics.items():
        kind = "count" if key in {"valid_trading_days", "return_observations", "trading_days_per_year"} else "percent"
        fact(f"history.{key}", value, key, "个" if kind == "count" else "比例（小数）", "calculate_indicators", kind)
    values = analysis.data
    fact("history.start", values["date"].iloc[0].date().isoformat(), "有效指标开始日期")
    fact("history.end", values["date"].iloc[-1].date().isoformat(), "有效指标结束日期")
    if report_type in {"history", "comprehensive"}:
        statement("performance", "B", "有效指标范围{{history.start}}至{{history.end}}，累计收益率{{history.cumulative_return}}，年化波动率{{history.annualized_volatility}}，最大回撤{{history.max_drawdown}}，平均简单日收益率{{history.mean_daily_return}}。有效观测{{history.valid_trading_days}}个，年化参数{{history.trading_days_per_year}}个交易日；收益以区间首个有效收盘为基准。", True)
        direction = "高于" if analysis.metrics["cumulative_return"] > 0 else "低于" if analysis.metrics["cumulative_return"] < 0 else "等于"
        statement("historical_direction", "B", f"期末有效收盘{direction}所选样本起点；这是对过去价格的描述，不能据此推断后续方向。")
    else:
        statement("history_not_requested", "B", "当前报告类型未包含历史收益与风险解读。", True)
    for field in ("close", "ma5", "ma20"):
        value = values[field].iloc[-1]
        fact(f"technical.{field}", None if pd.isna(value) else value, field, "输入行情价格单位", "calculate_indicators", "number")
    if report_type in {"technical", "comprehensive"}:
        statement("technical_values", "C", "末个有效观测{{history.end}}的收盘价为{{technical.close}}，MA5为{{technical.ma5}}，MA20为{{technical.ma20}}。均线仅使用当前及历史有效收盘，不足窗口保留缺失值。", True)
        short, long = values["ma5"].iloc[-1], values["ma20"].iloc[-1]
        if pd.notna(short) and pd.notna(long):
            relation = "高于" if short > long else "低于" if short < long else "等于"
            statement("ma_position", "C", f"末个有效观测的短均线{relation}长均线。相对位置不等于当日发生交叉，也不代表未来价格信号。")
    else:
        statement("technical_not_requested", "C", "当前报告类型未包含技术指标解读。", True)
    backtest_state = "not_requested"
    if report_type in {"backtest", "comprehensive"}:
        try:
            result = run_backtest(frame, params)
            backtest_state = "available"
            warnings.extend(result.warnings)
            for portfolio, prefix in (("双均线策略", "strategy"), ("买入持有", "benchmark")):
                for field, value in result.metrics.loc[portfolio].items():
                    kind = "count" if field in {"trade_count", "observation_count", "trading_days_per_year"} else "number" if field == "sharpe_ratio" else "percent"
                    fact(f"{prefix}.{field}", None if pd.isna(value) else value, f"{portfolio} {field}", "次数" if field == "trade_count" else "比例（小数）" if kind == "percent" else "", "run_backtest / portfolio_statistics", kind)
            for field, value in asdict(params).items():
                fact(f"backtest.{field}", value, f"回测参数 {field}", "研究单位" if field == "initial_cash" else "", "BacktestParams", "count" if isinstance(value, int) else "percent" if field.endswith("rate") else "number")
            fact("backtest.start", result.equity["date"].iloc[0].date().isoformat(), "回测开始日期")
            fact("backtest.end", result.equity["date"].iloc[-1].date().isoformat(), "回测结束日期")
            difference = float(result.metrics.loc["双均线策略", "cumulative_return"] - result.metrics.loc["买入持有", "cumulative_return"]) * 100
            fact("backtest.return_difference", difference, "策略与基准累计收益差", "百分点", "Python: (strategy_return-benchmark_return)*100", "points")
            statement("strategy_results", "D", "回测区间{{backtest.start}}至{{backtest.end}}。双均线策略累计收益率{{strategy.cumulative_return}}，买入持有基准{{benchmark.cumulative_return}}；策略最大回撤{{strategy.max_drawdown}}，夏普比率{{strategy.sharpe_ratio}}，成交委托数{{strategy.trade_count}}。零波动时夏普不计算，成交次数不是完整往返次数。", True)
            statement("strategy_assumptions", "D", "短/长均线窗口{{backtest.short_window}}/{{backtest.long_window}}，初始研究资金{{backtest.initial_cash}}，双向手续费{{backtest.commission_rate}}，单边滑点{{backtest.slippage_rate}}，无风险年利率{{backtest.risk_free_rate}}。收盘交叉在下一缓存日线开盘模拟执行，首日空仓、只做多、末日不强制平仓。", True)
            statement("strategy_comparison", "D", "策略与基准期末累计收益之差为{{backtest.return_difference}}；该差异只属于本次样本、参数及成本假设，不能证明样本外有效。")
        except BacktestInputError as exc:
            backtest_state = "insufficient"
            warnings.append(f"回测未生成：{exc}")
            statement("backtest_insufficient", "D", "数据不足或条件未满足，未生成有效回测结果；不能编造策略收益或与基准作数值比较。", True)
    else:
        statement("backtest_not_requested", "D", "当前报告类型未包含策略回测，未运行回测引擎。", True)
    statement("limitations", "E", "报告只解释已缓存历史，不预测未来走势或提供交易指令。来源字段与本地契约检查不等于独立认证上游数据真实性；日期空档不补齐，缺失区间不外推，复权数据可能被事后修订。", True)
    if report_type in {"backtest", "comprehensive"}:
        statement("research_units", "E", "回测为首日开盘设为100的归一化研究模型，现金与数量是研究单位，不能解释为人民币或真实股数；未模拟完整公司行为、涨跌停、税费和流动性。历史收益与回测基准的起点及成本口径不同。", True)
    statements = [item for item in candidates.values()]
    assert all(set(item["refs"]).issubset(facts) for item in statements)
    payload = {"schema_version": "1.0", "data_version": DATA_VERSION, "instrument": identity,
               "period": actual_period, "report_type": report_type, "facts": facts,
               "statements": candidates, "required": required, "warnings": list(dict.fromkeys(warnings)),
               "backtest_state": backtest_state,
               "data_digest": hashlib.sha256(pd.util.hash_pandas_object(frame, index=False).values.tobytes()).hexdigest()}
    payload["analysis_id"] = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()
    rows = [{"fact_id": key, **value, "symbol": code, "name": name,
             "data_start": actual_period["start"], "data_end": actual_period["end"],
             "requested_start": start, "requested_end": end, "source": facts["data.source"]["value"],
             "actual_adjustment": facts["data.adjustment"]["value"], "analysis_id": payload["analysis_id"]}
            for key, value in facts.items()]
    export = pd.DataFrame(rows).rename(columns={"fact_id": "fact_id（指标标识）", "value": "value（原始值）",
                                              "display": "display（显示值）", "unit": "unit（单位）", "origin": "origin（计算来源）"})
    return AnalysisPacket(payload, export.to_csv(index=False).encode("utf-8-sig"))
