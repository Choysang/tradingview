# 2026论文方法与本站研究路线

截至2026年11月10日，核心证据集为38篇：29篇定向阅读方法、实验或指定附录，7篇仅摘要，2篇摘要及出版方正文节选。没有在本轮执行作者训练或收益回测。正式出版、已录用、Findings、Workshop和预印本在每张卡中分别标记；2026正式发表也可能起源于更早工作。

该集合来自定向系统检索，不能宣称穷尽尚未结束的2026全年。覆盖日志同时保留查过而未找到可核实相关方法的venue、仅题名候选、旧基础方法和未取得全文的项目。全部原始方法卡、查询日志及复现步骤在research/；正文将核心思想逐项映射到本站模块。

## 1 研究证据如何改变设计

第一，因子发现要保存成功与失败经验、检测池内相关性，并检验对组合的新增信息。第二，新闻解释质量、方向识别和延迟后可交易收益是不同目标。第三，LLM历史收益需要检查记忆、风格暴露与当时可得信息。第四，预测、配置和执行分别采用匹配的目标和约束。第五，多Agent的价值由互补信息及成本后增量证明，不用票数替代证据。第六，研究次数与选择过程本身进入评估记录。[AP01][AP06][AP10][AP19][AP23][EP11]

这些是从文献与本项目需求形成的工程取舍。没有一篇论文证明本站在A股全市场已经盈利；也不把某基准的失败结论外推成所有未来模型无效。

## 2 值得先做的六组复现

| 研究组 | 借鉴方法 | 必须检验的本站增量 |
| --- | --- | --- |
| 因子研究 | AlphaBench、AlphaMaster、FactorMiner、AlphaSAGE | 相同搜索预算下，独立信息与成本后样本外组合增量 |
| 消息与事实 | FinRAQ、Fin-RATE、FinAuditing、JFE新闻研究 | 首发与延迟分开；数字正确；消息加入后是否改变可执行结果 |
| 模型验证 | KTD-Fin、AlphaForgeBench、AgenticFinBench | 匿名/记忆控制、重复运行差异、风格归因与冻结前瞻 |
| 配置与路由 | STABLE、决策导向稀疏组合、ReCAP、FineFT、MRC | 加入A股现金/库存/换手约束后是否胜过简单等权与状态规则 |
| 执行与环境 | DASRL、连续时间RL、LOB-Impact、MACE | 真实规则、涨跌停日、部分成交、容量与冲击下的净值和成交质量 |
| 研究晋级与持仓操作 | PROMOTE、RFS可实施前沿、网格研究 | 反复试验记录、净财富而非已实现价差、独立样本验证 |

## 3 核心方法逐篇说明

### AP01 AlphaBench

