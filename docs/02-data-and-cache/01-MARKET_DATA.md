# 行情来源、成交量与成交额

验证日期：2026-10-20。真实请求只使用腾讯及新浪，没有访问东方财富，也不需要行情账户。集成验证使用临时缓存目录，结束后清理，不覆盖用户原有行情文件。

## 模块与数据流

```text
Streamlit 明确点击获取 / 强制刷新
  → service.get_stock_history：先查询独立股票行情缓存
  → provider.fetch_stock_history：腾讯复权 OHLC、成交量
  → turnover.fetch_sina_turnover：新浪原始日线成交量、成交额
  → normalize_turnover：字段、日期、数值、单位及重复数据校验
  → enrich_turnover：按证券与交易日期对齐并核验成交量
  → 原子写入 CSV + JSON 元数据
  → 页面预览、来源与校验表、CSV 导出
```

- 腾讯接口：`https://web.ifzq.gtimg.cn/appstock/app/fqkline/get`，保留 qfq/hfq OHLC。
- 新浪接口：`https://finance.sina.com.cn/realstock/company/{sh|sz}{symbol}/hisdata_klc2/klc_kl.js`。使用当前已安装 AKShare 的 `hk_js_decode` 和 `mini-racer` 解码，仅提取数据，不执行下载的 JS。
- 新浪每次返回整段历史，本项目在本地筛选日期，不能称为精确增量下载。缓存命中不请求两家接口。
- 新浪文档没有完整列出成交额；本次真实响应解码后确实包含 `amount`。该字段是运行时校验的必要字段，上游删除它时会明确提示补充失败。

## 单位、合并及来源

- 腾讯原始 `volume` 为手，换算为股（A 股）或份（ETF）后，再与新浪比较。新浪返回更精细的整数股数/份数。
- 样本同日成交量差不足一手。合并允许最多 100 股/份的绝对差，作为手单位舍入容差；这只是两个来源的一致性检查，不是对行情绝对真实性的证明。
- 任意一源为零、另一源非零时，无论差值大小均标为冲突。
- 核验通过才同时采用新浪成交量、成交额。使用左连接，不添加腾讯没有的交易日期，不用收盘价乘成交量估算真实成交额。
- 成交额统一为元，保留历史真实交易金额，不随 qfq/hfq 调整。
- 有新浪原始高低价时，进一步校验 `amount / volume` 是否落在未复权日内价格区间内，以识别明显单位变化。不会使用腾讯复权价格做此判断。
- 同日期不同成交量或成交额不会静默去重；相同重复行可以去重。
- 每行增加 `volume_unit`、`amount_unit`、`volume_source`、`amount_source`、`turnover_status`、`turnover_message`、`turnover_fetched_at`。原有 `source/provider` 记录价格来源。CSV 导出包含这些字段。

## 失败与旧缓存

- 新浪失败、缺日或校验冲突：保留腾讯 OHLC 和换算后的成交量，成交额为空，并记录具体原因。
- 已有核验通过的成交数据在刷新时失败：保留原成交值和原采集时间，标记 `STALE`，不冒充本次新数据。
- 旧缓存未声明单位时：以 `LEGACY` / `UNKNOWN` 标记，不猜测单位、不直接改变原值。页面和 CSV 保留逐行状态；使用前请强制刷新所需区间，不能把混合单位的旧记录直接用于后续分析。
- 进入页面、修改选择、普通 rerun 均不自动下载。旧缓存补充及失败重试通过“强制刷新数据”显式触发；缓存失败采用原有原子写入与回滚机制。
- `LIVE_FETCHED` / `CACHE` 描述主要行情的获取状态；`turnover_status` 独立描述成交字段是否完整，不会将部分字段失败伪装为全部成功。

## 真实接口验证

请求区间：2026-10-15 至 2026-10-19。

