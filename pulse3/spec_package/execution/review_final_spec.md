# 最终总规范与集成合同独立审阅

审阅者：统一执行与记账专项 Agent。日期：2026-11-10。

## 1 范围与方法

逐段读取 `spec/FINAL_SYSTEM_SPEC.md`、`database/database_design.md`、`sources_sectors/sources_sectors_design.md`、`execution/execution_standard.md`。随后审阅集成DDL与正式v3合同，检查业务对象、接口、数据库关联、数值政策、枚举和发布代次。

本审阅不重新声称已实现整站、真实供应商接入、真实撮合或数据库吞吐。发现分为明确冲突、会影响实现的遗漏和已声明的后续集成工作；没有把所有待实现项都改称缺陷。

## 2 总规范审阅结论

产品定位一致：这是A股选股与个股交易计划平台；Agent用于开发协作；运行期语言模型是有版本、输入输出和时限的分析能力。全市场、板块内个股、板块总榜与个股工作台使用共同来源、特征、策略和发布对象。已实现的参考程序与未实现的生产接入有明确区分。

以下问题在本轮审阅中反馈作者，作者修订后已经再次读取对应段落确认。这里的“关闭”指规范口径已关闭，不代表其生产接口已经部署。

| 编号 | 原问题与影响 | 修订与复核结论 |
| --- | --- | --- |
| FS-01 | 总规将“无可卖库存”列为不展示“现在可买”的理由，普通空仓买入因此可能被错误禁止 | 总规§6现按方向区分：买入检查现金/权限，卖出检查可卖库存，做T分腿检查。已关闭 |
| FS-02 | action_rank被定义为当前可行动排名，但READY发布内的ranking_entry不可变；只固定ranking_id/release分页无法容纳任意实时重排 | 总规§6/§11现将发布时action_rank冻结；实时account_action_rank采用ActionabilitySnapshot，绑定账户、报价、政策与有效期，分页固定actionability_id。规范已关闭，集成表/合同另审 |
| FS-03 | 执行专项明确真实成交必须保存，但总规范只写共用内核，容易被误解成实际成交也须重新经过模拟撮合/资源拒绝 | 总规§8现明确先保存原始成交事实，缺订单/库存/序号依赖时进入对账，不丢弃实际成交或负债。已关闭 |
| FS-04 | decision_as_of、模型完成、实际发布和报价覆盖层虽然都有字段，但没有直接表达派生结果最早可用边界 | 总规§3现要求observed模式下available_at不早于所有依赖available_at和computed_at最大值；输入截点与实际发布分开，历史重建另声明政策。已关闭 |
| FS-05 | “同模型、参数、seed必须重现”容易把外部模型重调用也包含进确定性保证 | 总规§7及执行专项现以保存的结构化模型输出和内容hash重放，再运行确定性计划/账本；不保证外部服务重新调用逐字一致。已关闭 |
| FS-06 | 总规说v1/v2仅历史，执行专项原句却允许旧共享特征参考继续独立保留，可能形成两套权威路径 | 总规§1.2与执行专项§7已统一：只有正式迁入v3 reference_features、统一字段并登记authority_manifest的副本有效；旧账户与旧路径归档。已关闭 |
| FS-07 | 一个“T+1”词可能被实现成证券可卖、卖出款可复用、现金可取三种约束共用同一开关 | 总规§7.2现分别引用MarketRules、CashUsePolicy与交收事实；与现有账本可卖批次、应收应付、现金复用一致。已关闭 |

没有发现把开发Agent改成开放自治交易产品、将社区传闻硬编码为席位必然行为、把轮询描述为恢复供应商从未提供的数据、或把论文收集等同策略已经有效的承诺。

## 3 跨专项命名和调用顺序

### FS-08：价格基准字段

数据库文字曾用adjustment_basis，而实际映射样例使用adjustment_mode。已反馈合同作者并由其统一为adjustment_mode。最终以v3 Schema及显式Repository映射为准，不保留两套同义公开字段。

### FS-09：manifest发布顺序

数据库文字曾简写为“同事务写READY manifest、依赖和outbox”。但新增guard_ready_dependency会拒绝向已READY主行添加依赖，若开发者按文字顺序先INSERT READY再写依赖，就会失败。

