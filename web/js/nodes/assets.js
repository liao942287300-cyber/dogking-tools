/**
 * dogking-tools —— 节点 4「资产图多重」前端（T04）
 *
 * 核心是**动态输出口**：最多 9 个图片口，**没有固定口**（名称/总数/状态已按用户要求删除，
 * 「输出模式」参数也一并删除 —— 只剩独立输出口一种行为）。
 *
 * 🔴 纯尾部增删，绝不删中间槽位：
 *    `removeOutput(i)` 会让 i 之后所有槽位左移重编号，删中间等于把后面所有
 *    连线全部断掉，而且**不可撤销**（用户手工会疯）。所以只从末尾删。
 *    因为现在整张输出表都是图片口，尾部就是全部 —— 这条约束比原来更容易守。
 *
 * 🔴 端口 label 必做：`图2 · 角色四视图-侧面`
 *    端口竖排时用户要能一眼看出哪个口是哪张图，不能让他去数序号。
 *    名称**不再有输出口**，但 label 里带上名字对辨认很有用，所以保留。
 *
 * 4 个同步时机（少一个就会出现「清单和口数对不上」）：
 *    ① onNodeCreated  ② onConfigure（setTimeout 0）
 *    ③ 点「确定」写回（面板里的选中是草稿，确定后才生效）  ④ 缩略图条 × 移除
 *    ⑤ 选定态里「更换 / 移除」单张
 */

import { app, pvCategories, pvThumbUrl } from "../lib/api.js";
import { hideWidgetAsData } from "../lib/ui.js";
import {
  createPanelHost,
  readSelection,
  writeSelection,
  syncCollapseButton,
} from "./panel-host.js";

const NODE = "DogkingPVAssets";
const ALL = "全部";
// 🔴 必须和 `pv_asset_nodes.py` 的 `MAX_IMAGE_PORTS` 一致。
// 后端决定 RETURN_TYPES 长度（真有 9 个口），这里决定显示几个 + 状态文案里的数字。
const MAX_PORTS = 9;

function findWidget(node, name) {
  return (node.widgets || []).find((w) => w.name === name) || null;
}

/** 拉资产图的分类/项目（两个平级下拉，不联动） */
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

/**
 * 状态条：`已选 4 张：正面 / 侧面 / 背面 / 45°`
 * 超过 3 张截断：`已选 6 张：正面 / 侧面 / 背面 等 6 张`
 */
function statusText(sel) {
  const count = sel.ids.length;
  if (!count) return "";
  const names = (sel.names || []).filter(Boolean);
  // 超过上限时**必须在这里就说清楚**，不能等执行完才由后端拼「仅前 N 张」——
  // 用户点完确定就应该知道「剩下的不会输出」，否则他会以为 10 张全接上了。
  const over = count > MAX_PORTS ? `（只输出前 ${MAX_PORTS} 张）` : "";
  if (!names.length) return `已选 ${count} 张${over}`;
  if (names.length <= 3) {
    return `已选 ${count} 张${over}：${names.join(" / ")}`;
  }
  return `已选 ${count} 张${over}：${names.slice(0, 3).join(" / ")} 等 ${count} 张`;
}

/**
 * 把输出口数量对齐到「已选清单」。
 *
 * 整张输出表都是图片口，所以只动尾部 = 动全部。
 * 这样任何一次增删都不会碰中间槽位，已连的下游连线只会在「删掉最后一张」时断开。
 */
