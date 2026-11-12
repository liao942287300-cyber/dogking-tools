/**
 * dogking-tools —— 节点内嵌面板的「宿主」（T03/T04 共用）
 *
 * 为什么单独一个文件：4 个 PV 节点都要做同样四件事 ——
 *   ① 用 `addDOMWidget` 挂一个 600px 的面板并给对高度；
 *   ② 收起/展开（`type='hidden'` 原生折叠）；
 *   ③ 执行后把状态条画到节点底部；
 *   ④ 恢复工作流时只渲染占位条、不发请求。
 * 复制 4 遍必然某一份忘了某条（尤其是 ② 用错成 `dogkingHidden` 就静默失效）。
 *
 * ⚠️ 高度只能给固定值：
 *    `getHeight: () => node.size[1] - 130` 这种「跟着节点高度走」的写法会形成
 *    `computeLayoutSize → computeSize → setSize → widget 高度` 的反馈环，
 *    实测节点会一路涨到爆。这里永远返回 PANEL_H。
 */

import { PvPanel } from "../lib/panel.js";
import {
  PANEL_H,
  drawBadge,
  hideWidgetAsData,
  showWidgetAsData,
  isWidgetHidden,
} from "../lib/ui.js";

/** 节点底部给状态条留的高度（不留的话 drawBadge 会被面板盖住） */
export const STATUS_BAR_H = 28;
/** 面板节点的最小宽度：600px 高的面板里 8 列 96px 网格需要 ~800px */
export const PANEL_MIN_W = 760;
/**
 * 面板高度下限（用户把节点拖小时最多缩到这么高）。
 * 默认高度用 ui.js 的 PANEL_H（600）。
 */
export const PANEL_MIN_H = 180;
/** 面板高度上限（兜底，防止任何残余反馈环把节点无限拉长） */
export const PANEL_MAX_H = 2400;

/**
 * 给节点装一个内嵌面板。
 *
 * @param {object} node litegraph 节点
 * @param {object} opts
 *   kind:      'image' | 'text' | 'asset'
 *   view:      'grid' | 'card'
 *   pageSize:  number
 *   maxSelect: number
 *   getMode:   () => 'single' | 'multi' | 'random'   每次重建面板时调
 *   getFilters:() => ({addr, category, subCategory, project, textType, crumb})
 *   onConfirm: (selected) => void                    **写回**：实时确认，任何选中变化都调
 *   onChange:  (selected) => void                    选中集变了（只更新文案，别写 widget）
 * @returns {object} 宿主句柄
 */
