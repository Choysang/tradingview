-- PostgreSQL domain extension; apply AFTER database/core_schema.sql.
-- Core migration must include ranking_scope='sector_overview'. No source is activated here.
-- Tables hold normalized, versioned facts; complete raw bodies remain in ingest.raw_object.
-- Cross-record PIT/readiness rules listed in sources_sectors_fields.json are publication gates.
-- Runtime writes/publication use READ COMMITTED; other isolation levels need separate concurrency validation.
BEGIN;
CREATE SCHEMA IF NOT EXISTS intelligence;

-- Explicit source lineage must agree with the immutable raw object's source.
CREATE UNIQUE INDEX IF NOT EXISTS raw_object_id_source_lineage
    ON ingest.raw_object(raw_object_id,source_id);

CREATE TABLE intelligence.document_revision (
    document_revision_id uuid PRIMARY KEY, -- 文稿版本身份，所有证据引用精确版本
    document_id uuid NOT NULL, -- 同一逻辑文稿的稳定身份
    source_id text NOT NULL REFERENCES core.source, -- 实际接入来源
    source_record_id text NOT NULL, -- 来源原始文稿ID；缺ID时使用有版本的稳定键政策
    revision integer NOT NULL, -- 追加修订号，推送和轮询重复使用同一版本
    raw_object_id uuid NOT NULL, -- 完整原文只引用公共原始层
    document_status text NOT NULL, -- published/corrected/withdrawn/deleted_at_source/unknown
    origin_group text, -- 已识别的原始事实传播链，未知不能凭媒体名称猜测
    origin_status text NOT NULL, -- confirmed_original/confirmed_independent_report/republication/unknown
    original_publisher_source_id text REFERENCES core.source, -- 可核验原始发布机构，未知为null
    title text, -- 获准保存的标题，可缺失并在field_quality说明
    language_code text, -- 语言标记，未知保留null
    published_at timestamptz, -- 来源公布时间，不能替代实际可得时间
    published_time_precision text NOT NULL, -- date/minute/second/microsecond/unknown
    event_time timestamptz, -- 现实事件时间，可能与发布不同或未知
    first_seen_at timestamptz NOT NULL, -- 本平台首次见到该版本
    available_at timestamptz NOT NULL, -- 规范版本可供本平台决策的最早时间
    recorded_at timestamptz NOT NULL, -- 规范行写入时间
    quality_status text NOT NULL, -- valid/partial/disputed/invalid，与时效分开
    field_quality jsonb NOT NULL, -- 缺字段、纠正和补齐来源，按公共字段质量契约
    UNIQUE(source_id,source_record_id,revision),
    UNIQUE(document_id,revision),
    FOREIGN KEY(raw_object_id,source_id) REFERENCES ingest.raw_object(raw_object_id,source_id),
    CHECK(revision > 0),
    CHECK(document_status IN ('published','corrected','withdrawn','deleted_at_source','unknown')),
    CHECK(origin_status IN ('confirmed_original','confirmed_independent_report','republication','unknown')),
    CHECK(origin_status='unknown' OR origin_group IS NOT NULL),
    CHECK(published_time_precision IN ('date','minute','second','microsecond','unknown')),
    CHECK(available_at >= first_seen_at),
    CHECK(quality_status IN ('valid','partial','disputed','invalid')),
    CHECK(jsonb_typeof(field_quality)='object')
);
CREATE INDEX document_pit ON intelligence.document_revision(document_id,available_at DESC,revision DESC);
CREATE INDEX document_origin ON intelligence.document_revision(origin_group,available_at);

