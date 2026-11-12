/**
 * dogking-tools —— 内嵌选图面板 PvPanel（T02）
 *
 * 这是一个**纯组件**：只管「拉一页数据 → 画出来 → 收选中 → 回调」，
 * 不碰节点、不碰 canvas、不认识 litegraph。节点层（T03/T04）负责
 * 把它挂到 `addDOMWidget` 里，并在 onConfirm 里把结果写回隐藏 widget。
 *
 * ## 🔴 三按钮确认（本次改动的核心语义）
 *
 * 底栏是「清空 / 取消 / 确定」三个按钮，**选中变化本身不写回**：
 *
 *   · 点卡片 / Shift 连选 / 随机抽 → 只改面板内部选中集，调 `onChange`
 *     （节点层拿它更新状态条文案 / 徽标数字，**不要**写 widget）。
 *   · 点「确定」→ `confirm()`：把当前选中集记为「已提交」快照，再调 `onConfirm`
 *     （节点层在这里写回隐藏 widget / 切预览态 / 同步输出口）。
 *   · 点「取消」→ `cancel()`：把选中集退回**上次确定**的快照，**不关面板**。
 *   · 点「清空」→ 清空选中集（同样只调 onChange，等用户点「确定」才落盘）。
 *   · `close()` → 收起面板时的兜底回传（面板宿主在 setCollapsed 里调）。
 *
 * 为什么要有「确定」：面板是内嵌的，用户点一张图之后面板还开着，此时
 * 「已选」到底生效了没有必须有个明确边界。有了确定/取消，用户可以放心地
 * 连续比较、多选、再反悔 —— 「确定之前一律不生效」是一句用户能立刻理解的话。
 * 上一轮改成实时写回（没有确定按钮）的代价是「点错了只能再点一次撤回」，
 * 而且清空 / 随机抽都直接落盘，用户没有反悔余地。
 *
 * ⚠️ `_committed` 是**唯一**的「已生效」真相：`setSelection()`（打开工作流回填）
 * 和 `confirm()` 都会刷新它，`cancel()` 只从它回滚。
 *
 * 三条硬约束（都来自 docs/ARCH-comfyui-nodes-v2.md §2.1）：
 *
 * 1. **翻页只动列表容器**。DOM 骨架一次建好，翻页只
 *    `listEl.replaceChildren(fragment)` + 改 footer 两个 textContent。
 *    绝不调 `node.setSize`、绝不 `setDirtyCanvas` —— 否则
 *    `computeLayoutSize → computeSize → setSize → 高度` 会形成反馈环，节点一路涨。
 *
 * 2. **事件必须隔离**。面板是 `position:fixed` 浮在 canvas 上的，
 *    但 litegraph 在 `document` 上挂了 pointermove，部分版本还把 wheel 挂在
 *    canvas 容器上。不隔离的话：点卡片会拖动节点、滚列表会缩放画布。
 *
 * 3. **不 preventDefault**。滚轮一旦 preventDefault，原生滚动就废了，
 *    得手写 scrollTop 累加（还得自己处理惯性、边界回弹）。这里用 passive 监听 +
 *    只在「还能滚」时 stopPropagation，滚到头自然交还给画布缩放。
 */

import { pvList, pvRandom, pvMeta, pvThumbUrl } from "./api.js";
import { renderImageGrid, renderTextCards, renderSkeletons, attachCardEvents } from "./panel-views.js";
import { injectPanelStyles } from "./ui.js";

const SEARCH_DEBOUNCE_MS = 300;
const PAGE_SIZES = [20, 40, 80];

