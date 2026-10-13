/* ComfyMediaViewer · js/batch.js（2026-09-09 从 index.html 拆出，加载顺序与原文一致） */
"use strict";
/* ---------------- 批量选择模式（顶栏「☑ 批量」或右键网格空白处） ---------------- */
let batchOn=false;
let lastBatchIdx=-1;                                   // Shift 连选的锚点（旧下标口径，仅作兜底）
let lastBatchPath="";                                  // 锚点路径：视图重建后仍能定位（P1 修复）
const batchSel=new CMV3.Selection();
/* P1-1：与 server.py 的 DELETE_MAX_PATHS 保持一致。服务端超限时会把被丢弃的路径
   计入 dropped，前端据此不再把它们当成已删除。两边同时改才算改完。 */
const DELETE_MAX_PATHS=20000;

function setBatch(on){
  batchOn=on;
  if(!on){ lastBatchIdx=-1; lastBatchPath=""; }
  document.body.classList.toggle("batch",on);
  if(!on) batchSel.clear();
  syncBatchUI(); updateBatchBar();
  $("#batchBar").classList.toggle("on",on);
  const bb=$("#btnBatch"); if(bb) bb.classList.toggle("on",on);
  /* 批量条与提示条同为「底部居中」⇒ 开关后必须重算避让高度，否则提示条压住批量条 */
  if(window.mvSyncToastLift) window.mvSyncToastLift();
  if(on) toast("已开启批量选择：单击勾选 · Shift 连选 · Esc 退出","ok");
}
function syncBatchUI(){
  grid.querySelectorAll(".card[data-p]").forEach(el=>
    el.classList.toggle("sel", batchSel.has(el.dataset.p)));
}
function updateBatchBar(){
  $("#batchN").textContent=`已选 ${batchSel.size}`;
  $("#bDel").disabled=!batchSel.size;
  $("#bExp").disabled=!batchSel.size;
  $("#bCmp").disabled=!(batchSel.size>=2&&batchSel.size<=4);   // 对比仅 2-4 张可用
}

async function selectAllView(){
  const tok=viewGen,button=$("#bAll");button.disabled=true;
  try {
    if(CMVQuery.active && FLOW) await CMVQuery.ensureAll(()=>!batchOn);
    if(tok!==viewGen || !batchOn)return;
    for(const it of S.view) if(it.type==="image"||it.type==="video") batchSel.addItem(it);
    syncBatchUI();updateBatchBar();
  }catch(error){toast(error.message,"err");}
  finally{button.disabled=false;}
}
grid.addEventListener("contextmenu",e=>{
  if(e.target.closest(".card")) return;                // 卡片上的右键留给收藏菜单
  if(lb.classList.contains("on")) return;
  e.preventDefault(); e.stopPropagation();   // 阻止冒泡：否则本处理器末尾注册的 once contextmenu 关闭监听会在冒泡阶段立刻移除菜单
  closeCatMenu();
  const m=document.createElement("div"); m.id="catMenu";
  /* 与卡片右键菜单同一个 #catMenu：补一个空图标槽，保持同栅格的行首对齐。
     这里的条目（批量选择 / 全选 / 清除选择）本批不图标化。 */
  const mk=(txt,cls,fn,icon)=>{ const d=document.createElement("div");
    d.className="cmi"+(cls?" "+cls:"");
    d.appendChild(menuCellIcon(icon)); d.appendChild(menuCellText(txt));
    d.onclick=()=>{ m.remove(); fn(); }; m.appendChild(d); };
  mk(batchOn?"退出批量选择":"批量选择","",()=>setBatch(!batchOn), batchOn?null:"batch");
  if(batchOn){
    mk("全选当前视图","",()=>void selectAllView());
    mk("清除选择","",()=>{ batchSel.clear(); syncBatchUI(); updateBatchBar(); });
  }
  document.body.appendChild(m);
  /* 贴边上限与 CSS 同源（V2.10.0 · E4）：#catMenu{min-width:calc(190px*var(--fsx))}。
     字号放大后菜单更宽更高，这里也要跟着放大，否则菜单会被推出视口。 */
  const _fsx=parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--fsx"))||1;
  m.style.left=Math.min(e.clientX, innerWidth-190*_fsx)+"px";
  m.style.top=Math.min(e.clientY, innerHeight-130*_fsx)+"px";
  document.addEventListener("click",closeCatMenu,{once:true});
  document.addEventListener("contextmenu",closeCatMenu,{once:true});
});
$("#bAll").onclick=()=>void selectAllView();
$("#bNone").onclick=()=>{ batchSel.clear(); syncBatchUI(); updateBatchBar(); };
$("#bCmp").onclick=()=>openCmp();
$("#cmpExit").onclick=closeCmp;
$("#bExit").onclick=()=>setBatch(false);
$("#btnBatch").onclick=()=>setBatch(!batchOn);
/* H5（2026-10-06）：顶栏「⋯ 工具▾」下拉已拆掉 —— 它只剩「查重 / 出图统计」两项，
   两个都是要跑全库扫描的低频操作，为它们在顶栏常驻一个下拉触发器不划算。
   两项已收进设置面板「数据」页的「分析工具」区（绑定见 js/settings.js 的 setupSettingsPanel）。
   ⚠ 别在这里重新加回 #btnMore 的绑定 —— 那个 id 已从 index.html 移除。 */
