-- Pulse v3 integration seed; apply after core_schema.sql, ledger_schema.sql, sources_sectors_schema.sql.
-- PostgreSQL 18-compatible syntax. Static parsing is not runtime/concurrency verification.
BEGIN;
CREATE SCHEMA IF NOT EXISTS research;
CREATE SCHEMA IF NOT EXISTS analysis;

CREATE TABLE market.quote_projection (
    quote_snapshot_id uuid NOT NULL REFERENCES market.dataset_manifest, -- 报价事实所属不可变数据快照
    instrument_id uuid NOT NULL REFERENCES core.instrument, -- 证券稳定身份
    quote_policy_version text NOT NULL, -- 源选择、去重、乱序与修订政策；版本序号仅在此政策空间可比
    live_quote_version bigint NOT NULL, -- 同证券同报价政策的单调发布序号，不使用价格或时间字符串代替
    source_id text NOT NULL, -- 选定观测来源，不从多源平均制造一笔报价
    endpoint_id text NOT NULL, -- 精确报价接口
    source_schema_version text NOT NULL, -- 来源结构版本
    mapping_version text NOT NULL, -- 字段、单位与时区映射版本
    observation_id uuid NOT NULL REFERENCES ingest.observation, -- 原始规范观测血缘
    trade_date date, -- 源/交易所明确的交易日，未知不以抓取日假填
    source_event_time timestamptz, -- 源给出的报价时刻，未知必须null
    first_seen_at timestamptz NOT NULL, -- 本平台首次收到该报价版本
    available_at timestamptz NOT NULL, -- 规范化完成可用时刻，不能改成历史推测时刻
    currency char(3) NOT NULL, -- 当前A股报价为CNY
    last_price_raw core.price_raw, -- 未复权最新成交价；无新成交/未知时允许null
    bid_price_raw core.price_raw, -- 当前买一价；无L1能力或无报价为null，不填日线收盘
    ask_price_raw core.price_raw, -- 当前卖一价；无L1能力或无报价为null
    bid_volume_shares bigint, -- 买一挂单股数，不是成交量或手
    ask_volume_shares bigint, -- 卖一挂单股数
    quality_status text NOT NULL, -- valid/partial/invalid/disputed，与按请求时刻派生的新鲜度独立
    field_quality jsonb NOT NULL, -- 缺L1、来源修订、缺时间与单位处理依据
    PRIMARY KEY(quote_snapshot_id,instrument_id),
    UNIQUE(instrument_id,quote_policy_version,live_quote_version),
    FOREIGN KEY(source_id,endpoint_id,source_schema_version,mapping_version) REFERENCES core.source_endpoint,
    CHECK(live_quote_version>0 AND currency='CNY'),
    CHECK(available_at>=first_seen_at),
    CHECK(bid_volume_shares IS NULL OR bid_volume_shares>=0),
    CHECK(ask_volume_shares IS NULL OR ask_volume_shares>=0),
    CHECK(quality_status IN ('valid','partial','invalid','disputed')),
    CHECK(jsonb_typeof(field_quality)='object')
);
CREATE INDEX quote_instrument_current ON market.quote_projection(instrument_id,quote_policy_version,live_quote_version DESC);
COMMENT ON TABLE market.quote_projection IS 'Rebuildable hot cache, not canonical immutable truth. QuoteQuery must verify the pinned canonical manifest/artifact and source/row identity before serving. Freshness is derived at response time; daily close is never a live-quote fallback.';

ALTER TABLE decision.trade_plan ADD CONSTRAINT trade_plan_account_fk
    FOREIGN KEY(account_id) REFERENCES execution.account_head(account_id);
ALTER TABLE decision.trade_plan ADD CONSTRAINT trade_plan_known_account_version_fk
    FOREIGN KEY(account_id,expected_account_version) REFERENCES execution.account_event(account_id,account_sequence);
ALTER TABLE execution.journal_line ADD CONSTRAINT journal_line_instrument_fk
    FOREIGN KEY(instrument_id) REFERENCES core.instrument(instrument_id);
ALTER TABLE execution.position_projection ADD CONSTRAINT position_instrument_fk
    FOREIGN KEY(instrument_id) REFERENCES core.instrument(instrument_id);
ALTER TABLE execution.quantity_lot ADD CONSTRAINT lot_instrument_fk
    FOREIGN KEY(instrument_id) REFERENCES core.instrument(instrument_id);
ALTER TABLE execution.order_reservation ADD CONSTRAINT reservation_instrument_fk
    FOREIGN KEY(instrument_id) REFERENCES core.instrument(instrument_id);

