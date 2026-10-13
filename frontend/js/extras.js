/* ComfyMediaViewer · js/extras.js（2026-09-09 从 index.html 拆出，加载顺序与原文一致） */
"use strict";
/* ---------------- 跨目录收藏夹视图 ---------------- */
let favMode=false;
async function loadFavView(){
  CMVQuery.cancelPending();
  /* E6-4：这是**权威视图加载**（它整片重写 S.items / S.dirs / favMode / 面包屑）
     ⇒ 开头 ++viewGen，既作废在途的目录加载、也作废在途的另一次收藏夹加载。
     不加这一句的实际后果：用户在「切目录」后立刻点收藏夹，慢的 /api/fav_view
     后到 ⇒ 刚切到的目录被收藏夹列表盖掉，而面包屑与路径框仍显示目录态
     （界面自相矛盾，看起来像「点了没反应」或「目录里显示了别的图」）。 */
  const tok=++viewGen;
  const cat=S.filters.favcat||"*";       // H2（2026-10-07）：状态来自 S.filters（原 `#favcat` 已删）
  const j=await api("/api/fav_view?cat="+encodeURIComponent(cat==="*"?"":cat))
    .catch(e=>({error:e.message}));
  if(tok!==viewGen) return;                  // 期间已切到别的视图 ⇒ 本次结果作废
  if(j.error){ toast(j.error,"err"); return; }
  CMVQuery.leave();
  favMode=true; S.metaMap=null; resetMetaFilters();
  if(tagFilterActive || TAG_FILTER.length || TAG_EXCLUDE.length){   // 收藏夹视图与标签筛选不叠加
    TAG_FILTER=[]; TAG_EXCLUDE=[]; tagFilterActive=false; S.tagView=null;
    renderTagNav();
  }
  S.dirs=[];
  S.items=j.items.map(it=>{                    // 卡片下沿显示相对根目录的路径
    const rel=(S.root && it.path.startsWith(S.root))
      ? it.path.slice(S.root.length).replace(/^[\\/]/,"") : it.path;
    it.snipIcon="dir";            /* V4 批次 4：来源前缀交给图标，snippet 只留纯文本 */
    it.snippet=rel;
    return it;
  });
  if(batchOn) setBatch(false);
  applyFilter();
  mkCrumbs($("#crumbs"),                                 // 收藏夹模式：自定义面包屑（V37 统一构造）
    [{label:"收藏夹"+(cat!=="*"?" · "+cat:"")}],
    {back:{label:"返回目录",onclick:()=>load(S.path||"")}});
  toast(`收藏夹：${S.items.length} 项（切换右上分类可过滤）`,"ok");
}
/* 收藏夹入口已并入左栏「收藏」分区（全部收藏 + 分类），此处不再重复绑定 */

