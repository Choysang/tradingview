# Pulse v3 数据库、数据标准与发布一致性设计

核查日期：2026-11-11。本文是可开发的数据库专项规范。`core_schema.sql` 是 PostgreSQL 语法种子，`field_dictionary.json` 覆盖其全部字段；此轮完成语法解析，没有执行 PostgreSQL 迁移、供应商真实接入或生产吞吐测试。设计针对选股、板块榜与个股工作台，模型是分析能力，Agent 是开发协作方式。

## 1. 先确定数据职责，再确定数据库

首发小规模档采用 **单PostgreSQL业务事务 + 不可变本地Parquet与manifest + 事务outbox轮询**；DuckLake、对象存储服务及NATS作为增长后可选适配器。DuckDB/Polars 是分析执行器，Arrow 是进程间批量数据格式。ClickHouse 保留为经过实测后添加的在线分析投影，不是首发又一套主数据。数据库变多并不自动消除瓶颈；没有统一时点、单位和发布协议，速度越快越容易把错误传播到多个页面。

| 负载 | 首发存放/执行 | 原因与边界 |
|---|---|---|
| 证券身份、来源配置、字段合同、策略定义、分类、决策发布、用户布局 | PostgreSQL `pulse_app` | 唯一性、外键、版本更新、短事务；必须能在一次业务事务中提交 |
| 现金、持仓、预留、分录、成交、账户版本 | PostgreSQL `execution` 域，由统一执行专项定义 | 不允许不同回测入口自建收益与现金表；本文件不重复账本 |
| 供应商原始 JSON/HTML/PDF、评论/新闻原文、映射前记录 | 本地不可变对象（扩展档可迁对象存储）；PG保存定位、哈希、批次和血缘 | 能重新解析、核查修订、避免把大量正文放进消息 |
| 日/分钟行情、特征矩阵、历史事件、实验切片 | Parquet + 不可变成员manifest；DuckLake目录可选；DuckDB/Polars worker | 批量列式扫描，按不可变快照复算；不让批量回测扫描业务主库 |
| 榜单、个股页面、近期图表窗口 | PG 小型版本投影 + 有界内存/本地列式缓存 | 有完整输入版本、可重建，页面无需重复计算策略 |
| 模块通知、失效提示、补算任务 | PG事务outbox轮询；扩展时可接JetStream | 小事件和引用；持久真相仍在业务库与不可变对象中 |
| 全市场高并发分钟/逐笔筛选 | 达到实测条件后增加 ClickHouse | 仅在线分析投影，不代替 PG 事务或 DuckLake 研究快照 |

Arrow 的列式格式解决语言间的类型与数据传递，不负责跨库提交。DuckLake 有自身事务和快照，也不会把一个独立 PostgreSQL 业务事务自动变成跨系统原子事务。[DB06][DB07][DB13]

### 首发物理部署

默认小规模档如下，逻辑职责与物理进程分开管理。

1. `web-api`：前端/BFF与模块化API，连接共享应用服务；查询不执行重回测。
2. `app-db`：一个PostgreSQL18兼容状态库，保留金额、外键、发布和账本事务；不是一开始部署第二个目录数据库。
3. `data-dir`：内容寻址的原始对象、不可变Parquet和成员manifest；只在校验、落盘和发布后成为可读版本。
4. 摄取、分析、投影和研究worker按配额运行；可合并轻任务，研究独立进程并限制峰值与临时磁盘。
5. 更新分发首先消费同库事务outbox，保留幂等、游标、重放与反压。跨机器或消费积压经测量成为瓶颈后再加NATS。

输入材料的Windows Server2025、2核8GB作为待确认目标档；C盘可用空间和其他应用负载须现场测量。PostgreSQL官方Windows安装器测试平台列出PG18对应Server2025/2022；扩展与驱动另做兼容验收。无需把WSL或Linux当成必须前置。当前未运行生产负载，不能宣称这档硬件足够承载所有历史分钟与LLM研究。

