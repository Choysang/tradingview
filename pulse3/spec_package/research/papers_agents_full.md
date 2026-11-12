# 2026 金融 Agent、因子、预测与基准：证据台账与 A 股适配

检索截止：**2026-11-10**。本文件与 `papers_agents.json` 一一对应，供 A 股交易研究网站重写方案的论文、数据与模型决策使用。

## 1. 覆盖与证据口径

当前登记 **23 篇核心文献**，其中 **17 篇定向阅读全文的方法、实验或附录，5 篇仅摘要，1 篇为摘要及出版方节选**。这里的“定向全文”不是逐字阅读全部附录。另有 **7 条待研究候选**，不计入 23 篇。

**2026 发表/录用与 2026 首次提出必须区分。**本次核实到 8 篇在 2026 年提交 arXiv v1。其中 Biased-Consensus 的 ICML 正式发表早于其 arXiv v1，因此最早公开日仍未知。按最早定位的作者稿分组：7 篇为 2026、8 篇早于 2026、8 篇最早日期未确认；两种口径不可直接相加。arXiv v1 日期也不保证比会议网站、SSRN 或作者主页更早。尤其 JFE 2026 新闻论文源于 2023，Kronos、VTA、AlphaSAGE、FinSearchComp、FinRpt、TiMi、FinAuditing 都有 2025 作者稿。任何“集成全部2026新论文”的表述都不成立。

技术事实取自正式 proceedings、论文、作者/机构页面和官方代码；第三方索引仅用于发现。多个来源互证出版状态或数据口径，并不等于多个独立团队复现同一收益。**本次没有运行作者训练或回测，所有数值均为作者实验设定或作者报告，不能当成本项目收益。**

NeurIPS 2026 的 Market Regime Council 截至截止日为 accepted/in press，会议尚未召开；ICAIF 2026 有官方录用目录，但这里若仅得到题名就保留在待研究池。AAAI 的直接页面被反爬，采用官方搜索索引的出版信息与作者稿交叉核实；不得把这种元数据核验写成已读正式 PDF。

采用等级：**P0** 为优先建设的证据/工具/评测基础设施；**P1** 为进入统一 A 股复现的研究候选；**P2** 为思想/待补证据候选。它们不是策略上架证书。

## 2. 最值得改变架构的结论

下面是基于文献作出的工程推断，具体论证见后续逐篇记录。

|决策|证据|直接影响|
|---|---|---|
|共同事实与工具契约|AP03/AP07/AP21/AP22|证券、期间、单位、时点、版本成为共享数据接口的一部分；新闻、选股、个股与回测用同一事实。|
|把消息解释和可交易收益分开|AP08/AP23|重要性、方向、持续期、首发后漂移、加入实际延迟后的收益分别建模。|
|多 Agent 的价值来自新增信息|AP19/AP20/AP12|先独立判断再互评；按样本外增量、误差相关和校准融合，不能把同意票数当独立证据。|
|因子进化记住失败与相关簇|AP01/AP04/AP05/AP06|同时约束候选重复、表达式复杂度、算力预算和经济收益；所有尝试入试验台账。|
|保存状态与可执行计划版本|AP11/AP17|同时允许程序策略和实时 Agent 计划，比较同条件的实际增量；每次更新可回放。|
|每个模型匹配自己的目标|AP02/AP13/AP14/AP15/AP18|技术解释、横截面排名、波动、市场状态与订单执行分开；统一融合但不混淆标签。|
|证明大模型提供了什么|AP10/AP12/AP23|价格-only/新闻-only/组合消融，风格归因，冻结前瞻预测；报告质量不能替代净收益。|

### 2.1 同模型多 Agent 为何不能自动形成多方校验

ICML 的共识论文说明特定互动会放大共同偏差；MRC 的消融又显示两两协作未必优于最佳单体。它们不证明辩论总是无效，而是支持一种可测设计：先存储各专家独立的证据与判断，再允许互相提出具体反证；评估新增轮次究竟提高了什么。模型换了名字、写了不同角色提示，并不确保数据或错误彼此独立。AP19 是通用方法研究，AP20 是加密资产实验，不能借此宣称已有 A 股验证。

### 2.2 把论文成绩转成网站功能前必须对齐语义

- AP23 的约 90% portfolio-day 命中率针对不可交易的初始反应，不是可买点胜率。
- AP06 的 10 分钟收益预测、AP14 的 15 分钟简单多空回测，不能直接给普通 A 股新买仓生成当日卖出计划。做 T 的动作依赖可卖底仓。
- AP11 主评估零交易费；AP20 主表也零费，但另有 5/10bps 敏感性。零费表、扣费表与实际成交应分列。
- AP14 的 400 个专有特征即使提供匿名矩阵，也未解决在线生成这些特征的复现问题。
- AP18 的市场状态主要反映波动/相关结构，不能直接等同涨跌预测。
- AP08 网页摘要为 6 股，正式 PDF 为 21 资产。采用 PDF，并保存冲突，而不是选更好看的数字。

## 3. 核心文献索引

|ID|简称|出版/录用状态|最早核实作者稿日期|阅读深度|采用|
|---|---|---|---|---|---|
|AP01|AlphaBench|ICLR 2026 Conference；正式主会论文|未确认|定向全文|P0|
|AP02|VTA|ICLR 2026 Conference；正式主会论文|2025-11-06|定向全文|P1|
|AP03|FinSearchComp|ICLR 2026 Conference；正式主会论文|2025-09-16|定向全文|P0|
|AP04|AlphaSAGE|ICLR 2026 Conference；正式主会论文|2025-09-29|定向全文|P1|
|AP05|AlphaMaster|KDD 2026 V.2；正式会议论文；track 待核实|未确认|仅摘要|P2|
|AP06|FactorMiner|KDD 2026；正式会议论文；track 待核实|2026-02-16|定向全文|P0|
|AP07|Fin-RATE|KDD 2026 Datasets and Benchmarks；正式 Datasets & Benchmarks|2026-02-07|定向全文|P0|
|AP08|FinRAQ|Findings of EMNLP 2026；Findings（非主会）|未确认|定向全文|P1|
|AP09|FinEvo|11th FinNLP Workshop 2026；Workshop（非主会）|未确认|仅摘要|P2|
|AP10|KTD-Fin|arXiv；预印本|2026-05-27|定向全文|P0|
|AP11|AlphaForgeBench|KDD 2026 Datasets and Benchmarks；正式 Datasets & Benchmarks|2026-02-10|定向全文|P0|
|AP12|AgenticFinBench|arXiv；预印本|2026-11-03|定向全文|P0|
|AP13|PRISM-VQ|IJCAI 2026 Main Track；正式主会论文|未确认|定向全文|P1|
|AP14|TransAlpha|IJCAI 2026 Main Track；正式主会论文|未确认|定向全文|P1|
|AP15|Kronos|AAAI 2026 Technical Track；正式主会论文|2025-08-02|定向全文|P1|
|AP16|FinRpt|AAAI 2026 Technical Track；正式主会论文|2025-11-10|定向全文|P1|
|AP17|TiMi|ICLR 2026；官方会议目录确认|2025-10-06|定向全文|P1|
|AP18|Market-JEPA|ICML 2026 / PMLR 306；正式主会论文|未确认|仅摘要|P2|
|AP19|Biased-Consensus|ICML 2026 / PMLR 306；正式主会论文|未确认|仅摘要|P0|
|AP20|Market-Regime-Council|NeurIPS 2026 (accepted/in press; exact track unverified)；已录用/待出版；track 待核实|2026-05-23|定向全文|P1|
|AP21|FinAuditing|SIGIR 2026 Resource Track；正式 Resource Track|2025-10-10|仅摘要|P0|
|AP22|FC4FinQA|The Web Conference 2026 Industry Track；正式 Industry Track|2026-04-07|定向全文|P0|
|AP23|JFE-LLM-News|Journal of Financial Economics 184 (2026), 104335；正式期刊版|2023-04-15|摘要与出版方节选|P1|

## 4. 逐篇证据与适配记录

### AP01 · AlphaBench

**AlphaBench: Benchmarking Large Language Models in Formulaic Alpha Factor Mining**

作者：Haochen Luo、HoTin Ko、Jiandong Chen、David Sun、Yuan Zhang、Chen Liu。出版：ICLR 2026 Conference；正式主会论文。最早核实作者稿：未确认；本次使用版本：2026。

