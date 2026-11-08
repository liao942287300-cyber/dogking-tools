"""
dogking-tools —— PromptVault 数据源节点

PromptVault 桌面版会在 127.0.0.1:8765-8775 上开一个只读 HTTP 服务，
这里把它接成 4 个节点：

    DogkingPVPrompt  提示词（正向 / 负向 / 参数）
    DogkingPVImage   图片
    DogkingPVPair    图片 + 提示词（最常用）
    DogkingPVText    词库（LORA 触发词 / 编辑指令 / 系统提示词）

设计要点：
  · 端口自动发现（探 8765-8775），结果进程内缓存；也可手动填「服务地址」强制指定
  · 进度完全复用 nodes.py 的 state.json 机制，记录 kind="pv" 与老节点互不干扰
  · 任何网络异常都不抛出去 —— 批量队列靠的是「跳过这张、状态里说原因」，
    抛异常会让整个队列中断
  · 所有请求 3 秒超时，PromptVault 没开时快速失败，不卡队列

注意：浏览器网页调不到 PromptVault（对方只监听回环、且刻意不发 CORS 头），
所以前端下拉框的分类列表要经 ComfyUI 后端代转（server.py 的 /dogking-tools/pv_categories）。
"""

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

try:  # 作为包被 ComfyUI 导入
    from .nodes import _blank_image, _load_state, _save_state, _state_lock
except ImportError:  # 独立测试环境（按文件路径加载）
    from nodes import _blank_image, _load_state, _save_state, _state_lock

NODE_DIR = os.path.dirname(os.path.abspath(__file__))
# 上次成功拉到的分类列表，落盘缓存。
# 作用：PromptVault 没开时 INPUT_TYPES 仍能给出完整选项，
# 否则已保存的工作流会因为「值不在列表里」被 ComfyUI 拒绝入队。
CATEGORY_CACHE_FILE = os.path.join(NODE_DIR, "pv_categories_cache.json")

PV_KIND = "pv"

# ---------------------------------------------------------------- 模式 / 类型

PV_MODE_ALL = "全部模式"
PV_MODE_RANGE = "范围模式"
PV_MODE_INDEX = "编号模式"
PV_MODES = [PV_MODE_ALL, PV_MODE_RANGE, PV_MODE_INDEX]

ALL_CATEGORY = "全部"

TEXT_LORA = "LORA触发词"
TEXT_EDIT = "编辑指令"
TEXT_PROMPT = "系统提示词"
TEXT_TYPES = [TEXT_LORA, TEXT_EDIT, TEXT_PROMPT]
# 对外统一叫 text 的字段，内部字段名各不相同（见 js/bridge.js 的映射）
TEXT_TYPE_KEYS = {
    TEXT_LORA: "loras",
    TEXT_EDIT: "edits",
    TEXT_PROMPT: "system-prompts",
}

# ---------------------------------------------------------------- 网络参数

PORT_CANDIDATES = tuple(range(8765, 8776))
# 数据请求超时（秒）
TIMEOUT = 3.0
# 探测端口用更短的超时：连不上要立刻换下一个，11 个端口 × 3 秒会把队列卡死
PROBE_TIMEOUT = 0.6
# /api/items 这类数据 1 秒内复用，避免同一轮里多个节点重复拉一遍
DATA_TTL = 1.0
# 分类列表缓存时间（/object_info 会被频繁调用，别每次都打网络）
CATS_TTL = 5.0

NOT_RUNNING = "PromptVault 未运行（试过 8765-8775），请先打开 PromptVault"

# 进程内缓存
_cached_base = None
# 上次「一个端口都没探到」的时间：短时间内重复探测没意义，直接快速失败
_base_fail_ts = 0.0
BASE_FAIL_TTL = 2.0
_data_cache = {}
_cats_cache = {"addr": None, "ts": 0.0, "choices": [ALL_CATEGORY]}
_cache_lock = threading.Lock()
# 后台刷新分类的标记（同一时间只跑一个线程）
_cats_refreshing = False

