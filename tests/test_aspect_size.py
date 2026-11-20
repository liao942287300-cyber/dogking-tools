# -*- coding: utf-8 -*-
"""
aspect_size.py 测试（同比例定尺寸）

守四件事：

  ① **比例误差受控** —— 输出的 w/h 和源比例之差，不能超过用户给的上限
  ② **32 倍数** —— 下游上采样节点只认 32 的倍数
  ③ **严格档真的严格** —— 申请 0 误差时，结果必须精确等于源比例
  ④ **像素不会缩水** —— 选档不能为了让误差好看而把成片尺寸做小
"""

import importlib.util
import math
import os

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, alias):
    spec = importlib.util.spec_from_file_location(alias, os.path.join(HERE, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m = _load("aspect_size", "dk_aspect_size_for_test")


# ---------------------------------------------------------------- ① 比例误差

@pytest.mark.parametrize("sw,sh", [
    (544, 960), (768, 1376), (576, 1024), (720, 1280), (1080, 1920),
    (960, 544), (1024, 1024), (832, 1248),
])
def test_error_within_limit(sw, sh):
    """任何源比例，近似档或严格档都要落在用户给的上限内。"""
    for limit in (0.5, 0.3, 0.1):
        w, h, note = m.solve_size(sw, sh, 1.0, limit)
        err = abs((w / h) / (sw / sh) - 1) * 100
        assert err <= limit + 1e-9, (sw, sh, limit, w, h, err, note)


# ---------------------------------------------------------------- ② 32 倍数

@pytest.mark.parametrize("sw,sh", [(544, 960), (720, 1280), (1080, 1920), (1024, 1024)])
def test_multiple_of_32(sw, sh):
    w, h, _ = m.solve_size(sw, sh, 1.0, 0.5)
    assert w % 32 == 0 and h % 32 == 0, (w, h)
    assert w >= 32 and h >= 32


# ---------------------------------------------------------------- ③ 严格档

def test_strict_mode_is_exact():
    """申请 0 误差时必须精确命中源比例（17:30 只有 32*17k / 32*30k）。"""
    w, h, note = m.solve_size(544, 960, 1.0, 0.0)
    assert w / h == pytest.approx(544 / 960, rel=0)
    assert (w, h) == (1088, 1920)
    assert "严格" in note


def test_strict_falls_back_when_approx_too_coarse():
    """误差上限压到 0.1% 时，800x1408 的 0.27% 不达标，应退到严格档。"""
    w, h, note = m.solve_size(544, 960, 1.0, 0.1)
    assert abs((w / h) / (544 / 960) - 1) * 100 <= 0.1
    assert "严格" in note


# ---------------------------------------------------------------- ④ 像素

def test_pixels_near_target():
    """目标 1M 时结果不能掉到 0.5M 以下。"""
    for sw, sh in [(544, 960), (576, 1024), (720, 1280)]:
        w, h, note = m.solve_size(sw, sh, 1.0, 0.5)
        px = w * h / 1e6
        assert 0.7 <= px <= 1.8, (sw, sh, w, h, px, note)
        assert w * h >= sw * sh, "上采样目标不能比源小"


def test_544_960_expects_800_1408():
    """这是当前工作流用的那组：0.5% 容差下应为 800x1408。"""
    w, h, note = m.solve_size(544, 960, 1.0, 0.5)
    assert (w, h) == (800, 1408)
    assert "近似" in note


def test_576_1024_9_16():
    """9:16 源在 1M 附近：864x1536 是精确解（9:16 = 288:512 = 9:16）。"""
    w, h, note = m.solve_size(576, 1024, 1.0, 0.5)
    assert abs((w / h) / (576 / 1024) - 1) < 1e-9
    assert (w, h) == (864, 1536)
    assert "严格" in note


# ---------------------------------------------------------------- 边界

def test_ratio_change_is_followed():
    """换比例（一采改成 4:3）时，输出要跟着变，不能还停在竖屏。"""
    w1, h1, _ = m.solve_size(544, 960, 1.0, 0.5)
    w2, h2, _ = m.solve_size(1024, 768, 1.0, 0.5)
    assert w1 < h1, "竖屏源应输出竖屏"
    assert w2 > h2, "横屏源应输出横屏"
    # 两条都各自守住比例
    assert abs((w1 / h1) / (544 / 960) - 1) <= 0.005
    assert abs((w2 / h2) / (1024 / 768) - 1) <= 0.005


def test_invalid_source_raises():
    with pytest.raises(ValueError):
        m.solve_size(0, 960, 1.0, 0.5)
    with pytest.raises(ValueError):
        m.solve_size(544, -1, 1.0, 0.5)