AlphaBench: Benchmarking Large Language Models in Formulaic Alpha Factor Mining。状态：ICLR 2026 Conference；conference_published_main。阅读：定向全文。[论文原始来源](https://proceedings.iclr.cc/paper_files/paper/2026/hash/4f3820576130a8f796ddbf204c841487-Abstract-Conference.html)。

核心思想：把因子生成、因子质量判断、迭代搜索分开评测；以可执行金融 DSL 和统一回测比较提示、推理配置及搜索范式。

本站映射：先把自然语言研究假说编译为带类型与单位的因子表达式；所有模型调用同一 as-of 数据快照、算子定义和评估服务。语义解释只供研究，不替代收益验证。

关键边界：基准能力不等于策略经济价值。；同一基准反复优化可能耗尽样本外信息；需另设本项目保留集。

### AP02 VTA

Reasoning on Time-Series for Financial Technical Analysis。状态：ICLR 2026 Conference；conference_published_main。阅读：定向全文。[论文原始来源](https://proceedings.iclr.cc/paper_files/paper/2026/hash/68c1b9784d982fa5575bb45ace7bd64e-Abstract-Conference.html)。

核心思想：将价格序列转为文字标注，训练语言推理，再把推理属性作为时间序列骨干的条件；推理奖励与预测误差相关。

本站映射：做技术分析专家：数值指标由统一行情算子计算，模型解释趋势和情景，并输出预测分布、适用周期与反证；与纯时序/树模型按同数据比较。

关键边界：优化 MSE 不自动优化排序与成本后收益。；解释流畅与实际因果归因不是同一指标。

### AP03 FinSearchComp

FinSearchComp: Towards a Realistic, Expert-Level Evaluation of Financial Search and Reasoning。状态：ICLR 2026 Conference；conference_published_main。阅读：定向全文。[论文原始来源](https://proceedings.iclr.cc/paper_files/paper/2026/hash/4d42358702dff82e1436550a05ade260-Abstract-Conference.html)。

核心思想：用实时数据获取、简单历史检索、复杂历史调查三类任务测金融搜索与推理。

本站映射：建立中文金融检索回归集：公告、宏观发布日期、跨公司财务口径、国际事件传导；每题冻结 answer_as_of、证据链接、单位和实体主键。

关键边界：排行榜是特定检索时间/产品配置的快照。；本次未完整核实每项最新问答的截止时刻与网页修订版本。

### AP04 AlphaSAGE

AlphaSAGE: Structure-Aware Alpha Mining via GFlowNets for Robust Exploration。状态：ICLR 2026 Conference；conference_published_main。阅读：定向全文。[论文原始来源](https://proceedings.iclr.cc/paper_files/paper/2026/hash/06115011841a8d500050261c0ac4b1e4-Abstract-Conference.html)。

核心思想：RGCN 编码因子表达式图，GFlowNet 探索多个有价值表达式区域，密集多目标奖励同时考虑预测与多样性。

本站映射：作为因子候选生成器，和 GP/LLM 候选共享 DSL、预算与同一评估器；不能把不同公式文字当成独立经济信号。

关键边界：美国样本止于 2020。；生成分布多样性仍需在本项目用实际收益/暴露相关性检验。

### AP05 AlphaMaster

AlphaMaster: Dual-Chain Feedback for Scalable and Diverse Alpha Factor Discovery。状态：KDD 2026 V.2；conference_published_track_unverified。阅读：摘要。[论文原始来源](https://doi.org/10.1145/3770855.3818120)。

核心思想：生成链与优化链分工，联合 RL、GFlowNet、LLM 符号生成和 GP；用相关性约束减少同质因子。

本站映射：借鉴研究编排：多生成器提供候选，统一评估器筛选，优化器只看训练/验证反馈；公共因子库记录相关簇与替换历史。

关键边界：没有全文验证，暂不能确认回测协议和数据时点。；组合多种生成器本身不证明收益独立。

### AP06 FactorMiner

FactorMiner: A Self-Evolving Agent with Skills and Experience Memory for Financial Alpha Discovery。状态：KDD 2026；conference_published_track_unverified。阅读：定向全文。[论文原始来源](https://kdd2026.kdd.org/papers/)。

核心思想：可执行研究技能加成功/失败经验库；快速 IC 筛选、池内相关性检查、批内去重、完整验证和近邻因子替换。

本站映射：优先实现完整试验台账与失败记忆。日内因子先映射为机会评分；做 T 只使用可卖底仓，长周期选股另外构建目标。数据计算缓存共享，避免各 Agent 重算相同特征。

关键边界：10 分钟预测收益不等于 A 股买入后可同日卖出。；绝对 IC 汇总需与有符号、训练期固定方向的结果分清。

### AP07 Fin-RATE

Fin-RATE: A Real-world Financial Analytics and Tracking Evaluation Benchmark for LLMs on SEC Filings。状态：KDD 2026 Datasets and Benchmarks；conference_published_benchmark。阅读：定向全文。[论文原始来源](https://doi.org/10.1145/3770855.3817528)。

核心思想：区分单文档细节推理、跨企业比较、跨期追踪，并把检索、实体、时间与推理错误拆开。

本站映射：为公告/财务服务建立 entity_id、period、accounting_basis、unit、revision 约束；表格数值由程序复算，跨期比较必须记录重述版本。

关键边界：美国 SEC 与中国财报格式、会计口径不同。；生成题目和评分器也需要人工抽检。

### AP08 FinRAQ

FinRAQ: Financial News Reasoning through Importance Ranking and Socratic Questioning。状态：Findings of EMNLP 2026；findings_published。阅读：定向全文。[论文原始来源](https://aclanthology.org/2026.findings-emnlp.1432/)。

核心思想：NewsRank 以时间图的语义/时间双通道聚合新闻重要性；多空视角使用结构化 Socratic 提问相互检查假设。

本站映射：将消息重要性、方向、持续期分开建模；事件关系进入共享图，给新闻、选股与个股策略共同使用。辩论仅增加新证据或可检验反证时继续，用等预算单 Agent 作对照。

关键边界：网页摘要写 6 股票，正式 PDF 写 21 资产；本记录按 PDF，保留冲突。；资产范围有限、模型训练记忆与历史新闻重叠尚需单独审计。

### AP09 FinEvo

FinEvo: Financial Language Agents Shape the Markets That Evaluate Them。状态：11th FinNLP Workshop 2026；workshop_published。阅读：摘要。[论文原始来源](https://aclanthology.org/2026.finnlp-1.5/)。

核心思想：把新闻驱动的异质金融 Agent 放入连续双向拍卖市场，并通过资本重分配使参与者种群变化。

本站映射：用于策略拥挤、新闻传播与流动性变化的压力实验；保留独立真实历史回放，评估一组策略同时使用后是否抵消收益。

关键边界：模拟内生成的价格不是观察到的真实 A 股价格。；不能从仿真种群胜出直接推断实盘 alpha。

### AP10 KTD-Fin

From Knowing to Doing: A Memory-Controlled Benchmark for LLM Trading Agents on Stock Markets。状态：arXiv；preprint。阅读：定向全文。[论文原始来源](https://arxiv.org/abs/2605.28359)。

核心思想：股票标识和日期匿名化控制历史记忆，再用 Barra 风格归因区分市场、风格和选股贡献。

本站映射：给所有研究保留模型版本/训练截止信息；同时做匿名、原始实体和真正未来的冻结预测。股票排名应展示成本后超额、风格解释和独立选股贡献。

关键边界：不能据此否定有真实新闻和基本面输入的 LLM 增量。；匿名不保证抹除全部训练记忆；只有部分窗口严格晚于锚模型 cutoff。

### AP11 AlphaForgeBench

AlphaForgeBench: Benchmarking End-to-End Trading Strategy Design with Large Language Models。状态：KDD 2026 Datasets and Benchmarks；conference_published_benchmark。阅读：定向全文。[论文原始来源](https://kdd2026.kdd.org/papers/)。

核心思想：把可执行因子与组合策略代码作为研究结果，固定执行环境来区分研究能力与逐次动作波动。

本站映射：保存结构化计划、状态、策略代码版本与输入快照，避免同一信号反复买卖；保留可执行策略和实时 Agent 两条实验路线，统一比较收益增量与动作稳定性。

关键边界：其直接 Agent 实验配置不代表所有具备持仓记忆的未来 Agent。；无费收益不能作为 A 股上架依据。

### AP12 AgenticFinBench

AgenticFinBench: Benchmarking Financial Agents on Diverse Tasks from Real-World Requests。状态：arXiv；preprint。阅读：定向全文。[论文原始来源](https://arxiv.org/abs/2611.04813)。

核心思想：问题专属 rubric 加两阶段原子声明核验，区分缺少轨迹证据与被外部证据反驳。

本站映射：把推荐和个股计划拆为可核查声明：事实、计算结果、推断、情景与动作条件。每项事实指向证据版本，数值附计算口径；跨源冲突保留而非直接平均。

关键边界：样本规模与任务集有限；方差分解不是任意产品的普遍因果比率。；预印本发布仅一周，尚无本项目复现。

### AP13 PRISM-VQ

Vector-Quantized Discrete Latent Factors Meet Financial Priors: Dynamic Cross-Sectional Stock Ranking Prediction for Portfolio Construction。状态：IJCAI 2026 Main Track；conference_published_main。阅读：定向全文。[论文原始来源](https://ijcai.org/proceedings/2026/492)。

核心思想：金融先验因子与 VQ 离散潜变量结合，结构条件 MoE 动态生成因子载荷。

本站映射：进入中短期横截面排名专家池；MoE 路由输出保留状态与解释。先验因子先经 PIT 验证，不把 latent code 直接解释成固定经济因果。

关键边界：5 日重叠收益标签的依赖结构与多重试验需在项目中统一处理。；使用 JKP 因子先验需核对中国市场可用版本和真正可得时间。

### AP14 TransAlpha

TransAlpha: Lightweight Design Empowers Stock Return Forecasting。状态：IJCAI 2026 Main Track；conference_published_main。阅读：定向全文。[论文原始来源](https://ijcai.org/proceedings/2026/580)。

核心思想：横截面去噪、时间注意力门控、聚合与混合损失，降低日内高维因子预测计算量。

本站映射：优先抽取轻量去噪/门控思路，在本项目公开定义的因子上重新训练；作为 15 分钟机会预测，执行由持仓可卖数量、费用及价位确定。

关键边界：专有因子的生成过程限制真实在线复刻；匿名矩阵不等于完整实时数据管线。；简单多空收益不能直接当普通 A 股账户收益。

### AP15 Kronos

Kronos: A Foundation Model for the Language of Financial Markets。状态：AAAI 2026 Technical Track；conference_published_main。阅读：定向全文。[论文原始来源](https://ojs.aaai.org/index.php/AAAI/article/view/39730)。

核心思想：把金融 K 线离散为同时表达价量的 token，使用自回归预训练支撑价格、波动与生成任务。

本站映射：做可替换的价格/波动专家；保留原始数值与交易日语义，不将生成 K 线当未来事实。用概率分布辅助买卖区间和网格间距，和传统波动模型比较。

关键边界：预训练规模不是 A 股未来样本外有效性的证据。；跨交易所时间、复权及训练重叠需要查明。

### AP16 FinRpt

FinRpt: Dataset, Evaluation System and LLM-based Multi-agent Framework for Equity Research Report Generation。状态：AAAI 2026 Technical Track；conference_published_main。阅读：定向全文。[论文原始来源](https://ojs.aaai.org/index.php/AAAI/article/view/37014)。

核心思想：聚合公司、财报、公告、新闻及行情等资料生成研究报告，配套报告评价与专用 Agent 训练。

本站映射：个股页面按证据组织可持续更新的研究档案；事实与计算一次生成、多视图复用，新增公告只重算受影响段落与策略条件。

关键边界：数据时间跨度短，集中在特定行情。；报告生成/评分可能共享模型偏差，需要独立事实审查。

### AP17 TiMi

Trade in Minutes! Rationality-Driven Agentic System for Quantitative Financial Trading。状态：ICLR 2026；conference_accepted_official_catalog。阅读：定向全文。[论文原始来源](https://iclr.cc/Downloads/2026)。

核心思想：语义分析、编程与数学推理 Agent 生成并优化交易程序，降低每次执行对连续大模型推理的依赖。

本站映射：区分低延迟执行程序与随新事件更新的研究计划；重大消息可触发模型重写计划，执行器读取已经版本化的条件与预算，不必每个 tick 重做全链研究。

关键边界：未完整确认 A 股专属成交约束。；代码发布承诺不等同本次确认已公开可运行。

### AP18 Market-JEPA

Joint-Embedding Predictive Learning of Latent Market States in U.S. Equities。状态：ICML 2026 / PMLR 306；conference_published_main。阅读：摘要。[论文原始来源](https://proceedings.mlr.press/v306/mahns26a.html)。

核心思想：把每日无序资产特征编码为一组因子 token，时序 JEPA 学习紧凑市场状态。

本站映射：作为行情状态/专家路由候选特征，与简单波动、涨跌广度和相关性基线比较；同时保留新闻事件通道识别突发变化。

关键边界：关联不等于可利用的方向性 alpha。；全文未读，不能确认其状态标签的所有构造细节。

### AP19 Biased-Consensus

Emergence of Biased Consensus in Multi-Agent LLM Debates。状态：ICML 2026 / PMLR 306；conference_published_main。阅读：摘要。[论文原始来源](https://proceedings.mlr.press/v306/okawa26a.html)。

核心思想：用受控实验与社会动力学模型研究偏差、从众和采样噪声怎样形成多 Agent 共识，并测试异质性。

本站映射：按独立数据、技能和模型家族形成专家；存储最初独立判断后再允许互评。融合权重依据样本外误差相关性、校准和增量，不依据同意人数。

关键边界：未证明所有多数投票或所有辩论都无效。；角色换名不一定产生统计独立信息。

### AP20 Market-Regime-Council

Market Regime Council for Dynamic Credit Assignment in Multi-Agent LLM Decision Systems。状态：NeurIPS 2026 (accepted/in press; exact track unverified)；accepted_in_press_track_unverified。阅读：定向全文。[论文原始来源](https://neurips.cc/Downloads/2026)。

核心思想：三个专家的单体/双体/全体输出用于 Shapley 贡献估计，配合指数加权历史、贝叶斯冷启动和状态权重。

本站映射：作为专家贡献归因与动态权重实验；仅在收益标签已成熟后更新信誉。可对少量策略簇做精确贡献，多策略用近似贡献或分层路由，保留最强单策略对照。

关键边界：加密市场和 A 股规则不同，不能搬运其回报水平。；三个专家的精确 coalition 枚举不能无代价扩大到任意多策略。

### AP21 FinAuditing

FinAuditing: A Financial Taxonomy-Structured Multi-Document Benchmark for Evaluating LLMs。状态：SIGIR 2026 Resource Track；conference_published_resource。阅读：摘要。[论文原始来源](https://doi.org/10.1145/3805712.3808578)。

核心思想：用 XBRL/会计分类结构评测语义匹配、关系抽取和数值一致性，跨文档核对财务事实。

本站映射：财务数据层保存科目、合并/母公司、累计/单季、单位与校验等式；模型负责定位证据和解释，数值关系由可执行计算器校验，结果供所有策略共享。

关键边界：US-GAAP 分类不直接对应中国会计口径。；预印本与正式引用的作者列表有变动；本记录不拼接作者名单。

### AP22 FC4FinQA

Data-Driven Function Calling Improvements in Large Language Model for Online Financial QA。状态：The Web Conference 2026 Industry Track；conference_published_industry。阅读：定向全文。[论文原始来源](https://www2026.thewebconf.org/accepted/industry)。

核心思想：从真实查询构建工具调用样本，用 AugFC 补参数分布盲点，再 SFT/RL 训练函数选择与参数提取。

本站映射：公共工具目录统一名称、输入输出、证券实体与 as_of 参数；选股、新闻、个股和回测都调用同一数据产品。记录错选工具/错单位/错时点样本，持续修复调用能力。

关键边界：私有接口和用户查询不等于可公开复现完整产品。；模型互相同意只是标注线索，仍需真实工具结果核验。

### AP23 JFE-LLM-News

Can ChatGPT forecast stock price movements? Return predictability and large language models。状态：Journal of Financial Economics 184 (2026), 104335；journal_published。阅读：摘要及正文节选。[论文原始来源](https://www.sciencedirect.com/science/article/pii/S0304405X26001066)。

核心思想：用 LLM 新闻标题评分研究初始价格反应与后续漂移，并分析模型规模、消息类型和收益随普及变化。

本站映射：新闻因子分别记录初始解释、发布后漂移与加入数据/推理/下单延迟后的可交易收益；测公司规模、消息类别及流动性分层，避免使用已发生价格反应做买点标签。

关键边界：90% 不是逐只股票可交易买点胜率。；不能把早期 GPT 模型的知识截止当所有新模型的截止。

### EP01 AlphaQuanter

AlphaQuanter: An End-to-End Tool-Augmented Agentic Reinforcement Learning Framework for Stock Trading。状态：Findings of ACL 2026，正式论文；不是 ACL 主会论文；详见原卡。阅读：定向全文。[论文原始来源](https://aclanthology.org/2026.findings-acl.456/)。

核心思想：以 GRPO 训练单个工具调用型语言模型，按需查询行情、基本面、新闻及宏观工具后输出日频买入/卖出/持有。

本站映射：采纳按需工具路由与结构化证据输出；将选股预测和账户下单权限分开。；输入必须带 available_at；同一时点的新闻事实由全站共享，避免每个 Agent 重建不一致消息库。

关键边界：作者明确承认单资产范围、固定工具集合和上下文增长限制。；五只美国大盘股不能验证 A 股全市场选股；新闻字段的原始可见时间及版本需单独复核。

### EP02 STABLE

STABLE: Shift-Tolerant Allocation via Black-Litterman Using Conditional Diffusion Estimates。状态：ICLR 2026，正式会议论文；详见原卡。阅读：定向全文。[论文原始来源](https://proceedings.iclr.cc/paper_files/paper/2026/hash/4d4dbc5c955c5d273eed12d565335068-Abstract-Conference.html)。

核心思想：宏观与个股条件扩散生成收益轨迹，估计联合收益分布，再将均值/协方差信息送入 Black–Litterman 配置。

本站映射：保留分布预测、观点不确定性和宏观信息融合；把最终配置交给统一约束优化器。；宏观数据使用当时发布版本，回测股票池使用历史时点成分；需重做完整时间外测试。

关键边界：按测试期末规模与完整历史选择股票存在事后成分/存活者问题。；标准归一化逆协方差权重表达式没有自动施加 A 股长仓、手数与换手约束。

### EP03 Decision-focused Sparse Portfolio

Decision-focused Sparse Tangent Portfolio Optimization。状态：ICML 2026，PMLR 306 正式论文；详见原卡。阅读：定向全文。[论文原始来源](https://proceedings.mlr.press/v306/jeon26a.html)。

核心思想：将可微稀疏 Sharpe 优化层接入预测模型，以决策损失与预测误差混合训练，并用可微 top-k 选择持股。

本站映射：把可微决策层当可插拔研究实现，与不可微但约束完整的优化器公平比较。；稀疏持股数、行业限额、单股上限和现金缓冲均为显式账户参数。

关键边界：数据池过滤有存活者偏差。；作者代码取当前成分并删除不完整历史，重新下载会随数据版本改变。

### EP04 PSGA/STCO

A Linearly Convergent Proximal Subgradient Algorithm for Sparse Portfolio Optimization with Transaction Cost。状态：ICML 2026，PMLR 306 正式论文；详见原卡。阅读：定向全文。[论文原始来源](https://proceedings.mlr.press/v306/yao26s.html)。

核心思想：以稀疏持股与交易成本构造优化问题，采用差分凸松弛和近端次梯度算法；理论给出特定条件下的线性收敛。

本站映射：采纳成本和稀疏度进入同一目标函数的思想，作为可解释 CPU 基准。；对账成本单位、单边/双边换手和现金权重；保留显式的 no-trade 区域。

关键边界：旧小样本市场集不能验证当前 A 股表现。；Best/BCRP 等事后最优基准只是诊断上界，不能作为可部署竞争者。

### EP05 ReCAP

Regime-Adaptive Continual Learning for Portfolio Management。状态：KDD 2026 正式录用/会议论文；DOI 10.1145/3770855.3817620；详见原卡。阅读：定向全文。[论文原始来源](https://arxiv.org/abs/2606.00143)。

核心思想：ReCAP 用在线状态变化检测器维护市场阶段，保存 PPO 的策略参数增量；门控混合阶段经验，适配新阶段并合并相似增量。

本站映射：用过去信息估计阶段概率，并登记检测延迟、置信度和阶段切换费用。；把任意策略的组合统一为预测或目标权重接口；只有同骨架模型才可以研究参数增量合成。

关键边界：连续上市筛选存在存活者偏差。；代码明确是最小可运行版本，训练规模与完整论文复现存在差别。

### EP06 FineFT

FineFT: Efficient and Risk-Aware Ensemble Reinforcement Learning for Futures Trading。状态：KDD 2026 正式论文；DOI 10.1145/3770854.3780187；思想首次公开于 2025；详见原卡。阅读：定向全文。[论文原始来源](https://arxiv.org/abs/2512.23773)。

核心思想：以选择性 TD 更新训练专家，筛选各状态中表现较好的策略，再用能力/分布外状态路由决定采用哪个专家。

本站映射：采用“符合条件的专家路由”而不是买卖票数相加。；专家输出统一为预期增量收益、持有期限及目标库存；统一组合器处理现金与可卖持仓。

关键边界：24 小时双向杠杆合约与 A 股现金股票交易条件不同。；筛选、路由与评估阶段必须清楚隔离，避免按测试收益选择专家。

### EP07 DigMA

Controllable Financial Market Generation with Diffusion Guided Meta Agent。状态：AAAI 2026 正式论文（DigMA）；DOI 10.1609/aaai.v40i1.37009；首次公开于 2024；详见原卡。阅读：定向全文。[论文原始来源](https://ojs.aaai.org/index.php/AAAI/article/view/37009)。

核心思想：条件扩散生成中间价收益和订单到达参数，再由带经济先验的元 Agent 生成订单流。

本站映射：将生成器作为压力情景和模型训练的补充，不替代真实历史回放。；按时间、股票和未见市场阶段分层留出，并加入 LOB-Impact 式自身订单干预测试。

关键边界：Wind 完整数据受许可约束，公开代码不意味着原数据可任意再分发。；同年股票日随机划分不能证明跨时期泛化。

### EP08 DASRL

Dynamic Action Space Reinforcement Learning for Optimal Trading Execution。状态：AAMAS 2026 Research Track，正式论文；DOI 10.65109/GFLB4392；详见原卡。阅读：定向全文。[论文原始来源](https://ifaamas.org/Proceedings/aamas2026/pdfs/GFLB4392.pdf)。

核心思想：根据当前市场状态缩减执行动作集合，结合自适应探索与经验回放重整，学习大额卖出母单的拆分执行。

本站映射：让 RL 只决定已批准母单的拆分、时机和价格类型，不让执行器重新发明选股方向。；动作可行域来自实时可卖量、涨跌停、交易时段、板块手数和已保留订单。

关键边界：十年前的八股样本且排除涨跌停日，不能验证当前全 A 股执行。；母单数量规模与市场参与率须逐例重核；数据单次回放不一定完整模拟自身冲击。

### EP09 Continuous-time Execution RL

Reinforcement learning for continuous-time optimal execution: actor–critic algorithm and error analysis。状态：Finance and Stochastics 30:597–655，正式期刊论文；不是 JF/JFE/RFS；详见原卡。阅读：定向全文。[论文原始来源](https://link.springer.com/article/10.1007/s00780-026-00589-5)。

核心思想：在熵正则的均值—二次变差执行控制下构造可解析的策略/价值参数化，通过 actor–critic 与重校准学习执行路径。

本站映射：作为有解析结构的 RL 执行基准，与复杂神经网络进行同成本比较。；按 A 股交易时段重新标定波动、冲击和可用库存；午休不能当普通连续交易时段。

关键边界：理论依赖模型、步长及正则假设。；得到的是执行时间表/速率决策，不自动解决真实限价单排队。

### EP10 LOB-Impact

LOB-Impact Benchmark: Emergent Market Impact in Generative Limit Order Book Models。状态：ICAIF 2026 官方名单确认 oral 录用；截至 2026-11-10 会议尚未举行；作者预印本可读；详见原卡。阅读：定向全文。[论文原始来源](https://arxiv.org/abs/2610.27100)。

核心思想：通过向生成订单簿注入母单，并比较可见/隐藏订单干预，测量自身市场冲击和执行后的恢复。

本站映射：把订单干预、冲击恢复和容量响应列为模拟器验收指标。；针对 A 股加入涨跌停、集合竞价、午间停盘与有限可卖量，并按市场状态验证。

关键边界：只有五只美股与一个测试月份。；实验中用测试月校准干预规模用于诊断，不能偷换为实时交易策略可预知参数。

### EP11 PROMOTE

Can We Stop the Backtest Yet? Anytime-Valid Promotion Tests for Adaptive Financial Research Agents。状态：ICAIF 2026 官方名单确认 poster 录用；截至截止日会议未举行；作者预印本；详见原卡。阅读：定向全文。[论文原始来源](https://arxiv.org/abs/2611.03384)。

核心思想：PROMOTE 给新候选登记不可返还的检验预算，只用出生后净收益的有界分数更新 e-process，以控制持续搜索中的错误晋级概率。

本站映射：采纳策略版本、候选出生时刻、数据快照、费用模型及全部淘汰历史的可追溯登记。；区分统计检验预算与账户资金；冠军资金配置不由 e 值机械决定。

关键边界：被检验对象是裁剪后的有界净分数，其条件均值与未裁剪平均收益不是同一命题。；长期累积证据不会自动随优势衰退而失效；直接滚动删除旧乘积破坏原定理。

### EP12 MACE

Realistic Market Impact Modeling for Reinforcement Learning Trading Environments。状态：arXiv 2026 预印本；未核实正式会议/期刊录用；详见原卡。阅读：定向全文。[论文原始来源](https://arxiv.org/abs/2603.29086)。

核心思想：MACE 以可替换执行成本模块比较固定比例费用、Almgren–Chriss 类冲击和衰减冲击，记录交易参与率与换手。

本站映射：优先建设全站共享的费用/滑点/冲击插件；选股、配置、做T与回测使用同一版本。；加入板块、交易通道和券商收费，参数按生效日期而非全历史常数存储。

关键边界：作者承认静态成分的存活者偏差。；“每轮 OOS 评估/选择最佳 OOS Sharpe”与“最终未见2025”需用代码澄清是否不同数据段；不能直接接受完全未触碰的测试集说法。

### EP13 Implementable Efficient Frontier

Machine Learning and the Implementable Efficient Frontier。状态：The Review of Financial Studies 39(10):3035–3078，正式顶级金融期刊；DOI 10.1093/rfs/hhag022；不是2026首次提出；详见原卡。阅读：摘要及正文节选。[论文原始来源](https://academic.oup.com/rfs/article/39/10/3035/8524346)。

核心思想：把机器学习收益预测放入含交易成本与多期持有决策的组合问题，评价可实施有效前沿及经济意义上的特征贡献。

本站映射：将“给现有账户增加多少净收益/效用”作为因子、模型和策略的统一经济检验。；长短周期预测共同服务同一目标库存和交易成本函数；不要让各期限互相高频打架。

关键边界：作者仓库工作论文版本早于最终期刊版，需核对配置差异。；原始金融数据许可与计算资源需要单独落实。

### EP14 Grid Wealth Decomposition

Volatility Harvesting or Inventory Risk? Market-Wide Evidence on Grid Trading in China's Equity ETF Market。状态：SSRN 7429819 工作论文/预印本；未核实正式录用；详见原卡。阅读：摘要。[论文原始来源](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7429819)。

核心思想：将网格交易和相同初始风险敞口的静态组合比较，重点区分波动收益与额外库存风险。

本站映射：立即采用匹配初始库存/现金的全账户净值比较，不以“已完成网格价差”代替盈利。；将剩余库存按同一时点市价计入；正T、倒T和只完成一条腿均不能隐藏。

关键边界：工作论文且本次只读摘要，不能据此支持任何特定网格收益数字。；经济机制可能受样本 ETF 退出、分红、交易限制与初始敞口定义影响，需阅读全文复核。

### EP15 EDGMGT

Decoupling Grid Triggers from Trade Decisions: A Constrained Event-Driven Reinforcement Learning Architecture for ETF Trading。状态：SSRN 7313639 工作论文/预印本；未核实正式录用；详见原卡。阅读：摘要。[论文原始来源](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7313639)。

核心思想：EDGMGT 将网格越界作为事件，之后由有动作掩码的 PPO 选择买/卖/持有；不要求每次越界都成交。

本站映射：可以直接采用“触发事件 ≠ 下单命令”的接口。；掩码由全账户可卖量、保留资金和交易规则产生，策略不能自己创建独立钱包。

关键边界：单 ETF、较短测试、摘要读取深度，不支持“RL 网格已经有效”的结论。；可行动作掩码是否阻止全部非法交易，需要代码和账户转移测试。

## 4 多方核验中的具体差异

FinRAQ的Anthology网页摘要写6只股票，正式PDF写21个资产；记录采用PDF并保留冲突。FactorMiner的十分钟预测目标不能自动变成新买A股的十分钟可卖操作。AlphaForgeBench主评估零交易成本，不能直接拿其收益晋级。KTD-Fin的主要设定是价格输入，不能替代新闻增量试验。[AP08][AP06][AP11][AP10]

ICML稀疏组合论文原版尚未包含普通A股账户所需的全部长仓、杠杆和换手约束；AAMAS执行实验排除了涨跌停日；MACE对最终OOS与跨epoch选择OOS的表述有待代码澄清。这些限制决定复现任务如何改写，不是简单给论文贴好坏标签。[EP03][EP08][EP12]

ICAIF官方会议时间为2026年11月14至17日，因此截止日只能标注官方确认录用与可读作者稿，不能说会议已召开。两篇SSRN网格工作未取得全文，只纳入机制研究候选，不补造实验细节。[EP10][EP11][EP14][EP15]

## 5 持续发现与晋级

官方proceedings/作者稿更新后，先按DOI、arXiv版本和规范题名合并，再检查发表状态、公开日期和读深度。每一项思想生成Hypothesis，绑定数据可得性、比较基线、费用、规则和消融。去掉无法复现的收益承诺，保留可验证方法；失败试验进入负结果库。跨源状态不一致时保持disputed，不因自动检索而擅自升级正式状态。

完整发现记录包括query、provider、executed_at、venue/year、命中数、已读/未读、排除理由和下一检查时间。发现服务可以按日/周自动运行；本次没有声称该任务已在服务器持续执行。论文原文术语仅作证据记录，本站正式字段和接口以contracts/v3.schema.json及contracts/openapi.json为准。