/* ---------------- 重复图片检测 ---------------- */
$("#btnDupes").onclick=()=>{
  const dlg=bigDialog("重复图片检测");
  dlg.body.innerHTML=
    '<div style="display:flex;gap:10px;align-items:center;margin-bottom:12px;flex-wrap:wrap">'
    +'<label style="display:flex;gap:4px;align-items:center;font-size:calc(12px*var(--fsx));color:var(--tx2)">'
    +'<input type="checkbox" id="dRec" style="width:auto"'+($("#rec").checked?" checked":"")+'>含子文件夹</label>'
    +'<button class="primary" id="dGo">开始扫描</button>'
    +'<button id="dKeepNew" data-tip="每组只保留修改时间最新的一张">保留最新</button>'
    +'<button id="dKeepBig" data-tip="每组只保留体积最大的一张">保留最大</button>'
    +'<button class="danger-solid" id="dDelAll" style="display:none">删除全部组的未勾选项</button>'
    +'<span id="dStat" style="font-size:calc(12px*var(--fsx));color:var(--tx2)"></span></div>'
    +'<div id="dBody"><div style="color:var(--tx2)">按感知哈希找相似图（缩放/轻微后期不影响判断）。'
    +`每组默认保留最新一张，点缩略图切换保留项，未勾选的可一键删除（进${delShort()}）。</div></div>`;
  const body=dlg.body.querySelector("#dBody"), stat=dlg.body.querySelector("#dStat");
  let groups=[];
  const render=()=>{
    if(!groups.length){ body.innerHTML='<div style="color:var(--tx2)">没有发现相似图片</div>'; return; }
    body.innerHTML="";
    for(const g of groups){
      const gd=document.createElement("div"); gd.className="dupGroup";
      const hd=document.createElement("div");
      hd.style.cssText="display:flex;align-items:center;gap:8px;margin-bottom:8px";
      /* B6-5：把「完全重复」与「只是相似」分开说清楚。
         后端给 exact（组内最大距离 == 0）/ maxdist。
         为什么会存在「相似但不是同一张」：感知哈希阈值内可能挂着不同曝光 / 不同裁剪 /
         加了水印的**不同图**，全删就是真丢图。所以近似组在界面上必须显式区分，并且
         不参与「一键删全部组」。maxdist<0 = 组太大后端不作结论，按「相似」处理。 */
      const near = !g.exact;
      const tag = near
        ? `<span style="font-size:calc(11px*var(--fsx));color:var(--warn-tx);background:var(--warn-bg);`
          +`border:1px solid var(--warn-bd);border-radius:calc(4px*var(--fsx));padding:1px 6px">相似`
          +`（不保证同一张图${g.maxdist>=0?`，差异 ${g.maxdist}/64`:""}）</span>`
        : `<span style="font-size:calc(11px*var(--fsx));color:var(--tx2)">完全重复</span>`;
      hd.innerHTML=`<b style="font-size:calc(12px*var(--fsx))">${g.items.length} 张相似</b>`+tag
        +`<button class="dDel danger-solid" style="margin-left:auto;font-size:calc(12px*var(--fsx));padding:3px 10px">删除未勾选</button>`;
      gd.appendChild(hd);
      const imgs=document.createElement("div"); imgs.className="dupImgs";
      for(const it of g.items){
        const d=document.createElement("div"); d.className="dupItem"+(it.keep?" keep":"");
        d.innerHTML=`<img src="${thumbURL(it)}" `
          +`onerror="this.onerror=null;this.style.visibility='hidden';this.style.background='var(--sunken)'">`
          +`<div class="dn" data-tip="${esc(it.name)}">${esc(it.name)}</div>`
          +`<div class="dc">${it.keep?ico("check")+" 保留":"点击保留"}</div>`;
        d.onclick=()=>{ it.keep=!it.keep; d.classList.toggle("keep",it.keep); };
        imgs.appendChild(d);
      }
      gd.appendChild(imgs);
      /* P1-7：这是**删除入口**，此前是全弹窗里唯一不二次确认的一个 ——
         误点一次就把这一组未勾选的图全删了（同文件的「删除全部组的未勾选项」
         「彻底删除」「全部还原」「清空回收站」都问了）。补上，文案与它们同口径。 */
      hd.querySelector(".dDel").onclick=()=>{
        const targets=g.items.filter(x=>!x.keep).map(it=>CMV3.target(it));
        const paths=targets.map(it=>it.path);
        if(!paths.length){ toast("该组没有未勾选的项","err"); return; }
        confirmDialog("删除未勾选的图片",
          `将删除本组 ${paths.length} 张未勾选的图（移入${delTipWhere()}）。`, async()=>{
          const failed=await delPaths(paths, targets);
          /* P1-19：failed 集合里的路径已 normPath 归一化（见 delPaths），这里必须同样归一化
             再比对 —— 否则带 `..` / 大小写 / 分隔符差异的路径永远匹配不上，
             失败项会被静默移出分组，用户失去重试入口。 */
          for(const it of g.items) if(failed.has(normPath(it.path))) it.keep=true;   // 失败项恢复保留位，不假删
          g.items=g.items.filter(x=>x.keep);
          groups=groups.filter(gr=>gr.items.length>=2);
          render();
        });
      };
      body.appendChild(gd);
    }
  };
  const delPaths=async (paths, targets)=>{
    if(!paths.length) return new Set();
    let ok=0, fb=0; const failed=new Set();
    try{
      const j=await api("/api/delete",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({paths,targets})});        // 单请求批量，失败文件保留不假删
      ok=j.deleted||0;
      fb=j.fallback||0;                        // 系统回收站不可用、退回应用内回收站的个数
      for(const p of (j.failed||[])) failed.add(normPath(p));   // 归一化后再比对
      noteDeleteMeta(j);                     // §6.1：元数据没写回要说出来
    }catch(e){ for(const p of paths) failed.add(normPath(p)); }
    const dead=paths.filter(p=>!failed.has(normPath(p)));
    purgePaths(dead);
    const msg = failed.size ? `已删除 ${ok} 张，失败 ${failed.size} 张`
                            : `已删除 ${ok} 张（${delDoneMsg()}）`;
    if(dead.length){
      // 进了系统回收站的文件没法在应用内还原 → 不给「撤销」按钮，免得点了必然失败
      if(delIsSystem() && !fb) toast(msg, failed.size?"err":"ok");
      else toastAction(msg, "撤销", ()=>undoDelete(dead), failed.size?"err":"ok");
    }
    else if(failed.size) toast(`删除失败 ${failed.size} 张`,"err");
    return failed;
  };
  dlg.body.querySelector("#dGo").onclick=async()=>{
    stat.textContent="扫描中…（大目录可能需要几十秒）";
    try{
      const j=await api("/api/dupes?path="+encodeURIComponent(S.path||S.root)
        +"&rec="+(dlg.body.querySelector("#dRec").checked?"1":"0"));
      /* B6-5：带上 exact / maxdist —— 后端按「组内全对距离 ≤ near」分组，
         exact=true 表示组内最大距离为 0（真·同一张图）。缺字段时按**保守**处理
         （当作「相似」，不给一键全删），宁可多问一次也不误删。 */
      groups=j.groups.map(g=>({items:g.items.map((it,i)=>({...it,keep:i===0})),
                              exact:g.exact===true, maxdist:g.maxdist}));
      stat.textContent=`扫描 ${j.scanned} 张，发现 ${groups.length} 组相似`
        +(j.truncated?"（已达扫描上限，结果可能不全）":"");
      dlg.body.querySelector("#dDelAll").style.display=groups.length?"":"none";
      render();
    }catch(e){ stat.textContent=""; toast("查重失败："+e.message,"err"); }
  };
  /* 一键保留：按每组最新 / 最大重设 keep 标记 */
  const keepBy=(key,label)=>{
    if(!groups.length){ toast("先点「开始扫描」","err"); return; }
    for(const g of groups){
      let best=g.items[0];
      for(const it of g.items) if((it[key]||0)>(best[key]||0)) best=it;
      for(const it of g.items) it.keep=(it===best);
    }
    render();
    toast(`已按「${label}」每组保留一张`,"ok");
  };
  dlg.body.querySelector("#dKeepNew").onclick=()=>keepBy("mtime","保留最新");
  dlg.body.querySelector("#dKeepBig").onclick=()=>keepBy("size","保留最大");
  dlg.body.querySelector("#dDelAll").onclick=()=>{
    /* B6-5：这里只处理**完全重复**组。近似组（组内最大距离 > 0）里可能挂着不同曝光 /
       不同裁剪 / 带水印的**不同图** —— 「一键删掉所有未勾选项」在那种组上等于批量丢图，
       而用户在弹窗里根本来不及逐张看。近似组仍可逐组用组内的「删除未勾选」处理
       （那一步有二次确认，且用户已经看着缩略图做过取舍）。 */
    const use=groups.filter(g=>g.exact), nearN=groups.length-use.length;
    const paths=[], targets=[];
    for(const g of use) for(const it of g.items) if(!it.keep){ paths.push(it.path); targets.push(CMV3.target(it)); }
    if(!paths.length){
      toast(nearN ? `只有 ${nearN} 组「相似」需要逐组确认，没有可一键删除的完全重复项`
                  : "没有未勾选的项","err");
      return;
    }
    const extra=nearN ? `；另有 ${nearN} 组只是「相似」（不保证同一张图），不会一起删，请逐组确认`
                      : "";
    confirmDialog("批量删除重复图",
      `将删除 ${use.length} 组「完全重复」中未勾选的 ${paths.length} 张（移入${delTipWhere()}）`
      + extra + "。", async()=>{
      let ok=0;
      /* 失败集合一律存归一化路径：服务端回传的分隔符/大小写可能与本地 it.path 不一致，
         直接比较会把删掉的图误判成失败、继续留在分组里 */
      const failed=new Set();
      try{
        const j=await api("/api/delete",{method:"POST",headers:{"Content-Type":"application/json"},
          body:JSON.stringify({paths,targets})});       // 单请求批量，失败文件保留
        ok=j.deleted||0;
        for(const p of (j.failed||[])) failed.add(normPath(p));
        noteDeleteMeta(j);                   // §6.1：元数据没写回要说出来
      }catch(e){ for(const p of paths) failed.add(normPath(p)); }
      const dead=paths.filter(p=>!failed.has(normPath(p)));
      purgePaths(dead);
      for(const gr of groups) gr.items=gr.items.filter(x=>x.keep || failed.has(normPath(x.path)));
      groups=groups.filter(gr=>gr.items.length>=2);
      render();
      stat.textContent=`剩余 ${groups.length} 组相似`;
      toast(failed.size ? `已删除 ${ok} 张，失败 ${failed.size} 张` : `已删除 ${ok} 张重复图`,
            failed.size?"err":"ok");
    });
  };
};

