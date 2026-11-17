# -*- coding: utf-8 -*-
"""
audio_split.py 测试（数字人音频分段）

守四件事：

  ① **分段表容错** —— 前端拖动过程中会写进 `start >= end`、null、字符串数字，
     这些必须被安静丢掉，不能抛异常（否则用户手一抖节点就红）。
  ② **切片准确** —— 秒 → 采样点的换算不能差一帧，越界要裁到文件长度。
  ③ **游标** —— 顺序模式走完一轮归零；改切点重置，只改提示词**不**重置。
  ④ **提示词覆盖** —— 段内有词就用段内，空着才回退全局（含全是空白的坑）。

全部用假音频（torch 张量），不读真实文件，也不写用户 state.json。
"""

import importlib.util
import json
import os
import sys
import wave

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 插件根目录


def _load(name, alias):
    spec = importlib.util.spec_from_file_location(alias, os.path.join(HERE, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_av = _load("audio_split", "dk_audio_split_for_test")

torch = pytest.importorskip("torch")


@pytest.fixture(autouse=True)
def _clean_state():
    """每个用例前清空内存态游标，避免互相干扰。"""
    _av._MEM_STATE.clear()
    yield
    _av._MEM_STATE.clear()


def _audio(seconds=2.0, rate=8000):
    return {
        "waveform": torch.zeros(1, 1, int(round(seconds * rate))),
        "sample_rate": rate,
    }


def _segments(*pairs):
    return json.dumps({"segments": [{"start": a, "end": b, "prompt": p} for a, b, p in pairs]})


# ---------------------------------------------------------------- ① 解析

def test_parse_normal():
    segs = _av.parse_segments(_segments((0, 1.5, ""), (1.5, 3.0, "唱歌")))
    assert len(segs) == 2
    assert segs[0] == {"start": 0.0, "end": 1.5, "prompt": ""}
    assert segs[1]["prompt"] == "唱歌"


def test_parse_sorts_by_start():
    segs = _av.parse_segments(_segments((5, 6, ""), (0, 2, "")))
    assert [s["start"] for s in segs] == [0.0, 5.0]


def test_parse_drops_broken_entries():
    raw = json.dumps({"segments": [
        {"start": 2, "end": 1},          # 倒序 → 丢
        {"start": 1, "end": 1},          # 零长 → 丢
        {"start": None, "end": 3},       # 非数字 → 丢
        {"start": 0, "end": 1},          # 正常
    ]})
    segs = _av.parse_segments(raw)
    assert len(segs) == 1


def test_parse_accepts_string_numbers_and_negative_start():
    segs = _av.parse_segments('{"segments":[{"start":"-0.5","end":"2.5"}]}')
    assert len(segs) == 1
    assert segs[0]["start"] == 0.0          # 负数夹到 0
    assert segs[0]["end"] == 2.5


def test_parse_tolerates_garbage():
    for raw in ("", "{}", "null", "[]", "not json", None, 42):
        assert _av.parse_segments(raw) == []


def test_parse_accepts_bare_list():
    segs = _av.parse_segments([{"start": 0, "end": 1}])
    assert len(segs) == 1


# ---------------------------------------------------------------- ② 指纹 / 提示词

def test_signature_changes_with_cut_points():
    a = _av.parse_segments(_segments((0, 5, "")))
    b = _av.parse_segments(_segments((0, 4, ""), (4, 5, "")))
    assert _av.timeline_signature(a) != _av.timeline_signature(b)


def test_signature_ignores_prompt_edits():
    """只改某段提示词不该重置游标 —— 否则改一个错字就得从头跑。"""
    a = _av.parse_segments(_segments((0, 5, "")))
    b = _av.parse_segments(_segments((0, 5, "换了个词")))
    assert _av.timeline_signature(a) == _av.timeline_signature(b)


def test_prompt_priority():
    seg = {"start": 0, "end": 1, "prompt": "段内词"}
    assert _av.resolve_prompt(seg, "全局词") == "段内词"
    assert _av.resolve_prompt({"prompt": ""}, "全局词") == "全局词"
    # 全是空白 ≠ 有独立提示词（输入框点一下再点走很容易留空格）
    assert _av.resolve_prompt({"prompt": "   "}, "全局词") == "全局词"
    assert _av.resolve_prompt({"prompt": ""}, "") == ""


# ---------------------------------------------------------------- ③ 切片

def test_slice_length():
    audio = _audio(2.0, 8000)
    piece = _av.slice_audio(audio, 0.5, 1.25)
    # 0.75 秒 @ 8000 = 6000 样本
    assert int(piece["waveform"].shape[-1]) == 6000
    assert piece["sample_rate"] == 8000


def test_slice_clamps_beyond_end():
    audio = _audio(1.0, 8000)
    piece = _av.slice_audio(audio, 0.5, 99.0)
    assert int(piece["waveform"].shape[-1]) == 4000     # 裁到文件末尾


def test_slice_negative_start():
    audio = _audio(1.0, 8000)
    piece = _av.slice_audio(audio, -3, 0.5)
    assert int(piece["waveform"].shape[-1]) == 4000


def test_slice_empty_raises():
    audio = _audio(1.0, 8000)
    with pytest.raises(ValueError):
        _av.slice_audio(audio, 1.0, 1.0)


def test_slice_does_not_touch_source():
    audio = _audio(1.0, 8000)
    before = audio["waveform"].clone()
    piece = _av.slice_audio(audio, 0.1, 0.2)
    piece["waveform"] += 1.0            # 改切片
    assert torch.equal(audio["waveform"], before)


def test_to_comfy_audio_shapes():
    assert _av.to_comfy_audio(torch.zeros(100), 8000)["waveform"].shape == (1, 1, 100)
    assert _av.to_comfy_audio(torch.zeros(2, 100), 8000)["waveform"].shape == (1, 2, 100)
    assert _av.to_comfy_audio(torch.zeros(1, 2, 100), 8000)["waveform"].shape == (1, 2, 100)


def test_audio_duration():
    assert _av.audio_duration(_audio(2.5, 8000)) == pytest.approx(2.5)
    assert _av.audio_duration({}) == 0.0


# ---------------------------------------------------------------- ④ 节点

def _run(node, segments, mode="顺序", prompt="全局词", audio=None, uid="t1", auto=False, cur=1):
    return node.split(
        分段数据=segments,
        全局提示词=prompt,
        输出模式=mode,
        当前段=cur,
        自动连续=auto,
        文件路径="",
        音频=audio if audio is not None else _audio(3.0),
        unique_id=uid,
    )


def test_node_sequence_walks_and_wraps():
    node = _av.DogkingAudioSplit()
    segs = _segments((0, 1, ""), (1, 2, ""), (2, 3, ""))

    seen = [_run(node, segs)["result"][3] for _ in range(4)]
    # 三轮走完，第四轮回到第一段
    assert seen == [1, 2, 3, 1]


def test_node_single_mode_ignores_cursor():
    node = _av.DogkingAudioSplit()
    segs = _segments((0, 1, ""), (1, 2, ""))
    out = _run(node, segs, mode="单段", cur=2)
    assert out["result"][3] == 2
    # 连跑两次都是第 2 段（单段模式不推进游标）
    assert _run(node, segs, mode="单段", cur=2)["result"][3] == 2


def test_node_single_mode_clamps_cursor():
    node = _av.DogkingAudioSplit()
    segs = _segments((0, 1, ""), (1, 2, ""))
    assert _run(node, segs, mode="单段", cur=99)["result"][3] == 2


def test_node_cut_change_resets_cursor():
    node = _av.DogkingAudioSplit()
    segs_a = _segments((0, 1, ""), (1, 2, ""), (2, 3, ""))
    _run(node, segs_a)                      # 游标推到 1
    _run(node, segs_a)                      # 推到 2
    segs_b = _segments((0, 1.5, ""), (1.5, 3, ""))   # 切点变了
    assert _run(node, segs_b)["result"][3] == 1


def test_node_prompt_edits_keep_cursor():
    node = _av.DogkingAudioSplit()
    segs_a = _segments((0, 1, ""), (1, 2, ""), (2, 3, ""))
    _run(node, segs_a)                      # → 1
    segs_b = _segments((0, 1, "新词"), (1, 2, ""), (2, 3, ""))
    # 只改了提示词：游标继续，出第 2 段
    assert _run(node, segs_b)["result"][3] == 2


def test_node_prompt_fallback_and_override():
    node = _av.DogkingAudioSplit()
    segs = _segments((0, 1, "独立词"), (1, 2, ""))
    out1 = _run(node, segs, mode="单段", cur=1, prompt="全局词")
    assert out1["result"][1] == "独立词"
    assert out1["result"][2] == "全局词"     # 全局原样透传
    out2 = _run(node, segs, mode="单段", cur=2, prompt="全局词")
    assert out2["result"][1] == "全局词"


def test_node_returns_sliced_audio():
    node = _av.DogkingAudioSplit()
    segs = _segments((0.5, 1.0, ""))
    out = _run(node, segs, mode="单段", audio=_audio(3.0, 8000))
    assert int(out["result"][0]["waveform"].shape[-1]) == 4000
    assert out["result"][5] == pytest.approx(0.5)


def test_node_errors_are_chinese_and_actionable():
    node = _av.DogkingAudioSplit()
    with pytest.raises(ValueError) as err:
        _run(node, "{}")                     # 没有分段
    assert "还没有分段" in str(err.value)

    with pytest.raises(ValueError) as err2:
        node.split(分段数据=_segments((0, 1, "")), 全局提示词="", 输出模式="单段",
                   当前段=1, 自动连续=False, 文件路径="", 音频=None, unique_id="x")
    assert "没有音频" in str(err2.value)


def test_node_status_and_badge():
    node = _av.DogkingAudioSplit()
    out = _run(node, _segments((0, 0.5, "独立词"), (0.5, 1, "")), mode="单段", cur=1)
    assert "第 1/2 段" in out["ui"]["dogking_status"][0]
    assert "独立提示词" in out["ui"]["dogking_status"][0]
    assert out["ui"]["dogking_badge"][0].startswith("1/2")


def test_is_changed_always_dirty():
    """顺序模式的游标是外部状态，必须每次都重跑，不能吃缓存。"""
    value = _av.DogkingAudioSplit.IS_CHANGED(分段数据="", 全局提示词="")
    assert value != value                    # NaN 的经典自检


# ---------------------------------------------------------------- ⑤ 波形峰值

def test_compute_peaks_from_wav():
    """真写一个 wav，验证时长、峰值数量与归一化。"""
    import math
    import tempfile

    path = os.path.join(tempfile.mkdtemp(prefix="dk_peaks_"), "tone.wav")
    rate = 8000
    seconds = 1.0
    frames = int(rate * seconds)
    with wave.open(path, "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        data = bytearray()
        for i in range(frames):
            value = int(12000 * math.sin(2 * math.pi * 220 * i / rate))
            data += int(value).to_bytes(2, "little", signed=True)
        fh.writeframes(bytes(data))

    duration, got_rate, peaks, raw = _av.compute_peaks(path, buckets=200)
    assert duration == pytest.approx(seconds, abs=1e-3)
    assert got_rate == rate
    assert len(peaks) == 200
    assert max(peaks) == pytest.approx(1.0)     # 归一化后最大值为 1
    assert raw > 0.2                            # 原始峰值合理（12000/32767 ≈ 0.37）


def test_compute_peaks_more_buckets_than_samples_is_clamped():
    import tempfile

    path = os.path.join(tempfile.mkdtemp(prefix="dk_peaks2_"), "tiny.wav")
    with wave.open(path, "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(8000)
        fh.writeframes(b"\x00\x00" * 10)        # 只有 10 个样本
    _duration, _rate, peaks, _raw = _av.compute_peaks(path, buckets=500)
    assert len(peaks) == 10                     # 桶数不能超过样本数


# ---------------------------------------------------------------- 直接运行

if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            # pytest 里靠 autouse fixture 清游标，直接运行时没有 fixture，手动清
            _av._MEM_STATE.clear()
            try:
                fn()
                print(f"PASS {name}")
                passed += 1
            except Exception as exc:  # noqa: BLE001
                print(f"FAIL {name}: {exc}")
                failed += 1
    print(f"\n通过 {passed} / 失败 {failed}")
    sys.exit(1 if failed else 0)
