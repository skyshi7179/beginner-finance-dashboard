"""User-confirmed acquisition from research pages, reusing the data service."""
import streamlit as st
from src.stock_data.symbols import get_stock_universe,get_etf_universe,search_stocks
from src.stock_data.errors import StockDataError
from src.stock_data.workspace import get_stock_history,storage_choice
from src.stock_data.context import save_research_context


def search_shortcut(prefix):
    with st.expander("搜索其他股票 / ETF（支持尚未缓存证券）"):
        asset=st.selectbox("证券类别",["A股","ETF"],key=f"{prefix}_lookup_asset")
        with st.form(f"{prefix}_lookup_form"):
            query=st.text_input("代码或中文名称",key=f"{prefix}_lookup_query")
            submit=st.form_submit_button("搜索证券")
        signature=(asset,query.strip())
        if submit:
            try:
                universe,_=(get_etf_universe if asset=="ETF" else get_stock_universe)(allow_fetch_if_missing=True)
                st.session_state[f"{prefix}_matches"]=(signature,search_stocks(query,universe).to_dict("records"))
            except StockDataError as exc:st.error(str(exc))
        saved=st.session_state.get(f"{prefix}_matches")
        if saved and saved[0]==signature:
            results=saved[1]
            if not results:st.info("当前代码表未找到匹配项，可前往数据获取刷新代码表。")
            else:
                index=st.selectbox("确认目标证券",range(len(results)),index=None,placeholder="请选择，名称模糊匹配不会自动采用",format_func=lambda i:f"{results[i]['symbol']} {results[i]['name']} · {results[i]['market']}",key=f"{prefix}_lookup_match")
                if st.button("使用所选证券",disabled=index is None,key=f"{prefix}_use_security"):
                    context=st.session_state.get("research_context",{})
                    st.session_state.research_context={**context,"selected":results[index],"asset_type":asset}
                    st.session_state.pop(f"{prefix}_security",None)
                    st.rerun()


def _dismiss():
    st.session_state.pop("inline_dialog_request", None)


@st.dialog("所选区间缺少行情",width="small",on_dismiss=_dismiss)
def _fetch_dialog(selected,start,end,adjustment,data_dir,prefix):
    st.write(f"{selected['symbol']} {selected.get('name','')}，{start}至{end}。确认后复用数据获取服务补齐缓存，不会自动运行回测。")
    mode=storage_choice(f"{prefix}_inline_storage")
    if st.button("确认获取缺失数据",type="primary",key=f"{prefix}_confirm_fetch"):
        try:
            with st.spinner("检查缓存并获取缺失区间…"):
                result=get_stock_history(selected["symbol"],start,end,adjustment=adjustment,asset_type=selected["asset_type"],
                    name=selected.get("name",""),market=selected.get("market",""),data_dir=data_dir,storage_mode=mode)
            if result.status in {"CACHE","LIVE_FETCHED"}:
                _dismiss()
                st.session_state[f"{prefix}_fetch_notice"]=result.message
                save_research_context(st.session_state,selected,selected["asset_type"],start,end,adjustment)
                st.rerun()
            else:st.error(result.message)
        except (StockDataError,OSError,ValueError) as exc:st.error(f"未获取成功：{exc}")
    if st.button("前往数据获取页面",key=f"{prefix}_dialog_acquisition"):
        _dismiss()
        from src.navigation import navigate_to,PAGE_DATA_ACQUISITION
        navigate_to(st.session_state,PAGE_DATA_ACQUISITION); st.rerun()


def offer_fetch(selected,start,end,adjustment,data_dir,prefix):
    if st.button("获取缺失数据并继续",type="primary",key=f"{prefix}_inline_fetch"):
        st.session_state.inline_dialog_request=prefix
    if st.session_state.get("inline_dialog_request")==prefix:
        _fetch_dialog(selected,start,end,adjustment,data_dir,prefix)
