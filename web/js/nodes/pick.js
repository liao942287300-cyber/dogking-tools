/**
 * dogking-tools —— 节点 1「图片+提示词」前端（T03）
 *
 * 职责：
 *   · 把内嵌面板挂到节点上（600px，DOM widget）
 *   · 两级分类联动（一级变了 → 收窄二级；**二级只显示叶子名**）
 *   · 按「选择模式」显隐「输出方式」/「随机数量」/「自动连续」
 *   · 面板选中变化 → **实时**把清单写进隐藏 widget「已选」，节点上显示「已选 N 张」
 *   · 恢复工作流：只渲染占位条，**一个请求都不发**
 *
 * ⚠️ 面板已经没有「确定」按钮了：点卡片 / 随机抽 / 清空 都会立刻写回
 * （见 lib/panel.js 文件头的「实时确认」）。所以这里**不存在**「未确定」状态，
 * 也不需要在 onChange 里为随机模式做特殊写回 —— 所有模式统一走 onConfirm。
 *
 * 三条必须照做的约定（都是踩过的坑）：
 *   ① 面板折叠用 `hideWidgetAsData`（`type='hidden'`），
 *      普通 widget 显隐用 `setWidgetVisible`（`'dogkingHidden'`）。**不能混用**。
 *   ② 面板高度固定 600，绝不写 `getHeight: () => node.size[1] - 130`（反馈环）。
 *   ③ `onConfigure` 里不调任何接口 —— 打开工作流时 4 个节点各发一次请求纯浪费。
 */

import { app, pvCategories, resetCount } from "../lib/api.js";
import { setWidgetVisible, hideWidgetAsData } from "../lib/ui.js";
import {
  createPanelHost,
  readSelection,
  writeSelection,
  syncCollapseButton,
} from "./panel-host.js";

const NODE = "DogkingPVPick";

const PICK_SINGLE = "单选";
const PICK_MULTI = "批量选";
const PICK_RANDOM = "随机多选";
const OUT_QUEUE = "排队输出";

const ALL = "全部";

/**
 * 中文选择模式 → PvPanel 认的英文模式。
 *
 * ⚠️ PvPanel 的 mode 只认 'single' / 'multi' / 'random'，传中文会**静默**落到
 * 'multi'（它的构造函数是 `mode === 'single' || mode === 'random' ? mode : 'multi'`）。
 * 表现就是「单选模式点卡片没反应」——排查过一次，别再踩。
 */
const MODE_TO_PANEL = {
  [PICK_SINGLE]: "single",
  [PICK_MULTI]: "multi",
  [PICK_RANDOM]: "random",
};

/** 拿 widget 的小工具（节点上同名 widget 只有一个） */
function findWidget(node, name) {
  return (node.widgets || []).find((w) => w.name === name) || null;
}

function modeOf(widget) {
  const value = String((widget && widget.value) || "").trim();
  if (value === PICK_MULTI || value === PICK_RANDOM) return value;
  return PICK_SINGLE;
}

function panelModeOf(widget) {
  return MODE_TO_PANEL[modeOf(widget)] || "multi";
}

/** 完整路径 → 叶子名（`风格/写实摄影` → `写实摄影`） */
function leafOf(path) {
  const text = String(path || "");
  const idx = text.indexOf("/");
  return idx >= 0 ? text.slice(idx + 1) : text;
}

/**
 * 按「选择模式 / 输出方式」决定哪些参数可见。
 *
 *   单选          → 隐藏「输出方式」「随机数量」（只有 1 张，排队无意义）
 *   批量选        → 显示「输出方式」，隐藏「随机数量」
 *   随机多选      → 两个都显示
 *   「自动连续」  → 只在「排队输出」时显示
 *
 * @param {object} node
 * @param {string} [modeOverride] 直接用这个值（callback 里 widget.value 可能还没更新）
 */