/* ---------------- 元数据筛选面板 ---------------- */
function resetMetaFilters(){ $("#fModel").value=""; $("#fSampler").value=""; $("#fDim").value=""; }
async function loadMetaMap(){
  /* E6-4：这是**附属数据加载** —— 只快照 viewGen、**不 ++**。
     它没有资格作废权威视图加载（若这里也 ++，用户切目录后 load() 的结果会被
     一次元数据补算踢掉，界面永远停在旧目录）。反过来，权威视图一旦切换，
     这份映射就属于**旧目录**了 ⇒ await 回来必须丢弃，否则新目录的
     「模型 / 采样器 / 尺寸」下拉里会混进旧目录的值。 */
  const g=viewGen;
  // 基准 = 当前目录列表 ∪ 标签筛选结果（按路径去重）：下拉选项保持完整，
  // 同时保证标签结果里的子目录图也有元数据条目，不会被 metaFilter 误排除
  const seen=new Set(), pool=[];
  for(const it of S.items){ if(!seen.has(it.path)){ seen.add(it.path); pool.push(it); } }
  if(tagFilterActive && S.tagView) for(const it of S.tagView){
    if(!seen.has(it.path)){ seen.add(it.path); pool.push(it); }
  }
  const imgs=pool.filter(i=>i.type==="image");
  const paths=imgs.map(i=>i.path);                 // 不再截断：服务端优先走提示词索引（覆盖全量）
  if(!paths.length) return "empty";
  let j;
  try{
    j=await api("/api/meta_batch",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({paths})});
  }catch(e){ return "err"; }
  if(g!==viewGen) return "stale";                  // 期间权威视图已切换 ⇒ 这份映射属于旧目录
  if(!j || typeof j!=="object" || j.error) return "err";   // 服务端异常：不覆盖已有映射
  S.metaMap=j;
  /* ε-11 §6.4：现场解析超限的截断信号（哨兵键 __meta_truncated，见 server.py meta_batch） */
  if(j.__meta_truncated) toast("有 "+j.__meta_truncated+" 个文件的元数据未解析（超出单次上限），筛选维度可能不全");
  const models={},samplers={},dims={};
  for(const k in S.metaMap){ const m=S.metaMap[k];
    if(typeof m!=="object"||!m) continue;          // ε-11：跳过哨兵键等非条目
    if(m.model) models[m.model]=(models[m.model]||0)+1;
    if(m.sampler) samplers[m.sampler]=(samplers[m.sampler]||0)+1;
    if(m.w&&m.h){ const d=m.w+"x"+m.h; dims[d]=(dims[d]||0)+1; } }
  const fill=(sel,map,label)=>{
    const cur=sel.value;
    sel.innerHTML=`<option value="">${label}（全部）</option>`
      +Object.entries(map).sort((a,b)=>b[1]-a[1])
        .map(([v,n])=>`<option value="${esc(v)}">${esc(v)} (${n})</option>`).join("");
    if([...sel.options].some(o=>o.value===cur)) sel.value=cur;
  };
  fill($("#fModel"),models,"模型"); fill($("#fSampler"),samplers,"采样器"); fill($("#fDim"),dims,"尺寸");
  return "ok";
}
/* 模型 / 采样器 / 分辨率三个下拉常驻左栏「生成参数」区（H4 · 2026-10-06 由「筛选」迁入，
   H1 · 2026-10-07 该区由「工作流类型」更名为「生成参数」并加子标题；三者与该区同属
   「生成参数」维度，此前挂在「筛选」分区时跨了 收藏/标签/格式 三个区）。
   ⚠️ 下面只按 **id** 取元素，所以 H4 的搬迁 / H1 的改序对这里零影响。 */
