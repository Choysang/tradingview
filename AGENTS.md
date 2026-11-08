# A-Share Pulse（极光 A 股实时量块看板）— 2026 Agent-Native 全栈工程交接总纲

> **状态（2026-10-30，Pulse 3.0）**：本文件第 2 节的目录拓扑、第 3 节的契约代码属于**历史设计草案**，与当前实现不一致。当前权威文档是：
> - `docs/ARCHITECTURE.md`：模块边界、数据流、因子口径、回测/形态/因子检验模型、前端架构、Agent 能力面、扩展方式、已知限制。
> - `contracts/schema.py`：唯一契约（Pydantic v2），前端类型由 `contracts/generate_types.py` 生成到 `frontend/src/core/contract.ts`。
> - `docs/EXECUTION.md`：各轮已完成项、验证证据与遗留缺口。
> - `docs/research/2026-product-review.md`（第一轮 14 个项目）与 `docs/research/2026-10-30-review-v3.md`（第二轮 11 个开源项目 + 4 个商业产品）。
> - Agent 接入：`GET /llms.txt`、`GET /api/v1/agent/manifest`、MCP `http://127.0.0.1:8000/mcp`，三者由 `backend/app/agent/capabilities.py` 同源生成。
>
> 启动方式统一为 `python scripts/pulse.py install|check|test|build|dev`，不要按下方 Stage 0–4 的旧流程重建目录。

> **使用方法（历史交接指南，已被上面的状态说明取代）**：
> 1. 在本地新建一个空目录 `ashare-pulse`，将本文档保存为根目录下的 `AGENTS.md`。
> 2. 启动你的主控编码智能体（如 Claude Code、Cursor Agent、Cline、Devin 或多智能体编排器），直接向其发送以下启动指令：
>    `请完整阅读根目录下的 AGENTS.md，严格按照文档定义的目录拓扑、数据契约（SSOT）、纯函数因子舱机制与 UI 规范，按 Stage 0 至 Stage 4 顺序全自动完成前后端代码编写、联调与自测验证。不要向我反复确认中间细节，遇到环境依赖直接通过 uv 和 pnpm 自动安装，确保最终可以一键启动并看到实时跳动的看板数据。`

---

## 1. 系统定位与核心原则（System Mandates）

你（AI 编码智能体）正在从零构建一套 **2026 教科书级、零历史包袱的 A 股盘中实时连板梯队与连阳多因子筛选 Web 系统（A-Share Pulse）**。在编写任何代码前，必须严格遵守以下工程铁律：

0. **默认接入 Team Agent 多智能体编排（Sub-Agent Standing Directive）**：
   本项目已全面接入 `team-agent`（配置真相源：`.team-agent/profile.json`，子代理注册池：`.agents/agents/team-*.md`）。智能体在收到任何代码检索、调研、实现或审查任务时，**默认自动关联此配置并启动子代理协同作业，无需用户反复手动提醒**：
   - **大面积代码定位**：自动派发 `team-explorer`（只读，`Gemini 3.8 Flash`，思考强度 `High`）；
   - **外网文档与生态调研**：自动派发 `team-researcher`（只读，`Gemini 3.8 Flash`，思考强度 `High`）；
   - **具体代码实现与修复**：自动派发 `team-executor`（`Gemini 3.8 Flash`，思考强度 `High`，限定独占文件白名单）；
   - **核心代码交付前**：**默认必须自动派发 `team-reviewer`（只读，`Claude Sonnet 4.6 (Thinking)`，思考强度 `Thinking`）进行零历史冷启动 Diff 审查**；
   - **疑难死锁连续 2 轮失败**：自动熔断唤醒 `team-expertadvisor`（只读，`Claude Opus 4.6 (Thinking)`，思考强度 `Thinking`）；
   - **直通车特权**：仅当已知修改 $\le 2$ 个文件且无需调研时，允许主 Agent 直接完成；
   - **真实档位与指纹核验**：严格执行各模型真实支持的思考档位（Gemini 仅用 High/Medium/Low，Claude 仅用 Thinking），并在子代理结果末尾核验 `[Agent-Fingerprint]`。
1. **前后端 100% 契约驱动（Contract-First SSOT）**：
   根目录下的 `contracts/schema.py` 是全系统的唯一真理源（Single Source of Truth）。后端 API 的输入输出、Polars 内存表的列名、前端 TypeScript 的接口定义必须 100% 与 `contracts/schema.py` 保持一致，严禁在前端或后端私自捏造未在契约中声明的字段名。
