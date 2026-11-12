# 04 · 数据平台

> 本卷回答：数据存在哪、表怎么设计、字段怎么命名、不同来源的字段不一致时怎么补齐和纠正、怎么校验、信息源中断时怎么保证系统仍在刷新。
> 结论先行：现在的问题不是数据库性能不够，而是没有统一的数据模型。先统一模型，物理层按 03 卷第 11 节的触发条件再升级。

## 1. 对“要不要升级数据库”的回答

| 问题 | 回答 | 依据 |
|---|---|---|
| 数据量是否逼近现有存储的极限 | 没有。全 A 日线 3 年约 400 万行（几百 MB）；60 秒全市场快照每个交易日约 60 MB Parquet，一年约 15 GB。真正占空间的是原始响应（每个交易日约 300 MB），它需要转存策略而不是更强的数据库（第 12 节） | 服务器实测（项目记忆：首个录制日原始压缩文件 299.9 MB、Parquet 60.2 MB） |
| 现在真正的瓶颈 | 11 类存储、4 种代码格式、4 种成交额单位、4 种时间表示、写入时零校验、5 处“当前快照冒充历史” | 审计 A2 |
| 现在就上服务型数据库是否有收益 | 没有。2 核 8 GB 的机器上它只增加常驻内存和运维，不解决上面任何一条 | 03 卷第 5.3 节 |
| 应该做什么 | 一套规范数据模型（本卷）+ Parquet 数据湖 + 一个嵌入式状态库；物理层藏在端口后面 | — |
| 什么时候升级 | 03 卷第 11 节列出的可观测触发条件出现时 | — |

## 2. 存储布局

### 2.1 两个存储，各司其职

| 存储 | 放什么 | 为什么 |
|---|---|---|
| 数据湖（Parquet 文件 + 目录） | 一切“事实”：行情、快照、事件、消息、特征、信号、打分。行数多、只追加、按日期分区 | 不可变文件天然支持一个写者多个读者、时点快照、便宜的备份与同步 |
| 状态库（SQLite，WAL，单文件） | 一切“状态”：目录与清单、事件日志、作业运行、证券主数据、策略登记与联赛、订单与账本、工作台、告警、大模型调用账本 | 需要事务与唯一性约束；行数少；与事件发件箱同库保证原子性 |

内存里不保存任何不能由这两处重建的东西。现状里个股抽屉的日线、形态面板、异动带、情绪曲线都在进程内存里，重启即丢；筛选条件与布局在浏览器本地存储里，换设备即丢（审计 A4）。

### 2.2 数据湖的三个区

```text
<lake>/
├── raw/<source_id>/<yyyy-mm-dd>/…        原始响应（压缩、带长度前缀、含被拒页面），永不修改
├── canon/<dataset>/<分区>/<seq>-<id>.parquet   规范表：统一命名、统一单位、通过校验
├── derived/<dataset>/<分区>/…            由 canon 计算得到：特征、信号、打分、面板
├── inbox/                                 PC 推来的产物，等待校验与登记
└── quarantine/<dataset>/…                 未通过校验的行与原因
```

- `raw` 是一切的重放起点：解析器升级后可以从 raw 重建 canon。现状股吧抓取失败时不保存原始响应，失败原因无从查起（审计 A5）。
- `canon` 的每个文件只属于一个数据集的一个分区，写入是“先写临时文件、原子改名、再在目录登记”。未登记的文件对读者不可见，所以进程中途被杀不会留下半截数据（现状盘中录制、新闻、公司事件直接写最终文件名，审计 A1）。
- `derived` 的每个文件都记录它依赖的输入快照号，可以整体丢弃重算。

### 2.3 目录（清单）

```text
dataset(name, schema_version, key_cols, partition_cols, time_semantics, freshness_slo, owner)
snapshot(seq 自增, created_at, job_run_id, note)
data_file(file_id, dataset, partition, path, rows, key_min, key_max, schema_version,
          content_hash, snapshot_seq, status[active|retired], job_run_id, source_ids)
```

- 读取某数据集 = 取“快照号 ≤ N 的活动文件”，按主键去重，保留修订号最大的一行。这沿用了现有 Parquet 湖“读时去重、最新写入覆盖”的已验证做法。
- 压缩合并 = 在一个事务里登记合并后的新文件并把旧文件标为退役；退役文件延迟删除，正在读的进程不受影响。这同样是现有做法，只是把“退役清单 JSON”换成了事务（现状的退役清单是读改写且没有跨进程锁，审计 A2）。
- `snapshot_seq` 就是全系统的数据版本号。所有缓存、所有研究结果、所有报告都记录它。
- 这套目录与 DuckLake 的概念一一对应（快照、数据文件、模式版本）。M0 阶段做一次对比试验：若 DuckLake 在这台机器上通过多进程读写测试，就用它实现同一个端口。

## 3. 规范命名与单位

现状同一概念的写法：代码有 `600000`、`sh600000`、`CN.SH.600000`、`1.600000` 四种；成交量有“股”和“手”，同一个字段按来源不同单位不同；成交额有元、万元、亿元和估算值四种；时间有带时区、不带时区的北京时间、不带时区的本机时间、字符串四种（审计 A2 口径表）。

### 3.1 标识符

