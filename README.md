# A股短线情绪高度分析 V1

`a-share-sentiment-height-v1` 是一个面向 WorkBuddy 的 A 股短线情绪分析技能。它以动态强势股票池为基础，从固定 10 日涨幅、严格底分型起点和当前有效首板起点三个口径衡量个股与市场的情绪高度，并生成可审计、可复现的离线 HTML 报告。

当前语义版本：`1.0.0`

> 本项目用于历史数据研究与量化分析，不构成投资建议。

## 核心能力

- 按交易日动态生成强势股票池；
- 支持全市场、沪深主板、科创成长板三个市场范围；
- 计算 T1、T2、T3 三套情绪高度指标；
- 标记相比前一交易日新进入强势池的股票；
- 汇总前 5 个交易日内曾进入强势池、但当前已退出的股票；
- 输出逐日榜首、并列榜首和榜首切换；
- 计算涨停家数、首板、连板梯队和最高连板；
- 识别市场分化、三口径分歧及高度—广度背离；
- 聚合观察窗口内的异常事件、异常个股和异常原因；
- 生成数据来源、过滤规则、覆盖边界与执行过程审计；
- 输出数据内嵌、无需联网、可离线打开的单文件 SPA 报告。

## 三口径定义

对观察日 `T`，先形成当日强势股票池：

```text
P(T) = {固定 10 日涨幅达到阈值的股票}
```

随后对 `P(T)` 中每只股票计算：

```text
T1 = close(T) / close(T-10 个交易日前) - 1
T2 = close(T) / 截止 T 日最近已确认严格底分型的真实最低价 - 1
T3 = close(T) / 截止 T 日当前有效首板的收盘价 - 1
```

约束：

- 每个观察日独立计算；
- 只使用 `T` 日及以前已经确认的信息；
- 不使用未来日期确认的底分型或首板起点；
- T2/T3 无有效起点时保留为空，并输出中文异常原因；
- 缺失值不使用前后日期填补。

## 市场范围与趋势图

统一的市场范围筛选作用于全部趋势图：

- 全市场；
- 沪深主板：沪市主板 + 深市主板；
- 科创成长板：科创板 + 创业板。

报告同时提供：

1. **三口径最大值趋势**

   ```text
   每日最大值 = max(T1 当日最大值, T2 当日最大值, T3 当日最大值)
   ```

2. **T1/T2/T3 独立趋势**
   - 分屏：分别展示三个独立图表；
   - 重叠：在同一图表叠加三条趋势线。

“分屏/重叠”只控制 T1、T2、T3 独立趋势，不影响三口径最大值趋势。

## 股票池变动口径

### 新进

```text
新进(T) = P(T) - P(T-1)
```

### 退出

```text
退出(T) = [P(T-1) ∪ P(T-2) ∪ P(T-3) ∪ P(T-4) ∪ P(T-5)] - P(T)
```

即前 5 个交易日内任意一天进入过强势池、但当前已经退出的股票；同一股票在窗口内去重。

## 默认范围与过滤规则

- 默认观察最近 30 个已完成交易日；
- 固定涨幅回看期为 10 个交易日；
- 默认额外缓存 30 个交易日；
- 默认新股过滤阈值为上市不足 60 个自然日；
- 排除 ST、`*ST`、退市整理、退市风险及非正常上市证券；
- 无法核验上市日期的股票不参与榜首计算，并记录在审计信息中；
- 北交所默认不纳入报告市场范围；
- 主板与科创板、创业板涨跌幅制度不同，报告不会机械比较不同市场的绝对高度。

## 报告内容

最终单文件报告包含：

- 情绪结论与市场状态；
- 高度趋势与市场分化；
- 涨停家数、首板和连板梯队；
- 三口径最大值趋势；
- T1/T2/T3 独立趋势（分屏/重叠）；
- 每日强势股票池及三套证据；
- 新进个股与前 5 日退出个股；
- 全市场和分市场逐日榜首；
- 分歧日时间轴；
- 观察窗口级异常汇总与异常个股明细；
- 数据来源、过滤规则、覆盖警告和执行日志。

## 安装

### 方式一：克隆到 WorkBuddy 用户技能目录

```bash
git clone https://github.com/luohaoyu/a-share-sentiment-height.git ~/.workbuddy/skills/a-share-sentiment-height-v1
```

