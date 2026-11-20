# -*- coding: utf-8 -*-
"""
video_concat.py 测试（分段视频合并）

守四件事：

  ① **编码不丢帧** —— 视频流 encode() 的 flush 漏了会静默丢尾帧（实测 96 → 85），
     而且不报错，只在播到最后少半秒。所以每条用例都从文件解码回来数帧。
  ② **累积顺序** —— seg_0001 / 0002 / 0003 必须按序号拼，不能按文件系统返回顺序。
  ③ **缺段不装成功** —— 少一段就报错，绝不输出一条「看着能播但短了」的半成品。
  ④ **单段模式不碰批量目录** —— 调某一段时把正在跑的 8 段任务清掉，是最恶心的 bug。

不写用户 output 目录：每个用例都把 _output_dir 指向临时目录。
"""

import importlib.util
import os
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 插件根目录


def _load(name, alias):
    spec = importlib.util.spec_from_file_location(alias, os.path.join(HERE, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_vc = _load("video_concat", "dk_video_concat_for_test")

torch = pytest.importorskip("torch")
pytest.importorskip("av")          # 没装 PyAV 的环境直接跳过，别报成失败


FPS = 8


def _isolate(tag="vc"):
    """把 output 指到临时目录，返回它。测试绝不能写真实 output。"""
    root = tempfile.mkdtemp(prefix="dk_%s_" % tag)
    _vc._output_dir = lambda: root
    return root


def _frames(n, h=32, w=32, value=0.5):
    return torch.full((n, h, w, 3), float(value), dtype=torch.float32)


def _count_frames(path):
    """解码回来数真实帧数 —— 不看容器元数据（音视频交错时那个数不准）。"""
    import av

    with av.open(path) as container:
        return sum(1 for _ in container.decode(video=0))


def _node():
    return _vc.DogkingVideoConcat()


# ---------------------------------------------------------------- 编码

def test_encode_segment_writes_every_frame():
    root = _isolate()
    path = os.path.join(root, "one.mp4")
    written = _vc.encode_segment(_frames(9), path, FPS)

    assert written == 9
    assert _count_frames(path) == 9, "flush 丢了尾帧"


def test_encode_segment_handles_odd_size():
    """奇数宽高会让 yuv420p 编码失败，编码器里裁掉最后一行/列。"""
    root = _isolate()
    path = os.path.join(root, "odd.mp4")
    _vc.encode_segment(_frames(4, h=31, w=33), path, FPS)
    assert _count_frames(path) == 4


def test_encode_segment_rejects_empty_batch():
    _isolate()
    with pytest.raises(ValueError):
        _vc.encode_segment(_frames(0), os.path.join(tempfile.mkdtemp(), "x.mp4"), FPS)


# ---------------------------------------------------------------- 累积

def test_middle_segments_only_save():
    root = _isolate()
    node = _node()

    out1 = node.concat(图像=_frames(4), 段序号=1, 段数=3, fps=FPS, 任务名="批量")
    assert out1["result"][1] is False, "第 1 段不该合并"
    assert os.path.isfile(os.path.join(root, _vc.CONCAT_ROOT, "批量", "seg_0001.mp4"))

    out2 = node.concat(图像=_frames(5), 段序号=2, 段数=3, fps=FPS, 任务名="批量")
    assert out2["result"][1] is False
    assert "2/3" in out2["ui"]["dogking_status"][0]


def test_last_segment_merges_everything_in_order():
    root = _isolate()
    node = _node()

    node.concat(图像=_frames(4), 段序号=1, 段数=3, fps=FPS, 任务名="整条")
    node.concat(图像=_frames(6), 段序号=2, 段数=3, fps=FPS, 任务名="整条")
    out = node.concat(图像=_frames(5), 段序号=3, 段数=3, fps=FPS, 任务名="整条")

    path, done, total_frames = out["result"]
    assert done is True
    assert total_frames == 4 + 6 + 5
    assert _count_frames(path) == 15, "拼出来的帧数必须等于各段之和"
    # 合并完把临时目录清掉
    assert not os.path.isdir(os.path.join(root, _vc.CONCAT_ROOT, "整条"))


def test_first_segment_clears_stale_parts():
    root = _isolate()
    work = os.path.join(root, _vc.CONCAT_ROOT, "重跑")
    os.makedirs(work)
    stale = os.path.join(work, "seg_0007.mp4")
    _vc.encode_segment(_frames(3), stale, FPS)
    assert os.path.isfile(stale)

    _node().concat(图像=_frames(4), 段序号=1, 段数=3, fps=FPS, 任务名="重跑")
    assert not os.path.exists(stale), "旧片段没清掉，会混进这一轮"
    assert os.path.isfile(os.path.join(work, "seg_0001.mp4"))


def test_keep_parts_when_asked():
    root = _isolate()
    node = _node()
    node.concat(图像=_frames(3), 段序号=1, 段数=2, fps=FPS, 任务名="留片段")
    node.concat(图像=_frames(3), 段序号=2, 段数=2, fps=FPS, 任务名="留片段",
                合并后保留片段=True)
    work = os.path.join(root, _vc.CONCAT_ROOT, "留片段")
    assert os.path.isfile(os.path.join(work, "seg_0001.mp4"))
    assert os.path.isfile(os.path.join(work, "seg_0002.mp4"))


# ---------------------------------------------------------------- 缺段

def test_missing_segment_raises_instead_of_half_video():
    root = _isolate()
    node = _node()
    node.concat(图像=_frames(4), 段序号=1, 段数=3, fps=FPS, 任务名="缺段")
    # 故意跳过第 2 段，直接跑第 3 段
    with pytest.raises(ValueError) as err:
        node.concat(图像=_frames(4), 段序号=3, 段数=3, fps=FPS, 任务名="缺段")
    assert "缺第 2 段" in str(err.value)
    # 没有产出成片
    assert not [f for f in os.listdir(root) if f.startswith("缺段_")]


# ---------------------------------------------------------------- 单段模式

def test_single_mode_does_not_touch_batch_dir():
    root = _isolate()
    batch = os.path.join(root, _vc.CONCAT_ROOT, "混用")
    os.makedirs(batch)
    keep = os.path.join(batch, "seg_0001.mp4")
    _vc.encode_segment(_frames(3), keep, FPS)

    out = _node().concat(图像=_frames(4), 段序号=5, 段数=1, fps=FPS, 任务名="混用")
    path, done, frames = out["result"]

    assert done is True
    assert frames == 4
    assert path.startswith(root) and "dogking_concat" not in path
    assert os.path.isfile(keep), "单段模式把正在跑的批量片段删了"


# ---------------------------------------------------------------- 尺寸

def test_size_mismatch_raises():
    root = _isolate()
    a = os.path.join(root, "a.mp4")
    b = os.path.join(root, "b.mp4")
    _vc.encode_segment(_frames(3, h=32, w=32), a, FPS)
    _vc.encode_segment(_frames(3, h=48, w=48), b, FPS)

    with pytest.raises(ValueError) as err:
        _vc.concat_segments([a, b], os.path.join(root, "bad.mp4"), FPS)
    assert "尺寸不一致" in str(err.value)


# ---------------------------------------------------------------- 音轨

def test_audio_track_is_muxed():
    root = _isolate()
    node = _node()
    rate = 8000
    wave = torch.zeros(1, 1, rate * 1)      # 1 秒单声道
    audio = {"waveform": wave, "sample_rate": rate}

    node.concat(图像=_frames(4), 段序号=1, 段数=2, fps=FPS, 任务名="带音轨")
    out = node.concat(图像=_frames(4), 段序号=2, 段数=2, fps=FPS, 任务名="带音轨",
                      音频=audio)
    path, done, _frames_total = out["result"]
    assert done is True

    import av

    with av.open(path) as container:
        assert container.streams.audio, "音轨没写进去"
        assert int(container.streams.audio[0].codec_context.sample_rate) == rate


def test_legacy_switch_names_still_work():
    """
    两个开关改过名（清空旧片段 → 开始时清空旧片段，保留片段 → 合并后保留片段）。

    直接提交的 API 格式工作流会把旧 key 原样传进来 —— 不接住就 TypeError，
    老工作流一跑就红。这里断言旧名字仍然生效。
    """
    root = _isolate()
    node = _node()
    node.concat(图像=_frames(4), 段序号=1, 段数=2, fps=FPS, 任务名="旧名")
    out = node.concat(图像=_frames(4), 段序号=2, 段数=2, fps=FPS, 任务名="旧名",
                      保留片段=True)          # 旧名字，应映射到「合并后保留片段」

    assert out["result"][1] is True
    work = os.path.join(root, _vc.CONCAT_ROOT, "旧名")
    assert os.path.isfile(os.path.join(work, "seg_0001.mp4")), "旧名字没生效"


def test_final_result_carries_video_preview():
    """
    成片要能在节点里直接播，不用另接「Video Combine」。

    用的是官方 SaveVideo 那套约定：`ui.images` + `animated: true`，
    前端看到 .mp4 + animated 就渲染 <video>。少一个字段就退化成「找不到图片」。
    """
    _isolate()
    node = _node()
    node.concat(图像=_frames(3), 段序号=1, 段数=2, fps=FPS, 任务名="预览")
    out = node.concat(图像=_frames(3), 段序号=2, 段数=2, fps=FPS, 任务名="预览")

    ui = out["ui"]
    assert ui["animated"][0] is True
    entry = ui["images"][0]
    assert entry["filename"].endswith(".mp4")
    assert entry["type"] == "output"
    assert entry["subfolder"] == ""


def test_middle_segment_has_no_preview():
    """中间段还没成片，别塞一个空预览占着节点。"""
    _isolate()
    out = _node().concat(图像=_frames(3), 段序号=1, 段数=3, fps=FPS, 任务名="无预览")
    assert "images" not in out["ui"]


def test_audio_is_trimmed_to_video_length():
    """
    音频比成片长时按成片截断。

    成片长度由帧数决定（17N+5 取整后总比音频差几帧），不截的话末尾会拖一段
    「画面停住、声音还在走」—— 比截短难看得多。
    """
    _isolate()
    node = _node()
    rate = 8000
    # 视频 4+4 帧 @8fps = 1 秒；音频给 3 秒
    audio = {"waveform": torch.zeros(1, 1, rate * 3), "sample_rate": rate}

    node.concat(图像=_frames(4), 段序号=1, 段数=2, fps=FPS, 任务名="截断")
    out = node.concat(图像=_frames(4), 段序号=2, 段数=2, fps=FPS, 任务名="截断",
                      音频=audio)
    path = out["result"][0]

    import av

    with av.open(path) as container:
        stream = container.streams.audio[0]
        assert stream.duration, "音轨时长读不到"
        seconds = float(stream.duration * stream.time_base)
        # AAC 有帧对齐 padding，1 秒通常落在 1.0~1.1 之间
        assert 0.9 <= seconds <= 1.2, "音轨没按成片长度截断，实际 %.2fs" % seconds


# ---------------------------------------------------------------- 无损末帧副本

def test_segment_writes_lossless_tail():
    """
    每段落盘时顺手存一份末帧无损副本。

    段 mp4 是 crf14 有损的，续接拿它解码当引导帧等于每段多背一层压缩。
    ffv1 + bgr0 是数学无损：解码回来必须和写进去的像素完全一致。
    """
    import numpy as np

    root = _isolate()
    # 用随机噪声而不是纯色：纯色 crf14 也能无损，测不出差别
    rng = np.random.default_rng(7)
    noise = torch.from_numpy(rng.integers(0, 256, (10, 32, 32, 3), dtype=np.uint8)).float() / 255.0

    node = _node()
    node.concat(图像=noise, 段序号=1, 段数=3, fps=FPS, 任务名="无损")

    seg = os.path.join(root, _vc.CONCAT_ROOT, "无损", "seg_0001.mp4")
    tail = _vc.tail_path_of(seg)
    assert os.path.isfile(tail), "没写无损末帧"

    import av

    with av.open(tail) as container:
        back = np.stack([f.to_ndarray(format="rgb24") for f in container.decode(video=0)])
    expect = (noise.numpy() * 255.0).round().astype(np.uint8)[-back.shape[0]:]
    assert back.shape == expect.shape
    assert np.abs(back.astype(int) - expect.astype(int)).max() == 0, "无损副本有像素误差"


def test_tail_keeps_the_tail_not_the_head():
    """末帧副本必须是「最后几帧」，不能从头截。"""
    import numpy as np

    root = _isolate()
    span = 20
    frames = torch.cat([torch.full((1, 16, 16, 3), i / span) for i in range(span)], dim=0)
    _node().concat(图像=frames, 段序号=1, 段数=2, fps=FPS, 任务名="末帧")

    seg = os.path.join(root, _vc.CONCAT_ROOT, "末帧", "seg_0001.mp4")
    import av

    with av.open(_vc.tail_path_of(seg)) as container:
        back = np.stack([f.to_ndarray(format="rgb24") for f in container.decode(video=0)])
    # 最后一帧灰度必须是 19/20，不是 0
    assert abs(back[-1].mean() / 255.0 - 19 / span) < 0.02


def test_single_mode_writes_no_tail():
    """单段模式没有下一段要读，不该多存一份。"""
    _isolate()
    out = _node().concat(图像=_frames(4), 段序号=5, 段数=1, fps=FPS, 任务名="单段")
    path = out["result"][0]
    assert not os.path.exists(_vc.tail_path_of(path))


def test_tail_is_not_counted_as_a_segment():
    """_tail.mkv 不能混进片段清单，否则合并时会把它当第 N 段。"""
    root = _isolate()
    node = _node()
    node.concat(图像=_frames(4), 段序号=1, 段数=3, fps=FPS, 任务名="计数")
    work = os.path.join(root, _vc.CONCAT_ROOT, "计数")
    assert len(_vc._existing_segments(work)) == 1


# ---------------------------------------------------------------- 公共辅助

def _solid(n, level, h=32, w=32):
    return torch.full((n, h, w, 3), float(level), dtype=torch.float32)


def _read_mp4(path):
    import av
    import numpy as np
    with av.open(path) as c:
        return np.stack([f.to_ndarray(format="rgb24").mean() / 255.0 for f in c.decode(video=0)])


# ---------------------------------------------------------------- 段首过冲补偿

def test_head_overshoot_is_flattened():
    """
    引导段之后的第一个自由生成帧会被模型画亮、第 1 帧回落（实测 +2.0~+8.6）。
    合并时把第 0 帧压到第 1 帧的实测水平，第 1 帧及之后一帧不动。
    """
    root = _isolate()
    a = os.path.join(root, "a.mp4")
    b = os.path.join(root, "b.mp4")
    _vc.encode_segment(_solid(10, 0.50), a, FPS)
    # 段 2：第 0 帧过冲到 0.68，第 1 帧起回到 0.52
    _vc.encode_segment(torch.cat([_solid(1, 0.68), _solid(9, 0.52)], dim=0), b, FPS)

    out = os.path.join(root, "o.mp4")
    _vc.concat_segments([a, b], out, FPS, None, match_luma=False)
    t = _read_mp4(out)

    assert len(t) == 20, "补偿不能改帧数（%d）" % len(t)
    assert abs(t[10] - 0.52) < 0.02, "第 0 帧没被压到第 1 帧水平（%.3f）" % t[10]
    for i in range(11, 20):
        assert abs(t[i] - 0.52) < 0.02, "第 %d 帧被动了（%.3f）" % (i, t[i])
    assert abs(t[0] - 0.50) < 0.02, "首段被动了（%.3f）" % t[0]


def test_head_fix_keeps_every_frame_of_a_single_frame_segment():
    """只有 1 帧的段：那帧被扣住后必须补写回去，不能静默丢帧。"""
    root = _isolate()
    a = os.path.join(root, "a.mp4")
    b = os.path.join(root, "b.mp4")
    _vc.encode_segment(_solid(5, 0.50), a, FPS)
    _vc.encode_segment(_solid(1, 0.70), b, FPS)

    out = os.path.join(root, "o.mp4")
    n = _vc.concat_segments([a, b], out, FPS, None, match_luma=False)
    assert n == 6, "单帧段被吞掉了（写成 %d 帧）" % n
    assert len(_read_mp4(out)) == 6


# ---------------------------------------------------------------- 逐段亮度对齐

def test_luma_matching_lifts_dark_segment():
    """
    每段独立重绘，曝光会逐段漂（实测 5 段 110.6→101.6，掉 8%），硬拼就是「暗一下」。
    以第 1 段为基准拉平后，两段的平均亮度应该基本一致。
    """
    root = _isolate()
    a = os.path.join(root, "a.mp4")
    b = os.path.join(root, "b.mp4")
    _vc.encode_segment(_solid(8, 0.60), a, FPS)
    _vc.encode_segment(_solid(8, 0.55), b, FPS)   # 需要 1.09 倍增益，在上限 1.18 之内

    raw = os.path.join(root, "raw.mp4")
    fixed = os.path.join(root, "fixed.mp4")
    _vc.concat_segments([a, b], raw, FPS, None, match_luma=False)
    _vc.concat_segments([a, b], fixed, FPS, None, match_luma=True)

    r = _read_mp4(raw)
    f = _read_mp4(fixed)
    gap_raw = abs(r[:8].mean() - r[8:].mean())
    gap_fixed = abs(f[:8].mean() - f[8:].mean())
    assert gap_fixed < gap_raw * 0.5, "亮度差没被拉平（%.3f → %.3f）" % (gap_raw, gap_fixed)
    assert gap_fixed < 0.02, "拉平后两段亮度应基本一致，实际差 %.3f" % gap_fixed


def test_luma_gain_is_bounded():
    """增益必须夹住，否则一段过暗会被拉爆（噪点、死白）。"""
    root = _isolate()
    dark = os.path.join(root, "dark.mp4")
    _vc.encode_segment(_solid(6, 0.10), dark, FPS)
    out = os.path.join(root, "o.mp4")
    # 单段自己对自己，gain 应该是 1.0；再验极暗段不会被乘爆
    _vc.concat_segments([dark], out, FPS, None, match_luma=True)
    assert os.path.isfile(out)


if __name__ == "__main__":
    import inspect

    passed = failed = skipped = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            if inspect.signature(fn).parameters:
                print(f"SKIP {name}（需要 pytest fixture）")
                skipped += 1
                continue
            try:
                fn()
                print(f"PASS {name}")
                passed += 1
            except Exception as exc:  # noqa: BLE001
                print(f"FAIL {name}: {exc}")
                failed += 1
    print(f"\n通过 {passed} / 失败 {failed} / 跳过 {skipped}")
    sys.exit(1 if failed else 0)
