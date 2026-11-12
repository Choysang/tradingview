# 特征计算参考样例 v3

这里保留本轮重写方案此前新编写的内存特征计算样例，并统一公共字段名称。**这不是从旧交易网站业务代码导入的实现**，也没有把任何因素或策略目录条目变成已实现策略。

本次代码和测试只进行了四个完整标识符替换，其他行为保持不变：

| 先前参考样例字段 | v3 公共字段 |
|---|---|
| `instruments` | `instrument_ids` |
| `interval` | `timeframe` |
| `price_mode` | `adjustment_mode` |
| `universe_version` | `universe_policy_version` |

`rename_manifest.json` 保存转换规则、输入/输出 SHA-256 和来源说明。生产 `instrument_ids` 应使用主数据稳定身份；测试里的 `XSHG:600000` 等只是原测试的示例标识，代码没有实现代码映射。`numeric_policy_version="decimal28-v1"` 也只是原测试的不透明版本字符串，不是生产数值精度配置；正式实现应引用公共数值政策，例如已登记的 `numeric(38,12)` 规则。

## 运行

只依赖 Python 标准库。从包含本目录的方案根目录运行：

```bash
python -m unittest discover -s reference_features -p test_feature_store.py -v
```

本工作区运行时目录前缀是 `research_v3/`。实际结果：**原 19 项测试全部通过**，输出见 `validation.log`。测试覆盖完整 19 维键、UTC 等价时点、同键 100 个并发请求只计算一次、不同快照/截止时间隔离、取消等待者、失败重试、可变对象隔离、LRU、并发关闭与取消关闭等待者。

## 具体范围

- `FeatureKey` 保存完整输入身份。它要求显式维度、冻结字段并规范化决策时间，**不会替数据库执行 PIT 过滤**。
- `SingleFlightFeatureStore` 在一个进程、一个 asyncio 事件循环内共享同键计算。它没有分布式锁、持久任务、数据采集器或跨机器一致性协议。
- 成功结果按 LRU 保留，`None` 也能表示一次成功计算得到的缺值。新数据或修订必须产生新键；不能等缓存自然过期来补救遗漏的版本维度。
- `max_entries` 约束已完成缓存条数，不是整个服务的并发任务上限。生产调度器应另设执行容量、排队和资源预算。
- 小结果通过深拷贝防止生产者/消费者改坏共享缓存。大型矩阵与原文应使用不可变对象或数据库/对象存储引用。

完整的特征键、依赖图、UNKNOWN、六种策略职责和组合规则见 `FEATURE_STRATEGY_PROTOCOLS.md`。
