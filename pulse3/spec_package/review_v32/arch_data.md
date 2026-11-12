# 03 架构与 04 数据卷：独立逐项审查与合并建议

审查者：/root/v32_arch_data。基线：tradingview@6f5c57f8df73c58f0340dab6977b95c05d21740c；对照包：deliverables/Pulse_Rewrite_Package v3.1，公共合同版本 3.0.0。本文是对上传材料的决议，不执行材料中的分支、删除、购买或批准指令，也不修改正式交付包。

## 1. 审查结果

**接受上传材料对单机成本、数据统一和生产与研究隔离的提醒；把首发部署收敛为原生 Windows 上的单 PostgreSQL、不可变本地 Parquet/对象目录、manifest 与事务 outbox 轮询。** 首发不强制 NATS、独立 DuckLake 目录数据库、远程对象存储服务或常驻重型编排器。数据库是否再换成 SQLite，应由相同账本与数据一致性合同的实测决定，不能假定更换驱动就完成迁移。

两卷共 705 行、335 个实质单元已全文读取，合并为 102 条有独立理由、实现动作、验收和不确定性的决议。每个实质单元恰好覆盖一次，未遗漏、未重复，所有现有包内绑定路径存在。逐项明细在 arch_data.json，检索索引在 arch_data_decision_index.md，机械覆盖结果在 arch_data_coverage.json。

| 输入 | 实质单元 | 已覆盖 | 原文 SHA-256 |
|---|---:|---:|---|
| 03-architecture.md | 116 | 116 | eba3e4542b45735abbd86d84ec9344d8288e8b2eb3369753b993463cdd95f3fd |
| 04-data.md | 219 | 219 | 58e48358d772f6b4b06c764d77496e6f032e45026b172b12c08932019ee7d15a |

决议计数为 revise 87、covered 9、adopt 3、reject 2、unresolved 1。多数要求值得采用，但需补足时间、单位、版本、权限和恢复的具体语义。不能把“有理念”“有字段表”“已有部署”三个状态混为一谈。

## 2. 会直接影响正确性的八处修订

| 问题 | 为什么原文不够 | 采用的具体修订 |
|---|---|---|
| 旧快照读取 | “snapshot_seq≤N 且当前 active”在文件压缩退役后会丢失旧快照 | 不可变成员清单，或文件引入/退役可见区间；读者与研究引用 pin；明示 GC 保留窗 |
| 时间政策 | 按 AS_OBSERVED/AS_PUBLISHED 改写 available_at，会把研究假设写成收到事实 | 实际 first_seen/available 不变；availability policy 单独推导研究可用时刻 |
| 60 秒快照合成分钟线 | 采样点不能恢复中间最高/最低、首次封板及开板次数 | 区分真实 1m、采样区间和合成 bar；明确允许使用的策略/执行模型 |
| 容量 20–25GB | 状态库加 7 个完整备份的上界情景已推到约 36.5GB，另有大量未列项 | 按数据集、热冷保留、副本、构建峰值、归档离线期重算 |
| SQLite“只换驱动” | NUMERIC 亲和性、延迟约束、JSONB、FK 与账本事务并不等价 | 默认兼容现有 SQL 的单 PG；SQLite 另做精确金额和事务语义移植 |
| outbox 原子性 | DB 状态与事件同事务，不意味着外部 Parquet 写入也同事务 | staged object → 校验/持久化 → 目录登记 → READY+outbox；失败状态可恢复 |
| 冻结主干过渡 | PC 离线时得分可能断供；一条试用策略仅有 10% 上限，却触发旧桥结束 | 桥接产物有有效期、输入和模型版本；剩余权重显式分配；切换不按首条晋级触发 |
| 前端失效重取 | 同时重取 latest 仍可能混合榜单、图表、计划版本 | 先取 current release 指针，再固定 release_id 读取整页；实时行情独立叠加 |

这些问题都有具体反例或合同差异；其共同目的，是让榜单、个股分析、策略比较和持仓结果使用同一套事实。

## 3. 首发部署：采用真正的简化

### 3.1 物理组件