来源：[正式/官方入口](https://proceedings.iclr.cc/paper_files/paper/2026/hash/4f3820576130a8f796ddbf204c841487-Abstract-Conference.html)；[论文/作者稿](https://proceedings.iclr.cc/paper_files/paper/2026/file/4f3820576130a8f796ddbf204c841487-Paper-Conference.pdf)；[作者代码](https://github.com/CityU-MLO/AlphaBench)。阅读深度：**定向全文**；正式摘要、PDF 方法/搜索实验与数据附录、作者项目页和代码首页。

|核验项|记录|
|---|---|
|核心方法|把因子生成、因子质量判断、迭代搜索分开评测；以可执行金融 DSL 和统一回测比较提示、推理配置及搜索范式。|
|数据/市场/时间|附录因子数据集使用 CSI300、2021–2025；项目列出 1,857 条指令；不同子任务使用不同构造，不把它当单一训练/测试区间。|
|验证与基准|比较 Chain-of-Experience、Tree-of-Thought、Evolutionary Algorithms 及不同模型/提示；同时看有效性、搜索质量与多样性。|
|交易/计算成本|论文中的 search/retry cost 是研究计算成本；本次未核实交易费率，不能把成本效率等同扣费收益。|
|作者结论|作者认为 LLM 可辅助因子挖掘，但不用执行结果直接判断因子好坏仍较弱，搜索预算与多样性是关键。|

**适用边界：**基准能力不等于策略经济价值。 同一基准反复优化可能耗尽样本外信息；需另设本项目保留集。

**本项目适配（工程推断）：**先把自然语言研究假说编译为带类型与单位的因子表达式；所有模型调用同一 as-of 数据快照、算子定义和评估服务。语义解释只供研究，不替代收益验证。

模块映射：`research.factor_dsl`、`research.factor_search`、`evaluation.model_benchmarks`。采用等级：**P0**。

复现路线：

1. 固定作者版本与依赖，核对算子时间方向和缺失值语义。
2. 用统一 A 股日频/分钟数据复现三个任务，设置等 token/时延/试验次数预算。
3. 再以未接触的滚动区间检验成本后边际贡献和相关性。

需要数据：历史动态指数成分；OHLCV/复权事件；因子公式与生成轨迹；统一交易成本模型。

未决事项：正式首公开日期未核实。 尚未运行官方代码。

### AP02 · VTA

**Reasoning on Time-Series for Financial Technical Analysis**

作者：Kelvin J. L. Koa、Jan Chen、Yunshan Ma、Huanhuan Zheng、Tat-Seng Chua。出版：ICLR 2026 Conference；正式主会论文。最早核实作者稿：2025-11-06；本次使用版本：2026-03-02。

来源：[正式/官方入口](https://proceedings.iclr.cc/paper_files/paper/2026/hash/68c1b9784d982fa5575bb45ace7bd64e-Abstract-Conference.html)；[论文/作者稿](https://proceedings.iclr.cc/paper_files/paper/2026/file/68c1b9784d982fa5575bb45ace7bd64e-Paper-Conference.pdf)；[作者代码](https://github.com/chen-jan/VTA)。阅读深度：**定向全文**；方法、实验数据与 CAPM 附录；作者代码首页。

|核验项|记录|
|---|---|
|核心方法|将价格序列转为文字标注，训练语言推理，再把推理属性作为时间序列骨干的条件；推理奖励与预测误差相关。|
|数据/市场/时间|StockNet 88 只美股，2012–2017；额外 Dow Jones、China A50、EURO STOXX50 数据，测试 2024-01-01 至 2025-01-01。|
|验证与基准|预测误差、解释评价以及附录收益/市场 beta 分析；附录中 alpha 的统计显著性只在 China A50 达成。|
|交易/计算成本|本次未在阅读部分确认完整交易成本/成交假设。|
|作者结论|作者报告跨市场预测准确度和专家解释评价改善；附录结果不支持所有市场都有显著 alpha。|

**适用边界：**优化 MSE 不自动优化排序与成本后收益。 解释流畅与实际因果归因不是同一指标。

**本项目适配（工程推断）：**做技术分析专家：数值指标由统一行情算子计算，模型解释趋势和情景，并输出预测分布、适用周期与反证；与纯时序/树模型按同数据比较。

模块映射：`stock.technical_analysis`、`experts.time_series_reasoning`、`forecast.multi_horizon`。采用等级：**P1**。

复现路线：

1. 固定序列标注和模型版本，先复现预测与解释两套指标。
2. 在 A 股用 1/5/20 日与分钟窗口分别标定，比较有无语言推理的增量。
3. 接入真实持仓与费用后再测买卖策略。

需要数据：完整未收盘/已收盘 bar 标记；统一复权行情；专家解释评分集。

未决事项：完整成本协议需进一步核对。 作者姓名在会议页面顺序/缩写与 arXiv 有差异，本记录使用作者稿姓名。

### AP03 · FinSearchComp

**FinSearchComp: Towards a Realistic, Expert-Level Evaluation of Financial Search and Reasoning**

作者：Liang Hu et al.。出版：ICLR 2026 Conference；正式主会论文。最早核实作者稿：2025-09-16；本次使用版本：2026 conference version。

来源：[正式/官方入口](https://proceedings.iclr.cc/paper_files/paper/2026/hash/4d42358702dff82e1436550a05ade260-Abstract-Conference.html)；[论文/作者稿](https://proceedings.iclr.cc/paper_files/paper/2026/file/4d42358702dff82e1436550a05ade260-Paper-Conference.pdf)。阅读深度：**定向全文**；正式摘要、问题分类、实验与复现说明。

|核验项|记录|
|---|---|
|核心方法|用实时数据获取、简单历史检索、复杂历史调查三类任务测金融搜索与推理。|
|数据/市场/时间|635 问、70 位金融专家、全球及大中华市场；21 个模型/产品。属于检索任务集，不是股票回测时间窗。|
|验证与基准|比较具有不同搜索/金融工具配置的系统；区域与工具能力会影响结果。|
|交易/计算成本|关注检索正确性；无可直接迁移的股票交易成本设定。|
|作者结论|作者报告工具接入显著改善结果，模型与工具的地区适配差异明显。|

**适用边界：**排行榜是特定检索时间/产品配置的快照。 本次未完整核实每项最新问答的截止时刻与网页修订版本。

**本项目适配（工程推断）：**建立中文金融检索回归集：公告、宏观发布日期、跨公司财务口径、国际事件传导；每题冻结 answer_as_of、证据链接、单位和实体主键。

模块映射：`data.source_registry`、`research.search_agent`、`evaluation.retrieval_benchmarks`。采用等级：**P0**。

复现路线：

1. 按任务分类复现公开题集，不把今天答案用于历史题。
2. 增加 A 股官方来源、跨市场时区和同名证券消歧题。
3. 比较同模型不同工具、同工具不同模型，分别归因。

需要数据：网页/公告版本；发布时间与抓取时间；金融工具调用轨迹。

未决事项：作者列表随会议版/预印本有变化，未合并两版完整名单。 代码地址未由本次页面中可靠提取，保留 null。

### AP04 · AlphaSAGE

**AlphaSAGE: Structure-Aware Alpha Mining via GFlowNets for Robust Exploration**

作者：Binqi Chen、Hongjun Ding、Ning Shen、Jinsheng Huang、Taian Guo、Luchen Liu、Ming Zhang。出版：ICLR 2026 Conference；正式主会论文。最早核实作者稿：2025-09-29；本次使用版本：2026-05-19。

来源：[正式/官方入口](https://proceedings.iclr.cc/paper_files/paper/2026/hash/06115011841a8d500050261c0ac4b1e4-Abstract-Conference.html)；[论文/作者稿](https://arxiv.org/html/2509.25055v3)；[作者代码](https://github.com/BerkinChen/AlphaSAGE)。阅读深度：**定向全文**；方法、市场划分与基线；会议 PDF 太大，改读作者 HTML v3。

|核验项|记录|
|---|---|
|核心方法|RGCN 编码因子表达式图，GFlowNet 探索多个有价值表达式区域，密集多目标奖励同时考虑预测与多样性。|
|数据/市场/时间|中国 CSI300/500/1000：2010–2020 训练、2021 验证、2022–2024 测试；S&P500：2010–2016/2017/2018–2020。|
|验证与基准|与树模型、GP、AlphaGen、AlphaQCM、AlphaForge、AlphaAgent 等比较。|
|交易/计算成本|本次未核实交易成本完整参数；不能由 IC 表直接判断可交易性。|
|作者结论|作者报告公式搜索的多样性、新颖性与预测效果改善。|

**适用边界：**美国样本止于 2020。 生成分布多样性仍需在本项目用实际收益/暴露相关性检验。

**本项目适配（工程推断）：**作为因子候选生成器，和 GP/LLM 候选共享 DSL、预算与同一评估器；不能把不同公式文字当成独立经济信号。

模块映射：`research.expression_graph`、`research.factor_search`、`research.diversity`。采用等级：**P1**。

复现路线：

1. 重建同样算子库与分区，测搜索采样质量。
2. 以计算预算匹配 GP、随机搜索及 LLM。
3. 在盲测区间评估去相关后增量，而非只比较最佳单因子。

需要数据：动态股票池；因子 DAG/算子库；收益与风格暴露面板。

未决事项：会议与 arXiv 的部分作者顺序不同。 本次未执行官方代码或核实最新版本全部新增实验。

### AP05 · AlphaMaster

**AlphaMaster: Dual-Chain Feedback for Scalable and Diverse Alpha Factor Discovery**

作者：Haozengran Wang、Shuo Yin、Rong Fu、Mengting Zhang、Jiayi Zeng、Simon James Fong。出版：KDD 2026 V.2；正式会议论文；track 待核实。最早核实作者稿：未确认；本次使用版本：2026-08-08。

来源：[正式/官方入口](https://doi.org/10.1145/3770855.3818120)。阅读深度：**仅摘要**；ACM DOI 的正式元数据与摘要；未取得全文。

|核验项|记录|
|---|---|
|核心方法|生成链与优化链分工，联合 RL、GFlowNet、LLM 符号生成和 GP；用相关性约束减少同质因子。|
|数据/市场/时间|摘要：CSI500/CSI1000，2010–2025。|
|验证与基准|摘要报告 IC、RankIC、AR、IR 等比较；逐项训练/验证/测试与基线预算未核实。|
|交易/计算成本|摘要称 realistic trading constraints；费率、滑点、停牌与成交实现未核实。|
|作者结论|作者声称多项指标优于比较方法；本记录不将其数值当项目目标收益。|

**适用边界：**没有全文验证，暂不能确认回测协议和数据时点。 组合多种生成器本身不证明收益独立。

**本项目适配（工程推断）：**借鉴研究编排：多生成器提供候选，统一评估器筛选，优化器只看训练/验证反馈；公共因子库记录相关簇与替换历史。

模块映射：`research.multi_generator`、`strategy.evolution`、`research.diversity`。采用等级：**P2**。

复现路线：

1. 取得正式全文、代码与交易假设。
2. 统一每个生成器预算，比较单生成器与组合生成器。
3. 测新增候选对现有池的边际贡献、衰减与换手。

需要数据：CSI 历史成分和行情；全部搜索尝试；多目标评估结果。

未决事项：全文、代码、确切 track 与拆分窗口未确认。

### AP06 · FactorMiner

**FactorMiner: A Self-Evolving Agent with Skills and Experience Memory for Financial Alpha Discovery**

作者：Yanlong Wang、Jian Xu、Hongkang Zhang、Shao-Lun Huang、Danny Dongning Sun、Xiao-Ping Zhang。出版：KDD 2026；正式会议论文；track 待核实。最早核实作者稿：2026-02-16；本次使用版本：2026-08-19。

来源：[正式/官方入口](https://kdd2026.kdd.org/papers/)；[论文/作者稿](https://arxiv.org/html/2602.14670v2)。阅读深度：**定向全文**；检索—生成—评估—蒸馏循环、接纳/替换规则、实验与计算性能。

|核验项|记录|
|---|---|
|核心方法|可执行研究技能加成功/失败经验库；快速 IC 筛选、池内相关性检查、批内去重、完整验证和近邻因子替换。|
|数据/市场/时间|CSI300/500/1000 与 64 个加密资产，10 分钟 bars；2024 训练、2025 测试，另检验至 2026-Q1。|
|验证与基准|比较随机公式、Alpha101、GP、AlphaForge、AlphaAgent 与无记忆消融；Top40 于 2024 固定后测 2025，指标主要 IC/ICIR/相关性。|
|交易/计算成本|性能是预测指标；GPU/C 吞吐结果排除 I/O，不能直接作为端到端线上延迟。|
|作者结论|作者报告搜索效率、因子质量及相关性控制改善；无需更新 LLM 权重，进化来自记忆与库更新。|

**适用边界：**10 分钟预测收益不等于 A 股买入后可同日卖出。 绝对 IC 汇总需与有符号、训练期固定方向的结果分清。

**本项目适配（工程推断）：**优先实现完整试验台账与失败记忆。日内因子先映射为机会评分；做 T 只使用可卖底仓，长周期选股另外构建目标。数据计算缓存共享，避免各 Agent 重算相同特征。

模块映射：`research.memory`、`research.factor_library`、`strategy.evolution`、`data.feature_compute`。采用等级：**P0**。

复现路线：

1. 保持相同算子/字段/候选数量，复现记忆消融。
2. 冻结方向和组合权重，增加费用、库存、限价可成交模拟。
3. 把纯计算性能与读数据/排队/传输延迟分别测量。

需要数据：10 分钟 PIT 行情；动态成分；因子执行轨迹与拒绝原因；库存和交易日规则。

未决事项：论文 HTML 未可靠找到项目代码链接；不自行猜测仓库。 官方列表确认论文；准确 track 仍待补。

### AP07 · Fin-RATE

**Fin-RATE: A Real-world Financial Analytics and Tracking Evaluation Benchmark for LLMs on SEC Filings**

作者：Yidong Jiang、Junrong Chen、Eftychia Makri、Jialin Chen、Peiwen Li、Ali Maatouk、Leandros Tassiulas、Eliot Brenner、Bing Xiang、Rex Ying。出版：KDD 2026 Datasets and Benchmarks；正式 Datasets & Benchmarks。最早核实作者稿：2026-02-07；本次使用版本：2026-06-10。

来源：[正式/官方入口](https://doi.org/10.1145/3770855.3817528)；[论文/作者稿](https://arxiv.org/html/2602.07294v4)；[作者代码](https://github.com/jyd777/Fin-RATE)。阅读深度：**定向全文**；数据构造、错误分类、任务及 RAG/给定上下文实验设计。

|核验项|记录|
|---|---|
|核心方法|区分单文档细节推理、跨企业比较、跨期追踪，并把检索、实体、时间与推理错误拆开。|
|数据/市场/时间|2020–2025 SEC 文件；43 公司、2,472 份申报、15,311 块；三任务各 2,500 例。|
|验证与基准|17 个模型，给定正确上下文与 RAG 两种条件，领域错误分类和评分。|
|交易/计算成本|问答基准；没有交易成本或投资收益结论。|
|作者结论|跨期和跨企业任务较单文档准确率明显下降，常见实体/时间错配与虚构比较。|

**适用边界：**美国 SEC 与中国财报格式、会计口径不同。 生成题目和评分器也需要人工抽检。

**本项目适配（工程推断）：**为公告/财务服务建立 entity_id、period、accounting_basis、unit、revision 约束；表格数值由程序复算，跨期比较必须记录重述版本。

模块映射：`data.financial_statements`、`evidence.claim_registry`、`evaluation.filing_reasoning`。采用等级：**P0**。

复现路线：

1. 先复现公开任务分类和错误分解。
2. 用 A 股年报/季报/更正公告构建同样三任务。
3. 对同一证据比较模型，对同一模型比较检索，避免混淆瓶颈。

需要数据：原始财报与表格；发布日期/重述时间；实体/会计科目/单位映射；证据段落坐标。

未决事项：未运行公开 benchmark。

### AP08 · FinRAQ

**FinRAQ: Financial News Reasoning through Importance Ranking and Socratic Questioning**

作者：Zee Hen Tang、Mi-Yen Yeh。出版：Findings of EMNLP 2026；Findings（非主会）。最早核实作者稿：未确认；本次使用版本：2026-10。

来源：[正式/官方入口](https://aclanthology.org/2026.findings-emnlp.1432/)；[论文/作者稿](https://aclanthology.org/2026.findings-emnlp.1432.pdf)。阅读深度：**定向全文**；正式 PDF 摘要/方法、实验和附录 A；与网页摘要交叉核对。

|核验项|记录|
|---|---|
|核心方法|NewsRank 以时间图的语义/时间双通道聚合新闻重要性；多空视角使用结构化 Socratic 提问相互检查假设。|
|数据/市场/时间|正式 PDF 为 21 资产（20 美股及 BTC）；训练 2022-03-01 至 2024-03-01，测试至 2026-03-01。|
|验证与基准|多个骨干模型与新闻/Agent 基线；每天决策，使用此前五天最多 50 条新闻。|
|交易/计算成本|允许做空；附录称滑点、借券与佣金同先前工作，未给出本次可核实具体数值。|
|作者结论|作者报告新闻排序和结构化提问改善收益等指标；不在此复述最高增幅作为普遍结论。|

**适用边界：**网页摘要写 6 股票，正式 PDF 写 21 资产；本记录按 PDF，保留冲突。 资产范围有限、模型训练记忆与历史新闻重叠尚需单独审计。 允许做空的基准与普通 A 股账户不同。

**本项目适配（工程推断）：**将消息重要性、方向、持续期分开建模；事件关系进入共享图，给新闻、选股与个股策略共同使用。辩论仅增加新证据或可检验反证时继续，用等预算单 Agent 作对照。

模块映射：`news.importance_ranker`、`news.temporal_event_graph`、`agents.evidence_debate`。采用等级：**P1**。

复现路线：

1. 先重建时间/语义边和新闻可得时间，再比较去掉 NewsRank/提问的消融。
2. 冻结新闻与模型版本，测新闻延迟、重复转载和跨市场休市场景。
3. 重跑长仓 A 股成本/可成交协议并比较单 Agent 同预算。

需要数据：新闻首发/转载/修订时间；实体和供应链关系；完整新闻正文；历史行情。

未决事项：首公开时间与官方代码地址未核实。 交易费率必须取得原实现确认。

### AP09 · FinEvo

**FinEvo: Financial Language Agents Shape the Markets That Evaluate Them**

作者：Mingxi Zou、Jiaxiang Chen、Langzhang Liang、Zhuo Wang、Aotian Luo、Chi Zhang、Danny Dongning Sun、Zenglin Xu。出版：11th FinNLP Workshop 2026；Workshop（非主会）。最早核实作者稿：未确认；本次使用版本：2026-10。

来源：[正式/官方入口](https://aclanthology.org/2026.finnlp-1.5/)。阅读深度：**仅摘要**；ACL Anthology 正式 workshop 摘要和出版元数据。

|核验项|记录|
|---|---|
|核心方法|把新闻驱动的异质金融 Agent 放入连续双向拍卖市场，并通过资本重分配使参与者种群变化。|
|数据/市场/时间|模拟冲击和历史新闻驱动的 128 次实验；本次未取得具体市场日期和逐项配置。|
|验证与基准|考察制度、市场状态、种群与创新机制对结果的影响。|
|交易/计算成本|仿真环境中的机制结果；真实交易费用/冲击迁移未核实。|
|作者结论|作者报告 Agent 的排名依赖其所处市场与其他参与者，移除创新会提高集中度。|

**适用边界：**模拟内生成的价格不是观察到的真实 A 股价格。 不能从仿真种群胜出直接推断实盘 alpha。

**本项目适配（工程推断）：**用于策略拥挤、新闻传播与流动性变化的压力实验；保留独立真实历史回放，评估一组策略同时使用后是否抵消收益。

模块映射：`simulation.agent_market`、`strategy.population_diagnostics`。采用等级：**P2**。

复现路线：

1. 取得全文与环境后核对撮合/消息时间及种群规则。
2. 与固定市场回放分别报告，先标定真实市场统计。
3. 以跨策略容量/拥挤变化作为研究输出。

需要数据：订单簿/成交统计用于标定；新闻时间线；Agent 种群参数。

未决事项：未读全文；不确认模拟收益可迁移。

### AP10 · KTD-Fin

**From Knowing to Doing: A Memory-Controlled Benchmark for LLM Trading Agents on Stock Markets**

作者：Taojie Zhu、Wentao Zhao、Rui Sun、Beidi Luan、Jiacheng Lu、Sinuo Wang、Jing Li、Daxin Jiang、Yonghong He、Zuo Bai。出版：arXiv；预印本。最早核实作者稿：2026-05-27；本次使用版本：2026-05-27。

来源：[正式/官方入口](https://arxiv.org/abs/2605.28359)；[论文/作者稿](https://arxiv.org/html/2605.28359v1)。阅读深度：**定向全文**；masking、执行协议、实验时间、市场/风格/选股归因与限制。

|核验项|记录|
|---|---|
|核心方法|股票标识和日期匿名化控制历史记忆，再用 Barra 风格归因区分市场、风格和选股贡献。|
|数据/市场/时间|CSI300；长窗 2024-01-01 至 2026-04-10；价格/技术指标输入，无新闻或财报；十个 LLM、18 个训练基线。|
|验证与基准|多随机种子、不同匿名与决策模式、分市场区间；传统模型 2015–2022 训练、2023 验证。|
|交易/计算成本|作者协议：多头、T+1、下一交易日开盘、买 5bps/卖 15bps、每单最低 5 元，另做涨跌停过滤；这是基准假设，不是实时费率表。|
|作者结论|在其价格输入和匿名实验中，LLM 收益多由 beta/风格解释，持续选股 alpha 证据有限。|

**适用边界：**不能据此否定有真实新闻和基本面输入的 LLM 增量。 匿名不保证抹除全部训练记忆；只有部分窗口严格晚于锚模型 cutoff。

**本项目适配（工程推断）：**给所有研究保留模型版本/训练截止信息；同时做匿名、原始实体和真正未来的冻结预测。股票排名应展示成本后超额、风格解释和独立选股贡献。

模块映射：`evaluation.contamination`、`portfolio.attribution`、`recommendations.rank_quality`。采用等级：**P0**。

复现路线：

1. 先用相同价格数据复现 masking × 决策模式。
2. 加新闻/财报后设计价格-only、新闻-only、组合消融。
3. 持续存档未来到期标签前的预测，待标签可得再评分。

需要数据：PIT 成分/行情；股票别名匿名映射；模型配置与调用响应；风格因子与费用版本。

未决事项：公开代码链接本次未核实；不能声称已复现。

### AP11 · AlphaForgeBench

**AlphaForgeBench: Benchmarking End-to-End Trading Strategy Design with Large Language Models**

作者：Wentao Zhang、Mingxuan Zhao、Jincheng Gao、Jieshun You、Huaiyu Jia、Yilei Zhao、Bo An、Shuo Sun。出版：KDD 2026 Datasets and Benchmarks；正式 Datasets & Benchmarks。最早核实作者稿：2026-02-10；本次使用版本：2026-05-27。

来源：[正式/官方入口](https://kdd2026.kdd.org/papers/)；[论文/作者稿](https://arxiv.org/abs/2602.18481)；[项目页](https://finbrain-lab-hkustgz.github.io/AlphaForgeBench/)。阅读深度：**定向全文**；行为不稳定性实验、因子/策略接口与 transaction costs 节；官方 KDD 列表交叉确认。

|核验项|记录|
|---|---|
|核心方法|把可执行因子与组合策略代码作为研究结果，固定执行环境来区分研究能力与逐次动作波动。|
|数据/市场/时间|多模型和多个金融任务；本次未完整抄录所有市场及日期，不能合并成单一时间窗。|
|验证与基准|重复运行/动作切换诊断，代码生成和固定执行评估。|
|交易/计算成本|主评估明确采用零交易成本；早期直接动作诊断使用全仓买入/全部退出、不做空、零费。|
|作者结论|作者报告直接逐次动作存在高重复方差，而可执行策略设计可提高评估可重复性。|

**适用边界：**其直接 Agent 实验配置不代表所有具备持仓记忆的未来 Agent。 无费收益不能作为 A 股上架依据。

**本项目适配（工程推断）：**保存结构化计划、状态、策略代码版本与输入快照，避免同一信号反复买卖；保留可执行策略和实时 Agent 两条实验路线，统一比较收益增量与动作稳定性。

模块映射：`strategy.specification`、`agents.stateful_planning`、`evaluation.reproducibility`。采用等级：**P0**。

复现路线：

1. 复现实验并加入持仓记忆、迟滞和计划版本基线。
2. 用本项目交易协议重新计算费用/滑点后的效果。
3. 同模型同预算比较直接计划、代码策略和二者组合。

需要数据：决策状态与输入快照；全部模型调用记录；可执行策略工件；交易费用/成交模拟。

未决事项：项目页提供 GitHub 按钮，仓库地址本次未点击核实。 摘要网页曾被搜索转为全文，记录的是定向阅读而非全部 appendix。

### AP12 · AgenticFinBench

**AgenticFinBench: Benchmarking Financial Agents on Diverse Tasks from Real-World Requests**

作者：Shu Zhou、Ziyang He、Yuxin Rao、Yudong Fang、Jiali Qian、Dongyuan Chen、Juntao Zhang、Yong Hu。出版：arXiv；预印本。最早核实作者稿：2026-11-03；本次使用版本：2026-11-04。

来源：[正式/官方入口](https://arxiv.org/abs/2611.04813)；[论文/作者稿](https://arxiv.org/html/2611.04813v2)；[项目页](https://nashnovaresearch.github.io/AgenticFinBench/)。阅读深度：**定向全文**；任务、claim audit、18 配置评估、模型/harness 方差分析。

|核验项|记录|
|---|---|
|核心方法|问题专属 rubric 加两阶段原子声明核验，区分缺少轨迹证据与被外部证据反驳。|
|数据/市场/时间|230 个真实金融任务、8 类、2 种语言、4 个复杂度；18 系统配置；非交易收益基准。|
|验证与基准|任务评分、证据可追溯性和幻觉审查；模型/harness 分析来自完整 3×3 子集。|
|交易/计算成本|关注研究任务质量与证据；不能映射为交易费用收益。|
|作者结论|作者报告忽略错误声明会改变排名；该子集中 harness 对证据 grounding 的解释差异高于模型差异。|

**适用边界：**样本规模与任务集有限；方差分解不是任意产品的普遍因果比率。 预印本发布仅一周，尚无本项目复现。

**本项目适配（工程推断）：**把推荐和个股计划拆为可核查声明：事实、计算结果、推断、情景与动作条件。每项事实指向证据版本，数值附计算口径；跨源冲突保留而非直接平均。

模块映射：`evidence.claim_registry`、`evaluation.agent_research`、`stock.research_dossier`。采用等级：**P0**。

复现路线：

1. 复现公开评分协议，构建中文投资研究任务集。
2. 固定工具比较模型，固定模型比较工具与上下文。
3. 对高影响数据和结论随机人工双评，检查自动 grader 偏差。

需要数据：工具 trace；原始证据文档；原子 claim 与计算结果；评审标注。

未决事项：代码链接及公开样本获取未执行。

### AP13 · PRISM-VQ

**Vector-Quantized Discrete Latent Factors Meet Financial Priors: Dynamic Cross-Sectional Stock Ranking Prediction for Portfolio Construction**

作者：Namhyoung Kim、Jae Wook Song。出版：IJCAI 2026 Main Track；正式主会论文。最早核实作者稿：未确认；本次使用版本：2026。

来源：[正式/官方入口](https://ijcai.org/proceedings/2026/492)；[作者代码](https://github.com/finxlab/PRISM-VQ)。阅读深度：**定向全文**；官方 HTML 全文中的架构、数据/基线、成本、五随机种子与鲁棒性。

|核验项|记录|
|---|---|
|核心方法|金融先验因子与 VQ 离散潜变量结合，结构条件 MoE 动态生成因子载荷。|
|数据/市场/时间|CSI300/S&P500 动态成分，Alpha158、20 日回看、主目标 5 日远期收益；2009–2019/2020–2021/2022–2024 划分。|
|验证与基准|同数据预处理与验证集调参；XGBoost、GRU、TCN、Transformer、MASTER 等；五随机种子，RankIC block bootstrap。|
|交易/计算成本|TopK-DropN K30/N5；开仓 5bps、平仓 15bps；更广泛成本敏感性在扩展附录。|
|作者结论|作者报告排序与组合指标改善，并展示潜变量的因子暴露。|

**适用边界：**5 日重叠收益标签的依赖结构与多重试验需在项目中统一处理。 使用 JKP 因子先验需核对中国市场可用版本和真正可得时间。

**本项目适配（工程推断）：**进入中短期横截面排名专家池；MoE 路由输出保留状态与解释。先验因子先经 PIT 验证，不把 latent code 直接解释成固定经济因果。

模块映射：`recommendations.cross_section_ranker`、`models.mixture_of_experts`、`factors.prior_library`。采用等级：**P1**。

复现路线：

1. 复现公开 CSI300 设置并检查动态成分与前向填充边界。
2. 与 LightGBM、线性、无先验、无 VQ、无 MoE 同预算比较。
3. 扩展全 A 股和盘后数据延迟，测费用/容量及风格暴露。

需要数据：Alpha158 PIT 特征；历史成分；可得时点一致的先验因子；交易成本/可成交规则。

未决事项：官方 PDF 超大小限制，正文由官方 HTML 阅读。 代码链接由官方论文给出，未下载运行。

### AP14 · TransAlpha

**TransAlpha: Lightweight Design Empowers Stock Return Forecasting**

作者：Xiao Yang。出版：IJCAI 2026 Main Track；正式主会论文。最早核实作者稿：未确认；本次使用版本：2026。

来源：[正式/官方入口](https://ijcai.org/proceedings/2026/580)；[论文/作者稿](https://ijcai.org/proceedings/2026/0580.pdf)。阅读深度：**定向全文**；9 页正式 PDF 的架构、数据与回测设定。

|核验项|记录|
|---|---|
|核心方法|横截面去噪、时间注意力门控、聚合与混合损失，降低日内高维因子预测计算量。|
|数据/市场/时间|沪深全 A 股及 CSI300/500/1000/2000；400 个专有 15 分钟因子；2021-01 至 2023-09 训练、2023Q4 验证、2024 测试。|
|验证与基准|比较线性、非 Transformer 与 Transformer 基线；下一 15 分钟收益，naive long-short 50 回测。|
|交易/计算成本|清仓单边收 0.1% 包含费用；并非完整普通 A 股长仓/底仓做 T 协议。|
|作者结论|作者报告预测、组合效果及计算效率改善；正文称提供匿名处理后的因子数据。|

**适用边界：**专有因子的生成过程限制真实在线复刻；匿名矩阵不等于完整实时数据管线。 简单多空收益不能直接当普通 A 股账户收益。

**本项目适配（工程推断）：**优先抽取轻量去噪/门控思路，在本项目公开定义的因子上重新训练；作为 15 分钟机会预测，执行由持仓可卖数量、费用及价位确定。

模块映射：`forecast.intraday`、`data.feature_matrix`、`stock.t_opportunity`。采用等级：**P1**。

复现路线：

1. 核实作者匿名数据/代码可得性，再复现实验。
2. 换为自己可实时计算的特征，同时对照线性与 LightGBM。
3. 加入 T+1、底仓、滑点、涨跌停和长仓约束重测。

需要数据：15 分钟 PIT 因子矩阵；因子定义和历史版本；账户可卖库存；成交/费用模型。

未决事项：匿名数据下载与代码未核实。 完整实盘规则实现未确认。

### AP15 · Kronos

**Kronos: A Foundation Model for the Language of Financial Markets**

作者：Yu Shi、Zongliang Fu、Shuo Chen、Bohan Zhao、Wei Xu、Changshui Zhang、Jian Li。出版：AAAI 2026 Technical Track；正式主会论文。最早核实作者稿：2025-08-02；本次使用版本：2026-03-14 journal/proceedings。

来源：[正式/官方入口](https://ojs.aaai.org/index.php/AAAI/article/view/39730)；[论文/作者稿](https://arxiv.org/html/2508.02739v1)；[作者代码](https://github.com/shiyu-coder/Kronos)。阅读深度：**定向全文**；AAAI 官方索引元数据与作者稿的 tokenizer/预训练/任务方法；未逐项核实全部训练数据。

|核验项|记录|
|---|---|
|核心方法|把金融 K 线离散为同时表达价量的 token，使用自回归预训练支撑价格、波动与生成任务。|
|数据/市场/时间|作者称跨 45 个交易所、超过 120 亿 K 线；本次未完整确认所有市场训练截止和重叠排除清单。|
|验证与基准|零样本价格与波动预测、合成序列质量，对照通用时间序列基础模型与非预训练模型。|
|交易/计算成本|主要预测/生成指标；不能从 RankIC/MAE 改善直接推导成本后收益。|
|作者结论|作者报告多任务指标优于所选基线；公开预训练模型和代码。|

**适用边界：**预训练规模不是 A 股未来样本外有效性的证据。 跨交易所时间、复权及训练重叠需要查明。

**本项目适配（工程推断）：**做可替换的价格/波动专家；保留原始数值与交易日语义，不将生成 K 线当未来事实。用概率分布辅助买卖区间和网格间距，和传统波动模型比较。

模块映射：`models.financial_foundation`、`forecast.price_distribution`、`stock.grid_parameters`。采用等级：**P1**。

复现路线：

1. 固定模型/ tokenizer 版本，核对训练截止。
2. 在严格未见区间比较零样本、微调与简单基线。
3. 将预测分布接到统一决策器后检验校准与净收益。

需要数据：多周期原始/复权价量；模型训练数据说明；预测分布与校准标签。

未决事项：AAAI 直接打开遇反爬，元数据由官方搜索索引、方法由作者稿交叉核实。 未下载权重或核实全部数据授权/训练重叠。

### AP16 · FinRpt

**FinRpt: Dataset, Evaluation System and LLM-based Multi-agent Framework for Equity Research Report Generation**

作者：Song Jin、Shuqi Li、Shukun Zhang、Rui Yan。出版：AAAI 2026 Technical Track；正式主会论文。最早核实作者稿：2025-11-10；本次使用版本：2026-08-05 arXiv v3; 2026-03-14 AAAI。

来源：[正式/官方入口](https://ojs.aaai.org/index.php/AAAI/article/view/37014)；[论文/作者稿](https://arxiv.org/html/2511.07322v3)。阅读深度：**定向全文**；作者稿任务、数据构建、报告指标与多 Agent 方法；AAAI 元数据交叉确认。

|核验项|记录|
|---|---|
|核心方法|聚合公司、财报、公告、新闻及行情等资料生成研究报告，配套报告评价与专用 Agent 训练。|
|数据/市场/时间|CSI800；2024-09-03 至 2024-11-05，周度 10 个分析日，6,825 份输入/报告样本。|
|验证与基准|11 类报告评价指标及多 Agent 生成实验；属于报告质量，不是买卖收益检验。|
|交易/计算成本|本次未核实完整调用成本；没有证明可交易 alpha。|
|作者结论|作者报告数据、评价系统和 FinRpt-Gen 对研究报告生成有效。|

**适用边界：**数据时间跨度短，集中在特定行情。 报告生成/评分可能共享模型偏差，需要独立事实审查。

**本项目适配（工程推断）：**个股页面按证据组织可持续更新的研究档案；事实与计算一次生成、多视图复用，新增公告只重算受影响段落与策略条件。

模块映射：`stock.research_dossier`、`evidence.document_pipeline`、`agents.report_composer`。采用等级：**P1**。

复现路线：

1. 重现报告输入和评价后，增加更长时段及不同规模股票。
2. 将事实准确率、数字口径、引用覆盖与投资判断分开评分。
3. 报告变化与股票实际收益增量另外验证。

需要数据：公告/财报版本；新闻归档；公司/证券实体映射；价量和行业基准。

未决事项：AAAI 被反爬；正式 metadata 与作者稿版本并列。 公开代码/数据的具体链接尚未核实。

### AP17 · TiMi

**Trade in Minutes! Rationality-Driven Agentic System for Quantitative Financial Trading**

作者：Zifan Song、Kaitao Song、Guosheng Hu、Ding Qi、Junyao Gao、Xiaohua Wang、Dongsheng Li、Cairong Zhao。出版：ICLR 2026；官方会议目录确认。最早核实作者稿：2025-10-06；本次使用版本：2026-02-09。

来源：[正式/官方入口](https://iclr.cc/Downloads/2026)；[论文/作者稿](https://arxiv.org/html/2510.04787v2)；[作者机构页面](https://www.microsoft.com/en-us/research/publication/trade-in-minutes-rationality-driven-agentic-system-for-quantitative-financial-trading/)。阅读深度：**定向全文**；策略—优化—部署结构、反思数学化与部署附录；录用由 ICLR 官方目录和作者单位页面交叉确认。

|核验项|记录|
|---|---|
|核心方法|语义分析、编程与数学推理 Agent 生成并优化交易程序，降低每次执行对连续大模型推理的依赖。|
|数据/市场/时间|作者称股票与加密市场 200+ 交易对；逐市场样本起止本次未完整核实。|
|验证与基准|比较收益/执行效率与反思优化；具体交易假设仍需取得实现。|
|交易/计算成本|论文区分离线分析成本与运行 bot 成本；不可与交易佣金混为一谈。|
|作者结论|作者报告策略部署效率与收益表现改善，并讨论限价/止盈止损/市价订单。|

**适用边界：**未完整确认 A 股专属成交约束。 代码发布承诺不等同本次确认已公开可运行。

**本项目适配（工程推断）：**区分低延迟执行程序与随新事件更新的研究计划；重大消息可触发模型重写计划，执行器读取已经版本化的条件与预算，不必每个 tick 重做全链研究。

模块映射：`agents.strategy_research`、`strategy.compiler`、`strategy.plan_versions`。采用等级：**P1**。

复现路线：

1. 核实数据和代码后复现部署/优化循环。
2. 固定反思频率与预算，比较持续 Agent、固定程序、事件驱动更新。
3. 在 A 股验证订单可执行性与计划更新造成的换手。

需要数据：交易轨迹；价格/新闻 as-of 快照；策略版本与参数；时间与算力成本。

未决事项：正式 proceedings 单篇 URL 未找到，使用会议官方目录+作者单位证据。 完整样本与费用未核实。

### AP18 · Market-JEPA

**Joint-Embedding Predictive Learning of Latent Market States in U.S. Equities**

作者：Simon Mahns、Randall Balestriero、Mido Assran。出版：ICML 2026 / PMLR 306；正式主会论文。最早核实作者稿：未确认；本次使用版本：2026。

来源：[正式/官方入口](https://proceedings.mlr.press/v306/mahns26a.html)；[论文/作者稿](https://raw.githubusercontent.com/mlresearch/v306/main/assets/mahns26a/mahns26a.pdf)。阅读深度：**仅摘要**；官方 PMLR 文章元数据与摘要；PDF 由工具返回不支持的 content-type。

|核验项|记录|
|---|---|
|核心方法|把每日无序资产特征编码为一组因子 token，时序 JEPA 学习紧凑市场状态。|
|数据/市场/时间|美国股票；具体股票池、日期与划分在本次可读摘要中未确认。|
|验证与基准|检验市场状态与波动、相关集中、有效维度、方向及新闻主题的关联。|
|交易/计算成本|状态表征研究；无可直接迁移的交易成本结果。|
|作者结论|作者报告状态与二阶结构关系强、与方向关系弱；更能预测渐进恢复，对突发压力起点较弱。|

**适用边界：**关联不等于可利用的方向性 alpha。 全文未读，不能确认其状态标签的所有构造细节。

**本项目适配（工程推断）：**作为行情状态/专家路由候选特征，与简单波动、涨跌广度和相关性基线比较；同时保留新闻事件通道识别突发变化。

模块映射：`market.regime_encoder`、`strategy.expert_router`。采用等级：**P2**。

复现路线：

1. 取得 PDF/代码确认因果掩码与标签时点。
2. 在 A 股训练无监督状态，再冻结下游样本外路由。
3. 测其对组合专家净增量，而不只展示状态图好看。

需要数据：横截面价量/因子面板；动态股票池；波动/相关结构标签；新闻时间线。

未决事项：全文、精确数据与代码未核实。

### AP19 · Biased-Consensus

**Emergence of Biased Consensus in Multi-Agent LLM Debates**

作者：Maya Okawa。出版：ICML 2026 / PMLR 306；正式主会论文。最早核实作者稿：未确认；本次使用版本：2026。

来源：[正式/官方入口](https://proceedings.mlr.press/v306/okawa26a.html)；[论文/作者稿](https://raw.githubusercontent.com/mlresearch/v306/main/assets/okawa26a/okawa26a.pdf)。阅读深度：**仅摘要**；正式 PMLR 摘要与作者 arXiv 摘要；PDF content-type 阻断。

|核验项|记录|
|---|---|
|核心方法|用受控实验与社会动力学模型研究偏差、从众和采样噪声怎样形成多 Agent 共识，并测试异质性。|
|数据/市场/时间|通用决策、投资决策和 LLM-as-a-judge 任务；不是 A 股收益回测。|
|验证与基准|理论预测与受控实验；不把所有 Agent 系统归为同一失败情形。|
|交易/计算成本|非交易成本研究；计算预算匹配需要本项目自行控制。|
|作者结论|特定设置下辩论放大共同偏差，异质性可缓和共识形成。|

**适用边界：**未证明所有多数投票或所有辩论都无效。 角色换名不一定产生统计独立信息。

**本项目适配（工程推断）：**按独立数据、技能和模型家族形成专家；存储最初独立判断后再允许互评。融合权重依据样本外误差相关性、校准和增量，不依据同意人数。

模块映射：`agents.collaboration_protocol`、`evaluation.error_correlation`、`strategy.expert_fusion`。采用等级：**P0**。

复现路线：

1. 构建同模型多次采样、异模型、异证据、单模型等预算四类对照。
2. 测方向准确率外，还测共同错误与错误自信。
3. 只保留能带来增量的交互轮次。

需要数据：每个 Agent 原始证据与独立判断；争议/共识过程；已到期标签；调用预算。

未决事项：全文未读；仅采用摘要已明确的方向性发现。 ICML 会议早于其 arXiv v1，因此不把 arXiv 日期标为全球首次公开。

### AP20 · Market-Regime-Council

**Market Regime Council for Dynamic Credit Assignment in Multi-Agent LLM Decision Systems**

作者：Yunhua Pei、Zerui Ge、Jin Zheng、John Cartlidge。出版：NeurIPS 2026 (accepted/in press; exact track unverified)；已录用/待出版；track 待核实。最早核实作者稿：2026-05-23；本次使用版本：2026-05-23 preprint; accepted by cutoff。

来源：[正式/官方入口](https://neurips.cc/Downloads/2026)；[论文/作者稿](https://arxiv.org/html/2605.24490v1)；[作者机构页面](https://research-information.bris.ac.uk/en/publications/market-regime-council-for-dynamic-credit-assignment-in-multi-agen/)。阅读深度：**定向全文**；Shapley/冷启动混合方法、主实验、消融与费用敏感性；官方目录与作者机构确认 accepted/in press。

|核验项|记录|
|---|---|
|核心方法|三个专家的单体/双体/全体输出用于 Shapley 贡献估计，配合指数加权历史、贝叶斯冷启动和状态权重。|
|数据/市场/时间|13 个加密资产，2023-03-01 至 2025-12-31，1,037 天，五随机种子。|
|验证与基准|单专家、两两协作、全体与完整融合消融；两两辩论未必优于最佳单体。|
|交易/计算成本|主表 0bps；另有 5/10bps 单边换手成本敏感性。|
|作者结论|完整融合在其样本中改善相对表现；作者也限制结论为匹配执行约束下的相对鲁棒性。|

**适用边界：**加密市场和 A 股规则不同，不能搬运其回报水平。 三个专家的精确 coalition 枚举不能无代价扩大到任意多策略。

**本项目适配（工程推断）：**作为专家贡献归因与动态权重实验；仅在收益标签已成熟后更新信誉。可对少量策略簇做精确贡献，多策略用近似贡献或分层路由，保留最强单策略对照。

模块映射：`strategy.expert_fusion`、`strategy.credit_assignment`、`market.regime_router`。采用等级：**P1**。

复现路线：

1. 复现三个专家全部 coalition，拆开辩论与加权收益。
2. 在 A 股同费用、同库存约束测等权/最佳单体/动态加权。
3. 测权重稳定、标签延迟、冷启动和 regime 错判影响。

需要数据：专家输出/反事实组合；延迟到期收益；市场状态；费用与换手。

未决事项：截至 2026-11-10 仅 accepted/in press，不说会议已经召开或 proceedings 已正式出版。 精确 track 与代码公开地址未核实。

### AP21 · FinAuditing

**FinAuditing: A Financial Taxonomy-Structured Multi-Document Benchmark for Evaluating LLMs**

作者：Yan Wang et al.。出版：SIGIR 2026 Resource Track；正式 Resource Track。最早核实作者稿：2025-10-10；本次使用版本：2026-05-17 pre-camera-ready。

来源：[正式/官方入口](https://doi.org/10.1145/3805712.3808578)；[论文/作者稿](https://arxiv.org/abs/2510.08886)；[作者代码](https://github.com/The-FinAI/FinAuditing)。阅读深度：**仅摘要**；作者稿最新版摘要/录用说明、作者项目正式引用和官方代码首页；DOI 直接打开失败。

|核验项|记录|
|---|---|
|核心方法|用 XBRL/会计分类结构评测语义匹配、关系抽取和数值一致性，跨文档核对财务事实。|
|数据/市场/时间|US-GAAP XBRL；1,102 个标注实例，平均超过 33k tokens；财报起止日期未核实。|
|验证与基准|13 个模型，分别测概念、层级关系与数值推理。|
|交易/计算成本|文档核对任务，不是投资交易收益。|
|作者结论|作者报告结构化长文档推理仍存在明显瓶颈；不照搬早版摘要的极端百分比。|

**适用边界：**US-GAAP 分类不直接对应中国会计口径。 预印本与正式引用的作者列表有变动；本记录不拼接作者名单。

**本项目适配（工程推断）：**财务数据层保存科目、合并/母公司、累计/单季、单位与校验等式；模型负责定位证据和解释，数值关系由可执行计算器校验，结果供所有策略共享。

模块映射：`data.accounting_taxonomy`、`evidence.numeric_verification`、`evaluation.financial_tables`。采用等级：**P0**。

复现路线：

1. 核实正式资源版本与公开代码。
2. 用中国报表科目映射构造语义/关系/数值三类测试。
3. 比较原文 RAG、结构化工具和组合，单独量化数字错误率。

需要数据：财报结构化事实；会计分类/计算关系；证据表格位置；单位和期间版本。

未决事项：未执行 benchmark 或获取完整正式 PDF。

### AP22 · FC4FinQA

**Data-Driven Function Calling Improvements in Large Language Model for Online Financial QA**

作者：Xing Tang、Hao Chen、Shiwei Li、Fuyuan Lyu、Weijie Shi、Lingjie Li、Dugang Liu、Weihong Luo、Xiku Du、Xiuqiang He。出版：The Web Conference 2026 Industry Track；正式 Industry Track。最早核实作者稿：2026-04-07；本次使用版本：2026-04-07 author manuscript。

来源：[正式/官方入口](https://www2026.thewebconf.org/accepted/industry)；[论文/作者稿](https://arxiv.org/html/2604.05387v1)。阅读深度：**定向全文**；方法、工具 schema、实体参数示例、离线/线上实验。

|核验项|记录|
|---|---|
|核心方法|从真实查询构建工具调用样本，用 AugFC 补参数分布盲点，再 SFT/RL 训练函数选择与参数提取。|
|数据/市场/时间|金融 QA 私有 API/线上查询与公开工具基准；Qwen2.5 1.5B/7B/32B；不是股价回测。|
|验证与基准|工具选择 F1、参数与线上质量；对照种子/增强/组合数据及训练设置。|
|交易/计算成本|强调调用准确和响应效率；缺少可迁移的股票收益结论。|
|作者结论|作者报告该流程在金融 QA 应用有效，并称已用于腾讯元宝场景。|

**适用边界：**私有接口和用户查询不等于可公开复现完整产品。 模型互相同意只是标注线索，仍需真实工具结果核验。

**本项目适配（工程推断）：**公共工具目录统一名称、输入输出、证券实体与 as_of 参数；选股、新闻、个股和回测都调用同一数据产品。记录错选工具/错单位/错时点样本，持续修复调用能力。

模块映射：`platform.tool_registry`、`data.semantic_gateway`、`evaluation.tool_calling`。采用等级：**P0**。

复现路线：

1. 先用无需训练的强模型加规范 schema 建基线。
2. 建立证券代码/简称/别名、日期、周期、单位的参数回归集。
3. 足够失败样本后再比较数据增强/小模型蒸馏是否改善成本与延迟。

需要数据：工具契约；执行结果和错误分类；实体别名字典；人工核实的调用样本。

未决事项：作者稿会议时间/地点模板与官网更新不一致；仅采纳已核实年份和 industry track。 代码链接未核实。

### AP23 · JFE-LLM-News

**Can ChatGPT forecast stock price movements? Return predictability and large language models**

作者：Alejandro Lopez-Lira、Yuehua Tang。出版：Journal of Financial Economics 184 (2026), 104335；正式期刊版。最早核实作者稿：2023-04-15；本次使用版本：2026-10 journal version; 2025-10-28 arXiv v6。

来源：[正式/官方入口](https://www.sciencedirect.com/science/article/pii/S0304405X26001066)；[论文/作者稿](https://arxiv.org/abs/2304.07619)。阅读深度：**摘要与出版方节选**；期刊出版元数据、官方摘要/数据片段与作者稿摘要交叉核验；publisher 直接 open 403。

|核验项|记录|
|---|---|
|核心方法|用 LLM 新闻标题评分研究初始价格反应与后续漂移，并分析模型规模、消息类型和收益随普及变化。|
|数据/市场/时间|美国股票，2021-10 至 2024-05；CRSP、新闻、RavenPack、NYSE TAQ；作者称使用晚于所测模型知识截止的新闻。|
|验证与基准|区分初始反应与后续漂移，比较模型与新闻类别；是历史研究，不是 2026 模型实时榜。|
|交易/计算成本|本次未完整读取最终版成本表；不引用媒体转述的费率敏感性数字。|
|作者结论|约 90% 的 portfolio-day 命中率对应不可交易的初始反应；后续漂移更集中于小股/负面消息，策略收益随 LLM 普及下降。|

**适用边界：**90% 不是逐只股票可交易买点胜率。 不能把早期 GPT 模型的知识截止当所有新模型的截止。 美国做空/小盘微结构与 A 股不同。

**本项目适配（工程推断）：**新闻因子分别记录初始解释、发布后漂移与加入数据/推理/下单延迟后的可交易收益；测公司规模、消息类别及流动性分层，避免使用已发生价格反应做买点标签。

模块映射：`news.event_response`、`evaluation.latency_adjusted_alpha`、`strategy.signal_decay`。采用等级：**P1**。

复现路线：

1. 取得正式论文成本与事件定义，锁定模型版本。
2. 以真实新闻可得时刻加总延迟定义可交易起点。
3. A 股按市值、流动性、正负消息和公告种类做样本外事件研究。

需要数据：原始首发新闻时间；分钟/逐笔价格；可成交量；模型可得时间和费率。

未决事项：arXiv 页面称之前已在 SSRN 公开，2023-04-15 仅为核实的 arXiv v1 日期。 正式期刊全文成本与附表尚未取得。

## 5. 各会议/期刊检索状态

|来源|已纳入|检查结论与缺口|
|---|---|---|
|ICLR 2026|AP01、AP02、AP03、AP04、AP17|四篇单篇 proceedings 已打开；TiMi 会议目录与作者单位页面相互核验。QuantAgent 的 OpenReview 遇验证、未核实接受状态，留历史/待核实池。 [官方入口](https://proceedings.iclr.cc/)|
|ICML 2026|AP18、AP19|PMLR 单篇正式元数据与摘要已打开，PDF content-type 阻断。本分工没有主攻组合优化/RL；对应 sibling 台账。 [官方入口](https://proceedings.mlr.press/v306/)|
|NeurIPS 2026|AP20|官方目录发现题名，作者机构明确 accepted/in press；精确 track 未确认。不能在11月10日写成12月会议已经召开。 [官方入口](https://neurips.cc/Downloads/2026)|
|KDD 2026|AP05、AP06、AP07、AP11|AlphaMaster DOI 有正式出版元数据；FactorMiner、Fin-RATE、AlphaForgeBench 官方列表核验。后两者 Datasets and Benchmarks 明确，FactorMiner track 未确认。列表为动态页，搜索能取更多条目，打开页部分省略。 [官方入口](https://kdd2026.kdd.org/papers/)|
|WWW / The Web Conference 2026|AP22|FC4FinQA 确认为 Industry Track；不能写成主研究赛道。When Agents Trade 仅列待研究题名。 [官方入口](https://www2026.thewebconf.org/accepted/research-tracks.html)|
|AAAI 2026|AP15、AP16|直接 open 被反爬；官方检索返回 DOI/卷期/出版日，方法以作者 arXiv 交叉核实。DigMA 已交 sibling。 [官方入口](https://ojs.aaai.org/index.php/AAAI/article/view/39730)|
|IJCAI 2026|AP13、AP14|主赛道已核实；PRISM PDF 过大，官方 HTML 有正文。 [官方入口](https://ijcai.org/proceedings/2026/492)|
|ACL 2026|本分工无核心计数|AlphaQuanter 是 Findings，交 execution/RL 分工登记，不在此重复计数。未宣称 ACL 全部论文完成覆盖。 [官方入口](https://aclanthology.org/2026.findings-acl.456/)|
|EMNLP 2026|AP08|FinRAQ 为 Findings，非 main conference；网页摘要与 PDF 的资产数冲突已保留。 [官方入口](https://aclanthology.org/2026.findings-emnlp.1432/)|
|FinNLP 2026|AP09|FinEvo 为 workshop。仅摘要阅读。 [官方入口](https://aclanthology.org/2026.finnlp-1.5/)|
|SIGIR 2026|AP21|FinAuditing 作者稿明示 Resource Track，作者项目给最终正式引用，DOI 直接访问失败；不以第三方名录单独判定。 [官方入口](https://doi.org/10.1145/3805712.3808578)|
|ICAIF 2026|本分工无核心计数|11月10日已有 accepted list；候选尚未取得可读全文，不计入23核心。 [官方入口](https://icaif2026.org/accepted-papers)|
|Financial journals 2026|AP23|JFE 正式版与2023起源明确区分；其他JF/RFS/MS方法由 sibling 记录。 [官方入口](https://www.sciencedirect.com/science/article/pii/S0304405X26001066)|
|arXiv 2026|AP10、AP12|纯预印本两篇单列；其余预印本与正式会议归并同一记录，不重复计数。 [官方入口](https://arxiv.org/abs/2605.28359)|

## 6. 待研究候选：不计入 23 篇

这些条目只说明下一轮应该读什么，**不据题名推断方法、回测结论或已实现功能**。

|ID|题名|核实程度|下一步|
|---|---|---|---|
|W01|[Access Is Not Use: The Reconstruction–Use Gap in Language-Model Financial Agents](https://icaif2026.org/accepted-papers)|ICAIF2026 accepted oral; title/authors only|信息可获取与真正被决策使用的差别，题名相关；尚不推断结论。|
|W02|[AlphaLeak: What `Blind' LLM Trading Benchmarks Still See](https://icaif2026.org/accepted-papers)|ICAIF2026 accepted oral; title/authors only|与 KTD-Fin 匿名协议互补的待审计方向；无全文不宣称已证明任何泄漏。|
|W03|[Shape Beyond Level: Event-Type News Persistence in Volatility Forecasting](https://icaif2026.org/accepted-papers)|ICAIF2026 accepted oral; title only|新闻事件种类与持续期，获取全文后才能确定算法/结论。|
|W04|[The Division of Research: Information Access and Independent-Book Capacity in LLM Investment Teams](https://icaif2026.org/accepted-papers)|ICAIF2026 accepted proceedings-only; title only|团队信息分工待研究；不混入已解析论文。|
|W05|[CallRank: Isolating What Changed in Earnings-Call Q&A for Sector Ranking and Cost-Adjusted Alpha](https://icaif2026.org/accepted-papers)|ICAIF2026 accepted oral; title only|增量消息、行业排名与成本适配待全文验证。|
|W06|[When Agents Trade: Live Multi-Market Trading Arena for LLM Agents](https://www2026.thewebconf.org/accepted/industry)|WWW2026 Industry official accepted list; not read|实时 forward benchmark 方向，后续核实时间、执行假设与收益归因。|
|W07|[AUDITFLOW: Executable Symbolic Environments for Structured Financial Reporting Verification](https://arxiv.org/abs/2606.03031)|2026 arXiv preprint candidate; abstract discovered only|结构化财务核验可执行环境；尚未核实正式 venue，不计入核心。|

## 7. 历史基础池与归并规则

- **H01 TradingAgents: Multi-Agents LLM Financial Trading Framework**（[原始入口](https://arxiv.org/abs/2412.20138)）：可作为多角色协作基线；2024起源、2025更新，不计入2026新思想。没有在本次复现其收益。
- **H02 QuantAgent: Price-Driven Multi-Agent LLMs for High-Frequency Trading**（[原始入口](https://arxiv.org/abs/2509.09995)）：作者摘要的实验周期为4小时，OpenReview投稿PDF不是录用凭据；不能根据标题把它描述为交易所微秒级高频实现。
- **H03 2026正式发表但更早起源的核心记录**：这些论文可以算2026会议/期刊队列，但不能算2026首次提出。JFE论文还注明arXiv前已在SSRN公开。

同一工作在 arXiv、OpenReview、正式会论文集、代码仓库出现多次，只保留一个核心 ID；发表年份与首次作者稿年份均保留。后续发现正式稿更正，不覆盖旧数据，新增版本与差异记录。

## 8. 查询日志

所有定向新论文搜索使用 system1 与 365 天 recency；日期判断仍以官方文档版本而非搜索引擎“几个月前”标签为准。下表是查询族与发现过程，全文打开、click、find 已以每篇 source_ref 留存在 JSON。

### Q01 · system1

查询：`2026 LLM finance agents stock alpha ICLR ICML KDD`；`2026 financial benchmark news factor mining`。

结果：发现 AlphaBench/AlphaMaster/FactorMiner/Fin-RATE/FinRAQ/FinEvo/AlphaForgeBench；逐项转官方来源。

### Q02 · system1

查询：`2026 ICLR financial stock reasoning benchmark`；`2026 IJCAI stock prediction`；`2026 AAAI financial foundation report`。

结果：正式 ICLR、IJCAI、AAAI 元数据与作者稿；识别专有特征/旧稿2026发表。

### Q03 · system1

查询：`"FactorMiner" "2026" arxiv`；`"Fin-RATE" 2026 arxiv`；`"AlphaMaster" "2026" arxiv`；`"Emergence of Biased Consensus in Multi-Agent LLM Debates" ICML 2026`。

结果：arXiv版本、ACM出版和PMLR正式元数据。

### Q04 · system1

查询：`"QuantAgent" "ICLR 2026"`；`"Trade in Minutes" "ICLR 2026"`；`"Kronos: A Foundation Model for the Language of Financial Markets" arxiv`；`"FinRpt" arxiv`。

结果：QuantAgent录用未证实；TiMi官方目录/作者单位互证；Kronos/FinRpt首发2025。

### Q05 · system1

查询：`"Market Regime Council" 2026`；`The Web Conference 2026 accepted papers financial stock prediction`；`SIGIR 2026 accepted papers financial stock market`；`ICAIF 2026 accepted papers program finance agent`。

结果：NeurIPS accepted/inpress 与 ICAIF 待阅读题名池；WWW/SIGIR继续精确检索。

### Q06 · system1

查询：`"Market Regime Council" site:neurips.cc 2026`；`"The Division of Research" "LLM" 2026`；`"Access Is Not Use" "Financial Agents" 2026`；`"Shape Beyond Level" "News" 2026`。

结果：官方目录可核对，题名不足以形成方法结论。

### Q07 · system1

查询：`"SIGIR 2026" "accepted" financial`；`"FinSearchComp" arxiv`；`"Reasoning on Time-Series for Financial Technical Analysis" arxiv`；`"AlphaBench" "arxiv.org/abs"`。

结果：FinAuditing线索，FinSearch/VTA旧稿日期明确。

### Q08 · system1

查询：`"FinAuditing" "2026" arxiv`；`"FinAuditing" site:sigir2026.org`；`"FinAuditing" site:dl.acm.org`；`"stock" site:www2026.thewebconf.org/accepted/research-tracks.html`。

结果：FinAuditing Resource Track 作者稿/最终作者引用；发现 FC4FinQA 线索。

### Q09 · system1

查询：`"Data-Driven Function Calling Improvements" 2026`；`"AlphaForgeBench" "2026" "ICLR"`；`"AlphaForgeBench" site:kdd2026.kdd.org`；`"Data-Driven Function Calling" site:www2026.thewebconf.org`。

结果：AlphaForgeBench修正为KDD D&B；FC4FinQA为WWW Industry。

### Q10 · system1

查询：`"Can ChatGPT forecast stock price movements" "2026"`；`"Can ChatGPT forecast stock price movements" "S0304405X26001066"`。

结果：JFE正式元数据/摘要与arXiv旧稿互证；明确初始反应不可交易。

### Q11 · primary_open_click_find

查询：`official PDF/HTML: dataset, transaction, date, github, assumptions`。

结果：定向阅读20余文档；发现FinRAQ网页/PDF数量差异，AlphaForge零费用、MRC零费主表+敏感性、TransAlpha单边清仓费+多空假设。

## 9. 下一轮复现应交付的最小证据包

每个进入策略池的研究工件至少提交：论文与代码版本、数据清单及可得时点、特征定义、训练/验证/测试分区、模型与工具配置、全部试验次数、基线与消融、实际执行假设、成本前后结果、波动与风格归因、失效区间、运行资源及复现命令。没有这些字段的论文仍可贡献思想，但不能被包装为已验证策略。

本项目可以持续增加模型能力与并行 Agent 数量。增长接口应由任务/证据/工件契约定义，让更强模型替换某个专家或研究步骤时能以同一个评测框架证明增量。新模型不应被强制固定成旧模型的能力边界；同样，也不应仅因更大或更新就无条件提高其资金权重。

## 10. 主文作者应亲自打开的核心 URL

1. https://proceedings.iclr.cc/paper_files/paper/2026/hash/4f3820576130a8f796ddbf204c841487-Abstract-Conference.html
2. https://arxiv.org/html/2602.14670v2
3. https://arxiv.org/html/2602.07294v4
4. https://aclanthology.org/2026.findings-emnlp.1432.pdf
5. https://arxiv.org/html/2605.28359v1
6. https://arxiv.org/abs/2602.18481
7. https://arxiv.org/abs/2611.04813
8. https://ijcai.org/proceedings/2026/492
9. https://proceedings.mlr.press/v306/okawa26a.html
10. https://arxiv.org/html/2605.24490v1
11. https://arxiv.org/html/2604.05387v1
12. https://www.sciencedirect.com/science/article/pii/S0304405X26001066

完整结构化字段、检索引用 ID、日期口径、状态统计与待研究池见同目录 `papers_agents.json`。

