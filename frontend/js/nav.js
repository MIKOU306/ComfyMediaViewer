/* ComfyMediaViewer · js/nav.js（2026-09-09 从 index.html 拆出，加载顺序与原文一致） */
"use strict";
/* ---------------- 常用目录快速切换（自定义下拉） ----------------
   原生 <select> 在未展开时 hover 不触发 title，装了多个整合包时会扫出多个
   同名 output 而只能盲选。改为自定义 listbox：
     · 按钮与每个菜单项都带完整路径 title —— 鼠标移上去即显示地址
     · 同名项自动附加父目录名后缀（逐级上溯直到能互相区分），不悬停也能分辨 */
let ROOTS=[], ROOT_LABEL=new Map();          // path -> {name, suffix}

const segPath=p=>String(p).replace(/[\\/]+$/,"").split(/[\\/]/).filter(Boolean);

/* 为每条路径算显示名：basename 冲突时逐级补父目录段作后缀 */
function rootLabels(list){
  const out=new Map(), g=new Map();
  for(const p of list){
    const s=segPath(p), b=(s[s.length-1]||p).toLowerCase();
    if(!g.has(b)) g.set(b,[]);
    g.get(b).push(p);
  }
  for(const ps of g.values()){
    for(const p of ps){                        // 先按「无后缀」落底，冲突者再补
      const s=segPath(p);
      out.set(p, {name:s[s.length-1]||p, suffix:""});
    }
    if(ps.length===1) continue;
    // 同名：逐项单独上溯，取「刚好能把自己区分出来」的最小深度（父 → 祖父/父 → …）
    for(const p of ps){
      const s=segPath(p), maxUp=s.length-1;
      for(let d=1; d<=maxUp; d++){
        const up=s.slice(s.length-1-d, s.length-1).join("\\");
        const key=up.toLowerCase();
        const cnt=ps.filter(q=>{ const t=segPath(q);
          return t.length-1>=d &&
                 t.slice(t.length-1-d, t.length-1).join("\\").toLowerCase()===key; }).length;
        if(cnt===1 || d===maxUp){ out.set(p, {name:s[s.length-1]||p, suffix:up}); break; }
      }
    }
  }
  return out;
}

function paintRootBtn(){
  const btn=$("#rootBtn"); if(!btn) return;
  const L=ROOT_LABEL.get(S.root)||{name:S.root||"—", suffix:""};
  btn.querySelector(".rs-name").textContent=L.name||"—";
  btn.querySelector(".rs-suf").textContent=L.suffix? "· "+L.suffix : "";
  btn.dataset.tip=S.root? S.root : "常用目录（切换即保存）";
}

/* ε-2（2026-09-20）：菜单打开期间挂的滚动监听句柄。
   ⚠️ 原先是在模块加载时**一次性挂到 document 的捕获阶段**（永不注销），见下方 bindRootSel 的说明。 */
let _rmScrollH=null;
function _rmUnbindScroll(){
  if(_rmScrollH){ document.removeEventListener("scroll", _rmScrollH, true); _rmScrollH=null; }
}
function closeRootMenu(){
  _rmUnbindScroll();
  const m=document.getElementById("rootMenu"); if(m) m.remove();
  const btn=$("#rootBtn"); if(btn) btn.setAttribute("aria-expanded","false");
}

function openRootMenu(){
  closeRootMenu();
  const btn=$("#rootBtn"); if(!btn) return;
  const m=document.createElement("div");
  m.id="rootMenu"; m.setAttribute("role","listbox");
  /* 目录列表与底部「选择文件夹…」拆成两段（V2.9.2）：列表自己滚动，
     底部入口固定、不浮动 —— 早先用 sticky 粘底，它会浮在滚动内容之上，
     把滚到那一行的目录项盖住（看着在那儿、点下去却是"选文件夹"）。 */
  const list=document.createElement("div"); list.className="rm-list";
  if(!ROOTS.length){
    list.innerHTML='<div class="rm-empty">暂无常用目录</div>';
  }else{
    for(const p of ROOTS){
      const L=ROOT_LABEL.get(p)||{name:p, suffix:""};
      const d=document.createElement("div");
      d.className="rmi"+(p===S.root?" cur":"");
      d.setAttribute("role","option");
      d.dataset.path=p;
      d.dataset.tip=p;                              // 悬停即显示完整路径
      d.innerHTML='<span class="rm-name">'+esc(L.name)+'</span>'+
        (L.suffix? '<span class="rm-suf">· '+esc(L.suffix)+'</span>' : '')+
        '<span class="rm-path">'+esc(p)+'</span>';
      d.onclick=e=>{ e.stopPropagation(); closeRootMenu(); pickRoot(p); };
      d.onmouseenter=()=>{ const hi=m.querySelector(".rmi.sel");
        if(hi) hi.classList.remove("sel"); d.classList.add("sel"); };
      list.appendChild(d);
    }
  }
  m.appendChild(list);
  /* 末尾固定一项：选择任意文件夹（原顶栏「目录选择」按钮并入这里，
     这样「切换根目录」只有一个入口：常用目录 + 选择文件夹） */
  const foot=document.createElement("div");
  foot.className="rm-foot"+(ROOTS.length?" sep":"");
  const pick=document.createElement("div");
  pick.className="rmi rmi-pick"; pick.dataset.tip="选择任意文件夹作为根目录";
  /* ε-2：键盘分支需要一个「点它该干什么」的标记 —— 见 rootMenuKey 的 Enter 分支。 */
  pick.dataset.act="pick";
  pick.innerHTML='<span class="rm-name">'+ico("dirOpen")+'<span>选择文件夹…</span></span>';
  pick.onclick=e=>{ e.stopPropagation(); closeRootMenu(); dirPicker(); };
  foot.appendChild(pick);
  m.appendChild(foot);
  document.body.appendChild(m);
  const r=btn.getBoundingClientRect();
  let top=r.bottom+4;                          // 默认贴按钮下沿，放不下则翻到上方
  if(top+m.offsetHeight > innerHeight-8) top=Math.max(6, r.top-m.offsetHeight-4);
  m.style.left=Math.max(6, Math.min(r.left, innerWidth-m.offsetWidth-8))+"px";
  m.style.top=Math.max(6, top)+"px";
  btn.setAttribute("aria-expanded","true");
  const cur=m.querySelector(".rmi.cur")||m.querySelector(".rmi");
  if(cur){ cur.classList.add("sel"); cur.scrollIntoView({block:"nearest"}); }
  setTimeout(()=>{                             // 延后一拍：否则打开菜单的这次点击会立刻关掉它
    document.addEventListener("click", closeRootMenu, {once:true});
    document.addEventListener("contextmenu", closeRootMenu, {once:true});
  }, 0);
  /* ε-2（2026-09-20）：滚动关闭 —— 从此有了**两条例外**。
     原先写的是 `document.addEventListener("scroll", closeRootMenu, true)`（模块期注册、
     永不注销）。scroll 事件**不冒泡**，但**捕获阶段照样下传**到目标 ⇒ 这句等价于
     「页面上任何地方滚动都关菜单」，比它的注释「左栏滚动也算」宽得多。

     后果很具体：`rootMenuKey` 用 ↓ 键导航时会对选中项调 `scrollIntoView`，
     列表一长 ⇒ 我们自己触发的这个 scroll 被上面的捕获监听接住 ⇒ **菜单当场关闭**，
     末尾那项「选择文件夹…」永远用键盘够不到（它在独立滚动区里，必须滚才能到）。

     所以两条例外：① 事件来自菜单内部 ⇒ 忽略（保住键盘导航与手指拨列表）；
     ② 改为**开菜单时才注册、关菜单时注销**（原来是常驻，白听一辈子）。
     注意 set 之前先 unbind：连点一次也不会叠出第二个句柄。 */
  _rmUnbindScroll();
  _rmScrollH=e=>{
    const mm=document.getElementById("rootMenu");
    if(!mm){ _rmUnbindScroll(); return; }        // 已经关了 ⇒ 顺手摘掉监听
    if(mm.contains(e.target)) return;            // 菜单自己滚动不算
    closeRootMenu();
  };
  document.addEventListener("scroll", _rmScrollH, true);
}

