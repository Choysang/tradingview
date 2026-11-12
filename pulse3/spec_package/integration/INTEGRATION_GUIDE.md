# v3 集成边界、发布协议与合同登记

本目录补齐前述三个领域种子之间的实际关联。正式 API 只有 `../contracts/v3.schema.json`、`openapi.json`、`api_bindings.json`、`naming_migration.json` 四份权威文件；历史 v1/v2 合同不参与新生产接口验证。本轮交付是规范、数据库种子与结构验证，没有部署 API、连接 PostgreSQL 或接通真实行情源。

## 1. 文件与迁移顺序

顺序固定为：

1. `database/core_schema.sql`：证券、源、规范观测、快照、策略、发布包、排名、工作台、outbox 等。
2. `execution/ledger_schema.sql`：实际存在的 `execution.account_head`、账户事件、账本、数量与预留。
3. `sources_sectors/sources_sectors_schema.sql`：消息、主张、讨论、资金代理、龙虎榜、板块总榜与候选池。
4. `integration/integration_schema.sql`：跨领域外键、发布代次、实验联赛、用户持久数据、模型/任务与报价投影。

四份文件均自行包含事务；部署工具按顺序逐个执行，不把某份后置迁移漏掉。集成增加 `TradePlan.account_id → account_head`，并把期望账户版本关联到确切 account_event；执行域的证券列关联同一个 core.instrument。不存在另一个虚构的 execution.account 或独立股票主表。

`field_dictionary.json` 覆盖本次新增表。`all_fields.json` 从四份实际 SQL AST 汇总所有表与字段，包括类型、NULL、默认值、PK/FK、注释来源与明确单位；没有说明的字段标记需要领域文档，不能根据名字猜一个业务定义。SQL约束与当前正式合同不一致时应修复源文件后重新生成字典，不能手改生成字典掩盖问题。

## 2. 发布时间与任务代次

### 2.1 为什么只比较 as_of 或现读指针不够

两个任务可以具有相同 decision_as_of，却使用不同修订或模型结果。旧任务完成较晚，若临时读取最新 pointer_generation 并把它当作自己的 CAS 期望，就能覆盖较新的结果。时间大小比较不能阻止这种“重新绑定旧任务”。

因此分开三个持久对象：

| 对象 | 含义 | 何时可变 |
|---|---|---|
| release_slot | 作用域的当前包、指针代次、最新签发任务代次 | 仅协调服务的短事务更新 |
| release_stage | 一次任务固定的 release、输入闭包、as_of 与创建时 base | 创建后不可更新/删除 |
| release_publication | 一次成功发布的永久事实与 outbox 引用 | 追加后不可变，重试返回原记录 |

`begin_release_stage` 先锁 slot，捕获当时的 current_release_id 和 pointer_generation，再签发新的 stage_generation。调用方不提供“新的期望base”。后续 job_run 的 stage/参数/定义不可改；model_run 的 stage/输入/政策/as_of 不可改，带stage的模型运行还必须匹配stage输入摘要和as_of。

`publish_release_stage` 在同一 slot 锁下同时要求：

- stage_generation 等于最新已签发代次；
- 固定 base_pointer_generation 与 current pointer 一致；
- 固定 base_release_id 与 current release 一致；
- 新 release 仍属于此任务、处于 BUILDING，as_of 没有后退。

最新任务失败时保留前一个完整发布包；不会为了“看起来总有最新结果”自动让已被新任务取代的旧worker发布。需要重算时签发一个新的、可追踪的stage。重试一个已经成功的stage必须使用相同event_id和release_hash，否则报冲突。

### 2.2 正确的提交顺序

分析、模型调用、对象上传都在数据库发布事务外完成。短事务按下面顺序进行：

1. 建立 BUILDING release；调用 `begin_release_stage` 获取固定基准。
2. worker 在此stage内产生不可变结果引用；组件仍绑定原输入闭包。
3. 在发布事务中写齐 BUILDING release 的必需组件与排名，完成合同、PIT、有效期及输入闭包核验。
4. 调用 `publish_release_stage`，得到发布记录；函数将包改为 READY 并更新指针。
5. **同一事务内**写 `ops.outbox` 的 `decision.published` 事件，使用发布记录的 event_id、slot_key、pointer_generation，payload 至少匹配 release_id 和 stage_id。
6. COMMIT 时，核心完整性触发器和publication/outbox延迟检查共同执行；缺组件、缺outbox或身份不符不能提交。

发布函数不会自行发网络消息。Outbox publisher 在提交后发送，消息重试与 consumer_inbox 语义沿用核心专项。业务发布事实保留在 release_publication，不依赖消息流或outbox的清理期限。

