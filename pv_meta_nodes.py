"""
dogking-tools —— 提示词元数据嵌入 PNG（PromptVault 可读）

    DogkingPVMetaStamp   图片 + 正向提示词 + 负向提示词 → 图片（原样透传）

两个提示词口是 forceInput（**只接线，不给文本框**）：词都在上游节点里，
在节点上再摆编辑框只会让人以为手填的内容也会被写进去。

--------------------------------------------------------------------------
为什么要「自己落盘」，而不是把元数据挂在张量上
--------------------------------------------------------------------------
ComfyUI 的图片在节点之间传的是 **纯张量**（[B,H,W,C] float32），不携带任何
附带信息。PNG 的 tEXt / iTXt 文本块只在**写文件的那一刻**产生 ——

    ComfyUI 内置 SaveImage：metadata.add_text("prompt", json.dumps(prompt))

也就是说：只要提示词字符串停在某个节点的 widget 里，下游无论接哪个保存节点，
它都不会被写进 PNG。所以本节点的做法是 **自己写一份带元数据的 PNG**，同时把图片
原样返回，既不打断下游，也保证磁盘上一定有可被 PromptVault 读到的那份文件。

⚠️ 因此它**替代**保存步骤：后面再接 SaveImage 会多出一份「没有提示词」的拷贝。

--------------------------------------------------------------------------
生成参数怎么来的
--------------------------------------------------------------------------
不额外开输入口，直接读 `hidden: {"prompt": "PROMPT"}` —— 也就是**本次执行的
API prompt 全图**，里面有每个节点的 class_type 和 inputs。扫描一遍就能拿到：

    模型      ckpt_name / unet_name / model_name（Flux、Qwen 走 UNETLoader 的 unet_name）
    LoRA      lora_name + strength_model（多少个都收）
    VAE       vae_name
    采样      steps / cfg / sampler_name / scheduler / seed / denoise
    Clip skip stop_at_clip_layer 取绝对值（A1111 记的是层数，不是负偏移）
    尺寸      实际存下来的那张图，不是 EmptyLatent 的设定值

步数还有一条兜底：新版 ComfyUI 主力是 `SamplerCustomAdvanced` + `ManualSigmas`
（Qwen / Flux 工作流几乎都是），**全图根本没有 `steps` 字段**，步数藏在 sigmas 串里
（10 个 sigma 值 = 9 步）。不管这条，这类工作流一个参数都写不出来。

拼成 A1111 的一行贴在负向词后面：

    Steps: 20, Sampler: euler, Schedule: simple, CFG scale: 3.5, Seed: 123,
    Size: 1024x1024, Model: flux1-dev.safetensors, Lora_1: detail.safetensors:0.8

⚠️ 这一行必须**以 `Steps:` 开头**。PromptVault 的 parseA1111 靠
`^(?:^|\n)[ \t]*Steps[ \t]*:` 认定「从这儿起是参数」；没有这个标记，后面的
内容会被它当成**负向词的延续** —— 参数栏空白、负向词多出一串垃圾。
所以真拿不到步数时，这一行走**独立文本块**（key = `params`），由 PromptVault 的
「其余文本块 → 参数栏」兜底分支收进去，而不是硬塞进 `parameters`。

🔴 那个 key 只能是 **ASCII**：PNG 规范里文本块的 keyword 是 Latin-1，
   Pillow 的 `add_itxt` 会按 latin-1 编码 keyword —— 写成中文 `参数`
   直接抛 `UnicodeEncodeError`，整张图存不下来。值（value）才是 UTF-8。

--------------------------------------------------------------------------
为什么只写 `parameters` 一个文本块
--------------------------------------------------------------------------
PromptVault（js/png-meta.js）按这个顺序识别：

    ① parameters  → 按 A1111 规范拆成 正向 / Negative prompt: / Steps:
    ② prompt、workflow → 当 ComfyUI JSON 解析，只从 CLIPTextEncode 取词
    ③ comment（NovelAI）
    ④ negative_prompt / negative prompt / uc → 直接当负向
    ⑤ 其余块 → 能当词就当正向兜底，否则拼进「参数」栏

所以这里**只写 `parameters`**，正向、负向、参数三栏都由 ① 精确拆出来，不依赖 ③
那个「负向词特征词猜测」的启发式（靠 worst quality / blurry 这类词判断，
正向词里碰巧出现就会误判）。

🔴 反过来，**绝不能顺手塞 `positive` / `negative_prompt` / `workflow` 这些块**：
   - 塞 `positive`：正向已被 ① 填满，这个块会落到 ⑤ 的 extras 里，
     被当成「参数」拼进去 → 用户在提示词管理里看到参数栏莫名其妙多一行自己的正向词。
   - 塞 `workflow`（ComfyUI JSON）：② 会把整段 JSON 当 params 塞进「参数」栏。
   少写一块反而干净，这是刻意的取舍。

--------------------------------------------------------------------------
三条安全约定（和其它 PV 节点一致）
--------------------------------------------------------------------------
1. **绝不抛异常** —— 写盘失败也只是状态条里说清原因，图和队列照常往下走。
2. **图片原样返回**，一个像素都不改（不做缩放 / 不做量化）。
3. **写入的是 UTF-8 的 iTXt 块** —— 中文提示词用 tEXt 会被 Pillow 按 latin-1
   编码直接报错，iTXt 是 PNG 规范里唯一声明 UTF-8 的文本块。
"""

