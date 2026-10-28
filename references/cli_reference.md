# CLI 调用契约

## 目的

为 Agent 提供已经核验的脚本调用入口，避免每次业务运行前机械执行 `python <script> --help`、反复读取 `argparse` 源码或试错参数。

- 机器可读事实：`references/cli_contract.json`
- 一致性校验：`scripts/validate/validate_cli_contract.py`
- 工作流与业务口径：`SKILL.md`
- 脚本运行时事实：各脚本的 `argparse`，仅作为契约失效时的兜底

## 强制调用规则

1. 从技能根目录运行命令；本文所有脚本路径均相对技能根目录。
2. 优先读取 `cli_contract.json` 中对应脚本的 `required_options`、`defaults`、`choices`、`template` 和 `notes`。
3. 契约校验通过、脚本未修改且任务使用已登记模式时，直接套用模板，禁止例行执行 `--help`。
4. 仅在以下情况执行单脚本 `--help`：
   - 所需脚本或参数未登记；
   - `validate_cli_contract.py` 报告不一致；
   - 脚本返回参数解析错误（通常退出码 2）；
   - 用户要求使用契约未覆盖的模式；
   - 脚本自契约最后校验后发生变化。
5. 非零退出码不得忽略；不得通过删参数、跳过校验或改用未登记历史脚本绕过失败。
6. `--start-date` 已废弃且被忽略，不得写入新命令。
7. `--skip-validate` 只用于明确的诊断场景；正式交付禁止使用。

## Python 与路径

优先使用 WorkBuddy 托管 Python 的绝对路径；若运行环境已经激活项目隔离环境，可使用该环境中的 `python`。命令模板中的 `python` 是运行时占位符，不表示允许使用未经确认的全局解释器。

路径规则：

- `<cache>`：缓存根目录，例如 `./cache/akshare`
- `<run>`：本次运行产物目录，例如 `./run`
- `<end-date>`：最近一个已完成交易日，格式 `YYYY-MM-DD`
- `<symbols...>`：候选股 `ts_code` 列表，例如 `600241.SH 002242.SZ`

## 主链路速查

| 阶段 | 脚本 | 联网 | 核心输出 |
|---|---|---:|---|
| 缓存判定 | `scripts/fetch/fetch_akshare_data.py --mode status` | 否 | `next_action` |
| 首次建库/增量 | `scripts/fetch/fetch_akshare_data.py --mode bootstrap` | 是 | schema v5 缓存 |
| 缓存校验 | `scripts/validate/validate_cache_contract.py` | 否 | JSON 校验报告 |
| 候选派生 | `scripts/fetch/derive_cache.py --mode derive` | 否 | `forward_daily/`、`unadjusted_daily/` |
| T1/强势池 | `scripts/compute/screen_market_threshold_gainers.py` | 否 | `threshold_gainers.json` |
| T2 | `scripts/compute/calculate_structure_waves.py` | 否 | `structure_waves.json` |
| T3 | `scripts/compute/calculate_emotion_rallies.py` | 否 | `emotion_rallies.json` |
| 三口径合并 | `scripts/validate/validate_three_height_metrics.py` | 否 | `three_height_metrics.json` |
| 涨停梯队 | `scripts/compute/calculate_limit_up_ladder.py` | 否 | `limit_up_ladder.json` |
| 梯队校验 | `scripts/validate/validate_limit_up_ladder.py` | 否 | 校验结果 |
| 正式报告 | `scripts/report/build_report_spa.py` | 否 | 单文件 HTML + 契约数据 |

## 标准命令模板

### 1. 缓存状态判定

```bash
python scripts/fetch/fetch_akshare_data.py \
  --mode status \
  --output-dir <cache> \
  --end-date <end-date> \
  --target-count 30 --lookback 10 --buffer-sessions 30
```

读取输出中的 `next_action`：

- `compute`：缓存覆盖完整，禁止行情联网，直接进入计算；
- `bootstrap`：无有效缓存，执行首次建库；
- `bootstrap_incremental`：已有缓存但覆盖不足，仍调用同一 `--mode bootstrap`，由脚本只落盘缺失会话。

### 2. 首次建库或增量更新

