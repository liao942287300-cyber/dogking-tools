"""分段视频合并：每段单独生成后，自动拼成一条完整视频。

为什么需要它
------------
数字人一次只能生成十几秒，所以音频被切成 N 段、一段一次队列。每次队列结束产出
的是「那一段」的帧批次 —— ComfyUI 不会跨执行累积输出，所以每段接一个「保存视频」
只会得到 N 个独立文件，最后还得手工拼。

这个节点把「累积」落到磁盘上：

    段 1 → seg_0001.mp4
    段 2 → seg_0002.mp4
    ...
    段 N → 合成 output/<任务名>_<时间>.mp4（可选带整条音轨）

接线：`图像` 接该段的帧批次，`段序号` / `段数` 接「数字人音频分段」的对应两个口。
最后一段跑完，完整视频直接出现在 output/ 里，不用再碰任何东西。

--------------------------------------------------------------------------
两种模式的分工
--------------------------------------------------------------------------
- **段数 > 1**（顺序模式）：走「目录累积 → 最后合并」。
- **段数 == 1**（单段模式）：不碰累积目录，直接输出一个独立文件。
  这样调某一段时不会把正在进行的批量任务清掉。

--------------------------------------------------------------------------
为什么合并要重编码，而不是无损拼接
--------------------------------------------------------------------------
严格说 mp4 可以直接 `-c copy` 拼接（不损失质量、几乎不耗时）。但 PyAV 17 在
mp4 上做 packet 级 remux 会报 `Invalid argument`（实测：模板流 + time_base
对齐都试过，仍失败），而调外部 ffmpeg.exe 依赖用户机器上的路径。
所以这里走「解码 → 重编码」：中间片段用 crf 14（近无损）存，合并用 crf 16，
两次压缩的损失肉眼看不出来，且完全不依赖外部进程。

⚠️ 视频流的 `encode()` flush 必须调用。漏了会静默丢尾帧 —— 实测 96 帧只出 85 帧，
而且不报错，只是播到最后少半秒。
"""

import collections
import os
import re
import time
import glob

import numpy as np
import torch


# 中间片段目录（相对 ComfyUI/output）
CONCAT_ROOT = "dogking_concat"
# 中间片段编码质量：14 已经接近无损，合并再走一次 16，两次压缩肉眼无差
SEG_CRF = 14
FINAL_CRF = 16
PRESET = "veryfast"
# 单帧像素转 uint8 时裁到这个范围，避免 HDR / 越界值导致编码器报错
PIX_MIN, PIX_MAX = 0.0, 1.0
# 每段额外存一份「末 N 帧无损副本」，给「上一段末帧续接」读。
# 段 mp4 是 crf14 有损的，续接再拿它解码当引导帧，等于每段多背一层压缩；
# ffv1 + bgr0 存成 mkv 是数学无损（实测 RGB 往返误差 0），8 段链上少叠 8 层损失。
TAIL_FRAMES = 39
TAIL_SUFFIX = "_tail.mkv"


def _output_dir():
    try:
        import folder_paths

        return folder_paths.get_output_directory()
    except Exception:  # noqa: BLE001
        return os.path.join(os.getcwd(), "output")


def _safe_name(text, fallback="成片"):
    """任务名要当目录名 / 文件名用，剔掉 Windows 不允许的字符。"""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(text or "").strip())
    name = name.strip(". ")
    return name[:60] or fallback


def _seg_pattern(work_dir):
    return os.path.join(work_dir, "seg_*.mp4")


def _existing_segments(work_dir):
    return sorted(glob.glob(_seg_pattern(work_dir)))


def _clear_dir(work_dir):
    """清空累积目录。逐个删 + 容错：批量删在某些环境下会被整批拦下。"""
    removed = 0
    for path in glob.glob(os.path.join(work_dir, "*")):
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass
    return removed


def _frame_to_ndarray(frame):
    """IMAGE[...,3] float 0~1 → uint8 RGB。逐帧转换，避免整批 .numpy() 吃爆内存。"""
    arr = frame.detach().to("cpu").float().numpy()
    if arr.ndim == 2:                       # 灰度帧兜一下
        arr = np.repeat(arr[:, :, None], 3, axis=2)
    elif arr.shape[-1] == 1:
        arr = np.repeat(arr, 3, axis=2)
    elif arr.shape[-1] > 3:
        arr = arr[:, :, :3]
    return (np.clip(arr, PIX_MIN, PIX_MAX) * 255.0).astype(np.uint8)