["#fModel","#fSampler","#fDim"].forEach(s=>$(s).onchange=applyFilter);
/* 从统计面板点某一行 → 直接按模型/采样器/尺寸筛选网格 */
async function filterByMeta(kind, value){
  const sel = kind==="model"?"#fModel":kind==="sampler"?"#fSampler":"#fDim";
  /* E6-4：`loadMetaMap` 现在会在「权威视图已切换」时返回 "stale" —— 那时这份
     元数据属于**上一个目录**，按它设下拉与筛选只会把用户带到错误的视图上。 */
  if(!S.metaMap){ if(await loadMetaMap()==="stale") return; }
  const el=$(sel); if(!el) return;
  if(![...el.options].some(o=>o.value===value)){
    const o=document.createElement("option"); o.value=value; o.textContent=value; el.appendChild(o);
  }
  el.value=value; applyFilter();
  toast(`已按${kind==="model"?"模型":kind==="sampler"?"采样器":"尺寸"}筛选：${value}`,"ok");
}

/* ---------------- 应用内回收站 ---------------- */
$("#btnTrash").onclick=async()=>{
  const dlg=bigDialog("应用内回收站（"+CMV3.trashRetention()+"）");
  dlg.body.innerHTML=
    '<div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap">'
    +'<button class="primary" id="tRestoreAll">全部还原</button>'
    +'<button class="danger-solid" id="tPurgeAll">清空回收站</button>'
    +'<span id="tStat" style="font-size:calc(12px*var(--fsx));color:var(--tx2);align-self:center"></span></div>'
    +'<div id="tList"><div class="skel" style="height:14px;margin-bottom:6px;width:60%"></div><div class="skel" style="height:14px;width:40%"></div></div>';
  const list=dlg.body.querySelector("#tList"), stat=dlg.body.querySelector("#tStat");
  let items=[];
  const refresh=async()=>{
    let j;
    try{ j=await api("/api/trash"); }
    catch(e){ list.innerHTML='<div class="row">读取失败：'+esc(e.message)+'</div>'; return; }
    if(!dlg.mask.isConnected) return;
    items=j.items||[];
    if(window.mvSetTrashBadge) window.mvSetTrashBadge(items.length);   // V2：面板内操作后同步顶栏角标
    stat.textContent=items.length
      ? `${items.length} 项 · ${fmtSize(items.reduce((a,b)=>a+(b.size||0),0))}` : "";
    if(!items.length){
      /* V18（D3）：原来是「一行灰字 + 说明」，换成与网格共用同一套空状态四件套
         （图标 + 主文案 + 副文案），视觉上不再是「异类」。回收站天然没有「行动按钮」
         （空的时候没东西可还原），所以只给前两件 + 可选副文案。
         ⚠️ .empty 带 grid-column:1/-1，在 dialog 的普通块级容器里会被忽略（不是 grid 子项）。 */
      const box=list.parentNode && list.parentNode.classList.contains("empty") ? list.parentNode : null;
      if(box){ box.remove(); }
      const sys=delIsSystem();
      const es=emptyState("回收站是空的",
        sys ? "当前删除去向是 Windows 回收站，新删除的文件不会出现在这里 —— 要还原请到资源管理器的回收站。"
            : "删除的文件在这里"+CMV3.trashRetention()+"，可随时还原或彻底删除。",
        ico("trash"));
      es.classList.add("trash-empty");
      list.innerHTML=""; list.appendChild(es);
      return;
    }
    list.innerHTML="";
    for(const it of items){
      const row=document.createElement("div"); row.className="trRow";
      const thumb=it.type==="image"
        ? `<img src="/api/trash_thumb?key=${encodeURIComponent(it.key)}" onerror="this.style.visibility='hidden'">`
        : `<div style="width:calc(72px*var(--fsx));height:calc(72px*var(--fsx));display:flex;align-items:center;justify-content:center;flex:none" class="ph">${ico(it.type==="video"?"film":"file")}</div>`;
      row.innerHTML=thumb
        +`<div class="ti"><div class="tn" data-tip="${esc(it.name)}">${esc(it.name)}</div>`
        +`<div class="td">原位置：${esc(it.orig)}<br>${fmtTime(it.t)} · ${fmtSize(it.size)}</div></div>`
        +`<button class="tR" style="flex:none">还原</button>`
        +`<button class="tP danger-solid" style="flex:none">彻底删除</button>`;
      row.querySelector(".tR").onclick=async()=>{
        try{
          const r=await api("/api/trash_restore",{method:"POST",
            headers:{"Content-Type":"application/json"},body:JSON.stringify({key:it.key})});
          /* P1-2：文件还原成功、但它的收藏/标签/标记没写回去时不能报「已还原」就完了 ——
             用户会以为数据都回来了，下次才发现收藏没了（后端现在如实回 partial/warning）。 */
          const what=r.renamed
            ? `已还原为「${r.path.split(/[\\/]/).pop()}」（原名被占用）`
            : "已还原到原位置";
          if(r.partial) toastErr(what+"，但 "+(r.failed||[]).join("、")+"未能写回",
                                 r.warning||"这部分需要手工恢复");
          /* §6.1：索引没落盘时条目会残留 —— 不说的话，用户看到「已还原」却发现列表里还在，
             再点一次只会得到「条目不存在」，比不提示更迷惑。 */
          else if(r.idx_stale) toastErr(what, "回收站索引未能写回，列表里可能仍显示该条目");
          else toast(what,"ok");
          refresh(); load(S.path||S.root,{silent:true});
        }catch(e){ toast("还原失败："+e.message,"err"); }
      };
      row.querySelector(".tP").onclick=()=>{
        confirmDialog("彻底删除", `彻底删除「${it.name}」？此操作不可恢复。`, async()=>{
          try{
            const r=await api("/api/trash_purge",{method:"POST",
              headers:{"Content-Type":"application/json"},body:JSON.stringify({keys:[it.key]})});
            /* §6.1：后端现在把「删不掉的文件」如实带回，且条目会**保留在列表里** ——
               不解释的话，用户只会看到「点了彻底删除却还在」。 */
            if(r.failed && r.failed.length)
              toastErr("未能彻底删除", r.error||"文件可能被占用或只读");
            else toast("已彻底删除","ok");
            refresh();
          }catch(e){ toast("删除失败："+e.message,"err"); }
        });
      };
      list.appendChild(row);
    }
  };
  dlg.body.querySelector("#tRestoreAll").onclick=()=>{
    if(!items.length) return;
    confirmDialog("全部还原", `把回收站的 ${items.length} 个文件全部还原到原位置？`, async()=>{
      let ok=0; const partial=[]; const bad=[];   // partial：文件回来了元数据没写回；bad：这条直接失败
      for(const it of items){
        try{
          const r=await api("/api/trash_restore",{method:"POST",
            headers:{"Content-Type":"application/json"},body:JSON.stringify({key:it.key})});
          ok++;
          if(r.partial) partial.push(it.name);
        /* §6.1：原先这里是 `catch(e){}` —— 单条失败只剩一个计数，连「哪几个、为什么」都问不出来。
           批量还原本来最容易出问题（原路径被占、权限、盘满），却恰恰最没有线索。 */
        }catch(e){ bad.push(it.name+(e&&e.message?`（${e.message}）`:"")); }
      }
      if(bad.length)
        toastErr(`已还原 ${ok}/${items.length} 个`,
                 `${bad.length} 个还原失败：`+bad.slice(0,3).join("、")
                 +(bad.length>3?` 等 ${bad.length} 个`:""));
      else if(partial.length)
        toastErr(`已还原 ${ok}/${items.length} 个`,
                 `${partial.length} 个文件还原成功，但它们的收藏 / 标签 / 标记未能写回：`
                 +partial.slice(0,3).join("、")+(partial.length>3?" 等":""));
      else
        toast(`已还原 ${ok}/${items.length} 个`,"ok");
      refresh(); load(S.path||S.root,{silent:true});
    });
  };
  dlg.body.querySelector("#tPurgeAll").onclick=()=>{
    if(!items.length) return;
    confirmDialog("清空回收站", `彻底删除回收站全部 ${items.length} 个文件？此操作不可恢复。`, async()=>{
      try{
        /* P0-2：后端已不再把「keys 为空」当作清空全部 —— 清空必须显式 all:true。
           这样「前端漏传字段」再也不会变成不可逆的整站清空。 */
        const r=await api("/api/trash_purge",{method:"POST",
          headers:{"Content-Type":"application/json"},body:JSON.stringify({all:true,keys:[]})});
        if(r.failed && r.failed.length)
          toastErr(`已删除 ${r.purged||0} 个`,
                   r.error||`${r.failed.length} 个文件未能删除，已保留在列表中`);
        else toast(`已彻底删除 ${r.purged||0} 个文件`,"ok");
        refresh();
      }catch(e){ toast("清空失败："+e.message,"err"); }
    });
  };
  refresh();
};

