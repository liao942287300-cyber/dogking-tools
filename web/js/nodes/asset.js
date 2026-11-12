/**
 * dogking-tools —— 节点 3「资产图（单张）」前端（T04）
 *
 * 和节点 1 的差别：
 *   · 只有**一个**选择维度对（项目 + 分类，两个平级下拉，不联动）；
 *     用户要求「项目在分类的上一层」，所以项目排前面、面板工具条也显示 `项目 · 分类`
 *   · 强制单选（点一张即生效）
 *   · 面板有**两个状态**（见下）
 *
 * ## 🔴 两态：picking / viewing
 *
 * ```
 * 【选择态 picking】                【预览态 viewing】
 * ┌────────────────────┐          ┌────────────────────┐
 * │ 项目 ▸ 分类 ▸ 搜索  │          │                    │
 * │ ┌──┐┌──┐┌──┐┌──┐   │          │   ┌──────────┐     │
 * │ │  ││  ││  ││  │   │   ──→    │   │  已选大图 │     │
 * │ └──┘└──┘└──┘└──┘   │          │   └──────────┘     │
 * │  分页器 / 已选 N     │          │   一天先生仓库大叔  │
 * └────────────────────┘          │   3840×2160 · 9.5MB│
 *                                 │     [更改图片]      │
 *                                 └────────────────────┘
 * ```
 *
 * 状态机（4 条边，缺一条就会出现「点进预览出不来」或「选完还停在网格」）：
 *
 *   picking --(点一张卡片 + 点「确定」)--> viewing
 *   viewing --(点「更改图片」，**不改已选**)--> picking
 *   viewing --(清空 / 清单变空)--> picking
 *   打开工作流恢复：有已选 → viewing，无已选 → picking
 *
 * 为什么需要这个状态机：单张节点只输出一张图，用户在网格里其实**无法确认**
 * 「我到底选中了哪一张、它多大、是不是这张」—— 网格里 40 个 96px 缩略图
 * 长得都很像。选完立刻切成一张大图 + 尺寸体积，用户看一眼就知道对不对，
 * 不满意点「更改图片」回去换。这比「在网格里找哪张带蓝框」可靠得多。
 *
 * 复用 `panel-host.js`：面板挂载、收起/展开、状态条、恢复时的占位条都在那里，
 * 四个 PV 节点行为一致，不在每个节点里各写一遍。
 */

import { app, pvCategories, pvThumbUrl } from "../lib/api.js";
import { hideWidgetAsData } from "../lib/ui.js";
import {
  createPanelHost,
  readSelection,
  writeSelection,
  syncCollapseButton,
} from "./panel-host.js";

const NODE = "DogkingPVAsset";
const ALL = "全部";

/** 面板的两个状态 */
const MODE_PICKING = "picking";
const MODE_VIEWING = "viewing";

function findWidget(node, name) {
  return (node.widgets || []).find((w) => w.name === name) || null;
}

/** 体积显示：`84 KB` / `10.2 MB` */
function formatSize(bytes) {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n <= 0) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/**
 * 拉一次资产图的分类/项目，填进两个下拉。
 *
 * 资产图只有 `category` / `project` 两个**平级字符串维度**（不是两级分类），
 * 所以两个下拉互不联动，一次请求就能都填好。
 *
 * 只在「节点创建」和「用户手动刷新」时调 —— `onConfigure` 里只在缓存为空时补一次，
 * 保证打开工作流不会给每个节点都发请求。
 */