2. **全链路列式内存计算（Zero-Copy Arrow Pipeline）**：
   **严禁使用 Pandas 或传统行式 ORM（如 SQLAlchemy 逐行查询）处理行情计算**。盘后历史特征归档统一使用 `DuckDB + Parquet`；盘中实时全市场 5300+ 只股票的多因子拼接与筛选统一使用 `Polars (LazyFrame)`，确保单次全市场 20 因子交叉过滤耗时小于 $5\text{ ms}$。
3. **纯函数因子舱解耦（Pure-Function Factor Pods）**：
   `backend/app/factors/` 目录下的每个 `.py` 文件代表一个独立的算子模块。每个因子只允许接收 `pl.LazyFrame` 并返回追加计算列后的 `pl.LazyFrame`，严禁在因子函数内部发起网络请求或读写磁盘。
4. **全天候双模驱动（Live + High-Fidelity Replay/Simulation）**：
   A 股仅在交易日 `09:15-15:00` 有盘中实时流。为了保证系统在周末、夜间及自动化测试时均可 100% 验证所有交互，数据采集层必须内置 **`Auto-Fallback 双引擎`**：
   * **实盘模式（`LIVE`）**：通过 `akshare` / `efinance` / `pytdx` 异步拉取东财涨停股池与全市场实时快照。
   * **全真模拟流模式（`SIM`）**：当处于非交易时段、网络受限或设置环境变量 `PULSE_MODE=sim` 时，自动切换为内置的**高保真 A 股盘口模拟发生器**（内置 200+ 只涵盖 1板至7板、3连阳至8连阳、10cm/20cm/30cm、不同市值与题材簇的真实 A 股标的，每 2 秒模拟一次分时游走、封单变化、炸板回封与换手率递增），确保前端看板打开瞬间即刻呈现高频流式刷新体验。
5. **2026 Modern Quant Dark 极客审美**：
   前端严禁使用陈旧的亮色后台模板或刺眼的纯红纯绿。必须严格执行本文档第 5 节定义的 **Linear $\times$ Bloomberg 暗色视觉规范**（曜石黑底色、珊瑚绯红涨停色、薄荷翡翠绿下跌色、流金空间板高亮、全等宽数字 `tabular-nums` 以及单元格内嵌 Canvas 微缩分时图）。

---

## 2. Monorepo 目录拓扑（Target File Tree）

智能体必须严格按照以下目录结构创建文件，不得随意更改层级：

```text
ashare-pulse/
├── AGENTS.md                           # 本工程交接总纲
├── Makefile                            # 一键安装、契约生成、前后端并发启动脚本
├── contracts/
│   ├── __init__.py
│   ├── schema.py                       # 【核心】Pydantic v2 全量数据模型、枚举与过滤请求体
│   └── export_openapi.py               # 导出 openapi.json 供前端生成类型
├── backend/
│   ├── pyproject.toml                  # Python 3.12+ 依赖配置 (uv 管理)
│   ├── data/                           # 存放 DuckDB 库文件与盘后 Parquet 历史快照
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py                     # FastAPI 入口、CORS 配置、生命周期启动调度器
│   │   ├── config.py                   # 环境变量配置 (LIVE / SIM 模式切换、刷新频率)
│   │   ├── core/
│   │   │   ├── memory_store.py         # 全局 Polars DataFrame 内存状态机 + 筛选执行引擎
│   │   │   └── duckdb_store.py         # DuckDB 历史日线与盘后预计算特征读写封装
│   │   ├── ingestors/
│   │   │   ├── base.py                 # 采集器抽象基类
│   │   │   ├── live_akshare.py         # 实盘采集器：调用 AkShare 涨停池 + 全市场实时行情
│   │   │   └── sim_generator.py        # 全天候高保真盘口流模拟器（每 2 秒演进一次盘口状态）
│   │   ├── factors/
│   │   │   ├── registry.py             # @register_factor 装饰器与 DAG 拓扑排序执行器
│   │   │   ├── f01_limit_ladder.py     # 连板数、N天M板反包池、炸板/冲击涨停状态算子
│   │   │   ├── f02_growth_streak.py    # 连续多日正增长（连阳天数、连涨累计涨幅、假阴容错）算子
│   │   │   ├── f03_turnover_metrics.py # 今日换手、昨日换手、昨换比（放量/缩量识别）算子
│   │   │   ├── f04_seal_quality.py     # 首次/最后封板时间、封成比、封流比、炸板次数算子
│   │   │   └── f05_history_gene.py     # 近1年涨停基因、历史封板成功率、距60日高点距离算子
│   │   ├── api/
│   │   │   ├── router_screener.py      # POST /api/v1/screener/query & GET /api/v1/macro/summary
│   │   │   └── router_sse.py           # GET /api/v1/stream/sse 实时增量快照与宏观情绪推送
│   │   └── mcp_server.py               # 面向外部超级智能体的 MCP (Model Context Protocol) 工具集
│   └── tests/
│       └── test_factors_and_filter.py  # Polars 算子与多条件交叉筛选自动化测试
└── frontend/
    ├── package.json                    # React 19 + Vite 6 + Tailwind v4 + TanStack Virtual + Zustand
    ├── vite.config.ts
    ├── index.html                      # 引入 JetBrains Mono & Inter 字体
    └── src/
        ├── main.tsx
        ├── App.tsx
        ├── types/
        │   └── contract.ts             # 与 contracts/schema.py 100% 映射的强类型定义
        ├── store/
        │   └── usePulseStore.ts        # Zustand 全局状态：当前一级量块、二级过滤条件、预设战法、SSE 实时数据流
        ├── components/
        │   ├── TopRegimeBar.tsx        # 顶栏：市场情绪温度计 + 10cm/20cm/30cm/剔除ST全局开关 + 数据源状态灯
        │   ├── BentoMacroMatrix.tsx    # 一级量块矩阵：连板梯队胶囊（1板~最高标、N天M板、炸板池）+ 连阳梯度胶囊（3~7+连阳）
        │   ├── MultiFactorPillBar.tsx  # 二级多要素筛选栏：分布直方图滑块、昨换比快捷切换、封板品质胶囊、题材云、战法预设
        │   ├── VirtualStockTable.tsx   # 高密度虚拟滚动表格：支持列排序、共振题材高亮、点击展开详情抽屉
        │   └── SparklineCanvas.tsx     # 高性能 Canvas 微图表：日内分时走势线 + 今昨换手双色对比条
        └── utils/
            └── formatters.ts           # A股数字格式化（亿、万、百分比、封板时间）
```