/** 小工具：建元素 */
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export class PvPanel {
  /**
   * @param {object} opts
   *   kind:      'image' | 'text' | 'asset'
   *   mount:     HTMLElement 挂载点
   *   view:      'grid' | 'card'
   *   mode:      'single' | 'multi' | 'random'
   *   pageSize:  number（默认 40）
   *   filters:   () => ({category, subCategory, project, addr, textType, crumb}) 每次请求前调
   *   onChange:  (selected) => void        选中集变了（只更新 UI 文案，别写 widget）
   *   onConfirm: (selected) => void        **写回**：只有点「确定」才会调
   *   maxSelect: number（多选上限）
   *   liveConfirm: boolean  实时确认模式（默认 false）
   *     true  → 底栏只留「清空」，任何选中变化**立刻**写回（节点 1/2/3 用）
   *     false → 底栏三个按钮 清空/取消/确定，点确定才写回（节点 4 用）
   *
   * 为什么节点 4 单独要确认：它是**动态输出口**，每张图占一个口，
   * 点一下就写回会让输出口跟着疯狂增删（连带断用户已经连好的线）。
   * 其余三个节点只吐固定数量的口，实时写回没有副作用。
   */
  constructor(opts = {}) {
    this.kind = opts.kind === "text" || opts.kind === "asset" ? opts.kind : "image";
    this.view = opts.view === "card" ? "card" : "grid";
    this.mode = opts.mode === "single" || opts.mode === "random" ? opts.mode : "multi";
    this.pageSize = PAGE_SIZES.includes(Number(opts.pageSize)) ? Number(opts.pageSize) : 40;
    this.filters = typeof opts.filters === "function" ? opts.filters : () => ({});
    this.onChange = typeof opts.onChange === "function" ? opts.onChange : null;
    this.onConfirm = typeof opts.onConfirm === "function" ? opts.onConfirm : null;
    this.maxSelect = Number(opts.maxSelect) > 0 ? Number(opts.maxSelect) : 8;
    // 随机数量由节点层提供（面板不自己存，避免两处状态不一致）
    this.randomCount = typeof opts.randomCount === "function" ? opts.randomCount : null;
    this.liveConfirm = Boolean(opts.liveConfirm);

    // 列表状态
    this._page = 1;
    this._pages = 1;
    this._total = 0;
    this._items = [];
    this._q = "";
    this._addr = "";
    this._tookMs = 0;

    // 选中：有序数组（顺序即语义，节点 4 的输出口顺序 = 这个数组顺序）+ Set 做 O(1) 判断
    this._selOrder = [];
    this._selSet = new Set();
    // 「已提交」快照：点「确定」或 setSelection() 时刷新；「取消」从这里回滚。
    // 存的是 getSelection() 的深拷贝，避免外部改动污染快照。
    this._committed = [];

    this._lastIndex = -1;
    this._reqSeq = 0;
    this._searchTimer = 0;
    this._destroyed = false;
    this._state = "idle";

    this._build(opts.mount);
    // 首屏：异步拉第一页（不阻塞构造，节点可以先完成自己的挂载）
    this._load(1);
  }

  /* ------------------------------------------------------------ DOM 骨架 */

  _build(mount) {
    injectPanelStyles();

    const root = el("div", "dk-pv-panel");
    root.tabIndex = -1;

    /* ---- 工具条 ---- */
    const toolbar = el("div", "dk-pv-toolbar");

    this.crumbEl = el("div", "dk-pv-crumb", "全部");

    this.searchEl = document.createElement("input");
    this.searchEl.className = "dk-pv-search";
    this.searchEl.type = "search";
    this.searchEl.placeholder = "搜索…";
    this.searchEl.addEventListener("input", () => this._onSearchInput());

    this.countEl = el("div", "dk-pv-count", "共 0 条");

    this.refreshBtn = el("button", "dk-pv-btn", "↻");
    this.refreshBtn.type = "button";
    this.refreshBtn.title = "刷新（跳过缓存重新拉取）";
    this.refreshBtn.addEventListener("click", () => this.refresh());

    toolbar.append(this.crumbEl, this.searchEl, this.countEl, this.refreshBtn);

    // 随机模式专用控件（其他模式不插入 DOM，避免占位）
    //
    // 这里**刻意不放数字输入框**：随机数量是节点上的 INT widget，
    // 面板再放一个输入框就有两处状态，必然出现「面板改成 50、节点还是 4，
    // 执行按 4 跑」这类不一致。改成按钮标签显示节点上当前的 N，单一数据源。
    if (this.mode === "random") {
      this.randomBtn = el("button", "dk-pv-btn", "随机抽");
      this.randomBtn.type = "button";
      this.randomBtn.title = "从当前筛选范围随机抽（数量由节点上的「随机数量」决定）";
      this.randomBtn.addEventListener("click", () => this._doRandom());
      toolbar.append(this.randomBtn);
    }

    /* ---- 列表区 ---- */
    const scroll = el("div", "dk-pv-scroll");
    const list = el("div", this.view === "card" ? "dk-pv-cards" : "dk-pv-grid");

    const state = el("div", "dk-pv-state");
    this.stateMsgEl = el("div", "dk-pv-state-msg");
    this.stateBtnEl = el("button", "dk-pv-btn", "重试");
    this.stateBtnEl.type = "button";
    this.stateBtnEl.addEventListener("click", () => this.refresh());
    state.append(this.stateMsgEl, this.stateBtnEl);

    scroll.append(list, state);

    /* ---- 底部 ---- */
    const footer = el("div", "dk-pv-footer");

    const pager = el("div", "dk-pv-pager");
    this.firstBtn = el("button", "dk-pv-btn", "⏮");
    this.prevBtn = el("button", "dk-pv-btn", "‹");
    this.pageInfoEl = el("div", "dk-pv-pageinfo", "1 / 1");
    this.nextBtn = el("button", "dk-pv-btn", "›");
    this.lastBtn = el("button", "dk-pv-btn", "⏭");
    [this.firstBtn, this.prevBtn, this.nextBtn, this.lastBtn].forEach((btn) => {
      btn.type = "button";
    });
    this.firstBtn.title = "首页";
    this.prevBtn.title = "上一页";
    this.nextBtn.title = "下一页";
    this.lastBtn.title = "末页";
    this.firstBtn.addEventListener("click", () => this._goPage(1));
    this.prevBtn.addEventListener("click", () => this._goPage(this._page - 1));
    this.nextBtn.addEventListener("click", () => this._goPage(this._page + 1));
    this.lastBtn.addEventListener("click", () => this._goPage(this._pages));
    pager.append(this.firstBtn, this.prevBtn, this.pageInfoEl, this.nextBtn, this.lastBtn);

    this.sizeSel = document.createElement("select");
    this.sizeSel.className = "dk-pv-pagesize";
    this.sizeSel.title = "每页条数";
    PAGE_SIZES.forEach((size) => {
      const opt = document.createElement("option");
      opt.value = String(size);
      opt.textContent = `${size} / 页`;
      if (size === this.pageSize) opt.selected = true;
      this.sizeSel.appendChild(opt);
    });
    this.sizeSel.addEventListener("change", () => {
      const next = Number(this.sizeSel.value);
      if (!PAGE_SIZES.includes(next)) return;
      this.pageSize = next;
      this._load(1); // 改页大小必须回第 1 页，否则会落在不存在的页上
    });

    this.selCountEl = el("div", "dk-pv-selcount", "已选 0");

    const actions = el("div", "dk-pv-actions");
    this.clearBtn = el("button", "dk-pv-btn", "清空");
    this.clearBtn.type = "button";

    if (this.liveConfirm) {
      // ---- 实时确认：只有「清空」，点选立刻生效 ----
      // 没有「取消」是因为不存在"草稿"这个概念：每次点选都已经写进 widget 了，
      // 想撤销就再点一次取消选中（或点清空）。
      this.clearBtn.title = "清空选择（立即生效）";
      this.clearBtn.addEventListener("click", () => {
        this._setSelection([]);
        this._syncCardStates();
        this._updateFooter();
        if (this.onChange) this.onChange(this.getSelection());
        this._emitConfirm();
      });
      actions.append(this.clearBtn);
    } else {
      // ---- 三按钮确认（节点 4）----
      // 清空只是「把选中集置空」，**不写回** —— 用户点「确定」才落盘，
      // 所以「清空 → 确定」是一次合法的「提交空清单」（确定按钮因此不能 disabled）。
      this.cancelBtn = el("button", "dk-pv-btn", "取消");
      this.confirmBtn = el("button", "dk-pv-btn is-primary", "确定");
      [this.cancelBtn, this.confirmBtn].forEach((b) => {
        b.type = "button";
      });
      this.clearBtn.title = "清空当前选择（点「确定」后生效）";
      this.cancelBtn.title = "放弃本次修改，回到上次确定的状态";
      this.confirmBtn.title = "把当前选择写回节点";
      this.clearBtn.addEventListener("click", () => {
        this._setSelection([]);
        this._syncCardStates();
        this._updateFooter();
        if (this.onChange) this.onChange(this.getSelection());
      });
      this.cancelBtn.addEventListener("click", () => this.cancel());
      this.confirmBtn.addEventListener("click", () => this.confirm());
      actions.append(this.clearBtn, this.cancelBtn, this.confirmBtn);
    }

    footer.append(pager, this.sizeSel, this.selCountEl, actions);

    root.append(toolbar, scroll, footer);

    /* ---- 事件隔离（决策 1，必须做） ---- */
    // 不隔离的话：点卡片会被 litegraph 当成节点拖拽，右键会弹画布菜单
    root.addEventListener("pointerdown", (e) => e.stopPropagation());
    root.addEventListener("contextmenu", (e) => e.stopPropagation());
    // 关键：passive + 不 preventDefault，让原生滚动生效；
    // 只在「还能滚」时吞掉事件，滚到两端就交还给画布缩放。
    scroll.addEventListener(
      "wheel",
      (e) => {
        const atTop = scroll.scrollTop <= 0 && e.deltaY < 0;
        const atBottom =
          scroll.scrollTop + scroll.clientHeight >= scroll.scrollHeight - 1 && e.deltaY > 0;
        if (!atTop && !atBottom) e.stopPropagation();
      },
      { passive: true }
    );

    // 卡片点击用事件委托，容器上只绑一次（翻页重建不需要重绑）
    attachCardEvents(list, {
      onToggle: (id, index, event) => this._onCardClick(id, index, event),
    });

    this.root = root;
    this.scrollEl = scroll;
    this.listEl = list;
    this.stateEl = state;

    if (mount) {
      mount.innerHTML = "";
      mount.appendChild(root);
    }

    this._updateFooter();
    this._updateCrumb();
    this.updateRandomButton();
  }

  /* ------------------------------------------------------------ 数据 */

  /** 把当前 kind/filters 翻译成 pv_list 的请求体 */
  _body(page) {
    const f = this.filters() || {};
    const body = {
      addr: f.addr || "",
      kind: this.kind,
      page,
      pageSize: this.pageSize,
      q: this._q,
    };
    if (this.kind === "image") body.categoryId = f.category || "";
    if (this.kind === "asset") {
      body.category = f.category || "";
      body.project = f.project || "";
    }
    if (this.kind === "text") body.textType = f.textType || "loras";
    return body;
  }

  /**
   * 拉一页并渲染。
   *
   * `_reqSeq` 是防「旧请求覆盖新结果」的：用户快速翻页 / 快速输入时，
   * 先发的请求可能后到，直接把列表刷成旧数据。每次请求领一个序号，
   * 回来时序号不是最新就丢弃。
   */
  async _load(page, opts = {}) {
    if (this._destroyed) return;
    const seq = ++this._reqSeq;
    this._setState("loading");

    const body = this._body(page);
    if (opts.refresh) body.refresh = true;

    try {
      const data = await pvList(body);
      if (this._destroyed || seq !== this._reqSeq) return;
      this._items = Array.isArray(data.items) ? data.items : [];
      this._total = Number(data.total) || 0;
      this._pages = Math.max(1, Number(data.pages) || 1);
      this._page = Math.max(1, Number(data.page) || 1);
      this._addr = data.addr || "";
      this._tookMs = Number(data.tookMs) || 0;
      this._lastIndex = -1;
      this._renderList();
      this._updateFooter();
      this._updateCrumb();
      this._setState(this._total === 0 ? "empty" : "idle");
    } catch (err) {
      if (this._destroyed || seq !== this._reqSeq) return;
      this._items = [];
      this._total = 0;
      this._pages = 1;
      this._page = 1;
      this._renderList();
      this._updateFooter();
      this._setState("error", (err && err.message) || "加载失败");
    }
  }

  /** 只重绘列表容器（不碰工具栏 / 底栏 / canvas） */
  _renderList() {
    const opts = {
      thumbUrl: (id, size) => pvThumbUrl({ id, kind: this.kind, size, addr: this._addr }),
      orderOf: (id) => this._orderOf(id),
    };
    const frag =
      this.view === "card"
        ? renderTextCards(this._items, this._selSet, opts)
        : renderImageGrid(this._items, this._selSet, opts);
    this.listEl.replaceChildren(frag);
  }

  /* ------------------------------------------------------------ 状态 */

  _setState(state, message) {
    this._state = state;
    const open = state === "error" || state === "empty";

    if (state === "loading") {
      // 骨架数量按当前页大小给（最多 60 个，避免 80/页 时画一堆）
      this.listEl.replaceChildren(renderSkeletons(Math.min(this.pageSize, 60)));
      this.stateEl.classList.remove("is-open");
      return;
    }

    if (state === "error") {
      this.stateMsgEl.className = "dk-pv-state-msg is-error";
      this.stateMsgEl.textContent = message || "加载失败";
      this.stateBtnEl.style.display = "";
      this.stateEl.classList.add("is-open");
      return;
    }

    if (state === "empty") {
      this.stateMsgEl.className = "dk-pv-state-msg";
      this.stateMsgEl.textContent = this._q ? "没有匹配的内容" : "这里还没有内容";
      this.stateBtnEl.style.display = "none";
      this.stateEl.classList.add("is-open");
      return;
    }

    if (!open) this.stateEl.classList.remove("is-open");
  }

  /* ------------------------------------------------------------ 选中 */

  _orderOf(id) {
    for (let i = 0; i < this._selOrder.length; i += 1) {
      if (String(this._selOrder[i].id) === String(id)) return i + 1;
    }
    return 0;
  }

  /** 整体替换选中（用于「清空」/「恢复已保存清单」/ 随机结果） */
  _setSelection(items) {
    const list = Array.isArray(items) ? items : [];
    const seen = new Set();
    this._selOrder = [];
    list.forEach((item) => {
      const id = String(item && item.id != null ? item.id : "");
      if (!id || seen.has(id)) return;
      seen.add(id);
      this._selOrder.push(this._normalizeSel(item));
    });
    this._selSet = seen;
    if (this.onChange) this.onChange(this.getSelection());
  }

  /**
   * 选中项只保留必要字段（要写进工作流的隐藏 widget，别塞整条记录）
   */
  _normalizeSel(item) {
    const out = { id: String(item.id), name: String(item.name || "") };
    ["width", "height", "size", "category", "project"].forEach((key) => {
      if (item[key] !== undefined && item[key] !== null) out[key] = item[key];
    });
    return out;
  }

  _add(item) {
    const id = String(item && item.id != null ? item.id : "");
    if (!id || this._selSet.has(id)) return false;
    if (this._selOrder.length >= this.maxSelect) {
      this._flashLimit();
      return false;
    }
    this._selOrder.push(this._normalizeSel(item));
    this._selSet.add(id);
    return true;
  }

  _remove(id) {
    const key = String(id);
    if (!this._selSet.has(key)) return false;
    this._selSet.delete(key);
    this._selOrder = this._selOrder.filter((item) => String(item.id) !== key);
    return true;
  }

  _flashLimit() {
    if (!this.selCountEl) return;
    const prev = this.selCountEl.textContent;
    this.selCountEl.textContent = `最多选 ${this.maxSelect} 个`;
    clearTimeout(this._limitTimer);
    this._limitTimer = setTimeout(() => {
      if (this.selCountEl) this.selCountEl.textContent = prev;
    }, 1200);
  }

  /**
   * 卡片点击。
   *
   * single：替换选中集（**只改内部状态**，等用户点「确定」才写回）
   * random：卡片不参与点选（结果由「随机抽」决定）
   * multi ：切换选中；按住 Shift 选中「上次点击 → 这次点击」的区间
   */
  _onCardClick(id, index, event) {
    const item = this._items[index] || { id };
    const key = String(id || item.id || "");
    if (!key) return;

    if (this.mode === "random") return;

    if (this.mode === "single") {
      this._setSelection([item]);
      this._syncCardStates();
      this._updateFooter();
      if (this.liveConfirm) this._emitConfirm();
      return;
    }

    if (event && event.shiftKey && this._lastIndex >= 0) {
      const from = Math.min(this._lastIndex, index);
      const to = Math.max(this._lastIndex, index);
      for (let i = from; i <= to; i += 1) {
        const row = this._items[i];
        if (row) this._add(row);
      }
    } else if (this._selSet.has(key)) {
      this._remove(key);
    } else {
      this._add(item);
    }

    this._lastIndex = index;
    // 就地更新样式，**不重建列表** —— 重建会让 40 个 <img> 重新加载，画面闪一下
    this._syncCardStates();
    this._updateFooter();
    if (this.onChange) this.onChange(this.getSelection());
    if (this.liveConfirm) this._emitConfirm();
  }

  /** 只改 class / 顺序号，不动 DOM 结构（图片不重新加载） */
  _syncCardStates() {
    const cards = this.listEl.querySelectorAll("[data-id]");
    cards.forEach((card) => {
      const id = card.dataset.id;
      const on = this._selSet.has(id);
      card.classList.toggle("is-selected", on);
      const orderEl = card.querySelector(".dk-pv-order");
      if (orderEl) {
        const ord = this._orderOf(id);
        orderEl.textContent = ord > 0 ? String(ord) : "";
      }
    });
  }

  /* ------------------------------------------------------------ 交互 */

  _onSearchInput() {
    const next = this.searchEl.value || "";
    if (next === this._q) return;
    this._q = next;
    clearTimeout(this._searchTimer);
    // 300ms 防抖：每敲一个字都发请求的话，17726 条的后端搜索会被打爆
    this._searchTimer = setTimeout(() => {
      if (!this._destroyed) this._load(1);
    }, SEARCH_DEBOUNCE_MS);
  }

  _goPage(page) {
    const target = Math.max(1, Math.min(Number(page) || 1, this._pages));
    if (target === this._page) return;
    this._load(target);
  }

  /**
   * 当前该抽几张（每次调用都重新问节点层，所以用户改了节点上的数字，
   * 下一次点按钮就生效，不需要同步）。
   *
   * 上限 200：随机抽 200 张 + **排队输出**是完全合法的用法（一次吐一张跑 200 次）。
   * 32 只约束「一次性输出」，不约束清单长度。
   *
   * @returns {number} 1..200
   */
  _randomCount() {
    let n = 4;
    if (this.randomCount) {
      try {
        n = Number(this.randomCount());
      } catch (err) {
        n = 4;
      }
    }
    if (!Number.isFinite(n) || n < 1) n = 1;
    return Math.max(1, Math.min(Math.floor(n), 200));
  }

  /** 按钮标签跟着节点的「随机数量」走 */
  updateRandomButton() {
    if (!this.randomBtn) return;
    this.randomBtn.textContent = `随机抽 ${this._randomCount()} 张`;
  }

  async _doRandom() {
    const n = this._randomCount();
    const body = this._body(1);
    delete body.page;
    delete body.pageSize;
    body.n = n;
    body.exclude = this._selOrder.map((item) => item.id);

    try {
      this._setState("loading");
      const res = await pvRandom(body);
      if (this._destroyed) return;
      const ids = Array.isArray(res.ids) ? res.ids.map(String) : [];

      // 尽量把 id 补成完整条目（缩略图/名字要靠它）。
      // pv_meta 万一将来被删掉，这里退化成只显示 id，不影响「抽到哪几个」这个核心功能。
      let items = ids.map((id) => ({ id, name: id }));
      if (ids.length) {
        try {
          const meta = await pvMeta({
            addr: body.addr,
            kind: this.kind,
            ids,
            textType: body.textType,
          });
          const byId = new Map();
          (meta.items || []).forEach((it) => byId.set(String(it.id), it));
          items = ids.map((id) => byId.get(id) || { id, name: id });
        } catch (err) {
          // 补全失败就用 id 兜底，不把整个随机流程判失败
          console.warn("[dogking-tools] 随机结果补全元数据失败，只显示 id", err);
        }
      }

      if (this._destroyed) return;
      this._items = items;
      this._total = items.length;
      this._pages = 1;
      this._page = 1;
      this._renderList();
      this._setSelection(items);
      this._syncCardStates();
      this._updateFooter();
      this._setState(items.length ? "idle" : "empty");
      // 实时模式：抽到即生效（节点 1 的随机多选不需要再点一次确定）
      if (this.liveConfirm) this._emitConfirm();
    } catch (err) {
      if (this._destroyed) return;
      this._setState("error", (err && err.message) || "随机抽取失败");
    }
  }

  _updateFooter() {
    if (this.pageInfoEl) this.pageInfoEl.textContent = `${this._page} / ${this._pages}`;
    if (this.firstBtn) this.firstBtn.disabled = this._page <= 1;
    if (this.prevBtn) this.prevBtn.disabled = this._page <= 1;
    if (this.nextBtn) this.nextBtn.disabled = this._page >= this._pages;
    if (this.lastBtn) this.lastBtn.disabled = this._page >= this._pages;
    if (this.selCountEl) this.selCountEl.textContent = `已选 ${this._selOrder.length}`;
    if (this.countEl) {
      const took = this._tookMs ? ` · ${this._tookMs}ms` : "";
      this.countEl.textContent = `共 ${this._total} 条${took}`;
    }
  }

  _updateCrumb() {
    if (!this.crumbEl) return;
    const f = this.filters() || {};
    if (this.kind === "image") {
      // 优先用 `categoryPath`（人读的「风格 / 写实摄影」）；
      // `category` 是发给后端的**分类 id**，直接显示出来就是一串乱码。
      const top = f.categoryPath || f.category || "全部";
      const sub = f.subCategory ? ` / ${f.subCategory}` : "";
      this.crumbEl.textContent = `${top}${sub}`;
    } else if (this.kind === "asset") {
      // 节点 3 的工具条要按「项目 · 分类」显示（项目在分类的上一层），
      // 节点 4 仍是「分类 · 项目」—— 所以顺序不能在这里写死，由 filters() 直接给文案。
      if (f.crumb) {
        this.crumbEl.textContent = f.crumb;
      } else {
        const cat = f.category || "全部";
        const proj = f.project || "全部";
        this.crumbEl.textContent = `${cat} · ${proj}`;
      }
    } else {
      this.crumbEl.textContent = `数据源：${f.textType || "loras"}`;
    }
    this.crumbEl.title = this.crumbEl.textContent;
  }

  /* ------------------------------------------------------------ 公开 API */

  /** 重置到第 1 页并**跳过缓存**重拉（工具条 ↻） */
  refresh() {
    return this._load(1, { refresh: true });
  }

  /**
   * 筛选变了：归零页码重拉，但**保留已选**。
   * 架构约定：source 变了只归零游标，selected 不清（用户切个分类回来还在）。
   */
  setFilters() {
    this._updateCrumb();
    return this._load(1);
  }

  /** 当前选中（有序，返回副本，外部改不到内部状态） */
  getSelection() {
    return this._selOrder.map((item) => Object.assign({}, item));
  }

  /** 恢复选中（打开工作流 / onConfigure 时用）。**只改内部状态，不写回。**
   *
   * 同时刷新 `_committed`：回填进来的就是「已生效」的那一份，
   * 否则用户打开工作流后点一次「取消」会把清单退回空。
   */
  setSelection(items) {
    this._setSelection(items);
    this._committed = this.getSelection();
    this._syncCardStates();
    this._updateFooter();
  }

  /**
   * 实时确认：选中变化立刻写回 widget。
   *
   * 只有 `liveConfirm: true` 的面板会走到这里（节点 1/2/3）。
   * 节点 4 是动态输出口，点一下就写回会让口疯狂增删、断用户连线，所以它走三按钮。
   */
  _emitConfirm() {
    this._committed = this.getSelection();
    if (this.onConfirm) this.onConfirm(this.getSelection());
  }

  /**
   * 「确定」：把当前选中集记为「已提交」，再回调节点层写回。
   *
   * 面板不关闭（内嵌面板，收起由用户决定），这样用户确定之后还能继续看列表。
   * 允许「空清单 → 确定」：清空本身就是一次合法提交（节点会输出灰图/空串）。
   */
  confirm() {
    this._committed = this.getSelection();
    if (this.onConfirm) this.onConfirm(this.getSelection());
  }

  /**
   * 「取消」：把选中集退回上次确定的状态，**不关面板**。
   *
   * 为什么不是关面板：用户点取消的场景大多是「我改了几个但想反悔」，
   * 关掉面板会让刚才那批缩略图全部重新加载，反而更慢。
   */
  cancel() {
    this._setSelection(this._committed);
    this._syncCardStates();
    this._updateFooter();
    if (this.onChange) this.onChange(this.getSelection());
  }

  /**
   * 「关闭」= 收起面板（由节点层实现折叠），这里把当前选中**兜底提交**一次。
   *
   * 三按钮确认之后这已经不是主路径（用户应该点「确定」），但保留它有一个实际用处：
   * 面板宿主在 `setCollapsed(true)` 里调它，保证「收起」这个动作不会把用户刚点的
   * 那几张悄悄丢掉 —— 收起 = 隐含确定，比「收起了但没生效」更符合直觉。
   *
   * ⚠️ **选中为空时什么都不做**。这一条是必须的：打开工作流时
   * `onConfigure → setCollapsed(true)` 会走到这里，而那一刻面板可能刚建好、
   * 还没被 `setSelection()` 回填。若在这里无条件写回空清单，就会把工作流里
   * 存着的「已选」直接冲掉 —— 表现为「保存重开之后选择全没了」。
   * 真正需要写空清单的场景（用户清空后点确定）走的是 confirmBtn。
   *
   * 注意面板是内嵌的，**不是弹窗**：Esc 不响应、点面板外不关闭。
   */
  close() {
    if (!this._selOrder.length) return;
    // 收起即提交：同步刷新快照，否则下次展开点「取消」会退回更早的一份
    this._committed = this.getSelection();
    if (this.onConfirm) this.onConfirm(this.getSelection());
  }

  destroy() {
    this._destroyed = true;
    this._reqSeq += 1;
    clearTimeout(this._searchTimer);
    clearTimeout(this._limitTimer);
    if (this.root && this.root.parentNode) this.root.parentNode.removeChild(this.root);
    this.root = null;
    this.listEl = null;
    this.scrollEl = null;
  }
}

export default PvPanel;
