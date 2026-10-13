/* ComfyMediaViewer · js/settings.js（2026-09-09 从 index.html 拆出，加载顺序与原文一致） */
"use strict";
/* ================= 适配层 v2（新布局交互 · 2026-09-07） ================= */

/* 0. 线性图标填充（顶栏 / 工具条按钮；侧栏列表由各自渲染函数生成） */
(function setupIcons(){
  const map={ "#btnUp .ic":"up", "#btnAR .ic":"refresh",
              "#btnTrash .ic":"trash", "#btnSettings .ic":"gear",   /* V2/V1：回收站 / 设置入口 */
              "#btnOpenImg .ic":"openimg",                            /* V2.10.7：单图查看入口 */
              "#btnRefresh .ic":"refresh",                            /* V2.10.7：手动刷新，与设置页 setRescan 同一实现 */
              "#viewGrid .ic":"grid", "#viewList .ic":"list", "#viewTime .ic":"timeline",
              "#flowPage .ic":"pager", "#flowInf .ic":"masonry",
              "#btnBatch .ic":"batch",
              "#rootBtn .rs-ico":"dir",
              /* ---------- V4 图标批次 2：大图页与对比页（静态槽位） ---------- */
              "#btnClose .ic":"arrowLeft", "#btnFav .ic":"star",
              "#btnOpen .ic":"refresh", "#btnDlWF .ic":"download",
              "#btnOpenFolder .ic":"dirOpen",
              "#navL":"chevronLeft", "#navR":"chevronRight",
              "#filmTgl":"caretDown",
              "#vPlay":"play", "#vLoop":"loop",
              "#vFrameL":"skipBack", "#vFrameR":"skipFwd", "#vMute":"volume",
              "#sideTglX":"chevronRight", "#sideTgl":"chevronLeft",
              "#cmpWipe .ic":"swap", "#cmpExit .ic":"x",
              "#wipeHandle .knob":"swap",
              /* ---------- V4 图标批次 3：弹窗与菜单（静态槽位） ---------- */
              "#btnRestart .ic":"refresh",
              "#hintClose .ic":"x",
              "#btnDupes .ic":"dupe", "#btnStats .ic":"chart",
              /* H5（2026-10-06）：查重 / 出图统计的界面入口从顶栏「工具▾」搬到设置面板
                 「数据」页；隐藏宿主按钮的图标槽（上一行）仍保留，两个新按钮复用同一图标 */
              "#setDupes .ic":"dupe", "#setStats .ic":"chart",
              /* ---------- V4 图标批次 4：左栏与空状态（静态槽位） ---------- */
              "#rootBtn .rs-car":"caretDown", "#leftTgl":"chevronLeft",
              "#tagManage":"gear", "#hintTrig":"keyboard", "#toTop":"up" };
  for(const sel in map){ const el=$(sel); if(el) el.innerHTML=ico(map[sel]); }
  /* 左栏分区标题的折叠箭头：7 个分区各一个槽（原由 CSS 伪元素 content:"▾" 画，
     伪元素塞不进 svg ⇒ 改成真实槽位，展开/收起仍靠 CSS 旋转） */
  document.querySelectorAll("#left h4 .secar").forEach(e=>{ e.innerHTML=ico("caretDown"); });
  /* 批次 B：搜索框左端的放大镜（原由 layout.css 的 .qico::before/::after 两个伪元素手绘） */
  document.querySelectorAll(".qico").forEach(e=>{ e.innerHTML=ico("search"); });
})();

/* 1. 详情面板开关（Tab / 边缘把手） */
let sideHidden=false;
function toggleSide(force){
  sideHidden = (force!==undefined)?force:!sideHidden;
  document.getElementById("side").classList.toggle("hidden", sideHidden);
  setTimeout(centerFilmOn,230);   // 面板滑出动画结束后，当前缩略图重新居中（对齐大图中轴）
}
function centerFilmOn(){          // 当前预览缩略图水平居中（即大图正下方）
  const c=$("#lbFilm .on"); if(c) c.scrollIntoView({block:"nearest",inline:"center"});
}
$("#sideTgl").onclick=()=>toggleSide(false);
$("#sideTglX").onclick=()=>toggleSide(true);

/* 1.5 底部预览栏收起/展开（F 键 / 右下角开关），状态记忆 */
function toggleFilm(){
  const off=$("#lbFilm").classList.toggle("off");
  $("#filmTgl").innerHTML = ico(off?"caretUp":"caretDown");
  $("#filmTgl").dataset.tip = off?"展开预览栏 (F)":"收起预览栏 (F)";
  try{ localStorage.setItem("mv.film", off?"0":"1"); }catch(_){}
}
$("#filmTgl").onclick=toggleFilm;
try{ if(localStorage.getItem("mv.film")==="0"){
  $("#lbFilm").classList.add("off");
  $("#filmTgl").innerHTML=ico("caretUp"); $("#filmTgl").dataset.tip="展开预览栏 (F)";
} }catch(_){}
/* ε-11 §6.4：缩略图「裁切/适应」持久化（mv.fit）—— 抽屉里此前唯一不落盘的设置 */
try{ if(localStorage.getItem("mv.fit")==="1"){
  document.body.classList.add("cover");
  const _fitBtn=$("#fit"); if(_fitBtn) _fitBtn.textContent="缩略图：裁切";
} }catch(_){}
$("#lbFilm").addEventListener("wheel",e=>{          // 纵向滚轮 → 横向滚动预览栏
  if(Math.abs(e.deltaY)>Math.abs(e.deltaX)){ e.preventDefault(); $("#lbFilm").scrollLeft+=e.deltaY; }
},{passive:false});

/* 2. 网格密度滑块（连续调节卡片大小）
      默认 200px = 设计规范 1.2 的 minmax(200px,1fr)；间距在 8~16px 之间随卡片大小线性变化，
      200px 时正好 16px（规范 1.2 的 grid-gap）。选择结果记在 localStorage（mv.dens）。 */
const densRange=$("#densRange");
if(densRange){
  const applyDens=v=>{
    document.documentElement.style.setProperty("--card-min", v+"px");
    document.documentElement.style.setProperty("--grid-gap", Math.max(8,Math.min(16,Math.round(v/12.5)))+"px");
    const lv=$("#densVal"); if(lv) lv.textContent=v+"px";
  };
  let savedDens=parseInt(densRange.value,10)||200;
  try{ savedDens=parseInt(localStorage.getItem("mv.dens")||savedDens,10)||savedDens; }catch(_){}
  if(savedDens>=+densRange.min && savedDens<=+densRange.max) densRange.value=savedDens;
  densRange.oninput=()=>applyDens(+densRange.value);
  densRange.onchange=()=>{ try{ localStorage.setItem("mv.dens", densRange.value); }catch(_){} };
  applyDens(+densRange.value);
  /* V21：密度档位（紧凑 / 舒适 / 宽松）按下时经这里把值灌回滑块。
     滑块是唯一真相、档位只是它的三个快捷键 —— 两套状态各记一份必然互相打架。 */
  window.mvSetDens=v=>{
    if(v<+densRange.min || v>+densRange.max) return;
    densRange.value=String(v);
    applyDens(v);
    try{ localStorage.setItem("mv.dens", String(v)); }catch(_){}
  };
  window.mvGetDens=()=>+densRange.value;
}

/* 视图切换：网格 / 列表 / 时间轴（V2.8.5 三态）。
   LIST_VIEW 仍是**派生值**，其它 5 处读它的地方一行都不用改。 */
(function setupViewToggle(){
  const btns=[$("#viewGrid"),$("#viewList"),$("#viewTime")].filter(Boolean);
  if(btns.length<2) return;
  const sync=mode=>{
    VIEW_MODE = (mode==="list"||mode==="time") ? mode : "grid";
    const list = VIEW_MODE==="list";
    LIST_VIEW=list;
    document.body.classList.toggle("list-view",list);
    const lh=$("#listHead"); if(lh) lh.hidden=!list;   // 列表视图表头
    for(const b of btns){
      const on=b.dataset.v===VIEW_MODE;
      b.classList.toggle("on",on); b.setAttribute("aria-pressed",String(on));
    }
    /* 时间轴与「展开文件夹」互斥：展开文件夹时序列按文件夹数算（seqN），
       两套分组口径叠在一起会互相打架，所以选中时间轴时自动收起文件夹。 */
    if(VIEW_MODE==="time" && S.expandDirs) S.expandDirs=false;
  };
  let mode="grid";
  try{ mode=localStorage.getItem("mv.view")||"grid"; }catch(_){}
  if(!["grid","list","time"].includes(mode)) mode="grid";
  sync(mode);
  window.setViewMode = m=>{
    if(m===VIEW_MODE) return;
    sync(m);
    try{ localStorage.setItem("mv.view",VIEW_MODE); }catch(_){}
    if(LIST_VIEW && !S.metaMap) loadMetaMap();       // 顺带取尺寸/模型/采样器
    PAGE=1;                                          // 视图换了 → 行数口径变了 → 回第 1 页
    applyFilter();                                   // 重建行（列表要多渲染字段列 / 时间轴要重新分组）
    /* C-P1：广播视图变化 —— columns.js 据此把「列显隐」重新贴到列表行上。
       为什么需要：columns.js 的数据行处理已改为「网格视图短路 + 只维护新增节点」，
       而网格 ⇄ 列表切换时若卡片被复用（未重建），就不会有 childList 变化 ⇒ 补类落空。
       放在 applyFilter() **之后**：先让新行就位，再补类。 */
    try{ window.dispatchEvent(new CustomEvent("mv:viewchange", { detail:{ view:VIEW_MODE } })); }catch(_){}
    requestAnimationFrame(playViewEnter);            // V28：网格就位后播一次转场
  };
  /* V28：切视图时播一次淡入 + 微位移。只在「切视图」触发 —— 翻页 / 筛选不播，
     否则回到第 1 页会重复动画（构想取舍）。只动 opacity/transform，合成层不重排。 */
  function playViewEnter(){
    const gw=$("#gridwrap"); if(!gw) return;
    gw.classList.remove("view-enter"); void gw.offsetWidth; gw.classList.add("view-enter");
    setTimeout(()=>gw.classList.remove("view-enter"), 400);
  }
  for(const b of btns) b.onclick=()=>window.setViewMode(b.dataset.v);
})();

/* ---------------- 时间轴视图（V2.8.5）----------------
   工具条（粒度 / 回到今天 / 日期索引）+ [` `]` 跳组 + T 切视图。
   快捷键沿用既有守卫：焦点不在输入控件、且**不在大图内**（大图里 T 已被「聚焦标签输入」占用）。 */
let TL_PENDING=null;                     // 分页模式下点索引时暂存目标组，切到瀑布流后补跳
let TL_SUPPRESS=0;                       // 程序化跳组后的短暂抑制窗：期间不让滚动高亮覆盖点选结果

function renderTlUI(on){
  TL_JOB++;                              // 视图重建 → 作废仍在跑的分帧补齐任务（避免跳到旧位置）
  document.body.classList.toggle("timeline", !!on);
  const bar=$("#tlBar"); if(bar) bar.hidden=!on;
  const idx=$("#tlIndex");
  if(!on){
    if(idx){ idx.hidden=true; idx.classList.remove("open"); }
    TL_GROUPS=[]; return;
  }
  if(idx) idx.hidden = TL_GROUPS.length===0;
  syncTlGrain(); renderTlIndexList(); updateTlTodayBtn(); updateTlIndexActive();
  if(TL_PENDING!==null){ const g=TL_PENDING; TL_PENDING=null;
    requestAnimationFrame(()=>tlJump(g)); }
}
function syncTlGrain(){
  const box=$("#tlGrain"); if(!box) return;
  for(const b of box.querySelectorAll("button[data-g]")){
    const on=b.dataset.g===TL_GRAIN;
    b.classList.toggle("on",on); b.setAttribute("aria-pressed",String(on));
  }
}
/* 日期索引栏：只列**当前结果集里真实存在**的组（不猜、不补空日期）
   F1 P2-1：applyFilter 的**每次**调用都会走到这里（翻页 / 排序 / 搜索防抖 /
   切粒度 / 元数据到位），而大库在 hour·batch 粒度下 TL_GROUPS 可达数千组 ⇒
   原来每次都要清空 + 逐个 appendChild 新建数千节点与闭包。三处提速：
     · 组签名未变直接跳过重建（同 loadStats 的 _sig 思路）；
     · 用 DocumentFragment 一次性插入（原来逐个 append 各自触发重排）；
     · 顺便建 gi→节点 的映射表，供 tlSetIndexActive 做 O(1) 高亮。 */
let _tlIdxMap = null, _tlIdxOn = null;
function renderTlIndexList(){
  const box=$("#tlIdxList"); if(!box) return;
  const wrap=$("#tlIndex"), keep=wrap?wrap.scrollTop:0;
  const sig=TL_GRAIN+"|"+TL_GROUPS.length+"|"
    +TL_GROUPS.map(m=>m.gi+","+m.title+","+m.n).join(";");
  // 节点数一并校验：万一 DOM 被外部清过（换版 / 别的脚本动了），签名相同也不能短路
  if(sig===renderTlIndexList._sig && box.children.length===TL_GROUPS.length){
    if(wrap) wrap.scrollTop=keep;
    return;
  }
  renderTlIndexList._sig=sig;
  box.innerHTML="";
  _tlIdxMap=new Map(); _tlIdxOn=null;          // 节点全换 ⇒ 旧缓存全部作废
  const frag=document.createDocumentFragment();
  for(const m of TL_GROUPS){
    const b=document.createElement("button");
    b.type="button"; b.className="ti"; b.dataset.gi=String(m.gi);
    b.dataset.tip=m.title+" · "+m.n+" 张";
    const d=document.createElement("span"); d.className="ti-d"; d.textContent=m.title;
    const n=document.createElement("span"); n.className="ti-n"; n.textContent=String(m.n);
    b.appendChild(d); b.appendChild(n);
    b.onclick=()=>tlJump(m.gi);
    frag.appendChild(b);
    _tlIdxMap.set(String(m.gi), b);
  }
  box.appendChild(frag);                       // 一次性入 DOM
  if(wrap) wrap.scrollTop=keep;
}
function tlTodayGi(){                    // 结果里真实包含「今天」的那一组
  const k=tlDayKey(Date.now()/1000);
  for(const m of TL_GROUPS) if(m.d0<=k && k<=m.d1) return m.gi;
  return -1;
}
function updateTlTodayBtn(){
  const b=$("#tlToday"); if(!b) return;
  const gi=tlTodayGi();
  b.disabled = gi<0;
  b.dataset.tip = gi<0 ? "当前筛选结果里没有今天的图" : "跳到今天那一组";
}
/* 跳组：瀑布流下目标组头可能还没渲染出来（S.view 是全量，DOM 是分批的）→ 先按需补齐再跳；
   分页模式下目标可能真的不在本页（S.view 已切片）→ 切到瀑布流后补跳。
   跨度小就同步补齐（点击即到位）；跨度中等走分帧（不冻页面）；
   跨度极大（跨半个库）先把中间组折叠起来 —— 网格要连续渲染，否则得插几千张卡、实测 7500 张要 6.6s。 */
const TL_SYNC_MAX=300;                   // 同步补齐上限（行）；超过则分帧
const TL_FOLD_MIN=800;                   // 超过这个跨度就先折叠中间组（行数骤降后才谈得上「跳」）
function tlJump(gi){
  if(!TL_GROUPS.some(m=>m.gi===gi)) return;
  const el=document.querySelector('#grid .tl-head[data-gi="'+gi+'"]');
  if(el){ tlScrollTo(el); return; }
  /* F1 P2-2：分页模式下目标组不在本页时，原实现是 `$("#flowInf").click()` ——
     走的是和用户手动点按钮**完全相同**的 set(true)：① 展示方式被改成无限瀑布流；
     ② `mv.flow` 写进 localStorage ⇒ 下次启动还是瀑布流。用户只是想跳个日期，
     展示偏好被顺手改掉了（而 toast 那句「已切换到无限瀑布流」在他盯着日期时
     基本读不到）。改为：按全量行索引算出目标页码，**只切页、不动展示方式**。 */
  if(!FLOW){
    const ri=TL_ROWIDX[gi];
    if(typeof ri==="number" && ri>=0){
      const p=Math.floor(ri/PER)+1;
      if(p>=1 && p!==PAGE){
        TL_PENDING=gi;                          // 先挂上：applyFilter → renderTlUI 会取走并补跳
        setPage(p);
        toast("该日期在第 "+p+" 页，已为你翻过去");
        return;
      }
    }
    // 拿不到行索引（异常兜底）：保持「会跳到位」的能力，但把副作用说清楚
    TL_PENDING=gi; $("#flowInf").click();
    toast("该日期不在当前页，已临时切到瀑布流以定位；可在工具条切回分页");
    return;
  }
  const i=S.view.findIndex(x=>x.type==="tlhead" && x.gi===gi);
  if(i<0){ toast("该分组不在当前页"); return; }
  // 补到「组头 + 一批」：只补到组头的话它下方是空的，滚动会被内容高度夹住 → 贴不到顶
  const target=Math.min(i+BATCH, S.view.length-1);
  if(target+1-S.shown>TL_FOLD_MIN){
    let folded=0;
    for(const m of TL_GROUPS){
      if(m.gi>=gi) break;                // 只折目标组之前的
      if(!TL_COLLAPSED[m.gkey]){ TL_COLLAPSED[m.gkey]=true; folded++; }
    }
    if(folded){
      TL_PENDING=gi;                     // 先挂上：applyFilter → renderTlUI 会取走它并在新视图里补跳
      applyFilter();                     // 重建行（折叠后行数骤降）
      toast("已折叠中间 "+folded+" 组（点组标题可展开）");
      return;
    }
  }
  if(target+1-S.shown<=TL_SYNC_MAX){
    renderUpTo(target);
    const e=document.querySelector('#grid .tl-head[data-gi="'+gi+'"]');
    if(e){ tlScrollTo(e); return; }
  }
  const job=++TL_JOB;                    // 大跨度：分帧补齐，过程中页面保持响应
  const tgt=TL_GROUPS.find(x=>x.gi===gi);
  toast("正在定位到 "+(tgt?tgt.title:"该日期")+"…");
  tlFillAsync(target, job, ()=>{
    if(job!==TL_JOB) return;
    const e=document.querySelector('#grid .tl-head[data-gi="'+gi+'"]');
    if(e) tlScrollTo(e);
  });
}
/* 组头是 sticky：吸顶状态下它的 rect / offsetTop 都被改写（实测 tl_probe.py），
   拿它算目标位置会「点了没反应」。所以先把吸顶临时摘掉量一次静态位置，再滚过去。 */
