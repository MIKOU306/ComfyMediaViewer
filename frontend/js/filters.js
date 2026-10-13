/* ComfyMediaViewer · js/filters.js（2026-09-09 从 index.html 拆出，加载顺序与原文一致） */
"use strict";
/* ---------- V37（D1）：面包屑统一构造器 ----------
   此前四处各写各的（extras / settings 两处甚至逐字复制，改一处必忘另一处）：
     filters.js 的 crumbs() / extras.js 的收藏夹 / settings.js 的跨目录视图 / nav.js 的目录选择器。
   统一到这里，DOM 结构保持不变（可点的 a + span.sep；末尾「当前项」是 span）。
   segs: [{label, path?, onclick?, cls?}]；opts.back = {label, onclick} → 「‹ 返回目录」式入口。
   注意 mkCrumbs 是全局函数（普通 script，声明提升），nav/extras/settings 都能直接用。 */
function crumbSep(){
  return Object.assign(document.createElement("span"),{className:"sep",textContent:"›"});
}
/* ---------- 路径切分（UNC 感知）· E5-1（2026-09-23）----------
   ⚠️ 为什么需要它：`\\NAS\photos\2024` 用 `split(/[\\/]/)` 切出来是 ["","","NAS","photos","2024"]
   ⇒ `filter(Boolean)` 之后**前导 `\\` 被丢掉**。只影响显示还算小事，但凡是「拿切出来的段
   **重新拼回路径**」的地方（面包屑、左栏树）就会拼出 `NAS\photos` 这种**不存在**的相对路径
   —— 实测：目录选择器里点任何一层面包屑都会跳错（`nav.js` 的 `acc` 从 `segs[0]` 起步）。
   所以 UNC 的 `\\server\share` 必须当成**一段原子前缀**，不许再拆。
   返回 `{unc, segs}`；UNC 时 `segs[0]` 就是整段 `\\server\share`。
   ⚠️ 只在「值是**根目录**」时用它来出显示名（rootDisplayName）；**文件**的 basename 照旧用
   `split(/[\\/]/).pop()`，那是正确的。 */
function splitPathSegs(p){
  const s=String(p==null?"":p);
  const all=s.replace(/[\\/]+$/,"").split(/[\\/]/).filter(Boolean);
  if(!/^\\\\/.test(s)) return {unc:false, segs:all};      // 本地路径：原样
  /* 残路径 `\\NAS`（缺 share）也保住前缀，不退回相对名 */
  const head = all.length>1 ? "\\\\"+all[0]+"\\"+all[1] : (all.length ? "\\\\"+all[0] : "\\\\");
  return {unc:true, segs:[head].concat(all.slice(2))};
}
/* 根目录的显示名：本地盘取 basename（`C:\图库` → `图库`）；
   UNC 显示**整段 `\\server\share`** —— 只显示 `photos` 的话，两台 NAS 上的同名共享没法分辨。 */
function rootDisplayName(p){
  const r=splitPathSegs(p), last=r.segs[r.segs.length-1];
  return last||String(p||"");
}
function mkCrumbs(el, segs, opts){
  opts=opts||{}; el.innerHTML="";
  if(opts.back){
    const b=document.createElement("a");
    /* 批次 B：原来的返回值靠 label 里写「‹ 」这个字符当图标（进不去 ICO 体系）。
       这里改成真实图标槽 + 文本节点组装（不用 esc —— label 走 textContent 更安全）。 */
    const _bic=document.createElement("span"); _bic.className="ico-in";
    _bic.innerHTML=ico("arrowUpLeft");
    b.appendChild(_bic); b.appendChild(document.createTextNode(opts.back.label));
    b.onclick=opts.back.onclick;
    el.appendChild(b); el.appendChild(crumbSep());
  }
  (segs||[]).forEach((s,i)=>{
    if(i) el.appendChild(crumbSep());
    if(s.onclick||s.path){
      const a=document.createElement("a");
      if(s.cls) a.className=s.cls;
      a.textContent=s.label;
      a.onclick=s.onclick||(()=>load(s.path));
      el.appendChild(a);
    }else{
      const t=document.createElement("span");
      t.style.color="var(--tx)"; t.textContent=s.label; el.appendChild(t);
    }
  });
}
function crumbs(){
  /* E5-1：根名走 rootDisplayName —— UNC 根（`\\NAS\photos`）此前只显示 `photos`，看不出在哪台机器上。
     段路径的拼接仍从 S.root 起步（本来就是对的），不受影响。 */
  const rootName=rootDisplayName(S.root)||S.root;
  const rel=S.path.slice(S.root.length).replace(/^[\\/]/,"");
  const segs=[{label:rootName,path:S.root}];
  if(rel){ let acc=S.root;
    for(const seg of rel.split(/[\\/]/)){ acc=acc.replace(/[\\/]+$/,"")+"\\"+seg;
      segs.push({label:seg,path:acc}); } }
  mkCrumbs($("#crumbs"), segs);
}

/* ---------- 内容安全过滤（V2.11.0）----------
   只在「隐藏」模式下动刀：把被判为 NSFW 的图从列表里剔掉。
   文件夹卡（type:"dir"）不参与剔除 —— 它只是导航入口，没有 n 字段。
   必须在**排序与分页之前**调用（applyFilter 里就放在那个位置）：
   放在切片之后会出现「末页空白、页码对不上」，和 tlBuildRows 必须在切片之前分组
   是同一类错误，别再踩一次。 */
let NSFW_HIDDEN_N = 0;            // 本轮剔除的张数，筛选状态条要用
function nsfwFilter(v){
  NSFW_HIDDEN_N = 0;
  /* ⚠️ 判据是 nsfwReady() 而不是 nsfwOn()（V2.12.6 修的假成功）：
     「引擎装了但一次没跑过」时每条还是 n="unknown"，isNsfw() 恒假 ——
     照旧用 nsfwOn() 会走进这个循环却一张都剔不掉，界面还弹「已隐藏」。
     现在这种状态根本不放行，交给 setNsfwMode / 工具条去提示「先跑一次检测」。 */
  if(!(nsfwReady() && NSFW_MODE === "hide")) return v;
  const out = [];
  for(const it of v){
    if(isNsfw(it)){ NSFW_HIDDEN_N++; continue; }
    out.push(it);
  }
  return out;
}
/* 切显示方式：写偏好 → 同步两处控件 → 重算。
   切到「隐藏」时如果当前大图那张正好被筛掉，主动关掉大图 ——
   否则用户会在大图里看到一张列表上已经没有的图，而按删除键删掉的是 S.view[S.idx]，
   「屏幕上看着 A、删掉的却是 B」这种事这个版本已经修过一次了（keepIdxByPath）。 */
function setNsfwMode(m, silent){
  if(NSFW_MODES.indexOf(m) < 0) m = "blur";
  /* 闸门前置：没装引擎 / 装了但没跑过检测 —— 一律**不改状态**、给出路。
     ⚠️ 必须在写 NSFW_MODE 与 localStorage 之前拦，否则屏幕没变、偏好却被改了，
        下次进来又是一个「控件停在 hide 但什么都没藏」的错位态。 */
  if(!nsfwReady()){
    if(typeof nsfwNudge === "function") nsfwNudge();
    return false;
  }
  NSFW_MODE = m;
  try{ localStorage.setItem("mv.nsfw", m); }catch(_){}
  if(typeof nsfwSyncUI === "function") nsfwSyncUI();
  const cur = (S.view && S.idx >= 0 && S.view[S.idx]) ? S.view[S.idx].path : "";
  applyFilter();
  if(cur && !keepIdxByPath(cur) && lb && lb.classList.contains("on")) closeLb();
  if(!silent) toast(m === "hide" ? "已隐藏敏感内容"
                  : m === "blur" ? "敏感内容显示为模糊（点单张可揭开）"
                                 : "敏感内容原样显示");
  return true;
}

/* 把某条路径的揭开状态刷到网格里所有对应卡片（含左上角眼睛按钮的图标态），
   以及大图底部的胶片条缩略图（V2.11.1）—— 大图里揭开/盖回时，胶片条里那张
   不该还糊着/露着。按 dataset.p 比对而**不是**拼 [p="..."] 属性选择器：
   路径里有反斜杠，塞进去会炸。 */
