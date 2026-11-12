/**
 * dogking-tools 前端扩展
 *
 * 1. 给「提示词批量加载」节点加「选择文件」按钮（弹系统原生选择框）
 * 2. 加「重置计数」按钮：点一下立即把本节点计数清零，不需要执行工作流
 * 3. 按模式动态显示参数：全部生成只留基础项，范围选取才显示起止行，编号选择才显示编号
 * 4. 自动连续：节点执行完如果还有下一行，自动排下一次队列
 * 5. 在节点上显示「第 N/M 行」计数器
 * 6. Ripple 二采放大：按模式收起没用到的尺寸参数（按倍数 / 按二采像素）
 *
 * 注意：这里用 window.comfyAPI 而不是 import —— 插件放在 web/js/ 下时
 * 相对路径 `../../scripts/app.js` 会解析到 /extensions/scripts/ 而 404，
 * 扩展直接加载失败（表现为节点上什么按钮都没有）。
 *
 * T02 起，`drawBadge` / `setWidgetVisible` / `moveWidgetAfter` 三个通用函数
 * 搬到 `lib/ui.js`（新节点的面板组件也要用），这里改成 import。
 * 逻辑一个字没改，只是不再有两份实现。
 */

import { drawBadge, setWidgetVisible, moveWidgetAfter } from "./lib/ui.js";

// T03：PromptVault 的两个新节点（图片+提示词 / 词库）。
// 它们各自在模块内 app.registerExtension，这里只负责把模块加载进来。
// 旧 4 个 PV 节点（Prompt/Image/Pair/Text）已按用户决策**硬删**，
// 它们的前端分支（PV_NODES / applyPvModeVisibility / refreshPvCategories /
// applyPvCategoryValues）也已从本文件移除 —— 老工作流里这些节点会变红，符合预期。
import "./nodes/pick.js";
import "./nodes/vault.js";

// T04：资产图两个节点（单张 / 多重 + 动态输出口）
import "./nodes/asset.js";
import "./nodes/assets.js";

const comfyAPI = window.comfyAPI || {};
const app = comfyAPI.app?.app;
const api = comfyAPI.api?.api;

const NODE_NAME = "DogkingPromptLoader";
const NODE_UPSCALE = "DogkingRippleUpscale";
const NODE_IMAGE = "DogkingImageLoader";
/** PromptVault 数据源节点（提示词 / 图片 / 图片+提示词 / 词库） */
const EVENT_NEXT = "dogking-tools.next";
/** 图片节点拖拽区高度（px）。DOM widget 靠它算尺寸，太小节点会塌 */
const DROP_HEIGHT = 220;

if (!app || !api) {
  console.error("[dogking-tools] 拿不到 ComfyUI 的 app/api，扩展未加载");
} else {
  boot();
}

