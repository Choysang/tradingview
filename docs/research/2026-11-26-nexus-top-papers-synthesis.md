# 2024–2026 金融与计算机顶会量化论文深度综述及 Pulse 3.5 Nexus 实证转化论证报告

> **研究合规与学术严谨声明（Research Governance）**：本文档为经过独立主源检索交叉核验的文献综述与算子转化论证报告（非冻结预注册变更文件），严格遵守 `AGENTS.md` 研究准则——不修改任何既有冻结预注册文件（`E092`/`E099`/`E100`/`E101` sha256 保持原状），不篡改追加只读实验台账 `docs/research/ledger/experiments.jsonl`。本文严格区分「原论文数学定义」与「Pulse A 股工程适配」，所有实装算子均遵守 **Point-in-Time（PIT）零前视**、**`GIPS-AShare-2026-v1` 统一 T+1 次日开盘成交记账标准**与**模型-评分硬隔离**红线。

---

## 1. 文献筛选原则、2024–2026 A 股制度变更与台账（E001–E101）校准

A 股微观结构与美股存在根本制度差异，所有国际顶会论文引入前均须经以下真实制度与历史台账校验：
1. **2024-08-19 北向资金日度披露停止**：沪深港通不再公布盘中及日度北向净买卖额，持股数改为季度第 5 交易日披露。本系统以「宽基 ETF 份额申赎脉冲 + 两融余额加速度 + 资金流大小单背离 + 龙虎榜沪深股通席位 + 成交额集中度」作为五大高频替代锚。
2. **2025-01 央行 M1 口径修订**：狭义货币 M1 首次纳入个人活期存款与非银行支付机构客户备付金，宏观流动性剪刀差（M1−M2）按新口径跟踪。
3. **2025-04 国泰君安吸收合并海通证券更名「国泰海通」**：证券简称自 2025-04-11 变更，龙虎榜知名席位正则（章盟主、作手新一、消闲派等）已全面兼容更名前后名称。
4. **2026-07-06 沪深主板风险警示股（ST / \*ST）涨跌幅放宽至 ±10%**：自 2026-07-06 起，沪深主板 ST/\*ST 日涨跌幅由 ±5% 调整为 ±10%；创业板（含 ST）与科创板（含 ST）自注册制以来一直保持 ±20%，北交所保持 ±30%。统一记账基准与前向路径守卫均按板别与生效日期精确分段。
5. **本仓库 101 组台账实证铁律**：
   - 日频黑盒模型极易过拟合体制切换（`E010`/`E015`/`E016`）；低维离散门控、下尾稳健评估、正交相关性去冗余与最坏情形混合器在样本外显著更稳健；
   - 追高连板接力与拉萨天团扎堆在扣费后长期负期望（`E048` 拉萨 3+ 次日跳空 `-3.15%`、5日超额 `-2.52%`；`E070` 强板块追高 `-1.92%`；`E094`/`E098` 小盘假机构 `-1.81%`）；
   - 高换手吞噬多头超额，必须用无回测相对秩熵（RRE）与 2x 成本压力测试前置拦截。

---

## 2. 2024–2026 顶会与顶刊权威论文核验及 Pulse 3.5 转化对照

### 2.1 ICLR 2026 — *AlphaSAGE: Structure-Aware Alpha Mining via GFlowNets for Robust Exploration*（`arXiv:2509.25055`）
- **作者**：Binqi Chen, Hongjun Ding, Ning Shen, Jinsheng Huang, Taian Guo, Luchen Liu, Ming Zhang（另参见同届 **ICLR 2026 *AlphaBench*** 跨中美市场 LLM 因子挖掘基准与 **KDD 2025 *AlphaAgent***，Ziyi Tang 等）。
- **原文机制**：以关系图卷积（RGCN）编码公式因子 AST 语法树，通过 GFlowNets 轨迹平衡目标（$\mathcal{L}_{\text{TB}}$）与熵正则（$\mathcal{L}_{\text{ENT}}$），按综合奖励（预测力 $R_{\text{IC}}$、结构/预测新颖度 $R_{\text{NOV}}$、结构对齐 $R_{\text{SA}}$）正比采样多样化公式因子，根治 PPO 模式坍塌。
- **Pulse 3.5 实装**：在 `domain/arena/swiss_glicko.py` 中基于历史真实截面预测序列计算**策略两两经验相关性矩阵 $|\rho_{ij}|$**，对 $|\rho_{ij}| \ge 0.70$ 的高相关策略动态归入同一正交簇，每簇仅保留保守分最高的冠军参与实盘投票。

