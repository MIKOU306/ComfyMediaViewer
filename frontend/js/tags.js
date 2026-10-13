/* ComfyMediaViewer · js/tags.js（2026-09-09 从 index.html 拆出，加载顺序与原文一致） */
"use strict";
/* ================= 标签系统(P1:大图 chip 编辑 + 卡片 hover) ================= */
let SUGGEST_TAGS = [];              // 全部标签缓存(联想/建议用)
function refreshTagStats(){
  api("/api/tags_list").then(j=>{ SUGGEST_TAGS = (j.tags||[]); }).catch(()=>{});
}
/* 当前大图 item 的标签 chip 面板 */
function renderTagPanel(){
  const panel=$("#tagPanel"); if(!panel) return;
  if(S.solo){ panel.innerHTML=""; return; }   // 单图模式：临时副本没有标签概念
  const it = (S.idx>=0) ? S.view[S.idx] : null;
  if(!it || lb.classList.contains("on")===false){ panel.innerHTML=""; return; }
  const tags = it.tags || [];
  const seg = t => { const i=t.indexOf("/"); return i>=0 ? '<span class="seg">'+esc(t.slice(0,i+1))+'</span>' : ""; };
  let h='<div class="tph">标签<span class="cnt">'+tags.length+'</span></div><div class="tagwrap">';
  for(const t of tags)
    h+='<span class="tagchip '+tagClass(t)+'" data-t="'+esc(t)+'">'+seg(t)+'<span class="leaf">'+esc(t.split("/").pop())+'</span>'
      +'<span class="x" data-rm="'+esc(t)+'" data-tip="摘除">'+ico("x")+'</span></span>';
  h+='<div id="tagInputWrap"><input id="tagInput" placeholder="+ 加标签…" maxlength="40" autocomplete="off">'
    +'<div id="tagSuggest"></div></div></div>';
  panel.innerHTML=h;
  // 摘除
  panel.querySelectorAll(".tagchip .x").forEach(x=>x.onclick=async()=>{
    const t=x.dataset.rm;
    const j=await api("/api/tags",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({op:"remove",path:it.path,tag:t})}).catch(e=>({error:e.message}));
    if(j.error){ toast(j.error,"err"); return; }
    it.tags=j.tags||[];
    renderTagPanel(); refreshTagStats();
  });
  // chip 点击可改名(管理弹窗放 P2,这里点击=无操作占位)
  // 输入联想
  const inp=panel.querySelector("#tagInput");
  const sug=panel.querySelector("#tagSuggest");
  let _sel=0, _items=[];
  function closeSug(){ sug.style.display="none"; _items=[]; _sel=0; }
  function showSug(list){
    /* P1-17：列表换了就必须把高亮下标收回 0。
       此前 showSug 只改 _items，_sel 却留着上一轮的值 —— 上一轮有 5 条、这一轮只有 2 条时，
       _sel=3 越界，回车取到 `_items[3]`（undefined）后回退成输入框文本，于是「明明高亮的是
       第 1 条、回车上屏的却是别的词」。越界一律夹回合法区间。 */
    _items=list;
    if(_sel >= list.length) _sel = 0;
    if(_sel < 0) _sel = 0;
    if(!list.length){ closeSug(); return; }
    sug.innerHTML=list.map((t,i)=>
      '<div class="tsi'+(i===_sel?" sel":"")+'" data-i="'+i+'"><span>'+esc(t.name)+'</span>'
      +'<span class="c">'+t.count+'</span></div>').join("");
    sug.style.display="block";
  }
  inp.oninput=()=>{
    const q=inp.value.trim().toLowerCase();
    if(!q){ closeSug(); return; }
    const has = (it.tags||[]).map(x=>x.toLowerCase());
    const list = SUGGEST_TAGS.filter(t=>!has.includes(t.name.toLowerCase())
      && t.name.toLowerCase().includes(q)).slice(0,8);
    const exact=list.some(t=>t.name.toLowerCase()===q);
    if(!exact && q.length>=1)
      list.push({name:inp.value.trim(), count:0, _new:true});   /* ε-11 §6.4：保留用户输入的大小写（q 只用于匹配比较，后端 _norm_tag 不做小写归一） */
    showSug(list);
  };
  inp.onkeydown=async e=>{
    if(e.isComposing || e.keyCode===229) return;   // 中文输入法选词的回车不是提交
    if(e.key==="ArrowDown"){ e.preventDefault(); moveSel(1); }
    else if(e.key==="ArrowUp"){ e.preventDefault(); moveSel(-1); }
    else if(e.key==="Escape"){ closeSug(); inp.value=""; }
    else if(e.key==="Enter"){
      e.preventDefault(); closeSug();
      const q=inp.value.trim();
      /* P1-17 兜底：只认下标合法的项。越界/为空一律回退到输入框原文，
         绝不拿 `_items[_sel]` 里一个 undefined 去猜名字。 */
      const cur = (_sel>=0 && _sel<_items.length) ? _items[_sel] : null;
      const tag = (cur && cur.name) ? cur.name : q;
      if(!tag) return;
      await tagAdd(it, tag);
      inp.value=""; _sel=0;
      inp.focus();
    } else if(e.key==="Tab"){ closeSug(); }
  };
  function moveSel(d){
    _sel=Math.max(0, Math.min(_items.length-1, _sel+d));
    sug.querySelectorAll(".tsi").forEach((el,i)=>el.classList.toggle("sel",i===_sel));
  }
  sug.onmousedown=e=>e.preventDefault();      // 防失焦
  sug.onclick=async e=>{
    const el=e.target.closest(".tsi"); if(!el) return;
    const item=_items[+el.dataset.i];
    const tag=item?item.name:"";
    closeSug(); inp.value="";
    if(tag) await tagAdd(it, tag);
  };
}
async function tagAdd(it, tag){
  if(!tag || !it || !it.path) return;
  if((it.tags||[]).some(t=>t.toLowerCase()===tag.toLowerCase())){
    toast("已存在该标签"); return;
  }
  const j=await api("/api/tags",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({op:"add",path:it.path,tag:tag})}).catch(e=>({error:e.message}));
  if(j.error){ toast(j.error,"err"); return; }
  it.tags=j.tags||[];
  toast("已贴「"+tag+"」","ok");
  renderTagPanel(); refreshTagStats(); refreshStatsSoon();
  syncCardTags(it);
}
/* 同步网格中该卡的 hover 标签行 */
function syncCardTags(it){
  const tags=it.tags||[];
  const _ce = (typeof S !== "undefined" && S.cardEl) ? S.cardEl.get(it) : null;
  let cardEl = (_ce && _ce.isConnected) ? _ce : null;
  // 列表视图的标签是纯文本列，走这里同步（网格视图才有 hover 行）

  if(!cardEl){                                  // 网格重渲染后旧引用失效：按路径回查
    const g=document.getElementById("grid");
    if(g) cardEl = [...g.querySelectorAll("[data-p]")].find(c=>c.dataset.p===it.path) || null;
  }
  if(!cardEl) return;
  syncCardTagsInto(cardEl, it);
}
/* §6.3：命中卡片后的写入段（列表列 / hover 行）。单卡与批量路径共用，
   单卡回查是 O(n)、一次点击无感；批量（prepCardTags）必须走映射表。 */