扩展档可采用DuckLake目录、独立对象存储、NATS及额外分析worker；只有达到容量、连接、尾延迟或恢复目标触发条件后才拆资源。目录可先共用PG实例中的独立数据库/配额，不强制从第一天多实例。ClickHouse、Redis也仅添加实测有收益的可重建投影或缓存。SQLite是可选实现路径，但当前SQL和账本不能只换驱动；精确金额、FK、延迟约束、并发和恢复必须完整适配。

## 2. 容量、分区、写入与读隔离

使用公开可复算的容量公式：

`rows_per_year = instrument_count × trading_days × observations_per_instrument_day`

`stored_bytes = rows × measured_bytes_per_row × retained_revisions × replica_factor + indexes + raw_objects + WAL + temporary_spill`

以下只用于估算，假设 6,000 支证券、250 个交易日、常规连续交易 240 分钟，不代表当前证券数量或全部交易会话；集合竞价、盘后、停牌、上市退市、源缺口需由实际日历修正：

| 数据 | 行数/年 | 示例每行压缩字节假设 | 单副本数据体积估算 |
|---|---:|---:|---:|
| 日线 | 1,500,000 | 80 | 120 MB |
| 1 分钟 | 360,000,000 | 80 | 28.8 GB |
| 3 秒快照 | 7,200,000,000 | 200 | 1.44 TB |

这些体积没有包含上述附加项，且 Parquet 压缩比须用实际样本测量。分钟数据和快照差约 20 倍，不能把“股票只有几千支”误当成数据量小。历史数据复权、修订和新特征也不能每次全表复制。

**Parquet与可选DuckLake：** 先按交易月/日与稳定证券桶组织数据；文件大小由真实扫描与对象请求测试校准，避免每证券每分钟一小文件。保留原始未复权行情；复权由有版本的权益事件/因子派生。快照引用已经存在的文件与目录版本，不能为每次榜单刷新复制全历史。研究任务固定目录和快照，临时溢写到独立预算盘。

**PostgreSQL：** 身份、策略、发布包等短表先用精确 B-tree 与唯一约束，不把每张表都分区。`market.bar_projection` 是按交易月分区的**有界图表热投影**，其唯一键包含分区键，符合 PG 分区唯一约束的要求；查询必须给出日期范围，否则分区剪枝没有收益。BRIN 只辅助与物理顺序相关的范围读，不能替代单证券窗口 B-tree。过多分区会增加规划及维护开销。[DB01]

**图表投影的完整性：** 不把“热表里找到了几行”当成完整图表。窗口键包含证券、周期、会话、区间、复权、快照、可用性政策；`market.feature_result` 的 `feature_id=bars.window` 保存窗口完整结果与行数清单引用。READY 才可用热行返回；不完整则从固定 Lake 快照重建。PG 只保留被实际请求的窗口及有限版本，旧发布包仍可从 Lake 重建。不能为每个新分钟快照向 PG 重拷全市场多年历史。

**写入：** 原始对象落盘→批次成员提交→游标 CAS 推进；批量规范数据用有界批次落列式文件。小型业务写入保持短事务；大批 `COPY` 写 staging，再校验和发布引用，不能持锁等待模型推理。SQL 种子不带默认分区，缺分区应显式报错；部署任务提前创建未来月份并确认历史范围。

**读取：** 页面读取主库的 READY 投影；重型扫描走分析 worker。只读副本可以承担允许滞后的页面，但必须比较请求发布包是否可见，不能在副本缺少新包时拼接旧图表。多条 SQL 共同构建一个响应时使用同一固定 `release_id`；需要对可变元数据作一致读取时使用短 `REPEATABLE READ` 事务。PG 默认 Read Committed 的两条查询可看到不同提交结果，不能靠“同一请求”自然获得一致性。[DB02]

### 添加 ClickHouse 的明确条件

先记录真实混合负载基线，再进行相同数据、相同修订口径的候选对比。只有当下列问题持续出现，且已经解决查询范围、文件布局和无界并发问题，才提交引入决策：