CREATE TABLE decision.release_slot (
    slot_key text PRIMARY KEY, -- 稳定发布作用域，包含市场/目标/期限/用户作用域；不是latest字符串随意复用
    current_release_id uuid REFERENCES decision.release, -- 当前对外完整发布包，初次发布前为null
    pointer_generation bigint NOT NULL DEFAULT 0, -- 当前指针代次；成功发布递增，重试不递增
    issued_stage_generation bigint NOT NULL DEFAULT 0, -- 已签发的最新任务代次，创建新stage时单调递增
    published_stage_generation bigint NOT NULL DEFAULT 0, -- 当前指针来自的stage代次
    updated_at timestamptz NOT NULL, -- 该指针或任务代次最后变更时间
    CHECK (pointer_generation >= 0 AND issued_stage_generation >= 0 AND published_stage_generation >= 0),
    CHECK (published_stage_generation <= issued_stage_generation),
    CHECK ((current_release_id IS NULL) = (pointer_generation=0)),
    CHECK ((current_release_id IS NULL) = (published_stage_generation=0))
);
CREATE TABLE decision.release_stage (
    stage_id uuid PRIMARY KEY, -- 一次分析任务不可复用的身份，失败重试保留此身份
    slot_key text NOT NULL REFERENCES decision.release_slot, -- 任务被签发到哪个发布作用域
    stage_generation bigint NOT NULL, -- 任务签发代次，不能由旧worker重新获取来冒充新任务
    base_pointer_generation bigint NOT NULL, -- 创建stage时捕获的发布指针代次，永不重写
    base_release_id uuid REFERENCES decision.release, -- 创建stage时捕获的旧发布包，初始为null
    release_id uuid NOT NULL UNIQUE REFERENCES decision.release, -- 此任务唯一可生成的BUILDING包
    decision_as_of timestamptz NOT NULL, -- 此任务固定的信息可得性截止点
    input_context_hash core.sha256_hex NOT NULL, -- 固定输入闭包摘要；新输入必须创建新stage
    created_at timestamptz NOT NULL, -- 由协调服务签发stage的时间
    UNIQUE(slot_key,stage_generation),
    CHECK(stage_generation>0 AND base_pointer_generation>=0),
    CHECK((base_release_id IS NULL)=(base_pointer_generation=0))
);
CREATE TABLE decision.release_publication (
    stage_id uuid PRIMARY KEY REFERENCES decision.release_stage, -- 一个stage最多成功发布一次，作为永久幂等记录
    slot_key text NOT NULL REFERENCES decision.release_slot, -- 发布作用域
    pointer_generation bigint NOT NULL, -- 这次成功发布之后的代次
    previous_release_id uuid REFERENCES decision.release, -- 发布前包，初次发布为null
    release_id uuid NOT NULL UNIQUE REFERENCES decision.release, -- 本次成功发布的完整包
    event_id uuid NOT NULL UNIQUE, -- 对应同事务outbox事件，永久发布事实不依赖消息保留时长
    published_at timestamptz NOT NULL, -- 指针、release与outbox在同一事务的业务发布时间
    UNIQUE(slot_key,pointer_generation),
    FOREIGN KEY(event_id) REFERENCES ops.outbox(event_id) DEFERRABLE INITIALLY DEFERRED,
    CHECK(pointer_generation>0)
);
ALTER TABLE decision.ranking_run ADD CONSTRAINT ranking_identity_release_unique UNIQUE(ranking_id,release_id);
CREATE TABLE decision.actionability_snapshot (
    actionability_id uuid PRIMARY KEY, -- 当前可行动性叠层身份，分页必须固定此ID
    release_id uuid NOT NULL REFERENCES decision.release, -- 冻结分析包，不原地修改其action_rank
    ranking_id uuid REFERENCES decision.ranking_run, -- 可选对应的冻结榜单
    account_id uuid, -- 个性化叠层账户，公共研究叠层为null
    account_version bigint, -- 参与计算的确定账户序号，与account_id成对
    quote_snapshot_id uuid NOT NULL REFERENCES market.dataset_manifest, -- 本次实时复核使用的具体报价快照
    actionability_policy_version text NOT NULL, -- 触发、库存、价格与排序政策版本
    checked_at timestamptz NOT NULL, -- 复核时刻，不是旧分析decision_as_of
    valid_until timestamptz NOT NULL, -- 叠层有效期；事件硬失效可更早终止当前展示
    result_ref text NOT NULL, -- 不可变ActionabilitySnapshot规范结果，含account_action_rank
    result_hash core.sha256_hex NOT NULL, -- 规范叠层结果摘要
    FOREIGN KEY(account_id,account_version) REFERENCES execution.account_event(account_id,account_sequence),
    FOREIGN KEY(ranking_id,release_id) REFERENCES decision.ranking_run(ranking_id,release_id),
    CHECK((account_id IS NULL)=(account_version IS NULL)),
    CHECK(account_version IS NULL OR account_version>=0),
    CHECK(valid_until>checked_at)
);
CREATE INDEX actionability_account_recent ON decision.actionability_snapshot(account_id,checked_at DESC);

