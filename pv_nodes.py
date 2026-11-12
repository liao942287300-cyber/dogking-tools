"""
dogking-tools —— PromptVault 数据源节点 v2（图片+提示词 / 词库）

两个节点：

    DogkingPVPick    图片 + 正向提示词 —— 支持单选 / 批量 / 随机多选
    DogkingPVVault   词库（LORA 触发词 / 编辑指令 / LLM 系统提示词）—— 只能单选

⚠️ 输出口只有「最少必要」的那几个（用户明确要求精简）：

    DogkingPVPick   → ("IMAGE","STRING")  图片 / 提示词
    DogkingPVVault  → ("STRING",)         内容

负向提示词 / 参数 / 当前序号 / 总数 / 状态这些口已经全部砍掉。注意**状态文案没有丢**：
它不再走独立输出口，而是走 `ui.dogking_status`（前端 `host.setStatus()` 画在节点底部）。
同理「第 3/12 张」这类信息仍然在 status 里，只是不再占用输出口。

和 v1 的根本差别：**用户不再"按分类顺序往下翻"，而是先在面板里看见图、点选，
选中的 id 清单写进隐藏 widget 随工作流保存**。所以：

  · 执行阶段只认「已选清单」，分类/搜索只是面板的筛选条件，不参与执行；
  · 排队输出推进的是「清单里的第几张」，不是「分类里的第几张」；
  · 清单变了（用户重新选过）游标就归零 —— 靠 `_sel_hash` 指纹判断，
    清单本身存在工作流里，state.json 只存指纹，避免两份真相。

三条硬约束（都是踩过的坑）：

1. **绝不抛异常**。批量队列靠的是「跳过这张、状态里说原因」，一抛整个队列就断。
   任何网络/解码失败都返回灰图 + 状态文案。
2. **失效的 id 也要返回灰图，不能返回 None**。那个输出口可能有下游连线，
   None 会让下游节点直接崩。
3. **INPUT_TYPES 不做网络请求**。它被 /object_info 和入队校验高频调用，
   一旦等网络（PromptVault 没开时最长几秒）整个界面会卡住。只读缓存 + 踢后台刷新。

网络访问全部在 `pv_client.py`（纯 HTTP，可整体打桩），内存缓存在 `pv_store.py`。
"""

import hashlib
import json
import time

try:  # 作为包被 ComfyUI 导入
    from .nodes import _blank_image, _load_state, _save_state, _state_lock
except ImportError:  # 独立测试环境（按文件路径加载）
    from nodes import _blank_image, _load_state, _save_state, _state_lock

try:  # 作为包被 ComfyUI 导入
    from . import pv_client
except ImportError:  # 独立测试环境：按文件路径加载时没有包上下文，手动补 sys.path
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    import pv_client

# ---------------------------------------------------------------- 常量

# 新语义的状态记录标记。旧的 kind="pv" / "image" / 无 kind 一律当没有记录，
# 避免旧节点残留的游标污染新节点（图片批量加载节点仍在用 kind="image"）。
PV2_KIND = "pv2"

ALL_CATEGORY = pv_client.ALL_CATEGORY

PICK_SINGLE = "单选"
PICK_MULTI = "批量选"
PICK_RANDOM = "随机多选"
PICK_MODES = [PICK_SINGLE, PICK_MULTI, PICK_RANDOM]

OUT_ONESHOT = "一次性全部输出"
OUT_QUEUE = "排队输出"
OUT_MODES = [OUT_ONESHOT, OUT_QUEUE]

# 一次性输出硬上限。超过就自动改走排队 —— 一张 3840x2160 的图 decode 成
# float32 约 100MB，32 张堆成 batch 已经接近 3GB，再多用户必然 OOM。
MAX_ONESHOT = 32
# 一次性输出超过这个张数就在状态里提醒显存
WARN_ONESHOT = 8

TEXT_LORA = "LORA触发词"
TEXT_EDIT = "编辑指令"
TEXT_PROMPT = "LLM系统提示词"
TEXT_TYPES = [TEXT_LORA, TEXT_EDIT, TEXT_PROMPT]
# 界面上的中文名 → PromptVault /api/text 的 type 参数
TEXT_TYPE_KEYS = {
    TEXT_LORA: "loras",
    TEXT_EDIT: "edits",
    TEXT_PROMPT: "system-prompts",
}

