"""dogking-tools 的后端接口：原生文件/文件夹选择框 + 行数/图片数预览 + 重置计数
+ PromptVault 分类代转（浏览器跨源调不到 PromptVault，只能由后端中转）。"""

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

async def pv_categories(request):
    """
    返回 PromptVault 的分类路径列表，前端用来填「分类」下拉框。

    为什么不让前端直接调 PromptVault：那边的服务只监听 127.0.0.1，而且
    刻意不发 Access-Control-Allow-Origin（防止任意网页读到本地数据），
    浏览器会拦掉跨源响应。所以只能由 ComfyUI 后端代转一次。

    body: {"addr": "127.0.0.1:8766"}（留空 = 自动发现 8765-8775）
    返回: {"ok": true, "categories": [{"path": "风格", "id": "...", "level": 1}]}
          连不上时 {"ok": false, "error": "PromptVault 未运行（试过 8765-8775）..."}
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    # 畸形请求（字符串 / 数组 / 数字）一律当「没填地址」处理
    if not isinstance(body, dict):
        body = {}
    addr = body.get("addr", "")

    from .pv_nodes import list_category_paths

    # 网络请求最长 3 秒，绝不能放在事件循环里同步跑，否则 ComfyUI 整个假死
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, list_category_paths, addr)
    return web.json_response(result)


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
