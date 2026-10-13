/* Step 1: cursor pages for plain directories; preserve the classic full-list views. */
"use strict";
const CMVQuery = (() => {
  let session = null, busy = false, serial = 0, controller = null, legacyKey = null;
  let paging = false, deferredFilter = false;
  let pagingView = 0, pagingPresentation = "";
  const scans = new Map(), scanned = new Map();
  let scanQueue = Promise.resolve();
  let scanView = null;
  let workflowFamily = null;
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const value = selector => ($(selector) || {}).value || "";
  const key = path => [normPath(path || S.path), !!$("#rec").checked, value("#sort"), (FLOW ? BATCH : PER),FLOW,workflowFamily||""].join("|");
  const eligible = () => VIEW_MODE === "grid" && !S.expandDirs
    && !favMode && !libMode && S.filters.type === "all"
    && !value("#q").trim() && (!value("#dateRange") || value("#dateRange") === "all")
    && !FMTS.size && !WFK && !TAG_FILTER.length && !TAG_EXCLUDE.length && !tagFilterActive
    && !value("#fModel") && !value("#fSampler") && !value("#fDim")
    && NSFW_MODE !== "hide";
  const post = (url, body, signal) => api(url, {method:"POST",
    headers:{"Content-Type":"application/json"}, body:JSON.stringify(body), signal});

  function loading(on, text) {
    busy = on;
    grid.setAttribute("aria-busy", String(on));
    if (on && text) setBotState("busy", text, null, true);
    if (on) $("#pager")?.querySelectorAll("button").forEach(button => { button.disabled = true; });
  }
  function cancelPending() {
    serial++;
    controller?.abort(); controller = null;
    paging = false; deferredFilter = false;
    scanView = null; CMVLoad.cancel();
    loading(false);
    renderPagerOverride();
  }
  function leave() {
    cancelPending();
    session = null; S.queryPaged = false; legacyKey = null;
    grid.dataset.cmvSource = "classic";
    $("#pgJump").disabled = false;
  }
  function scope(context, requested) {
    const root = context.roots?.find(root => root.rootId === context.activeRootId);
    if (!root || !context.libraryId) return null;
    const normalize = text => String(text).replace(/\\/g, "/").replace(/\/+$/, "");
    const target = normalize(requested || root.path), base = normalize(root.path);
    const fold = text => root.caseSensitive === 0 ? text.toLowerCase() : text;
    if (fold(target) !== fold(base) && !fold(target).startsWith(fold(base) + "/")) return null;
    const directory = fold(target) === fold(base) ? null : target.slice(base.length + 1);
    if (directory?.split("/").some(part => !part || part === "." || part === "..")) return null;
    return {root, path: requested || root.path, directory,
      libraryId:context.libraryId, rootIds:[root.rootId], recursive:!!$("#rec").checked};
  }

  function scan(rootId, force, tok, fingerprint, coverage) {
    if (scans.has(rootId)) return scans.get(rootId);
    const previous=scanned.get(rootId);
    // Revisit a completed, unchanged root without rewriting its entire catalog.
    // Explicit refresh, directory changes, expired cache or changed ownership rescan.
    if (!force && previous && coverage?.state === "complete" && previous.epoch === coverage.epoch
      && previous.sig === fingerprint.sig && previous.scope === fingerprint.scope
      && Date.now()-previous.at < 300000) return Promise.resolve();
    const task = scanQueue.catch(() => {}).then(async () => {
      let job;
      const deadline = Date.now() + 180000;
      while (!job) {
        try { job = await post("/api/v2/catalog/scan/start", {rootId}); }
        catch (error) {
          if (error.code !== "scan_busy" || Date.now() >= deadline) throw error;
          if (scanView?.rootId === rootId && scanView.token === viewGen) {
            loading(true, "等待目录扫描…");
            CMVLoad.phase(scanView.token, "等待后台目录扫描…", "waiting");
          }
          await wait(750);
        }
      }
      const started=Date.now();
      while (job.state === "running") {
        if (scanView?.rootId === rootId && scanView.token === viewGen) {
          loading(true, "正在扫描目录 · 已发现 " + (job.observed || 0) + " 项");
          CMVLoad.phase(scanView.token, "正在扫描目录…", "scanning", job, scanView.total);
        }
        if (Date.now() >= deadline) throw new Error("目录扫描仍在进行，请稍后刷新");
        await wait(Date.now() - started < 1000 ? 50 : 250);
        job = await api("/api/v2/catalog/scan/status?jobId=" + encodeURIComponent(job.jobId));
      }
      if (job.state !== "complete") throw new Error(job.error || "目录扫描未完整完成，请使用连续浏览或刷新重试");
      scanned.set(rootId, {...fingerprint,epoch:job.epoch,at:Date.now()});
      if(fingerprint.libraryId) {try {
        sessionStorage.setItem("cmvScan:"+fingerprint.libraryId+":"+rootId,JSON.stringify(scanned.get(rootId)));
      } catch(_) {}}
    });
    scanQueue = task;
    scans.set(rootId, task);
    task.finally(() => { if (scans.get(rootId) === task) scans.delete(rootId); }).catch(() => {});
    return task;
  }
  async function directoryPage(spec,cursor,signal) {
    const result=await api("/api/v2/query/directories?rootId="+encodeURIComponent(spec.rootIds[0])
      +(spec.directory?"&directory="+encodeURIComponent(spec.directory):"")+"&limit=200"
      +(cursor?"&cursor="+encodeURIComponent(cursor):""),{signal});
    if(result.directoryAvailability!=="online") throw new Error("当前目录不可访问，请检查目录后刷新");
    return {...result,dirs:result.dirs.filter(item=>item.availability==="online").map(item=>({...item,n:null}))};
  }
  const directories=(spec,signal)=>directoryPage(spec,null,signal);
  function validate(page, spec) {
    if (!Array.isArray(page.items) || typeof page.queryId !== "string"
      || typeof page.indexGeneration !== "string" || typeof page.partial !== "boolean"
      || (page.nextCursor !== null && typeof page.nextCursor !== "string")
      || page.items.length > spec.limit || !Number.isSafeInteger(page.indexedCount)) {
      throw new Error("分页响应不完整，请刷新重试");
    }
    const ids = new Set();
    const base = normPath(spec.root.path) + "\\";
    for (const item of page.items) {
      if (!item.mediaId || !item.revisionId || !item.path || ids.has(item.mediaId)
        || !normPath(item.path).startsWith(base) || !["image","video"].includes(item.type)) {
        throw new Error("分页中的媒体身份或目录范围无效");
      }
      ids.add(item.mediaId);
      const ext = (item.path.match(/\.[^.\\/]+$/) || [""])[0].toLowerCase();
      item.png = ext === ".png";
      item.playable = item.type === "image" || [".mp4",".webm",".m4v",".mov"].includes(ext);
      CMV3.remember(item);
    }
    return page;
  }
  const body = (spec, cursor) => ({libraryId:spec.libraryId,rootIds:spec.rootIds,
    directory:spec.directory, recursive:spec.recursive,
    filters:spec.workflowFamily ? {workflowFamilyUuids:[spec.workflowFamily]} : {},
    sort:spec.sort,limit:spec.limit,cursor});

  async function readDirectory(path, opts, tok) {
    const operation = ++serial;
    paging = false; deferredFilter = false;
    controller?.abort(); controller = new AbortController();
    const signal = controller.signal;
    let preserveLegacy = !!opts.metadataOnly || !!opts.preserveLegacy;
    loading(true, "加载中…");
    CMVLoad.begin(tok, path);
    scanView = null;
    const stillCurrent = () => {
      if (operation !== serial || tok !== viewGen) throw new DOMException("已切换视图", "AbortError");
    };
    try {
      if (!opts.legacy && eligible()) {
        await CMV3.initialize(); stillCurrent();
        if (CMV3.info.storageMode !== "sqlite") {
          const result = await api("/api/list?path=" + encodeURIComponent(path || "") + "&rec=" + ($("#rec").checked ? "1" : "0"), {signal});
          stillCurrent(); result.preserveLegacy = true; return result;
        }
        const context = await api("/api/v2/query/context", {signal}); stillCurrent();
        const target = scope(context, path);
        if (target) {
          // Root ownership is durable. Overlapping registered roots need a
          // physical-directory query contract; the classic list already has it.
          const relatedRoots=context.roots.filter(root=>{
            const normalize=text=>String(text).replace(/\//g,"\\").replace(/\\+$/,"");
            const a=normalize(root.path),b=normalize(target.root.path);
            const fold=text=>root.caseSensitive===1 || target.root.caseSensitive===1 ? text : text.toLowerCase();
            const left=fold(a),right=fold(b);
            return left===right || left.startsWith(right+"\\") || right.startsWith(left+"\\");
          });
          if(relatedRoots.length>1) {
            if(workflowFamily) throw new Error("当前目录范围暂不能使用家族筛选，请先清除该筛选");
            const result=await api("/api/list?path="+encodeURIComponent(path||"")+"&rec="+($("#rec").checked?"1":"0"),{signal});
            stillCurrent();result.preserveLegacy=true;return result;
          }
          const force = !!opts.forceScan;
          const rootSignature=await api("/api/dirinfo?path="+encodeURIComponent(target.root.path)+"&rec=1",{signal});
          stillCurrent();
          if(typeof rootSignature.sig !== "string" || !rootSignature.sig) throw new Error("目录指纹不可用，请刷新重试");
          const base=normPath(target.root.path);
          const related=context.roots.filter(root=>{
            const path=normPath(root.path);
            return path===base || path.startsWith(base+"\\") || base.startsWith(path+"\\");
          }).map(root=>[root.rootId,normPath(root.path)]);
          const fingerprint={sig:rootSignature.sig,scope:JSON.stringify(related),libraryId:context.libraryId};
          const coverage=context.scanCoverage?.roots?.find(root=>root.rootId===target.root.rootId);
          const estimated = Number(rootSignature.sig.split(':')[0]);
          scanView = {rootId:target.root.rootId, token:tok,
            total:Number.isSafeInteger(estimated) && estimated > 0 ? estimated : null};
          CMVLoad.phase(tok, "等待目录扫描…", "waiting");
          if (coverage?.state !== "complete" || force) {
            await scan(target.root.rootId, force, tok, fingerprint, coverage); stillCurrent();
          } else {
            // Persist the observed fingerprint across page reloads, bound to library/root/epoch.
            const storageKey="cmvScan:"+context.libraryId+":"+target.root.rootId;
            let previous=null;
            try { previous=JSON.parse(sessionStorage.getItem(storageKey)||"null"); } catch (_) {}
            if (coverage.fingerprint!==fingerprint.sig && (!previous || previous.sig!==fingerprint.sig || previous.scope!==fingerprint.scope || previous.epoch!==coverage.epoch)) {
              setTimeout(()=>{
                if(tok!==viewGen) return;
                scan(target.root.rootId,false,tok,fingerprint,coverage).then(()=>{
                  const observed=scanned.get(target.root.rootId);
                  if(observed) sessionStorage.setItem(storageKey,JSON.stringify(observed));
                  if(tok===viewGen) {
                    if(!batchOn && !lb.classList.contains("on")) void load(S.path,{silent:true,queryOnly:true});
                    else toast("后台目录检查已完成；新内容可通过刷新查看");
                  }
                }).catch(error=>{if(tok===viewGen) toast(error.message,"err");});
              },1000);
            }
          }
          CMVLoad.phase(tok, "正在读取首屏图片…", "query");
          const spec = {...target, sort:value("#sort") || "mtime_desc", limit:FLOW ? BATCH : PER,workflowFamily};
          const [page, folders] = await Promise.all([
            post("/api/v2/media/query", body(spec,null), signal),
            directories(spec, signal),
          ]);
          stillCurrent();
          validate(page, spec);
          return {root:target.root.path, path:folders.path, dirs:folders.dirs, items:page.items,
            has_thumb:!!CMV3.info.has_pil,
            querySession:{spec, page, number:1, cursors:[null], folders,
              key:[normPath(folders.path),spec.recursive,spec.sort,spec.limit,FLOW,spec.workflowFamily||''].join('|'), items:page.items.slice()}};
        }
        preserveLegacy = true; // A missing explicit root identity must not cause an upgrade loop.
      }
      CMVLoad.phase(tok, "正在读取目录图片…", "classic");
      const result = await api("/api/list?path=" + encodeURIComponent(path || "")
        + "&rec=" + ($("#rec").checked ? "1" : "0"), {signal});
      stillCurrent();
      result.preserveLegacy = preserveLegacy;
      return result;
    } finally {
      CMVLoad.finish(tok);
      if (scanView?.token === tok) scanView = null;
      if (operation === serial) {
        loading(false);
        // qmode invalidates viewGen without starting another load.
        if (tok !== viewGen && deferredFilter) {
          deferredFilter = false;
          queueMicrotask(() => void load(path,{...opts,legacy:!eligible()}));
        }
      }
    }
  }
  function install(result) {
    session = result.querySession || null;
    S.queryPaged = !!session;
    legacyKey = !session && result.preserveLegacy && eligible() ? key(result.path) : null;
    grid.dataset.cmvSource = session ? "server" : "classic";
    PAGE = 1;
    $("#pgJump").disabled = !!session;
    if (!session) setBotState("idle","经典浏览",null,true);
    else if(session.folders?.nextCursor) {
      const selected=session,tok=viewGen;
      void (async()=>{
        let cursor=selected.folders.nextCursor;
        while(cursor && selected===session && tok===viewGen && !S.expandDirs) {
          const page=await directoryPage(selected.spec,cursor);
          if(selected!==session || tok!==viewGen) return;
          if(page.directoryGeneration!==selected.folders.directoryGeneration) throw new Error("子目录已变化，请刷新");
          if(page.nextCursor===cursor) throw new Error("子目录分页未前进");
          if(S.dirs.length+page.dirs.length>10000) throw new Error("子目录较多，请打开文件夹分组后浏览");
          S.dirs.push(...page.dirs);cursor=page.nextCursor;
          if(S.view[0]?.type==="dirgroup") {
            S.view[0].n=S.dirs.length;
            const el=grid.querySelector(".card.dir .dc");if(el)el.textContent=S.dirs.length+" 个文件夹";
          }
        }
      })().catch(error=>{if(selected===session && tok===viewGen) toast(error.message,"err");});
    }
  }

  function render() {
    const page = session.page;
    PAGE = session.number;
    S.items = FLOW ? session.items : page.items;
    const folders = (FLOW || PAGE === 1) && S.dirs.length ? [{type:"dirgroup",n:S.dirs.length}] : [];
    S.view = folders.concat(S.items);
    S.shown = 0; vObs.disconnect(); grid.innerHTML = ""; renderMore();
    $("#count").textContent = FLOW ? "已加载 "+S.items.length+" / "+page.indexedCount+" 项" : "已索引 " + page.indexedCount + " 项 · 第 " + PAGE + " 页";
    $("#count").dataset.tip = "当前查询的已索引媒体数量；未完成索引的内容不计入。翻页按新版后端返回的顺序。";
    announceCount($("#count").textContent);
    renderChips(); renderTlUI(false); buildLibNav(); syncFavNavSel();
    buildFmtNav(); buildWfNav();
    setBotState("idle", FLOW ? "连续浏览 · 已加载 "+S.items.length+" 项" : "新版分页 · 本页 " + page.items.length + " 项", null, true);
    renderPager(page.indexedCount, 1);
    /* CMV3-LOCAL-FIX(FE1, 2026-10-12) —— 删除后接替的兑现点（三方文件，仅此 1 行）。
       本文件是三方 Classic-3.0.0 骨架，render() 是分页模式下 S.view 的唯一重建出口：
       那条路上 filters.js 的 applyFilter() 会被 interceptFilter() 接管、不自己重建视图，
       所以「删除后打开邻居」必须等这里重建完再兑现（否则按路径查不到 ⇒ 白关一次大图）。
       下次合版三方时需重新贴回本行。 */
    if(window.mvConsumePendingFocus) window.mvConsumePendingFocus();
  }
  function interceptFilter() {
    if(workflowFamily && !eligible()) {
      workflowFamily=null;
      window.CMVClassic?.familyCleared();
    }
    // Special views install their own complete source; never replace them with a directory.
    if (favMode || libMode) return false;
    if (busy) {
      if (!paging) { deferredFilter = true; return true; }
      if (eligible() && session?.key === key(S.path) && viewGen === pagingView
        && pagingPresentation === VIEW_MODE + "|" + NSFW_MODE) return true;
      // User controls take priority over an unfinished next/previous page.
      serial++; controller?.abort(); paging = false; loading(false);
    }
    if (S.queryPaged) {
      if (!eligible()) { void load(S.path, {silent:true,legacy:true,preserveExpanded:!!S.expandDirs}); return true; }
      if (!session || session.key !== key(S.path)) {
        void load(S.path, {silent:true,queryOnly:true}); return true;
      }
      render(); return true;
    }
    if (eligible() && S.path && legacyKey !== key(S.path)) {
      void load(S.path, {silent:true,queryOnly:true}); return true;
    }
    if (!eligible()) legacyKey = null;
    $("#count").removeAttribute("data-tip");
    $("#pgJump").disabled = false;
    $("#pgJump").removeAttribute("title");
    return false;
  }
  function renderPagerOverride() {
    if (!S.queryPaged || !session || FLOW) return false;
    const box = $("#pager"), current = session.number;
    const button = (number, label, disabled, classes) => {
      const el = document.createElement("button");
      el.dataset.p = number; el.className = classes; el.innerHTML = label;
      el.disabled = disabled || busy;
      el.onclick = () => void goPage(number);
      return el;
    };
    box.innerHTML = "";
    box.append(button(current-1,ico("chevronLeft"),current===1,"pgnav"));
    const numbers = document.createElement("span"); numbers.className = "pgnums";
    // Display only pages whose cursor boundaries have actually been reached.
    const first = Math.max(1,current-2), last = Math.min(session.cursors.length,current+2);
    for(let number=first;number<=last;number++) numbers.append(button(number,String(number),false,number===current?"on":""));
    box.append(numbers,button(current+1,ico("chevronRight"),!session.page.nextCursor,"pgnav"));
    $("#pgTotal").textContent = "?";
    $("#pgJump").value = current; $("#pgJump").disabled = true;
    $("#pgJump").title = "新版分页按顺序前后翻页，当前步骤不支持任意跳页";
    if (String(PER) !== $("#perPage").value) $("#perPage").value = String(PER);
    return true;
  }
  async function goPage(number) {
    if (busy || !session || !S.queryPaged) return;
    if (!eligible() || session.key !== key(S.path)) { applyFilter(); return; }
    if (number < 1 || number === session.number || number > session.cursors.length + 1) return;
    let cursor;
    if (number === session.number + 1) cursor = session.page.nextCursor;
    else if (number <= session.cursors.length) cursor = session.cursors[number-1];
    if (number !== 1 && !cursor) return;
    const selected = session, tok = viewGen, operation = ++serial;
    paging = true;
    pagingView = tok; pagingPresentation = VIEW_MODE + "|" + NSFW_MODE;
    controller?.abort(); controller = new AbortController();
    loading(true,"正在翻页…");
    try {
      const result = await post("/api/v2/media/query",body(selected.spec,cursor),controller.signal);
      if (operation !== serial || tok !== viewGen || session !== selected) return;
      // applyFilter events can arrive during transport; a late page cannot consume them.
      if (!eligible() || selected.key !== key(S.path)) {
        loading(false); applyFilter(); return;
      }
      const page = validate(result,selected.spec);
      if (page.queryId !== selected.page.queryId || page.indexGeneration !== selected.page.indexGeneration) {
        const error = new Error("图库已变化，请刷新"); error.code = "cursor_expired"; throw error;
      }
      if (number > selected.cursors.length) selected.cursors.push(cursor);
      selected.page = page; selected.number = number;
      loading(false); render();
      if (batchOn) syncBatchUI();
      $("#gridwrap").scrollTop = 0;
    } catch(error) {
      if(operation !== serial || tok !== viewGen || session !== selected) return;
      loading(false);
      if (!eligible() || selected.key !== key(S.path)) { applyFilter(); return; }
      if(error.code === "cursor_expired") {
        toast("图库已变化，已返回第一页，请继续翻页");
        await load(S.path,{silent:true,queryOnly:true});
      } else if(error.name !== "AbortError") {
        toast(error.message,"err"); setBotState("err","翻页失败，请重试",null,true);
        renderPagerOverride();
      }
    } finally { if(operation === serial) { paging = false; loading(false); } }
  }
  async function loadMore({strict=false}={}) {
    if (!FLOW || busy || !session || !session.page.nextCursor || !eligible()) return;
    const selected=session, tok=viewGen, operation=++serial;
    const cursor=selected.page.nextCursor;
    paging=true; controller?.abort(); controller=new AbortController(); loading(true,"正在加载更多…");
    try {
      const page=validate(await post("/api/v2/media/query",body(selected.spec,cursor),controller.signal),selected.spec);
      if(operation!==serial || tok!==viewGen || session!==selected) return;
      if(page.queryId!==selected.page.queryId || page.indexGeneration!==selected.page.indexGeneration) {
        const error=new Error("图库已变化，请刷新后继续浏览"); error.code="cursor_expired"; throw error;
      }
      const ids=new Set(selected.items.map(item=>item.mediaId));
      if(page.items.some(item=>ids.has(item.mediaId))) throw new Error("分页出现重复媒体，请刷新");
      selected.items.push(...page.items); selected.page=page; selected.number++;
      S.items=selected.items;
      S.view.push(...page.items);
      loading(false); setBotState("idle","连续浏览 · 已加载 "+S.items.length+" 项",null,true);
      $("#count").textContent="已加载 "+S.items.length+" / "+page.indexedCount+" 项";
      if(batchOn) syncBatchUI();
      renderMore();
      return true;
    } catch(error) {
      if(operation!==serial || tok!==viewGen || session!==selected || error.name==="AbortError") return;
      if(strict) throw error;
      if(error.code==="cursor_expired" && !batchOn && !lb.classList.contains("on")) {
        loading(false); toast("图库已更新，正在重新读取列表");
        await load(S.path,{silent:true,queryOnly:true}); return;
      }
      toast(error.message,"err"); setBotState("err","续载失败，请刷新或重试",null,true);
    } finally { if(operation===serial) {paging=false;loading(false);} }
  }
  async function ensureAll(cancelled=()=>false) {
    const selected=session,tok=viewGen,deadline=Date.now()+180000;
    while(FLOW && session?.page.nextCursor) {
      if(cancelled() || session!==selected || tok!==viewGen) throw new Error("视图已改变，已取消全选");
      if(Date.now()>deadline) throw new Error("读取全部条目超时，请稍后重试");
      if(busy) {await wait(50);continue;}
      if(!await loadMore({strict:true})) throw new Error("未能读取完整列表，请刷新后重试");
    }
  }
  // Preserve model/sampler/dimension controls: obtain their full metadata source on demand.
  let metadataFocus = false;
  document.addEventListener("focusin", event => {
    if (S.queryPaged && ["fModel","fSampler","fDim"].includes(event.target.id)) {
      metadataFocus = true;
      void load(S.path,{silent:true,legacy:true,metadataOnly:true});
    }
  });
  document.addEventListener("focusout", event => {
    if (!metadataFocus || !["fModel","fSampler","fDim"].includes(event.target.id)) return;
    setTimeout(async () => {
      const currentView = viewGen;
      while (busy && currentView === viewGen) await wait(50);
      if (currentView !== viewGen || !metadataFocus) return;
      metadataFocus = false;
      if (eligible()) { legacyKey = null; applyFilter(); }
    },0);
  });
  function setWorkflowFamily(uuid) {
    if(uuid!==null && !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(uuid))
      throw new Error("工作流身份无效");
    leave(); workflowFamily=uuid;
  }
  return {eligible,readDirectory,install,leave,cancelPending,interceptFilter,renderPagerOverride,goPage,loadMore,ensureAll,setWorkflowFamily,
    get active(){return !!session && !!S.queryPaged;},
    get state(){return {active:!!session && !!S.queryPaged,loading:busy,
      page:session?.number || 1,nextCursor:session?.page.nextCursor || null,
      indexedCount:session?.page.indexedCount ?? null,queryId:session?.page.queryId || null};}};
})();
