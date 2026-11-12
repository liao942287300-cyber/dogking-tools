# -*- coding: utf-8 -*-
"""
pv_store.py 测试（缓存 / 分页 / 搜索 / 随机 / 缩略图）

重点守两类东西：
1. **缓存的 TTL 与淘汰** —— `_full_by_id` 过期必须真的失效（用户改了提示词却一直吐旧内容，
   是最难排查的一类 bug）；`_search` 必须「只留一份」（否则内存随浏览历史线性增长）。
2. **分页/随机的边界** —— page 越界要夹取、pageSize 要限幅、随机不能重复也不能越界。

HTTP 全部打桩，不依赖真实 PromptVault。可被 pytest 收集，也可直接 `python tests/test_pv_store.py`。
"""

import importlib.util
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 注意：**不往 sys.path 里塞插件目录**。
# 塞了之后 `__init__.py` 会变成一个可导入的顶层模块 `__init__`，
# pytest 在 prepend 模式下会把它当测试模块去 import，直接报
# 「attempted relative import with no known parent package」。
# 我们靠 sys.modules 注册来满足兄弟模块的相互引用，不需要 sys.path。


def _load(name, alias):
    spec = importlib.util.spec_from_file_location(alias, os.path.join(HERE, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_nodes = _load("nodes", "dk_nodes_for_pv_store")
sys.modules["nodes"] = _nodes
_TMPDIR = tempfile.mkdtemp(prefix="dk_pv_store_")
_nodes.STATE_FILE = os.path.join(_TMPDIR, "state.json")

pv_client = _load("pv_client", "dk_pv_client_for_pv_store")
sys.modules["pv_client"] = pv_client
pv_client.CATEGORY_CACHE_FILE = os.path.join(_TMPDIR, "pv_categories_cache.json")
pv_client.ASSETS_CACHE_FILE = os.path.join(_TMPDIR, "pv_assets_cache.json")

store = _load("pv_store", "dk_pv_store")

# 绑定自检：`sys.modules["pv_client"]` 是多个测试文件共用的键，必须确认
# pv_store 拿到的是**本文件**加载的那份（详见 test_pv_nodes.py 里的完整说明）。


# ---------------------------------------------------------------- 假数据

IMAGES = {
    "": [  # 全部分类
        {"id": "i1", "name": "海边少女", "categoryId": "c1", "width": 1024, "height": 1536,
         "positive": "写实摄影，少女，海边", "negative": "模糊", "params": "Steps: 20", "tags": []},
        {"id": "i2", "name": "赛博城市", "categoryId": "c2", "width": 1024, "height": 1024,
         "positive": "赛博朋克，夜景，霓虹", "negative": "", "params": "", "tags": []},
        {"id": "i3", "name": "水墨山水", "categoryId": "c1", "width": 768, "height": 1024,
         "positive": "水墨，山水，留白", "negative": "", "params": "Steps: 30", "tags": []},
    ],
    "c1": [
        {"id": "i1", "name": "海边少女", "categoryId": "c1", "width": 1024, "height": 1536,
         "positive": "写实摄影，少女，海边", "negative": "模糊", "params": "Steps: 20", "tags": []},
        {"id": "i3", "name": "水墨山水", "categoryId": "c1", "width": 768, "height": 1024,
         "positive": "水墨，山水，留白", "negative": "", "params": "Steps: 30", "tags": []},
    ],
}

TEXTS = [
    {"id": "t1", "name": "姬紫月", "text": "触发词内容"},
    {"id": "t2", "name": "空词条", "text": ""},
]

ASSET_ROWS = [
    {"id": "a1", "name": "角色正面", "category": "角色四视图", "project": "凡人",
     "note": "", "width": 512, "height": 768, "size": 86016, "mime": "image/png", "createdAt": 1},
    {"id": "a2", "name": "角色侧面", "category": "角色四视图", "project": "凡人",
     "note": "", "width": 512, "height": 768, "size": 81234, "mime": "image/png", "createdAt": 2},
    {"id": "a3", "name": "场景远景", "category": "场景资产图", "project": "",
     "note": "", "width": 1024, "height": 576, "size": 90000, "mime": "image/png", "createdAt": 3},
]

CALLS = {"items": 0, "text": 0, "assets": 0}


def _fetch_items(addr="", category_id="", search="", ids=None):
    CALLS["items"] += 1
    if ids:
        rows = [r for r in IMAGES[""] if r["id"] in ids]
        return rows, None
    return list(IMAGES.get(category_id, IMAGES[""])), None


def _fetch_text(addr="", text_type="loras"):
    CALLS["text"] += 1
    return list(TEXTS), None


def _fetch_assets(addr="", category="", project="", q="", limit=0, offset=0, ids=None):
    CALLS["assets"] += 1
    return list(ASSET_ROWS), None


def _fetch_items_by_ids(addr="", ids=None):
    wanted = [str(i) for i in (ids or [])]
    rows = [r for r in IMAGES[""] if r["id"] in wanted]
    missing = [i for i in wanted if i not in {r["id"] for r in rows}]
    return rows, missing, None


def _fetch_assets_by_ids(addr="", ids=None):
    wanted = [str(i) for i in (ids or [])]
    rows = [r for r in ASSET_ROWS if r["id"] in wanted]
    missing = [i for i in wanted if i not in {r["id"] for r in rows}]
    return rows, missing, None


def _install_stubs():
    pv_client.fetch_items = _fetch_items
    pv_client.fetch_text = _fetch_text
    pv_client.fetch_assets = _fetch_assets
    pv_client.fetch_items_by_ids = _fetch_items_by_ids
    pv_client.fetch_assets_by_ids = _fetch_assets_by_ids
    pv_client.fetch_image_bytes = lambda addr="", image_id="", size="full": (b"jpeg-bytes", None)
    pv_client.fetch_asset_image = lambda addr="", asset_id="", size="thumb": (b"png-bytes", None)


_install_stubs()


def reset_store():
    """每个用例前清空缓存 + 计数。"""
    store.invalidate(None)
    pv_client.clear_data_cache()
    CALLS["items"] = CALLS["text"] = CALLS["assets"] = 0


# ================================================================ get_slim

def test_get_slim_image_caches_second_call():
    reset_store()
    items, err = store.get_slim("image")
    assert err is None and len(items) == 3
    first = CALLS["items"]
    items2, err2 = store.get_slim("image")
    assert err2 is None and len(items2) == 3
    assert CALLS["items"] == first, "第二次应命中缓存，不再请求"


def test_get_slim_image_refresh_bypasses_cache():
    reset_store()
    store.get_slim("image")
    before = CALLS["items"]
    store.get_slim("image", refresh=True)
    assert CALLS["items"] == before + 1, "refresh=True 必须真的重拉"


def test_get_slim_image_fields_are_five_short_ones():
    """slim 只留 5 个短字段（1.8MB 那个预算），不能把 positive 带进来。"""
    reset_store()
    items, _ = store.get_slim("image")
    assert set(items[0]) == {"id", "name", "categoryId", "width", "height"}


def test_get_slim_text():
    reset_store()
    items, err = store.get_slim("text", text_type="loras")
    assert err is None
    assert set(items[0]) == {"id", "name", "text", "chars"}
    assert items[1]["chars"] == 0


def test_get_slim_asset_filters_by_category_and_project():
    reset_store()
    items, err = store.get_slim("asset", category="角色四视图")
    assert err is None and len(items) == 2
    items, _ = store.get_slim("asset", project="凡人")
    assert len(items) == 2
    items, _ = store.get_slim("asset", project=pv_client.NO_PROJECT)
    assert [i["id"] for i in items] == ["a3"], "「未分项目」应只匹配 project 为空的"


def test_get_slim_unknown_kind_returns_error():
    items, err = store.get_slim("nope")
    assert items is None and "不支持" in err


# ================================================================ page

def test_page_basic_slicing():
    rows = [{"id": f"x{i}", "name": f"名{i}"} for i in range(10)]
    res = store.page(rows, "", 1, 4)
    assert res["total"] == 10 and res["pages"] == 3
    assert [i["id"] for i in res["items"]] == ["x0", "x1", "x2", "x3"]
    res = store.page(rows, "", 3, 4)
    assert [i["id"] for i in res["items"]] == ["x8", "x9"]


def test_page_clamps_out_of_range():
    rows = [{"id": f"x{i}", "name": ""} for i in range(10)]
    assert store.page(rows, "", 0, 4)["page"] == 1
    assert store.page(rows, "", 999, 4)["page"] == 3
    assert store.page([], "", 5, 4) == {"total": 0, "page": 1, "pageSize": 4, "pages": 1, "items": []}


def test_page_clamps_page_size():
    rows = [{"id": f"x{i}", "name": ""} for i in range(1000)]
    assert store.page(rows, "", 1, 99999)["pageSize"] == store.MAX_PAGE_SIZE
    assert store.page(rows, "", 1, 0)["pageSize"] == 40
    assert store.page(rows, "", 1, -5)["pageSize"] == 40


def test_page_search_matches_item_fields():
    rows = [{"id": "1", "name": "海边少女"}, {"id": "2", "name": "赛博城市"}]
    assert [i["id"] for i in store.page(rows, "少女", 1, 10)["items"]] == ["1"]
    assert store.page(rows, "不存在的词", 1, 10)["total"] == 0


def test_page_search_with_haystack_uses_it():
    """图片的全文搜索靠 haystack（positive 不在 slim 里）。"""
    rows = [{"id": "1", "name": "a"}, {"id": "2", "name": "b"}]
    haystack = {"1": "写实摄影 少女", "2": "赛博朋克 夜景"}
    assert [i["id"] for i in store.page(rows, "赛博", 1, 10, haystack=haystack)["items"]] == ["2"]


def test_page_search_multiple_words_requires_all():
    rows = [{"id": "1", "name": "海边 少女"}, {"id": "2", "name": "海边 大叔"}]
    assert [i["id"] for i in store.page(rows, "海边 少女", 1, 10)["items"]] == ["1"]


# ================================================================ search_haystack

def test_search_haystack_covers_positive():
    reset_store()
    haystack, err = store.search_haystack("image")
    assert err is None
    assert "写实摄影" in haystack["i1"]
    assert "赛博朋克" in haystack["i2"]


def test_search_haystack_returns_none_for_text_and_asset():
    """text / asset 的 slim 本身已含全部可搜字段，不需要额外索引。"""
    assert store.search_haystack("text") == (None, None)
    assert store.search_haystack("asset") == (None, None)


# ================================================================ _search 只留一份

def test_search_index_keeps_only_one_category():
    """
    🔴 `_search` 全进程只留最近一个分类的索引。
    改成按分类多份缓存 → 内存随浏览历史线性增长（三份 positive 就 31MB+）。
    """
    reset_store()
    store.search_haystack("image", category_id="")
    assert len(store._search) == 1
    store.search_haystack("image", category_id="c1")
    assert len(store._search) == 1, "换分类后旧索引必须被丢弃，不能累积"
    assert list(store._search)[0][1] == "c1"


# ================================================================ lookup_*

def test_lookup_images_order_and_missing():
    reset_store()
    items, missing, err = store.lookup_images("", ["i3", "i1", "nope"])
    assert err is None
    assert [i["id"] for i in items] == ["i3", "i1"], "按传入顺序返回"
    assert missing == ["nope"]


def test_lookup_images_empty_ids():
    assert store.lookup_images("", []) == ([], [], None)


def test_lookup_text_returns_content():
    reset_store()
    items, missing, err = store.lookup_text("", "loras", ["t1", "nope"])
    assert err is None
    assert items[0]["text"] == "触发词内容"
    assert missing == ["nope"]


def test_lookup_assets_returns_metadata():
    reset_store()
    items, missing, err = store.lookup_assets("", ["a2", "nope"])
    assert err is None
    assert items[0]["name"] == "角色侧面"
    assert missing == ["nope"]


def test_lookup_uses_full_cache_on_second_call():
    reset_store()
    store.lookup_images("", ["i1"])
    before = len(store._full_by_id)
    store.lookup_images("", ["i1"])
    assert len(store._full_by_id) == before, "第二次应命中 _full_by_id，不新增条目"


# ================================================================ _full_by_id TTL

class _Clock:
    def __init__(self, start=1000.0):
        self.t = start

    def time(self):
        return self.t


def test_full_by_id_ttl_expires_at_600s():
    """
    `_full_by_id` 必须带 TTL：用户改了提示词后，永不过期的缓存会一直吐旧内容
    且不报错 —— 这是最难排查的一类 bug。
    """
    reset_store()
    real_time = store.time
    clock = _Clock()
    store.time = types.SimpleNamespace(time=clock.time)
    try:
        store._remember_full([{"id": "z1", "name": "旧名字"}])
        found, rest = store._from_full_cache(["z1"])
        assert list(found) == ["z1"] and rest == []

        clock.t += 599
        found, rest = store._from_full_cache(["z1"])
        assert list(found) == ["z1"], "599 秒仍在 TTL 内（同一轮批量执行保持一致）"

        clock.t += 2
        found, rest = store._from_full_cache(["z1"])
        assert list(found) == [] and rest == ["z1"], "601 秒必须过期重取"

        assert len(store._full_by_id) == 0, "过期条目要被真的删掉，否则 stats() 条目数虚高"
    finally:
        store.time = real_time


def test_full_by_id_lru_evicts_oldest():
    reset_store()
    limit = store.FULL_MAX
    rows = [{"id": f"lru{i}", "name": "x"} for i in range(limit + 5)]
    store._remember_full(rows)
    assert len(store._full_by_id) <= limit


# ================================================================ sample_random

def test_sample_random_unique_and_within_range():
    reset_store()
    ids, total, err = store.sample_random("image", n=2)
    assert err is None
    assert total == 3
    assert len(ids) == 2
    assert len(set(ids)) == 2, "不能重复"
    assert set(ids) <= {r["id"] for r in IMAGES[""]}


def test_sample_random_respects_exclude():
    reset_store()
    ids, total, _ = store.sample_random("image", n=2, exclude=["i1", "i2"])
    assert total == 3, "total 是排除前的候选总数"
    assert ids == ["i3"], "只剩一个候选就返回它"


def test_sample_random_never_exceeds_pool():
    reset_store()
    ids, total, err = store.sample_random("image", n=100)
    assert err is None
    assert len(ids) == total == 3


def test_sample_random_with_query_uses_same_filter_as_page():
    """pv_random 与 pv_list 必须共用同一套筛选（否则抽出来的在列表里找不到）。"""
    reset_store()
    listed = store.page(store.get_slim("image")[0], "赛博", 1, 100)["items"]
    ids, total, err = store.sample_random("image", n=10, q="赛博")
    assert err is None
    assert total == len(listed) == 1
    assert set(ids) == {i["id"] for i in listed}


# ================================================================ 失效

def test_invalidate_clears_everything():
    reset_store()
    store.get_slim("image")
    store.lookup_images("", ["i1"])
    store.invalidate(None)
    assert store.stats()["slim"] == 0
    assert store.stats()["full"] == 0
    assert store.stats()["search_index"] == 0


def test_invalidate_by_addr_only_clears_that_addr():
    reset_store()
    store.get_slim("image", addr="127.0.0.1:8765")
    assert store.stats()["slim"] >= 1
    store.invalidate("127.0.0.1:9999")
    assert store.stats()["slim"] >= 1, "清别的地址不该动这条"
    store.invalidate("127.0.0.1:8765")
    assert store.stats()["slim"] == 0


# ================================================================ 缩略图

def test_thumb_returns_bytes_and_mime():
    reset_store()
    data, mime, err = store.thumb("image", "", "i1", "thumb")
    assert err is None and data == b"jpeg-bytes"
    assert mime in ("image/jpeg", "application/octet-stream")


def test_thumb_caches_thumb_size_only():
    reset_store()
    store.thumb("image", "", "i1", "thumb")
    assert len(store._thumb) == 1
    store.thumb("image", "", "i1", "full")
    assert len(store._thumb) == 1, "原图不缓存（一张几 MB，收益低）"


def test_thumb_rejects_text_kind():
    data, mime, err = store.thumb("text", "", "t1", "thumb")
    assert data is None and "不支持" in err


# ================================================================ 不可变性

def test_page_returns_new_list_not_the_cache():
    """page 不能把缓存里的列表切片直接暴露出去（调用方改了会污染缓存）。"""
    reset_store()
    items, _ = store.get_slim("image")
    res = store.page(items, "", 1, 10)
    assert res["items"] is not items


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
