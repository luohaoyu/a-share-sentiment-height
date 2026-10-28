# 报告应用层：契约 + SPA（`report-view-v1`）

本文件说明「技能只产数据、HTML 是单页面应用」这一层约定。渲染不再是脚本拼 HTML 字符串，
而是固定一份 SPA（`assets/app.html`）消费一份契约数据（`data.json`）。

```text
三口径合并 JSON（three_height_metrics.json）
        ↓ scripts/report/to_contract_data.py      只做数据搬运与单位换算
data.json（契约数据，report-view-v1）
        ↓ scripts/report/validate_contract.py     五道结构防线
data.json（校验通过）
        ↓ scripts/report/pack_report.py           内嵌 + 合并
report.html（单文件，双击离线可开，零外部依赖）
```

一条命令跑完三步（推荐）：

```bash
python scripts/report/build_report_spa.py \
    --input three_height_metrics.json \
    --forward-daily cache/akshare/forward_daily \
    --output report.html \
    [--end-date 2026-10-23] [--data-js report.data.js] [--skip-validate]
```

分步执行（排查问题时应拆开跑，能看清是哪一层坏）：

```bash
python scripts/report/to_contract_data.py --input three_height_metrics.json \
    --forward-daily cache/akshare/forward_daily --output data.json --return-days 10
python scripts/report/validate_contract.py --data data.json
python scripts/report/pack_report.py --data data.json --output report.html
```

`--app` / `--contract` 已默认指向 `<skill>/assets/app.html` 与 `<skill>/assets/report.contract.json`，
所以在任意工作目录下执行都不需要写全路径。

## 文件归属

| 路径 | 角色 | 谁改 |
|---|---|---|
| `assets/app.html` | SPA 模板，含占位符 `__REPORT_DATA__` | 只在改版式/交互时改，与数据无关 |
| `assets/ai_rules.json` | AI 分析规则：阈值、标签、策略映射、未覆盖清单、免责声明 | **调口径只改这里**，口径说明见 `references/trading_framework.md` |
| `assets/report.contract.json` | 契约单一真相源（字段、单位、分层、视图、降级） | 改数据结构时先改契约，再改两边实现 |
| `scripts/report/to_contract_data.py` | 技能侧唯一出口：算好的结果 → 契约数据 | 新增字段时改 |
| `scripts/report/validate_contract.py` | 结构校验，零依赖（不引 jsonschema） | 新增校验规则时改 |
| `scripts/report/pack_report.py` | 打包器，纯标准库（不需要 node/npm） | 基本不用改 |
| `scripts/report/build_report_spa.py` | 三步串联的便捷入口 | 基本不用改 |

## 高度锚定的股票池情绪K线信号

全市场和分市场高度图的Y轴均仍是各自每日10日涨幅最大值。原来的每日点升级为“昨日强势股次日表现”K线信号：全市场、沪深主板、科创成长板分别在各自范围内，于前一交易日收盘后按**同一套强势股口径**确定股票池并固定——`{10日涨幅达标股} ∪ {10日涨幅排名前N}`，达标股全收（数量可多于N），不足N只时由排名补足（默认N=10）；表现日计算对应股票池相对选择日收盘价的等权平均OHLC收益，再以表现日对应范围的10日涨幅最大值为锚点生成显示四价：

```text
strong_pool(t)     = {code: return_10d(t-1) >= threshold} ∪ top_N_by_10d_return(t-1)
display_O/H/L/C(t) = maximum_return_10d(t) + mean((OHLC(t) - close(t-1)) / close(t-1), members in strong_pool(t))
```

该口径与阈值池同源：`qualified ∪ rank_fill` 恰好等于当日强池，区别只在锚定日（信号K锚定 t-1，阈值池锚定 t）。

`height.points[].candle_signal.selection_date` 保存全市场股票池确定日，`performance_date` 必须与高度点日期一致；`height.points[].market_candle_signals[市场分组]` 使用完全相同公式，但必须在对应市场分组内独立排名、独立选池，并以该分组表现日的10日涨幅最大值为锚点，禁止从全市场池事后拆分。`return_offsets` 保存昨日强势股的次日收益信号，`display` 保存同一Y轴的绘图值；全市场 `base_height_10d` 必须与 `height.points[].value` 相等，分市场则必须与 `height.points[].by_market[市场分组]` 相等。红涨绿跌按 `display.close >= display.open` 判断。该K线只是信号编码，不代表真实价格或真实10日涨幅OHLC；SPA必须显示说明，tooltip必须同时给出市场范围、选择日、表现日、高度基准、原始OHLC偏移、入池构成（达标只数 / 排行补足只数）、池中位涨幅（`pool_median_return_10d`，用于区分“池本来就弱”与“池够强但次日承接差”）和有效样本数。缺少任一OHLC或昨收的成员不得用收盘价补齐，需从共同有效样本中排除并降低覆盖状态。

## 契约三层

