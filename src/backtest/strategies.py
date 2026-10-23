"""Whitelisted, past-only strategy rules. No eval, imports or generated code."""
from dataclasses import asdict, dataclass, field
import json
import math

import numpy as np
import pandas as pd

LABELS = {"ma": "双均线策略", "channel": "价格通道突破", "rsi": "RSI阈值策略", "grid": "日线研究网格", "custom": "自定义条件策略"}
DESCRIPTIONS = {
    "ma": "把较短一段时间的平均收盘价与较长一段时间比较。短均线从下方穿到上方时形成进入信号，反向交叉退出；不是看到短线在上方就立即进入。窗口越短越敏感，也可能在来回波动时频繁交易。",
    "channel": "把今天收盘与此前一段时间的收盘区间比较。高于此前N日最高收盘时形成进入信号，低于此前M日最低收盘时退出。区间不含今天，因此没有把当日价格提前放入历史门槛。横盘中的反复突破可能造成连续损失。",
    "rsi": "RSI把一段历史上涨与下跌的平均幅度换成0到100之间的相对强弱指标。本版使用简单滚动均值：向下穿过较低阈值时形成进入信号，向上穿过较高阈值时退出。低RSI不保证反弹，持续下跌中可能长期持有亏损。",
    "grid": "先用研究价格指定上下边界，再分成若干档。收盘价格下降跨档时增加目标仓位，上升跨档时减少目标仓位；下一缓存开盘按目标仓位模拟调仓。它不猜测日内高低价先后，也不逐档假装成交。突破下边界可接近满仓，持续下跌仍会亏损；跳空、费用和整手限制会使实际模拟仓位偏离目标。",
    "custom": "用价格、均线、RSI、量额及持仓盈亏组合条件；条件组可嵌套且/或，交叉需前后关系变化。空仓判断买入、持仓判断卖出，收盘触发后下一开盘执行；缺失指标不能补零。",
}


@dataclass(frozen=True)
class StrategySpec:
    kind: str = "ma"
    period: int = 20
    exit_period: int = 10
    lower: float = 30.0
    upper: float = 70.0
    grid_lower: float = 80.0
    grid_upper: float = 120.0
    grid_levels: int = 5
    rules: dict = field(default_factory=dict)

    def validate(self):
        if self.kind not in LABELS:
            raise ValueError("不支持的策略类型。")
        for value in (self.period, self.exit_period, self.grid_levels):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 250:
                raise ValueError("策略窗口/网格档数必须是1至250的整数。")
        for value in (self.lower, self.upper, self.grid_lower, self.grid_upper):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("阈值必须是有限数值。")
        if not 0 <= self.lower < self.upper <= 100:
            raise ValueError("RSI要求0≤低阈值<高阈值≤100。")
        if not 0 < self.grid_lower < self.grid_upper or not 2 <= self.grid_levels <= 30:
            raise ValueError("网格要求0<下边界<上边界，档数2至30。")
        if self.kind == "custom":
            validate_rules(self.rules)


RULE_SCHEMA_VERSION = 2
FIELDS = {"open", "high", "low", "close", "volume", "amount", "daily_return", "ma", "volume_ma", "rsi", "position_return", "holding_days"}
ACCOUNT_FIELDS = {"position_return", "holding_days"}
OPS = {"gt", "lt", "gte", "lte", "eq", "cross_above", "cross_below"}
DIMENSIONS = {"open":"price", "high":"price", "low":"price", "close":"price", "ma":"price",
              "volume":"quantity", "volume_ma":"quantity", "amount":"money", "daily_return":"return",
              "position_return":"return", "rsi":"oscillator", "holding_days":"days"}


