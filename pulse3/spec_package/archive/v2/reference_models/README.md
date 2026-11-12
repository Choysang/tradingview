# Pulse v2 最小参考内核

这份代码独立于旧仓库，仅使用 Python 标准库，说明两个决定闭环正确性的接口：**共享特征计算**与**账户库存、现金和总净值语义**。它是可执行规格，不是完整交易引擎，也没有接入实际券商。

## 文件与验证

| 文件 | 职责 |
|---|---|
| `feature_store.py` | 完整版本缓存键、单进程异步 singleflight、取消隔离、失败重试、成功结果的有界缓存 |
| `account_model.py` | Decimal 账户、T+1库存、卖出预留、可配置卖款可用策略、账户目标净额、T的总净值增量 |
| `test_feature_store.py` | 19个测试，含100并发、取消、重试、版本/决策时点隔离、结果变异及关闭竞态 |
| `test_account_model.py` | 26个测试，含重复事件、部分成交、撤单累计成交对齐、资金策略、T+1与总净值反例 |
| `validation.log` | 真实运行命令、Python版本、逐项结果及退出码 |

复核后验证：**Python 3.12.14，45个测试，全部通过，退出码0**。运行时间是该次测试环境记录，不是生产性能基准。测试数量按unittest测试方法计数，`subTest`中的多个边界案例没有额外计数。初版37项基础上新增8项回归测试，没有把子案例重复计数。

在本目录运行：

```bash
python -B -m unittest discover -s . -p 'test_*.py' -v
```

也可从任意目录将 `-s` 指向本目录的绝对路径。无需安装第三方库。

## 1. 为什么要有共享Feature计算

图表、选股、警报、模型上下文若同时需要同一MACD，应共享同一快照下的计算。否则会重复拉取数据、重复计算，并可能因为输入时点不同而互相矛盾。

`FeatureKey`有19个显式维度：特征ID、定义版本、实现摘要、参数摘要、证券序列、周期、价格模式、快照ID、**决策时点、可用性政策版本、交易会话政策版本**、输入修订、输入Schema版本、上游依赖摘要、股票池版本、交易日历版本、复权版本、交易规则版本、数值政策版本。不使用的维度也必须显式填写 `not-applicable` 等值。`snapshot_id`必须是冻结数据版本，不能是会随时间改变的“latest”别名。`decision_as_of`必须为带时区的 `datetime`，构造时归一到UTC；同一快照的不同决策时点不能共用结果。

“键完整”是依赖契约，不是由缓存自动证明的事实。**键隔离不等于DataQuery已实施可见性过滤**：实际查询还必须按决策时点、available_at和可用性政策筛选输入，不能把快照中的未来行交给计算函数。计算函数不能偷偷读取未进入身份的当前时间、最新新闻、全局模型或可变参数。模型权重、因子依赖、行业映射等影响结果的版本，应进入实现/依赖摘要或输入修订。证券顺序保留，因为跨证券输出可能依赖顺序。

参数摘要是计算当时的不可变字符串，不会冻结调用方原字典。计算函数必须使用与摘要对应的不可变参数快照或独立副本；“先取摘要，再修改原字典并由闭包读取”违反契约。FeatureKey使用冻结slots，外部参数变更不会改变已存在键的哈希身份。

### 关键函数存在理由与覆盖

| 对象/函数 | 存在理由 | 主要测试 |
|---|---|---|
| `parameter_digest` | 参数顺序不影响身份；参数值变化影响身份；拒绝非有限值和歧义键 | `test_canonical_parameters_and_parameter_change`、`test_parameter_digest_rejects_ambiguous_or_nonfinite_parameters` |
| `FeatureKey` | 防止不同快照、复权、日历、代码等被错误复用 | `test_every_version_dimension_changes_the_key`、`test_no_implicit_or_mutable_key_dimensions` |
| 显式决策时点与政策版本 | 同一数据快照可在不同历史决策时点产生不同可见输入 | `test_decision_time_requires_timezone_and_normalizes_same_instant`、`test_same_snapshot_different_decision_time_does_not_reuse_future_result` |
| 冻结键与参数摘要 | 原始参数变更不改变已入缓存键的身份，新参数获得不同摘要 | `test_parameter_mutation_cannot_mutate_existing_key_identity` |
| `get` | 同键请求只创建一个producer，其他请求共享结果 | `test_100_concurrent_same_key_compute_once` |
| `asyncio.shield`所在等待路径 | 页面取消或调用方超时只结束该等待者，不取消其他模块依赖的工作 | `test_cancelling_creator_waiter_does_not_cancel_other_waiter`、`test_all_waiters_cancel_but_later_waiter_joins_original_work` |
| `_run`成功缓存/失败清理 | 失败不污染缓存；下一次请求可以重试；None也可以是合法结果 | `test_shared_failure_is_not_cached_and_retry_succeeds`、`test_none_is_a_cached_success` |
| 结果进入缓存及返回时复制 | 防止生产者或一个消费者修改dict/list后污染其他消费者 | `test_mutable_results_are_isolated_from_producer_waiters_and_cache` |
| 快照隔离 | 不把不同时间可见数据的计算合并 | `test_different_snapshots_compute_independently` |
| 有界LRU | 参考缓存也不能随快照无限积累成功结果 | `test_bounded_lru_evicts_least_recently_used_result` |
| `_observe_completion` | 所有等待者取消后仍观察producer异常，避免未消费异常的后台任务警告 | `test_failure_after_all_waiters_cancel_has_no_unobserved_exception` |
| `_bind_loop` | 明确拒绝把同一实例跨事件循环使用 | 通过运行时检查表达范围；不宣称线程或多进程安全 |
| `aclose` / `_shutdown` | 所有关闭调用共享同一清理任务，避免第二次cancel打断producer的finally | `test_shutdown_cancels_producer_and_rejects_new_work`、`test_concurrent_shutdowns_do_not_interrupt_producer_cleanup`、`test_cancelling_shutdown_waiter_does_not_abort_shutdown` |
| `stats` | 观察命中、合并及进行中的数量，并让并发测试使用真实状态同步 | 100并发、不同快照与关闭测试 |

