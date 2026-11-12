# -*- coding: utf-8 -*-
"""
pv_meta_nodes.py 测试（提示词嵌入 PNG 元数据）

守三件事：

  ① **参数扫描** —— 尤其是 `SamplerCustomAdvanced + ManualSigmas` 这种
     「全图没有 steps」的主力工作流（用户实测报过「参数没存上」，就是这条）。
  ② **参数行的位置** —— 没有 Steps 时绝不能拼进 `parameters`，
     否则 PromptVault 会把整行当成负向词的延续。
  ③ **真的能写进去** —— 落盘后用 Pillow 读回来，中文 iTXt 不乱码。

落盘全部指到临时目录，**不写用户真实的 ComfyUI output/**。
"""

import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 插件根目录


def _load(name, alias):
    spec = importlib.util.spec_from_file_location(alias, os.path.join(HERE, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_meta = _load("pv_meta_nodes", "dk_pv_meta_for_test")

# 落盘目录固定到临时目录（`_resolve_output_dir` 优先问 folder_paths，
# 测试环境没有它，退化成插件目录下 output/ —— 那也是真实目录，必须挡掉）
_TMPDIR = tempfile.mkdtemp(prefix="dk_pv_meta_")
_meta._resolve_output_dir = lambda: _TMPDIR


# ---------------------------------------------------------------- ① 文本拼装

def test_build_parameters_all_parts():
    text = _meta.build_parameters_text("一只猫", "模糊", "Steps: 9")
    assert text == "一只猫\nNegative prompt: 模糊\nSteps: 9"


def test_build_parameters_negative_only_keeps_mark():
    """只有负向词时标记行必须留着 —— 少了它整段会被当成正向词。"""
    text = _meta.build_parameters_text("", "模糊")
    assert text == "Negative prompt: 模糊"


def test_build_parameters_all_empty():
    assert _meta.build_parameters_text("", "", "") == ""
    assert _meta.build_parameters_text(None, None, None) == ""


# ---------------------------------------------------------------- 步数推导

def test_steps_from_sigmas_string():
    """10 个 sigma = 9 步。"""
    sigmas = "1.0, 0.978, 0.9, 0.8, 0.6, 0.5, 0.3, 0.2, 0.1, 0.0"
    assert _meta._steps_from_sigmas(sigmas) == 9


def test_steps_from_sigmas_list():
    assert _meta._steps_from_sigmas([1.0, 0.5, 0.0]) == 2


def test_steps_from_sigmas_rejects_link():
    """连线 `["201", 0]` 长得像两个数字，必须挡住（否则算出 1 步）。"""
    assert _meta._steps_from_sigmas(["201", 0]) is None
    assert _meta._steps_from_sigmas(["128", 0]) is None


def test_steps_from_sigmas_rejects_junk():
    assert _meta._steps_from_sigmas("") is None
    assert _meta._steps_from_sigmas("1.0") is None
    assert _meta._steps_from_sigmas("abc, 0.5, 0") is None
    assert _meta._steps_from_sigmas(None) is None


# ---------------------------------------------------------------- 参数扫描

def test_collect_ksampler_classic():
    prompt = {
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "sd_xl.safetensors"},
        },
        "10": {
            "class_type": "KSampler",
            "inputs": {
                "seed": 123456, "steps": 20, "cfg": 7.0,
                "sampler_name": "dpmpp_2m", "scheduler": "karras",
                "denoise": 1.0, "model": ["4", 0],
            },
        },
    }
    fields = _meta.collect_generation_params(prompt)
    assert fields["steps"] == 20
    assert fields["sampler"] == "dpmpp_2m"
    assert fields["scheduler"] == "karras"
    assert fields["seed"] == 123456
    assert fields["model"] == "sd_xl.safetensors"


def test_collect_sampler_custom_advanced_no_steps():
    """
    主力工作流：SamplerCustomAdvanced + ManualSigmas + KSamplerSelect + RandomNoise。
    全图没有 `steps` 字段，只有 sigmas —— 这条不测就会重现「参数没存上」。
    """
    prompt = {
        "3": {"class_type": "UNETLoader", "inputs": {"unet_name": "qwen_image.safetensors"}},
        "201": {
            "class_type": "ManualSigmas",
            "inputs": {"sigmas": "1.0, 0.97, 0.9, 0.8, 0.6, 0.4, 0.2, 0.05, 0.0"},
        },
        "202": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "203": {"class_type": "RandomNoise", "inputs": {"noise_seed": 987}},
        "204": {
            "class_type": "SamplerCustomAdvanced",
            "inputs": {"noise": ["203", 0], "guider": ["210", 0], "sampler": ["202", 0],
                       "sigmas": ["201", 0], "latent_image": ["205", 0]},
        },
        "210": {"class_type": "CFGGuider", "inputs": {"cfg": 3.5}},
    }
    fields = _meta.collect_generation_params(prompt)
    # 9 个 sigma = 8 步
    assert fields["steps"] == 8
    assert fields["sampler"] == "euler"
    assert fields["seed"] == 987
    assert fields["cfg"] == 3.5
    assert fields["model"] == "qwen_image.safetensors"


