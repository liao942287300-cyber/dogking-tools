"""dogking-tools Ripple 二采放大测试：32 对齐 + 两种尺寸算法 + 整节点跑通"""
import importlib.util
import math
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 插件根目录
COMFY = os.path.dirname(os.path.dirname(HERE))                      # ComfyUI 根目录
if os.path.isdir(COMFY):
    sys.path.insert(0, COMFY)

spec = importlib.util.spec_from_file_location("dk_ripple_upscale", os.path.join(HERE, "ripple_upscale.py"))
upscale_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upscale_mod)

PASS, FAIL = [], []


def check(name, got, want):
    ok = got == want
    (PASS if ok else FAIL).append(name)
    print(f"{'PASS' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"     期望: {want}")
        print(f"     实际: {got}")


# ---------------------------------------------------------------- 32 对齐
check("对齐 960 不动", upscale_mod.align_up(960), 960)
check("对齐 浮点余量 960.0000000001", upscale_mod.align_up(960.0000000001), 960)
check("对齐 1728 不动", upscale_mod.align_up(1728), 1728)
check("对齐 720 向上到 736", upscale_mod.align_up(720), 736)
check("对齐 极小值兜底", upscale_mod.align_up(0.5), 32)

# ---------------------------------------------------------------- 系数模式
check("2 倍 480x864", upscale_mod.target_canvas(480, 864, 2.0, 0), (960, 1728))
check("1.5 倍 480x864", upscale_mod.target_canvas(480, 864, 1.5, 0), (736, 1312))
check("2 倍 864x512", upscale_mod.target_canvas(864, 512, 2.0, 0), (1728, 1024))

# ---------------------------------------------------------------- 像素模式
# 必须跟二采流里的「数学表达式宽度/高度」算出来的一模一样
def expr_canvas(a, b, c):
    width = int(eval("ceil( sqrt(c*1000000 * a / b) / 32 ) * 32",
                     {"ceil": math.ceil, "sqrt": math.sqrt, "a": a, "b": b, "c": c}))
    height = int(eval("ceil( ( (ceil( sqrt(c*1000000 * a / b) / 32 ) * 32 ) * b / a ) / 32 ) * 32",
                      {"ceil": math.ceil, "sqrt": math.sqrt, "a": a, "b": b, "c": c}))
    return width, height


for a, b in ((864, 512), (512, 864), (480, 864)):
    check(f"1.3MP {a}x{b} 跟二采表达式一致",
          upscale_mod.target_canvas(a, b, 2.0, 1.3), expr_canvas(a, b, 1.3))

# ---------------------------------------------------------------- 内容留边等比
check("2 倍内容缩放", upscale_mod.scaled_content([0, 6, 480, 852], (480, 864), (960, 1728)),
      [0, 12, 960, 1704])

# ---------------------------------------------------------------- 整节点
try:
    import torch

    T, H, W = 5, 64, 96
    ripple = {
        "window": torch.rand(T, H, W, 3),
        "markers": [{"id": 1, "frame": 0, "edited": "", "edited_at": None}],
        "length": T, "width": W, "height": H, "fps": 24, "window_start": 3, "window_time": 0.125,
        "audio": None, "source": "x.mp4", "source_version": "1-2",
        "window_key": ("k", 1, 2, 3, T, "{}"),
        "binding": ("x.mp4", 2, 3, T, (0,), W, H),
        "geometry": {"crop": [0.0, 0.0, 1.0, 1.0], "source_rect": [0, 0, 192, 128],
                     "width": W, "height": H, "content_rect": [0, 8, 96, 48], "resize_method": "lanczos"},
    }
    node = upscale_mod.DogkingRippleUpscale()

    def run(*args):
        """跑一次，返回 (结果元组, 节点上显示的那行字)"""
        res = node.upscale(*args)
        return res["result"], res["ui"]["dogking_size"][0]

    (out, frames, w, h), badge = run(ripple, "按倍数", 2.0, 0.0)
    check("输出宽高", (w, h), (192, 128))
    check("画面尺寸", tuple(frames.shape), (T, 128, 192, 3))
    check("ripple 宽高同步", (out["width"], out["height"]), (192, 128))
    check("geometry 内容", out["geometry"]["content_rect"], [0, 16, 192, 96])
    check("缓存键已换", out["window_key"] != ripple["window_key"], True)
    check("binding 更新", out["binding"][-2:], (192, 128))
    check("原 ripple 没被改", (ripple["width"], ripple["height"]), (W, H))
    check("markers 原样保留", out["markers"], ripple["markers"])
    content2 = upscale_mod.scaled_content([0, 8, W, 48], (W, H), (W * 2, H * 2))
    check("节点显示尺寸（倍数）", badge,
          f"{W}×{H} → {W * 2}×{H * 2}（内容 {content2[2]}×{content2[3]}）")
    check("1 倍原样返回", run(ripple, "按倍数", 1.0, 0.0)[0][0] is ripple, True)

    # 模式切换：只认当前模式那个参数
    check("按倍数时忽略目标百万像素",
          run(ripple, "按倍数", 2.0, 0.5)[0][0]["width"], 192)
    check("按二采像素时忽略放大倍数",
          run(ripple, "按二采像素", 8.0, 0.01)[0][0]["width"],
          upscale_mod.target_canvas(W, H, 0, 0.01)[0])
    w_mp, h_mp = upscale_mod.target_canvas(W, H, 0, 0.01)
    content_mp = upscale_mod.scaled_content([0, 8, W, 48], (W, H), (w_mp, h_mp))
    check("节点显示尺寸（像素）", run(ripple, "按二采像素", 2.0, 0.01)[1],
          f"{W}×{H} → {w_mp}×{h_mp}（内容 {content_mp[2]}×{content_mp[3]}）")
    try:
        node.upscale(ripple, "按二采像素", 2.0, 0.0)
        check("按二采像素没填像素要报错", "没报错", "报错")
    except ValueError:
        check("按二采像素没填像素要报错", True, True)
except ImportError as error:  # 没装 torch / 不在 ComfyUI 环境里跑
    print(f"SKIP 整节点测试：{error}")

print("\n" + "=" * 50)
print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
if FAIL:
    print("失败项:", FAIL)
    sys.exit(1)
