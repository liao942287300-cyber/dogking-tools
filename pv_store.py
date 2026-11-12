"""
dogking-tools —— PromptVault 数据缓存与分页层

为什么需要这一层：PromptVault 的 `/api/items` 一次吐全量（实测 17726 条 / 36MB，
`limit`/`offset` 是被忽略的）。面板每翻一页都重拉一遍是不可接受的，所以分页、
搜索、随机抽样全部在 ComfyUI 后端的内存里做。

缓存分四层：

    _slim        精简列表，按 (kind, addr, 筛选条件) 缓存，TTL 120s
                 image 只留 5 个短字段（17726 条 ≈ 1.8MB）
    _search      搜索索引（id → 小写文本），**全进程只留最近一个分类的**，TTL 60s
                 实测：全量 dict ≈ 50MB / 只留 positive ≈ 22.7MB / 本索引 ≈ 23MB
    _full_by_id  按 id 的完整元数据，LRU 2000 条（≈2MB），**每条 TTL 600s**
                 不是「进程内长驻」：用户改过提示词后长驻缓存会一直吐旧内容且不报错
    _thumb       缩略图字节，LRU 256 条（≈9MB）
    （text / asset 的量级很小：68 条 / 78 条，直接按全量缓存）

为什么 `_search` 不并进 `_slim`：`_slim` 是按分类分别缓存的，
用户把三个一级分类都点一遍就会同时留三份长文本（合计 31MB+，TTL 内不释放）。
搜索索引全进程只留一份，换分类即丢旧的，内存不随浏览历史增长。

两条关键约定：

1. **norm_addr 用用户填的原始值**（空串记 "auto"），绝不能用探测到的端口。
   PromptVault 重启后可能换端口（8765 被占就跑到 8766），如果 key 里带端口，
   所有缓存会在用户毫无感知的情况下集体作废，表现就是「每重启一次就卡一下」。

2. **搜索词不进缓存 key**。q 变化只在这份精简列表上重算当前页，一次网络都不打。
   否则用户每敲一个字都要重拉 36MB。

所有公开函数在 PromptVault 没开时都返回 (None, err) / (None, None, err)，
**绝不抛异常** —— 面板要能把错误文案显示出来，而不是整页 500。
"""

import random
import threading
import time
from collections import OrderedDict

try:  # 作为包被 ComfyUI 导入
    from . import pv_client
except ImportError:  # 独立测试环境（按文件路径加载）
    import pv_client

# ---------------------------------------------------------------- 容量与 TTL

# 精简列表存活时间。PromptVault 是本地只读库，内容不会秒变；
# 而一次重拉在本地只要 0.4s + 解析，用户几乎无感。
SLIM_TTL = 120.0
# 搜索索引存活时间。索引里只放「小写后的可搜文本」，不放完整 dict。
SEARCH_TTL = 60.0
# 完整元数据 LRU 上限（每条约 1KB → 约 2MB）
FULL_MAX = 2000
# 完整元数据的存活时间。
# 为什么必须有 TTL 而不是「进程内长驻」：用户在 PromptVault 里改了提示词之后，
# 长驻缓存会让节点**一直吐旧内容**，而且不报错、界面上看不出来 —— 这类 bug 最难排查。
# 600 秒的取法：一轮批量执行通常远小于 10 分钟，这期间保持一致反而是好事
# （12 张图跑 12 次队列只需 1 次 HTTP），跨会话就重取。
FULL_TTL = 600.0
# 缩略图 LRU 上限（每张约 35KB → 约 9MB）
THUMB_MAX = 256
# 单页最大条数（防止前端传个 100000 把内存打满）
MAX_PAGE_SIZE = 500

KIND_IMAGE = "image"
KIND_ASSET = "asset"
KIND_TEXT = "text"
KINDS = (KIND_IMAGE, KIND_ASSET, KIND_TEXT)

# ---------------------------------------------------------------- 内部状态