CREATE TABLE intelligence.claim_revision (
    claim_revision_id uuid PRIMARY KEY, -- 一个已规范化主张版本
    claim_id uuid NOT NULL, -- 主张稳定身份，不等同文稿ID
    revision integer NOT NULL, -- 主张事实或核验状态修订
    subject_kind text NOT NULL, -- instrument/issuer/sector/macro/event/unresolved等
    subject_key text, -- 未解析实体为null，不能伪造证券身份
    predicate text NOT NULL, -- 版本化谓词，如宣布订单或报告收入
    object_payload jsonb NOT NULL, -- 按claim_schema_version验证的对象值、期间和必要上下文
    claim_schema_version text NOT NULL, -- 主张结构合同版本
    assertion_type text NOT NULL, -- official_disclosure/independently_reported/attributed_statement/opinion/forecast/rumor
    claim_status text NOT NULL, -- supported/contradicted/disputed/unverified/not_falsifiable_yet
    verification_policy_version text NOT NULL, -- 多源核验和冲突处理政策
    model_version text, -- 如使用模型解析则记录，确定性解析可为null
    event_time timestamptz, -- 所指事实时间，可未知
    available_at timestamptz NOT NULL, -- 已收到必要证据且计算完成后的可用时间
    recorded_at timestamptz NOT NULL, -- 此规范版本写入时间
    quality_status text NOT NULL, -- 数据有效性与主张真假状态分别记录
    UNIQUE(claim_id,revision),
    CHECK(revision > 0),
    CHECK(jsonb_typeof(object_payload)='object'),
    CHECK(assertion_type IN ('official_disclosure','independently_reported','attributed_statement','opinion','forecast','rumor')),
    CHECK(claim_status IN ('supported','contradicted','disputed','unverified','not_falsifiable_yet')),
    CHECK(quality_status IN ('valid','partial','disputed','invalid'))
);
CREATE INDEX claim_pit ON intelligence.claim_revision(claim_id,available_at DESC,revision DESC);
CREATE INDEX claim_subject ON intelligence.claim_revision(subject_kind,subject_key,available_at DESC);

CREATE TABLE intelligence.claim_evidence (
    claim_revision_id uuid NOT NULL REFERENCES intelligence.claim_revision, -- 被支持或反驳的主张精确版本
    document_revision_id uuid NOT NULL REFERENCES intelligence.document_revision, -- 证据文稿精确版本
    evidence_ordinal integer NOT NULL, -- 同文稿多处证据的位置序号
    relation text NOT NULL, -- supports/refutes/attributes/context，转载不是额外独立票
    evidence_span jsonb NOT NULL, -- 字段路径和原文偏移；只保存许可的短引文或指针
    extraction_version text NOT NULL, -- 解析器或模型版本
    available_at timestamptz NOT NULL, -- 此证据关联可用于决策的时间
    recorded_at timestamptz NOT NULL, -- 关联写入时间
    PRIMARY KEY(claim_revision_id,document_revision_id,evidence_ordinal),
    CHECK(evidence_ordinal > 0),
    CHECK(relation IN ('supports','refutes','attributes','context')),
    CHECK(jsonb_typeof(evidence_span)='object')
);

CREATE TABLE intelligence.event_revision (
    event_revision_id uuid PRIMARY KEY, -- 聚合事件精确版本
    event_id uuid NOT NULL, -- 跨稿件事件稳定身份
    revision integer NOT NULL, -- 新证据、纠正和传播路径改变均追加版本
    event_category text NOT NULL, -- taxonomy.json中的版本化事件分类
    taxonomy_version text NOT NULL, -- 事件分类合同版本
    event_time timestamptz, -- 事件发生时间，可未知
    event_time_end timestamptz, -- 持续事件结束时间，可未知
    analysis_payload jsonb NOT NULL, -- 已验证Schema的实体、传播路径、期限与竞争解释
    analysis_schema_version text NOT NULL, -- 分析载荷合同版本
    analysis_model_version text NOT NULL, -- 确定性或模型分析实现版本
    available_at timestamptz NOT NULL, -- 引用证据与分析输出都已可用的时点
    recorded_at timestamptz NOT NULL, -- 版本写入时间
    quality_status text NOT NULL, -- 数据质量，不混同事件正负方向
    UNIQUE(event_id,revision),
    CHECK(revision > 0),
    CHECK(event_time_end IS NULL OR event_time IS NULL OR event_time_end >= event_time),
    CHECK(jsonb_typeof(analysis_payload)='object'),
    CHECK(quality_status IN ('valid','partial','disputed','invalid'))
);
CREATE INDEX event_pit ON intelligence.event_revision(event_id,available_at DESC,revision DESC);

