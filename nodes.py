"""
dogking-tools —— ComfyUI 提示词批量加载节点

读取 PromptVault（或任意工具）导出的 .txt / .csv 提示词文件，
按「全部生成 / 范围选取 / 编号选择」三种模式逐行输出。

配套前端扩展：文件选择按钮 + 重置计数按钮 + 自动连续排队。
"""

import csv
import io
import json
import os
import re
import threading

NODE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(NODE_DIR, "state.json")

MODE_ALL = "全部生成"
MODE_RANGE = "范围选取"
MODE_INDEX = "编号选择"
MODES = [MODE_ALL, MODE_RANGE, MODE_INDEX]

_state_lock = threading.Lock()


# ---------------------------------------------------------------- 状态持久化

def _load_state():
    """读取状态文件。坏文件 / 不存在都当作空。"""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(data):
    """原子写入，避免写一半断电把状态搞坏。"""
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"[dogking-tools] 保存状态失败：{e}")


# ---------------------------------------------------------------- 文件解析

def _resolve_path(path):
    """
    把用户填的路径解析成真实路径。

    常见脏输入（都会让 os.path.isabs 判错、被当成相对路径拼到 input 下）：
      · 从别处复制带首尾引号
      · 开头粘了 BOM / 零宽字符 —— strip() 去不掉，必须显式清
      · 中文输入法打出的全角冒号
      · 正斜杠反斜杠混用

    绝对路径（有盘符 / UNC）原样用，其余按 ComfyUI 的 input 目录解析。
    """
    # 非字符串（前端传了数字 / 对象 / 数组 / 布尔）一律当空处理。
    # 否则下面 (path or "").strip() 会抛 AttributeError，接口直接 500：
    # /list_images 的 folder、/count 的 path 都是从这里过的。
    if not isinstance(path, str):
        path = ""

    raw = path
    path = (path or "").strip()

    # 1. 去掉 BOM 和零宽字符（这些不是空白，strip 处理不了）
    for ch in ("\ufeff", "\u200b", "\u200c", "\u200d", "\u2060"):
        path = path.replace(ch, "")

    # 2. 剥掉各种引号（可能粘在两头）
    for ch in ('"', "'", "“", "”", "‘", "’", "「", "」"):
        path = path.strip(ch)

    # 3. 全角冒号 → 半角（中文输入法很容易打错）
    path = path.replace("：", ":").strip()

    if not path:
        return ""

    # 4. 有盘符（C:\ / C:/）或 UNC（\\server）→ 绝对路径，直接用
    if re.match(r"^[A-Za-z]:[\\/]", path) or path.startswith("\\\\") or path.startswith("//"):
        real = os.path.normpath(path)
    elif path.startswith("\\") or path.startswith("/"):
        real = os.path.normpath(path)
    else:
        try:
            import folder_paths
            real = os.path.normpath(os.path.join(folder_paths.get_input_directory(), path))
        except Exception:
            real = os.path.abspath(path)

    if real != raw:
        print(f"[dogking-tools] 路径解析：{raw!r} -> {real!r}")
    return real


def read_prompts(path):
    """
    读取 txt / csv 提示词文件。

    txt：一行一条，空行跳过。
    csv：优先按表头找「提示词 / positive / prompt」列，找不到就用第 3 列
         （兼容 PromptVault 导出的「序号,名称,正向提示词」）。

    @returns {(list[str], str|None)} 行列表 / 错误信息
    """
    real = _resolve_path(path)
    if not real:
        return [], "请先选择提示词文件"
    if not os.path.isfile(real):
        return [], f"文件不存在：{real}"

    try:
        with open(real, "r", encoding="utf-8-sig", errors="replace") as f:
            raw = f.read()
    except Exception as e:
        return [], f"读取失败：{e}"

    if not raw.strip():
        return [], "文件是空的"

    ext = os.path.splitext(real)[1].lower()

    if ext == ".csv":
        rows = list(csv.reader(io.StringIO(raw)))
        if not rows:
            return [], "CSV 是空的"

        header = [str(c).strip().lower() for c in rows[0]]
        has_header = any(
            ("提示词" in c) or ("positive" in c) or ("prompt" in c) or ("名称" in c)
            for c in header
        )

        col = 2  # PromptVault 导出格式：序号,名称,正向提示词
        if has_header:
            for i, c in enumerate(header):
                if ("提示词" in c) or ("positive" in c) or ("prompt" in c):
                    col = i
                    break

        body = rows[1:] if has_header else rows
        out = []
        for r in body:
            if not r:
                continue
            cell = r[col] if len(r) > col else ",".join(r)
            cell = str(cell or "").strip()
            if cell:
                out.append(cell)
        if not out:
            return [], "CSV 里没有解析到提示词列"
        return out, None

    # 其它一律按纯文本处理
    lines = [ln.strip() for ln in raw.splitlines()]
    out = [ln for ln in lines if ln]
    if not out:
        return [], "文件里没有非空行"
    return out, None


