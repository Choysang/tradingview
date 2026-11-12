# 补充材料的数据库、契约与统一账本审查

审查日：2026-11-11。对象为用户附件《A-Share OmniPulse 2026 … v4.0》，与已交付 Pulse v3 实际文件比较。结论为**保留 v3 的事实模型与账本，吸收附件能落实为边界和验收的细节**；附件的版本号更高、形容词更强，不构成替换已有契约的理由。本报告没有修改既有交付包。

## 1. 值得吸收的真正增量

### C-ADD-01：将 ASOF 意图落实成可执行的输入对齐政策

附件把 `join_asof`、缺值补齐、换手率推导明确写到一起，这有助于把“供应商字段统一”推进到“异步数据何时可以共同参与判断”。v3 已有 `ops.reconciliation_result.alignment_policy_version`、`tolerance_policy_version`，并在数据库 §3 和公共特征协议 §1–3 定义 PIT、缺失与血缘，但没有独立的 ASOF 参考实现。因此采纳**具体化对齐政策**，不采纳“所有时序断点前向填充”。

政策须先固定证券、来源、端点、源 schema、mapping、统计方法、单位、币种、会话及复权口径，再分别处理以下情况。

- **状态值**：只允许已登记状态字段向后查找，保留原始 `event_time` 与 `available_at`；每次使用检查 `max_age`。查询命中旧值不生成一条“现在新观察到”的行情。
- **流量和区间极值**：成交量、成交额、OHLC 不用上一根值补成新 bar；只能匹配确切期间的真实数据或输出缺口。
- **修订**：对每个 `natural_key` 先按截止时点筛除尚不可得的修订，再选当时可得的最高 `observation_revision`。最新有效修订为 invalid/missing 时，默认明确输出该状态，不能悄悄选更旧的好值。
- **分母**：历史换手率使用当时已知、在该时点生效的流通股本。不能用今天的“最新股本”覆盖历史；自由流通股本也不能替代流通股本。
- **派生**：VWAP 要求成交额和成交量为相同证券、来源统计口径、窗口、会话、币种和复权口径，分母非零；输出数值携带两个输入引用和计算版本。派生 `available_at` 不早于输入与本次计算完成时刻。

官方 Polars 文档证明 ASOF 具有 backward、forward、nearest 三种策略，要求排序并允许 tolerance；它只提供连接算子，不自动知道金融 PIT 与来源修订的语义。[CR01] 因此上述边界是本项目必须实现的政策，不能以 `join_asof` 四个字替代。

**落点**：内部 `InputAlignmentPolicy` 注册到已有 `core.contract_version`；其版本进入 `ops.reconciliation_result.alignment_policy_version` 和特征定义/参数及依赖摘要。结果存入既有 `market.feature_result`/不可变产物，不增设平行主表或 HTTP 口径。参考代码在 `research_v4/reference_alignment/`，保持为有限、合成数据、纯 Python 行为参考。

### C-ADD-02：合同跨语言往返验收

附件同时列 Protobuf、Pydantic、Zod、Arrow，有提醒开发栈协同的价值，但四套手写 schema 会重新制造割裂。v3 已有唯一 JSON Schema/OpenAPI 及生成客户端的原则，建议补充“合同往返样例集”，检验同一十进制字符串、UTC 时刻、显式 null、缺失键、枚举、整数边界经 Python→JSON→TypeScript→批量列式读取后语义一致。新增 Protobuf/Flight 只有明确消费方和实测收益时才安排；所有投影须追溯唯一合同及生成器版本。

**理由**：结构可编译不等于单位、空值、精度不变。附件示例金额使用 float，恰好表明名字统一不能保证契约统一。最有价值的工作是建立差异可定位的 fixture，而不是再添加一种序列化格式。

**落点**：`contracts` 和 `W01/W10` 的验收说明；不改变现有公开字段。验收反例含超出 JS safe integer 的股数、金额科学计数法、日期冒充时刻、毫秒/微秒混用、百分数/fraction 混用，以及 nullable 与 absent 的差异。

