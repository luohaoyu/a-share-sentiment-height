---
name: a-share-sentiment-height-v1
description: "This skill should be used when a user requests A-share short-term sentiment-height analysis across the full market, Shanghai/Shenzhen Main Boards, or STAR/ChiNext growth boards. It builds dynamic strong-stock pools and evaluates T1 fixed 10-session returns, T2 confirmed bottom-fractal returns, and T3 active first-limit-up returns, including pool entries and exits, daily leaders, limit-up ladders, divergence timelines, trend charts, anomaly diagnostics, source notes, and an auditable process log."
version: 1.0.0
agent_created: true
---

# A股短线情绪高度分析 V1

## 版本

- 技能大版本：V1
- 当前语义版本：1.0.1
- 版本定位：三口径强势池、全市场及分市场趋势、涨停连板与异常审计的首个稳定版本；增加经校验的 CLI 调用契约，减少重复参数探测。

## 目的

将“最近 N 个交易日，逐日提取全市场 10 日区间涨幅最大值，并通过极值的抬升、回落、连续方向和榜首切换观察短线情绪强弱变化”的请求固化为可复用、可审计、可复现的标准流程。每日最大值及对应榜首股票是核心业务指标；达到指定阈值的股票数量、前排中位数和分市场最大值仅用于验证极值是否孤立及识别制度差异。所有结果必须同时给出统计口径、数据时点、数据源、过滤规则、缺失原因和工具调用链路；不得用猜测值补齐数据。

## 触发场景

在以下请求中加载本技能：

- 查询最近若干交易日每日 10 日区间涨幅最大值，并据此观察短线情绪高度变化；
- 要求在沪市主板、深市主板、科创板和创业板范围内，按“沪深主板”与“科创成长板”两个组合市场拆分；
- 要求去除 ST、*ST、退市整理股、风险警示股、新股或次新股；
- 要求输出趋势图、明细表、过滤审计、数据获取过程或工具调用日志；
- 需要复现或扩展已有的分市场历史涨幅统计。

## 默认参数与可配置项

未明确指定时采用以下默认值，并在交付物中显式写出：

- 股票池：仅纳入沪市主板、深市主板、科创板和创业板；明确排除北交所及其他市场；
- 市场分组：`沪深主板 = 沪市主板 + 深市主板`；`科创成长板 = 科创板 + 创业板`。报告不得将“科创成长板”简称为官方“科创板”；
- 时间：最近 N 个已完成交易日，默认 N=30；
- 指标：10 个交易日区间涨幅；
- 缓存准备：默认额外向前缓存 30 个交易日；缓存总量 = N + 涨幅计算回看期 + buffer，默认 30 + 10 + 30 = 70 个交易日；`buffer-sessions` 可覆盖该默认值；
- 首板搜索：不再使用 10 日涨幅回看期作为搜索上限；在当前缓存可见范围内从最早会话搜索至命中日，缓存起点是唯一硬边界；
- 新股排除：名称带 N/C 等新股标识，或统计日距上市日不足 60 个自然日；
- 无法核验上市日期：不纳入冠军，记入审计日志；
- 风险排除：排除 ST、*ST、名称含“退市”或以“退”结尾的股票、退市整理期股票，以及股票主数据中非正常上市状态的证券；过滤必须发生在最大值计算之前；
- 核心指标：每个目标交易日全部合格股票中的 10 日区间涨幅最大值、对应榜首股票、较前日变化、连续方向和榜首切换。
- 辅助指标：前 3 名中位数、达到配置阈值的股票数量、榜首与前排差异、分市场最大值；辅助指标不得替代极值成为报告主线。
- 输出：HTML 报告，主标题固定为“短线情绪高度”；副标题说明通过沪深主板与科创成长板每日10日涨幅最大值观察情绪扩张、分化与收缩。首屏增加数据范围和过滤说明，核心趋势区按“全市、沪深主板、科创成长板”拆分为三张独立图，随后给逐日榜首表、阈值广度验证、两个组合市场最大值、来源说明和调用链路。
- 情绪解读：最大值连续抬升表示高度扩张，连续回落表示高度收缩，下降后回升表示修复；榜首切换后最大值仍抬升表示新标杆接力。不得仅凭单日绝对值或固定阈值给出买卖结论。
- 制度差异：总体最大值必须同时配套沪深主板与科创成长板最大值；主板与双创涨跌幅制度不同，不得机械比较绝对数值，一升一降应解释为风格分化。

如果用户指定“3 个月、6 个月、1 年”或其他新股标准，覆盖默认 60 个自然日，并在报告标题和数据字段中记录实际阈值。

## 分层职责与确定性脚本

使用 AkShare 时先读取 `references/akshare_adapter.md`，按其中接口映射、缓存模型、历史 ST 边界和数据质量闸门执行。

建库、排查缓存结构或核对字段时读取 `references/cache_layout.md`：schema **v5** 下每个目录/文件的字段与语义、唯一价格权威 `price_store/`（单条前复权序列 `qfq[]` + 派生的 `is_limit_up`，**没有 raw 腿、没有复权因子与除权事件**）、派生层（`daily_adjusted/`、`forward_daily/`、`unadjusted_daily/`）的可删可重建性质、`calendar.json` 按 `--as-of` 截断及 `calendar_upstream.json` 的审计用途，以及 v3→v4→v5 的演进与保真度验证方法。`is_limit_up` 自 2026-10-26 起由本地 K 线按官方涨跌幅限制派生，不再有独立的涨停权威层。v5（qfq-only 单请求）为什么功能零损失、以及它与 v4 的取舍，见 `references/redesign_qfq_perday.md`；改采集层前必读。

按流程逐步执行、核对参数或复现异常时读取 `references/workflow_reference.md`；建库/增量到底怎么判、边界条件长什么样，见流程图 `references/bootstrap_flow.svg`。

调用主链路脚本前读取 `references/cli_contract.json`；需要理解参数语义、命令模板和维护规则时读取 `references/cli_reference.md`。两者是已经核验的 CLI 调用契约：契约有效、脚本未修改且调用已登记模式时，直接套用模板，**禁止例行执行 `python <script> --help` 或反复读取 argparse 源码**。仅当脚本/参数未登记、契约校验失败、参数解析报错、用户要求未覆盖模式，或脚本在契约校验后发生变化时，才允许用 `--help` 兜底。修改已登记脚本的 CLI 后，必须同步更新契约并运行 `scripts/validate/validate_cli_contract.py`，通过后才可交付。

需要**更换数据源或新增数据源**时，先读 `references/source_capability.akshare.json`，再对照《原始数据字段规范 v1.2》第 9 节的三层验收执行。该文件以实测记录 AkShare 七个接口族的能力：字段四值支持度（`supported`/`partial`/`derived`/`absent`）、常量兜底（如 `list_status` 只能填 `L`）、可用窗口（涨停池实测仅 **15 个交易日**）、漂移策略与取数粒度。适配器必须以此声明为设计输入，并用 `scripts/contract/validate_source_contract.py` 验收。

生成报告时先读取 `references/report_spa.md`：报告采用「契约 + 单页面应用」结构，技能只产契约数据，版式与交互固定在 `assets/app.html`，不得每次由 Agent 重新拼 HTML。

生成报告里的「AI 分析」页（情绪层状态卡 / 周期判定 / 策略约束）前先读 `references/trading_framework.md`：它把《交易决策框架总结 V2.1.1》情绪层逐字段落成取数口径，并给出三态判定表与四类策略映射。三条硬约束：