# 直连 opener：**必须绕过系统代理**。
# 用户开着 Clash / V2Ray 时 HTTP_PROXY 指向代理端口，urllib 默认会把
# 127.0.0.1 的请求也丢给代理（代理不认识本机服务，直接回 502），
# 表现就是「PromptVault 明明开着，节点却连不上」。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _open(req, timeout):
    """统一的请求入口（测试里替换这个函数即可打桩）。"""
    return _OPENER.open(req, timeout=timeout)


def reset_caches():
    """清空所有进程内缓存（测试用；PromptVault 换端口时也靠它重探）。"""
    global _cached_base, _base_fail_ts
    with _cache_lock:
        _cached_base = None
        _base_fail_ts = 0.0
        _data_cache.clear()
        _cats_cache["addr"] = None
        _cats_cache["ts"] = 0.0
        _cats_cache["choices"] = [ALL_CATEGORY]


# ---------------------------------------------------------------- HTTP 基础

def _not_running(addr=""):
    """「没连上」的提示文案。手动指定地址时带上地址，方便用户排查。"""
    raw = str(addr or "").strip()
    if raw:
        return f"PromptVault 未运行（{raw}），请先打开 PromptVault 或检查服务地址"
    return NOT_RUNNING


def _build_url(base, path, params=None):
    url = f"{base}{path}"
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    return url