| 概念 | 规范写法 | 例子 | 说明 |
|---|---|---|---|
| 证券 | `instrument_id` = 代码.交易所 | `600000.SH`、`000001.SZ`、`920001.BJ` | 国内通行写法（Tushare、Wind 同式），可读、可扩展到港美股 |
| 指数 | 同上，后缀区分编制方 | `000001.SH`、`399001.SZ`、`000985.CSI`、`801010.SI` | 与股票是否撞号不靠前缀猜，靠主数据里的 `asset_type` |
| 期货、外汇、商品、外盘 | 同上 | `IF2612.CFE`、`USDCNH.FX`、`XAU.CMD`、`SPX.GI` | — |
| 板块 | `sector_id` = 体系.代码 | `SW2021.801080`、`KPL.xxxx`、`ZT.<原因规范名>` | 体系前缀让申万、题材、涨停原因并存而不混 |
| 席位 | `seat_id` | 内部整数 | 名称与别名在实体表里，带有效期 |
| 其他 | `source_id`、`strategy_id`、`event_id`、`cluster_id`、`account_id` | — | 全部是字符串主键 |

板别（主板、创业板、科创板、北交所）和涨跌幅比例只从主数据和规则表读取。现状有 4 份互不一致的代码前缀判断，其中一份漏掉了北交所 92 号段（审计 A2）。任何业务代码里出现对代码前缀的判断，都视为缺陷。

每个数据源的代码写法只出现在该源的适配器和 `ref.instrument_alias` 表里。

### 3.2 字段命名规则

1. 英文小写蛇形，不用缩写（公认缩写除外：id、pct、bp、mv、ts）。
2. **单位写进后缀**，没有后缀的数值字段不允许进入规范表：

| 后缀 | 含义 | 例子 |
|---|---|---|
| `_yuan` | 人民币元 | `amount_yuan`、`float_mv_yuan`、`seal_amount_yuan` |
| `_shares` | 股 | `volume_shares`、`float_shares`、`bid1_shares` |
| `_pct` | 百分点（9.98 表示 9.98%） | `change_pct`、`turnover_pct` |
| `_ratio` | 比例（0–1） | `seal_to_amount_ratio`、`participation_ratio` |
| `_bp` | 基点 | `slippage_bp` |
| `_count`、`_days`、`_sessions` | 计数、自然日、交易日 | `open_board_count`、`hold_sessions` |

3. 价格字段是唯一的例外：`open`、`high`、`low`、`close`、`prev_close`、`limit_up`、`limit_down`、`bid1`…… 单位固定为元，类型是定点小数（3 位），永远是**不复权的原始价**。复权价只出现在派生面板里，并且必须带 `_adj` 后缀。
4. 时间字段：交易日用 `trade_date`（日期类型，北京时间的交易日）；时刻一律以 `_at` 结尾，存 UTC 微秒时间戳，界面显示北京时间。
5. 布尔字段以 `is_`、`has_` 开头。枚举值用大写蛇形。
6. “手”“万元”“亿元”“千元”不允许出现在规范表里。适配器负责换算，换算系数写在适配器的映射表里并有测试。
7. 估算值与观测值不能共用一个字段。例如没有真实成交额时，估算值写入 `amount_est_yuan`，`amount_yuan` 保持为空。现状把“典型价 × 成交量”的估算值当成交额用，同一天龙虎榜里的成交额却是真实值（审计 A2）。

### 3.3 时间语义字段（每张规范表按需携带）

| 字段 | 含义 | 谁需要 |
|---|---|---|
| `event_time` | 事情发生的时刻 | 行情、事件、消息 |
| `published_at` | 第一次公开的时刻（来源自报，可能不可靠） | 公告、消息、龙虎榜 |
| `received_at` | 我们的采集器实际收到的时刻（按批记录） | 一切前向数据 |
| `available_at` | 在当前知识模式下最早可以被决策使用的时刻 | 一切数据；`DataView` 用它过滤 |
| `recorded_at` | 写入本地存储的时刻 | 一切数据 |
| `valid_from` / `valid_to` | 这个属性在现实里生效的区间 | 证券属性、板块成分、规则、费率 |
| `revision` / `supersedes` | 内容版本与它替代的版本 | 可被更正的数据 |
| `time_precision` | 时间精度（毫秒、秒、分钟、日、未知） | 不允许声称比来源更高的精度 |

约束：`available_at ≥ received_at ≥ published_at`（已在现有内核契约里验证过）。盘中快照的 `received_at` 记录每一批的实际到达时间；现状用“轮询开始时间加固定 2 或 5 秒”，而一轮全量轮询最长约 40 秒，回放时后几批数据会被提前当成已知（审计 A5）。

### 3.4 每行的来历字段

规范表的每一行都带 `source_id`（这行的观测值来自哪个源）、`recorded_at`、`quality_flags`（例如 `FILLED_FROM_SECONDARY`、`CROSS_SOURCE_MISMATCH`、`LATE_REVISION`）。字段级的来源差异（比如价格来自 A 源、涨停价来自 B 源）用 `quality_flags` 加隔离区记录，不拆成多行。

## 4. 数据集目录

命名：`域.名称`。域有 `ref`（参考数据）、`mkt`（行情）、`evt`（结构化事件）、`intel`（消息与社区）、`feat`/`sig`/`score`（派生）。交易与研究状态在状态库里（07、06 卷）。

### 4.1 参考数据

