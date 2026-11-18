"""
dogking-tools —— 数字人音频分段

一个节点管完整件事：选音频 → 看波形 → 切段 → 给每段配提示词 → 按段吐给下游。

--------------------------------------------------------------------------
它解决的问题
--------------------------------------------------------------------------
数字人视频的时长是**按段**来的（模型一次只吃几秒音轨），但音频往往是一整段口播
或一首歌。以前要在外面用「音频裁剪」节点反复改起止时间、一次次重跑，切 10 段就是
10 次手工操作。这个节点把分段表存在节点里，一次配好，之后按顺序自动跑完。

--------------------------------------------------------------------------
三段数据流
--------------------------------------------------------------------------
1. **分段表**（前端画波形、写提示词）→ 序列化成 JSON 存进 `分段数据` widget。
   格式：`{"segments": [{"start": 0.0, "end": 5.2, "prompt": ""}, ...]}`
   `start` / `end` 是**秒**（浮点），前端负责时间换算。
2. **音频**：优先用 `音频` 输入口（上游给的 AUDIO），没有就用 `文件路径` 读本机文件。
3. **提示词**：每段自己的提示词优先，空着就回退 `全局提示词` —— 这就是「独立提示词
   覆盖全局」的实现，覆盖逻辑放在后端，前端只管把两栏都填进 JSON。

   输出侧**只有一个「提示词」口**，用户不用自己判断该接哪个：这个口吐出来的永远是
   「该段最终该用的词」，有独立词就是独立词，没有才是全局词。
   （早先还单独开过一个「全局提示词」口，实测只会让人接错 —— 接它等于把段内词全顶掉，
   正好把覆盖功能废了。）

--------------------------------------------------------------------------
两种输出模式
--------------------------------------------------------------------------
- **单段**：`当前段` 指哪段就出哪段，不推进游标。适合调试某一段。
- **顺序**：按分段表顺序每次出下一段，游标存在 `state.json` 里（按节点 id），
  跑完一轮自动归零。配合 `自动连续` 会在每次执行后发事件，让前端自动排下一次 ——
  和插件里「提示词批量加载」用的是同一套机制，只是事件名不同
  （`dogking-tools.audio.next`），避免两个节点互相触发。

  游标的重置条件是**分段的时间轴变了**（切点增删改），而不是每执行一次都重置。
  只改某段的提示词不会重置游标 —— 否则用户改一个错字就得从头再跑一遍。

--------------------------------------------------------------------------
「段序号」和「段数」不是一回事
--------------------------------------------------------------------------
- **段序号**：这次吐出来的是第几段（1 起），单段模式下就是 `当前段` 那个数，
  顺序模式下跟着游标走。
- **段数**：**这次要产出几段** —— 不是切了多少段。单段模式恒为 1（一次只吐一段），
  顺序模式才是总段数。拿它当循环次数 / 批量上限时，单段不该被「切了 8 段」牵连。

  面板状态行上的 `第 3 / 8 段` 用的是真实总段数（给人看的），和这里这个输出口无关。

--------------------------------------------------------------------------
几个必须守住的点
--------------------------------------------------------------------------
1. **切点越界一律裁剪**，不报错：前端拖到音频末尾之外是常事，裁到文件长度即可。
2. **start >= end 的段直接丢掉**，不抛异常 —— 前端正在拖动时中间状态经常是这样。
3. 没有分段 / 没有音频时给**中文错误**，说清去哪儿点。
4. 切的都是 `clone()` 出来的新张量，不动上游 waveform。
"""

import hashlib
import json
import os

try:  # 作为包被 ComfyUI 导入
    from .nodes import _load_state, _save_state, _state_lock
except ImportError:  # 独立测试环境
    import threading

    _state_lock = threading.Lock()
    _MEM_STATE = {}

    def _load_state():
        return dict(_MEM_STATE)

    def _save_state(data):
        _MEM_STATE.clear()
        _MEM_STATE.update(data)


MODE_SINGLE = "单段"
MODE_SEQUENCE = "顺序"
MODES = [MODE_SINGLE, MODE_SEQUENCE]

