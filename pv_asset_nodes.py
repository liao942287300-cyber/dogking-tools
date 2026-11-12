"""
dogking-tools —— 资产图节点 v2（单张 / 多重）

    DogkingPVAsset    选 1 张资产图 → 图片
    DogkingPVAssets   选 N 张资产图（批量选择）→ 最多 9 个独立图片口

和图片节点（pv_nodes.py）的区别：资产图**没有提示词**，只有一张原图 + 名称；
而且资产图只有 `category` / `project` 两个**平级字符串维度**（不是两级分类），
所以两个下拉互不联动。

输出口只有图片（用户要求精简）：

    DogkingPVAsset   RETURN_TYPES = ("IMAGE",)                    ← 1 位
    DogkingPVAssets  RETURN_TYPES = ("IMAGE",) * 8                ← 8 位，无固定口

🔴 为什么「多重」节点不再有固定口（名称 / 总数 / 状态）：

  用户明确要求「去掉名字总数和状态输出口，去掉模式选择，只保留独立输出口功能」。
  固定口原本存在的唯一技术理由（架构文档 §2.2）是「前端 `addOutput()` 只能尾部追加，
  `removeOutput(i)` 会让 i 之后所有槽位左移重编号，固定口排在前面时增删图片口永远
  只动尾部，连线不会被断」。现在**一个固定口都没有**，整张输出表都是图片口，
  这条理由自然消失 —— 增删永远只发生在尾部。

  「超 8 张」「N 张已失效」这类提示**一个字都不能少**，只是不再占输出口：
  走 `_result` 的 `ui.dogking_status`，前端 `host.setStatus()` 画在节点底部。

三条安全约定：
1. **未使用的图片位返回 `None`** —— 没连线的口 ComfyUI 根本不会去取，安全。
2. **已失效的 id 必须返回 `_blank_image()`** —— 那个口可能有连线，`None` 会让下游崩。
   这个 bug 只在「图恰好在 PromptVault 里被删了」时复现，极难排查。
3. **绝不抛异常** —— 任何失败都返回灰图/空串 + 状态文案。
"""

import torch

try:  # 作为包被 ComfyUI 导入
    from .nodes import _blank_image
    from . import pv_client
    from .pv_nodes import _parse_sel, _result
except ImportError:  # 独立测试环境（按文件路径加载）
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    from nodes import _blank_image
    import pv_client
    from pv_nodes import _parse_sel, _result

# ---------------------------------------------------------------- 常量

# 🔴 端口数量上限。**必须和前端 `web/js/nodes/assets.js` 的 `MAX_PORTS` 保持一致** ——
# 后端决定 RETURN_TYPES 长度（实际有几个口），前端决定显示几个口。
# 不一致的后果：状态条说「只输出前 9 张」但节点上只有 8 个口（或反之）。
MAX_IMAGE_PORTS = 9

ALL_CATEGORY = pv_client.ALL_CATEGORY
NO_PROJECT = pv_client.NO_PROJECT


# ---------------------------------------------------------------- 下拉选项

def _asset_facet_choices():
    """
    资产图的「分类 / 项目」下拉选项。

    必须**只读落盘缓存**（`pv_assets_cache.json`），不能在这里发网络请求：
    INPUT_TYPES 会被 /object_info 和入队校验高频调用，一等网络整个界面就卡住。
    缓存在用户第一次打开面板时由 `asset_facet_choices()` 写入。

    缓存还没有时只给「全部」—— 前端在节点创建时会拉一次并刷新 options。

    @returns {(list, list)} (分类选项, 项目选项)，第一项都是「全部」
    """
    try:
        cached = pv_client._load_assets_cache("") or {}
    except Exception:
        cached = {}

    cats = []
    for item in cached.get("categories") or []:
        if isinstance(item, dict):
            name = str(item.get("path") or "").strip()
        else:
            name = str(item or "").strip()
        if name and name not in cats:
            cats.append(name)

    projects = []
    for item in cached.get("projects") or []:
        name = str(item or "").strip()
        if name and name not in projects:
            projects.append(name)

    return [ALL_CATEGORY] + cats, [ALL_CATEGORY] + projects