function syncCardTagsInto(cardEl, it){
  const tags=it.tags||[];
  if(LIST_VIEW){
    const cell=cardEl.querySelector(".lv-tags");
    if(cell) fillLvTags(cell, it);           // V4 批次 1：这一列含标记图标，统一走组装器
    return;
  }
  let row=cardEl.querySelector(".crdtags .tgs") || cardEl.querySelector(".crdtags");
  if(!row) return;
  const show=tags.slice(0,2);
  row.innerHTML = show.map(t=>'<span class="ct">'+tagDot(t)+esc(t.split("/").pop())+'</span>').join("")
    + (tags.length>2?'<span class="ct more">+'+(tags.length-2)+'</span>':"");
}
/* 卡片构建时若带标签,hover 行预埋 */
function prepCardTags(){
  if(!S.view) return;
  /* §6.3：原来逐项调 syncCardTags —— WeakMap miss/失效（网格刚重渲染）时
     每一项都全量 querySelectorAll 按路径回查，带标签项一多就是 O(n²)
     （实测几千项全带标签时冻结数百 ms~秒级）。改为单次遍历建 path→el
     映射（O(n)），逐项 Map 命中；单卡路径（贴/删标签后的刷新）保持原样。 */
  const g=document.getElementById("grid");
  let map=null;
  if(g){
    map=new Map();
    for(const c of g.querySelectorAll("[data-p]")) map.set(c.dataset.p, c);
  }
  for(let i=0;i<S.view.length;i++){
    const it=S.view[i];
    if(!it||!it.tags||!it.tags.length) continue;
    const _ce=(typeof S!=="undefined"&&S.cardEl)?S.cardEl.get(it):null;
    const cardEl=(_ce&&_ce.isConnected)?_ce:(map?(map.get(it.path)||null):null);
    if(cardEl) syncCardTagsInto(cardEl, it);
  }
}
/* 打开大图时渲染标签面板 */
window._renderTagPanelOnOpen=function(){
  if(S.solo) return;   // 单图模式：S.idx 指的是库里上一张，别拿它去算建议
  const it = (S.idx>=0) ? S.view[S.idx] : null;
  if(it){
    renderTagPanel(); setTimeout(prepCardTags,0);
    // meta 就绪后(轮询检测 curMeta 变化)渲染 AI 建议
    const _i=S.idx, _p=it.path;                 // 轮询期间可能已切图：用索引+路径双重校验
    const _t=setInterval(()=>{
      if(!lb.classList.contains("on")){ clearInterval(_t); return; }
      if(S.curMeta){ clearInterval(_t);
        const cur=S.view[S.idx];
        if(S.idx===_i && cur && cur.path===_p) renderTagSuggestions(it); }
    },300);
  }
};

/* ================= 标签筛选 P2:左栏 AND 累加 ================= */
let TAG_FILTER = [];            // 当前激活的标签
let TAG_EXCLUDE = [];           // 单独排除的标签（与 TAG_FILTER 互斥，右键标签行切换）
let TAG_MODE = "and";           // 多标签匹配：and=同时满足 / or=任一满足 / not=都不包含（排除）
try{ const m=localStorage.getItem("mv.tagmode"); if(m==="and"||m==="or"||m==="not") TAG_MODE=m; }catch(_){}
let tagListCache = [];          // 完整标签统计(渲染左栏)
let tagFilterActive = false;    // 当前视图是否处于标签筛选结果

/* 渲染左栏标签导航(带层级折叠,Top14 + 「全部」入口)
   ⚠️ E5-3（2026-09-23）订正：这行注释此前写的是「Top12 + 全部入口」—— 与实现**两处都不符**
   （实为 Top14，且**根本没有**「全部」入口）。实测的后果比报告写的更差：
     · 分组溢出那行（`… 还有 N 个子标签`）是 cursor:default 的**死文本**，点不动；
     · **扁平标签超过 14 个是「静默丢弃」** —— 连「还有 N 个」都不显示，用户不知道自己漏看了。
   现在两处都补可点的「展开全部 / 收起」，展开状态记在 TAG_NAV_MORE（跨重渲染保持）。 */
