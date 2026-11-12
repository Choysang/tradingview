-- Pulse v3 PostgreSQL core schema seed.
-- SQL dialect checked with PostgreSQL parser; not an executed production migration.
-- UUIDs supplied by application; no UUID extension required. Money uses decimal strings at API boundaries.
-- execution.* tables are owned by the unified execution/accounting migration, not duplicated here.
BEGIN;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS ingest;
CREATE SCHEMA IF NOT EXISTS market;
CREATE SCHEMA IF NOT EXISTS strategy;
CREATE SCHEMA IF NOT EXISTS decision;
CREATE SCHEMA IF NOT EXISTS ops;
CREATE SCHEMA IF NOT EXISTS ui;

CREATE DOMAIN core.finite_decimal AS numeric(38,12)
    CHECK (VALUE::text NOT IN ('NaN','Infinity','-Infinity'));
CREATE DOMAIN core.price_raw AS numeric(38,12)
    CHECK (VALUE > 0 AND VALUE::text NOT IN ('NaN','Infinity','-Infinity'));
CREATE DOMAIN core.nonnegative_amount AS numeric(38,12)
    CHECK (VALUE >= 0 AND VALUE::text NOT IN ('NaN','Infinity','-Infinity'));
CREATE DOMAIN core.sha256_hex AS text
    CHECK (VALUE ~ '^[0-9a-f]{64}$');

CREATE TABLE core.contract_version (
    contract_id text NOT NULL, -- 规范合同稳定名称；生产者与消费者共享，不含供应商临时列名
    schema_version text NOT NULL, -- 合同版本；与策略版本、数据修订版本分开
    schema_document jsonb NOT NULL, -- 实际JSON Schema；不得只存自然语言字段表
    schema_hash core.sha256_hex NOT NULL, -- 规范序列化后的合同内容摘要
    compatibility_policy text NOT NULL, -- 兼容性规则版本；含严格消费者的新增字段处理
    created_at timestamptz NOT NULL, -- 合同登记时刻，UTC语义
    PRIMARY KEY (contract_id, schema_version),
    CHECK (jsonb_typeof(schema_document)='object')
);
CREATE TABLE core.source (
    source_id text PRIMARY KEY, -- 稳定来源标识，如tushare或akshare_eastmoney
    display_name text NOT NULL, -- 展示名称，不用于关联主键
    provider_uri text NOT NULL, -- 供应商或发布方官方入口
    default_origin_group text, -- 已知原始内容产生者；未知为null，不填供应商猜测
    created_at timestamptz NOT NULL -- 来源登记时间
);
CREATE TABLE core.source_endpoint (
    source_id text NOT NULL REFERENCES core.source(source_id), -- 来源注册标识
    endpoint_id text NOT NULL, -- 精确接口标识；同供应商daily与rt_k不共享单位配置
    source_schema_version text NOT NULL, -- 原始字段结构或文档观测版本
    mapping_version text NOT NULL, -- 解析、单位转换、时区、代码映射政策版本
    contract_id text NOT NULL, -- 该适配器输出的规范合同
    schema_version text NOT NULL, -- 输出合同版本
    mapping_hash core.sha256_hex NOT NULL, -- 映射规则摘要；变更后不能覆盖旧记录
    enabled boolean NOT NULL, -- 用户或部署政策期望启用，不能当健康状态
    credential_ref text, -- 凭据引用，不保存明文密钥
    transport_kind text NOT NULL, -- stream/poll/webhook等传输类型
    poll_interval_seconds integer, -- 轮询起始间隔，受源限额与会话政策约束
    stale_after_seconds integer NOT NULL, -- 端点时效判定阈值配置；不是保证源一定更新
    policy_document jsonb NOT NULL, -- 时区、日历、空结果、回看窗、配额和会话边界政策
    created_at timestamptz NOT NULL, -- 此端点版本登记时刻
    PRIMARY KEY (source_id, endpoint_id, source_schema_version, mapping_version),
    FOREIGN KEY (contract_id,schema_version) REFERENCES core.contract_version,
    CHECK (transport_kind IN ('stream','poll','webhook','batch','manual_import')),
    CHECK (poll_interval_seconds IS NULL OR poll_interval_seconds > 0),
    CHECK (stale_after_seconds > 0),
    CHECK (jsonb_typeof(policy_document)='object')
);
CREATE TABLE core.instrument (
    instrument_id uuid PRIMARY KEY, -- 内部稳定证券身份；不使用会复用或变更的代码当唯一身份
    instrument_type text NOT NULL, -- equity/etf/index等；交易规则按类型选择
    venue_code text NOT NULL, -- 规范交易场所标识，如XSHE/XSHG
    currency char(3) NOT NULL, -- 计价币种；A股CNY，海外数据另存各自币种
    exchange_timezone text NOT NULL, -- IANA时区；交易日历负责会话与日期
    listed_on date, -- 上市日期；未知保留null
    delisted_on date, -- 退市日期；历史证券不可因退市从股票池历史中删除
    created_at timestamptz NOT NULL, -- 内部身份登记时间
    CHECK (delisted_on IS NULL OR listed_on IS NULL OR delisted_on >= listed_on)
);
CREATE TABLE core.instrument_alias (
    alias_id uuid PRIMARY KEY, -- 一次来源代码映射修订的身份
    source_id text NOT NULL REFERENCES core.source(source_id), -- 别名所属来源
    source_symbol text NOT NULL, -- 原始证券代码，保留前导零、大小写和后缀
    instrument_id uuid NOT NULL REFERENCES core.instrument, -- 解析后的稳定身份
    effective_from timestamptz NOT NULL, -- 代码映射的业务生效区间起点
    effective_to timestamptz, -- 生效区间终点，不含该时刻
    available_at timestamptz NOT NULL, -- 系统何时知道这个映射版本
    revision integer NOT NULL, -- 同一映射的修订号
    source_anchor text NOT NULL, -- 支持此映射的原始记录定位
    UNIQUE (source_id,source_symbol,effective_from,revision),
    CHECK (effective_to IS NULL OR effective_to > effective_from),
    CHECK (revision > 0)
);
CREATE INDEX instrument_alias_lookup ON core.instrument_alias(source_id,source_symbol,effective_from DESC,available_at DESC);
CREATE TABLE core.instrument_state (
    instrument_id uuid NOT NULL REFERENCES core.instrument, -- 证券身份
    state_version integer NOT NULL, -- 证券状态修订号
    board_code text NOT NULL, -- 主板/创业板/科创板等规范板块编码
    display_name text NOT NULL, -- 该历史状态的证券简称
    is_risk_warning boolean NOT NULL, -- 当时适用的风险警示状态，不从当前名称倒填历史
    trading_status text NOT NULL, -- active/suspended/delisted等明确状态
    effective_from timestamptz NOT NULL, -- 状态生效时间
    effective_to timestamptz, -- 状态业务终止时间
    available_at timestamptz NOT NULL, -- 此状态可用于系统决策的最早时点
    evidence_ref text NOT NULL, -- 公告或主数据原文引用
    PRIMARY KEY (instrument_id,state_version),
    CHECK (state_version > 0),
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);
CREATE INDEX instrument_state_pit ON core.instrument_state(instrument_id,effective_from DESC,available_at DESC);
CREATE TABLE core.taxonomy (
    taxonomy_id text NOT NULL, -- 分类体系身份，如行业、概念、策略方法；不同提供方不强行合并
    taxonomy_version integer NOT NULL, -- 分类体系版本
    taxonomy_kind text NOT NULL, -- industry/concept/strategy_method/horizon/market_regime等
    owner_source_id text REFERENCES core.source, -- 已知外部分类提供者；本站自定义可为null
    definition jsonb NOT NULL, -- 分类方法、层级与适用范围
    available_at timestamptz NOT NULL, -- 分类版本可得时点
    PRIMARY KEY (taxonomy_id,taxonomy_version),
    CHECK (taxonomy_version > 0)
);
CREATE TABLE core.taxonomy_node (
    taxonomy_id text NOT NULL, -- 所属分类体系
    taxonomy_version integer NOT NULL, -- 所属体系版本
    node_id text NOT NULL, -- 节点稳定编码；中文显示名不是主键
    parent_node_id text, -- 同一体系版本中的父节点，根节点为null
    display_name text NOT NULL, -- 节点名称
    definition jsonb NOT NULL, -- 节点精确定义与纳入标准
    PRIMARY KEY (taxonomy_id,taxonomy_version,node_id),
    FOREIGN KEY (taxonomy_id,taxonomy_version) REFERENCES core.taxonomy,
    FOREIGN KEY (taxonomy_id,taxonomy_version,parent_node_id) REFERENCES core.taxonomy_node,
    CHECK (parent_node_id IS NULL OR parent_node_id <> node_id)
);
CREATE TABLE core.instrument_tag (
    assignment_id uuid PRIMARY KEY, -- 证券与分类节点的一次有时点依据的关联
    instrument_id uuid NOT NULL REFERENCES core.instrument, -- 被分类证券
    taxonomy_id text NOT NULL, -- 分类体系，保留提供方口径
    taxonomy_version integer NOT NULL, -- 分类版本
    node_id text NOT NULL, -- 行业、概念等节点
    effective_from timestamptz NOT NULL, -- 业务归属起点
    effective_to timestamptz, -- 业务归属终点
    available_at timestamptz NOT NULL, -- 当时何时知道该归属
    assertion_kind text NOT NULL, -- observed/inferred/user_declared，事实与推断不混同
    evidence_ref text NOT NULL, -- 归属来源或推断记录
    weight core.finite_decimal, -- 分类有权重时使用；未定义权重则null
    FOREIGN KEY (taxonomy_id,taxonomy_version,node_id) REFERENCES core.taxonomy_node,
    CHECK (effective_to IS NULL OR effective_to > effective_from),
    CHECK (assertion_kind IN ('observed','inferred','user_declared')),
    UNIQUE (instrument_id,taxonomy_id,taxonomy_version,node_id,effective_from,available_at)
);
CREATE INDEX instrument_tag_sector_pit ON core.instrument_tag(taxonomy_id,taxonomy_version,node_id,effective_from,available_at,instrument_id);