function rootMenuKey(e){
  const m=document.getElementById("rootMenu"); if(!m) return;
  const items=[...m.querySelectorAll(".rmi")]; if(!items.length) return;
  let i=items.findIndex(x=>x.classList.contains("sel"));
  if(e.key==="ArrowDown") i=(i+1)%items.length;
  else if(e.key==="ArrowUp") i=(i<=0? items.length-1 : i-1);
  else if(e.key==="Home") i=0;
  else if(e.key==="End") i=items.length-1;
  else if(e.key==="Enter"||e.key===" "){
    e.preventDefault();
    const t=items[i>=0? i : 0];
    /* ε-2（F5 P2-4 **下半段**）：末项「选择文件夹…」按下去要真的有用。
       它带 .rmi 类 ⇒ 会被 items 收进去，但它没有 dataset.path，
       原来的 `pickRoot(t.dataset.path)` 传进去的是 undefined ⇒
       pickRoot 首行 `if(!v || v===S.root) return;` 直接返回，菜单关掉、什么也没发生。
       ④⑤ 里 ④ 只让键盘**够得到**它（菜单不再被自己的滚动关掉），
       这条才让**按下去有效** —— 够得到 ≠ 有用。
       ⚠️ 与鼠标保持同一语义：pick.onclick 调的也是 dirPicker()。 */
    if(t){
      const act=t.dataset.act;
      closeRootMenu();
      if(act==="pick") dirPicker();
      else pickRoot(t.dataset.path);
    }
    return;
  }
  else if(e.key==="Escape"){
    e.preventDefault(); closeRootMenu();
    const b=$("#rootBtn"); if(b) b.focus();
    return;
  }
  else return;
  e.preventDefault();
  items.forEach(x=>x.classList.remove("sel"));
  items[i].classList.add("sel");
  items[i].scrollIntoView({block:"nearest"});
}

async function pickRoot(v){
  if(!v || v===S.root) return;
  const j2=await (window.CMVClassic?CMVClassic.switchRoot(v):api("/api/root",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({path:v})})).catch(e=>({error:e.message}));
  if(!j2)return;
  if(j2.error){ toast(j2.error,"err"); return; }
  toast("已切换到 "+v,"ok");
  load("");
  /* 若这个根是「旧目录搬了家」，后端此刻正在后台做指纹迁移 —— 稍等一拍取报告，
     好让用户知道数据找回来了（取走即清，没有迁移时这个请求安静地返回 null）。 */
  setTimeout(()=>{ try{ mvMigrateReport(); }catch(e){} }, 4000);
}

async function refreshRoots(){
  const j=await api("/api/roots").catch(()=>({roots:[]}));
  const list=(j.roots||[]).slice();
  if(S.root && !list.includes(S.root)) list.unshift(S.root);   // 当前目录不在列表里时补上
  ROOTS=list;
  ROOT_LABEL=rootLabels(list);
  paintRootBtn();
}

(function bindRootSel(){
  const btn=$("#rootBtn"); if(!btn) return;
  btn.onclick=e=>{ e.stopPropagation();
    document.getElementById("rootMenu")? closeRootMenu() : openRootMenu(); };
  btn.onkeydown=e=>{
    if(!document.getElementById("rootMenu") &&
       (e.key==="ArrowDown"||e.key==="Enter"||e.key===" ")){
      e.preventDefault(); openRootMenu(); return;
    }
    rootMenuKey(e);
  };
  document.addEventListener("keydown",e=>{      // Esc 关闭（按钮失焦时也能关）
    if(e.key==="Escape" && document.getElementById("rootMenu")) closeRootMenu();
  });
  // 这里的 scroll 监听已移入 openRootMenu（随开随挂、随关随摘，并忽略菜单内部滚动）——
  // 见 openRootMenu 里 ε-2 那段注释：常驻的捕获监听会把键盘导航吃到关闭。
  /* C-P4：rAF 节流 —— 关菜单本身很轻，但 resize 在拖窗口时不断触发，
     统一走项目共用的帮助函数（app.js 定义，本文件在其之后加载）。 */
  window.addEventListener("resize", (window.mvThrottleRaf || (f => f))(closeRootMenu));
})();

