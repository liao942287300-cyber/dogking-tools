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


def _write_audio(container, stream, data, rate):
    """按 1024 样本一块喂 AAC 编码器，最后一块补零；末尾必须 flush。"""
    import av

    chunk = 1024
    total = int(data.shape[-1])
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


def concat_segments(files, out_path, fps, audio=None):
    """
    按给定顺序把若干 mp4 拼成一条。

    重编码而不是 remux：见模块文档。尺寸以第一段为准，不一致直接报错 ——
    混尺寸硬拼出来的画面会在接缝处跳一帧黑边。
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

        for path in files:
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
                for frame in part.decode(video=0):
                    arr = frame.to_ndarray(format="rgb24")
                    for packet in stream.encode(
                        av.VideoFrame.from_ndarray(arr, format="rgb24")
                    ):
                        container.mux(packet)
                    written += 1
        for packet in stream.encode():        # 同样必须 flush
            container.mux(packet)

        if audio_stream is not None:
            _write_audio(container, audio_stream, audio_data, audio_rate)
    finally:
        container.close()
    return written


def _fmt_seconds(frames, fps):
    if not fps:
        return "?"
    return "%.2fs" % (frames / float(fps))


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
                "清空旧片段": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "段 1 执行时清掉同名任务的旧片段，避免上一轮的残留混进来",
                }),
                "保留片段": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "合并完成后留着临时片段（排查接缝问题时会用到）",
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
               音频=None, 清空旧片段=True, 保留片段=False):
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
                },
                "result": (final_path, True, int(frames)),
            }

        work_dir = os.path.join(out_root, CONCAT_ROOT, name)
        os.makedirs(work_dir, exist_ok=True)

        # ---- 段 1：新一轮开始，清掉同名的旧片段
        if idx == 1 and 清空旧片段:
            removed = _clear_dir(work_dir)
            if removed:
                print("[dogking-tools] 分段合并：清掉 %d 个旧片段" % removed)

        seg_path = os.path.join(work_dir, "seg_%04d.mp4" % idx)
        frames = encode_segment(图像, seg_path, fps)

        parts = _existing_segments(work_dir)
        saved = len(parts)

        # 还没到最后一段：只落盘，提示还剩多少
        if idx < total:
            status = "第 %d/%d 段已存 · %d 帧（%.2fs）· 待合并 %d 段" % (
                idx, total, frames, frames / float(fps), total - saved
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
        total_frames = concat_segments(parts, final_path, fps, 音频)

        if not 保留片段:
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
            },
            "result": (final_path, True, int(total_frames)),
        }


NODE_CLASS_MAPPINGS = {
    "DogkingVideoConcat": DogkingVideoConcat,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DogkingVideoConcat": "分段视频合并（dogking-tools）",
}