CREATE TABLE ingest.batch (
    batch_id uuid PRIMARY KEY, -- 一次可恢复采集/规范化批次身份，重试沿用
    source_id text NOT NULL, -- 采集来源
    endpoint_id text NOT NULL, -- 精确接口
    source_schema_version text NOT NULL, -- 本次原始结构版本
    mapping_version text NOT NULL, -- 本次规范化映射版本
    partition_key text NOT NULL, -- 流分片或查询范围，如证券批/交易日
    cursor_before jsonb, -- 批次前供应商游标；保留不透明结构
    cursor_after jsonb, -- 全部原始对象持久化后可提交的游标
    batch_state text NOT NULL, -- INGESTING/RAW_COMMITTED/NORMALIZED/FAILED
    expected_count bigint, -- 源给出的预期条数；无证据不得猜全量
    received_count bigint NOT NULL DEFAULT 0, -- 已收取并可核对的原始条数
    started_at timestamptz NOT NULL, -- 批次实际开始
    raw_committed_at timestamptz, -- 原始对象及成员清单持久化完成
    failure_code text, -- 失败分类；不以空结果隐藏异常
    FOREIGN KEY (source_id,endpoint_id,source_schema_version,mapping_version) REFERENCES core.source_endpoint,
    CHECK (batch_state IN ('INGESTING','RAW_COMMITTED','NORMALIZED','FAILED')),
    CHECK (expected_count IS NULL OR expected_count >= 0),
    CHECK (received_count >= 0),
    CHECK (batch_state NOT IN ('RAW_COMMITTED','NORMALIZED') OR raw_committed_at IS NOT NULL)
);
CREATE TABLE ingest.raw_object (
    raw_object_id uuid PRIMARY KEY, -- 原始材料不可变身份
    source_id text NOT NULL REFERENCES core.source, -- 原始取得来源
    endpoint_id text NOT NULL, -- 来源接口；同源不同端点独立去重空间
    source_schema_version text NOT NULL, -- 原始结构观测版本
    source_record_key text NOT NULL, -- 供应商ID或明确构造的自然键；保留构造规则版本
    content_hash core.sha256_hex NOT NULL, -- 原始字节内容哈希，修订保留新版本
    storage_uri text NOT NULL, -- 原始对象地址，业务事件只传引用
    mime_type text NOT NULL, -- JSON、HTML、PDF等内容格式
    origin_group text, -- 确认后的原始事实来源；不是转载站数量
    published_at timestamptz, -- 已知真实发布时间；只有日期时保留精度信息不伪造时分秒
    source_time_precision text NOT NULL, -- second/minute/date/unknown等时间精度
    first_seen_at timestamptz NOT NULL, -- 本系统首次收到该原始版本的时间
    ingested_at timestamptz NOT NULL, -- 原始材料持久化时间
    UNIQUE (source_id,endpoint_id,source_schema_version,source_record_key,content_hash),
    CHECK (ingested_at >= first_seen_at)
);
CREATE TABLE ingest.batch_member (
    batch_id uuid NOT NULL REFERENCES ingest.batch, -- 本次批次
    raw_object_id uuid NOT NULL REFERENCES ingest.raw_object, -- 原始对象可在重复拉取批次中被再次引用
    received_at timestamptz NOT NULL, -- 该批次实际观察到这份原文的时间
    PRIMARY KEY (batch_id,raw_object_id)
);
CREATE TABLE ingest.source_cursor (
    source_id text NOT NULL, -- 端点来源
    endpoint_id text NOT NULL, -- 端点身份
    source_schema_version text NOT NULL, -- 原始结构版本
    mapping_version text NOT NULL, -- 映射版本
    partition_key text NOT NULL, -- 游标所属流或范围
    committed_cursor jsonb, -- 已持久化原文对应的最后连续游标，不能用最大已见值替代
    committed_batch_id uuid REFERENCES ingest.batch, -- 推进依据的RAW_COMMITTED批次
    source_event_watermark timestamptz, -- 源事件时间水位；未知保留null
    cursor_version bigint NOT NULL DEFAULT 0, -- 比较更新版本，避免并发采集者覆盖
    updated_at timestamptz NOT NULL, -- 游标成功提交时间
    PRIMARY KEY (source_id,endpoint_id,source_schema_version,mapping_version,partition_key),
    FOREIGN KEY (source_id,endpoint_id,source_schema_version,mapping_version) REFERENCES core.source_endpoint,
    CHECK (cursor_version >= 0)
);
CREATE TABLE ingest.source_health (
    source_id text NOT NULL, -- 来源
    endpoint_id text NOT NULL, -- 健康必须细到端点，不能用供应商首页可达证明数据流健康
    source_schema_version text NOT NULL, -- 监测配置版本
    mapping_version text NOT NULL, -- 适配映射版本
    partition_key text NOT NULL, -- 单流或分片身份
    runtime_state text NOT NULL, -- healthy/degraded/unavailable/auth_required/paused/unconfigured
    freshness_state text NOT NULL, -- current/stale/unknown/not_expected，独立于进程存活
    last_attempt_at timestamptz, -- 最近拉取或订阅尝试
    last_transport_ok_at timestamptz, -- 最近传输成功；HTTP200不自动更新数据新鲜度
    last_new_record_at timestamptz, -- 最近发现新原始记录
    last_canonical_ready_at timestamptz, -- 最近有效规范数据发布
    consecutive_failures integer NOT NULL DEFAULT 0, -- 连续错误次数，用于退避/恢复判断
    next_attempt_at timestamptz, -- 计划重试或轮询时间
    last_error_code text, -- 错误分类，如rate_limited/schema_changed/auth_expired
    updated_at timestamptz NOT NULL, -- 此健康投影刷新时刻
    PRIMARY KEY (source_id,endpoint_id,source_schema_version,mapping_version,partition_key),
    FOREIGN KEY (source_id,endpoint_id,source_schema_version,mapping_version) REFERENCES core.source_endpoint,
    CHECK (runtime_state IN ('healthy','degraded','unavailable','auth_required','paused','unconfigured')),
    CHECK (freshness_state IN ('current','stale','unknown','not_expected')),
    CHECK (consecutive_failures >= 0)
);
CREATE TABLE ingest.observation (
    observation_id uuid PRIMARY KEY, -- 一个规范观测修订的稳定身份
    raw_object_id uuid NOT NULL REFERENCES ingest.raw_object, -- 原始事实可回查
    contract_id text NOT NULL, -- 规范观测合同
    schema_version text NOT NULL, -- 规范合同版本
    mapping_version text NOT NULL, -- 使用的字段映射版本
    entity_type text NOT NULL, -- instrument/issuer/sector/macro/news等领域对象类别
    entity_key text NOT NULL, -- 该领域的稳定身份；实体识别不明时不得假填证券
    natural_key text NOT NULL, -- 对象业务键，用于版本重建与差异定位
    observation_revision integer NOT NULL, -- 追加修订号，不覆盖旧结果
    event_time timestamptz, -- 事实实际发生时刻；原始源未给时可为null
    available_at timestamptz NOT NULL, -- 此规范化版本完成并可供使用的时间
    recorded_at timestamptz NOT NULL, -- 此观测写入事务时间
    quality_status text NOT NULL, -- valid/partial/invalid/disputed；字段级状态在field_quality
    canonical_payload jsonb NOT NULL, -- 已通过对应Schema的有类型载荷，不是任意字段大杂烩
    field_quality jsonb NOT NULL, -- 字段missing/invalid/not_applicable/derived等及来源、转换、精度
    content_hash core.sha256_hex NOT NULL, -- 规范内容摘要，用于同版本复算和对账
    FOREIGN KEY (contract_id,schema_version) REFERENCES core.contract_version,
    UNIQUE (raw_object_id,contract_id,schema_version,mapping_version,natural_key,observation_revision),
    CHECK (observation_revision > 0),
    CHECK (quality_status IN ('valid','partial','invalid','disputed')),
    CHECK (jsonb_typeof(canonical_payload)='object' AND jsonb_typeof(field_quality)='object')
);
CREATE INDEX observation_pit ON ingest.observation(entity_type,entity_key,natural_key,available_at DESC,observation_revision DESC);