# (kind, norm_addr, *filters) -> {"ts": float, "items": list}
_slim = {}
# (norm_addr, category_id) -> {"ts": float, "idx": {id: 小写文本}}，**全进程只留一份**
_search = {}
# id -> 完整元数据（LRU：访问时 move_to_end）
_full_by_id = OrderedDict()
# (kind, item_id, size) -> bytes（LRU）
_thumb = OrderedDict()

_lock = threading.RLock()


# ---------------------------------------------------------------- 小工具

def norm_addr(addr=""):
    """
    地址归一化：小写去空白，空串记 "auto"。

    沿用 pv_nodes.source_key 的做法 —— 用户留空表示「自动发现」，
    这时端口是探测出来的、随时可能变，把它写进 key 等于自废缓存。
    """
    return str(addr or "").strip().lower() or "auto"


def _norm_kind(kind):
    k = str(kind or "").strip().lower()
    if k in ("image", "images", "img"):
        return KIND_IMAGE
    if k in ("asset", "assets"):
        return KIND_ASSET
    if k in ("text", "loras", "edits", "system-prompts", "system_prompts"):
        return KIND_TEXT
    return ""


def _norm_page_size(page_size):
    try:
        size = int(page_size)
    except (TypeError, ValueError):
        return 40
    if size <= 0:
        return 40
    return min(size, MAX_PAGE_SIZE)


def _norm_ids(ids):
    """id 列表归一化：去空、去重、保序。"""
    out = []
    seen = set()
    for raw in (ids or []):
        item_id = str(raw or "").strip()
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        out.append(item_id)
    return out


def _lru_put(store, key, value, limit):
    """通用 LRU 写入：超出上限就丢最旧的。"""
    store[key] = value
    store.move_to_end(key)
    while len(store) > limit:
        store.popitem(last=False)


def _lru_get(store, key):
    """通用 LRU 读取：命中时把 key 挪到末尾（表示最近使用）。"""
    if key not in store:
        return None
    store.move_to_end(key)
    return store[key]


# ---------------------------------------------------------------- 原始列表

def _search_key(addr, category_id=""):
    return (norm_addr(addr), str(category_id or ""))


def _get_search_index(addr="", category_id="", refresh=False):
    """
    取某分类的**搜索索引**：{id: 小写后的可搜文本}。

    这里刻意不缓存完整 dict，只缓存「拼好的小写文本」。实测数据（17726 条）：

        全量 dict 驻留        ≈ 50MB      ← 原来 `_raw` 的做法
        只留 positive 字符串  ≈ 22.7MB    ← 把 positive 塞进 _slim 的做法
        只留拼好的小写文本    ≈ 23MB      ← 本函数（且全进程只有一份）

    为什么不把 positive 放进 `_slim`：`_slim` 是**按分类分别缓存**的，
    用户把三个一级分类都点一遍，就会同时留三份（合计 31MB+，而且 TTL 120s 内不会释放）。
    这里全进程只留最近一份索引，换分类即丢旧的，内存不随浏览历史增长。

    @returns {(dict|None, str|None)} id → 小写文本 / 错误文案
    """
    key = _search_key(addr, category_id)
    now = time.time()

    if not refresh:
        with _lock:
            hit = _search.get(key)
            if hit and now - hit["ts"] <= SEARCH_TTL:
                return hit["idx"], None

    items, err = pv_client.fetch_items(addr, category_id)
    if err:
        return None, err

    idx = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "")
        if not item_id:
            continue
        idx[item_id] = "\n".join([
            str(item.get("name") or ""),
            str(item.get("positive") or ""),
            str(item.get("negative") or ""),
            str(item.get("params") or ""),
        ]).lower()

    with _lock:
        # 只留这一份：别的分类的索引一律丢，内存不随浏览历史增长
        _search.clear()
        _search[key] = {"ts": now, "idx": idx}
    return idx, None


# ---------------------------------------------------------------- 精简列表

def _slim_image_item(item):
    """图片 → 面板需要的 5 个短字段。"""
    return {
        "id": str(item.get("id") or ""),
        "name": str(item.get("name") or ""),
        "categoryId": str(item.get("categoryId") or ""),
        "width": int(item.get("width") or 0),
        "height": int(item.get("height") or 0),
    }


