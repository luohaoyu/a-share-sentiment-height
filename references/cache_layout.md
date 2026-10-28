# 缓存目录结构说明（Cache Layout · schema v5）

本文档说明 `scripts/fetch/fetch_akshare_data.py` 在 `--output-dir`（实际运行根目录为 `cache/akshare`）下生成的全部缓存结构与维护方式。产物**均由脚本自动生成，无需手工编辑**；当前契约版本由 `fetch_akshare_data.SCHEMA_VERSION = 5` 标记，可用 `scripts/validate/validate_cache_contract.py` 自检（报告里的 `schema_version` / `layout` 都应为 `5` / `v5`）。

> 本文档描述 **v5（qfq-only）**。若遇到 v3（`symbol_cache/` + 四份镜像）或 v4（`raw` + 每交易日 `factor`）的旧缓存，见文末「历史版本」一节；校验器会按 store 内的 `schema_version` 自动分支，不必手工迁移即可读。

---

## 1. 三代缓存的关键差别

| 项目 | v3 | v4 | **v5（当前）** |
|---|---|---|---|
| 价格权威层 | `symbol_cache/`（每行 12 个价格） | `price_store/`（4 未复权价 + 1 复权因子/行） | **`price_store/`（单条前复权 `qfq[]`，每行四价 + `is_limit_up`）** |
| 复权怎么来 | 采集时 hfq/qfq/raw 各落一份 | 采集 hfq + raw，`raw × factor` 派生 | **直接采集 `adjust="qfq"`，不做乘法** |
| 单股请求数 | 2（raw + hfq/qfq） | 2（hfq + raw） | **1（qfq 一次拿全）** |
| 除权/因子机制 | 有 | 有（`anchor_factor` + `events`） | **无**（比值下游不需要） |
| 绝对价漂移风险 | 有（新旧基准混排） | 由因子版本化控制 | **表面存在、实际无影响**（下游只用比值） |
| `is_limit_up` | 三方涨停池 | 本地 K 线前复权口径派生 | 本地 K 线前复权口径派生 |
| 磁盘（5577 只 × 45 日） | — | 约 39 MB（20 日） | 约 34 MB |

v5 为什么功能零损失：`compute/` 只消费两类量——**相邻日比值**（10 日涨幅、`is_limit_up` 判定）——复权基准在比值里相消，因此"绝对 qfq 值是否会随新除权漂移"这件事对结果没有影响。完整论证与取舍见 `references/redesign_qfq_perday.md`。

---

## 2. 目录树

```
cache/akshare/                         # --output-dir 根
├── calendar.json                      # 交易日历（已按 --as-of 截断，唯一消费视图）
├── calendar_upstream.json             # 上游原始预排表（含未来交易日，仅审计）
├── stock_basic.json                   # 全市场 A 股主数据（5577 只）
├── price_store/                       # ★唯一价格权威（bootstrap 写）
│   └── <ts_code>.json                 #   单条前复权 qfq[] 四价 + is_limit_up
├── daily_adjusted/                    # 派生·横截面（bootstrap 写）
│   └── <YYYY-MM-DD>.json              #   按交易日分区的全市场前复权四价
├── forward_daily/                     # 派生·作用域（derive 写，仅候选股）
│   └── <ts_code>.json
├── unadjusted_daily/                  # 派生·作用域（derive 写，仅候选股）
│   └── <ts_code>.json
├── _shards/                           # 仅 --shards>1 时存在：各分片中间产物与日志
├── bootstrap_manifest.json            # bootstrap 运行摘要 + 逐股状态
└── cache_v4_manifest.json             # derive 台账（文件名沿用 v4 时代，内容是当前派生作用域）
```

> **两层结构**：`price_store/`（外加 `calendar` / `stock_basic`）是权威层；`daily_adjusted/`、`forward_daily/`、`unadjusted_daily/` 全部可由权威层**派生**。派生可以随时删掉重建，权威层不能。
>
> **注意**：`forward_daily/` 与 `unadjusted_daily/` **不是 bootstrap 的产物**，只有跑过 `derive_cache.py --mode derive` 才会出现，且只覆盖候选作用域（典型 39–77 只）。仅跑 bootstrap 时它们缺失是正常状态，不是漏数据。

---

## 3. 权威层：`price_store/<ts_code>.json`

```json
{
  "schema_version": 5,
  "ts_code": "002724.SZ",
  "list_date": "2014-11-04",
  "price_mode": "akshare_tencent_qfq",
  "basis": "qfq_direct",
  "definition": "qfq fetched directly; downstream consumes ratios only",
  "last_update": "2026-10-26T19:33:08+08:00",
  "qfq": [
    {"trade_date": "2026-10-24", "open": 11.05, "high": 11.30, "low": 11.02,
     "close": 11.20, "is_limit_up": false},
    {"trade_date": "2026-10-26", "open": 11.10, "high": 11.96, "low": 11.10,
     "close": 11.90, "is_limit_up": false}
  ]
}
```

