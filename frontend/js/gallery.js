/* ComfyMediaViewer · js/gallery.js（2026-09-09 从 index.html 拆出，加载顺序与原文一致） */
"use strict";
/* 返回上级卡片：非根目录时固定排在网格首位（不放进 S.view，避免影响大图翻页/批量选择） */
function needBackCard(){
  return !favMode && !!S.path && !!S.root && S.path!==S.root;
}
function backCard(){
  const d=document.createElement("div");
  d.className="card dir";
  d.dataset.tip="返回上一级";
  d.innerHTML='<div class="fic">'+ico("arrowUpLeft")+'</div>'
    +'<div class="dn">返回上级</div><div class="dc">回到上一级目录</div>';
  d.onclick=goUp;
  return d;
}

/* 空状态图标（V2.9.2 · U2）：16 网格线性图标，stroke=currentColor 随主题变色、随 --fsx 缩放。
   用内联 SVG 而不是字符/emoji，避免不同系统字体渲染不一致（U6 图标体系统一的第一批）。 */
/* 空状态图标（V4 批次 4）：4 个内联 <svg> 已收编进 ICO，这里只留映射，
   与全站同一套 16 网格线性图标（stroke-width:1.5 · currentColor · 随 --fsx 缩放）。 */
const EMPTY_IC={
  folder:()=>ico("dir"), filter:()=>ico("filter"),
  search:()=>ico("search"), star:()=>ico("star")
};
/* 空状态四件套：图标 + 主文案 + 副文案 + 行动按钮（V2.9.2 · U2） */
function emptyState(title, sub, icon, btn){
  const d=document.createElement("div"); d.className="empty";
  d.innerHTML='<span class="em-ic">'+icon+'</span>'
    +'<div class="em-t">'+esc(title)+'</div>'
    +(sub?'<div class="em-s">'+esc(sub)+'</div>':"");
  if(btn){
    const w=document.createElement("div"); w.className="em-act";
    const b=document.createElement("button"); b.className="primary";
    b.textContent=btn.label; b.onclick=btn.fn;
    w.appendChild(b); d.appendChild(w);
  }
  return d;
}

/* V18（D3，2026-09-18）：筛选/视图「筛空了」时——到底该说什么文案、给什么按钮。
   此前所有「有内容但被筛光」的情况共用一句「当前筛选条件下没有内容」，
   用户看不出是工作流类型、格式、标签、时间轴分组还是元数据条件把自己卡住了。
   这里按**当前生效的条件优先级**给最贴近的一条，并按需换成对症的清除入口。 */
function emptyStateFor(n){
  const sub="目录下共 "+n+" 项，全部被当前条件过滤掉了";
  /* 1) 工作流类型 / 工作流库分类 —— 左栏「生成参数」区的「类型」组是用户最常点、也最容易被忘的一个 */
  if(libMode==="workflow" || WFK){
    const lbl=WFK ? ({t2i:"文生图",i2i:"图生图",cn:"ControlNet",other:"其他 / 无生成参数"}[WFK]||WFK)
                  : ({full:"有完整工作流",prompt:"只有提示词",none:"无生成参数"}[S.filters.wfClass]||"有生成参数");
    return emptyState("没有符合条件的生成参数",
      "当前限定「"+lbl+"」，"+sub, EMPTY_IC.filter(),
      {label:"清除全部筛选", fn:clearAllFilters});
  }
  /* 2) 格式筛选（左栏「格式」分区是多选框） */
  if(typeof FMTS!=="undefined" && FMTS && FMTS.size){
    return emptyState("该格式下没有内容",
      "当前限定 "+[...FMTS].map(f=>fmtLabel(f)).join(" / ")+"，"+sub, EMPTY_IC.filter(),
      {label:"清除全部筛选", fn:clearAllFilters});
  }
  /* 3) 标签视图：标签筛选生效但结果为空。
     ⚠️ F4 P2-10：这里被筛的是 **S.tagView**（跨目录的标签结果集），不是当前目录 ——
     原先沿用 sub 里那句「目录下共 N 项」是把两套口径混用（n 取自 items+dirs）。 */
  if(tagFilterActive){
    const tn = (typeof S.tagView !== "undefined" && S.tagView) ? S.tagView.length : 0;
    return emptyState("没有同时满足这些标签的媒体",
      "标签结果共 "+tn+" 项，全部被当前条件过滤掉了（标签筛选可与目录、类型叠加）",
      EMPTY_IC.filter(), {label:"清除全部筛选", fn:clearAllFilters});
  }
  /* 4) 时间轴视图：分组前就被筛空了 —— 说清是「这条时间范围」还是「分组后为空」 */
  const dr=(document.getElementById("dateRange")||{}).value||"all";
  if(VIEW_MODE==="time" && dr!=="all"){
    return emptyState("该时间范围内没有内容",
      "当前限定「"+({today:"今天",week:"本周",month:"本月"}[dr]||dr)+"」，"+sub, EMPTY_IC.filter(),
      {label:"清除全部筛选", fn:clearAllFilters});
  }
  /* 5) 兜底：普通条件筛空 */
  return emptyState("当前筛选条件下没有内容", sub, EMPTY_IC.filter(),
    {label:"清除全部筛选", fn:clearAllFilters});
}