def _suffix(category="", project=""):
    """状态条里的维度后缀：` · 角色四视图/凡人`。都是「全部」时返回空串。"""
    cat = str(category or "").strip()
    proj = str(project or "").strip()
    if cat == ALL_CATEGORY:
        cat = ""
    if proj == ALL_CATEGORY:
        proj = ""
    if cat and proj:
        return f" · {cat}/{proj}"
    if cat:
        return f" · {cat}"
    if proj:
        return f" · {proj}"
    return ""


# ---------------------------------------------------------------- 取图

def _load_asset(addr, asset_id):
    """
    取一张资产图的原图 → (tensor, err)。

    失败返回灰图 + 原因，**绝不抛异常、绝不返回 None**。
    """
    data, err = pv_client.fetch_asset_image(addr, asset_id, "full")
    if err:
        return _blank_image(), err
    return pv_client.tensor_from_bytes(data)


def _asset_name(item):
    return str((item or {}).get("name") or "")


def _limit_short_edge(image, short_edge):
    """
    短边限制：0 = 原图；>0 = 把短边缩到该值，长边等比缩放。

    为什么不裁切也不补边：用户要的是「限制输出尺寸」（省显存 / 统一下游输入），
    不是「构图对齐」。裁切会切掉内容，补边会引入黑条被 VAE 编码进 latent
    （这条在节点 1 的 batch 堆叠上已经踩过，见 pv_nodes._stack_images）。

    返回 (image, (h, w))，第二个元素给状态条用；short_edge 为 0 时原样返回。
    """
    n = int(short_edge or 0)
    if image is None:
        return image, (0, 0)
    h, w = int(image.shape[1]), int(image.shape[2])
    if n <= 0 or min(h, w) == n:
        return image, (h, w)

    scale = n / float(min(h, w))
    nh, nw = max(1, int(round(h * scale))), max(1, int(round(w * scale)))
    # interpolate 要 NCHW；antialias=True 在缩小时质量明显更好（默认 False 会有锯齿）
    x = image.permute(0, 3, 1, 2)
    x = torch.nn.functional.interpolate(
        x, size=(nh, nw), mode="bilinear", align_corners=False, antialias=True
    )
    return x.permute(0, 2, 3, 1).contiguous(), (nh, nw)


# ================================================================ 节点 3

