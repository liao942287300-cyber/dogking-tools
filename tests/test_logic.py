"""dogking-tools 逻辑测试：三种模式 + 边界情况"""
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 插件根目录
spec = importlib.util.spec_from_file_location("dk_nodes", os.path.join(HERE, "nodes.py"))
nodes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nodes)

# 用临时状态文件，别污染真实 state.json
tmpdir = tempfile.mkdtemp()
nodes.STATE_FILE = os.path.join(tmpdir, "state.json")

PASS, FAIL = [], []


def check(name, got, want):
    ok = got == want
    (PASS if ok else FAIL).append(name)
    print(f"{'PASS' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"     期望: {want}")
        print(f"     实际: {got}")


def run(path, mode, start=1, end=0, index=1, auto=True, node="n1"):
    r = nodes.DogkingPromptLoader().load(
        path, mode, start, end, index, auto, unique_id=node
    )
    text, line, total, status = r["result"]
    return text, line, total, status


# ---------------------------------------------------------------- 准备测试文件
txt = os.path.join(tmpdir, "p.txt")
with open(txt, "w", encoding="utf-8") as f:
    f.write("第一行提示词\n第二行提示词\n\n第三行提示词\n第四行提示词\n第五行提示词\n")

csvf = os.path.join(tmpdir, "p.csv")
with open(csvf, "w", encoding="utf-8-sig") as f:
    f.write("序号,名称,正向提示词\n1,a,CSV第一行\n2,b,CSV第二行\n3,c,CSV第三行\n")

print("=== 1. txt 解析（空行跳过）===")
prompts, err = nodes.read_prompts(txt)
check("行数 5（空行被跳过）", len(prompts), 5)
check("第一行内容", prompts[0], "第一行提示词")

print("\n=== 2. csv 解析（取提示词列）===")
prompts, err = nodes.read_prompts(csvf)
check("行数 3", len(prompts), 3)
check("第一行内容", prompts[0], "CSV第一行")

print("\n=== 3. 全部生成模式：连续 6 次应 1,2,3,4,5,1 ===")
lines = []
for _ in range(6):
    _, line, total, status = run(txt, nodes.MODE_ALL)
    lines.append(line)
check("行号序列", lines, [1, 2, 3, 4, 5, 1])
check("总行数", total, 5)

print("\n=== 4. 全部生成：状态文案 ===")
_, line, total, status = run(txt, nodes.MODE_ALL, node="n2")
check("状态格式", status, "第 1/5 行（全部生成 1-5）")

print("\n=== 5. 范围模式 2-4：连续 4 次应 2,3,4,2 ===")
lines = []
for _ in range(4):
    _, line, _, _ = run(txt, nodes.MODE_RANGE, start=2, end=4, node="n3")
    lines.append(line)
check("行号序列", lines, [2, 3, 4, 2])

print("\n=== 6. 范围模式反填 4-2：应纠正成 2-4 ===")
lines = []
for _ in range(3):
    _, line, _, _ = run(txt, nodes.MODE_RANGE, start=4, end=2, node="n4")
    lines.append(line)
check("行号序列", lines, [2, 3, 4])

print("\n=== 7. 范围模式超范围 1-999：按总行数 5 处理 ===")
lines = []
for _ in range(6):
    _, line, _, _ = run(txt, nodes.MODE_RANGE, start=1, end=999, node="n5")
    lines.append(line)
check("行号序列", lines, [1, 2, 3, 4, 5, 1])

print("\n=== 8. 范围模式 结束行=0：表示到最后 ===")
lines = []
for _ in range(3):
    _, line, _, _ = run(txt, nodes.MODE_RANGE, start=3, end=0, node="n6")
    lines.append(line)
check("行号序列", lines, [3, 4, 5])

print("\n=== 9. 编号模式 固定第 3 行：连续 3 次都是 3 ===")
lines = []
texts = []
for _ in range(3):
    t, line, _, _ = run(txt, nodes.MODE_INDEX, index=3, node="n7")
    lines.append(line)
    texts.append(t)
check("行号不变", lines, [3, 3, 3])
check("内容正确", texts[0], "第三行提示词")

print("\n=== 10. 编号超范围：文件 5 行填编号 99 → 取第 5 行 ===")
_, line, _, _ = run(txt, nodes.MODE_INDEX, index=99, node="n8")
check("收敛到最后一行", line, 5)

print("\n=== 11. 重置计数（现在是前端按钮，走 state 层）===")
run(txt, nodes.MODE_ALL, node="n9")
run(txt, nodes.MODE_ALL, node="n9")
_, line, _, _ = run(txt, nodes.MODE_ALL, node="n9")
check("推进到第 3 行", line, 3)

# 「重置计数」已从 BOOLEAN 输入参数改成一次性按钮，
# 底层就是 server.py 的 /dogking-tools/reset：把该节点的 index 写回 0。
_state = nodes._load_state()
_state["n9"]["index"] = 0
nodes._save_state(_state)
_, line, _, _ = run(txt, nodes.MODE_ALL, node="n9")
check("重置后回到第 1 行", line, 1)

print("\n=== 12. 换文件自动清零 ===")
run(txt, nodes.MODE_ALL, node="n10")
run(txt, nodes.MODE_ALL, node="n10")
_, line, _, _ = run(csvf, nodes.MODE_ALL, node="n10")
check("换文件后从第 1 行开始", line, 1)

print("\n=== 13. 错误处理 ===")
_, line, total, status = run("", nodes.MODE_ALL, node="n11")
check("空路径提示", status, "请先选择提示词文件")
_, line, total, status = run("Z:/nope/none.txt", nodes.MODE_ALL, node="n12")
check("文件不存在提示", status.startswith("文件不存在"), True)

print("\n=== 14. 状态持久化 ===")
run(txt, nodes.MODE_ALL, node="n13")
run(txt, nodes.MODE_ALL, node="n13")
saved = nodes._load_state()
check("state.json 有记录", "n13" in saved, True)
check("记录的 index = 2", saved["n13"]["index"], 2)

print("\n=== 15. should_continue 逻辑（决定要不要自动排下一次）===")
events = []
orig_send = None


class FakeServer:
    class instance:
        @staticmethod
        def send_sync(name, data):
            events.append((name, data))


sys.modules["server"] = type("M", (), {"PromptServer": FakeServer})

run(txt, nodes.MODE_ALL, node="n14")          # 第 1/5 行，应继续
check("第 1 行后应继续", events[-1][1]["should_continue"], True)
for _ in range(3):
    run(txt, nodes.MODE_ALL, node="n14")
run(txt, nodes.MODE_ALL, node="n14")          # 第 5 行，不应继续
check("最后一行后不继续", events[-1][1]["should_continue"], False)

events.clear()
run(txt, nodes.MODE_RANGE, start=1, end=2, node="n15")   # 1-2 的第一行，应继续
check("范围模式第 1 行后应继续", events[-1][1]["should_continue"], True)
run(txt, nodes.MODE_RANGE, start=1, end=2, node="n15")   # 第 2 行，结束
check("范围模式最后一行不继续", events[-1][1]["should_continue"], False)

events.clear()
run(txt, nodes.MODE_INDEX, index=2, node="n16")
check("编号模式不继续", events[-1][1]["should_continue"], False)

print("\n" + "=" * 50)
print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
if FAIL:
    print("失败项:", FAIL)
    sys.exit(1)