CREATE TABLE research.paper_revision (
    paper_id uuid NOT NULL, -- 论文稳定身份，DOI/arXiv等外部ID另存
    paper_revision integer NOT NULL, -- 本站方法卡与来源修订版本
    canonical_id text NOT NULL, -- 有依据的DOI、arXiv或正式目录ID
    title text NOT NULL, -- 论文原标题
    primary_url text NOT NULL, -- 原论文/正式会议/作者资源，不把二手总结当论文
    venue text, -- 已核验会议或期刊，未知为null
    publication_year integer, -- 已核验年份
    publication_status text NOT NULL, -- preprint/submitted/accepted/published/withdrawn/unknown
    publication_status_note text NOT NULL, -- 保留原始状态与核查依据，不能把预印本改称顶会录用
    read_depth text NOT NULL, -- metadata/abstract/full_text_sections/full_text/code_review/reproduced，互不冒充
    raw_object_id uuid REFERENCES ingest.raw_object, -- 已存原文证据，可用公开目录研究时暂null
    method_card jsonb NOT NULL, -- 假设、方法、数据、作者结果、局限及可复现步骤的类型化内容
    content_hash core.sha256_hex NOT NULL, -- 该方法卡与证据版本摘要
    published_at timestamptz, -- 可证明的源公布时间
    available_at timestamptz NOT NULL, -- 本站方法卡版本实际可用时间
    recorded_at timestamptz NOT NULL, -- 登记时间
    PRIMARY KEY(paper_id,paper_revision),
    UNIQUE(canonical_id,paper_revision),
    CHECK(paper_revision>0),
    CHECK(publication_year IS NULL OR publication_year BETWEEN 1900 AND 2200),
    CHECK(publication_status IN ('preprint','submitted','accepted','published','withdrawn','unknown')),
    CHECK(read_depth IN ('metadata','abstract','full_text_sections','full_text','code_review','reproduced'))
);
CREATE TABLE research.hypothesis (
    hypothesis_id uuid NOT NULL, -- 可检验研究假设身份
    hypothesis_version integer NOT NULL, -- 假设内容变化追加版本
    paper_id uuid, -- 可选方法来源论文
    paper_revision integer, -- 精确论文方法卡版本
    strategy_id uuid, -- 可选对应策略定义
    strategy_version integer, -- 精确策略版本
    claim text NOT NULL, -- 具体可证伪命题，不能只写某论文很强
    mechanism text NOT NULL, -- 预期经济或统计机制
    forecast_target text NOT NULL, -- 检验目标定义
    horizon_id text NOT NULL, -- 目标期限
    input_requirements jsonb NOT NULL, -- 可得数据、单位、采样与覆盖要求
    falsification_conditions jsonb NOT NULL, -- 何种结果否定/限制假设
    specification_hash core.sha256_hex NOT NULL, -- 假设及协议摘要
    available_at timestamptz NOT NULL, -- 假设预登记时点
    PRIMARY KEY(hypothesis_id,hypothesis_version),
    FOREIGN KEY(paper_id,paper_revision) REFERENCES research.paper_revision,
    FOREIGN KEY(strategy_id,strategy_version) REFERENCES strategy.definition,
    CHECK(hypothesis_version>0),
    CHECK((paper_id IS NULL)=(paper_revision IS NULL)),
    CHECK((strategy_id IS NULL)=(strategy_version IS NULL))
);
CREATE TABLE research.evaluation_context (
    evaluation_context_id uuid PRIMARY KEY, -- 一组可比较实验的固定语义环境
    context_hash core.sha256_hex NOT NULL UNIQUE, -- 完整比较口径摘要
    forecast_target text NOT NULL, -- 共同预测/执行收益目标
    horizon_id text NOT NULL, -- 共同预测/持有期限
    decision_as_of timestamptz NOT NULL, -- 信息可得性边界
    availability_policy_version text NOT NULL, -- observed/reconstructed等政策明确版本
    availability_policy_document jsonb NOT NULL, -- 派生policy_usable_at的规则引用与延迟假设；绝不改原始available_at
    universe_policy_version text NOT NULL, -- 股票池及成员历史政策
    market_rules_version text NOT NULL, -- 统一规则版本
    execution_policy_version text NOT NULL, -- 统一成交模型版本
    cost_policy_version text NOT NULL, -- 成本与渠道费用版本
    ledger_policy_version text NOT NULL, -- 统一账本版本
    performance_policy_version text NOT NULL, -- TWR/归因/基准定义版本
    numeric_policy_version text NOT NULL, -- 金额与科学计算精度政策
    train_start timestamptz NOT NULL, -- 训练区间起点
    train_end timestamptz NOT NULL, -- 训练区间终点，半开区间
    validation_start timestamptz NOT NULL, -- 验证区间起点
    validation_end timestamptz NOT NULL, -- 验证区间终点
    blind_start timestamptz, -- 最终盲测区间起点，未设置为null
    blind_end timestamptz, -- 最终盲测终点
    embargo_seconds bigint NOT NULL, -- 按标签跨度等预登记的隔离时长，不等于自动消除泄漏
    benchmark_specification jsonb NOT NULL, -- 简单基线/持有基线、指标与切片定义
    access_policy jsonb NOT NULL, -- 训练/验证/盲测访问预算，禁止联赛轮次反复调盲测
    created_at timestamptz NOT NULL, -- 比较协议登记时刻
    sealed_at timestamptz, -- 输入关系写齐后封存，实验只能使用sealed环境
    CHECK(train_end>train_start AND validation_end>validation_start AND validation_start>=train_end),
    CHECK((blind_start IS NULL)=(blind_end IS NULL)),
    CHECK(blind_start IS NULL OR (blind_start>=validation_end AND blind_end>blind_start)),
    CHECK(embargo_seconds>=0)
);
CREATE TABLE research.evaluation_input (
    evaluation_context_id uuid NOT NULL REFERENCES research.evaluation_context, -- 固定比较环境
    snapshot_id uuid NOT NULL REFERENCES market.dataset_manifest, -- 确切已发布数据快照
    input_role text NOT NULL, -- bars/evidence/sector_membership/calendar等
    PRIMARY KEY(evaluation_context_id,snapshot_id,input_role)
);
CREATE TABLE research.experiment_run (
    experiment_id uuid PRIMARY KEY, -- 一次真实运行身份；失败重试和新参数不能混成同一结果
    instance_id uuid NOT NULL REFERENCES strategy.instance, -- 固定策略参数实例
    hypothesis_id uuid NOT NULL, -- 被检验假设
    hypothesis_version integer NOT NULL, -- 精确假设版本
    evaluation_context_id uuid NOT NULL REFERENCES research.evaluation_context, -- 固定比较环境
    run_purpose text NOT NULL, -- screening/validation/shadow/blind_final
    run_state text NOT NULL, -- queued/running/completed/failed/cancelled，与策略生命周期不同
    implementation_digest text NOT NULL, -- 代码/容器/依赖锁摘要引用，不能只有策略名称
    random_seed bigint NOT NULL, -- 可重放随机种子
    run_manifest jsonb NOT NULL, -- 模型、特征、数据、状态与环境的完整运行清单
    lease_epoch bigint NOT NULL, -- 提交权令牌，失效worker不能覆盖新结果
    started_at timestamptz, -- 开始计算时间
    completed_at timestamptz, -- 终态完成时间
    result_ref text, -- 结果、交易轨迹与指标清单不可变引用
    result_hash core.sha256_hex, -- 结果内容摘要
    error_code text, -- 失败原因；失败不能伪装成功空集
    FOREIGN KEY(hypothesis_id,hypothesis_version) REFERENCES research.hypothesis,
    CHECK(run_purpose IN ('screening','validation','shadow','blind_final')),
    CHECK(run_state IN ('queued','running','completed','failed','cancelled')),
    CHECK(random_seed>=0 AND lease_epoch>0),
    CHECK(run_state<>'completed' OR (result_ref IS NOT NULL AND result_hash IS NOT NULL AND completed_at IS NOT NULL)),
    CHECK(completed_at IS NULL OR started_at IS NULL OR completed_at>=started_at)
);
CREATE INDEX experiment_instance_context ON research.experiment_run(instance_id,evaluation_context_id,completed_at DESC);
CREATE TABLE research.metric_value (
    experiment_id uuid NOT NULL REFERENCES research.experiment_run, -- 指标所属运行
    metric_name text NOT NULL, -- 明确经济含义的指标名，如executed_strategy_twr
    metric_definition_version text NOT NULL, -- 指标公式与方向政策
    slice_key text NOT NULL, -- overall或预登记市场/行业/期限切片
    value core.finite_decimal, -- 结果值；无法计算为null并给原因
    unit text NOT NULL, -- ratio/CNY/count等；净值变化与概率不可混用
    lower_bound core.finite_decimal, -- 若有区间估计则给下界
    upper_bound core.finite_decimal, -- 与下界成对
    confidence_level core.finite_decimal, -- 区间含义；无区间时null
    sample_count bigint NOT NULL, -- 有效观测数，不把股票日数误称独立实验数
    quality_status text NOT NULL, -- valid/partial/invalid/disputed
    missing_reason text, -- value=null的明确原因
    computed_at timestamptz NOT NULL, -- 指标完成时刻
    PRIMARY KEY(experiment_id,metric_name,metric_definition_version,slice_key),
    CHECK(sample_count>=0),
    CHECK(quality_status IN ('valid','partial','invalid','disputed')),
    CHECK(value IS NOT NULL OR missing_reason IS NOT NULL),
    CHECK((lower_bound IS NULL)=(upper_bound IS NULL)),
    CHECK(lower_bound IS NULL OR upper_bound>=lower_bound),
    CHECK(confidence_level IS NULL OR confidence_level>0 AND confidence_level<1)
);
CREATE TABLE research.league_definition (
    league_id uuid NOT NULL, -- 同目标同环境的研究联赛身份
    league_version integer NOT NULL, -- 配对/预算/晋级规则变化产生新版本
    evaluation_context_id uuid NOT NULL REFERENCES research.evaluation_context, -- 所有可比对手共用环境
    display_name text NOT NULL, -- 展示名称
    pairing_policy_version text NOT NULL, -- 瑞士轮/近邻比较及轮空政策
    multiple_testing_policy_version text NOT NULL, -- 尝试计数与多重比较处理
    promotion_policy_version text NOT NULL, -- 晋级门槛，联赛名次不自动授予active
    round_budget integer NOT NULL, -- 最大轮数/计算预算的一部分
    policy_document jsonb NOT NULL, -- 重复对手、失效场景、同源家族及盲测边界
    definition_hash core.sha256_hex NOT NULL, -- 联赛定义摘要
    created_at timestamptz NOT NULL, -- 规则冻结时间
    PRIMARY KEY(league_id,league_version),
    CHECK(league_version>0 AND round_budget>0)
);
CREATE TABLE research.league_round (
    league_id uuid NOT NULL, -- 联赛身份
    league_version integer NOT NULL, -- 固定联赛定义
    round_no integer NOT NULL, -- 从1开始的轮次
    round_state text NOT NULL, -- scheduled/running/completed/failed
    scheduled_at timestamptz NOT NULL, -- 本轮安排时间
    completed_at timestamptz, -- 本轮结束时间
    result_ref text, -- 本轮比较与不可比较原因的完整记录
    PRIMARY KEY(league_id,league_version,round_no),
    FOREIGN KEY(league_id,league_version) REFERENCES research.league_definition,
    CHECK(round_no>0),
    CHECK(round_state IN ('scheduled','running','completed','failed')),
    CHECK(round_state<>'completed' OR (completed_at IS NOT NULL AND result_ref IS NOT NULL))
);
CREATE TABLE research.league_pairing (
    pairing_id uuid PRIMARY KEY, -- 一次可追溯配对
    league_id uuid NOT NULL, -- 联赛身份
    league_version integer NOT NULL, -- 固定定义版本
    round_no integer NOT NULL, -- 所属轮次
    left_instance_id uuid NOT NULL REFERENCES strategy.instance, -- 左侧策略实例
    right_instance_id uuid REFERENCES strategy.instance, -- 轮空时为null
    left_experiment_id uuid REFERENCES research.experiment_run, -- 明确左侧已运行结果
    right_experiment_id uuid REFERENCES research.experiment_run, -- 明确右侧已运行结果
    outcome text NOT NULL, -- pending/left/right/tie/bye/incomparable
    comparison_policy_version text NOT NULL, -- 配对指标、成本与区间判定政策
    score_delta core.finite_decimal, -- 只在已对齐可比较目标下记录
    reason_ref text NOT NULL, -- 对齐检查、结果或无法比较原因
    completed_at timestamptz, -- 比较完成时间
    FOREIGN KEY(league_id,league_version,round_no) REFERENCES research.league_round,
    CHECK(right_instance_id IS NULL OR right_instance_id<>left_instance_id),
    CHECK(outcome IN ('pending','left','right','tie','bye','incomparable')),
    CHECK(outcome<>'bye' OR right_instance_id IS NULL),
    CHECK(outcome NOT IN ('left','right','tie') OR (right_instance_id IS NOT NULL AND left_experiment_id IS NOT NULL AND right_experiment_id IS NOT NULL AND completed_at IS NOT NULL))
);
CREATE TABLE research.promotion_decision (
    promotion_id uuid PRIMARY KEY, -- 一次晋级/暂停/退役政策判定
    instance_id uuid NOT NULL REFERENCES strategy.instance, -- 被评估策略实例
    evaluation_experiment_id uuid NOT NULL REFERENCES research.experiment_run, -- 核心验收运行，必须对应本实例
    policy_version text NOT NULL, -- 生命周期准入与退役政策版本
    from_lifecycle text NOT NULL, -- 判定时的原状态
    to_lifecycle text NOT NULL, -- 政策建议的新状态，拒绝时可与旧状态相同
    outcome text NOT NULL, -- approved/rejected；保存被拒证据不只保存胜者
    gate_results jsonb NOT NULL, -- 数据、执行、样本外、成本、盲测、影子等逐门槛依据
    reason text NOT NULL, -- 具体通过/拒绝理由
    decided_at timestamptz NOT NULL, -- 实际决定时刻，不能倒填入训练
    applied_at timestamptz, -- StrategyRegistry CAS成功应用时刻；纯记录不自动改策略
    CHECK(from_lifecycle IN ('discovered','specified','data_ready','screened','validated','shadow','active','paused','retired')),
    CHECK(to_lifecycle IN ('discovered','specified','data_ready','screened','validated','shadow','active','paused','retired')),
    CHECK(outcome IN ('approved','rejected')),
    CHECK(outcome='approved' OR applied_at IS NULL)
);
CREATE TABLE research.strategy_parent (
    strategy_id uuid NOT NULL, -- 新组合/派生策略身份
    strategy_version integer NOT NULL, -- 新定义版本
    parent_strategy_id uuid NOT NULL, -- 来源父策略身份
    parent_strategy_version integer NOT NULL, -- 精确父版本，不使用latest
    contribution_role text NOT NULL, -- selection/allocation/entry/holding/execution/exit/blend
    composition_policy_version text NOT NULL, -- 合成、去冗余与权重语义版本
    evidence_ref text NOT NULL, -- 合并或借鉴理由及消融验证
    PRIMARY KEY(strategy_id,strategy_version,parent_strategy_id,parent_strategy_version,contribution_role),
    FOREIGN KEY(strategy_id,strategy_version) REFERENCES strategy.definition,
    FOREIGN KEY(parent_strategy_id,parent_strategy_version) REFERENCES strategy.definition,
    CHECK(strategy_id<>parent_strategy_id OR strategy_version<>parent_strategy_version),
    CHECK(contribution_role IN ('selection','allocation','entry','holding','execution','exit','blend'))
);

