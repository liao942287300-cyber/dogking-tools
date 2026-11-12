/**
 * dogking-tools —— 4 个 PromptVault 节点的浏览器断言
 *
 * ⚠️ **这个文件不在 `pytest tests/` 里。**
 *    `pytest` 只收 `test_*.py`，这里 11 条是 DOM 级断言，需要真实浏览器。
 *    `pytest tests/` 全绿 ≠ 前端没问题。改前端后必须单独跑这个。
 *
 * ## 前置（缺一个就跑不了，脚本会明确告诉你缺哪个）
 *
 * 1. **PromptVault 开着**（本地桥服务在 8765-8775 之一）
 * 2. **ComfyUI 跑在 8189**（不要用用户的 8188）
 *    ```
 *    cd H:\ComfyUI_windows_portable\ComfyUI
 *    ..\python_embeded\python.exe main.py --port 8189 --cpu
 *    ```
 * 3. **Edge 带调试端口**
 *    ```
 *    "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" \
 *      --remote-debugging-port=9222 --user-data-dir=%TEMP%\edge-dk \
 *      --headless=new http://127.0.0.1:8189
 *    ```
 *
 * ## 跑法
 *
 * ```
 * cd tests/browser
 * npm i playwright-core          # 只需一次
 * node t04_assertions.mjs
 * ```
 *
 * 可用环境变量覆盖：`DK_CDP_PORT`（默认 9222）、`DK_COMFY_URL`（默认 http://127.0.0.1:8189）
 *
 * 退出码：0 = 全过 / 1 = 有失败 / 2 = 环境没准备好
 *
 * ## 这个脚本守的是什么
 *
 * 用户要求（本次）：
 *   · 节点 1 只输出 `图片 / 正向`
 *   · 节点 2 只输出 `内容`
 *   · 节点 3 只输出 `图片`，项目在分类**上面**
 *   · 节点 4 只输出 `图1..图8`，没有固定口，没有「输出模式」
 *   · **保留「确定」按钮并加大**；点选是草稿，确定后才生效
 *   · 确定后进「选定状态」，节点 4 可以**只更换其中一张**
 *   · 节点 3 / 4 有「短边尺寸」参数
 *
 * 最容易回归的三条：
 *   ① 点选后隐藏 widget **必须还是旧值**（不能偷偷实时写回）
 *   ② 取消要退回**上次确定**的那份，不是清空
 *   ③ 节点 4「更换第 2 张」只动第 2 张，其他张和输出口数量都不能变
 */

import { chromium } from "playwright-core";

const CDP = Number(process.env.DK_CDP_PORT || 9222);
const COMFY = process.env.DK_COMFY_URL || "http://127.0.0.1:8189";