### C-ADD-03：技术版本采用表与可复算性能预算

附件引导检查当前版本有价值。本轮官方资料验证 DuckDB 2.0.0 已于 2026-10-21 发布，Polars 2.1 已于 10-27 发布；不能凭旧记忆把它们判成虚构。[CR02][CR03] 可将二者加入**候选兼容矩阵**，验证 DuckLake 扩展、Arrow decimal/timestamp、序列化和部署镜像后锁版本。Pydantic 官方 10-30 说 V3 尚在开发且没有发布日期，不能写作已稳定可用的默认依赖。[CR04] PostgreSQL 19 当前为 RC1，GA 计划 11-19；保留 v3 PG18 兼容基线合理，PG17不是因附件较新就需要降级的目标。[CR05]

v3 数据库 §2 已要求冷/热缓存、并发、P50/P95/P99、读写与故障一致性实测。可将本次审查落实为版本化 `benchmark_profile`：固定数据行数/列/压缩字节、硬件、进程数、缓存条件、查询内容、返回行数、RSS 峰值、临时溢写与对象 IO、原文到规范及规范到页面两段延迟。**不新增未执行成绩**。

**理由**：Polars 官方小内存预算示例明确会溢写到磁盘；低内存不是“全量数据均在 500 MB 内”，更不是“十年全市场回测已在数百毫秒完成”。[CR03]

## 2. 需要保留 v3、拒绝回退的条款