- **只用报告已有字段**（情绪高度、达标广度、逐日榜首）。均线承载、板块承接、个股操作级别一律输出「未覆盖：…」，**不得用情绪高度反推**这三项结论。
- **阈值、标签、策略映射全部来自 `assets/ai_rules.json`**。调整口径只改这个 JSON，不在 Python 里硬编码，也不在回答里临时改变判定标准。
- **结论是方法论映射，不是投资建议**。输出必须带 `disclaimer`，且不得给出仓位数值或个股买卖点；框架执行层的仓位公式本技能不计算。

采用“Agent 决策层 + 确定性脚本层 + 数据源接入层”三层结构：

```text
Agent 决策层
  需求解析、口径冻结、数据源选择、数据质量判断、异常解释、最终结论
        ↓
确定性脚本层
  交易日输入、抓取缓存、字段解析、市场分类、过滤、计算、排序、校验、渲染
        ↓
数据源接入层
  历史行情、交易日历、上市日期、风险标识、市场字段
```

结构总览见 `references/skill_structure.svg`（三层职责 / 5 组脚本 / 主链与连板支链 / 契约层）。

### 必须优先脚本化的步骤

将以下步骤交给 `scripts/` 中的确定性脚本，避免每次由 Agent 临时重写：

1. 抓取历史页面或接口响应并缓存原始数据；
2. 解析和标准化日期、代码、名称、市场、涨幅、上市日期；
3. 根据代码前缀和官方字段执行市场分类；
4. 根据 ST/风险标识执行资格过滤；
5. 根据上市日期和配置阈值执行新股过滤；
6. 计算 10 日区间涨幅；
7. 对每个交易日×市场排序并取冠军；
8. 检查日期、字段、重复记录、缺失组合和异常数值；
9. 输出 JSON 与契约数据；颜色、坐标与交互固定在 `assets/app.html`，脚本**不拼 HTML/SVG**；趋势图按涨幅情绪区间着色，并支持默认市场与其他市场切换。

### 必须由 Agent 判断的步骤

不要把以下判断硬编码成脚本的静默决策：

- 用户所说“最近”“新股”“不同市场”的具体含义；
- 数据源是否具备完整历史横截面能力；
- `Chg10D` 的真实定义、复权方式和起止交易日；
- 榜单是否存在展示阈值，结果能否代表全市场；
- 页面结构变化、解析失败和异常涨幅的原因；
- 多数据源不一致时的主数据源选择；
- 实际发生了哪些技能、连接器和工具调用。

脚本发现问题后必须返回错误或警告；不得自行猜测、补零或伪造工具调用。

### Skill 指令与 Python 脚本的协作边界

| 层级                | 负责事项                                                                             | 禁止事项                                           |
| ----------------- | -------------------------------------------------------------------------------- | ---------------------------------------------- |
| Skill / Agent 决策层 | 解析 N、结束日期、复权口径、新股阈值和市场范围；判断收盘状态；选择 AkShare、MCP 或降级源；解释数据缺口；决定是否允许 ST 降级；记录真实调用链路 | 不手算收益、不临时改写市场规则、不把失败日期补零、不把当日名称快照伪装成历史 ST 全量数据 |
| AkShare 接入脚本      | 调用固定 AkShare 接口；做限速、重试、断点续传、字段映射和原始缓存；输出稳定 JSON 契约                               | 不决定用户口径，不选冠军，不解释异常，不静默跳过失败股票                   |
| 本地计算脚本            | 读取稳定契约；按交易日对齐；执行复权收益、市场分类、ST/新股过滤、排序、审计和质量检查                                     | 不联网，不猜缺失上市日期或 ST 状态，不更换数据源                     |
| 报告与校验脚本           | 检查完整性并渲染 JSON/HTML/SVG                                                           | 不修改原始数据，不掩盖缺失组合                                |

执行时先由 Skill 冻结参数，再运行接入脚本，再运行计算、校验和报告脚本。任何脚本非零退出码都返回给 Skill/Agent 判断，不在脚本内擅自切换统计口径。

### 脚本目录分组

`scripts/` 按职责分 5 个子目录。跨目录协作一律走 CLI（`subprocess`），仅 `contract/validate_source_contract_selftest.py` 直接 import 同目录的校验器。各脚本的技能根目录由 `SKILL.md` 向上定位，移动脚本无需再改路径常量。

| 子目录                 | 职责                                  | 脚本数                            | 联网 |
| ------------------- | ----------------------------------- | ------------------------------ | -- |
| `scripts/fetch/`    | 采集：AkShare 唯一接入点 + 派生/迁移 + 网页榜单降级通道 | 10（含 2 个测试、2 个未接入的 neodata 通道） | 部分 |
| `scripts/compute/`  | 计算：阈值筛选、结构波段、情绪波段、涨停连板梯队、批量冠军       | 6                              | 否  |
| `scripts/report/`   | 渲染：契约适配、报告契约校验、单文件打包、一键链路           | 6                              | 否  |
| `scripts/validate/` | 校验：CLI契约、缓存契约、三口径一致性、涨停连板一致性、榜单与报告数据完整性 | 6                              | 否  |
| `scripts/contract/` | 契约：缓存→契约投影、52 条规则校验器及其自测            | 3                              | 否  |

合计 31 个脚本。`scripts/fetch/` 中两个 `*_neodata*.py`（`init_limit_up_neodata.py`、`ingest_neodata_raw.py`）**未接入主流程、也未被任何脚本 import**，属历史通道留存：不要照它们的字段假设去排查采集问题，也不要擅自删除——排查基准以 `fetch_akshare_data.py` 为准。

同目录下还有四个脚本不在主链路上，备查即可：`cache_refresh.py`（增量决策纯函数，`fetch_akshare_data.py` 直接 import 它的 `sessions_to_fetch`）、`repair_limit_up.py`（历史修复工具，不参与日常流程）、`test_bootstrap_v5.py` 与 `test_cache_refresh.py`（离线契约测试，mock 网络层）。


### 固定脚本职责