function closeLb(){ lbTok++;
  /* V3.0（2026-10-12）：作废在途的「删除后接替」声明 ——
     否则「删除 → 分页 load 在途 → 用户自己按 Esc 关掉 → load 回来」会把它又翻开。
     用户显式关大图的意图必须压过自动接替。 */
  if(typeof mvClearPendingFocus==="function") mvClearPendingFocus();
  // 单图模式关掉就结束：S.view / S.idx 全程没被动过，网格与左栏选中态原样还在
  if(S.solo){ S.solo=null; document.body.classList.remove("solo"); }
  const _v=stage.querySelector("video");      // 释放视频：先暂停再断 src，避免后台继续解码
  // ε-2：改走 viewer.js 的统一实现（关大图与切条目同一份逻辑），保留内联 ops 作兜底
  if(window.mvReleaseStageVideo) window.mvReleaseStageVideo();
  else if(_v){ try{ _v.pause(); _v.removeAttribute("src"); _v.load(); }catch(e){} }
  lb.classList.remove("on"); stage.innerHTML="";
  /* G2·V16③：**先解冻再归还** —— 触发元素在 #content/#main 内，inert 未撤时聚焦不了。
     ⚠️ 这一句不能省：`lb.on` 期间整块 #main 是 inert 的（app.js modalOpen()），
     而 closeLb 与 focus() 是同一个同步块 —— 等 MutationObserver 下一拍才解冻，
     此刻 focus() 会**静默失败**（实测：门禁 G13a 焦点回落不到原卡片）。
     V3.0（Phase 3）：触发卡片已经被删掉时（本次改动后很常见 —— 删完自动接替又关掉大图），
     焦点没处可还 ⇒ 把网格滚到「当前项」那张，否则用户看到的是列表顶部。 */
  if(window.mvSyncInert) window.mvSyncInert();
  if(lbFocusBack && lbFocusBack.isConnected && lbFocusBack.focus) lbFocusBack.focus();
  else if(typeof mvScrollToCurrentCard==="function") mvScrollToCurrentCard();
  lbFocusBack=null;
  $("#vidBar").classList.remove("on"); $("#zoomBadge").style.display="none"; S.vidInfo=null;
  if(window.mvSyncToastLift) window.mvSyncToastLift();   // 视频条收起 ⇒ 提示条落回
  if(window.mvClearPrefetch) window.mvClearPrefetch();   // V52：关大图清预取（不再占带宽）
}
async function step(d){
  if(d>0 && curIdx()+d>=curList().length && FLOW && CMVQuery.active && CMVQuery.state.nextCursor) {
    const token=lbTok,path=curItem()?.path;
    try {
      while(CMVQuery.state.loading) await new Promise(done=>setTimeout(done,50));
      if(token!==lbTok || curItem()?.path!==path)return;
      await CMVQuery.loadMore({strict:true});
      if(token!==lbTok || curItem()?.path!==path)return;
    }catch(error){toast(error.message,"err");return;}
  }
  const v=curList(); let n=curIdx()+d;   // 单图列表只有 1 张 → 自然不动
  while(n>=0 && n<v.length){
    const it=v[n];
    if(it.type==="image"||it.type==="video"){ openLb(n); return; }
    n+=d;
  } }

/* ---------------- events ---------------- */

$("#btnClose").onclick=closeLb;
$("#navL").onclick=()=>step(-1); $("#navR").onclick=()=>step(1);
$("#btnDl").onclick=()=>{ const it=curItem(); if(!it) return;
  // 走统一下载入口（gallery.js）：落盘目录设置、元数据抹除与否的如实提示都在那边
  downloadClean(it);
};
$("#btnDel").onclick=()=>{ const it=curItem();
  // 双保险：按钮在单图模式下已被 CSS 隐藏，但 Delete 快捷键仍会点它，
  // 这一句挡住「看着别人的图，顺手删掉自己库里一个文件」
  if(!it || S.solo) return;
  confirmDialog("删除图片", `确定删除「${it.name}」吗？文件将移入${delTipWhere()}。`, async()=>{
    /* V3.0（2026-10-12）：删掉后在大图里接替打开邻居，不再一律退回网格。
       邻居必须在**删除前**的列表上算 —— 删完 S.view 已变短，S.idx 那个位置已经不是它了。 */
    const wasOn=lb.classList.contains("on");
    const succ=wasOn ? pickSuccessorPath(curList(), curIdx(), +1) : "";
    try{
      const j=await api("/api/delete",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify(CMV3.target(it))});
      if(j.error || j.ok===false){ toast(j.error||"删除失败","err"); return; }
      noteDeleteMeta(j);                     // §6.1：文件删了、元数据没写回要说出来
      // 从列表与视图中移除
      const i1=S.items.indexOf(it); if(i1>=0) S.items.splice(i1,1);
      const i2=S.view.indexOf(it);  if(i2>=0) S.view.splice(i2,1);
      S.idx=-1;                                // 先复位：视图变短后旧下标可能越界
      applyFilter();                           // 重建视图（服务端分页模式下可能是异步的）
      if(wasOn){
        /* 接替是「就地翻页」，与 step() 同一个走法 —— **不先 closeLb()**：
           关了会白闪一次、还会把 lbFocusBack 提前归还再重记。 */
        if(succ) focusAfterViewRebuild(succ);
        else closeLb();                        // 列表已无可开项 ⇒ 这时才该退回网格
      }
      mvNoteLbDelete(wasOn ? it.path : "");    // V3.0：撤销时据此把这张重新打开（非大图删除则清空标记）
      // 计数交给 applyFilter 统一口径（时间轴下是「N 天 · M 项」，不能被这里覆盖）
      refreshStatsSoon();
      // 进了系统回收站的文件没法在应用内还原 → 不给「撤销」按钮，免得点了必然失败
      if(delIsSystem() && !(j.fallback>0)) toast(delDoneMsg()+"；如需还原请到资源管理器","ok");
      else toastAction(delDoneMsg(),"撤销",()=>undoDelete([it.path]),"ok");
    }catch(e){ toast("删除失败："+e.message,"err"); }
  }); };
