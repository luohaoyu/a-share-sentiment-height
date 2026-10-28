# AkShare 数据适配与协作边界

## 定位

将 AkShare 作为默认的本地 Python 数据接入层，而不是金融 MCP 的替代品。AkShare 适合无密钥获取交易日历、交易所股票名录、单股历史日线和收盘后的全市场快照；MCP/权威数据源更适合补齐历史 ST/风险状态、复权因子或按交易日全市场历史横截面。

## 接口映射

| 目的 | AkShare 接口 | 粒度 | 落盘 |
|---|---|---|---|
| 交易日历 | `tool_trade_date_hist_sina()` | 全区间一次 | `calendar.json` |
| 沪市主板名录 | `stock_info_sh_name_code(symbol="主板A股")` | 全板块一次 | 合并至 `stock_basic.json` |
| 科创板名录 | `stock_info_sh_name_code(symbol="科创板")` | 全板块一次 | 合并至 `stock_basic.json` |
| 深市名录 | `stock_info_sz_name_code(symbol="A股列表")` | 全市场一次 | 按 `板块` 拆主板/创业板 |
| 北交所名录 | `stock_info_bj_name_code()` | 全市场一次 | 合并至 `stock_basic.json` |
| 主源（默认开通） | `stock_zh_a_daily(..., adjust="qfq")`（新浪） | 单股一次 | `price_store/<ts_code>.json` 的 `qfq[]`（`price_mode=akshare_sina_qfq`） |
| 备源 | `stock_zh_a_hist(..., adjust="qfq")`（东财，默认关闭）、`stock_zh_a_hist_tx(..., adjust="qfq")`（腾讯） | 单股一次 | 主源失败后按 roster 换源，`price_mode` 记录实际命中源 |
| 增量更新 | **无独立增量接口**：仍走 `bootstrap`，由 `cache_refresh.sessions_to_fetch` 判定缺哪些交易日 | 单股一次（只对需要更新的股票） | 追加 `qfq[]` 行 → 重建 `daily_adjusted/<date>.json` |
| 涨停判定（is_limit_up） | 本地 K 线按官方涨跌幅限制计算（不调用任何三方接口） | 无网络依赖，每次写入 `price_store` 前重算 | `price_store/<ts_code>.json` 的 `qfq[].is_limit_up`（派生字段，非权威层） |
| 候选股派生 | **不再有网络请求**：`derive_cache.py --mode derive --qfq-symbols` | 0 次请求 | `forward_daily/`（前复权四价）与 `unadjusted_daily/`（+ `is_limit_up`） |

## 为什么是"逐股一次 qfq + 按交易日增量"

AkShare 没有"指定历史交易日一次返回全 A 股日线横截面"的稳定接口，三个 K 线接口都只能逐股调用；每天都对五千余只股票重复调用会触发限流且耗时很长。因此：

1. 首次建库只拉 `N + lookback + buffer` 个交易日的窗口，v5 每只股票只抓**前复权一条腿**；
2. 每只股票独立缓存（`price_store/<ts_code>.json`），失败后重跑只补缺失股票；
3. **吞吐天花板来自上游、不是本地参数**（300/200 只样本对照压测）：
   - `--rate-limit 12 / 16 / 20` 三档墙钟完全相同（约 115 s / 300 只）；
   - `--rate-limit 0`（关闸门）8.9 req/s ≈ `--rate-limit 9` 的 8.8 req/s；
   - `--workers 32` 与 `--workers 16` 同样无差异。
   结论：**瓶颈在上游节流（约 8.8 req/s）**。要真正缩短建库时间只能减少请求数——v5 已把每只股票从 2 次请求降到 1 次，下一步只能换按交易日返回横截面的批量源（如 Tushare `daily`+`adj_factor`，18 个交易日仅 36 次调用）。
   另注：一次全新缓存目录的 bootstrap 有约 30 s 固定开销（calendar + 名录），与股票数无关。
   注意 `--rate-limit` 现在只是"**全源开关**"（>0 全开、0 全关），不再限制请求速率；每源启停一律走 `--source-rates`。
