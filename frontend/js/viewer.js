/* ComfyMediaViewer · js/viewer.js（2026-09-09 从 index.html 拆出，加载顺序与原文一致） */
"use strict";
/* ---------------- lightbox ---------------- */

let lbTok=0;                                            // 竞态令牌：快速翻页时过期元数据不回填
/* V52（批次 G3）：大图相邻预取 —— 打开/翻页 600ms 后把左右邻居（仅图片）各预取一张。
   实测（uix_probe_20260927 M3）：缓存命中时翻页 50~78ms，但冷缓存 / 外接盘 / 数 MB PNG
   会放大到数百 ms；预取把这段挪到用户还在看当前张的时候。
   只预取图片：视频体积大，且 releaseStageVideo 已有完整释放纪律，不掺和。
   _pf 持有 Image 引用防 GC 掉请求；closeLb 清空（window.mvClearPrefetch）。 */
let _pf=[]; let _pfT=0;
function prefetchLb(){
  clearTimeout(_pfT);
  _pfT=setTimeout(()=>{
    _pf=[]; const v=curList(), i=curIdx();
    for(const d of [-1,1]){
      const n=i+d;
      if(n<0||n>=v.length) continue;
      if(!v[n] || v[n].type!=="image") continue;
      const im=new Image(); im.src=fileURL(v[n].path, v[n]); _pf.push(im);
    }
  }, 600);
}
window.mvPrefetchLb=prefetchLb;
function clearPrefetch(){ clearTimeout(_pfT); _pf=[]; }
window.mvClearPrefetch=clearPrefetch;
let lbFocusBack=null;   // G2·V16③：打开大图前的焦点，关闭时归还（翻页时不要覆盖它）

/* ε-2（2026-09-20）：**显式释放舞台里的视频**。
   为什么要有它：`stage.innerHTML=""` 只是把节点摘出 DOM。规范里 UA 确实会
   隐式暂停，但**已开始的资源获取不会立刻中止** —— 连按方向键快速翻过几十个视频时，
   后台仍在一路下載已翻走的 src，白白吃带宽与内存。
   （对比模式 exitWipe() 早就这么释放了，这里只是把同一条纪律补到大图。）

   顺序**必须是 pause → removeAttribute("src") → load()**：
   先断 src 会让有的浏览器立刻把 readyState 甩到 HAVE_NOTHING 并吐一个 error。
   ⚠️ `load()` 会让 UA 走一遍资源选择算法；没有 src 时它只是清理，不会发请求。
   挂到 window 上给 nav.js 的 closeLb() 复用 —— 关大图与切条目是同一份释放逻辑，
   不该各写各的（nav.js 在 viewer.js 之后加载，调用时它一定已就位）。 */
function releaseStageVideo(){
  const v = stage.querySelector("video");
  if(!v) return;
  CMVPlayback.release(v);
  try{ v.pause(); v.removeAttribute("src"); v.load(); }catch(e){}
  hideLbPlay();                       // M1：视频一走，中央播放按钮必须跟着收（否则切到图片还挂着）
}
window.mvReleaseStageVideo = releaseStageVideo;

/* B1（2026-10-08）→ B1b（2026-10-09）：视频「手动开始」—— 中央播放按钮的显隐/点击。
   为什么不做在 #stage 里：见 index.html #lbPlay 旁的说明（点 stage 空白会触发关大图）。
   唯一真相来源：按钮由 v 现推 —— 播放中**收起来**（别挡画面），暂停 / 首帧 / 播完显示 play。
   两轮报障的账都记在这一个按钮上，别再翻回去：
     · M1 把按钮做成「播放中整个收掉」，同时把 #vPlay / 空格也收窄成「只开始」
       ⇒ 三条路径全都没有暂停入口，用户报「暂停按钮失效」。
     · B1 把三条路径改成对称切换（正确，保留），却又把中央按钮改成**常驻**
       （`btn.hidden=false` 写死）⇒ 播放、暂停都挂着个大按钮挡画面，用户报「按钮不消失」。
   ⇒ 正解：切换入口归 #vPlay / 空格 / **点画面**（见 nav.js），中央按钮只在暂停态露面。
   事件用 addEventListener 而不是 `v.onplay=`：后者会**覆盖** extras.js 里 bindVidBar 设的
   同名 onplay/onpause（赋值式只有一个赢家）⇒ #vPlay 的图标从此不再翻转。
   closeLb 时仍由 releaseStageVideo 收掉（视频一走按钮必须跟着走）。 */
function hideLbPlay(){ const b=$("#lbPlay"); if(b) b.hidden=true; }

function lbPlayFor(v){
  const btn=$("#lbPlay");
  if(!btn) return;
  const sync=()=>{ btn.hidden=!v.paused; btn.innerHTML=ico(v.paused?"play":"pause"); };
  v.addEventListener("play",sync); v.addEventListener("pause",sync);
  v.addEventListener("ended",sync); sync();
  btn.onclick=()=>{ if(v.paused) v.play(); else v.pause(); };
}

/* L1（2026-09-27）：大图顶栏放不下时收进横向滚动（viewer.css 基础规则），
   滚动条已隐藏 ⇒ 纵向滚轮转成横向滚动（与 settings.js 里 #lbFilm 的同款交互）。 */
(function(){
  const bar=document.getElementById("lb-top");
  if(!bar) return;
  bar.addEventListener("wheel",e=>{
    if(Math.abs(e.deltaY)>Math.abs(e.deltaX)){ e.preventDefault(); bar.scrollLeft+=e.deltaY; }
  });
})();

