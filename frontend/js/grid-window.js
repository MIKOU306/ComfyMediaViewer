/* Keep stable grid positions; release heavy offscreen card subtrees. */
"use strict";
const CMVGridWindow = (() => {
  const records=new Map(),wrap=document.getElementById("gridwrap"),host=document.getElementById("grid");
  function restore(element){
    const r=records.get(element);
    if(!r?.parked || !element.isConnected || S.view[r.index]!==r.item)return element;
    const focus=document.activeElement===element,tab=element.tabIndex;
    records.delete(element);observer.unobserve(element);
    const fresh=card(r.index);fresh.tabIndex=tab;element.replaceWith(fresh);
    if(focus){gridSyncRoving(fresh);fresh.focus({preventScroll:true});}
    return fresh;
  }
  function park(element,r){
    if(r.parked || !element.isConnected || element.contains(document.activeElement) || !FLOW || !CMVQuery.active || VIEW_MODE!=="grid")return;
    const height=element.getBoundingClientRect().height;
    if(height<=0)return;
    for(const media of element.querySelectorAll("img,video")){
      vObs.unobserve(media);
      if(media.tagName==="VIDEO")CMVPlayback.release(media);
      media.removeAttribute("src");
    }
    ldUnqueue(element);S.cardEl.delete(r.item);
    const shell=element.cloneNode(false);shell.classList.remove("cmv-ld");
    shell.style.height=height+"px";shell.style.boxSizing="border-box";shell.dataset.cmvParked="1";
    shell.setAttribute("aria-label",r.item.name||"媒体");
    r.parked=true;records.delete(element);observer.unobserve(element);
    element.replaceWith(shell);records.set(shell,r);observer.observe(shell);
    shell.addEventListener("focus",()=>restore(shell));
    shell.addEventListener("pointerenter",()=>restore(shell),{once:true});
    shell.addEventListener("click",event=>{
      event.stopImmediatePropagation();const fresh=restore(shell);
      if(fresh!==shell)fresh.click();
    });
  }
  const observer=new IntersectionObserver(entries=>{
    for(const entry of entries){
      const r=records.get(entry.target);if(!r)continue;
      if(entry.isIntersecting)restore(entry.target);else park(entry.target,r);
    }
  },{root:wrap,rootMargin:"1000px 0px"});
  new MutationObserver(()=>{
    for(const [element]of records)if(!element.isConnected){observer.unobserve(element);records.delete(element);}
  }).observe(host,{childList:true});
  let width=wrap.clientWidth,resizePending=false;
  new ResizeObserver(()=>{
    if(width===wrap.clientWidth)return;width=wrap.clientWidth;
    if(resizePending)return;resizePending=true;
    requestAnimationFrame(()=>{resizePending=false;for(const [element]of [...records])restore(element);});
  }).observe(wrap);
  return {observe(element,index,item){
    if(!FLOW || !CMVQuery.active || VIEW_MODE!=="grid")return;
    records.set(element,{index,item,parked:false});observer.observe(element);
  },get state(){return {tracked:records.size,parked:[...records.values()].filter(r=>r.parked).length};}};
})();
