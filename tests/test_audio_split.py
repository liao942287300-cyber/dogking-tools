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


def test_parse_ignores_range_key():
    """
    前端现在会往「分段数据」里多写一个 range（选区/裁剪窗口）。

    后端只按 segments 切音频 —— range 只是前端的框选状态，真正的裁剪已经体现在
    每段的 start/end 上了。多出来这个 key 不能被当成坏数据。
    """
    raw = json.dumps({
        "range": {"start": 6.0, "end": 20.0},
        "segments": [
            {"start": 6.0, "end": 9.0, "prompt": "甲"},
            {"start": 9.0, "end": 12.0, "prompt": ""},
        ],
    }, ensure_ascii=False)
    segs = _av.parse_segments(raw)
    assert [s["start"] for s in segs] == [6.0, 9.0]
    assert segs[0]["prompt"] == "甲"


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

    seen = [_run(node, segs)["result"][2] for _ in range(4)]
    # 三轮走完，第四轮回到第一段
    assert seen == [1, 2, 3, 1]


def test_node_single_mode_ignores_cursor():
    node = _av.DogkingAudioSplit()
    segs = _segments((0, 1, ""), (1, 2, ""))
    out = _run(node, segs, mode="单段", cur=2)
    assert out["result"][2] == 2
    # 连跑两次都是第 2 段（单段模式不推进游标）
    assert _run(node, segs, mode="单段", cur=2)["result"][2] == 2


def test_node_single_mode_clamps_cursor():
    node = _av.DogkingAudioSplit()
    segs = _segments((0, 1, ""), (1, 2, ""))
    assert _run(node, segs, mode="单段", cur=99)["result"][2] == 2


def test_node_out_total_reports_run_size():
    """
    「段数」口报的是**这次产出几段**，不是切了几段：
    单段模式恒为 1（一次只吐一段），顺序模式才是总段数。
    拿它当循环次数 / 批量上限时，单段不该被「切了 8 段」牵连。
    """
    node = _av.DogkingAudioSplit()
    segs = _segments((0, 1, ""), (1, 2, ""), (2, 3, ""))

    for cur in (1, 2, 3):
        out = _run(node, segs, mode="单段", cur=cur)
        assert out["result"][3] == 1, "单段模式的段数必须是 1"
        assert out["result"][2] == cur, "段序号仍报真实段号"

    assert _run(node, segs, mode="顺序", cur=1)["result"][3] == 3


def test_node_cut_change_resets_cursor():
    node = _av.DogkingAudioSplit()
    segs_a = _segments((0, 1, ""), (1, 2, ""), (2, 3, ""))
    _run(node, segs_a)                      # 游标推到 1
    _run(node, segs_a)                      # 推到 2
    segs_b = _segments((0, 1.5, ""), (1.5, 3, ""))   # 切点变了
    assert _run(node, segs_b)["result"][2] == 1


def test_node_prompt_edits_keep_cursor():
    node = _av.DogkingAudioSplit()
    segs_a = _segments((0, 1, ""), (1, 2, ""), (2, 3, ""))
    _run(node, segs_a)                      # → 1
    segs_b = _segments((0, 1, "新词"), (1, 2, ""), (2, 3, ""))
    # 只改了提示词：游标继续，出第 2 段
    assert _run(node, segs_b)["result"][2] == 2


def test_node_prompt_fallback_and_override():
    node = _av.DogkingAudioSplit()
    segs = _segments((0, 1, "独立词"), (1, 2, ""))
    out1 = _run(node, segs, mode="单段", cur=1, prompt="全局词")
    assert out1["result"][1] == "独立词"
    out2 = _run(node, segs, mode="单段", cur=2, prompt="全局词")
    assert out2["result"][1] == "全局词"


def test_node_returns_sliced_audio():
    node = _av.DogkingAudioSplit()
    segs = _segments((0.5, 1.0, ""))
    out = _run(node, segs, mode="单段", audio=_audio(3.0, 8000))
    assert int(out["result"][0]["waveform"].shape[-1]) == 4000
    assert out["result"][4] == pytest.approx(0.5)


def test_output_ports_have_single_prompt_port():
    """
    输出口只有**一个**「提示词」，不再单独开「全局提示词」。

    两个口摆一起的实际后果：用户接到「全局提示词」那一路，全局词就把段内独立词
    全顶掉了，「独立词覆盖全局」这个功能等于白做。合并成一个口，它吐的永远是
    「该段最终该用的词」。
    """
    cls = _av.DogkingAudioSplit
    assert "全局提示词" not in cls.RETURN_NAMES
    assert cls.RETURN_NAMES.count("提示词") == 1
    assert len(cls.RETURN_TYPES) == len(cls.RETURN_NAMES) == 5

    segs = _segments((0, 1, "独立词"), (1, 2, ""))
    node = cls()
    with_prompt = _run(node, segs, mode="单段", cur=1, prompt="全局词")
    without_prompt = _run(node, segs, mode="单段", cur=2, prompt="全局词")
    assert len(with_prompt["result"]) == len(cls.RETURN_NAMES)
    assert with_prompt["result"][1] == "独立词"      # 有独立词 → 出独立词
    assert without_prompt["result"][1] == "全局词"   # 没独立词 → 出全局词


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