async function openLb(i, solo){
  // solo=true：调用方（uploadSolo）已经填好 S.solo，这里只是别把它清掉。
  // 其余入口（网格 / 胶片条 / 翻页）一律回到库模式 —— 否则 S.solo 还在时
  // 会从「只有一张」的单图列表里取到下标相同的另一张，出现「点 A 打开 B」。
  if(!solo && S.solo){ S.solo=null; document.body.classList.remove("solo"); }
  const it=curList()[i];
  if(!it || (it.type!=="image" && it.type!=="video")) return;   // 文件夹占位卡不可打开
  const tok=++lbTok;
  if(window.CMVMediaDetail) window.CMVMediaDetail.cancel(meta);
  if(window.CMVLibrary) window.CMVLibrary.cancel(meta);
  if(S.solo) S.solo.idx=i; else S.idx=i;
  if(!lb.classList.contains("on")){                        // 只有「第一次打开」才记触发点：
    lbFocusBack=(document.activeElement && document.activeElement!==document.body)
      ? document.activeElement : null;                     // 翻页（step）会反复调进来
  }
  lb.classList.add("on");
  { const c=$("#btnClose"); if(c && !lb.contains(document.activeElement)) c.focus(); }
  /* E5-2（2026-09-23）：顶栏文件名被 ellipsis 截断后，用户此前**没有任何办法看全**
     —— 补 data-tip 作出口（与 D2 定下的「全站只有 data-tip 一个出口」一致）。
     无条件挂、不做「截断才挂」：后者要读 scrollWidth 量一次，且窗口宽度变化后提示会变陈旧
     （变宽后多余、变窄后缺失，缺的正好是需要的那个）。 */
  { const nm=$("#lbName"); nm.textContent=it.name; nm.dataset.tip=it.name; }
  setIcoLabel($("#btnFav"), it.fav?"starFill":"star", it.fav?"取消收藏":"收藏",
                it.fav?"filled":"");
  { const ms=$("#lbMark"); if(ms) ms.value=MARK_MAP[it.path]||"none"; }
  // 单图模式不记「最近使用」：临时副本会被暂存区清掉，记进去就是一条死链
  if(!S.solo) recordRecent(it.path, "preview");   // 打开大图 = 一次「最近使用」
  fillCatSelect($("#lbCat"), false);
  { const want=it.favcat||"默认";
    const opts=[...$("#lbCat").options];
    $("#lbCat").value=opts.some(o=>o.value===want)
      ? want : (opts.find(o=>o.value!=="__new__")||{value:"默认"}).value; }
  releaseStageVideo();                 // ε-2：切条目前先释放上一个视频（原只摘 DOM，下载仍在跑）
  stage.innerHTML="";
  $("#zoomBadge").style.display="none";
  S.vidInfo=null;
  if(it.type==="image"){
    /* ε-2（2026-09-20）：补 onerror。
       原写法只有 onload ⇒ 图加载失败（文件被移走/删掉、权限、损坏）时舞台**永久空白**，
       既没有提示也不报错，用户只能以为「卡住了」。
       对照：网格卡片（gallery.js）与回收站缩略图（extras.js）都有 onerror 兜底，
       唯独大图这条最该有反馈的路没有 —— 属于口径不一致。
       ⚠️ 处理器要在赋值 src **之前**挂上：src 是同步起手的，写在后面可能错过当次失败。 */
    const img=new Image();
    img.alt=it.name;                   // 读屏用户至少能拿到文件名
    img.onerror=()=>{
      if(!img.isConnected) return;     // 已经翻到别的条目了，别再往新舞台上写
      img.remove();
      stage.innerHTML='<div class="lb-fail">图片加载失败<div class="sub">「'+
        esc(it.name)+'」可能已被移动、删除，或格式已损坏</div></div>';
    };
    img.onload=()=>{ if(img.isConnected) bindZoom(img); };
    img.decoding="async";              // V45：解码让出主线程（大图翻页更顺）
    img.src=fileURL(it.path, it);
    stage.appendChild(img);
  }else if(it.playable){
    /* V46（批次 G3）：撤原生 controls —— 跟不了主题与 --fsx（与 V35 滑块同源问题）；
       进度 / 音量 / 时间由 #vidBar 自绘（extras.js bindVidBar），播放/倍速/循环/逐帧原本就有。
       M1（2026-10-08，用户令「视频不要自动播放，突然出声有点吓人」）：
        去掉了这里的**自动播放属性** —— 视频现在停在**首帧当海报**，等一次明确动作才播。
        ⚠️ 本注释**刻意不写出那个被删掉的属性名**：它含有 auto/play 字样，留在注释里会让
        「源码里不得出现自动播放属性」这类子串断言**假命中**（本项目「注释冒充代码」的老坑）。
        「要点一下」这次点击本身就是音量的明确大开，比静音自动播更彻底（后者一解除静音仍会突然出声，
        且 bindVidBar 每次把音量复位 ⇒ 翻页时屡次漏声）。 */
    const v=document.createElement("video"); v.playsInline=true;
    stage.appendChild(v); bindVidBar(v);
    v.addEventListener("cmv-playback-error",event=>{if(tok===lbTok) toast(event.detail,"err");});
    void CMVPlayback.attach(v,it);
    v.onloadedmetadata=()=>{                          // 视频元信息：时长/分辨率
      S.vidInfo=`时长：${fmtDur(v.duration)} ｜ 分辨率：${v.videoWidth} x ${v.videoHeight}`;
      const r=meta.querySelector(".vidinfo"); if(r) r.textContent=S.vidInfo;
    };
    lbPlayFor(v);
  }else{
    stage.innerHTML='<div style="color:var(--tx2)">该视频格式浏览器无法直接播放（'+esc(it.name.split(".").pop())+'）</div>';
  }
  nsfwApplyLb(it);                                  // 内容安全：模糊 + 提示条
  prefetchLb();                                     // V52：稳定后预取左右邻居
  meta.innerHTML='<div class="skel" style="height:14px;margin-bottom:8px"></div><div class="skel" style="height:12px;width:70%"></div>'; S.curMeta=null;
  try{
    const j=await api("/api/meta?p="+encodeURIComponent(it.path)+CMV3.urlTarget(it));
    if(tok!==lbTok || curIdx()!==i) return;          // 已切到别的条目，丢弃
    S.curMeta=j; renderMeta(j,it);
    // 单图模式的唯一目的就是看配方，落空时必须解释原因；库内不弹（见函数注释）
    if(S.solo && !hasGenMeta(j)) soloNoMetaToast(j);
  }catch(e){ if(tok!==lbTok) return;
    meta.innerHTML='<div class="row">元数据读取失败：'+esc(e.message)+"</div>"; }
}