4. 增量按**交易日**判定：`cache_refresh.sessions_to_fetch` 算出缓存缺哪些会话，只对缺这些日期的股票发请求；缓存已有的会话不重抓；
5. 不跨缺口推进：若中间缺任何交易日，先回填，不做跨缺口外推；
6. **涨停判定随写入派生**（is_limit_up）：每次写 `price_store` 前用前一日的 `qfq` 收盘按官方涨跌幅限制（主板 10% / 科创板·创业板 20% / 北交所 30%，ST 5% 暂不启用）本地计算，结果作为 `qfq[].is_limit_up` 与价格同源落盘；不调用任何三方涨停池接口，下游只读缓存；
7. **实时会话判定**：`live_session_of(sessions)` 取"不晚于今天的最新交易日"；因为涨停判定是本地的纯 K 线派生，每次运行重算即可自然保持最新，无需任何"网络刷新"或"覆盖写"；
8. **不再有 snapshot / waves 模式**：v5 把两者并入 `bootstrap`——`--mode` 只有 `bootstrap` / `status`。照旧文档敲 `--mode snapshot` 或 `--mode waves` 会被 argparse 直接拒掉。

## 进程分片（`--shards`）：绕开 GIL 的唯一有效手段

> 单进程加线程的收益很早就封顶，见上一节实测。

单进程下加线程的收益很早就封顶：每只股票约 **0.5 s CPU**，其中 **约 320 ms 是 AkShare 内部 `py_mini_racer` 对新浪 JS 混淆行情的解码**（每只股票两个腿各一次 160 ms，解出 8000+ 行却只用 45 行）。这段是纯 Python/原生 CPU 且不释放 GIL，所以 `--workers 16/32/48` 的实测吞吐只从 1.87 只/秒爬到 2.99 只/秒，而机器有 16 个逻辑核、实际只用到 1.4 核。

`--shards N` 把股票池按 `ts_code` 连续切片交给 N 个 OS 进程，每个进程仍跑原有的线程池（`--workers`），从而真正并行：

```bash
python scripts/fetch/fetch_akshare_data.py --mode bootstrap --output-dir ./cache/akshare \
  --end-date 2026-10-23 --target-count 30 --lookback 10 --buffer-sessions 30 \
  --shards 8 --workers 6 --retries 3 --shard-retries 2
```

- 父进程只拉一次 calendar/stock_basic，然后并行起子进程；**只有父进程写 `bootstrap_manifest.json` 与 `daily_adjusted/`**，子进程写 `_shards/part<i>/{manifest.json,daily/}`，由父进程按 `ts_code` 去重合并。这样避免 N 个进程对同一分区做 read-modify-write 丢行。
- 断点续跑语义不变：仍以 `price_store/<ts_code>.json` 落地为准；子进程重跑时已落盘的股票跳过网络、分区行由 store 重建。

**实测（2026-10-26，5577 只，45 个交易日窗口）**：单进程 48 线程稳态 2.99 只/秒（整轮约 31–36 min）；`--shards 8 --workers 6` 稳定 7.38 只/秒，整轮 13.5 min（**约 2.5×**），等效核数从 1.4 升到 1.9–2.4。

两个只在这个模式下才会遇到的坑，代码里已修，勿回退：

1. **V8 并发构造会直接 abort 进程**：`[FATAL:partition_address_space.cc(243)] Check failed: !IsConfigurablePoolInitialized()`。AkShare 每次调用都 `new MiniRacer()`，多线程并发构造时 V8 二次初始化 partition allocator 并 abort —— 这是进程级崩溃，`try/except` 抓不住。首版 8 分片实测 8 个进程里死了 4 个。修法：`install_shared_v8()` 把 `py_mini_racer.MiniRacer` 换成"全进程单例 + 锁"，构造只发生一次。
2. **共享 V8 上下文会阻止进程退出**：V8 留下非守护线程，解释器正常退出被卡住，父进程永远认为子进程没跑完（实测 8 个分片全部打完却全部挂住）。修法：`__main__` 里刷完缓冲区后 `os._exit(code)`。
3. **分片会放大上游失败**：8 分片实测把 eastmoney 的 `RemoteDisconnected` 死信从 22 只推到 149 只（全部 `tried_sources=['eastmoney','eastmoney','eastmoney']`）——见下面「源开关与重试默认值」。

## 源开关与重试默认值（2026-10-26 变更）

