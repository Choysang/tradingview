# v3唯一执行与经济账本参考

本目录提供统一口径的设计、PostgreSQL种子、手算基准和可运行的单账户事件归约器。它成为v3经济账本的唯一参考；v2账户参考归档，避免两套权威。它不是完成的交易所撮合器或券商连接器。

## 重复运行

账本和原有基准只依赖Python标准库。进入本目录后运行：

```bash
python -m unittest -v test_unified_ledger.py
python run_benchmark.py
```

`build_golden_fixtures.py`保留手工期望值及推导，只有修改基准时才重建fixture。它不导入被测实现。`benchmark_results.json`是实际运行结果；`validation.log`记录测试命令、环境、输出与退出码。

## 文件与用途

| 文件 | 用途 |
| --- | --- |
| execution_standard.md | 标准分层、共同内核、费用与成交证据、经济账户、业绩和边界 |
| evidence.json | 官方来源、读取深度、支持的命题与未覆盖范围 |
| benchmark_spec.json | 已实现检查与生产待执行门槛，不把待办写成通过 |
| golden_fixtures.json | 10个合成手算场景，明确原始价格、费用、股息及外部现金流 |
| unified_ledger.py | 确定性事件归约、复式分录、数量批次与预留、TWR |
| test_unified_ledger.py | 手算校验、独立现金数量恒等式、重复/乱序/失败原子性等 |
| ledger_schema.sql | 持久化种子；本环境未启动PostgreSQL执行，不宣称事务验收完成 |
| synthetic_vertical_slice.py | 复用正式v3合同与唯一Ledger的合成纵向样例 |
| fixtures/artifacts.json、fixtures/result.json | 冻结发布、独立报价覆盖层、11个API响应与B01评估结果；均为合成数据 |
| review_contracts_static.py、final_contract_review.json | 独立合同、接口与FK静态复核；不连接PostgreSQL |
| review_final_spec.md | 最终总规、跨专项与集成冷审结果、修订闭环和剩余边界 |

## 哪些相同，哪些可以不同

相同规范事件、政策版本、初始账户与估值输入必须产生同一账本。三个transport harness只是传输/去重测试，共用一个事件归约器。实际回测、paper、券商具有不同的成交证据、粒度、延迟与队列假设，成交序列可能合理不同，但都必须用这个口径入账并解释差异。

参考只接受已规范化、按账户顺序协调完毕的CNY现金账户事件。生产原始成交事实必须先持久保存；迟到、撤销、更正与外部交易不能因为暂时不适配此有限参考就被丢弃。生产需另补来源回补与重算协议，仍使用唯一账本。

## 金额与数据库映射

API金额为字符串，金额分录与持仓成本使用12位小数，Decimal计算精度50、ROUND_HALF_EVEN。数量以整数股表示。服务还必须按实际货币/费用政策确定结算分位舍入，保留明细并调节尾差；这里不是银行结算收费系统。

fixture的可读ID在生产映射成稳定UUID。序列0对应开户快照事件。没有金额变化的纯拆股和零资产开户只写account_event与数量子账，不向SQL创建空journal。数据库中的cash、receivable、payable是互斥经济账户；broker.available、settled、unsettled、withdrawable是不同视图，不能互相直接累加。

参考事件的kind/sequence对应正式数据库event_type/account_sequence，生产统一Schema与Repository负责显式映射。所有公开金额运算固定数值上下文；事件effective_at按Asia/Shanghai核对会话日期，不允许客户端把会话提前以解锁当天新买库存。

## 合成纵向验收与合同冷审

已有环境依赖为`jsonschema==4.26.0`、`pglast==8.7`。脚本运行不访问外网，不调用供应商，不要求数据库。进入本目录后运行：

```bash
python synthetic_vertical_slice.py
python review_contracts_static.py
```

两个脚本默认相对自身位置读取`../contracts`和相邻SQL文件，不依赖调用者当前工作目录。纵向样例默认生成`fixtures/artifacts.json`、`fixtures/result.json`；可以用`--out-dir`指定输出目录。冷审脚本可以用`--root`指定整个v3包，用`--output`指定报告。

纵向样例包含同一release的市场榜、板块榜、板块总榜、TradePlan和冻结Workspace，随后叠加独立报价快照与ActionabilitySnapshot；发布后报价不会进入旧发布的内容hash。样例将可读证券ID显式映射为UUID，API时刻规范化为UTC Z，Decimal有符号零序列化为`"0"`。这些是接口适配，不改变经济账本。

既有B01事件仍由唯一Ledger计算，终值10,090元、净损益90元、总费用10元，区间无外部现金流的TWR为0.009。Evaluation引用账户结果和固定政策。评估是在合成情景结束后执行的记账一致性检查，训练区间只是合同占位，不是模型训练或策略盈利验证。`28`为DTO/响应Schema校验次数，`11`为API响应样例数，`1`为复用的golden场景数；没有把assert次数计为单元测试方法数。实际运行输出另存`synthetic_vertical_validation.log`。

独立静态审阅检查80表、149个FK的列、精确声明类型和最终唯一目标，31个API operation及OpenAPI与规范Schema的一致性，并检查18个数值/时钟/枚举边界。目标唯一键检查包含非部分的普通列唯一索引。它不能代替真实PostgreSQL建表顺序、约束触发、隔离级别、并发竞争或HTTP处理器验收。

## 尚未实现的生产部分

真实交易所规则的完整版本注册、交易日历、撮合排队/容量/滑点模型、完整费用和税费引擎、真实broker adapter、数据库并发与崩溃恢复、全量公司行动、一般成交撤销更正、融资融券与多币种。这些列在benchmark_spec.json的生产门槛中。采用IFRS/GIPS/FIX相关原则没有构成其认证或完整合规声明。