| 数据集 | 主键 | 内容 | 要解决的现状问题 |
|---|---|---|---|
| `ref.instrument` | `instrument_id`, `valid_from` | 名称、交易所、板别、资产类型、上市日、退市日、风险警示状态；**双时态** | 现状证券表只以代码为键，每日快照被去重丢弃，历史 ST、历史名称无从得知（01 卷 #15）；仓库只含现存股票（幸存者偏差） |
| `ref.instrument_alias` | `source_id`, `source_symbol` | 各源代码写法到 `instrument_id` 的映射，含北交所新旧代码 | 4 种代码格式无转换函数 |
| `ref.calendar` | `exchange`, `trade_date` | 是否交易日、各时段起止 | 现状有三套交易日来源，日历文件缺失时静默退回“只看工作日” |
| `ref.rule` | `rule_id`, `board`, `valid_from` | 涨跌幅、整手、最小报价单位、价格笼子、时段、盘后定价等，**按生效日版本化**，带来源与核实级别 | 主板 ST 涨跌幅 2026-07-06 起由 5% 改为 10%（上交所官方原文，09 卷），现状代码仍有写死 5% 的兜底 |
| `ref.fee` | `fee_id`, `valid_from` | 印花税、过户费、经手费、证管费；佣金方案 | 实验脚本里散落着 0.1%–0.45% 的各种成本常数（审计 A3） |
| `ref.sector` / `ref.sector_member` | `sector_id` / (`sector_id`, `instrument_id`, `valid_from`) | 行业、概念、题材、涨停原因及其成分，**带生效区间与可用时刻** | 免费源只给概念的当前成分，用它回测题材跟随有严重前视（经验卡，E026） |
| `ref.adj_factor` | `instrument_id`, `trade_date` | 累计复权因子 | 现状仓库存的是复权后的价格，除权时要整段重拉，且受单次 1023 根的上限影响（01 卷 #14） |
| `ref.corp_action` | `instrument_id`, `ex_date`, `kind` | 现金分红、送转、配股：公告日、登记日、除权除息日、派息日 | 现有全部回测引擎都没有分红入账（审计 A3） |
| `ref.share_capital` | `instrument_id`, `trade_date` | 总股本、流通股本、自由流通股本 | 现状用最新股本倒推历史市值，自由流通市值是流通市值乘常数 0.62 |
| `ref.seat` / `ref.seat_alias` | `seat_id`, `valid_from` | 营业部实体、更名与搬迁、类型（机构池、陆股通池、量化候选、普通） | 席位更名（如券商合并）后画像断裂 |

### 4.2 行情

| 数据集 | 主键 | 内容 |
|---|---|---|
| `mkt.bar_1d` | `instrument_id`, `trade_date` | 原始开高低收、`prev_close`、`volume_shares`、`amount_yuan`、`limit_up`、`limit_down`、`is_suspended`。停牌日有显式的一行 |
| `mkt.bar_1m` | `instrument_id`, `bar_at` | 分钟线；注明来自源还是由快照合成（`is_synthetic`） |
| `mkt.snapshot_l1` | `instrument_id`, `quote_at` | 五档盘口快照：全市场 60 秒、热点 10 秒、竞价 5 秒；带时段标签（集合竞价、连续竞价、收盘竞价、盘后定价） |
| `mkt.limit_pool` | `trade_date`, `instrument_id` | 涨停、炸板、跌停池：首封与末封时刻、封单额、炸板次数、连板数、涨停原因 |
| `mkt.index_bar_1d` / `mkt.fund_bar_1d` | 同日线 | 指数、ETF |
| `mkt.xasset_quote` | `instrument_id`, `quote_at` | 股指期货、A50、恒指期货、汇率、黄金、原油、外盘指数 |

### 4.3 结构化事件

| 数据集 | 内容 | 可用时刻的取法 |
|---|---|---|
| `evt.lhb_stock` / `evt.lhb_seat` | 龙虎榜个股与席位明细（上榜理由、买卖方向、金额、榜内名次） | 我们收到的时刻；当天 18:00 前不拉取（已验证规则） |
| `evt.announcement` | 公告标题、类别、链接、主体 | 巨潮或交易所的发布时刻与我们的收到时刻取晚者 |
| `evt.calendar` | 宏观数据、会议、财报、解禁、交割日、指数调样；含预期值与实际值 | 首见时间取导入时间，不倒填（已验证规则） |
| `evt.unlock`、`evt.suspension`、`evt.margin`、`evt.block_trade`、`evt.holder_change`、`evt.buyback`、`evt.survey`、`evt.etf_share` | 解禁、停复牌、两融、大宗、增减持、回购、机构调研、ETF 份额 | 各自的公告或披露时刻 |

### 4.4 消息与社区（详见 05 卷）

| 数据集 | 内容 |
|---|---|
| `intel.item` | 规范化后的单条消息：来源、等级、标题、正文、链接、内容哈希、修订链 |
| `intel.cluster` | 事件簇：首发源、跟发源、确认等级（已确认、待确认、传闻、已辟谣） |
| `intel.event` | 结构化事件：类型、主体、方向、强度、新颖度、依据句、映射到的证券与板块、已反映程度 |
| `intel.heat` | 社区热度采样：平台、帖量、阅读、人气排名、关注数 |
| `intel.opinion` | 抽取出的观点与到期后的对账结果 |

### 4.5 派生数据

| 数据集 | 内容 |
|---|---|
| `feat.panel_1d` | 日频特征面板：复权收益、技术指标、涨跌停特征、时点市值、行业、上市天数等 |
| `feat.intraday` | 盘中特征：分时均价偏离、封单变化、量比、广度 |
| `sig.signal` | 四个面的信号统一表（05 卷） |
| `sig.market_state` | 市场状态五轴：指数、广度、风格、情绪、题材 |
| `sig.sector_strength` | 板块强度与升温分 |
| `score.strategy` / `score.ensemble` | 各策略打分与融合分；按首次计算时的版本存档（as-of），之后不改写 |

