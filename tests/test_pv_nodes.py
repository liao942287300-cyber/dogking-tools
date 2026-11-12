# -*- coding: utf-8 -*-
"""
pv_nodes.py 测试（节点 1 图片+提示词 / 节点 2 词库）

**这份文件是重写的**：旧版测的是 `_iterate` / `_advance` / `source_key`，
那些函数在 v2 已经全部删除，所以旧版必然全红。

组织方式：**按「出口」分组**，不按功能点分组。
功能点列法会漏掉「功能看起来对、但某条 early return 走错了」的情况
（T04 的 P0-1 就是这么漏的：单个 id 失效那条路测了，整体失败那条路没测）。

本文件的每个 `test_*` 都能被 pytest 收集，也可以直接 `python tests/test_pv_nodes.py` 跑。
HTTP 全部打桩，状态文件用临时目录，**不碰用户真实 state.json**。
"""

import importlib.util
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 插件根目录


# ---------------------------------------------------------------- 模块加载
#
# 加载顺序有讲究：pv_nodes 里写的是 `import pv_client`（独立加载时的兜底分支），
# 所以必须**先把自己的 pv_client 实例注册进 sys.modules**，否则 pv_nodes 会从磁盘
# 再导入一份新的，我的桩就打不上了。
#
# 每个测试文件都这样做，保证各自的模块实例互不污染（pytest 是同一个进程）。