同键第一次提交的计算函数生效。后续请求传入不同函数但保持同键，属于调用方违反身份契约。本参考实现对小结果使用 `deepcopy` 隔离，结果类型必须支持正确的复制语义。**生产FeatureResult应优先采用不可变Arrow/Parquet引用或只读值，不能让100个消费者各复制一份大型矩阵。**共享计算不意味着可以随意共享可变对象。CPU密集计算不能直接长时间阻塞事件循环，生产实现应调用worker；此处只证明协作和身份语义。

## 2. 账户字段及不变量

全部金额、价格和数量使用 `Decimal`，直接传入浮点数会被拒绝。数量为整数股。交易动作数量必须严格大于0；目标持仓允许0；规划差额允许负数。价格必须为正，估值标记允许0。费用显式提供且非负。

每个持仓保持：

```text
0 <= reserved_sell_quantity <= sellable_quantity <= quantity
available_to_sell = sellable_quantity - reserved_sell_quantity
```

`sellable_quantity`包含已经被卖单预留的合格库存，`available_to_sell`才是其他策略还能使用的数量。买入只增加总持仓；卖出成交同时减少总持仓、合格库存和预留。释放卖出预留不会恢复已经成交的股票。

`advance_trading_day`由上层CalendarService提供真实的下一交易日期。它解锁股票库存；不推断某服务的现金何时结算，也不自动判断跨日委托是否仍有效。

### 两种现金政策

| 政策 | 卖出净款去向 | 何时可以继续买入 | 不代表什么 |
|---|---|---|---|
| `DOMESTIC_SALE_REUSABLE` | `cash_available` | 同一交易日可用于后续交易 | 不表示可立即转出到银行 |
| `UNSETTLED_UNTIL_RELEASE` | `cash_unsettled` | 收到确定结算事件后显式调用 `release_unsettled_cash` | 不把某服务的unsettled限制冒充全部国内A股规则 |

两种政策只改变可用性，不改变财富。本模型的 `cash_available` 与 `cash_unsettled` 是**互斥的资产桶**，因此可以相加；后者特指“不可复用、等待释放的余额”。DOMESTIC模式下该桶为0不表示券商已经完成资金交收。真实券商的available、settled、unsettled、withdrawable字段可能互相重叠，不能把它们原样灌入本模型相加；完整系统应从互斥现金账本派生可交易/可转出视图。若极小卖单的费用高于卖出金额，差额立即作为现金支出，不能把负费用变成一个待结算资产。

### 关键函数存在理由与覆盖

| 函数 | 存在理由 | 主要测试 |
|---|---|---|
| `Position` / 数值校验函数 | 将数量、库存顺序与Decimal要求收束到一处 | `test_position_invariants_are_enforced`、`test_positive_integral_quantities_required_for_actions`、`test_floats_are_rejected_at_money_boundary` |
| `reserve_sell` | 两个策略不能同时卖出同一份库存；相同order_id重试不重复预留 | `test_reservations_prevent_two_strategies_from_overselling` |
| `apply_buy` | 扣除价格与费用，总库存增长但当日可卖不增长 | `test_today_buy_does_not_increase_sellable_until_next_day`、`test_failed_buy_leaves_all_state_unchanged` |
| `apply_sell` | 消耗预留并按资金政策入账，拒绝超量成交 | `test_overfill_and_fill_after_cancel_do_not_mutate_account`及现金政策测试 |
| `_is_repeat` | 同一fill_id重发不会重复记账；同ID不同内容会报错 | `test_duplicate_sell_fill_does_not_double_book_cash_or_inventory`、`test_duplicate_buy_fill_does_not_double_book` |
| `cancel_sell` | 仅应用已对齐的撤单确认；必填累计成交与本地不一致时不释放预留 | `test_partial_fill_then_cancel_releases_only_remainder`、`test_cancel_confirmation_with_missing_fills_preserves_reservation` |
| `advance_trading_day` | 库存结算与现金结算分开 | `test_today_buy_does_not_increase_sellable_until_next_day`、`test_inventory_day_roll_does_not_assume_cash_release` |
| `release_unsettled_cash` | 只有明确的服务结算事实才改变资金可用性 | `test_service_unsettled_policy_blocks_reuse_until_explicit_release`、`test_cash_bucket_policy_does_not_change_total_equity` |
| `net_target_deltas` | 所有策略先合成账户目标，再扣现有持仓与尚未成交订单，减少冲突和重复委托 | 3个`NettingTests` |
| `total_equity` | 同时计入全部持仓和两种现金，拒绝缺失估值 | `test_marks_must_cover_inventory_and_be_finite`及总净值测试 |
| `fork` / `compare_t_equity` | 用同一起点和同一终值价格，对比实际操作与持有分支 | 4个T增量及比较前提测试 |

