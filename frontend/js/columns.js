/* ComfyMediaViewer · js/columns.js（V10 · 批次 D3，2026-09-18）
 *
 * 列表视图「列管理」：列显隐（表头右键）+ 列宽拖拽（表头右缘）+ 持久化。
 *
 * 设计要点
 * ────────
 * 1. **单一真相源**：列宽由 CSS 变量 `--lc-<col>-w` 提供，写在这两个地方——
 *    ① `document.body` 的行内 style（`#listHead` 与 `#grid` 都是它的后代，读的是同一个值）；
 *    ② 不用 JS 去改每一行的 flex —— 那样每加一列就要改两处，且拖拽时逐行写样式会重排。
 *    ⚠️ 原文此处误写成「`#listHead` 的行内 style」—— 正是这句错的说明把 V10 首版带进了坑
 *       （变量只在**后代**方向继承，而数据行是 `#listHead` 的**兄弟**）。
 *       真实写者见下方 `const root = document.body;` 的注释与 V4-D3 门禁 B4/B5/B12。
 * 2. **宽度单位**：变量值是「px 基数」，CSS 侧统一写 `calc(var(--lc-X-w)*var(--fsx))`，
 *    因此字号档位（--fsx）缩放仍然生效，拖出来的宽度是「逻辑宽」，换字号不跑偏。
 * 3. **拖拽**：pointerdown 在 handle 上 → setPointerCapture（指针移出表头也不丢事件）
 *    → pointermove 只写 CSS 变量（不碰 DOM 结构） → pointerup 落盘。
 *    用 `pointercancel` 一并收口，避免拖到一半切窗口把 body 的 dragging 类留死。
 * 4. **显隐**：走 `popupMenu`（全站卡片右键同一套菜单组件），列名前带「✓ / 空」。
 *    ⚠️ 不允许把「文件名」和「操作」两列藏掉 —— 藏了之后列表就既不能认也不能点了。
 * 5. **与窄屏 @media 的关系**：媒体查询里的 `display:none`（逐列收起）与用户的
 *    手动隐藏是两件事，靠 `.lch-off` 上的 `!important` 保证「用户主动藏的一定藏」，
 *    而媒体查询该收的照收（用户没主动开的话）。
 * 6. **持久化**：localStorage `mv.lcols` = `{w:{col:px}, off:[col,…]}`。
 *    与 mv.view / mv.dens / mv.sidecol 同族（纯前端界面偏好，不入服务端 settings.json）。
 * 7. **可调宽范围**：除 `ro:"all"` 的三列（name 弹性 / fav 走 --star-w / acts 操作入口）外，
 *    **其余 8 列全部可拖宽**，`clamp(36, 460)` 为硬护栏。E5-4b（2026-09-23）把缩略图列
 *    从"被守卫跳过"改为普通列 —— 见 COLS 上方注释与门禁 `v4_batchD3_check` B9 / B9b。
 */
