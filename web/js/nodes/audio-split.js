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
const HIT_PX = 9;
/** 波形高度 */
const WAVE_H = 150;

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
/* 试听中的那一段：绿色，编号位置显示 ♪ —— 和波形上的绿块是同一个色 */
.dk-as-row--playing {
  border-color: rgba(63, 211, 154, 0.7);
  background: rgba(35, 160, 115, 0.16);
}
.dk-as-row--playing .dk-as-idx { color: #3fd39a; font-weight: 700; }
.dk-as-row--playing .dk-as-time { color: #a9f3d4; }
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
    this.drag = null;       // {b: 边界序号} —— 拖的是"第几条切点线"，0 = 最左，n = 最右
    this.hoverB = -1;       // 鼠标悬停/靠近的边界序号，-1 = 没有
    this.cursor = -1;       // 波形上点的位置（秒），「加切点」就在这儿切；-1 = 没点过
    this.rows = [];         // 段清单的行引用（试听高亮时只改 class，不重建 DOM）
    this.playRaf = 0;
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
    this.btnCut.title = "在波形上点一下定位（虚线），再点这里就在虚线处切；没定位就切选中段的正中";
    this.btnEven = el("button", "", "自动等分");
    this.btnEven.title = "按右边填的秒数从 0 开始等分，之后可以拖切点微调";
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

  /**
   * 所有切点（共 `段数 + 1` 条）：第 0 条是最左端，最后一条是最右端。
   *
   * 中间任意一条都是**相邻两段共用的那条线** —— 拖动它，左边段的 end 和右边段的
   * start 一起动。之前只改一侧，于是拖一下就撕出一个空洞（或压成 0.06 秒的碎段），
   * 这就是「线条不能自由拉动」的真正原因。
   */
  boundaries() {
    const segs = this.segments;
    if (!segs.length) return [];
    const out = [segs[0].start];
    for (let i = 1; i < segs.length; i += 1) out.push(segs[i].start);
    out.push(segs[segs.length - 1].end);
    return out;
  }

  /** 第 b 条边界现在在几秒 */
  boundaryTime(b) {
    return this.boundaries()[b];
  }

  bindCanvas() {
    const cv = this.canvas;

    const xOf = (e) => {
      const rect = cv.getBoundingClientRect();
      return Math.min(Math.max(0, e.clientX - rect.left), rect.width);
    };
    const timeAt = (e) => {
      const rect = cv.getBoundingClientRect();
      return (xOf(e) / Math.max(1, rect.width)) * (this.duration || 0);
    };

    /** 找离鼠标最近的切点线，命中半径 HIT_PX 像素 */
    const hitEdge = (e) => {
      const rect = cv.getBoundingClientRect();
      const x = xOf(e);
      const dur = this.duration || 0;
      let best = -1;
      let bestPx = HIT_PX + 0.001;
      this.boundaries().forEach((t, b) => {
        const px = Math.abs((t / Math.max(0.0001, dur)) * rect.width - x);
        if (px <= bestPx) {
          bestPx = px;
          best = b;
        }
      });
      return best;
    };

    cv.addEventListener("pointerdown", (e) => {
      e.stopPropagation();
      if (!this.segments.length || e.button !== 0) return;

      const b = hitEdge(e);
      if (b >= 0) {
        // 抓切点：按住就跟着鼠标走，不再算增量（增量累计容易丢帧、还会跑偏）
        this.drag = { b };
        this.cursor = timeAt(e);
        cv.setPointerCapture?.(e.pointerId);
        cv.style.cursor = "ew-resize";
        e.preventDefault();
        this.draw();
        return;
      }

      // 点段内 → 定位光标 + 选中（单段模式下顺便把「当前段」同步过去）
      const t = timeAt(e);
      const idx = this.segments.findIndex((s) => t >= s.start && t <= s.end);
      this.cursor = t;
      if (idx >= 0) this.setActive(idx, true);
      else this.draw();
    });

    cv.addEventListener("pointermove", (e) => {
      const b = hitEdge(e);
      if (!this.drag) {
        if (b !== this.hoverB) {
          this.hoverB = b;
          this.draw();
        }
        cv.style.cursor = b >= 0 ? "ew-resize" : "pointer";
        return;
      }
      e.stopPropagation();
      this.moveBoundary(this.drag.b, timeAt(e));
    });

    const endDrag = (e) => {
      if (!this.drag) return;
      this.drag = null;
      try {
        cv.releasePointerCapture?.(e.pointerId);
      } catch (err) {
        /* 已经释放过就算了 */
      }
      // 拖动结束才写回 widget：拖动中每帧写会不停触发工作流变脏
      this.commit();
      this.renderList();
      this.draw();
    };
    cv.addEventListener("pointerup", endDrag);
    cv.addEventListener("pointercancel", endDrag);
    cv.addEventListener("pointerleave", () => {
      if (this.drag) return;          // 拖出去了也不能清掉悬停态
      if (this.hoverB !== -1) {
        this.hoverB = -1;
        this.draw();
      }
    });

    // 双击段内 → 在那里切一刀
    cv.addEventListener("dblclick", (e) => {
      e.stopPropagation();
      if (!this.segments.length) return;
      this.splitAt(timeAt(e));
    });
  }

  /**
   * 把第 b 条切点线挪到 `time`。
   *
   * 中间那条线同时改两侧（左段 end / 右段 start），所以永远共边、不产生空洞；
   * 两端的线只夹住自己那一侧。相邻段的另一头保持不动，因此单段最短可能被压到
   * MIN_GAP —— 但不会像以前那样把整段压成 0.06 秒还不自知。
   */
  moveBoundary(b, time) {
    const segs = this.segments;
    const n = segs.length;
    if (!n) return;

    const dur = this.duration || segs[n - 1].end;
    const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), Math.max(lo, hi));
    const t = clamp(Number(time) || 0, 0, dur);

    if (b <= 0) {
      const first = segs[0];
      first.start = clamp(t, 0, first.end - MIN_GAP);
    } else if (b >= n) {
      const last = segs[n - 1];
      last.end = clamp(t, last.start + MIN_GAP, dur);
    } else {
      const left = segs[b - 1];
      const right = segs[b];
      const lo = left.start + MIN_GAP;
      const hi = right.end - MIN_GAP;
      const v = clamp(t, lo, hi);
      left.end = v;
      right.start = v;                 // ← 关键：另一侧跟着走
    }

    this.cursor = t;
    // 拖动过程中**只重画画布**：段清单里有输入框，每帧重建 DOM 会打断输入、
    // 段数多时也卡。清单等 pointerup 时再重建（见 bindCanvas 的 endDrag）。
    this.draw();
  }

  /* ---------------- 分段操作 ---------------- */

  /**
   * 把分段表修成「首尾相接、没有空洞、没有碎段」。
   *
   * 老版本拖边界只改一侧，会存下带空洞的表（0.06 秒的碎段就是这么来的）。
   * 加载和写回时都过一遍：有空洞就用前一段的 end 补上，短于 MIN_GAP 的并进前一段，
   * 提示词保留。这个节点没有「跳过某段音频」的用法，空洞一律视为脏数据。
   */
  normalizeSegments() {
    if (!this.segments.length) return;
    const dur = this.duration || this.segments[this.segments.length - 1].end;
    const out = [];
    for (const raw of this.segments) {
      const start = Math.max(0, Math.min(Number(raw.start) || 0, dur));
      const end = Math.max(start, Math.min(Number(raw.end) || 0, dur));
      const prompt = String(raw.prompt || "");
      if (!out.length) {
        if (end - start >= MIN_GAP) out.push({ start, end, prompt });
        continue;
      }
      const prev = out[out.length - 1];
      const st = prev.end;                        // 强制共边
      // 留 1e-6 的余量：12.06 - 12 在浮点里是 0.0600000000000005，直接和 0.06 比会漏掉
      if (end - st <= MIN_GAP + 1e-6) {
        prev.end = Math.max(prev.end, end);       // 碎段（含正好 0.06 秒的）并进前一段
        if (!prev.prompt) prev.prompt = prompt;
      } else {
        out.push({ start: st, end, prompt });
      }
    }
    if (out.length) this.segments = out;
  }

  splitAt(time) {
    const t = Number(time);
    if (!Number.isFinite(t)) return;
    const idx = this.segments.findIndex((s) => t > s.start + MIN_GAP && t < s.end - MIN_GAP);
    if (idx < 0) {
      this.flash("这个位置切不开：太靠近已有切点（至少留 0.06 秒）");
      return;
    }
    const seg = this.segments[idx];
    const right = { start: t, end: seg.end, prompt: "" };
    seg.end = t;
    this.segments.splice(idx + 1, 0, right);
    this.cursor = t;
    this.setActive(idx, false);
    this.commit();
    this.renderList();
    this.draw();
  }

  /**
   * 「加切点」：在**光标处**切（先在波形上点一下定位）；没点过光标就切选中段中点。
   *
   * 以前固定切中点，在 4 分半的整段音频上连点几次会切出「0–18 秒 + 一堆 0.06 秒碎段」，
   * 用户根本没法预期切在哪。
   */
  cutAtPlayhead() {
    if (!this.duration) {
      this.flash("先选音频");
      return;
    }
    if (!this.segments.length) {
      this.segments = [{ start: 0, end: this.duration, prompt: "" }];
      this.commit();
      this.renderList();
    }
    const seg = this.segments[this.active] || this.segments[0];
    const inside = this.cursor >= seg.start + MIN_GAP && this.cursor <= seg.end - MIN_GAP;
    const t = inside ? this.cursor : (seg.start + seg.end) / 2;
    this.splitAt(t);
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
    this.updatePlayButton();
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
    // 超时兜底：后端解码要是卡住（以前 torchaudio 弹 DLL 框就是这种），
    // 不加这道兜底，界面就永远停在「解码中…」，用户只能重启。
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 25000);
    try {
      const res = await api.fetchApi("/dogking-tools/audio_peaks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path, buckets: 1600 }),
        signal: ctrl.signal,
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
      const aborted = err?.name === "AbortError";
      this.nameEl.textContent = aborted ? "解码超时" : "请求失败";
      this.flash(
        aborted
          ? "后端解码超时：音频可能太大或格式不支持。换 wav/mp3 再试，或看控制台报错"
          : "音频解码请求失败：" + err
      );
    } finally {
      clearTimeout(timer);
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

  /** 试听按钮的文案：直接写"第几段"，省得用户猜在放哪段 */
  updatePlayButton() {
    if (!this.btnPlay) return;
    if (this.playing >= 0) {
      const seg = this.segments[this.playing];
      const range = seg ? ` · ${fmtTime(seg.start)}` : "";
      this.btnPlay.textContent = `■ 停止试听（第 ${this.playing + 1} 段${range}）`;
      this.btnPlay.title = "停下";
    } else if (this.segments.length) {
      this.btnPlay.textContent = `▶ 试听第 ${this.active + 1} 段`;
      this.btnPlay.title = "从这一段的开头播到段尾，播到段尾自动停";
    } else {
      this.btnPlay.textContent = "▶ 试听本段";
      this.btnPlay.title = "还没有分段";
    }
  }

  /** 播放时每帧重画画布：让播放头和试听中的段实时跟着动 */
  startPlayTicker() {
    this.stopPlayTicker();
    const tick = () => {
      if (this.playing < 0) return;
      this.draw();
      this.playRaf = requestAnimationFrame(tick);
    };
    this.playRaf = requestAnimationFrame(tick);
  }

  stopPlayTicker() {
    if (this.playRaf) {
      cancelAnimationFrame(this.playRaf);
      this.playRaf = 0;
    }
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
    this.updatePlayButton();
    this.paintPlaying();
    this.startPlayTicker();
    this.draw();
  }

  stopPlay() {
    this.playing = -1;
    try {
      this.audioEl.pause();
    } catch (err) {
      /* 忽略 */
    }
    this.stopPlayTicker();
    this.updatePlayButton();
    this.paintPlaying();
    this.draw();
  }

  /* ---------------- 绘制 ---------------- */

  /** 圆角矩形路径（canvas 没内置，几行手写省得引入依赖） */
  roundRect(ctx, x, y, w, h, r) {
    const rr = Math.min(r, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + rr, y);
    ctx.arcTo(x + w, y, x + w, y + h, rr);
    ctx.arcTo(x + w, y + h, x, y + h, rr);
    ctx.arcTo(x, y + h, x, y, rr);
    ctx.arcTo(x, y, x + w, y, rr);
    ctx.closePath();
  }

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

    const TOP = 18;                     // 顶部留给刻度 + 段号
    const BOTTOM = 16;                  // 底部留给段底条
    const plotTop = TOP;
    const plotH = Math.max(24, h - TOP - BOTTOM);
    const midY = plotTop + plotH / 2;
    const toX = (t) => (Math.min(Math.max(0, t), this.duration) / this.duration) * w;

    // ---- 时间刻度
    const tickStep = this.duration > 120 ? 30 : this.duration > 40 ? 10 : 5;
    ctx.strokeStyle = "rgba(120, 150, 210, 0.13)";
    ctx.fillStyle = "#5f6a7d";
    ctx.font = "10px ui-monospace, Consolas, monospace";
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    ctx.lineWidth = 1;
    for (let t = 0; t <= this.duration + 0.001; t += tickStep) {
      const x = toX(t);
      ctx.beginPath();
      ctx.moveTo(x, plotTop);
      ctx.lineTo(x, plotTop + plotH);
      ctx.stroke();
      ctx.fillText(fmtTick(t), Math.min(x + 3, w - 26), 2);
    }

    // ---- 段底色：奇偶交替，让分段边界一眼可见
    const segs = this.segments;
    segs.forEach((seg, i) => {
      const x1 = toX(seg.start);
      const x2 = toX(seg.end);
      const sw = Math.max(1, x2 - x1);
      if (i % 2 === 1) {
        ctx.fillStyle = "rgba(255, 255, 255, 0.045)";
        ctx.fillRect(x1, plotTop, sw, plotH);
      }
    });

    // ---- 波形本身（按段画，段内色调统一）
    const n = this.peaks.length;
    if (n) {
      const barW = Math.max(1, w / n);
      for (let i = 0; i < n; i += 1) {
        const t = (i / n) * this.duration;
        const segIndex = this.segments.findIndex((s) => t >= s.start && t < s.end);
        const playing = segIndex >= 0 && segIndex === this.playing;
        const active = segIndex >= 0 && segIndex === this.active;
        ctx.fillStyle = playing ? "#5fe0ad" : active ? "#5f92e8" : "#3d6fbf";
        const amp = Math.max(0.5, (this.peaks[i] || 0) * plotH * 0.46);
        const x = (i / n) * w;
        ctx.fillRect(x, midY - amp, barW, amp * 2);
      }
    }

    // ---- 选中 / 试听：着色 + 顶部状态条 + 段号 chip
    segs.forEach((seg, i) => {
      const x1 = toX(seg.start);
      const x2 = toX(seg.end);
      const sw = Math.max(1, x2 - x1);
      const isActive = i === this.active;
      const isPlaying = i === this.playing;

      if (isActive && !isPlaying) {
        ctx.fillStyle = "rgba(96, 150, 255, 0.13)";
        ctx.fillRect(x1, plotTop, sw, plotH);
      }
      if (isPlaying) {
        ctx.fillStyle = "rgba(63, 211, 154, 0.16)";
        ctx.fillRect(x1, plotTop, sw, plotH);
      }

      // 顶部 3px 状态条：选中蓝、试听绿
      if (isActive || isPlaying) {
        ctx.fillStyle = isPlaying ? "#3fd39a" : "#5b8dff";
        ctx.fillRect(x1 + 1, plotTop, Math.max(0, sw - 2), 3);
      }

      // 底部段条：每条段一个色块，宽度恒定 4px，分段多的时候也看得清
      ctx.fillStyle = isPlaying
        ? "#3fd39a"
        : isActive
          ? "#5b8dff"
          : i % 2 === 0
            ? "rgba(120, 150, 210, 0.55)"
            : "rgba(120, 150, 210, 0.3)";
      ctx.fillRect(x1 + 1, plotTop + plotH + 3, Math.max(0, sw - 2), 4);

      // 段号 chip（段太窄就只显示选中的那条）。落在波形区顶部而不是刻度那一行，
      // 否则会和 0:05 这类刻度文字叠在一起
      const drawChip = sw > 16 || isActive || isPlaying;
      if (drawChip) {
        const label = String(i + 1);
        ctx.font = "bold 10px ui-monospace, Consolas, monospace";
        const tw = ctx.measureText(label).width;
        const cw = tw + 8;
        const cx = Math.min(x1 + 2, w - cw - 1);
        const chipY = plotTop + 2;
        ctx.fillStyle = isPlaying
          ? "rgba(20, 90, 66, 0.95)"
          : isActive
            ? "rgba(28, 56, 110, 0.95)"
            : "rgba(12, 16, 23, 0.78)";
        this.roundRect(ctx, cx, chipY, cw, 13, 3);
        ctx.fill();
        ctx.fillStyle = isPlaying ? "#c8ffe8" : isActive ? "#dbe8ff" : "#93a3bb";
        ctx.textAlign = "left";
        ctx.textBaseline = "top";
        ctx.fillText(label, cx + 4, chipY + 1);

        // 时长：段宽够就写在右下角
        if (sw > 62) {
          const dur = `${(seg.end - seg.start).toFixed(seg.end - seg.start < 10 ? 2 : 1)}s`;
          ctx.font = "10px ui-monospace, Consolas, monospace";
          ctx.fillStyle = isPlaying ? "rgba(200, 255, 232, 0.9)" : "rgba(190, 205, 228, 0.75)";
          ctx.textAlign = "right";
          ctx.textBaseline = "bottom";
          ctx.fillText(dur, Math.max(x1 + 4, x2 - 9), plotTop + plotH - 2);
        }
      }
    });

    // ---- 切点线 + 抓手（每条都能拖，手感一致）
    this.boundaries().forEach((t, b) => {
      const x = toX(t);
      const hot = this.drag ? this.drag.b === b : this.hoverB === b;
      ctx.strokeStyle = hot ? "#a9c9ff" : "rgba(233, 240, 252, 0.78)";
      ctx.lineWidth = hot ? 2.5 : 1.5;
      ctx.beginPath();
      ctx.moveTo(x, plotTop);
      ctx.lineTo(x, plotTop + plotH);
      ctx.stroke();

      // 中间那颗抓手：告诉用户"这条线能拖"
      ctx.fillStyle = hot ? "#a9c9ff" : "rgba(219, 232, 252, 0.9)";
      this.roundRect(ctx, x - 3, midY - 11, 6, 22, 3);
      ctx.fill();
      ctx.fillStyle = "rgba(20, 26, 36, 0.85)";
      ctx.fillRect(x - 1, midY - 6, 2, 12);
    });

    // ---- 播放头（试听中实线 + 光晕；光标位置虚线）
    if (this.playing >= 0) {
      const x = toX(this.audioEl.currentTime || 0);
      ctx.fillStyle = "rgba(63, 211, 154, 0.22)";
      ctx.fillRect(x - 3, plotTop, 6, plotH);
      ctx.fillStyle = "#3fd39a";
      ctx.fillRect(x - 1, plotTop, 2, plotH);
    } else if (this.cursor >= 0 && this.cursor <= this.duration) {
      const x = toX(this.cursor);
      ctx.save();
      ctx.setLineDash([3, 3]);
      ctx.strokeStyle = "rgba(143, 180, 255, 0.85)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x + 0.5, plotTop);
      ctx.lineTo(x + 0.5, plotTop + plotH);
      ctx.stroke();
      ctx.restore();
    }
  }

  /* ---------------- 段清单 ---------------- */

  renderList() {
    this.listEl.textContent = "";
    this.rows = [];
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
      const isPlaying = i === this.playing;
      const isActive = i === this.active;
      row.className =
        "dk-as-row" +
        (isActive ? " dk-as-row--active" : "") +
        (isPlaying ? " dk-as-row--playing" : "");

      const idx = document.createElement("span");
      idx.className = "dk-as-idx";
      idx.textContent = isPlaying ? "♪" : String(i + 1);
      idx.title = isPlaying ? "正在试听这一段" : "";

      const time = document.createElement("span");
      time.className = "dk-as-time";
      time.textContent = `${fmtTime(seg.start)}–${fmtTime(seg.end)}`;
      time.title = "点击试听这一段";
      time.addEventListener("click", () => {
        this.setActive(i, true);
        if (this.playing === i) {
          this.stopPlay();
          return;
        }
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
      this.rows.push({ row, idx, time });
    });
  }

  /** 只改试听高亮，不重建 DOM —— 重建会把用户正在输入的提示词冲掉 */
  paintPlaying() {
    this.rows.forEach((ref, i) => {
      const isPlaying = i === this.playing;
      ref.row.classList.toggle("dk-as-row--playing", isPlaying);
      ref.idx.textContent = isPlaying ? "♪" : String(i + 1);
      ref.idx.title = isPlaying ? "正在试听这一段" : "";
    });
    const ref = this.rows[this.playing];
    if (ref) ref.row.scrollIntoView({ block: "nearest" });
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
    this.normalizeSegments();
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
    this.normalizeSegments();
    this.active = Math.max(0, Math.min(this.active, this.segments.length - 1));
    this.updatePlayButton();
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
