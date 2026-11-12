"""dogking-tools 的后端接口：原生文件/文件夹选择框 + 行数/图片数预览 + 重置计数
+ PromptVault 代转（浏览器跨源调不到 PromptVault，只能由后端中转）。

PromptVault 相关接口分两批：
  · v1：pv_categories（分类下拉）
  · v2：pv_list / pv_meta / pv_thumb / pv_random（面板的分页/元数据/缩略图/随机）
"""

import asyncio
import sys
import threading
import time

from aiohttp import web

_ROUTES = None
# 同一时间只允许开一个文件选择框，避免连点弹出好几个
_dialog_busy = threading.Lock()


# ---------------------------------------------------------------- 文件选择框

def _ask_file_windows():
    """
    Windows 原生「打开文件」对话框。

    直接用 ctypes 调 comdlg32.GetOpenFileNameW —— ComfyUI 的 python_embeded
    是精简版，**不带 tkinter**，所以不能依赖它。

    @returns {str} 选中的路径；用户取消或失败返回空串
    """
    import ctypes
    from ctypes import wintypes

    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wintypes.DWORD),
            ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE),
            ("lpstrFilter", wintypes.LPCWSTR),
            ("lpstrCustomFilter", wintypes.LPWSTR),
            ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD),
            ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD),
            ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD),
            ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR),
            ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD),
            ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR),
            ("lCustData", wintypes.LPARAM),
            ("lpfnHook", wintypes.LPVOID),
            ("lpTemplateName", wintypes.LPCWSTR),
            ("pvReserved", wintypes.LPVOID),
            ("dwReserved", wintypes.DWORD),
            ("FlagsEx", wintypes.DWORD),
        ]

    buf = ctypes.create_unicode_buffer(4096)
    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)

    # 关键：必须给对话框一个 owner 窗口，否则它会掉到任务栏里，
    # 用户得点任务栏图标才能看到 —— 表现就是「只在 Windows 底部弹」。
    # 这里拿当前前台窗口（用户刚点的浏览器）当 owner。
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if hwnd:
        try:
            user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
    ofn.hwndOwner = hwnd or None

    # 过滤器要用 \0 分隔，最后再来一个 \0 收尾
    ofn.lpstrFilter = (
        "提示词文件\0*.txt;*.csv\0"
        "文本文件 (*.txt)\0*.txt\0"
        "CSV 文件 (*.csv)\0*.csv\0"
        "所有文件 (*.*)\0*.*\0\0"
    )
    # 注意：wintypes.LPWSTR 就是 ctypes.c_wchar_p，
    # cast 成 POINTER(c_wchar) 会得到 LP_c_wchar，赋值时报类型不兼容。
    ofn.lpstrFile = ctypes.cast(buf, ctypes.c_wchar_p)
    ofn.nMaxFile = 4096
    ofn.lpstrTitle = "选择提示词文件（txt / csv）"
    ofn.lpstrDefExt = "txt"
    # OFN_EXPLORER | OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_NOCHANGEDIR
    ofn.Flags = 0x00080000 | 0x00001000 | 0x00000800 | 0x00000008

    ok = ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn))
    if not ok:
        # 0 = 用户点了取消，或调用失败
        return ""
    return buf.value or ""