def _slim_text_item(item):
    """词库 → {id,name,text,chars}。chars 给前端显示字数，省一次字符串长度计算。"""
    text = str(item.get("text") or "")
    return {
        "id": str(item.get("id") or ""),
        "name": str(item.get("name") or ""),
        "text": text,
        "chars": len(text),
    }


def _slim_asset_item(item):
    """资产图 → 面板需要的元数据（含 note，hover 卡片要显示备注）。"""
    return {
        "id": str(item.get("id") or ""),
        "name": str(item.get("name") or ""),
        "category": str(item.get("category") or ""),
        "project": str(item.get("project") or ""),
        "note": str(item.get("note") or ""),
        "width": int(item.get("width") or 0),
        "height": int(item.get("height") or 0),
        "size": int(item.get("size") or 0),
        "mime": str(item.get("mime") or ""),
        "createdAt": int(item.get("createdAt") or 0),
    }


def get_slim(kind, addr="", refresh=False, **filters):
    """
    取某一筛选条件下的精简列表（面板分页/搜索/随机都用它）。

    @param {str} kind image | asset | text
    @param {str} addr 用户填的服务地址（留空 = 自动发现）
    @param {bool} refresh True = 跳过缓存强制重拉（面板工具条的 ↻）
    @param filters
        image: category_id
        asset: category, project
        text:  text_type
    @returns {(list|None, str|None)} 精简列表 / 错误文案
    """
    k = _norm_kind(kind)
    if not k:
        return None, f"不支持的数据类型：{kind}"

    if refresh:
        # 「刷新」必须真的打到网络：顺手清掉 pv_client 的 1 秒微缓存，
        # 否则用户点了刷新，请求会被那层缓存挡回去，表现为「点了没反应」。
        pv_client.clear_data_cache()

    if k == KIND_IMAGE:
        return _get_slim_image(addr, filters.get("category_id", ""), refresh)
    if k == KIND_TEXT:
        return _get_slim_text(addr, filters.get("text_type", "loras"), refresh)
    return _get_slim_asset(addr, filters.get("category", ""), filters.get("project", ""), refresh)


def _get_slim_image(addr, category_id, refresh):
    key = (KIND_IMAGE, norm_addr(addr), str(category_id or ""))
    now = time.time()

    if not refresh:
        with _lock:
            hit = _slim.get(key)
            if hit and now - hit["ts"] <= SLIM_TTL:
                return hit["items"], None

    # 这里**不**经过任何长驻缓存：完整 dict 只在构建 slim 的这一瞬间存在，
    # 出作用域就释放（峰值 ~50MB 是瞬时的，稳态只有下面这份 1.8MB 的 slim）。
    items, err = pv_client.fetch_items(addr, category_id)
    if err:
        return None, err

    slim = [_slim_image_item(it) for it in items if isinstance(it, dict)]
    del items
    with _lock:
        _slim[key] = {"ts": now, "items": slim}
    return slim, None


def _get_slim_text(addr, text_type, refresh):
    key = (KIND_TEXT, norm_addr(addr), str(text_type or "loras"))
    now = time.time()

    if not refresh:
        with _lock:
            hit = _slim.get(key)
            if hit and now - hit["ts"] <= SLIM_TTL:
                return hit["items"], None

    items, err = pv_client.fetch_text(addr, str(text_type or "loras"))
    if err:
        return None, err

    slim = [_slim_text_item(it) for it in items if isinstance(it, dict)]
    with _lock:
        _slim[key] = {"ts": now, "items": slim}
    return slim, None