function syncAssetOutputs(node) {
  const selW = findWidget(node, "已选");
  if (!selW || !Array.isArray(node.outputs)) return;

  const sel = readSelection(selW);
  const count = sel.ids.length;

  // 没有固定口了：口数 = min(已选张数, 8)
  const want = count ? Math.min(count, MAX_PORTS) : 0;
  const cur = node.outputs.length;

  // 从尾部删（**绝不能删中间**：removeOutput 会让后面所有槽位左移重编号）
  for (let i = cur; i > want; i -= 1) {
    node.removeOutput(node.outputs.length - 1);
  }
  // 从尾部加
  for (let i = cur; i < want; i += 1) {
    node.addOutput(`图${i + 1}`, "IMAGE", {});
  }

  // 🔴 统一刷新**所有**口的 label，不只是新加的那几个。
  // 原因：删掉中间某一项后，每个口承载的图会整体前移（口是位置语义），
  // 不刷新就会出现「图2 的标签还写着上一张的名字」。
  for (let i = 0; i < want; i += 1) {
    const port = node.outputs[i];
    if (!port) continue;
    const index = i + 1;
    // 名称后缀是 label 的一部分（不是输出口），对辨认很有帮助，保留
    const name = String((sel.names && sel.names[i]) || "");
    port.label = name ? `图${index} · ${name.slice(0, 8)}` : `图${index}`;
  }

  node.setDirtyCanvas?.(true, true);
}

/** 缩略图条容器（插在面板列表和底栏之间，48px） */
function ensureStrip(host) {
  if (!host || !host.panel || !host.panel.root) return null;
  const existing = host.el.querySelector(".dk-asset-strip");
  if (existing && existing.isConnected) return existing;

  const strip = document.createElement("div");
  strip.className = "dk-asset-strip";
  strip.style.cssText = [
    "flex:0 0 auto",
    "height:48px",
    "display:flex",
    "align-items:center",
    "gap:6px",
    "padding:4px 8px",
    "overflow-x:auto",
    "overflow-y:hidden",
    "border-top:1px solid rgba(255,255,255,.08)",
    "background:rgba(255,255,255,.02)",
  ].join(";");
  try {
    // 放在 footer 之前（root 里是 toolbar / scroll / footer）
    const footer = host.panel.root.lastElementChild;
    host.panel.root.insertBefore(strip, footer || null);
  } catch (err) {
    return null;
  }
  return strip;
}

/**
 * 面板内的确认框（不用 window.confirm）。
 *
 * 为什么自己画：`window.confirm` 在 Electron / 无头浏览器里会阻塞整个渲染进程，
 * 而且样式和面板完全脱节。这里做成面板内的一层遮罩，事件也天然被
 * panel-host 的 pointerdown 隔离挡住，不会拖动画布。
 *
 * @param {object} host 面板宿主
 * @param {string} message 多行文案（用 \n 换行）
 * @param {object} [opts] {okText, cancelText}
 * @returns {Promise<boolean>} 用户是否确认
 */
function confirmInPanel(host, message, opts) {
  const options = opts || {};
  const panelRoot = host && host.panel && host.panel.root;
  if (!panelRoot) {
    // 面板还没建（理论上点不到 ×），退化成原生确认框
    return Promise.resolve(window.confirm(message));
  }

  return new Promise((resolve) => {
    // 遮罩要 absolute 定位，父级必须是定位祖先
    if (getComputedStyle(panelRoot).position === "static") {
      panelRoot.style.position = "relative";
    }

    const overlay = document.createElement("div");
    overlay.className = "dk-asset-confirm";
    overlay.style.cssText = [
      "position:absolute",
      "inset:0",
      "background:rgba(0,0,0,.62)",
      "display:flex",
      "align-items:center",
      "justify-content:center",
      "z-index:30",
    ].join(";");

    const box = document.createElement("div");
    box.style.cssText = [
      "max-width:80%",
      "background:#23242a",
      "border:1px solid rgba(255,255,255,.16)",
      "border-radius:8px",
      "padding:14px 16px",
      "display:flex",
      "flex-direction:column",
      "gap:12px",
      "box-shadow:0 8px 28px rgba(0,0,0,.5)",
    ].join(";");

    const text = document.createElement("div");
    text.style.cssText = "font-size:12px;line-height:1.65;white-space:pre-wrap";
    text.textContent = String(message || "");

    const actions = document.createElement("div");
    actions.style.cssText = "display:flex;gap:8px;justify-content:flex-end";

    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "dk-asset-confirm-cancel";
    cancel.textContent = options.cancelText || "取消";
    cancel.style.cssText =
      "height:26px;padding:0 12px;border-radius:5px;border:1px solid rgba(255,255,255,.18);background:rgba(255,255,255,.06);color:inherit;font:inherit;cursor:pointer";

    const ok = document.createElement("button");
    ok.type = "button";
    ok.className = "dk-asset-confirm-ok";
    ok.textContent = options.okText || "确定";
    ok.style.cssText =
      "height:26px;padding:0 12px;border-radius:5px;border:1px solid #a8433c;background:#c0392b;color:#fff;font:inherit;cursor:pointer";

    const finish = (value) => {
      overlay.remove();
      resolve(Boolean(value));
    };
    cancel.addEventListener("click", (event) => {
      event.stopPropagation();
      finish(false);
    });
    ok.addEventListener("click", (event) => {
      event.stopPropagation();
      finish(true);
    });

    actions.append(cancel, ok);
    box.append(text, actions);
    overlay.appendChild(box);
    panelRoot.appendChild(overlay);
  });
}