| 提议编号 | 附件说法 | 分类 | 保留/修正的充分理由 | v3 实际锚点与影响 |
|---|---|---|---|---|
| C-KEEP-01 | `symbol_id=SH.600519` 为全域主键 | 不采纳替换 | 可读代码适合作别名；作为主键会把交易所代码/来源写法/证券实体混为一体。稳定实体身份与有时点别名能处理更名、映射修订、多市场类型。更名没有理由重写历史所有外键。 | `core.instrument.instrument_id` 与 `core.instrument_alias`；保留 UUID，对外展示代码通过别名解析。 |
| C-KEEP-02 | 每表两个 INT64 时刻即可彻底杜绝未来函数 | 必须修正 | 字段存在不等于查询使用它们；原文到达、规范完成、模型完成、修订可得不同。附件正文写微秒、DDL 注释写毫秒，内部已存在歧义。 | 数据库 §3；`ingest.raw_object`、`ingest.observation`；observed `available_at` 与研究 `policy_usable_at` 分离。 |
| C-KEEP-03 | 价格 DECIMAL(12,4)，金额20,2，DDL又用DOUBLE，API又用float | 不采纳替换 | 同份方案跨层互相冲突；科学计算可用声明误差的 float，但账本与规范金额不能随消费者改成二进制浮点。PG 官方把 numeric 定义为精确，double为非精确。[CR06] | v3 `Decimal` regex、`numeric(38,12)`、统一 ledger Decimal50/scale12；不因 MA 向量优化改变经济账本。 |
| C-KEEP-04 | 一张宽表放 OHLC、MACD、CYQ、封单与仲裁分数 | 不采纳替换 | 原始观测、衍生特征与预测版本生命周期不同。宽表把MACD参数/算法版本、公司行动、CYQ假设绑成固定列，容易覆盖历史，也会以默认0把未获得L2当零封单。 | `ingest.observation`→`dataset_manifest`→`market.feature_result`；可建热点读取投影，但不是第二套规范事实。 |
| C-KEEP-05 | 三源价格相差≤0.01%就采信，停滞3秒剔除 | 不采纳固定规则 | 不同时间的正常成交价不是冲突；同源转发不独立；定时快照和连续L2不可用同一心跳阈值；固定相对阈值与价格最小变动单位无必然对应。 | `ops.reconciliation_result`、来源独立性与时效分离；先同证券/时间窗/会话/原始价对齐，再按字段职责和精度比较。 |
| C-KEEP-06 | `f58` 自动乘100 等字段示例 | 暂不采纳该映射 | 附件未提供精确端点、版本、官方字段表与原始payload，无法证明 `f58` 的单位和含义。不能用字段名字或源品牌猜单位。 | `source_id+endpoint_id+source_schema_version+mapping_version`；保留v3已核实的Tushare daily/rt_k反例；新增映射须带fixture和证据。 |
| C-KEEP-07 | 所有时序缺口用 forward-fill，`is_imputed=true` 足够 | 修正后局部采纳 | 一个布尔值无法表示缺失、派生、第二来源、冲突、陈旧。填成交量、高低价会捏造不存在的交易；向后 ASOF 也不能自动解决晚到修订。 | 沿用 `FieldStatus/QualityStatus/FreshnessState`；新增内部对齐政策 C-ADD-01，保留原始时点和来源。 |
| C-KEEP-08 | 以“最新float_shares”补历史换手率 | 不采纳替换 | 当时未知的股本变更会改写历史排序；流通与自由流通股本口径不同。公式正确不能补救分母的时点错误。 | 公共特征协议 §4 的 `circulating_market_cap` 与 `shares.float` 绑定说明；分母使用有时点状态。 |
| C-KEEP-09 | 后复权价用于回测计算真实收益 | 必须限定 | 复权价格适合特定分析序列，是否包含权益、税费和现金流须定义；真实成交/费用/股息已入账再用复权价估值会重复计益。 | 执行标准 §5：实际经济账本用原始价与独立公司行动；复权数据须有版本及可得时间。 |
| C-KEEP-10 | stock_workbench_states每股唯一，一表含用户画线、账户成本与市场分 | 不采纳替换 | `UNIQUE(symbol_id)` 会使多个用户/账户相互覆盖；底仓与成本若在工作台自写就是第二账本；持久布局和实时行情也不能共享同一失效策略。 | `ui.workspace_layout(principal_id,layout_version)`、`execution.account_head`、`decision.workspace_projection`、`ActionabilitySnapshot` 分责并通过release/account引用贯通。 |
| C-KEEP-11 | 最新消息三秒内更新全部工作台值 | 不采纳无条件承诺 | 不同输入与模型的可得时间不同，重任务不能阻塞报价。只更新部分字段也会破坏快照一致性。 | 冻结 AnalysisRelease + 轻量当前quote/actionability覆盖层；阶段时延单列，UI分别标明报价时间和分析时间。 |
| C-KEEP-12 | Redis/Dragonfly、Timescale、DuckDB、NATS必须同时首发 | 不采纳替换 | 组件数量不证明容量，增加状态同步、升级和恢复接口。现有PG+Lake+worker已有明确隔离；共享缓存或在线分析库应按瓶颈实测添加。 | 数据库 §1–2；Redis可丢弃缓存，Timescale候选需独立性能与兼容收益，不取代持久事实。 |
| C-KEEP-13 | Arrow Flight/IPC保证零拷贝总线、策略全程<1ms | 不采纳承诺 | Arrow官方区分进程内 C Data Interface 的共享缓冲区与跨进程/网络 IPC；序列化、网络、数据库提交、模型和浏览器都有独立成本。[CR07] | Arrow用于批量数据通路；NATS小事件与引用；不让每个模块为了格式一致都跨进程RPC。 |
| C-KEEP-14 | GIPS国际标准FIFO复式账本 | 必须修正 | GIPS负责业绩计算与展示；FIFO是成本分配选择，复式是借贷一致性，FIX是订单状态。它们不是同一个标准，采用一个原则也不等于取得合规认证。[CR08][CR09] | 执行标准 §1、§4、§5；保留 `weighted_average_gross` 的明确项目政策，可比较其他成本法但不能悄悄替换。 |
| C-KEEP-15 | 回测、paper、真实券商使用同一个撮合算子 | 修正后采纳“同一内核” | 同一规范事件/费用/账本应共用；真实已成交事实不能再由模拟撮合拒绝。不同数据粒度不能装成有同样成交证据。 | ExecutionKernel中的MatcherPort与LedgerReducer分离；真实回报先持久化再对账。 |
| C-KEEP-16 | 万1.5最低5元、固定税费全市场统一硬编码 | 不采纳固定值 | 客户合同、渠道、交易日期、佣金是否含规费和订单聚合范围不同；每次部分成交都收最低费也会重复扣费。 | `CostProfile` 版本化；fixture5元仅合成输入，不是全市场费率。 |
| C-KEEP-17 | `ask_vol_1==0`等同确定无法买/可真实排队 | 必须限定 | 缺失L2不能填0；快照不能重建全部队列前序、撤单和逐笔成交。可建立有假设的排队模型，但不能标成真实队列。 | `execution_model_id`、data grade、scenario、统一Fill；订单状态与成交证据分离。 |
| C-KEEP-18 | 所有接口强制Protobuf/Pydantic/Zod，保证100%零歧义 | 修正后局部采纳 | 多套手工合同反而漂移。类型校验不验证资金归属、单位、时间、排序目标和账户可行动性。 | v3 schema/OpenAPI为权威；新增C-ADD-02往返契约测试，而非替换规范。 |
| C-KEEP-19 | 所有Rust/PyO3/WASM算子都应先写 | 作为可选性能路线 | 若Python/Polars已满足目标，先引入跨语言数字政策和双平台构建并无收益；最贵算子经profile再迁移最合理。 | 公共FeatureKey的implementation_digest与numeric_policy_version确保替换可追溯，golden差分后切换。 |
| C-KEEP-20 | 15分钟合同锁定、8Agent零冲突 | 不采纳时间/无冲突承诺 | 合同依赖顺序有价值；不同团队同时编辑主schema/SQL会发生真实合并冲突。分工、契约owner、独立验收可以减少冲突，不能保证消失。 | 既有W01–W10与AGENT_START_PROMPT；公共合同先落稳，其余按所有权并行；进度按验收产物，不按夸张倒计时。 |

