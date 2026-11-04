"""
dogking-tools —— Ripple 二采放大节点

把 H3 Ripple Clip 输出的 ripple 整体放大：画布宽高按系数（或按二采像素）
放大并向上对齐到 32 的倍数，画面帧同步放大，ripple 里的
width / height / geometry 一起改写。

用法：Ripple Clip 的 ripple → 本节点 → Ripple Apply 的 ripple 输入。
Ripple Apply 会按新的 width/height 建空 latent，guide 关键帧也用放大后的画面，
二采就在放大尺寸上跑，不会出现 latent 和关键帧尺寸对不上。
"""

import math

# 画布对齐：H3 的 latent 网格是 32 的倍数
ALIGN = 32

METHODS = ["lanczos", "bicubic", "bilinear", "area", "nearest-exact"]

MODE_FACTOR = "按倍数"
MODE_PIXELS = "按二采像素"
MODES = [MODE_FACTOR, MODE_PIXELS]


def align_up(value, multiple=ALIGN):
    """
    向上取到 multiple 的整数倍。

    减 1e-6 是留浮点余量：480 * 2 可能算出 960.0000000001，
    直接 ceil 会被顶到 992，白涨一档。
    """
    return max(multiple, int(math.ceil(value / multiple - 1e-6)) * multiple)


def target_canvas(width, height, factor, megapixels):
    """
    算出放大后的画布宽高。

    megapixels > 0 时按二采流的公式来（跟工作流里的「数学表达式宽度/高度」一致）：
        宽 = ceil(sqrt(百万 * 1e6 * 宽 / 高) / 32) * 32
        高 = ceil(宽 * 高 / 宽 / 32) * 32
    否则按系数放大。
    """
    if megapixels > 0:
        w = align_up(math.sqrt(megapixels * 1_000_000 * width / height))
        return w, align_up(w * height / width)
    return align_up(width * factor), align_up(height * factor)