---

## 3. 唯一真理源数据契约（`contracts/schema.py` 完整代码规范）

智能体在 **Stage 0** 必须原样创建 `contracts/schema.py`，作为前后端的强类型锚点：

```python
# contracts/schema.py
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field


class BoardType(str, Enum):
    MAIN_10CM = "10cm"       # 沪深主板 (10% 涨跌幅)
    CHINEXT_20CM = "20cm"    # 创业板 / 科创板 (20% 涨跌幅)
    BSE_30CM = "30cm"        # 北交所 (30% 涨跌幅)


class MacroPoolType(str, Enum):
    ALL = "all"                         # 全市场强势候选池
    LIMIT_UP_LADDER = "limit_up"        # 连续涨停梯队池
    N_DAYS_M_BOARDS = "n_days_m_boards" # N天M板断板反包池
    BROKEN_OR_ATTACK = "broken_attack"  # 冲刺涨停 / 炸板池
    CONTINUOUS_GROWTH = "growth_streak" # 连续多日增长（连阳）榜


class TurnoverRegime(str, Enum):
    ALL = "all"
    SHRINK_ACCEL = "shrink"   # 缩量加速 (昨换比 < 0.75)
    NORMAL_HAND = "normal"    # 平量良性换手 (0.75 <= 昨换比 <= 1.35)
    EXPAND_DIV = "expand"     # 放量分歧换手 (昨换比 > 1.35)


class StockTickRecord(BaseModel):
    """单只股票在内存池中的全量实时+历史融合快照（共 26 个核心字段）"""
    # --- 基础标识 ---
    code: str = Field(..., description="6位股票代码，如 002085")
    name: str = Field(..., description="股票名称，如 万丰奥威")
    board: BoardType = Field(..., description="涨跌幅制式: 10cm / 20cm / 30cm")
    is_st: bool = Field(False, description="是否为 ST/*ST 标的")
    is_new: bool = Field(False, description="是否上市未满20个交易日")

    # --- 实时盘口与市值 ---
    price: float = Field(..., description="当前最新价(元)")
    pct_chg: float = Field(..., description="今日实时涨跌幅(%)")
    amount_today: float = Field(..., description="今日实时成交额(亿元)")
    float_mkt_cap: float = Field(..., description="表面流通市值(亿元)")
    free_float_mkt_cap: float = Field(..., description="实际自由流通市值(亿元，扣除5%以上大股东锁定筹码)")

    # --- 一级量块核心指标（连板梯队 & 连阳榜） ---
    is_limit_up: bool = Field(..., description="当前是否封死涨停")
    ever_limit_up_today: bool = Field(..., description="今日盘中是否曾触及涨停")
    limit_streak: int = Field(0, description="当前连续涨停板数: 0=未涨停, 1=首板, 2=二连板... N=N连板")
    n_days_m_boards_label: Optional[str] = Field(None, description="N天M板标签，如 '5天4板'、'4天3板'")
    growth_streak: int = Field(0, description="实时连续多日正增长(连阳)天数: 盘后N_prev + 今日实时正涨幅")
    streak_cum_return: float = Field(0.0, description="当前连板或连阳周期内的累计涨幅(%)")

    # --- 量能与换手因子 ---
    turnover_today: float = Field(..., description="今日实时实际换手率(%)")
    turnover_yesterday: float = Field(..., description="昨日全天实际换手率(%)")
    turnover_ratio_yd: float = Field(..., description="昨换比 = 今日换手率 / 昨日换手率")
    volume_ratio: float = Field(1.0, description="实时量比")

    # --- 封板质量因子（涨停/冲板专属） ---
    first_seal_time: Optional[str] = Field(None, description="首次封板时间 HH:MM:SS，未封板为 None")
    last_seal_time: Optional[str] = Field(None, description="最后封板时间 HH:MM:SS")
    open_board_count: int = Field(0, description="今日炸板次数")
    seal_order_amount: float = Field(0.0, description="当前买一封单金额(亿元)")
    seal_to_amount_ratio: float = Field(0.0, description="封成比(%) = 封单额 / 今日成交额 * 100")
    seal_to_cap_ratio: float = Field(0.0, description="封流比(%) = 封单额 / 实际自由流通市值 * 100")

    # --- 历史股性基因与趋势位置 ---
    zt_count_1y: int = Field(0, description="近1年(250交易日)涨停总次数(涨停基因)")
    seal_success_rate_1y: float = Field(0.0, description="近1年触及涨停后的收盘封板成功率(%)")
    max_streak_1y: int = Field(0, description="近1年历史最高连板数")
    dist_to_60d_high: float = Field(0.0, description="当前价距近60日最高价百分比(%)，>=0代表突破60日新高")

    # --- 题材共振与分时微图 ---
    theme_tags: List[str] = Field(default_factory=list, description="所属核心题材概念标签(按当日热度排序)")
    intraday_sparkline: List[float] = Field(default_factory=list, description="日内分时涨跌幅采样序列(16个锚点，供前端绘制微缩图)")


class ScreenerFilterRequest(BaseModel):
    """前端发送的多要素交叉过滤请求契约"""
    # 0. 全局制度过滤
    boards: List[BoardType] = Field(default=[BoardType.MAIN_10CM, BoardType.CHINEXT_20CM, BoardType.BSE_30CM])
    exclude_st: bool = True
    exclude_new: bool = True

    # 1. 一级量块过滤
    pool_type: MacroPoolType = MacroPoolType.ALL
    limit_streak_exact: Optional[List[int]] = Field(None, description="指定连板层级，如 [1] 首板, [2] 二板, [5,6,7] 高标")
    min_growth_streak: Optional[int] = Field(None, description="最低连阳天数，如 >=3")
    allow_doji_tolerance: bool = Field(False, description="连阳榜是否允许假阴十字星容错")

    # 2. 二级多要素区间过滤
    free_cap_min: float = Field(0.0, description="最小实际自由流通市值(亿)")
    free_cap_max: float = Field(2000.0, description="最大实际自由流通市值(亿)")
    price_min: float = Field(0.0, description="最低股价")
    price_max: float = Field(1000.0, description="最高股价")
    turnover_today_min: float = Field(0.0, description="今日最低换手率(%)")
    turnover_today_max: float = Field(100.0, description="今日最高换手率(%)")
    turnover_yd_min: float = Field(0.0, description="昨日最低换手率(%)")
    turnover_yd_max: float = Field(100.0, description="昨日最高换手率(%)")
    turnover_regime: TurnoverRegime = TurnoverRegime.ALL
    min_amount_today: float = Field(0.0, description="今日最低成交额(亿)")

    # 3. 封板质量与历史股性过滤
    max_first_seal_time: Optional[str] = Field(None, description="最晚首次封板时间，如 '09:35:00' 或 '10:00:00'")
    max_open_board_count: Optional[int] = Field(None, description="最大允许炸板次数，0代表仅看硬板")
    min_seal_to_amount_ratio: float = Field(0.0, description="最低封成比(%)")
    min_streak_cum_return: float = Field(0.0, description="连板/连阳期间最低累计涨幅(%)")
    min_zt_count_1y: int = Field(0, description="近1年最少涨停次数(股性基因)")
    min_seal_success_rate_1y: float = Field(0.0, description="近1年最低封板成功率(%)")
    only_near_60d_high: bool = Field(False, description="仅看距60日高点 -5% 以内或已突破新高的标的")
    selected_themes: List[str] = Field(default_factory=list, description="按共振题材标签过滤")

    # 4. 排序指令
    sort_by: str = Field("limit_streak", description="排序字段名")
    sort_desc: bool = True


class LadderTierStat(BaseModel):
    """单个连板梯队或连阳梯度的宏观统计胶囊数据"""
    tier_key: str               # 如 "limit_1", "limit_2", "limit_5_plus", "growth_3", "growth_5_plus"
    title: str                  # 如 "首板 (1板)", "2连板 (1进2)", "5板+ 最高标", "5连阳+"
    count: int                  # 当前家数
    promotion_rate: Optional[float] = None  # 晋级率(%)，如 1进2 晋级率 24.5%
    top_stock_name: Optional[str] = None    # 代表性龙头名称
    top_theme: Optional[str] = None         # 该梯队第一共振题材


class ThemeResonanceStat(BaseModel):
    """当日共振题材热度统计"""
    theme_name: str
    limit_up_count: int
    growth_count: int
    avg_pct_chg: float


class MacroDashboardSummary(BaseModel):
    """顶部宏观情绪与 Bento 量块统计汇总"""
    timestamp: str
    data_mode: str                          # "LIVE" or "SIM"
    total_limit_up: int                     # 今日封板总家数
    total_broken_board: int                 # 今日炸板总家数
    broken_rate: float                      # 实时炸板率(%)
    max_limit_height: int                   # 今日空间最高板数
    max_height_stock: str                   # 空间板龙头名称
    yesterday_limit_up_premium: float       # 昨日涨停今日平均溢价率(%)
    limit_ladder_tiers: List[LadderTierStat]
    growth_streak_tiers: List[LadderTierStat]
    hot_themes: List[ThemeResonanceStat]


class ScreenerQueryResponse(BaseModel):
    """查询响应体"""
    macro: MacroDashboardSummary
    total_matched: int
    execution_ms: float
    records: List[StockTickRecord]
```