## 3. 三个手算反例说明替换为什么不值得

### 两个时间戳不能自动保证可用性

某原文事件发生10:00，10:00:01系统看到，10:00:02保存原文，但直到10:05才解析正确并完成依赖计算。10:01的历史决策不能使用10:05才可用的特征。仅过滤 `event_time<=10:01` 与原始 `ingested_at<=10:01` 会漏掉计算完成边界。修订亦同：第二天看到的更正即使事件日期仍为昨天，也不能覆盖昨天决策看到的版本。这是具体的时间语义问题，不是给列增加 `_utc` 后缀就能解决。

### 三源价格“异议”不一定是错误

甲源09:30:00报价10.00，乙源09:30:02报价10.01；按附件相对差阈值0.0001，两者相差0.001会判成冲突，但完全可能只是正常的一分变动。相反，两个渠道都转发同一过期快照时会“二比一获胜”。因此先确认相同对象、同一观测窗和独立起源，比投票数量更重要。此例只是算术反例，没有声称某具体供应商的数据错误。

### FIFO与加权平均不应该改变同一经济事件总财富

初始现金5000，买100股×10元、再买100股×20元，卖100股×25元，暂忽略税费；期末价格25元。现金4500、库存市值2500，总净值7000，总利润2000。

- FIFO：已实现1500，剩余成本2000、未实现500。
- 加权平均：已实现1000，剩余成本1500、未实现1000。

两种拆分不同，但经济总值相同。统一内核应锁定一种展示政策、保持可追溯，不能认为换成FIFO后策略多赚500。上述为独立手算经济恒等式，不是GIPS对成本法的规定。

## 4. 不新增API/表的落地映射