`score.*` 按“第一次算出来的值”只追加存档。依据：现有前向流程发现服务器数据修订会改写历史选股，所以单独保留了 as-of 分数表作为前向记录（项目记忆）。新系统把这个做法定为所有打分数据集的默认。

## 5. 数据源与适配器

### 5.1 适配器契约

每个源一个文件，只声明、不写盘：

```text
SourceAdapter
  source_id, tier                # 行情类：PRIMARY / SECONDARY / VERIFIER；消息类：T0–T3
  hosts: [{host, rate, burst}]   # 进入跨进程令牌桶
  feeds: [dataset…]              # 它供给哪些数据集
  schedule                       # 轮询节奏或触发条件
  fetch(cursor) -> RawBatch      # 只做网络请求，返回原始字节与新的游标
  parse(raw) -> rows             # 纯函数：原始字节 → 规范行（含单位换算、代码映射）
  mapping                        # 声明式：源字段 → 规范字段、换算系数、枚举映射
  fixtures                       # 录制的原始样本，作为解析器的标准测试
```

框架负责：限速、超时、重试与熔断、写 raw、调用 `parse`、校验、写 canon、登记、发事件、更新源健康。适配器作者不接触磁盘和数据库。

### 5.2 各源口径差异与换算（适配器必须实现）

| 概念 | Tushare | 东财（经 akshare） | 新浪 | 腾讯行情 | 规范值 |
|---|---|---|---|---|---|
| 代码 | `600000.SH` | `600000` | `sh600000` | `sh600000` | `600000.SH` |
| 成交量 | 手 | 手 | 股 | 手（本仓库代码实证：乘 100 入库） | 股 |
| 成交额 | 千元 | 元 | 元 | 万元（本仓库代码实证） | 元 |
| 复权 | 不复权价 + `adj_factor` | 前后复权成品 | 原始价 + 因子文件 | 前复权为**减法复权**，会放大老数据涨跌幅（本项目实测，经验卡） | 原始价 + 比例因子 |
| 昨收 | `pre_close` 为除权后的参考价 | — | — | 字段自带 | `prev_close` = 交易所公布的参考价 |
| 涨跌停价 | `stk_limit` | 涨停池 | — | 字段 47/48 | `limit_up` / `limit_down`；多源比对 |
| 停牌日 | 无行 | 无行 | 无行 | 价格冻结 | 显式一行，`is_suspended = true` |

来源：Tushare 与 akshare 的字段单位来自官方文档（09 卷）；腾讯与新浪的单位来自本仓库现有解析代码与实测。其余源在接入时逐项核对并补进本表。

### 5.3 推荐的源组合

| 数据域 | 主源 | 备源与校验源 | 理由 |
|---|---|---|---|
| 日线、复权因子、涨跌停价、逐日 ST、逐日股本、退市股 | Tushare Pro | 腾讯收盘快照（当日先到）、新浪日线（校验） | 个人预算内唯一能同时补上退市股、历史 ST、逐日股本、涨跌停价的来源；5000 积分档约 500 元/年（官方价格页，2026-11-10 查询）。**付费项，见 10 卷 D03** |
| 盘中快照 | 腾讯 `qt.gtimg.cn` | 新浪（服务器 IP 被拒，仅 PC 可用） | 现有录制已稳定运行；被拒即停规则沿用 |
| 分钟线 | 由快照合成 | 源分钟线只取热点与持仓 | 腾讯分钟主机每天在取到约 150–300 只股票后被拦截；分段请求的对策尚无结论（台账 E090） |
| 涨停、炸板、跌停池 | 东财涨停池接口（现有录制在用） | Tushare 涨跌停列表（2020 年起，用于历史回补）；由快照与涨停价自算（校验首封时刻、炸板次数、连板数） | 晋级率、涨停溢价、梯队、角色标签都依赖它；节假日接口会回放上一交易日的数据，日期不是当天不入库 |
| 龙虎榜 | 东财数据中心 | Tushare `top_list` / `top_inst` | 双源比对金额与席位名 |
| 行业成分（时点正确） | Tushare 申万成分（带进出日期） | 巨潮逐股行业历史 | 行业口径可无前视回测 |
| 题材成分（时点正确） | 自接入日起每日自存档；Tushare 开盘啦题材成分（按交易日存档，5000 积分档可得，起始日官方未写明） | — | 同花顺概念成分的进出日期官方标注不可用，不能做时点回测。带日期的涨停原因需要 8000 积分档，东财与通达信的按日成分需要 6000 积分档；在 10 卷 D03 的默认档位下，`mkt.limit_pool.reason_text` 取东财涨停池自带的行业字段，题材级的涨停原因为空（09 卷） |
| 公告 | 巨潮 | 交易所官网 | — |
| 快讯 | 财联社、东财、同花顺 | 金十、华尔街见闻等 | 05 卷 |
| 外盘与期货 | 腾讯、中金所文件 | PC 端补抓 | 服务器实测可达性（项目记忆） |

## 6. 校验规范

写入规范表之前必须依次通过四级校验。任何一级的规则都与数据集模型写在一起，由同一份定义生成。

