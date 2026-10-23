# 项目文档中心

文档按用户阅读顺序和功能板块编号。第一次使用建议先阅读`01`，需要理解指标、回测或报告实现时再进入相应专题；`06`和`07`主要面向开发者与作品集审查。

## 01 · 快速开始

1. [完整使用说明书](01-getting-started/01-USER_GUIDE.md)  
   安装、启动、首次操作路线、页面说明、数据状态、常见问题和能力边界。

## 02 · 数据与缓存

1. [行情来源、成交量与成交额](02-data-and-cache/01-MARKET_DATA.md)  
   腾讯价格、成交字段补充、单位、来源合并、旧缓存和失败处理。
2. [本地策略与模型配置](02-data-and-cache/02-LOCAL_PROFILES.md)  
   自定义策略、模型配置、API Key、删除、回收与恢复。

## 03 · 金融数据可视化

1. [指标、图表与验证说明](03-visualization/01-VISUALIZATION.md)  
   K线、成交量、均线、收益率、波动率、回撤及计算边界。

## 04 · 策略回测

1. [回测设计、假设与绩效公式](04-backtesting/01-BACKTEST.md)  
   信号时序、次日执行、成本、现金持仓、基准、指标和已知局限。

## 05 · 研究报告

1. [研究报告设计与输出核验](05-research-reports/01-RESEARCH_REPORT.md)  
   结构化事实、模型配置、数字与日期核验、规则回退和导出。
2. [Prompt设计记录](05-research-reports/02-PROMPT_DESIGN.md)  
   从自由摘要到受控事实计划的模板迭代与取舍。

## 06 · 工程记录

1. [平台基础与交互设计记录](06-engineering-records/01-PLATFORM_FOUNDATION.md)
2. [图表、策略和新手体验记录](06-engineering-records/02-INTERACTION_AND_STRATEGY.md)
3. [交易日、模型和缓存管理记录](06-engineering-records/03-CALENDAR_AND_PERSISTENCE.md)
4. [旧资金流子系统移除记录](06-engineering-records/04-LEGACY_REMOVAL.md)
5. [源码来源审计](06-engineering-records/05-SOURCE_PROVENANCE.md)

## 07 · 验证记录

1. [平台基础回归](07-validation/test-results/01-PLATFORM_PYTEST_2026-10-22.txt)
2. [交互与策略回归](07-validation/test-results/02-INTERACTION_AND_STRATEGY_PYTEST_2026-10-22.txt)
3. [行情与缓存集成记录](07-validation/test-results/03-INTERACTION_INTEGRATION_2026-10-22.json)
4. [本地策略与模型配置回归](07-validation/test-results/04-LOCAL_PROFILES_PYTEST_2026-10-22.txt)
5. [交易日与缓存管理回归](07-validation/test-results/05-CALENDAR_AND_PERSISTENCE_PYTEST_2026-10-22.txt)
6. [发布准备版完整回归](07-validation/test-results/06-RELEASE_CANDIDATE_PYTEST_2026-10-23.txt)

这些原始文件记录对应开发阶段的实际结果，README中的当前测试结论以最新完整回归为准。

## 08 · 页面截图

页面截图保存在[`08-assets/screenshots/`](08-assets/screenshots/)，用于README、专题文档和作品集展示。截图中的行情与结果只代表当时的缓存、日期和研究参数。

## 返回

- [项目首页](../README.md)
- [第三方与历史参考声明](../THIRD_PARTY_NOTICES.md)
- [MIT许可证](../LICENSE)