def test_collect_prefers_main_sampler_over_hires():
    """hires fix 的第二个采样器不能覆盖主采样器的 sampler / seed。"""
    prompt = {
        "5": {
            "class_type": "KSampler",
            "inputs": {"seed": 1, "steps": 20, "cfg": 7, "sampler_name": "euler",
                       "scheduler": "normal", "denoise": 1.0},
        },
        "9": {
            "class_type": "KSampler",
            "inputs": {"seed": 2, "steps": 10, "cfg": 7, "sampler_name": "dpmpp_2m",
                       "scheduler": "karras", "denoise": 0.5},
        },
    }
    fields = _meta.collect_generation_params(prompt)
    assert fields["steps"] == 20
    assert fields["sampler"] == "euler"
    assert fields["seed"] == 1


def test_collect_lora_vae_clip_skip():
    prompt = {
        "1": {"class_type": "LoraLoader",
              "inputs": {"lora_name": "a.safetensors", "strength_model": 0.8}},
        "2": {"class_type": "LoraLoaderModelOnly",
              "inputs": {"lora_name": "b.safetensors", "strength_model": 0.55}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "ae.safetensors"}},
        "4": {"class_type": "CLIPSetLastLayer", "inputs": {"stop_at_clip_layer": -2}},
    }
    fields = _meta.collect_generation_params(prompt)
    assert fields["loras"] == [("a.safetensors", 0.8), ("b.safetensors", 0.55)]
    assert fields["vae"] == "ae.safetensors"
    # A1111 记的是「跳几层」，要取绝对值
    assert fields["clip_skip"] == 2


def test_collect_numeric_node_order():
    """节点 id 按数值排：'10' 不能排到 '2' 前面。"""
    prompt = {
        "10": {"class_type": "KSampler",
               "inputs": {"steps": 99, "sampler_name": "wrong", "seed": 9}},
        "2": {"class_type": "KSampler",
              "inputs": {"steps": 20, "sampler_name": "right", "seed": 2}},
    }
    fields = _meta.collect_generation_params(prompt)
    assert fields["steps"] == 20
    assert fields["sampler"] == "right"


def test_collect_empty_prompt():
    assert _meta.collect_generation_params(None) == {}
    assert _meta.collect_generation_params({})["steps"] is None


# ---------------------------------------------------------------- 参数行

def test_params_line_starts_with_steps():
    fields = {"steps": 20, "sampler": "euler", "scheduler": "simple", "cfg": 3.5,
              "seed": 123, "model": "m.safetensors", "vae": "", "clip_skip": None,
              "denoise": 1.0, "loras": []}
    line = _meta.format_params_line(fields, 1024, 1024)
    assert line.startswith("Steps: 20, ")
    assert "Size: 1024x1024" in line
    assert "Model: m.safetensors" in line
    assert "Denoise" not in line  # denoise=1 是噪音，不写


def test_params_line_size_only_is_empty():
    """只有尺寸不算参数 —— 纯放大的图不该多一行 Size。"""
    fields = {"steps": None, "sampler": "", "scheduler": "", "cfg": None, "seed": None,
              "model": "", "vae": "", "clip_skip": None, "denoise": None, "loras": []}
    assert _meta.format_params_line(fields, 512, 512) == ""


def test_params_line_without_steps_has_no_steps_prefix():
    """没有步数时这一行不能以 Steps 开头，调用方据此改走独立文本块。"""
    fields = {"steps": None, "sampler": "euler", "scheduler": "", "cfg": None,
              "seed": None, "model": "m.safetensors", "vae": "", "clip_skip": None,
              "denoise": None, "loras": []}
    line = _meta.format_params_line(fields, 512, 512)
    assert line and not line.startswith("Steps")


def test_params_line_loras_numbered():
    fields = {"steps": 8, "sampler": "", "scheduler": "", "cfg": None, "seed": None,
              "model": "", "vae": "", "clip_skip": None, "denoise": None,
              "loras": [("a.safetensors", 0.8), ("b.safetensors", None)]}
    line = _meta.format_params_line(fields)
    assert "Lora_1: a.safetensors:0.8" in line
    assert "Lora_2: b.safetensors" in line


# ---------------------------------------------------------------- 真落盘

