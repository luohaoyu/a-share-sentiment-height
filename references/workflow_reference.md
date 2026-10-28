# 分市场 10 日区间涨幅流程参考

## 1. 分析对象

默认按每个交易日、每个市场分别求：

```text
argmax(stock, 10日区间涨幅(stock, trade_date))
```

若用户未要求分市场，而是要求“每个交易日全市场最大个股和值”，应在完成同一套市场归属、ST/风险和新股过滤后，跨已配置市场合并候选，再求每个交易日唯一冠军。确定性脚本通过 `--overall` 开启该模式，输出市场字段为“全市场”，并保留实际市场字段 `market_source`。

在求最大值前执行资格过滤：

```text
market_match
AND not_st
AND not_delisting_risk
AND not_new_stock
AND listing_date_verified
```

## 2. 10 日区间涨幅定义

推荐使用前复权收盘价：

```text
return_10d(t) = adj_close(t) / adj_close(t-10 trading sessions) - 1
```

这对应 11 个收盘点之间的 10 段价格变动。若供应商的 `Chg10D` 定义不同，必须在报告中记录供应商原始定义，不要擅自改名。

## 3. 新股过滤

默认规则：

- 名称以 `N` 或 `C` 开头：直接标记为新股候选；
- `listing_age_days = trade_date - listing_date`；
- `listing_age_days < 60`：排除；
- 上市日期查不到：排除并记为“未能核验上市日期”。

该规则是工程化默认值，不是交易所唯一官方定义。用户指定其他阈值时，以用户阈值为准并记录版本。

## 4. ST 与风险标识

至少排除：

- `ST`、`*ST` 开头名称；
- 退市整理、退市风险、终止上市等明确风险标识；
- 数据源风险状态字段中标记为风险警示的证券。

不能只通过代码判断 ST，也不能只排除名称等于 `ST` 的情况。

## 5. 批量日线主链路

优先把数据源返回结果按以下五类落盘，再运行 `scripts/compute/compute_market_winners_from_daily.py`：

| 数据集 | 最小字段 | 推荐请求粒度 | 缓存策略 |
|---|---|---|---|
| 交易日历 | `cal_date, is_open` | 一次查询覆盖完整区间 | 年度或区间文件，长期缓存 |
| 全市场日线 | `ts_code, trade_date, close` | 每个交易日一次全市场横截面 | `daily/YYYY-MM-DD.json`，历史永久缓存 |
| 复权因子 | `ts_code, trade_date, adj_factor` | 每个交易日一次全市场横截面 | `adj_factor/YYYY-MM-DD.json`，历史永久缓存 |
| 股票主数据 | `ts_code, name, market, exchange, list_date, list_status` | 按上市状态分批 | 单一快照，定期刷新 |
| 历史 ST 快照 | `ts_code, trade_date, name/type` | 每个交易日一次 | `st/YYYY-MM-DD.json`，历史永久缓存 |

Tushare 风格 JSON 的 `data.fields + data.items`、普通对象数组 JSON 和 CSV 均可直接作为脚本输入。目录参数会合并该目录下所有 `.json` 和 `.csv` 分片。

本地复权口径：

```text
adjusted_close(t) = close(t) × adj_factor(t)
return_10d(t) = adjusted_close(t) / adjusted_close(t-10) - 1
```

该比值对复权因子的整体缩放不敏感，可直接计算区间收益。若供应商已经返回统一口径的前复权或后复权收盘价，使用 `--prices-adjusted`，不得再次乘复权因子。

增量执行时先检查每个目标交易日对应的 `daily`、`adj_factor` 和 `st` 分片，只有文件缺失或上次请求失败时才重新取数。目标 N 日需要 N+10 个已完成交易日；不要为 30 日报告下载多年全市场历史数据。

## 6. 来源与复现

每个结果记录：

- 来源 URL 或接口名；
- 查询日期/数据时点；
- 原始字段名；
- 复权方式；
- 请求是否成功；
- 缓存文件或本地中间数据路径（如有）。

对网页榜单，保留原始页面缓存和解析行数；对接口数据，保留请求参数摘要和返回记录数，但不要在报告中暴露密钥。

## 7. 结果数据结构示例

```json
{
  "trade_date": "2026-10-23",
  "market": "创业板",
  "code": "300000.SZ",
  "name": "示例股份",
  "pct_10d": 42.18,
  "listing_date": "2010-01-01",
  "new_stock_threshold_days": 60,
  "qualified": true,
  "source": "historical_rank_endpoint",
  "source_timestamp": "2026-10-23 close"
}
```

## 8. 确定性脚本与 Agent 边界

确定性脚本可以稳定执行：

- 抓取与缓存固定 URL；
- HTML/JSON 字段解析；
- 市场代码分类；
- ST、退市风险和新股过滤；
- 10 日涨幅计算；
- 排序、取冠军和并列处理；
- 记录数、字段、重复和缺失校验；
- JSON、SVG 和 HTML 渲染。

Agent 必须负责：

- 将用户自然语言转换为参数；
- 选择和验证数据源能力；
- 判定数据源是否完整、是否存在展示阈值；
- 判断复权和 `Chg10D` 口径是否一致；
- 解释异常值、请求失败和跨源差异；
- 记录真实发生的工具调用，不得由脚本虚构。

脚本发现问题时应返回明确错误或警告。Agent 不得为了得到完整表格而忽略非零退出码、把缺失填成 0 或把未核验标的纳入结果。

## 9. 报告最小结构