class DogkingPVAsset:
    """选 1 张资产图 → 图片。"""

    @classmethod
    def INPUT_TYPES(cls):
        cats, projects = _asset_facet_choices()
        return {
            "required": {
                "服务地址": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "留空 = 自动发现（依次探 8765-8775）；也可填 127.0.0.1:8766 强制指定",
                }),
                # 「项目」排在「分类」前面：用户的心智模型是「先选项目，再看这个项目下
                # 有哪些分类」。项目是粗粒度、分类是细粒度，粗的在上。
                # ⚠️ 改顺序必须同步改 `load()` 的形参顺序和前端面板工具条
                #    （asset.js 的 getFilters 里 crumb 拼成 `项目 · 分类`）——
                #    ComfyUI 按**位置**把 widget 值传给 load()，顺序错位不会报错，
                #    只会静默把项目当分类用。
                "项目": (projects, {
                    "default": ALL_CATEGORY,
                    "tooltip": "资产图项目；「未分项目」= 没有填项目的资产图",
                }),
                "分类": (cats, {
                    "default": ALL_CATEGORY,
                    "tooltip": "资产图分类；「全部」= 不筛选",
                }),
                "短边尺寸": ("INT", {
                    # 默认 1024：ComfyUI 下游大多按 1024 级别工作，原图 4K 直接喂进去
                    # 既慢又吃显存；用户要原图时填 0 即可。
                    "default": 1024, "min": 0, "max": 8192, "step": 64,
                    "tooltip": "0 = 原图不缩放；默认 1024 = 短边缩到 1024，长边等比（不裁切、不变形）",
                }),
                "已选": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "已选资产图（JSON，随工作流保存，不要手改）",
                }),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    # 只有一个口：图片。名称 / 状态不再占输出口（用户要求精简），
    # 名称改由前端预览态和状态条显示。
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("图片",)
    FUNCTION = "load"
    CATEGORY = "dogking-tools"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("NaN")

    def load(self, 服务地址="", 项目=ALL_CATEGORY, 分类=ALL_CATEGORY,
             短边尺寸=0, 已选="", unique_id=None):
        addr = str(服务地址 or "").strip()
        sel = _parse_sel(已选)
        ids = sel["ids"]

        if not ids:
            return self._fail("未选择资产图")

        items, missing, err = pv_client.fetch_assets_by_ids(addr, ids)
        if err:
            return self._fail(err)

        by_id = {}
        for item in items or []:
            if isinstance(item, dict):
                by_id[str(item.get("id") or "")] = item

        # 只支持单张：取清单第一项
        target = ids[0]
        item = by_id.get(target)
        if item is None:
            # 失效：灰图 + 文案。**不能返回 None** —— 这个口可能有下游连线
            return self._fail("已选资产图已失效（PromptVault 里找不到了）")

        image, image_err = _load_asset(addr, target)
        name = _asset_name(item)
        status = f"已输出 1 张（{name or '未命名'}）"
        if len(ids) > 1:
            status += f" · 单张节点仅取第 1 张（共选 {len(ids)} 张）"
        if image_err:
            status += f" · 图片下载失败：{image_err}"
        else:
            image, (out_h, out_w) = _limit_short_edge(image, 短边尺寸)
            if int(短边尺寸 or 0) > 0:
                status += f" · 短边 {int(短边尺寸)} → {out_w}×{out_h}"
        return _result((image,), status)

    def _fail(self, message):
        return _result((_blank_image(),), message)


# ================================================================ 节点 4

