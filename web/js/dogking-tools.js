/**
 * dogking-tools 前端扩展
 *
 * 1. 给「提示词批量加载」节点加「选择文件」按钮（弹系统原生选择框）
 * 2. 加「重置计数」按钮：点一下立即把本节点计数清零，不需要执行工作流
 * 3. 按模式动态显示参数：全部生成只留基础项，范围选取才显示起止行，编号选择才显示编号
 * 4. 自动连续：节点执行完如果还有下一行，自动排下一次队列
 * 5. 在节点上显示「第 N/M 行」计数器
 *
 * 注意：这里用 window.comfyAPI 而不是 import —— 插件放在 web/js/ 下时
 * 相对路径 `../../scripts/app.js` 会解析到 /extensions/scripts/ 而 404，
 * 扩展直接加载失败（表现为节点上什么按钮都没有）。
 */

const comfyAPI = window.comfyAPI || {};
const app = comfyAPI.app?.app;
const api = comfyAPI.api?.api;

const NODE_NAME = "DogkingPromptLoader";
const EVENT_NEXT = "dogking-tools.next";

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

        ctx.save();
        ctx.font = "12px ui-monospace, Consolas, monospace";
        const w = ctx.measureText(text).width;
        const y = this.size[1] - 10;
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
      };
    },
  });
}

/* ==================================================================== */

/** 记住 widget 原始状态，隐藏时把 type 和高度一起塞掉 */
function setWidgetVisible(node, widget, visible) {
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
