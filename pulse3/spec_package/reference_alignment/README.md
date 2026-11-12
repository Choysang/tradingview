# 输入时点对齐参考

状态：有限、纯 Python、合成数据参考。接口只接收已规范化不可变观测，不直接读供应商、数据库、生产行情或模型。它补强 v3 对输入对齐的具体边界，不替换统一账本，不新增公共 HTTP 路由或数据库表。

## 1. 为什么需要它

“向后 ASOF”只规定一个排序键怎样连接，不能保证修订、实际可得性、会话、单位或分母正确。本参考将这些边界分别表达，验证旧好值不会掩盖当前坏值，晚到修订不会穿越到先前决策，上一分钟成交量不会伪装成下一分钟成交量。

## 2. 唯一内部政策合同

`input_alignment_policy.schema.json` 是 **InputAlignmentPolicy 1.0.0 的唯一 schema**。它不是公共 HTTP 合同，也不定义社区观点、图层或其他业务对象。

```json
{
  "alignment_policy_version": "quote-state-v1",
  "selection_mode": "state_hold",
  "max_age_seconds": 3,
  "strategy": "backward",
  "latest_invalid_policy": "surface_latest"
}
```

示例中的 3 秒只是测试政策参数，不是所有接口适用的刷新标准。生产值来自精确端点的能力、市场日历和用途。JSON 中五字段都必填；Python 类给两个不变字段提供构造便利，不授权配置文件省略它们。

| 字段 | 存在理由 |
|---|---|
| `alignment_policy_version` | 说明同一输入在何种选择/失效政策下被采用；变更不可覆盖历史。 |
| `selection_mode` | `state_hold` 可沿用尚有效的状态；`exact_event` 只接受确切事件时点，不能填补缺失 bar。 |
| `max_age_seconds` | 旧状态最多可使用多久；0表示只允许相同时刻。比较含微秒，不能因取整而多用一秒。 |
| `strategy` | 此参考只允许 backward；nearest/forward 不进入决策特征。 |
| `latest_invalid_policy` | 此参考只有 surface_latest。最新可得版本无效时明确输出状态；不默认回退更旧版本。 |

政策注册到已有 `core.contract_version`；校验使用它的版本填写 `ops.reconciliation_result.alignment_policy_version`。特征定义和参数固定政策内容/hash，完整 FeatureKey 的定义、参数、依赖摘要随之变化。不能仅改政策字符串而复用旧缓存。

## 3. 内部对象与既有字段的对应

| 内部对象/字段 | v3 实际落点 | 适配说明 |
|---|---|---|
| `Scope.instrument_id` | `core.instrument.instrument_id` | 生产UUID身份；显示代码不作此关联键。 |
| `Scope.source_id/endpoint_id/source_schema_version/mapping_version` | `core.source_endpoint`四字段身份 | 单位和解析版本属于精确端点。 |
| `Scope.snapshot_id` | `market.dataset_manifest.snapshot_id` | 固定不可变数据视图；参考fixture用可读ID，不声称通过生产UUID合同。 |
| `Scope.field_path` | `ops.reconciliation_result.field_path`及规范payload字段路径 | `shares.float`是已有目录的逻辑依赖名，此参考明确绑定流通股数，不是新HTTP列。 |
| `Scope.canonical_unit/currency/methodology_id/methodology_version` | 资金数据对应`intelligence.funding_observation`同名字段；普通bar由规范字段合同提供 | Scope由Repository/FeatureService组装，不假称每张表都有这些列。 |
| `Scope.timeframe/session_scope/trade_date/adjustment_mode` | CanonicalBar/FeatureKey及`market.bar_projection`相关字段、查询政策 | 此参考不实现CalendarService；调用方必须先给出正确会话分组和交易日。状态型股本使用effective_state、无日内trade_date；报价不能因此跨日续用。 |
| `Observation.observation_id/natural_key/observation_revision/event_time/available_at` | `ingest.observation`同名列 | 原文hash、recorded_at等仍留在既有观测/原始对象层，不由此参考重写。 |
| `Observation.effective_from/effective_to` | `core.instrument_state`、`core.instrument_alias`等有时点状态；新股本数据合同须另注册 | 业务有效期与系统知道时刻分开。 |
| `Observation.observation_window_start/end` | 资金观测同名字段；bar适配自`bar_open_time/bar_close_time` | 只是一种内部公共区间表示，不给公开bar字段改名。 |
| `Observation.field_status` | 规范字段质量映射自`FieldQualityEntry.status` | 保留observed/derived/missing/invalid/not_applicable/disputed。 |
| `AlignmentResult.reason` | `FieldQualityEntry.reason`或`ops.reconciliation_result.normalization_trace` | 无新“unknown质量”枚举。失败记录原因和选中版本，不生成假价格。 |
| `DerivedValue` | `market.feature_result.result_uri/result_hash/full_key/computed_at`及其不可变产物 | `price.vwap`、`turnover.fraction`是本参考特征标识示例；要作为生产特征运行须登记其合同与定义。 |

