"""dogking-tools 的后端接口：原生文件/文件夹选择框 + 行数/图片数预览 + 重置计数
+ PromptVault 代转（浏览器跨源调不到 PromptVault，只能由后端中转）。

PromptVault 相关接口分两批：
  · v1：pv_categories（分类下拉）
  · v2：pv_list / pv_meta / pv_thumb / pv_random（面板的分页/元数据/缩略图/随机）
"""

import asyncio
import os
import sys
import threading
import time

from aiohttp import web

_ROUTES = None
# 同一时间只允许开一个文件选择框，避免连点弹出好几个
_dialog_busy = threading.Lock()

# 各用途的对话框过滤器与标题。都写成常量，避免每个 handler 里各拼一份
# （\0 分隔的写法错一个字符就会让对话框里什么都不显示）。
FILTER_PROMPT = (
    "提示词文件\0*.txt;*.csv\0"
    "文本文件 (*.txt)\0*.txt\0"
    "CSV 文件 (*.csv)\0*.csv\0"
    "所有文件 (*.*)\0*.*\0\0"
)
TITLE_PROMPT = "选择提示词文件（txt / csv）"

FILTER_AUDIO = (
    "音频文件\0*.wav;*.mp3;*.flac;*.ogg;*.m4a;*.aac\0"
    "WAV (*.wav)\0*.wav\0"
    "MP3 (*.mp3)\0*.mp3\0"
    "所有文件 (*.*)\0*.*\0\0"
)
TITLE_AUDIO = "选择音频文件（wav / mp3 / flac / ogg / m4a）"

FILTER_IMAGE = (
    "图片文件\0*.png;*.jpg;*.jpeg;*.webp;*.bmp;*.tif;*.tiff\0"
    "PNG (*.png)\0*.png\0"
    "JPEG (*.jpg;*.jpeg)\0*.jpg;*.jpeg\0"
    "所有文件 (*.*)\0*.*\0\0"
)
TITLE_IMAGE = "选择参考图（可按住 Ctrl 一次多选）"


# 缩略图内存缓存：key = (路径, mtime, 大小, 边长)。
# 面板每次重画都会请求同一批图，不缓存的话每拖一下节点就重新解码一遍原图。
_THUMB_CACHE = {}
_THUMB_CACHE_MAX = 96


# ---------------------------------------------------------------- 文件选择框

def _ask_file_windows(filter_str=FILTER_PROMPT, title=TITLE_PROMPT, default_ext="txt"):
    """
    Windows 原生「打开文件」对话框。

    直接用 ctypes 调 comdlg32.GetOpenFileNameW —— ComfyUI 的 python_embeded
    是精简版，**不带 tkinter**，所以不能依赖它。

    @param {str} filter_str `\0` 分隔的过滤器串（见 FILTER_PROMPT / FILTER_AUDIO）
    @param {str} title 对话框标题
    @param {str} default_ext 用户没打扩展名时自动补的后缀
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
    ofn.lpstrFilter = filter_str
    # 注意：wintypes.LPWSTR 就是 ctypes.c_wchar_p，
    # cast 成 POINTER(c_wchar) 会得到 LP_c_wchar，赋值时报类型不兼容。
    ofn.lpstrFile = ctypes.cast(buf, ctypes.c_wchar_p)
    ofn.nMaxFile = 4096
    ofn.lpstrTitle = title
    ofn.lpstrDefExt = default_ext or ""
    # OFN_EXPLORER | OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_NOCHANGEDIR
    ofn.Flags = 0x00080000 | 0x00001000 | 0x00000800 | 0x00000008

    ok = ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn))
    if not ok:
        # 0 = 用户点了取消，或调用失败
        return ""
    return buf.value or ""


def _ask_images_windows(max_files=6):
    """
    Windows 原生「打开文件」对话框，**只能选图片、且支持一次多选**。

    为什么不复用 `_ask_file_windows`：多选的结果格式完全不一样 ——
    Windows 写的是 `目录\\0文件1\\0文件2\\0\\0`，`.value` 在第一个 \\0 就截断了，
    必须拿整个 buffer 再切。另外带 OFN_ALLOWMULTISELECT 时缓冲区不够会**静默丢文件**，
    所以这里给 32K。

    参考图一次要 6 张，分 6 次点、每次都重新导航到同一目录，实际用起来没人受得了。

    @returns {list} 选中的绝对路径，最多 `max_files` 个；取消返回空列表
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

    buf = ctypes.create_unicode_buffer(32768)
    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)

    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if hwnd:
        try:
            user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
    ofn.hwndOwner = hwnd or None

    ofn.lpstrFilter = FILTER_IMAGE
    ofn.lpstrFile = ctypes.cast(buf, ctypes.c_wchar_p)
    ofn.nMaxFile = 32768
    ofn.lpstrTitle = TITLE_IMAGE
    ofn.lpstrDefExt = ""
    # OFN_EXPLORER | OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_NOCHANGEDIR
    # | OFN_ALLOWMULTISELECT
    ofn.Flags = 0x00080000 | 0x00001000 | 0x00000800 | 0x00000008 | 0x00000200

    if not ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
        return []

    parts = [p for p in buf[:].split("\0") if p]
    if not parts:
        return []
    if len(parts) == 1:
        # 只选了一张：整条就是完整路径
        return parts[:1]
    folder, names = parts[0], parts[1:]
    picked = [os.path.join(folder, name) for name in names]
    return picked[: max(1, int(max_files))]


