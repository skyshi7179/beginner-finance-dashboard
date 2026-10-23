# 第二轮新手体验与策略优化记录

日期：2026-10-22。工作目录：`/Users/sky/Documents/Study/项目1-1`。

## 环境与版本保护

- macOS Apple Silicon（Darwin arm64）；Python3.11.15。
- Streamlit1.66.0、Pandas3.0.6、NumPy2.4.6、Plotly7.1.0。
- 开始与结束时Git文件仍为未跟踪状态，没有覆盖提交、重置、推送或部署。
- 修改前源码备份：`/private/tmp/novice_optimization_20261022_before.tar.gz`。该备份不含密钥、真实行情或虚拟环境。
- 既有`data/stocks`的18个文件SHA256前后相同。真实在线测试只写入`/private/tmp/novice_optimization_integration`。
- 旧业务子系统已在后续独立化重构中完整移除，当前应用只保留股票与ETF研究工作流。

## 已实现

### 收益率教学

可视化明确展示：**平均日收益为正，区间最终收益仍可能为零或负数。**

区间价格累计收益为`C末/C首−1`，可理解为以首个有效收盘为基准持有至终点的价格表现；不是完整实盘总回报。
日涨跌幅均值是有效简单日收益的算术平均；100→110→100，平均约+0.45%，累计0%。

已识别跨本地日历声明缺失交易日的变化保存在`observation_return`，不计入`daily_return`均值和波动率；累计价格表现仍由起止价格计算。日历未覆盖时不能证明所有缺口均被识别。

### 悬浮外观与逐图交互

- 右下角固定按钮直接选择浅色、深色、跟随系统；浏览器本地保存偏好。
- 通过受控、独立HTML适配调用Streamlit原生主题菜单动作，使原生输入、弹窗、表格及图表同步适配，不修改全局服务器配置。
- 当前固定Streamlit1.66.0，主题适配依赖其前端DOM标识。框架升级需重新验收；若标识不匹配，显示明确错误与原生菜单回退提示。HTML只读取仓库可信文件，不插入LLM/用户HTML。
- 每图有开启/关闭缩放和重置按钮；最多一张图拥有滚轮缩放，开启下一张会释放上一张。卡片悬停高亮，重置通过图表独立revision实现。
- 默认滚轮滚页面。开启缩放后，普通双指滑动仍可能缩放；未声称已可靠区分鼠标滚轮、触控板双指滑动与捏合。Mac物理捏合、跨浏览器及操作系统切色的完整矩阵尚未测试。

### 全部历史与缓存范围

- 数据获取快捷项：全部历史（上市以来，尽数据源所能）；应用只设置日期，点击获取才联网。
- 未独立取得上市日期时用1990-01-01作为查询起点，复用腾讯向前分页，增加分页安全上限与不推进错误，失败不提交半份下载。
- `history_completeness=UNVERIFIED`，记录实际最早/最晚日期，成功查询覆盖不等于交易所核验的完整上市历史。
- 可视化/回测的全部已缓存区间使用实际CSV最早与最晚记录，不使用1990等查询覆盖边界，不自动下载；应用后关闭“至今”覆盖，手工改日期退出全部模式。

### 量额基础规范与缺失

- 入库基础单位：股票股、ETF份、成交额人民币元；图表整张统一显示股/份、万股/万份或元/万元/亿元，悬停同步，CSV保留基础单位。
- 转换基于来源适配器的明确契约。旧缓存没有单位时不按大小猜测，保留原始字段供审计，只将无法确认的绘图/计算字段留空。
- 补充源采用显式部分校验模式：数值异常只影响该日该字段，其他日期继续使用；必要字段结构、日期及同日冲突仍会拒绝补充。
- 图表保留日期和有效K线，量额缺失柱留空，×只表示缺失、不代表0；提供缺失日期表。不同来源、单位及陈旧状态仍可追溯。
- 依赖成交量的条件在缺失日不补零；上一交易观测为零、缺失或跨已识别缺口时，倍数条件不可判断。回测本身仍保留原有不可交易日取消规则。