function nsfwPaintCard(path){
  const on = NSFW_REVEAL.has(path);
  for(const el of document.querySelectorAll("#grid .card.nsfw, #lbFilm .f.nsfw")){
    if(el.dataset.p !== path) continue;
    el.classList.toggle("revealed", on);
    const eye = el.querySelector(".nv-eye");       // 胶片条没有眼睛，天然跳过
    if(eye){
      eye.classList.toggle("on", on);
      eye.innerHTML = ico(on ? "eyeOff" : "eye");
      eye.setAttribute("data-tip", on ? "重新遮挡这张（只影响当前会话）" : "单独查看这张原图");
    }
  }
}

/* ready=false 表示元数据还没到位：此时**放行**而不是排除，否则会瞬间把网格清空；
   applyFilter 末尾会在元数据到位后自动重算，用户看到的是"稍等一下再筛好"，而不是"空了"。 */
function metaFilter(v, ready){
  const fm=$("#fModel").value, fs=$("#fSampler").value, fd=$("#fDim").value;
  if(!(fm||fs||fd) || !ready) return v;
  return v.filter(it=>{
    const m=S.metaMap[it.path];
    if(!m) return false;                      // 无元数据（视频/损坏/非本站生成）直接排除
    if(fm && (m.model||"")!==fm) return false;
    if(fs && (m.sampler||"")!==fs) return false;
    if(fd && (!m.w || !m.h || m.w+"x"+m.h!==fd)) return false;
    return true;
  });
}

/* 时间范围：今天=当日 0 点起 / 本周=本周一 0 点起 / 本月=1 号 0 点起 */
function dateCutoff(mode){
  if(!mode||mode==="all") return 0;
  const d=new Date(); d.setHours(0,0,0,0);
  if(mode==="today") return d.getTime()/1000;
  if(mode==="week"){ const w=new Date(d); w.setDate(w.getDate()-((w.getDay()+6)%7)); return w.getTime()/1000; }
  if(mode==="month") return new Date(d.getFullYear(),d.getMonth(),1).getTime()/1000;
  return 0;
}

/* 清除全部筛选（筛选状态条「全部清除」与空结果提示共用）
   ⚠️ P1-8：这里必须**真的清干净** —— 以前漏了格式（FMTS）与工作流类型（WFK），
   于是筛空之后点「清除全部筛选」，网格仍然是空的（applyFilter 算出来还是 0 条），
   用户看到的是「按钮点了没反应」。
   ⚠️ 另一处：原来在标签筛选生效时 `return` 掉了 applyFilter —— 前面几行虽然把控件
   写回了默认值，但界面停在旧结果上（setTagFilter([]) 在 tagFilterActive 为假时直接
   return，不会重算）。现在一律走到最后的 applyFilter。 */
function clearAllFilters(){
  /* 「已隐藏 NSFW」这枚 chip 也摆在筛选状态条里，用户就把它当筛选条件 ——
     全部清除必须把它一起放掉。V2.11.0 漏了这条：NSFW_MODE 还停在 hide，
     nsfwFilter 下一轮照样剔图、chip 又被画回来，按钮看起来完全失效。
     放掉的方向是回「模糊」而不是「显示」—— 和 chip 上那个 × 一致，
     也和「恢复默认状态」的语义一致（模糊就是默认档）。 */
  if(NSFW_MODE === "hide"){
    NSFW_MODE = "blur";
    try{ localStorage.setItem("mv.nsfw", NSFW_MODE); }catch(_){}
    if(typeof nsfwSyncUI === "function") nsfwSyncUI();
  }
  const backToMarks = libMode==="marked";          // 已标记视图：清完条件后重拉列表
  /* H2（2026-10-07）：四个幽灵控件的状态已外迁到 S.filters —— 一律写状态、不再写 DOM。
     `favcat` **有意不清**（与 H2 前一致）：收藏视图里「全部清除」不该顺手丢掉用户选的分类，
     分类的清除有它自己的那枚 chip。 */
  S.filters.type="all";
  if(libMode==="workflow"){                        // 退出「工作流」主视图
    libMode=""; S.filters.wfClass="any";
    if($("#wfClass")) $("#wfClass").style.display="none";
  }
  if(backToMarks) S.filters.markState="*";
  syncFiltersUI();
  if($("#dateRange")) $("#dateRange").value="all";
  $("#q").value=""; $("#fModel").value=""; $("#fSampler").value=""; $("#fDim").value="";
  if(typeof clearFmtWfk==="function") clearFmtWfk();   // P1-8：格式 + 工作流类型一并清
  /* P0-7（审计二批次·第 4 级）：**复位三件套缺一不可**。
     原实现只清 TAG_FILTER/TAG_EXCLUDE 两个数组，漏了 tagFilterActive 与 S.tagView ——
     而 applyFilter 的数据源选择是 `(tagFilterActive && S.tagView) ? S.tagView : S.items`
     ⇒ 标签 chip 确实消失了（renderTagNav 重画）、左栏「全部图片」也亮起，
     但网格里仍是那批标签命中的图，用户看到的是标准的「按钮点了没反应」。
     这里与另三处「切视图」保持同源：app.js:552 / extras.js:19 / settings.js:1065
     都是 `tagFilterActive=false; S.tagView=null;`。 */
  if(TAG_FILTER.length || TAG_EXCLUDE.length || tagFilterActive){
    TAG_FILTER=[]; TAG_EXCLUDE=[];
    tagFilterActive=false; S.tagView=null;
    if(typeof renderTagNav==="function") renderTagNav();
  }
  if(backToMarks){ loadLibView("marked"); return; }
  applyFilter();
}

/* ---------------- H2（2026-10-07）幽灵控件清零：唯一写入入口 ----------------
   四个 `<select>` 原先把筛选状态存在 DOM 里（`.value`）。现在状态唯一来源是
   `S.filters`（见 state.js），下面四个 setter 是**唯一**写入入口，任何地方都不许
   直接写 `S.filters.*`（除 clearAllFilters 这种「一键归零」的显式复位）。

   两个**仍会出现在特定视图里**的控件（`#wfClass` 在「工作流」视图、`#markState` 在
   「已标记」视图）退化为**只读视图** —— 由 syncFiltersUI() 单向推状态 → DOM，
   它们的 onchange 只调这里的 setter。两个**从不显示**的纯幽灵（`#filter` / `#favcat`）
   已从 DOM 删除。

   ⚠️ 四个 setter 的**副作用各不相同**，是照原 `onchange` 语义逐个保留的，别图省事统一：
     · setTypeFilter    —— 还要退出「工作流」主视图（原 `#filter.onchange` 语义）；
     · setFavCatFilter  —— 收藏视图里要**重新拉服务端**（`loadFavView`，按新分类过滤）；
     · setWfClassFilter —— 只在「工作流」视图里才重算；
     · setMarkStateFilter —— 只在「已标记」视图里才重新拉列表。 */
function syncFiltersUI(){
  const ws=[["#wfClass","wfClass"],["#markState","markState"]];
  for(const [sel,key] of ws){
    const el=$(sel); if(!el) continue;
    const v=S.filters[key];
    if(el.value!==v) el.value=v;          // 只在必要时写，避免打断用户展开中的原生下拉
  }
}
function setTypeFilter(v){
  v=v||"all";
  S.filters.type=v;
  /* 手动改类型 = 退出「工作流」主视图（原 `#filter.onchange` 的语义）。
     `#wfClass` 的显隐仍由 settings.js 按 libMode 管，这里只负责状态。 */
  if(libMode==="workflow"){ libMode=""; if($("#wfClass")) $("#wfClass").style.display="none"; }
  applyFilter();
}
function setFavCatFilter(cat){
  S.filters.favcat=cat||"*";
  syncFiltersUI();
  /* 在收藏夹视图里改分类：必须重新拉服务端（`/api/fav_view?cat=`）——
     applyFilter 的 favMode 分支用的是**服务端已按分类过滤好**的 S.items，
     只改状态不重拉的话界面会停在旧分类（这就是 v2.x 修过的「切分类无效」）。 */
  favMode ? loadFavView() : applyFilter();
}
function setWfClassFilter(v){
  S.filters.wfClass=v||"any";
  syncFiltersUI();
  if(libMode==="workflow") applyFilter();
}
function setMarkStateFilter(v){
  S.filters.markState=v||"*";
  syncFiltersUI();
  if(libMode==="marked") loadLibView("marked");
}