CREATE TABLE market.dataset_manifest (
    snapshot_id uuid PRIMARY KEY, -- 平台级不可变数据快照身份，不使用会变化的latest别名
    dataset_id text NOT NULL, -- 数据集名称，如bars_raw_1m或news_events
    manifest_version bigint NOT NULL, -- 该数据集发布版本
    batch_id uuid NOT NULL REFERENCES ingest.batch, -- 本次快照发布对应批次
    manifest_state text NOT NULL, -- INGESTING/COMMITTED_UNPUBLISHED/READY/FAILED
    lake_catalog_id text, -- 分析目录身份；DuckLake快照号只在目录内唯一
    lake_snapshot_id bigint, -- 已提交DuckLake目录快照号
    storage_uri text, -- 文件清单或小数据集对象引用
    content_hash core.sha256_hex, -- 所引用不可变文件清单的摘要
    contract_id text NOT NULL, -- 数据集行合同
    schema_version text NOT NULL, -- 数据集行Schema版本
    row_count bigint, -- 完成校验的行数，0允许表示已确认空集
    available_watermark timestamptz, -- 已处理到的可用时间，不代表所有源都覆盖完整
    coverage_document jsonb NOT NULL, -- 证券、时段、缺失、修订和供应商覆盖清单
    created_at timestamptz NOT NULL, -- 快照登记时间
    ready_at timestamptz, -- READY事务提交时间，发布事件与此状态同事务
    FOREIGN KEY (contract_id,schema_version) REFERENCES core.contract_version,
    UNIQUE (dataset_id,manifest_version),
    UNIQUE (dataset_id,batch_id),
    CHECK (manifest_version > 0),
    CHECK (manifest_state IN ('INGESTING','COMMITTED_UNPUBLISHED','READY','FAILED')),
    CHECK (row_count IS NULL OR row_count >= 0),
    CHECK (manifest_state <> 'READY' OR (ready_at IS NOT NULL AND row_count IS NOT NULL AND content_hash IS NOT NULL AND (storage_uri IS NOT NULL OR (lake_catalog_id IS NOT NULL AND lake_snapshot_id IS NOT NULL))))
);
CREATE TABLE market.dataset_dependency (
    snapshot_id uuid NOT NULL REFERENCES market.dataset_manifest, -- 当前派生数据快照
    input_snapshot_id uuid NOT NULL REFERENCES market.dataset_manifest, -- 被依赖的确切输入快照
    dependency_role text NOT NULL, -- bars/corporate_action/calendar/source_evidence等依赖职责
    PRIMARY KEY(snapshot_id,input_snapshot_id,dependency_role),
    CHECK (snapshot_id <> input_snapshot_id)
);
CREATE TABLE market.bar_projection (
    trade_date date NOT NULL, -- 交易所交易日；也是此热投影分区键
    instrument_id uuid NOT NULL REFERENCES core.instrument, -- 证券身份
    timeframe text NOT NULL, -- 1m/5m/1d/1w/1mo/1y；不能用m同时表示分钟与月
    session_scope text NOT NULL, -- 会话定义版本对应的范围，含不含盘后明确
    bar_open_time timestamptz NOT NULL, -- bar左边界
    bar_close_time timestamptz NOT NULL, -- bar右边界；不等于源实际发布时刻
    snapshot_id uuid NOT NULL REFERENCES market.dataset_manifest, -- 这一投影所依据的已发布不可变数据版本
    currency char(3) NOT NULL, -- 本A股热表当前限定CNY
    open_price_raw core.price_raw, -- 原始未复权开盘价，未知为null
    high_price_raw core.price_raw, -- 原始最高价
    low_price_raw core.price_raw, -- 原始最低价
    close_price_raw core.price_raw, -- 原始收盘或尚未完成bar的最新价
    volume_shares bigint, -- 区间成交股数；不是手，也不是累计快照未差分的值
    turnover_amount_cny core.nonnegative_amount, -- 区间成交额，人民币元
    exchange_reference_price_raw core.price_raw, -- 交易所/源明确的除权参考昨收；与上一bar close不同
    is_final boolean NOT NULL, -- 会话及源完整性政策确认的终结状态
    available_at timestamptz NOT NULL, -- 此bar版本的可用时间
    quality_status text NOT NULL, -- valid/partial/disputed等质量，不混同新鲜度
    observation_id uuid REFERENCES ingest.observation, -- 规范观测来源
    PRIMARY KEY (trade_date,instrument_id,timeframe,session_scope,bar_open_time,snapshot_id),
    CHECK (bar_close_time > bar_open_time),
    CHECK (currency='CNY'),
    CHECK (quality_status IN ('valid','partial','disputed','invalid')),
    CHECK (quality_status <> 'valid' OR (open_price_raw IS NOT NULL AND high_price_raw IS NOT NULL AND low_price_raw IS NOT NULL AND close_price_raw IS NOT NULL AND volume_shares IS NOT NULL AND turnover_amount_cny IS NOT NULL)),
    CHECK (NOT is_final OR available_at >= bar_close_time),
    CHECK (volume_shares IS NULL OR volume_shares >= 0),
    CHECK (high_price_raw IS NULL OR low_price_raw IS NULL OR high_price_raw >= low_price_raw),
    CHECK (open_price_raw IS NULL OR high_price_raw IS NULL OR open_price_raw <= high_price_raw),
    CHECK (close_price_raw IS NULL OR high_price_raw IS NULL OR close_price_raw <= high_price_raw),
    CHECK (open_price_raw IS NULL OR low_price_raw IS NULL OR open_price_raw >= low_price_raw),
    CHECK (close_price_raw IS NULL OR low_price_raw IS NULL OR close_price_raw >= low_price_raw)
) PARTITION BY RANGE (trade_date);
-- Monthly partitions must be created by deployment for the actually retained hot period.
-- No default catch-all partition: an absent partition is an explicit deploy/ingest error, not silent accumulation.
CREATE INDEX bar_projection_lookup ON market.bar_projection(instrument_id,timeframe,session_scope,trade_date,bar_open_time,snapshot_id);
CREATE INDEX bar_projection_date_brin ON market.bar_projection USING brin(trade_date);
CREATE TABLE market.feature_result (
    feature_key_hash core.sha256_hex PRIMARY KEY, -- 包含19维及依赖闭包的规范计算身份摘要
    feature_id text NOT NULL, -- 特征定义身份
    definition_version text NOT NULL, -- 算法定义版本
    full_key jsonb NOT NULL, -- 严格符合v3 FeatureKey的19维字段；保留instrument_ids声明顺序，使用timeframe/adjustment_mode/universe_policy_version，不只存哈希
    snapshot_id uuid NOT NULL REFERENCES market.dataset_manifest, -- 输入数据快照
    decision_as_of timestamptz NOT NULL, -- 历史可见性边界
    availability_policy_version text NOT NULL, -- 可得性过滤政策版本
    result_state text NOT NULL, -- RUNNING/READY/FAILED；失败不登记为空成功结果
    lease_epoch bigint NOT NULL, -- 分布式提交权令牌，旧worker不得覆盖新结果
    result_uri text, -- 不可变Arrow/Parquet或小结果引用
    result_hash core.sha256_hex, -- 结果内容摘要
    computed_at timestamptz, -- 本次成功计算完成时间
    CHECK (result_state IN ('RUNNING','READY','FAILED')),
    CHECK (lease_epoch > 0),
    CHECK (result_state <> 'READY' OR (result_uri IS NOT NULL AND result_hash IS NOT NULL AND computed_at IS NOT NULL))
);

