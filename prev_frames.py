"""上一段末帧续接：把上一条片段最后的几帧读回来，当 AddGuide 的引导帧。

为什么需要它
------------
MiniMax H3 生成超过十几秒要分段跑，段与段之间靠「上一段最后 5 帧」做续接
（`MiniMaxH3AddGuide` 把这几帧锚在新段的第 0 帧上）。问题在于：**这 5 帧不在
当前这次执行里** —— 它躺在上一段已经落盘的 mp4 里。

如果图省事把当前采样的 `VAEDecode` 输出接回同一个 `BasicGuider`，ComfyUI 会
直接判「无效工作流：存在循环节点连线」，因为那等于用自己还没算出来的结果引导自己，
而且这个校验是**执行前的静态拓扑检查**，把开关设成 false 也绕不过去。

所以引导帧必须来自**当前执行之外**的来源。这个节点就干这件事：

    段序号 = 5 → 读 output/dogking_concat/<任务名>/seg_0004.mp4 的最后 N 帧

段号自动算，不用手改路径，第 1 段 / 单段模式自然返回「没有上一段」。

接线
----
`图像` → 「一采循环体」「二采循环体」子图的 image 输入
`有上一段` → 这两个子图的 switch 输入（替换原来的 `a > 1 and b != 1` 判断）
`段序号` / `段数` → 「数字人音频分段」的对应两个口

`有上一段` 比算式更准：段序号为 1、上一段没落盘、或者段数=1（单段模式）时都给 False。
单段模式必须不续接 —— 那时是在单独调某一段，磁盘上的 seg 文件属于上一轮，接上去画面是错的。

--------------------------------------------------------------------------
缓存
--------------------------------------------------------------------------
ComfyUI 按输入值做缓存，光看「段序号=5」会一直复用旧结果 —— 重跑第 4 段之后
再跑第 5 段，拿到的还是上一批帧。所以 `IS_CHANGED` 把目标文件的 mtime + 大小
拼进签名里，片段一被重写，签名变、节点重算。
"""

import collections
import os
import re

import numpy as np
import torch


# 与 video_concat 保持一致：中间片段落在这里
CONCAT_ROOT = "dogking_concat"
# 占位帧尺寸：AddGuide 会自己缩放到目标分辨率，这里只要能读就行
PLACEHOLDER_SIZE = 64


def _output_dir():
    try:
        import folder_paths

        return folder_paths.get_output_directory()
    except Exception:  # noqa: BLE001
        return os.path.join(os.getcwd(), "output")


def _safe_name(text, fallback="成片"):
    """和 video_concat 同一套规则，否则这里算出来的目录名对不上。"""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(text or "").strip())
    name = name.strip(". ")
    return name[:60] or fallback


def _parts_root(片段根目录=""):
    """`片段根目录` 留空 = ComfyUI 的 output/dogking_concat。"""
    text = str(片段根目录 or "").strip()
    if text:
        return os.path.abspath(os.path.expanduser(text))
    return os.path.join(_output_dir(), CONCAT_ROOT)


def segment_path(段序号, 任务名, 片段根目录=""):
    """
    算出「上一段」的文件路径（段序号减一）。

    @returns {str} 段序号 <= 1 时返回空串（没有上一段）
    """
    prev = max(1, int(段序号)) - 1
    if prev < 1:
        return ""
    name = _safe_name(任务名)
    return os.path.join(_parts_root(片段根目录), name, "seg_%04d.mp4" % prev)


def read_last_frames(path, count):
    """
    解码整条 mp4，只留最后 count 帧。

    只保留末尾几帧而不是全读进内存：10 秒 720p 全解码成 ndarray 是几百 MB，
    而这里最多只要 17 帧。deque(maxlen) 边解边丢。

    @returns {list[np.ndarray]} RGB uint8，长度 <= count
    """
    import av

    keep = collections.deque(maxlen=max(1, int(count)))
    with av.open(path) as container:
        if not container.streams.video:
            raise ValueError("%s 里没有视频流" % os.path.basename(path))
        for frame in container.decode(video=0):
            keep.append(frame.to_ndarray(format="rgb24"))
    return list(keep)


def frames_to_image(frames):
    """list[uint8 RGB] → IMAGE [B,H,W,3] float 0~1。"""
    arr = np.stack(frames).astype(np.float32) / 255.0
    return torch.from_numpy(arr)


def _placeholder():
    """没有上一段时的占位：1 帧纯黑。AddGuide 会缩放到目标尺寸，不影响任何输出。"""
    return torch.zeros((1, PLACEHOLDER_SIZE, PLACEHOLDER_SIZE, 3), dtype=torch.float32)


def _save_preview(image, 任务名, 段序号):
    """
    把取到的最后一帧存成 png，让节点里能直接看到「接的是哪一段的哪一帧」。

    存不下就算了 —— 预览失败不该让整段生成失败。
    """
    try:
        from PIL import Image as PILImage

        out_root = _output_dir()
        folder = os.path.join(out_root, "dogking_prev")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "%s_seg%04d.png" % (_safe_name(任务名), int(段序号)))
        arr = (image[-1].clamp(0, 1).numpy() * 255.0).round().astype(np.uint8)
        PILImage.fromarray(arr).save(path)
        return {
            "images": [{
                "filename": os.path.basename(path),
                "subfolder": "dogking_prev",
                "type": "output",
            }]
        }
    except Exception:  # noqa: BLE001
        return {}