def _get_slim_asset(addr, category, project, refresh):
    """
    资产图全量只有几十条，所以**一次拉全量**缓存起来，按分类/项目在内存里过滤。

    为什么不像图片那样按分类分别缓存：78 条全量才 40KB，
    按 (category, project) 组合缓存会产生一堆重复副本，且每切一次下拉就重拉一次。
    """
    key = (KIND_ASSET, norm_addr(addr), "")
    now = time.time()
    cached = None

    if not refresh:
        with _lock:
            hit = _slim.get(key)
            if hit and now - hit["ts"] <= SLIM_TTL:
                cached = hit["items"]

    if cached is None:
        items, err = pv_client.fetch_assets(addr)
        if err:
            return None, err
        cached = [_slim_asset_item(it) for it in items if isinstance(it, dict)]
        with _lock:
            _slim[key] = {"ts": now, "items": cached}

    wanted_cat = str(category or "").strip()
    wanted_proj = str(project or "").strip()

    out = []
    for item in cached:
        if wanted_cat and wanted_cat not in ("全部", "all") and item["category"] != wanted_cat:
            continue
        if wanted_proj and wanted_proj not in ("全部", "all"):
            # 「未分项目」= project 为空字符串（和 PromptVault 的 __none__ 语义一致）
            if wanted_proj in (pv_client.NO_PROJECT, pv_client.NO_PROJECT_KEY):
                if item["project"]:
                    continue
            elif item["project"] != wanted_proj:
                continue
        out.append(item)

    return out, None


# ---------------------------------------------------------------- 搜索

def search_haystack(kind, addr="", **filters):
    """
    给图片列表准备「搜索用的全文」（id → 小写后的可搜文本）。

    为什么单独一个函数：图片的精简列表只有 5 个短字段（1.8MB），
    而搜索要匹配 positive / negative / params 这些长文本。
    把长文本塞进 `_slim` 不划算 —— `_slim` 是按分类分别缓存的，
    点过几个分类就留几份；实测 positive 全量 ≈ 31MB(UTF-8) / 22.7MB(Python 驻留)。

    所以：图片走**单独一份**搜索索引（全进程只留最近一个分类的，见 `_get_search_index`）；
    text / asset 的精简列表本身已经含全部可搜字段，直接返回 None 让 page() 自己拼。

    @returns {(dict|None, str|None)} id → 可搜文本 / 错误文案
    """
    k = _norm_kind(kind)
    if k != KIND_IMAGE:
        return None, None

    return _get_search_index(addr, filters.get("category_id", ""))


def _item_haystack(item, haystack=None):
    """
    拼一条记录的「可搜文本」。

    优先用外部给的 haystack（图片：含长文本）；没有就退化成
    「这条记录自己所有字符串字段」——对 text（name/text）和
    asset（name/category/project/note）来说这就是完整集合。
    """
    if haystack:
        hit = haystack.get(str(item.get("id") or ""))
        if hit is not None:
            return hit

    parts = []
    for value in item.values():
        if isinstance(value, str) and value:
            parts.append(value)
    return "\n".join(parts).lower()


def _filter_items(items, q="", haystack=None):
    """按关键字过滤（空格分词 = 同时包含，和 PromptVault 自己的语义一致）。"""
    words = [w for w in str(q or "").strip().lower().split() if w]
    if not words:
        return list(items)
    out = []
    for item in items:
        text = _item_haystack(item, haystack)
        if all(word in text for word in words):
            out.append(item)
    return out