/**
 * 单张移除：改清单 → 重画缩略图条 → 同步输出口 → 更新状态条
 *
 * ⚠️ 删**中间项**会让后面每张整体前移（输出口是位置语义，不是 id 语义），
 * 用户看到的却是「我删了第 2 张，怎么第 4 条的线断了」。所以中间项要先确认。
 */
async function removeAt(node, index) {
  const selW = findWidget(node, "已选");
  const host = node.__dkHost;
  if (!selW) return;

  const sel = readSelection(selW);
  const ids = sel.ids.slice();
  const names = sel.names.slice();
  if (index < 0 || index >= ids.length) return;

  const shifted = ids.length - index - 1;      // 后面还有几张要前移
  const imagePorts = node.outputs ? node.outputs.length : 0;

  // 只有「删的不是最后一个」且「确实有多于一个图片口」时才需要确认：
  // 删最后一个只断它自己的线，不前移，不打扰用户。
  if (shifted > 0 && imagePorts > 1) {
    const okToGo = await confirmInPanel(
      host,
      `删除第 ${index + 1} 张会让后面 ${shifted} 张整体前移，\n` +
      `相关输出口的连线可能错位。\n\n继续删除？`,
      { okText: "删除并前移", cancelText: "取消" }
    );
    if (!okToGo) return;
  }

  ids.splice(index, 1);
  names.splice(index, 1);

  selW.value = JSON.stringify({ ids, names });

  if (host && host.panel) {
    try {
      host.panel.setSelection(ids.map((id, i) => ({ id, name: names[i] || "" })));
    } catch (err) {
      console.warn("[dogking-tools] 节点 4 回填选中失败", err);
    }
  }
  renderStrip(node, host, { ids, names });
  syncAssetOutputs(node);              // ④ 缩略图条移除
  if (node.__dkAssetsMode === MODE_SELECTED) renderSelectedView(node, host);
  if (host) {
    // 前移过的要明确告诉用户发生了什么，否则他会以为删错了
    host.setStatus(shifted > 0
      ? `已删除第 ${index + 1} 张，后续 ${shifted} 张已前移`
      : statusText({ ids, names }));
  }
}

