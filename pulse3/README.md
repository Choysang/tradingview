# Pulse 3

从零重写的新版 Pulse，与旧版（仓库根目录的 `backend/`、`frontend/`）并行运行。旧版代码不被引用；旧版数据仓只作为一个声明过的数据来源被导入。

**仅用于研究和学习，不构成任何投资建议。**

## 组成

| 部分 | 位置 | 说明 |
|---|---|---|
| 契约 | `src/pulse/contracts/data/` | v3 契约（31 个接口）+ 扩展契约（41 个接口），后端校验与前端类型都来自这里 |
| 数据库 | `migrations/` | PostgreSQL 18，迁移脚本按编号执行并记录校验和 |
| 数据湖 | `src/pulse/adapters/lake/` | 按内容寻址的快照文件，带目录索引 |
| 领域逻辑 | `src/pulse/domain/` | 交易规则与费用、账本、排名、特征、战法生命周期 |
| 应用服务 | `src/pulse/application/` | 数据导入、分析发布、账户、研究、情报 |
| 接口 | `src/pulse/api/` | FastAPI，路径 `/v1/...`，扩展接口 `/v1/x/...` |
| 网页 | `apps/web/` | React 19 + Vite + TanStack Query + lightweight-charts |
| 后台任务 | `src/pulse/workers/` | `ingest`（行情导入）、`intel`（新闻、公告、席位）、`analysis`（生成发布） |

## 本地运行

```bash
uv sync
uv run python -m pulse.adapters.postgres.migrate
uv run uvicorn pulse.api.app:app --host 127.0.0.1 --port 8200
```

网页：`cd apps/web && npm ci && npm run build`，构建结果由同一个服务提供。

## 每日流程（收盘后）

1. `ingest legacy-import`：导入旧版数据仓的增量（研究用前复权日线）。
2. `ingest live-daily --universe`：全市场原始日线增量（新浪，每秒最多 1 次请求，约一小时）。
3. `intel news / claims / seats`：新闻、公告观点、龙虎榜席位。
4. `analysis daily`：生成当天的分析发布。原始日线覆盖不足一半时拒绝发布，并说明原因。

## 测试

```bash
uv run pytest -q
cd apps/web && npx tsc --noEmit && npx vitest run
```

数据库测试需要本机 PostgreSQL（`PULSE_TEST_DSN`）。

## 现状与已知缺口

- 已完成：方案中的 M0–M4、I1–I3 与 16 张产品扩展卡片，各自的验证记录在 `docs/*/..._evidence.md`。
- M5 已做（`docs/m5/`）：
  - E01 新旧系统同一截止时点（2026-11-09）对账：证券池、价格、信号、账本逐项比较，只剩 1 只股票的差异未能解释，已单独列出。
  - E02 目标服务器上的中断恢复演练：数据库重启、接口进程被杀、数据源中断、重复构建。演练发现的 3 个问题已修复并复测通过。
  - E05 候选策略、基线和数据源健康的状态快照。
  - E04 影子前向记录已预注册，2026-11-12 开始记录。
- 需要时间才能完成：连续 10 个交易日稳定运行（M5-E03）、前向记录的 5/10/20 日观察节点（M5-E04），以及最终切换（M6，需要用户拍板）。
- 已知缺口：没有盘中逐笔的首次封板时间；换手率为估算值；部分契约字段（类型化特征值、联赛结果枚举、推送流信封）待补，需要契约负责人决定。
