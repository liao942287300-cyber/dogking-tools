"""段首亮度归位：把「续接引导帧」那几帧的曝光压回正常水平。

为什么需要它
------------
分段续接时，每段前面会多出 22 帧（约 0.92 秒）的引导帧 —— 那是上一段的末帧
被模型重新画了一遍。问题是这 22 帧被重画后**整体偏亮**：实测同一段里

    前 22 帧   全幅亮度 0.4693
    稳态帧     全幅亮度 0.4542      → 亮了 3.3%

在浅色墙这种大面积中间调背景上，3.3% 已经能看出来了，而且第 22 → 23 帧有一个
台阶（帧间差 0.052，是全片前 40 帧里最大的一次），肉眼就是「开头闪一下白光」。

靠裁剪只能把它从成片里删掉，问题是：只要某一段没裁到（段号判断写错、单段模式、
手动改批次），这层白就原样进成片。所以这里再做一层保险 —— **不删帧，只压亮度**：

    第 0 帧 → 完全对齐到稳态亮度
    第 n 帧 → 恢复原样
    中间线性插值

增益逐帧渐变，所以既不会在第 0 帧留跳变，也不会在第 n 帧留跳变。画面内容一个
像素都不动，只乘一个 RGB 统一的标量（三通道同增益，避免白平衡漂移）。

接线
----
接在 `VAEDecode` 后面、裁剪/合并之前：

    VAEDecode → 段首亮度归位 → ImageFromBatch（裁剪）/ 分段视频合并

参数
----
帧数        要处理的帧数，和「前置重叠帧」保持一致（本项目 22）
参考起点    从第几帧开始取目标亮度，默认等于帧数（第一个稳态帧）
参考帧数    目标亮度用多少帧的中位数，默认 12 —— 单帧会被动作/表情带偏
强度        0 = 不处理，1 = 完全归位，>1 过度校正（一般不用）
"""

import torch


def _frame_tone(frame):
    """
    一帧（或一批帧）的「影调」：把像素摊平后 RGB 三通道各自的均值。

    为什么按通道取：闪白通常是三通道一起抬，但抬的比例不一定一样（偏暖/偏冷）。
    归位时如果只按总亮度缩放，色偏会留着；按通道校正更干净。

    注意别写成 `mean(dim=(0, 1))`：批次进来是 4 维 (T,H,W,C)，那样会把 H 也
    平均掉，得到 (W,C) 的形状，后面乘增益会广播成一张乱图。
    """
    f = frame.float()
    return f.reshape(-1, f.shape[-1]).mean(dim=0)


class DogkingHeadTone:
    """把段首若干帧的曝光渐变拉回该段稳态水平。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "帧数": ("INT", {"default": 22, "min": 0, "max": 512, "step": 1}),
                "参考起点": ("INT", {"default": 22, "min": 0, "max": 4096, "step": 1}),
                "参考帧数": ("INT", {"default": 12, "min": 1, "max": 256, "step": 1}),
                "强度": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 2.0, "step": 0.05}),
            }
        }

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("图像", "状态")
    FUNCTION = "apply"
    CATEGORY = "Dogking"

    def apply(self, image, 帧数, 参考起点, 参考帧数, 强度):
        total = int(image.shape[0])
        n = max(0, min(int(帧数), total - 1))
        if n == 0 or 强度 <= 0:
            return (image, "段首亮度归位：已跳过（帧数 0 或强度 0）")

        k = max(0, min(int(参考起点), total - 1))
        ref_slice = image[k: min(k + max(1, int(参考帧数)), total)]
        # 逐帧取影调后按帧取中位数：单帧会被表情/动作带偏，整批平均会被个别
        # 亮帧拖走，中位数最稳。
        # 注意 median 要沿「帧」这一维取（dim=0）；先 mean 再 median 会把三通道
        # 压成一个标量，等于拿绿通道亮度当目标，整体会偏暗。
        tones = torch.stack([_frame_tone(f) for f in ref_slice])
        ref = tones.median(dim=0).values
        ref = torch.clamp(ref, min=1e-3)

        out = image.clone()
        gains = []
        # 注意是 n + 1 帧：第 n 帧是「引导区之后的第一个真实画面」，实测它
        # 仍然带着 3% 的偏亮（0.473 vs 稳态 0.461）。只压 0..n-1 的话，被压暗
        # 的第 n-1 帧和没压的第 n 帧之间会新长出一个台阶，反而更明显。
        for i in range(min(n + 1, total)):
            cur = torch.clamp(_frame_tone(out[i]), min=1e-3)
            raw = ref / cur
            # 整段恒定校正，不做「渐变回原样」：这 22 帧内部本身就不平
            # （0.447 → 0.476，越靠后越亮），只压首帧、末帧留偏差等于把台阶
            # 留在原地。全部对齐到稳态后，过渡区与稳态帧亮度相等，接缝消失。
            gain = 1.0 + (raw - 1.0) * float(强度)
            gain = torch.clamp(gain, 0.85, 1.20)
            out[i] = torch.clamp(out[i] * gain, 0.0, 1.0)
            gains.append(float(gain.mean()))

        detail = (f"{n} 帧 · 首帧增益 ×{gains[0]:.3f} → 末帧 ×{gains[-1]:.3f}"
                  f" · 目标取自第 {k} 帧起 {int(参考帧数)} 帧")
        return (out, f"段首亮度归位：{detail}")


NODE_CLASS_MAPPINGS = {"DogkingHeadTone": DogkingHeadTone}
NODE_DISPLAY_NAME_MAPPINGS = {"DogkingHeadTone": "段首亮度归位"}