function renderMore(){
  /* V18 修正（2026-09-19）：S.view 里可能只有**合成导航卡**（子文件夹聚合卡 dirgroup /
     收起文件夹卡 dirback）而没有真内容 —— 那时「筛空了」的判定必须按内容算。
     旧判据 `if(!S.view.length)` 在「目录里有子文件夹 + 文件被格式/参数筛光」时：
     S.view=[{type:"dirgroup"}] 非空 ⇒ 空状态整段不执行 ⇒ 网格里只剩一张「子文件夹」
     卡，底栏却写着「0 项」，用户看到的是一个空网格 + 零解释 + 零出口。
     该目录下**有子文件夹**是很常见的情况（ComfyUI 的 output 按日期/工作流分层），
     所以这个洞会经常被撞到。 */
  const realN = S.view.reduce((a,it)=>a+((it && (it.type==="dirgroup" || it.type==="dirback"))?0:1), 0);
  if(!realN){
    grid.innerHTML="";
    if(favMode){
      /* F4 P2-2：收藏视图「被筛空」与「真的没有收藏」是两回事。原实现一律说
         「还没有收藏」，与实际状态相反（收藏是有的，只是被筛掉了），而且给的
         空状态**没有按钮** ⇒ 用户卡在一个假空状态里没有出口。 */
      const favN = (S.items && S.items.length) || 0;
      if(!favN){
        grid.appendChild(emptyState("还没有收藏",
          "点卡片右上角的星标收藏，或右键星标收藏到指定分类", EMPTY_IC.star()));
        return;
      }
      const qEl = document.getElementById("q");
      const q = qEl ? (qEl.value || "").trim() : "";
      if(q){
        grid.appendChild(emptyState("没有匹配「"+q+"」的收藏",
          "收藏里共 "+favN+" 项，全部被当前条件过滤掉了",
          EMPTY_IC.search(), {label:"清除全部筛选", fn:clearAllFilters}));
        return;
      }
      grid.appendChild(emptyState("没有符合条件的收藏",
        "收藏里共 "+favN+" 项，全部被当前筛选条件过滤掉了",
        EMPTY_IC.filter(), {label:"清除全部筛选", fn:clearAllFilters}));
      return;
    }
    const back=needBackCard();
    if(back) grid.appendChild(backCard());
    /* 子文件夹聚合卡是进这些子目录的**唯一入口**（普通浏览下子文件夹不单列卡片），
       所以即使本目录文件被筛光也要把它留下 —— 否则用户既没了内容、也没了导航，
       只剩一个死网格。这一条与下面的空状态文案并存：卡片在上、说明在下。 */
    for(let i=0;i<S.view.length;i++)
      if(S.view[i] && S.view[i].type==="dirgroup") grid.appendChild(card(i));
    const hasAny=(S.items&&S.items.length) || (S.dirs&&S.dirs.length);
    if(hasAny){
      // 目录里其实有东西，只是被筛选条件 / 搜索词过滤光了
      const qEl=document.getElementById("q");
      const q=qEl?(qEl.value||"").trim():"";
      const n=(S.items?S.items.length:0)+(S.dirs?S.dirs.length:0);
      if(q){
        grid.appendChild(emptyState("没有匹配「"+q+"」的结果",
          "目录下共 "+n+" 项，全部被当前条件过滤掉了",
          EMPTY_IC.search(), {label:"清除全部筛选", fn:clearAllFilters}));
        return;
      }
      /* V18（D3）：不再一律说「当前筛选条件下没有内容」——先认出是哪一类条件筛空的，
         文案与按钮对症下药（实测：此前「工作流类型 / 格式」筛空与「模型 / 尺寸」筛空
         长得一模一样，用户不知道从哪退回去）。 */
      grid.appendChild(emptyStateFor(n));
      return;
    }
    const es=emptyState("此目录没有媒体文件",
      "换一个目录，或勾选左栏「含子文件夹」把子目录里的图一起显示", EMPTY_IC.folder());
    if(typeof openRootMenu==="function"){       // 顺手给个换目录的入口
      let w=es.querySelector(".em-act");
      if(!w){ w=document.createElement("div"); w.className="em-act"; es.appendChild(w); }
      const b=document.createElement("button"); b.className="secondary";
      b.textContent="选择文件夹…"; b.onclick=()=>openRootMenu();
      w.appendChild(b);
    }
    grid.appendChild(es);
    return;
  }
  /* 返回上级卡同样只在第 1 页：分页翻到后面时首格应该是内容，而不是导航卡 */
  if(S.shown===0 && needBackCard() && (FLOW || PAGE===1)) grid.appendChild(backCard());
  /* 分页模式：一页已切好，整页一次渲染；无限瀑布流：每批 BATCH 张，滚到底由哨兵继续调用 */
  const end = FLOW ? Math.min(S.shown+BATCH, S.view.length) : S.view.length;
  /* V53（批次 G3）：一批卡片攒进 fragment 一次插入 —— 一批 BATCH=100 张以前要
     appendChild 100 次、各触发一次样式失效；fragment 合并成 1 次。
     滚动实测本就无掉帧（p95 16.7ms），本条定位是省续载时的样式重算，不是救火。 */
  const frag=document.createDocumentFragment();
  for(let i=S.shown;i<end;i++) frag.appendChild(card(i));
  grid.appendChild(frag);
  S.shown=end;
  if(FLOW && S.shown===S.view.length && CMVQuery.active && sent.getBoundingClientRect().top<innerHeight+1600) void CMVQuery.loadMore();
  // 若 sentinel 仍在预载范围内（视口+1600px），继续分帧渲染，避免无限滚动断层
  const r=sent.getBoundingClientRect();
  if(S.shown<S.view.length && r.top < innerHeight+1600)
    requestAnimationFrame(renderMore);
  gridSyncRoving();                                  // V16①：渲染后保证「唯一入口」存在
}

/* 把 S.view 补齐渲染到（含）下标 i —— 供时间轴索引栏跳转用。
   瀑布流是分批渲染的（每批 BATCH 张，滚到底才续），而索引栏列出的是**全量**分组，
   于是靠后的组头还没进 DOM，直接 querySelector 找不到 → 以前会误报「该分组不在当前页」。
   这里同步补齐（fragment 一次插入，避免逐张 append 反复触发布局），跳转才真正到位。
   注意：跨度大时（几千张）同步补齐会长时间占住主线程，见 tlFillAsync。 */
function renderUpTo(i){
  if(!S.view.length) return;
  if(S.shown===0) renderMore();                 // 首屏让 renderMore 走既有逻辑（返回上级首卡等）
  if(S.shown>i) return;
  const end=Math.min(i+1, S.view.length);
  const frag=document.createDocumentFragment();
  for(let k=S.shown;k<end;k++) frag.appendChild(card(k));
  grid.appendChild(frag);
  S.shown=end;
  gridSyncRoving();                                  // V16①：同上
}

/* ---------- G2 · V16①：网格 roving tabindex（2026-09-18） ----------
   实测（dev/g2_probe.py T1/T2）：卡片此前 **0 张可 Tab**，焦点在卡片上按 Tab 会直接跳到
   `#hintTrig` ⇒ 网格整体被键盘跳过，「逐张浏览」根本走不通。
   做法是 WAI-ARIA 的 roving tabindex：网格里**始终只有一张**卡 tabindex=0（Tab 的唯一入口），
   其余 -1；进入后用方向键在卡之间移动，Enter/Space 等同于点击。
   ⚠️ perRow 必须按**当前所在行**的 offsetTop 分组来数，不能用「第一行有几张」——
      时间轴视图里组头 `.tl-full` 占满整行、列表视图一行只有一张，两种情况首行都会算错列数。
   ⚠️ 复杂度要 O(1)：卡片在 `card()` 里已经默认 `tabIndex=-1`，这里只切「唯一入口」两个节点；
      否则每次瀑布流续载（几千张）都要把全部子元素重写一遍 tabIndex。 */
let gridCur=null;
const GRID_ITEM = el => !!(el && el.classList && el.classList.contains("card"));
function gridSyncRoving(target){
  const items=grid.children; if(!items.length) return;
  /* ⚠️ target 由调用方传入（方向键要把焦点送去的那张）。
     首轮门禁 B8 抓到：调用方若先改 gridCur 再调本函数，「旧入口降回 -1」那一步
     读到的就已经是新卡片了 ⇒ 永远不执行 ⇒ 同时出现多个 tabindex=0。 */
  let cur = (target && target.parentNode===grid && GRID_ITEM(target)) ? target
          : (gridCur && gridCur.parentNode===grid && GRID_ITEM(gridCur)) ? gridCur : null;
  if(!cur){ for(const el of items){ if(GRID_ITEM(el)){ cur=el; break; } } }
  if(!cur) return;
  if(gridCur && gridCur!==cur && gridCur.tabIndex===0) gridCur.tabIndex=-1;
  if(cur.tabIndex!==0) cur.tabIndex=0;
  gridCur=cur;
}
/* 骨架兜底计时器（F4 P2-4）：原先**每张**未就绪的卡各挂一个 6s setTimeout ——
   续载一批 100 张就是 100 个待触发 timer，峰值可上千，且各自闭包持有卡片节点。
   改成**全局单例 + 待摘集合**：第一个挂起时启动计时器，6s 后把窗口内所有仍挂着
   .cmv-ld 的卡一次性摘掉。load/loadeddata/error 仍是主路径，本计时器只在三者
   全丢时兜底 ⇒「提前摘」对已加载完的卡无影响。 */
const _ldPend = new Set();
let _ldTimer = 0;
function ldQueue(d){
  _ldPend.add(d);
  if(_ldTimer) return;
  _ldTimer = setTimeout(()=>{
    _ldTimer = 0;
    const batch = [..._ldPend]; _ldPend.clear();
    for(const x of batch) if(x.isConnected) x.classList.remove("cmv-ld");
  }, 6000);
}
function ldUnqueue(d){ _ldPend.delete(d); }
/* F4 P2-5：只从当前卡向左右扫到 offsetTop 变化为止（列数有界），
   不再对**全部**卡片逐个读 offsetTop —— 后者在几千张卡的瀑布流里
   每按一次方向键都触发一遍全量强制同步布局（layout thrash）。 */