- 全市场分钟筛选/聚合在目标并发下超出产品验收延迟，任务积压影响新一轮选股；
- 热工作集和交互分析并发明显超出有界 DuckDB worker 的经济容量；
- 相同费用和硬件预算下，ClickHouse 给出可重现的改善，并通过修订、回放和双读一致性测试。

引入后按主要过滤条件选排序键，例如 `(instrument_id, timeframe, session_scope, trade_date, bar_open_time)`，再对跨证券扫描另测投影。MergeTree 主键不是唯一约束；ReplacingMergeTree 后台合并并不提供立即去重。必须保留观测修订键、明确版本选择查询，不能把“最终会合并”当成页面与账本的即时正确性。[DB04][DB05]

压测必须同时报告 P50/P95/P99、数据覆盖规模、并发、冷/热缓存、磁盘和内存、查询内容、摄取延迟、故障恢复与一致性差异。若只是单线程读 CSV，不得写成生产基准。

## 3. 一套规范字段，保留每个来源的事实

### 身份、数值、时间

- SQL 与 JSON 使用 `lower_snake_case`；证券统一 `instrument_id` UUID。交易所代码是有时间范围的别名；简称、ST 状态、行业成员都保留历史版本。
- 金额和价格采用 Decimal，SQL 共同使用 numeric(38,12) 基线，与执行专项对齐。写入前由合同校验尺度与范围，禁止依赖数据库强制类型转换悄悄舍入超精度输入。API 金额/价格用十进制字符串，避免 JS/Python 二进制浮点产生不同账本。`volume_shares` 使用非负整数，JSON 合同限制在安全整数范围。原始供应商若已把金额转成浮点，则记下精度受限，不能声称重新获得丢失的小数。
- `price_raw` 是通用未复权价格语义；K 线用 `open_price_raw/high_price_raw/low_price_raw/close_price_raw`，报价用 `last_price_raw`。同时传币种与 `adjustment_mode`。`turnover_amount_cny` 单位人民币元，比例以 fraction 表示，2% 是 `0.02`。
- `event_time` 是事实发生时刻；`published_at` 是可证明的源发布时间；`first_seen_at` 是本系统首次接收；`available_at` 是规范版本完成可用的时间；`recorded_at` 是入库时刻。不能把它们混成一个 `timestamp`。
- 数据库瞬时统一 `timestamptz`/UTC；交易日使用交易所日历与时区推导。只有日期的公告记录精度为 date，不编造 00:00:00 为真实发布时间。历史实验需显式选择 observed/reconstructed 可得性政策，重建历史不能冒充真实线上观察。

### 已核实的单位反例

| 来源与精确接口 | 原字段 | 官方单位 | 规范转换 |
|---|---|---|---|
| Tushare `daily` | `vol` | 手 | ×100 → `volume_shares` |
| Tushare `daily` | `amount` | 千元 | ×1000 → `turnover_amount_cny` |
| Tushare `rt_k` | `vol` | 股 | 原值 → `volume_shares` |
| Tushare `rt_k` | `amount` | 元 | 原值 → `turnover_amount_cny` |
| AKShare `stock_zh_a_hist`，东方财富来源 | `成交量` | 手 | ×100 → `volume_shares` |
| 同接口 | `成交额` | 元 | 原值 → `turnover_amount_cny` |

Tushare 同一供应商两个接口的同名字段已经不同；因此映射主键必须包含 **source_id + endpoint_id + source_schema_version + mapping_version**。Tushare 日线 `pre_close` 文档标注为除权参考价，应进入 `exchange_reference_price_raw`，不能伪装成上一根未复权收盘价。AKShare 原始、前复权、后复权由请求参数区分，必须进入不同数据口径。[DB09][DB10][DB11]

附带 JSON 使用合成数值，验证换算逻辑，不声称已经购买权限或拉取供应商实盘。盘中累计快照与完整日线即使单位相同，也不自动成为可比较的同一对象；先对齐证券、期间、会话、终结状态和复权，才能比较。不得把连续累计成交量相加，分钟量应按同会话、同源、无缺口的累计差分或源明确区间量得到，并保留 reset/修订处理政策。

