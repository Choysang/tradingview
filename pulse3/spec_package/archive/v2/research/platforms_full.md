# Pulse 主流交易平台能力研究与模块映射

核验截止：**2026-11-10**。覆盖 **10 家平台、56 项功能、42 个官方来源**。这是公开文档研究，未登录产品或实际调用收费数据/交易接口。

## 1. 结论：继承工作流与契约，组合成同一套系统

最有价值的继承不是把十个终端的页面堆到一起，而是把它们验证过的交互和能力接到同一证券身份、数据快照、策略规格、交易计划和账户账本上。TradingView提供图表与研究上下文；TrendSpider展示规则复用；QuantConnect展示分角色策略与多Alpha；国内数据终端补足A股字段、事件和产业语义；富途/IBKR展示账户和执行接口；雪球提供公开观点与研究组合的来源形式。上述判断是Pulse的设计归纳，各产品具体事实见逐项证据。

必须区分三个事实：图表软件有某功能，不代表可嵌入图表库附带该功能；数据API提供A股行情，不代表该账户可经其交易A股；平台展示策略业绩，不代表符合Pulse的统一成交和费用口径。它们是模块边界和数据语义，直接决定代码怎么实现。

每项表格左侧为**公开文档事实**，右侧为**Pulse设计推断**。`integrate`表示纳入规划，`research`表示须先实验。P0为闭环基础，P1为增强交互/能力，P2为验证后扩展；本报告没有声称已完成这些实现。

### 1.1 跨平台交叉核对得到的共同原则

| 共同原则 | 相互独立的产品证据 | Pulse决策 |
|---|---|---|
| 一次定义，多处使用 | TrendSpider Visual Scripts；通达信公式角色/转换；Choice条件接口 | 统一有类型Rule AST和FeatureDefinition，扫描/回测/警报共用。 |
| 研究上下文贯穿工作区 | TradingView多图同步；IBKR颜色分组；TrendSpider MTFA | InstrumentContext + 分组ContextBus，显式控制联动方向。 |
| 组合必须独立于单股图 | Pine单标的边界；QuantConnect多Alpha；TradingView/iFinD组合视图 | Alpha与PositionPolicy分角色，统一组合目标及账户归因。 |
| 数据接口是共享基础 | Wind数据服务；iFinD查询分类；Choice接口；富途订阅 | DataHub统一字段、订阅、快照、能力与水位，适配器可替换。 |
| 历史表现依赖成交/计算假设 | TradingView模拟撮合；雪球组合FAQ；富途成交回调 | 统一模拟语义；虚拟组合、回放、实盘账本类型清楚。 |

## 2. 最值得优先集成的12项

| 优先能力 | 对应条目 | 为什么纳入 |
|---|---|---|
| 一套条件贯通选股、回测和警报 | TS-F03 | 直接解决用户担心的模块重复与数据堵塞；同一想法从发现到运行不需要手工改写三份。 |
| 多周期联动个股工作台 | TV-F01 | 把年/月/周/日趋势与盘中量价放在同一上下文，降低页面切换和口径混乱。 |
| 多Alpha与操作策略有类型组合 | QC-F02 | 允许大模型、传统量价及学习型模型取长补短，同时保留期限和相关性，避免简单多数票。 |
| 统一字段、查询与订阅DataHub | IF-F01 | 选股、图表、回测和模型使用同一份数据语义，是接口共享的基础。 |
| 持仓与候选股事件驱动重算 | TV-F11 | 让最新消息沿行业和企业暴露更新具体排名、计划与解释，而不是只刷新资讯列表。 |
| 消息和行情同步的可恢复回放 | TV-F06 | 能验证“当时知道这些信息会怎么做”，也是策略自我改进最有价值的训练与复盘界面。 |
| 有成交依据的统一回测 | TV-F04 | 回测可逐笔解释，才能用结果淘汰和提升策略；相同引擎复用在线规则。 |
| 底仓感知的网格与做T操作模板 | FU-F01 | 把个股降成本要求落实为可执行状态机、库存和费用计算，而不是静态买卖箭头。 |
| 订单、成交与账户对账内核 | FU-F02 | 推荐、计划和账户事实能够闭环，策略评估才能使用真实执行结果。 |
| 收益归因与真实成本视图 | TV-F10 | 分别回答选股赚了多少、择时赚了多少、做T扣费后赚了多少，防止成本口径自我误导。 |
| 操作前后组合情景对比 | IB-F03 | 买入一个高分股票还要知道组合是否已集中暴露于同一行业和事件。 |
| 外部策略证据与统一复现 | XQ-F02 | 持续搜索的新战法先变成可验证候选，再进入可比实验与优胜劣汰，保留来源和有效范围。 |

## 3. 平台覆盖概览

| 平台 | 功能数 | 深读重点 |
|---|---:|---|
| TradingView | 13 | 图表、脚本、筛选、警报、回放及组合交互 |
| TrendSpider | 6 | 规则共享、多周期叠加、可视扫描与AI辅助 |
| QuantConnect | 6 | 策略角色、组合融合与历史/在线引擎 |
| IBKR TWS | 4 | 联动交易工作台、条件订单及组合情景 |
| 通达信 | 6 | A股公式语义、盘中选股、热点与产业链 |
| 同花顺 / iFinD | 4 | 结构化数据、问财入口及研究查询 |
| 东方财富 Choice | 5 | 资讯与日历接口、条件选股及数据浏览器 |
| 富途牛牛 / Futubull | 5 | 行情订阅、账户交易接口及网格模板 |
| Wind | 4 | 数据服务、跨资产输入及专业质量核对 |
| 雪球 | 3 | 观点、研究组合及外部策略证据口径 |

## 4. 关键功能详细映射