def tail_path_of(seg_path):
    """seg_0004.mp4 -> seg_0004_tail.mkv"""
    base, _ext = os.path.splitext(seg_path)
    return base + TAIL_SUFFIX


def encode_tail_lossless(frames, seg_path, fps, count=TAIL_FRAMES):
    """
    把这一段最后 count 帧存成无损 mkv，返回写了几帧。

    存不成功不算错误：这是「减少续接损失」的优化，不该让整段生成失败。
    只在段数 > 1 时调用（单段模式没有下一段要读）。
    """
    import av

    if frames is None or not hasattr(frames, "shape"):
        return 0
    if frames.ndim == 3:
        frames = frames.unsqueeze(0)
    total = int(frames.shape[0])
    if total <= 0:
        return 0
    n = min(int(count), total)
    part = frames[total - n:]
    height, width = int(part.shape[1]), int(part.shape[2])
    path = tail_path_of(seg_path)

    container = av.open(path, "w")
    try:
        stream = container.add_stream("ffv1", rate=int(fps))
        stream.width, stream.height = width, height
        stream.pix_fmt = "bgr0"          # ffv1 只在这个像素格式下能开起来，且真无损
        for i in range(n):
            arr = _frame_to_ndarray(part[i])
            for packet in stream.encode(av.VideoFrame.from_ndarray(arr, format="rgb24")):
                container.mux(packet)
        for packet in stream.encode():    # 同样必须 flush
            container.mux(packet)
    finally:
        container.close()
    return n


def segment_luma(path):
    """一段视频的平均亮度（抽样 1/16 像素，够准又便宜）。给逐段亮度对齐用。"""
    import av

    total, n = 0.0, 0
    try:
        with av.open(path) as container:
            for frame in container.decode(video=0):
                arr = frame.to_ndarray(format="rgb24")
                total += float(arr[::4, ::4].mean())
                n += 1
    except Exception:  # noqa: BLE001
        return 0.0
    return total / max(n, 1)