```text
标题
统计口径卡片
数据时点与来源
分市场趋势图
每日×市场明细
过滤审计
工具调用链路
流程日志
数据限制与风险提示
```

趋势图用静态 SVG 做离线降级；表格将数据直接嵌入 HTML；任何缺失组合都展示缺失原因，不填 0。

趋势分析区展示规范：

- 默认展示沪市主板、深市主板；创业板、科创板、北交所通过市场切换控件查看。
- 按 10 日区间涨幅分成六档：`0%—30%`、`30%—50%`、`50%—100%`、`100%—200%`、`200%—300%`、`300%+`。
- 区间颜色表达短期情绪强度，不能表达收益确定性；必须同时展示“情绪越强，波动与尾部风险越大”的提示。
- 市场切换只改变可视化层，不改变底层冠军数据、过滤结果和审计记录。
- JSON 中保留 `trend_ui.default_markets`、`trend_ui.available_markets`、`trend_ui.gain_bands` 和 `trend_ui.risk_notice`。

## 10. 三口径扩展（顶底分型结构 + 首板情绪波段）

在 10 日情绪高度之外，对阈值候选逐股增加两类波段高度，用于观察符合条件个股的完整走势高度：

- **顶底分型结构**：前复权日线先按时间顺序迭代消除包含关系，再在合并K线上识别并去重顶底分型；选择最新达标日前最近一个已确认底分型作为起点。只做到分型层，不使用MA5，也不构造笔、线段或中枢。
- **首板情绪波段**：以显式 `is_limit_up` 识别首板（当日涨停、前一日未涨停、此前 4 个交易日无涨停），跟踪前复权最高价，连续 4 个交易日不创新高确认结束；涨停状态只能来自上游数据，不得从涨跌幅或收盘价推断。

两类波段都对阈值候选按 6 位代码去重后逐股运行顺序状态机（不同股票可并发），再群体汇总；同股多日命中只计算一次但保留全部命中日期。合并校验 `validate_three_height_metrics.py` 强制三类 JSON 的候选代码集合与逐股命中日期完全一致。

### 数据前置条件

- 结构段：需要每只候选股的 `open/high/low/close` 四价齐全且同尺度。
- 情绪段：需要 `forward-daily`（前收/前高）与 `unadjusted-daily`（未复权收盘 + 显式 `is_limit_up`）。

v5 起每只股票只抓一条前复权序列，`price_store/*.json` 的 `qfq[]` 已含完整四价。**该缺口通过 `derive_cache.py --mode derive --qfq-symbols <候选 ts_code>` 补齐**（纯离线、零请求）：由 `price_store` 派生出 `forward_daily/`（前复权四价）与 `unadjusted_daily/`（未复权 + `is_limit_up`），涨停标记由本地 K 线按官方涨跌幅限制派生（不调用 `stock_zt_pool_em`）。窗口由 bootstrap 的 `--buffer-sessions`（默认 5）保证覆盖最早 hit_date 之前 5 个交易日。跑这两段脚本前先执行 derive 即可——**`--mode waves` 已不存在**，照旧敲会被 argparse 拒绝。

## 11. 涨停连板梯队（每日涨停家数 / 连板数）

与第 10 节互补：第 10 节是"达标个股能涨多高"，本节是"整个市场的涨停广度与连板高度"，不看 10 日涨幅阈值。

```bash
python scripts/compute/calculate_limit_up_ladder.py \
  --price-store ./cache/akshare/price_store \
  --calendar ./cache/akshare/calendar.json \
  --stock-basic ./cache/akshare/stock_basic.json \
  --end-date 2026-10-26 --window-days 30 \
  --output ./run/limit_up_ladder.json

python scripts/validate/validate_limit_up_ladder.py \
  --input ./run/limit_up_ladder.json --expected-dates 30
```

**必须读 `price_store/`，不能读 `daily_adjusted/`**：横截面分区每行只有 `ts_code/open/high/low/close/price_mode`，没有 `is_limit_up`，读它会得到全零而不报错。`price_store/<ts_code>.json` 的 `qfq[].is_limit_up` 才是权威标记。

口径（脚本内冻结，不接受运行期切换）：

| 项 | 取值 |
|---|---|
| 市场 | 沪市主板、深市主板、创业板、科创板（北交所不计） |
| 排除 | ST/*ST/退市风险名称；上市不足 `--min-listing-days`（默认 60）个自然日 |
| 连板 | 连续交易日 `is_limit_up is True` 的长度 |
| 缺口 | 缺行或 `is_limit_up=None` 记"数据不足"，不断板、不计入涨停 |

逐日输出 `total`（涨停家数）、`first_board`、`consecutive`（二板及以上）、`by_boards`（板数 → 家数）、`max_boards`、`consecutive_ratio`、`by_market`，以及 `ladder`（板数 ≥ `--ladder-min-boards` 的个股明细）。`by_boards` 求和必须等于 `total`，校验器与契约校验器都会拦。

两个必须写进报告的限制：

1. **下限**：缓存最早交易日的连板无法确认前序，`window.lower_bound_before` 标注该日及以前只能视为下限；
2. **只认收盘涨停**：日线层面无法识别盘中触板后炸板、封板时间、开板次数与封单金额，报告里必须明说。

报告接入：`build_report_spa.py --limit-up-ladder ./run/limit_up_ladder.json`。不传该参数时 `limit_up=null`，SPA 自动隐藏"涨停连板"视图；`summary.state` 的判定口径与本节无关，不因新增指标而变化。