function tlStaticTop(el){
  const prev=el.style.position;
  el.style.position="static";
  const off=el.offsetTop;                  // 读 offsetTop 会强制一次同步布局，拿到的就是静态位置
  el.style.position=prev;
  return off;
}
function tlScrollTo(el){
  const gw=$("#gridwrap"); if(!gw||!el) return;
  gw.scrollTop=tlStaticTop(el);
  for(let k=0;k<3;k++){                    // 微调：末组滚不到顶时会被 scrollTop 上限夹住，自然收敛
    const d=el.getBoundingClientRect().top-gw.getBoundingClientRect().top;
    if(Math.abs(d)<=1) break;
    gw.scrollTop+=d;
  }
  TL_SUPPRESS=performance.now()+700;       // 这次滚动是程序触发的：别让滚动高亮把「用户点的那一组」抢走
  tlSetIndexActive(+el.dataset.gi);
}
/* 当前视口顶部所在的组 = **最后一个已经吸顶的组头**。
   组头是 sticky 且都是 #grid 的直接子项，滚过的组头会叠在顶部（视觉上后一个盖住前一个），
   于是它们的 rect 全被改写成吸附位置 —— 只能取文档序里最后一个「贴住顶部」的那个。
   注意：末尾组下方内容不足时贴不到顶，此时几何上「视口顶部」确实还在上一组，
   所以跳转后靠 TL_SUPPRESS 保住用户点选的高亮（见 tlScrollTo）。 */
let _tlCurHead=null;                       // 上一次命中的组头（就近扫描的锚点）
function tlCurrentGi(){
  const gw=$("#gridwrap"); if(!gw) return -1;
  const top=gw.getBoundingClientRect().top+1;
  const heads=gw.querySelectorAll(".tl-head");
  const n=heads.length;
  if(!n) return -1;
  /* F1 P2-1：原来每帧从头遍历全部组头（每项一次 getBoundingClientRect ⇒ 强制布局）。
     组头沿文档序的 top 单调不减（已吸顶的那批都等于吸附位置，之后递增），
     所以「最后一个 top<=视口顶」这个边界只有一处 ⇒ 从上次命中的组头出发
     双向扩几步就够（滚动时通常只移动 0~2 格）。锚点失效（网格重建）则回退全扫。 */
  let k=-1;
  if(_tlCurHead) for(let i=0;i<n;i++) if(heads[i]===_tlCurHead){ k=i; break; }
  if(k<0) k=n-1;
  while(k>=0 && heads[k].getBoundingClientRect().top>top) k--;
  while(k+1<n && heads[k+1].getBoundingClientRect().top<=top) k++;
  if(k<0){ _tlCurHead=heads[0]; return +heads[0].dataset.gi; }
  _tlCurHead=heads[k];
  return +heads[k].dataset.gi;
}
/* 高亮指定组（并把索引栏里那一行滚进可见区） */
function tlSetIndexActive(gi){
  if(VIEW_MODE!=="time") return;
  const box=$("#tlIdxList"), wrap=$("#tlIndex");
  if(!box||!wrap) return;
  /* F1 P2-1：滚动时 rAF 每帧都会调这里，原来是遍历全部索引行做 classList.toggle
     （大库数千次 DOM 写）。现在查映射表，只动「旧命中 -1、新命中 +1」两个节点。 */
  if(!_tlIdxMap || _tlIdxMap.size!==box.children.length){
    _tlIdxMap=new Map(); _tlIdxOn=null;        // 兜底：映射表缺失/过期就现建一次
    for(const b of box.children) _tlIdxMap.set(b.dataset.gi, b);
  }
  const hit=_tlIdxMap.get(String(gi)) || null;
  if(hit!==_tlIdxOn){
    if(_tlIdxOn) _tlIdxOn.classList.remove("on");
    if(hit) hit.classList.add("on");
    _tlIdxOn=hit;
  }
  if(!hit) return;
  const r=hit.getBoundingClientRect(), pr=wrap.getBoundingClientRect();   // 索引栏内自动滚到可见
  if(r.top<pr.top+26) wrap.scrollTop-=(pr.top+26-r.top);
  else if(r.bottom>pr.bottom-4) wrap.scrollTop+=(r.bottom-pr.bottom+4);
}
function updateTlIndexActive(){ tlSetIndexActive(tlCurrentGi()); }
function tlStepGroup(d){
  if(VIEW_MODE!=="time" || !TL_GROUPS.length) return;
  let i=TL_GROUPS.findIndex(m=>m.gi===tlCurrentGi());
  if(i<0) i=0;
  const n=Math.max(0, Math.min(TL_GROUPS.length-1, i+d));
  if(n!==i) tlJump(TL_GROUPS[n].gi);
}

(function setupTimeline(){
  const box=$("#tlGrain");
  try{ const g=localStorage.getItem("mv.tlgrain");
       if(["hour","day","week","month","batch"].includes(g)) TL_GRAIN=g; }catch(_){}
  if(box) for(const b of box.querySelectorAll("button[data-g]")){
    b.onclick=()=>{
      const g=b.dataset.g;
      if(g===TL_GRAIN) return;
      TL_GRAIN=g; TL_COLLAPSED={}; TL_SHOWN={}; TL_PENDING=null;
      try{ localStorage.setItem("mv.tlgrain",g); }catch(_){}
      PAGE=1; applyFilter();
    };
  }
  const tt=$("#tlToday");
  if(tt) tt.onclick=()=>{
    const gi=tlTodayGi();
    if(gi<0){ toast("当前筛选结果里没有今天的图"); return; }
    tlJump(gi);
  };
  const tg=$("#tlIdxTgl");
  if(tg) tg.onclick=e=>{ e.stopPropagation();
    const idx=$("#tlIndex"); if(idx) idx.classList.toggle("open"); };
  const gw=$("#gridwrap");
  if(gw){
    let raf=0;                            // 滚动高亮：rAF 节流，避免每像素都重算
    gw.addEventListener("scroll",()=>{
      if(VIEW_MODE!=="time" || raf) return;
      if(performance.now()<TL_SUPPRESS) return;      // 跳组引起的程序化滚动，别抢高亮
      raf=requestAnimationFrame(()=>{ raf=0; updateTlIndexActive(); });
    },{passive:true});
    gw.addEventListener("click",()=>{     // 窄屏抽屉：点内容区即收起
      const idx=$("#tlIndex");
      if(idx && idx.classList.contains("open") && innerWidth<=900) idx.classList.remove("open");
    });
  }
  document.addEventListener("keydown",e=>{
    const el=e.target, tg2=(el.tagName||"").toUpperCase();
    if(tg2==="INPUT"||tg2==="TEXTAREA"||tg2==="SELECT"||el.isContentEditable) return;
    if(lb.classList.contains("on")) return;                      // 大图内有自己那套（T=聚焦标签输入）
    if(document.querySelector(".bigMask")||document.getElementById("catInputMask")) return;
    if(e.key==="t"||e.key==="T"){ e.preventDefault();
      window.setViewMode(VIEW_MODE==="time"?"grid":"time"); return; }
    if(VIEW_MODE!=="time") return;
    if(e.key==="["){ e.preventDefault(); tlStepGroup(-1); return; }
    if(e.key==="]"){ e.preventDefault(); tlStepGroup(1); }
  });
})();

/* 2a. 展示方式：分页 ⇄ 无限瀑布流（工具条「批量」左侧）
   分页：applyFilter 只取当前页，底栏显示页码与每页数量
   瀑布流：保留整份结果，renderMore 每批 100 张续载，底栏分页与每页数量隐藏 */
(function setupFlowMode(){
  const seg=$("#flowSeg"), pageBtn=$("#flowPage"), infBtn=$("#flowInf");
  if(!seg) return;
  const sync=()=>{
    document.body.classList.toggle("flow", FLOW);
    pageBtn.classList.toggle("on", !FLOW);
    infBtn.classList.toggle("on", FLOW);
    pageBtn.setAttribute("aria-pressed", String(!FLOW));
    infBtn.setAttribute("aria-pressed", String(FLOW));
    const pg=$("#pager"), pp=$("#perPage");
    if(pg) pg.hidden = FLOW;                 // 无限流：底栏不显示页码
    if(pp) pp.hidden = FLOW;                 // 也不显示每页数量
  };
  sync();
  const set=m=>{
    if(m===FLOW) return;
    FLOW=m;
    try{ localStorage.setItem("mv.flow", m?"1":"0"); }catch(_){}
    PAGE=1;
    sync();
    applyFilter();                            // 两种模式的切片/续载逻辑不同，重建视图
    toast(m ? "已切换到无限瀑布流（滚到底自动续载）" : "已切换到分页展示");
  };
  pageBtn.onclick=()=>set(false);
  infBtn.onclick=()=>set(true);
})();

/* 2b. 字体大小滑块：整页字号缩放（--fsx 同时驱动顶栏/侧栏/面板等承载文字的尺寸，
       因此放大字号时边框跟着长，不会挤成两行或截断）。

       拖动时只回显右上角数字、不立即应用：字号一变，上方区块高度和侧栏宽度都会变，
       滑块会被布局重排推离鼠标（不跟手）。松开鼠标（change）才应用并记忆；
       键盘方向键会同时触发 input+change，所以键盘操作仍是即时生效。 */
const fsRange=$("#fsRange");
if(fsRange){
  const fsVal=$("#fsVal");
  const applyFs=v=>{
    document.documentElement.style.setProperty("--fsx",(v/100).toFixed(3));
    if(fsVal) fsVal.textContent=v+"%";
    if(LAST_PAGER) renderPager(LAST_PAGER[0], LAST_PAGER[1]);   // 字号变了 -> 底栏可用宽度变了 -> 重排槽位
  };
  let savedFs=100;
  try{ savedFs=parseInt(localStorage.getItem("mv.fs")||"100",10)||100; }catch(_){}
  if(savedFs>=+fsRange.min && savedFs<=+fsRange.max) fsRange.value=savedFs;
  applyFs(+fsRange.value);                       // 初始化：按记住的值应用一次
  fsRange.oninput=()=>{                          // 拖动中：只回显数字，保持滑块在指针下
    if(fsVal) fsVal.textContent=fsRange.value+"%";
  };
  fsRange.onchange=()=>{                         // 松手/键盘确认：应用并记住
    applyFs(+fsRange.value);
    try{ localStorage.setItem("mv.fs",fsRange.value); }catch(_){}
  };
}

/* 2c. 主题：深色 / 亮色 / 跟随系统（大图页与对比页固定深色，见 CSS） */
(function(){
  const sel=$("#themeSel"); if(!sel) return;
  const mq=window.matchMedia("(prefers-color-scheme: light)");
  const apply=pref=>{
    const use = pref==="auto" ? (mq.matches?"light":"dark") : pref;
    document.documentElement.setAttribute("data-theme", use);
  };
  let pref="dark";                               // 默认深色（设计规范的主题口径）；侧栏固定深色，见 tokens.css
  try{ pref=localStorage.getItem("mv.theme")||"dark"; }catch(_){}
  if(!["dark","light","auto"].includes(pref)) pref="light";
  sel.value=pref; apply(pref);
  sel.onchange=()=>{
    pref=sel.value;
    try{ localStorage.setItem("mv.theme", pref); }catch(_){}
    apply(pref);
    toast(`主题：${{dark:"深色",light:"亮色",auto:"跟随系统"}[pref]}`);
  };
  mq.addEventListener("change",()=>{ if(pref==="auto") apply(pref); });
})();

/* 2d. 删除去向：应用内回收站 / Windows 回收站
   权威值在服务端 settings.json（删除是后端执行的）；这里只负责 UI 读写与
   全局 DEL_MODE 缓存 —— 各删除入口的确认文案、按钮提示都跟着它走。 */
(function setupDelMode(){
  const sel=$("#delModeSel"); if(!sel) return;
  /* 只改本地：DEL_MODE 缓存 + 下拉值 + 两处删除按钮 tip。**不碰服务端**。 */
  const applyLocal=(m)=>{
    DEL_MODE = (m==="system") ? "system" : "app";
    if(sel.value!==DEL_MODE) sel.value=DEL_MODE;
    const sys = DEL_MODE==="system";
    const tip = sys ? "删除（移入 Windows 回收站，可在资源管理器里还原）"
                    : "删除（移入应用内回收站，可在顶栏「回收站」还原）";
    const del=$("#btnDel");                       // 大图页删除
    if(del) del.dataset.tip=tip;
    const bdel=$("#bDel");                        // 批量条删除（V31 第②③步后全站只走 data-tip，不再写 title）
    if(bdel) bdel.dataset.tip=tip;
    const tb=$("#btnTrash");
    if(tb) tb.dataset.tip = sys
      ? "应用内回收站（当前删除去向是 Windows 回收站，这里不会有新条目）"
      : "应用内回收站：还原或彻底删除";
  };
  /* P1（2026-10-09）：「先落盘、成功后再改本地」。
     旧实现是**先改本地**（DEL_MODE / 下拉值 / 删除按钮 tip 在请求发出**之前**就翻转），
     失败分支只弹一条 3.2 秒自动消失的 toast 且什么都不回滚 ⇒ `DEL_MODE` 停在错值。
     而 `delIsSystem()` 驱动**所有**删除入口的确认文案（batch / extras / gallery / nav）——
     用户在确认框里看到「移入 Windows 回收站」，文件实际进了应用内回收站（或反之）。
     旧实现还在保存结果未知时就置 `DEL_MODE_READY=true`，主动关掉了别的模块
     「以服务端为准」的兜底说明，与它自己下方注释写的治理意图正好相反。
     现在：失败 → 回滚到切换前的值 + 底栏 warn + toastErr，且**不**置 READY。 */
  let _delSaving=false;
  const saveDelMode=(want)=>{
    if(_delSaving) return;            // 无防重入时连点两次会并发两个 POST，后到者定 toast
    const prev=DEL_MODE;
    _delSaving=true;
    sel.disabled=true;
    api("/api/prefs",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({delete_mode:want})})
      .then(r=>{
        const got=(r.delete_mode==="system") ? "system" : "app";
        DEL_MODE_READY=true;          // 只有服务端确认过才算「已同步」
        applyLocal(got);              // 以**服务端返回值**为准，不是请求值
        if(got!==want) toast("删除去向：服务端仍为"
          +(got==="system"?"Windows 回收站":"应用内回收站"),"warn");
        else toast(`删除去向：${got==="system"?"Windows 回收站":"应用内回收站"}`);
      })
      .catch(e=>{
        applyLocal(prev);             // 回滚：界面与服务端重新一致
        setBotState("warn","删除去向未能保存，仍按上一次的去向执行",null,false);
        toastErr("设置保存失败", String((e&&e.message)||e));
      })
      .finally(()=>{ _delSaving=false; sel.disabled=false; });
  };
  sel.onchange=()=>{ saveDelMode(sel.value); };
  /* F1 P2-4 / F3 P2-1：原来失败被 `.catch(()=>{})` 完全吞掉 —— 没有任何提示，
     下拉停在默认「应用内回收站」，而服务端若存的是 system，文件其实进了 Windows
     回收站，用户会去应用内回收站里找刚删的文件（找不到）。属于「用户以为 A、
     实际发生 B」的一类。改为：失败重试一次；仍失败则明确反馈（底栏 warn + toast +
     下拉提示），并保持 DEL_MODE_READY=false 供删除入口判断。 */
  const pullDelMode=(retry)=>{
    api("/api/info").then(j=>{
      DEL_MODE_READY=true;
      sel.removeAttribute("data-tip");
      applyLocal(j.delete_mode);
    }).catch(e=>{
      if(retry>0){ setTimeout(()=>pullDelMode(retry-1), 900); return; }
      DEL_MODE_READY=false;
      sel.dataset.tip="未能读取服务端「删除去向」设置（已重试一次）：界面显示的可能"
        +"不是服务端实际使用的去向；重新选择一次即可写入";
      setBotState("warn", "未能读取服务端「删除去向」设置，删除实际去向以服务端为准", null, false);
      toastErr("删除去向设置读取失败",
        "已重试一次仍失败：" + String((e&&e.message)||e));
    });
  };
  pullDelMode(1);
})();

/* 2e. 下载保存位置：留空 = 浏览器默认下载；填了 = 服务端直接落盘到该目录。
   权威值同样在服务端 settings.json（S.downloadDir 只是缓存），下载入口按它分叉。 */
(function setupDlDir(){
  const inp=$("#dlDirInp");
  const save=async()=>{
    if(!inp) return;
    const v=(inp.value||"").trim();
    try{
      const r=await api("/api/prefs",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({download_dir:v})});
      S.downloadDir = r.download_dir || "";
      inp.value = S.downloadDir;
      toast(S.downloadDir ? "下载将保存到："+S.downloadDir : "已恢复浏览器默认下载", "ok", 5000);
    }catch(e){ toast("保存失败："+e.message,"err"); }
  };
  if(inp){
    if($("#dlDirSave")) $("#dlDirSave").onclick=save;
    inp.addEventListener("keydown",e=>{ if(e.key==="Enter") save(); });
  }
  // S.downloadDir 的初始化与 DOM 无关，必须无条件跑（下载入口依赖它）
  api("/api/info").then(j=>{
    S.downloadDir=j.download_dir||"";
    if(inp) inp.value=S.downloadDir;
  }).catch(()=>{});
})();

