"""受保护上下文节点的形状契约测试。

这个节点最容易错的地方不是逻辑，是**形状**：mask 要扩到 latent 的通道数、
要用 pack_latents 打包、长度必须和打包后的 latent 严格相等。任何一处错了，
采样器不会报错，而是安静地算出花屏或者锁错位置 —— 所以这里逐条钉死。
"""

import os
import sys

import pytest

torch = pytest.importorskip("torch")

HERE = os.path.dirname(os.path.abspath(__file__))
COMFY_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))  # .../ComfyUI
if COMFY_ROOT not in sys.path:
    sys.path.insert(0, COMFY_ROOT)

import comfy.utils  # noqa: E402
from comfy.nested_tensor import NestedTensor  # noqa: E402

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "dk_protected_context", os.path.join(os.path.dirname(HERE), "protected_context.py")
)
_pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pc)


class FakeVAE:
    """按目标 latent 形状返回固定张量，不加载真模型。"""

    def __init__(self, t, h, w, c=24, value=0.5):
        self.t, self.h, self.w, self.c, self.value = t, h, w, c, value

    def encode(self, image):
        return torch.full((1, self.c, self.t, self.h, self.w), self.value)


def _latent(T=152, H=96, W=54, A=800, C=24):
    video = torch.randn(1, C, T, H, W)
    audio = torch.randn(1, 32, 2, A)
    return {"samples": NestedTensor((video, audio))}


def _run(frames=39, lock_audio=True, T=152, A=800):
    latent = _latent(T=T, A=A)
    vt = _pc.video_latent_t(frames)
    vae = FakeVAE(vt, 96, 54, value=0.25)
    guide = torch.rand(39, 1536, 864, 3)
    out, status = _pc.DogkingProtectedContext().apply(latent, guide, vae, frames, lock_audio)
    return out, status, vt, T, A


def test_video_latent_t_matches_h3():
    assert _pc.video_latent_t(5) == 2
    assert _pc.video_latent_t(22) == 7
    assert _pc.video_latent_t(39) == 12
    assert _pc.video_latent_t(56) == 17


def test_mask_is_nested_not_packed():
    """mask 必须是 nested。打包过的 mask 会被 guider 当成单路塞进 reshape_mask，
    元素总数多出 audio 那一截，时间维直接撑错 —— 花屏就是这么来的。"""
    out, _, vt, T, A = _run()
    m = out["noise_mask"]
    assert m.is_nested, "mask 不能是打包好的 5D"
    vm, am = m.tensors
    assert tuple(vm.shape) == (1, 1, T, 96, 54)
    assert tuple(am.shape) == (1, 1, 1, A)


def test_guider_mask_chain_matches_latent():
    """照 guider.sample 的真实处理链走一遍：unbind -> prepare_mask -> pack，
    结果必须和打包后的 latent 完全同形。"""
    out, _, _, _, _ = _run()
    import comfy.sampler_helpers
    shapes = [tuple(x.shape) for x in out["samples"].tensors]
    masks = [comfy.sampler_helpers.prepare_mask(x, shapes[i], "cpu")
             for i, x in enumerate(out["noise_mask"].tensors)]
    packed_mask, _ = comfy.utils.pack_latents(masks)
    packed_latent, _ = comfy.utils.pack_latents(list(out["samples"].tensors))
    assert packed_mask.shape == packed_latent.shape
    back = comfy.utils.unpack_latents(packed_mask, shapes)
    assert [tuple(x.shape) for x in back] == shapes


def test_mask_zero_on_first_vt_frames():
    out, _, vt, T, A = _run()
    vm = out["noise_mask"].tensors[0]
    assert float(vm[:, :, :vt].max()) == 0.0, "前 vt 个 latent 帧必须是 0（锁死）"
    assert float(vm[:, :, vt:].min()) == 1.0, "vt 之后的帧必须是 1（可重绘）"


def test_audio_locked_by_proportion():
    out, _, _, T, A = _run(frames=39, A=800)
    am = out["noise_mask"].tensors[1]
    at = int(round(39 * 5.0 / 3.0))
    assert float(am[..., :at].max()) == 0.0
    assert float(am[..., at:].min()) == 1.0


def test_audio_not_locked_when_disabled():
    out, _, _, T, A = _run(lock_audio=False)
    am = out["noise_mask"].tensors[1]
    assert float(am.min()) == 1.0


def test_guide_written_into_samples():
    out, _, vt, _, _ = _run()
    video = out["samples"].tensors[0]
    assert float(video[:, :, :vt].min()) == pytest.approx(0.25)
    assert float(video[:, :, :vt].max()) == pytest.approx(0.25)


def test_frames_snapped_to_17k_plus_5():
    out, status, vt, _, _ = _run(frames=40)  # 40 -> 39
    assert "39 帧" in status
    assert vt == 12


def test_rejects_plain_latent():
    with pytest.raises(ValueError):
        _pc.DogkingProtectedContext().apply(
            {"samples": torch.randn(1, 24, 8, 4, 4)}, torch.rand(5, 64, 64, 3), FakeVAE(2, 4, 4), 39, True
        )