---

## 4. 后端引擎实现规范（Polars + DuckDB + FastAPI）

### 4.1 因子注册表与算子流水线（`backend/app/factors/`）

1. **`registry.py`**：
   实现 `@register_factor(name: str, depends_on: list[str], output_cols: list[str])` 装饰器。当 `memory_store.py` 刷新盘口快照时，按拓扑顺序依次调用所有注册的算子函数，传入 `pl.LazyFrame`，最后统一执行 `.collect()` 落地为内存中的 `pl.DataFrame`。
2. **五大核心算子逻辑要求**：
   * `f01_limit_ladder.py`：
     * 根据 `board` 制式计算涨停阈值（`10cm` 为 $\ge 9.8\%$，`20cm` 为 $\ge 19.8\%$，`30cm` 为 $\ge 29.8\%$）。
     * 结合盘后预计算的 `yesterday_limit_streak` 字段，若今日 `is_limit_up == True`，则 `limit_streak = yesterday_limit_streak + 1`，否则为 `0`。
     * 根据近 5 日涨停次数生成 `n_days_m_boards_label`（如近 5 日涨停 $\ge 3$ 次且今日未断板或反包，标记为 `"5天4板"` 或 `"4天3板"`）。
   * `f02_growth_streak.py`：
     * 结合盘后 DuckDB 预计算的截至昨日连阳天数 `prev_growth_streak` 与累计涨幅 `prev_cum_return`。
     * 若今日实时涨跌幅 `pct_chg > 0.0`（当开启假阴十字星容错时放宽至 `pct_chg >= -0.4` 且 `turnover_ratio_yd < 0.9`），则实时连阳天数 $\text{growth\_streak} = \text{prev\_growth\_streak} + 1$，实时累计涨幅通过复利公式计算：
       $$\text{streak\_cum\_return} = \left( (1 + \frac{\text{prev\_cum\_return}}{100}) \times (1 + \frac{\text{pct\_chg}}{100}) - 1 \right) \times 100$$
     * 若今日 `pct_chg <= 0.0`，则实时连阳天数归零。
   * `f03_turnover_metrics.py`：
     * 计算昨换比：
       $$\text{turnover\_ratio\_yd} = \frac{\text{turnover\_today}}{\max(\text{turnover\_yesterday}, 0.05)}$$
   * `f04_seal_quality.py`：
     * 计算封成比与封流比：
       $$\text{seal\_to\_amount\_ratio} = \frac{\text{seal\_order\_amount}}{\max(\text{amount\_today}, 0.01)} \times 100$$
       $$\text{seal\_to\_cap\_ratio} = \frac{\text{seal\_order\_amount}}{\max(\text{free\_float\_mkt\_cap}, 1.0)} \times 100$$
   * `f05_history_gene.py`：
     * 关联 DuckDB 历史静态特征表中的 `zt_count_1y`（近一年涨停次数）、`seal_success_rate_1y`（近一年封板率）、`max_streak_1y` 与 `high_60d`（近60日最高价），实时计算当前价距 60 日高点的百分比距离：
       $$\text{dist\_to\_60d\_high} = \frac{\text{price} - \text{high\_60d}}{\text{high\_60d}} \times 100$$