function boot() {
  /** 本次执行结束后要不要自动排下一次 */
  let pendingContinue = false;

  api.addEventListener(EVENT_NEXT, (e) => {
    pendingContinue = Boolean(e?.detail?.should_continue);
  });

  api.addEventListener("execution_success", () => {
    if (!pendingContinue) return;
    pendingContinue = false;
    // 等 ComfyUI 把队列状态收干净再排，否则偶发丢队列
    setTimeout(() => {
      try {
        app.queuePrompt(0, 1);
      } catch (err) {
        console.warn("[dogking-tools] 自动排队失败", err);
      }
    }, 80);
  });

  // 手动停止 / 出错 → 不再自动续
  ["execution_interrupted", "execution_error"].forEach((ev) => {
    api.addEventListener(ev, () => { pendingContinue = false; });
  });

  app.registerExtension({
    name: "dogking.tools.promptLoader",

    async beforeRegisterNodeDef(nodeType, nodeData) {
      if (nodeData.name !== NODE_NAME) return;

      const onNodeCreated = nodeType.prototype.onNodeCreated;
      nodeType.prototype.onNodeCreated = function () {
        const ret = onNodeCreated?.apply(this, arguments);
        const node = this;
        const find = (name) => node.widgets?.find((w) => w.name === name);

        /* ---------------- 文件选择按钮 ---------------- */
        const pathWidget = find("文件路径");
        if (pathWidget) {
          const btn = node.addWidget("button", "选择文件", null, async () => {
            try {
              const res = await api.fetchApi("/dogking-tools/select_file", { method: "POST" });
              const data = await res.json();
              if (data?.path) {
                pathWidget.value = data.path;
                if (typeof pathWidget.callback === "function") pathWidget.callback(data.path);
                node.setDirtyCanvas?.(true, true);
                refreshTotal(node, data.path);
              } else if (data?.error) {
                alert("打开文件选择框失败：\n" + data.error);
              }
            } catch (err) {
              alert("请求失败：" + err);
            }
          });

          // 把按钮挪到「文件路径」正下方
          if (btn && Array.isArray(node.widgets)) {
            const bi = node.widgets.indexOf(btn);
            const pi = node.widgets.indexOf(pathWidget);
            if (bi >= 0 && pi >= 0 && bi > pi + 1) {
              node.widgets.splice(bi, 1);
              node.widgets.splice(pi + 1, 0, btn);
            }
          }

          // 手改路径时也刷新行数
          const origCb = pathWidget.callback;
          pathWidget.callback = function (value, ...rest) {
            const r = origCb?.apply(this, [value, ...rest]);
            refreshTotal(node, value);
            return r;
          };
        }

        /* ---------------- 重置计数按钮（一次性，不需要执行工作流） ---------------- */
        // 以前这是个常驻 BOOLEAN 开关：用户打开后忘了关，每次执行都把 index
        // 归零，自动连续就永远卡在第 1 行。改成按钮后点一下只清一次计数。
        const contWidget = find("自动连续");
        const resetBtn = node.addWidget("button", "重置计数", null, async () => {
          try {
            const res = await api.fetchApi("/dogking-tools/reset", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              // node.id 是 ComfyUI 分配的节点 id；load() 里用的 unique_id
              // 也是同一个 id 的字符串形式，所以这里能对上 state.json 的 key
              body: JSON.stringify({ node: String(node.id) }),
            });
            const data = await res.json();
            if (data?.ok) {
              node.dogkingStatus = "计数已重置（下次从第 1 行开始）";
              node.setDirtyCanvas?.(true, true);
            } else {
              alert("重置失败：" + (data?.error || "未知错误"));
            }
          } catch (err) {
            alert("重置请求失败：" + err);
          }
        });

        // 把按钮挪到「自动连续」正下方
        if (resetBtn && Array.isArray(node.widgets)) {
          const ri = node.widgets.indexOf(resetBtn);
          const ci = contWidget ? node.widgets.indexOf(contWidget) : -1;
          if (ri >= 0 && ci >= 0 && ri > ci + 1) {
            node.widgets.splice(ri, 1);
            node.widgets.splice(ci + 1, 0, resetBtn);
          }
        }

        /* ---------------- 按模式显示参数 ---------------- */
        const modeWidget = find("模式");
        if (modeWidget) {
          const origModeCb = modeWidget.callback;
          modeWidget.callback = function (value, ...rest) {
            const r = origModeCb?.apply(this, [value, ...rest]);
            // combo 的 callback 有时传索引而不是字符串，统一转成实际值
            let v = value;
            if (typeof v === "number" && Array.isArray(modeWidget.options?.values)) {
              v = modeWidget.options.values[v];
            }
            applyModeVisibility(node, v ?? modeWidget.value);
            return r;
          };
        }

        node.dogkingStatus = "";
        // 同步摆一次，节点一出现就是正确的参数组合（不用 setTimeout，
        // 否则会先闪一下全部参数再收起）
        applyModeVisibility(node);
        return ret;
      };

      /* ---------------- 加载已有工作流后重新摆一次 ---------------- */
      const onConfigure = nodeType.prototype.onConfigure;
      nodeType.prototype.onConfigure = function () {
        const r = onConfigure?.apply(this, arguments);
        setTimeout(() => applyModeVisibility(this), 0);
        return r;
      };

      /* ---------------- 执行后更新计数器 ---------------- */
      const onExecuted = nodeType.prototype.onExecuted;
      nodeType.prototype.onExecuted = function (message) {
        onExecuted?.apply(this, arguments);
        const status = message?.dogking_status?.[0];
        if (typeof status === "string") {
          this.dogkingStatus = status;
          this.setDirtyCanvas?.(true, true);
        }
      };

      /* ---------------- 在节点上画计数器 ---------------- */
      const onDrawForeground = nodeType.prototype.onDrawForeground;
      nodeType.prototype.onDrawForeground = function (ctx) {
        onDrawForeground?.apply(this, arguments);
        const text = this.dogkingStatus;
        if (!text || this.flags?.collapsed) return;
        drawBadge(ctx, this, text);
      };
    },
  });

  /* ================= Ripple 二采放大 ================= */
  // 模式切换：按倍数 / 按二采像素，只显示当前模式用的那个尺寸参数。
  // 两个都摆着的话分不清到底哪个在生效。
  app.registerExtension({
    name: "dogking.tools.rippleUpscale",

    async beforeRegisterNodeDef(nodeType, nodeData) {
      if (nodeData.name !== NODE_UPSCALE) return;

      const onNodeCreated = nodeType.prototype.onNodeCreated;
      nodeType.prototype.onNodeCreated = function () {
        const ret = onNodeCreated?.apply(this, arguments);
        const node = this;
        const modeWidget = node.widgets?.find((w) => w.name === "模式");

        if (modeWidget) {
          const origModeCb = modeWidget.callback;
          modeWidget.callback = function (value, ...rest) {
            const r = origModeCb?.apply(this, [value, ...rest]);
            // combo 的 callback 有时传索引而不是字符串，统一转成实际值
            let v = value;
            if (typeof v === "number" && Array.isArray(modeWidget.options?.values)) {
              v = modeWidget.options.values[v];
            }
            applyUpscaleVisibility(node, v ?? modeWidget.value);
            return r;
          };
        }

        applyUpscaleVisibility(node);
        return ret;
      };

      // 加载已有工作流后重新摆一次
      const onConfigure = nodeType.prototype.onConfigure;
      nodeType.prototype.onConfigure = function () {
        const r = onConfigure?.apply(this, arguments);
        setTimeout(() => applyUpscaleVisibility(this), 0);
        return r;
      };

      /* ---------------- 执行后在节点上显示放大前后的尺寸 ---------------- */
      const onExecuted = nodeType.prototype.onExecuted;
      nodeType.prototype.onExecuted = function (message) {
        onExecuted?.apply(this, arguments);
        const text = message?.dogking_size?.[0];
        if (typeof text === "string") {
          this.dogkingSize = text;
          this.setDirtyCanvas?.(true, true);
        }
      };

      const onDrawForeground = nodeType.prototype.onDrawForeground;
      nodeType.prototype.onDrawForeground = function (ctx) {
        onDrawForeground?.apply(this, arguments);
        const text = this.dogkingSize;
        if (!text || this.flags?.collapsed) return;
        drawBadge(ctx, this, text);
      };
    },
  });

  /* ================= 图片批量加载 ================= */
  // 文件夹选择 + 模式显隐 + 拖拽上传 + 缩略图网格 + 重置计数。
  //
  // 「手动列表」是隐藏 widget：拖进来的图片会被上传到 ComfyUI 的 input 目录
  // （浏览器拿不到本地完整路径），相对路径 JSON 化后写进这个 widget，
  // 后端按「文件夹扫描在前、手动列表追加在后」合并去重。
  app.registerExtension({
    name: "dogking.tools.imageLoader",

    async beforeRegisterNodeDef(nodeType, nodeData) {
      if (nodeData.name !== NODE_IMAGE) return;

      injectDropStyles();

      const onNodeCreated = nodeType.prototype.onNodeCreated;
      nodeType.prototype.onNodeCreated = function () {
        const ret = onNodeCreated?.apply(this, arguments);
        const node = this;
        const find = (name) => node.widgets?.find((w) => w.name === name);

        node.dogkingManual = [];
        node.dogkingFolderTotal = 0;
        node.dogkingStatus = "";

        const folderWidget = find("文件夹路径");
        const sortWidget = find("排序方式");
        const modeWidget = find("模式");
        const manualWidget = find("手动列表");

        /* ---------------- 隐藏「手动列表」widget ---------------- */
        // 值仍然会提交给后端（widget 的 serialize 默认 true），只是不显示。
        if (manualWidget) {
          manualWidget.type = "dogkingHidden";
          manualWidget.computeSize = () => [0, -4];
        }

        /* ---------------- 选择文件夹按钮 ---------------- */
        if (folderWidget) {
          const origCb = folderWidget.callback;
          folderWidget.callback = function (value, ...rest) {
            const r = origCb?.apply(this, [value, ...rest]);
            refreshImageTotal(node);
            return r;
          };

          const btn = node.addWidget("button", "选择文件夹", null, async () => {
            try {
              const res = await api.fetchApi("/dogking-tools/select_folder", { method: "POST" });
              const data = await res.json();
              if (data?.path) {
                folderWidget.value = data.path;
                if (typeof folderWidget.callback === "function") folderWidget.callback(data.path);
                node.setDirtyCanvas?.(true, true);
              } else if (data?.error) {
                alert("打开文件夹选择框失败：\n" + data.error);
              }
            } catch (err) {
              alert("请求失败：" + err);
            }
          });
          // 按钮挪到「文件夹路径」正下方
          moveWidgetAfter(node, btn, folderWidget);
        }

        /* ---------------- 换排序方式 → 重新数一遍 ---------------- */
        if (sortWidget) {
          const origSortCb = sortWidget.callback;
          sortWidget.callback = function (value, ...rest) {
            const r = origSortCb?.apply(this, [value, ...rest]);
            refreshImageTotal(node);
            return r;
          };
        }

        /* ---------------- 按模式显示参数 ---------------- */
        if (modeWidget) {
          const origModeCb = modeWidget.callback;
          modeWidget.callback = function (value, ...rest) {
            const r = origModeCb?.apply(this, [value, ...rest]);
            // combo 的 callback 有时传索引而不是字符串，统一转成实际值
            let v = value;
            if (typeof v === "number" && Array.isArray(modeWidget.options?.values)) {
              v = modeWidget.options.values[v];
            }
            applyImageModeVisibility(node, v ?? modeWidget.value);
            return r;
          };
        }

        /* ---------------- 重置计数按钮 ---------------- */
        const contWidget = find("自动连续");
        const resetBtn = node.addWidget("button", "重置计数", null, async () => {
          try {
            const res = await api.fetchApi("/dogking-tools/reset", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ node: String(node.id) }),
            });
            const data = await res.json();
            if (data?.ok) {
              node.dogkingStatus = "计数已重置（下次从第一张开始）";
              updateImageHeader(node);
              node.setDirtyCanvas?.(true, true);
            } else {
              alert("重置失败：" + (data?.error || "未知错误"));
            }
          } catch (err) {
            alert("重置请求失败：" + err);
          }
        });
        moveWidgetAfter(node, resetBtn, contWidget);

        /* ---------------- 拖拽区（DOM widget） ---------------- */
        const dropEl = document.createElement("div");
        dropEl.className = "dk-drop";

        const head = document.createElement("div");
        head.className = "dk-drop-head";
        head.textContent = "共 0 张";

        const grid = document.createElement("div");
        grid.className = "dk-drop-grid";

        const empty = document.createElement("div");
        empty.className = "dk-drop-empty";
        empty.textContent = "拖拽图片到这里，或点上方「选择文件夹」";

        dropEl.appendChild(head);
        dropEl.appendChild(grid);
        dropEl.appendChild(empty);

        // getHeight 是必须的：不给高度 litegraph 算不出 widget 尺寸，
        // 节点会直接塌成一条（拖拽区看不见）。
        const domWidget = node.addDOMWidget("拖选图片", "dkdrop", dropEl, {
          serialize: false,
          hideOnZoom: false,
          getHeight: () => DROP_HEIGHT,
          getValue: () => "",
          setValue: () => {},
        });

        node.dkDrop = { root: dropEl, head, grid, empty, widget: domWidget };

        dropEl.addEventListener("dragover", (e) => {
          e.preventDefault();
          e.stopPropagation();
          dropEl.classList.add("dk-drop--over");
        });
        dropEl.addEventListener("dragenter", (e) => {
          e.preventDefault();
          e.stopPropagation();
          dropEl.classList.add("dk-drop--over");
        });
        dropEl.addEventListener("dragleave", (e) => {
          e.preventDefault();
          e.stopPropagation();
          dropEl.classList.remove("dk-drop--over");
        });
        dropEl.addEventListener("drop", (e) => {
          // 必须阻止冒泡：不拦的话 ComfyUI 画布会把拖进来的文件当工作流 JSON
          // 解析，轻则弹「加载工作流失败」，重则把当前画布清空。
          e.preventDefault();
          e.stopPropagation();
          dropEl.classList.remove("dk-drop--over");
          const files = e.dataTransfer?.files;
          if (files && files.length) uploadImageFiles(node, files);
        });

        /* ---------------- 初始摆位 ---------------- */
        applyImageModeVisibility(node);
        renderImageDrop(node);
        return ret;
      };

      /* ---------------- 加载已有工作流后恢复手动列表 ---------------- */
      const onConfigure = nodeType.prototype.onConfigure;
      nodeType.prototype.onConfigure = function () {
        const r = onConfigure?.apply(this, arguments);
        const node = this;
        setTimeout(() => {
          const w = (node.widgets || []).find((x) => x.name === "手动列表");
          let list = [];
          try {
            const parsed = JSON.parse(w?.value || "[]");
            if (Array.isArray(parsed)) {
              list = parsed.filter((x) => typeof x === "string" && x.trim());
            }
          } catch (err) {
            list = [];
          }
          node.dogkingManual = list;
          applyImageModeVisibility(node);
          renderImageDrop(node);
          refreshImageTotal(node);
        }, 0);
        return r;
      };

      /* ---------------- 执行后更新状态条 ---------------- */
      const onExecuted = nodeType.prototype.onExecuted;
      nodeType.prototype.onExecuted = function (message) {
        onExecuted?.apply(this, arguments);
        const status = message?.dogking_status?.[0];
        if (typeof status === "string") {
          this.dogkingStatus = status;
          updateImageHeader(this);
          this.setDirtyCanvas?.(true, true);
        }
      };

      /* ---------------- 在节点底部画状态条 ---------------- */
      const onDrawForeground = nodeType.prototype.onDrawForeground;
      nodeType.prototype.onDrawForeground = function (ctx) {
        onDrawForeground?.apply(this, arguments);
        const text = this.dogkingStatus;
        if (!text || this.flags?.collapsed) return;
        drawBadge(ctx, this, text);
      };
    },
  });
}