### 策略教学与规则v2

- 五类策略均有数字例子、逐参数含义、调整影响和限制；MA5明确为5个交易观测的平均价，5/20只是教学默认值。
- 窗口、手续费、滑点、初始研究资金旁有帮助。非均线策略不再要求填写额外MA参数。
- 条件支持研究OHLC、MA、简单RSI、量额、成交量均线、前收盘变化、持仓收益及持仓观测日数。
- 允许历史偏移0..250、正倍数、比较与交叉、嵌套且/或；最多3层组、60节点，每组8项；JSON白名单，无eval与代码执行。
- 不同量纲字段不能直接比较；收益率常量可填百分比，内部保存小数。规则生成括号中文预览，修改必须重新确认。
- 可载入“成交量≥上一交易观测2倍且上涨；持仓收益≥10%或≤−10%或MA5下穿MA20”示例。
- 空仓只判断买入，持仓只判断卖出。缺失采用三值逻辑；例如“真或未知”为真，“假且未知”为假，其余不可确定的结果不触发订单。
- 每次预设载入使用独立控件代次，修复真实浏览器中旧控件值回写覆盖预设的问题；确认身份包含规则摘要与代次。JSON及编辑器跨页面保留。
- 可选AI草案适配v2结构，仅显式点击调用；只转换规则，仍需结构/单位校验和用户确认。没有模型也能手工使用。

### 持仓成本与止盈止损

信号所需历史指标可预先计算，账户条件必须在逐日循环中、当日开盘成交后、收盘判断时计算。

`持仓收益率=当前收盘持仓市值/剩余成本−1`。买入成本包含已发生手续费与滑点；部分卖出同比例移除成本，完全清仓重置，新一笔重新建账。不预扣未来卖出费用。

止盈止损均是收盘触发、下一条经本地日历确认连续的交易日开盘执行，仍保留原100模拟单位、无效日取消、末日不强制卖出规则。长假休市不按自然日长度取消；开市日缺记录或日历未覆盖时保守取消。10%阈值不保证最终成交收益/最大亏损为10%。研究价格首开盘=100，不是精确人民币实盘模型，也不推断日内止盈止损先后。

## 数据流与函数职责

`页面主动获取 → workspace.get_stock_history → 原service缓存检查 → 腾讯价格＋新浪量额适配 → 逐字段校验/来源记录 → 安全CSV与JSON保存`。

`缓存只读 → canonical_turnover规范分析视图 → calculate_indicators或run_backtest → 独立Plotly构图 → render_chart按图管理交互`。

`条件编辑器/可选模型 → validate_rules → market_operands → 逐日evaluate_at＋Portfolio.close_state → 下一开盘execute_order/execute_target → 净值、日志及导出`。

前端外观/图表变化不调用行情服务或模型。规则例子不是推荐策略。

## 实际验证

- 修改前基线：**486 passed in 51.01s**。
- 最终全量：**521 passed in 55.24s，0失败**，含35项新增验收。命令与原始输出见[test_results/02-INTERACTION_AND_STRATEGY_PYTEST_2026-10-22.txt](../07-validation/test-results/02-INTERACTION_AND_STRATEGY_PYTEST_2026-10-22.txt)。
- Python源码编译：129个文件通过；不写字节码。VS Code启动JSON解析通过。
- 新测试覆盖：正均值但零/负累计、单位换算及未知单位、部分缺失、混合且/或及未知逻辑、未来偏移/单位/深度拒绝、量倍数边界与前日0、持仓成本/部分卖出/清仓重置、10%浮点边界、次日开盘跳空、未来数据扰动不改变历史、日历缺口、逐图状态/重置、全部日期与手工修改、预设确认/JSON跨页面及AI Mock。

### 真实网络与缓存复用（独立于离线pytest）

请求1990-01-01至2026-10-21，全部写入临时测试目录：

