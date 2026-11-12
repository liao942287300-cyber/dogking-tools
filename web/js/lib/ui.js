/**
 * dogking-tools —— UI 公共层（T02）
 *
 * 三个函数是从 `dogking-tools.js` **原样搬过来的**（旧节点还在用，逻辑一个字没改），
 * 搬家的目的只是让新节点和旧节点共用同一份实现，不再各写一遍。
 * 另外两个是本层新增的：隐藏 widget、注入面板样式。
 */

/** 面板高度（px）。和 CSS 里的 --comfy-widget-height 必须一致 */
export const PANEL_H = 600;
/** 节点 4 的图片输出口上限 */
export const MAX_PORTS = 8;

/* ---------------------------------------------------------------- 从旧文件搬来的 */

/**
 * 在节点底部画一条状态徽标（「第 3/12 张」之类）。
 *
 * @param {CanvasRenderingContext2D} ctx
 * @param {object} node
 * @param {string} text
 */
export function drawBadge(ctx, node, text) {
  ctx.save();
  ctx.font = "12px ui-monospace, Consolas, monospace";
  const w = ctx.measureText(text).width;
  const y = node.size[1] - 10;
  ctx.fillStyle = "rgba(47, 111, 237, 0.18)";
  if (typeof ctx.roundRect === "function") {
    ctx.beginPath();
    ctx.roundRect(10, y - 14, w + 16, 20, 6);
    ctx.fill();
  } else {
    ctx.fillRect(10, y - 14, w + 16, 20);
  }
  ctx.fillStyle = "#5b9bff";
  ctx.fillText(text, 18, y);
  ctx.restore();
}

/** 记住 widget 原始状态，隐藏时把 type 和高度一起塞掉 */
export function setWidgetVisible(node, widget, visible) {
  if (!widget) return;
  if (!widget.__dkOrig) {
    // origComputeSize 可能是 undefined（litegraph 默认不给 widget 这个方法），
    // 所以不能靠「有没有」来判断要不要恢复 —— 直接原样存、原样还。
    widget.__dkOrig = { type: widget.type, computeSize: widget.computeSize };
  }
  widget.type = visible ? widget.__dkOrig.type : "dogkingHidden";
  widget.computeSize = visible ? widget.__dkOrig.computeSize : () => [0, -4];
  node.setDirtyCanvas?.(true, true);
}

/**
 * 把 widget 挪到另一个 widget 正下方（按钮跟参数挨着才好看）
 *
 * @param {object} node
 * @param {object} widget 要移动的 widget
 * @param {object} anchor 目标位置（移动到这个 widget 后面）
 */
export function moveWidgetAfter(node, widget, anchor) {
  if (!widget || !anchor || !Array.isArray(node.widgets)) return;
  const wi = node.widgets.indexOf(widget);
  const ai = node.widgets.indexOf(anchor);
  if (wi < 0 || ai < 0 || wi === ai + 1) return;
  node.widgets.splice(wi, 1);
  node.widgets.splice(ai + 1, 0, widget);
}

/* ---------------------------------------------------------------- 新增 */

/**
 * 把 widget 变成「隐藏但仍然是数据」。
 *
 * 用 `type = 'hidden'` 而不是 `setWidgetVisible` 的 `'dogkingHidden'`：
 * `DOMWidgetImpl.computeLayoutSize()` 认的是 `type === 'hidden'`，
 * 命中后返回 `{minHeight:0, maxHeight:0}`，litegraph 原生把这块折叠成 0 高度。
 * 这样「收起面板」不需要我们自己算尺寸，也不会触发
 * `computeLayoutSize → computeSize → setSize → 高度` 那个反馈环。
 *
 * 为什么叫「asData」：widget 仍然留在 `node.widgets` 里，
 * `serialize` 时它的 value 照常写进工作流 —— 隐藏的是**显示**，不是数据。
 *
 * @param {object} widget
 */
export function hideWidgetAsData(widget) {
  if (!widget) return;
  if (widget.__dkTypeOrig === undefined) widget.__dkTypeOrig = widget.type;
  widget.type = "hidden";
}

/**
 * 恢复 `hideWidgetAsData` 收起来的 widget。
 * @param {object} widget
 */
export function showWidgetAsData(widget) {
  if (!widget) return;
  if (widget.__dkTypeOrig === undefined) return;
  widget.type = widget.__dkTypeOrig;
  delete widget.__dkTypeOrig;
}

/**
 * widget 当前是不是被收起来了（面板「收起/展开」按钮要据此切文案）
 * @param {object} widget
 * @returns {boolean}
 */
export function isWidgetHidden(widget) {
  return Boolean(widget && widget.type === "hidden");
}

/* ---------------------------------------------------------------- 样式注入 */

const STYLE_MARK = "data-dk-panel-style";