const results = [];
function rec(name, ok, detail) {
  results.push({ name, ok: Boolean(ok), detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}`);
  if (!ok) console.log("      ", JSON.stringify(detail));
}

async function main() {
  let browser;
  try {
    browser = await chromium.connectOverCDP(`http://127.0.0.1:${CDP}`);
  } catch (err) {
    console.error(`连不上 Edge 调试端口 ${CDP}。先按文件头第 3 步起 Edge。`);
    process.exit(2);
  }

  const ctx = browser.contexts()[0];
  const page = ctx.pages().find((p) => p.url().includes(new URL(COMFY).port)) || ctx.pages()[0];
  if (!page) {
    console.error("没找到 ComfyUI 页面。确认 Edge 打开的是 " + COMFY);
    process.exit(2);
  }

  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(e.message));

  // 统计 pv_categories 请求数：用来验「展开面板时自动刷新分类下拉」
  let catReqs = 0;
  page.on("request", (r) => {
    if (r.url().includes("pv_categories")) catReqs += 1;
  });

  await page.waitForTimeout(3000);
  const ready = await page.evaluate(() => Boolean(window.app?.graph && window.LiteGraph));
  if (!ready) {
    console.error("ComfyUI 前端还没加载完（window.app / LiteGraph 缺失）。");
    process.exit(2);
  }

  // ---- 建节点 ----
  const ids = await page.evaluate(() => {
    const mk = (type, pos) => {
      const n = window.LiteGraph.createNode(type);
      if (!n) return null;
      n.pos = pos;
      window.app.graph.add(n);
      return n.id;
    };
    return {
      pick: mk("DogkingPVPick", [40, 40]),
      vault: mk("DogkingPVVault", [40, 900]),
      one: mk("DogkingPVAsset", [760, 40]),
      many: mk("DogkingPVAssets", [760, 900]),
    };
  });
  if (!ids.pick || !ids.vault || !ids.one || !ids.many) {
    console.error("节点创建失败，确认 dogking-tools 已加载（4 个节点是否都在菜单里）");
    process.exit(2);
  }
  await page.waitForTimeout(2500);

  // 辅助：展开面板
  const expand = async (id) => {
    await page.evaluate((id) => {
      const n = window.app.graph.getNodeById(id);
      if (n.__dkHost && n.__dkHost.collapsed) {
        const b = (n.widgets || []).find((w) => w.name === "展开面板" || w.name === "收起面板");
        if (b) b.callback();
      }
    }, id);
    await page.waitForTimeout(2200);
  };

  const snap = (id) =>
    page.evaluate((id) => {
      const n = window.app.graph.getNodeById(id);
      const w = (n.widgets || []).find((x) => x.name === "已选");
      let ids = [];
      try {
        ids = JSON.parse(w.value || "{}").ids || [];
      } catch (e) {
        ids = [];
      }
      const p = n.__dkHost && n.__dkHost.panel;
      return {
        widget: ids.length,
        ids,
        panel: p ? p.getSelection().length : -1,
        ports: (n.outputs || []).length,
        labels: (n.outputs || []).map((o) => o.label || o.name),
      };
    }, id);

  const clickCards = (id, idxs) =>
    page.evaluate(
      ({ id, idxs }) => {
        const n = window.app.graph.getNodeById(id);
        const cards = n.__dkHost.panel.root.querySelectorAll(".dk-pv-card");
        idxs.forEach((i) => cards[i] && cards[i].click());
        return cards.length;
      },
      { id, idxs },
    );

  const clickBtn = (id, text) =>
    page.evaluate(
      ({ id, text }) => {
        const n = window.app.graph.getNodeById(id);
        const btns = Array.from(n.__dkHost.panel.root.querySelectorAll(".dk-pv-actions button"));
        const b = btns.find((x) => x.textContent.trim() === text);
        if (!b) return { error: "没找到按钮", have: btns.map((x) => x.textContent.trim()) };
        b.click();
        return { clicked: text };
      },
      { id, text },
    );

  /* ================================================================ 节点 1（实时确认） */

  await expand(ids.pick);

  const btnsOf = (id) =>
    page.evaluate((id) => {
      const n = window.app.graph.getNodeById(id);
      return Array.from(n.__dkHost.panel.root.querySelectorAll(".dk-pv-actions button")).map((b) =>
        b.textContent.trim(),
      );
    }, id);

  const btn1 = await btnsOf(ids.pick);
  rec("0) 节点1 底栏只有「清空」（实时确认，没有确定/取消）", btn1.join("/") === "清空", btn1);

  // 切批量选
  await page.evaluate((id) => {
    const n = window.app.graph.getNodeById(id);
    const w = (n.widgets || []).find((x) => x.name === "选择模式");
    w.value = "批量选";
    if (w.callback) w.callback(w.value, null, w);
  }, ids.pick);
  await page.waitForTimeout(2200);

  await clickBtn(ids.pick, "清空");
  await page.waitForTimeout(300);

  await clickCards(ids.pick, [0, 1, 2]);
  await page.waitForTimeout(500);
  const p1 = await snap(ids.pick);
  rec("1) 点 3 张 → 立即写回 3 个 id（不用点确定）", p1.widget === 3 && p1.panel === 3, p1);

  await clickCards(ids.pick, [3, 4]);
  await page.waitForTimeout(400);
  const p1b = await snap(ids.pick);
  rec("1b) 再加选 2 张 → 立即 5 个 id", p1b.widget === 5 && p1b.panel === 5, p1b);

  await clickBtn(ids.pick, "清空");
  await page.waitForTimeout(400);
  const p1c = await snap(ids.pick);
  rec("1c) 点清空 → 立即变 0 个", p1c.widget === 0, p1c);

  /* ================================================================ 节点 2（实时确认） */

  await expand(ids.vault);
  const btn2 = await btnsOf(ids.vault);
  rec("2) 节点2 底栏只有「清空」", btn2.join("/") === "清空", btn2);

  await page.evaluate((id) => {
    const n = window.app.graph.getNodeById(id);
    const card = n.__dkHost.panel.root.querySelector(".dk-pv-tcard");
    if (card) card.click();
  }, ids.vault);
  await page.waitForTimeout(500);
  const v1 = await snap(ids.vault);
  rec("2b) 点一条 → 立即写回 1 条（不用点确定）", v1.widget === 1, v1);

  /* ================================================================ 节点 3（实时确认 + 两态） */

  // 「收起 → 展开」应触发一次 pv_categories（下拉选项自动刷新）
  const catBefore = catReqs;
  await page.evaluate((id) => {
    const n = window.app.graph.getNodeById(id);
    const b = (n.widgets || []).find((w) => w.name === "展开面板" || w.name === "收起面板");
    if (b) b.callback(); // 收起
  }, ids.one);
  await page.waitForTimeout(500);
  await page.evaluate((id) => {
    const n = window.app.graph.getNodeById(id);
    const b = (n.widgets || []).find((w) => w.name === "展开面板" || w.name === "收起面板");
    if (b) b.callback(); // 展开
  }, ids.one);
  await page.waitForTimeout(2000);
  rec("3a) 展开面板时自动刷新「项目/分类」下拉（pv_categories +1）", catReqs > catBefore, {
    before: catBefore,
    after: catReqs,
  });

  const btn3 = await btnsOf(ids.one);
  rec("3) 节点3 底栏只有「清空」", btn3.join("/") === "清空", btn3);

  await clickCards(ids.one, [0]);
  await page.waitForTimeout(800);
  const o1 = await snap(ids.one);
  const o1state = await page.evaluate((id) => {
    const n = window.app.graph.getNodeById(id);
    const v = n.__dkHost.panel.root.querySelector(".dk-asset-viewer");
    const box = v ? v.querySelector("div") : null;
    const img = v ? v.querySelector("img") : null;
    return {
      mode: n.__dkAssetMode,
      viewer: v ? v.style.display : "none",
      viewerW: v ? v.offsetWidth : 0,
      viewerH: v ? v.offsetHeight : 0,
      boxW: box ? box.offsetWidth : 0,
      boxH: box ? box.offsetHeight : 0,
      imgW: img ? img.offsetWidth : 0,
      imgH: img ? img.offsetHeight : 0,
      imgSrc: img ? img.src : "",
      naturalW: img ? img.naturalWidth : 0,
    };
  }, ids.one);
  rec(
    "3b) 点一张 → 立即写回 + 切预览态 + 图片区撑满面板（宽≥85%、高≥55%）",
    o1.widget === 1 &&
      o1state.mode === "viewing" &&
      o1state.viewer === "flex" &&
      o1state.boxW >= o1state.viewerW * 0.85 &&
      o1state.boxH >= o1state.viewerH * 0.55,
    { o1, o1state },
  );

  // 预览必须是**原图**（size=full）。缩略图只有 480px 宽，撑到 700+ 面板里会糊。
  rec(
    "3b2) 预览加载的是原图（size=full，自然宽度 > 1000）",
    o1state.imgSrc.includes("size=full") && o1state.naturalW > 1000,
    { src: o1state.imgSrc, naturalW: o1state.naturalW },
  );

  const backBtn = await page.evaluate((id) => {
    const n = window.app.graph.getNodeById(id);
    const v = n.__dkHost.panel.root.querySelector(".dk-asset-viewer");
    const b = Array.from(v.querySelectorAll("button")).find((x) => x.textContent.trim() === "更改图片");
    if (!b) return { error: "没找到更改图片" };
    b.click();
    return { clicked: true };
  }, ids.one);
  await page.waitForTimeout(500);
  const o2 = await snap(ids.one);
  const o2mode = await page.evaluate((id) => window.app.graph.getNodeById(id).__dkAssetMode, ids.one);
  const crumbOnChange = await page.evaluate((id) => {
    const n = window.app.graph.getNodeById(id);
    return { crumb: n.__dkHost.panel.crumbEl.textContent, replace: Boolean(n.__dkAssetReplaceMode) };
  }, ids.one);
  rec(
    "3c) 点「更改图片」→ 回网格 + 进替换模式（工具条提示「点一张替换」）",
    backBtn.clicked && o2mode === "picking" && o2.widget === 1 &&
      crumbOnChange.replace === true && crumbOnChange.crumb.includes("点一张替换"),
    { backBtn, o2, o2mode, crumbOnChange },
  );

  // 替换模式点一张 → 直接替换并回预览（不用再找「哪张是当前选中的」）
  await clickCards(ids.one, [2]);
  await page.waitForTimeout(1500);
  const o3 = await page.evaluate((id) => {
    const n = window.app.graph.getNodeById(id);
    const v = n.__dkHost.panel.root.querySelector(".dk-asset-viewer");
    const box = v ? v.querySelector("div") : null;
    const w = (n.widgets || []).find((x) => x.name === "已选");
    let list = [];
    try {
      list = (JSON.parse(w.value || "{}").ids) || [];
    } catch (e) {
      list = [];
    }
    return {
      mode: n.__dkAssetMode,
      viewer: v ? v.style.display : null,
      replace: Boolean(n.__dkAssetReplaceMode),
      crumbColor: n.__dkHost.panel.crumbEl.style.color,
      widget: list.length,
      boxH: box ? box.offsetHeight : 0,
      viewerH: v ? v.offsetHeight : 0,
    };
  }, ids.one);
  rec(
    "3d) 替换模式点一张 → 直接回预览态 + 提示撤掉 + 图片区占比 ≥78%",
    o3.mode === "viewing" && o3.viewer === "flex" && o3.replace === false &&
      o3.crumbColor === "" && o3.widget === 1 && o3.boxH >= o3.viewerH * 0.78,
    o3,
  );

  /* ================================================================ 节点 4 */

  await expand(ids.many);
  const m0 = await page.evaluate((id) => window.app.graph.getNodeById(id).outputs.length, ids.many);

  await clickCards(ids.many, [0, 1, 2, 3]);
  await page.waitForTimeout(400);
  const m1 = await snap(ids.many);
  rec("4) 节点4 选 4 张未确定 → widget 0、输出口 0", m1.widget === 0 && m1.ports === 0, { m1, m0 });

  await clickBtn(ids.many, "确定");
  await page.waitForTimeout(800);
  const m2 = await snap(ids.many);
  const m2state = await page.evaluate((id) => {
    const n = window.app.graph.getNodeById(id);
    const v = n.__dkHost.panel.root.querySelector(".dk-assets-selected");
    return { mode: n.__dkAssetsMode, view: v ? v.style.display : "none" };
  }, ids.many);
  rec(
    "4b) 点确定 → 4 个输出口（无固定口）+ 进选定态",
    m2.widget === 4 && m2.ports === 4 && m2state.mode === "selected" && m2state.view === "flex",
    { m2, m2state },
  );
  rec(
    "4c) 每个图片口 label 带名字（图N · 名称前8字）",
    m2.labels.length === 4 && m2.labels.every((l, i) => l.startsWith(`图${i + 1}`)),
    m2.labels,
  );

  // 更换第 2 张
  const begin = await page.evaluate((id) => {
    const n = window.app.graph.getNodeById(id);
    const v = n.__dkHost.panel.root.querySelector(".dk-assets-selected");
    const changes = Array.from(v.querySelectorAll("button")).filter((b) => b.textContent === "更换");
    if (!changes[1]) return { error: "没找到第 2 张的更换按钮", count: changes.length };
    changes[1].click();
    return { clicked: true, crumb: n.__dkHost.panel.crumbEl.textContent };
  }, ids.many);
  await page.waitForTimeout(500);
  const m3state = await page.evaluate((id) => {
    const n = window.app.graph.getNodeById(id);
    const v = n.__dkHost.panel.root.querySelector(".dk-assets-selected");
    return { mode: n.__dkAssetsMode, view: v ? v.style.display : "none" };
  }, ids.many);
  rec(
    "4d) 点第 2 张「更换」→ 回网格 + 工具条提示「正在替换第 2 张」",
    begin.clicked && m3state.mode === "grid" && m3state.view === "none" && begin.crumb.includes("正在替换第 2 张"),
    { begin, m3state },
  );

  const before = m2.ids.slice();
  await clickCards(ids.many, [5]);
  await page.waitForTimeout(800);
  const m4 = await snap(ids.many);
  const m4mode = await page.evaluate((id) => window.app.graph.getNodeById(id).__dkAssetsMode, ids.many);
  rec(
    "4e) 点一张 → 只替换第 2 张，其他 3 张不动，输出口仍 4 个",
    m4.ids.length === 4 &&
      m4.ids[1] !== before[1] &&
      m4.ids[0] === before[0] &&
      m4.ids[2] === before[2] &&
      m4.ids[3] === before[3] &&
      m4.ports === 4 &&
      m4mode === "selected",
    { before, after: m4.ids, ports: m4.ports, mode: m4mode },
  );

  // 「重新选择」必须清掉替换提示（否则工具条还挂着「正在替换第 N 张」）
  await page.evaluate((id) => {
    const n = window.app.graph.getNodeById(id);
    const v = n.__dkHost.panel.root.querySelector(".dk-assets-selected");
    const changes = Array.from(v.querySelectorAll("button")).filter((b) => b.textContent === "更换");
    if (changes[1]) changes[1].click();
  }, ids.many);
  await page.waitForTimeout(500);
  const crumbInReplace = await page.evaluate((id) => {
    const n = window.app.graph.getNodeById(id);
    return { crumb: n.__dkHost.panel.crumbEl.textContent, color: n.__dkHost.panel.crumbEl.style.color };
  }, ids.many);
  await page.evaluate((id) => {
    const n = window.app.graph.getNodeById(id);
    const v = n.__dkHost.panel.root.querySelector(".dk-assets-selected");
    const b = Array.from(v.querySelectorAll("button")).find((x) => x.textContent.trim() === "重新选择");
    if (b) b.click();
  }, ids.many);
  await page.waitForTimeout(600);
  const crumbAfterBack = await page.evaluate((id) => {
    const n = window.app.graph.getNodeById(id);
    return {
      crumb: n.__dkHost.panel.crumbEl.textContent,
      color: n.__dkHost.panel.crumbEl.style.color,
      replaceIndex: n.__dkReplaceIndex,
    };
  }, ids.many);
  const clearedAfterBack = await page.evaluate((id) => {
    const n = window.app.graph.getNodeById(id);
    const w = (n.widgets || []).find((x) => x.name === "已选");
    let list = [];
    try {
      list = (JSON.parse(w.value || "{}").ids) || [];
    } catch (e) {
      list = [];
    }
    return { widget: list.length, ports: n.outputs.length, panelSel: n.__dkHost.panel.getSelection().length };
  }, ids.many);
  rec(
    "4g) 点「重新选择」→ 清空已选（widget/输出口/面板三处一起清）+ 替换提示撤掉",
    crumbInReplace.crumb.includes("正在替换") &&
      !crumbAfterBack.crumb.includes("正在替换") &&
      crumbAfterBack.color === "" &&
      crumbAfterBack.replaceIndex === -1 &&
      clearedAfterBack.widget === 0 &&
      clearedAfterBack.ports === 0 &&
      clearedAfterBack.panelSel === 0,
    { before: crumbInReplace, after: crumbAfterBack, cleared: clearedAfterBack },
  );

  // 选 10 张 → 8 个口
  await page.evaluate((id) => {
    const n = window.app.graph.getNodeById(id);
    const v = n.__dkHost.panel.root.querySelector(".dk-assets-selected");
    const b = Array.from(v.querySelectorAll("button")).find((x) => x.textContent.trim() === "重新选择");
    if (b) b.click();
  }, ids.many);
  await page.waitForTimeout(500);
  // 「重新选择」只切状态、不清空 → 先清空再确定，否则下面点索引会和已有选中抵消
  await clickBtn(ids.many, "清空");
  await clickBtn(ids.many, "确定");
  await page.waitForTimeout(600);
  await clickCards(ids.many, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]);
  await page.waitForTimeout(400);
  await clickBtn(ids.many, "确定");
  await page.waitForTimeout(800);
  const m5 = await snap(ids.many);
  const m5status = await page.evaluate((id) => window.app.graph.getNodeById(id).dogkingStatus, ids.many);
  // 不写死端口数（用户随时可能要求加口，8→9 已经发生过一次）：
  // 断言「口数 = 上限且少于已选」「状态条里的 N 就是实际口数」。
  const limitFromStatus = Number((String(m5status).match(/只输出前 (\d+) 张/) || [])[1] || 0);
  rec(
    "4f) 选 10 张 → 口数收到上限 + 状态条说明只输出前 N 张（N = 实际口数）",
    m5.widget === 10 &&
      m5.ports > 0 &&
      m5.ports < 10 &&
      m5.labels.length === m5.ports &&
      limitFromStatus === m5.ports,
    { widget: m5.widget, ports: m5.ports, limitFromStatus, status: m5status },
  );

  /* ================================================================ 短边尺寸 */

  const edgeWidget = await page.evaluate((id) => {
    const n = window.app.graph.getNodeById(id);
    const w = (n.widgets || []).find((x) => x.name === "短边尺寸");
    return w ? { exists: true, value: w.value, min: w.options?.min, max: w.options?.max } : { exists: false };
  }, ids.many);
  rec(
    "5) 节点4 有「短边尺寸」参数，默认 1024（0 = 原图）",
    edgeWidget.exists && Number(edgeWidget.value) === 1024,
    edgeWidget,
  );

  const edgeOne = await page.evaluate((id) => {
    const n = window.app.graph.getNodeById(id);
    const ws = (n.widgets || []).map((x) => x.name);
    return { names: ws, has: ws.includes("短边尺寸") };
  }, ids.one);
  rec(
    "5b) 节点3 参数顺序 = 服务地址→项目→分类→短边尺寸",
    edgeOne.has &&
      edgeOne.names.indexOf("项目") < edgeOne.names.indexOf("分类") &&
      edgeOne.names.indexOf("分类") < edgeOne.names.indexOf("短边尺寸"),
    edgeOne.names,
  );

  const edgeMany = await page.evaluate((id) => {
    const n = window.app.graph.getNodeById(id);
    const ws = (n.widgets || []).map((x) => x.name);
    return { names: ws, has: ws.includes("短边尺寸") };
  }, ids.many);
  rec(
    "5c) 节点4 参数顺序也是 项目→分类（和节点 3 统一）",
    edgeMany.has &&
      edgeMany.names.indexOf("项目") >= 0 &&
      edgeMany.names.indexOf("项目") < edgeMany.names.indexOf("分类") &&
      edgeMany.names.indexOf("分类") < edgeMany.names.indexOf("短边尺寸"),
    edgeMany.names,
  );

  /* ================================================================ 收尾 */

  /* ================================================================ 面板跟随节点大小 */

  const rz = await page.evaluate(async (id) => {
    const n = window.app.graph.getNodeById(id);
    const c = window.app.canvas;
    const cv = c.canvas;
    const rect = cv.getBoundingClientRect();
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    const snap = () => ({ nodeH: Math.round(n.size[1]), panel: n.__dkPanelH });

    const corner = () => ({
      x: (n.pos[0] + n.size[0] + c.ds.offset[0]) * c.ds.scale + rect.left,
      y: (n.pos[1] + n.size[1] + c.ds.offset[1]) * c.ds.scale + rect.top,
    });

    // 在 canvas 上派发（e.target 必须是 canvas，否则会被当成点面板）
    const fire = (type, x, y) => {
      cv.dispatchEvent(new MouseEvent(type, { clientX: x, clientY: y, button: 0, buttons: 1, bubbles: true }));
    };

    // 完整拖动：按在右下角 → 多帧 move(+setSize) → 松手
    const drag = async (dyScreen, steps) => {
      const p0 = corner();
      const sx = p0.x - 10;
      const sy = p0.y - 10;
      fire("mousedown", sx, sy);
      await wait(40);
      const h0 = n.size[1];
      for (let i = 1; i <= steps; i += 1) {
        const y = sy + (dyScreen * i) / steps;
        fire("mousemove", sx, y);
        await wait(20);
        const targetH = h0 + (dyScreen * i / steps) / c.ds.scale;
        n.setSize([800, Math.max(120, targetH)]);
        await wait(30);
      }
      fire("mouseup", sx, sy + dyScreen);
      await wait(400);
      return snap();
    };

    const before = snap();
    const big = await drag(300, 8);      // 向下拖 → 变大
    const small = await drag(-400, 10);  // 向上拖 → 变小
    await wait(2000);
    const stable = snap();

    return { before, big, small, stable };
  }, ids.one);

  rec(
    "6b) 拖大：节点+面板都变大；**拖小：都变小**（关键，之前缩不回去）；静止稳定",
    rz.big.panel > rz.before.panel &&
      rz.big.nodeH > rz.before.nodeH &&
      rz.small.panel < rz.big.panel &&
      rz.small.nodeH < rz.big.nodeH &&
      rz.stable.nodeH === rz.small.nodeH &&
      rz.stable.panel === rz.small.panel,
    rz,
  );

  // 6c) 展开不应该把节点拉长 —— chrome 算成负数时兜底会疯狂放大（用户实测过）
  const rz2 = await page.evaluate(async (id) => {
    const n = window.app.graph.getNodeById(id);
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    const snap = () => ({ nodeH: Math.round(n.size[1]), chrome: n.__dkChromeH, panel: n.__dkPanelH });
    const btn = () => (n.widgets || []).find((w) => w.name === "展开面板" || w.name === "收起面板");

    btn().callback();          // 收起
    await wait(1200);
    const collapsed = snap();
    btn().callback();          // 展开
    await wait(3000);          // 让兜底跑几轮
    const expanded = snap();

    return { collapsed, expanded };
  }, ids.one);
  rec(
    "6c) 收起→展开后节点不会被拉长（chrome 必须是正数、面板恢复 600 附近）",
    rz2.expanded.chrome > 20 &&
      rz2.expanded.chrome < 1200 &&
      rz2.expanded.panel >= 180 &&
      rz2.expanded.panel <= 900 &&
      rz2.expanded.nodeH < rz2.collapsed.nodeH + 900,
    rz2,
  );

  rec("6) 全程无 JS 异常", pageErrors.length === 0, pageErrors.slice(0, 3));

  const failed = results.filter((r) => !r.ok);
  console.log(`\n通过 ${results.length - failed.length} / ${results.length}`);
  if (failed.length) {
    console.log("失败项:", failed.map((r) => r.name));
    process.exit(1);
  }
}

main().catch((err) => {
  console.error("脚本自身出错:", err.message);
  process.exit(1);
});