function gridRowSpan(items, el){
  const t=el.offsetTop;
  const i=items.indexOf(el); if(i<0) return 1;
  let n=1;
  for(let k=i-1;k>=0;k--){ if(items[k].offsetTop!==t) break; n++; }
  for(let k=i+1;k<items.length;k++){ if(items[k].offsetTop!==t) break; n++; }
  return n;
}
grid.addEventListener("keydown",e=>{
  const a=document.activeElement;
  if(!grid.contains(a) || !GRID_ITEM(a)) return;
  if(e.target.closest && e.target.closest("input,select,textarea")) return;
  const items=[...grid.children].filter(GRID_ITEM);
  const i=items.indexOf(a); if(i<0) return;
  let n=-1;
  if(e.key==="ArrowRight")      n=i+1;
  else if(e.key==="ArrowLeft")  n=i-1;
  else if(e.key==="ArrowDown")  n=i+gridRowSpan(items,a);
  else if(e.key==="ArrowUp")    n=i-gridRowSpan(items,a);
  else if(e.key==="Home")       n=0;
  else if(e.key==="End")        n=items.length-1;
  else if(e.key==="Enter"||e.key===" "){ e.preventDefault(); a.click(); return; }
  else return;
  if(n<0 || n>=items.length) return;                // 到边就停，不环绕（列表惯例）
  e.preventDefault();
  gridSyncRoving(items[n]); items[n].focus();
});

/* 分帧补齐（时间轴跨大步跳转专用）：每帧只渲染 TL_STEP 张，避免一次插入上千张卡把页面冻住。
   实测约 0.28ms/张 —— 同步补 1100 张要 318ms，外推 7484 张约 2s。
   job 是令牌：用户中途再点别的日期时，旧任务作废（否则会在错误位置滚动）。 */