# 顺序模式执行完发的事件。**不能**复用 `dogking-tools.next`：
# 那个是「提示词批量加载」在用，两个节点同时存在时前端会连续排两次队列。
EVENT_NEXT = "dogking-tools.audio.next"

# 没有分段时的默认提示
EMPTY_HINT = "还没有分段：点面板上的「自动等分」，或在波形上点一下加切点"

# 允许的音频扩展名。选文件框、波形接口、试听代理共用这一份 ——
# 三处各写一遍必然有一处漏掉某种格式。
AUDIO_EXTENSIONS = (".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".wma")


# ---------------------------------------------------------------- 分段表

def parse_segments(raw):
    """
    把前端写的分段 JSON 解析成干净的段列表。

    容错优先：这是前端实时写进来的，拖动过程中会短暂出现 `start >= end`、
    `null`、字符串数字等中间状态，全部当「这段还没成形」丢掉，不抛异常。

    @param {str|dict|list} raw `{"segments":[...]}` / 数组 / JSON 字符串
    @returns {list} `[{"start": float, "end": float, "prompt": str}]`，按 start 升序
    """
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, (list, tuple)):
        data = list(raw)
    else:
        try:
            data = json.loads(str(raw or "").strip() or "{}")
        except Exception:
            data = {}

    if isinstance(data, dict):
        segments = data.get("segments")
    else:
        segments = data
    if not isinstance(segments, (list, tuple)):
        return []

    out = []
    for item in segments:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item.get("start"))
            end = float(item.get("end"))
        except (TypeError, ValueError):
            continue
        if not (end > start):
            continue
        out.append({
            "start": max(0.0, start),
            "end": end,
            "prompt": str(item.get("prompt") or "").strip(),
        })
    out.sort(key=lambda seg: (seg["start"], seg["end"]))
    return out


def timeline_signature(segments):
    """
    分段「时间轴」的指纹，用来判断游标要不要重置。

    故意**不包含提示词**：用户改某段提示词时游标应该保持不动，
    否则改一个错字就得从第一段重新跑。
    """
    joined = "|".join("%.3f-%.3f" % (seg["start"], seg["end"]) for seg in segments)
    return hashlib.md5(("%d|" % len(segments) + joined).encode("utf-8")).hexdigest()[:12]


def resolve_prompt(segment, global_prompt):
    """
    该段的提示词：自己写了就用自己，空着回退全局。

    前后空白会先 strip —— 前端输入框里点一下再点走，很容易留下一个空格，
    那不该被当成「这段有独立提示词」。
    """
    own = str(segment.get("prompt") or "").strip()
    if own:
        return own
    return str(global_prompt or "").strip()


# ---------------------------------------------------------------- 音频

def resolve_audio_path(path):
    """
    把用户给的路径解析成真实文件路径。

    两种来源都要认：
      · 系统选择框给的**绝对路径**
      · 前端上传到 ComfyUI `input/` 后的**相对路径**（`voice.wav` 或 `sub/voice.wav`）

    @returns {str} 存在的绝对路径；找不到返回空串
    """
    text = str(path or "").strip().strip('"')
    if not text:
        return ""
    if os.path.isfile(text):
        return text

    try:
        import folder_paths

        candidate = folder_paths.get_annotated_filepath(text)
        if candidate and os.path.isfile(candidate):
            return candidate
        candidate = os.path.join(folder_paths.get_input_directory(), text)
        if os.path.isfile(candidate):
            return candidate
    except Exception:
        pass
    return ""


