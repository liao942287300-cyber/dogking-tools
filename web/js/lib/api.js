/**
 * dogking-tools —— 后端调用层（T02 公共层）
 *
 * 面板和节点都只通过这里调后端，不自己写 fetch。集中在一处的好处：
 *   · 错误约定只有一套（见下），面板的「错误态 + 重试」只写一次；
 *   · 子路径部署（ComfyUI 挂在 /comfy/ 之类下面）只需要在这里处理一次；
 *   · 后端把 `ok:false` 放在 HTTP 200 里返回（PromptVault 没开时就是这样），
 *     这个坑只在这里踩一次，不会每个调用点各踩一遍。
 *
 * ⚠️ 错误约定：**失败一律 throw**（抛 Error，带 .payload 原始响应）。
 *    成功时返回后端 JSON。不混用「返回 {ok:false}」那种写法 ——
 *    混用必然有人忘了判断，表现为「面板空白但控制台没报错」。
 */

/**
 * ComfyUI 的 app / api。
 *
 * 为什么不在模块顶层只取一次：这个模块可能比 comfyAPI 先加载完
 * （扩展加载顺序不受我们控制），顶层取到的就是 undefined，之后永远拿不回来。
 * 所以导出常量给「已经就绪」的场景用，函数内部一律走 getApi() 重新取。
 */
export const app = (window.comfyAPI && window.comfyAPI.app && window.comfyAPI.app.app) || window.app;
export const api = (window.comfyAPI && window.comfyAPI.api && window.comfyAPI.api.api) || window.api;

/** 每次调用时重新解析（模块顶层取值可能太早，那时 comfyAPI 还没挂上） */
function getApp() {
  return (window.comfyAPI && window.comfyAPI.app && window.comfyAPI.app.app) || window.app || null;
}

function getApi() {
  return (window.comfyAPI && window.comfyAPI.api && window.comfyAPI.api.api) || window.api || null;
}

/**
 * 拼后端地址。
 *
 * 必须走 api.apiURL()：ComfyUI 可能部署在子路径下（`/comfy/`），
 * 硬编码 `/dogking-tools/...` 会 404。apiURL 在部分版本里会拼出双斜杠
 * （api_base 以 `/` 结尾时），这里顺手折叠掉 —— 双斜杠对 aiohttp 是另一个路径。
 *
 * @param {string} path 形如 /dogking-tools/pv_list
 * @returns {string}
 */
export function apiUrl(path) {
  const a = getApi();
  let url = path;
  if (a && typeof a.apiURL === "function") {
    try {
      url = String(a.apiURL(path) || path);
    } catch (err) {
      console.warn("[dogking-tools] apiURL 失败，退回原始路径", err);
      url = path;
    }
  }
  // 折叠重复斜杠，但别动 http:// 里的 `//`
  return url.replace(/([^:/])\/{2,}/g, "$1/");
}

/**
 * 解析响应：HTTP 状态 + 后端业务状态两层都要判。
 *
 * 后端约定：业务失败用 `{ok:false, error:"..."}` 返回，HTTP 码多数是 200
 * （pv_thumb 例外，它用 404/503）。只看 res.ok 会把「PromptVault 没开」
 * 当成成功，面板就会渲染一个空列表而不是错误提示。
 *
 * @param {Response} res
 * @param {string} path 仅用于错误文案
 * @returns {Promise<object>} 后端 JSON
 */
async function parseResponse(res, path) {
  let payload = null;
  let text = "";
  try {
    text = await res.text();
    payload = text ? JSON.parse(text) : null;
  } catch (err) {
    payload = null;
  }

  if (!res.ok) {
    const msg = (payload && payload.error) || text || `HTTP ${res.status}`;
    throw makeError(`请求失败（${path}）：${msg}`, payload, res.status);
  }
  if (payload && payload.ok === false) {
    throw makeError(payload.error || "接口返回失败", payload, res.status);
  }
  if (!payload) {
    throw makeError(`响应不是 JSON（${path}）`, null, res.status);
  }
  return payload;
}

/** 统一的错误对象：带 payload 方便调用方按需取字段（比如 missing） */
function makeError(message, payload, status) {
  const err = new Error(String(message || "未知错误"));
  err.payload = payload || null;
  err.status = status || 0;
  return err;
}

/**
 * POST 一个 JSON 接口。
 * @param {string} path
 * @param {object} [body]
 * @returns {Promise<object>}
 */
async function post(path, body) {
  const res = await fetch(apiUrl(path), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return parseResponse(res, path);
}

/* ---------------------------------------------------------------- 具体接口 */

/** 分页列表 → {ok,total,page,pageSize,pages,addr,tookMs,items} */
export function pvList(body) {
  return post("/dogking-tools/pv_list", body);
}

/** 按 id 批量取完整元数据 → {ok,items,missing} */
export function pvMeta(body) {
  return post("/dogking-tools/pv_meta", body);
}

/** 从当前筛选范围随机抽 N 个 → {ok,ids,total} */
export function pvRandom(body) {
  return post("/dogking-tools/pv_random", body);
}

/** 分类/项目下拉 → {ok,categories,projects?} */
export function pvCategories(body) {
  return post("/dogking-tools/pv_categories", body);
}

/** 重置某个节点的排队计数（现有接口） */
export function resetCount(body) {
  return post("/dogking-tools/reset", body);
}

/**
 * 缩略图 URL（给 <img src> 直接用，所以是同步函数，不返回 Promise）。
 *
 * @param {{id:string, kind?:string, size?:string, addr?:string}} opts
 * @returns {string}
 */
export function pvThumbUrl(opts) {
  const o = opts || {};
  const base = apiUrl("/dogking-tools/pv_thumb");
  const params = new URLSearchParams();
  params.set("id", String(o.id == null ? "" : o.id));
  params.set("kind", String(o.kind || "image"));
  params.set("size", String(o.size || "thumb"));
  if (o.addr) params.set("addr", String(o.addr));
  const glue = base.indexOf("?") >= 0 ? "&" : "?";
  return `${base}${glue}${params.toString()}`;
}

export default {
  app,
  api,
  apiUrl,
  pvList,
  pvMeta,
  pvRandom,
  pvCategories,
  resetCount,
  pvThumbUrl,
};