class DogkingPVAssets:
    """选 N 张资产图 → 最多 9 个独立图片口（批量选择）。"""

    @classmethod
    def INPUT_TYPES(cls):
        cats, projects = _asset_facet_choices()
        return {
            "required": {
                "服务地址": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "留空 = 自动发现（依次探 8765-8775）；也可填 127.0.0.1:8766 强制指定",
                }),
                # 和节点 3 一致：「项目」在「分类」上面（粗粒度在上，先选项目再看分类）。
                # ⚠️ 改顺序必须同步改 `load()` 的形参顺序 —— ComfyUI 按**位置**传值，
                #    错位不会报错，只会静默把项目当分类用。
                "项目": (projects, {
                    "default": ALL_CATEGORY,
                    "tooltip": "资产图项目；「未分项目」= 没有填项目的资产图",
                }),
                "分类": (cats, {
                    "default": ALL_CATEGORY,
                    "tooltip": "资产图分类；「全部」= 不筛选",
                }),
                "短边尺寸": ("INT", {
                    # 默认 1024：ComfyUI 下游大多按 1024 级别工作，原图 4K 直接喂进去
                    # 既慢又吃显存；用户要原图时填 0 即可。
                    "default": 1024, "min": 0, "max": 8192, "step": 64,
                    "tooltip": "0 = 原图不缩放；默认 1024 = 短边缩到 1024，长边等比（不裁切、不变形）",
                }),
                # 「输出模式」已按用户要求整段删除：只剩独立输出口一种行为。
                "已选": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "已选资产图（JSON，随工作流保存，不要手改）",
                }),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    # 8 个口全是图片口，**没有固定口**（名称/总数/状态已按用户要求删除）。
    # 因为没有固定口，整张输出表都是可增删的尾部 → 前端 removeOutput 只会动尾部，
    # 不会再出现「删中间导致后面所有连线左移」的问题。
    RETURN_TYPES = ("IMAGE",) * MAX_IMAGE_PORTS
    RETURN_NAMES = tuple(f"图{i + 1}" for i in range(MAX_IMAGE_PORTS))
    FUNCTION = "load"
    CATEGORY = "dogking-tools"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("NaN")

    def load(self, 服务地址="", 项目=ALL_CATEGORY, 分类=ALL_CATEGORY,
             短边尺寸=0, 已选="", unique_id=None):
        addr = str(服务地址 or "").strip()
        sel = _parse_sel(已选)
        ids = sel["ids"]

        if not ids:
            return self._fail("未选择资产图")

        items, missing, err = pv_client.fetch_assets_by_ids(addr, ids)
        if err:
            return self._fail(err)

        by_id = {}
        for item in items or []:
            if isinstance(item, dict):
                by_id[str(item.get("id") or "")] = item

        # 逐张取图。失效的 id **也要占一个位置并给灰图** —— 那个口可能有连线，
        # 返回 None 会让下游节点直接崩（而且只在「图被删了」时复现，极难排查）。
        images = []
        failed = 0
        missing_n = 0

        for asset_id in ids:
            item = by_id.get(asset_id)
            if item is None:
                missing_n += 1
                images.append(_blank_image())
                continue
            image, image_err = _load_asset(addr, asset_id)
            if image_err:
                failed += 1
            else:
                # 每张各自缩到短边 N，**保持各自的宽高比**（不是统一成一个尺寸）
                image, _dims = _limit_short_edge(image, 短边尺寸)
            images.append(image)

        total = len(ids)
        suffix = _suffix(分类, 项目)

        # ---- 状态文案（R19：任何自动改变意图的分支都要说清原因）----
        # 这个字符串**不再进返回值**，而是走 ui.dogking_status 由前端画在节点底部。
        status = f"已输出 {total} 张（独立输出口{suffix}）"
        if total > MAX_IMAGE_PORTS:
            # 不许静默截断：用户选了 10 张却只有 8 个口，必须说明白
            status += (f" · 已选 {total} 张，仅前 {MAX_IMAGE_PORTS} 张有独立输出口"
                       f"（其余不会输出）")
        if missing_n > 0:
            status += f" · {missing_n} 张已失效，已用灰图占位"
        if failed > 0:
            status += f" · {failed} 张下载失败，已用灰图占位"
        if int(短边尺寸 or 0) > 0:
            status += f" · 短边已限制为 {int(短边尺寸)}"

        used = images[:MAX_IMAGE_PORTS]
        # 未使用的位填 None：没连线的口 ComfyUI 根本不会取，安全
        payload = tuple(used) + (None,) * (MAX_IMAGE_PORTS - len(used))

        return _result(payload, status)

    def _fail(self, message):
        """
        整次执行失败（PromptVault 没开 / 接口报错 / 清单为空）。

        🔴 8 个图片位全部填 `_blank_image()`，**绝不能填 `None`**。

        我最初这里写的是 `None`，理由是「没连线的口 ComfyUI 不会去取」——
        但那个前提在**失败路径上不成立**：走到 _fail 说明用户已经在用这个节点了，
        图1/图2/图3 早就连好下游。填 None 会让**所有已连线的下游节点一起崩**，
        而且用户只会以为是 PromptVault 的问题，根本想不到是上游给了 None。

        填满 8 个而不是「按当前口数」：`_fail` 里看不到前端连了几个口，全填最安全。
        代价 8 × 64×64×3 float32 ≈ 96KB，可忽略。
        """
        return _result((_blank_image(),) * MAX_IMAGE_PORTS, message)


# ---------------------------------------------------------------- 注册映射

NODE_CLASS_MAPPINGS = {
    "DogkingPVAsset": DogkingPVAsset,
    "DogkingPVAssets": DogkingPVAssets,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DogkingPVAsset": "PromptVault 资产图（dogking-tools）",
    "DogkingPVAssets": "PromptVault 资产图批量选择（dogking-tools）",
}
