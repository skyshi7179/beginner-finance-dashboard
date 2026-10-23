"""Plain-language strategy selection and optional, explicitly confirmed AI draft."""
import hashlib
import json
from datetime import datetime
from pathlib import Path
import uuid
import streamlit as st

from src.ai_report.models import ModelCallError
from src.backtest.rule_assistant import draft_rules
from src.backtest.strategies import StrategySpec, LABELS, DESCRIPTIONS, DEFAULT_RULES, parse_rules, explain_rules, VOLUME_PROFIT_RULES
from src.backtest.strategy_store import StrategyStoreError, list_strategies, save_strategy


STRATEGY_ROOT = Path("data/user_strategies")


def _clear_editor():
    for key in list(st.session_state):
        if key.startswith("rule_edit_"):
            del st.session_state[key]


def _apply_pending_strategy():
    profile = st.session_state.pop("pending_custom_strategy_load", None)
    if not profile:
        return
    _clear_editor()
    for key in ("custom_rules_json", "custom_rule_text", "custom_strategy_name", "custom_strategy_description"):
        st.session_state.pop(key, None)
    st.session_state.rule_editor_revision = st.session_state.get("rule_editor_revision", 0) + 1
    st.session_state.custom_editor_seed = profile["rules"]
    st.session_state.custom_strategy_name = profile["name"]
    st.session_state.custom_strategy_description = profile.get("description", "")
    st.session_state.custom_strategy_active_id = profile["strategy_id"]
    st.session_state.custom_strategy_active_source = profile.get("_source", "local")
    st.session_state.custom_rule_mode = "条件编辑器"


def _new_custom_strategy():
    _clear_editor()
    for key in ("custom_rules_json", "custom_rule_text", "custom_strategy_name", "custom_strategy_description",
                "custom_strategy_active_id", "custom_strategy_active_source"):
        st.session_state.pop(key, None)
    st.session_state.rule_editor_revision = st.session_state.get("rule_editor_revision", 0) + 1
    st.session_state.custom_editor_seed = DEFAULT_RULES
    st.session_state.custom_rule_mode = "条件编辑器"
    st.session_state.custom_strategy_notice = "已新建空白策略；买入和卖出条件各从1条开始。"


def _session_profiles():
    return list(st.session_state.get("session_custom_strategies", {}).values())


def _save_session_strategy(name, description, rules, identifier=None):
    identifier = identifier or uuid.uuid4().hex
    profiles = dict(st.session_state.get("session_custom_strategies", {}))
    duplicate = next((item for key, item in profiles.items()
                      if item["name"].casefold() == name.casefold() and key != identifier), None)
    if duplicate:
        raise StrategyStoreError("本次会话中已有同名策略，请修改名称。")
    old = profiles.get(identifier, {})
    now = datetime.now().astimezone().isoformat()
    profile = {"strategy_id": identifier, "name": name, "description": description, "rules": rules,
               "created_at": old.get("created_at", now), "updated_at": now, "_source": "session"}
    profiles[identifier] = profile
    st.session_state.session_custom_strategies = profiles
    return profile


def _render_strategy_loader():
    _apply_pending_strategy()
    local = [{**item, "_source": "local"} for item in list_strategies(root=STRATEGY_ROOT)]
    session = [{**item, "_source": "session"} for item in _session_profiles()]
    profiles = {f"{item['_source']}:{item['strategy_id']}": item for item in [*session, *local]}
    with st.expander("新建或加载自定义策略", expanded=not profiles):
        st.caption("会话策略在关闭应用后消失；本地策略可以在“数据管理→自定义策略”中删除和恢复。")
        if profiles:
            selected = st.selectbox("选择已保存策略", list(profiles),
                                    format_func=lambda value: f"{'本次会话' if profiles[value]['_source']=='session' else '本地'} · {profiles[value]['name']}",
                                    key="custom_strategy_load_selection")
            a, b = st.columns(2)
            if a.button("加载所选策略", key="custom_strategy_load", use_container_width=True):
                st.session_state.pending_custom_strategy_load = profiles[selected]
                st.rerun()
            b.button("新建自定义策略", key="custom_strategy_new", on_click=_new_custom_strategy,
                     use_container_width=True)
        else:
            st.button("新建自定义策略", key="custom_strategy_new", on_click=_new_custom_strategy)
    notice = st.session_state.pop("custom_strategy_notice", None)
    if notice:
        st.success(notice)