| 字段 | 语义 |
|---|---|
| `schema_version` | 恒为 `5`。校验器以它判定 `layout: v5`（不看目录形状，因为 v4/v5 目录一样） |
| `price_mode` | 该股实际命中源：`akshare_sina_qfq` / `akshare_tencent_qfq` / `akshare_eastmoney_qfq`。**同一缓存内不同股票可能来源不同**，这是混合源的正常结果，不是不一致 |
| `basis` | 恒 `qfq_direct`：表示"直接向前复权，不是乘因子得到的" |
| `definition` | 口径自述，供审计；下游只消费比值 |
| `last_update` | 该股 store 的写入时间（本地时区 ISO8601） |
| `qfq[]` | 每个交易日一行，`trade_date` 严格升序；四价 `open/high/low/close` 均为**前复权**值，四舍五入到 0.01。四价**始终存在**（缺列为 `null`，不补收盘价） |
| `qfq[].is_limit_up` | `true` 涨停 / `false` 未涨停 / `null` 首笔无前驱（**未知**，禁止推断） |

**没有的东西**（读 v4 文档时最容易找错）：没有 `raw[]`、没有 `adjust{}`、没有 `factor`/`anchor_factor`/`events`/`detection_floor_pct`、没有 `official_pct`。如果你在 v5 store 里找不到因子——这是对的。

`is_limit_up` 的判定口径（本地派生，每轮重算，无网络、无三方涨停池）：

- 前复权口径下比较 `close` 与 `round(prev_close × (1 + ratio), 2) - 0.005`；单日比值中复权基准相消，与收益率口径天然一致；
- 涨跌幅上限：主板 10%、科创板/创业板 20%、北交所 30%（ST 5% 暂不启用，因为无可靠历史 ST 状态）；
- 首笔交易日无 `prev_close` → `null`，下游判 `data_insufficient`，**不允许**从涨跌幅反推。

---

## 4. 派生层（可删可重建）

| 路径 | 形态 | 内容 | 谁写 | 消费方 |
|---|---|---|---|---|
| `daily_adjusted/<YYYY-MM-DD>.json` | 纯 list | `{ts_code, trade_date, open, high, low, close, price_mode}`，close 为前复权 | bootstrap（父进程合并分片后写） | `screen_market_threshold_gainers.py`、`compute_market_winners_from_daily.py` |
| `forward_daily/<ts_code>.json` | 纯 list | `{ts_code, trade_date, open, high, low, close}`，前复权 | derive（仅候选作用域） | `calculate_structure_waves.py`、`calculate_emotion_rallies.py` |
| `unadjusted_daily/<ts_code>.json` | 纯 list | 同上 + `is_limit_up` | derive（仅候选作用域） | `calculate_emotion_rallies.py` |

关于 `unadjusted_daily` 的一个实测注意点：v5 权威层只存前复权价，所以这一层的"未复权"是由 store 重投影得到的镜像。**若窗口内该股没有除权除息，它与 `forward_daily` 逐值相同**（002724.SZ 2026-08-17～10-26 即是如此）。这不是 bug，但意味着**不能靠比对这两个目录来判断复权是否生效**——要判断复权，只能跨越同一只股票的除权日看比值是否连续。

作用域而非漏数据：给全市场 5577 只都落一份镜像，正是 v4/v5 要消除的重复。当前作用域记录在 `cache_v4_manifest.json` 的 `qfq_scope`（数组）与 `symbols_without_qfq`（缺口）里。

---

## 5. 派生与迁移命令

```bash
# 日常必跑：从权威层派生候选作用域（离线，30 日窗 77 只候选实测 53.8 s）
python scripts/fetch/derive_cache.py --mode derive \
    --cache-dir cache/akshare \
    --qfq-symbols 002724.SZ 002343.SZ 600241.SH \
    --json-out run/derive.json

# 也可以让 derive 自己从某个旧目录的 forward_daily/ 推断作用域
python scripts/fetch/derive_cache.py --mode derive \
    --cache-dir cache/akshare --scope-from cache/akshare_v3

# 历史遗留：v3 缓存 → price_store（零网络）
python scripts/fetch/derive_cache.py --mode factorize \
    --cache-dir cache/akshare_v3 --out-dir cache/akshare

# 历史遗留：证明派生能复现旧镜像（仅对 v3/v4 基线有意义）
python scripts/fetch/derive_cache.py --mode verify \
    --cache-dir cache/akshare --baseline-dir cache/akshare_v3
```

`verify` 输出按 `(镜像, 字段)` 分桶的 `exact / one_tick / beyond_tolerance / null_mismatch` 计数，出现 `beyond_tolerance` 或 `null_mismatch` 时退出码 2。**v5 缓存上不要指望 `verify` 给出有意义的结论**：v4 的因子腿已经不存在，没有可比对的第二腿。

---

## 6. 运行模式（只有两个）