const TAG_NAV_TOP=14, TAG_NAV_KIDS=8;
let TAG_NAV_MORE=new Set();                     // '#flat' 或组名
function tagMoreRow(n, lvl, key, expanded){
  const pad = 9 + lvl*14;
  return '<div class="tlvl more" data-t="__more__" data-k="'+esc(key)+'" role="button" tabindex="0"'
    +' data-tip="'+(expanded?"收起这一组":"展开其余 "+n+" 个")+'"'
    +' style="padding-left:calc('+pad+'px*var(--fsx))">'
    +'<span class="ic">'+ico(expanded?"caretUp":"caretDown")+'</span>'
    +'<span class="nm">'+(expanded?"收起":"全部标签 · 还有 "+n+" 个")+'</span></div>';
}
function renderTagNav(){
  const nav=$("#tagNav"); if(!nav) return;
  refreshTagStatsNow(stats=>{
    tagListCache = stats||[];
    /* 层级标签的展示规则（此前会重复）：
       后端 tag_stats() 已把子标签计入父级链（人物/黑发 → 人物 也 +1），所以
         ① 有子标签的父级只在「分组头」渲染一次，不再同时出现在平铺标签列表里；
         ② 父级计数直接取统计值，不再把子项相加（那会重复计数）。 */
    const active=new Set(TAG_FILTER);
    const byName=new Map(stats.map(t=>[t.name,t.count]));
    const groups={};                                  // 父级名 → 子标签列表
    for(const t of stats){
      const i=t.name.indexOf("/");
      if(i<0) continue;
      const p=t.name.slice(0,i);                      // 只按第一段分组
      (groups[p]=groups[p]||[]).push(t);
    }
    const rows=[];                                    // {count, html}：顶层按计数降序排列
    const flats=stats.filter(t=>!t.name.includes("/") && !groups[t.name]);
    const flatOpen=TAG_NAV_MORE.has("#flat");
    for(const t of (flatOpen?flats:flats.slice(0,TAG_NAV_TOP)))
      rows.push({count:t.count,
        html:tagRow(t.name, t.count, 0, active.has(t.name), false)});
    if(flats.length>TAG_NAV_TOP){
      /* ⚠️ 这条独立成行、count=-1 ⇒ 排到最后。**不能**把它并进上面那 14 行做成一个整体行 ——
         那会把「扁平标签与分组行按计数交错」的既有版面改掉（每行独立参与排序是既有行为）。 */
      rows.push({count:-1,
        html:tagMoreRow(flats.length-TAG_NAV_TOP, 0, "#flat", flatOpen)});
    }
    for(const [p, kids] of Object.entries(groups)){
      const gOpen=TAG_NAV_MORE.has(p);
      let html=tagRow(p, byName.get(p)||0, 0, active.has(p), true);
      for(const k of (gOpen?kids:kids.slice(0,TAG_NAV_KIDS)))
        html+=tagRow(k.name, k.count, 1, active.has(k.name), false);
      if(kids.length>TAG_NAV_KIDS)
        html+=tagMoreRow(kids.length-TAG_NAV_KIDS, 1, p, gOpen);
      rows.push({count:byName.get(p)||0, html});      // 展开行在组内 ⇒ 排序后仍紧随其子项
    }
    rows.sort((a,b)=>b.count-a.count);                // 父级行插到它该在的位置，子项紧随其后
    nav.innerHTML=rows.map(r=>r.html).join("") + tagClearRow();
    bindTagNav();
  });
}
/* 标签配色（设计规范 2.5「语义化标签色」）：
   先按标签名里的关键词归类到 8 组柔和半透明色（风景→青、人物→粉、动物→紫…），
   命中不到关键词时按名字哈希兜底，保证同名标签在左栏、卡片、大图面板里始终同色。
   CSS 侧只认类名 .tg-*（见 css/tags.css），改色不用改这里。 */