def load_waveform(path):
    """
    读音频文件 → `(waveform[C,L], sample_rate)`。

    🔴 **绝不要用 `torchaudio.load()`。** 这台机器的 torchaudio 2.10 把解码全部交给
    torchcodec，而 `python_embeded` 里那个 torchcodec 的 DLL 和 torch 版本对不上 ——
    一调用就弹 Windows 的「无法定位程序输入点 torch_dtype_float8_e8m0fnu 于
    libtorchcodec_image.dll」，那个模态框不点掉，请求线程就一直卡着，
    面板永远停在「解码中…」。

    所以解码走这条链，全是纯 Python / 自带二进制，不碰 torch 的音频后端：
        ① soundfile（libsndfile）—— wav / flac / ogg / mp3，最快
        ② PyAV（ffmpeg 绑定）—— m4a / mp4 / aac / webm 这些容器，视频里抽音轨也行
        ③ 标准库 wave —— 前两个都没装时，至少 wav 还能读

    @returns {(torch.Tensor, int)} waveform 形状 [C, L]，float32
    @throws {ValueError} 三种方式都读不出来
    """
    import numpy as np
    import torch

    errors = []

    # ① soundfile
    try:
        import soundfile as sf

        data, sr = sf.read(path, dtype="float32", always_2d=True)  # [L, C]
        if data.size:
            return torch.from_numpy(np.ascontiguousarray(data.T)), int(sr)
        errors.append("soundfile: 文件里没有采样点")
    except Exception as e:  # noqa: BLE001
        errors.append("soundfile: %s" % e)

    # ② PyAV
    try:
        wav, sr = _load_with_av(path)
        return wav, sr
    except Exception as e:  # noqa: BLE001
        errors.append("pyav: %s" % e)

    # ③ 标准库 wave
    try:
        import wave

        with wave.open(path, "rb") as fh:
            channels = fh.getnchannels()
            width = fh.getsampwidth()
            sr = fh.getframerate()
            raw = fh.readframes(fh.getnframes())

        dtype = {1: np.uint8, 2: np.int16, 4: np.int32}.get(width)
        if dtype is None:
            raise ValueError("不支持的 wav 位深：%s 字节" % width)
        data = np.frombuffer(raw, dtype=dtype).astype(np.float32)
        if dtype is np.uint8:
            data = (data - 128.0) / 128.0
        else:
            data = data / float(np.iinfo(dtype).max)
        if channels > 1:
            data = data.reshape(-1, channels).T
        else:
            data = data.reshape(1, -1)
        return torch.from_numpy(np.ascontiguousarray(data)), int(sr)
    except Exception as e:  # noqa: BLE001
        errors.append("wave: %s" % e)

    raise ValueError("解码失败（%s）" % "；".join(errors))


def _load_with_av(path):
    """
    用 PyAV 解码任意容器 → `(waveform[C,L], sample_rate)`。

    先交给 AudioResampler 转成 `fltp`（planar float32）：容器里的原始格式可能是
    s16 / flt / packed 交错，形状随格式变，直接 `to_ndarray()` 出来的布局不一致，
    重采样器统一之后拿到的永远是 [C, L]。
    """
    import av
    import numpy as np
    import torch

    with av.open(path) as container:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise ValueError("这个文件里没有音频轨")

        rate = int(stream.rate or 0)
        layout = stream.layout.name or "stereo"
        resampler = av.AudioResampler(format="fltp", layout=layout, rate=rate)

        blocks = []
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                blocks.append(out.to_ndarray())
        for out in resampler.resample(None):  # 冲掉尾部缓冲
            blocks.append(out.to_ndarray())

    if not blocks:
        raise ValueError("解码出来是空的")
    data = np.concatenate(blocks, axis=1).astype(np.float32, copy=False)
    return torch.from_numpy(np.ascontiguousarray(data)), rate


def to_comfy_audio(waveform, sample_rate):
    """统一成 ComfyUI 的 AUDIO 结构：`{"waveform": [B,C,L], "sample_rate": int}`。"""
    import torch

    if not torch.is_tensor(waveform):
        waveform = torch.as_tensor(waveform)
    if waveform.dim() == 1:            # [L]
        waveform = waveform.unsqueeze(0).unsqueeze(0)
    elif waveform.dim() == 2:          # [C,L]
        waveform = waveform.unsqueeze(0)
    return {"waveform": waveform, "sample_rate": int(sample_rate)}


def audio_duration(audio):
    """AUDIO 的时长（秒）。拿不到就返回 0。"""
    try:
        length = int(audio["waveform"].shape[-1])
        rate = int(audio["sample_rate"])
        return length / float(rate) if rate > 0 else 0.0
    except Exception:
        return 0.0