/* ---------- 内容安全：大图（V2.11.0，V2.11.1 扩展，V2.11.2 删提示条）----------
   从网格点进大图是最容易意外曝光的一条路径 —— 所以这里**绝不自动揭开**，
   必须由用户再点一次。揭开只影响本次会话（见 state.js 里 NSFW_REVEAL 的注释）。

   大图上有两个安全相关的控件，状态**全部**由 nsfwApplyLb() 现算：
     #lbEye      舞台左上角的眼睛（和网格卡片的 .nv-eye 同一套语义：
                 盖着=红眼睛看原图，揭开=划线眼重新模糊）
     #lbNsfwCls  顶栏快速归类按钮（标签随当前判定翻转，双向）
   V2.11.2 删掉了横跨顶部的黄色提示条（#lbNsfw）：它的两个按钮（点击查看 /
   判错了）与眼睛、归类按钮完全重复，属于第三个入口，还占一整行高度。

   为什么不做「点舞台揭开」：大图上还有拖拽平移和滚轮缩放，拿 click 揭开的话
   「拖一下就露出来了」，那是把安全功能交给了手势误判 —— 揭开只走明确的按钮。

   同一个真相来源原则：控件的显隐/文案一律从 it + NSFW_MODE + NSFW_REVEAL
   现场推导，HTML 不预置任何状态。改判定（人工归类）后调 nsfwRefreshLb() 就能
   让控件 + 网格卡片 + 胶片条一起跟上，不存在某个控件忘了刷的死角。 */
function nsfwApplyLb(it){
  stage.classList.remove("nsfw-blur");
  nsfwSyncLbEye(it);
  nsfwSyncLbCls(it);
  if(nsfwShouldVeil(it)) stage.classList.add("nsfw-blur");
}
/* 大图正开着、且操作的就是当前这张 → 三个安全控件立刻重算。
   供 setNsfwManual（人工归类）在改完 it.n/it.ns 之后调用。 */
function nsfwRefreshLb(path){
  if(!lb.classList.contains("on")) return;
  const cur=curItem();
  if(!cur || cur.path!==path) return;
  nsfwApplyLb(cur);
}
/* 左上角眼睛：盖着/揭开两态**共用同一枚斜杠眼**（eyeOff）—— 和网格卡片完全同一套规则，
   唯一区别是这里没有中央遮挡层，模糊直接挂在 stage 上。
   δ-2c：按用户要求撤掉 δ-2b 临时加的「眼睛+×」（eyeHide），回到斜杠眼。 */
function nsfwSyncLbEye(it){
  const eye=$("#lbEye"); if(!eye) return;
  const show = isNsfw(it) && NSFW_MODE !== "show";
  eye.hidden = !show;
  if(!show) return;
  const on = NSFW_REVEAL.has(it.path);
  eye.classList.toggle("on", on);
  eye.innerHTML = ico("eyeOff");
  /* ⚠️ 只写 data-tip，不写 title —— D2 起全站提示只有 data-tip 一个出口，
     写 title 会与自绘气泡双显（v4_batchD2_check / v31_tip_step1_check 直接红）。 */
  eye.setAttribute("data-tip",
    on ? "重新模糊这张（只影响当前会话）" : "单独查看这张原图");
}
/* 顶栏快速归类按钮：当前是 NSFW → 「移出」；否则 → 「标为」。
   判定口径用 nsfwLabelOf()（与遮罩/菜单一致），避免界面和按钮各说各话。
   单图模式不显示：临时副本会被暂存区清掉，给它写人工纠正没有意义。 */
function nsfwSyncLbCls(it){
  const b=$("#lbNsfwCls"); if(!b) return;
  const media = it && (it.type==="image" || it.type==="video");
  /* 判据 nsfwReady() 而不是 nsfwOn()：没跑过检测时每张都是 unknown，标 NSFW/移出
     都不会有任何视觉变化（除非点「移出」的人想让这张躲过将来的检测）。
     引擎都没装就出现这个按钮更没道理 —— 它写的是人工纠正表，那是有检测结果之后的事。 */
  const show = nsfwReady() && !S.solo && media;
  b.style.display = show ? "" : "none";
  if(!show) return;
  const toNsfw = nsfwLabelOf(it) !== "nsfw";
  b.innerHTML = '<span class="ic">'+ico(toNsfw?"eyeOff":"eye")+'</span>'
    + (toNsfw ? "标为 NSFW" : "移出 NSFW");
  b.setAttribute("data-tip", toNsfw
    ? "把这张标为 NSFW（写入手动纠正表，重新检测也不会翻回去）"
    : "把这张移出 NSFW（写入手动纠正表，重新检测也不会翻回去）");
}
function nsfwToggleLbReveal(){
  const it=curItem(); if(!it) return;
  if(NSFW_REVEAL.has(it.path)) NSFW_REVEAL.delete(it.path);
  else NSFW_REVEAL.add(it.path);
  nsfwApplyLb(it);
  nsfwSyncGridCard(it.path);
}
/* 揭开后顺手把网格里那张卡也点亮（含左上角眼睛的图标态）：
   同一张图不该「大图看了、回网格又糊回去」。绘制逻辑统一在 nsfwPaintCard（filters.js）。 */
function nsfwSyncGridCard(path){
  if(typeof nsfwPaintCard === "function") nsfwPaintCard(path);
}
/* V2.11.2：随提示条一起删掉的还有 nsfwRevealLb（#lnShow 专用）与
   nsfwMarkSfw（#lnFix 专用）—— 职责已由眼睛的 nsfwToggleLbReveal 与
   顶栏按钮的 setNsfwManual 直调接管，不留死代码。 */
(function wireNsfwLb(){
  const eye=$("#lbEye"), cls=$("#lbNsfwCls");
  if(eye) eye.onclick=nsfwToggleLbReveal;
  if(cls) cls.onclick=()=>{ const it=curItem(); if(!it) return;
    setNsfwManual(it, nsfwLabelOf(it)==="nsfw" ? "sfw" : "nsfw"); };
})();

