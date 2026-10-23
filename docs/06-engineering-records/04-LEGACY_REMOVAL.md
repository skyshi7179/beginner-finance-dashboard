# 旧板块资金流子系统移除记录

日期：2026-10-22。

## 目标

将应用收敛为股票与ETF研究平台，完整移除不再使用的板块资金流页面、采集器、快照仓库、主题分析、样例行情、旧测试与运行日志。后续若重新增加板块类功能，应基于当前统一数据契约单独设计，不能恢复或伪装旧模块。

## 当前应用边界

```text
Streamlit入口 app.py
  ├─ 股票 / ETF 数据获取      src/stock_data
  ├─ 金融数据可视化           src/stock_analysis
  ├─ 自定义策略回测           src/backtest
  ├─ 研究报告                 src/ai_report
  └─ 数据与模型配置管理       src/stock_data/management.py
```

入口只导入以上模块。`src/navigation.py`只保留对应页面常量，页面重绘不会加载已移除业务。

## 已移除内容

- 板块资金流页面及路由、侧边栏设置和SAMPLE演示入口。
- AKShare行业资金流provider、采集命令、代理诊断及采集策略。
- CSV快照、SQLite历史索引、快照目录、主题雷达、日内热点、多日趋势、观察简报、持仓池与证据连续性代码。
- 旧模块专用配置、样例数据、测试、截图、说明文档和本机采集日志。
- 只为旧模块存在的依赖与全局UI代码。

交易日期事实整理为当前平台的`config/trading_calendar.json`，用于日期选择、节假日判断与回测连续性；文件内记录AKShare/Sina数据来源及非交易所权威边界。根许可证使用当前作者署名，早期参考关系另见`THIRD_PARTY_NOTICES.md`。

## 重构后的公共代码

- `src/time_utils.py`：北京时间获取，替代旧工具模块中的单一复用函数。
- `src/chart_theme.py`：股票图表红涨绿跌颜色，替代旧资金流图表模块。
- `src/ui_experience.py`：只保留当前页面需要的全局样式、主题和图表交互。
- `app.py`：缩减为显式页面路由，不包含业务计算和兼容旧页面的分支。

## 验证

```bash
PYTHONPYCACHEPREFIX=/private/tmp/financial_platform_compile \
  .venv/bin/python -m compileall -q app.py src tests

PYTHONPYCACHEPREFIX=/private/tmp/financial_platform_pytest \
  .venv/bin/python -m pytest -q -p no:cacheprovider --tb=short
```

- 编译检查：退出码0。
- 完整离线回归：293通过，0失败，耗时13.19秒。
- AST导入检查：62个Python文件成功解析，没有指向已删除本地模块的导入。
- Streamlit真实启动：本机18600端口启动成功，`/_stcore/health`返回`ok`，随后正常停止测试进程。
- 清理后`src`约444KB、`tests`约168KB；`.venv`仍约537MB，是本机依赖环境，不会提交Git。

测试使用固定数据与Mock，不依赖外部行情或LLM网络。此次没有执行真实行情下载、远程部署或GitHub推送。