$("#btnOpenFolder").onclick=async()=>{ const it=curItem();
  if(it) recordRecent(it.path, "folder");
  const j=await api("/api/open?p="+encodeURIComponent(it.path),{method:"POST"}).catch(e=>({error:e.message}));
  if(j.error) toast(j.error,"err"); };
$("#btnFav").onclick=()=>{ const it=curItem(); if(!it || S.solo) return;
  toggleFav(it, null, $("#lbCat").value==="__new__"?"默认":$("#lbCat").value); };
$("#lbCat").onchange=async()=>{
  const it=curItem(); if(!it || S.solo) return;
  const sel=$("#lbCat");
  if(sel.value==="__new__"){
    // 立即复位选中值，否则再次选"＋新建分类…"不触发 change
    const opts=[...sel.options].filter(o=>o.value!=="__new__");
    const want=it.favcat||"默认";
    sel.value=(opts.find(o=>o.value===want)||opts[0]||{}).value||"";
    catInputDialog(async name=>{
      /* ε-3b：catInputDialog 现在会在取消 / 被顶掉时以 null 了结（原来直接挂起）。
         没有这一句守卫，null 会被 push 进 extraCats 并真的创建一个名叫「null」的分类。 */
      if(!name) return;
      if(!S.extraCats.includes(name)) S.extraCats.push(name);
      await api("/api/fav",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({op:"add_cat",cat:name})}).catch(()=>{});   // 服务端持久化，重开不丢
      await refreshFavCats();
      fillCatSelect(sel, false);
      sel.value=name;                                // extraCats 已合并，选项必存在
      if(it.fav) await setFavCat(it, name);          // 已收藏：直接改分类
      else toast(`已创建分类「${name}」，点「收藏」即可存入`, "ok");
    });
    return;
  }
  if(it.fav) await setFavCat(it, sel.value);   // 已收藏：切换即改分类
};
$("#btnCopy").onclick=async()=>{
  const it=curItem();
  // 复制「正向提示词」正文 —— 取自按钮自身的 data-cp（由 renderMeta 写入），
  // 绝不能复制 meta.prompt：那是 PNG 内嵌的整份 API 格式工作流 JSON（{"1":{...}}），
  // 用户拿到的就是一堆乱码。
  const t=(document.querySelector("#btnCopy").dataset.cp||"").trim();
  if(!t){ toast("这张图没有可解析的正向提示词","err"); return; }
  /* ε-2：改走 window.mvCopyText（app.js）—— 局域网 http 下 navigator.clipboard 是 undefined，
     裸调用会抛「Cannot read properties of undefined」；带 execCommand 降级后才真能复制。 */
  try{ await (window.mvCopyText ? window.mvCopyText(t) : navigator.clipboard.writeText(t));
    toast("已复制正向提示词","ok");
    flashCopied($("#btnCopy"));                                   // V14：按钮短暂变「✓」
    if(it) recordRecent(it.path,"copy"); }
  catch(e){ toast("复制失败："+e.message,"err"); } };
/* 导出工作流 json（摆好参数、不自动生成）：PNG 内嵌优先，否则模板合成。
   注意：**不再用前端 <a download>**。桌面壳（Tauri / WebView2）没有下载事件接管，
   那种做法点了不会有文件落盘（界面却提示「已导出」，用户找不到文件）。改由后端
   /api/export_save 弹系统「另存为」框直接写盘，web / 桌面两端行为一致、位置可控。 */
function tmplDialog(list, cb, okLabel){
  const old=document.getElementById("catInputMask"); if(old) old.remove();
  const m=document.createElement("div"); m.id="catInputMask";
  const ordered=[...list].sort((a,b)=>(b.compatible?1:0)-(a.compatible?1:0));
  m.innerHTML='<div id="catInputBox"><div class="cit">选择 ComfyUI 工作流模板</div>'+
    '<div style="font-size:calc(12px*var(--fsx));color:var(--tx2);margin-bottom:8px">该图未内嵌界面工作流，将以模板布局+本图参数合成（提示词 / seed / 模型 / LoRA / 尺寸）· ✓=可合成</div>'+
    '<select style="width:100%">'+ordered.map(t=>`<option value="${esc(t.id)}">${t.compatible?"✓ ":"· "}${esc(t.name)}</option>`).join("")+'</select>'+
    '<div class="cbtns"><button class="cancel">取消</button><button class="ok">'+esc(okLabel||"导出")+'</button></div></div>';
  document.body.appendChild(m);
  const close=()=>m.remove();
  m.querySelector(".cancel").onclick=close;
  m.querySelector(".ok").onclick=()=>{ const v=m.querySelector("select").value; close(); cb(v); };
  m.addEventListener("click",e=>{ if(e.target===m) close(); });
}
/* 把桥接脚本装进 ComfyUI：装好后回传直接进「你已经开着的那个页面」，不再弹新窗口。
   首次引导做成大弹窗而不是一条小 toast：小提示很容易被划过去，用户回头看到的还是
   「又弹了个新窗口」，等于没引导。 */