/** 重画 48px 缩略图条（每张右上角 × 可单独移除） */
function renderStrip(node, host, sel) {
  const strip = ensureStrip(host);
  if (!strip) return;
  strip.replaceChildren();

  if (!sel.ids.length) {
    const empty = document.createElement("div");
    empty.style.cssText = "font-size:11px;opacity:.5;padding:0 4px";
    empty.textContent = "已选 0 张";
    strip.appendChild(empty);
    return;
  }

  sel.ids.forEach((id, index) => {
    const cell = document.createElement("div");
    cell.style.cssText =
      "position:relative;flex:0 0 auto;width:40px;height:40px;border-radius:4px;overflow:hidden;background:rgba(255,255,255,.06)";

    const img = document.createElement("img");
    // contain 不是 cover：选定态是「核对选了哪几张」，裁掉边缘等于看不清
    img.style.cssText = "width:100%;height:100%;object-fit:contain;display:block";
    img.decoding = "async";
    img.loading = "lazy";
    img.alt = "";
    img.addEventListener("error", () => {
      img.remove();
      cell.textContent = "🖼";
      cell.style.cssText += ";display:flex;align-items:center;justify-content:center;opacity:.35";
    });
    img.src = pvThumbUrl({ id, kind: "asset", size: "thumb" });
    img.title = String((sel.names && sel.names[index]) || id);
    cell.appendChild(img);

    const x = document.createElement("button");
    x.type = "button";
    x.textContent = "×";
    x.title = "移除这张";
    x.style.cssText = [
      "position:absolute",
      "top:0",
      "right:0",
      "width:14px",
      "height:14px",
      "padding:0",
      "border:0",
      "border-radius:0 0 0 4px",
      "background:rgba(0,0,0,.62)",
      "color:#fff",
      "font-size:11px",
      "line-height:14px",
      "cursor:pointer",
    ].join(";");
    x.addEventListener("click", (event) => {
      event.stopPropagation();
      removeAt(node, index);
    });
    cell.appendChild(x);

    strip.appendChild(cell);
  });
}

/* ==================================================================
 *  选定态（点「确定」之后）
 *
 *  用户要求：「确定后图片为选定状态，可以支持只更改其中一个」。
 *  所以确定后网格收起，换成一张张已选卡片，每张带「更换 / 移除」。
 *  「更换」只替换那一张 —— 不用把 4 张全部重选。
 * ================================================================== */

const MODE_GRID = "grid";
const MODE_SELECTED = "selected";

/** 选定态覆盖层（和节点 3 的预览层同一个做法：绝对定位铺满面板） */
function ensureSelectedView(host) {
  if (!host || !host.panel || !host.panel.root) return null;
  const root = host.panel.root;
  const existing = root.querySelector(".dk-assets-selected");
  if (existing && existing.isConnected) return existing;

  // 绝对定位要有定位祖先（.dk-pv-panel 的 position 是 static）
  if (getComputedStyle(root).position === "static") root.style.position = "relative";

  const view = document.createElement("div");
  view.className = "dk-assets-selected";
  view.style.cssText = [
    "position:absolute",
    "inset:0",
    "display:none",                 // 默认网格态
    "flex-direction:column",
    "gap:10px",
    "padding:12px",
    "box-sizing:border-box",
    "background:var(--comfy-input-bg, #1d1e22)",
    "z-index:6",
  ].join(";");
  root.appendChild(view);
  return view;
}

