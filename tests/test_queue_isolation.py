# -*- coding: utf-8 -*-
"""
同一个分段节点上「换项目排队」的隔离测试

真实场景：A 项目（6 段）跑到第 3 段，用户把界面改成 B 项目（4 段、另一段音频），
两个 job 排在同一个 ComfyUI 队列里串行执行。要守住四件事：

  ① **游标按「节点 + 音频」隔离** —— B 的执行不能把 A 的进度往前推
  ② **老格式记录在签名对得上时迁移** —— 升级后不能让人跑到一半的进度凭空归零
  ③ **老格式记录属于别的项目时不能删** —— 那是另一个项目的进度
  ④ **项目指纹** —— 前端靠它阻止「换项目后自动连续拿新参数跑旧项目的下一段」
"""

import importlib.util
import json
import os

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, alias):
    spec = importlib.util.spec_from_file_location(alias, os.path.join(HERE, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_split = _load("audio_split", "dk_audio_split_for_queue")

torch = pytest.importorskip("torch")

NODE = "440"


@pytest.fixture(autouse=True)
def _clean_state():
    _split._MEM_STATE.clear()
    yield
    _split._MEM_STATE.clear()


def _audio(seconds=40.0, rate=8000):
    # 够长：分段表最多铺到 12 秒，切不到就抛「切出来是空的」，那测的就不是游标了
    return {"waveform": torch.zeros(1, 1, int(round(seconds * rate))), "sample_rate": rate}


def _segments(count, step=2.0):
    return json.dumps({
        "segments": [
            {"start": i * step, "end": (i + 1) * step, "prompt": ""}
            for i in range(count)
        ]
    })


def _step(node, segments, path, uid=NODE):
    """跑一次顺序模式，返回本次输出的段序号（1 起）"""
    out = node.split(
        分段数据=segments, 全局提示词="", 输出模式="顺序", 当前段=1,
        自动连续=False, 文件路径=path, 续接帧数=0,
        音频=_audio(), unique_id=uid,
    )
    return out["result"][2]


# ---------------------------------------------------------------- ① 隔离

def test_two_projects_do_not_share_cursor():
    """
    A 跑两段 → B 跑一段 → 再跑 A。
    B 必须从第 1 段开始，且**不能**把 A 的进度顶到第 3 段之后。
    """
    node = _split.DogkingAudioSplit()
    seg_a = _segments(6)
    seg_b = _segments(4)

    assert _step(node, seg_a, "C:/audio/A.wav") == 1
    assert _step(node, seg_a, "C:/audio/A.wav") == 2

    assert _step(node, seg_b, "C:/audio/B.wav") == 1     # B 自己从头数
    assert _step(node, seg_a, "C:/audio/A.wav") == 3     # A 接着第 3 段，没被 B 推走


def test_same_audio_keeps_one_cursor():
    """同一个音频反复跑 = 同一个项目，游标当然要连续。"""
    node = _split.DogkingAudioSplit()
    segs = _segments(4)
    got = [_step(node, segs, "C:/audio/A.wav") for _ in range(4)]
    assert got == [1, 2, 3, 4]
    # 跑完一轮归零
    assert _step(node, segs, "C:/audio/A.wav") == 1


def test_cursor_wraps_back_to_one():
    node = _split.DogkingAudioSplit()
    segs = _segments(3)
    assert [_step(node, segs, "C:/a.wav") for _ in range(4)] == [1, 2, 3, 1]


def test_path_variants_are_the_same_project():
    """同一个文件写成反斜杠 / 正斜杠 / 带引号，都该认成同一个项目。"""
    node = _split.DogkingAudioSplit()
    segs = _segments(4)
    assert _step(node, segs, r"C:\audio\A.wav") == 1
    assert _step(node, segs, "C:/audio/A.wav") == 2
    assert _step(node, segs, '"C:/audio/A.wav"') == 3


# ---------------------------------------------------------------- ② 迁移

def test_legacy_record_migrates_when_signature_matches():
    """
    升级前的老记录键是纯节点 id。签名对得上 = 同一个项目，
    必须搬到新 key 上，不能让人跑到一半的进度归零。
    """
    node = _split.DogkingAudioSplit()
    segs = _segments(6)
    parsed = _split.parse_segments(segs)
    _split._MEM_STATE[NODE] = {
        "audio_sig": _split.timeline_signature(parsed),
        "index": 2,                       # 已经跑完 2 段
    }

    assert _step(node, segs, "C:/audio/A.wav") == 3

    new_key = _split.project_key(NODE, "C:/audio/A.wav")
    assert new_key in _split._MEM_STATE
    assert NODE not in _split._MEM_STATE        # 搬走了，不留重复
    assert _split._MEM_STATE[new_key]["index"] == 3


def test_legacy_record_of_other_project_is_kept():
    """
    老记录是别的项目的（签名对不上）。可以不用它，但**不能删** ——
    删了就等于把另一个项目跑到一半的进度抹掉。
    """
    node = _split.DogkingAudioSplit()
    _split._MEM_STATE[NODE] = {"audio_sig": "deadbeefcafe", "index": 5}

    assert _step(node, _segments(4), "C:/audio/B.wav") == 1
    assert NODE in _split._MEM_STATE
    assert _split._MEM_STATE[NODE]["index"] == 5


# ---------------------------------------------------------------- ③ key 规则

def test_project_key_uses_audio_basename():
    assert _split.project_key("440", "C:/a/b/voice.wav") == "440|voice"
    assert _split.project_key("440", r"C:\a\voice.mp3") == "440|voice"
    assert _split.project_key("440", "") == "440"
    assert _split.project_key("440", '  "C:/x/归期.wav"  ') == "440|归期"


def test_project_key_is_stable_across_dots_in_name():
    """文件名里有多个点只去最后一个后缀。"""
    assert _split.project_key("1", "C:/a/my.voice.v2.wav") == "1|my.voice.v2"


# ---------------------------------------------------------------- ④ 项目指纹

def test_execution_signature_is_basename_and_count():
    segs = _split.parse_segments(_segments(4))
    assert _split.execution_signature("C:/audio/A.wav", segs) == "A.wav|4"
    assert _split.execution_signature("", []) == "|0"


def test_execution_signature_changes_when_project_changes():
    """换音频 / 重新分段都要变 —— 前端靠它判断「界面还是不是刚跑的那一套」。"""
    segs_a = _split.parse_segments(_segments(6))
    segs_b = _split.parse_segments(_segments(4))
    a = _split.execution_signature("C:/a/A.wav", segs_a)
    assert a != _split.execution_signature("C:/a/B.wav", segs_a)
    assert a != _split.execution_signature("C:/a/A.wav", segs_b)


def test_execution_signature_ignores_prompt_edits():
    """
    改提示词不该改变指纹：用户边跑边调词是常态，
    因为改个词就把自动连续停掉，那这个功能没法用。
    """
    base = _split.parse_segments(_segments(3))
    edited = json.loads(_segments(3))
    edited["segments"][1]["prompt"] = "换个词"
    assert _split.execution_signature("a.wav", base) == _split.execution_signature(
        "a.wav", _split.parse_segments(json.dumps(edited))
    )