function installBridge(){
  const dlg=bigDialog("让回传直接送进你正开着的 ComfyUI");
  dlg.body.innerHTML=
    '<div style="font-size:var(--fs-ui);line-height:1.75;color:var(--tx)">'
    +'<p style="margin:0 0 12px">开启「桥接」后，回传<b>不再另开一个浏览器窗口</b> —— 工作流直接送进你'
    +'<b>已经开着</b>的那个 ComfyUI 页面，并<b>自动载入成一张新画布</b>；你原来在编辑的工作流不受影响，可以随时切回去。</p>'
    +'<ol style="margin:0 0 14px;padding-left:22px;color:var(--tx2)">'
    +'<li>点下方「<b style="color:var(--tx)">立即安装</b>」，脚本会写进 ComfyUI 的 <code>custom_nodes</code> 目录</li>'
    +'<li><b style="color:var(--warn)">重启 ComfyUI</b>（完全关掉再启动一次）—— 这一步不能省，否则画布那边收不到</li>'
    +'<li>刷新 ComfyUI 页面，之后的回传就直接送达了</li>'
    +'</ol>'
    +'<div style="display:flex;gap:8px;justify-content:flex-end">'
    +'<button id="bridLater">以后再说</button>'
    +'<button class="primary" id="bridGo">立即安装</button></div></div>';
  dlg.body.querySelector("#bridLater").onclick=dlg.close;
  dlg.body.querySelector("#bridGo").onclick=async()=>{
    const btn=dlg.body.querySelector("#bridGo");
    btn.disabled=true; btn.textContent="正在安装…";
    const j=await api("/api/bridge/install",{method:"POST"}).catch(e=>({error:e.message}));
    if(!j.ok){ toast("安装桥接脚本失败："+(j.error||"未知错误"),"err");
      btn.disabled=false; btn.textContent="立即安装"; return; }
    installDoneDialog(j.path);
  };
}
/* 装完之后的大提示：把「必须重启 ComfyUI」说到用户没法忽略 */
function installDoneDialog(path){
  const dlg=bigDialog("桥接脚本已装好 —— 还差最后一步");
  dlg.body.innerHTML=
    '<div style="font-size:var(--fs-ui);line-height:1.75;color:var(--tx)">'
    +'<p style="margin:0 0 8px;font-size:var(--fs-sm);color:var(--tx2)">脚本已写入：</p>'
    +'<div style="font-size:var(--fs-sm);color:var(--tx2);word-break:break-all;'
    +'background:rgba(127,127,127,.13);border-radius:8px;padding:8px 10px;margin-bottom:14px">'
    +esc(path)+'</div>'
    +'<p style="margin:0 0 14px;padding:10px 12px;border-radius:8px;background:var(--warn-bg);'
    +'border-left:3px solid var(--warn)"><span class="ico-in">'+ico("warn")+'</span>现在请<b>完全关闭 ComfyUI，再重新启动一次</b>'
    +'（它只在启动时扫描这个脚本），然后刷新 ComfyUI 页面。之后回传就直接送进你开着的那个页面并自动载入，'
    +'不再弹新窗口、也不用再点确认。</p>'
    +'<div style="display:flex;gap:8px;justify-content:flex-end">'
    +'<button id="bridCopy">复制路径</button>'
    +'<button class="primary" id="bridOk">我知道了</button></div></div>';
  dlg.body.querySelector("#bridCopy").onclick=()=>copyText(path);
  dlg.body.querySelector("#bridOk").onclick=dlg.close;
}
/* 回传 ComfyUI：优先送进已打开的页面（拉取模式），没有可用页面时回落独立窗口注入 */
$("#btnOpen").onclick=async()=>{
  if(!S.curMeta) return;
  const it=curItem(); if(!it) return;
  const send=extra=>api("/api/open_in_comfy",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify(Object.assign({path:it.path},extra))}).catch(e=>({error:e.message}));
  // 模板回退链：PNG 内嵌工作流优先，缺失时用模板 + 本图参数合成
  const run=async(open)=>{
    const j0=await open(null);
    if(j0===true) return;
    if(j0.error && !String(j0.error).includes("模板")){
      toastErr("发送到 ComfyUI 失败",
        j0.error + "\n\n可改用「⬇ 工作流」导出 json，拖入 ComfyUI 窗口加载。"); return; }
    const t=await api("/api/templates").catch(()=>({templates:[]}));
    if(!t.templates||!t.templates.length){ toast("发送失败："+(j0.error||"无可用模板"),"err"); return; }
    tmplDialog(t.templates, tpl=>open(tpl), "发送");
  };
  const st=await api("/api/bridge/status").catch(()=>({pages:[]}));
  const pages=st.pages||[];
  /* ε-7（§6.7 P2-2）：把「上次有件被放弃」告诉用户。
     桥接脚本连试 3 次仍载入失败时会放弃该件（工作流损坏 / ComfyUI 缺自定义节点 /
     前端没起来）。此前它发的是 ack，而 ack 的语义是「已成功载入」⇒ 服务端把「放弃」
     记成「送达成功」：件销毁、无记录、CMV 侧完全不知道，用户以为工作流已经进画布了。
     现在服务端留了账（/api/bridge/status 的 dropped），而「用户正要再发一次」这个时刻
     提示最有意义 —— 修好了就能当场重发。用 localStorage 记到哪个 seq 已提示过，
     同一条不会反复弹。 */
  const seenKey="mv.bridge.droppedSeen";
  const seenSeq=parseInt(localStorage.getItem(seenKey)||"0",10)||0;
  const fresh=(st.dropped||[]).filter(d=>d&&d.seq>seenSeq);
  if(fresh.length){
    localStorage.setItem(seenKey,String(Math.max.apply(null,fresh.map(d=>d.seq))));
    const names=fresh.map(d=>d.name||("工作流 #"+d.seq)).slice(0,3).join("、");
    toastAction(`上次有 ${fresh.length} 件没能载入 ComfyUI（连续 3 次失败已放弃）：`
      +names+(fresh.length>3?" 等":"")
      +"。常见原因是 ComfyUI 缺自定义节点或前端没起来，处理好后可重新发送",
      "重新发送", ()=>$("#btnOpen").click(), "err", 30000);
  }
  if(pages.length){                       // ① 送进已打开的 ComfyUI 页面（不弹新窗口）
    const port=pages.indexOf(st.target)>=0?st.target:pages[0];
    await run(async tpl=>{
      const j=await send({template:tpl,mode:"pull",port});
      if(j.ok){
        // ε-7（§6.7 P2-3）：同一端口开着多个 ComfyUI 页面时，如实说明「落点不可预期」。
        // 服务端只按端口分发，谁先轮询到谁载入 —— 此前用户完全无从理解为什么发到了
        // 一个切在后台的页面里。这里不改变行为，只是把事实说出来。
        const tabN=(st.tabs||[]).filter(x=>String(x.port)===String(port)).length;
        toast(`已送进 :${port} 上打开的 ComfyUI，稍候会作为新画布自动载入；`
          +`你当前在编辑的工作流不受影响`
          +(tabN>1?`（该端口开着 ${tabN} 个页面，工作流会落在先取到的那个）`:""), "ok", 9000);
        recordRecent(it.path,"comfy"); return true; }
      return j;
    });
    // 装了但内容与当前版本不一致：ComfyUI 只在启动时扫一次 custom_nodes，
    // 旧脚本不会自己升级 —— 不提醒的话，新行为等于没送达。
    if(st.stale){
      toastAction("ComfyUI 里的桥接脚本还是旧版，重装一次才能用上新的推送交互（装完记得重启 ComfyUI）",
        "重新安装", installBridge, "ok", 26000);
    }
    return;
  }
  // ② 回落：另开独立窗口注入（原方式）
  await run(async tpl=>{
    toast("正在打开 ComfyUI 并摆好参数…","ok");
    const j=await send({template:tpl});
    if(!j.ok) return j;
    toast("已在 ComfyUI 画布加载（不会自动生成），确认参数后手动 Queue Prompt","ok",11000);
    recordRecent(it.path,"comfy");
    // 到这里说明没检测到在线的 ComfyUI 页面。按真实状态给「下一步」，别让用户瞎试：
    //   没装      → 装一次（并告知装完要重启）
    //   装了没生效 → ComfyUI 没重启，页面压根没加载脚本 —— 重装无用，必须重启
    //   已生效    → 桥接没问题，缺的是「开着并已刷新」的 ComfyUI 页面，直接帮他打开
    const cfUrl="http://127.0.0.1:"+(st.target||"8188")+"/";
    if(!st.installed){
      installBridge();                 // 首次使用：直接给说清楚的大引导，不做小 toast
    }else if(st.active===false){
      toastAction("桥接脚本已装到 ComfyUI，但 ComfyUI 还没重启，页面没加载它 —— 重启后回传就直接送达",
        "复制路径", ()=>copyText(st.installed), "ok", 25000);
    }else if(st.active===true){
      toastAction("桥接已就绪，只是没检测到你开着的 ComfyUI 页面",
        "打开 ComfyUI", ()=>window.open(cfUrl,"_blank"), "ok", 22000);
    }
    return true;
  });
};
$("#btnDlWF").onclick=async()=>{
  if(!S.curMeta) return;
  const it=curItem(); if(!it) return;
  /* 导出走后端「系统另存为框 + 落盘」：桌面壳没有下载接管，旧的 <a download> 不会产生
     文件。后端写盘后回传真实路径，界面据此提示；用户取消则静默（不弹红）。 */
  const save=async tpl=>{
    toast("正在导出工作流…","ok");
    const j=await api("/api/export_save",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({path:it.path, template:tpl})}).catch(e=>({error:e.message}));
    if(j.ok){ toast("已保存工作流："+j.path+"（拖入 ComfyUI 窗口加载）","ok",9000); return; }
    if(j.canceled) return;                                  // 用户取消：不打扰
    toastErr("导出工作流失败", String(j.error||"未知错误"));
  };
  // 先不带模板试一次：内嵌工作流会直接弹保存框；没有则回 need_template
  const j0=await api("/api/export_save",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({path:it.path})}).catch(e=>({error:e.message}));
  if(j0.ok){ toast("已保存工作流："+j0.path+"（拖入 ComfyUI 窗口加载）","ok",9000); return; }
  if(j0.canceled) return;
  if(j0.need_template){
    const t=await api("/api/templates").catch(()=>({templates:[]}));
    if(!t.templates||!t.templates.length){ toast("导出失败："+(j0.error||"无可用模板"),"err"); return; }
    tmplDialog(t.templates, tpl=>save(tpl)); return;
  }
  toastErr("导出工作流失败", String(j0.error||"未知错误"));
};
/* ---------------- 目录选择器（内嵌，替代系统文件夹对话框） ---------------- */
async function dirPicker(){
  const dlg=bigDialog("浏览目录",{wide:true});
  // 固定弹窗高度：目录树自适应撑满剩余空间，底部「取消 / 选择此目录」始终可见
  dlg.box.style.height="min(88vh,calc(720px*var(--fsx)))";
  const body=dlg.body; body.classList.add("dirPick");
  let pathId=0;
  body.innerHTML=
    '<div class="dpaddr"><input id="dpPath" placeholder="粘贴或输入完整路径，回车跳转" spellcheck="false" autocapitalize="off">'
    +'<button id="dpGo">打开</button></div>'
    +'<div class="dpcrumbs" id="dpCrumb"></div>'
    +'<div class="dpquick" id="dpQuick"></div>'
    +'<div class="dptree" id="dpTree"><div class="dpempty">加载中…</div></div>'
    +'<div class="dpfoot"><span class="sel" id="dpSel"></span>'
    +'<button id="dpCancel">取消</button><button class="primary" id="dpOK">选择此目录</button></div>';
  const inp=body.querySelector("#dpPath"), crumb=body.querySelector("#dpCrumb"),
        quick=body.querySelector("#dpQuick"), tree=body.querySelector("#dpTree"),
        sel=body.querySelector("#dpSel");
  let cur="", j=null;
  /* E5-2（2026-09-23）：这一行放的是**完整路径**，必然被 ellipsis 截断 —— 挂 data-tip 作出口。
     占位文案不挂（跟可见文字一样的空气泡没意义）。 */
  const setSel=p=>{ sel.textContent=p||"（请在左侧选一个目录）";
    if(p) sel.dataset.tip=p; else sel.removeAttribute("data-tip"); };
  const mkQuick=p=>{
    if(p==null) return "";
    const label=rootDisplayName(p)||p;       // E5-1：UNC 根显示整段 \\server\share
    return `<button class="chp${p===cur?' on':''}" data-p="${esc(p)}">${ico("drive")}${esc(label)}</button>`;
  };
  // 注意：这里不能叫 load——那会遮蔽全局 load()，导致「选择此目录」后应用不重新加载
  async function browseTo(path){
    const t=++pathId;
    const r=await api("/api/browse?path="+encodeURIComponent(path||"")).catch(e=>({error:e.message}));
    if(t!==pathId) return;
    if(r.error){ toast(r.error,"err"); return; }
    cur=r.path||""; j=r;
    inp.value=cur; setSel(cur);
    // 面包屑
    /* E5-1：UNC 必须当成一段原子前缀 —— 此前这里用 split(/[\\/]/) 直接切，`acc` 会从 "server"
       起步，于是拼出来的每一层都是**不存在**的路径，点任何一层面包屑都跳错（详见 splitPathSegs）。 */
    const _sp=splitPathSegs(cur);
    const segs=_sp.segs;
    let acc="";
    if(segs.length){
      if(_sp.unc){ acc=segs[0]; }                       // \\server\share：整段起步
      else if(/^[A-Za-z]:$/.test(segs[0])){ acc=segs[0]+"\\"; }
      else acc=segs[0];
    }
    /* V37 统一构造。⚠️ 顺带修一处闭包 bug：原写法 `a.onclick=()=>browseTo(acc)` 直接引用外层 acc，
       而 acc 每次迭代被覆盖 ⇒ 所有面包屑段点下去都跳到**最深一级**。这里先取出当前段路径 p 再闭包。 */
    const crumbSegs=[];
    segs.forEach((s,i)=>{
      acc = i===0 ? acc : (acc.replace(/[\\/]+$/,"")+"\\"+s);
      const p=acc;
      crumbSegs.push({label:s, cls:"crumb", onclick:()=>browseTo(p)});
    });
    mkCrumbs(crumb, crumbSegs);
    // 快捷栏：盘符 + ComfyUI output + 当前 root
    let q='';
    (r.drives||[]).forEach(d=>{ q+=mkQuick(d); });
    if(r.comfy) q+=`<button class="chp${cur===r.comfy?' on':''}" data-p="${esc(r.comfy)}">${ico("palette")}ComfyUI 出图</button>`;
    if(S.root) q+=mkQuick(S.root);
    // 常用目录
    const roots=await api("/api/roots").catch(()=>({roots:[]}));
    /* E6-1：这是 `browseTo` 里的**第二次 await** —— 此前这里**没有**令牌检查
       （第一次 await，即上面那次 `/api/browse`，是有的）。后果：快速连点两级目录时，
       先发起的那次 `browseTo` 会带着**旧的 `cur` 与旧的 `r`** 从这一步往下覆盖
       快捷栏与目录树，而输入框/面包屑已在上面被更新成新目录 ⇒ 面板自相矛盾：
       面包屑写着 B 目录，树里列的却是 A 目录的子文件夹，点进去就跳到别处。
       判据与第一次 await 之后那次完全一致：过期就**整段放弃**（不是只跳过一半）。 */
    if(t!==pathId) return;
    (roots.roots||[]).forEach(x=>{ if(x!==cur && x!==S.root) q+=`<button class="chp" data-p="${esc(x)}">${ico("history")} ${esc(rootDisplayName(x)||x)}</button>`; });
    quick.innerHTML=q;
    quick.querySelectorAll(".chp").forEach(b=>b.onclick=()=>browseTo(b.dataset.p));
    // 目录树
    let h='';
    if(r.parent) h+=`<div class="dprow back" data-p="${esc(r.parent)}"><span class="ic">${ico("arrowUpLeft")}</span><span class="nm">返回上级</span></div>`;
    if(r.dirs&&r.dirs.length){
      for(const d of r.dirs) h+=`<div class="dprow" data-p="${esc(d.path)}"><span class="ic">${ico("dir")}</span><span class="nm">${esc(d.name)}</span><span class="n">${d.n??''} 张</span></div>`;
    } else if(!r.parent && !(r.dirs&&r.dirs.length)){
      h=`<div class="dpempty">${r.path?'此目录为空或无子文件夹':'请从上方盘符或快捷入口选择'}</div>`;
    }
    tree.innerHTML=h;
    tree.querySelectorAll(".dprow").forEach(row=>{
      row.onclick=()=>{ browseTo(row.dataset.p); };       // 单击进入（树切换视图），符合直觉
      row.ondblclick=()=>{ browseTo(row.dataset.p); };    // 双击同样进入
    });
  }
  // 交互
  body.querySelector("#dpGo").onclick=()=>{ const p=inp.value.trim(); if(p) browseTo(p); };
  inp.addEventListener("keydown",e=>{ if(e.key==="Enter"){ const p=inp.value.trim(); if(p) browseTo(p); } });
  body.querySelector("#dpCancel").onclick=dlg.close;
  body.querySelector("#dpOK").onclick=async()=>{
    const p=sel.textContent; if(!p||p.startsWith("（")){ toast("请先选择一个目录","err"); return; }
    const r=await (window.CMVClassic?CMVClassic.switchRoot(p):api("/api/root",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({path:p})})).catch(e=>({error:e.message}));
    if(!r)return;
    if(r.error){ toast(r.error,"err"); return; }
    toast("已切换到 "+r.root,"ok");
    dlg.close();
    load("");                        // 全局 load：按新 root 重新拉列表并刷新常用目录
  };
  browseTo(S.path||S.root||"");
}

