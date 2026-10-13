/* ComfyMediaViewer · js/favorites.js（2026-09-09 从 index.html 拆出，加载顺序与原文一致） */
"use strict";
/* ---------------- 收藏分类 ---------------- */
async function refreshFavCats(){
  /* F3 P2-2：原来 `.catch(()=>({cats:[]}))` 把失败**伪装成空结果** ——
     S.favCats 被写成 []，两个下拉只剩「全部分类 / 新建分类…」，没有任何提示，
     用户会以为自己的分类丢了，或者顺手新建一个同名的空分类（而数据还在服务端）。
     改为：失败时**保留上一次的分类**（不动内存态）并明确报错。 */
  try{
    const j = await api("/api/favs");
    S.favCats = j.cats || [];
  }catch(e){
    toastErr("收藏分类读取失败",
      "列表可能不是最新的（已保留上一次读取到的分类）：" + String((e&&e.message)||e));
  }
  /* H2（2026-10-07）：原先还要 `fillCatSelect($("#favcat"), true)` 维护一个隐藏下拉的选项，
     好让 `.value` 能承载「当前收藏分类」。该幽灵控件已删除，分类状态改由
     `S.filters.favcat` 承载 ⇒ 这行不再需要（左栏 `#favNav` 由 buildFavNav 自建）。
     下面 `#lbCat` 是**可见**的「收藏到分类」下拉，与筛选无关，保留。 */
  fillCatSelect($("#lbCat"), false);
}
/* 合并真实分类与本地新建的未使用分类（后者 count=0） */
function catList(){
  const map={};
  for(const c of S.favCats) map[c.name]=c.count;
  for(const c of S.extraCats) if(!(c in map)) map[c]=0;
  return Object.entries(map).map(([name,count])=>({name,count}))
    .sort((a,b)=>b.count-a.count);
}
function fillCatSelect(sel, withAll){
  const cur = sel.value;
  const cats = catList();
  sel.innerHTML = "";
  if(withAll){
    sel.appendChild(new Option("全部分类", "*"));
    for(const c of cats) sel.appendChild(new Option(c.count?`${c.name} (${c.count})`:c.name, c.name));
  }else{
    if(!cats.length) sel.appendChild(new Option("默认", "默认"));   // 空分类兜底：否则 "__new__" 是唯一选项、会被自动选中，再点它不触发 change（新建分类失灵）
    for(const c of cats) sel.appendChild(new Option(c.name, c.name));
    sel.appendChild(new Option("新建分类…", "__new__"));
  }
  // 恢复原选择（选项仍在时）
  if([...sel.options].some(o=>o.value===cur)) sel.value = cur;
}
async function newCatPrompt(){
  return new Promise(resolve=>catInputDialog(resolve));
}
/* 新建分类弹窗（替代浏览器 prompt，部分浏览器会拦截 prompt） */
function catInputDialog(cb){
  /* F3 P2-4：两个弹窗共用 #catInputMask，后开的会把先开的 `old.remove()` 掉。
     而 close() 只 remove、**不调 cb** ⇒ newCatPrompt() 的 Promise 永不 resolve，
     调用方那串 await 整条静默挂起（无提示、无报错）。
     修法：每个实例挂 `_dismiss`（语义 = 按「取消」了结），顶替方负责先调用它。
     沿用同一个 id 是有意的 —— inert 基建与 mvTopOverlay 都按 #catInputMask 认这一族浮层。 */
  const old=document.getElementById("catInputMask");
  if(old){ if(typeof old._dismiss==="function") old._dismiss(); else old.remove(); }
  const mask=document.createElement("div"); mask.id="catInputMask";
  mask.innerHTML='<div id="catInputBox"><div class="cit">新建收藏分类</div>'+
    '<input maxlength="20" placeholder="输入分类名称">'+
    '<div class="cbtns"><button class="cancel">取消</button><button class="ok">确定</button></div></div>';
  document.body.appendChild(mask);
  const inp=mask.querySelector("input");
  const close=()=>mask.remove();
  /* 取消 / 点遮罩 / Esc / 空输入点确定 —— 一律以 null 了结 Promise。
     原先「取消」也不 resolve（只是调用方恰好在 await 它，才没暴露成 bug）。 */
  const settle=v=>{ close(); cb(v); };
  const ok=()=>{ const v=inp.value.trim(); settle(v?v:null); };
  mask._dismiss=()=>settle(null);            // 被别的弹窗顶掉 ⇒ 按取消了结
  mask.addEventListener("click",e=>{ if(e.target===mask) settle(null); });
  mask.querySelector(".cancel").onclick=()=>settle(null);
  mask.querySelector(".ok").onclick=ok;
  inp.onkeydown=e=>{
    if(e.isComposing || e.keyCode===229) return;   // 中文输入法确认候选词的回车不当「确定」
    if(e.key==="Enter") ok(); if(e.key==="Escape") settle(null); };
  setTimeout(()=>inp.focus(),0);
}

/* 通用确认弹窗（复用分类弹窗样式） */
function confirmDialog(title, msg, cb){
  const old=document.getElementById("catInputMask");
  if(old){ if(typeof old._dismiss==="function") old._dismiss(); else old.remove(); }
  const mask=document.createElement("div"); mask.id="catInputMask";
  mask.innerHTML='<div id="catInputBox"><div class="cit"></div>'+
    '<div style="font-size:calc(13px*var(--fsx));color:var(--tx2)"></div>'+
    '<div class="cbtns"><button class="cancel">取消</button><button class="ok danger-solid">删除</button></div></div>';
  mask.querySelector(".cit").textContent=title;
  mask.querySelector("#catInputBox > div:nth-child(2)").textContent=msg;
  document.body.appendChild(mask);
  const close=()=>mask.remove();
  const ok=()=>{ close(); cb(); };
  // 被顶掉 ⇒ 等价于「取消」：只关掉自己，**不**执行 cb（否则等于用户没点也删了）
  mask._dismiss=()=>close();
  mask.addEventListener("click",e=>{ if(e.target===mask) close(); });
  mask.querySelector(".cancel").onclick=close;
  mask.querySelector(".ok").onclick=ok;
  mask.tabIndex=0; mask.focus();
  const openedAt=Date.now();
  mask.onkeydown=e=>{ if(e.key==="Escape") close();
    // Enter 有 300ms 保护窗：按 Delete 弹窗后连按 Enter 不会立即误删
    if(e.key==="Enter" && Date.now()-openedAt>300) ok(); };
}

