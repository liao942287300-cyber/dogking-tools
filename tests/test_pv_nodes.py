# -*- coding: utf-8 -*-
"""
PromptVault 节点测试：分类路径 / 三种模式推进 / 数据源指纹 / 断线兜底 / 请求参数

HTTP 全部打桩（替换 urllib.request.urlopen），不依赖真实 PromptVault，
也不碰用户真实的 state.json。
"""
import importlib.util
import io
import json
import math
import os
import sys
import tempfile
import urllib.error

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 插件根目录

# 先加载 nodes.py（pv_nodes 的 state 读写来自它）
spec = importlib.util.spec_from_file_location("dk_nodes_pv", os.path.join(HERE, "nodes.py"))
nodes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nodes)

# 用临时状态文件，绝对不碰用户真实的 state.json
tmpdir = tempfile.mkdtemp()
nodes.STATE_FILE = os.path.join(tmpdir, "state.json")

# pv_nodes 里写的是 `from .nodes import ...`；独立加载时走兜底 `from nodes import ...`，
# 所以先把已加载的模块注册成 "nodes"
sys.modules["nodes"] = nodes

spec2 = importlib.util.spec_from_file_location("dk_pv_nodes", os.path.join(HERE, "pv_nodes.py"))
pv = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(pv)

# 分类缓存也指到临时目录，别写进插件目录
pv.CATEGORY_CACHE_FILE = os.path.join(tmpdir, "pv_categories_cache.json")

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


# ---------------------------------------------------------------- HTTP 打桩

class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


CALLS = []
ROUTES = {}
_real_open = pv._open