const TL_STEP = 120;             // 每帧张数（≈34ms：略超一帧，但总时长更短；仍是可响应的小块）
let TL_JOB = 0;
function tlFillAsync(target, job, done){
  const step=()=>{
    if(job!==TL_JOB) return;
    if(S.shown>target){ done&&done(); return; }
    renderUpTo(Math.min(S.shown+TL_STEP, S.view.length-1));
    requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function card(i){
  const it=S.view[i];
  const d=document.createElement("div"); d.className="card";
  d.setAttribute("role","listitem");            // G2·V41：#grid 是 role=list，子项必须成对出现
  d.tabIndex=-1;                                // G2·V16①：默认不进 Tab 序，由 roving 指定唯一入口
  if(it.type==="dir"){
    d.classList.add("dir");
    d.innerHTML=`<div class="fic">${ico("dir")}</div><div class="dn" data-tip="${esc(it.name)}">${esc(it.name)}</div>`
      +`<div class="dc">${it.n} 个文件</div>`;
    d.onclick=()=>load(it.path);
    d.oncontextmenu=e=>{
      e.preventDefault(); e.stopPropagation();
      popupMenu(e.clientX,e.clientY,[
        {label:"打开", icon:"dir", fn:()=>load(it.path)},
        {label:"复制路径", fn:()=>copyText(it.path)},
      ]);
    };
    return d;
  }
  if(it.type==="dirgroup"){
    d.classList.add("dir");
    d.innerHTML=`<div class="fic">${ico("dir")}</div><div class="dn">子文件夹</div>`
      +`<div class="dc">${it.n} 个文件夹 · 点击展开</div>`;
    d.onclick=()=>{ S.expandDirs=true; applyFilter(); };
    return d;
  }
  if(it.type==="dirback"){
    d.classList.add("dir");
    d.innerHTML=`<div class="fic">${ico("x")}</div><div class="dn">收起文件夹</div>`
      +`<div class="dc">显示本目录的文件</div>`;
    d.onclick=()=>{ S.expandDirs=false; applyFilter(); };
    return d;
  }
  if(it.type==="tlhead"){                       // 时间轴组头：占满整行 + 吸顶
    d.classList.add("tl-full","tl-head");
    d.dataset.gi=String(it.gi);
    d.dataset.tip=it.collapsed?"展开这一组":"折叠这一组";
    /* 整行都能点：点一下就折叠/展开（里面那个折叠箭头只是指示器，不做独立按钮，
       否则会与整行的 onclick 冒泡叠加成「点了没反应」） */
    d.innerHTML='<span class="tl-rail" aria-hidden="true"></span>'
      +'<span class="tl-ttl">'+esc(it.title)+'</span>'
      +'<span class="tl-sub">'+esc(it.sub||"")+'</span>'
      +'<span class="tl-tgl" aria-hidden="true">'
        +ico(it.collapsed?"caretRight":"caretDown")+'</span>';
    d.onclick=()=>{ TL_COLLAPSED[it.gkey]=!it.collapsed; applyFilter(); };
    return d;
  }
  if(it.type==="tlmore"){                       // 组内超出 TL_CAP 的尾巴
    d.classList.add("tl-full","tl-more");
    d.textContent="展开全部（还有 "+it.n+" 张）";
    d.onclick=()=>{ TL_SHOWN[it.gkey]=true; applyFilter(); };
    return d;
  }
  let inner;
  if(it.type==="image"){
    // 缩略图生成失败（损坏文件等）时回退到原图，仍失败才标红
    // 图片直接挂 src（原生 loading=lazy 兜底视口外延迟）：data-src 懒加载在
    // 切换搜索模式/筛选重建网格时会让所有卡片先渲染成空框再逐个补图，肉眼可见闪烁
    const fp=fileURL(it.path, it);
    const t1=S.hasThumb?thumbURL(it):fp;
    const t2=S.hasThumb?thumbURL(it,2):fp;
    /* V44：srcset 1x/2x（无缩略图时 t1==t2 不发 srcset，onerror 兜底不变）。
       V45：decoding=async 解码让出主线程；一批的前 6 张 fetchpriority=high 抢首屏带宽
       （i 是 S.view 下标 ⇒ 只落在首个渲染批，续载批不受影响）。
       P0-6（审计第 3 级）：onerror 回退前**必须先清掉 srcset 属性** ——
       srcset 的存在会让浏览器按候选集重选，赋了 this.src=原图也压不过它，
       currentSrc 仍指失败的 /api/thumb ⇒ 整屏永久停在错误占位（已实测 8/8）。 */
    inner=`<img ${CMVThumbs.attributes(t1,t2)} data-fb="${fp}" loading="lazy" decoding="async"${i<6?' fetchpriority="high"':""} `
      +`onerror="if(!this.dataset.fbd){this.dataset.fbd=1;this.removeAttribute('srcset');this.src=this.dataset.fb}else{this.classList.add('err')}">`;
  }else{
    // 视频懒加载：先不挂 src，进入视口再由 vObs 补上，避免几百个视频同时占连接。
    // V47（2026-10-08）：`#t=0.001` 让浏览器解出**真首帧**而非黑框 —— 光靠
    // preload="metadata" 只拿元数据、不画帧（用户看到的就是「黑底 + 播放角标」）。
    // 0.001 是媒体片段 seek 起点（0 帧在部分编码器下是空白关键帧，见 settings.js 同款说明）。
    // ⚠️ 片段参数必须写在 **data-src** 上（vObs 是直接把 dataset.src 赋给 v.src 的），
    //    写成 src 属性会被 vObs 的 `!v.src` 判断当作「已挂载」而永不补 src。
    inner=`<video preload="metadata" muted playsinline data-src="${fileURL(it.path, it)}#t=0.001"></video>`;
  }
  const ext=(it.name.split(".").pop()||"?").toUpperCase();
  /* 尺寸就地渲染：渲染时 metaMap 已加载就直接填角标，翻页/瀑布流续载的卡片不再依赖
     patchCardDim 的一次性回填（旧逻辑只补首屏，后面翻页全是空） */
  const mm0=S.metaMap && S.metaMap[it.path];
  const dimTxt=(mm0&&mm0.w&&mm0.h)?(mm0.w+"×"+mm0.h):"";
  const tagList=it.tags||[];
  const tagsH=tagList.slice(0,2).map(t=>`<span class="ct ${tagClass(t)}">${esc(t.split("/").pop())}</span>`).join("")
    +(tagList.length>2?`<span class="ct more">+${tagList.length-2}</span>`:"");
  d.innerHTML=inner
    +`<span class="wicon"><b>${esc(ext)}</b><i data-dim>${esc(dimTxt)}</i></span>`
    +(it.type==="video"?'<span class="badge" data-tip="视频">'+ico("play")+'</span>':"")
    +(it.type==="video"&&!it.playable?'<span class="badge" data-tip="浏览器无法直接播放">!</span>':"")
    +(it.fav?`<span class="fcat" data-tip="收藏分类">${esc(it.favcat||"默认")}</span>`:"")
    +(MARK_MAP[it.path]?`<span class="mkb" data-tip="已标记：${esc(MARK_LABELS[MARK_MAP[it.path]]||MARK_MAP[it.path])}">${ico("flag")}${esc(MARK_LABELS[MARK_MAP[it.path]]||"")}</span>`:"")
    +`<span class="starcat" data-tip="收藏到分类…">${ico("caretDown")}</span>`
    +`<span class="star${it.fav?" on":""}" data-tip="点击收藏 · 右键选择分类">${
        ico(it.fav?"starFill":"star", it.fav?"filled":"")}</span>`
    /* 网格视图：cap/crdtags 包进悬浮信息层，平时纯图、悬停显示；列表视图用 display:contents 拆开保持列序 */
    +`<div class="cinfo"><div class="cap" data-tip="${esc(it.name)}">${esc(it.name)}</div>`
    +`<div class="crdtags"><span class="tgs">${tagsH}</span>`
      +`<span class="qacts">`
        +`<span class="qa" data-a="dl" data-tip="${dlTipOf(it)}">${ico("download")}</span>`
        +`<span class="qa" data-a="open" data-tip="在资源管理器中打开">${ico("dirOpen")}</span>`
      +`</span>`
      +`<span class="more" data-tip="更多操作">${ico("more")}</span>`
    +`</div></div>`;
  if(it.type==="image") CMVThumbs.observe(d.querySelector("img"));
  if(it.snippet){ const s=document.createElement("span");
    s.className="snip"; s.dataset.tip=it.snippet;
    s.innerHTML=(it.snipIcon?ico(it.snipIcon):"")+'<span>'+esc(it.snippet.slice(0,90))+'</span>';
    d.appendChild(s); }
  if(LIST_VIEW){                                       // 列表视图：追加字段列（列序由 CSS order 决定）
    const mm=S.metaMap && S.metaMap[it.path], loaded=!!S.metaMap;
    /* V15：索引未就绪时这三列给「行内骨架」而不是 `…` —— 读得出是「在加载」。
       `—` 从此刻起只表示「已加载，但这张图没有该字段」。 */
    const dimTxt=(mm&&mm.w&&mm.h)?(mm.w+"×"+mm.h):"—";
    const modelTxt=mm?(mm.model||"—"):"—";
    const sampTxt=mm?(mm.sampler||"—"):"—";
    const SKL='<span class="skl"></span>';
    const dimH=loaded?esc(dimTxt):SKL, modelH=loaded?esc(modelTxt):SKL, sampH=loaded?esc(sampTxt):SKL;
    const capEl=d.querySelector(".cap");
    if(capEl && it.snippet){                           // 相对路径跟在文件名后（灰字）
      const sub=document.createElement("span");
      sub.className="cap-sub"; sub.dataset.tip=it.snippet;
      sub.innerHTML=(it.snipIcon?ico(it.snipIcon):"")+'<span>'+esc(it.snippet.slice(0,90))+'</span>';
      capEl.appendChild(sub);
    }
    /* V10（D3）：每个字段列补 lv-cell —— 列显隐（.lch-off）按「列名」同时作用于表头与数据行，
       靠的就是两边都有这个稳定的锚类（不用 :nth-child，那个在缩略图/星标列缺失时会错位）。 */
    d.insertAdjacentHTML("beforeend",
      '<span class="lv-type lv-cell" data-col="type">'+(it.type==="video"?"视频":"图片")+'</span>'
      +'<span class="lv-dim lv-cell" data-col="dim" data-tip="尺寸">'+dimH+'</span>'
      +'<span class="lv-size lv-cell" data-col="size" data-tip="文件大小">'+esc(fmtSize(it.size||0))+'</span>'
      +'<span class="lv-time lv-cell" data-col="time" data-tip="修改时间">'+esc(fmtTime(it.mtime||0))+'</span>'
      +'<span class="lv-model lv-cell" data-col="model" data-tip="'+esc(modelTxt)+'">'+modelH+'</span>'
      +'<span class="lv-sampler lv-cell" data-col="sampler" data-tip="'+esc(sampTxt)+'">'+sampH+'</span>');
    /* 标签列走元素组装（V4 批次 1：列内要放标记图标，字符串拼不出来） */
    d.appendChild(makeLvTags(it));
  }
  /* ---------- 内容安全遮罩（V2.11.0）----------
     追加一个绝对定位的遮罩层，而不是包裹既有结构 —— 网格和列表两套布局共用一套样式，
     改动面最小（列表视图里靠 CSS 把它收窄到缩略图那一格）。
     δ-2b：遮罩**不再可点**（用户反馈整图随便一点就能揭开、太容易误触）——
     揭开 / 恢复都只走左上角那颗红眼。遮罩现在是纯视觉层（pointer-events:none）。
     只改当前这张的 class，不整表重排（避免滚动位置跳动）。
     批量模式下遮罩 pointer-events:none（见 css/nsfw.css），批量勾选照常能跨过 NSFW 图。

     「显示」模式下一律不挂：那等于关掉这个功能，卡片该是什么样就什么样。
     只有「模糊」模式才有遮罩（「隐藏」模式的图早被 nsfwFilter 从列表里剔掉了）。

    结构（δ-2b 起）：
       .nv-eye   左上角红色眼睛 —— **唯一**入口，揭开/盖回这一张，不影响别的图
       .nsfw-veil  中央「眼睛+× + 已屏蔽」—— 纯视觉层，不可点
     两者都只改本卡的 class（.revealed），不整表重排，也不会引起滚动跳动。
     揭开过的图，打开大图时不再二次模糊 —— 用户已经明确说过要看这张了。 */
  if(isNsfw(it) && NSFW_MODE !== "show"){
    d.classList.add("nsfw");
    const on = NSFW_REVEAL.has(it.path);
    if(on) d.classList.add("revealed");
    const veil=document.createElement("div");
    veil.className="nsfw-veil";
    veil.setAttribute("aria-hidden","true");
    /* ns 字段有分数时顺便把置信度亮出来，帮用户判断「要不要信它」；
       人工纠正过的（ns=-1）没有分数，就不显示这行，免得误导。
       δ-1：默认不显示、卡片 hover 才淡入（见 css/nsfw.css）—— 它是辅助信息，不是首看内容。 */
    const conf=(typeof it.ns==="number" && it.ns>=0)
      ? '<span class="nv-conf">置信度 '+Math.round(it.ns*100)+'%</span>' : "";
    /* δ-1 卡片瘦身（用户截图反馈「元素过多」）：遮罩上从 4 层降到 2 层 ——
       · 删 .nv-rate（红色 NSFW 徽标）：整卡已在模糊 + 遮罩，再标一遍「这是 NSFW」是冗余
       · 删 .nv-show（「显示」按钮）：整块遮罩已改成可点，按钮没必要
       · 补 .nv-icon（大号眼睛）：接替被删徽标的视觉锚点
       · .nv-ttl 文案：「已屏蔽」—— 纯状态描述。旧版写「已屏蔽 · 点此查看」是**动作指令**，
         但遮罩已不可点，再写「点此查看」就是骗人（点了没反应）；怎么打开交给 data-tip 悬停气泡。 */
    /* δ-2b（用户反馈「整图全局任何地方点击都能解开屏蔽有点不妥，很容易误触」）：
       遮罩**不再可点**，解开/恢复都只走左上角那颗红眼。这层阈值保持不变。
       · 文案删掉「点此查看」：既然整层不可点，再写动作指令就是骗人（点了没反应）。
       · data-tip 写在遮罩上，把「怎么打开」交给悬停气泡 —— 不占版面，入口也不会丢。
       · 图标：δ-2b 曾用「眼睛+×」（eyeHide），**δ-2c 按用户要求回到斜杠眼** ——
         与左上角那颗、以及揭开后的状态**共用同一枚 `eyeOff`**（两个状态同形，按下换态）。
       ⚠️ 历史坑：曾让遮罩吃掉第一次点击来揭开，导致大图闸门永不出现；
       现在反过来——遮罩**彻底**不接指针（见 css pointer-events:none）。 */
    veil.innerHTML='<span class="nv-icon">'+ico("eyeOff")+'</span>'
      +'<span class="nv-ttl">已屏蔽</span>'+conf;
    veil.setAttribute("data-tip","点左上角的眼睛单独查看这张");
    d.appendChild(veil);
    const eye=document.createElement("button");
    eye.type="button";
    eye.className="nv-eye"+(on?" on":"");
    eye.setAttribute("data-tip",
      on?"重新遮挡这张（只影响当前会话）":"单独查看这张原图");
    eye.innerHTML=ico(on?"eyeOff":"eye");
    eye.onclick=e=>{ e.preventDefault(); e.stopPropagation();
      if(NSFW_REVEAL.has(it.path)) NSFW_REVEAL.delete(it.path);
      else NSFW_REVEAL.add(it.path);
      nsfwPaintCard(it.path); };
    d.appendChild(eye);
  }
  d.dataset.p=it.path;                                 // 批量模式按路径同步选中态
  if(batchOn && batchSel.has(it.path)) d.classList.add("sel");   // 重渲染后恢复勾选高亮
  const vd=d.querySelector("video"); if(vd) vObs.observe(vd);    // 视频入视口才挂 src
  const im=d.querySelector("img[data-src]"); if(im) vObs.observe(im); // 图片同机制(嵌套滚动容器 lazy 失效)
  const bs=document.createElement("span"); bs.className="bsel"; d.appendChild(bs);
  d.onclick=e=>{
    if(batchOn){                                       // 批量模式：单击=勾选/取消，Shift=范围连选
      const add=!batchSel.has(it.path);
      // 锚点按路径回找下标：applyFilter 重建视图后下标会指到别的文件，连选范围随之错位
      const anchor = lastBatchPath ? S.view.findIndex(x=>x && x.path===lastBatchPath) : lastBatchIdx;
      if(e.shiftKey && anchor>=0 && anchor!==i){
        const a=Math.min(anchor,i), b=Math.max(anchor,i);
        for(let k=a;k<=b;k++){
          const x=S.view[k];
          if(!x || (x.type!=="image"&&x.type!=="video")) continue;
          if(add) batchSel.add(x.path); else batchSel.delete(x.path);
        }
      }else{
        if(add) batchSel.add(it.path); else batchSel.delete(it.path);
      }
      lastBatchIdx=i; lastBatchPath=it.path;
      syncBatchUI(); updateBatchBar();
      return;
    }
    if(e.ctrlKey||e.metaKey||e.shiftKey){               // 非批量模式：Ctrl/Shift 点击 = 直接进入批量并选中
      setBatch(true);
      batchSel.add(it.path); lastBatchIdx=i; lastBatchPath=it.path;
      syncBatchUI(); updateBatchBar();
      return;
    }
    openLb(i);
  };
  const star=d.querySelector(".star");
  if(star) star.classList.add("lv-cell");  // V10：星标列显隐锚点
  S.cardStar.set(it, star);                // 大图页收藏时反查卡片星标
  star.onclick=e=>{
    e.stopPropagation();
    toggleFav(it, star);
  };
  star.oncontextmenu=e=>{                // 右键：弹出分类菜单
    e.preventDefault(); e.stopPropagation();
    showCatMenu(e.clientX, e.clientY, it, star);
  };
  const scat=d.querySelector(".starcat");   // 小箭头：左键也开分类菜单（发现性）
  if(scat) scat.onclick=e=>{
    e.preventDefault(); e.stopPropagation();
    showCatMenu(e.clientX, e.clientY, it, star);
  };
  d.querySelectorAll(".qa").forEach(q=>{    // 悬停快捷操作：下载 / 打开文件夹
    q.onclick=e=>{
      e.preventDefault(); e.stopPropagation();
      if(q.dataset.a==="dl") downloadClean(it);
      else api("/api/open?p="+encodeURIComponent(it.path),{method:"POST"})
        .catch(err=>toast(err.message,"err"));
    };
  });
  d.oncontextmenu=e=>{                      // 右键：完整卡片菜单
    if(batchOn) return;                     // 批量模式下右键留给网格菜单
    e.preventDefault(); e.stopPropagation();
    openCardMenu(e.clientX, e.clientY, it, i);
  };
  /* 标签行已在模板里渲染（.tgs），这里只登记卡片引用供 syncCardTags 回写。
     ⚠️ F4 P2-3：用 WeakMap（S.cardEl）而不是 `it._cardEl` —— 后者在 applyFilter
     重建网格后仍指向已 detached 的旧卡，卡内 <img>/<video> 又被 item 持有，
     反复切目录/改筛选会持续累积。 */
  S.cardEl.set(it, d);
  const moreBtn=d.querySelector(".more");
  if(moreBtn) moreBtn.onclick=e=>{
    e.preventDefault(); e.stopPropagation();
    openCardMenu(e.clientX, e.clientY, it, i);
  };
  /* 缩略图骨架 + 渐显（V2.9.2 · U1）：只有「还没解码完」的媒体卡才挂 .cmv-ld，
     期间骨架呼吸、图片压到透明；load / loadeddata / error 一到就摘掉，两者交叉淡变。
     已缓存（img.complete / video.readyState>=2）的不挂，避免白闪一下骨架。
     JS 没跑到的情形（脚本报错）等于没挂类 = 完全回退现状，不会把图永久藏起来。 */
  if(it.type==="image"||it.type==="video"){
    const md=d.querySelector("img,video");
    if(md){
      const ready=(md.tagName==="IMG")?md.complete:(md.readyState>=2);
      if(!ready){
        d.classList.add("cmv-ld");
        const fin=()=>{ d.classList.remove("cmv-ld"); ldUnqueue(d); };
        md.addEventListener("load", fin, {once:true});
        md.addEventListener("loadeddata", fin, {once:true});
        md.addEventListener("error", fin, {once:true});
        ldQueue(d);              // 兜底：事件万一丢失，6s 后无条件摘除（全局单例）
      }
    }
  }
  CMVGridWindow.observe(d,i,it);
  return d;
}

/* 右键分类菜单：现有分类直接点选，内置新建输入框 */
function showCatMenu(x, y, it, star){
  closeCatMenu();
  const m=document.createElement("div"); m.id="catMenu";
  const title=document.createElement("div"); title.className="cmtitle";
  title.textContent=it.fav?("当前分类："+(it.favcat||"默认")):"收藏到分类：";
  m.appendChild(title);
  const pick=async cat=>{ closeCatMenu();
    await (it.fav ? setFavCat(it, cat) : toggleFav(it, star, cat)); };
  const cur=it.favcat||"默认";
  // 当前分类由行首的 check 图标表示（原来是「✓ 」文字前缀）
  const mk=(label, fn, cls, icon)=>{ const b=document.createElement("div");
    b.className="cmi"+(cls?" "+cls:"");
    b.appendChild(menuCellIcon(icon)); b.appendChild(menuCellText(label));
    b.onclick=e=>{ e.stopPropagation(); fn(); }; m.appendChild(b); };
  mk("默认", ()=>pick("默认"), "", cur==="默认"?"check":"");
  for(const c of catList()){ if(c.name==="默认") continue;
    mk(c.name, ()=>pick(c.name), "", c.name===cur?"check":""); }
  // 新建分类：内联输入框（不再用浏览器 prompt）
  const nb=document.createElement("div"); nb.className="cmi new";
  nb.appendChild(menuCellIcon("plus")); nb.appendChild(menuCellText("新建分类…"));
  const row=document.createElement("div"); row.className="inputrow"; row.style.display="none";
  row.innerHTML='<input placeholder="分类名称"><button>确定</button>';
  const commit=async()=>{ const v=row.querySelector("input").value.trim();
    if(!v) return; closeCatMenu();
    await api("/api/fav",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({op:"add_cat",cat:v})}).catch(()=>{});   // 新分类先持久化
    await pick(v); };
  row.onclick=e=>e.stopPropagation();
  row.querySelector("button").onclick=commit;
  row.querySelector("input").onkeydown=e=>{
    if(e.key==="Enter") commit();
    if(e.key==="Escape") closeCatMenu(); };
  nb.onclick=e=>{ e.stopPropagation();
    nb.style.display="none"; row.style.display="flex";
    row.querySelector("input").focus(); };
  m.appendChild(nb); m.appendChild(row);
  if(it.fav){
    m.appendChild(Object.assign(document.createElement("div"),{className:"cmsep"}));
    mk("取消收藏", ()=>{ closeCatMenu(); toggleFav(it, star); }, "danger", "x");
  }
  document.body.appendChild(m);
  // 定位：避免超出视口
  const r=m.getBoundingClientRect();
  m.style.left=Math.max(4, Math.min(x, innerWidth-r.width-8))+"px";
  m.style.top=Math.max(4, Math.min(y, innerHeight-r.height-8))+"px";
  setTimeout(()=>{                       // 下一次点击/右键任意处即关闭
    document.addEventListener("click", closeCatMenu, {once:true});
    document.addEventListener("contextmenu", closeCatMenu, {once:true});
  }, 0);
}
function closeCatMenu(){ const m=document.getElementById("catMenu"); if(m) m.remove(); }

/* ---------------- 通用小菜单 / 复制 / 下载 / 删除（卡片右键与悬停按钮共用） ---------------- */
/* V14：复制成功的微反馈 —— 触发元素短暂变成功色（只动 color / border-color，不重排）。 */
function flashCopied(el){
  if(!el) return;
  el.classList.remove("copied"); void el.offsetWidth; el.classList.add("copied");
  /* 批次 B：原样式靠 `#lb-top #btnCopy.copied::after{content:" ✓"}` 挂一个**字符**勾
     （CSS 伪元素塞不进 svg）。改成把按钮**已有的图标槽**临时换成 check —— 无图标槽的
     元素（#meta a.cp / .cpv 等）走同一条路径但 `if(svg)` 不成立，行为与原来一致。 */
  const svg = el.querySelector(".iico");
  if(svg && !el._cpIco){ el._cpIco = svg.getAttribute("data-i"); svg.outerHTML = ico("check"); }
  clearTimeout(el._cpT);
  el._cpT = setTimeout(()=>{
    el.classList.remove("copied");
    if(el._cpIco){ const s2 = el.querySelector(".iico"); if(s2) s2.outerHTML = ico(el._cpIco); el._cpIco = null; }
  }, 900);
}
async function copyText(t, el){
  // ε-2：同上 —— 非安全上下文（局域网 http）下 clipboard API 不存在，降级到 execCommand
  try{ await (window.mvCopyText ? window.mvCopyText(String(t)) : navigator.clipboard.writeText(String(t)));
    toast("已复制","ok"); flashCopied(el); }
  catch(e){ toast("复制失败："+e.message,"err"); }
}
/* 菜单行的两格结构：图标格 + 文字格。
   没有图标也留一个等宽空槽 —— 否则「有图标的行」与「纯文字的行」文字起点不齐，
   菜单左边缘会参差不齐。
   文字格用 textContent 而不是 innerHTML：菜单文案里会出现文件名/分类名等外部数据。 */
function menuCellIcon(icon, cls){
  const s=document.createElement("span"); s.className="mi";
  if(icon) s.innerHTML=ico(icon, cls);
  return s;
}
function menuCellText(label){
  const s=document.createElement("span"); s.className="tx"; s.textContent=label;
  return s;
}
function popupMenu(x, y, items){
  closeCatMenu();
  const m=document.createElement("div"); m.id="catMenu";
  for(const it of items){
    if(!it){ const s=document.createElement("div"); s.className="cmsep"; m.appendChild(s); continue; }
    const d=document.createElement("div");
    d.className="cmi"+(it.cls?" "+it.cls:"");
    d.appendChild(menuCellIcon(it.icon, it.iconCls));
    d.appendChild(menuCellText(it.label));
    d.onclick=e=>{ e.stopPropagation(); m.remove(); it.fn(); };
    m.appendChild(d);
  }
  document.body.appendChild(m);
  const r=m.getBoundingClientRect();
  m.style.left=Math.max(6,Math.min(x, innerWidth-r.width-8))+"px";
  m.style.top=Math.max(6,Math.min(y, innerHeight-r.height-8))+"px";
  // 延迟一拍再挂关闭监听：否则「打开菜单的这一次点击」会立刻把菜单关掉
  setTimeout(()=>{
    document.addEventListener("click",closeCatMenu,{once:true});
    document.addEventListener("contextmenu",closeCatMenu,{once:true});
  }, 0);
  return m;
}
/* 下载并抹去元数据（大图页与卡片快捷按钮共用）。
   两条路：设置页配过「下载保存位置」→ 服务端直接落盘到那儿（位置可记，
   桌面端与网页版行为一致）；没配 → 走浏览器的默认下载目录。
   是否真的抹了元数据以响应头 X-Clean-Applied 为准 —— 白名单之外的格式
   （.avi/.mkv 等）后端原样下发，这里就不能再谎报「已抹除」，
   文件名也不加 _clean 后缀（与服务端命名保持一致）。 */
/* 元数据可抹除的格式（与服务端 _STRIP_BY_EXT 对齐）——只用于按钮提示文案：
   白名单外的格式下载的是原文件，按钮就不该写「抹去元数据」（名不副实）。 */
const CLEAN_ABLE = ["png", "jpg", "jpeg", "webp", "mp4", "gif"];
function dlTipOf(it){
  const ext=(it.name.split(".").pop()||"").toLowerCase();
  return CLEAN_ABLE.includes(ext) ? "下载（抹去元数据）" : "下载（原文件）";
}
async function downloadClean(it){
  if(!it) return;
  try{
    if(S.downloadDir){
      const j=await api("/api/save_download",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({path:it.path, clean:true})});
      toast((j.cleaned?"已保存（元数据已抹除）：":"已保存：")+j.dest,"ok",7000);
      recordRecent(it.path,"download");
      return;
    }
    const r=await fetch(fileURL(it.path, it)+"&dl=1&clean=1");
    if(!r.ok){ const j=await r.json().catch(()=>({})); throw new Error(j.error||r.statusText); }
    const cleaned=r.headers.get("X-Clean-Applied")==="1";
    const b=await r.blob();
    const dlName=cleaned ? it.name.replace(/\.([^.]+)$/,"_clean.$1") : it.name;
    const a=document.createElement("a");
    a.href=URL.createObjectURL(b); a.download=dlName; a.click();
    setTimeout(()=>URL.revokeObjectURL(a.href), 30000);
    toast(cleaned?"已下载（元数据已抹除）":"已下载（该格式不支持抹除元数据，是原文件）","ok",cleaned?undefined:6000);
    recordRecent(it.path, "download");
  }catch(e){ toast("下载失败："+e.message,"err"); }
}
/* 关大图时把网格滚到「当前项」（V3.0 · Phase 3 · 2026-10-12）

   原先只靠 closeLb 里的 `lbFocusBack.focus()` 归还焦点，而**删除后那张卡片已经不在
   DOM 里**（视图重建时网格被清空重渲）⇒ 既不聚焦也不滚动 ⇒ 用户关掉大图看到的是
   列表顶部，「刚才删到哪儿了」全丢。这里按当前项（S.view[S.idx]，删除后就是自动
   接替的那张）找卡片并滚到视口中间。

   ⚠️ **懒渲染是这个功能的主战场，不是边角**：视图重建处（filters.js:653 /
      classic-query.js:223）一律 `S.view=…; S.shown=0; grid.innerHTML=""; renderMore();`
      ⇒ 重建后**只有首批 BATCH(=100) 张进了 DOM**。主人真库是几千张的量级：在第 3000
      张上删完再关大图，S.idx≈3000 而 S.shown=100 ⇒ 卡片根本不在 DOM 里。
      （实测取证：门禁 G11b 前半段 `grid.querySelector('[data-p=…]') === null`。）

   ⇒ 必须补齐渲染。但**不能**直接把 `S.shown` 跳到目标下标（renderMore 是按
      `[S.shown, S.shown+BATCH)` 区间追加的，跳过去中间那批永远不渲染，网格里留空洞），
      也**不能**在 closeLb 里同步紧循环补完（几千张卡一次性渲染会把主线程占死一两秒，
      「关个图卡一下」）—— 同文件 tlFillAsync 就是为这个问题存在的，这里沿用同一走法：
      **逐帧推进，命中即滚**。
   ⚠️ MV_REVEAL_STEP 用 let 而非 const：门禁把它调成 1 来强制走满多帧路径
      （6 张的夹具否则一帧就补完了，多帧逻辑等于没测）。 */
