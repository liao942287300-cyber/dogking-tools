# -*- coding: utf-8 -*-
"""
prev_frames.py 测试（上一段末帧续接）

守四件事：

  ① **段号自动减一** —— 段 5 读 seg_0004.mp4，不能读成别的段，否则续接接错画面。
  ② **第 1 段不炸** —— 单段模式 / 首段没有上一段，必须给占位帧 + False，照常能跑。
  ③ **末帧是真的末帧** —— 按内容验证，不看帧数对不对就算过（取错帧最难排查）。
  ④ **重跑上一段要刷新缓存** —— IS_CHANGED 不带文件签名的话，永远拿到旧引导帧。

不写用户 output 目录：每个用例都把 _output_dir 指向临时目录。
"""

import importlib.util
import os
import tempfile

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 插件根目录


def _load(name, alias):
    spec = importlib.util.spec_from_file_location(alias, os.path.join(HERE, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_vc = _load("video_concat", "dk_video_concat_for_prev_test")
_pf = _load("prev_frames", "dk_prev_frames_for_test")

torch = pytest.importorskip("torch")
pytest.importorskip("av")


FPS = 8
NAME = "续接测试"


def _isolate(tag="pf"):
    root = tempfile.mkdtemp(prefix="dk_%s_" % tag)
    _pf._output_dir = lambda: root
    return root


def _gray(index, h=32, w=32):
    """一帧纯灰，灰度值由 index 决定 —— 解码后靠均值就能认出是第几帧。"""
    return torch.full((1, h, w, 3), index / 16.0, dtype=torch.float32)


def _write_segment(root, count, idx=1, name=NAME):
    """造一段 mp4，第 i 帧灰度 = i/16（i 从 0 开始）。"""
    path = os.path.join(root, _pf.CONCAT_ROOT, name, "seg_%04d.mp4" % idx)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    frames = torch.cat([_gray(i) for i in range(count)], dim=0)
    _vc.encode_segment(frames, path, FPS)
    return path


def _node():
    return _pf.DogkingPrevSegmentFrames()


def _mean(image, i):
    return float(image[i].mean())


# ---------------------------------------------------------------- 常量一致

def test_constants_match_video_concat():
    """目录名 / 任务名清洗规则必须和「分段视频合并」一致，否则找不到片段。"""
    assert _pf.CONCAT_ROOT == _vc.CONCAT_ROOT
    for text in ["数字人成片", "a/b:c*d", "  收尾  ", ""]:
        assert _pf._safe_name(text) == _vc._safe_name(text)


# ---------------------------------------------------------------- 首段

def test_first_segment_returns_placeholder():
    _isolate()
    out = _node().load_prev(段序号=1, 任务名=NAME)
    image, has_prev, source, count = out["result"]

    assert has_prev is False
    assert source == ""
    assert count == 1
    assert tuple(image.shape) == (1, _pf.PLACEHOLDER_SIZE, _pf.PLACEHOLDER_SIZE, 3)
    assert "无上一段" in out["ui"]["dogking_status"][0]


def test_first_segment_never_touches_disk(tmp_path):
    """段 1 不该去找 seg_0000.mp4，也不该报错。"""
    root = _isolate()
    out = _node().load_prev(段序号=1, 任务名=NAME)
    assert out["result"][1] is False
    assert not os.path.isdir(os.path.join(root, _pf.CONCAT_ROOT, NAME))


# ---------------------------------------------------------------- 取帧

def test_reads_last_frames_of_previous_segment():
    root = _isolate()
    _write_segment(root, count=10)          # seg_0001，帧灰度 0/16 … 9/16

    image, has_prev, source, count = _node().load_prev(段序号=2, 任务名=NAME)["result"]

    assert has_prev is True
    assert count == 5
    assert os.path.basename(source) == "seg_0001.mp4"
    assert tuple(image.shape)[0] == 5
    # 取的必须是最后 5 帧（帧 5~9），不是前 5 帧
    for k in range(5):
        expect = (5 + k) / 16.0
        assert abs(_mean(image, k) - expect) < 0.06, "第 %d 帧不是末帧（%.3f vs %.3f）" % (
            k, _mean(image, k), expect
        )


def test_respects_frame_count():
    root = _isolate()
    _write_segment(root, count=9)

    image, _, _, count = _node().load_prev(段序号=2, 任务名=NAME, 取末帧数=3)["result"]
    assert count == 3
    assert abs(_mean(image, -1) - 8 / 16.0) < 0.06     # 最后一张仍是第 9 帧


def test_segment_shorter_than_requested():
    """上一段只有 3 帧时给 3 帧，不补帧也不报错。"""
    root = _isolate()
    _write_segment(root, count=3)

    image, has_prev, _, count = _node().load_prev(段序号=2, 任务名=NAME, 取末帧数=5)["result"]
    assert has_prev is True
    assert count == 3
    assert tuple(image.shape)[0] == 3


def test_image_dtype_and_range():
    root = _isolate()
    _write_segment(root, count=6)

    image, _, _, _ = _node().load_prev(段序号=2, 任务名=NAME)["result"]
    assert image.dtype == torch.float32
    assert float(image.min()) >= 0.0 and float(image.max()) <= 1.0


def test_reads_the_segment_before_the_given_index():
    """段 3 必须读 seg_0002，不能顺手读成 seg_0001。"""
    root = _isolate()
    _write_segment(root, count=4, idx=1)
    _write_segment(root, count=4, idx=2)

    _, has_prev, source, _ = _node().load_prev(段序号=3, 任务名=NAME)["result"]
    assert has_prev is True
    assert os.path.basename(source) == "seg_0002.mp4"


def test_single_mode_forces_no_guide():
    """
    段数=1（单段模式）时不续接。

    单段模式是在单独调某一段，磁盘上的 seg_0004 属于上一轮批量任务，
    接上去的画面和这次要生成的段对不上 —— 宁可无引导。
    """
    root = _isolate()
    _write_segment(root, count=8, idx=4)

    out = _node().load_prev(段序号=5, 任务名=NAME, 段数=1)
    image, has_prev, source, count = out["result"]

    assert has_prev is False
    assert source == ""
    assert tuple(image.shape)[0] == 1
    assert "单段模式" in out["ui"]["dogking_status"][0]


def test_batch_mode_still_guides():
    """段数 > 1 时正常续接，别被单段判断误伤。"""
    root = _isolate()
    _write_segment(root, count=6, idx=4)

    _, has_prev, source, count = _node().load_prev(段序号=5, 任务名=NAME, 段数=8)["result"]
    assert has_prev is True
    assert count == 5
    assert os.path.basename(source) == "seg_0004.mp4"


# ---------------------------------------------------------------- 缺段

def test_missing_segment_placeholder_by_default():
    _isolate()
    out = _node().load_prev(段序号=4, 任务名="从没跑过")
    image, has_prev, source, _ = out["result"]

    assert has_prev is False
    assert source == ""
    assert tuple(image.shape)[0] == 1
    assert "找不到上一段" in out["ui"]["dogking_status"][0]


def test_missing_segment_raises_when_asked():
    _isolate()
    with pytest.raises(ValueError) as err:
        _node().load_prev(段序号=4, 任务名="从没跑过", 缺段时="报错")
    assert "找不到上一段" in str(err.value)


def test_audio_only_file_raises():
    """没有视频流的文件要明确报错，不能静默返回 0 帧。"""
    import av

    root = _isolate()
    folder = os.path.join(root, _pf.CONCAT_ROOT, NAME)
    os.makedirs(folder)
    path = os.path.join(folder, "seg_0001.mp4")
    container = av.open(path, "w")
    try:
        stream = container.add_stream("aac", rate=8000)
        stream.layout = "mono"
        import numpy as np

        block = np.zeros((1, 1024), dtype=np.float32)
        for _ in range(4):
            frame = av.AudioFrame.from_ndarray(block, format="fltp", layout="mono")
            frame.sample_rate = 8000
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    finally:
        container.close()

    with pytest.raises(ValueError) as err:
        _node().load_prev(段序号=2, 任务名=NAME)
    assert "没有视频流" in str(err.value)


# ---------------------------------------------------------------- 目录 / 缓存

def test_custom_parts_root():
    root = _isolate()
    custom = os.path.join(tempfile.mkdtemp(prefix="dk_pf_custom_"), _pf.CONCAT_ROOT)
    os.makedirs(os.path.join(custom, NAME))
    frames = torch.cat([_gray(0), _gray(4), _gray(8)], dim=0)
    _vc.encode_segment(frames, os.path.join(custom, NAME, "seg_0001.mp4"), FPS)

    _, has_prev, source, count = _node().load_prev(
        段序号=2, 任务名=NAME, 片段根目录=custom
    )["result"]
    assert has_prev is True
    assert count == 3
    assert source.startswith(custom)


def test_is_changed_tracks_file_rewrite(tmp_path):
    """
    重跑上一段之后，签名必须变 —— 否则 ComfyUI 复用旧缓存，接缝还是上一个版本。
    """
    root = _isolate()
    sig_missing = _pf.DogkingPrevSegmentFrames.IS_CHANGED(段序号=2, 任务名=NAME)
    assert "|missing" in sig_missing

    path = _write_segment(root, count=4)
    sig_a = _pf.DogkingPrevSegmentFrames.IS_CHANGED(段序号=2, 任务名=NAME)
    assert "|missing" not in sig_a

    # 改大小 + 重写，模拟重跑上一段
    os.utime(path, None)
    _write_segment(root, count=6)
    sig_b = _pf.DogkingPrevSegmentFrames.IS_CHANGED(段序号=2, 任务名=NAME)
    assert sig_a != sig_b, "片段被重写但签名没变，会一直吃旧缓存"


def test_node_is_registered():
    assert "DogkingPrevSegmentFrames" in _pf.NODE_CLASS_MAPPINGS
    assert _pf.NODE_DISPLAY_NAME_MAPPINGS["DogkingPrevSegmentFrames"].endswith("dogking-tools）")


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
    raise SystemExit(1 if failed else 0)