`summary`（首屏必载，目标 < 200 KB）与 `details`（懒加载，`evidence` 体量大）分开。
SPA 首屏只读 `summary` 层；用户点开某只股票时才读 `details[ts_code]`。
现在数据量小，两层都内嵌在同一个 `<script type="application/json">` 里；
数据长大以后可以把 `details` 拆成 `<script src="report.details.js">` 而不动 SPA 逻辑。

八个视图（`#/summary`、`#/ai`、`#/height`、`#/pool`、`#/leaders`、`#/structure`、`#/emotion`、`#/method`）
由契约 `views` 声明，SPA 的 `VIEWS` 常量与之逐项对齐；未知路由回退到首个视图，不白屏。
视图 label 是给人看的（如 `pool` 在界面上叫「达标股能涨多高」），改文案时
`assets/report.contract.json` 与 `assets/app.html` **必须同步改**，否则契约与界面会各说一套。

## 单位纪律（历史上真出过事故，必须遵守）

契约内**所有 gain 类字段统一为小数比例**，渲染层统一 ×100 后展示，**数据侧禁止预先乘 100**。

| 来源字段 | 原始单位 | 适配器处理 |
|---|---|---|
| `qualified[].pct` | 百分数（58.4293 表示 58.43%） | `pct_to_ratio()` ÷100 |
| `emotion_rallies[].rally_gain_pct` | 百分数（140.89 表示 140.89%） | `pct_to_ratio()` ÷100 |
| `structure_waves[].theoretical_gain_pct` | **小数比例**（0.5 表示 50%） | `as_ratio()` 原样保留 |

历史事故：报告侧对已是百分数的 `rally_gain_pct` 又 ×100，页面上出现 `14089.46%`（正确值 140.89%）。
`validate_contract.py` 已加防线：任一 gain 类字段 `|value| > 10` 直接判错并拒绝打包。

## 参数语义陷阱

`threshold_days`（实测值 60）是**新股排除的自然日阈值**，不是区间涨幅天数，也不是缓存 buffer。
10 日涨幅的长度默认是 10，由 `--return-days` 独立指定；它必须与输入 `lookback_trading_sessions` 一致，绝不复用 `threshold_days`。报告 `window_days` 直接取实际输出 `dates` 数量。
混用会让 T1 曲线整体偏低/偏高（曾经把 89.46% 算成 174.85%）。

## 「达标股能涨多高」三种算法的口径

界面叫「达标股能涨多高（三种算法）」，数据键仍为 `pool_trends`（历史命名，不改以免破坏契约）。

- 基础池快照 `P(t) = {code | 10日涨幅(code,t) ≥ 阈值}`，每个交易日一张，全程复用。
- `t1` 固定 10 日涨幅：对观察窗口内每个截止日 `T`，取 `P(T)` 内 `close(T) / close(T-10个交易日) - 1` 的最大值。
- `t2` 严格底分型起点涨幅：对每个截止日 `T`，只使用 `T` 及之前的数据，取 `P(T)` 内 `close(T) / 最近已确认严格底分型真实最低价 - 1` 的最大值。底分型来自包含关系处理后的合并K线，不构造笔、线段或中枢；T日之后确认的底分型不得回填到T日及更早日期。
- **结构波段的终点是另一件事**：`end_date/end_confirm_date/end_high` 取「起点确认日之后、测量日之前最近一个**已确认顶分型**」。观察期内没有已确认顶分型时四字段（含 `gain_dual`）一律为 `null`，UI 显示「未确认」——这表示该股确实还没形成确认顶分型，不是数据缺失。30 日窗实测约 20/76 只成立。`gain_dual` 只回撤到顶分型确认日，与 t1/t2 的测量终点不同，**不可与前两条比大小**；历史上它曾直接复制 `gain_observable`，已修正。
- `t3` 首板情绪起点涨幅：对每个截止日 `T`，只使用 `T` 及之前的数据，取 `P(T)` 内 `close(T) / 当前有效首板日收盘 − 1` 的最大值。首板识别不受10日收益 lookback限制，但当前有效波段必须在T日尚未按连续四个不创新高交易日结束；T日之后发现的首板、终点或最高价不得回填到T日及更早日期。缓存起点是唯一历史边界，缺失状态仍保持为不可计算。
- **三种算法时间跨度不同，不可直接比大小**：t1 是固定10日；t2/t3 是截至各截止日动态确定起点的区间涨幅。三条曲线都严格以对应交易日为结束点。
  因此 y 轴标题统一为「涨幅高度」而不是「10日涨幅」，图例与正文都要保留这句提醒。
- 三条曲线共用 `y_max`（数据侧给定 = 全局最大值 × 1.15），SPA 不得各自缩放。
- x 轴起点 = 池内个股波段起点的最早值，可早于观察窗口；终点 = 最新交易日或用户指定。
- 每个观察日 t 用**当日**作波段结束，无前视偏差。

### 分市场序列 `pool_trends.by_market`

合并池会掩盖板块差异（实测 60 日：主板 102 只峰值 222%，科创成长板 136 只峰值 332%），
所以数据侧额外产出：

```text
pool_trends.by_market = { "沪深主板": {y_max, series[], coverage}, "科创成长板": {...} }
```