let MV_REVEAL_STEP = 240;        // 每帧补齐的张数（≈TL_STEP 量级；越大越快但每帧越重）
let MV_REVEAL_JOB = 0;           // 令牌：新请求作废旧请求

function mvScrollToCurrentCard(){
  const it=(S.view||[])[S.idx];
  if(!it || !it.path || typeof grid==="undefined" || !grid) return false;
  const path=it.path, idx=S.idx;
  const esc=(window.CSS && CSS.escape) ? CSS.escape(it.path) : it.path;
  const sel='[data-p="'+esc+'"]';
  const hit=()=>grid.querySelector(sel);
  const reveal=c=>{ try{ c.scrollIntoView({block:"center"}); }
                    catch(_){ try{ c.scrollIntoView(); }catch(__){} } };   // 老引擎不支持 options

  const c0=hit();
  if(c0){ reveal(c0); return true; }            // 常见情形（小目录）：已渲染，同步滚到位
  if(typeof renderUpTo!=="function" || S.shown>idx) return false;

  const job=++MV_REVEAL_JOB;
  const step=()=>{
    if(job!==MV_REVEAL_JOB) return;                       // 已有更新的请求
    if(lb.classList.contains("on")) return;               // 用户又打开了大图 ⇒ 无需再滚
    /* 当前项必须仍是当初那张 —— 否则（用户又删/又筛选）越补越偏，就地放弃。
       按 path 比对而不是按下标：视图重建后 S.idx 是重新回找出来的。 */
    if(((S.view[S.idx]||{}).path)!==path) return;
    if(S.shown>idx){ const c=hit(); if(c) reveal(c); return; }
    renderUpTo(Math.min(S.shown+MV_REVEAL_STEP-1, idx));   // renderUpTo 的入参是**闭区间**上界
    const c=hit();
    if(c){ reveal(c); return; }
    requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
  return true;
}
window.mvScrollToCurrentCard=mvScrollToCurrentCard;

/* 删除单个文件（卡片右键菜单用；带撤销） */
function deleteOne(it){
  if(!it) return;
  confirmDialog("删除文件", `确定删除「${it.name}」吗？文件将移入${delTipWhere()}。`, async()=>{
    /* V3.0（2026-10-12）：与 #btnDel（nav.js）同口径 —— 大图开着时删掉后接替打开邻居。
       这里**不能**自己调 applyFilter()：purgePaths 会先把卡片淡出 200ms 再统一重建视图，
       所以接替挂在它的 onCommit 里兑现。 */
    const wasOn=lb.classList.contains("on");
    const succ=wasOn ? pickSuccessorPath(curList(), curIdx(), +1) : "";
    try{
      const j=await api("/api/delete",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({path:it.path})});
      if(j.error || j.ok===false){ toastErr("删除失败", String(j.error||"后端未返回原因")); return; }
      noteDeleteMeta(j);                     // §6.1：文件删了、元数据没写回要说出来
      if(wasOn) S.idx=-1;                    // 先复位：视图变短后旧下标可能越界
      purgePaths([it.path], ()=>{
        if(!wasOn) return;
        if(succ) focusAfterViewRebuild(succ);   // 有邻居 → 就地翻过去
        else closeLb();                         // 无可开项 → 才退回网格
      });
      mvNoteLbDelete(wasOn ? it.path : "");     // V3.0：撤销时据此把这张重新打开
      refreshStatsSoon();
      if(window.mvUpdateTrashBadge) window.mvUpdateTrashBadge();   // V2：单张删除后同步顶栏角标
      // 进了系统回收站的文件没法在应用内还原 → 不给「撤销」按钮，免得点了必然失败
      if(delIsSystem() && !(j.fallback>0)) toast(delDoneMsg()+"；如需还原请到资源管理器","ok");
      else toastAction(delDoneMsg(),"撤销",()=>undoDelete([it.path]),"ok");
    }catch(e){ toastErr("删除失败", String(e && e.message || e)); }
  });
}
/* 人工归类（卡片菜单「标为 NSFW / 移出 NSFW」）。
   走服务端的人工纠正表（nsfw_manual.json）—— 重新检测也翻不回去。
   内存里这条要同步两件事：
     it.n  = 新标签（界面立刻反映）
     it.ns = -1  ← 关键。有分数的话前端会拿阈值现场重算，用户明确标过的就被算回去了；
                   置 -1 等于告诉前端「以标签为准，别自己算」（与服务端下发语义一致）。 */
