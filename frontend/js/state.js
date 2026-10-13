/* ComfyMediaViewer · js/state.js（2026-09-09 从 index.html 拆出，加载顺序与原文一致） */
"use strict";
/* ================= JS 功能层（原版验证逻辑，P1-P5 收官版） ================= */
const $ = s => document.querySelector(s);
const grid = $("#grid"), lb = $("#lb"), stage = $("#stage"), meta = $("#meta");
const BATCH = 100;
/* 路径归一化：Windows 下大小写/斜杠不一致会让「删除失败」判定误报，比较前统一 */
const normPath = p => String(p || "").replace(/\//g, "\\").toLowerCase();
/* ---------- 媒体 URL 版本号（防「同名重建后仍显示旧图」） ----------
   浏览器对「同一个 URL」会直接拿缓存里的位图渲染，有时连请求都不发（内存缓存）。
   而 ComfyUI 删掉最后一张图后计数器会回退，重新生成的图往往落在**同一个文件名**上：
   URL 没变 → 主页一直显示已删除的旧图，只有 F5（重建文档、内存缓存清空）才恢复；
   大图是另一个 URL（/api/file）所以看着是对的。
   把 mtime+size 拼进查询串，内容一变 URL 就变，缓存自然失效（比全局禁用缓存更划算）。 */
const mediaVer = it => (it && it.mtime)
  ? (it.mtime + "-" + (it.size || 0)) : "";
function itemByPath(p){
  const k = normPath(p);
  for(const arr of [S.view, S.items]){
    if(!arr) continue;
    for(const x of arr) if(x && x.path && normPath(x.path) === k) return x;
  }
  return null;
}
/* it 缺省时按路径在内存列表里找回元数据（对比/擦除视图只拿到路径） */
function fileURL(p, it){
  const item = it || itemByPath(p);
  const v = mediaVer(item);
  return "/api/file?p=" + encodeURIComponent(p) + (v ? "&v=" + encodeURIComponent(v) : "") + CMV3.urlTarget(item);
}
function thumbURL(it, dpr){
  const v = mediaVer(it);
  /* V44（批次 G3）：dpr=2 走 640 档 —— /api/thumb 的 s 参数本就按尺寸进缓存键
     （sha1(p|mtime|size)），两档各存各的互不挤兑；缺省 320 与旧行为完全一致。
     实测（dev/uix_probe_20260927 M4）：320 档在 DPR=1 就已欠采样（213 vs 238），
     2K/4K 欠 1.7~2.2×。 */
  const s = dpr === 2 ? 640 : 320;
  return "/api/thumb?p=" + encodeURIComponent(it.path) + (v ? "&v=" + encodeURIComponent(v) : "") + "&s=" + s + CMV3.urlTarget(it);
}
/* cardEl：卡片 DOM 的反查表。F4 P2-3 —— 用 WeakMap（同 cardStar）而不是挂在
   item 上的 `it._cardEl`：后者在网格重建后仍指向已 detached 的旧卡，
   卡里的 <img>/<video> 又被 item 持有 ⇒ 反复切目录/改筛选会持续累积。
   WeakMap 以 item 为键、不阻止 GC，离屏卡自然回收。 */
let S = { root: "", path: "", view: [], shown: 0, idx: -1, hasThumb: false, curMeta: null, favCats: [], extraCats: [], expandDirs: false, cardStar: new WeakMap(), cardEl: new WeakMap(), stats: null, solo: null,
  /* ---------- H2（2026-10-07）筛选状态对象：幽灵控件清零 ----------
     原先有 4 个 `<select>` 把**筛选状态直接存在 DOM 里**（读 `.value` 判断当前条件）：
       `#filter`（类型）· `#favcat`（收藏分类）· `#wfClass`（工作流分类）· `#markState`（标记状态）
     病灶不是「它们存在」，而是「**状态寄存在隐藏 DOM 里**」—— 切目录 / 切视图时
     一旦哪条重置路径漏了某个控件，就会出现 **P1-9**（「在 A 目录勾了 .webp，切到 B 目录
     筛选还生效、张数莫名偏少」）。项目为此在 `app.js` 的 load() 里堆过两层兜底补偿。
     现在状态唯一来源是本对象；`#wfClass` / `#markState` 两个**仍会出现在特定视图里**的
     控件退化为**只读视图**（由 filters.js 的 syncFiltersUI() 单向推状态 → DOM），
     `#filter` / `#favcat` 两个**从不显示**的纯幽灵已从 DOM 删除。
     ⚠️ 写入口只有 filters.js 的 setTypeFilter / setFavCatFilter / setWfClassFilter /
        setMarkStateFilter 四个 setter —— 别处一律不许直接改这些字段。 */
  filters: { type: "all", favcat: "*", wfClass: "any", markState: "*" } };

/* ---------- 权威视图世代（ε-6 · E6-4）----------
   为什么需要它：原来**只有** `load()` 自己有一个 `loadTok` —— 于是「切目录」作废不了
   **别的**在途视图加载：
     · `extras.js` 的 `loadFavView()` await 回来就整片重写 `S.items` / `S.dirs` /
       `favMode` / 面包屑 ⇒ 慢的收藏夹响应会把**刚切到的目录**盖成收藏夹列表；
     · `extras.js` 的 `loadMetaMap()` await 回来就写 `S.metaMap` + 三个下拉 ⇒
       新目录的「模型 / 采样器 / 尺寸」里混进旧目录的值。
   两者都是「await 之后没有当前性检查就写共享状态」—— §6.2 竞态族的同一个病。

   用法（**单向权威**，不是都 ++）：
     · **权威视图加载**（`app.js` `load()` / `extras.js` `loadFavView()`）：
       开头 `const tok=++viewGen;` —— 它有权作废一切在途的视图加载；
     · **附属数据加载**（`extras.js` `loadMetaMap()`）：
       只做 `const g=viewGen;` 快照，await 后 `if(g!==viewGen) return "stale";`
       —— **不 ++**，它不该把权威加载踢掉。
   判据是「谁有权作废谁」。若两边都 ++，方向就反了：附属加载会把权威加载作废，
   用户切了目录却一直看不到新目录。
   ⚠️ 声明在 state.js（**最先加载**的文件），extras.js / app.js 都晚于它。 */
let viewGen = 0;

/* ---------- 单图查看（V2.10.7）----------
   S.solo 非空时，大图面板看的是「库外图片的临时副本」，形状是 { items:[单条], idx:0 }。
   特意**不复用** S.view / S.idx：那两个是网格与左栏选中态的真相，一旦被临时顶掉，
   关掉大图后网格会跳回错的位置，删除 / 收藏一类操作也可能落到别的条目上。
   凡是要「对当前这一张动手」的地方，一律读下面这三个取值函数。 */
const curList = () => (S.solo ? S.solo.items : S.view);
const curIdx  = () => (S.solo ? S.solo.idx  : S.idx);
const curItem = () => (curList()[curIdx()] || null);
/* ---------- 删除去向（设置项） ----------
   app    = 应用内回收站：删到 %LOCALAPPDATA%\ComfyMediaViewer\trash，可在顶栏「回收站」还原
   system = Windows 回收站：走系统回收站，在资源管理器里还原（软件内看不到条目）
   权威值存在服务端 settings.json（删除是后端执行的，光记在 localStorage 里
   别的窗口 / 桥接脚本读不到，会各删各的），前端启动时从 /api/info 同步过来，
   这里只做缓存 + 文案统一，避免同几句话在 5 个文件里各写一遍。 */
let DEL_MODE = "app";
/* F1 P2-4 / F3 P2-1：权威值是否已经从服务端同步成功。
   同步失败时 DEL_MODE 只是**本地的默认值**，界面上所有「文件将移入 X」
   的文案都可能与实际去向不符 ⇒ 各删除入口据此决定是否加一句说明。 */
let DEL_MODE_READY = false;
const delIsSystem = () => DEL_MODE === "system";
/* 删除确认框里「文件将移入 ……」的补语 */
const delTipWhere = () => delIsSystem()
  ? "Windows 回收站（可在资源管理器的回收站里还原）"
  : "应用内回收站（"+CMV3.trashRetention()+"，可在顶栏「回收站」中还原）";
/* 删除完成后的提示语 */
const delDoneMsg = () => delIsSystem() ? "已移入 Windows 回收站" : "已移入应用内回收站";
/* 名词简称，用于句子中间的「进 X」「移入 X」 */
const delShort = () => delIsSystem() ? "Windows 回收站" : "应用内回收站";
/* 左栏「库」当前主视图：""=目录浏览 / "workflow"=按生成参数筛目录 / "recent"=最近使用 / "marked"=已标记 */
let libMode = "";
const RECENT_LABELS = {preview:"预览", comfy:"打开 ComfyUI", download:"下载",
                       copy:"复制提示词", folder:"打开位置"};
const MARK_LABELS = {review:"待处理", publish:"待发布", compare:"待比较", delete:"待删除"};
let MARK_MAP = {};             // {路径: 状态}，来自 /api/stats 的 nav.mark_map
/* 本地刚写入的标记 {路径: {st, t}} —— 只在 loadStats 里当判据用（见那里的注释）：
   /api/stats 的 mark_map 是**一次扫描的快照**，统计又是 2s 轮询来的 ⇒ 晚到的旧快照整表覆盖
   就会把刚写入的标记抹掉、角标随之被 syncAllCardMarks() 摘掉（用户看到「标记完角标闪一下
   不见了」）。
   ⚠ 判据**不能只看墙钟宽限期**：一次 loadStats 的返回时刻由网络与主线程决定，定时器被节流时
   可以晚到远超宽限期（2026-09-17 batch1 §② 间歇红就是这么暴露的）⇒ 宽限期一过期，晚到的旧
   快照照样把标记抹掉。改为「**确认收敛**」：快照里出现本地写入的那个值才交还服务端，没出现
   就继续以本地为准（覆盖时长由「服务端何时读到」决定，不由墙钟决定）。
   MARK_TTL_MS 只是兜底：另一端改了同一张时，最终仍会生效。 */
let MARK_LOCAL = new Map();
const MARK_TTL_MS = 15000;     // 兜底上限（远大于一次扫描+轮询的耗时，不是收敛判据）
let LIST_VIEW = false;         // 列表视图（由 #viewToggle 同步；卡片要额外渲染字段列）
/* ---------- 视图模式（V2.8.5 时间轴） ----------
   VIEW_MODE = "grid" | "list" | "time"；LIST_VIEW 仍是**派生值**（由 setupViewToggle 同步），
   这样其它 5 处读 LIST_VIEW 的地方一行都不用改。 */
let VIEW_MODE = "grid";
/* 时间轴状态：全部内存态（分组是纯前端算的，不需要后端配合） */
let TL_GRAIN = "day";          // 粒度：hour | day | week | month | batch
let TL_BATCH_GAP = 300;        // 批次聚类阈值（秒）：同批两次落盘间隔超过它就开新批
let TL_COLLAPSED = {};         // {组键:true} 整组折叠
let TL_SHOWN = {};             // {组键:true} 组内已点过「展开全部」
let TL_GROUPS = [];            // 最近一次渲染的组元数据（日期索引栏用）
const TL_CAP = 60;             // 单组默认最多渲染多少张（超出给「展开全部」）
/* 合成条目类型：只是导航/分隔用的占位行，不算「条目」、不参与大图翻页与批量选择 */
const TL_SYNTH = {dir:1, dirgroup:1, dirback:1, tlhead:1, tlmore:1};
/* ---------- 内容安全（V2.11.0）----------
   NSFW_MODE  : hide | blur | show —— 显示方式。为什么是三态不是开关，见 index.html 的注释。
   NSFW_INFO  : /api/nsfw/status 的快照（引擎可用性 / 档位 / 阈值 / 扫描进度 / 计数）。
                null 表示还没拉到 —— 没拉到之前一切按「原样显示」处理，绝不能反过来把图藏了。
   NSFW_REVEAL: 本次会话中被手动揭开的路径集合。**故意不持久化**：
                揭开的意图是「现在看一眼」，刷新就该重新盖上；
                「这张以后都别糊了」是另一件事 —— 那是人工纠正，由服务端持有
                （写进 nsfw_manual.json，重新检测也翻不回去）。 */
let NSFW_MODE = "blur";
let NSFW_INFO = null;
const NSFW_REVEAL = new Set();
const NSFW_MODES = ["hide", "blur", "show"];
try{ const _n = localStorage.getItem("mv.nsfw");
     if(NSFW_MODES.indexOf(_n) >= 0) NSFW_MODE = _n; }catch(_){}
/* 功能真的开着吗（装了推理库 + 有模型 + 没被关掉）。
   不可用一律 false，界面原样显示 —— 宁可「没保护」也不能「把不该藏的藏了」。 */
const nsfwOn = () => !!(NSFW_INFO && NSFW_INFO.available && NSFW_INFO.enabled);

/* ---------- 三态判据（V2.12.6）----------
   用户实报：装了引擎但**从没去设置页跑过检测**时，工具条三档看着是好的、点了也有反应、
   还弹「已隐藏 NSFW 内容」，可一张图都没被过滤 —— 因为列表里每条还是 n="unknown"，
   而 isNsfw() 对 unknown 恒假。控件"报成功"了、实际什么都没干，比直接置灰更糟。

   病根是判据混了一件事：nsfwOn() 只问**引擎装没装好**，没问**跑没跑过**。
   引擎可用但 count 全是 0（一条都还没判定）＝ 还没跑过 → 必须单独立一态。

   ⚠️ 判据用「counts 三项之和 > 0」而**不是** `done > 0`：
      · total 是「收集阶段」的进度，收集中 done 会从 0 开始涨，但那时一张都还没判定完；
      · 扫描结束后 counts 才会被真正写满。取三项和已经够用，且顺带覆盖
        「扫描完了但库里 0 张图」这种边角（那时确实没什么可过滤的）。
   ⚠️ available=false 时 counts 恒空 → nsfwScanned() 必然是 false，
      所以 noengine 态天然落在 undetected 之外，两态不会打架。 */
const nsfwScanned = () => {
  const c = (NSFW_INFO && NSFW_INFO.counts) || {};
  return ((c.nsfw | 0) + (c.sfw | 0) + (c.unknown | 0)) > 0;
};
/* 引擎可用 **且** 跑过检测 —— 只有这个状态下的三档才是真能生效的 */
const nsfwReady = () => nsfwOn() && nsfwScanned();
/* 引擎根本没装（两态里的第一态）。和「装了但没跑」是两码事，文案/颜色/出路都不同。 */
const nsfwNoEngine = () => !nsfwOn();
/* 这张到底算不算 NSFW。两个来源，顺序不能反：
     1. ns >= 0（服务端给了分数）→ 现场按**当前阈值**重算。
        这样阈值滑块改完立刻生效，不必重拉列表、更不必重跑推理（分数只算一次）。
     2. ns < 0 → 以服务端给的 n 为准，前端不许自己算。
        这里头包括**人工纠正**的情况 —— 用户明确标过的判定，绝不能被阈值算回去；
        也包括还没检测、检测失败、文件被改过（这些都是 n="unknown"）。 */
const nsfwLabelOf = it => {
  if(!it) return "unknown";
  const thr = (NSFW_INFO && NSFW_INFO.threshold != null) ? NSFW_INFO.threshold : 0.60;
  if(typeof it.ns === "number" && it.ns >= 0) return it.ns >= thr ? "nsfw" : "sfw";
  return it.n || "unknown";
};
const isNsfw = it => nsfwOn() && nsfwLabelOf(it) === "nsfw";
/* 隐藏模式下要从列表里剔除 */
const nsfwShouldHide = it => isNsfw(it) && NSFW_MODE === "hide";
/* 模糊模式下要盖遮罩（已手动揭开的除外） */
const nsfwShouldVeil = it => isNsfw(it) && NSFW_MODE === "blur" && !NSFW_REVEAL.has(it.path);

let metaLoading = false;       // 元数据批量请求进行中，避免列表视图重复发起
let metaTok = 0;               // 元数据加载序号：过期回调不再重算（切目录/切视图后会作废）
let metaErrAt = 0;             // 上次加载失败的时间戳，失败后 10s 内不重试，避免错误时反复打请求
let statsTimer = null, statsTok = 0, statsPolls = 0;
let statsBuildAt = 0;      // §6.1：索引进度轮询的**墙钟**起点（替代按调用次数算预算）
/* 展示方式：false=分页（底栏页码 + 每页条数）/ true=无限瀑布流（滚到底自动续载）
   V2.7：默认改为瀑布流（历史记录里显式选过「分页」才回退分页模式） */
let FLOW = true, PAGE = 1, PER = 20, FILT_SIG = null;
/* ---- 审计修复 ε-3b 新增的跨文件状态 ----------------------------------
   FMT_COUNTS  左栏「格式」计数，**当前结果集**口径（filters.applyFilter 里现算，
               统计点选在 FMTS 过滤**之前** ⇒「数字 = 勾选该格式后能看到的张数」。
               原口径取自 S.stats.formats，那是**根目录全树**的全库数字，
               在子目录里会出现「PNG 3000」而实际 0 张（勾了必然筛空）。
   TL_ROWIDX   时间轴：组 gi → 该组头在**全量行序列**里的下标。
               分页模式下点日期索引时用它算出目标页码，避免「为了跳组把展示
               方式偷偷换成无限瀑布流」。 */
let FMT_COUNTS = {};
let TL_ROWIDX = {};
try{ if(localStorage.getItem("mv.flow")==="0") FLOW=false; }catch(_){}
try{ const _p=parseInt(localStorage.getItem("mv.per")||"20",10);
     if([20,40,60,100].includes(_p)) PER=_p; }catch(_){}