| 脚本                                                      | 确定性职责                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | 主要输入                                                                                                                                                                                                                                          | 主要输出                                                                                                                                                                                                                                                                                                                            |                                                                  |                       |
| ------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- | --------------------- |
| `scripts/fetch/fetch_akshare_data.py`                   | AkShare 默认接入层，`--mode` 只有 **`bootstrap`** 与 **`status`** 两种（`snapshot` 的全市场快照与 `waves` 的候选池补数已并入 bootstrap：增量由 `cache_refresh.sessions_to_fetch` 按交易日判定，只抓缓存缺失的会话）；每只股票只发一次 `adjust="qfq"` 请求，涨停标记 `is_limit_up` 由本地 K 线按官方涨跌幅限制派生（不再调用 `stock_zt_pool_em` 三方接口），每次写入 `price_store` 前重算。输出采用 **schema v5 整合缓存**：`price_store/<ts_code>.json` 是唯一价格权威（单条前复权 `qfq[]` 四价序列 + 派生的 `is_limit_up`，**没有 raw 腿、没有复权因子/除权事件**），`calendar.json` 按观测日 `--as-of` 截断（上游原表另存 `calendar_upstream.json`）；raw/hfq 视图与纯 list 镜像全部为派生产物 | 结束日期、起始日期、N、缓存目录、限速和重试参数                                                                                                                                                                                                                      | `calendar.json`、`calendar_upstream.json`、`stock_basic.json`、`price_store/`、`daily_adjusted/`、`*_manifest.json`（含 `schema_version` 与 `coverage`）。**不产出** `forward_daily/`、`unadjusted_daily/`（由 `derive_cache.py --mode derive` 生成），也**不产出任何 ST/风险名称快照**——manifest 里 `historical_st_status` 恒为 `not_available_from_this_adapter` |                                                                  |                       |
| `scripts/fetch/prices_v4.py`                            | 历史命名遗留的价格层库（v5 下仅 `read_store/derive_rows` 分支仍被 `validate_cache_contract.py` 的可选派生比对调用；文件名中的 v4 是历史包袱，不代表当前 schema）：`build_store` 折叠权威层、`derive_rows` 投影出 raw/hfq/qfq 视图、`detect_factor_events` 识别除权——**这三条是 v4 语义，v5 缓存上不适用**                                                                                                                                                                                                                                                                                       | 作为库被引用                                                                                                                                                                                                                                        | —                                                                                                                                                                                                                                                                                                                               |                                                                  |                       |
| `scripts/fetch/derive_cache.py`                         | 派生与迁移（无网络，日常必跑一次）：`derive` 由 `price_store/` 重建 `daily_adjusted/`、`forward_daily/`、`unadjusted_daily/`——**v5 下 bootstrap 不再产出后两者，候选池的 qfq 四价与未复权涨停标记全靠这一步**；`factorize` 把 v3 缓存拆成 `price_store/`、`verify` 与旧镜像逐值比对并按「镜像 × 字段」输出精确/差1tick/越界计数（出现越界退出码 2，仅对 v4/v3 基线有意义）                                                                                                                                                                                                                                               | `--mode factorize\|derive\|verify`、`--cache-dir`、**`--qfq-symbols`（derive 的候选作用域，直接传 ts_code 列表，最常用）**、`--scope-from`（derive 的作用域来源目录，取其中 `forward_daily/`）、`--out-dir`、`--baseline-dir`、`--with-views`、`--tolerance`、`--sample`、`--json-out` | 迁移/派生台账、保真度报告                                                                                                                                                                                                                                                                                                                   |                                                                  |                       |
| `scripts/validate/validate_cli_contract.py`             | 离线读取 `references/cli_contract.json`，通过 Python AST 核对主链路脚本的路径、参数名、必填标记、枚举值、可静态求值默认值和命令模板；不启动业务脚本、不导入 AkShare、不联网，也不逐脚本执行 `--help`                                                                                                                  | `--contract`（默认 `references/cli_contract.json`）、`--json-out`                                                                                                                                            | JSON 校验报告；一致为 0，不一致为 1                                                                                                                                                                                                                                                                                                      |                                                                  |                       |
| `scripts/validate/validate_cache_contract.py`           | 按布局自动分支校验整合缓存契约（输出的 `layout` ∈ `v5\|v4\|v3`，由 store 内 `schema_version` 决定，不再只看目录形状）：`calendar`/`stock_basic` 完整性；**v5 下校验 `price_store` 的 `schema_version == 5`、`qfq[]` 四价键齐全、价格有限、`is_limit_up` 布尔性（首笔 `None`）**；v4 遗留缓存仍按 raw+factor+anchor 校验；并抽样重新派生与 `daily_adjusted`/`forward_daily` 比对以发现过期派生产物；派生层必须为纯 list；`daily_adjusted` 分区与 `coverage` 对齐。文件数量始终穷举，内容深检默认每目录抽样 300 个（`--deep-all` 恢复逐文件穷举；45 日窗×5577 只实测约 8.4 s）。**`forward_daily`/`unadjusted_daily` 在 v5 下是 derive 产物，bootstrap 后缺失不报警告**             | `--cache-dir`、`--sample`、`--deep-sample`、`--deep-all`、`--workers`、`--json-out`                                                                                                                                                                | JSON 报告（`ok`/`counts`/`issues`）与退出码（有 `error` 为 1）                                                                                                                                                                                                                                                                              |                                                                  |                       |
| `scripts/compute/compute_market_winners_from_daily.py`  | 读取批量交易日历、全市场日线、复权因子、股票主数据和历史 ST 快照；本地计算并取冠军                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | 供应商导出的 JSON/CSV 文件或文件目录                                                                                                                                                                                                                       | `market_trend_data.json`、过滤审计、缺失组合和质量计数                                                                                                                                                                                                                                                                                         |                                                                  |                       |
| `scripts/compute/screen_market_threshold_gainers.py`    | 逐日计算四板块股票池的 10 日收益，先排除 ST、*ST、退市、退市整理和新股，再提取总体、沪深主板及科创成长板最大值；同时保留达到阈值的股票作为广度辅助样本                                                                                                                                                                                                                                                                                                                                                                                                                                     | 与批量计算脚本相同的标准数据契约、`--min-return`                                                                                                                                                                                                               | 每日总体最大值、两个组合市场最大值、榜首、环比、榜首切换、连续榜首、前3中位数、阈值达标明细与资格审计 JSON                                                                                                                                                                                                                                                                        |                                                                  |                       |
| `scripts/validate/validate_threshold_gainers.py`        | 校验阈值、日期数、重复记录、资格过滤和逐日汇总一致性，并将校验结果写回输入 JSON                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | 阈值筛选 JSON                                                                                                                                                                                                                                     | `validation` 字段、流程状态和退出码                                                                                                                                                                                                                                                                                                        |                                                                  |                       |
| `scripts/compute/calculate_structure_waves.py`          | 三口径扩展：对阈值候选去重后逐股按时间顺序迭代消除K线包含关系，在合并K线上识别并去重顶底分型，选择最新达标日前最近一个已确认底分型作为结构起点；不使用MA5，不构造笔、线段或中枢                                                                                                                                                                                                                                                                                                                                                                                                                           | 阈值候选 JSON、`--forward-daily`（含 open/high/low/close 的日线，qfq 或 hfq 均可，但必须四价齐全且同尺度）                                                                                                                                                               | `results`（每条候选一股一记录：confirmed/pending/data_insufficient/rejected + 起止日期与三套收益）                                                                                                                                                                                                                                                   |                                                                  |                       |
| `scripts/compute/calculate_emotion_rallies.py`          | 三口径扩展：以 `is_limit_up` 涨停标记识别首板起点，跟踪前复权最高价，连续 4 个交易日不创新高确认结束；`is_limit_up` 由 `price_store` 本地 K 线按官方涨跌幅限制派生（不再调用三方涨停池），首笔无前驱 `prev_close` 时判 `data_insufficient`。读取日线时按候选代码预过滤文件（文件名无可解析代码时自动回退全量扫描），全市场缓存下 IO 从分钟级降到秒级                                                                                                                                                                                                                                                                                               | 阈值候选 JSON、`--forward-daily`（前收/前高）、`--unadjusted-daily`（未复权收盘 + `is_limit_up`）                                                                                                                                                                | `emotion_rallies`（每条候选一股一记录：confirmed/pending/data_insufficient/rejected + 首板/最高/结束日与波段高度）                                                                                                                                                                                                                                      |                                                                  |                       |
| `scripts/validate/validate_three_height_metrics.py`     | 三口径扩展：校验并合并 10 日情绪高度、缠论结构波段、首板情绪波段三类 JSON，要求候选股代码集合与逐股命中日期完全一致，状态合法且收益有限                                                                                                                                                                                                                                                                                                                                                                                                                                             | 三份 JSON                                                                                                                                                                                                                                       | `schema_version=three-height-metrics-v1`、`height_metrics`、`structure_waves`、`emotion_rallies`、`three_height_validation`；错误退出码 2                                                                                                                                                                                                 |                                                                  |                       |
| `scripts/compute/calculate_limit_up_ladder.py`          | 涨停连板指标：**直接读 `price_store/` 全市场**（`daily_adjusted/` 没有 `is_limit_up`，读它必错），按交易日维护每只股的连续涨停长度，输出每日涨停家数、首板/二板+ 分层、最高连板、市场分布与二板以上个股明细。口径固定为沪深主板+创业板+科创板，排除 ST/退市风险与上市不足 N 个自然日次新，不含北交所；缺行或 `is_limit_up=null` 记「数据不足」不断板；缓存首个交易日的连板只作下限（`window.lower_bound_before`）                                                                                                                                                                                                                                                     | `--price-store`、`--calendar`、`--stock-basic`、`--end-date`、`--window-days`、`--min-listing-days`、`--ladder-min-boards`                                                                                                                          | `limit_up_ladder.json`（`window`/`scope`/`dates`/`points`/`audit`）                                                                                                                                                                                                                                                               |                                                                  |                       |
| `scripts/validate/validate_limit_up_ladder.py`          | 连板数据自洽校验：日期升序无重复且与 points 等长、`by_boards` 求和 == `total`、`total == first_board + consecutive`、`max_boards == 梯队最高板`、ladder 条目板数不低于下限且不超过对应层计数、首日必须带下限标记                                                                                                                                                                                                                                                                                                                                                                | `limit_up_ladder.json`                                                                                                                                                                                                                        | 校验 JSON 与退出码（有 error 为 1）                                                                                                                                                                                                                                                                                                       |                                                                  |                       |
| `scripts/report/build_threshold_gainers_report.py`      | 将每日极值与阈值广度 JSON 渲染为短线情绪高度报告；输入含 `structure_waves`/`emotion_rallies` 时自动追加两个波段面板                                                                                                                                                                                                                                                                                                                                                                                                                                      | 情绪高度 JSON（或三口径合并 JSON）                                                                                                                                                                                                                        | 固定主标题、三张核心趋势图、逐日榜首、阈值广度、分市场、缠论结构波段、首板情绪波段、折叠审计                                                                                                                                                                                                                                                                                  |                                                                  |                       |
| `scripts/report/to_contract_data.py`                    | **契约数据适配器（报告的正式出口）**：把三口径合并 JSON 转成 `report-view-v1` 契约数据；只做字段搬运与单位换算（`pct` / `rally_gain_pct` ÷100，`theoretical_gain_pct` 原样），构建首屏 `summary`、逐日 `height`、涨停连板 `limit_up`（`unit=count`，禁止 ×100）、达标池三口径 `pool_trends`、两张明细表与 `details` 懒加载层。给 `--limit-up-ladder` 时在首屏追加涨停家数/连板家数/最高连板三张卡并把一句事实写入 `tldr`，**不改变 `summary.state` 判定**；不给时 `limit_up=null`，SPA 自动隐藏该视图。不产任何 HTML/SVG/颜色/坐标                                                                                                                               | `--input`、`--forward-daily`、`--return-days`（固定 10）、`--end-date`、`--limit-up-ladder`、`--output`                                                                                                                                                | `data.json`（契约版本 `report-view-v1`）                                                                                                                                                                                                                                                                                              |                                                                  |                       |
| `scripts/report/validate_contract.py`                   | **契约校验（六道防线）**：`contract_version` 匹配、曲线 `points` 与 `x` 等长、`y_max` 不小于实际最大值、`pending` 状态终点必须为 `null`、gain 类字段 \`                                                                                                                                                                                                                                                                                                                                                                                                      | value                                                                                                                                                                                                                                         | > 10` 判为百分数漏除 100、`limit_up` 计数自洽（`by_boards`求和 ==`total`、`max_boards\` 与梯队一致、缺失日必须整体为 null）。零依赖，不引 jsonschema                                                                                                                                                                                                                  | `--data`、`--contract`（默认 `assets/report.contract.json`）、`--json` | 校验报告与退出码（有 error 为 1） |
| `scripts/report/pack_report.py`                         | **单文件打包器**：把 `assets/app.html` 与 `data.json` 合并为内嵌 `<script type="application/json">` 的 `report.html`，双击可离线打开，零外部引用；打包前强制跑契约校验，`--data-js` 可另出开发期拆分文件。纯标准库，不需要 node/npm                                                                                                                                                                                                                                                                                                                                              | `--data`、`--output`、`--app`（默认 `assets/app.html`）、`--contract`、`--data-js`、`--no-embed`、`--skip-validate`                                                                                                                                     | `report.html`（单文件报告）与打包统计                                                                                                                                                                                                                                                                                                       |                                                                  |                       |
| `scripts/report/build_report_spa.py`                    | **一键链路**：`to_contract_data` → `validate_contract` → `pack_report` 串联，任一步非零退出立即终止，不重写业务逻辑                                                                                                                                                                                                                                                                                                                                                                                                                             | `--input`、`--forward-daily`、`--output`、`--end-date`、`--data-out`、`--data-js`、`--limit-up-ladder`                                                                                                                                              | `report.html`、`report.data.json`                                                                                                                                                                                                                                                                                                |                                                                  |                       |
| `scripts/fetch/fetch_and_parse_rankings.py`             | 网页榜单降级通道：抓取、缓存、解析、上市日期读取                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | 交易日和数据源 URL                                                                                                                                                                                                                                   | `normalized_candidates.json`、原始缓存、请求清单                                                                                                                                                                                                                                                                                          |                                                                  |                       |
| `scripts/compute/filter_and_rank_winners.py`            | 网页榜单降级通道：市场分类、去 ST、去新股、排序取冠军                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | 标准化候选 JSON                                                                                                                                                                                                                                    | 过滤后的冠军 JSON 和审计记录                                                                                                                                                                                                                                                                                                               |                                                                  |                       |
| `scripts/validate/validate_market_trend_data.py`        | 完整性、重复、字段、资格和缺失检查                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | 冠军 JSON                                                                                                                                                                                                                                       | 校验 JSON/终端结果和退出码                                                                                                                                                                                                                                                                                                                |                                                                  |                       |
| `scripts/report/build_market_trend_report.py`           | 静态 SVG、HTML 明细和过滤审计渲染                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | 冠军 JSON                                                                                                                                                                                                                                       | HTML 报告                                                                                                                                                                                                                                                                                                                         |                                                                  |                       |
| `scripts/contract/validate_source_contract.py`          | **原始数据契约校验器**：把《原始数据字段规范 v1.2》第 8 节 **52 条规则**变成可执行检查（交易日历、市场股票、日 K、涨停历史、风险状态、能力声明）。无法执行的规则**显式列入 skipped**，不静默通过。与 `validate_contract.py`（校验报告产物 data.json）职责不同                                                                                                                                                                                                                                                                                                                                                     | `--data-dir`、`--baseline-dir`、`--json-out`、`--now`                                                                                                                                                                                            | 规则级违规清单（含规则号）、未执行清单、退出码                                                                                                                                                                                                                                                                                                         |                                                                  |                       |
| `scripts/contract/validate_source_contract_selftest.py` | 校验器离线自测：造合规 fixture 断言 0 error，再对 52 条规则**逐条施加破坏**并断言被抓到，另加 9 项负向（不该误报）断言，共 61 项                                                                                                                                                                                                                                                                                                                                                                                                                                     | 无                                                                                                                                                                                                                                             | 逐项断言结果与退出码                                                                                                                                                                                                                                                                                                                      |                                                                  |                       |
| `scripts/contract/export_contract_dataset.py`           | **缓存 → 契约形态投影**：把"一行三组列"的折叠缓存展开成"一行一口径"的契约数据集，供 `validate_source_contract.py` 消费。`--mode raw` 只改名换单位、不修语义（用于暴露缓存与契约的真实差距）；`--mode normalized` 执行契约要求的归一（日历截断、`availability`→`not_collected`）并写 `projection_log.json`。`--enrich-ohlc` 按股补抓四价（本地缓存可复用），`--security-scope daily-union` 剥离样本覆盖度干扰                                                                                                                                                                                                                        | `--cache-dir`、`--out-dir`、`--as-of`、`--mode`、`--enrich-ohlc`、`--security-scope`、`--risk-from-master`                                                                                                                                          | 契约形态文件组 + `projection_log.json`                                                                                                                                                                                                                                                                                                 |                                                                  |                       |

脚本使用示例：

> 以下示例用于解释完整工作流；日常组装命令时以 `references/cli_contract.json` 为机器可读入口，以 `references/cli_reference.md` 为参数语义入口。契约校验有效时不得为确认用法而逐脚本执行 `--help`。

```bash
# AkShare 首次建库：只回填 N+gain-lookback+buffer 个交易日，逐股请求可断点续跑。
# 源默认 roster：sina + tencent，eastmoney 默认关闭（实测失败率 47%~99%，纯损耗）。
# 需要它时先写一个 JSON 文件，再传文件路径——该参数是 Path，不接受内联 JSON：
#   printf '{"eastmoney": true}' > ./run/src.json
#   ... --source-rates ./run/src.json
# --retries 1 = 不做单次尝试内部的 HTTP 重试（默认）；失败交给任务级重投去换源。
python scripts/fetch/fetch_akshare_data.py \\
  --mode bootstrap \\
  --output-dir ./cache/akshare \\
  --end-date 2026-10-23 \\
  --target-count 30 \\
  --lookback 10 \\
  --buffer-sessions 30 \\
  --shards 8 \\
  --workers 6 \\
  --retries 1 \\
  --task-attempts 3 \\
  --shard-retries 2