| 对齐参考内容 | 现有持久位置/DTO | 不应做的事 |
|---|---|---|
| `alignment_policy_version` | `ops.reconciliation_result.alignment_policy_version`；内部policy schema登记`core.contract_version` | 不在每个API里新增各自命名的对齐开关 |
| 精确源与端点范围 | `core.source_endpoint`复合身份；`FieldQualityEntry`已含source/endpoint/mapping | 不用source品牌当单位/方法版本 |
| 观测与修订 | `ingest.observation.observation_id/natural_key/observation_revision/event_time/available_at` | 不更新旧版本或倒填available_at |
| 方法、统计窗口 | `intelligence.funding_observation.methodology_id/methodology_version/observation_window_start/end`；规范bar映射`bar_open_time/bar_close_time` | 不把逐笔、累计快照、分钟量当一类数相加 |
| 原文与字段质量 | `raw_object_id`、`field_quality`；公开`FieldQualityEntry.reason` | 不新造quality_state=unknown或is_imputed总开关 |
| 派生输出 | `market.feature_result.full_key`、结果对象/manifest、依赖摘要 | 不把VWAP加成每张表手写计算列 |
| 计算可得时间 | 产物`computed_at/available_at`，按v3 observed/reconstructed分开 | 不让历史重建重写事实的实际available_at |
| 工作台 | 固定`release_id`与既有`Workspace` DTO；实时quote/actionability独立 | 不改变已有冻结投影hash含义 |

参考里的流通股数字段仅作 `shares.float` 既有逻辑名的明确 circulating basis 绑定案例；v3 并没有一个可直接冒称已实现的“全市场流通股本生产表”。正式实现仍需注册相应规范数据合同、接源、完成PIT历史。

## 5. 一手证据登记

以下为核查到的事实，不把项目推论混为原文断言。每源摘要控制在短范围，详细接口使用原链接。

| ID | 原始来源 | 本次读到的支持内容 | 浏览引用 |
|---|---|---|---|
| CR01 | https://docs.pola.rs/api/python/stable/reference/dataframe/api/polars.DataFrame.join_asof.html | ASOF三种方向、排序、by分组和tolerance；backward是键不大于目标的最近项；它不包含金融可得时间规则。 | turn173view0 / turn177view2 |
| CR02 | https://duckdb.org/2026/10/21/announcing-duckdb-200 | 2.0.0发布日、LTS、异步IO、旧版本不能读2.0新建原生数据库文件，以及迁移需验证。 | turn169view1 / turn173view4 |
| CR03 | https://pola.rs/posts/polars-2-1-changelog/ | 2.1发布日，group-by可溢写；200MB预算示例会写磁盘，并非全数据在内存中。 | turn169view2 |
| CR04 | https://pydantic.dev/articles/pydantic-validation-next-steps | 2026-10-30文章：V3开始开发，尚无发布日期；V2.14系列可用。 | turn169view3 |
| CR05 | https://www.postgresql.org/about/news/postgresql-19-rc-1-released-3409/ | 19 RC1于10-15；计划RC2为11-12、GA为11-19。计划不等于已经发布。 | turn173view2 |
| CR06 | https://www.postgresql.org/docs/current/datatype-numeric.html | numeric/decimal精确，double precision非精确；numeric适于要求准确的金额但通常慢于浮点。 | turn169view4 |
| CR07 | https://arrow.apache.org/docs/format/Intro.html | 进程内C Data Interface共享缓冲区与跨进程/网络IPC明确区分。 | turn169view0 / turn173view3 |
| CR08 | https://www.cfainstitute.org/insights/professional-learning/refresher-readings/2026/overview-of-the-global-investment-performance-standards | GIPS用于业绩计算/展示、现金流/费用/估值与可比性；该2026课程讲的是2020版标准。 | turn173view1 / turn177view0 |
| CR09 | https://www.gipsstandards.org/standards/ | 宣称合规须遵守全部适用规定及指导、解释和问答；不是采用一个公式就算合规。 | turn169view5 |

## 6. 本轮结论边界

这是对附件及v3工件的定向审查，未重新声称435个旧文件完成逐行审计。未运行供应商生产连接、生产数据库压测、账本真实券商集成或全量回测。新增对齐参考仅证明列出的时间、修订、单位和数值边界，不证明数据源本身正确或任何策略有收益。
