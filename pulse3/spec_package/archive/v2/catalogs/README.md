# Pulse 来源 因素与策略目录

本目录是 `spec/GREENFIELD_SPEC.md` 的机器可读研究起点。它将来源、可计算因素和策略候选分开登记，使多个 Agent 能在相同ID和状态语义下并行实现。当前交付包含 **50 个研究参考记录、16 类共 96 个因素候选、13 类共 60 个策略候选**。

所有因素与策略均未完成实现和效果验证。本次核查仅覆盖引用文档；没有完成任何付费数据接入授权、在线采集集成、模型训练、历史回测、前向交易验证或券商连接。目录中的参数是实验候选，不是优化结果，也不是对用户实际账户的配置建议。

## 文件和用途

| 文件 | 用途 |
| --- | --- |
| `source_catalog.json` | 保存 A01–A30 和 T01–T20 共50个研究参考，区分资料内容核查与实际连接状态 |
| `factor_catalog.json` | 16类因素，每类6项；登记作用范围、信号时钟、字段、定义、来源与缺值规则 |
| `strategy_catalog.json` | 13类60个研究候选；登记入场退出、持有、数据粒度、成交模型和重复族 |
| `reference_inputs.json` | 随包提供的50条原始参考输入，包含生成脚本需要的全部引用元数据 |
| `build_catalogs.py` | 从原始50项研究参考清单生成三个JSON；不调用网络、不执行策略、不测试收益 |

JSON使用UTF-8，顶层包含 `schema_version`、`catalog_type`、`as_of` 和 `entries`。研究核查日期为2026-11-10。交易会话按 `Asia/Shanghai` 解释，跨市场原始时区仍须保存；用户显示时区不改变交易日归属。

## 来源目录字段

| 字段 | 含义 |
| --- | --- |
| `source_id` | 稳定研究引用ID；A类是市场资料，T类是工程和研究资料 |
| `url` `title` `supports` | 可追溯的原始资料及其支持范围 |
| `reference_kind` | 规则文档、提供方或披露资料、工程研究资料 |
| `documentation_status` | 当前为 `verified_documentation`，只表示研究过程中核实了资料内容 |
| `temporal_scope` | `checked_as_of_research_date` 或 `historical_background`；不是永久有效性保证 |
| `integration_applicable` | 该参考是否对应未来可能接入的数据能力；技术论文本身通常不对应在线连接 |
| `connection_status` | 可接入项当前均为 `unconfigured`；纯参考项为 `null` |
| `integration_test_status` | 当前全部为 `not_attempted` |
| `live_endpoint_verified` | 当前全部为 `false`，没有通过本项目端到端在线接口验收 |
| `user_entitlement_verified` | 当前全部为 `false`，不能把网页或接口文档当成用户已有权限 |
| `access_methods_described_or_proposed` | 文档描述或设计拟采用的接入方式，不是已实现的客户端 |
| `measured_latency_ms` `measured_coverage` | 当前为 `null`，没有测量就不填写推测结果 |

**50个参考记录不等于50个新闻或行情流。** 多个记录可能指向同一提供方的产品、权限、规则或技术说明。例如A10与A11是Tushare不同文档；T01–T20主要是架构和研究参考。实际 `SourceRegistry` 应在实现时为每个真实feed分配单独的 `feed_id`，关联一个或多个 `source_id`，并登记认证、字段、延迟、覆盖、版本与健康检查结果。

A12采用已核查的官方统计发布日程入口 `https://www.stats.gov.cn/sj/fbrc/bnxxfb/`。A27是2020年的期货细则，明确标为 `historical_background`，不能声称它是2026最新规则，也不能据此直接生成当前合约到期表。A08也是用于确认既往披露调整的历史年报。A25和A26共同说明抖音文档存在与实际开放资格必须分别核实。

## 因素目录字段

| 字段 | 含义 |
| --- | --- |
| `factor_id` `slug` | 稳定机器ID和可读标识，分别为F001–F096及描述名 |
| `family_id` `family_name` | 对应规格第4.1节的16个因素族；族ID为F01–F16，与三位因素ID区别 |
| `scope` | 作用于市场、行业、证券、事件、主题、账户或订单等对象 |
| `clock.trigger` | `event_available`、`bar_close`、`quote`、`session_open` 或 `scheduled` |
| `clock.as_of_rule` | 决策只读取当时已可用的输入和版本 |
| `required_fields` | 领域语义字段路径；实现时映射到版本化契约，不能直接当供应商原始字段名 |
| `output_unit` | 输出单位或向量类型，价格和比例口径需要进一步进入FeatureDefinition |
| `definition` | 计算定义和解释边界，属于设计合同，不是可执行代码 |
| `source_refs` | 支持来源接入或市场机制的参考ID，**不代表因子预测能力已经得到证实** |
| `proposed_feed` | 需要接入的具体来源类型；没有核实到准确来源时保留设计说明 |
| `data_readiness` | 普通定义已给但未连接、需要更高粒度、需要进一步研究定义等状态 |
| `missing_handling` | 缺值为 `null`、状态为 `unknown`，不能按0处理，不能让入场条件自动成立 |

不把不相关参考强挂在因子上。某项 `source_refs` 可以为空，表示它是明确提出的设计因素，仍需补充真实feed及证据；这比把一般文档误当数据来源更准确。

`event_available` 使用本系统解析或模型输出实际可用时刻。源站发稿时间不等于系统可用时刻。`bar_close` 消费指定周期已完成K线；进行中的月K、年K不能冒充最终值。订单流因子若缺全量委托，必须保持缺值或使用新ID登记的代理因子，不能继续输出同名真订单流指标。

