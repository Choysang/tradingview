# 代码覆盖与理由台账

基线 `6f5c57f8df73c58f0340dab6977b95c05d21740c`。登记 435 个受版本管理文件、3484 个声明（Python AST + TypeScript/JavaScript 顶层声明正则，非完整 TS 语义树）。

清点覆盖不等于逐行人工验证。完整读取的文件仍只对列明问题作语义结论；未深入阅读的文件明确标为 static_inventory_only，不以自动生成文字冒充代码正确性证明。新实现每个业务模块必须补充需求、证据、契约、不变量和验收，而不是给每一行机械增加注释。

| 区域 | 文件数 | 新实现理由 |
| --- | ---: | --- |
| agents | 4 | 通过统一工具目录与能力路由重写；模型只提交结构化提案，事实和计划均可回放。 |
| api | 17 | 按新领域契约生成API投影，屏幕查询只读；不在路由重复生成业务决策。 |
| contracts | 8 | 从新规格重新定义数据与业务契约，明确版本兼容与生成方向；旧类型只作迁移输入。 |
| data | 23 | 区分真实行情、模拟行情、历史行情和主数据；统一交易日历、价格口径与数据版本。 |
| decision-research | 16 | 提取策略意图为Blueprint与独立实例；选股、配置、持仓操作和执行分层，共用回放内核。 |
| delivery | 2 | 消费已生效事件和计划版本，独立投递回执；不重新推导买卖策略。 |
| domain-kernel | 10 | 将纯规则重新实现为与时点市场规则、账户和统一执行内核一致的确定性函数；逐项迁移行为样例。 |
| features | 4 | 重新实现有类型的依赖DAG、按需子图与版本缓存；不直接继承旧评分参数。 |
| historical-evidence | 54 | 作为历史决策和实验来源归档；不自动覆盖本次v2规格。 |
| market-runtime | 5 | 将单进程行情状态拆成版本化只读快照、增量投影与可回放事件；保留显式来源和SIM标识的产品意图。 |
| operations | 3 | 迁移必要的数据操作与验证意图到有状态工作流/命令；删除重复入口前映射其消费者。 |
| project-support | 48 | 按文件消费者和新架构要求逐项确认用途；当前仅完成清点，不依据文件名宣称其正确或可删除。 |
| query | 2 | 自然语言编译到类型条件AST，筛选、回测与预警复用相同语义。 |
| research | 107 | 保留研究问题和样例，计算与执行统一契约；研究结果必须绑定数据、成本、规则和代码版本。 |
| verification | 66 | 将有意义的历史反例迁移为行为契约；不要求新实现复制旧内部实现。 |
| web | 58 | 按工作台重写，复用同一快照和事件版本；保留现有有效交互意图，避免前端自行计算独立排名。 |
| web-tooling | 8 | 按选定Web技术重新初始化依赖和构建；保留可证明必要的工程检查。 |

## 开发时如何关闭未验证项

1. 从用户功能选择一个纵向切片，登记 requirement_id 与可观察行为。
2. 确认字段单位、可获知时点、来源与执行规则；将证据链接到 ADR。
3. 为新文件/核心符号填写存在原因、输入输出、失败语义和替代方案；旧文件只决定迁移资产，不限制新实现。
4. 用跨模块场景验证，而不是只测函数重复自身计算。
5. 标记 reviewed、reproduced、implemented、integrated、forward_validated 各自状态；不得用一个 completed 掩盖差别。

## 核心ReasonRecord与需求映射

新增 `reason_records.json` 与 `requirements_map.json`，登记24条具体核心理由和24个已有需求锚点。`requirement_id`直接复用真实的C审计编号或V2验收编号；RR编号是本次新增的理由记录ID。并未制造新的审计发现、平台feature_id、paper_id或测试名称。

| 状态 | 本次条数 | 含义 |
| --- | ---: | --- |
| implemented_reference | 6 | 现存纯Python参考代码与本地日志覆盖的明确子集；不代表完整系统、分布式服务或网站已经实现 |
| planned | 15 | 设计、替代方案、取舍与验收已明确；planned_component只是规划责任，不能当作已存在的实现文件 |
| observed_issue | 3 | 既有审计定位的问题或适用范围差距；不自动代表已经修复或存在外部利用路径 |

每条ReasonRecord包含 requirement_id、实际component_path或planned_component、reason、evidence_refs、alternatives、tradeoff、acceptance_refs、status_scope和remaining_work。真实参考文件才使用component_path。旧仓库组件另列observed_legacy_components，并引用基线、索引条目和文件摘要，避免把旧文件冒充新实现。

requirements_map提供从需求到C发现、平台功能、论文、官方来源、测试和规格验收的正向关联，以及按这些ID反查RR记录的反向索引。映射覆盖全部30个C发现与45个实际参考测试；这不等于30项旧问题都已修复。未纳入本核心台账的平台/论文/验收编号明确列于coverage的unmapped字段，原始完整registry仍保留。

单进程FeatureStore覆盖19维身份、100同键并发、取消/失败/关闭及值变异边界；账户参考覆盖净额目标、T+1、现金政策、成交幂等、撤单累计成交对齐和总净值。这些行为已有45项通过日志。DataQuery真实可见性过滤、分布式总线、DAG调度、完整网格、论文自动发现、实验晋级和Web联动仍是planned，不能因有文字、方法卡或测试名称就计为已实现。

435个文件和3484个声明仍仅是索引覆盖。理由记录给出的是核心设计链，既不宣称每个文件都经过逐行人工审计，也不要求每个括号附一个论文。开发时沿该索引找到实际业务符号，为新增实现补齐局部理由与对应验收。

### 本次自校验

- 24个reason_id与24个requirement_id均唯一；全部需求锚点可在现有C审计或V2验收中找到。
- evidence_refs中的C编号、平台feature_id、paper_id和source_id逐项对照实际registry；没有悬空ID。
- 文件路径、规格heading、JSON指针均解析到现有交付文件；规划组件不伪造路径。
- 测试名称从真实Python AST取得，并逐项核对reference_models/validation.log中的通过记录；45个测试不重复累加。本次没有重新执行测试，也不将索引检查算作新增测试。
- 只编辑本README、reason_records.json及requirements_map.json；主规格、汇总与构建程序保持不动。
