# 2026 执行、组合与策略闭环研究证据册

**知识截点：2026-11-10。核心文献 15 篇。** 本册用于支持架构取舍和复现实验，不是已验证收益策略清单。正式会议、已录用但未开会、作者预印本和仅摘要阅读分别标注。没有任何作者结果经过本轮独立复跑。

## 结论先行

值得先做的是统一账户、成本模型、时点数据和可比较研究接口。2026 论文提供了有价值的配置、状态路由、执行和生成市场方法，但论文中的收益表不能直接证明在 A 股账户上可实施。少数最相关正式论文仍使用事后成分、删除难成交日、忽略长仓/换手约束，或只有有限股票与单一时期。工程上应吸收有根据的机制，然后在同一账户和数据条件下比较它们。

三类事实尤其影响本站设计：STABLE 在 CSI300 样本的 Sharpe 为负；ICML 稀疏决策优化原模型明确不含长仓/杠杆/换手约束；AAMAS 执行论文删去了涨跌停日。这些不是拒绝技术的理由，而是移植时必须替换的实验条件。网格方面，两篇 2026 中国 ETF 预印本只取得官方摘要，因此不引用它们的详细收益数字；账户净值和库存机会成本的评价则可独立用现金流恒等式确定。

## 阅读深度与采用等级

|标记|含义|
|---|---|
|D1|官方元数据与摘要，未验证全文细节|
|D2|摘要/正文片段加作者代码等交叉证据，全文未完整取得|
|D3|读原文方法、数据、评估和相关局限；未复跑|
|D4|独立复现，本轮为零|
|A|工程接口、经济评价或验证方法可纳设计；不代表策略可直接实盘|
|B|较强研究候选，在统一环境重新比较|
|C|探索方向或证据不足，保留研究池|

## 核心文献总览