CREATE TABLE intelligence.event_claim (
    event_revision_id uuid NOT NULL REFERENCES intelligence.event_revision, -- 聚合事件精确版本
    claim_revision_id uuid NOT NULL REFERENCES intelligence.claim_revision, -- 依赖主张精确版本，修订不能偷换
    relation text NOT NULL, -- core_fact/context/competing_explanation
    PRIMARY KEY(event_revision_id,claim_revision_id,relation),
    CHECK(relation IN ('core_fact','context','competing_explanation'))
);

CREATE TABLE intelligence.community_observation (
    community_observation_id uuid PRIMARY KEY, -- 一次帖子版本及互动采样观察
    source_id text NOT NULL REFERENCES core.source, -- 社区平台接入来源
    raw_object_id uuid NOT NULL, -- 引用公共原始层，正文不再重复保存
    post_id text NOT NULL, -- 平台帖子或稳定引用ID
    post_revision integer NOT NULL, -- 帖子内容修订号
    author_pseudonym text, -- 必需时保存平台匿名标识，不是真实账户身份
    post_created_at timestamptz, -- 平台声称的首次发表时间
    post_edited_at timestamptz, -- 平台提供的修改时间
    sampled_at timestamptz NOT NULL, -- 点赞、评论等互动数据所对应的实际采样时间
    sampling_entry text NOT NULL, -- 页面、流或导出入口，便于解释覆盖
    sampling_sort text NOT NULL, -- 最新/热门等排序方式，防止样本口径隐匿变化
    sampling_window_start timestamptz NOT NULL, -- 抽样窗口起点
    sampling_window_end timestamptz NOT NULL, -- 抽样窗口终点，不含该时刻
    coverage_status text NOT NULL, -- complete_for_declared_scope/partial/unknown
    like_count bigint, -- 该次采样的点赞数，未知为null
    reply_count bigint, -- 该次采样的回复数，未知为null
    unique_participant_count bigint, -- 平台确实提供时才填写，不能用发帖数代替
    analysis_payload jsonb NOT NULL, -- 观点、反讽、去重与主张引用，按版本Schema验证
    analysis_schema_version text NOT NULL, -- 社区分析载荷版本
    analysis_model_version text NOT NULL, -- 情绪/观点模型版本
    available_at timestamptz NOT NULL, -- 本次观察及其分析真正可用时间
    recorded_at timestamptz NOT NULL, -- 规范行写入时间
    quality_status text NOT NULL, -- 数据有效性，与观点正确性不同
    UNIQUE(source_id,post_id,post_revision,sampled_at,analysis_model_version),
    FOREIGN KEY(raw_object_id,source_id) REFERENCES ingest.raw_object(raw_object_id,source_id),
    CHECK(post_revision > 0),
    CHECK(sampling_window_end > sampling_window_start),
    CHECK(coverage_status IN ('complete_for_declared_scope','partial','unknown')),
    CHECK(like_count IS NULL OR like_count >= 0),
    CHECK(reply_count IS NULL OR reply_count >= 0),
    CHECK(unique_participant_count IS NULL OR unique_participant_count >= 0),
    CHECK(available_at >= sampled_at),
    CHECK(jsonb_typeof(analysis_payload)='object'),
    CHECK(quality_status IN ('valid','partial','disputed','invalid'))
);
CREATE INDEX community_pit ON intelligence.community_observation(source_id,post_id,available_at DESC);