def scaled_content(content, base_canvas, canvas):
    """
    按画布比例缩放画面区域，留边（模型 padding）也按同比例走。

    Ripple 的画面是「内容 + 居中留边」拼成的画布，只放大画布不管内容的话
    留边比例会变，构图就歪了。
    """
    _, _, cw, ch = content
    w0, h0 = base_canvas
    w1, h1 = canvas
    nw = min(w1, max(2, 2 * round(cw * w1 / w0 / 2)))
    nh = min(h1, max(2, 2 * round(ch * h1 / h0 / 2)))
    return [(w1 - nw) // 2, (h1 - nh) // 2, nw, nh]


def resize_frames(window, width, height, method="lanczos", step=8):
    """把整段画面缩放到新尺寸。分块做，避免一次性多占一倍显存/内存。"""
    import torch

    import comfy.utils

    out = torch.empty((window.shape[0], height, width, window.shape[3]), dtype=window.dtype)
    for start in range(0, window.shape[0], step):
        chunk = window[start:start + step, ..., :3].movedim(-1, 1)
        out[start:start + step] = comfy.utils.common_upscale(chunk, width, height, method, "disabled").movedim(1, -1)
    return out


class DogkingRippleUpscale:
    """把 ripple 放大到二采尺寸（宽高对齐 32）。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "ripple": ("WN_RIPPLE",),
                "模式": (MODES, {
                    "default": MODE_FACTOR,
                    "tooltip": "按倍数：画布 × 放大倍数；按二采像素：按目标像素和宽高比算（跟二采流的数学表达式一致）",
                }),
                "放大倍数": ("FLOAT", {
                    "default": 2.0, "min": 0.05, "max": 8.0, "step": 0.05,
                    "tooltip": "画布宽高 × 这个系数，结果向上对齐 32 的倍数",
                }),
                "目标百万像素": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 100.0, "step": 0.01,
                    "tooltip": "二采目标像素：ceil(sqrt(百万*1e6*宽/高)/32)*32，跟二采流的数学表达式一致",
                }),
            },
            "optional": {
                "放大算法": (METHODS, {"default": "lanczos"}),
            },
        }

    RETURN_TYPES = ("WN_RIPPLE", "IMAGE", "INT", "INT")
    RETURN_NAMES = ("ripple", "画面", "宽度", "高度")
    OUTPUT_TOOLTIPS = (
        "放大后的 ripple，接给 Ripple Apply。",
        "放大后的窗口画面（含模型留边）。",
        "放大后的画布宽，32 的倍数。",
        "放大后的画布高，32 的倍数。",
    )
    FUNCTION = "upscale"
    CATEGORY = "dogking-tools"
    DESCRIPTION = (
        "把 Ripple Clip 的 ripple 放大到二采尺寸：画布宽高向上对齐到 32 的倍数，"
        "画面帧和 geometry 同步放大，Ripple Apply 就会在放大尺寸上建 latent 和关键帧。"
        "「模式」切换按倍数还是按二采像素，只显示当前模式用的那个参数。"
        "注意内存：2 倍放大等于 4 倍像素，长窗口（300+ 帧）会比较吃内存。"
    )

    def upscale(self, ripple, 模式, 放大倍数, 目标百万像素, 放大算法="lanczos"):
        window = ripple.get("window") if isinstance(ripple, dict) else None
        geometry = ripple.get("geometry") if isinstance(ripple, dict) else None
        if window is None or not isinstance(geometry, dict):
            raise ValueError("ripple 输入不对：这个节点要接 H3 Ripple Clip 的 ripple 输出。")

        w0, h0 = int(ripple["width"]), int(ripple["height"])
        if tuple(window.shape[1:3]) != (h0, w0):
            raise ValueError(
                f"ripple 里的尺寸对不上：画面是 {window.shape[2]}x{window.shape[1]}，"
                f"width/height 是 {w0}x{h0}。"
            )

        by_pixels = 模式 == MODE_PIXELS
        factor = 0.0 if by_pixels else float(放大倍数)
        megapixels = float(目标百万像素) if by_pixels else 0.0
        if by_pixels:
            if not math.isfinite(megapixels) or megapixels <= 0:
                raise ValueError("「按二采像素」模式下，目标百万像素要填大于 0 的数。")
        elif not math.isfinite(factor) or factor <= 0:
            raise ValueError("放大倍数要填正数。")

        w1, h1 = target_canvas(w0, h0, factor, megapixels)
        content = scaled_content(geometry["content_rect"], (w0, h0), (w1, h1))
        # 节点底部那行字：画布前后尺寸 + 有效画面尺寸。
        # Ripple Apply 提示里报的 (宽x高) 就是「内容」，不写出来两边看着像对不上。
        badge = f"{w0}×{h0} → {w1}×{h1}（内容 {content[2]}×{content[3]}）"
        if (w1, h1) == (w0, h0):
            return {"ui": {"dogking_size": [badge]}, "result": (ripple, window, w1, h1)}

        new_window = resize_frames(window, w1, h1, 放大算法)

        out = dict(ripple)
        out.update(
            window=new_window,
            width=w1,
            height=h1,
            geometry={**geometry, "width": w1, "height": h1, "content_rect": content},
            # 换掉缓存键，否则 Ripple Apply 会复用一采尺寸的 guide latent
            window_key=(ripple.get("window_key"), w1, h1),
        )
        binding = ripple.get("binding")
        if isinstance(binding, tuple) and len(binding) >= 2:
            out["binding"] = binding[:-2] + (w1, h1)

        print(f"[dogking-tools] Ripple 放大（{模式}）：{w0}x{h0} -> {w1}x{h1}（画面 {content[2]}x{content[3]}）")
        return {"ui": {"dogking_size": [badge]}, "result": (out, new_window, w1, h1)}


NODE_CLASS_MAPPINGS = {
    "DogkingRippleUpscale": DogkingRippleUpscale,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DogkingRippleUpscale": "Ripple 二采放大（dogking-tools）",
}
