# v3.1 合并包来源、候选目录与数学语义最终冷读

审阅日期：2026-11-11。对象：`deliverables/Pulse_Rewrite_Package_v31_build`。本报告检查本轮新增内容和 P01–P50 决议，不把设计采纳、目录登记、局部参考程序或论文阅读写成真实数据接入与收益验证。

## 1 结论

本轮内容校核完成，目录与来源计数成立，新增候选保持关闭生产。P01–P50 的外部事实、项目推理及旧仓库证据已分开映射；没有把附件的阈值、论文名、性能数字或席位传说改写成已验证事实。具体修正已保存到 staging，内容编辑现已冻结，可以生成并检查 Word。

独立静态/引用/计数检查 **21 项通过**；随包目录验证器 **20 项通过**。这两组是不同检查范围，不应加成“41 个独立交易效果实验”。没有重新执行旧仓库的策略实验，也没有训练论文模型或连接付费数据源。Word 视觉检查另行进行。

## 2 独立复核的精确计数

| 对象 | 结果 | 含义 |
| --- | --- | --- |
| 策略目录 | 61 条 | S001–S060原条目结构完全未改，新增S061 |
| 因素目录 | 105 条 | F001–F096原条目结构完全未改，新增F097–F105 |
| 核心论文 | 46 篇，文件逐字节不变 | 未将补充摘要核查冒充新增核心精读/复现 |
| 补充论文线索 | 10 个名称：9已识别、1未唯一识别 | Stock-Pretrain保留unresolved；核心计数变化0 |
| 来源索引 | 231条记录、231个唯一ID、218个不同URL | 同一官方页面被不同证据目的引用，不能将231说成231个独立信息源 |
| 决议 | P01–P50共50条 | adopt4、revise29、covered3、reject14 |
| 决议证据映射 | 50条完整，31条有直接相关外部来源 | 共引用53个不同现有source ID；纯设计推理不硬凑外部引文 |
| 分类与职责 | 41个facet、6个role | holding_overlay仍为holding形式，不新增第7职责 |

核心论文文件SHA-256：`9c81b4887c0426d68283d6e2a9477e907f2ba96daf14bc428dcf5965025e047d`，与v3原包及baseline_v3一致。本任务验证的是不改写原证据，不声称本轮重新阅读全文46篇。

所有目录`source_refs`均可在索引解析，所有已声明`evidence_file`路径存在，策略依赖的因素ID均可解析。S061仍按保留的UUIDv5政策生成。全部61策略均`not_implemented/unvalidated`且生产路由关闭；F097–F105均`not_implemented/unvalidated/not_connected`且显式关闭生产。

## 3 本次冷读直接修正的具体问题

### F103 债股残差的数学定义

原新条目说“历史模型残差除以过去残差尺度”，缺少零分母、拟合秩和收益窗口的明确处理。补入：收益定义、价格基准、同步窗口与报价容差；过去窗口拟合的alpha/beta、估计器、样本数和可得时点；残差尺度定义和自由度；预登记最小样本必须大于参数数目。尺度零/近零/非有限或拟合秩亏返回缺失/无效及UNKNOWN条件，不用任意epsilon制造极大信号。

同时区分同期相对残差与领先预测；后者需要独立目标、滞后和费用后样本外验证。新增`ConvertibleEquityResidual.v1`为`required_contract_not_yet_registered`，不冒称现有SQL已含这些列。上交所转债规则只支持市场机制和单位，不能支持“领先正股1至3分钟”或无风险套利。

### F104 题材状态的类型一致性

原定义允许“若输出概率”，而目录类型已经是`probability_vector`。现在固定本候选输出概率向量：须先登记标签、目标互斥与穷尽性、标签生成时间、向量顺序、有限区间、总和容差及校准版本。若只得到启发式类别或分数，必须另行登记定义，不能装进此概率类型。

无可用状态时，明确`value=null`、`field_status=missing/invalid`、`quality_status=partial/invalid`、`condition_value=UNKNOWN`；不新增`unknown`质量枚举。概率向量合法也不自动表明已校准，更不直接指定买卖方向。

### F105 个股预测与板块聚合的边界

当前严格`CommunityForecastPayload`针对`instrument_id`。目录的sector范围现在明确为待登记的个股评分聚合，须冻结分类/成分、目标与期限、权重、去重、样本门槛及未到期分母；不把板块ID塞进证券ID，也不声称已支持直接板块预测payload。

补入来源三元定位、单条Brier与聚合舍入政策、样本选择及结果可得性。前向采集组要求原预测在结果发生前保存；后来发现、原始时点证据可核验的历史预测可以单列回顾组，保留真实接收/available_at，不能混入前向组或倒填时间。低平均Brier不单独证明概率校准，更不等于交易增益。

### 新增候选生产状态

F102–F105补上`production_route_enabled=false`，与已有实现/校验状态一致。未修改原96因素和60策略条目，也未触碰`contracts/`或`reference_extensions/`。

### 论文日期字段证据强度

再次打开arXiv版本记录后，确认此前5个日期均为页面`Submitted on`记录。将补充表字段从`first_public_date`改为`first_arxiv_submission_date`，另记`first_public_availability_date=null`与证据范围。提交日不能直接当第一次公开可见时刻，更不能充当系统真实`available_at`。同步研究说明及增补正文的TradingAgents/FinMem措辞；P41决议原本写正式发表年份，保留其已合并的引用和证据字段。