def page(items, q="", page=1, page_size=40, haystack=None):
    """
    在内存里完成「搜索 + 分页」（**一次网络都不打**）。

    page 越界自动夹到 [1, pages]：用户在第 1 页点「上一页」、
    或者筛选后页码超界，都不该看到空面板。

    @returns {dict} {total, page, pageSize, pages, items}
    """
    source = [it for it in (items or []) if isinstance(it, dict)]
    picked = _filter_items(source, q, haystack)

    total = len(picked)
    size = _norm_page_size(page_size)
    pages = max(1, (total + size - 1) // size) if total else 1

    try:
        wanted = int(page)
    except (TypeError, ValueError):
        wanted = 1
    current = max(1, min(wanted, pages))

    start = (current - 1) * size
    return {
        "total": total,
        "page": current,
        "pageSize": size,
        "pages": pages,
        "items": picked[start:start + size],
    }


# ---------------------------------------------------------------- 按 id 反查

def _remember_full(items):
    """
    把完整元数据塞进 LRU（执行阶段会反复按 id 取同一批图）。

    存的是 `(时间戳, 记录)`，不是裸记录 —— 见 FULL_TTL 的说明。
    """
    now = time.time()
    with _lock:
        for item in items or []:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            if item_id:
                _lru_put(_full_by_id, item_id, (now, item), FULL_MAX)


def _from_full_cache(ids):
    """
    从 LRU 里捞已缓存的完整元数据 → (命中表, 还需要拉取的 id 列表)。

    过期的条目**当场删掉**再当作未命中：留着它只会在下一次访问时再判一次过期，
    而且会让 `stats()` 里的条目数虚高。
    """
    found = {}
    rest = []
    now = time.time()
    with _lock:
        for item_id in ids:
            entry = _full_by_id.get(item_id)
            if entry is None:
                rest.append(item_id)
                continue
            ts, item = entry
            if now - ts > FULL_TTL:
                # 过期：用户可能刚在 PromptVault 里改过提示词，
                # 继续吐旧内容还不报错，是最难排查的一类 bug
                del _full_by_id[item_id]
                rest.append(item_id)
                continue
            _full_by_id.move_to_end(item_id)
            found[item_id] = item
    return found, rest


def lookup_images(addr="", ids=None):
    """
    按 id 批量取图片的完整元数据（执行阶段用）。

    @returns {(list|None, list, str|None)} items（按传入顺序）/ missing / 错误文案
    """
    wanted = _norm_ids(ids)
    if not wanted:
        return [], [], None

    found, rest = _from_full_cache(wanted)

    if rest:
        items, missing, err = pv_client.fetch_items_by_ids(addr, rest)
        if err:
            return None, wanted, err
        _remember_full(items)
        for item in items or []:
            if isinstance(item, dict):
                found[str(item.get("id") or "")] = item
        missing_set = set(missing or [])
    else:
        missing_set = set()

    ordered = []
    missing_ids = []
    for item_id in wanted:
        item = found.get(item_id)
        if item is None:
            missing_ids.append(item_id)
        else:
            ordered.append(item)

    # 服务端没报 missing 但确实没拿到的，也要算失效
    for item_id in wanted:
        if item_id not in found and item_id not in missing_ids:
            missing_ids.append(item_id)

    return ordered, missing_ids, None


def lookup_text(addr="", text_type="loras", ids=None):
    """
    按 id 批量取词库条目。

    /api/text 没有 ids 参数（词库总共几十条），所以拉全量后在内存里挑。

    @returns {(list|None, list, str|None)}
    """
    wanted = _norm_ids(ids)
    if not wanted:
        return [], [], None

    items, err = pv_client.fetch_text(addr, str(text_type or "loras"))
    if err:
        return None, wanted, err

    by_id = {}
    for item in items or []:
        if isinstance(item, dict):
            by_id[str(item.get("id") or "")] = item

    ordered = []
    missing = []
    for item_id in wanted:
        item = by_id.get(item_id)
        if item is None:
            missing.append(item_id)
        else:
            ordered.append(item)
    return ordered, missing, None


def lookup_assets(addr="", ids=None):
    """
    按 id 批量取资产图元数据。

    @returns {(list|None, list, str|None)}
    """
    wanted = _norm_ids(ids)
    if not wanted:
        return [], [], None

    items, missing, err = pv_client.fetch_assets_by_ids(addr, wanted)
    if err:
        return None, wanted, err

    by_id = {}
    for item in items or []:
        if isinstance(item, dict):
            by_id[str(item.get("id") or "")] = item

    ordered = []
    missing_ids = []
    for item_id in wanted:
        item = by_id.get(item_id)
        if item is None:
            missing_ids.append(item_id)
        else:
            ordered.append(item)
    return ordered, missing_ids, None


# ---------------------------------------------------------------- 随机抽样

def sample_random(kind, addr="", n=1, exclude=None, q="", **filters):
    """
    从**当前筛选下的全部记录**里随机抽 N 个不重复的 id。

    为什么必须在后端做：前端手里只有当前页 40 张，在 40 张里抽，
    用户点两次就会发现「随机总是这几个」。

    @returns {(list|None, int, str|None)} ids / 候选总数（排除前）/ 错误文案
    """
    k = _norm_kind(kind)
    if not k:
        return None, 0, f"不支持的数据类型：{kind}"

    items, err = get_slim(k, addr, **filters)
    if err:
        return None, 0, err

    haystack = None
    if str(q or "").strip():
        haystack, hay_err = search_haystack(k, addr, **filters)
        if hay_err:
            # 图片搜索需要原始列表；拉不到就把错误抛出去，
            # 否则用户会看到「随机结果和搜索条件不符」这种更难受的错
            return None, 0, hay_err

    pool = _filter_items(items or [], q, haystack)
    total = len(pool)

    skip = set(_norm_ids(exclude))
    candidates = [it.get("id") for it in pool if str(it.get("id") or "") not in skip]

    try:
        want = int(n)
    except (TypeError, ValueError):
        want = 1
    want = max(1, want)

    if len(candidates) <= want:
        # 候选不够就全给（顺序打散，避免总是按库内顺序）
        picked = list(candidates)
        random.shuffle(picked)
        return picked, total, None

    return random.sample(candidates, want), total, None


# ---------------------------------------------------------------- 缩略图

def thumb(kind, addr="", item_id="", size="thumb"):
    """
    取缩略图/原图字节（LRU 256 条）。

    只缓存缩略图尺寸：原图动辄 1~3MB，缓存收益低、内存占用高，
    而面板一次只显示一页 40 张缩略图。

    @returns {(bytes|None, str, str|None)} 字节 / mime / 错误文案
    """
    k = _norm_kind(kind)
    item_id = str(item_id or "").strip()
    if not k or k == KIND_TEXT:
        return None, "", f"不支持的数据类型：{kind}"
    if not item_id:
        return None, "", "缺少 id"

    want_size = "full" if str(size or "").strip().lower() == "full" else "thumb"
    cacheable = want_size == "thumb"
    key = (k, item_id, want_size)

    if cacheable:
        with _lock:
            hit = _lru_get(_thumb, key)
        if hit is not None:
            return hit, pv_client.sniff_image_mime(hit), None

    if k == KIND_ASSET:
        data, err = pv_client.fetch_asset_image(addr, item_id, want_size)
    else:
        data, err = pv_client.fetch_image_bytes(addr, item_id, want_size)

    if err:
        return None, "", err

    if cacheable:
        with _lock:
            _lru_put(_thumb, key, data, THUMB_MAX)

    return data, pv_client.sniff_image_mime(data), None


# ---------------------------------------------------------------- 失效

def invalidate(addr=None):
    """
    清缓存。

    addr=None → 全清（PromptVault 换端口时用）；
    addr=某地址 → 只清这个地址下的（面板「刷新」按钮用）。
    """
    target = None if addr is None else norm_addr(addr)

    with _lock:
        if target is None:
            _slim.clear()
            _search.clear()
            _full_by_id.clear()
            _thumb.clear()
            return

        for store in (_slim, _search):
            for key in [k for k in store if k[1] == target]:
                store.pop(key, None)

        # _full_by_id / _thumb 的 key 里没有地址（id 本身就是全局唯一的），
        # 换地址时它们会自然过期或被 LRU 淘汰，这里一并清掉更安全
        _full_by_id.clear()
        _thumb.clear()


def stats():
    """
    当前缓存规模（测试 / 排查用；也给面板「调试」留个入口）。
    """
    with _lock:
        search_entries = sum(len(v.get("idx") or {}) for v in _search.values())
        return {
            "slim": len(_slim),
            "search_index": len(_search),
            "search_ids": search_entries,
            "full": len(_full_by_id),
            "thumb": len(_thumb),
        }
