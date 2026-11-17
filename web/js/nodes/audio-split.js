/**
 * 数字人音频分段 —— 面板
 *
 * 目标：在这个节点里完成「选音频 → 看波形 → 切段 → 每段配提示词」，
 * 不用在外面反复用裁剪节点试时间。
 *
 * ## 界面
 *
 *   ┌ 工具栏 ─────────────────────────────────────────┐
 *   │ [选择音频] [重新载入]  voice.wav · 0:32.08  [▶试听] │
 *   ├ 波形（canvas，点击选中、双击切分、拖边界） ────────┤
 *   ├ 工具行 ─────────────────────────────────────────┤
 *   │ [加切点] [自动等分 5.0 秒] [删除段]  模式: 单段/顺序 │
 *   ├ 段清单（每行：时间 + 提示词输入框 + ×） ───────────┤
 *   └─────────────────────────────────────────────────┘
 *
 * ## 三条硬约束（都是踩过的坑）
 *
 * 1. **DOM 事件一律 stopPropagation**。litegraph 在 document 上挂了 pointermove，
 *    不拦的话在面板里拖切点会连带拖动整个节点。
 *
 * 2. **写回 widget 只在「操作结束」时做**（pointerup / 输入框 blur）。
 *    拖动过程中每帧写一次会让 ComfyUI 不停标脏，画布狂闪。
 *
 * 3. **画布尺寸跟着容器走**（ResizeObserver），不要用固定宽高 ——
 *    节点宽度是用户可调的，画布不跟着变就会留白或拉伸变形。
 *
 * ## 数据
 *
 * 分段表以 JSON 存在节点的 `分段数据` widget 里（这个 widget 被改成隐藏类型），
 * 工作流保存时会一起带走，所以每段的提示词不会丢。
 * 音频路径存在 `文件路径` widget 里 —— 它保持可见，用户能一眼看到用的哪个文件。
 */

const comfyAPI = window.comfyAPI || {};
const app = comfyAPI.app?.app;
const api = comfyAPI.api?.api;

const NODE_NAME = "DogkingAudioSplit";
/** 面板高度。给波形 150 + 工具栏 + 段清单滚动区留够空间 */
const PANEL_HEIGHT = 430;
/** 顺序模式执行完，后端发这个事件让前端自动排下一段 */
const EVENT_NEXT = "dogking-tools.audio.next";
/** 最小段长（秒）。比这更短的段切出来是噪音，拖到头就当合并 */
const MIN_GAP = 0.06;
/** 边界判定的像素容差：离切点这么近就认为是「想拖这条线」 */
const HIT_PX = 7;
/** 波形高度 */
const WAVE_H = 150;
/** 时间刻度上方的留白 */
const WAVE_PAD = 4;

/* ==================================================================== 样式 */

