# -*- coding: utf-8 -*-
"""
pv_client.py 测试：HTTP 层的硬不变量 + 分类路径 + 资产图接口

HTTP 全部打桩（替换 pv_client._open），不依赖真实 PromptVault，
也不写用户目录里的任何缓存文件（CATEGORY_CACHE_FILE / ASSETS_CACHE_FILE 都指到临时目录）。

**最重要的一个测试是第 1 组**：`fetch_items` 带分类时必须带 includeChildren=1。
删掉这个参数的后果是「选任何一级分类都返回 0 条」，而且不报错、不抛异常、
界面上只显示「该分类下没有图片」—— 排查极痛苦，所以用断言把它钉住。
"""
import importlib.util
import io
import json
import os
import sys
import tempfile
import urllib.parse

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 插件根目录

# pv_client 里写的是 `from .nodes import _blank_image`，独立加载时走兜底 `from nodes import ...`，
# 所以先把 nodes.py 加载并注册成 "nodes"
spec = importlib.util.spec_from_file_location("dk_nodes_client", os.path.join(HERE, "nodes.py"))
nodes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nodes)
sys.modules["nodes"] = nodes

spec2 = importlib.util.spec_from_file_location("dk_pv_client", os.path.join(HERE, "pv_client.py"))
pv = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(pv)

# 缓存文件指到临时目录，别写进插件目录
tmpdir = tempfile.mkdtemp()
pv.CATEGORY_CACHE_FILE = os.path.join(tmpdir, "pv_categories_cache.json")
pv.ASSETS_CACHE_FILE = os.path.join(tmpdir, "pv_assets_cache.json")

PASS, FAIL = [], []


def check(name, got, want):
    ok = got == want
    (PASS if ok else FAIL).append(name)
    print(f"{'PASS' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"     期望: {want!r}")
        print(f"     实际: {got!r}")


def check_true(name, got):
    check(name, bool(got), True)


# ---------------------------------------------------------------- HTTP 打桩

class _FakeResp:
    """假的 urlopen 响应：支持 with 语句 + read()。"""

    def __init__(self, payload, status=200):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload).encode("utf-8")
        elif isinstance(payload, str):
            payload = payload.encode("utf-8")
        self._body = payload
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Stub:
    """记录请求 URL，并按路由返回预设 JSON。"""

    def __init__(self, routes=None):
        self.urls = []
        self.routes = routes or {}

    def __call__(self, req, timeout):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        self.urls.append(url)
        path = urllib.parse.urlparse(url).path
        payload = self.routes.get(path)
        if payload is None:
            payload = {"ok": True, "items": [], "categories": []}
        return _FakeResp(payload)

    def last(self):
        return self.urls[-1] if self.urls else ""

    def query(self, url=None):
        target = url if url is not None else self.last()
        return urllib.parse.parse_qs(urllib.parse.urlparse(target).query)


_real_open = pv._open
ADDR = "127.0.0.1:9999"  # 非空 → resolve_base 直接返回，不会去探 11 个端口


def install(routes=None):
    """装一个新桩并清掉 1 秒微缓存（否则上一个用例的结果会被复用）。"""
    stub = _Stub(routes)
    pv._open = stub
    pv.reset_caches()
    return stub


# ================================================================
print("=== 1. includeChildren 硬不变量（一级分类漏传就返回 0 条）===")

stub = install()
pv.fetch_items(ADDR, category_id="mvz14azd-3y2j1v")
q = stub.query()
check("带分类时 URL 含 includeChildren=1", q.get("includeChildren"), ["1"])
check("带分类时 URL 含 category", q.get("category"), ["mvz14azd-3y2j1v"])
check("type 固定 images", q.get("type"), ["images"])
check_true("请求打到了 /api/items", stub.last().find("/api/items") >= 0)

stub = install()
pv.fetch_items(ADDR, category_id="", search="")
q = stub.query()
check("不选分类时不带 category", "category" in q, False)
check("不选分类时不带 includeChildren", "includeChildren" in q, False)

stub = install()
pv.fetch_items(ADDR, category_id="cat_x", include_children=False)
check("显式关掉时确实不带 includeChildren", "includeChildren" in stub.query(), False)

stub = install()
pv.fetch_items(ADDR, category_id="cat_x", search="写实")
q = stub.query()
check("搜索词照常带上", q.get("search"), ["写实"])
check("搜索时也保留 includeChildren=1", q.get("includeChildren"), ["1"])