/** 重画选定态：已选卡片 + 更换/移除 + 重新选择 */
function renderSelectedView(node, host) {
  const view = ensureSelectedView(host);
  if (!view) return;
  const sel = readSelection(findWidget(node, "已选"));
  view.replaceChildren();

  const head = document.createElement("div");
  head.style.cssText =
    "flex:0 0 auto;display:flex;align-items:center;gap:8px;font-size:12px;opacity:.8";
  head.textContent = sel.ids.length
    ? `已选定 ${sel.ids.length} 张${sel.ids.length > MAX_PORTS ? `（只输出前 ${MAX_PORTS} 张）` : ""}`
    : "未选择资产图";
  view.appendChild(head);

  const grid = document.createElement("div");
  grid.style.cssText = [
    "flex:1 1 auto",
    "min-height:0",
    "overflow-y:auto",
    "display:grid",
    // 卡片放大到 180px：96px 的缩略图在选定态里根本看不清是哪张
    // （用户要「确定后能看清选了哪些」），这里不是网格浏览、是核对清单。
    "grid-template-columns:repeat(auto-fill, minmax(180px, 1fr))",
    "gap:10px",
    "align-content:start",
    "scrollbar-width:thin",
  ].join(";");
  view.appendChild(grid);

  sel.ids.forEach((id, index) => {
    const card = document.createElement("div");
    card.style.cssText = "display:flex;flex-direction:column;gap:4px";

    const box = document.createElement("div");
    box.style.cssText =
      "position:relative;width:100%;aspect-ratio:1/1;border-radius:5px;overflow:hidden;background:rgba(255,255,255,.06)";
    const img = document.createElement("img");
    img.style.cssText = "width:100%;height:100%;object-fit:cover;display:block";
    img.decoding = "async";
    img.loading = "lazy";
    img.alt = "";
    img.addEventListener("error", () => {
      img.remove();
      box.textContent = "🖼";
      box.style.cssText += ";display:flex;align-items:center;justify-content:center;opacity:.35";
    });
    img.src = pvThumbUrl({ id, kind: "asset", size: "thumb" });
    box.appendChild(img);

    // 左上角序号：和输出口「图N」对得上，用户才知道哪张接到哪个口
    const badge = document.createElement("div");
    badge.textContent = String(index + 1);
    badge.style.cssText = [
      "position:absolute", "top:0", "left:0",
      "min-width:22px", "height:22px", "padding:0 5px",
      "border-radius:0 0 5px 0",
      "background:rgba(47,111,237,.9)", "color:#fff",
      "font-size:12px", "line-height:22px", "text-align:center",
    ].join(";");
    box.appendChild(badge);
    card.appendChild(box);

    const nameEl = document.createElement("div");
    nameEl.textContent = String((sel.names && sel.names[index]) || id);
    nameEl.title = nameEl.textContent;
    nameEl.style.cssText =
      "font-size:12px;opacity:.8;overflow:hidden;text-overflow:ellipsis;white-space:nowrap";
    card.appendChild(nameEl);

    const actions = document.createElement("div");
    actions.style.cssText = "display:flex;gap:4px";

    const changeBtn = document.createElement("button");
    changeBtn.type = "button";
    changeBtn.textContent = "更换";
    changeBtn.title = `只替换第 ${index + 1} 张`;
    changeBtn.style.cssText =
      "flex:1 1 0;height:28px;padding:0;border:1px solid rgba(255,255,255,.16);border-radius:4px;background:rgba(255,255,255,.06);color:inherit;font-size:12px;cursor:pointer";
    changeBtn.addEventListener("click", () => beginReplace(node, host, index));
    actions.appendChild(changeBtn);

    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.textContent = "移除";
    delBtn.style.cssText =
      "flex:1 1 0;height:28px;padding:0;border:1px solid rgba(255,255,255,.16);border-radius:4px;background:rgba(255,255,255,.06);color:inherit;font-size:12px;cursor:pointer";
    delBtn.addEventListener("click", () => removeAt(node, index));
    actions.appendChild(delBtn);

    card.appendChild(actions);
    grid.appendChild(card);
  });

  const foot = document.createElement("div");
  foot.style.cssText = "flex:0 0 auto;display:flex;justify-content:flex-end;gap:8px";
  const backBtn = document.createElement("button");
  backBtn.type = "button";
  backBtn.className = "dk-pv-btn";
  backBtn.textContent = "重新选择";
  backBtn.addEventListener("click", () => {
    // 「重新选择」= **清空重来**（用户明确要求，不是「保留已选继续加」）。
    // 三个状态要一起清，少一个就会出现「面板空了但输出口还在」这种鬼状态：
    //   ① node 上的替换状态  ② 面板内部选中  ③ widget + 输出口 + 缩略图条
    node.__dkReplaceIndex = -1;
    node.__dkReplaceBase = null;
    clearReplaceHint(host);
    try {
      if (host && host.panel) host.panel.setSelection([]);
    } catch (err) {
      console.warn("[dogking-tools] 清空面板选中失败", err);
    }
    const selW = findWidget(node, "已选");
    writeSelection(selW, []);
    syncAssetOutputs(node);
    renderStrip(node, host, { ids: [], names: [] });
    host.setStatus("未选择资产图");
    setAssetsMode(node, host, MODE_GRID);
  });
  foot.appendChild(backBtn);
  view.appendChild(foot);
}