/* 3b. 重启程序（后端 execv 原地重启；页面轮询等它回来再刷新） */
(function bindRestart(){
  const btn=$("#btnRestart"); if(!btn) return;
  let restarting=false;             /* ε-13（§6.6）：防重入 —— 数据目录切换后的
       「立即重启」走 window.mvRestart 直调，绕过工具栏按钮的 disabled 守卫；
       用户在等待期连点 toast 动作键 / 底栏动作条，会并发发多次 /api/restart，
       后端起出多个新进程抢同一端口。模块级标志一次重启只放行一发。 */
  const doRestart=async()=>{
    if(restarting) return;
    restarting=true;
    btn.disabled=true; setIcoLabel(btn, "refresh", "重启中…");
    toast("正在重启后端…","ok");
    try{ await api("/api/restart",{method:"POST"}); }catch(_){ /* 连接可能被重启掐断，正常 */ }
    const t0=Date.now();
    const tick=async()=>{
      try{
        const r=await fetch("/api/stats",{cache:"no-store"});
        if(r.ok){ location.reload(); return; }
      }catch(_){}
      // P1-34：起新进程失败时后端会继续在本进程服务，并在 /api/info.restart_failed
      // 给出原因 —— 旧实现只写日志，用户看到的是「点了重启界面就死了」。
      try{
        const ir=await fetch("/api/info",{cache:"no-store"});
        if(ir.ok){
          const j=await ir.json();
          if(j && j.restart_failed){
            restarting=false;               /* ε-13：放行下一次尝试 */
            btn.disabled=false; setIcoLabel(btn, "refresh", "重启程序");
            toast("重启失败："+j.restart_failed+"（程序仍在运行）","err");
            return;
          }
        }
      }catch(_){}
      if(Date.now()-t0>90000){
        restarting=false;                 /* ε-13：放行下一次尝试 */
        btn.disabled=false; setIcoLabel(btn, "refresh", "重启程序");
        toast("重启超时，请手动重新启动程序","err");
        return;
      }
      setTimeout(tick,600);
    };
    setTimeout(tick,800);
  };
  window.mvRestart=doRestart;        // 供其它模块调用（F28 数据目录切换后引导重启）
  btn.onclick=()=>{
    const dlg=bigDialog("重启程序");
    dlg.body.innerHTML=
      '<div style="font-size:calc(13px*var(--fsx));line-height:1.75">'
      +'后端进程会立刻重启（约 1~3 秒），期间页面短暂断开，重启完成后自动刷新。</div>'
      +'<div style="font-size:calc(12px*var(--fsx));color:var(--tx3);margin-top:6px">'
      +'若解析版本号有变化，启动后会重建一次提示词索引（约 10 秒）。</div>'
      +'<div style="display:flex;justify-content:flex-end;gap:calc(8px*var(--fsx));margin-top:calc(16px*var(--fsx))">'
      +'<button id="rstCancel">取消</button><button class="primary" id="rstGo">重启</button></div>';
    dlg.body.querySelector("#rstCancel").onclick=dlg.close;
    dlg.body.querySelector("#rstGo").onclick=()=>{ dlg.close(); doRestart(); };
  };
})();

/* 3. 快捷键说明：V17 起改为分类面板（原先是切换一行提示条的 display），
      入口 #hintTrig 仍保留，绑定统一在文件末尾的 setupKeysPanel() 里。 */

/* 4. 左侧栏：宽屏=折叠；≤900px=覆盖式抽屉（把手开合 + 遮罩关闭） */
const isNarrow=()=>window.matchMedia("(max-width:900px)").matches;
function closeNav(){ document.body.classList.remove("nav-open"); }
function toggleNav(){
  if(isNarrow()){ document.body.classList.toggle("nav-open"); return; }
  const l=$("#left"); l.classList.toggle("collapsed");
  $("#leftTgl").innerHTML = ico(l.classList.contains("collapsed")?"chevronRight":"chevronLeft");
}
$("#leftTgl").onclick=toggleNav;
$("#navBack").onclick=closeNav;
/* C-P4：rAF 节流 —— 这里每次 resize 都会调 isNarrow()（读 innerWidth，强制样式重算） */
window.addEventListener("resize",(window.mvThrottleRaf||(f=>f))(()=>{ if(!isNarrow()) closeNav(); }));
// 窄屏下点导航项/常用目录（不含下拉按钮）后自动收起抽屉
$("#left").addEventListener("click",e=>{
  if(!isNarrow()) return;
  if(e.target.closest("#rootBtn")) return;
  if(e.target.closest(".litem")||e.target.closest(".rs-btn")) closeNav();
});

/* 5. 左栏:位置导航(当前路径层级;子文件夹跳转走网格目录卡/面包屑,不在此列冗余) */
function buildSideNav(){
  const nav=$("#sideNav"); if(!nav) return;
  const root=S.root||"", cur=S.path||"";
  const btn=$("#rootBtn");
  const hint=(btn && btn.querySelector(".rs-hint")) || null;   // A 方案：根目录总数落点
  /* ⚠️ /api/list 返回**之前** S.root 是空串，而 loadStats()（统计先到一步）会调到这里 ——
     此刻 root=cur="" ⇒ cur===root 成立 ⇒ 走进「顶层」分支，画出一行没有名字、没有
     data-tip 的幽灵行（诊断 4 实测 t=2.00s 采样：data-p="" data-tip="" .nm=""）。
     用户侧表现为左栏闪一下空白条目；门禁侧表现为 B1 探针偶发假红（正好落在窗口内）。
     根路径未知时先空着，等已知路径的那次重建（同 buildLibNav 的「…」占位思路）。 */
  if(!root){ nav.innerHTML=""; if(hint) hint.textContent=""; return; }
  /* A 方案（2026-10-07 · 主人令）：**顶层不再画根行**。
     那一行与下方 #rootBtn 显示的是同一个根名（output），加上顶栏面包屑就是三处 ⇒
     主人反馈「一排好几个 output，我都搞不清具体干什么的」。改由 #rootBtn 单独承担
     「根名 + 总数」，点它仍是**切换根目录**的唯一入口。
       · 总数走 S.stats.total（nav_stats() 的全局口径，与左栏「库 · 全部」同源），
         而不是 S.view.length（那是筛选后的当前页）。
       · 统计未到位时不写（同 buildLibNav 的 `…` 占位思路，避免先闪一个假的 0）。
     ⚠️ 只在**真在顶层**（cur===root）时带张数 —— 进了子目录后用户看的是子目录内容，
        再挂根目录总数会误导（首轮门禁 B2 的同类教训：那一行曾错挂当前目录张数）。 */
  if(cur===root){
    nav.innerHTML="";
    const totN=(S.stats && typeof S.stats.total === "number") ? S.stats.total : null;
    /* V30（体验批次 D3）：顶层那行的落点文案。两个态都要有话可说 ——
       统计未到位时显式写「根目录」（而不是留空），到位后写「根目录 · N 项」。
       留空会让用户以为那一行坏了；写死「根目录」又不带信息量。 */
    const rootHint=(totN===null) ? "根目录" : ("根目录 · "+totN+" 项");
    if(hint) hint.textContent = rootHint;
    return;
  }
  if(hint) hint.textContent="";
  /* 子目录：保留完整层级（含根行）—— 这一行此刻是「点一下回到根目录」的导航项，
     与 #rootBtn 的「切换根目录」职责不同；且窄屏下 #crumbs 被 responsive.css 隐藏时，
     这里是唯一的路径导航，所以这一分支**不能省**（方案 C「直接砍掉」因此被否）。
     E5-1：根名走 rootDisplayName —— UNC 根（`\\NAS\photos`）要显示整段，否则看不出在哪台机器上。 */
  const rn=(rootDisplayName(root)||root);
  let h='<div class="litem root" data-p="'+esc(root)+'" data-tip="'+esc(root)+'">'
    +'<span class="ic">'+ico("dir")+'</span><span class="nm">'+esc(rn)+'</span>'
    +'<span class="hint">根目录</span></div>';
  const parts=cur.slice(root.length).replace(/^[\\/]/,"").split(/[\\/]/).filter(Boolean);
  let acc=root;
  parts.forEach((seg,idx)=>{
    acc=acc.replace(/[\\/]+$/,"")+"\\"+seg;
    const last = idx===parts.length-1;
    const pad = Math.min(20+idx*13, 62);          // 逐级缩进，太深就封顶
    h+='<div class="litem seg'+(acc===cur?" on":"")+'" data-p="'+esc(acc)+'"'
      +' style="padding-left:calc('+pad+'px*var(--fsx))" data-tip="'+esc(acc)+'">'
      +'<span class="ic">'+(last?"└":"├")+'</span><span class="nm">'+esc(seg)+'</span></div>';
  });
  nav.innerHTML=h;
  nav.querySelectorAll(".litem").forEach(el=>el.onclick=()=>load(el.dataset.p));
  const on=nav.querySelector(".litem.on");        // 路径很深时把当前项滚进视野
  if(on && on.scrollIntoView) on.scrollIntoView({block:"nearest"});
}

/* 6. 左栏:收藏分类入口 */
function buildFavNav(){
  const nav=$("#favNav"); if(!nav) return;
  const totalFav = S.stats ? S.stats.favorites : null;
  let h='<div class="litem" data-cat="*"><span class="ic">'+ico("starFill","filled")+'</span><span class="nm">全部收藏</span>'
    +(totalFav===null?"":'<span class="cnt">'+totalFav+'</span>')+'</div>';
  for(const c of catList())
    h+='<div class="litem" data-cat="'+esc(c.name)+'"><span class="ic">'+ico("star")+'</span><span class="nm">'+esc(c.name)+'</span>'
      +(c.count?'<span class="cnt">'+c.count+'</span>':"")+'</div>';
  nav.innerHTML=h;
  nav.querySelectorAll(".litem").forEach(el=>el.onclick=()=>{
    const cat=el.dataset.cat||"*";
    /* H2（2026-10-07）：分类状态已从 `#favcat` 外迁到 `S.filters.favcat`。 */
    // 再点一次当前已选中的分类（含「全部收藏」）= 取消收藏夹视图，退回目录列表
    if(favMode && (S.filters.favcat||"*")===cat){ load(S.path||S.root); return; }
    S.filters.favcat = cat==="*" ? "*" : cat;
    loadFavView();                       // 进入/切换：均按所选分类重拉服务端
    // 高亮
    nav.querySelectorAll(".litem").forEach(x=>x.classList.remove("on"));
    el.classList.add("on");
  });
  /* H3（2026-10-06）顺带修复 ——「进了收藏夹，左栏高亮闪一下就没」：
     本函数用 innerHTML 整片重写 #favNav，会把 .on 一起抹掉；而下方 _lightTick
     （350ms 轮询）只要感知到 favMode 翻转就重建这一区（`p=(S.path||"")+(favMode?"·fav":"")`
     变了），于是点击时同步加上的高亮在几百毫秒后被抹掉、且此后不再恢复。
     探针实测：点击后 50ms .on 在 → 200ms 已被 MutationObserver 记录的重建抹除。
     ⇒ 渲染完立刻按当前状态补一次高亮，让**所有**重建路径都自带正确高亮。 */
  syncFavNavSel();
}

/* 收藏分类高亮：跟随 favMode 与当前分类（离开收藏夹视图时自动清除，避免「全部收藏」一直亮着） */
function syncFavNavSel(){
  const nav=$("#favNav"); if(!nav) return;
  const cur=S.filters.favcat||"*";            // H2：状态外迁后不再读 `#favcat`
  nav.querySelectorAll(".litem").forEach(el=>{
    el.classList.toggle("on", favMode && (el.dataset.cat||"*")===cur);
  });
}

/* 6b-0. 左栏「工作流 / 格式」分区（内容区不再放重复的类型/格式 chips：
   类型走「库」，格式走「格式」分区，内容区工具条只留视图相关控件） */
const FMTS = new Set();                         // 格式筛选（可多选，空集=全部）
let WFK = "";                                   // 工作流类型：""=全部 / t2i / i2i / cn / other
const fmtLabel = ext => String(ext).replace(".", "").toUpperCase();
/* F1 P2-3：计数改用**当前结果集**口径（filters.applyFilter 里现算，见 state.js 注释）。
   S.stats.formats 是根目录全树的全局口径，在子目录/收藏/最近视图里与可筛结果对不上。 */
const fmtCounts = () => CMVQuery.active
  ? Object.fromEntries(Object.keys(S.stats?.formats || {}).map(format=>[format,"—"]))
  : FMT_COUNTS || {};
const fmtKeys = () => Object.keys(fmtCounts()).sort((a, b) => fmtCounts()[b] - fmtCounts()[a]);
/* F1 P2-3 口径 = 「当前结果集」里该格式**可筛**的张数（勾上后能看到的数）。
   P0-8（审计第 4 级）：提示词搜索现也走 filters.js 的 `_applyCommonFilters` 里
   同一段 FMT_COUNTS 统计 ⇒ 左栏数字在搜索态同样自洽，不再停留在上一次目录筛选的数。 */
function recountFmt(v) {
  FMT_COUNTS = {};
  for (const it of (v || [])) {
    if (it.type === "dir") continue;
    const e = (it.path.match(/\.[^.\\/]+$/) || [""])[0].toLowerCase();
    if (e) FMT_COUNTS[e] = (FMT_COUNTS[e] || 0) + 1;
  }
}

function setFmt(f) {                             // 空串 = 清空（全部格式）
  if (!f) FMTS.clear();
  else if (FMTS.has(f)) FMTS.delete(f);
  else FMTS.add(f);
  applyFilter();
}

function buildFmtNav() {
  const nav = $("#fmtNav"); if (!nav) return;
  const c = fmtCounts();
  const keys = fmtKeys();
  if (!keys.length) { nav.innerHTML = '<div class="lchk" style="cursor:default">暂无数据</div>'; return; }
  nav.innerHTML = keys.map(k =>
    `<label class="lchk${FMTS.has(k) ? " on" : ""}" data-tip="${esc(fmtLabel(k))}（可多选；数字为当前视图可筛张数）">`
    + `<input type="checkbox" data-f="${esc(k)}"${FMTS.has(k) ? " checked" : ""}>`
    + `<span class="nm">${esc(fmtLabel(k))}</span>`
    + `<span class="cnt">${c[k]}</span></label>`).join("");
  nav.querySelectorAll("input[data-f]").forEach(inp => inp.onchange = () => setFmt(inp.dataset.f));
}

function buildWfNav() {
  const nav = $("#wfNav"); if (!nav) return;
  const c = (S.stats && S.stats.wfk) || {};
  const total = S.stats ? (c.t2i || 0) + (c.i2i || 0) + (c.cn || 0) + (c.other || 0) : null;
  const rows = [["", "grid", "全部", total, "不按类型细分（含无生成参数的媒体）"],
                ["t2i", "write", "文生图", c.t2i, "纯提示词出图（latent 来自 EmptyLatent）"],
                ["i2i", "image", "图生图", c.i2i, "有输入图片（latent 来自 VAEEncode / LoadImage）"],
                ["cn", "cn", "ControlNet", c.cn, "工作流里用了 ControlNet 节点"],
                ["other", "dot", "其他 / 无生成参数", c.other, "认不出工作流类型，或没有生成参数"]];
  nav.innerHTML = rows.map(([k, ic, nm, n, tip]) =>
    `<div class="litem${WFK === k ? " on" : ""}" data-w="${k}" data-tip="${esc(tip)}">`
    + `<span class="ic">${ico(ic)}</span><span class="nm">${nm}</span>`
    + `<span class="cnt">${n == null ? "…" : n}</span></div>`).join("");
  nav.querySelectorAll(".litem").forEach(el => el.onclick = () => setWfk(el.dataset.w));
}

function setWfk(k) {
  WFK = k || "";
  applyFilter();                                // 类型筛选走提示词索引里的 wfk（无需进入工作流主视图）
}

/* P1-8：清空格式 + 工作流类型筛选。
   「清除全部筛选」以前漏了这两个，结果是**筛空之后点清除、网格还是空的**——
   看起来像按钮坏了（左栏分区还可折叠，收起时连线索都没有）。
   统一从这里清，保证「清全部」语义完整。 */
function clearFmtWfk() {
  const had = FMTS.size > 0 || !!WFK;
  FMTS.clear();
  WFK = "";
  /* ⚠️ 这里**不能**调用 applyFilter()：它函数体里会读 FMTS/WFK（filters.js 的 _sig 计算），
     而它被 app.js 的 load() 在「换目录」分支里调用时，data 与筛选状态正在重建，
     提前重算会拿到半成品状态。刷新的责任在调用方：
       · filters.js 的 clearAllFilters() —— 清完自己调 applyFilter()
       · app.js 的 load() —— 清完自己调 applyFilter()
     两边都不许省，否则「点了清除网格还是空的」。 */
  if (had) { buildFmtNav(); buildWfNav(); }      // 勾选态/高亮态跟着归位
  return had;
}

var LAST_PAGER = null;          // 最近一次 renderPager 的 (total, pages)，供 resize / 改字号后重排槽位

/* ---------- 分页条槽位：按底栏实际可用宽度算，不写死个数 ----------
   ① 底栏宽 - 两侧预留（--pg-side，与 CSS 的 #pager max-width 同源，常量只有一份）
   ② 再减掉 ‹ › 两个箭头及其间距
   ③ 取奇数，下限 5、上限按屏宽分 15 / 21 / 31：偶数会让「当前页」不在正中；太少没得挑，
      太多是视觉噪音（真要跳去远处，用右侧「跳至」输入框更快）
   槽位数只随「窗口宽度 / 字号缩放」变化，不随翻页变化 ——
   所以翻页时整行宽度仍然恒定，不会左右抖（这是旧版固定 7 个槽位的初衷，
   现在改成「按屏算个数」，把宽屏白白空着的几千像素用起来）。 */