function esc(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;")}

/* 「有没有可看的东西」的唯一判定。renderMeta 的空态文案与单图模式的提示都用它，
   一处定义两处使用，免得出现「面板说没有、提示不说」这类漂移。

   ⚠️ 判定口径是「有没有**生成信息**」，不是「meta 对象空不空」：
   后端对**任何**图片都会塞进 IHDR（尺寸），所以 `Object.keys(meta).length` 恒 ≥1，
   拿它当条件的话这个分支永远不成立 —— 旧代码正是这么写的，于是那句空态文案
   在真实图片上从未显示过，用户点开一张没有配方的图只看到「修改时间｜大小｜尺寸」，
   没有任何解释。（IHDR / EXIF 这类技术字段也不该算「配方」。） */
function hasGenMeta(j){
  if(!j) return false;
  if(j.parsed) return true;                       // 已解析出模型 / 步数 / 提示词
  const m=j.meta||{};
  return !!(m.prompt || m.workflow || m.parameters);   // 原始块也算：面板里能看到
}

/* V8 · 长文本折叠：阈值 6 行。行数按「换行 + 长行折行估算」算，不写死 px。 */
const META_FOLD_LINES = 6;
function metaTextLines(t){
  if(!t) return 0;
  let n=0;
  for(const ln of String(t).split("\n")) n += Math.max(1, Math.ceil(ln.length/72));
  return n;
}
/* V8 · 展开/收起状态全局记忆：同一个面板里切图不跳回折叠态。 */
let META_FOLD = false;

/* V8 · 可折叠长文本块（提示词）。折叠高度由 --lh-loose 算出（见 viewer.css）。
   注意：仍输出 <pre class="ptxt"> 原样 —— 既有门禁按 'class="ptxt"' 精确串断言，
   且第一个 .ptxt 必须是正向提示词正文（copy_prompt_check「所见即所复制」）。 */
function metaFold(title, txt){
  if(!txt) return "";
  const long = metaTextLines(txt) > META_FOLD_LINES;
  const open = META_FOLD && long;
  return '<div class="mfold'+(long?(open?" open":""):" short")+'">'
    +'<div class="mfhd"><span>'+esc(title)+'</span>'
      +(long?'<button type="button" class="mfbtn" data-fold="1">'+(open?"收起":"展开")+'</button>':"")
    +'</div>'
    +'<pre class="ptxt">'+esc(txt)+'</pre>'
    +'<div class="mfact"><a href="javascript:void(0)" class="cp" data-cp="'+esc(txt)+'">复制'+esc(title)+'</a></div>'
    +'</div>';
}

/* V47（2026-10-08）：LoRA 结构化区块 —— 一条一行 + 权重右锚徽章。
   输入是后端拍平的 list[str]（"名字 (权重)"），这里回拆成 {n:名字, s:权重文本}。
   ⚠️ 名字用 title 属性保留全称：面板窄（内容 ~300px），长 LoRA 名必然省略，
      省略后 hover 能看全 —— 否则用户只知道「有这么个 LoRA」却读不出是哪个。
   ⚠️ 复制走 data-cp（沿用 .cpv 的既有复制机制）：点名字复制**完整原文**（含权重），
      避免「省略号看起来像名字的一部分」。 */
function metaLoras(list){
  const items = list.map(x=>{
    const s0 = String(x||"").trim();
    const m = /^(.*?)\s*\(([\d.]+)\)$/.exec(s0);
    return m ? { n: m[1], s: m[2] } : { n: s0, s: "" };
  }).filter(o=>o.n);
  if(!items.length) return "";
  return '<div class="lorabox">'
    + '<div class="lh"><span>LoRA</span><span class="cnt">'+items.length+'</span></div>'
    + items.map(o=>
        '<div class="lora-row">'
          + '<span class="lora-nm cpv" data-tip="点击复制" data-cp="'+esc(o.n+(o.s?" ("+o.s+")":""))+'">'+esc(o.n)+'</span>'
          + (o.s ? '<span class="lora-st">'+esc(o.s)+'</span>' : "")
        + '</div>').join("")
    + '</div>';
}

/* V8 · 三层卡片：① 总览（定义列表左右对齐）② 生成参数（+ 长提示词折叠）③ 原始数据（<details>）。 */
function formatRawJSON(text){
  if(typeof text!=="string" || text.length>2*1024*1024)return text;
  try {JSON.parse(text);} catch(_) {return text;}
  // Validate JSON, then format its original tokens: 64-bit seeds stay exact.
  const out=[];let depth=0,quoted=false,escaped=false,previous="";
  const newline=()=>out.push("\n"+"  ".repeat(depth));
  for(let i=0;i<text.length;i++){
    const ch=text[i];
    if(quoted){out.push(ch);if(escaped)escaped=false;else if(ch==="\\")escaped=true;else if(ch==='"')quoted=false;continue;}
    if(/\s/.test(ch))continue;
    if(ch==='"'){quoted=true;out.push(ch);}
    else if(ch==="{"||ch==="["){
      out.push(ch);depth++;if(depth>64)return text;if(!/^[\s]*[}\]]/.test(text.slice(i+1)))newline();
    }else if(ch==="}"||ch==="]"){
      depth--;if(previous!=="{"&&previous!=="[")newline();out.push(ch);
    }else if(ch===","){out.push(ch);newline();}
    else if(ch===":")out.push(": ");else out.push(ch);
    previous=ch;
  }
  return out.join("");
}

