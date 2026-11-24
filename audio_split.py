"""
dogking-tools —— 数字人音频分段

一个节点管完整件事：选音频 → 看波形 → 切段 → 给每段配提示词 → 按段吐给下游。

--------------------------------------------------------------------------
它解决的问题
--------------------------------------------------------------------------
数字人视频的时长是**按段**来的（模型一次只吃几秒音轨），但音频往往是一整段口播
或一首歌。以前要在外面用「音频裁剪」节点反复改起止时间、一次次重跑，切 10 段就是
10 次手工操作。这个节点把分段表存在节点里，一次配好，之后按顺序自动跑完。

--------------------------------------------------------------------------
三段数据流
--------------------------------------------------------------------------
1. **分段表**（前端画波形、写提示词）→ 序列化成 JSON 存进 `分段数据` widget。
   格式：`{"segments": [{"start": 0.0, "end": 5.2, "prompt": ""}, ...]}`
   `start` / `end` 是**秒**（浮点），前端负责时间换算。
2. **音频**：优先用 `音频` 输入口（上游给的 AUDIO），没有就用 `文件路径` 读本机文件。
3. **提示词**：每段自己的提示词优先，空着就回退 `全局提示词` —— 这就是「独立提示词
   覆盖全局」的实现，覆盖逻辑放在后端，前端只管把两栏都填进 JSON。

   输出侧**只有一个「提示词」口**，用户不用自己判断该接哪个：这个口吐出来的永远是
   「该段最终该用的词」，有独立词就是独立词，没有才是全局词。
   （早先还单独开过一个「全局提示词」口，实测只会让人接错 —— 接它等于把段内词全顶掉，
   正好把覆盖功能废了。）

--------------------------------------------------------------------------
两种输出模式
--------------------------------------------------------------------------
- **单段**：`当前段` 指哪段就出哪段，不推进游标。适合调试某一段。
- **顺序**：按分段表顺序每次出下一段，游标存在 `state.json` 里（按节点 id），
  跑完一轮自动归零。配合 `自动连续` 会在每次执行后发事件，让前端自动排下一次 ——
  和插件里「提示词批量加载」用的是同一套机制，只是事件名不同
  （`dogking-tools.audio.next`），避免两个节点互相触发。

  游标的重置条件是**分段的时间轴变了**（切点增删改），而不是每执行一次都重置。
  只改某段的提示词不会重置游标 —— 否则用户改一个错字就得从头再跑一遍。

--------------------------------------------------------------------------
「段序号」和「段数」不是一回事
--------------------------------------------------------------------------
- **段序号**：这次吐出来的是第几段（1 起），单段模式下就是 `当前段` 那个数，
  顺序模式下跟着游标走。
- **段数**：**这次要产出几段** —— 不是切了多少段。单段模式恒为 1（一次只吐一段），
  顺序模式才是总段数。拿它当循环次数 / 批量上限时，单段不该被「切了 8 段」牵连。

  面板状态行上的 `第 3 / 8 段` 用的是真实总段数（给人看的），和这里这个输出口无关。

--------------------------------------------------------------------------
几个必须守住的点
--------------------------------------------------------------------------
1. **切点越界一律裁剪**，不报错：前端拖到音频末尾之外是常事，裁到文件长度即可。
2. **start >= end 的段直接丢掉**，不抛异常 —— 前端正在拖动时中间状态经常是这样。
3. 没有分段 / 没有音频时给**中文错误**，说清去哪儿点。
4. 切的都是 `clone()` 出来的新张量，不动上游 waveform。
"""

import hashlib
import json
import os

try:  # 作为包被 ComfyUI 导入
    from .nodes import _load_state, _save_state, _state_lock
except ImportError:  # 独立测试环境
    import threading

    _state_lock = threading.Lock()
    _MEM_STATE = {}

    def _load_state():
        return dict(_MEM_STATE)

    def _save_state(data):
        _MEM_STATE.clear()
        _MEM_STATE.update(data)


MODE_SINGLE = "单段"
MODE_SEQUENCE = "顺序"
MODES = [MODE_SINGLE, MODE_SEQUENCE]

# 顺序模式执行完发的事件。**不能**复用 `dogking-tools.next`：
# 那个是「提示词批量加载」在用，两个节点同时存在时前端会连续排两次队列。
EVENT_NEXT = "dogking-tools.audio.next"

# 没有分段时的默认提示
EMPTY_HINT = "还没有分段：点面板上的「自动等分」，或在波形上点一下加切点"