已复核作者修订：先建立COMMITTED_UNPUBLISHED manifest及依赖，完成检查后最后切换READY并同事务写outbox。已区分“整体同事务”与“事务内任何顺序都合法”，规范口径关闭。

## 4 集成DDL冷审记录

集成文件生成过程中发现以下具体问题，已反馈作者并复读最终函数、约束与说明。下列分析为静态约束/控制流分析，没有执行PostgreSQL事务。

| 编号 | 具体路径 | 最终复核 |
| --- | --- | --- |
| INT-01 | actionability_snapshot.ranking_id与release_id只有独立FK，能分别指向两个不同发布包 | 已增加(ranking_id,release_id)复合FK；guard要求分析为READY/SUPERSEDED、报价manifest为READY且均在checked_at之前可用。静态已关闭 |
| INT-02 | publish_release_stage命中既有发布记录时只核event_id，改变p_release_hash仍会返回旧成功 | 既有发布重试同时核event_id和原release_hash；不同内容报错。静态已关闭 |
| INT-03 | guard_league_pairing只检查NEW所属轮次，且没有DELETE触发；完成轮次的条目可以移到开放轮次或被删除 | 已禁止改变配对所属联赛/轮次；DELETE检查旧轮次并锁父行；已关闭/失败轮次本身也不能重新开启。静态已关闭 |
| INT-04 | 数据库专项提到回滚到旧release，但普通stage要求BUILDING、发布release唯一，并禁止as_of倒退，不能用同一路径合法记录回滚 | 已明确普通stage只做前向发布；独立版本化rollback命令/事件列为待实现。承诺与实现边界已关闭，回滚功能本轮未完成 |

已确认的基础对齐：账户FK指向execution.account_head，版本FK指向(account_id,account_sequence)；opening序列0可以通过outbox版本0；实时可行动性另有不可变结果引用，未改写原排名；stage捕获不可变base pointer与签发代次，旧任务不能通过重试重新获取新base。

研究上下文输入封存、已完成实验和指标结果封存也已加入：先建未封存context，写齐READY输入后最后seal；封存后禁止改输入，实验必须引用sealed环境；已完成实验和指标不可原地改写。这些是静态控制流结论，事务竞争仍需真实PostgreSQL验收。

## 5 正式合同独立复核

唯一正式合同位于`contracts/v3.schema.json`、`contracts/openapi.json`、`contracts/api_bindings.json`及`contracts/naming_migration.json`。旧integration目录合同副本已移除，避免两套权威。

`review_contracts_static.py`实际检查通过：80张表；149个FK的来源列、目标列、数量、精确声明SQL类型及最终目标唯一键；142个Schema定义；31个API操作、30个路径；OpenAPI DTO规则与正式Schema相同；所有局部引用及API所列物理表可解析。目标唯一性同时考虑PRIMARY/UNIQUE约束和非部分、普通列唯一索引，不会把raw_object的独立唯一索引漏报为缺陷。另通过18个独立数值/时间/枚举边界样例。最终文件哈希与完整FK列表保存在`final_contract_review.json`。

这些检查不把“API映射到表”理解成每个接口只能读一张表；业务查询可以组合多个领域表，但其写入所有者和语义合同必须唯一。接口路径存在也不表示HTTP处理器已经实现。