function pagerSlots(){
  const bot=$("#bot"); if(!bot) return 7;
  const cs=getComputedStyle(document.documentElement);
  const fsx=parseFloat(cs.getPropertyValue("--fsx"))||1;
  const gap=8*fsx;                        // 底栏 / #pager 的 gap（--sp-2）
  const slotW=26*fsx+4*fsx;               // 单槽宽 + .pgnums 内部的 gap（--sp-1）
  const navW=2*(24*fsx)+2*gap;            // ‹ › 两个箭头 + 两侧间距
  const wOf=sel=>{ const e=bot.querySelector(sel); return e ? e.getBoundingClientRect().width : 0; };
  const left=wOf("#botState");                                   // 左侧「已就绪 / N 项」
  const right=wOf(".pgjump")+wOf("#perPage")+wOf("#hintTrig");   // 右侧跳页框 / 每页数量 / 快捷键
  const pg=$("#pager");
  let avail;
  if(pg && getComputedStyle(pg).position==="absolute"){
    /* 宽屏：#pager 绝对定位居中，两侧固定件不参与它的排布 ——
       居中意味着左右各留 (W-宽度)/2，所以只要它 ≥ **较宽的那一侧**就不会压住，
       而不是「两侧宽度之和」（旧算法按和算，右侧一堆控件时会把页码挤宽到重叠）。 */
    avail=bot.clientWidth-2*(Math.max(left, right)+2*gap+32*fsx);
  }else{
    /* 窄屏（≤620px，见 responsive.css）：#pager 改为 flex 子项，两侧各占自己那份 */
    avail=bot.clientWidth-left-right-4*gap-16*fsx;
  }
  let n=Math.floor((avail-navW)/slotW);
  if(n%2===0) n-=1;                       // 取奇数：当前页才在正中
  /* 上限随底栏宽度分级：4K 一行能舒舒服服放下 30+ 个，只给 20 个等于继续浪费；
     但也不设成无上限 —— 页码太多反而难扫视，跳到远处用右侧「跳至」框更快。 */
  const cap = bot.clientWidth >= 2400 ? 31 : bot.clientWidth >= 1600 ? 21 : 15;
  return Math.max(5, Math.min(cap, n));
}

/* 页码序列：长度恒等于 slots（总页数不够填满时就是 pages 个）。
   贴左：1 2 3 4 … 末页 ／ 中间：1 … c-1 c c+1 … 末页 ／ 贴右：1 … 末页-1 末页
   三个分支长度都等于 slots，所以翻页时槽位个数不变、整行宽度不变。 */
function pageSeq(cur, pages, slots){
  if(pages<=slots){ const a=[]; for(let n=1;n<=pages;n++) a.push(n); return a; }
  const mid=slots-4, half=(mid-1)>>1;     // 两个省略号都在场时，中间数字的个数
  const s0=cur-half, e0=s0+mid-1;
  if(s0<=2){                              // 贴左：前 slots-2 个数字 + 省略号 + 末页
    const a=[]; for(let n=1;n<=slots-2;n++) a.push(n);
    a.push(null, pages); return a;
  }
  if(e0>=pages-1){                        // 贴右：首页 + 省略号 + 末尾 slots-2 个数字
    const a=[1, null];
    for(let n=pages-(slots-2)+1;n<=pages;n++) a.push(n);
    return a;
  }
  const a=[1, null];                      // 中间：首页 + 省略号 + 窗口 + 省略号 + 末页
  for(let n=s0;n<=e0;n++) a.push(n);
  a.push(null, pages);
  return a;
}

/* 6b-1. 底栏分页（客户端切页；整表已在 applyFilter 里排好序） */
function setPage(n){
  if(CMVQuery.active){ void CMVQuery.goPage(Math.max(1, parseInt(n,10)||1)); return; }
  const p=Math.max(1, parseInt(n,10)||1);
  if(p===PAGE) return;
  PAGE=p; applyFilter();
  const gw=$("#gridwrap"); if(gw) gw.scrollTop=0;
}
function renderPager(total, pages){
  if(CMVQuery.renderPagerOverride()) return;
  const box=$("#pager"); if(!box) return;
  LAST_PAGER=[total, pages];                   // 记下来：窗口缩放 / 改字号后按新宽度重排
  if(FLOW){ box.innerHTML=""; return; }        // 无限瀑布流：不分页，底栏页码区留空
  const cur=Math.min(PAGE, pages);
  const btn=(n,label,dis,cls)=>
    `<button data-p="${n}"${dis?" disabled":""}${cls?` class="${cls}"`:""}>${label}</button>`;
  const GAP='<span class="gap">…</span>';
  const parts=pageSeq(cur, pages, pagerSlots());
  /* 结构：[‹] [页码（可横向滚动）] [›]
     左右箭头是 .pgnums 的兄弟节点而不是列表首尾 —— 页码很多时箭头固定不动，
     也不会紧挨着「1 / 尾页」导致误点。 */
  const inner=parts.map(n=>n===null?GAP:btn(n,String(n),false,n===cur?"on":"")).join("");
  /* 批次 B：箭头从「‹ / › 字符」改走 ICO（chevronLeft / chevronRight）——
     字符箭头在不同 Windows 字体下字形差异明显，且是全站唯一不跟 ICO 体系的导航箭头。 */
  box.innerHTML = btn(cur-1,ico("chevronLeft"),cur<=1,"pgnav")
    + '<span class="pgnums">'+inner+'</span>'
    + btn(cur+1,ico("chevronRight"),cur>=pages,"pgnav");
  box.querySelectorAll("button[data-p]").forEach(b=>b.onclick=()=>setPage(+b.dataset.p));
  /* 跳页输入框已挪到 #bot 静态结构（右侧与「每页数量」并排），这里只同步范围与当前值 */
  const jump=$("#pgJump"), jt=$("#pgTotal");
  if(jt) jt.textContent=pages;
  if(jump && document.activeElement!==jump){ jump.max=pages; jump.value=cur; }
  const onNum=box.querySelector(".pgnums .on");        // 当前页滚进可视区（页码多时）
  if(onNum && onNum.scrollIntoView) onNum.scrollIntoView({block:"nearest",inline:"center"});
  const sel=$("#perPage"); if(sel && String(PER)!==sel.value) sel.value=String(PER);
  /* 条目总数只显示在顶栏 #count 一处（此前底栏 #botTotal 是同一个数字的重复显示） */
}
/* 窗口尺寸变化 -> 槽位数重算（只重排分页条，不重绘网格）。
   改字号（--fsx）不触发 resize，所以 applyFs 里也调一次。 */
(function bindPagerResize(){
  let t=null;
  /* C-P4：rAF 节流 + 原有 120ms 去抖叠加（原本只有后者 —— 拖窗口时仍会每 120ms
     重算一次分页条，且重算前每次都先 clearTimeout 再排，等于按 120ms 节流）。 */
  const onResize = (window.mvThrottleRaf || (f => f))(()=>{
    if(!LAST_PAGER || FLOW) return;
    clearTimeout(t);
    t=setTimeout(()=>{ if(LAST_PAGER) renderPager(LAST_PAGER[0], LAST_PAGER[1]); }, 120);
  });
  window.addEventListener("resize", onResize);
})();
(function bindPerPage(){
  const sel=$("#perPage"); if(!sel) return;
  sel.value=String(PER);
  sel.onchange=()=>{
    PER=parseInt(sel.value,10)||20;
    try{ localStorage.setItem("mv.per", String(PER)); }catch(_){}
    PAGE=1; applyFilter();
  };
})();
/* 跳页输入（#bot 静态框）：回车直达，非法输入回弹当前页 */
(function bindJump(){
  const jump=$("#pgJump"); if(!jump) return;
  jump.onkeydown=e=>{
    if(e.key!=="Enter") return;
    const pages=parseInt(jump.max,10)||1, v=parseInt(jump.value,10);
    if(v>=1 && v<=pages) setPage(v);
    else jump.value=PAGE;
  };
  jump.onblur=()=>{ jump.value=PAGE; };
})();

/* 6b. 左栏:库数量（全部/图片/视频/工作流）——统一取 /api/stats 的 nav 口径 */
function refreshStatsSoon(){
  clearTimeout(statsTimer);                        // 收藏/标签/删除连点时合并成一次请求
  statsTimer=setTimeout(()=>loadStats(), 400);
}
/* 索引构建期，统计读取连续失败几次后彻底停机（免无限请求） */
const STATS_RETRY_MAX=3;
async function loadStats(){
  const tok=++statsTok;
  const statsRoot=normPath(S.root || "");
  let j;
  try{ j=await api("/api/stats"); }
  catch(e){
    /* P1（2026-10-09）：失败不能裸 return —— 续轮询的 setTimeout 只写在下面的
       **成功分支**里，一次失败轮询链就永久断开；底栏还挂着上一轮写入的 sticky
       busy（sticky 不走 4.2s 自动回落）「建立提示词索引 1234/9700」，用户以为
       还在建（实际可能早已失败或早已建完）。失败本身也完全静默：无 toast、
       无 warn、无重试 —— 与下方 pullDelMode 的治理标准（失败重试 + 明确反馈）
       正好不一致。现在：构建预算内自动重试；重试用尽或不在构建期则落非 sticky
       的 warn，不再假装还在进行。 */
    if(tok!==statsTok) return;                     // 已有更新的请求，丢弃
    loadStats._fails=(loadStats._fails||0)+1;
    const _building=!!(S.stats && S.stats.index_building);
    if(!statsBuildAt) statsBuildAt=Date.now();     // 失败也起预算，免得无限重试
    if(_building && loadStats._fails<=STATS_RETRY_MAX
       && Date.now()-statsBuildAt<120000){
      setBotState("warn","统计读取失败，索引进度暂时未知（自动重试中）",null,true);
      setTimeout(()=>{ if(tok===statsTok) loadStats(); }, 2000);
      return;
    }
    setBotState("warn","统计读取失败，底栏数字可能不是最新的",null,false);
    return;
  }
  if(tok!==statsTok || statsRoot!==normPath(S.root || "")) return;                       // 已有更新的请求 / 根目录已变，丢弃
  loadStats._fails=0;                              // 成功即清零连续失败计数
  if(j.mig) mvMigrateReport();                     // 迁移完成待领取：随 stats 捎信，取走即清（P4，V2.12.1）
  S.stats=j.nav||null;
  if(S.stats && S.stats.mark_map){
    MARK_MAP=S.stats.mark_map;                     // 卡片角标/大图下拉用
    /* ⚠️ 别整表覆盖到「本地刚写的标记」头上：这份 mark_map 是**一次扫描的快照**，
       而统计在索引构建期每 2s 轮询一次 ⇒ 晚到的旧快照会把刚写入的标记抹掉，
       紧接着下面的 syncAllCardMarks() 摘掉角标 —— 2026-09-17 实测表现是
       「标记完角标闪一下不见了，过一会儿又回来」，且会让 v4_batch1_check §② 间歇性红
       （门禁 `tests/mark_badge_check.py` 已确定性取证：注入一份不含该路径的 stats 响应 ⇒
       MARK_MAP 掉键 + 角标消失；真实响应回来 ⇒ 恢复）。
       ⚠️ 判据是「**确认收敛**」，不是墙钟宽限期（那版 2026-09-17 当天就被 §② 的间歇红打回）：
       宽限期固定 3s，可是一次 loadStats 的返回时刻由网络与主线程决定，定时器被节流时会晚到
       远超 3s ⇒ 宽限期一过期，迟到的旧快照照样抹掉标记（batch1 在回归里红、单独跑绿，就是
       窗口被拉长那几次）。改成：**只要快照还没反映出这次本地写入，就继续以本地为准；一旦
       快照出现了它（标记 = 同值 / 取消 = 键不在）立刻交还服务端**。覆盖时长由「服务端何时
       读到」决定，与墙钟无关。MARK_TTL_MS 只在另一端改了同一张时兜底。*/
    const _now=Date.now();
    for(const [p,v] of MARK_LOCAL){
      const caughtUp = v.st ? MARK_MAP[p] === v.st : MARK_MAP[p] === undefined;
      if(caughtUp || _now-v.t > MARK_TTL_MS){ MARK_LOCAL.delete(p); continue; }
      if(v.st) MARK_MAP[p]=v.st; else delete MARK_MAP[p];
    }
  }
  buildLibNav();
  buildFavNav();                                   // 收藏分类计数与「全部收藏」同步刷新
  /* 格式 / 工作流类型的计数同样来自 /api/stats，而首次 buildFmtNav 是在统计到达
     之前（applyFilter 里）跑的，那时 S.stats 还是空 —— 不补这一下，首屏「格式」
     会一直显示「暂无数据」。只在内容真的变了才重建：索引构建期 loadStats 每 2s
     轮询一次，无脑重建会把用户正在点的复选框冲掉。 */
  /* F1 P2-3：格式计数据换成 FMT_COUNTS（现算，不再等 /api/stats）⇒ 这里跟着换，
     否则统计到达后会误判「内容变了」把用户正在点的复选框冲掉。 */
  const _sig=JSON.stringify(FMT_COUNTS||{})
    +"|"+JSON.stringify((S.stats&&S.stats.wfk)||{})
    +"|"+((S.stats&&S.stats.index_building)?1:0)+"|"+Array.from(FMTS).sort().join(",");
  if(_sig!==(loadStats._sig||"")){ loadStats._sig=_sig; buildFmtNav(); buildWfNav(); }
  syncAllCardMarks();                                          // 已渲染的卡片补上角标（单次遍历）
  /* V30（D3）：左栏「位置」顶层那行的张数取自 S.stats.total —— 而首次 buildSideNav()
     发生在统计到达**之前**，此后只有「路径 / 收藏 / 筛选变化」才重建（下方 350ms 轮询），
     ⇒ 数字一直空缺（首轮门禁 B1 抓到：total=240 已到位，那行却仍是旧文案）。
     这里按 total 变化补一次重建：变了才调，避免索引构建期每 2s 轮询都冲掉侧栏 DOM。 */
  if((S.stats ? S.stats.total : null) !== loadStats._lastTotal){
    loadStats._lastTotal = S.stats ? S.stats.total : null;
    buildSideNav();
  }
  if(S.stats && S.stats.index_building){           // 索引在建：工作流数先显示「…」，建完回填
    const _d=S.stats.index_done||0, _t=S.stats.index_total||0;
    setBotState("busy", _t ? ("建立提示词索引 "+_d+"/"+_t) : "正在扫描生成参数…", null, true);   // V3
    /* §6.1：预算原按「调用次数」算（statsPolls++ < 40），而**任何一次** loadStats 都吃一格 ——
       用户在索引构建期切页 / 改筛选 / 拖时间轴几次就能把 40 格吃完，轮询提前停止，
       进度条卡在旧数字（索引其实还在后台建，界面却不再报数）。
       改成按**墙钟**算：以首次进入构建态的时刻为基准，120s 封顶，手动调用不吃预算。 */
    const _now = Date.now();
    if(!statsBuildAt) statsBuildAt = _now;
    if(_now - statsBuildAt < 120000)               // 最多轮询 ~120s，避免异常时无限请求
      setTimeout(()=>{ if(tok===statsTok) loadStats(); }, 2000);
    else setBotState("warn","索引仍在后台构建",null,false);   // 停轮询后别再挂着「进行中」
  }else{
    if(statsBuildAt) setBotState("idle","已就绪",null,true);  // 建完落回就绪（V3）
    statsBuildAt=0;
  }
}
function buildLibNav(){
  const nav=$("#libNav"); if(!nav) return;
  const st=S.stats, f=S.filters.type;      // H2：类型状态来自 S.filters（原 `#filter` 已删）
  const wf = !st ? "…" : (st.index_building && !st.workflow ? "…" : st.workflow);
  const inDir = !favMode && libMode!=="recent" && libMode!=="marked";
  const rows=[
    ["all","grid","全部", st?st.total:"…", inDir && libMode==="" && f==="all",
      "根目录下全部图片和视频（含子目录）"],
    ["image","image","图片", st?st.images:"…", inDir && libMode==="" && f==="image", "只显示图片"],
    ["video","video","视频", st?st.videos:"…", inDir && libMode==="" && f==="video", "只显示视频"],
    ["workflow","flow","有元数据", wf, libMode==="workflow",
      "按图片内嵌的工作流或生成参数筛选（依赖提示词索引）"],
    ["recent","clock","最近使用", st?st.recent:"…", libMode==="recent",
      "最近预览 / 下载 / 复制提示词 / 打开过的图（最多 200 条，按路径去重）"],
    ["marked","flag","已标记", st?st.marked:"…", libMode==="marked",
      "手动标记的临时状态：待处理 / 待发布 / 待比较 / 待删除（与收藏分开）"],
  ];
  nav.innerHTML=rows.map(([k,ic,nm,cnt,on,tip])=>
    '<div class="litem'+(on?" on":"")+'" data-k="'+k+'" data-tip="'+esc(tip)+'">'
      +'<span class="ic">'+ico(ic)+'</span><span class="nm">'+nm+'</span>'
      +'<span class="cnt">'+esc(String(cnt))+'</span></div>').join("");
  nav.querySelectorAll(".litem").forEach(el=>el.onclick=()=>setLibFilter(el.dataset.k));
}
async function setLibFilter(kind){
  if(kind==="recent" || kind==="marked"){ await loadLibView(kind); return; }
  // 收藏夹视图（favMode）同样要先退回目录列表：否则「全部 / 图片 / 视频 / 工作流」
  // 点下去只是改了筛选条件，网格还是收藏夹那批数据，看起来像按钮失灵
  const leaving = favMode || libMode==="recent" || libMode==="marked";
  if(leaving) await load(S.path||S.root,{silent:true});   // 先回到目录列表（load 会复位 favMode/libMode）
  if(kind==="workflow"){
    if(!S.metaMap){                                // 首次点「工作流」：拉元数据（顺带触发索引构建）
      await loadMetaMap();
      if(!S.metaMap){ toast("当前目录没有可解析生成参数的图片"); return; }
    }
    libMode="workflow";                            // 「库」各行是互斥主视图
    $("#wfClass").style.display="";                // H2：显隐仍按 libMode 管（值由 syncFiltersUI 推）
    if($("#markState")) $("#markState").style.display="none";
    S.filters.type="all"; syncFiltersUI();
    applyFilter(); return;
  }
  libMode="";
  $("#wfClass").style.display="none"; if($("#markState")) $("#markState").style.display="none";
  S.filters.type = kind==="all" ? "all" : kind;
  syncFiltersUI();
  applyFilter();
}