**eastmoney 默认关闭**（`DEFAULT_SOURCES_ENABLED = {"sina": true, "eastmoney": false, "tencent": true}`）。依据是分源实测（按尝试次数统计）：

| 源 | 尝试占比 | 失败率（单进程） | 失败率（8 分片） | 延迟 p50 |
|---|---|---|---|---|
| sina | 51.3% | 0.1% | 0.0% | 7.54 s → 1.05 s |
| eastmoney | 30.0% | **47.2%** | **99.0%** | 0.56 s（秒挂） |
| tencent | 18.7% | 0.0% | 0.0% | 6.38 s |

它分到 30% 权重却只贡献 14.5% 的成功量，还把总请求量抬高约 16%（919 次失败尝试）。需要时写一个 JSON 文件再传路径（该参数是 `Path`，**不接受内联 JSON**）：

```bash
printf '{"eastmoney": true}' > ./run/src.json
python scripts/fetch/fetch_akshare_data.py --mode bootstrap --output-dir ./cache/akshare \
  --end-date 2026-10-26 --source-rates ./run/src.json
```

**单次尝试内部的 HTTP 重试默认关闭**（`--retries 1`）。`call_with_retry` 的退避是 `2^attempt + random`，实测 retries=3 时三次失败要睡 6.85 s；对 eastmoney 这种"秒挂"源是纯浪费，而任务级重投本来就会重新抽源。现在失败直接上抛，由 `--task-attempts`（默认 3）在任务层换源重试；要彻底不重试就 `--task-attempts 1 --retries 1`。

> `--rate-limit` 现在只是全源开关（0 = 关掉所有源），不再控制限速、也不再"一键全开"；每源的启停一律走 `--source-rates`。

v5 **不再用官方涨跌幅合成调整价**：旧的 `adjusted_close(t) = adjusted_close(t-1) × (1 + pct_chg(t)/100)` 递推路径已废弃——每只股票的 `qfq[]` 是接口直取的真实前复权序列，不存在"合成价"与"真实价"两套口径。`price_mode` 现在只标注实际命中源（`akshare_sina_qfq` / `akshare_tencent_qfq` / `akshare_eastmoney_qfq`）。若在旧缓存里看到 `price_mode=synthetic_adjusted_from_official_pct_change`，那是 v3/v4 遗留，新缓存不应再出现。

## 涨停判定（is_limit_up）计算方法与缓存

涨停状态**不是**主流程（`bootstrap` / 筛选）的必需输入；主指标"短线情绪高度"只算 `close(t)/close(t-10)-1`，不需要涨停状态。涨停数据服务于 `derive` 之后的"首板情绪波段"（`unadjusted_daily/` 的 `is_limit_up`）。

`is_limit_up` 自 2026-10-26 起**由本地 K 线按官方涨跌幅限制直接计算，且与收益率/波段等指标统一为前复权（qfq）口径**，不再调用任何三方涨停池接口（旧的 `stock_zt_pool_em` 资产化方案已移除——本环境实测该接口会挂起导致整轮采集卡死）。计算规则：

- **计算口径（前复权）**：`prev_close` 与 `close` 取**前复权**价（= 未复权收盘 × 后复权因子；涨停判定是单日比值，前复权锚点因子在比值中相消，故数值等价于"未复权×因子"）。非除权日与未复权完全等价；**除权除息日**由前复权序列吸收公司行为（分红导致的价格跳空不算涨停），使涨停口径与收益率/波段指标彻底统一；
- **官方涨跌幅限制**：主板（沪市 60 / 深市 00 开头，除科创板）10%，科创板（688/689）与创业板（300/301）20%，北交所（8/4 开头）30%；ST 5% 因无可靠历史 ST 状态暂不启用；
- **涨停价**：`limit_up_price = round(prev_close × (1 + ratio), 2)`（<1 元时用 0.01 最小变动单位），其中 `prev_close` 为前复权前收盘价；
- **判定**：`close >= limit_up_price - 0.005`（留一个 tick 的四舍五入余量）；首笔交易日无前驱 `prev_close`，记为 `None`（下游判 `data_insufficient`）；
- **本地派生、每轮重算**：`is_limit_up` 是 `price_store/<ts_code>.json` 中 `qfq[]` 行的派生字段，每次写入 `price_store` 前由 `annotate_limit_up()` 重算；无网络依赖、无三值 `pool_status` 概念、无"超出保留窗口不可补"问题——只要 K 线齐全即可判定；
- **下游只读**：`forward_daily/`、`unadjusted_daily/` 的 `is_limit_up` 由 `price_store` 派生投影，不单独落盘来源；计算层只读 `unadjusted_daily/` 的标记做首板识别。