# 允许的音频扩展名。选文件框、波形接口、试听代理共用这一份 ——
# 三处各写一遍必然有一处漏掉某种格式。
AUDIO_EXTENSIONS = (".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".wma")

# 允许的参考图扩展名。前后端共用：后端挑图时过滤，缩略图接口也照这个放行。
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")

# 参考图槽位数。**必须和前端 MAX_REFS 一致** —— 后端决定返回几个图片口，
# 前端决定画几个槽位；不一致就会出现「状态说 6 张，节点上只有 5 个口」。
MAX_REFS = 6

# 短边限制的默认值和红线。
# 为什么是「短边」而不是「宽」或「高」：同一个工作流里既可能喂竖图也可能喂横图，
# 按短边缩放才能保证两种图放进来尺寸量级一致，又不会把某一边撑到爆显存。
REF_SHORT_EDGE_DEFAULT = 1024
# 长边硬上限。6 张 4K 图一起进 VAE 会直接把显存吃干净，超过这个就再缩一档。
REF_MAX_SIDE = 4096

# H3 生成固定 24fps。帧数 ↔ 秒的换算全走这一个常量，别在各处再写 24。
FPS = 24

# 引导帧只认 17k+5（5 / 22 / 39 / 56…），其余值下游会悄悄往下取整。
GUIDE_STEP = 17
GUIDE_BASE = 5


def align_guide_frames(n):
    """
    把任意帧数向下对齐到 17k+5，最小 5。

    为什么要在这里对齐：同一个数要在三个地方用 —— 取上一段末几帧当引导、画面裁掉
    开头几帧、音频往前多带几帧。只要有一处还是原始值（比如填了 17 而下游偷偷降到 5），
    音画就会错位。所以由这个节点吐出去的永远是**对齐后**的数。
    """
    n = int(n)
    if n <= 0:
        return 0          # 0 = 不续接，别被下限抬成 5
    n = max(GUIDE_BASE, n)
    return n - (n - GUIDE_BASE) % GUIDE_STEP


def segment_frame_plan(duration, overlap, has_prev):
    """
    一段的帧数账本。以前这是工作流里 4 个数学节点在算，现在收在这里。

    @param {float} duration 本段真实时长（秒，不含前置）
    @param {int} overlap 续接帧数（已对齐）
    @param {bool} has_prev 是否有上一段（段 1 / 单段模式为 False）
    @returns {(int, int, int, float)} (生成长度, 有效帧数, 画面裁剪起点, 音频起点秒)
    """
    # 生成侧：本段帧数对齐到 17k+5
    seg_frames = max(GUIDE_BASE, round(duration * FPS))
    seg_frames += (GUIDE_BASE - seg_frames % GUIDE_STEP) % GUIDE_STEP

    # 有上一段时，前面要塞进 N 帧引导，生成长度要一起加长再对齐
    gen_length = seg_frames + (overlap if has_prev else 0)
    gen_length += (GUIDE_BASE - gen_length % GUIDE_STEP) % GUIDE_STEP

    # 成片只保留「本段真实时长」这么多帧，引导帧全部裁掉
    valid_frames = int(duration * FPS)
    crop_start = overlap if has_prev else 0
    audio_start = (overlap / float(FPS)) if has_prev else 0.0
    return gen_length, valid_frames, crop_start, audio_start


# ---------------------------------------------------------------- 分段表

def parse_segments(raw):
    """
    把前端写的分段 JSON 解析成干净的段列表。

    容错优先：这是前端实时写进来的，拖动过程中会短暂出现 `start >= end`、
    `null`、字符串数字等中间状态，全部当「这段还没成形」丢掉，不抛异常。

    @param {str|dict|list} raw `{"segments":[...]}` / 数组 / JSON 字符串
    @returns {list} `[{"start": float, "end": float, "prompt": str}]`，按 start 升序
    """
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, (list, tuple)):
        data = list(raw)
    else:
        try:
            data = json.loads(str(raw or "").strip() or "{}")
        except Exception:
            data = {}

    if isinstance(data, dict):
        segments = data.get("segments")
    else:
        segments = data
    if not isinstance(segments, (list, tuple)):
        return []

    out = []
    for item in segments:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item.get("start"))
            end = float(item.get("end"))
        except (TypeError, ValueError):
            continue
        if not (end > start):
            continue

        # 每段的自定义参考图。空列表 / 缺字段都表示「沿用全局」，
        # 所以这里统一收敛成 list，不留 None 给下游判断。
        raw_images = item.get("images")
        images = []
        if isinstance(raw_images, (list, tuple)):
            for entry in raw_images:
                text = str(entry or "").strip()
                if text and text not in images:
                    images.append(text)

        out.append({
            "start": max(0.0, start),
            "end": end,
            "prompt": str(item.get("prompt") or "").strip(),
            "images": images[:MAX_REFS],
        })
    out.sort(key=lambda seg: (seg["start"], seg["end"]))
    return out


def timeline_signature(segments):
    """
    分段「时间轴」的指纹，用来判断游标要不要重置。

    故意**不包含提示词**：用户改某段提示词时游标应该保持不动，
    否则改一个错字就得从第一段重新跑。
    """
    joined = "|".join("%.3f-%.3f" % (seg["start"], seg["end"]) for seg in segments)
    return hashlib.md5(("%d|" % len(segments) + joined).encode("utf-8")).hexdigest()[:12]