| 组件 | 紧凑默认 | 理由 |
|---|---|---|
| 业务、目录和状态 | 单 PostgreSQL 实例，按 schema 分域，连接池有上限 | 复用已定义的精确 Decimal、唯一账本、外键、不可变发布和事务约束 |
| 原始对象和分析文件 | 本地不可变目录，按批次和数据集组织，Parquet 为主 | 单机不必跑对象存储服务器；目录用明确 manifest |
| 内部通知 | 事务 outbox/inbox 与轮询 | 首发无需消息服务器；保留幂等和重放语义 |
| 数据查询 | 数据库热投影和只读列式查询 | API 不每次从 raw 重扫全市场；历史分析固定文件集 |
| 作业 | 应用内持久化 JobSpec、运行账本与有限并发 | 产物依赖替代时间错开；重计算不堵采集 |
| 服务化 | Windows 原生服务，可合并轻量角色 | 不增加 Linux/WSL 或 Docker Desktop 前置条件 |
| 大规模训练与挖掘 | PC 或另配计算节点 | 生产日更的固定特征和已发布模型不依赖 PC 在线 |

PostgreSQL 官方 Windows 下载页提供服务器安装器，并在测试平台表中列出 PostgreSQL 18 对 Windows Server 2025/2022 的测试。这支持原生部署的可行性；具体 Python 驱动、SQL 迁移、扩展和二进制版本仍须在锁定组合上验证，不表示本项目已跑通。[S1]

逻辑上保留 collector、stream、worker、api、watchdog 五个职责；物理进程数是性能配置。最重要的隔离是重任务不能占满采集/消息执行资源，API 不持有唯一业务状态，watchdog 自己由系统服务管理器托管。第三方库硬卡死需可终止子进程或可取消的底层 IO；Python 线程等待超时不代表线程已经被终止。

### 3.2 为什么不直接否定 SQLite

SQLite WAL 允许读者与写者并发，但写事务串行，WAL 依赖同机共享内存机制。[S2] DuckLake 官方把 SQLite 作为多个本地客户端的可选目录，并说明锁冲突重试；所以“嵌入式数据库完全不能多进程”是错误概括。[S3]

真正的迁移成本在语义。现有 SQL 使用 numeric(38,12)、JSONB、timestamptz、关系约束、延迟执行的借贷平衡约束等。SQLite 的 NUMERIC 类型名不提供等同的 38 位精确金额，可能转成 REAL。[S4] 本次最小例子插入 12345678901234567890123456.123456789012，取回类型为 REAL，数值变成 1.2345678901234568e+25。这是类型语义反例，**不是 SQLite 不能用于资金系统的结论**：可以用十进制文本或定标整数及对应算术实现，但需要明确定义并验收另一份存储适配。

先选单 PG，是降低已有合同迁移成本；不是宣称 PG 一定比 SQLite 占用更低。SQLite 备份不能仅复制主文件而忽略 WAL；官方 Backup API 提供一致备份机制。[S5] 如果选择 SQLite，账本备份、并发、恢复和金额精度需要一起验收。

DuckDB 原生嵌入文件的写入模式与服务器协议、DuckLake 目录模式不同；官方文档分别描述它们，不能用一个“单写者”口号概括所有组合。[S6] 没必要为了采用新能力而同时引入所有模式。

### 3.3 硬件约束的证据边界

上传材料写的是 **2 核 8GB、Windows Server 2025、C 盘约 50GB 可用**。仓库历史 docs/research/2026-11-01-design-v1-review.md 第 47/71 行支持前两项历史场景，docs/EXECUTION.md 第 219 行也记录 Server 2025。此次没有连接主机，所以不能把历史记录说成当前机器实测。

五个角色的表内峰值之和约 3.35GB；“常驻 1.9GB”需要额外的 worker 常驻值才算得出来。无论 1.9 还是 3.35，都不能自动涵盖 OS、同机其他应用、PG、Arrow 缓冲、临时子进程、文件缓存和查询 spill。应记录进程 RSS、系统可用内存和并发时长，再确定预算。

Docker 官方确认 Docker Desktop 不支持 Windows Server，这只支持不把 Desktop 作为前置依赖；不支持把所有服务器容器方案一并判为不可用。[S7]

## 4. 容量：15GB/年不能作为整库规划

若附件的 60.2MB 规范快照样本能代表完整交易日，按 250 个交易日估算是 15.05GB/年；299.9MB raw 则是 74.975GB/年。250 是规划样例，实际日历由日历表决定。这两项没有证明样本包含全部证券、时段、五档深度、热点 10 秒和竞价 5 秒增量。

### 4.1 把表内数据先算全

| 项目 | 沿用附件的规划值或上界情景 |
|---|---:|
| 规范日线历史 | 1GB |
| 全市场 60 秒快照 6 个月 | 7.5GB |
| raw 20 个交易日 | 6GB |
| 派生数据 2 年 | 6GB |
| 活跃状态库上界情景 | 2GB |
| 7 个完整日备份，各按 2GB 上界 | 14GB |
| 合计 | **36.5GB** |