export function createPanelHost(node, opts) {
  const options = opts || {};
  const el = document.createElement("div");
  el.style.width = "100%";

  // 面板高度是**可变的**：用户拖节点右下角放大时跟着变高（见下面的 onResize）。
  // 初始值 PANEL_H（600）。
  node.__dkPanelH = PANEL_H;

  /**
   * 应用面板高度。
   *
   * ⚠️ 必须同时改 **inline 的 height 和 min-height** ——
   * `.dk-pv-panel` 的 CSS 写死了 `height:600px; min-height:600px`，
   * 只改 height 会被 min-height 顶回去。
   */
  function applyPanelHeight(h, force = false) {
    // 上限是兜底：正常情况下不会到，但万一还有别的路径形成环，
    // 宁可面板不跟随也不要让节点无限拉长（用户很难手动拖回来）。
    const next = Math.min(PANEL_MAX_H, Math.max(PANEL_MIN_H, Math.round(h)));
    if (!force && Math.abs(next - node.__dkPanelH) < 2) return;
    node.__dkPanelH = next;
    el.style.height = `${next}px`;
    el.style.minHeight = `${next}px`;
    el.style.setProperty("--comfy-widget-height", `${next}px`);
    el.style.setProperty("--comfy-widget-min-height", `${next}px`);
    // 🔴 真正决定**视觉高度**的是面板根节点：`.dk-pv-panel` 的 CSS 里写死了
    //    `height:600px; min-height:600px`，只改外层 el 的话里面还是 600，
    //    表现就是「变量变了但面板没变大」。
    try {
      const root = state.panel && state.panel.root;
      if (root) {
        root.style.height = `${next}px`;
        root.style.minHeight = `${next}px`;
      }
    } catch (err) { /* 面板还没建，忽略 */ }
    node.setDirtyCanvas?.(true, true);
  }

  // 初始 inline 高度（压过 CSS 的固定 600）
  el.style.height = `${PANEL_H}px`;
  el.style.minHeight = `${PANEL_H}px`;
  // 两条路一起声明高度：DOMWidgetImpl.computeLayoutSize() 会先看 getHeight 回调，
  // 再看这两个 CSS 变量。少给一边，某些版本会退回「内容高度」把面板压扁。
  el.style.setProperty("--comfy-widget-height", `${PANEL_H}px`);
  el.style.setProperty("--comfy-widget-min-height", `${PANEL_H}px`);

  const widget = node.addDOMWidget("面板", "dkpvpanel", el, {
    serialize: false,   // 面板不是数据，别写进工作流
    // 🔴 必须是 false：`hideOnZoom: true` 时 ComfyUI 前端这样做（GraphView-*.js）：
    //      r.visible = ... && !(n.options.hideOnZoom && t)   // t = 画布缩得太小
    //    一旦 visible=false，DOM 面板整体消失，只剩 canvas 画的灰块 ——
    //    表现就是「画布缩小到某个比例，图片突然全没了，放大又回来」。
    //    改 false 后无论画布缩多小，面板始终可见。
    hideOnZoom: false,
    // 🔴 读 `node.__dkPanelH`，**绝不能读 `node.size[1]`** ——
    //    computeLayoutSize → computeSize → setSize 会形成反馈环，高度一路涨到爆。
    //    这里只读一个由 onResize 单向写入的变量，环断开了。
    getHeight: () => node.__dkPanelH,
    getMinHeight: () => node.__dkPanelH,
    getValue: () => "",
    setValue: () => {},
  });

  /* ------------------------------------------------- 布局下限（缩不回去的真正原因）
   *
   * ComfyUI 前端 1.58 的拖拽改大小是这样夹的（treeUtil-*.js，`findResizeDirection` 那段）：
   *
   *     let u = node.computeSize();
   *     l.height < u[1] && (l.height = u[1]);      // ← 拖小被夹回 computeSize
   *     node.setSize(l.size);
   *
   * 而 `computeSize()` 里 DOM widget 的高度就来自 `computeLayoutSize().minHeight`。
   * 我们原来让 minHeight 等于**当前面板高度** ⇒ 下限 = 面板 + chrome ⇒
   * 面板拉大过后，节点永远缩不到那个高度以下（"只能缩到固定值，拉得越大这个固定值越大"）。
   *
   * 破法：**拖动期间**把下限降到 PANEL_MIN_H，让 litegraph 的夹取失去作用；
   * 面板高度由指针位移驱动（见下面的 moveDrag），两者同增同减，不用反算。
   * 拖完立刻把下限恢复成面板高度 —— 这样节点底部那两个 canvas 按钮仍然排在面板
   * 下方（下限 < 面板高度会让按钮跑到面板背后被盖住）。
   */
  widget.computeLayoutSize = function () {
    const min = node.__dkResizing ? PANEL_MIN_H : node.__dkPanelH;
    return { minHeight: min, maxHeight: undefined, minWidth: 0 };
  };

  const state = {
    panel: null,
    collapsed: false,
    status: "",
    pendingTimer: 0,
  };

  /* ---------------------------------------------------------------- 跟随节点大小
   *
   * 用户拖动节点右下角 → 面板跟着变高/变矮（不然 600px 写死，拉大节点只是白边变多）。
   *
   * 🔴 为什么不用 `getHeight: () => node.size[1] - 130`：
   *    computeLayoutSize(读 getHeight) → computeSize → setSize → 节点高变 →
   *    computeLayoutSize 再读 → **正反馈**，实测会一路涨到画布装不下。
   *
   * 这里的做法是**单向**的：
   *    pointer 拖动 → 写 node.__dkPanelH → 改 DOM 高度 → **不调 setSize**
   *    环在这里断掉。程序自己调 resize() 时用 __dkSelfResize 跳过。
   */
  /**
   * 面板高度跟随节点：**按鼠标位移改面板**（判断依据只看两件事：鼠标 Y 和节点位置）。
   *
   * 🔴 前六轮为什么都不行（都是「判据不可靠」）：
   *    · `onResize` 的 `size[1]` 被 `Math.max(size, computeSize)` 夹过 →
   *      **拖小时 delta 恒为 0，缩不回去**；
   *    · `canvas.resizing_node` 真实环境不一定等于本节点 → 完全不触发；
   *    · `e.target === canvas.canvas` 真实点击时 target 可能是上层覆盖元素 → 完全不触发；
   *    · 反算 `size - chrome` 口径差几十 px → 每轮累积。
   *
   * ✅ 现在只依赖**鼠标坐标** + **节点位置是否变化**，不碰任何框架内部状态：
   *
   *    mousedown：在节点范围内、且不在面板里 → 记基线（鼠标 Y、当前面板高、节点 pos）
   *    mousemove：**节点 pos 变了 = 用户在拖节点，立刻放弃**；
   *                pos 没变 = 在拖 resize 手柄 → 按鼠标位移改面板
   *    mouseup  ：清基线
   *
   *    「pos 变没变」是区分「拖节点」和「拖 resize」最可靠的信号 ——
   *    拖节点必然改 pos，resize 必然不改。
   *
   *    mousemove 在 capture 阶段执行，**早于** litegraph 的 setSize：
   *    面板先变小 → computeSize 变小 → setSize 的下限跟着降 → 节点才能缩回去。
   */
  let dragBase = null;

  /* 🔴 必须监听 **pointer 事件**，不能只监听 mouse。
   *
   * ComfyUI 1.58 的画布拖拽走的是 `pointerdown/pointermove/pointerup`；
   * 浏览器规范要求 pointer 事件**先于**同一次操作的兼容 mouse 事件派发。
   * 之前只挂 mousemove：等我们收到事件时，前端已经用旧的面板高度算完
   * `computeSize()` 并把节点夹住了 —— 所以我们改小面板也晚了一步，
   * 表现就是「放大没事（放大不受下限限制），缩小永远缩不回去」。
   *
   * 现在 pointer 分支先跑：面板先变小 → 前端那次 computeSize 才是小的 → 节点能跟着缩。
   * mouse 分支保留做兜底（老版本前端 / 只发 mouse 的环境），用时间戳去重，
   * 避免同一动作被执行两遍（面板会跳一下）。
   */
  let lastPointerAt = -1e9;
  const POINTER_GRACE = 600;
  function nowMs() {
    return typeof performance !== "undefined" && performance.now
      ? performance.now()
      : Date.now();
  }

  function canvasPtOf(e) {
    const canvas = window.app && window.app.canvas;
    if (!canvas) return null;

    // 优先用框架的换算（不同版本都有，但保险起见还有下面的备用公式）
    if (typeof canvas.convertEventToCanvasOffset === "function") {
      try {
        const p = canvas.convertEventToCanvasOffset(e);
        if (p && Number.isFinite(p[0]) && Number.isFinite(p[1])) return p;
      } catch (err) { /* 落到备用公式 */ }
    }

    // 备用公式：画布坐标 = (屏幕坐标 - canvas 左上) / 缩放 - 平移
    try {
      const el = canvas.canvas;
      if (!el) return null;
      const rect = el.getBoundingClientRect();
      const scale = (canvas.ds && canvas.ds.scale) || 1;
      const offset = (canvas.ds && canvas.ds.offset) || [0, 0];
      const x = (e.clientX - rect.left) / scale - offset[0];
      const y = (e.clientY - rect.top) / scale - offset[1];
      return Number.isFinite(x) && Number.isFinite(y) ? [x, y] : null;
    } catch (err) {
      return null;
    }
  }

  function beginDrag(e) {
    try {
      const canvas = window.app && window.app.canvas;
      if (!canvas || e.button !== 0) return;
      if (state.collapsed) return;

      // 🔴 关键：面板拉大后会**盖住整个节点**（面板高度 ≈ 节点高度），
      //    用户拖的「右下角」其实落在面板内部。如果一律把「点在面板里」当成选图，
      //    就永远拖不动。所以：面板底部 60px 内算 resize 区，其余面板区域才是选图。
      const t = e.target;
      if (t && typeof t.closest === "function") {
        const panelEl = t.closest(".dk-pv-panel");
        if (panelEl) {
          let nearPanelBottom = false;
          try {
            const r = panelEl.getBoundingClientRect();
            nearPanelBottom = Math.abs(e.clientY - r.bottom) <= 60;
          } catch (err) { /* ignore */ }
          if (!nearPanelBottom) return;   // 面板内部（非底边）= 选图
        }
      }

      const p = canvasPtOf(e);
      if (!p) return;

      // 放宽 ±20：不同版本节点边框/圆角会让精确边界差几个像素
      const TOL = 20;
      const x0 = node.pos[0] - TOL;
      const y0 = node.pos[1] - TOL;
      const x1 = node.pos[0] + node.size[0] + TOL;
      const y1 = node.pos[1] + node.size[1] + TOL;
      if (p[0] < x0 || p[0] > x1 || p[1] < y0 || p[1] > y1) return;

      dragBase = {
        mouseY: e.clientY,
        panelH: node.__dkPanelH,
        pos: [node.pos[0], node.pos[1]],
      };
      // 拖动期间放开布局下限（见 widget.computeLayoutSize）。
      // 没有这一句，前端 `l.height = Math.max(l.height, computeSize)` 会把节点
      // 夹在「旧面板高度 + chrome」上，怎么拖都缩不回去。
      node.__dkResizing = true;
    } catch (err) { /* ignore */ }
  }

  function moveDrag(e) {
    if (!dragBase) return;
    try {
      // 节点位置变了 → 用户在拖整个节点，不是改大小
      if (node.pos[0] !== dragBase.pos[0] || node.pos[1] !== dragBase.pos[1]) {
        endDrag();
        return;
      }

      const canvas = window.app && window.app.canvas;
      const scale = (canvas && canvas.ds && canvas.ds.scale) || 1;
      const delta = (e.clientY - dragBase.mouseY) / scale;
      if (Math.abs(delta) < 2) return;      // 手抖忽略

      applyPanelHeight(dragBase.panelH + delta);
    } catch (err) {
      console.warn("[dogking-tools] 面板跟随指针失败", err);
    }
  }

  function endDrag() {
    dragBase = null;
    node.__dkResizing = false;
  }

  // pointer 先跑（前端画布用 pointer 拖拽，pointer 派发早于兼容 mouse 事件）
  function onPointerDown(e) { lastPointerAt = nowMs(); beginDrag(e); }
  function onPointerMove(e) { lastPointerAt = nowMs(); moveDrag(e); }
  function onPointerUp() { lastPointerAt = nowMs(); endDrag(); }

  // mouse 兜底：同一次操作已经走过 pointer 分支就跳过，避免面板跳两下
  function onMouseDown(e) {
    if (nowMs() - lastPointerAt < POINTER_GRACE) return;
    beginDrag(e);
  }
  function onMouseMove(e) {
    if (nowMs() - lastPointerAt < POINTER_GRACE) return;
    moveDrag(e);
  }
  function onMouseUp() {
    if (nowMs() - lastPointerAt < POINTER_GRACE) return;
    endDrag();
  }

  const EVENTS = [
    ["pointerdown", onPointerDown],
    ["pointermove", onPointerMove],
    ["pointerup", onPointerUp],
    ["pointercancel", onPointerUp],
    ["mousedown", onMouseDown],
    ["mousemove", onMouseMove],
    ["mouseup", onMouseUp],
  ];
  EVENTS.forEach(([type, fn]) => window.addEventListener(type, fn, true));


  const prevOnRemoved = node.onRemoved;
  node.onRemoved = function () {
    try {
      EVENTS.forEach(([type, fn]) => window.removeEventListener(type, fn, true));
      clearInterval(fallbackTimer);
      clearTimeout(state.pendingTimer);
    } catch (err) { /* ignore */ }
    return prevOnRemoved ? prevOnRemoved.apply(this, arguments) : undefined;
  };

  /* ---------------------------------------------------------------- 节点变小时的保险
   *
   * 指针方案是实时的；万一是触摸板 / 某些环境 pointermove 没到 window，
   * 也还有一条路：litegraph 每次 setSize 都会回调 onResize —— 那时节点**已经被缩完了**，
   * 面板却还停在原来的高度。
   *
   * 这里只处理「节点比面板还矮」的收方向：
   *   room = 节点高 - chrome，若 room < 当前面板高 → 面板缩到 room。
   * 只收不放，所以不会反过来把节点撑大（放大走指针 / 兜底定时器）。
   * 必须链式调用原 onResize —— addWidget 已经在上面挂了 beforeResize/afterResize。
   */
  const prevOnResize = node.onResize;
  node.onResize = function (size) {
    const result = prevOnResize ? prevOnResize.apply(this, arguments) : undefined;
    try {
      if (!state.collapsed && !node.__dkResizing && !node.__dkSelfResize) {
        const h = size && Number.isFinite(size[1])
          ? size[1]
          : (node.size && node.size[1]);
        const chrome = node.__dkChromeH;
        if (Number.isFinite(h) && Number.isFinite(chrome)) {
          const room = h - chrome;
          // room 太小说明还在初始化（节点尺寸是默认值），别拿它改面板
          if (room >= PANEL_MIN_H && room < node.__dkPanelH - 2) {
            applyPanelHeight(room);
          }
        }
      }
    } catch (err) { /* ignore */ }
    return result;
  };

  /**
   * 兜底：每 800ms 检查一次「节点高 - 面板高」是否和 chrome 严重不匹配。
   *
   * 指针方案是即时的，但它依赖 pointermove 真的到 window ——
   * 不同 ComfyUI 版本 / 覆盖层 / 环境都可能让事件不触发（headless 里就复现过：
   * CDP 发的指针事件根本到不了页面）。所以补一条**不依赖任何事件**的兜底。
   *
   * 阈值 80px 是刻意的：chrome 的口径误差只有几十像素，小于阈值不会动，
   * 所以不会像第三轮那样每轮累积；只有用户真的拖了几百像素才会触发。
   * **只放大不缩小** —— 缩小由 onResize 那条路负责（那里判据更准）。
   */
  let lastFallbackAt = 0;
  const fallbackTimer = setInterval(() => {
    try {
      // 拖拽标志卡住（pointerup 丢了）会让布局下限一直是 PANEL_MIN_H，
      // 后续任何一次 computeSize 都会把节点夹得很矮。这里兜一次。
      if (node.__dkResizing && !dragBase) node.__dkResizing = false;

      if (state.collapsed) return;

      // chrome 必须在合理区间，否则说明还没初始化好 —— 直接跳过，
      // 不然负数 chrome 会算出巨大的 diff，一次就把节点拉爆。
      const chrome = node.__dkChromeH;
      if (!Number.isFinite(chrome) || chrome < 20 || chrome > 1200) return;

      if (node.__dkResizing) return;   // 正在拖动，别插手

      const h = node.size && node.size[1];
      if (!Number.isFinite(h)) return;

      const diff = h - (node.__dkPanelH + chrome);

      // 只处理「节点明显比面板大」的情况（拖大）；
      // 上限 1000 防止任何异常值一次调太多；两次调整至少隔 1.5 秒，避免来回抖。
      if (diff <= 80 || diff > 1000) return;
      const now = Date.now();
      if (now - lastFallbackAt < 1500) return;
      lastFallbackAt = now;

      applyPanelHeight(node.__dkPanelH + diff);
    } catch (err) { /* ignore */ }
  }, 800);

  /** 按当前的 mode/filters 重建面板（选择模式变了就得换一个实例） */
  function rebuild() {
    clearTimeout(state.pendingTimer);
    state.pendingTimer = 0;
    if (state.panel) {
      try {
        state.panel.destroy();
      } catch (err) {
        console.warn("[dogking-tools] 销毁旧面板失败", err);
      }
      state.panel = null;
    }
    if (state.collapsed) return; // 收起来时不用拉数据，展开时再建
    try {
      // 新建的 PvPanel 会带一个 CSS height:600px 的根节点，
      // 节点被拉大过的话要立刻把高度改回来，否则一重建就缩回 600。
      state.panel = new PvPanel({
        kind: options.kind || "image",
        mount: el,
        view: options.view || "grid",
        mode: typeof options.getMode === "function" ? options.getMode() : "multi",
        pageSize: options.pageSize || 40,
        maxSelect: options.maxSelect || 8,
        filters: typeof options.getFilters === "function" ? options.getFilters : () => ({}),
        // 随机数量由节点层提供（面板只显示、不存，避免两处状态不一致）
        randomCount: typeof options.randomCount === "function" ? options.randomCount : null,
        // 实时确认：节点 1/2/3 传 true（点选即生效、底栏只有清空）；
        // 节点 4 不传（三按钮，因为动态输出口不能跟着点选乱增删）
        liveConfirm: Boolean(options.liveConfirm),
        onConfirm: (selected) => {
          if (typeof options.onConfirm === "function") options.onConfirm(selected);
        },
        onChange: (selected) => {
          if (typeof options.onChange === "function") options.onChange(selected);
        },
      });
      // 把当前高度套到新面板（CSS 默认 600，节点被拉大过就得改回来）
      try {
        if (state.panel && state.panel.root) {
          state.panel.root.style.height = `${node.__dkPanelH}px`;
          state.panel.root.style.minHeight = `${node.__dkPanelH}px`;
        }
      } catch (err) { /* ignore */ }
    } catch (err) {
      console.error("[dogking-tools] 面板创建失败", err);
    }
  }

  /**
   * 延后一拍再建面板。
   *
   * 为什么必须延后：ComfyUI 加载工作流时是「先 onNodeCreated、紧接着 onConfigure」，
   * 都在同一个任务里。如果创建时就立刻拉数据，打开一个存了 4 个 PV 节点的工作流
   * 会立刻发 4 次 pv_list + 上百个缩略图请求 —— 而用户此时只是"打开"而已。
   *
   * 延后到下一个宏任务，`onConfigure` 就有机会先调 `cancelPending()` 把它取消掉，
   * 节点保持「收起 + 占位条」的状态，等用户真的点开面板才发请求。
   */
  function scheduleBuild() {
    clearTimeout(state.pendingTimer);
    state.pendingTimer = setTimeout(() => {
      state.pendingTimer = 0;
      rebuild();
    }, 0);
  }

  /** 取消还没执行的首次建面板（onConfigure 里调，避免加载工作流时发请求） */
  function cancelPending() {
    clearTimeout(state.pendingTimer);
    state.pendingTimer = 0;
  }

  /**
   * 打开工作流时调：**保持展开**，并把面板建起来。
   *
   * 之前的做法是 onConfigure 里 `setCollapsed(true)` —— 确实省了请求，
   * 但用户每次切工作流面板都自己关回去，得手点一下「展开面板」，很烦。
   * 现在默认展开。
   *
   * 三个细节：
   *   ① **错峰**：一次打开 4 个 PV 节点会在同一瞬间发 4 个 pv_list + 上百个缩略图，
   *      所以按序号错开 160ms，先让画布渲染完再逐个拉数据。
   *   ② **反推面板高度**：`serialize:false` 意味着 `__dkPanelH` 不进工作流，
   *      不反推的话用户存了个 850 高的面板，打开就缩回 600（节点底部一大片空白）。
   *      精度靠 `__dkChromeH`，误差几十像素无所谓，兜底定时器会补齐。
   *   ③ **只在装不下时撑高节点**：无条件 resize 会把用户手动拉大的节点缩回默认值。
   * @param {Function} [onReady] 面板重建完后的回调（各节点用它回填已选 / 套用视图状态）
   */
  let restoreSeq = 0;
  function restoreExpanded(onReady) {
    clearTimeout(state.pendingTimer);
    state.pendingTimer = 0;

    if (state.collapsed) {
      state.collapsed = false;
      showWidgetAsData(widget);
      el.style.display = "";
    }

    // 🔴 必须在**同步这一刻**抓节点高度快照。
    //    onConfigure 已经把工作流里存的尺寸应用上了；而下一帧的 rAF 里
    //    `resize()` 会把它改成「面板 600 + chrome」—— 那时再读就只能读到 600，
    //    用户存的大面板永远恢复不回来。
    const savedH = node.size && Number.isFinite(node.size[1]) ? node.size[1] : NaN;

    const wait = 180 + (restoreSeq++ % 8) * 160;
    state.pendingTimer = setTimeout(() => {
      state.pendingTimer = 0;

      let changed = false;
      try {
        const chrome = node.__dkChromeH;
        if (Number.isFinite(savedH) && Number.isFinite(chrome) && chrome > 20 && chrome < 1200) {
          const panelH = savedH - chrome;
          // 差值 < 80 不动，避免 chrome 的几十像素误差让每次打开都微调
          if (
            panelH >= PANEL_MIN_H &&
            panelH <= PANEL_MAX_H &&
            Math.abs(panelH - node.__dkPanelH) > 80
          ) {
            applyPanelHeight(panelH, true);
            changed = true;
          }
        }
      } catch (err) { /* ignore */ }

      rebuild();

      // 面板是新建的（display 状态、已选高亮全丢）→ 交给节点层重新套一遍
      try {
        if (typeof onReady === "function") onReady(state.panel);
      } catch (err) {
        console.warn("[dogking-tools] 恢复面板状态失败", err);
      }

      try {
        const chrome = Number.isFinite(node.__dkChromeH) ? node.__dkChromeH : 210;
        const need = node.__dkPanelH + chrome;
        if (changed || !Number.isFinite(node.size[1]) || node.size[1] < need - 4) resize();
      } catch (err) { /* ignore */ }
    }, wait);
  }

  /** 节点尺寸：宽度不够就撑到 760，高度 = 所有 widget 高 + 状态条 */
  function resize() {
    // 同一帧里可能被调好几次（改模式、改输出方式、展开面板…）——
    // 排一个就够，避免连续 setSize 抖动。
    if (node.__dkResizeQueued) return;
    node.__dkResizeQueued = true;
    // 标记「这次 setSize 是我们自己调的」，onResize 里要跳过，
    // 否则会把面板高度当成用户拖动来重算（环）。
    node.__dkSelfResize = true;
    requestAnimationFrame(() => {
      node.__dkResizeQueued = false;
      try {
        const size = node.computeSize();
        const height = Number.isFinite(size && size[1]) ? size[1] : 200;
        node.setSize([
          Math.max(node.size[0], PANEL_MIN_W),
          Math.max(height + STATUS_BAR_H, 160),
        ]);
        // 记下「节点总高 - 面板高」= 标题 + 其他 widget + 状态条。
        //
        // 🔴 必须**每次重算**（之前写成「只算一次」是错的）：
        //    这个值不是常量 —— pick 节点切「输出方式」会显隐「重置计数 / 随机数量 /
        //    自动连续」三个按钮，每多一个 widget 就多 24px。冻结成初次的值以后，
        //    节点高度不再跟着长，最后一个按钮就溢出到节点外边、被面板压住
        //    （用户截图：收起面板被重置计数挤上去）。
        //    重算不会形成反馈环：computeSize 只读 widget 高度，不读 node.size。
        //
        // 🔴 范围校验保留：节点还没布局完时 `node.size[1]` 可能是默认值（几十像素），
        //    减去面板 600 会得到负数；负数 chrome 会让兜底算出巨大的 diff，
        //    一展开就把节点拉到几千像素。
        //    只在展开态更新：收起时面板不参与布局（size 会远小于面板高），
        //    那个差值不是 chrome。
        if (!state.collapsed) {
          const c = node.size[1] - node.__dkPanelH;
          if (Number.isFinite(c) && c > 20 && c < 1200) node.__dkChromeH = c;
        }
        node.setDirtyCanvas(true, true);
      } catch (err) {
        // 节点还没挂到画布上，忽略
      }
      setTimeout(() => {
        node.__dkSelfResize = false;
      }, 0);
    });
  }

  /**
   * 收起 / 展开。
   *
   * 收起必须用 `type='hidden'`（`hideWidgetAsData`）—— 只有这个值会让
   * `DOMWidgetImpl.computeLayoutSize()` 返回 0 高度、由 litegraph 原生折叠。
   * 用 `setWidgetVisible` 的 `'dogkingHidden'` 对 DOM widget **无效且不报错**。
   */
  function setCollapsed(collapsed) {
    state.collapsed = Boolean(collapsed);
    if (state.collapsed) {
      // 收起前兜底写回一次（实时确认下每次交互都写过了，这里是最后一道保险）。
      // 面板的 close() 在选中为空时是 no-op，所以「打开工作流 → 收起」这条
      // 路径不会把已存的清单冲掉。
      if (state.panel) {
        try {
          state.panel.close();
        } catch (err) {
          console.warn("[dogking-tools] 收起面板时回传选中失败", err);
        }
      }
      hideWidgetAsData(widget);
      el.style.display = "none";
    } else {
      showWidgetAsData(widget);
      el.style.display = "";
      if (!state.panel) rebuild(); // 收起期间没建面板，展开时补上
    }
    resize();
  }

  function toggleCollapsed() {
    setCollapsed(!state.collapsed);
    return state.collapsed;
  }

  /** 筛选条件变了：面板保留已选、回到第 1 页重拉 */
  function applyFilters() {
    if (state.panel && !state.collapsed) {
      try {
        state.panel.setFilters();
      } catch (err) {
        console.warn("[dogking-tools] 面板刷新失败", err);
      }
    }
  }

  /** 执行后写入状态条文案（不触发网络） */
  function setStatus(text) {
    state.status = String(text || "");
    node.dogkingStatus = state.status;
    node.setDirtyCanvas?.(true, true);
  }

  /** 在节点底部画状态条 */
  function drawStatus(ctx) {
    const text = state.status || node.dogkingStatus || "";
    if (!text) return;
    try {
      drawBadge(ctx, node, text);
    } catch (err) {
      // 状态条画不出来不影响节点功能
    }
  }

  scheduleBuild();
  resize();

  return {
    el,
    widget,
    get panel() { return state.panel; },
    get collapsed() { return state.collapsed; },
    rebuild,
    cancelPending,
    restoreExpanded,
    resize,
    setCollapsed,
    toggleCollapsed,
    applyFilters,
    setStatus,
    drawStatus,
  };
}