CREATE TABLE intelligence.funding_observation (
    funding_observation_id uuid PRIMARY KEY, -- 一次资金观测版本
    source_id text NOT NULL REFERENCES core.source, -- 供应商或正式披露机构
    raw_object_id uuid NOT NULL, -- 原始披露或数据响应
    source_record_key text NOT NULL, -- 来源记录的稳定业务键
    revision integer NOT NULL, -- 观测修订号
    instrument_id uuid REFERENCES core.instrument, -- 个股指标对象，市场指标时为null
    market_key text, -- 市场或方向统计对象，与instrument_id二选一
    metric_namespace text NOT NULL, -- 供应商和指标族命名空间
    metric_name text NOT NULL, -- 精确指标名，不能只叫资金流
    methodology_id text NOT NULL, -- 分类/统计算法身份
    methodology_version text NOT NULL, -- 方法变更产生新版本，不静默拼接
    measurement_class text NOT NULL, -- disclosed_observation/provider_derived/research_inference
    side_convention text NOT NULL, -- 买卖方向、主动方、委托分类或无方向总额等
    trade_or_order_basis text NOT NULL, -- trade/order/holding/report/not_applicable/unknown
    session_scope text NOT NULL, -- 竞价、全天、报告统计期等明确范围
    market_direction text NOT NULL, -- northbound/southbound/domestic/not_applicable/unknown
    observation_window_start timestamptz NOT NULL, -- 统计区间起点
    observation_window_end timestamptz NOT NULL, -- 统计区间终点；瞬时观测允许等于起点
    period_end date, -- 季报等统计期末，与披露时间区分
    raw_value text, -- 原始数值文本及格式，不覆盖
    raw_unit text NOT NULL, -- 原始单位，如CNY_10000或shares
    canonical_value core.finite_decimal, -- 标准值，未知不填0
    canonical_unit text NOT NULL, -- 标准单位，必须由指标合同定义
    currency char(3), -- 金额需币种，份额等非金额可以null
    missing_reason text, -- canonical_value为null时必填
    published_at timestamptz, -- 来源实际公开时间，可未知
    available_at timestamptz NOT NULL, -- 平台可使用此版本的时间
    recorded_at timestamptz NOT NULL, -- 规范行写入时间
    quality_status text NOT NULL, -- 值质量与数据新鲜度分开
    field_quality jsonb NOT NULL, -- 字段补齐、单位和缺失证明
    UNIQUE(source_id,source_record_key,metric_namespace,metric_name,methodology_version,revision),
    FOREIGN KEY(raw_object_id,source_id) REFERENCES ingest.raw_object(raw_object_id,source_id),
    CHECK((instrument_id IS NULL) <> (market_key IS NULL)),
    CHECK(revision > 0),
    CHECK(measurement_class IN ('disclosed_observation','provider_derived','research_inference')),
    CHECK(trade_or_order_basis IN ('trade','order','holding','report','not_applicable','unknown')),
    CHECK(market_direction IN ('northbound','southbound','domestic','not_applicable','unknown')),
    CHECK(observation_window_end >= observation_window_start),
    CHECK((canonical_value IS NULL AND missing_reason IS NOT NULL) OR (canonical_value IS NOT NULL AND missing_reason IS NULL)),
    CHECK(quality_status IN ('valid','partial','disputed','invalid')),
    CHECK(quality_status <> 'valid' OR canonical_value IS NOT NULL),
    CHECK(jsonb_typeof(field_quality)='object')
);
CREATE INDEX funding_series_pit ON intelligence.funding_observation(instrument_id,metric_namespace,metric_name,methodology_version,observation_window_end DESC,available_at DESC);

CREATE TABLE intelligence.seat_entity_version (
    seat_id uuid NOT NULL, -- 已确认营业部或交易单位的稳定身份，不是最终账户
    seat_version integer NOT NULL, -- 名称或实体证据修订号
    entity_kind text NOT NULL, -- broker_branch/trading_unit，未知名称不插入确定实体
    canonical_name text NOT NULL, -- 已核验完整名称
    broker_legal_name text NOT NULL, -- 已核验券商法人名称
    city_code text, -- 营业部分支需城市；交易单位可不适用
    effective_from timestamptz NOT NULL, -- 名称或组织版本实际生效起点
    effective_to timestamptz, -- 生效终点，不含该时刻
    evidence_raw_object_id uuid NOT NULL REFERENCES ingest.raw_object, -- 别名和实体解析证据
    alias_evidence jsonb NOT NULL, -- 别名、迁址、券商更名等有证据关系，不是交易者身份
    available_at timestamptz NOT NULL, -- 平台何时知道该实体版本
    recorded_at timestamptz NOT NULL, -- 实体版本写入时间
    PRIMARY KEY(seat_id,seat_version),
    CHECK(seat_version > 0),
    CHECK(entity_kind IN ('broker_branch','trading_unit')),
    CHECK(entity_kind <> 'broker_branch' OR city_code IS NOT NULL),
    CHECK(effective_to IS NULL OR effective_to > effective_from),
    CHECK(jsonb_typeof(alias_evidence)='array')
);
CREATE INDEX seat_entity_pit ON intelligence.seat_entity_version(seat_id,effective_from,available_at);