const TAG_CLASSES = ["teal","pink","purple","yellow","orange","green","blue","red"];
const TAG_GROUPS = [
  ["teal",   ["风景","景","海","湖","山","天空","云","自然","户外","landscape","scenery","sea","ocean","sky","nature","mountain"]],
  ["pink",   ["人物","人像","肖像","女孩","男孩","美食","食物","portrait","person","girl","boy","face","food","woman","man"]],
  ["purple", ["动物","猫","狗","鸟","兽","动漫","二次元","插画","animal","cat","dog","bird","anime","manga","illustration"]],
  ["yellow", ["城市","街","都市","科幻","赛博","夜景","city","street","urban","scifi","sci-fi","cyber","neon","night"]],
  ["orange", ["建筑","室内","机械","科技","机甲","architecture","building","indoor","tech","mecha","robot"]],
  ["green",  ["植物","花","草","树","森林","plant","flower","tree","forest","leaf"]],
  ["blue",   ["空间","太空","宇宙","抽象","其他","space","cosmos","abstract","other"]],
  ["red",    ["最近","最新","收藏","重要","recent","favorite","fav","important"]],
];
function tagClass(name){
  const full=String(name||"").toLowerCase();
  const leaf=full.split("/").pop();
  for(const [cls,keys] of TAG_GROUPS)
    for(const k of keys)
      if(full.includes(k) || leaf.includes(k)) return "tg-"+cls;
  let h=0; const s=String(name||"");
  for(let i=0;i<s.length;i++) h=(h*31+s.charCodeAt(i))>>>0;
  return "tg-"+TAG_CLASSES[h%TAG_CLASSES.length];
}
/* 圆点：类名驱动配色，样式全在 css/tags.css 的 .tg-* 里 */
function tagDot(name){
  return '<i class="tdot '+tagClass(name)+'"></i>';
}
function tagRow(name, count, lvl, on, isHeader){
  const ex = TAG_EXCLUDE.includes(name) || (TAG_MODE==="not" && on);   // 被排除：⊘ 红色
  const ic = ex ? '<span class="ic">'+ico("ban")+'</span>'
    : (isHeader ? '<span class="ic">'+ico("caretDown")+'</span>'
    : (lvl ? '<span class="ic">'+ico("dot")+'</span>' : '<span class="ic tdotwrap">'+tagDot(name)+'</span>'));
  const pad = 9 + lvl*14;
  const tip = ex ? "已排除：点击取消排除 · 右键切换" : "点击筛选 · 右键排除该标签";
  return '<div class="tlvl'+(on?" on":"")+(ex?" ex":"")+'" data-t="'+esc(name)+'" data-tip="'+tip+'"'
    +' style="padding-left:calc('+pad+'px*var(--fsx))">'
    +ic+'<span class="nm">'+esc(name)+'</span>'
    +'<span class="c">'+count+'</span></div>';
}
function tagFilterDesc(){
  let d="";
  if(TAG_FILTER.length)
    d = TAG_MODE==="not" ? TAG_FILTER.map(t=>"非 "+t).join(" ")
                         : TAG_FILTER.join(TAG_MODE==="or"?" 或 ":" + ");
  if(TAG_EXCLUDE.length)
    d = (d?d+" ":"") + TAG_EXCLUDE.map(t=>"非 "+t).join(" ");
  return d;
}
function tagClearRow(){
  const none = !TAG_FILTER.length && !TAG_EXCLUDE.length;
  const d = tagFilterDesc();
  return '<div class="litem'+(none?" on":"")+'" data-t="__clear__"><span class="ic">'+ico("gridTable")+'</span><span class="nm">全部图片</span>'
    +(d?'<span class="cnt">'+esc(d)+'</span>':"")+'</div>';
}
function bindTagNav(){
  const nav=$("#tagNav");
  nav.querySelectorAll("[data-t]").forEach(el=>{
    el.onclick=()=>{
      const t=el.dataset.t;
      if(t==="__clear__"){ TAG_EXCLUDE=[]; setTagFilter([]); return; }
      if(t==="__more__"){                              // E5-3：展开/收起（**不是**筛选，别落进下面）
        const k=el.dataset.k||"";
        if(TAG_NAV_MORE.has(k)) TAG_NAV_MORE.delete(k); else TAG_NAV_MORE.add(k);
        renderTagNav(); return;
      }
      if(TAG_EXCLUDE.includes(t)){                     // 已排除的：点击=取消排除
        TAG_EXCLUDE=TAG_EXCLUDE.filter(x=>x!==t);
        setTagFilter([...TAG_FILTER]);
        return;
      }
      const i=TAG_FILTER.indexOf(t);
      const next = i>=0 ? TAG_FILTER.filter(x=>x!==t) : [...TAG_FILTER, t];
      setTagFilter(next);
    };
    /* E5-3：展开行带了 tabindex=0 ⇒ 必须配键盘触发，否则是「可聚焦但无响应」
       （WCAG 2.1.1 键盘可达性反例，比不给它 tabindex 更糟）。其余标签行维持既有鼠标语义。 */
    if(el.dataset.t==="__more__")
      el.onkeydown=e=>{ if(e.key==="Enter"||e.key===" "){ e.preventDefault(); el.click(); } };
    el.oncontextmenu=e=>{                              // 右键：切换「排除该标签」
      const t=el.dataset.t;
      if(t==="__clear__") return;
      if(t==="__more__") return;                       // E5-3：展开行没有「排除」语义
      e.preventDefault(); e.stopPropagation();
      if(TAG_EXCLUDE.includes(t)) TAG_EXCLUDE=TAG_EXCLUDE.filter(x=>x!==t);
      else { TAG_EXCLUDE=[...TAG_EXCLUDE, t]; TAG_FILTER=TAG_FILTER.filter(x=>x!==t); }
      if(TAG_FILTER.length || TAG_EXCLUDE.length) setTagFilter([...TAG_FILTER]);
      else setTagFilter([]);
    };
  });
}
/* 应用标签筛选:走 /api/tags_filter 全量换 S.view(保留 S.path 供退出恢复) */
/* ε-6 E6-4：这里原来是本文件**自造**的 `tagTok`。它与 app.js 的 `loadTok`、
   filters.js 的 `pTok` 是同一个病的三份药 —— 各自只能作废「自己这一类」的在途请求，
   于是「切目录」作废不了在途的标签筛选（慢的 /api/tags_filter 后到 ⇒ 刚切到的目录
   又被换成上一次的标签结果，而路径框/面包屑仍显示目录态）。
   统一到 state.js 的 `viewGen`：**凡决定整个列表内容**的加载（load / loadFavView /
   setTagFilter / promptSearch）都是权威视图加载，一律 `++viewGen`。 */