正文展开Top12；全部56项的完整事实、推断、数据依赖、A股改造、模块责任与验收条件见同目录 `platforms.json`。

### TradingView

#### TV-F01 · 多周期图表与同步分组 · P0 / integrate

**公开事实：** 多图可同步证券、周期、十字光标及时间范围；同一证券可同步绘图。 [官方来源 TV01](https://www.tradingview.com/support/solutions/43000629992-how-to-sync-the-charts-of-my-layout/)。

**设计推断：** 做年/月/周/日/分钟联动工作台；用带 group_id 的选择事件联动，不把全部组件强行绑定。

| 实施项 | 明确要求 |
|---|---|
| A股改造 | 跨午休、节假日及未完结周/月K线明确标识；允许同股不同周期。 |
| 数据依赖 | 标准证券ID、交易日历、带完结标志的多周期 OHLCV、图形对象 |
| 模块责任 | `market` |
| 验收 | 日/周/月图切换证券同步，周期保持各自设定；指向同一时刻时缺失数据不平移伪对齐。 |

#### TV-F04 · 可解释的策略回测报告 · P0 / integrate

**公开事实：** 策略测试使用模拟撮合；默认按图表OHLC推断柱内路径，也可使用较低周期数据增加细节。 [官方来源 TV04](https://www.tradingview.com/pine-script-docs/concepts/strategies/)。

**设计推断：** 成交假设要进入 BacktestSpec；报告展示数据粒度、延迟、成交约束及逐笔原因，图表点击可回到决策输入。

| 实施项 | 明确要求 |
|---|---|
| A股改造 | 通过统一撮合内核应用 T+1、停牌、涨跌停、数量及费用规则；触价不直接等于成交。 |
| 数据依赖 | 原始行情、交易规则版本、费用表、企业行动、订单与成交事件 |
| 模块责任 | `research` |
| 验收 | 同一模拟引擎在单步回放与批量回测下账本一致；柱内先后无法判定时显示假设，不能伪装成已知成交。 |

#### TV-F06 · 可恢复的历史交易回放 · P0 / integrate

**公开事实：** Replay Trading 可设初始资金和费用，并在历史图表上交易；官方说明当前会话结果不持久保存，可导出。 [官方来源 TV06](https://www.tradingview.com/support/solutions/43000691889-learn-to-trade-on-historical-data/)。

**设计推断：** 建立持久化 ReplaySession，保存时间游标、当时可见消息、计划版本、操作与账本；支持从任一检查点继续。

| 实施项 | 明确要求 |
|---|---|
| A股改造 | 回放切到历史交易日时，只展示当时已公布且已取得的资讯、财务与证券状态。 |
| 数据依赖 | point-in-time数据快照、行情事件、消息可用时间、持仓账本 |
| 模块责任 | `research` |
| 验收 | 暂停、退出、恢复后结果一致；未来新闻不能出现在回放侧栏或模型上下文中。 |

#### TV-F10 · 持仓导入与收益归因 · P0 / integrate

**公开事实：** Portfolios支持手工或文件导入交易，显示持仓、收益、费用及基准对比。 [官方来源 TV09](https://www.tradingview.com/support/solutions/43000760937-tradingview-portfolios-track-your-assets-know-your-trades/)。

**设计推断：** 用成交、现金流、分红等记账事件重建组合；同时展示已实现、未实现、费用、基准超额及策略贡献。

| 实施项 | 明确要求 |
|---|---|
| A股改造 | 按A股企业行动和现金流记账；区分原始成本、摊薄成本与做T净收益。 |
| 数据依赖 | 成交、资金流水、费用、分红送转、基准行情 |
| 模块责任 | `execution` |
| 验收 | 一次导入可重复执行而不重复记账；收益分解之和与账户净值变化核对一致。 |

#### TV-F11 · 持仓关联新闻与事件日历 · P0 / integrate

**公开事实：** 组合页面提供持仓相关资讯及收益/分红日历等视图。 [官方来源 TV09](https://www.tradingview.com/support/solutions/43000760937-tradingview-portfolios-track-your-assets-know-your-trades/)。

**设计推断：** 同一 EventRecord 供持仓日历、图表、选股解释和策略触发使用；重要消息到达后按暴露关系重算相关计划。

| 实施项 | 明确要求 |
|---|---|
| A股改造 | 扩展财报、解禁、股东会、回购、停复牌及政策事件；未来预告和已经发生分开。 |
| 数据依赖 | 统一事件表、证券/行业/供应链映射、公告时间、预定时间 |
| 模块责任 | `intelligence` |
| 验收 | 一个事件在四个页面显示同一来源和版本；仅受影响计划失效重算，其他证券结果可继续使用。 |

### TrendSpider

#### TS-F03 · 选股、测试、预警共用可视规则 · P0 / integrate

**公开事实：** Visual Scripts及同一规则界面可用于回测、扫描和多因素警报。 [官方来源 TS01](https://help.trendspider.com/kb/interface/interface-overview)。

**设计推断：** 这是最值得继承的交互原则：用户只定义一次条件树，编译为特征查询、历史评估和流式触发。

| 实施项 | 明确要求 |
|---|---|
| A股改造 | 条件类型包含可交易状态、已结算持仓及公告可用时间；前端可视块不能绕过这些字段。 |
| 数据依赖 | 规则AST、特征注册表、类型系统、版本化编译产物 |
| 模块责任 | `decision` |
| 验收 | 同一输入快照下，选股命中、回测触发和在线警报一致；规则变化统一生成新版本。 |

### QuantConnect

#### QC-F02 · 多Alpha统一合成 · P0 / integrate

**公开事实：** 多个Alpha可各自输出带名称的Insight，合并后的集合交给组合构建。 [官方来源 QC02](https://www.quantconnect.com/docs/v2/writing-algorithms/algorithm-framework/alpha/key-concepts)。

**设计推断：** 增加预测期限、校准收益分布、成本、相关性与策略归属；按可比较目标融合，保持分歧可见。

| 实施项 | 明确要求 |
|---|---|
| A股改造 | 短线事件信号和长线价值信号分期限建模；共同生成账户可执行目标。 |
| 数据依赖 | Alpha预测、期限、校准报告、协方差/相关性、费用估计 |
| 模块责任 | `decision` |
| 验收 | 高度相关策略不会因数量多就放大同一观点；冲突信号能显示取舍及最终权重。 |

### IBKR TWS

#### IB-F03 · 组合情景分析工作台 · P1 / integrate

**公开事实：** Risk Navigator可对真实或假设组合查看风险及情景变化。 [官方来源 IB02](https://www.ibkrguides.com/traderworkstation/open-risk-navigator.htm)。

**设计推断：** 将行业冲击、指数下跌、流动性变化和事件影响施加于当前/拟议组合；比较操作前后暴露。

| 实施项 | 明确要求 |
|---|---|
| A股改造 | 先实现A股行业/风格/集中度及跌停可卖性情景；复杂期权希腊值暂不进入主线。 |
| 数据依赖 | 持仓、行业/风格暴露、情景定义、相关性、流动性 |
| 模块责任 | `decision` |
| 验收 | 同一场景下能对比当前与拟议组合的变化，列出各持仓贡献和假设。 |

### 同花顺 / iFinD

#### IF-F01 · 统一数据字段与多频查询 · P0 / integrate

**公开事实：** 接口提供基础数据、时间序列、板块报表、宏观、高频与实时行情。 [官方来源 IF01](https://quantapi.5ifind.cn/gwstatic/static/ds_web/quantapi-web/)。

**设计推断：** DataHub的查询面统一证券集合、字段、时间区间和快照；字段注册说明单位、频率、版本及来源。

| 实施项 | 明确要求 |
|---|---|
| A股改造 | A股主数据为锚，国际市场和宏观系列作为解释输入；不同频率不强行前向填充为实时值。 |
| 数据依赖 | 字段字典、证券映射、数据版本、查询适配器 |
| 模块责任 | `data` |
| 验收 | 同一字段在选股、图表、模型上下文中值和单位一致；查询明确返回时效和缺失原因。 |

### 富途牛牛 / Futubull

#### FU-F01 · 可视化网格策略模板 · P1 / integrate

**公开事实：** 牛牛量化教程包含网格预设、可视条件、回测和启动/停止。 [官方来源 FU01](https://www.futunn.com/learn/grid-trading-strategy)。

**设计推断：** PositionPolicy提供网格模板，明确价格区间、间距、层数、资金、底仓和退出条件；网格只是可组合操作策略。

| 实施项 | 明确要求 |
|---|---|
| A股改造 | 以可卖底仓支持盘中操作；新增买入的结算状态、费用后有效间距及当日累计操作单独计算。 |
| 数据依赖 | 原始价、账户库存、费用、网格状态、订单事件 |
| 模块责任 | `execution` |
| 验收 | 震荡样例可完成往返；单边上/下行情不无限追加资金；每层订单可追踪且不会重复挂单。 |

#### FU-F02 · 账户/订单/成交完整接口 · P0 / integrate

**公开事实：** 交易API覆盖账户、资金、持仓、下撤改单与订单/成交回调。 [官方来源 FU02](https://openapi.futunn.com/futu-api-doc/trade/overview.html)。

**设计推断：** 定义BrokerAdapter最小接口及能力矩阵；订单意图、券商订单和成交分层存储，真实成交驱动账本。

| 实施项 | 明确要求 |
|---|---|
| A股改造 | 账户及证券的真实交易权限逐项核实；不因支持行情就推断能交易同市场。 |
| 数据依赖 | 账户/持仓、订单ID、成交ID、费用、能力矩阵 |
| 模块责任 | `execution` |
| 验收 | 部分成交、撤单、拒单及乱序回报可正确更新；重复回调不重复记账。 |

### 雪球

#### XQ-F02 · 公开策略证据与计算假设 · P0 / integrate

**公开事实：** 官方FAQ说明停牌估值、分红再投入及调仓成交等特定收益计算口径。 [官方来源 XQ01](https://xueqiu.com/p/faq)。

**设计推断：** 任何外部策略先登记数据口径与可复现性；截图/公开组合收益不能直接进入冠军策略排行。

| 实施项 | 明确要求 |
|---|---|
| A股改造 | 重算包含成本、实际可卖、涨跌停及历史成分；缺失依据会标为待补证据。 |
| 数据依赖 | 来源链接、收益口径、策略描述、交易/权重历史 |
| 模块责任 | `research` |
| 验收 | 每个候选可显示证据等级、缺失项和复现差异；未复现者不被标成已验证策略。 |
| 证据边界 | `official_search_excerpt_no_readable_body`；见来源表和覆盖不足。 |

### 全部能力索引

| 平台/ID | 能力 | 阶段 | 模块 | 官方证据 |
|---|---|---|---|---|
| tradingview / TV-F01 | 多周期图表与同步分组 | P0 / integrate | market | [TV01](https://www.tradingview.com/support/solutions/43000629992-how-to-sync-the-charts-of-my-layout/) |
| tradingview / TV-F02 | 可保存的研究工作区 | P1 / integrate | market | [TV02](https://www.tradingview.com/support/solutions/43000746975-tradingview-layouts-a-quick-guide/) |
| tradingview / TV-F03 | 自定义指标选股与排序 | P0 / integrate | decision | [TV03](https://www.tradingview.com/support/solutions/43000742436-tradingview-pine-screener-key-features-and-requirements/) |
| tradingview / TV-F04 | 可解释的策略回测报告 | P0 / integrate | research | [TV04](https://www.tradingview.com/pine-script-docs/concepts/strategies/) |
| tradingview / TV-F05 | 单股策略与组合策略边界 | P0 / integrate | decision | [TV05](https://www.tradingview.com/pine-script-docs/faq/strategies) |
| tradingview / TV-F06 | 可恢复的历史交易回放 | P0 / integrate | research | [TV06](https://www.tradingview.com/support/solutions/43000691889-learn-to-trade-on-historical-data/) |
| tradingview / TV-F07 | 图表操作计划与订单标记 | P1 / integrate | execution | [TV06](https://www.tradingview.com/support/solutions/43000691889-learn-to-trade-on-historical-data/) |
| tradingview / TV-F08 | 指标、事件与成交警报 | P0 / integrate | market | [TV07](https://tradingview.com/pine-script-docs/faq/alerts/) |
| tradingview / TV-F09 | Webhook外部信号入口 | P1 / integrate | data | [TV08](https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/) |
| tradingview / TV-F10 | 持仓导入与收益归因 | P0 / integrate | execution | [TV09](https://www.tradingview.com/support/solutions/43000760937-tradingview-portfolios-track-your-assets-know-your-trades/) |
| tradingview / TV-F11 | 持仓关联新闻与事件日历 | P0 / integrate | intelligence | [TV09](https://www.tradingview.com/support/solutions/43000760937-tradingview-portfolios-track-your-assets-know-your-trades/) |
| tradingview / TV-F12 | 自选分组与增强列表 | P0 / integrate | market | [TV10](https://www.tradingview.com/support/solutions/43000745825-mastering-the-tradingview-watchlists/) |
| tradingview / TV-F13 | 可组合研究看板 | P0 / integrate | market | [TV11](https://www.tradingview.com/support/solutions/43000746464-getting-started-with-supercharts/) |
| trendspider / TS-F01 | 跨周期指标与结构叠加 | P1 / integrate | market | [TS01](https://help.trendspider.com/kb/interface/interface-overview) |
| trendspider / TS-F02 | 自动形态与关键位候选 | P2 / research | research | [TS01](https://help.trendspider.com/kb/interface/interface-overview) |
| trendspider / TS-F03 | 选股、测试、预警共用可视规则 | P0 / integrate | decision | [TS01](https://help.trendspider.com/kb/interface/interface-overview) |
| trendspider / TS-F04 | 动态扫描与智能自选 | P1 / integrate | decision | [TS01](https://help.trendspider.com/kb/interface/interface-overview) |
| trendspider / TS-F05 | 多因素动态警报 | P1 / integrate | market | [TS02](https://trendspider.com/product/) |
| trendspider / TS-F06 | AI研究助手与工具行动 | P1 / integrate | assistant | [TS02](https://trendspider.com/product/) |
| quantconnect / QC-F01 | 分角色策略模块协议 | P0 / integrate | decision | [QC01](https://www.quantconnect.com/docs/v2/writing-algorithms/algorithm-framework/overview) |
| quantconnect / QC-F02 | 多Alpha统一合成 | P0 / integrate | decision | [QC02](https://www.quantconnect.com/docs/v2/writing-algorithms/algorithm-framework/alpha/key-concepts) |
| quantconnect / QC-F03 | 事件时间推进与数据切片 | P0 / integrate | data | [QC03](https://www.quantconnect.com/docs/v2/writing-algorithms/key-concepts/algorithm-engine) |
| quantconnect / QC-F04 | 回测与在线逻辑复用 | P0 / integrate | research | [QC03](https://www.quantconnect.com/docs/v2/writing-algorithms/key-concepts/algorithm-engine) |
| quantconnect / QC-F05 | 实盘重启与状态恢复 | P0 / integrate | execution | [QC04](https://www.quantconnect.com/docs/v2/writing-algorithms/live-trading/key-concepts) |
| quantconnect / QC-F06 | 策略框架与状态策略共存 | P0 / integrate | decision | [QC01](https://www.quantconnect.com/docs/v2/writing-algorithms/algorithm-framework/overview) |
| ibkr / IB-F01 | 行情、研究、账户与订单同屏联动 | P0 / integrate | market | [IB01](https://www.ibkrguides.com/traderworkstation/mosaic-layout.htm) |
| ibkr / IB-F02 | 条件订单与组合订单 | P1 / integrate | execution | [IB03](https://www.interactivebrokers.com/docs/tws-api/protobuf/order-condition) |
| ibkr / IB-F03 | 组合情景分析工作台 | P1 / integrate | decision | [IB02](https://www.ibkrguides.com/traderworkstation/open-risk-navigator.htm) |
| ibkr / IB-F04 | 面板联动方向与工作区分组 | P1 / integrate | market | [IB04](https://ibkrguides.com/traderworkstation/color-grouping-linked-panels.htm) |
| tdx / TD-F01 | 有类型的公式角色 | P0 / integrate | decision | [TD01](https://help.tdx.com.cn/gspt/) |
| tdx / TD-F02 | 盘中条件选股与预警 | P1 / integrate | market | [TD02](https://help.tdx.com.cn/gspt/docs/markdown/tdxgs-gsxt/tdxgs-1d1kngjqe0gck.html) |
| tdx / TD-F03 | 大事热点与行情回顾 | P1 / integrate | intelligence | [TD03](https://www.tdx.com.cn/article/soft_7_66.html) |
| tdx / TD-F04 | 产业链研究视图 | P1 / integrate | intelligence | [TD03](https://www.tdx.com.cn/article/soft_7_66.html) |
| tdx / TD-F05 | 组合策略统一评测 | P0 / integrate | research | [TD03](https://www.tdx.com.cn/article/soft_7_66.html) |
| tdx / TD-F06 | 行情订阅与策略消息接口 | P0 / integrate | data | [TD04](https://help.tdx.com.cn/quant/docs/markdown/ctx.stock.md/) |
| ifind / IF-F01 | 统一数据字段与多频查询 | P0 / integrate | data | [IF01](https://quantapi.5ifind.cn/gwstatic/static/ds_web/quantapi-web/) |
| ifind / IF-F02 | 问财式自然语言研究入口 | P1 / integrate | assistant | [IF01](https://quantapi.5ifind.cn/gwstatic/static/ds_web/quantapi-web/) |
| ifind / IF-F03 | 组合动态与净值分析 | P1 / integrate | execution | [IF01](https://quantapi.5ifind.cn/gwstatic/static/ds_web/quantapi-web/) |
| ifind / IF-F04 | 数据能力与策略执行解耦 | P0 / integrate | data | [IF02](https://quantapi.10jqka.com.cn/gwstatic/static/ds_web/quantapi-web/help-center/faq.html) |
| choice / CH-F01 | 截面、序列、宏观及订阅接口统一 | P0 / integrate | data | [CH01](https://quantapi.eastmoney.com/Manual/CSharp?from=client) |
| choice / CH-F02 | 证券与行业资讯查询 | P0 / integrate | intelligence | [CH02](https://quantapi.eastmoney.com/Upload/EMQuantAPI_Csharp.html?_=639277105644042728) |
| choice / CH-F03 | 集中交易日历与日期运算 | P0 / integrate | data | [CH03](https://quantapi.eastmoney.com/Upload/EMQuantAPI_Python.html?_=637077181105957032) |
| choice / CH-F04 | 结构化条件选股表达式 | P1 / integrate | decision | [CH04](https://quantapi.eastmoney.com/Upload/EMQuantAPI_Java.pdf?_=637592694454980342) |
| choice / CH-F05 | 数据浏览器与研究模板 | P1 / integrate | market | [CH05](https://choice.eastmoney.com/School/Help) |
| futu / FU-F01 | 可视化网格策略模板 | P1 / integrate | execution | [FU01](https://www.futunn.com/learn/grid-trading-strategy) |
| futu / FU-F02 | 账户/订单/成交完整接口 | P0 / integrate | execution | [FU02](https://openapi.futunn.com/futu-api-doc/trade/overview.html) |
| futu / FU-F03 | 行情订阅、盘口与逐笔回调 | P0 / integrate | data | [FU03](https://openapi.futunn.com/futu-api-doc/quote/overview.html) |
| futu / FU-F04 | 行情订阅额度与生命周期 | P1 / integrate | data | [FU04](https://openapi.futunn.com/futu-api-doc/qa/quote.html) |
| futu / FU-F05 | K线复权与时区显式参数 | P0 / integrate | data | [FU05](https://openapi.futunn.com/futu-api-doc/quote/get-kl.html) |
| wind / WI-F01 | 数据入库、API及组件多层交付 | P0 / integrate | data | [WI01](https://www.wind.com.cn/portal/zh/WDS/index.html) |
| wind / WI-F02 | 宏观、跨资产及另类数据研究 | P1 / integrate | intelligence | [WI01](https://www.wind.com.cn/portal/zh/WDS/index.html) |
| wind / WI-F03 | 财务与新闻的结构化研究接口 | P1 / integrate | intelligence | [WI02](https://www.wind.com.cn/portal/zh/ClientApi/index.html) |
| wind / WI-F04 | 专业研究数据核对工具 | P0 / integrate | data | [WI01](https://www.wind.com.cn/portal/zh/WDS/index.html) |
| xueqiu / XQ-F01 | 研究组合与调仓时间线 | P1 / integrate | research | [XQ01](https://xueqiu.com/p/faq) |
| xueqiu / XQ-F02 | 公开策略证据与计算假设 | P0 / integrate | research | [XQ01](https://xueqiu.com/p/faq) |
| xueqiu / XQ-F03 | 标的、作者与讨论关联 | P2 / research | intelligence | [XQ02](https://www.xueqiu.com/about/faq) |

## 5. 共用契约建议（原创设计，不是平台内部架构描述）

下面七个契约足以把本研究中的大部分能力连接起来。它们不要求七套微服务；可以在同一模块化应用内运行，并由明确的接口和事件边界隔离。

| 契约 | 关键内容 | 使用方 |
|---|---|---|
| InstrumentContext | 标准证券ID、市场、交易时区、会话、价格模式、group_id | 图表、资讯、扫描、订单面板 |
| SnapshotContext | as_of、available_at、数据版本、calendar_version、来源与质量 | 全部研究和决策模块 |
| FeatureDefinition / RuleSpec | 输入字段、输出类型、周期、参数、完结语义、规则树、版本 | 图表、选股、预警、回测 |
| StrategySpec | role、预测期限、依赖、状态模型、参数、适用宇宙、验证结果 | 策略库、多Alpha、操作策略 |
| EventRecord | 原文来源、发布/观察时间、事件类型、实体与暴露关系、修订 | 资讯、模型分析、日历、计划更新 |
| TradePlan / OrderIntent | 触发条件、目标/增量仓位、可卖库存依赖、有效期、策略归属 | 个股页、持仓、执行、复盘 |
| LedgerEvent / ExperimentRun | 真实/模拟类型、费用与企业行动、输入快照、运行和代码版本 | 账户、策略评测、收益归因 |

### 5.1 数据不堵塞的具体落点

DataHub按证券、字段、周期和权限合并上游订阅；消费者以自己的水位读取事件。图表慢渲染、模型长推理、回测扫描都不得阻塞行情和消息接收。价格更新主要失效技术特征，财报更新失效对应财务特征，事件更新沿实体/产业暴露失效计划。高频路径复用已计算特征，模型分析走异步任务，生成的新计划携带输入快照和替代前一版本的原因。这个设计借鉴多平台公开能力的组合需求，未声称任何平台使用相同内部实现。

### 5.2 不应照搬的边界

- 不把Pine单图策略当成多资产组合内核。Pine策略FAQ给出单标的与交易面板下单边界；Pulse需要自己的组合及执行契约。
- 不把默认OHLC撮合或研究组合调仓价当作真实可成交结果。平台自己公开了各自假设，Pulse应使用统一可检查的模拟语义。
- 不让自然语言选股成为只能在聊天窗口重现的功能。应生成独立可版本化的查询/策略对象。
- 不自动继承网格模板的盈利暗示。模板负责表达操作状态，是否有收益增量由独立实验决定。
- 不把所有海外终端专业模块都列为主板股票首期必需。复杂衍生品定价和高频执行留在明确扩展点中。

## 6. 覆盖不足与后续核验

| 范围 | 已知边界 | 下一步 |
|---|---|---|
| 2026与最新 | 搜索均以365日新鲜度检索并在2026-11-10核验；部分页面无日期，通达信V7.66为2024发布，IBKR布局文档2025更新。 | 发布产品规格时复核关键接口和产品能力，不能把本研究说成覆盖2026所有版本。 |
| 没有账户实测 | 本轮是公开文档能力研究，未登录购买产品、未实际跑券商/数据接口，未测延迟、覆盖率、限制或费用。 | 各适配器进入实现时，以已获得凭证跑契约样例和数据样本核对。 |
| 动态页及索引证据 | 通达信量化接口正文超时；Wind ClientAPI与雪球组合FAQ正文读取有限；Choice部分帮助依赖动态页面。均保留官方搜索索引证据而未宣称完成正文深读。 | 这些源优先用官方导出手册、账号内API说明或可访问公开静态版本补证；当前设计建议不依赖未见的具体参数。 |
| 同花顺消费端 | 本轮主要核验iFinD/数据接口。消费端条件单、同花顺做T工具等没有足够官方细文档证据，未填充为已验证能力。 | 如要复制某具体消费端交互，补充其官方版本说明与实机操作记录。 |
| 平台执行权限 | 富途/IBKR存在某API或订单类型，并不证明用户A股账户具备对应执行权限；本报告不作这种推断。 | BrokerCapability按市场、账户、证券、订单类型实测登记。 |
| 图表产品与可嵌入库 | TradingView官网功能、Pine运行环境、Advanced Charts、Trading Platform及Lightweight Charts不是同一产品授权/能力集合。 | 图表选型独立验证嵌入库及授权条款；本研究只继承交互思想，不承诺一引入图表库就获得Pine/策略测试器。 |
| 策略优越性 | 产品有某功能不能证明其提高收益；自动形态、AI助手、网格及社会观点需独立评估。 | 加入统一实验、消融和样本外评测；功能实施优先级与投资效果证据分开记录。 |
| 私有实现 | 没有分析私有源码、内部消息架构或专有算法。本文的事件总线、契约和状态机均为Pulse设计推断。 | 保持文档事实/实现建议双栏；开放源码采用另行检查许可证和接口。 |
| 未纳入部分衍生品/专业功能 | 没有将期权链、复杂希腊值、债券定价、高频做市等扩为首期主板股票系统必需功能。 | 保留扩展契约；只有新资产范围明确后再设计相应账本与执行语义。 |

## 7. 查询日志

### Q1 · system1 · recency=365

- `site.tradingview.com/support Pine Screener alerts webhooks Bar Replay portfolio 2026`
- `site.trendspider.com learning automated analysis strategy tester multi timeframe alerts scanner`
- `site.quantconnect.com/docs/v2 research backtesting live trading framework universe portfolio execution`
- `site.interactivebrokers.com TWS Mosaic portfolio risk navigator API conditional orders`

找到图表、回放、脚本、策略框架和执行主文档。

### Q2 · system1 · recency=365

- `site.tdx.com.cn 通达信 公式 选股 预警 功能`
- `site.10jqka.com.cn iFinD 数据接口 量化 选股 策略 官方`
- `site.choice.eastmoney.com Choice 金融终端 条件选股 量化 接口 官方`
- `site.futunn.com 网格交易 条件单 量化 回测 做T 官方`

取得国内公式、选股、网格与数据接口证据；未将第三方镜像和券商教程混作平台官方证据。

### Q3 · system1 · recency=365

- `site.tradingview.com/support "portfolio" "watchlist" "replay"`
- `site.ifind.10jqka.com.cn 同花顺 iFinD 数据接口 功能 股票 量化`
- `site.wind.com.cn 金融终端 量化 API 数据浏览器 Excel 产业链`
- `site.xueqiu.com/help 组合 自选 新闻 财报`

补齐组合、看板、宏观/财务/资讯、研究社区；雪球部分正文动态加载。

### Q4 · system1 · recency=365

- `site.tradingview.com/pine-script-docs/concepts/strategies "broker emulator" "Bar Magnifier"`
- `site.tradingview.com/support "How to configure webhook alerts"`
- `site.quantconnect.com/docs/v2/writing-algorithms/algorithm-framework "Alpha" "Portfolio Construction" "Execution"`
- `site.open.futunn.com/futu-api-doc "订阅" "回调" "解锁交易"`

核对模拟成交假设、多Alpha和Webhook限制；从旧Pine版本切换到当前无版本路径文档。

### Q5 · system1 · recency=365

- `site.tradingview.com/support synchronize charts layout symbol interval crosshair drawings`
- `site.help.trendspider.com "Multi-Timeframe Analysis" "Smart Watchlist"`
- `site.openapi.futunn.com/futu-api-doc/quote "订阅" "实时K线"`
- `site.ibkrguides.com/traderworkstation Mosaic linked windows watchlist order entry`

确认同步分组、账户工作台和行情细接口；TrendSpider该搜索无有效结果，改在已找到官方帮助页内检索MTFA。

### Q6 · system1 · recency=365

- `site.quantapi.eastmoney.com/Manual/CSharp "新闻" "选股" "交易日期"`
- `site.quantapi.5ifind.cn/gwstatic/static/ds_web/quantapi-web/ "问财" "组合" "新闻"`
- `site.wind.com.cn/portal/zh/ClientApi/index.html "新闻" "高频" "组合"`
- `site.xueqiu.com/p/faq "分红" "停牌" "调仓"`

找到Choice具体资讯、日历及条件选股手册；Wind API正文不可读，使用官方产品索引及WDS原文，记录边界。

### D1 · open/find

- `TradingView 当前策略文档 Broker emulator、Pine Screener 500、TrendSpider MTFA/Visual Scripts`
- `QuantConnect Framework Overview、Alpha Key Concepts`
- `iFinD 接口首页/FAQ、通达信公式分类、Choice帮助及API目录、Wind API、雪球组合FAQ`

深读关键行为；通达信量化正文获取超时，Wind ClientAPI/雪球组合正文缺可读行，Choice部分动态正文仅目录；相应来源在记录内标明访问状态。

## 8. 官方来源目录

以下source_ref供主代理在自己的检索上下文打开后引用；最终文档读者可直接使用官方链接。没有第三方评测、平台排名或未经验证的私有实现来源。

| ID | 官方文档 | 读取方式/边界 | 日期说明 | source_ref |
|---|---|---|---|---|
| TV01 | [多图同步](https://www.tradingview.com/support/solutions/43000629992-how-to-sync-the-charts-of-my-layout/) | official_document_read | 未提供明确发布日期；核验日期不代表功能发布日 | `turn55search0` |
| TV02 | [布局保存、事件、图形同步](https://www.tradingview.com/support/solutions/43000746975-tradingview-layouts-a-quick-guide/) | official_document_read | 未提供明确发布日期；核验日期不代表功能发布日 | `turn55search1` |
| TV03 | [Pine Screener 功能及限制](https://www.tradingview.com/support/solutions/43000742436-tradingview-pine-screener-key-features-and-requirements/) | official_document_read | 未提供明确发布日期；核验日期不代表功能发布日 | `turn39view0` |
| TV04 | [Pine v6 策略与模拟成交](https://www.tradingview.com/pine-script-docs/concepts/strategies/) | official_document_read | 未提供明确发布日期；核验日期不代表功能发布日 | `turn51view1` |
| TV05 | [Pine 策略 FAQ](https://www.tradingview.com/pine-script-docs/faq/strategies) | official_document_read | 未提供明确发布日期；核验日期不代表功能发布日 | `turn35search1` |
| TV06 | [历史回放交易](https://www.tradingview.com/support/solutions/43000691889-learn-to-trade-on-historical-data/) | official_document_read | 未提供明确发布日期；核验日期不代表功能发布日 | `turn39view2` |
| TV07 | [Pine 警报 FAQ](https://tradingview.com/pine-script-docs/faq/alerts/) | official_document_read | 未提供明确发布日期；核验日期不代表功能发布日 | `turn29search1` |
| TV08 | [Webhook 警报](https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/) | official_document_read | 未提供明确发布日期；核验日期不代表功能发布日 | `turn39view4` |
| TV09 | [投资组合](https://www.tradingview.com/support/solutions/43000760937-tradingview-portfolios-track-your-assets-know-your-trades/) | official_document_read | 未提供明确发布日期；核验日期不代表功能发布日 | `turn39view3` |
| TV10 | [自选列表](https://www.tradingview.com/support/solutions/43000745825-mastering-the-tradingview-watchlists/) | official_document_read | 未提供明确发布日期；核验日期不代表功能发布日 | `turn32search1` |
| TV11 | [Supercharts 工作区](https://www.tradingview.com/support/solutions/43000746464-getting-started-with-supercharts/) | official_document_read | 未提供明确发布日期；核验日期不代表功能发布日 | `turn32search3` |
| TS01 | [界面、MTFA、规则共用与扫描](https://help.trendspider.com/kb/interface/interface-overview) | official_document_read | 未提供明确发布日期；核验日期不代表功能发布日 | `turn58view2` |
| TS02 | [产品功能](https://trendspider.com/product/) | official_product_claim | 未提供明确发布日期；核验日期不代表功能发布日 | `turn29search2` |
| QC01 | [Algorithm Framework](https://www.quantconnect.com/docs/v2/writing-algorithms/algorithm-framework/overview) | official_document_read | 未提供明确发布日期；核验日期不代表功能发布日 | `turn58view0` |
| QC02 | [多 Alpha 与 Insight](https://www.quantconnect.com/docs/v2/writing-algorithms/algorithm-framework/alpha/key-concepts) | official_document_read | 未提供明确发布日期；核验日期不代表功能发布日 | `turn58view4` |
| QC03 | [算法引擎](https://www.quantconnect.com/docs/v2/writing-algorithms/key-concepts/algorithm-engine) | official_document_read | 未提供明确发布日期；核验日期不代表功能发布日 | `turn29search5` |
| QC04 | [实盘关键概念](https://www.quantconnect.com/docs/v2/writing-algorithms/live-trading/key-concepts) | official_document_read | 未提供明确发布日期；核验日期不代表功能发布日 | `turn29search4` |
| IB01 | [Mosaic 布局](https://www.ibkrguides.com/traderworkstation/mosaic-layout.htm) | official_document_read | 文档标注更新于 2025-10-08；2026-11-10 核验 | `turn55search10` |
| IB02 | [Risk Navigator](https://www.ibkrguides.com/traderworkstation/open-risk-navigator.htm) | official_document_read | 未提供明确发布日期；核验日期不代表功能发布日 | `turn29search7` |
| IB03 | [API 条件订单类型](https://www.interactivebrokers.com/docs/tws-api/protobuf/order-condition) | official_document_read | 未提供明确发布日期；核验日期不代表功能发布日 | `turn29search9` |
| IB04 | [颜色联动面板](https://ibkrguides.com/traderworkstation/color-grouping-linked-panels.htm) | official_document_read | 未提供明确发布日期；核验日期不代表功能发布日 | `turn55search6` |
| TD01 | [公式分类](https://help.tdx.com.cn/gspt/) | official_document_read | 未提供明确发布日期；核验日期不代表功能发布日 | `turn69view1` |
| TD02 | [条件选股](https://help.tdx.com.cn/gspt/docs/markdown/tdxgs-gsxt/tdxgs-1d1kngjqe0gck.html) | official_document_read | 未提供明确发布日期；核验日期不代表功能发布日 | `turn30search0` |
| TD03 | [V7.66 产品更新](https://www.tdx.com.cn/article/soft_7_66.html) | official_document_read | 发布于 2024-11-04；作为已存在能力证据，不称为 2026 最新版本 | `turn30search3` |
| TD04 | [量化通用接口](https://help.tdx.com.cn/quant/docs/markdown/ctx.stock.md/) | official_search_excerpt_body_timeout | 未提供明确发布日期；核验日期不代表功能发布日 | `turn30search4` |
| IF01 | [iFinD 数据接口](https://quantapi.5ifind.cn/gwstatic/static/ds_web/quantapi-web/) | official_document_read | 未提供明确发布日期；核验日期不代表功能发布日 | `turn69view0` |
| IF02 | [数据接口与数据库差别](https://quantapi.10jqka.com.cn/gwstatic/static/ds_web/quantapi-web/help-center/faq.html) | official_document_read | 未提供明确发布日期；核验日期不代表功能发布日 | `turn62view1` |
| CH01 | [数据量化接口目录](https://quantapi.eastmoney.com/Manual/CSharp?from=client) | official_search_excerpt_dynamic_body_partial | 未提供明确发布日期；核验日期不代表功能发布日 | `turn30search5` |
| CH02 | [资讯函数](https://quantapi.eastmoney.com/Upload/EMQuantAPI_Csharp.html?_=639277105644042728) | official_document_read | 未提供明确发布日期；核验日期不代表功能发布日 | `turn64search0` |
| CH03 | [交易日历](https://quantapi.eastmoney.com/Upload/EMQuantAPI_Python.html?_=637077181105957032) | official_document_read | 未提供明确发布日期；核验日期不代表功能发布日 | `turn64search1` |
| CH04 | [条件选股接口](https://quantapi.eastmoney.com/Upload/EMQuantAPI_Java.pdf?_=637592694454980342) | official_search_excerpt_pdf | 搜索标注约四个月前；未独立确认 PDF 具体发布日期 | `turn64search8` |
| CH05 | [数据浏览器](https://choice.eastmoney.com/School/Help) | official_search_excerpt_dynamic_body_partial | 未提供明确发布日期；核验日期不代表功能发布日 | `turn30search6` |
| FU01 | [网格策略教程](https://www.futunn.com/learn/grid-trading-strategy) | official_product_tutorial | 未提供明确发布日期；核验日期不代表功能发布日 | `turn30search8` |
| FU02 | [交易 API 总览](https://openapi.futunn.com/futu-api-doc/trade/overview.html) | official_document_read | 未提供明确发布日期；核验日期不代表功能发布日 | `turn35search6` |
| FU03 | [行情 API 总览](https://openapi.futunn.com/futu-api-doc/quote/overview.html) | official_document_read | 未提供明确发布日期；核验日期不代表功能发布日 | `turn55search9` |
| FU04 | [行情订阅与异步回调](https://openapi.futunn.com/futu-api-doc/qa/quote.html) | official_document_read | 未提供明确发布日期；核验日期不代表功能发布日 | `turn55search8` |
| FU05 | [实时 K 线参数](https://openapi.futunn.com/futu-api-doc/quote/get-kl.html) | official_document_read | 未提供明确发布日期；核验日期不代表功能发布日 | `turn55search7` |
| WI01 | [数据服务](https://www.wind.com.cn/portal/zh/WDS/index.html) | official_document_read | 未提供明确发布日期；核验日期不代表功能发布日 | `turn64search3` |
| WI02 | [ClientAPI](https://www.wind.com.cn/portal/zh/ClientApi/index.html) | official_search_excerpt_no_readable_body | 未提供明确发布日期；核验日期不代表功能发布日 | `turn32search8` |
| WI03 | [金融终端](https://www.wind.com.cn/portal/zh/WFT/index.html) | official_product_claim | 未提供明确发布日期；核验日期不代表功能发布日 | `turn32search9` |
| XQ01 | [雪球组合 FAQ](https://xueqiu.com/p/faq) | official_search_excerpt_no_readable_body | 未提供明确发布日期；核验日期不代表功能发布日 | `turn64search4` |
| XQ02 | [官方功能 FAQ](https://www.xueqiu.com/about/faq) | official_document_read | 未提供明确发布日期；核验日期不代表功能发布日 | `turn69view2` |

## 9. 机器可读交付

同目录 `platforms.json` 包含全部来源、逐项记录、责任域、验收条件、查询日志、覆盖不足与Top12。可以直接转为任务卡，但任务卡中的事实与推断字段应保持分离。