def _ask_images_tk(max_files=6):
    """非 Windows 兜底：tkinter 的多选对话框。"""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("当前环境没有 tkinter：%s" % e)

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        files = filedialog.askopenfilenames(
            title=TITLE_IMAGE,
            filetypes=[("图片文件", "*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff"), ("所有文件", "*.*")],
        )
        return list(files)[: max(1, int(max_files))]
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def _ask_file_tk(filter_str=FILTER_PROMPT, title=TITLE_PROMPT):
    """非 Windows 平台的兜底：有 tkinter 就用它。"""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as e:
        raise RuntimeError(f"当前环境没有 tkinter：{e}")

    # tkinter 要的是 ("说明", "*.wav *.mp3") 这种形式，和 Win32 的 \0 串不同。
    # 从过滤器串里把 *.ext 抽出来，拼成一格。
    patterns = []
    for chunk in (filter_str or "").split("\0"):
        chunk = chunk.strip()
        if chunk.startswith("*.") or chunk.startswith("*."):
            patterns.extend(p for p in chunk.split(";") if p)
    filetypes = [("支持的文件", " ".join(dict.fromkeys(patterns)) or "*.*"), ("所有文件", "*.*")]

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        return filedialog.askopenfilename(title=title, filetypes=filetypes) or ""
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


async def select_audio(request):
    """
    弹系统原生文件选择框（只列音频），返回选中的路径。

    和 select_file 只有过滤器不同 —— 共用 _dialog_busy，
    所以同时点「选提示词」和「选音频」也只会弹一个框。
    """
    global _dialog_busy
    if not _dialog_busy.acquire(blocking=False):
        return web.json_response({"path": "", "error": "已经有一个选择框开着，先关掉它"})

    try:
        if sys.platform == "win32":
            loop = asyncio.get_running_loop()
            path = await loop.run_in_executor(
                None, lambda: _ask_file_windows(FILTER_AUDIO, TITLE_AUDIO, "wav")
            )
        else:
            path = _ask_file_tk(FILTER_AUDIO, TITLE_AUDIO)
        return web.json_response({"path": path or ""})
    except Exception as e:
        return web.json_response({"path": "", "error": str(e)})
    finally:
        _dialog_busy.release()


async def select_images(request):
    """
    弹系统原生文件选择框（只列图片，**支持一次多选**），返回路径列表。

    body: {"max": 6} —— 最多收几张；不传按 6 张（音频分段节点的参考图槽位数）。

    多选是这个功能的刚需：6 张图分 6 次点、每次都要重新导航到同一目录，
    实际用起来没人受得了。_dialog_busy 和音频 / 提示词选择框共用 ——
    同时点两处也只会弹一个框。
    """
    global _dialog_busy
    if not _dialog_busy.acquire(blocking=False):
        return web.json_response({"paths": [], "error": "已经有一个选择框开着，先关掉它"})

    try:
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        try:
            limit = int(body.get("max") or 6)
        except (TypeError, ValueError):
            limit = 6
        limit = max(1, min(limit, 24))

        if sys.platform == "win32":
            loop = asyncio.get_running_loop()
            paths = await loop.run_in_executor(None, lambda: _ask_images_windows(limit))
        else:
            paths = _ask_images_tk(limit)
        return web.json_response({"paths": paths or []})
    except Exception as e:  # noqa: BLE001
        return web.json_response({"paths": [], "error": str(e)})
    finally:
        _dialog_busy.release()


