# 2026计算机研究增补：可迁移机制与验证方案

本增补纳入8篇正式会议论文，读取了官方会议页以及全文中的指定方法、假设或实验设定页。检索截止2026-11-10；未运行作者实验，未验证A股盈利。每条将论文结论与本项目迁移设计分开。

## CS01 · LOAM / LOAM-Race

**论文**：Designing Observation and Action Models for Efficient Reinforcement Learning with LLMs。ICML 2026。[CS01](https://proceedings.mlr.press/v306/yoon26b.html)。

**精读范围**：全文第1,2,3,4,5页；全文共34页。

**原始机制与实验**：由大模型设计观测和动作接口；以学习曲线外推及不确定性分配训练预算，减少对所有候选完整训练的开销。原始实验是 HumanoidBench、Isaac Lab 连续控制；论文采用 FastTD3，并比较观测/动作设计及计算预算。不是股票收益实验。

**迁移判断**：为离线交易环境生成可解释的候选观测组合，动作必须经过固定交易规则映射；采用均匀预算、successive-halving和学习曲线外推三组对照。对训练慢而后期更好的策略保留探索份额。

**适用条件与边界**：观察空间、动作空间、训练预算和选择目标明确；学习曲线外推可能偏好早熟候选。 论文记录训练过程中的评估 return 历史，最终按 max_k max(R_k) 选择候选；金融应用仍必须另做未参与选择的时间保留集验证。

**本项目实验**：同一PIT数据、初始资产、时间切分、候选数量、环境步数和模型调用预算。 比较找到合格候选的总成本、候选多样性及未接触区间成本后增量；不能只比较训练最大收益。 大模型不能生成交易所规则、补造行情或改变账本。

**对照**：uniform_budget, successive_halving, fixed_feature_action_baseline。

**当前状态**：方案已指定；实现未开始；作者实验未复现；A股收益未验证。

## CS02 · PATH

**论文**：Active Curriculum Refinement for Reinforcement Learning。ICML 2026。[CS02](https://proceedings.mlr.press/v306/liu26j.html)。

**精读范围**：全文第3,4,5页；全文共23页。

**原始机制与实验**：把环境先修关系表示成课程DAG，先沿多条随机路径扩大覆盖，再用学习潜力信号集中训练未掌握区域。原始任务包括 MiniGrid、BipedalWalker，正文使用 PPO；报告训练效率、泛化、均值/IQM和完成率，非证券交易。

**迁移判断**：构造交易机制训练课程：价格观测→手续费→资金库存→部分成交→T+1→涨跌停和停牌→跨日计划失效。把已经通过的机制场景作为先修节点，市场状态仍来自真实历史样本。

**适用条件与边界**：先修结构和掌握阈值需要由领域定义，不保证适用于任何市场状态。 简单到复杂的课程不能让测试期收益或未来市场状态进入训练集。

**本项目实验**：相同训练更新数下比较随机训练、固定课程、PATH式课程。 验收实际订单合法率、持仓与现金一致性、未见机制组合下的净收益和失败率。 课程只是训练安排，不能通过放宽实盘交易规则制造高分。

**对照**：uniform_scenario_sampling, fixed_curriculum, replay_prioritization。

**当前状态**：方案已指定；实现未开始；作者实验未复现；A股收益未验证。

## CS03 · DAL

**论文**：DAL: A Practical Prior-Free Black-Box Framework for Piecewise Stationary Bandits。ICML 2026。[CS03](https://proceedings.mlr.press/v306/gerogiannis26a.html)。

**精读范围**：全文第1,2,3,4页；全文共28页。

**原始机制与实验**：将平稳 bandit 算法、变化检测和强制探索组成框架；检测到变化后重启当前 bandit 学习器并清空所有检测历史。本项目设计另要求：平台已登记的历史实验成绩不随之改写。覆盖多种参数化/核/上下文bandit，比较合成分段变化与漂移情景下动态遗憾和运行时间。

**迁移判断**：在影子组合和已成熟收益标签上实验变化检测，将已验证策略路由权重随状态变化更新；把变化证据记为新路由版本，不修改策略历史成绩。

**适用条件与边界**：理论的噪声、变化间距与覆盖集合假设不自动适用于有持仓依赖、延迟回报的股票策略。 原始框架要求探索；真实账户不可为了算法探索而被迫执行差策略。

**本项目实验**：以固定权重、滑窗权重、折扣权重为对照，统一切换成本与延迟奖励。 分别检验误报警率、检测延迟、切换次数和成本后增量。 先在研究/影子环境探索，正式计划仅采用已经准入的动作。

**对照**：fixed_router, sliding_window_router, discounted_router。

**当前状态**：方案已指定；实现未开始；作者实验未复现；A股收益未验证。

## CS04 · GLBM-SCB

**论文**：Generalized Linear Bandits with Memory。ICML 2026。[CS04](https://proceedings.mlr.press/v306/ann26a.html)。

**精读范围**：全文第2,3页；全文共28页。

**原始机制与实验**：用过去动作构成记忆矩阵，研究动作对未来奖励的影响；把一个反馈区间内的动作当作整体，构造分块估计与置信界。理论与数值实验分析线性及广义线性bandit中的记忆、非线性反馈与延迟更新。

**迁移判断**：把库存、最近交易、费用累计、剩余可卖额度加入策略路由上下文，检验无记忆路由与有限记忆路由差异；暂不以该论文理论为A股盈利保证。

**适用条件与边界**：模型的记忆矩阵和连接函数是具体数学假设，不能直接等同真实库存冲击。 收益与操作历史相关时，简单把每个策略当独立臂可能错误。

**本项目实验**：构造相同价格但不同库存/执行历史的配对场景。 将动作影响和市场外生变化分开消融，检查动作反馈何时真实可见。 评价联合账户权益，不相加互相占用同一资金的独立回测结果。

**对照**：memoryless_router, fixed_history_features, shared_account_policy。

**当前状态**：方案已指定；实现未开始；作者实验未复现；A股收益未验证。

## CS05 · Nonstationary Linear BAI

**论文**：On the Complexity of Best-Arm Identification in Non-Stationary Linear Bandits。COLT 2026。[CS05](https://proceedings.mlr.press/v336/maynard-zhang26a.html)。

**精读范围**：全文第1,2,3,4页；全文共32页。

**原始机制与实验**：区分累计遗憾最小化与固定预算下识别最优臂；分析非平稳线性奖励中由候选几何相邻关系决定的识别难度。主问题为固定预算、有限线性动作集、独立零均值 sub-Gaussian 噪声；目标是在给定参数序列下识别累计期望线性奖励最优的臂，研究错误概率，而非累计噪声实现值最优的臂。

**迁移判断**：为策略池分别设“研究候选识别”和“已准入策略在线分配”两个任务；瑞士轮只安排可比候选的实验顺序，晋级仍依赖统一执行、独立验证和组合增量。

**适用条件与边界**：论文的相邻指动作凸包上相邻顶点，不能说瑞士轮积分接近就是满足其理论。 固定预算挑冠军与在线分配资本是不同优化目标，不能共用一个评价分数。

**本项目实验**：同预算比较全量轮询、瑞士轮式调度和模型化候选筛选。 统计选错冠军率、计算耗时和最终盲测表现，不用积分替代经济价值。 所有配对保留共同样本、种子、费用、成交分辨率和候选家族身份。

**对照**：round_robin_equal_budget, swiss_style_equal_context, fixed_budget_selection。

**当前状态**：方案已指定；实现未开始；作者实验未复现；A股收益未验证。

## CS06 · Robust-CPD-UCB

**论文**：Catoni-Style Change Point Detection for Regret Minimization in Piecewise-Stationary Heavy-Tailed Bandits。AISTATS 2026。[CS06](https://proceedings.mlr.press/v300/genalti26a.html)。

**精读范围**：全文第2,3,4页；全文共25页。

**原始机制与实验**：用适合重尾分布的Catoni式估计与置信序列进行变化检测，再结合平稳阶段的bandit学习处理突变。理论讨论有界(1+epsilon)阶中心绝对矩、分段平稳环境、检测延迟与误报的关系；并有数值实验。

**迁移判断**：对成熟的成本后相对收益、命中率校准误差和成交偏差分别做稳健漂移研究；数据中断先由数据质量层处理，不由策略退役机制背锅。

**适用条件与边界**：重尾矩界、变化幅度和有效样本要求需要验证；股票标签重叠与相关性不能忽略。 极端行情造成的大波动不等于策略失效，需区分市场状态变化和数据错误。

**本项目实验**：以普通均值CUSUM、滑窗检验、稳健检测做对照。 报告误报/漏报、恢复时长、降权损失和被避免的损失，按市场状态分组。 使用固定训练阈值及独立状态区间，检测后产生可审计的暂停/重评原因。

**对照**：rolling_mean_detector, cusum_detector, robust_change_detector。

**当前状态**：方案已指定；实现未开始；作者实验未复现；A股收益未验证。

独立冷审另补读 CS06 的第5–6页：完整 Robust-CPD-UCB 还包含循环均匀探索与全重置。本站先研究 Catoni-FCS-detector 的稳健漂移检测价值；这不等于复现完整 bandit 分配算法。原始卡片读取页码与本轮补读范围分别记录。

## CS07 · GC-TTT

**论文**：Test-time Offline Reinforcement Learning on Goal-related Experience。ICML 2026。[CS07](https://proceedings.mlr.press/v306/bagatella26a.html)。

**精读范围**：全文第2,3,4页；全文共19页。

**原始机制与实验**：在测试时从既有离线经验中选择与当前状态及目标相关的子轨迹，用少量梯度步局部调整策略，并以滚动方式重复。原始研究是目标条件离线强化学习，使用导航/控制/操作类基准及既有离线RL骨干；比较测试计算与扩大模型。

**迁移判断**：仅在研究环境尝试按市场状态检索历史片段的局部策略适配，记录每次适配消耗、训练数据manifest和参数版本；正式计划仍经统一编译及账户约束。

**适用条件与边界**：本文并不要求使用未来测试轨迹标签；金融迁移也必须冻结可用历史集和更新规则。 市场目标及状态距离不像机器人目标距离那样自然，局部相似不代表因果相同。

**本项目实验**：冻结模型、相同FLOPs大模型、历史检索但不微调、局部微调四组比较。 更新可用数据严格available_at<=decision_as_of，禁止吸收未来持有期结果。 将训练/推理延迟计入可行动时间，比较净增量而非只看离线预测。

**对照**：frozen_policy, larger_equal_compute_policy, retrieval_only_policy。

**当前状态**：方案已指定；实现未开始；作者实验未复现；A股收益未验证。

## CS08 · Retriever Portfolios

**论文**：Retriever Portfolios: A Principled Approach to Adaptive RAG。ICML 2026。[CS08](https://proceedings.mlr.press/v306/stouras26a.html)。

**精读范围**：全文第2,3,4页；全文共21页。

**原始机制与实验**：离线选择一小组互补检索器，以查询集合上best-of-k覆盖为目标；在线路由到少量检索器并组合回答。原始验证为HotpotQA、2WikiMultihopQA、TriviaQA、MuSiQue等问答检索；不是金融资产配置。

**迁移判断**：按公告、政策、海外影响、公司事实和社区主张分类检索，学习小型互补检索组合；把事实原始来源数、证据支持率、延迟、调用成本作为验收指标。

**适用条件与边界**：best-of-k目标有已知分数和抽样假设；线上路由未必知道哪个检索器最好。 多检索器找到同一篇转载不能增加独立证据数；理论覆盖率不等于事实正确率。

**本项目实验**：单检索器、全部并行、固定分组和学习路由按相同时间/成本比较。 使用有发布日期、实体歧义和转载链的A股验证题。 只把检索结果当证据入口，不能由回答模型投票创造事实共识。

**对照**：single_retriever, all_retrievers, fixed_category_router。

**当前状态**：方案已指定；实现未开始；作者实验未复现；A股收益未验证。

## 增补检索的排除与去重

NeurIPS 2026官方目录出现 Learning-Augmented Online Portfolio Selection: Optimal Robustness-Consistency Tradeoffs；当前只核验题名，未取得对应全文，记为发现候选，未编造方法卡。检索同时命中 Online Portfolio Selection with ML Predictions，但官方页面属于NeurIPS 2025，不归入2026新增核心集。TiMi等已在原核心集中的论文不重复计数。

这里的bandit与瑞士轮式实验调度属于不同层面：论文用于启发预算分配、漂移和选择目标；瑞士轮只是本站用于管理候选比较的调度设计。检索portfolio指检索器组合，不得据题名误记为股票投资组合算法。