stub = install()
pv.fetch_items(ADDR, ids=["a1", "a2"])
q = stub.query()
check("按 id 取时走 ids 参数", q.get("ids"), ["a1,a2"])
check("按 id 取时不带 category", "category" in q, False)
check("按 id 取时不带 includeChildren", "includeChildren" in q, False)


# ================================================================
print("\n=== 2. build_category_paths：按 parentId 拼路径 + parentPath ===")

CATS = [
    {"id": "t1", "name": "风格", "level": 1, "parentId": None, "order": 0},
    {"id": "s2", "name": "油画", "level": 2, "parentId": "t1", "order": 2},
    {"id": "s1", "name": "写实摄影", "level": 2, "parentId": "t1", "order": 1},
    {"id": "t2", "name": "主体", "level": 1, "parentId": None, "order": 1},
    {"id": "s3", "name": "女性", "level": 2, "parentId": "t2", "order": 0},
    {"id": "orphan", "name": "孤儿分类", "level": 2, "parentId": "gone", "order": 9},
]
paths = pv.build_category_paths(CATS)
by_path = {p["path"]: p for p in paths}

check("第一项固定是「全部」", paths[0]["path"], pv.ALL_CATEGORY)
check("「全部」的 level 是 0", paths[0]["level"], 0)
check("一级路径 = 名字本身", "风格" in by_path, True)
check("二级路径 = 一级/二级", "风格/写实摄影" in by_path, True)
check("一级的 parentPath 为空", by_path["风格"]["parentPath"], "")
check("二级的 parentPath = 一级名字", by_path["风格/写实摄影"]["parentPath"], "风格")
check("二级的 id 正确", by_path["风格/写实摄影"]["id"], "s1")
check("孤儿分类也保留", "孤儿分类" in by_path, True)
check("孤儿分类的 parentPath 为空", by_path["孤儿分类"]["parentPath"], "")

order_paths = [p["path"] for p in paths if p["level"] >= 1]
check("按 order 排序（一级内二级有序）",
      order_paths, ["风格", "风格/写实摄影", "风格/油画", "主体", "主体/女性", "孤儿分类"])

check("category_id_from_path 能反查 id",
      pv.category_id_from_path("风格/写实摄影", CATS), "s1")
check("「全部」反查为空串", pv.category_id_from_path(pv.ALL_CATEGORY, CATS), "")
check("不存在的路径返回空串", pv.category_id_from_path("没有这个", CATS), "")


# ================================================================
print("\n=== 3. resolve_category_id：两种调用方式 ===")

install({"/api/categories": {"ok": True, "categories": CATS}})
check("完整路径形式", pv.resolve_category_id(ADDR, "风格/写实摄影"), "s1")
pv.reset_caches()
check("一级 + 二级两段形式", pv.resolve_category_id(ADDR, "风格", "写实摄影"), "s1")
pv.reset_caches()
check("只传一级", pv.resolve_category_id(ADDR, "风格"), "t1")
pv.reset_caches()
check("「全部」返回空串", pv.resolve_category_id(ADDR, pv.ALL_CATEGORY), "")


# ================================================================
print("\n=== 4. fetch_items_by_ids / fetch_assets_by_ids：missing 兜底 ===")

stub = install({"/api/items": {"ok": True, "items": [{"id": "a1", "name": "图1"}]}})
items, missing, err = pv.fetch_items_by_ids(ADDR, ["a1", "a2"])
check("服务端没给 missing 时自己算", missing, ["a2"])
check("拿到的条目正确", [i["id"] for i in items], ["a1"])
check("无错误", err, None)
check("ids 用逗号拼接", stub.query().get("ids"), ["a1,a2"])

stub = install({"/api/items": {"ok": True, "items": [], "missing": ["x", "y"]}})
items, missing, err = pv.fetch_items_by_ids(ADDR, ["x", "y"])
check("服务端给了 missing 就原样用", missing, ["x", "y"])

items, missing, err = pv.fetch_items_by_ids(ADDR, [])
check("空 id 列表直接返回空", (items, missing, err), ([], [], None))


# ================================================================
print("\n=== 5. fetch_assets：project 的「未分项目」映射 ===")

stub = install({"/api/assets": {"ok": True, "items": []}})
pv.fetch_assets(ADDR, project=pv.NO_PROJECT)
check("未分项目 → __none__", stub.query().get("project"), [pv.NO_PROJECT_KEY])