### 4.2 Polars 毫秒级多因子过滤器（`backend/app/core/memory_store.py`）

`MemoryStore` 单例持有全市场最新的 `pl.DataFrame`。当收到 `ScreenerFilterRequest` 时，将其动态编译为单条 Polars 布尔掩码表达式 `pl.Expr`：
* 一级量块过滤：
  * `LIMIT_UP_LADDER`：`pl.col("is_limit_up") == True`，若传入 `limit_streak_exact`（如 `[2]`），则叠加 `pl.col("limit_streak").is_in(...)`（注：若包含 `5`，则匹配 `limit_streak >= 5`）。
  * `N_DAYS_M_BOARDS`：`pl.col("n_days_m_boards_label").is_not_null()`。
  * `BROKEN_OR_ATTACK`：`(pl.col("ever_limit_up_today") & ~pl.col("is_limit_up")) | (pl.col("pct_chg") >= 7.5)`。
  * `CONTINUOUS_GROWTH`：`pl.col("growth_streak") >= (req.min_growth_streak or 3)`。
* 二级多要素过滤：将市值区间、换手率区间、昨换比枚举（`SHRINK_ACCEL` 对应 `< 0.75`，`EXPAND_DIV` 对应 `> 1.35`）、首次封板时间字符串比较（`pl.col("first_seal_time") <= req.max_first_seal_time`）、炸板次数、封成比、涨停基因、题材交集（`pl.col("theme_tags").list.eval(...)`）全部通过 `&` 链式拼接，一次性执行过滤与排序，记录耗时 `execution_ms`。