CREATE TABLE intelligence.seat_report (
    report_revision_id uuid PRIMARY KEY, -- 一份龙虎榜披露的精确版本
    source_id text NOT NULL REFERENCES core.source, -- 实际取得来源
    raw_object_id uuid NOT NULL, -- 原始报告引用
    source_report_id text NOT NULL, -- 来源报告ID或规范业务键
    revision integer NOT NULL, -- 披露修订号
    exchange_code text NOT NULL, -- 交易所
    instrument_id uuid NOT NULL REFERENCES core.instrument, -- 已解析证券
    window_start date NOT NULL, -- 榜单统计交易日起点，含当日
    window_end date NOT NULL, -- 榜单统计交易日终点，含当日
    disclosure_reason text NOT NULL, -- 原始原因或已核验代码，不硬编码未来规则
    rule_version text NOT NULL, -- 该披露适用的规则版本
    trade_scope text NOT NULL, -- auction_only/explicit_other/unknown
    published_at timestamptz, -- 来源公布时间，可未知
    available_at timestamptz NOT NULL, -- 平台可用时间，盘后资料不能进入当日盘中
    recorded_at timestamptz NOT NULL, -- 规范报告写入时间
    quality_status text NOT NULL, -- 榜单完整性与字段有效性
    UNIQUE(source_id,source_report_id,revision),
    FOREIGN KEY(raw_object_id,source_id) REFERENCES ingest.raw_object(raw_object_id,source_id),
    CHECK(revision > 0),
    CHECK(window_end >= window_start),
    CHECK(trade_scope IN ('auction_only','explicit_other','unknown')),
    CHECK(quality_status IN ('valid','partial','disputed','invalid'))
);
CREATE INDEX seat_report_pit ON intelligence.seat_report(instrument_id,window_end DESC,available_at DESC);

CREATE TABLE intelligence.seat_report_row (
    report_revision_id uuid NOT NULL REFERENCES intelligence.seat_report, -- 精确报告版本
    side text NOT NULL, -- buy/sell，同营业部可在两边分别出现
    rank integer NOT NULL, -- 榜内排名
    raw_seat_name text NOT NULL, -- 完整原始披露名称，不能自动纠正前东路
    raw_amount core.finite_decimal NOT NULL, -- 来源原始金额
    raw_unit text NOT NULL, -- CNY/CNY_10000，接口映射明确
    canonical_amount_cny core.finite_decimal NOT NULL, -- 标准金额，单位人民币元
    resolution_status text NOT NULL, -- resolved/ambiguous/unresolved/report_local_label
    seat_id uuid, -- 已确认稳定实体；未知或通用机构局部标签为null
    seat_version integer, -- 实体版本，不能默认最新
    resolution_evidence jsonb NOT NULL, -- 解析证据或无法解析原因
    PRIMARY KEY(report_revision_id,side,rank),
    FOREIGN KEY(seat_id,seat_version) REFERENCES intelligence.seat_entity_version,
    CHECK(side IN ('buy','sell')),
    CHECK(rank > 0),
    CHECK(raw_amount >= 0 AND canonical_amount_cny >= 0),
    CHECK(raw_unit IN ('CNY','CNY_10000')),
    CHECK(canonical_amount_cny=raw_amount * CASE raw_unit WHEN 'CNY_10000' THEN 10000 ELSE 1 END),
    CHECK(resolution_status IN ('resolved','ambiguous','unresolved','report_local_label')),
    CHECK((seat_id IS NULL)=(seat_version IS NULL)),
    CHECK((resolution_status='resolved')=(seat_id IS NOT NULL)),
    CHECK(jsonb_typeof(resolution_evidence)='object')
);