_as_int = pv_client._as_int


# ---------------------------------------------------------------- 分类下拉

def _split_choices(addr=""):
    """
    把「风格 / 风格/写实摄影」这种完整路径拆成两级。

    为什么要拆：节点上是两个下拉。一级给「风格」，二级给「写实摄影」——
    用户明确要求二级只显示**叶子名**（完整路径由面板工具条显示）。

    本函数**不做网络请求**（`category_choices` 只读缓存 + 踢后台刷新）。

    @returns {(list, list)} (一级选项, 二级叶子名选项)，各自第一项都是「全部」
    """
    paths = pv_client.category_choices(addr)
    tops = []
    subs = []
    for path in paths:
        text = str(path or "").strip()
        if not text or text == ALL_CATEGORY:
            continue
        if "/" in text:
            leaf = text.split("/", 1)[1]
            if leaf and leaf not in subs:
                subs.append(leaf)
        elif text not in tops:
            tops.append(text)
    return [ALL_CATEGORY] + tops, [ALL_CATEGORY] + subs


def _category_suffix(top="", sub=""):
    """状态条里的分类后缀：` · 风格/写实摄影`。都是「全部」时返回空串。"""
    t = str(top or "").strip()
    s = str(sub or "").strip()
    if t == ALL_CATEGORY:
        t = ""
    if s == ALL_CATEGORY:
        s = ""
    if t and s:
        return f" · {t}/{s}"
    if t:
        return f" · {t}"
    if s:
        return f" · {s}"
    return ""


# ---------------------------------------------------------------- 已选清单

def _parse_sel(raw):
    """
    解析隐藏 widget 里的「已选清单」。

    兼容两种格式（架构文档 §4.3 写的是对象数组，前端实际存的是 {ids,names}，
    两种都得能吃）：
        {"ids": ["a1","a2"], "names": ["图1","图2"]}
        [{"id":"a1","name":"图1"}, ...]
        ["a1","a2"]

    **任何脏数据都不抛异常**：空串 / 畸形 JSON / 非数组 / 元素缺 id 一律丢弃。
    这个函数每次执行都会跑，脏数据抛异常等于节点永久不可用。

    @returns {{ids: list[str], names: list[str]}} 去重保序
    """
    text = str(raw or "").strip()
    if not text:
        return {"ids": [], "names": []}

    try:
        data = json.loads(text)
    except Exception:
        return {"ids": [], "names": []}

    pairs = []
    if isinstance(data, dict):
        raw_ids = data.get("ids")
        raw_names = data.get("names")
        if isinstance(raw_ids, list):
            for i, item in enumerate(raw_ids):
                sid = str(item or "").strip()
                if not sid:
                    continue
                name = ""
                if isinstance(raw_names, list) and i < len(raw_names):
                    name = str(raw_names[i] or "")
                pairs.append((sid, name))
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                sid = str(item.get("id") or "").strip()
                if sid:
                    pairs.append((sid, str(item.get("name") or "")))
            elif isinstance(item, str):
                sid = item.strip()
                if sid:
                    pairs.append((sid, ""))

    ids = []
    names = []
    seen = set()
    for sid, name in pairs:
        if sid in seen:
            continue
        seen.add(sid)
        ids.append(sid)
        names.append(name)
    return {"ids": ids, "names": names}


def _sel_hash(ids):
    """
    清单指纹：`sha1("|".join(ids))[:12]`。

    为什么不把清单存进 state.json：清单已经在工作流的隐藏 widget 里了，
    再存一份就是两份真相，必然对不上。指纹只回答一个问题 ——
    「这次执行看到的清单，和上次推进时是不是同一份？」不是就归零。
    12 位碰撞概率对单人使用可忽略，即使碰撞最坏也只是游标没归零。

    @param {list[str]} ids
    @returns {str} 12 位十六进制
    """
    joined = "|".join(str(i) for i in (ids or []))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- 图片堆叠

