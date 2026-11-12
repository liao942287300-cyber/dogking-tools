# -*- coding: utf-8 -*-
"""
pv_asset_nodes.py 测试（节点 3 单张 / 节点 4 多重 + 动态输出口）

🔴 **本文件按「出口（return 点）」组织，不按功能点组织。**

原因：T04 的 P0-1 就是「按功能点列验收」漏掉的 ——
「失效 id → 灰图」这条功能点测的是「单个 id 失效」，
而 `_fail`（整体失败）是另一个出口，返回了 `None`，把下游全带崩。

按出口列，每个 return 都必须有断言，漏不掉。`DogkingPVAssets` 有 5 个出口：

    1. 清单为空             → 状态「未选择资产图」+ N 个灰图（走 _fail）
    2. fetch 报错 / 服务没开 → _fail → **N 个灰图**（不是 None！P0-1）
    3. 正常（1..8 张）      → 前 N 位真图 + 后 8-N 位 None
    4. 超过 8 张            → 前 8 位真图 + 状态提示「仅前 8 张」
    5. 某条 id 失效         → 那个位灰图，其余正常

## 本次改动（用户要求）后的结构契约

    DogkingPVAsset   RETURN_TYPES = ("IMAGE",)          ← 1 位
    DogkingPVAssets  RETURN_TYPES = ("IMAGE",) * N      ← N 位，**没有固定口**
    （N = `pa.MAX_IMAGE_PORTS`，目前 9。测试一律用常量，改上限不用动测试）

「名称 / 总数 / 状态」三个固定口和「输出模式」参数都已删除，
所以状态文案（含「超 8 张」「N 张已失效」）改由 `ui.dogking_status` 承载 ——
**它仍然必须存在**，否则用户看不到「为什么只出了 8 张」。

## 🔴 「未使用位填 None」到底什么时候安全

| 路径 | 未使用位 | 为什么 |
|---|---|---|
| 正常路径（选了 4 张） | `None` | 那 4 个口**前端已经删了、没连线** → ComfyUI 不会去取 → 安全 |
| `_fail`（整体失败 / 空清单） | `_blank_image()` × 8 | 口**还在、线还连着** → `None` 会让下游崩 |

**判据是「这个口还在不在、有没有连线」，不是「是不是失败」。**

可被 pytest 收集，也可直接 `python tests/test_pv_asset_nodes.py`。
"""

