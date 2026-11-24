# -*- coding: utf-8 -*-
"""fast_slice.py 测试（ImageFromBatch 零拷贝补丁）

守四件事：

  ① **结果和官方一致** —— 负索引、超界、length 截断，切片范围和原实现逐帧相同
  ② **真的是视图** —— 不复制数据（storage 指针相同），这是省下 9.95GB 的关键
  ③ **只打一次** —— 重复调用 apply() 不再替换
  ④ **能关掉** —— ENABLE=False 时不碰官方节点

用假的 comfy_extras / comfy_api 模块跑，不依赖真的 ComfyUI 在跑。
"""

import importlib.util
import os
import sys
import types

import pytest
import torch

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, alias):
    spec = importlib.util.spec_from_file_location(alias, os.path.join(HERE, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeNodeOutput:
    def __init__(self, *args):
        self.result = args


def _install_fakes():
    """造一个和官方同构的 ImageFromBatch（带 clone），供补丁替换。"""
    class _OfficialImageFromBatch:
        @classmethod
        def execute(cls, image, batch_index, length):
            s_in = image
            if batch_index < 0:
                batch_index += s_in.shape[0]
            batch_index = max(0, min(s_in.shape[0] - 1, batch_index))
            length = min(s_in.shape[0] - batch_index, length)
            s = s_in[batch_index:batch_index + length].clone()
            return _FakeNodeOutput(s)

        frombatch = execute

    mod_ni = types.ModuleType("comfy_extras.nodes_images")
    mod_ni.ImageFromBatch = _OfficialImageFromBatch

    mod_ce = types.ModuleType("comfy_extras")
    mod_ce.nodes_images = mod_ni

    mod_io = types.ModuleType("comfy_api.latest.IO")
    mod_io.NodeOutput = _FakeNodeOutput
    mod_latest = types.ModuleType("comfy_api.latest")
    mod_latest.IO = mod_io
    mod_api = types.ModuleType("comfy_api")
    mod_api.latest = mod_latest

    saved = {k: sys.modules.get(k) for k in (
        "comfy_extras", "comfy_extras.nodes_images", "comfy_api", "comfy_api.latest")}
    sys.modules["comfy_extras"] = mod_ce
    sys.modules["comfy_extras.nodes_images"] = mod_ni
    sys.modules["comfy_api"] = mod_api
    sys.modules["comfy_api.latest"] = mod_latest
    return mod_ni, saved


def _restore(saved):
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


@pytest.fixture()
def patched():
    ni, saved = _install_fakes()
    m = _load("fast_slice", "dk_fast_slice_for_test")
    assert m.apply() is True
    yield ni.ImageFromBatch, m
    _restore(saved)


# ---------------------------------------------------------------- ① 结果一致

@pytest.mark.parametrize("batch_index,length", [
    (0, 1), (0, 4), (3, 2), (-1, 1), (-4, 2), (100, 3), (9, 5), (8, 10),
])
def test_same_range_as_official(patched, batch_index, length):
    """补丁前后切出来的像素必须完全一致。"""
    cls, _m = patched
    img = torch.arange(10 * 2 * 2 * 3, dtype=torch.float32).reshape(10, 2, 2, 3)

    # 官方原实现（clone 版）作为参照
    s_in = img
    bi = batch_index + s_in.shape[0] if batch_index < 0 else batch_index
    bi = max(0, min(s_in.shape[0] - 1, bi))
    ln = min(s_in.shape[0] - bi, length)
    expect = s_in[bi:bi + ln]

    got = cls.execute(img, batch_index, length).result[0]
    assert got.shape == expect.shape
    assert torch.equal(got, expect)


# ---------------------------------------------------------------- ② 是视图

def test_returns_view_not_copy(patched):
    """同一块 storage 才叫零拷贝 —— 一旦有人把 .clone() 加回来，这条会红。"""
    cls, _m = patched
    img = torch.zeros(6, 4, 4, 3)
    out = cls.execute(img, 1, 2).result[0]
    assert out.shape == (2, 4, 4, 3)
    assert out.untyped_storage().data_ptr() == img.untyped_storage().data_ptr()
    assert out._base is not None


# ---------------------------------------------------------------- ③ 只打一次

def test_idempotent(patched):
    cls, m = patched
    # 绑定方法每次访问都是新对象，比 __func__ 才是「同一个函数」
    first = cls.execute.__func__
    assert m.apply() is False
    assert cls.execute.__func__ is first
    assert m.is_applied() is True


# ---------------------------------------------------------------- ④ 能关掉

def test_disable(monkeypatch):
    ni, saved = _install_fakes()
    try:
        before = ni.ImageFromBatch.execute.__func__
        m = _load("fast_slice", "dk_fast_slice_off_for_test")
        m.ENABLE = False
        assert m.apply() is False
        assert ni.ImageFromBatch.execute.__func__ is before
        assert m.is_applied() is False
    finally:
        _restore(saved)