async function setTagFilter(tags){
  /* P1（2026-10-09）：进入先快照。下面 TAG_FILTER 是**同步**改的，而
     tagFilterActive / S.tagView 只在请求成功后才更新 —— 失败时若不回滚，
     界面会停在「chip 说筛了狗、网格还是猫（或干脆是目录列表）」的自相矛盾态，
     而且只有一句 3.2 秒就消失的 toast。回滚需要调用前的原值。 */
  const _prevF=TAG_FILTER, _prevE=TAG_EXCLUDE, _prevActive=tagFilterActive;
  TAG_FILTER=tags;
  const tok=++viewGen;
  renderTagNav();                     // 立即刷新高亮(含"全部图片"清除行)
  if(!TAG_FILTER.length && !TAG_EXCLUDE.length){
    S.tagView=null;
    if(tagFilterActive){
      tagFilterActive=false;
      S.view=[]; S.shown=0;
      vObs.disconnect(); grid.innerHTML="";
      if(S.metaMap) await loadMetaMap();      // 元数据面板开着：基准列表切回目录列表
      if(tok!==viewGen) return;
      applyFilter();                          // 用内存里的目录列表重建视图；不动元数据筛选
      toast("已退出标签筛选");
    }
    return;
  }
  toast("按标签筛选: "+tagFilterDesc());
  try{
    const j=await api("/api/tags_filter?tags="+encodeURIComponent(TAG_FILTER.join("|"))
      +"&mode="+TAG_MODE
      +"&not="+encodeURIComponent(TAG_EXCLUDE.join("|"))
      +"&path="+encodeURIComponent(S.path||S.root));
    if(tok!==viewGen) return;
    tagFilterActive=true;
    S.tagView=j.items||[];              // 标签筛选的基准列表：后续本地筛选都基于它
    S.shown=0;
    vObs.disconnect(); grid.innerHTML="";
    if(S.metaMap) await loadMetaMap();       // 先让元数据映射覆盖标签结果，再叠加筛选
    if(tok!==viewGen) return;
    applyFilter();                           // 叠加类型/时间/排序/元数据筛选（不再直接 renderMore）
    $("#count").textContent=S.view.filter(x=>!TL_SYNTH[x.type]).length+" 项 · 标签";
  }catch(e){
    /* P1（2026-10-09）：失败必须回滚到调用前 —— 否则 chip 与结果长期不符。
       仅当本次仍是最新一代才回滚：tok!==viewGen 说明已被更新的一次调用接管，
       那种情况由新调用决定最终状态，老的乱回滚反而会覆盖新的。 */
    if(tok===viewGen){
      TAG_FILTER=_prevF; TAG_EXCLUDE=_prevE; tagFilterActive=_prevActive;
      renderTagNav();
      if(typeof renderChips==="function") renderChips();
      applyFilter();                     // 用回滚后的条件把视图拉回一致
    }
    toast("标签筛选失败:"+e.message,"err");
  }
}

/* 标签管理弹窗:改名/删除 */
function openTagManager(){
  const dlg=bigDialog("标签管理");
  refreshTagStatsNow(stats=>{
    if(!dlg.mask.isConnected) return;
    if(!stats || !stats.length){
      dlg.body.innerHTML='<div style="color:var(--tx3)">还没有标签。在图片详情面板或批量操作里打标签后,这里可全局改名/删除。</div>';
      return;
    }
    let h='<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px">';
    for(const t of stats)
      h+='<div class="tagchip '+tagClass(t.name)+'" data-t="'+esc(t.name)+'" style="cursor:default"><span class="leaf">'+esc(t.name)+'</span>'
        +'<span style="color:var(--tx3);font-size:calc(10px*var(--fsx))">'+t.count+'</span>'
        +'<span class="x" data-del="'+esc(t.name)+'" data-tip="删除(从所有图摘除)">'+ico("x")+'</span></div>';
    h+='</div><div style="display:flex;gap:8px;align-items:center;margin-top:4px">'
      +'<input id="mgRenameFrom" placeholder="要改名的标签(可从上面点击填入)" style="flex:2;min-width:140px">'
      +'<span style="color:var(--tx3)">→</span>'
      +'<input id="mgRenameTo" placeholder="新名字" style="flex:1;min-width:100px">'
      +'<button id="mgRenameGo" class="primary">改名合并</button></div>';
    dlg.body.innerHTML=h;
    // 点 chip 填入 renameFrom
    dlg.body.querySelectorAll(".tagchip").forEach(ch=>{
      ch.addEventListener("click",e=>{
        if(e.target.dataset.del) return;
        dlg.body.querySelector("#mgRenameFrom").value=ch.dataset.t;
      });
      const x=ch.querySelector(".x");
      if(x) x.onclick=async e=>{
        e.stopPropagation();
        const t=x.dataset.del;
        confirmDialog("删除标签","从所有图片上摘除「"+t+"」?该操作不可撤销(可重新打标)。",async()=>{
          const j=await api("/api/tags",{method:"POST",headers:{"Content-Type":"application/json"},
            body:JSON.stringify({op:"delete",tag:t})}).catch(e=>({error:e.message}));
          if(j.error){ toast(j.error,"err"); return; }
          toast("已删除标签「"+t+"」("+j.removed+" 张图)","ok");
          SUGGEST_TAGS=(j.tags_all||[]);
          applyTagRenameLocal(new Map([[t,null]]));      // P1-13：内存标签同步摘除
          dlg.close();
          openTagManager();
          renderTagNav();
        });
      };
    });
    const go=dlg.body.querySelector("#mgRenameGo");
    go.onclick=async()=>{
      const f=dlg.body.querySelector("#mgRenameFrom").value.trim();
      const t=dlg.body.querySelector("#mgRenameTo").value.trim();
      if(!f||!t){ toast("请填写两个名字","err"); return; }
      const j=await api("/api/tags",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({op:"rename",from:f,to:t})}).catch(e=>({error:e.message}));
      if(j.error){ toast(j.error,"err"); return; }
      toast("已改名:「"+f+"」→「"+t+"」("+j.moved+" 张图)","ok");
      SUGGEST_TAGS=(j.tags_all||[]);
      applyTagRenameLocal(tagRenameMap(f, t));       // P1-13：内存标签同步改名（含子标签）
      dlg.close();
      openTagManager();
      renderTagNav();
    };
  });
}