def _load(name, alias):
    spec = importlib.util.spec_from_file_location(alias, os.path.join(HERE, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_nodes = _load("nodes", "dk_nodes_for_pv_nodes")
sys.modules["nodes"] = _nodes
# 状态文件指到临时目录：绝不写用户真实的 state.json
_TMPDIR = tempfile.mkdtemp(prefix="dk_pv_nodes_")
_nodes.STATE_FILE = os.path.join(_TMPDIR, "state.json")

pv_client = _load("pv_client", "dk_pv_client_for_pv_nodes")
sys.modules["pv_client"] = pv_client
# 缓存文件也指到临时目录
pv_client.CATEGORY_CACHE_FILE = os.path.join(_TMPDIR, "pv_categories_cache.json")
pv_client.ASSETS_CACHE_FILE = os.path.join(_TMPDIR, "pv_assets_cache.json")

pv = _load("pv_nodes", "dk_pv_nodes")

# ---- 绑定自检（防止多个测试文件互相覆盖 sys.modules 时"悄悄拿错实例"）----
#
# 背景：四个测试文件各自加载一份 nodes / pv_client / pv_nodes 实例，并临时占用
# `sys.modules["nodes"]` / `["pv_client"]` 这两个**共享键**（因为生产代码里写的是
# `from nodes import ...` / `import pv_client`，绕不开）。
#
# 正确性依赖一个隐含不变量：**每个文件在 import 依赖之前，先把共享键指向自己的实例**。
# 这个不变量一旦被破坏（比如将来有人新增测试文件时顺序写反），症状会是
# 「某个测试莫名其妙红」（例如游标期望 4 得到 1），和源码 bug 长得一模一样，极难排查。
#
# 所以在 import 期就把"我拿到的确实是我加载的那份"断言出来 —— 出问题时是
# 一条清晰的 ImportError 式报错，而不是一个令人困惑的测试失败。
assert pv._load_state.__module__ == _nodes.__name__, \
    f"pv_nodes 的 _load_state 来自 {pv._load_state.__module__}，不是本文件的 {_nodes.__name__}"
assert pv._blank_image.__module__ == _nodes.__name__, \
    "pv_nodes 的 _blank_image 不是本文件加载的 nodes 实例"


# ---------------------------------------------------------------- 打桩

class _Assets:
    """可变的假资产库：id → {name, positive, negative, params, h, w, fill}。"""

    def __init__(self):
        self.items = {}
        self.down = False          # True = 模拟 PromptVault 未运行
        self.fetch_error = None    # 非空 = 模拟接口报错

    def reset(self):
        self.items.clear()
        self.down = False
        self.fetch_error = None

    def add(self, item_id, name="", h=48, w=64, fill=0.5):
        self.items[item_id] = {
            "id": item_id, "name": name or item_id,
            "positive": "正向-" + item_id, "negative": "", "params": "参数-" + item_id,
            "h": h, "w": w, "fill": fill,
        }

    # --- 打桩用的函数 ---
    def fetch_items_by_ids(self, addr="", ids=None):
        if self.down:
            return None, list(ids or []), "PromptVault 未运行（试过 8765-8775），请先打开 PromptVault"
        if self.fetch_error:
            return None, list(ids or []), self.fetch_error
        got = [i for i in (ids or []) if i in self.items]
        items = [{k: v for k, v in self.items[i].items() if k not in ("h", "w", "fill")} for i in got]
        return items, [i for i in (ids or []) if i not in self.items], None

    def fetch_image_bytes(self, addr="", image_id="", size="full"):
        if self.down:
            return None, "PromptVault 未运行（试过 8765-8775），请先打开 PromptVault"
        if image_id not in self.items:
            return None, "HTTP 404"
        return b"fake-bytes", None

    def tensor_from_bytes(self, data):
        import torch
        # 用「当前正在解码的 id」决定尺寸/填充值，让每张图可区分
        spec = self.items.get(self._decoding, {"h": 48, "w": 64, "fill": 0.5})
        return torch.full((1, spec["h"], spec["w"], 3), float(spec["fill"]), dtype=torch.float32), None

    def fetch_text(self, addr="", text_type="loras"):
        if self.down:
            return None, "PromptVault 未运行（试过 8765-8775），请先打开 PromptVault"
        return [
            {"id": "t1", "name": "姬紫月", "text": "触发词内容"},
            {"id": "t2", "name": "空触发词", "text": ""},
        ], None


ASSETS = _Assets()
ASSETS._decoding = ""


def _install_stubs():
    """把桩装到 pv_nodes 真正使用的 pv_client 实例上。"""
    pv.pv_client.fetch_items_by_ids = ASSETS.fetch_items_by_ids
    pv.pv_client.fetch_image_bytes = ASSETS.fetch_image_bytes
    pv.pv_client.tensor_from_bytes = ASSETS.tensor_from_bytes
    pv.pv_client.fetch_text = ASSETS.fetch_text
    # _load_one 里先用 fetch_image_bytes 再 tensor_from_bytes，
    # 用 _load_one 包一层只是为了记录「正在解码哪个 id」，让每张图可区分
    pv._load_one = _load_one


def _load_one(addr, image_id):
    import torch
    if ASSETS.down:
        return pv._blank_image(), "PromptVault 未运行（试过 8765-8775），请先打开 PromptVault"
    spec = ASSETS.items.get(image_id)
    if spec is None:
        return pv._blank_image(), "HTTP 404"
    return torch.full((1, spec["h"], spec["w"], 3), float(spec["fill"]), dtype=torch.float32), None


_install_stubs()


def sel(*ids):
    """构造前端写进隐藏 widget 的清单。"""
    return json.dumps({"ids": list(ids), "names": [ASSETS.items.get(i, {}).get("name", "") for i in ids]})


def fresh_state():
    """每个用例开始前清空状态文件，避免游标互相干扰。"""
    try:
        os.remove(_nodes.STATE_FILE)
    except OSError:
        pass


def status_of(result):
    """
    状态文案 —— 它**不再占输出口**，走 `ui.dogking_status`（前端画在节点底部）。

    输出口精简成 (图片, 正向) 之后，所有「第 N/M 张」「N 张已失效」这类信息
    都只能从这里取。这也是这次改动最容易漏测的地方：输出口断言全绿，
    但提示文案悄悄没了。
    """
    return result["ui"]["dogking_status"][0]


def index_of(result):
    """从状态条里解析排队游标「第 3/12 张」→ (3, 12)。"""
    m = re.search(r"第 (\d+)/(\d+) 张", status_of(result))
    assert m, f"状态条里没有游标：{status_of(result)!r}"
    return int(m.group(1)), int(m.group(2))


# ================================================================ _parse_sel

def test_parse_sel_accepts_three_formats():
    """前端实际存 {ids,names}，架构文档写的是对象数组，两种都得吃。"""
    assert pv._parse_sel('{"ids":["a1","a2"],"names":["图1","图2"]}') == {
        "ids": ["a1", "a2"], "names": ["图1", "图2"]}
    assert pv._parse_sel('[{"id":"a1","name":"图1"}]') == {"ids": ["a1"], "names": ["图1"]}
    assert pv._parse_sel('["a1","a2"]')["ids"] == ["a1", "a2"]


def test_parse_sel_rejects_dirty_input_without_raising():
    """脏数据一律收敛成空清单 —— 这个函数每次执行都跑，抛异常等于节点永久不可用。"""
    for raw in ("", None, "   ", "{not json", "123", "null", '{"ids":"不是数组"}'):
        assert pv._parse_sel(raw) == {"ids": [], "names": []}, f"输入 {raw!r} 应得空清单"


def test_parse_sel_dedupes_and_skips_items_without_id():
    assert pv._parse_sel('["a1","a2","a1"]')["ids"] == ["a1", "a2"]
    assert pv._parse_sel('[{"name":"没有id"},{"id":"a1"}]')["ids"] == ["a1"]


# ================================================================ _sel_hash

def test_sel_hash_is_stable_and_order_sensitive():
    h = pv._sel_hash(["a1", "a2"])
    assert len(h) == 12
    assert h == pv._sel_hash(["a1", "a2"])
    # 顺序变了就是另一份清单（节点 4 的输出口顺序有意义）
    assert h != pv._sel_hash(["a2", "a1"])
    assert h != pv._sel_hash(["a1", "a3"])


def test_sel_hash_empty_list():
    assert len(pv._sel_hash([])) == 12


# ================================================================ _stack_images

def _img(h, w, fill=0.5):
    import torch
    return torch.full((1, h, w, 3), float(fill), dtype=torch.float32)


def test_stack_images_same_size():
    batch, padded = pv._stack_images([_img(48, 64), _img(48, 64), _img(48, 64)])
    assert tuple(batch.shape) == (3, 48, 64, 3)
    assert padded == 0


def test_stack_images_empty_returns_gray_not_none():
    """空列表必须返回灰图 —— 返回 None 会让下游崩。"""
    batch, padded = pv._stack_images([])
    assert batch is not None
    assert tuple(batch.shape)[0] == 1
    assert padded == 0


def test_stack_images_letterbox_pads_and_counts():
    """尺寸差 >2% 走补边分支，并计入 padded。"""
    batch, padded = pv._stack_images([_img(100, 200), _img(100, 100)])
    assert tuple(batch.shape) == (2, 100, 200, 3)
    assert padded == 1


def test_stack_images_ratio_within_2_percent_does_not_pad():
    """宽高比差 <2% → 直接 resize，不补边（1024×1530 对 1024×1536 只差 0.39%）。"""
    _, padded = pv._stack_images([_img(1536, 1024), _img(1530, 1024)])
    assert padded == 0


def test_stack_images_pure_scale_counts_zero_padding():
    """
    纯等比放大（512×768 → 1024×1536）比例差为 0 → 走 resize 分支，
    **不能**被计入「已补边」—— 一个像素都没补，状态条不能撒谎。
    """
    batch, padded = pv._stack_images([_img(1536, 1024), _img(768, 512)])
    assert tuple(batch.shape) == (2, 1536, 1024, 3)
    assert padded == 0


def test_stack_images_padding_uses_edge_replicate_not_black():
    """
    补边用边缘复制而不是黑边：黑条是高对比区域，会被 VAE 当成图像内容编码。
    用非均匀图验证（全 0 假图分不出 replicate 和 constant）。
    """
    import torch
    base = torch.full((1, 100, 200, 3), 0.2)
    base[:, :, 0, :] = 0.9     # 左边缘
    base[:, :, -1, :] = 0.7    # 右边缘
    narrow = torch.full((1, 100, 100, 3), 0.2)
    narrow[:, :, 0, :] = 0.9
    narrow[:, :, -1, :] = 0.7

    batch, padded = pv._stack_images([base, narrow])
    assert padded == 1
    row = batch[1, 50]
    assert torch.allclose(row[0], row[50]), "左填充应等于缩放后的左边缘"
    assert not torch.allclose(row[0], torch.zeros(3)), "填充不能是黑的"


# ================================================================ advance_pick_state

def test_advance_first_call_starts_at_one():
    fresh_state()
    index, total, more = pv.advance_pick_state("k1", "hashA", 3)
    assert (index, total, more) == (1, 3, True)


def test_advance_consecutive_then_wraps():
    fresh_state()
    seq = [pv.advance_pick_state("k2", "hashA", 3)[0] for _ in range(4)]
    assert seq == [1, 2, 3, 1], "跑完一轮应归零回第 1 张"


def test_advance_fingerprint_change_resets():
    fresh_state()
    pv.advance_pick_state("k3", "hashA", 3)
    pv.advance_pick_state("k3", "hashA", 3)
    index, _, _ = pv.advance_pick_state("k3", "hashB", 3)
    assert index == 1, "换清单（指纹变）必须归零"


def test_advance_does_not_inherit_old_kind():
    """旧 kind="image" 的记录是图片批量加载节点写的，绝不能被 PV 节点继承。"""
    fresh_state()
    _nodes._save_state({"k4": {"kind": "image", "folder": "x", "index": 5}})
    index, _, _ = pv.advance_pick_state("k4", "hashA", 3)
    assert index == 1


def test_advance_zero_total_is_safe():
    fresh_state()
    assert pv.advance_pick_state("k5", "hashA", 0) == (0, 0, False)


# ================================================================ 节点 1：出口 1（空清单）

def test_pick_empty_selection():
    fresh_state()
    r = pv.DogkingPVPick().load(已选="")
    payload = r["result"]
    assert len(payload) == 2, "输出口只剩 (图片, 正向)"
    assert payload[1] == "", "空清单时正向是空串"
    assert status_of(r) == "未选择图片"
    assert payload[0] is not None, "空清单也要给灰图，不能返回 None"


# ================================================================ 节点 1：出口 2（整体失败）

def test_pick_promptvault_down_returns_gray_and_message():
    """整体失败路径：不能抛异常、不能返回 None。"""
    fresh_state()
    ASSETS.reset()
    ASSETS.add("a1")
    ASSETS.down = True
    try:
        r = pv.DogkingPVPick().load(已选=sel("a1"))
        payload = r["result"]
        assert len(payload) == 2
        assert "未运行" in status_of(r)
        assert payload[0] is not None
        assert payload[1] == ""
    finally:
        ASSETS.down = False


def test_pick_interface_error_returns_message():
    fresh_state()
    ASSETS.reset()
    ASSETS.add("a1")
    ASSETS.fetch_error = "接口返回 HTTP 500"
    try:
        r = pv.DogkingPVPick().load(已选=sel("a1"))
        assert "500" in status_of(r)
    finally:
        ASSETS.fetch_error = None


# ================================================================ 节点 1：出口 3（一次性）

def test_pick_oneshot_batch_and_texts():
    fresh_state()
    ASSETS.reset()
    for i in ("a1", "a2", "a3"):
        ASSETS.add(i)
    r = pv.DogkingPVPick().load(已选=sel("a1", "a2", "a3"), 输出方式="一次性全部输出")
    assert len(r["result"]) == 2, "输出口只剩 图片 + 正向（负向/参数/序号/总数/状态已删）"
    img, pos = r["result"]
    assert tuple(img.shape) == (3, 48, 64, 3)
    assert pos == "正向-a1\n\n正向-a2\n\n正向-a3", "正向按空行拼接"
    assert "已输出 3 张" in status_of(r), "张数信息不能丢，只是改走状态条"


def test_pick_oneshot_over_limit_degrades_with_verbatim_message():
    """
    超过 32 张必须降级为排队，**并且逐字告知** —— 不许静默切换、不许截断、不许报错。
    """
    fresh_state()
    ASSETS.reset()
    many = [f"m{i}" for i in range(50)]
    for i in many:
        ASSETS.add(i)
    r = pv.DogkingPVPick().load(已选=json.dumps({"ids": many}), 输出方式="一次性全部输出", unique_id="k_over")
    payload = r["result"]
    assert tuple(payload[0].shape) == (1, 48, 64, 3), "降级后只出 1 张"
    status = status_of(r)
    assert "清单 50 张超过一次性输出上限（32），已自动改为排队输出（本次输出第 1 张）" in status
    # 游标仍在状态条里（原来靠「当前序号 / 总数」两个输出口，现在没有那两个口了）
    assert index_of(r) == (1, 50)


def test_pick_oneshot_exactly_32_still_oneshot():
    fresh_state()
    ASSETS.reset()
    ids = [f"e{i}" for i in range(32)]
    for i in ids:
        ASSETS.add(i)
    r = pv.DogkingPVPick().load(已选=json.dumps({"ids": ids}), 输出方式="一次性全部输出", unique_id="k_32")
    assert tuple(r["result"][0].shape) == (32, 48, 64, 3)
    assert "已自动改为排队" not in status_of(r)


# ================================================================ 节点 1：出口 4（排队）

def test_pick_queue_cycles():
    fresh_state()
    ASSETS.reset()
    for i in ("a1", "a2", "a3"):
        ASSETS.add(i)
    seq = [index_of(pv.DogkingPVPick().load(
        已选=sel("a1", "a2", "a3"), 输出方式="排队输出", unique_id="k_q"))[0]
        for _ in range(4)]
    assert seq == [1, 2, 3, 1]
    r = pv.DogkingPVPick().load(已选=sel("a1", "a2", "a3"), 输出方式="排队输出", unique_id="k_q")
    assert tuple(r["result"][0].shape) == (1, 48, 64, 3)
    assert r["result"][1] == "正向-a2", "正向跟着游标走"


def test_pick_queue_deleted_item_does_not_reset_or_emit_gray():
    """
    清单 12 张、其中 2 张在 PromptVault 里被删：
      · 指纹用原始清单 → 游标不归零
      · 范围用有效清单 → 只跑 10 步，尾部**不吐灰帧**
    """
    fresh_state()
    ASSETS.reset()
    ids = [f"t{i}" for i in range(1, 13)]
    for i in ids:
        ASSETS.add(i)
    del ASSETS.items["t11"]
    del ASSETS.items["t12"]

    seen = []
    for _ in range(12):
        r = pv.DogkingPVPick().load(已选=json.dumps({"ids": ids}), 输出方式="排队输出", unique_id="k_del")
        idx, tot = index_of(r)
        seen.append((idx, tot, tuple(r["result"][0].shape)))

    assert all(tot == 10 for _, tot, _ in seen), "总数应为有效张数 10"
    assert all(shape != (1, 64, 64, 3) for _, _, shape in seen), "不该出现灰帧"
    assert sorted({idx for idx, _, _ in seen}) == list(range(1, 11))


def test_pick_queue_cursor_continues_after_middle_deletion():
    """删掉清单里的一张，游标应继续推进而不是回到 1。"""
    fresh_state()
    ASSETS.reset()
    five = [f"a{i}" for i in range(1, 6)]
    for i in five:
        ASSETS.add(i)
    seq = [index_of(pv.DogkingPVPick().load(
        已选=json.dumps({"ids": five}), 输出方式="排队输出", unique_id="k_mid"))[0]
        for _ in range(3)]
    assert seq == [1, 2, 3]
    del ASSETS.items["a5"]
    nxt = index_of(pv.DogkingPVPick().load(
        已选=json.dumps({"ids": five}), 输出方式="排队输出", unique_id="k_mid"))
    assert nxt[0] == 4, "游标应继续到 4，不是回到 1"


# ================================================================ 节点 1：出口 5（部分失效）

def test_pick_partial_missing_reports_and_skips():
    fresh_state()
    ASSETS.reset()
    for i in ("a1", "a2"):
        ASSETS.add(i)
    r = pv.DogkingPVPick().load(已选=sel("a1", "zzz", "a2"), 输出方式="一次性全部输出")
    assert tuple(r["result"][0].shape) == (2, 48, 64, 3), "失效的那张应被跳过（不出灰帧）"
    assert "已输出 2 张" in status_of(r)
    assert "1 张已失效" in status_of(r)


def test_pick_all_missing_returns_gray_with_message():
    fresh_state()
    ASSETS.reset()
    r = pv.DogkingPVPick().load(已选=json.dumps({"ids": ["zzz1", "zzz2"]}), 输出方式="排队输出")
    assert "已失效" in status_of(r)
    assert r["result"][0] is not None, "全部失效也要给灰图"


# ================================================================ 节点 2

def test_vault_single_selection():
    ASSETS.reset()
    r = pv.DogkingPVVault().load(已选=json.dumps({"ids": ["t1"], "names": ["姬紫月"]}))
    assert r["result"] == ("触发词内容",), "词库只输出「内容」一个口"
    # 名称不再占输出口，但必须能在状态条里看到（否则用户不知道取的是哪一条）
    assert "姬紫月" in status_of(r)


def test_vault_empty_text_is_reported():
    ASSETS.reset()
    r = pv.DogkingPVVault().load(已选=json.dumps({"ids": ["t2"]}))
    assert "内容为空" in status_of(r)


def test_vault_empty_selection():
    r = pv.DogkingPVVault().load(已选="")
    assert r["result"] == ("",)
    assert status_of(r) == "未选择词条"


def test_vault_missing_item():
    ASSETS.reset()
    r = pv.DogkingPVVault().load(已选=json.dumps({"ids": ["nope"]}))
    assert "已失效" in status_of(r)


def test_vault_multi_selection_truncates_with_note():
    """只支持单选，但清单里有多条时必须说明，不能静默截断。"""
    ASSETS.reset()
    r = pv.DogkingPVVault().load(已选=json.dumps({"ids": ["t1", "t2"]}))
    assert "仅支持单选" in status_of(r)
    assert "共选 2 条" in status_of(r)


def test_vault_promptvault_down():
    ASSETS.reset()
    ASSETS.down = True
    try:
        r = pv.DogkingPVVault().load(已选=json.dumps({"ids": ["t1"]}))
        assert "未运行" in status_of(r)
    finally:
        ASSETS.down = False


# ================================================================ 注册映射

def test_node_mappings_only_two_nodes():
    """v1 的 4 个节点已硬删，这里只应剩 2 个。"""
    assert set(pv.NODE_CLASS_MAPPINGS) == {"DogkingPVPick", "DogkingPVVault"}


def test_pick_return_types():
    """用户要求：节点 1 只留「图片 + 正向」。"""
    assert pv.DogkingPVPick.RETURN_TYPES == ("IMAGE", "STRING")
    assert pv.DogkingPVPick.RETURN_NAMES == ("图片", "提示词")


def test_vault_return_types():
    """用户要求：节点 2 只留「内容」。"""
    assert pv.DogkingPVVault.RETURN_TYPES == ("STRING",)
    assert pv.DogkingPVVault.RETURN_NAMES == ("内容",)


# ---------------------------------------------------------------- 直接运行入口

if __name__ == "__main__":
    failed = []
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001 - 测试入口要抓住所有异常
                failed.append((name, exc))
                print(f"FAIL {name}: {exc}")
    print("\n" + "=" * 50)
    print(f"通过 {passed} / 失败 {len(failed)}")
    if failed:
        print("失败项:", [n for n, _ in failed])
        sys.exit(1)
