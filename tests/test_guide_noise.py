# -*- coding: utf-8 -*-
"""续接引导加噪（尾帧保护）的测试。"""

import importlib.util
import os

import pytest
import torch

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load():
    spec = importlib.util.spec_from_file_location("gn", os.path.join(ROOT, "guide_noise.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


_gn = _load()


def _clip(frames=5, h=32, w=24):
    """造一段有梯度的图，方便看噪声有没有进来。"""
    t = torch.linspace(0, 1, frames).view(frames, 1, 1, 1)
    base = torch.linspace(0.2, 0.8, h * w).view(1, h, w, 1)
    return (base + t * 0.05).repeat(1, 1, 1, 3).clamp(0, 1)


def test_tail_untouched():
    img = _clip()
    out, add_n, keep = _gn.add_noise_protecting_tail(img, 0.1, 1, 0)
    assert add_n == 4 and keep == 1
    assert torch.allclose(out[-1], img[-1]), "最后一帧必须原样"


def test_head_changed():
    img = _clip()
    out, _, _ = _gn.add_noise_protecting_tail(img, 0.1, 1, 0)
    diff = (out[:-1] - img[:-1]).abs().mean()
    assert float(diff) > 0.01, "除尾帧外应该被加噪"


def test_zero_strength_noop():
    img = _clip()
    out, add_n, _ = _gn.add_noise_protecting_tail(img, 0.0, 1, 0)
    assert add_n == 0
    assert torch.allclose(out, img)


def test_protect_zero_adds_all():
    img = _clip()
    out, add_n, keep = _gn.add_noise_protecting_tail(img, 0.1, 0, 0)
    assert add_n == 5 and keep == 0
    assert not torch.allclose(out[-1], img[-1])


def test_protect_more_than_frames():
    img = _clip(frames=3)
    out, add_n, keep = _gn.add_noise_protecting_tail(img, 0.1, 99, 0)
    assert keep == 3 and add_n == 0
    assert torch.allclose(out, img), "保护帧数超过总帧数时等于全保护"


def test_shape_and_range():
    img = _clip()
    out, _, _ = _gn.add_noise_protecting_tail(img, 0.3, 1, 7)
    assert out.shape == img.shape
    assert float(out.min()) >= 0.0 and float(out.max()) <= 1.0


def test_same_seed_same_noise():
    img = _clip()
    a, _, _ = _gn.add_noise_protecting_tail(img, 0.1, 1, 42)
    b, _, _ = _gn.add_noise_protecting_tail(img, 0.1, 1, 42)
    assert torch.allclose(a, b)


def test_different_seed_differs():
    img = _clip()
    a, _, _ = _gn.add_noise_protecting_tail(img, 0.1, 1, 1)
    b, _, _ = _gn.add_noise_protecting_tail(img, 0.1, 1, 2)
    assert not torch.allclose(a[:-1], b[:-1])


def test_node_outputs():
    node = _gn.DogkingGuideNoise()
    img = _clip()
    out, status = node.apply(img, 0.1, 1, 0)
    assert out.shape == img.shape
    assert "加噪 4" in status and "保护尾 1" in status


def test_node_zero_strength_status():
    node = _gn.DogkingGuideNoise()
    out, status = node.apply(_clip(), 0.0, 1, 0)
    assert "未加噪" in status


def test_mappings():
    assert "DogkingGuideNoise" in _gn.NODE_CLASS_MAPPINGS
    assert "DogkingGuideNoise" in _gn.NODE_DISPLAY_NAME_MAPPINGS
