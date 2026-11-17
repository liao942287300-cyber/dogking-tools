/**
 * 分段视频合并 —— 标题栏状态 badge
 *
 * 这个节点没有面板（它就是个累积器），执行进度只能靠标题栏显示：
 *   `3/8 已存 3 段` → 进行中
 *   `完成 1909 帧`   → 最后一段已经拼好
 *
 * 为什么用 `node.badges` 而不是自己画：前端渲染顺序是
 *   drawNodeShape → drawBadges → onDrawForeground → drawSlots → drawNodeWidgets
 * widget 画在 onDrawForeground **之后**，画在节点里的状态条会被输入框盖住。
 */

const comfyAPI = window.comfyAPI || {};
const app = comfyAPI.app?.app;

const NODE_CONCAT = "DogkingVideoConcat";

/** 配色：进行中偏蓝、完成偏绿，扫一眼就知道这轮跑完没有 */
const STYLE_PENDING = {
  bgColor: "#24406e",
  fgColor: "#cfe0ff",
  fontSize: 11,
  height: 18,
  padding: 7,
  cornerRadius: 9,
};

const STYLE_DONE = {
  bgColor: "#1c5b46",
  fgColor: "#c8ffe8",
  fontSize: 11,
  height: 18,
  padding: 7,
  cornerRadius: 9,
};

/**
 * badge 宽度靠前端估算，实际字宽比估算大约 35%，
 * 不补这个偏移会从节点右边缘溢出去（同 meta-stamp.js 的处理）。
 */
function badgeShift(text, style) {
  return -Math.round(text.length * style.fontSize * 0.35);
}

if (app) {
  app.registerExtension({
    name: "dogking.tools.videoConcat",

    async beforeRegisterNodeDef(nodeType, nodeData) {
      if (nodeData.name !== NODE_CONCAT) return;

      const onExecuted = nodeType.prototype.onExecuted;
      nodeType.prototype.onExecuted = function (message) {
        onExecuted?.apply(this, arguments);

        const text = message?.dogking_badge?.[0];
        // 后端没给文案时清掉旧 badge，否则上一轮的「完成 1909 帧」会一直挂着骗人
        const Badge = window.LGraphBadge;
        if (!text || typeof Badge !== "function") {
          this.badges = [];
          this.setDirtyCanvas?.(true, true);
          return;
        }

        // 文案以「完成」/「单段」开头就是收工状态，其余算进行中
        const done = text.startsWith("完成") || text.startsWith("单段");
        const style = done ? STYLE_DONE : STYLE_PENDING;
        try {
          this.badges = [new Badge({ text, ...style, xOffset: badgeShift(text, style) })];
        } catch (err) {
          this.badges = [];
        }
        this.setDirtyCanvas?.(true, true);
      };
    },
  });
}
