"""Explicit cache cleanup and promotion; permanent deletes go to recovery."""
from pathlib import Path
from datetime import datetime
import json
import hashlib
import shutil
import uuid

import pandas as pd
import streamlit as st

from src.navigation import PAGE_HOME
from src.stock_data import service
from src.stock_data.errors import StockDataError
from src.stock_data.cache import cache_paths, load_history_cache, save_history_cache
from src.stock_data.session_cache import clear_session, get_session_id, protect
from src.stock_data.workspace import temporary_directory, temporary_root
from src.backtest.strategy_store import (
    StrategyStoreError,
    archive_strategy,
    list_strategies,
    permanently_delete_strategy,
    restore_strategy,
)

DATA_DIR = Path("data/stocks")


def archive_entry(directory: Path, item: dict, recovery: Path) -> Path:
    """Move only a validated pair into a recoverable application trash entry."""
    code = str(item["symbol"])
    service.validate_request(code, "2000-01-01", "2000-01-01", item["adjustment"])
    if item["asset_type"] not in {"A股", "ETF"}:
        raise ValueError("未知证券类别。")
    paths = cache_paths(code, "daily", item["adjustment"], directory, item["asset_type"])
    if any(p.is_symlink() for p in paths) or any(Path(directory).resolve() not in p.resolve().parents for p in paths):
        raise ValueError("拒绝操作缓存目录以外的文件。")
    if not all(p.exists() for p in paths):
        raise ValueError("缓存CSV/JSON不完整，请先检查文件。")
    target = recovery / uuid.uuid4().hex
    target.mkdir(parents=True)
    moved = []
    try:
        manifest = {
            "item": {key: value for key, value in item.items() if key != "directory"},
            "relative": [str(p.relative_to(directory)) for p in paths],
            "archived_at": datetime.now().astimezone().isoformat(),
            "source_scope": item.get("保存方式", "未记录"),
        }
        (target / "entry.json").write_text(json.dumps(manifest, ensure_ascii=False))
        for p in paths:
            shutil.move(str(p), str(target / p.name))
            moved.append(p)
    except OSError:
        for p in moved:
            shutil.move(str(target / p.name), str(p))
        raise
    return target


def invalidate_results(state):
    for key in ("stock_history_result", "stock_history_signature", "backtest_run_result", "backtest_run_signature", "ai_report_result", "ai_report_signature"):
        state.pop(key, None)


def _identity(item: dict) -> str:
    return hashlib.sha256(json.dumps(item, sort_keys=True, default=str).encode()).hexdigest()[:20]


def _item_label(item: dict) -> str:
    adjustment = item.get("adjustment") or "不复权"
    period = f"{item.get('start') or '无日期'}～{item.get('end') or '无日期'}"
    return (f"{item.get('保存方式') or item.get('source_scope') or '回收区'} · "
            f"{item.get('asset_type', '未知')} {item.get('symbol', '------')} "
            f"{item.get('name') or '名称未记录'} · {adjustment} · {period} · "
            f"{int(item.get('record_count') or 0)}条")


def list_recovery_entries(recovery: Path) -> tuple[list[dict], list[str]]:
    """Read safe, human-facing recovery metadata without trusting UUID labels."""
    entries, errors = [], []
    for manifest in sorted(recovery.glob("*/entry.json")):
        if manifest.is_symlink() or manifest.parent.is_symlink() or recovery.resolve() not in manifest.resolve().parents:
            continue
        try:
            payload = json.loads(manifest.read_text())
            item = dict(payload["item"])
            service.validate_request(item["symbol"], "2000-01-01", "2000-01-01", item["adjustment"])
            if item.get("asset_type") not in {"A股", "ETF"}:
                raise ValueError("证券类别无效")
            csv_file = manifest.parent / f"{item['symbol']}.csv"
            if not csv_file.is_file() or csv_file.is_symlink():
                raise ValueError("行情CSV不存在")
            frame = pd.read_csv(csv_file, dtype={"symbol": str})
            dates = pd.to_datetime(frame.get("date"), errors="coerce").dropna()
            if not dates.empty:
                item["start"], item["end"] = dates.min().date().isoformat(), dates.max().date().isoformat()
            item["record_count"] = int(len(frame))
            if not item.get("name") and "name" in frame and frame["name"].notna().any():
                item["name"] = str(frame.loc[frame["name"].notna(), "name"].iloc[0])
            if not item.get("market") and "market" in frame and frame["market"].notna().any():
                item["market"] = str(frame.loc[frame["market"].notna(), "market"].iloc[0])
            item.update({"manifest": str(manifest), "archived_at": payload.get("archived_at") or "旧条目未记录",
                         "source_scope": payload.get("source_scope") or item.get("保存方式") or "未记录"})
            entries.append(item)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            errors.append(f"回收条目 {manifest.parent.name} 无法读取：{exc}")
    return entries, errors


