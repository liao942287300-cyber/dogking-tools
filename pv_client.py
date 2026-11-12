"""
dogking-tools —— PromptVault HTTP 客户端（纯网络层）

这个模块只做一件事：把 PromptVault 桌面版在 127.0.0.1:8765-8775 上的只读 HTTP
服务封装成若干 Python 函数。**不持有业务缓存、不做分页、不做搜索** —— 那些在
`pv_store.py` 里。分层的原因：网络层要能在测试里整体打桩（替换 `_open`），
而缓存层需要独立验证 TTL / LRU，混在一起两边都测不干净。

设计要点（都是从 pv_nodes.py 原样搬过来的既有结论，别改）：
  · 端口自动发现（探 8765-8775），结果进程内缓存；也可手动填「服务地址」强制指定
  · 任何网络异常都不抛出去，统一返回 (None, 用户可读的错误文案)
  · 所有请求 3 秒超时，PromptVault 没开时快速失败
  · 必须绕过系统代理（用户开着 Clash / V2Ray 时 urllib 会把 127.0.0.1 也丢给代理）

注意：浏览器网页调不到 PromptVault（对方只监听回环、且刻意不发 CORS 头），
所以前端下拉框要经 ComfyUI 后端代转（server.py 的 /dogking-tools/pv_categories）。
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
    from .nodes import _blank_image
except ImportError:  # 独立测试环境（按文件路径加载）
    from nodes import _blank_image

NODE_DIR = os.path.dirname(os.path.abspath(__file__))

# 上次成功拉到的分类列表，落盘缓存。
# 作用：PromptVault 没开时 INPUT_TYPES 仍能给出完整选项，
# 否则已保存的工作流会因为「值不在列表里」被 ComfyUI 拒绝入队。
CATEGORY_CACHE_FILE = os.path.join(NODE_DIR, "pv_categories_cache.json")
# 资产图的分类 / 项目列表，同样的落盘兜底（结构不同，所以单独一个文件）
ASSETS_CACHE_FILE = os.path.join(NODE_DIR, "pv_assets_cache.json")

# ---------------------------------------------------------------- 常量

# 「不筛选」在界面上的显示值。图片分类和资产分类共用同一个字面量。
ALL_CATEGORY = "全部"

# 资产图「项目」维度里代表「空 project」的显示值。
# PromptVault 的 /api/assets 用特殊值 __none__ 表示「未分项目」，两者要能互转。
NO_PROJECT = "未分项目"
NO_PROJECT_KEY = "__none__"

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


def clear_data_cache():
    """
    清掉那层 1 秒微缓存。

    面板上的「刷新」必须真的打到网络：如果只清 pv_store 的 TTL 缓存，
    1 秒内的 refresh 会被这里的微缓存挡回去，用户看到的是「点了刷新没反应」。
    """
    with _cache_lock:
        _data_cache.clear()


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


def fetch_items(addr="", category_id="", search="", ids=None, include_children=True):
    """
    GET /api/items?type=images → (list[dict]|None, err)

    ids 非空时走「按 id 批量取」分支（PromptVault 会忽略其余筛选参数）；
    这条路径下拿不到 missing，需要 missing 请用 fetch_items_by_ids。

    ⚠️ include_children 为什么默认 True（实测数据，2026-11 在 17726 张图的库上量的）：

        一级「风格」 +includeChildren → 12042      不带 → 0
        一级「主体」 +includeChildren →  4003      不带 → 0
        一级「场景」 +includeChildren →  1386      不带 → 0
        二级「写实摄影」带 / 不带 → 9328 / 9328（20 个二级分类逐个量过，全部相等）

    原因：这个库里 **每一张图都挂在二级分类下**，一级分类自己不直接挂图，
    所以一级分类不带 includeChildren 会得到 0 条（不是「筛不到」，是筛选语义本身如此）。
    二级分类带不带都一样，因此对任何分类都带上是最安全的做法 —— 永远不可能踩到 0 条。
    传 False 可以显式关掉（只在一级分类上才有区别，且结果必然是 0 条）。

    补充：item 里的 categoryId 恒为**一级分类 id**（全库只有 4 个取值：3 个一级 + null），
    二级分类 id 不出现在 item 里（桥服务不暴露 subCategoryId）。
    所以**二级筛选只能靠这个服务端参数**，插件侧无法在内存里筛二级。
    """
    params = {"type": "images"}
    if ids:
        params["ids"] = ",".join(str(i) for i in ids)
    else:
        if category_id:
            params["category"] = category_id
            # 选了分类就要带子分类：PromptVault 里图片都挂在二级分类下，
            # 不带这个参数只能拿到「直接挂在一级分类上」的图（本库为 0 张）。
            if include_children:
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

    return _cached(("items", str(addr or ""), category_id, search, params.get("ids")), _load)


def fetch_items_by_ids(addr="", ids=None):
    """
    GET /api/items?ids=a,b,c → (items, missing, err)

    专门为「按 id 取完整元数据」准备的：PromptVault 会按传入顺序返回，
    并额外给出 missing（已删除的 id）。不做 1 秒缓存 —— 执行阶段每次都要最新。
    """
    wanted = [str(i).strip() for i in (ids or []) if str(i).strip()]
    if not wanted:
        return [], [], None

    data, err = _fetch_json(addr, "/api/items", {"type": "images", "ids": ",".join(wanted)})
    if err:
        return None, wanted, err

    items = data.get("items")
    if not isinstance(items, list):
        return None, wanted, "图片列表格式不对"
    missing = data.get("missing")
    if not isinstance(missing, list):
        # 老版本 PromptVault 没有 missing 字段，自己算一遍
        got = {str(it.get("id") or "") for it in items if isinstance(it, dict)}
        missing = [i for i in wanted if i not in got]
    return items, [str(m) for m in missing], None


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


def fetch_image_bytes(addr="", image_id="", size="full"):
    """
    GET /api/image?id=... → (bytes|None, err)

    size 默认 full（老节点的行为）；面板缩略图传 thumb。
    二进制不缓存：一张图可能几 MB，缓存收益低、内存占用高（缩略图 LRU 在 pv_store）。
    """
    if not image_id:
        return None, "图片 id 是空的"

    base = resolve_base(addr)
    if not base:
        return None, _not_running(addr)

    url = _build_url(base, "/api/image", {"id": image_id, "size": size or "full"})
    return _read_binary(url, addr)


def _read_binary(url, addr=""):
    """GET 一个二进制地址 → (bytes|None, err)。"""
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


# ---------------------------------------------------------------- 资产图（新增）

def fetch_assets(addr="", category="", project="", q="", limit=0, offset=0, ids=None):
    """
    GET /api/assets → (list[dict]|None, err)

    资产图只有几十条（实测 78），所以默认 limit=0 一次拉全量，
    分页/搜索交给 pv_store 在内存里做 —— 省一次网络往返，也让翻页变成纯内存操作。

    ids 非空时走「按 id 批量取」分支（会忽略 category/project/q/limit/offset）。

    @param {str} project 传 NO_PROJECT（未分项目）时自动换成 PromptVault 认的 __none__
    """
    params = {}
    if ids:
        params["ids"] = ",".join(str(i) for i in ids)
    else:
        if category and category != ALL_CATEGORY:
            params["category"] = category
        if project:
            params["project"] = NO_PROJECT_KEY if project == NO_PROJECT else project
        if q:
            params["q"] = q
        if limit:
            params["limit"] = int(limit)
        if offset:
            params["offset"] = int(offset)

    data, err = _fetch_json(addr, "/api/assets", params or None)
    if err:
        return None, err
    items = data.get("items")
    if not isinstance(items, list):
        return None, "资产图列表格式不对"
    return items, None


def fetch_assets_by_ids(addr="", ids=None):
    """GET /api/assets?ids= → (items, missing, err)"""
    wanted = [str(i).strip() for i in (ids or []) if str(i).strip()]
    if not wanted:
        return [], [], None

    data, err = _fetch_json(addr, "/api/assets", {"ids": ",".join(wanted)})
    if err:
        return None, wanted, err

    items = data.get("items")
    if not isinstance(items, list):
        return None, wanted, "资产图列表格式不对"
    missing = data.get("missing")
    if not isinstance(missing, list):
        got = {str(it.get("id") or "") for it in items if isinstance(it, dict)}
        missing = [i for i in wanted if i not in got]
    return items, [str(m) for m in missing], None


def fetch_asset_image(addr="", asset_id="", size="thumb"):
    """
    GET /api/asset-image?id=...&size=thumb|full → (bytes|None, err)

    和 fetch_image_bytes 的区别只有默认值：资产图原图常有 1~3MB，
    面板预览几乎总是要缩略图（约 35KB），所以默认 thumb。
    """
    if not asset_id:
        return None, "资产图 id 是空的"

    base = resolve_base(addr)
    if not base:
        return None, _not_running(addr)

    url = _build_url(base, "/api/asset-image", {"id": asset_id, "size": size or "thumb"})
    return _read_binary(url, addr)


def asset_facet_choices(addr=""):
    """
    资产图的「分类」与「项目」两个维度的可选值（给节点上的两个下拉用）。

    PromptVault 的桥接口**没有**单独的「资产分类列表」接口，所以只能从资产列表里
    聚合去重 —— 这也是为什么资产分类没有 id（它就是记录上的一个字符串字段）。

    @returns {dict} {"ok": True, "categories": [{path,id,level,parentPath}],
                    "projects": ["角色", ..., "未分项目"]}
                   连不上时 {"ok": False, "error": ..., "categories": [], "projects": []}
    """
    items, err = fetch_assets(addr)

    if err:
        cached = _load_assets_cache(addr)
        if cached:
            # 落盘兜底：PromptVault 没开时下拉框也不至于空掉（旧工作流还能入队）
            return {
                "ok": True,
                "stale": True,
                "categories": cached.get("categories") or [],
                "projects": cached.get("projects") or [],
            }
        return {"ok": False, "error": err, "categories": [], "projects": []}

    cat_names = []
    proj_names = []
    seen_cat = set()
    seen_proj = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("category") or "").strip()
        if name and name not in seen_cat:
            seen_cat.add(name)
            cat_names.append(name)
        proj = str(item.get("project") or "").strip()
        if proj and proj not in seen_proj:
            seen_proj.add(proj)
            proj_names.append(proj)

    cat_names.sort()
    proj_names.sort()

    categories = [
        {"path": name, "id": "", "level": 1, "parentPath": ""}
        for name in cat_names
    ]
    # 「未分项目」固定排最后：它是兜底桶，不是正常项目
    projects = proj_names + [NO_PROJECT]

    _save_assets_cache(addr, categories, projects)
    return {"ok": True, "categories": categories, "projects": projects}


def _load_assets_cache(addr=""):
    """读上次成功拉到的资产分类/项目（结构 {addr, categories, projects}）。"""
    try:
        with open(ASSETS_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    if str(data.get("addr") or "") != str(addr or ""):
        return {}
    categories = data.get("categories")
    projects = data.get("projects")
    return {
        "categories": categories if isinstance(categories, list) else [],
        "projects": projects if isinstance(projects, list) else [],
    }


def _save_assets_cache(addr, categories, projects):
    """落盘兜底（写不进去不影响节点本身，和分类缓存同样的处理）。"""
    try:
        payload = {
            "addr": str(addr or ""),
            "categories": list(categories or []),
            "projects": list(projects or []),
        }
        tmp = ASSETS_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, ASSETS_CACHE_FILE)
    except Exception:
        pass


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

    parentPath 是新增字段：前端做「一级分类变更 → 过滤二级分类」联动时，
    需要知道某个二级分类属于哪个一级，靠字符串前缀猜容易在名字含 / 时出错。

    @param {list} categories
    @returns {list[dict]} [{path, id, level, parentPath}]，第一项固定是「全部」
    """
    if not isinstance(categories, list):
        categories = []

    tops = [c for c in categories if isinstance(c, dict) and _as_int(c.get("level")) == 1]
    tops.sort(key=_sort_key)
    top_ids = {str(c.get("id") or "") for c in tops}

    out = [{"path": ALL_CATEGORY, "id": "", "level": 0, "parentPath": ""}]

    for top in tops:
        top_id = str(top.get("id") or "")
        top_name = str(top.get("name") or "")
        if not top_name:
            continue
        out.append({"path": top_name, "id": top_id, "level": 1, "parentPath": ""})

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
                "parentPath": top_name,
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
            # 孤儿二级分类的父级已经没了，parentPath 只能留空
            out.append({"path": name, "id": str(orphan.get("id") or ""), "level": 2,
                        "parentPath": ""})

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


