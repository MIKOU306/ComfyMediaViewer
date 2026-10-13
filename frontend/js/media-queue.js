/* Two admissions include fetch and decode; retries preserve selected versions. */
"use strict";
const CMVThumbs = (() => {
  const records=new Map(),pending=[],active=new Set();
  const viewport=document.querySelector("#gridwrap");
  let scheduled=false,maxActive=0,blobBytes=0;
  function schedule(){if(!scheduled){scheduled=true;queueMicrotask(()=>{scheduled=false;pump();});}}
  function enqueue(r){
    if(r.queued||r.running||r.done||!r.img.isConnected||!r.near)return;
    r.queued=true;pending.push(r);schedule();
  }
  function revoke(r){if(r.blob){URL.revokeObjectURL(r.blob);r.blob=null;blobBytes-=r.bytes;r.bytes=0;}}
  function finish(r){clearTimeout(r.timeout);r.running=false;active.delete(r);schedule();}
  function dispose(r){
    r.done=true;r.controller?.abort();if(r.running)finish(r);clearTimeout(r.timeout);revoke(r);
    r.img.removeAttribute("onerror");r.img.onerror=null;r.img.removeAttribute("src");
    observer.unobserve(r.img);records.delete(r.img);
  }
  function terminal(r){r.done=true;r.img.classList.add("err");observer.unobserve(r.img);revoke(r);}
  function fail(r,status){
    if(!r.running)return;
    r.img.dataset.cmvError=String(status);
    r.img.title=status===409?"文件版本已变化，请刷新列表":status===503?"缩略图服务忙，请稍后刷新":"图片加载失败";
    r.controller?.abort();finish(r);revoke(r);
    if(!r.img.isConnected){dispose(r);return;}
    if(status===503 && r.attempt<3){r.timeout=setTimeout(()=>enqueue(r),[250,750,1500][r.attempt++]);return;}
    if(!r.original && (status===404 || status===415 || status===422)){
      r.original=true;r.img.dataset.fbd="1";enqueue(r);return;
    }
    terminal(r);
  }
  function loaded(r){if(r.running){delete r.img.dataset.cmvError;r.img.removeAttribute("title");r.done=true;finish(r);}}
  async function admit(r){
    const img=r.img;r.running=true;active.add(r);maxActive=Math.max(maxActive,active.size);
    img.loading="eager";img.removeAttribute("srcset");
    r.timeout=setTimeout(()=>fail(r,408),30000);
    if(r.original){img.src=img.dataset.fb;return;}
    const source=window.devicePixelRatio>1?img.dataset.cmvSrc2:img.dataset.cmvSrc;
    r.controller=new AbortController();
    try{
      const response=await fetch(source,{signal:r.controller.signal});
      if(!response.ok){fail(r,response.status);return;}
      const blob=await response.blob();
      if(!r.running || !img.isConnected){dispose(r);return;}
      if(blob.size>2*1024*1024 || blobBytes+blob.size>16*1024*1024){fail(r,422);return;}
      revoke(r);r.bytes=blob.size;blobBytes+=blob.size;r.blob=URL.createObjectURL(blob);img.src=r.blob;
    }catch(error){if(r.running)fail(r,error.name==="AbortError"?408:503);}
  }
  function pump(){
    const bounds=viewport.getBoundingClientRect();
    const ranked=pending.splice(0).map(r=>{
      const rect=r.img.getBoundingClientRect();
      return {r,visible:rect.bottom>bounds.top&&rect.top<bounds.bottom&&rect.right>bounds.left&&rect.left<bounds.right};
    });
    ranked.sort((a,b)=>Number(b.visible)-Number(a.visible));pending.push(...ranked.map(row=>row.r));
    while(active.size<2&&pending.length){
      const r=pending.shift();r.queued=false;
      if(!r.img.isConnected){dispose(r);continue;}
      if(r.near&&!r.done)void admit(r);
    }
  }
  const observer=new IntersectionObserver(entries=>{
    for(const entry of entries){
      const r=records.get(entry.target);if(!r)continue;
      r.near=entry.isIntersecting;
      if(r.near)enqueue(r);
      else if(r.done&&r.blob){revoke(r);r.img.removeAttribute("src");r.done=false;}
    }
  },{root:viewport,rootMargin:"150px 0px"});
  new MutationObserver(()=>{for(const [img,r]of records)if(!img.isConnected)dispose(r);schedule();})
    .observe(document.querySelector("#grid"),{childList:true,subtree:true});
  window.addEventListener("pagehide",()=>{for(const r of records.values())dispose(r);});
  function observe(img){
    if(!img||records.has(img))return;
    const r={img,near:false,queued:false,running:false,done:false,attempt:0,original:false,controller:null,blob:null,bytes:0};
    records.set(img,r);img.dataset.cmvManaged="1";
    img.addEventListener("load",()=>loaded(r));
    img.addEventListener("error",event=>{if(!r.running)return;event.stopImmediatePropagation();fail(r,422);},true);
    observer.observe(img);
  }
  return {observe,attributes:(one,two)=>'data-cmv-src="'+esc(one)+'" data-cmv-src2="'+esc(two)+'"',
    get state(){return {active:active.size,pending:pending.length,maxActive,blobBytes,tracked:records.size};}};
})();