这不是“真实库一定占 36.5GB”：状态库更小或备份压缩/增量化会降低数字。它证明附件的“小于 2GB，加 7 份备份”不能在总额里忽略备份而笃定 20–25GB。

尚未计入：独立分钟线、长期热点/持仓快照、竞价密采、周末新闻、索引/WAL、压缩时输入和新输出并存、临时排序/构建文件、隔离区、日志、研究收件目录，以及同机其他应用。

以 6000 证券、240 个分钟区间、250 交易日作纯行数演算，全市场分钟线一年可达 3.6 亿行。**没有实际 codec、列集合和样本测量，不能把行数直接换成几个 GB。** 三年日线约 400 万行不能代表平台最终数据规模。

### 4.2 PC 离线不是容量策略

取 raw+canon 约 0.3601GB/交易日的示例，只要归档未确认，旧 raw 就不能删。假设空闲只剩 13.5GB，保留 8GB 操作底线，示例增长约 15.3 个交易日就抵达底线，还没加周末新闻和其他写入。这个数字是反例情景，不是服务器预计耗尽日期。

容量状态要显示“当前空闲减去构建/恢复峰值预留和运维底线”，再除以近期观测净增量，得到可解释的预计可持续时间。每数据集明确 raw/canonical/derived 热留存、冷归档位置、备份副本数、重算能力和删除条件。正在回放、研究引用或尚未归档的唯一证据不能被普通 active=false 清理器删除。

若要求所有原始行情和消息持续完整录制，归档端应有可用性承诺；常关机 PC 只能是可选消费者。暂停非必要社区原文可以是一种有限资源政策，但必须标覆盖缺口和暂停原因，不能同时继续声称全链路可重放。

## 5. 时间、文件、事件：三个原子性边界

### 5.1 文件快照不是当前活动清单

最小反例：快照 1 引入 F1；快照 2 把 F1 压缩成 F2、F1 变 inactive；读取快照 1 时按“introduced≤1 且 active=true”查询，得到空集。

正确方法是固定快照成员，或查询 introduced_snapshot≤N 且 retired_snapshot 为空或 N<retired_snapshot。随后尊重文件内行的决策可得时点，再选该时点可见的修订。把最新 revision 先选出来、之后才滤 available_at，也可能错误丢弃当时可用的旧版本。

平台级 snapshot 使用既有 UUID；DuckLake 本地序号与 catalog_id 组合。release 固定多个 dataset manifest 的依赖闭包，不能用一个全局自增数掩盖来源不同步。

### 5.2 可得时间不随研究模式改变

| 时间 | 含义 |
|---|---|
| source_published_at | 来源报告的发布时间及精度 |
| first_seen_at/received_at | 本系统实际看到或收到时刻，按既有合同映射 |
| available_at | 该版本经规定处理后在本系统可供决策使用的时刻 |
| valid_from/valid_to | 业务世界中何时生效 |
| recorded_at | 何时持久化 |
| availability_policy_version | 研究怎样从这些证据确定可使用边界 |

available≥received≥published 可作为可信时钟下的期望，不能靠改写原值强制使其总成立。源发布时间异常、日期精度或时区错误应保留原值并标质量问题。AS_PUBLISHED 只在明确证据和假设下重建，不会变成我们真的当时收到。

特征、榜单、计划、实验和缓存共用现有 FeatureKey/ASOF 语义；代码版本+snapshot+参数不足以区分复权、会话、日历、股票池、证券顺序、政策和账户范围。

### 5.3 外部文件与数据库不是一个事务

可恢复流程是：获取响应，持久化 raw 与批次，解析校验，同卷临时文件写入，校验与持久化，不可变对象定位，COMMITTED_UNPUBLISHED，最后 READY 与 outbox 同数据库事务。

状态名称可沿用现有表。重点是 READY 前对象完整且哈希可核，游标在 raw 已持久化后推进，失败重试按 batch_id 幂等；孤儿文件可识别，已发布文件不可原位覆写。

库内状态变更、业务事件、消费者 inbox 和该消费者业务结果可以同事务。文件 IO 和外部推送不能靠一句“outbox 原子性”获得分布式 ACID。

## 6. 内部通信与前端打通

### 6.1 紧凑轮询也使用完整事件合同

至少保留 event_id、payload_hash、schema_version、producer_version、aggregate_type/id/version、event_ordinal、occurred_at、recorded_at 和 payload/ref。消费者业务结果与 inbox 去重同事务。