class DogkingPrevSegmentFrames:
    """
    读上一段成片的最后 N 帧，供分段续接使用。

    段序号 - 1 就是要读的片段；没有上一段时输出占位帧 + `有上一段=False`，
    让下游 switch 直接走「不引导」分支，第 1 段照样能跑。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "段序号": ("INT", {
                    "default": 1, "min": 1, "max": 9999, "step": 1,
                    "tooltip": "接「数字人音频分段」的「段序号」口；节点会去读 段序号-1 那段",
                }),
                "任务名": ("STRING", {
                    "default": "数字人成片",
                    "tooltip": "必须和「分段视频合并」的任务名一致，否则找不到片段目录",
                }),
                "取末帧数": ("INT", {
                    "default": 5, "min": 1, "max": 17, "step": 1,
                    "tooltip": "H3 的引导片段只认 5、22、39…（17k+5）帧；一般填 5",
                }),
                "片段根目录": ("STRING", {
                    "default": "",
                    "tooltip": "留空 = ComfyUI 的 output/dogking_concat；填了就当存放各任务子目录的根",
                }),
                "缺段时": (["黑帧占位", "报错"], {
                    "default": "黑帧占位",
                    "tooltip": "黑帧占位：第 1 段/单段模式照常跑；报错：上一段没落盘就直接停下",
                }),
            },
            "optional": {
                "段数": ("INT", {
                    "default": 0, "min": 0, "max": 9999, "step": 1,
                    "tooltip": "接「数字人音频分段」的「段数」口。填 1（单段模式）时强制不续接 —— "
                               "单段模式是在调某一段，读到的 seg 文件是上一轮的，接上去反而错。"
                               "留 0 = 不判断",
                }),
            },
        }

    RETURN_TYPES = ("IMAGE", "BOOLEAN", "STRING", "INT")
    RETURN_NAMES = ("末帧", "有上一段", "来源", "帧数")
    FUNCTION = "load_prev"
    CATEGORY = "dogking-tools"
    OUTPUT_NODE = True
    DESCRIPTION = (
        "读取上一段成片的最后几帧，作为分段续接的引导图。"
        "段号自动减一，第 1 段返回占位帧且「有上一段」为 False。"
    )

    @classmethod
    def IS_CHANGED(cls, 段序号=1, 任务名="", 取末帧数=5, 片段根目录="", 缺段时="黑帧占位", 段数=0):
        # 片段被重写（重跑上一段）时要重算，否则会一直用旧的引导帧
        path = segment_path(段序号, 任务名, 片段根目录)
        try:
            stat = os.stat(path)
            return "%s|%d|%d|%d" % (path, stat.st_mtime_ns, stat.st_size, int(段数))
        except OSError:
            return "%s|missing|%d" % (path or "-", int(段数))

    def load_prev(self, 段序号, 任务名, 取末帧数=5, 片段根目录="", 缺段时="黑帧占位", 段数=0):
        idx = max(1, int(段序号))
        count = max(1, int(取末帧数))
        total = max(0, int(段数))

        # ---- 单段模式：段数填 1 说明在单独调某一段，磁盘上的 seg 文件属于上一轮，接上去是错的
        if total == 1:
            status = "单段模式不续接（段数=1）· 输出占位帧"
            print("[dogking-tools] 上一段末帧：%s" % status)
            return {
                "ui": {"dogking_status": [status]},
                "result": (_placeholder(), False, "", 1),
            }

        path = segment_path(idx, 任务名, 片段根目录)

        # ---- 没有上一段：第 1 段、单段模式、或上一段没落盘
        if not path or not os.path.isfile(path):
            if not path:
                reason = "第 %d 段没有上一段" % idx
            else:
                reason = "找不到上一段 %s" % os.path.basename(path)
            if str(缺段时) == "报错":
                raise ValueError(
                    "%s。先把上一段跑完（段号必须连续），或把「缺段时」改成「黑帧占位」"
                    % reason
                )
            status = "无上一段 · %s · 输出占位帧（不引导）" % reason
            print("[dogking-tools] 上一段末帧：%s" % status)
            return {
                "ui": {"dogking_status": [status]},
                "result": (_placeholder(), False, "", 1),
            }

        # ---- 有上一段：只留末尾 count 帧
        frames = read_last_frames(path, count)[:count]
        if not frames:
            raise ValueError(
                "上一段 %s 解码出来是 0 帧（文件可能损坏或还没写完）"
                % os.path.basename(path)
            )
        image = frames_to_image(frames)
        got = int(image.shape[0])
        status = "续接第 %d 段 · 取自 %s 末 %d 帧" % (
            idx, os.path.basename(path), got
        )
        print("[dogking-tools] 上一段末帧：%s" % status)
        return {
            "ui": {
                "dogking_status": [status],
                "dogking_badge": ["续接 %d 帧" % got],
                **_save_preview(image, 任务名, idx),
            },
            "result": (image, True, path, got),
        }


NODE_CLASS_MAPPINGS = {
    "DogkingPrevSegmentFrames": DogkingPrevSegmentFrames,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DogkingPrevSegmentFrames": "上一段末帧续接（dogking-tools）",
}