def head_luma(path, probe=14, skip=2):
    """
    一段视频开头几帧的「本地影调」（采样 1/16 像素的亮度中位数）。

    为什么不能用整段均值：整段亮度是跟着内容走的（实测同一段内 20 帧窗口在
    101~114 之间来回），拿均值当目标会把接缝拉到「这段全片的平均亮度」，
    而接缝紧邻的其实是「开头这十几帧的亮度」——两者能差 5 个点，拉错方向
    反而造出一段假的渐变。

    跳过前 skip 帧：第 0~1 帧正好是「上一段影调」的残留，用它当目标等于把
    本段拽回上一段，接缝就白调了。
    """
    import av

    vals = []
    try:
        with av.open(path) as container:
            if not container.streams.video:
                return 0.0
            for i, frame in enumerate(container.decode(video=0)):
                arr = frame.to_ndarray(format="rgb24")
                vals.append(float(arr[::4, ::4].mean()))
                if i + 1 >= probe:
                    break
    except Exception:  # noqa: BLE001
        return 0.0
    body = vals[skip:] or vals
    return float(sorted(body)[len(body) // 2]) if body else 0.0


def read_tail_frames(seg_path, count):
    """
    读 `seg_000N_tail.mkv` 的最后 count 帧（uint8 RGB ndarray 列表）。

    这个副本是 ffv1 无损的、正好就是该段的结尾 —— 拿它给下一段做接缝过渡，
    不会像读 mp4 那样再叠一层 crf14 的损失。文件不在就返回空列表，调用方
    自己决定退回「不过渡」。
    """
    import av

    path = tail_path_of(seg_path)
    if not os.path.isfile(path):
        return []
    keep = collections.deque(maxlen=max(1, int(count)))
    try:
        with av.open(path) as container:
            if not container.streams.video:
                return []
            for frame in container.decode(video=0):
                keep.append(frame.to_ndarray(format="rgb24"))
    except Exception:  # noqa: BLE001
        return []
    return list(keep)


def encode_segment(frames, path, fps):
    """
    把一批帧编码成 mp4 片段。

    @param frames {torch.Tensor} [B,H,W,C] float 0~1
    @param path {str} 输出文件
    @param fps {int} 帧率
    @returns {int} 实际写入帧数
    """
    import av

    if frames is None or not hasattr(frames, "shape"):
        raise ValueError("「图像」口没有拿到帧批次（是不是上游没输出？）")
    if frames.ndim == 3:
        frames = frames.unsqueeze(0)
    total, height, width, _ch = frames.shape
    if total <= 0:
        raise ValueError("「图像」批次是空的（0 帧），没法编码")

    # yuv420p 要求宽高是偶数，奇数裁掉最后一行/列比补边更省事
    if width % 2 or height % 2:
        frames = frames[:, : height - height % 2, : width - width % 2, :]
        height, width = frames.shape[1], frames.shape[2]

    container = av.open(path, "w")
    try:
        stream = container.add_stream("libx264", rate=int(fps))
        stream.width, stream.height = int(width), int(height)
        stream.pix_fmt = "yuv420p"
        stream.options = {"crf": str(SEG_CRF), "preset": PRESET}

        for i in range(total):
            arr = _frame_to_ndarray(frames[i])
            for packet in stream.encode(av.VideoFrame.from_ndarray(arr, format="rgb24")):
                container.mux(packet)
        # 🔴 flush：不调用的话编码器缓冲里的尾帧全丢，而且不报错
        for packet in stream.encode():
            container.mux(packet)
    finally:
        container.close()
    return int(total)


def _audio_payload(audio):
    """
    把 ComfyUI 的 AUDIO 字典拆成 (wave[C,L] float32, rate, channels)。

    `音频` 接的是**整条**音轨（不是某一段）—— 每段自己带音轨没有意义，
    最后拼接会重复，所以音频只在合并那一次写进去。
    """
    waveform = audio.get("waveform") if isinstance(audio, dict) else None
    rate = int(audio.get("sample_rate") or 44100) if isinstance(audio, dict) else 44100
    if waveform is None:
        return None, rate, 0

    wave = waveform
    if wave.dim() == 3:
        wave = wave[0]                       # [B,C,L] → 取第一条
    channels = int(wave.shape[0])
    if channels > 2:
        wave = wave[:2]
        channels = 2
    data = wave.detach().to("cpu").float().numpy().astype(np.float32)
    return data, rate, channels


def _add_audio_stream(container, data, rate, channels):
    """
    音轨流必须在**写任何视频包之前**建好。

    先写视频再 add_stream 会拿到一个 time_base 为 0 的流，mux 时报
    `Cannot rebase to zero time` —— 实测踩过，只在这条路径触发。
    """
    stream = container.add_stream("aac", rate=int(rate))
    stream.bit_rate = 192000
    stream.layout = "stereo" if channels == 2 else "mono"
    return stream


def _write_audio(container, stream, data, rate, max_seconds=None):
    """
    按 1024 样本一块喂 AAC 编码器，最后一块补零；末尾必须 flush。

    `max_seconds` 给了就按它截断：成片长度由帧数决定（17N+5 取整后总比音频差几帧），
    不截的话末尾会出现「画面停住、声音还在走」—— 反过来截短更自然。
    """
    import av

    chunk = 1024
    total = int(data.shape[-1])
    if max_seconds:
        limit = int(float(max_seconds) * int(rate))
        if 0 < limit < total:
            data = data[:, :limit]
            total = limit
    pts = 0
    for start in range(0, total, chunk):
        block = data[:, start : start + chunk]
        if block.shape[-1] < chunk:
            block = np.pad(block, ((0, 0), (0, chunk - block.shape[-1])))
        frame = av.AudioFrame.from_ndarray(block, format="fltp", layout=stream.layout.name)
        frame.sample_rate = int(rate)
        frame.pts = pts
        pts += chunk
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode():            # flush，同样不能漏
        container.mux(packet)
    return round(total / float(rate), 3)


def concat_segments(files, out_path, fps, audio=None, seam_smooth=0, match_luma=True):
    """
    按给定顺序把若干 mp4 拼成一条。

    重编码而不是 remux：见模块文档。尺寸以第一段为准，不一致直接报错 ——
    混尺寸硬拼出来的画面会在接缝处跳一帧黑边。

    接缝亮度过渡（seam_smooth > 0）
    ------------------------------
    每段开头几帧亮度偏高、随后回落（实测接缝后 114.9 → 109.0），硬切就是
    肉眼看到的「闪一下」。

    **只调亮度，不混内容**：前 `seam_smooth` 帧的亮度从「上一段末帧的亮度」
    线性过渡到「本段开头十来帧的本地影调」。曾经把两段画面按权重混合过 ——
    那等于让画面倒退 K 帧时间，人物一动就是重影加一记明显的回跳，废案。

    裁剪本身不会造成跳变：实测段 2 第 0 帧和段 1 末帧的差是 6.1，与段 1 内部
    相邻帧的差（4.5~7.2）同级，时间上严丝合缝。看得见的跳变来自两次独立采样
    的整体影调差（实测亮度 108.3 → 105.5，锐度 18.5 → 15.8）。

    帧数不变、构图不变、音频不动。
    """
    import av

    if not files:
        raise ValueError("没有可拼接的片段")

    with av.open(files[0]) as probe:
        ref = probe.streams.video[0]
        width = int(ref.codec_context.width)
        height = int(ref.codec_context.height)

    # 音轨参数先备好：流要在写视频包之前建（见 _add_audio_stream 的注释）
    audio_data, audio_rate, audio_channels = (None, 0, 0)
    if audio is not None:
        audio_data, audio_rate, audio_channels = _audio_payload(audio)

    container = av.open(out_path, "w")
    written = 0
    try:
        stream = container.add_stream("libx264", rate=int(fps))
        stream.width, stream.height = width, height
        stream.pix_fmt = "yuv420p"
        stream.options = {"crf": str(FINAL_CRF), "preset": PRESET}

        audio_stream = None
        if audio_data is not None and audio_channels:
            audio_stream = _add_audio_stream(container, audio_data, audio_rate, audio_channels)

        smooth = max(0, int(seam_smooth))
        prev_luma = 0.0
        # 每段独立重绘，曝光会逐段漂（实测 5 段 110.6 → 101.6，掉 8%），
        # 硬拼起来接缝处就是一次「暗一下」。以第 1 段为基准把每段整体拉平。
        base_luma = segment_luma(files[0]) if match_luma else 0.0
        for path in files:
            gain = 1.0
            luma = 0.0
            if match_luma and base_luma > 0:
                luma = segment_luma(path)
                if luma > 1e-6:
                    gain = min(1.18, max(0.85, base_luma / luma))
            else:
                luma = segment_luma(path)
            with av.open(path) as part:
                src = part.streams.video[0]
                if int(src.codec_context.width) != width or int(src.codec_context.height) != height:
                    raise ValueError(
                        "片段尺寸不一致：%s 是 %dx%d，第一段是 %dx%d。"
                        "同一批分段必须用同样的分辨率生成。"
                        % (
                            os.path.basename(path),
                            src.codec_context.width,
                            src.codec_context.height,
                            width,
                            height,
                        )
                    )
                # 过渡目标用「本段开头这十几帧的本地影调」，不是整段均值：
                # 整段亮度跟着内容走（同段内能差 5 个点），拿均值当目标会把
                # 接缝往错方向拉，反倒造出一段假渐变。
                steady = head_luma(path) * gain if luma > 1e-6 else 0.0
                n_in = 0
                for frame in part.decode(video=0):
                    arr = frame.to_ndarray(format="rgb24")
                    if gain != 1.0:
                        arr = (arr.astype("float32") * gain).round().clip(0, 255).astype("uint8")
                    # 接缝亮度过渡：前 smooth 帧的亮度从上一段末帧拉回本段水平。
                    # 只乘标量增益，像素结构一点不动 —— 混合画面会倒退时间、出重影。
                    if smooth and prev_luma > 0 and n_in < smooth and steady > 0:
                        a = (n_in + 1.0) / (smooth + 1.0)
                        target = prev_luma + (steady - prev_luma) * a
                        cur = float(arr.mean())
                        if cur > 1.0:
                            fac = min(1.12, max(0.88, target / cur))
                            arr = (arr.astype("float32") * fac).round().clip(0, 255).astype("uint8")
                    prev_luma = float(arr.mean())
                    for packet in stream.encode(
                        av.VideoFrame.from_ndarray(arr, format="rgb24")
                    ):
                        container.mux(packet)
                    written += 1
                    n_in += 1

        for packet in stream.encode():        # 同样必须 flush
            container.mux(packet)

        if audio_stream is not None:
            # 按成片长度截断：视频比音频短时（17N+5 取整、或最后一段少几帧），
            # 不截的话末尾会拖一段「画面停住、声音还在走」。
            _write_audio(container, audio_stream, audio_data, audio_rate,
                         max_seconds=written / float(fps))
    finally:
        container.close()
    return written


def _fmt_seconds(frames, fps):
    if not fps:
        return "?"
    return "%.2fs" % (frames / float(fps))


def _preview_ui(path, out_root):
    """
    让节点自己把成片显示出来（和「Video Combine」那样能直接播）。

    用的是官方 `SaveVideo` 同一套约定：`ui.images` 里放 `{filename, subfolder, type}`
    再加 `animated: true`，前端看到 .mp4 + animated 就渲染 `<video>`。
    自己拼 `ui.gifs` 是 VHS 的私有格式，只在他们那个前端补丁里生效，这里不用。
    """
    rel = os.path.relpath(path, out_root).replace("\\", "/")
    subfolder = os.path.dirname(rel)
    return {
        "images": [{
            "filename": os.path.basename(path),
            "subfolder": subfolder,
            "type": "output",
        }],
        "animated": (True,),
    }


class DogkingVideoConcat:
    """
    分段视频合并：每段落盘，最后一段自动拼成一条完整视频。

    段数 > 1 时才走累积目录；段数 == 1（单段模式）直接输出独立文件。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "图像": ("IMAGE", {
                    "tooltip": "这一段生成的帧批次",
                }),
                "段序号": ("INT", {
                    "default": 1, "min": 1, "max": 9999, "step": 1,
                    "tooltip": "接「数字人音频分段」的「段序号」口",
                }),
                "段数": ("INT", {
                    "default": 1, "min": 1, "max": 9999, "step": 1,
                    "tooltip": "接「数字人音频分段」的「段数」口：顺序模式下是总段数",
                }),
                "fps": ("INT", {
                    "default": 24, "min": 1, "max": 120, "step": 1,
                    "tooltip": "生成时的帧率，必须和模型实际帧率一致，否则音画会对不上",
                }),
                "任务名": ("STRING", {
                    "default": "数字人成片",
                    "tooltip": "同一个任务名共用一套临时片段；换项目时改个名字即可",
                }),
            },
            "optional": {
                "音频": ("AUDIO", {
                    "tooltip": "整条音频（不是某一段）。合并时一起压进 mp4；不接就是无声视频",
                }),
                "开始时清空旧片段": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "段 1 执行前清掉同名任务的旧片段，避免上一轮的残留混进来。"
                               "和下面的「合并后保留片段」不冲突：那个管结尾，这个管开头",
                }),
                "合并后保留片段": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "最后一段合并完成后，留着这一轮的临时片段（排查接缝问题时会用到）",
                }),
                "逐段亮度对齐": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "每段生成的曝光会逐段漂（实测 5 段掉 8%），以第 1 段为基准"
                               "把每段整体拉平，接缝就不再「暗一下」。增益限制在 0.85~1.18",
                }),
                "接缝过渡": ("INT", {
                    "default": 6, "min": 0, "max": 24, "step": 1,
                    "tooltip": "每段开头几帧的亮度从上一段末帧平滑拉回本段水平，抹掉接缝处"
                               "的「闪一下」。只改亮度、不混画面（混画面会倒退时间、出重影）。"
                               "0 = 硬切。帧数/构图/音频都不变",
                }),
            },
        }

    RETURN_TYPES = ("STRING", "BOOLEAN", "INT")
    RETURN_NAMES = ("视频路径", "完成", "总帧数")
    FUNCTION = "concat"
    CATEGORY = "dogking-tools"
    OUTPUT_NODE = True
    DESCRIPTION = (
        "每段视频先落到临时目录，最后一段执行完自动拼成一条完整 mp4（可带整条音轨）。"
        "段序号 / 段数 接「数字人音频分段」，不用手工拼接。"
    )

    def concat(self, 图像, 段序号, 段数, fps, 任务名,
               音频=None, 开始时清空旧片段=True, 合并后保留片段=False,
               接缝过渡=4, 逐段亮度对齐=True, **kwargs):
        # 兼容旧工作流：这两个开关原来叫「清空旧片段」/「保留片段」。
        # UI 重开会按新名字重建 widget，但直接提交的 API 格式工作流会把旧 key 传进来，
        # 不认就 TypeError —— 这里接住并映射，老工作流不用改动。
        if "清空旧片段" in kwargs:
            开始时清空旧片段 = kwargs.pop("清空旧片段")
        if "保留片段" in kwargs:
            合并后保留片段 = kwargs.pop("保留片段")
        idx = max(1, int(段序号))
        total = max(1, int(段数))
        fps = max(1, int(fps))
        name = _safe_name(任务名)
        out_root = _output_dir()
        stamp = time.strftime("%Y%m%d_%H%M%S")

        # ---- 单段模式：段数 == 1，不碰累积目录，直接出一个独立文件
        if total == 1:
            final_path = os.path.join(out_root, "%s_%s.mp4" % (name, stamp))
            frames = encode_segment(图像, final_path, fps)
            status = "单段输出 · %s · %d 帧（%s）" % (
                os.path.basename(final_path), frames, _fmt_seconds(frames, fps)
            )
            print("[dogking-tools] 分段合并：%s" % status)
            return {
                "ui": {
                    "dogking_status": [status],
                    "dogking_badge": ["单段 · %d 帧" % frames],
                    **_preview_ui(final_path, out_root),
                },
                "result": (final_path, True, int(frames)),
            }

        work_dir = os.path.join(out_root, CONCAT_ROOT, name)
        os.makedirs(work_dir, exist_ok=True)

        # ---- 段 1：新一轮开始，清掉同名的旧片段
        if idx == 1 and 开始时清空旧片段:
            removed = _clear_dir(work_dir)
            if removed:
                print("[dogking-tools] 分段合并：清掉 %d 个旧片段" % removed)

        seg_path = os.path.join(work_dir, "seg_%04d.mp4" % idx)
        frames = encode_segment(图像, seg_path, fps)
        try:
            saved_tail = encode_tail_lossless(图像, seg_path, fps)
        except Exception as exc:  # noqa: BLE001
            saved_tail = 0
            print("[dogking-tools] 分段合并：无损末帧没存下来（%s），续接会退回读 mp4" % exc)

        parts = _existing_segments(work_dir)
        saved = len(parts)

        # 还没到最后一段：只落盘，提示还剩多少
        if idx < total:
            status = "第 %d/%d 段已存 · %d 帧（%.2fs）· 待合并 %d 段 · 末帧无损 %d" % (
                idx, total, frames, frames / float(fps), total - saved, saved_tail
            )
            print("[dogking-tools] 分段合并：%s" % status)
            return {
                "ui": {
                    "dogking_status": [status],
                    "dogking_badge": ["%d/%d 已存 %d 段" % (idx, total, saved)],
                },
                "result": (seg_path, False, int(frames)),
            }

        # ---- 最后一段：合并。先校验片段齐不齐，缺了就报错，别输出半成品
        if saved != total:
            missing = [
                str(i) for i in range(1, total + 1)
                if not os.path.exists(os.path.join(work_dir, "seg_%04d.mp4" % i))
            ]
            raise ValueError(
                "最后一段跑完了，但只找到 %d/%d 个片段，缺第 %s 段。"
                "多半是某一段执行失败或被跳过了 —— 重新跑那一段，或从第 1 段整轮重来。"
                % (saved, total, "、".join(missing))
            )

        final_path = os.path.join(out_root, "%s_%s.mp4" % (name, stamp))
        total_frames = concat_segments(parts, final_path, fps, 音频,
                                       seam_smooth=接缝过渡, match_luma=逐段亮度对齐)

        if not 合并后保留片段:
            _clear_dir(work_dir)
            try:
                os.rmdir(work_dir)          # 目录空了才删得掉，非空会抛，忽略即可
            except OSError:
                pass

        status = "已合并 %d 段 · %d 帧（%s）→ %s" % (
            total, total_frames, _fmt_seconds(total_frames, fps),
            os.path.basename(final_path),
        )
        print("[dogking-tools] 分段合并：%s" % status)
        return {
            "ui": {
                "dogking_status": [status],
                "dogking_badge": ["完成 %d 帧" % total_frames],
                **_preview_ui(final_path, out_root),
            },
            "result": (final_path, True, int(total_frames)),
        }


NODE_CLASS_MAPPINGS = {
    "DogkingVideoConcat": DogkingVideoConcat,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DogkingVideoConcat": "分段视频合并（dogking-tools）",
}
