# 采集层重构落地方案：qfq-only 单请求 + 按日增量 bootstrap

> 状态：**已实施（2026-10-26 全量跑通）**。Phase 1–5 全部落地，真实全市场缓存已验证（5577 只 × 45 交易日，schema 5 / `basis=qfq_direct` / `layout=v5`）。本文档保留为 v5 的**设计依据与取舍记录**，不再是待办方案。
> 相关文档：`cache_layout.md`（v5 现状）、`bootstrap_flow.svg`、`workflow_reference.md`、`SKILL.md`

> **实施结果（实测）**：单股请求数 2→1；8 分片 × 6 workers 全市场 45 日窗 526 s（10.6 只/秒），单进程 48 线程为 2.99 只/秒；`price_store` 从"raw + factor"变为"单条 `qfq[]`"，磁盘占用同步下降；三口径下游（10 日涨幅、结构波段、首板波段）结果与 v4 一致性通过派生比对。**唯一遗留的版本错位是文档与校验器的布局探测**——`cache_layout.md`、`SKILL.md` 与 `validate_cache_contract.detect_layout()` 已在同日修正到 v5。

---

## 0. 已确认决策（用户拍板）

| # | 决策 | 来源 |
|---|---|---|
| D1 | **方案 B**：每只股只调一次 `adjust="qfq"` 接口，砍掉 raw + hfq 两条腿 | 用户确认「改为一次请求」 |
| D2 | 缓存增量更新**从个股维度改为交易日维度**（主单元 = 一个交易日） | 用户澄清 |
| D3 | 缓存更新机制 = **区间包含判定 + 边界门控**，三模式收敛为 **bootstrap 单模式** | 用户确认「只剩 bootstrap 一种模式」 |
| D4 | 边界决策树（见 §2）：C<A 或 D>B 才更新；D==B 看今天/开盘；D>B 交易日则更新、非交易日不更新（D>B 交易日开/未开都更） | 用户逐条确认（含「第4种都是更新」修正） |

---

## 1. 为什么方案 B 在功能上零损失（关键论证）

下游 `compute/` 实际只消费两类量，二者都是**相邻日比值**，复权基准在比值里相消：

- **收益率 / 涨幅**：`compute_market_winners_from_daily.py:265`、`screen_market_threshold_gainers.py:100` 读 `forward_daily`（qfq）算 `close_t/close_{t-1}-1`。
- **涨停标记 `is_limit_up`**：`annotate_limit_up`（`fetch_akshare_data.py:1185`）算的是相邻日 close 比值，注释已明确「前复权锚点因子在比值中抵消，口径无关」；`calculate_emotion_rallies.py:403/427` 读它做情绪波段。

因此 **v4 的「抗前复权基准漂移」机制（factor/anchor 版本化）解决的是「绝对 qfq 值漂移」，而本项目下游根本不消费绝对 qfq**——只消费比值。方案 B 直接存 qfq，下游 return/涨幅/limit_up 与现状**逐点相等**。

**唯一真实代价**：放弃对「绝对 qfq 值」的版本化控制。鉴于下游不用它，这是可接受取舍。若日后要出绝对价报表，再单独加轻量锚点即可（见 §7 回退）。

---

## 2. 缓存更新决策树（已落地为 `cache_refresh.py`）

缓存区间 `[A, B]`（A=最早缓存交易日，B=最新缓存交易日，含 `last_update` 时间戳）；请求区间 `[C, D]`。

```
1. 空缓存 (A/B 为 None)                          -> 更新（抓整个请求窗）
2. C < A                                         -> 更新（向前回填更早交易日）
3. D < B                                         -> 不更新（请求整体落在缓存内）
4. D == B:
     - 非今天 或 非交易日                          -> 不更新
     - 今天 且 交易日:
         last_update >= session_open(B)           -> 不更新（今天这根 bar 已拿到）
         否则                                    -> 更新（今天这根还没拿到）
5. D > B:
     - D 非交易日                                 -> 不更新（无数据，B 已是最新交易日）
     - D 是交易日                                 -> 更新（开/未开都更）
```