async function refreshFacets(node, keepCurrent = true) {
  const catW = findWidget(node, "分类");
  const projW = findWidget(node, "项目");
  const addrW = findWidget(node, "服务地址");
  if (!catW || !projW) return;

  const addr = String((addrW && addrW.value) || "");
  let facets = null;
  try {
    facets = await pvCategories({ addr, kind: "asset" });
  } catch (err) {
    console.warn("[dogking-tools] 拉资产分类失败，下拉保持原样", err);
    return;
  }
  if (!facets || !facets.ok) return;

  node.__dkFacets = facets;

  const cats = [ALL].concat((facets.categories || []).map((c) => String(c.path || "")).filter(Boolean));
  const projs = [ALL].concat((facets.projects || []).map((p) => String(p || "")).filter(Boolean));

  const applyOptions = (widget, options) => {
    if (!widget) return;
    const previous = String(widget.value || ALL);
    widget.options = widget.options || {};
    widget.options.values = options;
    // 原来选的值还在新列表里就保留，否则回「全部」
    widget.value = keepCurrent && options.includes(previous) ? previous : ALL;
  };
  const prevCat = String(catW.value || ALL);
  const prevProj = String(projW.value || ALL);
  applyOptions(catW, cats);
  applyOptions(projW, projs);
  node.setDirtyCanvas?.(true, true);

  // 选项变了、当前值被重置（原来选的项目/分类在新列表里没了）→ 面板必须跟着重筛。
  // 直接改 widget.value **不会**触发它的 callback，不补这一句就会停在旧值上筛出空列表。
  if (String(catW.value || ALL) !== prevCat || String(projW.value || ALL) !== prevProj) {
    const host = node.__dkHost;
    if (host) {
      try {
        host.applyFilters();
      } catch (err) {
        console.warn("[dogking-tools] 分类刷新后重载面板失败", err);
      }
    }
  }
}

/** 状态条：`已选 1 张：角色正面` */
function statusText(sel) {
  if (!sel.ids.length) return "";
  const name = (sel.names && sel.names[0]) || "未命名";
  return `已选 1 张：${name}`;
}

/* ---------------------------------------------------------------- 两态面板 */

/**
 * 取某一项的尺寸/体积。
 *
 * 优先级：
 *   ① `node.__dkSelMeta`（选中那一刻从面板条目里抄下来的，最可靠）
 *   ② 面板当前页的 `_items`（展开后、还没点选时也能拿到）
 *   ③ 都没有 → 返回 null，调用方只显示名称
 *
 * 为什么不用 `pv_meta` 现拉：那是一次额外网络往返，而「尺寸」只是锦上添花的信息；
 * 拿不到就少显示一行，**绝不能因此让预览态卡在加载中**。
 *
 * @returns {{width:number, height:number, size:number}|null}
 */
function metaOf(node, host, id) {
  const key = String(id || "");
  if (!key) return null;
  const cache = node.__dkSelMeta || {};
  if (cache[key]) return cache[key];
  const items = (host && host.panel && host.panel._items) || [];
  const hit = items.find((it) => String(it && it.id) === key);
  if (!hit) return null;
  return {
    width: Number(hit.width) || 0,
    height: Number(hit.height) || 0,
    size: Number(hit.size) || 0,
  };
}

/** 记住一次选中的尺寸/体积（面板条目里带着，顺手抄下来） */
function rememberMeta(node, selected) {
  const cache = node.__dkSelMeta || (node.__dkSelMeta = {});
  (selected || []).forEach((item) => {
    if (!item || item.id == null) return;
    if (item.width == null && item.height == null && item.size == null) return;
    cache[String(item.id)] = {
      width: Number(item.width) || 0,
      height: Number(item.height) || 0,
      size: Number(item.size) || 0,
    };
  });
}

/**
 * 预览态那一层（绝对定位盖住整个面板）。
 *
 * 为什么用 `position:absolute; inset:0` 而不是把网格 display:none 再塞个兄弟节点：
 * 面板根是 `display:flex; flex-direction:column`，高度由 CSS 变量钉死 600px。
 * 加一个 flex 子节点会让「工具栏 + 列表 + 预览块」三个一起分 600px，
 * 预览块被挤扁；absolute 覆盖层不参与 flex 布局，布局零风险。
 */