/* 返回上一级（顶栏按钮与空文件夹里的卡片共用） */
function goUp(){
  const rt=S.root.replace(/[\\/]+$/,"");
  let up=S.path.replace(/[\\/]+$/,"");
  if(up.length<=rt.length) return;                       // 已在根目录：无上级可去
  up=up.replace(/[\\/][^\\/]+$/,"");
  if(/^[A-Za-z]:$/.test(up)) up+="\\";                   // 盘符根补反斜杠（否则相对解析会出错）
  if(up.replace(/[\\/]+$/,"").length>=rt.length) load(up);
}
$("#btnUp").onclick=goUp;
/* H2（2026-10-07）幽灵控件清零：`#filter` / `#favcat` 两个**从不显示**的纯幽灵已从 DOM
   删除，它们的 onchange 绑定随之删除；`#wfClass` / `#markState` 两个**仍会出现在
   特定视图里**的控件退化为**只读视图** —— 值由 syncFiltersUI() 推下来，改动只回写状态。
   四个控件的状态与写入入口统一在 filters.js 的
   setTypeFilter / setFavCatFilter / setWfClassFilter / setMarkStateFilter。 */
$("#sort").onchange=applyFilter;
/* 左栏「时间范围」下拉此前**没有任何 change 监听**，选了不生效（V2.8.5 发现并补上）。
   dateCutoff() 一直在 applyFilter 里被调用，缺的只是这一行绑定。 */