运行角色应仅通过协调/Registry服务使用这些入口，不能给一般worker任意更新slot指针的权限。种子没有替部署创建身份与GRANT策略；实际权限、双会话竞争和崩溃恢复仍需数据库集成验收。

**回滚边界：** 普通stage仅支持前向发布。把已 READY 的旧release交给普通stage不构成回滚功能。本轮尚未实现独立rollback命令/事件；实施时应追加明确的补偿发布事实、原因和指针代次，而不是修改旧分录或伪造新任务as_of。

## 3. 历史可见性与真实可用时间

原始/规范记录的 available_at 始终表示实际处理完成并可用的时刻。回测不得把它改成多年以前的预测时刻。observed 模式按真实记录可见性过滤；reconstructed 模式由版本化 AvailabilityPolicy 在查询/manifest中派生 policy_usable_at，并保存来源时间依据、延迟假设和限制。

EvaluationContext 存 availability_policy_version 及其规则引用/假设文档。API可以返回可选的派生 policy_usable_at，但它没有替代任何数据库里的实际 available_at，也没有另建一套“推测事实库”。不知道源实际发布时间或覆盖时，应保留未知并说明可重建范围。

19维 FeatureKey 与共享特征参考保持相同语义：规范化序列化而保留 instrument_ids 声明顺序。只有特征定义明确声明集合无序时才能稳定排序，该规则进入 definition_version。生产名称统一为 instrument_ids、timeframe、adjustment_mode、universe_policy_version；旧别名仅存在迁移说明中。

## 4. 报价、工作台与可行动排序

### 4.1 报价事实与新鲜度

CanonicalQuote 包含实际来源与端点映射、稳定证券、未复权 last/bid/ask 价、可空的源时刻、first_seen_at/available_at、挂单数量和质量。没有L1能力就返回null，不能用日线close填成实时价格，也不能把最近一次HTTP200时间伪装成源行情时刻。

quote_snapshot_id 固定行情事实；live_quote_version 仅在同证券、同 quote_policy_version 的序号空间中比较。QuoteSnapshot 的 freshness_state 按 freshness_evaluated_at 派生；同一份旧报价随时间变 stale 不表示市场产生了新报价。Actionability始终引用明确quote_snapshot_id和自身checked_at。

`market.quote_projection` 和 `market.bar_projection` 都是可重建热投影，**不是规范行情的最终事实库**。不可变对象/列式快照和manifest是事实依据。读服务必须通过经过验证的canonical artifact、hash和完整范围登记来使用缓存；不能只凭“热表有几行”宣布快照完整、或信任任何更新后的缓存行。缓存可清理重建，源修订通过新规范观测和新manifest表达。数据库种子没有把热缓存DML等同于不可变事实写入保护。

图表已有 `feature_result(feature_id=bars.window)` 的READY窗口登记。报价读服务需要相同的验证纪律：从指定READY manifest的canonical报价记录重建/核对热行，绑定来源、证券、quote_policy_version与版本；校验失败丢弃缓存并重建，不改变原manifest。该物化/核对服务仍是实现任务，本轮没有假称已经跑过真实报价更新。

### 4.2 冻结分析与实时叠层

Workspace.projection_hash 只绑定持久化的冻结分析核心。冻结artifact中的 quote_overlay 与 actionability_id 为null；API可以叠加独立当前报价/可行动快照，但不能重写原projection_hash、release component或历史排名。ResponseMeta.generated_at 是本次响应时间，不是新的分析发布时间。

release中的 action_rank 是发布时上下文中的排序。当前账户条件下的 account_action_rank 存于 ActionabilitySnapshot，固定 account_version、quote_snapshot_id、政策、checked_at/valid_until。分页固定actionability_id；报价或账户变化产生新叠层，不能在翻到第二页时偷偷换排序基准。

买方向检查可用现金、权限与订单资源，卖方向检查可卖库存；叠层中的eligible不是已成交或已经预留。提交真实/模拟订单时仍须由统一执行协调器在账户事务内做最后检查。

## 5. 研究协议与策略晋级

paper_revision区分元数据、摘要、指定全文章节、全文、代码审阅和实际复现。full_text_sections必须在method_card记录读过哪些章节；部分章节核验不能被写成通读全文。论文作者的结果与本站实验结果分别保存。

假设先预登记可证伪命题、目标、期限和输入要求。EvaluationContext先创建未封存对象，再写精确evaluation_input快照，最后设置sealed_at；只有所有输入已发布才能封存。封存后不能追加输入或改协议，实验只引用sealed环境。

ExperimentRun 固定实例、假设、比较环境、代码、随机种子和清单；输入不允许原地换。指标在实验完成前写齐，再标completed；completed运行与指标不可改。一次错误复现应保留为失败结果，新参数或定义另开运行。