async def audio_peaks(request):
    """
    解码音频并返回画波形用的峰值数组。

    body: {"path": "D:/a.wav", "buckets": 1200}
    返回: {"ok": true, "duration": 32.08, "sampleRate": 44100,
           "peaks": [0.0, 0.31, ...], "name": "a.wav", "path": "解析后的绝对路径"}
          失败 {"ok": false, "error": "..."}

    为什么由后端解码：浏览器解码各家格式行为不一致（mp3 时长有偏差、m4a 依赖系统编解码器），
    而后端本来就要用同一套波形去切音频 —— **画的和切的是同一份数据**，
    不会出现「波形上看着 5 秒、切出来 4.7 秒」。buckets 是画布取样点数，前端按宽度传。

    解码放在线程池：一两分钟的音频读盘 + 计算要几百毫秒，同步跑会卡住
    ComfyUI 的事件循环（进度条不动、界面假死）。
    """
    body = await _read_json_body(request)
    path = str(body.get("path") or "")
    try:
        buckets = int(body.get("buckets") or 1200)
    except (TypeError, ValueError):
        buckets = 1200
    buckets = max(64, min(buckets, 8000))

    def _work():
        from .audio_split import compute_peaks, resolve_audio_path

        resolved = resolve_audio_path(path)
        if not resolved:
            return {"ok": False, "error": "找不到音频文件：%s" % (path or "(没填路径)")}
        try:
            duration, rate, peaks, raw = compute_peaks(resolved, buckets)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": "这个音频读不出来：%s" % e}
        return {
            "ok": True,
            "duration": duration,
            "sampleRate": rate,
            "peaks": peaks,
            "rawPeak": raw,
            "name": os.path.basename(resolved),
            "path": resolved,
        }

    loop = asyncio.get_running_loop()
    payload = await loop.run_in_executor(None, _work)
    return web.json_response(payload)


async def audio_file(request):
    """
    把本机音频文件当 HTTP 资源吐出去（面板试听用）。

    query: path=<绝对路径 或 input 目录下的相对路径>
    返回: 二进制（aiohttp 的 FileResponse 自带 Range 支持，拖进度条才正常）

    为什么必须代理：面板里的 <audio> 拿不到本地文件路径，
    `file://` 在浏览器里也被拦；上传到 input 的可以用 /view，
    但从系统选择框选的是任意位置的绝对路径，只能由后端读出来转发。

    安全：只放行音频扩展名。这个接口只服务本机用户，但限制一下能少一堆误用
    （比如手滑把 .bat 的路径传进来）。
    """
    path = request.query.get("path", "")

    from .audio_split import AUDIO_EXTENSIONS, resolve_audio_path

    resolved = resolve_audio_path(path)
    if not resolved:
        return web.json_response({"ok": False, "error": "找不到音频文件：%s" % (path or "(空)")}, status=404)

    ext = os.path.splitext(resolved)[1].lower()
    if ext not in AUDIO_EXTENSIONS:
        return web.json_response({"ok": False, "error": "只允许音频文件：%s" % ext}, status=403)

    # FileResponse 会自己处理 Range / ETag / Content-Length
    return web.FileResponse(resolved)