/* 筛选状态条：把当前生效的筛选条件列出来，每个可单独清除 */
function renderChips(){
  const box=$("#filterChips"); if(!box) return;
  const f=S.filters.type, cat=S.filters.favcat;
  const q=QP ? QP.text : $("#q").value.trim();
  const dr=$("#dateRange")?$("#dateRange").value:"all";
  const fm=$("#fModel").value, fs=$("#fSampler").value, fd=$("#fDim").value;
  const chips=[];
  if(f!=="all") chips.push(["类型："+({image:"图片",video:"视频",fav:"★ 收藏"}[f]||f),
    ()=>setTypeFilter("all")]);
  if(f==="fav" && cat && cat!=="*") chips.push(["分类："+cat,
    ()=>setFavCatFilter("*")]);
  if(dr!=="all") chips.push(["时间："+({today:"今天",week:"本周",month:"本月"}[dr]||dr),
    ()=>{ $("#dateRange").value="all"; applyFilter(); }]);
  if(q) chips.push(["搜索："+q, ()=>{               // 只清普通文本，保留语法条件
    $("#q").value=((QP&&QP.parts)||[]).map(p=>p.key+":"+(/[\s"]/.test(p.value)?'"'+p.value+'"':p.value)).join(" ");
    viewGen++; applyFilter(); }]);
  for(const p of ((QP&&QP.parts)||[]))             // 语法条件：每条单独可删
    chips.push([_condLabel(p), ()=>removeCond(p.key, p.value)]);
  if(fm) chips.push(["模型："+fm, ()=>{ $("#fModel").value=""; applyFilter(); }]);
  if(fs) chips.push(["采样器："+fs, ()=>{ $("#fSampler").value=""; applyFilter(); }]);
  if(fd) chips.push(["尺寸："+fd, ()=>{ $("#fDim").value=""; applyFilter(); }]);
  /* P1-8：格式与工作流类型以前不在状态条里 —— 它们作用着却看不见，用户找不到
     「网格为什么是空的」的线索。这里补上，并各自支持单独清除。 */
  if(typeof FMTS!=="undefined" && FMTS.size)
    for(const k of Array.from(FMTS))
      chips.push(["格式："+(typeof fmtLabel==="function"?fmtLabel(k):k.replace(".","").toUpperCase()),
        ()=>{ FMTS.delete(k); if(typeof buildFmtNav==="function") buildFmtNav(); applyFilter(); }]);
  if(typeof WFK!=="undefined" && WFK)
    chips.push(["工作流类型："+({t2i:"文生图",i2i:"图生图",cn:"ControlNet",
                              other:"其他 / 无生成参数"}[WFK]||WFK),
      ()=>{ WFK=""; if(typeof buildWfNav==="function") buildWfNav(); applyFilter(); }]);
  if(libMode==="workflow"){
    const wc=S.filters.wfClass||"any";
    chips.push(["元数据："+({any:"有元数据",full:"有完整工作流",prompt:"只有提示词",none:"无生成参数"}[wc]||wc),
      ()=>setWfClassFilter("any")]);
  }
  if(libMode==="marked" && S.filters.markState!=="*")
    chips.push(["标记："+(MARK_LABELS[S.filters.markState]||S.filters.markState),
      ()=>setMarkStateFilter("*")]);
  for(const t of TAG_FILTER) chips.push([(TAG_MODE==="not"?"排除标签：":"标签：")+t,
    ()=>setTagFilter(TAG_FILTER.filter(x=>x!==t))]);
  for(const t of TAG_EXCLUDE) chips.push(["排除标签："+t,
    ()=>{ TAG_EXCLUDE=TAG_EXCLUDE.filter(x=>x!==t); setTagFilter([...TAG_FILTER]); }]);
  /* 「隐藏」模式下必须留一枚可点回来的提示：
     图凭空少了十几张，用户只会以为「我图丢了」，不会想到是安全模式干的。
     点它回到「模糊」——比「显示」温和，既恢复了可见性又不会一下全裸出来。
     ⚠️ 判据用 nsfwReady() 而不是 nsfwOn()：与 nsfwFilter 同上，未检测时根本剔不掉东西，
        更不该冒出一枚「已隐藏 0 张」的假 chip。（NSFW_HIDDEN_N>0 其实已经隐式拦住了，
        但判据保持一致，将来别处复用这行时不会踩坑。） */
  if(nsfwReady() && NSFW_MODE==="hide" && NSFW_HIDDEN_N>0)
    chips.push(["已隐藏敏感内容："+NSFW_HIDDEN_N+" 张", ()=>setNsfwMode("blur")]);
  if(!chips.length){ box.hidden=true; box.innerHTML=""; return; }
  box.hidden=false; box.innerHTML="";
  const lbl=document.createElement("span"); lbl.className="fclbl"; lbl.textContent="已筛选：";
  box.appendChild(lbl);
  for(const [txt,fn] of chips){
    const el=document.createElement("span"); el.className="fchip";
    const lb=document.createElement("span"); lb.className="lbl";
    lb.textContent=txt; lb.dataset.tip=txt;                 // 悬停看全名
    el.appendChild(lb);
    const x=document.createElement("span"); x.className="x"; x.innerHTML=ico("x"); x.dataset.tip="清除该条件";
    x.onclick=fn; el.appendChild(x);
    box.appendChild(el);
  }
  const all=document.createElement("button"); all.className="sm"; all.textContent="全部清除";
  all.onclick=clearAllFilters;
  box.appendChild(all);
}

/* ---------------- 搜索语法（阶段四）：tag: model: sampler: size: fav: type: after: before: name: wf:
   规则：已知前缀才当条件；未知前缀（如 foo:bar）与中文冒号一律按普通文本处理。
   普通文本在「文件名」模式匹配文件名，在「提示词」模式仍交给 /api/search。 */
const Q_KEYS = {tag:1, tags:1, model:1, sampler:1, size:1, fav:1, type:1,
                after:1, before:1, name:1, file:1, wf:1};
let QP = null;                                   // 最近一次解析结果（渲染 Chips / 删除条件用）

function _parseDay(s, end){
  const m=/^(\d{4})[-/](\d{1,2})(?:[-/](\d{1,2}))?$/.exec(String(s||"").trim());
  if(!m) return 0;
  const d=new Date(+m[1], +m[2]-1, m[3]?+m[3]:1, end?23:0, end?59:0, end?59:0);
  return Math.floor(d.getTime()/1000);
}
function _condLabel(p){
  const k=p.key;
  if(k==="tag"||k==="tags") return "标签："+p.value;
  if(k==="model") return "模型："+p.value;
  if(k==="sampler") return "采样器："+p.value;
  if(k==="size") return "尺寸："+p.value;
  if(k==="fav") return "收藏："+(p.value||"true");
  if(k==="type") return "类型："+({image:"图片",video:"视频"}[p.value]||p.value);
  if(k==="after") return "晚于："+p.value;
  if(k==="before") return "早于："+p.value;
  if(k==="name"||k==="file") return "文件名："+p.value;
  if(k==="wf") return "工作流："+({full:"有完整工作流",prompt:"只有提示词",none:"无生成参数"}[p.value]||p.value);
  return k+":"+p.value;
}
function parseQuery(raw){
  const q={text:"", tag:[], model:"", sampler:"", size:"", fav:null, type:"",
           after:0, before:0, name:"", wf:"", parts:[]};
  const src=String(raw||"").trim();
  if(!src) return q;
  const re=/([A-Za-z_]+):("[^"]*"|[^\s"]*)|("[^"]*"|\S+)/g;   // 支持 "带 空格" 的引号值
  let m;
  while((m=re.exec(src))){
    const key=(m[1]||"").toLowerCase();
    if(key && Q_KEYS[key]){
      const v=(m[2]||"").replace(/^"|"$/g,"");
      if(key==="tag"||key==="tags"){                 // 标签可逗号分隔、可重复（AND）
        for(const t of v.split(/[,，]/)){
          if(!t.trim()) continue;
          q.tag.push(t.trim()); q.parts.push({key:"tag", value:t.trim()});
        }
      }else if(key==="model"){ q.model=v; q.parts.push({key, value:v}); }
      else if(key==="sampler"){ q.sampler=v; q.parts.push({key, value:v}); }
      else if(key==="size"){ q.size=v.toLowerCase().replace(/\s/g,"").replace("×","x"); q.parts.push({key, value:q.size}); }
      else if(key==="fav"){ q.fav=/^(1|true|yes|y|是|★)$/i.test(v); q.parts.push({key, value:v}); }
      else if(key==="type"){
        q.type=/^(image|img|图片|图)$/i.test(v)?"image":(/^(video|vid|mp4|视频)$/i.test(v)?"video":"");
        q.parts.push({key, value:v});
      }
      else if(key==="after"){ q.after=_parseDay(v,false); q.parts.push({key, value:v}); }
      else if(key==="before"){ q.before=_parseDay(v,true); q.parts.push({key, value:v}); }
      else if(key==="name"||key==="file"){ q.name=v; q.parts.push({key:"name", value:v}); }
      else if(key==="wf"){
        q.wf=/^(full|完整|有完整工作流)$/i.test(v)?"full"
            :/^(prompt|提示词|只有提示词)$/i.test(v)?"prompt"
            :/^(none|无|无生成参数)$/i.test(v)?"none":"";
        q.parts.push({key, value:v});
      }
    }else{
      const t=(m[3]||m[0]).replace(/^"|"$/g,"");     // 未知前缀/普通文本 → 原样当搜索词
      if(t) q.text += (q.text?" ":"")+t;
    }
  }
  return q;
}
/* 把解析出的结构化条件套到结果集上（复杂提示词仍由 /api/search 负责） */
function queryFilter(v, q, ready){
  if(!q) return v;
  const needMeta = !!(q.model||q.sampler||q.size||q.wf) && ready!==false;
  return v.filter(it=>{
    if(q.type && it.type!==q.type) return false;
    if(q.fav!==null && !!it.fav !== q.fav) return false;
    if(q.name && !it.name.toLowerCase().includes(q.name.toLowerCase())) return false;
    if(q.after && !(it.mtime>=q.after)) return false;
    if(q.before && !(it.mtime<=q.before)) return false;
    if(q.tag.length){
      const have=(it.tags||[]).map(t=>t.toLowerCase());
      for(const t of q.tag){                         // 父标签命中子标签（风格 命中 风格/汉服）
        const w=t.toLowerCase();
        if(!have.some(h=>h===w || h.startsWith(w+"/"))) return false;
      }
    }
    if(needMeta){
      const m=S.metaMap && S.metaMap[it.path];
      if(q.model && !String((m&&m.model)||"").toLowerCase().includes(q.model.toLowerCase())) return false;
      if(q.sampler && !String((m&&m.sampler)||"").toLowerCase().includes(q.sampler.toLowerCase())) return false;
      if(q.size && (!m || !m.w || !m.h || (m.w+"x"+m.h)!==q.size)) return false;
      if(q.wf){
        const cls=(m&&m.wf)||"";
        if(q.wf==="none"){ if(!(cls==="none" || !m)) return false; }
        else if(cls!==q.wf) return false;
      }
    }
    return true;
  });
}
/* 删掉某个语法条件（Chips 上的 ✕）→ 重写搜索框再重算 */
function removeCond(key, value){
  const q=QP; if(!q) return;
  const rest=(q.parts||[]).filter(p=>!(p.key===key && p.value===value))
    .map(p=>p.key+":"+(/[\s"]/.test(p.value)?'"'+p.value.replace(/"/g,"")+'"':p.value));
  $("#q").value=[q.text].concat(rest).filter(Boolean).join(" ");
  viewGen++;
  applyFilter();
}

/* ================= 时间轴视图（V2.8.5） =================
   核心判断：时间轴是**渲染层**特性，不是数据层特性 —— 每条媒体记录已带 mtime
   （服务端 _pack_media 返回 int(st.st_mtime)），所以分组**纯前端**算得出来：
   零后端改动、零新端点。唯一的前提是分组必须发生在「分页切片之前」（与 dirgroup 同款），
   否则会重演 V2.8.3 修过的那个 BUG：每页首格都被导航卡占着 —— 这里是每页重插日期头。 */
const _p2 = n => String(n).padStart(2,"0");
/* 本地日期键：一律用 getFullYear/getMonth/getDate 取**本地**时间，禁用 toISOString()——
   用 UTC 分组会让「今天」在东八区错位到前一天。 */
function tlDayKey(t){ const d=new Date((t||0)*1000);
  return d.getFullYear()+"-"+_p2(d.getMonth()+1)+"-"+_p2(d.getDate()); }
function tlDayTag(t){                       // 0=今天 1=昨天 其它=差值
  const d=new Date((t||0)*1000), n=new Date();
  n.setHours(0,0,0,0);
  const t0=new Date(d.getFullYear(),d.getMonth(),d.getDate()).getTime();
  return Math.round((n.getTime()-t0)/86400000);
}
function tlDur(sec){                        // 批次耗时 → 「2 分 12 秒」
  sec=Math.max(0, Math.round(sec));
  const m=Math.floor(sec/60), s=sec%60;
  return m ? `${m} 分 ${s} 秒` : `${s} 秒`;
}
const TL_UNIT = {hour:"小时", day:"天", week:"周", month:"个月", batch:"批"};
const TL_WD = ["周日","周一","周二","周三","周四","周五","周六"];

/* 时 / 日 / 周 / 月：按当前排序顺序走一遍（最新优先 → 最新的天排在最前，符合直觉） */
function tlFlatGroups(v, grain){
  const map=new Map(), out=[];
  for(const it of v){
    const t=it.mtime||0, d=new Date(t*1000);
    let key, title, sub="";
    if(grain==="hour"){
      key=tlDayKey(t)+" "+_p2(d.getHours());
      const tag=tlDayTag(t);
      title=(tag===0?"今天":tag===1?"昨天":((d.getMonth()+1)+"月"+d.getDate()+"日"))
            +" "+_p2(d.getHours())+":00";
      sub=TL_WD[d.getDay()];
    }else if(grain==="week"){                       // ISO 周：周一起算
      const w=new Date(d); w.setDate(w.getDate()-((w.getDay()+6)%7)); w.setHours(0,0,0,0);
      key="W"+w.getFullYear()+"-"+_p2(w.getMonth()+1)+"-"+_p2(w.getDate());
      const e=new Date(w); e.setDate(e.getDate()+6);
      title=(w.getMonth()+1)+"月"+w.getDate()+"日 – "+(e.getMonth()+1)+"月"+e.getDate()+"日";
      sub="周一 – 周日";
    }else if(grain==="month"){
      key=d.getFullYear()+"-"+_p2(d.getMonth()+1);
      const n=new Date();
      title=d.getFullYear()+"年"+(d.getMonth()+1)+"月";
      sub=(d.getFullYear()===n.getFullYear()&&d.getMonth()===n.getMonth())?"本月":"";
    }else{                                          // day（默认）
      key=tlDayKey(t);
      const tag=tlDayTag(t);
      title=tag===0?"今天":tag===1?"昨天":((d.getMonth()+1)+"月"+d.getDate()+"日");
      sub=TL_WD[d.getDay()];
    }
    let g=map.get(key);
    if(!g){ g={key, title, sub, t0:t, t1:t, items:[]}; map.set(key,g); out.push(g); }
    g.items.push(it);
    if(t<g.t0) g.t0=t;
    if(t>g.t1) g.t1=t;
  }
  return out;
}
/* 批次：ComfyUI 的心智里「一次 Queue = 一批」。先按时间升序聚类（升序才能正确判间隔），
   再按当前排序方向决定组的先后。顺带白拿一个信息：t1-t0 = 该批耗时。 */
function tlBatchGroups(v, gapSec){
  const asc=v.slice().sort((a,b)=>(a.mtime||0)-(b.mtime||0));
  const out=[];
  for(const it of asc){
    const t=it.mtime||0, last=out[out.length-1];
    if(!last || t-last.t1>gapSec) out.push({key:"B"+t, t0:t, t1:t, items:[it]});
    else { last.t1=t; last.items.push(it); }
  }
  if((($("#sort")||{}).value||"").startsWith("mtime_desc")) out.reverse();
  for(const g of out){
    const d=new Date(g.t0*1000);
    const sameDay=tlDayKey(g.t0)===tlDayKey(g.t1);
    g.title="批次 "+_p2(d.getHours())+":"+_p2(d.getMinutes());
    g.sub=(sameDay?"":(d.getMonth()+1)+"月"+d.getDate()+"日 · ")+tlDur(g.t1-g.t0);
    g.key=g.key+"#"+g.items.length;                 // 同秒并发时防键重复
  }
  return out;
}
/* 把分组摊平成渲染序列：组头（+ 组内条目 + 「展开全部」尾巴）。
   组头是**合成条目**，不参与大图翻页 / 批量选择 / 计数（TL_SYNTH）。 */
function tlBuildRows(v){
  const groups=(TL_GRAIN==="batch")?tlBatchGroups(v, TL_BATCH_GAP):tlFlatGroups(v, TL_GRAIN);
  const rows=[], meta=[];
  for(let gi=0; gi<groups.length; gi++){
    const g=groups[gi];
    const collapsed=!!TL_COLLAPSED[g.key];
    rows.push({type:"tlhead", gi, gkey:g.key, title:g.title,
               sub:[g.sub, g.items.length+" 张"].filter(Boolean).join(" · "),
               n:g.items.length, collapsed});
    meta.push({gi, gkey:g.key, title:g.title, n:g.items.length,
               d0:tlDayKey(g.t0), d1:tlDayKey(g.t1)});
    if(collapsed) continue;
    const cap=TL_SHOWN[g.key]?g.items.length:TL_CAP;
    for(const it of g.items.slice(0,cap)) rows.push(it);
    if(cap<g.items.length) rows.push({type:"tlmore", gi, gkey:g.key, n:g.items.length-cap});
  }
  return {rows, meta};
}

function applyFilter(){
  if(CMVQuery.interceptFilter()) return;
  const raw=$("#q").value.trim();
  const q=parseQuery(raw); QP=q;
  if($("#qmode").value==="prompt" && q.text){ promptSearch(q.text); return; }
  const ql=q.text.toLowerCase();
  const f=S.filters.type, s=$("#sort").value;
  const cat=S.filters.favcat;
  /* H2（2026-10-07）：两个仍会出现在特定视图里的控件（#wfClass / #markState）是**只读视图**，
     每次重算都推一次状态 → DOM，保证用户看到的下拉选中项与实际生效条件一致。 */
  syncFiltersUI();
  // 标签筛选生效时，后续的类型/时间/排序/元数据筛选都作用在「标签结果」之上，
  // 而不是目录原始列表——否则改排序或选模型就会把被排除的图重新放回来
  const src = (tagFilterActive && S.tagView) ? S.tagView : S.items;
  /* 依赖元数据的筛选（下拉 / 语法 model:·size:·wf: / 工作流类型 / 工作流库）在元数据到位前全部放行，
     不参与本轮过滤。metaReady 由下方"按需加载"逻辑在拿到数据后触发重算补全。 */
  const metaReady = !!S.metaMap;
  let v;
  /* P1-11：**三个特殊视图也必须套 queryFilter**。
     favMode / recent / marked 的数据来自服务端，只做了「类型 + 文件名」的粗过滤，
     而搜索框里的 tag: / after: / model: 等结构化语法被整段跳过 —— 界面上 chip 条
     照样渲染这些条件，用户以为筛了、其实一条没生效。
     metaReady 交给 queryFilter 自己按 ready!==false 判定，不需要元数据的条件照筛。 */
  if(favMode){                                 // 跨目录收藏视图：服务端已按分类过滤
    v=S.items.filter(it=>it.fav && (!ql || it.name.toLowerCase().includes(ql)));
    v=queryFilter(v, q, metaReady);
  }else if(libMode==="recent" || libMode==="marked"){
    // 最近使用 / 已标记：服务端已过滤好，这里只叠加类型与文件名搜索
    v=S.items.filter(it=>(f==="all"||it.type===f) && (!ql || it.name.toLowerCase().includes(ql)));
    v=queryFilter(v, q, metaReady);
  }else{
    v=src.filter(it=>{
      if(f==="fav"){ if(!it.fav) return false;
        if(cat && cat!=="*" && (it.favcat||"默认")!==cat) return false; }
      else if(f!=="all" && it.type!==f) return false;
      return !ql || it.name.toLowerCase().includes(ql);
    });
    v=queryFilter(v, q, metaReady);               // 结构化条件（tag:/model:/size:…）
  }
  v=_applyCommonFilters(v);
  const timeKey=libMode==="recent" ? "rtime" : "mtime";
  const cmp={mtime_desc:(a,b)=>b[timeKey]-a[timeKey], mtime_asc:(a,b)=>a[timeKey]-b[timeKey],
    name_asc:(a,b)=>a.name.localeCompare(b.name,"zh"), name_desc:(a,b)=>b.name.localeCompare(a.name,"zh"),
    size_desc:(a,b)=>b.size-a.size, size_asc:(a,b)=>a.size-b.size};
  v.sort(cmp[s]||cmp.mtime_desc);
  /* ---- 分页：整表排序后只取当前页（参考图口径，替代无限滚动） ---- */
  /* 切换显示方式也要重置页码：从「显示」切到「隐藏」后结果条数变了，
     若页码留在第 5 页而新的结果只有 2 页，applyFilter 里 PAGE>pages 会把它钳到末页，
     但那是补救；把 NSFW_MODE 纳入签名才是正解（条件变了就回第 1 页）。 */
  /* F4 P2-7：把「影响行数但不属于筛选条件」的几项也纳入签名 —— 折叠/展开时间轴组头、
     展开文件夹、切视图、改每页条数都会大幅改变行数，原先不在签名里 ⇒ PAGE 不复位，
     只能靠下面 `PAGE>pages` 夹取 ⇒ 往往被夹到末页，落在意料之外的页码上。 */
  const _collapsed=Object.keys(TL_COLLAPSED).filter(k=>TL_COLLAPSED[k]).sort().join(",");
  const _shownGroups=Object.keys(TL_SHOWN).sort().join(",");
  const _sig=[f,s,($("#dateRange")||{}).value||"",raw,[...FMTS].sort().join(","),WFK,cat,
              S.filters.wfClass,S.filters.markState,
              S.path||"",favMode,libMode,TAG_FILTER.join(","),TAG_EXCLUDE.join(","),
              NSFW_MODE,VIEW_MODE,!!S.expandDirs,_collapsed,_shownGroups,PER].join("|");
  if(_sig!==FILT_SIG){ FILT_SIG=_sig; PAGE=1; }

  /* 子文件夹卡（收藏视图除外）：带语法条件时文件夹不可能命中，直接不展示；
     普通文本搜索仍保留命中的文件夹做导航 */
  const dirNav = f!=="fav" && !favMode && libMode!=="recent" && libMode!=="marked";
  const ds = dirNav ? (q.parts.length?[]:S.dirs).filter(d=>!ql || d.name.toLowerCase().includes(ql)) : [];
  ds.sort((a,b)=>a.name.localeCompare(b.name,"zh"));
  const dcards=ds.map(d=>({type:"dir",name:d.name,path:d.path,n:d.n}));
  // 展开文件夹 = 纯导航模式：序列就是「收起卡 + 子文件夹」，隐藏本目录文件
  const dirOnly = dirNav && !ql && S.expandDirs;

  /* 分页的序列：普通浏览分的是文件，展开文件夹分的是子文件夹
     （旧版页码恒按文件数算，纯文件夹视图里点页码内容纹丝不动） */
  /* 时间轴：分组**必须**发生在切片之前（否则每页都会重插日期头）。
     dirOnly（展开文件夹）在时间轴下不启用 —— 二者互斥，见切换视图时的自动收起。 */
  const tlOn = VIEW_MODE==="time" && !dirOnly;
  const tlB = tlOn ? tlBuildRows(v) : null;
  TL_GROUPS = tlB ? tlB.meta : [];
  /* F1 P2-2：记下每个组头在**全量行序列**里的下标。分页模式下 S.view 已切片，
     光靠它算不出目标组在第几页 —— 有了这张表才能「切页」而不是「切展示方式」。 */
  TL_ROWIDX = {};
  if(tlB) tlB.rows.forEach((r,i)=>{ if(r && r.type==="tlhead") TL_ROWIDX[r.gi]=i; });
  const seqN = dirOnly ? dcards.length : (tlB ? tlB.rows.length : v.length);
  /* H7（2026-10-06）计数去重：顶栏 #count 是「筛选后 / 总数」的**唯一**表达。
     分母 = **当前数据源未加任何筛选条件时**的条目数 —— 收藏 / 最近 / 已标记三个跨目录
     视图的数据源是 S.items 的一个子集，分母取该子集的全量（不是整个 S.items，
     否则收藏视图会写成「12 / 9687」，拿别处的分母来吓人）。
     两者相等时不写分母：「3408 / 3408 项」没有任何信息量。
     语义是「看的是哪一本账」，所以口径必须与分子同源 ——
     搜索（promptSearch 走 /api/search，含子目录）与时间轴（seqN 含日期组头行）
     两条路径的分子口径都与本函数不同，分母无法同源，保持原样不加（见各自分支注释）。
     左栏「位置」根项的「根目录 · N 项」是全库**递归**口径（nav_stats），与本项
     的**当前层**口径不同源，不构成重复，保持不动。 */
  const totalN = favMode ? S.items.filter(it=>it.fav).length
               : (libMode==="recent"||libMode==="marked") ? S.items.length
               : src.filter(it=>it.type!=="dir").length;
  const cntDen = (totalN>seqN) ? ` / ${totalN}` : "";
  const pages = Math.max(1, Math.ceil(seqN/PER));
  if(PAGE>pages) PAGE=pages;
  const _start=(PAGE-1)*PER;
  const take = arr => FLOW ? arr : arr.slice(_start, _start+PER);  // 瀑布流留整份，交给 renderMore 分批续载
  /* 合成导航卡（子文件夹聚合卡 / 返回上级）只在**第 1 页**出现。
     旧版这两张卡是在切片之后才 prepend 的，于是每页首格都被它占着，
     等于翻到第 N 页就白少一个内容位。 */
  const head = FLOW || PAGE===1;
  const mid = tlB ? take(tlB.rows) : take(v);
  let view;
  if(dirOnly){                                  // 展开文件夹：收起卡是唯一出口，常驻；后接本页文件夹
    view=[{type:"dirback"}].concat(take(dcards));
  }else if(ql){                                 // 搜索：命中的文件夹 + 文件一起给
    view=(head?dcards:[]).concat(mid);
  }else if(!dirNav){                            // 收藏 / 最近 / 已标记：纯文件序列
    view=mid;
  }else{                                        // 普通浏览：子文件夹收拢成一张聚合卡
    view=(head && dcards.length ? [{type:"dirgroup", n:dcards.length}] : []).concat(mid);
  }
  S.view=view; S.shown=0; vObs.disconnect(); grid.innerHTML=""; renderMore();
  renderPager(seqN, pages);                     // 底栏分页（无限瀑布流下自动跳过）
  const vtag = libMode==="recent" ? " · 最近使用" : libMode==="marked" ? " · 已标记" : "";
  // 计数口径与左栏「库」一致：合成卡片（子文件夹聚合卡 / 收起文件夹卡 / 时间轴组头）不算条目
  /* F4 P2-8：搜索会同时把**命中的子文件夹**（dcards）与文件一起渲染到网格，
     但计数只算文件 ⇒ 屏上 15 张卡、底栏写「12 项」，与空状态那套（items + dirs）
     自相矛盾。这里把当页实际渲染出来的文件夹数单独标出，两处口径对齐。 */
  const dirShown = (ql && head && dcards.length) ? dcards.length : 0;
  $("#count").textContent = dirOnly ? `${dcards.length} 个文件夹`
    : (tlOn ? `${tlB.meta.length} ${TL_UNIT[TL_GRAIN]} · ${v.length} 项${dirShown?` · ${dirShown} 个文件夹`:""}${vtag}`
            : `${seqN}${cntDen} 项${dirShown?` · ${dirShown} 个文件夹`:""}${vtag}`);
  announceCount($("#count").textContent);           // G2·V40：结果数播报（变化才出声）
  renderChips();
  renderTlUI(tlOn);                                // 时间轴工具条 / 日期索引栏
  syncSideNavs();                                  // 左栏「库 / 收藏 / 格式 / 工作流」同步收尾（P1 抽）
  /* 元数据没到位时按需拉一次。注意判断要放在回调里重算：
     首次进入目录时往往是"无筛选"状态发起的加载，用户随后才输入 model: 语法或选下拉，
     若只在发起时判断，这批加载完成就不会补算，筛选会一直不生效。 */
  if(!S.metaMap && !metaLoading && Date.now()-metaErrAt>10000){
    metaLoading=true;
    const _tok=++metaTok, _p=S.path;
    loadMetaMap().then(r=>{                        // r: "ok" | "empty"（无图可查）| "err"（请求失败）
      metaLoading=false;
      if(r==="err") metaErrAt=Date.now();          // 仅真失败才进冷却，避免出错时反复打请求
      if(_tok!==metaTok || S.path!==_p) return;    // 过期回调（期间切了目录/视图）：交给新一次处理
      patchListMeta(); patchCardDim();             // 网格角标 / 列表视图字段补齐
      if(S.metaMap && _filterNeedsMeta()) applyFilter();   // 当前有待生效的元数据条件 → 重算
    });
  }
}

/* ---------------- 左栏同步收尾（P1，2026-10-09 抽） ----------------
   `applyFilter`（目录 / 收藏 / 标记视图）与 `promptSearch`（提示词搜索）是两条
   独立路径。凡是「决定整个列表内容」的加载，收尾都必须把左栏拉回一致 ——
   原先这四行只写在 applyFilter 末尾，提示词搜索整片换完 S.view 却一个都不调，
   于是整段搜索期间「库」高亮 /「收藏」分类高亮 /「格式·工作流」勾选态全部
   停在进入搜索前的状态（favMode 下搜完仍亮着「收藏夹」）。与 P0-8 抽出
   `_applyCommonFilters` 同一个道理：两条链的收尾也收敛成一处。 */
function syncSideNavs(){
  buildLibNav();                                   // 左栏「库」高亮跟随当前筛选
  syncFavNavSel();                                 // 左栏「收藏」高亮跟随 favMode（离开收藏夹视图即熄灭）
  buildFmtNav(); buildWfNav();                     // 左栏「格式 / 工作流」分区
}

/* ---------------- 公共过滤链（P0-8，审计第 4 级起抽） ----------------
   `applyFilter`（目录/收藏/标记视图）与 `promptSearch`（提示词搜索）是**两条独立路径**：
   数据源不同（S.items / 服务端 /api/search 结果），前段也各筛各的（类型 / 文件名 / 标签集合）。
   但**从「日期 + 格式 + 工作流类型 + 元数据 + 内容安全 + 工作流库分类」起的后段，
   口径必须完全一致** —— 界面上那一排 chip 是共用的，任何一条链漏掉某一项，
   用户就会看到「chip 挂着但条件不生效」。

   历史教训：P1-10 就是上一次漂移的产物（promptSearch 曾整段跳过这些条件）；
   P0-8（本次）又发现 promptSearch 漏了 `markState`（已标记视图）与
   `libMode==="workflow"` 的 `wfClass`（工作流库分类）。所以这里**收敛成唯一实现**，
   两条链都只调这一个函数 —— 以后加条件只需改这一处。

   ★ 调用顺序有讲究，不可随意调换：
     ① dateCutoff 必须在 FMT_COUNTS 统计**之前** —— 左栏格式数字的口径是
        「当前结果集里勾上该格式后能看到的张数」，先按日期收窄再统计才自洽；
     ② FMT_COUNTS 统计必须在 FMTS 过滤**之前** —— 统计的是「可筛张数」而非「已筛结果」；
     ③ 末尾两道是**视图专属**分支（已标记 / 工作流库），只有对应视图开着时才会命中。
   返回收窄后的数组；`v` 是否被就地修改与原实现一致（filter 恒返回新数组）。 */
function _applyCommonFilters(v){
  /* metaReady：依赖元数据的条件（下拉 model/sampler/size、语法 model:·size:·wf:、
     工作流类型 WFK、工作流库 wfClass）在元数据到位前**一律放行**，不参与本轮过滤；
     拿到数据后由 applyFilter 末尾的按需加载回调（_filterNeedsMeta）触发重算补全。
     ⚠️ 不要改成读 S.metaMap 的实时值来决定「要不要跳」—— 两条链的补数时机不同
     （promptSearch 是 ensureMetaFor 按结果集补齐），口径差异会在这里被放大。 */
  const metaReady = !!S.metaMap;
  const cut=dateCutoff($("#dateRange")?$("#dateRange").value:"all");
  if(cut) v=v.filter(it=>it.type==="dir"||it.mtime>=cut);   // 文件夹保留（它只是导航入口）
  /* F1 P2-3：格式计数改为**当前结果集**口径。★ 统计点必须在 FMTS 过滤**之前** ——
     这样左栏数字才等于「勾选该格式后能看到的张数」，与勾选的实际作用范围自洽。
     原来的 S.stats.formats 是根目录全树的全局数字，在子目录里对不上（能筛出 0 张
     却写着 3000 张）。文件夹（type=dir）不计入格式统计，与下面 FMTS 过滤时
     对 dir 一律放行的口径一致。P0-8：统计逻辑已抽到 settings.js 的 recountFmt()，
     提示词搜索走同一条链 ⇒ 左栏数字不再停留在上一次目录筛选的值。 */
  recountFmt(v);
  if(FMTS.size) v=v.filter(it=>it.type==="dir"||FMTS.has((it.path.match(/\.[^.\\/]+$/)||[""])[0].toLowerCase()));   // 格式筛选（可多选）
  if(WFK && metaReady) v=v.filter(it=>it.type==="dir"                        // 工作流类型（文生图/图生图/ControlNet）
    || (S.metaMap && S.metaMap[it.path] && ((S.metaMap[it.path].wfk||"other")===WFK)));
  v=metaFilter(v, metaReady);
  v=nsfwFilter(v);                                  // 内容安全：隐藏模式剔除 NSFW
  /* P0-8 之一：「已标记」主视图里选的标记状态（markState）。
     原先只在 applyFilter 的这段后链里生效，提示词搜索整段没有 ⇒ 搜索框一输入文本
     就绕过了它，而 renderChips() 照旧把那枚 chip 画在搜索框下（界面在说谎）。
     `libMode==="marked"` 才生效：其它视图下这枚控件是隐藏的、值也不参与筛选。 */
  if(libMode==="marked"){
    const ms=S.filters.markState||"*";
    if(ms!=="*") v=v.filter(it=>it.type==="dir" || (ms==="yes" ? !!it.mark : !it.mark));
  }
  /* P0-8 之二：「工作流」主视图里选的生成参数分类（wfClass）。
     归位到公共链后，提示词搜索也受它约束（same as applyFilter，原先只在一侧）。 */
  if(libMode==="workflow" && metaReady){            // 「工作流」：按生成参数分类过滤
    const want=S.filters.wfClass||"any";
    v=v.filter(it=>{
      const m=S.metaMap && S.metaMap[it.path];
      const cls=(m && m.wf) || "";
      if(want==="any") return cls==="full" || cls==="prompt";
      if(want==="none") return cls==="none" || !m;    // 视频/非 ComfyUI 图也算「无生成参数」
      return cls===want;
    });
  }
  return v;
}

/* -------- 重建视图后把 S.idx 重新指回同一张图（V2.9.0 P0 修复）--------
   收藏 / 分类 / 标记变更会重建 S.view；当前图一旦被筛掉，S.idx 就指向了另一个文件，
   而大图里的「删除」走的是 S.view[S.idx] —— 屏幕上看着 A，删掉的却是 B。
   所以凡是「改了属性又要重排」的地方都必须走这里：按路径回找，找不到就关掉大图。 */
function keepIdxByPath(keep){
  if(!keep) return false;
  const v=S.view||[];
  for(let i=0;i<v.length;i++){
    if(v[i] && v[i].path===keep){
      S.idx=i;
      if(typeof buildFilm==="function" && lb && lb.classList.contains("on")) buildFilm();  // 底部预览条高亮跟随
      return true;
    }
  }
  S.idx=-1;
  if(lb && lb.classList.contains("on")) closeLb();   // 已被筛出列表：别让删除键落在别人身上
  return false;
}
/* 重排并保持当前图：keep 省略时取大图当前那张 */
function refreshKeepCur(keep){
  const cur = (S.view && S.idx>=0 && S.view[S.idx]) ? S.view[S.idx].path : "";
  applyFilter();
  keepIdxByPath(keep || cur);
}

/* -------- 删除后的「接替」（V3.0 · 2026-10-12）--------
   原来大图里删掉一张就无条件关掉大图回网格，用户搞不清刚看到哪张。
   这里把 keepIdxByPath 的「保留自己」扩成「保留邻居」，安全语义原样复用：
   按路径回找（**不吃下标** —— 视图可能被筛选 / 排序 / 时间轴分组 / 服务端分页重排），
   找不到就关大图（那条「别让删除键落在别人身上」的兜底不能丢）。 */
function pickSuccessorPath(list, i0, dir){
  if(!list || i0<0 || i0>=list.length) return "";
  /* 与 step()（nav.js）和 openLb()（viewer.js）同一套「可打开」口径：
     聚合卡 dirgroup / 时间轴 tlmore 都不是 image/video，落进去会被 openLb 静默 return，
     而它**不清 stage** ⇒ 大图会停在已删除的那张上（看着像「点了没反应」）。 */
  const ok=t=>!!(t && (t.type==="image" || t.type==="video"));
  for(let n=i0+dir; n>=0 && n<list.length; n+=dir) if(ok(list[n])) return list[n].path;
  for(let n=i0-dir; n>=0 && n<list.length; n-=dir) if(ok(list[n])) return list[n].path;
  return "";
}
/* 声明「视图重建完成后该打开哪一张」，由两条视图路径各自兑现：
     同步（applyFilter 直接重建 S.view）→ 当场兑现
     异步（CMVQuery 走 load，S.view 稍后才回来）→ 由 render() 收尾的 mvConsumePendingFocus() 再兑现
   这样调用点不需要知道当前走的是哪条路。 */
let _focusPend="", _focusPendAt=0;
function focusAfterViewRebuild(path){
  _focusPend=path||""; _focusPendAt=Date.now();
  mvConsumePendingFocus();
}
function mvConsumePendingFocus(){
  if(!_focusPend) return;
  /* 过期作废：若 load 失败 / render 未触发，别让一条旧声明在很久以后才被兑现，
     把用户早已离开的那张图重新翻开。 */
  if(Date.now()-_focusPendAt > 8000){ _focusPend=""; return; }
  /* 视图重建在途 ⇒ 先不动，等收尾时再来一次（此刻兑现只会白关一次大图）。 */
  if(typeof CMVQuery!=="undefined" && CMVQuery.state && CMVQuery.state.loading) return;
  const p=_focusPend; _focusPend="";
  if(keepIdxByPath(p)) openLb(S.idx);      // 找到邻居 → 就地翻过去（不关大图）
  else { closeLb(); S.idx=-1; }            // 真没了 / 被筛掉 → 才退回网格
}
/* 用户自己关大图 / 切目录时作废在途声明（nav.js closeLb 调用）：
   否则「删除 → load 在途 → 用户按 Esc 关掉 → load 回来」会把它又翻开。 */
function mvClearPendingFocus(){ _focusPend=""; }
window.mvConsumePendingFocus=mvConsumePendingFocus;
window.mvClearPendingFocus=mvClearPendingFocus;

/* 当前是否挂着依赖元数据的筛选条件（下拉 / 语法 model:·size:·wf: / 工作流类型 / 工作流库） */
function _filterNeedsMeta(){
  const q=QP;
  return !!($("#fModel").value||$("#fSampler").value||$("#fDim").value)
      || !!(q && (q.model||q.sampler||q.size||q.wf))
      || !!WFK || libMode==="workflow";
}

/* ---------------- 提示词搜索 ---------------- */
/* ε-6 E6-4：本文件原来有个自造的 `let pTok`（只作废「上一次搜索」）。已统一到
   state.js 的 `viewGen` —— 提示词搜索**整片换 S.view**，属权威视图加载，
   所以它既作废在途的旧搜索，也作废在途的目录/收藏夹加载（反之亦然）。
   见 state.js 里「权威视图世代」的用法说明。 */
/* 按需补齐一批路径的元数据（提示词搜索结果可能不在当前目录列表里） */
async function ensureMetaFor(paths){
  const miss=(paths||[]).filter(p=>!S.metaMap || !(p in S.metaMap));
  if(!miss.length) return;
  const j=await api("/api/meta_batch",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({paths:miss})}).catch(()=>({}));
  if(!j || j.error) return;
  S.metaMap=Object.assign({}, S.metaMap||{}, j);
}
let PS_SIG = null;   // F4 P2-6：提示词搜索的筛选签名（条件变化才复位页码，翻页/补算重入不重置）
async function promptSearch(q){
  const tok=++viewGen;
  /* P1（2026-10-09）：先记下大图当前这张。本函数末尾会**整片替换 S.view**，
     需要按它把 S.idx 重算回来（见下方 keepIdxByPath 调用处的说明）。 */
  const _keepCur = (S.view && S.idx>=0 && S.view[S.idx]) ? S.view[S.idx].path : "";
  const qp=QP || parseQuery(q);                   // 结构化条件与普通文本一起生效
  const f=S.filters.type;
  const tagSet = (tagFilterActive && S.tagView) ? new Set(S.tagView.map(x=>x.path)) : null;
  const j=await api("/api/search?q="+encodeURIComponent(q)
    +"&base="+encodeURIComponent(S.path||""))       // 限定当前目录（含子目录）
    .catch(e=>({error:e.message}));
  if(tok!==viewGen) return;                       // 已有更新的搜索，丢弃
  if(j.error){ toast(j.error,"err"); return; }
  let v=j.results.filter(it=>{
    if(tagSet && !tagSet.has(it.path)) return false;      // 标签筛选生效时，搜索结果也要受限
    if(f==="fav"){ if(!it.fav) return false;
      const cat=S.filters.favcat;
      if(cat && cat!=="*" && (it.favcat||"默认")!==cat) return false; }
    else if(f!=="all" && it.type!==f) return false;
    return true;
  });
  if(qp.model || qp.sampler || qp.size || qp.wf){  // 需要元数据的条件：先补齐结果集元数据
    await ensureMetaFor(v.map(it=>it.path));
    if(tok!==viewGen) return;
  }
  v=queryFilter(v, qp);                            // tag:/model:/size:/fav:… 结构化条件
  v=nsfwFilter(v);                                 // 与网格同口径：提示词搜索也要受限
  /* P0-8（审计第 4 级）：后段整段交给 `_applyCommonFilters` —— 与 applyFilter **同一实现**。
     此前这里是 P1-10 手抄的一份拷贝（格式 / 元数据 / 工作流类型 / 日期），
     但漏了 `markState`（已标记视图）与 `libMode==="workflow"` 的 `wfClass`（工作流库分类）：
     用户在这两个视图里一输入文本，条件就静默失效，而 renderChips() 照旧把那两枚 chip
     画上 ⇒ 界面在说谎。收敛为唯一实现后，这类漂移不可能再发生。
     ⚠️ 保持调用点在 `queryFilter` 之后、分页切片之前（与 applyFilter 的相对顺序一致）。 */
  v=_applyCommonFilters(v);
  /* 与 applyFilter 同口径分页：分页模式下只渲染当前页，并刷新底栏页码
     （此前提示词搜索直接灌整批结果，底栏页码停留在上一次目录筛选的页数，点页码会跳错）。
     时间轴同样在**切片之前**分组。 */
  const _tlOn = VIEW_MODE==="time";
  const _tlB = _tlOn ? tlBuildRows(v) : null;
  TL_GROUPS = _tlB ? _tlB.meta : [];
  const _hitN = v.length;                      // 命中总数（切片前）
  let seq = _tlB ? _tlB.rows : v;
  /* F4 P2-6：提示词搜索是**独立于 applyFilter 的路径**，绕过了那句
     `if(_sig!==FILT_SIG){ PAGE=1; }` ⇒ 停在第 5 页时搜新词，只要新结果页数 ≥5
     就仍停在第 5 页，配着「命中 N 项」看起来就像「搜到的东西不对 / 少了很多」。
     用独立签名（与目录筛选同口径）在**条件变化时**才复位；翻页或元数据补算
     触发的重入（q 未变）不重置。 */
  /* P0-8：签名补齐 `markState` 与 `wfClass`（以及影响行数的 FLOW）——
     原先这两项变了、签名不变 ⇒ PAGE 不复位，换条件后仍停在第 N 页，
     和 applyFilter 的 `_sig` 口径也对不上（同名不同义）。 */
  const _psig=[q,f,S.filters.favcat,($("#dateRange")||{}).value||"",
               [...FMTS].sort().join(","),WFK,VIEW_MODE,NSFW_MODE,
               S.filters.wfClass,S.filters.markState,libMode,FLOW,
               TAG_FILTER.join(","),TAG_EXCLUDE.join(","),S.path||""].join("|");
  if(_psig!==PS_SIG){ PS_SIG=_psig; PAGE=1; }
  const totalV=seq.length, pages=Math.max(1, Math.ceil(totalV/PER));
  if(PAGE>pages) PAGE=pages;
  if(!FLOW){ const _s=(PAGE-1)*PER; seq=seq.slice(_s, _s+PER); }
  S.view=seq; S.shown=0; vObs.disconnect(); grid.innerHTML=""; renderMore();
  renderPager(totalV, pages);
  $("#count").textContent=j.building
    ? `索引中 ${j.done}/${j.total} · 命中 ${_hitN}`
    : (_tlOn ? `${_tlB.meta.length} ${TL_UNIT[TL_GRAIN]} · 命中 ${_hitN} 项` : `命中 ${_hitN} 项`);
  renderChips();
  renderTlUI(_tlOn);
  /* P1（2026-10-09）左栏同步：见 syncSideNavs 的注释 —— 提示词搜索此前
     一个都不调，左栏高亮/勾选态整段搜索期间不跟随。 */
  syncSideNavs();
  /* P1（2026-10-09）S.idx 重算：applyFilter 在提示词模式下只是**发起**本函数
     （第 543 行 `promptSearch(q.text); return;`），调用方随后的 keepIdxByPath
     是在旧 S.view 上算的，而这里整片换完列表却不重算 S.idx ⇒ 大图开着时
     S.idx 指向新列表里的另一个文件，「屏幕上看着 A、删掉的却是 B」的旧 bug
     会经这条路径复现。按发起时记下的路径回找（找不到则置 -1 并关掉大图）。 */
  keepIdxByPath(_keepCur);
  if(j.building) setTimeout(()=>{ if(tok===viewGen && $("#qmode").value==="prompt") promptSearch(q); }, 800);
}

