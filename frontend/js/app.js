/* ComfyMediaViewer · js/app.js（2026-09-09 从 index.html 拆出，加载顺序与原文一致） */
"use strict";
/* ---------------- C-P4（批次 C，2026-10-06）：全站共用的 rAF 节流器 ----------------
 * 为什么需要：本项目有 5 处 window resize 监听 —— 本文档的顶栏实高上报 /
 * columns.js 的响应式收列（applyResponsiveCollapse）/ nav.js 关右键菜单 /
 * settings.js 关窄屏抽屉与分页条重算。此前**全部无节流** ⇒ 拖窗口边界时每秒 60+ 次；
 * 其中 columns.js 那个最贵：getComputedStyle(documentElement) 强制样式重算 +
 * 向 documentElement 连写 5 次 classList。
 * 做法：rAF 合帧（一帧最多执行一次）+ setTimeout 兜底 —— rAF 在页面不可见时不触发，
 * headless 门禁会挂住，所以必须留兜底（与 columns.js 既有的 rAF+setTimeout 同源）。
 * 挂在 window 上是为了让后加载的 nav/settings/columns 复用，**不引任何库**。
 * useArgs：需要把事件参数透传给 fn 时传 true。
 */
window.mvThrottleRaf = function(fn, useArgs){
  let queued = 0, lastArgs = null;
  return function(){
    lastArgs = useArgs ? arguments : null;
    if(queued) return;
    queued = 1;
    const run = () => {
      /* ⚠️ rAF 与兜底 setTimeout 都会到点 —— 用状态机保证只执行一次，
         否则每次 resize 都会白跑第二遍（合成期白做一次样式重算）。 */
      if(queued !== 1) return;
      queued = 0;
      if(lastArgs) fn.apply(this, lastArgs); else fn();
    };
    requestAnimationFrame(run); setTimeout(run, 60);
  };
};
/* ---------------- 桌面壳融合（V2.7） ----------------
 * 两种运行形态：
 *   A) 被 Tauri 桌面壳以 iframe 内嵌（window.self !== window.top）
 *      —— 壳页不再单独占一条标题栏，三个窗口按钮直接悬浮在「本级顶栏」右端。
 *         本文件负责：勾出 body.in-shell（给顶栏右侧让位）、把顶栏空白区的拖拽/双击
 *         经 postMessage 转发给壳页执行，并上报顶栏实高让按钮高度对齐。
 *   B) 直接作为 Tauri 顶层页运行（历史形态）—— 仍保留 #tauri-bar 独立标题栏。
 * 浏览器里两者都不成立：__TAURI__ 不存在且不是 iframe，跳过，UI 与网页版一致。
 */
(function setupShellBridge(){
  if (window.self === window.top) return;              // 非内嵌：交给下面的历史标题栏分支
  document.body.classList.add("in-shell");

  const bar = document.getElementById("top");
  /* 贴窗口顶边、横跨整宽的横栏（同一时刻只有一条真正可见，但都接上，
     大图/对比模式才拖得动窗口）：网格 #top / 大图 #lb-top / 并排对比 #cmpBar */
  const bars = ["top", "lb-top", "cmpBar"]
    .map(id => document.getElementById(id)).filter(Boolean);
  const toShell = (t, extra) => {
    try { parent.postMessage(Object.assign({ mv: t }, extra || {}), "*"); } catch (_) {}
  };

  const throttleRaf = (fn, useArgs) => (window.mvThrottleRaf || (f => f))(fn, useArgs);
  // 上报顶栏实高：壳页据此把三个窗口按钮对齐到同一行（字号/视口变化时会跟着变）
  const report = () => { if (bar) toShell("bar-h", { h: bar.getBoundingClientRect().height }); };
  report();
  window.addEventListener("resize", throttleRaf(report));   /* C-P4：rAF 节流 */
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(report).catch(() => {});
  // 左侧栏收起/展开会改变顶栏内元素宽度，高度也可能随媒体查询变化
  setTimeout(report, 300);

  if (!bars.length) return;
  // 顶栏里这些是「要用」的控件：点它们不拖窗口
  const UI = "button,input,select,textarea,a,label,.seg,.rs-wrap,.pgjump,[contenteditable]";
  const onUI = e => !!(e.target && e.target.closest && e.target.closest(UI));

  bars.forEach(b => {
    b.addEventListener("mousedown", e => {
      if (e.button !== 0 || onUI(e)) return;
      toShell("drag");                                 // 空白处按下 = 拖动窗口
    });
    b.addEventListener("dblclick", e => {
      if (onUI(e)) return;
      toShell("max");                                  // 空白处双击 = 最大化 / 还原
    });
  });
  toShell("shell-ready");
})();

/* ---------------- Tauri 自定义标题栏（历史形态：本页直接作为 Tauri 顶层页）----------------
 * 勾出 #tauri-bar、把 body 标记为 is-tauri、绑定窗口按钮。
 */
(function setupTauriTitlebar(){
  if (window.self !== window.top) return;   // 桌面壳 iframe 内：窗口按钮由壳页负责，此处不显示独立标题栏
  const T = window.__TAURI__;
  if (!T) return;
  const bar = document.getElementById('tauri-bar');
  if (!bar) return;
  bar.hidden = false;
  document.body.classList.add('is-tauri');

  // 取当前窗口句柄：兼容不同 Tauri 2.x 导出路径（window / webviewWindow / getCurrent）
  let win = null;
  try {
    const wm = T.window || T.webviewWindow || (T.core && T.core.window);
    if (wm) {
      win = wm.getCurrentWindow ? wm.getCurrentWindow()
          : (wm.getCurrentWebviewWindow ? wm.getCurrentWebviewWindow()
          : (wm.getCurrent ? wm.getCurrent() : null));
    }
  } catch (e) { console.error('[tauri-bar] getWindow error', e); }
  if (!win) {
    console.warn("[tauri-bar] Unable to get the Tauri window instance; using HTML drag fallback.");
    bar.setAttribute('data-tauri-drag-region', '');   // 兜底：至少整条 bar 可拖
    return;
  }

  const isMax = () => { try { return win.isMaximized(); } catch { return Promise.resolve(false); } };

  // 拖拽：整个顶栏（按钮除外）按下即拖动。改用 JS 显式调用，不依赖 HTML data-tauri-drag-region
  if (typeof win.startDragging === 'function') {
    bar.addEventListener('mousedown', (e) => {
      if (e.button !== 0) return;
      if (e.target.closest('.tb-btn')) return;        // 按钮不触发拖动
      try { win.startDragging(); } catch (err) { console.error('[tauri-bar] drag', err); }
    });
  } else {
    bar.setAttribute('data-tauri-drag-region', '');   // 极旧版本兜底
  }

  // 双击顶栏（非按钮）最大化 / 还原
  bar.addEventListener('dblclick', (e) => {
    if (e.target.closest('.tb-btn')) return;
    (async () => { try { (await isMax()) ? await win.unmaximize() : await win.maximize(); } catch (_) {} })();
  });

  // 三个窗口按钮
  bar.querySelectorAll('.tb-btn').forEach(btn => {
    const act = btn.dataset.act;
    btn.addEventListener('mousedown', e => e.stopPropagation());
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      try {
        if (act === 'minimize') await win.minimize();
        else if (act === 'maximize') { (await isMax()) ? await win.unmaximize() : await win.maximize(); }
        else if (act === 'close') await win.close();
      } catch (err) { console.error('[tauri-bar]', act, err); }
    });
  });

  // 同步最大化按钮标题（最大化 / 还原）；V31 第③步起改走 data-tip，并同步 aria-label（读屏口径）
  if (T.event && T.event.listen) {
    T.event.listen('tauri://resize', async () => {
      try {
        const m = await isMax();
        const b = bar.querySelector('.tb-max');
        /* 只写 data-tip —— aria-label 交给 attachTips 的 MutationObserver 同步。
           别在这里手写 aria-label：那会让该元素带上「人工 aria-label」的身份，
           之后状态再变（还原⇄最大化）attachTips 就不敢覆盖了，读屏会一直念旧文案。 */
        if (b) b.dataset.tip = m ? '还原' : '最大化';
      } catch (_) {}
    });
  }
})();

function fmtSize(n){ if(n<1024)return n+" B"; if(n<1048576)return (n/1024).toFixed(1)+" KB";
  if(n<1073741824)return (n/1048576).toFixed(1)+" MB"; return (n/1073741824).toFixed(2)+" GB"; }