### 4.3 全天候高保真模拟器与实盘双驱（`backend/app/ingestors/`）

* **`live_akshare.py`**：使用 `asyncio.to_thread` 包装 `akshare.stock_zt_pool_em`、`akshare.stock_zt_pool_zbgc_em`（炸板池）与 `akshare.stock_zh_a_spot_em`，拉取真实 A 股盘中数据并对齐至 Polars 内存表。
* **`sim_generator.py`（关键保底机制）**：
  * 初始化时生成包含 120 只精选真实 A 股代码与名称的种子池，涵盖：
    * **连板梯队代表**：`002085 万丰奥威 (5板/低空经济)`、`600611 大众交通 (4板/无人驾驶)`、`300476 胜宏科技 (20cm 3板/AI算力)`、`002130 沃尔核材 (3板/铜缆高速连接)`、`002402 和而泰 (2板/摩尔线程)`、`300058 蓝色光标 (20cm 2板/AI智能体)`、`000977 浪潮信息 (首板/AI算力)` 等 45 只不同连板高度的标的；
    * **N天M板反包代表**：`002261 拓维信息 (6天4板)`、`601127 赛力斯 (5天3板)` 等 15 只标的；
    * **连续多日正增长（连阳榜）代表**：`002273 水晶光电 (7连阳)`、`688256 寒武纪 (6连阳)`、`002463 沪电股份 (5连阳)`、`300750 宁德时代 (4连阳)`、`600519 贵州茅台 (3连阳)` 等 60 只涵盖 3 连阳至 8 连阳的趋势股。
  * 后台协程每 $2.0\text{ s}$ 执行一次 `tick()`：对非封死涨停股施加微幅随机游走（$\Delta p \in [-0.35\%, +0.45\%]$），实时累加 `amount_today` 与 `turnover_today`，动态更新 `intraday_sparkline` 序列与封单金额，并模拟小概率的盘中炸板与回封事件。

### 4.4 API 路由与 SSE 实时流（`backend/app/api/`）

1. `POST /api/v1/screener/query`：接收 `ScreenerFilterRequest`，返回 `ScreenerQueryResponse`。
2. `GET /api/v1/stream/sse`：使用 `sse-starlette` 或 FastAPI 原生 `StreamingResponse (text/event-stream)`，每 $2.0\text{ s}$ 向前端推送一次最新事件，前端收到信号后自动使用当前筛选条件刷新视图或直接合并增量快照。
3. `POST /api/v1/admin/mode`：允许在前端顶栏一键切换 `"LIVE"`（实盘东财接口）与 `"SIM"`（高频盘口模拟流）模式。

---

## 5. 前端 UI/UX 规范（Linear $\times$ Bloomberg 2026 极客看板）

### 5.1 色彩与排版 Token（Tailwind CSS v4 配置）