# 建库后自检整合缓存契约（schema v5）。
# 校验器按 store 内 schema_version 判定布局（输出 layout: v5|v4|v3）：
# v5 校验 qfq[] 四价与 is_limit_up；v4 遗留缓存仍按 raw+factor+anchor 校验。
# --sample 控制"抽样重新派生比对"的股票数；深检数量由 --deep-sample 控制（默认 300）。
# 45 日窗 × 5577 只实测：默认深检 300 个约 8.4 s；--deep-all 逐文件穷举约 5 min。
python scripts/validate/validate_cache_contract.py --cache-dir ./cache/akshare --sample 200

# 后续每日收盘后的"增量"：仍是同一条 bootstrap，不做重抓；默认 buffer 为 30，若需查询最近 30 日且实际只缓存 60 日，可显式传 --buffer-sessions 20。
# 增量按交易日判定（cache_refresh.sessions_to_fetch）：只补缓存里缺的会话，
# 已落盘的股票直接跳过网络。--confirm-completed-session 只在确认该日已收盘时才加。
python scripts/fetch/fetch_akshare_data.py \\
  --mode bootstrap \\
  --output-dir ./cache/akshare \\
  --end-date 2026-10-26 \\
  --target-count 30 --lookback 10 --buffer-sessions 30 \\
  --shards 8 --workers 6 \\
  --confirm-completed-session

