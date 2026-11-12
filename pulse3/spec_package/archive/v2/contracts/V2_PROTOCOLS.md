# v2 跨模块协议与实现边界

本文件补充definitions.schema.json的37个种子定义；原Schema和fixture仍按其已有范围验证。下列新协议是实现规格，尚未全部加入可执行JSON Schema，不把文本字段表算作机器已验证。StrategyBlueprint是StrategySpec的定义角色；实现时应扩展/统一StrategySpec，不创建两份竞争的策略真相。StrategyInstance引用该定义的固定版本。

## 1 共同基础

所有金额使用Decimal或十进制定点字符串，现金与价格必须附币种和单位；数量按品种用整数股或明确单位的Decimal，不能在同一字段混用股和手。时间采用有时区UTC存储，展示按交易所时区，交易日来自日历。所有版本引用都有id/version，所有结果都能追溯输入快照和方法版本。

空值表示未知；成功空结果、输入不满足、数据不可得、供应商失败、解析失败和模型失败是不同状态。所有跨模块方法必须定义这些状态，不统一返回空数组。

| 类型 | 核心字段 | 不变量 |
| --- | --- | --- |
| SourceClaim | claim_id、source_id、origin_group、content_hash、source_anchor、published_at、first_seen_at、revision、claim_type | 引用可定位到原文；解析推断和事实分开 |
| ReconciliationResult | reconciliation_id、observation_ids、normalizations、tolerance_policy_version、status、chosen_observation_id、reason | 未对齐单位/时点不判matched；同源转载不增加独立事实数 |
| DatasetRef | dataset_id、snapshot_id、manifest_version、availability_watermark、content_hash、row_count、storage_ref | 只读取READY版本；结果引用不随latest改变 |
| FeatureKey | 定义/实现/参数/依赖闭包摘要、数据快照/修订/schema、证券范围、股票池、周期、decision_as_of、availability_policy、日历/会话/复权/规则/数值政策版本 | 相同键具有同一确定性计算语义；参数规范化版本入定义 |
| FeatureResult | key、status、value_ref、quality、dependencies、computed_at | 失败与成功null分开；依赖修订产生新键 |
| StrategyBlueprint | strategy_id/version、role、horizons、input_requirements、output_type、state_schema、reason_ids、evaluation_protocol | role决定输出语义；不能同时伪装成选股和执行回报 |
| StrategyInstance | instance_id、definition_ref、parameters、universe_ref、account_scope、sleeve_id、policy_refs、state_ref、stage | 发布后不可变；调参形成新版本，旧实验仍可复现 |
| SignalProposal | signal_id、instance_ref、instrument_id、decision_time、horizon、forecast_target、distribution、valid_until、evidence_refs、input_refs | 不携带已执行成交；不同预测目标不得直接排序 |
| OverlayProposal | proposal_id、instance_ref、base_plan_ref、base_account_version、relative_target、inventory_rights、expiry、invalidation | 只有配置的库存/资金权利；旧账户版本需重新编译 |
| PortfolioTarget | target_id/version、account_id、base_account_version、positions、cash_target、contributions、constraints、policy_ref | 同账户物理目标唯一；目标先净额，之后产生订单 |
| Reservation | reservation_id、order_id、account_id、instrument_id、side、remaining_qty、remaining_cash、status | 每个预留只属于一个订单；部分成交按剩余量更新 |
| GridDefinition | grid_id/version、instrument_id、spacing_kind、step、anchor_ref、bounds、per_level_budget、core_floor、cash_floor、reanchor_policy | 事件触发不等于成交；重锚不得丢弃未完成腿 |
| PaperMethodCard | paper_id、canonical_id、version、venue、publication_status_code、publication_status_note、read_depth、method、assumptions、reported_results、reproduction_status | 状态码用于筛选，原始说明和日期另存；作者结果不是本站结果 |
| ReasonRecord | reason_id、requirement_id、claim_refs、decision_ref、component/symbol、alternatives、invariants、tests、owner、status | 未验证状态不得被生成器改成reviewed |

## 2 端口和调用方向

DataQueryService负责证券主数据、行情、公告、财务、公司行动和宏观vintage查询。研究与决策调用必须同时固定snapshot_id、decision_as_of和availability_policy；只有可证明已物化为指定as_of可见视图的数据集，才可由manifest继承后两者。冻结快照可能包含未来修订，不能因此跳过逐时点可见性过滤。探索页面可以显式请求latest，进入研究或计划编译时固定具体版本和时点。

FeatureService.resolve(request)返回FeatureResult或可追踪任务id；只计算依赖闭包。同一数据被新闻Agent、选股策略和研究Worker使用时，读取同一数据服务与目录，不各自拉一份来源网页或重新下载行情。

IntelligenceService.analyze(evidence_bundle, analysis_session)返回ImpactProposal/PlanProposal。DecisionService.compile(proposals, portfolio_snapshot, rulebook)形成TradePlan与PortfolioTarget。ExecutionService.plan_orders(target, account_snapshot)产生OrderIntent；ExecutionService.apply_report(report)记录真实或模拟回执。ResearchService.run(hypothesis, release_bundle, dataset_refs)返回可复现ExperimentRun。