function ensureViewer(host) {
  if (!host || !host.panel || !host.panel.root) return null;
  const root = host.panel.root;
  const existing = root.querySelector(".dk-asset-viewer");
  if (existing && existing.isConnected) return existing;

  // 绝对定位需要定位祖先。`.dk-pv-panel` 的 CSS 没写 position，这里是 static。
  if (getComputedStyle(root).position === "static") root.style.position = "relative";

  const viewer = document.createElement("div");
  viewer.className = "dk-asset-viewer";
  viewer.style.cssText = [
    "position:absolute",
    "inset:0",
    "display:none",                 // 默认选择态：不显示
    "flex-direction:column",
    "align-items:center",
    "justify-content:flex-start",   // 图片区靠 flex:1 自己吃满，不靠居中
    "gap:10px",
    "padding:12px",
    "box-sizing:border-box",
    "background:var(--comfy-input-bg, #1d1e22)",
    "z-index:6",
  ].join(";");
  root.appendChild(viewer);
  return viewer;
}

/** 画预览态内容：大图 + 名称 + 尺寸/体积 + 「更改图片」 */
function renderViewer(node, host) {
  const viewer = ensureViewer(host);
  if (!viewer) return;

  const sel = readSelection(findWidget(node, "已选"));
  const id = sel.ids[0] || "";
  const name = sel.names[0] || "未命名";

  // 缩略图必须用 size=thumb：full 是 10MB 级的原图 PNG，
  // 当成节点内预览会直接把渲染进程拖住（而且完全没必要）。
  // 图片区**撑满整个面板**（用户要求「选完应该撑满画框」）。
  // 原来固定 320×320 会在 600px 高的面板里四周留一大片黑边，
  // 图本身只有中间一小块，白瞎了预览态的全部意义。
  // `flex:1 1 auto` + `min-height:0` 让它吃掉名称/尺寸/按钮之外的所有空间；
  // img 用 object-fit:contain 保持比例撑到最大，不裁切、不变形。
  const box = document.createElement("div");
  box.style.cssText = [
    "flex:1 1 auto",
    "width:100%",
    "min-height:0",
    "display:flex",
    "align-items:center",
    "justify-content:center",
    "border-radius:8px",
    "overflow:hidden",
    "background:rgba(255,255,255,.05)",
  ].join(";");

  if (id) {
    const img = document.createElement("img");
    img.style.cssText =
      "max-width:100%;max-height:100%;width:100%;height:100%;object-fit:contain;display:block";
    img.decoding = "async";
    img.alt = "";
    img.draggable = false;
    img.addEventListener("error", () => {
      img.remove();
      box.textContent = "🖼";
      box.style.cssText += ";opacity:.35;font-size:40px";
    });
    // 🔴 预览态必须用 **原图**（size=full），不能用缩略图。
    // 缩略图只有 480px 宽，撑到 700+ 宽的面板里会糊成一团 ——
    // 用户要的是「确认这张图对不对」，糊了就失去预览的意义。
    // 只加载一张（不是 40 张），且后端有 Cache-Control: max-age=600，
    // 同一个 id 反复开关面板不会重复下载。
    img.src = pvThumbUrl({ id, kind: "asset", size: "full" });
    box.appendChild(img);
  } else {
    box.textContent = "还没有选择资产图";
    box.style.cssText += ";opacity:.55;font-size:12px";
  }

  // 名称 + 尺寸 + 按钮**压成一行**：原来三行竖排要占 ~90px，
  // 现在一行 ~36px，省下的高度全给图片（用户就是要看大图）。
  const infoRow = document.createElement("div");
  infoRow.style.cssText =
    "flex:0 0 auto;display:flex;align-items:center;gap:10px;width:100%;min-width:0";

  const title = document.createElement("div");
  title.style.cssText =
    "flex:1 1 auto;min-width:0;font-size:14px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap";
  title.textContent = name;
  title.title = name;

  const metaEl = document.createElement("div");
  metaEl.style.cssText = "flex:0 0 auto;font-size:11px;opacity:.65;white-space:nowrap";
  const meta = id ? metaOf(node, host, id) : null;
  const bits = [];
  if (meta && meta.width && meta.height) bits.push(`${meta.width}×${meta.height}`);
  if (meta) {
    const sizeText = formatSize(meta.size);
    if (sizeText) bits.push(sizeText);
  }
  // 拿不到尺寸就只显示名称（**不显示「尺寸未知」这种噪声**，用户只关心图对不对）
  metaEl.textContent = bits.join(" · ");
  metaEl.style.display = bits.length ? "" : "none";

  const changeBtn = document.createElement("button");
  changeBtn.type = "button";
  changeBtn.className = "dk-pv-btn";
  changeBtn.textContent = "更改图片";
  changeBtn.style.cssText += ";height:28px;padding:0 16px;flex:0 0 auto";
  changeBtn.addEventListener("click", () => {
    // 只切状态，**绝不改已选** —— 用户点「更改图片」是「我还没决定」，不是「取消选择」。
    // 同时进「替换模式」：网格里点一张就**直接替换并回预览**，
    // 不用再找「哪张是当前选中的」——之前停在网格态用户会以为预览坏了。
    node.__dkAssetReplaceMode = true;
    setAssetMode(node, host, MODE_PICKING);
    try {
      if (host.panel && host.panel.crumbEl) {
        host.panel.crumbEl.textContent = "点一张替换当前图片";
        host.panel.crumbEl.style.color = "#ffc857";
      }
    } catch (err) { /* 提示失败不影响功能 */ }
  });

  infoRow.append(title, metaEl, changeBtn);
  viewer.replaceChildren(box, infoRow);
}