function renderMeta(j,it){
  const m=j.meta||{}, P=j.parsed; let h="";
  /* ① 总览 —— 原来是一行「A ｜ B ｜ C」流水，现在拆成两列对齐 */
  const info=[["修改时间",fmtTime(it.mtime),""],["大小",fmtSize(it.size),""]];
  if(it.type==="image"&&m.IHDR) info.push(["尺寸",m.IHDR,""]);
  if(it.type==="video") info.push(["视频",S.vidInfo||"读取视频信息…","vidinfo"]);
  h+='<section class="mcard"><div class="k mh">总览</div><dl class="mdl">'
    +info.map(r=>'<div class="mrow"><dt>'+esc(r[0])+'</dt><dd'+(r[2]?' class="'+r[2]+'"':"")
      +'>'+esc(r[1])+'</dd></div>').join("")
    +'</dl></section>';
  /* ② 生成参数 */
  if(P){
    const rows=[];
    if(P.model) rows.push(["模型",P.model+(P.model_hash?` (${P.model_hash})`:"")]);
    if(P.size) rows.push(["出图尺寸",P.size]);
    if(P.sampler) rows.push(["采样器",P.sampler+(P.scheduler?` / ${P.scheduler}`:"")]);
    if(P.steps!=null) rows.push(["步数",P.steps]);
    if(P.cfg!=null) rows.push(["CFG",P.cfg]);
    if(P.seed!=null) rows.push(["Seed",P.seed]);
    if(P.wfk) rows.push(["工作流",({t2i:"文生图",i2i:"图生图",cn:"ControlNet"}[P.wfk]||P.wfk)]);
    /* V47（2026-10-08）：LoRA **不再**拼成单行塞进字段表。
       原来把 loras 用逗号拼成一个字符串值 —— 实测 4 个 LoRA 拼出 179 字符，
       而面板内容宽仅 ~300px（#side 340px 减内边距），换行成一坨且权重淹没在文本里；
       ⚠️ 本注释**刻意不写出那个被删掉的拼接表达式**：它含 join/comma 字样，
          留在注释里会让「源码里不得再拼单行」这类子串断言**假命中**
          （本项目「注释冒充代码」的老坑，M1 门禁已因此误报过一轮）。
       更根本的是它走 .mdl 网格（左 70px 字段名），LoRA 被当**单个值**渲染，天然分行不了。
       改法：升格为独立区块（metaLoras），一条一行 + 权重右锚徽章（见 viewer.css .lorabox 组）。
       ⚠️ 数据形态：parsed.loras 是 list[str]，元素形如 "名字.safetensors (0.8)" ——
          权重是**尾部括号里的文本**，不是结构化字段（server.py 的 _strength_fmt 已拍平）。
          所以这里必须正则回拆，而不是改后端（后端格式被索引/搜索/导出多处依赖，不动）。
       ⚠️ 正则用**非贪婪 + 锚定结尾**：LoRA 名字本身可能含括号（如 "foo (v2).safetensors (0.8)"），
          贪婪会把名字里的括号吃掉；`^(.*?)\s*\(([\d.]+)\)$` 只认**最后一组纯数字括号**，
          且 `([\d.]+)` 限定数字/点，名字里的 "(v2)" 不会误判成权重。 */
    const loraBox = (P.loras&&P.loras.length) ? metaLoras(P.loras) : "";
    let body = rows.length
      ? '<dl class="mdl">'+rows.map(r=>'<div class="mrow"><dt>'+esc(r[0])+'</dt><dd>'
          +'<span class="cpv" data-tip="点击复制" data-cp="'+esc(r[1])+'">'+esc(r[1])+'</span></dd></div>').join("")+'</dl>'
      : "";
    body += metaFold("正向提示词",P.positive) + metaFold("负向提示词",P.negative) + loraBox;
    if(body) h+='<section class="mcard"><div class="k mh">生成参数'
      +(P.source?' · '+esc(P.source):"")+'</div>'+body+'</section>';
  }
  /* ③ 原始数据 */
  let raw="";
  const rawKeys=["prompt","workflow"];
  for(const k of rawKeys){
    if(m[k]==null) continue;
    raw+='<details data-raw-key="'+k+'"><summary>'+k+'（原始 JSON）</summary><pre></pre></details>';
  }
  if(m.parameters) raw+='<details><summary>parameters（原始文本）</summary><pre>'+esc(m.parameters)+'</pre></details>';
  for(const k in m){ if(rawKeys.includes(k)||k==="IHDR"||k==="parameters") continue;
    raw+='<div class="k">'+esc(k)+'</div><pre>'+esc(String(m[k]).slice(0,2000))+'</pre>'; }
  if(raw) h+='<section class="mcard"><div class="k mh">原始数据</div>'+raw+'</section>';
  /* 用 += 而非 =：总览是任何文件都该看到的，不能被一句空态文案冲掉。
     E5-7（2026-09-23）：**「读失败」与「本来就没有」必须分开说** —— 此前两种情形都打
     「没有内嵌生成信息」，把「我们读不到」讲成了「这张图本来就没有配方」（与事实相反）。
     服务端在 png_meta 读失败时登记旁路状态，API 回带 metaError；这里据此换一句话。 */
  if(!hasGenMeta(j)) h+= (j && j.metaError)
    ? '<div class="row err">元数据读取失败：'+esc(j.metaError)
      +'（文件可能正被其它程序占用，稍后可重试）</div>'
    : '<div class="row">这张图没有内嵌生成信息（提示词 / 模型 / 参数）</div>';
  meta.innerHTML=h;
  meta.querySelectorAll("details[data-raw-key]").forEach(details=>{
    details.addEventListener("toggle",()=>{
      if(!details.open || details.dataset.rendered) return;
      let text=m[details.dataset.rawKey];
      text=formatRawJSON(text);
      details.querySelector("pre").textContent=String(text); details.dataset.rendered="1";
    });
  });
  if(window.CMVMediaDetail){
    const detailToken=lbTok, detailPath=it.path;
    window.CMVMediaDetail.mount(meta,j,()=>lb.classList.contains("on") && lbTok===detailToken
      && curItem() && curItem().path===detailPath);
  }
  if(window.CMVLibrary){
    const token=lbTok,path=it.path;
    window.CMVLibrary.mount(meta,meta,j,()=>lb.classList.contains('on')&&lbTok===token&&curItem()?.path===path);
  }
  /* 折叠按钮：状态写回全局记忆（切图沿用） */
  meta.querySelectorAll(".mfbtn").forEach(b=>b.onclick=()=>{
    const box=b.closest(".mfold"); if(!box) return;
    const on=box.classList.toggle("open"); META_FOLD=on;
    b.textContent=on?"收起":"展开";
  });
  $("#btnOpen").style.display=m.prompt?"":"none";
  $("#btnDlWF").style.display=m.prompt?"":"none";
  // 「复制 prompt」复制的是解析后的正向提示词 → 没有它就别显示（避免给出会报错的死按钮）。
  const _pos=((P&&P.positive)||"").trim();
  $("#btnCopy").dataset.cp=_pos;
  $("#btnCopy").style.display=_pos?"":"none";
  setIcoLabel($("#btnCopy"), "copy", "复制 prompt");   /* 批次 B：图标槽从 ICO 取，取代旧的字符勾 */
  /* V14：两个复制入口都走 copyText，成功后由它给触发元素一次成功色微反馈 */
  meta.querySelectorAll("a.cp").forEach(a=>a.onclick=()=>copyText(a.dataset.cp, a));
  meta.querySelectorAll(".cpv").forEach(el=>el.onclick=()=>copyText(el.dataset.cp, el));  // 参数逐项点击复制
}

