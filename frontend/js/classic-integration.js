"use strict";
window.CMVClassic=(()=>{
  let jumpSequence=0;
  let rootQueue=Promise.resolve();
  function switchRoot(root, sequence=++jumpSequence){
    const operation=rootQueue.catch(()=>{}).then(async()=>{
      if(sequence!==jumpSequence)return null;
      const result=await api('/api/root',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:root})});
      return sequence===jumpSequence?result:null;
    });
    rootQueue=operation;
    return operation;
  }
  const request=async(url,options={})=>{
    const r=await fetch(url,{cache:'no-store',...options}),j=await r.json();
    if(!r.ok||j.error)throw new Error(j.error||'读取失败');
    return j;
  };
  function familyCleared(){ const el=document.querySelector('#cmvFamilyFilter'); if(el)el.hidden=true; }
  async function filterWorkflow(uuid,label){
    CMVLibrary.closeHub();
    closeLb(); favMode=false; libMode='';
    clearAllFilters();
    if(FLOW)document.querySelector('#flowPage')?.click();
    window.setViewMode('grid');
    CMVQuery.setWorkflowFamily(uuid);
    let el=document.querySelector('#cmvFamilyFilter');
    if(!el){el=document.createElement('div');el.id='cmvFamilyFilter';el.className='chips';
      document.querySelector('#stageWrap').before(el);}
    el.hidden=false;el.replaceChildren();
    const text=document.createElement('span');text.textContent='当前目录工作流：'+(label||'所选工作流');
    const clear=document.createElement('button');clear.className='sm';clear.textContent='取消筛选';
    clear.onclick=()=>{CMVQuery.setWorkflowFamily(null);familyCleared();void load(S.path,{queryOnly:true});};
    el.append(text,clear);
    await load(S.path||S.root,{queryOnly:true});
  }
  async function openMedia(identity){
    const sequence=++jumpSequence;
    const initialView=viewGen, initialLightbox=lbTok;
    const target=await request('/api/v2/classic/resolve-media?mediaId='+encodeURIComponent(identity.mediaId)
      +(identity.revisionId?'&revisionId='+encodeURIComponent(identity.revisionId):''));
    if(sequence!==jumpSequence||initialView!==viewGen||initialLightbox!==lbTok)return;
    closeLb(); CMVQuery.setWorkflowFamily(null);familyCleared();
    favMode=false;libMode='';clearAllFilters();
    CMVQuery.cancelPending();
    const rootView=++viewGen, rootLightbox=lbTok;
    const changed=await switchRoot(target.root,sequence);
    if(!changed||sequence!==jumpSequence)return;
    if(rootView!==viewGen||rootLightbox!==lbTok){void load('',{silent:true});return;}
    const loading=load(target.directory,{legacy:true,preserveLegacy:true}), targetView=viewGen;
    await loading;
    if(sequence!==jumpSequence||targetView!==viewGen||rootLightbox!==lbTok)return;
    if(normPath(S.root)!==normPath(target.root)||normPath(S.path)!==normPath(target.directory))
      throw new Error('目录切换未完成，请重试关联跳转');
    const item=S.items.find(x=>x.mediaId===target.item.mediaId&&x.revisionId===target.item.revisionId);
    if(!item)throw new Error('关联素材已变化，请刷新后重试');
    // Load uses the existing filtering and navigation machinery; establish the
    // exact selected snapshot even when a previous UI filter excluded it.
    S.view=[CMV3.remember(item)];S.idx=-1;
    const restore=()=>{if(!lb.classList.contains('on')){observer.disconnect();if(targetView===viewGen)applyFilter();}};
    const observer=new MutationObserver(restore);observer.observe(lb,{attributes:true,attributeFilter:['class']});
    await openLb(0);
    restore();
  }
  function pickMedia(signal){
    return new Promise((resolve,reject)=>{
      const dlg=bigDialog('选择已登记的图库素材');let sequence=0,cursor=null,context=null,done=false,controller=null;
      const search=document.createElement('input');search.placeholder='按文件名查找';search.setAttribute('aria-label','按文件名查找素材');
      const find=document.createElement('button');find.className='sm';find.textContent='查找';
      const status=document.createElement('p');status.setAttribute('role','status');
      const list=document.createElement('div');list.className='cmv-lib-list';
      const more=document.createElement('button');more.className='sm';more.textContent='下一批';more.hidden=true;
      dlg.body.append(search,find,status,list,more);
      const finish=value=>{if(done)return;done=true;controller?.abort();observer.disconnect();signal?.removeEventListener('abort',abort);
        dlg.close();resolve(value);};
      const abort=()=>{if(done)return;done=true;controller?.abort();observer.disconnect();dlg.close();reject(new DOMException('已取消','AbortError'));};
      const observer=new MutationObserver(()=>{if(!dlg.mask.isConnected)finish(null);});observer.observe(document.body,{childList:true});
      signal?.addEventListener('abort',abort,{once:true});if(signal?.aborted){abort();return;}
      async function read(reset){
        const own=++sequence;controller?.abort();controller=new AbortController();
        if(reset){cursor=null;list.replaceChildren();}
        status.textContent='读取已登记的图库素材…';more.disabled=true;
        try{
          context ||= await request('/api/v2/query/context',{signal:controller.signal});
          const filters={};if(search.value.trim())filters.nameTerms=[search.value.trim()];
          const data=await request('/api/v2/media/query',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({libraryId:context.libraryId,rootIds:[],recursive:true,filters,sort:'mtime_desc',limit:50,cursor}),signal:controller.signal});
          if(done||own!==sequence)return;
          for(const item of data.items){
            const button=document.createElement('button');button.className='cmv-lib-card';button.dataset.cmvPickMediaId=item.mediaId;
            button.textContent=item.name+' · '+item.path;button.onclick=()=>finish({...item,displayPath:item.path});list.append(button);
          }
          cursor=data.nextCursor;more.hidden=!cursor;status.textContent=data.indexedCount+' 个已登记素材（未扫描的目录不计入）';
        }catch(e){if(!done&&own===sequence&&e.name!=='AbortError')status.textContent=e.message;}
        finally{if(own===sequence)more.disabled=false;}
      }
      find.onclick=()=>void read(true);search.onkeydown=e=>{if(e.key==='Enter')void read(true);};more.onclick=()=>void read(false);
      void read(true);
    });
  }
  document.addEventListener('DOMContentLoaded',()=>{
    const originalClear=window.clearAllFilters;
    window.clearAllFilters=()=>{CMVQuery.setWorkflowFamily(null);familyCleared();return originalClear();};
    document.querySelectorAll('[data-cmv-library-tab]').forEach(el=>el.onclick=()=>window.CMVLibrary.openHub(el.dataset.cmvLibraryTab));
    document.querySelectorAll('[data-cmvd-open]').forEach(el=>el.onclick=()=>window.CMVData.open());
  });
  return {openMedia,filterWorkflow,pickMedia,familyCleared,switchRoot,cancelJump(){jumpSequence++;}};
})();