/* 「导出…」：把所选文件复制到目标文件夹（图片剥离生成元数据，其余原样复制）。
   注：此按钮此前只有 disabled 状态、没有任何点击处理，属于历史遗留的死按钮，本次补上。 */
$("#bExp").onclick=()=>{
  const paths=[...batchSel];
  const targets=batchSel.targets(paths);
  if(!paths.length){ toast("先勾选要导出的文件"); return; }
  const dlg=bigDialog("导出到文件夹");
  dlg.body.innerHTML=
    '<div class="row" style="color:var(--tx2);margin-bottom:10px">已选 '+paths.length
    +' 个文件；图片会剥离生成元数据后复制，其余格式原样复制。目标目录不存在会自动创建。</div>'
    +'<div style="display:flex;gap:8px">'
    +'<input id="expPath" style="flex:1" placeholder="目标文件夹绝对路径，如 D:\\export" spellcheck="false">'
    +'<button id="expGo" class="primary">导出</button></div>';
  const inp=dlg.body.querySelector("#expPath");
  inp.value=S.lastExportDir||"";
  const go=async()=>{
    const dest=inp.value.trim();
    if(!dest){ toast("请输入目标文件夹路径","err"); return; }
    const btn=dlg.body.querySelector("#expGo"); btn.disabled=true;
    const j=await api("/api/batch_export",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({paths, dest, targets})}).catch(e2=>({error:e2.message}));
    btn.disabled=false;
    if(j.error){ toast(j.error,"err"); return; }
    S.lastExportDir=j.dest||dest;
    dlg.close();
    if(j.failed && j.failed.length)          // V12：一行只放摘要，逐条原因进「详情」
      toastErr(`已导出 ${j.done} 个，${j.failed.length} 个失败`,
        j.failed.map(f=>`${f.path||f.name||"(未命名)"}\n  → ${f.error||"未知原因"}`).join("\n\n"));
    else toast(`已导出 ${j.done} 个到 ${j.dest}`,"ok");
  };
  dlg.body.querySelector("#expGo").onclick=go;
  inp.onkeydown=e2=>{ if(e2.key==="Enter") go(); };
  inp.focus();
};
$("#bDel").onclick=()=>{
  const paths=[...batchSel]; if(!paths.length) return;
  const targets=batchSel.targets(paths);
  const hasMore=paths.length>DELETE_MAX_PATHS;
  confirmDialog("批量删除",
    `确定删除选中的 ${paths.length} 个文件吗？文件将移入${delTipWhere()}。`
    + (hasMore?`\n\n⚠️ 一次最多删除 ${DELETE_MAX_PATHS} 个，超出的部分不会被删除，请删完这批再继续。`:""),
    async()=>{
    /* ε-14（§6.8）：删除期间给进行中反馈 —— 此前弹窗一关界面就静默，大批量
       （几百张，含系统回收站 COM 调用）好几秒没有任何迹象，用户会再点一次。 */
    setBotState("busy", `正在删除 ${paths.length} 个文件…`, null, true);
    let ok=0, fb=0; const failed=new Set(); let dropped=0;
    try{
      const j=await api("/api/delete",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({paths, targets})});          // 单请求批量，避免 N 次往返
      ok=j.deleted||0;
      fb=j.fallback||0;                          // 系统回收站不可用、退回应用内回收站的个数
      dropped=j.dropped||0;                      // P1-1：服务端因超上限而未处理的条数
      for(const p of (j.failed||[])) failed.add(normPath(p));   // 归一化后再比对
      noteDeleteMeta(j);                       // §6.1：元数据没写回要说出来
    }catch(e){ for(const p of paths) failed.add(normPath(p)); }
    /* ⚠️ P1-1：删除成功的集合必须以**服务端回报的 deleted 数量**为界，不能按
       「全部路径 − failed」推断 —— 服务端若因超限丢弃了尾部路径，它们不在 failed 里，
       推断法会把还在磁盘上的文件从列表里抹掉（界面显示删了、文件其实还在）。 */
    let dead;
    if(failed.size || dropped){
      // 有失败或截断：只把「按服务端计数确定已删」的部分当作已删，其余保持原样
      const candidates=[...paths].filter(p=>!failed.has(normPath(p)));
      dead=new Set(candidates.slice(0, ok));
    }else{
      dead=new Set(paths);
    }
    batchSel.clear();
    S.items=S.items.filter(it=>!dead.has(it.path));    // 同步内存列表
    applyFilter(); updateBatchBar();
    if(window.mvUpdateTrashBadge) window.mvUpdateTrashBadge();   // V2：删完同步顶栏角标
    const donePaths=[...dead];
    const left = paths.length - ok;                    // 没删掉的（含失败 + 被截断）
    const msg = left ? `已删除 ${ok} 个，未删除 ${left} 个`
                     : `已删除 ${ok} 个（${delDoneMsg()}）`;
    if(dropped && !failed.size){
      setBotState("err","有文件未删除",null,false);
      toastErr(`已删除 ${ok} 个`, `超出单次上限 ${DELETE_MAX_PATHS} 个，剩余 ${left} 个未处理，请再删一次`);
    }
    // 进了系统回收站的文件没法在应用内还原 → 不给「撤销」按钮，免得点了必然失败
    else if(delIsSystem() && !fb){ setBotState(left?"err":"ok", left?"部分未删除":"删除完成", null, false); toast(msg, left?"err":"ok"); }
    else { setBotState(left?"err":"ok", left?"部分未删除":"删除完成", null, false); toastAction(msg, "撤销", ()=>undoDelete(donePaths), left?"err":"ok"); }
  });
};
document.addEventListener("keydown",e=>{               // Esc：菜单 → 对比 → 批量 → 大图
  if(e.key==="Escape" && document.getElementById("catMenu")){ closeCatMenu(); return; }
  if(e.key==="Escape" && $("#cmp").classList.contains("on")){ closeCmp(); return; }
  if(e.key==="Escape" && batchOn && !lb.classList.contains("on")
     && e.target.tagName!=="INPUT") setBatch(false);
});
/* 全局快捷键：/ 聚焦搜索框 · Esc（在搜索框里）清空搜索 */
document.addEventListener("keydown",e=>{
  const el=e.target, tg=(el.tagName||"").toUpperCase();
  const typing = tg==="INPUT"||tg==="TEXTAREA"||tg==="SELECT"||el.isContentEditable;
  if(e.key==="/" && !typing && !lb.classList.contains("on")
     && !document.querySelector(".bigMask")){
    e.preventDefault(); const q=$("#q"); if(q){ q.focus(); q.select(); }
    return;
  }
  if(e.key==="Escape" && el===$("#q") && $("#q").value){
    $("#q").value=""; viewGen++;   // ε-6 E6-4：统一令牌，作废在途的搜索/视图加载
    applyFilter(); toast("已清空搜索");
  }
});
/* 网格回到顶部按钮：滚动超过一屏才出现 */
(function(){
  const gw=document.getElementById("gridwrap"), btn=document.getElementById("toTop");
  if(!gw||!btn) return;
  const sync=()=>{ btn.hidden = gw.scrollTop < 600; };
  gw.addEventListener("scroll",sync,{passive:true});
  btn.onclick=()=>gw.scrollTo({top:0,behavior:"smooth"});
  sync();
})();
const sent=document.createElement("div"); sent.id="sent";
sent.style.cssText="height:1px";
grid.insertAdjacentElement("afterend", sent);
new IntersectionObserver(es=>{ if(es[0].isIntersecting) renderMore(); },
  {rootMargin:"1600px"}).observe(sent);