/* ---------------- 出图统计 ---------------- */
$("#btnStats").onclick=()=>{
  const dlg=bigDialog("出图统计（基于提示词索引）",{wide:true});
  const render=async()=>{
    if(!dlg.mask.isConnected) return;
    dlg.body.innerHTML='<div style="color:var(--tx2)">统计中…（首次访问需建立提示词索引，大目录耗时较长）</div>';
    let j;
    try{ j=await api("/api/stats"); }
    catch(e){ dlg.body.innerHTML='<div class="row">统计失败：'+esc(e.message)+'</div>'; return; }
    if(!dlg.mask.isConnected) return;
    if(!j.count){
      dlg.body.innerHTML='<div style="color:var(--tx2)">索引为空（稍后重试）</div>';
      if(j.building) setTimeout(render, 1500);
      return;
    }
    const cards=[[String(j.count),"已索引图片"],[fmtSize(j.bytes||0),"总体积"],
      [(j.span?j.span.join(" ~ "):"-"),"时间跨度"],
      [String((j.by_day||[]).length)+" 天","有产出的天数"]];
    /* ε-11 §6.4：building=true 时渲染的是「已扫描部分」，必须常驻提示，不能看着像全量 */
    let h=(j.building
      ? '<div style="margin-bottom:calc(10px*var(--fsx));padding:calc(6px*var(--fsx)) calc(10px*var(--fsx));'
        +'border-radius:6px;color:var(--tx2);font-size:calc(12px*var(--fsx));border:1px solid var(--border)">'
        +'<span class="ico-in">'+ico("clock")+'</span>索引仍在构建（'+(j.done|0)+'/'+(j.total|0)+'）：以下为已扫描部分的结果，稍后自动刷新</div>'
      : "");
    h+='<div class="statGrid">'
      +cards.map(c=>`<div class="statCard"><div class="v">${esc(c[0])}</div><div class="l">${c[1]}</div></div>`).join("")
      +'</div>';
    const bars=(title,rows,kind)=>{
      if(!rows||!rows.length) return "";
      const max=rows[0][1]||1;
      const hint=kind?'<span style="font-weight:400;color:var(--tx3);font-size:calc(11px*var(--fsx));margin-left:8px">点一行即可筛选网格</span>':"";
      return `<div style="font-weight:600;margin:14px 0 6px">${title}${hint}</div>`
        +rows.map(([l,n])=>`<div class="brow${kind?" clickable":""}"${kind?` data-fk="${kind}" data-fv="${esc(String(l))}"`:""}>`
          +`<span class="bl" data-tip="${esc(String(l))}">${esc(String(l))}</span>`
          +`<span class="bt"><span class="bar" style="width:${Math.max(2,Math.round(100*n/max))}%"></span></span>`
          +`<span class="bn2">${n}</span></div>`).join("");
    };
    /* V11（2026-09-29）：每日产出从「条形图」升级为「日历热力图」——
       条形图给的是数字，看不出趋势；格子图（一列一周、一行一天）一眼扫过去
       就知道哪几天高产、哪段日子停更。
       数据源 by_day 有两个坑必须处理：
       ① **只含有产出的天** ⇒ 中间停更的日子要补成 0 级格子，
          否则「停更」看起来像「那天根本不存在」，趋势直接失真；
       ② 后端给的是**最近 30 个产出日**而不是 30 个自然日 —— 稀疏出图时长度是没有意义的
          参照（可能横跨好几个月），所以窗口按**周数**算、最多 12 周，
          更老的天整段截掉，绝不把图拉成一条望不到头的长带。 */
    const heat=(rows)=>{
      if(!rows||!rows.length)
        return '<div class="heatEmpty">暂无每日产出数据（图片缺少修改时间时不进时间桶，见 server.py ε-11 §6.4）</div>';
      const rd=(d)=>d.getFullYear()+"-"+String(d.getMonth()+1).padStart(2,"0")+"-"+String(d.getDate()).padStart(2,"0");
      const md=(s)=>{const p=s.split("-");return (+p[1])+"/"+(+p[2]);};
      const cnt=new Map(rows);
      const ds=rows.map(r=>r[0]).sort();
      const end=new Date(ds[ds.length-1]+"T00:00:00");
      const first=new Date(ds[0]+"T00:00:00");
      const weeks=Math.min(12,Math.max(4,Math.ceil((Math.round((end-first)/864e5)+1)/7)));
      const winStart=new Date(end.getTime()-(weeks*7-1)*864e5);   // 窗口起点
      /* 网格必须从「winStart 所在周的周一」起算，否则每周会从任意星期几开始，
         同一列混进不同周的日子 —— 一眼看不出周节律。 */
      const cur=new Date(winStart.getTime()-((winStart.getDay()+6)%7)*864e5);
      const max=Math.max.apply(null,rows.map(r=>r[1]))||1;
      const lv=(n)=>n<=0?0:(max<=4?n:Math.min(4,Math.ceil(n*4/max)));   // 小样本直接按张数分档
      let g="";
      for(const es=end.getTime();cur.getTime()<=es;cur.setTime(cur.getTime()+864e5)){
        const k=rd(cur);
        if(cur<winStart){ g+='<i class="pad"></i>'; continue; }   // 补首行行首空格，不占 tip
        const n=cnt.get(k)||0;
        g+=`<i class="lv${lv(n)}" data-tip="${k} · ${n?n+" 张":"无产出"}"></i>`;
      }
      return '<div class="heat">'
        +`<div class="heatHead">每日产出<span class="hsub">${md(ds[0])} ~ ${md(ds[ds.length-1])}，最近 ${weeks} 周</span></div>`
        +'<div class="heatBody">'
          +'<div class="heatDow" aria-hidden="true"><span>一</span><span>二</span><span>三</span><span>四</span><span>五</span><span>六</span><span>日</span></div>'
          +`<div class="heatGrid" role="img" aria-label="每日出图热力图：${ds[0]} 至 ${ds[ds.length-1]}，共 ${rows.length} 天有产出">${g}</div>`
        +'</div>'
        +'<div class="heatLegend">少<i style="background:var(--bg)"></i><i class="lv1"></i><i class="lv2"></i><i class="lv3"></i><i class="lv4"></i>多</div>'
        +'</div>';
    };
    h+=heat(j.by_day)+bars("模型 Top 10",j.by_model,"model")
      +bars("采样器 Top 10",j.by_sampler,"sampler")+bars("出图尺寸 Top 10",j.by_dim,"dim");
    dlg.body.innerHTML=h;
    dlg.body.querySelectorAll(".brow[data-fk]").forEach(r=>{
      r.onclick=()=>{
        const k=r.dataset.fk, v=r.dataset.fv;
        dlg.mask.remove();                       // 关掉统计弹窗，回到网格
        filterByMeta(k,v);
      };
    });
    if(j.building) setTimeout(render, 1500);
  };
  render();
};