| 级别 | 检查什么 | 例子 | 不通过时 |
|---|---|---|---|
| V1 模式 | 列、类型、可空性、枚举、主键唯一 | `volume_shares` 为非负整数；`instrument_id` 在主数据里存在 | 整批拒绝，作业失败 |
| V2 行内规则 | 单行的逻辑一致性 | `low ≤ open, close ≤ high`；价格为正且对齐最小报价单位；`limit_up` 等于按规则表由 `prev_close` 算出的值（容差半个最小单位）；涨跌幅不超过涨跌停加容差 | 该行进隔离区，其余继续 |
| V3 时序与截面 | 跨行、跨日 | 交易日 × 证券的覆盖矩阵；重复日；复权因子变化日必须对应 `prev_close` 的跳变；相邻日 `prev_close` 与前一日 `close` 的关系；截面覆盖率 | 覆盖率低于 98% 触发只补缺失代码的回补；仍低于 95% 则该分区标记为拒绝，下游短路 |
| V4 跨源 | 主源与校验源比对 | 价格差不超过 0.01 元；成交量相对差不超过 0.5%；成交额不超过 1%；龙虎榜收盘价与日线一致；真实成交额与估算值量级一致 | 主源值入库并打标记，两个值都写进隔离区供裁决；同一字段连续多日不一致时告警 |

校验结果汇总成分区级的质量分（缺失率、隔离率、跨源差异率、迟到修订数），写入目录，数据健康页直接展示。

现状写入时没有任何模式校验，唯一的结构约束是写入前的类型强转（审计 A2）。

## 7. 多源裁决、补齐与纠正

1. **每个字段有明确的主源顺序**，写在数据集模型里。主源缺失时用备源补，并打上 `FILLED_FROM_SECONDARY`。
2. **不静默替换**。备源只用于补缺和比对；主源有值而与校验源冲突时，先入主源值、标记、进隔离区，由规则或人工裁决后以“修订”的形式更正。
3. **三源以上时用独立多数**，但“独立”有定义：转载同一上游的两个源只算一个。
4. **观测值不补**。价格、成交量、成交额、封单这类观测值缺失就是空。能做的只有“派生”：用公开的公式从别的观测值算出一个带 `_est` 后缀的估算字段。
5. **更正是新版本**。晚到的更正写成新修订并指向旧修订；下游通过 `DataView` 看到的是决策时刻可用的那个版本。
6. **除权除息的检测**用复权因子的变化，不用价格跳变的阈值。现状用“昨收相差超过 0.5%”检测，会漏掉股息率低于 0.5% 的分红（01 卷 #16）。
7. **单位换算只发生在适配器里一次**，换算后的行不带任何源单位信息流向下游。

## 8. 时点正确与双时态

- 证券属性、板块成分、规则、费率都是“在某段时间内成立”的事实，用 `valid_from` / `valid_to` 表达；同时带 `available_at`，表示我们何时知道它。
- 每日快照型的源（名称、ST、市值、题材成分）一律**追加存档**，再由作业折叠成区间表。永远不用今天的快照回填历史。
- 历史上拿不到时点数据的属性，在面板里就是空，并在数据健康页明确列出“自哪一天起才有时点数据”。例如带日期的题材成分只能从接入日或 2024 年底的接口起点开始（09 卷）。
- `DataView` 默认只返回 `available_at ≤ decision_time` 的行。需要事后视角的复盘功能必须显式声明“事后模式”，界面上有固定标记。

## 9. 复权

- 湖里只存原始价和每日累计复权因子。
- 读时复权：`close_adj(t) = close(t) × factor(t) / factor(基准日)`。前复权取基准日为视图的最后一天，后复权取上市首日。
- 收益率直接用“原始价之比 × 因子之比”计算，与基准日无关，所以新除权不需要改写任何历史行。
- 成交与记账永远用原始价（07 卷）。
- 标准用例：高分红股票在无除权的季度里，原始价与复权价之比必须恒定（本项目曾用它发现减法复权的问题）。

## 10. 采集健康、轮询与故障切换

目标：任何一个信息源中断或变慢，系统都知道、都记录、都尽量由别的源顶上，并在界面上如实显示。

### 10.1 源注册表与健康状态

```text
source(source_id, tier, feeds, expected_rate_by_session, freshness_slo)
source_health(source_id, state, last_ok_at, last_new_item_at, consecutive_failures,
              last_error_kind, breaker_until, items_per_min, staleness_seconds)
```

状态机：

```text
HEALTHY ──连续失败或陈旧──▶ DEGRADED ──达到阈值──▶ OPEN（熔断）
   ▲                                               │ 冷却结束
   └────────探测成功──────── HALF_OPEN（只放一次探测）◀┘
```

- **健康按有效新增判断**，不按返回 200 或返回条数判断（经验库红线）。
- **两种冷却**：被拒（403、拦截页、跳转）进入长冷却并上报；超时与握手失败进入短冷却后自动探测。现状连续 3 次失败（含超时）就停用一整天且不恢复，同花顺每天 09:00 的握手超时因此让它全天失明（审计 A5）。
- **已知窗口**：某源在固定时段的固定故障可以配置为“已知窗口”，窗口内降低告警级别但照常记录与探测。
- **主备联动**：同一数据需求至少登记两个源。主源熔断时，备源自动提高轮询频率；主源保持探测，恢复后自动切回。
- **游标持久化**：每个源保存最新条目的标识与时间。重启后从游标续抓；增量抓取命中游标时跳过而不是中断（经验库红线）。游标不连续则记为缺口。
- **缺口回补**：缺口检测生成回补任务，用该源的翻页或备源补同一时间窗。现状每源每次只拉一页（20–30 条），夜间与周末不录，重启后只剩最后一页（审计 A5）。
- **全天候**：采集器不受交易日限制。政策、外盘、公司公告大量出现在收盘后与周末。
- **请求预算**：按主机设跨进程令牌桶。现状限速器只在单进程内有效，实时快照与盘中录制在同一 IP 上各自全市场拉报价（审计 A5）。
- **原始响应必存**，包括被拒页面。

