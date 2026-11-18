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


def _span(count):
    """灰度分母：帧数超过 16 时要用更大的分母，否则 i/16 > 1 会被编码器裁成纯白。"""
    return 16 if count <= 16 else count


def _gray(index, span=16, h=32, w=32):
    """一帧纯灰，灰度值 = index/span —— 解码后靠均值就能认出是第几帧。"""
    return torch.full((1, h, w, 3), index / float(span), dtype=torch.float32)


def _write_segment(root, count, idx=1, name=NAME):
    """造一段 mp4，第 i 帧灰度 = i/span（i 从 0 开始）。"""
    path = os.path.join(root, _pf.CONCAT_ROOT, name, "seg_%04d.mp4" % idx)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    span = _span(count)
    frames = torch.cat([_gray(i, span) for i in range(count)], dim=0)
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
    assert "起始段" in out["ui"]["dogking_status"][0]


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
    """合法长度（22）要原样生效，不能被多砍。"""
    root = _isolate()
    _write_segment(root, count=30)

    image, _, _, count = _node().load_prev(段序号=2, 任务名=NAME, 取末帧数=22)["result"]
    assert count == 22
    assert abs(_mean(image, -1) - 29 / 30.0) < 0.06     # 最后一张仍是第 30 帧


def test_segment_shorter_than_requested():
    """上一段只有 3 帧时给 1 帧（AddGuide 对不足 5 帧只取第 1 张），不补帧也不报错。"""
    root = _isolate()
    _write_segment(root, count=3)

    image, has_prev, _, count = _node().load_prev(段序号=2, 任务名=NAME, 取末帧数=5)["result"]
    assert has_prev is True
    assert count == 1
    assert tuple(image.shape)[0] == 1


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


# ---------------------------------------------------------------- 引导帧长度对齐

def test_guide_frame_alignment():
    """
    H3 只认 17k+5。填 17 时 AddGuide 会砍到 5 并且只用前 5 帧（差十几帧画面），
    所以节点必须自己先对齐，交出去的批次数永远是合法的。
    """
    cases = {1: 1, 4: 1, 5: 5, 6: 5, 17: 5, 21: 5, 22: 22, 25: 22, 38: 22, 39: 39, 56: 56, 73: 73}
    for given, expect in cases.items():
        assert _pf.align_guide_frames(given) == expect, "取末帧数 %d 应对齐到 %d" % (given, expect)


def test_17_frames_falls_back_to_last_5_not_first_5():
    """填 17 时必须取「最后 5 帧」，不能交给 AddGuide 去砍成前 5 帧。"""
    root = _isolate()
    _write_segment(root, count=40)      # 40 帧，灰度 i/40

    out = _node().load_prev(段序号=2, 任务名=NAME, 取末帧数=17)
    image, has_prev, _, count = out["result"]

    assert has_prev is True
    assert count == 5, "17 应该被对齐成 5 帧"
    for k in range(5):                  # 最后 5 帧 = 第 35~39 帧
        expect = (35 + k) / 40.0
        assert abs(_mean(image, k) - expect) < 0.06, "第 %d 帧不对（%.3f vs %.3f）" % (
            k, _mean(image, k), expect
        )
    assert "17 → 按 5 帧" in out["ui"]["dogking_status"][0]


def test_22_frames_kept_whole():
    """22 是合法长度，原样取 22 帧，别多砍。"""
    root = _isolate()
    _write_segment(root, count=40)

    image, _, _, count = _node().load_prev(段序号=2, 任务名=NAME, 取末帧数=22)["result"]
    assert count == 22
    assert abs(_mean(image, 0) - 18 / 40.0) < 0.06      # 第 18 帧（40-22）
    assert abs(_mean(image, -1) - 39 / 40.0) < 0.06


def test_short_file_never_returns_illegal_length():
    """片段只有 3 帧时返回 1 帧（AddGuide 对 <5 帧只取第 1 张）。"""
    root = _isolate()
    _write_segment(root, count=3)

    image, has_prev, _, count = _node().load_prev(段序号=2, 任务名=NAME, 取末帧数=22)["result"]
    assert has_prev is True
    assert count == 1
    assert abs(_mean(image, 0) - 2 / 16.0) < 0.06


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
    # 报错信息要能指路：说清是第几段缺、缺的是第几段
    assert "第 4 段" in str(err.value)
    assert "第 3 段" in str(err.value)


def test_first_segment_never_raises_even_when_asked():
    """
    段 1 天然没有上一段，选「报错」也不能抛 —— 否则一开局就废掉整条流。

    实际踩过：缺段时=报错 时跑第 1 段直接 ValueError，任务队列红一片。
    """
    _isolate()
    out = _node().load_prev(段序号=1, 任务名=NAME, 缺段时="报错")
    image, has_prev, source, count = out["result"]

    assert has_prev is False
    assert source == ""
    assert count == 1
    assert tuple(image.shape)[0] == 1
    assert "起始段" in out["ui"]["dogking_status"][0]


def test_single_segment_mode_never_raises_even_when_asked():
    """单段模式同理：段数=1 是正常调试，不该报错。"""
    _isolate()
    out = _node().load_prev(段序号=5, 任务名=NAME, 缺段时="报错", 段数=1)
    assert out["result"][1] is False


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
    assert count == 1
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


# ---------------------------------------------------------------- 无损末帧副本

def test_tail_suffix_matches_concat():
    """两个节点必须用同一个后缀，否则永远读不到无损副本。"""
    assert _pf.TAIL_SUFFIX == _vc.TAIL_SUFFIX
    assert _pf.tail_path_of("a/seg_0004.mp4") == _pf.tail_path_of("a/seg_0004.mp4")
    assert _pf.tail_path_of("a/seg_0004.mp4").endswith("seg_0004_tail.mkv")