/* ---------------- 目录变化自动刷新 ---------------- */
/* V2.10.7 起本段承担三件事，共用一个「静默重载」内核：
     ① 5 秒轮询；② 切回窗口 / 重新聚焦时立刻补一次；③ 顶栏「刷新」与 R 键的手动入口。
   为什么非要有手动入口：轮询带一组**防打扰暂停条件**（收藏视图 / 批量 / 页面隐藏 / 对比 /
   提示词搜索中 / 参数筛选中 / 被手动关掉）—— 单看每条都合理，副作用却是「刚生成的图没出现」
   时用户毫无办法，只能干等或整页刷新。 */
let autoOn=true;
$("#btnAR").onclick=()=>{
  autoOn=!autoOn;
  setIcoLabel($("#btnAR"), "refresh", autoOn?"自动":"手动");
  $("#btnAR").style.opacity=autoOn?"":"0.5";
  if(autoOn){ lastSig=null; toast("自动刷新已开启","ok"); }
  else toast("自动刷新已关闭");
};

/* 静默重载 + **按 path 复位当前项**。
   复位不是可选项：列表按时间倒序，新图插在最前，而 load() 只换 S.items 不动 S.idx ——
   S.idx 会原地不动地指向**别人**（大图开着时后果最重：Delete 删掉的是另一张，史上有过此坑）。 */