CREATE TABLE strategy.definition (
    strategy_id uuid NOT NULL, -- 方法稳定身份
    strategy_version integer NOT NULL, -- 不可变定义版本；调参另建instance版本
    display_name text NOT NULL, -- 名称用于检索展示，不能代替形式化定义
    role text NOT NULL, -- selection/allocation/entry/holding/execution/exit之一
    method_family text NOT NULL, -- 规则、统计、ML、RL、LLM等可检索方法族
    forecast_target text NOT NULL, -- 预测/行动目标；不同目标不能直接平均分数
    horizon_id text NOT NULL, -- 明确持有/预测期限定义身份
    specification jsonb NOT NULL, -- 类型化输入、输出、条件、状态与实验政策
    definition_hash core.sha256_hex NOT NULL, -- 定义内容摘要
    created_at timestamptz NOT NULL, -- 登记时间
    PRIMARY KEY (strategy_id,strategy_version),
    CHECK (strategy_version > 0),
    CHECK (role IN ('selection','allocation','entry','holding','execution','exit'))
);
CREATE TABLE strategy.instance (
    instance_id uuid PRIMARY KEY, -- 固定定义、参数、股票池、模型与政策后的实验/运行实例
    strategy_id uuid NOT NULL, -- 定义身份
    strategy_version integer NOT NULL, -- 确切定义版本
    parameters jsonb NOT NULL, -- 规范参数，实例发布后不原地修改
    parameter_hash core.sha256_hex NOT NULL, -- 参数摘要
    universe_policy_version text NOT NULL, -- 股票池过滤政策版本
    lifecycle text NOT NULL, -- 研究晋级状态；实现/复现/健康另列
    implementation_status text NOT NULL, -- not_implemented/prototype/implemented
    reproduction_status text NOT NULL, -- not_started/running/reproduced/failed
    health text NOT NULL, -- healthy/degraded/unavailable，不能代替paused
    created_at timestamptz NOT NULL, -- 实例登记时间
    FOREIGN KEY(strategy_id,strategy_version) REFERENCES strategy.definition,
    CHECK (lifecycle IN ('discovered','specified','data_ready','screened','validated','shadow','active','paused','retired')),
    CHECK (implementation_status IN ('not_implemented','prototype','implemented')),
    CHECK (reproduction_status IN ('not_started','running','reproduced','failed')),
    CHECK (health IN ('healthy','degraded','unavailable'))
);
CREATE TABLE strategy.tag_assignment (
    strategy_id uuid NOT NULL, -- 被分类的策略定义
    strategy_version integer NOT NULL, -- 被分类定义的版本
    taxonomy_id text NOT NULL, -- 方法/状态/数据要求等分类体系
    taxonomy_version integer NOT NULL, -- 分类版本
    node_id text NOT NULL, -- 分类节点
    reason_ref text NOT NULL, -- 分类依据或ReasonRecord
    PRIMARY KEY(strategy_id,strategy_version,taxonomy_id,taxonomy_version,node_id),
    FOREIGN KEY(strategy_id,strategy_version) REFERENCES strategy.definition,
    FOREIGN KEY(taxonomy_id,taxonomy_version,node_id) REFERENCES core.taxonomy_node
);
CREATE TABLE strategy.routing_policy (
    routing_policy_id uuid NOT NULL, -- 场景路由政策身份
    routing_policy_version integer NOT NULL, -- 版本化路由规则
    market_regime_contract text NOT NULL, -- 输入市场状态定义版本
    eligible_roles text[] NOT NULL, -- 允许参与的职责集合，应用层验证与role枚举一致
    routing_ast jsonb NOT NULL, -- 数据覆盖、市场状态、期限、预算等类型条件
    calibration_manifest_ref text NOT NULL, -- 权重/阈值校准数据及训练窗口引用
    available_at timestamptz NOT NULL, -- 可用于在线选择的时刻
    PRIMARY KEY(routing_policy_id,routing_policy_version),
    CHECK (routing_policy_version > 0)
);

