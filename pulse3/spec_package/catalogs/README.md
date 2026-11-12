# v3 因素与策略目录

本目录整理此前重写方案新编写的候选定义，统一公共字段、职责、单位和路由；它不是旧业务实现的迁移，也不是已完成策略库。

- `strategy_catalog_v3.json`：61 条候选；S 编号保留为 `catalog_id`，生产登记使用可重复的 UUIDv5。每条包含主/支持职责、方法、期限、数据要求、状态、受控 taxonomy 标签和组合合同。
- `factor_catalog_v3.json`：105 条因素候选；缺值、记录质量、字段状态、新鲜度和 UNKNOWN 分开，单位使用受控词汇。
- `catalog_build_summary.json`：目录数量、角色分布、方法分布、UUID 命名空间和流通市值口径。
- `validate_catalogs.py` / `catalog_validation.json`：静态一致性检查及本次结果。

策略主职责数量：选股 32、入场 18、配置 3、执行 1、退出 4、持仓覆盖 3。全部保持 `not_implemented / unvalidated`，生产路由未启用。目录里的一个角色组件不自动拥有其他角色的实现。

原默认股票池的市值 basis 为 `circulating_market_cap`；v3 将含糊的外层名 `float_market_cap` 也统一成 **流通市值 circulating_market_cap**。数值和研究候选属性保持原定义，没有换成自由流通市值。

部分因素的输出是混合单位向量或品种相关单位，不能假填一个共同单位。其 `output_unit=null` 明确要求先完成 `unit_contract` 的分量或品种绑定；这是计算前资格条件。旧逻辑依赖路径不等于真实供应商字段，须通过版本化适配器绑定公共合同。

策略六职责、19 维 FeatureKey、依赖图、三值条件与合法组合详见相邻 `reference_features/FEATURE_STRATEGY_PROTOCOLS.md`。

## v3.1增补

S061与F097至F105来自补充材料的逐项校核，仍为研究候选。加入理由与需要的输入合同见spec/ADDENDUM_V31.md和traceability/proposal_decisions_v31.json。不存在“新增条目即已实现因子”的状态转换。