def count_lines(path):
    """只数行数，给前端预览用。"""
    prompts, err = read_prompts(path)
    return {"total": len(prompts), "error": err}


# ---------------------------------------------------------------- 节点

class DogkingPromptLoader:
    """
    按行加载提示词文件。

    每执行一次输出一行；「自动连续」打开时，前端会在本次执行结束后
    自动排下一次队列，直到这个范围跑完。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "文件路径": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "txt / csv 的完整路径，也可以用右侧「选择文件」按钮挑",
                }),
                "模式": (MODES, {
                    "default": MODE_ALL,
                    "tooltip": "全部生成：从第 1 行跑到最后一行；范围选取：只跑指定的行区间；编号选择：固定输出某一行",
                }),
                "起始行": ("INT", {
                    "default": 1, "min": 1, "max": 999999, "step": 1,
                    "tooltip": "仅「范围选取」模式生效",
                }),
                "结束行": ("INT", {
                    "default": 0, "min": 0, "max": 999999, "step": 1,
                    "tooltip": "仅「范围选取」模式生效；填 0 表示到最后一行",
                }),
                "编号": ("INT", {
                    "default": 1, "min": 1, "max": 999999, "step": 1,
                    "tooltip": "仅「编号选择」模式生效",
                }),
                "自动连续": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "打开后每生成完一张会自动排下一行，直到跑完整个范围",
                }),
                # 「重置计数」不再是输入参数：它以前是常驻 BOOLEAN，
                # 用户忘了关就会每次都把 index 归零、永远卡在第 1 行。
                # 现在改成前端的一次性按钮，点一下直接调 /dogking-tools/reset。
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("STRING", "INT", "INT", "STRING")
    RETURN_NAMES = ("提示词", "当前行号", "总行数", "状态")
    FUNCTION = "load"
    CATEGORY = "dogking-tools"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        # 返回 NaN 让 ComfyUI 每次都重新执行这个节点 —— 批量生成靠的就是这个
        return float("NaN")

    def load(self, 文件路径, 模式, 起始行, 结束行, 编号, 自动连续, unique_id=None):
        key = str(unique_id or "0")
        prompts, err = read_prompts(文件路径)
        total = len(prompts)

        if err:
            return self._result("", 0, 0, err, key, False)

        with _state_lock:
            state = _load_state()
            rec = state.get(key)
            # state.json 被手工编辑成 {"12": 5} 时 rec 会是 int，
            # 后面 rec.get(...) 直接 AttributeError，节点报错、这张图跑不出来。
            # 脏数据一律当没有记录处理（下面会按「换文件/换模式」重新初始化）。
            if not isinstance(rec, dict):
                rec = {}

            # 换了文件或换了模式 → 计数清零重来
            if rec.get("path") != 文件路径 or rec.get("mode") != 模式:
                rec = {"path": 文件路径, "mode": 模式, "index": 0}

            if 模式 == MODE_INDEX:
                line = max(1, min(int(编号), total))
                rec["index"] = 0
                more = False
            else:
                if 模式 == MODE_RANGE:
                    a = max(1, int(起始行))
                    b = int(结束行) if int(结束行) > 0 else total
                    lo, hi = (a, b) if a <= b else (b, a)   # 40-20 自动变成 20-40
                    lo = min(max(1, lo), total)
                    hi = min(max(1, hi), total)
                    if lo > hi:
                        lo, hi = hi, lo
                else:
                    lo, hi = 1, total

                idx = int(rec.get("index", 0))
                line = lo + idx
                if line > hi:
                    # 这一轮跑完了：归零，下次执行重新从 lo 开始
                    rec["index"] = 0
                    line = lo
                    more = False
                else:
                    more = (line + 1) <= hi
                    rec["index"] = idx + 1

            state[key] = rec
            _save_state(state)

        text = prompts[line - 1]
        span = f"{lo}-{hi}" if 模式 != MODE_INDEX else str(line)
        status = f"第 {line}/{total} 行（{模式} {span}）"

        # 通知前端：还要不要继续排下一行
        try:
            from server import PromptServer
            PromptServer.instance.send_sync("dogking-tools.next", {
                "node": key,
                "line": line,
                "total": total,
                "should_continue": bool(more and 自动连续),
            })
        except Exception:
            pass

        return self._result(text, line, total, status, key, more)

    def _result(self, text, line, total, status, key, more):
        return {
            "ui": {
                "text": [text],
                "dogking_status": [status],
            },
            "result": (text, line, total, status),
        }


# ---------------------------------------------------------------- 图片批量加载
#
# 和提示词节点同一套思路：每执行一次吐一张图，进度写进 state.json，
# 前端「自动连续」负责排下一次队列。区别只在数据源是图片文件。
# 状态记录的 kind 字段用来区分两类节点 —— 提示词节点的老记录没有 kind，
# 直接当「没有记录」处理，两边互不干扰。

IMG_MODE_ALL = "全部模式"
IMG_MODE_RANGE = "范围模式"
IMG_MODES = [IMG_MODE_ALL, IMG_MODE_RANGE]

SORT_NUMERIC = "数字升序"
SORT_ALPHA = "字母升序"
SORTS = [SORT_NUMERIC, SORT_ALPHA]

# 只认这些扩展名；.gif 只取第一帧（PIL 默认行为）
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}

IMAGE_KIND = "image"


def _natural_key(name):
    """
    自然排序 key：img2.png 排在 img10.png 前面。

    数字段包成 (0, int)，文本段包成 (1, str) —— 两种元组的第一个元素
    类型一致，比较才不会落到 int 和 str 上。否则同一位置出现
    (0, 1) 和 (0, "a") 时 Python 3 直接抛 TypeError。
    """
    parts = re.split(r"(\d+)", (name or "").lower())
    return [(0, int(p)) if p.isdigit() else (1, p) for p in parts]


def _sort_names(names, sort_mode):
    """数字升序 → 自然排序；字母升序 → 按文件名小写字母序。"""
    if sort_mode == SORT_ALPHA:
        return sorted(names, key=lambda n: (n or "").lower())
    return sorted(names, key=_natural_key)


def list_images(folder, sort_mode=SORT_NUMERIC):
    """
    扫描文件夹里的图片文件。

    @param {str} folder 文件夹路径（支持 _resolve_path 的各种脏输入）
    @param {str} sort_mode 排序方式
    @returns {(list[str], str|None)} 绝对路径列表（已排序）/ 错误信息
    """
    real = _resolve_path(folder)
    if not real:
        return [], "请先选择图片文件夹"
    if not os.path.isdir(real):
        return [], f"文件夹不存在：{real}"

    try:
        names = os.listdir(real)
    except Exception as e:
        return [], f"读取文件夹失败：{e}"

    files = []
    for name in names:
        if os.path.splitext(name)[1].lower() not in IMAGE_EXTS:
            continue
        # 目录名可能也带 .png 后缀，必须确认是文件
        if os.path.isfile(os.path.join(real, name)):
            files.append(name)

    if not files:
        return [], f"文件夹里没有图片：{real}"

    files = _sort_names(files, sort_mode)
    return [os.path.join(real, name) for name in files], None


def _parse_manual_list(raw):
    """
    解析前端拖拽区写进来的 JSON 数组。

    元素是相对 input 目录的路径字符串（形如 `dogking-tools/abc.png`），
    为了兼容旧数据也接受 `{name, subfolder}` 对象。
    坏数据静默丢弃 —— 一个坏元素不能把整批图拖死。

    @param {str} raw 前端写入的 JSON 文本
    @returns {list[str]} 相对路径列表
    """
    if not raw or not str(raw).strip():
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if not isinstance(data, list):
        return []

    out = []
    for item in data:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            sub = str(item.get("subfolder") or "").strip().strip("/\\")
            if not name:
                continue
            out.append(f"{sub}/{name}" if sub else name)
        elif isinstance(item, str):
            value = item.strip()
            if value:
                out.append(value)
    return out


def collect_images(folder, sort_mode, manual_raw):
    """
    合并两种来源：文件夹扫描结果在前，手动列表追加在后（按真实路径去重）。

    用户经常既选一个文件夹、又额外拖几张图进来，所以是合并而不是二选一。
    排序只作用于文件夹扫描出来的那部分，手动列表保持拖入顺序。

    @returns {(list[str], str|None)} 绝对路径列表 / 错误信息
    """
    paths, err = [], None
    if (folder or "").strip():
        paths, err = list_images(folder, sort_mode)

    seen = {os.path.normcase(os.path.normpath(p)) for p in paths}
    for rel in _parse_manual_list(manual_raw):
        real = _resolve_path(rel)
        if not real:
            continue
        key = os.path.normcase(os.path.normpath(real))
        if key in seen:
            continue
        seen.add(key)
        paths.append(real)

    if not paths:
        # 文件夹报错、又没有手动图片 → 把文件夹的错误抛给用户；
        # 文件夹有错但手动列表里有图 → 用手动列表，不打扰（图能跑就行）
        return [], (err or "请先选择图片文件夹，或把图片拖到节点上")
    return paths, None


def count_images(folder, sort_mode=SORT_NUMERIC):
    """给前端显示「共 N 张」用；附前 8 个文件名做预览。"""
    paths, err = list_images(folder, sort_mode)
    names = [os.path.basename(p) for p in paths]
    return {"total": len(names), "preview": names[:8], "error": err}


def _blank_image(size=64, value=0.5):
    """
    一张灰色占位图，形状和正常输出一致：[1, size, size, 3]。

    读图失败 / 没图可跑时用它兜底 —— 直接抛异常会把整个队列打断，
    用户要的是「这张跳过，状态里告诉我原因」。
    没有 torch（独立测试环境）时返回 None；真实 ComfyUI 里一定有 torch。
    """
    try:
        import torch
        return torch.full((1, size, size, 3), float(value), dtype=torch.float32)
    except Exception:
        return None


def _load_image_safe(path):
    """
    读一张图 → [1, H, W, 3] 的 float32 tensor（值域 0-1）。

    @param {str} path 图片路径（绝对路径，或相对 input 目录）
    @returns {(tensor|None, str|None)} tensor / 错误原因
    """
    real = _resolve_path(path)
    if not real:
        return _blank_image(), "图片路径是空的"
    if not os.path.isfile(real):
        return _blank_image(), f"图片不存在：{real}"

    try:
        from PIL import Image, ImageOps
        import numpy as np
        import torch
    except Exception as e:
        return _blank_image(), f"缺少依赖：{e}"

    try:
        with Image.open(real) as img:
            # 手机照片靠 EXIF 记录旋转方向，不转过来的话图是躺着的
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGB")
            arr = np.array(img).astype(np.float32) / 255.0
        return torch.from_numpy(arr).unsqueeze(0), None
    except Exception as e:
        return _blank_image(), f"读取失败：{e}"


def _load_image_tensor(path):
    """只取 tensor 的便捷版；失败时是一张 64×64 灰图。"""
    tensor, _ = _load_image_safe(path)
    return tensor


class DogkingImageLoader:
    """
    按顺序加载图片文件夹。

    每执行一次输出一张图；「自动连续」打开时，前端会在本次执行结束后
    自动排下一次队列，直到这个范围跑完。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "文件夹路径": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "图片文件夹的完整路径，也可以用下方「选择文件夹」按钮挑",
                }),
                "排序方式": (SORTS, {
                    "default": SORT_NUMERIC,
                    "tooltip": "数字升序：img2 排在 img10 前面（自然排序）；字母升序：按文件名 a-z",
                }),
                "模式": (IMG_MODES, {
                    "default": IMG_MODE_ALL,
                    "tooltip": "全部模式：从头跑到尾；范围模式：只跑指定的序号区间",
                }),
                "起始序号": ("INT", {
                    "default": 1, "min": 1, "max": 999999, "step": 1,
                    "tooltip": "仅「范围模式」生效",
                }),
                "结束序号": ("INT", {
                    "default": 0, "min": 0, "max": 999999, "step": 1,
                    "tooltip": "仅「范围模式」生效；填 0 表示到最后一张",
                }),
                "自动连续": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "打开后每跑完一张会自动排下一张，直到跑完整个范围",
                }),
                # 前端拖拽区写入的 JSON 数组。节点上这个 widget 被隐藏，
                # 但值照常提交给后端（widget 的 serialize 默认就是 true）。
                "手动列表": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "前端拖拽区自动写入，不需要手动改",
                }),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("IMAGE", "INT", "INT", "STRING")
    RETURN_NAMES = ("图片", "当前序号", "总数量", "状态")
    FUNCTION = "load"
    CATEGORY = "dogking-tools"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        # 返回 NaN 让 ComfyUI 每次都重新执行这个节点 —— 批量出图靠的就是这个
        return float("NaN")

    def load(self, 文件夹路径="", 排序方式=SORT_NUMERIC, 模式=IMG_MODE_ALL,
             起始序号=1, 结束序号=0, 自动连续=True, 手动列表="", unique_id=None):
        key = str(unique_id or "0")

        paths, err = collect_images(文件夹路径, 排序方式, 手动列表)
        total = len(paths)

        if err or total == 0:
            return self._result(_blank_image(), 0, 0, err or "没有可用的图片", False)

        with _state_lock:
            state = _load_state()
            rec = state.get(key)
            # 老记录可能是提示词节点写的（没有 kind），脏数据一律当没有记录
            if not isinstance(rec, dict) or rec.get("kind") != IMAGE_KIND:
                rec = {}
            # 换文件夹 / 换排序 / 换模式 → 计数清零重来
            if (rec.get("folder") != 文件夹路径
                    or rec.get("sort") != 排序方式
                    or rec.get("mode") != 模式):
                rec = {
                    "kind": IMAGE_KIND,
                    "folder": 文件夹路径,
                    "sort": 排序方式,
                    "mode": 模式,
                    "index": 0,
                }

            if 模式 == IMG_MODE_RANGE:
                a = max(1, int(起始序号))
                b = int(结束序号) if int(结束序号) > 0 else total
                lo, hi = (a, b) if a <= b else (b, a)   # 4-2 自动变成 2-4
                lo = min(max(1, lo), total)
                hi = min(max(1, hi), total)
                if lo > hi:
                    lo, hi = hi, lo
            else:
                lo, hi = 1, total

            idx = int(rec.get("index", 0))
            line = lo + idx
            if line > hi:
                # 这一轮跑完了：归零，下次执行重新从 lo 开始
                rec["index"] = 0
                line = lo
                more = False
            else:
                more = (line + 1) <= hi
                rec["index"] = idx + 1

            state[key] = rec
            _save_state(state)

        image, load_err = _load_image_safe(paths[line - 1])
        status = f"第 {line}/{total} 张（{模式} {lo}-{hi}）"
        if load_err:
            # 读图失败不打断队列，只在状态里带上原因
            status = f"{status} {load_err}"

        # 通知前端：还要不要继续排下一张（和提示词节点共用同一个事件）
        try:
            from server import PromptServer
            PromptServer.instance.send_sync("dogking-tools.next", {
                "node": key,
                "index": line,
                "total": total,
                "should_continue": bool(more and 自动连续),
            })
        except Exception:
            pass

        return self._result(image, line, total, status, more)

    def _result(self, image, index, total, status, more):
        return {
            "ui": {
                "dogking_status": [status],
            },
            "result": (image, index, total, status),
        }


NODE_CLASS_MAPPINGS = {
    "DogkingPromptLoader": DogkingPromptLoader,
    "DogkingImageLoader": DogkingImageLoader,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DogkingPromptLoader": "提示词批量加载（dogking-tools）",
    "DogkingImageLoader": "图片批量加载（dogking-tools）",
}