CREATE TABLE decision.release (
    release_id uuid PRIMARY KEY, -- 一次对外一致发布的决策资料包
    release_state text NOT NULL, -- BUILDING/READY/FAILED/SUPERSEDED
    decision_as_of timestamptz NOT NULL, -- 全包共同的可见性截止时点
    availability_policy_version text NOT NULL, -- 全包历史可用性政策
    universe_policy_version text NOT NULL, -- 候选股票池定义
    market_rules_version text NOT NULL, -- 适用交易规则版本
    required_components text[] NOT NULL, -- 完整发布必需的组件名，不能靠页面自行猜依赖
    release_hash core.sha256_hex, -- 所有组件引用与版本的摘要
    created_at timestamptz NOT NULL, -- 开始构建时间
    published_at timestamptz, -- 完整READY后统一对外发布的时间
    CHECK (release_state IN ('BUILDING','READY','FAILED','SUPERSEDED')),
    CHECK (release_state NOT IN ('READY','SUPERSEDED') OR (published_at IS NOT NULL AND release_hash IS NOT NULL)),
    CHECK (published_at IS NULL OR published_at >= created_at)
);
CREATE TABLE decision.release_component (
    release_id uuid NOT NULL REFERENCES decision.release, -- 所属决策资料包
    component_key text NOT NULL, -- 如market_state/ranking/plans/bars/evidence/sector_membership
    component_state text NOT NULL, -- READY/MISSING/FAILED/STALE，由发布政策评估是否允许
    artifact_ref text NOT NULL, -- 不可变对象引用或明确缺失对象记录
    artifact_hash core.sha256_hex, -- 有效对象摘要
    snapshot_id uuid REFERENCES market.dataset_manifest, -- 组件依赖的数据快照，非数据类组件允许null
    input_version_hash core.sha256_hex NOT NULL, -- 输入闭包版本摘要
    computed_at timestamptz, -- 组件计算完成时间
    valid_until timestamptz, -- 组件有效期，非普适TTL
    PRIMARY KEY(release_id,component_key),
    CHECK (component_state IN ('READY','MISSING','FAILED','STALE')),
    CHECK (component_state <> 'READY' OR (artifact_hash IS NOT NULL AND computed_at IS NOT NULL))
);
CREATE TABLE decision.trade_plan (
    plan_id uuid NOT NULL, -- 计划稳定身份
    plan_version integer NOT NULL, -- 计划版本
    instrument_id uuid NOT NULL REFERENCES core.instrument, -- 目标证券
    account_id uuid, -- 若针对账户，引用execution.account；外键由集成迁移添加
    state text NOT NULL, -- 沿用v1/v2正式TradePlan状态
    plan_payload jsonb NOT NULL, -- 已通过TradePlan Schema的条件、期限、价格区间、依赖等
    based_on_release_id uuid NOT NULL REFERENCES decision.release, -- 编译输入资料包
    expected_account_version bigint, -- 编译时账户版本；实际订单触发时仍需再次验证
    available_at timestamptz NOT NULL, -- 正式计划可用时间
    PRIMARY KEY(plan_id,plan_version),
    CHECK (plan_version > 0),
    CHECK (state IN ('watching','actionable','holding','superseded','expired')),
    CHECK (expected_account_version IS NULL OR expected_account_version >= 0),
    CHECK ((account_id IS NULL) = (expected_account_version IS NULL))
);
CREATE TABLE decision.ranking_run (
    ranking_id uuid PRIMARY KEY, -- 一次不可变排序结果
    release_id uuid NOT NULL REFERENCES decision.release, -- 与个股工作台共用的发布包
    ranking_scope text NOT NULL, -- market/sector/sector_overview/watchlist/account，sector_overview排序对象是板块而非证券
    scope_key text NOT NULL, -- 市场、板块、关注池或账户具体身份
    taxonomy_id text, -- 板块榜使用的分类体系
    taxonomy_version integer, -- 板块分类版本
    node_id text, -- 板块节点
    horizon_id text NOT NULL, -- 当前比较期限
    forecast_target text NOT NULL, -- 排名目标，不能混合短线做T与长期价值分数
    ranking_policy_version text NOT NULL, -- 排序校准、成本与并列处理政策
    membership_snapshot_id uuid REFERENCES market.dataset_manifest, -- 当时板块成分，不使用今日成分回填历史
    created_at timestamptz NOT NULL, -- 排名生成完成时间
    FOREIGN KEY(taxonomy_id,taxonomy_version) REFERENCES core.taxonomy,
    FOREIGN KEY(taxonomy_id,taxonomy_version,node_id) REFERENCES core.taxonomy_node,
    CHECK (ranking_scope IN ('market','sector','sector_overview','watchlist','account')),
    CHECK (ranking_scope <> 'sector' OR (taxonomy_id IS NOT NULL AND taxonomy_version IS NOT NULL AND node_id IS NOT NULL AND membership_snapshot_id IS NOT NULL)),
    CHECK (ranking_scope <> 'sector_overview' OR (taxonomy_id IS NOT NULL AND taxonomy_version IS NOT NULL AND node_id IS NULL AND membership_snapshot_id IS NOT NULL))
);
CREATE TABLE decision.ranking_entry (
    ranking_id uuid NOT NULL REFERENCES decision.ranking_run, -- 所属同目标同期限排序
    instrument_id uuid NOT NULL REFERENCES core.instrument, -- 候选证券
    alpha_rank integer NOT NULL, -- 研究机会顺序
    action_rank integer, -- 满足可行动条件的顺序，未可行动为null
    relative_rank_score core.finite_decimal NOT NULL, -- 相对排名分，不宣称上涨概率
    calibrated_probability core.finite_decimal, -- 有明确事件与校准依据才填写概率
    action_status text NOT NULL, -- watching/actionable/blocked/missing_data等状态
    explanation_ref text NOT NULL, -- 证据、策略贡献与排名变化理由
    plan_id uuid, -- 关联正式个股计划
    plan_version integer, -- 明确计划版本，不默认最新
    PRIMARY KEY(ranking_id,instrument_id),
    UNIQUE(ranking_id,alpha_rank),
    UNIQUE(ranking_id,action_rank),
    FOREIGN KEY(plan_id,plan_version) REFERENCES decision.trade_plan,
    CHECK (alpha_rank > 0 AND (action_rank IS NULL OR action_rank > 0)),
    CHECK (calibrated_probability IS NULL OR calibrated_probability BETWEEN 0 AND 1),
    CHECK ((plan_id IS NULL) = (plan_version IS NULL))
);
CREATE TABLE decision.workspace_projection (
    instrument_id uuid NOT NULL REFERENCES core.instrument, -- 个股工作台证券
    release_id uuid NOT NULL REFERENCES decision.release, -- 全页固定的一致资料包
    view_kind text NOT NULL, -- public_research/account_holding等投影视图类别
    scope_key text NOT NULL, -- public或账户/用户作用域，防止共享错误账户投影
    projection_version integer NOT NULL, -- 投影算法版本
    projection_payload jsonb NOT NULL, -- 图表引用、证据卡、计划引用与质量信息，不复制巨大行情矩阵
    projection_hash core.sha256_hex NOT NULL, -- 当前投影内容摘要/ETag依据
    generated_at timestamptz NOT NULL, -- 投影构建完成时间
    PRIMARY KEY(instrument_id,release_id,view_kind,scope_key,projection_version),
    CHECK (projection_version > 0)
);

