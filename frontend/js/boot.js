/* ComfyMediaViewer · js/boot.js（2026-09-09 从 index.html 拆出，加载顺序与原文一致） */
"use strict";
/* 10. 启动 */
refreshTagStats();
loadStats();                   // 左栏「库」数量（全部/图片/视频/工作流）——异步，不阻塞首屏
/* 内容安全状态：异步拉一次即可。拿到之前 NSFW_INFO 是 null，界面按「原样显示」处理 ——
   顺序很关键：不能反过来先假设「要保护」再把图藏了，那样一旦接口失败
   用户看到的就是一片空白。拉失败也只记 log，不影响其它任何功能。 */
try{ if(typeof nsfwLoadStatus==="function") nsfwLoadStatus(true); }catch(e){}
$("#tagManage").onclick=openTagManager;
/* 标签匹配方式切换（AND=同时满足 / OR=任一满足），立即按新语义重算当前筛选 */
(function(){
  const b=$("#tagMode"); if(!b) return;
  const sync=()=>{
    b.textContent = TAG_MODE==="or" ? "OR" : TAG_MODE==="not" ? "NOT" : "AND";
    b.classList.toggle("on", TAG_MODE!=="and");
    b.dataset.tip = TAG_MODE==="and" ? "多标签：同时满足（点一下切到 OR）"
            : TAG_MODE==="or"  ? "多标签：任一满足（点一下切到 NOT = 都不含）"
            : "多标签：都不包含 / 排除（点一下回到 AND）";
  };
  sync();
  b.onclick=()=>{
    TAG_MODE = TAG_MODE==="and" ? "or" : TAG_MODE==="or" ? "not" : "and";
    try{ localStorage.setItem("mv.tagmode", TAG_MODE); }catch(_){}
    sync();
    toast(TAG_MODE==="and" ? "标签筛选：同时满足（AND）"
        : TAG_MODE==="or"  ? "标签筛选：任一满足（OR）"
        : "标签筛选：都不包含（NOT）——选中的标签会被排除");
    if(TAG_FILTER.length || TAG_EXCLUDE.length) setTagFilter([...TAG_FILTER]);
  };
})();
$("#bTag").onclick=openBatchTag;
load("");
/* 换根迁移报告：自动迁移在后台跑（stats 轮询触发），启动几秒后大概率已完成。
   只在启动时和切根后各查一次 —— /api/migrate_report 取走即清，这里只是补上
   「迁移完成时用户可能不在场」的提示时机。 */
setTimeout(()=>{ try{ mvMigrateReport(); }catch(e){} }, 4000);
