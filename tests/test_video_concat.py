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
                保留片段=True)
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
