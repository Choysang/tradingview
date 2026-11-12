# Pulse 契约种子

本目录将 `spec/GREENFIELD_SPEC.md` 第 10、15、16 节的一部分约定写成可机器校验的 JSON Schema。它是新实现的起点，提供字段、类型、条件树和引用样例；没有行情采集、模型调用、PlanCompiler、撮合、账本或网站运行实现。

所有示例都显式标为 `hypothetical`。`SAMPLE_MAIN_001` 是假设证券；价格、消息、费用、分数、账户、模型名称、时间线和输出都是测试值，不构成真实股票建议，也不表示已经产生了这些交易。

## 文件

| 文件 | 用途 |
| --- | --- |
| `definitions.schema.json` | JSON Schema draft 2020-12，当前版本 `1.0.0-seed.1`；根对象校验完整 fixture，各独立合同位于 `$defs` |
| `fixture.json` | 一个假设事件到模型提案、计划版本、决策快照和发布事件的关联样例 |
| `validate_fixture.py` | 使用 `jsonschema` 检查结构，并检查样例中的部分引用、时间、单位、版本和报价容忍关系 |
| `requirements-validation.txt` | 本次验证使用的直接依赖版本 |
| `validation.log` | 实际验证命令、退出状态与输出；只证明本目录所声明的验证范围 |

Schema 的 `$id` 使用 `example.invalid` 作为未发布的命名标识。内部引用全部采用 `#/$defs/...`，验证不需要访问该域名。实施时由契约负责人确定正式 namespace。

## 核心对象与关联

| 对象 | 本种子的明确边界 |
| --- | --- |
| `EventEnvelope` | 目前只定义 `decision.published`、`plan.revised` 两种有类型的 payload；第 10 节其他事件需逐个加入明确分支 |
| `StrategySpec` | 假设、来源、股票池、数据要求、时钟、入场、退出、仓位、持有期、参数和执行依赖 |
| `RunManifest` | 输入清单、代码及参数标识、策略/模型政策版本、费用/执行规则与时间口径；模型分析和计划编译各有一份 manifest |
| `ModelRun` | 此种子只覆盖 completed 形态，保存具体模型、提示/工具版本、输入、时间、用量及真实输出字段；上线前需扩展 pending/failed 等明确变体 |
| `PlanProposal` | 完整结构化建议、hard/soft 依赖、预期计划版本、有效期、报价容忍和必需复核项 |
| `TradePlan` | 唯一正式计划合同；`PlanVersion` 就是一份 `(plan_id, version)` 的 `TradePlan`，不能再创建含义不同的 `TradingPlan` |
| `DecisionSnapshot` | 绑定同一批排名、计划版本、证据、ModelRun、运行输入与发布包，供推荐页和个股页共用 |

基本引用方向是：`StrategySpec ← RunManifest ← ModelRun → PlanProposal → TradePlan ← DecisionSnapshot ← EventEnvelope`。这里描述对象关联，不表示提交顺序；有效快照与计划发布必须遵循规格中的同事务规则。

`fixture.json` 的 `external_references` 仅用于让这个小样例自洽，包含假设的数据 manifest、证据版本、证券状态、账户、行情和规则版本。它不是这些领域的正式生产 Schema，也没有实际 Parquet、公告正文或完整规则数据。正式实现仍需补齐 Observation、DatasetManifest、MarketRules、OrderIntent、Fill 和 LedgerEntry 等合同。

## 条件树与时间

AST 只允许 `all`、`any`、`not`、`compare`、`crosses`、`within_session`、`event_state` 七类节点。每个节点有明确 `clock`。操作数是带 `feature_id` 与定义版本的特征，或带单位的十进制字符串常量。不存在 SQL、Python、自然语言执行式或 `eval` 字段。

`crosses` 必须声明方向及 `previous_valid` 观察选择规则；没有上一有效值时产生 unknown。所有条件按三值逻辑求值，unknown 不触发。`bar_close` 需要 timeframe，并且运行时仅使用对应 `is_final` 的观察。Schema 能检查字段，无法证明历史数据确实已经收盘；这由运行内核验证。

触发器以 `plan_id + plan_version + trigger_id + account_id + transition_sequence` 去重。默认 `on_transition_to_true` 表示 false/unknown 转为 true 才触发一次；重新武装、cooldown、max_fires 与分批最小间隔必须由运行器实现。样例是 watching 状态，等待发布后的有效观察；价格处于区间内并不代表已经成交。