def project_key(node_key, audio_path=""):
    """
    顺序游标的 key：**按「节点 + 音频」隔离**，不同项目各走各的。

    为什么不能只用节点 id：同一个分段节点上，用户会换一段音频去做另一个项目。
    排队跑的时候两个项目共用一条游标，就会互相推 —— A 跑完第 3 段、B 的 job 一执行
    就接着记成「B 的第 4 段」，A 剩下的段全乱，最后合并报缺段。

    用音频文件名区分，而不是再加一个「项目名」参数：换项目必然换音频，这是最自然的
    区分，用户不用多填一个东西。用「音频」输入口时拿不到路径，退回节点级 ——
    那种接法本来就只有一个音频源，不存在多项目混跑。
    """
    text = str(audio_path or "").strip().strip('"')
    base = os.path.splitext(os.path.basename(text))[0] if text else ""
    if not base:
        return str(node_key or "0")
    return "%s|%s" % (node_key or "0", base)


def execution_signature(file_path, segments):
    """
    这一轮执行用的「项目指纹」：`音频文件名|段数`。

    给前端做一件事：自动连续要排下一段之前，先比对界面上的参数还是不是刚跑的这一套。
    用户跑到一半换项目（换音频）、或重新等分之后，指纹就变了 —— 这时**不能**自动排，
    否则会拿新项目的参数去跑「上一段的下一段」，两个项目互相污染。

    只取文件名和段数，不带时间：浮点数两端格式化会有极小的差异（`%.3f` 与 `toFixed`），
    那会让自动连续永远排不上，比不比对还糟。
    """
    base = os.path.basename(str(file_path or "").strip().strip('"'))
    return "%s|%d" % (base, len(segments or []))


def resolve_prompt(segment, global_prompt):
    """
    该段的提示词：自己写了就用自己，空着回退全局。

    前后空白会先 strip —— 前端输入框里点一下再点走，很容易留下一个空格，
    那不该被当成「这段有独立提示词」。
    """
    own = str(segment.get("prompt") or "").strip()
    if own:
        return own
    return str(global_prompt or "").strip()


# ---------------------------------------------------------------- 参考图

def parse_ref_config(raw):
    """
    解析「参考图数据」—— 全局参考图 + 短边限制。

    格式：`{"short": 1024, "images": ["C:/a.png", ...]}`

    容错优先：这是前端实时写进来的，可能还没初始化；老工作流里这个字段干脆不存在。
    任何解析失败都退化成「没有参考图 + 默认短边」，绝不抛异常 —— 参考图是可选项，
    不该因为它把整条片子卡死。

    @returns {(int, list)} (短边限制, 全局参考图路径列表)
    """
    data = {}
    try:
        data = json.loads(str(raw or "").strip() or "{}")
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}

    try:
        short = int(data.get("short", REF_SHORT_EDGE_DEFAULT))
    except (TypeError, ValueError):
        short = REF_SHORT_EDGE_DEFAULT
    short = max(0, min(short, 8192))

    images = data.get("images")
    if not isinstance(images, (list, tuple)):
        images = []
    picked = []
    for item in images:
        text = str(item or "").strip()
        if text and text not in picked:
            picked.append(text)
    return short, picked[:MAX_REFS]


def resolve_ref_paths(segment, global_images):
    """
    这一段该用哪几张参考图。

    **段内有图就整组覆盖全局**，不是逐张合并 —— 用户的语义是「这一段换一批参考」。
    逐张合并会造出「换掉第 2 张、另外 5 张还是全局的」这种半换状态，对接镜头切换
    毫无用处，而且完全没法预期。

    @returns {(list, str)} (路径列表, 来源 `segment` / `global` / 空串)
    """
    own = segment.get("images") if isinstance(segment, dict) else None
    if isinstance(own, (list, tuple)):
        picked = []
        for item in own:
            text = str(item or "").strip()
            if text and text not in picked:
                picked.append(text)
        if picked:
            return picked[:MAX_REFS], "segment"

    kept = [str(p).strip() for p in (global_images or []) if str(p or "").strip()]
    if kept:
        return kept[:MAX_REFS], "global"
    return [], ""


def resolve_image_path(path):
    """
    参考图路径 → 存在的绝对路径。

    和 `resolve_audio_path` 同款：绝对路径直接用，相对路径按 ComfyUI `input/`
    目录解析（拖进节点上传的图就落在那里）。找不到返回空串。
    """
    text = str(path or "").strip().strip('"')
    if not text:
        return ""
    if os.path.isfile(text):
        return text

    try:
        import folder_paths

        candidate = folder_paths.get_annotated_filepath(text)
        if candidate and os.path.isfile(candidate):
            return candidate
        candidate = os.path.join(folder_paths.get_input_directory(), text)
        if os.path.isfile(candidate):
            return candidate
    except Exception:
        pass
    return ""