### 缺失、异常、补齐与冲突

`field_quality` 的状态是 observed / derived / missing / invalid / not_applicable / disputed。新鲜度和来源健康是独立维度。

1. 字段不存在或值缺失：记录 missing。业务不适用：记录 not_applicable。源提供了坏类型/负量/NaN：记录 invalid。三者不统一填 0。
2. 可确定的单位换算、日期解析属于规范化；显式计算指标属于 derived。大模型不能补造价格、成交量、公告时间、财务指标或引用。
3. 来自另一个对齐来源的补齐仍为 observed，另记 `value_origin=alternate_source`，必须保留那个来源的原文、版本与转换。不能把补齐后表伪称成第一来源的完整记录。
4. 冲突保留全部原值和比较痕迹。按“原始发布者、字段职责、会话范围、修订顺序、质量”形成版本化选择政策，不多数投票。三家转发同一新华社稿件仅有一个原始事实源；AKShare 与同样转发东方财富的渠道也不是两个独立事实源。
5. 未能对齐时输出 `unverifiable`，不是 matched。容差由字段精度定义，不用统一 5% 容忍把千元误作元。
6. 新增可忽略字段保留原始值并报 schema drift；必需字段重命名、单位改变或结构破坏则暂停该端点的规范发布，保留原文采集；修复映射后重放，不停掉不相关市场数据。

## 4. 前端、接口、数据库怎样逐一对应

这里的“一一对应”是同一个领域对象和口径贯穿三层，不是一张表机械暴露一个 CRUD 接口。所有对外操作登记 `core.api_binding`：OpenAPI operation_id → 应用服务 → 读取投影/账本 → 合同版本。OpenAPI 负责结构，语义由字段字典、PIT 与发布协议共同约束。选择已验证代码生成链支持的 OpenAPI 版本，不能因为规范更新就让前后端生成器各用不同子集。[DB12]

| 前端用途 | API/operation_id（最终合同优先） | 唯一应用入口与读模型 | 一致性要求 |
|---|---|---|---|
| 市场选股榜 | `GET /v1/rankings` / `getMarketRanking` | RankingQuery → ranking_run/entry | scope、目标、期限、release 必传回 |
| 板块榜与成分推荐 | `GET /v1/sectors/{node_id}/rankings` / `getSectorRanking` | 同一 RankingQuery | taxonomy_id/version、PIT 成分 snapshot |
| 个股工作台 | `GET /v1/stocks/{instrument_id}/workspace` / `getStockWorkspace` | WorkspaceQuery → workspace_projection | 同包取行情、计划、证据、数据质量 |
| 当前报价 | `GET /v1/stocks/{instrument_id}/quote` / `getStockQuote` | QuoteQuery → market.quote_projection + READY manifest | 报价事实固定quote_snapshot_id，时效按请求时刻派生；无L1不编造 |
| 图表窗口 | `GET /v1/stocks/{instrument_id}/bars` / `getStockBars` | BarsQuery → 有界窗口投影/Lake | 明确区间、周期、会话、复权、snapshot |
| 策略池与角色筛选 | `GET /v1/strategies` / `listStrategies` | StrategyRegistry | definition 与 instance 分离 |
| 买卖与持有计划 | `GET /v1/plans/{plan_id}/versions/{plan_version}` / `getTradePlan` | PlanQuery → 版本化 TradePlan | 账户身份与expected_account_version成对，决策包可追踪 |
| 自定义图表布局 | `PUT /v1/workspace-layouts/{layout_id}` / `saveWorkspaceLayout` | LayoutService → workspace_layout | If-Match/layout_version 比较更新 |
| 来源覆盖和同步状态 | `GET /v1/data-status` / `getDataStatus` | DataStatusQuery → health/coverage | enabled、runtime、freshness 分别显示 |