export function applyPickVisibility(node, modeOverride) {
  const modeW = findWidget(node, "选择模式");
  const outW = findWidget(node, "输出方式");
  const numW = findWidget(node, "随机数量");
  const autoW = findWidget(node, "自动连续");
  const resetBtn = findWidget(node, "重置计数");

  const mode = modeOverride !== undefined && modeOverride !== null ? modeOverride : modeOf(modeW);
  const isSingle = mode === PICK_SINGLE;
  const isRandom = mode === PICK_RANDOM;
  const isQueue = String((outW && outW.value) || "").trim() === OUT_QUEUE;

  setWidgetVisible(node, outW, !isSingle);
  setWidgetVisible(node, numW, isRandom);
  setWidgetVisible(node, autoW, !isSingle && isQueue);
  // 「重置计数」只对排队有意义（一次性输出没有游标）
  setWidgetVisible(node, resetBtn, !isSingle && isQueue);

  // 🔴 widget 显隐一变，节点高度必须重算 —— 否则多出来的按钮（切到「排队」会多
  //    「重置计数 / 自动连续」两个）就画到节点边界外，被上面 600px 的 DOM 面板压住。
  //    resize() 只排一个 rAF，连点几下不会抖。
  try {
    if (node.__dkHost) node.__dkHost.resize();
  } catch (err) { /* 还没建好面板，忽略 */ }

  node.setDirtyCanvas?.(true, true);
}

/**
 * 拉一次分类列表并收窄「二级分类」下拉。
 *
 * 只在**用户改了一级分类**时调用（不在 onNodeCreated / onConfigure 里调），
 * 这样打开工作流不会产生任何请求。
 *
 * @param {object} node
 * @param {boolean} [keepCurrent] 当前二级值仍然合法时是否保留
 */
async function refreshSubCategories(node, keepCurrent = true) {
  const catW = findWidget(node, "分类");
  const subW = findWidget(node, "二级分类");
  if (!catW || !subW) return;

  const addr = String((findWidget(node, "服务地址") || {}).value || "");
  const top = String(catW.value || ALL).trim();

  let categories = [];
  try {
    const res = await pvCategories({ addr, kind: "image" });
    if (res && res.ok) categories = res.categories || [];
  } catch (err) {
    console.warn("[dogking-tools] 拉分类失败，二级下拉保持原样", err);
    return;
  }

  node.__dkCats = categories;

  const leaves = [];
  categories.forEach((c) => {
    if (Number(c.level) !== 2) return;
    if (top !== ALL && String(c.parentPath || "") !== top) return;
    const leaf = leafOf(c.path);
    if (leaf && !leaves.includes(leaf)) leaves.push(leaf);
  });

  const options = [ALL].concat(leaves);
  const previous = String(subW.value || ALL);
  subW.options = subW.options || {};
  subW.options.values = options;
  // 原来选的二级如果还在新列表里就保留，否则回「全部」
  subW.value = keepCurrent && options.includes(previous) ? previous : ALL;
  node.setDirtyCanvas?.(true, true);
}

/**
 * 把「一级 + 二级」解析成 PromptVault 认的分类 id。
 *
 * 二级优先（更精确）：选了二级就发二级 id；只选一级就发一级 id，
 * 后端会自动带 includeChildren=1（不带会返回 0 条，见架构 R14）。
 *
 * @param {object} node
 * @returns {string} 分类 id；「全部」返回空串
 */
function resolveCategoryId(node) {
  const catW = findWidget(node, "分类");
  const subW = findWidget(node, "二级分类");
  const top = String((catW && catW.value) || ALL).trim();
  const sub = String((subW && subW.value) || ALL).trim();
  const categories = node.__dkCats || [];

  if (sub !== ALL) {
    const hit = categories.find((c) => {
      if (Number(c.level) !== 2) return false;
      if (leafOf(c.path) !== sub) return false;
      if (top === ALL) return true;
      return String(c.parentPath || "") === top;
    });
    if (hit) return String(hit.id || "");
  }
  if (top !== ALL) {
    const hit = categories.find((c) => Number(c.level) === 1 && c.path === top);
    if (hit) return String(hit.id || "");
  }
  return "";
}

/** 已选数量写到节点上（badge 用） */
function updateSelBadge(node, count) {
  node.dogkingSelCount = count;
  node.setDirtyCanvas?.(true, true);
}