function injectStyles() {
  if (document.getElementById("dk-as-style")) return;
  const style = document.createElement("style");
  style.id = "dk-as-style";
  style.textContent = `
.dk-as {
  box-sizing: border-box;
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px;
  overflow: hidden;
  border: 1px solid rgba(120, 150, 210, 0.28);
  border-radius: 8px;
  background: #1b1f27;
  color: #cfd8ea;
  font: 12px/1.4 ui-monospace, Consolas, monospace;
  user-select: none;
}
.dk-as-bar, .dk-as-tools {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.dk-as button {
  padding: 3px 9px;
  border: 1px solid rgba(140, 170, 255, 0.35);
  border-radius: 5px;
  background: rgba(90, 120, 200, 0.16);
  color: #cfd8ea;
  font: inherit;
  cursor: pointer;
}
.dk-as button:hover { background: rgba(90, 120, 200, 0.34); }
.dk-as button:active { background: rgba(90, 120, 200, 0.5); }
.dk-as button:disabled { opacity: 0.4; cursor: default; }
.dk-as-name {
  flex: 1 1 auto;
  min-width: 60px;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  color: #8fb4ff;
}
.dk-as-wavewrap {
  flex: 0 0 auto;
  position: relative;
  height: ${WAVE_H}px;
  border-radius: 6px;
  background: #12161d;
  overflow: hidden;
}
.dk-as-wavewrap canvas { display: block; width: 100%; height: 100%; }
.dk-as-sec {
  flex: 0 0 auto;
  width: 56px;
  padding: 2px 4px;
  border: 1px solid rgba(140, 170, 255, 0.35);
  border-radius: 5px;
  background: rgba(0, 0, 0, 0.35);
  color: #cfd8ea;
  font: inherit;
  text-align: right;
}
.dk-as-list {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding-right: 2px;
}
.dk-as-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 2px 4px;
  border-radius: 5px;
  border: 1px solid transparent;
}
.dk-as-row--active {
  border-color: rgba(120, 170, 255, 0.55);
  background: rgba(90, 130, 220, 0.14);
}
.dk-as-idx {
  flex: 0 0 auto;
  width: 20px;
  color: #7f8ea8;
  text-align: right;
}
.dk-as-time {
  flex: 0 0 auto;
  color: #9fb0c8;
  cursor: pointer;
}
.dk-as-time:hover { color: #cfe0ff; text-decoration: underline; }
.dk-as-prompt {
  flex: 1 1 auto;
  min-width: 40px;
  padding: 2px 6px;
  border: 1px solid rgba(140, 170, 255, 0.22);
  border-radius: 5px;
  background: rgba(0, 0, 0, 0.3);
  color: #cfd8ea;
  font: inherit;
}
.dk-as-prompt:focus { outline: none; border-color: rgba(140, 180, 255, 0.7); }
.dk-as-prompt::placeholder { color: #5c6779; }
.dk-as-del {
  flex: 0 0 auto;
  width: 20px;
  padding: 0;
  line-height: 18px;
  text-align: center;
  color: #8a94a6;
  border: none;
  background: transparent;
}
.dk-as-del:hover { color: #ff8f8f; background: transparent; }
.dk-as-empty {
  padding: 10px;
  color: #6c778a;
  text-align: center;
}
`;
  document.head.appendChild(style);
}

/* ==================================================================== 工具 */

/** 秒 → `mm:ss.xx` */
function fmtTime(sec) {
  const s = Math.max(0, Number(sec) || 0);
  const m = Math.floor(s / 60);
  const rest = s - m * 60;
  return `${String(m).padStart(2, "0")}:${rest.toFixed(2).padStart(5, "0")}`;
}