def _stack_images(images):
    """
    多张 `[1,H,W,3]` 堆成 `[N,H,W,3]`。

    尺寸不一致时以第 1 张的宽高为基准做 **letterbox（等比缩放 + 居中补边）**：
      · 直接拉伸会把人脸压扁（对角色立绘是灾难）；
      · 居中裁切会切掉内容；
      · 补黑边虽然不变形，但纯黑是**高对比区域**，下游 VAEEncode / IPAdapter
        会把黑条当图像内容编码进 latent，参考图语义被稀释。
    所以填充用 **边缘像素复制（replicate）**：不变形、不丢内容、也没有黑条。

    宽高比差异 < 2% 时直接 resize（省一次 pad，且这种微小差异肉眼看不出补边）。

    没有图片时返回灰图（调用方已判过空，这里只是兜底，**绝不返回 None**）。

    @param {list} images
    @returns {(torch.Tensor, int)} 堆好的 batch / 被补边对齐的张数（0 表示都没补）
    """
    if not images:
        return _blank_image(), 0

    try:
        import torch
        import torch.nn.functional as F
    except Exception:
        return images[0], 0

    base = images[0]
    target_h = int(base.shape[1])
    target_w = int(base.shape[2])

    out = []
    padded = 0
    for image in images:
        try:
            h = int(image.shape[1])
            w = int(image.shape[2])
        except Exception:
            continue
        if h == target_h and w == target_w:
            out.append(image)
            continue

        x = image.permute(0, 3, 1, 2)  # [1,H,W,3] → [1,3,H,W]

        # 宽高比差异 < 2% → 直接缩放，不补边。
        #
        # 为什么判**宽高比**而不是逐维比尺寸：2% 这个阈值的目的是
        # 「别为了一两个像素的差异造出一条假边」。而纯等比放大（512×768 → 1024×1536）
        # 比例差是 0、拉伸完全不变形，逐维比却会算出 50% 的差、误判成「需要补边」。
        src_ratio = w / max(h, 1)
        target_ratio = target_w / max(target_h, 1)
        ratio_diff = abs(src_ratio - target_ratio) / max(src_ratio, target_ratio)
        if ratio_diff < 0.02:
            try:
                x = F.interpolate(x, size=(target_h, target_w), mode="bilinear",
                                  align_corners=False, antialias=True)
            except TypeError:  # 老版本 torch 没有 antialias
                x = F.interpolate(x, size=(target_h, target_w), mode="bilinear",
                                  align_corners=False)
            except Exception:
                out.append(base)
                continue
            out.append(x.permute(0, 2, 3, 1))
            continue

        # 等比缩放后居中补边（边缘复制）
        scale = min(target_w / max(w, 1), target_h / max(h, 1))
        new_w = max(1, int(round(w * scale)))
        new_h = max(1, int(round(h * scale)))
        try:
            x = F.interpolate(x, size=(new_h, new_w), mode="bilinear",
                              align_corners=False, antialias=True)
        except TypeError:
            x = F.interpolate(x, size=(new_h, new_w), mode="bilinear", align_corners=False)
        except Exception:
            out.append(base)
            continue

        left = max(0, (target_w - new_w) // 2)
        top = max(0, (target_h - new_h) // 2)
        right = max(0, target_w - new_w - left)
        bottom = max(0, target_h - new_h - top)
        # 只有**真的补了像素**才算「补边张数」。
        # 等比缩放到正好填满时（例如 512×768 → 1024×1536）四个方向都是 0，
        # 这种图走了补边分支但一个像素都没补，不能算进状态条的「N 张已补边」。
        did_pad = bool(left or right or top or bottom)
        if did_pad:
            try:
                x = F.pad(x, (left, right, top, bottom), mode="replicate")
            except Exception:
                # replicate 对某些 dtype/尺寸不支持时退回零填充，宁可黑边也不要丢图
                x = F.pad(x, (left, right, top, bottom), mode="constant", value=0.0)
            padded += 1

        out.append(x.permute(0, 2, 3, 1))  # 回到 [1,H,W,3]

    if not out:
        return base, 0
    return torch.cat(out, dim=0), padded


# ---------------------------------------------------------------- 排队状态

def advance_pick_state(key, sel_hash, total):
    """
    推进排队游标。整段读-改-写持锁，避免并发执行时计数错乱。

    旧记录（kind 不是 "pv2"）或指纹变了 → 从 0 开始，绝不继承别人的游标。

    @returns {(int, int, bool)} (本次是第几张 1-based, 总数, 还有没有下一张)
    """
    key = str(key or "0")
    total = max(0, _as_int(total, 0))

    with _state_lock:
        state = _load_state()
        rec = state.get(key)
        # 旧记录可能是提示词节点（无 kind）、图片节点（kind="image"）
        # 或 v1 的 PV 节点（kind="pv"），一律当没有记录处理
        if not isinstance(rec, dict) or rec.get("kind") != PV2_KIND or rec.get("sel") != sel_hash:
            rec = {"kind": PV2_KIND, "sel": sel_hash, "index": 0, "total": total, "ts": 0}

        idx = max(0, _as_int(rec.get("index", 0), 0))
        if total <= 0:
            rec.update({"index": 0, "total": 0, "ts": int(time.time())})
            state[key] = rec
            _save_state(state)
            return 0, 0, False
        if idx >= total:
            # 上一轮已经跑完 → 归零重头（沿用现有行为）
            idx = 0

        rec["index"] = idx + 1
        rec["total"] = total
        rec["ts"] = int(time.time())
        state[key] = rec
        _save_state(state)

    return idx + 1, total, (idx + 2) <= total


def notify_next(key, line, total, more, auto):
    """通知前端：还要不要自动排下一次队列（复用老事件，多带 kind 便于前端区分）。"""
    try:
        from server import PromptServer

        PromptServer.instance.send_sync("dogking-tools.next", {
            "node": key,
            "kind": PV2_KIND,
            "index": line,
            "line": line,
            "total": total,
            "should_continue": bool(more and auto),
        })
    except Exception:
        pass


def _result(payload, status):
    """统一包装返回值：ui.dogking_status 给前端画底部状态条。"""
    return {"ui": {"dogking_status": [status]}, "result": payload}


# ---------------------------------------------------------------- 取图

def _load_one(addr, image_id):
    """
    下载 + 解码一张图 → (tensor, err)。

    任何一步失败都返回灰图 + 原因，**绝不抛异常、绝不返回 None**。
    """
    data, err = pv_client.fetch_image_bytes(addr, image_id, "full")
    if err:
        return _blank_image(), err
    return pv_client.tensor_from_bytes(data)


def _positive_of(item):
    """
    从完整元数据里取正向提示词（缺字段一律空串）。

    为什么不再返回 (positive, negative, params)：用户明确要求输出口只留
    「图片 + 正向」，负向和参数没有任何下游消费者了。留着计算出来只是给
    返回值多塞两个没人用的槽位 —— 那正是这次要删掉的东西。
    """
    return str(item.get("positive") or "")


# ================================================================ 节点 1

class DogkingPVPick:
    """从 PromptVault 选图 + 取它的提示词。"""

    @classmethod
    def INPUT_TYPES(cls):
        tops, subs = _split_choices()
        return {
            "required": {
                "服务地址": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "留空 = 自动发现（依次探 8765-8775）；也可填 127.0.0.1:8766 强制指定",
                }),
                "分类": (tops, {
                    "default": ALL_CATEGORY,
                    "tooltip": "一级分类；「全部」= 不筛选",
                }),
                "二级分类": (subs, {
                    "default": ALL_CATEGORY,
                    "tooltip": "二级分类（只显示叶子名）；选了一级后会自动收窄",
                }),
                "选择模式": (PICK_MODES, {
                    "default": PICK_SINGLE,
                    "tooltip": "单选：点一张就确定；批量选：多张 + Shift 连选；随机多选：从当前筛选范围随机抽",
                }),
                "输出方式": (OUT_MODES, {
                    "default": OUT_QUEUE,
                    "tooltip": "排队输出：每次队列吐一张；一次性全部输出：一次吐整个 batch（上限 32 张）",
                }),
                "随机数量": ("INT", {
                    "default": 4, "min": 1, "max": 200, "step": 1,
                    "tooltip": "仅「随机多选」生效",
                }),
                "自动连续": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "排队输出时，每跑完一张自动排下一次队列",
                }),
                "已选": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "已选清单（JSON，随工作流保存，不要手改）",
                }),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    # 只有两个口：图片 + 提示词。负向/参数/序号/总数/状态全部砍掉（用户要求精简）。
    # 状态条仍然要显示，走 ui.dogking_status，不占输出口。
    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("图片", "提示词")
    FUNCTION = "load"
    CATEGORY = "dogking-tools"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        # 永远重跑：排队输出靠的就是「每次执行都重新推进游标」
        return float("NaN")

    def load(self, 服务地址="", 分类=ALL_CATEGORY, 二级分类=ALL_CATEGORY,
             选择模式=PICK_SINGLE, 输出方式=OUT_QUEUE, 随机数量=4,
             自动连续=True, 已选="", unique_id=None):
        addr = str(服务地址 or "").strip()
        sel = _parse_sel(已选)
        ids = sel["ids"]

        if not ids:
            return self._fail("未选择图片")

        cat_part = _category_suffix(分类, 二级分类)

        items, missing, err = pv_client.fetch_items_by_ids(addr, ids)
        if err:
            return self._fail(err)

        by_id = {}
        for item in items or []:
            if isinstance(item, dict):
                by_id[str(item.get("id") or "")] = item

        valid = [i for i in ids if i in by_id]
        missing_n = len(ids) - len(valid)
        if not valid:
            return self._fail("已选图片都已失效（PromptVault 里找不到了）")

        # 一次性输出硬夹 32 张：超过就自动改走排队。
        # ⚠️ **不许静默切换** —— 用户选了 50 张、点「一次性」，结果只出 1 张，
        # 界面上必须有一句话解释，否则用户会以为「怎么只出了一张」。
        # 也不许截前 32 张（语义变了，用户以为出了 50 张）、不许报错拒绝执行
        # （把可用功能变成不可用）。
        note = ""
        mode = OUT_QUEUE if str(输出方式 or "").strip() == OUT_QUEUE else OUT_ONESHOT
        if mode == OUT_ONESHOT and len(valid) > MAX_ONESHOT:
            mode = OUT_QUEUE
            note = (f"清单 {len(valid)} 张超过一次性输出上限（{MAX_ONESHOT}），"
                    f"已自动改为排队输出（本次输出第 1 张）")

        if mode == OUT_QUEUE:
            return self._queued(addr, ids, valid, by_id, cat_part, missing_n,
                                自动连续, unique_id, note)
        return self._one_shot(addr, valid, by_id, cat_part, missing_n)

    # ------------------------------------------------------------ 两条分支

    def _queued(self, addr, ids, valid, by_id, cat_part, missing_n, auto, unique_id, note=""):
        """
        排队输出：只取游标指向的那一张。

        指纹用**原始清单 `ids`**，迭代范围用**有效清单 `valid`** —— 两者必须分开：

          · 身份（防归零）用 `ids`：清单没变，就不该因为某张图被删而重置游标；
          · 范围（跑几步）用 `valid`：失效的图已经跳过了，若还按 `ids` 的长度跑，
            尾部会多出几张灰图 —— 而状态里明明写着「N 张已失效，已跳过」，
            说跳过却给灰图，自相矛盾。
        """
        key = str(unique_id or "0")
        index, total, more = advance_pick_state(key, _sel_hash(ids), len(valid))

        status = f"第 {index}/{total} 张（排队输出{cat_part}）"
        if missing_n > 0:
            status += f"（{missing_n} 张已失效，已跳过）"
        if note:
            status += f" · {note}"

        target = valid[index - 1] if 0 < index <= len(valid) else ""
        item = by_id.get(target) or {}

        if not target:
            # 游标指向的那张恰好失效（或游标和清单对不上）——给灰图，不崩
            notify_next(key, index, total, more, auto)
            return _result((_blank_image(), ""), status)

        image, image_err = _load_one(addr, target)
        if image_err:
            status += f" 图片下载失败：{image_err}"

        # index / total 只用于拼 status（「第 3/12 张」），不再进返回值
        positive = _positive_of(item)
        notify_next(key, index, total, more, auto)
        return _result((image, positive), status)

    def _one_shot(self, addr, valid, by_id, cat_part, missing_n):
        """一次性输出：全部取回来堆成 batch。"""
        images = []
        positives = []
        failed = 0

        for image_id in valid:
            image, image_err = _load_one(addr, image_id)
            if image_err:
                failed += 1
            images.append(image)
            positives.append(_positive_of(by_id.get(image_id) or {}))

        batch, padded = _stack_images(images)
        count = len(images)

        status = f"已输出 {count} 张（一次性{cat_part}）"
        if padded > 0:
            # 补边改变了画面，必须让用户知道（不然会以为图被裁了）
            height = int(batch.shape[1])
            width = int(batch.shape[2])
            status += f" · {padded} 张已补边对齐到 {width}×{height}"
        if count > WARN_ONESHOT:
            status += " · 注意显存"
        if missing_n > 0:
            status += f"（{missing_n} 张已失效，已跳过）"
        if failed > 0:
            status += f"（{failed} 张下载失败，已用灰图占位）"

        # 只返回 (图片, 提示词)。多张时提示词按空行拼接，和原来的 batch 语义一致。
        return _result((batch, "\n\n".join(positives)), status)

    def _fail(self, message):
        """整次执行失败：返回灰图 + 文案，绝不抛异常。"""
        return _result((_blank_image(), ""), message)