### 2.2 KDD 2026 — *AlphaEval: A Comprehensive and Efficient Evaluation Framework for Formula Alpha Mining*（`arXiv:2508.13174`）
- **作者**：Hongjun Ding, Binqi Chen, Jinsheng Huang, Taian Guo 等（另参见 **KDD 2026 *AlphaMaster*** 双链反馈因子发现）。
- **原文机制**：构建无回测 5 维快速评估框架，核心包括：
  - **相对秩熵（RRE）**：度量相邻交易日截面排序分布 $S(F_t)$ 的 KL 散度，预判换手率与排序稳定性：
    $$\text{RRE}(F) = \frac{1}{T-1}\sum_{t=2}^{T} \frac{1}{1 + D_{\text{KL}}\big(S(F_t) \parallel S(F_{t-1})\big)}$$
  - **微扰保真度（PFS）**：向**输入市场特征数据**注入微观结构高斯噪声与极端冲击 Student-$t$ 噪声，计算原排序与微扰排序的 Spearman 秩相关系数，识别脆性过拟合。
- **Pulse 3.5 实装**：`domain/benchmark/accounting.py` 中的 `compute_relative_rank_entropy` 与基于真实输入特征高斯/Student-$t$ 扰动的 `verify_strategy_multi_gate`（作为 5 门禁验证的 Gate 1）。

### 2.3 KDD 2026 — *Beyond Black Boxes: An Energy-Based Unified Framework for Interpretable Stock Selection*
- **作者**：Junren Xiao, Ruiyao Miao, Zixuan Yuan, Hui Xiong
- **原文机制**：以能量模型（Energy-Based Model）替代黑盒神经网络做横截面选股，显式分解特征能量与关系能量项，使每只股票的排序得分具备可追踪的因果归因。
- **Pulse 3.5 实装**：`domain/arena/top_papers_2026.py`（`score_kdd2026_ebm_interpretable`），将趋势能量、筹码低波能量、量价一致性能量与 Amihud 流动性摩擦能量显式线性分解为确定性截面打分。

### 2.4 Journal of Finance (Vol. 81, No. 4, Aug 2026) — *The Drivers and Implications of Retail Margin Trading* & 下尾稳健防御盾
- **作者**：Jiangze Bian, Zhi Da, Zhiguo He, Dong Lou, Kelly Shue, Hao Zhou
- **原文机制**：基于中国账户级券商两融与场外配资数据，证明散户高杠杆显著偏好高波动“彩票特征（MAX）”标的；市场下跌触发平仓线时，强制平仓会产生**跨股票流动性冲击与风险传染**。
- **关于 ICML 2025 *AlphaQCM*（Zhoufan Zhu & Ke Zhu）的严谨辨析**：*AlphaQCM: Alpha Discovery in Finance with Distributional Reinforcement Learning* 的核心贡献是在分布式强化学习（IQN+DQN）中利用 **Quantiled Conditional Moment** 无偏估计累积奖励方差作为**探索奖励（exploration bonus）**，以解决因子挖掘的奖励稀疏与非平稳问题，并非直接作为股票截面下尾风险选股因子。
- **Pulse 3.5 实装**：`domain/arena/top_papers_2026.py`（`score_jf2026_margin_lottery_shield`，兼保旧名映射），融合 `JF 2026` 彩票股拥挤踩踏惩罚、Pulse 分位数下尾稳健度（Q20）与 `E004` 特质低波构建抗崩盘防御盾；`AlphaQCM` 归类至分布式 RL 探索范式目录，杜绝概念嫁接。

### 2.5 Management Science (Vol. 72, No. 3, Mar 2026, pp. 2064–2089) — *Nominal Prices, Retail Investor Participation, and Return Momentum*
- **作者**：Jun Du, Dashan Huang, Yu-Jane Liu, Yushui Shi, Avanidhar Subrahmanyam, Huacheng Zhang
- **原文机制**：证明 A 股 100 股整手买入制度使高名义股价（`100 × Close`）构成天然散户资金门槛——高名义价股由机构定价主导，呈现显著正向动量；低名义价股散户参与度高，过度反应导致短期显著反转。
- **Pulse 3.5 实装**：`domain/arena/top_papers_2026.py`（`score_ms2025_lot_momentum`），按截面一手成本分位数动态切换高价动量与低价反转算子。

### 2.6 AAAI 2026 — *MetaTrader: Learning to Generalize RL Trading Policies Beyond Offline Data*（`arXiv:2505.12759`）& NeurIPS 2024 *FinCon*
- **作者**：Haochen Yuan, Minting Pan, Yunbo Wang, Siyu Gao, Xiaokang Yang（上海交通大学 & 中金公司）；Yangyang Yu 等（NeurIPS 2024 *FinCon*）。
- **原文机制**：在波动率拉伸、趋势反转与流动性/下采样等多变换域上执行双层保守 TD 集成更新 $\hat{Q}_{\text{ens}}(s_t, a_t) = \min_n [r_n + \gamma Q_\phi(s'_n, a'_n)]$，防止离线 RL 策略在分布外牛熊切换中崩溃。
- **Pulse 3.5 实装**：`domain/benchmark/accounting.py`（`metatrader_worst_case_cvar`）作为 Pulse 工程改编的多扰动域下尾 CVaR 风控门控，与 `E036` 20 日净值均线 0.3x 降仓闸门联动。