/** 节点 1 的初始化 */
function setupPick(node) {
  const selW = findWidget(node, "已选");
  const modeW = findWidget(node, "选择模式");
  const outW = findWidget(node, "输出方式");
  const catW = findWidget(node, "分类");
  const subW = findWidget(node, "二级分类");
  const addrW = findWidget(node, "服务地址");

  if (!selW) {
    console.warn("[dogking-tools] 节点 1 找不到「已选」widget，跳过面板挂载");
    return;
  }

  // 已选清单：**隐藏但仍序列化**（type='hidden'，随工作流保存）。
  // 这是数据不是 UI —— 用户不该看到/编辑它，但必须跟着工作流走。
  hideWidgetAsData(selW);

  // ---- 面板宿主 ----
  const host = createPanelHost(node, {
    kind: "image",
    view: "grid",
    pageSize: 40,
    maxSelect: 200,
    getMode: () => panelModeOf(modeW),
    // 随机数量只有一个数据源：节点上的「随机数量」widget（面板不自己存）
    randomCount: () => {
      const w = findWidget(node, "随机数量");
      const n = Number(w && w.value);
      return Number.isFinite(n) && n > 0 ? n : 4;
    },
    getFilters: () => {
      const top = String((catW && catW.value) || ALL).trim();
      const sub = String((subW && subW.value) || ALL).trim();
      return {
        addr: String((addrW && addrW.value) || ""),
        // 面板要的是**分类 id**（pv_list 的 categoryId），不是路径
        category: resolveCategoryId(node),
        // 工具条显示用的完整路径（Q-B：一级 / 二级），和 category 分开传，
        // 否则面板会把 id 当路径显示出来
        categoryPath: top === ALL ? "" : (sub === ALL ? top : `${top} / ${sub}`),
        subCategory: "",
      };
    },
    // 实时确认：任何选中变化都会走到这里，把清单写进隐藏 widget。
    // 单选模式下点一张就写回**并且面板不关闭**（内嵌面板，收起由用户决定）。
    // 实时确认：点选即写回，底栏只有「清空」（用户要求节点 1 不要确定按钮）
    liveConfirm: true,
    onConfirm: (selected) => {
      const count = writeSelection(selW, selected);
      updateSelBadge(node, count);
      host.setStatus(count ? `已选 ${count} 张` : "未选择图片");
    },
    // onChange 只更新徽标数字：写回统一在 onConfirm 里做，避免同一次交互写两遍。
    onChange: (selected) => {
      updateSelBadge(node, selected.length);
    },
  });
  node.__dkHost = host;

  // ---- 按钮 ----
  const collapseBtn = node.addWidget("button", "收起面板", null, () => {
    const collapsed = host.toggleCollapsed();
    syncCollapseButton(collapseBtn, collapsed);
  });
  collapseBtn.serialize = false;
  node.__dkCollapseBtn = collapseBtn;

  const resetBtn = node.addWidget("button", "重置计数", null, async () => {
    try {
      await resetCount({ node: String(node.id) });
      host.setStatus("计数已重置");
    } catch (err) {
      host.setStatus(`重置失败：${err && err.message ? err.message : err}`);
    }
  });
  resetBtn.serialize = false;

  // ---- 联动 ----
  if (catW) {
    const previous = catW.callback;
    catW.callback = async function () {
      const result = previous ? previous.apply(this, arguments) : undefined;
      await refreshSubCategories(node);
      host.applyFilters();   // 分类变了，面板回到第 1 页重拉（保留已选）
      return result;
    };
  }

  if (subW) {
    const previous = subW.callback;
    subW.callback = function () {
      const result = previous ? previous.apply(this, arguments) : undefined;
      host.applyFilters();
      return result;
    };
  }

  if (modeW) {
    const previous = modeW.callback;
    modeW.callback = function () {
      const result = previous ? previous.apply(this, arguments) : undefined;
      applyPickVisibility(node);
      // 选择模式变了要换一个面板实例（single/multi/random 的交互完全不同）
      host.rebuild();
      return result;
    };
  }

  if (outW) {
    const previous = outW.callback;
    outW.callback = function () {
      const result = previous ? previous.apply(this, arguments) : undefined;
      applyPickVisibility(node);   // 「自动连续」跟着「输出方式」显隐
      return result;
    };
  }

  // 节点上改了「随机数量」→ 面板按钮标签跟着变（面板不存这个值）
  const numWidget = findWidget(node, "随机数量");
  if (numWidget) {
    const previous = numWidget.callback;
    numWidget.callback = function () {
      const result = previous ? previous.apply(this, arguments) : undefined;
      if (host.panel) host.panel.updateRandomButton();
      return result;
    };
  }

  applyPickVisibility(node);
  syncCollapseButton(collapseBtn, host.collapsed);
}