def _ask_file_tk():
    """非 Windows 平台的兜底：有 tkinter 就用它。"""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as e:
        raise RuntimeError(f"当前环境没有 tkinter：{e}")

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        return filedialog.askopenfilename(
            title="选择提示词文件（txt / csv）",
            filetypes=[("提示词文件", "*.txt *.csv"), ("所有文件", "*.*")],
        ) or ""
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def _ask_folder_windows():
    """
    Windows 原生「选择文件夹」对话框。

    选文件和选文件夹是两个完全不同的 API：GetOpenFileNameW 只能选文件，
    选目录必须用 shell32.SHBrowseForFolderW（BROWSEINFOW 结构体）。

    两个必须注意的点：
      1. pszDisplayName 的类型是 LPWSTR（= ctypes.c_wchar_p）。
         cast 成 POINTER(c_wchar) 会得到 LP_c_wchar，赋值时报
         「incompatible types」—— 这个坑踩过一次。
      2. SHBrowseForFolderW 返回的是 PIDL（指针）。不显式声明 restype 的话
         ctypes 按默认的 c_int 处理，64 位下高 32 位被截断，后面
         SHGetPathFromIDListW 直接读坏内存 / 崩溃。所以 restype 必须
         声明成 c_void_p，用完还要 CoTaskMemFree 释放，否则内存泄漏。

    @returns {str} 选中的文件夹路径；用户取消或失败返回空串
    """
    import ctypes
    from ctypes import wintypes

    BIF_RETURNONLYFSDIRS = 0x0001   # 只能选目录
    BIF_NEWDIALOGSTYLE = 0x0040     # 新式对话框（可拖动、可新建文件夹）

    class BROWSEINFOW(ctypes.Structure):
        _fields_ = [
            ("hwndOwner", wintypes.HWND),
            ("pidlRoot", ctypes.c_void_p),
            ("pszDisplayName", wintypes.LPWSTR),
            ("lpszTitle", wintypes.LPCWSTR),
            ("ulFlags", wintypes.UINT),
            ("lpfn", ctypes.c_void_p),
            ("lParam", wintypes.LPARAM),
            ("iImage", ctypes.c_int),
        ]

    shell32 = ctypes.windll.shell32
    shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BROWSEINFOW)]
    shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
    shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]

    buf = ctypes.create_unicode_buffer(1024)
    bi = BROWSEINFOW()
    bi.hwndOwner = ctypes.windll.user32.GetForegroundWindow()
    bi.pszDisplayName = ctypes.cast(buf, ctypes.c_wchar_p)
    bi.lpszTitle = "选择图片文件夹"
    bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE

    pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
    if not pidl:
        # 0 = 用户点了取消
        return ""
    try:
        path_buf = ctypes.create_unicode_buffer(1024)
        # argtypes 里声明的是 LPWSTR，显式 cast 一下，避免传数组时的类型歧义
        if not shell32.SHGetPathFromIDListW(pidl, ctypes.cast(path_buf, ctypes.c_wchar_p)):
            return ""
        return path_buf.value or ""
    finally:
        # PIDL 是 shell 分配的内存，必须还回去
        try:
            ctypes.windll.ole32.CoTaskMemFree(ctypes.c_void_p(pidl))
        except Exception:
            pass


def _ask_folder_tk():
    """非 Windows 平台的兜底：有 tkinter 就用它选目录。"""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as e:
        raise RuntimeError(f"当前环境没有 tkinter：{e}")

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        return filedialog.askdirectory(title="选择图片文件夹") or ""
    finally:
        try:
            root.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------- 接口实现

async def select_file(request):
    """
    弹系统原生文件选择框，返回选中的路径。

    Windows 上放到线程池里跑 —— 实测 GetOpenFileNameW 在子线程也能正常弹窗，
    这样对话框开着的时候 ComfyUI 其他请求（进度、预览）照常响应，不会假死。
    tkinter 必须在主线程，非 Windows 只能同步调。
    """
    global _dialog_busy
    if not _dialog_busy.acquire(blocking=False):
        return web.json_response({"path": "", "error": "已经有一个选择框开着，先关掉它"})

    try:
        if sys.platform == "win32":
            loop = asyncio.get_running_loop()
            path = await loop.run_in_executor(None, _ask_file_windows)
        else:
            path = _ask_file_tk()
        return web.json_response({"path": path or ""})
    except Exception as e:
        return web.json_response({"path": "", "error": str(e)})
    finally:
        _dialog_busy.release()


async def select_folder(request):
    """
    弹系统原生文件夹选择框，返回选中的路径。

    和 select_file 完全同款：Windows 走线程池（对话框开着的时候
    ComfyUI 其他请求照常响应），并共用 _dialog_busy 锁 —— 连点两下
    只会弹一个框，用户不会看到一叠对话框。
    """
    global _dialog_busy
    if not _dialog_busy.acquire(blocking=False):
        return web.json_response({"path": "", "error": "已经有一个选择框开着，先关掉它"})

    try:
        if sys.platform == "win32":
            loop = asyncio.get_running_loop()
            path = await loop.run_in_executor(None, _ask_folder_windows)
        else:
            path = _ask_folder_tk()
        return web.json_response({"path": path or ""})
    except Exception as e:
        return web.json_response({"path": "", "error": str(e)})
    finally:
        _dialog_busy.release()


async def list_images(request):
    """
    返回文件夹里的图片数量和前几个文件名，前端用来显示「共 N 张」。

    body: {"folder": "D:/pics", "sort": "数字升序"}
    返回: {"total": 120, "preview": [...], "error": null}
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    folder = body.get("folder", "")
    sort_mode = body.get("sort", "")

    from .nodes import SORT_NUMERIC, count_images
    return web.json_response(count_images(folder, sort_mode or SORT_NUMERIC))


async def count(request):
    """返回文件行数，前端用来在节点上显示「共 N 条」。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    # body 是字符串 / 数组 / 数字时 .get 会 AttributeError 变成 500，
    # 和 list_images 一样先收敛成 dict（畸形请求当没填路径处理）
    if not isinstance(body, dict):
        body = {}
    path = body.get("path", "")

    from .nodes import count_lines
    return web.json_response(count_lines(path))