function catMap(){ const m={}; for(const c of S.favCats) m[c.name]=1; return m; }

/* ---------------- 缩放：滚轮缩放 / 拖拽平移 / 双击 100% ↔ 适应 ---------------- */
/* 所有可缩放元素共享一套 window 级拖拽调度：
   原先每个 bind 各自覆写 window.onmousemove/onmouseup，大图页与对比页互相覆盖，
   且元素销毁后处理器残留。改为注册表 + 自动清理断连元素。 */
const _zoomDrags=new Map();   // img -> {move(e), up()}
window.addEventListener("mousemove",e=>{
  for(const [img,d] of [..._zoomDrags]){
    if(!img.isConnected){ _zoomDrags.delete(img); continue; }
    d.move(e);
  }
});
window.addEventListener("mouseup",()=>{
  for(const [img,d] of [..._zoomDrags]){
    if(!img.isConnected){ _zoomDrags.delete(img); continue; }
    d.up();
  }
});
const zoom={s:1,tx:0,ty:0};
let zoomFit=4;
function bindZoom(img){
  zoom.s=1; zoom.tx=0; zoom.ty=0; zoomFit=4;
  const fitW=img.getBoundingClientRect().width||1;
  if(img.naturalWidth) zoomFit=Math.max(1,img.naturalWidth/fitW);   // 100% 像素所需的缩放比
  img.classList.remove("grabbing");
  const apply=()=>{
    img.style.transform=`translate(${zoom.tx}px,${zoom.ty}px) scale(${zoom.s})`;
    const b=$("#zoomBadge");
    if(b){ if(zoom.s>1.001){ b.textContent=Math.round(100*zoom.s/zoomFit)+"%"; b.style.display="block"; }
           else b.style.display="none"; }
  };
  img.onwheel=e=>{
    e.preventDefault();
    const r=img.getBoundingClientRect();
    const dx=e.clientX-(r.left+r.width/2), dy=e.clientY-(r.top+r.height/2);
    const ns=Math.min(Math.max(8,zoomFit), Math.max(1, zoom.s*(e.deltaY<0?1.2:1/1.2)));
    zoom.tx=dx*(1-ns/zoom.s)+zoom.tx*ns/zoom.s;
    zoom.ty=dy*(1-ns/zoom.s)+zoom.ty*ns/zoom.s;
    zoom.s=ns; apply();
  };
  let drag=null;
  img.onmousedown=e=>{ if(zoom.s>1.001){ drag={x:e.clientX,y:e.clientY,tx:zoom.tx,ty:zoom.ty};
    img.classList.add("grabbing"); e.preventDefault(); } };
  _zoomDrags.set(img, {
    move:e=>{ if(!drag) return;
      zoom.tx=drag.tx+(e.clientX-drag.x); zoom.ty=drag.ty+(e.clientY-drag.y);
      img.style.transform=`translate(${zoom.tx}px,${zoom.ty}px) scale(${zoom.s})`; },
    up:()=>{ if(drag){ drag=null; img.classList.remove("grabbing"); } }
  });
  img.ondblclick=()=>{
    if(zoom.s>1.05){ zoom.s=1; zoom.tx=0; zoom.ty=0; }
    else{ zoom.s=zoomFit; zoom.tx=0; zoom.ty=0; }
    apply();
  };
}

/* ---------------- 并排对比（批量模式下 2-4 张）---------------- */
function bindCmpZoom(img){
  const st={s:1,tx:0,ty:0};
  const fitW=img.getBoundingClientRect().width||1;
  const fit=img.naturalWidth? Math.max(1,img.naturalWidth/fitW):4;
  const apply=()=>{ img.style.transform=`translate(${st.tx}px,${st.ty}px) scale(${st.s})`; };
  img.onwheel=e=>{ e.preventDefault();
    const r=img.getBoundingClientRect();
    const dx=e.clientX-(r.left+r.width/2), dy=e.clientY-(r.top+r.height/2);
    const ns=Math.min(Math.max(8,fit), Math.max(1, st.s*(e.deltaY<0?1.2:1/1.2)));
    st.tx=dx*(1-ns/st.s)+st.tx*ns/st.s; st.ty=dy*(1-ns/st.s)+st.ty*ns/st.s; st.s=ns; apply(); };
  let drag=null;
  img.onmousedown=e=>{ if(st.s>1.001){ drag={x:e.clientX,y:e.clientY,tx:st.tx,ty:st.ty};
    img.classList.add("grabbing"); e.preventDefault(); } };
  _zoomDrags.set(img, {
    move:e=>{ if(!drag) return;
      st.tx=drag.tx+(e.clientX-drag.x); st.ty=drag.ty+(e.clientY-drag.y); apply(); },
    up:()=>{ if(drag){ drag=null; img.classList.remove("grabbing"); } }
  });
  img.ondblclick=()=>{ st.s=st.s>1.05?1:fit; st.tx=0; st.ty=0; apply(); };
}
function openCmp(){
  const paths=[...batchSel];
  if(paths.length<2||paths.length>4){ toast("并排对比需选中 2-4 张（当前 "+paths.length+" 张）","err"); return; }
  exitWipe();                                   // 若残留滑块模式，先复位回并排
  const g=$("#cmpGrid"); g.innerHTML="";
  $("#cmpN").textContent=`对比 ${paths.length} 张 · 每格：滚轮缩放 / 拖拽 / 双击 100%↔适应`;
  g.style.gridTemplateColumns = paths.length===3 ? "repeat(3,1fr)" : "repeat(2,1fr)";
  for(const p of paths){
    const cell=document.createElement("div"); cell.className="cp";
    const im=new Image(); im.src=fileURL(p);   // 带版本号：刚被替换过的图不拿旧缓存
    im.onload=()=>bindCmpZoom(im);
    const cap=document.createElement("div"); cap.className="cpc";
    cap.textContent=p.split(/[\\/]/).pop();
    cell.append(im,cap); g.appendChild(cell);
  }
  $("#cmpWipe").style.display = paths.length===2 ? "" : "none";   // 恰好 2 张才提供滑块模式
  if(!$("#cmp").classList.contains("on")){
    cmpFocusBack=(document.activeElement && document.activeElement!==document.body)
      ? document.activeElement : null;
  }
  $("#cmp").classList.add("on");
  { const x=$("#cmpExit"); if(x && !document.getElementById("cmp").contains(document.activeElement)) x.focus(); }
}
let cmpFocusBack=null;   // G2·V16③：对比层的触发点
function closeCmp(){ exitWipe(); $("#cmp").classList.remove("on"); $("#cmpGrid").innerHTML="";
  if(window.mvSyncInert) window.mvSyncInert();             // 先解冻再归还（同 #lb）
  if(cmpFocusBack && cmpFocusBack.isConnected && cmpFocusBack.focus) cmpFocusBack.focus();
  cmpFocusBack=null; }