/**
 * 切换节点 4 的面板状态。
 *
 * @param {object} node
 * @param {object} host
 * @param {string} mode 'grid' | 'selected'
 */
function setAssetsMode(node, host, mode) {
  const next = mode === MODE_SELECTED ? MODE_SELECTED : MODE_GRID;
  node.__dkAssetsMode = next;

  const view = ensureSelectedView(host);
  if (!view) return;   // 面板没建（收起状态）→ 状态记在 node 上，展开时套用

  const root = host.panel.root;
  const selected = next === MODE_SELECTED;

  view.style.display = selected ? "flex" : "none";
  // 选定态把「工具条 + 网格 + 分页器」整块藏起来（此刻用户只关心选了哪几张）
  const toolbar = root.querySelector(".dk-pv-toolbar");
  const footer = root.querySelector(".dk-pv-footer");
  [toolbar, host.panel.scrollEl, footer].forEach((el) => {
    if (el) el.style.display = selected ? "none" : "";
  });

  if (selected) renderSelectedView(node, host);
  node.setDirtyCanvas?.(true, true);
}

/** 展开面板时按 node.__dkAssetsMode 套用一次（rebuild 会新建 PvPanel，display 状态全丢） */
function applyAssetsMode(node, host) {
  const mode = node.__dkAssetsMode === MODE_SELECTED ? MODE_SELECTED : MODE_GRID;
  setAssetsMode(node, host, mode);
}

/**
 * 退出替换模式：把工具条从「正在替换第 N 张」恢复成正常的筛选面包屑。
 *
 * `_updateCrumb()` 会重新读 `filters().crumb` 拼文字，所以只要调它一次就行；
 * 黄色也要手动清掉（那是 beginReplace 里加的）。
 *
 * 漏了这步的症状：用户点「重新选择」回到网格，工具条还挂着
 * 「正在替换第 3 张，点一张替换」—— 但他其实已经不在替换模式了，点哪张都是加选。
 */
function clearReplaceHint(host) {
  try {
    if (!host || !host.panel) return;
    if (host.panel.crumbEl) host.panel.crumbEl.style.color = "";
    if (typeof host.panel._updateCrumb === "function") host.panel._updateCrumb();
  } catch (err) {
    console.warn("[dogking-tools] 恢复工具条失败", err);
  }
}

/** 进入替换模式：记住要替换的位置，回网格让用户点一张 */
function beginReplace(node, host, index) {
  node.__dkReplaceIndex = index;
  node.__dkReplaceBase = readSelection(findWidget(node, "已选"));
  setAssetsMode(node, host, MODE_GRID);
  // 工具条提示（crumb 是面板里唯一常驻的文字位）
  try {
    if (host.panel && host.panel.crumbEl) {
      host.panel.crumbEl.textContent = `正在替换第 ${index + 1} 张，点一张替换`;
      host.panel.crumbEl.style.color = "#ffc857";
    }
  } catch (err) { /* 提示失败不影响功能 */ }
}