Windows PowerShell 示例：

```powershell
git clone https://github.com/luohaoyu/a-share-sentiment-height.git "$env:USERPROFILE\.workbuddy\skills\a-share-sentiment-height-v1"
```

克隆后重启 WorkBuddy，或等待技能列表刷新。

### 方式二：现有仓库更新

```bash
git -C ~/.workbuddy/skills/a-share-sentiment-height-v1 pull --ff-only
```

## 环境要求

- Python 3.11 或更高版本；
- Git（仅安装和更新需要）；
- AkShare 及其运行依赖（使用 AkShare 数据接入流程时需要）；
- 可写的本地缓存和报告输出目录。

建议使用虚拟环境安装 Python 依赖，不要污染系统 Python。

## 在 WorkBuddy 中使用

安装后可直接提出类似请求：

```text
使用 a-share-sentiment-height-v1 分析最近 30 个交易日的 A 股短线情绪。
```

```text
分析全市场、沪深主板和科创成长板的三口径情绪高度，生成 HTML 报告。
```

```text
查看每日强势股票池、新进个股、前 5 日退出个股及分歧时间轴。
```

WorkBuddy 会根据 `SKILL.md` 冻结统计参数、准备缓存、运行确定性脚本、校验数据并生成报告。

## 命令行主流程

以下示例在仓库根目录执行。日期仅作格式示例，请替换为实际已完成交易日。

### 1. 建立或增量更新缓存

```bash
python scripts/fetch/fetch_akshare_data.py \
  --mode bootstrap \
  --output-dir ./cache/akshare \
  --end-date 2026-10-27 \
  --target-count 30 \
  --lookback 10 \
  --buffer-sessions 30 \
  --shards 8 \
  --workers 6
```

缓存总交易日需求为：

```text
目标观察日数 + 固定涨幅回看期 + 缓冲交易日数
```

默认即 `30 + 10 + 30 = 70` 个交易日。后续执行相同命令时会按交易日补充缺口，不重抓已经覆盖的数据。

### 2. 派生基础日线视图

```bash
python scripts/fetch/derive_cache.py \
  --mode derive \
  --cache-dir ./cache/akshare \
  --json-out ./run/derive-base.json
```

首次执行会生成全市场按日分区的 `daily_adjusted/`。此时还没有阈值候选清单，因此不会生成候选股范围的 `forward_daily/` 和 `unadjusted_daily/`。

### 3. 生成动态强势股票池

```bash
python scripts/compute/screen_market_threshold_gainers.py \
  --calendar ./cache/akshare/calendar.json \
  --daily ./cache/akshare/daily_adjusted \
  --stock-basic ./cache/akshare/stock_basic.json \
  --st-coverage partial \
  --allow-name-only-st \
  --prices-adjusted \
  --target-count 30 \
  --lookback 10 \
  --min-return 50 \
  --strong-pool-rank-size 10 \
  --threshold-days 60 \
  --end-date 2026-10-27 \
  --output ./run/threshold_gainers.json
```

`--min-return` 使用百分数，`50` 表示 50%。

根据 `threshold_gainers.json` 中的候选股票代码，再派生候选范围日线。下面以示例代码说明；实际执行时应从 `qualified[]` 提取全部 `ts_code`：

```bash
python scripts/fetch/derive_cache.py \
  --mode derive \
  --cache-dir ./cache/akshare \
  --qfq-symbols 600241.SH 002242.SZ 301190.SZ \
  --json-out ./run/derive-candidates.json
```

### 4. 计算 T2 与 T3

```bash
python scripts/compute/calculate_structure_waves.py \
  --input ./run/threshold_gainers.json \
  --forward-daily ./cache/akshare/forward_daily \
  --output ./run/structure_waves.json

python scripts/compute/calculate_emotion_rallies.py \
  --input ./run/threshold_gainers.json \
  --forward-daily ./cache/akshare/forward_daily \
  --unadjusted-daily ./cache/akshare/unadjusted_daily \
  --output ./run/emotion_rallies.json
```

### 5. 合并并校验三口径

```bash
python scripts/validate/validate_three_height_metrics.py \
  --base-input ./run/threshold_gainers.json \
  --structure-input ./run/structure_waves.json \
  --emotion-input ./run/emotion_rallies.json \
  --output ./run/three_height_metrics.json
```

