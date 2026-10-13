  // ============ 窗口控制逻辑 ============
  // 本页是 Tauri 顶层页，__TAURI__ 一定可用、IPC 可靠；
  // 应用在跨源 iframe 里拿不到窗口 API，所以拖拽/最大化由它 postMessage 过来、这里执行。
  (function () {
    var T = window.__TAURI__;
    var frame = document.getElementById('app-frame');
    var boot = document.getElementById('boot');
    var box = document.getElementById('winctrls');

    if (!T) { console.warn('[winctrls] __TAURI__ 未注入，窗口按钮不可用'); return; }

    // 取当前窗口句柄：兼容不同 Tauri 2.x 导出路径
    var win = null;
    try {
      var wm = T.window || T.webviewWindow || (T.core && T.core.window);
      win = wm ? (wm.getCurrentWindow ? wm.getCurrentWindow()
              : (wm.getCurrentWebviewWindow ? wm.getCurrentWebviewWindow()
              : (wm.getCurrent ? wm.getCurrent() : null))) : null;
    } catch (e) { console.error('[winctrls] getWindow error', e); }
    if (!win) { console.warn('[winctrls] 取不到窗口实例，窗口按钮不可用'); return; }

    var isMax = function () { try { return Promise.resolve(win.isMaximized()); } catch (e) { return Promise.resolve(false); } };
    var toggleMax = function () {
      isMax().then(function (m) { try { m ? win.unmaximize() : win.maximize(); } catch (_) {} }).catch(function () {});
    };
    var drag = function () { try { win.startDragging(); } catch (e) { console.error('[winctrls] drag', e); } };

    // 拖拽热区：
    //   - 启动遮罩（后端未就绪时也能拖动窗口）
    //   - 应用顶栏的空白区：app.js 发 mv:drag 过来，下方 message 监听里执行
    // 三个按钮只负责点击，不参与拖动。
    if (boot) {
      boot.addEventListener('mousedown', function (e) { if (e.button === 0) drag(); });
      boot.addEventListener('dblclick', function () { toggleMax(); });
    }

    // 三个窗口按钮
    var btns = box.querySelectorAll('button');
    for (var i = 0; i < btns.length; i++) {
      (function (btn) {
        var act = btn.getAttribute('data-act');
        btn.addEventListener('mousedown', function (e) { e.stopPropagation(); });
        btn.addEventListener('click', function (e) {
          e.stopPropagation();
          try {
            if (act === 'minimize') win.minimize();
            else if (act === 'maximize') toggleMax();
            else if (act === 'close') win.close();
          } catch (err) { console.error('[winctrls]', act, err); }
        });
      })(btns[i]);
    }

    // 来自应用 iframe 的消息：拖拽 / 双击最大化 / 顶栏实高
    // ⚠️ E8-1（2026-09-23，审计 §6.7-7-8）：两道闸 —— ① 来源窗口必须是本 iframe；
    //    ② 来源 origin 必须与 iframe **当前** src 同源。
    //    只用 ① 是**挡不住**的：WindowProxy 跨导航存活 ⇒ iframe 一旦被导航到别的源，
    //    `ev.source !== frame.contentWindow` **依然为假**，那个外部页面就能直接调
    //    drag() / toggleMax()（两者都**无参数**、不需要任何内部约定）。
    //    期望 origin **从 frame.src 推导**：端口是运行期决定的（绝不能硬编码），
    //    也**不能**放宽成「是 localhost 就放行」—— 同机任意本地服务都是 localhost。
    var frameOrigin = function () {
      try {
        var s = frame && frame.src;
        if (!s || s === 'about:blank') return '';
        return new URL(s, location.href).origin;
      } catch (e) { return ''; }
    };
    window.addEventListener('message', function (ev) {
      if (!frame || ev.source !== frame.contentWindow) return;   // ① 只认自己的 iframe
      var exp = frameOrigin();
      if (!exp || ev.origin !== exp) {                           // ② 且必须与它同源
        console.warn('[winctrls] 忽略非同源消息：' + ev.origin);
        return;
      }
      var d = ev.data || {};
      if (d.mv === 'drag') drag();
      else if (d.mv === 'max') toggleMax();
      else if (d.mv === 'bar-h') {
        // E8-2（2026-09-23，审计 §6.7-7-10）：三验 —— typeof / isFinite / 上限。
        //    原写法 `d.mv === 'bar-h' && d.h > 0` 只做**隐式转换**：`"500"` 能用、
        //    `NaN` / `Infinity` 静默失效，而 `1e9` 会把三个窗口按钮撑到 1e9px 高、
        //    顶栏被整块盖住（触发门槛极低）。
        //    上限取 `window.innerHeight`：按钮与顶栏**同一行**，顶栏高不可能超过整个
        //    视口 —— 超出即「上报方出错或伪造」，照收只会毁掉布局。
        var h = d.h;
        if (typeof h === 'number' && isFinite(h) && h > 0 && h <= window.innerHeight) {
          document.documentElement.style.setProperty('--winctrl-h', Math.round(h) + 'px');
        }
      }
    });

    // 最大化状态 → 按钮提示语
    var syncTip = function () {
      isMax().then(function (m) {
        var b = box.querySelector('.max');
        if (b) { b.title = m ? '还原' : '最大化'; b.setAttribute('aria-label', b.title); }
      }).catch(function () {});
    };
    if (T.event && T.event.listen) T.event.listen('tauri://resize', syncTip);
  })();

  // ============ 启动动画：粒子汇聚 + 星点底生成 ============
  // 光点粒子用纯 CSS 动画飞行（不引任何图片资源，零新增体积）。
  // 数量按屏幕尺寸与核心数自适应：低配少放，避免 box-shadow 开销堆叠。
  (function () {
    var field = document.getElementById('bootField');
    if (!field) return;

    var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduced) return;                       // 降级模式不放粒子，只留静态品牌块与光环

    var cores = (navigator.hardwareConcurrency || 4);
    var wide = Math.max(window.innerWidth, window.innerHeight);
    var small = Math.min(window.innerWidth, window.innerHeight);
    var n = wide >= 2000 ? 64 : (wide >= 1400 ? 56 : (wide >= 900 ? 46 : 36));
    if (cores <= 4) n = Math.min(n, 30);       // 低核心数减量，保流畅

    // 粒子配色：品牌钢蓝为主，青色与近白点缀（与光环/扫描线同族）。
    var pal = ['#4f7cff', '#6f9bff', '#3fb8d8', '#8fa8ff', '#c8d6ff'];

    var frag = document.createDocumentFragment();
    var i, d, ang, r1, r2;
    // 汇聚粒子：起点在外环（短边 30%~52%），终点没入方块（26~66px）。
    // 纵向压扁 0.78 ⇒ 沿椭圆轨道汇聚，与扁窗构图匹配。
    for (i = 0; i < n; i++) {
      d = document.createElement('i');
      d.className = 'pt';
      ang = Math.random() * Math.PI * 2;
      r1 = small * (0.30 + Math.random() * 0.22);
      r2 = 26 + Math.random() * 40;
      d.style.setProperty('--s', (2 + Math.random() * 3).toFixed(1) + 'px');
      d.style.setProperty('--c', pal[i % pal.length]);
      d.style.setProperty('--sx', (Math.cos(ang) * r1).toFixed(0) + 'px');
      d.style.setProperty('--sy', (Math.sin(ang) * r1 * 0.78).toFixed(0) + 'px');
      d.style.setProperty('--ex', (Math.cos(ang) * r2).toFixed(0) + 'px');
      d.style.setProperty('--ey', (Math.sin(ang) * r2).toFixed(0) + 'px');
      d.style.setProperty('--dur', (2.6 + Math.random() * 1.8).toFixed(2) + 's');
      d.style.setProperty('--delay', (-Math.random() * 4.4).toFixed(2) + 's');  // 负延迟：一上来就是进行中
      frag.appendChild(d);
    }
    // 星点底：稀疏远星闪烁，拉开纵深（不参与汇聚）
    var ns = wide >= 1400 ? 26 : 18;
    for (i = 0; i < ns; i++) {
      d = document.createElement('i');
      d.className = 'star';
      ang = Math.random() * Math.PI * 2;
      r1 = small * (0.18 + Math.random() * 0.34);
      d.style.transform = 'translate(' + (Math.cos(ang) * r1).toFixed(0) + 'px,'
                                     + (Math.sin(ang) * r1 * 0.8).toFixed(0) + 'px)';
      d.style.setProperty('--td', (2.2 + Math.random() * 3).toFixed(2) + 's');
      d.style.setProperty('--delay', (-Math.random() * 4).toFixed(2) + 's');
      frag.appendChild(d);
    }
    field.appendChild(frag);
  })();

  // 启动页错误上报（main.rs 在后端失败时调用）
  window.__bootError = function (m) {
    var b = document.getElementById('boot');
    if (b) b.classList.add('failed');          // 停动画、复位静态可见
    var e = document.getElementById('bootErr');
    if (e) { e.textContent = m; e.classList.add('show'); }
    var t = document.getElementById('bootStage');
    if (t) t.textContent = '启动失败';
  };
  // iframe 真正加载完成后隐藏启动遮罩（保留 .hide 语义，淡出结束再 display:none）；
  // 同时给 iframe 加 .ready 淡入——与遮罩淡出交叉，交接不硬切。
  var fr = document.getElementById('app-frame');
  if (fr) fr.addEventListener('load', function () {
    var b = document.getElementById('boot'); if (!b) return;
    fr.classList.add('ready');
    b.classList.add('hide');
    setTimeout(function () { b.classList.add('gone'); }, 300);
  });
