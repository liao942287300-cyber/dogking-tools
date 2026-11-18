#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
画质体检：给一段生成的 mp4 出几个数，用于「改完参数到底有没有变好」的量化对比。

为什么需要它
------------
「背景糊了 / 变绿了 / 有块」这类判断靠肉眼非常不可靠 —— 同一帧放大到 1:1 看
灰墙也会觉得糊。这里把三件事拆成数字：

  色偏   G/R、B/R 两个比值。同一条片子里这些值应该基本恒定（尤其 G/R），
         如果越往后越偏，才是「颜色漂移」；恒定但偏离 1 只是白平衡/光线。
  锐度   中央区域拉普拉斯标准差。首帧 vs 末帧的变化率最能说明「段内衰减」。
  块效应 16 像素网格上的边界落差 ÷ 非边界落差。>1.3 说明网格上有台阶
         （量化/编码劣化）；1.0 左右是正常的。

用法
----
    python tools/quality_report.py <视频> [<视频2> ...]

多个文件会并排比较 —— 换 VAE / 改 sigmas 之后跑同一个脚本就知道谁更好。
"""

import os
import sys

import numpy as np

# 开头这几帧是模型的收敛过程（实测 25.2 平滑降到 17.7），不参与基线统计
WARMUP_FRAMES = 12


def _detail(gray):
    """中央区域的拉普拉斯标准差：越高越锐。避开边缘，只测画面主体。"""
    h, w = gray.shape
    c = gray[h // 4: h * 3 // 4, w // 4: w * 3 // 4]
    lap = (-4 * c[1:-1, 1:-1] + c[:-2, 1:-1] + c[2:, 1:-1] + c[1:-1, :-2] + c[1:-1, 2:])
    return float(lap.std())


def _blockiness(gray):
    """16px 网格边界处的高频能量 ÷ 其余位置。约 1 = 干净。"""
    vert = np.abs(np.diff(gray.astype(np.float32), axis=1)).mean(0)
    cols = np.arange(1, gray.shape[1])
    edge = cols % 16 == 0
    return float(vert[edge].mean() / max(vert[~edge].mean(), 1e-6))


def frame_stats(frame):
    f = frame.astype(np.float32)
    r, g, b = f[..., 0].mean(), f[..., 1].mean(), f[..., 2].mean()
    mx, mn = f.max(2), f.min(2)
    return {
        "亮度": float(f.mean()),
        "G/R": float(g / max(r, 1e-6)),
        "B/R": float(b / max(r, 1e-6)),
        "饱和": float(((mx - mn) / np.maximum(mx, 1e-6)).mean()),
        "锐度": _detail(f.mean(2)),
        "块比": _blockiness(f.mean(2)),
    }


def report(path):
    import av

    with av.open(path) as container:
        frames = [fr.to_ndarray(format="rgb24") for fr in container.decode(video=0)]
    if not frames:
        raise SystemExit("%s 里没有视频帧" % path)

    total = len(frames)
    picks = sorted({0, total // 4, total // 2, total * 3 // 4, total - 1})
    print("=" * 78)
    print("%s  %d 帧  %dx%d  %.2fs@24" % (
        os.path.basename(path), total, frames[0].shape[1], frames[0].shape[0], total / 24.0))
    print("%-8s %8s %7s %7s %7s %7s %7s" % ("帧", "亮度", "G/R", "B/R", "饱和", "锐度", "块比"))
    stats = []
    for i in picks:
        s = frame_stats(frames[i])
        stats.append((i, s))
        print("%-8s %8.2f %7.3f %7.3f %7.3f %7.2f %7.2f" % (
            "#%d" % i, s["亮度"], s["G/R"], s["B/R"], s["饱和"], s["锐度"], s["块比"]))

    # 基线要用全片算，且去掉开头收敛段。
    # 只拿「首帧 vs 末帧」比会得出「掉 29%」的假结论 —— 实测开头 12 帧是
    # 25.2 平滑降到 17.7，之后 470 帧稳定在 18.7±1.3，那是收敛不是衰减。
    sharp_all = np.array([_detail(f.astype(np.float32).mean(2)) for f in frames])
    warm = min(WARMUP_FRAMES, max(1, total // 10))
    body = sharp_all[warm:] if total > warm + 1 else sharp_all
    base, spread = float(body.mean()), float(body.std())
    rel_spread = spread / max(base, 1e-6) * 100

    first, last = stats[0][1], stats[-1][1]
    g_drift = (last["G/R"] - first["G/R"]) / first["G/R"] * 100
    block_worst = max(s["块比"] for _, s in stats)

    print("-" * 78)
    print("基线锐度 %.2f ± %.2f（去掉前 %d 帧收敛段，波动 %+.1f%%）" % (base, spread, warm, rel_spread))
    print("首帧 %.2f → 末帧 %.2f（%+.1f%%）　G/R 漂移 %+.2f%%　最高块比 %.2f" % (
        first["锐度"], last["锐度"],
        (last["锐度"] - first["锐度"]) / first["锐度"] * 100, g_drift, block_worst))

    verdict = []
    if abs(g_drift) > 3:
        verdict.append("颜色在漂（G/R 变化超过 3%）")
    if block_worst > 1.3:
        verdict.append("有 16px 网格块效应")
    if rel_spread > 12:
        verdict.append("段内忽清忽糊（波动超过 12%）")
    print("结论：" + ("；".join(verdict) if verdict else "整段稳定"))
    print("提示：单条片子只能看稳不稳；「基线够不够锐」要和改参数前的另一条比 —— "
          "把两个文件一起传给本脚本，直接比基线。")
    return base


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    results = []
    for p in sys.argv[1:]:
        results.append((os.path.basename(p), report(p)))
    if len(results) > 1:
        print("=" * 78)
        print("基线对比（越高越锐）：")
        best = max(r[1] for r in results)
        for name, base in sorted(results, key=lambda r: -r[1]):
            print("  %-46s %6.2f  %+6.1f%%" % (name, base, (base / best - 1) * 100))