$("#dateRange").onchange=applyFilter;
$("#fit").onclick=()=>{ const on=document.body.classList.toggle("cover");
  $("#fit").textContent=on?"缩略图：裁切":"缩略图：适应";
  try{ localStorage.setItem("mv.fit", on?"1":"0"); }catch(_){} };   /* ε-11 §6.4：落盘（抽屉里此前唯一不持久化的设置） */
$("#wfClass").onchange=()=>setWfClassFilter($("#wfClass").value);
$("#markState").onchange=()=>setMarkStateFilter($("#markState").value);
$("#lbMark").onchange=()=>{ const it=curItem();
  if(it && !S.solo) setMark(it, $("#lbMark").value); };
$("#rec").onchange=()=>load(S.path);
$("#qmode").onchange=()=>{ $("#q").placeholder=$("#qmode").value==="prompt"
  ?"搜索提示词/模型/LoRA…（支持 tag: fav: type: 等语法）"
  :"搜索文件名…（支持 tag: model: size: 等语法）";
  /* 2026-10-07 修复（切搜索范围会抛 ReferenceError）：
     此处原写 `pTok++`，而 `pTok` 早在 ε-6 E6-4 就已并入 state.js 的 `viewGen`
     （见 filters.js「提示词搜索」段上方的迁移注释）⇒ 该标识符**已不存在**，
     于是用户一切换「提示词/文件名」就抛 `ReferenceError: pTok is not defined`，
     **后面的 applyFilter() 根本不执行** —— 表现为「切换后界面没反应」（静默失效）。
     ⚠️ 之所以长期没被发现：默认值原为「文件名」，大多数人从不切换；
        2026-10-07 把默认改成「提示词」后，切回「文件名」成了必经操作才浮出来。
     改用 `viewGen++` 保留原意：作废在途的旧搜索/旧加载，再重算视图。 */
  viewGen++; applyFilter(); };