def _operand(value):
    if not isinstance(value, dict):
        raise ValueError("条件两侧必须使用字段或常量对象。")
    if set(value) == {"value"}:
        number = value["value"]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or abs(number) > 1e12 or not math.isfinite(number):
            raise ValueError("常量必须是绝对值不超过1e12的有限数值。")
        return
    field = value.get("field")
    if not isinstance(field,str) or field not in FIELDS or set(value) - {"field", "window", "lag", "scale"}:
        raise ValueError("未知字段或代码；只支持表单列出的历史指标。")
    if field in {"ma", "rsi", "volume_ma"}:
        window = value.get("window")
        if isinstance(window, bool) or not isinstance(window, int) or not 1 <= window <= 250:
            raise ValueError("指标窗口必须为1至250的整数。")
    elif "window" in value:
        raise ValueError("该字段不使用窗口。")
    lag, scale = value.get("lag", 0), value.get("scale", 1.)
    if isinstance(lag, bool) or not isinstance(lag, int) or not 0 <= lag <= 250:
        raise ValueError("历史偏移必须为0至250，不能使用未来数据。")
    if isinstance(scale, bool) or not isinstance(scale, (int,float)) or not math.isfinite(scale) or not 0 < scale <= 1e6:
        raise ValueError("倍数必须是大于0且不超过1000000的有限数值。")
    if field in ACCOUNT_FIELDS and lag:
        raise ValueError("持仓条件只能使用当前持仓状态，不能引用历史持仓或未来状态。")


def iter_conditions(node):
    if "conditions" in node:
        for child in node["conditions"]:
            yield from iter_conditions(child)
    else:
        yield node


def rule_fields(rules):
    return {operand.get("field") for group in rules.values() for condition in iter_conditions(group)
            for operand in (condition["left"], condition["right"]) if "field" in operand}


def validate_rules(rules):
    if not isinstance(rules, dict) or set(rules) != {"buy", "sell"}:
        raise ValueError("规则必须且只能包含buy与sell。")
    nodes = [0]
    def visit(node, depth):
        nodes[0] += 1
        if nodes[0] > 60 or depth > 3:
            raise ValueError("规则最多60个节点、3层条件组。")
        if not isinstance(node, dict):
            raise ValueError("条件必须为对象。")
        if "conditions" in node:
            if set(node) != {"logic", "conditions"} or not isinstance(node["logic"],str) or node["logic"] not in {"all", "any"}:
                raise ValueError("规则组需要all/any逻辑与条件列表。")
            if not isinstance(node["conditions"], list) or not 1 <= len(node["conditions"]) <= 8:
                raise ValueError("每组需要1至8个条件或子组。")
            for child in node["conditions"]:
                visit(child, depth + (1 if isinstance(child,dict) and "conditions" in child else 0))
        else:
            if set(node) != {"left", "op", "right"} or not isinstance(node["op"],str) or node["op"] not in OPS:
                raise ValueError("不支持的比较关系或条件结构。")
            _operand(node["left"]); _operand(node["right"])
            left, right = node["left"].get("field"), node["right"].get("field")
            if left and right and DIMENSIONS[left] != DIMENSIONS[right]:
                raise ValueError("条件两侧单位不匹配，不能直接比较价格、数量、金额和收益率。")
            if node["op"].startswith("cross_") and ({left,right} & ACCOUNT_FIELDS):
                raise ValueError("持仓条件使用达到/低于等阈值比较，不支持交叉或历史持仓。")
    for group in rules.values():
        if not isinstance(group,dict) or "conditions" not in group:
            raise ValueError("买卖规则必须是条件组。")
        visit(group, 1)
    return rules


def parse_rules(text):
    if len(text) > 24000:
        raise ValueError("规则文本过长。")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("规则JSON有重复字段。")
            result[key] = value
        return result
    try:
        return validate_rules(json.loads(text, object_pairs_hook=unique, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("不允许NaN/Infinity。"))))
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError(f"规则无效：{exc}") from exc


def simple_rsi(close, window):
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(window, min_periods=window).mean()
    loss = (-delta.clip(upper=0)).rolling(window, min_periods=window).mean()
    ratio = gain / loss.replace(0, np.nan)
    result = 100 - 100 / (1 + ratio)
    result = result.mask(loss.eq(0) & gain.gt(0), 100).mask(loss.eq(0) & gain.eq(0), 50)
    return result


def minimum_bars(spec, params):
    if spec.kind == "ma":
        return params.long_window + 1
    if spec.kind == "channel":
        return max(spec.period, spec.exit_period) + 1
    if spec.kind == "rsi":
        return spec.period + 2
    if spec.kind == "custom":
        windows = [o.get("window", 1) + o.get("lag",0) + (1 if o.get("field") in {"rsi","daily_return"} else 0)
                   for g in spec.rules.values() for c in iter_conditions(g) for o in (c["left"], c["right"])]
        return max(windows) + 1
    return 2


