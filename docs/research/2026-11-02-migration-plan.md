# 迁移计划：在本仓库内迁到设计 1.0 的核心契约（2026-11-02）

> 依据：`2026-11-01-design-v1-review.md` 第 7 节的拍板。不新建仓库，不上 PostgreSQL。
> 原则：新内核先建、旧模块逐个改用新契约，每迁完一个就删掉旧路径；任何一步都不能让服务器上的 Pulse 和录制器停摆。

## 1. 已完成（本轮）

| 项 | 位置 | 验证 |
|---|---|---|
| 8 个时间字段 | `contracts/core.py` 的 `TimeFields`、`EventEnvelope` | 时间必须带时区；可用时间不早于收到时间，收到时间不早于公开时间 |
| 四个 P1 契约 | `DataViewSpec`、`ActionIntent`、`PortfolioState` / `Position`、`OrderPlan` / `ExecutionReport` + 订单状态机 | `backend/tests/test_core_contracts.py` 8 项 |
| T+1 账本 | `backend/app/domain/inventory.py`：买入先冻结资金，卖出先预占可卖股；当日买入进 `acquired_today`，`roll_session` 换到下一真实交易日才可卖 | 设计 8.5 节的例子（1000 股可卖，买 500、卖 500，剩 1000，可卖 500）原样通过；拒绝透支、重复卖出、卖当日买入股 |
| 机会账本 | `OpportunityRecord`：每个已触发意图都记录，未成交必须写原因（排队未轮到、一字板、价格不到、过期、资金、不可卖、停牌、预算、拒单） | 同上 |
| 排队仿真 v2 | `backend/app/domain/queue_fill.py`：乐观 / 基准 / 悲观三档；炸板一律按全额成交并标记 | `backend/tests/test_queue_fill.py` 6 项（合成快照）；真实数据从 11-02 录制开始 |
| 盘后时段录制 | `backend/app/warehouse/intraday.py`，已部署 | 服务器上调度检查：15:05:06 触发盘后网格与 15:05 全量 |

后端全部测试 722 项通过。

## 2. 旧模块到新结构的对应

| 新结构（设计 17.1） | 当前位置 | 处理方式 |
|---|---|---|
| packages/contracts | `contracts/schema.py`（旧看板契约）、`contracts/core.py`（新） | 新代码只用 `core.py`；`schema.py` 留给现有前端，页面迁完后删 |
| packages/domain | `backend/app/domain/` | 新建，继续放费用表、交易规则表（按生效日） |
| packages/data | `backend/app/warehouse/`、`backend/app/market/` | 加 DataView：只返回 `available_at <= decision_time` 的行；表加时间字段 |
| apps/recorder | `backend/app/warehouse/intraday.py` | 原地保留；输出改写成带 8 个时间字段的事件表（P1 数据湖） |
| packages/strategies | `backend/app/playbooks/`、`backend/app/factors/` | 选股器改为输出 `ActionIntent` |
| packages/execution | `backend/app/playbooks/backtest.py` 的 `simulate` | 用 `inventory` + `queue_fill` + 撮合器替换；替换前两者并行跑同一真实周对账 |
| packages/research | `backend/app/research/`、`scripts/lab/` | 实验台账、jobq 保留；实验结果写成 ExperimentSpec / Result |
| apps/api、apps/web | `backend/app/api/`、`frontend/` | 最后迁；先加 `/v1/portfolio`、`/v1/opportunities` |
| 存储 | Parquet 仓库 + 嵌入式数据库 | 账本与订单事件用 DuckDB 单写进程，表结构按设计 15.5 节 |

## 3. 下一步顺序

1. **11-02 收盘后**：检查第一天录制（`summary.json`、盘后时段的实际字段），确认腾讯行情 15:05 以后的成交量是否包含盘后成交。
2. **数据湖**：把当天的快照转成带 8 个时间字段的事件表（`received_at` 用 `poll_ts`，`event_time` 用 `quote_ts`）。
3. **仿真器上真实数据**：用 11-02 起的涨停股回放，统计三档成交差距；差距大的策略标"收益依赖未知路径"。
4. **纸面账户**：主书的每日信号走 `ActionIntent → OrderPlan → 撮合 → inventory`，与旧 `simulate` 对账一周；一致后旧引擎只留作对照。
5. **卖法比较器**：固定同一批入场成交，只换退出规则。
6. **多轴市场状态**：指数、广度、风格、情绪、题材分别估计，替换线上的单一"熊市模式"。