stub = install({"/api/assets": {"ok": True, "items": []}})
pv.fetch_assets(ADDR, project="凡人")
check("普通项目名原样传", stub.query().get("project"), ["凡人"])

stub = install({"/api/assets": {"ok": True, "items": []}})
pv.fetch_assets(ADDR, category=pv.ALL_CATEGORY)
check("「全部」分类不传 category", "category" in stub.query(), False)


# ================================================================
print("\n=== 6. asset_facet_choices：从资产列表聚合分类/项目 ===")

ASSETS = [
    {"id": "a1", "category": "角色四视图", "project": "凡人"},
    {"id": "a2", "category": "角色四视图", "project": ""},
    {"id": "a3", "category": "场景资产图", "project": "星穹铁道"},
    {"id": "a4", "category": "", "project": ""},
]
install({"/api/assets": {"ok": True, "items": ASSETS}})
facets = pv.asset_facet_choices(ADDR)
check("ok", facets["ok"], True)
check("分类去重排序", [c["path"] for c in facets["categories"]], ["场景资产图", "角色四视图"])
check("分类带 parentPath 字段（与图片分类结构一致）",
      all("parentPath" in c for c in facets["categories"]), True)
check("项目去重排序 + 未分项目在最后",
      facets["projects"], ["凡人", "星穹铁道", pv.NO_PROJECT])


# ================================================================
print("\n=== 7. PromptVault 没开时：返回 (None, err)，绝不抛异常 ===")

import urllib.error


def _refuse(req, timeout):
    raise urllib.error.URLError("connection refused")


pv._open = _refuse
pv.reset_caches()

items, err = pv.fetch_items(ADDR, category_id="c1")
check("fetch_items 返回 None", items, None)
check_true("fetch_items 给出「未运行」文案", err and "未运行" in err)

assets, err = pv.fetch_assets(ADDR)
check("fetch_assets 返回 None", assets, None)
check_true("fetch_assets 给出「未运行」文案", err and "未运行" in err)

data, err = pv.fetch_asset_image(ADDR, "a1")
check("fetch_asset_image 返回 None", data, None)
check_true("fetch_asset_image 给出「未运行」文案", err and "未运行" in err)

items, missing, err = pv.fetch_items_by_ids(ADDR, ["a1"])
check("fetch_items_by_ids 返回 None", items, None)
check_true("fetch_items_by_ids 有错误文案", bool(err))

# 落盘兜底：先成功一次把缓存写下来，再断网，应该还能拿到列表
pv._open = _Stub({"/api/assets": {"ok": True, "items": ASSETS}})
pv.reset_caches()
pv.asset_facet_choices(ADDR)  # 这次会写 pv_assets_cache.json
pv._open = _refuse
fallback = pv.asset_facet_choices(ADDR)
check("断网时用落盘缓存兜底", fallback.get("ok"), True)
check("兜底结果标记 stale", fallback.get("stale"), True)
check("兜底仍有分类", [c["path"] for c in fallback["categories"]], ["场景资产图", "角色四视图"])

# 还原（同一进程里可能还有别的测试）
pv._open = _real_open
pv.reset_caches()


def test_pv_client_suite():
    """
    给 pytest 的入口。

    这个文件是**脚本式**的：53 条 `check()` 在模块 import 时就已经全部跑完了。
    pytest 只能收集 `test_` 开头的函数，所以这里补一个粗粒度的包装，
    把脚本式的结果转成一条 pytest 断言 —— 否则 `pytest tests/` 里
    **完全不含这个文件的断言**（而它守着最关键的 includeChildren 不变量）。

    为什么把 `sys.exit` 收进 `__main__` 守卫：
        原来 `sys.exit(1)` 写在模块级，pytest 一 import 就被它带走 ——
        失败时不是一条干净的红灯，而是 collection 期抛 SystemExit，
        pytest 报 INTERNALERROR 崩掉整轮，还会吞掉其它文件的报表。
        「最有价值的测试配了最差的失败模式」，改掉它。

    以后若要更精确的失败定位，可以把 53 条 check 拆成独立 test_ 函数；
    现在先用这条包装兜住，保证「跑 pytest 就等于跑了全部测试」。
    """
    assert not FAIL, f"test_pv_client.py 有 {len(FAIL)} 条断言失败：{FAIL}"


if __name__ == "__main__":
    # 直接 `python tests/test_pv_client.py` 时才有汇总与退出码
    print("\n" + "=" * 50)
    print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)