/* 网格视频懒加载：进入视口附近才挂 src（数据存在 data-src 上），
   重新渲染时由 applyFilter/promptSearch 先 disconnect 清掉旧观察 */
const vObs=new IntersectionObserver(ens=>{
  for(const en of ens){ if(!en.isIntersecting) continue;
    const v=en.target;
    if(!v.src && v.dataset.src) {
      const item=v.tagName==="VIDEO" ? itemByPath(v.closest(".card")?.dataset.p) : null;
      if(item) void CMVPlayback.preview(v,item); else v.src=v.dataset.src;
    }
    vObs.unobserve(v);
  }
},{rootMargin:"800px"});

/* ================================================================
   扩展功能：收藏夹视图 / 查重 / 元数据筛选 / 回收站 / 统计 / 自动刷新 / 视频控制
   ================================================================ */

/* 从内存列表移除已删除文件并重渲染（查重/批量删除共用）
   V3.0（2026-10-12）：新增可选 onCommit —— 视图重建完成后回调，
   供「大图里删除后接替打开邻居」在那时兑现（applyFilter 可能被淡出动画延后 200ms）。 */
let PURGE_T=null;
let PURGE_PEND=null;   // 已经加了 .removing、但淡出动画还没落地的那一批（E6-5）
function purgePaths(paths, onCommit){
  /* E6-5：`commit` 原先闭包捕获**当次**的 `dead`，而「上一次还没落地就再来一次」
     的分支写的是 `clearTimeout(...); commit();` —— 它取消的是**上一次**的定时器，
     执行的却是**这一次**的 commit ⇒ **上一次那一批永远不会被移除**：
     数据还在 S.items 里、卡片还挂着 `.removing`（半透明残留在网格里），
     点它还能打开一张磁盘上已经没有的图；计数与统计也跟着偏大。
     原注释写的「立即补 commit（不叠加）」本意是对的，实现漏了「把两批并起来」。
     现在 `commit(deadSet)` 收一个集合，补提交时把上一批并入 —— 两批都不丢。 */
  const dead=new Set(paths);
  const commit=(deadSet)=>{
    S.items=S.items.filter(it=>!deadSet.has(it.path));
    if(S.tagView) S.tagView=S.tagView.filter(it=>!deadSet.has(it.path));  // 标签筛选基准列表同步清理
    if(S.metaMap) for(const p of deadSet) delete S.metaMap[p];
    for(const p of deadSet) batchSel.delete(p);
    if(batchOn) updateBatchBar();
    applyFilter();
    refreshStatsSoon();                            // 删除会改变目录数量口径
    /* V3.0：唯一的「视图已重建」挂点 —— 接替必须在这里兑现，而不是调用 purgePaths 的当下
       （PURGE_T 分支会把 applyFilter 推到淡出动画之后，那时列表才真正变短）。 */
    if(onCommit){ try{ onCommit(); }catch(e){} }
  };
  /* V14：被删的卡片先淡出并让位，之后再重建网格 —— 「我这一下点到了」的确认感。
     数据仍立即从内存移除（commit 里做），只是 DOM 延后一个动画时长；
     reduced-motion 下跳过动画；上一次还没落地就再来一次时立即补 commit（不叠加）。 */
  const cards=[...(typeof grid!=="undefined"&&grid?grid.querySelectorAll("[data-p]"):[])]
    .filter(c=>dead.has(c.dataset.p));
  const reduce=window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if(PURGE_T){
    /* 上一次的淡出还没落地 ⇒ 立刻补提交，且**必须把上一批并进来**（E6-5）。 */
    clearTimeout(PURGE_T); PURGE_T=null;
    const merged=new Set(PURGE_PEND||[]);
    for(const p of paths) merged.add(p);
    PURGE_PEND=null;
    commit(merged);
    return;
  }
  if(!cards.length || reduce){ commit(dead); return; }
  cards.forEach(c=>c.classList.add("removing"));
  PURGE_PEND=[...paths];
  PURGE_T=setTimeout(()=>{ PURGE_T=null; PURGE_PEND=null; commit(dead); }, 200);
}