CREATE TABLE ui.watchlist (
    watchlist_id uuid PRIMARY KEY, -- 自选分组身份
    principal_id uuid NOT NULL, -- 所有者，认证域由实施接入，不能拿模型身份代替
    watchlist_version bigint NOT NULL, -- 整组及成员修改的乐观版本
    display_name text NOT NULL, -- 分组名称
    description text, -- 用户说明
    created_at timestamptz NOT NULL, -- 初次保存
    updated_at timestamptz NOT NULL, -- 最近一次显式更改
    CHECK(watchlist_version>0)
);
CREATE TABLE ui.watchlist_member (
    watchlist_id uuid NOT NULL REFERENCES ui.watchlist ON DELETE CASCADE, -- 自选分组
    instrument_id uuid NOT NULL REFERENCES core.instrument, -- 复用证券身份，不新建代码表
    sort_position integer NOT NULL, -- 用户排序，与研究推荐rank不同
    added_release_id uuid REFERENCES decision.release, -- 可选记录加入时看到的分析版本
    note text, -- 简短用户注释
    added_at timestamptz NOT NULL, -- 用户加入时刻
    PRIMARY KEY(watchlist_id,instrument_id),
    UNIQUE(watchlist_id,sort_position) DEFERRABLE INITIALLY DEFERRED,
    CHECK(sort_position>0)
);
CREATE TABLE ui.annotation (
    annotation_id uuid PRIMARY KEY, -- 笔记或画线稳定身份
    principal_id uuid NOT NULL, -- 所有者
    annotation_version bigint NOT NULL, -- 编辑并发版本
    instrument_id uuid REFERENCES core.instrument, -- 个股注释对象，可选
    watchlist_id uuid REFERENCES ui.watchlist ON DELETE SET NULL, -- 可选关联分组，删除分组不删除笔记
    annotation_kind text NOT NULL, -- note/drawing/review
    anchor_release_id uuid REFERENCES decision.release, -- 复盘锚定的分析版本
    anchor_snapshot_id uuid REFERENCES market.dataset_manifest, -- 图表画线锚定的数据/复权版本
    body jsonb NOT NULL, -- 按Annotation合同存笔记/绘图坐标，不与市场事实混写
    created_at timestamptz NOT NULL, -- 创建时间
    updated_at timestamptz NOT NULL, -- 最近显式编辑
    CHECK(annotation_version>0),
    CHECK(annotation_kind IN ('note','drawing','review')),
    CHECK(jsonb_typeof(body)='object')
);
CREATE TABLE analysis.model_policy (
    model_policy_id text NOT NULL, -- 分析能力政策身份，不绑定宣传中的未来商品名
    model_policy_version integer NOT NULL, -- 提示、检索、模型、预算变化必须新版本
    task_kind text NOT NULL, -- claim_extraction/impact/community/research/plan_proposal等职责
    provider_id text NOT NULL, -- 已配置模型供应商或本地运行器
    model_locator jsonb NOT NULL, -- 实际可调用模型及精确版本/快照，不预设未来型号存在
    response_contract_id text NOT NULL, -- 结构化输出规范
    response_schema_version text NOT NULL, -- 输出规范版本
    prompt_digest core.sha256_hex NOT NULL, -- 系统模板与任务模板摘要
    retrieval_policy_version text NOT NULL, -- 检索、引用与上下文构建政策
    capability_requirements jsonb NOT NULL, -- 上下文、结构输出、工具等能力要求
    timeout_seconds integer NOT NULL, -- 有界超时
    token_budget integer NOT NULL, -- 有界调用预算
    rollout_state text NOT NULL, -- candidate/shadow/active/retired
    evaluation_experiment_id uuid REFERENCES research.experiment_run, -- 升级评测依据，可未完成时null
    created_at timestamptz NOT NULL, -- 政策登记时间
    PRIMARY KEY(model_policy_id,model_policy_version),
    FOREIGN KEY(response_contract_id,response_schema_version) REFERENCES core.contract_version,
    CHECK(model_policy_version>0 AND timeout_seconds>0 AND token_budget>0),
    CHECK(rollout_state IN ('candidate','shadow','active','retired'))
);
CREATE TABLE analysis.model_run (
    model_run_id uuid PRIMARY KEY, -- 一次分析调用或明确复用结果身份
    model_policy_id text NOT NULL, -- 固定能力政策
    model_policy_version integer NOT NULL, -- 精确政策版本
    stage_id uuid REFERENCES decision.release_stage, -- 可选正式重分析任务；慢结果不能跳到另一个stage
    input_context_hash core.sha256_hex NOT NULL, -- 分析输入闭包摘要
    input_manifest jsonb NOT NULL, -- 原文/特征/快照精确引用，不能只有自由文本提示
    analysis_as_of timestamptz NOT NULL, -- 输入信息可得性截止
    run_state text NOT NULL, -- queued/running/succeeded/failed/cancelled
    publication_eligibility text NOT NULL, -- unchecked/eligible/superseded/expired，与调用成功不同
    started_at timestamptz, -- 开始调用
    completed_at timestamptz, -- 收到结果或终止
    output_ref text, -- 不可变结构化输出引用
    output_hash core.sha256_hex, -- 输出摘要
    usage_document jsonb NOT NULL, -- 实际token、延迟与可用的费用证据；未知不伪造
    error_code text, -- 失败分类
    FOREIGN KEY(model_policy_id,model_policy_version) REFERENCES analysis.model_policy,
    CHECK(run_state IN ('queued','running','succeeded','failed','cancelled')),
    CHECK(publication_eligibility IN ('unchecked','eligible','superseded','expired')),
    CHECK(run_state<>'succeeded' OR (output_ref IS NOT NULL AND output_hash IS NOT NULL AND completed_at IS NOT NULL)),
    CHECK(completed_at IS NULL OR started_at IS NULL OR completed_at>=started_at)
);
CREATE TABLE ops.job_definition (
    job_id uuid NOT NULL, -- 调度任务身份
    job_version integer NOT NULL, -- 调度/参数/重试政策版本
    application_operation text NOT NULL, -- 复用同一应用服务操作，不独立复制业务代码
    schedule_document jsonb NOT NULL, -- cron/周期/交易日历/事件触发与时区的类型化配置
    parameter_contract_id text NOT NULL, -- 参数合同
    parameter_schema_version text NOT NULL, -- 参数合同版本
    enabled boolean NOT NULL, -- 期望启用，与run健康不同
    concurrency_key text NOT NULL, -- 源/账户/任务组并发作用域
    max_concurrency integer NOT NULL, -- 每个作用域的并行上限
    timeout_seconds integer NOT NULL, -- 超时界限
    retry_policy jsonb NOT NULL, -- 退避、抖动、可重试错误与预算
    created_at timestamptz NOT NULL, -- 登记时间
    PRIMARY KEY(job_id,job_version),
    FOREIGN KEY(parameter_contract_id,parameter_schema_version) REFERENCES core.contract_version,
    CHECK(job_version>0 AND max_concurrency>0 AND timeout_seconds>0)
);
CREATE TABLE ops.job_run (
    job_run_id uuid PRIMARY KEY, -- 一次可恢复调度运行
    job_id uuid NOT NULL, -- 任务定义
    job_version integer NOT NULL, -- 精确定义版本
    idempotency_key text NOT NULL, -- 同一计划时间/事件触发的去重键
    stage_id uuid REFERENCES decision.release_stage, -- 分析任务绑定不可变stage，重试不能换base
    parameter_hash core.sha256_hex NOT NULL, -- 本次参数摘要
    scheduled_for timestamptz NOT NULL, -- 计划执行时间
    run_state text NOT NULL, -- queued/running/succeeded/failed/cancelled
    lease_epoch bigint NOT NULL, -- fencing token，旧worker不再有提交权
    attempt_count integer NOT NULL, -- 本次逻辑run累计尝试数
    started_at timestamptz, -- 实际开始
    heartbeat_at timestamptz, -- 最近心跳，不能当作数据刷新成功
    completed_at timestamptz, -- 终态时间
    error_code text, -- 具体失败分类
    FOREIGN KEY(job_id,job_version) REFERENCES ops.job_definition,
    UNIQUE(job_id,job_version,idempotency_key),
    CHECK(run_state IN ('queued','running','succeeded','failed','cancelled')),
    CHECK(lease_epoch>0 AND attempt_count>=0),
    CHECK(run_state NOT IN ('succeeded','failed','cancelled') OR completed_at IS NOT NULL)
);
CREATE INDEX job_run_ready ON ops.job_run(scheduled_for,job_run_id) WHERE run_state='queued';