import os
import re

NODE_DIR = os.path.dirname(os.path.abspath(__file__))

try:  # 作为包被 ComfyUI 导入
    from .nodes import _blank_image
except ImportError:  # 独立测试环境（按文件路径加载）
    def _blank_image(size=64, value=0.5):
        """灰图兜底。没有 torch 时返回 None —— 独立测试环境下够用。"""
        try:
            import torch
            return torch.full((1, size, size, 3), float(value), dtype=torch.float32)
        except Exception:
            return None

# 默认文件名前缀。`folder_paths.get_save_image_path` 会自动补 `_00001_` 序号，
# 所以用户看到的文件名形如 `PromptVault_00001_.png`。
DEFAULT_PREFIX = "PromptVault"

# 与 ComfyUI 内置 SaveImage 同档的压缩级别：体积和耗时平衡点（1 最快，9 最小）
COMPRESS_LEVEL = 4

# A1111 里分隔负向提示词的那一行。PromptVault 的正则要求它出现在**行首**
# （`(?:^|\n)[ \t]*Negative prompt[ \t]*:`），所以正向词后面必须换行再接。
NEG_MARK = "Negative prompt:"

# 「参数行没有 Steps」时用的独立文本块 key。
# 🔴 必须是 ASCII：PNG 的 keyword 是 Latin-1，中文 key 会让 Pillow 抛
#    UnicodeEncodeError，整张图都存不下来（测试 test_save_metadata_extra_chunk 守着这条）。
# PromptVault 会把非保留 key 的块拼进「参数」栏，显示成 `params: Steps: ...`。
PARAMS_EXTRA_KEY = "params"

# 不写进参数行的值：denoise 默认 1 时写了只是噪音
SKIP_DENOISE = 1.0


# ---------------------------------------------------------------- 文本拼装

def build_parameters_text(positive, negative, params_line=""):
    """
    拼成 A1111 的 `parameters` 文本。

        <正向>
        Negative prompt: <负向>
        Steps: 20, Sampler: ...

    三段都是可选的，缺哪段就不写哪行。

    @param {str} positive 正向提示词
    @param {str} negative 负向提示词
    @param {str} params_line 由 `format_params_line()` 生成的参数行
    @returns {str} 全空时返回空串（调用方据此不写文本块）
    """
    pos = str(positive or "").strip()
    neg = str(negative or "").strip()
    params = str(params_line or "").strip()

    parts = []
    if pos:
        parts.append(pos)
    # 正向为空、只有负向时也要保留标记行：PromptVault 靠它认出「这一行之后是负向」，
    # 少了标记整段会被当成正向词。
    if neg:
        parts.append(f"{NEG_MARK} {neg}")
    if params:
        parts.append(params)
    return "\n".join(parts)


# ---------------------------------------------------------------- 生成参数