CREATE TABLE ops.outbox (
    event_id uuid PRIMARY KEY, -- 业务事件唯一身份；重发沿用，不依靠短期消息去重窗口
    event_type text NOT NULL, -- 小写点分名称；subject由此和schema major唯一生成
    schema_version text NOT NULL, -- 事件载荷合同版本
    aggregate_type text NOT NULL, -- 账户、计划、快照、发布包等聚合类别
    aggregate_id text NOT NULL, -- 聚合身份，作用域内稳定
    aggregate_version bigint NOT NULL, -- 聚合内单调版本；允许0表示开户/引导事件，消费者检测后续缺口
    event_ordinal integer NOT NULL DEFAULT 0, -- 同聚合版本内多事件的明确次序
    occurred_at timestamptz NOT NULL, -- 业务事实发生时间
    recorded_at timestamptz NOT NULL, -- 业务事务落库时间
    producer_version text NOT NULL, -- 生产者实现版本
    payload jsonb NOT NULL, -- 小载荷或内容引用；不搬运整个DataFrame
    payload_hash core.sha256_hex NOT NULL, -- 同ID不同内容必须报冲突
    published_at timestamptz, -- 收到发布确认后登记；未登记的事件可重复发送
    publication_attempts integer NOT NULL DEFAULT 0, -- 发布尝试次数
    next_attempt_at timestamptz, -- 退避后的重发时间
    UNIQUE(aggregate_type,aggregate_id,aggregate_version,event_ordinal),
    CHECK (aggregate_version >= 0 AND event_ordinal >= 0 AND publication_attempts >= 0)
);
CREATE INDEX outbox_unpublished ON ops.outbox(recorded_at,event_id) WHERE published_at IS NULL;
CREATE TABLE ops.consumer_inbox (
    consumer_id text NOT NULL, -- 逻辑消费者身份；不同模块独立，模块内worker共享
    event_id uuid NOT NULL, -- 消费的业务事件
    payload_hash core.sha256_hex NOT NULL, -- 同ID不同内容的检测依据
    applied_at timestamptz NOT NULL, -- 业务效果与此记录在同一事务成功
    PRIMARY KEY(consumer_id,event_id)
);
CREATE TABLE ops.consumer_checkpoint (
    consumer_id text NOT NULL, -- 逻辑消费者
    stream_name text NOT NULL, -- 流身份
    stream_epoch text NOT NULL, -- 重建流时区分旧序号空间
    contiguous_stream_sequence bigint NOT NULL, -- 连续确认水位，不能用并行处理最大序号替代
    last_effective_at timestamptz, -- 最近真正完成业务效果的事件时间
    oldest_pending_at timestamptz, -- 最老未完成事件时间，用于积压告警
    updated_at timestamptz NOT NULL, -- 检查点持久化时间
    PRIMARY KEY(consumer_id,stream_name,stream_epoch),
    CHECK (contiguous_stream_sequence >= 0)
);
CREATE TABLE ops.reconciliation_result (
    reconciliation_id uuid PRIMARY KEY, -- 一次可追溯的字段/数据集校验
    reconciliation_status text NOT NULL, -- matched/disputed/unverifiable，未对齐不能伪造匹配
    field_path text NOT NULL, -- 核验字段或明确的集合口径
    alignment_policy_version text NOT NULL, -- 证券、时间、币种、单位、会话、复权对齐政策
    tolerance_policy_version text NOT NULL, -- 字段精度对应的容差，不用统一百分比掩盖数量级错误
    chosen_observation_id uuid REFERENCES ingest.observation, -- 选定规范值来源；争议未决可null
    normalization_trace jsonb NOT NULL, -- 各来源值、变换和差异详情
    independent_origin_count integer NOT NULL, -- 独立原始事实源数，不是网址数或模型数
    decided_at timestamptz NOT NULL, -- 此校验结论可用时间
    CHECK (reconciliation_status IN ('matched','disputed','unverifiable')),
    CHECK (independent_origin_count >= 0)
);
CREATE TABLE ops.reconciliation_member (
    reconciliation_id uuid NOT NULL REFERENCES ops.reconciliation_result, -- 校验记录
    observation_id uuid NOT NULL REFERENCES ingest.observation, -- 被比较的原始观测版本
    comparison_role text NOT NULL, -- primary/secondary/revision/derived等
    PRIMARY KEY(reconciliation_id,observation_id)
);
CREATE TABLE ops.data_issue (
    issue_id uuid PRIMARY KEY, -- 数据问题可追踪身份
    batch_id uuid REFERENCES ingest.batch, -- 涉及批次
    raw_object_id uuid REFERENCES ingest.raw_object, -- 原始问题数据
    observation_id uuid REFERENCES ingest.observation, -- 涉及规范观测
    issue_code text NOT NULL, -- missing_required_field/unit_mismatch/schema_drift/duplicate_conflict等
    field_path text, -- 具体字段；集合级问题可null
    severity text NOT NULL, -- 阻止哪个发布范围由政策决定，不一律停全站
    issue_payload jsonb NOT NULL, -- 期待值、实际值、来源、修复依据；不自动编造替代事实
    issue_state text NOT NULL, -- open/acknowledged/resolved/rejected
    created_at timestamptz NOT NULL, -- 问题登记时间
    resolved_at timestamptz, -- 追加修订确认完成的时间
    CHECK (issue_state IN ('open','acknowledged','resolved','rejected'))
);
CREATE TABLE core.api_binding (
    operation_id text PRIMARY KEY, -- OpenAPI唯一操作名，前端生成客户端按此调用
    http_method text NOT NULL, -- HTTP方法
    path_template text NOT NULL, -- 唯一路由模板；不另建同义股票资源
    response_contract_id text NOT NULL, -- 响应规范合同
    response_schema_version text NOT NULL, -- 响应版本
    application_service text NOT NULL, -- 唯一业务服务入口，API不能复制策略计算
    read_model text NOT NULL, -- 读取的投影/账本/分析端口，不意味着一张表等于一个API
    freshness_policy_version text NOT NULL, -- 响应时效、回退和版本一致政策
    FOREIGN KEY(response_contract_id,response_schema_version) REFERENCES core.contract_version,
    UNIQUE(http_method,path_template)
);
CREATE TABLE ui.workspace_layout (
    layout_id uuid PRIMARY KEY, -- 用户保存的个股/多图布局身份
    principal_id uuid NOT NULL, -- 所有者身份，认证域外键由集成迁移添加
    layout_version bigint NOT NULL, -- 乐观并发版本，避免多终端覆盖
    display_name text NOT NULL, -- 用户布局名称
    layout_document jsonb NOT NULL, -- 面板、周期、画线与指标配置，不包含重新计算的市场事实
    created_at timestamptz NOT NULL, -- 初次保存时间
    updated_at timestamptz NOT NULL, -- 最近一次明确保存
    CHECK (layout_version > 0),
    CHECK (jsonb_typeof(layout_document)='object')
);