async def reset(request):
    """
    清掉某个节点的计数（前端「重置计数」按钮用）。

    节点从来没有执行过时 state.json 里没有它的记录 —— 这种情况等同于
    「计数本来就是 0」，直接返回成功，前端不必弹错误框。
    body 里不带 node 时表示清空全部记录。

    两个坑（都踩过）：
      1. 请求体畸形时绝不能落到「清空全部」分支 —— 否则任何一个坏请求
         都会把所有节点的进度抹掉。畸形请求一律当「没做事」返回成功。
      2. 读-改-写必须和 nodes.load() 共用 _state_lock。不然会出现
         load 读(index=3) → reset 写 0 → load 写回 4，重置被静默吞掉：
         界面提示「已重置」，下次却仍从下一行开始。
    """
    try:
        body = await request.json()
    except Exception:
        # 畸形请求不能当成「清空全部」，当没做事处理
        return web.json_response({"ok": True})

    # 同上：body 不是对象（列表 / 字符串 / 数字）也当畸形请求
    if not isinstance(body, dict):
        return web.json_response({"ok": True})

    raw_node = body.get("node", "")
    if raw_node is None:
        # 显式传 null 也当畸形请求 —— 宁可不做事，也不能误清空全部
        return web.json_response({"ok": True})
    node = str(raw_node).strip()

    from .nodes import _load_state, _save_state, _state_lock

    # 整段读-改-写持锁；返回值构造放到锁外面，别持锁做别的活
    with _state_lock:
        state = _load_state()
        if node:
            # isinstance 检查是必要的：_load_state() 只保证顶层是 dict，
            # 用户手工把 state.json 编辑成 {"12": 5} 时 state[node]["index"]
            # 会抛 TypeError 变成 500。脏数据静默跳过，宁可不做事也不能 500。
            if isinstance(state.get(node), dict):
                state[node]["index"] = 0
                _save_state(state)
        else:
            _save_state({})

    if node:
        return web.json_response({"ok": True})
    return web.json_response({"ok": True, "all": True})


# ---------------------------------------------------------------- PromptVault

async def _read_json_body(request):
    """
    读请求体并收敛成 dict。

    畸形请求（字符串 / 数组 / 数字 / 空 body）一律当「什么都没填」处理 ——
    否则 body.get 会 AttributeError 变成 500，前端只能看到「服务器错误」。
    """
    try:
        body = await request.json()
    except Exception:
        return {}
    if not isinstance(body, dict):
        return {}
    return body


def _filters_from_body(kind, body):
    """
    把请求体里的筛选字段翻译成 pv_store.get_slim 的关键字参数。

    三种 kind 的筛选维度完全不同（图片按分类 id，资产按分类名 + 项目，词库按类型），
    所以集中在这里翻译，避免每个 handler 各写一遍、字段名写错一个就静默失效。
    """
    k = str(kind or "").strip().lower()
    if k in ("asset", "assets"):
        return {
            "category": str(body.get("category") or ""),
            "project": str(body.get("project") or ""),
        }
    if k in ("text", "loras", "edits", "system-prompts", "system_prompts"):
        return {"text_type": str(body.get("textType") or "loras")}
    return {"category_id": str(body.get("categoryId") or "")}


async def pv_categories(request):
    """
    返回 PromptVault 的分类路径列表，前端用来填「分类」下拉框。

    为什么不让前端直接调 PromptVault：那边的服务只监听 127.0.0.1，而且
    刻意不发 Access-Control-Allow-Origin（防止任意网页读到本地数据），
    浏览器会拦掉跨源响应。所以只能由 ComfyUI 后端代转一次。

    body: {"addr": "127.0.0.1:8766", "kind": "image"|"asset"}
          （addr 留空 = 自动发现 8765-8775；kind 缺省 = image，向后兼容）
    返回: {"ok": true, "categories": [{"path","id","level","parentPath"}]}
          kind=asset 时额外有 "projects": ["角色", ..., "未分项目"]
          连不上时 {"ok": false, "error": "PromptVault 未运行（试过 8765-8775）..."}
    """
    body = await _read_json_body(request)
    addr = body.get("addr", "")
    kind = str(body.get("kind") or "image").strip().lower()

    loop = asyncio.get_running_loop()

    if kind in ("asset", "assets"):
        from . import pv_client
        # 资产图的分类/项目是从资产列表聚合出来的（桥接口没有单独的列表接口）
        result = await loop.run_in_executor(None, pv_client.asset_facet_choices, addr)
        return web.json_response(result)

    # kind=image 的行为与改造前**完全一致**，只是 build_category_paths 多给了 parentPath
    from .pv_client import list_category_paths

    # 网络请求最长 3 秒，绝不能放在事件循环里同步跑，否则 ComfyUI 整个假死
    result = await loop.run_in_executor(None, list_category_paths, addr)
    return web.json_response(result)