/** 节点 4 初始化 */
function setupAssets(node) {
  const selW = findWidget(node, "已选");
  const catW = findWidget(node, "分类");
  const projW = findWidget(node, "项目");
  const addrW = findWidget(node, "服务地址");

  if (!selW) {
    console.warn("[dogking-tools] 节点 4 找不到「已选」widget，跳过面板挂载");
    return;
  }

  hideWidgetAsData(selW); // 已选：隐藏但仍序列化

  const host = createPanelHost(node, {
    kind: "asset",
    view: "grid",
    pageSize: 40,
    // ⚠️ 这里**不能**设成 8（图片口数量）。
    // 设成 MAX_PORTS 的话用户根本选不到第 10 张，「已选 10 张，仅前 9 张有独立输出口」
    // 这句提示就永远不可能出现 —— 而它存在的意义正是告诉用户「超出的不会输出」。
    // 图片口只有 MAX_PORTS 个是**输出**限制，不是**选择**限制。
    maxSelect: 200,
    getMode: () => "multi",
    getFilters: () => {
      const cat = String((catW && catW.value) || ALL).trim();
      const proj = String((projW && projW.value) || ALL).trim();
      return {
        addr: String((addrW && addrW.value) || ""),
        category: cat === ALL ? "" : cat,
        project: proj === ALL ? "" : proj,
        // 工具条顺序跟节点参数一致：项目在前、分类在后（和节点 3 统一）
        categoryPath: cat === ALL ? "" : (proj === ALL ? cat : `${proj} / ${cat}`),
        crumb: `${proj === ALL ? "全部" : proj} · ${cat === ALL ? "全部" : cat}`,
      };
    },
    // 点「确定」才写回 —— 面板里的选中是草稿，确定后才生效。
    // 确定后切到选定态（网格收起，显示已选卡片 + 更换/移除）：
    // 用户不用在 40 张网格里找「哪几张是我选的」。
    onConfirm: (selected) => {
      writeSelection(selW, selected);
      const sel = readSelection(selW);
      syncAssetOutputs(node);            // ③ 确定后同步输出口
      renderStrip(node, host, sel);
      host.setStatus(statusText(sel));
      node.__dkReplaceIndex = -1;        // 确定 = 结束任何进行中的替换
      node.__dkReplaceBase = null;
      setAssetsMode(node, host, MODE_SELECTED);
      clearReplaceHint(host);
    },
    // 面板内部选中变化：只更新状态条。
    // 唯一例外是「替换模式」——用户点的那张要立刻替换到目标位置，然后退出替换模式。
    onChange: (selected) => {
      host.setStatus(selected.length ? `已选 ${selected.length} 张（点「确定」生效）` : "未选择资产图");
      const idx = node.__dkReplaceIndex;
      if (idx === undefined || idx < 0) return;

      const base = node.__dkReplaceBase || { ids: [], names: [] };
      const baseIds = new Set(base.ids);
      // 用户新点的那张 = 不在替换前清单里的那张。
      // 只取消选中、还没点新的时这里拿不到 → 继续等，不动清单。
      const added = selected.find((item) => !baseIds.has(item.id));
      if (!added) return;

      const nextIds = base.ids.slice();
      const nextNames = base.names.slice();
      nextIds[idx] = added.id;
      nextNames[idx] = added.name || "";

      node.__dkReplaceIndex = -1;
      node.__dkReplaceBase = null;

      const nextSel = nextIds.map((id, i) => ({ id, name: nextNames[i] || "" }));
      // 面板内部选中集现在是「原来 N 张 + 新点那张」，要同步成替换后的 N 张，
      // 否则用户再点「确定」会把多出来的那张也提交上去。
      try {
        host.panel.setSelection(nextSel);
      } catch (err) {
        console.warn("[dogking-tools] 节点 4 替换后同步选中失败", err);
      }
      writeSelection(selW, nextSel);
      const sel = readSelection(selW);
      syncAssetOutputs(node);
      renderStrip(node, host, sel);
      host.setStatus(`已替换第 ${idx + 1} 张：${added.name || added.id}`);
      clearReplaceHint(host);   // 替换已完成，提示要撤掉（下次回网格就是正常面包屑）
      setAssetsMode(node, host, MODE_SELECTED);
    },
  });
  node.__dkHost = host;

  const collapseBtn = node.addWidget("button", "收起面板", null, () => {
    const collapsed = host.toggleCollapsed();
    syncCollapseButton(collapseBtn, collapsed);
    if (!collapsed) {
      // 展开 = 用户主动要看数据 → 顺手刷新「项目/分类」下拉的选项
      // （从资产列表聚合而来，不刷新就看不到新建的项目；一次 2KB，不阻塞）
      refreshFacets(node, true);
      setTimeout(() => {
        renderStrip(node, host, readSelection(selW));
        applyAssetsMode(node, host);   // rebuild 会新建 PvPanel，display 状态全丢，必须重套
      }, 0);
    }
  });
  collapseBtn.serialize = false;
  node.__dkCollapseBtn = collapseBtn;

  const refreshBtn = node.addWidget("button", "刷新分类", null, () => {
    refreshFacets(node, true);
  });
  refreshBtn.serialize = false;

  [catW, projW].forEach((widget) => {
    if (!widget) return;
    const previous = widget.callback;
    widget.callback = function () {
      const result = previous ? previous.apply(this, arguments) : undefined;
      host.applyFilters();
      return result;
    };
  });

  // ① 创建时对齐一次（新节点 sel 为空 → 0 个口）
  syncAssetOutputs(node);
  renderStrip(node, host, readSelection(selW));
  refreshFacets(node, true);
  syncCollapseButton(collapseBtn, host.collapsed);
}