/* -------- 撤销删除后回到大图（V3.0 · 2026-10-12）--------
   删除后接替打开了邻居；用户接着按「撤销」，若大图停在邻居上，等于撤销只做了一半。
   ⚠️ `undoDelete` 是 batch / extras / nav **三处共用**的全局函数，所以这里两条纪律：
     ① 来源标记**读后即清** —— 否则「批量删除 → 稍后撤销」会莫名其妙打开大图；
     ② 标记带时间戳、60s 过期 —— toast 上的撤销本来就有时效，过期残留不该再生效。 */
let _lastLbDel={path:"", at:0};
function mvNoteLbDelete(path){ _lastLbDel={path:path||"", at:Date.now()}; }
/* 还原会让 `undoDelete` 末尾的 load() 重新扫目录，文件要等异步加载回来才重新进 S.view，
   ⇒ 不能在还原成功的当下定位。这里轮询等它出现（≤6s）—— 比往 applyFilter 里塞钩子侵入面小。
   ⚠️ 首版这里把「大图开着且当前不是目标」当成「用户自己换了图」而早退 —— 那是错的：
      撤销场景下大图本来**就**开着（停在删除后自动接替的那张），条件恒真 ⇒ 永不兑现。
      正确的让位判据是「大图被换到了**别的地方**」：拿调用瞬间那张做基线比对。 */