def test_prefers_lossless_tail_over_mp4():
    """
    有 _tail.mkv 时优先读它。

    mp4 是 crf14 有损的，续接拿它当引导帧等于每段多背一层压缩；
    这里让两者内容不同，断言读到的是 tail 那份。
    """
    root = _isolate()
    seg = os.path.join(root, _pf.CONCAT_ROOT, NAME, "seg_0001.mp4")
    os.makedirs(os.path.dirname(seg))

    # mp4 里是「接近黑」，tail 里是「接近白」—— 一眼能区分读到哪份
    _vc.encode_segment(torch.cat([_gray(0)] * 6, dim=0), seg, FPS)
    _vc.encode_tail_lossless(torch.cat([_gray(14)] * 8, dim=0), seg, FPS)

    image, has_prev, source, count = _node().load_prev(段序号=2, 任务名=NAME)["result"]

    assert has_prev is True
    assert source.endswith("_tail.mkv")
    assert count == 5
    assert _mean(image, 0) > 0.7, "读到的还是 mp4（有损那份），没走无损副本"


def test_falls_back_to_mp4_when_tail_missing():
    """老片段没有 tail 文件时仍然能用，退回读 mp4。"""
    root = _isolate()
    _write_segment(root, count=8)

    _, has_prev, source, count = _node().load_prev(段序号=2, 任务名=NAME)["result"]
    assert has_prev is True
    assert source.endswith("seg_0001.mp4")
    assert count == 5


def test_is_changed_tracks_tail_file():
    """重跑上一段会重写 tail，签名必须跟着变。"""
    root = _isolate()
    seg = os.path.join(root, _pf.CONCAT_ROOT, NAME, "seg_0001.mp4")
    os.makedirs(os.path.dirname(seg))
    _vc.encode_segment(torch.cat([_gray(3)] * 6, dim=0), seg, FPS)

    sig_mp4 = _pf.DogkingPrevSegmentFrames.IS_CHANGED(段序号=2, 任务名=NAME)
    _vc.encode_tail_lossless(torch.cat([_gray(3)] * 6, dim=0), seg, FPS)
    sig_tail = _pf.DogkingPrevSegmentFrames.IS_CHANGED(段序号=2, 任务名=NAME)

    assert sig_mp4 != sig_tail, "出现了 tail 但签名没变，会一直读旧来源"
    assert sig_tail.endswith("|0") or "|missing" not in sig_tail


# ---------------------------------------------------------------- 去锐化（抗累积过锐）

def _noisy_image(n=6, h=64, w=64, seed=0):
    """平滑渐变 + 细噪声：高频能量可控，便于验证衰减比例。"""
    g = torch.linspace(0, 1, h).view(1, h, 1, 1).expand(n, h, w, 3)
    gen = torch.Generator().manual_seed(seed)
    noise = torch.rand(n, h, w, 3, generator=gen) * 0.25
    return (g + noise).clamp(0, 1)


def test_dehaze_zero_is_identity():
    """去锐化=0 时必须字节级原样，不能顺手糊一下。"""
    img = _noisy_image()
    assert torch.equal(_pf.attenuate_high_freq(img, 0.0), img)


def test_dehaze_scales_high_freq_by_amount():
    """高频能量应当按 (1-amount) 缩放：这是让环路增益小于 1 的唯一依据。"""
    img = _noisy_image()
    base = _pf.high_freq_energy(img)
    for amount in (0.2, 0.35, 0.5):
        got = _pf.high_freq_energy(_pf.attenuate_high_freq(img, amount))
        expect = base * (1.0 - amount)
        assert abs(got - expect) / base < 0.06, (
            "amount=%.2f 实际 %.5f，应约 %.5f" % (amount, got, expect)
        )


def test_dehaze_keeps_low_freq():
    """低频（构图、颜色、明暗）不能被改动，否则接缝会跳。"""
    img = _noisy_image()
    low_a = _pf._blur(img, sigma=1.0)
    low_b = _pf._blur(_pf.attenuate_high_freq(img, 0.5), sigma=1.0)
    assert float((low_a - low_b).abs().max()) < 0.02
    # 整体均值也要保持一致（不能整体变暗/变亮）
    assert abs(float(img.mean()) - float(_pf.attenuate_high_freq(img, 0.5).mean())) < 0.01


def test_dehaze_reported_in_status():
    """状态行要写清削了多少，否则用户没法判断到底有没有生效。"""
    root = _isolate()
    _write_segment(root, count=20)

    out = _node().load_prev(段序号=2, 任务名=NAME, 去锐化=0.35)
    assert "去锐化 0.35" in out["ui"]["dogking_status"][0]
    assert "高频 -" in out["ui"]["dogking_status"][0]

    out2 = _node().load_prev(段序号=2, 任务名=NAME, 去锐化=0.0)
    assert "去锐化" not in out2["ui"]["dogking_status"][0]


def test_dehaze_from_workflow_default_still_five_frames():
    """去锐化不能改变帧数和对齐结果。"""
    root = _isolate()
    _write_segment(root, count=40)
    a = _node().load_prev(段序号=2, 任务名=NAME, 取末帧数=22, 去锐化=0.0)["result"]
    b = _node().load_prev(段序号=2, 任务名=NAME, 取末帧数=22, 去锐化=0.35)["result"]
    assert a[3] == b[3] == 22
    assert tuple(a[0].shape) == tuple(b[0].shape)


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