/** 秒 → `m:ss`（刻度用，省地方） */
function fmtTick(sec) {
  const s = Math.round(Math.max(0, Number(sec) || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** 读分段表：坏数据一律当空，绝不让面板崩掉 */
function readSegments(raw) {
  let data = raw;
  if (typeof raw === "string") {
    try {
      data = JSON.parse(raw || "{}");
    } catch (err) {
      data = {};
    }
  }
  const list = Array.isArray(data) ? data : data?.segments;
  if (!Array.isArray(list)) return [];
  const out = [];
  for (const item of list) {
    if (!item || typeof item !== "object") continue;
    const start = Number(item.start);
    const end = Number(item.end);
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) continue;
    out.push({ start: Math.max(0, start), end, prompt: String(item.prompt || "") });
  }
  out.sort((a, b) => a.start - b.start);
  return out;
}

/* ==================================================================== 面板 */

class AudioSplitPanel {
  /**
   * @param {object} node litegraph 节点
   * @param {HTMLElement} mount 挂载点（DOM widget 的容器）
   */
  constructor(node, mount) {
    this.node = node;
    this.mount = mount;
    this.segments = [];
    this.duration = 0;
    this.peaks = [];
    this.active = 0;
    this.drag = null;       // {edge: bool, index: number, left: number}
    this.audioEl = new Audio();
    this.playing = -1;      // 正在试听第几段（-1 = 没在放）

    this.build();
    this.bindAudio();
    this.restore();
  }

  /* ---------------- 构建 DOM ---------------- */

  build() {
    const el = (tag, cls, text) => {
      const n = document.createElement(tag);
      if (cls) n.className = cls;
      if (text !== undefined) n.textContent = text;
      return n;
    };
    this.el = el("div", "dk-as");

    /* 工具栏 */
    const bar = el("div", "dk-as-bar");
    this.btnPick = el("button", "", "选择音频");
    this.btnReload = el("button", "", "重新载入");
    this.nameEl = el("span", "dk-as-name", "还没选音频");
    this.btnPlay = el("button", "", "▶ 试听本段");
    bar.append(this.btnPick, this.btnReload, this.nameEl, this.btnPlay);

    /* 波形 */
    const wrap = el("div", "dk-as-wavewrap");
    this.canvas = document.createElement("canvas");
    wrap.appendChild(this.canvas);

    /* 工具行 */
    const tools = el("div", "dk-as-tools");
    this.btnCut = el("button", "", "加切点");
    this.btnEven = el("button", "", "自动等分");
    this.secInput = el("input", "dk-as-sec");
    this.secInput.type = "number";
    this.secInput.min = "0.5";
    this.secInput.step = "0.5";
    this.secInput.value = "5";
    this.secInput.title = "每段目标秒数";
    this.btnClear = el("button", "", "清空分段");
    tools.append(this.btnCut, this.btnEven, this.secInput, el("span", "", "秒/段"), this.btnClear);

    /* 段清单 */
    this.listEl = el("div", "dk-as-list");

    this.el.append(bar, wrap, tools, this.listEl);
    this.mount.appendChild(this.el);

    /* 事件（全部隔离，别让画布跟着动） */
    const stop = (e) => {
      e.stopPropagation();
    };
    this.el.addEventListener("pointerdown", stop);
    this.el.addEventListener("wheel", (e) => e.stopPropagation(), { passive: true });
    this.el.addEventListener("dblclick", (e) => e.stopPropagation());
    this.el.addEventListener("contextmenu", (e) => e.stopPropagation());

    this.btnPick.addEventListener("click", () => this.pickAudio());
    this.btnReload.addEventListener("click", () => this.loadPeaks(true));
    this.btnPlay.addEventListener("click", () => this.togglePlay());
    this.btnCut.addEventListener("click", () => this.cutAtPlayhead());
    this.btnEven.addEventListener("click", () => this.splitEvenly());
    this.btnClear.addEventListener("click", () => this.clearSegments());

    this.bindCanvas();

    /* 画布尺寸跟着容器走 */
    this.ro = new ResizeObserver(() => this.resize());
    this.ro.observe(wrap);
  }

  resize() {
    const wrap = this.canvas.parentElement;
    if (!wrap) return;
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(80, wrap.clientWidth);
    const h = Math.max(60, wrap.clientHeight);
    this.canvas.width = Math.round(w * dpr);
    this.canvas.height = Math.round(h * dpr);
    this.ctx = this.canvas.getContext("2d");
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.cssW = w;
    this.cssH = h;
    this.draw();
  }

  /* ---------------- 波形交互 ---------------- */

  bindCanvas() {
    const cv = this.canvas;

    const timeAt = (e) => {
      const rect = cv.getBoundingClientRect();
      const x = Math.min(Math.max(0, e.clientX - rect.left), rect.width);
      return (x / Math.max(1, rect.width)) * (this.duration || 0);
    };

    /** 找离点击位置最近的切点（段边界）。返回 {index, side} */
    const hitEdge = (e) => {
      const rect = cv.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const t = (x / Math.max(1, rect.width)) * (this.duration || 0);
      let best = null;
      for (let i = 0; i < this.segments.length; i += 1) {
        const seg = this.segments[i];
        // 每段的左边界；最后一段额外检查右边界
        const marks = [{ t: seg.start, index: i, side: "start" }];
        if (i === this.segments.length - 1) marks.push({ t: seg.end, index: i, side: "end" });
        for (const m of marks) {
          const px = Math.abs((m.t / Math.max(0.0001, this.duration)) * rect.width - x);
          if (px <= HIT_PX && (!best || px < best.px)) best = { ...m, px };
        }
      }
      return best;
    };

    cv.addEventListener("pointerdown", (e) => {
      e.stopPropagation();
      if (!this.segments.length) return;

      const edge = hitEdge(e);
      if (edge && e.button === 0) {
        // 拖切点：只改这个边界，两侧段的另一头不动
        this.drag = { index: edge.index, side: edge.side, lastX: e.clientX };
        cv.setPointerCapture?.(e.pointerId);
        e.preventDefault();
        return;
      }

      // 点段内 → 选中（单段模式下顺便把「当前段」同步过去）
      const t = timeAt(e);
      const idx = this.segments.findIndex((s) => t >= s.start && t <= s.end);
      if (idx >= 0) {
        this.setActive(idx, true);
      }
    });

    cv.addEventListener("pointermove", (e) => {
      if (!this.drag) {
        // 光标提示：靠近切点显示横向拖动
        const edge = hitEdge(e);
        cv.style.cursor = edge ? "ew-resize" : "pointer";
        return;
      }
      e.stopPropagation();
      const rect = cv.getBoundingClientRect();
      const dt = ((e.clientX - this.drag.lastX) / Math.max(1, rect.width)) * (this.duration || 0);
      this.drag.lastX = e.clientX;
      this.moveEdge(this.drag.index, this.drag.side, dt);
    });

    const endDrag = (e) => {
      if (!this.drag) return;
      this.drag = null;
      cv.releasePointerCapture?.(e.pointerId);
      this.commit();          // 拖动结束才写回 widget
      this.renderList();
    };
    cv.addEventListener("pointerup", endDrag);
    cv.addEventListener("pointercancel", endDrag);

    // 双击段内 → 在那里切一刀
    cv.addEventListener("dblclick", (e) => {
      e.stopPropagation();
      if (!this.segments.length) return;
      const t = timeAt(e);
      this.splitAt(t);
    });
  }

  /** 拖某条边界 dt 秒 */
  moveEdge(index, side, dt) {
    const seg = this.segments[index];
    if (!seg) return;

    if (side === "end") {
      seg.end = Math.max(seg.start + MIN_GAP, Math.min(this.duration, seg.end + dt));
    } else {
      const lower = index > 0 ? this.segments[index - 1].start + MIN_GAP : 0;
      seg.start = Math.min(seg.end - MIN_GAP, Math.max(lower, seg.start + dt));
    }
    // 拖动过程中**只重画画布**：段清单里有输入框，每帧重建 DOM 会打断输入、
    // 段数多时也卡。清单等 pointerup 时再重建（见 bindCanvas 的 endDrag）。
    this.draw();
  }

  /* ---------------- 分段操作 ---------------- */

  splitAt(time) {
    const t = Number(time);
    if (!Number.isFinite(t)) return;
    const idx = this.segments.findIndex((s) => t > s.start + MIN_GAP && t < s.end - MIN_GAP);
    if (idx < 0) return;
    const seg = this.segments[idx];
    const right = { start: t, end: seg.end, prompt: "" };
    seg.end = t;
    this.segments.splice(idx + 1, 0, right);
    this.setActive(idx, false);
    this.commit();
    this.renderList();
    this.draw();
  }

  /** 「加切点」：在没有分段时从整段音频切一刀；有分段时在选中段中点切 */
  cutAtPlayhead() {
    if (!this.duration) {
      this.flash("先选音频");
      return;
    }
    if (!this.segments.length) {
      this.segments = [{ start: 0, end: this.duration, prompt: "" }];
    }
    const seg = this.segments[this.active] || this.segments[0];
    this.splitAt((seg.start + seg.end) / 2);
  }

  splitEvenly() {
    const step = Math.max(0.5, Number(this.secInput.value) || 5);
    if (!this.duration) {
      this.flash("先选音频");
      return;
    }
    const count = Math.max(1, Math.ceil(this.duration / step - 1e-6));
    const segments = [];
    for (let i = 0; i < count; i += 1) {
      const start = i * step;
      const end = Math.min(this.duration, (i + 1) * step);
      if (end - start < MIN_GAP) continue;
      segments.push({ start, end, prompt: "" });
    }
    this.segments = segments;
    this.active = 0;
    this.commit();
    this.renderList();
    this.draw();
  }

  clearSegments() {
    this.segments = [];
    this.active = 0;
    this.commit();
    this.renderList();
    this.draw();
  }

  /** 删掉某段：把它并进前一段（第一段则并进后一段），比"留个洞"更好用 */
  removeSegment(index) {
    if (this.segments.length <= 1) {
      this.clearSegments();
      return;
    }
    const seg = this.segments[index];
    if (index > 0) {
      this.segments[index - 1].end = seg.end;
    } else {
      this.segments[1].start = seg.start;
    }
    this.segments.splice(index, 1);
    this.active = Math.min(this.active, this.segments.length - 1);
    this.commit();
    this.renderList();
    this.draw();
  }

  setActive(index, syncWidget) {
    this.active = Math.max(0, Math.min(index, this.segments.length - 1));
    if (syncWidget) this.writeWidget("当前段", this.active + 1);
    this.renderList();
    this.draw();
  }

  /* ---------------- 音频 ---------------- */

  async pickAudio() {
    try {
      const res = await api.fetchApi("/dogking-tools/select_audio", { method: "POST" });
      const data = await res.json();
      if (data?.error) {
        this.flash(data.error);
        return;
      }
      if (!data?.path) return;
      this.writeWidget("文件路径", data.path);
      await this.loadPeaks(true);
    } catch (err) {
      this.flash("选择音频失败：" + err);
    }
  }

  async loadPeaks(resetSegments) {
    const path = this.readWidget("文件路径");
    if (!path) {
      this.nameEl.textContent = "还没选音频";
      this.duration = 0;
      this.peaks = [];
      this.draw();
      return;
    }

    this.nameEl.textContent = "解码中…";
    try {
      const res = await api.fetchApi("/dogking-tools/audio_peaks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path, buckets: 1600 }),
      });
      const data = await res.json();
      if (!data?.ok) {
        this.nameEl.textContent = "读不出来";
        this.flash(data?.error || "音频解码失败");
        return;
      }
      this.duration = Number(data.duration) || 0;
      this.peaks = Array.isArray(data.peaks) ? data.peaks : [];
      this.nameEl.textContent = `${data.name} · ${fmtTime(this.duration)}`;

      if (resetSegments || !this.segments.length) {
        // 换音频：默认整段一刀，用户再点「自动等分」或自己切
        this.segments = this.duration > 0
          ? [{ start: 0, end: this.duration, prompt: this.segments[0]?.prompt || "" }]
          : [];
        this.active = 0;
        this.commit();
      } else {
        // 同一音频重新载入：只把超出新时长的段裁掉
        this.segments = this.segments
          .map((s) => ({ ...s, end: Math.min(s.end, this.duration) }))
          .filter((s) => s.end - s.start > MIN_GAP);
        this.commit();
      }
      this.renderList();
      this.draw();
    } catch (err) {
      this.nameEl.textContent = "请求失败";
      this.flash("音频解码请求失败：" + err);
    }
  }

  audioUrl() {
    const path = this.readWidget("文件路径");
    if (!path) return "";
    const base = `/dogking-tools/audio_file?path=${encodeURIComponent(path)}`;
    return typeof api?.apiURL === "function" ? api.apiURL(base) : base;
  }

  bindAudio() {
    // 试听到段尾自动停 —— 用户靠它确认切点切得对不对
    this.audioEl.addEventListener("timeupdate", () => {
      if (this.playing < 0) return;
      const seg = this.segments[this.playing];
      if (!seg) return;
      if (this.audioEl.currentTime >= seg.end) this.stopPlay();
    });
    this.audioEl.addEventListener("ended", () => this.stopPlay());
  }

  togglePlay() {
    if (this.playing >= 0) {
      this.stopPlay();
      return;
    }
    const seg = this.segments[this.active];
    if (!seg || !this.duration) {
      this.flash("还没有可试听的段");
      return;
    }
    const url = this.audioUrl();
    if (!url) return;
    if (this.audioEl.src !== new URL(url, window.location.href).href) {
      this.audioEl.src = url;
    }
    this.playing = this.active;
    this.audioEl.currentTime = seg.start;
    this.audioEl.play().catch((err) => {
      this.stopPlay();
      this.flash("试听失败：" + err);
    });
    this.btnPlay.textContent = "■ 停止";
  }

  stopPlay() {
    this.playing = -1;
    try {
      this.audioEl.pause();
    } catch (err) {
      /* 忽略 */
    }
    this.btnPlay.textContent = "▶ 试听本段";
  }

  /* ---------------- 绘制 ---------------- */

  draw() {
    const ctx = this.ctx;
    const w = this.cssW || 0;
    const h = this.cssH || 0;
    if (!ctx || !w || !h) return;

    ctx.clearRect(0, 0, w, h);

    // 背景
    ctx.fillStyle = "#12161d";
    ctx.fillRect(0, 0, w, h);

    if (!this.duration) {
      ctx.fillStyle = "#5c6779";
      ctx.font = "12px ui-monospace, Consolas, monospace";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("选一个音频，波形会画在这里", w / 2, h / 2);
      return;
    }

    const plotTop = WAVE_PAD + 12;      // 顶部留给时间刻度
    const plotH = h - plotTop - 6;
    const midY = plotTop + plotH / 2;
    const toX = (t) => (t / this.duration) * w;

    // 时间刻度
    const tickStep = this.duration > 120 ? 30 : this.duration > 40 ? 10 : 5;
    ctx.strokeStyle = "rgba(120, 150, 210, 0.14)";
    ctx.fillStyle = "#5f6a7d";
    ctx.font = "10px ui-monospace, Consolas, monospace";
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    ctx.lineWidth = 1;
    for (let t = 0; t <= this.duration + 0.001; t += tickStep) {
      const x = toX(t);
      ctx.beginPath();
      ctx.moveTo(x, plotTop);
      ctx.lineTo(x, h);
      ctx.stroke();
      ctx.fillText(fmtTick(t), Math.min(x + 3, w - 26), 2);
    }

    // 波形（按峰值画竖线）
    const n = this.peaks.length;
    if (n) {
      const barW = Math.max(1, w / n);
      ctx.fillStyle = "#3d6fbf";
      for (let i = 0; i < n; i += 1) {
        const amp = Math.max(0.5, (this.peaks[i] || 0) * plotH * 0.46);
        const x = (i / n) * w;
        ctx.fillRect(x, midY - amp, barW, amp * 2);
      }
    }

    // 段底色 + 选中高亮
    this.segments.forEach((seg, i) => {
      const x1 = toX(seg.start);
      const x2 = toX(seg.end);
      if (i === this.active) {
        ctx.fillStyle = "rgba(110, 160, 255, 0.20)";
        ctx.fillRect(x1, plotTop, x2 - x1, plotH);
      }
      // 段边界
      ctx.strokeStyle = "rgba(255, 255, 255, 0.55)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x1 + 0.5, plotTop);
      ctx.lineTo(x1 + 0.5, plotTop + plotH);
      ctx.stroke();

      // 段号
      if (x2 - x1 > 16) {
        ctx.fillStyle = "#e6edf9";
        ctx.font = "bold 11px ui-monospace, Consolas, monospace";
        ctx.textAlign = "left";
        ctx.textBaseline = "top";
        ctx.fillText(String(i + 1), x1 + 4, plotTop + 3);
      }
    });

    // 最后一段的右边界
    const last = this.segments[this.segments.length - 1];
    if (last) {
      ctx.strokeStyle = "rgba(255, 255, 255, 0.55)";
      ctx.beginPath();
      ctx.moveTo(toX(last.end) - 0.5, plotTop);
      ctx.lineTo(toX(last.end) - 0.5, plotTop + plotH);
      ctx.stroke();
    }
  }

  /* ---------------- 段清单 ---------------- */

  renderList() {
    this.listEl.textContent = "";
    if (!this.segments.length) {
      const hint = document.createElement("div");
      hint.className = "dk-as-empty";
      hint.textContent = this.duration
        ? "还没有分段：点「自动等分」，或双击波形自己切"
        : "选一个音频开始";
      this.listEl.appendChild(hint);
      return;
    }

    this.segments.forEach((seg, i) => {
      const row = document.createElement("div");
      row.className = "dk-as-row" + (i === this.active ? " dk-as-row--active" : "");

      const idx = document.createElement("span");
      idx.className = "dk-as-idx";
      idx.textContent = String(i + 1);

      const time = document.createElement("span");
      time.className = "dk-as-time";
      time.textContent = `${fmtTime(seg.start)}–${fmtTime(seg.end)}`;
      time.title = "点击试听这一段";
      time.addEventListener("click", () => {
        this.setActive(i, true);
        this.stopPlay();
        this.togglePlay();
      });

      const input = document.createElement("input");
      input.className = "dk-as-prompt";
      input.type = "text";
      input.value = seg.prompt || "";
      input.placeholder = "沿用全局提示词";
      input.addEventListener("pointerdown", (e) => e.stopPropagation());
      input.addEventListener("change", () => {
        seg.prompt = input.value.trim();
        this.commit();
      });

      const del = document.createElement("button");
      del.className = "dk-as-del";
      del.textContent = "×";
      del.title = "删除这一段（并入相邻段）";
      del.addEventListener("click", () => this.removeSegment(i));

      row.append(idx, time, input, del);
      this.listEl.appendChild(row);
    });
  }

  /* ---------------- 读写 widget ---------------- */

  widget(name) {
    return (this.node.widgets || []).find((w) => w.name === name);
  }

  readWidget(name) {
    const w = this.widget(name);
    return w ? String(w.value ?? "") : "";
  }

  /**
   * 写 widget 并触发回调 + 标脏。
   *
   * 不调 node.setSize：面板是固定高度，尺寸变了反而会让下面的 widget 抖。
   */
  writeWidget(name, value) {
    const w = this.widget(name);
    if (!w) return;
    if (w.value === value) return;
    w.value = value;
    if (typeof w.callback === "function") {
      try {
        w.callback(value);
      } catch (err) {
        /* 回调挂了不影响数据 */
      }
    }
    this.node.setDirtyCanvas?.(true, true);
  }

  commit() {
    const data = {
      segments: this.segments.map((s) => ({
        start: Number(s.start.toFixed(3)),
        end: Number(s.end.toFixed(3)),
        prompt: String(s.prompt || ""),
      })),
    };
    this.writeWidget("分段数据", JSON.stringify(data));
  }

  restore() {
    this.segments = readSegments(this.readWidget("分段数据"));
    this.active = Math.max(0, Math.min(this.active, this.segments.length - 1));
    this.renderList();
    this.resize();
    if (this.readWidget("文件路径")) this.loadPeaks(false);
  }

  flash(text) {
    this.nameEl.textContent = String(text || "");
  }
}

/* ==================================================================== 注册 */

if (app && api) {
  app.registerExtension({
    name: "dogking.tools.audioSplit",

    async beforeRegisterNodeDef(nodeType, nodeData) {
      if (nodeData.name !== NODE_NAME) return;

      injectStyles();

      const onNodeCreated = nodeType.prototype.onNodeCreated;
      nodeType.prototype.onNodeCreated = function () {
        const ret = onNodeCreated?.apply(this, arguments);
        const node = this;

        // 「分段数据」是内部字段：值照常提交，但节点上不显示一行大文本框
        const dataWidget = (node.widgets || []).find((w) => w.name === "分段数据");
        if (dataWidget) {
          dataWidget.type = "dogkingHidden";
          dataWidget.computeSize = () => [0, -4];
          dataWidget.serializeValue = () => dataWidget.value || "";
        }

        const mount = document.createElement("div");
        node.addDOMWidget("音频分段", "dkAudioSplit", mount, {
          serialize: false,
          hideOnZoom: false,
          getHeight: () => PANEL_HEIGHT,
          getValue: () => "",
          setValue: () => {},
        });

        node.dkAudio = new AudioSplitPanel(node, mount);
        return ret;
      };

      // 打开已有工作流：DOM widget 里的内容不会自动回来，重新读一遍 widget
      const onConfigure = nodeType.prototype.onConfigure;
      nodeType.prototype.onConfigure = function () {
        const r = onConfigure?.apply(this, arguments);
        const node = this;
        setTimeout(() => {
          if (node.dkAudio) node.dkAudio.restore();
        }, 0);
        return r;
      };

      // 顺序模式：后端说「还有下一段」就自动排队
      const onExecuted = nodeType.prototype.onExecuted;
      nodeType.prototype.onExecuted = function (message) {
        onExecuted?.apply(this, arguments);
        const badge = message?.dogking_badge?.[0];
        const Badge = window.LGraphBadge;
        if (badge && typeof Badge === "function") {
          try {
            this.badges = [new Badge({
              text: badge,
              bgColor: "#24406e",
              fgColor: "#cfe0ff",
              fontSize: 11,
              height: 18,
              padding: 7,
              cornerRadius: 9,
              xOffset: -Math.round(badge.length * 11 * 0.35),
            })];
          } catch (err) {
            this.badges = [];
          }
        }
        // 面板跟着更新「当前段」高亮
        const idx = message?.dogking_index?.[0];
        if (node_has_panel(this) && Number.isFinite(idx)) {
          this.dkAudio.setActive(Number(idx) - 1, false);
        }
        this.setDirtyCanvas?.(true, true);
      };
    },
  });

  function node_has_panel(node) {
    return Boolean(node?.dkAudio);
  }

  /**
   * 自动连续：后端每跑完一段发一次这个事件。
   *
   * 用独立事件名（`dogking-tools.audio.next`），不碰「提示词批量加载」的
   * `dogking-tools.next` —— 两个节点同时在工作流里时不会互相排队。
   */
  let pendingContinue = false;
  api.addEventListener(EVENT_NEXT, (e) => {
    pendingContinue = Boolean(e?.detail?.should_continue);
  });
  api.addEventListener("execution_success", () => {
    if (!pendingContinue) return;
    pendingContinue = false;
    setTimeout(() => {
      try {
        app.queuePrompt(0, 1);
      } catch (err) {
        console.warn("[dogking-tools] 音频分段自动排队失败", err);
      }
    }, 80);
  });
  ["execution_interrupted", "execution_error"].forEach((ev) => {
    api.addEventListener(ev, () => {
      pendingContinue = false;
    });
  });
}