def load_reference_image(path, short_edge):
    """
    读一张参考图 → IMAGE 张量 `[1,H,W,3]`（float32，0~1）。

    短边限制：`short_edge > 0` 就把**短边**缩到该值、长边等比。**不裁切、不补边** ——
    裁切会切掉内容，补边会引入黑条，两者都会被 VAE 当成画面内容编码进去。放大也做：
    1200 的短边配 1024 的限制时缩小，800 的短边配 1024 就放大，行为一致才好预期。

    长边另有 4096 的硬上限：6 张 4K 一起进来会直接把显存吃干净。

    @returns {(torch.Tensor|None, str)} (张量, 成功是 "宽x高"，失败是原因)
    """
    resolved = resolve_image_path(path)
    if not resolved:
        return None, "找不到文件"

    ext = os.path.splitext(resolved)[1].lower()
    if ext not in IMAGE_EXTENSIONS:
        return None, "不是图片（%s）" % (ext or "无扩展名")

    try:
        import numpy as np
        import torch
        from PIL import Image
    except Exception as e:  # noqa: BLE001
        return None, "缺少图像库：%s" % e

    try:
        img = Image.open(resolved)
        img = img.convert("RGB")
        w, h = img.size
        if w < 1 or h < 1:
            return None, "尺寸为 0"

        scale = 1.0
        target = int(short_edge or 0)
        if target > 0:
            scale = target / float(min(w, h))
        long_side = max(w, h) * scale
        if long_side > REF_MAX_SIDE:
            scale *= REF_MAX_SIDE / float(long_side)

        if abs(scale - 1.0) > 1e-3:
            nw = max(1, int(round(w * scale)))
            nh = max(1, int(round(h * scale)))
            img = img.resize((nw, nh), Image.LANCZOS)

        arr = np.asarray(img, dtype=np.float32) / 255.0
    except Exception as e:  # noqa: BLE001
        return None, "读不出来：%s" % e

    # PIL 的调色板 / 灰度已经在 convert("RGB") 时统一，这里一定是 3 通道
    tensor = torch.from_numpy(np.ascontiguousarray(arr))[None, ...]
    return tensor, "%dx%d" % (img.size[0], img.size[1])


def build_reference_outputs(segment, ref_config, limit=MAX_REFS):
    """
    把一段的参考图准备成 `limit` 个输出值（不足补 None）。

    为什么补 None 而不是灰图：这些口只有连着线时才会被下游取用，没连线时 ComfyUI
    根本不读它。补灰图反而会把「没设第 3 张」变成「第 3 张是一张灰片」，静悄悄地
    污染生成结果 —— 那比报错难查得多。

    @returns {(tuple, str, list)} (输出元组, 状态描述, 每张图的尺寸说明)
    """
    short_edge, global_images = parse_ref_config(ref_config)
    paths, source = resolve_ref_paths(segment, global_images)

    tensors = []
    sizes = []
    failed = []
    for path in paths:
        tensor, info = load_reference_image(path, short_edge)
        if tensor is None:
            failed.append("%s %s" % (os.path.basename(str(path)), info))
            continue
        tensors.append(tensor)
        sizes.append(info)

    if not paths:
        note = ""
    elif source == "segment":
        note = " · 参考图 %d 张（本段覆盖）" % len(tensors)
    else:
        note = " · 参考图 %d 张（全局）" % len(tensors)
    if failed:
        note += " · 失败 %d 张：%s" % (len(failed), "；".join(failed[:2]))

    tensors = tensors[:limit]
    outs = tuple(tensors) + (None,) * max(0, limit - len(tensors))
    return outs, note, sizes


# ---------------------------------------------------------------- 音频

def resolve_audio_path(path):
    """
    把用户给的路径解析成真实文件路径。

    两种来源都要认：
      · 系统选择框给的**绝对路径**
      · 前端上传到 ComfyUI `input/` 后的**相对路径**（`voice.wav` 或 `sub/voice.wav`）

    @returns {str} 存在的绝对路径；找不到返回空串
    """
    text = str(path or "").strip().strip('"')
    if not text:
        return ""
    if os.path.isfile(text):
        return text

    try:
        import folder_paths

        candidate = folder_paths.get_annotated_filepath(text)
        if candidate and os.path.isfile(candidate):
            return candidate
        candidate = os.path.join(folder_paths.get_input_directory(), text)
        if os.path.isfile(candidate):
            return candidate
    except Exception:
        pass
    return ""