### 10.2 新鲜度目标（第一版，可调）

| 数据集 | 时段 | 目标 |
|---|---|---|
| `mkt.snapshot_l1`（全市场） | 连续竞价 | 最新一帧不超过 120 秒 |
| `mkt.snapshot_l1`（竞价与热点） | 集合竞价、热点池 | 不超过 15 秒 |
| `intel.item`（快讯类，合并各源） | 07:00–23:00 | 不超过 5 分钟无新条目即降级；夜间 30 分钟 |
| `mkt.bar_1d` | 交易日 | 16:00 前当日分区登记；主源核对在 19:00 前完成 |
| `evt.lhb_*` | 交易日 | 18:30 前 |
| `evt.announcement` | 全天 | 15 分钟 |
| `evt.calendar` | 每日 | 08:00 前同步未来 90 天 |

`/health` 与数据健康页由这张表和 `source_health` 计算得出。任一目标被突破都会产生 `source.health` 事件并推送。

### 10.3 看门狗

采集器、作业执行器、接口服务各自每 10 秒写一次心跳。看门狗发现心跳超时即重启对应服务，并记录一次事故。第三方库调用一律放在可被终止的子进程或带硬超时的线程里；现状有一次东财调用挂死，使站点数据冻结两天（项目记忆，审计 A1、A5）。

## 11. 从旧数据迁移

新系统不读旧代码，但继承旧数据。迁移是一组一次性作业，每个都有对账标准。

| 旧资产 | 处理 | 对账 |
|---|---|---|
| 真实日线仓（新浪按比例前复权） | 不直接导入价格；以新主源重建原始价与因子，把旧仓当校验源 | 逐日收益率与旧仓一致（容差 1 个最小报价单位引起的误差） |
| 盘中录制的原始文件（2026-11-02 起） | 用新适配器从原始文件重新解析进 `mkt.snapshot_l1` | 行数、每只每分钟的最后价与旧 Parquet 一致 |
| 7×24 新闻原始文件 | 重新解析进 `intel.item`，保留最早的收到时刻 | 条数与修订链一致 |
| 龙虎榜、板块、公司事件、财经日历 | 导入并与新主源比对 | 金额、席位名、日期一致率 |
| 实验台账 92 条、因子登记簿 | 导入策略登记库的“实验”与“因子”表，编号保留 | 条数、编号、判定一致 |
| 冻结主书与影子账本的前向记录、as-of 分数表 | 作为只读的“旧制前向记录”导入；旧流程继续每日运行直到新系统通过并行对账 | 新旧两套在并行期内逐日持仓一致或差异可解释 |

并行期内旧系统是记录方，新系统是影子。切换的条件写在 08 卷。

## 12. 容量

| 项 | 估算 | 服务器上保留 |
|---|---|---|
| 规范日线（含全部退市股，10 年） | 约 1,300 万行，1 GB 以内 | 全部 |
| 盘中快照（规范表） | 每个交易日约 60 MB，约 15 GB/年 | 全市场 60 秒快照保留最近 6 个月（约 7.5 GB），更早的转存；热点、持仓与涨停池相关股票的快照长期保留 |
| 原始响应 | 每个交易日约 300 MB，约 75 GB/年 | 最近 20 个交易日（约 6 GB）；更早的在确认已转存且校验和一致后才从服务器删除 |
| 派生数据（面板、信号、打分） | 约 3 GB/年 | 最近 2 年 |
| 状态库与备份 | 小于 2 GB，加 7 份每日备份 | 全部 |
| 合计常驻 | 约 20–25 GB | — |

服务器 C 盘约 50 GB 可用，所以这张表是硬约束，不是建议：

- 转存目标默认是 PC（PC 上线时按清单增量拉取并回报校验和）。PC 长期不在线时不会删除未转存的原始响应，磁盘会持续增长，因此看门狗在可用空间低于 15 GB 时告警、低于 8 GB 时暂停非必需的原始响应保存（先停社区页面的原始响应，行情与消息的不停）。
- 转存策略与水位告警是 M1 的验收项（08 卷）。加一块数据盘可以去掉对 PC 的依赖，见 10 卷 D13。

## 13. 附：核心数据集的字段（契约第一版的起点）

M0 冻结契约时以下表为起点。类型缩写：`str` 字符串，`date` 日期，`ts` UTC 微秒时间戳，`px` 定点小数（12 位，3 位小数，单位元），`amt` 定点小数（20 位，2 位小数，单位元），`i64` 整数，`f64` 浮点，`bool` 布尔，`enum` 枚举，`list` 列表。除主键外都可为空，空表示未知。每张表另带 `source_id`、`recorded_at`、`quality_flags`（第 3.4 节），下表不重复列出。

### 13.1 数据湖

