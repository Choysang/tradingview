# v3.1 增补最终规范的独立契约审查

审查日期：2026-11-11。审查者：v4_contract_review。

范围：`deliverables/Pulse_Rewrite_Package_v31_build/spec/ADDENDUM_V31.md`、`spec/PROPOSAL_DECISIONS_V31.md`、`contracts/extension_bindings.json`、`contracts/supplemental.schema.json`；对照实际v3 Schema/OpenAPI/SQL及本次ASOF参考。未修改被审文件。本报告不重复金融论文、旧仓库研究或真实供应商验证。

## 结论

**主干与增补方式可接受；下面三个契约/时间问题应在定稿前明确，两个措辞问题一并修正。** 新功能复用现有JSON/artifact扩展点成立，不需要增表、增HTTP操作或重建账本。

## 实际核对通过的事项

- `contracts/v3.schema.json`、`contracts/openapi.json`、`contracts/api_bindings.json`及四份SQL，与既有v3交付字节完全相同。
- `integration/all_fields.json`亦字节不变，记录 **80表/883列**；公共Schema实际 **142个defs**，OpenAPI实际 **30paths/31operations**。
- 新增业务Schema恰为五个defs：CommunityForecastPayload、CommunityOutcomePayload、ConvertibleRelationPayload、ChartLayerDescriptor、MacdProfile；ASOF唯一政策Schema独立位于reference_alignment目录，没有复制第二份。
- `getStockEvidence`实际响应是`data: Evidence`，其中claims的EvidenceClaim确有object_payload扩展位置；`getStockWorkspace`实际响应是`data: Workspace`，确有features[].artifact、chart_window_refs、features[].key.parameter_digest。
- `intelligence.claim_revision`的object_payload/claim_schema_version/subject_kind/subject_key/predicate均真实存在；`intelligence.claim_evidence`实际主键为`(claim_revision_id,document_revision_id,evidence_ordinal)`，增补加入精确文档版本消除了只引用ordinal的歧义。
- `market.feature_result`真实存在result_uri/result_hash/full_key/definition_version/computed_at；Macd参数artifact与full_key只存digest的责任在文档中有明确区分。
- `decision.workspace_projection.projection_payload/projection_version`真实存在；图层通过ArtifactRef而非给Workspace增加随意字段，做法成立。
- 新增内容继续区分实际数据能力、规范/参考实现、Repository待实现与真实运行验证，不将fixture冒称生产完成。
- ASOF代码副本SHA256为`8c0e1bb779e827a0a5045cc54ff38b003d72877a3be6d4f4f503c03db541e859`，与27作者测试、16独立边界检查通过的冻结版一致。

## FSR-01：图层价格基准枚举重新引入未映射别名

**位置**：supplemental.schema.json / ChartLayerDescriptor.price_basis。

**实际问题**：新增enum为`raw/qfq/hfq/not_applicable`；公共v3 `AdjustmentMode`是`raw/forward_adjusted/backward_adjusted`。文档要求统一复权身份，但未登记两套枚举的正式映射。新的`price_basis`属性可以有自己的用途；相同复权方式用两种值且无映射，会让图层与FeatureKey比较产生假不一致。

**建议**：直接引用base `AdjustmentMode`并额外允许`not_applicable`，或者在新合同中给出明确且唯一的映射并在语义验证中应用。前者最简洁，不影响公共142defs或HTTP。

**验证**：raw、forward_adjusted、backward_adjusted图层能与同方式FeatureKey比较；不能让未映射qfq/hfq穿过。非价格图层仍可not_applicable。

## FSR-02：claim_schema_version尚不能唯一分派到新增的两种payload

**位置**：extension_bindings.json 的 CommunityForecastPayload、ConvertibleRelationPayload；真实表`intelligence.claim_revision`。

**实际问题**：两种绑定分别有contract_id，但schema_version均为1.0，version_column都指向claim_schema_version。真实claim表只有claim_schema_version，没有contract_id列。如果据绑定把该列仅写1.0，Repository无法仅凭该值找到`core.contract_version`的复合主键(contract_id,schema_version)。forecast的predicate也未在绑定中登记成唯一分派规则。

