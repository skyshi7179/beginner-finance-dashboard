"""Downloadable run artifacts contain parameters and research-unit labels."""

from dataclasses import asdict
from io import BytesIO
import json
import zipfile

from src.backtest.engine import BacktestResult


def export_run(result: BacktestResult, context: dict) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for filename, frame in (("daily_equity.csv", result.equity), ("orders.csv", result.orders),
                                 ("filled_trades.csv", result.trades), ("benchmark_orders.csv", result.benchmark_orders),
                                 ("performance.csv", result.metrics.reset_index())):
            bundle.writestr(filename, frame.to_csv(index=False, date_format="%Y-%m-%d").encode("utf-8-sig"))
        bundle.writestr("assumptions.json", json.dumps({"context": context, "params": asdict(result.params),
                         "metadata": result.metadata, "warnings": result.warnings,
                         "units": "Normalized research units; not real CNY or executable shares.",
                         "execution": "Close condition signal; next cached bar open. Untradable/gap>7 cancels. No forced end sale."},
                         ensure_ascii=False, indent=2, allow_nan=False))
    return buffer.getvalue()