let _qT=null;                                        // 搜索输入防抖，避免逐键触发请求/重排
$("#q").oninput=()=>{ clearTimeout(_qT); _qT=setTimeout(applyFilter, 220); };
document.addEventListener("keydown",e=>{
  if(!lb.classList.contains("on")) return;
  if(document.getElementById("catInputMask")||document.querySelector(".bigMask")) return;      // 弹窗优先，快捷键不穿透
  const tag=e.target.tagName;
  if(tag==="INPUT"||tag==="SELECT"||tag==="TEXTAREA") return;
  if(e.key==="Escape") closeLb();
  if(e.key==="ArrowLeft") step(-1);
  if(e.key==="ArrowRight") step(1);
  if(e.key==="Delete" && !e.repeat) $("#btnDel").click();
  if(e.key==="p"||e.key==="P"){                        // 隐藏/显示详情面板（P；不再占用 Tab，键盘可正常遍历）
    e.preventDefault(); toggleSide(); return;
  }
  if(e.key==="f"||e.key==="F"){                       // 收起/展开底部预览栏（F）
    e.preventDefault(); toggleFilm(); return;
  }
  if(e.key==="t"||e.key==="T"){                       // 聚焦标签输入(T)
    const inp=document.getElementById("tagInput");
    if(!inp){ renderTagPanel(); }
    const inp2=document.getElementById("tagInput");
    if(inp2) inp2.focus();
  }
  const v=stage.querySelector("video");               // 视频快捷键
  if(!v) return;
  if(e.key===" "){ e.preventDefault(); if(v.paused) v.play(); else v.pause(); }   // B1：空格恢复为播放/暂停切换（与 #vPlay 同语义）
  if(e.key===",") $("#vFrameL").click();
  if(e.key===".") $("#vFrameR").click();
  if(e.key==="l"||e.key==="L") $("#vLoop").click();
  if(e.key==="m"||e.key==="M") $("#vMute").click();
});
/* B1b（2026-10-09）：视频**播放中**点画面 = 暂停。
   为什么需要这一条：中央 #lbPlay 现在只在暂停态露面（播放中收起，别挡画面），
   于是「播放中想暂停」必须有个直觉入口 —— 点画面就是所有播放器的默认动作。
   「画面」取 **#stage 整块**（用 contains 把 <video> 自身与其后代一起算进来）——
   只判 `target===stage` 接不住平铺的视频：那一下 target 是 video，当年也是因此
   「点画面没反应」。
   暂停态**保持旧行为不动**：点空白关大图、点视频无反应（老习惯不改）。
   播放中想直接关图，用右上角关闭按钮或 Esc（两者都不受影响）。 */
lb.addEventListener("click",e=>{
  const v=stage.querySelector("video");
  if(v && !v.paused && (e.target===lb || stage.contains(e.target))){
    v.pause(); return;                                 // 播放中：先暂停，不关图
  }
  if(e.target===lb||e.target===stage) closeLb();
});