async function setNsfwManual(it, label){
  try{
    const j=await api("/api/nsfw/label",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({path:it.path,label})});
    if(!j || j.ok===false){ toast("归类失败："+((j&&j.error)||"未知原因"),"err"); return; }
    it.n=label; it.ns=-1;
    NSFW_REVEAL.delete(it.path);   // 判定变了，旧的揭开记录作废，按新状态重新呈现
    toast(label==="nsfw" ? "已标为 NSFW（重新检测也不会翻回去）"
                         : "已移出 NSFW（重新检测也不会翻回去）");
    /* 隐藏模式下这张可能刚被筛掉：不把 S.idx 找回来，它就指向别人 ——
       大图里的「删除」走 S.view[S.idx]，是 keepIdxByPath 注释里那个 V2.9.0 P0 的同类
       （审查探针实测：顶栏归类按钮连点两下会把没打开过的另一张写进人工纠正表）。
       与 setNsfwMode 同款守卫，之前只在那里有。 */
    const cur = (S.view && S.idx >= 0 && S.view[S.idx]) ? S.view[S.idx].path : "";
    applyFilter();                 // 隐藏模式下这张会立刻消失 / 出现
    if(cur && !keepIdxByPath(cur) && lb && lb.classList.contains("on")) closeLb();
    /* 大图正开着且操作的就是当前这张 → 眼睛/提示条/顶栏归类按钮三个控件立刻跟上；
       胶片条里那张缩略图的模糊态也不能等下次翻页才变（V2.11.1）。 */
    if(typeof nsfwRefreshLb === "function") nsfwRefreshLb(it.path);
    if(typeof buildFilm === "function" && lb && lb.classList.contains("on")) buildFilm();
    if(typeof nsfwLoadStatus==="function") nsfwLoadStatus(true);   // 设置页计数跟上
  }catch(e){ toast("归类失败："+(e.message||e),"err"); }
}
/* 卡片右键：完整操作菜单 */
function openCardMenu(x, y, it, i){
  const isMedia = it.type==="image" || it.type==="video";
  /* P1-12：菜单可能停留很久（5 秒轮询/自动刷新会重建 S.view），
     而 openLb(i) / setNsfwManual(null) 等入口收的是**下标**。菜单一开就把它
     冻在打开那一刻的 i 上，等点击时列表早已换人 ⇒ 「点 A 菜单、打开 B」。
     所以这里不绑死下标，而是绑 **path**：点击瞬间按 path 在当前列表里反查真下标，
     查不到（那张图已被轮询刷掉 / 已删除）就当这次操作失效并提示，绝不误伤别人。 */
  const at = (fn)=>{
    const k = it.path;
    return ()=>{
      const list = curList();
      const j = list.findIndex(o=>o && o.path===k);
      if(j < 0){ toast("这张图已不在当前列表（列表已刷新）","err"); return; }
      fn(j);
    };
  };
  popupMenu(x, y, [
    {label:"打开大图", icon:"search", fn:at(j=>openLb(j))},
    {label:it.fav?"取消收藏":"收藏", icon:it.fav?"starFill":"star",
     iconCls:it.fav?"filled":"", fn:()=>toggleFav(it, null)},
    {label:"收藏到分类…", icon:"tag", fn:()=>showCatMenu(x, y, it, null)},
    null,
    /* 标记 5 项：行首图标即状态 —— 命中项是 check，其余是 flag（原来是「✓ / ⚑」文字前缀） */
    ...Object.entries(MARK_LABELS).map(([k,v])=>{
      const on = MARK_MAP[it.path]===k;
      return {label:"标记："+v, icon:on?"check":"flag",
              fn:()=>setMark(it, on?"none":k)};}),
    ...(MARK_MAP[it.path]?[{label:"取消标记", icon:"x", fn:()=>setMark(it,"none")}]:[]),
    /* 内容安全：人工归类，双向。当前判定用 nsfwLabelOf()（与卡片遮罩同一口径），
       避免「界面显示 A、菜单里写 B」。模型只自动测图片，但视频也能手动标 ——
       标了照样模糊/隐藏，遮罩和列表过滤都不区分类型。 */
    ...(nsfwOn() && isMedia ? (()=>{
      const cur=nsfwLabelOf(it);
      return [null, cur==="nsfw"
        ? {label:"移出 NSFW", icon:"eye", fn:()=>setNsfwManual(it,"sfw")}
        : {label:"标为 NSFW", icon:"eyeOff", fn:()=>setNsfwManual(it,"nsfw")}];
    })() : []),
    null,
    {label:"复制文件名", icon:"copy", fn:()=>copyText(it.name)},
    {label:"复制完整路径", icon:"copy", fn:()=>copyText(it.path)},
    {label:"复制提示词", icon:"copy", fn:async()=>{
      try{
        const m=await api("/api/meta?p="+encodeURIComponent(it.path));
        const p=(m.parsed&&m.parsed.positive)||"";
        /* E5-7：读失败 ≠ 本来就没有 —— 同一链路上的第二个「会向用户说错话」的点。 */
        if(!p){ toast(m.metaError ? ("元数据读取失败："+m.metaError+"（稍后可重试）")
                                  : "这张图没有可解析的提示词","err"); return; }
        copyText(p); recordRecent(it.path,"copy");
      }catch(e){ toast("读取失败："+e.message,"err"); }
    }},
    null,
    ...(isMedia?[
      {label:dlTipOf(it), icon:"download", fn:()=>downloadClean(it)},
      {label:"在资源管理器中打开", icon:"dirOpen", fn:()=>{
        recordRecent(it.path,"folder");
        api("/api/open?p="+encodeURIComponent(it.path),{method:"POST"})
          .catch(e=>toast(e.message,"err")); }},
      null,
      {label:"删除（移入回收站）", icon:"trash", cls:"danger", fn:()=>deleteOne(it)},
    ]:[]),
  ]);
}