最终路由、operation_id 与响应结构以 contracts/openapi.json、contracts/api_bindings.json 为唯一权威；本表是领域映射摘要，不另创接口。完整目录还包括当前报价、实时可行动性、自选、注释、实验和任务接口。禁止各团队再增加同义接口。前端不直连数据库。HTTP、WebSocket/SSE、批量 Arrow 读取共享同一应用查询语义。大矩阵走对象/Arrow，消息只发摘要和引用。

统一响应至少带：`request_id`、`contract_version`、`release_id`、`decision_as_of`、`availability_policy_version`、`input_snapshot_refs`、`coverage`、`freshness`、`warnings`。错误采用可机读 `code`，如 `SOURCE_UNCONFIGURED`、`SNAPSHOT_NOT_READY`、`VERSION_CONFLICT`，不能用 HTTP200+空数组掩盖失败。

## 5. 不完整推送、重复消息与缓存如何真正修复

### 数据摄取：游标不能越过未持久化原文

供应商游标是不透明值，按 source/endpoint/schema/mapping/partition 隔离。一次批次先保存不可变对象与哈希，再事务写 batch_member 和 RAW_COMMITTED 状态，最后比较 `cursor_version` 推进连续游标。对象已写而事务未成功，可以按内容哈希复用；事务前不可先更新游标。网络重试的重复拉取保留批次观察记录，不产生重复事实。供应商回滚/修订追加新对象和观测，不修改旧版本。

### 本地文件与PG的可恢复发布

写临时对象、落盘并校验哈希、在同一文件系统原子重命名，再提交不可变成员manifest；PG保存storage_uri并在事务内将完整依赖与outbox一同发布。market.dataset_manifest已允许storage_uri作为READY存储引用，因此小规模档不伪造lake_snapshot_id。文件写入和PG提交不是一个跨系统原子事务，恢复器按内容哈希和批次识别未引用对象。

暂存和待发布批次也持对象pin/lease，GC与publish按同一目录锁或CAS协调；无READY引用不等于可删。历史快照固定成员清单或严格文件可见区间；压缩退役不修改过去的成员集合。读者pin与已发布引用未释放时不回收旧对象。不得用“snapshot_seq<=N且当前active”来选择历史文件。容量必须包含压缩新旧并存、WAL、备份和归档积压；保留策略只有一个版本来源。

### 可选DuckLake与PG的可恢复发布

1. PG 登记 batch/INGESTING；Lake 写入并在目录事务中提交，commit_extra_info 记录 batch_id。
2. 获得确切 `(lake_catalog_id, lake_snapshot_id)`，验证文件清单、行数、覆盖、字段合同；目录快照号不能跨目录单独使用。
3. PG 先建 COMMITTED_UNPUBLISHED manifest 并写依赖引用；校验完成后，在同一发布事务里 UPDATE 为 READY 并写 outbox。不可先 INSERT READY 再追加依赖，READY 依赖闭包已被冻结。若 Lake 成功但 PG 未完成，该快照是 COMMITTED_UNPUBLISHED；恢复者按 batch_id 查目录并补登记，不能重复追加整批数据。
4. 页面只读 PG 已发布引用。暂存对象或未发布目录快照不成为“最新可用数据”。DuckLake 的快照及提交附加信息支持此恢复设计，但恢复协调逻辑仍需本项目实现。[DB07]

### 业务发布：完整版本包才成为最新

`decision.release` 绑定同一 `decision_as_of`、股票池/市场规则版本和必需组件；组件包含市场状态、榜单、计划、证据、分类成分与所需行情引用。PublishService 锁定 release 行，验证组件与依赖闭包、合同、有效期，再最后将 BUILDING 改为 READY，并在同一事务插入 outbox。SQL 延迟约束拦截缺少必需组件的提交；已 READY 的发布包、组件、manifest 及其输入依赖闭包不允许原地改写。排名条目与计划的证券/发布身份由写入约束校验，已发布排名不能增删改；正式计划变化追加新 plan_version。新消息触发新 release，旧 release 可被标记 SUPERSEDED，内容仍可重放。