def parse_range(raw):
    """
    从分段 JSON 里取「选区」——波形上最外侧那两条边界之间的一段。

    选区决定的是「整条音频要哪一截」，和怎么分段无关。单独解析它，是因为下游
    「分段视频合并」最后要拿**整条**音轨压进成片，而「音频」口只吐当前段。

    @returns {tuple|None} `(start, end)` 秒；老工作流没有 range 字段时返回 None
    """
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, (list, tuple)):
        return None
    else:
        try:
            data = json.loads(str(raw or "").strip() or "{}")
        except Exception:
            return None

    if not isinstance(data, dict):
        return None
    rng = data.get("range")
    if not isinstance(rng, dict):
        return None
    try:
        start = float(rng.get("start"))
        end = float(rng.get("end"))
    except (TypeError, ValueError):
        return None
    if not (end > start):
        return None
    return max(0.0, start), end


def slice_audio(audio, start, end):
    """
    按秒切一段出来。

    越界一律裁剪（拖到末尾之外很常见）；切出来是 `clone()`，
    下游改它不会污染上游波形。
    """
    rate = int(audio["sample_rate"])
    waveform = audio["waveform"]
    total = int(waveform.shape[-1])

    begin = max(0, min(total, int(round(float(start) * rate))))
    stop = max(begin, min(total, int(round(float(end) * rate))))
    if stop <= begin:
        raise ValueError(
            "第 %.2f–%.2f 秒切出来是空的（音频总长 %.2f 秒）" % (start, end, total / float(rate or 1))
        )
    return {"waveform": waveform[..., begin:stop].clone(), "sample_rate": rate}


def compute_peaks(path, buckets=1200):
    """
    给前端画波形用的峰值数组。

    做法：把单声道化后的波形分成 `buckets` 个桶，每桶取绝对值最大，再按全局峰值归一化。
    桶数固定（不是按秒采样）—— 前端画布宽度在变，固定桶数能保证画出来永远是满的。

    @returns {(float, int, list, float)} (时长秒, 采样率, 峰值 0~1, 原始峰值)
    @throws {ValueError} 文件读不出来或没有样本
    """
    import torch

    waveform, rate = load_waveform(path)
    if waveform.dim() == 3:                 # [B,C,L] → 第一个 batch
        waveform = waveform[0]
    if waveform.dim() == 2:                 # [C,L]
        waveform = waveform.mean(dim=0)
    if waveform.numel() == 0:
        raise ValueError("这个文件是空的，没有可画的波形")

    total = int(waveform.shape[-1])
    count = max(1, min(int(buckets), total))
    chunk = total // count
    trimmed = waveform[: chunk * count].reshape(count, chunk).abs().amax(dim=1)
    raw_peak = float(trimmed.max()) if trimmed.numel() else 0.0
    scale = raw_peak if raw_peak > 0 else 1.0
    peaks = (trimmed / scale).clamp(0.0, 1.0).tolist()

    return total / float(rate), rate, peaks, raw_peak


# ================================================================ 节点