def load_waveform(path):
    """
    读音频文件 → `(waveform[C,L], sample_rate)`。

    🔴 **绝不要用 `torchaudio.load()`。** 这台机器的 torchaudio 2.10 把解码全部交给
    torchcodec，而 `python_embeded` 里那个 torchcodec 的 DLL 和 torch 版本对不上 ——
    一调用就弹 Windows 的「无法定位程序输入点 torch_dtype_float8_e8m0fnu 于
    libtorchcodec_image.dll」，那个模态框不点掉，请求线程就一直卡着，
    面板永远停在「解码中…」。

    所以解码走这条链，全是纯 Python / 自带二进制，不碰 torch 的音频后端：
        ① soundfile（libsndfile）—— wav / flac / ogg / mp3，最快
        ② PyAV（ffmpeg 绑定）—— m4a / mp4 / aac / webm 这些容器，视频里抽音轨也行
        ③ 标准库 wave —— 前两个都没装时，至少 wav 还能读

    @returns {(torch.Tensor, int)} waveform 形状 [C, L]，float32
    @throws {ValueError} 三种方式都读不出来
    """
    import numpy as np
    import torch

    errors = []

    # ① soundfile
    try:
        import soundfile as sf

        data, sr = sf.read(path, dtype="float32", always_2d=True)  # [L, C]
        if data.size:
            return torch.from_numpy(np.ascontiguousarray(data.T)), int(sr)
        errors.append("soundfile: 文件里没有采样点")
    except Exception as e:  # noqa: BLE001
        errors.append("soundfile: %s" % e)

    # ② PyAV
    try:
        wav, sr = _load_with_av(path)
        return wav, sr
    except Exception as e:  # noqa: BLE001
        errors.append("pyav: %s" % e)

    # ③ 标准库 wave
    try:
        import wave

        with wave.open(path, "rb") as fh:
            channels = fh.getnchannels()
            width = fh.getsampwidth()
            sr = fh.getframerate()
            raw = fh.readframes(fh.getnframes())

        dtype = {1: np.uint8, 2: np.int16, 4: np.int32}.get(width)
        if dtype is None:
            raise ValueError("不支持的 wav 位深：%s 字节" % width)
        data = np.frombuffer(raw, dtype=dtype).astype(np.float32)
        if dtype is np.uint8:
            data = (data - 128.0) / 128.0
        else:
            data = data / float(np.iinfo(dtype).max)
        if channels > 1:
            data = data.reshape(-1, channels).T
        else:
            data = data.reshape(1, -1)
        return torch.from_numpy(np.ascontiguousarray(data)), int(sr)
    except Exception as e:  # noqa: BLE001
        errors.append("wave: %s" % e)

    raise ValueError("解码失败（%s）" % "；".join(errors))


def _load_with_av(path):
    """
    用 PyAV 解码任意容器 → `(waveform[C,L], sample_rate)`。

    先交给 AudioResampler 转成 `fltp`（planar float32）：容器里的原始格式可能是
    s16 / flt / packed 交错，形状随格式变，直接 `to_ndarray()` 出来的布局不一致，
    重采样器统一之后拿到的永远是 [C, L]。
    """
    import av
    import numpy as np
    import torch

    with av.open(path) as container:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise ValueError("这个文件里没有音频轨")

        rate = int(stream.rate or 0)
        layout = stream.layout.name or "stereo"
        resampler = av.AudioResampler(format="fltp", layout=layout, rate=rate)

        blocks = []
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                blocks.append(out.to_ndarray())
        for out in resampler.resample(None):  # 冲掉尾部缓冲
            blocks.append(out.to_ndarray())

    if not blocks:
        raise ValueError("解码出来是空的")
    data = np.concatenate(blocks, axis=1).astype(np.float32, copy=False)
    return torch.from_numpy(np.ascontiguousarray(data)), rate


def to_comfy_audio(waveform, sample_rate):
    """统一成 ComfyUI 的 AUDIO 结构：`{"waveform": [B,C,L], "sample_rate": int}`。"""
    import torch

    if not torch.is_tensor(waveform):
        waveform = torch.as_tensor(waveform)
    if waveform.dim() == 1:            # [L]
        waveform = waveform.unsqueeze(0).unsqueeze(0)
    elif waveform.dim() == 2:          # [C,L]
        waveform = waveform.unsqueeze(0)
    return {"waveform": waveform, "sample_rate": int(sample_rate)}


def audio_duration(audio):
    """AUDIO 的时长（秒）。拿不到就返回 0。"""
    try:
        length = int(audio["waveform"].shape[-1])
        rate = int(audio["sample_rate"])
        return length / float(rate) if rate > 0 else 0.0
    except Exception:
        return 0.0


def parse_range(raw):
    """
    从分段 JSON 里取「选区」——波形上最外侧那两条边界之间的一段。

    选区决定的是「整条音频要哪一截」，和怎么分段无关。单独解析它，是因为下游
    「分段视频合并」最后要拿**整条**音轨压进成片，而「音频」口只吐当前段。

    @returns {tuple|None} `(start, end)` 秒；老工作流没有 range 字段时返回 None
    """
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, (list, tuple)):
        return None
    else:
        try:
            data = json.loads(str(raw or "").strip() or "{}")
        except Exception:
            return None

    if not isinstance(data, dict):
        return None
    rng = data.get("range")
    if not isinstance(rng, dict):
        return None
    try:
        start = float(rng.get("start"))
        end = float(rng.get("end"))
    except (TypeError, ValueError):
        return None
    if not (end > start):
        return None
    return max(0.0, start), end


def slice_audio(audio, start, end):
    """
    按秒切一段出来。

    越界一律裁剪（拖到末尾之外很常见）；切出来是 `clone()`，
    下游改它不会污染上游波形。
    """
    rate = int(audio["sample_rate"])
    waveform = audio["waveform"]
    total = int(waveform.shape[-1])

    begin = max(0, min(total, int(round(float(start) * rate))))
    stop = max(begin, min(total, int(round(float(end) * rate))))
    if stop <= begin:
        raise ValueError(
            "第 %.2f–%.2f 秒切出来是空的（音频总长 %.2f 秒）" % (start, end, total / float(rate or 1))
        )
    return {"waveform": waveform[..., begin:stop].clone(), "sample_rate": rate}


