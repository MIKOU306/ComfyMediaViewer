/* Classic data lifecycle panel. Mount its entry from the data settings page. */
"use strict";
window.CMVData = (() => {
  const esc = value => String(value == null ? "" : value).replace(/[&<>"']/g, c =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const controllers = new Set();
  let active = null;
  let loadSequence = 0;
  const absolute = value => /^(?:[A-Za-z]:\\|\\\\|\/)/.test(String(value || "").trim());
  const stateText = value => ({"waiting-for-exit":"等待退出程序",waiting:"等待处理",queued:"排队中",running:"处理中",copying:"复制资料中",preparing:"准备资料库中",reconciling:"核对资料中",publishing:"切换资料位置中",recovering:"恢复处理中",completed:"已完成",error:"遇到问题",failed:"未完成",idle:"尚未开始"}[value] || value || "状态未知");
  const taskLabel = job => job.originalSource || job.source || job.destination || job.candidate || job.bundle || "资料任务";
  const taskDate = job => job.completedAt || job.updatedAt || job.createdAt || job.updated_at || job.created_at || "";
  const optionsFor = (jobs, predicate, empty) => [{value:"",label:empty},...(Array.isArray(jobs)?jobs.filter(predicate):[]).map(job=>({value:job.jobId,label:`${taskLabel(job)} · ${stateText(job.state)}${taskDate(job)?` · ${taskDate(job)}`:""}`}))];

  async function request(url, options = {}) {
    const controller = new AbortController(); controllers.add(controller);
    try {
      const response = await fetch(url, {...options, signal:controller.signal, cache:"no-store"});
      let body;
      try { body = await response.json(); } catch (_) { body = {}; }
      if (!response.ok) throw new Error(body.error || `${url}（${response.status}）`);
      return body;
    } finally { controllers.delete(controller); }
  }
  const get = url => request(url);
  const post = (url, body) => request(url, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});

  function form(action, title, description, fields, button, disabled = false) {
    return `<form class="cmvd-card" data-cmvd-action="${esc(action)}">
      <h3>${esc(title)}</h3><p>${esc(description)}</p>
      ${fields.map(field=>`<label>${esc(field.label)}${field.options?`<select name="${esc(field.name)}" ${field.required===false?"":"required"}>${field.options.map(option=>`<option value="${esc(option.value)}">${esc(option.label)}</option>`).join("")}</select>`:`<input name="${esc(field.name)}" type="text" value="${esc(field.value||"")}" placeholder="${esc(field.placeholder||"")}" autocomplete="off" ${field.required===false?"":"required"}>`}</label>`).join("")}
      <button class="sm primary" type="submit" ${disabled?"disabled":""}>${esc(button)}</button>
    </form>`;
  }
  function jobList(label, items, note) {
    const rows=Array.isArray(items)?items:[];
    return `<section class="cmvd-jobs"><h3>${esc(label)}</h3><p>${esc(note)}</p>${rows.length
      ? `<ul>${rows.map(job=>`<li><b>${esc(stateText(job.state))}</b> · ${esc(job.type||"")} · ${esc(taskLabel(job))}${taskDate(job)?` · ${esc(taskDate(job))}`:""}
        <div class="cmvd-job-actions"><button type="button" data-cmvd-job="${esc(job.kind||"")}" data-job-id="${esc(job.jobId||"")}">读取任务详情</button>
        ${job.state==='completed'?'':`<button type="button" data-cmvd-run="${esc(job.kind||"")}" data-job-id="${esc(job.jobId||"")}">重新提交后台任务</button>`}</div>
        <details class="cmvd-tech"><summary>技术详情</summary><pre>${esc(JSON.stringify(job,null,2))}</pre></details></li>`).join("")}</ul>`
      : '<div class="cmvd-empty">当前接口没有返回待处理任务。</div>'}</section>`;
  }
  function locationText(current) {
    if (!current) return "正在读取当前数据位置…";
    return `当前模式：${current.storageMode||current.storage_mode||current.mode||"未知"}；数据目录：${current.dataDir||current.dir||"未返回"}；选择器：${current.selector||"未返回"}。`;
  }
  function cleanup(dialog) {
    if (!dialog || dialog.closed) return;
    dialog.closed=true;
    dialog.observer?.disconnect();
    for(const controller of controllers) controller.abort();
    controllers.clear();
    if(active===dialog) active=null;
  }
  function close(dialog) {
    if (!dialog || dialog.closed) return;
    dialog.closeOriginal(); cleanup(dialog);
  }

  async function load(dialog) {
    if(dialog.closed) return;
    const body=dialog.body, sequence=++loadSequence;
    body.innerHTML='<div class="cmvd-loading" role="status">读取当前数据位置与可恢复任务…</div>';
    const outcomes=await Promise.all([
      get('/api/v2/classic/data').then(value=>({key:'classic',value})).catch(error=>({key:'classic',error})),
      get('/api/sysinfo').then(value=>({key:'sysinfo',value})).catch(error=>({key:'sysinfo',error})),
      get('/api/v2/migration/downgrade-review').then(value=>({key:'downgrade',value})).catch(error=>({key:'downgrade',error})),
    ]);
    if(dialog.closed||sequence!==loadSequence) return;
    const data=Object.fromEntries(outcomes.map(item=>[item.key,item.value||null]));
    const errors=outcomes.filter(item=>item.error).map(item=>`${item.key}: ${item.error.message}`);
    dialog.data=data;
    const current=data.classic?.current||data.sysinfo?.datadir||null;
    const legacy=data.classic?.legacySource||{};
    const jobs=data.classic?.jobs||{};
    const preparation=Array.isArray(jobs.preparation)?jobs.preparation:[];
    const sourcePath=legacy.sourceDataDir||"";
    const defaultDestination=legacy.suggestedDestination||"";
    const review=data.downgrade||{};
    const blockers=Array.isArray(review.blockers)?review.blockers:[];
    const html=[];
    html.push(`<div class="cmvd-location">${esc(locationText(current))}</div>`);
    html.push(`<div class="cmvd-warning"><b>后台任务说明</b><br>提交后会自动安排后台任务，并等待本程序退出。只有点击“退出并执行”才会请求关闭程序；也可从系统托盘退出。完成状态不代表新进程已验证，重新打开后仍需核对数据位置与资料库状态。</div>`);
    if(errors.length) html.push(`<div class="cmvd-error" role="alert">部分状态暂时无法读取：<br>${errors.map(esc).join('<br>')}</div>`);
    html.push('<div class="cmvd-grid">');
    const canExternal=legacy.available===true;
    html.push(form('legacy-prepare','导入旧版数据并准备资料库',
      canExternal?'请先退出旧版程序，避免复制期间旧数据继续变化。旧版源目录只读，系统会把它复制为独立新副本并在副本上准备资料库。完成后选择转换后的独立副本，不会与当前资料库合并；原资料库和旧版源均保留。':(legacy.reason||'当前后端尚未提供外部旧数据目录的导入准备接口。'),
      [{name:'sourceDataDir',label:'旧版数据目录绝对路径',value:sourcePath,placeholder:'例如 C:\\Users\\…\\ComfyMediaViewer'},
       {name:'destination',label:'新准备目录（必须不存在）',value:defaultDestination,placeholder:'例如 D:\\CMV-Migration\\prepared'}],
      '提交离线准备任务',!canExternal));
    html.push(form('install','安装已核验的准备结果',
      '只安装已核验的准备任务。安装材料目录必须是新的独立目录，与旧源、准备目录和程序选择器分离。提交后退出程序，由后台任务完成并发布选择器；然后重新启动验证。',
      [{name:'preparationJobId',label:'已完成的准备任务',options:optionsFor(preparation,x=>x.state==='completed','请选择准备任务')},
       {name:'bundleDirectory',label:'安装材料目录（必须不存在）',placeholder:'绝对路径'}],
      '提交安装任务',!preparation.some(x=>x.state==='completed')));
    html.push(form('recovery-export','导出完整备份材料',
      '退出程序后由后台任务生成校验清单和完整保留副本。此操作不会切换当前数据目录。',
      [{name:'destination',label:'新的备份目录（必须不存在）',placeholder:'绝对路径'}],'提交完整备份任务'));
    html.push(form('recovery-restore','验证并恢复到独立目录',
      '备份材料必须存在且通过清单校验；恢复目标必须是全新目录，并与当前数据、备份材料及选择器分离。仅恢复到隔离副本，不自动启用。',
      [{name:'bundle',label:'备份材料目录',placeholder:'包含 recovery-export.json 的目录'},
       {name:'destination',label:'新的恢复目录（必须不存在）',placeholder:'绝对路径'}],'提交恢复任务'));
    const restoreJobs=(jobs.recovery||[]).filter(job=>job.action==='restore'&&job.state==='completed');
    html.push(form('select-restored','选择已验证的恢复资料库',
      '此步骤会先备份当前选择器，再把选择器切到恢复目录。提交后退出程序；重启后检查资料库和数据位置。',
      [{name:'restoreJobId',label:'已完成的恢复任务',options:optionsFor(restoreJobs,()=>true,'请选择恢复任务')},
       {name:'backupDirectory',label:'当前资料库选择器备份目录（必须不存在）',placeholder:'绝对路径'}],
      '提交资料库选择任务',!restoreJobs.length));
    html.push(form('relocation','搬迁当前资料库数据目录',
      '目标目录必须尚不存在，与当前数据目录和程序选择器分离，并且路径不包含链接/重解析点。提交后退出程序让后台任务复制、校验并切换；重新启动后再验证。',
      [{name:'destination',label:'新的搬迁目标目录',placeholder:'绝对路径'}],'提交搬迁任务',current?.storageMode!=='sqlite'&&current?.storage_mode!=='sqlite'&&current?.mode!=='sqlite'));
    html.push(`<section class="cmvd-card cmvd-review"><h3>降级前只读审阅</h3><p>${esc(review.message||'只导出传统格式可表达的组织数据，不会声称旧版可直接启动。')}</p>
      <div>可导出组织数据：<b>${review.organizationExportable===true?'可以':review.organizationExportable===false?'暂不可':'读取中'}</b></div>
      ${blockers.length?`<ul>${blockers.slice(0,30).map(x=>`<li>${esc(x.domain)} · ${esc(x.path)}：${esc(x.reason)}</li>`).join('')}</ul>`:''}
      ${review.requiresSeparateRecovery?`<p>仍需另行保留：${esc(review.requiresSeparateRecovery.join('、'))}。</p>`:''}
      ${review.notRepresentedInLegacy?`<p>传统格式无法表示：${esc(review.notRepresentedInLegacy.join('、'))}。</p>`:''}
      ${review.requiredPreservation?`<p>${esc(review.requiredPreservation)}</p>`:''}
    </section>`);
    html.push(form('downgrade-export','导出传统格式组织数据',
      '只生成收藏、标签、标记、分类等组织数据文件；保留完整 SQLite/归档材料并另行恢复其他域。导出目录不得覆盖现有文件。',
      [{name:'destination',label:'新的导出目录（必须不存在）',placeholder:'绝对路径'}],
      '导出组织数据',review.organizationExportable!==true));
    html.push('</div>');
    html.push(jobList('后台任务记录',[
      ...(jobs.installation||[]).map(job=>({...job,kind:'installation',type:'安装材料'})),
      ...(jobs.recovery||[]).map(job=>({...job,kind:'recovery',type:'备份/恢复'})),
      ...(jobs.recoverySelection||[]).map(job=>({...job,kind:'recoverySelection',type:'恢复库选择'})),
      ...(jobs.relocation||[]).map(job=>({...job,kind:'relocation',type:'搬迁'})),
      ...(!jobs.relocation?.length&&data.sysinfo?.datadir?.relocation?.job
        ? [{...data.sysinfo.datadir.relocation.job,kind:'relocation',type:'搬迁'}] : []),
      ...preparation.map(job=>({...job,kind:'preparation',type:'旧版准备'})),
    ],'提交后会自动安排后台任务并等待本程序退出。任务状态仅用于查看处理进度，不是运行时验收。关闭后重新打开本面板查看详情；需要重试时可手动重新提交。'));
    html.push('<div class="cmvd-result" aria-live="polite"></div><div class="cmvd-actions"><button type="button" data-cmvd-exit>退出并执行后台任务</button><button type="button" data-cmvd-refresh>刷新任务状态</button><button type="button" data-cmvd-close>关闭</button></div>');
    body.innerHTML=html.join('');
    bind(dialog);
  }

  function bind(dialog) {
    const body=dialog.body;
    body.querySelector('[data-cmvd-close]').onclick=()=>close(dialog);
    body.querySelector('[data-cmvd-exit]').onclick=()=>void exitForTasks(dialog);
    body.querySelector('[data-cmvd-refresh]').onclick=()=>load(dialog);
    body.querySelectorAll('[data-cmvd-job]').forEach(button=>button.onclick=()=>void inspectJob(dialog,button.dataset.cmvdJob,button.dataset.jobId));
    body.querySelectorAll('[data-cmvd-run]').forEach(button=>button.onclick=()=>void resumeJob(dialog,button.dataset.cmvdRun,button.dataset.jobId));
    body.querySelectorAll('form[data-cmvd-action]').forEach(formEl=>formEl.addEventListener('submit',event=>{
      event.preventDefault(); void submit(dialog,formEl);
    }));
  }
  function formValues(formEl) { return Object.fromEntries(new FormData(formEl).entries()); }
  function requireAbsolute(value,label) { if(!absolute(value)) throw new Error(`${label}必须填写绝对路径`); }
  async function submit(dialog, formEl) {
    if(dialog.closed) return;
    const action=formEl.dataset.cmvdAction, value=formValues(formEl), result=dialog.body.querySelector('.cmvd-result');
    const routes={
      'legacy-prepare':{
        url:'/api/v2/classic/data/legacy-prepare',
        body:()=>{requireAbsolute(value.sourceDataDir,'旧版数据目录');requireAbsolute(value.destination,'准备目录');
          if(!confirm('旧版数据源将保持不变，系统会创建独立副本并在副本上准备资料库。完成后选择转换后的独立副本，不与当前资料库合并；原资料库与旧版源均保留。继续？')) return null;
          return {sourceDataDir:value.sourceDataDir.trim(),destination:value.destination.trim()};}
      },
      install:{url:'/api/v2/classic/data/legacy-install',body:()=>{
        requireAbsolute(value.bundleDirectory,'安装材料目录');
        if(!confirm('将使用已核验的准备任务创建安装任务。提交后必须退出程序；安装会发布新数据目录选择，随后需要重新启动验证。继续？')) return null;
        return {preparationJobId:value.preparationJobId.trim(),bundleDirectory:value.bundleDirectory.trim()};
      }},
      'recovery-export':{url:'/api/v2/migration/recovery-job',body:()=>{
        requireAbsolute(value.destination,'备份目录');
        if(!confirm('创建完整数据备份任务。提交后请点击“退出并执行”；这不会切换当前数据目录。继续？')) return null;
        return {action:'export',destination:value.destination.trim()};
      }},
      'recovery-restore':{url:'/api/v2/migration/recovery-job',body:()=>{
        requireAbsolute(value.bundle,'备份材料目录');requireAbsolute(value.destination,'恢复目录');
        if(!confirm('将校验备份并恢复到一个新的隔离目录。当前资料库选择不会改变；必须退出程序后执行。继续？')) return null;
        return {action:'restore',bundle:value.bundle.trim(),destination:value.destination.trim()};
      }},
      'select-restored':{url:'/api/v2/migration/recovery-selection-job',body:()=>{
        requireAbsolute(value.backupDirectory,'选择器备份目录');
        if(!confirm('退出程序后，将先备份当前资料库选择，再选择已验证的恢复目录。重启后必须检查实际资料库。继续？')) return null;
        return {restoreJobId:value.restoreJobId.trim(),backupDirectory:value.backupDirectory.trim()};
      }},
      relocation:{url:'/api/v2/relocation/prepare',body:()=>{
        requireAbsolute(value.destination,'搬迁目标目录');
        if(!confirm('创建当前资料库搬迁任务。提交后请点击“退出并执行”，后台任务会校验并切换数据目录；随后需要重启验证。继续？')) return null;
        return {destination:value.destination.trim()};
      }},
      'downgrade-export':{url:'/api/v2/migration/export-organization',body:()=>{
        requireAbsolute(value.destination,'导出目录');
        if(!confirm('只导出旧版格式能表达的组织数据，不会生成可直接启动的旧版完整数据。完整 SQLite 与其余数据域必须保留。继续？')) return null;
        return {destination:value.destination.trim()};
      }},
    };
    const operation=routes[action]; if(!operation) return;
    try {
      const payload=operation.body(); if(payload===null) return;
      result.className='cmvd-result cmvd-pending'; result.textContent='正在提交明确选择的操作…';
      const response=await post(operation.url,payload);
      if(dialog.closed) return;
      const task=response.job||response;
      const nonRuntime=response.runtimeVerified===false||response.handoffRequired===true||response.authorityPublished===false||!!response.job;
      result.className='cmvd-result cmvd-success';
      result.textContent=action==='downgrade-export'
        ? `组织数据导出已完成（${response.scope||'范围未返回'}）。报告：${response.report||'请核对目标目录中的 export-manifest.json'}。这不是可直接启动旧版的完整资料库；继续保留完整 SQLite 和其余数据域。`
        : `已安排后台任务。当前状态：${stateText(task.state||'已创建')}；${nonRuntime?'这不是运行时验收。准备退出时点击“退出并执行”，重新启动后核对实际数据目录。':'请核对返回的导出清单与目标目录。'}`;
      const refresh=dialog.body.querySelector('[data-cmvd-refresh]'); if(refresh) await load(dialog);
      const resultAgain=dialog.body.querySelector('.cmvd-result');
      if(resultAgain){resultAgain.className='cmvd-result cmvd-success';resultAgain.textContent=action==='downgrade-export'
        ? `组织导出范围：${response.scope||'未返回'}；报告：${response.report||'请核对目标目录中的 export-manifest.json'}。请保留完整 SQLite 数据库与导出未覆盖的数据域。`
        : `${stateText(task.state||'操作已完成')}：已记录请求结果。${nonRuntime?'点击“退出并执行”后，重新启动验证；此状态不代表已验证。':'请保留导出报告，并继续保留完整数据库材料。'}`;}
    } catch(error) {
      if(dialog.closed||error.name==='AbortError') return;
      result.className='cmvd-result cmvd-error'; result.textContent=error.message||String(error);
    }
  }

  async function inspectJob(dialog,kind,jobId) {
    if(dialog.closed||!kind||!jobId) return;
    const result=dialog.body.querySelector('.cmvd-result');
    try {
      const detail=await get(`/api/v2/classic/data/job?kind=${encodeURIComponent(kind)}&jobId=${encodeURIComponent(jobId)}`);
      if(dialog.closed) return;
      const job=detail.job||detail;
      result.className='cmvd-result'; result.innerHTML=`<div>${esc(stateText(job.state))} · ${esc(job.type||kind)} · ${esc(taskLabel(job))}</div><details class="cmvd-tech"><summary>技术详情</summary><pre>${esc(JSON.stringify(detail,null,2))}</pre></details>`;
    } catch(error) {
      if(dialog.closed||error.name==='AbortError') return;
      result.className='cmvd-result cmvd-error'; result.textContent=error.message||String(error);
    }
  }
  async function resumeJob(dialog,kind,jobId) {
    if(dialog.closed||!kind||!jobId) return;
    if(!confirm('重新提交此后台任务。任务会等待本程序退出；重新启动并核对数据位置后才能确认完成。继续？')) return;
    const result=dialog.body.querySelector('.cmvd-result');
    try {
      const response=await post('/api/v2/classic/data/run',{kind,jobId});
      if(dialog.closed) return;
      result.className='cmvd-result cmvd-success';
      result.textContent=`已重新提交：${stateText(response.job?.state||response.state||'waiting-for-exit')}。此状态不是运行时验收；请点击“退出并执行”后重启验证。`;
      await load(dialog);
    } catch(error) {
      if(dialog.closed||error.name==='AbortError') return;
      result.className='cmvd-result cmvd-error'; result.textContent=error.message||String(error);
    }
  }

  async function exitForTasks(dialog) {
    if(dialog.closed) return;
    if(!confirm('现在退出本程序，让已提交的后台任务继续处理？请先保存其他工作。')) return;
    const result=dialog.body.querySelector('.cmvd-result');
    try {
      result.className='cmvd-result cmvd-pending'; result.textContent='正在请求退出并执行已提交的后台任务…';
      await post('/api/v2/classic/data/exit',{});
      result.className='cmvd-result cmvd-success';
      result.textContent='退出请求已提交。后台任务状态可在重新打开程序后查看；完成状态仍需重新启动核对数据位置和资料库。';
    } catch(error) {
      if(dialog.closed||error.name==='AbortError') return;
      result.className='cmvd-result cmvd-error'; result.textContent=error.message||String(error);
    }
  }

  function open() {
    if(active) { active.body.focus?.(); return active; }
    if(typeof bigDialog!=='function') throw new Error('数据管理弹窗尚未加载');
    const popup=bigDialog('数据迁移、备份与恢复',{wide:true});
    popup.body.classList.add('cmvd-body'); popup.body.tabIndex=-1;
    const dialog={...popup,closed:false,closeOriginal:popup.close,data:null}; active=dialog;
    dialog.observer=new MutationObserver(()=>{if(!dialog.mask.isConnected) cleanup(dialog);});
    dialog.observer.observe(document.body,{childList:true});
    void load(dialog);
    return dialog;
  }
  return {open};
})();