### 2.7 AAAI 2026 *ArchetypeTrader* & ACM ICAIF 2026 *Crumble-Aware / Neurosymbolic RL* & Avellaneda-Stoikov (2008)
- **作者**：Chuqiao Zong, Molei Qin, Haochong Xia, Bo An（南洋理工大学，AAAI 2026）；Haohan Xu, Pawel Polak, Gary Kazantsev 等（Bloomberg & Stony Brook，ACM ICAIF 2026）；Zeynep Yıldız 等（ACM ICAIF 2026）。
- **原文机制与严谨归因**：
  - *ArchetypeTrader* 通过动态规划提取专家轨迹 + VQ 编解码器蒸馏可复用离散战略原型，并由上下文策略选择微调；
  - *Crumble-Aware RL* 引入盘口流动性溃散感知惩罚，缩小回测与实盘成交落差；
  - *Neurosymbolic RL* 将投资纪律与风控红线内化为可执行符号约束层；
  - 做市库存偏斜保留价 $R(s,q,t) = s - q\gamma\sigma^2(T-t)$ 源自经典 **Avellaneda & Stoikov (2008)** 做市理论。
- **Pulse 3.5 实装**：`domain/execution/grid_and_t.py`，将离散做T执行原型、Avellaneda-Stoikov 保留价、Crumble-Aware 流动性溃散评估与 Neurosymbolic 硬约束（严格 A 股 T+1 老底仓、现金不透支锁、零前视滚动回测、高价股整手预算守卫）合为一体。

### 2.8 IJCAI 2026 *PRISM-VQ* & AAAI 2025 *AlphaForge* & arXiv 2026 *Alpha-R1*
- **作者**：Namhyoung Kim, Jae Wook Song（IJCAI 2026）；Hao Shi 等（AAAI 2025）；Zuoyou Jiang 等（arXiv 2025–2026）。
- **原文机制与工程适配**：PRISM-VQ 在主实验中使用 $K=512$ 对比正则化向量量化（VQ）离散原型与结构条件 MoE 结合金融先验进行截面排序（实测于沪深 300 / S&P 500）；AlphaForge 通过动态权重更新组合因子；Alpha-R1 使用推理模型基于市场状态做稀疏因子语义门控（失效体制因子归零）。
- **Pulse 3.5 实装**：`domain/arena/top_papers_2026.py`（`score_ijcai2026_prism_vq_moe`），适配 Pulse 既有 4 维实盘市场体制（主升/轮动/震荡/防御），按离散体制门控平滑分配低换手专家权重，服务中长线（20~60 日）赛道。

### 2.9 NeurIPS 2025 — *Online Portfolio Selection with ML Predictions (RAM)*
- **作者**：Ziliang Zhang, Tianming Zhao, Albert Zomaya
- **原文机制**：提出学习增强在线组合算法 **RAM（Rebalanced Arithmetic Mean with Predictions）**——当多策略/ML 预测准确时逼近事后最优定常再平衡组合（BCRP）；**当预测完全错误甚至对抗性失效时，依然严格保底超越资产收益几何均值**。
- **Pulse 3.5 实装**：`domain/arena/top_papers_2026.py`（`compute_ram_portfolio_weights`）与 `swiss_glicko.py`，为瑞士轮簇冠军的实盘合成权重加装 RAM 最坏情形保底混权，确保单一范式阶段失效不致崩盘。

### 2.10 NeurIPS 2025 *RAPTOR (Black-Litterman)* & ACL 2026 *News Sentiment under Liquidity Frictions*
- **作者**：NeurIPS 2025 RAPTOR 作者组；ACL 2026 流动性摩擦新闻情绪评估作者组。
- **原文机制**：RAPTOR 将多源定性/多智能体辩论观点通过 **Black-Litterman 贝叶斯逆优化**严谨映射为有界组合倾斜，避免 LLM 直接输出任意权重；ACL 2026 证明新闻情绪信号必须经过**买卖价差、费率与 Amihud 非流动性过滤**，仅在高流动性可控摩擦区间具备真实可交易超额。
- **Pulse 3.5 实装**：`domain/intelligence/realtime_adjuster.py` + `domain/arena/top_papers_2026.py`（`compute_amihud_illiquidity`, `compute_raptor_bl_tilt`），把实时跨源核验新闻（新华社/巨潮/财联社/东财/同花顺）及受控 `gpt-6-luna` 观点严格经由 Amihud 流动性门禁与有界 `±150 bps` Black-Litterman 倾斜反哺选股排名与个股风控止损。