事件量需说明粒度：6000 证券每 60 秒一个报价，逐证券事件是约 100 次/秒的平均产生率；整帧一次事件完全不同。这是算术示意，不是源吞吐测量。宜以完成批次、帧或数据集分区通知，载荷引用 manifest；细粒度个股告警按独立语义产生。

PostgreSQL 序列分配不等于提交顺序。发布器不能仅用“seq>已见最大seq”扫描后永不回头；应扫描待处理事件，或使用不会漏迟提交事件的连续完成机制。恢复按 inbox 与已保留事件对账，offset 是优化而非唯一业务真相。并行消费者需明确租约/提交权，不能用全局最大完成序号冒充连续完成水位。

至少一次投递下，同 event_id 不同 payload_hash 必须显式冲突，不能直接说已处理然后丢掉。

### 6.2 同一页面固定一个决策发布版本

前端先读取 current release 指针，再给榜单、个股工作台、证据、图表计划和板块成员请求带固定 release_id。SSE 通知新 release 就绪后整页切换，更新中半成品不可见；SSE 有游标、重连、去重、乱序与 resync 语义。

实时报价可独立覆盖在旧决策图上，但标出 quote_as_of；它不会暗中把旧推荐解释改成新时刻分析。账户可行动性需基于精确账户版本再次判断，而非让旧 ranking 行动状态永久有效。

serverless/无状态 API 只是缓存可抛弃：布局、图表、笔记、自选股、告警规则仍在数据库。缓存键包括用户/账户范围、release、投影版本、复权和时间参数；不同用户的持仓视图不能共用裸 instrument_id 缓存。

## 7. 字段和数据集归并

现有包的 80 表/883 列、31 操作是合同种子与静态验证产物，不能因数量大便认定所有源已接入。附件新增的内部数据集应登记行合同和作业绑定，而不是冒称已有同名公共 Schema。

| 附件逻辑数据 | 归并位置/方式 | 必补语义 |
|---|---|---|
| instrument/alias | core.instrument/alias/state | UUID、别名有效期、业务时间、知识修订 |
| calendar/rule/fee | 版本化内部参考数据集；执行标准 MarketRules/CostProfile | 会话、effective/available、渠道、舍入；正式行合同需登记 |
| sector/member | taxonomy/node/tag 与成员 manifest | 体系、版本、当时成员；role_hint为派生判断 |
| factor/corp_action/capital | 内部 canonical 数据集及 manifest | 因子基准、行为独立ID、登记/到账、股本定义 |
| daily/minute bars | CanonicalBar、market.bar_projection | raw价、参考昨收、区间量额、is_final、采样类型 |
| snapshot | CanonicalQuote、market.quote_projection | frame、源/接收/可用时刻、深度能力 |
| limit_pool | 内部状态观测，供候选池/特征消费 | 池类型、时点、修订、首封精度 |
| index/fund/xasset | 共用基础观测，资产元数据分型 | 币种、乘数、计量单位、夜盘、连续合约 |
| lhb/seat | intelligence.seat_report/row/entity_version | 单/多日窗口、side/rank、匿名池、可得性 |
| announcement/news | document_revision、claim/evidence | 多主体、原文版本、标题/全文水位、转载链 |
| calendar/event | 内部计划/发布事件，映射claim/event时保留身份 | 参考期、单位、预期/初值/修订值、延期 |
| community/opinion | community_observation；观点研究扩展 | 窗口覆盖、固定评价目标、只读采集 |
| feature/signal/state | FeatureKey/FeatureResult；内部Signal等需补合同 | 定义、target/horizon/scope、依赖与权限 |
| strategy/ensemble score | ranking_run/entry及内部评分产物 | 前向与事后分开、分母、校准、桥接来源 |
| 工作台 | ui.watchlist/member、workspace_layout、annotation | owner/version、复权坐标、筛选单位 |
| jobs/model calls | ops.job_definition/run；analysis.model_policy/run | 版本、输入、租约、原模型输出和调用费用 |

不能偷偷把 instrument_id 从 UUID 换成代码字符串，也不能把 0.0998 比例换成 9.98 百分点却沿用同字段。规范数字保留 unit 字典；核心金额/价格保留 Decimal 字符串传输和现有精度。SQL生成器不能替代跨行平衡与事务实现。

“非主键全部可空”应淘汰。观测数值可以未知；合同身份、来源、可得时点、状态、哈希和已发布引用按状态强制必填。

同一对象同一窗口的备源真实观测补缺可接受；昨天值、0或模型猜测冒充观测不可接受。补入后要记录字段级lineage，整行一个source_id和flags不够。跨源多数票只能辅助诊断，不能决定全部真值；媒体转载数量尤其不能变成事实确认程度。