/**
 * 恢复工作流：回填已选 + 占位条，并把分类信息补齐。
 *
 * ⚠️ 这里**必须**把 `__dkCats` 补上：面板的 `filters()` 靠它把
 * 「风格/写实摄影」解析成 `categoryId`，而 `__dkCats` 只在用户手动改分类时
 * 才会被填。不补的话，打开已保存的工作流后面板会拿空筛选去拉
 * **全部 17726 张**（用户看到「分类没生效」，还白拉 34MB）。
 *
 * 代价是一次 `pv_categories`（≤2KB，只读缓存），换来筛选正确还原。
 * 注意仍然**不调 `pv_meta` / `pv_thumb`** —— 那些才是被禁止的。
 */
async function restorePick(node) {
  const selW = findWidget(node, "已选");
  const host = node.__dkHost;
  if (!selW) return;

  const sel = readSelection(selW);
  updateSelBadge(node, sel.ids.length);

  if (host) {
    // 切工作流后**保持展开**（旧行为是 setCollapsed(true)，每开一个工作流都得手点一次展开）。
    // restoreExpanded 内部错峰重建面板，重建完在回调里回填已选 ——
    // 面板是全新实例，选中高亮不会自己回来。
    host.restoreExpanded(() => {
      if (!host.panel) return;
      try {
        host.panel.setSelection(sel.ids.map((id, i) => ({ id, name: sel.names[i] || "" })));
      } catch (err) {
        console.warn("[dogking-tools] 回填已选失败", err);
      }
    });
    syncCollapseButton(node.__dkCollapseBtn, false);
    host.setStatus(sel.ids.length ? `已选 ${sel.ids.length} 张` : "");
  }

  applyPickVisibility(node);

  // ③ 补分类信息（只在「分类」不是全部、且还没拉过时）
  const catW = findWidget(node, "分类");
  const top = String((catW && catW.value) || ALL).trim();
  if (top !== ALL && !(Array.isArray(node.__dkCats) && node.__dkCats.length)) {
    try {
      await refreshSubCategories(node, true);
    } catch (err) {
      console.warn("[dogking-tools] 恢复分类失败（面板将显示全部）", err);
    }
  }
  // ④ 面板若已经建好（用户抢先展开了），用正确筛选重载一次
  if (host && host.panel) host.applyFilters();
}

app.registerExtension({
  name: "dogking.pv.pick",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (!nodeData || nodeData.name !== NODE) return;

    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const result = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;
      try {
        setupPick(this);
      } catch (err) {
        console.error("[dogking-tools] 节点 1 初始化失败", err);
      }
      return result;
    };

    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () {
      const result = onConfigure ? onConfigure.apply(this, arguments) : undefined;
      try {
        // restorePick 是 async（要补一次分类），别让 rejection 变成未捕获异常
        const pending = restorePick(this);
        if (pending && typeof pending.catch === "function") {
          pending.catch((err) => console.error("[dogking-tools] 节点 1 恢复失败", err));
        }
      } catch (err) {
        console.error("[dogking-tools] 节点 1 恢复失败", err);
      }
      return result;
    };

    const onExecuted = nodeType.prototype.onExecuted;
    nodeType.prototype.onExecuted = function (message) {
      const result = onExecuted ? onExecuted.apply(this, arguments) : undefined;
      try {
        const text = message && message.dogking_status ? message.dogking_status[0] : "";
        if (text && this.__dkHost) this.__dkHost.setStatus(text);
      } catch (err) {
        console.warn("[dogking-tools] 节点 1 状态更新失败", err);
      }
      return result;
    };

    const onDrawForeground = nodeType.prototype.onDrawForeground;
    nodeType.prototype.onDrawForeground = function (ctx) {
      const result = onDrawForeground ? onDrawForeground.apply(this, arguments) : undefined;
      try {
        if (this.flags && this.flags.collapsed) return result;
        if (this.__dkHost) this.__dkHost.drawStatus(ctx);
      } catch (err) {
        // 状态条画不出来不影响节点
      }
      return result;
    };
  },
});

export { setupPick, restorePick, resolveCategoryId };
