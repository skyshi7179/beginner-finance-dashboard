"""Durable research selections, separate from Streamlit widget lifetimes."""

from __future__ import annotations

from datetime import date
from typing import MutableMapping


ADJUSTMENT_LABELS = {"qfq": "前复权（qfq）", "hfq": "后复权（hfq）", "": "不复权（none）"}


def save_research_context(state: MutableMapping, selected: dict, asset_type: str,
                          start: date, end: date, adjustment: str) -> None:
    state["research_context"] = {"selected": dict(selected), "asset_type": asset_type,
                                 "start": start.isoformat(), "end": end.isoformat(), "adjustment": adjustment}


def restore_acquisition_context(state: MutableMapping) -> None:
    context = state.get("research_context")
    if not context:
        return
    if "stock_search_asset_type" not in state:
        state["stock_search_asset_type"] = context["asset_type"]
        state["stock_selected"] = dict(context["selected"])
        state["stock_selected_type"] = context["asset_type"]
        state["stock_search_results"] = []
    if "stock_history_dates" not in state:
        state["stock_history_dates"] = (date.fromisoformat(context["start"]), date.fromisoformat(context["end"]))
    if "stock_history_adjustment" not in state:
        adjustment = context["adjustment"] if context["adjustment"] in {"qfq", "hfq"} else "qfq"
        state["stock_history_adjustment"] = ADJUSTMENT_LABELS[adjustment]