def restore_entry(manifest: Path, data_dir: Path, temporary_base: Path, sid: str) -> dict:
    payload = json.loads(manifest.read_text())
    info = payload["item"]
    source = manifest.parent
    expected = cache_paths(info["symbol"], "daily", info["adjustment"], data_dir, info["asset_type"])
    service.validate_request(info["symbol"], "2000-01-01", "2000-01-01", info["adjustment"])
    if info["asset_type"] not in {"A股", "ETF"} or any(path.exists() for path in expected):
        raise ValueError("永久缓存已存在或证券类别无效，不能覆盖。")
    for path in expected:
        source_file = source / path.name
        if not source_file.is_file() or source_file.is_symlink():
            raise ValueError("回收文件不完整。")
    with protect(temporary_base, sid) as session:
        stage = session / "restore" / uuid.uuid4().hex
        for destination in cache_paths(info["symbol"], "daily", info["adjustment"], stage, info["asset_type"]):
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / destination.name, destination)
        frame, metadata = load_history_cache(info["symbol"], "daily", info["adjustment"], stage, info["asset_type"])
        save_history_cache(frame, metadata, info["symbol"], "daily", info["adjustment"], data_dir, info["asset_type"])
    return info


def _render_notice(notice) -> None:
    if not notice:
        return
    if isinstance(notice, str):
        st.success(notice)
        return
    for value in notice.get("success", []):
        st.success(value)
    for value in notice.get("skipped", []):
        st.warning(value)
    for value in notice.get("errors", []):
        st.error(value)


def _strategy_table(items):
    return [{
        "策略名称": item["name"],
        "说明": item.get("description") or "未填写",
        "买入条件": item["buy_summary"],
        "卖出条件": item["sell_summary"],
        "修改时间": item["updated_at"],
        "规则版本": item["rule_schema_version"],
    } for item in items]


def _render_strategy_management(root: Path) -> None:
    st.subheader("自定义策略")
    st.caption("这里只管理已明确选择“保存到本地”的策略；会话策略关闭应用后自动消失。")
    active = list_strategies(root=root)
    if not active:
        st.info("尚无本地自定义策略。请在“自定义策略回测”中设置、命名并保存。")
    else:
        st.dataframe(_strategy_table(active), hide_index=True, width="stretch")
        lookup = {item["strategy_id"]: item for item in active}
        chosen = st.multiselect("选择要移入回收区的策略", list(lookup), default=list(lookup)[:1],
                                format_func=lambda value: lookup[value]["name"], key="strategy_manage_selection")
        digest = hashlib.sha256("|".join(sorted(chosen)).encode()).hexdigest()[:12]
        confirm = st.checkbox(f"我确认将所选{len(chosen)}个策略移入回收区",
                              key=f"strategy_archive_confirm_{digest}", disabled=not chosen)
        if st.button("批量移入策略回收区", key="strategy_archive", disabled=not chosen or not confirm):
            outcome = {"success": [], "errors": []}
            for identifier in chosen:
                try:
                    profile = archive_strategy(identifier, root=root)
                    outcome["success"].append(f"已移入回收区：{profile['name']}。")
                    if st.session_state.get("custom_strategy_active_id") == identifier:
                        st.session_state.pop("custom_strategy_active_id", None)
                        st.session_state.pop("custom_strategy_active_source", None)
                except (OSError, StrategyStoreError) as exc:
                    outcome["errors"].append(f"{lookup[identifier]['name']}：{exc}")
            invalidate_results(st.session_state)
            st.session_state.cache_management_notice = outcome
            st.rerun()
    st.markdown("#### 策略回收区")
    recovery = list_strategies(root=root, status="recovery")
    if not recovery:
        st.caption("策略回收区为空。")
        return
    st.dataframe(_strategy_table(recovery), hide_index=True, width="stretch")
    lookup = {item["strategy_id"]: item for item in recovery}
    chosen = st.multiselect("选择回收区策略", list(lookup), default=list(lookup)[:1],
                            format_func=lambda value: lookup[value]["name"], key="strategy_recovery_selection")
    digest = hashlib.sha256("|".join(sorted(chosen)).encode()).hexdigest()[:12]
    confirm = st.checkbox(f"我确认恢复所选{len(chosen)}个策略",
                          key=f"strategy_restore_confirm_{digest}", disabled=not chosen)
    if st.button("批量恢复策略", key="strategy_restore", disabled=not chosen or not confirm):
        outcome = {"success": [], "errors": []}
        for identifier in chosen:
            try:
                profile = restore_strategy(identifier, root=root)
                outcome["success"].append(f"已恢复：{profile['name']}。")
            except (OSError, StrategyStoreError) as exc:
                outcome["errors"].append(f"{lookup[identifier]['name']}：{exc}")
        st.session_state.cache_management_notice = outcome
        st.rerun()
    with st.expander("高级：永久删除回收区策略"):
        permanent = st.checkbox("我理解永久删除后无法恢复",
                                key=f"strategy_delete_confirm_{digest}", disabled=not chosen)
        if st.button("永久删除所选策略", key="strategy_delete_permanent",
                     disabled=not chosen or not permanent):
            outcome = {"success": [], "errors": []}
            for identifier in chosen:
                try:
                    profile = permanently_delete_strategy(identifier, root=root)
                    outcome["success"].append(f"已永久删除：{profile['name']}。")
                except (OSError, StrategyStoreError) as exc:
                    outcome["errors"].append(f"{lookup[identifier]['name']}：{exc}")
            st.session_state.cache_management_notice = outcome
            st.rerun()