API是这些服务的投影入口：GET /v1/market/state、POST /v1/screeners/query、GET /v1/stocks/{id}/workspace、GET /v1/plans/{id}/versions、GET /v1/portfolios/{id}、POST /v1/experiments。模型工具复用相同应用服务，不再建一套计算结果不同的MCP逻辑。真实路由命名可以统一调整，但唯一业务实现不可复制。

## 3 事件订阅语义

市场、信息、决策和账本事件按领域组织stream，每个独立下游模块拥有自己的durable consumer。同一个模块的多个Worker共享其consumer以分担任务；不能让选股和新闻模块误用同一工作队列并互相抢走对方必需事件。

消息采用显式ack。成功业务事务提交后才ack；处理失败保留可重试状态。传输的去重窗口不是永久业务去重，inbox唯一键与聚合版本负责业务幂等。影响同一账户的订单/成交按account_version处理，发现版本间隙先补齐，不能静默越过。

主要事件包括ObservationIngested、DatasetReady、BarFinalized、BarRevised、EvidenceEventRevised、FeatureReady、SignalProposed、PlanActivated、PortfolioTargetChanged、OrderAccepted、OrderCanceled、FillBooked、ExperimentCompleted、StrategyStageChanged。EventEnvelope的payload schema按event_type注册；首版现有两种Envelope样例只是起点。

实时消费者、历史回放和修订重算使用独立consumer和checkpoint。历史回放禁止重新向生产发布订单，只把相同策略和执行规则接到回放账户。流的保留容量、复制和清理按数据恢复需求实测配置；达到容量时有显式处理状态，不能默默把Fill当可丢报价。

## 4 生命周期与恢复

SourceCursor只在原始数据持久化后推进；规则完成和模型完成分别记录。DatasetManifest经历INGESTING、COMMITTED_UNPUBLISHED、READY或FAILED。Lake快照成功而业务发布失败时，按batch_id恢复同一版本，不通过盲重试制造重复批次。

StrategyInstance按第6节唯一生命周期转换，事件包含依据ExperimentRun与policy_version。TradePlan.state沿用种子Schema的watching、actionable、holding、superseded、expired；draft与compiled描述提案编译阶段，不是计划状态，plan.activated事件也不新增active枚举。替换计划不删除旧订单或成交。网格腿和做T周期单列operation_id，可以跨计划版本延续或按明确政策结束。

## 5 参考实现范围

reference_models/feature_store.py验证单进程异步共享、完整键、失败重试与取消隔离；account_model.py验证单标的现金/库存、订单预留示例、目标净额和做T相对持有的总净值。它们是独立编写的行为样例，未导入旧项目业务代码。

完整实现还需要：PostgreSQL事务与多进程竞争、NATS投递和回放、DuckLake发布恢复、实际日历与公司行动、券商回执去重、资金交收日规则、逐笔成交模型、真实数据源与Web集成。参考测试通过只证明其声明的子集，不替代这些集成验收。


## 6 唯一命名与状态来源

时间字段以主规格第3.1节为准：first_seen_at表示首次获知原始版本，ingested_at表示原始材料持久化，available_at表示处理结果可用；不另设语义相同的first_observed_at。EventEnvelope使用recorded_at表示领域事件持久化，payload承载小字段及内容引用，不把payload_ref平铺成竞争字段。公共生产者字段为producer_version。

现有event_type decision.published、plan.revised及其JSON Schema保持不变；新增market.bar.finalized、market.bar.revised、event.revised、plan.activated、fill.recorded等按注册表扩展。文中CamelCase是概念名称，线协议统一为小写点分event_type，NATS主题由pulse.{event_type}.v{schema_major}唯一生成。API前缀及资源命名以主规格第11节为准，新增接口不得私建第二套/stocks与/instruments同义资源。

StrategyLifecycle唯一枚举：discovered、specified、data_ready、screened、validated、shadow、active、paused、retired。implementation_status、reproduction_status和health分别描述实现、复现及健康，不改变生命周期含义。paused表示按已发布政策暂停运行；degraded只是health值，不是另一种生命周期。


跨进程FeatureJob使用lease_epoch/fencing_token及唯一结果登记：健康并发尽量单算；故障允许重算，旧Worker无权发布覆盖结果。FeePolicy含aggregation_scope、minimum_commission、rounding_policy、effective_range及已收费累计值；费用是否已包含在fill_price或独立fee字段必须可判定。


唯一决策调用顺序为SessionCoordinator → PortfolioCoordinator → PlanCompiler；触发实际订单时由ExecutionCoordinator做最终资源检查并原子预留。计划激活、资源预留和委托成交是三个不同事实；未预留的未来条件计划不得在页面保证立即可成交数量。新缓存实现必须对应reference_models/feature_store.py::FeatureKey的完整语义，并对新增影响因素扩展版本闭包。