/* ==================================================================== */

// drawBadge / setWidgetVisible / moveWidgetAfter 已搬到 ./lib/ui.js（见文件头 import）。
// 搬家的原因：T02 的面板组件也要用这三个，放在这里会导致两份实现各自演化。

/**
 * 全部生成 → 只留文件路径 / 模式 / 自动连续（「重置计数」是按钮，始终显示）
 * 范围选取 → 多出 起始行 / 结束行
 * 编号选择 → 多出 编号
 *
 * @param {object} node
 * @param {string} [modeOverride] 直接用这个模式值（callback 里 widget.value 可能还没更新）
 */
function applyModeVisibility(node, modeOverride) {
  const widgets = node.widgets || [];
  const find = (name) => widgets.find((w) => w.name === name);
  const mode = modeOverride ?? find("模式")?.value;

  const isRange = mode === "范围选取";
  const isIndex = mode === "编号选择";

  setWidgetVisible(node, find("起始行"), isRange);
  setWidgetVisible(node, find("结束行"), isRange);
  setWidgetVisible(node, find("编号"), isIndex);

  // 等一帧让 litegraph 把 widget 高度算进去，再重设节点尺寸
  requestAnimationFrame(() => {
    try {
      const size = node.computeSize();
      const h = Number.isFinite(size?.[1]) ? size[1] : 120;
      // +28 是给底部那条「第 N/M 行」状态条留的空间，
      // 不留的话状态文字会被压在最后一个 widget 下面看不到
      node.setSize([node.size[0], Math.max(h + 28, 148)]);
      node.setDirtyCanvas?.(true, true);
    } catch (err) {
      // 节点还没挂到画布上，忽略
    }
  });
}