def strategy_signals(data, spec, params):
    spec.validate()
    result = data.copy()
    close = data["close"].where(data["can_trade"])
    # Keep reference MAs for chart compatibility; only the selected rules drive signals.
    result["short_ma"] = close.rolling(params.short_window).mean()
    result["long_ma"] = close.rolling(params.long_window).mean()
    result["target_weight"] = np.nan
    if spec.kind == "ma":
        diff = result["short_ma"] - result["long_ma"]
        buy, sell = (diff > 0) & (diff.shift(1) <= 0), (diff < 0) & (diff.shift(1) >= 0)
    elif spec.kind == "channel":
        result["channel_high"] = close.rolling(spec.period).max().shift(1)
        result["channel_low"] = close.rolling(spec.exit_period).min().shift(1)
        buy, sell = close.gt(result["channel_high"]), close.lt(result["channel_low"])
    elif spec.kind == "rsi":
        rsi = simple_rsi(close, spec.period)
        result["rsi"] = rsi
        buy = rsi.lt(spec.lower) & rsi.shift(1).ge(spec.lower)
        sell = rsi.gt(spec.upper) & rsi.shift(1).le(spec.upper)
    elif spec.kind == "grid":
        target = ((spec.grid_upper - close) / (spec.grid_upper - spec.grid_lower)).clip(0, 1)
        target = np.floor(target * spec.grid_levels) / spec.grid_levels
        result["target_weight"] = target
        previous = target.shift(1).fillna(0)
        buy, sell = target.gt(previous), target.lt(previous)
    else:
        # Account-aware conditions are evaluated later inside the chronological
        # account loop, after today's open fill. No future account state exists.
        buy = evaluate_rule(data, spec.rules["buy"]).fillna(False).astype(bool)
        sell = evaluate_rule(data, spec.rules["sell"]).fillna(False).astype(bool)
    result["signal"] = np.select([sell & data["can_trade"], buy & data["can_trade"]], [-1, 1], default=0)
    return result


def market_operands(data, rules):
    """Compute each historical operand once; missing remains unknown, not zero."""
    cache = {}
    for group in rules.values():
        for condition in iter_conditions(group):
            for operand in (condition["left"], condition["right"]):
                if operand.get("field") not in ACCOUNT_FIELDS:
                    key = json.dumps(operand,sort_keys=True)
                    if key not in cache:
                        cache[key] = operand_series(data,operand)
    return cache


def operand_series(data, value):
    if "value" in value:
        return pd.Series(float(value["value"]), index=data.index)
    field = value["field"]
    close = data["close"].where(data["can_trade"])
    if field in ACCOUNT_FIELDS:
        series = pd.Series(np.nan,index=data.index)
    elif field in {"ma", "rsi"}:
        series = close.rolling(value["window"]).mean() if field == "ma" else simple_rsi(close,value["window"])
    elif field == "daily_return":
        series = close.pct_change(fill_method=None)
        # Long gaps are not described as yesterday. Exchange-calendar gaps
        # are marked by prepare_bars when the local calendar can verify them.
        if "previous_observation_valid" in data:
            series = series.where(data["previous_observation_valid"])
    elif field == "volume_ma":
        series = data["volume"].where(data["can_trade"]).rolling(value["window"]).mean()
    else:
        series = pd.to_numeric(data.get(field,pd.Series(np.nan,index=data.index)),errors="coerce").where(data["can_trade"])
    lag=value.get("lag",0)
    series = series.shift(lag)
    if lag and field in {"volume","volume_ma"}:
        series=series.where(series.gt(0))
    if lag and "previous_observation_valid" in data:
        valid = data["previous_observation_valid"].rolling(lag,min_periods=lag).min().eq(1)
        series=series.where(valid)
    return series * value.get("scale",1.)


def _compare(left, right, op):
    if op in {"gt","cross_above"}:return left > right
    if op in {"lt","cross_below"}:return left < right
    if op == "gte":return (left >= right) | np.isclose(left,right,rtol=1e-12,atol=1e-12)
    if op == "lte":return (left <= right) | np.isclose(left,right,rtol=1e-12,atol=1e-12)
    equal=np.isclose(left,right,rtol=1e-12,atol=1e-12)
    return pd.Series(equal,index=left.index) if isinstance(left,pd.Series) else equal