# ================================================================ 节点 2

class DogkingPVVault:
    """从 PromptVault 取词库条目（只能单选）。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "服务地址": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "留空 = 自动发现（依次探 8765-8775）；也可填 127.0.0.1:8766 强制指定",
                }),
                "数据源": (TEXT_TYPES, {
                    "default": TEXT_LORA,
                    "tooltip": "LORA触发词 / 编辑指令 / LLM系统提示词",
                }),
                "已选": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "已选词条（JSON，随工作流保存，不要手改）",
                }),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    # 只要「内容」一个口（用户要求）。名称/序号/总数/状态全部砍掉，
    # 状态（含「仅支持单选，已取第 1 条」这类必须说的话）走 ui.dogking_status。
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("内容",)
    FUNCTION = "load"
    CATEGORY = "dogking-tools"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("NaN")

    def load(self, 服务地址="", 数据源=TEXT_LORA, 已选="", unique_id=None):
        addr = str(服务地址 or "").strip()
        sel = _parse_sel(已选)
        ids = sel["ids"]

        if not ids:
            return self._fail("未选择词条")

        text_type = TEXT_TYPE_KEYS.get(str(数据源 or "").strip(), "loras")
        items, err = pv_client.fetch_text(addr, text_type)
        if err:
            return self._fail(err)

        by_id = {}
        for item in items or []:
            if isinstance(item, dict):
                by_id[str(item.get("id") or "")] = item

        # 只支持单选：取清单第一项。
        # 但如果清单里有多条（面板将来开了多选、或用户手工塞了多条），
        # **必须说出来** —— 选了 3 条只出 1 条却没有任何提示，用户会以为坏了。
        target = ids[0]
        item = by_id.get(target)
        if item is None:
            return self._fail("已选词条已失效（PromptVault 里找不到了）")

        content = str(item.get("text") or "")
        name = str(item.get("name") or "")
        status = f"已输出 1 条（{数据源}）"
        if name:
            status += f"：{name}"
        if len(ids) > 1:
            status += f" · 词库节点仅支持单选，已取第 1 条（共选 {len(ids)} 条）"
        if not content:
            status += " · 该词条内容为空"
        return _result((content,), status)

    def _fail(self, message):
        return _result(("",), message)


# ---------------------------------------------------------------- 注册映射

NODE_CLASS_MAPPINGS = {
    "DogkingPVPick": DogkingPVPick,
    "DogkingPVVault": DogkingPVVault,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DogkingPVPick": "PromptVault 图片+提示词（dogking-tools）",
    "DogkingPVVault": "PromptVault 词库（dogking-tools）",
}