### 全市场涨停 / 连板统计读哪一层

`daily_adjusted/<date>.json` 每行只有 `ts_code/open/high/low/close/price_mode`，**没有 `is_limit_up`**。要统计"每日涨停家数 / 连板梯队"必须直接扫 `price_store/*.json` 的 `qfq[].is_limit_up`（`calculate_limit_up_ladder.py` 即如此实现），读横截面分区会静默得到全零。

该脚本的口径与限制：
- 只统计沪深主板 / 创业板 / 科创板，ST 与上市不足 N 个自然日的次新股排除在外（ST 5% 涨跌幅上游未启用，纳入会系统性漏判），北交所不计；
- 连板 = 连续交易日 `is_limit_up is True` 的长度；缺行或 `is_limit_up is None` 记为"数据不足"，**不断板也不计入**（停牌复牌再涨停在真实连板里是延续的）；
- 缓存窗口最早交易日的连板无法确认前序，输出 `window.lower_bound_before` 标注为下限，越靠近窗口起点被低估越多。


## 整合缓存契约（schema v5）

`fetch_akshare_data.py` 输出的缓存分为「权威层」与「派生层」两层：权威层只存**不可再生**的观测，派生层全部可由权威层重建（`scripts/fetch/derive_cache.py --mode derive`）。`validate_cache_contract.py` 按 store 内的 `schema_version` 自动分支校验（报告的 `layout` 为 `v5|v4|v3`）：

| 层 | 路径 | 形态 | 说明 |
|---|---|---|---|
| **权威·价格** | `price_store/<ts_code>.json` | `{schema_version:5, ts_code, list_date, price_mode, basis:"qfq_direct", definition, last_update, qfq:[{trade_date, open/high/low/close, is_limit_up}]}` | **唯一价格权威**：单条前复权四价序列。**没有 raw 腿、没有 factor / anchor_factor / events** |
| **派生·涨停标记** | 同一文件的 `qfq[].is_limit_up` | 布尔（首笔 `None`） | 本地 K 线按官方涨跌幅限制派生，每次写入前重算，非独立权威层 |
| 派生·横截面 | `daily_adjusted/<date>.json` | 纯 list（`ts_code/trade_date, open/high/low/close, price_mode`） | 按交易日全市场前复权四价，计算层唯一输入（bootstrap 写） |
| 派生·作用域 | `forward_daily/`、`unadjusted_daily/` | **纯 list** | 仅候选作用域（39–77 只）：前复权四价、未复权四价 + `is_limit_up`。**必须跑 derive 才有** |
| 台账 | `bootstrap_manifest.json`、`cache_v4_manifest.json` | dict | 含 `schema_version`（=5）、`coverage{first_session,last_session,session_count}`、`qfq_scope`、`symbols_without_qfq`、`historical_st_status` |

v5 不存在 `symbol_cache/`、`waves_manifest.json`、`snapshot_manifests/`、`st/` 这些目录——旧文档提到它们时说的都是 v3/v4 或已废弃的采集路径。

**精度契约**：v5 的价格就是接口返回的前复权值，不做任何乘算，因此**没有 v4 那种"因子回乘差 1 个 tick"的精度损失**。唯一的口径风险是"绝对前复权值会随新的除权事件整体重锚"，但下游只消费相邻日比值，比值在重锚下不变。


`validate_cache_contract.py --cache-dir <dir> --sample 200` 在 **v5** 下校验：`price_store` 的 `schema_version == 5`、`qfq[]` 四价键齐全、价格为有限数、`is_limit_up` 为 bool/null；并抽样重新派生与 `daily_adjusted`/`forward_daily` 逐值比对，以发现过期的派生产物（过期的分区本身看起来完全合法，这是唯一能识别它的检查）。v4 遗留缓存仍按 raw+factor+anchor 校验，两代共用同一份报告字段。`--sample` 控制"抽样派生比对"的数量，**深检数量由 `--deep-sample` 控制**（默认 300）。