| 证券类别 | 代码 | 复权 | 返回条数 | 成交字段核验 | 第二次请求 |
| --- | --- | --- | ---: | --- | --- |
| A 股 | 600519 | qfq | 3 | COMPLETE | CACHE |
| A 股 | 600519 | hfq | 3 | COMPLETE | CACHE |
| ETF | 159516 | qfq | 3 | COMPLETE | CACHE |
| ETF | 159516 | hfq | 3 | COMPLETE | CACHE |
| A 股 | 000001 | qfq | 3 | COMPLETE | CACHE |
| ETF | 510300 | qfq | 3 | COMPLETE | CACHE |

六组首次结果均为 `LIVE_FETCHED`，成交额非空记录均为 3，成交额来源均为 `sina.klc_kl`。

长区间额外验证：159516，qfq，2023-10-01 至 2026-10-19，共 734 条，728 条 `COMPLETE`、6 条 `CONFLICT`。例如 2023-12-11、12-13、12-14、12-15、12-18，新浪返回零成交，而腾讯返回非零成交量；小区间重新请求 qfq/hfq 后仍观察到该差异。本项目保留腾讯成交量，冲突日成交额留空，不把这些日期解释为零成交。上游差异原因尚未确定。

## 离线验证

运行命令：

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider
```

成交数据补充完成时的实际输出：`58 passed in 1.84s`，失败 0 项。下文记录后续兼容性修复及更新的测试结果。

覆盖成交字段规范化、两类证券及两种复权、单位变化、来源冲突、零值占位、缺失日期、重复日期、网络失败、缓存重复请求、刷新保留旧值、旧缓存兼容，以及 Streamlit 按钮后展示和 rerun 不重复下载。

## 本次环境

- macOS arm64，Python 3.11.15
- AKShare 1.19.1，mini-racer 0.14.1
- pandas 3.0.6，requests 2.34.2
- Streamlit 1.66.0，pytest 9.1.1

上述真实接口结果只说明本次样本和网络环境可用，不代表所有证券和日期均完整。沪深以外证券的支持范围没有扩展。

参考实现：[AKShare 新浪基金历史行情解码](https://github.com/akfamily/akshare/blob/main/akshare/fund/fund_etf_sina.py)。以当前安装版本和真实返回字段为准。

## 301660 原始 day 字段兼容修复

同日用户反馈：301660 请求 qfq 时提示没有 `qfqday`。真实复现发现，腾讯请求成功（code=0），但请求 qfq 或 hfq 均只返回 `day`，旧解析器固定读取请求对应的复权字段，因此误报为证券不受支持。

修复规则：

- 优先读取请求对应的 `qfqday` / `hfqday`，缺少该字段时才读取 `day`。
- 不用另一种复权序列代替用户所选序列；错误响应、格式错误仍然报错。
- 增加 `requested_adjustment` 和 `price_series`，原有行级 `adjustment` 保存实际价格方式：`day` 对应 `none`，不会把原始价格标为已复权。
- 缓存路径按请求方式区分；路径中的 qfq/hfq 是请求标识，分析时必须检查行级实际复权方式。缓存及 CSV 均保留实际系列，页面对 `day` 回退显示警告。
- 不能仅凭返回 `day` 推断企业没有发生过权益事件。本次没有补造复权因子。

真实端到端复测：301660，请求 2025-10-20 至 2026-10-19。qfq/hfq 两组各返回 5 条，实际系列均为 `day`、实际复权 `none`，成交数据均为 `COMPLETE`，第二次均为 `CACHE`。验证在临时目录执行，不改用户原缓存。

已有样本回归：600519 和 159516，qfq，2026-10-15 至 2026-10-19，各 3 条，仍为 `qfqday` / `qfq`，成交数据均完整。

新增 `tests/test_tencent_series.py`，覆盖两种复权请求的 day 返回、复权字段优先级、错误响应、禁止错用另一复权序列、缓存实际状态，以及页面提示。离线完整测试实际输出：`68 passed in 1.94s`。

官方上市日期参考：[深交所 301660 上市公告](https://www.szse.cn/www/disclosure/notice/company/t20261012_623256.html)。腾讯适配字段处理参考：[AKShare 腾讯日线实现](https://github.com/akfamily/akshare/blob/main/akshare/stock_feature/stock_hist_tx.py)。