### 账户净额示例

账户持有200股；策略A的目标从100变成150，策略B从100变成50。目标总和仍为200，因此不生成账户交易。策略各50股的对向变化，不能被执行成买50股再卖50股并浪费费用。

另一个例子：A目标300、B目标200、现有400、还有未成交买单50，新的差额是：

```python
delta = 300 + 200 - 400 - 50  # 只需新增50股
```

输入必须覆盖全部活跃策略的绝对目标；没有目标的已有证券被解释为目标0。`pending_deltas`是尚未成交的有符号数量，不能包括已计入账户持仓的成交。这个函数只规划差额；它不处理委托撤换、资金预留或成交时序。负差额仍须经过卖出预留和外部MarketRules。

## 3. 做T用总净值增量评价

使用相同起始资产、相同终值价格和相同估值交易日：

```text
total_equity = cash_available + cash_unsettled + sum(quantity * terminal_mark)
T_increment = active_total_equity - passive_total_equity
```

费用在每一笔买入和卖出时已经进入现金，所以双边费用都被扣除。结果还分解为现金差额与剩余库存市值差额。这能处理尚未买回、只买回一部分及股价继续上涨的情况。

测试使用初始现金10,000元、底仓1,000股：

| 情况 | 主动操作 | 同一终值 | 相对持有的总净值增量 |
|---|---|---:|---:|
| 完整往返 | 卖200股×10.50、买200股×10.00；各付5元 | 9.80 | +90元 |
| 卖后未买回 | 卖200股×10.50，付5元 | 12.00 | -305元 |
| 只买回一半 | 卖200股×10.50、买100股×10.00；各付5元 | 12.00 | -110元 |

第二、第三行是必须保留的反例：只看已经实现的差价，会忽略减少底仓后错过的上涨。本实现没有使用closed profit替代total equity。账户不记录独立成本价，所以也不会把“摊薄成本下降”误显示成无条件财富增加。

## 4. 明确范围

这两份模块刻意保持小型：

- **单进程、单事件循环FeatureStore**；不是跨进程锁、分布式缓存或持久任务系统。不同进程同键仍会各算一次。生产接入还需要共享结果持久化、分布式协调与恢复协议。
- **账户由单个写入者顺序调用**；不是线程安全数据库事务。订单ID、成交ID和预留仅存内存，重启后需由持久事件/券商对账恢复。
- **买入API是成交应用原语**；没有未成交买单资金预留。生产OMS需另建cash reservation并与成交释放一致；不能把这份参考代码直接当作完整下单服务。
- **撤单表示已对齐的撤单确认，不是撤单请求**；`confirmed_cumulative_filled`必须来自权威确认。若券商已成交30股而本地尚未收到成交明细，累计量不一致会拒绝释放并保留预留；OMS需保留待处理确认，补齐成交后重试。此处没有实现完整乱序回报队列。
- `release_unsettled_cash`尚无结算事件ID；重复事件的幂等须由外层事件账本保证。该函数只演示互斥现金桶转移，不充当完整结算接入器。
- **费用由调用方提供**；没有硬编码费率、最低佣金、税费、舍入规则或资金转出规则。Decimal采用调用方当前上下文；正式计算需固定数值政策版本并锁定精度/舍入。
- **不覆盖涨跌停排队、集合竞价、价格笼子、整手约束或证券特例**；这些是外部MarketRules/撮合器的职责。
- **比较账户没有外部出入金、分红送转、融资借贷、跨币种或公司行动**；扩展后必须先统一现金流/企业行动口径再比较总净值。
- 测试证明这些有限契约的行为，未证明生产吞吐量、持久化能力、实际交易权限或投资收益。

这些范围是实现边界，不需要增加人工确认流程。它们为后续Agent并行实现DataHub、OMS、回测与账户模块提供同一份可运行语义。
