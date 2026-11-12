# FSR01–05 定向修复与复验

日期：2026-11-11。范围仅为v3.1 staging中的增补合同、有限语义参考和对应文档/决议；生成源`research_v4/build_extensions.py`、`build_review.py`同步修补。没有运行consolidate.py，没有改base schema、SQL、OpenAPI或账本。

## 已完成的五项修复

| 审查项 | 实际改动 | 为什么足够与仍有的边界 |
|---|---|---|
| FSR01价格口径 | ChartLayerDescriptor.price_basis引用base AdjustmentMode，并为非价格图层允许not_applicable。qfq/hfq被Schema拒绝。 | 不再新增无映射别名；真实图层与FeatureKey/输入artifact关联仍由Repository检查。 |
| FSR02主张合同分派 | extension_bindings给两个claim定义登记唯一wire_schema_token：pulse.community_forecast@1.0、pulse.convertible_relation@1.0；明确其定位core.contract_version复合键。新增resolve_claim_binding/validate_bound_claim有限参考，检查主体、谓词和断言类型。 | 现有claim_schema_version字符串列足以携带限定身份，不新增字段。未知/仅1.0/latest token不猜测。真实注册和SQL读写没有实现。 |
| FSR03预测与平台时间 | 明确forecast_as_of是原预测信息截点，实际document/claim.available_at仍是完成时间。新增forecast_known_at，以可信时刻验证平台何时知道该预测，晚到不改原payload。 | 09:45预测、09:45:03接收可正常保留，但不能供09:45:01决策读取；函数只验证所传时间，不证明原文真实性，也不等于可行动性。回顾样本单列仍由Repository实现。 |
| FSR04哈希口径 | 新增canonical_payload_bytes，canonical_hash对项目UTF-8排序JSON字节hash；文档改为规范payload内容哈希，与原文原始字节hash区分。 | 同一payload的字典键顺序不改变hash；没有宣称RFC8785或已经实现全部跨语言一致性。 |
| FSR05ASOF范围 | 正文和P13改为同证券、来源端点、映射、方法、快照、窗口等的严格匹配；窗口内股本生效变化返回明确缺失结果。 | 与冻结alignment.py实际行为一致，不扩充其生产能力或自动跨源仲裁。 |

预测claim的注册谓词为`community_binary_forecast`且assertion_type为forecast；债股关系谓词为`convertible_underlying_of`，允许的事实断言类型为official_disclosure、independently_reported、attributed_statement。这些是本次新增合同注册规则，不改写base assertion枚举。

已保留root此前在extension_bindings中增加的完整证据身份和逐artifact能力/类型/窗口/覆盖检查，并同步进build_extensions生成源，后续单独运行该生成器不会回滚这些要求。

## 实际验证

- 作者测试从 **22项增加到26项**，26项全部通过。新增4项对应复权值、qualified token、主体/谓词/断言绑定、晚到预测及hash一致性。
- 独立extension probe重新运行，**13项记录均符合已声明边界**：10项已实现边界检查，3项继续明确演示未实现的Repository能力/覆盖/artifact类型关联。不能把后3项称为生产通过。
- base schema、OpenAPI、API binding、四份SQL、all_fields共8项与v3交付仍字节一致；因此80表/883列/31操作/142base defs的基线未改变。
- 生成器内defs/bindings与staging实际JSON相等；50项决议的数量与处置分类不变，只更新P06、P13、P19、P23、P29的相关验收措辞。

证据文件：`final_contract_fixes_author_tests.log`、`final_contract_fix_results.json`和更新后的`extension_independent_results.json`。修复前8份文件保存在`final_contract_fix_before/`，供差异审查。文件哈希完整登记在结果JSON。

## 对最终集成者的提示

需要将最终验证汇总里的“22个扩展作者测试”更新为26，并从本轮结果取新hash。若交付包已复制旧extension_independent_results.json，应换为本轮复验结果。其3项Repository缺口仍存在，README已保留边界说明。本次未改研究目录或新增策略/因素数量。
