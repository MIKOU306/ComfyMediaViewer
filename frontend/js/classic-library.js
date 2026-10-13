/* Classic lightbox bridge for versioned library metadata. */
"use strict";
window.CMVLibrary = (() => {
  const PAGE = 40, mounts = new WeakMap();
  const node = (tag, cls, value) => { const n=document.createElement(tag); if(cls)n.className=cls; if(value!=null)n.textContent=String(value); return n; };
  const button = (label, action) => { const b=node("button","cmv-lib-btn",label); b.type="button"; b.addEventListener("click",action); return b; };
  const field = (label, control) => { const l=node("label","cmv-lib-field"); l.append(node("span","",label),control); return l; };
  const query = values => new URLSearchParams(Object.entries(values).filter(([,v])=>v!=null&&v!=="").map(([k,v])=>[k,String(v)])).toString();
  const request = (path, options={}) => window.api(path,options);
  const setStatus = (host, value, state="ready") => { host.dataset.state=state; host.replaceChildren(node("p",state==="error"?"cmv-lib-error":"cmv-lib-status",value)); };
  const makeFold = (title,key) => { const sec=node("section","cmv-lib-section"); sec.dataset.cmvLibrarySection=key; sec.dataset.cmvLibSection=key; const d=node("details","cmv-lib-fold"); d.append(node("summary","",""+title)); const body=node("div","cmv-lib-body"); body.dataset.state="idle"; d.append(body); sec.append(d); return {sec,d,body}; };

  function cancel(host) {
    const state=host&&mounts.get(host); if(!state)return;
    state.cancelled=true; state.generation++;
    if(state.dispose){try{state.dispose();}catch{}state.dispose=null;}
    for(const c of state.controllers)c.abort(); state.controllers.clear();
    if(state.observer)state.observer.disconnect();
  }
  function mount(host, meta, j, isCurrent) {
    if(!host||!j)return;
    cancel(host);
    const identity={mediaId:j.mediaId||"",revisionId:j.revisionId||"",snapshotId:j.metadataArchive&&j.metadataArchive.snapshotId||""};
    const state={generation:0,controllers:new Set(),observer:null,cancelled:false,dispose:null}; mounts.set(host,state);
    const active=()=>mounts.get(host)===state&&!state.cancelled&&host.isConnected&&(!isCurrent||isCurrent());
    const sections=[makeFold("工作流","workflow"),makeFold("模型","model"),makeFold("作品关联","relations")];
    for(const x of sections)host.append(x.sec);
    sections[0].d.addEventListener("toggle",()=>{if(sections[0].d.open&&sections[0].body.dataset.state==="idle"){sections[0].body.dataset.state="loading";void workflowSources(sections[0].body,state,active,identity);}});
    sections[1].d.addEventListener("toggle",()=>{if(sections[1].d.open&&sections[1].body.dataset.state==="idle"){sections[1].body.dataset.state="loading";void models(sections[1].body,state,active,identity);}});
    sections[2].d.addEventListener("toggle",()=>{if(sections[2].d.open&&sections[2].body.dataset.state==="idle"){sections[2].body.dataset.state="loading";void relations(sections[2].body,state,active,identity);}});
    const lb=document.getElementById("lb");
    if(lb&&window.MutationObserver){state.observer=new MutationObserver(()=>{if(!lb.classList.contains("on"))cancel(host);});state.observer.observe(lb,{attributes:true,attributeFilter:["class"]});}
  }
  async function get(state,host,active,path,options={}) {
    if(!active())return null;
    const c=new AbortController();state.controllers.add(c);
    try{const r=await request(path,{...options,signal:c.signal});return active()?r:null;}
    finally{state.controllers.delete(c);}
  }
  const post=(state,host,active,path,data)=>get(state,host,active,path,{method:"POST",headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});

  async function workflowSources(host,state,active,id) {
    host.replaceChildren();
    if(!id.snapshotId){setStatus(host,"当前媒体没有图库快照，无法读取工作流来源。","unavailable");return;}
    const msg=node("p","cmv-lib-status","工作流来源是嵌入元数据声明，不代表实际执行。");
    const list=node("div","cmv-lib-list"), detail=node("div","cmv-lib-detail");
    const publish=button("提取此版本的工作流来源",async()=>{publish.disabled=true;msg.textContent="正在提取…";try{await post(state,host,active,"/api/v2/workflows/sources/publish",{snapshotUuid:id.snapshotId});if(active()){host.dataset.state="idle";await read(true);}}catch(e){msg.textContent="提取失败："+(e.message||e);}finally{if(active())publish.disabled=false;}});publish.dataset.cmvLibAction="publish-sources";
    const more=button("加载更多来源",()=>void read(false));more.hidden=true;
    host.append(msg,publish,list,more,detail);
    let after=0,next=null,items=[];
    async function read(reset){const cursor=reset?0:next;if(!reset&&cursor==null)return;more.disabled=true;msg.textContent="正在读取工作流来源…";
      try{const p=await get(state,host,active,"/api/v2/workflows/sources?"+query({snapshotUuid:id.snapshotId,afterId:cursor,limit:PAGE}));if(!p||!active())return;
        items=reset?(p.items||[]):items.concat(p.items||[]);next=p.nextAfterId??null;after=next??after;list.replaceChildren();
        for(const x of items){const uuid=x.uuid||x.sourceUuid;if(!uuid)continue;list.append(button((x.kind||"来源")+" · "+(x.status||"状态未知")+(x.ambiguous?" · 重复来源":""),()=>void source(uuid)));}
        more.hidden=next==null;msg.textContent=items.length?"已读取 "+items.length+" 条来源":"此版本没有工作流来源记录。";
      }catch(e){if(active())msg.textContent="读取失败："+(e.message||e);}finally{if(active())more.disabled=false;}
    }
    async function source(sourceUuid){detail.replaceChildren(node("p","cmv-lib-status","正在读取来源…"));try{
      const d=await get(state,host,active,"/api/v2/workflows/source?"+query({sourceUuid:sourceUuid}));if(!d||!active())return;
      const r=d.record||{};detail.replaceChildren(node("h4","",(r.kind||"工作流")+"来源"),node("p","cmv-lib-note","状态："+(r.status||"未知")+"；嵌入声明不证明执行。"));
      if(r.reason)detail.append(node("p","cmv-lib-note",r.reason));
      detail.append(node("pre","cmv-lib-code",JSON.stringify(r.source_locator||{},null,2)));
      if(typeof r.text!=="string"){detail.append(node("p","cmv-lib-note","没有可用的来源文本。"));return;}
      if(new TextEncoder().encode(r.text).length>8*1024*1024){detail.append(node("p","cmv-lib-note","来源文本超过导入上限。"));return;}
      const raw=node("details","cmv-lib-raw");raw.append(node("summary","","查看来源原文"),node("pre","cmv-lib-code",r.text));detail.append(raw);
      detail.append(button("导出来源原文",()=>{const a=document.createElement("a");a.href=URL.createObjectURL(new Blob([r.text],{type:"application/json;charset=utf-8"}));a.download="workflow-source-"+sourceUuid+".json";a.click();setTimeout(()=>URL.revokeObjectURL(a.href),3000);}));
      const families=await get(state,host,active,"/api/v2/workflows/families?"+query({afterId:0,limit:100}));if(!families||!active())return;
      const select=document.createElement("select");select.append(new Option("创建新家族","__new__"));for(const f of families.items||[])select.append(new Option(f.name||"未命名家族",f.workflow_uuid));
      const name=document.createElement("input");name.value=(r.kind||"工作流")+"（嵌入来源）";name.dataset.cmvLibField="source-family-name";select.dataset.cmvLibField="source-family";
      const importSource=button("明确导入此来源",async()=>{try{
        let uuid=select.value;if(uuid==="__new__"){if(!crypto.randomUUID)throw Error("无法安全创建家族编号");uuid=crypto.randomUUID();}
        await post(state,host,active,"/api/v2/workflows/sources/import",{sourceUuid:sourceUuid,workflowUuid:uuid,name:name.value.trim()});
        if(active())detail.append(node("p","cmv-lib-status","导入请求已完成。来源声明仍不证明执行。"));
      }catch(e){if(active())detail.append(node("p","cmv-lib-error","导入失败："+(e.message||e)));}});importSource.dataset.cmvLibAction="import-source";
      detail.append(field("导入到家族",select),field("新家族名称",name),importSource);
    }catch(e){if(active())detail.replaceChildren(node("p","cmv-lib-error","读取来源失败："+(e.message||e)));}}
    await read(true);if(active())host.dataset.state=host.dataset.state==="error"?"error":"ready";
  }

  async function models(host,state,active,id) {
    if(!id.mediaId||!id.revisionId){setStatus(host,"当前临时媒体没有图库身份，无法读取模型引用。","unavailable");return;}
    const msg=node("p","cmv-lib-status","正在读取模型引用…"), rootsBox=node("div","cmv-lib-list"), cards=node("div","cmv-lib-cards");host.append(msg,rootsBox,cards);
    let roots=[],refs=[];const timers=new Set();
    async function refresh(){const pair=await Promise.all([get(state,host,active,"/api/v2/models/roots"),get(state,host,active,"/api/v2/models/references?"+query({mediaUuid:id.mediaId,revisionUuid:id.revisionId}))]);if(!pair[0]||!pair[1]||!active())return;roots=pair[0].roots||[];refs=pair[1].references||[];draw();}
    async function write(route,data){return post(state,host,active,"/api/v2/models/"+route,data);}
    function draw(){
      rootsBox.replaceChildren(node("h4","","已登记模型目录"));for(const r of roots)rootsBox.append(node("p","cmv-lib-root",(r.name||"模型目录")+" · "+(r.path||r.rootUuid)));
      const rf=document.createElement("form");rf.className="cmv-lib-form";const rp=document.createElement("input");rp.placeholder="模型目录完整路径";rp.required=true;const rn=document.createElement("input");rn.value="Models";const rb=node("button","cmv-lib-btn","登记目录");rb.type="submit";const rs=node("p","cmv-lib-status","");rf.append(field("目录",rp),field("名称",rn),rb,rs);rf.addEventListener("submit",async e=>{e.preventDefault();rb.disabled=true;try{await write("roots/register",{path:rp.value.trim(),name:rn.value.trim()});await refresh();rs.textContent="目录已登记。";}catch(err){rs.textContent=err.message||"登记失败";}finally{rb.disabled=false;}});rootsBox.append(rf);
      cards.replaceChildren();if(!refs.length)cards.append(node("p","cmv-lib-note","此版本没有可确认的模型引用。"));for(const x of refs)cards.append(reference(x));
    }
    function reference(entry){
      const ref=entry.reference||{},key=entry.referenceKey,card=node("article","cmv-lib-card");if(key)card.dataset.cmvModelReference=key;card.append(node("h4","",(ref.name||"模型引用")+" · "+(ref.referenceFamily||ref.kind||"类型未知")));
      const ov=entry.override;card.append(node("p","cmv-lib-status",ov&&ov.status==="confirmed"?"已确认人工映射："+(ov.modelName||ref.name||"模型")+"（版本 "+ov.version+"）":ov&&ov.status==="revoked"?"人工映射已撤销":"尚无人工映射"));
      if(!key||entry.confirmationAvailable===false){card.append(node("p","cmv-lib-note",entry.reason||"来源定位不足，暂不能确认。"));return card;}
      const root=document.createElement("select");root.dataset.cmvLibField="model-root";root.append(new Option("选择已登记目录",""));for(const r of roots)root.append(new Option((r.name||"目录")+" · "+r.path,r.rootUuid));
      const rp=document.createElement("input");rp.placeholder="新目录完整路径（可选）";const rn=document.createElement("input");rn.value="Models";
      const rel=document.createElement("input");rel.placeholder="目录内相对路径";rel.dataset.cmvLibField="model-relative-path";const type=document.createElement("select");
      for(const [v,l] of [["checkpoint","检查点"],["lora","LoRA"],["embedding","嵌入"],["vae","VAE"],["controlnet","ControlNet"],["other","其他"]])type.append(new Option(l,v));
      type.value=ref.referenceFamily==="lyco"?"lora":(["checkpoint","lora","embedding","vae","controlnet"].includes(ref.kind)?ref.kind:"other");
      const msg=node("p","cmv-lib-status",""),actions=node("div","cmv-lib-actions");card.append(field("模型目录",root),field("新增目录",rp),field("目录名称",rn),field("相对路径",rel),field("模型类型",type),msg,actions);
      let job=null,registered=null,fresh=null,pollTimer=null,terminal=false,firstCompleted=false,running=false;
      const schedulePoll=(fn,delay)=>{if(pollTimer){clearTimeout(pollTimer);timers.delete(pollTimer);}const timer=setTimeout(()=>{timers.delete(timer);if(pollTimer===timer)pollTimer=null;void fn();},delay);pollTimer=timer;timers.add(timer);};
      const fields=[root,rp,rn,rel,type];
      const restoreControls=()=>{const locked=firstCompleted||registered!==null||!!fresh;for(const f of fields)f.disabled=locked;start.disabled=firstCompleted||registered!==null;register.disabled=registered!==null||!firstCompleted;freshBtn.disabled=registered===null||!!fresh;confirm.disabled=registered===null||!fresh;cancelJob.hidden=true;cancelJob.disabled=false;};
      const cancelJob=button("取消核验",async()=>{if(!job||terminal||!running||cancelJob.disabled)return;cancelJob.disabled=true;try{await write("verification/cancel",{jobUuid:job});msg.textContent="已请求取消核验，正在等待任务停止…";}catch(e){msg.textContent="取消失败："+(e.message||e);cancelJob.disabled=false;}});cancelJob.dataset.cmvLibAction="cancel-model-verification";cancelJob.hidden=true;
      async function verify(second){
        if(running||(second?(registered===null||!!fresh):(firstCompleted||registered!==null)))return;
        running=true;
        for(const f of fields)f.disabled=true;start.disabled=true;register.disabled=true;freshBtn.disabled=true;confirm.disabled=true;cancelJob.disabled=false;
        try{let rootUuid=root.value;if(!rootUuid&&rp.value.trim()){const rr=await write("roots/register",{path:rp.value.trim(),name:rn.value.trim()});rootUuid=rr.rootUuid;const option=new Option((rn.value.trim()||"目录")+" · "+rp.value.trim(),rootUuid);root.append(option);root.value=rootUuid;}
          if(!rootUuid||!rel.value.trim())throw Error("请选择目录并填写相对路径");
          terminal=false;firstCompleted=second?firstCompleted:false;if(second)fresh=null;
          const r=await write("verification/start",{rootUuid,relativePath:rel.value.trim()});job=r.jobUUID||r.jobUuid;if(!job)throw Error("未取得核验任务编号");
          msg.textContent=second?"正在重新核验…":"正在核验文件…";cancelJob.hidden=false;
          const poll=async()=>{if(!active()||!job)return;try{const q=await get(state,host,active,"/api/v2/models/verification/status?"+query({jobUuid:job}));if(!q)return;
            if(q.status==="completed"){terminal=true;running=false;job=q.jobUuid||q.jobUUID||job;if(second)fresh=job;else firstCompleted=true;msg.textContent=second?"复核完成，可保存人工映射。":"核验完成，可登记文件。";restoreControls();return;}
            if(["failed","cancelled","interrupted"].includes(q.status)){terminal=true;running=false;job=null;if(second)fresh=null;else firstCompleted=false;msg.textContent=q.error||(q.status==="cancelled"?"核验已取消。":q.status==="interrupted"?"核验已中断。":"核验失败。");restoreControls();return;}
            const p=q.progress||{};msg.textContent="核验中 "+Number(p.bytesRead||0).toLocaleString()+" / "+Number(p.sizeBytes||0).toLocaleString()+" 字节";schedulePoll(poll,1100);
          }catch(e){if(active()){msg.textContent="读取任务失败："+(e.message||e);schedulePoll(poll,1500);}}};schedulePoll(poll,250);
        }catch(e){terminal=true;running=false;job=null;if(second)fresh=null;else firstCompleted=false;msg.textContent="无法启动核验："+(e.message||e);restoreControls();}
      }
      const start=button("第 1 步：核验文件",()=>void verify(false));start.dataset.cmvLibAction="verify-model";
      const register=button("登记已核验文件",async()=>{register.disabled=true;try{registered=await write("files/register",{rootUuid:root.value,relativePath:rel.value.trim(),jobUuid:job,modelType:type.value});job=null;firstCompleted=false;msg.textContent="文件已登记；需要重新核验后保存映射。";freshBtn.disabled=false;start.disabled=false;}catch(e){msg.textContent="登记失败："+(e.message||e);register.disabled=false;}});register.dataset.cmvLibAction="register-model-file";
      register.disabled=true;const freshBtn=button("第 2 步：重新核验",()=>void verify(true));freshBtn.disabled=true;freshBtn.dataset.cmvLibAction="verify-model-again";
      const confirm=button("保存人工映射",async()=>{confirm.disabled=true;try{await write("references/confirm",{mediaUuid:id.mediaId,revisionUuid:id.revisionId,referenceKey:key,modelUuid:registered.modelUuid,locationUuid:registered.locationUuid,rootUuid:root.value,relativePath:rel.value.trim(),freshJobUuid:fresh,expectedOverrideVersion:ov&&ov.version||0});await refresh();}catch(e){msg.textContent="保存失败："+(e.message||e);confirm.disabled=false;}});confirm.dataset.cmvLibAction="confirm-model-reference";
      confirm.disabled=true;if(ov&&ov.status==="confirmed")actions.append(button("撤销人工映射",async()=>{try{await write("references/revoke",{mediaUuid:id.mediaId,revisionUuid:id.revisionId,referenceKey:key,expectedOverrideVersion:ov.version});await refresh();}catch(e){msg.textContent="撤销失败："+(e.message||e);}}));
      actions.append(start,register,freshBtn,confirm,cancelJob);return card;
    }
    try{await refresh();if(active())host.dataset.state="ready";}catch(e){if(active())setStatus(host,"模型读取失败："+(e.message||e),"error");}
    const previousDispose=state.dispose;state.dispose=()=>{if(previousDispose)previousDispose();for(const timer of timers)clearTimeout(timer);timers.clear();};
  }

  async function relations(host,state,active,id) {
    if(!id.mediaId||!id.revisionId){setStatus(host,"当前媒体没有图库身份，无法读取作品关联。","unavailable");return;}
    const msg=node("p","cmv-lib-status","正在读取执行记录…"),select=document.createElement("select"),more=button("加载更多执行记录",()=>void page(false)),graph=node("div","cmv-lib-graph");
    select.dataset.cmvLibField="relation-current-execution";
    const record=node("details","cmv-lib-candidate");record.dataset.cmvLibForm="record-execution";record.append(node("summary","","记录一次执行声明"));
    const executionKey=document.createElement("input");executionKey.required=true;executionKey.maxLength=1024;executionKey.dataset.cmvLibField="execution-source-key";
    const executionRole=document.createElement("input");executionRole.value="generated_image";executionRole.required=true;executionRole.maxLength=128;
    const executionDirection=document.createElement("select");executionDirection.dataset.cmvLibField="execution-direction";
    executionDirection.append(new Option("作为输出","output"),new Option("作为输入","input"));
    const executionNote=document.createElement("textarea");executionNote.required=true;executionNote.maxLength=4096;
    const executionStatus=node("p","cmv-lib-status","这会记录一条人工执行声明，不代表系统已核实执行。");
    const saveExecution=button("记录执行声明",async()=>{if(!executionKey.value.trim()||!executionNote.value.trim()||!executionRole.value.trim()){executionStatus.textContent="请填写来源键、角色和说明。";return;}
      saveExecution.disabled=true;try{const binding={revisionUuid:id.revisionId,role:executionRole.value.trim()},output=executionDirection.value==="output";
        await post(state,host,active,"/api/v2/relations/executions",{sourceKey:executionKey.value.trim(),evidence:{sourceType:"user_entry",note:executionNote.value.trim()},inputs:output?[]:[binding],outputs:output?[binding]:[]});
        executionStatus.textContent="执行声明已记录，身份状态为“执行未核实”。";executionKey.value="";executionNote.value="";await page(true);
      }catch(e){executionStatus.textContent="记录失败："+(e.message||e);}finally{if(active())saveExecution.disabled=false;}});
    saveExecution.dataset.cmvLibAction="record-execution";
    record.append(field("稳定来源键",executionKey),field("当前版本角色",executionRole),field("角色方向",executionDirection),field("来源说明",executionNote),executionStatus,saveExecution);
    select.append(new Option("选择此版本的执行记录",""));host.append(node("p","cmv-lib-note","关联候选需人工确认；候选不代表已确认作品链。"),record,field("执行记录",select),more,msg,graph);
    let next=null,rows=[];more.hidden=true;select.addEventListener("change",()=>void loadGraph());
    async function page(reset){const cursor=reset?0:next;if(!reset&&cursor==null)return;more.disabled=true;try{const p=await get(state,host,active,"/api/v2/relations/media?"+query({mediaUuid:id.mediaId,revisionUuid:id.revisionId,afterId:cursor,limit:PAGE}));if(!p||!active())return;
      rows=reset?(p.items||[]):rows.concat(p.items||[]);next=p.nextAfterId??null;select.replaceChildren(new Option("选择此版本的执行记录",""));for(const x of rows)select.append(new Option((x.identityState||"未知")+" · "+(x.source&&x.source.key||x.executionUuid),x.executionUuid));more.hidden=next==null;msg.textContent=rows.length+" 条执行记录。";
    }catch(e){if(active())msg.textContent="执行记录读取失败："+(e.message||e);}finally{if(active())more.disabled=false;}}
    async function loadGraph(){const uuid=select.value;graph.replaceChildren();if(!uuid)return;try{const g=await get(state,host,active,"/api/v2/relations/graph?"+query({executionUuid:uuid,includeCandidates:1}));if(!g||!active()||select.value!==uuid)return;
      const edges=(g.edges||[]).concat(g.evidenceOnly||[]);if(!edges.length)graph.append(node("p","cmv-lib-note","没有关联记录或候选。"));
      for(const edge of edges){const c=node("article","cmv-lib-card");c.append(node("h4","",(edge.domain||"关系")+" · "+(edge.kind||"类型未知")+" · "+(edge.evidenceState||"状态未知")));
        const label=(x)=>x&&x.source&&x.source.key||x&&x.executionUuid||"未知";
        c.append(node("p","",label(edge.source)+" → "+label(edge.target)));
        for(const endpoint of [edge.source,edge.target])for(const binding of endpoint&&endpoint.revisions||[])if(binding.mediaUuid&&binding.revisionUuid)c.append(button("打开关联媒体版本",()=>{if(window.CMVClassic&&window.CMVClassic.openMedia)window.CMVClassic.openMedia({mediaId:binding.mediaUuid,revisionId:binding.revisionUuid});}));
        const decision=edge.decision;
        c.append(node("p","cmv-lib-status",decision?(decision.state==="confirmed"?"已确认":"已撤销")+"（版本 "+decision.version+"）":"尚未决定"));
        const evidence=node("details","cmv-lib-evidence");evidence.append(node("summary","","查看来源说明"),node("pre","cmv-lib-code",JSON.stringify(edge.evidence||{},null,2)));c.append(evidence);
        const actions=node("div","cmv-lib-actions");
        if(edge.evidenceState==="inferred"||edge.domain==="experiment"){
          if(!decision||decision.state!=="confirmed"){const b=button("确认关系",()=>void decide(edge,"confirm",actions));b.dataset.cmvRelationAction="confirm";b.dataset.cmvRelationUuid=edge.relationUuid;actions.append(b);}
          if(decision&&decision.state==="confirmed"){const b=button("撤销确认",()=>void decide(edge,"revoke",actions));b.dataset.cmvRelationAction="revoke";b.dataset.cmvRelationUuid=edge.relationUuid;actions.append(b);}
        }
        const hd=node("details","cmv-lib-history");hd.append(node("summary","","查看决定历史"));const hb=node("div","");const hm=button("加载更多历史",()=>void history(edge,hb,hm));hm.dataset.cmvRelationHistory=edge.relationUuid;hd.append(hb,hm);actions.append(hd);c.append(actions);graph.append(c);
      }
      if(g.status==="incomplete")graph.append(node("p","cmv-lib-note","关联图达到读取上限，部分内容尚未展开。"));
    }catch(e){if(active())msg.textContent="关联读取失败："+(e.message||e);}}
    async function decide(edge,action,box){const reason=prompt(action==="confirm"?"确认理由：":"撤销理由：");if(reason==null)return;if(!reason.trim())return;
      try{await post(state,host,active,"/api/v2/relations/"+action,{relationUuid:edge.relationUuid,expectedDecisionVersion:edge.decision&&edge.decision.version||0,reason:reason.trim()});await loadGraph();}catch(e){box.append(node("p","cmv-lib-error","保存失败："+(e.message||e)));}}
    async function history(edge,box,b){b.disabled=true;const after=Number(b.dataset.after||0);try{const p=await get(state,host,active,"/api/v2/relations/history?"+query({relationUuid:edge.relationUuid,afterVersion:after,limit:PAGE}));if(!p||!active())return;for(const x of p.items||[])box.append(node("p","cmv-lib-history-row","版本 "+x.version+" · "+(x.state==="confirmed"?"确认":"撤销")+" · "+(x.createdAt||"")+" · "+(x.reason||"无说明")));b.dataset.after=p.nextAfterVersion??"";b.hidden=p.nextAfterVersion==null;}catch(e){box.append(node("p","cmv-lib-error","历史读取失败："+(e.message||e)));}finally{if(active())b.disabled=false;}}
    const candidate=node("details","cmv-lib-candidate");candidate.append(node("summary","","记录关系候选"));
    const candidateMsg=node("p","cmv-lib-status","请先选择来源素材、来源执行记录和当前版本的目标执行记录。");
    const sourceMediaLabel=node("p","cmv-lib-status","尚未选择来源素材。"),pickSourceMedia=button("选择图库中的来源素材",()=>void pickMedia()),sourceExecution=document.createElement("select"),targetExecution=document.createElement("select");
    pickSourceMedia.dataset.cmvLibAction="pick-source-media";sourceExecution.dataset.cmvLibField="relation-source-execution";targetExecution.dataset.cmvLibField="relation-target-execution";
    const executionMore=button("加载更多来源执行",()=>void sourceExecutionPage(false));
    const sourceKey=document.createElement("input");sourceKey.required=true;sourceKey.maxLength=1024;sourceKey.placeholder="可重复识别该关系的来源键";sourceKey.dataset.cmvLibField="relation-source-key";
    const evidence=document.createElement("textarea");evidence.required=true;evidence.maxLength=4096;evidence.placeholder="说明为什么提出这条候选";evidence.dataset.cmvLibField="relation-evidence";
    const domain=document.createElement("select");domain.append(new Option("衍生","derivation"),new Option("实验","experiment"));
    const kind=document.createElement("select");for(const k of ["unknown","img2img","inpaint","upscale","variation"])kind.append(new Option(k==="unknown"?"类型未知":k,k));
    const saveCandidate=button("保存候选关系",()=>void createCandidate());saveCandidate.disabled=true;saveCandidate.dataset.cmvLibAction="create-relation-candidate";
    candidate.append(candidateMsg,sourceMediaLabel,pickSourceMedia,field("来源执行记录",sourceExecution),executionMore,
      field("目标执行记录（当前版本）",targetExecution),field("来源键",sourceKey),field("关系领域",domain),field("关系类型",kind),field("候选说明",evidence),saveCandidate);
    graph.before(candidate);
    let selectedSourceMedia=null,sourceExecutionCursor=null,sourceExecutionRows=[];
    async function pickMedia(){if(!window.CMVClassic||typeof window.CMVClassic.pickMedia!=="function"){candidateMsg.textContent="图库素材选择器尚未接入。";return;}
      const c=new AbortController();state.controllers.add(c);pickSourceMedia.disabled=true;
      try{const item=await window.CMVClassic.pickMedia(c.signal);if(!active()||!item)return;
        selectedSourceMedia=item;sourceMediaLabel.textContent="来源素材："+(item.displayPath||item.name||item.path||item.mediaId)+" · "+item.mediaId;
        sourceExecutionRows=[];sourceExecutionCursor=null;sourceExecution.replaceChildren(new Option("选择来源执行记录",""));await sourceExecutionPage(true);
      }catch(e){if(active()&&e.name!=="AbortError")candidateMsg.textContent="素材选择失败："+(e.message||e);}
      finally{state.controllers.delete(c);if(active())pickSourceMedia.disabled=false;}}
    async function sourceExecutionPage(reset=true){const mediaId=selectedSourceMedia&&selectedSourceMedia.mediaId;if(!mediaId)return;const cursor=reset?0:sourceExecutionCursor;if(!reset&&cursor==null)return;
      try{const p=await get(state,host,active,"/api/v2/relations/media?"+query({mediaUuid:mediaId,afterId:cursor,limit:PAGE}));if(!p||!active())return;
        sourceExecutionRows=reset?(p.items||[]):sourceExecutionRows.concat(p.items||[]);sourceExecutionCursor=p.nextAfterId??null;
        sourceExecution.replaceChildren(new Option("选择来源执行记录",""));for(const x of sourceExecutionRows)sourceExecution.append(new Option((x.identityState||"未知")+" · "+(x.source&&x.source.key||x.executionUuid),x.executionUuid));
        executionMore.hidden=sourceExecutionCursor==null;updateCandidate();
      }catch(e){candidateMsg.textContent="来源执行读取失败："+(e.message||e);}}
    function updateCandidate(){const selected=targetExecution.value;targetExecution.replaceChildren(new Option("选择目标执行记录",""));for(const x of rows)targetExecution.append(new Option((x.identityState||"未知")+" · "+(x.source&&x.source.key||x.executionUuid),x.executionUuid));if(rows.some(x=>x.executionUuid===selected))targetExecution.value=selected;
      saveCandidate.disabled=!sourceExecution.value||!targetExecution.value||sourceExecution.value===targetExecution.value;}
    async function createCandidate(){if(saveCandidate.disabled||!sourceKey.value.trim()||!evidence.value.trim()){candidateMsg.textContent="请选择两个不同的执行记录并填写来源键与说明。";return;}
      saveCandidate.disabled=true;try{await post(state,host,active,"/api/v2/relations/candidates",{sourceExecutionUuid:sourceExecution.value,targetExecutionUuid:targetExecution.value,sourceKey:sourceKey.value.trim(),evidence:{sourceType:"user_entry",note:evidence.value.trim()},domain:domain.value,kind:kind.value});
        candidateMsg.textContent="候选已记录，尚未确认。";sourceKey.value="";evidence.value="";await loadGraph();
      }catch(e){candidateMsg.textContent="记录候选失败："+(e.message||e);}finally{if(active())updateCandidate();}}
    candidate.addEventListener("toggle",()=>{if(candidate.open&&window.CMVClassic&&typeof window.CMVClassic.pickMedia!=="function")candidateMsg.textContent="图库素材选择器尚未接入。";});
    sourceExecution.addEventListener("change",updateCandidate);targetExecution.addEventListener("change",updateCandidate);
    executionMore.addEventListener("click",()=>void sourceExecutionPage(false));
    await page(true);if(active())host.dataset.state="ready";
  }

  function openHub(tab="workflows") {
    closeHub();
    const overlay=node("div","cmv-library-hub"),panel=node("section","cmv-library-hub-panel"),head=node("header","cmv-library-hub-head"),tabs=node("nav","cmv-library-hub-tabs"),content=node("div","cmv-library-hub-content");
    overlay.dataset.cmvLibraryHub="true";
    overlay.setAttribute("role","dialog");overlay.setAttribute("aria-modal","true");overlay.setAttribute("aria-label","图库管理");
    const state={generation:0,controllers:new Set()};const current=()=>overlay.isConnected;
    overlay.cmvClose=()=>{state.generation++;for(const c of state.controllers)c.abort();overlay.remove();};
    const close=button("关闭",overlay.cmvClose);head.append(node("h2","","图库管理"),close);panel.append(head,tabs,content);overlay.append(panel);document.body.append(overlay);
    for(const [key,label] of [["workflows","工作流库"],["models","模型目录"]]){const t=button(label,()=>void render(key));t.dataset.cmvHubTab=key;tabs.append(t);}
    async function render(key){const gen=++state.generation;for(const c of state.controllers)c.abort();content.replaceChildren();const ok=()=>current()&&state.generation===gen;if(key==="workflows")await workflowHub(content,state,ok);else await modelHub(content,state,ok);}
    void render(tab==="models"?"models":"workflows");
  }

  async function workflowHub(host,state,active) {
    const msg=node("p","cmv-lib-status","正在读取家族…"),familiesBox=node("div","cmv-lib-list"),versionsBox=node("div","cmv-lib-list"),capturesBox=node("div","cmv-lib-list"),detail=node("div","cmv-lib-detail"),moreF=button("加载更多家族",()=>void familyPage(false)),moreV=button("加载更多版本",()=>void versionPage(false)),moreC=button("加载更多捕获",()=>void capturePage(false));
    host.append(msg,node("h3","","工作流家族"),familiesBox,moreF,node("h3","","家族版本"),versionsBox,moreV,node("h3","","捕获及家族归属"),capturesBox,moreC,detail);
    let familyNext=null,versionNext=null,captureNext=null,families=[],versions=[],captures=[],family=null,version=null;
    const imp=node("details","cmv-lib-import"),raw=document.createElement("textarea"),familySelect=document.createElement("select"),name=document.createElement("input");imp.dataset.cmvLibForm="workflow-import";raw.dataset.cmvLibField="workflow-json";familySelect.dataset.cmvLibField="workflow-family";name.dataset.cmvLibField="workflow-family-name";
    imp.append(node("summary","","导入工作流 JSON"),field("目标家族",familySelect),field("家族名称",name),field("工作流原文",raw));
    const importWorkflow=button("明确导入工作流",async()=>{try{let uuid=familySelect.value;if(uuid==="__new__"){if(!crypto.randomUUID)throw Error("当前环境不支持安全家族编号");uuid=crypto.randomUUID();}
      const r=await post(state,host,active,"/api/v2/workflows/import",{rawWorkflow:raw.value,workflowUuid:uuid,name:name.value.trim()});if(r&&active()){msg.textContent="工作流原文已导入。";await familyPage(true);}
    }catch(e){if(active())msg.textContent="导入失败："+(e.message||e);}});importWorkflow.dataset.cmvLibAction="import-workflow";imp.append(importWorkflow);host.prepend(imp);
    async function familyPage(reset){const after=reset?0:familyNext;if(!reset&&after==null)return;try{const p=await get(state,host,active,"/api/v2/workflows/families?"+query({afterId:after,limit:PAGE}));if(!p||!active())return;families=reset?(p.items||[]):families.concat(p.items||[]);familyNext=p.nextAfterId??null;familiesBox.replaceChildren();familySelect.replaceChildren(new Option("创建新家族","__new__"));for(const f of families){const id=f.workflow_uuid;const row=node("div","cmv-lib-row");const familyButton=button(f.name||"未命名家族",()=>{family=id;version=null;void versionPage(true);});familyButton.dataset.cmvWorkflowFamily=id;row.append(familyButton);row.append(button("按此家族筛选图库",()=>{if(window.CMVClassic&&window.CMVClassic.filterWorkflow)window.CMVClassic.filterWorkflow(id,f.name||"工作流");}));familiesBox.append(row);familySelect.append(new Option(f.name||"未命名家族",id));}moreF.hidden=familyNext==null;msg.textContent="已读取 "+families.length+" 个家族。";}catch(e){if(active())msg.textContent="家族读取失败："+(e.message||e);}}
    async function versionPage(reset){if(!family)return;const after=reset?0:versionNext;if(!reset&&after==null)return;try{const p=await get(state,host,active,"/api/v2/workflows/versions?"+query({workflowUuid:family,afterId:after,limit:PAGE}));if(!p||!active())return;versions=reset?(p.items||[]):versions.concat(p.items||[]);versionNext=p.nextAfterId??null;versionsBox.replaceChildren();for(const v of versions)versionsBox.append(button("版本 "+(v.version_no??"?")+" · "+(v.source_format||"格式未知"),()=>{version=v.version_uuid;void capturePage(true);void versionDetail(version);}));moreV.hidden=versionNext==null;}catch(e){if(active())msg.textContent="版本读取失败："+(e.message||e);}}
    async function versionDetail(uuid){try{const d=await get(state,host,active,"/api/v2/workflows/detail?"+query({versionUuid:uuid}));if(!d||!active())return;detail.replaceChildren(node("h4","","工作流版本"),node("p","","格式："+(d.source_format||"未知")+"；保存图快照不证明执行。"));
      const exportBtn=button("导出所选版本原始捕获",async()=>{const p=await get(state,host,active,"/api/v2/workflows/captures?"+query({versionUuid:uuid,afterId:0,limit:1}));const c=p&&p.items&&p.items[0];if(!c)return;const x=await get(state,host,active,"/api/v2/workflows/export?"+query({versionUuid:uuid,captureUuid:c.capture_uuid}));if(!x||!active())return;const bytes=Uint8Array.from(atob(x.raw_payload),z=>z.charCodeAt(0));const a=document.createElement("a");a.href=URL.createObjectURL(new Blob([bytes],{type:"application/json"}));a.download="workflow-"+c.capture_uuid+".json";a.click();setTimeout(()=>URL.revokeObjectURL(a.href),3000);});detail.append(exportBtn);
    }catch(e){if(active())detail.replaceChildren(node("p","cmv-lib-error","版本读取失败："+(e.message||e)));}}
    async function capturePage(reset){if(!version)return;const after=reset?0:captureNext;if(!reset&&after==null)return;try{const p=await get(state,host,active,"/api/v2/workflows/captures?"+query({versionUuid:version,afterId:after,limit:PAGE}));if(!p||!active())return;captures=reset?(p.items||[]):captures.concat(p.items||[]);captureNext=p.nextAfterId??null;capturesBox.replaceChildren();for(const c of captures)capturesBox.append(button((c.captured_at||"捕获")+" · "+(c.identity_available?"身份可比较":"无身份记录"),()=>void captureDetail(c)));moreC.hidden=captureNext==null;}catch(e){if(active())msg.textContent="捕获读取失败："+(e.message||e);}}
    async function captureDetail(c){const uuid=c.capture_uuid;try{const results=await Promise.all([get(state,host,active,"/api/v2/workflows/capture-identity?"+query({captureUuid:uuid})).catch(()=>null),get(state,host,active,"/api/v2/workflows/decision?"+query({captureUuid:uuid}))]);if(!active())return;const ident=results[0],d=results[1],v=d&&d.decisionVersion||0;detail.replaceChildren(node("h4","","捕获身份与归属决定"),node("p","cmv-lib-note","家族归属是人工分类，不证明此工作流执行过。"));
      if(ident)detail.append(node("pre","cmv-lib-code",JSON.stringify({status:ident.status,coverage:ident.coverage,workflowUuid:ident.workflow_uuid,definitionUuid:ident.definition_uuid},null,2)));else detail.append(node("p","cmv-lib-note","捕获没有可比较的身份记录。"));
      const target=document.createElement("select");target.dataset.cmvLibField="workflow-decision-family";target.append(new Option("选择目标家族",""));for(const f of families)target.append(new Option(f.name||"未命名家族",f.workflow_uuid));
      const reason=document.createElement("input");reason.placeholder="填写确认或撤销理由";reason.dataset.cmvLibField="workflow-decision-reason";detail.append(node("p","cmv-lib-status","当前家族："+(d&&d.effectiveFamily&&d.effectiveFamily.name||"未知")+" · 决定版本 "+v),field("目标家族",target),field("理由",reason));
      const confirm=button("确认家族归属",()=>void decide("confirm",{captureUuid:uuid,targetWorkflowUuid:target.value,expectedDecisionVersion:v,reason:reason.value.trim()}));confirm.dataset.cmvLibAction="confirm-workflow-family";
      const revoke=button("撤销家族归属",()=>void decide("revoke",{captureUuid:uuid,expectedDecisionVersion:v,reason:reason.value.trim()}));revoke.dataset.cmvLibAction="revoke-workflow-family";detail.append(confirm,revoke);
      let saving=false;
      async function decide(action,payload){if(saving)return;if(!payload.reason||action==="confirm"&&!payload.targetWorkflowUuid){msg.textContent="请填写理由并选择目标家族。";return;}saving=true;for(const control of [confirm,revoke,target,reason])control.disabled=true;try{await post(state,host,active,"/api/v2/workflows/"+action,payload);await captureDetail(c);}catch(e){if(active())detail.append(node("p","cmv-lib-error","保存失败："+(e.message||e)));}finally{saving=false;for(const control of [confirm,revoke,target,reason])control.disabled=false;}}
    }catch(e){if(active())detail.replaceChildren(node("p","cmv-lib-error","读取决定失败："+(e.message||e)));}}
    moreF.hidden=moreV.hidden=moreC.hidden=true;await familyPage(true);
  }
  async function modelHub(host,state,active){const title=node("h3","","模型目录管理"),note=node("p","cmv-lib-note","登记目录不会自动确认模型身份。"),list=node("div","cmv-lib-list"),form=document.createElement("form"),path=document.createElement("input"),name=document.createElement("input"),status=node("p","cmv-lib-status","");path.placeholder="模型目录完整路径";path.required=true;name.value="Models";const b=node("button","cmv-lib-btn","登记目录");b.type="submit";form.append(field("目录",path),field("名称",name),b,status);host.append(title,note,list,form);
    async function refresh(){const r=await get(state,host,active,"/api/v2/models/roots");if(!r||!active())return;list.replaceChildren();for(const x of r.roots||[])list.append(node("p","cmv-lib-root",(x.name||"目录")+" · "+x.path));}
    form.addEventListener("submit",async e=>{e.preventDefault();b.disabled=true;try{await post(state,host,active,"/api/v2/models/roots/register",{path:path.value.trim(),name:name.value.trim()});status.textContent="目录已登记。";await refresh();}catch(err){status.textContent="登记失败："+(err.message||err);}finally{if(active())b.disabled=false;}});try{await refresh();}catch(e){if(active())setStatus(host,"目录读取失败："+(e.message||e),"error");}
  }
  function closeHub(){document.querySelector('.cmv-library-hub')?.cmvClose?.();}
  return {mount,cancel,openHub,closeHub};
})();