CREATE TABLE decision.candidate_pool_event (
    pool_event_id uuid PRIMARY KEY, -- 候选池状态变化事件身份，重复投递沿用
    pool_id uuid NOT NULL, -- 候选池稳定身份
    instrument_id uuid NOT NULL REFERENCES core.instrument, -- 被观察证券
    event_sequence bigint NOT NULL, -- 此池此证券的单调状态序号，CAS提交
    policy_id text NOT NULL, -- 进入退出和滞回政策
    policy_version text NOT NULL, -- 冻结参数及验证结果版本
    taxonomy_id text NOT NULL, -- 板块体系
    taxonomy_version integer NOT NULL, -- 当时体系版本
    node_id text NOT NULL, -- 板块节点
    membership_snapshot_id uuid NOT NULL REFERENCES market.dataset_manifest, -- 当时已知成分输入快照
    release_id uuid NOT NULL REFERENCES decision.release, -- 与榜单和工作台同一资料包
    previous_state text, -- 首事件为null；后续必须与上一事件当前状态一致
    current_state text NOT NULL, -- observing/qualified/actionable/cooling/removed
    trigger_code text NOT NULL, -- 入池、维持、降温、过期或硬失效原因
    trigger_payload jsonb NOT NULL, -- 可重放的阈值输入、窗口、计数和证据引用
    plan_id uuid, -- actionable必须引用已验证个股计划
    plan_version integer, -- 精确计划版本
    decision_as_of timestamptz NOT NULL, -- 本次评估使用的历史信息截止点
    available_at timestamptz NOT NULL, -- 此状态事件完成并可对外使用的时间
    recorded_at timestamptz NOT NULL, -- 事件写入时间
    UNIQUE(pool_id,instrument_id,event_sequence),
    FOREIGN KEY(taxonomy_id,taxonomy_version,node_id) REFERENCES core.taxonomy_node,
    FOREIGN KEY(plan_id,plan_version) REFERENCES decision.trade_plan,
    CHECK(event_sequence > 0),
    CHECK(current_state IN ('observing','qualified','actionable','cooling','removed')),
    CHECK(previous_state IS NULL OR previous_state IN ('observing','qualified','actionable','cooling','removed')),
    CHECK(previous_state IS NOT NULL OR (event_sequence=1 AND current_state='observing')),
    CHECK((plan_id IS NULL)=(plan_version IS NULL)),
    CHECK(current_state <> 'actionable' OR plan_id IS NOT NULL),
    CHECK(available_at >= decision_as_of),
    CHECK(jsonb_typeof(trigger_payload)='object')
);
CREATE INDEX candidate_pool_current ON decision.candidate_pool_event(pool_id,instrument_id,event_sequence DESC);

CREATE TABLE decision.sector_rank_entry (
    ranking_id uuid NOT NULL REFERENCES decision.ranking_run, -- 父run必须为sector_overview，和个股榜同一release机制
    taxonomy_id text NOT NULL, -- 板块体系
    taxonomy_version integer NOT NULL, -- 体系版本
    node_id text NOT NULL, -- 被排名板块，不是instrument_id
    rank_kind text NOT NULL, -- sector_opportunity_rank/sector_attention_rank/sector_rising_rank
    rank integer NOT NULL, -- 本run中唯一顺序
    score core.finite_decimal NOT NULL, -- 特定目标内的相对分，不自动是概率
    member_count integer NOT NULL, -- 此成分快照中的成员数
    observed_member_count integer NOT NULL, -- 关键指标有覆盖的成员数
    membership_snapshot_id uuid NOT NULL REFERENCES market.dataset_manifest, -- 成分版本与数据可得性
    explanation_ref text NOT NULL, -- 优势、集中度、消息独立性和缺数据解释
    quality_status text NOT NULL, -- valid/partial/disputed/invalid
    PRIMARY KEY(ranking_id,taxonomy_id,taxonomy_version,node_id),
    UNIQUE(ranking_id,rank),
    FOREIGN KEY(taxonomy_id,taxonomy_version,node_id) REFERENCES core.taxonomy_node,
    CHECK(rank_kind IN ('sector_opportunity_rank','sector_attention_rank','sector_rising_rank')),
    CHECK(rank > 0),
    CHECK(member_count >= 0 AND observed_member_count >= 0 AND observed_member_count <= member_count),
    CHECK(quality_status IN ('valid','partial','disputed','invalid'))
);

CREATE FUNCTION decision.guard_sector_rank_entry() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target_id uuid; target_release uuid; target_state text; target_scope text;
        target_taxonomy text; target_taxonomy_version integer; target_membership uuid;