**实现要点**
- `session_open` 默认 09:30 CST（与 `_snapshot_window` 的 15:05 收盘门控互补；用户明确用「开盘时间」判定）。
- 日历只含交易日且在 `as_of` 处截断 → 规则 5 的「非交易日不更新」由枚举天然满足（非交易日不在日历里）；未来交易日也不在截断日历里 → 不会抓未发生的会话。
- **不动中间挖洞**：本规则只看首尾边界。按日增量只往后追加、不删历史，正常不会出洞；若需防洞，在 `sessions_to_fetch` 之外加一道 `set(req_days).issubset(cache_days)` 校验即可（当前不强制）。

**已交付（Phase 1）**
- `scripts/fetch/cache_refresh.py`：`sessions_to_fetch()`（权威实现，返回待抓取交易日列表）+ `needs_refresh()`（布尔视图，二者互不分歧）。
- `scripts/fetch/test_cache_refresh.py`：11 个离线契约用例全绿（空缓存/内部包含/向前回填/向后追加含周末自动排除/D==B 今天开盘前后/D>B 跨交易日/D>B 落在周末 等）。

---

## 3. 新 price_store schema（v5，qfq-only）

> 目标形态；Phase 2 实施。当前 v4 见 `cache_layout.md`。

```json
{
  "schema_version": 5,
  "ts_code": "688808.SH",
  "list_date": "2020-07-22",
  "price_mode": "akshare_qfq_direct",
  "basis": "qfq_direct",
  "definition": "qfq fetched directly; downstream consumes ratios only",
  "last_update": "2026-10-26T18:00:00",
  "qfq": [
    {"trade_date": "2026-10-23", "open": 110.2, "high": 112.0, "low": 109.5,
     "close": 111.37, "vol": 1234567, "amount": 1.37e8, "is_limit_up": false}
  ]
}
```

| 变更 | v4 | v5 |
|---|---|---|
| 价格权威 | `raw[]` + `adjust{factor, anchor_factor, events}` | `qfq[]`（单口径四价+量额） |
| 复权因子 | 显式存储、派生 qfq/hfq | **消失**（qfq 即最终口径） |
| `is_limit_up` | `raw[]` 行派生 | `qfq[]` 行派生（逻辑不变，比值口径无关） |
| 锚点 | per-store 末日 `anchor_factor` | 无需（下游只吃比值） |
| `last_update` | 无 | **新增**（支撑 D==B 今天开盘门控） |

`build_store` / `derive_rows` / `read_store` 改为只认 `qfq[]`；派生层 `forward_daily/` 直接展平 `qfq[]`（不再有 hfq/qfq 重缩放）；`daily_adjusted/`、`unadjusted_daily/` 中 `close` 同为 qfq；`validate_cache_contract.py` 同步更新（弃用 `price_store_anchor_mismatch` / `price_store_orphan_factor` 检查）。

---

## 4. 按日增量统一 bootstrap 调度（目标形态；Phase 3/4）

将「增量主单元」从股改为**交易日**，单模式 `bootstrap` 同时承担建仓与追加：

1. 解析 `as_of` → 锁定 `calendar`（截断于 `as_of`）。
2. 解析请求区间 `[C, D]`（默认 `[list_date, 最新可观测交易日]`）。
3. 对全市场每只股：用 `sessions_to_fetch(A,B,last_update,C,D,calendar,...)` 算出**该股缺失的交易日集合**。
4. **按日派发**：以「(交易日 D, 全市场股票)** 为任务单元，worker 池并发抓取该日 qfq；抓回后合并进各股 `price_store`（按日 append，天然 per-day 语义）。
   - 比当前「按股派发、股内补缺失天」更贴合 D2，且让 anchor 概念自然消失（不再有 per-store 末日）。
5. 末尾批量 `merge_partition` 聚合当日横截面（沿用现 bootstrap 的聚合逻辑）。

**候选集白名单**：原 `waves` 的「只跑候选子集」需求，收敛为 bootstrap 的 `--whitelist <file>` 参数（股票集约束），模式本身不复存在。

**当日实时性约定**：统一用历史 K 线 `qfq` 接口补「当天」，需等当日数据在源就绪（收盘后数小时延迟），不像原 `snapshot` 用 spot 接口收盘即拿。若业务要求「刚收盘即出快照」，在 bootstrap 为「当天」保留一个 spot 兜底钩子（仅补当日、不延展因子），或接受延迟。

---

## 5. 三模式收敛