# 三口径扩展前置：为阈值候选小股票池派生 qfq 四价与未复权涨停标记。
# bootstrap 只写 price_store/；qfq 四价与 unadjusted 镜像由 derive 生成（全程离线）。
# --qfq-symbols 直接给候选 ts_code，作用域外的股票不会落盘。
# 若候选股比缓存窗口更早的日期也要覆盖，先把 bootstrap 的 buffer-sessions 放大。
python scripts/fetch/derive_cache.py \
  --mode derive \
  --cache-dir ./cache/akshare \
  --qfq-symbols 600241.SH 002242.SZ 301190.SZ \
  --json-out ./run/derive.json

# 计算层：AkShare 缓存已是统一前复权调整价，因此使用 --prices-adjusted。
# ST 过滤的现实边界：fetch 不产出 st/ 目录（manifest 的 historical_st_status 恒为
# not_available_from_this_adapter），所以只能走"按当前名称过滤"的降级路径：
#   --st-coverage partial --allow-name-only-st
# 若将来接入了具备历史风险状态的 MCP/权威源，再用 --st <目录> --st-coverage complete。
python scripts/compute/compute_market_winners_from_daily.py \\
  --calendar ./cache/akshare/calendar.json \\
  --daily ./cache/akshare/daily_adjusted \\
  --stock-basic ./cache/akshare/stock_basic.json \\
  --st-coverage partial \\
  --allow-name-only-st \\
  --prices-adjusted \\
  --target-count 30 \\
  --lookback 10 \\
  --threshold-days 60 \\
  --end-date 2026-10-23 \\
  --output ./run/market_trend_data.json

# MCP 或其他数据源若导出原始收盘价和复权因子，则不使用
# --prices-adjusted，并同时提供 --adj-factor。

# 短线情绪高度：逐日输出四板块合并股票池的总体10日涨幅最大值，以及沪深主板、科创成长板各自最大值；>=50%的股票数量仅作为广度辅助验证。
# 强势股池 = {10日涨幅达标} ∪ {10日涨幅排名前N}：达标股全部收入，不足N只才由排名补足
# （--strong-pool-rank-size 配置N，默认10；--signal-pool-size 为同义兼容别名）。
# 信号K取前一交易日收盘后的强池并固定，计算这批昨日强势股在今日相对昨收的等权平均OHLC收益；
# 报告将其作为围绕今日10日涨幅最大值的K线信号编码，不视为真实10日OHLC。
# 同一口径同时决定阈值池扩展（rank_fill）：rank_fill 即当日强池中未达标的那部分。
python scripts/compute/screen_market_threshold_gainers.py \\
  --calendar ./cache/akshare/calendar.json \\
  --daily ./cache/akshare/daily_adjusted \\
  --stock-basic ./cache/akshare/stock_basic.json \\
  --st-coverage partial \\
  --allow-name-only-st \\
  --prices-adjusted \\
  --target-count 30 \\
  --lookback 10 \\
  --min-return 50 \\
  --strong-pool-rank-size 10 \\
  --threshold-days 60 \\
  --end-date 2026-10-23 \\
  --output ./run/threshold_gainers.json

python scripts/report/build_threshold_gainers_report.py \\
  --input ./run/threshold_gainers.json \\
  --output ./run/threshold_gainers.html

# 三口径扩展：在10日情绪高度基础上，对阈值候选逐股计算包含处理后的顶底分型结构与首板情绪波段，并合并校验。
# 输入契约：阈值候选 JSON 与 10 日报告共用，必须含 qualified[]（每条含 date/code/ts_code/market/market_group/name）。
python scripts/compute/calculate_structure_waves.py \\
  --input ./run/threshold_gainers.json \\
  --forward-daily ./cache/akshare/forward_daily \\
  --output ./run/structure_waves.json --workers 4

python scripts/compute/calculate_emotion_rallies.py \\
  --input ./run/threshold_gainers.json \\
  --forward-daily ./cache/akshare/forward_daily \\
  --unadjusted-daily ./cache/akshare/unadjusted_daily \\
  --output ./run/emotion_rallies.json --workers 4

python scripts/validate/validate_three_height_metrics.py \\
  --base-input ./run/threshold_gainers.json \\
  --structure-input ./run/structure_waves.json \\
  --emotion-input ./run/emotion_rallies.json \\
  --output ./run/three_height_metrics.json