class DogkingAudioSplit:
    """
    数字人音频分段：波形上切段、每段配提示词，按段输出音频 + 提示词。

    面板（前端）负责波形、切点、每段提示词；节点只做「按游标取一段 + 切片 + 合并提示词」。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                # 前端把分段表写进这里（widget 被改成隐藏类型，节点上看不到，
                # 但工作流保存时会带上，所以每段的提示词不会丢）。
                "分段数据": ("STRING", {
                    "default": "",
                    "multiline": True,
                    "dynamicPrompts": False,
                    "tooltip": "内部字段：面板生成的 {segments:[{start,end,prompt}]}，不用手改",
                }),
                "全局提示词": ("STRING", {
                    "default": "",
                    "multiline": True,
                    "dynamicPrompts": False,
                    "tooltip": "所有段共用的提示词；某段自己写了提示词时以该段为准",
                }),
                "输出模式": (MODES, {
                    "default": MODE_SEQUENCE,
                    "tooltip": "单段 = 只出「当前段」；顺序 = 每次出下一段，跑完自动归零",
                }),
                "当前段": ("INT", {
                    "default": 1, "min": 1, "max": 999, "step": 1,
                    "tooltip": "单段模式下生效；顺序模式忽略它",
                }),
                "自动连续": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "顺序模式下每跑完一段自动排下一段，直到全部跑完",
                }),
                "文件路径": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "本机音频文件；接了「音频」输入口时以输入口为准",
                }),
                "前置重叠帧": ("INT", {
                    "default": 0, "min": 0, "max": 96, "step": 1,
                    "tooltip": "本段音频往前多带几帧（24fps）上一段的尾巴，给「上一段末帧续接」的"
                               "引导帧当时间基准。必须与画面「去重裁剪起点」完全一致，否则每段"
                               "音画错位、口型越跑越快。默认 0 = 不前置（不影响其它工作流）；"
                               "接了续接节点时填成和裁剪起点一样（22 帧方案 → 22）",
                }),
            },
            "optional": {
                "音频": ("AUDIO", {
                    "forceInput": True,
                    "tooltip": "上游给的音频；接了就用它，忽略「文件路径」",
                }),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    # 输出里**只有一个**「提示词」口：段内有独立词就出独立词，没有就出全局词。
    # 不再单独开「全局提示词」口 —— 两个口摆在一起，用户不知道接哪个，
    # 接错了还会把全局词无脑顶掉段内词，等于把「覆盖」功能绕过去了。
    RETURN_TYPES = ("AUDIO", "STRING", "INT", "INT", "FLOAT", "AUDIO", "AUDIO")
    RETURN_NAMES = ("音频", "提示词", "段序号", "段数", "本段时长", "选区音频", "对齐音频")
    FUNCTION = "split"
    CATEGORY = "dogking-tools"
    DESCRIPTION = (
        "数字人音频分段：波形上自由切段，每段可写独立提示词（覆盖全局提示词）；"
        "支持单段输出与顺序输出（配合自动连续按段跑完整个音频）。"
        "「提示词」输出口已合并：有独立词出独立词，没有就出全局词。"
    )

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        # 顺序模式的游标是外部状态（state.json），输入没变也必须重新执行，
        # 否则 ComfyUI 会拿上次的缓存 —— 表现为「每次都吐同一段」。
        return float("NaN")

    def split(self, 分段数据, 全局提示词, 输出模式, 当前段, 自动连续, 文件路径,
              前置重叠帧=0, 音频=None, unique_id=None):
        key = str(unique_id or "0")

        segments = parse_segments(分段数据)
        if not segments:
            raise ValueError(EMPTY_HINT)

        audio = self._resolve_audio(音频, 文件路径)
        total = len(segments)

        # 「选区音频」= 整条选区（裁掉头尾之后），不是某一段。
        # 下游「分段视频合并」最后要拿整条音轨压进成片，而「音频」口只给当前段 ——
        # 用户在面板上传的音频 + 框的选区都在这个节点里，外面拿不到，所以从这里吐一份。
        # 老工作流没有 range 字段时直接给整条（和以前的行为一致）。
        sel_range = parse_range(分段数据)
        if sel_range:
            try:
                选区音频 = slice_audio(audio, sel_range[0], sel_range[1])
            except ValueError:
                选区音频 = audio      # 选区越界（音频比记录短）就退回整条，别让节点红
        else:
            选区音频 = audio

        if 输出模式 == MODE_SINGLE:
            index = max(1, min(int(当前段), total)) - 1
            more = False
            # 「段数」口报的是**这次要产出几段**，不是切了多少段。
            # 单段模式一次只吐一段，所以这里是 1 —— 拿它当循环次数 / 批量上限时，
            # 单段不该被别人切了 8 段牵连。
            out_total = 1
        else:
            index, more = self._next_index(key, segments, total)
            out_total = total

        segment = segments[index]

        # 段 2 以后把音频往前多带 `前置重叠帧` 帧（上一段的尾巴）。
        #
        # 为什么必须这么做：画面那头「上一段末帧续接」把上一段的最后 N 帧钉在输出
        # 第 0..N-1 帧上，模型从第 N 帧才回到本段音频的起点。生成完再裁掉这 N 帧，
        # 留下来的画面就比音频整体超前 N 帧 —— 每段累加，第 2 段起口型就"快一点"。
        # 音频分支是 SetLatentNoiseMask(0) 锁死的原声，时间轴以它为准，所以只能让
        # 音频也把这段前导带进来，两者才对得上。
        #
        # 「本段时长」仍然只报真正的段长（end - start），不能把前导算进去：
        # 下游的帧长算式和裁剪长度都基于它，多算了会连锁出错。
        overlap = max(0, int(前置重叠帧))
        lead = overlap / 24.0 if index > 0 else 0.0
        piece = slice_audio(audio, max(0.0, segment["start"] - lead), segment["end"])
        # 「对齐音频」= 不带前置的那一份，长度正好等于裁剪后的画面。
        #
        # 画面那头把前 22 帧引导裁掉了，所以带前置的「音频」比画面长 22 帧；
        # 拿它直接配预览视频，声音就会比画面早 0.917 秒（第 2 段起每一段都这样）。
        # 生成要前置（给被钉住的画面当时间基准），预览要对齐 —— 两个口分开给。
        aligned = piece if lead <= 0 else slice_audio(audio, segment["start"], segment["end"])
        prompt = resolve_prompt(segment, 全局提示词)
        duration = max(0.0, float(segment["end"]) - float(segment["start"]))

        status = "第 %d/%d 段（%.2f–%.2f 秒）%s%s" % (
            index + 1, total, segment["start"], segment["end"],
            " · 独立提示词" if segment["prompt"] else "",
            (" · 音频前置 %d 帧" % overlap) if lead > 0 else "",
        )
        print("[dogking-tools] 音频分段：%s" % status)

        # 通知前端还要不要继续排下一段。和「提示词批量加载」用的事件分开，
        # 两个节点同时存在时不会互相触发。
        try:
            from server import PromptServer

            PromptServer.instance.send_sync(EVENT_NEXT, {
                "node": key,
                "index": index + 1,
                "total": total,
                "should_continue": bool(more and 自动连续),
            })
        except Exception:
            pass

        return {
            "ui": {
                "dogking_status": [status],
                "dogking_badge": ["%d/%d · %.1fs" % (index + 1, total, duration)],
                "dogking_index": [index + 1],
            },
            "result": (piece, prompt, index + 1, out_total, duration, 选区音频, aligned),
        }

    @staticmethod
    def _resolve_audio(audio_input, file_path):
        """输入口优先，其次本机文件。都没有就给出能照着做的中文报错。"""
        if audio_input is not None:
            return to_comfy_audio(audio_input["waveform"], audio_input["sample_rate"])

        resolved = resolve_audio_path(file_path)
        if not resolved:
            raise ValueError(
                "没有音频：点面板上的「选择音频」选一个文件，"
                "或者把上游的 AUDIO 接到「音频」输入口"
            )
        waveform, rate = load_waveform(resolved)
        return to_comfy_audio(waveform, rate)

    @staticmethod
    def _next_index(key, segments, total):
        """
        顺序模式的游标：取当前下标，然后把游标推到下一个。

        @returns {(int, bool)} (本次下标, 后面还有没有)
        """
        signature = timeline_signature(segments)

        with _state_lock:
            state = _load_state()
            record = state.get(key)
            # state.json 被手工改坏时 record 可能不是 dict，按「没有记录」处理
            if not isinstance(record, dict):
                record = {}

            # 切点变了 → 从头开始；只改提示词不会改签名，游标保持
            if record.get("audio_sig") != signature:
                record = {"audio_sig": signature, "index": 0}

            index = int(record.get("index", 0))
            if index < 0 or index >= total:
                index = 0

            record["index"] = index + 1
            more = (index + 1) < total
            state[key] = record
            _save_state(state)

        return index, more


# ---------------------------------------------------------------- 注册映射

NODE_CLASS_MAPPINGS = {
    "DogkingAudioSplit": DogkingAudioSplit,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DogkingAudioSplit": "数字人音频分段（dogking-tools）",
}
