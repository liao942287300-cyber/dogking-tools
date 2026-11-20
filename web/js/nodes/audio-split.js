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
/** 面板高度。给全局参考图 + 波形 150 + 工具栏 + 段清单滚动区留够空间 */
const PANEL_HEIGHT = 538;   // 多了一行全局提示词输入；参考图已挪到节点顶部空白区
/** 顺序模式执行完，后端发这个事件让前端自动排下一段 */
const EVENT_NEXT = "dogking-tools.audio.next";
/** 最小段长（秒）。比这更短的段切出来是噪音，拖到头就当合并 */
const MIN_GAP = 0.06;
/** 边界判定的像素容差：离切点这么近就认为是「想拖这条线」 */
const HIT_PX = 9;
/** 波形高度 */
const WAVE_H = 150;
/**
 * 参考图槽位数。**必须和后端 audio_split.MAX_REFS 一致** ——
 * 后端用它决定返回几个图片口，前端用它决定画几个槽。
 */
const MAX_REFS = 6;
/** 短边限制默认值，和后端 REF_SHORT_EDGE_DEFAULT 保持一致 */
const REF_SHORT_DEFAULT = 1024;

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
/* 全局提示词：原生多行 widget 藏掉之后，值从这里编辑 */
.dk-as-global {
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.dk-as-global-lbl {
  color: #8fb4ff;
  font-size: 11px;
  font-weight: 600;
}
.dk-as .dk-as-global-input {
  box-sizing: border-box;
  width: 100%;
  min-height: 34px;
  resize: vertical;
  padding: 5px 6px;
  border: 1px solid rgba(120, 150, 210, 0.35);
  border-radius: 5px;
  background: #151a22;
  color: #d6e0f2;
  font: 12px/1.45 ui-monospace, Consolas, monospace;
  outline: none;
}
.dk-as .dk-as-global-input:focus {
  border-color: rgba(120, 170, 255, 0.75);
}
.dk-as-bar, .dk-as-exec {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 8px;
  /* 面板被拖窄时换行，而不是把按钮挤成「选/择/音/频」竖排 */
  flex-wrap: wrap;
  row-gap: 4px;
  padding: 3px 6px;
  border-radius: 6px;
  background: rgba(90, 130, 220, 0.10);
  border: 1px solid rgba(120, 150, 210, 0.22);
}
/* 按钮文字永远单行：这是「竖排」的唯一根因（flex 允许收缩 + 可换行） */
.dk-as button {
  flex: 0 0 auto;
  white-space: nowrap;
}
.dk-as-exec-dot {
  flex: 0 0 auto;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #3fd39a;
  box-shadow: 0 0 6px rgba(63, 211, 154, 0.7);
}
.dk-as-exec-text {
  /* 下限要够放下「顺序 · 第 1 / 8 段 · 15.00s」整句（约 175px）：
     窄到放不下时让后面的控件换行，而不是把状态文字截成「顺序 · 第 1 / 8 …」 */
  flex: 1 1 210px;
  min-width: 210px;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  color: #e2ecff;
  font-size: 14px;
  font-weight: 600;
  letter-spacing: 0.2px;
}
.dk-as-reset {
  flex: 0 0 auto;
  font-size: 12px !important;
  padding: 2px 8px !important;
}
.dk-as-jump {
  flex: 0 0 auto;
  width: 44px;
  padding: 2px 4px !important;
  border: 1px solid rgba(140, 170, 255, 0.35);
  border-radius: 5px;
  background: rgba(16, 22, 34, 0.9);
  color: #e2ecff;
  font: inherit;
  font-size: 12px !important;
  text-align: center;
}
.dk-as-flash {
  flex: 0 0 auto;
  display: none;
  padding: 2px 8px;
  border-radius: 5px;
  background: rgba(255, 200, 90, 0.14);
  border: 1px solid rgba(255, 200, 90, 0.35);
  color: #ffd479;
  font-size: 12px;
  line-height: 1.5;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.dk-as-tools {
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
.dk-as-range {
  flex: 0 0 auto;
  max-width: 200px;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  color: #6fd8c2;
  font-size: 11px;
}
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
/* 播放进度条：轨道 + 填充 + 切点刻度，整条可点可拖（定位播放位置） */
.dk-as-prog {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0 2px;
}
.dk-as-track {
  position: relative;
  flex: 1 1 auto;
  height: 10px;
  border-radius: 5px;
  background: rgba(120, 150, 210, 0.18);
  box-shadow: inset 0 0 0 1px rgba(120, 150, 210, 0.22);
  cursor: pointer;
  overflow: hidden;
}
.dk-as-fill {
  position: absolute;
  left: 0;
  top: 0;
  height: 100%;
  width: 0;
  border-radius: 5px;
  background: linear-gradient(90deg, #2f9c76, #3fd39a);
  box-shadow: 0 0 6px rgba(63, 211, 154, 0.5);
  transition: width 60ms linear;
}
.dk-as-ticks { position: absolute; inset: 0; pointer-events: none; }
.dk-as-knob {
  position: absolute;
  top: 50%;
  left: 0;
  width: 12px;
  height: 12px;
  margin: -6px 0 0 -6px;
  border-radius: 50%;
  background: #eaf5ff;
  box-shadow: 0 0 0 2px rgba(63, 211, 154, 0.9), 0 1px 4px rgba(0, 0, 0, 0.55);
  pointer-events: none;
}
.dk-as-tick {
  position: absolute;
  top: 0;
  width: 1px;
  height: 100%;
  background: rgba(226, 236, 252, 0.45);
}
.dk-as-proglbl {
  flex: 0 0 auto;
  min-width: 104px;
  text-align: right;
  color: #9fb0c8;
  font-size: 11px;
}
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
.dk-as-dur {
  flex: 0 0 auto;
  min-width: 40px;
  text-align: right;
  color: #7f8ea8;
  font-size: 11px;
}
.dk-as-row--playing .dk-as-dur { color: #a9f3d4; }
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

/* ---------------- 参考图 ---------------- */

.dk-as-refs {
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 5px 6px 6px;
  border-radius: 6px;
  background: rgba(90, 130, 220, 0.07);
  border: 1px solid rgba(120, 150, 210, 0.22);
}
.dk-as-refs-head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  row-gap: 3px;
}
.dk-as-refs-title { color: #8fb4ff; font-weight: 600; }
.dk-as-refs-count { color: #7f8ea8; font-size: 11px; }
.dk-as-refs-gap { flex: 1 1 auto; min-width: 4px; }
.dk-as-refs-unit { color: #7f8ea8; font-size: 11px; }
.dk-as .dk-as-refs-short {
  flex: 0 0 auto;
  width: 58px;
  padding: 1px 4px;
  border: 1px solid rgba(140, 170, 255, 0.35);
  border-radius: 5px;
  background: rgba(0, 0, 0, 0.35);
  color: #cfd8ea;
  font: inherit;
  font-size: 11px;
  text-align: right;
}
.dk-as-refs-grid {
  display: flex;
  gap: 5px;
  flex-wrap: nowrap;
}
.dk-as-ref-slot {
  position: relative;
  flex: 0 0 auto;
  width: 56px;
  height: 56px;
  border-radius: 5px;
  border: 1px dashed rgba(140, 170, 255, 0.32);
  background: rgba(0, 0, 0, 0.28);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  overflow: hidden;
}
.dk-as-ref-slot:hover {
  border-color: rgba(140, 180, 255, 0.78);
  background: rgba(90, 120, 200, 0.18);
}
.dk-as-ref-slot--has {
  border-style: solid;
  border-color: rgba(120, 170, 255, 0.55);
}
.dk-as-ref-slot img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.dk-as-ref-plus { color: #5c7099; font-size: 18px; line-height: 1; }
/* 删除按钮要压过通用按钮样式，所以多一层选择器 */
.dk-as .dk-as-ref-del {
  position: absolute;
  top: 1px;
  right: 1px;
  width: 15px;
  height: 15px;
  min-width: 0;
  padding: 0;
  border: none;
  border-radius: 50%;
  background: rgba(10, 14, 22, 0.82);
  color: #ff9a9a;
  font-size: 11px;
  line-height: 15px;
  text-align: center;
  cursor: pointer;
  opacity: 0;
  transition: opacity 90ms;
}
.dk-as-ref-slot:hover .dk-as-ref-del { opacity: 1; }
.dk-as .dk-as-ref-del:hover { background: rgba(60, 16, 16, 0.92); color: #ff6b6b; }
.dk-as-ref-tag {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  padding: 0 3px;
  font-size: 9px;
  line-height: 12px;
  color: #cfe0ff;
  background: rgba(10, 14, 22, 0.72);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* 段行里的「图 N」按钮 */
.dk-as .dk-as-refbtn {
  flex: 0 0 auto;
  min-width: 32px;
  padding: 1px 5px;
  font-size: 11px;
  color: #7c8aa4;
  border-color: rgba(140, 170, 255, 0.22);
  background: rgba(90, 120, 200, 0.08);
}
.dk-as .dk-as-refbtn:hover { color: #cfe0ff; }
.dk-as .dk-as-refbtn--on {
  color: #6fd8c2;
  border-color: rgba(63, 211, 154, 0.55);
  background: rgba(35, 160, 115, 0.2);
}

/* 展开后的段内参考图子行 */
.dk-as-refsub {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 1px 0 6px 26px;
  padding: 5px 6px;
  border-radius: 5px;
  border: 1px solid rgba(120, 170, 255, 0.22);
  background: rgba(90, 130, 220, 0.08);
}
.dk-as-refsub-head {
  display: flex;
  align-items: center;
  gap: 6px;
}
.dk-as-refsub-note {
  flex: 1 1 auto;
  min-width: 0;
  color: #8b98ae;
  font-size: 11px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.dk-as-refs-grid--small .dk-as-ref-slot { width: 42px; height: 42px; }
.dk-as-refs-grid--small .dk-as-ref-plus { font-size: 15px; }
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
    // 每段的自定义参考图。空数组 / 缺字段 = 沿用全局，所以统一成数组再往下传。
    out.push({
      start: Math.max(0, start),
      end,
      prompt: String(item.prompt || ""),
      images: cleanPaths(item.images),
    });
  }
  out.sort((a, b) => a.start - b.start);
  return out;
}

/** 路径数组清洗：去掉空串和重复项，最多留 MAX_REFS 个 */
function cleanPaths(raw) {
  if (!Array.isArray(raw)) return [];
  const out = [];
  for (const item of raw) {
    const text = String(item ?? "").trim();
    if (text && !out.includes(text)) out.push(text);
  }
  return out.slice(0, MAX_REFS);
}

/**
 * 读「参考图数据」：`{short: 1024, images: [路径…]}`。
 *
 * 容错优先 —— 老工作流里这个字段根本不存在，面板也得正常打开。
 */
function readRefConfig(raw) {
  let data = raw;
  if (typeof raw === "string") {
    try {
      data = JSON.parse(raw || "{}");
    } catch (err) {
      data = {};
    }
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) data = {};
  const short = Math.round(Number(data.short));
  return {
    short: Number.isFinite(short) && short >= 0 ? short : REF_SHORT_DEFAULT,
    images: cleanPaths(data.images),
  };
}

/** 参考图缩略图地址（走后端代理；本机绝对路径浏览器读不到） */
function refThumbUrl(path) {
  const base = `/dogking-tools/image_file?thumb=1&path=${encodeURIComponent(path)}`;
  // ComfyUI 可能挂在子路径下，和试听音频一样过一遍 apiURL
  return typeof api?.apiURL === "function" ? api.apiURL(base) : base;
}

/** 取文件名（路径可能带 / 也可能带 \） */
function baseName(path) {
  const text = String(path || "");
  const cut = Math.max(text.lastIndexOf("/"), text.lastIndexOf("\\"));
  return cut >= 0 ? text.slice(cut + 1) : text;
}

/** 往数组里并入路径：去重，最多 MAX_REFS 个 */
function mergePaths(target, extra) {
  for (const item of extra || []) {
    const text = String(item ?? "").trim();
    if (!text || target.includes(text)) continue;
    if (target.length >= MAX_REFS) break;
    target.push(text);
  }
  return target;
}

/** 从同一份 JSON 里读选区；没有 / 坏掉返回 null（调用方退回 0~总长） */
function readRange(raw) {
  let data = raw;
  if (typeof raw === "string") {
    try {
      data = JSON.parse(raw || "{}");
    } catch (err) {
      return null;
    }
  }
  const r = data && !Array.isArray(data) ? data.range : null;
  if (!r || typeof r !== "object") return null;
  const start = Number(r.start);
  const end = Number(r.end);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end - start < 0.06) return null;
  return { start: Math.max(0, start), end };
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
    // 选区（裁剪窗口）：只有这段范围参与分段。默认整段音频，拖最外侧两条线就能裁，
    // 拖的时候两侧被排除的部分会压暗 + 打斜纹，一眼看得出哪些不要
    this.range = { start: 0, end: 0 };
    this.audioEl = new Audio();
    this.playing = -1;      // 正在试听第几段（-1 = 没在放）

    // 参考图：全局 6 张 + 每段的独立 6 张（段内有图就整组覆盖全局）
    this.globalRefs = [];
    this.refShort = REF_SHORT_DEFAULT;
    this.refOpen = -1;      // 展开了参考图子行的段下标（-1 = 都没展开）

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

    /* 全局参考图不在这里 —— 它画在节点顶部的空白区（见 drawRefsPanel）。
       那块空白是 18 个输出口把 widget 区起点推下去造成的，DOM widget 挪不上去：
       容器的 top 由 litegraph 控制、改了就跑到节点外面。节点自己的画布不受这个
       限制，而那一片本来就什么都没有，正好拿来放图。 */

    /* 全局提示词：原本是节点上的多行 widget，它占掉顶部空白区下半截，
       把参考图第 3 行挡得看不见。挪进面板之后那块空白就整块归参考图。 */
    const gp = el("div", "dk-as-global");
    gp.appendChild(el("span", "dk-as-global-lbl", "全局提示词"));
    this.globalPromptEl = el("textarea", "dk-as-global-input");
    this.globalPromptEl.rows = 2;
    this.globalPromptEl.placeholder = "所有段共用；某段自己写了就以那段的为准";
    this.globalPromptEl.addEventListener("pointerdown", (e) => e.stopPropagation());
    this.globalPromptEl.addEventListener("keydown", (e) => e.stopPropagation());
    this.globalPromptEl.addEventListener("input", () => {
      this.writeWidget("全局提示词", this.globalPromptEl.value);
    });
    gp.appendChild(this.globalPromptEl);

    /* 工具栏 */
    const bar = el("div", "dk-as-bar");
    this.btnPick = el("button", "", "选择音频");
    this.btnReload = el("button", "", "重新载入");
    this.nameEl = el("span", "dk-as-name", "还没选音频");
    this.rangeEl = el("span", "dk-as-range");
    this.btnPlay = el("button", "", "▶ 试听本段");
    bar.append(this.btnPick, this.btnReload, this.nameEl, this.rangeEl, this.btnPlay);

    /* 波形 */
    const wrap = el("div", "dk-as-wavewrap");
    this.canvas = document.createElement("canvas");
    wrap.appendChild(this.canvas);

    /* 播放进度条：波形下面一条细轨道，填充到当前播放位置，可点击/拖动定位 */
    const prog = el("div", "dk-as-prog");
    this.progTrack = el("div", "dk-as-track");
    this.progTicks = el("div", "dk-as-ticks");
    this.progFill = el("div", "dk-as-fill");
    this.progKnob = el("div", "dk-as-knob");
    this.progTrack.append(this.progTicks, this.progFill, this.progKnob);
    this.progLabel = el("span", "dk-as-proglbl", "00:00.00 / 00:00.00");
    this.progLabel.title = "当前位置 / 音频总长";
    prog.append(this.progTrack, this.progLabel);
    this.bindProgress();

    /* 输出状态行：模式 + 第几段/共几段 + 本段秒数（原来塞在标题栏 badge 里，太小） */
    const exec = el("div", "dk-as-exec");
    this.execDot = el("span", "dk-as-exec-dot");
    this.execEl = el("span", "dk-as-exec-text", "");
    this.btnResetSeq = el("button", "dk-as-reset", "↺ 回到第 1 段");
    this.btnResetSeq.title = "清掉顺序游标：下次执行从第 1 段重新开始（切点和提示词都不动）";

    /* 跳到指定段：中途断掉 / 想补某一段时用，避免手改 state.json。
       带上它就不用「回第 1 段」把前功尽弃 —— 已生成的片段一个都不动。 */
    this.jumpInput = el("input", "dk-as-jump");
    this.jumpInput.type = "number";
    this.jumpInput.min = "1";
    this.jumpInput.step = "1";
    this.jumpInput.title = "下次执行从第几段开始（1 ~ 段数）";
    this.btnJump = el("button", "dk-as-reset", "跳到");
    this.btnJump.title = "把顺序游标设到这一段：下次执行从这里开始，已生成的片段不动";

    exec.append(this.execDot, this.execEl, this.jumpInput, this.btnJump, this.btnResetSeq);

    /* 提示行：独立一行，不再覆盖上面那个文件名 —— 以前 flash() 直接写 nameEl，
       「已回到第 1 段」会把「voice.wav · 0:32.08」永久顶掉，窄面板下还被截成「已回到第_」。 */
    this.flashEl = el("div", "dk-as-flash");

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
    this.btnRange = el("button", "", "选区取整段");
    this.btnRange.title = "把选区恢复成整段音频（清掉裁剪）";
    tools.append(this.btnCut, this.btnEven, this.secInput, el("span", "", "秒/段"), this.btnClear, this.btnRange);

    /* 段清单 */
    this.listEl = el("div", "dk-as-list");

    this.el.append(gp, bar, wrap, prog, exec, this.flashEl, tools, this.listEl);
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
    this.btnRange.addEventListener("click", () => this.applyRange(0, this.duration));
    this.btnResetSeq.addEventListener("click", () => this.resetSequence());
    this.btnJump.addEventListener("click", () => this.jumpSequence());
    // 输入框里回车也跳，省一次鼠标
    this.jumpInput.addEventListener("keydown", (e) => {
      e.stopPropagation();          // 别让 ComfyUI 把回车当成排队
      if (e.key === "Enter") this.jumpSequence();
    });

    this.bindCanvas();

    /* 6 个全局参考图槽画在节点顶部那块空白里（不是面板内部）。
       挂上去之后节点每次重绘都会带上，面板高度因此不用再留这 96px。 */
    this.bindNodeCanvas();
    this.renderRefs();

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
   *
   * 最左/最右那两条同时就是**选区边界**：把它们往里拖 = 裁掉头尾不要的部分。
   * 没有分段时也返回这两条，用户可以先框选区、再点「自动等分」。
   */
  boundaries() {
    const segs = this.segments;
    if (!segs.length) {
      const r = this.range;
      return r && r.end - r.start >= MIN_GAP ? [r.start, r.end] : [];
    }
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
      if (e.button !== 0) return;
      if (!this.segments.length && !(this.range.end - this.range.start >= MIN_GAP)) return;

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
   * 最左/最右那条同时是**选区边界**：往里拖就是裁掉头尾，range 跟着改，
   * 被裁掉的部分在画布上压暗 + 斜纹。
   */
  moveBoundary(b, time) {
    const segs = this.segments;
    const n = segs.length;
    const dur = this.duration || (this.range.end || 0);
    if (!dur) return;

    const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), Math.max(lo, hi));
    const t = clamp(Number(time) || 0, 0, dur);

    // 没有分段时只有两条边界（0 = 头、1 = 尾），有分段时是 n+1 条
    const lastEdge = n || 1;
    if (b <= 0 || b >= lastEdge) {
      // 选区边界：往里拖就是裁掉头/尾。拖过里面的切点就把那些段一起吃掉，
      // 而不是像以前那样卡在 0.06 秒死活过不去
      const isHead = b <= 0;
      const lo = isHead ? clamp(t, 0, this.range.end - MIN_GAP) : this.range.start;
      const hi = isHead ? this.range.end : clamp(t, this.range.start + MIN_GAP, dur);
      this.range = { start: lo, end: hi };
      this.clipToRange();
      this.cursor = clamp(t, lo, hi);
    } else if (!n) {
      // 没有分段、也没有合法选区：什么都不能拖
      return;
    } else {
      const left = segs[b - 1];
      const right = segs[b];
      const lo = left.start + MIN_GAP;
      const hi = right.end - MIN_GAP;
      const v = clamp(t, lo, hi);
      left.end = v;
      right.start = v;                 // ← 关键：另一侧跟着走
      this.syncRange();
      this.cursor = clamp(t, this.range.start, this.range.end);
    }

    this.paintTicks();
    this.updateRangeLabel();
    // 拖动过程中**只重画画布**：段清单里有输入框，每帧重建 DOM 会打断输入、
    // 段数多时也卡。清单等 pointerup 时再重建（见 bindCanvas 的 endDrag）。
    this.draw();
  }

  /** 分段的头尾就是选区头尾，两边始终一致 */
  syncRange() {
    if (!this.segments.length) return;
    this.range.start = this.segments[0].start;
    this.range.end = this.segments[this.segments.length - 1].end;
  }

  /**
   * 按当前 range 裁分段：窗口外的段丢掉，跨边界的段夹到边界上。
   *
   * 拖选区边界时每帧都会调 —— 是「剪」不是「删」：段被裁短后往回拖只会把那一段
   * 拉长，已经剪掉的切点不会回来（和剪辑软件的裁剪一样）。
   */
  clipToRange() {
    if (!this.segments.length) return;
    const lo = this.range.start;
    const hi = this.range.end;
    const clipped = [];
    for (const s of this.segments) {
      const st = Math.max(s.start, lo);
      const en = Math.min(s.end, hi);
      if (en - st > MIN_GAP) clipped.push({ start: st, end: en, prompt: s.prompt || "" });
    }
    // 超出预期地空掉（比如旧数据本身是断的）：兜一段覆盖整个选区，别让画布空着
    if (!clipped.length && hi - lo > MIN_GAP) {
      clipped.push({ start: lo, end: hi, prompt: "" });
    }
    this.segments = clipped;
    this.normalizeSegments();
    this.active = Math.max(0, Math.min(this.active, this.segments.length - 1));
    // 段被裁少了，原来的「当前段」可能已经越界 —— 写回去，别留一个指向不存在段号的 widget
    this.syncActiveWidget();
  }

  /** 选区变了以后按新窗口重排分段：裁掉外面的、贴上两端的 */
  applyRange(start, end) {
    const dur = this.duration || end;
    const lo = Math.max(0, Math.min(start, dur));
    const hi = Math.min(dur, Math.max(end, lo + MIN_GAP));
    this.range = { start: lo, end: hi };

    if (this.segments.length) {
      this.clipToRange();
      this.commit();
      this.renderList();
    }
    this.updateRangeLabel();
    this.paintTicks();
    this.updatePlayButton();
    this.draw();
  }

  /** 工具栏 / 顶栏上显示选区长度，免得用户以为自己框错了 */
  updateRangeLabel() {
    if (!this.rangeEl) return;
    const r = this.range;
    if (!(r.end - r.start >= MIN_GAP)) {
      this.rangeEl.textContent = "";
      return;
    }
    const len = r.end - r.start;
    const full = this.duration && Math.abs(len - this.duration) < 0.01;
    this.rangeEl.textContent = full ? "选区=整段" : `选区 ${fmtTick(r.start)}–${fmtTick(r.end)} · ${len.toFixed(1)}s`;
    this.rangeEl.title = "只有选区内的音频会参与分段；拖波形最左/最右那两条线可以改";
  }

  /* ---------------- 输出状态 ---------------- */

  /**
   * 面板里的输出状态行：模式 + 第几段 / 共几段 + 本段秒数。
   *
   * 以前把这些塞进节点标题栏的 badge，字小得看不清；现在放在面板里，
   * 14px 加粗，配合右边的「回到第 1 段」按钮 —— 顺序跑偏了一眼就能发现并拉回来。
   */
  updateExecInfo() {
    if (!this.execEl) return;
    const mode = this.readWidget("输出模式") || "顺序";
    const total = this.segments.length;
    // 试听中显示正在放的那段，否则显示选中的
    const i = this.playing >= 0 ? this.playing : this.active;
    const seg = this.segments[i];
    if (!seg) {
      this.execEl.textContent = `${mode} · 还没有分段`;
      if (this.execDot) this.execDot.style.background = "#5c6779";
      return;
    }
    const len = seg.end - seg.start;
    this.execEl.textContent = `${mode} · 第 ${i + 1} / ${total} 段 · ${len.toFixed(2)}s`;
    // 输入框跟着高亮段走；正在输入时不要抢用户打的字
    if (this.jumpInput && document.activeElement !== this.jumpInput) {
      this.jumpInput.value = String(i + 1);
    }
    if (this.execDot) {
      this.execDot.style.background = this.playing >= 0 ? "#3fd39a" : "#5b8dff";
    }
  }

  /**
   * 清掉顺序游标，下次执行从第 1 段开始。
   *
   * 借的是「提示词批量加载」那个 /dogking-tools/reset 接口（同一个 state.json，
   * 按节点 id 把 index 置 0）—— 不用为音频节点再开一套。
   */
  async resetSequence() {
    const key = String(this.node.id ?? "");
    try {
      const res = await api.fetchApi("/dogking-tools/reset", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ node: key }),
      });
      const data = await res.json();
      if (!data?.ok) {
        this.flash(data?.error || "重置失败");
        return;
      }
      this.stopPlay();
      this.setActive(0, true);
      this.updateExecInfo();
      this.flash(this.segments.length
        ? "已回到第 1 段 · 下次从 1 开始"
        : "已清空顺序游标（还没有分段）");
    } catch (err) {
      this.flash("重置失败：" + err);
    }
  }

  /**
   * 把顺序游标设到面板上填的那一段，下次执行从它开始。
   *
   * 后台游标是 0 起的「下一条下标」，所以传 index = N - 1。
   * 中途强杀 / 手动补跑都靠这个，比「回到第 1 段」温和 —— 已生成的
   * seg_000N.mp4 一个都不动，只有下一次从哪段开始变了。
   */
  async jumpSequence() {
    const total = this.segments.length;
    if (!total) {
      this.flash("还没有分段");
      return;
    }
    const raw = Number.parseInt(this.jumpInput.value, 10);
    const seg = Math.min(Math.max(Number.isFinite(raw) ? raw : 1, 1), total);
    this.jumpInput.value = String(seg);

    try {
      const res = await api.fetchApi("/dogking-tools/reset", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ node: String(this.node.id ?? ""), index: seg - 1 }),
      });
      const data = await res.json();
      if (!data?.ok) {
        this.flash(data?.error || "跳转失败");
        return;
      }
      this.stopPlay();
      this.setActive(seg - 1, true);
      this.updateExecInfo();
      this.flash(`已跳到第 ${seg} 段 · 下次从 ${seg} 开始`);
    } catch (err) {
      this.flash("跳转失败：" + err);
    }
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
    const r = this.range;
    const hasRange = r && r.end - r.start >= MIN_GAP;
    const hiRaw = hasRange ? r.end : this.duration || this.segments[this.segments.length - 1].end;
    const lo = hasRange ? Math.max(0, Math.min(r.start, hiRaw)) : 0;
    const hi = Math.max(lo, hiRaw);

    const out = [];
    for (const raw of this.segments) {
      const start = Math.max(lo, Math.min(Number(raw.start) || 0, hi));
      const end = Math.max(start, Math.min(Number(raw.end) || 0, hi));
      const prompt = String(raw.prompt || "");
      // 每段的参考图必须跟着段一起搬 —— 这里整表重建，漏一个字段就等于
      // 用户配好的图被「改一下提示词」顺手清空了
      const images = cleanPaths(raw.images);
      if (!out.length) {
        if (end - start >= MIN_GAP) out.push({ start, end, prompt, images });
        continue;
      }
      const prev = out[out.length - 1];
      const st = prev.end;                        // 强制共边
      // 留 1e-6 的余量：12.06 - 12 在浮点里是 0.0600000000000005，直接和 0.06 比会漏掉
      if (end - st <= MIN_GAP + 1e-6) {
        prev.end = Math.max(prev.end, end);       // 碎段（含正好 0.06 秒的）并进前一段
        if (!prev.prompt) prev.prompt = prompt;
        // 碎段自己的参考图一起带过去（前一段有图就以它为准）
        if (!prev.images.length && images.length) prev.images = images;
      } else {
        out.push({ start: st, end, prompt, images });
      }
    }
    if (!out.length) return;
    // 头尾贴住选区：选区边界就是第一个切点和最后一个切点
    out[0].start = lo;
    out[out.length - 1].end = hi;
    this.segments = out;
    this.syncRange();
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
    // 切开后新出现的右半段不继承参考图：本段的图是配给「这一段画面」的，
    // 复制到两段等于凭空多出一段用同一批参考的画面，用户得自己删一半
    const right = { start: t, end: seg.end, prompt: "", images: [] };
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
      // 先有选区：没有分段时按选区建一段，再切
      const r = this.range;
      const seg = r.end - r.start >= MIN_GAP ? { start: r.start, end: r.end } : { start: 0, end: this.duration };
      this.segments = [{ ...seg, prompt: "", images: [] }];
      this.syncRange();
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
    // 只在选区里等分 —— 头尾不要的部分（选区外）不参与
    const r = this.range;
    const from = r.end - r.start >= MIN_GAP ? r.start : 0;
    const to = r.end - r.start >= MIN_GAP ? r.end : this.duration;
    const count = Math.max(1, Math.ceil((to - from) / step - 1e-6));
    const segments = [];
    for (let i = 0; i < count; i += 1) {
      const start = from + i * step;
      const end = Math.min(to, from + (i + 1) * step);
      if (end - start < MIN_GAP) continue;
      segments.push({ start, end, prompt: "", images: [] });
    }
    // 尾巴太短就并进前一段，别留下 0.05 秒的空壳
    const last = segments[segments.length - 1];
    if (last && to - last.end > 1e-6 && to - last.end <= MIN_GAP) last.end = to;
    this.segments = segments;
    this.active = 0;
    this.refOpen = -1;
    this.syncActiveWidget();
    this.commit();
    this.renderList();
    this.draw();
  }

  clearSegments() {
    // 只清分段，选区保留 —— 用户裁好的头尾不该被「清空分段」一起抹掉
    this.segments = [];
    this.active = 0;
    this.refOpen = -1;
    this.syncActiveWidget();
    this.updateRangeLabel();
    this.commit();
    this.renderList();
    this.draw();
  }

  /**
   * 把面板选中的段写回「当前段」widget。
   *
   * 后端单段模式只读这个 widget，面板上高亮哪一段跟它无关 —— 两边一旦不同步，
   * 就会出现「面板亮第 1 段、实际输出第 8 段」这种看起来像 bug、其实是数据陈旧的情况。
   * 所有改动 active 的地方（等分 / 删段 / 改选区 / 换音频）都要过这里。
   */
  syncActiveWidget() {
    this.writeWidget("当前段", this.active + 1);
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
    this.refOpen = -1;          // 段号全往前挪了一位，展开面板会指错段
    this.syncActiveWidget();
    this.commit();
    this.renderList();
    this.draw();
  }

  setActive(index, syncWidget) {
    this.active = Math.max(0, Math.min(index, this.segments.length - 1));
    if (syncWidget) this.writeWidget("当前段", this.active + 1);
    this.updatePlayButton();
    this.updateExecInfo();
    this.renderList();
    this.draw();
  }

  /* ---------------- 音频 ---------------- */

  /* ---------------- 参考图 ---------------- */

  /**
   * 画一组参考图槽位（固定 MAX_REFS 个）。
   *
   * 为什么固定画满 6 个而不是只画已有的：空槽就是「还能再加」的入口，
   * 用户不用先找「添加」按钮再理解它加在哪儿 —— 点哪个空位就填哪个。
   */
  buildRefGrid(paths, opts) {
    const grid = document.createElement("div");
    grid.className = "dk-as-refs-grid" + (opts.small ? " dk-as-refs-grid--small" : "");
    for (let i = 0; i < MAX_REFS; i += 1) {
      grid.appendChild(this.makeRefSlot(paths[i], i, opts));
    }
    return grid;
  }

  makeRefSlot(path, index, opts) {
    const slot = document.createElement("div");
    slot.className = "dk-as-ref-slot" + (path ? " dk-as-ref-slot--has" : "");
    slot.title = path ? `${path}\n点一下换掉这张` : "点一下选图（可一次多选）";

    if (path) {
      const img = document.createElement("img");
      img.src = refThumbUrl(path);
      img.draggable = false;
      img.alt = "";
      // 缩略图生成失败（文件被删 / 格式怪）时退回一个占位，不要把槽位留空
      img.addEventListener("error", () => {
        img.remove();
        const mark = document.createElement("span");
        mark.className = "dk-as-ref-plus";
        mark.textContent = "?";
        slot.appendChild(mark);
      });
      slot.appendChild(img);

      const tag = document.createElement("span");
      tag.className = "dk-as-ref-tag";
      tag.textContent = baseName(path);
      slot.appendChild(tag);

      const del = document.createElement("button");
      del.className = "dk-as-ref-del";
      del.textContent = "×";
      del.title = "移除这张参考图";
      del.addEventListener("pointerdown", (e) => e.stopPropagation());
      del.addEventListener("click", (e) => {
        e.stopPropagation();
        opts.onClear(index);
      });
      slot.appendChild(del);
    } else {
      const plus = document.createElement("span");
      plus.className = "dk-as-ref-plus";
      plus.textContent = "＋";
      slot.appendChild(plus);
    }

    slot.addEventListener("pointerdown", (e) => e.stopPropagation());
    slot.addEventListener("click", (e) => {
      e.stopPropagation();
      opts.onPick(index);
    });
    return slot;
  }

  /* ---------------- 全局参考图（画在节点顶部空白区） ---------------- */

  /**
   * 6 个全局参考图槽位画在哪儿。
   *
   * 那块空白不是排版失误：litegraph 的 widget 区起点 = 标题高 +
   * max(输入口高, 输出口高)。这节点有 18 个输出口，右侧占掉约 360px，
   * 左边同高的一段就没有任何 widget 覆盖。
   *
   * DOM widget 挪不进去 —— 容器是绝对定位的，top 由 litegraph 每帧写死，
   * 改 top / margin / transform 都会让面板跑到节点外面。而节点自己的
   * canvas 在这一层没有限制，那一片本来就是空的，直接画就行。
   */
  /**
   * 面板 DOM 顶边落在节点坐标的哪儿。
   *
   * 不能用 widget.y / last_y —— 隐藏掉「全局提示词」之后，litegraph 没有把
   * 后面 widget 的 y 往下重算，报出来的 544 比实际位置大 170 像素，
   * 照着画会把下面一两行槽位画到面板底下。直接量 DOM 最准。
   */
  panelTopNode() {
    const app = globalThis.comfyAPI?.app?.app;
    const cvs = app?.canvas?.canvas;
    const ds = app?.canvas?.ds;
    const el = this.el;
    if (!cvs || !ds || !el || !el.isConnected || !ds.scale) return null;
    const r = el.getBoundingClientRect();
    if (!r.height) return null;
    const cr = cvs.getBoundingClientRect();
    // 屏幕 = (画布坐标 + offset) * scale，反解出画布坐标后减去节点 pos，
    // 才是 onDrawForeground 里那套「节点内坐标」
    const canvasY = (r.top - cr.top) / ds.scale - ds.offset[1];
    return canvasY - (this.node?.pos?.[1] ?? 0);
  }

  refGeometry() {
    const node = this.node;
    if (!node) return null;
    // 格子区下界 = **第一行控件（输出模式）的顶上**，不是 DOM 面板的顶。
    // 控件行（输出模式~续接帧数）画在 [widgets_start_y, 面板顶] 这一段里，
    // 之前量面板顶，格子伸下去正好压在控件行上 —— 这就是「超边界」的来源。
    // litegraph 每帧布局时算好这个值，输入/输出口再多它也是对的。
    let bottom = Number(node.widgets_start_y);
    if (!Number.isFinite(bottom) || bottom < 120) {
      const first = (node.widgets || []).find((w) => w.name === "输出模式");
      bottom = Number(first?.last_y ?? first?.y);
    }
    if (!Number.isFinite(bottom) || bottom < 120) return null;

    const TITLE = globalThis.LiteGraph?.NODE_TITLE_HEIGHT ?? 30;
    const W = node.size[0];
    const pad = 10;
    const rightPad = 132;                 // 右侧那排输出口标签要占的宽度
    const cols = 3;
    const rows = 2;
    const gapX = 8;
    const gapY = 8;
    // 格子只大到「能看清缩略图」为止：节点拉得再宽再高，格子也不跟着膨胀，
    // 多出来的空间用横向/纵向居中消化掉 —— 不然随手一拉节点，格子就变成海报墙。
    const MAX_CELL_W = 200;
    const MAX_CELL_H = 150;
    const availW = W - rightPad - pad * 2 - gapX * (cols - 1);
    const cellW = Math.max(60, Math.min(MAX_CELL_W, availW / cols));
    const headH = 34;                     // 顶上那行「全局参考图 / 短边」
    const usable = bottom - (TITLE + headH) - 6;
    const cellH = Math.max(
      40,
      Math.min(
        cellW * 0.78,                     // 上限取 4:3 而不是 16:9：素材多是竖版人像，格子高一点反而裁得少
        (usable - gapY * (rows - 1)) / rows,
        MAX_CELL_H,
      ),
    );
    // 格子组整体**顶格往上贴**：紧挨「音频」输入口下面开始，多出来的高度
    // 全部留在格子底下 —— 别居中，居中会让格子下坠、底边贴到「输出模式」
    const gridW = cellW * cols + gapX * (cols - 1);
    const x0 = pad + Math.max(0, (W - rightPad - pad * 2 - gridW) / 2);
    const top = TITLE + headH;
    return { TITLE, bottom, W, pad, rightPad, cols, rows, gapX, gapY, cellW, cellH, top, x0 };
  }

  refSlotRect(i) {
    const g = this.refGeometry();
    if (!g) return null;
    return {
      x: g.x0 + (i % g.cols) * (g.cellW + g.gapX),
      y: g.top + Math.floor(i / g.cols) * (g.cellH + g.gapY),
      w: g.cellW,
      h: g.cellH,
    };
  }

  refShortRect() {
    const g = this.refGeometry();
    if (!g) return null;
    const w = 104;
    const h = 18;
    return { x: g.W - g.rightPad - g.pad - w, y: g.top - 24, w, h };
  }

  /** 缩略图对象：同一路径只 new 一次，加载完主动重绘一次 */
  refImage(path) {
    if (!this._refImgs) this._refImgs = new Map();
    const hit = this._refImgs.get(path);
    if (hit) return hit;
    const rec = { img: new Image(), ready: false };
    rec.img.onload = () => {
      rec.ready = true;
      this.refreshCanvas();
    };
    rec.img.onerror = () => {
      rec.ready = false;
      this.refreshCanvas();
    };
    rec.img.src = refThumbUrl(path);
    this._refImgs.set(path, rec);
    return rec;
  }

  refreshCanvas() {
    this.node?.setDirtyCanvas?.(true, true);
  }

  /** 每帧由节点的 onDrawForeground 调用 */
  drawRefsPanel(ctx) {
    const g = this.refGeometry();
    if (!g) return;

    ctx.save();
    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";

    // 标题行
    ctx.fillStyle = "rgba(160,190,235,0.95)";
    ctx.font = "bold 11px ui-monospace, Consolas, monospace";
    ctx.fillText("全局参考图", g.pad, g.top - 8);
    const tw = ctx.measureText("全局参考图").width;
    ctx.fillStyle = "rgba(120,140,175,0.9)";
    ctx.font = "10px ui-monospace, Consolas, monospace";
    ctx.fillText(`${this.globalRefs.length}/${MAX_REFS}`, g.pad + tw + 8, g.top - 8);

    // 短边（点一下弹输入）
    const sr = this.refShortRect();
    if (sr) {
      ctx.fillStyle = "rgba(120,140,175,0.9)";
      ctx.textAlign = "right";
      ctx.fillText(`短边 ${this.refShort} px`, sr.x + sr.w, g.top - 8);
      ctx.textAlign = "left";
    }

    for (let i = 0; i < MAX_REFS; i += 1) {
      const r = this.refSlotRect(i);
      if (!r || r.y + r.h > g.bottom - 4) break;
      const path = this.globalRefs[i];

      ctx.beginPath();
      if (ctx.roundRect) ctx.roundRect(r.x, r.y, r.w, r.h, 6);
      else ctx.rect(r.x, r.y, r.w, r.h);

      if (path) {
        const rec = this.refImage(path);
        ctx.save();
        ctx.clip();
        ctx.fillStyle = "rgba(30,36,48,1)";
        ctx.fillRect(r.x, r.y, r.w, r.h);
        if (rec.ready && rec.img.naturalWidth) {
          // cover：铺满整格，多出来的居中裁掉
          const ir = rec.img.naturalWidth / rec.img.naturalHeight;
          const cr = r.w / r.h;
          let sw = rec.img.naturalWidth;
          let sh = rec.img.naturalHeight;
          if (ir > cr) sw = sh * cr;
          else sh = sw / cr;
          ctx.drawImage(
            rec.img,
            (rec.img.naturalWidth - sw) / 2,
            (rec.img.naturalHeight - sh) / 2,
            sw, sh,
            r.x, r.y, r.w, r.h,
          );
        }
        ctx.restore();

        // 左上角序号
        ctx.fillStyle = "rgba(0,0,0,0.55)";
        ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(r.x + 5, r.y + 5, 20, 16, 4);
        else ctx.rect(r.x + 5, r.y + 5, 20, 16);
        ctx.fill();
        ctx.fillStyle = "rgba(220,235,255,0.95)";
        ctx.font = "10px ui-monospace, Consolas, monospace";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(String(i + 1), r.x + 15, r.y + 13);

        // 右上角删除
        ctx.fillStyle = "rgba(0,0,0,0.55)";
        ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(r.x + r.w - 22, r.y + 5, 17, 17, 4);
        else ctx.rect(r.x + r.w - 22, r.y + 5, 17, 17);
        ctx.fill();
        ctx.fillStyle = "rgba(255,190,190,0.95)";
        ctx.font = "bold 12px ui-monospace, Consolas, monospace";
        ctx.fillText("×", r.x + r.w - 13.5, r.y + 14);

        ctx.textAlign = "left";
        ctx.textBaseline = "alphabetic";
      } else {
        ctx.fillStyle = "rgba(90,130,220,0.10)";
        ctx.fill();
        ctx.strokeStyle = "rgba(120,150,210,0.45)";
        ctx.lineWidth = 1;
        ctx.setLineDash([5, 4]);
        ctx.stroke();
        ctx.setLineDash([]);

        ctx.fillStyle = "rgba(140,168,210,0.75)";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.font = "14px ui-monospace, Consolas, monospace";
        ctx.fillText("＋", r.x + r.w / 2, r.y + r.h / 2 - 8);
        ctx.font = "10px ui-monospace, Consolas, monospace";
        ctx.fillText(`图${i + 1}`, r.x + r.w / 2, r.y + r.h / 2 + 12);
        ctx.textAlign = "left";
        ctx.textBaseline = "alphabetic";
      }
    }

    ctx.restore();
  }

  /** 命中测试：节点内坐标 -> 点到了什么 */
  hitRef(pos) {
    const g = this.refGeometry();
    if (!g || !pos) return null;
    if (pos[1] <= g.TITLE || pos[1] >= g.bottom) return null;

    const sr = this.refShortRect();
    if (sr && pos[0] >= sr.x && pos[0] <= sr.x + sr.w &&
        pos[1] >= sr.y - 2 && pos[1] <= sr.y + sr.h) {
      return { kind: "short" };
    }

    for (let i = 0; i < MAX_REFS; i += 1) {
      const r = this.refSlotRect(i);
      if (!r || r.y + r.h > g.bottom - 4) break;
      if (pos[0] < r.x || pos[0] > r.x + r.w || pos[1] < r.y || pos[1] > r.y + r.h) {
        continue;
      }
      if (this.globalRefs[i]) {
        const dx = r.x + r.w - 22;
        const dy = r.y + 5;
        if (pos[0] >= dx && pos[0] <= dx + 17 && pos[1] >= dy && pos[1] <= dy + 17) {
          return { kind: "clear", index: i };
        }
        return { kind: "replace", index: i };
      }
      return { kind: "add", index: i };
    }
    return null;
  }

  onRefClick(hit) {
    if (!hit) return;
    if (hit.kind === "short") {
      const v = window.prompt(
        "短边限制（像素）：短边缩到这个值、长边等比，不裁切不补边；0 = 原图",
        String(this.refShort || REF_SHORT_DEFAULT),
      );
      if (v === null) return;
      const n = Math.round(Number(v));
      if (!Number.isFinite(n)) return;
      this.refShort = Math.max(0, Math.min(n, 8192));
      this.commitRefs();
      this.refreshCanvas();
      return;
    }
    if (hit.kind === "clear") {
      this.globalRefs.splice(hit.index, 1);
      this.commitRefs();
      this.refreshCanvas();
      this.renderList();          // 段行里的「沿用全局 N 张」要跟着变
      return;
    }
    this.pickGlobalRefs(hit.index);
  }

  /** 把画布层挂到节点上（只挂一次） */
  bindNodeCanvas() {
    const node = this.node;
    if (!node || node.__dkRefCanvas) return;
    node.__dkRefCanvas = true;

    const prevFg = node.onDrawForeground;
    node.onDrawForeground = function (ctx) {
      // 原生绘制可能改过变换矩阵，包起来再画自己的，免得坐标被带偏
      ctx.save();
      if (prevFg) prevFg.apply(this, arguments);
      ctx.restore();
      this.dkAudio?.drawRefsPanel(ctx);
    };

    const prevDown = node.onMouseDown;
    node.onMouseDown = function (e, localPos) {
      const hit = this.dkAudio?.hitRef(localPos);
      if (hit) {
        this.dkAudio.onRefClick(hit);
        return true;
      }
      return prevDown ? prevDown.apply(this, arguments) : false;
    };
  }

  renderRefs() {
    this.refreshCanvas();
  }

  /** 全局参考图：点空槽补图，点已有槽换那一张 */
  async pickGlobalRefs(index) {
    const replacing = Boolean(this.globalRefs[index]);
    const room = MAX_REFS - this.globalRefs.length + (replacing ? 1 : 0);
    const paths = await this.askImages(Math.max(1, room));
    if (!paths.length) return;

    if (replacing) {
      this.globalRefs[index] = paths[0];
      mergePaths(this.globalRefs, paths.slice(1));
    } else {
      mergePaths(this.globalRefs, paths);
    }
    this.commitRefs();
    this.renderRefs();
    this.flash(`全局参考图 ${this.globalRefs.length}/${MAX_REFS} 张`);
  }

  /** 段内参考图：点空槽补图，点已有槽换那一张 */
  async pickSegRefs(segIndex, slot) {
    const seg = this.segments[segIndex];
    if (!seg) return;
    if (!Array.isArray(seg.images)) seg.images = [];

    const replacing = Boolean(seg.images[slot]);
    const room = MAX_REFS - seg.images.length + (replacing ? 1 : 0);
    const paths = await this.askImages(Math.max(1, room));
    if (!paths.length) return;

    if (replacing) {
      seg.images[slot] = paths[0];
      mergePaths(seg.images, paths.slice(1));
    } else {
      mergePaths(seg.images, paths);
    }
    this.commit();
    this.renderList();
    this.flash(`第 ${segIndex + 1} 段参考图 ${seg.images.length} 张（覆盖全局）`);
  }

  /** 弹多选对话框。失败 / 取消都返回空数组，调用方不用管错误分支 */
  async askImages(max) {
    try {
      const res = await api.fetchApi("/dogking-tools/select_images", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ max }),
      });
      const data = await res.json();
      if (data?.error) {
        this.flash(data.error);
        return [];
      }
      return cleanPaths(data?.paths);
    } catch (err) {
      this.flash("选择参考图失败：" + err);
      return [];
    }
  }

  /** 只写「参考图数据」这一个 widget */
  commitRefs() {
    this.writeWidget("参考图数据", JSON.stringify({
      short: Number(this.refShort) || 0,
      images: this.globalRefs.slice(0, MAX_REFS),
    }));
  }

  /**
   * 当前界面这一套参数的「项目指纹」：`音频文件名|段数`。
   *
   * 必须和后端 `execution_signature()` 算出完全一样的串 —— 所以只取文件名和段数，
   * **不带浮点时间**：Python 的 `%.3f` 和 JS 的 `toFixed` 在 x.xxx5 这种边界上
   * 会差一位，那会让自动连续永远对不上、永远排不上队。
   */
  executionSignature() {
    const path = String(this.readWidget("文件路径") || "").trim().replace(/^"+|"+$/g, "");
    const base = path ? path.split(/[\\/]/).pop() : "";
    return `${base}|${this.segments.length}`;
  }

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
        // 换音频：选区重置为整段，默认整段一刀，用户再拖边界裁剪 / 点「自动等分」
        this.range = { start: 0, end: this.duration };
        this.segments = this.duration > 0
          ? [{ start: 0, end: this.duration, prompt: this.segments[0]?.prompt || "" }]
          : [];
        this.active = 0;
        this.syncActiveWidget();
        this.commit();
      } else {
        // 同一音频重新载入：只把超出新时长的段裁掉
        this.range = {
          start: Math.max(0, Math.min(this.range.start, this.duration)),
          end: Math.min(this.duration, this.range.end || this.duration),
        };
        this.segments = this.segments
          .map((s) => ({ ...s, end: Math.min(s.end, this.duration) }))
          .filter((s) => s.end - s.start > MIN_GAP);
        this.commit();
      }
      this.renderList();
      this.paintTicks();
      this.updateRangeLabel();
      this.updatePlayButton();
      this.updateExecInfo();
      // 换音频后播放位置归零；同一音频重载则保留原位置（够不到就夹回总长）
      this.updateProgress(Math.min(this.audioEl.currentTime || 0, this.duration));
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
      this.updateProgress();
      if (this.playing < 0) return;
      const seg = this.segments[this.playing];
      if (!seg) return;
      if (this.audioEl.currentTime >= seg.end) this.stopPlay();
    });
    this.audioEl.addEventListener("ended", () => this.stopPlay());
  }

  /* ---------------- 播放进度条 ---------------- */

  bindProgress() {
    const track = this.progTrack;
    const at = (e) => {
      const rect = track.getBoundingClientRect();
      const x = Math.min(Math.max(0, e.clientX - rect.left), rect.width);
      return (x / Math.max(1, rect.width)) * (this.duration || 0);
    };

    let dragging = false;
    track.addEventListener("pointerdown", (e) => {
      e.stopPropagation();
      if (!this.duration) return;
      dragging = true;
      track.setPointerCapture?.(e.pointerId);
      this.seekTo(at(e));
      e.preventDefault();
    });
    track.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      e.stopPropagation();
      this.seekTo(at(e));
    });
    const stopSeek = (e) => {
      if (!dragging) return;
      dragging = false;
      try {
        track.releasePointerCapture?.(e.pointerId);
      } catch (err) {
        /* 已释放 */
      }
    };
    track.addEventListener("pointerup", stopSeek);
    track.addEventListener("pointercancel", stopSeek);
  }

  /** 跳到某个时间；正在放就接着放。顺便把光标挪过去，「加切点」就跟着这里 */
  seekTo(time) {
    const t = Math.min(Math.max(0, Number(time) || 0), this.duration || 0);
    this.cursor = t;
    try {
      this.audioEl.currentTime = t;
    } catch (err) {
      /* 音频还没加载好，忽略；fill 仍然按 t 画 */
    }

    // 跳到哪一段，就把选中/试听状态切到那一段 —— 否则进度条在响第 5 段，
    // 高亮还停在第 2 段，看着就是错的
    const idx = this.segments.findIndex((s) => t >= s.start && t < s.end);
    if (idx >= 0 && (idx !== this.active || (this.playing >= 0 && idx !== this.playing))) {
      this.active = idx;
      if (this.playing >= 0) this.playing = idx;
      this.writeWidget("当前段", this.active + 1);
      this.updatePlayButton();
      this.paintPlaying();
      this.draw();
    }

    this.updateProgress(t);
    this.draw();
  }

  /** 刷新进度条填充与「当前 / 总长」文字 */
  updateProgress(pos) {
    if (!this.progFill || !this.progLabel) return;
    const dur = this.duration || 0;
    const cur = Math.min(Math.max(0, pos === undefined || pos === null ? this.audioEl.currentTime || 0 : pos), dur);
    const pct = dur > 0 ? (cur / dur) * 100 : 0;
    this.progFill.style.width = `${pct.toFixed(2)}%`;
    if (this.progKnob) this.progKnob.style.left = `${pct.toFixed(2)}%`;
    this.progLabel.textContent = `${fmtTime(cur)} / ${fmtTime(dur)}`;
  }

  /** 进度条上的切点刻度：和波形上的切点一一对应（首尾不画，贴边看不出来） */
  paintTicks() {
    if (!this.progTicks) return;
    this.progTicks.textContent = "";
    const dur = this.duration || 0;
    if (!dur) return;
    this.boundaries().forEach((t, i) => {
      if (i === 0 || t >= dur - 1e-6) return;
      const tick = document.createElement("div");
      tick.className = "dk-as-tick";
      tick.style.left = `${(t / dur) * 100}%`;
      this.progTicks.appendChild(tick);
    });
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
      this.updateProgress();
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
    this.updateExecInfo();
    this.updateProgress(seg.start);
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
    this.updateExecInfo();
    this.updateProgress();
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

  /** 斜纹填充：盖在选区外，明确表示「这一段不要」 */
  hatch(ctx, x, y, w, h) {
    if (w <= 1 || h <= 0) return;
    ctx.save();
    ctx.beginPath();
    ctx.rect(x, y, w, h);
    ctx.clip();
    ctx.strokeStyle = "rgba(255, 255, 255, 0.075)";
    ctx.lineWidth = 1;
    for (let i = -h; i < w + h; i += 9) {
      ctx.beginPath();
      ctx.moveTo(x + i, y + h);
      ctx.lineTo(x + i + h, y);
      ctx.stroke();
    }
    ctx.restore();
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
      if (i % 2 === 1) {
        const x1 = toX(seg.start);
        ctx.fillStyle = "rgba(255, 255, 255, 0.045)";
        ctx.fillRect(x1, plotTop, Math.max(1, toX(seg.end) - x1), plotH);
      }
    });

    // ---- 波形：统一中性蓝；选中/试听靠后面的遮罩和着色区分
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

    // ---- 选中 / 试听底色 + 顶部状态条 + 底部段条
    segs.forEach((seg, i) => {
      const x1 = toX(seg.start);
      const x2 = toX(seg.end);
      const sw = Math.max(1, x2 - x1);
      const isActive = i === this.active;
      const isPlaying = i === this.playing;
      if (isActive && !isPlaying) {
        ctx.fillStyle = "rgba(96, 150, 255, 0.26)";
        ctx.fillRect(x1, plotTop, sw, plotH);
      }
      if (isPlaying) {
        ctx.fillStyle = "rgba(63, 211, 154, 0.30)";
        ctx.fillRect(x1, plotTop, sw, plotH);
      }
      if (isActive || isPlaying) {
        ctx.fillStyle = isPlaying ? "#3fd39a" : "#6ea8ff";
        ctx.fillRect(x1 + 1, plotTop, Math.max(0, sw - 2), 3);
      }
      ctx.fillStyle = isPlaying
        ? "#3fd39a"
        : isActive
          ? "#6ea8ff"
          : i % 2 === 0
            ? "rgba(120, 150, 210, 0.55)"
            : "rgba(120, 150, 210, 0.3)";
      ctx.fillRect(x1 + 1, plotTop + plotH + 3, Math.max(0, sw - 2), 4);
    });

    // ---- 遮罩层：这是「选区」和「选中段」最直观的表达
    //   ① 选区外：重压暗 + 斜纹 = 这段不要，不会参与分段
    //   ② 选区内、非选中段：轻压暗 = 只有选中那一段是亮的
    const rng = this.range;
    const hasSel = rng && rng.end - rng.start >= MIN_GAP;
    const selX1 = toX(hasSel ? rng.start : 0);
    const selX2 = toX(hasSel ? rng.end : this.duration);
    if (hasSel) {
      ctx.fillStyle = "rgba(6, 8, 12, 0.8)";
      if (selX1 > 0.5) ctx.fillRect(0, plotTop, selX1, plotH);
      if (selX2 < w - 0.5) ctx.fillRect(selX2, plotTop, w - selX2, plotH);
      this.hatch(ctx, 0, plotTop, selX1, plotH);
      this.hatch(ctx, selX2, plotTop, w - selX2, plotH);
    }
    if (segs.length > 1) {
      ctx.fillStyle = "rgba(6, 8, 12, 0.38)";
      segs.forEach((seg, i) => {
        if (i === this.active || i === this.playing) return;
        const x1 = toX(seg.start);
        ctx.fillRect(x1, plotTop, Math.max(1, toX(seg.end) - x1), plotH);
      });
    }

    // ---- 选中段的亮边框：压暗之后靠这一圈让它"浮"出来
    segs.forEach((seg, i) => {
      const isPlaying = i === this.playing;
      if (i !== this.active && !isPlaying) return;
      const x1 = toX(seg.start);
      const x2 = toX(seg.end);
      ctx.strokeStyle = isPlaying ? "#3fd39a" : "#8ab6ff";
      ctx.lineWidth = 2;
      ctx.strokeRect(x1 + 1, plotTop + 1, Math.max(1, x2 - x1) - 2, plotH - 2);
    });

    // ---- 段号 chip + 段长（画在遮罩之上，压暗的段也读得清）
    segs.forEach((seg, i) => {
      const x1 = toX(seg.start);
      const x2 = toX(seg.end);
      const sw = Math.max(1, x2 - x1);
      const isActive = i === this.active;
      const isPlaying = i === this.playing;
      const drawChip = sw > 16 || isActive || isPlaying;
      if (!drawChip) return;

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

      if (sw > 62) {
        const len = seg.end - seg.start;
        ctx.font = "10px ui-monospace, Consolas, monospace";
        ctx.fillStyle = isPlaying ? "rgba(200, 255, 232, 0.95)" : "rgba(214, 226, 245, 0.85)";
        ctx.textAlign = "right";
        ctx.textBaseline = "bottom";
        ctx.fillText(`${len.toFixed(len < 10 ? 2 : 1)}s`, Math.max(x1 + 4, x2 - 9), plotTop + plotH - 2);
      }
    });

    // ---- 切点线 + 抓手。最左/最右那两条同时是**选区边界**：青色 + 顶部小方块
    const edges = this.boundaries();
    edges.forEach((t, b) => {
      const isEdge = b === 0 || b === edges.length - 1;
      const x = toX(t);
      const hot = this.drag ? this.drag.b === b : this.hoverB === b;
      ctx.strokeStyle = hot
        ? isEdge
          ? "#7ff0dd"
          : "#a9c9ff"
        : isEdge
          ? "rgba(111, 216, 194, 0.95)"
          : "rgba(233, 240, 252, 0.72)";
      ctx.lineWidth = hot ? 2.5 : isEdge ? 2 : 1.5;
      ctx.beginPath();
      ctx.moveTo(x, plotTop);
      ctx.lineTo(x, plotTop + plotH);
      ctx.stroke();

      if (isEdge) {
        // 顶部小方块 = 裁剪边界的抓手（中间那排竖条是切点抓手，两种形状分开认）
        ctx.fillStyle = hot ? "#7ff0dd" : "#6fd8c2";
        this.roundRect(ctx, x - 4, plotTop - 1, 8, 14, 2);
        ctx.fill();
      } else {
        ctx.fillStyle = hot ? "#a9c9ff" : "rgba(219, 232, 252, 0.9)";
        this.roundRect(ctx, x - 3, midY - 11, 6, 22, 3);
        ctx.fill();
        ctx.fillStyle = "rgba(20, 26, 36, 0.85)";
        ctx.fillRect(x - 1, midY - 6, 2, 12);
      }
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
        ? "还没有分段：拖波形最左/最右那两条青线框出需要的范围，再点「自动等分」"
        : "选一个音频开始";
      this.listEl.appendChild(hint);
      this.paintTicks();
      this.updateExecInfo();
      return;
    }

    this.segments.forEach((seg, i) => {
      if (!Array.isArray(seg.images)) seg.images = [];
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

      // 段长单独一列：4 分半的音频上，光看起止时间是算不出「这段几秒」的
      const durEl = document.createElement("span");
      durEl.className = "dk-as-dur";
      const secs = Math.max(0, seg.end - seg.start);
      durEl.textContent = `${secs.toFixed(secs < 10 ? 2 : 1)}s`;
      durEl.title = `这一段 ${secs.toFixed(2)} 秒`;

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

      // 本段参考图：有图就显示张数并标绿（此时整组覆盖全局），没图显示「＋图」
      const refBtn = document.createElement("button");
      refBtn.className = "dk-as-refbtn" + (seg.images.length ? " dk-as-refbtn--on" : "");
      refBtn.textContent = seg.images.length ? `图${seg.images.length}` : "＋图";
      refBtn.title = seg.images.length
        ? `本段用自己这 ${seg.images.length} 张参考图（整组覆盖全局）。点一下管理`
        : "给这一段单独指定参考图；不设就沿用全局";
      refBtn.addEventListener("pointerdown", (e) => e.stopPropagation());
      refBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        this.refOpen = this.refOpen === i ? -1 : i;
        this.renderList();
      });

      row.append(idx, time, durEl, input, refBtn, del);
      this.listEl.appendChild(row);
      this.rows.push({ row, idx, time });

      if (this.refOpen === i) {
        this.listEl.appendChild(this.buildSegRefRow(i, seg));
      }
    });
    this.paintTicks();
    this.updateExecInfo();
  }

  /**
   * 段内参考图子行：展开在所属段行下面。
   *
   * 单独一行而不是塞进段行里：段行本来就挤（编号/时间/时长/提示词/图/删除），
   * 再塞 6 个 42px 的缩略图会把提示词框压没。
   */
  buildSegRefRow(index, seg) {
    const wrap = document.createElement("div");
    wrap.className = "dk-as-refsub";

    const head = document.createElement("div");
    head.className = "dk-as-refsub-head";

    const note = document.createElement("span");
    note.className = "dk-as-refsub-note";
    note.textContent = seg.images.length
      ? `本段 ${seg.images.length} 张，整组覆盖全局（全局那 ${this.globalRefs.length} 张不参与）`
      : "本段没设 → 沿用全局参考图";
    head.appendChild(note);

    if (seg.images.length) {
      const clear = document.createElement("button");
      clear.textContent = "清空本段";
      clear.title = "清掉本段的图，回到沿用全局";
      clear.addEventListener("pointerdown", (e) => e.stopPropagation());
      clear.addEventListener("click", (e) => {
        e.stopPropagation();
        seg.images = [];
        this.commit();
        this.renderList();
      });
      head.appendChild(clear);
    }

    wrap.appendChild(head);
    wrap.appendChild(this.buildRefGrid(seg.images, {
      small: true,
      onPick: (slot) => this.pickSegRefs(index, slot),
      onClear: (slot) => {
        seg.images.splice(slot, 1);
        this.commit();
        this.renderList();
      },
    }));
    return wrap;
  }

  /** 只改选中/试听高亮，不重建 DOM —— 重建会把用户正在输入的提示词冲掉 */
  paintPlaying() {
    this.rows.forEach((ref, i) => {
      const isPlaying = i === this.playing;
      ref.row.classList.toggle("dk-as-row--playing", isPlaying);
      ref.row.classList.toggle("dk-as-row--active", i === this.active);
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
      // 选区单独存一份：清空分段后它还在，重新等分时继续用
      range: { start: Number(this.range.start.toFixed(3)), end: Number(this.range.end.toFixed(3)) },
      segments: this.segments.map((s) => {
        const item = {
          start: Number(s.start.toFixed(3)),
          end: Number(s.end.toFixed(3)),
          prompt: String(s.prompt || ""),
        };
        // 空数组不写进 JSON：没有自定义图的段占绝大多数，全写成 "images":[]
        // 只是把整张表撑大一截，老版本节点读到这个字段也当没看见
        const images = cleanPaths(s.images);
        if (images.length) item.images = images;
        return item;
      }),
    };
    this.writeWidget("分段数据", JSON.stringify(data));
  }

  restore() {
    const raw = this.readWidget("分段数据");
    this.segments = readSegments(raw);
    const saved = readRange(raw);
    if (saved) {
      this.range = saved;
    } else if (this.segments.length) {
      this.range = { start: this.segments[0].start, end: this.segments[this.segments.length - 1].end };
    }

    // 全局参考图 + 短边限制。老工作流没这个字段时读出来是「0 张 + 1024」，
    // 面板照常打开、只是没有图。
    const refCfg = readRefConfig(this.readWidget("参考图数据"));
    this.globalRefs = refCfg.images;
    this.refShort = refCfg.short;
    this.refOpen = -1;

    // 全局提示词：原生 widget 已经藏起来了，值从这儿搬到面板里那个输入框
    if (this.globalPromptEl && document.activeElement !== this.globalPromptEl) {
      this.globalPromptEl.value = this.readWidget("全局提示词") || "";
    }

    this.normalizeSegments();
    this.updateRangeLabel();
    // 选中段必须以「当前段」widget 为准 —— 那个值才是执行时后端真正用的。
    // 以前这里直接沿用内存里的 this.active（初始 0），于是打开工作流后面板亮着
    // 第 1 段、实际却输出第 8 段，看着就像「选哪段都一样」。
    const cur = Math.round(Number(this.readWidget("当前段")));
    if (Number.isFinite(cur) && cur >= 1) this.active = cur - 1;
    this.active = Math.max(0, Math.min(this.active, this.segments.length - 1));
    this.syncActiveWidget();   // widget 存了个越界段号（改过切点/选区）就顺手夹回来
    this.updatePlayButton();
    this.renderRefs();
    this.renderList();
    this.resize();
    if (this.readWidget("文件路径")) this.loadPeaks(false);
  }

  /**
   * 浮层提示：写在自己那一行，4 秒后自动收起来。
   *
   * 以前直接写 this.nameEl —— 那个元素显示的是「文件名 · 时长」，
   * 点一次「回到第 1 段」就把文件名换成提示语、之后再也回不来，
   * 面板一窄还被截成「已回到第_」。提示归提示，信息归信息。
   */
  flash(text) {
    const line = String(text || "").trim();
    if (!this.flashEl) return;
    if (this._flashTimer) clearTimeout(this._flashTimer);
    this.flashEl.textContent = line;
    this.flashEl.style.display = line ? "block" : "none";
    if (line) {
      this._flashTimer = setTimeout(() => {
        this.flashEl.textContent = "";
        this.flashEl.style.display = "none";
      }, 4000);
    }
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

        // 内部字段（分段数据 / 参考图数据）：值照常提交，但节点上不显示大文本框。
        //
        // 🔴 只改 `type` 是不够的：这两个都是 multiline STRING，ComfyUI 会按**内容长度**
        // 算控件高度。分段表是一整串 JSON，6 段就 400 多字符，按默认宽度折行能折出
        // 十几行 —— 节点顶部凭空多出 366px 空白，面板被挤到最底下。
        // 三条都要给：computeSize 管老逻辑，computeLayoutSize 是 1.58 的新钩子，
        // options.multiline 断掉「按行数算高度」这条路。
        for (const name of ["分段数据", "参考图数据", "全局提示词"]) {
          const w = (node.widgets || []).find((x) => x.name === name);
          if (!w) continue;
          w.type = "dogkingHidden";
          if (w.options) w.options.multiline = false;
          w.computeSize = (width) => [width || 0, 0];
          w.computeLayoutSize = () => ({ minHeight: 0, minWidth: 0 });
          w.serializeValue = () => w.value || "";
        }

        const mount = document.createElement("div");
        node.addDOMWidget("音频分段", "dkAudioSplit", mount, {
          serialize: false,
          hideOnZoom: false,
          // 🔴 必须是常量。动态 getHeight（按节点高度补差值）会和拖拽缩放形成
          // 反馈：拖动高度 → getHeight 变 → DOM 重排 → panelTopNode 量到过期值
          // → onResize 的钳制把高度顶回去 → 拖拽失效、节点卡死在巨大高度。
          getHeight: () => PANEL_HEIGHT,
          getValue: () => "",
          setValue: () => {},
        });

        node.dkAudio = new AudioSplitPanel(node, mount);

        // 缩放节点时只标脏重绘（顶部的参考图格子要按新尺寸重排）。
        // 🔴 这里绝不能改 node.size —— 拖拽过程中 panelTopNode 量到的是上一帧
        // 的 DOM 位置，拿它钳制高度会让「高度不跟随鼠标」。
        const prevOnResize = node.onResize;
        node.onResize = function () {
          this.setDirtyCanvas?.(true, true);
          prevOnResize?.apply(this, arguments);
        };

        // 改「输出模式」/「当前段」时让面板跟上：
        // 模式影响状态行文案，当前段影响选中高亮 —— 不然要等下一次执行才刷新
        const hook = (name, fn) => {
          const w = (node.widgets || []).find((x) => x.name === name);
          if (!w) return;
          const prev = w.callback;
          w.callback = function () {
            const r = prev?.apply(this, arguments);
            try {
              fn(node, arguments[0]);
            } catch (err) {
              /* 面板已销毁之类的，忽略 */
            }
            return r;
          };
        };
        hook("输出模式", (n) => n.dkAudio?.updateExecInfo());
        hook("当前段", (n, value) => {
          const p = n.dkAudio;
          if (!p) return;
          const idx = Math.max(0, Math.round(Number(value) || 1) - 1);
          if (idx !== p.active) p.setActive(idx, false);
        });
        return ret;
      };

      // 打开已有工作流：DOM widget 里的内容不会自动回来，重新读一遍 widget
      const onConfigure = nodeType.prototype.onConfigure;
      nodeType.prototype.onConfigure = function () {
        const r = onConfigure?.apply(this, arguments);
        const node = this;
        // 旧工作流保存时输出口只有 12 个（那时还没有参考图）。ComfyUI 恢复是照着
        // 存下来的 outputs 建的，不会按后端 RETURN_TYPES 自动补 —— 这里补齐，
        // 追加在尾部所以已有连线全都不动。
        ensureOutputs(node, nodeData);
        setTimeout(() => {
          if (node.dkAudio) node.dkAudio.restore();
          // 面板加了参考图区、输出口多了 6 个，老工作流存的节点高度可能装不下，
          // 内容会被裁在节点框外面。这里按实际需要补高 —— 只增不减，
          // 免得把用户手动拉大的尺寸又缩回去。
          try {
            const need = node.computeSize();
            if (need[1] > node.size[1]) node.setSize([node.size[0], need[1]]);
          } catch (err) {
            /* 布局算不出来就保持原样，不该因为补高度把工作流打不开 */
          }
        }, 0);
        return r;
      };

      // 顺序模式：后端说「还有下一段」就自动排队
      const onExecuted = nodeType.prototype.onExecuted;
      nodeType.prototype.onExecuted = function (message) {
        onExecuted?.apply(this, arguments);
        // 标题栏 badge 已经去掉：那几个数字放在节点顶部又小又挡事，
        // 现在改到面板里的输出状态行（14px），空间大、还能顺手点「回到第 1 段」。
        const idx = message?.dogking_index?.[0];
        if (node_has_panel(this)) {
          if (Number.isFinite(idx)) this.dkAudio.setActive(Number(idx) - 1, false);
          // 记下这一轮真正用的参数指纹，自动连续排队前要拿它和界面比对
          const sig = message?.dogking_sig?.[0];
          if (typeof sig === "string") this.dkAudio.lastSig = sig;
          this.dkAudio.updateExecInfo();
        }
        this.setDirtyCanvas?.(true, true);
      };
    },
  });

  function node_has_panel(node) {
    return Boolean(node?.dkAudio);
  }

  /** 按 unique_id 找节点（EVENT_NEXT 给的是节点 key，不是对象） */
  function findNodeByKey(key) {
    if (!key) return null;
    const nodes = app?.graph?._nodes || app?.rootGraph?._nodes || [];
    return nodes.find((n) => String(n.id) === String(key)) || null;
  }

  /**
   * 按节点定义补足输出口。
   *
   * 为什么需要：ComfyUI 恢复工作流时用**存下来的** outputs 数组建槽位，
   * 后端 RETURN_TYPES 后来多了 6 个图片口，老工作流不会自动多出来 ——
   * 表现就是「后端说 18 个口，节点上只看到 12 个，图片根本接不上」。
   * addOutput 只在尾部追加，已有连线（下标 0~11）不受影响。
   */
  function ensureOutputs(node, nodeData) {
    const types = nodeData?.output;
    if (!node || !Array.isArray(types) || !Array.isArray(node.outputs)) return;
    if (node.outputs.length >= types.length) return;
    const names = nodeData.output_name || [];
    while (node.outputs.length < types.length) {
      const i = node.outputs.length;
      node.addOutput(String(names[i] || `out${i}`), types[i] || "IMAGE", {});
    }
  }

  /**
   * 自动连续：后端每跑完一段发一次这个事件。
   *
   * 用独立事件名（`dogking-tools.audio.next`），不碰「提示词批量加载」的
   * `dogking-tools.next` —— 两个节点同时在工作流里时不会互相排队。
   */
  let pendingContinue = false;
  let pendingNodeKey = "";
  api.addEventListener(EVENT_NEXT, (e) => {
    pendingContinue = Boolean(e?.detail?.should_continue);
    pendingNodeKey = String(e?.detail?.node ?? "");
  });
  api.addEventListener("execution_success", () => {
    if (!pendingContinue) return;
    pendingContinue = false;

    // 🔴 自动连续提交的是**当前界面**的图，不是刚跑完的那一份。
    // 用户跑到一半换项目（换音频）或者重新等分，这时候再自动排，就会拿新参数
    // 去跑「上一段的下一段」—— 两个项目的段数、参考图、提示词全串在一起。
    // 所以先比对指纹：界面上还是刚跑的那一套才继续，否则停下来让人自己点。
    const panel = findNodeByKey(pendingNodeKey)?.dkAudio;
    if (panel && panel.lastSig && panel.executionSignature() !== panel.lastSig) {
      panel.flash("界面参数已改动（换了音频或重新分段），自动连续已暂停 —— 点运行继续");
      return;
    }

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