def fake_open(req, timeout=None):
    url = req.full_url if hasattr(req, "full_url") else str(req)
    CALLS.append(url)
    path = url.split("?")[0]
    for suffix, value in ROUTES.items():
        if path.endswith(suffix):
            if isinstance(value, Exception):
                raise value
            if isinstance(value, bytes):
                return FakeResp(value)
            return FakeResp(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    raise urllib.error.URLError("no route")


pv._open = fake_open

CATEGORIES = [
    {"id": "c1", "name": "风格", "level": 1, "parentId": None, "order": 0},
    {"id": "c1a", "name": "写实摄影", "level": 2, "parentId": "c1", "order": 0},
    {"id": "c1b", "name": "动漫插画", "level": 2, "parentId": "c1", "order": 1},
    {"id": "c2", "name": "主体", "level": 1, "parentId": None, "order": 1},
    {"id": "c2a", "name": "人物", "level": 2, "parentId": "c2", "order": 0},
]


def make_items(count=5):
    return [
        {
            "id": f"img{i}",
            "name": f"图{i}",
            "positive": f"正向{i}",
            "negative": f"负向{i}",
            "params": f"Steps: {i}, CFG: 7",
            "tags": ["古风"],
            "categoryId": "c1",
            "width": 64,
            "height": 64,
            "createdAt": 1730000000000 + i,
        }
        for i in range(1, count + 1)
    ]


TEXT_ITEMS = [
    {"id": "l1", "name": "角色A", "text": "charA, 1girl, solo"},
    {"id": "l2", "name": "角色B", "text": "charB, 1boy"},
    {"id": "l3", "name": "角色C", "text": "charC, 1other"},
]

try:
    from PIL import Image as _PILImage
    _buf = io.BytesIO()
    _PILImage.new("RGB", (12, 8), (10, 20, 30)).save(_buf, format="PNG")
    PNG_BYTES = _buf.getvalue()
    HAS_PIL = True
except Exception:
    PNG_BYTES = b"\x89PNG\r\n\x1a\nbroken"
    HAS_PIL = False


def routes(items=None, categories=None, text=None, image=PNG_BYTES):
    """装一套默认可用的接口响应"""
    ROUTES.clear()
    ROUTES.update({
        "/api/health": {"ok": True, "app": "PromptVault", "version": "1.1.9", "counts": {}},
        "/api/categories": {
            "ok": True,
            "categories": CATEGORIES if categories is None else categories,
        },
        "/api/items": {
            "ok": True,
            "total": len(make_items(5) if items is None else items),
            "items": make_items(5) if items is None else items,
        },
        "/api/text": {"ok": True, "items": TEXT_ITEMS if text is None else text},
        "/api/image": image,
    })
    CALLS.clear()
    pv.reset_caches()


def run_prompt(mode="全部模式", start=1, end=0, idx=1, cat="全部", addr="",
               node="pv_prompt", auto=True):
    r = pv.DogkingPVPrompt().load(addr, cat, mode, start, end, idx, auto, unique_id=node)
    positive, negative, params, line, total, status = r["result"]
    return positive, negative, params, line, total, status


def run_image(mode="全部模式", start=1, end=0, idx=1, cat="全部", addr="",
              node="pv_image", auto=True):
    r = pv.DogkingPVImage().load(addr, cat, mode, start, end, idx, auto, unique_id=node)
    image, line, total, status = r["result"]
    return image, line, total, status


def run_pair(mode="全部模式", start=1, end=0, idx=1, cat="全部", addr="",
             node="pv_pair", auto=True):
    r = pv.DogkingPVPair().load(addr, cat, mode, start, end, idx, auto, unique_id=node)
    image, positive, negative, params, line, total, status = r["result"]
    return image, positive, negative, params, line, total, status


def run_text(kind="LORA触发词", mode="全部模式", start=1, end=0, idx=1, cat="全部",
             addr="", node="pv_text", auto=True):
    r = pv.DogkingPVText().load(addr, cat, kind, mode, start, end, idx, auto, unique_id=node)
    content, name, line, total, status = r["result"]
    return content, name, line, total, status


# ================================================================ 1. 分类路径
print("=== 1. 分类路径构建（扁平 → 路径）===")
routes()
paths = pv.build_category_paths(CATEGORIES)
check("路径顺序（一级在前，其子级紧随）",
      [p["path"] for p in paths],
      ["全部", "风格", "风格/写实摄影", "风格/动漫插画", "主体", "主体/人物"])
check("第一项是「全部」且 id 为空", (paths[0]["path"], paths[0]["id"]), ("全部", ""))
check("二级分类 level = 2", paths[2]["level"], 2)

print("\n=== 2. 路径反查 id ===")
check("反查一级", pv.category_id_from_path("风格", CATEGORIES), "c1")
check("反查二级", pv.category_id_from_path("风格/写实摄影", CATEGORIES), "c1a")
check("「全部」→ 空（不筛选）", pv.category_id_from_path("全部", CATEGORIES), "")
check("空字符串 → 空", pv.category_id_from_path("", CATEGORIES), "")
check("不存在的路径 → 空", pv.category_id_from_path("不存在/xx", CATEGORIES), "")
check("脏数据（None）→ 空", pv.category_id_from_path(None, CATEGORIES), "")

print("\n=== 3. 孤儿二级分类不丢 ===")
orphan_cats = CATEGORIES + [{"id": "x9", "name": "孤儿分类", "level": 2,
                             "parentId": "gone", "order": 0}]
orphan_paths = [p["path"] for p in pv.build_category_paths(orphan_cats)]
check_true("父分类已删的二级分类追加在末尾", "孤儿分类" in orphan_paths, orphan_paths)
check("孤儿分类排在最后", orphan_paths[-1], "孤儿分类")

print("\n=== 4. 空 / 脏分类数据不炸 ===")
check("空列表 → 只剩全部", [p["path"] for p in pv.build_category_paths([])], ["全部"])
check("None → 只剩全部", [p["path"] for p in pv.build_category_paths(None)], ["全部"])
check("元素是字符串 → 只剩全部",
      [p["path"] for p in pv.build_category_paths(["a", "b"])], ["全部"])

# ================================================================ 5. 三种模式
print("\n=== 5. 全部模式：5 条 → 1,2,3,4,5,1 ===")
routes()
seq = [run_prompt(node="pv_all")[3] for _ in range(6)]
check("序号序列", seq, [1, 2, 3, 4, 5, 1])

print("\n=== 6. 范围模式 2-4 → 2,3,4,2 ===")
routes()
seq = [run_prompt(mode="范围模式", start=2, end=4, node="pv_range")[3] for _ in range(4)]
check("序号序列", seq, [2, 3, 4, 2])

print("\n=== 7. 范围模式反填 4-2 → 自动纠正成 2-4 ===")
routes()
seq = [run_prompt(mode="范围模式", start=4, end=2, node="pv_rev")[3] for _ in range(4)]
check("序号序列", seq, [2, 3, 4, 2])

print("\n=== 8. 范围模式超范围 1-999 → clamp 到 5 ===")
routes()
seq = [run_prompt(mode="范围模式", start=1, end=999, node="pv_clamp")[3] for _ in range(6)]
check("序号序列", seq, [1, 2, 3, 4, 5, 1])

print("\n=== 9. 范围模式 结束序号=0 → 表示到最后 ===")
routes()
seq = [run_prompt(mode="范围模式", start=3, end=0, node="pv_end0")[3] for _ in range(3)]
check("序号序列", seq, [3, 4, 5])

print("\n=== 10. 编号模式：固定第 3 条，不推进 ===")
routes()
seq = [run_prompt(mode="编号模式", idx=3, node="pv_index")[3] for _ in range(3)]
check("序号序列", seq, [3, 3, 3])
check_true("编号模式状态文案带序号", run_prompt(mode="编号模式", idx=3, node="pv_index")[5]
           .startswith("第 3/5 条（编号模式 3）"), "")

print("\n=== 11. 编号模式超范围 clamp ===")
routes()
check("编号 999 → 取最后一条", run_prompt(mode="编号模式", idx=999, node="pv_idx_clamp")[3], 5)

# ================================================================ 12. 指纹
print("\n=== 12. 换分类 → 计数归零 ===")
routes()
run_prompt(cat="风格", node="pv_src")
run_prompt(cat="风格", node="pv_src")
check("同分类第 3 次是第 3 条", run_prompt(cat="风格", node="pv_src")[3], 3)
check("换分类后回到第 1 条", run_prompt(cat="主体", node="pv_src")[3], 1)

print("\n=== 13. 换服务地址 → 计数归零 ===")
routes()
run_prompt(addr="127.0.0.1:8765", node="pv_addr")
run_prompt(addr="127.0.0.1:8765", node="pv_addr")
check("同地址第 3 次是第 3 条",
      run_prompt(addr="127.0.0.1:8765", node="pv_addr")[3], 3)
check("换地址后回到第 1 条",
      run_prompt(addr="127.0.0.1:8766", node="pv_addr")[3], 1)

print("\n=== 14. 换词库类型 → 计数归零 ===")
routes()
run_text("LORA触发词", node="pv_type")
run_text("LORA触发词", node="pv_type")
check("同类型第 3 次是第 3 条", run_text("LORA触发词", node="pv_type")[2], 3)
check("换类型后回到第 1 条", run_text("编辑指令", node="pv_type")[2], 1)

print("\n=== 15. 换模式不清零（和老节点一致：模式不算数据源）===")
routes()
run_prompt(mode="全部模式", node="pv_mode")
check("切到范围模式仍从第 1 条开始（index 已推进到 1）",
      run_prompt(mode="范围模式", start=1, end=5, node="pv_mode")[3], 2)

# ================================================================ 16. 断线
print("\n=== 16. PromptVault 未运行：不抛异常、状态正确 ===")
ROUTES.clear()
CALLS.clear()
pv.reset_caches()
positive, negative, params, line, total, status = run_prompt(node="pv_down")
check("状态串", status, pv.NOT_RUNNING)
check("序号归零", line, 0)
check("总数归零", total, 0)
check("正向为空", positive, "")

image, line, total, status = run_image(node="pv_down_img")
check("图片节点状态串", status, pv.NOT_RUNNING)
check_true("图片节点仍返回占位图或 None", image is None or hasattr(image, "shape"), type(image))
check("图片节点序号归零", line, 0)

print("\n=== 17. 手动填了地址时，提示里带上地址 ===")
ROUTES.clear()
pv.reset_caches()
status = run_prompt(addr="127.0.0.1:9999", node="pv_down_addr")[5]
check_true("提示含自定义地址", "127.0.0.1:9999" in status, status)
check_true("提示含「未运行」", "未运行" in status, status)

print("\n=== 18. 接口返回 500：给 HTTP 状态，不假装没开 ===")
routes()
ROUTES["/api/items"] = urllib.error.HTTPError("http://x", 500, "boom", None, None)
pv.reset_caches()
status = run_prompt(node="pv_500")[5]
check_true("状态里带 HTTP 500", "HTTP 500" in status, status)

print("\n=== 19. 响应不是 JSON 对象：不炸 ===")
routes()
ROUTES["/api/items"] = b"[1,2,3]"
pv.reset_caches()
status = run_prompt(node="pv_badjson")[5]
check_true("有可读的错误提示", "格式不对" in status, status)

# ================================================================ 20. 空列表
print("\n=== 20. 分类下没有图片 ===")
routes(items=[])
status = run_image(node="pv_empty")[3]
check("状态串", status, "该分类下没有图片")
check("总数", run_image(node="pv_empty")[2], 0)

routes(text=[])
check("词库为空的状态串", run_text(node="pv_empty_text")[4], "该类型下没有内容")

print("\n=== 21. 分类不存在（分类被删了）===")
routes()
status = run_image(cat="已经删掉的分类", node="pv_nocat")[3]
check_true("提示分类不存在", "分类不存在" in status, status)
check_true("词库节点忽略分类（不会因此报错）",
           "分类不存在" not in run_text(cat="已经删掉的分类", node="pv_text_cat")[4],
           run_text(cat="已经删掉的分类", node="pv_text_cat")[4])

# ================================================================ 22. 图片
print("\n=== 22. 图片下载失败：灰图 + 状态带原因，不打断队列 ===")
routes()
ROUTES["/api/image"] = urllib.error.URLError("refused")
pv.reset_caches()
image, line, total, status = run_image(node="pv_img_fail")
check_true("状态里带「图片下载失败」", "图片下载失败" in status, status)
check("序号仍然推进", line, 1)
check_true("返回占位图或 None", image is None or hasattr(image, "shape"), type(image))

routes()
ROUTES["/api/image"] = b"this is not an image"
pv.reset_caches()
image, line, total, status = run_image(node="pv_img_bad")
check_true("坏图也有原因", "图片下载失败" in status and "读取失败" in status, status)

print("\n=== 23. 图片下载成功：tensor 形状 [1,H,W,3] ===")
routes()
image, line, total, status = run_image(node="pv_img_ok")
if HAS_PIL:
    check("序号", line, 1)
    check("总数", total, 5)
    check_true("状态文案", status.startswith("第 1/5 张（全部模式 1-5）"), status)
    if image is not None and hasattr(image, "shape"):
        check("形状（12×8 的图 → 1,8,12,3）", tuple(image.shape), (1, 8, 12, 3))
        check("dtype", str(image.dtype), "torch.float32")
    else:
        skip("图片 tensor 形状", "当前环境没有 torch")
else:
    skip("图片 tensor 形状", "当前环境没有 PIL")

print("\n=== 24. 图片+提示词节点：图和词一一对应 ===")
routes()
image, positive, negative, params, line, total, status = run_pair(node="pv_pair")
check("正向", positive, "正向1")
check("负向", negative, "负向1")
check("参数", params, "Steps: 1, CFG: 7")
check("序号", line, 1)
image, positive, negative, params, line, total, status = run_pair(node="pv_pair")
check("第 2 次取第 2 条", (line, positive), (2, "正向2"))

# ================================================================ 25. 请求参数
print("\n=== 25. 选一级分类必须带 includeChildren=1 ===")
routes()
run_image(cat="风格", node="pv_param_children")
item_urls = [u for u in CALLS if "/api/items" in u]
check_true("发过 /api/items 请求", len(item_urls) >= 1, CALLS)
check_true("URL 里带 includeChildren=1",
           any("includeChildren=1" in u for u in item_urls), item_urls)
check_true("URL 里带 category=c1",
           any("category=c1" in u for u in item_urls), item_urls)
check_true("URL 里带 type=images",
           any("type=images" in u for u in item_urls), item_urls)

print("\n=== 26. 选「全部」时不传 category ===")
routes()
run_image(cat="全部", node="pv_param_all")
item_urls = [u for u in CALLS if "/api/items" in u]
check_true("没有 category 参数", all("category=" not in u for u in item_urls), item_urls)
check_true("没有 includeChildren 参数",
           all("includeChildren" not in u for u in item_urls), item_urls)

print("\n=== 27. 二级分类：路径反查成子分类 id ===")
routes()
run_image(cat="风格/写实摄影", node="pv_param_sub")
item_urls = [u for u in CALLS if "/api/items" in u]
check_true("URL 里带 category=c1a",
           any("category=c1a" in u for u in item_urls), item_urls)

print("\n=== 28. 词库类型映射到正确的接口参数 ===")
routes()
run_text("LORA触发词", node="pv_t1")
check_true("loras", any("type=loras" in u for u in CALLS if "/api/text" in u), CALLS)
routes()
run_text("编辑指令", node="pv_t2")
check_true("edits", any("type=edits" in u for u in CALLS if "/api/text" in u), CALLS)
routes()
run_text("系统提示词", node="pv_t3")
check_true("system-prompts",
           any("type=system-prompts" in u for u in CALLS if "/api/text" in u), CALLS)

print("\n=== 29. 词库节点输出内容 / 名称 ===")
routes()
content, name, line, total, status = run_text("LORA触发词", node="pv_t_out")
check("内容 = text 字段", content, "charA, 1girl, solo")
check("名称 = name 字段", name, "角色A")
check("序号", line, 1)
check("总数", total, 3)

print("\n=== 30. 显式服务地址：不探测 8765-8775 ===")
routes()
run_image(addr="127.0.0.1:8766", node="pv_explicit")
check_true("所有请求都打向指定地址",
           all(u.startswith("http://127.0.0.1:8766/") for u in CALLS), CALLS)
check_true("没有探过别的端口", not any(":8765/" in u for u in CALLS), CALLS)

print("\n=== 31. 自动发现：并发探测 + 结果进程内缓存 ===")
routes()
run_image(node="pv_cache1")
after_first = len([u for u in CALLS if "/api/health" in u])
run_image(node="pv_cache2")
after_second = len([u for u in CALLS if "/api/health" in u])
check("第二次执行不再探测（进程内缓存生效）", after_second, after_first)
check_true("发现的是 8765（按端口号顺序取第一个，不是按完成顺序）",
           pv.resolve_base("") == "http://127.0.0.1:8765", pv._cached_base)

print("\n=== 32. 1 秒内重复拉 items 只发一次请求 ===")
routes()
pv.fetch_items("", "c1")
pv.fetch_items("", "c1")
check("items 只请求 1 次", len([u for u in CALLS if "/api/items" in u]), 1)
pv.fetch_items("", "c2")
check("换分类会重新请求", len([u for u in CALLS if "/api/items" in u]), 2)

# ================================================================ 33. 结构
print("\n=== 33. 四个节点的 INPUT_TYPES / RETURN_TYPES 结构 ===")
EXPECTED = {
    "DogkingPVPrompt": (("STRING", "STRING", "STRING", "INT", "INT", "STRING"),
                        ("正向", "负向", "参数", "当前序号", "总数", "状态")),
    "DogkingPVImage": (("IMAGE", "INT", "INT", "STRING"),
                       ("图片", "当前序号", "总数", "状态")),
    "DogkingPVPair": (("IMAGE", "STRING", "STRING", "STRING", "INT", "INT", "STRING"),
                      ("图片", "正向", "负向", "参数", "当前序号", "总数", "状态")),
    "DogkingPVText": (("STRING", "STRING", "INT", "INT", "STRING"),
                      ("内容", "名称", "当前序号", "总数", "状态")),
}
routes()
for node_name, (types, names) in EXPECTED.items():
    cls = pv.NODE_CLASS_MAPPINGS[node_name]
    check(f"{node_name} RETURN_TYPES", tuple(cls.RETURN_TYPES), types)
    check(f"{node_name} RETURN_NAMES", tuple(cls.RETURN_NAMES), names)
    check(f"{node_name} FUNCTION", cls.FUNCTION, "load")
    check(f"{node_name} OUTPUT_NODE", cls.OUTPUT_NODE, True)
    check(f"{node_name} CATEGORY", cls.CATEGORY, "dogking-tools")

    it = cls.INPUT_TYPES()
    req = it.get("required", {})
    for key in ("服务地址", "分类", "模式", "起始序号", "结束序号", "编号", "自动连续"):
        check_true(f"{node_name} 有输入「{key}」", key in req, list(req.keys()))
    check(f"{node_name} hidden.unique_id", it.get("hidden", {}).get("unique_id"), "UNIQUE_ID")

    cat_opts = req["分类"][0]
    check_true(f"{node_name} 分类是列表且含「全部」",
               isinstance(cat_opts, list) and "全部" in cat_opts, cat_opts)
    check(f"{node_name} 模式候选", list(req["模式"][0]), ["全部模式", "范围模式", "编号模式"])

check("文本节点有「类型」输入",
      list(pv.DogkingPVText.INPUT_TYPES()["required"].get("类型", ([],))[0]),
      ["LORA触发词", "编辑指令", "系统提示词"])
check_true("非文本节点没有「类型」",
           "类型" not in pv.DogkingPVImage.INPUT_TYPES()["required"])

print("\n=== 34. IS_CHANGED 强制每次执行 ===")
for node_name in EXPECTED:
    value = pv.NODE_CLASS_MAPPINGS[node_name].IS_CHANGED()
    check_true(f"{node_name} 返回 NaN",
               isinstance(value, float) and math.isnan(value), value)

print("\n=== 35. 显示名统一格式 ===")
for node_name in EXPECTED:
    display = pv.NODE_DISPLAY_NAME_MAPPINGS[node_name]
    check_true(f"{node_name} 显示名", display.startswith("PromptVault ") and "dogking-tools" in display,
               display)

# ================================================================ 36. 兼容
print("\n=== 36. 老节点的 state 记录不会被 PromptVault 节点误用 ===")
routes()
old = nodes._load_state()
old["pv_legacy"] = {"path": "x.txt", "mode": "全部生成", "index": 9}
old["pv_legacy_img"] = {"kind": "image", "folder": "d", "sort": "数字升序",
                        "mode": "全部模式", "index": 9}
nodes._save_state(old)
check("无 kind 的记录当无记录", run_prompt(node="pv_legacy")[3], 1)
check("kind=image 的记录当无记录", run_image(node="pv_legacy_img")[1], 1)

print("\n=== 37. PromptVault 节点自己的记录格式 ===")
routes()
run_prompt(node="pv_record")
saved = nodes._load_state()
rec = saved.get("pv_record", {})
check("kind = pv", rec.get("kind"), "pv")
check("index = 1", rec.get("index"), 1)
check_true("source 里带服务地址标识", str(rec.get("source", "")).startswith("auto|"), rec.get("source"))
check("source = auto||（自动发现 + 全部分类 + 无类型）", rec.get("source"), "auto||")

routes()
run_prompt(cat="风格", node="pv_record2")
check("选了分类时 source 带分类 id",
      nodes._load_state().get("pv_record2", {}).get("source"), "auto|c1|")

print("\n=== 38. 重置计数后从第 1 条开始 ===")
routes()
run_prompt(node="pv_reset")
run_prompt(node="pv_reset")
state = nodes._load_state()
state["pv_reset"]["index"] = 0
nodes._save_state(state)
check("重置后回到第 1 条", run_prompt(node="pv_reset")[3], 1)

print("\n=== 39. 状态串格式（提示词用「条」，图片用「张」）===")
routes()
check("提示词状态串", run_prompt(node="pv_fmt")[5], "第 1/5 条（全部模式 1-5）")
routes()
check("图片状态串", run_image(node="pv_fmt_img")[3], "第 1/5 张（全部模式 1-5）")

# 恢复真实请求入口（同一进程里可能还有别的测试）
pv._open = _real_open

print("\n" + "=" * 50)
print(f"通过 {len(PASS)} / 失败 {len(FAIL)} / 跳过 {len(SKIP)}")
if FAIL:
    print("失败项:", FAIL)
    sys.exit(1)