## ST 数据边界

AkShare 的交易所名录和实时快照可识别当前名称中的 `ST`、`*ST`、退市等标识，但不能稳定提供所有历史日期的风险警示生效区间。因此：

- **当前 `fetch_akshare_data.py` 不产出 `st/` 目录**：`bootstrap_manifest.summary.historical_st_status` 恒为 `not_available_from_this_adapter`。任何"当日快照落盘到 `st/YYYY-MM-DD.json`"的旧描述都不成立；
- 历史报告只能基于**当前名称**做风险过滤，不能称为完整历史 ST 数据；
- 因此运行计算脚本时必须使用 `--st-coverage partial --allow-name-only-st` 显式确认降级（`screen_market_threshold_gainers.py` 会强制这项组合：不给 `--st` 又不加 `--allow-name-only-st` 会直接报错退出）；
- 若将来由 MCP/交易所数据提供历史风险状态，把它整理成日期分片目录后用 `--st <目录> --st-coverage complete` 显式启用，并在报告里改写 ST 过滤说明。

`stock_info_change_name()` 只能返回曾用名称列表，没有可靠的每个名称生效起止日期，不能用于严格历史 ST 过滤。

## Skill 与脚本分工

### Skill / Agent

负责：

- 把用户需求冻结为参数；
- 判断目标日期是否已收盘；
- 决定使用 AkShare、MCP 补齐或网页降级；
- 审核日历末日、股票池规模、失败率、缺失组合和 ST 覆盖；
- 遇到 AkShare 字段变化时停止并诊断，不直接猜字段；
- 解释风险与数据限制。

### `fetch_akshare_data.py`

负责：

- 固定调用已验证接口；
- 做字段映射、限速、重试、断点续传和原始缓存；
- 输出稳定的 `calendar / calendar_upstream / stock_basic / price_store / daily_adjusted` 契约（**不写 `st/`，也不写 `forward_daily`/`unadjusted_daily`**）；
- 为每次请求生成 manifest；
- 单股失败时记录并返回非零退出码。

### `compute_market_winners_from_daily.py`

负责：

- 完全离线地读取标准契约；
- 对齐 `N + gain-lookback + buffer` 交易日窗口；默认 N=30、gain-lookback=10、buffer=30，即缓存 70 个交易日；
- 计算 10 日收益；
- 合并股票主数据；
- 去 ST、去新股；
- 按交易日和市场排序取最大值；
- 输出质量计数、过滤审计和缺失组合。

### 校验和报告脚本

负责验证结构与资格，再把结果渲染成 HTML/SVG。不得反向修改计算结果。

## 执行状态机

```text
Skill 冻结口径
  → 检查 AkShare 环境与缓存（--mode status）
  → 缓存为空：bootstrap
  → 缓存已有且目标日缺失：确认收盘后跑 bootstrap（按交易日增量）
  → 校验缓存契约（validate_cache_contract，layout 应为 v5）
  → 逐日筛选出候选池（screen_market_threshold_gainers）
  → derive 候选作用域（derive_cache --mode derive --qfq-symbols）
  → 三口径计算（structure_waves / emotion_rallies / 合并校验）
  → 涨停连板计算与校验（calculate_limit_up_ladder / validate_limit_up_ladder，读 price_store）
  → build report（build_report_spa，带 --limit-up-ladder）
  → Skill 审核警告并交付
```

## 安装与版本

将依赖安装到 WorkBuddy 管理的 Python 虚拟环境，不污染系统 Python：

```bash
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/pip.exe install akshare pandas
```

每次运行把 `akshare.__version__` 写入 manifest。升级 AkShare 后，先用 `--symbols 600519 000001 300750 688981 920xxx` 选取各市场小样本回归，再执行全市场回填。北交所样本应从当期 `stock_info_bj_name_code()` 返回结果中选择，不硬编码不存在的代码。

## 数据质量闸门

- 日历最后日期早于目标日：停止；
- 名录为空或市场缺失：停止；
- 历史接口返回空表：记录失败，不补零；
- 单股目标窗口不足：排除并记录；
- 每日快照缺少上一交易日分片：停止；
- 请求失败率大于 0：bootstrap 返回非零；
- 计算缺失日期×市场组合：返回非零；
- ST 覆盖不完整：报告必须显示降级警告。