/* ---------------- 线性图标（参考图口径：细描边矢量，统一 16×16 网格） ---------------- */
const ICO = {
  grid:   '<path d="M2.6 2.6h4.4v4.4H2.6zM9 2.6h4.4v4.4H9zM2.6 9h4.4v4.4H2.6zM9 9h4.4v4.4H9z"/>',
  list:   '<path d="M2.6 4.2h10.8M2.6 8h10.8M2.6 11.8h10.8"/>',
  /* 时间轴：左侧竖轴 + 两个时间点 + 右侧条目 */
  timeline:'<path d="M3.6 2.4v11.2"/><circle cx="3.6" cy="5.2" r="1.25"/><circle cx="3.6" cy="10.8" r="1.25"/><path d="M7 5.2h6.4M7 10.8h4.4"/>',
  image:  '<rect x="2.4" y="3" width="11.2" height="10" rx="1.6"/><path d="M2.4 10.4l3.1-2.6 2.5 2 2-1.5 3.6 3.1"/>',
  video:  '<rect x="2" y="3.6" width="8.8" height="8.8" rx="1.6"/><path d="M10.8 7.6l3.2-2v4.8l-3.2-2z"/>',
  flow:   '<circle cx="8" cy="3.4" r="1.8"/><circle cx="3.4" cy="12.6" r="1.8"/><circle cx="12.6" cy="12.6" r="1.8"/><path d="M7 5l-2.6 6M9 5l2.6 6"/>',
  clock:  '<circle cx="8" cy="8" r="5.4"/><path d="M8 5v3.2l2.2 1.3"/>',
  flag:   '<path d="M4 2.6v10.8M4 3.4h7.6l-1.7 2.6 1.7 2.6H4"/>',
  /* 批次 B（2026-10-06）：`heart` 已删 —— 全前端（含 CSS / tauri 壳页）零字面量出现，
     且「收藏」语义由 star / starFill 承担，实测是 63 个定义里**唯一**的真零调用。
     如需爱心，按极径 ≤7.0 重画一枚（dev/v4_icon_set.py）。 */
  up:     '<path d="M8 13.2V3.2M3.6 7.6L8 3.2l4.4 4.4"/>',
  /* V2.10.7 重画：原路径 (13.3,2.6) 极径 7.57 超出 §3.3 的 <=7.0（它是死配置才一直没暴露）。
     新图：r=4.5 圆弧右上留缺口 + 末端箭尖朝上的箭头，极径上限 5.89。 */
  refresh:'<path d="M8.78 3.57A4.5 4.5 0 1 0 12.43 7.22M13.87 8.49L12.43 7.22L11.51 8.9"/>',
  more:   '<circle cx="3.7" cy="8" r="1.15"/><circle cx="8" cy="8" r="1.15"/><circle cx="12.3" cy="8" r="1.15"/>',
  filter: '<path d="M2.6 4.2h10.8M4.7 8h6.6M6.7 11.8h2.6"/>',
  batch:  '<rect x="2.4" y="2.4" width="11.2" height="11.2" rx="2.4"/><path d="M5.4 8.2l1.9 1.9 3.5-3.7"/>',
  write:  '<path d="M11.3 2.9l1.8 1.8-7.4 7.4-2.4.6.6-2.4z"/>',
  cn:     '<path d="M8 2.4l5.4 3.1v5.4L8 14l-5.4-3.1V5.5z"/><path d="M8 8l5.4-3.1M8 8v6M8 8L2.6 4.9"/>',
  dot:    '<circle cx="8" cy="8" r="1.4"/>',
  dir:    '<path d="M2.2 5.4A1.2 1.2 0 013.4 4.2h2.3l1.2 1.4h5.7a1.2 1.2 0 011.2 1.2v5.6a1.2 1.2 0 01-1.2 1.2H3.4a1.2 1.2 0 01-1.2-1.2z"/>',
  /* 展示方式：分页（一叠纸）/ 瀑布流（错落方块） */
  pager:  '<rect x="2.2" y="2.6" width="8.4" height="10.8" rx="1.6"/><path d="M4.5 5.8h3.8M4.5 8.2h3.8M4.5 10.6h2.3"/><path d="M12.3 4.6h.6a1 1 0 011 1v7.8a1 1 0 01-1 1h-.6"/>',
  masonry:'<rect x="2.4" y="2.6" width="4.8" height="6.2" rx="1"/><rect x="8.8" y="2.6" width="4.8" height="10.8" rx="1"/><rect x="2.4" y="10.4" width="4.8" height="3" rx="1"/>',
  /* ---- V12 / V1 / V2 新增组件所需图标 ----
     注：旧图标体系的整体替换是 V4（批次 4，分四批做），此处只补新组件要用的，
     不触碰既有字符图标 —— 两件事混在一起，失败时无法定位是哪一批引入的。 */
  check:  '<path d="M3.2 8.4l3.2 3.2 6.4-7"/>',
  info:   '<circle cx="8" cy="8" r="5.6"/><path d="M8 7.4v4"/><path d="M8 5.3h.01"/>',
  warn:   '<path d="M8 2.7l5.7 10.1H2.3z"/><path d="M8 6.6v3.1"/><path d="M8 11.6h.01"/>',
  /* 齿轮：8 齿外廓（齿冠 20° / 齿谷 25°）+ 中心孔。
     旧版是「圆 + 8 根放射线」，16~18px 下跟太阳图标分不出来 —— 顶栏那颗按钮
     是「设置」，不是「配色」，必须一眼认得出。 */
  gear:   '<circle cx="8" cy="8" r="2.3"/><path d="M6.8 1.2L9.2 1.2L9.7 3L10.4 3.3L12 2.3L13.7 4L12.7 5.6L13 6.3L14.8 6.8L14.8 9.2L13 9.7L12.7 10.4L13.7 12L12 13.7L10.4 12.7L9.7 13L9.2 14.8L6.8 14.8L6.3 13L5.6 12.7L4 13.7L2.3 12L3.3 10.4L3 9.7L1.2 9.2L1.2 6.8L3 6.3L3.3 5.6L2.3 4L4 2.3L5.6 3.3L6.3 3Z"/>',
  trash:  '<path d="M3 4.6h10"/><path d="M6.2 4.6V3.2h3.6v1.4"/><path d="M4.5 4.6l.6 8.2h5.8l.6-8.2"/><path d="M6.9 7v3.4M9.1 7v3.4"/>',
  chart:  '<path d="M2.6 13.4h10.8"/><rect x="3.6" y="8.4" width="2.4" height="4.2" rx=".6"/><rect x="6.8" y="4.6" width="2.4" height="8" rx=".6"/><rect x="10" y="6.6" width="2.4" height="6" rx=".6"/>',
  dupe:   '<rect x="2.4" y="2.4" width="8" height="8" rx="1.4"/><rect x="5.6" y="5.6" width="8" height="8" rx="1.4"/>',
  /* 打开单张图片（V2.10.7）：相框 + 山形 + 太阳点。外廓 10.8 x 8.8，极径约 6.9
     （图标口径要求 <= 7.0）；16px 下与「网格 / 瀑布流 / 设置」不混。 */
  openimg:'<rect x="2.6" y="3.6" width="10.8" height="8.8" rx="1.7"/><circle cx="10.1" cy="6.7" r="1.05"/><path d="M2.9 11.2l2.9-2.5 2.3 1.8 2.1-1.7 2.9 2.5"/>',

  /* ---------- 以下为 V4 图标批次 1（卡片与列表）新增 ----------
     路径由 dev/v4_icon_set.py 生成并已过 §3.3 极径 <= 7.0 校验；
     加图标就改那个文件再跑 dev/v4b1_sync_icons.py，别在这里手改。 */
  /* 时间轴组头展开态 / 卡片「收藏到分类」小箭头（▾） */
  caretDown: '<path d="M4.8 6.6L8 9.8l3.2-3.2"/>',
  /* 时间轴组头折叠态（原 ▸） */
  caretRight: '<path d="M6.6 4.8L9.8 8l-3.2 3.2"/>',
  /* 卡片视频角标（原 ▶） */
  play:     '<path d="M5.8 3.6l6.8 4.4-6.8 4.4z"/>',
  /* 卡片未收藏星（原 ☆） */
  star:     '<path d="M8.00 2.70L9.35 6.74L13.61 6.78L10.19 9.31L11.47 13.37L8.00 10.90L4.53 13.37L5.81 9.31L2.39 6.78L6.65 6.74z"/>',
  /* 卡片已收藏星（原 ★，配 .filled 实心） */
  starFill: '<path d="M8.00 3.10L9.26 6.86L13.23 6.90L10.04 9.26L11.23 13.05L8.00 10.75L4.77 13.05L5.96 9.26L2.77 6.90L6.74 6.86z"/>',
  /* 卡片快捷操作·下载（原 ⬇） */
  download: '<path d="M8 3v7.2M5 7.3l3 3 3-3"/><path d="M3.4 12.8h9.2"/>',
  /* 卡片快捷操作·打开文件夹（原 📂） */
  dirOpen:  '<path d="M12.4 8.6V5.8a1 1 0 00-1-1H9.2L8.2 3.5H3.8a1 1 0 00-1 1v6.2a1 1 0 001 1h3.2"/><path d="M10.4 9.9h2.6v2.6M13 9.9L10 12.9"/>',
  /* 文件夹卡片「返回上级」大字（原 ↰） */
  arrowUpLeft: '<path d="M12.4 13V7.2a2.6 2.6 0 00-2.6-2.6H4.6"/><path d="M7.2 2.4L4.6 4.6l2.6 2.2"/>',
  /* 文件夹卡片「收起文件夹」大字、右键「取消标记」（原 ✖） */
  x:        '<path d="M4.4 4.4l7.2 7.2M11.6 4.4l-7.2 7.2"/>',
  /* 右键「打开大图」（原 🔍） */
  search:   '<circle cx="7.2" cy="7.2" r="4.2"/><path d="M10.4 10.4l2.3 2.3"/>',
  /* 右键「收藏到分类…」（原 🏷） */
  tag:      '<path d="M8.4 3h4v4l-5.4 5.4L3 8.4z"/><path d="M10.4 5h.01"/>',
  /* 收藏分类菜单「新建分类…」（原 ＋） */
  plus:     '<path d="M8 3.4v9.2M3.4 8h9.2"/>',
  /* 右键「复制文件名 / 完整路径 / 提示词」 */
  copy:     '<rect x="5.8" y="5.8" width="7" height="7" rx="1.3"/><path d="M10.2 4.2h-6a1 1 0 00-1 1v6"/>',

  /* ---------- 以下为 V4 图标批次 2（大图页与对比页）新增 ----------
     同样由 dev/v4_icon_set.py 生成并过极径校验；
     加图标就改那个文件再跑 dev/v4b2_sync_icons.py。 */
  /* 大图顶栏「返回」（原 ←） */
  arrowLeft:    '<path d="M13 8H3M6.8 4.8L3 8l3.8 3.2"/>',
  /* 大图左导航 / 详情面板展开把手（原 ‹ ❮） */
  chevronLeft:  '<path d="M9.8 3.8L5.6 8l4.2 4.2"/>',
  /* 大图右导航 / 详情面板收起把手（原 › ❯） */
  chevronRight: '<path d="M6.2 3.8L10.4 8l-4.2 4.2"/>',
  /* 预览栏收起后的展开箭头（原 ▴） */
  caretUp:      '<path d="M4.8 9.4L8 6.2l3.2 3.2"/>',
  /* 视频播放中（原 ⏸） */
  pause:        '<path d="M6.3 3.9v8.2M9.7 3.9v8.2"/>',
  /* 上一帧（原 ⏮） */
  skipBack:     '<path d="M4.2 3.9v8.2"/><path d="M6.4 8l5-3.3v6.6z"/>',
  /* 下一帧（原 ⏭） */
  skipFwd:      '<path d="M11.8 3.9v8.2"/><path d="M9.6 8L4.6 4.7v6.6z"/>',
  /* 音量开（原 🔊） */
  volume:       '<path d="M3.2 6.3h2.2L8.3 3.9v8.2L5.4 9.7H3.2z"/><path d="M10.7 6.4a2.4 2.4 0 010 3.2"/>',
  /* 静音（原 🔇） */
  volumeOff:    '<path d="M3.2 6.3h2.2L8.3 3.9v8.2L5.4 9.7H3.2z"/><path d="M10.6 6.6l3 2.8M13.6 6.6l-3 2.8"/>',
  /* 对比·切到滑块模式（原 ⇄） */
  swap:         '<path d="M3.4 5.7h7.2M8.4 3.5l2.2 2.2-2.2 2.2"/><path d="M12.6 10.3H5.4M7.6 8.1l-2.2 2.2 2.2 2.2"/>',
  /* 对比·切回并排模式（原 ▦） */
  gridTable:    '<rect x="3" y="3.2" width="10" height="9.6" rx="1.4"/><path d="M3 6.4h10M3 9.6h10M8 6.4v6.4"/>',
  /* 胶片栏 / 回收站的视频占位（原 🎬） */
  film:         '<rect x="2.8" y="3.6" width="10.4" height="8.8" rx="1.4"/><path d="M2.8 6.4h10.4"/><path d="M6.4 3.6l-1.4 2.8M11 3.6L9.6 6.4"/>',
  /* 循环播放（原 🔁） */
  loop:         '<path d="M4 8a4 4 0 014-4h3.2M11.2 1.9l2.1 2.1-2.1 2.1"/><path d="M12 8a4 4 0 01-4 4H4.8M4.8 14.1l-2.1-2.1 2.1-2.1"/>',
  palette:      '<path d="M8 2.6a5.4 5.4 0 000 10.8c.95 0 1.55-.62 1.55-1.35 0-.42-.2-.76-.48-1.1-.25-.32-.4-.64-.4-1.03 0-.78.62-1.4 1.4-1.4h.85c1.6 0 2.75-1.05 2.75-2.55C13.65 4.3 11.1 2.6 8 2.6z"/><path d="M6.5 6.5h.01M5.5 8.9h.01"/>',
  history:      '<path d="M2.9 8a5.1 5.1 0 105.1-5.1c-1.95 0-3.65 1.1-4.5 2.7"/><path d="M2.8 3.5v2.7h2.7"/><path d="M8 5.5V8l1.9 1.15"/>',
  file:      '<path d="M4.2 2.6h4.9l3 3v7.8H4.2z"/><path d="M9.1 2.6v3h3"/>',
  sparkle:      '<path d="M6.4 2.6l1.05 2.95L10.4 6.6 7.45 7.65 6.4 10.6 5.35 7.65 2.4 6.6l2.95-1.05z"/><path d="M11.6 8.8l.62 1.75 1.75.62-1.75.62-.62 1.75-.62-1.75-1.75-.62 1.75-.62z"/>',
  drive:      '<rect x="2.6" y="4.8" width="10.8" height="6.4" rx="1.4"/><path d="M2.6 8h10.8"/><path d="M5.4 12.6h1.2M9.4 12.6h1.2"/>',
  keyboard:      '<rect x="2.6" y="4.2" width="10.8" height="7.6" rx="1.5"/><path d="M5.2 6.9h.01M8 6.9h.01M10.8 6.9h.01M6 9.3h4"/>',
  ban:      '<circle cx="8" cy="8" r="5.2"/><path d="M4.3 4.3l7.4 7.4"/>',
  /* NSFW 卡片左上角「单独查看这张」（原 👁）。手加的：dev/v4_icon_set.py 不在本源码包里，
     无法走生成脚本；极径已按同一约束校过（最远点距中心 6.8 < 7.0）。 */
  eye:      '<path d="M1.9 8c1.85-2.85 3.9-4.3 6.1-4.3S12.25 5.15 14.1 8c-1.85 2.85-3.9 4.3-6.1 4.3S3.75 10.85 1.9 8z"/><circle cx="8" cy="8" r="1.9"/>',
  /* 「斜杠眼」（原 🙈）：盖着 / 揭开的**两个状态共用同一枚斜杠眼**——按下换态，
     图标不变。这是图标集**本就装着的**符号，不要另造。
     δ-2b 曾为遮罩态临时加过一枚「眼睛+×」（eyeHide）；δ-2c 按用户要求撤掉，
     回到这一枚。**删掉比留着更安全**：留着它，以后很容易又被哪处 `ico("eyeHide")`
     悄悄捡起来用（这正是 δ-2b 当初「另造一枚」埋下的隐患）。 */
  eyeOff:   '<path d="M1.9 8c1.85-2.85 3.9-4.3 6.1-4.3S12.25 5.15 14.1 8c-1.85 2.85-3.9 4.3-6.1 4.3S3.75 10.85 1.9 8z"/><circle cx="8" cy="8" r="1.9"/><path d="M3.2 12.8L12.8 3.2"/>',
};
const ico = (name, cls) =>
  `<svg class="iico${cls ? " " + cls : ""}" data-i="${name}" viewBox="0 0 16 16" aria-hidden="true">${ICO[name] || ""}</svg>`;