# ---------------------------------------------------------------- 解码链
#
# 🔴 这一组守的是「别再走 torchaudio」：这台机器的 torchaudio 把解码交给
#    torchcodec，而它的 DLL 和 torch 版本对不上 → 弹「无法定位程序输入点」，
#    模态框不点掉请求就卡死，面板永远停在「解码中…」。


def _tiny_wav(prefix="dk_decode_"):
    """写一个 0.25 秒的立体声 wav，返回路径。"""
    import math
    import tempfile

    path = os.path.join(tempfile.mkdtemp(prefix=prefix), "tone.wav")
    rate = 8000
    with wave.open(path, "wb") as fh:
        fh.setnchannels(2)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        data = bytearray()
        for i in range(int(rate * 0.25)):
            value = int(8000 * math.sin(2 * math.pi * 220 * i / rate))
            data += int(value).to_bytes(2, "little", signed=True) * 2
        fh.writeframes(bytes(data))
    return path, rate


def test_load_waveform_never_touches_torchaudio():
    """解码链里不能再出现 torchaudio —— 它一被 import 就可能触发 torchcodec。"""
    import ast

    src = open(os.path.join(HERE, "audio_split.py"), encoding="utf-8").read()
    tree = ast.parse(src)

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names]
            if isinstance(node, ast.ImportFrom):
                names.append(node.module or "")
            assert not any("torchaudio" in n for n in names), (
                "audio_split.py 又 import torchaudio 了 —— 这台机器上它会拉起 torchcodec "
                "并弹「无法定位程序输入点」的模态框，面板会卡在「解码中…」"
            )
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "load"
            and isinstance(node.value, ast.Name)
            and node.value.id == "torchaudio"
        ):
            raise AssertionError("又用 torchaudio.load() 解码了")

    path, rate = _tiny_wav()
    wav, got_rate = _av.load_waveform(path)
    assert got_rate == rate
    assert tuple(wav.shape) == (2, int(rate * 0.25))


def test_load_waveform_falls_back_to_av(monkeypatch):
    """soundfile 读不了（比如某些容器）时，PyAV 要能顶上。"""
    import soundfile

    real_read = soundfile.read

    def _boom(*_a, **_kw):
        raise RuntimeError("libsndfile 不支持这个格式")

    monkeypatch.setattr(soundfile, "read", _boom)
    path, rate = _tiny_wav("dk_decode_av_")
    try:
        wav, got_rate = _av.load_waveform(path)
    finally:
        monkeypatch.setattr(soundfile, "read", real_read)

    assert got_rate == rate
    assert tuple(wav.shape) == (2, int(rate * 0.25))


def test_load_waveform_falls_back_to_stdlib_wave(monkeypatch):
    """soundfile 和 PyAV 都不可用时，纯 wav 还得能读。"""
    import builtins

    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name in ("soundfile", "av"):
            raise ImportError("模拟没装 %s" % name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    path, rate = _tiny_wav("dk_decode_wave_")
    try:
        wav, got_rate = _av.load_waveform(path)
    finally:
        monkeypatch.setattr(builtins, "__import__", real_import)

    assert got_rate == rate
    assert tuple(wav.shape) == (2, int(rate * 0.25))
    assert float(wav.abs().max()) > 0.1


def test_load_waveform_reports_all_failures():
    """三种方式全失败时报错要带上各自原因，别只说一句「解码失败」。"""
    import tempfile

    path = os.path.join(tempfile.mkdtemp(prefix="dk_decode_bad_"), "bad.mp3")
    with open(path, "wb") as fh:
        fh.write(b"not audio at all")
    with pytest.raises(ValueError) as err:
        _av.load_waveform(path)
    message = str(err.value)
    assert "soundfile" in message and "pyav" in message and "wave" in message



# ---------------------------------------------------------------- 直接运行

if __name__ == "__main__":
    import inspect

    passed = failed = skipped = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            # 依赖 pytest fixture（monkeypatch 等）的用例直接运行时没法给参数，跳过
            if inspect.signature(fn).parameters:
                print(f"SKIP {name}（需要 pytest fixture）")
                skipped += 1
                continue
            # pytest 里靠 autouse fixture 清游标，直接运行时没有 fixture，手动清
            _av._MEM_STATE.clear()
            try:
                fn()
                print(f"PASS {name}")
                passed += 1
            except Exception as exc:  # noqa: BLE001
                print(f"FAIL {name}: {exc}")
                failed += 1
    print(f"\n通过 {passed} / 失败 {failed} / 跳过 {skipped}")
    sys.exit(1 if failed else 0)