| 因素族 | 条目数 |
| --- | --- |
| 国内流动性与财政 | 6 |
| 国内宏观 | 6 |
| 市场制度 | 6 |
| 海外金融条件 | 6 |
| 国际产业映射 | 6 |
| 地缘与贸易 | 6 |
| 产业与技术 | 6 |
| 商品与库存 | 6 |
| 公司经营与股权 | 6 |
| 公司治理与监管 | 6 |
| 资金与筹码 | 6 |
| A股情绪 | 6 |
| 技术与量价 | 6 |
| 订单微观结构 | 6 |
| 日历与注意力 | 6 |
| 叙事与突发 | 6 |

部分条目输出向量，例如RSI/ATR及质量价值。正式实现前应在FeatureDefinition中为每个分量分配单独定义版本、单位、参数、预热和缺值规则。目录条目负责研究覆盖，不能据此绕过细粒度契约。

## 策略目录字段

| 字段 | 含义 |
| --- | --- |
| `strategy_id` `slug` | S001–S060及可读标识 |
| `family_id` `family_name` | 对应规格第8.2节13个策略大类，族ID为C01–SF13 |
| `signal_spec.entry_rule` `exit_rule` | 可检查的候选规则描述；需要编译成条件AST和行为fixture |
| `signal_spec.is_executable_implementation` | 当前全部为 `false`，JSON本身不会产生交易 |
| `holding_period` | 持有单位、候选范围和首笔成交锚点；0仅用于已有库存叠加或委托生命周期 |
| `position_policy` | 普通长仓、已有底仓做T或父委托执行；均受T+1、整手和账户统一预留约束 |
| `data_requirements` | 最低粒度及所需因素ID；同时要求历史股票池、公司行动与费用版本 |
| `execution_model` | 日频下一会话、分钟下一合格事件或L2队列回放的设计配置 |
| `parameter_candidates` | 首轮可检验参数，不表示已调优或全参数空间已经穷尽 |
| `duplicate_family` | 与其他名称或参数版本重复的机制族，用于共享试验预算与消融 |
| `provenance` | 当前均为架构设计候选，未声称复现某位作者的原始完整策略 |
| `lifecycle_state` | 当前为 `discovered`；补齐AST、仓位、失效条件及fixture后才能到specified |
| `validation_status` | 当前全部为 `unvalidated` |
| `evaluation_status` | 回测、前向与训练run列表当前为空，`profitability_claim`为false |

| 策略大类 | 条目数 |
| --- | --- |
| 趋势与突破 | 5 |
| 反转 | 4 |
| 情绪接力 | 6 |
| 题材传播 | 4 |
| 量价与微观 | 5 |
| 事件 | 5 |
| 跨市场 | 4 |
| 基本面 | 4 |
| 因子组合 | 4 |
| 日历 | 4 |
| 机器学习 | 4 |
| 模型与RL | 4 |
| 退出与组合 | 7 |

所有日频策略都要从信号完成后的可交易时刻开始撮合。分钟策略不能借用日线收盘后才知道的量额。L2策略必须确有所需数据才进入 `data_ready`。正T和倒T要求已有可卖底仓，两条腿各自计成交、最低费用和未完成结果，不能卖出当天新买入的锁定库存。执行RL只优化已有父委托，不自行改变买入理由或目标总量。

股票池默认沪深主板普通A股，价格不高于100元、流通市值不高于100亿元为可关闭的初始偏好。新股排除窗口须由UniversePolicy登记后再运行首个实验；不在未确定窗口时默认视为已合格。没有把任何历史示例资金量当作用户真实账户资金。

## 新增和升级流程

1. **来源Agent**增加或更新真实feed，关联引用，保存样本和字段映射。只有接口、时间、覆盖和权限验收完成后，实际feed才可从unconfigured变为connected或partial。资料文档核实状态和feed状态分别维护。
2. **数据或因素Agent**为新因素登记ID、单位、作用对象、信号时钟、参数、预热、缺值和依赖版本；用冻结样本验证时点读取。模型估计使用独立字段，不能冒充原始观测值。
3. **策略Agent**保存来源原文快照和首次获知时刻，将自然语言变成可执行AST与StrategySpec；登记全部参数trial、仓位和费用依赖。仅给策略名称不算specified完成。
4. **研究Agent**先检查数据就绪，再做快筛和统一执行内核验证；按市场状态、龙头生命周期和时间样本外报告结果。失败也保留run、原因及复测条件。
5. **发布Agent**根据版本化EvaluationPolicy决定screened、validated、shadow、active等状态；发布新bundle，仅影响之后决策，不覆盖旧结果。

来源、因素、策略ID一旦有实验引用便不复用。修改语义时升级定义版本；改参数产生新trial，不复制出貌似独立的策略以稀释多重试验统计。不同Agent负责的目录由主控集成，避免同时覆写同一JSON。

## 覆盖界限和生成方法

本目录是可扩展的初始覆盖，不能称为已穷尽全网资讯、所有影响因素或所有交易策略。跨年、谐音、交割日等用户假设已保留为可测试候选；其效果不预先判定。未来更强模型可以提出新的因素和策略，仍通过相同数据、执行和研究合同接入。

生成脚本只接收本次研究的50条原始引用数组，每条至少有 `id`、`url`、`title`、`supports`。该输入已随包保存为 `catalogs/reference_inputs.json`。脚本仅使用Python标准库，不依赖网络、凭证、付费服务或包外文件。在解压后的 `Pulse_Rewrite_Package` 根目录运行：

```bash
python3 catalogs/build_catalogs.py --references catalogs/reference_inputs.json
```

脚本检查50个引用ID、96个因素、60个策略、16/13类覆盖和引用一致性，再写三个JSON。该检查只证明目录结构完整，不证明指标计算、在线接入、撮合逻辑或投资效果正确。使用交付JSON不需要运行脚本。
