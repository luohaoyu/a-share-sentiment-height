#!/usr/bin/env python3
"""离线校验 CLI 契约与脚本 argparse 定义的一致性。

本校验器只解析 Python AST，不启动业务脚本、不导入 AkShare、不联网，也不逐个执行
``--help``。它检查：脚本存在、登记参数存在、required/choices/可静态求值默认值一致，
以及命令模板没有使用未登记参数。
"""
from __future__ import annotations

import argparse
import ast
import json
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONTRACT = SKILL_ROOT / "references" / "cli_contract.json"


@dataclass
class Option:
    names: list[str]
    required: bool | None
    default: Any
    default_known: bool
    choices: list[Any] | None


def literal(node: ast.AST | None, constants: dict[str, Any] | None = None) -> tuple[bool, Any]:
    if node is None:
        return True, None
    if isinstance(node, ast.Name) and constants is not None and node.id in constants:
        return True, constants[node.id]
    try:
        return True, ast.literal_eval(node)
    except (ValueError, TypeError):
        return False, None


def parse_options(path: Path) -> dict[str, Option]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    constants: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            ok, value = literal(node.value, constants)
            if ok:
                constants[node.targets[0].id] = value
    options: dict[str, Option] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "add_argument":
            continue
        names: list[str] = []
        for arg in node.args:
            ok, value = literal(arg, constants)
            if ok and isinstance(value, str) and value.startswith("--"):
                names.append(value)
        if not names:
            continue
        keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        req_ok, required = literal(keywords.get("required"), constants)
        def_ok, default = literal(keywords.get("default"), constants)
        choices_ok, choices = literal(keywords.get("choices"), constants)
        option = Option(
            names=names,
            required=bool(required) if req_ok and required is not None else None,
            default=default,
            default_known=def_ok and "default" in keywords,
            choices=list(choices) if choices_ok and choices is not None else None,
        )
        for name in names:
            options[name] = option
    return options


def template_options(template: str) -> set[str]:
    try:
        tokens = shlex.split(template)
    except ValueError:
        return set()
    return {token for token in tokens if token.startswith("--")}


def validate(contract_path: Path) -> dict[str, Any]:
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    checked: dict[str, Any] = {}

    for script_id, spec in contract.get("scripts", {}).items():
        rel = spec.get("path")
        path = SKILL_ROOT / str(rel)
        if not path.is_file():
            errors.append({"script": script_id, "code": "script_missing", "path": str(rel)})
            continue
        try:
            actual = parse_options(path)
        except (SyntaxError, OSError, UnicodeError) as exc:
            errors.append({"script": script_id, "code": "ast_parse_failed", "detail": str(exc)})
            continue

        for name in spec.get("required_options", []):
            option = actual.get(name)
            if option is None:
                errors.append({"script": script_id, "code": "required_option_missing", "option": name})
            elif option.required is not True:
                errors.append({"script": script_id, "code": "required_flag_mismatch", "option": name, "actual": option.required})

        for name, expected in spec.get("choices", {}).items():
            option = actual.get(name)
            if option is None:
                errors.append({"script": script_id, "code": "choice_option_missing", "option": name})
            elif option.choices is None:
                warnings.append({"script": script_id, "code": "choices_not_statically_readable", "option": name})
            elif option.choices != expected:
                errors.append({"script": script_id, "code": "choices_mismatch", "option": name, "expected": expected, "actual": option.choices})

        for name, expected in spec.get("defaults", {}).items():
            option = actual.get(name)
            if option is None:
                errors.append({"script": script_id, "code": "default_option_missing", "option": name})
            elif not option.default_known:
                warnings.append({"script": script_id, "code": "default_not_statically_readable", "option": name, "expected": expected})
            elif option.default != expected:
                errors.append({"script": script_id, "code": "default_mismatch", "option": name, "expected": expected, "actual": option.default})

        for name in spec.get("deprecated_options", []):
            if name not in actual:
                errors.append({"script": script_id, "code": "deprecated_option_missing", "option": name})

        for name in spec.get("forbidden_for_delivery", []):
            if name not in actual:
                errors.append({"script": script_id, "code": "forbidden_option_missing", "option": name})

        unknown_template = sorted(template_options(str(spec.get("template", ""))) - set(actual))
        for name in unknown_template:
            errors.append({"script": script_id, "code": "template_option_unknown", "option": name})

        checked[script_id] = {
            "path": str(rel),
            "registered_options": len(actual),
            "required_checked": len(spec.get("required_options", [])),
            "defaults_checked": len(spec.get("defaults", {})),
        }

    return {
        "ok": not errors,
        "contract_version": contract.get("contract_version"),
        "skill_version": contract.get("skill_version"),
        "validation_mode": "python_ast_no_import_no_help_no_network",
        "scripts_checked": len(checked),
        "checked": checked,
        "errors": errors,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate cli_contract.json against argparse definitions without running scripts")
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    result = validate(args.contract)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
