/* Loading feedback belongs to a view generation, never to the global scan worker. */
"use strict";
const CMVLoad = (() => {
  let current = null, timer = null;
  const panel = document.querySelector('#cmvLoad');
  const bar = document.querySelector('#cmvLoadBar');
  const text = document.querySelector('#cmvLoadText');
  const count = document.querySelector('#cmvLoadCount');
  const elapsed = document.querySelector('#cmvLoadElapsed');
  const grid = document.querySelector('#grid');
  function finish(token) {
    if (!current || current.token !== token) return;
    clearInterval(timer); timer = null; current = null;
    panel.hidden = true; grid.setAttribute('aria-busy', 'false');
  }
  function tick() {
    if (!current) return;
    if (current.token !== viewGen) { finish(current.token); return; }
    elapsed.textContent = '已用 ' + Math.floor((performance.now() - current.started) / 1000) + ' 秒';
  }
  function begin(token, path) {
    clearInterval(timer);
    current = {token, started:performance.now(), total:null};
    panel.hidden = false; panel.dataset.token = String(token);
    panel.dataset.path = path || ''; panel.dataset.stage = 'directory';
    text.textContent = '正在读取目录信息…'; count.textContent = '';
    bar.removeAttribute('value'); bar.removeAttribute('aria-valuenow');
    bar.setAttribute('aria-valuetext', '正在读取目录信息');
    grid.setAttribute('aria-busy', 'true'); tick();
    // 300ms ⇒ 常驻定时器预算门禁（tests/perf_timerbudget_check.py A2）要求 ≥300ms。
    // 此处只刷新「已用 N 秒」的整数文案，250 vs 300 用户完全无感；
    // 且本定时器随 finish() 一起 clearInterval，不是常驻轮询。
    timer = setInterval(tick, 300);
  }
  function phase(token, label, stage, job, total) {
    if (!current || current.token !== token || token !== viewGen) return;
    panel.dataset.stage = stage;
    text.textContent = label;
    if (Number.isSafeInteger(total) && total > 0) current.total = total;
    if (job && Number.isSafeInteger(job.observed) && job.observed >= 0) {
      const discovered = job.observed, skipped = job.ownedElsewhere || 0;
      panel.dataset.observed = String(discovered);
      count.textContent = '已登记 ' + discovered + ' 项'
        + (current.total ? ' · 目录约 ' + current.total + ' 项' : '')
        + (skipped ? ' · 另有 ' + skipped + ' 项属于其他图库' : '');
      if (current.total) {
        // The directory fingerprint count is an estimate, not proof of scan completion.
        const fraction = job.state === 'complete' ? 1 : Math.min(.99, (discovered + skipped) / current.total);
        bar.max = 1; bar.value = fraction;
        bar.setAttribute('aria-valuetext', count.textContent + '，' + (job.state === 'complete' ? '扫描完成' : '扫描尚未完成'));
      } else {
        bar.removeAttribute('value'); bar.setAttribute('aria-valuetext', count.textContent);
      }
    } else {
      count.textContent = ''; delete panel.dataset.observed;
      bar.removeAttribute('value'); bar.setAttribute('aria-valuetext', label);
    }
    tick();
  }
  return {begin, phase, finish, cancel:() => { if (current) finish(current.token); }};
})();