|ID|论文|正式状态|深度|层次|采用|
|---|---|---|---|---|---|
|EP01|[AlphaQuanter: An End-to-End Tool-Augmented Agentic Reinforcement Learning Framework for Stock Trading](https://aclanthology.org/2026.findings-acl.456/)|Findings of ACL 2026，正式论文；不是 ACL 主会论文|D3|selection, allocation, exit|B|
|EP02|[STABLE: Shift-Tolerant Allocation via Black-Litterman Using Conditional Diffusion Estimates](https://proceedings.iclr.cc/paper_files/paper/2026/hash/4d4dbc5c955c5d273eed12d565335068-Abstract-Conference.html)|ICLR 2026，正式会议论文|D3|allocation|B|
|EP03|[Decision-focused Sparse Tangent Portfolio Optimization](https://proceedings.mlr.press/v306/jeon26a.html)|ICML 2026，PMLR 306 正式论文|D3|selection, allocation|B|
|EP04|[A Linearly Convergent Proximal Subgradient Algorithm for Sparse Portfolio Optimization with Transaction Cost](https://proceedings.mlr.press/v306/yao26s.html)|ICML 2026，PMLR 306 正式论文|D3|allocation|B|
|EP05|[Regime-Adaptive Continual Learning for Portfolio Management](https://arxiv.org/abs/2606.00143)|KDD 2026 正式录用/会议论文；DOI 10.1145/3770855.3817620|D3|allocation|B|
|EP06|[FineFT: Efficient and Risk-Aware Ensemble Reinforcement Learning for Futures Trading](https://arxiv.org/abs/2512.23773)|KDD 2026 正式论文；DOI 10.1145/3770854.3780187；思想首次公开于 2025|D3|allocation, overlay, execution|B|
|EP07|[Controllable Financial Market Generation with Diffusion Guided Meta Agent](https://ojs.aaai.org/index.php/AAAI/article/view/37009)|AAAI 2026 正式论文（DigMA）；DOI 10.1609/aaai.v40i1.37009；首次公开于 2024|D3|execution, overlay|C|
|EP08|[Dynamic Action Space Reinforcement Learning for Optimal Trading Execution](https://ifaamas.org/Proceedings/aamas2026/pdfs/GFLB4392.pdf)|AAMAS 2026 Research Track，正式论文；DOI 10.65109/GFLB4392|D3|execution|B|
|EP09|[Reinforcement learning for continuous-time optimal execution: actor–critic algorithm and error analysis](https://link.springer.com/article/10.1007/s00780-026-00589-5)|Finance and Stochastics 30:597–655，正式期刊论文；不是 JF/JFE/RFS|D3|execution|B|
|EP10|[LOB-Impact Benchmark: Emergent Market Impact in Generative Limit Order Book Models](https://arxiv.org/abs/2610.27100)|ICAIF 2026 官方名单确认 oral 录用；截至 2026-11-10 会议尚未举行；作者预印本可读|D3|execution|A|
|EP11|[Can We Stop the Backtest Yet? Anytime-Valid Promotion Tests for Adaptive Financial Research Agents](https://arxiv.org/abs/2611.03384)|ICAIF 2026 官方名单确认 poster 录用；截至截止日会议未举行；作者预印本|D3|selection, allocation|A|
|EP12|[Realistic Market Impact Modeling for Reinforcement Learning Trading Environments](https://arxiv.org/abs/2603.29086)|arXiv 2026 预印本；未核实正式会议/期刊录用|D3|allocation, execution|A|
|EP13|[Machine Learning and the Implementable Efficient Frontier](https://academic.oup.com/rfs/article/39/10/3035/8524346)|The Review of Financial Studies 39(10):3035–3078，正式顶级金融期刊；DOI 10.1093/rfs/hhag022；不是2026首次提出|D2|selection, allocation|A|
|EP14|[Volatility Harvesting or Inventory Risk? Market-Wide Evidence on Grid Trading in China's Equity ETF Market](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7429819)|SSRN 7429819 工作论文/预印本；未核实正式录用|D1|overlay|C|
|EP15|[Decoupling Grid Triggers from Trade Decisions: A Constrained Event-Driven Reinforcement Learning Architecture for ETF Trading](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7313639)|SSRN 7313639 工作论文/预印本；未核实正式录用|D1|overlay, execution|C|

## 完整证据卡

### EP01 · AlphaQuanter: An End-to-End Tool-Augmented Agentic Reinforcement Learning Framework for Stock Trading

原始来源：https://aclanthology.org/2026.findings-acl.456/

- **时间：** formal_publication：2026-07；first_public_date_verified：未核实 / 未发现
- **正式状态：** Findings of ACL 2026，正式论文；不是 ACL 主会论文
- **作者代码：** https://github.com/horizon-llm/AlphaQuanter
- **阅读深度：** level：D3；read：正式 PDF 的方法、数据切分、费用、结果与局限章节；未运行代码
- **核心方法：** 以 GRPO 训练单个工具调用型语言模型，按需查询行情、基本面、新闻及宏观工具后输出日频买入/卖出/持有。
- **市场与数据：** market：美国五只大型科技股 GOOGL/MSFT/META/NVDA/TSLA；逐只交易；train：2022-09-01—2024-03-30；validation：2024-05-15—2024-11-14；test：2025-01-01—2025-06-30
- **评价设计：** 三次独立种子；报告收益、夏普、最大回撤和工具使用消融。各单资产结果的平均数不等于共享资金组合的结果。
- **成本：** 买卖单边费率 0.001；买入使用资金系数 0.9；下一日收盘成交式简化，不包含真实排队与自身冲击。
- **基线：** 买入持有；MACD；均值回归；A2C/PPO；Chronos-2；FinMem；TradingAgents；相同 Agent 不进行 RL
- **作者主张：** 作者报告其受训 Agent 在该五股样本中优于所列基线；这是有限资产和测试期的实验结论。
- **证据边界：** 作者明确承认单资产范围、固定工具集合和上下文增长限制。；五只美国大盘股不能验证 A 股全市场选股；新闻字段的原始可见时间及版本需单独复核。
- **本站 A 股适配建议（本报告判断）：** 采纳按需工具路由与结构化证据输出；将选股预测和账户下单权限分开。；输入必须带 available_at；同一时点的新闻事实由全站共享，避免每个 Agent 重建不一致消息库。；重建动态 A 股股票池及可交易日，比较相同信息预算下的非 RL、纯规则与模型组合。
- **必要输入：** OHLCV；fundamentals_asof；news_event_id；available_at；macro_vintage；tool_result_version；account_snapshot
- **策略层次：** selection；allocation；exit
- **复现计划：** 锁定原文配置，复跑五股作为接口正确性检查。；在共享现金、多资产、T+1、最低佣金及涨跌停环境中重新训练和评价。；测量工具调用的增量信息、净收益、延迟和成本；不能只比较语言解释质量。
- **采用结论：** level：B；decision：工具路由可进入设计；交易策略须独立复现，不沿用论文收益承诺。
- **检索证据ID：** turn36view3；turn38view0；turn46view1；turn97view2；turn97view3

### EP02 · STABLE: Shift-Tolerant Allocation via Black-Litterman Using Conditional Diffusion Estimates

原始来源：https://proceedings.iclr.cc/paper_files/paper/2026/hash/4d4dbc5c955c5d273eed12d565335068-Abstract-Conference.html

- **时间：** formal_publication：2026；first_public_date_verified：未核实 / 未发现
- **正式状态：** ICLR 2026，正式会议论文
- **作者代码：** 未核实 / 未发现
- **阅读深度：** level：D3；read：正式 PDF 的模型、数据构造、结果表与相关局限；未找到可核实作者代码链接
- **核心方法：** 宏观与个股条件扩散生成收益轨迹，估计联合收益分布，再将均值/协方差信息送入 Black–Litterman 配置。
- **市场与数据：** market：美国 55、中国 CSI300 55、欧洲 37、韩国 44 只股票；train：2013-01—2024-09；test：截至 2025-03；universe：按 2025-03-31 规模选各行业股票并要求长历史完整
- **评价设计：** 跨四个地区比较；论文列出固定种子。CSI300 的 STABLE 测试夏普为 -0.41，相对较好不代表绝对盈利。
- **成本：** 正式 PDF 未检出明确 transaction 费用建模；不能把表中收益解释为完整扣费后实盘结果。
- **基线：** CRP；MVO；MOM；DeepTrader；MetaTrader；AlphaMix
- **作者主张：** 作者报告条件分布估计和宏观融合改善跨市场配置的相对表现。
- **证据边界：** 按测试期末规模与完整历史选择股票存在事后成分/存活者问题。；标准归一化逆协方差权重表达式没有自动施加 A 股长仓、手数与换手约束。
- **本站 A 股适配建议（本报告判断）：** 保留分布预测、观点不确定性和宏观信息融合；把最终配置交给统一约束优化器。；宏观数据使用当时发布版本，回测股票池使用历史时点成分；需重做完整时间外测试。；同时检验点预测、bootstrap 分布与条件扩散；生成样本数量不是额外独立市场证据。
- **必要输入：** historical_returns；macro_vintage；sector；market_cap_asof；predictive_mean；predictive_covariance；view_uncertainty
- **策略层次：** allocation
- **复现计划：** 复现文中四地设置并明确其选择偏差。；替换为时点股票池和实际可交易约束，补全费用、换手和容量曲线。；记录宏观信息增量和扩散模型额外贡献，防止把复杂度当效果。
- **采用结论：** level：B；decision：纳入分布预测与配置比较池；原始权重公式和收益结果不直接上线。
- **检索证据ID：** turn61view0；turn70view3；turn74view1；turn74view0

### EP03 · Decision-focused Sparse Tangent Portfolio Optimization

原始来源：https://proceedings.mlr.press/v306/jeon26a.html

- **时间：** formal_publication：2026-07；arxiv_v1：2026-07-01
- **正式状态：** ICML 2026，PMLR 306 正式论文
- **作者代码：** https://github.com/feuerwerksh/Diffble-card-SR
- **阅读深度：** level：D3；read：官方 PMLR PDF 已下载，读方法、数据、附录和局限；读作者仓库 README；未运行
- **核心方法：** 将可微稀疏 Sharpe 优化层接入预测模型，以决策损失与预测误差混合训练，并用可微 top-k 选择持股。
- **市场与数据：** market：Euro Stoxx 50、FTSE100、KOSPI200、Nikkei225；period：2016-01—2025-12；split：按时间约 80/20；100 日历史输入；universe：仅稳定成员；忽略期间进出指数者
- **评价设计：** 多个持股数设置、五个种子；比较样本外收益风险。
- **成本：** 作者明确未加入换手惩罚、长仓约束和杠杆限制；不能视为净费后 A 股组合优化。
- **基线：** 历史均值配置；预测后优化；OSCAR；SD-relaxation；mSSRM-PGA
- **作者主张：** 作者报告决策感知训练在较大股票集合上改善稀疏组合的样本外 Sharpe。
- **证据边界：** 数据池过滤有存活者偏差。；作者代码取当前成分并删除不完整历史，重新下载会随数据版本改变。；允许的权重集合不同于普通 A 股现金账户；收敛/可微性不等于可交易性。
- **本站 A 股适配建议（本报告判断）：** 把可微决策层当可插拔研究实现，与不可微但约束完整的优化器公平比较。；稀疏持股数、行业限额、单股上限和现金缓冲均为显式账户参数。；预测训练必须能接收同一账户真实成本与可卖库存状态，而不是事后裁切权重掩盖模型缺陷。
- **必要输入：** return_forecasts；covariance；cardinality_budget；current_weights；cost_estimates；eligible_universe
- **策略层次：** selection；allocation
- **复现计划：** 先复现原配置确认梯度与稀疏性。；构造动态股票池、长仓、换手、费率、涨跌停与 T+1 约束版本。；用等权、简单动量和成本感知二阶段优化作为必须击败的基线。
- **采用结论：** level：B；decision：适合配置研究；原作者实现不符合普通 A 股现金账户约束。
- **检索证据ID：** turn61search1；turn66view2；turn105view0；turn106view1

### EP04 · A Linearly Convergent Proximal Subgradient Algorithm for Sparse Portfolio Optimization with Transaction Cost

原始来源：https://proceedings.mlr.press/v306/yao26s.html

- **时间：** formal_publication：2026-07
- **正式状态：** ICML 2026，PMLR 306 正式论文
- **作者代码：** https://github.com/Ting221/STCO
- **阅读深度：** level：D3；read：官方 PMLR PDF 已下载，读优化模型、数据表、费用与代码说明；代码地址由正式 PDF 核实，仓库未成功读取
- **核心方法：** 以稀疏持股与交易成本构造优化问题，采用差分凸松弛和近端次梯度算法；理论给出特定条件下的线性收敛。
- **市场与数据：** market：NYSE(O) 36 股、NYSE(N) 23 股、TSE 88 股、MSCI 24 地区指数；period：各数据段分别位于 1962—2010；不是 2026 交易数据
- **评价设计：** 终值、费用敏感性及数值优化表现；收敛对象是算法临界点，不能解释成金融收益保证。
- **成本：** 实验含 0、0.25%、0.5% 费用及更广费率敏感性；使用其比例成本模型，非 A 股税费与盘口机制全实现。
- **基线：** UBAH；Best hindsight；BCRP hindsight；TCO1/2；SSPO；S1/2/3；DENRPO
- **作者主张：** 作者报告优化效率和若干传统投资组合数据集上的净财富优势。
- **证据边界：** 旧小样本市场集不能验证当前 A 股表现。；Best/BCRP 等事后最优基准只是诊断上界，不能作为可部署竞争者。；公式费用与实际成交费用、参与率冲击不是一回事。
- **本站 A 股适配建议（本报告判断）：** 采纳成本和稀疏度进入同一目标函数的思想，作为可解释 CPU 基准。；对账成本单位、单边/双边换手和现金权重；保留显式的 no-trade 区域。；不因复杂求解器的数值收敛就提升其资金分配等级。
- **必要输入：** expected_returns；current_weights；transaction_cost_schedule；cardinality_budget；portfolio_constraints
- **策略层次：** allocation
- **复现计划：** 复现原文公开小数据与收敛曲线。；移植到同一时点 A 股数据、成本函数和账户约束，比较简单二次优化。；记录求解时延、最优性/可行性残差与净收益分别发生什么变化。
- **采用结论：** level：B；decision：作为稀疏成本优化的候选基线；不移植旧样本盈利率。
- **检索证据ID：** turn59search0；turn94view2

### EP05 · Regime-Adaptive Continual Learning for Portfolio Management

原始来源：https://arxiv.org/abs/2606.00143

- **时间：** arxiv_v1：2026-05-29；conference：2026-08-09—2026-08-13
- **正式状态：** KDD 2026 正式录用/会议论文；DOI 10.1145/3770855.3817620
- **作者代码：** https://github.com/Dumail/ReCAP
- **阅读深度：** level：D3；read：作者 PDF 已下载，读方法、数据、成本和持续学习对照；作者仓库说明已读，未运行
- **核心方法：** ReCAP 用在线状态变化检测器维护市场阶段，保存 PPO 的策略参数增量；门控混合阶段经验，适配新阶段并合并相似增量。
- **市场与数据：** market：DOW 29、NASDAQ 73、S&P500 398、Nikkei 29、商品 ETF 7；pretrain：2008-05-01—2020-04-30；evaluation：2020-05-01—2025-04-29；universe：要求连续上市/完整数据
- **评价设计：** 十个种子；匹配训练更新预算，比较多种持续学习方式，并做额外成本实验。
- **成本：** 基准交易费 10bp，另加 5/10bp 敏感性；未验证 A 股最低收费、卖出税与 T+1。
- **基线：** B&H；CRP；EG；UP；OLMAR；WMAMR；静态预训练；重训；微调；经验回放；EWC；CoR
- **作者主张：** 作者报告阶段知识复用优于所列持续学习方案。
- **证据边界：** 连续上市筛选存在存活者偏差。；代码明确是最小可运行版本，训练规模与完整论文复现存在差别。；参数增量合成依赖共同骨架，不支持任意异构策略参数直接平均。
- **本站 A 股适配建议（本报告判断）：** 用过去信息估计阶段概率，并登记检测延迟、置信度和阶段切换费用。；把任意策略的组合统一为预测或目标权重接口；只有同骨架模型才可以研究参数增量合成。；状态库删除/合并要保留评估历史，避免淘汰差策略后重写成绩。
- **必要输入：** market_state_features；regime_posterior；base_model_version；policy_delta；realized_costs；training_budget
- **策略层次：** allocation
- **复现计划：** 核对论文设置与简化仓库差异后建立锁定配置。；动态股票池、滚动训练、严格时间外逐日重演，所有更新只能使用已经成熟的标签。；对照简单指数加权专家组合、定期重训和冻结模型，计算切换收益及额外换手。
- **采用结论：** level：B；decision：优先验证阶段路由和知识复用；不直接把任意战法拼成一个神经网络。
- **检索证据ID：** turn60search0；turn76academia9；turn77view2

### EP06 · FineFT: Efficient and Risk-Aware Ensemble Reinforcement Learning for Futures Trading

原始来源：https://arxiv.org/abs/2512.23773

- **时间：** arxiv_v1：2025-12-29；formal_publication：2026
- **正式状态：** KDD 2026 正式论文；DOI 10.1145/3770854.3780187；思想首次公开于 2025
- **作者代码：** https://github.com/qinmoelei/FineFT_code_space
- **阅读深度：** level：D3；read：作者全文的方法、市场仿真、数据、基线与结果；未运行代码
- **核心方法：** 以选择性 TD 更新训练专家，筛选各状态中表现较好的策略，再用能力/分布外状态路由决定采用哪个专家。
- **市场与数据：** market：Binance BNB/BTC/DOT/ETH USDT 永续合约；train：2022；validation：2023-01—2023-05；test：2023-06—2024-01-01；frequency：分钟级，含 5 倍杠杆设置
- **评价设计：** 与十二种交易/集成方法对比，并研究训练效率与状态外表现。
- **成本：** 包含订单簿深度消耗、佣金和资金费；该费用/动作模型属于加密永续合约。
- **基线：** DQN 类模型；QR-DQN；集成 RL；传统规则；Winnow 等组合方法
- **作者主张：** 作者报告专家选择改善风险收益和训练效率；数值不能迁移为 A 股收益预期。
- **证据边界：** 24 小时双向杠杆合约与 A 股现金股票交易条件不同。；筛选、路由与评估阶段必须清楚隔离，避免按测试收益选择专家。
- **本站 A 股适配建议（本报告判断）：** 采用“符合条件的专家路由”而不是买卖票数相加。；专家输出统一为预期增量收益、持有期限及目标库存；统一组合器处理现金与可卖持仓。；保留简单规则与不交易作为合法专家，比较路由价值是否超过额外交易成本。
- **必要输入：** technical_features；orderbook_depth；inventory；expert_capability；regime_features；cost_model
- **策略层次：** allocation；overlay；execution
- **复现计划：** 先复现作者合约实验，避免把移植差异误报成复现失败。；另建 A 股长仓及 T+1 动作空间，单独训练评估。；比较最优单专家、平均预测、动态专家路由，分别报告训练预算和换手。
- **采用结论：** level：B；decision：路由设计值得采用；加密合约策略本身只进入研究池。
- **检索证据ID：** turn60search0；turn70view1；turn73view1；turn89view0

### EP07 · Controllable Financial Market Generation with Diffusion Guided Meta Agent

原始来源：https://ojs.aaai.org/index.php/AAAI/article/view/37009

- **时间：** arxiv_v1：2024-08-23；arxiv_v3：2026-01-15；formal_publication：2026-03-14
- **正式状态：** AAAI 2026 正式论文（DigMA）；DOI 10.1609/aaai.v40i1.37009；首次公开于 2024
- **作者代码：** https://github.com/microsoft/TimeCraft
- **阅读深度：** level：D3；read：官方刊页元数据/摘要与作者 arXiv v3 方法、数据、结果；AAAI 正文页有反爬，使用作者公开稿
- **核心方法：** 条件扩散生成中间价收益和订单到达参数，再由带经济先验的元 Agent 生成订单流。
- **市场与数据：** market：Wind 深交所 2020 年主板 1452 股、创业板 854 股；split：以股票日随机抽取各 5000 验证/测试实例，其余训练；不是纯时间外划分
- **评价设计：** 比较统计特征、条件控制与模拟环境中的下游交易表现。
- **成本：** 生成质量实验不等价于一套校准完整交易税费、排队和自身冲击的真实账户回测。
- **基线：** RFD；RMSC；LOBGAN
- **作者主张：** 作者报告其生成机制可更好匹配若干市场特征并控制目标市场状态。
- **证据边界：** Wind 完整数据受许可约束，公开代码不意味着原数据可任意再分发。；同年股票日随机划分不能证明跨时期泛化。；生成统计特征逼真不自动意味着对本策略下单干预反应真实。
- **本站 A 股适配建议（本报告判断）：** 将生成器作为压力情景和模型训练的补充，不替代真实历史回放。；按时间、股票和未见市场阶段分层留出，并加入 LOB-Impact 式自身订单干预测试。；条件新闻情景只表示一个假设世界；真实新闻效应要由历史可见数据单独识别。
- **必要输入：** level2_orderbook；order_arrivals；midprice_returns；market_regime；simulation_seed；scenario_condition
- **策略层次：** execution；overlay
- **复现计划：** 复现公开示例与统计量。；在有许可的 A 股逐笔数据上重建时间外测试。；注入不同规模母单，检查冲击、恢复和队列机制，再决定能否用于策略训练。
- **采用结论：** level：C；decision：用于可控情景研究；在干预有效性验证前，不承担实盘收益证明。
- **检索证据ID：** turn60search5；turn70view2；turn73view2；turn92view2；turn98view6

### EP08 · Dynamic Action Space Reinforcement Learning for Optimal Trading Execution

原始来源：https://ifaamas.org/Proceedings/aamas2026/pdfs/GFLB4392.pdf

- **时间：** conference：2026-05-25—2026-05-29
- **正式状态：** AAMAS 2026 Research Track，正式论文；DOI 10.65109/GFLB4392
- **作者代码：** 未核实 / 未发现
- **阅读深度：** level：D3；read：官方 PDF 方法、A 股数据、执行目标、对照与指标
- **核心方法：** 根据当前市场状态缩减执行动作集合，结合自适应探索与经验回放重整，学习大额卖出母单的拆分执行。
- **市场与数据：** market：8 只上证50成分股、十档盘口；period：2014-06—2015-03；split：前约80%训练，后约20%测试；filter：排除了涨跌停日
- **评价设计：** 使用相对 VWAP 滑点与学习效率指标；并非选股收益回测。
- **成本：** 执行价格与流动性反映部分成交成本；没有核实最低佣金、卖出税和全部真实排队模型。
- **基线：** TWAP；Arrival Price；DQN；DDQN；A2C
- **作者主张：** 作者报告相对比较模型降低执行成本并提高学习效率。
- **证据边界：** 十年前的八股样本且排除涨跌停日，不能验证当前全 A 股执行。；母单数量规模与市场参与率须逐例重核；数据单次回放不一定完整模拟自身冲击。
- **本站 A 股适配建议（本报告判断）：** 让 RL 只决定已批准母单的拆分、时机和价格类型，不让执行器重新发明选股方向。；动作可行域来自实时可卖量、涨跌停、交易时段、板块手数和已保留订单。；加入部分成交、撤单回报延迟、午休和集合竞价；保留 TWAP/VWAP 作为可解释基线。
- **必要输入：** parent_order；remaining_qty；remaining_time；sellable_qty；orderbook；queue_state；price_limits；reserved_qty
- **策略层次：** execution
- **复现计划：** 在相同历史八股条件下检验原结果。；扩充当前动态股票池且不删除难成交日，按参与率及母单方向分组。；用成交后净滑点、完成率和未完成头寸终值联合评价。
- **采用结论：** level：B；decision：动作可行域思想可采用；执行优势须在当前数据重新证明。
- **检索证据ID：** turn81view0；turn97view0；turn92view1

### EP09 · Reinforcement learning for continuous-time optimal execution: actor–critic algorithm and error analysis

原始来源：https://link.springer.com/article/10.1007/s00780-026-00589-5

- **时间：** formal_publication：2026-03-10
- **正式状态：** Finance and Stochastics 30:597–655，正式期刊论文；不是 JF/JFE/RFS
- **作者代码：** 未核实 / 未发现
- **阅读深度：** level：D3；read：出版社全文的控制模型、算法、模拟/真实市场数据与误差结论
- **核心方法：** 在熵正则的均值—二次变差执行控制下构造可解析的策略/价值参数化，通过 actor–critic 与重校准学习执行路径。
- **市场与数据：** market：七只 Nasdaq 股票 LOBSTER 数据及三类模拟市场；period：2016Q3—2017Q1；protocol：以前一季度训练后一季度评估；约10%日均量母单，约6小时执行
- **评价设计：** 理论误差与收敛分析，结合模拟和历史订单流执行比较。
- **成本：** 研究模型中的冲击和执行目标；不应扩张成所有现实微观结构摩擦均已覆盖。
- **基线：** 经典参数估计后代入控制解；SAC；不同模拟器
- **作者主张：** 作者在其设定下给出学习收敛/误差结果，并报告执行质量改善。
- **证据边界：** 理论依赖模型、步长及正则假设。；得到的是执行时间表/速率决策，不自动解决真实限价单排队。；样本为美国股票，不能验证 A 股 T+1 与集合竞价机制。
- **本站 A 股适配建议（本报告判断）：** 作为有解析结构的 RL 执行基准，与复杂神经网络进行同成本比较。；按 A 股交易时段重新标定波动、冲击和可用库存；午休不能当普通连续交易时段。；策略方向与母单总量由配置层决定，执行层只优化交易质量。
- **必要输入：** execution_horizon；parent_qty；remaining_qty；volatility；impact_parameters；orderflow；trading_calendar
- **策略层次：** execution
- **复现计划：** 先复现解析模拟与经典控制解。；在当前 A 股订单数据重新估计冲击与时段效应。；检验不同母单量级及市场阶段的改善，并检查未完成部分的机会成本。
- **采用结论：** level：B；decision：适合作为执行优化基准；理论保证的适用条件必须保留。
- **检索证据ID：** turn52view3；turn89view5

### EP10 · LOB-Impact Benchmark: Emergent Market Impact in Generative Limit Order Book Models

原始来源：https://arxiv.org/abs/2610.27100

- **时间：** arxiv_v1：2026-10-19；conference：2026-11-14—2026-11-17
- **正式状态：** ICAIF 2026 官方名单确认 oral 录用；截至 2026-11-10 会议尚未举行；作者预印本可读
- **作者代码：** https://github.com/george-nigmatulin/lob_impact
- **阅读深度：** level：D3；read：作者全文方法、注入实验、市场样本、结果与局限；录用状态用官方名单交叉核对
- **核心方法：** 通过向生成订单簿注入母单，并比较可见/隐藏订单干预，测量自身市场冲击和执行后的恢复。
- **市场与数据：** market：EA、NVDA、MSFT、AMD、GOOG 五只 Nasdaq 股票；test：2026-01 订单簿保留月份；intervention：100 个子单的母单，按约10%参与率校准实验
- **评价设计：** 比较五种神经生成器及机械/历史回放对照；既检验自由生成，也检验干预响应。
- **成本：** 这是市场冲击模型验证，不是包含佣金、税费的完整策略收益测算。
- **基线：** 历史回放负对照；机械冲击正对照；五类神经订单簿生成器
- **作者主张：** 作者发现生成器的冲击弹性与执行后恢复仍不理想，提醒仅拟合市场统计量不足以验证交易模拟器。
- **证据边界：** 只有五只美股与一个测试月份。；实验中用测试月校准干预规模用于诊断，不能偷换为实时交易策略可预知参数。；不应要求任何市场任何量级都遵守固定平方根指数。
- **本站 A 股适配建议（本报告判断）：** 把订单干预、冲击恢复和容量响应列为模拟器验收指标。；针对 A 股加入涨跌停、集合竞价、午间停盘与有限可卖量，并按市场状态验证。；拒绝用不受自身下单影响的静态历史价格证明大资金策略可扩容。
- **必要输入：** event_orderbook；agent_injected_orders；participation_rate；midprice_path；post_trade_relaxation；simulator_version
- **策略层次：** execution
- **复现计划：** 跑作者公开 benchmark 检验每种生成器。；扩展 A 股样本与时段，和真实母单冲击曲线校准。；分开报告生成拟合分数和策略干预响应，不能合成一个掩盖缺陷的总分。
- **采用结论：** level：A；decision：优先采纳模拟器验收方法；不据此宣称已有可盈利策略。
- **检索证据ID：** turn52view0；turn73view3；turn92view3；turn36view0

### EP11 · Can We Stop the Backtest Yet? Anytime-Valid Promotion Tests for Adaptive Financial Research Agents

原始来源：https://arxiv.org/abs/2611.03384

- **时间：** arxiv_v1：2026-11-02；conference：2026-11-14—2026-11-17
- **正式状态：** ICAIF 2026 官方名单确认 poster 录用；截至截止日会议未举行；作者预印本
- **作者代码：** 未核实 / 未发现
- **阅读深度：** level：D3；read：作者全文协议、定理条件、市场与模拟实验、失败边界；未找到可独立读取的作者制品仓库
- **核心方法：** PROMOTE 给新候选登记不可返还的检验预算，只用出生后净收益的有界分数更新 e-process，以控制持续搜索中的错误晋级概率。
- **市场与数据：** market：公开 ETF 与行业序列的24条规则；另有受控零均值模拟；period：伪样本外约2015—2026-05；数据快照 2026-07-31
- **评价设计：** 3000 条共享符号零假设路径、动态候选出生模拟与完整市场回放；实际市场回放没有策略晋级。
- **成本：** 协议要求事前确定费用并按实际单边换手计扣；本卡不填尚未逐行核实的具体 bp 费率。
- **基线：** 反复使用未调整 p 值；固定期末 e 检验；逐策略不分配家族预算；有预算的随时有效晋级
- **作者主张：** 在明确的出生后条件均值非正零假设下，给出家族错误控制；它不是当前或未来盈利证书。
- **证据边界：** 被检验对象是裁剪后的有界净分数，其条件均值与未裁剪平均收益不是同一命题。；长期累积证据不会自动随优势衰退而失效；直接滚动删除旧乘积破坏原定理。；有效性有检测时延代价，不能把该门槛强制套到所有探索实验。
- **本站 A 股适配建议（本报告判断）：** 采纳策略版本、候选出生时刻、数据快照、费用模型及全部淘汰历史的可追溯登记。；区分统计检验预算与账户资金；冠军资金配置不由 e 值机械决定。；需要随时有效声明时，严格实现其预算及重验证规则；普通候选筛选仍比较经济效果与验证成本。
- **必要输入：** strategy_hash；birth_time；cost_version；bounded_score；score_scale；test_mass；e_process_state；activation_time
- **策略层次：** selection；allocation
- **复现计划：** 复现零假设模拟、晋级账本和下一时段部署边界。；用本站策略池的真实候选产生过程检验选择偏差。；同时报告错误晋级率、发现率、等待时间和增量净收益，避免只优化一个指标。
- **采用结论：** level：A；decision：立即采用研究登记与时间边界；统计晋级协议作为有明确假设的可选评估器。
- **检索证据ID：** turn52view1；turn70view6；turn92view4；turn109view1；turn36view0

### EP12 · Realistic Market Impact Modeling for Reinforcement Learning Trading Environments

原始来源：https://arxiv.org/abs/2603.29086

- **时间：** arxiv_v1：2026-03-30；arxiv_v2：2026-04-04
- **正式状态：** arXiv 2026 预印本；未核实正式会议/期刊录用
- **作者代码：** 未核实 / 未发现
- **阅读深度：** level：D3；read：作者全文环境、费用、数据、调参和结果；未核实独立可用代码仓库
- **核心方法：** MACE 以可替换执行成本模块比较固定比例费用、Almgren–Chriss 类冲击和衰减冲击，记录交易参与率与换手。
- **市场与数据：** market：NASDAQ100 股票及多种 RL 环境；period：2010-01—2026-01；universe：约2021年的静态成分；evaluation_claim：作者称2025为最终样本外，另以样本外 Sharpe 跨 epoch 调参
- **评价设计：** A2C/PPO/DDPG/SAC/TD3，默认/优化超参及不同成本模型交叉比较。
- **成本：** 固定10bp与规模、波动、流动性相关冲击模型；成本设定显著影响策略排名和交易强度。
- **基线：** 固定比例费用环境；冲击费用环境；默认超参；Optuna 调参；QQEW
- **作者主张：** 作者强调交易成本模型的变化会改变 RL 策略训练和回测结论。
- **证据边界：** 作者承认静态成分的存活者偏差。；“每轮 OOS 评估/选择最佳 OOS Sharpe”与“最终未见2025”需用代码澄清是否不同数据段；不能直接接受完全未触碰的测试集说法。；美国费用函数仍需当前 A 股真实成交校准。
- **本站 A 股适配建议（本报告判断）：** 优先建设全站共享的费用/滑点/冲击插件；选股、配置、做T与回测使用同一版本。；加入板块、交易通道和券商收费，参数按生效日期而非全历史常数存储。；展示资金规模—净收益曲线，而非只展示零成本单一 Sharpe。
- **必要输入：** trade_qty；trade_price；volatility；ADV；participation_rate；spread；impact_decay；fee_schedule_version
- **策略层次：** allocation；execution
- **复现计划：** 先用固定策略检查成本计算是否守恒且随规模合理变化。；清楚分开训练、调参和锁定终测，并保存 epoch 选择规则。；以真实成交样本拟合成本，再比较不同模型下策略排名是否稳定。
- **采用结论：** level：A；decision：采纳可替换成本环境的工程思想；论文策略成绩只作待复现参考。
- **检索证据ID：** turn36view2；turn38view1；turn50view0；turn109view2

### EP13 · Machine Learning and the Implementable Efficient Frontier

原始来源：https://academic.oup.com/rfs/article/39/10/3035/8524346

- **时间：** working_paper_first_year：2022；verified_working_paper_version：2024-06-19；online_publication：2026-03-15；issue：2026-10
- **正式状态：** The Review of Financial Studies 39(10):3035–3078，正式顶级金融期刊；DOI 10.1093/rfs/hhag022；不是2026首次提出
- **作者代码：** https://github.com/theisij/ml-and-the-implementable-efficient-frontier
- **阅读深度：** level：D2；read：正式刊页元数据/摘要与可检索正文片段、作者公开代码说明；出版社/机构 PDF 未成功获取，未声称全文核验
- **核心方法：** 把机器学习收益预测放入含交易成本与多期持有决策的组合问题，评价可实施有效前沿及经济意义上的特征贡献。
- **市场与数据：** market：美国股票；作者仓库使用 WRDS/CRSP、公司特征及借券相关数据；period：未核实 / 未发现；horizons：代码包含1—12个月预测期限；完整最终论文测试日期未核实
- **评价设计：** 关注扣费后的组合前沿及不同管理规模；最终论文所有数值尚未逐表复核。
- **成本：** 成本与持有期限共同进入组合决策；作者代码涉及交易成本和借券信息，不能直接复用于普通 A 股长仓账户。
- **基线：** 成本不敏感的预测/组合；不同多期配置与特征模型；详细最终对照配置待复现核对
- **作者主张：** 作者主张应按可实施组合净效用评价预测，而非单独按预测精度排序模型。
- **证据边界：** 作者仓库工作论文版本早于最终期刊版，需核对配置差异。；原始金融数据许可与计算资源需要单独落实。；本次未完成全文，因此不引用未经核实的特定收益倍数。
- **本站 A 股适配建议（本报告判断）：** 将“给现有账户增加多少净收益/效用”作为因子、模型和策略的统一经济检验。；长短周期预测共同服务同一目标库存和交易成本函数；不要让各期限互相高频打架。；按资金量、持有期限与现有仓位报告可实施收益，而不是只有选股命中率。
- **必要输入：** multi_horizon_forecasts；current_holdings；risk_covariance；transaction_costs；capital_size；holding_horizon
- **策略层次：** selection；allocation
- **复现计划：** 获取最终正式全文并对照作者工作论文配置，补全数据期。；先复现简单成本感知动态组合，再加入机器学习。；在 A 股长仓约束、动态股票池和统一费用模型下作公平基准。
- **采用结论：** level：A；decision：经济评价目标可直接采纳；具体模型与数值保持待完整复现。
- **检索证据ID：** turn76search2；turn81search0；turn96search1；turn97view1；turn98view4

### EP14 · Volatility Harvesting or Inventory Risk? Market-Wide Evidence on Grid Trading in China's Equity ETF Market

原始来源：https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7429819

- **时间：** ssrn_posted：2026-09-07
- **正式状态：** SSRN 7429819 工作论文/预印本；未核实正式录用
- **作者代码：** 未核实 / 未发现
- **阅读深度：** level：D1；read：SSRN 官方元数据与摘要；PDF 返回403，未阅读全文，尚不具备详细复现资格
- **核心方法：** 将网格交易和相同初始风险敞口的静态组合比较，重点区分波动收益与额外库存风险。
- **市场与数据：** market：中国股票 ETF 分钟数据；period：2009—2026，精确最后交易日未核实；grid：摘要列3%、5%、10%间距及1/3/5年窗口
- **评价设计：** 摘要报告多市场窗口与严格样本外网格间距选择；尚未核实具体划分、ETF清单和统计细节。
- **成本：** 摘要明确考虑 T+1、手数、现金/库存约束、佣金与不利滑点；具体参数未核实。
- **基线：** 相同初始敞口的静态持有组合；不同固定网格间距；样本外间距选择
- **作者主张：** 作者摘要称多数短窗口胜率并不排除左尾亏损；较长窗口平均结果不同，且间距预测没有稳定优势。
- **证据边界：** 工作论文且本次只读摘要，不能据此支持任何特定网格收益数字。；经济机制可能受样本 ETF 退出、分红、交易限制与初始敞口定义影响，需阅读全文复核。
- **本站 A 股适配建议（本报告判断）：** 立即采用匹配初始库存/现金的全账户净值比较，不以“已完成网格价差”代替盈利。；将剩余库存按同一时点市价计入；正T、倒T和只完成一条腿均不能隐藏。；ETF 是否当日回转按标的与交易通道配置，不能把所有 ETF 统一写成 T+1。
- **必要输入：** grid_anchor；grid_spacing；inventory；sellable_qty；cash_policy；fees；terminal_mark；matched_hold_baseline
- **策略层次：** overlay
- **复现计划：** 先获取作者全文和可验证数据说明，补足严格样本外细节。；建立机械网格经济账本，人工可核对每笔现金、库存和费用。；比较等敞口持有、无网格和多种网格；在趋势/震荡、上涨/下跌中分层。
- **采用结论：** level：C；decision：摘要支持研究方向；经济账本是独立可推导的工程要求，收益主张暂不采信。
- **检索证据ID：** turn84search0；turn100search0

### EP15 · Decoupling Grid Triggers from Trade Decisions: A Constrained Event-Driven Reinforcement Learning Architecture for ETF Trading

原始来源：https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7313639

- **时间：** written：2026-08-19；ssrn_posted：2026-08-20
- **正式状态：** SSRN 7313639 工作论文/预印本；未核实正式录用
- **作者代码：** 未核实 / 未发现
- **阅读深度：** level：D1；read：SSRN 官方元数据/摘要；PDF 返回403，未阅读完整方法与所有结果
- **核心方法：** EDGMGT 将网格越界作为事件，之后由有动作掩码的 PPO 选择买/卖/持有；不要求每次越界都成交。
- **市场与数据：** market：中国银行 ETF 分钟数据；period：2018—2023；test：摘要称共同测试242日，完整切分待核实
- **评价设计：** 摘要称采用时间划分、等敞口对照和配对区块 bootstrap；详细统计流程未知。
- **成本：** 动作掩码含现金、库存与持仓约束；具体费率、T+1掩码及滑点是否完整均未核实。
- **基线：** 机械网格；随机可行操作；敞口匹配策略
- **作者主张：** 作者称有选择地跳过网格触发改善该样本的风险收益；不能外推到其他 ETF 或市场。
- **证据边界：** 单 ETF、较短测试、摘要读取深度，不支持“RL 网格已经有效”的结论。；可行动作掩码是否阻止全部非法交易，需要代码和账户转移测试。
- **本站 A 股适配建议（本报告判断）：** 可以直接采用“触发事件 ≠ 下单命令”的接口。；掩码由全账户可卖量、保留资金和交易规则产生，策略不能自己创建独立钱包。；持有/跳过是主动策略动作，收益比较必须匹配暴露，否则可能只是少持仓。
- **必要输入：** grid_event；ATR；anchor_state；available_cash；sellable_qty；reserved_qty；target_inventory；policy_version
- **策略层次：** overlay；execution
- **复现计划：** 获取完整论文/代码，复核时间切分、费用和动作掩码。；机械网格先通过可计算账本验证，再加入学习策略。；在同一成交引擎、资本、初始敞口与费用下比较固定阈值、监督学习和 PPO。
- **采用结论：** level：C；decision：事件与交易决策分离值得采用；RL 优势尚待完整证据。
- **检索证据ID：** turn84search2；turn100search1

## 已读或发现的补充文献（不计完整核心覆盖）

### Signature-Informed Transformer for Asset Allocation

- 原始链接：https://proceedings.mlr.press/v306/hwang26j.html
- 状态：ICML 2026 正式论文；arXiv 首次2025-10
- 阅读：官方 PDF 已下载，读方法/数据；2020—2024美中股票月频配置，含0/5/10bp费用敏感性；未运行
- 保留理由：路径签名与尾部目标可作配置候选；因收束核心数量不另写完整卡。

### Reinforcement Learning with Scenario-Context Rollout in Portfolio Management

- 原始链接：https://arxiv.org/abs/2602.24037
- 状态：KDD 2026 正式录用/论文；arXiv2026
- 阅读：读作者方法与数据；美股/ETF 2009—2023，成本细节尚未完成核实
- 保留理由：合成奖励必须对应一致的后继状态；仅增加合成场景不能弥补 Bellman 目标不一致。

### Deep Parametric Portfolio Policies

- 原始链接：https://pubsonline.informs.org/doi/10.1287/mnsc.2025.00721
- 状态：Management Science，2026-06-03正式在线
- 阅读：正式 PDF 已下载；读目标、美国1971—2020样本与滚动切分
- 保留理由：直接按投资者效用训练配置；可作偏好目标研究，未纳核心以保持范围。

### MLP-Initialized Reinforcement Learning for Exit Timing in SPX Short-Straddle Portfolios

- 原始链接：https://arxiv.org/abs/2610.13126
- 状态：ICAIF 2026 poster 已录用；作者预印本
- 阅读：PDF已下载，读数据、买卖价与费用、监督初始化；2016—2023训练/2024—2026评估
- 保留理由：美国卖期权退出与 A 股性质不同；方法启示是必须比较强监督学习基线，不能把20个持仓槽说成20个Agent。

### Model Ambiguity versus Model Misspecification in Dynamic Portfolio Choice

- 原始链接：https://onlinelibrary.wiley.com/doi/10.1111/jofi.70027
- 状态：Journal of Finance 81(3):1741–1795，2026-01-21首次在线
- 阅读：出版社全文摘要、控制设定与引言；理论论文
- 保留理由：参数不确定性与模型形式错误应分开评估；没有可搬运的选股回测收益。

### Trading Costs and Machine-Learning Return Forecasts

- 原始链接：https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7617338
- 状态：SSRN工作论文，2026-10-16公布
- 阅读：官方摘要与作者研究页，全文未取得
- 保留理由：作者报告组合构造的成本处理可能比预测训练成本加权更重要；应与端到端配置共同消融，不能据摘要定论。

### Smart Trading Rule: A Modular Machine Learning Framework for Portfolio Optimization with Transaction Costs

- 原始链接：https://collaborate.princeton.edu/en/publications/smart-trading-rule-a-modular-machine-learning-framework-for-portf/
- 状态：Journal of Financial Data Science 8(2)，2026正式论文
- 阅读：作者机构正式元数据与摘要
- 保留理由：无摩擦目标组合后接交易/不交易层，可作为联合优化的替代基线；该刊不标为JF/JFE/RFS。

### SciPhy Reinforcement Learning for Portfolio Optimization

- 原始链接：https://arxiv.org/abs/2607.15195
- 状态：arXiv2026-07-16预印本
- 阅读：作者69页PDF已下载，读方法、数据及 oracle 信号构造
- 保留理由：包含工程化 oracle 预测，主要检验有信号条件下的配置控制；不能把其收益当可实际获得的预测优势。

### Selling at the Opening: The “T+1” Rule, Short-term Speculation, and Stock Returns

- 原始链接：https://www.sciencedirect.com/science/article/pii/S0927539826000988
- 状态：Journal of Empirical Finance 2026-10-27在线预校样；工作稿2025
- 阅读：出版社索引摘要与部分引言；完整PDF未取得
- 保留理由：收盘到开盘特征的月度横截面预测，不等于已证明日内做T盈利；剔除涨跌停后观察的影响需复核。

### Positive feedback trading, the T+1 rule, and asymmetric return reversals in China

- 原始链接：https://www.sciencedirect.com/science/article/pii/S0264999326003123
- 状态：Economic Modelling 164，2026，107783
- 阅读：出版社索引摘要；完整作者/数据窗口尚未复核
- 保留理由：与T+1次日盘中反转相关；不与其他开盘/收盘论文混作同一作用期限。相关SSRN稿不能在未核对作者前直接合并。

## 按 venue 的检索与真实覆盖日志

检索主要使用官方 proceedings、出版社、OpenReview、arXiv、SSRN 和作者代码；近期查询使用检索系统1并带 recency。下列记录是本分工的主题检索，并不构成每个 venue 所有论文的穷尽检索。

### ICLR 2026

- 查询族：site:proceedings.iclr.cc/paper_files/paper/2026 portfolio；site:openreview.net 2026 portfolio reinforcement trading
- 一级来源：https://proceedings.iclr.cc/paper_files/paper/2026/hash/4d4dbc5c955c5d273eed12d565335068-Abstract-Conference.html
- 覆盖：STABLE正式论文已读；因子发现由另一研究分工覆盖。不是对所有接收论文的逐篇全文审计。

### ICML 2026

- 查询族：site:proceedings.mlr.press/v306 portfolio transaction cost；site:proceedings.mlr.press/v306 signature asset allocation
- 一级来源：https://proceedings.mlr.press/v306/jeon26a.html；https://proceedings.mlr.press/v306/yao26s.html；https://proceedings.mlr.press/v306/hwang26j.html
- 覆盖：三篇正式PDF核实；两篇核心卡、一篇补充索引。

### NeurIPS 2026

- 查询族：site:neurips.cc/Downloads/2026 portfolio；Learning-Augmented Online Portfolio Selection Optimal Robustness Consistency Tradeoffs
- 一级来源：https://neurips.cc/Downloads/2026
- 覆盖：官方清单命中相关标题；本分工未取得该2026条目的完整方法证据，保留watchlist。不能把相似NeurIPS2025论文当成2026。

### AAAI 2026

- 查询族：site:ojs.aaai.org/index.php/AAAI/article 2026 trading execution；Controllable Financial Market Generation Diffusion Guided Meta Agent
- 一级来源：https://ojs.aaai.org/index.php/AAAI/article/view/37009；https://arxiv.org/html/2408.12991v3
- 覆盖：DigMA正式元数据与作者稿双源核实；官方页面部分反爬。首次公开年份2024明确保留。

### IJCAI 2026

- 查询族：site:ijcai.org/proceedings/2026 portfolio；site:ijcai.org/proceedings/2026 reinforcement trading
- 一级来源：https://ijcai.org/proceedings/2026/288；https://ijcai.org/proceedings/2026/492
- 覆盖：两篇因子/股票排序正式论文已发现并移交因子研究分工；本分工不重复计算核心覆盖。

### AAMAS 2026

- 查询族：site:ifaamas.org/Proceedings/aamas2026 optimal trading execution
- 一级来源：https://ifaamas.org/Proceedings/aamas2026/pdfs/GFLB4392.pdf
- 覆盖：DASRL官方全文已读；其他金融多智能体条目未穷尽。

### KDD 2026

- 查询族：KDD 2026 accepted papers portfolio reinforcement learning；Regime-Adaptive Continual Learning Portfolio Management；FineFT KDD 2026
- 一级来源：https://arxiv.org/abs/2606.00143；https://arxiv.org/abs/2512.23773；https://arxiv.org/abs/2602.24037
- 覆盖：官方接收名单、作者论文/代码交叉核实 ReCAP/FineFT/SCR。两篇核心，一篇补充。

### ICAIF 2026

- 查询族：site:icaif2026.org/accepted-papers execution portfolio；site:arxiv.org 2026 LOB Impact PROMOTE trading
- 一级来源：https://icaif2026.org/accepted-papers；https://arxiv.org/abs/2610.27100；https://arxiv.org/abs/2611.03384
- 覆盖：官方名单212篇（52oral/108poster/52proceedings-only）用于发现；本分工只深读其中少数。会议11月14—17，截止日未召开；录用、预印本、正式出版分开标注。

### JF / JFE / RFS

- 查询族：2026 Journal Finance dynamic portfolio model ambiguity；2026 Review Financial Studies implementable efficient frontier；2026 Journal Financial Economics liquidity trading costs
- 一级来源：https://onlinelibrary.wiley.com/doi/10.1111/jofi.70027；https://academic.oup.com/rfs/article/39/10/3035/8524346
- 覆盖：RFS成本感知配置纳核心，JF理论纳补充。JFE流动性条目全文覆盖不足；JFE语言模型预测论文已移交其他分工。不能声称顶刊全年全部覆盖。

### Management Science / Finance and Stochastics

- 查询族：2026 deep parametric portfolio policies；2026 actor critic continuous time optimal execution error analysis
- 一级来源：https://pubsonline.informs.org/doi/10.1287/mnsc.2025.00721；https://link.springer.com/article/10.1007/s00780-026-00589-5
- 覆盖：DPPP和EMQV均读原文相关章节；后者核心。

### SSRN / arXiv 2026

- 查询族：2026 China ETF grid trading inventory risk；2026 event driven grid reinforcement learning ETF；2026 realistic market impact reinforcement trading
- 一级来源：https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7429819；https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7313639；https://arxiv.org/abs/2603.29086
- 覆盖：核心网格2篇仅摘要，明确D1；MACE作者全文。未录用预印本绝不称顶会。

## 发现级 watchlist

以下条目不能被包装成已读论文、已掌握算法或已验证结果。

- [Learning-Augmented Online Portfolio Selection: Optimal Robustness-Consistency Tradeoffs](https://neurips.cc/Downloads/2026)：NeurIPS2026官方清单；仅标题发现，未核实全文/作者；不可与2025同方向论文合并。
- [From Simulator Labels to Real Markets: Crumble-Aware Reinforcement Learning for Trade Execution](https://icaif2026.org/accepted-papers)：ICAIF2026 oral；官方标题/录用状态，未取得全文。
- [Uncertainty-Conditioned Hierarchical Specialist Routing for Dynamic Long-Short Portfolio Allocation](https://icaif2026.org/accepted-papers)：ICAIF2026 poster；官方标题/录用状态，不能据标题编写方法。
- [Making Tail Constraints Bind: Two Silent Failure Modes in CVaR-Constrained Portfolio RL](https://icaif2026.org/accepted-papers)：ICAIF2026接收名单；发现级；尚无可核实全文证据。
- [Micro-Prudence, Macro-Fragility: Crowding and Strategic Response of Risk-Aware Optimal Execution Agents](https://icaif2026.org/accepted-papers)：ICAIF2026接收名单；发现级；不声称已验证拥挤效应大小。

## 2026 之前的基础研究单列

- Online Portfolio Selection with ML Predictions（2025）：NeurIPS2025正式论文；学习增强在线组合的历史基础，不能列为2026成果；交易成本尚非其核心实证。 来源：https://papers.neurips.cc/paper_files/paper/2025/hash/76981cb7a06b5ad53639f5bbe9924388-Abstract-Conference.html
- Almgren–Chriss optimal execution / Black–Litterman / classical online portfolio selection（1990s—2000s）：作为本轮论文明确依赖的历史理论方向单列；本分工未补全原始文献卡，不编造已逐篇查证。
- Sequential e-processes, multiple testing and backtest overfitting（2026之前）：PROMOTE组合了已有统计工具；2026贡献是金融候选登记和晋级流程，不应说它发明了全部理论。

## 交易制度一级来源

- [上海证券交易所交易规则（2026年修订）及发布通知](https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20260424_10816492.shtml)：证券买入后在交收前不得卖出，回转交易品种例外；应用按品种/板块/通道生效规则。（turn103search1）
- [深圳证券交易所交易规则（2026年修订）](https://docs.static.szse.cn/www/lawrules/rule/trade/current/W020260424690713155663.pdf)：交易规则第3.1.4条交收前卖出限制及例外；具体标的回转资格需查相应品种规则。（turn103search11）
- [恒生银行股票通常见问题](https://www.hangseng.com/zh-cn/investment/securities-services/stock-connect/faq-stock-connect/)：该券商/账户FAQ明确未交收A股卖出款不能立即购买另一A股；这是特定服务规则，不能推广成所有沪深港通券商普遍规定。（turn103view0）


## 对本站组合架构的具体建议

以下是本报告根据已核实机制、账户恒等式与实现需求提出的设计判断。它们不冒充任何一篇论文已经证明的 A 股盈利方案。

### 1. 多策略组合必须先分清组合的对象

|组合对象|合法输入/输出|应避免的混淆|
|---|---|---|
|预测集成|同一标的、同一可见时点、同一期限的收益分布或校准分数|把月度看多和一分钟超买直接投票|
|配置集成|统一现金、风险、成本约束下的目标权重/股数|把各自假定独占全部资金的组合直接相加|
|参数增量合成|相同模型骨架、相同参数坐标、兼容训练目标|把任意战法、不同网络和提示词做权重平均|
|专家路由|按当时可见状态选择或组合有资格的策略|按本次测试期收益事后挑冠军|
|做T/网格叠加|在允许的库存偏离与期限内提出目标变化|把网格越界当成必须成交的命令|
|执行路由|已批准母单的拆分、时机、价格方式与取消|由执行器擅自扩大方向敞口|

ReCAP 支持研究阶段知识复用；FineFT 支持专家路由；ICML 决策感知优化支持研究目标组合训练。这三者不是同一层算法，不能混称“所有策略融合”。MACD、均线趋势和动量可能高度相关，应按增量预测/净收益贡献评价，不能把三个同源信号算成三份独立证据。来源见 EP03、EP05、EP06。

建议最小契约为：可追溯事实 → 带期限预测 → 全账户目标组合 → 有限额的叠加调整 → 母单与执行。退出条件是目标组合更新的一种原因；长期仓、短线仓和做T不分别发出互相冲突且无法对账的命令。最终由单一账户协调者决定净交易，并向各策略分摊实际成交、费用与持仓归因。

### 2. 一个物理账户，多个虚拟策略仓

长期底仓、短线仓、正T、倒T和网格共享真实现金与股份。虚拟策略仓是归因和目标管理单位，不是可以各自复制现金的独立账户。一次真实成交只能在账户总账记一次；可按明确规则归因到多个策略，但归因金额必须与真实总账相加一致。

策略提交 `target_qty`、`horizon`、`reason`、`forecast_version`、`data_snapshot`、`expiry` 与允许偏离范围。协调者读取当前库存、已经成交和仍在途的订单，决定净交易。改变挂单方向时先决定撤单/改单，再计算新的可用额；撤单请求发出不意味着资金或库存已经释放。

底仓需要区分“期望持有量”与“不可暂借的最低持有量”。若允许做T借用旧底仓，应明确可借量、最迟回补条件和未回补评价；若某部分底仓不允许动用，就将其从可借额度中排除。不能同时承诺该股份始终保有，又把它作为倒T可卖库存。

### 3. 证券可卖和资金可用是两个维度

对普通 T+1 股票，当天买入不能增加当天可卖量。完成一轮买卖后即使总持仓恢复，可卖旧库存仍已被消耗。正T通常先买入、再卖出原有可卖股份；倒T先卖旧股、后回补。倒T能否用刚卖所得回补，取决于账户交易通道、券商资金复用政策及已有现金，不能单凭股票 T+1 推定。

必须保存 `account_access_channel` 与 `cash_reuse_policy`，区分 `total_qty`、`sellable_qty`、`today_buy_qty`、`reserved_sell_qty`。现金总账要由互斥资产项目组成，再派生 `available_cash`、`withdrawable_cash` 和 `settled_cash` 等视图。真实券商的这些视图可能重叠，绝不能把所有字段简单相加作为总资产。

当前参考模型采用的 `cash_available + cash_unsettled` 只在两个字段定义为互斥桶时成立；国内可复用模式下卖出款直接放入可用桶，不代表现实结算已经完成。完整实现应显式保留结算状态，而不靠变量名推断。

上海证券交易所2026修订交易规则自2026-07-06施行，交易制度需带生效时间。恒生银行股票通 FAQ 的未交收卖出款不可复用，是该服务的明确实例，不应推广为所有沪深港通券商通用规则。ETF 也要按具体品种与通道判定当日回转资格。来源见本册“交易制度一级来源”。

### 4. 做T/网格的经济评价：总权益和匹配持有基线

令主动账户与不实施本次叠加策略的基线拥有同样的初始现金和库存，并经历相同外生现金流、分红及公司行动。在相同终点价格下：

$$
\Delta W_T = (C_T^{\mathrm{active}}-C_T^{\mathrm{hold}})
            + \sum_i (Q_{i,T}^{\mathrm{active}}-Q_{i,T}^{\mathrm{hold}})P_{i,T}.
$$

费用若已从现金扣除，不再重复扣一次。未回补导致的少持仓上涨损失已在库存差中体现，不应再额外减一次“机会成本”。已完成格子的价差、账面平均成本和实际净值增量是不同指标，都可以展示，但只有完整经济账本能够判断叠加策略是否创造了增量价值。

以下仅为人工可核对的算例，暂不计费用；它们不声称市场上能够获得这些价格。

|场景|初始|操作与终价|主动总权益|同条件持有总权益|增量|
|---|---|---|---|---|---|
|完整正T|1000股@10，现金10000|买200@9.8，再卖旧股200@10.1；终价10|20060|20000|+60；扣两腿费用后为净增量|
|倒T只完成卖出|同上|卖200@10.2，未回补；终价11|20840|21000|-160；不能以卖出实现40认定做T成功|
|正T只完成买入|同上|买200@9.8，未卖；终价9.5|19440|19500|-60；新增库存也要按终价计价|

第一例最终仍有1000股，其中800股是当天仍可卖的旧库存、200股为当天新买。若账户没有更多旧库存，当天反复做同样回转会逐步用尽可卖额度。完成格子的胜率不反映未完成库存、额外占用现金和尾部亏损。

应同时报告：全账户净值、相对匹配持有的增量净值、已完成现金价差、剩余库存与持有时间、最大资金占用、成交与未成交费用、滑点、方向暴露。EP14/15 的网格论文只是相关研究线索；上述恒等式本身不依赖论文的收益结论。

### 5. 网格触发与交易决策分离

等差、等比和 ATR 网格分别是事件生成方式。事件到达后，策略可以买、卖、跳过或调整目标，不应强制成交；这与 EP15 的研究方向一致。锚点更新须带时间、原因和版本，防止回测后移网格解释已经发生的走势。

动作掩码由真实账户与交易规则产生。不能由每个网格实例私建现金、借用同一旧仓多次，或忽略已保留订单。仅有 OHLC 的同一根K线同时穿过上下网格时，无法从四个价格确定先后顺序；应使用更细成交数据或报告有界/不确定结果，不能默认总能先低买再高卖。

对照至少含相同初始敞口的持有、无叠加操作、机械网格、简单波动阈值和学习策略。市场趋势、震荡、流动性和执行费用不同，优胜者可能变化。选择稠密网格必须扣除其额外交易成本，不能只统计触发次数。

### 6. 策略池的优胜劣汰应按增量经济价值发生

候选来源可以是传统战法、模型、RL、论文和人工研究，但入池后的表示必须统一：可执行定义、时间可见输入、适用标的、期限、资金/库存需求、费用模型、训练/验证/测试快照与版本。文章或 Agent 的文字解释不是可执行策略。

先比较简单可解释基线，再进行滚动时间外验证；标签跨越训练/测试边界时需要清除重叠，历史成分与宏观修订使用时点版本。所有失败版本留档，不能因为删除失败策略就只展示幸存冠军。成交成本、容量和现有组合相关性决定新增策略的价值；一个单独高 Sharpe、但与现有持仓完全重复且换手更高的策略，可能没有增量贡献。

PROMOTE 的研究登记、出生后证据和下一时段部署边界可以直接吸收；它的有界条件均值零假设、预算和重启规则必须按原条件实现。不能把 e 值说成未来盈利概率，也不应让所有探索研究机械等待同一个保守终身门槛。研究筛选、验证证据与真实资金分配是相邻但不同的决策。来源 EP11。

### 7. 数据共享应让每个模块看到同一个“当时已知世界”

消息、公告、价格、财报和宏观版本共用事实存储与字段语义，最少有 `event_id`、`source_id`、`published_at`、`available_at`、`ingested_at`、`revision_id`、`instrument_links`。价格和因子快照由同一时点服务提供；模型输出单独存储，不覆盖原事实。

新闻更新只触发受影响标的/行业/策略的重新计算，并允许不同期限的更新频率不同。实时查询与大规模研究回测使用独立工作队列和算力配额，复用版本化数据，避免一个策略搜索占住全站实时路径。这是容量与一致性设计，不需要为每个策略造一套消息 API。

模型可更换的边界应以结构化预测、工具能力、延迟、成本和实测质量定义。未来更强模型可以替换研究、解释、预测或路由模块；账户事实、交易规则和验证记录仍应由确定性的契约维护。