/* ---------------- 滑块擦除对比（2 张图重叠，拖分隔线看细节差异） ---------------- */
const wipe={a:null,b:null,x:50,z:{s:1,tx:0,ty:0,fit:4},hDrag:false};
function applyWipe(){
  const t=`translate(${wipe.z.tx}px,${wipe.z.ty}px) scale(${wipe.z.s})`;
  $("#wipeA").style.transform=t;
  $("#wipeB").style.transform=t;
  /* 裁切写在**不参与 transform** 的 #wipeBClip 上，不写在被 scale 的 #wipeB 上：
     clip-path 按元素本地坐标裁、裁完再整体过 transform ⇒ 挂在图片上时那条裁切边会随
     缩放一起平移放大，和按 stage 百分比定死的 #wipeHandle 错位（主人 2026-10-09 报的
     「放大后滑块和画面里的分割线对不上」）。下移到裁切层后两者共用 stage 宽这个基宽，恒重合。 */
  $("#wipeBClip").style.clipPath=`inset(0 0 0 ${wipe.x}%)`;   // B 只露出分隔线右侧
  $("#wipeHandle").style.left=wipe.x+"%";
}
function wipeFit(){
  const A=$("#wipeA"), w=$("#wipeStage").clientWidth||1;
  if(A.naturalWidth) wipe.z.fit=Math.max(1,A.naturalWidth/w);
}
function openWipe(pa, pb){
  const A=$("#wipeA"), B=$("#wipeB");
  $("#cmpGrid").style.display="none";
  $("#wipeWrap").classList.add("on");
  setIcoLabel($("#cmpWipe"), "gridTable", "并排模式");
  wipe.a=pa; wipe.b=pb; wipe.x=50; wipe.z={s:1,tx:0,ty:0,fit:4};
  A.classList.add("draggable"); B.classList.add("draggable");
  A.src=fileURL(pa);
  B.src=fileURL(pb);
  A.onload=B.onload=()=>{ wipeFit(); if(wipe.z.s<=1.001) applyWipe(); };
  $("#wipeLblA").textContent=pa.split(/[\\/]/).pop();
  $("#wipeLblB").textContent=pb.split(/[\\/]/).pop();
  $("#cmpN").textContent=`滑块对比：左 = ${$("#wipeLblA").textContent} ｜ 右 = ${$("#wipeLblB").textContent}`;
  applyWipe();
}
function exitWipe(){
  if(!wipe.a) return;
  wipe.a=wipe.b=null; wipe.hDrag=false;
  const A=$("#wipeA"), B=$("#wipeB");
  A.removeAttribute("src"); B.removeAttribute("src");   // 释放原图请求/内存
  A.style.transform=""; B.style.transform="";
  $("#wipeBClip").style.clipPath="";                    // 裁切层也要复位，否则残留一条裁切窗口
  A.classList.remove("draggable","grabbing"); B.classList.remove("draggable","grabbing");
  $("#cmpGrid").style.display="";
  $("#wipeWrap").classList.remove("on");
  setIcoLabel($("#cmpWipe"), "swap", "滑块模式");
}
$("#cmpWipe").onclick=()=>{
  if(wipe.a){                                   // 滑块 → 并排
    exitWipe();
    $("#cmpN").textContent=`对比 ${[...batchSel].length} 张 · 每格：滚轮缩放 / 拖拽 / 双击 100%↔适应`;
    return;
  }
  const paths=[...batchSel];
  if(paths.length!==2){ toast("滑块对比需要正好选中 2 张","err"); return; }
  openWipe(paths[0], paths[1]);
};

const wStage=$("#wipeStage");
$("#wipeHandle").onmousedown=e=>{ wipe.hDrag=true; e.preventDefault(); e.stopPropagation(); };
window.addEventListener("mousemove",e=>{          // 拖分隔线（handle 之外继续跟随）
  if(!wipe.hDrag) return;
  const r=wStage.getBoundingClientRect();
  wipe.x=Math.max(0, Math.min(100, (e.clientX-r.left)/r.width*100));
  applyWipe();
});
window.addEventListener("mouseup",()=>wipe.hDrag=false);
wStage.onwheel=e=>{
  if(!wipe.a) return;
  e.preventDefault();
  const z=wipe.z, r=wStage.getBoundingClientRect();
  const dx=e.clientX-(r.left+r.width/2), dy=e.clientY-(r.top+r.height/2);
  const ns=Math.min(Math.max(8,z.fit), Math.max(1, z.s*(e.deltaY<0?1.2:1/1.2)));
  z.tx=dx*(1-ns/z.s)+z.tx*ns/z.s; z.ty=dy*(1-ns/z.s)+z.ty*ns/z.s; z.s=ns;
  applyWipe();
};
let wDrag=null;                                    // 放大后拖拽平移（两图同步）
for(const im of [$("#wipeA"), $("#wipeB")]){
  im.onmousedown=e=>{
    if(!wipe.a || wipe.z.s<=1.001) return;
    wDrag={x:e.clientX,y:e.clientY,tx:wipe.z.tx,ty:wipe.z.ty};
    $("#wipeA").classList.add("grabbing"); $("#wipeB").classList.add("grabbing");
    e.preventDefault();
  };
}
_zoomDrags.set(wStage,{                           // 复用共享拖拽调度，断连自动清理
  move:e=>{ if(!wDrag) return;
    wipe.z.tx=wDrag.tx+(e.clientX-wDrag.x); wipe.z.ty=wDrag.ty+(e.clientY-wDrag.y);
    applyWipe(); },
  up:()=>{ if(!wDrag) return; wDrag=null;
    $("#wipeA").classList.remove("grabbing"); $("#wipeB").classList.remove("grabbing"); }
});
wStage.ondblclick=()=>{
  if(!wipe.a) return;
  if(wipe.z.s>1.05) wipe.z={s:1,tx:0,ty:0,fit:wipe.z.fit};
  else{ wipeFit(); wipe.z={s:wipe.z.fit,tx:0,ty:0,fit:wipe.z.fit}; }
  applyWipe();
};
document.addEventListener("keydown",e=>{           // ←/→ 微调分隔线（Shift 大步进）
  if(!wipe.a || !$("#cmp").classList.contains("on")) return;
  const tag=e.target.tagName;
  if(tag==="INPUT"||tag==="SELECT"||tag==="TEXTAREA") return;
  if(e.key==="ArrowLeft"||e.key==="ArrowRight"){
    e.preventDefault();
    wipe.x=Math.max(0, Math.min(100, wipe.x+(e.key==="ArrowRight"?1:-1)*(e.shiftKey?10:2)));
    applyWipe();
  }
});