`fetch_akshare_data.py --mode` 的 `choices` 是 **`bootstrap` / `status`**。`snapshot`（全市场快照增量）与 `waves`（候选池补数）已在 v5 合并进 bootstrap——**照旧文档敲 `--mode snapshot` / `--mode waves` 会直接被 argparse 拒掉**。

- **bootstrap**：拉日历与名录 → 按 `cache_refresh.sessions_to_fetch` 逐日判定增量 → 对需要更新的股票各发**一次** `adjust="qfq"` 请求 → 写 `price_store/`（先算 `is_limit_up`）→ 由各分片合并出 `daily_adjusted/` 分区。
  - 增量是**交易日维度**的：缓存里已有的会话不会重复抓；已落盘的股票直接跳过网络。
  - 某会话派不出 qfq 收盘时该行不进分区（分区内不允许 null 收盘），缺口计入 manifest 的 `sessions_without_adjusted_close`。
- **status**：只读缓存并打印覆盖统计，不联网、不写入。排查"到底缺哪天"时先跑这个。

`sessions_to_fetch` 的边界规则（`--as-of` 与今天的关系）决定是否更新；`--confirm-completed-session` 是"我确认该交易日已收盘"的显式确认，不能在盘中乱加。

---

## 7. 契约自检

```bash
python scripts/validate/validate_cache_contract.py --cache-dir cache/akshare --sample 200
# 权威层逐文件穷举（全市场约 5 min）
python scripts/validate/validate_cache_contract.py --cache-dir cache/akshare --deep-all
```

- `--sample` 控制"抽样重新派生比对"的股票数；**深检数量由 `--deep-sample` 控制（默认 300）**，两者不是一回事。
- 报告字段：`schema_version`（当前契约 = 5）、`layout`（`v5|v4|v3`，由 store 内 `schema_version` 判定）、`counts`、`issues`。

校验器在 **v5** 下检查：

| 层 | 检查项 |
|---|---|
| `calendar` / `stock_basic` | 存在性、必填字段、`list_status` 常量 |
| `price_store` | `schema_version == 5`、`qfq[]` 四价键齐全、价格为有限数、`is_limit_up` 为 bool 或 null（首笔 `null` 合法） |
| 派生一致性 | 抽样重新派生并与 `daily_adjusted/` / `forward_daily/` 逐值比对（`derived_price_mismatch`）——这是唯一能发现"派生产物已过期"的检查，因为过期的分区本身就是一份看起来完全合法的价格表 |
| 派生层形态 | `daily_adjusted` / `forward_daily` / `unadjusted_daily` 必须为纯 list（下游要求） |
| 分区覆盖 | `daily_adjusted` 分区与 `bootstrap_manifest.coverage` 对齐 |

`forward_daily` / `unadjusted_daily` 在 v5 下是 **derive 产物**，bootstrap 后缺失只记一个 `*_absent_optional` 计数，**不报警告**；一旦存在就照常校验。有 `error` 退出码 1；仅 `warning` 退出码 0。

45 日窗 × 5577 只实测：默认深检 300 个 store 约 8.4 s，`--deep-all` 约 5 min（受每文件约 14 ms 的打开扫描成本约束）。

---

## 8. 下游消费关系

- `screen_market_threshold_gainers.py` / `compute_market_winners_from_daily.py`：`daily_adjusted/` + `stock_basic.json`。
- `calculate_structure_waves.py`：`forward_daily/`（前复权四价，纯 list）。
- `calculate_emotion_rallies.py`：`forward_daily/`（前收/前高）+ `unadjusted_daily/`（`is_limit_up`）。
- `export_contract_dataset.py`：从 `price_store/` 直接折叠（v5 无 `symbol_cache/`）。
- 计算层与报告层**不感知** v3/v4/v5 差异：它们读的是派生层的纯 list，字段名与形状未变。

---

## 9. 历史版本（只读参考）

- **v4**：`price_store` 存 `raw[]`（未复权四价）+ `adjust{anchor_date, anchor_factor, rows:[{trade_date, factor}], events[]}`，hfq/qfq 由 `raw × factor` 派生；存在 `snapshot`/`waves` 两个额外模式与 `snapshot_manifests/`、`waves_manifest.json`、`st/` 目录。
- **v3**：`symbol_cache/`（每行 12 个价格）+ `symbol_cache_hfq/` 四份镜像；`derive_cache.py --mode factorize` 可零网络升级到新结构。
- 校验器仍能读这两代：`layout` 报 `v4`/`v3` 时按各自的不变量（raw+factor+anchor / 折叠视图）校验，不会误判为损坏。

---

一句话记忆：**`price_store` 是唯一的 qfq 权威（一条 `qfq[]` + 派生的 `is_limit_up`），`calendar` / `stock_basic` 是只读事实；`daily_adjusted` / `forward_daily` / `unadjusted_daily` 全部可删可重建，后两者必须跑 `derive` 才有，且只覆盖候选作用域。**