async function _reopenAfterUndo(path){
  const atPath=((S.view[S.idx]||{}).path)||"";
  for(let i=0;i<24;i++){
    const idx=(S.view||[]).findIndex(x=>x && x.path===path);
    if(idx>=0){ openLb(idx); return; }
    if(lb.classList.contains("on")){
      const cur=((S.view[S.idx]||{}).path)||"";
      if(cur && cur!==atPath) return;     // 用户自己翻到别处了 ⇒ 让位
    }
    await new Promise(r=>setTimeout(r,250));
  }
}

/* 撤销删除：/api/delete 只返回删除数量，所以按原始路径去回收站里找回 key 再还原 */
async function undoDelete(paths){
  const _note=_lastLbDel; _lastLbDel={path:"", at:0};      // 读后即清（见上方①）
  const backTo=(_note.path && paths.includes(_note.path) && Date.now()-_note.at<60000)
    ? _note.path : "";
  try{
    const t=await api("/api/trash");
    const keys=[];
    for(const p of paths){
      const want=normPath(p);
      const hit=(t.items||[]).filter(x=>normPath(x.orig)===want).sort((a,b)=>b.t-a.t)[0];
      if(hit) keys.push(hit.key);
    }
    if(!keys.length){ toast("撤销失败：回收站里找不到这些文件","err"); return; }
    let ok=0; const partial=[]; const bad=[];
    for(const k of keys){
      try{
        const r=await api("/api/trash_restore",{method:"POST",
          headers:{"Content-Type":"application/json"},body:JSON.stringify({key:k})});
        ok++;
        if(r.partial) partial.push((r.path||"").split(/[\\/]/).pop()||"");   // P1-2 同上
      /* P0-17：原先这里是 `catch(_){}` —— 还原**全部失败**时会一路走到下面那句
         「已撤销删除，还原 0 个文件」（成功样式），用户以为撤销生效了，实际一个都没回来。
         失败必须计数、必须如实说，且不能混进「成功」文案。 */
      }catch(e){ bad.push((e&&e.message)||"未知错误"); }
    }
    if(bad.length && !ok)
      toastErr("撤销失败", `${bad.length} 个文件都没能还原：${bad[0]}`);
    else if(bad.length)
      toastErr(`已撤销 ${ok} 个，${bad.length} 个失败`,
               `${bad.length} 个未还原：${bad[0]}`);
    else if(partial.length)
      toastErr(`已撤销删除，还原 ${ok} 个文件`,
               `${partial.length} 个的收藏 / 标签 / 标记未能写回，需要手工恢复`);
    else
      toast(`已撤销删除，还原 ${ok} 个文件`,"ok");
    refreshStatsSoon();
    if(window.mvUpdateTrashBadge) window.mvUpdateTrashBadge();   // V2：还原后同步顶栏角标
    load(S.path||S.root,{silent:true});
    /* V3.0：这次撤销本来就是从大图里点的删除 ⇒ 等文件回到列表后把它重新打开 */
    if(backTo) _reopenAfterUndo(backTo).catch(()=>{});
  }catch(e){ toast("撤销失败："+e.message,"err"); }
}

/* 通用大弹窗：Esc/点遮罩关闭（Esc 用捕获阶段，优先于 lightbox 等快捷键） */
function bigDialog(title, opts={}){
  const old=document.querySelector(".bigMask");
  if(old){ if(old._close) old._close(); else old.remove(); }   // 走 close 才能摘掉上一个弹窗的 keydown
  const mask=document.createElement("div"); mask.className="bigMask";
  const box=document.createElement("div"); box.className="bigBox";
  if(opts.wide) box.style.width="min(1060px,96vw)";
  box.innerHTML='<div class="bhd"><b></b><button><span class="ic">'+ico("x")+'</span></button></div><div class="bbd"></div>';
  box.querySelector("b").textContent=title;
  mask.appendChild(box); document.body.appendChild(mask);
  // close 必须移除监听器：此前每次开弹窗都在 document 上留一个永不摘除的 keydown，
  // 关掉的弹窗仍会被后续 Esc 触发（且 mask.isConnected 为 false 后变成纯空转的常驻句柄）
  const onKey=e=>{
    if(e.key!=="Escape" || !mask.isConnected) return;
    e.stopPropagation(); close();
  };
  const close=()=>{ document.removeEventListener("keydown", onKey, true); mask.remove(); };
  box.querySelector(".bhd button").onclick=close;
  if(!opts.noDismiss) mask.addEventListener("click",e=>{ if(e.target===mask) close(); });
  document.addEventListener("keydown", onKey, true);
  mask._close=close;
  return {mask, box, body:box.querySelector(".bbd"), close};
}