**建议**：不改表，明确claim_schema_version保存带命名空间的合同token，例如`pulse.community_forecast@1.0`与`pulse.convertible_relation@1.0`。extension_bindings登记token→(contract_id,schema_version,definition)的精确映射及合法谓词/断言类型。若选择谓词注册表分派，也必须明确唯一规则，而不能留给不同开发包各自猜测。

**验证**：同为1.0的两类payload可以稳定解析到各自Schema；未知token、错误predicate/assertion_type与payload错配明确失败。无需添加公共属性或数据库列。

## FSR-03：原帖预测时点与平台实际可得时点应分别定义

**位置**：extension_bindings.json / CommunityForecastPayload repository_checks的`all source versions available at forecast_as_of`；ADDENDUM §4.1。

**反例**：09:45的原帖明确预测09:45–10:00走势，平台09:45:03才接收并规范化文档。若forecast_as_of为原作者预测时点，要求document.available_at≤forecast_as_of会拒绝一条正常晚几秒收到的预测。若反过来将forecast_as_of设成09:45:03，又会违反当前validator的forecast_as_of≤horizon_start_at，或被迫改写原预测开始时点。

**建议**：明确forecast_as_of是经原文版本证据核验的原预测信息截点；原预测必须早于其目标窗口并具备可核查版本证据。平台`document_revision.available_at`、`claim_revision.available_at`保持真实处理完成时间，只用于“平台当时可用于决策吗”的PIT。对于后来发现的历史原帖，可以保留并研究作者预测，但不得回填成平台过去已知，也不得默默混为前向收集样本。没有原文时间/版本证明时保留未确认。

**验证**：上述3秒晚到原帖可按明确规则记录，09:45:01平台决策不可读取；若是事后编辑或事后发现样本，不能冒充前向研究记录。不要通过改写actual available_at消除这个差异。

## FSR-04：预测“字节哈希”与实际规范化对象哈希措辞不完全一致

**位置**：ADDENDUM §4.1“原预测字节哈希”；reference_extensions/semantic_validation.py canonical_hash。

**实际行为**：canonical_hash先对Python对象执行sort_keys、指定分隔符等JSON编码后再hash，不是对任何已经保存的原始JSON字节直接hash。代码已明确不宣称RFC8785，因此正文也应沿用这一边界。

**建议**：称“按本项目规范JSON序列化政策生成的payload内容哈希”，并要求真实存储的规范artifact使用同一政策。原文HTTP/文档原始字节hash留给既有raw_object，不与payload hash混同。

## FSR-05：ASOF派生范围的两处措辞

**位置**：ADDENDUM §2。

- “参考对跨窗口股本变化采用显式拒绝”建议改为“对**窗口内**股本生效变化采用明确缺失结果”，因为代码检查分母effective_from是否晚于bar窗口起点，返回`DENOMINATOR_CHANGED_WITHIN_WINDOW`，不抛通用错误。
- 当前VWAP参考要求methodology_id/version、来源端点和snapshot等**完全相同**；若正文只说“兼容方法”，要说明生产兼容映射仍待定义，本参考没有实现自动跨源等价仲裁。

## 非错误但须保留的交付状态

- 61策略/105因素属于root正在合并的目录变更，本审查不把尚未合并的数量当作已验证完成；发布前应由目录验证器重新核对ID、labels、状态和数量。
- 新严格Schema提供结构约束，概率范围、时间顺序、引用真实性、能力与artifact绑定仍需要明确语义验证和Repository；正文已说明这点，可保留。
- 图层descriptor没有自行保存全部证券/数据覆盖字段，依赖manifest关联验证；正文明确此项待实现，因此没有据此判成已经具备完整回放能力。
- 规范没有把本轮ASOF速度当作数据库压测，也没有声称新增指标或策略实现盈利，可接受。

审查结果已通过消息发送给root。待上述问题处理后，可做针对变更点的短复核，无需重复整包旧审计。