/* 同步拉取标签统计(带回调) */
let _tsBusy=false;
/* P1-13：标签**全局改名 / 删除**是在服务端批量改所有图，前端内存里的
   `S.items[].tags` / `S.tagView[].tags` / 大图当前项的 tags 都还留着旧名字 ——
   此前只调 renderTagNav()，于是左栏显示新名、卡片与大图 chip 仍是旧名，
   界面自相矛盾。这里就地重写内存标签并回写已渲染的卡片，不重新拉整列表
   （重拉会丢滚动位置与翻页进度，且改名这种纯标签操作没必要重扫目录）。
   新名用一个 Map 表达：{旧名: 新名|null}，null = 删除。 */
function applyTagRenameLocal(map){
  /* ⚠️ map 是 **Map** 不是普通对象：`Object.keys(map)` 恒为空数组，
     早先写成 `!Object.keys(map).length` 会让本函数永远提前 return 0（静默失效）。
     判空要走 map.size。 */
  if(!map || !map.size) return 0;
  /* 子标签的改名规则**不依赖 SUGGEST_TAGS**（它是异步拉的，改名时可能还没到位，
     真页面实测就出现过「只改了父标签、风格/汉服 原样不动」）。
     这里按前缀就地推导：任何以 `旧名/` 开头的标签都改成 `新名/` + 余下部分。 */
  const rules=[];                              // [{from, to}]  to=null 表示删除
  for(const [k,v] of map) rules.push({from:k, to:v});
  /* 最长前缀优先：先处理 `风格/汉服`，再处理 `风格`，避免短规则把长标签先切掉 */
  rules.sort((x,y)=>y.from.length-x.from.length);
  const fix = (arr)=>{
    if(!Array.isArray(arr)) return;
    for(const r of rules){
      for(let i=arr.length-1;i>=0;i--){
        const t=arr[i];
        if(t===r.from){
          if(r.to===null) arr.splice(i,1);
          else arr[i]=r.to;
          continue;
        }
        /* P1（2026-10-09）：删除（to===null）也要按前缀连带子标签。
           后端 tags 的删除是 `x != n and not x.startswith(n+"/")` —— 删「风格」
           会连带删掉「风格/汉服」；前端原来只在 to!==null（改名）时走前缀规则，
           于是左栏已经没有子标签、卡片 hover 行与大图 chip 仍写着「风格/汉服」，
           一直挂到下次整列表刷新。这里与后端对齐。 */
        if(t.startsWith(r.from+"/")){
          if(r.to===null) arr.splice(i,1);
          else arr[i]=r.to+t.slice(r.from.length);
        }
      }
    }
    // 去重（改名后可能与已有标签撞名），保持首次出现顺序
    const seen=new Set();
    for(let i=0;i<arr.length;i++){
      if(seen.has(arr[i])){ arr.splice(i,1); i--; }
      else seen.add(arr[i]);
    }
  };
  let touched=0;
  const seen=new Set();
  const visit=(arr)=>{
    for(const it of (arr||[])){
      if(!it || typeof it!=="object" || !Array.isArray(it.tags)) continue;
      if(seen.has(it)) continue;
      seen.add(it);
      const before=it.tags.join("\u0000");
      fix(it.tags);
      if(it.tags.join("\u0000")!==before){
        touched++;
        if(typeof syncCardTags==="function") syncCardTags(it);   // 卡片 hover 行 / 列表「标签」列
      }
    }
  };
  visit(S.view); visit(S.tagView); visit(S.items);
  /* 注：S.dirs 只是子文件夹卡片 {name,path,n}，不带 items，无需遍历 */
  /* 大图正开着 → 重新渲染元数据面板（标签 chip 在其中；用当前已缓存的 meta，不重发请求） */
  if(typeof curItem==="function" && S.curMeta){
    const cur=curItem();
    if(cur && typeof renderMeta==="function") renderMeta(S.curMeta, cur);
  }
  return touched;
}
/* 把「旧名→新名」的清单整理成 Map（含父标签：改名「风格」时「风格/汉服」也要变） */
function tagRenameMap(from,to){
  const m=new Map(); m.set(from, to);
  for(const t of (SUGGEST_TAGS||[])){
    const n=t && t.name;
    if(n && n!==from && n.startsWith(from+"/")) m.set(n, to+n.slice(from.length));
  }
  return m;
}

function refreshTagStatsNow(cb){
  api("/api/tags_list").then(j=>{ if(cb) cb(j.tags||[]); }).catch(()=>{ if(cb) cb([]); });
}

/* ================= P3:批量标签 / 右键子菜单 / AI 建议 ================= */