def _thumb_bytes(path, size=192):
    """
    生成缩略图字节（JPEG）。失败返回 None。

    为什么服务端做：参考图可能是 6000×4000、20MB 的相机原图，面板上只画 56px。
    让浏览器去下原图，6 个槽位就是 100MB 的解码量，拖一下节点卡三秒。
    缓存按 (路径, mtime, 大小) 做 key —— 用户在外面换了同名文件时 mtime 会变，
    不会一直拿旧图。
    """
    import io

    try:
        stat = os.stat(path)
        key = (path, stat.st_mtime_ns, stat.st_size, int(size))
    except OSError:
        return None

    hit = _THUMB_CACHE.get(key)
    if hit:
        return hit

    try:
        from PIL import Image

        img = Image.open(path).convert("RGB")
        img.thumbnail((int(size), int(size)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=82)
        data = buf.getvalue()
    except Exception:  # noqa: BLE001
        return None

    if len(_THUMB_CACHE) > _THUMB_CACHE_MAX:
        _THUMB_CACHE.clear()
    _THUMB_CACHE[key] = data
    return data


async def image_file(request):
    """
    把本机图片当 HTTP 资源吐出去（面板缩略图 / 预览用）。

    query: path=<绝对路径 或 input 目录下的相对路径>
           thumb=1 → 返回 192px 的 JPEG 缩略图，不传就返回原图
    返回: 二进制

    和 audio_file 同款理由：面板里拿不到本地路径，`file://` 又被浏览器拦，
    从系统选择框选的是任意位置的绝对路径，只能由后端读出来转发。
    安全上只放行图片扩展名。
    """
    path = request.query.get("path", "")
    want_thumb = request.query.get("thumb", "") not in ("", "0", "false", "no")

    from .audio_split import IMAGE_EXTENSIONS, resolve_image_path

    resolved = resolve_image_path(path)
    if not resolved:
        return web.json_response(
            {"ok": False, "error": "找不到图片：%s" % (path or "(空)")}, status=404
        )

    ext = os.path.splitext(resolved)[1].lower()
    if ext not in IMAGE_EXTENSIONS:
        return web.json_response(
            {"ok": False, "error": "只允许图片文件：%s" % ext}, status=403
        )

    if want_thumb:
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, _thumb_bytes, resolved)
        if data:
            return web.Response(
                body=data,
                content_type="image/jpeg",
                headers={"Cache-Control": "max-age=300"},
            )

    # FileResponse 会自己处理 Range / ETag / Content-Length
    return web.FileResponse(resolved)


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
    清掉某个节点的计数，或者把计数设到指定位置。

    前端两种按钮都用它：
      - 「重置计数」/「↺ 回到第 1 段」：不带 index，等价于 index=0
      - 音频分段的「跳到第 N 段」：带 index = N-1（顺序游标是 0 起的下一条下标）

    节点从来没有执行过时 state.json 里没有它的记录 —— 这种情况等同于
    「计数本来就是 0」，直接返回成功，前端不必弹错误框。
    body 里不带 node 时表示清空全部记录。

    三个坑（都踩过）：
      1. 请求体畸形时绝不能落到「清空全部」分支 —— 否则任何一个坏请求
         都会把所有节点的进度抹掉。畸形请求一律当「没做事」返回成功。
      2. 读-改-写必须和 nodes.load() 共用 _state_lock。不然会出现
         load 读(index=3) → reset 写 0 → load 写回 4，重置被静默吞掉：
         界面提示「已重置」，下次却仍从下一行开始。
      3. index 要显式校验。前端传个 "abc" 或负数时不能用默认值把人家
         辛苦跑出来的进度悄悄清成 0 —— 宁可报错让用户看见。
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

    # 没给 index 就当 0（老的「重置计数」调用方都不带这个字段）
    target = 0
    if "index" in body:
        raw_index = body.get("index")
        try:
            target = int(raw_index)
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "index 必须是整数"})
        if target < 0:
            return web.json_response({"ok": False, "error": "index 不能是负数"})

    from .nodes import _load_state, _save_state, _state_lock, set_index

    if node:
        set_index(node, target)      # 自带锁 + 脏数据保护
    else:
        with _state_lock:
            _save_state({})

    if node:
        return web.json_response({"ok": True, "index": target})
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
    # 音频分段节点用：选音频 + 波形峰值
    _ROUTES.post("/dogking-tools/select_audio")(select_audio)
    _ROUTES.post("/dogking-tools/audio_peaks")(audio_peaks)
    _ROUTES.get("/dogking-tools/audio_file")(audio_file)
    # 参考图：多选对话框 + 缩略图/原图代理
    _ROUTES.post("/dogking-tools/select_images")(select_images)
    _ROUTES.get("/dogking-tools/image_file")(image_file)
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
