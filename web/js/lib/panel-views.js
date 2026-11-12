/**
 * dogking-tools —— 面板的两种列表渲染（T02）
 *
 * 只负责「一批数据 → 一个 DocumentFragment」，不碰状态、不发请求、不绑全局事件。
 * 拆出来的原因：grid 和 card 两套卡片的 DOM 结构差异很大（96×96 方块 vs 320×110 矩形），
 * 混在 panel.js 里会让那个文件同时管「分页状态机」和「两套卡片样式」，改一个动另一个。
 *
 * 所有文本一律用 textContent 赋值，**不用 innerHTML** —— 图名/提示词里带 `<`
 * 是常态（提示词里写 `<lora:xxx:1>` 很常见），拼 HTML 会直接把卡片结构打坏。
 */

/**
 * 图片网格里的一张卡。
 *
 * @param {object} item 精简后的条目（id/name/width/height）
 * @param {Set<string>} selectedSet
 * @param {object} opts
 *   - thumbUrl(id, size) → string
 *   - orderOf(id) → number|0   多选顺序号（0 = 不显示）
 *   - onToggle(item, index, event)
 *   - index 在当前页里的下标
 * @returns {HTMLElement}
 */
function buildImageCard(item, selectedSet, opts, index) {
  const id = String(item && item.id != null ? item.id : "");
  const selected = selectedSet ? selectedSet.has(id) : false;

  const card = document.createElement("div");
  card.className = "dk-pv-card" + (selected ? " is-selected" : "");
  card.dataset.id = id;
  card.dataset.index = String(index);
  if (item && item.name) card.title = String(item.name);

  const thumb = document.createElement("div");
  thumb.className = "dk-pv-thumb is-pending";

  const img = document.createElement("img");
  img.loading = "lazy";        // 只解码视口内的
  img.decoding = "async";      // 解码不占主线程
  img.draggable = false;
  img.alt = "";
  img.addEventListener("load", () => {
    thumb.classList.remove("is-pending");
    img.classList.add("is-loaded");
  });
  // 代理返回 404 / 记录已删除时换占位块，绝不出现浏览器的裂图图标
  img.addEventListener("error", () => {
    thumb.classList.remove("is-pending");
    thumb.classList.add("is-broken");
    img.remove();
  });
  try {
    img.src = opts.thumbUrl(id, "thumb");
  } catch (err) {
    thumb.classList.remove("is-pending");
    thumb.classList.add("is-broken");
  }
  thumb.appendChild(img);

  const name = document.createElement("div");
  name.className = "dk-pv-name";
  name.textContent = (item && item.name) || "（未命名）";

  const check = document.createElement("span");
  check.className = "dk-pv-check";
  check.textContent = "✓";

  const order = document.createElement("span");
  order.className = "dk-pv-order";
  const ord = opts.orderOf ? opts.orderOf(id) : 0;
  if (ord > 0) order.textContent = String(ord);

  card.append(thumb, name, check, order);
  return card;
}

/**
 * 词库（text）卡片。
 *
 * text 为空是常态（现有 68 条 LORA 触发词里大量为空），
 * 空的时候显示灰字「（无触发词）」，而不是留一块空白让人以为没加载出来。
 *
 * @param {object} item {id,name,text,chars}
 * @param {Set<string>} selectedSet
 * @param {object} opts {onToggle}
 * @param {number} index
 * @returns {HTMLElement}
 */
function buildTextCard(item, selectedSet, opts, index) {
  const id = String(item && item.id != null ? item.id : "");
  const selected = selectedSet ? selectedSet.has(id) : false;
  const text = String((item && item.text) || "");

  const card = document.createElement("div");
  card.className = "dk-pv-tcard" + (selected ? " is-selected" : "");
  card.dataset.id = id;
  card.dataset.index = String(index);

  const title = document.createElement("div");
  title.className = "dk-pv-tcard-title";
  title.textContent = (item && item.name) || "（未命名）";
  if (item && item.name) card.title = String(item.name);

  const rule = document.createElement("div");
  rule.className = "dk-pv-tcard-rule";

  const body = document.createElement("div");
  body.className = "dk-pv-tcard-body" + (text ? "" : " is-empty");
  body.textContent = text || "（无触发词）";

  const meta = document.createElement("div");
  meta.className = "dk-pv-tcard-meta";
  const chars = Number(item && item.chars);
  meta.textContent = Number.isFinite(chars) && chars > 0 ? `${chars} 字` : "";

  card.append(title, rule, body, meta);
  return card;
}

/**
 * 绑定点击 / Shift 连选。
 *
 * 用事件委托而不是给每张卡单独绑：40 张卡 × 3 个监听器 = 120 个，
 * 每次翻页重建都要重新绑一遍；委托只需要在容器上绑一次。
 *
 * @param {HTMLElement} container
 * @param {object} opts {onToggle}
 */
function bindCardEvents(container, opts) {
  container.addEventListener("click", (event) => {
    const card = event.target && event.target.closest
      ? event.target.closest(".dk-pv-card, .dk-pv-tcard")
      : null;
    if (!card || !container.contains(card)) return;
    const index = Number(card.dataset.index);
    if (opts.onToggle) opts.onToggle(String(card.dataset.id || ""), index, event);
  });
}

/**
 * 渲染图片网格。
 *
 * @param {Array<object>} items
 * @param {Set<string>} selectedSet
 * @param {object} opts {thumbUrl, orderOf, onToggle}
 * @returns {DocumentFragment}
 */
export function renderImageGrid(items, selectedSet, opts) {
  const o = opts || {};
  const frag = document.createDocumentFragment();
  const list = Array.isArray(items) ? items : [];
  list.forEach((item, index) => {
    frag.appendChild(buildImageCard(item, selectedSet, o, index));
  });
  return frag;
}

/**
 * 渲染词库卡片。
 *
 * @param {Array<object>} items
 * @param {Set<string>} selectedSet
 * @param {object} opts {onToggle}
 * @returns {DocumentFragment}
 */
export function renderTextCards(items, selectedSet, opts) {
  const frag = document.createDocumentFragment();
  const list = Array.isArray(items) ? items : [];
  list.forEach((item, index) => {
    frag.appendChild(buildTextCard(item, selectedSet, opts || {}, index));
  });
  return frag;
}

/**
 * 生成骨架（加载态）。数量按「当前页可能有多少条」给，避免加载时面板突然塌下去。
 *
 * @param {number} count
 * @returns {DocumentFragment}
 */
export function renderSkeletons(count) {
  const frag = document.createDocumentFragment();
  const n = Math.max(0, Math.min(Number(count) || 0, 60));
  for (let i = 0; i < n; i += 1) {
    const box = document.createElement("div");
    box.className = "dk-pv-skel";
    frag.appendChild(box);
  }
  return frag;
}

/**
 * 给容器装事件委托（导出给 panel.js 用一次即可）。
 * @param {HTMLElement} container
 * @param {object} opts
 */
export function attachCardEvents(container, opts) {
  bindCardEvents(container, opts || {});
}

export default { renderImageGrid, renderTextCards, renderSkeletons, attachCardEvents };
