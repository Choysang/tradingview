# v3.1 严格 payload 与有限语义参考：独立冷读

日期：2026-11-11。审阅对象：`contracts/supplemental.schema.json`、`extension_bindings.json`、`reference_extensions/semantic_validation.py`、`test_semantics.py`，以及 50 项 `proposal_decisions_v31.json`。

本报告区分三件事：结构 Schema 能拒绝的值、有限纯函数能证明的语义，以及尚待 Repository/数据访问层兑现的真实身份和时点关系。未连接数据库或真实图层数据，未解析任何原始评论。独立合成反例脚本与当次内容哈希见 `extension_independent_review.py`、`extension_independent_results.json`。

## A. 已发现并由主实现修正的具体问题

### A1. 概率出处的 ordinal 不具有全局身份

最初 CommunityForecastPayload 仅有 `source_evidence_ordinal`。原 v3 SQL 明确 `intelligence.claim_evidence` 的主键为 `(claim_revision_id, document_revision_id, evidence_ordinal)`；两篇文稿同为 ordinal=1 是合法状态。因此只按 claim+ordinal 查找时可得到两条不同概率的证据。

修正是在 payload 增加 `source_document_revision_id`，Repository 按完整三元组定位。这个修改是必要的身份补齐，未新增重复文稿表。独立脚本保留两篇文稿同序号的合成例，说明旧指针为何含糊。

### A2. “成本分布估计”可以伪装成实测

最初 `cost_distribution_estimate` 可以用 `derivation_kind=observed`、`assumptions=[]` 通过。修正后必须保持 estimated，且存在假设。X01 已验证拒绝。

### A3. 社区图层只靠分钟 K 线即可声称有数据

最初 `layer_kind=community` 配置 `required_capabilities=[minute_bars]` 可通过；修正后社区必须具有 `historical_community`，news/seat 需要历史证据，plan 需要已发布计划，竞价需要相应行情能力。X02 已验证拒绝。

### A4. bar 内分配被标为精确成交量分布

修正后，缺少 individual_trades 的 volume_profile 不允许假称确定的逐笔价格分布，必须声明 estimated。X04 已验证拒绝。这不否认估计算法本身可确定复现，而是准确区分测量对象：它对未知 bar 内成交分布作了估计。

## B. 后续发现并已修正：独立反例确认

| 反例 | 判断 | 应有处理 |
|---|---|---|
| X03：anchored_vwap 的 price_basis=not_applicable | 纯 payload 自相矛盾；价格水平必有价格口径 | AVWAP、volume_profile、cost_distribution_estimate 至少要求 raw/qfq/hfq；具体复权版本继续由 FeatureKey/输入合同绑定。 |
| X06：horizon_end_at 用 .000Z 和 Z 表示同一时点被拒绝 | 基础 Timestamp 允许两者，语义比较却按字符串 | 比较 `instant`；保留完整 forecast hash 用于原始 payload 版本绑定，二者职责不同。 |
| X07：任意 scoring_policy_version 仍按固定 Brier 量化规则计算 | 有版本字段但未绑定实际政策，不能诚实重放 | 参考实现只接受一个声明明确的政策 ID，或接收并校验已登记的评分政策上下文；不支持的版本拒绝。 |

最终独立重跑确认：X03 已拒绝；X06 的等价时点已接受；X07 的未支持评分版本已拒绝。Schema 只支持 `brier_binary_decimal12_v1`，没有默换算法。X11 验证缺少文稿身份会被拒绝；X12 验证有效预测与手算 Brier 仍可通过。

## C. Repository 上下文边界：不是当前纯函数已经完成的能力

X05 / X08 / X09 展示：目前 layer validator 接收全局 `verified_capabilities` 集合和 `(ref,hash)→available_at` 映射。这足以检查“调用者说有能力”及“输出不早于这些输入时间”，不足以证明能力属于本张图、本只股票、本个窗口和本次历史截止点。