/**
 * Ripple 二采放大：按倍数 → 只显示「放大倍数」；按二采像素 → 只显示「目标百万像素」
 *
 * 两个尺寸参数同时摆着的话，用户没法判断到底哪个在生效。
 *
 * @param {object} node
 * @param {string} [modeOverride] 直接用这个模式值（callback 里 widget.value 可能还没更新）
 */
function applyUpscaleVisibility(node, modeOverride) {
  const widgets = node.widgets || [];
  const find = (name) => widgets.find((w) => w.name === name);
  const mode = modeOverride ?? find("模式")?.value;
  const byPixels = mode === "按二采像素";

  setWidgetVisible(node, find("放大倍数"), !byPixels);
  setWidgetVisible(node, find("目标百万像素"), byPixels);

  // 等一帧让 litegraph 把 widget 高度算进去，再把节点收回去
  requestAnimationFrame(() => {
    try {
      const size = node.computeSize();
      const h = Number.isFinite(size?.[1]) ? size[1] : 120;
      // +28 给底部那条「480×864 → 960×1728」留位置
      node.setSize([node.size[0], Math.max(h + 28, 120)]);
      node.setDirtyCanvas?.(true, true);
    } catch (err) {
      // 节点还没挂到画布上，忽略
    }
  });
}

/** 查文件行数，写进节点底部的计数器 */
async function refreshTotal(node, path) {
  if (!node || !path) return;
  try {
    const res = await api.fetchApi("/dogking-tools/count", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    const data = await res.json();
    node.dogkingStatus = data?.error ? data.error : `共 ${data.total} 条`;
    node.setDirtyCanvas?.(true, true);
  } catch (err) {
    // 静默失败，不影响主流程
  }
}

/* ==================== 图片批量加载相关工具 ==================== */

/** 一次注入样式，避免每个节点都插一段 <style> */
function injectDropStyles() {
  if (document.getElementById("dk-drop-style")) return;
  const style = document.createElement("style");
  style.id = "dk-drop-style";
  style.textContent = `
.dk-drop {
  box-sizing: border-box;
  width: 100%;
  height: ${DROP_HEIGHT}px;
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  overflow: hidden;
  border: 1px dashed rgba(140, 170, 255, 0.45);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.04);
  color: #cfd8ea;
  font: 12px/1.4 ui-monospace, Consolas, monospace;
}
.dk-drop--over {
  border-color: #5b9bff;
  background: rgba(91, 155, 255, 0.14);
}
.dk-drop-head {
  flex: 0 0 auto;
  color: #8fb4ff;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.dk-drop-grid {
  flex: 1 1 auto;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-content: flex-start;
  overflow-y: auto;
}
.dk-thumb {
  position: relative;
  width: 56px;
  height: 56px;
  flex: 0 0 auto;
  overflow: hidden;
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 6px;
  background: rgba(0, 0, 0, 0.35);
}
.dk-thumb img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.dk-thumb-x {
  position: absolute;
  top: 0;
  right: 0;
  width: 16px;
  height: 16px;
  padding: 0;
  border: 0;
  border-radius: 0 0 0 6px;
  background: rgba(0, 0, 0, 0.65);
  color: #fff;
  font-size: 12px;
  line-height: 14px;
  text-align: center;
  cursor: pointer;
}
.dk-thumb-x:hover {
  background: #d9534f;
}
.dk-drop-empty {
  flex: 1 1 auto;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0 8px;
  color: #7d879b;
  text-align: center;
}
`;
  document.head.appendChild(style);
}

/**
 * 全部模式 → 只留文件夹路径 / 排序方式 / 自动连续（「重置计数」是按钮，始终显示）
 * 范围模式 → 多出 起始序号 / 结束序号
 *
 * 和提示词节点同款思路，只是判断值不同（那边是「范围选取」，这边是「范围模式」）。
 *
 * @param {object} node
 * @param {string} [modeOverride] 直接用这个模式值（callback 里 widget.value 可能还没更新）
 */
function applyImageModeVisibility(node, modeOverride) {
  const widgets = node.widgets || [];
  const find = (name) => widgets.find((w) => w.name === name);
  const mode = modeOverride ?? find("模式")?.value;

  const isRange = mode === "范围模式";

  setWidgetVisible(node, find("起始序号"), isRange);
  setWidgetVisible(node, find("结束序号"), isRange);

  // 等一帧让 litegraph 把 widget 高度算进去，再重设节点尺寸
  requestAnimationFrame(() => {
    try {
      const size = node.computeSize();
      const h = Number.isFinite(size?.[1]) ? size[1] : 320;
      // +28 给底部状态条留位置；宽度下限 360 才放得下缩略图网格
      node.setSize([Math.max(node.size[0], 360), Math.max(h + 28, 320)]);
      node.setDirtyCanvas?.(true, true);
    } catch (err) {
      // 节点还没挂到画布上，忽略
    }
  });
}

/* ==================== PromptVault 节点相关工具 ==================== */

/** 手动列表里的相对路径 → /view 地址（缩略图用） */
function imageViewUrl(rel) {
  const raw = String(rel || "");
  const idx = raw.lastIndexOf("/");
  const subfolder = idx >= 0 ? raw.slice(0, idx) : "";
  const filename = idx >= 0 ? raw.slice(idx + 1) : raw;
  const path = `/view?filename=${encodeURIComponent(filename)}`
    + `&subfolder=${encodeURIComponent(subfolder)}&type=input`;
  // ComfyUI 部署在子路径下时 apiURL 会补前缀
  return typeof api?.apiURL === "function" ? api.apiURL(path) : path;
}

/** 顶部那行「共 N 张，当前第 M 张」 */
function updateImageHeader(node) {
  const el = node?.dkDrop;
  if (!el) return;

  if (node.dogkingUploadError) {
    el.head.textContent = node.dogkingUploadError;
    return;
  }

  const manual = Array.isArray(node.dogkingManual) ? node.dogkingManual.length : 0;
  const folderTotal = Number(node.dogkingFolderTotal) || 0;
  const total = manual + folderTotal;
  if (!total) {
    el.head.textContent = "共 0 张";
    return;
  }

  const matched = String(node.dogkingStatus || "").match(/第\s*(\d+)\s*\//);
  const current = matched ? Number(matched[1]) : 0;
  el.head.textContent = current ? `共 ${total} 张，当前第 ${current} 张` : `共 ${total} 张`;
}

/** 重画缩略图网格（每个缩略图右上角有 × 可以移除） */
function renderImageDrop(node) {
  const el = node?.dkDrop;
  if (!el) return;

  const list = Array.isArray(node.dogkingManual) ? node.dogkingManual : [];
  el.grid.textContent = "";

  for (const rel of list) {
    const box = document.createElement("div");
    box.className = "dk-thumb";
    box.title = rel;

    const img = document.createElement("img");
    img.src = imageViewUrl(rel);
    img.loading = "lazy";
    img.draggable = false;
    box.appendChild(img);

    const x = document.createElement("button");
    x.className = "dk-thumb-x";
    x.textContent = "×";
    x.title = "移除";
    x.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      setManualList(node, list.filter((p) => p !== rel));
      refreshImageTotal(node);
    });
    box.appendChild(x);

    el.grid.appendChild(box);
  }

  el.grid.style.display = list.length ? "flex" : "none";
  el.empty.style.display = list.length ? "none" : "flex";
  updateImageHeader(node);
}