/* P0-5（2026-10-09）：收藏 / 改分类的异步令牌 **按 path 分桶**。
   原先是一个全局单值 `favTok`，只能表达「谁最后发出」——两个**不同文件**的写操作同时在途时，
   只有最后一个响应被采纳，其余在服务端**已经写成功**却在此处提前 return ⇒ 星标不点亮、
   分类角标不出现、左栏收藏计数不刷新、favMode 下漏 refreshKeepCur，界面与数据长期不一致
   （用户以为没收藏上 → 再点一次其实是「取消收藏」，把刚写成功的那次推翻）。
   改 `Map<path, token>` 后：**同一路径**的逆序响应仍被丢弃（原意完整保留），
   **不同路径**的并发互不干扰。条目按 path 累积，量级 = 本次会话动过收藏的路径数，可忽略。 */
const FAV_TOK = new Map();                       // path -> 该路径最近一次写操作的令牌
function newFavTok(path){
  const t = (FAV_TOK.get(path) || 0) + 1;
  FAV_TOK.set(path, t);
  return t;
}
async function toggleFav(it, el, cat){
  const path=it.path, tok=newFavTok(path);
  try{
    if(!el && S.cardStar) el=S.cardStar.get(it) || null;   // 大图页进来也能同步卡片星标
    const j=await api("/api/fav",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({path:path, cat:cat||it.favcat||"默认"})});
    if(FAV_TOK.get(path)!==tok) return;   // **同一路径**又有更新的写操作 ⇒ 本次结果作废（不同路径互不影响）
    /* F4 P2-9：await 期间列表可能被整体重建（5s 轮询 / 切目录 / 改筛选），
       `it` 可能已成孤儿对象 —— 写它界面不会体现。先按 path 找回**当前视图**里的
       同一项再写；找不到才退回 it。 */
    const live=(S.view||[]).find(x=>x && x.path===path) || it;
    live.fav=j.fav; if(j.favcat) live.favcat=j.favcat;
    if(live!==it){ it.fav=j.fav; if(j.favcat) it.favcat=j.favcat; }
    if(el){
      el.classList.remove("pop"); void el.offsetWidth; el.classList.add("pop");   // V14：星标弹一下
      el.classList.toggle("on",j.fav);
      /* 卡片星标是「纯图标」，大图顶栏 #btnFav 是「图标 + 文字」（批次 2 起也走图标，但保留 .ic 槽）。
         两者结构不同，必须分开写 —— 否则图标会把文字冲掉。 */
      if(el.classList.contains("star")) el.innerHTML=ico(j.fav?"starFill":"star", j.fav?"filled":"");
      else setIcoLabel(el, j.fav?"starFill":"star", j.fav?"取消收藏":"收藏",
                        j.fav?"filled":"");
      const cd=el.closest(".card");                      // 即时更新分类角标
      if(cd){
        let b=cd.querySelector(".fcat");
        if(it.fav){
          if(!b){ b=document.createElement("span"); b.className="fcat"; b.dataset.tip="收藏分类";
            /* .cap 被包进 .cinfo 悬浮信息层后成了孙节点，而 insertBefore 只接受
               **直接子节点**（传后代节点抛 NotFoundError，异常被本函数 catch 吞掉 →
               表现为「收藏后角标不出现 + 弹出红色收藏失败提示」）。
               角标仍插在 .cinfo 之前，与首屏 innerHTML 的落位保持一致。 */
            const info=cd.querySelector(".cinfo");
            if(info) cd.insertBefore(b, info); else cd.appendChild(b); }
          b.textContent=it.favcat||"默认";
        }else if(b) b.remove();
      }
    }
    if(favMode || S.filters.type==="fav") refreshKeepCur(it.path);   // 收藏视图：取消收藏即时移出（S.idx 跟着走，防删错）
    /* 顶栏 #btnFav 只该反映**当前正在看的那张**（viewer.js 打开/翻页时也在写它）。
       不加 path 守卫的话，「连点别的卡片星标 / 右键菜单收藏」都会把大图按钮改成别人的状态 ——
       又一处「界面与数据不一致」。 */
    { const ci=curItem();
      if(ci && ci.path===path)
        setIcoLabel($("#btnFav"), live.fav?"starFill":"star", live.fav?"取消收藏":"收藏",
                      live.fav?"filled":""); }
    await refreshFavCats();
    refreshStatsSoon();
    toast(j.fav?`已收藏到「${it.favcat||"默认"}」`:"已取消收藏","ok");
  }catch(e){ toast("收藏失败："+e.message,"err"); }
}

async function setFavCat(it, cat){
  const path=it.path, tok=newFavTok(path);
  try{
    const j=await api("/api/fav",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({path:path, op:"set_cat", cat})});
    if(FAV_TOK.get(path)!==tok) return;   // 同 toggleFav：按 path 判过期，不与其他文件的在途写操作互踩
    const live=(S.view||[]).find(x=>x && x.path===path) || it;
    live.favcat=j.favcat; if(live!==it) it.favcat=j.favcat;
    await refreshFavCats();
    refreshStatsSoon();
    refreshKeepCur(it.path);              // 分类筛选下改分类可能把当前图筛出去 → 必须复位 S.idx
    toast(`分类已改为「${cat}」`,"ok");
  }catch(e){ toast("修改分类失败："+e.message,"err"); }
}