## 8. 冻结主干桥和新策略上线

桥接旧融合分需要显式 LegacyScoreBridge 产物，不能直接读取研究私有目录。

| 组别 | 内容 |
|---|---|
| 身份 | producer、legacy_source_id、代码提交、策略/因子定义、参数哈希 |
| 输入 | manifest、universe、规则/日历、availability policy、训练截止 |
| 输出语义 | 目标、期限、分数尺度/排名分母、单股覆盖、缺失原因 |
| 时效 | decision_as_of、computed_at、imported_at、valid_until |
| 权限 | legacy待复验标签、可影响榜单/角色、与新策略组合政策 |
| 证据 | 原文件哈希、导入批次、旧主书引用、差异对账 |

桥分可以成为新 release 的一个已声明输入，但不能借此宣称旧分由新内核计算、旧证据已通过新门槛，或把旧账本与新账本混记。

必须测试：PC 整日离线；仅一条试用策略合格且权重上限10%；迟到旧分较晚才上传。剩余权重明确走基准、继续有效的冻结桥或现金等既定政策，不自动把10%拉满，也不因首条试用出现就结束桥。

结束过渡需新生产链独立日更、必需目标/期限覆盖、分数与权限版本齐全、剩余权重完整、并行对账通过。月度研究可以在PC，但服务器应独立执行已批准的固定日更公式。

## 9. 有辨别力的验收

1. 快照压缩后旧manifest结果不变；研究pin未释放禁止GC。
2. 晚公告、ST更正、成员变动、宏观修订不会出现在早期决策；invalid或删除修订不擅自退回旧valid。
3. 手/股、千元/元、参考昨收/实际前收、累计/区间量、raw/adjusted有真实样本。
4. 主源缺失可用对齐备源；主源硬错误不被优先放行；重复转载不算独立多数。
5. raw写后、目录登记前、READY前后kill，恢复不漏数据、不重复推进游标。
6. 晚提交、重投、乱序、同ID异载荷、消费者离线后恢复，不漏业务效果或重复分录。
7. SSE重连和连续发布后榜单/计划/证据仍同release；账户scope缓存隔离。
8. 部分成交、撤单中成交、T+1、分红、送转、费用更正全走唯一账本。
9. PC离线、过期桥分、单策略10%时有显式政策，不伪装完整新模型评分。
10. 完整交易日及周末采样、归档中断增长、临时峰值、备份还原都进入容量预算。
11. 原生Windows锁定版本安装、迁移、服务恢复、并发RSS与API尾延迟实测，无Linux前置。
12. 每新增数据集有schema/owner/job/manifest/API或内部消费者/页面链路，新增操作显式登记。

## 10. 证据边界

本次完成输入全文读取、合同/SQL/参考程序对照、固定仓库历史文档定位、官方技术资料核查及四类合成/微型反例：SQLite金额类型、压缩快照、分钟采样不可辨识、容量算术。没有连接生产主机、没有执行生产PG迁移、没有购买或实时连接金融源。

335项有裁决不等于全部外部事实已有原始实证。Tushare套餐/权限/历史起点保留待官方核证，交主审集中处理；图表库和Python生态的兼容版本也不能用此文代替锁定与安装验收。

| 引用 | 官方资料 | 支持范围 |
|---|---|---|
| S1 | https://www.postgresql.org/download/windows/ | Windows安装与EDB测试平台；不证明项目已跑通 |
| S2 | https://www.sqlite.org/wal.html | WAL读写并发、单写者、同机机制 |
| S3 | https://ducklake.select/docs/stable/duckdb/usage/choosing_a_catalog_database | SQLite本地多客户端目录选项 |
| S4 | https://www.sqlite.org/datatype3.html | NUMERIC亲和性及REAL转换 |
| S5 | https://sqlite.org/backup.html | 一致备份API机制 |
| S6 | https://duckdb.org/docs/2.0/connect/concurrency | 原生文件、服务器、DuckLake的不同并发模式 |
| S7 | https://docs.docker.com/desktop/troubleshoot-and-support/faqs/windowsfaqs/ | 仅Docker Desktop不支持Windows Server |

已核公共v3有Ranking、SectorOverview、CandidatePool、CanonicalBar、CanonicalQuote、TradePlan、ModelRun、JobRun等；**没有同名公共Signal、MarketState、ActionIntent、MarketRuleSet或FeeSchedule定义**。有些概念存在于执行标准或内部设计，有些需要补合同，合并时不能虚称已作为公共接口实现。报警、日历、公司行为等扩展也要先登记真实合同与存储绑定，再写已支持。