CREATE TABLE ops.command_receipt (
    principal_id uuid NOT NULL, -- 用户/服务主体作用域，不能跨用户重用幂等键
    operation_id text NOT NULL REFERENCES core.api_binding, -- 精确注册API操作
    idempotency_key text NOT NULL, -- 调用方重试键；相同键异载荷拒绝
    request_hash core.sha256_hex NOT NULL, -- 规范请求摘要
    response_contract_id text NOT NULL, -- 已提交响应合同
    response_schema_version text NOT NULL, -- 响应合同版本
    response_document jsonb NOT NULL, -- 已提交的原始规范响应或可重建版本引用
    committed_at timestamptz NOT NULL, -- 与业务写入和outbox同事务完成
    expires_at timestamptz NOT NULL, -- 明确幂等保留窗口，过期后按公布政策处理
    PRIMARY KEY(principal_id,operation_id,idempotency_key),
    FOREIGN KEY(response_contract_id,response_schema_version) REFERENCES core.contract_version,
    CHECK(expires_at>committed_at),
    CHECK(jsonb_typeof(response_document)='object')
);

-- Stage creation captures the base under the same slot lock. No caller-supplied rebasing.
CREATE FUNCTION decision.begin_release_stage(p_stage_id uuid,p_slot_key text,p_release_id uuid,p_input_hash core.sha256_hex)
RETURNS decision.release_stage LANGUAGE plpgsql AS $$
DECLARE slot_row decision.release_slot%ROWTYPE; release_row decision.release%ROWTYPE; stage_row decision.release_stage%ROWTYPE;
BEGIN
    SELECT * INTO STRICT slot_row FROM decision.release_slot WHERE slot_key=p_slot_key FOR UPDATE;
    SELECT * INTO stage_row FROM decision.release_stage WHERE stage_id=p_stage_id;
    IF FOUND THEN
        IF stage_row.slot_key<>p_slot_key OR stage_row.release_id<>p_release_id OR stage_row.input_context_hash<>p_input_hash THEN
            RAISE EXCEPTION 'Stage retry content conflict';
        END IF;
        RETURN stage_row;
    END IF;
    SELECT * INTO STRICT release_row FROM decision.release WHERE release_id=p_release_id FOR UPDATE;
    IF release_row.release_state<>'BUILDING' THEN RAISE EXCEPTION 'New stage requires BUILDING release'; END IF;
    UPDATE decision.release_slot SET issued_stage_generation=issued_stage_generation+1,updated_at=clock_timestamp()
      WHERE slot_key=p_slot_key RETURNING * INTO slot_row;
    INSERT INTO decision.release_stage VALUES
      (p_stage_id,p_slot_key,slot_row.issued_stage_generation,slot_row.pointer_generation,slot_row.current_release_id,
       p_release_id,release_row.decision_as_of,p_input_hash,clock_timestamp()) RETURNING * INTO stage_row;
    RETURN stage_row;