- `x` 轴复用父级 `pool_trends.x`，**不单独给**，避免同一张图两条时间轴。
- 每个市场有**自己的 `y_max`**：不同市场的量级不可共用一条 y 轴。
- 该分组取自 `qualified[].market_group`，因此只覆盖窗口内实际达标过的股票；
  `coverage.pool_union` 是该市场自己的池大小，不是全市场。
- 市场口径与文件名/标题必须一致（标题写「沪深主板」时，正文要说明口径含科创成长板）。

## 图表与表格的既有约定

- **参考线**：`refTiers(f, dataMax)` 画 100/200/300…% 绝对档位虚线，**只画 ≤ 该图实际最大值的档位**
  （高度图 max=160% 时只出现 100%，不会出现 200/300%）。y 轴上界仍按数据自适应，不为显示高档位而拉高。
- **断档标注**：`height.points[].change ≤ −20pp` 的当日，在日变化副图用深红柱 + 「断档」文字标出。
- **分屏 / 叠加**：`STATE.poolMode`（`split` / `overlay`），默认分屏；叠加是单图三条线，便于看两者差值。
- **分市场**：`STATE.poolMarket`（`all` / `沪深主板` / `科创成长板`）。
- **波段表分页**：`PAGE_SIZE = 25`，页码存 `STATE.page.{structure,emotion}`；切筛选或搜索时页码归零。
- **两套筛选维度不可混用**：`STATE.filters[kind]` 按 `status` 过滤（起点是否成立：已确认/待确认/数据不足/不达标），
  对应表内「起点状态」列；`STATE.endFilters[kind]` 按终点是否已确认过滤，对应「终点状态」列。
  结构表 `status` 恒为 `confirmed`（起点已确认且达标），**终点为空必须靠 `endFilters` 才能筛出**，
  否则会出现「76 行全显示已确认、终点列却全未确认」的矛盾观感。情绪表 `unconfirmed` 只算 `status ∈ {confirmed, pending}` 且终点为空的，
  `rejected` / `data_insufficient` 归入「不适用」，不当作未确认。
- **深链**：视图内部用 `<a href="#/pool">` 互相引用，避免正文写死「见第几张图」。

## AI 分析视图（`#/ai`）

数据来自 `ai_analysis`，由 `to_contract_data.py::build_ai_analysis` 按
`assets/ai_rules.json` 规则化合成，**不是 LLM 生成**，离线可复现、可审计。

- 渲染：状态卡表格（`fields[]`）+ 策略约束 kv（`advice`）+ 三条件三列（`conditions`）+ 免责声明。
- 口径、判定表、四类策略映射、未覆盖清单：见 `references/trading_framework.md`。
- 硬约束：`fields` 里凡属报告没有的数据（均线承载 / 板块承接 / 个股操作级别），
  值必须写成「未覆盖：…」，**不得用情绪高度反推**。
- 改阈值只改 `assets/ai_rules.json`；改判定逻辑才动 Python。

## 图表交互与 PNG 导出

所有图表统一走 `chartBox(svg, name)` 渲染，它产出一枚「导出 PNG」按钮 + 图表容器。

- 导出链路：`cloneNode` → 注入内联 `<style>`（SVG 脱离文档后 class 样式会丢）→
  序列化为 `data:image/svg+xml` → `Image` → `canvas`（2 倍分辨率）→ `canvas.toBlob` → `<a download>`。
- 全前端实现，无第三方库，离线可用；文件名 = `视图名_图表名_结束日.png`。
- 新增图表**必须用 `chartBox` 包一层**，否则没有导出按钮。
- 已知坑：参考线的 100/200% 标签必须画在折线**之后**（`refTierLabels`），
  否则会被数据线盖住；标签下要垫白底 `rect`，压线时才读得清。
- 导出按钮 `.chart-bar` 在打印媒体下隐藏。

## 降级语义

- 缺失值一律 `null`，渲染为 `—`；禁止空字符串、0 或省略键。
- 曲线 `points` 中缺数据的点填 `null`，SPA 断开折线而不是补零。
- `pending` 状态的 `end_date` / `trend_end_date` **必须为 `null`**，禁止用数据末端日期伪造终点。
- 候选不足或数据撑不起结论时，`summary.state = insufficient`，且 `summary.tldr` 必须写清原因。

## 已知覆盖缺口

`to_contract_data.py` 的 `build_pool_trends()` 从已算好的 `structure_waves` / `emotion_rallies`
里读取波段起点，因此 T2/T3 只覆盖「已跑过算法」的股票。
全市场规模下池内大量成员没有起点记录，两条曲线会偏低甚至断续。
彻底解法是在池内成员上就地调用 `calculate_structure_waves.py` / `calculate_emotion_rallies.py`
（或把两者抽成可 import 的函数），而不是依赖预计算结果。在解决之前，
`pool_trends.coverage.no_structure_start` / `no_first_board` 会把缺口数量显式暴露出来，
`validate_contract.py` 也会给出覆盖缺口警告——**不要把它当成 0 或者隐藏掉**。