/* 6c. 跨目录视图：最近使用 / 已标记（服务端过滤，前端只叠加类型与搜索） */
async function loadLibView(kind, keepPath){
  CMVQuery.cancelPending();
  const tok=++viewGen;
  setBotState("busy", "正在读取"+(kind==="recent"?"最近使用":"已标记")+"…", null, true);
  grid.setAttribute("aria-busy", "true");
  const url = kind==="recent"
    ? "/api/recent_view"
    : "/api/marks_view?state="+encodeURIComponent(S.filters.markState||"*");
  const j=await api(url).catch(e=>({error:e.message}));
  if(tok!==viewGen) return;
  grid.setAttribute("aria-busy", "false");
  if(j.error){ setBotState("idle", "读取失败", null, true); toast(j.error,"err"); return; }
  CMVQuery.leave();
  favMode=false; libMode=kind; S.metaMap=null; resetMetaFilters();
  if(tagFilterActive || TAG_FILTER.length || TAG_EXCLUDE.length){   // 与标签筛选不叠加
    TAG_FILTER=[]; TAG_EXCLUDE=[]; tagFilterActive=false; S.tagView=null;
    renderTagNav();
  }
  S.dirs=[];
  S.items=(j.items||[]).map(it=>{                 // 卡片下沿显示来源与相对路径
    const rel=(S.root && it.path.startsWith(S.root))
      ? it.path.slice(S.root.length).replace(/^[\\/]/,"") : it.path;
    /* V4 批次 4：来源前缀由图标承担（it.snipIcon），snippet 只留纯文本 */
    if(kind==="recent"){ it.snipIcon="clock";
      it.snippet=(RECENT_LABELS[it.ract]||"使用")+" · "+rel; }
    else { it.snipIcon="flag";
      it.snippet=(it.marklabel||"已标记")+" · "+rel; }
    if(it.mark) MARK_MAP[it.path]=it.mark;
    return it;
  });
  if(batchOn) setBatch(false);
  /* H2（2026-10-07）：`#favcat` 已删除；`#wfClass` / `#markState` 的显隐仍按 libMode 管，
     它们的**值**由 applyFilter → syncFiltersUI() 按 S.filters 推下来。 */
  $("#wfClass").style.display="none";
  $("#markState").style.display = kind==="marked" ? "" : "none";
  applyFilter();
  if(keepPath) keepIdxByPath(keepPath);   // 取消标记后当前图会被移出「已标记」→ S.idx 复位/关大图
  mkCrumbs($("#crumbs"),                                 // 跨目录视图：自定义面包屑（V37 统一构造）
    [{label: kind==="recent" ? "最近使用" : "已标记"}],
    {back:{label:"返回目录",onclick:()=>load(S.path||"")}});
  toast((kind==="recent"?"最近使用":"已标记")+`：${S.items.length} 项`,"ok");
}

/* 记录一次「最近使用」（失败不打扰用户，数量随后自动刷新） */
function recordRecent(path, action){
  if(!path) return;
  api("/api/recent",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({path, action})}).catch(()=>{});
  refreshStatsSoon();
}

/* 列表视图「标签」列文本（**纯文本**：喂 title 悬停提示，不能含 svg）
   V4 批次 1：标记状态不再带字符 ⚑，改由图标承担（见 fillLvTags） */
function listTagText(it){
  const parts=[];
  const st=it && MARK_MAP[it.path];
  if(st) parts.push(MARK_LABELS[st]||"");
  for(const t of ((it&&it.tags)||[])) parts.push(t);
  return parts.join(" · ");
}
/* V4 批次 1：列表视图「标签」列的内容 = 标记矢量旗子 + 纯文本。
   图标走 inline-flex，这样单元格的 nowrap + text-overflow:ellipsis 仍然生效。 */
function fillLvTags(cell, it){
  if(!cell) return;
  const txt=listTagText(it);
  cell.textContent="";
  cell.dataset.tip=txt||"—";
  if(it && MARK_MAP[it.path]){
    const ic=document.createElement("span"); ic.className="lvmk";
    ic.innerHTML=ico("flag");
    cell.appendChild(ic);
  }
  cell.appendChild(document.createTextNode(txt||"—"));
}
function makeLvTags(it){
  const cell=document.createElement("span"); cell.className="lv-tags lv-cell";  /* V10：列显隐锚点 */
  cell.dataset.col="tags";
  fillLvTags(cell, it);
  return cell;
}

/* 列表视图：元数据到位后把已渲染行的尺寸/模型/采样器补上（不重排 DOM） */
function patchListMeta(){
  if(!LIST_VIEW || !S.metaMap) return;
  const g=$("#grid"); if(!g) return;
  g.querySelectorAll(".card[data-p]").forEach(el=>{
    const m=S.metaMap[el.dataset.p];
    const set=(sel,txt,title)=>{ const c=el.querySelector(sel); if(!c) return;
      c.textContent=txt; if(title!==undefined) c.dataset.tip=title; };
    set(".lv-dim", (m&&m.w&&m.h)?(m.w+"×"+m.h):"—", "尺寸");
    set(".lv-model", m?(m.model||"—"):"—", m&&m.model?m.model:"");
    set(".lv-sampler", m?(m.sampler||"—"):"—", m&&m.sampler?m.sampler:"");
  });
}

/* 网格卡片：元数据到位后把缩略图角标上的尺寸补上 */
function patchCardDim(){
  if(!S.metaMap) return;
  const g=$("#grid"); if(!g) return;
  g.querySelectorAll(".card[data-p] .wicon i").forEach(el=>{
    const card=el.closest(".card"), m=card && S.metaMap[card.dataset.p];
    el.textContent=(m&&m.w&&m.h)?(m.w+"×"+m.h):"";
  });
}

/* 卡片上的标记角标（网格）／列表视图则并进「标签」列 */
function syncCardMark(path, state){
  const g=$("#grid"); if(!g) return;
  const el=[...g.querySelectorAll("[data-p]")].find(c=>c.dataset.p===path);
  if(!el) return;
  syncCardMarkEl(el, state);
}
/* 全量同步：一次遍历卡片、按 MARK_MAP 查表。
   此前 loadStats 对每个已标记路径各扫一遍网格 = O(标记数 × 卡片数)，索引构建期每 2 秒一次。 */
function syncAllCardMarks(){
  const g=$("#grid"); if(!g) return;
  for(const el of g.querySelectorAll("[data-p]")) syncCardMarkEl(el, MARK_MAP[el.dataset.p]);
}
function syncCardMarkEl(el, state){
  const path=el.dataset.p;
  if(LIST_VIEW){
    const cell=el.querySelector(".lv-tags"); if(!cell) return;
    const it=(S.view||[]).find(x=>x.path===path);
    fillLvTags(cell, it||{path, tags:[]});   // V4：标记走图标，不再回写 ⚑ 字符
    return;
  }
  let b=el.querySelector(".mkb");
  if(!state){ if(b) b.remove(); return; }
  if(!b){ b=document.createElement("span"); b.className="mkb"; el.appendChild(b); }
  /* 图标是常量（靠 innerHTML 挂 svg），标签走 textContent —— 两者分开，标签不进 HTML 解析。
     ⚠ 这里是卡片的第二个渲染点：改了 makeCard 的角标就必须同步改这里。 */
  b.innerHTML=ico("flag");
  b.appendChild(document.createTextNode(MARK_LABELS[state]||""));
  b.dataset.tip="已标记："+(MARK_LABELS[state]||state);
}

/* 设置 / 取消标记：同步内存映射、角标、大图下拉与数量 */
async function setMark(it, state){
  if(!it || !it.path) return;
  const j=await api("/api/mark",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({path:it.path, state:state||"none"})}).catch(e=>({error:e.message}));
  /* P1-4：后端现在会因「没落盘」返回 ok:false —— 这里必须**先判失败再动本地状态**，
     否则角标与 MARK_MAP 会被改成磁盘上并不存在的状态（刷新就消失，且过程中
     syncCardMark 已经把角标画上去了，用户看到的是一次「成功」假象）。 */
  if(j.error || j.ok===false){ toastErr("标记失败", j.error||"写入未落盘"); return; }
  const st=j.state||"";
  if(st) MARK_MAP[it.path]=st; else delete MARK_MAP[it.path];
  MARK_LOCAL.set(it.path,{st, t:Date.now()});      // 记下本地写入时刻，供 loadStats 防覆盖（见那里注释）
  it.mark=st; it.marklabel=MARK_LABELS[st]||"";
  const sel=$("#lbMark"); if(sel) sel.value=st||"none";
  toast(st?`已标记为「${MARK_LABELS[st]}」`:"已取消标记","ok");
  refreshStatsSoon();
  if(libMode==="marked") loadLibView("marked", it.path);    // 已标记视图：状态变化即时重排（大图保持指向同张）
  else syncCardMark(it.path, st);
}

/* 7. filmstrip:大图打开/切换时渲染当前视图前后缩略图 */
function buildFilm(){
  const film=$("#lbFilm"); if(!film) return;
  if(!lb.classList.contains("on")) return;
  if(S.solo){ film.innerHTML=""; return; }   // 单图模式：没有「前后邻居」可翻
  const idx=S.idx, v=S.view; if(idx<0||!v) return;
  const start=Math.max(0,idx-8), end=Math.min(v.length,idx+12);
  let h='<div class="fsp"></div>';
  for(let i=start;i<end;i++){
    const it=v[i];
    if(!it||(it.type!=="image"&&it.type!=="video")) continue;
    const med = it.type==="image"
      ? '<img src="'+(S.hasThumb?thumbURL(it):fileURL(it.path,it))+'" alt="">'
      /* V47（2026-10-08）：视频格子**渲染真首帧**，不再是「黑底 + 播放图标」。
         原因：后端 /api/thumb 只认图片后缀（server.py 的 thumb_for 里 `not in IMAGE_EXTS`
         直接返回 None），视频请求一律 404；而这里此前压根没挂过 <video>，只放了个图标 ⇒
         格子必然是「纯黑 + 半透明播放按钮」（黑底来自 #lbFilm .f 的 --canvas 底色）。
         做法：挂 <video preload="metadata" muted> + **`#t=0.001` 媒体片段** —— 让浏览器
         直接 seek 到第 1 毫秒解码出首帧，比「JS 里设 currentTime 再等 seeked」稳
         （后者在 loadeddata 之前设值会被忽略，要退回 0 帧）。0.001 而不是 0：部分编码器
         在第 0 帧给的是空白关键帧。
         muted + 无 controls + playsInline：纯展示，不参与播放（点格子仍是 openLb 翻页）。
         ⚠️ 不做懒加载观察器：胶片条最多 ~20 格且**只在选中项附近**（前后各 8/12），
            总量小、且用户已经在看大图，留几个 metadata 请求可接受（主网格那边有 vObs 管）。 */
      : '<video preload="metadata" muted playsinline src="'+fileURL(it.path,it)+'#t=0.001"></video>';
    /* 内容安全（V2.11.1）：NSFW 缩略图同样上模糊（「显示」档除外）。
       data-p 供 nsfwPaintCard 在大图里揭开/盖回时同步 —— 隐藏模式下
       NSFW 早被 nsfwFilter 从 S.view 里剔掉了，胶片条天然不会出现它们。 */
    const nsfwCls = isNsfw(it) && NSFW_MODE!=="show" ? " nsfw" : "";
    const revCls = nsfwCls && NSFW_REVEAL.has(it.path) ? " revealed" : "";
    h+='<div class="f'+(i===idx?" on":"")+nsfwCls+revCls+'" data-i="'+i+'" data-p="'+esc(it.path)+'" data-tip="'+esc(it.name)+'">'+med+'</div>';
  }
  h+='<div class="fsp"></div>';
  film.innerHTML=h;
  film.querySelectorAll(".f").forEach(el=>el.onclick=()=>{ const n=+el.dataset.i; if(!isNaN(n)) openLb(n); });
  centerFilmOn();
}

/* 8. 轻量轮询:原 JS 状态无事件,检测变化后刷新左栏/胶片条
   C-P6（批次 C，2026-10-06）三处改造：
   ① **标签签名不再每 tick 无条件做 O(N) 拼接**。原实现每 350ms 执行
      `(SUGGEST_TAGS||[]).map(t=>t.name+":"+t.count).join(",")` —— 每秒 2.86 次 ×
      O(标签数) 的字符串分配（本票唯一没被消除的规模依赖开销）。
      现在先比**数组引用本身**：tags.js 的 4 个写入点（:6/:410/:427/:558）都是
      「整组重建后重新赋值」，引用一变即内容一变 ⇒ 引用相同就直接跳过，零分配。
   ② 页面不可见时**不计时**（保留状态令牌，回到前台立刻补对一次）。
      长期挂后台 / 长时间看大图时没必要每 350ms 重建三处导航 DOM。
   ③ 补 clearInterval（pagehide 释放）—— 原实现是全站唯一一个没有清理的 setInterval
      （其余 7 处都有守卫）。 */
let _prevPath="",_prevIdx=-2,_prevFavCats=-1,_prevFilter="",
    _prevTagSig="",_prevTagRef=null,_prevTagSigApplied="\u0000";   /* 首帧必须触发一次 renderTagNav */
function _lightTick(){
  if(document.hidden) return;              /* C-P6 ②：后台不跑 */
  const p=(S.path||"")+(favMode?"·fav":"");
  const fc=(S.favCats||[]).length;
  const fl=S.filters.type;                 // H2（2026-10-07）：状态来自 S.filters（原 `#filter` 已删）
  if(p!==_prevPath||fc!==_prevFavCats||fl!==_prevFilter){
    _prevPath=p; _prevFavCats=fc; _prevFilter=fl;
    buildSideNav(); buildFavNav();
  }
  /* C-P6 ①：引用未变 ⇒ 内容未变，连 join 都不做 */
  const tref = SUGGEST_TAGS || null;
  if(tref!==_prevTagRef){
    _prevTagRef = tref;
    _prevTagSig = (tref||[]).map(t=>t.name+":"+t.count).join(",");
  }
  if(_prevTagSig!==_prevTagSigApplied){ _prevTagSigApplied=_prevTagSig; renderTagNav(); }
  if(S.idx!==_prevIdx){ _prevIdx=S.idx; buildFilm(); }
}
const _lightTimer = setInterval(_lightTick, 350);
/* 回到前台时立即对一次，避免切回来时导航与胶片条滞后才更新 */
document.addEventListener("visibilitychange", ()=>{ if(!document.hidden) _lightTick(); });
/* C-P6 ③：页面卷起来时释放定时器（长时间看图不占一个轮询） */
window.addEventListener("pagehide", ()=>clearInterval(_lightTimer));

/* 9. 包装 openLb:打开大图时刷新维度 + 胶片条 + 详情面板默认开 */
const _openLbOrig=window.openLb;
window.openLb=function(i, solo){     // ⚠️ 第二个参数必须透传：丢了它单图会被当库内条目打开
  toggleSide(false);
  const r=_openLbOrig(i, solo);
  const dim=$("#lbDim");
  if(dim){
    const ih=S.curMeta&&S.curMeta.meta&&S.curMeta.meta.IHDR;
    dim.textContent=ih||"";
  }
  setTimeout(()=>{ buildFilm(); if(window._renderTagPanelOnOpen) _renderTagPanelOnOpen(); },120);   // 等原逻辑填入 stage
  return r;
};
/* 关大图时清胶片 */
const _closeLbOrig=window.closeLb;
window.closeLb=function(){
  const r=_closeLbOrig();
  const film=$("#lbFilm"); if(film) film.innerHTML="";
  return r;
};

/* ==================================================================
   V1 独立设置面板（批次 2）
   原先「外观与操作」整块塞在左栏底部 —— 8 个分区里它一个就装了 7 个控件，
   而且和高频变化的「去哪儿看 / 看哪些」混在同一条滚动动线上。
   现在搬进右侧抽屉，按 外观 / 行为 / 数据 / 关于 四页签归类：左栏瘦身为 7 个纯浏览分区，
   今后任何新设置都有地方放，左栏不再变长。控件 id 全部沿用（绑定按 id 走，搬家不动绑定）。
   ================================================================== */