| 证券 | 首次状态 | 实际记录与范围 | 单位与实际复权 | 二次请求 |
|---|---|---|---|---|
| ETF159325 | LIVE_FETCHED | 472条，2024-11-08至2026-10-21 | 份/元，qfq，成交额472条非空 | CACHE |
| A股301660 | LIVE_FETCHED | 7条，2026-10-13至2026-10-21 | 股/元，实际none，明确提示未返回请求的qfq；成交额7条非空 | CACHE |

实际输出见[test_results/03-INTERACTION_INTEGRATION_2026-10-22.json](../07-validation/test-results/03-INTERACTION_INTEGRATION_2026-10-22.json)。这证明本次取得的范围与缓存行为，不证明上市完整性或所有证券均支持。

### 真实缓存回测与浏览器

- 使用项目既有ETF159325前复权缓存，243条，2025-10-20至2026-10-20，没有更新或覆盖它。
- 放量/持仓盈亏示例：策略累计−11.10%，最大回撤11.13%，2笔成交；买入持有13.16%。仅记录该历史区间、默认费率0.03%及滑点0.05%的研究结果。
- CLI与真实浏览器结果一致；所有成交日期晚于对应信号日期。负结果与超过10%的回撤均如实展示，未挑选盈利结果。
- 浏览器已操作：新首页、返回路径、已缓存证券选择、全部缓存日期、策略教程、载入预设、确认与运行、图表独立缩放状态、深浅切换和滚动中的悬浮入口。
- 真实LLM未调用；使用Mock完成规则草案离线验证。

## 修改文件及原因

- `.vscode/launch.json`
- `README.md`
- `docs/01-getting-started/01-USER_GUIDE.md`
- `requirements.txt`
- `src/backtest/charts.py`
- `src/backtest/engine.py`
- `src/backtest/export.py`
- `src/backtest/page.py`
- `src/backtest/rule_assistant.py`
- `src/backtest/strategies.py`
- `src/backtest/strategy_controls.py`
- `src/date_controls.py`
- `src/home_page.py`
- `src/stock_analysis/charts.py`
- `src/stock_analysis/indicators.py`
- `src/stock_analysis/page.py`
- `src/stock_data/cache.py`
- `src/stock_data/page.py`
- `src/stock_data/provider.py`
- `src/stock_data/service.py`
- `src/stock_data/turnover.py`
- `src/ui_experience.py`
- `tests/test_navigation.py`
- `tests/test_platform_improvements.py`
- `tests/test_stock_visualization.py`

新增源码与测试：

- `src/backtest/learning.py`
- `src/stock_data/units.py`
- `src/theme_switcher.html`
- `tests/test_novice_optimization.py`

新增本记录、原始测试日志、真实集成JSON及页面截图；当前分别整理到`docs/07-validation/`和`docs/08-assets/`。使用说明书与README同步修改，历史工程记录保留。

## 面试解释

可说明：先统一来源与单位，再将缺失当成显式数据质量状态；规则保存为受限的条件树而非执行代码；市场指标与账户状态分开，后者在成交后的事件循环中判断，避免前视偏差。浏览器状态另做真实交互验收，因为AppTest不能完整模拟前端旧控件值回写。

## 验收结论

本轮本地功能、离线回归及两类真实行情获取：PASS。
完整上市历史独立证明、物理触控板捏合与跨浏览器矩阵、真实LLM集成：NOT RUN。
下一步先由用户体验验收；不自动进入新功能开发、远程推送或部署。


## 页面截图

- [浅色首页与悬浮外观选项](../08-assets/screenshots/novice_optimization_home_light.png)
- [深色页面与固定外观选项](../08-assets/screenshots/novice_optimization_home_dark.png)
- [每图独立缩放与量额单位](../08-assets/screenshots/novice_optimization_chart.png)
- [真实缓存自定义策略结果](../08-assets/screenshots/novice_optimization_backtest.png)

截图来自本机内置浏览器的窄窗口，因此核心卡片按响应式布局纵向排列；未用该截图声称完成全部桌面分辨率或手势矩阵验证。