def render_data_management():
    if st.button("← 返回首页", key="return_home_data_management"):
        return PAGE_HOME
    st.title("数据管理")
    notice = st.session_state.pop("cache_management_notice", None)
    _render_notice(notice)
    st.write("查看本机行情、批量管理缓存，以及配置研究报告和策略规则助手使用的模型。浏览器下载文件不在本页删除范围内。")
    sid = get_session_id(st.session_state)
    temporary = temporary_directory(DATA_DIR)
    local_tab, recovery_tab, strategy_tab, model_tab = st.tabs(
        ["本地行情", "缓存回收区", "自定义策略", "模型与 API"]
    )
    items = []
    for directory, scope in ((DATA_DIR, "永久"), (temporary, "本次临时")):
        entries, errors = service.list_cached_stock_history(directory)
        for error in errors:
            st.warning(error)
        for item in entries:
            paths = cache_paths(item["symbol"], "daily", item["adjustment"], directory, item["asset_type"])
            items.append({**item, "保存方式": scope, "大小KB": round(sum(p.stat().st_size for p in paths if p.exists()) / 1024, 1), "directory": str(directory)})
    with local_tab:
        if items:
            labels={"symbol":"股票/ETF代码","name":"名称","market":"市场","asset_type":"证券类别","adjustment":"缓存请求复权",
                    "start":"实际首条日期","end":"实际末条日期","updated_at":"更新时间","record_count":"记录数"}
            st.dataframe([{labels.get(k,k):v for k,v in item.items() if k != "directory"} for item in items], hide_index=True, width="stretch")
            lookup = {_identity(item): item for item in items}
            option_ids = list(lookup)
            chosen = st.multiselect("选择一个或多个缓存条目", option_ids, default=option_ids[:1],
                                    format_func=lambda value: _item_label(lookup[value]), key="cache_manage_entries")
            selected_items = [lookup[value] for value in chosen]
            digest = hashlib.sha256("|".join(sorted(chosen)).encode()).hexdigest()[:16]
            confirm = st.checkbox(f"我确认操作所选 {len(selected_items)} 个缓存条目的整份CSV和元数据，相关旧分析结果会失效",
                                  key=f"cache_manage_confirm_{digest}", disabled=not selected_items)
            promotable = len(selected_items) == 1 and selected_items[0]["保存方式"] == "本次临时"
            if st.button("将所选临时缓存保存为永久缓存", disabled=not confirm or not promotable, key="cache_promote",
                         help="转存一次只能选择一个本次临时条目；不会覆盖已有永久缓存。"):
                item = selected_items[0]
                try:
                    with protect(temporary_root(DATA_DIR), sid):
                        frame, meta = load_history_cache(item["symbol"], "daily", item["adjustment"], Path(item["directory"]), item["asset_type"])
                        if cache_paths(item["symbol"], "daily", item["adjustment"], DATA_DIR, item["asset_type"])[0].exists():
                            raise ValueError("永久缓存已存在；请先将旧条目移入回收区。")
                        save_history_cache(frame, meta, item["symbol"], "daily", item["adjustment"], DATA_DIR, item["asset_type"])
                    invalidate_results(st.session_state)
                    st.session_state.cache_management_notice={"success":["已保存永久副本；临时副本仍按会话规则清理。"]}
                    st.rerun()
                except (OSError, ValueError, StockDataError) as exc:
                    st.error(f"转存失败：{exc}")
            if st.button("批量移入回收区", disabled=not confirm or not selected_items, key="cache_archive"):
                outcome = {"success": [], "errors": []}
                recovery = DATA_DIR.parent / "cache_recovery"
                for item in selected_items:
                    try:
                        archive_entry(Path(item["directory"]), item, recovery)
                        outcome["success"].append("已移除：" + _item_label(item))
                    except (OSError, ValueError, StockDataError) as exc:
                        outcome["errors"].append(f"移除失败：{_item_label(item)} · {exc}")
                if outcome["success"]:
                    invalidate_results(st.session_state)
                st.session_state.cache_management_notice=outcome
                st.rerun()
        else:
            st.info("当前没有可管理的行情缓存。")
        st.caption("自动清理：会话无活跃信号约10分钟后清理。应用停止期间不运行清理，下次使用时兜底；不承诺关闭标签瞬间删除。")
        if st.button("结束本次使用并清理临时数据", key="cache_end_session"):
            try:
                clear_session(temporary_root(DATA_DIR), sid)
                invalidate_results(st.session_state)
                st.session_state.pop("temporary_cache_id", None)
                st.session_state.cache_management_notice={"success":["本次临时缓存已清理，永久缓存与下载文件保留。"]}
                st.rerun()
            except OSError as exc:
                st.error(f"清理未完成：{exc}")
    with recovery_tab:
        st.subheader("缓存回收区")
        st.caption("恢复操作不覆盖已有永久缓存；回收副本会继续保留，便于再次核对。")
        recovery = DATA_DIR.parent / "cache_recovery"
        recovered, recovery_errors = list_recovery_entries(recovery)
        for error in recovery_errors:
            st.warning(error)
        if not recovered:
            st.caption("回收区为空。")
        else:
            st.dataframe([{"证券代码":item.get("symbol"), "名称":item.get("name") or "名称未记录", "类别":item.get("asset_type"),
                           "市场":item.get("market") or "未记录", "复权":item.get("adjustment") or "不复权",
                           "实际首条日期":item.get("start"), "实际末条日期":item.get("end"), "记录数":item.get("record_count"),
                           "原保存方式":item.get("source_scope"), "移入时间":item.get("archived_at")}
                          for item in recovered], hide_index=True, width="stretch")
            recovery_lookup = {item["manifest"]: item for item in recovered}
            choices = st.multiselect("选择要恢复的回收条目", list(recovery_lookup), default=list(recovery_lookup)[:1],
                                     format_func=lambda value: _item_label(recovery_lookup[value]), key="cache_recovery_items")
            selected_recovery = [recovery_lookup[value] for value in choices]
            digest = hashlib.sha256("|".join(sorted(choices)).encode()).hexdigest()[:16]
            confirm = st.checkbox(f"我确认恢复所选 {len(choices)} 个条目到永久缓存（不覆盖）",
                                  key=f"cache_restore_confirm_{digest}", disabled=not choices)
            targets = [(item.get("asset_type"), item.get("symbol"), item.get("adjustment")) for item in selected_recovery]
            duplicate_targets = len(targets) != len(set(targets))
            if duplicate_targets:
                st.error("所选回收条目包含同一证券、同一复权方式的多个版本。请每个目标只选择一个版本后再恢复。")
            if st.button("批量恢复到永久缓存（不覆盖）", key="cache_restore",
                         disabled=not confirm or not choices or duplicate_targets):
                outcome = {"success": [], "errors": []}
                for item in selected_recovery:
                    try:
                        restored = restore_entry(Path(item["manifest"]), DATA_DIR, temporary_root(DATA_DIR), sid)
                        outcome["success"].append(f"已恢复：{restored.get('asset_type')} {restored.get('symbol')} {restored.get('name') or '名称未记录'} · {restored.get('adjustment') or '不复权'}")
                    except (OSError, ValueError, KeyError, StockDataError, json.JSONDecodeError) as exc:
                        outcome["errors"].append(f"恢复失败：{_item_label(item)} · {exc}")
                if outcome["success"]:
                    invalidate_results(st.session_state)
                st.session_state.cache_management_notice=outcome
                st.rerun()
    with strategy_tab:
        _render_strategy_management(DATA_DIR.parent / "user_strategies")
    with model_tab:
        from src.ai_report.settings import render_model_settings
        render_model_settings()
    return None
