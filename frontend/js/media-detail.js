/* On-demand archived metadata detail for the classic lightbox. */
"use strict";
window.CMVMediaDetail = (() => {
  const esc = value => String(value == null ? "" : value).replace(/[&<>"']/g, c =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const text = value => value == null || value === "" ? "—" : String(value);
  const row = (label, value) => `<div class="mrow"><dt>${esc(label)}</dt><dd>${esc(text(value))}</dd></div>`;
  const obj = value => value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const stateLabel = value => ({complete:"已完整采集",captured:"已采集",partial:"部分采集",no_metadata:"没有元数据",empty:"无可采集内容",error:"采集失败",unsupported:"格式不支持",too_large:"内容过大"}[value] || text(value));
  const coverageLabel = value => ({complete:"完整",partial:"部分",empty:"无",none:"无"}[value] || text(value));

  function markup(j) {
    const id = j && j.mediaId, rev = j && j.revisionId;
    return `<section class="mcard" id="cmvArchiveRecord" data-status="idle"><details class="mv-archive-detail" data-media-id="${esc(id||"")}" data-revision-id="${esc(rev||"")}">
      <summary class="k mh">元数据记录</summary><div class="mv-archive-body" data-state="idle"><div class="row mv-archive-state">展开后读取归档记录</div></div></details></section>`;
  }

  function render(detail, expected) {
    const a = detail.metadataArchive;
    let html = `<div class="mv-archive-summary"><dl class="mdl">`
      + row("文件版本", detail.historical ? "历史文件版本" : "当前文件版本")
      + row("归档状态", a ? stateLabel(a.state) : "没有归档记录")
      + row("覆盖范围", a ? coverageLabel(a.coverage) : "无")
      + row("来源格式", a ? a.source_format : "无")
      + row("采集时间", a ? a.captured_at : "无")
      + row("采集器版本", a ? a.collector_version : "无") + `</dl></div>`;
    if (expected.snapshotId && (!a || a.uuid !== expected.snapshotId)) throw new Error("归档记录已变化，请重新打开这张图片后重试");
    if (!a) return html + `<div class="row">此文件版本没有归档记录；原图可能没有可采集的生成信息。</div>`;
    const result = obj(a.result), generation = detail.generation;
    if (a.coverage === "partial") html += '<div class="row">元数据只采集了部分内容。</div>';
    if (generation && generation.status === "successful" && generation.result) {
      const g = obj(generation.result), steps = Array.isArray(g.steps) ? g.steps : [];
      if (generation.revisionUUID !== expected.revisionId) throw new Error("生成参数记录绑定了另一个文件版本");
      if (g.coverage === "partial") html += `<div class="row">生成参数解析不完整，以下仅显示已识别的阶段和提示词。</div>`;
      if (g.coverage === "empty") html += `<div class="row">解析器没有识别到可展示的生成阶段。</div>`;
      html += `<section class="mv-generation"><div class="k mh">生成阶段</div>`;
      if (steps.length) html += `<div class="mv-generation-steps">${steps.map((s,i)=>{
        s=obj(s);
        const seed = s.seed != null ? String(s.seed) : "";
        const label = s.classType || s.class_type || `阶段 ${i+1}`;
        let h = `<article class="mv-generation-step"><div class="mv-generation-title">${esc(i+1)}. ${esc(label)}</div>`;
        if (seed) h += `<div class="mv-generation-seed">Seed：<span>${esc(seed)}</span></div>`;
        const params = [
          ["采样步数",s.steps], ["CFG",s.cfg], ["采样器",s.sampler], ["调度器",s.scheduler]
        ].filter(([,v])=>v != null);
        if (params.length) h += `<dl class="mdl">${params.map(([k,v])=>row(k,v)).join("")}</dl>`;
        const promptRows = Array.isArray(g.prompts) ? g.prompts : [];
        for (const role of ["positive","negative"]) {
          const prompts = promptRows.filter(p=>p && p.stepId === s.nodeId && p.role === role).map(p=>p.text);
          if (prompts.length) h += `<div class="mv-generation-prompts"><div class="k">${role === "positive" ? "正向提示词" : "负向提示词"}</div>${prompts.map((p,stage)=>`<details><summary>阶段 ${stage+1}</summary><pre>${esc(p)}</pre></details>`).join("")}</div>`;
        }
        return h + `</article>`;
      }).join("")}</div>`;
      const warnings = Array.isArray(g.warnings) ? g.warnings : [];
      if (warnings.length) html += `<div class="row mv-generation-warnings">解析说明：${warnings.map(esc).join("；")}</div>`;
      html += `</section>`;
    } else {
      const coverage = result.capture_scope || a.coverage || "未知";
      html += `<div class="row">${generation && generation.status === "failed" ? "生成参数解析未成功" : "没有可用的生成参数记录"}（归档范围：${esc(coverageLabel(coverage))}）。原始数据与旧配方仍可查看。</div>`;
    }
    return html;
  }

  const active = new WeakMap();
  function cancel(container) {
    const state = container && active.get(container);
    if (state) {
      state.generation++;
      if (state.controller) state.controller.abort();
      state.controller = null;
      if (state.observer) state.observer.disconnect();
    }
  }
  function mount(container, j, isCurrent) {
    if (!container) return;
    cancel(container);
    container.querySelector("#cmvArchiveRecord")?.remove();
    container.insertAdjacentHTML("beforeend", markup(j));
    const details = container.querySelector(".mv-archive-detail");
    if (!details) return;
    const record = container.querySelector("#cmvArchiveRecord");
    const body = details.querySelector(".mv-archive-body");
    const mediaId = j && j.mediaId, revisionId = j && j.revisionId;
    const expected = {snapshotId: j && j.metadataArchive && j.metadataArchive.snapshotId,
      revisionId};
    const state = {controller:null, generation:0, observer:null};
    active.set(container, state);
    const lightbox = document.getElementById("lb");
    if (lightbox && window.MutationObserver) {
      const observer = new MutationObserver(() => {
        if (!lightbox.classList.contains("on")) { cancel(container); observer.disconnect(); }
      });
      state.observer = observer;
      observer.observe(lightbox, {attributes:true, attributeFilter:["class"]});
    }
    let requested = false;
    details.addEventListener("toggle", async () => {
      if (!details.open) {
        state.generation++;
        if (state.controller) { state.controller.abort(); requested = false; }
        state.controller = null;
        return;
      }
      if (requested) return;
      if (!mediaId || !revisionId) {
        requested = true;
        record.dataset.status = "unavailable";
        body.dataset.state = "unavailable";
        body.innerHTML = `<div class="row">${typeof CMV3!=="undefined" && CMV3.info?.storageMode==="legacy"
          ? "当前使用旧版资料，迁移后可读取媒体版本归档。" : "当前是单图临时文件，没有图库身份，因此未读取归档记录。"}</div>`;
        return;
      }
      requested = true;
      const mine = ++state.generation;
      state.controller = new AbortController();
      record.dataset.status = "loading";
      body.dataset.state = "loading";
      body.innerHTML = `<div class="row mv-archive-state">正在读取元数据记录…</div>`;
      try {
        const url = `/api/v2/media/detail?mediaId=${encodeURIComponent(mediaId)}&revisionId=${encodeURIComponent(revisionId)}`;
        const response = await fetch(url, {signal: state.controller.signal, cache:"no-store"});
        let detail;
        try { detail = await response.json(); } catch (_) { throw new Error("服务器返回了无法识别的数据"); }
        if (!response.ok) throw new Error(detail.error || `读取失败（${response.status}）`);
        if (mine !== state.generation || !details.open || !isCurrent()) return;
        if (detail.mediaId !== mediaId || detail.revisionId !== revisionId) throw new Error("返回的媒体身份或 revision 与当前选择不一致");
        body.innerHTML = render(detail, expected);
        record.dataset.status = "ready";
        body.dataset.state = "ready";
        state.controller = null;
      } catch (error) {
        if (mine !== state.generation || !details.open || (error && error.name === "AbortError") || !isCurrent()) return;
        state.controller = null;
        record.dataset.status = "error";
        body.dataset.state = "error";
        body.innerHTML = `<div class="row err">元数据记录读取失败：${esc(error.message || error)}</div><button type="button" data-cmv-detail-retry>重试</button>`;
        body.querySelector("[data-cmv-detail-retry]").addEventListener("click", () => {
          requested = false;
          details.open = false;
          requestAnimationFrame(() => { if (details.isConnected) details.open = true; });
        });
      }
    });
  }
  return {mount, cancel};
})();