| 旧模式 | 收敛后 |
|---|---|
| `bootstrap`（全市场建仓 + 按股补缺失） | 升级为「建仓 + 按日增量」统一入口 |
| `snapshot`（spot 落当日 + 手延因子） | **吸收**：补当日统一走 K 线 qfq；因子概念消失，spot 特例随之消失（实时性见 §4 约定） |
| `waves`（候选集 raw+qfq 派生） | **吸收**：变为 bootstrap + `--whitelist` |

CLI：`--mode {bootstrap}` 单一；移除 `--mode snapshot/waves`；新增 `--whitelist`、`--source-weights`（已落地）、`--req-start/--req-end`（请求区间）。

---

## 6. 下游切换清单（Phase 5）

| 文件 | 改动 |
|---|---|
| `calculate_emotion_rallies.py:403/427` | `is_limit_up` 来源从 `unadjusted_daily`（raw）改读 `forward_daily`（qfq）——数值等价（比值口径无关） |
| `compute_market_winners_from_daily.py:265`、`screen_market_threshold_gainers.py:100` | 已吃 `forward_daily`/`daily_adjusted`；`close` 同为 qfq，无需改字段，仅校验契约版本 |
| `export_contract_dataset.py` | 优先 `symbol_cache/` → 改为优先从 `price_store.qfq[]` 派生 |
| `derive_cache.py` | `derive` 模式改为展平 `qfq[]`；废弃 hfq 派生分支 |
| `validate_cache_contract.py` | 移除 anchor/factor 检查；改为校验 `schema_version==5` 与 `qfq[]` 四价键齐全 |

下游消费纯 list 形状（字段名 `open/high/low/close/is_limit_up`）不变，**计算层与报告层不感知 v4→v5 差异**。

---

## 7. 分阶段实施计划

| 阶段 | 内容 | 风险 | 状态 |
|---|---|---|---|
| **Phase 1** | `cache_refresh.py` 决策函数 + 离线契约测试 | 零（纯函数，无下游侵入） | ✅ 已完成（`fetch_akshare_data.py:90` import `sessions_to_fetch`） |
| **Phase 2** | price_store schema v5（qfq-only）+ `build_store/derive_rows/read_store` + 校验器同步 | 中（权威层形状变更） | ✅ 已完成（`SCHEMA_VERSION=5`；`price_store` 只存 `qfq[]`；校验器 `layout=v5` 分支同日补齐） |
| **Phase 3** | `fetch_one/fetch_symbol` 改单 qfq 请求 + `sessions_to_fetch` 接入按日增量调度 | 高（抓取路径） | ✅ 已完成（三源均 `adjust="qfq"`，全市场实测 10.6 只/秒） |
| **Phase 4** | 三模式收敛为 bootstrap 单模式 + CLI 收敛 + 候选集白名单 | 高（入口/参数） | ✅ 已完成（`choices=("bootstrap","status")`） |
| **Phase 5** | 下游 compute 切换 + 回归（派生层重跑 + verify 比对） | 中（跨模块） | ✅ 已完成（compute 读 `forward_daily`/`daily_adjusted`，字段未变） |

> 每阶段均配离线契约测试（mock 网络层），遵循本项目既有「离线覆盖核心契约」验证习惯。

---

## 8. 风险与回退

- ~~**三源 qfq 支持待验证**~~：已实测通过——新浪 `stock_zh_a_daily`、东财 `stock_zh_a_hist`、腾讯 `stock_zh_a_hist_tx` 均支持 `adjust="qfq"` 单请求；腾讯不覆盖北交所，故北交所标的仍需新浪/东财（本技能股票池本就不含北交所，不受影响）。
- **当日就绪延迟**：K 线接口补当日有延迟；如业务不可接受，保留 spot 兜底钩子（§4）。
- **绝对价需求**：若未来要出绝对 qfq 报表，可在 v5 之上加一个轻量全局锚（存 `anchor_date` + 一次性重缩放），不影响比值下游。
- **中间挖洞**：边界包含判定不管洞；按日只追加则无洞，否则加 `issubset` 校验（§2）。
- **回退**：v4 与 v5 并存期间可保留 `price_store`（v4）备份；`derive_cache.py --mode factorize` 可从原始 v4 重派生，回退成本低。