async function silentReload(msg,forceScan=false){
  const keep=(S.view && S.idx>=0 && S.view[S.idx]) ? S.view[S.idx].path : "";
  await load(S.path||S.root, {silent:true,forceScan});
  if(!S.solo && keep) keepIdxByPath(keep);
  if(msg){ toast(msg); setBotState("done", msg, null, false); }    // V3：底栏状态中枢
}

let _pollAt=0;                     // 合并「可见性变化」与「窗口聚焦」两条同时到达的通知
async function pollDir(){
  if(!autoOn || favMode || batchOn || document.hidden || S.solo) return;
  if($("#cmp").classList.contains("on")) return;          // 对比视图另有状态，不打扰
  if($("#qmode").value==="prompt" && $("#q").value.trim()) return;   // 搜索中不打扰
  if($("#fModel").value||$("#fSampler").value||$("#fDim").value) return;   // 筛选分析中不打扰
  const t=Date.now(); if(t-_pollAt<1200) return; _pollAt=t;
  try{
    const j=await api("/api/dirinfo?path="+encodeURIComponent(S.path||S.root)
      +"&rec="+($("#rec").checked?"1":"0"));
    if(!lastSig){ lastSig=j.sig; return; }         // 首次只立基线，不刷
    if(j.sig===lastSig) return;
    lastSig=j.sig;
    await silentReload("检测到目录变化，已刷新",true);
  }catch(e){}
}
/* 常驻轮询（故意不 clear）：每 5s 探一次目录签名（/api/dirinfo），签名变了才静默刷新。
   页面隐藏时 pollDir 内部整条跳过，生命期就是整个页面 ⇒ 没有清理点。
   批次 F 门禁 perf_timerbudget_check.py A6 要求这类裸调用显式写明「常驻」。 */
setInterval(pollDir, 5000);
/* 切回窗口立刻补一次：生成完图切回来是最常见的动线，等满一个 5 秒周期会显得「没反应」。
   （隐藏期间轮询整条跳过、lastSig 保持离开时的值 —— 所以这一补必然比得出变化。）
   注：大图态**不再整条跳过** —— 改为照常刷新 + 按 path 复位，让新图进胶片条而不动当前这张。 */
document.addEventListener("visibilitychange", ()=>{ if(!document.hidden) pollDir(); });
window.addEventListener("focus", ()=>pollDir());

/* ---- 手动刷新：顶栏按钮 / R 键 / 设置页「强制重扫当前目录」共用这一份 ----
   用 /api/rescan 而不是直接 load()：它会清掉服务端的**目录指纹短缓存与统计节流**，
   否则刚写进来的图可能仍被缓存挡住（「重扫」这个接口正是为此存在）。
   代价只丢指纹与节流，不动提示词索引表 —— 比「重建索引」便宜得多。 */