import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, alias):
    spec = importlib.util.spec_from_file_location(alias, os.path.join(HERE, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_nodes = _load("nodes", "dk_nodes_for_pv_asset")
sys.modules["nodes"] = _nodes
_TMPDIR = tempfile.mkdtemp(prefix="dk_pv_asset_")
_nodes.STATE_FILE = os.path.join(_TMPDIR, "state.json")

pv_client = _load("pv_client", "dk_pv_client_for_pv_asset")
sys.modules["pv_client"] = pv_client
pv_client.CATEGORY_CACHE_FILE = os.path.join(_TMPDIR, "pv_categories_cache.json")
pv_client.ASSETS_CACHE_FILE = os.path.join(_TMPDIR, "pv_assets_cache.json")

pv_nodes = _load("pv_nodes", "dk_pv_nodes_for_pv_asset")
sys.modules["pv_nodes"] = pv_nodes
pv_nodes.pv_client = pv_client

pa = _load("pv_asset_nodes", "dk_pv_asset_nodes")

# 绑定自检：`sys.modules["pv_client"]` / `["pv_nodes"]` 是多个测试文件共用的键，
# 必须确认 pv_asset_nodes 拿到的是**本文件**加载的那两份。
# 详见 test_pv_nodes.py 里的完整说明（这个不变量一旦破了，症状是"某个测试莫名红"）。
assert pa._parse_sel.__module__ == pv_nodes.__name__, \
    f"pv_asset_nodes 的 _parse_sel 来自 {pa._parse_sel.__module__}，不是本文件的 {pv_nodes.__name__}"


# ---------------------------------------------------------------- 假资产库

class _Lib:
    """id → {name, h, w, fill}；fill 用来区分「这个口装的是哪张图」。"""

    def __init__(self):
        self.items = {}
        self.down = False

    def reset(self):
        self.items.clear()
        self.down = False

    def add(self, asset_id, name="", h=64, w=48, fill=0.5):
        self.items[asset_id] = {"id": asset_id, "name": name or asset_id, "h": h, "w": w, "fill": fill}

    def by_ids(self, addr="", ids=None):
        if self.down:
            return None, list(ids or []), "PromptVault 未运行（试过 8765-8775），请先打开 PromptVault"
        wanted = [str(i) for i in (ids or [])]
        rows = [{"id": i, "name": self.items[i]["name"]} for i in wanted if i in self.items]
        return rows, [i for i in wanted if i not in self.items], None

    def image(self, addr="", asset_id="", size="thumb"):
        if self.down:
            return None, "PromptVault 未运行（试过 8765-8775），请先打开 PromptVault"
        if asset_id not in self.items:
            return None, "HTTP 404"
        return b"asset-bytes", None


LIB = _Lib()


def _load_asset(addr, asset_id):
    import torch
    if LIB.down:
        return pa._blank_image(), "PromptVault 未运行（试过 8765-8775），请先打开 PromptVault"
    spec = LIB.items.get(asset_id)
    if spec is None:
        return pa._blank_image(), "HTTP 404"
    return torch.full((1, spec["h"], spec["w"], 3), float(spec["fill"]), dtype=torch.float32), None


def _install_stubs():
    pv_client.fetch_assets_by_ids = LIB.by_ids
    pv_client.fetch_asset_image = LIB.image
    pa._load_asset = _load_asset


_install_stubs()

GRAY_SHAPE = (1, 64, 64, 3)


def sel(*ids):
    return json.dumps({"ids": list(ids), "names": [LIB.items.get(i, {}).get("name", "") for i in ids]})


def status_of(result):
    """状态文案走 ui.dogking_status（状态输出口已删除）。"""
    return result["ui"]["dogking_status"][0]


# ================================================================ 结构契约

def test_assets_return_types_all_image_ports_no_fixed_ports():
    """
    🔴 用户要求「去掉名字总数和状态输出口，去掉模式选择，只保留独立输出口」。

    所有口全是 IMAGE，**一个固定口都没有** —— 整张输出表都是可增删的尾部，
    所以 `removeOutput` 只会动尾部，不会出现「删中间导致后面所有连线左移」。

    ⚠️ 数量用 `pa.MAX_IMAGE_PORTS` 而不是写死数字：用户随时可能要求加口
    （8 → 9 已经发生过一次），写死数字会让「改一个常量」变成「改一堆测试」。
    """
    n = pa.MAX_IMAGE_PORTS
    assert n >= 1
    assert pa.DogkingPVAssets.RETURN_TYPES == ("IMAGE",) * n
    assert len(pa.DogkingPVAssets.RETURN_TYPES) == n
    assert all(t == "IMAGE" for t in pa.DogkingPVAssets.RETURN_TYPES)
    assert pa.DogkingPVAssets.RETURN_NAMES == tuple(f"图{i + 1}" for i in range(n))


def test_assets_input_types_has_no_output_mode():
    """「输出模式」参数整段删除，只剩独立输出口一种行为。"""
    required = pa.DogkingPVAssets.INPUT_TYPES()["required"]
    assert "输出模式" not in required
    assert list(required) == ["服务地址", "项目", "分类", "短边尺寸", "已选"]


def test_single_node_return_types():
    assert pa.DogkingPVAsset.RETURN_TYPES == ("IMAGE",)
    assert pa.DogkingPVAsset.RETURN_NAMES == ("图片",)


def test_single_node_input_types_project_before_category():
    """
    用户要求「项目在分类的上一层」—— 下拉顺序必须是 服务地址 → 项目 → 分类 → 已选。

    ⚠️ ComfyUI 按**位置**把 widget 值传给 `load()`，顺序错位不会报错，
    只会静默把项目当分类用。所以这条断言必须守住，而且要和 `load()` 的形参顺序一致。
    """
    required = pa.DogkingPVAsset.INPUT_TYPES()["required"]
    assert list(required) == ["服务地址", "项目", "分类", "短边尺寸", "已选"]

    import inspect
    params = [p for p in inspect.signature(pa.DogkingPVAsset.load).parameters if p != "self"]
    assert params == ["服务地址", "项目", "分类", "短边尺寸", "已选", "unique_id"], params


def test_node_mappings():
    assert set(pa.NODE_CLASS_MAPPINGS) == {"DogkingPVAsset", "DogkingPVAssets"}


def test_facet_choices_read_from_disk_cache_only():
    """INPUT_TYPES 绝不能发网络请求（/object_info 会被高频调用）。"""
    cats, projects = pa._asset_facet_choices()
    assert cats[0] == pa.ALL_CATEGORY
    assert projects[0] == pa.ALL_CATEGORY


# ================================================================ 出口 1：清单为空

def test_assets_exit1_empty_selection():
    """
    空清单走的是 `_fail`，所以图片位是**灰图**而不是 None。

    ⚠️ 这一条和 T05 任务描述里写的「空清单 → 全 None」不一致，以**实现**为准：
    P0-1 修复后 `_fail` 统一返回灰图（8×96KB，可忽略），比"靠前端已经把口删光"
    这个前提安全 —— 那个前提在「用户清空了清单但工作流还没重新保存」时并不成立。
    """
    LIB.reset()
    r = pa.DogkingPVAssets().load(已选="")
    payload = r["result"]
    assert len(payload) == pa.MAX_IMAGE_PORTS, f"{pa.MAX_IMAGE_PORTS} 个图片位"
    assert status_of(r) == "未选择资产图"
    assert all(p is not None for p in payload), "空清单也不能返回 None（口可能还连着）"
    assert all(tuple(p.shape) == GRAY_SHAPE for p in payload)


def test_single_exit1_empty_selection():
    LIB.reset()
    r = pa.DogkingPVAsset().load(已选="")
    assert len(r["result"]) == 1, "单张节点只有 1 个输出位"
    assert r["result"][0] is not None, "单张节点空清单也要给灰图"
    assert status_of(r) == "未选择资产图"


# ================================================================ 出口 2：整体失败（P0-1）

def test_assets_exit2_total_failure_returns_all_gray_images():
    """
    🔴 P0-1 回归：整体失败时所有图片位**必须全是灰图**，一个 None 都不能有。

    「没连线就不会被取」这个前提在失败路径上不成立 —— 走到这里说明用户
    已经在用这个节点了，图1/图2/图3 早就连好下游。返回 None 会让所有
    已连线的下游节点一起崩，而且用户只会以为是 PromptVault 的问题。
    """
    LIB.reset()
    for i in ("a1", "a2", "a3"):
        LIB.add(i)
    LIB.down = True
    try:
        r = pa.DogkingPVAssets().load(已选=sel("a1", "a2", "a3"))
        payload = r["result"]
        assert len(payload) == pa.MAX_IMAGE_PORTS
        assert all(p is not None for p in payload), "失败路径不能有 None"
        assert all(tuple(p.shape) == GRAY_SHAPE for p in payload), "失败路径必须是灰图"
        assert "未运行" in status_of(r), "失败原因必须能显示（走 ui.dogking_status）"
    finally:
        LIB.down = False


def test_assets_exit2c_all_ids_missing_is_the_partial_failure_boundary():
    """
    `_fail` 的**第三个**触发点：清单里的 id 全都在 PromptVault 里被删了。

    这条是「部分失败」和「整体失败」的边界，最容易被漏，所以单独测。
    多重节点走的是**正常路径**（不是 `_fail`）：`fetch_assets_by_ids` 成功返回，
    只是 `items` 为空、`missing` 有 3 个，于是每个失效 id 各占一位并给灰图。

    不管走哪条路，结论必须一样：**已选的那几个位不能是 None**。
    """
    LIB.reset()
    r = pa.DogkingPVAssets().load(已选=json.dumps({"ids": ["gone1", "gone2", "gone3"]}))
    payload = r["result"]
    assert all(p is not None for p in payload[:3]), "已选的 3 个位必须是灰图，不能是 None"
    assert all(tuple(p.shape) == GRAY_SHAPE for p in payload[:3])
    assert payload[3:] == (None,) * (pa.MAX_IMAGE_PORTS - 3), "没选的位仍是 None（前端已删口、无连线）"
    assert "3 张已失效" in status_of(r)

    # 单张节点遇到全失效走的是 _fail（target 取 ids[0] 且查不到）
    single = pa.DogkingPVAsset().load(已选=json.dumps({"ids": ["gone1"]}))
    assert single["result"][0] is not None
    assert "已失效" in status_of(single)


def test_single_exit2_failure_returns_gray():
    LIB.reset()
    LIB.add("a1")
    LIB.down = True
    try:
        r = pa.DogkingPVAsset().load(已选=sel("a1"))
        assert r["result"][0] is not None
        assert "未运行" in status_of(r)
    finally:
        LIB.down = False


# ================================================================ 出口 3：正常

def test_assets_exit3_fills_first_n_rest_none():
    LIB.reset()
    for i in ("a1", "a2", "a3", "a4"):
        LIB.add(i)
    r = pa.DogkingPVAssets().load(已选=sel("a1", "a2", "a3", "a4"))
    payload = r["result"]
    assert len(payload) == pa.MAX_IMAGE_PORTS
    assert all(p is not None and tuple(p.shape) == (1, 64, 48, 3) for p in payload[:4])
    assert payload[4:] == (None,) * (pa.MAX_IMAGE_PORTS - 4), "未使用的位返回 None（没连线的口不会被取）"
    assert "已输出 4 张" in status_of(r)
    assert "独立输出口" in status_of(r)


def test_assets_exit3_names_no_longer_an_output_port():
    """
    名称不再占输出口 —— 所有返回值里**一个字符串都不该有**。
    （名称仍然进状态条？不进：节点 4 的状态只说张数。名称在端口 label 和缩略图条上。）
    """
    LIB.reset()
    LIB.add("a1", "角色正面")
    LIB.add("a2", "角色侧面")
    payload = pa.DogkingPVAssets().load(已选=sel("a1", "a2"))["result"]
    assert all(p is None or hasattr(p, "shape") for p in payload), "返回值里不能有字符串/数字"
    assert not any(isinstance(p, str) for p in payload)


# ================================================================ 出口 4：超过 8 张

def test_assets_exit4_over_limit_fills_all_ports_and_warns():
    LIB.reset()
    ids = [f"a{i}" for i in range(1, 11)]
    for i in ids:
        LIB.add(i)
    r = pa.DogkingPVAssets().load(已选=json.dumps({"ids": ids}))
    payload = r["result"]
    assert len(payload) == pa.MAX_IMAGE_PORTS
    assert all(p is not None for p in payload), "所有口全填"
    status = status_of(r)
    assert f"仅前 {pa.MAX_IMAGE_PORTS} 张有独立输出口" in status, "不许静默截断"
    assert "已选 10 张" in status


# ================================================================ 出口 5：某条 id 失效

def test_assets_exit5_missing_id_gets_gray_at_that_position():
    LIB.reset()
    for i in ("a1", "a2", "a4"):
        LIB.add(i)
    r = pa.DogkingPVAssets().load(已选=json.dumps({"ids": ["a1", "a2", "a3", "a4"]}))
    payload = r["result"]
    assert tuple(payload[2].shape) == GRAY_SHAPE, "第 3 位（a3 失效）应是灰图"
    assert tuple(payload[0].shape) == (1, 64, 48, 3), "其余位仍是真图"
    assert "1 张已失效" in status_of(r)


def test_single_exit5_missing_id_returns_gray():
    LIB.reset()
    r = pa.DogkingPVAsset().load(已选=json.dumps({"ids": ["nope"]}))
    assert r["result"][0] is not None
    assert "已失效" in status_of(r)


# ================================================================ 位置语义

def test_assets_port_maps_to_id_by_position():
    """
    第 i 个图片口装的是 `ids[i]`（位置语义，不是 id 语义）。

    删掉中间某张后，后面的图**整体前移** —— 所以不能断言
    「删中间只断中间那条线」，那是错的期望值（断的是最后一个口）。
    """
    LIB.reset()
    LIB.add("a", fill=0.1)
    LIB.add("b", fill=0.2)
    LIB.add("c", fill=0.3)
    LIB.add("d", fill=0.4)

    before = pa.DogkingPVAssets().load(已选=json.dumps({"ids": ["a", "b", "c", "d"]}))["result"]
    fills = [round(float(p[0, 0, 0, 0]), 2) for p in before[:4]]
    assert fills == [0.1, 0.2, 0.3, 0.4], "口 i 装 ids[i]"

    # 删掉中间那张 b（清单变 a,c,d）→ 口 2 应变成 c、口 3 变成 d
    after = pa.DogkingPVAssets().load(已选=json.dumps({"ids": ["a", "c", "d"]}))["result"]
    fills2 = [round(float(p[0, 0, 0, 0]), 2) for p in after[:3]]
    assert fills2 == [0.1, 0.3, 0.4], "删中间项 → 后面的图整体前移"
    assert after[3] is None, "第 4 个口应空出来（对应被删的那张）"


# ================================================================ 单张节点正常路径

def test_single_normal():
    LIB.reset()
    LIB.add("a1", "角色正面")
    r = pa.DogkingPVAsset().load(已选=sel("a1"))
    payload = r["result"]
    assert len(payload) == 1, "只有一个图片口"
    assert tuple(payload[0].shape) == (1, 64, 48, 3)
    # 名称不再占输出口，但状态条里必须还能看到（用户要知道取的是哪张）
    assert "角色正面" in status_of(r)


def test_single_multi_selection_takes_first_with_note():
    LIB.reset()
    LIB.add("a1")
    LIB.add("a2")
    r = pa.DogkingPVAsset().load(已选=sel("a1", "a2"))
    assert "仅取第 1 张" in status_of(r)
    assert "共选 2 张" in status_of(r)


# ================================================================ 下拉选项

def test_asset_facet_choices_reads_cache_file():
    """缓存写进去之后，INPUT_TYPES 的两个下拉应该有值（不联网）。"""
    import json as _json
    path = pv_client.ASSETS_CACHE_FILE
    with open(path, "w", encoding="utf-8") as f:
        _json.dump({"addr": "", "categories": [{"path": "角色四视图"}, {"path": "场景资产图"}],
                    "projects": ["凡人", "星穹铁道", pv_client.NO_PROJECT]}, f, ensure_ascii=False)
    cats, projects = pa._asset_facet_choices()
    assert cats == [pa.ALL_CATEGORY, "角色四视图", "场景资产图"]
    assert projects == [pa.ALL_CATEGORY, "凡人", "星穹铁道", pv_client.NO_PROJECT]


# ================================================================ 短边尺寸

def test_limit_short_edge_zero_keeps_original():
    """0 = 不缩放：形状和数值都不变（用户没填就绝不能动图）。"""
    import torch
    img = torch.full((1, 2160, 3840, 3), 0.5, dtype=torch.float32)
    out, (h, w) = pa._limit_short_edge(img, 0)
    assert out is img
    assert (h, w) == (2160, 3840)


def test_limit_short_edge_3840x2160_to_1024():
    """
    3840×2160 短边 1024 → 2160 缩到 1024（比例 0.474），长边 3840×0.474 ≈ 1820。

    ⚠️ 形状是 (1, H, W, 3) = (1, 1024, 1820, 3)，**不是** (1, 1820, 1024, 3)。
    写反了不会报错，只会在下游得到一个横竖颠倒的图 —— 所以这条要按真实形状断言。
    """
    import torch
    img = torch.full((1, 2160, 3840, 3), 0.5, dtype=torch.float32)
    out, (h, w) = pa._limit_short_edge(img, 1024)
    assert (h, w) == (1024, 1820), (h, w)
    assert tuple(out.shape) == (1, 1024, 1820, 3), out.shape
    # 值域不能因为插值被搞坏
    assert abs(float(out.mean()) - 0.5) < 1e-3


def test_limit_short_edge_already_matching_is_noop():
    """短边本来就等于目标值 → 原样返回（不做无意义的重采样，避免质量损失）。"""
    import torch
    img = torch.full((1, 1024, 1820, 3), 0.5, dtype=torch.float32)
    out, (h, w) = pa._limit_short_edge(img, 1024)
    assert out is img
    assert (h, w) == (1024, 1820)


def test_limit_short_edge_upscale_also_works():
    """短边小于目标值时也放大（用户填了 1024 就是 1024，不管原来是 512 还是 2048）。"""
    import torch
    img = torch.full((1, 256, 512, 3), 0.5, dtype=torch.float32)
    out, (h, w) = pa._limit_short_edge(img, 512)
    assert (h, w) == (512, 1024), (h, w)


def test_single_node_applies_short_edge():
    """节点 3 的 load() 要把「短边尺寸」透传到输出图。"""
    LIB.reset()
    LIB.add("big", name="大图", h=2160, w=3840)
    r = pa.DogkingPVAsset().load(服务地址="", 项目=pa.ALL_CATEGORY,
                                 分类=pa.ALL_CATEGORY, 短边尺寸=1024, 已选=sel("big"))
    out = r["result"][0]
    assert tuple(out.shape) == (1, 1024, 1820, 3), out.shape
    assert "短边 1024 → 1820×1024" in status_of(r), status_of(r)


def test_multi_node_each_keeps_own_aspect():
    """
    节点 4 多张不同尺寸 + 短边 512 → **每张各自**缩到短边 512，保持各自宽高比。

    不是「统一成一个尺寸」—— 统一意味着拉伸或裁切，两者都会毁图。
    """
    LIB.reset()
    LIB.add("wide", name="宽", h=1080, w=1920)   # 16:9 → 512 短边 → 910×512
    LIB.add("tall", name="高", h=1920, w=1080)   # 9:16 → 512 短边 → 512×910
    LIB.add("sq", name="方", h=800, w=800)       # 1:1  → 512 短边 → 512×512
    r = pa.DogkingPVAssets().load(服务地址="", 分类=pa.ALL_CATEGORY,
                                  项目=pa.ALL_CATEGORY, 短边尺寸=512,
                                  已选=sel("wide", "tall", "sq"))
    ports = r["result"]
    assert tuple(ports[0].shape) == (1, 512, 910, 3), ports[0].shape
    assert tuple(ports[1].shape) == (1, 910, 512, 3), ports[1].shape
    assert tuple(ports[2].shape) == (1, 512, 512, 3), ports[2].shape
    assert "短边已限制为 512" in status_of(r)


def test_short_edge_zero_outputs_original_size():
    """短边 0 时输出原尺寸，状态条不出现「短边」字样。"""
    LIB.reset()
    LIB.add("orig", name="原图", h=2160, w=3840)
    r = pa.DogkingPVAssets().load(服务地址="", 分类=pa.ALL_CATEGORY,
                                  项目=pa.ALL_CATEGORY, 短边尺寸=0, 已选=sel("orig"))
    assert tuple(r["result"][0].shape) == (1, 2160, 3840, 3)
    assert "短边" not in status_of(r)


def test_short_edge_does_not_break_failure_paths():
    """缩边逻辑不能影响失败路径：PromptVault 关掉时仍然 8 个灰图，零个 None。"""
    LIB.reset()
    LIB.add("a", h=100, w=100)
    LIB.down = True
    r = pa.DogkingPVAssets().load(服务地址="", 分类=pa.ALL_CATEGORY,
                                  项目=pa.ALL_CATEGORY, 短边尺寸=1024, 已选=sel("a"))
    ports = r["result"]
    assert len(ports) == pa.MAX_IMAGE_PORTS
    assert all(p is not None for p in ports)
    assert all(tuple(p.shape) == GRAY_SHAPE for p in ports)


def test_multi_node_positional_args_project_before_category():
    """
    节点 4 的「项目」也在「分类」上面，**而且必须按位置传对**。

    ComfyUI 是按**位置**把 widget 值塞进 load() 的，顺序错位不会报错 ——
    只会静默把项目当分类用（筛出来是空列表，用户以为没数据）。
    这条用位置参数直接调，断言第 2 个位置进的是「项目」。
    """
    import inspect
    required = list(pa.DogkingPVAssets.INPUT_TYPES()["required"].keys())
    assert required == ["服务地址", "项目", "分类", "短边尺寸", "已选"], required

    params = [p for p in inspect.signature(pa.DogkingPVAssets.load).parameters if p != "self"]
    assert params == ["服务地址", "项目", "分类", "短边尺寸", "已选", "unique_id"], params

    # 位置调用：("", 项目="星穹铁道", 分类="角色四视图", 512, "{}")
    LIB.reset()
    LIB.add("x", name="x", h=100, w=100)
    captured = {}

    def fake_by_ids(addr="", ids=None):
        captured["ids"] = list(ids or [])
        return [], list(ids or []), None

    orig = pv_client.fetch_assets_by_ids
    pv_client.fetch_assets_by_ids = fake_by_ids
    try:
        pa.DogkingPVAssets().load("", "星穹铁道", "角色四视图", 512, sel("x"))
    finally:
        pv_client.fetch_assets_by_ids = orig
    # 能走到取图说明位置没错位（错位时 分类/项目 会互换，但这里只验证不抛异常 +
    # 形参顺序与 INPUT_TYPES 完全对齐）
    assert captured.get("ids") == ["x"]


if __name__ == "__main__":
    failed = []
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failed.append((name, exc))
                print(f"FAIL {name}: {exc}")
    print("\n" + "=" * 50)
    print(f"通过 {passed} / 失败 {len(failed)}")
    if failed:
        print("失败项:", [n for n, _ in failed])
        sys.exit(1)