* **背景色阶**：
  * 全局画布背景：`#090A0F`（深邃曜石黑）
  * 一级量块卡片 / 表格容器：`#11131C`（暗夜石板灰），边框 `1px solid rgba(255, 255, 255, 0.07)`
  * 激活态胶囊 / 悬停行背景：`#1A1D2B`，激活态发光边框 `1px solid rgba(244, 63, 94, 0.45)`
* **金融语义色阶（A股红涨绿跌）**：
  * 涨停 / 正增长色（Coral Rose）：文字 `#F43F5E`，徽章背景 `rgba(244, 63, 94, 0.14)`
  * 炸板 / 下跌色（Emerald Mint）：文字 `#10B981`，徽章背景 `rgba(16, 185, 129, 0.14)`
  * 空间最高标 / 主流题材共振（Amber Gold）：文字 `#F59E0B`，徽章背景 `rgba(245, 158, 11, 0.15)`
  * 20cm/30cm 制度标识色（Electric Cyan）：`#06B6D4`
* **字体规范**：
  * 所有价格、涨幅、市值、换手率、昨换比、封单时间、连板数必须应用 `font-mono tabular-nums tracking-tight`，杜绝数字刷新时的水平抖动。

### 5.2 四大核心视图组件要求

1. **`TopRegimeBar.tsx`（全局顶栏）**：
   * 左侧：品牌 Logo `A-SHARE PULSE // 2026` + 实时连接脉冲呼吸灯（支持一键切换 `SIM 高频模拟流` / `LIVE 实盘东财流`）+ 引擎过滤耗时徽章（如 `1.8 ms`）。
   * 中部：**市场情绪温度计**（实时显示：`今日涨停 X 家`、`炸板率 Y%`、`昨日涨停溢价 +Z%`、`最高空间板 N板`）。
   * 右侧：制度快速过滤胶囊组 `[全部]`、`[仅10cm主板]`、`[仅20cm双创]`、`[30cm北交]`、`[剔除ST]`。
2. **`BentoMacroMatrix.tsx`（一级量块 Bento 矩阵）**：
   * 分为左右两大阵列卡片区：
     * **左区【连续涨停梯队池】**：包含 `全部涨停`、`5板+ 空间高标`、`4连板 (3进4)`、`3连板 (2进3)`、`2连板 (1进2)`、`首板 (1板)`、`N天M板反包`、`冲刺/炸板池` 八个可点击量块胶囊。每个胶囊显示当前家数、晋级率百分比条、以及该梯队领涨龙头名称。
     * **右区【连续多日增长（连阳）榜】**：包含 `全部连阳(3日+)`、`3连阳`、`4连阳`、`5连阳`、`6连阳`、`7连阳+ 极强趋势` 六个可点击量块胶囊，右上角附带 `[允许假阴十字星容错]` 快捷开关。
3. **`MultiFactorPillBar.tsx`（二级多要素药丸筛选栏）**：
   * **一键战法预设栏（Strategy Presets）**：内置 5 个实战高频战法按钮，点击即刻自动组合二级筛选参数：
     1. `🔥 早盘硬板一进二`（连板=2，首次封板 $\le$ `09:40:00`，炸板=0，封成比 $\ge 10\%$）
     2. `🚀 缩量加速连板龙`（连板 $\ge 3$，昨换比 `< 0.75` 缩量，近1年涨停基因 $\ge 5$ 次）
     3. `💎 小盘高换手首板`（连板=1，实际自由流通市值 $15 \sim 80\text{ 亿}$，今日换手率 $10\% \sim 35\%$）
     4. `📈 温和放量五连阳突破`（连阳 $\ge 5$，连涨累计涨幅 $\ge 15\%$，昨换比 $0.8 \sim 1.5$，临近或突破60日新高）
     5. `🔄 重置全部过滤`
   * **精细化多维控制器**：
     * 实际自由流通市值上限滑块（$10\text{ 亿} \sim 1000\text{ 亿}$）
     * 今日换手率 / 昨日换手率区间滑块
     * 昨换比三态胶囊（`全部` / `缩量加速 <0.75` / `平量良性 0.75-1.35` / `放量分歧 >1.35`）
     * 封成比下限滑块 & 首次封板时间过滤（`全部` / `09:25 一字` / `09:35前秒板` / `10:00前早盘板`）
     * 历史涨停基因下限滑块（近1年涨停 $\ge N$ 次）
     * **当日共振题材云（Hot Theme Pills）**：横向展示当日涨停家数最多的前 8 大题材胶囊（如 `#低空经济 (12)`、`#AI算力硬件 (9)`、`#无人驾驶 (7)`），点击即可按题材交叉过滤。