END;
$$;
CREATE FUNCTION decision.publish_release_stage(p_stage_id uuid,p_release_hash core.sha256_hex,p_event_id uuid)
RETURNS decision.release_publication LANGUAGE plpgsql AS $$
DECLARE s decision.release_stage%ROWTYPE; h decision.release_slot%ROWTYPE; r decision.release%ROWTYPE;
        previous_as_of timestamptz; publication decision.release_publication%ROWTYPE; stamp timestamptz; recorded_hash core.sha256_hex;
BEGIN
    SELECT * INTO STRICT s FROM decision.release_stage WHERE stage_id=p_stage_id;
    SELECT * INTO STRICT h FROM decision.release_slot WHERE slot_key=s.slot_key FOR UPDATE;
    SELECT * INTO publication FROM decision.release_publication WHERE stage_id=p_stage_id;
    IF FOUND THEN
        IF publication.event_id<>p_event_id THEN RAISE EXCEPTION 'Publication retry event ID conflict'; END IF;
        SELECT release_hash INTO recorded_hash FROM decision.release WHERE release_id=publication.release_id;
        IF recorded_hash IS DISTINCT FROM p_release_hash THEN RAISE EXCEPTION 'Publication retry content hash conflict'; END IF;
        RETURN publication;
    END IF;
    IF s.stage_generation<>h.issued_stage_generation OR s.base_pointer_generation<>h.pointer_generation
       OR s.base_release_id IS DISTINCT FROM h.current_release_id THEN
        RAISE EXCEPTION 'Stale stage: immutable base or latest-issued generation differs';
    END IF;
    SELECT * INTO STRICT r FROM decision.release WHERE release_id=s.release_id FOR UPDATE;
    IF r.release_state<>'BUILDING' OR r.decision_as_of<>s.decision_as_of THEN
        RAISE EXCEPTION 'Stage/release binding changed or release is not BUILDING';
    END IF;
    IF h.current_release_id IS NOT NULL THEN
        SELECT decision_as_of INTO previous_as_of FROM decision.release WHERE release_id=h.current_release_id;
        IF s.decision_as_of<previous_as_of THEN RAISE EXCEPTION 'Cannot move analysis as_of backwards'; END IF;
    END IF;
    stamp:=clock_timestamp();
    UPDATE decision.release SET release_state='READY',release_hash=p_release_hash,published_at=stamp WHERE release_id=s.release_id;
    INSERT INTO decision.release_publication VALUES
      (s.stage_id,s.slot_key,h.pointer_generation+1,h.current_release_id,s.release_id,p_event_id,stamp)
      RETURNING * INTO publication;
    UPDATE decision.release_slot SET current_release_id=s.release_id,pointer_generation=publication.pointer_generation,
       published_stage_generation=s.stage_generation,updated_at=stamp WHERE slot_key=s.slot_key;
    -- Caller inserts matching canonical outbox event in this SAME transaction before COMMIT.
    -- Deferred FK and publication checks reject a missing or mismatched event.
    RETURN publication;