def compute_peaks(path, buckets=1200):
    """
    给前端画波形用的峰值数组。

    做法：把单声道化后的波形分成 `buckets` 个桶，每桶取绝对值最大，再按全局峰值归一化。
    桶数固定（不是按秒采样）—— 前端画布宽度在变，固定桶数能保证画出来永远是满的。

    @returns {(float, int, list, float)} (时长秒, 采样率, 峰值 0~1, 原始峰值)
    @throws {ValueError} 文件读不出来或没有样本
    """
    import torch

    waveform, rate = load_waveform(path)
    if waveform.dim() == 3:                 # [B,C,L] → 第一个 batch
        waveform = waveform[0]
    if waveform.dim() == 2:                 # [C,L]
        waveform = waveform.mean(dim=0)
    if waveform.numel() == 0:
        raise ValueError("这个文件是空的，没有可画的波形")

    total = int(waveform.shape[-1])
    count = max(1, min(int(buckets), total))
    chunk = total // count
    trimmed = waveform[: chunk * count].reshape(count, chunk).abs().amax(dim=1)
    raw_peak = float(trimmed.max()) if trimmed.numel() else 0.0
    scale = raw_peak if raw_peak > 0 else 1.0
    peaks = (trimmed / scale).clamp(0.0, 1.0).tolist()

    return total / float(rate), rate, peaks, raw_peak


# ================================================================ 节点