def evaluate_rule(data, node):
    """Vectorized, three-valued logic for market-only rules and teaching tests."""
    if "conditions" in node:
        values=[evaluate_rule(data,child) for child in node["conditions"]]
        result=values[0]
        for value in values[1:]:
            result = result & value if node["logic"] == "all" else result | value
        return result
    left,right=operand_series(data,node["left"]),operand_series(data,node["right"])
    valid=left.notna() & right.notna()
    result=_compare(left,right,node["op"])
    if node["op"].startswith("cross_"):
        valid &= left.shift(1).notna() & right.shift(1).notna()
        result &= left.shift(1).le(right.shift(1)) if node["op"]=="cross_above" else left.shift(1).ge(right.shift(1))
    return result.astype("boolean").where(valid,pd.NA)


def evaluate_at(node, index, operands, account):
    """Evaluate a close rule against only the account state known at this bar."""
    if "conditions" in node:
        values=[evaluate_at(child,index,operands,account) for child in node["conditions"]]
        if node["logic"]=="all":
            return False if any(v is False for v in values) else None if any(v is None for v in values) else True
        return True if any(v is True for v in values) else None if any(v is None for v in values) else False
    def get(value,at):
        field=value.get("field")
        if field in ACCOUNT_FIELDS:
            number=account.get(field)
            return None if number is None else number * value.get("scale",1.)
        if at < 0:return None
        number=operands[json.dumps(value,sort_keys=True)].iloc[at]
        return None if pd.isna(number) or not np.isfinite(number) else float(number)
    left,right=get(node["left"],index),get(node["right"],index)
    if left is None or right is None:return None
    answer=bool(_compare(left,right,node["op"]))
    if node["op"].startswith("cross_"):
        previous_left,previous_right=get(node["left"],index-1),get(node["right"],index-1)
        if previous_left is None or previous_right is None:return None
        answer &= previous_left <= previous_right if node["op"]=="cross_above" else previous_left >= previous_right
    return bool(answer)


def explain_rules(rules):
    validate_rules(rules)
    names={"open":"研究开盘价","high":"研究最高价","low":"研究最低价","close":"研究收盘价",
           "volume":"成交量（股/份）","amount":"成交额（元）","daily_return":"较上次收盘涨跌幅",
           "ma":"MA","volume_ma":"成交量均线","rsi":"简单RSI","position_return":"当前持仓收益率","holding_days":"持仓观测日数"}
    def operand(o):
        if "value" in o:return str(o["value"])
        label=names[o["field"]]+(str(o["window"]) if "window" in o else "")
        if o.get("lag"):label=f"前{o['lag']}个交易观测的"+label
        if o.get("scale",1)!=1:label+=f" × {o['scale']:g}"
        return label
    relations={"gt":" > ","lt":" < ","gte":" ≥ ","lte":" ≤ ","eq":" = ","cross_above":" 向上穿过 ","cross_below":" 向下穿过 "}
    def explain(node):
        if "conditions" in node:
            return "（" + (" 且 " if node["logic"]=="all" else " 或 ").join(explain(c) for c in node["conditions"]) + "）"
        left,right=node["left"],node["right"]
        def label(value,other):
            if "value" in value and other.get("field") in {"position_return","daily_return"}:
                return f"{value['value']:.2%}"
            return operand(value)
        return label(left,right)+relations[node["op"]]+label(right,left)
    return {side:explain(group) for side,group in rules.items()}


DEFAULT_RULES = {side: {"logic": "all", "conditions": [{"left": {"field": "ma", "window": 5}, "op": op,
                                                          "right": {"field": "ma", "window": 20}}]} for side, op in (("buy", "cross_above"), ("sell", "cross_below"))}


VOLUME_PROFIT_RULES = {
    "buy": {"logic":"all", "conditions":[
        {"left":{"field":"volume"}, "op":"gte", "right":{"field":"volume","lag":1,"scale":2}},
        {"left":{"field":"daily_return"}, "op":"gt", "right":{"value":0}}]},
    "sell": {"logic":"any", "conditions":[
        {"left":{"field":"position_return"}, "op":"gte", "right":{"value":.10}},
        {"left":{"field":"position_return"}, "op":"lte", "right":{"value":-.10}},
        {"left":{"field":"ma","window":5}, "op":"cross_below", "right":{"field":"ma","window":20}}]}}
