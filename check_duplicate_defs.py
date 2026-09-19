#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静态检查：模块内不允许存在同名（顶层）函数/类/常量定义。

背景：曾出现两个同名 `_http_fetch`（签名不同），后者在模块加载时静默覆盖前者，
导致只传 url 的既有调用点参数错位、运行即报错。py_compile 查不出这类问题，
故上线前用 AST 做一次重复定义检查。

用法：
    python3 check_duplicate_defs.py [file.py ...]
默认检查 backend/app/main.py。发现重名时以非零码退出。
"""
import ast
import sys
from collections import defaultdict


def check_file(path: str) -> list:
    src = open(path, "rb").read().decode("utf-8")
    tree = ast.parse(src, filename=path)
    # 模块级定义（顶层函数、异步函数、类、以及普通赋值名）
    names = defaultdict(list)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names[node.name].append(node.lineno)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names[t.id].append(node.lineno)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names[node.target.id].append(node.lineno)
    dup = {name: lines for name, lines in names.items() if len(lines) > 1}
    return [(name, lines) for name, lines in sorted(dup.items())]


def main(argv: list) -> int:
    files = argv[1:] or ["backend/app/main.py"]
    failed = False
    for path in files:
        try:
            dup = check_file(path)
        except Exception as e:  # noqa: BLE001
            print(f"[ERROR] 无法解析 {path}: {e}")
            failed = True
            continue
        if dup:
            failed = True
            print(f"[FAIL] {path} 存在同名顶层定义（后定义会静默覆盖先定义）：")
            for name, lines in dup:
                print(f"       - {name}: 行 {lines}")
        else:
            print(f"[OK] {path} 无同名顶层定义")
    if failed:
        print("\n静态检查未通过：请重命名重复定义（保持各自调用点签名一致）后再发版。")
        return 1
    print("\n静态检查通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
