# 数据库与来源映射的独立冷审

审查者：统一执行与记账专项 Agent。日期：2026-11-10。

直接审查 `database/core_schema.sql` 与 `database/source_mapping_examples.json`，为确认板块排行的承载位置，另外读取 `sources_sectors/sources_sectors_schema.sql`。本审查未修改上述文件。结论来自DDL逐段阅读、独立合成数值检查及官方接口文档复查；没有启动PostgreSQL，因此不把静态推断称为已执行的事务测试。

## 结论

基础设计能承接“统一来源身份—原始材料—规范观测—数据快照—发布包—排行与个股工作台”的主线。复核确认原始版本、端点单位、缺失值、来源独立性和当前健康/新鲜度已区分。冷审发现的四处具体跨表一致性缺口已经反馈作者，作者修订后再次读取DDL确认了约束方向；仍需在真实PostgreSQL完成触发器、事务与并发验收。

这是一份有明确生产门槛的schema种子。它没有完成全部供应商适配、时点查询服务或持久事务实现，不能用“DDL可以解析”替代这些功能的完成状态。

## 1 已确认的设计优点

| 对象 | 确认结果 | 实际意义 |
| --- | --- | --- |
| source_endpoint | 映射按source、endpoint、source_schema_version、mapping_version定位 | 同一个供应商的daily和rt_k不共享错误单位配置 |
| raw_object / batch_member | 原文按来源业务键加内容摘要保留修订，重复批次可以重新引用同一原文 | 不需要覆盖旧证据来处理供应商更正 |
| observation | 事件发生时间与可得时间分别存储，并保留原始对象、字段状态和转换版本 | 为时点重放和字段级追溯提供基础 |
| 数值与缺失 | SQL有限十进制域排除NaN/Infinity，JSON金额约定字符串，未知值不补零 | 数量级、精度和“未知/无成交”不被混淆 |
| dataset_manifest | 快照身份不可用latest代替，READY后主行不可变 | 修订生成新快照，旧实验保持确定输入 |
| release | 必需组件完整性在提交时检查，发布后组件引用不可变 | 排行、计划、图表和证据可以读取同一个资料包 |
| outbox / inbox | 事件有稳定ID、内容hash、聚合版本；消费去重按consumer+event | 为至少一次投递的幂等业务效果提供持久支点 |
| 分类与行情 | 分类体系保留提供者/版本；行情按交易日分区，板块成分带时点 | 不把不同分类来源强行揉成一个无来源标签 |

## 2 冷审发现与修订状态

### DBR-01：已发布快照的依赖闭包可以被改写

初读时，READY保护仅作用于dataset_manifest主行；dataset_dependency仍可追加、删除或修改。在snapshot_id和content_hash不变时改动输入依赖，会改变结果解释、失效传播及重放输入闭包。

作者补充 `market.guard_ready_dependency`：修改依赖时锁父manifest，禁止对READY快照增删改依赖，并禁止把依赖行移到另一快照。另补延迟检查，READY快照的直接输入必须READY。**再次静态阅读确认修订方向正确。** 后续数据库验收须覆盖并发发布和依赖写入、发布前补齐依赖、发布后拒绝改动。

依赖图整体无环仍是另一个应用层门槛；“输入READY”本身不能证明没有在同一事务里互相依赖后共同READY的循环。

### DBR-02：股票排行可以关联另一证券或另一发布包的计划

原FK只证明plan_id/plan_version存在，没有证明该计划属于ranking_entry.instrument_id，也没有证明其based_on_release_id与排行的release一致。这会出现列表中的A股点进去拿到B股计划，或旧发布包的计划被混入新排行。

作者补充 `decision.guard_ranking_entry` 核对证券与发布包身份，并冻结已发布排行run与entry；TradePlan版本改为追加式，不允许原地修改或删除。**再次静态阅读确认修订。** PostgreSQL验收应尝试错误证券、错误release、已有READY发布包新增/修改entry，以及正确的BUILDING→组装→READY事务。

该约束要求编译和排序在共同发布包组装阶段完成；不能先把发布包设READY，再绕开规范服务补排行条目。

### DBR-03：账户计划与账户版本允许单独为空

原trade_plan中account_id和expected_account_version各自可空，可能出现“账户计划没有编译时账户版本”或“公开计划带无对应账户的版本”。作者增加两者成对为空/非空的约束。**已静态确认修订。** 触发实际委托时仍需和当前账户版本比较，成对存在不代表版本仍然有效。

### DBR-04：开户序列0与outbox版本限制不兼容

统一账本参考将opening定义为账户sequence=0，原outbox要求aggregate_version>0。若AccountOpened采用账户序列，初始事件无法写入。作者将outbox允许版本0。**已静态确认修订。** 后续消费程序同样要允许这个初始值，不得把0当成空值或未初始化来丢弃。

### DBR-05：板块实体排行承载位置

仅看core表，ranking_scope允许sector_overview，而ranking_entry的主对象固定instrument_id，初看有承载缺口。跨文件复核后确认扩展已经提供 `decision.sector_rank_entry`，其对象为taxonomy/version/node；因此这项不记为“功能缺失”。核心现已拒绝将instrument entry写进sector_overview。

