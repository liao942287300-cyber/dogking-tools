/**
 * 提示词嵌入 PNG 元数据 —— 标题栏状态 badge
 *
 * 只做一件事：把后端返回的 `ui.dogking_badge`（短文案，例如
 * `PVMeta_00001_.png · 空词`）挂到节点标题栏右侧的 badge 上。
 *
 * 🔴 为什么不用 `onDrawForeground` 自己画一条底栏（本项目其它节点是那么做的）：
 *    前端渲染顺序是
 *        drawNodeShape → drawBadges → node.onDrawForeground → drawSlots → drawNodeWidgets
 *    **widget 是在 onDrawForeground 之后画的**，所以画在节点底部的状态条会被
 *    最后一个输入框盖住 —— 实测节点尺寸报 222、字也确实画了，屏幕上就是看不见。
 *    `node.badges` 由前端在标题栏自己画，位置固定、不会被任何 widget 遮挡。
 *
 * 拿不到 `LGraphBadge`（老版本前端）时不报错，只是不显示 badge；
 * 完整文案仍然在 `ui.dogking_status` 和控制台日志里。
 */

const comfyAPI = window.comfyAPI || {};
const app = comfyAPI.app?.app;

const NODE_META = "DogkingPVMetaStamp";

/** badge 配色跟着节点走：深底 + 浅蓝字，和项目其它节点的状态条同色系 */
const BADGE_STYLE = {
  bgColor: "#24406e",
  fgColor: "#cfe0ff",
  fontSize: 11,
  height: 18,
  padding: 7,
  cornerRadius: 9,
};

/**
 * 算 badge 该往左挪多少。
 *
 * 前端排 badge 的位置时用 `this.width - getWidth()` 推算起点，而 getWidth 量的是
 * 「估算宽度」；真正绘制时字宽比它大约 35%（实测：22 个字符的
 * `PromptVault_00002_.png`，估算 151px，画出来 177px）—— 于是整条标签从节点
 * 右边缘溢出到画布上。
 *
 * 这里按字符数补一个负 xOffset 把它拉回节点内部。挪多一点也不报错（只是偏左），
 * 所以这个系数宁可取大不取小。
 */
function badgeShift(text) {
  return -Math.round(text.length * BADGE_STYLE.fontSize * 0.35);
}

if (app) {
  app.registerExtension({
    name: "dogking.tools.metaStamp",

    async beforeRegisterNodeDef(nodeType, nodeData) {
      if (nodeData.name !== NODE_META) return;

      const onExecuted = nodeType.prototype.onExecuted;
      nodeType.prototype.onExecuted = function (message) {
        onExecuted?.apply(this, arguments);

        const text = message?.dogking_badge?.[0];
        // 后端说「没有可显示的内容」时把旧 badge 清掉，
        // 否则上一次成功执行的绿标会一直挂在那儿骗人。
        const Badge = window.LGraphBadge;
        if (!text || typeof Badge !== "function") {
          this.badges = [];
          this.setDirtyCanvas?.(true, true);
          return;
        }

        try {
          this.badges = [new Badge({ text, ...BADGE_STYLE, xOffset: badgeShift(text) })];
        } catch (err) {
          this.badges = [];
        }
        this.setDirtyCanvas?.(true, true);
      };
    },
  });
}
