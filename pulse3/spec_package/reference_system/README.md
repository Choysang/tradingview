# 数据规范化 发布和来源状态参考

本目录是用于验证关键不变量的有限参考，不是生产数据接入服务。只依赖 Python 标准库，SQLite 仅用于本地事务示范，不证明 PostgreSQL 的并发、触发器、故障恢复或吞吐。

## 运行

从实施包根目录运行：

```bash
python -m unittest discover -s reference_system -p test_data_plane.py -v
```

39 个测试方法覆盖下列边界；实际输出另存于 validation.log。源专项作者进行了独立代码审查，并发现、修复后复核了三个问题：外部 Decimal 精度影响规范化、同时间旧任务能重取预期版本覆盖新发布、未来心跳或内容时点未被拒绝。复核见 sources_sectors/review_data_plane.md。

## 文件与必要性

| 文件 | 作用 | 已实现的关键不变量 |
| --- | --- | --- |
| normalization.py | 精确端点字段和单位转换 | 来源、端点、源 schema 与映射版本成组匹配；缺值不补零；Decimal 精确转换；原值和血缘保留；对齐后才可跨源比较 |
| release_store.py | 发布包与消息的本地事务参考 | 必需视图完整才发布；stage 捕获不可变 base；CAS 拒绝旧任务；指针与 outbox 同事务；消费者 inbox 与效果同事务 |
| source_health.py | 传输和内容状态、连续游标 | 开启、传输、新鲜度分离；安静新闻不等于断流；空响应不推进内容时钟；先保存再推进连续游标 |
| test_data_plane.py | 正常与反例验证 | 同名异单位、精度边界、缺失、映射漂移、修订、重复、乱序、版本竞争、断点重放与未来时点 |

规范化只证明已编码的字段、单位与记录语义有效。`normalization_valid` 不代表 PIT 资格、真实源完整性或可交易性。原始 hash 对应传入的 JSON 对象规范表示，未冒充原始 HTTP 字节的 hash。生产还需保留原始响应字节。

引用的 mapping 样例位于 database/source_mapping_examples.json。参考只支持该文件中明确声明的三个端点映射，以及已编码的日线/盘中累计记录。生产端点、权限、时区、修订、分页、限额必须逐项验收。

## 显式映射和未覆盖部分

本地 release 的 staged/published 映射到生产 BUILDING/READY；可读 fixture ID 映射到生产 UUID。manifest hash 只能证明内容身份，不能证明真实湖表已提交或所有输入在当时可得。生产发布服务应使用 integration/integration_schema.sql 的 slot、generation、base 和 publication 机制。

来源游标示例验证连续区间，不实现任何特定供应商的不透明游标或跨页完整性协议。没有真实推送、轮询守护进程、定时器或 HTTP 调用。实际源配置仍为 unconfigured。来源恢复、补采和覆盖对账的实现要求见 database/database_design.md 与 sources_sectors/sources_sectors_design.md。
