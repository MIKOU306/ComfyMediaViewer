/* 3.0.0 transport adapter. The classic DOM, CSS and rendering remain authoritative. */
"use strict";
const CMV3 = (() => {
  const identities = new Map();
  const queues = new Map();
  const favoriteIntents = new Map();
  let favoriteSequence = Date.now() * 1000;
  let info = null;
  let ready = null;
  const key = path => normPath(path);

  async function raw(url, options = {}) {
    const response = await fetch(url, {cache: "no-store", ...options});
    const data = await response.json().catch(() => null);
    if (!response.ok || data?.error || data === null) {
      const error = new Error(data?.error || `HTTP ${response.status}`);
      error.status = response.status;
      error.code = data?.code || null;
      throw error;
    }
    return data;
  }

  function initialize() {
    if (!ready) ready = raw("/api/info").then(async data => {
      // CMV3-LOCAL-FIX(P1) — 放宽 storageMode 硬校验，详见 doc/上游缺陷适配清单.md
      // 上游原代码：
      //     if (data.version !== "3.0.0" || data.storageMode !== "sqlite") {
      //       throw new Error("经典界面需要 3.0.0 数据库后端，请使用随包的启动入口");
      //     }
      // 问题：`api()` 的每个调用都要先 await initialize()，而 initialize() 硬要求
      //   sqlite 模式 —— 于是**未迁移的数据目录**（storageMode="legacy"）下，所有
      //   api() 一律 reject。实测后果：app.js load() 失败 ⇒ S.items 恒空 ⇒
      //   页面永远「未就绪」，56 个依赖页面数据的既有门禁集体假红。
      // 而 3.0.0 后端本身是「SQLite 优先、legacy 兜底」的双模设计：
      //   legacy 模式下 /api/info · /api/list · /api/prefs 等**全部照常可用**
      //   （已验证：legacy 模式 select/prefs/info 链路全通）。
      // 故把「版本不匹配」与「模式非 sqlite」降级为**告警**：
      //   · version 不是 3.0.0 ⇒ 仍抛错（前后端版本必须一致，这是硬约束）
      //   · storageMode 非 sqlite ⇒ 仅在 console 提示，不阻断
      // 迁移到 sqlite 后本分支自然不再触发，3.0.0 能力不受影响。
      if (data.version !== "3.0.0") {
        throw new Error("经典界面需要 3.0.0 后端，请使用随包的启动入口");
      }
      if (data.storageMode !== "sqlite") {
        try {
          console.warn("[cmv] 当前为 " + data.storageMode + " 存储模式（未迁移到 3.0.0 数据库）。"
            + "当前使用旧版浏览；请在资料管理中迁移后启用版本校验与元数据归档。");
        } catch (_) {}
      }
      // CMV3-LOCAL-FIX(C4) — 撤销上游对「删除去向=Windows 回收站(system)」的**收口**。
      // 上游原代码（已删除）：
      //     if (data.delete_mode === "system") {
      //       await raw("/api/prefs", {method: "POST", ..., body: JSON.stringify({delete_mode: "app"})});
      //       data.delete_mode = "app";
      //       if (typeof toast === "function") toast("新版资料库使用应用内回收站，已调整删除去向");
      //     }
      // 为什么撤销（主人 2026-10-10 拍板 · 详见 doc/v3门禁语义冲突台账.md · C4）：
      //   · 本工程后端**完整保留** system 能力，且已被权威门禁实测全绿 ——
      //     `delmode_check` 42/42：/api/prefs 写 system 返 200、/api/delete 真进
      //     Windows 回收站（$I 取证）、卷不可用时回退应用内回收站且回报 fallback。
      //   · 经典界面（本适配层的服务对象）**本来就带**这个下拉，index.html 里
      //     `<option value="system">删到：Windows 回收站</option>` 仍在。
      //     保留收口 = 用户点得动、却报错回滚的**死控件**。
      //   · 档 4 替换路线的目标是「3.0.0 新骨架 + 本地全部能力」，不是砍功能。
      // 恢复后 persist_ui_check 的 _V3 分流同步撤销（回到 2.x 期望）。
      info = data;
      return data;
    }).catch(error => { ready = null; throw error; });
    return ready;
  }

  function remember(item) {
    if (!item?.path) return item;
    const token = item.expectedVersion || item.revisionToken
      || (item.revisionId ? `rev:${item.revisionId}` : null);
    if (item.mediaId && token) {
      item.expectedVersion = token;
      identities.delete(key(item.path));
      identities.set(key(item.path), Object.freeze({
        path: item.path, mediaId: item.mediaId, expectedVersion: token,
      }));
      while(identities.size>4096) identities.delete(identities.keys().next().value);
    }
    return item;
  }

  function target(itemOrPath) {
    const item = typeof itemOrPath === "string" ? itemByPath(itemOrPath) : itemOrPath;
    const path = typeof itemOrPath === "string" ? itemOrPath : itemOrPath?.path;
    // An explicit UI object is the selected snapshot, never replace it with a newer path alias.
    const selected = item?.mediaId && (item.expectedVersion || item.revisionId)
      ? {path, mediaId: item.mediaId, expectedVersion: item.expectedVersion || `rev:${item.revisionId}`}
      : identities.get(key(path));
    if (info?.storageMode === "legacy" && path) return {path};
    if (!selected?.mediaId || !selected.expectedVersion) {
      throw new Error("操作目标缺少媒体版本，请刷新列表后重新选择");
    }
    return {...selected, path};
  }

  function urlTarget(item) {
    if (!item?.mediaId) return "";
    const version = item.expectedVersion || (item.revisionId ? `rev:${item.revisionId}` : "");
    return "&mediaId=" + encodeURIComponent(item.mediaId)
      + (version ? "&expectedVersion=" + encodeURIComponent(version) : "");
  }

  class Selection extends Set {
    constructor() { super(); this.snapshots = new Map(); }
    addItem(item) {
      if(!this.has(item.path)) this.snapshots.set(key(item.path),Object.freeze(target(item)));
      return super.add(item.path);
    }
    add(path) {
      if (!this.has(path)) this.snapshots.set(key(path), Object.freeze(target(path)));
      return super.add(path);
    }
    delete(path) { this.snapshots.delete(key(path)); return super.delete(path); }
    clear() { this.snapshots.clear(); super.clear(); }
    targets(paths) {
      return paths.map(path => {
        const saved = this.snapshots.get(key(path));
        if (!saved) throw new Error("所选媒体版本已失效，请重新选择");
        return {...saved, path};
      });
    }
  }

  async function ingest(data) {
    const items = [...(data.items || []), ...(data.results || []),
      ...(data.groups || []).flatMap(group => group.items || [])];
    if (info?.storageMode === "legacy") return data;
    let next = 0;
    await Promise.all(Array.from({length: Math.min(4, items.length)}, async () => {
      while (next < items.length) {
        const item = items[next++];
        if (!item?.path) continue;
        if (!item.mediaId || (!item.revisionId && !item.expectedVersion)) {
          const image = item.type !== "video";
          const version = await raw((image ? "/api/content_version?p=" : "/api/meta?p=") + encodeURIComponent(item.path));
          Object.assign(item, {mediaId: version.mediaId, revisionId: version.revisionId,
            expectedVersion: version.revisionToken || `rev:${version.revisionId}`, contentVersion: version.contentVersion});
        }
        remember(item);
      }
    }));
    return data;
  }

  function enqueue(identity, fn) {
    const prior = queues.get(identity) || Promise.resolve();
    const promise = prior.catch(() => {}).then(fn);
    queues.set(identity, promise);
    return promise.finally(() => { if (queues.get(identity) === promise) queues.delete(identity); });
  }

  async function request(url, options = {}) {
    await initialize();
    const parsed = new URL(url, location.href);
    const route = parsed.pathname;
    const method = options.method || "GET";
    // Capture selection before the first asynchronous boundary.
    let body = method === "POST" && typeof options.body === "string" ? JSON.parse(options.body) : null;
    if (body) {
      body = {...body};
      if (["/api/delete", "/api/batch_export"].includes(route)) {
        if (Array.isArray(body.paths)) body.targets ||= body.paths.map(target);
        else if (body.path) Object.assign(body, target(body));
      } else if (["/api/fav", "/api/mark", "/api/nsfw/label", "/api/save_download", "/api/recent"].includes(route) && body.path
          && !(route === "/api/save_download" && S.solo?.items?.some(item => key(item.path) === key(body.path)))) {
        Object.assign(body, body.mediaId ? body : target(body.path));
      } else if (route === "/api/tags") {
        if (body.path) Object.assign(body, target(body.path));
        if (Array.isArray(body.paths)) body.targets ||= body.paths.every(path => batchSel.has(path))
          ? batchSel.targets(body.paths) : body.paths.map(target);
      }
      if (route === "/api/fav" && body.path && !body.op) {
        const item = itemByPath(body.path);
        const previous = favoriteIntents.get(body.mediaId || key(body.path));
        const desired = typeof body.fav === "boolean" ? body.fav : !(previous?.desired ?? item?.fav ?? false);
        const sequence = ++favoriteSequence;
        body.fav = desired;
        body.seq = sequence;
        favoriteIntents.set(body.mediaId || key(body.path), {desired, sequence});
      }
      // CMV3-LOCAL-FIX(C4)：上游原代码在这里**直接拒绝** system ——
      //     if (route === "/api/prefs" && body.delete_mode === "system") {
      //       throw new Error("新版资料库目前使用应用内回收站，可在原回收站入口还原");
      //     }
      // 已撤销（与 initialize() 里的收口同一决策，理由见上）。
      // ⇒ 经典界面的「删到：Windows 回收站」重新可用，`/api/prefs` 请求原样透传。
      if (route === "/api/datadir") {
        throw new Error("新版资料库需要关闭程序后搬家；当前数据保存在程序目录的 cmv-data 中");
      }
      options = {...options, body: JSON.stringify(body)};
    }
    const solo = S.solo?.items?.some(item => key(item.path) === key(parsed.searchParams.get("p")));
    if (route === "/api/meta" && !solo && !parsed.searchParams.has("mediaId")) {
      const path = parsed.searchParams.get("p");
      const selected = target(path);
      if (selected.mediaId) {
        parsed.searchParams.set("mediaId", selected.mediaId);
        parsed.searchParams.set("expectedVersion", selected.expectedVersion);
      }
      url = parsed.pathname + parsed.search;
    }
    await initialize();
    const send = () => raw(url, options);
    let data;
    try {
      data = body?.path && ["/api/fav", "/api/mark", "/api/tags", "/api/nsfw/label"].includes(route)
        ? await enqueue(body.mediaId || key(body.path), send) : await send();
    } catch (error) {
      if (route === "/api/fav" && favoriteIntents.get(body?.mediaId || key(body?.path))?.sequence === body?.seq) favoriteIntents.delete(body.mediaId || key(body.path));
      throw error;
    }
    if (route === "/api/fav" && favoriteIntents.get(body?.mediaId || key(body?.path))?.sequence === body?.seq) favoriteIntents.delete(body.mediaId || key(body.path));
    if (route === "/api/info") {
      info = data;
      const selector = document.querySelector("#delModeSel");
      if (selector) {
        const unsupported = selector.querySelector('option[value="system"]');
        if (unsupported) { unsupported.disabled = false; unsupported.textContent = "删到：Windows 回收站"; }
      }
    }
    if (route === "/api/prefs" && info && data.trash_keep_days != null) info.trash_keep_days = data.trash_keep_days;
    if (route === "/api/root" && method === "POST" && typeof CMVPlayback!=="undefined") CMVPlayback.releaseAll();
    if (data.items || data.results || data.groups) await ingest(data);
    if (route === "/api/solo" && data.item) {
      await ingest({items: [data.item]});
    }
    return data;
  }

  // Frozen-thumbnail preparation may return temporary 503s under load. Keep the same
  // identity/version while retrying; never silently reload a replacement file.
  document.addEventListener("error", event => {
    const img = event.target;
    if (!(img instanceof HTMLImageElement) || !img.src.includes("/api/thumb?")) return;
    if(img.dataset.cmvManaged === "1") return;
    const count = Number(img.dataset.cmvRetry || 0);
    if (count >= 3) return;
    event.stopImmediatePropagation();
    img.dataset.cmvRetry = String(count + 1);
    const source = img.currentSrc || img.src;
    const locator = img.src;
    setTimeout(() => {
      if (img.isConnected && img.src === locator) {
        img.removeAttribute("srcset");
        img.src = source;
      }
    }, [300, 1000, 2500][count] + Math.floor(Math.random() * 250));
  }, true);

  const trashRetention = () => info?.trash_keep_days === 0 ? "不自动清空" : `保留 ${info?.trash_keep_days ?? 30} 天`;
  return {request, remember, target, urlTarget, Selection, initialize, ingest, trashRetention, get info() { return info; }};
})();