/* V4 批次 2：「图标 + 文字」的按钮统一从这里写内容。
   保留 .ic 槽（与顶栏同构：font-size:0 + flex 居中，避免 svg 与文字之间的基线抖动），
   于是「收藏 / 取消收藏」这类状态切换只要换图标名，不会把文字或 svg 冲掉。 */
function setIcoLabel(btn, name, label, cls){
  if(!btn) return;
  btn.innerHTML = (name ? '<span class="ic">' + ico(name, cls) + '</span>' : '') + (label || '');
}

function fmtTime(ts){ const d=new Date(ts*1000),p=x=>String(x).padStart(2,"0");
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`; }
function fmtDur(s){ if(!isFinite(s)) return "未知";
  s=Math.round(s); const m=Math.floor(s/60), ss=String(s%60).padStart(2,"0");
  const h=Math.floor(m/60);
  return h? `${h}:${String(m%60).padStart(2,"0")}:${ss}` : `${m}:${ss}`; }
/* ---------------- Toast 分层（V12） ----------------
   四态（ok / info / warn / err）各配图标与左侧色条；带 detail 的提示挂「详情」展开；
   同屏最多 3 条，**同一文案在合并窗内累计为 ×N** —— 连点同一个动作不刷屏。
   「详情」是给功能线的危险操作（F14 移动 / F15 重命名 / F19 再生成）准备的：
   那类失败有明确原因（路径冲突、跨盘失败、节点定位失败），一行文案装不下。 */
const TOAST_MAX = 3;            // 同屏上限（超出挤掉最旧的一条）
const TOAST_MERGE_MS = 2500;    // 同文案合并窗（毫秒）
/* ---------- 底部浮层避让（2026-10-07） ----------
   #toastStack 与 #batchBar / #vidBar 都是「底部居中」的浮层，底距只差 --sp 一档
   ⇒ 同屏时提示条会把操作条整条盖住（实测覆盖比 1.00 / 0.89，按钮全部点不到）。
   toast 的层级刻意最高（--z-toast 650 > --z-raise 300 / --z-vbar 8），
   所以只能从**几何**上避让：量测遮挡物的真实矩形，把整组 toast 抬到它上沿之上。
   ⚠️ 不写死遮挡物高度 —— 它随 --fsx 与窄屏 padding 变化（真值同源）。
   ⚠️ **新增「底部居中的操作条」时必须登记进 TOAST_AVOID**，否则重演本 bug。 */
const TOAST_AVOID=["#batchBar","#vidBar"];
function _toastAvoidTop(){
  let top=null;
  for(const sel of TOAST_AVOID){
    const el=document.querySelector(sel); if(!el) continue;
    const cs=getComputedStyle(el);
    if(cs.display==="none"||cs.visibility==="hidden") continue;
    const r=el.getBoundingClientRect();
    if(r.width<2||r.height<2||r.top<=0||r.top>=window.innerHeight) continue;
    top=(top==null)?r.top:Math.min(top,r.top);
  }
  return top;
}
function syncToastLift(){
  const root=document.documentElement;
  if(!document.getElementById("toastStack")) return;      // 没弹过 toast ⇒ 无需预留
  const top=_toastAvoidTop();
  if(top==null){ root.style.removeProperty("--toast-bottom"); return; }
  const fsx=parseFloat(getComputedStyle(root).getPropertyValue("--fsx"))||1;
  root.style.setProperty("--toast-bottom",
    Math.max(0, window.innerHeight-top+8*fsx).toFixed(1)+"px");
}
window.mvSyncToastLift=syncToastLift;
window.addEventListener("resize", syncToastLift, {passive:true});
/* --fsx（字号档）写在 documentElement 的内联样式上 ⇒ 观察 style 属性即可。
   ⚠️ 本回调会写 --toast-bottom（同样是 style 变更）⇒ 用「--fsx 值比对」收口：
      第二次进来发现字号没变就直接 return，不会自激成死循环。 */
let _toastLastFsx=null;
new MutationObserver(()=>{
  const v=getComputedStyle(document.documentElement).getPropertyValue("--fsx").trim();
  if(v===_toastLastFsx) return;
  _toastLastFsx=v; syncToastLift();
}).observe(document.documentElement,{attributes:true,attributeFilter:["style"]});
function _toastBox(){
  return document.getElementById("toastStack") || (()=>{
    const d=document.createElement("div"); d.id="toastStack"; document.body.appendChild(d); return d; })();
}
function _toastKill(el, now){
  if(!el || el._dead) return;
  el._dead=true; clearTimeout(el._timer);
  if(now){ el.remove(); return; }                  // 超额挤掉：立即移除，不占位
  el.classList.add("out");
  setTimeout(()=>el.remove(), 220);
}
function _toastLive(box, msg){
  for(const el of box.children)                    // 正在淡出的（.out）不算存活
    if(el.dataset.msg===msg && !el.classList.contains("out")) return el;
  return null;
}
function _attachDetail(el, detail){
  if(!detail || el.querySelector(".tdetail")) return;
  const msg=el.querySelector(".tmsg");
  if(!msg) return;
  const d=document.createElement("pre"); d.className="tdetail"; d.textContent=detail;
  msg.appendChild(d);
  if(el.querySelector(".tmore")) return;
  const b=document.createElement("button");
  b.className="tmore"; b.type="button"; b.textContent="详情"; b.setAttribute("aria-expanded","false");
  b.onclick=()=>{ const on=el.classList.toggle("open");
    b.textContent=on?"收起":"详情"; b.setAttribute("aria-expanded",on?"true":"false"); };
  el.insertBefore(b, el.querySelector(".tact") || null);
}
function _toastPush(msg, cls, ms, detail, action){
  const box=_toastBox();
  syncToastLift();                                 // 弹出前先把整组让开底部操作条
  const kind = (cls==="err"||cls==="warn") ? "warn" : cls==="ok" ? "check" : "info";
  announce(msg, cls==="err"||cls==="warn");       // V40：toast 的「听」出口
  const live=_toastLive(box, msg);
  if(live){                                        // 同文案：累计 ×N 并续命，不新增一条
    live._n=(live._n||1)+1;
    const tn=live.querySelector(".tn");
    if(tn) tn.textContent="×"+live._n;
    else{
      const b=document.createElement("span"); b.className="tn"; b.textContent="×"+live._n;
      live.insertBefore(b, live.querySelector(".tmore") || live.querySelector(".tact") || null);
    }
    if(detail) _attachDetail(live, detail);
    clearTimeout(live._timer);
    live._timer=setTimeout(()=>_toastKill(live), ms);
    return live;
  }
  const el=document.createElement("div");
  el.className=cls||"info"; el.dataset.msg=msg; el._n=1;
  const ic=document.createElement("span"); ic.className="tico"; ic.innerHTML=ico(kind);
  const wrap=document.createElement("div"); wrap.className="tmsg";
  const sp=document.createElement("span"); sp.textContent=msg;
  wrap.appendChild(sp);
  el.appendChild(ic); el.appendChild(wrap);
  if(detail) _attachDetail(el, detail);
  if(action){                                      // 操作按钮（如「撤销」）
    const btn=document.createElement("button");
    btn.className="tact"; btn.type="button"; btn.textContent=action.label;
    btn.onclick=()=>{ _toastKill(el); action.fn(); };
    el.appendChild(btn);
  }
  box.appendChild(el);
  const alive=[...box.children].filter(c=>!c.classList.contains("out"));
  for(const old of alive.slice(0, Math.max(0, alive.length-TOAST_MAX))) _toastKill(old, true);
  el._timer=setTimeout(()=>_toastKill(el), ms);
  return el;
}
/* G2·V40：读屏状态播报（宿主见 index.html #livePolite / #liveAlert）
   与 toast 并行、互不替代：toast 是「看」的出口，这里是「听」的出口。
   ⚠️ 同一句话重复播报时读屏不会念第二遍（DOM 文本没变化就不触发）
      ⇒ 先把文本清空、下一拍再写回，强制产生一次变更。
   ⚠️ 计时器挂在**元素自己**身上，两个区各自独立 —— 共用一个会让密集播报互相吞掉。 */
function announce(msg, urgent){
  const el=document.getElementById(urgent?"liveAlert":"livePolite");
  if(!el || !msg) return;
  const s=String(msg), now=Date.now();
  /* toast 与底栏状态中枢经常报**同一件事**（extras.js 就是「toast(msg) + setBotState('done',msg)」），
     不去重读屏会连念两遍 ⇒ 同一句话 1.2s 内不重复进同一个区。 */
  if(s===el._last && now-(el._at||0)<1200) return;
  el._last=s; el._at=now;
  el.textContent="";
  clearTimeout(el._t);
  el._t=setTimeout(()=>{ el.textContent=s; },30);
}
window.mvAnnounce=announce;
/* 结果数播报：只在**计数真的变了**时出声（翻页、瀑布流续载、重渲染都会再写 #count）。
   首屏那一次也不播 —— 一进页面就念一串数字是噪声，不是信息。 */
let _lastCount=null;
function announceCount(txt){
  const s=String(txt||"").trim();
  if(!s || s===_lastCount) return;
  const first=(_lastCount===null);
  _lastCount=s;
  if(first) return;
  announce(s);
}
function toast(msg, cls, ms=3200, detail=""){ return _toastPush(msg, cls, ms, detail, null); }
function toastAction(msg, label, fn, cls, ms=7000, detail=""){
  return _toastPush(msg, cls, ms, detail, {label, fn});
}
/* 失败提示 + 可展开详情：功能线的危险操作统一走这里（一行文案装不下的原因） */
function toastErr(msg, detail){ return _toastPush(msg, "err", 5200, detail, null); }

/* §6.1：删除类响应的**统一出口** —— 后端现在会带回「文件删了、但元数据（收藏 / 标签 /
   标记 / 回收站快照）没能写回」的明细（meta_failed / warning）。从前这类失败是全静默的：
   文件没了、收藏还挂着，用户毫无线索。各删除调用点各调一次即可，不必各写一遍。 */
function noteDeleteMeta(j){
  if(!j || !j.meta_failed || !j.meta_failed.length) return false;
  toastErr("文件已删除", j.warning || ("但 " + j.meta_failed.join("、") + " 未能写回"));
  return true;
}

/* ---- 换根迁移：报告展示 + 自动迁移结果拉取 ----
   换根（移动/改名/换盘符）后后端会自动把旧目录的检测结果、人工纠正、收藏等接回来；
   报告经 /api/migrate_report「取走即清」，一次迁移只提示一次。手动兜底（设置页）
   拿到的就是同一份报告结构，共用这一个展示函数。 */
function mvToastMigrate(r){
  if(!r || !r.ok || !r.migrated) return;
  const parts=[];
  if(r.cache)  parts.push("检测结果 " + r.cache);
  if(r.manual) parts.push("人工纠正 " + r.manual);
  if(r.favs)   parts.push("收藏 " + r.favs);
  if(r.tags)   parts.push("标签 " + r.tags);
  if(r.marks)  parts.push("标记 " + r.marks);
  if(!parts.length) return;
  toast("已从旧目录找回：" + parts.join("、"), "ok", 7000);
}
async function mvMigrateReport(){
  try{
    const j = await api("/api/migrate_report");
    mvToastMigrate(j && j.report);
  }catch(_){ /* 报告拉不到就拉不到，不值得打扰用户 */ }
}

/* ================= 底栏状态中枢（V3） =================
   底栏左侧从写死的「已就绪」改为实时状态位：它是**全局反馈原语**（与 toast 同级），
   所以放在 app.js —— 任何模块都能报状态，不必互相依赖。
     kind  idle 就绪 / busy 进行中（点脉冲）/ done 完成 / warn 需注意 / err 出错
     action 可选 {label, fn}：在状态右侧挂一个行动按钮（如「刷新」「查看」）
     sticky true=不自动回落到「已就绪」（用于「索引构建中」这类要一直挂着的过程态）
   回落设计：非 idle 状态默认 4.2 秒后自动归位，这样「闪一下」的状态不会永久滞留底栏。 */
const BOT_KINDS=["idle","busy","done","warn","err"];
let _botResetTimer=0;
function setBotState(kind, msg, action, sticky){
  const box=document.getElementById("botState");
  if(!box) return;
  const act=document.getElementById("botAct"), lab=document.getElementById("botMsg");
  box.classList.remove(...BOT_KINDS);
  box.classList.add(BOT_KINDS.indexOf(kind)>=0?kind:"idle");
  box.dataset.tip=msg||"后台状态";
  if(lab) lab.textContent=msg||"已就绪";
  /* G2·V40：底栏是全局反馈原语 ⇒ 它的**终态**也要播报（索引进度建完、刷新完成、出错）。
     busy 刻意不播：进度百分比每 2 秒变一次，播了就是刷屏。 */
  if(msg && (kind==="done"||kind==="err"||kind==="warn")) announce(msg, kind==="err");
  if(act){
    if(action && action.label && typeof action.fn==="function"){
      act.hidden=false; act.textContent=action.label; act.onclick=()=>action.fn();
    }else{ act.hidden=true; act.onclick=null; act.textContent=""; }
  }
  clearTimeout(_botResetTimer);
  if(!sticky && kind!=="idle")
    _botResetTimer=setTimeout(()=>setBotState("idle","已就绪",null,true), 4200);
}

/* ε-6 E6-3：`api()` 原来**只看响应体里的 error**，不看 HTTP 状态码 ——
   服务端 500 但响应体里恰好没有 `error` 字段时，这次调用会被**当成成功**：
   上层的 `if(j.error) return;` 一路放过，于是界面继续用「失败请求」的结果
   （典型：/api/list 挂了却把空数组当「这个目录是空的」渲染出来）。
   后端 §6.1 已保证异常都带 `error`（server.py 的 500 兜底），但**网关/代理/
   LAN 中间层**返回的非 JSON 错误页不归它管 ⇒ 客户端必须自己兜这一层。
   注意顺序：先看 `j.error`（更具体、带人话），没有再退到状态码文案。 */
async function api(url, opt){ return CMV3.request(url, opt); }

/* 竞态令牌：慢响应不得覆盖新状态。
   ε-6 E6-4：原来这里是 `let loadTok=0`（只有 load() 用）—— 已提升为 state.js 的
   **共享** `viewGen`，让「切目录」也能作废在途的收藏夹视图 / 元数据映射加载。
   见 state.js 里那段「权威视图世代」的用法说明（**单向权威**，别都 ++）。 */
let lastSig=null;                                        // 目录签名：自动刷新轮询比对用
async function load(path, opts={}){
  const tok=++viewGen;
  const prevPath=S.path;                                 // 用于区分「换目录」与「同目录刷新」
  if(!opts.silent) toast("加载中…");
  try{
    const j = await CMVQuery.readDirectory(path||"", opts, tok);
    if(tok!==viewGen) return;                        // 已有更新的加载在进行/完成
    const dirChanged = prevPath !== j.path;
    const rootChanged = normPath(S.root || "") !== normPath(j.root || "");
    if(rootChanged){
      statsTok++; S.stats=null; statsBuildAt=0;
      loadStats._sig=""; loadStats._lastTotal=undefined;
    }
    S.root=j.root; S.path=j.path; S.dirs=j.dirs; S.items=j.items; S.hasThumb=j.has_thumb;
    if(!opts.preserveExpanded) S.expandDirs=false;
    favMode=false; libMode="";
    CMVQuery.install(j);
    $("#wfClass").style.display="none"; $("#markState").style.display="none";
    if(dirChanged){                                  // 只有真的换了目录才重置筛选（同目录静默刷新要保留）
      S.metaMap=null; resetMetaFilters();
      TL_COLLAPSED={}; TL_SHOWN={};        // 时间轴折叠按目录隔离：组键只含日期，不重置会串台
      /* P1-9：格式 / 工作流类型也必须跟着重置 —— 它们与标签、时间轴折叠同属「按目录隔离」
         的状态。不重置的话：在 A 目录勾了 .webp / cn，切到 B 目录后筛选条件还生效，
         B 的条目被悄悄滤掉（数量明显偏少甚至为空），而左栏那个分区折叠着的时候
         用户完全看不到线索。
         ⚠️ settings.js 的 clearFmtWfk 是**非提升**的函数声明、且不带 applyFilter()
         （刷新由本函数在第 430 行统一做），所以它「函数尚不存在」或「存在但没清干净」
         都必须兜住 —— 这两条路任意一条漏了，用户看到的就是「B 目录张数莫名其妙偏少」。 */
      let _fmtCleared = false;
      if(typeof clearFmtWfk==="function"){ try{ clearFmtWfk(); _fmtCleared=true; }catch(_){} }
      if(!_fmtCleared){ FMTS.clear(); WFK=""; }
      if(tagFilterActive || TAG_FILTER.length || TAG_EXCLUDE.length){
        TAG_FILTER=[]; TAG_EXCLUDE=[]; tagFilterActive=false; S.tagView=null;
        renderTagNav();
      }
    }
    const sy=(document.getElementById("gridwrap")||{}).scrollTop||0;   // 滚动容器是 #gridwrap 不是 window
    if(batchOn) setBatch(false);                       // 切换目录退出批量，防跨目录残留选中
    applyFilter(); crumbs(); refreshRoots();
    if(rootChanged) void loadStats();
    if(opts.silent){ const gw=document.getElementById("gridwrap"); if(gw) gw.scrollTop=sy; }  // 静默刷新保持滚动位置
    else toast(`已加载 ${j.items.length} 个文件`, "ok");
    /* P1-14：「目录签名」是与主请求**成对**发出的，但此前只有主请求做了令牌守卫，
       dirinfo 的 then 无条件写 lastSig ⇒ 快速连点换目录时，**慢的旧响应后到**
       会把 lastSig 覆盖成旧目录的签名。后果：轮询比对「签名变了 ⇒ 有新文件」时
       拿旧签名当基线 ⇒ 要么漏报新文件（该刷新不刷新），要么把旧目录误判成「变了」
       ⇒ 在当前目录里触发一次多余的刷新（严重时用户正在看的内容被重置）。
       ⚠️ 必须在 **then 内**再判一次 —— 发请求时 tok 还是新的，回来时可能已被顶掉。
       写法与本函数开头主请求的守卫同源（`if(tok!==viewGen) return;`）。 */
    api("/api/dirinfo?path="+encodeURIComponent(j.path)
      +"&rec="+($("#rec").checked?"1":"0"))
      .then(jj=>{ if(tok!==viewGen) return; lastSig=jj.sig; }).catch(()=>{});
  }catch(e){ if(tok!==viewGen) return; toast(e.message, "err"); }
}

/* ---------------- 左栏区块收起/展开 ----------------
 * 点区块标题（位置 / 库 / 生成参数 / 收藏 / 标签 / 格式 / 其他）收起或展开，省掉不常用区块占的高度；
 * 状态存 localStorage（mv.sidecol），刷新后保持。标题里的按钮（标签 AND / ⚙）不触发收起。
 * ⚠️ 存储键就是 sec.id —— 所以**改分区标题文案不会动记忆**，但**改 id 会**（老键成孤儿、分区回到默认态）。
 *    H4（2026-10-06）把末块「筛选」更名为「其他」时正是为此保留 id #filtSec 不改。
 */
(function setupSideSections(){
  const KEY="mv.sidecol";
  const secs=[...document.querySelectorAll("#left .lsec")].filter(s=>s.id);
  if(!secs.length) return;
  let saved={};
  try{ saved=JSON.parse(localStorage.getItem(KEY)||"{}")||{}; }catch(_){ saved={}; }
  const write=()=>{ try{ localStorage.setItem(KEY, JSON.stringify(saved)); }catch(_){} };

  secs.forEach(sec=>{
    const h=sec.querySelector("h4"); if(!h) return;
    const label=((h.querySelector("span")||h).textContent||"").trim();
    const apply=(collapsed)=>{
      sec.classList.toggle("collapsed", collapsed);
      h.setAttribute("role","button");
      h.setAttribute("aria-expanded", collapsed?"false":"true");
      h.tabIndex=0;
      const tip=(label?label+"：":"")+"点击"+(collapsed?"展开":"收起");
      h.dataset.tip=tip;
    };
    const toggle=()=>{
      const collapsed=!sec.classList.contains("collapsed");
      apply(collapsed); saved[sec.id]=collapsed; write();
    };
    h.addEventListener("click",e=>{
      if(e.target.closest("button")) return;      // 标题里的按钮只管自己
      toggle();
    });
    h.addEventListener("keydown",e=>{
      if(e.target!==h) return;
      if(e.key==="Enter"||e.key===" "){ e.preventDefault(); toggle(); }
    });
    apply(!!saved[sec.id]);
  });
})();

/* ---------------- V31 第①步：让 data-tip 真正生效（2026-09-17） ----------------
 * 背景：data-tip 此前是「写了 60+ 处、零实现」的幽灵属性 —— 气泡全部来自原生 title；
 *       而「只有 data-tip、没有 title」的元素（实测 7 处静态 + 4 处动态）**完全不出任何提示**，
 *       其中目录卡 / 查重 / 回收站的文件名被 ellipsis 截断后，用户没有任何办法看到全名。
 *
 * 第①步（2026-09-17）只修这一处真缺陷，范围刻意收窄：选择器**排除带 title 的元素** ⇒ 零双气泡；
 * 那 48 处「data-tip + title 双写」的元素保持原状。
 *
 * 第②③步（2026-09-18 · 体验批次 D2）把全站统一到 **data-tip 一个出口**：
 *   · 非表单元素的 40 处「双写」删掉 title、26 处「仅 title」改写成 data-tip（静态 + 动态同批）；
 *   · **表单控件（input / select / option / textarea）保留原生 title 作降级** ——
 *     原生控件与自绘气泡混用易出问题（V31 既定取舍），所以排除判据从
 *     「有没有 title」**收窄成「是不是表单控件」**；
 *   · 顺手补 `aria-label`（给没有可访问名的元素）—— 这是 **V16 的出口**：
 *     `data-tip` 与 `aria-label` 是同一件事的两个出口（鼠标 / 读屏），分两批做必然漏一半。
 * 实现要点：
 *   · 事件委托（mouseover / mouseout）：卡片、列表行、弹窗内容都是动态生成的，逐元素绑定必漏；
 *   · position:fixed + 视口夹取（右边界夹取、下方放不下则翻到鼠标上方）——
 *     不用伪元素是因为卡片与详情面板都有 overflow:hidden，会被裁掉；
 *   · 滚动 / 滚轮 / 按下 / 拖拽 / 窗口失焦时立刻收起，避免气泡挂在旧位置。
 * 键盘聚焦提示（focusin）**留给 V16** —— 鼠标与读屏是同一件事的两个出口，同批做才不会漏。
 */
/* 表单控件清单：这些**保留原生 title 作降级**，不走自绘气泡（V31 既定取舍）。 */
const TIP_FORM = new Set(["INPUT", "SELECT", "OPTION", "TEXTAREA", "PROGRESS", "METER"]);

function attachTips(){
  let el = null, cur = null;
  const hide = () => { if(el){ el.remove(); el = null; } cur = null; };
  const target = (node) => {
    const t = node && node.closest ? node.closest("[data-tip]") : null;
    return (t && !TIP_FORM.has(t.tagName)) ? t : null;
  };
  /* ---------- V16 出口：给没有可访问名的元素补 aria-label ----------
     · 已有 aria-label / aria-labelledby 的不覆盖（人工写过的更准）；
     · 动态节点（卡片列 / 标签树 / 弹窗内容 / 时间轴组）靠 MutationObserver 兜 ——
       逐元素绑定必漏，这也是第①步用事件委托的同一个理由；
     · **只监听 childList** ⇒ 自己 setAttribute 不会自触发，不会绕成死循环；
     · rAF 合并批量渲染（9000 张图逐张 append 会打出上万次回调）；
       页面不可见时 rAF 不触发，用 60ms 定时器兜底（headless 门禁下 rAF 是否跑不保证）。 */
  /* 谁的可访问名是**我们**写的：用 WeakSet 记（不往 DOM 里塞标记属性，保持 DOM 干净）。
     · 人工写过 aria-label 的元素（如壳页窗口按钮）**永不覆盖**；
     · 但**动态改过 data-tip** 的元素（标签匹配方式 AND/OR/NOT、最大化⇄还原、预览栏开关、
       删除去向…）必须跟着更新 —— 否则读屏会一直念旧文案，而那恰恰是「按钮状态变了」的场景。

     ⚠️ P1-21：**有可见文字的元素不补 aria-label**。
       可访问名（accname）的优先级是 aria-label > 元素文本内容，所以给一个写着「删除所选」
       的按钮补 aria-label="移入应用内回收站（可在「回收站」还原）"，读屏念的就不是用户
       看到的字了 —— 语音控制喊「删除所选」也匹配不上（违反 WCAG 2.5.3 Label in Name）。
       而这在 D2 之前不会发生：那时提示走原生 title，title 只在元素没有其它名字时才兜底。
       所以只给「确实没有可见文字」的元素补（图标按钮、纯图形按钮），看文字就够了。 */
  const _isSrOnly = (el) => {
    const c = el && el.classList;
    return !!(c && (c.contains("sr-only") || c.contains("vhid")));
  };
  const _visibleText = (t) => {
    /* 只看可见子树里的文字，不取 aria-label / title（它们不是「可见文字」）。
       sr-only 里的文本是给读屏的、用户看不见 ⇒ 不算数（否则纯图标按钮会被误判成
       「有可见文字」而丢掉 aria-label，那是反向回归）。 */
    if(_isSrOnly(t)) return false;
    const walk = (el) => {
      for(const n of el.childNodes){
        if(n.nodeType === 3){                       // 文本节点
          if((n.nodeValue || "").trim()) return true;
          continue;
        }
        if(n.nodeType !== 1) continue;
        if(_isSrOnly(n)) continue;
        if(walk(n)) return true;
      }
      return false;
    };
    return walk(t);
  };
  /* ⚠️ P1-21 修正（2026-09-19）：AUTO 的**登记必须落在 DOM 上**（data-mvl）。
     attachTips() 常规只初始化一次，但门禁 / 显式重跑都会再调一次 ——
     若只靠函数体内 new 出来的 WeakSet，第二次调用就是一张白纸，
     「我们写过的名字」全体失忆：既不跟随 data-tip 更新，
     也永远清不掉（下面那个「有可见文字就撤名」的分支直接不可达）。
     用属性而不是 WeakSet 作真相：跨调用、跨 observer 都认得出自己。 */
  const label = (root) => {
    const one = (t) => {
      if(!t || t.hasAttribute("aria-labelledby")) return;      // 人工绑定的更准，不碰
      const mine = t.dataset.mvl === "1";                      // 这个名字是我们写的（持久登记）
      if(t.hasAttribute("aria-label") && !mine) return;        // 人工写的，不碰
      if(_visibleText(t)){                                     // P1-21：有可见文字 ⇒ 不补，且撤掉我们的旧名
        if(mine){ t.removeAttribute("aria-label"); delete t.dataset.mvl; }
        return;
      }
      const txt = t.getAttribute("data-tip");
      if(txt == null){                                         // 提示没了，我们的名字也该走
        if(mine){ t.removeAttribute("aria-label"); delete t.dataset.mvl; }
        return;
      }
      if(t.getAttribute("aria-label") !== txt){
        t.setAttribute("aria-label", txt);
        t.dataset.mvl = "1";                                   // 登记：下次能认出是自己写的
      }
    };
    if(root.nodeType !== 1) return;
    if(root.matches && root.matches("[data-tip]")) one(root);
    if(root.querySelectorAll) root.querySelectorAll("[data-tip]").forEach(one);
  };
  label(document.body || document.documentElement);
  let pend = [], raf = 0, tid = 0;
  const flush = () => {
    if(raf){ cancelAnimationFrame(raf); raf = 0; }
    if(tid){ clearTimeout(tid); tid = 0; }
    const ns = pend; pend = []; for(const n of ns) label(n);
  };
  new MutationObserver(muts => {
    for(const m of muts){
      if(m.type === "attributes"){ label(m.target); continue; }   // data-tip 被改 → 立刻同步可访问名
      for(const n of m.addedNodes) if(n.nodeType === 1) pend.push(n);
    }
    if(pend.length && !raf && !tid){
      raf = requestAnimationFrame(flush);
      tid = setTimeout(flush, 60);
    }
  }).observe(document.body || document.documentElement,
             /* ⚠️ attributeFilter 只盯 data-tip：自己写 aria-label 不会把观察器再触发一轮（不自激）。
                childList 只收「新增节点」，setAttribute 不改结构 ⇒ 也不会滚成死循环。 */
             {childList:true, subtree:true, attributes:true, attributeFilter:["data-tip"]});
  const show = (t, x, y) => {
    if(cur === t) return;                 // 仍在同一元素内（如移到它的图标上）不重建，避免闪
    const txt = t.getAttribute("data-tip");
    if(!txt) return;
    hide();
    const b = document.createElement("div");
    b.className = "mv-tip";
    b.setAttribute("role","tooltip");
    b.textContent = txt;
    document.body.appendChild(b);
    const r = b.getBoundingClientRect(), pad = 8;
    let L = x + 12, T = y + 18;
    if(L + r.width > innerWidth - pad) L = Math.max(pad, innerWidth - pad - r.width);
    if(T + r.height > innerHeight - pad) T = y - r.height - 14;   // 下方放不下 → 翻到上方
    if(T < pad) T = pad;
    b.style.left = L + "px"; b.style.top = T + "px";
    el = b; cur = t;
  };
  document.addEventListener("mouseover", e => {
    const t = target(e.target);
    if(t) show(t, e.clientX, e.clientY); else hide();
  });
  document.addEventListener("mouseout", e => {
    const t = target(e.target);
    if(!t) return;
    if(e.relatedTarget && t.contains(e.relatedTarget)) return;    // 还在同一元素内部
    hide();
  });
  for(const ev of ["scroll","wheel","mousedown","keydown","dragstart"])
    document.addEventListener(ev, hide, true);
  window.addEventListener("blur", hide);
}
attachTips();

/* =====================================================================
   V38 浮层冻结（2026-09-18 · 体验批次 G1）
   ---------------------------------------------------------------------
   问题（实测 · dev/g1_probe.py）：大图打开后连按 14 次 Tab，**14/14 全部落到背景**
   （#btnUp → #qmode → #q → #btnRefresh …）—— 浮层把画面盖住了，但键盘焦点与读屏
   仍在主界面里游走，键控用户会彻底迷失到看不见的那一层里。

   做法：全屏浮层开启时，给背景区加 inert（浏览器原生语义：不可聚焦 + 不可点 +
   对辅助技术隐藏），关闭时移除。

   为什么用 MutationObserver 一处收口，而不是在开合函数里逐个加：
     开合点分散在 settings.js / viewer.js / nav.js / batch.js 四个文件
     ⇒ 逐个加必漏。这是 D2「同一件事的两个出口必须同批做」的同款教训。

   ⚠️⚠️ 判据必须回源码核对，**别按 HTML 属性猜**（本项目在此栽过）：
     `#setMask` / `#hintMask` 的 `hidden` 只是「首帧不闪」的初始态 ——
     settings.js:1054 / 1381 明确写着 `mask.hidden=false; hidden 只作初始态；开合一律走 .on`，
     真实的开合状态是 `.on`（同文件 `isOpen()`）。第一版按 `hidden` 判 ⇒ 初始化后
     `hidden` 已被移除 ⇒ 判据恒为「开着」⇒ 背景一上来就被永久冻住（门禁首轮抓到）。

   ⚠️ 其余三个必须守住的边界：
     1. attributeFilter 只盯 class —— 我们只写 inert，不在过滤列表里 ⇒ 不自激（D2 同款约束）；
     2. **#left 在窄屏抽屉模式下是浮层本身**，不是背景 ⇒ 抽屉开时不能冻它，
        否则抽屉里的导航项点不动；#leftTgl 同理（那时它是「关抽屉」的开关）；
     3. #toastStack（读屏播报出口）与 #batchBar（批量操作条）**永不冻结**。
   ===================================================================== */
function attachInert(){
  const $id = (i) => document.getElementById(i);
  /* 全屏浮层判据（两族，**判法不同**）：
     ① 常驻容器 #lb / #cmp / #setMask / #hintMask —— 一直在 DOM 里，开合由 `.on` 表示；
     ② **按需创建**的浮层 .bigMask（bigDialog）与 #catInputMask（confirmDialog /
        catInputDialog / tmplDialog）—— 打开时才 appendChild、关闭时 remove()，
        **没有 `.on` 状态**。对它们只能判「在不在 DOM 里」。

     ⚠️ P1-20（本批修复）：第 ② 族此前**完全在机制之外** —— 实测（dev/p1b_probe.py）
     6 个全屏浮层里恰好这 2 个 `main=-`（未冻）。其中 .bigMask 有 11 处调用点
     （查重 / 模板选择 / 桥接引导…），键盘用户在弹窗里按 Tab 会 12/12 全部落到背景控件。

     ⚠️ 判据必须用 closest('#content') 的**存在性**而非「可见性」——
     .bigMask 在打开瞬间可能还没算出布局，用 offsetParent/可见性判会漏掉首帧。 */
  const modalOpen = () => {
    for(const id of ["lb", "cmp", "setMask", "hintMask"]){
      const e = $id(id);
      if(e && e.classList.contains("on")) return true;
    }
    /* 按需创建的浮层：只要节点还在文档里就算「开着」（它们的生命周期与打开态等价） */
    if(document.querySelector(".bigMask")) return true;
    if($id("catInputMask")) return true;
    return false;
  };
  const drawerOpen = () => document.body.classList.contains("nav-open");   // 窄屏左栏抽屉
  const mark = new WeakSet();                        // 记「inert 是我们加的」⇒ 不碰人工写法
  const apply = (el, on) => {
    if(!el) return;
    if(on){
      if(mark.has(el) || el.hasAttribute("inert")) return;   // 已是（我们的或人工的）⇒ 不动
      el.setAttribute("inert", ""); mark.add(el);
    }else if(mark.has(el)){
      el.removeAttribute("inert"); mark.delete(el);
    }
  };
  /* 每个受控元素**每次都被显式设置**（幂等）—— 不写「仅当 X 才设」的分支，
     避免任何一条路径漏掉解冻、把界面永久冻死。
       · 全屏浮层 ⇒ 冻 #main 整块（它含 #left / #content / #leftTgl）；
       · 窄屏抽屉 ⇒ 只冻 #top/#content/#bot，#left（抽屉本体）与 #leftTgl（开合开关）留活。 */
  /* ---- P1-20b：按需创建的两族浮层的**语义层** ----
     P1-20 只做了机制层（inert 冻结 + Tab 环绕）：键盘走不出去了，但读屏仍然不知道
     「现在弹出的是一个对话框」。WAI-ARIA 要把三件事同时做到才算达标：
       ① role="dialog" + aria-modal="true" —— 宣告「这是个模态对话框」；
       ② 可访问名（aria-label）—— 宣告「这是什么框」（没名字的框读屏只会念「对话框」）；
       ③ 打开时焦点移入 / 关闭时归还 —— 落点不丢（WCAG 2.4.3 Focus Order）。
     对照：#lb / #cmp / #setPanel / #hintPanel 四个**常驻**浮层在 index.html 里早已带全
     role/aria-modal（本批在同批门禁里加了回归断言，防止后人把它删掉）。

     ⚠️ 为什么在这里收口：这两族浮层的开合点共 20+ 处（bigDialog 11 处调用点 +
     catInputDialog / confirmDialog / tmplDialog 9 处），逐个加必漏 —— 与 attachInert 同款理由。
     复用**同一个** MutationObserver（不新增监听器：新增 observer = 新增自激与竞态面）。

     ⚠️ 四条不能违反的边界（每一条都对应一次真会踩的坑）：
       1. **只在焦点还没进框时才搬**。catInputDialog 自己 setTimeout(inp.focus,0)、
          confirmDialog 自己 mask.focus() —— 我们再搬一次会把输入框踢掉（用户打不了字）。
       2. **判「进了框」要用内层盒子，不能用外层 mask**。confirmDialog 把焦点放在 mask 自身，
          按 host 判就会认为「已经在里面了」⇒ 那条路径永远搬不动。
       3. **归还前必须已经解冻、且只在焦点确实丢了时才归还**。焦点目标在 #main 里，
          而 #main 正被 inert 冻着 ⇒ focus() 静默失败落回 BODY（G2·V16③ 踩过）；
          反过来，若调用方自己把焦点放到了别处（settings.js 有 lastFocus 逻辑），不要抢。
          sync() 的顺序天然满足前一半：先 apply(解冻) 再 syncDialog(归还)。
       4. **父层遮罩的判定不能按 `.on`**：这两族没有 `.on`，生命周期就是「在不在 DOM 里」。 */
  const dlgBox = (host) => host.querySelector(".bigBox, #catInputBox") || host;
  const dlgVisible = (el) => !el.disabled && el.tabIndex >= 0 && el.offsetParent !== null;
  const dlgPick = (host) => {
    const all = [...host.querySelectorAll("a[href],button,input,select,textarea,[tabindex]")]
      .filter(dlgVisible);
    /* 优先级：文本控件（用户多半要打字 / 选模板）> 标题栏之外的第一个按钮 > 全体第一个。
       刻意跳过 .bhd 里的「关闭」按钮 —— 它是兜底出口，不是主操作。 */
    return all.filter(el => el.matches("input,select,textarea"))[0]
        || all.filter(el => !el.closest(".bhd"))[0]
        || all[0] || null;
  };
  let dlgHost = null, dlgBack = null;      // 当前已语义化的浮层 / 打开前的焦点落点
  const syncDialog = () => {
    const host = document.querySelector(".bigMask") || $id("catInputMask");

    if(!host){                             // ① 浮层没了 ⇒ 归还焦点（且只在确实丢了时）
      if(!dlgHost) return;
      const a = document.activeElement;
      if(dlgBack && dlgBack.isConnected && (!a || a === document.body || !a.isConnected)){
        try{ dlgBack.focus(); }catch(e){}
      }
      dlgBack = null; dlgHost = null;
      return;
    }
    if(host === dlgHost) return;           // ② 还是同一个框 ⇒ 幂等返回（sync 会被反复调用）
    dlgHost = host;                        // ③ 新框（首开，或「关旧开新」同一拍）⇒ 接管

    /* 记「打开前的落点」。连续弹窗（bigDialog 先 close 旧的再建新的）在同一拍完成，
       此时 activeElement 已是失效节点 ⇒ 保留上一个有效值，别把焦点还给 BODY。 */
    const a0 = document.activeElement;
    if(a0 && a0 !== document.body && a0.isConnected) dlgBack = a0;

    const box = dlgBox(host);
    if(!box.hasAttribute("role")) box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");
    if(!box.hasAttribute("aria-label") && !box.hasAttribute("aria-labelledby")){
      const h = box.querySelector(".cit, .bhd b, .bhd");
      const t = ((h && h.textContent) || "").trim();
      if(t) box.setAttribute("aria-label", t);   // 没名字的 dialog，读屏只会念「对话框」
    }

    /* ④ 让一拍再搬：调用方常在同一拍里自己 focus ⇒ 我们晚一步判定就不会抢（边界 1）。 */
    setTimeout(() => {
      if(!host.isConnected || dlgHost !== host) return;
      const a = document.activeElement;
      if(box.contains(a)) return;                // 焦点已在对话框内容里 ⇒ 不抢
      const tgt = dlgPick(host);
      if(tgt){ try{ tgt.focus(); }catch(e){} return; }
      if(!box.hasAttribute("tabindex")) box.setAttribute("tabindex", "-1");
      try{ box.focus(); }catch(e){}              // 一个可聚焦元素都没有 ⇒ 让容器自己可聚焦
    }, 0);
  };
  const sync = () => {
    const full = modalOpen();
    const dr = !full && drawerOpen();
    apply($id("top"), full || dr);
    apply($id("main"), full);
    apply($id("left"), full);
    apply($id("content"), full || dr);
    apply($id("bot"), full || dr);
    syncDialog();                                // P1-20b：语义层（排在解冻之后 ⇒ 归还焦点才生效）
  };
  let queued = false;
  const flush = () => { queued = false; sync(); };
  const schedule = () => {                            // 卡片逐张 append 时 class 变化极密集 ⇒ 合并
    if(queued) return;
    queued = true;
    requestAnimationFrame(flush);
    setTimeout(flush, 60);
  };
  new MutationObserver(schedule).observe(document.body || document.documentElement,
    /* ⚠️ 只盯 class：我们只写 inert，不在过滤列表里 ⇒ 不会自激。
       `hidden` 不必观察 —— 四个浮层的开合状态一律走 `.on`。 */
    {childList:true, subtree:true, attributes:true, attributeFilter:["class"]});
  /* G2·V16③：把 sync 暴露出去 —— 关闭浮层时必须**同步**解冻，不能等 MutationObserver 的
     下一拍：触发元素就在被冻结的 #content/#main 里，`inert` 子树的元素聚焦不了，
     归还焦点会静默失败（实测：settings 的 lastFocus 逻辑照跑，焦点仍回到 BODY）。 */
  window.mvSyncInert = sync;
  sync();                                             // 初始态：无浮层 ⇒ 不冻
}
attachInert();

/* G2·V16③：浮层 Tab 环绕陷阱。
   背景已经被 G1·V38 的 inert 挡住了（Tab 到不了 #top/#main），但焦点仍会从**末位元素**
   「掉到 body」—— 实测 #cmp（只有 2 个可聚焦元素）12 次 Tab 漏出 4 次，读屏上下文随即丢失。
   所以这里按 WAI-ARIA 对话框惯例把首尾接起来。#lb/#cmp 用；#setPanel/#hintPanel 自带等价逻辑。 */
function attachTrap(hostId){
  const host=document.getElementById(hostId);
  if(!host) return;
  host.addEventListener("keydown",e=>{
    if(e.key!=="Tab") return;
    const f=[...host.querySelectorAll("a[href],button,input,select,textarea,[tabindex]")]
      .filter(el=>!el.disabled && el.tabIndex>=0 && el.offsetParent!==null);
    if(!f.length) return;
    const first=f[0], last=f[f.length-1], a=document.activeElement;
    if(e.shiftKey && (a===first || !host.contains(a))){ e.preventDefault(); last.focus(); }
    else if(!e.shiftKey && (a===last || !host.contains(a))){ e.preventDefault(); first.focus(); }
  },true);
}
attachTrap("lb"); attachTrap("cmp");

/* P1-20 续：**按需创建**的两族浮层也要 Tab 环绕。
   ---------------------------------------------------------------------
   实测（dev/p1b_probe_tab.py，CDP Input.dispatchKeyEvent 真按键）：
     .bigMask        浮层内只有 1 个可聚焦元素（关闭按钮）
                     ⇒ Tab 12 次：浮层内 6 / 掉到 BODY 6，序列 BUTTON↔BODY 反复横跳
     #catInputMask   2 个按钮 ⇒ Tab 12 次：浮层内 9 / 掉到 BODY 3
   ⚠️ 注意口径：这两处**没有**漏到背景可操作控件（背景已被 P1-20 的 inert 挡住，
     实测「落在 #main 内」= 0）。掉到 BODY 是**焦点逃出文档**，读屏上下文丢失 ——
     与 G2·V16③ 给 #lb/#cmp 修的是同一类问题，所以这里复用同一套 WAI-ARIA 环绕做法。

   为什么不能用 attachTrap(id)：那两个节点是**开合时创建/销毁**的，
   attachTrap 在加载期跑，那时它们还不存在 ⇒ getElementById 返回 null 直接 return。
   所以改为**委托到 document 的捕获阶段**：不依赖节点何时出现，只在 Tab 按下时
   现查「当前哪个浮层开着」，再在它内部做首尾环绕。
   ⚠️ 必须带 `!document.querySelector` 空判 —— 没有浮层时绝不能拦 Tab。 */
(function attachDynamicOverlayTrap(){
  const DYNAMIC = [".bigMask", "#catInputMask"];
  document.addEventListener("keydown", e => {
    if(e.key !== "Tab") return;
    let host = null;
    for(const sel of DYNAMIC){
      const el = document.querySelector(sel);
      if(el){ host = el; break; }
    }
    if(!host) return;
    const f = [...host.querySelectorAll("a[href],button,input,select,textarea,[tabindex]")]
      .filter(el => !el.disabled && el.tabIndex >= 0 && el.offsetParent !== null);
    if(!f.length) return;
    const first = f[0], last = f[f.length - 1], a = document.activeElement;
    /* 焦点已在浮层内 ⇒ 只在首尾接环；焦点不在浮层内（或等于 body）⇒ 拉回首/末位。
       ⚠️ 用 closest 判「是否在浮层内」，别用 === 比 host —— 浮层内的子元素也算在内。 */
    const inside = host.contains(a) && a !== host;
    if(e.shiftKey && (!inside || a === first)){ e.preventDefault(); last.focus(); }
    else if(!e.shiftKey && (!inside || a === last)){ e.preventDefault(); first.focus(); }
  }, true);
})();

/* ε-2（2026-09-20）：**当前最顶层的全屏浮层**是哪一个。

   为什么需要它：`setupSettingsPanel` 的 Esc 处理注册在**捕获阶段**并 `stopPropagation()`
   （settings.js 的焦点陷阱需要这么写），于是抽屉开着时 Esc 到不了任何别的处理器。
   后果是：抽屉上再盖一层浮层（比如数据页的「清除单图暂存」二次确认 /
   `.bigMask` 系列）时，按 Esc 关不掉上面那层，反而把抽屉关走了。
   **Esc 应该只作用于最顶层** —— 这条判断必须有一个共用的真相来源，
   不能每个模块各写一套 z-index 比较。

   判据沿用 `attachInert` 的既有口径（同一份清单、同一个「DOM 靠后即更上层」假设）：
     常驻容器 #lb / #cmp / #setMask / #hintMask —— 用 `.on` 表示开合；
     按需创建 .bigMask / #catInputMask —— 在 DOM 里就算开着。
   ⚠️ 取「文档中排最后的」而不是「先遍历到的」：后者会把抽屉判成顶层，
     恰好就是我们要修的那个错。 */
window.mvTopOverlay = function mvTopOverlay(){
  const found = [];
  for(const id of ["lb","cmp","setMask","hintMask"]){
    const e = document.getElementById(id);
    if(e && e.classList.contains("on")) found.push(e);
  }
  for(const sel of [".bigMask","#catInputMask"]){
    const e = document.querySelector(sel);
    if(e) found.push(e);
  }
  if(!found.length) return null;
  if(found.length === 1) return found[0];
  /* 谁在文档里靠后谁更上层（这些浮层同一层叠上下文、都不用 z-index 直接盖 z-* 令牌）。
     ⚠️ 排序方向：「b 在 a 之后」⇒ a 排前面（-1），成升序；**取最后一个**才是最顶层。
     写成 +1 就反了 —— 会拿到最底层那个，恰好是要修的那个错。 */
  found.sort((a,b)=>(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1);
  return found[found.length-1];
};

/* ε-2（2026-09-20）：复制文本 —— 带 `execCommand` 降级。

   `navigator.clipboard` 只在**安全上下文**（https / localhost / 127.0.0.1）里存在。
   本机局域网访问（`http://192.168.x.x`）下它是 `undefined`，原来的裸调用会抛
   「Cannot read properties of undefined」—— 而 LAN 恰恰是最可能用到「复制提示词」的场景。
   （LAN 目前是 reserved 功能，但它正是这条降级存在的理由。）

   降级顺序：新 API → 隐藏 textarea + execCommand("copy") → 都不行就抛错，
   由调用点照旧弹失败提示。⚠️ 用完**必须还原用户原来的选区**，
   否则用户在网格里批量选中后一复制，选区就被清掉了。 */
window.mvCopyText = async function mvCopyText(text){
  const t = String(text == null ? "" : text);
  if(navigator.clipboard && window.isSecureContext){
    try{ await navigator.clipboard.writeText(t); return; }catch(e){ /* 落到下面的降级 */ }
  }
  const ta = document.createElement("textarea");
  ta.value = t;
  ta.setAttribute("readonly","");
  ta.style.cssText = "position:fixed;top:-1000px;left:-1000px;opacity:0";
  document.body.appendChild(ta);
  const sel = document.getSelection();
  const prev = (sel && sel.rangeCount) ? sel.getRangeAt(0) : null;
  ta.select(); ta.setSelectionRange(0, ta.value.length);   // iOS 上光 select() 不够
  let ok = false;
  try{ ok = document.execCommand("copy"); }catch(e){ ok = false; }
  ta.remove();
  if(prev && sel){ try{ sel.removeAllRanges(); sel.addRange(prev); }catch(e){} }
  if(!ok) throw new Error("浏览器拒绝了复制权限");
};