/* --- 批量给 batchSel 贴标签 --- */
function openBatchTag(){
  const n=batchSel.size;
  if(!n){ toast("请先选择图片","err"); return; }
  const dlg=bigDialog("批量贴标签（"+n+" 张）");
  dlg.body.innerHTML='<div style="margin-bottom:10px;font-size:calc(12px*var(--fsx));color:var(--tx3)">'
    +'勾选要贴到全部 '+n+' 张上的标签；已存在的不会重复。也可输入新标签回车添加。</div>'
    +'<div id="btChips" class="tagwrap" style="margin-bottom:10px"></div>'
    +'<div style="display:flex;gap:6px;align-items:center;margin-bottom:14px">'
    +'<input id="btNew" placeholder="新标签名…" style="flex:1;min-width:120px" maxlength="40">'
    +'<button id="btAddNew" class="primary">'+ico("plus")+' 添加</button></div>'
    +'<div class="cbtns" style="display:flex;justify-content:flex-end;gap:8px">'
    +'<button id="btCancel">取消</button><button id="btGo" class="primary" disabled>贴到 '+n+' 张</button></div>';
  const chipBox=dlg.body.querySelector("#btChips");
  const picked=new Set();
  let pendingNew="";
  function renderChips(){
    chipBox.innerHTML="";
    for(const t of SUGGEST_TAGS){
      if(t.count===0) continue;
      chipBox.insertAdjacentHTML("beforeend",
        '<span class="tagchip '+tagClass(t.name)+'" data-t="'+esc(t.name)+'" style="'+(picked.has(t.name)
          ?'background:var(--ac-soft);border-color:var(--ac)':"")+'">'
        +'<span class="leaf">'+esc(t.name)+'</span><span class="c" style="color:var(--tx3);font-size:calc(10px*var(--fsx))">'+t.count+'</span></span>');
    }
    for(const t of picked){ if(!SUGGEST_TAGS.some(x=>x.name===t)){
      chipBox.insertAdjacentHTML("beforeend",
        '<span class="tagchip" data-t="'+esc(t)+'" style="background:var(--ac-soft);border-color:var(--ac)">'
        +'<span class="leaf">'+esc(t)+'</span></span>'); } }
    chipBox.querySelectorAll(".tagchip").forEach(ch=>ch.onclick=()=>{
      const t=ch.dataset.t;
      if(picked.has(t)) picked.delete(t); else picked.add(t);
      renderChips();
      dlg.body.querySelector("#btGo").disabled=!picked.size;
    });
  }
  renderChips();
  const addPending=()=>{
    const v=dlg.body.querySelector("#btNew").value.trim();
    if(!v) return;
    pendingNew=v;
    picked.add(v);
    SUGGEST_TAGS=[{name:v,count:0},...SUGGEST_TAGS.filter(x=>x.name!==v)];
    dlg.body.querySelector("#btNew").value="";
    renderChips();
    dlg.body.querySelector("#btGo").disabled=false;
  };
  dlg.body.querySelector("#btNew").onkeydown=e=>{ if(e.isComposing || e.keyCode===229) return;   // 同上
    if(e.key==="Enter"){ e.preventDefault(); addPending(); } };
  dlg.body.querySelector("#btAddNew").onclick=addPending;
  dlg.body.querySelector("#btCancel").onclick=dlg.close;
  dlg.body.querySelector("#btGo").onclick=async()=>{
    if(!picked.size) return;
    const paths=[...batchSel];
    const tags=[...picked];
    /* P1-18：原来是 N×M 次串行 await，且 `ok++` 写在内层循环外 —— 成功计数恒等于
       选中张数，全失败也照样报「已给 N 张图贴上 M 个标签」。现在改走服务端单请求
       （op:add_many），一次锁一次落盘，并按**服务端返回的真实对数**汇报。
       失败也逐条说清是哪一张、为什么，而不是含糊一句「失败 X 次」。 */
    const go=dlg.body.querySelector("#btGo");
    go.disabled=true;
    const oldTxt=go.textContent;
    setBotState("busy",`正在为 ${paths.length} 张图贴上 ${tags.length} 个标签…`,null,true);
    let j;
    try{
      j=await api("/api/tags",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({op:"add_many",paths:paths,tags:tags})});
    }catch(e){ j={error:e.message}; }
    go.disabled=false; go.textContent=oldTxt;
    if(j.error){
      dlg.close();
      setBotState("err","贴标签失败",null,false);
      toastErr("贴标签失败", j.error);
      return;
    }
    /* P1（2026-10-09）两条口径修正：
       ① `j.ok` 是**成功标志**（后端返回前会置 True），拿它当计数 ⇒ `pairs`
          恒为布尔，底栏「共 true 处新增」、toast 里「部分图本来已有该标签」
          的归因全是假的。计数改读 `j.pairs`，并对后端再次漂移兜底。
       ② 落盘失败的记录 `path` 是**空串**（后端目前只有整表级失败这一支），
          原来 `filter(x=>x.path)` 把真正的写盘失败全滤掉 ⇒ failedN 恒 0，
          界面把「磁盘写不进去」说成「这几张图早就贴过了」，用户不去查数据
          目录只会反复重试。改按**条数**判，并把原始异常摆到详情里。 */
    const added=j.files||0;
    const pairs=Number.isFinite(j.pairs)?j.pairs:added;
    const fails=(j.failed||[]);
    const failedN=fails.length;
    dlg.close();
    if(added===0){
      setBotState(failedN?"err":"warn", failedN?"贴标签失败":"没有新增标签", null, false);
      toastErr("没有贴上任何标签",
               failedN ? ("写入标签失败，请检查数据目录是否可写"
                          + ((fails[0]&&fails[0].error) ? "（"+fails[0].error+"）" : ""))
                       : "所选的标签这几张图上都已经有了");
    }else{
      setBotState("done",`已给 ${added} 张图贴上标签（共 ${pairs} 处新增）`,null,false);
      toast(`已给 ${added} 张图贴上标签`+(pairs!==added?`（共 ${pairs} 处新增，部分图本来已有该标签）`:"")
            +(failedN?`；${failedN} 处未成功`:""));
    }
    /* 内存同步：把新增的标签就地并进这些图，避免等下次整列表刷新才看到 */
    if(added){
      const seen=new Set();
      for(const it of ((S.view||[]).concat(S.tagView||[], S.items||[]))){
        if(!paths.includes(it.path) || seen.has(it)) continue;
        seen.add(it);
        it.tags=Array.from(new Set([...(it.tags||[]), ...tags]));
        if(typeof syncCardTags==="function") syncCardTags(it);
      }
    }
    refreshTagStats(); refreshStatsSoon();
  };
}