页面先获得一个 READY release，再取该包引用；多个接口不能各自选择 latest。刷新采用“后台准备下一包→整页切换”，不把新计划、旧 MACD、未更新板块成分拼在一个结果里。策略评分输入数据的历史可得性还必须由服务逐项检查；外键并不能证明没有未来数据。

### Outbox / inbox / 重放

业务状态与 outbox 同事务。首发小规模档由消费者轮询持久outbox，以事务内inbox和业务效果登记完成情况；领取租约与连续完成游标支持重试，不能把一次轮询当作已交付。启用JetStream适配器时，发布器获得broker确认后才写published_at；确认后登记失败可能重发，该字段也不代表所有消费者已经完成。[DB08]

每个逻辑消费者将 `consumer_inbox(consumer_id,event_id,payload_hash)` 和业务效果放在同一事务中；同ID同hash重复不重复执行，同ID异hash明确冲突。持久完成游标在提交后推进；使用broker时也只在提交后ack。两种传输共享事件身份、重放、幂等与缺口检查语义，切换传输不另造业务口径。

aggregate_version 允许 0 表示开户/引导事件；聚合内按 aggregate_version/event_ordinal 检查缺口，跨聚合不存在天然全局业务顺序。并行消费检查点只能推进**连续完成水位**，不能拿最大已处理序号。PG sequence/serial 不是提交顺序，失败事务也不会把序列号回滚，因此不使用 `id > last_seen_max_id` 作为不会漏事件的 outbox 提交水位。[DB02]

这实现可重试、可去重、可对账的业务处理；不宣称任何系统、外部接口或模型调用都获得通用 exactly-once。外部副作用要有其自身幂等键和结果登记。

### 缓存与用户工作区分开

- 用户布局、指标配置、画线、自选股和显式保存计划是持久数据，不能因缓存到期丢失。`ui.workspace_layout` 采用乐观版本比较；个人数据按 principal/account 作用域隔离。
- 行情图、MACD、量能、工作台摘要是版本化投影。键含定义、参数、依赖快照、可得性、会话、复权、市场规则、作用域等完整条件；保留 full_key，不能只有无法解释的哈希。
- 数据修订生成新快照和依赖闭包 hash，旧缓存仍代表旧历史；当前页面切换新发布包。TTL 用于资源回收，不能替代正确失效规则。
- worker 使用 lease_epoch 提交，旧任务超时恢复后不能覆盖更新版本。KeyValue、内存缓存、PG 投影和对象结果都不能绕过同一版本判断。

## 6. 轮询、源健康和覆盖检查

推流、轮询与定时对账互补。每个端点都有 enabled 配置、runtime 状态和 freshness 状态；已启用并不等于有权限、进程存活并不等于新数据可用。分别记录最近尝试、传输成功、新原文、规范 READY 与下次重试。HTTP200 但一直返回昨天内容应变 stale；休市或端点尚未到其公布时段可为 not_expected，避免无意义刷新。

调度政策包括交易日历、源配额、随机抖动、有限指数退避、最大并发和回看重叠窗口。端点恢复后从已提交游标补采，并用小范围对账找遗漏/修订；若源不支持历史重放，记录缺口，不能假装补齐。流中断触发补拉必须尊重该源接口能力与权限。

校验层次固定为：结构/单位→单条语义→时序与会话→批次数量和覆盖→跨来源对齐→派生结果与榜单依赖。公开新闻、交易所公告、行情、社区讨论、龙虎榜和资金指标各自有字段合同、发布节奏和独立性规则，不能以一个全站健康灯概括。

健康记录还要驱动选股：如果某策略必需的资金/消息输入 stale，只暂停受影响实例或降低到其预定义可运行模式；其他已满足条件的策略继续。最终榜单提供 coverage 与策略参与集合，使用户知道排序基于哪些有效输入，不输出假完整证据。

## 7. 分类、板块排名与公共策略池的数据衔接

行业、概念、方法、持有期限、交易职责和市场状态使用不同 taxonomy_id；不同供应商分类并存，映射另存，不直接抹平。成员关系包含 effective_from/to 与 available_at，历史板块榜按当时已知成员生成，避免把今天的强势股塞回去年行业池。