def _text(value):
    """取一个字符串输入；不是非空字符串就返回空串（链接形式是 list，会被挡掉）。"""
    if isinstance(value, str):
        return value.strip()
    return ""


def _number(value):
    """取一个数值输入；链接形式（[node, slot]）会被挡掉返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _sort_key(node_id):
    """
    节点 id 排序：能当整数就按数值，否则按字符串。

    为什么不用字典顺序：API prompt 里 id 是字符串，`"10" < "2"`，
    多个采样器时「第一个」会选错到 10 号而不是 2 号。
    """
    text = str(node_id)
    try:
        return (0, int(text), "")
    except ValueError:
        return (1, 0, text)


def _is_link(value):
    """
    是不是 ComfyUI 的连线描述（`[node_id, slot]`）。

    🔴 必须单独判一次：连线是 `["201", 0]` 这种**两元素数组**，看起来很像
       「两个数字的 sigmas」，不挡掉的话会算出「1 步」这种鬼值。
    """
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], int)
    )


def _steps_from_sigmas(value):
    """
    从 sigmas 串推步数：n 个 sigma 值 = n-1 步。

    为什么需要这条：新版 ComfyUI 的主力采样是 `SamplerCustomAdvanced` +
    `ManualSigmas`（Qwen / Flux 工作流几乎都是），**整张图里没有 `steps` 字段**，
    步数藏在 `sigmas` 的字符串里：`"1.0, 0.978453, ..., 0"` 10 个值 = 9 步。
    只认 `steps` 的话这类工作流一个参数都写不出来（用户实测就是这个问题）。

    @param {str|list} value `"1.0, 0.97, 0"` 或数字数组；连线会被挡掉
    @returns {int|None}
    """
    if _is_link(value):
        return None
    if isinstance(value, str):
        parts = [p for p in re.split(r"[,;\s]+", value.strip()) if p]
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        return None
    if len(parts) < 2:
        return None
    for part in parts:
        try:
            float(part)
        except (TypeError, ValueError):
            return None
    return len(parts) - 1


def collect_generation_params(prompt):
    """
    从本次执行的 API prompt 里扫出模型 / LoRA / 采样参数。

    只按 **字段名** 和 **class_type 关键词** 认，不绑死具体节点类：
    `KSampler`、`KSamplerAdvanced`、`SamplerCustomAdvanced` + `KSamplerSelect`
    + `RandomNoise` + `ManualSigmas` 这套组合拳都能收。

    采集分两轮：
      ① 先找「主采样节点」（有 `steps`，或者能从 `sigmas` 推出步数），
         采样相关字段优先从它身上取 —— 否则 hires fix 的第二个采样器会把主采样的
         sampler / seed 覆盖掉；
      ② 再补空：模型、LoRA、VAE 这些分散在别的节点上，采样字段有缺也从别处捞
         （`SamplerCustomAdvanced` 自己不带 sampler_name / seed，它们分别在
         `KSamplerSelect` 和 `RandomNoise` 里）。

    @param {dict} prompt API 格式的 prompt：`{node_id: {class_type, inputs}}`
    @returns {dict} 各字段；一个都没扫到时是空 dict
    """
    if not isinstance(prompt, dict):
        return {}

    fields = {
        "steps": None,
        "cfg": None,
        "sampler": "",
        "scheduler": "",
        "seed": None,
        "denoise": None,
        "model": "",
        "vae": "",
        "clip_skip": None,
        "loras": [],
    }

    nodes = [(nid, node) for nid, node in prompt.items() if isinstance(node, dict)]
    nodes.sort(key=lambda pair: _sort_key(pair[0]))

    def fill_sampling(inputs):
        """从某个节点的 inputs 里取采样字段；只在空着时填。"""
        if fields["cfg"] is None:
            fields["cfg"] = _number(inputs.get("cfg"))
        if not fields["sampler"]:
            fields["sampler"] = _text(inputs.get("sampler_name")) or _text(inputs.get("sampler"))
        if not fields["scheduler"]:
            fields["scheduler"] = _text(inputs.get("scheduler"))
        if fields["seed"] is None:
            # KSamplerAdvanced 用 noise_seed；RandomNoise 也是 noise_seed
            seed = _number(inputs.get("seed"))
            if seed is None:
                seed = _number(inputs.get("noise_seed"))
            fields["seed"] = seed
        if fields["denoise"] is None:
            fields["denoise"] = _number(inputs.get("denoise"))

    # ---------- ① 主采样节点 ----------
    for _nid, node in nodes:
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        steps = _number(inputs.get("steps"))
        if steps is None:
            steps = _steps_from_sigmas(inputs.get("sigmas"))
        if steps is not None:
            fields["steps"] = int(steps)
            fill_sampling(inputs)
            break

    # ---------- ② 补空：模型 / LoRA / VAE / 采样字段 ----------
    for _nid, node in nodes:
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        class_type = str(node.get("class_type") or "").lower()

        fill_sampling(inputs)

        # 模型：ckpt_name（checkpoint 类）/ unet_name（Flux、Qwen 类）/ model_name
        if not fields["model"]:
            for key in ("ckpt_name", "unet_name", "model_name"):
                name = _text(inputs.get(key))
                if name:
                    fields["model"] = name
                    break

        if not fields["vae"]:
            fields["vae"] = _text(inputs.get("vae_name"))

        # LoRA：靠 lora_name 认，不靠 class_type
        # （LoraLoader / LoraLoaderModelOnly / 各种魔改类都只有这个字段是共通的）
        name = _text(inputs.get("lora_name"))
        if name:
            strength = _number(inputs.get("strength_model"))
            if strength is None:
                strength = _number(inputs.get("strength"))
            fields["loras"].append((name, strength))

        # Clip skip：A1111 记「跳几层」，ComfyUI 的 stop_at_clip_layer 是负值
        if fields["clip_skip"] is None:
            layer = _number(inputs.get("stop_at_clip_layer"))
            if layer:
                fields["clip_skip"] = abs(int(layer))

    return fields


def format_params_line(fields, width=0, height=0):
    """
    把扫描结果拼成一行 A1111 参数。

    `Steps:` 排在第一个的规矩见模块文档：PromptVault 靠它认定「从这儿起是参数」。
    没扫到步数时这一行**不以 Steps 开头**，调用方必须改走独立文本块，
    不能直接塞进 `parameters`。

    @param {dict} fields `collect_generation_params()` 的返回值
    @param {int} width 实际存下来的宽
    @param {int} height 实际存下来的高
    @returns {str} 空串表示没有可写的参数
    """
    if not isinstance(fields, dict):
        return ""

    # 只有尺寸不算「有参数」—— 纯裁切 / 放大的图没有采样信息，
    # 给它写一行 `Size: 512x512` 只是噪音。
    meaningful = any([
        fields.get("steps") is not None,
        fields.get("sampler"),
        fields.get("scheduler"),
        fields.get("cfg") is not None,
        fields.get("seed") is not None,
        fields.get("model"),
        fields.get("vae"),
        fields.get("clip_skip"),
        fields.get("denoise") is not None,
        bool(fields.get("loras")),
    ])
    if not meaningful:
        return ""

    parts = []
    if fields.get("steps") is not None:
        parts.append(f"Steps: {fields['steps']}")
    if fields.get("sampler"):
        parts.append(f"Sampler: {fields['sampler']}")
    if fields.get("scheduler"):
        parts.append(f"Schedule: {fields['scheduler']}")
    cfg = fields.get("cfg")
    if cfg is not None:
        parts.append(f"CFG scale: {cfg:g}")
    if fields.get("seed") is not None:
        parts.append(f"Seed: {int(fields['seed'])}")
    if width and height:
        parts.append(f"Size: {int(width)}x{int(height)}")

    if fields.get("model"):
        parts.append(f"Model: {fields['model']}")
    for index, (name, strength) in enumerate(fields.get("loras") or [], 1):
        if strength is None:
            parts.append(f"Lora_{index}: {name}")
        else:
            parts.append(f"Lora_{index}: {name}:{strength:g}")
    if fields.get("vae"):
        parts.append(f"VAE: {fields['vae']}")
    if fields.get("clip_skip"):
        parts.append(f"Clip skip: {fields['clip_skip']}")
    denoise = fields.get("denoise")
    if denoise is not None and abs(denoise - SKIP_DENOISE) > 1e-6:
        parts.append(f"Denoise: {denoise:g}")

    return ", ".join(parts)


# ---------------------------------------------------------------- 落盘

def _resolve_output_dir():
    """ComfyUI 输出目录；独立测试环境（没有 folder_paths）退到插件目录下 output/。"""
    try:
        import folder_paths
        return folder_paths.get_output_directory()
    except Exception:
        fallback = os.path.join(NODE_DIR, "output")
        os.makedirs(fallback, exist_ok=True)
        return fallback


def _next_file_path(prefix, output_dir, width, height):
    """
    生成下一个不重名的路径 → (完整目录, 文件名, 子目录)。

    优先用 ComfyUI 的 `get_save_image_path`：它会处理 `%date%` 这类占位符、
    自动建子目录、并且按已有文件算下一个序号（和 SaveImage 的编号规则一致）。
    拿不到就退化成「时间戳命名」，功能不受影响。
    """
    try:
        import folder_paths
        folder, name, counter, subfolder, _p = folder_paths.get_save_image_path(
            prefix, output_dir, width, height
        )
        return folder, f"{name}_{counter:05}_.png", subfolder
    except Exception:
        import time
        folder = output_dir
        os.makedirs(folder, exist_ok=True)
        return folder, f"{prefix}_{int(time.time() * 1000)}.png", ""


def _to_pil(image):
    """
    ComfyUI 的 float 张量（单张，[H,W,C]）→ PIL.Image。

    `image` 从 batch 里切出来时已经是 3 维；C=1 的灰度图 PIL 不认，
    要先 squeeze 成 2 维。
    """
    import numpy as np
    from PIL import Image

    arr = image
    if hasattr(arr, "cpu"):
        arr = arr.cpu().numpy()
    arr = np.clip(255.0 * np.asarray(arr), 0, 255).astype(np.uint8)
    if arr.ndim == 3 and arr.shape[2] == 1:
        arr = arr[:, :, 0]
    return Image.fromarray(arr)


def save_with_metadata(pil_img, text, prefix, extra_chunks=None):
    """
    把一张 PIL 图存成 PNG，并把 `parameters` 文本块写进去。

    🔴 用 `add_itxt` 而不是 `add_text`：`add_text` 走 latin-1，中文提示词会抛
       UnicodeEncodeError（Pillow 对新版会转 iTXt，但老版本直接失败，行为不一致）。
       显式写 iTXt，编码固定 UTF-8，PromptVault 那边按 iTXt 解析没问题。

    @param {str} text `parameters` 块内容，空串则不写这个块
    @param {Iterable} extra_chunks 额外的 (key, value) 文本块。只在「参数行不以
        Steps 开头」时用：PromptVault 会把非保留 key 的块并进「参数」栏，
        这样参数不会污染负向词。见模块文档。
    @returns {(dict, str)} (供节点预览用的描述, 错误信息)。成功时错误为空串。
        描述形如 `{"filename": "x.png", "subfolder": "", "type": "output"}`，
        和内置 SaveImage 返回的 `ui.images` 元素同构 —— 前端靠它把图显示在节点上。
    """
    from PIL import PngImagePlugin

    output_dir = _resolve_output_dir()
    folder, filename, subfolder = _next_file_path(
        prefix, output_dir, pil_img.width, pil_img.height
    )

    info = PngImagePlugin.PngInfo()
    if text:
        info.add_itxt("parameters", text)
    for key, value in (extra_chunks or ()):
        if not value:
            continue
        try:
            info.add_itxt(key, value)
        except (UnicodeEncodeError, ValueError):
            # keyword 只能是 Latin-1。传入非 ASCII key 时不抛（本节点「绝不抛异常」），
            # 退到固定的 ASCII key —— 参数栏里多一行前缀，总比整张图存不下来强。
            info.add_itxt(PARAMS_EXTRA_KEY, value)

    path = os.path.join(folder, filename)
    pil_img.save(path, pnginfo=info, compress_level=COMPRESS_LEVEL)

    return {"filename": filename, "subfolder": subfolder, "type": "output"}, ""


def _clean_prefix(prefix):
    """前缀清洗：去掉路径分隔符和空白，空值退回默认名。"""
    text = str(prefix or "").strip().replace("\\", "/")
    text = text.strip("/")
    if not text:
        return DEFAULT_PREFIX
    # 允许用户写 `子目录/名字`（get_save_image_path 支持），只拦 `..` 这类越级
    parts = [p for p in text.split("/") if p and p not in (".", "..")]
    return "/".join(parts) or DEFAULT_PREFIX


# ================================================================ 节点

class DogkingPVMetaStamp:
    """把提示词与生成参数写进 PNG 元信息，图片原样透传。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                # 顺序就是节点上的显示顺序：图 → 正向 → 负向 → 文件名。
                # 形参名必须和 `stamp()` 完全一致（ComfyUI 按**名字**传参），
                # 拼错一个字不会报错，只会让那一栏空着落盘 —— 极难发现。
                "图片": ("IMAGE", {
                    "tooltip": "要写入元数据的图片；原样透传，不改尺寸不改像素",
                }),
                # 🔴 forceInput：这两栏**只做接线口，不给文本框**。
                #    它们是纯管道，词都在上游节点里（PromptVault 节点的「提示词」输出、
                #    或任意产出字符串的节点）。在节点上再摆两个编辑框只会让人以为
                #    手填的内容也会被写进去，而实际上有线连着时手填值会被覆盖。
                "正向提示词": ("STRING", {
                    "forceInput": True,
                    "tooltip": "接上游的字符串输出；写进 PNG 的 parameters 块",
                }),
            },
            "optional": {
                # 负向词**必须是可选口**：大量工作流根本没有负向词，设成必需会让
                # 节点直接报「必需的输入插槽没有连接」，逼用户去接一个空字符串节点。
                # 不接时走 `stamp()` 的默认空串，PNG 里就不写 Negative prompt: 行。
                "负向提示词": ("STRING", {
                    "forceInput": True,
                    "tooltip": "接上游的字符串输出；写成 Negative prompt: 行。不接 = 不写负向",
                }),
                "文件名前缀": ("STRING", {
                    "default": DEFAULT_PREFIX,
                    "multiline": False,
                    "tooltip": "存到 ComfyUI 的 output/ 下，自动补 _00001_ 序号；支持 子目录/名字",
                }),
            },
            # PROMPT = 本次执行的 API prompt 全图，用来扒模型 / LoRA / 采样参数。
            # 这是**只读**的，不会改动任何东西。
            "hidden": {"prompt": "PROMPT"},
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("图片",)
    FUNCTION = "stamp"
    CATEGORY = "dogking-tools"
    # OUTPUT_NODE：即使这个节点的图片没接下游，也应该执行 ——
    # 它的产物是磁盘上的文件，不是输出线上的数据。
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        # 有副作用（每次都要落一个新文件），不能因为输入没变就走缓存
        return float("NaN")

    def stamp(self, 图片, 正向提示词="", 负向提示词="", 文件名前缀=DEFAULT_PREFIX, prompt=None):
        prefix = _clean_prefix(文件名前缀)

        # 图片为空：状态条说清楚 + 给灰图。
        # 🔴 **不能返回 None** —— 这个图片口是透传的，用户早就连好下游了，
        #    None 会让下游节点直接崩，而且他只会以为是下游的问题。
        if 图片 is None:
            return self._result(_blank_image(), "没有输入图片，未写入")

        # 参数扫一次就够（同一张图里的模型/采样设置不变），尺寸按每张图各自算
        fields = collect_generation_params(prompt)

        try:
            batch = len(图片)
        except Exception:
            batch = 1

        saved = []       # 相对路径（状态条 / badge 用）
        previews = []    # ui.images（前端在节点上画缩略图用，和 SaveImage 同构）
        errors = []
        params_used = ""

        for index in range(max(1, batch)):
            try:
                single = 图片[index] if batch > 1 else 图片[0]
                pil_img = _to_pil(single)
                params_line = format_params_line(fields, pil_img.width, pil_img.height)
                params_used = params_line

                # 参数行以 Steps 开头 → 直接拼进 parameters（A1111 标准位置）。
                # 没有 Steps（图里没有采样器，或 sigmas 推不出步数）→ 单独写一块，
                # 交给 PromptVault 的「其余文本块 → 参数栏」兜底分支，
                # 绝不能让它接在 Negative prompt: 后面（会被当成负向词）。
                extra = ()
                if params_line and fields.get("steps") is None:
                    text = build_parameters_text(正向提示词, 负向提示词)
                    extra = ((PARAMS_EXTRA_KEY, params_line),)
                    print("[dogking-tools]   没有 Steps，参数改走独立文本块")
                else:
                    text = build_parameters_text(正向提示词, 负向提示词, params_line)

                entry, _err = save_with_metadata(pil_img, text, prefix, extra)
                previews.append(entry)
                rel = entry["filename"]
                if entry.get("subfolder"):
                    rel = f"{entry['subfolder']}/{rel}"
                saved.append(rel)
                print(f"[dogking-tools] 已写入提示词元数据：{rel}")
                if params_line:
                    print(f"[dogking-tools]   参数：{params_line}")
            except Exception as e:  # 单个失败不影响其它张
                errors.append(str(e))

        status = self._status_text(正向提示词, 负向提示词, params_used, saved, errors)
        badge = self._badge_text(params_used, saved, errors)
        return {
            "ui": {
                # images 让节点自己显示刚存的那张图（等价于「保存图像」的预览），
                # 存完能立刻看见，不用去 output 目录翻。
                "images": previews,
                "dogking_status": [status],
                "dogking_badge": [badge],
            },
            "result": (图片,),
        }

    @staticmethod
    def _badge_text(params_line, saved, errors):
        """
        标题栏 badge 的短文案。

        为什么不复用 `dogking_status`：状态条那句有 40 多个字符，塞进标题栏会把
        节点标题挤掉。badge 只放「文件名 + 有没有参数」这种一眼够用的信息。
        """
        if not saved:
            return "写入失败" if errors else ""
        label = os.path.basename(saved[0])
        if len(saved) > 1:
            label += f" 等 {len(saved)} 张"
        if not params_line:
            # 没有参数说明图里没有采样器（或扫不到），这点必须让人看见，
            # 否则他会以为参数"没生效"是 bug。
            label += " · 无参数"
        return label[:32]

    @staticmethod
    def _status_text(positive, negative, params_line, saved, errors):
        """状态条文案。用户靠它确认「到底写进去了什么、文件在哪」。"""
        if not saved and errors:
            return f"写入失败：{errors[0][:80]}"

        if len(saved) == 1:
            status = f"已写入 {saved[0]}"
        else:
            status = f"已写入 {len(saved)} 张（{saved[0]} 等）"

        # 词长也报出来：用户最容易犯的错是「线没连上，写进去的是空字符串」，
        # 只有文件名会让他以为成功了。
        pos_len = len(str(positive or "").strip())
        neg_len = len(str(negative or "").strip())
        if not pos_len and not neg_len:
            status += " · 提示词为空，PNG 里没有文本块"
        else:
            word = f"正向 {pos_len} 字"
            if neg_len:
                word += f" / 负向 {neg_len} 字"
            status += " · " + word

        if params_line:
            # 只报「几项」，完整内容在控制台 —— 状态条塞不下整行
            status += f" · 参数 {params_line.count(',') + 1} 项"
        if errors:
            status += f" · {len(errors)} 张失败（{errors[0][:40]}）"
        return status

    @staticmethod
    def _result(image, message):
        """异常路径的统一出口：带上状态文案，图片口给灰图（绝不能是 None）。"""
        return {"ui": {"dogking_status": [message]}, "result": (image,)}


# ---------------------------------------------------------------- 注册映射

NODE_CLASS_MAPPINGS = {
    "DogkingPVMetaStamp": DogkingPVMetaStamp,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DogkingPVMetaStamp": "提示词嵌入PNG元数据（dogking-tools）",
}