/** 恢复工作流：回填已选 → 对齐输出口 → 缩略图条 → 收起 + 占位条 */
async function restoreAssets(node) {
  const selW = findWidget(node, "已选");
  const host = node.__dkHost;
  if (!selW) return;

  const sel = readSelection(selW);

  if (host) {
    // 切工作流后保持展开（旧行为是 setCollapsed(true)，每次打开都得手点一次展开）
    host.restoreExpanded(() => {
      if (!host.panel) return;
      try {
        host.panel.setSelection(sel.ids.map((id, i) => ({ id, name: sel.names[i] || "" })));
      } catch (err) {
        console.warn("[dogking-tools] 节点 4 回填已选失败", err);
      }
      // 面板是新建的 → 两态显示状态全丢，必须重套
      applyAssetsMode(node, host);
    });
    syncCollapseButton(node.__dkCollapseBtn, false);
    host.setStatus(statusText(sel));
  }

  // ② configure 后对齐一次（以清单为准，不依赖保存时的 outputs）
  syncAssetOutputs(node);
  renderStrip(node, host, sel);

  // 有已选 → 选定态；空 → 网格态。面板此刻是收起的，状态先记在 node 上，
  // 展开时 applyAssetsMode 会套用（收起状态下 ensureSelectedView 返回 null 是预期的）。
  node.__dkAssetsMode = sel.ids.length ? MODE_SELECTED : MODE_GRID;
  applyAssetsMode(node, host);

  const catW = findWidget(node, "分类");
  const hasOptions = catW && catW.options && (catW.options.values || []).length > 1;
  if (!hasOptions && !node.__dkFacets) {
    try {
      await refreshFacets(node, true);
    } catch (err) {
      console.warn("[dogking-tools] 节点 4 恢复分类失败", err);
    }
  }

  // 硬结论 4「状态变化 ≠ UI 重载」：补完状态必须显式通知面板重载
  if (host && host.panel) host.applyFilters();
}

app.registerExtension({
  name: "dogking.pv.assets",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (!nodeData || nodeData.name !== NODE) return;

    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const result = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;
      try {
        setupAssets(this);
      } catch (err) {
        console.error("[dogking-tools] 节点 4 初始化失败", err);
      }
      return result;
    };

    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () {
      const result = onConfigure ? onConfigure.apply(this, arguments) : undefined;
      try {
        const pending = restoreAssets(this);
        if (pending && typeof pending.catch === "function") {
          pending.catch((err) => console.error("[dogking-tools] 节点 4 恢复失败", err));
        }
      } catch (err) {
        console.error("[dogking-tools] 节点 4 恢复失败", err);
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
        console.warn("[dogking-tools] 节点 4 状态更新失败", err);
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

export { setupAssets, restoreAssets, syncAssetOutputs, statusText, removeAt };
