# 来源、板块与分类专项：render04 发布复核

复核日期：2026-11-10。复核人：`v3_sources_sectors`。

## 范围与方法

本次仅复核 `/workspace/scratch/5ac6aa9eb6c9/report_render_v3_final_04/` 中父任务分配的 **25、26、32、33、34 页，共 5 页**。五页均分别通过 `view_image` 打开原始整页 PNG，实际检查正文、表格、页眉、页脚与页码；未以文字提取、缩略图或像素相等报告代替目视检查。

第 22 页由主控复核，不包含在本报告的实看范围。此前版本的全量分工复核及 render02 变动页记录见同目录 `source_pages.md`、`source_pages_final.md`。本次没有修改正文、代码、Schema 或 SQL。

## 逐页结果

| render04 页码 | 实际查看内容 | 内容闭合检查 | 版面结果 |
|---|---|---|---|
| 25 | §3.1 时间口径表、§3.2 不补造数据 | `available_at` 保持实际可用时间，历史研究另用 `policy_usable_at`；缺失值明确为 `null`，公共质量记录使用 `status=missing` 与 `reason`；领域内已有 `missing_reason` 由 Repository 映射到公共 `reason`，未增加同义契约字段；异源补齐使用 `value_origin=alternate_source` 并保留正式来源字段 | 时间表各列及长标识符完整，标题后正文充足，无裁切、溢出或遮挡；页码 25 正确 |
| 26 | §3.3 最小逻辑表 | 表中明确是逻辑对象，排名对象名称为 `ranking_run/ranking_entry`；扩展表与公共表复用说明衔接正常 | 表格宽度、行高和三列文字正常；原先长表名末字母孤立问题未复现；页尾段落作为跨页连续正文合理；页码 26 正确 |
| 32 | 龙虎榜研究段落结尾、§9.1 板块主数据、§9.2 三种排名契约 | `core.instrument_tag` 的赋值、证券、分类版本、节点、生效时间、可得时间、断言类型、证据和权重字段与实际 DDL 相符；板块内使用 `RankingEntry.alpha_rank`，板块机会使用 `SectorRankEntry.rank` 与 `rank_kind=sector_opportunity_rank`，账户顺序使用 `ActionabilityEntry.account_action_rank`，对象与语义没有混用 | 三类排名表完整；“板块内 alpha_rank”消除旧长标识符末尾孤立字母；正文与表格均未溢出；页码 32 正确 |
| 33 | §9.3 快速晋升特征、§9.4 候选池、§9.5 发布一致性、§10 分类与路由开头 | 候选池状态未覆盖 `TradePlan.state`；排名运行使用 `ranking_run`，进入工作台携带其 `ranking_id`；六角色中文为选股、配置、入场、持有、执行、退出 | 状态列表、节标题及正文间距正常；§10 标题后保留完整说明段，无孤立标题、截断或重叠；页码 33 正确 |
| 34 | §10 续文、§11 数据库迁移与发布约束、§12 工程验收与交付边界 | 迁移顺序列全为 core、ledger、sources_sectors、integration；发布父行锁、`READ COMMITTED` 和其他隔离级别未验证的边界保持明确；合成契约检查、SQL 静态解析与真实来源接通/策略收益测试区分清楚 | 压缩后的正文仍清晰可读，标题与后续段落连贯；底部正文与页脚保留间隔，无近空白页、裁切或内容碰撞；页码 34 正确 |

## 与实际契约的定点核对

作为上述目视检查的补充，只读核对了 `contracts/v3.schema.json` 和 `database/core_schema.sql`：

- `FieldQualityEntry` 的公共原因字段确为 `reason`；本次第 25 页已修复 render02 报告中的 C02。
- `RankingEntry.alpha_rank`、`SectorRankEntry.rank_kind/rank`、`ActionabilityEntry.account_action_rank` 均在现行 Schema 中存在；板块排名的三种 `rank_kind` 为 `sector_opportunity_rank`、`sector_attention_rank`、`sector_rising_rank`。
- `core.instrument_tag` 的实际列包含 `assignment_id`、`instrument_id`、`taxonomy_id`、`taxonomy_version`、`node_id`、`effective_from`、`effective_to`、`available_at`、`assertion_kind`、`evidence_ref`、`weight`。正文用 `taxonomy_id/version` 与 `effective_from/to` 表示成组字段，未引入新的生产列。
- `decision.ranking_run` 的主键确为 `ranking_id`；第 33 页的运行对象和传递身份对应。
- 第 34 页列出的四份迁移文件均存在。此处核对名称与顺序表述，不构成真实 PostgreSQL 迁移或并发事务执行验证。

## 结论

**本次 5 页全部通过内容与布局复核，没有新增阻塞项，也没有需要主控继续修正的排版缺陷。** 此结论仅覆盖列出的页面及其定点契约核对，不扩大为本报告独立审阅全部 112 页、执行数据库迁移或接通真实数据源的声明。
