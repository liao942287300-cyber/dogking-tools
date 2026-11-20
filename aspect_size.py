"""
dogking-tools —— 同比例定尺寸

解决的问题
--------------------------------------------------------------------------
上采样（LatentUpscale）会把画面拉到目标宽高。只要目标比例和源比例不一致，
画面就被纵向或横向拉伸：

    544x960 (0.5667) -> 768x1376 (0.5581)   纵向多拉 1.54%   人变瘦
    544x960 (0.5667) -> 800x1408 (0.5682)   纵向少拉 0.27%   基本无感

以前这里是一条 `sqrt(c*1e6*a/b)` 再 ceil 到 32 的算式。问题在于「ceil」把高度
顶出去，比例就散了；而且换一个分辨率或换一个比例，得重新手算。

这个节点的做法
--------------------------------------------------------------------------
输入源宽高（一采实际输出的尺寸）和目标像素，输出一组**宽高都是 32 倍数**、
**比例误差受控**的目标尺寸。换任何比例都不用改公式。

两档策略
--------------------------------------------------------------------------
1. **近似档**：按目标比例在 32 网格上找比例误差最小的组合，误差 <= `最大比例误差`。
   绝大多数情况走这档，像素几乎不变。
2. **严格档**：把源比例化简成最简整数比 p:q，只有 `32p*k × 32q*k` 能精确命中。
   近似档达不到要求时自动退到这档（例如 544:960 = 17:30，只有 544x960 / 1088x1920）。

为什么像素不取"最接近"而取"不小于"
--------------------------------------------------------------------------
成片尺寸不能比现在小。严格档里选 k 时优先满足 `像素 >= 目标 × 0.8`，
否则 544x960 会一直选中 0.52M 那一档，画质反而降。
"""

import math

STEP = 32


def solve_size(src_w, src_h, megapixels=1.0, max_err_pct=0.5, step=STEP):
    """
    算出一组与源同比例、宽高均为 `step` 倍数的目标尺寸。

    @param {int} src_w 源宽
    @param {int} src_h 源高
    @param {float} megapixels 目标像素（百万）
    @param {float} max_err_pct 允许的最大比例误差（百分比）
    @param {int} step 宽高对齐步长
    @returns {(int, int, str)} (宽, 高, 说明)
    """
    src_w, src_h = int(src_w), int(src_h)
    if src_w <= 0 or src_h <= 0:
        raise ValueError("源宽高必须是正数，当前 %dx%d" % (src_w, src_h))

    target = max(0.05, float(megapixels)) * 1e6
    src_px = float(src_w * src_h)
    # 成片不能比源小：上采样节点只会放大，给它一个更小的目标没意义
    target = max(target, src_px)
    src_ar = src_w / float(src_h)

    # ---------- 严格档优先：p:q 的最简比，只有 32p*k / 32q*k 能精确命中 ----------
    g = math.gcd(src_w, src_h)
    p, q = src_w // g, src_h // g

    strict = None
    for k in range(1, 129):
        w, h = step * p * k, step * q * k
        px = float(w * h)
        if px < src_px * 1.05:      # 至少要比源大一点，否则等于没上采样
            continue
        if px > target * 2.5:       # 再大就离谱了
            break
        d = abs(math.log(px / target))
        if strict is None or d < strict[0]:
            strict = (d, w, h, px)

    if strict is not None and strict[3] <= target * 1.8:
        _, w, h, px = strict
        return w, h, "严格 %dx%d · 比例误差 0.00%% · %.2fM（最简比 %d:%d，零误差只有这一档）" % (
            w, h, px / 1e6, p, q
        )

    # ---------- 近似档：只保留**误差达标**的候选，再挑像素最接近目标的 ----------
    s0 = math.sqrt(target / src_px)
    lo = max(step, int(src_w * s0 * 0.80) // step * step)
    hi = max(lo + step, int(src_w * s0 * 1.30) + step)

    best = None
    for w in range(lo, hi + 1, step):
        if w < src_w * 0.98:        # 不缩小
            continue
        h = int(round(w * src_h / float(src_w) / step)) * step
        if h < step or h < src_h * 0.98:
            continue
        err = (w / float(h)) / src_ar - 1.0
        if abs(err) * 100.0 > float(max_err_pct):
            continue                # 先过硬门槛，再谈像素
        px = w * float(h)
        score = abs(math.log(px / target))
        if best is None or score < best[0]:
            best = (score, w, h, err, px)

    if best is not None:
        _, w, h, err, px = best
        if (w, h) == (src_w, src_h):
            return w, h, "= 源尺寸 %dx%d（目标像素不比源大，无需放大）" % (w, h)
        return w, h, "近似 %dx%d · 比例误差 %+.2f%% · %.2fM" % (w, h, err * 100.0, px / 1e6)

    # ---------- 兜底：连近似都没有（误差要求过严，只有严格档能命中） ----------
    if strict is not None:
        _, w, h, px = strict
        return w, h, "严格 %dx%d · 比例误差 0.00%% · %.2fM（超出像素上限，仍取严格）" % (
            w, h, px / 1e6
        )
    raise ValueError(
        "找不到合适尺寸：源 %dx%d，目标 %.2fM，误差上限 %.2f%%" % (
            src_w, src_h, float(megapixels), float(max_err_pct))
    )


class DogkingMatchAspectSize:
    """
    同比例定尺寸：源宽高 -> 目标宽高，保持比例、保持 32 倍数。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "源宽度": ("INT", {
                    "default": 544, "min": 32, "max": 8192, "step": 32,
                    "tooltip": "生成阶段的宽度（一采输出的宽）；换比例时这里会自动变",
                }),
                "源高度": ("INT", {
                    "default": 960, "min": 32, "max": 8192, "step": 32,
                    "tooltip": "生成阶段的高度",
                }),
                "目标像素_百万": ("FLOAT", {
                    "default": 1.0, "min": 0.05, "max": 8.0, "step": 0.05,
                    "tooltip": "希望最终成片落在多少百万像素，只影响选档，不影响比例",
                }),
                "最大比例误差(%)": ("FLOAT", {
                    "default": 0.5, "min": 0.0, "max": 5.0, "step": 0.05,
                    "tooltip": "单位是百分比：0.5 表示允许 0.5% 的偏差（不是 50%）。"
                               "近似档算出来超过它就退到严格档（严格档零误差，"
                               "但只有 32p*k×32q*k 那几档，像素可能跳很多）",
                }),
            }
        }

    RETURN_TYPES = ("INT", "INT", "STRING")
    RETURN_NAMES = ("宽度", "高度", "说明")
    FUNCTION = "solve"
    CATEGORY = "dogking-tools"
    DESCRIPTION = "按源宽高算出同比例、32 倍数的目标宽高，换任何比例都不用改公式"

    def solve(self, 源宽度, 源高度, 目标像素_百万, **kwargs):
        # 兼容旧键名：ComfyUI 按 widget 位置传值，键名改了值还是对的
        最大比例误差 = kwargs.get("最大比例误差(%)", kwargs.get("最大比例误差", 0.5))
        w, h, note = solve_size(源宽度, 源高度, 目标像素_百万, 最大比例误差)
        print("[dogking-tools] 同比例定尺寸：%dx%d -> %dx%d（%s）" % (
            源宽度, 源高度, w, h, note))
        return (w, h, note)


NODE_CLASS_MAPPINGS = {
    "DogkingMatchAspectSize": DogkingMatchAspectSize,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DogkingMatchAspectSize": "同比例定尺寸（dogking-tools）",
}
