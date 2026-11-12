/**
 * dogking-tools —— 节点 2「词库」前端（T03）
 *
 * 比节点 1 简单：**只能单选**，所以没有「选择模式 / 输出方式 / 随机数量 / 自动连续」，
 * 也没有排队语义（不写 state.json）。
 *
 * 参数只有三个：服务地址 / 数据源 / 已选（隐藏）。
 * 注意：词库**没有分类维度**（LORA 触发词、编辑指令、系统提示词都是平铺的），
 * 所以不做两级联动 —— 加两个永远「全部」的下拉只会让界面更乱。
 *
 * 面板用卡片视图（320×110 矩形卡）：词条的价值在正文，缩略图网格没有意义。
 */

import { app } from "../lib/api.js";
import { hideWidgetAsData } from "../lib/ui.js";
import {
  createPanelHost,
  readSelection,
  writeSelection,
  syncCollapseButton,
} from "./panel-host.js";

const NODE = "DogkingPVVault";

/** 界面中文名 → 后端 /api/text 的 type */
const TEXT_TYPE_KEYS = {
  LORA触发词: "loras",
  编辑指令: "edits",
  LLM系统提示词: "system-prompts",
};

function findWidget(node, name) {
  return (node.widgets || []).find((w) => w.name === name) || null;
}

function textTypeOf(node) {
  const widget = findWidget(node, "数据源");
  const label = String((widget && widget.value) || "").trim();
  return TEXT_TYPE_KEYS[label] || "loras";
}

function updateSelBadge(node, count) {
  node.dogkingSelCount = count;
  node.setDirtyCanvas?.(true, true);
}

function setupVault(node) {
  const selW = findWidget(node, "已选");
  const addrW = findWidget(node, "服务地址");
  const typeW = findWidget(node, "数据源");

  if (!selW) {
    console.warn("[dogking-tools] 节点 2 找不到「已选」widget，跳过面板挂载");
    return;
  }

  // 已选词条：隐藏但仍序列化（type='hidden'）
  hideWidgetAsData(selW);

  const host = createPanelHost(node, {
    kind: "text",
    view: "card",
    pageSize: 24,
    maxSelect: 1,        // 只能单选
    getMode: () => "single",
    getFilters: () => ({
      addr: String((addrW && addrW.value) || ""),
      textType: textTypeOf(node),
    }),
    // 实时确认：点一张卡片立刻写回（面板不再有「确定」按钮，也不关闭面板）。
    // 实时确认：点一条即写回，底栏只有「清空」
    liveConfirm: true,
    onConfirm: (selected) => {
      // 单选：只保留第一项，防止历史遗留的多选清单继续往下传
      const one = selected.length ? [selected[0]] : [];
      const count = writeSelection(selW, one);
      updateSelBadge(node, count);
      host.setStatus(count ? `已选 1 条` : "未选择词条");
    },
    onChange: (selected) => {
      updateSelBadge(node, selected.length);
    },
  });
  node.__dkHost = host;

  const collapseBtn = node.addWidget("button", "收起面板", null, () => {
    const collapsed = host.toggleCollapsed();
    syncCollapseButton(collapseBtn, collapsed);
  });
  collapseBtn.serialize = false;
  node.__dkCollapseBtn = collapseBtn;

  if (typeW) {
    const previous = typeW.callback;
    typeW.callback = function () {
      const result = previous ? previous.apply(this, arguments) : undefined;
      // 换数据源 = 换了一份完全不同的列表，已选里的 id 也不属于新数据源了
      writeSelection(selW, []);
      updateSelBadge(node, 0);
      host.applyFilters();
      return result;
    };
  }

  syncCollapseButton(collapseBtn, host.collapsed);
}

/** 恢复工作流：只回填已选 + 占位条，不发任何请求 */
function restoreVault(node) {
  const selW = findWidget(node, "已选");
  const host = node.__dkHost;
  if (!selW) return;

  const sel = readSelection(selW);
  updateSelBadge(node, sel.ids.length);

  if (host) {
    // 切工作流后保持展开（旧行为是 setCollapsed(true)，用户每次都得手点展开）
    host.restoreExpanded(() => {
      if (!host.panel) return;
      try {
        host.panel.setSelection(sel.ids.slice(0, 1).map((id, i) => ({ id, name: sel.names[i] || "" })));
      } catch (err) {
        console.warn("[dogking-tools] 节点 2 回填已选失败", err);
      }
    });
    syncCollapseButton(node.__dkCollapseBtn, false);
    host.setStatus(sel.ids.length ? `已选 1 条` : "");
  }
}

app.registerExtension({
  name: "dogking.pv.vault",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (!nodeData || nodeData.name !== NODE) return;

    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const result = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;
      try {
        setupVault(this);
      } catch (err) {
        console.error("[dogking-tools] 节点 2 初始化失败", err);
      }
      return result;
    };

    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () {
      const result = onConfigure ? onConfigure.apply(this, arguments) : undefined;
      try {
        restoreVault(this);
      } catch (err) {
        console.error("[dogking-tools] 节点 2 恢复失败", err);
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
        console.warn("[dogking-tools] 节点 2 状态更新失败", err);
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

export { setupVault, restoreVault };