- 若外部集合含 full_order_events，但本层唯一输入是 minute-bars artifact，有限函数仍会通过；它没有每个 artifact 的能力映射。
- 有效 available 图层的 window_end 可以晚于实际数据覆盖；函数没有 range completeness 或真实输入最后时间可检查。
- 改掉 ArtifactRef.contract_id/schema_version，只要 ref/hash 仍相同，也可能通过；当前上下文键未包含合同身份。
- 没有传入历史显示 cutoff，不能据本函数宣称“回放不会读取未来数据”。这个要求已经列在 extension_bindings，仍是 Repository 待实现事项。

最小生产上下文应由可信读取层构建：精确 artifact 身份（ref/hash/contract/version）、instrument/scope、每项能力、有效范围与覆盖状态、会话/价格基准/复权身份、实际 available_at。再绑定调用的 instrument/scope/release 与输入 cutoff/历史显示时点。能力从与当前图层匹配的 artifacts 汇总，不能拿另一个股票或另一日期拥有逐笔权限来补足本层。

`decision_as_of` 是输入截点；分析实际完成时间可能更晚。历史可见性还需区分“按真实已观测时间回放”和“显式研究重建政策”。不要为了满足回放显示，将今天计算的 layer.available_at 改写为历史数据发生时间。

参考只校验传入的已验证上下文；生产必须完成 `input artifact → capabilities + instrument_ids + window + available_at` 的绑定。该任务明确尚未实现，不能把三个演示的 accepted 解释为生产正确。

这是一项真实集成边界，不是要求本次补丁造新数据库或补全数据采集。交付可以维持有限参考，只要文档、测试名与可用状态清楚说明：提供了合同及边界示例，尚未实现可信 Repository。

## D. 其余五类 payload 检查

1. **CommunityForecast**：概率缺失保持 null，不能由看多文字推成 0.8；结构合法还需实际原文对应。概率的出处定位按新增文稿 ID+ordinal 严格对应当前 claim 版本。`forecast_as_of` 的语义应明确为平台使用该预测的时点边界；作者发帖时间仍在源证据中记录，不把后收集的历史评论伪装成当时已被系统看到。
2. **CommunityOutcome**：独立结果产物绑定确切 forecast hash、target hash、claim revision；resolved 只允许二元标签及成熟时点。真实目标内容、label 的原始数据与快照范围仍由 Repository 检查，不由 hash 存在本身证明。
3. **ConvertibleRelation**：关系与转股价/赎回条款分开是正确选择。当前函数只比较调用者提供的 instrument types 与有效区间，未证明这些实体/关系真的存在或在历史时点已披露；绑定说明不应扩大。
4. **MacdProfile**：EMA seed、hist倍率、闭合 bar 和 pivot右侧确认已明确。最少 bar 数是本项目 profile 政策，不是普遍收敛定理；实际数值计算、取样与 pivot 确认时间并没有在这段参考里实现。
5. **Decimal**：主实现已将 Context 的精度、舍入、指数范围及 traps 显式固定。与调用者 Decimal 环境隔离的方向正确；仍只验证 Brier 算术，不因此证明原始概率可校准。

## E. 对 50 项决策与论文的冷读

P18、P24、P27—P31、P37—P42 与本次方法研究结论一致：新增条目保持候选；RFD 不强行等于陷阱，排名变化不替代行动，CYQ 不当实测，自动前 20% 晋级被取消。

P41 准确区分 WWW Companion、CIKM2022、FinMem2024及未定位 Stock-Pretrain；P42 没有把终值财富比较保证写成最大回撤保证，也没有把 Hawkes 做市论文写成 A 股未来高低点预测。未发现这份决策矩阵扩大论文已读深度、冒称复现或宣称已有盈利验证。

## F. 验证计数与精确表达

本轮独立脚本共 13 项探针：10 项针对已实现边界的校验（含 1 项完整证据身份唯一性演示）全部符合预期；另 3 项用于明确尚未实现的 Repository 能力/范围/合同绑定边界，不能并入“生产校验通过”数量。每项结果及被审文件内容哈希保存在 JSON 中。

可说：严格 payload 已登记，有限语义函数和合成反例检验了概率缺失、目标版本、标签成熟、估计标识、基本图层能力和 MACD 参数约束。

不能说：已完成原文真实性核验、历史社区数据接通、逐笔能力/范围认证、可信 Repository、真实回放或生产买卖点验证。后者需要真实输入和实现，不由合成 fixture 推导。