(function setupSettingsPanel(){
  const mask=$("#setMask"), panel=$("#setPanel");
  if(!mask || !panel) return;
  mask.hidden=false;                 // hidden 只作初始态；开合一律走 .on 才能过渡
  let lastFocus=null;
  let sysTimer=0;                    // F26 索引进度轮询（面板关掉 / 建完即停）
  const ddState={dir:"",want:"user",targets:{}};   // F28 数据目录位置（真值只来自 /api/sysinfo）
  const isOpen=()=>mask.classList.contains("on");

  function open(){
    if(isOpen()) return;
    lastFocus=document.activeElement;
    mask.classList.add("on");
    syncSysinfo();                   // 数据 / 关于页每次打开都重新取真值
    setTimeout(()=>{ const t=panel.querySelector(".set-tab.on")||panel.querySelector(".set-tab");
      if(t) t.focus(); },60);
  }
  function close(){
    if(!isOpen()) return;
    clearTimeout(sysTimer);          // 面板关了就别再轮询（F26）
    mask.classList.remove("on");
    /* G2·V16③：**先同步解冻再归还焦点** —— 触发元素在 #content/#main 里，
       G1 的 inert 没撤时它聚焦不了，`focus()` 会静默失败（实测焦点落回 BODY）。
       注意 MutationObserver 是下一拍才撤 inert，等不起，必须同步调。 */
    if(window.mvSyncInert) window.mvSyncInert();
    if(lastFocus && lastFocus.focus) lastFocus.focus();
    lastFocus=null;
  }
  window.mvOpenSettings=open;        // 供其它模块调用（例如将来快捷键呼出）
  window.mvCloseSettings=close;

  /* ---- 页签：只切 class，不重建 DOM —— 页内控件状态天然保留 ---- */
  const tabs=[].slice.call(panel.querySelectorAll(".set-tab"));
  const pages=[].slice.call(panel.querySelectorAll(".set-page"));
  function showTab(name){
    for(const t of tabs){
      const on=t.dataset.tab===name;
      t.classList.toggle("on",on); t.setAttribute("aria-selected",String(on));
      t.tabIndex = on ? 0 : -1;                  // V42：roving 与 aria-selected 同步
    }
    for(const p of pages) p.classList.toggle("on", p.dataset.page===name);
    const b=panel.querySelector(".set-body"); if(b) b.scrollTop=0;
    if(name==="data") syncSysinfo();   // 切到数据页即取一次真值（F26：进度/占用/缓存）
    if(name==="nsfw") nsfwLoadStatus(); // 切到内容安全页取一次真值（引擎/档位/阈值/进度）
  }
  for(const t of tabs) t.onclick=()=>showTab(t.dataset.tab);

  /* G2·V42：WAI-ARIA 页签键盘模型 —— 未选中页签 tabindex=-1（否则「半套 ARIA」做不成），
     所以必须同时提供 ←/→ 与 Home/End，否则页签从键盘上「消失」了。 */
  function tabRoving(){
    tabs.forEach(t=>{ t.tabIndex = t.classList.contains("on") ? 0 : -1; });
  }
  tabs.forEach((t,i)=>{
    t.onkeydown=e=>{
      let n=-1;
      if(e.key==="ArrowRight") n=(i+1)%tabs.length;
      else if(e.key==="ArrowLeft") n=(i-1+tabs.length)%tabs.length;
      else if(e.key==="Home") n=0;
      else if(e.key==="End") n=tabs.length-1;
      if(n<0) return;
      e.preventDefault();
      showTab(tabs[n].dataset.tab);
      tabs[n].focus();
    };
  });
  tabRoving();

  const bs=$("#btnSettings"); if(bs) bs.onclick=open;

  /* H5（2026-10-06）：顶栏「工具▾」下拉拆掉，查重 / 出图统计的入口落在「数据」页。
     两项都会开一个全屏弹窗（bigDialog），若抽屉还盖在上面就成了「两层浮层叠着」，
     而且 Esc 的归属也含糊 —— 所以**先关抽屉，再开弹窗**。
     真正的实现仍绑在隐藏宿主 #btnDupes / #btnStats 上（见 js/extras.js），
     这里只转发一次 click ⇒ 不改任何业务逻辑，只是换了个门。
     ⚠️ 关闭要放在 click 处理器里同步做：close() 会把焦点还给 #btnSettings，
        弹窗随后接管焦点，顺序天然正确。 */
  [["setDupes", "btnDupes"], ["setStats", "btnStats"]].forEach(([btnId, hostId])=>{
    const b=$("#"+btnId), h=$("#"+hostId);
    if(b && h) b.onclick=()=>{ close(); h.click(); };
  });
  mask.addEventListener("click",e=>{ if(e.target===mask) close(); });   // 点遮罩空白处关闭
  /* Esc 关闭 + 焦点陷阱：面板打开时 Tab 只在面板内循环，不逃到背后的网格上 */
  document.addEventListener("keydown",e=>{
    if(!isOpen()) return;
    /* ε-2（2026-09-20）：**上层还有浮层时，Esc 让给它**。
       本监听注册在**捕获阶段**（焦点陷阱需要），原来的写法一进来就 stopPropagation ⇒
       抽屉开着时 Esc 到不了**任何**别的处理器。于是：数据页点「清除单图暂存」等
       二次确认弹出后按 Esc —— 关不掉上面那层，反而把底下的抽屉关走了，
       用户得重新打开抽屉找回刚才的位置。
       （⚠️ 口径：confirmDialog 建的节点是 `#catInputMask`，不是 `.bigMask`；
         `mvTopOverlay()` 两个族都认，这里不必区分。）

       修法是加一道「我是不是最顶层」的判断，真相来源复用 app.js 的 `mvTopOverlay()`
       （与 attachInert / attachDynamicOverlayTrap 同一份浮层清单）。
       ⚠️ 只在**有上层浮层**时 return；没有上层就照旧 stopPropagation 并关闭，
       不改变原有的「Esc 关抽屉 + Tab 只在面板内循环」行为。 */
    if(e.key==="Escape"){
      if(window.mvTopOverlay && window.mvTopOverlay()!==mask) return;
      e.stopPropagation(); e.preventDefault(); close(); return;
    }
    if(e.key!=="Tab") return;
    /* ε-2（F2 P2-1 **下半段**）：Tab 同样要让给上层 —— 光给 Esc 让位修不全这条。
       抽屉的焦点陷阱会把焦点从上面的确认框**搬回抽屉面板**，于是那个确认框
       变成收不到任何键盘事件的孤儿（Esc 分支已经让位了，可 Tab 还在抢），
       而它恰好是「清除缩略图缓存」「清除单图暂存」这类不可逆操作 ——
       用户被迫改用鼠标点「取消/删除」，误触风险实打实。
       判据与 Esc 那道共用 mvTopOverlay()：只有上层真的没浮层时才照旧循环。 */
    if(window.mvTopOverlay && window.mvTopOverlay()!==mask) return;
    const f=[].slice.call(panel.querySelectorAll("button,select,input,[tabindex]"))
      .filter(el=>!el.disabled && el.tabIndex>=0 && el.offsetParent!==null);
    if(!f.length) return;
    const first=f[0], last=f[f.length-1];
    if(e.shiftKey && document.activeElement===first){ e.preventDefault(); last.focus(); }
    else if(!e.shiftKey && document.activeElement===last){ e.preventDefault(); first.focus(); }
  },true);

  /* ---- V21 密度档位：三档预设 ⇄ 滑块 ----
     滑块是唯一真相，档位只是它的三个快捷键 —— 两套状态各记一份必然打架。
     当前值不落在三档上时三档全灭（只显示滑块数值），不伪造「选中」。 */
  const preset=$("#densPreset");
  function syncPreset(){
    if(!preset || !window.mvGetDens) return;
    const v=window.mvGetDens();
    let hit=false;
    for(const b of preset.querySelectorAll(".segbtn")){
      const on=(+b.dataset.d===v);
      b.classList.toggle("on",on); b.setAttribute("aria-pressed",String(on));
      if(on) hit=true;
    }
    preset.dataset.free = hit ? "" : "1";
  }
  if(preset){
    for(const b of preset.querySelectorAll(".segbtn"))
      b.onclick=()=>{ if(window.mvSetDens) window.mvSetDens(+b.dataset.d); syncPreset(); };
    const dr=$("#densRange");
    if(dr){ dr.addEventListener("input",syncPreset); dr.addEventListener("change",syncPreset); }
    syncPreset();
  }

  /* ---- 数据 / 关于页：值全部来自 /api/sysinfo 与 /api/bridge/status，前端不自造 ---- */
  const fmtBytes=b=>{
    b=b||0;
    if(!b) return "0 B";
    const u=["B","KB","MB","GB"]; let i=0, v=b;
    while(v>=1024 && i<u.length-1){ v/=1024; i++; }
    return (i===0 ? String(v) : v.toFixed(v<10?1:0)) + " " + u[i];
  };
  const setTxt=(id,t)=>{ const el=document.getElementById(id); if(el) el.textContent=t; };
  async function syncSysinfo(){
    let j;
    try{ j=await api("/api/sysinfo"); }
    catch(e){ setTxt("setDataDir","（读取失败）"); return; }
    setTxt("setDataDir", j.data_dir||"—");
    /* P1-3：真值只来自后端（settings.json 是权威，环境变量还能再覆盖一层），
       前端不自造。拿到后就地回填并刷新那句说明。 */
    if(tkInp && j.trash_keep_days!=null && document.activeElement!==tkInp){
      tkInp.value=j.trash_keep_days;
    }
    renderTrashKeep();
    setTxt("setVer", "V"+(j.version||""));
    setTxt("setAboutVer", "V"+(j.version||"—"));
    setTxt("setPil", j.has_pil ? "已就绪" : "未安装（缩略图与查重不可用）");
    /* ---- F28 数据目录位置：分段控件反映**磁盘上的选择** ----
       pending=True 时磁盘选择已改、但这个进程还跑在旧目录（切换后未重启），此时按
       磁盘选择点亮、并在说明里点明「重启后生效」—— 让界面跟着用户刚才的操作走，
       而不是跟着一个即将失效的进程状态走，否则用户会以为没切成功。 */
    const dd=j.datadir||{};
    for(const id of ["migRun","migOldRoot"]){ const el=$("#"+id); if(el){ el.disabled=dd.root_recovery_available===false; if(el.disabled) el.dataset.tip="新版资料库按媒体身份恢复，暂不支持旧路径推断迁移"; } }
    ddState.dir=dd.dir||"";
    ddState.targets=dd.targets||{};
    const ddWant=(dd.pending ? (dd.flag_exists?"portable":"user") : (dd.mode||"user"));
    ddState.want=ddWant;
    const ddSeg=$("#ddMode");
    if(ddSeg){
      [].forEach.call(ddSeg.querySelectorAll(".segbtn"),
        b=>{ b.classList.toggle("on", b.dataset.m===ddWant); b.disabled=dd.switch_available===false && b.dataset.m!==ddWant; });
    }
    /* 文案要如实说清「东西放哪、什么会丢」——
       用户目录在系统盘：重装系统 / 换机会丢，但换程序版本、挪程序位置都不受影响；
       程序目录跟着程序走：拷到 U 盘就能带走，反过来删了 / 挪了程序数据也跟着走。
       两边各有代价，不存在「哪种绝对安全」，所以不写「不受影响」这种一边倒的话。 */
    /* B7（2026-10-07 文案梳理）：两种模式的取舍已写在按钮 tooltip 上，此处不再逐字重复，
       只留一行「当前在哪 / 是否需要重启」的状态。
       ⚠️ 措辞必须含「用户目录」/「程序目录」—— tests/f28v17_check.py 的 3b 断言锚它。 */
    const ddHint=$("#ddHint");
    if(ddHint) ddHint.textContent = dd.pending
      ? "已切换，重启程序后生效（当前这次会话仍在旧目录）。"
      : (ddWant==="portable"
          ? "当前放在程序目录（cmv-data，随程序文件夹一起走）。"
          : "当前放在用户目录（系统盘的用户配置里）。");

    /* ---- B1-3：数据目录写不进去 ⇒ 必须红字说出来，不能只留日志 ----
       save_favs / save_tags / _save_settings_locked 失败原本只往数据目录里的
       日志文件写一行 —— 而那个目录恰好就是写不进去的那个。于是常用目录、
       上次目录、删除偏好全都「看着改了、重启回原样」，用户无从察觉。
       data_write 由后端给：ok=False 是**最近一次实际写盘**失败，
       dir_writable=False 是**启动自检**就没写进去。任一为假都要告警。 */
    const dw=j.data_write||{};
    const ddWarn=$("#ddWarn");
    if(ddWarn){
      const bad = dw.ok===false || dw.dir_writable===false;
      ddWarn.hidden = !bad;
      if(bad){
        ddWarn.textContent =
          "数据目录写不进去" + (dw.error ? "（"+dw.error+"）" : "") +
          "：设置 / 收藏 / 标签的改动会在重启后丢失。"
          + "请检查该目录是否只读、或所在盘是否掉线。";
      }
    }

    /* ---- F26 索引进度：done/total 来自后端 _index_builder，前端只负责画 ---- */
    const ix=j.index||{}, pr=ix.progress||{};
    const building=!!pr.building, done=pr.done|0, total=pr.total|0;
    const prog=$("#setIdxProg"), fill=$("#setIdxFill");
    const pct = total ? Math.max(0,Math.min(100,Math.round(done*100/total))) : (building?0:100);
    if(prog){
      prog.classList.toggle("building", building);
      prog.classList.toggle("ready", !building && !!ix.count);
    }
    /* 进行中至少给 2% 的可见宽度：total 还没数出来时（扫描阶段）条子不能是空的，
       否则用户看到「正在建立」配一条空槽，会以为卡死了。 */
    if(fill) fill.style.width = (building ? Math.max(pct,2) : (ix.count?100:0)) + "%";
    setTxt("setIdxState", building ? "正在建立提示词索引…"
      : (ix.count ? "已就绪（有新增时会自动补充）" : "尚未建立索引"));
    setTxt("setIdxNum", total ? (done+" / "+total) : ((ix.count||0)+" 条"));

    /* ---- 占用：缩略图 / 索引 / 回收站（索引行取的是 IDX_FILE 实际大小） ---- */
    const rows=$("#setRows");
    if(rows){
      /* 「单图暂存」= 「打开图片」复制进来的临时副本（上限 20 张，哈希命名同图复用） */
      const data=[[ "缩略图缓存", j.thumbs ], [ "提示词索引", ix ], [ "回收站", j.trash ],
                  [ "单图暂存", j.solo ]];
      rows.innerHTML=data.map(function(row){
        const l=row[0], o=row[1]||{};
        const n=(o.count!=null) ? (" · "+o.count+" 个") : "";
        return '<div class="set-row"><span>'+esc(l+n)+'</span><b>'+esc(fmtBytes(o.bytes))+'</b></div>';
      }).join("");
    }


    const hint=$("#setDataHint");
    if(hint) hint.textContent = ix.root
      ? ("当前索引根目录：" + ix.root)
      : "尚未建立索引（搜索提示词或点开「工作流」筛选即会构建）。";

    /* 建索引是慢活：面板开着才跟着刷，关掉或建完立即停 —— 空转请求没意义（F26） */
    clearTimeout(sysTimer);
    if(isOpen() && building) sysTimer=setTimeout(syncSysinfo, 700);
  }
  (async function(){                                  // 关于页：桥接安装/在线状态
    try{
      const b=await api("/api/bridge/status");
      setTxt("setBridge", b.installed
        ? (b.active ? "已安装 · 页面在线" : "已安装 · 页面未打开")
        : "未安装（在 ComfyUI 页面里点「安装桥接」）");
    }catch(e){ setTxt("setBridge","—"); }
  })();
  setTxt("setDataDir","—");

  /* ---- P1-3 回收站自动清理：保留天数可查、可配、可关（0=关闭）----
     此前是写死的 30 天常量，到点就在启动时静默永久删除，用户既不知道也无从关闭。
     这里不新增一套「设置项」概念，直接复用 /api/prefs（delete_mode 已在用）。 */
  const tkInp=$("#trashKeepDays"), tkHint=$("#trashKeepHint");
  function renderTrashKeep(){
    if(!tkHint) return;
    const v=parseInt(tkInp && tkInp.value, 10);
    if(!isFinite(v)){ tkHint.textContent="请填 0-3650 的整数。"; return; }
    tkHint.textContent = v===0
      ? "已关闭：回收站里的文件不会被自动删除，只能手工彻底删除。"
      : `超过 ${v} 天的条目会在程序启动时被自动彻底删除（不可恢复）。`;
  }
  if(tkInp){
    tkInp.addEventListener("input", renderTrashKeep);
    tkInp.addEventListener("change", async()=>{
      const v=parseInt(tkInp.value,10);
      if(!isFinite(v) || v<0 || v>3650){
        toast("保留天数需要是 0-3650 的整数","err"); syncSysinfo(); return;
      }
      const j=await api("/api/prefs",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({trash_keep_days:v})}).catch(e=>({error:e.message}));
      if(j.error){ toastErr("回收站清理设置保存失败", j.error); syncSysinfo(); return; }
      tkInp.value=j.trash_keep_days;
      renderTrashKeep();
      toast(v===0?"已关闭回收站自动清理":`回收站保留天数：${v} 天`,"ok");
    });
  }

  const mk=(id,fn)=>{ const b=document.getElementById(id); if(b) b.onclick=fn; };
  mk("setOpenDir", async()=>{
    const j=await api("/api/open_data",{method:"POST"}).catch(e=>({error:e.message}));
    if(j.error) toast("打开失败："+j.error,"err");
  });
  mk("setReindex", ()=>{
    confirmDialog("重建提示词索引",
      "会丢弃现有索引缓存并重新扫描当前根目录（不动任何原图）。"
      +"重建期间按提示词搜索会暂时为空，大目录可能要数十秒。", async()=>{
      const j=await api("/api/reindex",{method:"POST"}).catch(e=>({error:e.message}));
      if(j.error){ toast(j.error,"err"); return; }
      toast("已开始重建索引","ok");
      setBotState("busy","正在重建提示词索引…",null,true);
      statsBuildAt=0; loadStats();                    // 立刻接上进度，底栏开始报数（V3）
      setTimeout(syncSysinfo, 2500);
    });
  });
  mk("setClearThumb", ()=>{
    confirmDialog("清除缩略图缓存",
      "会删除本地缩略图缓存文件（不动原图），下次浏览时按需重新生成。", async()=>{
      const j=await api("/api/cache_clear",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({what:"thumbs"})}).catch(e=>({error:e.message}));
      if(j.error){ toast(j.error,"err"); return; }
      toast("已清除缩略图缓存（"+(j.removed||0)+" 个文件）","ok");
      syncSysinfo();
    });
  });
  mk("setClearSolo", ()=>{
    confirmDialog("清除单图暂存",
      "会删除「打开图片」复制进来的临时副本（不动原图、不动库内文件）。"
      +"已打开的那张如果还开着，界面不受影响；下次打开会重新复制一份。", async()=>{
      const j=await api("/api/cache_clear",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({what:"solo"})}).catch(e=>({error:e.message}));
      if(j.error){ toast(j.error,"err"); return; }
      toast("已清除单图暂存（"+(j.removed||0)+" 个文件）","ok");
      syncSysinfo();
    });
  });
  /* F26 第三个出口：强制重扫当前目录。
     与「重建索引」的分工 —— 重建丢索引表（贵、期间搜不到），重扫只丢指纹与节流（便宜、
     不动索引表），专治「目录变了界面没动 / 新图搜不到」。
     V2.10.7：实现已抽到 extras.js 的 mvForceRefresh（顶栏「刷新」按钮与 R 键同一份）——
     早先这里自己写过一遍，两处逻辑迟早漂移。 */
  mk("setRescan", ()=>{ if(typeof mvForceRefresh==="function") mvForceRefresh(); });
  /* ---- F28 数据目录位置：两档切换（必须问「要不要把现有数据搬过去」）----
     静默切换的后果是用户以为收藏 / 标签 / 索引全丢了，所以这里**不给静默路径**：
     每次切换都弹三选一（取消 / 不复制 / 复制并切换）。 */
  const ddSegEl=$("#ddMode");
  /* P1-16：切数据目录是「长时间 + 有写副作用」的操作（可能搬几个 G 的文件），
     此前唯一的守卫是 ddState.want —— 它要等**服务端返回后**才被改写，
     所以用户在等待期间连点两次分段按钮，就会并发发两次 /api/datadir：
     两边同时往目标目录写 favorites.json / tags.json，后写的那份会截断前一份。
     用一个 in-flight 标志把窗口期堵死：进行中直接拒绝并提示，等收尾才放开。 */
  let ddBusy=false;
  async function ddSwitch(mode){
    if(ddBusy){ toast("上一次数据目录操作还在进行中，请稍候","err"); return; }
    if(!mode || mode===ddState.want) return;
    ddBusy=true;
    ddSegEl && ddSegEl.classList.add("busy");
    try{ await ddSwitchInner(mode); }
    finally{
      ddBusy=false;
      ddSegEl && ddSegEl.classList.remove("busy");
    }
  }
  async function ddSwitchInner(mode){
    /* ε-13（§6.6）：/api/sysinfo 还没回来（ddState.dir 为空）时不许切换 ——
       此前弹窗里目标路径显示成空串、提交的 mode 却照发后端，用户在
       「读取失败」状态下作出的目录决策是盲的。先等真值到手。 */
    if(!ddState.dir){
      toast("数据目录信息还没读到，稍候再试","err");
      syncSysinfo();
      return;
    }
    if(!mode || mode===ddState.want) return;
    const tgt=(ddState.targets||{})[mode]||"";
    const dlg=bigDialog("切换数据目录");
    dlg.body.innerHTML=
      '<div style="font-size:calc(13px*var(--fsx));line-height:var(--lh-body)">'
      +'数据目录将改为：<br><code style="font-family:var(--mono);font-size:calc(12px*var(--fsx));'
      +'overflow-wrap:anywhere">'+esc(tgt)+'</code></div>'
      +'<div style="margin-top:calc(10px*var(--fsx));font-size:calc(12.5px*var(--fsx));'
      +'color:var(--tx3);line-height:var(--lh-loose)">'
      +'设置 / 收藏 / 标签 / 最近使用 / 标记 / 索引 / 缩略图缓存 / 回收站都在数据目录里。'
      +'要不要把现在的这一份<b>复制</b>过去？（目标里同名且内容相同的文件会跳过；'
      +'内容不同的，旧文件先备份为 .cmv-conflict-* 再写入，不覆盖）</div>'
      +'<div style="display:flex;justify-content:flex-end;flex-wrap:wrap;'
      +'gap:calc(8px*var(--fsx));margin-top:calc(16px*var(--fsx))">'
      +'<button id="ddCancel" type="button">取消</button>'
      +'<button id="ddNo" type="button">不复制，直接切换</button>'
      +'<button class="primary" id="ddYes" type="button">复制并切换</button></div>';
    dlg.body.querySelector("#ddCancel").onclick=dlg.close;
    dlg.body.querySelector("#ddNo").onclick=()=>ddGo(mode,false,dlg);
    dlg.body.querySelector("#ddYes").onclick=()=>ddGo(mode,true,dlg);
  }
  async function ddGo(mode, migrate, dlg){
    if(dlg) dlg.close();
    setBotState("busy", migrate?"正在迁移数据目录…":"正在切换数据目录…", null, true);
    let j=await api("/api/datadir",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({mode:mode, migrate:!!migrate})})
      .catch(e=>({error:e.message}));
    /* ε-12（§6.3）：迁移已挪后台线程 —— 拿到 migrating 就轮询
       /api/datadir_status 到 done/error，再走统一的收尾分支。
       好处：迁移几个 G 时请求线程不再被独占，浏览器 fetch 也不会
       挂到代理/服务器超时；UI 侧 busy 状态由本循环维持，语义不变。 */
    if(j.migrating){
      const t0=Date.now();
      for(;;){
        await new Promise(r=>setTimeout(r,500));
        const st=await api("/api/datadir_status").catch(e=>({error:e.message}));
        if(st.error){ j=st; break; }
        if(st.state==="done"||st.state==="error"){ j=st.result||{error:"迁移失败"}; break; }
        if(Date.now()-t0>1800000){ j={error:"迁移超过 30 分钟仍未完成，请查看服务日志"}; break; }
      }
    }
    if(j.error){
      setBotState("err","数据目录切换失败",null);
      toastErr("数据目录切换失败", j.error);
      syncSysinfo();
      return;
    }
    const mv=j.migrated||{};
    if(j.same){
      toast("数据目录未变化");
    }else if(j.partial){
      /* P0-2：有文件没搬过去 —— 绝不能报「已切换…复制 N 个文件」让用户以为搬完了。
         走警告色 + 明确的「数据还在原目录」，避免用户重启后以为收藏/设置丢了。 */
      const where=(mode==="portable")?"程序目录":"用户目录";
      setBotState("warn","数据目录已切换，但有文件未复制",null,true);
      toastErr("已切换为"+where+"，但 "+mv.failed+" 个文件未能复制",
               j.warning||"这些文件仍在原目录，请手工处理后再重启");
    }else{
      const where=(mode==="portable")?"程序目录":"用户目录";
      const how=migrate
        ? ("，复制 "+mv.files+" 个文件"+(mv.skipped?("（跳过 "+mv.skipped+" 个已存在）"):"")
           +(mv.conflicts?("，"+mv.conflicts+" 个同名旧文件已备份为 .cmv-conflict-*"):""))   /* ε-11 §6.4 */
        : "，未复制数据";
      toast("已切换为"+where+how,"ok");
    }
    if(j.restart_required){
      /* 那 8 个路径常量在进程启动时算好，运行中改不了 —— 如实告知并给一键重启。
         ε-6 E6-6：文案**必须同时说清「重启前的新改动仍写进原目录」**。
         后端 `datadir_switch` 只写标记、不改内存里的 DATA_DIR（这是**有意**的：
         半个进程用旧目录、半个用新目录比「重启后统一生效」糟得多），
         但此前只说「重启后生效」⇒ 用户很容易以为**此刻起就已经写新目录**了。
         于是这段时间打的标签/收藏/标记其实落在旧目录，重启后「看起来丢了」，
         而它们其实好好躺在原目录里 —— 「以为是新目录、其实是旧目录」这类误解
         正是最该避免的（P0-2 同款）。 */
      const go=()=>{ if(window.mvRestart) window.mvRestart(); };
      setBotState("warn",
                  j.partial?"有文件未复制，仍重启到新目录？"
                           :"数据目录已切换，重启前的新改动仍写入原目录",
                  {label:"立即重启",fn:go},true);
      toastAction("重启前的新改动仍写入原目录，建议立即重启","立即重启",go);
    }else{
      setBotState("idle","已就绪",null,true);
    }
    syncSysinfo();
  }
  if(ddSegEl) ddSegEl.addEventListener("click",e=>{
    const b=e.target.closest(".segbtn");
    if(b) ddSwitch(b.dataset.m);
  });
  mk("setKeys", ()=>{ close(); if(window.mvOpenKeys) window.mvOpenKeys(); });

  setBotState("idle","已就绪",null,true);             // 初始态归位（V3）

  /* ---- V2 回收站角标：把「里面有 N 件」摆在顶栏按钮上（省掉「点开才知道」）----
     每 30 秒轮询一次（本地 JSON 列表，开销可忽略）；删除/还原后由各操作点主动调一次。 */
  window.mvSetTrashBadge=function(n){
    const b=$("#trashBadge"), btn=$("#btnTrash");
    if(!b) return;
    n=n|0;
    if(n>0){
      b.hidden=false; b.textContent = n>99 ? "99+" : String(n);
      if(btn) btn.classList.add("has-badge");
    }else{
      b.hidden=true; b.textContent="";
      if(btn) btn.classList.remove("has-badge");
    }
  };
  let badgeTok=0;
  window.mvUpdateTrashBadge=async function(){
    const tok=++badgeTok;
    let n;
    try{ const j=await api("/api/trash"); n=((j&&j.items)||[]).length; }
    catch(e){ return; }
    if(tok!==badgeTok) return;
    window.mvSetTrashBadge(n);
  };
  window.mvUpdateTrashBadge();
  // 常驻轮询（故意不 clear）：回收站角标每 30s 对一次数，页面隐藏时跳过。生命期=整个页面。
  setInterval(function(){ if(!document.hidden) window.mvUpdateTrashBadge(); },30000);
})();