END;
$$;
CREATE FUNCTION decision.assert_publication_outbox() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE event_row ops.outbox%ROWTYPE; stage_row decision.release_stage%ROWTYPE;
BEGIN
    SELECT * INTO STRICT event_row FROM ops.outbox WHERE event_id=NEW.event_id;
    SELECT * INTO STRICT stage_row FROM decision.release_stage WHERE stage_id=NEW.stage_id;
    IF event_row.event_type<>'decision.published' OR event_row.aggregate_type<>'release_slot'
       OR event_row.aggregate_id<>NEW.slot_key OR event_row.aggregate_version<>NEW.pointer_generation
       OR event_row.payload->>'release_id' IS DISTINCT FROM NEW.release_id::text
       OR event_row.payload->>'stage_id' IS DISTINCT FROM NEW.stage_id::text
       OR NEW.release_id<>stage_row.release_id OR NEW.slot_key<>stage_row.slot_key THEN
        RAISE EXCEPTION 'Publication and outbox/stage identities differ';
    END IF;
    RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER publication_outbox_at_commit AFTER INSERT ON decision.release_publication
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION decision.assert_publication_outbox();
CREATE FUNCTION research.reject_immutable_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Versioned record is immutable; append a new version'; END;
$$;
CREATE TRIGGER release_stage_immutable BEFORE UPDATE OR DELETE ON decision.release_stage FOR EACH ROW EXECUTE FUNCTION research.reject_immutable_mutation();
CREATE TRIGGER release_publication_immutable BEFORE UPDATE OR DELETE ON decision.release_publication FOR EACH ROW EXECUTE FUNCTION research.reject_immutable_mutation();
CREATE TRIGGER actionability_immutable BEFORE UPDATE OR DELETE ON decision.actionability_snapshot FOR EACH ROW EXECUTE FUNCTION research.reject_immutable_mutation();
CREATE TRIGGER paper_revision_immutable BEFORE UPDATE OR DELETE ON research.paper_revision FOR EACH ROW EXECUTE FUNCTION research.reject_immutable_mutation();
CREATE TRIGGER hypothesis_immutable BEFORE UPDATE OR DELETE ON research.hypothesis FOR EACH ROW EXECUTE FUNCTION research.reject_immutable_mutation();
CREATE TRIGGER league_definition_immutable BEFORE UPDATE OR DELETE ON research.league_definition FOR EACH ROW EXECUTE FUNCTION research.reject_immutable_mutation();
CREATE TRIGGER strategy_parent_immutable BEFORE UPDATE OR DELETE ON research.strategy_parent FOR EACH ROW EXECUTE FUNCTION research.reject_immutable_mutation();

CREATE FUNCTION research.guard_league_pairing() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE context_id uuid; round_status text; run_row research.experiment_run%ROWTYPE;
BEGIN
    IF TG_OP='DELETE' THEN
        SELECT round_state INTO STRICT round_status FROM research.league_round WHERE league_id=OLD.league_id AND league_version=OLD.league_version AND round_no=OLD.round_no FOR UPDATE;
        IF round_status IN ('completed','failed') THEN RAISE EXCEPTION 'Closed league round is immutable'; END IF;
        RETURN OLD;
    END IF;
    IF TG_OP='UPDATE' AND (NEW.league_id<>OLD.league_id OR NEW.league_version<>OLD.league_version OR NEW.round_no<>OLD.round_no) THEN
        RAISE EXCEPTION 'Pairing league/round binding is immutable';
    END IF;
    SELECT d.evaluation_context_id,r.round_state INTO STRICT context_id,round_status
      FROM research.league_round r JOIN research.league_definition d USING(league_id,league_version)
      WHERE r.league_id=NEW.league_id AND r.league_version=NEW.league_version AND r.round_no=NEW.round_no FOR UPDATE OF r;
    IF round_status IN ('completed','failed') THEN RAISE EXCEPTION 'Closed league round is immutable'; END IF;
    IF EXISTS (SELECT 1 FROM research.league_pairing p WHERE p.league_id=NEW.league_id AND p.league_version=NEW.league_version
        AND p.round_no=NEW.round_no AND p.pairing_id<>NEW.pairing_id
        AND (p.left_instance_id IN (NEW.left_instance_id,NEW.right_instance_id)
             OR p.right_instance_id IN (NEW.left_instance_id,NEW.right_instance_id))) THEN
        RAISE EXCEPTION 'An instance may appear only once in one league round';
    END IF;
    IF NEW.left_experiment_id IS NOT NULL THEN
        SELECT * INTO STRICT run_row FROM research.experiment_run WHERE experiment_id=NEW.left_experiment_id;
        IF run_row.instance_id<>NEW.left_instance_id OR run_row.evaluation_context_id<>context_id
           OR run_row.run_state<>'completed' OR run_row.run_purpose='blind_final' THEN
            RAISE EXCEPTION 'Left experiment is not a comparable non-blind completed run';
        END IF;
    END IF;
    IF NEW.right_experiment_id IS NOT NULL THEN
        SELECT * INTO STRICT run_row FROM research.experiment_run WHERE experiment_id=NEW.right_experiment_id;
        IF NEW.right_instance_id IS NULL OR run_row.instance_id<>NEW.right_instance_id OR run_row.evaluation_context_id<>context_id
           OR run_row.run_state<>'completed' OR run_row.run_purpose='blind_final' THEN
            RAISE EXCEPTION 'Right experiment is not a comparable non-blind completed run';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER league_pairing_comparable BEFORE INSERT OR UPDATE OR DELETE ON research.league_pairing
FOR EACH ROW EXECUTE FUNCTION research.guard_league_pairing();
CREATE FUNCTION research.guard_promotion_identity() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE run_row research.experiment_run%ROWTYPE;
BEGIN
    SELECT * INTO STRICT run_row FROM research.experiment_run WHERE experiment_id=NEW.evaluation_experiment_id;
    IF run_row.instance_id<>NEW.instance_id OR (NEW.outcome='approved' AND run_row.run_state<>'completed') THEN
        RAISE EXCEPTION 'Promotion experiment must belong to the evaluated instance and be completed when approved';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER promotion_experiment_identity BEFORE INSERT OR UPDATE ON research.promotion_decision
FOR EACH ROW EXECUTE FUNCTION research.guard_promotion_identity();

-- Runtime roles must use Coordinator/Registry services (or these functions), not direct pointer DML.
-- Deployment must grant only the intended service entry points and run concurrency/fault tests.
-- Remaining gates: full dependency/strategy DAG cycles; PIT visibility; model rollout evaluation;
-- auth principal FKs; per-source/financial-rule semantics; explicit rollback command/event (ordinary stage is forward-only).

CREATE FUNCTION decision.guard_actionability_inputs() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE release_row decision.release%ROWTYPE; quote_row market.dataset_manifest%ROWTYPE;
BEGIN
    SELECT * INTO STRICT release_row FROM decision.release WHERE release_id=NEW.release_id;
    SELECT * INTO STRICT quote_row FROM market.dataset_manifest WHERE snapshot_id=NEW.quote_snapshot_id;
    IF release_row.release_state NOT IN ('READY','SUPERSEDED') OR quote_row.manifest_state<>'READY'
       OR release_row.published_at>NEW.checked_at OR quote_row.ready_at>NEW.checked_at THEN
        RAISE EXCEPTION 'Actionability requires published analysis and quotes already available at checked_at';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER actionability_inputs_ready BEFORE INSERT ON decision.actionability_snapshot