| 数据集 | 字段 |
|---|---|
| `ref.instrument` | `instrument_id` str，`valid_from` date，`valid_to` date，`available_at` ts，`name` str，`exchange` enum（SH、SZ、BJ、CSI、SI、CFE、FX、CMD、GI），`asset_type` enum（STOCK、INDEX、FUND、CBOND、FUTURE、FX、COMMODITY），`board` enum（MAIN、CHINEXT、STAR、BSE），`list_date` date，`delist_date` date，`risk_status` enum（NORMAL、ST、STAR_ST、DELISTING） |
| `ref.sector_member` | `sector_id` str，`instrument_id` str，`valid_from` date，`valid_to` date，`available_at` ts，`role_hint` str |
| `ref.adj_factor` | `instrument_id` str，`trade_date` date，`adj_factor` f64，`available_at` ts |
| `ref.share_capital` | `instrument_id` str，`trade_date` date，`total_shares` i64，`float_shares` i64，`free_float_shares` i64，`available_at` ts |
| `mkt.bar_1d` | `instrument_id` str，`trade_date` date，`open` `high` `low` `close` `prev_close` px，`volume_shares` i64，`amount_yuan` amt，`limit_up` `limit_down` px，`is_suspended` bool，`available_at` ts，`revision` i64 |
| `mkt.snapshot_l1` | `instrument_id` str，`quote_at` ts（源的报价时刻），`received_at` ts（该批实际到达时刻），`session` enum（PRE_OPEN、OPEN_AUCTION、CONTINUOUS_AM、CONTINUOUS_PM、CLOSE_AUCTION、AFTER_HOURS_FIXED、CLOSED），`poll_kind` enum（FULL、HOT、AUCTION、AFTER_HOURS），`last` `open` `high` `low` `prev_close` `limit_up` `limit_down` px，`volume_shares` i64（当日累计），`amount_yuan` amt（当日累计），`bid1`…`bid5` `ask1`…`ask5` px，`bid1_shares`…`ask5_shares` i64，`after_hours_volume_shares` i64，`after_hours_amount_yuan` amt |
| `mkt.limit_pool` | `trade_date` date，`instrument_id` str，`pool` enum（LIMIT_UP、BROKEN、LIMIT_DOWN），`first_seal_at` `last_seal_at` ts，`open_board_count` i64，`seal_amount_yuan` amt，`limit_streak` i64，`reason_text` str，`reason_sector_id` str，`available_at` ts |
| `evt.lhb_seat` | `trade_date` date，`instrument_id` str，`reason_code` str，`side` enum（BUY、SELL），`rank` i64，`seat_id` str，`seat_name_raw` str，`buy_amount_yuan` `sell_amount_yuan` `net_amount_yuan` amt，`received_at` `available_at` ts |
| `evt.announcement` | `announcement_id` str，`instrument_id` str，`title` str，`category` str，`url` str，`source_published_at` `received_at` `available_at` ts，`revision` i64 |
| `evt.calendar` | `calendar_event_id` str，`kind` str，`title` str，`scheduled_at` ts，`time_precision` enum，`expected_value` `actual_value` `previous_value` f64，`unit` str，`first_seen_at` `available_at` ts，`revision` i64 |
| `intel.item` | `source_id` str，`item_id` str，`revision` i64，`supersedes` str，`tier` enum（T0–T3），`title` str，`body` str，`url` str，`content_hash` str，`source_published_at` `received_at` `available_at` ts，`raw_ref` str |
| `intel.cluster` | `cluster_id` str，`version` i64，`first_item_ref` str，`first_seen_at` ts，`confirm_level` enum（CONFIRMED、PENDING、RUMOR、REFUTED），`confirm_rule` enum（T0_HIT、T1_PLUS_T2、TWO_T2），`confirmed_at` ts，`source_ids` list，`independent_source_count` i64，`is_recombined` bool，`update_of` str |
| `intel.event` | `event_id` str，`cluster_id` str，`event_type` str，`direction` `scope` `tier` `novelty` `uncertainty` enum，`verifiable` bool，`entities` list（`subject_id`、`role`、`confidence`），`evidence` list（`item_ref`、`quote`、`char_start`、`char_end`），`classifier` enum（RULE、SMALL_MODEL、LLM），`model_id` `prompt_version` str，`pre_abnormal_return_pct` f64，`volume_multiple` f64，`reflected_level` enum（LOW、MID、HIGH、UNKNOWN），`available_at` ts |
| `intel.heat` | `instrument_id` str，`platform` str，`sampled_at` ts，`post_count` `read_count` `follower_count` i64，`hot_rank` i64，`heat_z20` f64 |
| `sig.signal` | 见 05 卷第 1 节 |
| `score.strategy` | `strategy_id` str，`strategy_version` str，`decision_date` date，`instrument_id` str，`score` f64，`rank_ratio` f64，`snapshot_seq` i64，`computed_at` ts |

### 13.2 状态库