/* ==================================================================
   6. 快捷键速查面板（V17）
   原先底栏 ⌨ 弹的是一行 `white-space:nowrap` 的提示条，条目早就排到超宽
   （`/` `←→` `Esc` `F` `T` `[ ]` `Del` `P` `空格` …），再加一条就溢出。
   改成分组卡片：内容写在下面的 KEY_GROUPS 里 —— 加 / 改一条快捷键只动这一处，
   不碰 index.html。`?` 随时唤出，Esc / 点遮罩 / 点 ✕ 关闭。

   本 IIFE 刻意放在 settings.js 末尾：Esc 与 Tab 走**捕获阶段**，而设置抽屉自己也有一套
   捕获监听（Esc 关闭 + 焦点陷阱）。同阶段同节点按注册顺序触发，注册在前的先跑；
   放最后注册才能让「速查面板开着时」的按键由本面板说了算。
   ================================================================== */
(function setupKeysPanel(){
  const mask=$("#hintMask"), panel=$("#hintPanel"), body=$("#hintBody");
  if(!mask || !panel || !body) return;
  mask.hidden=false;                 // hidden 只作初始态；开合一律走 .on 才能过渡

  /* 分组只列**真的绑了键**的项 —— 面板里写一条没实现的快捷键，比不写更糟。
     键名以「显示给人看」为准（←/→ 而不是 ArrowLeft），说明里带上生效前提。 */
  const KEY_GROUPS=[
    {g:"浏览", items:[
      [["/"],          "聚焦搜索框"],
      [["R"],          "刷新当前目录（立即重扫，等同顶栏刷新按钮）"],
      [["Esc"],        "清空搜索（光标在搜索框内时）"],
      [["?"],          "唤出 / 关闭本面板"],
    ]},
    {g:"视图", items:[
      [["T"],          "时间轴 ⇄ 网格"],
      [["N"],          "内容显示方式：隐藏 ⇄ 模糊 ⇄ 显示"],
      [["[","]"],      "时间轴：上一组 / 下一组"],
      [["↑","↓"],      "下拉菜单里移动选择"],
      [["Home","End"], "跳到第一项 / 最后一项"],
    ]},
    {g:"大图", items:[
      [["←","→"],      "上一张 / 下一张"],
      [["Esc"],        "关闭大图"],
      [["P"],          "详情面板"],
      [["F"],          "底部预览栏"],
      [["T"],          "聚焦标签输入"],
      [["Del"],        "删除当前图"],
    ]},
    {g:"视频", items:[
      [["空格"],       "播放 / 暂停"],
      [[",","."],      "上一帧 / 下一帧"],
      [["L"],          "循环播放"],
      [["M"],          "静音"],
    ]},
    {g:"返回与关闭", items:[
      [["Esc"],        "逐层关闭：大图 → 对比 → 批量选择"],
      [["Esc"],        "关闭弹窗 / 设置抽屉 / 本面板"],
      [["Enter"],      "确认（各类弹窗与内联重命名）"],
    ]},
  ];

  body.innerHTML=KEY_GROUPS.map(function(grp){
    const rows=grp.items.map(function(it){
      const keys=it[0].map(function(k){ return "<kbd>"+esc(k)+"</kbd>"; }).join("");
      return '<div class="hint-row"><span class="hint-keys">'+keys+"</span>"
             +"<span>"+esc(it[1])+"</span></div>";
    }).join("");
    return '<div class="hint-grp"><div class="hint-gl">'+esc(grp.g)+"</div>"+rows+"</div>";
  }).join("");

  let lastFocus=null;
  const isOpen=()=>mask.classList.contains("on");
  function open(){
    if(isOpen()) return;
    lastFocus=document.activeElement;
    mask.classList.add("on");
    setTimeout(()=>{ const b=$("#hintClose"); if(b) b.focus(); },60);
  }
  function close(){
    if(!isOpen()) return;
    mask.classList.remove("on");
    if(window.mvSyncInert) window.mvSyncInert();          // G2·V16③：先解冻（同上）
    if(lastFocus && lastFocus.focus) lastFocus.focus();   // 焦点归还触发器
    lastFocus=null;
  }
  window.mvOpenKeys=open;
  window.mvCloseKeys=close;
  window.mvToggleKeys=()=>{ isOpen()?close():open(); };

  $("#hintClose").onclick=close;
  const trig=$("#hintTrig");                            // 底栏 ⌨ 仍然是入口之一
  if(trig){
    trig.onclick=()=>window.mvToggleKeys();
    /* span 挂了 role=button + tabindex=0 就要自己接 Enter/Space —— 只给 onclick 的话
       键盘用户按下去没反应（原生 button 才有这个默认行为）。 */
    trig.addEventListener("keydown",e=>{
      if(e.key==="Enter" || e.key===" "){ e.preventDefault(); window.mvToggleKeys(); }
    });
  }
  mask.addEventListener("click",e=>{ if(e.target===mask) close(); });

  /* 捕获阶段：大图那套按键是同在 document 上的冒泡监听，不拦住的话
     关掉本面板的同时还会顺手把大图一起关掉。 */
  document.addEventListener("keydown",e=>{
    if(isOpen()){
      if(e.key==="Escape" || e.key==="?"){
        e.stopPropagation(); e.preventDefault(); close(); return;
      }
      if(e.key==="Tab"){                                 // 焦点陷阱：面板只有一个焦点位
        e.stopPropagation(); e.preventDefault();
        const b=$("#hintClose"); if(b) b.focus();
      }
      return;
    }
    if(e.ctrlKey || e.metaKey || e.altKey) return;
    const el=e.target, tg=(el.tagName||"").toUpperCase();
    if(tg==="INPUT" || tg==="TEXTAREA" || tg==="SELECT" || el.isContentEditable) return;
    if(e.key==="?"){ e.preventDefault(); open(); }        // Shift+/ —— 与 `/` 搜索不冲突
  },true);
})();


/* ============================================================
   内容安全（V2.11.0）
   ============================================================
   前端只负责三件事：显示方式、把用户操作发给服务端、把进度画出来。
   判定、阈值、缓存、模型全在服务端 —— 前端不做任何推理，也不自己算分。
   ============================================================ */