def _operand_control(container, prefix, seed):
    from src.backtest.strategies import FIELDS
    labels={"open":"研究开盘价","high":"研究最高价","low":"研究最低价","close":"研究收盘价",
        "volume":"成交量（股/份）","amount":"成交额（元）","daily_return":"较上一交易观测涨跌幅",
        "ma":"价格均线MA","volume_ma":"成交量均线","rsi":"简单RSI","position_return":"当前持仓收益率",
        "holding_days":"持仓观测日数","value":"数值常量"}
    options=["close","open","high","low","volume","amount","daily_return","ma","volume_ma","rsi","position_return","holding_days","value"]
    default="value" if "value" in seed else seed.get("field","ma")
    field=container.selectbox("指标 / 常量",options,index=options.index(default),format_func=lambda v:labels[v],key=prefix+"_field")
    if field=="value":
        unit_options=["普通数值","百分比（%）"] if seed.get("percent_input") else ["普通数值"]
        if st.session_state.get(prefix+"_unit") not in unit_options:
            st.session_state.pop(prefix+"_unit",None)
        percent=container.selectbox("常量单位",unit_options,index=1 if seed.get("percent_input") else 0,key=prefix+"_unit")
        number=container.number_input("常量（百分比填10表示10%）",value=float(seed.get("value",0))*(100 if percent.startswith("百分") else 1),key=prefix+"_value")
        return {"value":float(number)/(100 if percent.startswith("百分") else 1)}
    result={"field":field}
    if field in {"ma","volume_ma","rsi"}:
        result["window"]=int(container.number_input("窗口（交易观测数）",1,250,int(seed.get("window",5)),key=prefix+"_window",help="例如5：取最近5个交易观测，不是5个自然日；越小通常越敏感。"))
    if field not in {"position_return","holding_days"}:
        lag=int(container.number_input("历史偏移（0今天，1上一观测）",0,250,int(seed.get("lag",0)),key=prefix+"_lag"))
        if lag:result["lag"]=lag
    scale=container.number_input("倍数",min_value=.000001,max_value=1000000.,value=float(seed.get("scale",1)),key=prefix+"_scale",help="例如右侧昨日成交量×2：偏移设1，倍数填2。")
    if scale!=1:result["scale"]=float(scale)
    return result


def _group_editor(prefix,seed,depth=1):
    logic=st.selectbox("本组组合方式",["all","any"],index=0 if seed.get("logic","all")=="all" else 1,
        format_func=lambda v:"全部满足（且）" if v=="all" else "任一满足（或）",key=prefix+"_logic")
    children=seed.get("conditions",[])
    count=int(st.number_input("本组条件 / 子组数量",1,8,min(max(len(children),1),8),key=prefix+"_count"))
    result=[]
    fallback=DEFAULT_RULES["buy"]["conditions"][0]
    for index in range(count):
        child=children[index] if index<len(children) else fallback
        node_key=f"{prefix}_{index}"
        with st.container(border=True):
            st.caption(f"第{index+1}项 · 第{depth}层")
            nested=depth<3 and st.selectbox("条目类型",["单个条件","子条件组"],index=1 if "conditions" in child else 0,key=node_key+"_type")=="子条件组"
            if nested:
                result.append(_group_editor(node_key,child if "conditions" in child else {"logic":"all","conditions":[fallback]},depth+1))
                continue
            if "conditions" in child:child=fallback
            a,b,c=st.columns([3,2,3])
            left=_operand_control(a,node_key+"_left",child["left"])
            ops=["gte","lte","gt","lt","eq","cross_above","cross_below"]
            relations={"gte":"大于或等于（达到）","lte":"小于或等于","gt":"大于","lt":"小于","eq":"等于","cross_above":"向上穿过","cross_below":"向下穿过"}
            op=b.selectbox("比较关系",ops,index=ops.index(child["op"]),format_func=lambda v:relations[v],key=node_key+"_op")
            right_seed=dict(child["right"])
            if left.get("field") in {"daily_return","position_return"} and "value" in right_seed:right_seed["percent_input"]=True
            right=_operand_control(c,node_key+"_right",right_seed)
            result.append({"left":left,"op":op,"right":right})
    return {"logic":logic,"conditions":result}


