# -*- coding: utf-8 -*-
"""图片批量加载节点测试：排序 / 三种来源 / 模式 / 持久化 / 读图"""
import importlib.util
import json
import math
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 插件根目录
spec = importlib.util.spec_from_file_location("dk_nodes_img", os.path.join(HERE, "nodes.py"))
nodes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nodes)

# 用临时状态文件，绝对不碰用户真实的 state.json
tmpdir = tempfile.mkdtemp()
nodes.STATE_FILE = os.path.join(tmpdir, "state.json")

PASS, FAIL, SKIP = [], [], []


def check(name, got, want):
    ok = got == want
    (PASS if ok else FAIL).append(name)
    print(f"{'PASS' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"     期望: {want}")
        print(f"     实际: {got}")


def check_true(name, cond, detail=""):
    ok = bool(cond)
    (PASS if ok else FAIL).append(name)
    print(f"{'PASS' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"     详情: {detail}")


def skip(name, why):
    SKIP.append(name)
    print(f"SKIP {name}（{why}）")


def run(folder="", sort_mode=nodes.SORT_NUMERIC, mode=nodes.IMG_MODE_ALL,
        start=1, end=0, auto=True, manual="", node="i1"):
    """跑一次节点，返回 (image, index, total, status)"""
    r = nodes.DogkingImageLoader().load(
        folder, sort_mode, mode, start, end, auto, manual, unique_id=node
    )
    image, index, total, status = r["result"]
    return image, index, total, status


def touch(path):
    """造一个占位文件（只测列表/排序，不真解码）"""
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")


# ---------------------------------------------------------------- 测试素材
folder_a = os.path.join(tmpdir, "picsA")
os.makedirs(folder_a, exist_ok=True)
for name in ("a.png", "img1.png", "img2.png", "img10.png", "note.txt"):
    touch(os.path.join(folder_a, name))

folder_b = os.path.join(tmpdir, "picsB")
os.makedirs(folder_b, exist_ok=True)
touch(os.path.join(folder_b, "only.png"))

folder_empty = os.path.join(tmpdir, "picsEmpty")
os.makedirs(folder_empty, exist_ok=True)

print("=== 1. 数字升序：img2.png 必须排在 img10.png 前面 ===")
paths, err = nodes.list_images(folder_a, nodes.SORT_NUMERIC)
names = [os.path.basename(p) for p in paths]
check("文件数（txt 被过滤）", len(names), 4)
check("自然排序结果", names, ["a.png", "img1.png", "img2.png", "img10.png"])
check_true("img2 在 img10 之前",
           names.index("img2.png") < names.index("img10.png"), names)

print("\n=== 2. 字母升序 ===")
paths, err = nodes.list_images(folder_a, nodes.SORT_ALPHA)
names = [os.path.basename(p) for p in paths]
check("字母排序结果", names, ["a.png", "img1.png", "img10.png", "img2.png"])

print("\n=== 3. 全部模式：连续 4 次应 1,2,3,4 ===")
idx = [run(folder_a, node="img_all")[1] for _ in range(4)]
check("序号序列", idx, [1, 2, 3, 4])
check("总数量", run(folder_a, node="img_all")[2], 4)
check("跑完一轮归零后回到 1", run(folder_a, node="img_all")[1], 1)

print("\n=== 4. 状态文案格式 ===")
_, index, total, status = run(folder_a, node="img_status")
check_true("状态以「第 1/4 张（全部模式 1-4）」开头",
           status.startswith("第 1/4 张（全部模式 1-4）"), status)

print("\n=== 5. 范围模式 2-4：连续 4 次应 2,3,4,2 ===")
idx = [run(folder_a, mode=nodes.IMG_MODE_RANGE, start=2, end=4, node="img_range")[1]
       for _ in range(4)]
check("序号序列", idx, [2, 3, 4, 2])

print("\n=== 6. 范围模式反填 4-2：应纠正成 2-4 ===")
idx = [run(folder_a, mode=nodes.IMG_MODE_RANGE, start=4, end=2, node="img_rev")[1]
       for _ in range(3)]
check("序号序列", idx, [2, 3, 4])
_, _, _, status = run(folder_a, mode=nodes.IMG_MODE_RANGE, start=4, end=2, node="img_rev")
check_true("范围模式状态文案", status.startswith("第 2/4 张（范围模式 2-4）"), status)

print("\n=== 7. 范围模式超范围 1-999：按总数 4 clamp ===")
idx = [run(folder_a, mode=nodes.IMG_MODE_RANGE, start=1, end=999, node="img_clamp")[1]
       for _ in range(5)]
check("序号序列", idx, [1, 2, 3, 4, 1])

print("\n=== 8. 范围模式 结束序号=0：表示到最后 ===")
idx = [run(folder_a, mode=nodes.IMG_MODE_RANGE, start=3, end=0, node="img_end0")[1]
       for _ in range(3)]
check("序号序列", idx, [3, 4, 3])

print("\n=== 9. 换文件夹 → 计数归零 ===")
run(folder_a, node="img_switch")
run(folder_a, node="img_switch")
_, index, _, _ = run(folder_b, node="img_switch")
check("换文件夹后从第 1 张开始", index, 1)

print("\n=== 10. 换排序方式 → 计数归零 ===")
run(folder_a, node="img_sort")
run(folder_a, node="img_sort")
_, index, _, _ = run(folder_a, sort_mode=nodes.SORT_ALPHA, node="img_sort")
check("换排序后从第 1 张开始", index, 1)

print("\n=== 11. 换模式 → 计数归零 ===")
run(folder_a, node="img_mode")
run(folder_a, node="img_mode")
_, index, _, _ = run(folder_a, mode=nodes.IMG_MODE_RANGE, start=1, end=2, node="img_mode")
check("换模式后从范围起点开始", index, 1)

print("\n=== 12. 手动列表：追加在文件夹结果之后 ===")
manual = json.dumps(["zzz_extra.png"])
paths, err = nodes.collect_images(folder_a, nodes.SORT_NUMERIC, manual)
check("合并后总数", len(paths), 5)
check("最后一个是手动拖入的图", os.path.basename(paths[-1]), "zzz_extra.png")
check("文件夹部分顺序不变", [os.path.basename(p) for p in paths[:4]],
      ["a.png", "img1.png", "img2.png", "img10.png"])
_, _, total, _ = run(folder_a, manual=manual, node="img_manual")
check("节点总数量含手动列表", total, 5)

print("\n=== 13. 手动列表单独可用（不选文件夹）===")
_, index, total, _ = run("", manual=json.dumps(["extra1.png"]), node="img_manual_only")
check("总数量", total, 1)
check("序号", index, 1)

print("\n=== 14. 手动列表去重（同一张图不重复出现）===")
dup = json.dumps([os.path.join(folder_a, "a.png")])
paths, err = nodes.collect_images(folder_a, nodes.SORT_NUMERIC, dup)
check("重复项被去掉", len(paths), 4)

print("\n=== 15. 手动列表坏数据不炸 ===")
for bad in ("", "not-json", "{}", "[1, 2, 3]", "[null]", '["  "]'):
    got = nodes._parse_manual_list(bad)
    check(f"坏输入 {bad!r} → 空列表", got, [])

print("\n=== 16. 空文件夹 / 不存在的文件夹：不抛异常，返回提示 ===")
_, index, total, status = run(folder_empty, node="img_empty")
check("空文件夹 total", total, 0)
check("空文件夹 index", index, 0)
check_true("空文件夹有提示", "没有图片" in status, status)

_, index, total, status = run("Z:/nope/nowhere", node="img_missing")
check("不存在 total", total, 0)
check_true("不存在有提示", "文件夹不存在" in status, status)

_, _, _, status = run("", node="img_none")
check_true("什么都没填有提示", "请先选择图片文件夹" in status, status)

print("\n=== 17. 读图失败不打断队列（坏图只报状态）===")
image, index, total, status = run(folder_a, node="img_broken")
check("坏图仍然输出序号", index, 1)
check_true("状态里带失败原因", ("读取失败" in status) or ("缺少依赖" in status), status)
check_true("坏图返回占位图或 None", image is None or hasattr(image, "shape"), type(image))

print("\n=== 18. 状态持久化 ===")
run(folder_a, node="img_state")
run(folder_a, node="img_state")
saved = nodes._load_state()
check("state.json 有记录", "img_state" in saved, True)
check("记录的 index = 2", saved["img_state"].get("index"), 2)
check("记录 kind = image", saved["img_state"].get("kind"), "image")
check("记录 folder", saved["img_state"].get("folder"), folder_a)

print("\n=== 19. 提示词节点的老记录不会被图片节点误用 ===")
old = nodes._load_state()
old["img_legacy"] = {"path": "x.txt", "mode": "全部生成", "index": 9}
nodes._save_state(old)
_, index, _, _ = run(folder_a, node="img_legacy")
check("没有 kind 的记录当作无记录", index, 1)

print("\n=== 20. IS_CHANGED 强制每次执行 ===")
value = nodes.DogkingImageLoader.IS_CHANGED()
check_true("返回 NaN", isinstance(value, float) and math.isnan(value), value)

print("\n=== 21. 真读图：形状 [1,H,W,3] float32 0-1 ===")
try:
    from PIL import Image as PILImage
    import torch  # noqa: F401
    has_img_stack = True
except Exception as e:  # 独立测试环境可能没有 PIL / torch
    has_img_stack = False
    skip("真读图 tensor", f"缺少 PIL/torch：{e}")

if has_img_stack:
    from PIL import Image as PILImage

    folder_c = os.path.join(tmpdir, "picsC")
    os.makedirs(folder_c, exist_ok=True)
    real_png = os.path.join(folder_c, "real.png")
    PILImage.new("RGB", (40, 24), (255, 128, 0)).save(real_png)

    tensor, err = nodes._load_image_safe(real_png)
    check("没有错误", err, None)
    check("形状", tuple(tensor.shape), (1, 24, 40, 3))
    check("dtype", str(tensor.dtype), "torch.float32")
    check_true("值域 0-1", float(tensor.max()) <= 1.0 and float(tensor.min()) >= 0.0,
               (float(tensor.min()), float(tensor.max())))
    # 橙色 (255,128,0) → (1.0, 0.5019…, 0.0)
    check_true("像素值正确", abs(float(tensor[0, 0, 0, 0]) - 1.0) < 1e-6, float(tensor[0, 0, 0, 0]))

    _, index, total, status = run(folder_c, node="img_real")
    check("节点输出总数量", total, 1)
    check_true("真图无错误状态", status.startswith("第 1/1 张（全部模式 1-1）"), status)

    # 灰度图也要转成 3 通道
    gray_png = os.path.join(folder_c, "gray.png")
    PILImage.new("L", (10, 10), 200).save(gray_png)
    g_tensor, g_err = nodes._load_image_safe(gray_png)
    check("灰度图无错误", g_err, None)
    check("灰度图补成 3 通道", tuple(g_tensor.shape), (1, 10, 10, 3))

print("\n" + "=" * 50)
print(f"通过 {len(PASS)} / 失败 {len(FAIL)} / 跳过 {len(SKIP)}")
if FAIL:
    print("失败项:", FAIL)
    sys.exit(1)