-- This deferred check catches partial decision publication at commit time.
-- It does not replace the PublishService's version comparison, API authorization or outbox write.
CREATE FUNCTION decision.assert_release_components() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    target_id uuid;
    target_state text;
    required_keys text[];
BEGIN
    IF TG_TABLE_NAME = 'release' THEN
        target_id := NEW.release_id;
    ELSIF TG_OP = 'DELETE' THEN
        target_id := OLD.release_id;
    ELSE
        target_id := NEW.release_id;
    END IF;
    SELECT release_state, required_components INTO target_state, required_keys
      FROM decision.release WHERE release_id = target_id;
    IF NOT FOUND OR target_state NOT IN ('READY','SUPERSEDED') THEN
        RETURN NULL;
    END IF;
    IF coalesce(cardinality(required_keys),0) = 0
       OR cardinality(required_keys) <> (SELECT count(DISTINCT required_key) FROM unnest(required_keys) AS required(required_key)) THEN
        RAISE EXCEPTION 'Release requires a nonempty distinct component list';
    END IF;
    IF EXISTS (
        SELECT 1 FROM unnest(required_keys) AS required(required_key)
        LEFT JOIN decision.release_component c
          ON c.release_id=target_id AND c.component_key=required.required_key
        WHERE c.component_key IS NULL OR c.component_state <> 'READY'
    ) THEN
        RAISE EXCEPTION 'Release % has a missing or unready required component',target_id;
    END IF;
    IF EXISTS (
        SELECT 1 FROM decision.release_component c
        JOIN market.dataset_manifest m ON m.snapshot_id=c.snapshot_id
        WHERE c.release_id=target_id AND c.component_state='READY' AND m.manifest_state<>'READY'
    ) THEN
        RAISE EXCEPTION 'Release % references an unpublished data snapshot',target_id;
    END IF;
    RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER release_complete_at_commit
    AFTER INSERT OR UPDATE ON decision.release
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION decision.assert_release_components();
CREATE CONSTRAINT TRIGGER release_component_complete_at_commit
    AFTER INSERT OR UPDATE OR DELETE ON decision.release_component
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION decision.assert_release_components();