FOR EACH ROW EXECUTE FUNCTION decision.guard_actionability_inputs();
CREATE FUNCTION research.guard_context_seal() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='INSERT' THEN
        IF NEW.sealed_at IS NOT NULL THEN RAISE EXCEPTION 'Create context unsealed, add inputs, then seal'; END IF;
        RETURN NEW;
    END IF;
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Evaluation context history is immutable'; END IF;
    IF OLD.sealed_at IS NOT NULL OR NEW.sealed_at IS NULL OR (to_jsonb(NEW)-'sealed_at')<>(to_jsonb(OLD)-'sealed_at') THEN
        RAISE EXCEPTION 'Only the initial context seal is allowed';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM research.evaluation_input WHERE evaluation_context_id=NEW.evaluation_context_id)
       OR EXISTS (SELECT 1 FROM research.evaluation_input i JOIN market.dataset_manifest m ON m.snapshot_id=i.snapshot_id
                  WHERE i.evaluation_context_id=NEW.evaluation_context_id AND m.manifest_state<>'READY') THEN
        RAISE EXCEPTION 'A sealed context requires published input snapshots';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER evaluation_context_seal BEFORE INSERT OR UPDATE OR DELETE ON research.evaluation_context
FOR EACH ROW EXECUTE FUNCTION research.guard_context_seal();
CREATE FUNCTION research.guard_evaluation_input() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE context_id uuid; sealed timestamptz;
BEGIN
    IF TG_OP='UPDATE' AND NEW.evaluation_context_id<>OLD.evaluation_context_id THEN RAISE EXCEPTION 'Input context binding is immutable'; END IF;
    IF TG_OP='DELETE' THEN context_id:=OLD.evaluation_context_id; ELSE context_id:=NEW.evaluation_context_id; END IF;
    SELECT sealed_at INTO STRICT sealed FROM research.evaluation_context WHERE evaluation_context_id=context_id FOR UPDATE;
    IF sealed IS NOT NULL THEN RAISE EXCEPTION 'Sealed evaluation inputs are immutable'; END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER evaluation_input_seal BEFORE INSERT OR UPDATE OR DELETE ON research.evaluation_input
FOR EACH ROW EXECUTE FUNCTION research.guard_evaluation_input();
CREATE FUNCTION research.guard_experiment_run() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE sealed timestamptz;
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Experiment history is retained'; END IF;
    IF TG_OP='UPDATE' THEN
        IF OLD.run_state='completed' THEN RAISE EXCEPTION 'Completed experiment is immutable'; END IF;
        IF ROW(NEW.instance_id,NEW.hypothesis_id,NEW.hypothesis_version,NEW.evaluation_context_id,NEW.run_purpose,NEW.implementation_digest,NEW.random_seed,NEW.run_manifest)
           IS DISTINCT FROM ROW(OLD.instance_id,OLD.hypothesis_id,OLD.hypothesis_version,OLD.evaluation_context_id,OLD.run_purpose,OLD.implementation_digest,OLD.random_seed,OLD.run_manifest) THEN
            RAISE EXCEPTION 'Experiment inputs are immutable; create a new experiment';
        END IF;
    END IF;
    SELECT sealed_at INTO STRICT sealed FROM research.evaluation_context WHERE evaluation_context_id=NEW.evaluation_context_id FOR SHARE;
    IF sealed IS NULL THEN RAISE EXCEPTION 'Experiment requires sealed evaluation context'; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER experiment_run_binding BEFORE INSERT OR UPDATE OR DELETE ON research.experiment_run
FOR EACH ROW EXECUTE FUNCTION research.guard_experiment_run();
CREATE FUNCTION research.guard_metric_result() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE run_id uuid; status text;
BEGIN
    IF TG_OP='UPDATE' AND NEW.experiment_id<>OLD.experiment_id THEN RAISE EXCEPTION 'Metric run binding is immutable'; END IF;
    IF TG_OP='DELETE' THEN run_id:=OLD.experiment_id; ELSE run_id:=NEW.experiment_id; END IF;
    SELECT run_state INTO STRICT status FROM research.experiment_run WHERE experiment_id=run_id FOR UPDATE;
    IF status='completed' THEN RAISE EXCEPTION 'Completed experiment metrics are immutable'; END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER metric_result_seal BEFORE INSERT OR UPDATE OR DELETE ON research.metric_value
FOR EACH ROW EXECUTE FUNCTION research.guard_metric_result();
CREATE FUNCTION research.guard_closed_round() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.round_state IN ('completed','failed') THEN RAISE EXCEPTION 'Closed league round cannot be reopened or deleted'; END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER league_round_seal BEFORE UPDATE OR DELETE ON research.league_round
FOR EACH ROW EXECUTE FUNCTION research.guard_closed_round();


CREATE FUNCTION analysis.guard_model_run_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE expected_hash core.sha256_hex; expected_as_of timestamptz;
BEGIN
    IF TG_OP='UPDATE' AND ROW(NEW.model_policy_id,NEW.model_policy_version,NEW.stage_id,NEW.input_context_hash,NEW.input_manifest,NEW.analysis_as_of)
       IS DISTINCT FROM ROW(OLD.model_policy_id,OLD.model_policy_version,OLD.stage_id,OLD.input_context_hash,OLD.input_manifest,OLD.analysis_as_of) THEN
        RAISE EXCEPTION 'Model run policy/input/stage binding is immutable';
    END IF;
    IF NEW.stage_id IS NOT NULL THEN
        SELECT input_context_hash,decision_as_of INTO STRICT expected_hash,expected_as_of FROM decision.release_stage WHERE stage_id=NEW.stage_id;
        IF NEW.input_context_hash<>expected_hash OR NEW.analysis_as_of<>expected_as_of THEN
            RAISE EXCEPTION 'Model run input and as_of must match its fixed stage';
        END IF;
    END IF;
    IF TG_OP='UPDATE' AND OLD.run_state='succeeded'
       AND (to_jsonb(NEW)-'publication_eligibility')<>(to_jsonb(OLD)-'publication_eligibility') THEN
        RAISE EXCEPTION 'Successful model result is immutable; only publication eligibility can change';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER model_run_fixed_inputs BEFORE INSERT OR UPDATE ON analysis.model_run
FOR EACH ROW EXECUTE FUNCTION analysis.guard_model_run_binding();
CREATE FUNCTION ops.guard_job_run_binding() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(NEW.job_id,NEW.job_version,NEW.idempotency_key,NEW.stage_id,NEW.parameter_hash,NEW.scheduled_for)
       IS DISTINCT FROM ROW(OLD.job_id,OLD.job_version,OLD.idempotency_key,OLD.stage_id,OLD.parameter_hash,OLD.scheduled_for) THEN
        RAISE EXCEPTION 'Job retry cannot change its definition, parameters or stage/base';
    END IF;
    IF NEW.lease_epoch<OLD.lease_epoch OR NEW.attempt_count<OLD.attempt_count THEN
        RAISE EXCEPTION 'Lease epoch and attempt count cannot move backwards';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER job_run_fixed_inputs BEFORE UPDATE ON ops.job_run
FOR EACH ROW EXECUTE FUNCTION ops.guard_job_run_binding();

COMMIT;