### 6. 可选：生成涨停连板梯队

```bash
python scripts/compute/calculate_limit_up_ladder.py \
  --price-store ./cache/akshare/price_store \
  --calendar ./cache/akshare/calendar.json \
  --stock-basic ./cache/akshare/stock_basic.json \
  --end-date 2026-10-27 \
  --window-days 30 \
  --output ./run/limit_up_ladder.json
```

### 7. 生成最终单文件报告

```bash
python scripts/report/build_report_spa.py \
  --input ./run/three_height_metrics.json \
  --forward-daily ./cache/akshare/forward_daily \
  --unadjusted-daily ./cache/akshare/unadjusted_daily \
  --limit-up-ladder ./run/limit_up_ladder.json \
  --output ./run/短线情绪高度.html
```

该命令依次执行：

```text
三口径合并数据 → 报告契约校验 → 单文件 HTML 打包
```

任一步失败都会停止，不会生成伪成功报告。

## 目录结构

```text
.
├── SKILL.md                    # WorkBuddy 技能入口与完整执行规范
├── README.md                   # 项目说明
├── assets/
│   ├── app.html                # 离线 SPA 模板
│   ├── report.contract.json    # 报告数据契约
│   └── ai_rules.json           # 情绪状态和策略约束规则
├── references/                 # 数据接入、缓存、报告和交易框架说明
└── scripts/
    ├── fetch/                  # 数据抓取、增量缓存与派生层
    ├── compute/                # 股票池、三口径与涨停连板计算
    ├── validate/               # 指标和缓存校验
    ├── report/                 # 契约转换、校验与 HTML 打包
    └── contract/               # 原始数据源契约验收工具
```

本地运行时产生的 `cache/`、`run/`、`outputs/`、`__pycache__/` 等内容已由 `.gitignore` 排除，不会提交到仓库。

## 数据与缓存边界

- `price_store/` 是价格权威层；
- `daily_adjusted/`、`forward_daily/`、`unadjusted_daily/` 是可重建派生层；
- 首板搜索从缓存最早交易日搜索到观察日，不再受固定 10 日回看限制；
- 缓存起点是首板搜索的唯一硬边界；
- 如果真实首板早于缓存边界，报告会明确标记覆盖不足，而不是猜测起点；
- AkShare 流程无法提供完整历史 ST 区间时，只能显式采用当前名称过滤的降级模式，并在报告中披露。

详细缓存结构见 [`references/cache_layout.md`](references/cache_layout.md)，完整流程见 [`references/workflow_reference.md`](references/workflow_reference.md)。

## 验证

验证技能结构：

```bash
python /path/to/skill-creator/scripts/quick_validate.py .
```

验证报告契约数据：

```bash
python scripts/report/validate_contract.py --data ./run/report.data.json
```

验证涨停连板数据：

```bash
python scripts/validate/validate_limit_up_ladder.py \
  --input ./run/limit_up_ladder.json \
  --expected-dates 30
```

项目要求：

- Python 脚本必须通过语法检查；
- `assets/app.html` 的内嵌 JavaScript 必须通过语法检查；
- 报告契约必须为合法 JSON；
- 契约校验错误数必须为 0；
- 真实数据覆盖警告必须保留，不得静默删除。

## 参考文档

- [AkShare 接入说明](references/akshare_adapter.md)
- [缓存结构](references/cache_layout.md)
- [完整工作流](references/workflow_reference.md)
- [报告 SPA 设计](references/report_spa.md)
- [交易框架映射](references/trading_framework.md)
- [数据源能力声明](references/source_capability.akshare.json)
- [技能结构图](references/skill_structure.svg)
- [建库流程图](references/bootstrap_flow.svg)

## 版本管理

- 技能标识：`a-share-sentiment-height-v1`
- 当前版本：`1.0.0`
- 修复问题：递增补丁版本，例如 `1.0.1`；
- 向后兼容的新功能：递增次版本，例如 `1.1.0`；
- 改变三口径定义、缓存契约或报告契约：递增主版本。

## 免责声明

本项目根据历史公开数据和确定性规则生成研究报告，仅用于学习、研究和工程验证，不构成证券投资咨询或任何买卖建议。数据源可能存在延迟、缺失或口径差异，请在使用结论前核验原始数据。市场有风险，投资需谨慎。