-- Published bundles and their content references remain replayable. Corrections use new IDs.
CREATE FUNCTION decision.guard_published_release() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.release_state IN ('READY','SUPERSEDED') THEN
        IF TG_OP='DELETE' THEN
            RAISE EXCEPTION 'Published release cannot be deleted';
        END IF;
        IF NOT (OLD.release_state='READY' AND NEW.release_state='SUPERSEDED'
                AND (to_jsonb(NEW)-'release_state')=(to_jsonb(OLD)-'release_state')) THEN
            RAISE EXCEPTION 'Published release content is immutable';
        END IF;
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER published_release_immutable
    BEFORE UPDATE OR DELETE ON decision.release FOR EACH ROW
    EXECUTE FUNCTION decision.guard_published_release();

CREATE FUNCTION decision.guard_published_component() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target_id uuid; target_state text;
BEGIN
    IF TG_OP='UPDATE' AND NEW.release_id<>OLD.release_id THEN
        RAISE EXCEPTION 'A component cannot be moved to a different release';
    END IF;
    IF TG_OP='DELETE' THEN target_id:=OLD.release_id; ELSE target_id:=NEW.release_id; END IF;
    -- Serialize this mutation with the PublishService's release row lock.
    SELECT release_state INTO target_state FROM decision.release WHERE release_id=target_id FOR UPDATE;
    IF target_state IN ('READY','SUPERSEDED') THEN
        RAISE EXCEPTION 'Published release components are immutable';
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER published_component_immutable
    BEFORE INSERT OR UPDATE OR DELETE ON decision.release_component FOR EACH ROW
    EXECUTE FUNCTION decision.guard_published_component();

CREATE FUNCTION market.guard_ready_manifest() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.manifest_state='READY' THEN
        RAISE EXCEPTION 'Ready dataset manifest is immutable; create a new snapshot for correction';
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER ready_manifest_immutable
    BEFORE UPDATE OR DELETE ON market.dataset_manifest FOR EACH ROW
    EXECUTE FUNCTION market.guard_ready_manifest();

CREATE FUNCTION market.guard_ready_dependency() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target_id uuid; target_state text;
BEGIN
    IF TG_OP='UPDATE' AND NEW.snapshot_id<>OLD.snapshot_id THEN
        RAISE EXCEPTION 'A dependency cannot be moved to another snapshot';
    END IF;
    IF TG_OP='DELETE' THEN target_id:=OLD.snapshot_id; ELSE target_id:=NEW.snapshot_id; END IF;
    SELECT manifest_state INTO target_state FROM market.dataset_manifest WHERE snapshot_id=target_id FOR UPDATE;
    IF target_state='READY' THEN
        RAISE EXCEPTION 'Ready snapshot dependency closure is immutable';
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER ready_dependency_immutable
    BEFORE INSERT OR UPDATE OR DELETE ON market.dataset_dependency FOR EACH ROW
    EXECUTE FUNCTION market.guard_ready_dependency();

CREATE FUNCTION market.assert_ready_manifest_inputs() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM market.dataset_manifest own
               JOIN market.dataset_dependency d ON d.snapshot_id=own.snapshot_id
               JOIN market.dataset_manifest input ON input.snapshot_id=d.input_snapshot_id
               WHERE own.snapshot_id=NEW.snapshot_id AND own.manifest_state='READY'
                 AND input.manifest_state<>'READY') THEN
        RAISE EXCEPTION 'Ready snapshot depends on an unpublished input';
    END IF;
    RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER manifest_inputs_ready_at_commit
    AFTER INSERT OR UPDATE ON market.dataset_manifest
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION market.assert_ready_manifest_inputs();

-- A formal plan version is an immutable value. A state/content change creates a new version.
CREATE FUNCTION decision.reject_plan_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'TradePlan versions are append-only; append the next version';
END;
$$;
CREATE TRIGGER trade_plan_version_immutable
    BEFORE UPDATE OR DELETE ON decision.trade_plan FOR EACH ROW
    EXECUTE FUNCTION decision.reject_plan_mutation();

CREATE FUNCTION decision.guard_ranking_run() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target_state text; target_release uuid;
BEGIN
    IF TG_OP='UPDATE' AND (NEW.release_id<>OLD.release_id OR NEW.ranking_scope<>OLD.ranking_scope) THEN
        RAISE EXCEPTION 'Ranking release and scope bindings are immutable';
    END IF;
    IF TG_OP='INSERT' THEN target_release:=NEW.release_id; ELSE target_release:=OLD.release_id; END IF;
    SELECT release_state INTO target_state FROM decision.release WHERE release_id=target_release FOR UPDATE;
    IF target_state IN ('READY','SUPERSEDED') THEN
        RAISE EXCEPTION 'Published ranking metadata is immutable';
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER published_ranking_run_immutable
    BEFORE INSERT OR UPDATE OR DELETE ON decision.ranking_run FOR EACH ROW
    EXECUTE FUNCTION decision.guard_ranking_run();

CREATE FUNCTION decision.guard_ranking_entry() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target_id uuid; target_release uuid; target_scope text; target_state text;
        plan_instrument uuid; plan_release uuid;
BEGIN
    IF TG_OP='UPDATE' AND NEW.ranking_id<>OLD.ranking_id THEN
        RAISE EXCEPTION 'A ranking entry cannot be moved to another ranking';
    END IF;
    IF TG_OP='DELETE' THEN target_id:=OLD.ranking_id; ELSE target_id:=NEW.ranking_id; END IF;
    SELECT r.release_id,r.ranking_scope,p.release_state INTO target_release,target_scope,target_state
      FROM decision.ranking_run r JOIN decision.release p ON p.release_id=r.release_id
      WHERE r.ranking_id=target_id FOR UPDATE OF p;
    IF target_state IN ('READY','SUPERSEDED') THEN
        RAISE EXCEPTION 'Published ranking entries are immutable';
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    IF target_scope='sector_overview' THEN
        RAISE EXCEPTION 'Sector overview uses decision.sector_rank_entry, not instrument entries';
    END IF;
    IF NEW.plan_id IS NOT NULL THEN
        SELECT instrument_id,based_on_release_id INTO plan_instrument,plan_release
          FROM decision.trade_plan WHERE plan_id=NEW.plan_id AND plan_version=NEW.plan_version;
        IF NOT FOUND OR plan_instrument<>NEW.instrument_id OR plan_release<>target_release THEN
            RAISE EXCEPTION 'Ranking and TradePlan instrument/release identities differ';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER ranking_entry_identity_and_publication
    BEFORE INSERT OR UPDATE OR DELETE ON decision.ranking_entry FOR EACH ROW
    EXECUTE FUNCTION decision.guard_ranking_entry();
COMMIT;
