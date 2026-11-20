# -*- coding: utf-8 -*-
"""
续接引导加噪（尾帧保护）
========================

为什么不用原生的 `Add Noise to Image`
--------------------------------------
原生节点对整个 batch 一视同仁地加噪，包括**最后一帧**。而续接时最后一帧
正好是生成第 0 帧的直接参照 —— 模型会把那一帧上的噪声当成内容继续画。

实测（3 段续接，强度 0.1）：

    帧    高频 std   一阶自相关
    0     0.0946     +0.038   ← 白噪声特征，是引导帧的噪声被画了出来
    1     0.0674     +0.114
    2     0.0559     +0.167
    5     0.0487     +0.257   ← 正常结构边缘

第 0 帧高频是稳态的 1.9 倍、自相关接近 0，肉眼就是「开头突然插进一张带
颗粒的图」。

这个节点保留加噪的作用（让模型别把引导帧当完美锚点），但**最后 N 帧不加**，
噪声就不会顺着衔接点传进生成结果。默认保护 1 帧，正好是衔接的那一帧。
"""

import torch


def add_noise_protecting_tail(image, strength, keep_tail, seed):
    """
    给 image（[T,H,W,C]，0~1）加高斯噪声，最后 `keep_tail` 帧原样保留。

    返回 (out, 实际加噪帧数, 保护帧数)。
    """
    if not torch.is_tensor(image):
        raise TypeError("image 必须是 torch.Tensor")

    n = int(image.shape[0])
    keep = max(0, min(int(keep_tail), n))
    add_n = n - keep
    s = float(strength)

    out = image.clone()
    if s <= 0.0 or add_n <= 0:
        return out, 0, keep

    gen = torch.Generator(device="cpu")
    gen.manual_seed(int(seed) & 0x7FFFFFFFFFFFFFFF)
    noise = torch.randn(
        tuple(out[:add_n].shape), generator=gen, dtype=torch.float32, device="cpu"
    ).to(device=out.device, dtype=out.dtype)

    out[:add_n] = torch.clamp(out[:add_n] + s * noise, 0.0, 1.0)
    return out, add_n, keep


class DogkingGuideNoise:
    """续接引导加噪：前 n-keep 帧加噪，尾部 keep 帧原样。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "图像": ("IMAGE",),
                "强度": ("FLOAT", {
                    "default": 0.10, "min": 0.0, "max": 0.5, "step": 0.01,
                    "tooltip": "噪声强度。0 = 完全关闭。0.05~0.12 是轻微扰动区间，"
                               "再大就会明显糊掉引导帧的细节",
                }),
                "保护尾帧": ("INT", {
                    "default": 1, "min": 0, "max": 8, "step": 1,
                    "tooltip": "最后几帧不加噪。1 = 只保护衔接的那一帧（推荐）；"
                               "0 = 全部加噪（等于原生 Add Noise to Image，会污染生成第 0 帧）",
                }),
                "种子": ("INT", {
                    "default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF, "step": 1,
                    "tooltip": "固定种子 = 每段噪声一致，便于复现",
                }),
            }
        }

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("图像", "状态")
    FUNCTION = "apply"
    CATEGORY = "Dogking"

    def apply(self, 图像, 强度, 保护尾帧, 种子):
        out, add_n, keep = add_noise_protecting_tail(图像, 强度, 保护尾帧, 种子)
        n = int(图像.shape[0])
        if add_n <= 0:
            status = "%d 帧 · 未加噪（强度 0 或全部保护）" % n
        else:
            status = "%d 帧 · 加噪 %d · 保护尾 %d · 强度 %.2f" % (n, add_n, keep, float(强度))
        return (out, status)


NODE_CLASS_MAPPINGS = {
    "DogkingGuideNoise": DogkingGuideNoise,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DogkingGuideNoise": "续接引导加噪（尾帧保护）",
}
