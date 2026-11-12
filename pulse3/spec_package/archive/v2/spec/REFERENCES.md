# 参考资料与来源索引

以下索引与正文中的A、T、X编号对应。AP、EP为论文卡，TV等为平台证据，已在相应章节给出原始链接，完整索引在research/source_index.json。A为A股规则与信息来源，T为原有架构与研究方法，X为v2新增共享数据与账户渠道资料。来源文档证明规则或接口说明存在，实际接入状态以catalogs/source_catalog.json为准；数据权限、可用历史与延迟由适配器验收记录确定。规则和接口后续变化以原发布方为准。

审计代码证据使用C编号，与本表独立。代码行号绑定基线提交6f5c57f8df73c58f0340dab6977b95c05d21740c，不指向会继续变化的分支。

## A股规则与信息来源

**A01  正式2026规则 2026-07-06生效及部分此前条文继续暂缓**

[原始文档](https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20260424_10816492.shtml)。用途：正式2026规则 2026-07-06生效及部分此前条文继续暂缓。

**A02  官方说明ST 5%改10% 盘后固定价扩容**

[原始文档](https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20260424_10816474.shtml)。用途：官方说明ST 5%改10% 盘后固定价扩容。

**A03  深证上〔2026〕551号 2026-07-06生效**

[原始文档](https://investor.szse.cn/lawrules/rule/trade/t20260424_620190.html)。用途：深证上〔2026〕551号 2026-07-06生效。

**A04  正式深交所2026交易规则全文 时段/数量/价格/T+1/盘后**

[原始文档](https://docs.static.szse.cn/www/lawrules/rule/trade/current/W020260424690713155663.pdf)。用途：正式深交所2026交易规则全文 时段/数量/价格/T+1/盘后。

**A05  现行交易时段 主板10% 创业板20%**

[原始文档](https://www.szse.cn/English/services/trading/tradOverview/)。用途：现行交易时段 主板10% 创业板20%。

**A06  沪市A股盘后固定价15:05–15:30**

[原始文档](https://one.sse.com.cn/onething/gptz/)。用途：沪市A股盘后固定价15:05–15:30。

**A07  北向实时买卖及总成交不再提供 季度持股披露**

[原始文档](https://www.hkex.com.hk/News/Market-Communications/2024/2404122news?sc_lang=en)。用途：北向实时买卖及总成交不再提供 季度持股披露。

**A08  2024披露调整实际生效日期**

[原始文档](https://www.hkex.com.hk/-/media/HKEX-Market/News/News-Release/2025/250227news/250227_FY-Results_e.pdf)。用途：2024披露调整实际生效日期。

**A09  L2包含逐笔/订单关联等 专网协议**

[原始文档](https://www.sseinfo.com/services/assortment/level2/)。用途：L2包含逐笔/订单关联等 专网协议。

**A10  news来源 最大1500条 需独立新闻权限**

[原始文档](https://tushare.pro/document/2?doc_id=143)。用途：news来源 最大1500条 需独立新闻权限。

**A11  积分与独立权限分离 分钟/公告/新闻非同一权限**

[原始文档](https://tushare.pro/document/1?doc_id=290)。用途：积分与独立权限分离 分钟/公告/新闻非同一权限。

**A12  2026发布日程 数据库更新与新闻首发有时差**

[原始文档](https://www.stats.gov.cn/sj/fbrc/bnxxfb/)。用途：2026发布日程 数据库更新与新闻首发有时差。

**A13  官方货币政策/讲话/统计RSS**

[原始文档](https://www.federalreserve.gov/feeds/feeds.htm)。用途：官方货币政策/讲话/统计RSS。

**A14  ALFRED当时已知版本 realtime_start/end**

[原始文档](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html)。用途：ALFRED当时已知版本 realtime_start/end。

**A15  FRED API key和参数**

[原始文档](https://fred.stlouisfed.org/docs/api/fred/series.html)。用途：FRED API key和参数。

**A16  BLS官方HTTPS GET/POST JSON数据API**

[原始文档](https://www.bls.gov/audience/developers.htm)。用途：BLS官方HTTPS GET/POST JSON数据API。

**A17  BLS API限制和API可用延迟说明**

[原始文档](https://www.bls.gov/bls/api_features.htm)。用途：BLS API限制和API可用延迟说明。

**A18  EIA APIv2 免费key及接口结构**

[原始文档](https://www.eia.gov/opendata/documentation.php)。用途：EIA APIv2 免费key及接口结构。

**A19  EIA库存报告API可能滞后至两小时 秒级首发另有服务**

[原始文档](https://www.eia.gov/opendata/faqs.php)。用途：EIA库存报告API可能滞后至两小时 秒级首发另有服务。

**A20  GDELT2.0 65语言 15分钟更新**

[原始文档](https://gdeltproject.org/data.html)。用途：GDELT2.0 65语言 15分钟更新。

**A21  YouTube官方API配额机制**

[原始文档](https://developers.google.com/youtube/data/getting-started)。用途：YouTube官方API配额机制。

**A22  Reddit数据API须批准/认证 商业需对应权限**

[原始文档](https://support.reddithelp.com/hc/en-us/articles/14945211791892-Developer-Platform-Accessing-Reddit-Data)。用途：Reddit数据API须批准/认证 商业需对应权限。

**A23  Reddit开发者批准和使用条件**

[原始文档](https://support.reddithelp.com/hc/en-us/articles/16471395473812-Moderation-Bots-Tooling)。用途：Reddit开发者批准和使用条件。

**A24  巨潮公开数据/API/商城入口**

[原始文档](https://www.cninfo.com.cn/new/index?lang=en)。用途：巨潮公开数据/API/商城入口。

**A25  抖音视频搜索接口与scope**

[原始文档](https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/douyin-search-capability/)。用途：抖音视频搜索接口与scope。

**A26  抖音搜索能力页面仍标实验不对外开放**

[原始文档](https://partner.open-douyin.com/docs/resource/zh-CN/dop/ability/search-management/item-search)。用途：抖音搜索能力页面仍标实验不对外开放。

**A27  中金所股指期货历史细则背景材料**

[原始文档](https://www.cffex.com.cn/u/cms/www/202003/27165505w3j5.pdf)。用途：2020年历史背景；不得作为2026年当前适用规则，接入时须核验现行规则与交易日历。

**A28  财政部政策发布和政策解读栏目**

[原始文档](https://www.mof.gov.cn/zhengwuxinxi/)。用途：财政部政策发布和政策解读栏目。

**A29  发改委政策通知栏目**

[原始文档](https://www.ndrc.gov.cn/xxgk/zcfb/tz/wap_index.html)。用途：发改委政策通知栏目。

**A30  BLS发布日历 美国东部时区 可订阅ICS**

[原始文档](https://www.bls.gov/schedule/2026/11_sched_list.htm)。用途：BLS发布日历 美国东部时区 可订阅ICS。

## 架构与研究方法

**T01  Temporal Workflow Definition**

[原始文档](https://docs.temporal.io/workflow-definition)。用途：Workflow须确定性；外部API/LLM/DB交互应放Activity。

**T02  Temporal Activity Definition**

[原始文档](https://docs.temporal.io/activity-definition)。用途：Activity会重试、多次执行；幂等由业务端实现。

**T03  NATS JetStream**

[原始文档](https://docs.nats.io/concepts/jetstream)。用途：持久化、回放、至少一次交付、消费者独立进度。

**T04  Polars Lazy API**

[原始文档](https://docs.pola.rs/user-guide/lazy/using/)。用途：整体查询优化、流式与schema检查。

**T05  DuckDB Parquet**

[原始文档](https://duckdb.org/docs/stable/data/parquet/overview)。用途：直接读写Parquet，按列/过滤扫描。

**T06  TradingView Product Comparison**

[原始文档](https://www.tradingview.com/charting-library-docs/latest/getting_started/product-comparison/)。用途：Lightweight Charts不自带行情或指标。

**T07  Qlib PIT**

[原始文档](https://qlib.readthedocs.io/en/latest/advanced/PIT.html)。用途：修订财报版本的时点读取；当前主要季/年因子。

**T08  Bailey & López de Prado DSR paper**

[原始文档](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf)。用途：多重试验选择偏差与非正态修正。

**T09  Bailey et al PBO paper**

[原始文档](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)。用途：回测过拟合概率与CSCV方法。

**T10  Microsoft Qlib**

[原始文档](https://github.com/microsoft/qlib)。用途：ML量化研究管线与模型/数据/回测组织。

**T11  Microsoft RD-Agent**

[原始文档](https://github.com/microsoft/RD-Agent)。用途：因子/模型提出、实现与联合研究流程。

**T12  vectorbt**

[原始文档](https://vectorbt.dev/)。用途：数组化批量策略与参数研究；注意免费和PRO能力差别。

**T13  LEAN Trade Fills**

[原始文档](https://www.quantconnect.com/docs/v2/writing-algorithms/reality-modeling/trade-fills/key-concepts)。用途：fill与slippage职责；默认完全成交、部分成交自定义。

**T14  NautilusTrader Backtesting**

[原始文档](https://nautilustrader.io/docs/latest/concepts/backtesting/)。用途：回测共享核心组件，流数据驱动。

**T15  NautilusTrader Fill Models**

[原始文档](https://nautilustrader.io/docs/latest/concepts/backtesting/fill-models/)。用途：book type、触价成交概率、滑点、流动性消耗约束。

**T16  FinRL Training Testing Trading**

[原始文档](https://github.com/AI4Finance-Foundation/FinRL/blob/master/docs/source/finrl_meta/overview.rst)。用途：DRL训练、测试、交易的阶段分离。

**T17  MCP Tools Specification**

[原始文档](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/server/tools.mdx)。用途：工具定义有inputSchema/outputSchema，稳定工具顺序有利缓存。

**T18  A2A Specification**

[原始文档](https://github.com/a2aproject/A2A/blob/main/docs/specification.md)。用途：Agent Card、任务、跨独立Agent系统交互。

**T19  QuantConnect Research Guide**

[原始文档](https://www.quantconnect.com/docs/v2/writing-algorithms/key-concepts/research-guide)。用途：财务发布日期、复权价格、动态股票池与生存偏差。

**T20  PostgreSQL Transaction Isolation**

[原始文档](https://www.postgresql.org/docs/current/transaction-iso.html)。用途：事务隔离含义；作为交易与版本状态库的基础。

## v2共享数据与账户渠道资料

**X01  NATS JetStream事件持久化与独立消费者**

[原始文档](https://docs.nats.io/concepts/jetstream)。用途：区分Core NATS与JetStream；独立消费游标、至少一次投递和回放。。

**X02  DuckLake官方FAQ**

[原始文档](https://ducklake.select/faq)。用途：2026年1.0生产可用声明、多实例共享Parquet和SQL目录；业务表不提供主外键约束。。

**X03  DuckLake事务与快照**

[原始文档](https://ducklake.select/docs/stable/duckdb/advanced_features/transactions)。用途：数据及目录快照隔离、多表事务；不能推导与业务数据库天然跨库原子。。

**X04  上交所2026交易规则发布与正式附件**

[原始文档](https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20260424_10816492.shtml)。用途：2026年7月6日生效，按品种与规则版本处理交易。。

**X05  深交所2026交易规则正式PDF**

[原始文档](https://docs.static.szse.cn/www/lawrules/rule/trade/current/W020260424690713155663.pdf)。用途：交收前卖出限制及回转例外、会话和盘后交易规则。。

**X06  恒生A股通服务常见问题**

[原始文档](https://www.hangseng.com/zh-cn/investment/securities-services/stock-connect/faq-stock-connect/)。用途：该服务未交收卖款不可再买A股；只用于特定渠道，不能推广全部账户。。

**X07  东方财富证券投资者教育基础知识**

[原始文档](https://edu.18.cn/CaiFuShuYuan/JiChuZhiShi)。用途：官方索引片段说明国内卖出款可交易使用与可取不同；正文打开遇重定向，接入账户时仍须核对券商政策。。