/**
 * 从「已选」隐藏 widget 里读清单。
 *
 * 存的是 `{"ids":[...], "names":[...]}`（见后端 `_parse_sel`，两种格式都吃）。
 * 任何脏数据都返回空数组，绝不抛异常 —— 这个函数在 onConfigure 里跑，
 * 抛异常会让整个工作流打不开。
 *
 * @param {object} widget
 * @returns {{ids: string[], names: string[]}}
 */
export function readSelection(widget) {
  const raw = widget ? String(widget.value || "").trim() : "";
  if (!raw) return { ids: [], names: [] };
  try {
    const data = JSON.parse(raw);
    if (data && typeof data === "object" && Array.isArray(data.ids)) {
      const ids = data.ids.map((v) => String(v || "")).filter(Boolean);
      const names = Array.isArray(data.names) ? data.names.map((v) => String(v || "")) : [];
      return { ids, names };
    }
    if (Array.isArray(data)) {
      const ids = [];
      const names = [];
      data.forEach((item) => {
        if (item && typeof item === "object" && item.id) {
          ids.push(String(item.id));
          names.push(String(item.name || ""));
        } else if (typeof item === "string" && item) {
          ids.push(item);
          names.push("");
        }
      });
      return { ids, names };
    }
  } catch (err) {
    // 脏数据：当没选
  }
  return { ids: [], names: [] };
}

/**
 * 把选中结果写回「已选」隐藏 widget。
 *
 * @param {object} widget
 * @param {Array<{id:string,name?:string}>} selected
 * @returns {number} 写入的条数
 */
export function writeSelection(widget, selected) {
  const list = Array.isArray(selected) ? selected : [];
  const ids = [];
  const names = [];
  const seen = new Set();
  list.forEach((item) => {
    const id = String((item && item.id) || "");
    if (!id || seen.has(id)) return;
    seen.add(id);
    ids.push(id);
    names.push(String((item && item.name) || ""));
  });
  if (widget) widget.value = JSON.stringify({ ids, names });
  return ids.length;
}

/**
 * 收起面板按钮的文案要跟着状态走。
 * @param {object} widget
 * @param {boolean} collapsed
 */
export function syncCollapseButton(widget, collapsed) {
  if (!widget) return;
  widget.name = collapsed ? "展开面板" : "收起面板";
}

export { isWidgetHidden };

export default {
  createPanelHost,
  readSelection,
  writeSelection,
  syncCollapseButton,
  STATUS_BAR_H,
  PANEL_MIN_W,
};