def strategy_controls():
    saved=st.session_state.get("strategy_preferences",{})
    if "strategy_kind" not in st.session_state:
        st.session_state.strategy_kind=st.session_state.get("last_strategy_kind",saved.get("kind","ma"))
    kind = st.selectbox("选择策略", list(LABELS), format_func=lambda v:LABELS[v], key="strategy_kind")
    st.session_state.last_strategy_kind=kind
    preferences=saved if saved.get("kind")==kind else {}
    if kind != "custom":
        with st.expander("这项策略在做什么？", expanded=True):
            st.write(DESCRIPTIONS[kind])
            from src.backtest.learning import GUIDES
            st.markdown(GUIDES[kind])
            st.caption("研究用途；所有条件仅在收盘确认，下一缓存日线开盘模拟执行。历史效果不证明未来收益。")
    kwargs = {"kind":kind}
    if kind in {"channel", "rsi"}:
        kwargs["period"] = int(st.number_input("历史窗口", 2, 250, preferences.get("period",20 if kind == "channel" else 14), key=f"{kind}_period", help="使用多少条交易观测计算历史门槛或RSI，具体例子见上方说明。"))
    if kind == "channel":
        kwargs["exit_period"] = int(st.number_input("退出通道窗口", 1, 250, preferences.get("exit_period",10), key="channel_exit"))
    if kind == "rsi":
        a,b = st.columns(2)
        kwargs["lower"] = a.number_input("向下穿过的进入阈值", 0., 99., float(preferences.get("lower",30)), key="rsi_lower")
        kwargs["upper"] = b.number_input("向上穿过的退出阈值", 1., 100., float(preferences.get("upper",70)), key="rsi_upper")
    if kind == "grid":
        a,b,c=st.columns(3)
        kwargs["grid_lower"]=a.number_input("研究价格下边界", 1., 10000., float(preferences.get("grid_lower",80)), key="grid_lower")
        kwargs["grid_upper"]=b.number_input("研究价格上边界", 2., 10000., float(preferences.get("grid_upper",120)), key="grid_upper")
        kwargs["grid_levels"]=int(c.number_input("网格档数", 2,30,preferences.get("grid_levels",5),key="grid_levels"))
        st.warning("边界以首日开盘=100的研究价格指定，不是当前人民币报价。每次收盘跨档只形成一笔下一开盘目标仓位委托，不按日内高低价补造多笔成交。")
    if kind == "custom":
        _render_strategy_loader()
        if "custom_rule_mode" not in st.session_state:
            st.session_state.custom_rule_mode=st.session_state.get("last_custom_rule_mode","条件编辑器")
        if st.button("载入例子：放量上涨 + 10%止盈止损 / 均线退出",key="rules_load_volume_example"):
            _clear_editor()
            st.session_state.rule_editor_revision=st.session_state.get("rule_editor_revision",0)+1
            st.session_state.custom_editor_seed=VOLUME_PROFIT_RULES
            st.session_state.custom_rule_mode="条件编辑器"
        pending=st.session_state.pop("pending_rule_draft",None)
        if pending:
            st.session_state.custom_rule_mode="JSON规则"
            st.session_state.custom_rules_json=json.dumps(pending,ensure_ascii=False,indent=2)
        mode=st.radio("规则输入方式",["条件编辑器","JSON规则"],horizontal=True,key="custom_rule_mode")
        st.session_state.last_custom_rule_mode=mode
        if mode == "JSON规则":
            if "custom_rules_json" not in st.session_state:
                st.session_state.custom_rules_json=st.session_state.get("custom_rule_text",json.dumps(st.session_state.get("custom_editor_seed",preferences.get("rules") or DEFAULT_RULES),ensure_ascii=False,indent=2))
            text=st.text_area("买入/卖出条件JSON",height=240,key="custom_rules_json")
            st.session_state.custom_rule_text=text
            try:
                rules=parse_rules(text)
            except ValueError as exc:
                st.error(str(exc)); rules=None
        else:
            st.session_state.pop("custom_rule_text",None)
            seeds=st.session_state.get("custom_editor_seed",preferences.get("rules") or DEFAULT_RULES)
            rules={}
            for side,title in (("buy","空仓时：买入条件"),("sell","持仓时：卖出条件")):
                st.markdown(f"#### {title}")
                revision=st.session_state.get("rule_editor_revision",0)
                editor_prefix=f"rule_edit_v{revision}_{side}" if revision else f"rule_edit_{side}"
                rules[side]=_group_editor(editor_prefix,seeds[side])
            st.session_state.custom_editor_seed=rules
        with st.expander("可选：AI辅助起草规则"):
            st.caption("只发送你输入的条件描述；不发送行情。不自动运行；生成后必须审阅并确认。未配置模型时仍可手动编辑条件。")
            from src.ai_report.page import load_models
            models,errors=load_models()
            for error in errors:st.warning(error)
            description=st.text_area("描述你的历史研究条件",key="rule_ai_description",max_chars=2000)
            if models:
                index=st.selectbox("已配置模型",range(len(models)),format_func=lambda i:models[i].display_name,key="rule_ai_model")
            else:
                st.info("未配置模型，AI起草不可用；可前往首页→数据管理→模型与 API配置。条件编辑器仍可正常使用。")
            draft_signature = hashlib.sha256((description + (models[index].identity if models else "none")).encode()).hexdigest()
            if st.button("生成规则草案（调用模型）",disabled=not models or not description.strip(),key="rule_ai_generate"):
                try:
                    with st.spinner("生成并校验JSON草案…"):
                        st.session_state.rule_ai_draft=draft_rules(description,models[index])
                        st.session_state.rule_ai_draft_signature=draft_signature
                except (ValueError,ModelCallError) as exc:
                    st.session_state.pop("rule_ai_draft",None)
                    st.error(f"未采纳模型输出：{exc}")
            if st.session_state.get("rule_ai_draft") and st.session_state.get("rule_ai_draft_signature")==draft_signature:
                st.json(st.session_state.rule_ai_draft)
                if st.button("将草案放入JSON编辑器",key="rule_ai_apply"):
                    st.session_state.pending_rule_draft=st.session_state.rule_ai_draft
                    st.rerun()
        if rules is None:return None
        try:
            explanation=explain_rules(rules)
        except ValueError as exc:
            st.error(str(exc)); return None
        st.session_state.custom_editor_seed=rules
        st.write("进入条件："+explanation["buy"])
        st.write("退出条件："+explanation["sell"])
        st.caption("价格与MA均是归一化研究价格；空仓只判断买入，持仓只判断卖出；收盘触发，下一开盘执行。缺失条件不补零。")
        digest=hashlib.sha256(json.dumps(rules,sort_keys=True).encode()).hexdigest()[:16]
        confirmed = st.checkbox("我已审阅并确认以上条件",key=f"rules_confirm_{st.session_state.get('rule_editor_revision',0)}_{digest}")
        with st.expander("保存这个自定义策略", expanded=False):
            name = st.text_input("策略名称", key="custom_strategy_name", max_chars=80,
                                 placeholder="例如：放量上涨后止盈止损")
            description = st.text_area("策略说明（可选）", key="custom_strategy_description",
                                       max_chars=1000, height=80)
            save_local = st.checkbox("保存到本地，关闭应用后仍可使用", key="custom_strategy_save_local")
            active_id = st.session_state.get("custom_strategy_active_id")
            active_source = st.session_state.get("custom_strategy_active_source")
            update_col, copy_col = st.columns(2)
            if update_col.button("更新已加载策略", key="custom_strategy_update",
                                 disabled=not confirmed or not name.strip() or not active_id,
                                 use_container_width=True):
                try:
                    if active_source == "local":
                        profile = save_strategy(name, description, rules, root=STRATEGY_ROOT,
                                                strategy_id=active_id, overwrite=True)
                    else:
                        profile = _save_session_strategy(name.strip(), description.strip(), rules, active_id)
                    st.session_state.custom_strategy_notice = f"已更新策略“{profile['name']}”。"
                    st.rerun()
                except (OSError, StrategyStoreError, ValueError) as exc:
                    st.error(f"策略保存失败：{exc}")
            if copy_col.button("另存为新策略", key="custom_strategy_save_copy",
                               disabled=not confirmed or not name.strip(), use_container_width=True):
                try:
                    if save_local:
                        profile = save_strategy(name, description, rules, root=STRATEGY_ROOT)
                        source = "local"
                    else:
                        profile = _save_session_strategy(name.strip(), description.strip(), rules)
                        source = "session"
                    st.session_state.custom_strategy_active_id = profile["strategy_id"]
                    st.session_state.custom_strategy_active_source = source
                    suffix = "本地" if source == "local" else "本次会话"
                    st.session_state.custom_strategy_notice = f"已保存到{suffix}：{profile['name']}。"
                    st.rerun()
                except (OSError, StrategyStoreError, ValueError) as exc:
                    st.error(f"策略保存失败：{exc}")
            st.caption("“更新”保留原保存位置；“另存”为新策略时才按上方复选框决定是否写入本地。")
        if not confirmed:
            return None
        kwargs["rules"]=rules
    try:
        spec=StrategySpec(**kwargs); spec.validate()
        from dataclasses import asdict
        st.session_state.strategy_preferences=asdict(spec)
        return spec
    except ValueError as exc:
        st.error(str(exc)); return None