def _request_json(url, hint=NOT_RUNNING):
    """
    发一个 GET 请求并解析 JSON。

    @returns {(dict|None, str|None)} 数据 / 用户可读的错误文案
    """
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with _open(req, TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        return None, f"接口返回 HTTP {e.code}"
    except urllib.error.URLError:
        # 连接被拒绝 / 超时 —— 绝大多数情况就是 PromptVault 没开
        return None, hint
    except Exception as e:
        return None, f"请求失败：{e}"

    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception as e:
        return None, f"响应解析失败：{e}"

    if not isinstance(data, dict):
        return None, "响应格式不对（不是 JSON 对象）"
    if data.get("ok") is False:
        return None, str(data.get("error") or "接口返回失败")
    return data, None


def _probe(base):
    """探一下这个地址是不是 PromptVault。"""
    try:
        req = urllib.request.Request(f"{base}/api/health", headers={"Accept": "application/json"})
        with _open(req, PROBE_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return isinstance(data, dict) and data.get("ok") is True
    except Exception:
        return False


def _probe_port(port):
    """探单个端口；任何异常都算「不可用」，绝不让一个端口影响其他端口。"""
    try:
        return _probe(f"http://127.0.0.1:{port}")
    except Exception:
        return False


def _discover_base():
    """
    并发探测所有候选端口，返回第一个可用的基地址。

    为什么必须并发：本机连 127.0.0.1 的**关闭端口不是立刻 RST，而是一直挂到超时**。
    串行探 11 个端口最坏要 11 × PROBE_TIMEOUT ≈ 6.6 秒 —— 用户点 Queue 会以为卡死。
    并发后总耗时 ≈ 单个超时（0.6s 左右）。

    为什么按端口号顺序等（不是按完成顺序）：保证 8765 优先、行为确定，
    不会因为 8770 先返回就选中它；而且 8765 通的时候立刻返回，不用等其余端口超时。

    @returns {str|None} 形如 http://127.0.0.1:8765；一个都探不到返回 None
    """
    ports = list(PORT_CANDIDATES)
    if not ports:
        return None

    try:
        executor = ThreadPoolExecutor(max_workers=len(ports))
    except Exception:
        return None

    try:
        futures = {port: executor.submit(_probe_port, port) for port in ports}
        for port in ports:
            try:
                ok = bool(futures[port].result())
            except Exception:
                # 单个端口探失败（超时 / 异常）不能影响其他端口
                ok = False
            if ok:
                return f"http://127.0.0.1:{port}"
        return None
    finally:
        # 关键：**不等**剩下的探测。8765 通了就立刻返回，
        # 否则那 10 个关闭的端口会把 0.6s 的超时白白等满（实测会拖慢 600 倍）。
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:  # Python < 3.9 没有 cancel_futures
            executor.shutdown(wait=False)


def resolve_base(addr=""):
    """
    把「服务地址」输入解析成 http://host:port。

    留空 → 并发探 8765-8775，探到的结果进程内缓存；
    填了 → 直接用它（允许不带 http:// 前缀）。

    @returns {str|None} 可用的基地址；都探不到返回 None
    """
    global _cached_base, _base_fail_ts

    raw = str(addr or "").strip()
    if raw:
        if not (raw.lower().startswith("http://") or raw.lower().startswith("https://")):
            raw = "http://" + raw
        return raw.rstrip("/")

    if _cached_base:
        return _cached_base

    # 刚探过一轮都没探到：短时间内直接失败，别让 4 个节点同时创建时反复扫端口
    if _base_fail_ts and (time.time() - _base_fail_ts) < BASE_FAIL_TTL:
        return None

    base = _discover_base()
    if base:
        _cached_base = base
        _base_fail_ts = 0.0
        return base

    _base_fail_ts = time.time()
    return None


def _fetch_json(addr, path, params=None):
    """
    带「端口失效自动重探」的请求。

    自动发现的端口可能因为 PromptVault 重启而失效（比如 8765 被别的程序占了，
    重开之后跑到 8766），所以失败时清掉缓存再探一次。
    """
    base = resolve_base(addr)
    if not base:
        return None, _not_running(addr)

    hint = _not_running(addr)
    data, err = _request_json(_build_url(base, path, params), hint)

    if err and not str(addr or "").strip():
        reset_caches()
        base2 = resolve_base(addr)
        if base2 and base2 != base:
            data, err = _request_json(_build_url(base2, path, params), hint)

    return data, err


def _cached(key, producer):
    """1 秒内的重复请求直接复用（同一轮里多个节点拉同一份数据）。"""
    now = time.time()
    with _cache_lock:
        hit = _data_cache.get(key)
        if hit and now - hit[0] <= DATA_TTL:
            return hit[1]
    value = producer()
    with _cache_lock:
        _data_cache[key] = (now, value)
    return value


# ---------------------------------------------------------------- 接口封装

def fetch_categories(addr=""):
    """GET /api/categories → (list[dict]|None, err)"""
    def _load():
        data, err = _fetch_json(addr, "/api/categories")
        if err:
            return None, err
        cats = data.get("categories")
        if not isinstance(cats, list):
            return None, "分类数据格式不对"
        return cats, None

    return _cached(("categories", str(addr or "")), _load)


def fetch_items(addr="", category_id="", search=""):
    """GET /api/items?type=images → (list[dict]|None, err)"""
    params = {"type": "images"}
    if category_id:
        params["category"] = category_id
        # 选了分类就要带子分类：PromptVault 里一级分类的图都挂在二级分类下，
        # 不带这个参数只能拿到「直接挂在一级分类上」的极少数图。
        params["includeChildren"] = "1"
    if search:
        params["search"] = search

    def _load():
        data, err = _fetch_json(addr, "/api/items", params)
        if err:
            return None, err
        items = data.get("items")
        if not isinstance(items, list):
            return None, "图片列表格式不对"
        return items, None

    return _cached(("items", str(addr or ""), category_id, search), _load)


def fetch_text(addr="", text_type="loras"):
    """GET /api/text?type=... → (list[dict]|None, err)"""
    def _load():
        data, err = _fetch_json(addr, "/api/text", {"type": text_type})
        if err:
            return None, err
        items = data.get("items")
        if not isinstance(items, list):
            return None, "词库数据格式不对"
        return items, None

    return _cached(("text", str(addr or ""), text_type), _load)


def fetch_image_bytes(addr="", image_id=""):
    """
    GET /api/image?id=... → (bytes|None, err)

    二进制不缓存：一张图可能几 MB，缓存收益低、内存占用高。
    """
    if not image_id:
        return None, "图片 id 是空的"

    base = resolve_base(addr)
    if not base:
        return None, _not_running(addr)

    url = _build_url(base, "/api/image", {"id": image_id, "size": "full"})
    try:
        req = urllib.request.Request(url)
        with _open(req, TIMEOUT) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except urllib.error.URLError:
        return None, _not_running(addr)
    except Exception as e:
        return None, f"请求失败：{e}"

    if not data:
        return None, "图片数据是空的"
    return data, None


# ---------------------------------------------------------------- 分类路径

def _as_int(value, default=0):
    """任何脏值都收敛成 int（前端 / 手改 JSON 都可能塞字符串进来）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _sort_key(category):
    return (_as_int(category.get("order"), 0), str(category.get("name") or ""))


def build_category_paths(categories):
    """
    扁平分类 → 路径列表。

    PromptVault 返回的是 [{id, name, level, parentId, order}] 的扁平数组，
    这里拼成「风格」「风格/写实摄影」这种路径，并按
    「一级（order 升序）→ 其子级（order 升序）」排好。

    父分类已删掉的孤儿二级分类追加在末尾，不丢数据。

    @param {list} categories
    @returns {list[dict]} [{path, id, level}]，第一项固定是「全部」
    """
    if not isinstance(categories, list):
        categories = []

    tops = [c for c in categories if isinstance(c, dict) and _as_int(c.get("level")) == 1]
    tops.sort(key=_sort_key)
    top_ids = {str(c.get("id") or "") for c in tops}

    out = [{"path": ALL_CATEGORY, "id": "", "level": 0}]

    for top in tops:
        top_id = str(top.get("id") or "")
        top_name = str(top.get("name") or "")
        if not top_name:
            continue
        out.append({"path": top_name, "id": top_id, "level": 1})

        subs = [
            c for c in categories
            if isinstance(c, dict)
            and _as_int(c.get("level")) == 2
            and str(c.get("parentId") or "") == top_id
        ]
        subs.sort(key=_sort_key)
        for sub in subs:
            sub_name = str(sub.get("name") or "")
            if not sub_name:
                continue
            out.append({
                "path": f"{top_name}/{sub_name}",
                "id": str(sub.get("id") or ""),
                "level": 2,
            })

    orphans = [
        c for c in categories
        if isinstance(c, dict)
        and _as_int(c.get("level")) != 1
        and str(c.get("parentId") or "") not in top_ids
    ]
    orphans.sort(key=_sort_key)
    for orphan in orphans:
        name = str(orphan.get("name") or "")
        if name:
            out.append({"path": name, "id": str(orphan.get("id") or ""), "level": 2})

    return out


def category_id_from_path(path, categories):
    """
    路径反查 id：用户在 COMBO 里选的是「风格/写实摄影」这种字符串。

    @returns {str} 分类 id；「全部」/ 空 / 找不到都返回空串（= 不筛选）
    """
    wanted = str(path or "").strip()
    if not wanted or wanted == ALL_CATEGORY:
        return ""
    for entry in build_category_paths(categories):
        if entry["path"] == wanted:
            return entry["id"]
    return ""


def list_category_paths(addr=""):
    """
    给前端 / server.py 用的分类列表（同步请求，调用方负责放到线程里跑）。

    @returns {dict} {"ok": True, "categories": [...]} 或 {"ok": False, "error": ...}
    """
    cats, err = fetch_categories(addr)
    if err:
        return {"ok": False, "error": err, "categories": []}

    categories = build_category_paths(cats)
    choices = [c["path"] for c in categories]

    # 顺手落盘：PromptVault 下次没开时，INPUT_TYPES 还能给出完整选项，
    # 已保存的工作流不会因为「值不在列表里」被 ComfyUI 拒绝入队
    _save_category_cache(addr, choices)
    with _cache_lock:
        _cats_cache.update({"addr": str(addr or ""), "ts": time.time(), "choices": choices})

    return {"ok": True, "categories": categories}


# ---------------------------------------------------------------- 分类落盘缓存

def _load_category_cache(addr=""):
    """读上次成功拉到的分类（PromptVault 没开时兜底，保证旧工作流还能入队）。"""
    try:
        with open(CATEGORY_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    if str(data.get("addr") or "") != str(addr or ""):
        return []
    choices = data.get("choices")
    if not isinstance(choices, list):
        return []
    return [str(c) for c in choices if isinstance(c, str) and c]


def _save_category_cache(addr, choices):
    try:
        payload = {"addr": str(addr or ""), "choices": list(choices)}
        tmp = CATEGORY_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CATEGORY_CACHE_FILE)
    except Exception:
        pass  # 缓存写不进去不影响节点本身


def _refresh_categories_async(addr):
    """
    后台刷新分类列表。

    INPUT_TYPES 会被 /object_info 和入队校验频繁调用，**绝不能在里面等网络**：
    PromptVault 没开时一次端口探测最长要好几秒，会把 ComfyUI 的界面卡住。
    所以这里只负责踢一个后台线程，调用方立刻拿到「当前已知的列表」。
    """
    global _cats_refreshing
    with _cache_lock:
        if _cats_refreshing:
            return
        _cats_refreshing = True

    def _worker():
        global _cats_refreshing
        try:
            list_category_paths(addr)  # 成功时自己会更新 _cats_cache 并落盘
        except Exception:
            pass
        finally:
            with _cache_lock:
                _cats_refreshing = False

    threading.Thread(target=_worker, daemon=True).start()


def category_choices(addr=""):
    """
    INPUT_TYPES 里的「分类」下拉选项。

    注意这是给 ComfyUI 做取值校验用的（保存的工作流重新入队时，
    ComfyUI 会拿 INPUT_TYPES 的列表核对值是否存在），所以它必须包含
    用户可能选中的每一个路径 —— 拉不到就用上次落盘的列表兜底。

    本函数**不做网络请求**，只读缓存 + 踢后台刷新，保证永不阻塞。
    """
    now = time.time()
    key = str(addr or "")

    with _cache_lock:
        fresh = _cats_cache["addr"] == key and (now - _cats_cache["ts"]) < CATS_TTL
        if fresh:
            return list(_cats_cache["choices"])
        memory = list(_cats_cache["choices"]) if _cats_cache["addr"] == key else []

    _refresh_categories_async(addr)

    fallback = memory or _load_category_cache(key) or [ALL_CATEGORY]
    if ALL_CATEGORY not in fallback:
        fallback = [ALL_CATEGORY] + fallback
    return list(fallback)


# ---------------------------------------------------------------- 进度推进

def _advance(rec, mode, total, start=1, end=0, index=1):
    """
    按模式推进游标（和 DogkingImageLoader.load() 里那段逻辑完全一致，
    额外补上「编号模式」）。原地修改 rec["index"]。

    @returns {(line, lo, hi, more)} 本次序号 / 范围下限 / 范围上限 / 还有没有下一张
    """
    if mode == PV_MODE_INDEX:
        line = min(max(1, _as_int(index, 1)), max(1, total))
        rec["index"] = 0
        return line, line, line, False

    if mode == PV_MODE_RANGE:
        a = max(1, _as_int(start, 1))
        b = _as_int(end, 0)
        if b <= 0:
            b = total
        lo, hi = (a, b) if a <= b else (b, a)  # 4-2 自动变成 2-4
        lo = min(max(1, lo), total)
        hi = min(max(1, hi), total)
        if lo > hi:
            lo, hi = hi, lo
    else:
        lo, hi = 1, total

    idx = max(0, _as_int(rec.get("index", 0), 0))
    line = lo + idx
    if line > hi:
        # 这一轮跑完了：归零，下次执行重新从 lo 开始
        rec["index"] = 0
        return lo, lo, hi, False

    more = (line + 1) <= hi
    rec["index"] = idx + 1
    return line, lo, hi, more


def source_key(addr, category_id="", text_type=""):
    """
    数据源指纹：分类 / 类型 / 服务地址任何一个变了，计数就归零。

    服务地址用「用户填的原始值」而不是探测到的端口 —— 留空（自动发现）时
    记为 auto，这样 PromptVault 重启换了端口不会把进度清零。
    """
    raw_addr = str(addr or "").strip().lower() or "auto"
    return "|".join([raw_addr, str(category_id or ""), str(text_type or "")])


def advance_state(key, source, mode, start, end, index, total):
    """
    读 state.json → 推进游标 → 写回。整段持锁，避免并发执行时计数错乱。

    @returns {(line, lo, hi, more)}
    """
    with _state_lock:
        state = _load_state()
        rec = state.get(key)
        # 老记录可能是提示词节点（无 kind）或图片节点（kind=image）写的，
        # 脏数据一律当没有记录处理
        if not isinstance(rec, dict) or rec.get("kind") != PV_KIND:
            rec = {}
        if rec.get("source") != source:
            rec = {"kind": PV_KIND, "source": source, "index": 0}

        line, lo, hi, more = _advance(rec, mode, total, start, end, index)
        state[key] = rec
        _save_state(state)

    return line, lo, hi, more


def status_text(unit, line, total, mode, lo, hi):
    """状态串，格式和老节点保持一致：第 3/20 张（全部模式 1-20）"""
    span = str(line) if mode == PV_MODE_INDEX else f"{lo}-{hi}"
    return f"第 {line}/{total} {unit}（{mode} {span}）"


def notify_next(key, line, total, more, auto):
    """通知前端：还要不要自动排下一次队列（复用老事件）。"""
    try:
        from server import PromptServer
        PromptServer.instance.send_sync("dogking-tools.next", {
            "node": key,
            "index": line,
            "line": line,
            "total": total,
            "should_continue": bool(more and auto),
        })
    except Exception:
        pass


# ---------------------------------------------------------------- 图片解码

def tensor_from_bytes(data):
    """
    PNG/JPEG/WebP 字节 → [1, H, W, 3] 的 float32 tensor（值域 0-1）。

    失败时返回 64×64 灰图 + 原因：抛异常会把整个批量队列打断，
    用户要的是「这张跳过、状态里告诉我为什么」。
    """
    try:
        import io

        from PIL import Image, ImageOps
        import numpy as np
        import torch
    except Exception as e:
        return _blank_image(), f"缺少依赖：{e}"

    try:
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGB")
            arr = np.array(img).astype(np.float32) / 255.0
        return torch.from_numpy(arr).unsqueeze(0), None
    except Exception as e:
        return _blank_image(), f"读取失败：{e}"


# ---------------------------------------------------------------- 节点公共部分

def _base_inputs(with_type=False):
    """
    4 个节点共用的输入参数（顺序即界面顺序）。

    「分类」是动态 COMBO：INPUT_TYPES 里给的是当前 PromptVault 的分类路径，
    前端还会用 /dogking-tools/pv_categories 再刷新一次 options.values。
    """
    entries = [
        ("服务地址", ("STRING", {
            "default": "",
            "multiline": False,
            "tooltip": "留空 = 自动发现（依次探 8765-8775）；也可填 127.0.0.1:8766 强制指定",
        })),
        ("分类", (category_choices(), {
            "default": ALL_CATEGORY,
            "tooltip": "全部 = 不筛选；其余为「一级」或「一级/二级」路径",
        })),
    ]
    if with_type:
        entries.append(("类型", (TEXT_TYPES, {
            "default": TEXT_LORA,
            "tooltip": "LORA触发词 / 编辑指令 / 系统提示词",
        })))
    entries.extend([
        ("模式", (PV_MODES, {
            "default": PV_MODE_ALL,
            "tooltip": "全部模式：从头跑到尾；范围模式：只跑指定序号区间；编号模式：固定取某一个",
        })),
        ("起始序号", ("INT", {
            "default": 1, "min": 1, "max": 999999, "step": 1,
            "tooltip": "仅「范围模式」生效",
        })),
        ("结束序号", ("INT", {
            "default": 0, "min": 0, "max": 999999, "step": 1,
            "tooltip": "仅「范围模式」生效；填 0 表示到最后一条",
        })),
        ("编号", ("INT", {
            "default": 1, "min": 1, "max": 999999, "step": 1,
            "tooltip": "仅「编号模式」生效",
        })),
        ("自动连续", ("BOOLEAN", {
            "default": True,
            "tooltip": "打开后每跑完一条会自动排下一次队列，直到跑完整个范围",
        })),
    ])
    return dict(entries)


def _iterate(addr, category, mode, start, end, index, auto, unique_id, unit, text_type=""):
    """
    4 个节点共用的主流程：解析服务 → 取列表 → 推进游标 → 通知前端。

    @returns {dict} {item, line, total, more, status, err, empty}
             err 非空表示整次执行失败（没连上 / 接口报错 / 分类不存在）
    """
    key = str(unique_id or "0")
    out = {"item": None, "line": 0, "total": 0, "more": False,
           "status": "", "err": None, "empty": False}

    base = resolve_base(addr)
    if not base:
        out["err"] = _not_running(addr)
        return out

    category_id = ""
    if text_type:
        # 词库节点没有分类概念：「分类」是 4 个节点共用的输入项，这里直接忽略，
        # 免得选了个已删除的分类就报错，也省掉一次分类请求
        items, err = fetch_text(addr, text_type)
    else:
        cats, err = fetch_categories(addr)
        if err:
            out["err"] = err
            return out
        wanted = str(category or "").strip()
        category_id = category_id_from_path(wanted, cats)
        if wanted and wanted != ALL_CATEGORY and not category_id:
            out["err"] = f"分类不存在：{wanted}（请重新选一次分类）"
            return out
        items, err = fetch_items(addr, category_id)

    if err:
        out["err"] = err
        return out

    total = len(items)
    out["total"] = total
    if total == 0:
        out["empty"] = True
        out["status"] = "该分类下没有图片" if not text_type else "该类型下没有内容"
        return out

    source = source_key(addr, category_id, text_type)
    line, lo, hi, more = advance_state(key, source, mode, start, end, index, total)

    out["item"] = items[line - 1]
    out["line"] = line
    out["more"] = more
    out["status"] = status_text(unit, line, total, mode, lo, hi)

    notify_next(key, line, total, more, auto)
    return out


def _result(payload, status):
    """统一包装返回值：ui.dogking_status 给前端画底部状态条。"""
    return {"ui": {"dogking_status": [status]}, "result": payload}


# ---------------------------------------------------------------- 节点 1：提示词

class DogkingPVPrompt:
    """从 PromptVault 按顺序取提示词（正向 / 负向 / 参数）。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": _base_inputs(), "hidden": {"unique_id": "UNIQUE_ID"}}

    RETURN_TYPES = ("STRING", "STRING", "STRING", "INT", "INT", "STRING")
    RETURN_NAMES = ("正向", "负向", "参数", "当前序号", "总数", "状态")
    FUNCTION = "load"
    CATEGORY = "dogking-tools"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("NaN")

    def load(self, 服务地址="", 分类=ALL_CATEGORY, 模式=PV_MODE_ALL,
             起始序号=1, 结束序号=0, 编号=1, 自动连续=True, unique_id=None):
        run = _iterate(服务地址, 分类, 模式, 起始序号, 结束序号, 编号,
                       自动连续, unique_id, "条")
        if run["err"]:
            return _result(("", "", "", 0, 0, run["err"]), run["err"])
        if run["empty"]:
            return _result(("", "", "", 0, 0, run["status"]), run["status"])

        item = run["item"] or {}
        positive = str(item.get("positive") or "")
        negative = str(item.get("negative") or "")
        params = str(item.get("params") or "")
        return _result((positive, negative, params, run["line"], run["total"], run["status"]),
                       run["status"])


# ---------------------------------------------------------------- 节点 2：图片

class DogkingPVImage:
    """从 PromptVault 按顺序取图片。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": _base_inputs(), "hidden": {"unique_id": "UNIQUE_ID"}}

    RETURN_TYPES = ("IMAGE", "INT", "INT", "STRING")
    RETURN_NAMES = ("图片", "当前序号", "总数", "状态")
    FUNCTION = "load"
    CATEGORY = "dogking-tools"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("NaN")

    def load(self, 服务地址="", 分类=ALL_CATEGORY, 模式=PV_MODE_ALL,
             起始序号=1, 结束序号=0, 编号=1, 自动连续=True, unique_id=None):
        run = _iterate(服务地址, 分类, 模式, 起始序号, 结束序号, 编号,
                       自动连续, unique_id, "张")
        if run["err"]:
            return _result((_blank_image(), 0, 0, run["err"]), run["err"])
        if run["empty"]:
            return _result((_blank_image(), 0, 0, run["status"]), run["status"])

        item = run["item"] or {}
        image, image_err = self._load_image(item, 服务地址)
        status = run["status"]
        if image_err:
            status = f"{status} 图片下载失败：{image_err}"
        return _result((image, run["line"], run["total"], status), status)

    @staticmethod
    def _load_image(item, addr):
        """下载 + 解码；任何一步失败都返回灰图 + 原因。"""
        data, err = fetch_image_bytes(addr, str(item.get("id") or ""))
        if err:
            return _blank_image(), err
        return tensor_from_bytes(data)


# ---------------------------------------------------------------- 节点 3：图片 + 提示词

class DogkingPVPair:
    """PromptVault 最常用的节点：一张图 + 它的正向 / 负向 / 参数。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": _base_inputs(), "hidden": {"unique_id": "UNIQUE_ID"}}

    RETURN_TYPES = ("IMAGE", "STRING", "STRING", "STRING", "INT", "INT", "STRING")
    RETURN_NAMES = ("图片", "正向", "负向", "参数", "当前序号", "总数", "状态")
    FUNCTION = "load"
    CATEGORY = "dogking-tools"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("NaN")

    def load(self, 服务地址="", 分类=ALL_CATEGORY, 模式=PV_MODE_ALL,
             起始序号=1, 结束序号=0, 编号=1, 自动连续=True, unique_id=None):
        run = _iterate(服务地址, 分类, 模式, 起始序号, 结束序号, 编号,
                       自动连续, unique_id, "张")
        if run["err"]:
            return _result((_blank_image(), "", "", "", 0, 0, run["err"]), run["err"])
        if run["empty"]:
            return _result((_blank_image(), "", "", "", 0, 0, run["status"]), run["status"])

        item = run["item"] or {}
        image, image_err = DogkingPVImage._load_image(item, 服务地址)
        status = run["status"]
        if image_err:
            status = f"{status} 图片下载失败：{image_err}"

        payload = (
            image,
            str(item.get("positive") or ""),
            str(item.get("negative") or ""),
            str(item.get("params") or ""),
            run["line"],
            run["total"],
            status,
        )
        return _result(payload, status)


# ---------------------------------------------------------------- 节点 4：词库

class DogkingPVText:
    """从 PromptVault 取词库：LORA 触发词 / 编辑指令 / 系统提示词。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": _base_inputs(with_type=True), "hidden": {"unique_id": "UNIQUE_ID"}}

    RETURN_TYPES = ("STRING", "STRING", "INT", "INT", "STRING")
    RETURN_NAMES = ("内容", "名称", "当前序号", "总数", "状态")
    FUNCTION = "load"
    CATEGORY = "dogking-tools"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("NaN")

    def load(self, 服务地址="", 分类=ALL_CATEGORY, 类型=TEXT_LORA, 模式=PV_MODE_ALL,
             起始序号=1, 结束序号=0, 编号=1, 自动连续=True, unique_id=None):
        text_type = TEXT_TYPE_KEYS.get(str(类型 or "").strip(), "loras")
        run = _iterate(服务地址, 分类, 模式, 起始序号, 结束序号, 编号,
                       自动连续, unique_id, "条", text_type=text_type)
        if run["err"]:
            return _result(("", "", 0, 0, run["err"]), run["err"])
        if run["empty"]:
            return _result(("", "", 0, 0, run["status"]), run["status"])

        item = run["item"] or {}
        payload = (
            str(item.get("text") or ""),
            str(item.get("name") or ""),
            run["line"],
            run["total"],
            run["status"],
        )
        return _result(payload, run["status"])


# ---------------------------------------------------------------- 注册映射

NODE_CLASS_MAPPINGS = {
    "DogkingPVPrompt": DogkingPVPrompt,
    "DogkingPVImage": DogkingPVImage,
    "DogkingPVPair": DogkingPVPair,
    "DogkingPVText": DogkingPVText,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DogkingPVPrompt": "PromptVault 提示词（dogking-tools）",
    "DogkingPVImage": "PromptVault 图片（dogking-tools）",
    "DogkingPVPair": "PromptVault 图片+提示词（dogking-tools）",
    "DogkingPVText": "PromptVault 词库（dogking-tools）",
}