"use strict";
(function initListColumns(){
  const KEY = "mv.lcols";
  /* 列定义：与 index.html 的 #listHead 子元素一一对应。
     w  = 出厂宽度（px 基数，与 CSS 里的默认值一致）
     ro = **锁定的操作**（省略 = 可拖宽 + 可隐藏）
          "all" = 不可拖宽 **且** 不可隐藏
     ⚠️ `ro` 的取值写的是「被禁掉的能力」，不是「该列支持什么」—— 这一点极易读反，
        历史上就栽过一次（见下）。
     ⚠️ E5-4b（2026-09-23）：缩略图列原本写作 `{c:"thumb", ro:"resize"}`。值名 `"resize"`
        字面像「**支持**拖宽」，而五处守卫全部把它当「**禁止**拖宽」用
        （`ro === "resize" || ro === "all"` → skip）⇒ 命名与行为互相矛盾，
        后果是：① 该列既拖不动、也不进 `mvListCols()` 快照，`w:62` 成了**永不生效的幽灵值**；
        ② E5-4 修好的「表头与数据行同读 `--lc-thumb-w`」在用户侧**一个入口都没有**。
        复核后**放开**：删掉该列的 `ro`（与普通列同权 —— 可拖宽、可隐藏），
        `--lc-thumb-w` 这才真正可被用户改，「单一真相源」有了用户可见价值。
        ⛔ **隐藏能力刻意保留** —— 缩略图是列表行的视觉锚点，但「藏掉它」今天就能用，
           且 layout.css 的 `.card.thumb-off>img`（P1-22 专门修的「藏行内主图」）还在守它，
           改成不可隐藏等于**删掉一个既存功能**。 */
  const COLS = [
    { c:"thumb",   el:"lh-thumb",   w:62  },             // E5-4b：原 ro:"resize" 语义矛盾，已放开（可拖宽 + 可隐藏）
    { c:"name",    el:"lh-name",    w:0,   ro:"all" },
    { c:"type",    el:"lh-type",    w:46  },
    { c:"dim",     el:"lh-dim",     w:88  },
    { c:"size",    el:"lh-size",    w:72  },
    { c:"time",    el:"lh-time",    w:120 },
    { c:"model",   el:"lh-model",   w:180 },
    { c:"sampler", el:"lh-sampler", w:110 },
    { c:"fav",     el:"lh-fav",     w:0,   ro:"all" },   // 宽度走 --star-w
    { c:"tags",    el:"lh-tags",    w:150 },
    { c:"acts",    el:"lh-acts",    w:58,  ro:"all" },   // 操作入口，不许藏
  ];
  const NAME  = { thumb:"缩略图", name:"文件名", type:"类型", dim:"尺寸", size:"大小",
                  time:"修改时间", model:"模型", sampler:"采样器", fav:"收藏",
                  tags:"标签", acts:"操作" };
  const MIN_W = 36, MAX_W = 460;

  const head = document.getElementById("listHead");
  if(!head) return;
  const grid = document.getElementById("grid");
  /* ⚠️ V10 首轮门禁抓出的真问题（B4/B5/B12）：
     `--lc-*-w` 若写在 `#listHead` 上，**数据行读不到** —— `#grid .card .lv-*` 是
     #listHead 的兄弟子树，而 CSS 自定义属性只在**后代**方向继承，不跨兄弟。
     实测：拖「大小」列后表头 132px / 数据行仍是 fallback 72px，左缘差 60px。
     修法：挂在**共同祖先 body** 上（#listHead 与 #grid 都是它的后代）⇒ 一处写、两处读。 */
  const root = document.body;

  /* ---------- 批次 C · P1（2026-10-06）：数据行贴批的规模止血 ----------
     病灶：applyOff() 每次都对**整个网格**做 querySelectorAll("[data-col]") 与
     querySelectorAll(".card")，而它被下方 MutationObserver 在**每批 100 张卡片插入后**触发。
     7,484 张规模 ⇒ 6 万节点/次 × 74 批 ≈ **450 万次节点操作**，全在渲染主线程。
     讽刺的是：网格视图里那两段遍历**完全无意义** ——
       · [data-col] 节点只由 gallery.js 的**列表行**模板产出，网格卡片里一个都没有；
       · .thumb-off 的 CSS 只写在 `body.list-view #grid .card.thumb-off>img` 下。
     ⇒ 两条修法叠加：
       ① **视图短路**：不在列表视图就整段跳过（网格视图成本归零）；
       ② **增量贴批**：列表视图下只处理本批新增的节点（_newNodes），不再全量重扫。
     ⚠️⚠️ 为什么用**模块级变量** _newNodes 而不是给 applyOff 加第二个参数：
       门禁 `tests/p1bfix_batch_b_check.py:228` 直接用 `str.find` 找 applyOff 的
       **精确单参签名**（该字符串字面在下面函数定义处），再取其后 1400 字符
       断言 thumb-off / lch-off；`tests/pAfix_e5_check.py` 用大括号配平取整个函数体
       断言 thumb 分支。⇒ 加参数、抽函数、或**在本注释里写出那串签名字面**
       （`str.find` 会先命中注释，窗口整体前移 ⇒ 三条断言一起假红）
       都会让门禁失效。签名、函数体形态、关键子串位置**一律保持原样**，
       只做「外层加守卫 + 换遍历根」。 */
  const _inListView = () => document.body.classList.contains("list-view");
  let _newNodes = null;   // 本批新增的节点（增量贴批用）；null ⇒ 全量

  /* ---------- 状态 ---------- */
  let st = { w:{}, off:[] };
  try{
    const raw = JSON.parse(localStorage.getItem(KEY) || "{}") || {};
    if(raw && typeof raw === "object"){
      st.w   = (raw.w && typeof raw.w === "object") ? raw.w : {};
      st.off = Array.isArray(raw.off) ? raw.off.filter(x => typeof x === "string") : [];
    }
  }catch(_){ st = { w:{}, off:[] }; }
  const write = () => { try{ localStorage.setItem(KEY, JSON.stringify(st)); }catch(_){} };

  const byCol = c => COLS.find(x => x.c === c);
  const clamp = v => Math.max(MIN_W, Math.min(MAX_W, Math.round(v)));

  /* ---------- 应用 ---------- */
  function applyWidth(c){
    const def = byCol(c); if(!def || def.ro === "all") return;
    const v = st.w[c];
    if(typeof v === "number" && isFinite(v)) root.style.setProperty("--lc-" + c + "-w", clamp(v) + "px");
    else root.style.removeProperty("--lc-" + c + "-w");
  }
  function applyOff(cols){
    const off = new Set(st.off);
    /* 表头：按 el 类名 */
    for(const d of COLS){
      const node = head.querySelector("." + d.el);
      if(node) node.classList.toggle("lch-off", d.ro !== "all" && off.has(d.c));
    }
    /* 数据行：按 data-col（gallery.js / settings.js 已埋）。
       ⚠️ 缩略图列**没有** data-col 锚点 —— 它在列表行里就是卡片主图（card>img），
       不是 .lv-cell 兄弟元素，所以用卡片自身的 .thumb-off 类来收（见 layout.css）。
       P1-22 修复：此前只藏表头、行内主图还在 ⇒ 表头与数据行错开一格。 */
    if(grid && _inListView()){
      const _r = _newNodes || [grid];
      const thumbOff = d => d.ro !== "all" && off.has("thumb");
      const isOff = !!(thumbOff(COLS.find(x => x.c === "thumb") || {}));
      for(const _n of _r){
        (_n.matches("[data-col]") ? [_n] : _n.querySelectorAll("[data-col]"))
          .forEach(node => node.classList.toggle("lch-off", off.has(node.dataset.col)));
        (_n.matches(".card") ? [_n] : _n.querySelectorAll(".card"))
          .forEach(card => card.classList.toggle("thumb-off", isOff));
      }
    }
    head.classList.toggle("lch-has-off", st.off.length > 0);
  }
  function applyAll(){
    for(const d of COLS) applyWidth(d.c);
    // 出厂宽度一次性写进变量，保证「用户没动过」时也是显式值（便于门禁读到）
    for(const d of COLS){
      if(d.ro === "all") continue;
      if(typeof st.w[d.c] !== "number") root.style.setProperty("--lc-" + d.c + "-w", d.w + "px");
    }
    applyOff();
  }
  applyAll();

  /* C-P1：视图切换时补一次全量贴批（广播见 settings.js 的 setViewMode）。
     这是「网格视图短路 + 只维护新增节点」的配套兜底 —— 切到列表视图那一刻，
     卡片可能已被重建（新节点由下方观察器接管），也可能被复用（观察器只看 childList，
     看不到复用）⇒ 必须有这条显式补批。只在切视图时触发，不消耗任何可见帧。 */
  window.addEventListener("mv:viewchange", () => applyOff());

  /* ---------- 拖拽热区：给每个可调列的右缘挂 handle ---------- */
  const H = "lch-resizable";
  function mountHandles(){
    for(const d of COLS){
      if(d.ro === "all") continue;
      const node = head.querySelector("." + d.el);
      if(node) node.classList.add(H);
    }
  }
  mountHandles();

  /* V10 门禁可探的只读快照：把「用户改过的」与「当前生效值」都摊出来。
     ⚠️ 字段恒定为原始类型 —— JSON.stringify 会把 undefined 的键整个丢掉。 */
  window.mvListCols = () => {
    const out = { off: st.off.slice(), w: {} };
    for(const d of COLS){
      if(d.ro === "all") continue;
      const v = root.style.getPropertyValue("--lc-" + d.c + "-w");
      out.w[d.c] = v ? parseInt(v, 10) : (typeof st.w[d.c] === "number" ? st.w[d.c] : d.w);
    }
    return out;
  };
  window.mvListColsReset = () => { st = { w:{}, off:[] }; write();
    for(const d of COLS) root.style.removeProperty("--lc-" + d.c + "-w");
    applyAll(); };

  /* ---------- P1-25/26/27：收列断点随 --fsx 缩放 ----------
     问题（实测 · dev/p1b_probe.py，真实可达区间 85%~150%）：
       列宽是 `calc(NNpx * var(--fsx))` ⇒ 随字号放大而变宽，
       但收列断点写的是 `@media (max-width:1280/1080/900/760/620px)` —— **纯 px**，
       按「视口宽度」判，**完全不知道 --fsx 的存在**。
       于是「字号放大 ⇒ 列变宽 ⇒ 总宽超了」时，断点却一次都不触发。
     实测溢出（listHead.scrollWidth − clientWidth）：
       · 1024×768  115% 起异常，150% 溢出 **264px**
       · 1152×864  130% 起异常，150% 溢出 **151px**
       · 1366×768  135% 起异常，150% 溢出 **117px**
       · 1440×900  140% 起异常，150% 溢出  **43px**
       · 1280×800  145% 起异常，150% 溢出  **23px**
       · 1920/1600 全区间正常
     另有一种更隐蔽的坏法：model / tags 是 `flex:0 1`（可收缩）⇒ 溢出前会先被压到 **0 宽**，
     表头有字但**一格都看不见**（1440@140% 起 tags 0 宽、1366@135% 起 model+tags 0 宽）。

     ⚠️ 为什么不直接写 CSS：**媒体查询里不能用 var()**（CSS 规范：MQ 不支持自定义属性），
       容器查询同理。而 --fsx 是 `documentElement.style` 上的内联变量，也不改 html 字号，
       所以 em/rem 方案同样不成立。⇒ 只能在 JS 里算，把结果写成 body 上的档位类。

     做法：把「视口宽度」折算成「等效宽度 = 视口 / fsx」——
       等效宽度就是「这些列按 1.0 字号算需要多宽」，于是断点可以沿用原来的 px 阈值语义，
       只是判的对象从「视口」换成了「等效视口」。字号越大 ⇒ 等效视口越小 ⇒ 越早收列。
       阈值取原 @media 值：1280 / 1080 / 900 / 760 / 620。

     ⚠️ 别手算「等效宽度」—— 必须用**运行期实测的 window.innerWidth**：
       1440@150% 实测 innerWidth=1440（含经典滚动条），1440/1.5 = **950** ⇒ 只落 2 档
       （sampler + model：950<1080 且 950<1280，但 950>900 ⇒ rc-time 不收）。
       本文件初版注释误写成「等效 960 ⇒ 收 sampler/model/time」，是**把 1440 错记成 1425**
       再除以 1.5 得 960 所致 —— 且 960 仍 >900，无论如何都收不到 time。正确落点见
       tests/p1bfix_batch_b_check.py 的 B6（它按 innerWidth/fsx 独立重算，不拍数字）。 */
  const COLLAPSE_STEPS = [
    { eq: 1280, cls: "rc-sampler" },   // 收「采样器」
    { eq: 1080, cls: "rc-model" },     // 收「模型」
    { eq: 900,  cls: "rc-time" },      // 收「修改时间」
    { eq: 760,  cls: "rc-tags-type" }, // 收「标签」「类型」
    { eq: 620,  cls: "rc-dim-size" },  // 收「尺寸」「大小」
  ];
  const RC_ALL = COLLAPSE_STEPS.map(s => s.cls);
  function applyResponsiveCollapse(){
    let fsx = 1;
    try{
      const raw = getComputedStyle(document.documentElement).getPropertyValue("--fsx").trim();
      const n = parseFloat(raw);
      if(isFinite(n) && n > 0) fsx = n;
    }catch(_){}
    const eqw = window.innerWidth / fsx;
    /* ⚠️ 档位类写在 **documentElement（html）** 上，不是 body ——
       `root` 在本文件里是 `document.body`（列宽变量要挂在表头与数据行的共同祖先上），
       两处用途不同，别混用。CSS 侧的选择器也相应写成 `html.rc-*`。 */
    const he = document.documentElement;
    /* 逐档显式设置（幂等）—— 不写「仅当 X 才加」的分支，避免任何路径漏撤类 */
    for(const s of COLLAPSE_STEPS){
      he.classList.toggle(s.cls, eqw < s.eq);
    }
    he.classList.toggle("rc-active", RC_ALL.some(c => he.classList.contains(c)));
  }
  applyResponsiveCollapse();
  /* C-P4：rAF 节流 —— 这是全项目最贵的 resize 回调（getComputedStyle(documentElement)
     强制样式重算 + 向 documentElement 连写 5 次 classList）。
     ⚠️ 下方 MutationObserver 仍直接调未节流的 applyResponsiveCollapse —— 它只在
        --fsx（字号滑块）变化时触发，是低频人工操作，不需要合帧。 */
  window.addEventListener("resize", (window.mvThrottleRaf || (f => f))(applyResponsiveCollapse), { passive: true });
  /* --fsx 由设置面板写在 documentElement 的**内联样式**上 ⇒ 观察 style 属性即可。
     ⚠️ attributeFilter:["style"] —— 我们只写 body 的 class，不在过滤列表里 ⇒ 不自激。 */
  new MutationObserver(applyResponsiveCollapse).observe(document.documentElement,
    { attributes: true, attributeFilter: ["style"] });
  /* 供门禁/探针调用（也方便设置面板改完字号主动重算一次） */
  window.mvResponsiveCollapse = applyResponsiveCollapse;

  /* ---------- 拖拽实现 ---------- */
  let drag = null;
  head.addEventListener("pointerdown", e => {
    const t = e.target;
    if(!t || !t.classList || !t.classList.contains("lh")) return;
    const col = t.dataset.colResize || (function(){
      for(const d of COLS) if(t.classList.contains(d.el)) return (d.ro === "all") ? null : d.c;
      return null;
    })();
    if(!col) return;
    /* 只有在右缘 ~6px 内按下才算拖拽（中间留给未来可能的点排序） */
    const r = t.getBoundingClientRect();
    if(Math.abs(e.clientX - r.right) > 7 && e.clientX < r.right - 14) return;
    e.preventDefault();
    const startW = (function(){
      const v = root.style.getPropertyValue("--lc-" + col + "-w");
      return v ? parseInt(v, 10) : byCol(col).w;
    })();
    drag = { col, x0: e.clientX, w0: startW, fs: getFsx() };
    document.body.classList.add("lch-dragging");
    try{ t.setPointerCapture(e.pointerId); }catch(_){}
    drag.node = t; drag.pid = e.pointerId;
  });

  function getFsx(){
    const v = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--fsx"));
    return (isFinite(v) && v > 0) ? v : 1;
  }

  function onMove(e){
    if(!drag) return;
    /* 像素位移 ÷ 字号系数 = 逻辑宽位移（变量是逻辑值，乘 --fsx 后才是屏幕宽） */
    const next = clamp(drag.w0 + (e.clientX - drag.x0) / drag.fs);
    root.style.setProperty("--lc-" + drag.col + "-w", next + "px");
    st.w[drag.col] = next;
  }
  function onUp(){
    if(!drag) return;
    try{ if(drag.node && drag.pid != null) drag.node.releasePointerCapture(drag.pid); }catch(_){}
    document.body.classList.remove("lch-dragging");
    drag = null; write();
  }
  window.addEventListener("pointermove", onMove);
  window.addEventListener("pointerup", onUp);
  window.addEventListener("pointercancel", onUp);
  /* 指针离开窗口 / 失焦：指针事件可能收不到 up，兜底收口（否则 dragging 类留死） */
  window.addEventListener("blur", onUp);

  /* ---------- 表头右键：列显隐菜单 ---------- */
  head.addEventListener("contextmenu", e => {
    const t = e.target;
    const th = (t && t.closest) ? t.closest(".lh") : null;
    if(!th) return;
    e.preventDefault(); e.stopPropagation();
    const off = new Set(st.off);
    const items = COLS.filter(d => d.ro !== "all").map(d => ({
      icon: off.has(d.c) ? "" : "check",
      label: NAME[d.c] + (off.has(d.c) ? "（已隐藏）" : ""),
      fn: () => {
        const next = new Set(st.off);
        if(next.has(d.c)) next.delete(d.c); else next.add(d.c);
        st.off = [...next]; applyOff(); write();
        toast((next.has(d.c) ? "已隐藏「" : "已显示「") + NAME[d.c] + "」列", "ok");
      },
    }));
    items.push({ label:"恢复默认列宽与显隐", fn:() => { window.mvListColsReset(); toast("已恢复默认列设置","ok"); } });
    if(typeof popupMenu === "function"){
      popupMenu(e.clientX, e.clientY, items);
    }else{
      /* 兜底：popupMenu 不在（加载顺序/裁剪版）时至少能用右键切换「模型」列 */
      toast("列菜单组件不可用", "err");
    }
  });

  /* 列表视图重建后，数据行是新的 —— 把隐藏态重新贴上去（表头是常驻节点，不用管） */
  if(typeof MutationObserver === "function" && grid){
    let queued = 0;
    new MutationObserver(recs => {
      /* P1 增量（批次 C）：只收集本批新增的节点，交给 applyOff 做增量贴批。
         ⚠️ 收集必须在 `if(queued) return` **之前** —— 同一帧可能回调多次，
            提前 return 会把后几批的新增节点丢掉（那批卡片就永远贴不上隐藏态）。
         ⚠️ 网格视图不收集：applyOff 在那时整段短路，收了也没人用。 */
      if(_inListView()){
        for(const _rec of recs) for(const _n of _rec.addedNodes){
          if(_n.nodeType === 1) (_newNodes || (_newNodes = [])).push(_n);
        }
      }
      if(queued) return; queued = 1;
      /* rAF 在页面不可见时不触发（headless 门禁会挂）⇒ setTimeout 兜底。
         ⚠️ queued 当状态机用：两条路都会到点，原写法 `queued = 0; applyOff()`
            会**连跑两次** —— 第二次是 _newNodes 清空后的全量重扫，纯浪费。
            改成「只有把 queued 从 1 改成 0 的那次才执行」，兜底那次自然跳过。 */
      const run = () => { if(queued !== 1) return; queued = 0; applyOff(); _newNodes = null; };
      requestAnimationFrame(run); setTimeout(run, 60);
    }).observe(grid, { childList:true });
  }
})();