let refreshing=false;
async function mvForceRefresh(opts={}){
  if(refreshing) return null;                  // 连点防抖：一次重扫只发一个请求
  if(S.solo) return null;                      // 单图模式看的是库外文件，刷新库没有意义
  refreshing=true;
  const btn=$("#btnRefresh");
  if(btn){ btn.classList.add("spin"); btn.setAttribute("aria-busy","true"); }
  setBotState("busy","正在刷新当前目录…",null,true);
  try{
    const rc=$("#rec");
    const j=await api("/api/rescan?rec="+((rc && rc.checked)?"1":"0"), {method:"POST"})
      .catch(e=>({error:e.message}));
    if(j.error){
      toastErr("刷新失败", j.error);
      setBotState("err","刷新失败",null,false);
      return null;
    }
    lastSig=j.sig; _pollAt=Date.now();          // 前端指纹一并对齐，免得轮询紧接着再刷一遍
    const keep=(S.view && S.idx>=0 && S.view[S.idx]) ? S.view[S.idx].path : "";
    await load(S.path||S.root, {silent:true,forceScan:true});
    if(keep) keepIdxByPath(keep);
    const n=(typeof j.files==="number") ? j.files : (S.items||[]).length;
    if(typeof loadStats==="function") loadStats();      // 节流位已归零，统计会触发一次增量校验
    if(typeof syncSysinfo==="function") syncSysinfo();
    if(!opts.quiet) toast("已刷新（当层 "+n+" 个文件）","ok");
    /* 底栏让位给统计扫描器：重扫会让提示词索引重建，那边会报「正在扫描生成参数…」——
       那是**更重要**的状态（后台真在跑），抢回来只会让用户看不到索引进度。
       只在它无事可报时兜一个收尾，否则底栏会永久挂着「正在刷新当前目录…」。 */
    setTimeout(function(){
      const el=document.getElementById("botMsg");
      if(el && el.textContent.indexOf("正在刷新当前目录")>=0)
        setBotState("done","已刷新 · 当层 "+n+" 个媒体文件",null,false);
    }, 600);
    return j;
  } finally {
    refreshing=false;
    if(btn){ btn.classList.remove("spin"); btn.removeAttribute("aria-busy"); }
  }
}
window.mvForceRefresh=mvForceRefresh;
if($("#btnRefresh")) $("#btnRefresh").onclick=()=>mvForceRefresh();
/* R：立即刷新。单字母键的既有约定同 / 与 P / F / T —— 输入框里、弹窗里不抢键。 */
document.addEventListener("keydown",e=>{
  if(e.key!=="r" && e.key!=="R") return;
  if(e.ctrlKey||e.metaKey||e.altKey) return;
  const el=e.target, tg=(el.tagName||"").toUpperCase();
  if(tg==="INPUT"||tg==="TEXTAREA"||tg==="SELECT"||el.isContentEditable) return;
  if(document.querySelector(".bigMask")||document.getElementById("catInputMask")) return;
  e.preventDefault(); mvForceRefresh();
});

/* ---------------- lightbox 视频控制条 ---------------- */
function bindVidBar(v){
  const play=$("#vPlay"), speed=$("#vSpeed"), loop=$("#vLoop"), mute=$("#vMute");
  $("#vidBar").classList.add("on");
  if(window.mvSyncToastLift) window.mvSyncToastLift();   // 视频条出现 ⇒ 提示条让位
  speed.value="1"; v.playbackRate=1;
  loop.classList.remove("on"); v.loop=false;
  mute.innerHTML=ico("volume"); v.muted=false;
  const sync=()=>{ play.innerHTML=ico(v.paused?"play":"pause"); };
  v.onplay=sync; v.onpause=sync; sync();
  /* B1（2026-10-08）：这条按钮恢复为**对称切换** —— 暂停就播、播放就停。
     M1 曾把它收窄成「只负责开始」，导致播放中点它完全没反应（用户报障「暂停按钮失效」）：
     index.html 上 #vPlay 的 data-tip 一直写着「播放/暂停（空格）」，契约本就是切换，
     是实现在 M1 被单方面改小。三条入口（本处 / 空格 / 中央按钮）语义必须一致，谁也别
     各持一套。切换判断只读 v.paused 这一个真相来源。 */
  play.onclick=()=>{ if(v.paused) v.play(); else v.pause(); };
  speed.onchange=()=>{ v.playbackRate=+speed.value; };
  loop.onclick=()=>{ v.loop=!v.loop; loop.classList.toggle("on",v.loop); };
  mute.onclick=()=>{ v.muted=!v.muted; mute.innerHTML=ico(v.muted?"volumeOff":"volume"); syncVol(); };
  $("#vFrameL").onclick=()=>{ v.pause(); v.currentTime=Math.max(0,v.currentTime-1/30); };
  $("#vFrameR").onclick=()=>{ v.pause(); v.currentTime=Math.min((v.duration||1e9)-0.01,v.currentTime+1/30); };
  /* V46（批次 G3）：原生 controls 撤掉后补自绘 进度条 + 时间 + 音量。
     样式不用另写 —— V35 的 input[type=range] 全局自绘（base.css:104-123）自动生效，
     与设置抽屉里的滑条同一套令牌；vtime 用 tabular-nums 防时间跳动抖布局。 */
  const seek=$("#vSeek"), vol=$("#vVol"), tEl=$("#vTime");
  const fmtT=s=>{ if(!isFinite(s)) return "0:00"; s=Math.max(0,Math.round(s));
    return Math.floor(s/60)+":"+String(s%60).padStart(2,"0"); };
  let dragging=false;
  const syncT=()=>{ tEl.textContent=fmtT(v.currentTime)+" / "+fmtT(v.duration);
    if(!dragging) seek.value=Math.round((v.currentTime/(v.duration||1))*1000)||0; };
  v.ontimeupdate=syncT; v.onloadedmetadata=syncT; syncT();
  seek.oninput=()=>{ dragging=true; };
  seek.onchange=()=>{ v.currentTime=(+seek.value/1000)*(v.duration||0); dragging=false; };
  vol.oninput=()=>{ v.volume=+vol.value/100; v.muted=+vol.value===0;
    mute.innerHTML=ico(v.muted?"volumeOff":"volume"); };
  function syncVol(){ vol.value=v.muted?0:Math.round((v.volume||1)*100); }
  syncVol();
}

refreshFavCats();
api("/api/info").then(j=>{ if(j.last_export_dir) S.lastExportDir=j.last_export_dir; }).catch(()=>{});

