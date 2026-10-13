/* Frozen playback belongs to one selected version and one video element. */
"use strict";
const CMVPlayback = (() => {
  const records=new Map();
  const post=(route,body,extra={})=>fetch(route,{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify(body),...extra}).then(async response=>{
      const data=await response.json();
      if(!response.ok) {const error=new Error(data.error||"视频准备失败");error.status=response.status;throw error;}
      return data;
    });
  function close(record) {
    if(record.token) {
      const token=record.token;record.token=null;
      void post("/api/v2/playback/close",{...record.target,playback:token},{keepalive:true}).catch(()=>{});
    }
  }
  function release(video) {
    const record=records.get(video);if(!record) return;
    record.cancelled=true;clearTimeout(record.timer);close(record);records.delete(video);
    try {video.pause();video.removeAttribute("src");video.load();} catch(_) {}
  }
  async function attach(video,item) {
    release(video);
    if(!item?.mediaId || CMV3.info?.storageMode!=="sqlite") {video.src=fileURL(item.path,item);return;}
    const record={target:CMV3.target(item),token:null,cancelled:false,timer:null,attempt:0,renewing:false};
    records.set(video,record);
    async function prepare(position=0,resume=false) {
      if(record.cancelled || !video.isConnected || record.renewing) return;
      record.renewing=true;
      try {
        const data=await post("/api/v2/playback/start",record.target);
        record.token=data.playback;
        if(record.cancelled || !video.isConnected) {close(record);return;}
        video.src=fileURL(record.target.path,item)+"&playback="+encodeURIComponent(data.playback);
        if(position>0 || resume) video.addEventListener("loadedmetadata",()=>{
          if(record.cancelled) return;
          video.currentTime=Math.min(position,video.duration||position);
          if(resume) void video.play().catch(()=>{});
        },{once:true});
        record.attempt=0;
      } catch(error) {
        if(record.cancelled) return;
        if(error.status===503 && record.attempt<3) {
          record.timer=setTimeout(()=>void prepare(position,resume),[250,750,1500][record.attempt++]);
        } else { video.dispatchEvent(new CustomEvent("cmv-playback-error",{detail:error.message})); }
      } finally {record.renewing=false;}
    }
    video.addEventListener("error",async()=>{
      if(record.cancelled || record.renewing || !record.token || record.retried) return;
      // HTML media errors omit HTTP status: inspect the same frozen session with one byte.
      const response=await fetch(video.src,{headers:{Range:"bytes=0-0"}}).catch(()=>null);
      if(record.cancelled || response?.status!==410) return;
      record.retried=true;
      const position=video.currentTime,resume=!video.paused;
      close(record);void prepare(position,resume);
    });
    await prepare();
  }
  new MutationObserver(()=>{for(const [video] of records) if(!video.isConnected) release(video);})
    .observe(document.getElementById("gridwrap"),{childList:true,subtree:true});
  window.addEventListener("pagehide",()=>{for(const [video] of records) release(video);});
  let previews=Promise.resolve();
  function preview(video,item){
    previews=previews.catch(()=>{}).then(async()=>{
      if(!video.isConnected)return;
      let resolve;
      const ready=new Promise(done=>{resolve=done;});
      video.addEventListener("loadeddata",resolve,{once:true});
      video.addEventListener("error",resolve,{once:true});
      video.addEventListener("cmv-playback-error",resolve,{once:true});
      const timer=setTimeout(resolve,10000);
      await attach(video,item);await ready;clearTimeout(timer);
      const record=records.get(video);
      if(record){record.cancelled=true;close(record);records.delete(video);}
      video.removeEventListener("loadeddata",resolve);video.removeEventListener("error",resolve);
      video.removeEventListener("cmv-playback-error",resolve);
    });
  }
  return {attach,preview,release,releaseAll(){for(const [video]of records)release(video);},get active(){return records.size;}};
})();