联赛只安排可比目标中的研究预算。配对验证同一evaluation_context、同一实例的completed实验，并拒绝把blind_final作为每轮反复比较的数据。每个实例在一轮最多出现一次；已结束轮次不能通过删除配对、搬到另一轮或重新开放状态来改历史。

PromotionDecision记录主验收实验、逐门槛依据及from/to状态。它不是一个仅凭Swiss排名自动激活策略的开关。StrategyRegistry在应用时仍需检查政策、预算、多重比较、影子观察、所需盲测与当前生命周期，并与outbox同事务CAS更新。策略家族DAG的完整无环检查和统计门槛不是几个外键可以证明的事项。

## 6. 合同、命名与字段来源

OpenAPI 3.1.0是本种子的生成器兼容目标，不声称它是最新规范。JSON Schema采用2020-12。结构验证包括金额字符串的26位整数/12位小数界限、UUID证券身份、UTC输出、五种TradePlan状态、四种freshness、字段质量以及条件树。

金额、价格和账户收益跨API采用十进制字符串；份额采用安全整数。研究浮点矩阵的数值政策需要单独声明，不以JSON浮点替换账本金额。条件树使用类型对象Clock，bar_close保留周期/会话，FeatureOperand是可解析的语义引用；initial_feature_key_hash仅锚定编译解释，后续触发必须按解析政策得到新的明确FeatureKey，不能永远比较旧MACD。

AccountProjection直接映射唯一Ledger：settled现金、交易应收应付、股息应收、已实现毛收益、未实现损益、股息收入、交易费用、税和外部资本。`pnl_from_cost_origin_cny`用于成本起点净值桥接，不自动等于区间TWR。历史account_version必须按事件/版本投影重放；不能简单过滤当前position_projection.applied_sequence就假装得到历史持仓。

SQL列名到API展示名的显式例子：

| SQL/唯一领域结果 | API字段 | 转换性质 |
|---|---|---|
| account_head.current_sequence | AccountProjection.account_version | 同一序号的领域名称 |
| position_projection.quantity | AccountPosition.quantity_shares | 明示股数单位 |
| position_projection.sellable_quantity | AccountPosition.sellable_quantity_shares | 明示股数单位 |
| position_projection.reserved_sell_quantity | AccountPosition.reserved_sell_quantity_shares | 明示股数单位 |
| Ledger.snapshot.dividend_income | dividend_income_cny | 同一内核结果，十进制字符串 |
| Ledger.snapshot.pnl_from_cost_origin | pnl_from_cost_origin_cny | 明确成本起点，不混作区间回报 |
| journal/trial_balance + CashUsePolicy | cash_available_cny | 统一政策投影，不是另一个现金资产科目 |
| quote_projection.source_event_time | CanonicalQuote.source_event_time | 真实源时钟，null保留 |
| market.feature_result.full_key | FeatureKey | 完整19维规范对象，不只给hash |

core.contract_version先登记完整v3 bundle，再登记需要单独引用的响应/参数合同；组件合同可使用指向已登记bundle的URN引用。SchemaRegistry在本地注册该bundle并解析，不在每次请求时上网寻找URN。core.api_binding按api_bindings.json登记operation_id、路径、应用服务、响应合同和读取模型；生产API不能把这张登记表当作绕过领域服务的自动CRUD生成器。

PUT/POST的Idempotency-Key由principal+operation作用域限定。ops.command_receipt和业务更新、outbox同事务保存已提交响应；同键不同请求摘要报冲突。乐观expected_*_version防多端覆盖，幂等receipt解决提交成功后响应丢失的重试，两者各有职责。保留窗口须在服务政策中公布，不能假装无限保存。

## 7. 验证与明确待实现项

可重跑命令：

```bash
python integration/build_contracts.py --contracts-dir contracts
python integration/validate_integration.py --contracts-dir contracts --schema-root .
```

依赖见requirements-validation.txt；环境不带包时在独立虚拟环境安装。研究工作区本次使用的pglast与OpenAPI验证包是临时依赖，交付不依赖它们的绝对路径。

validation.json保存实际数量、SQL哈希、结构校验、FK目标与API表名检查。pglast不能在无catalog时解析部分自定义域/复合行类型函数，记录中单独列出这些函数；没有用替换类型的“伪解析”冒充实库通过。所有DDL仍需在PostgreSQL执行建库、升级、两个并发会话和故障注入后才能验收。

标为seed-extension-point的策略参数、主张对象、社区分析、编辑器文档等仍需各自具体JSON Schema。认证主表与所有权、源接入、真实交易日历与费用、模型供应商、报价物化服务、推送游标保留/恢复、显式指针回滚以及生产负载均未由这些文件实现。它们已映射到领域入口和验收要求；不会因为Schema存在就把功能状态改为implemented。