扩展作者随后补充 `decision.guard_sector_rank_entry` 与 `guard_sector_rank_parent`。本审查再次逐段读取：锁父release、禁止READY/SUPERSEDED条目增删改、父scope为sector_overview、taxonomy/version/membership一致、单run同rank_kind，以及“先插行后改父run”路径均有对应判断。**已静态确认补充。** 实库应验证同run并发写入不同rank_kind，并明确事务隔离级别；仅有父行锁不能代替对隔离语义的测试。快照READY、PIT成员可得性与覆盖度仍属于发布服务门槛。

## 3 来源映射的独立核对

独立读取三个mapping profile，对其中 **25个数值和日期转换** 重算，与canonical_expected全部一致；三个证券身份解析操作依赖时点别名库，没有伪装成已经执行。合成fixture的算术通过不等于供应商接口已接入。

| 接口 | 独立核对 | 结论 |
| --- | --- | --- |
| Tushare daily | 100.25手→10025股；100.25千元→100250元；2%→0.02 | 与映射一致 |
| Tushare rt_k | 10025股与100250元保持原单位；123笔保持整数 | 不能复用daily乘100/乘1000的映射 |
| AKShare stock_zh_a_hist raw | 101手→10100股；101000元保持；1.25%→0.0125 | 合成算术一致；本次官方网页直连未成功，未增加“独立全文阅读已完成”的声明 |

重新打开Tushare官方daily与rt_k文档，核对了daily的vol为手、amount为千元，rt_k的vol为股、amount为元；daily pre_close是除权参考口径，rt_k trade_time是可选快照时间。官方页面：

- https://tushare.pro/document/2?doc_id=27
- https://tushare.pro/document/2?doc_id=372

上述页面是字段文档，未发起实际行情接口请求。

空值与异常样例方向正确：null不能当0，手转股得到非整数不能默默四舍五入，复权数据不能进入raw表，源缺失的发布时间不能由采集时间冒充。盘中累计数据与日线终结数据必须先对齐时点和会话，数值相同也不能自动视作同一观测。

rt_k若没有trade_time，应先作为明确缺失源事件时间的observation保存。若发布到需要bar_open_time/bar_close_time/trade_date的行情投影，必须有经过验证的会话归属规则；不能因为请求发生在今天，就把可能陈旧的源返回当成今天的新bar。

## 4 应用层与实库验收仍要完成的具体门槛

这些是种子已声明范围之外的实现要求，不把它们写成已经运行失败的程序漏洞。

1. **版本内容不可原地改变。** contract、mapping、raw、observation、strategy.definition等带版本身份的记录，其不可变字段须由统一写入服务/权限/触发器落实。修改映射但不改变mapping_version，或修改定义而保留definition_hash，会破坏复现。TradePlan本轮已补数据库保护，其余版本对象仍需相同的更新政策和验收用例。
2. **游标要指向同一来源范围。** source_cursor.committed_batch_id必须属于相同source/endpoint/schema/mapping/partition，且批次RAW_COMMITTED或NORMALIZED；batch_member的raw同样要对应正确源范围。普通UUID外键只证明行存在，不能证明这些关系。发布事务不能推进到未持久化原文或另一个分片的batch。
3. **可得时间必须有真实根据。** observation.available_at应遵守原始first_seen/ingested、规范化完成与发布政策，不能从历史event_time或日期倒填。严格实见模式与使用公告发布时间的研究模式分开标识。PIT过滤在每一条源观测和全部派生依赖上执行，不只检查manifest文件的创建时间。
4. **时点版本选择需要确定规则。** instrument_alias/state/tag允许修订及时间区间，不应简单加一个全局排斥约束禁止合法修订；查询应在available_at截止下选取适用版本并检查同权威版本的冲突。分类父子关系和派生依赖需检查间接循环。
5. **去重记录和业务效果同事务。** inbox PK不能单独证明exactly-once效果；同ID不同payload_hash应报冲突，先写inbox后业务失败必须整体回滚。消费者只能推进连续水位，不能用并行完成的最大序号掩盖缺口。
6. **SQL语法、触发器行为与性能是三件事。** 种子SQL可解析，尚未在目标PostgreSQL版本运行；必须验证延迟约束、行锁并发、事务回滚、分区创建、索引查询计划和目标规模的资源开销。

## 5 审查证据

本次已执行：JSON读取、3份mapping profile的25个独立算术/日期断言、核心及扩展DDL逐段静态审查、作者修订后的相关约束复读、Tushare官方文档复查。

本次未执行：真实数据源调用、证券身份数据库解析、PostgreSQL建库/触发器/并发测试、端到端API一致性与性能压测。最终文件摘要由后附审查记录给出，不将并行修改期间的摘要冒称最初版本。

最终复读版本摘要：

```text
database/core_schema.sql
4cca1227a64fda926854ce22539d18e92187839e4fbdda861432abf28a8cfd9e

database/source_mapping_examples.json
17a5f776bf91d43b91ff3f18e6512af5d6de63134b4632804cca8dd5ffc5bccc

sources_sectors/sources_sectors_schema.sql
9c150cba150b2ad40c87c65a30b483dea415da879a52f1799b9f5cf1d3f752b2
```