/**
 * 切换面板状态。
 *
 * @param {object} node
 * @param {object} host
 * @param {string} mode 'picking' | 'viewing'
 */
function setAssetMode(node, host, mode) {
  const next = mode === MODE_VIEWING ? MODE_VIEWING : MODE_PICKING;
  node.__dkAssetMode = next;

  const viewer = ensureViewer(host);
  if (!viewer) return;   // 面板还没建（收起状态）→ 状态记在 node 上，展开时再套用

  const root = host.panel.root;
  const viewing = next === MODE_VIEWING;

  viewer.style.display = viewing ? "flex" : "none";
  // 预览态把「工具条 + 网格 + 分页器」整块藏起来（用户要的是「只看这一张」）
  const toolbar = root.querySelector(".dk-pv-toolbar");
  const footer = root.querySelector(".dk-pv-footer");
  [toolbar, host.panel.scrollEl, footer].forEach((el) => {
    if (el) el.style.display = viewing ? "none" : "";
  });

  if (viewing) renderViewer(node, host);
  node.setDirtyCanvas?.(true, true);
}

/**
 * 按 `node.__dkAssetMode`（默认 picking）套用一次状态。
 *
 * 展开面板时必须调一次：`host.rebuild()` 会新建一个 PvPanel，
 * 覆盖层和 display 状态全丢，不重新套用就会「明明选了图，展开却还是网格」。
 */
function applyAssetMode(node, host) {
  const mode = node.__dkAssetMode === MODE_VIEWING ? MODE_VIEWING : MODE_PICKING;
  setAssetMode(node, host, mode);
}