def test_save_metadata_roundtrip_chinese():
    """中文提示词写进 iTXt，读回来不能乱码。"""
    from PIL import Image

    img = Image.new("RGB", (32, 24), (10, 20, 30))
    text = _meta.build_parameters_text("一只银发的猫娘", "模糊", "Steps: 9")
    entry, err = _meta.save_with_metadata(img, text, "MetaTest")
    assert err == "" and entry["filename"].endswith(".png")

    with Image.open(os.path.join(_TMPDIR, entry["filename"])) as back:
        got = back.info.get("parameters")
    assert got == text
    assert "一只银发的猫娘" in got
    assert "Negative prompt: 模糊" in got


def test_save_metadata_extra_chunk():
    from PIL import Image

    img = Image.new("RGB", (16, 16), (0, 0, 0))
    entry, _err = _meta.save_with_metadata(
        img, "正向", "ExtraTest", ((_meta.PARAMS_EXTRA_KEY, "Sampler: euler"),)
    )
    with Image.open(os.path.join(_TMPDIR, entry["filename"])) as back:
        assert back.info.get("params") == "Sampler: euler"
        assert back.info.get("parameters") == "正向"


def test_save_metadata_extra_chunk_non_ascii_key_falls_back():
    """
    PNG 文本块的 keyword 是 Latin-1：中文 key 会抛 UnicodeEncodeError。
    节点「绝不抛异常」，所以必须退到 ASCII key 把内容保住。
    """
    from PIL import Image

    img = Image.new("RGB", (16, 16), (0, 0, 0))
    entry, _err = _meta.save_with_metadata(
        img, "正向", "FallbackTest", (("参数", "Sampler: euler"),)
    )
    with Image.open(os.path.join(_TMPDIR, entry["filename"])) as back:
        assert back.info.get(_meta.PARAMS_EXTRA_KEY) == "Sampler: euler"


# ---------------------------------------------------------------- 节点出口

class _FakeBatch(list):
    """够用的假 batch：有 len、能按序号取、每张是 [H,W,C] 的 numpy 数组。"""


def _batch(n=1, h=8, w=6):
    import numpy as np
    return _FakeBatch([np.zeros((h, w, 3), dtype="float32") for _ in range(n)])


def test_node_passes_image_through():
    node = _meta.DogkingPVMetaStamp()
    src = _batch(1)
    out = node.stamp(src, "正向词", "负向词", "NodeTest", prompt={})
    assert out["result"][0] is src                     # 原样透传，不是拷贝
    assert len(out["ui"]["images"]) == 1
    assert out["ui"]["dogking_status"][0].startswith("已写入")


def test_node_batch_writes_each_frame():
    node = _meta.DogkingPVMetaStamp()
    out = node.stamp(_batch(3), "正向", "", "BatchTest",
                     prompt={"7": {"class_type": "KSampler",
                                   "inputs": {"steps": 12, "sampler_name": "euler"}}})
    assert len(out["ui"]["images"]) == 3
    assert "3 张" in out["ui"]["dogking_status"][0]
    assert "Steps: 12" in out["ui"]["dogking_status"][0] or "参数" in out["ui"]["dogking_status"][0]


def test_node_none_image_returns_placeholder():
    """图片为空时不能返回 None —— 下游早就连好了，None 会让下游崩。"""
    node = _meta.DogkingPVMetaStamp()
    out = node.stamp(None, "正向", "", "NoneTest", prompt={})
    assert out["result"][0] is not None
    assert "没有输入图片" in out["ui"]["dogking_status"][0]


def test_node_without_steps_uses_separate_chunk():
    """没有步数 → 参数写独立块，不能污染 parameters 的负向词。"""
    from PIL import Image

    node = _meta.DogkingPVMetaStamp()
    prompt = {"7": {"class_type": "LoraLoader",
                    "inputs": {"lora_name": "x.safetensors", "strength_model": 1.0}}}
    out = node.stamp(_batch(1), "正向", "负向", "NoStepsTest", prompt=prompt)
    name = out["ui"]["images"][0]["filename"]
    with Image.open(os.path.join(_TMPDIR, name)) as back:
        params = back.info.get("parameters") or ""
        assert "Lora_1" not in params          # 关键：别把参数塞进负向词后面
        assert params == "正向\nNegative prompt: 负向"
        assert "Lora_1" in back.info.get(_meta.PARAMS_EXTRA_KEY, "")


def test_clean_prefix():
    assert _meta._clean_prefix("") == "PromptVault"
    assert _meta._clean_prefix("  ") == "PromptVault"
    assert _meta._clean_prefix("../叉") == "叉"
    assert _meta._clean_prefix("sub/名字") == "sub/名字"


# ---------------------------------------------------------------- 直接运行

if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
                passed += 1
            except Exception as exc:  # noqa: BLE001
                print(f"FAIL {name}: {exc}")
                failed += 1
    print(f"\n通过 {passed} / 失败 {failed}")
    sys.exit(1 if failed else 0)