| 表 | 主键 | 用途 |
|---|---|---|
| `dataset`、`snapshot`、`data_file` | 见第 2.3 节 | 目录 |
| `event_log`、`consumer_offset` | `seq`；（`consumer`, `topic`） | 内部消息 |
| `job`、`job_run` | `job_id`；（`job_id`, `partition`, `input_snapshot`） | 作业与运行账本 |
| `source`、`source_health` | `source_id` | 采集健康 |
| `strategy_spec` | （`strategy_id`, `version`） | 策略规格，不可变 |
| `strategy_status_history` | （`strategy_id`, `version`, `changed_at`） | 状态迁移，只追加 |
| `experiment` | `experiment_id`（沿用 E 编号） | 实验台账，只追加，带替代关系 |
| `league_round`、`league_match`、`league_standing` | （`season`, `round`）等 | 联赛 |
| `account` | `account_id` | 纸面、用户、旧制前向账户 |
| `action_intent`、`order_plan`、`execution_report` | 各自标识；`order_plan` 另有唯一的幂等键 | 意图、订单、成交事件 |
| `ledger_entry` | `entry_id` | 复式分录，只追加 |
| `position_lot` | （`account_id`, `instrument_id`, `lot_id`） | 持仓批次 |
| `portfolio_snapshot` | （`account_id`, `trade_date`） | 每日权益与持仓快照 |
| `opportunity` | `opportunity_id` | 机会账本 |
| `picks_report` | （`trade_date`, `board`, `version`） | 推荐榜与回避榜，不可变 |
| `trade_plan` | （`plan_id`, `version`） | 交易计划及其版本 |
| `alert_rule`、`alert` | 各自标识 | 告警规则与告警实例（含送达状态与对账） |
| `watchlist`、`layout`、`chart_state`、`drawing`、`note`、`saved_screen`、`review_entry` | 各自标识 | 工作区 |
| `seat`、`seat_alias`、`seat_profile` | `seat_id` 等 | 席位实体与画像 |
| `llm_call` | `call_id` | 大模型调用账本 |

### 13.3 状态库里与交易、计划、工作区相关的表的字段

| 表 | 字段 |
|---|---|
| `action_intent` | `intent_id`，`strategy_id`，`strategy_version`，`account_id`，`instrument_id`，`action`（INCREASE_LONG、DECREASE_LONG、EXIT_LONG、T_BUY_FIRST、T_SELL_FIRST），`decision_time`，`generated_at`，`expires_at`，`price_policy`（类型与限价），`quantity_policy`，`trigger_rule_id`，`invalidation_rule_ids`，`snapshot_seq`，`reason` |
| `order_plan` | `client_order_id`，`idempotency_key`（唯一），`intent_id`，`account_id`，`instrument_id`，`side`，`quantity`，`price_policy`，`session`，`time_in_force`，`created_at` |
| `execution_report` | `report_id`，`client_order_id`，`status`，`order_quantity`，`filled_quantity`（累计），`last_fill_quantity`，`last_fill_price`，`fees_yuan`（累计，分项另记分录），`scenario`（OPTIMISTIC、BASE、PESSIMISTIC，实盘为空），`event_time`，`reason_code` |
| `ledger_entry` | `entry_id`，`account_id`，`trade_date`，`posted_at`，`debit_account`，`credit_account`，`amount_yuan`，`instrument_id`，`lot_id`，`quantity`，`ref_type` 与 `ref_id`（指向成交、分红、送转、费用） |
| `position_lot` | `account_id`，`instrument_id`，`lot_id`，`acquired_date`，`quantity`，`remaining_quantity`，`cost_yuan`，`strategy_id` |
| `opportunity` | `opportunity_id`，`intent_id`，`account_id`，`instrument_id`，`strategy_id`，`decision_time`，`status`（TRIGGERED、ORDERED、PARTIALLY_FILLED、FILLED、UNFILLED、NOT_ORDERED），`scenario`，`target_quantity`，`filled_quantity`，`avg_fill_price`，`reference_price`，`unfilled_reason`（LIMIT_UP_QUEUE、ONE_WORD_BOARD、PRICE_BOUND、EXPIRED、NO_CASH、NOT_SELLABLE、SUSPENDED、BUDGET、REJECTED），`closed_at` |
| `picks_report` | `trade_date`，`board`（SHORT、SWING、LONG、AVOID），`version`，`snapshot_seq`，`published_at`，`items`（名次、`instrument_id`、融合分、投票策略及各自的状态、风险标记、计划引用），`supersedes` |
| `trade_plan` | `plan_id`，`version`，`instrument_id`，`account_id`，`origin`（SYSTEM、USER_EDIT），`status`（ACTIVE、INVALIDATED、COMPLETED），`thesis_strategy_ids`，`entry`，`sizing`，`stop`，`exits`，`hold_sessions`，`exit_at`，`tactic_id`，`risk_flags`，`invalidation_rule_ids`，`change_reason`（含触发它的事件引用），`snapshot_seq`，`created_at` |
| `alert_rule` | `rule_id`，`owner`，`kind`，`subject_id`，`condition`（结构化条件树），`channels`，`enabled` |
| `alert` | `alert_id`，`rule_id` 或系统来源，`kind`，`level`，`subject_id`，`evidence_refs`，`price_move_before_pct`，`raised_at`，`delivery_status`，`reconciled_result` |
| `watchlist` | `watchlist_id`，`name`，`group`，`items`（`instrument_id`、加入时间、备注） |
| `layout`、`chart_state` | `layout_id`，`name`，窗格配置；`chart_state_id`，`instrument_id`，周期，复权方式，指标及参数列表 |
| `drawing` | `drawing_id`，`instrument_id`，周期，类型，锚点（时间与价格），样式，绑定的 `rule_id` |
| `note`、`review_entry` | 标识，`instrument_id`（可空），`trade_date` 或时间点，正文，引用 |
| `saved_screen` | `screen_id`，`name`，条件树或策略引用，排序 |

工作区各表都带 `owner`、`updated_at`、`version`（乐观并发）。