# 合并后的 JSON 直接喂给同一份报告脚本，自动追加“底分型起点高度”与“首板情绪波段高度”两个面板。
python scripts/report/build_threshold_gainers_report.py \\
  --input ./run/three_height_metrics.json \\
  --output ./run/three_height_metrics.html

# 涨停连板指标：读全市场 price_store（不是 daily_adjusted，那里没有 is_limit_up）。
# 口径：沪深主板+创业板+科创板，排除 ST/退市风险股与上市不足 60 个自然日次新，不含北交所。
# 连板 = 连续交易日 is_limit_up 为真的长度；缓存首个交易日只能算下限，输出里带 lower_bound_before。
python scripts/compute/calculate_limit_up_ladder.py \\
  --price-store ./cache/akshare/price_store \\
  --calendar ./cache/akshare/calendar.json \\
  --stock-basic ./cache/akshare/stock_basic.json \\
  --end-date 2026-10-26 --window-days 30 \\
  --output ./run/limit_up_ladder.json

python scripts/validate/validate_limit_up_ladder.py \\
  --input ./run/limit_up_ladder.json --expected-dates 30

# 正式交付：一键生成契约数据 → 契约校验 → 单文件 SPA 报告。
# assets/app.html 与 assets/report.contract.json 已内置，--app/--contract 不必传。
# --return-days 固定 10，是 10 日涨幅的长度，切勿用输入里的 threshold_days（那是观察窗口天数）。
python scripts/report/build_report_spa.py \\
  --input ./run/three_height_metrics.json \\
  --forward-daily ./cache/akshare/forward_daily \\
  --limit-up-ladder ./run/limit_up_ladder.json \\
  --output ./run/report.html \\
  --data-js ./run/report.data.js

# 分步排查时拆开跑，能看清坏在哪一层。
python scripts/report/to_contract_data.py --input ./run/three_height_metrics.json \\
  --forward-daily ./cache/akshare/forward_daily --output ./run/data.json --return-days 10
python scripts/report/validate_contract.py --data ./run/data.json
python scripts/report/pack_report.py --data ./run/data.json --output ./run/report.html

# 降级：仅在没有完整横截面批量接口时使用历史网页榜单。
python scripts/fetch/fetch_and_parse_rankings.py \\
  --dates 2026-10-22 2026-10-23 \\
  --output-dir ./run \\
  --resolve-listing

python scripts/compute/filter_and_rank_winners.py \\
  --input ./run/normalized_candidates.json \\
  --output ./run/market_trend_data.json \\
  --threshold-days 60

# 用户要求“每个交易日全市场冠军”时，追加 --overall；输出每个交易日一条记录。
python scripts/compute/filter_and_rank_winners.py \\
  --input ./run/normalized_candidates.json \\
  --output ./run/market_trend_data_overall.json \\
  --threshold-days 60 \\
  --overall

python scripts/validate/validate_market_trend_data.py \\
  --input ./run/market_trend_data.json \\
  --expected-dates 2 \\
  --expected-markets 5

python scripts/report/build_market_trend_report.py \\
  --input ./run/market_trend_data.json \\
  --output ./run/market_trend_report.html