| 合同问题 | 最终口径 |
| --- | --- |
| 元数据使用旧data_manifest_refs/reason_code | 正式字段为ResponseMeta.input_snapshot_refs、Error.code；补generated_at和可空trace_id |
| 置信水平误用包含0/1的概率类型 | confidence_level使用开区间(0,1)，与SQL一致；排序分数与概率仍分开 |
| passed验证无结果引用 | PlanValidation passed必须有result_ref和validated_at，不能凭状态字符串冒称验证通过 |
| 条件Clock过度简化 | quote、bar_close、session_open、event_available、scheduled使用各自类型对象；bar_close含周期和会话政策 |
| 特征在编译时锁死旧值 | FeatureOperand保留语义引用和initial_feature_key_hash；触发时按明确解析政策获取新FeatureKey |
| FeatureKey无条件排序instrument_ids | 保留已声明顺序；只有特征定义明确无序集合时才排序，并进入definition_version |
| partial论文阅读被归为全文 | read_depth包含full_text_sections；不把部分正文阅读称全文复现 |
| 账户损益缺股息和成本起点桥接 | AccountProjection已含dividend_income_cny、pnl_from_cost_origin_cny、cost_basis_cny；成本起点损益不等于任意区间收益 |
| 实时价只有K线入口 | 新增CanonicalQuote、QuoteSnapshot、getStockQuote与可空Workspace.quote_overlay；源时刻可空，另有first_seen/available、live_quote_version与freshness评估时间 |
| Workspace冻结hash与实时覆盖层混用 | 冻结artifact中quote_overlay/actionability_id为null；API可叠加独立报价与账户结果，projection_hash不改写，响应时间不变成新分析发布时间 |
| 热表行可变与不可变快照事实混淆 | bar_projection/quote_projection明确定为可丢弃重建的热投影；canonical对象和manifest是事实，读服务须核实artifact/hash与完整覆盖。DDL本身没有宣称热行已经不可变封存 |

物理available_at始终保存实际处理完成时间；历史重建仅在版本化查询政策下派生policy_usable_at。源缺时间、缺报价或缺L1保持null，不能用收盘价、抓取日期或零金额伪装已知。

## 6 合成纵向验收

`synthetic_vertical_slice.py`已离线实际运行，使用正式v3合同生成同一release下的市场榜、板块内榜、板块总榜、TradePlan、冻结Workspace与图表窗口，再叠加晚于发布时间的独立报价和ActionabilitySnapshot。报价最新成交价9.99与已完成K线收盘价10故意不同，防止把bar close直接冒充实时价。冻结工作台hash保持不变，动态覆盖层带独立身份。

账务部分直接导入既有唯一Ledger并复用手算B01事件，没有另写撮合器或第二套现金/成本公式。可读样例证券ID显式映射为稳定UUID；API时间序列化为UTC Z，Decimal的有符号零序列化为0，经济值未改。初始账户没有库存，普通买单仍可通过现金预留探针；该探针使用独立Ledger实例，不占用原账户资金。

实际结果为28次DTO/响应Schema校验、11个API响应样例、1个既有golden场景，并检查发布、策略、证券、计划、账户、报价与评估引用。28不是unittest方法数，新增unittest方法为0。B01终值10,090元、已实现毛收益100元、费用10元、净损益90元、成本基础0，区间无外部现金流的TWR为0.009。Evaluation引用终态账户结果、政策和输入快照。

产物为`fixtures/artifacts.json`、`fixtures/result.json`及`synthetic_vertical_validation.log`。所有证券、报价、成交与时钟均明确标为合成；场景的后续日期不是已观察到的市场。Evaluation在场景后封存执行，是会计一致性检查；训练区间为结构占位，没有拟合模型或执行研究晋级。样例只证明合同引用和既有账本可以贯通，未实现完整PlanCompiler/ConditionRunner、供应商、数据库事务、HTTP服务、真实成交或策略盈利验证。

## 7 账本字段字典

`execution/field_dictionary.json`补齐7表68列，逐列解释业务语义、单位和语义来源，并与ledger_schema.sql的实际AST列集合校验一致。它补齐了总字段字典中原有67处无业务说明项，另保留已说明的reserved_cash字段。最终`integration/all_fields.json`已合并；独立脚本核对80表883列的集合、类型与非空业务说明，未解释字段为0。重点区分数量资格批次与成本账、可卖库存与卖单预留、活动剩余数量与累计成交、经济科目与可用/可取视图，以及created_xid与账户事件序号。字典没有改变账本代码或另增计算口径。

## 8 本次交付边界

数据库实库迁移、触发器并发、供应商实时连接、模型部署与成本评测、完整PlanCompiler、真实撮合/券商、整站性能仍按各专项列出的生产门槛实施。此轮审阅不会用合成fixture覆盖这些未完成状态，也不会因为存在明确边界而要求无关的额外架构或权限流程。
