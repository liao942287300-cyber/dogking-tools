# -*- coding: utf-8 -*-
"""路径解析测试：各种脏输入都要能正确识别为绝对路径"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("n", os.path.join(HERE, "nodes.py"))
n = importlib.util.module_from_spec(spec)
spec.loader.exec_module(n)

CASES = [
    ("正常绝对路径", r"C:\Users\demo\Downloads\prompts-100-2026-11-03.txt", "C:"),
    ("带 BOM", "\ufeffC:\\Users\\demo\\Downloads\\a.txt", "C:"),
    ("带双引号", '"C:\\Users\\demo\\a.txt"', "C:"),
    ("带单引号", "'C:\\Users\\demo\\a.txt'", "C:"),
    ("全角冒号", "C：\\Users\\demo\\a.txt", "C:"),
    ("零宽空格", "\u200bC:\\Users\\demo\\a.txt", "C:"),
    ("零宽连接符", "\u200dC:\\Users\\demo\\a.txt", "C:"),
    ("正斜杠", "C:/Users/demo/a.txt", "C:"),
    ("UNC", "\\\\nas\\share\\a.txt", "\\\\"),
    # 独立测试环境没有 folder_paths，相对路径会回退到 abspath；
    # 真实 ComfyUI 里会解析到 input 目录。这里只要求结果是个绝对路径。
    ("相对路径", "prompts.txt", None),
]

passed, failed = [], []
for name, raw, expect_prefix in CASES:
    got = n._resolve_path(raw)
    if expect_prefix is None:
        ok = os.path.isabs(got)
    else:
        ok = got.startswith(expect_prefix)
    (passed if ok else failed).append(name)
    print(f"{'PASS' if ok else 'FAIL'} {name:10s} -> {got}")

print()
print(f"通过 {len(passed)} / 失败 {len(failed)}")
if failed:
    print("失败:", failed)
    sys.exit(1)
