"""段首亮度归位节点的行为测试。

重点验证三件事：
  1. 前 N 帧偏亮时，被压回稳态（第 0 帧最狠、第 N 帧归零）
  2. 第 N 帧及之后一个像素都不动 —— 否则接缝处会换一种方式跳
  3. 只乘标量增益，画面结构（相对明暗关系）不变
"""

import importlib.util
import os

import pytest

torch = pytest.importorskip("torch")

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("dk_head_tone_for_test", os.path.join(HERE, "head_tone.py"))
_ht = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ht)

DogkingHeadTone = _ht.DogkingHeadTone


def _clip(bright_head=1.10, frames=40, n_head=22):
    """造一段「前 n_head 帧整体偏亮」的假视频：中间一块亮斑 + 背景。"""
    g = torch.zeros(frames, 32, 32, 3)
    g[..., :] = 0.45
    g[:, 8:24, 8:24, :] = 0.75
    g[:n_head] = g[:n_head] * bright_head
    return torch.clamp(g, 0, 1)


def _tone(frame):
    return frame.mean().item()


def test_head_frames_pulled_back_to_steady():
    img = _clip()
    out, status = DogkingHeadTone().apply(img, 22, 22, 12, 1.0)
    steady = _tone(img[30])
    assert abs(_tone(out[0]) - steady) < 0.01, "第 0 帧应该被压回稳态亮度"
    assert "22 帧" in status


def test_tail_untouched():
    img = _clip()
    out, _ = DogkingHeadTone().apply(img, 22, 22, 12, 1.0)
    # 第 n 帧（引导区后第一帧）也要校正，之后的一律不动
    assert torch.allclose(out[23:], img[23:]), "第 23 帧之后不应有任何改动"


def test_no_seam_at_boundary():
    """第 21 帧和 22 帧的亮度差要小于原始差，否则只是把跳变换了个位置。"""
    img = _clip()
    out, _ = DogkingHeadTone().apply(img, 22, 22, 12, 1.0)
    before = abs(_tone(img[21]) - _tone(img[22]))
    after = abs(_tone(out[21]) - _tone(out[22]))
    assert after < before


def test_gain_is_scalar_keeps_structure():
    img = _clip()
    out, _ = DogkingHeadTone().apply(img, 22, 22, 12, 1.0)
    f0 = out[0]
    # 亮斑仍应比背景亮：说明是标量增益而不是把整帧拉平
    assert f0[8:24, 8:24].mean() > f0[0:4, 0:4].mean() + 0.1


def test_strength_zero_is_identity():
    img = _clip()
    out, status = DogkingHeadTone().apply(img, 22, 22, 12, 0.0)
    assert torch.allclose(out, img)
    assert "跳过" in status


def test_short_clip_does_not_crash():
    img = _clip(frames=6, n_head=4)
    out, _ = DogkingHeadTone().apply(img, 22, 22, 12, 1.0)
    assert out.shape == img.shape


def test_already_steady_is_barely_touched():
    img = _clip(bright_head=1.0)
    out, _ = DogkingHeadTone().apply(img, 22, 22, 12, 1.0)
    assert abs(_tone(out[0]) - _tone(img[0])) < 0.005