/** 面板样式（一次性注入到 document.head） */
const PANEL_CSS = `
/* 面板根：高度靠 CSS 变量 + getHeight 回调双保险（DOMWidgetImpl 两处都读） */
.dk-pv-panel {
  --comfy-widget-height: ${PANEL_H}px;
  --comfy-widget-min-height: ${PANEL_H}px;
  box-sizing: border-box;
  display: flex;
  flex-direction: column;
  width: 100%;
  height: ${PANEL_H}px;
  min-height: ${PANEL_H}px;
  background: var(--comfy-input-bg, #1d1e22);
  color: var(--input-text, #ddd);
  font: 12px/1.45 ui-sans-serif, system-ui, "Segoe UI", "Microsoft YaHei", sans-serif;
  border-radius: 6px;
  overflow: hidden;
  user-select: none;
}

/* ---------------- 工具条 ---------------- */
.dk-pv-toolbar {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-bottom: 1px solid rgba(255,255,255,.08);
  background: rgba(255,255,255,.02);
}
.dk-pv-crumb {
  flex: 0 1 auto;
  min-width: 0;
  max-width: 40%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  opacity: .85;
}
.dk-pv-search {
  flex: 1 1 auto;
  min-width: 90px;
  height: 24px;
  padding: 0 8px;
  border-radius: 5px;
  border: 1px solid rgba(255,255,255,.14);
  background: rgba(0,0,0,.28);
  color: inherit;
  font: inherit;
  outline: none;
}
.dk-pv-search:focus { border-color: #4a8cff; }
.dk-pv-count { flex: 0 0 auto; opacity: .7; white-space: nowrap; }

.dk-pv-btn {
  flex: 0 0 auto;
  /* 用户要求「按钮变大一些」：24px 高在 600px 面板里太小，点「确定」容易点空。
     统一抬到 32px + 最小宽度 72px，三个按钮等宽，看起来像一组操作。 */
  height: 32px;
  min-width: 72px;
  padding: 0 16px;
  font-size: 13px;
  border-radius: 5px;
  border: 1px solid rgba(255,255,255,.16);
  background: rgba(255,255,255,.06);
  color: inherit;
  font: inherit;
  cursor: pointer;
  white-space: nowrap;
}
.dk-pv-btn:hover:not(:disabled) { background: rgba(255,255,255,.12); }
.dk-pv-btn:disabled { opacity: .38; cursor: default; }
.dk-pv-btn.is-primary {
  border-color: #3a6fd8;
  background: #2f6fed;
  color: #fff;
  font-weight: 600;
}
.dk-pv-btn.is-primary:hover:not(:disabled) { background: #3d7bf5; }

/* ---------------- 列表区 ---------------- */
.dk-pv-scroll {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 8px;
  scrollbar-width: thin;
  /* 加载/空/错误遮罩是 absolute inset:0，必须有个定位祖先把它圈在列表区内，
     否则它会盖住底部的分页器和按钮 */
  position: relative;
}
.dk-pv-scroll::-webkit-scrollbar { width: 9px; }
.dk-pv-scroll::-webkit-scrollbar-thumb {
  background: rgba(255,255,255,.16);
  border-radius: 5px;
}

/* 网格：随节点宽度自适应，**不固定列数**（固定 8 列在 600px 高的节点里要 820px 宽） */
.dk-pv-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(96px, 1fr));
  gap: 8px;
  align-content: start;
}

/* 卡片 */
.dk-pv-card {
  position: relative;
  cursor: pointer;
  border-radius: 6px;
  padding: 3px;
  border: 2px solid transparent;
  background: rgba(255,255,255,.03);
  transition: border-color .12s, background .12s;
}
.dk-pv-card:hover { background: rgba(255,255,255,.08); }
.dk-pv-card.is-selected {
  border-color: #4a8cff;
  background: rgba(74,140,255,.16);
}
/* 固定 96×96 盒子：图片到达前后布局不跳，不触发 reflow 雪崩。
   ⚠️ 用 contain 不是 cover —— cover 会把非正方形的图裁掉边缘
   （竖图裁上下、横图裁左右），用户看到的就是「图片不完整、被容器切了」。
   留白换来「整张图都能看到」，选图场景下这个更重要。 */
.dk-pv-thumb {
  position: relative;
  width: 100%;
  aspect-ratio: 1 / 1;
  min-height: 96px;
  max-height: 96px;
  border-radius: 4px;
  overflow: hidden;
  background: rgba(255,255,255,.06);
}
.dk-pv-thumb img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: contain;   /* 完整显示，不裁切 */
  opacity: 0;
  transition: opacity .18s;
}
.dk-pv-thumb img.is-loaded { opacity: 1; }
/* 加载失败 / 还没到 → 灰块 + 一个淡淡的图标字符，绝不出现裂图 */
.dk-pv-thumb.is-broken::after,
.dk-pv-thumb.is-pending::after {
  content: "🖼";
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
  opacity: .28;
}
.dk-pv-name {
  margin-top: 3px;
  font-size: 11px;
  line-height: 1.3;
  text-align: center;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  opacity: .82;
}
/* 选中角标 */
.dk-pv-check {
  position: absolute;
  top: 6px;
  right: 6px;
  width: 17px;
  height: 17px;
  border-radius: 50%;
  background: #4a8cff;
  color: #fff;
  font-size: 11px;
  line-height: 17px;
  text-align: center;
  display: none;
  box-shadow: 0 1px 4px rgba(0,0,0,.45);
}
.dk-pv-card.is-selected .dk-pv-check { display: block; }
/* 顺序号（多选时告诉用户「第几个被选的」，输出口顺序就按它） */
.dk-pv-order {
  position: absolute;
  top: 6px;
  left: 6px;
  min-width: 17px;
  height: 17px;
  padding: 0 4px;
  border-radius: 9px;
  background: rgba(0,0,0,.62);
  color: #fff;
  font-size: 10px;
  line-height: 17px;
  text-align: center;
  display: none;
}
.dk-pv-card.is-selected .dk-pv-order { display: block; }

/* ---------------- 卡片视图（词库） ---------------- */
.dk-pv-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 8px;
  align-content: start;
}
.dk-pv-tcard {
  position: relative;
  display: flex;
  flex-direction: column;
  min-height: 110px;
  max-height: 110px;
  padding: 7px 9px;
  border-radius: 6px;
  border: 2px solid transparent;
  background: rgba(255,255,255,.03);
  cursor: pointer;
  overflow: hidden;
}
.dk-pv-tcard:hover { background: rgba(255,255,255,.08); }
.dk-pv-tcard.is-selected {
  border-color: #4a8cff;
  background: rgba(74,140,255,.16);
}
.dk-pv-tcard-title {
  flex: 0 0 auto;
  font-size: 12px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.dk-pv-tcard-rule {
  flex: 0 0 auto;
  height: 1px;
  margin: 5px 0;
  background: rgba(255,255,255,.12);
}
/* 正文前 3 行（-webkit-box 是 Chromium 里做多行省略最稳的方式） */
.dk-pv-tcard-body {
  flex: 1 1 auto;
  min-height: 0;
  font-size: 11px;
  line-height: 1.45;
  opacity: .8;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  word-break: break-word;
}
.dk-pv-tcard-body.is-empty { opacity: .45; font-style: italic; }
.dk-pv-tcard-meta {
  flex: 0 0 auto;
  align-self: flex-end;
  font-size: 10px;
  opacity: .5;
}

/* ---------------- 骨架 / 空 / 错误 ---------------- */
.dk-pv-skel {
  border-radius: 4px;
  min-height: 96px;
  max-height: 96px;
  background: linear-gradient(90deg, rgba(255,255,255,.05), rgba(255,255,255,.11), rgba(255,255,255,.05));
  background-size: 200% 100%;
  animation: dk-pv-shimmer 1.1s linear infinite;
}
@keyframes dk-pv-shimmer {
  from { background-position: 200% 0; }
  to   { background-position: -200% 0; }
}

.dk-pv-state {
  position: absolute;
  inset: 0;
  display: none;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 16px;
  text-align: center;
  background: rgba(20,21,24,.86);
  z-index: 3;
}
.dk-pv-state.is-open { display: flex; }
.dk-pv-state-msg { max-width: 90%; opacity: .82; word-break: break-word; }
.dk-pv-state-msg.is-error { color: #ff8f8f; }

/* ---------------- 底部 ---------------- */
.dk-pv-footer {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-top: 1px solid rgba(255,255,255,.08);
  background: rgba(255,255,255,.02);
}
.dk-pv-pager {
  display: flex;
  align-items: center;
  gap: 4px;
}
.dk-pv-pageinfo {
  min-width: 62px;
  text-align: center;
  opacity: .8;
  white-space: nowrap;
}
.dk-pv-pagesize {
  height: 24px;
  padding: 0 4px;
  border-radius: 5px;
  border: 1px solid rgba(255,255,255,.16);
  background: rgba(0,0,0,.28);
  color: inherit;
  font: inherit;
}
.dk-pv-selcount {
  margin-left: auto;
  opacity: .8;
  white-space: nowrap;
}
.dk-pv-actions { display: flex; gap: 10px; }
`;

/**
 * 注入面板样式（幂等：靠 `data-dk-panel-style` 标记防止重复注入）。
 *
 * 为什么用一次性注入而不是给每个元素写内联样式：
 * 一个面板就有几十个元素（40 张卡 × 3 层），内联样式既写不下也没法做
 * hover / 选中态这些伪类。
 *
 * @returns {HTMLStyleElement} 注入的 style 元素
 */
export function injectPanelStyles() {
  const existing = document.querySelector(`style[${STYLE_MARK}]`);
  if (existing) return existing;
  const style = document.createElement("style");
  style.setAttribute(STYLE_MARK, "1");
  style.textContent = PANEL_CSS;
  document.head.appendChild(style);
  return style;
}

export default {
  PANEL_H,
  MAX_PORTS,
  drawBadge,
  setWidgetVisible,
  moveWidgetAfter,
  hideWidgetAsData,
  showWidgetAsData,
  isWidgetHidden,
  injectPanelStyles,
};
