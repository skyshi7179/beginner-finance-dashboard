# 源码来源审计

审计日期：2026-10-23。

## 1. 审计目的

区分当前项目的独立实现、通用框架用法、公开数据事实和早期参考代码，避免把Streamlit等公开API或常见软件工程模式误认为上游项目资产，同时保留必要的历史透明度。

对照对象：`JA0508/fund-flow-monitor`提交`7f4b0893abd4ed2c1817aa0e5c4f2cd149d824d3`。审计使用该提交内容，不把`项目1`工作区中尚未提交的后续首页修改算作上游作者代码。

## 2. 方法

1. 通过`git archive HEAD`读取上游提交的197个受控文件。
2. 排除`.git`、`.venv`、真实缓存、字节码和二进制截图，审计当前106个可发布文本/源码文件。
3. 对同路径文件进行逐字节比较和文本相似度比较。
4. 对当前Python文件和全部上游Python文件进行跨文件的标准化十词元连续块匹配，用于发现改名或移动的代码片段。
5. 人工复核唯一完全相同的数据文件、通用配置和少量颜色/时间常量。

相似度和词元扫描用于定位人工复核对象，不单独代替法律判断。

## 3. 结果

| 项目 | 结果 |
| --- | ---: |
| 上游受控文件 | 197 |
| 审计基线文件 | 106 |
| 新增来源审计与第三方声明后的当前文件 | 108 |
| 审计前同路径文件 | 8 |
| 审计前逐字相同文件 | 2 |
| 处理后同路径文件 | 7 |
| 处理后逐字相同文件 | 0 |
| 当前Python业务源码逐字相同 | 0 |
| 十词元跨文件业务代码匹配 | 0 |

审计前两个逐字相同文件是旧`LICENSE`和`config/market_session_calendar.json`。处理如下：

- 根`LICENSE`改为当前作者`Skyyyyyy (GitHub: skyshi7179)`的标准MIT许可证。
- 交易日期属于provider-derived市场日期事实。当前项目将其整理为`config/trading_calendar.json`，重写项目元数据并明确记录AKShare/Sina来源和非交易所权威边界；不再保留旧资金流观察域描述。

其余同路径文件：

- `app.py`文本相似度约0.4%，当前文件是只包含六个页面的轻量路由。
- `README.md`文本相似度约0.6%，内容描述当前股票与ETF研究平台。
- `.gitignore`、`.streamlit/config.toml`和`requirements.txt`包含各工具要求的通用配置行，当前内容已按缓存、主题和依赖重新配置。
- `src/__init__.py`仅是Python包标记和一行当前项目说明。

人工复核还确认：`src/time_utils.py`使用Python标准库`datetime`和`zoneinfo`独立实现北京时间；`src/chart_theme.py`只保留两个通用十六进制颜色值，不包含上游绘图函数或业务逻辑。

## 4. 当前项目作者范围

当前以下业务链路由本项目围绕新的产品目标独立实现：

- A股和ETF搜索、历史行情接入、字段标准化和缓存生命周期。
- K线、成交量、收益、均线和回撤指标及交互图表。
- 双均线、通道、RSI、研究网格和组合条件策略回测。
- 结构化研究报告、模型配置、数字/日期/证券身份核验和规则回退。
- 首页、导航、新手说明、数据管理、策略保存与自动化测试。

使用Python、Streamlit、Pandas、Plotly、AKShare及常见类、函数、缓存和路由模式，属于第三方框架API或通用工程方法，不构成对早期参考仓库具体业务代码的保留。

## 5. 许可结论与记录方式

当前仓库以`Skyyyyyy (GitHub: skyshi7179)`作为作者和维护者，使用MIT许可证发布。`THIRD_PARTY_NOTICES.md`保留早期学习参考和数据来源说明；该历史记录不表示参考仓库拥有当前独立开发代码。

如果以后重新引入外部代码、数据文件或复制实质性实现，应在合并时记录来源、版本、许可证和修改范围，并重新运行本审计。

## 6. 修改后验证

```bash
PYTHONPYCACHEPREFIX=/private/tmp/provenance_compile \
  .venv/bin/python -m compileall -q app.py src tests

PYTHONPYCACHEPREFIX=/private/tmp/provenance_pytest \
  .venv/bin/python -m pytest -q -p no:cacheprovider --tb=short
```

- 编译检查：退出码0。
- 完整离线回归：293通过，0失败，耗时14.83秒。
- 修改后哈希复核：与上游同路径逐字相同文件为0。
- 未调用真实行情或LLM接口，未部署，未推送GitHub。
