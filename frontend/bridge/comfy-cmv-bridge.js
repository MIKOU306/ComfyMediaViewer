// ==UserScript==
// @name         ComfyMediaViewer 回传桥（拉取模式）
// @namespace    cmv.bridge
// @version      1.5.0
// @description  在【已经打开】的 ComfyUI 页面里接收 ComfyMediaViewer 送来的工作流，直接载入当前页面——不新开浏览器窗口，也不需要调试端口。
// @match        http://127.0.0.1:*/*
// @match        http://localhost:*/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==
//
// 一份文件，两种装载方式（上面那段 UserScript 头对普通 <script> 只是注释，反之亦然）：
//   A. ComfyUI 前端扩展 —— ComfyMediaViewer 界面上的「安装桥接脚本到 ComfyUI」会自动写到
//      ComfyUI/custom_nodes/ComfyMediaViewer-Bridge/web/，重启 ComfyUI 即生效（推荐）。
//   B. 用户脚本 —— 直接把本文件拖进 Tampermonkey / Violentmonkey 新建脚本。
//
// 原理：脚本跑在 ComfyUI 页面里，天生持有 window.app，于是
//   app.loadGraphData(工作流) 可以直接调用；它只额外做一件事——轮询 ComfyMediaViewer 取件。
// 之所以必须「页面内脚本」而不是外部进程：往已存在的页面注入 JS 只能走 CDP，
// 而浏览器自 136 起禁止用默认 profile 开 --remote-debugging-port（只能另起实例＝弹新窗口）。
//
// ★ v1.5.0：投递代次 —— 迟到的回执不再误伤新一轮投递（审计二批次·第 2 级 P0-9/P0-10）
//   服务端是「取走 → 挂起等回执」（in-flight），而同一件会被重投（nack / 超时兜底）。
//   改前 in-flight 只按 seq 记账、回执也只报 seq，于是两件事都错：
//   · **迟到回执抹掉新一轮**（P0-9）：页面取件超时（10s）后服务端重投，
//     此时上一轮那条 ack 才姗姗来迟 —— 它按 seq 把**新一轮**的 in-flight 记录弹掉，
//     件既不回队列、也不在 in-flight ⇒ 永久丢失（用户看到「已回传」，画布里什么都没有）。
//   · **抢走正在载入的件**（P0-10）：abort 兜底回收的判据是「同端口又来人 + 挂起超 12s」，
//     而「又来人」在载入很慢时并不代表上一次请求已结束 —— 载入是独立链路、不占取件循环，
//     循环照常发空轮询 ⇒ 服务端把正在载入的件判成 abort 丢掉、放回队列重投
//     ⇒ 同一个工作流进画布两遍。
//   两道修：
//   · 每次投递服务端给一个 **claim**（这一次投递的身份），回执原样带回；对不上就忽略。
//   · 取件请求带上 **busy**（页面正忙时服务端不跑 abort 兜底回收）。
//   兼容：老版脚本不带 claim/busy ⇒ 服务端走回旧判据，行为与改动前一致。
//
// ★ v1.4.0：放弃不再伪装成「成功」，重试不再刷屏（§6.7 遗留 P2-2 / P2-3）
//   四条都是 γ 批逐条复核过的问题（报告 §6.7 的 7-1 / 7-2）：
//   · **放弃时不再发 ack**。ack 在服务端是「已成功载入」的语义，于是「连续 3 次失败后放弃」
//     被系统记成「送达成功」：件销毁、服务端无记录、CMV 侧无从得知，用户以为工作流已经进画布了
//     （若 ComfyUI 的 Toast 组件还没就绪，notify 还会退化成 console.log ⇒ 连页面里都看不到）。
//     现在发 /api/pull/drop：件同样不再重投，但服务端会留一条账，CMV 下次推送时能看到
//     「上次有件被放弃了，可重新发送」。
//   · **重试之间有退避**：服务端按尝试次数给件打 not_before（第 2 次失败后等 4s）。
//     原先 nack 一律立刻插回队首 + 1.5s 轮询 ⇒ 3 次尝试挤在 3 秒内。
//   · **同一件的失败提示只发一次**（首条说原因、放弃时说结论）⇒ 不再 3 条红 toast 连发。
//   · **done 改存 sessionStorage**：F5 之后仍记得「这件已了结」。原先 done 是页面级内存对象，
//     刷新即空 —— 而「载入成功但 ack 丢了」时服务端 120s 后会把件重投，
//     刷新过的页面于是把同一个工作流**载入第二遍**（凭空多一张画布）。
//   · **上报 tabId**（存 sessionStorage：刷新不变、新标签页新号）：让 CMV 能知道同一端口上
//     到底开着几个 ComfyUI 页面（此前只知道「有页面」，不知道有几个）。
//   注意：本文件内容变了，已装到 ComfyUI 的旧版会被判为「需重装」——按界面提示重装一次即可。
//
// ★ v1.3.1：载入不再掐断心跳（P1-33）
//   原先取到件后是「busy 期间 await doLoad()」，而 doLoad 里 waitApp 最多等 30s；
//   这 30s 一个 /api/pull 都发不出去 ⇒ 服务端 BRIDGE_TTL(25s) 一到就把本页判成
//   「已关闭」⇒ CMV 的 bridge_pages() 为空，回落路径去**另开一个窗口**注入，
//   于是用户在两处各拿到一份工作流（原页面等完还会再载入一次）。
//   两道修：① 载入与取件循环解耦（apply 不再挂在 busy 上，循环照常跑＝心跳照常）；
//          ② waitApp 等待期间每 5s 主动补一次空轮询心跳（双保险，防「循环卡在别处」）。
//   另加 loading 集合挡并发载入（解耦后同一件可能被重投）。
//
// ★ v1.3.0：取件失败不再静默
//   两条实测出来的坑（P1-31 / P1-32）：
//   · /api/pull 返回错误 JSON 时（401 令牌不对、或 500 服务端异常），fetch **不会** reject，
//     r.json() 也照样解析成功，于是 apply() 看到 it.seq 为空直接返回 —— 无 toast、
//     无 console 日志；更糟的是那一拍还把 fails 重置了 ⇒ 轮询钉死在 1.5s 全速空转，
//     不退避也不重探。CMV 那边却早就弹过「已送进 ComfyUI」。
//     现在显式看 HTTP 状态码（和 200 但 ok:false 的业务失败），失败一律走退避 + 日志，
//     令牌类错误额外给一次明确提示（那个用户自己能修）。
//   · 单次取件有 10s 超时；abort 的那一刻响应体还没读到、**拿不到 seq**，
//     于是既不能 ack 也不能 nack ⇒ 件只能干等服务端 120s 的超时才被放回队列。
//     现在超时后主动发一次「按端口放弃」的请求，让件立刻回队列。
//
// ★ v1.2.0：两阶段取件（配套服务端 /api/pull 的 in-flight 挂起机制）
//   此前服务端「取走即出队」，而本脚本先推进去重序号再载入 —— 一旦载入失败
//   （ComfyUI 前端没起来 / 工作流损坏），这件就彻底消失，只留一行 console 日志。
//   现在：载入成功才回执 ack（服务端销毁该件），失败则 nack（放回队列重投，同件最多 3 次），
//   每次失败都明确弹 toast；页面中途被关掉则由服务端超时（120s）兜底放回队列。
//
// ★ v1.1.0：不弹任何确认框
//   上一版在「画布非空」时挂一块「载入 / 忽略」浮层，前提是「载入会覆盖当前工作流」。
//   真机实测（ComfyUI 0.34.5 / 前端 1.49.6）这个前提不成立：loadGraphData 不会覆盖你正在
//   编辑的那张，ComfyUI 会把新工作流**另开一张画布**承接，原工作流原样留着。
//   既然不会丢东西，就不该拦着用户确认 —— 取到件直接载入，全程无声，只在成功后弹一条 toast。
//   顺带也不必再操心原生 confirm 阻塞页面（会掐断下面的取件轮询＝心跳）那堆麻烦。
//
// 可调项（浏览器 console 里设置，存在 localStorage）：
//   cmv.bridge.cmv     ComfyMediaViewer 地址。留空 = 自动探测（默认行为）
//   cmv.bridge.token   局域网模式下 CMV 的访问令牌（本机模式留空）
//   cmv.bridge.off     设为 "1" 暂停桥接（重载页面恢复）
(function () {
  'use strict';
  if (window.__cmvBridge) return;      // 两种装载方式同时存在时，只跑一份
  if (localStorage.getItem('cmv.bridge.off') === '1') return;
  window.__cmvBridge = true;

  function ssGet(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } }
  function ssSet(k, v) { try { sessionStorage.setItem(k, v); } catch (e) {} }

  var CFG = {
    cmv: (localStorage.getItem('cmv.bridge.cmv') || '').replace(/\/+$/, ''),   // '' = 自动探测
    token: localStorage.getItem('cmv.bridge.token') || '',
    interval: 1500,                    // 轮询间隔（毫秒）
    maxBackoff: 30000                  // CMV 不在线时的退避上限
  };
  var PORT = location.port || '80';    // 本页 ComfyUI 端口：只取发给这个端口的件
  /* ★ v1.4.0（§6.7 P2-3）：标签页标识。
     PORT 是端口维度 —— 同一端口的两个标签页在服务端看来是一个页面（BRIDGE_SEEN 的 key 是端口）。
     这里给每个标签页一个随机号并随取件上报，服务端另记一份按标签页的账（BRIDGE_TABS），
     CMV 据此能知道「同一端口上开着几个页面」。
     存 sessionStorage 而不是内存：刷新页面后仍是同一个标签页，号不该变。 */
  var TAB = (function () {
    var t = ssGet('cmv.bridge.tab');
    if (!t) {
      t = Math.random().toString(36).slice(2, 8) + Date.now().toString(36).slice(-3);
      ssSet('cmv.bridge.tab', t);
    }
    return t;
  })();
  var TAG = '[cmv-bridge]';
  var CANDS = 12;                      // 探测范围：8765 起 12 个端口（CMV 被占用会自动后移）

  var busy = false, fails = 0, timer = 0, warned = false, toutWarned = false;
  // 去重与重试（V2.8.4 两阶段取件）：服务端取走件后会挂起等回执，失败会被放回队列重投，
  // 所以同一件可能被取到多次 —— 用 done 记住「已了结的（成功或已放弃）」，
  // 用 tries 限制失败重试次数。done 不设上限：键是整数、每条约几十字节，一个会话最多几千件。
  var tries = {}, MAX_TRIES = 3;
  /* ★ v1.4.0（§6.7 P2-3）：done 落 sessionStorage。
     原先它是页面级内存对象，F5 后清空 —— 而「载入成功但 ack 丢失」（CMV 正好重启/卡住）时，
     服务端会在 BRIDGE_ACK_TTL(120s) 后把该件重投，刷新过的页面 done 里没有它
     ⇒ **同一个工作流被载入第二遍**，凭空多一张画布。
     seq 是服务端全局唯一、且跨进程不撞号（基数取启动时刻）⇒ 按 seq 记是安全的。
     写入有界（只留最近 DONE_KEEP 个），避免一个长会话把 sessionStorage 撑爆。 */
  var DONE_KEEP = 200;
  var done = (function () {
    try {
      var raw = JSON.parse(ssGet('cmv.bridge.done') || '{}');
      return (raw && typeof raw === 'object') ? raw : {};
    } catch (e) { return {}; }
  })();
  function markDone(seq) {
    done[seq] = 1;
    try {
      var ks = Object.keys(done);
      if (ks.length > DONE_KEEP) {
        ks.sort(function (a, b) { return a - b; });
        for (var i = 0; i < ks.length - DONE_KEEP; i++) delete done[ks[i]];
      }
      ssSet('cmv.bridge.done', JSON.stringify(done));
    } catch (e) {}                        // 存不进去也不能影响载入流程
  }
  /* ★ P1-33：载入与取件解耦后，同一件可能在载入期间被重投 —— 用 loading 挡并发载入。
     与 done 不同，它在载入结束时就清除（done 是「已了结」的永久去重）。 */
  var loading = {};
  var PULL_TIMEOUT = 10000;              // 单次取件超时（ms）：CMV 挂起时不能让 busy 永久为真
  // P1-32：超时（abort）单独计数 —— 它跟「CMV 不在运行」不是一回事，不能混进 fails
  //        （混进去会一路涨到 6 ⇒ 误判页面离线并重探地址）。但它仍要参与退避，
  //        否则 CMV 真卡死时会退化成「每 10 秒一次」的持续空转。
  var timeouts = 0;
  /* ★ v1.4.0（§6.7 P2-2）：同一件的失败提示只发一次。
     原先每次失败都 notify ⇒ 连败 3 次时 3 条红 toast 挤在几秒内砸完，
     用户来不及读第一条（每条 life:4000），也来不及去修（缺节点 / 前端没起）。
     现在：首次失败说明原因，放弃时再给一条结论 —— 3 条变 2 条，且两条信息不重复。 */
  var notified = {};

  function log() {
    try { console.log.apply(console, [TAG].concat([].slice.call(arguments))); } catch (e) {}
  }

  function alive(base) {
    return fetch(base + '/api/bridge/status', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (j) { return (j && j.ok) ? base : null; })
      .catch(function () { return null; });
  }

  function scan(omit) {
    var cands = [], i;
    for (i = 0; i < CANDS; i++) {
      var base = 'http://127.0.0.1:' + (8765 + i);
      if (base !== omit) cands.push(base);
    }
    return Promise.all(cands.map(alive)).then(function (hits) {
      for (var k = 0; k < hits.length; k++) { if (hits[k]) return hits[k]; }
      return null;
    });
  }

  // CMV 端口可能因被占用而后移（8765 被占 → 8766 …），所以先试默认端口，
  // 不通再并发扫一遍。正常情况下只多一次请求。
  function probeHost() {
    if (CFG.cmv) {                       // 手动指定（localStorage）时直接采用
      window.__cmvBridgeUrl = CFG.cmv;
      return Promise.resolve(CFG.cmv);
    }
    var base = 'http://127.0.0.1:8765';
    return alive(base).then(function (hit) {
      return hit || scan(base);
    }).then(function (hit) {
      CFG.cmv = hit || base;
      window.__cmvBridgeUrl = CFG.cmv;
      return CFG.cmv;
    });
  }

  /* ★ v1.3.1（P1-33）：等待 ComfyUI 前端就绪时**不能掐断心跳**。
     原先这里最多等 30s，而它是在 tick() 的 busy=true 期间被 await 的：
     这 30s 里一个 /api/pull 都发不出去 ⇒ 服务端 BRIDGE_TTL(25s) 一到就把本页
     判成「已关闭」⇒ CMV 那边 bridge_pages() 为空，回落去**另开一个窗口**。
     用户于是拿到两份工作流（原页面等完还会再载入一次＝三份）。
     修法：等待期间每 HEARTBEAT_MS 主动发一次空轮询当心跳（它就是 /api/pull 本身，
     服务端对空轮询也计 bridge_touch），这样「等多久」都不影响 CMV 侧的存活判定。 */
  var HEARTBEAT_MS = 5000;      // < BRIDGE_TTL(25s)，留足 5 倍余量

  function waitApp(ms) {
    return new Promise(function (res) {
      var t0 = Date.now();
      var lastBeat = 0;
      (function poll() {
        if (window.app && window.app.loadGraphData) return res(true);
        var el = Date.now() - t0;
        if (el > ms) return res(false);
        if (el - lastBeat >= HEARTBEAT_MS) { lastBeat = el; beat(); }
        setTimeout(poll, 200);
      })();
    });
  }

  /* 空轮询＝心跳：服务端 /api/pull 对带 port 的请求一律 bridge_touch（见 server.py
     的 `/api/pull` 分支），取不到件就回 {"seq":0}。刻意不 await、不设 busy：
     它只是让 CMV 知道「这个页面还活着」，不能影响取件循环自己的节奏。 */
  function beat() {
    if (!CFG.cmv || !PORT) return;
    var u = url();
    try { fetch(u, { cache: 'no-store' }).catch(function () {}); } catch (e) {}
  }

  function notify(sev, msg) {
    try {
      var em = window.app && window.app.extensionManager;
      if (em && em.toast && em.toast.add) {
        em.toast.add({ severity: sev, summary: 'ComfyMediaViewer',
                       detail: msg, life: 4000 });
        return;
      }
    } catch (e) {}
    log(msg);
  }

  // ★ v1.4.0（§6.7 P2-2）：同一件只提示一次（见 notified 的说明）
  function notifyOnce(seq, sev, msg) {
    if (notified[seq]) return;
    notified[seq] = 1;
    notify(sev, msg);
  }

  /* ---------------- 载入 ---------------- */

  // 回执：ack = 已成功载入（服务端销毁该件）；nack = 载入失败（服务端放回队列重投）；
  //       drop = 已放弃（ε-7：服务端终结该件并留一条账）。
  // extra 是可选的附加查询参数（nack 带 try=、drop 带 reason= / name=）。
  // ★ v1.5.0（P0-9）：claim 是「这一次投递」的身份，原样带回 ——
  //    服务端据此认出迟到回执（对不上就忽略），否则迟到的 ack 会把服务端重投的
  //    新一轮记录抹掉、件永久丢失。服务端没给 claim（老版本 / 异常）时自动省略。
  function report(kind, seq, extra, claim) {
    var u = CFG.cmv + '/api/pull/' + kind + '?seq=' + seq;
    if (claim !== undefined && claim !== null && claim !== '') {
      u += '&claim=' + encodeURIComponent(claim);
    }
    if (extra) {
      for (var k in extra) {
        if (extra[k] !== undefined && extra[k] !== null && extra[k] !== '') {
          u += '&' + k + '=' + encodeURIComponent(extra[k]);
        }
      }
    }
    if (CFG.token) u += '&token=' + encodeURIComponent(CFG.token);
    return fetch(u, { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .catch(function () {});          // 回执发不出去也没关系：服务端超时会自己放回队列
  }

  // P1-32：取件超时后主动告诉服务端「这件我不要了」。
  // 服务端是「先出队、再回包」（两阶段取件），abort 后前端拿不到 seq ⇒ 既不能 ack
  // 也不能 nack，件只能干等 BRIDGE_ACK_TTL(120s)，而服务端并不知道这边已经放弃。
  // 这里补一次不带 seq 的「按端口放弃」请求，让它立刻回到队列。
  // ⚠️ 刻意**不 await**：CMV 卡死时这个请求自己也可能挂住，不能让它拖住取件循环
  //    （下一个 tick 会自己发起；服务端还有 BRIDGE_CLAIM_GRACE 那条不依赖前端的兜底）。
  function releaseClaim() {
    if (!CFG.cmv || !PORT) return;
    var u = CFG.cmv + '/api/pull/release?port=' + encodeURIComponent(PORT)
          + (CFG.token ? '&token=' + encodeURIComponent(CFG.token) : '');
    try { fetch(u, { cache: 'no-store' }).catch(function () {}); } catch (e) {}
  }

  function doLoad(it) {
    return waitApp(30000).then(function (ok) {
      if (!ok) {
        // 此前这里只 log 一句就放弃，用户完全无感知（工作流凭空消失）。
        notifyOnce(it.seq, 'error',
                   'ComfyUI 前端尚未就绪，稍后自动重试：' + (it.name || '工作流'));
        log('ComfyUI 前端未就绪 #' + it.seq);
        return false;
      }
      // clean=true：跳过前端自带的「未保存改动」询问。这里安全——实测 loadGraphData 会另开
      // 一张画布承接，已有的工作流不会被替换掉，所以不需要（也没有）任何覆盖确认。
      return Promise.resolve(window.app.loadGraphData(it.workflow, true)).then(function () {
        notify('success', '已载入：' + (it.name || '工作流'));
        log('已载入 #' + it.seq);
        return true;
      });
    }).catch(function (e) {
      log('载入失败 #' + it.seq, e && e.message);
      notifyOnce(it.seq, 'error', '载入失败：' + ((e && e.message) || e));
      return false;
    });
  }

  // 取到件直接载入。服务端是「两阶段取件」（V2.8.4）：取走后挂在 in-flight，
  // 只有回执 ack 才销毁，nack 或超时都会回到队列 —— 所以载入失败不再是静默丢失。
  function apply(it) {
    if (!it || !it.seq) return Promise.resolve();
    var seq = it.seq;
    // ★ v1.5.0（P0-9）：本轮的投递代次。done 命中时这件已经了结过（多半是服务端重投的
    //    同一件），回执仍要带上它自己的 claim，但服务端那边这一轮的记录早已换了 —— 无所谓：
    //    claim 对不上会被忽略，件早已销毁，效果与「重复确认」一致。
    var claim = it.claim;
    if (done[seq]) return report('ack', seq, null, claim);   // 已了结过的重复投递：直接确认掉，不重载
    /* ★ P1-33：载入已与取件循环解耦（不再是 busy 期间 await），于是「同一件」在
       上一轮还在等 ComfyUI 前端时就可能被服务端重投一次 —— 必须挡住并发载入，
       否则会真的载入两遍。loading 在载入结束（成功/失败/放弃）后清除。 */
    if (loading[seq]) { log('#' + seq + ' 正在载入中，跳过这次重复投递'); return Promise.resolve(); }
    loading[seq] = 1;
    tries[seq] = (tries[seq] || 0) + 1;
    log('取到 #' + seq, it.name || '', '（第 ' + tries[seq] + ' 次尝试）');
    return doLoad(it).then(function (ok) {
      if (ok) {
        markDone(seq);
        return report('ack', seq, null, claim);
      }
      if (tries[seq] >= MAX_TRIES) {             // 连着失败这么多轮：放弃并明确告知
        notify('error', '已放弃载入（连续失败 ' + tries[seq] + ' 次）：'
               + (it.name || '工作流') + '。可在 ComfyMediaViewer 里重新发送');
        /* ★ v1.4.0（§6.7 P2-2）：这里原先是 report('ack') —— 而 ack 在服务端是
           「已成功载入」的语义，于是「放弃」被记成「送达成功」：件销毁、无记录、
           CMV 侧完全不知道，用户以为工作流已经进画布了。
           现在发 drop：件同样终结（不再重投），但服务端会留一条账，
           由 /api/bridge/status 交给 CMV ⇒ 用户下次推送时能看到「有件被放弃了」。 */
        markDone(seq);                           // 先记住「已了结」：万一 drop 请求没送到，
                                                 // 服务端 120s 后重投时也不会再试一遍（会被直接 ack）
        return report('drop', seq, { reason: 'max-tries', name: it.name || '' }, claim);
      }
      // 放回队列，**带退避**后重投（服务端按 try 给件打 not_before）
      return report('nack', seq, { try: tries[seq] }, claim);
    }).then(function (r) {
      delete loading[seq];
      return r;
    }, function (e) {
      delete loading[seq];                       // 异常也要解锁，否则这件永远不能再载入
      throw e;
    });
  }

  function url() {
    // ★ v1.5.0（P0-10）：busy=1 告诉服务端「本页正忙 / 正在载入」，此时不要跑
    //    abort 兜底回收 —— 载入是独立链路、不占取件循环，循环照常发空轮询；
    //    旧判据（同端口又来人 + 挂起超 12s）会把正在载入的件抢走重投 ⇒ 进画布两遍。
    var b = (busy || loadingBusy()) ? '&busy=1' : '';
    return CFG.cmv + '/api/pull?port=' + encodeURIComponent(PORT)
         + '&tab=' + encodeURIComponent(TAB) + b
         + (CFG.token ? '&token=' + encodeURIComponent(CFG.token) : '');
  }

  // 有没有件正在载入（apply 里挂上的 loading 标记）。比 busy 更准：tick 的 busy
  // 只覆盖「取件请求在飞」，而载入是可以跨很多轮取件的长过程。
  function loadingBusy() {
    for (var k in loading) { if (loading[k]) return true; }
    return false;
  }

  function tick() {
    if (busy) return;
    busy = true;
    // 没有超时的话，CMV 卡死/断点时这个请求永不返回：busy 一直为真、心跳停摆、投递彻底卡死
    var ac = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    var timedOut = false;
    var to = ac ? setTimeout(function () { timedOut = true; ac.abort(); }, PULL_TIMEOUT) : 0;
    var opt = { cache: 'no-store' };
    if (ac) opt.signal = ac.signal;
    fetch(url(), opt)
      .then(function (r) {
        // P1-31：fetch 不因状态码 reject —— 401/500 时 r.json() 照样能解析出
        // {"error":"..."}，apply() 看到 it.seq 为空直接返回 ⇒ 静默空转。必须显式看状态码。
        if (!r.ok) {
          var he = new Error('HTTP ' + r.status);
          he.httpStatus = r.status;
          throw he;
        }
        return r.json();
      })
      .then(function (it) {
        if (it && it.ok === false) {                 // 200 但业务失败：同一种静默，一并堵掉
          var be = new Error(it.error || 'bridge error');
          be.httpStatus = 200;
          throw be;
        }
        fails = 0; warned = false; timeouts = 0; toutWarned = false;
        /* ★ P1-33 的关键：载入是**另一条独立链路**，不能挂在取件循环的 busy 上。
           apply() 里的 doLoad() 可能要等 ComfyUI 前端就绪（最长 30s），
           若在 busy 期间 await 它，这 30s 就一个 /api/pull 都发不出去 ⇒
           服务端 BRIDGE_TTL(25s) 判定本页已死 ⇒ CMV 另开窗口，用户拿到两份工作流。
           这里**先把 busy 释放掉**（下面的 .then 收尾会立刻续上下一次 tick），
           apply() 自己在后台跑；用 loading 序号保证同一件不会被并发载入两次。 */
        apply(it);
      })
      .catch(function (e) {
        /* ⚠️ 超时必须与「网络不通」分开：abort 抛 AbortError，说明 CMV 还活着但不回话，
           把它算进 fails 会一路涨到 6 ⇒ 误判离线、把地址重探一遍（徒劳）。 */
        if (timedOut) {
          timeouts = Math.min(timeouts + 1, 3);
          log('取件超时（' + PULL_TIMEOUT + 'ms），已请服务端放回队列：' + CFG.cmv);
          if (!toutWarned) {
            toutWarned = true;
            notify('warn', 'ComfyMediaViewer 响应超时：工作流可能还没送达，正在自动重试');
          }
          releaseClaim();
          return;
        }
        fails = Math.min(fails + 1, 6);
        if (!warned) {
          warned = true;
          var st = e && e.httpStatus;
          if (st === 401 || st === 403) {
            // 令牌问题：用户自己能修（localStorage 的 cmv.bridge.token）—— 得说出来
            notify('error', 'ComfyMediaViewer 拒绝了取件请求（HTTP ' + st + '）：请检查桥接令牌');
            log('取件被拒 HTTP ' + st + '（检查 localStorage 的 cmv.bridge.token）');
          } else {
            log('取件失败（' + CFG.cmv + ' 是否在运行？）', e && e.message,
                st ? ('HTTP ' + st) : '');
          }
        }
        if (fails >= 6) { CFG.cmv = ''; probeHost(); }   // 地址可能变了，重探一次
      })
      .then(function () {
        if (to) clearTimeout(to);          // 成败都要收掉：否则已完成请求仍可能被 abort 打扰
        busy = false;
        /* 退避：超时也计入（timeouts 已在上面封顶 3 ⇒ 1.5s→3s→6s→12s）。
           超时通常一轮就恢复（服务端已把件放回），不必像网络不通那样涨到 30s。 */
        var n = fails ? Math.min(fails, 6) : timeouts;
        var wait = n ? Math.min(CFG.interval * Math.pow(2, n), CFG.maxBackoff) : CFG.interval;
        timer = setTimeout(tick, wait);
      });
  }

  // 后台标签的定时器会被浏览器降频，切回来时立刻补一次，避免「要等一会儿才收到」
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && !busy) {
      clearTimeout(timer);
      tick();
    }
  });

  probeHost().then(function () {
    log('已就绪：ComfyUI :' + PORT + ' ← ComfyMediaViewer ' + CFG.cmv
        + '（标签页 ' + TAB + '）');
    setTimeout(tick, 300);
  });
})();