### 2.11 NeurIPS 2025 *R&D-Agent-Quant* & arXiv 2026 *AlphaPROBE*
- **作者**：Yuante Li, Xu Yang, Xiao Yang 等（Microsoft Research，NeurIPS 2025）；Taian Guo, Binqi Chen, Ming Zhang 等（北京大学，arXiv 2026）。
- **原文机制**：R&D-Agent-Quant 验证“假设生成 → 算子实现 → 统一基准回测淘汰”协同演进闭环；AlphaPROBE 将因子演化建模为有向无环图（DAG），采用深度衰减先验 $(1-\gamma)^{\text{depth}(F)}$ 的贝叶斯检索器避免对单一母因子过度微调过拟合。
- **Pulse 3.5 实装**：`discover_alphaprobe_candidates` + `workers/nexus.py discover-strategies` + `research.strategy_discovery_candidate`，自动化生成、5 门禁检验、去冗余筛选并送入瑞士轮优胜劣汰。

### 2.12 板块超图、散户异质性、隔夜拔河与情绪传染
- **AAAI 2024 *CI-STHPAN / MDGNN / StockMixer***（Hong-Jie Xia 等；Hao Qian 等；Jinyong Fan & Yanyan Shen）：PIT 行业超边龙头向低位大容量中军溢出（`score_aaai2024_sector_hypergraph`）；注：**ACL 2025 FinNLP Workshop *LED-GNN*** 原文任务为基于新闻关系图的**成交量预测**，作为量能辅助线索参考，不混淆为板块动量溢出。
- **JFQA (Vol. 60, 2025, pp. 68–104)** — *Retail Trading and Return Predictability in China*（Charles M. Jones, Donghui Shi, Xiaoyan Zhang, Xinran Zhang）：基于中国交易所账户资金规模分层证明散户与机构资金流异质预测力。
- **JFE (2019)** — *A tug of war: Overnight versus intraday expected returns*（Dong Lou, Christopher Polk, Spyros Skouras）：原实证为美股隔夜与日内客户群拔河；Pulse 结合 A 股 T+1 制度与台账 `E004` 实证（隔夜减日内收益在 A 股显著正向预测截面收益）实装于 `score_jfqa2025_micro_tug_of_war`（含实装的 20 日量价背离项）。
- **PBFJ (Vol. 88, 2024, 102520)** — *Social media sentiment contagion and stock price jumps and crashes*（Jing Yang & Yan Xiong）：基于股吧与流行病学 SIR 传染率模型刻画情绪崩盘风险，与龙虎榜席位画像联合驱动 `guba_four_session.py`。

---

## 3. 学术回测陷阱防线（KDD 2026 *FINSABER* 对照守则）

**KDD 2026** 论文 *Can LLM-based Financial Investing Strategies Outperform the Market in Long Run?*（FINSABER，Weixian Waylon Li, Hyeonjun Kim, Mihai Cucuringu, Tiejun Ma，`arXiv:2505.07078`）系统审计了多篇声称高夏普的 LLM 选股工作，证实绝大多数表观超额源于**牛市短窗挑选、幸存者偏差、预训练语料跨期泄漏与忽略真实交易摩擦**。据此，Pulse 3.5 Nexus 在架构层面强制执行五条不可绕过的工程红线：
1. **模型与数值打分硬隔离**：LLM（严格受 `PULSE_LLM_ALLOWED_MODELS` 锁定仅 `gpt-6-luna`，且须开关文件 + 每日配额上限 + 密钥文件全部到位）仅生成带原文证据引用的多空分析摘要；**全部排名分、买卖区间、止损价、策略晋升与权重 100% 由确定性零前视代码计算**；
2. **统一 `GIPS-AShare-2026-v1` 国际记账与多门禁验证**：全策略在唯一公共引擎上跑真实历史 PIT 面板，同时报告 1x/2x 成本压力、一字板/封板队列 3 档敏感性（0%/30%/100%）、三段收益拆解（日内/隔夜/后续）、以及前后半段体制稳定性；
3. **真实 A 股涨跌停与退市处理**：精确纳入 **2026-07-06 沪深主板 ST/\*ST 涨跌幅由 ±5% 调整为 ±10%** 与双创 ±20%、北交所 ±30% 制度，并剔除收盘价 `< 2.0` 元退市风险股；
4. **Newey-West 重叠窗口自相关修正**：持有 $h$ 日的重叠信号严格采用滞后 $\ge h-1$ 的 Newey-West / 移动块 $t$ 统计量，拒绝夸大显著性；
5. **瑞士轮真回测优胜劣汰 + RAM 保底**：策略晋升淘汰 100% 依托真实历史回测表现与精确 Glicko-2 Illinois 迭代更新，实盘合成严格执行全体投票者权重归一（未入选记 0）+ NeurIPS 2025 RAM 最坏情形保底护栏。
