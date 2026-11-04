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


NODE_CLASS_MAPPINGS = {
    "DogkingPromptLoader": DogkingPromptLoader,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DogkingPromptLoader": "提示词批量加载（dogking-tools）",
}
