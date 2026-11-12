# 现有代码审计与已完成修复

## 1 审计结论与依据

现有工程已经积累了战法纯函数、前后端契约、时间切分、部分无前视测试、新闻原始响应与版本记录、库存和排队原型、冻结前向主书及实验档案。最需要改进的部分，是把这些能力接成同一条可追溯的产品链：**原始证据 → 影响分析 → 排名快照 → 交易计划 → 成交与持仓 → 实验反馈**。目前不同入口仍使用不同的策略、价格、费用和记录语义，页面的表现不能直接由某一个实验账本的收益来证明。

建议按 GREENFIELD_SPEC.md 全新实现业务代码，并迁移旧数据和实验资产。以下18项均有可定位的代码依据；其中3项完成窄修复，其余15项纳入重写。P0表示优先建立正确的数据、结果或闭环含义，P1表示需要在相应功能交付前落实，并不意味着增加逐次人工审批。

**审计基线：** Git commit `6f5c57f8df73c58f0340dab6977b95c05d21740c`，Git提交日期2026-11-10，提交说明“sync: 修复实时行情卡死，个股K线改用本地真实日线”。代码来源：[Choysang/tradingview](https://github.com/Choysang/tradingview/tree/6f5c57f8df73c58f0340dab6977b95c05d21740c)。C01–C18中的行号指该基线，路径相对仓库根目录；补丁验证部分另标当前工作区行号。Git提交日期是仓库元数据，审计以提交哈希确定对象。

**审阅范围：** 已读 AGENTS.md 与 docs/ARCHITECTURE.md；按 AGENTS.md 前言将旧架构草案视为历史记录。检查推荐、研究与冻结前向账本、交易与费用、时点数据、行情标准化、新闻录制和告警、日线同步与个股图表。结论区分实现缺陷、架构缺口及已披露建模限制。未声称全仓库每一行均已审计，也未执行生产交易、付费模型调用或对外通知。

| 编号 | 主要问题 | 优先级 | 本次状态 |
| --- | --- | --- | --- |
| C01 | 网页推荐与冻结主书不是同一策略 | P0 | 待按新协议统一并保留独立归因 |
| C02 | 推荐计划与“实盘记录”收益语义不同 | P0 | 待重写 |
| C03 | 推荐入口未统一限制沪深主板 | P1 | 已窄修复；保留旧缓存入口限制 |
| C04 | 同日推荐可覆盖已发布历史 | P1 | 待重写 |
| C05 | 最新证券属性回填历史特征 | P0 | 待时点主数据迁移与补建 |
| C06 | LIVE字段存在未标明估算和默认状态 | P0 | 待重写 |
| C07 | 主板ST后备涨停规则硬编码 | P1 | 待统一规则版本 |
| C08 | 新闻任务并非全天连续采集 | P1 | 待重写 |
| C09 | 分类异常后消息被永久标为完成 | P0 | 已窄修复；模型失败状态另列限制 |
| C10 | 慢模型阻塞同批规则消息 | P1 | 待拆规则与模型任务 |
| C11 | 消息告警未进入推荐和计划更新 | P0 | 待完成产品闭环 |
| C12 | 多个回测入口执行规则不同 | P1 | 待统一执行协议 |
| C13 | 回测期末清仓被用于前向估值 | P1 | 待分开持仓估值与实际成交 |
| C14 | 复权价兼任按股成交和现金记账 | P1 | 待原始价与公司行动账本 |
| C15 | 成本配置与绩效描述多套口径 | P1 | 待版本化费用协议 |
| C16 | 15:05即把当天日线视为完成 | P1 | 已延后到15:30并测试 |
| C17 | 同日新增数据无法使历史缓存失效 | P1 | 待数据版本缓存 |
| C18 | 个股仅日K，缺多周期及详情自动更新 | P1 | 待扩展；现有指标与量能应承认 |

## 2 逐项证据、影响与验收方法

## C01 网页推荐与冻结主账本执行不同的策略

- 类型与优先级：架构缺口，P0。
- 代码证据：`backend/app/playbooks/picks.py:443-458` 从 catalog 战法的轮换验证/fitness 获取权重，再 ensemble_day 排名；`scripts/lab/forward_book.py:28-45` 固定读取 fusion_static40_safe，持有5日、20仓、每日4只、top40和20日净值门控0.25。`backend/app/api/picks.py:10-17` 网站只接前者。
- 具体影响：研究主书的效果不能作为网页推荐的效果；网站排名并非冻结实验的建议清单。两个研究产品本身都可以合理，但缺少显式 policy_id 与单一 DecisionSnapshot，使用户很容易理解成同一策略。
- 改法：统一策略注册与决策快照接口，允许 frozen_baseline 与 adaptive_policy 同时存在、各自独立归因。网站明确显示当前策略版本、信号时间、计划和绩效。不要改写冻结主书去迎合新网站。
- 核实方法：在同一输入快照上分别调用两条路径，比较候选、持有期和门控；统一后同一 policy_id 在 API、回放、前向任务中应得到相同决策哈希。

## C02 推荐卡的买卖计划与所谓实盘记录不是同一口径

- 类型与优先级：产品语义/评测缺陷，P0。
- 代码证据：`backend/app/playbooks/picks.py:347-362` 每只股票携带 lead 战法优化后的 entry、ExitRules、holding_days；但 `backend/app/playbooks/picks.py:648-659` 只传(code,horizon)，用全局 HOLD(short=3,swing=10,long=40，见66行)；`backend/app/playbooks/picks.py:701-709` 存档评测丢弃实际买卖计划。`backend/app/playbooks/picks.py:618-635` 只做次日开盘→固定期末收盘收益。前端 `frontend/src/features/picks/PicksView.tsx:231,255` 标签为“实盘记录”。
- 具体影响：展示持有4日的股票，在波段战绩中可能按10日计算；回踩未成交、止损止盈、分批卖出、跌停卖不出都没有按推荐计划执行。该结果是推荐后固定窗口收益，不能表述为计划执行业绩或券商真实成交。
- 改法：保留并命名 signal_forward_return；新增以不可变 TradePlan/OrderIntent/Fill 为输入的 plan_execution_record；实盘仅指真实执行回报，纸面成交单独命名。
- 核实方法：构造一个持有4日、波段标签、第二日止损的推荐，核对计划收益与固定10日收益分开输出。

## C03 网站推荐未统一过滤沪深主板

- 类型与优先级：实现缺陷，P1，可窄修复。
- 代码证据：`backend/app/playbooks/picks.py:167-175` _eligible 只过滤ST、上市根数、成交额；`backend/app/playbooks/evaluate.py:76-109` context 读取完整 research_panel；例如 `backend/app/playbooks/library.py:113-120` 首板策略本身没有板块限制。与 `scripts/lab/harness.py:45-48,119` 的 LAB_UNIVERSE=main 路径不同。
- 具体影响：仓库包含20cm/30cm股票时，它们可进入网站推荐；这与用户仅沪深主板的核心条件不一致。
- 改法：在推荐共同入口使用证券主数据的 board=='10cm' 筛选，缺失板块不猜；保持名字为A/B的测试数据有效，通过显式board字段表达身份，不靠硬编码测试代码。此项已完成有限补丁，见文末验证记录。长期改用 instrument_type、exchange、board 的完整主数据。
- 核实方法：同分输入主板、创业板、科创板、北交所、ST、新股，确认只有合格主板推荐。回放同样受入口约束。

## C04 已发布推荐会被同日重算覆盖

- 类型与优先级：实现/记录缺口，P1。
- 代码证据：`backend/app/playbooks/picks.py:540-555` force 或数据版本改变时重建同一 trade_date；`backend/app/warehouse/store.py:163-170` write_doc 用 os.replace 覆盖 `<date>.json`；`backend/app/playbooks/picks.py:701-709` 战绩只读取当前文件内容。
- 具体影响：当日早先实际看过的推荐、生成时间与规则可丢失；后续再计算的推荐可能被当作原始发布记录。不是每次必然发生，但实现明确允许。
- 改法：decision_id/revision_id 追加式存储，latest作为投影；保留 published_at、effective_at、input_snapshot_id、policy_version、supersedes。历史绩效引用实际已发布的具体版本。
- 核实方法：同日生成两次不同推荐，两个版本都应可读取，已发布版本绩效不能随latest移动。

## C05 部分历史特征仍用今天的证券主数据

- 类型与优先级：已披露但未解决的时间点数据限制，P0。
- 代码证据：`backend/app/playbooks/panel.py:16-18,166-201` 把最新 securities 按code连到所有历史，cap_est=今天总市值×历史价/最新价；`backend/app/playbooks/crowd.py:82-94` 当前行业成分回连历史，114-120用当前名称建立字辈簇。`backend/app/warehouse/store.py:34-44,135-144` securities唯一键仅code，boards无valid_from/to，扫描只保留最新ingest，没有as_of接口。
- 具体影响：股本变化、行业变更、改名题材、历史上市/退市成分不能精确还原。截断行情测试不会发现“从今天静态表带入过去”的泄漏。README所称“行业成分、市值都按当时的数据”不应覆盖这些近似字段。
- 改法：证券、股本、行业、概念、名称、ST、退市均有有效时间和知识时间；扫描支持known_at。无历史来源时明确字段为proxy并在实验卡暴露，不称精确PIT。
- 核实方法：向主数据加入未来改名/增发/行业调整，固定过去as_of重新查询，历史特征应完全不变。

## C06 LIVE 行情中存在未标识的估算与默认状态

- 类型与优先级：实现缺陷，P0。
- 代码证据：`backend/app/market/live.py:320-342` 将自由流通市值写成流通市值×0.62，将 is_new=False、is_suspended=False，theme_tags固定“A股全市场”。
- 具体影响：用户强调的自由流通盘筛选可把估算当事实；新股/停牌过滤在来源缺失时失效。与“缺失即null、从不估算”的总体说明冲突。
- 改法：真实值、估算值分字段，free_float_cap_source/method可追溯；无数据为unknown；证券主数据补上市日与停复牌状态；筛选明确允许/禁止proxy，不把不同口径混成一列。
- 核实方法：真实快照缺自由流通/上市/停牌字段时，输出unknown而非0.62系数或False；已有真实字段应原样保留。

## C07 实时后备涨停规则仍硬编码主板 ST 5%

- 类型与优先级：规则实现不一致，P1。
- 代码证据：`backend/app/market/simulation.py:25-30` price_limit对主板ST固定5%；`backend/app/market/live.py:324-326,424-428` LIVE标准化复用该函数。腾讯真实涨停价可在 `backend/app/market/live.py:180-207` 覆盖，因此不能声称每条LIVE都错误。另一方面 `backend/app/playbooks/library_factor.py:38` 已记录2026-07-06规则切换日期，表明仓内不同路径已不同步。
- 改法：证券+日期驱动的TradingRules，优先采用交易所发布价；SIM按其模拟日期调用同一规则。规则生效日期及适用范围参见 GREENFIELD_SPEC.md 第3、7节与来源目录中的交易所原文；规则本身也应版本化。
- 核实方法：同一ST标的跨生效日前后、不提供腾讯覆盖时验证涨停价；普通主板/双创/BSE独立校验。

## C08 新闻源叫 7×24 但采集与告警不是全天连续运行

- 类型与优先级：需求缺口，P1。
- 代码证据：`backend/app/warehouse/news.py:54` STOP_AT=22:00，注释任务08:00启动；`backend/app/warehouse/news.py:374-395` 非交易日返回、到时退出；`backend/app/warehouse/news_alerts.py:55-56,696-724` 每30秒判读，非交易日跳过、22:30退出。
- 具体影响：夜间美股、FOMC及周末政策消息无法保证在发生时录到；下次首屏只返回源最近一页，不能保证补齐整段缺口。不能因源名称含724就说系统已7×24。
- 改法：新闻采集使用独立24小时日历与按日期滚动文件；交易时钟只限制下单/策略应用。跨夜和非交易日持续收集，盘前生成变化摘要。
- 核实方法：模拟周六和23:00/跨零点输入，源轮询、归档、事件分析持续运行；交易计划有效期按交易日历等待。

## C09 新闻分类异常被永久标成已处理

- 类型与优先级：实现缺陷，P0，可窄修复。
- 代码证据：`backend/app/warehouse/news_alerts.py:568-574` 先seen.add再safe_judge，再把整个fresh写入alerts_seen；`backend/app/warehouse/news_alerts.py:603-608` 异常返回[]，与成功但中性的[]无区别；读取时 `backend/app/warehouse/news_alerts.py:295-301` 的 raw_messages 原地前移游标，`backend/app/warehouse/news_alerts.py:334` 的 parquet_messages 标part已读。
- 具体影响：一次临时分类错误即永久跳过该消息版本；只移动seen.add还不够，因为输入游标已前移。
- 改法：成功与失败返回值分开；成功项才记录seen；失败时回退这批输入游标，成功项的seen仍保留，下次/重启重读只重试失败项。未来统一inbox状态可显式pending/done/failed。意外分类异常的有限补丁已完成；模型正常返回失败状态的情况仍未覆盖，见文末。
- 核实方法：首条抛一次、第二条成功，验证首条下一轮与重启均能重试、第二条无重复；parquet也覆盖；中性[]应只处理一次。

## C10 规则判读与大模型串行共用一个批处理阻塞链

- 类型与优先级：吞吐架构缺口，P1。
- 代码证据：`backend/app/warehouse/news_alerts.py:572` 对整个fresh串行safe_judge；`backend/app/warehouse/news_alerts.py:450-475` 同步httpx.post，timeout30秒；`backend/app/warehouse/news_alerts.py:573` 要等整批完成才一次写告警。
- 具体影响：批内前面的LLM慢消息会延迟后面的确定性P0规则告警；轮询每30秒不是最大告警延迟。数十条消息需LLM时，积压明显。
- 改法：规则快路立即落库发事件，LLM由独立受限并发任务处理，输出作为新的事件修订。不要在模型调用返回前阻塞规则与UI刷新。
- 核实方法：mock一个慢LLM消息后跟规则P0，规则事件应在模型完成前可见；测真实capture→rule→plan时延而非轮询间隔。

## C11 消息处理结束于告警日志 没有推动排名与计划更新

- 类型与优先级：核心产品闭环缺口，P0。
- 代码证据：`backend/app/warehouse/news_alerts.py:1-24,558-582,619-628` 输出alerts/summary/seen/state日志，无决策事件发布；`backend/app/playbooks/picks.py:443-458` 输入为仓库日线/战法/市场周期，不消费消息判读；现有 `ActionSignal` 在contracts/news.py与测试出现，领域库存/排队主要由 scripts/lab/paper_replay.py 调用。
- 正面核实：`scripts/lab/daily_forward.py:235-259` 已将冻结主书持仓与观察池派生到服务器book.json，新闻→持仓关联并非完全缺失，应该保留。
- 改法：事件映射产生结构化ImpactAssessment，按instrument/theme关联使DecisionSnapshot失效并重算，输出带supersedes的TradePlan；规则在热路执行，模型异步解释及提出变更。
- 核实方法：注入持仓负面公告，观察事件→影响→新计划→网页更新全链trace，旧计划仍可追溯。

## C12 多个回测入口对同一市场规则有不同实现

- 类型与优先级：实现分叉，P1。
- 代码证据：`backend/app/research/backtest.py:45-59` 用9.5%/19.5%/29.5%阈值、未覆盖92开头BSE；`backend/app/warehouse/features.py:18-26,56-68` 用分币涨跌停价和半分容差；`backend/app/playbooks/backtest.py:136-137` floor_lock仅检查开盘和收盘都跌停。`backend/app/playbooks/backtest.py:383` 无论回踩买法是否盘中到价，只要开盘涨停便跳过。
- 具体影响：实验室、组合与盘中回放会给同一策略不同成交；开盘跌停→盘中打开→收盘跌停被当全天锁死；9.5%近涨停也可能被判不可成交。
- 改法：同一ExecutionModel接口按数据粒度实现日线/分钟/逐笔，交易规则与成本统一；有局限的日线执行注明场景范围，不把推断当实际成交。
- 核实方法：提供开盘9.6%但未涨停、跌停开盘中途开板、涨停开盘后回踩等确定轨迹，各引擎应遵循同一声明口径。

## C13 期末强制卖出用于前向主书 会把估值当成交

- 类型与优先级：回测与前向复用的语义问题，P1。
- 代码证据：`backend/app/playbooks/backtest.py:468-472` 最末日无条件按收盘清仓，忽略当日买入/停牌/跌停；`backend/app/research/backtest.py:196-198` 同样；`scripts/lab/forward_book.py:45,53,61-64` 从exit_reason=end反推持仓，NAV使用已扣人工清仓费的末日值。
- 具体影响：终点平仓可以作为回测统计约定，却不能成为前向账户的真实成交状态；持仓市值、未实现损益、实际现金与绩效混在一个结果里。日线次日补进后，昨天曾作为end的人工成交又消失。
- 改法：引擎显式返回open_positions与mark_to_market_nav；终点清仓作为独立研究选项，默认前向关闭，不能绕过T+1与交易日状态。
- 核实方法：最后一天买入或停牌/一字跌停时，前向账户保留持仓，无虚构成交；次日重放昨天NAV不因回测终点变化。

## C14 复权价格同时承担研究信号与按股成交记账

- 类型与优先级：已披露建模限制，P1。
- 代码证据：`backend/app/playbooks/backtest.py:61` 明确输入前复权，`backend/app/playbooks/panel.py:187-203` 只有一套OHLC；`backend/app/playbooks/backtest.py:193-194,401-409` 按其价格计算100股整手、费用与现金；没有逐笔公司行动现金/股数账本。
- 具体影响：调整后历史价格适合指标与收益比较，但不能精确代表当时每手所需现金、实际成交价、价格门槛、分红送转与持仓股数。低价/高分红股票和跨公司行动时期尤需区分。
- 改法：保留原始OHLC、因子/总回报视图和公司行动；撮合记账只用当时未复权价格与股数，指标可选择稳定复权视图。
- 核实方法：构造现金分红和送转事件，现金+原始价×股数的权益守恒，信号视图无伪跳跃，股数不因显示复权发生变化。

## C15 成本与绩效描述之间有多套口径

- 类型与优先级：配置/披露不一致，P1。
- 代码证据：`backend/app/playbooks/backtest.py:28-38` DEFAULT_COSTS=双边2.5bp/卖出5bp/双边5bp滑点/过户0；USER_COSTS=佣金5bp、过户0.1bp；simulate默认DEFAULT_COSTS(149行)。`backend/app/playbooks/picks.py:611-612` 又手写费用因子，并且未包含transfer；冻结主书有意沿用历史DEFAULT。README“成本按真实账户算”描述用户费用。
- 具体影响：网站、冻结基准和后续实验可以有不同成本，但不能显示成单一用户账户成本，也不能只比较不一致费率后的收益。
- 改法：cost_profile_id贯穿实验、决策、计划、成交与报告；冻结基准保留原版本，再并列统一用户成本重算视图；费用直接调用同一函数，按订单金额处理最低佣金。
- 核实方法：相同订单回放每一路径，输出成本构成和profile；声明USER_COSTS的报告不得实际用DEFAULT_COSTS。

## C16 当日日线在15:05被过早视为最终数据

- 类型与优先级：当前交易时段规则与数据完成标志不一致，P1，已窄修复。
- 基线代码证据：`backend/app/warehouse/sync.py:47` 将 SESSION_DONE 固定为15:05；`backend/app/warehouse/sync.py:225-234` 的 session_cut / _complete 因而从15:05起允许存入当日日线。2026年盘后固定价格交易时段以及量额计入口径见 GREENFIELD_SPEC.md 第3、7、12节及官方来源目录。
- 具体影响：15:05至15:30之间，价格已形成但成交量和成交额仍可能变化；过早写入的日线可能被后续特征和推荐当作完成数据。问题是结束边界和数据版本，不能仅用15:00收盘价判断整根日线是否最终完成。
- 已实施改法：当前 `backend/app/warehouse/sync.py:47-49` 将界限设为15:30，保持 session_cut 的UTC到中国标准时间转换；使用同一入口的回填和日常同步共同受益。
- 核实方法：`backend/tests/test_warehouse.py:410-419` 覆盖15:05、15:15、15:29:59及15:30，包含UTC等价输入；同时运行既有完整仓库同步测试。此补丁仅更新当前完成边界，没有把旧周内判断改成完整的历史交易日历。新实现仍应按日期、市场、会话和源数据水位确定 is_final，不能只用一个全历史常量。

## C17 历史图表缓存只按创建日期判断新鲜度

- 类型与优先级：数据更新和缓存失效缺口，P1。
- 代码证据：`backend/app/market/history.py:32-34` 仅检查 DataProvenance.as_of 的日期等于今天；`backend/app/market/history.py:47-55` 一旦命中就直接返回当日缓存。`backend/app/market/history.py:76` 的LIVE面板缓存键包含日期和证券代码集合，不包含数据内容版本；`backend/app/market/history.py:149-157` 的预热同样跳过当天已有缓存。
- 具体影响：上午读入昨日数据后，即使下午仓库新增当天日线或更正同日数据，同一进程仍可能一直返回先前版本。前端手动刷新只重新发请求，无法自动越过服务端的同日缓存。该结论来自命中条件，不代表每次刷新一定陈旧。
- 改法：缓存键包含 instrument、timeframe、adjustment、session_scope 和 dataset_revision；数据发布事件更新最新版本映射。历史as_of查询仍绑定原manifest，不因新数据污染过去。TTL可以用于无版本来源，不能替代仓库已知版本。
- 核实方法：同日先读旧manifest，再导入新增K线和一个修订版本，最新查询应变化，固定旧as_of查询应不变；面板在代码集合不变但数据版本变化时也应失效。

## C18 个股图表已有指标，但缺少多周期工作台与自动同步

- 类型与优先级：用户目标的功能缺口，P1。
- 代码证据：`frontend/src/features/stock/StockSheet.tsx:87-112` 标题固定“日 K”，已提供 MA/BOLL 主图切换与 MACD/KDJ/RSI 副图；`frontend/src/features/stock/KChart.tsx:80-82` 已绘制成交量，`frontend/src/features/stock/KChart.tsx:96-106` 已绘制指标。`frontend/src/features/stock/StockSheet.tsx:122-123` 对详情请求使用 useLive(..., false)，报价请求则保持live；`frontend/src/core/live.ts:85-114` 明确false不随行情sequence刷新；`frontend/src/features/stock/StockSheet.tsx:175` 提供手动刷新。
- 具体影响：现有页面并非没有技术分析。真实缺口是周、月、年等周期选择、完整的数据周期语义，以及报价更新与K线/指标/分析详情更新之间的同步。只打开股票页面，不能保证详情随新消息或数据版本自动变化。
- 改法：按 GREENFIELD_SPEC.md 第3、6、11节实现分钟至年线、完整/未完成状态、复权视图、会话范围、消息标记和计划版本。页面消费 snapshot_id 与增量失效事件；缓存按 C17 的版本更新。周期切换不是对320根日线直接重新贴标签，历史长度和聚合时间边界必须满足所选周期。
- 核实方法：同一证券切换日/周/月/年和分钟周期，检查OHLCV、交易日历、未完成K线及指标种子；新消息或新manifest到达后，详情和计划更新且保留原snapshot可回看。保留现有MA、BOLL、MACD、KDJ、RSI和量能能力的行为验收。

## 3 本次已完成的六文件补丁

补丁位于 `patches/legacy_fixes.patch`，用途是在旧系统继续运行期间修复已复现的有限问题。它不构成新架构的业务实现，也不是复制旧代码进入新仓的模板。

| 文件 | 已完成变更 |
| --- | --- |
| backend/app/playbooks/picks.py | DAY_COLUMNS带入board；推荐统一只接纳已知10cm主板；REPORT_FORMAT从3升到4；更新过滤说明 |
| backend/tests/test_picks.py | 测试证券显式提供board；新增主板/创业板/科创板/北交所/ST/新股/未知板块的行为场景 |
| backend/app/warehouse/news_alerts.py | 保存输入游标；分类异常返回None；成功中性[]和失败分开；只标记成功项；失败批回退raw/parquet游标 |
| backend/tests/test_news_alerts.py | raw/parquet × 同进程/重启四种恢复，验证失败重试、成功和中性消息不重复 |
| backend/app/warehouse/sync.py | 当日日线完成边界15:05改15:30 |
| backend/tests/test_warehouse.py | 新增盘后期间和UTC边界测试，调整旧完成时刻样例 |

### 3.1 验证结果

推荐及新闻先加5个回归场景，在旧实现上观察到5个失败；修复后运行两个对应测试文件，结果为 **201 passed, 1 skipped**。日线同步对应完整测试文件为 **47 passed**。两组共 **248 passed, 1 skipped**，不等同于全仓库测试。跳过项是本地缺少申万行业数据文件的 `test_macro_industries_exist_in_the_sw_table`。

独立审查再运行3个测试入口（新闻入口展开为4个参数场景），得到 **6 passed**；这是对同一补丁的重复窄验证，未累加为254。独立审查还检查了6文件diff与直接调用链，结论为有限范围内通过。`git diff --check` 通过。没有commit、push、部署、生产通知或生产交易。

可复现的测试范围，在仓库根目录且装好项目测试依赖后执行：

```bash
python -m pytest backend/tests/test_picks.py backend/tests/test_news_alerts.py -q
python -m pytest backend/tests/test_warehouse.py -q
```

### 3.2 两个必须保留的补丁边界

**旧报告入口没有统一格式检查。** 当前 `backend/app/playbooks/picks.py:551` 的 generate_report 会检查格式，`backend/app/playbooks/picks.py:571` 的 latest_report 直接读取旧报告；`backend/app/scheduler.py:138` 和 `backend/app/api/notify.py:34` 仍可能优先使用旧报告。因此REPORT_FORMAT升级会使主推荐生成入口重建，但不能宣称所有通知或历史缓存入口立即统一。新架构应对“当前版本”统一新鲜度判断，同时保留历史发布记录，不能批量改写旧推荐。

**模型HTTP失败状态不属于新增异常恢复。** 当前 `backend/app/warehouse/news_alerts.py:650,667` 相关调用路径把网络超时/HTTP失败转换为正常返回状态；在无规则命中时，可能仍成为[]并由step标记完成。新增修复覆盖的是 judge 实际抛出的异常。独立离线MockTransport验证一次ConnectTimeout后，第二次step没有重发模型请求。新架构需分别保存规则完成与模型待分析状态，确保模型暂不可用时继续热路径、稍后补分析；这一机制未在旧系统补丁内实现。

## 4 重写与迁移的直接约束

1. 所有页面、研究、纸面执行共享 DecisionSnapshot、PlanVersion、OrderIntent、Fill 和账本合同，保留signal_return与executed_plan_return的区别。
2. 数据具有真实获知时间与版本；旧近似市值、当前行业回填等资产保留来源和限制，不包装成精确历史PIT数据。
3. 模型按能力接入，实际ModelRun输入输出可回放；确定性规则和行情不等待大模型返回。模型增强通过模块与政策版本增长，不要求重写核心账本。
4. 原始数据、冻结前向主书、实验失败记录、历史推荐和原文版本作为独立资产迁移；冻结策略与新策略分别归因。业务实现从新目录开始，迁移工具可以读取旧格式。
5. 审计发现是验收场景的来源。新实现不需要沿用旧类名、文件树或算法实现，但要能说明C01–C18分别由哪个新合同、场景或明确的待补数据解决。

完整分工、阶段验收与迁移步骤见 IMPLEMENTATION_GUIDE.md；业务目标和运行语义以 GREENFIELD_SPEC.md 为准。