`Scope`不是新增的全站规范DTO，它只是把单条规范观测、数据集manifest和选择政策已有信息组合起来供纯函数验证。只有完整scope相同才参与同次选择。即使两源都给出CNY，也不能默认它们的方法相同。

## 4. 选择顺序

1. 精确匹配scope并识别同observation_id异payload的冲突。
2. 先筛 `available_at <= decision_as_of`。对每个 `natural_key` 选择最高的当时可得 `observation_revision`，不以网络到达顺序替代版本。同一最高修订有多个内容时先保留该冲突组身份；即使后面时间过滤只剩一条，只要该组影响当前候选仍输出disputed。完全过时且不影响当前选择的组，不阻塞更新的无歧义状态。
3. 再筛 `event_time <= target_event_time`，且业务 `effective_from <= target_event_time`；目标不得晚于决策截点。
4. 状态按业务生效时刻排序；exact_event按事件时刻排序。相同最高时间存在多个自然键时输出disputed，不能随机取一条。
5. 检查业务失效区间、exact_event相等条件和max_age。
6. 最新候选若missing/invalid/disputed/not_applicable，直接保留该状态；不把旧值偷偷拿回来。

返回的是原来的观测对象，连同时区、来源、方法、快照、可得时刻和年龄。状态沿用不更新source event_time或available_at，不因此声称出现了一笔新交易。`AlignmentResult.freshness_state`只针对本次对齐政策，不替代来源健康服务。

## 5. 两个派生示例

### 区间 VWAP

`turnover_amount_cny / volume_shares`。二者必须同证券、来源端点、映射、统计方法、snapshot、周期、会话、交易日、raw口径和窗口，且bar已final。金额单位CNY，数量单位shares；输出CNY/share。0成交量不返回0元价格；金额与成交量一边为0另一边为正表示矛盾。

### 区间换手fraction

分子为该区间真实成交股数。分母可以来自独立正式股本来源，但必须明确是 `circulating_shares`，使用窗口结束时生效且决策时已可得的版本。**若该版本在窗口内才生效，参考返回 `DENOMINATOR_CHANGED_WITHIN_WINDOW`，要求更细区间；不把全窗口量除新股本当作已解决变更问题。** 自由流通股数、未来修订和无有效分母不能替代。输出小数比率，0.1表示10%，它不是概率，合理情况下可以超过1。

两类派生均使用独立 Decimal Context（precision50、scale12、ROUND_HALF_EVEN，显式设置Emin/Emax与完整flags/traps），不会继承调用者或全局DefaultContext的精度、舍入和Inexact/Rounded traps。正数计算若在12位小数政策下量化为0，则明确失败为`NUMERIC_RESOLUTION_UNDERFLOW`，不会把有效零价或零换手送给策略。`available_at`不早于计算完成和全部输入可得时刻。调用者传入计算早于决策截点、或输入晚于截点，会明确失败。

## 6. 运行与证据范围

在本目录运行：

```bash
python3 -m unittest -v test_alignment
```

测试覆盖PIT、修订、同源scope、会话、时区、年龄边界、过期状态、不静默回退、禁止流量插补、窗口与单位、股本修订、Decimal上下文和依赖引用。测试使用合成样本，没有获得真实股本或证明供应商值正确。

不在此参考内的生产责任包括：规范合同验证/UUID、source schema自动识别、交易日历、公司行动账务、跨源事实选择政策、数据仓库查询优化、海量排序、对象存储事务、分布式lease、历史reconstructed政策、执行撮合、实际接口和收益验证。不要把此小参考的运行时间写成全市场性能。