def resolve_category_id(addr="", top="", sub=""):
    """
    分类路径（或「一级 + 二级」两段）→ 分类 id。

    节点上是一级/二级两个下拉，而执行时要的是 id，所以这里两种调用方式都支持：
        resolve_category_id(addr, "风格/写实摄影")   # 完整路径
        resolve_category_id(addr, "风格", "写实摄影")  # 两段

    @returns {str} 分类 id；「全部」/ 空 / 拉不到分类都返回空串（= 不筛选）
    """
    first = str(top or "").strip()
    second = str(sub or "").strip()

    if second and second != ALL_CATEGORY:
        path = f"{first}/{second}" if first and first != ALL_CATEGORY else second
    else:
        path = first

    if not path or path == ALL_CATEGORY:
        return ""

    cats, err = fetch_categories(addr)
    if err:
        return ""
    return category_id_from_path(path, cats)


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


def sniff_image_mime(data):
    """
    从二进制头嗅探图片类型（只用于 pv_thumb 的 Content-Type 透传）。

    PromptVault 的 /api/asset-image 已经给了 Content-Type，但 pv_store 的缩略图
    LRU 只存字节不存 header；重新嗅一次比多存一份 header 简单，也避免缓存不一致。

    @returns {str} 形如 image/png；认不出返回 application/octet-stream
    """
    if not data or len(data) < 12:
        return "application/octet-stream"
    if data[0:4] == b"\x89PNG":
        return "image/png"
    if data[0:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[0:4] == b"GIF8":
        return "image/gif"
    if data[0:2] == b"BM":
        return "image/bmp"
    return "application/octet-stream"