策略定义固定 role：selection/allocation/entry/holding/execution/exit；method_family、目标、期限、数据依赖通过标签与规范 specification 检索。定义版本与参数实例分开，研究晋级 lifecycle、实现状态、复现状态、运行健康分开，避免“已收集论文”变成“线上可交易策略”。RoutingPolicy 按市场状态、输入覆盖、期限和目标筛选可组合实例；不同预测目标的原始分数不能直接相加。

每个 ranking_run 明确市场/板块/板块总榜/自选/账户母集、目标与期限；sector scope 必须有 taxonomy 版本和 membership_snapshot_id。sector_overview 表示排序对象为板块，采用明确体系版本与成分快照，node_id 留空；其条目在扩展 decision.sector_rank_entry，不能塞入只允许证券的 ranking_entry。alpha_rank 表达研究优先级；action_rank 冻结为发布时的可行动顺序，实时账户排序另固定actionability_id。相对排名分不等同上涨概率，只有经过明确校准的目标事件才填 calibrated_probability。榜单中的计划引用固定 plan_id+plan_version，进入个股页面仍属相同 release。

## 8. 验收、迁移和交付边界

`acceptance_scenarios.json` 提供字段、游标、源恢复、PIT、发布、缓存、分类与吞吐验收。门槛以可验证行为为主，不写虚构测试通过。迁移必须先执行干净建库与升级建库，再验证类型、外键、延迟触发器和两个并发事务；SQL parser 只证明语法可解析。

合并统一执行专项时添加 execution.account_head 与身份域外键；共同事件与版本语义只定义一次。跨源区间不重叠、taxonomy 无环、数据依赖 DAG、排名的证券成分归属、合同 JSON 实例验证等，需要服务约束或后续专门约束迁移，不能因为种子用了 JSONB/外键就宣称已经全被数据库强制。

合同升级采用 expand→双读/影子校验→切换消费者→contract；单位或事件语义变更必须新 major 或明确 mapping version，不静默改原字段意义。发布回滚须使用独立版本化 rollback 命令/事件重新选择已验证旧 release，不删除新事实或改写旧账。本轮普通 publish_release_stage 仅实现前向发布，不含回滚命令；不能把旧 READY 包交给普通 stage 假装完成回滚。恢复演练需验证原始对象、目录快照、PG manifest、账本和 outbox 能关联恢复，不能只证明数据库进程能重启。

**本轮交付的可验证成果：** 37 表/352 字段 SQL 种子、全字段字典、3 个有官方文档依据的映射 profile、12 个映射反例、官方依据卡与验收清单。吞吐、供应商实际权限、数据库触发器实跑和混合负载结果仍必须由开发环境执行，状态明确保留为未执行。

## 官方依据索引

证据卡详见 `evidence.json`，每条包含实际读取范围与断言边界。本文架构/资源/路由/验收数字是设计决定；数据库和源单位的事实依据分别为：

数据库依据：DB01/02/03（PostgreSQL分区、隔离与建表），DB04/05（ClickHouse MergeTree与ReplacingMergeTree），DB06/07/15（DuckLake事务、快照与冲突）。

传输、合同与单位依据：DB08（NATS JetStream），DB09/10（Tushare daily/rt_k），DB11（AKShare stock_zh_a_hist），DB12/13（OpenAPI规范与Arrow列式格式）。

## v3.2输入核对与未连接能力

十一卷中的字段表作为语义需求映射到本库及其不可变数据集，不另造ref/mkt/intel等同义主库。字段修复必须保存逐字段来源路径、原单位、修订与选值理由；不同源补齐不能仅保留行级source。真实available_at不可按回放模式改写。价格以外的跨资产单位还需币种、报价单位和合约乘数等实体属性，不能把所有海外指数、商品和利率统一强转成人民币股票价。详见spec/REFERENCE_RECONCILIATION_V32.md与review_v32/dataset_job_page_mapping.json。