```bash
python scripts/fetch/fetch_akshare_data.py \
  --mode bootstrap \
  --output-dir <cache> \
  --end-date <end-date> \
  --target-count 30 --lookback 10 --buffer-sessions 30 \
  --shards 8 --workers 6 --retries 1 --task-attempts 3 --shard-retries 2
```

仅确认 `end-date` 已经收盘时追加 `--confirm-completed-session`。`--source-rates` 和 `--source-weights` 接受 JSON **文件路径**，不接受内联 JSON。

### 3. 缓存契约校验

```bash
python scripts/validate/validate_cache_contract.py \
  --cache-dir <cache> --sample 50 --deep-sample 300 \
  --json-out <run>/cache_contract.json
```

日常使用抽样深检；仅专项审计使用 `--deep-all`。

### 4. T1 与强势池

```bash
python scripts/compute/screen_market_threshold_gainers.py \
  --calendar <cache>/calendar.json \
  --daily <cache>/daily_adjusted \
  --stock-basic <cache>/stock_basic.json \
  --st-coverage partial --allow-name-only-st --prices-adjusted \
  --target-count 30 --lookback 10 --min-return 50 \
  --strong-pool-rank-size 10 --threshold-days 60 \
  --end-date <end-date> --output <run>/threshold_gainers.json
```

AkShare v5 缓存是前复权价，必须传 `--prices-adjusted`；否则脚本要求 `--adj-factor`。

### 5. 候选派生

```bash
python scripts/fetch/derive_cache.py \
  --mode derive --cache-dir <cache> \
  --qfq-symbols <symbols...> \
  --json-out <run>/derive.json
```

`--qfq-symbols` 使用阈值候选去重后的 `ts_code`。该步骤纯离线。

### 6. T2、T3 与合并

```bash
python scripts/compute/calculate_structure_waves.py \
  --input <run>/threshold_gainers.json \
  --forward-daily <cache>/forward_daily \
  --output <run>/structure_waves.json --workers 4

python scripts/compute/calculate_emotion_rallies.py \
  --input <run>/threshold_gainers.json \
  --forward-daily <cache>/forward_daily \
  --unadjusted-daily <cache>/unadjusted_daily \
  --output <run>/emotion_rallies.json --workers 4

python scripts/validate/validate_three_height_metrics.py \
  --base-input <run>/threshold_gainers.json \
  --structure-input <run>/structure_waves.json \
  --emotion-input <run>/emotion_rallies.json \
  --output <run>/three_height_metrics.json
```

### 7. 涨停连板与校验

```bash
python scripts/compute/calculate_limit_up_ladder.py \
  --price-store <cache>/price_store \
  --calendar <cache>/calendar.json \
  --stock-basic <cache>/stock_basic.json \
  --end-date <end-date> --window-days 30 \
  --output <run>/limit_up_ladder.json

python scripts/validate/validate_limit_up_ladder.py \
  --input <run>/limit_up_ladder.json --expected-dates 30 \
  --json-out <run>/limit_up_ladder.validation.json
```

### 8. 正式 SPA 报告

```bash
python scripts/report/build_report_spa.py \
  --input <run>/three_height_metrics.json \
  --forward-daily <cache>/forward_daily \
  --unadjusted-daily <cache>/unadjusted_daily \
  --manifest <cache>/bootstrap_manifest.json \
  --stock-basic <cache>/stock_basic.json \
  --price-store <cache>/price_store \
  --limit-up-ladder <run>/limit_up_ladder.json \
  --end-date <end-date> --return-days 10 \
  --output <run>/report.html --data-out <run>/report.data.json
```

`--unadjusted-daily` 是当前一键报告入口的必填项。正式交付不得使用 `--skip-validate`。

## 契约维护

修改任一已登记脚本的 CLI 参数后，同一变更必须完成：

1. 更新 `references/cli_contract.json`；
2. 必要时更新本文命令模板与语义说明；
3. 运行 `python scripts/validate/validate_cli_contract.py`；
4. 校验通过后才允许更新技能版本或交付。

仅修改脚本内部实现且 CLI 参数完全不变时，不必改契约内容，但仍应执行一次一致性校验。