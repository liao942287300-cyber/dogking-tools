"""ImageFromBatch 零拷贝切片（dogking-tools 运行时补丁）

问题
--------------------------------------------------------------------------
ComfyUI 自带的「Get Image from Batch」（comfy_extras/nodes_images.py）最后一步是：

    s = s_in[batch_index:batch_index + length].clone()

`s_in[a:b]` 只是视图，不分配内存；真正吃内存的是 `.clone()`。

二采 800×1440、一段 30 秒（24fps）= 720 帧，一帧 float32 RGB 是 13.8MB：

    720 × 13.8MB ≈ 9.95GB

VAE 解码出来的源批次本身已经占 9.95GB，clone 又要一份同样大的，
这一步就得同时握住 19.9GB。模型权重还占着十几 G 到二十 G，
48G 内存的机器上必 OOM：

    RuntimeError: [enforce fail at alloc_cpu.cpp:117] ...
    not enough memory: you tried to allocate 9953280000 bytes

改法
--------------------------------------------------------------------------
只把 `.clone()` 去掉。边界处理（负索引、length 截断、batch_index 夹取）
全部照抄原实现，返回的是视图 —— 像素内容一模一样。

为什么视图是安全的：这条链路下游（DogkingVideoConcat 逐帧转 uint8 写盘）
只读不写。视图会引用源批次，而源批次本来就要活到裁帧结束，所以实际占用
只会少一份整段拷贝，不会多。

改的是谁
--------------------------------------------------------------------------
改的是**进程内存里的类对象**：ComfyUI 启动时把 comfy_extras/nodes_images.py
读进来，我们在插件加载阶段把 `ImageFromBatch.execute` 这个 classmethod
换成等价实现。磁盘上的官方文件一个字节都不动：

  * ComfyUI 升级 / 重装 / 校验文件完整性 → 不受影响，重装后还是原版；
  * 想回滚 → 删掉本文件、或设 DOGKING_NO_SLICE_PATCH=1，重启即可；
  * 官方哪天改了实现（源码里没有 `.clone()` 了）→ 我们不介入，直接跳过。

关掉这个补丁
--------------------------------------------------------------------------
把 `ENABLE` 改成 False，或设环境变量 `DOGKING_NO_SLICE_PATCH=1`。
"""

import inspect
import os

ENABLE = os.environ.get("DOGKING_NO_SLICE_PATCH", "") not in ("1", "true", "True", "yes")

_APPLIED = False


def _build_execute(IO, orig):
    """
    生成替换用的 execute。

    两处防御，保证官方节点以后怎么改都不会被我们带崩：

      ① 参数形状不对（官方加了新参数 / 换了名字）→ 原样交回官方实现
      ② image 不是 4 维 IMAGE → 同样交回官方实现

    @param {module} IO comfy_api.latest.IO（需要 NodeOutput）
    @param {callable} orig 官方未绑定的 execute 函数
    @returns {callable} 替换用的函数
    """
    def execute(cls, *args, **kwargs):
        try:
            image = args[0] if len(args) > 0 else kwargs["image"]
            batch_index = args[1] if len(args) > 1 else kwargs["batch_index"]
            length = args[2] if len(args) > 2 else kwargs["length"]
        except (IndexError, KeyError):
            return orig(cls, *args, **kwargs)
        if len(args) > 3 or not hasattr(image, "dim") or image.dim() != 4:
            return orig(cls, *args, **kwargs)

        s_in = image
        if batch_index < 0:
            batch_index += s_in.shape[0]
        batch_index = max(0, min(s_in.shape[0] - 1, batch_index))
        length = min(s_in.shape[0] - batch_index, length)
        # 唯一改动：去掉 .clone()，返回视图
        return IO.NodeOutput(s_in[batch_index:batch_index + length])

    return execute


def _looks_like_official(func):
    """
    官方实现里必须有 `.clone()`。没有就说明上游已经改过（或换了实现），
    这时候不介入 —— 宁可让用户吃一次 OOM，也不要把不明实现替换成我们的。
    """
    try:
        src = inspect.getsource(func)
    except (OSError, TypeError):
        return False
    return ".clone()" in src


def apply():
    """
    给 comfy_extras 的 ImageFromBatch 打上零拷贝补丁（只打一次）。

    注意：改的是**运行中的类对象**，磁盘上的 comfy_extras/nodes_images.py
    一个字节都没动。ComfyUI 升级/重装不会带着这个改动，删掉本文件或
    关掉 ENABLE 就恢复官方行为。

    @returns {bool} 本次是否真的替换了
    """
    global _APPLIED
    if not ENABLE or _APPLIED:
        return False
    try:
        from comfy_extras import nodes_images as ni
        from comfy_api.latest import IO
    except Exception as exc:                      # 非 ComfyUI 环境 / 结构变了，静默跳过
        print(f"[dogking-tools] 零拷贝裁帧补丁跳过（{exc}）")
        return False

    cls = getattr(ni, "ImageFromBatch", None)
    if cls is None or getattr(cls, "_dogking_zero_copy", False):
        return False

    orig = cls.execute.__func__
    if not _looks_like_official(orig):
        print("[dogking-tools] ImageFromBatch 实现和预期不同，零拷贝补丁不介入")
        return False

    execute = _build_execute(IO, orig)
    cls.execute = classmethod(execute)
    cls.frombatch = classmethod(execute)          # 旧别名，部分代码仍按这个名字取
    cls._dogking_zero_copy = True
    _APPLIED = True
    print("[dogking-tools] ImageFromBatch 已切到零拷贝（仅运行时替换，官方文件未改动）："
          "裁帧不再复制整段画面")
    return True


def is_applied():
    """补丁是否已经生效（测试和状态输出用）。"""
    return _APPLIED