/** 写回「手动列表」widget（JSON 字符串），并重画 */
function setManualList(node, arr) {
  node.dogkingManual = Array.isArray(arr) ? arr.slice() : [];
  const w = (node.widgets || []).find((x) => x.name === "手动列表");
  if (w) {
    w.value = node.dogkingManual.length ? JSON.stringify(node.dogkingManual) : "";
  }
  renderImageDrop(node);
}

/**
 * 上传拖进来的图片到 ComfyUI 的 input 目录。
 *
 * 浏览器拿不到本地完整路径（安全限制），只能走 /upload/image。
 * 返回的 {name, subfolder} 拼成相对路径存进手动列表，后端再用 _resolve_path
 * 把它解析回 input 目录下的绝对路径。
 *
 * @param {object} node
 * @param {FileList|File[]} files
 */
async function uploadImageFiles(node, files) {
  const el = node?.dkDrop;
  const list = Array.from(files || []).filter(
    (f) => f && typeof f.type === "string" && f.type.startsWith("image/"),
  );
  if (!el || !list.length) return;

  node.dogkingUploadError = "";
  if (!Array.isArray(node.dogkingManual)) node.dogkingManual = [];

  for (let i = 0; i < list.length; i += 1) {
    const file = list[i];
    el.head.textContent = `上传中 ${i + 1}/${list.length} …`;
    try {
      const form = new FormData();
      form.append("image", file);
      form.append("overwrite", "true");
      const res = await api.fetchApi("/upload/image", { method: "POST", body: form });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (data?.name) {
        const rel = data.subfolder ? `${data.subfolder}/${data.name}` : data.name;
        if (!node.dogkingManual.includes(rel)) node.dogkingManual.push(rel);
      }
    } catch (err) {
      console.warn("[dogking-tools] 上传失败", file?.name, err);
      node.dogkingUploadError = `上传 ${file?.name || "图片"} 失败：${err}`;
    }
  }

  // 先把数组写进 widget（值要提交给后端），再刷新显示
  setManualList(node, node.dogkingManual);
  refreshImageTotal(node);
}

/**
 * 问后端文件夹里有多少张图（前端自己算不出文件夹内容）
 *
 * @param {object} node
 */
async function refreshImageTotal(node) {
  if (!node?.dkDrop) return;

  const widgets = node.widgets || [];
  const folder = widgets.find((w) => w.name === "文件夹路径")?.value || "";
  const sort = widgets.find((w) => w.name === "排序方式")?.value || "数字升序";

  if (!folder) {
    node.dogkingFolderTotal = 0;
    renderImageDrop(node);
    return;
  }

  try {
    const res = await api.fetchApi("/dogking-tools/list_images", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder, sort }),
    });
    const data = await res.json();
    node.dogkingFolderTotal = Number(data?.total) || 0;
    if (data?.error) node.dogkingStatus = data.error;
    node.setDirtyCanvas?.(true, true);
    renderImageDrop(node);
  } catch (err) {
    // 静默失败，不影响主流程
  }
}