/** 节点 3 初始化 */
function setupAsset(node) {
  const selW = findWidget(node, "已选");
  const catW = findWidget(node, "分类");
  const projW = findWidget(node, "项目");
  const addrW = findWidget(node, "服务地址");

  if (!selW) {
    console.warn("[dogking-tools] 节点 3 找不到「已选」widget，跳过面板挂载");
    return;
  }

  hideWidgetAsData(selW); // 已选：隐藏但仍序列化

  // 初始状态：有已选就进预览态，否则选择态。
  // （新节点清单是空的，一定是 picking；打开工作流时 restoreAsset 会再算一次）
  node.__dkAssetMode = readSelection(selW).ids.length ? MODE_VIEWING : MODE_PICKING;
  node.__dkSelMeta = node.__dkSelMeta || {};

  const host = createPanelHost(node, {
    kind: "asset",
    view: "grid",
    pageSize: 40,
    maxSelect: 1,
    getMode: () => "single",
    getFilters: () => {
      const cat = String((catW && catW.value) || ALL).trim();
      const proj = String((projW && projW.value) || ALL).trim();
      const catText = cat === ALL ? "全部" : cat;
      const projText = proj === ALL ? "全部" : proj;
      return {
        addr: String((addrW && addrW.value) || ""),
        // 资产图的「分类」是**字符串名**（不是 id），直接透传给 pv_list
        category: cat === ALL ? "" : cat,
        project: proj === ALL ? "" : proj,
        // 给 UI 的完整路径。节点 3 的维度顺序是「项目 → 分类」，
        // 所以工具条也按 `项目 · 分类` 显示（面板不写死顺序，由这里给 crumb）。
        categoryPath: cat === ALL ? "" : (proj === ALL ? cat : `${proj} / ${cat}`),
        crumb: `${projText} · ${catText}`,
      };
    },
    // 点「确定」才写回（面板里的选中是草稿）→ 写回后立刻切到预览态。
    // 「取消」不写回，用户留在网格态，面板会把选中集退回上次确定的那张。
    // 实时确认：点一张即写回 + 切预览态，底栏只有「清空」
    liveConfirm: true,
    onConfirm: (selected) => {
      const one = selected.length ? [selected[0]] : [];
      rememberMeta(node, one);          // 顺手记下尺寸/体积，预览态要用
      const count = writeSelection(selW, one);
      const sel = readSelection(selW);
      host.setStatus(count ? statusText(sel) : "未选择资产图");
      // 替换模式结束：把「点一张替换当前图片」撤掉，恢复正常面包屑
      if (node.__dkAssetReplaceMode) {
        node.__dkAssetReplaceMode = false;
        try {
          if (host.panel && host.panel.crumbEl) host.panel.crumbEl.style.color = "";
          if (host.panel && typeof host.panel._updateCrumb === "function") host.panel._updateCrumb();
        } catch (err) { /* ignore */ }
      }
      // 选到了 → viewing；清空了 → 回 picking（不清空就永远留在预览态出不来）
      setAssetMode(node, host, count ? MODE_VIEWING : MODE_PICKING);
    },
    // 实时确认下 onChange 和 onConfirm 同一次点击都会触发：
    // onChange 只更新状态条，onConfirm 负责写回 + 切预览态。
    onChange: (selected) => {
      host.setStatus(selected.length ? "已选 1 张" : "未选择资产图");
    },
  });
  node.__dkHost = host;

  const collapseBtn = node.addWidget("button", "收起面板", null, () => {
    const collapsed = host.toggleCollapsed();
    syncCollapseButton(collapseBtn, collapsed);
    if (!collapsed) {
      // 展开 = 用户主动要看数据 → 顺手把「项目/分类」下拉的选项刷新一次。
      // 这两个下拉是从资产列表里聚合的（PromptVault 没有独立接口），
      // 不刷新的话用户新建了项目、改了归属，节点上永远看不到。
      // 一次 2KB，不阻塞面板展开；失败时 refreshFacets 内部会保留旧值。
      refreshFacets(node, true);
      // 展开会重建面板（覆盖层没了）→ 重新套用状态。
      // 顺便把已选回填进面板：这样从预览态点「更改图片」回到网格时，
      // 当前那张是带蓝框的，用户一眼能看出「现在选的是它」。
      setTimeout(() => {
        if (!host.panel) return;
        const sel = readSelection(selW);
        try {
          host.panel.setSelection(sel.ids.slice(0, 1).map((id, i) => ({ id, name: sel.names[i] || "" })));
        } catch (err) {
          console.warn("[dogking-tools] 节点 3 回填已选失败", err);
        }
        applyAssetMode(node, host);
      }, 0);
    }
  });
  collapseBtn.serialize = false;
  node.__dkCollapseBtn = collapseBtn;

  const refreshBtn = node.addWidget("button", "刷新分类", null, () => {
    refreshFacets(node, true);
  });
  refreshBtn.serialize = false;

  // 两个下拉都只是筛选条件，变了就让面板回到第 1 页重拉（保留已选）
  [catW, projW].forEach((widget) => {
    if (!widget) return;
    const previous = widget.callback;
    widget.callback = function () {
      const result = previous ? previous.apply(this, arguments) : undefined;
      host.applyFilters();
      return result;
    };
  });

  // 首次拉一次分类/项目（节点创建是用户动作，这一次请求值得）
  refreshFacets(node, true);
  syncCollapseButton(collapseBtn, host.collapsed);
}