持有期使用 `trading_sessions` 或 `duration`。本种子将第 15 节的默认计数命名为 `after_first_fill_session`：不将首次成交所在会话计为第一个完整会话。`entry_session_inclusive` 则包含入场会话；`holding_anchor` 分别指定首次成交、最近成交或逐批首次成交。样例为首次成交后一个完整交易会话。交易日历、可卖库存和最终退出时间仍须由执行内核计算。

所有示例时间都为 `2026-11-10` 的 UTC+8 假设时间：10:01 收到事件，10:01:02 输入就绪，10:01:25 模型完成，10:01:26 计划与快照发布。它不记录实际市场活动。

## 版本 依赖与计划替换

`schema_version` 是合同版本；`StrategySpec.version` 是策略版本；`TradePlan.version` 是单个计划的正整数版本；模型政策、规则、费用和数据版本另外引用，不能混为一个“最新版本”。每个领域对象保留第 10 节的共同字段和稳定幂等键。

本种子用整数表示 hard dependency 中的账户与计划版本，用字符串表示其余领域版本。初次创建计划使用 `expected_plan_version=0`，对应生成 v1；更新计划必须读取并提交当前预期版本。代码中的样例校验会检查这一关系，生产系统仍需依靠数据库比较更新与事务，文件校验不能阻止真实并发冲突。

hard dependencies 包括计划、账户、证券状态、规则和证据修订等；soft dependencies 包括普通报价与排名变化。提案包含 `analysis_as_of`、`expires_at`、`max_price_deviation_bps`、`required_revalidation`。编译后的计划另存实际复核的 soft 版本。样例从假设报价 10.00 元到 10.08 元，变化 80 bps，未超过示例容忍 150 bps；这只演示无需每次报价变化都重新调用模型的合同。

`replace_policy` 默认含义遵循第 15 节的 `cancel_remaining_and_reconcile`：新版本不删除旧委托和晚到成交，撤单确认前不释放预留资金。`keep_existing` 必须显式提出。种子提供字段与 supersedes 链，但尚未实现撤单竞争或资金预留。

## 数值与分数

价格与金额采用十进制字符串；股票数量为整数。价格明确为 raw，金额明确为 CNY；`ratio` 与 `percent` 是不同单位。运行实现应使用 Decimal 或整数最小单位完成计算，检查价位、整手、费用、现金和可卖数量。

相对排名分与校准概率是独立字段。样例分数 88 只是 `relative_rank_score`；`calibrated_probability=null`，不能向用户显示成 88% 上涨概率。alpha_rank 与 action_rank 也分开；未触发样例的 action_rank 为 null。

## 运行验证

在独立开发环境安装依赖后，从本目录运行：

```bash
python3 -m pip install -r requirements-validation.txt
python3 validate_fixture.py
```

验证器明确启用 `Draft202012Validator` 和 `FormatChecker`。验证内容包括 Schema 自身有效性、fixture 结构、选定的跨对象引用和时间关系，并含 8 个应被拒绝的合同反例：任意 eval 字段、非字符串常量、缺周期时钟、非法时间、未来证据、错误预期计划版本、丢失计划引用、超过容忍的报价。

这不是完整场景验收。尚未验证 T+1、涨跌停、部分成交、盘后时段、公司行动、费用计算、事件三值求值、数据库原子性、模型恢复、真实源延迟或盈利效果。对应实现完成后，按总规格第 14 节逐项提供实际运行证据。

## 后续代码生成与合同演进

1. 由契约负责人将种子移入新仓 `packages/contracts`，确定唯一权威定义；不得让 Python、TypeScript 与 Schema 三套类型分别手写演进。
2. 可以从 Schema 生成语言 DTO，也可以正式选定 Pydantic 为权威后生成 Schema/TypeScript；转换前后须保持这些样例与单位语义，并比较产出 Schema。当前不捆绑未经验证的代码生成器。
3. 用生成 DTO 校验同一 fixture，增加 Python/TypeScript 双向 JSON 往返样例；跨语言都保留价格字符串和时间偏移。
4. 扩展合同先补业务语义与典型样例，再改字段、消费者和验收。新增事件添加有类型的 payload 分支，避免退化为任意字典。
5. 当前对象关闭未知字段。即使只增加可选字段，旧版严格消费者也可能拒绝；必须先更新消费者或协商合同版本，不能一律宣称“增加字段总是向后兼容”。
6. 保存旧版本 fixture，持续验证历史决策仍可读取。破坏性变化建立新 schema/version 和显式迁移，不用新默认值改写旧计划或模型输出。

验收标准是合同能支持规格中的同一闭环，后续实现能够复用这些字段并解释版本与时间；本目录不声称已经完成整个系统。