/* ---------------- 单图查看：打开库外图片（V2.10.7） ----------------
   用途：别人发来一张原图，想直接看它的提示词 / 配方，不必先把图拷进 output 目录。
   流程：选文件 -> POST /api/solo（后端按内容哈希落进数据目录的 solo/）-> 拿到绝对
   路径 -> 用「只有这一张」的 S.solo 打开大图面板，其余一切照旧。

   为什么不直接用 URL.createObjectURL(file) 把本地文件喂给 <img>：
   提示词解析、下载、导出工作流、送 ComfyUI 全都按**路径**走后端，blob: URL 在后端
   根本不存在，那一整条能力得重写一遍；而且刷新一次 blob 就失效。落盘换来的代价只是
   数据目录里多一个可清理的暂存区（设置 · 数据页有「清除单图暂存」和占用显示）。
   ★ 转发的图读不到提示词：微信 / QQ 不勾「原图」会重新编码，PNG 的 tEXt 块就没了 ——
   这时面板照常打开，落成一句话说明 + 一条解释原因的 toast（soloNoMetaToast）。 */
const SOLO_MAX = 64 * 1024 * 1024;      // 与服务端 SOLO_MAX_BYTES 保持一致
function openSoloPicker(){
  const inp=$("#soloFile"); if(inp) inp.click();
}
async function uploadSolo(file){
  if(!file) return;
  if(file.size > SOLO_MAX){ toast("图片超过 64 MB 上限","err"); return; }
  setBotState("busy","正在打开图片…",null,false);
  try{
    const r=await fetch("/api/solo",{method:"POST",
      headers:{"Content-Type":file.type||"application/octet-stream"},body:file});
    const j=await r.json().catch(()=>({}));
    if(!r.ok || !j.ok) throw new Error(j.error||r.statusText);
    S.solo={ items:[{ type:"image", path:j.path, name:file.name||j.name,
                      size:file.size, mtime:Math.floor(Date.now()/1000) }], idx:0 };
    document.body.classList.add("solo");
    setBotState("idle","",null,false);
    await openLb(0, true);
    // 没有配方时不弹「已打开」：那会和 openLb 里弹出的解释互相打架，
    // 而此刻真正需要被看到的是「为什么没有」。
    if(hasGenMeta(S.curMeta)) toast("已打开："+(file.name||j.name),"ok");
  }catch(e){
    setBotState("err","打开图片失败："+e.message,null,false);
    toast("打开图片失败："+e.message,"err");
  }
}
/* 单图模式：一个字段都没解析出来时，必须解释「为什么」——
   这是核心诉求落空（打开就是为了看配方），不是普通的空面板。
   库内**故意不弹**：自己下载的壁纸 / 截图本来就没有生成信息，
   每点开一张都弹一次会变成噪音；那时面板里的空态文案已经说清楚了。 */
function soloNoMetaToast(j){
  /* E5-7（2026-09-23）：单图模式唯一的目的是看配方，落空时必须**说清是哪种落空**。
     读失败（文件被占用 / 权限 / 盘离线）与「本来就没有」是两回事，给的下一步也不同：
     前者重试就好，后者要回去找原图。此前一律按后者解释，等于把人往错方向指。 */
  if(j && j.metaError){
    toastAction("元数据读取失败："+j.metaError, "重试",
      ()=>openLb(curIdx(), !!S.solo), "err", 12000,
      "这次是**读不到**，不是这张图没有配方：\n"
      + "· 常见于文件正被其它程序占用（编辑器 / 云盘同步中 / 杀软扫描），稍后重试即可。\n"
      + "· 也可能是权限不足，或所在盘符/网络位置已离线。\n"
      + "\n点「重试」会立刻重新读一次。");
    return;
  }
  toastAction("这张图没有内嵌提示词与生成参数", "重新选图", openSoloPicker, "warn", 12000,
    "常见原因：\n"
    + "· 转发时没勾「原图」：微信 / QQ 会重新编码图片，PNG 的 tEXt 块、JPEG 的 APP1 段\n"
    + "  被整段丢掉 —— 图还能看，配方却没了。\n"
    + "· 图被截图、二次编辑或过了在线压缩工具，保存时元数据被剥掉。\n"
    + "· 这张图本来就不是 ComfyUI / WebUI 生成的，没有配方可读。\n"
    + "\n"
    + "怎么办：让对方以「文件」方式发原图（微信勾「原图」再发送，或直接发压缩包），\n"
    + "重新打开一次即可。");
}

{ const b=$("#btnOpenImg"); if(b) b.onclick=openSoloPicker;
  const f=$("#soloFile");
  if(f) f.onchange=()=>{ const fl=(f.files&&f.files[0])||null;
    f.value="";                       // 先清空：同一个文件连选两次也要能触发 change
    uploadSolo(fl); };
}