/** 恢复工作流：回填已选 + 套用两态 + 占位条，不拉 pv_list */
async function restoreAsset(node) {
  const selW = findWidget(node, "已选");
  const host = node.__dkHost;
  if (!selW) return;

  const sel = readSelection(selW);

  // 有已选 → 直接进预览态；无已选 → 选择态。这是状态机的「恢复」边。
  node.__dkAssetMode = sel.ids.length ? MODE_VIEWING : MODE_PICKING;

  if (host) {
    // 切工作流后保持展开（旧行为是 setCollapsed(true)，每次打开都得手点一次展开）
    host.restoreExpanded(() => {
      if (!host.panel) return;
      try {
        host.panel.setSelection(sel.ids.slice(0, 1).map((id, i) => ({ id, name: sel.names[i] || "" })));
      } catch (err) {
        console.warn("[dogking-tools] 节点 3 回填已选失败", err);
      }
      // 面板是新建的 → 两态显示状态全丢，必须重套
      applyAssetMode(node, host);
    });
    syncCollapseButton(node.__dkCollapseBtn, false);
    host.setStatus(statusText(sel));
  }

  // 分类/项目下拉：缓存为空才补一次（否则保持 INPUT_TYPES 给的选项，零请求）
  const catW = findWidget(node, "分类");
  const hasOptions = catW && catW.options && (catW.options.values || []).length > 1;
  if (!hasOptions && !node.__dkFacets) {
    try {
      await refreshFacets(node, true);
    } catch (err) {
      console.warn("[dogking-tools] 节点 3 恢复分类失败", err);
    }
  }

  // 硬结论 4「状态变化 ≠ UI 重载」：补完状态必须显式通知面板重载。
  // 面板此刻通常是收起/未建的（applyFilters 是 no-op），但用户若抢先展开了，
  // 这一步才保证用的是刚补好的筛选条件。
  if (host && host.panel) host.applyFilters();
}

app.registerExtension({
  name: "dogking.pv.asset",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (!nodeData || nodeData.name !== NODE) return;

    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const result = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;
      try {
        setupAsset(this);
      } catch (err) {
        console.error("[dogking-tools] 节点 3 初始化失败", err);
      }
      return result;
    };

    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () {
      const result = onConfigure ? onConfigure.apply(this, arguments) : undefined;
      try {
        const pending = restoreAsset(this);
        if (pending && typeof pending.catch === "function") {
          pending.catch((err) => console.error("[dogking-tools] 节点 3 恢复失败", err));
        }
      } catch (err) {
        console.error("[dogking-tools] 节点 3 恢复失败", err);
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
        console.warn("[dogking-tools] 节点 3 状态更新失败", err);
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

export { setupAsset, restoreAsset, refreshFacets, statusText, formatSize };
