"""把 app.html + data.json 打成单文件报告（纯标准库，无需 node/npm）。

三种产出模式：
    1. 默认：单文件 report.html，数据内嵌 <script type="application/json">，双击即可离线打开。
    2. --data-js：另出 report.data.js（window.REPORT_DATA=...），用于开发期拆分调试。
    3. --no-embed：只复制 app.html，用于「外部注入」场景。

内置契约校验，校验不通过默认拒绝打包（--skip-validate 可跳过）。

SPA 模板与契约文件固定在技能目录的 assets/ 下，无需传 --app / --contract：
    <skill>/assets/app.html
    <skill>/assets/report.contract.json

用法：
    # 最简：输出单文件报告
    python scripts/pack_report.py --data data.json --output report.html

    # 显式指定（换模板或换契约版本时）
    python scripts/pack_report.py --app assets/app.html --data data.json \
        --contract assets/report.contract.json --output report.html
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

PLACEHOLDER = "__REPORT_DATA__"

# 技能目录布局：<skill>/scripts/report/pack_report.py + <skill>/assets/{app.html,report.contract.json}
# 技能根目录靠 SKILL.md 定位，不依赖固定的目录层级
_HERE = Path(__file__).resolve().parent
SKILL_ROOT = next(
    (p for p in (_HERE, *_HERE.parents) if (p / "SKILL.md").exists()),
    _HERE.parents[1],
)
ASSETS = SKILL_ROOT / "assets"
DEFAULT_APP = ASSETS / "app.html"
DEFAULT_CONTRACT = ASSETS / "report.contract.json"


def validate(contract: Path, data: Path) -> tuple[bool, str]:
    script = Path(__file__).with_name("validate_contract.py")
    proc = subprocess.run(
        [sys.executable, str(script), "--contract", str(contract), "--data", str(data)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return proc.returncode == 0, (proc.stdout or proc.stderr).strip()


def shrink(data: dict) -> str:
    """紧凑 JSON；转义 </ 防止 </script> 提前闭合脚本块。"""
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return raw.replace("</", "<\\/")


def main() -> int:
    parser = argparse.ArgumentParser(description="app.html + data.json → 单文件报告")
    parser.add_argument("--app", type=Path, default=DEFAULT_APP, help="SPA 模板 app.html（默认 assets/app.html）")
    parser.add_argument("--data", type=Path, required=True, help="契约数据 data.json")
    parser.add_argument("--output", type=Path, required=True, help="输出 HTML 路径")
    parser.add_argument(
        "--contract", type=Path, default=DEFAULT_CONTRACT,
        help="契约文件，用于打包前校验（默认 assets/report.contract.json）",
    )
    parser.add_argument("--skip-validate", action="store_true", help="跳过契约校验")
    parser.add_argument("--data-js", type=Path, default=None, help="额外输出 report.data.js")
    parser.add_argument("--no-embed", action="store_true", help="不内嵌数据，仅复制模板")
    args = parser.parse_args()

    if not args.app.exists():
        print(f"错误：模板不存在 {args.app}")
        return 1
    # 模板文件不存在时就不必再谈校验；存在但未给契约时按默认契约处理。
    if args.contract and not args.contract.exists():
        print(f"提示：契约文件不存在 {args.contract}，跳过校验")
        args.contract = None

    if args.contract and not args.skip_validate:
        ok, output = validate(args.contract, args.data)
        print(output)
        if not ok:
            print("\n契约校验未通过，已拒绝打包。如确需强制打包请加 --skip-validate。")
            return 1
    elif not args.contract:
        print("提示：未提供 --contract，跳过契约校验（不推荐用于交付）")

    html = args.app.read_text(encoding="utf-8")
    placeholder_count = html.count(PLACEHOLDER)
    if placeholder_count != 1:
        print(
            f"错误：模板中的占位符 {PLACEHOLDER} 应恰好出现 1 次，"
            f"实际为 {placeholder_count} 次；拒绝打包以避免破坏运行脚本。"
        )
        return 1

    data = json.loads(args.data.read_text(encoding="utf-8"))

    if args.no_embed:
        out = html.replace(PLACEHOLDER, "", 1)
    else:
        out = html.replace(PLACEHOLDER, shrink(data), 1)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(out, encoding="utf-8")

    if args.data_js:
        args.data_js.parent.mkdir(parents=True, exist_ok=True)
        args.data_js.write_text(
            "window.REPORT_DATA=" + json.dumps(data, ensure_ascii=False) .replace("</", "<\\/") + ";\n",
            encoding="utf-8",
        )

    app_bytes = len(html.encode("utf-8"))
    out_bytes = len(out.encode("utf-8"))
    data_bytes = args.data.stat().st_size
    print(json.dumps({
        "output": str(args.output),
        "bytes": out_bytes,
        "app_bytes": app_bytes,
        "data_bytes": data_bytes,
        "data_share": f"{data_bytes / out_bytes * 100:.1f}%" if out_bytes else "0%",
        "embedded": not args.no_embed,
        "placeholder_replaced": PLACEHOLDER not in out,
    }, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