BEGIN
    IF TG_OP='UPDATE' AND
       (NEW.ranking_id<>OLD.ranking_id OR NEW.taxonomy_id<>OLD.taxonomy_id OR
        NEW.taxonomy_version<>OLD.taxonomy_version OR NEW.node_id<>OLD.node_id) THEN
        RAISE EXCEPTION 'A sector ranking row identity cannot be reassigned';
    END IF;
    IF TG_OP='DELETE' THEN target_id:=OLD.ranking_id; ELSE target_id:=NEW.ranking_id; END IF;
    SELECT release_id INTO target_release FROM decision.ranking_run WHERE ranking_id=target_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'Unknown parent ranking run'; END IF;
    -- This is the same parent lock used by publication and the core ranking guards.
    SELECT release_state INTO target_state FROM decision.release WHERE release_id=target_release FOR UPDATE;
    IF target_state IN ('READY','SUPERSEDED') THEN
        RAISE EXCEPTION 'Published sector ranking entries are immutable';
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    -- Read definition after acquiring the shared serialization lock.
    SELECT ranking_scope,taxonomy_id,taxonomy_version,membership_snapshot_id
      INTO target_scope,target_taxonomy,target_taxonomy_version,target_membership
      FROM decision.ranking_run WHERE ranking_id=target_id;
    IF target_scope<>'sector_overview' THEN
        RAISE EXCEPTION 'Sector entries require a sector_overview ranking run';
    END IF;
    IF NEW.taxonomy_id IS DISTINCT FROM target_taxonomy OR
       NEW.taxonomy_version IS DISTINCT FROM target_taxonomy_version OR
       NEW.membership_snapshot_id IS DISTINCT FROM target_membership THEN
        RAISE EXCEPTION 'Sector entry taxonomy or membership differs from its ranking run';
    END IF;
    IF EXISTS (
        SELECT 1 FROM decision.sector_rank_entry s
         WHERE s.ranking_id=target_id AND s.rank_kind<>NEW.rank_kind
           AND (TG_OP<>'UPDATE' OR s.node_id<>OLD.node_id)
    ) THEN
        RAISE EXCEPTION 'One sector ranking run must have one rank kind';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER sector_rank_entry_identity_and_publication
    BEFORE INSERT OR UPDATE OR DELETE ON decision.sector_rank_entry FOR EACH ROW
    EXECUTE FUNCTION decision.guard_sector_rank_entry();

CREATE FUNCTION decision.guard_sector_rank_parent() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    -- Prevent insert-valid-children-then-change-parent bypass, while staged.
    -- Core guard also locks this release; repeating the lock is transaction-local.
    PERFORM 1 FROM decision.release WHERE release_id=OLD.release_id FOR UPDATE;
    IF EXISTS (
        SELECT 1 FROM decision.sector_rank_entry s WHERE s.ranking_id=OLD.ranking_id
           AND (NEW.ranking_scope<>'sector_overview' OR
                s.taxonomy_id IS DISTINCT FROM NEW.taxonomy_id OR
                s.taxonomy_version IS DISTINCT FROM NEW.taxonomy_version OR
                s.membership_snapshot_id IS DISTINCT FROM NEW.membership_snapshot_id)
    ) THEN
        RAISE EXCEPTION 'Ranking metadata cannot invalidate existing sector entries';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER sector_rank_parent_consistency
    BEFORE UPDATE ON decision.ranking_run FOR EACH ROW
    EXECUTE FUNCTION decision.guard_sector_rank_parent();

-- SQL checks row-local invariants. Publication transaction additionally verifies:
-- 1. Observed mode filters actual available_at <= decision_as_of; declared reconstruction uses
--    derived policy_usable_at + availability_policy_version + explicit latency assumptions,
--    never overwrites actual available_at. Every required manifest must be READY.
-- 2. Claim/event available_at >= availability of ALL referenced evidence and model completion.
-- 3. Pool sequence/previous_state uses a per(pool_id,instrument_id) lock or CAS, and validates
--    policy-approved transition, membership, common release and exact plan/instrument identity.
-- 4. Sector/stock row kinds and parent taxonomy/membership are guarded above and in core migration.
--    Snapshot readiness, PIT membership availability and feature coverage remain publication gates.
-- 5. seat_report_row may use only seat versions available by the selected research snapshot;
--    a report-local institution label is never promoted to a stable actor without new evidence.
COMMIT;
