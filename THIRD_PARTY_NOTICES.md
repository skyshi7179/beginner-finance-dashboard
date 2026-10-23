# Third-Party and Historical Reference Notices

## Historical reference

The early study phase of this project referenced the open-source repository
[`JA0508/fund-flow-monitor`](https://github.com/JA0508/fund-flow-monitor),
which was distributed under the MIT License with the notice:

```text
Copyright (c) 2026 JA0508
```

The former sector fund-flow subsystem, its providers, snapshot warehouse,
analysis modules, sample data, command-line tools, tests, and user interface
were removed from the current project. A file-level audit against upstream
commit `7f4b0893abd4ed2c1817aa0e5c4f2cd149d824d3` found no substantial upstream
business source code in the current Python application.

This notice records the project's development history. It does not assign
ownership of the independently developed stock and ETF research platform to
the referenced repository.

## Market calendar data

`config/trading_calendar.json` contains provider-derived trading-date facts
obtained through AKShare's `tool_trade_date_hist_sina` interface, whose
underlying data source is Sina Finance. The file is an offline research aid
and is explicitly not described as an exchange-authoritative calendar.

## Python dependencies

Streamlit, Pandas, NumPy, Plotly, AKShare, Requests, MiniRacer, Keyring, and
Pytest remain subject to their respective licenses. They are installed as
dependencies and are not vendored into this repository.