4. **`VirtualStockTable.tsx` & `SparklineCanvas.tsx`（高密度表格与微图）**：
   * 表格列包含：
     1. `代码/名称`（附带 `20cm` 青色角标与 `N天M板` 琥珀金角标）
     2. `核心量块身份`（显示如 `5连板` 珊瑚红高亮徽章 或 `7连阳` 趋势徽章）
     3. `日内分时微图`（调用 `SparklineCanvas` 渲染 $72 \times 24\text{ px}$ 平滑发光走势曲线）
     4. `最新价 / 实时涨幅`
     5. `连涨累计涨幅`
     6. `实际自由流通市值`（副行小字对比表面流通市值）
     7. `今换 / 昨换 / 昨换比`（带双色进度对比条 + `缩量`/`放量` 标签）
     8. `封板品质`（首次封板时间 + 封成比 + 炸板次数提示）
     9. `历史股性基因`（`近1年 X次涨停` + `封板率 Y%` + `60日新高突破` 标记）
     10. `共振题材标签`（与当日 Top3 热门题材一致的标签自动高亮琥珀流金边框）
   * **交互详情抽屉（Stock Inspector Drawer）**：点击任意一行股票，右侧滑出毛玻璃详情抽屉，展示该股的完整 26 项指标雷达卡片、换手率演变以及一键复制代码功能。

---

## 6. 智能体分阶段执行与自检流水线（Execution & Verification Protocol）

智能体接手本文件后，必须严格按以下 5 个阶段顺序推进，并在每个阶段结束时运行自检命令：

### Stage 0：初始化仓库与唯一数据契约
1. 创建目录结构，编写 `contracts/schema.py` 与 `contracts/export_openapi.py`。
2. 验证命令：运行 `python3 -c "import contracts.schema as s; print(len(s.StockTickRecord.model_fields))"`，确认输出为 `26`。

### Stage 1：构建后端 Polars 内存引擎、五大算子与全天候采集器
1. 初始化 `backend/pyproject.toml`（核心依赖：`fastapi`, `uvicorn`, `polars`, `duckdb`, `pyarrow`, `pydantic`, `akshare`, `httpx`, `pytest`）。
2. 实现 `backend/app/factors/` 下的 `registry.py` 及 `f01` 至 `f05` 五个算子模块。
3. 实现 `sim_generator.py`（120+ 只 A 股高保真种子池 + 2 秒级盘口演进）与 `live_akshare.py`（带自动异常降级保护：若非交易时间或东财接口超时，无缝回退至 `sim_generator`）。
4. 实现 `memory_store.py` 与 FastAPI 路由（`router_screener.py`、`router_sse.py`、`mcp_server.py`）。

### Stage 2：运行后端算子与过滤引擎自动化测试（强制门禁）
1. 编写并运行 `backend/tests/test_factors_and_filter.py`，测试内容必须覆盖：
   * 连板梯队过滤（验证 1板、2板、5板+ 分层正确性）；
   * 连阳榜过滤（验证连阳天数 $\ge 4$ 且累计涨幅过滤正确性）；
   * 二级交叉过滤（验证 `free_cap_max`、`turnover_regime == shrink`、`min_zt_count_1y` 组合查询在 $10\text{ ms}$ 内返回准确结果）。
2. 确保 `pytest` 100% 通过后，方可进入前端开发。

### Stage 3：构建 React 19 + Tailwind v4 极光看板前端
1. 初始化 `frontend/`，配置 Tailwind CSS、Lucide 图标库与 Zustand 状态库。
2. 编写 `src/types/contract.ts`，确保与 `contracts/schema.py` 字段一一对应。
3. 依次实现 `TopRegimeBar`、`BentoMacroMatrix`、`MultiFactorPillBar`、`SparklineCanvas`、`VirtualStockTable` 与右侧标的详情抽屉。
4. 对接后端 `POST /api/v1/screener/query` 与 `GET /api/v1/stream/sse`，实现盘中每 2 秒无闪烁丝滑更新。

### Stage 4：端到端联调与一键启动脚本交付
1. 在根目录编写 `Makefile`（或 `start.sh`），提供：
   * `make install`：自动安装前后端依赖；
   * `make test`：运行后端 Polars 算子单元测试与前端 `tsc --noEmit` 类型检查；
   * `make dev`：并发启动 FastAPI 后端（端口 `8000`）与 Vite 前端（端口 `5173`）。
2. 启动服务后，用 `curl -s -X POST http://localhost:8000/api/v1/screener/query -H "Content-Type: application/json" -d '{}' | head -c 500` 验证接口返回非空且包含完整的宏观梯队汇总与股票列表。