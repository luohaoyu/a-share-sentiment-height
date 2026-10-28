"""一条命令完成「契约数据 → 校验 → 单文件报告」全链路。

本脚本只是把三步串起来，不重写任何业务逻辑：
    1. to_contract_data.py   三口径合并 JSON → data.json（契约数据）
    2. validate_contract.py  按 assets/report.contract.json 校验 data.json
    3. pack_report.py        assets/app.html + data.json → 单文件 report.html

任一步失败立即停止，退出码非零。产物：
    <output>              单文件报告，双击离线可开（数据内嵌，零外部依赖）
    <data-out>            契约数据（默认与 output 同目录的 report.data.json）
    <data-js>             可选，开发期拆分布局用

用法：
    python scripts/build_report_spa.py \\
        --input three_height_metrics.json \\
        --forward-daily cache/akshare/forward_daily \\
        --output report.html \\
        [--end-date 2026-10-23] [--return-days 10] [--unadjusted-daily cache/akshare/unadjusted_daily] [--data-js report.data.js]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
# 脚本按职责分组在 scripts/<group>/ 下，技能根目录靠 SKILL.md 定位，不依赖固定层级
SKILL_ROOT = next(
    (p for p in (SCRIPTS, *SCRIPTS.parents) if (p / "SKILL.md").exists()),
    SCRIPTS.parents[1],
)
ASSETS = SKILL_ROOT / "assets"


def run(stage: str, argv: list[str]) -> None:
    print(f"\n=== {stage} ===")
    proc = subprocess.run(
        [sys.executable, *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.stdout:
        print(proc.stdout.rstrip())
    if proc.returncode != 0:
        if proc.stderr:
            print(proc.stderr.rstrip(), file=sys.stderr)
        print(f"\n[失败] {stage} 退出码 {proc.returncode}，后续步骤已终止。", file=sys.stderr)
        raise SystemExit(proc.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="三口径合并 JSON → 契约数据 → 契约校验 → 单文件 SPA 报告"
    )
    parser.add_argument("--input", type=Path, required=True, help="three_height_metrics.json")
    parser.add_argument("--forward-daily", type=Path, default=None, help="前复权日线目录（构建达标池趋势必需）")
    parser.add_argument("--unadjusted-daily", type=Path, required=True, help="含显式涨停状态的日线目录（逐日首板口径，必需）")
    parser.add_argument("--output", type=Path, required=True, help="输出单文件 HTML")
    parser.add_argument("--data-out", type=Path, default=None, help="契约数据路径，默认 output 同目录 report.data.json")
    parser.add_argument("--data-js", type=Path, default=None, help="额外输出 report.data.js（开发调试）")
    parser.add_argument("--end-date", default=None, help="指定结束日，默认最新交易日")
    parser.add_argument("--return-days", type=int, default=10, help="区间涨幅长度，固定 10")
    parser.add_argument("--manifest", type=Path, default=None, help="bootstrap_manifest.json（抓取失败审计源）")
    parser.add_argument("--stock-basic", type=Path, default=None, help="stock_basic.json（失败股分类）")
    parser.add_argument("--price-store", type=Path, default=None, help="price_store 目录（失败股在库行数）")
    parser.add_argument("--limit-up-ladder", type=Path, default=None, help="limit_up_ladder.json（每日涨停家数与连板梯队）")
    parser.add_argument("--app", type=Path, default=ASSETS / "app.html", help="SPA 模板，默认 assets/app.html")
    parser.add_argument("--contract", type=Path, default=ASSETS / "report.contract.json", help="契约文件")
    parser.add_argument("--skip-validate", action="store_true", help="跳过契约校验（不推荐）")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"错误：输入不存在 {args.input}", file=sys.stderr)
        return 2
    if not args.unadjusted_daily.is_dir():
        print(f"错误：涨停状态目录不存在 {args.unadjusted_daily}", file=sys.stderr)
        return 2
    if not args.limit_up_ladder or not args.limit_up_ladder.exists():
        print("错误：必须提供有效的 --limit-up-ladder，禁止生成缺少涨停梯队的正式报告", file=sys.stderr)
        return 2
    if not args.app.exists():
        print(f"错误：SPA 模板不存在 {args.app}", file=sys.stderr)
        return 2

    output = args.output.resolve()
    data_out = args.data_out or output.parent / "report.data.json"

    # 1) 契约数据
    step1 = [
        str(SCRIPTS / "to_contract_data.py"),
        "--input", str(args.input),
        "--output", str(data_out),
        "--return-days", str(args.return_days),
    ]
    if args.forward_daily:
        step1 += ["--forward-daily", str(args.forward_daily)]
    step1 += ["--unadjusted-daily", str(args.unadjusted_daily)]
    if args.end_date:
        step1 += ["--end-date", args.end_date]
    if args.manifest:
        step1 += ["--manifest", str(args.manifest)]
    if args.stock_basic:
        step1 += ["--stock-basic", str(args.stock_basic)]
    if args.price_store:
        step1 += ["--price-store", str(args.price_store)]
    if args.limit_up_ladder:
        step1 += ["--limit-up-ladder", str(args.limit_up_ladder)]
    run("1/3 构造契约数据", step1)

    # 2) 契约校验
    if not args.skip_validate:
        run(
            "2/3 契约校验",
            [
                str(SCRIPTS / "validate_contract.py"),
                "--contract", str(args.contract),
                "--data", str(data_out),
            ],
        )
    else:
        print("\n=== 2/3 契约校验 ===\n已跳过（--skip-validate）")

    # 3) 打包单文件
    step3 = [
        str(SCRIPTS / "pack_report.py"),
        "--app", str(args.app),
        "--contract", str(args.contract),
        "--data", str(data_out),
        "--output", str(output),
    ]
    if args.data_js:
        step3 += ["--data-js", str(args.data_js)]
    if args.skip_validate:
        step3 += ["--skip-validate"]
    run("3/3 打包单文件报告", step3)

    print(f"\n完成：{output}")
    print(f"契约数据：{data_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