# ---------------------------------------------------------------- PromptVault v2
#
# 下面 4 个接口是面板（web/js/lib/*）用的数据入口。
#
# 铁律：**所有网络调用必须走 loop.run_in_executor**。
# urllib 是同步阻塞的，直接 await 一个同步函数会让 aiohttp 的事件循环停住 ——
# 表现是「点开面板的瞬间整个 ComfyUI 界面假死，进度条都不动」。
# 这和现有 pv_categories 的写法保持一致。

async def pv_list(request):
    """
    分页列表（面板的主数据源）。

    body: {"addr":"", "kind":"image"|"asset"|"text",
           "categoryId":"", "category":"", "project":"", "textType":"loras",
           "q":"", "page":1, "pageSize":40, "refresh":false}
    返回: {"ok":true, "total":9328, "page":1, "pageSize":40, "pages":234,
           "addr":"http://127.0.0.1:8765", "tookMs":12, "items":[...]}

    items 字段按 kind 精简（image: id/name/categoryId/width/height），
    **不含 positive/negative/params 长文本** —— 那些由 pv_meta 按 id 单独取。
    """
    body = await _read_json_body(request)
    addr = str(body.get("addr") or "")
    kind = str(body.get("kind") or "image")
    q = str(body.get("q") or "")
    page_no = body.get("page", 1)
    page_size = body.get("pageSize", 40)
    refresh = bool(body.get("refresh"))
    filters = _filters_from_body(kind, body)

    def _work():
        from . import pv_client, pv_store

        started = time.perf_counter()
        items, err = pv_store.get_slim(kind, addr, refresh=refresh, **filters)
        if err:
            return {"ok": False, "error": err}

        haystack = None
        if q.strip():
            # 图片的 positive/negative/params 不在精简列表里，搜索要单独拿全文
            haystack, hay_err = pv_store.search_haystack(kind, addr, **filters)
            if hay_err:
                return {"ok": False, "error": hay_err}

        result = pv_store.page(items, q, page_no, page_size, haystack=haystack)
        return {
            "ok": True,
            "total": result["total"],
            "page": result["page"],
            "pageSize": result["pageSize"],
            "pages": result["pages"],
            "addr": pv_client.resolve_base(addr) or "",
            "tookMs": int((time.perf_counter() - started) * 1000),
            "items": result["items"],
        }

    loop = asyncio.get_running_loop()
    payload = await loop.run_in_executor(None, _work)
    return web.json_response(payload)


async def pv_meta(request):
    """
    按 id 批量取完整元数据（执行阶段用：工作流里存的是 id 清单）。

    body: {"addr":"", "kind":"image"|"asset"|"text", "ids":["a1","a2"], "textType":"loras"}
    返回: {"ok":true, "items":[完整元数据...], "missing":["已失效的 id"]}

    missing 里的 id 是 PromptVault 里已经不存在的（用户删了图），
    节点据此跳过并提示，**不是错误**。
    """
    body = await _read_json_body(request)
    addr = str(body.get("addr") or "")
    kind = str(body.get("kind") or "image")
    ids = body.get("ids")
    if not isinstance(ids, list):
        ids = []
    text_type = str(body.get("textType") or "loras")

    def _work():
        from . import pv_store

        k = kind.strip().lower()
        if k in ("asset", "assets"):
            return pv_store.lookup_assets(addr, ids)
        if k in ("text", "loras", "edits", "system-prompts", "system_prompts"):
            return pv_store.lookup_text(addr, text_type, ids)
        return pv_store.lookup_images(addr, ids)

    loop = asyncio.get_running_loop()
    items, missing, err = await loop.run_in_executor(None, _work)
    if err:
        return web.json_response({"ok": False, "error": err, "items": [], "missing": []})
    return web.json_response({"ok": True, "items": items or [], "missing": missing or []})