/* --- AI 建议词表:从 PNG 内嵌正向提示词提风格/服装/发色/场景 --- */
const SUGGEST_LEXICON=[
  // 服装/风格大类
  {re:/汉服|hanfu|hanfu|中国风|古风|华服/i, tag:"风格/汉服"},
  {re:/旗袍|qipao/i, tag:"风格/旗袍"},
  {re:/和服|kimono/i, tag:"风格/和服"},
  {re:/cosplay|cos/i, tag:"风格/Cosplay"},
  {re:/赛博|cyberpunk|cyber/i, tag:"风格/赛博"},
  {re:/蒸汽朋克|steampunk/i, tag:"风格/蒸汽朋克"},
  {re:/洛丽塔|lolita/i, tag:"风格/洛丽塔"},
  {re:/哥特|gothic/i, tag:"风格/哥特"},
  {re:/未来|futuris/i, tag:"风格/未来"},
  {re:/废墟|ruins|wasteland/i, tag:"场景/废墟"},
  {re:/教室|classroom|school/i, tag:"场景/校园"},
  {re:/泳池|pool|beach|swim/i, tag:"场景/泳池海边"},
  {re:/浴室|bathroom|shower|bath/i, tag:"场景/浴室"},
  {re:/厨房|kitchen/i, tag:"场景/厨房"},
  {re:/街道|street|alley|neon.?city|night city/i, tag:"场景/城市夜景"},
  {re:/卧室|bedroom/i, tag:"场景/卧室"},
  {re:/森林|forest|woods/i, tag:"场景/森林"},
  {re:/雪|snow/i, tag:"场景/雪景"},
  {re:/花田|flower|garden/i, tag:"场景/花园"},
  // 人物属性
  {re:/黑发|black hair|dark hair/i, tag:"人物/黑发"},
  {re:/白发|silver hair|white hair|grey hair/i, tag:"人物/白发"},
  {re:/金发|blonde|golden hair/i, tag:"人物/金发"},
  {re:/粉发|pink hair|sakura hair/i, tag:"人物/粉发"},
  {re:/蓝发|blue hair/i, tag:"人物/蓝发"},
  {re:/紫发|purple hair|violet hair/i, tag:"人物/紫发"},
  {re:/红瞳|red eye|赤瞳/i, tag:"人物/红瞳"},
  {re:/蓝瞳|blue eye|碧眼/i, tag:"人物/蓝瞳"},
  {re:/少女|1girl|shota|teen/i, tag:"人物/少女"},
  {re:/御姐|mature|office lady/i, tag:"人物/御姐"},
  {re:/猫耳|cat ear|nekomimi/i, tag:"人物/猫耳"},
  {re:/兔耳|bunny|usagi/i, tag:"人物/兔耳"},
  {re:/眼镜|glasses|meganekko/i, tag:"人物/眼镜"},
  {re:/双马尾|twin tail|twintails/i, tag:"人物/双马尾"},
  {re:/高马尾|ponytail/i, tag:"人物/高马尾"},
  {re:/麻花辫|braid/i, tag:"人物/辫子"},
  {re:/黑丝|black stocking|pantyhose|stocking/i, tag:"穿搭/黑丝"},
  {re:/白丝|white stocking/i, tag:"穿搭/白丝"},
  {re:/蕾丝|lace/i, tag:"穿搭/蕾丝"},
  {re:/长裙|dress|gown|skirt/i, tag:"穿搭/裙装"},
  {re:/西装|suit|business/i, tag:"穿搭/西装"},
  {re:/泳衣|swimsuit|bikini/i, tag:"穿搭/泳装"},
  {re:/女仆|maid/i, tag:"穿搭/女仆装"},
  {re:/制服|uniform|seifuku/i, tag:"穿搭/制服"},
  {re:/运动|sport|yoga|gym/i, tag:"穿搭/运动装"},
  // 视角/构图(横竖由 IHDR 判断,这里只词表)
  {re:/from above|俯视/i, tag:"视角/俯视"},
  {re:/from below|仰视/i, tag:"视角/仰视"},
  {re:/back view|背影/i, tag:"视角/背面"},
  {re:/side view|侧面/i, tag:"视角/侧面"},
  {re:/close[- ]up|特写/i, tag:"视角/特写"},
  {re:/full body|全身/i, tag:"视角/全身"},
  {re:/portrait|半身/i, tag:"视角/半身"},
  // 质量/用途(不自动贴,留给用户)
];
function suggestTagsFromMeta(it){
  const out=[];
  const m=S.curMeta && (S.curMeta.parsed||{});
  const pos = m && m.positive ? m.positive : "";
  if(!pos) return out;
  for(const lx of SUGGEST_LEXICON){
    if(lx.re.test(pos) && !out.includes(lx.tag)) out.push(lx.tag);
  }
  return out.slice(0,8);
}

/* --- 大图详情面板里加"AI 建议"行(在 renderTagPanel 渲染后调用) --- */
function renderTagSuggestions(it){
  const panel=$("#tagPanel"); if(!panel) return;
  const sug=$("#tagSugRow"); if(sug) sug.remove();
  const tags=(it.tags||[]).map(x=>x.toLowerCase());
  const cand=suggestTagsFromMeta(it).filter(t=>!tags.includes(t.toLowerCase()));
  if(!cand.length) return;
  const row=document.createElement("div"); row.id="tagSugRow";
  row.style.cssText="margin-top:7px;display:flex;flex-wrap:wrap;gap:4px;align-items:center";
  row.innerHTML='<span style="font-size:calc(10px*var(--fsx));color:var(--tx3);margin-right:2px;display:inline-flex;align-items:center;gap:calc(2px*var(--fsx))">'+ico("sparkle")+' 建议</span>';
  for(const t of cand){
    const s=document.createElement("span"); s.className="tagchip sug"; s.style.cssText="border-style:dashed;font-size:calc(10.5px*var(--fsx))";
    s.textContent=t;
    s.onclick=async()=>{ await tagAdd(it, t); renderTagSuggestions(it); };
    row.appendChild(s);
  }
  panel.appendChild(row);
}

