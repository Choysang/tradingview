# 最终第二次渲染：来源专项负责页复查

日期：2026-11-10。对象：`report_render_v3_final_02/page-N.png`。

指定复查页：4、14、15、16、17、18、19、20、21、22、23、25、26、27、28、29、30、31、32、33、34、35，共 22 页。全部按原始整页图像逐页复查；本文件不代表复查其余未分配页。

## 逐页记录

| 页码 | 状态 | 观察 |
|---|---|---|
| 4 | 已看原始整页 | 架构表列宽调整后完整清晰，模块名换行减少，无溢出。 |
| 14 | 已看原始整页 | 现为数据库章节首页，旧近空白第 14 页已不存在；负载表完整。首发物理部署标题与介绍同页，列表接第 15 页。 |
| 15 | 已看原始整页 | 部署列表、容量公式与容量表完整可读，无裁切。 |
| 16 | 已看原始整页 | 分区/发布完整性及资源条件清晰；页底标题及第一条列表完整。 |
| 17 | 已看原始整页 | 单位映射表完整；公式与来源标记清晰，无溢出。 |
| 18 | 已看原始整页 | 缺失/补齐规则清晰，API 表头和两行完整，接第 19 页。 |
| 19 | 已看原始整页 | API 续表重复表头，无裁切；instrument_id 等长路径仍词中换行，属于轻微可读性余项。 |
| 20 | 已看原始整页 | 恢复协议、发布不变量与 Outbox 段落完整，无视觉缺陷。 |
| 21 | 已看原始整页 | 缓存、健康检查与分类衔接正文清晰，无视觉缺陷。 |
| 22 | 已看原始整页 | 分类、验收与依据索引清晰；索引最后一条被推至第 23 页。 |
| 23 | 已看原始整页 | 仅顶部 1 条 DB12/13 索引项目，余下近整页空白；新问题 F01。 |
| 25 | 已看原始整页 | 来源能力续表完整，表头重复；新增 3.1 的实际主键与字段关联可读。 |
| 26 | 已看原始整页 | 时间字段表完整；3.1/3.2 语义基本对齐，missing_reason 词仍需与正式 reason 对齐，见 C02。 |
| 27 | 已看原始整页 | 逻辑对象表完整；funding_observation、seat_disclosure_row 等已整词显示，旧孤字问题修正。 |
| 28 | 已看原始整页 | 来源能力、健康枚举与写入列表完整，无视觉缺陷。 |
| 29 | 已看原始整页 | 轮询与会话表完整，标题与正文相连，无视觉缺陷。 |
| 30 | 已看原始整页 | 多源核验与主张结构正文清晰，无视觉缺陷。 |
| 31 | 已看原始整页 | 社区表列宽改善，多个标识符整词显示；attention_price_divergence 仍词中换行但无丢字。 |
| 32 | 已看原始整页 | 资金口径与席位解析正文完整，无视觉缺陷。 |
| 33 | 已看原始整页 | 排名表完整；account_action_rank 整词显示，within_sector_alpha_rank 末尾 k 仍独占一行，见 F02。 |
| 34 | 已看原始整页 | 候选池和标签路由完整；中文六角色已改为“选股/配置/入场/持有/执行/退出”。 |
| 35 | 已看原始整页 | 迁移、发布锁与验收说明完整，无视觉缺陷。 |

## 本轮结论

**指定 22 页全部已用 view_image 按原始整页查看。**没有发现内容裁切、表格越界、缺字、重叠或页码错误。旧第 14 页近空白页不再存在；“持有覆盖”角色标签已修正；三列表列宽明显改善。尚有 1 个新近空白页需要回收，1 类轻微词中换行，以及 1 处正式字段名一致性提示。

### F01：新第 23 页只有 1 条索引项目

- 第 22 页“官方依据索引”最后一条 `DB12/13：OpenAPI 规范目录、Arrow 列式格式。` 被单独排到第 23 页。
- 第 23 页除这 1 行外近整页空白。此问题由第二次布局变化产生，不能因旧第 14 页已消除而忽略。
- 建议在第 22 页局部回收约一行空间，使最后一条索引回到同页；随后复看第 22 页及后续章节开头。

### F02：长字段词中换行的轻微余项

- 第 27 页旧 `funding_observation` 末尾 n 和 `seat_disclosure_row` 末尾 w 孤字问题已经修正。
- 第 33 页 `account_action_rank` 已完整显示，但 `within_sector_alpha_rank` 的末尾 k 单独占一行。
- 第 19 页 API 列的 `instrument_id`、第 31 页 `attention_price_divergence` 仍在词中换行；字符没有缺失，属于轻微可读性问题。
- 若继续修，建议仅在文档渲染中给下划线/斜杠增加适当换行机会，或局部调标识符字号/列宽；不要修改权威字段名。

## 来源 3.1 / 3.2 语义复核

本次同时只读核对了 `contracts/v3.schema.json` 的 `FieldQualityEntry`，以及公共 `ingest.raw_object`、`ingest.observation` 和扩展 `intelligence.document_revision` 的 SQL 声明。这是字段语义对读，不是数据库执行测试。

### 已对齐

- 第 25–26 页将原文身份和位置明确归到 `ingest.raw_object.raw_object_id/source_id/endpoint_id/source_record_key/storage_uri/content_hash`，规范记录归到 `ingest.observation.observation_id/schema_version/mapping_version/observation_revision/quality_status`；这些字段确实存在。
- 新闻修订明确使用 `document_revision_id/document_id/source_record_id/revision`；没有再要求所有对象增加同义 `record_id` 或 `raw_object_uri`。
- 第 26 页实际 `available_at` 与历史派生 `policy_usable_at` 明确分离，未把公告日期改写成真实可用时点。
- 第 26 页补齐血缘明确使用 `FieldQualityEntry.value_origin/source_id/raw_object_id/mapping_version/conversion`；另源补齐使用 `value_origin=alternate_source`，不另造 `donor_source_id`。
- 第 34 页中文角色已修正为“持有”，与正式 holding 对齐。

### C02：缺失原因仍写成 missing_reason

第 26 页 §3.2 首句仍写“缺失字段用 null + missing_reason”。正式 `FieldQualityEntry` 的字段是 **reason**，并在 required 内；Schema 不包含 `missing_reason`，且 `additionalProperties=false`。

建议改为：**“缺失值为 null，FieldQualityEntry.status=missing，并填写 reason。”** 这样缺失值、字段状态与原因全部对应正式合同，不留下可被实现者误作新字段的别名。

## 检查边界

本报告只证明第二次渲染中指定的 22 页已查看。主控报告第 13 页已复核；本代理没有再次查看该页。对于后续新渲染，需对变化页另行复核，不能将本报告视为尚未生成图像的验收。