class DogkingAudioSplit:
    """
    数字人音频分段：波形上切段、每段配提示词，按段输出音频 + 提示词。

    面板（前端）负责波形、切点、每段提示词；节点只做「按游标取一段 + 切片 + 合并提示词」。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                # 前端把分段表写进这里（widget 被改成隐藏类型，节点上看不到，
                # 但工作流保存时会带上，所以每段的提示词不会丢）。
                # 🔴 multiline 必须是 False。面板把它改成隐藏类型，但 ComfyUI 在
                # **创建节点时**就按「多行文本」量了一次高度 —— 分段表是一整串 JSON，
                # 6 段 300 多字符按控件宽度折行能折出十几行，节点顶部直接多出 366px
                # 空白（实测），下面所有内容被挤掉。前端事后改属性也不会重算，
                # 只有让定义层就是单行，布局才是 20px 一行。
                "分段数据": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "dynamicPrompts": False,
                    "tooltip": "内部字段：面板生成的 {segments:[{start,end,prompt}]}，不用手改",
                }),
                "全局提示词": ("STRING", {
                    "default": "",
                    "multiline": True,
                    "dynamicPrompts": False,
                    "tooltip": "所有段共用的提示词；某段自己写了提示词时以该段为准",
                }),
                "输出模式": (MODES, {
                    "default": MODE_SEQUENCE,
                    "tooltip": "单段 = 只出「当前段」；顺序 = 每次出下一段，跑完自动归零",
                }),
                "当前段": ("INT", {
                    "default": 1, "min": 1, "max": 999, "step": 1,
                    "tooltip": "单段模式下生效；顺序模式忽略它",
                }),
                "自动连续": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "顺序模式下每跑完一段自动排下一段，直到全部跑完",
                }),
                "文件路径": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "本机音频文件；接了「音频」输入口时以输入口为准",
                }),
                "续接帧数": ("INT", {
                    "default": 0, "min": 0, "max": 96, "step": 1,
                    "tooltip": "续接帧数 —— 全链唯一要填的数（24fps）。它同时决定："
                               "① 取上一段末几帧当引导；② 画面裁掉开头几帧；③ 音频往前多带几帧。"
                               "三个动作必须同值，所以收成一个参数，走「续接帧数」输出口往下发。"
                               "自动对齐到 17k+5（5 / 22 / 39 / 56…）。默认 0 = 不续接。"
                               "39 帧方案填 39",
                }),
            },
            "optional": {
                # 全局参考图 + 短边限制。被面板改成隐藏类型，节点上不占 widget 行：
                # 存 `{"short": 1024, "images": ["C:/a.png", ...]}`，
                # 面板把它画成 6 个缩略图槽位 + 一个「短边」输入框。
                #
                # 🔴 位置规则（2026-11-24 实测，别动）：
                # **它必须是最后一个「有 widget 的参数」，后面只能跟 forceInput。**
                #
                # 存盘和读盘用的不是同一套顺序：
                #   存盘 = 按 widget 顺序写 widgets_values（DOM widget 也算一格）
                #   读盘 = 按 INPUT_TYPES 的参数顺序往回填（连 forceInput 的「音频」也占一格）
                # 只要两边错开一位，参考图 JSON 就被填进「音频」——它没有 widget，
                # 值直接丢弃；「参考图数据」读到的是空格位。表现就是
                # **一切换工作流，参考图全没了**（面板显示 6 个空槽）。
                "参考图数据": ("STRING", {
                    "default": "",
                    # 同理：单行。这个值虽然短（一两百字符），但一旦上了 6 张图
                    # 路径能到 400 字符，多行一样会撑出空白
                    "multiline": False,
                    "dynamicPrompts": False,
                    "tooltip": "内部字段：面板生成的 {short:1024, images:[路径…]}，不用手改",
                }),
                # 🔴 「音频」必须排在参考图数据**后面**：它 forceInput、没有 widget，
                # 只在读盘时占一格。让它去接存盘时 DOM widget 留下的那个空串，
                # 两边下标就对齐了（参考图落在第 8 位，音频吃第 9 位的空串）。
                "音频": ("AUDIO", {
                    "forceInput": True,
                    "tooltip": "上游给的音频；接了就用它，忽略「文件路径」",
                }),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    # 输出里**只有一个**「提示词」口：段内有独立词就出独立词，没有就出全局词。
    # 不再单独开「全局提示词」口 —— 两个口摆在一起，用户不知道接哪个，
    # 接错了还会把全局词无脑顶掉段内词，等于把「覆盖」功能绕过去了。
    #
    # 后 6 个口是「分段参数」：帧数账本原来由 5 个数学节点分头算（本段帧数 / 生成长度 /
    # 有效帧数 / 画面裁剪起点 / 音频起点），改一次帧数要同步 5 处，漏一处就音画错位。
    # 收进这里之后全链只有一个「续接帧数」，其它全是它的派生值。
    #
    # 最后 6 个是参考图：每段吐当前该用的那几张（段内有图就覆盖全局），不足补 None。
    # 追加在**尾部**是硬要求 —— 前端连线按输出下标存，插在中间会把已有连线整体错位。
    RETURN_TYPES = (
        "AUDIO", "STRING", "INT", "INT", "FLOAT", "AUDIO",
        "INT", "INT", "INT", "FLOAT", "INT", "BOOLEAN",
    ) + ("IMAGE",) * MAX_REFS
    RETURN_NAMES = (
        "音频", "提示词", "段序号", "段数", "本段时长", "选区音频",
        "生成长度", "有效帧数", "裁剪起点", "音频起点", "续接帧数", "有上一段",
    ) + tuple("图%d" % (i + 1) for i in range(MAX_REFS))
    FUNCTION = "split"
    CATEGORY = "dogking-tools"
    DESCRIPTION = (
        "数字人音频分段：波形上自由切段，每段可写独立提示词（覆盖全局提示词）、"
        "也可单独指定参考图（覆盖全局参考图）；支持单段输出与顺序输出"
        "（配合自动连续按段跑完整个音频）。"
    )

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        # 顺序模式的游标是外部状态（state.json），输入没变也必须重新执行，
        # 否则 ComfyUI 会拿上次的缓存 —— 表现为「每次都吐同一段」。
        return float("NaN")

    def split(self, 分段数据, 全局提示词, 输出模式, 当前段, 自动连续, 文件路径,
              续接帧数=0, 参考图数据="", 音频=None, unique_id=None):
        key = str(unique_id or "0")

        segments = parse_segments(分段数据)
        if not segments:
            raise ValueError(EMPTY_HINT)

        audio = self._resolve_audio(音频, 文件路径)
        total = len(segments)

        # 「选区音频」= 整条选区（裁掉头尾之后），不是某一段。
        # 下游「分段视频合并」最后要拿整条音轨压进成片，而「音频」口只给当前段 ——
        # 用户在面板上传的音频 + 框的选区都在这个节点里，外面拿不到，所以从这里吐一份。
        # 老工作流没有 range 字段时直接给整条（和以前的行为一致）。
        sel_range = parse_range(分段数据)
        if sel_range:
            try:
                选区音频 = slice_audio(audio, sel_range[0], sel_range[1])
            except ValueError:
                选区音频 = audio      # 选区越界（音频比记录短）就退回整条，别让节点红
        else:
            选区音频 = audio

        if 输出模式 == MODE_SINGLE:
            index = max(1, min(int(当前段), total)) - 1
            more = False
            # 「段数」口报的是**这次要产出几段**，不是切了多少段。
            # 单段模式一次只吐一段，所以这里是 1 —— 拿它当循环次数 / 批量上限时，
            # 单段不该被别人切了 8 段牵连。
            out_total = 1
        else:
            # 游标按「节点 + 音频」隔离：同一个节点上换音频做另一个项目，
            # 两边进度各走各的。第二个 key 是老格式的兜底（见 _next_index）。
            index, more = self._next_index(
                [project_key(key, 文件路径), key], segments, total
            )
            out_total = total

        segment = segments[index]

        # 段 2 以后把音频往前多带 `续接帧数` 帧（上一段的尾巴）。
        #
        # 为什么必须这么做：画面那头「上一段末帧续接」把上一段的最后 N 帧钉在输出
        # 第 0..N-1 帧上，模型从第 N 帧才回到本段音频的起点。生成完再裁掉这 N 帧，
        # 留下来的画面就比音频整体超前 N 帧 —— 每段累加，第 2 段起口型就"快一点"。
        # 音频分支是 SetLatentNoiseMask(0) 锁死的原声，时间轴以它为准，所以只能让
        # 音频也把这段前导带进来，两者才对得上。
        #
        # 「本段时长」仍然只报真正的段长（end - start），不能把前导算进去：
        # 下游的帧长算式和裁剪长度都基于它，多算了会连锁出错。
        #
        # 单段模式（out_total == 1）即使跑的是第 3 段，也按「没有上一段」处理 ——
        # 以前这里只判断 index > 0，单段调试第 3 段时音频前置了 1.6 秒而画面没裁，
        # 音画直接错开一大截。现在统一走 has_prev 一个条件。
        overlap = align_guide_frames(续接帧数)
        has_prev = bool(index > 0 and out_total != 1)
        lead = (overlap / float(FPS)) if has_prev else 0.0

        piece = slice_audio(audio, max(0.0, segment["start"] - lead), segment["end"])
        prompt = resolve_prompt(segment, 全局提示词)
        duration = max(0.0, float(segment["end"]) - float(segment["start"]))

        gen_length, valid_frames, crop_start, audio_start = segment_frame_plan(
            duration, overlap, has_prev
        )

        # 参考图：段内有图就用段内的（整组覆盖），否则用全局
        ref_outs, ref_note, _ref_sizes = build_reference_outputs(segment, 参考图数据)

        status = "第 %d/%d 段（%.2f–%.2f 秒）%s%s%s" % (
            index + 1, total, segment["start"], segment["end"],
            " · 独立提示词" if segment["prompt"] else "",
            (" · 音频前置 %d 帧" % overlap) if lead > 0 else "",
            ref_note,
        )
        print("[dogking-tools] 音频分段：%s" % status)

        # 通知前端还要不要继续排下一段。和「提示词批量加载」用的事件分开，
        # 两个节点同时存在时不会互相触发。
        try:
            from server import PromptServer

            PromptServer.instance.send_sync(EVENT_NEXT, {
                "node": key,
                "index": index + 1,
                "total": total,
                "should_continue": bool(more and 自动连续),
            })
        except Exception:
            pass

        return {
            "ui": {
                "dogking_status": [status],
                "dogking_badge": ["%d/%d · %.1fs" % (index + 1, total, duration)],
                "dogking_index": [index + 1],
                # 项目指纹：前端自动连续排队前比对它，界面换过项目就不排（见 execution_signature）
                "dogking_sig": [execution_signature(文件路径, segments)],
            },
            "result": (
                piece, prompt, index + 1, out_total, duration, 选区音频,
                gen_length, valid_frames, crop_start, audio_start, overlap, has_prev,
            ) + ref_outs,
        }

    @staticmethod
    def _resolve_audio(audio_input, file_path):
        """输入口优先，其次本机文件。都没有就给出能照着做的中文报错。"""
        if audio_input is not None:
            return to_comfy_audio(audio_input["waveform"], audio_input["sample_rate"])

        resolved = resolve_audio_path(file_path)
        if not resolved:
            raise ValueError(
                "没有音频：点面板上的「选择音频」选一个文件，"
                "或者把上游的 AUDIO 接到「音频」输入口"
            )
        waveform, rate = load_waveform(resolved)
        return to_comfy_audio(waveform, rate)

    @staticmethod
    def _next_index(keys, segments, total):
        """
        顺序模式的游标：取当前下标，然后把游标推到下一个。

        @param {list} keys 候选 key，**第一个是当前项目的**，后面是兜底（见 project_key）。
                           读的时候按顺序找第一个有记录的，写的时候只写第一个。
        @returns {(int, bool)} (本次下标, 后面还有没有)
        """
        keys = [k for k in keys if k]
        signature = timeline_signature(segments)

        with _state_lock:
            state = _load_state()

            record = None
            used_key = None
            for k in keys:
                found = state.get(k)
                # state.json 被手工改坏时 record 可能不是 dict，当没记录处理
                if isinstance(found, dict):
                    record, used_key = found, k
                    break

            # 切点变了 → 从头开始；只改提示词不会改签名，游标保持
            if isinstance(record, dict) and record.get("audio_sig") == signature:
                if used_key != keys[0]:
                    # 老格式（纯节点 id）的记录，签名还对得上 = 同一个项目，
                    # 搬到新 key 上，别让它跑到一半的进度凭空归零
                    state.pop(used_key, None)
            else:
                record = {"audio_sig": signature, "index": 0}

            index = int(record.get("index", 0))
            if index < 0 or index >= total:
                index = 0

            record["index"] = index + 1
            more = (index + 1) < total
            state[keys[0]] = record
            _save_state(state)

        return index, more


# ---------------------------------------------------------------- 注册映射

NODE_CLASS_MAPPINGS = {
    "DogkingAudioSplit": DogkingAudioSplit,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DogkingAudioSplit": "数字人音频分段（dogking-tools）",
}