(function setupNsfw(){
  const segBar = $("#nsfwSeg");          // 工具条上的三态控件（红框那个位置）
  const segSet = $("#nsfwModeSeg");      // 设置页里同一套 —— 两个入口，一个真相
  const ctl = $("#nsfwCtl");             // 标签 + 三档的整组容器（off 状态挂这里）
  let pollTimer = null;
  let scanPulsed = false;                // 「开始检测」脉冲只放一次（首次从未跑过翻到跑过）
  /* 左侧标签的红眼睛：和被遮挡卡片左上角那只同色，扫一眼就知道这排按钮管什么 */
  { const li = $("#nsfwLblIco"); if(li) li.innerHTML = ico("eye"); }
  const NSFW_TITLES = {
    hide:"隐藏：敏感内容从列表里剔除（快捷键 N 循环切换）",
    blur:"模糊：敏感内容显示为毛玻璃，点卡片左上角的眼睛可单独查看（快捷键 N 循环切换）",
    show:"显示：不做任何处理（快捷键 N 循环切换）"};

  /* ---- 显示方式：工具条与设置页两处控件同步 ----
     三种状态，判据各不同（V2.12.6 加的后两态）：

       ready      引擎可用 + 跑过检测  → 正常，三档可点
       undetected 引擎可用 + 没跑过    → **琥珀**提醒「未检测」，点击给去开启的出路
       noengine   引擎不可用           → 整组置灰，红眼也一起**去色变灰**，提示说清原因

     ⚠️ 两个 class 挂在 #nsfwCtl（整组：标签 + 三档）上，不是挂在 seg 上 ——
        只挂 seg 会出现「字是亮的、按钮是灰的」。
     ⚠️ undetected 与 noengine **互斥**，且 ready 优先 —— 三态由同一次调用算出来，
        分两个 if 写容易出现「两个 class 同时挂上」的自相矛盾态。 */
  function nsfwSyncUI(){
    for(const s of [segBar, segSet]){
      if(!s) continue;
      for(const b of s.querySelectorAll(".segbtn")){
        const on = b.dataset.n === NSFW_MODE;
        b.classList.toggle("on", on);
        b.setAttribute("aria-pressed", String(on));
      }
    }
    if(!ctl) return;
    const ready = nsfwReady();
    const noEng = nsfwNoEngine();
    const undet = !noEng && !ready;        // 引擎在、但一次都没跑过
    ctl.classList.toggle("undetected", undet);
    ctl.classList.toggle("noengine", noEng);
    /* 提示语三态各一套，走 data-tip（D2 起全站唯一的提示出口），不写 title。
       回退值也读 data-tip —— 读 b.title 会拿到 undefined，
       等于「引擎不可用 ⇄ 可用」切换一次之后提示就空了。 */
    for(const b of segBar.querySelectorAll(".segbtn"))
      b.setAttribute("data-tip", ready
        ? (NSFW_TITLES[b.dataset.n] || b.getAttribute("data-tip") || "")
        : undet
          ? "尚未检测：先在设置页跑一次内容检测，这三档才会生效"
          : "内容检测不可用：" + ((NSFW_INFO && NSFW_INFO.reason) || "未知原因"));
    /* 标签自身的提示也换 —— 用户最先悬停到的就是那块「敏感内容」 */
    const lbl = segBar.querySelector(".nsfw-lbl");
    if(lbl) lbl.setAttribute("data-tip", ready
      ? "敏感内容显示方式（NSFW）"
      : undet ? "敏感内容：尚未检测（点此去开启）"
              : "敏感内容：检测不可用 —— " + ((NSFW_INFO && NSFW_INFO.reason) || "未知原因"));

    /* 设置页那行琥珀警示：未检测时说明「为什么不生效 + 怎么办」，就绪后收起来。
       hidden 属性而不是清空文本 —— 清空会留一个空的 .set-gh（它有 margin）。 */
    const warn = $("#nsfwModeWarn");
    if(warn){
      warn.hidden = !undet;
      if(undet) warn.textContent = "尚未检测 —— 这三档现在切换不会真正生效："
        + "库里的图还没被判定过（都算「未知」）。先点上面的「开始检测」跑一次全库扫描，"
        + "扫完这三档自动生效。";
    }
    /* 「开始检测」脉冲高亮：只从「未跑过」首次翻到「跑过」时闪一下，
       像「保存成功」那样给个收束信号。⚠️ 与 nsfwNudge 的 .flash 是同一个 class，
       用完都摘掉，不会互相顶掉（nudge 是 1.4s 定时摘，这里是首次翻转触发）。 */
    if(ready && !scanPulsed){
      scanPulsed = true;
      const b = $("#nsfwScan");
      if(b){
        b.classList.add("flash");
        setTimeout(()=>b.classList.remove("flash"), 1400);
      }
    }
  }
  window.nsfwSyncUI = nsfwSyncUI;

  /* ---- 未就绪时的统一出路（工具条点击、快捷键 N 都走这里）----
     两种处境给两条不同的路：
       undetected → 「去开启」直接开设置页的内容安全页签，并把「开始检测」按钮高亮一下
       noengine   → 没有可点的动作，只能把原因说清（缺库还是缺模型，设置页里分得清）

     ⚠️ 用 toastAction（app.js:365 的现成原语）而不是 toast：这个提示是要**点**的，
        文案里写「请去设置页」而没有任何可点入口，等于把用户扔在原地。
     ⚠️ 同文案的 toast 会被 _toastPush 合并成「×N」并续命 —— 连点三档不会刷屏，
        这正好是想要的（按钮本身是 <button>，不是 a，不会有跳转副作用）。 */
  window.nsfwNudge = function nsfwNudge(){
    if(nsfwNoEngine()){
      toast("内容检测不可用：" + ((NSFW_INFO && NSFW_INFO.reason) || "未安装检测引擎"), "err");
      return;
    }
    toastAction("尚未检测：敏感内容三档要跑过一次检测才生效", "去开启", ()=>{
      if(typeof mvOpenSettings === "function") mvOpenSettings();
      /* showTab 在 settings 的闭包里，这里够不着 —— 直接点页签（等价且不新增出口）。
         点击后 showTab 会自己 nsfwLoadStatus() 取一次真值。 */
      const t = document.querySelector('.set-tab[data-tab="nsfw"]');
      if(t) t.click();
      /* 把话说到底：停在设置页不代表检测跑了，得点「开始检测」。
         高亮那枚按钮 + 落到提示行，用户不用在整页里找。 */
      const b = $("#nsfwScan");
      if(b){
        b.classList.add("flash");          // 样式在 nsfw.css，1.4s 后自己摘掉
        setTimeout(()=>b.classList.remove("flash"), 1400);
        try{ b.focus({preventScroll:false}); }catch(_){}
      }
      toast("点「开始检测」跑一次全库扫描，完成后这三档自动生效", "info", 6000);
    }, "warn");
  };

  for(const s of [segBar, segSet]){
    if(!s) continue;
    s.addEventListener("click", e=>{
      const b = e.target.closest ? e.target.closest(".segbtn") : null;
      if(!b || !b.dataset.n) return;
      /* 未就绪（没装引擎 / 装了没跑过）都在 setNsfwMode 里统一拦 ——
         拦的时候它会调 nsfwNudge() 给出路。
         ⚠️ 这里**不再**自己判断 nsfwOn() 后 return：早期版本在这里 early-return，
            漏掉了「引擎可用但没跑过」那种状态，于是点下去弹「已隐藏」而什么都藏不住。 */
      setNsfwMode(b.dataset.n);
    });
  }

  /* ---- 状态行 / 档位 / 阈值 ---- */
  function syncStatusUI(){
    const I = NSFW_INFO || {};
    const on = nsfwOn();
    const st = $("#nsfwStateRow");
    if(st) st.textContent = !I.available ? "不可用"
                          : (I.enabled === false ? "已关闭" : "可用");
    const cnt = $("#nsfwCountRow");
    if(cnt){
      const c = I.counts || {};
      cnt.textContent = I.available
        ? ((c.nsfw||0) + " 张 NSFW · " + (c.sfw||0) + " 张安全"
           + ((c.unknown|0) ? " · " + c.unknown + " 未识别" : ""))
        : "—";
    }
    const mn = $("#nsfwManualRow");
    if(mn) mn.textContent = String(I.manual || 0);
    /* B9（2026-10-07 文案梳理）：本地推理这句与本区副标题「全本地推理，图片不出这台机器」
       同义，删说明段；但**不可用原因**（I.reason）tooltip 覆盖不到，必须留着。
       用 hidden 而非清空 —— 空的 .set-gh 仍占 margin。 */
    const hint = $("#nsfwHint");
    if(hint){
      hint.hidden = !!I.available;
      hint.textContent = I.available ? "" : (I.reason || "");
    }

    const sel = $("#nsfwModelSel");
    if(sel && (I.tiers || []).length){
      sel.innerHTML = I.tiers.map(t =>
        '<option value="' + esc(t.id) + '"'
        + (t.id === I.model ? " selected" : "")
        + (t.ready ? "" : " disabled") + '>'
        + esc(t.label + " · " + t.desc) + (t.ready ? "" : "（未安装）") + "</option>").join("");
      sel.disabled = !on;
    }
    const thr = $("#nsfwThr"), tv = $("#nsfwThrVal");
    const t = (I.threshold != null ? I.threshold : 0.60);
    if(thr){ thr.value = String(Math.round(t * 100)); thr.disabled = !on; }
    if(tv) tv.textContent = t.toFixed(2);
    const cm = $("#nsfwClearManual");
    if(cm) cm.disabled = !on || !(I.manual || 0);
    nsfwSyncUI();
  }

  function syncProgUI(){
    const I = NSFW_INFO || {};
    const fill = $("#nsfwFill"), txt = $("#nsfwProgTxt"), num = $("#nsfwProgNum");
    const tot = I.total || 0, done = I.done || 0;
    const pc = tot ? Math.round(done / tot * 100) : 0;
    if(fill) fill.style.width = (I.building ? Math.max(4, pc) : (tot ? 100 : 0)) + "%";
    if(txt) txt.textContent = I.building ? "正在检测…" : (tot ? "检测完成" : "尚未检测");
    if(num) num.textContent = done + " / " + tot;
    for(const b of [$("#nsfwScan"), $("#nsfwRescan")])
      if(b) b.disabled = !!(I.building) || !(I.available);
  }

  /* ---- 取状态。quiet=true 时不弹提示（轮询用）----

     失败处理是这里的关键，V2.11.0 在这上面栽过跟头：
     扫描刚开始时的第一个状态请求偶尔会拿到异常响应（HTML 而不是 JSON），
     api() 解析失败抛异常 —— 老代码在 catch 里把 NSFW_INFO 清成 null 且不续轮询，
     于是「正在检测」永远不出现，设置页一直停在 0 / 0（用户实报的 bug）。

     现在：已知有扫描在进行（nsfwScanning）时，失败只保留旧状态继续重试；
     连续失败很多次且确实没有扫描，才按「不可用」处理。 */
  let nsfwFails = 0;
  let nsfwScanning = false;
  let nsfwWasBuilding = false;
  async function nsfwLoadStatus(quiet){
    try{
      const j = await api("/api/nsfw/status");
      nsfwFails = 0;
      const justFinished = nsfwWasBuilding && !j.building;   // 扫描刚结束的那一拍
      nsfwScanning = !!j.building;
      nsfwWasBuilding = nsfwScanning;
      NSFW_INFO = j;
      syncStatusUI(); syncProgUI();
      if(j.building && !pollTimer)
        pollTimer = setInterval(()=>{ nsfwLoadStatus(true); }, 800);
      if(!j.building && pollTimer){ clearInterval(pollTimer); pollTimer = null; }
      if(justFinished) nsfwReloadAfterScan();
      return j;
    }catch(e){
      nsfwFails++;
      if(nsfwScanning && nsfwFails < 12){
        if(!pollTimer) pollTimer = setInterval(()=>{ nsfwLoadStatus(true); }, 800);
        return null;                      // 扫描还在跑：这次没拿到就下一拍再拿
      }
      NSFW_INFO = null;
      syncStatusUI(); syncProgUI();
      if(!quiet) toast("内容安全状态读取失败：" + (e.message || e), "err");
      return null;
    }
  }
  window.nsfwLoadStatus = nsfwLoadStatus;

  /* 扫描结束 → 必须重拉一次列表。
     这是「换根目录后重扫也不生效」的根因：nsfwRefreshList 在点击后 400ms 就跑了
     （那时扫描刚开始），而扫描结束时**文件本身没变** → 目录签名不变 →
     自动刷新永远不触发 → 网格里的 n/ns 还是扫描前拉回来的旧值（全 unknown）。
     服务端算得再对，界面不知道，用户看到的就是「功能失效」。 */
  function nsfwReloadAfterScan(){
    setTimeout(()=>{
      try{
        if(typeof silentReload === "function") silentReload("检测完成，列表已更新");
        else load(S.path || "");
      }catch(e){ try{ applyFilter(); }catch(_){} }
    }, 250);
  }

  const scan = $("#nsfwScan"), rescan = $("#nsfwRescan");
  if(scan) scan.onclick = ()=>nsfwRunScan(false);
  if(rescan) rescan.onclick = ()=>nsfwRunScan(true);
  async function nsfwRunScan(force){
    try{
      const r = await api("/api/nsfw/scan" + (force ? "?force=1" : ""),
        {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"});
      if(r && r.ok !== false){
        nsfwScanning = true;              // 先立标记：首个状态请求失败也要继续轮询
        if(!pollTimer) pollTimer = setInterval(()=>{ nsfwLoadStatus(true); }, 800);
        toast(force ? "已丢弃旧结果，重新检测中…" : "开始检测…");
        nsfwLoadStatus(true);
        /* 不在这里刷新列表：+400ms 时扫描还没跑完，拉回来的是旧数据。
           列表刷新由 nsfwLoadStatus 检测到「扫描刚结束」的跳变来做。 */
      }else{
        toast((r && r.error) || "检测失败", "err");
      }
    }catch(e){ toast("检测失败：" + (e.message || e), "err"); }
  }
  /* 检测完成后列表里每张的 n/ns 都变了，得让网格重取一次。
     用现有的 load() 而不是 applyFilter()：后者只重排内存里那批旧数据，拿不到新字段。 */
  function nsfwRefreshList(){
    setTimeout(()=>{
      try{ load(S.path || ""); }catch(e){ try{ applyFilter(); }catch(_){} }
    }, 400);
  }

  /* ---- 阈值：改完立刻生效，不用重新检测 ----
     分数已经躺在每条数据里（ns 字段），前端按新阈值重算一遍即可。 */
  const thr = $("#nsfwThr");
  if(thr){
    thr.addEventListener("input", ()=>{
      const v = +thr.value / 100;
      const tv = $("#nsfwThrVal"); if(tv) tv.textContent = v.toFixed(2);
    });
    thr.addEventListener("change", async ()=>{
      const v = +thr.value / 100;
      try{
        const r = await api("/api/prefs", {method:"POST",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify({nsfw_threshold:v})});
        if(r && r.ok){
          NSFW_INFO = Object.assign({}, NSFW_INFO || {}, {threshold:r.nsfw_threshold});
          const tv = $("#nsfwThrVal"); if(tv) tv.textContent = r.nsfw_threshold.toFixed(2);
          toast("阈值 " + r.nsfw_threshold.toFixed(2) + " 已生效（无需重新检测）");
          applyFilter();                    // 同一批分数，换个结论
        }else toast((r && r.error) || "阈值写入失败", "err");
      }catch(e){ toast("阈值写入失败：" + (e.message || e), "err"); }
    });
  }

  /* ---- 档位切换：换了模型，旧分数全部作废，必须重扫 ---- */
  const sel = $("#nsfwModelSel");
  if(sel) sel.addEventListener("change", async ()=>{
    const id = sel.value;
    try{
      const r = await api("/api/prefs", {method:"POST",
        headers:{"Content-Type":"application/json"}, body:JSON.stringify({nsfw_model:id})});
      if(r && r.ok){
        NSFW_INFO = Object.assign({}, NSFW_INFO || {}, {model:id});
        toast("已切换到 " + id + " 档，需要重新检测");
        nsfwRunScan(true);
      }else toast((r && r.error) || "切换失败", "err");
    }catch(e){ toast("切换失败：" + (e.message || e), "err"); }
  });

  /* ---- 清空人工纠正 ---- */
  const cm = $("#nsfwClearManual");
  if(cm) cm.onclick = async ()=>{
    if(!confirm("清空全部人工纠正？\n\n这些图会回到模型判定，下次检测后可能被重新标成 NSFW。")) return;
    try{
      const r = await api("/api/nsfw/label", {method:"POST",
        headers:{"Content-Type":"application/json"}, body:JSON.stringify({clear:true})});
      if(r && r.ok){
        toast("已清空人工纠正");
        nsfwLoadStatus(true); nsfwRefreshList();
      }else toast((r && r.error) || "清空失败", "err");
    }catch(e){ toast("清空失败：" + (e.message || e), "err"); }
  };

  /* ---- 找回旧目录数据（换根迁移手动兜底）----
     自动迁移靠指纹命中率判断「是不是搬家」，目录结构变过的场景它不敢动；
     这里用户显式指定旧根，后端跳过门槛但保留逐条验证。 */
  const mb = $("#migRun");
  if(mb) mb.onclick = async ()=>{
    const inp = $("#migOldRoot");
    const old = (inp && inp.value || "").trim();
    if(!old){ toast("请先填旧的根目录完整路径", "err"); return; }
    if(!confirm("把旧目录「" + old + "」的检测结果、人工纠正、收藏、标签、标记迁移到当前根目录？\n\n迁移只改路径不动内容，原数据不会被删除。")) return;
    mb.disabled = true;
    try{
      const r = await api("/api/root/migrate", {method:"POST",
        headers:{"Content-Type":"application/json"}, body:JSON.stringify({old_root:old})});
      if(r && r.ok && r.migrated){
        mvToastMigrate(r);
        if(inp) inp.value = "";
        nsfwLoadStatus(true); nsfwRefreshList();
      }else if(r && r.ok){
        toast((r.reason || "没有可迁移的条目"), "err");
      }else toast((r && r.error) || "恢复失败", "err");
    }catch(e){ toast("恢复失败：" + (e.message || e), "err"); }
    mb.disabled = false;
  };

  /* ---- 快捷键 N：循环三态（与 T 切时间轴同款）----
     捕获阶段注册，避免和输入框抢键。 */
  document.addEventListener("keydown", e=>{
    if(e.ctrlKey || e.metaKey || e.altKey) return;
    const el = e.target, tg = (el.tagName || "").toUpperCase();
    if(tg === "INPUT" || tg === "TEXTAREA" || tg === "SELECT" || el.isContentEditable) return;
    if(e.key !== "n" && e.key !== "N") return;
    if(document.body.classList.contains("modal-on")) return;
    /* 未就绪时**也要拦下这次按键**（preventDefault）再给提示：
       不拦的话 N 会一路落到别处（比如被其他模块吃掉当别的快捷键），
       而用户看到的只是「按了 N 什么都没发生」—— 和这次要修的毛病一模一样。
       setNsfwMode 内部已拦并调 nsfwNudge()，所以这里只需防默认 + 交出去。 */
    if(!nsfwReady()){
      e.preventDefault();
      setNsfwMode(NSFW_MODE);        // 状态不变，只借它走到 nsfwNudge()
      return;
    }
    e.preventDefault();
    setNsfwMode(NSFW_MODES[(NSFW_MODES.indexOf(NSFW_MODE) + 1) % NSFW_MODES.length]);
  }, true);
})();