## 4 新方法和论文的关键边界复核

- F097的两个Z分数相减不自动仍为单位方差：`Var(Za−Zf)=2−2ρ`。同窗、资金方法、样本门槛、零方差和冻结校准均已写入；阈值2不自动对应概率、身份或卖点。
- F098将rank 1定义为最好，使用固定母集的百分位改善；母集变化须重建比较身份。累计流量二阶差分与区间流量导数不混用，跨会话和计数器重置不制造“加速度”。
- F099有独立事件锚点，区别F078会话VWAP偏离；金额/股数参考价不等于特定资金真实持仓成本。
- F100是价位成交分布；仅bar输入必须声明估计，反复成交不等于剩余股份。F101是换手留存模型，归一与敏感性均属模型假设，不能解释为真实持仓数据库。
- S061是holding库存覆盖候选。附件网格常数只作为trial，ATR量纲、底仓可卖额度、两腿费用、未配对库存、重锚和统一期末财富均有验收要求。
- 匹配随机基准必须匹配当时母集、日期、资金、成本、换手及暴露。1000次为预算候选；有限随机p值依赖零假设/可交换等条件，不解决所有多次选择问题。

补充9个已识别工作中，RAM、TradingAgents、FinMem在v3已有历史线索；其他6项在本轮新增核校表中登记，仍未增加核心46篇。MERA/HRFT是WWW2025 Companion，DeepScalper是CIKM2022，FinMem正式发表于2024 Symposium。SAMBA的图对象是日频特征，不是A股供应链个股先后手图。Hawkes工作是记忆滤波与PPO做市；RAM的财富比较保证不是最大回撤上界。具体读取深度保持原记录，未扩张为完整训练复现。

## 5 P01–P50的来源支撑与旧代码证据

`decision_evidence_map.json`已被root合并到决议JSON和正文。每条只将直接支持相应外部事实的source ID加入`source_refs`，并用`evidence_note`写清该来源没有证明什么。纯项目选择、公式推导和反例保留空引用。未因为同一来源多个ID而增加论文数或独立消息源数量。

P05四个官方版本公告本轮再次打开：DuckDB2.0于2026-10-21发布，Polars2.1于10-27发布；Pydantic10-30说明V3尚无发布日期；PG19仍为RC，GA计划11-19。版本存在支持评估，不表示本包已运行这些版本的负载或迁移。

P02/P20独立只读检查Git提交`6f5c57f8df73c58f0340dab6977b95c05d21740c`：剧本路径确为`backend/app/playbooks/picks.py`；JSONL可解析92条，最后E092；E048/E049/E051席位研究存在，E051明确记载与旧主书几乎无交集。没有重跑该实验或认可旧收益为新系统成绩。当前提交树未找到`calendar_sync.ps1`，`git grep`未找到`15:33:33`；这仅说明本次可检索提交未定位，不证明所有历史版本不存在。

P18/20/21/22/23分别区分资金流算法代理、龙虎榜披露金额、最终投资者身份、ETF二级成交与一级申赎、转债制度与领先假说。P34的GIPS与FIX用途分开，P35不把交易所向会员收费等同完整客户佣金。P27的等级应读作未来校准要求，并无既成胜率。P38的Calmar估计窗口/年化/零分母须由项目明确，不能让IR或ICIR文档替所有Calmar实现背书。P46的库功能与项目数据接入/图层实现严格分开。

未发现需要否决当前50条决议的新增外部事实错误。P44与图层参考仍只验证传入可信上下文；生产Repository必须将每个输入artifact绑定到能力、证券集合、窗口、完整性和实际可得时间，不能由全局拥有L2能力推出当前文件含L2。该待实现边界已在正文中明确。

## 6 输出与复查材料

- `research_v4/final_evidence_results.json`：21个独立静态检查、修改范围、核心文件哈希及本轮再次打开的官方页面。
- `research_v4/decision_evidence_map.json`：50项来源/内部证据/推理映射；原始审阅时的决议文件哈希记录在内，root随后合并字段不会反向改写该审阅快照。
- `research_v4/extensions_independent_review.md`：之前对严格payload参考的独立反例和已修问题；不将其作为正在继续变化的生产Repository实现证明。

本轮重新打开的一级来源供最终作者复查：

| 主题 | 官方URL | 检索ref |
| --- | --- | --- |
| DuckDB2.0 | https://duckdb.org/2026/10/21/announcing-duckdb-200 | turn192view0 |
| Polars2.1 | https://pola.rs/posts/polars-2-1-changelog/ | turn192view1 |
| Pydantic计划 | https://pydantic.dev/articles/pydantic-validation-next-steps | turn192view2 |
| PG19RC | https://www.postgresql.org/about/news/postgresql-19-rc-1-released-3409/ | turn192view3 |
| SAMBA提交记录 | https://arxiv.org/abs/2410.03707 | turn195view0 |
| DeepScalper提交记录 | https://arxiv.org/abs/2201.09058 | turn195view1 |
| Hawkes工作提交记录 | https://arxiv.org/abs/2610.16524 | turn195view2 |
| FinMem提交记录 | https://arxiv.org/abs/2311.13743 | turn195view3 |

此前24个方法一级来源和逐篇读取边界仍在`methods_sources.json`及合并后的`research/source_index.json`。没有将未成功下载的PDF写成已阅读全文，没有分发作者PDF。
