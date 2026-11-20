# -*- coding: utf-8 -*-
"""
参考图功能测试（audio_split 的全局 / 每段参考图）

守五件事：

  ① **配置容错** —— 老工作流没有这个字段、JSON 坏掉、张数超限，都不能抛异常。
  ② **覆盖语义** —— 段内有图就整组覆盖全局，不是逐张合并；段内空着才回退全局。
  ③ **短边限制** —— 短边缩到指定值、长边等比，不裁切不补边。
  ④ **输出口** —— 永远吐 MAX_REFS 个，不足补 None（不能补灰图）。
  ⑤ **分段表** —— 每段的自定义图要跟着段一起存下来，改提示词不能把它冲掉。
"""

import importlib.util
import json
import os

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 插件根目录


def _load(name, alias):
    spec = importlib.util.spec_from_file_location(alias, os.path.join(HERE, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_split = _load("audio_split", "dk_audio_split_for_refs")

torch = pytest.importorskip("torch")
Image = pytest.importorskip("PIL.Image")


# ---------------------------------------------------------------- 造图

def _make_image(path, w, h, color=(120, 160, 200)):
    Image.new("RGB", (w, h), color).save(path)
    return str(path)


# ---------------------------------------------------------------- ① 配置容错

@pytest.mark.parametrize("raw", ["", "{}", "null", "[]", "not json", '{"images": "abc"}'])
def test_ref_config_never_raises(raw):
    """任何畸形输入都要退化成「没有图 + 默认短边」，不能把节点搞红。"""
    short, images = _split.parse_ref_config(raw)
    assert short == _split.REF_SHORT_EDGE_DEFAULT
    assert images == []


def test_ref_config_reads_short_and_images():
    raw = json.dumps({"short": 640, "images": ["C:/a.png", "C:/b.png"]})
    short, images = _split.parse_ref_config(raw)
    assert short == 640
    assert images == ["C:/a.png", "C:/b.png"]


def test_ref_config_clamps_and_dedupes():
    """短边为负数 / 夸张值要夹回来；重复路径只留一份；超过 6 张截断。"""
    raw = json.dumps({
        "short": 999999,
        "images": ["a.png", "a.png", "b.png", "c.png", "d.png", "e.png", "f.png", "g.png"],
    })
    short, images = _split.parse_ref_config(raw)
    assert short == 8192
    assert images[0] == "a.png"
    assert len(images) == 6
    assert len(set(images)) == 6

    assert _split.parse_ref_config(json.dumps({"short": -5}))[0] == 0


# ---------------------------------------------------------------- ② 覆盖语义

def test_segment_images_override_global_entirely():
    """
    段内有图 = 整组替换。这是「换一批参考」的语义，不是「打补丁」——
    混合会造出「第 1 张换了、第 2 张还是全局的」这种没法预期的状态。
    """
    seg = {"images": ["s1.png", "s2.png"]}
    paths, source = _split.resolve_ref_paths(seg, ["g1.png", "g2.png", "g3.png"])
    assert source == "segment"
    assert paths == ["s1.png", "s2.png"]


def test_empty_segment_falls_back_to_global():
    assert _split.resolve_ref_paths({"images": []}, ["g1.png"]) == (["g1.png"], "global")
    assert _split.resolve_ref_paths({}, ["g1.png"]) == (["g1.png"], "global")
    assert _split.resolve_ref_paths({"images": ["", "  "]}, ["g1.png"]) == (["g1.png"], "global")


def test_no_refs_at_all():
    assert _split.resolve_ref_paths({}, []) == ([], "")


def test_segment_images_are_capped():
    seg = {"images": ["%d.png" % i for i in range(12)]}
    paths, _ = _split.resolve_ref_paths(seg, [])
    assert len(paths) == _split.MAX_REFS == 6


# ---------------------------------------------------------------- ③ 短边限制

def test_short_edge_scales_long_side_proportionally(tmp_path):
    """竖图 800×1600，短边限制 1024 → 1024×2048（放大，等比，不裁切）。"""
    path = _make_image(tmp_path / "tall.png", 800, 1600)
    tensor, info = _split.load_reference_image(path, 1024)
    assert tensor is not None, info
    assert tuple(tensor.shape) == (1, 2048, 1024, 3)
    assert info == "1024x2048"


def test_short_edge_shrinks_oversized(tmp_path):
    """横图 3000×2000，短边 1024 → 1536×1024。"""
    path = _make_image(tmp_path / "wide.png", 3000, 2000)
    tensor, info = _split.load_reference_image(path, 1024)
    assert tuple(tensor.shape) == (1, 1024, 1536, 3)
    assert info == "1536x1024"


def test_short_edge_zero_keeps_original(tmp_path):
    path = _make_image(tmp_path / "raw.png", 320, 480)
    tensor, info = _split.load_reference_image(path, 0)
    assert tuple(tensor.shape) == (1, 480, 320, 3)
    assert info == "320x480"


def test_short_edge_never_crops_aspect(tmp_path):
    """缩放前后宽高比必须一致 —— 裁切会切掉内容，补边会被 VAE 当画面编码。"""
    path = _make_image(tmp_path / "odd.png", 700, 1310)
    tensor, _ = _split.load_reference_image(path, 512)
    h, w = int(tensor.shape[1]), int(tensor.shape[2])
    assert abs((w / h) - (700 / 1310)) < 0.01


def test_tensor_is_normalised_float(tmp_path):
    path = _make_image(tmp_path / "c.png", 64, 64, (255, 0, 0))
    tensor, _ = _split.load_reference_image(path, 0)
    assert tensor.dtype == torch.float32
    assert float(tensor.max()) <= 1.0
    assert float(tensor[0, 0, 0, 0]) > 0.95      # R
    assert float(tensor[0, 0, 0, 1]) < 0.05      # G


# ---------------------------------------------------------------- ④ 输出口

def test_outputs_always_six_slots_with_none(tmp_path):
    """
    不足的位置必须是 None，不能补灰图 —— 灰图会被当成真实参考送进模型，
    生成结果莫名其妙带灰，而且完全查不出来。
    """
    a = _make_image(tmp_path / "a.png", 100, 100)
    outs, note, sizes = _split.build_reference_outputs(
        {"images": [a]}, json.dumps({"short": 0}), limit=6
    )
    assert len(outs) == 6
    assert outs[0] is not None
    assert outs[1:] == (None,) * 5
    assert "本段覆盖" in note
    assert sizes == ["100x100"]


def test_outputs_note_marks_global_source(tmp_path):
    a = _make_image(tmp_path / "a.png", 100, 100)
    cfg = json.dumps({"short": 0, "images": [a]})
    _, note, _ = _split.build_reference_outputs({}, cfg, limit=6)
    assert "全局" in note


def test_missing_file_is_reported_not_silently_dropped(tmp_path):
    outs, note, _ = _split.build_reference_outputs(
        {"images": [str(tmp_path / "nope.png")]}, "{}", limit=6
    )
    assert outs == (None,) * 6
    assert "失败 1 张" in note


def test_non_image_extension_rejected(tmp_path):
    path = tmp_path / "evil.txt"
    path.write_text("not an image", encoding="utf-8")
    tensor, err = _split.load_reference_image(str(path), 1024)
    assert tensor is None
    assert "不是图片" in err


# ---------------------------------------------------------------- ⑤ 分段表

def test_parse_segments_keeps_images():
    raw = json.dumps({"segments": [
        {"start": 0, "end": 5, "prompt": "", "images": ["a.png", "b.png"]},
        {"start": 5, "end": 10, "prompt": "x"},
    ]})
    segs = _split.parse_segments(raw)
    assert segs[0]["images"] == ["a.png", "b.png"]
    assert segs[1]["images"] == []          # 缺字段收敛成空列表，下游不用判 None


def test_parse_segments_caps_and_dedupes_images():
    raw = json.dumps({"segments": [{
        "start": 0, "end": 5,
        "images": ["a.png", "a.png"] + ["%d.png" % i for i in range(9)],
    }]})
    segs = _split.parse_segments(raw)
    assert len(segs[0]["images"]) == 6
    assert segs[0]["images"][0] == "a.png"


def test_node_declares_six_image_outputs():
    """输出口数量必须和前端槽位数一致，否则「有 6 张图但只有 5 个口」。"""
    types = _split.DogkingAudioSplit.RETURN_TYPES
    names = _split.DogkingAudioSplit.RETURN_NAMES
    assert len(types) == len(names) == 12 + 6
    assert types[-6:] == ("IMAGE",) * 6
    assert names[-6:] == ("图1", "图2", "图3", "图4", "图5", "图6")


def test_ref_config_is_a_hidden_internal_field():
    """
    参考图数据是内部字段：默认空串，且**必须排在最后一个 widget**。

    ComfyUI 的 widgets_values 是按位置存的，在中间插新 widget 会让老工作流的
    每一个值往后错一位（提示词被读成输出模式、段号被读成布尔值…）。
    """
    spec = _split.DogkingAudioSplit.INPUT_TYPES()
    assert "参考图数据" not in spec["required"]
    assert spec["optional"]["参考图数据"][1]["default"] == ""

    # 按 ComfyUI 的遍历顺序排 widget（forceInput 的不生成控件）
    widgets = []
    for section in ("required", "optional"):
        for name, item in spec.get(section, {}).items():
            opts = item[1] if len(item) > 1 and isinstance(item[1], dict) else {}
            if not opts.get("forceInput"):
                widgets.append(name)
    assert widgets[-1] == "参考图数据"
    assert widgets[:7] == [
        "分段数据", "全局提示词", "输出模式", "当前段", "自动连续", "文件路径", "续接帧数",
    ]


# ---------------------------------------------------------------- 端到端

def test_split_returns_image_tensors(tmp_path):
    """走一遍真正的 split()：段内的图要出现在末尾 6 个口里，且按短边缩过。"""
    a = _make_image(tmp_path / "a.png", 100, 200)    # 竖
    b = _make_image(tmp_path / "b.png", 200, 100)    # 横
    segments = json.dumps({"segments": [{
        "start": 0, "end": 2, "prompt": "动作", "images": [a, b],
    }]})

    node = _split.DogkingAudioSplit()
    out = node.split(
        分段数据=segments, 全局提示词="", 输出模式="单段", 当前段=1,
        自动连续=False, 文件路径="", 续接帧数=0,
        参考图数据=json.dumps({"short": 64}),
        音频={"waveform": torch.zeros(1, 8000), "sample_rate": 8000},
        unique_id="refs-e2e",
    )

    r = out["result"]
    assert len(r) == 18
    first, second = r[12], r[13]
    assert first is not None and second is not None
    # 短边 64：100×200 → 64×128；200×100 → 128×64（各自保持自己的宽高比）
    assert tuple(first.shape) == (1, 128, 64, 3)
    assert tuple(second.shape) == (1, 64, 128, 3)
    assert r[14:] == (None,) * 4
    assert "本段覆盖" in out["ui"]["dogking_status"][0]


def test_split_falls_back_to_global_when_segment_has_none(tmp_path):
    """段内没图时，全局图要顶上 —— 这是「全局参考图」真正的用处。"""
    g = _make_image(tmp_path / "g.png", 80, 80)
    segments = json.dumps({"segments": [{"start": 0, "end": 2, "prompt": ""}]})

    node = _split.DogkingAudioSplit()
    out = node.split(
        分段数据=segments, 全局提示词="", 输出模式="单段", 当前段=1,
        自动连续=False, 文件路径="", 续接帧数=0,
        参考图数据=json.dumps({"short": 0, "images": [g]}),
        音频={"waveform": torch.zeros(1, 8000), "sample_rate": 8000},
        unique_id="refs-global",
    )
    assert out["result"][12] is not None
    assert tuple(out["result"][12].shape) == (1, 80, 80, 3)
    assert "全局" in out["ui"]["dogking_status"][0]