```

脚本退出码含义：`0` 表示本步骤成功，非 `0` 表示出现请求失败或校验失败。非零退出码不得被忽略；应由 Agent 读取日志后决定重试、切换数据源或停止交付。

### 三口径波段计算的数据前置条件

10 日情绪高度只需前复权（qfq）收盘价即可计算；但两类逐股波段对数据源有更高要求。**v5 起每只股票只抓一条 qfq 序列**：`price_store/*.json` 的 `qfq[]` 已含完整四价，因此 bootstrap 之后执行 `derive_cache.py --mode derive --qfq-symbols <候选 ts_code>`，即派生出 `forward_daily/`（qfq 四价）与 `unadjusted_daily/`（未复权 + `is_limit_up`）：

- **顶底分型结构**（`calculate_structure_waves.py`）：需要每只候选股的前复权 `open/high/low/close` 四价齐全且同尺度。先迭代处理包含关系，再识别并去重顶底分型，右侧合并K线收盘后确认；不使用MA5，不构造笔、线段或中枢。
- **首板情绪波段**（`calculate_emotion_rallies.py`）：需要 `forward-daily`（前收/前高，用于跟踪最高价）与 `unadjusted-daily`（未复权收盘 + `is_limit_up` 涨停标记）。`is_limit_up` 由本地 K 线按官方涨跌幅限制在前复权口径下派生（非三方涨停池），首笔无前驱 `prev_close` 时判 `data_insufficient`。

若只跑了 bootstrap 而没跑 derive：结构段会因缺 `forward_daily/` 直接判 `data_insufficient`；情绪段会因缺 `unadjusted_daily/` 同样退化。**该缺口通过 `derive_cache.py --mode derive --qfq-symbols <候选>` 补齐**（纯离线、不联网）：由 `price_store/qfq[]` 派生 `forward_daily/`（qfq 四价）与 `unadjusted_daily/`（未复权 + `is_limit_up`，涨停标记由本地 K 线按官方涨跌幅限制派生，不调用 `stock_zt_pool_em`）。缓存方面，bootstrap 的 `--buffer-sessions` 默认 30，可按需覆盖；缓存总量为 `target-count + gain-lookback + buffer-sessions`，例如 N=30、gain-lookback=10、buffer=30 时缓存 70 个交易日。首板搜索不再使用 gain-lookback 作为回溯上限，只受缓存起点约束。跑这两段前先执行 derive 即可。`validate_three_height_metrics.py` 会强制三类 JSON 的候选股代码集合与逐股命中日期完全一致，不一致直接报错退出码 2。

### 三口径数据模型契约（`schema_version=three-height-metrics-v1`）

三份 JSON 与合并产物共用以下约定，消费 `merged.json` 时必须遵守，避免量纲混算与字段语义错配：

- **标识符**：`structure_waves`、`emotion_rallies` 及基础 `qualified` 行统一使用 `code`=6 位裸代码（如 `000530`）+ `ts_code`=带后缀（如 `000530.SZ`）。跨数组关联一律用 `code` 或 `ts_code`，不得假设 `code` 带后缀。
- **状态枚举**：`status ∈ {confirmed, pending, data_insufficient, rejected}`；`pending` 时结构 `end_date/end_confirm_date/end_high` 缺失、情绪 `trend_end_date` 为 `null`，读取须用 `.get()` 并容忍字段缺省（不同 status 的键集不完全一致）。
- **结构波段起止两端不是同一件事**：`status=confirmed` 只代表**起点（底分型）已确认且当日达标**，不代表终点已确认。终点取「起点确认日之后、测量日（最新达标日）之前最近一个已确认顶分型」；观察期内没有已确认顶分型时，`end_date/end_confirm_date/end_high/dual_confirm_gain_pct` 一律为 `null`（实测 30 日窗 76 只候选里仅约 20 只成立），**禁止用测量日或 `observable_gain_pct` 顶替**。UI 上这三列显示「未确认」即表示该只个股确实没有已确认顶分型，而不是数据缺失。
- **首板搜索窗口与 `rejected` 语义**：首板在每个候选窗口的**闭区间 `[base_date, hit_date]`** 内搜索。`base_date` 当天若为涨停且其前 5 个交易日无涨停，它本身就是一个首板，**不得跳过**（早期实现写成 `base_index + 1` 会把这类个股整段漏掉）。窗口内找不到合格首板时，`rejected` 的 `reasons[0]` 必须区分两种真实成因：`no_limit_up_in_search_window`＝窗口内没有任何涨停，本就没有首板可锚（趋势型）；`first_board_precedes_search_window`＝窗口内涨停全是续板，真实首板早于窗口（长连板）。判定顺序固定为**先判续板、再判缺失**：前 5 个交易日只要有一个已确认涨停，就足以否定首板，缺的状态补上也改不了结论；只有「前 5 日全无涨停但存在缺失状态」才落 `data_insufficient`。`rejected` 永不表示数据缺失。
- **三口径量纲（不可混算）**：
  - `height_metrics.ten_day_return`：`percentage_points`（50 表示 50%），来源字段 `pct`。
  - `height_metrics.structure_wave`：`decimal_return`（0.5 表示 50%）。`theoretical_gain_pct=measurement_close/start_low-1`；`observable_gain_pct=measurement_close/start_confirm_close-1`（起点确认后买入、持有到测量日收盘）；`dual_confirm_gain_pct=end_confirm_close/start_confirm_close-1`（起止两端均确认，无顶分型时为 `null`）。三者依次递减——`dual` 例外，它只覆盖到顶分型确认日，与另两条的测量终点不同，不可直接比大小。
  - `height_metrics.emotion_rally`：`percentage_points`（50 表示 50%），字段 `rally_gain_pct`。
- **渲染约定**：结构波段的三套收益须 ×100 再显示为百分比；10 日涨幅与情绪波段本身已是百分比量纲，直接显示。三口径禁止相互替换或合并为单一数值。

## 标准执行流程

### 1. 解析需求并冻结口径

先把自然语言需求转换为结构化参数：

```text
统计日期范围
交易日数量
市场分组与代码规则
区间长度
是否复权
ST/风险股排除规则
新股排除规则
数据源与数据时点
输出格式
```

优先使用交易所交易日历或已连接金融数据源确定交易日。不得把周末、节假日或尚未收盘的日期混入“已完成交易日”。若当前日期处于交易时段，默认只统计上一个已完成交易日，除非用户明确要求盘中数据。

### 2. 选择并验证数据源

本 Skill 唯一数据源为 **AkShare 本地接入层**。这是硬约束，不是优先建议。以下规则不可违背：

1. **AkShare 是唯一主源**：交易所名录接口批量获取股票主数据；行情由 `cache_refresh.sessions_to_fetch` 判定缓存缺失的交易日，再**逐股发一次 `adjust="qfq"` 请求**补齐（一次请求即取回全历史，因此增量与建库请求数同阶，普通 A 股约 5,577 次）；
2. **MCP / 网页榜单不得作为主源、补充源或降级替代源**：任何 MCP 接口（含 NeoData `screen_cn_stocks`、Tushare `daily` 等）返回的横截面数据，都不得替代 AkShare 的本地采集与本地计算；
3. **MCP 仅允许用于字段含义与数据源边界的检索确认**：例如确认 `Chg10D` 定义、复权口径、接口字段语义；不得借“检索”之名直接返回候选集或冠军数据进入计算层；
4. **AkShare 不可用必须停止并报错**：若 AkShare 环境缺失、上游不可达，或 bootstrap 失败率 > 0，必须中断流程并向用户报告，**不得静默切换到 MCP 或网页**；
5. **执行顺序强制**：必须先运行 `--mode status` 确认缓存状态（零网络成本），再按 `next_action` 决定 `compute` / `bootstrap_incremental` / `bootstrap`；未执行此链不得进入计算层。

AkShare 的 `stock_zh_a_hist` 是单股接口，不应被描述为“按交易日批量获取全市场历史日线”。全市场回填必须逐股调用（限速、重试、缓存、可断点续跑）。**注意当前实现没有全市场快照增量通道**：`stock_zh_a_spot_em()` 已不在主链路，每个新交易日仍会重发约 5,577 次请求，日常增量成本与首次建库同阶（约 9–10 分钟）。改用按交易日返回横截面的批量源（如 Tushare `daily`）是已知优化方向，尚未落地，**但在本 Skill 中即便落地也只能作为 AkShare 的内部采集优化，不得改变“AkShare 是唯一对外数据源”的定位**。目标为最近 N 个交易日时，只回填 `N+gain-lookback+buffer` 个交易日；默认 `gain-lookback=10`、`buffer=30`，因此 N=30 时默认缓存 70 个交易日。首板搜索不再使用 gain-lookback 作为回溯上限，只受缓存起点约束；若搜索触达缓存起点，必须在诊断字段中明确记录，不能推断缓存起点之前的首板。

在取数前验证数据源是否同时支持：

- 指定历史交易日；
- 全市场或足够完整的股票横截面；
- 10 日区间涨幅或可自行计算的历史收盘价；
- 复权口径；
- 股票代码、名称、上市日期和风险标识；
- 市场范围过滤。

若数据源只展示涨幅超过某阈值的股票，必须在结果中标明“榜单展示阈值”，不能将未展示的市场/日期组合填为 0，也不能声称该市场没有冠军。若数据源无法支持完整横截面，优先请求用户连接合适的数据源；若继续交付，必须将结果标注为“展示范围内冠军”或“可核验范围内冠军”。

### 3. 获取历史候选榜单

对每个有效交易日获取完整候选集，至少保留以下字段：

```text
trade_date
code
name
market_label
close_or_return_field
pct_10d
listing_date
source_url
source_timestamp
```

若数据源提供历史 10 日涨幅字段，记录字段名和定义。若只提供历史收盘价，按以下公式计算：

```text
10日区间涨幅 = 当日复权收盘价 / 10个交易日前复权收盘价 - 1
```

明确说明该口径使用 11 个收盘点、形成 10 段收益；不得把自然日 10 天和交易日 10 天混为一谈。全市场榜单必须先按涨幅降序，再按市场代码规则筛选。

### 4. 识别市场并过滤资格

默认代码分组如下；如数据源提供官方市场字段，优先用官方字段并用代码前缀交叉检查：

| 市场   | 默认代码前缀          |
| ---- | --------------- |
| 沪市主板 | 600、601、603、605 |
| 深市主板 | 000、001、002、003 |
| 创业板  | 300、301         |
| 科创板  | 688             |

仅允许以上四个板块进入股票池；北交所及其他市场直接排除。输出时再合并为“沪深主板”和“科创成长板”两个组合市场。

对每个交易日、每个市场，按涨幅从高到低逐只执行资格检查：

1. 判断代码是否属于当前市场；
2. 判断名称是否含 ST、*ST 或明确退市/风险警示标识；
3. 判断名称是否带 N、C 等新股标识；
4. 查询并核验上市日期；
5. 计算统计日距上市日的自然日龄；
6. 按用户指定阈值排除新股/近端次新股；
7. 上市日期缺失或详情页无法核验时排除并记录原因；
8. 第一只通过全部检查的股票作为该日期、该市场的冠军。

过滤日志至少包含：

```text
trade_date, market, code, name, pct_10d,
listing_date, excluded, exclusion_reason, source_url
```

不要仅凭股票名称或模型记忆判断上市日期；不要把“没有检索到”改写成“不是新股”。

### 5. 做一致性与异常检查

交付前逐项检查：

- 日期数量是否等于要求的交易日数量；
- 每个日期是否为有效交易日；
- 每个日期×市场组合最多一条冠军；
- 冠军是否确实来自该市场；
- ST 和新股过滤是否真正生效；
- 上市日期是否早于统计日；
- 涨幅数值、正负号和单位是否一致；
- 是否发生复权口径混用；
- 是否存在页面阈值、空榜或请求失败；
- 全周期峰值是否能由明细表复核；
- 趋势图点数是否与明细记录一致。

发现异常时保留原始候选和排除记录，修正后重新生成，不得静默覆盖。

### 6. 生成可见且可复现的交付物

默认生成（文件名统一为 `短线情绪高度_<市场>_最近<N>个交易日.html`，与报告主标题一致）：

1. 单文件 SPA 报告（正式交付默认）或静态 HTML（降级预览）
   - 三个趋势模块：全市 / 沪深主板 / 科创成长板；
   - 每日×市场明细表；
   - 过滤审计样例和统计数量；
   - 数据源、计算公式、缺失说明；
   - 工具调用链路和流程日志。
2. `market_trend_data.json`
   - 参数、交易日、市场、冠军、过滤审计、来源页面和警告信息。
3. 可复现脚本（如数据源允许本地重跑）。

趋势图优先使用不依赖外部脚本的内嵌 SVG，确保离线预览时仍能显示；明细表直接写入 HTML，不依赖运行时 JavaScript 动态填充。若使用交互式图表，必须同时保留静态降级内容。

报告形态按场景二选一：

- **单文件 SPA 报告（正式交付默认）**：`python scripts/report/build_report_spa.py --output 短线情绪高度_<市场>_最近<N>个交易日.html`。
  产物是**一个** HTML 文件（建议文件名与主标题一致，如 `短线情绪高度_沪深主板_最近30个交易日.html`），数据内嵌、零外部引用、双击离线可开，含八个视图
  （情绪结论 / 高度趋势 / 涨停连板 / 达标池三口径 / 逐日榜首 / 缠论结构波段 / 首板情绪波段 / 方法与审计）、
  状态筛选、股票搜索和明细弹层。版式与交互固定在 `assets/app.html`，
  技能侧只负责产出符合 `assets/report.contract.json` 的数据，**不得为单次需求改写模板**。
  涨停连板视图只在传入 `--limit-up-ladder` 时出现；未传时 `limit_up=null`，SPA 隐藏该入口而不是渲染空图。
- **静态 HTML 报告（快速预览或降级）**：`build_threshold_gainers_report.py`、`build_market_trend_report.py`。
  纯静态、无 JS 执行，适合给不需要交互的场合。

交付 SPA 时必须同时说明：

- `contract_version`（当前 `report-view-v1`）与 `to_contract_data.py` 的实际参数（尤其 `--return-days 10`、`--end-date`）；
- 契约校验结果（错误数 / 警告数），有覆盖缺口警告时如实转述，不得隐藏；
- `pool_trends.coverage` 的 `pool_union` / `no_structure_start` / `no_first_board`，让读者知道 T2/T3 的覆盖边界；
- 若 `--skip-validate` 被使用过，必须明确告知。

趋势分析区规范：

- 报告主标题固定为“短线情绪高度”，副标题和数据说明写清两组市场、四板块股票池与过滤规则。
- 第一个趋势模块必须是“每日10日涨幅最大值与短线情绪高度”，并按“全市短线情绪高度、沪深主板短线情绪高度、科创成长板短线情绪高度”依次输出三张独立图；不得将三条序列叠加在同一张图，也不得先展示阈值达标数量、名单迁移或市场构成。
- 全市图及两个分市场图都把原每日最大值点替换为**高度锚定的昨日强势股次日表现K线信号**：全市场、沪深主板、科创成长板必须在各自范围内独立排名和选池，禁止从全市场池事后拆分；各图Y轴和灰色基准线仍为表现日对应范围的每日10日涨幅最大值。股票池在前一交易日收盘后按**「10日涨幅达标股全收 ∪ 10日涨幅排名前N」**确定并固定（N默认10，`--strong-pool-rank-size` 可配置，`--signal-pool-size` 为同义别名）：达标只数≥N时池即全部达标股（可多于N只），不足N只才由排名补足。显示四价 = 表现日对应范围10日涨幅最大值 + 该范围昨日强势股池在表现日相对昨日收盘的等权平均OHLC收益。红涨绿跌按信号C与O比较。该K线只编码昨日强势股的次日承接形态，不是实际价格K线，也不代表真实10日涨幅OHLC；图例、tooltip和报告说明必须显式披露。
- 三张图每根K线至少提供市场范围、表现日期、股票池选择日期、对应范围10日涨幅最大值基准、次日表现OHLC偏移、实际入选数、有效样本数及成员明细；全市场还需提供榜首名称、代码及所属市场。三图均标记期内最高、期内最低和最新点。
- 首屏 KPI 必须突出最新最大值、最新榜首、观察期高低值、榜首切换次数和当前情绪状态；累计达标股票数不得作为首要结论。
- 阈值达标数量只能放在“辅助验证”模块，用于识别“极值上升但广度收缩”等背离。
- 必须展示沪深主板与科创成长板最大值，明确主板与双创涨跌幅制度造成的可比性限制。
- 交付 JSON 应记录每日 `maximum_return_10d`、`maximum_change_vs_previous`、`leader`、`leader_changed`、`leader_streak_days`、`top3_median_return_10d`、`qualified_count`、`market_maxima` 和 `market_group_maxima`，保证前端展示可审计、可复现。

## 工具调用链路记录规范

在报告中按实际发生顺序记录，不得虚构未调用的工具。推荐采用以下日志格式：

```text
[序号] 工具/技能：...
目的：...
输入摘要：...
关键输出：...
状态：成功/部分成功/失败
影响：对后续步骤的影响
```

典型链路如下，仅可作为模板，实际输出必须以真实调用为准：

```text
1. 加载金融分析技能：冻结行情统计、来源、风险和输出口径。
2. 检索金融领域能力：确认历史横截面、10日字段和数据源边界。
3. 获取交易日历：确定最近 N 个已完成交易日。
4. 获取历史榜单/历史日线：逐日读取候选数据。
5. 解析候选字段：提取日期、代码、名称、涨幅、来源链接。
6. 核验上市日期：确认新股过滤所需的上市时间。
7. 执行市场分组和资格过滤：去 ST、退市整理和新股。
8. 生成 JSON/HTML/SVG：输出数据、图表和明细。
9. 结构与数值校验：检查记录数、HTML、脚本和图表点数。
10. 呈现最终文件：提供报告和可复现中间产物。
```

工具名可按实际情况记录，例如金融技能加载、金融领域检索、历史网页读取、Python/Bash 数据处理、文件读写、HTML 结构校验和结果呈现。不要把“技能”写成“行情数据库”，不要把 MCP 协议写成数据供应商；应分别说明方法层、接入层和数据层。

## 缺失、失败与降级处理

- 数据源未连接：先请求用户连接所需的金融数据服务，不得用静态记忆补数。
- 只有当前快照没有历史横截面：明确说明无法可靠回溯，并改用可核验历史页面或请求更合适的数据源。
- 页面请求失败：记录 URL、日期、错误状态和重试结果；失败日期不得伪造冠军。
- 上市日期无法核验：候选排除，除非用户明确允许“上市日期未知仍纳入”。
- 榜单存在展示阈值：保留缺失状态和警告，不能填 0。
- 某市场没有合格记录：输出“无可核验记录/展示范围不足”，不能写成“涨幅为 0”。
- 数据源之间不一致：优先保留主数据源结果，并列出差异字段、时间点和复权口径；不要静默选择更高值。

## 最终回答要求

最终回复必须简要说明：

- 使用的统计口径和时间范围；
- 实际使用的数据源/工具链；
- ST、新股和市场分组规则；
- 生成了哪些文件；
- 数据限制、缺失和未解决问题；
- 文件路径或可打开的交付物。

不要把趋势图解读成投资建议，不要省略新股基数效应、榜单阈值和数据时点等限制。
