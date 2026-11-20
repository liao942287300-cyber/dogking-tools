"""受保护上下文：把上一段的末 N 帧连同音频一起「钉」进 latent，重绘阶段碰不到。

为什么需要它
------------
`MiniMaxH3AddGuide` 是把引导帧写进 conditioning（`minimax_keyframes`），那是**软条件**：
采样从 sigma 0.85 起步，模型有极大自由度改写。实测 8 段续接时，每段开头都会把
构图朝模型的「默认构图」拽一次，然后再往外漂 —— 这就是段边界回弹、画面越接越窄的
来源。

H3 的官方续接做法（也是 H3-Anti-Burn-LoRA 的训练设置）是 **protected masked context**：
上一段的末 39 帧 + 音频作为「受保护上下文」，用 `noise_mask = 0` 钉死，模型一个
像素都改不了，从第 40 帧开始才是它自由发挥的地方。

原话：*"each one carried the previous clip's last 39 frames and their audio as a
protected masked context."*

实现
----
ComfyUI 的 `SamplerCustomAdvanced` 会把 `latent["noise_mask"]` 原样传给采样器：

    x = x * denoise_mask + inpaint(x) * (1 - denoise_mask)

而 `comfy.model_base` 收到 mask 后，会用 `unpack_latents(mask, latent_shapes)` 把它
拆成 video / audio 两路。所以这里要做的三件事：

1. 把引导帧 VAE 编码后**写进 samples 的前 vt 个 latent 帧**（光设 mask 不行，
   mask=0 的区域填充的是 `latent_image` 本身，不是引导帧）
2. 造一个和 latent 同形状的 mask：video 前 vt 帧、audio 前 at 帧置 0
3. 用 `pack_latents` 把 mask 打包成 5D —— 采样器内部会把 latent 打包，
   mask 必须用同一种打包方式，否则长度对不上、模型拆包会错位

注意 mask 要扩到 latent 的通道数（video 24 通道、audio 32×2），因为拆包是按
`latent_shapes` 切固定长度的；只给单通道会切不够。
"""

import math

import torch

import comfy.utils
from comfy.nested_tensor import NestedTensor

# H3 的视频 latent 时间压缩：首 token 1 帧，之后每 4 帧一个 token，17 帧一个循环
FRAME_PER_TOKEN = (1, 4, 4, 4, 4)
FRAME_RESCALE = 5.0 / 3.0


def video_latent_t(frames):
    """像素帧数 -> 视频 latent 帧数（和 nodes_minimax_h3.video_latent_t 一致）。"""
    return 2 if frames <= 5 else ((frames - 5) // 17) * 5 + 2


def _resize_center(image, width, height):
    """等比缩放后居中裁剪，和 AddGuide 内部 `_resize(..., "center")` 同一套。"""
    s = image[..., :3].movedim(-1, 1)
    s = comfy.utils.common_upscale(s, width, height, "lanczos", "center")
    return s.movedim(1, -1)


class DogkingProtectedContext:
    """把引导帧钉进 latent 前 N 帧（含音频），采样时不会被重绘。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "latent": ("LATENT",),
                "guide": ("IMAGE",),
                "vae": ("VAE",),
                "帧数": ("INT", {"default": 39, "min": 5, "max": 204, "step": 1,
                                 "tooltip": "受保护上下文长度，必须是 17k+5（5/22/39/56…），和「取末帧数」保持一致"}),
                "锁音频": ("BOOLEAN", {"default": True,
                                       "tooltip": "音频也一起钉住；LoRA 训练时就是视频+音频一起锁"}),
                "启用": ("BOOLEAN", {"default": True,
                                     "tooltip": "接「有上一段」。第 1 段没有上一段，必须关掉，\n否则会把占位帧锁进开头"}),
            }
        }

    RETURN_TYPES = ("LATENT", "STRING")
    RETURN_NAMES = ("latent", "状态")
    FUNCTION = "apply"
    CATEGORY = "Dogking"

    def apply(self, latent, guide, vae, 帧数, 锁音频, 启用=True):
        if not 启用:
            return (latent, "受保护上下文：未启用（第 1 段 / 单段模式）")

        samples = latent["samples"]
        if not getattr(samples, "is_nested", False) or len(samples.tensors) != 2:
            raise ValueError("需要 MiniMax H3 的 AV latent（video + audio 两路）")

        video, audio = samples.tensors
        b, c, t, h, w = video.shape
        n = int(帧数)

        # 引导帧：对齐到 17k+5，再 encode
        if n >= 5:
            while n % 17 != 5:
                n -= 1
        vt = min(video_latent_t(n), t)

        g = guide[:n]
        tw, th = w * 16, h * 16
        if (int(g.shape[2]), int(g.shape[1])) != (tw, th):
            g = _resize_center(g, tw, th)
        gz = vae.encode(g)
        gz = gz.to(device=video.device, dtype=video.dtype)
        vt = min(vt, int(gz.shape[2]))

        video_out = video.clone()
        video_out[:, :, :vt] = gz[:, :, :vt]

        # mask：0 = 锁死，1 = 交给模型。
        # 必须用 NestedTensor 交给下游，**不能**自己 pack 成 5D ——
        # guider.sample 里对 mask 的处理是：
        #     if denoise_mask.is_nested: denoise_masks = denoise_mask.unbind()
        #     else: denoise_masks = [denoise_mask]          # 打包的会走这条
        #     prepare_mask(mask, latent_shapes[0])          # -> reshape_mask
        # 打包过的 mask 元素总数是 video+audio 之和，被当成 video 一个张量去
        # reshape 到 [T,H,W]，多出来的部分会把时间维撑错，解码就是等间距竖条纹。
        vm = torch.ones((b, 1, t, h, w), dtype=video.dtype, device=video.device)
        vm[:, :, :vt] = 0.0

        am = torch.ones((1, 1, 1, audio.shape[-1]), dtype=audio.dtype, device=audio.device)
        at = 0
        if 锁音频:
            at = int(round(n * FRAME_RESCALE))
            at = max(0, min(at, audio.shape[-1]))
            am[..., :at] = 0.0

        out = latent.copy()
        out["samples"] = NestedTensor((video_out, audio))
        out["noise_mask"] = NestedTensor((vm, am))

        status = (f"受保护上下文：{n} 帧 → 锁定 {vt}/{t} 个视频 latent 帧"
                  f" · 音频 {at}/{audio.shape[-1]}" + ("（已锁）" if 锁音频 else "（未锁）"))
        return (out, status)


NODE_CLASS_MAPPINGS = {"DogkingProtectedContext": DogkingProtectedContext}
NODE_DISPLAY_NAME_MAPPINGS = {"DogkingProtectedContext": "受保护上下文（续接硬锁）"}