async def pv_thumb(request):
    """
    缩略图 / 原图代理（前端直接当 <img src> 用，所以是 GET）。

    为什么必须走后端代理：PromptVault 只监听回环且刻意不发 CORS 头，
    浏览器里 <img src="http://127.0.0.1:8765/..."> 会被拦掉。

    query: id=<id>&kind=image|asset&size=thumb|full
    返回: 二进制 + 透传的 Content-Type + Cache-Control: public, max-age=600
          失败 404（前端 img.onerror 换占位块）/ 503（PromptVault 没开）

    注意必须用 web.Response(body=...) 而不是 json_response —— 后者会把
    bytes 当 JSON 序列化，图片直接变成一串数字。
    """
    item_id = request.query.get("id", "")
    kind = request.query.get("kind", "image")
    size = request.query.get("size", "thumb")
    addr = request.query.get("addr", "")

    def _work():
        from . import pv_store
        return pv_store.thumb(kind, addr, item_id, size)

    loop = asyncio.get_running_loop()
    data, mime, err = await loop.run_in_executor(None, _work)

    if err or not data:
        message = err or "图片不存在"
        # PromptVault 没开是「服务不可用」，不是「这张图不存在」——
        # 区分开，前端才能决定是提示用户开 PromptVault 还是画占位块
        status = 503 if "未运行" in message else 404
        return web.json_response({"ok": False, "error": message}, status=status)

    return web.Response(
        body=data,
        content_type=mime or "application/octet-stream",
        headers={"Cache-Control": "public, max-age=600"},
    )


async def pv_random(request):
    """
    从**当前筛选下的全部记录**里随机抽 N 个不重复 id。

    body: {"addr":"", "kind":"image", "categoryId":"", "category":"", "project":"",
           "textType":"loras", "q":"", "n":4, "exclude":["a1"]}
    返回: {"ok":true, "ids":["x9","x3"], "total":17726}

    为什么必须放后端：随机范围是「当前筛选下全部 N 张」，
    而前端手里只有当前页 40 张 —— 在 40 张里抽，用户两次就发现「随机总是这几个」。
    """
    body = await _read_json_body(request)
    addr = str(body.get("addr") or "")
    kind = str(body.get("kind") or "image")
    q = str(body.get("q") or "")
    n = body.get("n", 1)
    exclude = body.get("exclude")
    if not isinstance(exclude, list):
        exclude = []
    filters = _filters_from_body(kind, body)

    def _work():
        from . import pv_store
        return pv_store.sample_random(kind, addr, n=n, exclude=exclude, q=q, **filters)

    loop = asyncio.get_running_loop()
    ids, total, err = await loop.run_in_executor(None, _work)
    if err:
        return web.json_response({"ok": False, "error": err, "ids": [], "total": 0})
    return web.json_response({"ok": True, "ids": ids or [], "total": total})


# ---------------------------------------------------------------- 注册

def register_routes():
    """
    把上面这些接口挂到 ComfyUI 的 aiohttp 路由表上。

    ComfyUI 正常启动时 PromptServer.instance 一定存在；但加载顺序或测试环境下
    它可能是 None，这时返回 False，由调用方决定要不要重试。

    @returns {bool} 是否注册成功
    """
    global _ROUTES
    try:
        from server import PromptServer
        _ROUTES = PromptServer.instance.routes
    except Exception:
        _ROUTES = None

    if _ROUTES is None:
        return False

    _ROUTES.post("/dogking-tools/select_file")(select_file)
    _ROUTES.post("/dogking-tools/select_folder")(select_folder)
    _ROUTES.post("/dogking-tools/count")(count)
    _ROUTES.post("/dogking-tools/list_images")(list_images)
    _ROUTES.post("/dogking-tools/reset")(reset)
    _ROUTES.post("/dogking-tools/pv_categories")(pv_categories)
    # v2 面板数据接口（4 条）。pv_thumb 是 GET —— 前端要能直接当 <img src> 用
    _ROUTES.post("/dogking-tools/pv_list")(pv_list)
    _ROUTES.post("/dogking-tools/pv_meta")(pv_meta)
    _ROUTES.post("/dogking-tools/pv_random")(pv_random)
    _ROUTES.get("/dogking-tools/pv_thumb")(pv_thumb)
    return True


def register_routes_async(retries=40, interval=1.0):
    """PromptServer 还没就绪时，后台等它起来再注册。"""
    def _worker():
        for _ in range(retries):
            time.sleep(interval)
            if register_routes():
                print("[dogking-tools] 后端接口已延迟注册成功")
                return
        print("[dogking-tools] 后端接口注册失败：PromptServer 一直没就绪")

    threading.Thread(target=_worker, daemon=True).start()
