#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ComfyUI 媒体浏览器 V2.14.0
========================
本地媒体浏览器：扫描目录、网格缩略图、图片/视频预览、读取 ComfyUI PNG 元数据、
一键回传工作流到 ComfyUI (127.0.0.1:8188)。

用法:
    python server.py                          # 自动探测默认目录
    python server.py --root "D:\\我的图片"     # 指定根目录
    python server.py --port 8765 --no-browser # 固定端口、不开浏览器

零第三方依赖；若环境里有 Pillow 则自动生成磁盘级缓存的缩略图。
仅监听 127.0.0.1，文件访问被限制在当前根目录内。
环境变量：COMFYUI_URL  = ComfyUI 地址（默认 http://127.0.0.1:8188）；
          COMFYUI_ROOT = ComfyUI 安装目录（用于定位 UI 工作流模板）。
"""

import argparse
import concurrent.futures
import ctypes
import email.utils
import hashlib
import io
import json
import mimetypes
import os
import re
import secrets
import shutil
import stat as stat_mod
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# The offline worker must branch before optional engines, directory selection,
# hotfix probes and startup writes; the old application may still be running.
if "--classic-data-worker" in sys.argv:
    worker_parser=argparse.ArgumentParser(description="Classic data offline task")
    for name in ('classic-data-worker','selector','current','job-id','parent-pid','parent-birth'):
        worker_parser.add_argument('--'+name,required=True)
    worker_args=worker_parser.parse_args()
    from backend.storage.classic_data import run_worker
    from backend.migration.relocation_job import _wait_for_exit
    try:
        birth=worker_args.parent_birth
        if birth.isdecimal(): birth=int(birth)
        _wait_for_exit(int(worker_args.parent_pid),birth,3600)
        result=run_worker(worker_args.selector,worker_args.current,
                          worker_args.classic_data_worker,worker_args.job_id)
        print(json.dumps(result,ensure_ascii=False))
    except Exception as error:
        print(str(error),file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(0 if result.get('state')=='completed' else 2)

if "--preparation-job-worker" in sys.argv:
    preparation_parser = argparse.ArgumentParser(description="Offline database preparation job")
    preparation_parser.add_argument("--preparation-job-worker", required=True)
    preparation_args = preparation_parser.parse_args()
    from backend.migration.preparation_job import run_preparation_job
    try:
        preparation_result = run_preparation_job(preparation_args.preparation_job_worker)
    except (OSError, ValueError, RuntimeError) as preparation_error:
        print(str(preparation_error), file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(preparation_result, ensure_ascii=False))
    raise SystemExit(0 if preparation_result.get("state") == "completed" else 2)

if "--migration-worker" in sys.argv:
    from backend.migration.worker import main as migration_worker_main
    raise SystemExit(migration_worker_main(sys.argv[1:]))

if "--recovery-worker" in sys.argv:
    from backend.migration.recovery_worker import main as recovery_worker_main
    raise SystemExit(recovery_worker_main(sys.argv[1:]))

if "--recovery-job-worker" in sys.argv:
    recovery_parser = argparse.ArgumentParser(description="Offline complete recovery job")
    recovery_parser.add_argument("--recovery-job-worker", required=True)
    recovery_args = recovery_parser.parse_args()
    from backend.migration.recovery_job import run_recovery_job
    try:
        recovery_result = run_recovery_job(recovery_args.recovery_job_worker)
    except (OSError, ValueError, RuntimeError) as recovery_error:
        print(str(recovery_error), file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(recovery_result, ensure_ascii=False))
    raise SystemExit(0 if recovery_result.get("state") == "completed" else 2)

if "--recovery-job-create" in sys.argv:
    recovery_create_parser = argparse.ArgumentParser(description="Persist an offline recovery task")
    recovery_create_parser.add_argument("--recovery-job-create", action="store_true")
    recovery_create_parser.add_argument("--selector", required=True)
    recovery_create_parser.add_argument("--action", required=True, choices=['export','restore'])
    recovery_create_parser.add_argument("--destination", required=True)
    recovery_create_parser.add_argument("--source")
    recovery_create_parser.add_argument("--bundle")
    recovery_create_args = recovery_create_parser.parse_args()
    from pathlib import Path as RecoveryPath
    from backend.migration.recovery_job import create_recovery_job, _selected
    try:
        source = recovery_create_args.source or str(_selected(RecoveryPath(recovery_create_args.selector)))
        job_path = create_recovery_job(source,recovery_create_args.selector,recovery_create_args.action,
                                      recovery_create_args.destination,bundle=recovery_create_args.bundle)
    except (OSError,ValueError,RuntimeError) as recovery_create_error:
        print(json.dumps({'error':str(recovery_create_error),'selectorChanged':False},ensure_ascii=False),file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps({'jobPath':str(job_path),'jobId':job_path.parent.name,'selectorChanged':False},ensure_ascii=False))
    raise SystemExit(0)

if "--preserved-selection-worker" in sys.argv:
    import sqlite3 as preserved_sqlite
    preserved_parser = argparse.ArgumentParser(description="Preserve current raw materials before selecting a verified restore")
    preserved_parser.add_argument("--preserved-selection-worker",action="store_true")
    for preserved_name in ("selector","bundle","target","evidence-directory"):
        preserved_parser.add_argument("--"+preserved_name,required=True)
    preserved_parser.add_argument("--acknowledge-raw-preservation",action="store_true",required=True)
    preserved_args=preserved_parser.parse_args()
    from backend.migration.preserved_selection import select_preserved_source_restore
    try:
        preserved_result=select_preserved_source_restore(preserved_args.selector,preserved_args.bundle,
            preserved_args.target,preserved_args.evidence_directory)
    except (OSError,ValueError,RuntimeError,preserved_sqlite.Error) as preserved_error:
        print(json.dumps({'error':str(preserved_error),'requiresStateInspection':True},ensure_ascii=False),file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(preserved_result,ensure_ascii=False))
    raise SystemExit(0)

if "--disaster-selection-worker" in sys.argv:
    disaster_parser = argparse.ArgumentParser(description="Select verified restore of a missing source")
    disaster_parser.add_argument("--disaster-selection-worker",action="store_true")
    for disaster_name in ("selector","bundle","target","evidence-directory"):
        disaster_parser.add_argument("--"+disaster_name,required=True)
    disaster_args=disaster_parser.parse_args()
    from backend.migration.disaster_selection import select_missing_source_restore
    try:
        disaster_result=select_missing_source_restore(disaster_args.selector,disaster_args.bundle,
            disaster_args.target,disaster_args.evidence_directory)
    except (OSError,ValueError,RuntimeError) as disaster_error:
        print(json.dumps({'error':str(disaster_error),'requiresStateInspection':True},ensure_ascii=False),file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(disaster_result,ensure_ascii=False))
    raise SystemExit(0)

if "--recovery-select-worker" in sys.argv:
    selection_parser = argparse.ArgumentParser(description="Offline verified restored-data selection")
    selection_parser.add_argument("--recovery-select-worker", action="store_true")
    selection_parser.add_argument("--selector", required=True)
    selection_parser.add_argument("--job-id", required=True)
    selection_parser.add_argument("--backup-directory", required=True)
    selection_args = selection_parser.parse_args()
    from backend.migration.recovery_selection import select_restored_data
    try:
        selection_result = select_restored_data(selection_args.selector,selection_args.job_id,
                                                 selection_args.backup_directory)
    except (OSError,ValueError,RuntimeError) as selection_error:
        print(json.dumps({'error':str(selection_error),'requiresStateInspection':True},ensure_ascii=False),file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(selection_result,ensure_ascii=False))
    raise SystemExit(0)

if "--recovery-selection-job-worker" in sys.argv:
    selection_job_parser = argparse.ArgumentParser(description="Offline restored-data selection job")
    selection_job_parser.add_argument("--recovery-selection-job-worker", required=True)
    selection_job_args = selection_job_parser.parse_args()
    from backend.migration.recovery_selection_job import run_selection_job
    try:
        selection_job_result = run_selection_job(selection_job_args.recovery_selection_job_worker)
    except (OSError,ValueError,RuntimeError) as selection_job_error:
        print(json.dumps({'error':str(selection_job_error),'requiresStateInspection':True},ensure_ascii=False),file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(selection_job_result,ensure_ascii=False))
    raise SystemExit(0 if selection_job_result.get('state')=='completed' else 2)

if "--installation-worker" in sys.argv:
    from backend.migration.installation_flow import main as installation_worker_main
    raise SystemExit(installation_worker_main(sys.argv[1:]))

if "--installation-job-worker" in sys.argv:
    installation_parser = argparse.ArgumentParser(description="Offline database installation job")
    installation_parser.add_argument("--installation-job-worker", required=True)
    installation_args = installation_parser.parse_args()
    from backend.migration.installation_job import run_installation_job
    try:
        installation_result = run_installation_job(installation_args.installation_job_worker)
    except (OSError, ValueError, RuntimeError) as installation_error:
        print(str(installation_error), file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(installation_result, ensure_ascii=False))
    raise SystemExit(0 if installation_result.get("state") == "completed" else 2)

if "--relocation-worker" in sys.argv:
    worker_parser = argparse.ArgumentParser(description="Offline data relocation worker")
    worker_parser.add_argument("--relocation-worker", required=True)
    worker_args = worker_parser.parse_args()
    from backend.migration.relocation_job import run_relocation_job
    try:
        worker_result = run_relocation_job(worker_args.relocation_worker)
    except (OSError, ValueError, RuntimeError) as worker_error:
        print(str(worker_error), file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(worker_result, ensure_ascii=False))
    raise SystemExit(0)

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# NSFW 检测引擎（V2.11.0，可选依赖）。这里**故意不套 try/except** ——
# onnxruntime / numpy / Pillow 缺哪个都由 nsfw.py 自己吞在里面，它永远导得进来；
# 真正的可用性看 nsfw.available()，缺库时只在界面上提示，绝不 500、不影响既有功能。
# 打包成 exe 时 nsfw 必须进 hiddenimports（见 ComfyMediaViewer.spec）。
import nsfw as NSFW_ENGINE

# 热更新核心（V2.13.0）。打包时必须进 hiddenimports（见 ComfyMediaViewer.spec），
# 否则 exe 里没有这个模块，更新功能会在运行时才炸。
import updater as UPDATER

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".m4v", ".mkv", ".avi"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS          # /api/file 只允许内联渲染这些类型
PLAYABLE_VIDEO = {".mp4", ".webm", ".m4v", ".mov"}
CHUNK = 1 << 16

APP_TITLE = "ComfyUI Media Viewer"
APP_VERSION = "3.0.0"           # 版本号唯一来源（/api/info 与 Server 头都取它）

COMFY_BASE = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188").rstrip("/")
# ---------------------------------------------------------------- 数据目录（F28 便携模式）
# 设置 / 收藏 / 标签 / 索引 / 缩略图缓存 / 回收站全在这里。默认在用户目录
# （%LOCALAPPDATA%\ComfyMediaViewer），便携模式改成程序目录下的 cmv-data/。
#
# 为什么用「exe 旁一个标记文件」而不是把选择写进 settings.json：后者自己就住在数据目录里，
# 等于「要先知道数据目录才能读到数据目录」，成了死循环。标记文件放在程序旁还有实际好处——
# 整个文件夹拷到 U 盘，便携状态跟着一起走（换机不用重新设一次）。
PORTABLE_DATA_NAME = "cmv-data"

# 程序目录覆盖（优先级见 _exe_dir）。三档来源：
#   1. `CMV_EXE_DIR` 环境变量 —— 验证脚本 / 自定义启动器用，最高优先级，随时能压过下面两个
#   2. --app-dir 命令行 —— **桌面壳专用**。桌面壳把后端 exe 释放到 %LOCALAPPDATA% 下去跑，
#      于是 sys.executable 指的是那个释放目录，不是用户放程序的地方；壳必须把自己 exe 的真实
#      位置从这儿告诉后端，否则便携目录会落在 C 盘（用户看到的就是「便携了个寂寞」）。
#   3. 都没有就按常规算（源码判断 __file__、打包判断 sys.executable）
_APP_DIR_OVERRIDE = None


def _preparse_app_dir(argv=None) -> str:
    """先于 argparse 把 --app-dir 抠出来。

    _exe_dir() 不只被 main() 用 —— 模块级就有 PORTABLE_FLAG / DATA_DIR 等一批常量要算，
    它们比 main() 跑得早。所以这个开关得在导入期就能生效，不能等 argparse。
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    for i, a in enumerate(argv):
        if a == "--app-dir" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--app-dir="):
            return a.split("=", 1)[1]
    return ""


def _exe_dir() -> Path:
    """程序所在目录：打包后 = exe 同目录；源码直启 = server.py 同目录。

    可被 _APP_DIR_OVERRIDE / CMV_EXE_DIR 覆盖 —— 便携模式的标记与数据目录都挂在这上面，
    验证脚本必须能把它指到临时目录，否则一次测试就会在工程里留下 portable.flag（下次真启动
    会被切到便携模式，看着像数据丢了）。桌面壳也靠它把「程序真正在哪」传进来。
    """
    env = os.environ.get("CMV_EXE_DIR")
    if env:
        return Path(env).resolve()
    if _APP_DIR_OVERRIDE:
        return Path(_APP_DIR_OVERRIDE).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


_APP_DIR_OVERRIDE = _preparse_app_dir()


def _user_data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ComfyMediaViewer"


PORTABLE_FLAG = _exe_dir() / "portable.flag"


def _portable_flag_dir() -> Path:
    """读 portable.flag 里的 {"dir": ...} 覆盖，返回绝对落点；无有效覆盖返回 None。

    ε-13（§6.6）：提取成独立函数，`_resolve_data_dir()` 与 `datadir_for()` 共用 ——
    此前只有前者读标记 JSON、后者写死「程序目录/ComfyMediaViewer_Data」，
    口径分叉的后果：切到便携模式（自定义 dir）后，`/api/sysinfo` 报的 targets、
    前端弹窗显示的目标路径全是错的（显示默认落点，实际数据去了自定义目录），
    用户再点一次「切回去」时算出的 dst 也不是真正生效的那份。
    """
    try:
        cfg = json.loads(PORTABLE_FLAG.read_text("utf-8") or "{}")
        if isinstance(cfg, dict):
            raw = str(cfg.get("dir") or "").strip() or None
            if raw:
                target = Path(raw)
                if not target.is_absolute():
                    target = _exe_dir() / target
                return target
    except (OSError, ValueError):
        pass                      # 标记存在但内容不是 JSON：仍按便携处理，落点取默认
    return None


def _resolve_data_dir():
    """返回 (数据目录, 模式)。标记文件里可以写 {"dir": "..."} 覆盖落点（相对路径按程序目录算）。"""
    if PORTABLE_FLAG.is_file():
        target = _portable_flag_dir() or (_exe_dir() / PORTABLE_DATA_NAME)
        return target, "portable"
    return _user_data_dir(), "user"


MIGRATION_PROTECT = "--migration-protect" in sys.argv
_FRESH_DATA_DIR = None
DATA_DIR, DATA_MODE = _resolve_data_dir()      # 进程内唯一真相；改它必须重启
# An explicit saved selection is existing user data, not a first-run directory.
# Fail before probes/logging/guards can recreate a missing selected source.
if PORTABLE_FLAG.is_file() and _portable_flag_dir() is not None and not DATA_DIR.is_dir():
    raise RuntimeError("已选择的数据目录不存在或不可访问，停止启动并保留原选择；请检查磁盘或使用独立恢复入口：" + str(DATA_DIR))
_FRESH_DATA_DIR = not DATA_DIR.exists() or (DATA_DIR.is_dir() and not any(DATA_DIR.iterdir()))
from backend.storage.runtime_state import check_relocation_startup
check_relocation_startup(DATA_DIR)  # Before hotfix probes, logs or any data-directory writes.


# ============================================================
# CMV3-LOCAL-FIX(C3) — 测试期自动迁移开关（**仅供 tests/ 门禁用，产品默认关闭**）
#
# 背景（取证见 doc/v3门禁语义冲突台账.md · C3）：
#   3.0.0 后端是「SQLite 优先、legacy 兜底」双模，但 **legacy 模式下不产生媒体身份**
#   （server.py 里 `identities = (_SQL_CATALOG.observe_many(...) if _SQL_CATALOG is not None
#     else [{} for _ in pending_stats])`，而 `_SQL_CATALOG` 只在 mode != 'legacy-json' 时赋值）。
#   后果：/api/list 的 items[].mediaId / revisionId 恒为 None ⇒ 上游前端 CMV3.target()
#   的硬校验（backend-compat.js:83 要求 mediaId && expectedVersion）抛错 ⇒
#   /api/meta、/api/fav、/api/tags、/api/mark、/api/delete 等**全部写操作入口被拦死**。
#   实测对账（同一份数据、同一前端，只切模式）：
#       legacy : mediaId=null · target 抛错 · 大图 #meta .k = 0 · S.curMeta = null
#       sqlite : mediaId=8 8 0daf31-… · target ok · 大图 #meta .k = 4 · S.curMeta 完整
#   而 tests/ 下 102 门**全部跑在 legacy 模式**（0 门做迁移）⇒ 25~33 门涉身份门禁
#   结构性不可能通过。门禁要能在 3.0.0 下工作，就必须跑在 sqlite 模式上。
#
# 为什么不改 100+ 个门禁的 spawn 点：
#   门禁设 LOCALAPPDATA 的写法有十几种（user_d / USER_D / SB / APP_DATA / ISO_DIR / …），
#   逐个改写既易漏又易错。而所有门禁都经 `dev/_regress_guard.run_gate()` 统一 spawn
#   （Popen 不传 env ⇒ 继承驱动进程环境），所以在**驱动侧设一个环境变量**即可全覆盖。
#
# 开关语义（**最小、可审计**）：
#   CMV_AUTO_MIGRATE=1 且当前数据目录是 legacy ⇒ 启动时自动跑官方两段 CLI 迁移，
#   成功后才继续启动；失败**直接抛错退出**（绝不静默降级成 legacy —— 那会让门禁
#   拿到假数据跑出假结论，正是本项目最忌讳的「假绿/假红」）。
#   roots 取自：settings.json 的 roots ∪ 启动参数 --root（门禁的素材目录）。
#
# 产品行为零变化：不设该变量时本段完全不执行。
# ============================================================
_AUTO_MIGRATE = os.environ.get("CMV_AUTO_MIGRATE", "").strip() in ("1", "true", "yes")


def _auto_migrate_if_needed(data_dir, extra_roots=()):
    """测试期自动迁移（仅在 CMV_AUTO_MIGRATE=1 时调用）。返回 True=已迁移。"""
    import json as _json
    from backend.storage.runtime_state import load_state as _load_state

    if _load_state(data_dir)["mode"] != "legacy-json":
        return False                      # 已是 sqlite：什么都不做

    data_dir = Path(data_dir)
    settings_path = data_dir / "settings.json"
    settings = {}
    if settings_path.is_file():
        try:
            settings = _json.loads(settings_path.read_text("utf-8")) or {}
        except (OSError, ValueError):
            settings = {}
    if not isinstance(settings, dict):
        settings = {}

    roots = [r for r in (settings.get("roots") or []) if isinstance(r, str) and r.strip()]
    for r in extra_roots:
        if r and str(r).strip():
            roots.append(str(Path(r).absolute()))
    roots = list(dict.fromkeys(roots))

    # roots 的三级兜底 —— 目标是**只要这门真的需要媒体身份，就一定能迁成**：
    #   ① settings.json 的 roots（正常途径）
    #   ② 启动参数 --root（已在 extra_roots 里）
    #   ③ **从 legacy 数据里反推**：favorites/tags/recent/settings 里出现过的文件路径
    #      （取共同父目录到一个「像根」的位置）。门禁常只喂素材目录、不写 settings。
    if not roots:
        inferred = _infer_legacy_roots(data_dir)
        roots = inferred
        if inferred:
            print("[CMV_AUTO_MIGRATE] settings 无 roots，已从 legacy 数据反推：%s" % inferred,
                  flush=True)

    if not roots:
        # 实在推不出来 ⇒ **跳过迁移**而不是抛错。
        # 判断依据：没有 root 就没有任何媒体，/api/list 返回空 ⇒ 该门禁也用不到
        # 媒体身份（mediaId）。此时保持 legacy 与迁移后等价，强行迁移只会炸掉
        # 一个本来与身份无关的门禁（如 delmode_check：只测 /api/prefs 的
        # delete_mode 持久化，全程不需要媒体）。
        print("[CMV_AUTO_MIGRATE] 数据目录无任何 root 线索 ⇒ 跳过迁移，保持 legacy"
              "（本门禁不涉及媒体身份，legacy 与 sqlite 等价）", flush=True)
        return False

    settings["roots"] = roots
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(_json.dumps(settings, ensure_ascii=False), "utf-8")

    from backend.migration.prepare import prepare_migration
    from backend.migration.installation_flow import complete_installation

    prep = Path(str(data_dir) + "-v3-automigrate")
    if prep.exists():
        import shutil as _shutil
        _shutil.rmtree(prep, ignore_errors=True)
    prepared = prepare_migration(str(data_dir), str(prep))
    if not prepared.get("readyForActivation"):
        # 迁移前置不通过 ⇒ **优雅跳过**（保持 legacy），不抛错。
        # 判据：preflight 拒绝的都是**非标准数据目录**——门禁临时造的空目录、带备份
        # 残留的半迁移目录、只有若干 JSON 没有真实媒体的目录。这类目录里本来就
        # 没有可迁移的媒体，迁移成功与否对门禁断言无影响；而一旦抛错，整门 rc=1，
        # 就成了「我的注入把一门与身份无关的门禁搞红了」的假红（实测
        # p0fix_20261009_check / pAfix_e5_check 两门）。
        # ⚠️ 与「roots 推不出 ⇒ 跳过」同一处置原则：**只在会假红时跳过，永远不假装成功**。
        print("[CMV_AUTO_MIGRATE] 迁移前置不通过 ⇒ 跳过迁移，保持 legacy（%s）"
              "（该门禁不涉及媒体身份，legacy 与 sqlite 等价）" % prepared.get("error"),
              flush=True)
        return False
    activated = complete_installation(str(prep), str(prep / "bundle"))
    if not activated.get("authorityPublished"):
        # 走到了这一步说明前置是过的，失败属真异常 ⇒ 必须暴露，不能吞。
        raise RuntimeError("CMV_AUTO_MIGRATE 迁移未发布权威指针")
    if _load_state(data_dir)["mode"] != "sqlite":
        raise RuntimeError("CMV_AUTO_MIGRATE 迁移后 state.mode 仍非 sqlite")
    return True


def _infer_legacy_roots(data_dir):
    """从 legacy JSON 数据里反推「根目录」候选（CMV_AUTO_MIGRATE 兜底用）。

    依据：legacy 的 favorites.json / tags.json / settings.json / recent.json
    的键（或值）里会出现真实文件绝对路径。取这些路径的**共同父目录**，
    并用「目录存在 + 是目录」做校验。推不出返回 []。
    """
    import json as _json
    from pathlib import Path as _Path

    data_dir = _Path(data_dir)
    paths = []

    def _harvest(obj):
        if isinstance(obj, str):
            if (len(obj) > 3 and (":" in obj[:3] or obj.startswith("/"))
                    and "." in _Path(obj).name):
                paths.append(obj)
        elif isinstance(obj, dict):
            for k, v in obj.items():
                _harvest(k)
                _harvest(v)
        elif isinstance(obj, list):
            for v in obj:
                _harvest(v)

    for name in ("favorites.json", "tags.json", "recent.json", "marks.json"):
        f = data_dir / name
        if not f.is_file():
            continue
        try:
            _harvest(_json.loads(f.read_text("utf-8")))
        except (OSError, ValueError):
            continue

    if not paths:
        return []

    # 取共同父目录链：逐个文件向上收敛，直到只剩一个
    try:
        commons = [_Path(p).parent for p in paths]
        while len(set(str(c) for c in commons)) > 1:
            parents = []
            for c in commons:
                parents.append(c if c == c.parent else c.parent)
            if set(str(p) for p in parents) == set(str(c) for c in commons):
                break
            commons = parents
        root = commons[0]
        if root.is_dir():
            return [str(root.resolve())]
        # 共同父目录不成立（跨盘等）⇒ 退回「各文件的最上级存在目录」
        cands = set()
        for c in commons:
            cands.add(str(c.resolve()))
        return sorted(cand for cand in cands if _Path(cand).is_dir())
    except (OSError, ValueError):
        return []


if _AUTO_MIGRATE and not MIGRATION_PROTECT and __name__ == "__main__":
    # ⚠️ 这里必须带 `__name__ == "__main__"` 判据（2026-10-10 补）。
    #   理由：本段是**模块级**代码，`import server` 也会执行一遍。而 in-process
    #   直调的门禁（pAfix_e5_check / p0fix_20261009_check 等）正是 `import server`，
    #   且它们的 `os.environ["LOCALAPPDATA"]` **是在 import 之后**才设的 ⇒ 模块级
    #   触发点读到的是**真实用户数据目录**（%LOCALAPPDATA%\ComfyMediaViewer），
    #   在那里跑迁移会：① 撞上别的进程持有的目录锁（Another cooperating process
    #   owns this data directory）；② 对已是 sqlite、带备份残留的真实目录反复
    #   preflight（Preflight issues block backup）—— 实测两门都因此 1~6 秒即炸。
    #   而这两门根本不需要媒体身份（一门测 _meta_fail_*、一门纯函数直调）。
    #   ⇒ 模块级只服务「真·CLI 启动」（`python server.py` == 门禁 spawn 的那条路，
    #     `__name__` 正是 "__main__"），import 路径一律不在这里迁移。
    #     门禁 spawn 的 156 处 server.py 启动点因此行为完全不变。
    # 启动参数里的 --root（若有）也要登记，否则门禁素材目录不在 roots 里。
    _argv_root = ""
    for _i, _a in enumerate(sys.argv):
        if _a == "--root" and _i + 1 < len(sys.argv):
            _argv_root = sys.argv[_i + 1]
        elif _a.startswith("--root="):
            _argv_root = _a.split("=", 1)[1]
    if _auto_migrate_if_needed(DATA_DIR, [_argv_root]):
        print("[CMV_AUTO_MIGRATE] 已将数据目录迁移至 3.0.0 SQLite：%s" % DATA_DIR, flush=True)


THUMB_DIR = DATA_DIR / "thumbs"
FAV_FILE = DATA_DIR / "favorites.json"
FAV_SEQ_FILE = DATA_DIR / "favorites.seq.json"   # FAV-01：收藏操作的单调序号（含未收藏项）
TAGS_FILE = DATA_DIR / "tags.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
TRASH_DIR = DATA_DIR / "trash"
TRASH_KEEP_DAYS = 30          # 回收站条目保留天数，超龄启动时自动清理

# ---- 热更新（V2.13.0）可覆盖目录 ------------------------------------------
# 为什么必须存在：打包成单文件 exe 后 frontend/ 被塞进 _MEIPASS —— 那是**只读的临时
# 解包目录，每次启动路径都不一样**。热更新下来的前端根本没地方落，覆盖更无从谈起。
# 所以这里开一个「程序目录/hotfix」，让它拥有比内置资源更高的优先级：
#     hotfix/frontend 整棵子树 → 覆盖 exe 里的前端（免重启，刷新页面即生效）
#     hotfix/applied.json  → 记录已应用的版本，供 /api/info 与更新检查比对
# 回滚方式刻意做得极简：**删掉 hotfix 目录即可**，不需要任何工具或参数。
HOTFIX_DIR_NAME = "hotfix"


def _resolve_hotfix_dir() -> Path:
    """挑一个**可写**的覆盖目录：优先程序目录（便携、用户看得见、跟着 exe 一起搬），
    写不进去（比如装在 Program Files）才退回数据目录。

    必须真的写一次探针 —— 只看目录是否存在会误判：Program Files 下的目录能列出来，
    但创建文件会被拒绝，等到更新时才失败就晚了。
    """
    if MIGRATION_PROTECT:
        return DATA_DIR / HOTFIX_DIR_NAME
    for cand in (_exe_dir() / HOTFIX_DIR_NAME, DATA_DIR / HOTFIX_DIR_NAME):
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / ".probe"
            probe.write_text("1", encoding="utf-8")
            probe.unlink()
            return cand
        except OSError:
            continue
    return DATA_DIR / HOTFIX_DIR_NAME      # 都不可写：仍返回数据目录，写入时如实报错


HOTFIX_DIR = _resolve_hotfix_dir()
FRONTEND_HOTFIX_DIR = HOTFIX_DIR / "frontend"
APPLIED_FILE = HOTFIX_DIR / "applied.json"

# --no-hotfix：强制忽略热更覆盖，只用内置/源码里的前端。
# 为什么需要它：hotfix 的优先级**高于源码目录**，所以在源码模式下改 frontend/ 时，
# 一旦目录里残留着热更下来的旧文件，改动会被静默盖住 —— 用户看到的现象是
# 「我明明改了，刷新还是老的」，而且完全看不出原因（hotfix 目录他多半不记得）。
IGNORE_HOTFIX = False
USE_NEXT_FRONTEND = False


def _hotfix_active() -> bool:
    """热更覆盖当前是否生效。判据用 index.html 是否存在（而非目录是否存在）——
    解压到一半的目录会把页面搞成白屏，宁可回落到内置版本。"""
    return (not IGNORE_HOTFIX) and (FRONTEND_HOTFIX_DIR / "index.html").is_file()


def load_applied() -> dict:
    """读已应用的更新记录；文件损坏/缺失一律当空（不因此拦住启动）。

    读写口径统一交给 updater 模块 —— 谁写谁读，免得两边实现漂移。
    """
    return UPDATER.load_applied(HOTFIX_DIR)


# ---- 更新源配置 ----------------------------------------------------------
# 更新源就是一个放着 latest.json + 更新包的静态目录。这里用发布者的 Gitee Release
# 固定标签 latest —— URL 永久不变，发新版只需覆盖同名附件。
#
# 写死一个默认源是**刻意的**：这程序是发给群友用的，让他们自己去填地址等于这功能对
# 大多数人不存在。用户可以随时在「设置 → 关于」里改写（存 settings.json），
# 环境变量 CMV_UPDATE_URL 优先级最高。
#
# ⚠️ 如果你 fork 了这个项目自己发版，**第一件事就是把下面这行改成你自己的仓库**，
#    否则你的用户会被指向发布者的更新源。
DEFAULT_UPDATE_URL = (os.environ.get("CMV_UPDATE_URL", "").strip()
                      or "https://gitee.com/MiKou2202/comfy-media-viewer"
                         "/releases/download/latest/latest.json")
UPDATE_CHECK_DELAY = 8.0        # 启动后延迟几秒再查：别和首屏、缩略图抢带宽
UPDATE_TIMEOUT = 15.0


def update_url() -> str:
    """当前生效的更新源。

    ⚠️ 三分支，不能简化成 `settings.get("update_url") or DEFAULT_UPDATE_URL`：
       · **键不存在**（从没设置过）→ 内置的发布者源，群友开箱即用
       · **显式设过**（哪怕值是空串）→ 以它为准；空串 = 用户主动关掉更新
    写成 `or DEFAULT` 的话，用户在设置页清空输入框后会**悄悄回落到内置源** ——
    等于「关不掉」，而且他完全看不出来（「我明明清空了啊」）。
    """
    s = load_settings()
    if "update_url" in s:
        return str(s.get("update_url") or "").strip()
    return DEFAULT_UPDATE_URL


def in_desktop_shell() -> bool:
    """是否跑在 Tauri 桌面壳里。

    壳把后端 exe **内嵌**进自己的安装包，每次启动都用内嵌版本覆盖释放目录
    （%LOCALAPPDATA%\\ComfyMediaViewer\\backend\\）。所以在壳里热更换掉的那份后端，
    下次启动会被原地抹掉 —— 用户白下 40 MB 且看不出任何变化。
    因此壳里只做前端热更（页面是后端现读现发的，照样能更），不碰程序本体。
    """
    return bool(_APP_DIR_OVERRIDE) or bool(os.environ.get("CMV_IN_SHELL"))


def update_auto_check() -> bool:
    v = load_settings().get("update_auto_check")
    return True if v is None else bool(v)


def update_work_dir() -> Path:
    return DATA_DIR / "update_cache"


# 一把**非阻塞**锁，保证「启动静默检查 / 用户手动检查 / 正在下载」三者互斥。
# 不加锁的话，静默检查与手动检查会并发跑，后一个把前一个写进 UPDATER.STATE 的结论
# 覆盖掉 —— 用户看到的就是「进度条突然跳回 0」「刚点检查就显示已是最新」这类怪象，
# 而且因为两个请求都「成功」了，日志里也看不出是谁覆盖了谁。
_UPDATE_LOCK = threading.Lock()


def update_check_sync(quiet: bool = False) -> dict:
    """同步检查一次（联网，可能几秒）。调用方自行决定放不放后台线程。

    quiet=True 用于启动时的静默检查：拿不到锁（说明有人正在更新）就安静退出，
    既不打扰用户，也不去覆盖别人正在写的状态。
    """
    url = update_url()
    if not url:
        return {"ok": False, "error": "尚未配置更新源，请到「设置 → 关于」填写",
                "configured": False, "quiet": quiet}
    if not _UPDATE_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "已有更新任务在进行中", "busy": True, "quiet": quiet}
    try:
        r = UPDATER.check(url, APP_VERSION, frontend_version(),
                          exe_path=(Path(sys.executable) if getattr(sys, "frozen", False) else None),
                          cache_file=HOTFIX_DIR / ".exe_hash.json",
                          allow_exe=not in_desktop_shell())
        r["configured"] = True
        return r
    finally:
        _UPDATE_LOCK.release()


def update_apply_async() -> dict:
    """触发更新：立刻回包，真正的下载/落盘在后台线程里跑，前端轮询 status。

    锁由**后台线程**持有到跑完 —— 于是「正在下载」期间用户再点检查会被明确拒掉，
    而不是并发跑出两份互相覆盖的状态。
    """
    url = update_url()
    if not url:
        return {"ok": False, "error": "尚未配置更新源"}
    if not _UPDATE_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "已有更新任务在进行中"}
    exe_path = Path(sys.executable) if getattr(sys, "frozen", False) else None

    def _run():
        try:
            r = UPDATER.apply(url, update_work_dir(), HOTFIX_DIR, APP_VERSION,
                              exe_path=exe_path, cache_file=HOTFIX_DIR / ".exe_hash.json",
                              allow_exe=not in_desktop_shell())
            if r.get("ok") and r.get("restart"):
                # exe 已下好：交给「新 exe 自己」完成替换（正在运行的 exe 改不了自己）。
                # 先把新 exe 拉起来当更新器，它会等本进程退出再动手。
                new_exe = r.get("exe_pkg")
                if new_exe:
                    me = str(Path(sys.executable))
                    try:
                        subprocess.Popen([str(new_exe), "--apply-update", str(os.getpid()), me],
                                         cwd=str(Path(me).parent), close_fds=True,
                                         creationflags=0x00000008,     # DETACHED_PROCESS
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
                        log("[update] 已拉起更新器，准备替换 exe 并重启")
                        threading.Timer(0.6, lambda: restart_self_after_update()).start()
                    except OSError as ex:
                        UPDATER.set_state(phase="error", error=f"启动更新器失败：{ex}")
                        log(f"[update] 启动更新器失败：{ex}")
        finally:
            _UPDATE_LOCK.release()      # 无论成败都要放锁，否则后续检查永远被拒

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "msg": "已开始更新"}


def restart_self_after_update():
    """更新收尾用的退出：不自己起新进程（新 exe 会起），把监听放掉后直接退。"""
    time.sleep(0.4)
    log("[update] 退出以让更新器完成替换")
    os._exit(0)


def update_check_bg():
    """启动后静默检查一次更新。

    **失败一律只写日志**：检查更新是锦上添花，网络不通、更新源挂了、公司代理拦了
    都不该在界面上冒错误、更不该影响正常使用。用户想主动看结果时走手动「检查更新」。
    """
    time.sleep(UPDATE_CHECK_DELAY)
    try:
        r = update_check_sync(quiet=True)
        if r.get("busy"):
            return          # 有人正在更新，静默检查让路，不覆盖别人写的状态
        if not r.get("ok"):
            log(f"[update] 启动检查未完成（已忽略）：{r.get('error')}")
        elif r.get("available"):
            log(f"[update] 发现新版本 {r['remote']['version']}"
                f"（前端={'要' if r.get('frontend') else '不用'}"
                f" / 程序={'要' if r.get('exe') else '不用'}）")
        else:
            log("[update] 已是最新版本")
    except Exception as ex:
        log(f"[update] 启动检查异常（已忽略）：{ex}")


# ---- 单图查看（V2.10.7）--------------------------------------------------
# 「打开一张图片」把外部图片（别人发来的原图）复制进这个目录，再走既有的
# /api/file、/api/thumb、/api/meta 端点。之所以落盘而非留在内存：预览/缩略图/
# 下载全部按「路径」取文件，纯内存态得另开一套并行数据通道；落盘后只要让
# inside_root() 放行该目录，整条链路零改动复用。
SOLO_DIR = DATA_DIR / "solo"
SOLO_MAX_BYTES = 64 << 20     # 单张上限（ComfyUI 4K 放大图常见 20~40MB）
SOLO_KEEP_MAX = 20            # 暂存上限张数；超出按 mtime 从旧删。同图同内容按
                              # 哈希复用同名文件，反复打开同一张不会累积。
MAX_BODY_BYTES = 64 << 20


REMEMBER_ROOTS = True         # 是否把根目录写进用户「常用目录」(settings.json)
_SQL_ORG = None               # Enabled only by a validated persistent authority pointer.
_SQL_BULK = None
_SQL_CATALOG = None
_SQL_SAFETY = None
_SQL_ARCHIVE = None
_SQL_GENERATION = None
_SQL_PROMPT_REFRESH = None
_SQL_QUERY = None
_SQL_SCAN_JOBS = None
_SQL_RECENT = None
_SQL_MODELS = None
_SQL_WORKFLOWS = None
_SQL_RELATIONS = None
_SQL_WORKFLOW_DECISIONS = None


def _initialize_catalog_storage():
    global _SQL_PROMPT_REFRESH
    global _SQL_ORG, _SQL_BULK, _SQL_CATALOG, _SQL_SAFETY, _SQL_ARCHIVE, _SQL_GENERATION, _SQL_QUERY, _SQL_SCAN_JOBS, _SQL_RECENT, _SQL_MODELS, _SQL_WORKFLOWS, _SQL_RELATIONS, _SQL_WORKFLOW_DECISIONS
    from backend.storage.runtime_state import load_state
    state = load_state(DATA_DIR)
    if state["mode"] == "legacy-json":
        return
    from backend.storage.legacy_adapter import LegacyOrganizationAdapter
    from backend.storage.legacy_bulk import LegacyBulkRepository
    from backend.storage.server_catalog import ServerCatalog
    from backend.storage.safety import SafetyRepository
    from backend.metadata.archive import MetadataArchive
    database = DATA_DIR / state["database"]
    from backend.storage.performance_schema import initialize as initialize_performance_schema
    initialize_performance_schema(database)
    from backend.storage.asset_lifecycle import initialize as initialize_asset_lifecycle
    initialize_asset_lifecycle(database, DATA_DIR / "trash-state.sqlite")
    _SQL_ORG = LegacyOrganizationAdapter(database, state["libraryUuid"], state["profileUuid"])
    _SQL_BULK = LegacyBulkRepository(database, state["libraryUuid"], state["profileUuid"])
    _SQL_CATALOG = ServerCatalog(database, state["libraryUuid"])
    from backend.storage.recent import RecentRepository
    _SQL_RECENT = RecentRepository(database)
    _SQL_RECENT.initialize()
    recent_source = DATA_DIR / "recent.json"
    if recent_source.is_file():
        recent_report = _SQL_RECENT.import_legacy(state["profileUuid"],
                                                  json.loads(recent_source.read_text("utf-8")))
        if recent_report["status"] != "already_imported" or not (DATA_DIR / "recent-import-report.json").is_file():
            if not _save_json(DATA_DIR / "recent-import-report.json", recent_report):
                raise RuntimeError("最近浏览导入报告无法保存")
    _SQL_SAFETY = SafetyRepository.initialize(database)
    from backend.query.safety_state import SafetyQueryState
    SafetyQueryState.initialize(database)
    _SQL_ARCHIVE = MetadataArchive(database)
    _SQL_ARCHIVE.initialize()
    from backend.metadata.workflow_projection_store import WorkflowProjectionStore
    WorkflowProjectionStore(database).initialize()
    from backend.metadata.generation_store import GenerationStore
    _SQL_GENERATION = GenerationStore(database)
    _SQL_GENERATION.initialize()
    from backend.query.prompt_fts import initialize as initialize_prompt_fts
    prompt_index = initialize_prompt_fts(database)
    if prompt_index['available']:
        from backend.query.prompt_index_refresh import PromptIndexRefresh
        _SQL_PROMPT_REFRESH = PromptIndexRefresh(database)
    from backend.metadata.model_service import ModelService
    _SQL_MODELS = ModelService(database, state["libraryUuid"], state["profileUuid"])
    _SQL_MODELS.initialize()
    from backend.metadata.workflow_service import WorkflowService
    _SQL_WORKFLOWS = WorkflowService(database, state["libraryUuid"])
    _SQL_WORKFLOWS.initialize()
    _SQL_WORKFLOWS.initialize_sources()
    _SQL_WORKFLOWS.initialize_source_bindings()
    from backend.metadata.workflow_decisions import WorkflowDecisions
    _SQL_WORKFLOW_DECISIONS = WorkflowDecisions(database)
    _SQL_WORKFLOW_DECISIONS.initialize()
    from backend.metadata.relations import CreationRelations
    _SQL_RELATIONS = CreationRelations(database, state['libraryUuid'])
    _SQL_RELATIONS.initialize()
    from backend.query.index_state import QueryIndexState
    QueryIndexState(database).initialize()
    from backend.query.key import load_or_create_key
    from backend.query.cursor import CursorCodec
    from backend.query.repository import QueryRepository
    _SQL_QUERY = QueryRepository(database, state["libraryUuid"], state["profileUuid"],
                                 CursorCodec(load_or_create_key(DATA_DIR / "query-cursor.key")))
    from backend.storage.scan_jobs import ScanJobs
    from backend.storage.directory_fingerprint import fingerprint as catalog_fingerprint
    _SQL_SCAN_JOBS = ScanJobs(database, state["libraryUuid"],fingerprint=catalog_fingerprint)
    if NSFW_MANUAL_FILE.is_file():
        _SQL_SAFETY.retain_legacy_evidence(state["libraryUuid"], str(NSFW_MANUAL_FILE),
                                          NSFW_MANUAL_FILE.read_bytes(),
                                          {"disposition": "unbound", "reason": "legacy_path_only",
                                           "originalFileRetained": True})


def _prepare_organization_target(path, request):
    if _SQL_CATALOG is None:
        return
    observed = _SQL_CATALOG.observe(STATE["root"], path, Path(path).stat())
    from backend.storage.legacy_adapter import IdentityConflictError
    if request.get("mediaId") and request["mediaId"] != observed["mediaId"]:
        raise IdentityConflictError("mediaId 与当前路径不一致")
    if request.get("expectedVersion") and request["expectedVersion"] != observed["contentVersion"]:
        raise IdentityConflictError("媒体版本已变化，请重新加载")


def _catalog_trash():
    from backend.operations.trash import CatalogTrash
    from backend.operations.journal import initialize_journal
    initialize_journal(DATA_DIR / "operation-journal.sqlite")
    service = CatalogTrash(_SQL_ORG.database, _SQL_ORG.library_uuid, STATE["root"], TRASH_DIR,
                           DATA_DIR / "operation-journal.sqlite", DATA_DIR / "operation-evidence")
    service.initialize()
    return service
                              # —— 自动化脚本 / 回归测试必须以 --no-remember 启动：
                              # 否则每次用临时目录起服务都会占一个常用目录名额（上限 8 个），
                              # 几次之后就把用户真实的 output 挤没了（V2.9.2 踩过）。
TRASH_LOCK = threading.Lock()
SETTINGS_LOCK = threading.RLock()   # settings.json 读-改-写串行化（可重入：save_settings 内部再取一次）
# 删除去向（设置项，存 settings.json）：app=应用内回收站 / system=Windows 回收站
DELETE_MODES = ("app", "system")
# 单次 /api/delete 的路径上限。真实图库上万张、列表视图「全选当前视图」很容易越过，
# 所以这个值刻意给得很宽（它只是防呆，不是分页）；真被截断时被丢弃的路径会并入
# failed 回报给前端 —— 详见 do_POST 里 /api/delete 那段（P1-1）。
DELETE_MAX_PATHS = 20000
# 仅测试用：允许用环境变量把上限压小，好复现「超限截断」这条分支（见 _delete_max_paths）。
try:
    DELETE_MAX_PATHS = int(os.environ.get("CMV_TEST_DELETE_MAX") or DELETE_MAX_PATHS)
except ValueError:
    pass

# P1-3：回收站保留天数的环境变量覆盖（0=关闭自动清理）。解析在 load_trash_keep_days()，
# 那里是权威；这里只做一次「提前暴露」——启动早期就把非法值挡掉并记一条日志。
TRASH_KEEP_DAYS_ENV = os.environ.get("CMV_TRASH_KEEP_DAYS")

STATE = {"root": None}
STATE_LOCK = threading.Lock()
FAV_LOCK = threading.Lock()
TAGS_LOCK = threading.Lock()


# ---------------------------------------------------------------- utilities

MAX_TEXT_BYTES = 64 << 20   # 单个文本元数据块解压上限（防 zTXt/iTXt 解压炸弹）
META_BATCH_MAX = 3000       # 筛选面板现场解析上限（索引命中时不受此限）


def _tmp_tag() -> str:
    """临时文件名标签：PID.线程号。

    ε-13（§6.6）：此前只带线程号不带 PID。线程号在 Windows 上是递增分配的、
    且不同进程经常撞号 —— 两个 CMV 进程（如重启竞态期间新旧并存）同时写
    favorites.json 时，旧进程的 `.123.tmp` 和新进程的 `.123.tmp` 是**同一个文件名**，
    后写者会截断先写者，再各自 replace 成正式文件 ⇒ 数据悄悄损坏。
    加上 PID 后每个进程的临时名必然不同，跨进程互斥成立。
    """
    return "%d.%d" % (os.getpid(), threading.get_ident())


_OPENER_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def load_favs() -> dict:
    """收藏表：{绝对路径: {"t": 时间戳, "cat": 分类名}}；兼容旧格式 {路径: 时间戳}。

    P1-6：单条记录的转换**必须**自己兜住异常。此前 `int(v["t"])` 在 try 之外——
    文件里只要有一条 `{"t": "??"}` / `{"t": null}` / `{"t": {}}`（手改、别的工具写坏、
    旧版本留下的脏值），整个 load_favs 就在 `int()` 上抛 TypeError/ValueError，
    而调用点全在请求线程里 ⇒ **整个服务起不来**（连首页都打不开），且报错只有一行
    "int() argument must be..."，看不出是哪个文件坏了。现在坏的那条跳过、其余照读。
    """
    if _SQL_ORG is not None:
        return {path: {"t": int(value["t"]) if value["t"] is not None else None,
                       "cat": value.get("cat") or "默认"}
                for path, value in _SQL_ORG.projection()["favorites"].items()}
    try:
        data = json.loads(FAV_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    dropped = 0
    if isinstance(data, dict):
        for k, v in data.items():
            try:
                if isinstance(v, dict) and v.get("t"):
                    out[str(k)] = {"t": int(v["t"]), "cat": str(v.get("cat") or "默认")}
                elif isinstance(v, (int, float)) and not isinstance(v, bool):
                    out[str(k)] = {"t": int(v), "cat": "默认"}
                else:
                    # ε-13（§6.6）：t=0 / 缺失也是「坏条目」，丢弃但不许静默 ——
                    # 用户看到收藏少了一条，得能在日志里找到原因。
                    dropped += 1
                    log(f"[warn] 收藏记录无效（t 缺失或为 0），已跳过：{k!r}")
            except (TypeError, ValueError):
                dropped += 1
                log(f"[warn] 收藏记录损坏，已跳过：{k!r}")
    if dropped:
        log(f"[warn] favorites.json 共丢弃 {dropped} 条无效记录")
    return out


class WriteError(Exception):
    """写盘失败。

    收藏 / 标签的保存此前返回 False 却没人检查，界面照常弹「已收藏」，
    刷新后数据就没了（磁盘只读 / 文件被占用 / 路径失效）。
    改成异常后由 do_POST 的兜底转成 500 + error，前端才会如实提示失败。
    """


def _need(ok: bool, what: str) -> bool:
    """写盘返回值必须检查：失败就抛，绝不假装成功。"""
    if not ok:
        raise WriteError(f"{what}写入失败（磁盘不可写或文件被占用）")
    return True


# ============================================================================
# B1-3（2026-10-09 全代码审计 · CMV3-LOCAL-FIX，从旧工程回植）
#
# 病根：数据目录不可写（便携模式指向只读 U 盘 / 外接盘掉线 / 写保护 SD 卡 /
#   JSON 被杀软或编辑器占用）时，常用目录、上次目录、删除偏好全都
#   「看着改了、重启回原样」，用户界面上**没有任何提示** —— 日志只写在数据目录
#   里的 log 文件里，而那个目录恰好写不进去。
#
# 处置：① 把「最近一次写盘失败」做成进程级信号，由 `/api/info` / `/api/sysinfo`
#         带出去，设置页红字告警；
#      ② 启动时对数据目录做一次可写自检（写一个探针文件再删掉）；
#      ③ 调用方（`/api/root`、`/api/prefs`）拿到失败必须如实回传，不再把
#         「写盘失败」和「值非法」混成同一句 400。
# ============================================================================
_DATA_WRITE = {"ok": True, "at": 0.0, "what": "", "error": ""}
_DATA_WRITE_LOCK = threading.Lock()
# 启动自检结果：None=还没测 / True=可写 / False=不可写
_DATA_DIR_PROBE = [None]


def _note_write_fail(what: str, ex) -> None:
    """记下「最近一次数据写盘失败」（供 /api/info 上报）。"""
    with _DATA_WRITE_LOCK:
        _DATA_WRITE.update({"ok": False, "at": time.time(), "what": what,
                            "error": f"{type(ex).__name__}: {ex}"})


def _note_write_ok() -> None:
    """写盘成功时复位信号（只在曾经失败过时记时间，避免每次都动锁）。"""
    with _DATA_WRITE_LOCK:
        if not _DATA_WRITE["ok"]:
            _DATA_WRITE.update({"ok": True, "at": time.time(), "what": "", "error": ""})


def probe_data_dir() -> bool:
    """启动自检：数据目录是否真的可写（**建目录 + 写探针 + 删探针**）。

    只做 `os.access(DATA_DIR, os.W_OK)` 是不够的 —— Windows 上写保护介质与
    被占用的目录都可能让 access 报真而实际写入失败。真写一次才算数。
    """
    ok = False
    probe = None
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe = DATA_DIR / f".write-probe.{_tmp_tag()}"
        probe.write_text("ok", "utf-8")
        ok = True
    except OSError as ex:
        log(f"[warn] 数据目录不可写（{DATA_DIR}）：{type(ex).__name__}: {ex} —— "
            f"设置 / 收藏 / 标签 / 索引都会「看着改了、重启回原样」，"
            f"界面会在设置页显式告警（请检查该目录是否只读 / 所在盘是否掉线）")
        _note_write_fail("数据目录自检", ex)
    finally:
        if probe is not None:
            try:
                probe.unlink()
            except OSError:
                pass
    _DATA_DIR_PROBE[0] = ok
    return ok


def data_write_state() -> dict:
    """数据目录写入状态（启动自检 + 最近一次实际写盘结果）。"""
    with _DATA_WRITE_LOCK:
        st = dict(_DATA_WRITE)
    st["dir_writable"] = _DATA_DIR_PROBE[0]
    return st


# P1-2 测试注入用；正常运行为 None，见 save_favs / _restore_meta。
# ε-4：允许**启动时**由环境变量置位（沿用 CMV_TEST_* 惯例）—— 否则进程外无法让
# 「/api/delete 的元数据回写」失败，那条静默路径（文件删了、收藏没清干净）就永远验不到。
_FAIL_SAVE_AT = os.environ.get("CMV_TEST_FAIL_SAVE_AT") or None


def save_favs(favs: dict):
    try:
        # ⚠️ 注入开关只对「恢复回写」生效（_FAIL_SAVE_AT 由 _restore_meta 置位、返回前复位），
        # 不用环境变量全局生效 —— 全局生效会把「设置收藏」这一步本身也打挂，
        # 于是回收站条目里根本没有收藏快照，还原时无事可做，永远复现不出 partial。
        if _FAIL_SAVE_AT == "favs":
            raise OSError("测试注入：save_favs 指定失败")
        if _SQL_BULK is not None:
            _SQL_BULK.replace_favorites(favs)
            return True
        FAV_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FAV_FILE.with_suffix(f".{_tmp_tag()}.tmp")
        tmp.write_text(json.dumps(favs, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(FAV_FILE)
        _note_write_ok()                     # B1-3（CMV3-LOCAL-FIX）：写成功即复位告警信号
        return True
    except Exception as ex:
        log(f"[warn] 收藏写入失败: {type(ex).__name__}: {ex}")
        _note_write_fail("收藏", ex)          # B1-3：不再只留一行日志，界面能看见
        return False


def _load_fav_seq() -> dict:
    """收藏操作的单调序号表：{绝对路径: 已见过的最大 seq}。

    FAV-01：单独立表（而非塞进 favorites.json）的原因见 set_fav 的注释 ——
    取消收藏会删记录，若 seq 随记录一起删，「更早的 set true」后到达时
    又会把已取消的项加回来。坏条目跳过，绝不让脏值把服务打挂（同 load_favs 的教训）。
    """
    if _SQL_ORG is not None:
        return _SQL_ORG.projection()["seq"]
    try:
        data = json.loads(FAV_SEQ_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    if isinstance(data, dict):
        for k, v in data.items():
            try:
                out[str(k)] = int(v)
            except (TypeError, ValueError):
                continue
    return out


def _save_fav_seq(seq: dict):
    try:
        if _SQL_ORG is not None:
            raise WriteError("SQLite 收藏序号必须与收藏意图同事务提交")
        FAV_SEQ_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FAV_SEQ_FILE.with_suffix(f".{_tmp_tag()}.tmp")
        tmp.write_text(json.dumps(seq, ensure_ascii=False), "utf-8")
        tmp.replace(FAV_SEQ_FILE)
        return True
    except Exception as ex:
        # 序号表仅用于「后点击者胜」的强保证；写失败不应阻断收藏本身
        log(f"[warn] 收藏序号写入失败: {type(ex).__name__}: {ex}")
        return False


def toggle_fav(path: str, cat: str = "默认") -> bool:
    with FAV_LOCK:
        if _SQL_ORG is not None:
            projection = _SQL_ORG.projection()
            return _SQL_ORG.set_favorite(path=path, favorite=path not in projection["favorites"],
                                          seq=projection["seq"].get(path, 0) + 1,
                                          category=cat)["favorite"]
        favs = load_favs()
        if path in favs:
            del favs[path]
            fav = False
        else:
            favs[path] = {"t": int(time.time()), "cat": cat or "默认"}
            fav = True
        _need(save_favs(favs), "收藏")
    return fav


def set_fav(path: str, want: bool, cat: str = "默认", seq: int = 0) -> bool:
    """把收藏状态**显式置为** want（幂等 + 单调序保护）。

    FAV-01（前端重写契约）：旧 toggle_fav 是「翻转」语义，不幂等 ——
    前端连点两次收藏/取消时，若两个请求**乱序到达服务端**（先发的后到），
    服务端会按错误的顺序翻转，磁盘落成与用户最后一次点击相反的状态。
    实测（3/3 确定性）：末次点击=收藏 ⇒ 磁盘 fav=true，但界面因 favTok 守卫
    丢弃了先发请求的响应而停在 false —— 用户以为没收藏，实际已收藏。

    ⚠️ 只做「幂等置位」还不够（实测证实）：两次点击的意图不同（一次 set true、
    一次 set false），若 set false 后到达，磁盘仍会落成 false ——
    **后到达者胜，而不是后点击者胜**。故必须引入**单调序号**：
    前端为每次操作分配递增 seq，服务端记住每个文件已见过的最大 seq，
    仅当 `seq >= 已记录值` 才应用，否则视为过期写入直接丢弃。

    这样才是真正的「最后表达意图胜出」：乱序、重放、重复投递都收敛到用户
    最后一次点击的结果。seq 缺省 0 时退化为纯幂等置位（兼容旧前端）。

    向后兼容：不传 fav 的旧前端走 toggle_fav（见 /api/fav 分发）；
    不传 seq 的新前端仍可享受幂等，只是丢了「后点击者胜」的强保证。
    """
    with FAV_LOCK:
        if _SQL_ORG is not None:
            from backend.storage.organization import StaleIntentError
            current = _SQL_ORG.projection()["seq"].get(path, 0)
            effective_seq = seq if seq else current + 1
            try:
                return _SQL_ORG.set_favorite(path=path, favorite=want,
                                              seq=effective_seq, category=cat)["favorite"]
            except StaleIntentError:
                return path in load_favs()
        favs = load_favs()
        rec = favs.get(path)
        # ---- 单调序保护（关键）----
        # 不能把 seq 只存在已收藏记录里：取消收藏会删记录，seq 随之丢失，
        # 于是「更早的 set true」后到达时又会把它加回来（实测确认过这个漏洞）。
        # 故 seq 单独存一张小表（含未收藏项），与收藏表独立持久化。
        seen = _load_fav_seq()
        if seq and int(seen.get(path) or 0) > seq:
            return path in favs        # 过期写入 ⇒ 丢弃，维持现状
        if seq:
            seen[path] = seq
            _save_fav_seq(seen)
        if want:
            if rec is None:
                favs[path] = {"t": int(time.time()), "cat": cat or "默认"}
            elif cat:
                rec["cat"] = cat
        else:
            if rec is None:
                return False           # 已是未收藏 ⇒ 无操作（幂等，不写盘）
            del favs[path]
        _need(save_favs(favs), "收藏")
    return want


def set_fav_cat(path: str, cat: str):
    """修改已收藏条目的分类；未收藏返回 None。"""
    with FAV_LOCK:
        favs = load_favs()
        if path not in favs:
            return None
        favs[path]["cat"] = cat or "默认"
        _need(save_favs(favs), "收藏分类")
        return True


def fav_cats() -> list:
    """分类列表 [{name, count}]，按数量降序；预建分类（0 条）也保留显示。"""
    cnt = {}
    for v in load_favs().values():
        c = v.get("cat") or "默认"
        cnt[c] = cnt.get(c, 0) + 1
    for name in _preset_cats():          # 预建分类持久化：新建后不挂图也不丢
        if name not in cnt:
            cnt[name] = 0
    return [{"name": k, "count": n} for k, n in
            sorted(cnt.items(), key=lambda x: -x[1])]


def _preset_cats() -> list:
    """settings.json 里持久化的预建分类名。"""
    if _SQL_ORG is not None:
        return [category["name"] for category in _SQL_ORG.projection()["cats"]]
    return [c for c in load_settings().get("fav_cats", [])
            if isinstance(c, str) and c.strip()]


def add_preset_cat(name: str) -> bool:
    """预建分类并持久化；重名（含已有收藏的分类）返回 False。

    ε-13（§6.6）：判重（读）与写覆盖此前是「锁外读、锁内整体覆盖」——
    两个并发 add 都读不到对方，后写者把先写者的新分类整个抹掉。
    读也必须放进锁里，读-改-写才算原子。
    """
    name = (name or "").strip()
    if not name:
        return False
    with SETTINGS_LOCK:
        cats = _preset_cats()
        if name in cats or any(v.get("cat") == name for v in load_favs().values()):
            return False
        if _SQL_ORG is not None:
            _SQL_ORG.repository.create_category(_SQL_ORG.profile_uuid, name)
            return True
        cats.append(name)
        s = load_settings()
        s["fav_cats"] = cats
        save_settings(s)
    return True


def del_preset_cat(name: str) -> bool:
    """删除预建分类；该分类下仍有收藏时拒绝（先清空分类再删）。"""
    with SETTINGS_LOCK:
        cats = _preset_cats()
        if name not in cats:
            return False
        if any(v.get("cat") == name for v in load_favs().values()):
            return False
        if _SQL_ORG is not None:
            category = next(c for c in _SQL_ORG.projection()["cats"] if c["name"] == name)
            _SQL_ORG.repository.delete_category(_SQL_ORG.profile_uuid, category["uuid"])
            return True
        cats.remove(name)
        s = load_settings()
        s["fav_cats"] = cats
        save_settings(s)
    return True


# ---- 悬空条目清理（收藏 / 标签共用的安全悬空判定）-----------------------
# ⚠️ P1-5：这一族函数是「启动即执行、无备份、无撤销」的整批删除，判定错了用户的
#   收藏就成批归零。原实现用 Path(k).is_file() 判悬空，而 is_file() 内部**吞掉
#   OSError 返回 False**（Python 3.8+ 语义），所以外层那个 `except OSError` 形同虚设：
#     · root 在外接盘 / 网络盘上，启动瞬间盘还没就绪 → 全部收藏被判悬空；
#     · root 下有当前进程无权遍历的目录 → 文件其实存在，被判悬空；
#     · 路径超过 260 字符且未开 LongPathsEnabled → 同上；
#     · 断链的符号链接 / junction → 文件本体可能还在别处。
#   三条防线：
#     ① 只对「确定不存在」（FileNotFoundError / NotADirectoryError）判悬空，
#        其余 OSError（权限、设备未就绪、路径过长）一律跳过；
#     ② 安全阀：root 根本读不到、或本次判定的悬空比例过高时**放弃清理并记日志**；
#     ③ 删除前把整份原表备份成 *.bak，用户/我们能据此恢复。
PRUNE_MAX_RATIO = 0.3      # 大样本（≥ PRUNE_RATIO_MIN 条）时，一次删超过这个比例就停下
PRUNE_MAX_ABS = 200        # 或绝对条数超过这个数也停下
PRUNE_RATIO_MIN = 20       # 比例判据的生效下限：样本太少时比例没有统计意义
PRUNE_SMALL_ABS = 8        # 小样本（< 20 条）时，一次最多清这么多条


def _volume_anchor(path: str):
    """路径所属的「卷根」：盘符根（`C:\\`）或 UNC 共享根（`\\\\host\\share\\`）。取不到返回 None。

    CMV3-LOCAL-FIX(P0-18)：从旧工程回植（旧 server.py:477）。
    """
    try:
        p = os.path.abspath(path)
    except (OSError, ValueError):
        return None
    drive, rest = os.path.splitdrive(p)
    if drive:
        return drive + os.sep
    if rest.startswith("\\\\"):
        seg = [x for x in rest.lstrip("\\").split("\\") if x]
        if len(seg) >= 2:
            return "\\\\" + seg[0] + "\\" + seg[1] + os.sep
    return None


def _anchor_online(path: str, memo: dict = None) -> bool:
    """该路径所在卷的卷根**当前是否可访问**（memo 按卷根缓存，一次清理只探一次）。

    CMV3-LOCAL-FIX(P0-18)：从旧工程回植（旧 server.py:493）。
    """
    a = _volume_anchor(path)
    if a is None:
        return False          # 连卷根都取不出来 ⇒ 不敢判悬空
    if memo is not None and a in memo:
        return memo[a]
    try:
        os.scandir(a).close()
        ok = True
    except OSError:
        ok = False
    if memo is not None:
        memo[a] = ok
    return ok


def _definitely_missing(path: str, _memo: dict = None) -> bool:
    """只有当路径**确定不存在**时才返回 True；不可判定（盘未挂载 / 权限 / 设备未就绪）返回 False。

    P0-18：在 Windows 上「盘不在」与「文件真删了」都抛 `FileNotFoundError`
    （实测 winerror 3 / 53 / 67 三种全走这里），**仅凭异常类型无法区分** —— 于是外接盘
    掉线时收藏会被判成悬空**永久删除**（不进回收站，`.bak` 只有一代）。所以判悬空前
    先独立取证「这个路径所在的卷还在不在」：卷根都扫不动就一律保守留下。
    代价是每个候选多一次 `os.scandir`（按卷根 memo，9700 条仍是毫秒级）。

    CMV3-LOCAL-FIX(P0-18)：从旧工程回植（旧 server.py:514）。
    """
    if not _anchor_online(path, _memo):
        return False          # 盘未挂载 / UNC 不可达 ⇒ 不可判定，保留
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return True           # 卷在线、路径确实没了 ⇒ 真删了
    except NotADirectoryError:
        return True
    except OSError:
        return False          # 不可判定 ⇒ 保守地留下，绝不当成悬空
    return not stat_mod.S_ISREG(st.st_mode) and not stat_mod.S_ISDIR(st.st_mode)


def _prune_dangling(entries: dict, root: str, kind: str) -> list:
    """在 entries 里挑出 root 下「确定已不存在」的键，返回键列表（**不修改** entries）。

    kind 只用于日志文案（收藏 / 标签）。安全阀不通过时返回空列表。
    """
    try:
        pref = str(Path(root).resolve()).lower().rstrip("\\/")
    except OSError:
        return []
    # ① root 自己读不到 ⇒ 这个盘/目录现在不可信，一律不清理
    try:
        os.scandir(root).close()
    except OSError as ex:
        log(f"[prune] 跳过{kind}清理：root 当前不可读（{root}）：{type(ex).__name__}: {ex}")
        return []
    mine, gone = [], []
    memo: dict = {}          # P0-18：卷根可达性按卷缓存，避免逐条重复 scandir
    for k in entries:
        kl = k.lower().rstrip("\\/")
        if kl != pref and not kl.startswith(pref + os.sep):
            continue                       # 只碰 root 内的条目；盘未挂载时不误判别的 root
        mine.append(k)
        if _definitely_missing(k, memo):
            gone.append(k)
    # ② 安全阀：绝对数超限 ⇒ 更可能是环境问题，不是用户真删了这么多
    if len(gone) > PRUNE_MAX_ABS:
        log(f"[prune] 跳过{kind}清理：本次判定悬空 {len(gone)}/{len(mine)} 条，"
            f"超过绝对阈值 {PRUNE_MAX_ABS} 条，疑似盘未就绪 —— "
            f"如需强制清理请手工编辑数据目录里的 JSON")
        return []
    # ③ 比例安全阀：只在样本足够大时才用。小收藏集里「10 条删掉 4 条」完全正常，
    #    拿比例去拦会变成「用户正常操作被拒绝」，比不拦更糟（首轮实测抓到的自己写的 bug）。
    if len(mine) >= PRUNE_RATIO_MIN and len(gone) > max(PRUNE_SMALL_ABS,
                                                        len(mine) * PRUNE_MAX_RATIO):
        log(f"[prune] 跳过{kind}清理：本次判定悬空 {len(gone)}/{len(mine)} 条，"
            f"超过比例阈值 {int(PRUNE_MAX_RATIO*100)}%，疑似盘未就绪 —— "
            f"如需强制清理请手工编辑数据目录里的 JSON")
        return []
    return gone


def _backup_json(path: Path) -> str:
    """清理前把数据文件原样备份一份（*.bak），返回备份路径（失败返回空串）。"""
    try:
        if not path.is_file():
            return ""
        bak = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(str(path), str(bak))
        return str(bak)
    except OSError as ex:
        log(f"[prune] 备份 {path.name} 失败：{type(ex).__name__}: {ex}")
        return ""


def prune_favs(root: str) -> int:
    """清理 root 下文件已不存在的悬空收藏，返回清理条数。

    只碰 root 内的条目 —— 其他盘 / 其他 root 的收藏在盘未挂载时不误判悬空。
    判定与安全阀见 `_prune_dangling`；写盘前先备份（P1-5）。
    """
    with FAV_LOCK:
        favs = load_favs()
        gone = _prune_dangling(favs, root, "收藏")
        if not gone:
            return 0
        _backup_json(FAV_FILE)
        for k in gone:
            favs.pop(k, None)
        if not save_favs(favs):
            log("[prune] 悬空收藏清理后写盘失败，本次改动未生效")
        log(f"[prune] 已清理 {len(gone)} 条悬空收藏（原表已备份为 {FAV_FILE.name}.bak）")
    return len(gone)

# ---------------------------------------------------------------- tags(标签)

TAG_SPECIAL = "`~!@#$%^&*()+=[]{}|\\:;\"'<>?,"   # 标签名禁用字符（"/" 作层级分隔）


def _norm_tag(t) -> str:
    """规范化标签名：去首尾空白；禁含特殊字符；层级段用 / 分隔。非法返回空串。"""
    if not isinstance(t, str):
        return ""
    t = "/".join(s.strip() for s in t.split("/") if s.strip())
    if not t or len(t) > 40:
        return ""
    for ch in t:
        if ch in TAG_SPECIAL and ch != "/":
            return ""
    if any(not seg or len(seg) > 16 for seg in t.split("/")):
        return ""
    return t


def load_tags() -> dict:
    """标签表：{绝对路径: [标签...]}；幂等去重、过滤非法名。"""
    if _SQL_ORG is not None:
        return _SQL_ORG.projection()["tags"]
    try:
        data = json.loads(TAGS_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    dropped = 0
    if isinstance(data, dict):
        for k, v in data.items():
            if not isinstance(v, list):
                dropped += 1
                continue
            seen, ok = [], set()
            for t in v:
                t = _norm_tag(t)
                if t and t not in seen and t not in ok:
                    seen.append(t)
                    ok.add(t)
            if seen:
                out[k] = seen
            else:
                # ε-13（§6.6）：全非法名 / 非列表条目整条丢弃必须留痕（ε-13 口径同 favorites）
                dropped += 1
        if dropped:
            log(f"[warn] tags.json 共丢弃 {dropped} 条无效记录（值非列表或标签全部非法）")
    return out


def save_tags(tags: dict):
    try:
        if _SQL_BULK is not None:
            _SQL_BULK.replace_tags(tags)
            return True
        TAGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = TAGS_FILE.with_suffix(f".{_tmp_tag()}.tmp")
        tmp.write_text(json.dumps(tags, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(TAGS_FILE)
        return True
    except Exception as ex:
        log(f"[warn] 标签写入失败: {type(ex).__name__}: {ex}")
        return False


def tags_of(path: str) -> list:
    return list(load_tags().get(path, []))


def tags_set(path: str, tags: list) -> list:
    """整表替换单图标签（前端先读后写）；返回落盘后的标签列表。"""
    clean, seen = [], set()
    for t in (tags or []):
        t = _norm_tag(t)
        if t and t not in seen:
            clean.append(t)
            seen.add(t)
    with TAGS_LOCK:
        ts = load_tags()
        if clean:
            ts[path] = clean
        else:
            ts.pop(path, None)
        _need(save_tags(ts), "标签")
    return clean


def tags_add(path: str, tag: str) -> list:
    """贴一个标签（不存在则建）；返回该图全部标签。"""
    tag = _norm_tag(tag)
    if not tag:
        return tags_of(path)
    with TAGS_LOCK:
        ts = load_tags()
        cur = ts.get(path, [])
        if tag not in cur:
            cur = cur + [tag]
        ts[path] = cur
        _need(save_tags(ts), "标签")
    return cur


def tags_remove(path: str, tag: str) -> list:
    """摘一个标签（按规范化名匹配）；返回该图剩余标签。"""
    tag = _norm_tag(tag)
    with TAGS_LOCK:
        ts = load_tags()
        cur = ts.get(path, [])
        if tag and tag in cur:
            cur = [t for t in cur if t != tag]
            if cur:
                ts[path] = cur
            else:
                ts.pop(path, None)
            _need(save_tags(ts), "标签")
    return list(ts.get(path, []))


def tags_add_many(paths: list, tags: list) -> dict:
    """P1-18：一次请求给多张图贴同一组标签。

    前端的「批量贴标签」原来是 **N×M 次串行 await**（500 次也不罕见），
    既慢又没法如实计数：任何一次失败只能笼统记一笔。这里在**一把锁里**读一次、
    写一次盘，逐张累计成功/失败，前端据此说出真话。
    返回 {"ok": 1/True, "pairs": 成功贴上的 (图,标签) 对数,
          "files": 真正改动的图数,
          "failed": [{"path":..., "error":...}], "tags_all": [...]}。

    P1（2026-10-09 · CMV3-LOCAL-FIX，从旧工程回植）：计数**只走 `pairs`**。原先计数落在
    `ok` 上，而 HTTP handler 返回前会 `r["ok"] = True` 把它覆盖成布尔值 —— 前端
    `j.ok||0` 恒为 1/true，界面上的「共 true 处新增」与「部分图本来已有该标签」的归因
    全是假的。`ok` 从此只当成功标志，数字另起字段，两个语义不再共用一个键。
    """
    clean, seen = [], set()
    for t in (tags or []):
        t = _norm_tag(t)
        if t and t not in seen:
            clean.append(t)
            seen.add(t)
    uniq = []
    dupes = set()
    for p in (paths or []):
        p = str(p or "")
        if p and p not in dupes:
            uniq.append(p)
            dupes.add(p)
    res = {"ok": 0, "pairs": 0, "files": 0, "failed": []}
    if not clean or not uniq:
        res["tags_all"] = tag_stats()
        return res
    # ⚠️ B1-2（2026-10-09 全代码审计 · CMV3-LOCAL-FIX，从旧工程回植）：落盘**必须留在锁内**
    #    —— 与 tags_set / tags_add / tags_remove 保持同一口径。此前 `_need(save_tags(ts))`
    #    写在 `with TAGS_LOCK:` 之外，而读取快照到写盘之间是一整表「读-改-写」：两个并发
    #    add_many（批量贴标签连点、多标签页同时操作；ThreadingHTTPServer 天然并发）各自在
    #    锁内拿到自洽快照，随后**都在锁外**写盘 —— 后落盘者用自己那份完整表覆盖前者，
    #    两个请求都返回成功，实际先完成的那批标签静默丢失（界面显示贴上、刷新后没了）。
    #    本函数的 docstring 自己写着「在一把锁里读一次、写一次盘」，实现却与注释不符。
    with TAGS_LOCK:
        ts = load_tags()
        for p in uniq:
            cur = list(ts.get(p, []))
            added = 0
            for t in clean:
                if t not in cur:
                    cur.append(t)
                    added += 1
            if added:
                ts[p] = cur
                res["ok"] += added
                res["pairs"] += added
                res["files"] += 1
        try:
            _need(save_tags(ts), "标签")
        except Exception as ex:                       # 落盘失败：如实报告，不虚报成功
            res["failed"].append({"path": "", "error": f"写入标签失败：{ex}"})
            res["ok"] = 0
            res["pairs"] = 0
            res["files"] = 0
    res["tags_all"] = tag_stats()
    return res


def tag_stats() -> list:
    """全部标签计数 [{name, count}] 按计数降序；层级子标签计入父级（风格/汉服 计入 风格）。"""
    cnt = {}
    for tags in load_tags().values():
        for t in tags:
            chain = ""
            for s in t.split("/"):
                chain = f"{chain}/{s}" if chain else s
                cnt[chain] = cnt.get(chain, 0) + 1
    return [{"name": k, "count": n} for k, n in
            sorted(cnt.items(), key=lambda x: (-x[1], x[0]))]


def tag_rename(from_name: str, to_name: str) -> dict:
    """全局改名（合并）：from→to；to 已存在时 from 的图并入 to。"""
    f, t = _norm_tag(from_name), _norm_tag(to_name)
    if not f or not t or f == t:
        return {"ok": False, "error": "无效的标签名"}
    moved = 0
    with TAGS_LOCK:
        ts = load_tags()
        for path, tags in list(ts.items()):
            if f in tags:
                # 目标标签已存在时只移除源标签，并去重，否则会留下两个「目标」标签
                merged = [t if x == f else x for x in tags]
                if t in tags:
                    merged = [x for x in tags if x != f]
                seen, out = set(), []
                for x in merged:
                    if x not in seen:
                        seen.add(x)
                        out.append(x)
                ts[path] = out
                moved += 1
        for path in [p for p, v in ts.items() if not v]:
            del ts[path]
        _need(save_tags(ts), "标签")
    return {"ok": True, "moved": moved, "to": t}


def tag_delete(name: str) -> dict:
    """全局删除标签：从所有图上摘除（含子层级标签，删「风格」会连带删「风格/汉服」）。"""
    n = _norm_tag(name)
    if not n:
        return {"ok": False, "error": "无效的标签名"}
    prefix = n + "/"
    removed = 0
    with TAGS_LOCK:
        ts = load_tags()
        for path, tags in list(ts.items()):
            kept = [x for x in tags if x != n and not x.startswith(prefix)]
            if len(kept) != len(tags):
                removed += 1
            if kept:
                ts[path] = kept
            else:
                ts.pop(path, None)
        _need(save_tags(ts), "标签")
    return {"ok": True, "removed": removed}


def prune_tags(root: str) -> int:
    """清理 root 下文件已不存在的悬空标签，返回清理条数（判定/安全阀/备份同 prune_favs）。"""
    with TAGS_LOCK:
        ts = load_tags()
        gone = _prune_dangling(ts, root, "标签")
        if not gone:
            return 0
        _backup_json(TAGS_FILE)
        for k in gone:
            ts.pop(k, None)
        if not save_tags(ts):
            log("[prune] 悬空标签清理后写盘失败，本次改动未生效")
        log(f"[prune] 已清理 {len(gone)} 条悬空标签（原表已备份为 {TAGS_FILE.name}.bak）")
    return len(gone)


def _tag_match(got: set, w: str) -> bool:
    """单个标签命中：精确命中，或 got 中有以其为父级前缀的标签（风格 命中 风格/汉服）。"""
    if w in got:
        return True
    p = w + "/"
    return any(t.startswith(p) for t in got)


def _tag_hit(got: set, want: set, mode: str) -> bool:
    """命中判定。mode: and=全部命中 / or=任一命中 / not=一个都不含（排除）。

    注意：mode="not" 时无标签的图也应命中（它当然不含被排除的标签）。
    """
    if mode == "not":
        return not any(_tag_match(got, w) for w in want)
    if mode == "and":
        return all(_tag_match(got, w) for w in want)
    return any(_tag_match(got, w) for w in want)


def tags_filter(tags: list, base_path: str = "", mode: str = "and", rec: bool = True,
                exclude: list = None) -> list:
    """按标签过滤：返回目录（含子目录）下命中媒体的 item 列表（同 api_list 形状）。

    mode: and = 全部命中；or = 任一命中；not = 选中标签一个都不含（排除）。
    exclude: 额外的排除标签（始终 AND-NOT，可与 and/or 组合成「有 A 且不含 B」）。
    父级标签命中其全部子标签(风格 命中 风格/汉服)。
    """
    tags = [_norm_tag(t) for t in (tags or [])]
    tags = [t for t in tags if t]
    excl = [_norm_tag(t) for t in (exclude or [])]
    excl = [t for t in excl if t]
    if not tags and not excl:
        return []
    ts = load_tags()
    favs = load_favs()        # 收藏状态：与 search_index / fav_view 口径一致（此前硬编码 False 导致星标丢失）
    want = set(tags)
    notwant = set(excl)
    base = str(Path(base_path or STATE["root"]).resolve())
    if not os.path.isdir(base):
        return []
    out = []
    for d, _sub, files in (os.walk(base) if rec else [(base, [], os.listdir(base))]):
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if ext in IMAGE_EXTS:
                t = "image"
            elif ext in VIDEO_EXTS:
                t = "video"
            else:
                continue
            sp = os.path.join(d, fn)
            got = set(ts.get(sp, []))
            if notwant and any(_tag_match(got, w) for w in notwant):
                continue                       # 命中任一排除标签 → 丢弃
            if want and not _tag_hit(got, want, mode):
                continue
            try:
                st = os.stat(sp)
                out.append({"name": fn, "path": sp, "type": t,
                            "size": st.st_size, "mtime": int(st.st_mtime),
                            "playable": t == "image" or ext in PLAYABLE_VIDEO,
                            "png": ext == ".png", "fav": sp in favs,
                            "favcat": favs[sp]["cat"] if sp in favs else None,
                            "tags": sorted(got)})
            except OSError:
                continue
    out.sort(key=lambda x: -x["mtime"])
    return out

# ---------------------------------------------------------------- 带缓存的 JSON 读盘
#
# 批次 F（F2 / F3，CMV3-LOCAL-FIX 从旧工程回植）：这些加载器此前一律
# 「read_text + json.loads」，**零缓存**。在逐条循环里被调一次就是上万次系统调用 ——
# 实测 `/api/list` 端到端 2.6 s、而同样一件事纯计算只要 41 ms，差的就是这里
# （settings.json 只有 306 B，开销全在 open/read/parse 本身）。统一走这一个工具，
# 把「读盘零缓存」这个**类别**消掉，而不是只修今天踩到的那一个。
#
# 缓存键取 (st_mtime_ns, st_size)：比 TTL 准（文件一改就失效），
# 又比秒级 mtime 稳（同一秒内改写也能区分）。
#
# ⚠️ 唯一且致命的陷阱：**写入方落盘后必须主动失效**（`_invalidate_json`），
#    否则「刚改的设置不生效」—— 批次 F 的 F2 负控就是专门钉这一条的。

_JSON_CACHE = {}
_JSON_CACHE_LOCK = threading.Lock()


def _json_fresh_key(path):
    """(mtime_ns, size)；文件不可 stat 时返回 None（调用方按「无缓存」处理）。"""
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _cached_json(path, default=None):
    """按 (mtime_ns, size) 缓存的 JSON 读盘。命中直接返回，未命中才 read + parse。

    返回**浅拷贝**（dict 顶层复制）而不是缓存对象本身：调用方普遍是
    「读 → 改 → 写回」（`save_delete_mode` / `tags_set` / `save_nsfw_*` …），
    把缓存对象交出去会被就地改脏，写盘失败时脏值还会留在缓存里。
    浅拷贝对「顶层增删键」已经够 —— settings.json 的嵌套值（`roots` 列表）
    在本项目里只有**整体替换**（`s["roots"] = roots[:8]`），没有就地改元素。

    解析失败（文件写坏）**不写缓存**：宁可每次重试，也不要把坏值钉住。
    """
    key = _json_fresh_key(path)
    if key is None:
        return default
    with _JSON_CACHE_LOCK:
        hit = _JSON_CACHE.get(str(path))
        if hit is not None and hit[0] == key:
            val = hit[1]
            return dict(val) if isinstance(val, dict) else val
    try:
        val = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return default
    with _JSON_CACHE_LOCK:
        _JSON_CACHE[str(path)] = (key, val)
    return dict(val) if isinstance(val, dict) else val


def _invalidate_json(path):
    """落盘之后调用。不调 = 下一个读者拿到旧值（F2 负控钉的就是这一条）。"""
    with _JSON_CACHE_LOCK:
        _JSON_CACHE.pop(str(path), None)


# ---------------------------------------------------------------- settings

def load_settings() -> dict:
    """批次 F（F2）：走 `_cached_json`。

    此前是每次 `read_text` + `json.loads`，而它在 NSFW 逐条判定路径上被调用 ——
    实测一次 `/api/list` 就是 **9726 次读盘**，这也是那 2.6 s 的主因。
    """
    d = _cached_json(SETTINGS_FILE, {})
    return d if isinstance(d, dict) else {}


def save_settings(d: dict):
    with SETTINGS_LOCK:
        return _save_settings_locked(d)


def _save_settings_locked(d: dict):
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(f".{_tmp_tag()}.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(SETTINGS_FILE)
        _invalidate_json(SETTINGS_FILE)      # F2（CMV3-LOCAL-FIX）：写后必须失效，否则读回旧值
        _note_write_ok()                     # B1-3（CMV3-LOCAL-FIX）：写成功即复位告警信号
        return True
    except OSError as ex:
        log(f"[warn] 设置写入失败: {ex}")
        _note_write_fail("设置", ex)          # B1-3：换根记忆 / 偏好全靠它，必须能被 /api/info 带出去
        return False


def load_last_root() -> str:
    """上次浏览的根目录（仍存在才返回）。

    P0-15（CMV3-LOCAL-FIX，从旧工程回植）：settings.json 可能被手改 / 写坏，
    `last_root` 不是字符串时 `Path(r)` 会抛 TypeError —— 本函数在启动路径上，
    等于**启动即崩**。类型不对就当没有。
    """
    r = load_settings().get("last_root")
    if isinstance(r, str) and r and Path(r).is_dir():
        return str(Path(r).resolve())
    return None


def load_delete_mode() -> str:
    """删除去向偏好：app=应用内回收站（默认，可在软件里还原）/ system=Windows 回收站。"""
    m = str(load_settings().get("delete_mode") or "").strip().lower()
    return m if m in DELETE_MODES else "app"


def save_delete_mode(m: str) -> bool:
    """写入删除去向偏好（非法值直接拒绝，避免把设置写成脏值）。"""
    m = str(m or "").strip().lower()
    if m not in DELETE_MODES:
        return False
    with SETTINGS_LOCK:
        s = load_settings()
        s["delete_mode"] = m
        return save_settings(s)


def valid_keep_days(days) -> bool:
    """trash_keep_days 的合法性（与 save_trash_keep_days 同口径）。

    B1-3（CMV3-LOCAL-FIX，从旧工程回植）：独立出来是为了让 `/api/prefs` 能
    **先校验、后写盘** —— 否则「值非法」与「写盘失败」共用同一个 False，
    数据目录不可写时会被报成「trash_keep_days 只能是 0-3650 的整数」，
    用户怎么改都不对。
    """
    try:
        n = int(str(days).strip())
    except (TypeError, ValueError):
        return False
    return 0 <= n <= 3650


def save_trash_keep_days(days) -> bool:
    """写入回收站保留天数（P1-3）。非法值直接拒绝，避免把设置写成脏值。"""
    if not valid_keep_days(days):
        return False
    n = int(str(days).strip())
    with SETTINGS_LOCK:
        s = load_settings()
        s["trash_keep_days"] = n
        return save_settings(s)


def load_roots() -> list:
    """常用目录列表（仍存在的），最新在前。"""
    out = []
    for r in load_settings().get("roots", []):
        if not r:
            continue
        try:
            if Path(r).is_dir():
                out.append(str(Path(r).resolve()))
        except OSError:
            pass
    return out


def add_root(root: str) -> bool:
    """把 root 记入常用目录（去重、置顶），同时更新 last_root，最多 8 个。

    B1-3（2026-10-09 · CMV3-LOCAL-FIX，从旧工程回植）：返回**是否真的持久化成功**。
    此前无返回值、调用方也都不看，数据目录不可写时「常用目录」看着加上了、重启全没了，
    且界面上没有任何提示。
    """
    r = str(root)
    with SETTINGS_LOCK:
        s = load_settings()
        roots = [x for x in s.get("roots", []) if x != r]
        roots.insert(0, r)
        s["roots"] = roots[:8]
        s["last_root"] = r
        return bool(save_settings(s))


def remove_root(root: str) -> bool:
    """只移除常用目录记录，不操作目录或其中的文件。"""
    with SETTINGS_LOCK:
        s = load_settings()
        roots = s.get("roots", [])
        if root not in roots:
            return False
        s["roots"] = [r for r in roots if r != root]
        if s.get("last_root") == root:
            s["last_root"] = s["roots"][0] if s["roots"] else ""
        return save_settings(s)


def _find_output_under(base: Path, maxdepth: int = 4):
    """在 base 下递归查找名为 output 的子目录（限深度，栈式迭代）。

    用于识别 ComfyUI_windows_portable_amd 这种嵌套布局里的 output。
    """
    stack = [(base, 0)]
    while stack:
        d, depth = stack.pop()
        if depth > maxdepth:
            continue
        try:
            for child in d.iterdir():
                if not child.is_dir():
                    continue
                if child.name.lower() == "output":
                    yield child
                elif depth < maxdepth:
                    stack.append((child, depth + 1))
        except OSError:
            pass


def _fixed_drives():
    """枚举本地盘（固定盘 + 可移动盘；跳过光驱/网络映射盘）。

    ε-13（§6.6）：此前只认 DRIVE_FIXED(3)，USB 外接盒（用户的 ComfyUI 就在
    G: 外接 NVMe 上）若是 DRIVE_REMOVABLE(2) 就既探测不到也选不到 ——
    盘符栏直接没有 G:，自动找 output 也扫不到它。GetDriveTypeW 本身不触媒体、
    不会卡；真正可能卡的是后续 iterdir，那层本来就有 OSError 兜底。
    网络盘(4) / 光驱(5) 维持排除（探测时长时间卡住的元凶）。
    """
    import string
    try:
        import ctypes
        get_type = ctypes.windll.kernel32.GetDriveTypeW
        for letter in string.ascii_uppercase:
            try:
                if get_type(letter + ":\\") in (2, 3):   # DRIVE_REMOVABLE / DRIVE_FIXED
                    yield Path(letter + ":\\")
            except Exception:
                continue
    except (AttributeError, OSError):                  # 非 Windows 兜底
        for letter in string.ascii_uppercase:
            yield Path(letter + ":\\")


def _fixed_dirs() -> list:
    """返回固定盘符字符串列表（如 [\"C:\\\\\", \"D:\\\\\"]），供目录选择器盘符栏用。"""
    return [str(d) for d in _fixed_drives()]


def _detect_comfyui_output():
    """自动搜索 ComfyUI 的 output 目录。

    只浅层扫描各固定盘根目录的前两级，寻找名称以 ComfyUI 开头的目录
    （如 ComfyUI、ComfyUI-aki-v3），并在其下递归（限深度）寻找 output
    子目录。不做全盘递归，保证启动速度。
    """
    candidates = []
    for drive in _fixed_drives():
        try:
            if not drive.is_dir():
                continue
            tops = [t for t in drive.iterdir() if t.is_dir()]
        except OSError:
            continue
        bases = list(tops)
        for t in tops:  # 第二级，覆盖 D:\AI\ComfyUI 这类布局
            try:
                bases += [t / s for s in t.iterdir() if (t / s).is_dir()]
            except OSError:
                pass
        for b in bases:
            try:
                if not b.name.lower().startswith("comfyui"):
                    continue
                # 匹配到 comfyui* 后在其下递归找 output（覆盖便携版嵌套布局）
                outs = set(str(o.resolve()) for o in _find_output_under(b))
                candidates.extend(sorted(outs))
            except OSError:
                pass
    if not candidates:
        return None
    # 多个候选时选条目最多的那个（通常是主力出图目录）
    best, best_n = None, -1
    for c in candidates:
        try:
            n = sum(1 for _ in Path(c).iterdir())
        except OSError:
            n = 0
        if n > best_n:
            best, best_n = c, n
    return best


def default_root() -> str:
    r = load_last_root()          # 记住用户上次选的目录
    if r:
        return r
    d = _detect_comfyui_output()
    if d:
        return d
    # ε-13（§6.6）：Path.home()/Path.cwd() 此前在 try **之外**求值 ——
    # cwd 被删（从临时目录启动后目录消失）时 Path.cwd() 直接抛 FileNotFoundError，
    # 整个 default_root 连兜底都走不到。现在求值本身也包进 try。
    try:
        for c in (Path.home() / "Pictures", Path.home() / "Documents"):
            try:
                if c.is_dir():
                    return str(c.resolve())
            except (OSError, RuntimeError):
                pass
        return str(Path.cwd().resolve())
    except (OSError, RuntimeError):
        return str(Path(__file__).resolve().parent)


def _count_media(d: Path) -> int:
    """递归统计目录内媒体文件数（用于文件夹卡片）；scandir 栈式遍历，一趟完成。

    带 visited(realpath) 去重 + 深度上限，防 junction/符号链接环导致重复计数或死循环。
    """
    media = IMAGE_EXTS | VIDEO_EXTS
    n, stack = 0, [(str(d), 0)]
    visited = {os.path.realpath(str(d))}
    while stack:
        cur, depth = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir():
                            if depth < 10:
                                rp = os.path.realpath(e.path)
                                if rp not in visited:
                                    visited.add(rp)
                                    stack.append((e.path, depth + 1))
                        elif os.path.splitext(e.name)[1].lower() in media:
                            n += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return n


def _dir_media_count(d: str) -> int:
    """单层目录内媒体文件数（scandir 一趟，DirEntry 类型信息免二次 stat）。"""
    media = IMAGE_EXTS | VIDEO_EXTS
    try:
        with os.scandir(d) as it:
            return sum(1 for e in it
                       if e.is_file() and
                       os.path.splitext(e.name)[1].lower() in media)
    except OSError:
        return 0


def _inside_registered_root(q: Path) -> bool:
    """该路径是否位于**本库已登记的某个根**之下，且在那里有媒体身份。

    F1（跨根媒体读取）：跨根收藏 / 最近浏览 / 标记这类视图的查询范围是全库，
    但 inside_root() 只认 STATE["root"]（当前活动根），于是「能列出来、读不到」——
    /api/thumb 403、/api/file 与 /api/meta 404。活动根不该成为全库视图中
    媒体的唯一读取根。

    放行条件是**两个都必须成立**，缺一不可：
      ① 路径在本库某条 library_root 之下（枚举已登记根，不接受任意外部目录）；
      ② 该路径在本库目录里确实有媒体身份（ServerCatalog.current 按 library 过滤）。

    只做①会变成「任何已登记根下的任意文件都能读」，那是越权；只做②则等于放行
    整个磁盘。所以这里必须查目录身份，而不是只比路径前缀。
    """
    catalog = _SQL_CATALOG
    if catalog is None:
        return False
    # 枚举本库已登记根：库标识由 catalog 自带，这里不另行传入，避免调用方各写一套。
    try:
        rows = catalog.registered_root_paths()
    except Exception:                                # noqa: BLE001 - 拒绝优先
        return False
    for root_path in rows:
        try:
            root = Path(root_path).resolve()
        except OSError:
            continue
        if q != root and root not in q.parents:
            continue
        # 命中根目录后还要确认身份：current() 内部按 library_uuid 过滤，
        # 未登记根的媒体、其他库的媒体都拿不到身份。
        try:
            return catalog.current(str(q)) is not None
        except Exception:                            # noqa: BLE001
            # 身份冲突等异常按拒绝处理，不因为「路径在根下」就放行。
            return False
    return False


def inside_root(p: Path) -> bool:
    """该路径是否允许被服务：活动根内、本库其他已登记根内有媒体身份，或单图暂存目录内。

    单图查看（V2.10.7）把外部图片复制进 SOLO_DIR，这里一并放行 —— 于是
    /api/file、/api/thumb、/api/meta 等既有端点无需任何改动就能服务它。
    放行范围只有这一个目录（哈希命名、由本进程写入），不是「库外一律放行」。

    跨根读取（F1）：活动根之外，本库**其他已登记根下且有媒体身份**的媒体同样放行，
    因为跨根收藏/最近/标记本来就把这些媒体列给用户看了，读不到等于自相矛盾。
    判定委托给 _inside_registered_root —— 它要求「在已登记根下」与「有目录身份」
    同时成立，因此不会把未登记根或其他库的路径放进来。
    """
    try:
        q = p.resolve()
    except OSError:
        return False
    solo = SOLO_DIR.resolve()
    if q == solo or solo in q.parents:
        return True
    if STATE.get("root"):
        root = Path(STATE["root"]).resolve()
        if q == root or root in q.parents:
            return True
    return _inside_registered_root(q)


# 单图查看允许的 Content-Type（用于挡跨站简单请求，真实类型另按文件头判定）
SOLO_CTYPES = ("image/png", "image/jpeg", "image/jpg", "image/webp",
               "image/gif", "image/bmp", "application/octet-stream")


def solo_sniff(raw: bytes):
    """按文件头判图像类型，返回规范后缀；不认识则 None。

    刻意不信任客户端给的后缀/Content-Type：内容是任意字节、后缀却是 .png 时，
    前端 <img src="/api/file?p=..."> 会把这段字节当图片渲染，等于开了个
    「往库里塞任意文件」的口子。这里只认真正的图片魔数。
    """
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if raw[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if raw[:2] == b"BM":
        return ".bmp"
    return None


def solo_prune() -> int:
    """暂存区清理：扫掉崩溃残留的半截文件，并把总张数压到 SOLO_KEEP_MAX 内。

    按 mtime 从旧删 —— 刚放进来的那张一定是最新的，不会被自己挤掉。
    """
    try:
        files = [f for f in SOLO_DIR.iterdir() if f.is_file()]
    except OSError:
        return 0
    now = time.time()
    removed, keep = 0, []
    for f in files:
        if f.name.endswith(".part"):
            try:                          # 半截文件只可能是写盘途中退出留下的
                if now - f.stat().st_mtime > 600:
                    f.unlink()
                    removed += 1
            except OSError:
                pass
            continue
        keep.append(f)

    def _mt(f):
        try:
            return f.stat().st_mtime
        except OSError:
            return 0.0

    keep.sort(key=_mt)
    for f in keep[:max(0, len(keep) - SOLO_KEEP_MAX)]:
        try:
            f.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def clear_solo_cache() -> int:
    """清空单图暂存区，返回删除数量（设置 · 数据页的清除按钮）。"""
    n = 0
    try:
        for f in SOLO_DIR.iterdir():
            if f.is_file():
                try:
                    f.unlink()
                    n += 1
                except OSError:
                    pass
    except OSError:
        pass
    _dir_usage_invalidate(SOLO_DIR)   # F5（CMV3-LOCAL-FIX）：清完不失效 ⇒ 设置页数字不变，像没清成功
    return n


_META_FAIL = {}


_META_FAIL_LOCK = threading.Lock()


_META_FAIL_MAX = 500


def _meta_fail_mark(path, why):
    """登记一次「元数据读取失败」。第一条最接近根因，后续同路径不覆盖。"""
    try:
        k = str(path)
        with _META_FAIL_LOCK:
            if k not in _META_FAIL and len(_META_FAIL) < _META_FAIL_MAX:
                _META_FAIL[k] = str(why)
    except Exception:
        pass                       # 纯旁路：登记失败绝不能影响主流程


def _meta_fail_clear(path):
    try:
        with _META_FAIL_LOCK:
            _META_FAIL.pop(str(path), None)
    except Exception:
        pass


def meta_fail_reason(path):
    """本次进程内该路径读到过失败吗？返回原因字符串；None = 干净（即「本来就没有」）。"""
    with _META_FAIL_LOCK:
        return _META_FAIL.get(str(path))


def png_meta(path: Path) -> dict:
    """解析 PNG 的 tEXt / zTXt / iTXt chunk（ComfyUI 的 workflow / prompt 就在这里）。"""
    meta = {}
    try:
        with open(path, "rb") as f:
            # E5-7：**能打开就等于读得到** ⇒ 立刻清掉可能存在的旧登记（失败常常是瞬时的，
            # 文件被别的进程占着一会儿）。放在这里而不是函数末尾，是因为函数中段还有
            # 「不是 PNG ⇒ return」的早退路径，走那条路时不该把旧登记留下来。
            _meta_fail_clear(path)
            if f.read(8) != b"\x89PNG\r\n\x1a\n":
                return meta
            while True:
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                length, ctype = struct.unpack(">I4s", hdr)
                # §6.1：旧实现遇到 IDAT 就 break，理由是「文本块都在图像数据之前」——
                # 但 PNG 规范允许 tEXt/zTXt/iTXt 出现在 IDAT **之后**（部分工具会这么写），
                # 这些元数据此前**永远读不到**，且与「本来就没有生成参数」无法区分。
                # 现在：IDAT 只 seek 跳过（不读进内存，开销与原先的 break 同量级），
                # 一路读到 IEND 才停。
                if ctype == b"IDAT":
                    f.seek(length + 4, 1)
                    continue
                if ctype == b"IEND":
                    break
                if length <= (1 << 26):
                    data = f.read(length)
                else:
                    f.seek(length, 1)          # 超大块：跳过数据但保持解析位置
                    data = b""
                f.read(4)
                if ctype == b"tEXt" and data:
                    k, _, v = data.partition(b"\x00")
                    meta[k.decode("latin-1", "replace")] = v.decode("latin-1", "replace")
                elif ctype == b"zTXt" and len(data) > 1:
                    k, _, rest = data.partition(b"\x00")
                    try:
                        d = zlib.decompressobj()
                        raw = d.decompress(rest[1:], MAX_TEXT_BYTES)
                        if not d.eof or d.unconsumed_tail:   # 解压结果超限/被截断 → 丢弃
                            raw = b""
                        meta[k.decode("latin-1", "replace")] = raw.decode("utf-8", "replace")
                    except zlib.error:
                        pass
                elif ctype == b"iTXt" and data:
                    k, _, rest = data.partition(b"\x00")
                    comp = rest[0] if rest else 0
                    body = rest[2:] if len(rest) > 2 else b""
                    _lang, _, body = body.partition(b"\x00")
                    _tr, _, body = body.partition(b"\x00")
                    if comp == 1:
                        try:
                            d = zlib.decompressobj()
                            body = d.decompress(body, MAX_TEXT_BYTES)
                            if not d.eof or d.unconsumed_tail:
                                body = b""
                        except zlib.error:
                            pass
                    meta[k.decode("latin-1", "replace")] = body.decode("utf-8", "replace")
                if ctype == b"IHDR" and len(data) >= 8:
                    w, h = struct.unpack_from(">II", data)
                    meta["IHDR"] = f"{w} x {h}"
                if ctype == b"IEND":
                    break
    except OSError as ex:
        # §6.1：读失败此前静默 ⇒ 与「这张图本来就没有元数据」不可区分。
        # 返回值结构不动（下游按「空 = 没有」用），但至少让日志能查出是**读不到**。
        # E5-7：除日志外再登记到旁路状态 —— api_meta 会回带给前端，
        #       让界面把「读不到」与「本来没有」分开说（见 _META_FAIL 注释）。
        _meta_fail_mark(path, "%s: %s" % (type(ex).__name__, ex))
        log(f"[warn] PNG 元数据读取失败 {path.name}: {type(ex).__name__}: {ex}")
    return meta


# ------------------------------------------------- 提示词解析（人话版）

def _inp(node) -> dict:
    """节点的 inputs 字典。

    畸形 PNG 里它可能是 list / 字符串 / None，直接 .get / .items() 会抛
    AttributeError —— 而索引构建是逐文件的，一次抛出整批索引就没了。
    """
    v = node.get("inputs") if isinstance(node, dict) else None
    return v if isinstance(v, dict) else {}


def _walk_nodes(start, nodes, visit, stop_keys=None, prune=None):
    """从 start 节点沿上游连线遍历，visit(node) 按序回调。

    stop_keys 里的输入名不再向上回溯（提示词只走条件/文本链路，见 collect_texts）；
    prune(node) 为真时该节点的上游一律不再回溯（如 ConditioningZeroOut 已把条件清零）。
    """
    seen, stack = set(), [start]
    while stack:
        nid = stack.pop()
        if not nid or nid in seen or nid not in nodes:
            continue
        seen.add(nid)
        n = nodes[nid]
        if isinstance(n, dict):
            visit(n)
            if prune and prune(n):
                continue
            # D3（ε-17，CMV3-LOCAL-FIX 从旧工程 server.py 回植）：同一节点的上游
            # **按输入顺序**回溯 —— 栈本身是 LIFO，逐个 append 会让多源头时的收块顺序
            # 与工作流里的输入顺序**相反**（提示词前後颠倒）。
            # 逆序压栈 ⇒ 弹栈顺序 == 输入顺序，DFS 深度语义不变。
            #
            # 实测症状（prompt_stringfield_check B6 / C100648 / C101031）：
            #   JoinStrings(string1=触发词, string2=正文) 是同一节点的两个输入；
            #   逐个 append ⇒ 先弹 string2 ⇒ 触发词被拼到正文**后面**，
            #   而 Krea2 归档样图要求「触发词落在正向前缀 @NIJISIS,,」。
            refs = [v[0] for k, v in _inp(n).items()
                    if not (stop_keys and k in stop_keys)
                    and isinstance(v, list) and len(v) == 2 and isinstance(v[0], str)]
            stack.extend(reversed(refs))


def _strength_fmt(st):
    """强度统一转成数字（兼容 "0.25" 这类字符串），转不了返回 None。"""
    if isinstance(st, bool):
        return None
    if isinstance(st, (int, float)):
        return st
    if isinstance(st, str):
        try:
            return float(st)
        except ValueError:
            return None
    return None


def _ct_key(n: dict) -> str:
    """节点类名归一化：去掉空格/下划线/括号等。

    这样 "Checkpoint Loader with Name (Image Saver)" 也能匹配 "checkpointloader"。
    """
    return re.sub(r"[^a-z0-9]", "", str(n.get("class_type", "")).lower())


def _entry_active(e: dict) -> bool:
    """多槽位加载器条目的启用开关；无开关字段视为启用。"""
    for k in ("active", "on", "enabled", "enable"):
        v = e.get(k)
        if isinstance(v, bool):
            return v
    return True


# 提示词里的 LoRA 引用语法：<lora:名字:强度[:CLIP强度]>（LoraManager / A1111 / tag loader 通用）
_LORA_TAG_RE = re.compile(r"<\s*l(?:ora|yco|ocon)\s*:[^<>]*>", re.I)

# 多块提示词节点（ComfyUI-Prompt-Composer 等）把提示词拆在 text_1 … text_10 上，
# 没有单数的 text 字段。见 collect_texts.visit() 里的收块分支。
_MULTI_TEXT_RE = re.compile(r"text_(\d+)$")


def _split_lora_tags(text: str):
    """拆出提示词里的 <lora:...> 标签，返回 (去掉标签的文本, 标签列表)。"""
    tags = _LORA_TAG_RE.findall(text)
    if not tags:
        return text, []
    return _LORA_TAG_RE.sub(" ", text).strip(), tags


def _lora_tag_entry(tag: str):
    """<lora:名字:模型强度[:CLIP强度]> → "名字 (强度)"；解析不出返回 None。"""
    body = tag[tag.index(":") + 1:-1].strip()
    parts = [p.strip() for p in body.split(":") if p.strip()]
    if not parts:
        return None
    st = _strength_fmt(parts[1]) if len(parts) > 1 else None
    return parts[0] + (f" ({st:g})" if st is not None else "")


def _is_lora_slot_node(ct_key: str, inputs: dict) -> bool:
    """带 LoRA 槽位的加载/堆叠节点：其 text 槽位存的是 <lora:...> 语法，不是提示词。

    例：LoraManager 的 Lora Loader / Lora Stacker——前端把加过的 LoRA 都写进 text，
    关掉槽位后标签仍留在文本里，早期版本会把它们当成正向提示词读出来。
    """
    if "trigger" in ct_key or not ("lora" in ct_key or "lyco" in ct_key):
        return False
    return any(k in inputs for k in ("loras", "lora_stack", "lora_name", "lora_name_1"))


def _wf_kind(obj: dict, ks: dict) -> str:
    """工作流类型：t2i=文生图 / i2i=图生图 / cn=ControlNet / ""=认不出。

    判定顺序（互斥，取优先级最高者）：
      1. 图里出现任何 ControlNet 节点 → cn
      2. 采样器的 latent_image 上游是 EmptyLatent* → t2i；是 VAEEncode* → i2i
      3. 兜底：整图里有 LoadImage 且没有 EmptyLatent → i2i
    """
    for n in obj.values():
        if isinstance(n, dict) and "controlnet" in _ct_key(n):
            return "cn"
    lat = _inp(ks).get("latent_image")
    kind = [""]

    def visit(n):
        if kind[0]:
            return
        ct = _ct_key(n)
        if "empty" in ct and "latent" in ct:          # EmptyLatentImage / EmptySD3LatentImage…
            kind[0] = "t2i"
        elif "vaeencode" in ct:                       # VAEEncode / VAEEncodeForInpaint
            kind[0] = "i2i"

    if isinstance(lat, list) and lat and isinstance(lat[0], str):
        _walk_nodes(lat[0], obj, visit, stop_keys=("model", "clip"))
    if kind[0]:
        return kind[0]
    has_img = any(isinstance(n, dict) and "loadimage" in _ct_key(n) for n in obj.values())
    return "i2i" if has_img else ""


def _parse_prompt_loose(obj: dict) -> dict:
    """最后一道兜底：不认节点名，只按**输入字段名**收集。

    只用于「连 positive/negative 宿主节点都找不到」的工作流 —— 典型是厂商封装节点：
    MiniMaxH3 的 Easy 系列把提示词放在业务节点的 `prompt` 字段里，采样参数散在
    seed / steps / sampler_name 上，既没有 KSampler 也没有 guider，按节点名追是追不完的。

    但 `prompt` / `steps` / `sampler_name` 这些**语义字段名是跨厂商稳定的**，
    所以按字段名捞一遍。刻意只在这个分支用：它原本的结果是「一条信息都没有」，
    因此任何正确提取都是净收益，不可能覆盖更精确的路径。
    """
    s = {}
    if not isinstance(obj, dict):
        return s
    best_prompt = ""
    seed = steps = cfg = None
    sampler = None
    ckpt, loras = [], []
    for n in obj.values():
        if not isinstance(n, dict):
            continue
        i = _inp(n)
        for k in ("prompt", "text"):
            v = i.get(k)
            # 只看够长的字符串：`prompt_optimizer_settings=False` 这类同名字段会被滤掉
            if isinstance(v, str) and len(v.strip()) >= 8:
                txt, tags = _split_lora_tags(v.strip())
                for t in tags:
                    e = _lora_tag_entry(t)
                    if e and e not in loras:
                        loras.append(e)
                if txt and len(txt) > len(best_prompt):
                    best_prompt = txt        # 取最长的一份：正片，而不是被拆开的片段
        if seed is None:
            for k in ("seed", "noise_seed"):
                if isinstance(i.get(k), int) and not isinstance(i.get(k), bool):
                    seed = i[k]
                    break
        if steps is None and isinstance(i.get("steps"), int):
            steps = i["steps"]
        if cfg is None and isinstance(i.get("cfg"), (int, float)) \
                and not isinstance(i.get("cfg"), bool):
            cfg = i["cfg"]
        if sampler is None and isinstance(i.get("sampler_name"), str):
            sampler = i["sampler_name"].strip() or None
        for k in ("ckpt_name", "unet_name", "fl2va_model", "ref2va_model"):
            v = i.get(k)
            # 「无」是 MiniMaxH3 这类节点用来表示「没选」的占位值，不是模型名
            if isinstance(v, str) and v.strip() and v.strip() != "无" and v.strip() not in ckpt:
                ckpt.append(v.strip())
        lv = i.get("lora_name")
        if isinstance(lv, str) and lv.strip():
            st = _strength_fmt(i.get("strength_model"))
            e = lv.strip() + (f" ({st:g})" if st is not None else "")
            if e not in loras:
                loras.append(e)
    if best_prompt:
        s["positive"] = best_prompt
    if seed is not None:
        s["seed"] = seed
    if steps is not None:
        s["steps"] = steps
    if cfg is not None:
        s["cfg"] = cfg
    if sampler:
        s["sampler"] = sampler
    if ckpt:
        s["model"] = ckpt[0]
    if loras:
        s["loras"] = loras
    if s:
        s["source"] = "ComfyUI"
    return s


def parse_comfy_prompt(obj: dict) -> dict:
    """ComfyUI API 格式 prompt（节点字典）→ 人类可读摘要。"""
    s = {}
    if not isinstance(obj, dict):        # 畸形 PNG：prompt 解出来可能压根不是节点字典
        return s
    ks = None
    # 采样节点按优先级分三轮找：标准 KSampler 系 > SamplerCustom 系 > 带 sampler_name 的兜底。
    # 必须分级而不是合在一轮：一张图可能有多个采样阶段（先生成、再高清修复），
    # 合在一起会变成「谁先出现谁当选」，把原本稳定的结果换掉。
    for n in obj.values():
        if isinstance(n, dict) and n.get("class_type") in ("KSampler", "KSamplerAdvanced"):
            ks = n
            break
    if ks is None:      # Flux / MiniMaxH3 等新式工作流：SamplerCustom / SamplerCustomAdvanced
        for n in obj.values():
            if isinstance(n, dict) and n.get("class_type") in ("SamplerCustom", "SamplerCustomAdvanced"):
                ks = n
                break
    if ks is None:  # 兜底：找任何带 sampler_name 的节点
        for n in obj.values():
            if not isinstance(n, dict):
                continue
            ct = _ct_key(n)
            # KSamplerSelect / SamplerSelector 只是「采样器选择器」，本身不采样。
            # 选中它会让模型 / 提示词 / 步数 / 种子全部读不到
            #（SamplerCustomAdvanced 工作流的典型症状：只剩「采样器」一行）。
            if "select" in ct or "selector" in ct:
                continue
            if "sampler_name" in _inp(n):
                ks = n
                break
    if ks is None:
        # 第四轮兜底：新式工作流（LTX 的 LTXVLoopingSampler、MiniMaxH3 的封装渲染节点等）
        # 常常既没有 KSampler*、也没有 sampler_name 输入 —— 按名字去追这类节点是追不完的。
        # 但「谁持有 positive/negative」这件事很稳定：KSampler / SamplerCustom / CFGGuider
        # 都长这样。所以退一步按**结构**识别，把提示词和模型挖出来，采样参数拿不到就留空
        # —— 总比整条工作流一条信息都不显示要好。
        # ⚠️ 后面的逻辑对 ks 没有硬依赖（都是「有则用、没有就跳过」），所以放宽是安全的；
        #    改前已用真实视频存了基线快照，改后逐条对比确认无回归（见 _test/snapshot_parse.py）。
        for n in obj.values():
            if not isinstance(n, dict):
                continue
            i2 = _inp(n)
            if i2.get("positive") is not None and i2.get("negative") is not None:
                ks = n
                break
    if ks is None:
        # 第五轮：连正负条件的宿主节点都找不到 —— 典型的厂商封装节点。
        # 例：MiniMaxH3 的 Easy 系列把提示词塞在业务节点的 `prompt` 字段，
        # 采样参数散在 seed / steps / sampler_name 上，既没有 KSampler 也没有 guider。
        # 这时按**输入字段名**通配收集一次。
        # 它是通配，但只在这个「原本一条信息都拿不到」的分支里生效，
        # 不会覆盖上面那些更精确的路径 —— 有得拿总比一片空白好。
        return _parse_prompt_loose(obj)

    def resolve_str(v, key="", depth=0):
        """名称类输入（sampler_name/scheduler）可能是连线引用，沿连线取真实字符串。

        例：ClownsharKSampler_Beta.sampler_name → ClownSamplerSelector_Beta，
        中间还可能隔着 rgthree「To String」等中转节点。
        """
        if isinstance(v, str):
            return v.strip() or None
        if depth > 6 or not (isinstance(v, list) and v and isinstance(v[0], str)):
            return None
        up = obj.get(v[0])
        if not isinstance(up, dict):
            return None
        uinp = _inp(up)
        for k in (key, "value", "string", "text", "sampler_name", "scheduler"):
            if k and k in uinp and uinp[k] is not None:
                r = resolve_str(uinp[k], key, depth + 1)
                if r:
                    return r
        for _k, val in uinp.items():      # 兜底：跟随首个连线引用（中转节点）
            if isinstance(val, list) and val and isinstance(val[0], str):
                r = resolve_str(val, key, depth + 1)
                if r:
                    return r
        return None

    inp = _inp(ks)

    def from_ref(node_ref, key):
        """取「引用节点」身上的某个字段（只下一层，不做连线兜底遍历）。"""
        if not (isinstance(node_ref, list) and node_ref and isinstance(node_ref[0], str)):
            return None
        up = obj.get(node_ref[0])
        return _inp(up).get(key) if isinstance(up, dict) else None

    seed = next((inp[k] for k in ("seed", "noise_seed", "seed_value")
                 if inp.get(k) is not None), None)
    if seed is None:                    # SamplerCustom*：种子在 noise 节点（RandomNoise.noise_seed）
        seed = from_ref(inp.get("noise"), "noise_seed")

    def resolve_num(v, keys, depth=0):
        """数值输入（seed/steps/cfg）可能是连线引用，沿连线取真实数值。"""
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            try:
                return int(v) if float(v).is_integer() else v
            except (OverflowError, ValueError):
                return v if isinstance(v, int) else None
        if depth > 6 or not (isinstance(v, list) and v and isinstance(v[0], str)):
            return None
        up = obj.get(v[0])
        if not isinstance(up, dict):
            return None
        uinp = _inp(up)
        for k in keys:
            if k in uinp and uinp[k] is not None:
                return resolve_num(uinp[k], keys, depth + 1)
        return None

    for k_src, k_dst, num_keys in (
            ("sampler_name", "sampler", None), ("scheduler", "scheduler", None),
            ("steps", "steps", ("steps", "steps_total", "value")),
            ("cfg", "cfg", ("cfg", "value"))):
        v = inp.get(k_src)
        if v is None:
            continue
        if num_keys:
            v = resolve_num(v, num_keys)
        elif not isinstance(v, str):
            v = resolve_str(v, k_src)   # 名称可能是连线引用，沿连线取真实字符串
        if v is not None and not isinstance(v, (list, dict)):
            s[k_dst] = v
    if not s.get("sampler"):
        # SamplerCustom*：采样器名字挂在 sampler 引用的节点上（KSamplerSelect.sampler_name）
        _sn = from_ref(inp.get("sampler"), "sampler_name")
        if isinstance(_sn, str) and _sn.strip():
            s["sampler"] = _sn.strip()
    if seed is not None:
        seed = resolve_num(seed, ("seed", "value", "noise_seed"))
    if seed is not None:
        s["seed"] = seed

    def ref(k):
        v = inp.get(k)
        return v[0] if isinstance(v, list) and v and isinstance(v[0], str) else None

    tag_loras = []                       # 提示词里 <lora:...> 标签解析出的 LoRA（兜底用）

    def _neg_input_keys(n):
        """节点上「负向专属」输入名（negative_prompt / negative / neg_prompt …）。

        双输出编码节点（如 TextEncodeQwenImage21：单节点同时输出正向/负向两路条件，
        KSampler.positive=[id,0]、negative=[id,1] 指向同一节点）靠这些字段区分两侧。

        L2（2026-09-27，CMV3-LOCAL-FIX 从旧工程 server.py 回植）：
        用户实测（QW21-20260922145354_00001_.png，Qwen-Image 2.1 工作流）——
        KSampler.positive=["522",0]、negative=["522",1] 指向同一个
        TextEncodeQwenImage21 节点，该节点 prompt=["517",0] 是连线（正向正文在上游）、
        negative_prompt="" 是空字面量。忽略输出槽位时负向侧也从本节点的 prompt 连线
        一路爬到 517 ⇒ **负向 = 正向的复制**。
        """
        return [k for k in _inp(n)
                if k.startswith("negative") or k == "neg_prompt"]

    def cond_entry(side):
        """该侧条件链路的起点节点 id。

        标准 KSampler：inputs.positive / inputs.negative；
        SamplerCustom*（Flux / MiniMaxH3 等新式工作流）：没有 positive/negative，
        条件挂在 inputs.guider 上：
          · CFGGuider —— 自己的 positive / negative（双侧）
          · BasicGuider —— 只有 conditioning（**单侧**，故 negative 返回 None，不虚构负向提示词）
        """
        v = inp.get(side)
        if isinstance(v, list) and v and isinstance(v[0], str):
            return v[0]
        g = inp.get("guider")
        if not (isinstance(g, list) and g and isinstance(g[0], str)):
            return None
        gn = obj.get(g[0])
        if not isinstance(gn, dict):
            return None
        gi = _inp(gn)
        gv = gi.get(side)
        if isinstance(gv, list) and gv and isinstance(gv[0], str):
            return gv[0]
        if side == "positive":
            c = gi.get("conditioning")
            if isinstance(c, list) and c and isinstance(c[0], str):
                return c[0]
        return None

    def collect_texts(start_key):
        side = start_key                 # "positive" / "negative"
        other = "negative" if side == "positive" else "positive"
        keys = ("text", "prompt", "t5xxl", "clip_l", "clip_g", side)   # 兼容 Flux/SD3 双编码、
                                                                       # Qwen-Image-Edit / Flux2-Klein-Edit
                                                                       # 等编辑类 TextEncode（字段名是 prompt）、提示词 UI 节点
        direct = inp.get(start_key)
        if isinstance(direct, str) and direct.strip():
            txt, tags = _split_lora_tags(direct.strip())
            tag_loras.extend(tags)
            return [txt] if txt else []
        texts = []
        # 显示类节点去重模式：False = 跳过缓存副本只收上游（首轮）；
        # 整条链路收不到文本时置 True 重走一遍（旧行为兜底，保证 0 丢失）。
        cache_ok = False
        def _links_ahead(n):
            # 该节点是否还有「会被继续回溯」的连线输入（与 _walk_nodes 同一套 stop_keys）
            for k, v in _inp(n).items():
                if k in ("model", "clip", other):
                    continue
                if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
                    return True
            return False
        def visit(n):
            ct = _ct_key(n)
            i2 = _inp(n)
            if _is_lora_slot_node(ct, i2):
                return                   # 槽位节点的 text 是 LoRA 语法，不是提示词
            # 显示类节点（easy showAnything / ShowText|pysssss 等）的字面 text /
            # text_N 是运行时写回的缓存副本，上游链路里还有同一份原文。
            # 有可回溯上游时跳过缓存改收上游，避免同一段提示词被收两遍
            # （2026-09-19 实测：Krea2 工作流 easy showAnything 缓存 A+B，
            #   上游 CR Text×2 又是 A、B，旧逻辑正向变成 A+B+A+B）。
            if not cache_ok and ("show" in ct or "display" in ct) and _links_ahead(n):
                return
            cand = keys
            if "string" in ct or "primitive" in ct:
                # PrimitiveStringMultiline 用 value；KJNodes StringConstant /
                # StringConstantMultiline 用 string（INPUT_TYPES 里的 required 键就叫 string）。
                # 2026-10-06 实测：漏了 string 会让 Krea2 + TE_MAN 工作流读不到正文 ——
                # 首轮收不到文本 ⇒ 回退收「显示节点缓存」⇒ 界面显示上一张图的提示词。
                # （CMV3-LOCAL-FIX，从旧工程 server.py 回植）
                cand = keys + ("value", "string")
            if "triggerword" in ct:
                # LoraManager TriggerWord Toggle：LoRA 触发词放在 orinalMessage
                # （字段名就是少一个 g 的拼写，值与其前端 active 词表一致）。
                # 该节点常经 JoinStrings 拼在正文之前，属正向提示词的一部分。
                # 2026-10-06 全库 9683 张实测：此字段唯 TriggerWord Toggle 一类节点持有
                # （1042 处，且多数未接进提示词链路）⇒ 加它不误伤其它工作流。
                # （CMV3-LOCAL-FIX，从旧工程 server.py 回植）
                cand = cand + ("orinalMessage",)
            hit = False
            for k in cand:
                t = i2.get(k)
                if isinstance(t, str) and t.strip():
                    txt, tags = _split_lora_tags(t.strip())
                    tag_loras.extend(tags)
                    if txt:
                        texts.append(txt)
                    hit = True
                    break
            if hit:
                return
            # 多块提示词节点（ComfyUI-Prompt-Composer 的 promptComposerTextMultiple 等）：
            # 提示词被拆在 text_1 … text_N 上，没有单数的 text / prompt 字段，按 N 升序全收。
            # 放在 keys 之后是刻意的 —— 命中标准字段就保持原行为，不被这条分支抢走。
            # ⚠️ 这是「通配」：显示类节点（ShowText|pysssss 等）也带 text_N，其值是上游文本的
            # 缓存，当前一并读出（实测全库 9038 张只影响 7 张，丢失 0 / 改写 0）；
            # 若日后要收紧语义，把这行换成 class_type 白名单判断即可。
            blocks = []
            for k2, t2 in i2.items():
                m2 = _MULTI_TEXT_RE.fullmatch(k2)
                if m2 and isinstance(t2, str) and t2.strip():
                    blocks.append((int(m2.group(1)), t2.strip()))
            for _n, t2 in sorted(blocks):
                txt, tags = _split_lora_tags(t2)
                tag_loras.extend(tags)
                if txt:
                    texts.append(txt)
        # —— L2（2026-09-27，CMV3-LOCAL-FIX 从旧工程 server.py 回植）：双输出编码节点的
        #    侧别隔离 ——
        # 规则：负向侧若入口节点带 neg* 专属字段，则【只认这些字段】——
        #   字面量直接用（空就是空，CFG=1 的负向本就常空）；连线只沿该连线回溯；
        #   绝不顺 prompt/text 等通用字段爬上游。正向侧对称保护：不许回溯进 neg* 连线。
        ent_id = cond_entry(side)
        ent = obj.get(ent_id) if isinstance(ent_id, str) else None
        negk = _neg_input_keys(ent) if isinstance(ent, dict) else []
        if side == "negative" and negk:
            for k in negk:
                t = _inp(ent).get(k)
                if isinstance(t, str):
                    if t.strip():
                        txt, tags = _split_lora_tags(t.strip())
                        tag_loras.extend(tags)
                        if txt:
                            texts.append(txt)
                    continue            # 空字面量 ⇒ 该字段就是空，不虚构、不兜底
                if isinstance(t, list) and len(t) == 2 and isinstance(t[0], str):
                    def _wneg(cache, _ref=t):
                        nonlocal cache_ok
                        cache_ok = cache
                        _walk_nodes(_ref[0], obj, visit,
                                    stop_keys=("model", "clip", other),
                                    prune=lambda n: "zeroout" in _ct_key(n))
                    _wneg(False)
                    if not texts:
                        _wneg(True)     # 与主路径同款：首轮空则回退收缓存（防 0 丢失变丢失）
                    break
            return texts                # neg* 字段全空 ⇒ 负向为空（比旧逻辑的「抄正向」正确）
        # 正向侧：入口节点带 neg* 字段时，禁止回溯进这些连线（负向文本不得混入正向）
        stop = ("model", "clip", other)
        if side == "positive" and negk:
            stop = stop + tuple(negk)

        # 只沿条件/文本连线回溯：不钻进 clip/model 加载器管线，也不跨到另一侧
        # （否则 LoRA 加载器槽位、对面提示词都会被当成本侧提示词）；
        # ConditioningZeroOut 把条件清零，该侧就没有提示词可读
        def _walk(cache):
            nonlocal cache_ok
            cache_ok = cache
            _walk_nodes(cond_entry(side), obj, visit,
                        stop_keys=stop,
                        prune=lambda n: "zeroout" in _ct_key(n))
        _walk(False)
        if not texts:
            _walk(True)                  # 首轮只收上游没收到 → 回退旧行为收缓存
        return texts

    pos, neg = collect_texts("positive"), collect_texts("negative")
    if pos:
        s["positive"] = "\n".join(pos)
    if neg:
        s["negative"] = "\n".join(neg)

    model_ref = next((ref(k) for k in ("model", "modelname", "ckpt_name", "unet_name")
                      if ref(k)), None)
    if model_ref is None:
        # SamplerCustom*：模型挂在 guider 上（BasicGuider.model / CFGGuider.model）
        _m = from_ref(inp.get("guider"), "model")
        if isinstance(_m, list) and _m and isinstance(_m[0], str):
            model_ref = _m[0]
    if model_ref:
        loras, ckpt = [], []
        def visit_m(n):
            ct = _ct_key(n)
            i2 = _inp(n)
            if "lora" in ct and "trigger" not in ct:
                got = False
                lv = i2.get("loras")
                if isinstance(lv, dict):                      # LoraManager: {"__value__": [...]}
                    lv = lv.get("__value__")
                if isinstance(lv, list):
                    for e in lv:
                        if isinstance(e, dict) and isinstance(e.get("name"), str):
                            if not _entry_active(e):          # 跳过未启用的槽位
                                continue
                            st = _strength_fmt(e.get("strength", e.get("strengthModel")))
                            loras.append(e["name"] + (f" ({st:g})" if st is not None else ""))
                            got = True
                if not got:
                    for k, v in i2.items():                   # rgthree Power Lora Loader: lora_1..lora_N
                        if (re.fullmatch(r"lora(_\d+)?", k) and isinstance(v, dict)
                                and isinstance(v.get("lora"), str)):
                            if not _entry_active(v):
                                continue
                            st = _strength_fmt(v.get("strength"))
                            loras.append(v["lora"] + (f" ({st:g})" if st is not None else ""))
                            got = True
                if not got:
                    for k, v in i2.items():                   # 常规格式: lora_name / lora_name_2
                        if re.fullmatch(r"lora_name(_\d+)?", k) and isinstance(v, str):
                            st = i2.get("strength_model" if k == "lora_name" else
                                        "strength_model" + (k[len("lora_name"):]))
                            st = _strength_fmt(st)
                            loras.append(v + (f" ({st:g})" if st is not None else ""))
            elif "checkpointloader" in ct and isinstance(i2.get("ckpt_name"), str):
                ckpt.append(i2["ckpt_name"])
            elif "unetloader" in ct and isinstance(i2.get("unet_name"), str):
                ckpt.append(i2["unet_name"])
        _walk_nodes(model_ref, obj, visit_m)
        if ckpt:
            s["model"] = ckpt[0]
        if loras:
            s["loras"] = loras

    if not s.get("loras") and tag_loras:
        # 加载器没给出 LoRA（提示词里直接写 <lora:...> 的 tag loader 工作流）：
        # 把标签还原成 LoRA 列表，信息不丢，但提示词里不再出现标签
        names = []
        for _tag in tag_loras:
            _v = _lora_tag_entry(_tag)
            if _v and _v not in names:
                names.append(_v)
        if names:
            s["loras"] = names

    if ref("latent_image"):
        sizes = []
        def visit_l(n):
            ctl = _ct_key(n)
            if "empty" in ctl and "latent" in ctl:   # EmptyLatent / EmptySD3Latent 等
                i2 = _inp(n)
                if isinstance(i2.get("width"), int) and isinstance(i2.get("height"), int):
                    sizes.append(f"{i2['width']} x {i2['height']}")
        _walk_nodes(ref("latent_image"), obj, visit_l)
        if sizes:
            s["size"] = sizes[0]
    if not s.get("size"):                        # 兜底：节点直接给了宽高（可能是连线引用）
        _w = resolve_num(inp.get("width"), ("width", "value"))
        _h = resolve_num(inp.get("height"), ("height", "value"))
        if isinstance(_w, int) and isinstance(_h, int):
            s["size"] = f"{_w} x {_h}"
    _k = _wf_kind(obj, ks)                       # 工作流类型（文生图/图生图/ControlNet）
    if _k:
        s["wfk"] = _k
    return s


def parse_a1111_parameters(text: str) -> dict:
    """A1111/Forge 的 parameters 纯文本 → 人类可读摘要。"""
    s = {}
    m = re.search(r"^Steps:\s*\d+", text, re.M)   # 行首匹配：正提示词为空时也能识别
    if not m:
        if text.strip():
            s["positive"] = text.strip()
        return s
    head, tail = text[:m.start()], text[m.start():]
    # 7-5：新键必须「大写字母开头」——A1111 全部已知参数键（Steps/CFG scale/Model hash/
    #      Schedule type/Lora hashes…）都符合；而值内部的「xx: yy」片段（Lora hashes 值里的
    #      "a: 0c3b, b: 1a2f"、Hires prompt 里的英文子句）几乎全是小写开头。
    #      旧正则 [A-Za-z] 大小写通吃 ⇒ 断点落进值内部，前一个键只剩半截、还多出垃圾键。
    for part in re.split(r",\s*(?=[A-Z][A-Za-z0-9 _]*:)", tail):
        k, _, v = part.partition(":")
        k, v = k.strip(), v.strip()
        if k and v:
            key = {"Steps": "steps", "CFG scale": "cfg", "Sampler": "sampler",
                   "Seed": "seed", "Size": "size", "Model": "model",
                   "Model hash": "model_hash", "Schedule type": "scheduler"}.get(k, k)
            s[key] = v
    nm = re.match(r"(?s)(.*?)\nNegative prompt:\s?(.*)", head)
    if nm:
        if nm.group(1).strip():
            s["positive"] = nm.group(1).strip()
        if nm.group(2).strip():
            s["negative"] = nm.group(2).strip()
    elif head.strip():
        s["positive"] = head.strip()
    return s


# ---- A1111 → ComfyUI 名称/类型适配（模板合成用）----

_A1111_SAMPLERS = {
    "euler": "euler", "euler a": "euler_ancestral", "heun": "heun",
    "dpm2": "dpm2", "dpm2 a": "dpm2_ancestral", "lms": "lms",
    "dpm fast": "dpm_fast", "dpm adaptive": "dpm_adaptive",
    "dpm++ 2m": "dpmpp_2m", "dpm++ 2m sde": "dpmpp_2m_sde",
    "dpm++ sde": "dpmpp_sde", "dpm++ 3m sde": "dpmpp_3m_sde",
    "ddim": "ddim", "plms": "plms", "uni_pc": "uni_pc",
    "lcm": "lcm", "restart": "restart",
}
_A1111_SCHEDULERS = {
    "automatic": "normal", "uniform": "normal", "normal": "normal",
    "karras": "karras", "exponential": "exponential",
    "polyexponential": "polyexponential", "sgm uniform": "sgm_uniform",
    "ddim uniform": "ddim_uniform", "beta": "beta",
}


def _a1111_sampler(name):
    """A1111 采样器名 → (ComfyUI sampler 名, 名称里隐含的 scheduler 或 None)。

    ComfyUI 原生名字（如 euler_ancestral）会原样透传。
    """
    n = (name or "").strip().lower()
    sch = None
    for suf, s in ((" karras", "karras"), (" exponential", "exponential"),
                   (" sgm uniform", "sgm_uniform"), (" uniform", "normal")):
        if n.endswith(suf):
            n, sch = n[:-len(suf)].strip(), s
            break
    return _A1111_SAMPLERS.get(n, n.replace("++", "pp").replace(" ", "_")), sch


def _num(v, as_int=False):
    """宽松数值转换（A1111 参数是字符串），失败返回 None。

    P0-14（CMV3-LOCAL-FIX，从旧工程回植）：整数目标**先走 int()**，不走 float() 中转
    —— float() 是 53 位有效位，对 > 2^53 的整数会静默改写（实测 2^64-1 → …616）。
    seed 一旦被改写，合成出来的工作流就**永远无法复现**，而界面上看不出任何异常。
    ComfyUI 的 seed 是 JSON 整数、A1111 的是十进制字符串，两条来源都会经过本函数，
    所以两条都会中招。
    """
    if as_int:
        try:
            return int(v)
        except (TypeError, ValueError, OverflowError):
            pass          # 形如 "12.7" / 12.7：退回「截断取整」的老口径
    try:
        f = float(v)
        return int(f) if as_int else f
    except (TypeError, ValueError, OverflowError):
        return None


def parse_meta(meta: dict):
    """从 png_meta 结果提取人类可读摘要；失败返回 None。"""
    try:
        if isinstance(meta.get("prompt"), str):
            obj = json.loads(meta["prompt"])
            if isinstance(obj, dict) and obj:
                s = parse_comfy_prompt(obj)
                if s:
                    s["source"] = "ComfyUI"
                    return s
    except Exception:      # 畸形 PNG 的 prompt 可能是任意结构：解析不了就当没有生成参数
        pass
    if isinstance(meta.get("parameters"), str):
        s = parse_a1111_parameters(meta["parameters"])
        if s:
            s["source"] = "A1111/Forge"
            return s
    return None


def parse_jpeg_meta(path: Path) -> dict:
    """从 PIL 可读的位图（JPEG/WebP/GIF/BMP）提取尺寸与生成参数。

    A1111/Forge 的参数存于 EXIF UserComment(0x9286)。
    """
    if not HAS_PIL:
        return {}
    try:
        with Image.open(path) as im:
            meta = {"IHDR": f"{im.width} x {im.height}"}
            try:
                exif = im.getexif()
            except Exception:
                return meta
            v = exif.get(0x9286)
            if isinstance(v, bytes):
                text = v[8:].decode("utf-8", "replace")  # 跳过 8 字节编码头
            elif isinstance(v, str):
                text = v
            else:
                return meta
            text = text.strip()
            if "Steps:" in text or "Negative prompt:" in text:
                meta["parameters"] = text
            return meta
    except Exception:
        return {}


def send_to_comfy(prompt: dict, base=None) -> dict:
    """把 API 格式 prompt POST 到本地 ComfyUI 的 /prompt 重新生成。"""
    base = (base or COMFY_BASE).rstrip("/")
    try:
        data = json.dumps({"prompt": prompt}).encode("utf-8")
        req = urllib.request.Request(base + "/prompt", data=data, headers={
            "Content-Type": "application/json"})
        with _OPENER_NO_PROXY.open(req, timeout=20) as r:
            j = json.loads(r.read().decode("utf-8"))
        if j.get("prompt_id"):
            return {"ok": True, "prompt_id": j["prompt_id"]}
        return {"ok": False, "error": "ComfyUI 未返回 prompt_id: " + str(j)[:200]}
    except urllib.error.HTTPError as ex:
        try:
            detail = json.loads(ex.read().decode("utf-8", "replace"))
        except Exception:
            detail = {}
        err = detail.get("error") or str(detail)[:200] or str(ex)
        return {"ok": False, "error": f"ComfyUI 拒绝提交: {err}"}
    except Exception as ex:
        return {"ok": False,
                "error": f"无法连接 ComfyUI({base}): {type(ex).__name__}: {ex}"}


# ------------------------------------------------- 工作流模板合成（导入 UI 摆好参数，不自动跑）

_COMFY_ROOT_CACHE = [None]     # 全盘探测缓存（None=未探测，False=探测失败）


def comfy_root():
    """定位 ComfyUI 安装目录：COMFYUI_ROOT 环境变量 → 当前根目录(…/output)推导 → 全盘探测。"""
    env = os.environ.get("COMFYUI_ROOT")
    if env:
        p = Path(env)
        if p.is_dir():
            return p
    root = STATE.get("root")
    if root:
        pr = Path(root)
        if pr.name.lower() == "output":
            return pr.parent
    if _COMFY_ROOT_CACHE[0] is None:
        d = _detect_comfyui_output()
        _COMFY_ROOT_CACHE[0] = Path(d).parent if d else False
    return _COMFY_ROOT_CACHE[0] or None


# ---- 视频生成参数读取（V2.13.2）------------------------------------------
# 目标：像读 PNG 元数据那样，把视频里 ComfyUI 写的提示词读出来。
#
# 位置（由 strip_mp4_meta 的注释与实测共同确认）：
#   mp4/mov/m4v → moov/udta/meta/ilst
#   webm/mkv    → EBML 的 Segment/Tags/Tag/SimpleTag
#
# 设计要点：**只负责把原始文本取出来**，解析成人类可读参数交给已有的 parse_meta()。
# 也就是说视频产出一份和 png_meta 同构的 {"prompt": "<json>"}，解析层一行都不用改 ——
# 共用一条路，才不会长出第二套口径（两套口径迟早会出现「图片能看视频不能看」这种不一致）。

# 头 / 尾各读这么多字节去找元数据。**刻意不整体读文件**：实测用户的 AI 视频有 472 MB 的，
# 为几个参数把几百 MB 读进内存既慢又危险；而元数据只可能在两头 ——
# mp4 的 moov 在不 faststart 时位于**尾部**（ffmpeg 默认），开了 faststart 才在头部；
# webm 的 Tags 在 Segment 前部（muxer 先写头再写簇）。两头都探，不用猜。
VIDEO_PROBE_BYTES = 16 << 20

# EBML 的 Tags 元素 ID（含 marker bit，固定 4 字节）。
# 尾部探测靠搜它来定位 —— EBML 是顺序容器、没有全局索引，没法像 mp4 那样按偏移找。
_TAGS_ID = b"\x12\x54\xc3\x67"
_VIDEO_META_KIND = {".mp4": "mp4", ".m4v": "mp4", ".mov": "mp4",
                    ".webm": "webm", ".mkv": "webm"}


def _mp4_meta_base(data: bytes, start: int, end: int):
    """算出 meta 载荷的起点，返回偏移；两种风格都不成立时返回 None。

    ⚠️ `meta` 带不带 version+flags 是 mp4 解析的经典分歧：ISO 说带（它是 FullBox），
    QuickTime 写出来的却不带。判据用「从这个起点能不能解析出子 box」，两种都试 ——
    只按一种来，换一个来源的视频就读不出东西。

    ⚠️ 另外注意：**keys 表和 ilst 是兄弟**，都在 meta 这一层。必须回到这一层去找它们；
    在 ilst 内部找 keys 永远找不到（自检时就这么错了一轮，现象是「keys 表是空的」）。
    """
    for base in (start + 4, start):
        if base >= end:
            continue
        try:
            if list(_mp4_boxes(data, base, end)):
                return base
        except Exception:
            continue
    return None


def _mp4_key_table(data: bytes, start: int, end: int) -> dict:
    """读 mdta 风格 meta 的 keys 表，返回 {1-based 索引: 键名}。

    🔴 这是 mp4 元数据最容易踩的一个坑，必须写清楚：
    `meta` box 的 handler 有两种，ilst 条目名的含义**完全不同** ——
      · handler = `mdta`（ffmpeg / iTunes 风格）→ 条目名是 **keys 表里的 1-based 索引**
      · handler = `mdir`（QuickTime 风格）→ 条目名才是 `©nam` 这种四字符码
    把索引当字符串解码出来会得到一串控制字符，现象是「ffprobe 明明读得到、
    我们的程序读不出来」—— 实测就是这么被绊住的（样本由 ffmpeg 写入，走的正是 mdta）。
    """
    out = {}
    try:
        for off, head, btype, ps, pe in _mp4_boxes(data, start, end):
            if btype != b"keys" or pe - ps < 8:
                continue
            count = int.from_bytes(data[ps + 4:ps + 8], "big")   # 前 4 字节是 version+flags
            p = ps + 8
            for i in range(1, count + 1):
                if p + 4 > pe:
                    break
                ksize = int.from_bytes(data[p:p + 4], "big")
                if ksize < 8 or p + ksize > pe:
                    break
                # 键名 = 命名空间(4 字节，如 "mdta") + 实际名字
                name = data[p + 8:p + ksize].decode("utf-8", "replace").strip()
                if name:
                    out[i] = name
                p += ksize
    except Exception:
        return out
    return out


def _mp4_ilst_value(data: bytes, start: int, end: int) -> str:
    """取一个 ilst 条目的文本值。

    条目下挂 data box：前 4 字节是类型（1 = UTF-8 文本）+ 4 字节 locale，之后才是内容。
    `----` 形式（mean / name / data）同样从 data 取，所以这里只认 data 就够。
    """
    try:
        for off, head, btype, ps, pe in _mp4_boxes(data, start, end):
            if btype != b"data" or pe - ps <= 8:
                continue
            return data[ps + 8:pe].decode("utf-8", "replace")
    except Exception:
        pass
    return ""


def _mp4_video_meta(data: bytes) -> dict:
    """mp4/mov/m4v：把 moov/udta/meta/ilst 下所有文本条目都取出来。

    **遍历而不是按名字取**：不同工具写进来的键名并不一致（ffmpeg 的自定义键叫
    `prompt`，标准键可能落在 `©cmt`），按名字取必然漏。统一捞出来，
    「哪个像 ComfyUI 的工作流」留给上层判断。

    条目名的解析要**分两种风格**（见 _mp4_key_table 的说明）：mdta 走 keys 索引表，
    QuickTime 的 mdir 才是四字符码。判断办法是「先当索引查表，查得到就用表里的名字」——
    索引合理时它必然落在 keys 表范围内，落不到就说明这是四字符码。
    """
    out = {}
    try:
        for off, head, btype, ps, pe in _mp4_boxes(data, 0, len(data)):
            if btype != b"moov":
                continue
            for o2, h2, t2, s2, e2 in _mp4_boxes(data, ps, pe):
                if t2 != b"udta":
                    continue
                for o3, h3, t3, s3, e3 in _mp4_boxes(data, s2, e2):
                    if t3 != b"meta":
                        continue
                    base = _mp4_meta_base(data, s3, e3)
                    if base is None:
                        continue
                    # keys 与 ilst 是兄弟、都在 meta 这一层：先把键名表建好，
                    # 才能把 ilst 条目名（mdta 风格下是索引）翻译成真正的键名
                    keytab = _mp4_key_table(data, base, e3)
                    for o4, h4, t4, s4, e4 in _mp4_boxes(data, base, e3):
                        if t4 != b"ilst":
                            continue
                        for o5, h5, t5, s5, e5 in _mp4_boxes(data, s4, e4):
                            val = _mp4_ilst_value(data, s5, e5)
                            if not val:
                                continue
                            key = keytab.get(int.from_bytes(t5, "big"))
                            if not key:
                                key = t5.decode("latin-1", "replace").strip()
                            # 键名可能带 mdta 命名空间前缀，去掉只留本体
                            if key.lower().startswith("mdta"):
                                key = key[4:].strip()
                            if key:
                                out[key] = val
    except Exception:
        return out
    return out


def _ebml_id(data: bytes, pos: int):
    """读 EBML 元素 ID。ID 是变长整数，**值含 marker bit**（0x45A3 就是 0x45A3）。
    返回 (id, 占几字节)；读不了返回 (None, 0)。"""
    if pos >= len(data):
        return None, 0
    first = data[pos]
    if first == 0:
        return None, 0
    n, mask = 1, 0x80
    while n <= 4 and not (first & mask):
        mask >>= 1
        n += 1
    if n > 4 or pos + n > len(data):
        return None, 0
    return int.from_bytes(data[pos:pos + n], "big"), n


def _ebml_size(data: bytes, pos: int):
    """读 EBML 长度字段。与 ID 相反，长度字段的**值不含** marker bit。
    返回 (size, 占几字节)；读不了返回 (None, 0)。"""
    if pos >= len(data):
        return None, 0
    first = data[pos]
    if first == 0:
        return None, 0
    n, mask = 1, 0x80
    while n <= 8 and not (first & mask):
        mask >>= 1
        n += 1
    if n > 8 or pos + n > len(data):
        return None, 0
    val = first & (mask - 1)
    for i in range(1, n):
        val = (val << 8) | data[pos + i]
    return val, n


def _ebml_children(data: bytes, start: int, end: int):
    """遍历 [start,end) 里的 EBML 子元素，产出 (id, 载荷起, 载荷止)。"""
    pos = start
    while pos < end:
        eid, n1 = _ebml_id(data, pos)
        if eid is None:
            return
        size, n2 = _ebml_size(data, pos + n1)
        if size is None:
            return
        ps = pos + n1 + n2
        if ps > end:
            return
        # 全 1 的长度 = 「未知长度，延伸到父元素末尾」（流式写入常见）
        unknown = size == (1 << (7 * n2)) - 1
        pe = end if unknown else min(ps + size, end)
        yield eid, ps, pe
        if unknown:
            return                      # 未知长度后面不会再有序的兄弟元素
        pos = pe


def _webm_tags_from(data: bytes, start: int, end: int) -> dict:
    """从 [start,end) 范围内的 Tags 元素里取 TagName → TagString。

    单独抽出来是因为有**两条入口**：
      · 常规路径：在 Segment 里找到 Tags（Segment 长度已知）；
      · 尾部探测路径：直接在文件尾部片段里搜到 Tags 的 ID —— 此时数据里没有 Segment，
        因为 Segment 的长度常常是「未知」（流式写入），我们只能跳到 Tags 处起解析。
    """
    out = {}
    try:
        for eid, ps, pe in _ebml_children(data, start, end):
            if eid != 0x1254C367:                # Tags
                continue
            for e2, p2, q2 in _ebml_children(data, ps, pe):
                if e2 != 0x7373:                 # Tag
                    continue
                for e3, p3, q3 in _ebml_children(data, p2, q2):
                    if e3 != 0x67C8:             # SimpleTag
                        continue
                    name = value = ""
                    for e4, p4, q4 in _ebml_children(data, p3, q3):
                        if e4 == 0x45A3:         # TagName
                            name = data[p4:q4].decode("utf-8", "replace").strip()
                        elif e4 == 0x4487:       # TagString
                            value = data[p4:q4].decode("utf-8", "replace")
                    if name and value:
                        out[name] = value
    except Exception:
        return out
    return out


def _webm_video_meta(data: bytes) -> dict:
    """webm/mkv：走 EBML Segment → Tags → Tag → SimpleTag。"""
    try:
        for eid, ps, pe in _ebml_children(data, 0, len(data)):
            if eid != 0x18538067:                    # Segment
                continue
            got = _webm_tags_from(data, ps, pe)
            if got:
                return got
    except Exception:
        pass
    return {}


def _read_at(path: Path, offset: int, n: int) -> bytes:
    """从文件指定位置读一段。offset 为负时按「距文件尾」处理。

    n <= 0 直接返回空 —— 因为 `f.read(-1)` 会**读整个文件**，那是几 GB 的意外内存占用，
    调用方少传或传错一个参数就会踩到（实测：50 字节的文件上 n=-1 读出了 50 字节 = 全部）。
    挡在入口比依赖调用方自觉更可靠。
    """
    if n <= 0:
        return b""
    try:
        size = path.stat().st_size
    except OSError:
        return b""
    if offset < 0:
        offset = max(0, size + offset)
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read(n)
    except OSError as ex:
        log(f"[meta] 读取失败：{path.name} {ex}")
        return b""


def _moov_candidates(data: bytes, limit: int = 8):
    """在片段里找 moov 的**可能起点**。

    读的是文件尾部片段时，片段起点会落在某个 box 中间，直接当 box 解析必然失败。
    所以先搜 `moov` 这 4 个字节，再回退 4 字节（box size 的位置）当候选起点，
    逐个试着解析 —— 谁解得出来算谁的。
    """
    out, i = [], data.find(b"moov")
    while i >= 4 and len(out) < limit:
        out.append(i - 4)
        i = data.find(b"moov", i + 1)
    return out


def video_meta(path: Path) -> dict:
    """读视频容器里的生成参数，返回与 png_meta 同构的 dict（prompt / workflow）。

    ⚠️ 键名统一转小写再匹配：**ffmpeg 写 webm 时会把自定义 tag 名转成大写**
    （实测得到 PROMPT / WORKFLOW，而 mp4 里是小写）。不统一大小写的话，
    webm 一个都匹配不上，而且看起来会像「这个格式本来就不带元数据」。

    不整体读文件，只探头、尾两段（原因见 VIDEO_PROBE_BYTES 的说明）。
    """
    kind = _VIDEO_META_KIND.get(path.suffix.lower())
    if not kind:
        return {}
    try:
        size = path.stat().st_size
    except OSError as ex:
        log(f"[meta] 读取视频失败：{path.name} {ex}")
        return {}
    if size <= 0:
        return {}

    if kind == "webm":
        # 通常 Tags 在 Segment 前部（muxer 先写头再写簇），读头就够。
        # 但它**也可以写在所有 Cluster 之后**（分段/流式写入时很常见），那时就落在头部
        # 窗口之外 —— 所以头部没找到时同样要探尾部，与 mp4 对称。
        # （审查实测：20MB 的 webm 把 Tags 放末尾，修这条之前读不到，且是静默失败。）
        raw = _webm_video_meta(_read_at(path, 0, VIDEO_PROBE_BYTES))
        if not raw and size > VIDEO_PROBE_BYTES:
            tail = _read_at(path, size - VIDEO_PROBE_BYTES, VIDEO_PROBE_BYTES)
            # EBML 是顺序容器、没有全局索引，没法像 mp4 那样按偏移定位；
            # 但 Tags 的 ID 是固定 4 字节，直接在尾部片段里搜它，找到就从那里起解析。
            # 尾部片段起点落在某个元素中间，所以搜到的位置可能不是真正的元素边界 ——
            # 这里只取第一个命中，_webm_tags_from 内部结构不对时会安全返回空。
            idx = tail.find(_TAGS_ID)
            if idx >= 0:
                raw = _webm_tags_from(tail, idx, len(tail))
    else:
        # moov 可能在头（开了 faststart）也可能在尾（ffmpeg 默认）—— 两头都探。
        # 只探一头必然漏掉一半来源，而且现象是「有些视频读不到」，很难归因。
        raw = _mp4_video_meta(_read_at(path, 0, VIDEO_PROBE_BYTES))
        if not raw and size > VIDEO_PROBE_BYTES:
            tail = _read_at(path, size - VIDEO_PROBE_BYTES, VIDEO_PROBE_BYTES)
            for st in _moov_candidates(tail):
                raw = _mp4_video_meta(tail[st:])
                if raw:
                    break
    if not raw:
        return {}
    low = {}
    for k, v in raw.items():
        low[str(k).strip().lower()] = v
    out = {}
    for key in ("prompt", "workflow", "parameters"):
        if low.get(key):
            out[key] = low[key]
    return out


def comfy_workflows_dir() -> Path:
    """ComfyUI 的 UI 工作流模板目录。"""
    r = comfy_root()
    if r:
        return r / "user" / "default" / "workflows"
    return Path(os.devnull)      # 占位：glob / is_file 均安全落空


# 批次 F · F6（CMV3-LOCAL-FIX 回植）：模板目录指纹缓存。
# 现状：每次 `/api/templates` 都要把 52 个模板（合计 3.3 MB）全部 json.loads
# 只为了判断「含不含 KSampler + CLIPTextEncode」—— 冷 1181 ms、稳态 97 ms，
# 而这个列表在一次会话里几乎不变（改模板是低频操作）。
#
# ⚠️ 不要改成子串匹配代替 JSON 解析 —— 实测 38 ms vs 30 ms，**子串更慢**，
#    而且 `"KSampler"` 出现在注释/标题里会误判（方案文档 §F6 已记）。
_TPL_CACHE = {"key": None, "val": []}
_TPL_LOCK = threading.Lock()


def _tpl_dir_key(d: Path):
    """模板目录指纹；目录读不了返回 None（表示「别用缓存」）。

    （CMV3-LOCAL-FIX，从旧工程 server.py 回植 · 批次 F 的 F6）
    指纹取「文件名 + size + mtime_ns」的整表元组，而不是只看目录 mtime：
    Windows 上**覆盖写同一文件不会改目录 mtime**，只看目录的话
    「改了模板内容，CMV 里 compatible 还是旧的」。52 次 stat 约 0.2 ms，可忽略。
    """
    parts = []
    try:
        for f in sorted(d.glob("*.json")):
            if f.name.startswith("."):
                continue
            try:
                st = f.stat()
                parts.append((f.name, st.st_size, st.st_mtime_ns))
            except OSError:
                parts.append((f.name, -1, -1))
    except OSError:
        return None
    return tuple(parts)


def list_templates() -> list:
    """ComfyUI 已保存的 UI 工作流模板 [{id, name, compatible}]，按名称排序。

    compatible=模板含 KSampler+CLIPTextEncode，可承载生成参数（Txt2Img 类）。

    （CMV3-LOCAL-FIX，从旧工程 server.py 回植 · 批次 F 的 F6）
    按目录指纹缓存（见 `_tpl_dir_key`），命中时直接返回。
    """
    d = comfy_workflows_dir()
    key = _tpl_dir_key(d)
    if key is not None:
        with _TPL_LOCK:
            if _TPL_CACHE["key"] == key:
                return [dict(x) for x in _TPL_CACHE["val"]]
    try:
        files = sorted(f for f in d.glob("*.json") if not f.name.startswith("."))
    except OSError:
        return []
    out = []
    for f in files:
        has_s = has_e = False
        try:
            nodes = json.loads(f.read_text("utf-8")).get("nodes") or []
            has_s = any(n.get("type") in ("KSampler", "KSamplerAdvanced") for n in nodes)
            has_e = any(n.get("type") == "CLIPTextEncode" for n in nodes)
        except (OSError, ValueError):
            pass
        out.append({"id": f.name, "name": f.stem, "compatible": has_s and has_e})
    if key is not None:
        with _TPL_LOCK:
            _TPL_CACHE["key"], _TPL_CACHE["val"] = key, out
    return [dict(x) for x in out]


def _apply_parsed(tpl: dict, parsed: dict) -> None:
    """把解析出的参数写进 UI 格式模板（就地修改 nodes 的 widgets_values）。

    采样器 / 大模型 / 出图尺寸只写第一个节点，避免多段采样（hires 等）模板被
    同一组参数无差别覆盖；LoRA 按模板里的 LoRA 节点顺序逐个填入，装不下的丢弃。
    A1111 来源的参数在此做类型与名称转换。
    """
    nodes = tpl.get("nodes") or []
    clipse = [n for n in nodes if n.get("type") == "CLIPTextEncode"]
    pos_n = neg_n = None
    for n in clipse:
        t = (n.get("title") or "").lower()
        if not pos_n and ("positive" in t or "正" in t) and "neg" not in t:
            pos_n = n
        elif not neg_n and ("negative" in t or "负" in t or "反向" in t):
            neg_n = n
    # 7-4：兜底配对只允许「双侧都没识别出」时整对猜（clipse[0]/clipse[-1]）；
    #      只识别出一侧时，仅当全图恰好两个编码器才取另一个——多编码器模板里
    #      乱猜会把负向提示词写进第二个正向节点（覆盖原正向内容且无任何报错）。
    if not pos_n and not neg_n and len(clipse) >= 2:
        pos_n, neg_n = clipse[0], clipse[-1]
    elif neg_n is None and pos_n is not None and len(clipse) == 2:
        neg_n = clipse[0] if clipse[1] is pos_n else clipse[1]
    elif pos_n is None and neg_n is not None and len(clipse) == 2:
        pos_n = clipse[0] if clipse[1] is neg_n else clipse[1]

    def set_widget(n, idx, val):
        if n is None or val is None:
            return
        wv = n.get("widgets_values")
        if isinstance(wv, list) and idx < len(wv):
            wv[idx] = val

    set_widget(pos_n, 0, parsed.get("positive"))
    set_widget(neg_n, 0, parsed.get("negative"))

    seed = _num(parsed.get("seed"), as_int=True)
    steps = _num(parsed.get("steps"), as_int=True)
    cfg = _num(parsed.get("cfg"))
    sampler = sch_from_sampler = None
    if parsed.get("sampler"):
        sampler, sch_from_sampler = _a1111_sampler(str(parsed["sampler"]))
    scheduler = parsed.get("scheduler")
    if scheduler:
        scheduler = _A1111_SCHEDULERS.get(str(scheduler).strip().lower(),
                                          str(scheduler).strip().lower())
    elif sch_from_sampler:
        scheduler = sch_from_sampler          # "DPM++ 2M Karras" 这类名字自带的调度器

    pending_loras = list(parsed.get("loras") or [])
    done = set()
    for n in nodes:
        ty = n.get("type") or ""
        tyl = ty.lower()
        wv = n.get("widgets_values")
        if not isinstance(wv, list):
            continue
        if ty == "KSampler":        # [seed, ctrl, steps, cfg, sampler, scheduler, denoise]
            if "sampler" in done:
                continue
            done.add("sampler")
            if seed is not None and len(wv) > 0: wv[0] = seed
            if steps is not None and len(wv) > 2: wv[2] = steps
            if cfg is not None and len(wv) > 3: wv[3] = cfg
            if sampler and len(wv) > 4: wv[4] = sampler
            if scheduler and len(wv) > 5: wv[5] = scheduler
        elif ty == "KSamplerAdvanced":
            # [add_noise, noise_seed, ctrl, steps, cfg, sampler, scheduler, ...]
            if "sampler" in done:
                continue
            done.add("sampler")
            if seed is not None and len(wv) > 1: wv[1] = seed
            if steps is not None and len(wv) > 3: wv[3] = steps
            if cfg is not None and len(wv) > 4: wv[4] = cfg
            if sampler and len(wv) > 5: wv[5] = sampler
            if scheduler and len(wv) > 6: wv[6] = scheduler
        elif "lora" in tyl and "trigger" not in tyl and pending_loras:
            first = pending_loras.pop(0)
            # 7-3：强度允许负数（概念抹除/反向 LoRA，如 <lora:xxx:-1> 会生成 "xxx (-1)"）；
            #      旧正则 [\d.]+ 不吃负号 ⇒ 整串「名字 (-1)」被当文件名写进 widgets_values[0]，
            #      ComfyUI 节点拿到不存在的文件名直接报错。负号是唯一缺口，加一个字符即闭合。
            m = re.match(r"(.+?)\s*\((-?[\d.]+)\)\s*$", first)
            nm, st = (m.group(1), float(m.group(2))) if m else (first, None)
            if len(wv) > 0: wv[0] = nm
            if st is not None and len(wv) > 1: wv[1] = st
        elif ("checkpoint" in tyl and "loader" in tyl) or "unetloader" in tyl:
            if "ckpt" in done:
                continue
            done.add("ckpt")
            if parsed.get("model") and len(wv) > 0: wv[0] = parsed["model"]
        elif "empty" in tyl and "latent" in tyl and parsed.get("size") and len(wv) > 1:
            if "latent" in done:
                continue
            done.add("latent")
            try:
                w, h = str(parsed["size"]).split("x")
                wv[0], wv[1] = int(w.strip()), int(h.strip())
            except (ValueError, AttributeError):
                pass


def _ask_save_path(suggested_name: str, initial_dir: str = ""):
    """弹 Windows 原生「另存为」对话框，返回用户选定的完整路径。

    返回三态：
      - 非空字符串：用户选定的完整路径（文件可能已存在，覆盖由调用方负责）
      - ""        ：用户取消
      - None      ：当前环境弹不出框（非 Windows / ctypes 调用失败）⇒ 调用方回落自动落盘

    只用 comdlg32!GetSaveFileNameW（系统自带，无第三方依赖、不增打包体积），
    与 recycle_bin_delete 同款路子。请求跑在 ThreadingHTTPServer 子线程上，
    模态框只阻塞这一个请求线程，其它接口照常响应。

    hwndOwner 取前台窗口：否则弹框没有归属，很容易被应用窗口盖住，
    用户会以为「点了导出没反应」。
    """
    if str(os.environ.get("CMV_NO_SAVE_DIALOG") or "").strip() not in ("", "0"):
        # 测试 / 无 GUI 环境：跳过对话框，让调用方走「自动落盘回落」分支
        # （回落 = 写到原图所在目录 + _unique_path 去重，调用方仍如实回传路径）
        return None
    if os.name != "nt":
        return None
    try:
        from ctypes import wintypes as W
        user32 = ctypes.windll.user32
        comdlg32 = ctypes.windll.comdlg32
        ole32 = ctypes.windll.ole32
    except (OSError, AttributeError):
        return None

    OFN_OVERWRITEPROMPT = 0x00000002
    OFN_NOCHANGEDIR     = 0x00000008
    OFN_PATHMUSTEXIST   = 0x00000800
    OFN_EXPLORER        = 0x00080000

    class OPENFILENAMEW(ctypes.Structure):
        # 字段顺序/类型须与 Win32 OPENFILENAMEW 严格一致，否则 GetSaveFileNameW
        # 以 CDERR_STRUCTSIZE 直接失败（hwnd 与指针在 x64 上各占 8 字节）
        _fields_ = [
            ("lStructSize",       W.DWORD),
            ("hwndOwner",         W.HWND),
            ("hInstance",         W.HINSTANCE),
            ("lpstrFilter",       W.LPCWSTR),
            ("lpstrCustomFilter", W.LPWSTR),
            ("nMaxCustFilter",    W.DWORD),
            ("nFilterIndex",      W.DWORD),
            ("lpstrFile",         W.LPWSTR),
            ("nMaxFile",          W.DWORD),
            ("lpstrFileTitle",    W.LPWSTR),
            ("nMaxFileTitle",     W.DWORD),
            ("lpstrInitialDir",   W.LPCWSTR),
            ("lpstrTitle",        W.LPCWSTR),
            ("Flags",             W.DWORD),
            ("nFileOffset",       W.WORD),
            ("nFileExtension",    W.WORD),
            ("lpstrDefExt",       W.LPCWSTR),
            ("lCustData",         ctypes.c_ssize_t),   # LPARAM = LONG_PTR，指针宽
            ("lpfnHook",          ctypes.c_void_p),
            ("lpTemplateName",    W.LPCWSTR),
            ("pvReserved",        ctypes.c_void_p),
            ("dwReserved",        W.DWORD),
            ("FlagsEx",           W.DWORD),
        ]

    hr = ole32.CoInitializeEx(None, 0x2)          # COINIT_APARTMENTTHREADED
    own_com = hr in (0, 1)                        # S_OK / S_FALSE 才是本线程自己初始化的
    try:
        buf = ctypes.create_unicode_buffer(suggested_name, 4096)
        # 过滤器是「显示名\0通配\0……\0\0」的双 NUL 结尾多字符串
        filt = ctypes.create_unicode_buffer(
            "工作流 JSON (*.json)\0*.json\0所有文件 (*.*)\0*.*\0\0")
        ofn = OPENFILENAMEW()
        ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
        try:
            user32.GetForegroundWindow.restype = W.HWND     # 默认 int 会截断 64 位句柄
            ofn.hwndOwner = user32.GetForegroundWindow()
        except Exception:
            ofn.hwndOwner = None
        ofn.lpstrFilter = ctypes.cast(filt, W.LPCWSTR)
        ofn.nFilterIndex = 1
        ofn.lpstrFile = ctypes.cast(buf, W.LPWSTR)
        ofn.nMaxFile = 4096
        ofn.lpstrInitialDir = initial_dir or None
        ofn.lpstrTitle = "保存工作流 JSON"
        ofn.lpstrDefExt = "json"
        ofn.Flags = (OFN_OVERWRITEPROMPT | OFN_PATHMUSTEXIST
                     | OFN_EXPLORER | OFN_NOCHANGEDIR)
        comdlg32.GetSaveFileNameW.restype = W.BOOL
        comdlg32.GetSaveFileNameW.argtypes = [ctypes.POINTER(OPENFILENAMEW)]
        if not comdlg32.GetSaveFileNameW(ctypes.byref(ofn)):
            return ""                             # 用户取消
        return buf.value
    except Exception as ex:
        log("[导出] 保存对话框不可用，回落自动落盘：%s: %s" % (type(ex).__name__, ex))
        return None
    finally:
        if own_com:
            try:
                ole32.CoUninitialize()
            except Exception:
                pass


def export_workflow(path: Path, template_id: str = None) -> dict:
    """提取 PNG 内嵌 UI 工作流；缺失时用模板 + 本图参数合成。只产出，不提交生成。"""
    suf = path.suffix.lower()
    if suf == ".png":
        meta = png_meta(path)
    elif suf in (".jpg", ".jpeg", ".webp"):
        meta = parse_jpeg_meta(path)
    else:
        meta = {}
    raw = meta.get("workflow")
    if raw:
        try:
            json.loads(raw)
            return {"ok": True, "json": raw, "source": "png"}
        except ValueError:
            pass
    if not template_id:
        return {"ok": False,
                "error": "该图未内嵌 UI 工作流（API 生成），请选择 ComfyUI 模板来合成参数"}
    if not re.fullmatch(r"[^/\\]+\.json", template_id):   # 只允许裸文件名，防路径穿越
        return {"ok": False, "error": "非法模板名: " + template_id}
    tpl_path = comfy_workflows_dir() / template_id
    if not tpl_path.is_file():
        return {"ok": False, "error": "模板不存在: " + template_id}
    try:
        tpl = json.loads(tpl_path.read_text("utf-8"))
    except (OSError, ValueError) as ex:
        return {"ok": False, "error": f"模板读取失败: {ex}"}
    if not isinstance(tpl, dict) or not tpl.get("nodes"):
        return {"ok": False, "error": "模板不是合法的 UI 工作流"}
    nodes = tpl.get("nodes") or []
    has_sampler = any(n.get("type") in ("KSampler", "KSamplerAdvanced") for n in nodes)
    if not has_sampler:
        return {"ok": False,
                "error": "模板「" + template_id + "」不含采样节点（可能是放大/后处理工作流），请选带 KSampler 的 Txt2Img 模板"}
    parsed = parse_meta(meta) or {}
    if not parsed:
        return {"ok": False, "error": "该图没有可解析的生成参数，无法合成"}
    try:
        _apply_parsed(tpl, parsed)
    except Exception as ex:
        return {"ok": False, "error": f"参数合成失败: {type(ex).__name__}: {ex}"}
    return {"ok": True, "json": json.dumps(tpl, ensure_ascii=False),
            "source": "template", "parsed": parsed}


# ------------------------------------------------- ComfyUI 画布注入（打开窗口摆好工作流，不自动跑）

EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
CDP_PORT = 9333
BROWSER_USER_DIR = DATA_DIR / "edge-profile"
COMFY_BROWSER = {"proc": None, "ws": None}


def _edge_exe() -> str:
    for p in EDGE_PATHS:
        if Path(p).is_file():
            return p
    return None


def _cdp_http(path: str, timeout: float = 8) -> dict:
    with _OPENER_NO_PROXY.open(f"http://127.0.0.1:{CDP_PORT}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _ensure_edge():
    """确保带调试端口的独立 Edge 实例在跑（独立 profile，不影响日常浏览器）。

    ε-13（§6.6）：此前旧 Edge 的进程句柄被静默丢弃 —— 句柄还在但不响应 CDP
    （浏览器假死 / 调试通道卡住）时，直接再 spawn 一个新 Edge：同一个
    --user-data-dir + 同一个调试端口，新实例要么附着到旧进程上要么起不来，
    而旧的那个永远没人回收。现在 spawn 前先把旧进程 terminate/kill 干净。
    """
    old = COMFY_BROWSER.get("proc")
    if old is not None:
        alive = old.poll() is None
        try:
            _cdp_http("/json")
            if alive:
                return                      # 进程活着且 CDP 可达：复用
        except Exception:
            pass
        # 走到这里 = 进程已死，或活着但 CDP 不可达（假死）：都按旧的清掉重来
        try:
            if old.poll() is None:
                old.terminate()
                try:
                    old.wait(timeout=3)
                except Exception:
                    old.kill()
        except Exception:
            pass
        COMFY_BROWSER["proc"] = None
    exe = _edge_exe()
    if not exe:
        raise RuntimeError("未找到 Microsoft Edge")
    BROWSER_USER_DIR.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([
        exe, f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={str(BROWSER_USER_DIR)}",
        "--no-first-run", "--no-default-browser-check", "--new-window", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    COMFY_BROWSER["proc"] = proc
    for _ in range(50):
        try:
            _cdp_http("/json")
            return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("Edge 调试端口启动超时")


def _open_comfy_tab() -> str:
    """确保调试浏览器里有一个 ComfyUI 标签页，返回其 webSocketDebuggerUrl。"""
    hostport = urllib.parse.urlparse(COMFY_BASE).netloc
    try:
        tabs = _cdp_http("/json")
    except Exception:
        return None
    for t in tabs:
        if t.get("type") == "page" and hostport in (t.get("url") or ""):
            return t.get("webSocketDebuggerUrl")
    for method in ("PUT", "GET"):   # 新版内核要求 PUT，老内核只认 GET
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{CDP_PORT}/json/new?url=" +
                urllib.parse.quote(COMFY_BASE + "/"),
                method=method)
            t = json.loads(_OPENER_NO_PROXY.open(req, timeout=8).read().decode("utf-8"))
            return t.get("webSocketDebuggerUrl")
        except urllib.error.HTTPError as ex:
            if ex.code == 405:
                continue
            return None
        except Exception:
            return None
    return None


def open_comfy_page(workflow: dict) -> dict:
    """在独立 Edge 窗口中把工作流加载到 ComfyUI 画布（不执行，用户手动 Queue）。"""
    _ensure_edge()
    try:
        import sys as _sys
        _d = str(Path(__file__).resolve().parent)
        if _d not in _sys.path:
            _sys.path.insert(0, _d)      # python_embeded 隔离模式：_pth 不含脚本目录
        import ws_client
        ws_url = _open_comfy_tab()
        if not ws_url:
            return {"ok": False, "error": f"无法打开 ComfyUI 页面({COMFY_BASE})"}
        ws = ws_client.ws_connect(ws_url, timeout=15)
    except Exception as ex:
        return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
    old = COMFY_BROWSER.get("ws")
    if old is not None and old is not ws:
        try:
            ws_client.ws_close(old)      # 换掉旧连接前先关，防句柄泄漏
        except Exception:
            pass
    COMFY_BROWSER["ws"] = ws
    try:
        # 确保标签页指向 ComfyUI（/json/new 的 url 参数在部分版本被忽略）
        tgt = ws_client.cdp_call(ws, "Page.navigate",
                                 {"url": COMFY_BASE + "/"}, timeout=30)
        if "error" in tgt:
            return {"ok": False, "error": "导航失败: " + str(tgt["error"])}
        # 等待前端 app 就绪并注入工作流（60s 上限，含页面加载）
        expr = ("(async () => { const wf = " + json.dumps(workflow, ensure_ascii=False) +
                "; for (let i = 0; i < 300; i++) { if (window.app && window.app.loadGraphData) break;"
                " await new Promise(r => setTimeout(r, 200)); }"
                " if (!(window.app && window.app.loadGraphData)) return 'NO_APP';"
                " await window.app.loadGraphData(wf); return 'LOADED'; })()")
        msg = ws_client.cdp_eval(ws, expr, timeout=90)
    except Exception as ex:
        COMFY_BROWSER["ws"] = None
        try:
            ws_client.ws_close(ws)       # 注入失败即回收连接
        except Exception:
            pass
        return {"ok": False, "error": f"注入失败: {type(ex).__name__}: {ex}"}
    res = ((msg.get("result") or {}).get("result") or {}).get("value")
    if res == "LOADED":
        return {"ok": True, "url": COMFY_BASE + "/"}
    if res == "NO_APP":
        return {"ok": False, "error": "ComfyUI 前端未就绪（无法注入 loadGraphData）"}
    return {"ok": False, "error": "注入异常: " + str(msg)[:160]}


# ------------------------------------------------- 回传·拉取模式（ComfyUI 页面内脚本取件）
#
# 上面 open_comfy_page() 是「推」：自己拉一个带调试端口的 Edge 去注入画布，
# 代价是必然弹出一个独立空 profile 的浏览器窗口。
#
# 这里是「拉」：ComfyUI 页面里跑一个极小的脚本（frontend/bridge/ 下那份），
# 它天然持有 window.app，直接 app.loadGraphData()。于是
#   · 不需要调试端口——浏览器自 136 起已禁止用默认 profile 开 CDP；
#   · 不需要新窗口——用户平时开着的那个 ComfyUI 标签页直接就收到了。
# 本层只做三件事：收件（入队）、发件（GET + CORS）、记账（谁在线）。
# 之所以取件走 GET：do_POST 有 _same_origin() CSRF 校验，跨源必 403。

PULL_QUEUE = []                             # 待取件（内存队列，进程退出即清空）
PULL_LOCK = threading.Lock()
# 取件序号。基数取「启动时刻」而不是 0：页面内脚本拿它给「已载入的件」去重，
# 若每次启动都从 1 开始，重启后新件会与上一进程的旧序号撞号，被误判成「已载入」而丢弃。
PULL_SEQ = [int(time.time())]
PULL_KEEP = 20                              # 无人取件时的队列上限，防无限堆积
# 两阶段取件（V2.8.4）：件被页面取走后先挂在这里，等页面回执才真正销毁。
# 此前是「取走即出队」——页面一旦载入失败（ComfyUI 前端没起来、工作流损坏），
# 件就凭空消失：用户看到「已回传」却什么都没发生，也无从重试。
PULL_INFLIGHT = {}                          # seq -> {"item","t","port","claim"}
# P0-9：投递代次计数器（单调递增，只增不减）。同一件会被重投（nack / 超时兜底），
# 于是一条 seq 对应过多次投递 —— claim 标记「这一次投递」的身份，随件交给页面、
# 回执时原样带回核对；对不上 = 迟到回执，必须忽略（否则会把新一轮的 in-flight
# 记录弹掉 ⇒ 件既不在队列也不在 in-flight ⇒ 永久丢失）。
PULL_CLAIM = [0]
BRIDGE_ACK_TTL = 120.0                      # 等回执的上限（秒）；载入大工作流也够用
# P1-32：取件超时（abort）之后的兜底回收窗口（秒）。
# 桥接脚本单次取件超时 = 10s（comfy-cmv-bridge.js 的 PULL_TIMEOUT），abort 后它拿不到 seq
# ⇒ 既不能 ack 也不能 nack，件只能干等 BRIDGE_ACK_TTL（120s），期间页面每轮都取不到件、
# 用户零提示 —— 「CMV 已报成功但工作流永远到不了」里最难自查的那一条。
# 判据：**同一个端口又来取件** ⇒ 页面侧 busy 串行，说明上一次请求已经结束；
# 若它挂起已超过本窗口，就认定它是被 abort 掉的那一件，立刻放回队列。
# 窗口比前端超时略大，是为了容忍「同端口开了两个标签页」这种并发 —— 那时不急着抢。
PULL_BACKOFF = (0.0, 0.0, 4.0, 12.0)        # [tries] → 延迟；tries 从 1 起（0 位留空）


PULL_DROPPED = []                           # [{"seq","name","reason","t"}]，有界


PULL_DROPPED_KEEP = 20


BRIDGE_CLAIM_GRACE = 12.0
BRIDGE_SEEN = {}                            # {端口: 最近一次取件时刻}
BRIDGE_TABS = {}                            # {tabId: {"port": …, "t": 最近活跃}}


BRIDGE_TAB_KEEP = 24                        # 记账上限，防标签页开开关关把内存撑大


BRIDGE_TTL = 25.0                           # 心跳有效期（秒）：超时视为页面已关闭
# 为什么是 25 秒而不是更短：页面每 1.5 秒取一次件（= 心跳），但
#   · 后台标签页的定时器会被浏览器降频到约 1 次/分钟；
#   · 页面若被某个原生对话框挡住，JS 会整段冻结。
# 窗口开太小会出现「我明明开着 ComfyUI，回传却另开了一个 Edge 窗口」这种误判；
# 开太大则页面关掉后还会在队列里空等一会儿。25 秒是两者的折中。
# 注意这**不是**判断送达的依据：件入队后会一直留着（PULL_KEEP 上限），
# 页面恢复轮询就会取走，所以这里放宽只会让「静默失败」更少见，不会丢件。
BRIDGE_NODE = "ComfyMediaViewer-Bridge"     # 装到 ComfyUI custom_nodes 下的目录名
BRIDGE_INIT = '''# ComfyMediaViewer 桥接（纯前端扩展，不注册任何节点）
#
# 让【已打开】的 ComfyUI 页面直接接收 ComfyMediaViewer 送来的工作流。
# 本文件除声明 WEB_DIRECTORY 外不做任何事，实际逻辑在 web/comfy-cmv-bridge.js。
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
WEB_DIRECTORY = "./web"
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
'''


def _comfy_port() -> str:
    """本机 ComfyUI 的端口，作为队列分发目标（多实例时按端口送到「对应的页面」）。"""
    netloc = urllib.parse.urlparse(COMFY_BASE).netloc
    return netloc.rsplit(":", 1)[1] if ":" in netloc else "80"


def bridge_push(path: Path, tpl=None, port="") -> dict:
    """导出工作流并入队，等 ComfyUI 页面内的桥接脚本取走。"""
    r = export_workflow(path, tpl)
    if not r.get("ok"):
        return r
    try:
        wf = json.loads(r["json"])
    except ValueError:
        return {"ok": False, "error": "工作流 JSON 解析失败"}
    if r.get("source") == "png":
        name = path.stem + "_内嵌.json"
    else:
        t = str(tpl)
        name = f"{path.stem}_{t[:-5] if t.endswith('.json') else t}.json"
    with PULL_LOCK:
        PULL_SEQ[0] += 1
        item = {"seq": PULL_SEQ[0], "kind": "workflow",
                "target": str(port or _comfy_port()),
                "name": name, "source": path.name, "workflow": wf}
        PULL_QUEUE.append(item)
        del PULL_QUEUE[:-PULL_KEEP]
        n = len(PULL_QUEUE)
    log(f"[bridge] 入队 #{item['seq']} {name} → 端口 {item['target']}（队列 {n}）")
    return {"ok": True, "seq": item["seq"], "queued": n,
            "name": name, "target": item["target"]}


def _requeue_stale(now: float) -> list:
    """把「已投递但迟迟没有回执」的件放回队列（页面在载入途中被关掉 / 崩了）。

    必须在持有 PULL_LOCK 时调用。放回队首：它们是最老的件，理应优先重投。
    """
    back = []
    for seq, rec in list(PULL_INFLIGHT.items()):
        if now - rec["t"] > BRIDGE_ACK_TTL:
            _it = PULL_INFLIGHT.pop(seq)["item"]
            _it.pop("not_before", None)   # ε-7：兜底放回的件应**立刻**可重投，留着退避会白等
            back.append(_it)
    if back:
        back.sort(key=lambda x: x.get("seq") or 0)
        for it in reversed(back):
            PULL_QUEUE.insert(0, it)
        if len(PULL_QUEUE) > PULL_KEEP:
            del PULL_QUEUE[:-PULL_KEEP]
        log(f"[bridge] 超过 {BRIDGE_ACK_TTL:.0f}s 无回执，放回队列 "
            f"{[x.get('seq') for x in back]}")
    return back


def _reclaim_abandoned(want: str, now: float) -> list:
    """P1-32 兜底回收：把「挂在同一个端口上过久」的件放回队列。

    与 bridge_release 的关系：那条是前端**明确告知**放弃；这条是前端没来得及说话时的兜底
    （它未必需要——前端 abort 后会立刻发 release，但 release 本身也可能发不出去，
    因为 CMV 正是卡住了才会让取件超时）。

    必须持有 PULL_LOCK 时调用。放回队首：它们是最老的件，理应优先重投。
    """
    if not want:
        return []
    back = []
    for seq, rec in list(PULL_INFLIGHT.items()):
        if str(rec.get("port") or "") == want and now - rec["t"] > BRIDGE_CLAIM_GRACE:
            _it = PULL_INFLIGHT.pop(seq)["item"]
            _it.pop("not_before", None)   # ε-7：同上（abort 兜底回收）
            back.append(_it)
    if back:
        back.sort(key=lambda x: x.get("seq") or 0)
        for it in reversed(back):
            PULL_QUEUE.insert(0, it)
        if len(PULL_QUEUE) > PULL_KEEP:
            del PULL_QUEUE[:-PULL_KEEP]
        log(f"[bridge] 端口 {want} 的取件挂起超过 {BRIDGE_CLAIM_GRACE:.0f}s（疑似 abort），"
            f"放回队列 {[x.get('seq') for x in back]}")
    return back


def _pull_ready(x, now: float) -> bool:
    """件是否**已到可重投时刻**（ε-7 退避，见 PULL_BACKOFF）。

    ⚠️ 未到时刻的件**仍留在队列里**，只是这一轮不摘走 —— 它既不是「丢弃」，
    也不是「出队失败」。必须持有 PULL_LOCK 时调用（只读，但读的是共享结构）。
    没有 not_before 字段（首次入队 / 超时兜底放回 / 主动放弃后收回）⇒ 恒为可取。
    """
    nb = x.get("not_before")
    return (nb is None) or (now >= nb)


def bridge_take(want: str, reclaim: bool = True):
    """取一件发给 want 端口的工作流，并挂起等回执（见 bridge_ack / bridge_nack）。

    优先级：端口严格匹配 > 未指定目标的件 > 单实例兜底（只有一个活着的桥接页面时才允许取任意件）。
    此前 want 为空时会命中任意端口的件，多页面同时在线时 A 页面可能取走发给 B 页面的工作流。

    reclaim（P0-10）：要不要跑「abort 兜底回收」。页面侧**正在载入**时不能跑 ——
    那时它取不到件，却又符合「同端口又来人 + 挂起够久」的旧判据，会把正在载入的件抢走重投。
    老版脚本不带这个参数 ⇒ 保持 True（行为与改动前一致）。
    """
    now = time.time()
    with PULL_LOCK:
        _requeue_stale(now)
        if reclaim:
            _reclaim_abandoned(want, now)      # P1-32：前端 abort 后的兜底回收
        it = None
        if want:
            for i, x in enumerate(PULL_QUEUE):
                # ε-7：退避期内的件跳过（continue 而非 break —— 后面可能还有别的件可取）
                if str(x.get("target") or "") == want and _pull_ready(x, now):
                    it = PULL_QUEUE.pop(i)
                    break
        if it is None:
            for i, x in enumerate(PULL_QUEUE):     # 入队时没指定目标的件：任何页面都能取
                if not str(x.get("target") or "") and _pull_ready(x, now):
                    it = PULL_QUEUE.pop(i)
                    break
        if it is None and not want and PULL_QUEUE:  # 单实例宽松兜底（不重入 PULL_LOCK，直接读 BRIDGE_SEEN）
            live = [p for p, t in BRIDGE_SEEN.items() if now - t <= BRIDGE_TTL]
            if len(live) <= 1:
                for i, x in enumerate(PULL_QUEUE):
                    if _pull_ready(x, now):
                        it = PULL_QUEUE.pop(i)
                        break
        if it is not None:
            # 挂起：只有页面回执 ack 才销毁；nack 或超时都会让它回到队列。
            # claim 是「这一次投递」的身份（见 PULL_CLAIM 的说明），随件交给页面、
            # 回执时带回来核对 —— 迟到的旧回执因此不会再抹掉新一轮投递。
            PULL_CLAIM[0] += 1
            claim = PULL_CLAIM[0]
            PULL_INFLIGHT[it["seq"]] = {"item": it, "t": now, "port": want or "",
                                        "claim": claim}
            it["claim"] = claim
        return it


def _claim_ok(rec, claim) -> bool:
    """这次回执是不是**当前这一轮投递**的回执（P0-9）。

    False = 迟到回执（页面超时重试后，上一轮的 ack/nack 才姗姗来迟）⇒ 必须忽略：
    它若被当成本轮的回执，会把新一轮的 in-flight 记录弹掉 ⇒ 件永久丢失
    （队列里没有、in-flight 里也没有，而用户已经看到「已回传」）。

    取件方拿不到 claim 时传空（abort 拿不到 seq 的自然延伸 / 老版脚本不带）⇒
    回落到「只按 seq 匹配」的旧行为，不破坏兼容。
    """
    if rec is None:
        return False
    want = str(claim or "").strip()
    if not want:
        return True
    return str(rec.get("claim") or "") == want


def bridge_ack(seq, claim="") -> dict:
    """页面回执：这件已经成功载入，可以销毁了。

    claim（P0-9）：这一次投递的身份。与当前 in-flight 记录不符 = 迟到回执
    ⇒ 忽略（不弹记录、不销毁）。改前只按 seq 弹 ⇒ 迟到 ack 会把新一轮投递的记录抹掉，
    件从 in-flight 消失、队列里也没有 ⇒ 永久丢失。
    """
    try:
        s = int(seq)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad seq"}
    rec = None
    with PULL_LOCK:
        cur = PULL_INFLIGHT.get(s)
        if _claim_ok(cur, claim):
            rec = PULL_INFLIGHT.pop(s, None)
    if rec:
        log(f"[bridge] 已确认送达 #{s}")
    elif cur is not None:
        log(f"[bridge] 忽略迟到的 ack #{s}（不是当前这一轮投递）")
    return {"ok": True, "seq": s, "dropped": bool(rec)}


def bridge_nack(seq, tries=0, claim="") -> dict:
    """页面回执：这件载入失败，放回队列等重投（带退避，见 PULL_BACKOFF）。

    tries = 前端报来的「这是第几次尝试失败」（从 1 起）。ε-7 之前不带这个参数，
    件一律立刻插回队首 ⇒ 3 次尝试挤在 3 秒内砸完。现在按次数给件打 not_before，
    bridge_take 会跳过还没到时刻的件（跳过 ≠ 丢弃，件仍留在队列里）。
    claim（P0-9）：同 bridge_ack —— 迟到回执必须忽略，否则会把新一轮投递的件
    又插回队列一次（同一个工作流进画布两遍）。
    """
    try:
        s = int(seq)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad seq"}
    try:
        n = int(tries) if tries else 0
    except (TypeError, ValueError):
        n = 0
    if n < 0:
        n = 0
    delay = PULL_BACKOFF[min(n, len(PULL_BACKOFF) - 1)]
    rec = None
    with PULL_LOCK:
        cur = PULL_INFLIGHT.get(s)
        if _claim_ok(cur, claim):
            rec = PULL_INFLIGHT.pop(s, None)
            if rec:
                rec["item"].pop("not_before", None)
                if delay > 0:
                    rec["item"]["not_before"] = time.time() + delay
                PULL_QUEUE.insert(0, rec["item"])
    if rec:
        log(f"[bridge] 载入失败，放回队列重投 #{s}"
            + (f"（第 {n} 次失败，退避 {delay:.0f}s）" if delay > 0 else ""))
    elif cur is not None:
        log(f"[bridge] 忽略迟到的 nack #{s}（不是当前这一轮投递）")
    return {"ok": True, "seq": s, "requeued": bool(rec), "backoff": delay}


def bridge_drop(seq, reason="", name="", claim="") -> dict:
    """页面回执：**已放弃**这件（连试到上限仍失败），不再要它了 —— 但请留档。

    ε-7（§6.7 P2-2）：此前这种情况前端发的是 ack，而 ack 的语义是「已成功载入」，
    于是「放弃」被系统记成「送达成功」：件销毁、无记录、CMV 侧无从得知。
    现在件不再重投（与 ack 一样终结该件），但会进 PULL_DROPPED 账本，由
    /api/bridge/status 交给 CMV ⇒ 用户**能知道**有件没进去，也就能重新发送。

    与 ack 的区别只在「留不留账」——刻意保留 ack 不动：它仍是「成功」的唯一语义。
    claim（P0-9）：迟到回执不得留下「已放弃」的假账（那会让用户以为件没进去，
    而实际上新一轮投递已经成功载入了）。
    """
    try:
        s = int(seq)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad seq"}
    rec = None
    stale = False
    with PULL_LOCK:
        cur = PULL_INFLIGHT.get(s)
        if _claim_ok(cur, claim):
            rec = PULL_INFLIGHT.pop(s, None)
        elif cur is not None:
            stale = True
        if not stale:
            nm = name or (rec["item"].get("name") if rec else "") or ""
            PULL_DROPPED.append({"seq": s, "name": nm,
                                 "reason": (reason or "max-tries")[:120],
                                 "t": time.time()})
            del PULL_DROPPED[:-PULL_DROPPED_KEEP]
    if stale:
        log(f"[bridge] 忽略迟到的 drop #{s}（不是当前这一轮投递，不留账）")
        return {"ok": True, "seq": s, "dropped": False, "name": name or ""}
    log(f"[bridge] 已放弃 #{s} {nm}（{reason or 'max-tries'}）—— 记入放弃账本")
    return {"ok": True, "seq": s, "dropped": bool(rec), "name": nm}


def bridge_dropped(limit=PULL_DROPPED_KEEP) -> list:
    """最近的「放弃」记录（新的在后），供前端提示用户「有件没进去」。"""
    now = time.time()
    with PULL_LOCK:
        out = list(PULL_DROPPED[-limit:])
    return [{"seq": d["seq"], "name": d["name"], "reason": d["reason"],
             "ago": round(max(0.0, now - d["t"]), 1)} for d in out]


def bridge_tabs() -> list:
    """最近 BRIDGE_TTL 秒内活跃的**标签页**（ε-7 / §6.7 P2-3）。

    与 bridge_pages() 的区别：那个按端口（用于「有没有活页面」的判定），
    这个按标签页（用于「同一端口上有几个页面」）。同一端口的两个标签页在
    bridge_pages() 里只有一项，在这里是两项。
    """
    now = time.time()
    with PULL_LOCK:
        return [{"tab": t, "port": r.get("port") or "",
                 "ago": round(max(0.0, now - r.get("t", 0.0)), 1)}
                for t, r in sorted(BRIDGE_TABS.items()) if now - r.get("t", 0.0) <= BRIDGE_TTL]


def bridge_release(want: str) -> dict:
    """页面**主动放弃**：把挂在这个端口上的件全部放回队列。

    P1-32：桥接脚本单次取件有 10s 超时，超时会 abort —— 那一刻它还没拿到响应体，
    也就**拿不到 seq**，既不能 ack 也不能 nack。这是这套两阶段取件的唯一缺口：
    缺的不是「记得回执」，而是「回执需要的信息在请求失败时必然没有」。
    所以改成按端口放弃：件挂在谁名下，谁的页面就能一句话收回。
    """
    if not want:
        return {"ok": False, "error": "no port"}
    back = []
    with PULL_LOCK:
        for seq, rec in list(PULL_INFLIGHT.items()):
            if str(rec.get("port") or "") == want:
                _it = PULL_INFLIGHT.pop(seq)["item"]
                _it.pop("not_before", None)   # ε-7：页面主动放弃取件 ⇒ 立刻回队列
                back.append(_it)
        if back:
            back.sort(key=lambda x: x.get("seq") or 0)
            for it in reversed(back):
                PULL_QUEUE.insert(0, it)
            if len(PULL_QUEUE) > PULL_KEEP:
                del PULL_QUEUE[:-PULL_KEEP]
            log(f"[bridge] 页面 {want} 主动放弃取件，放回队列 {[x.get('seq') for x in back]}")
    return {"ok": True, "port": want, "released": len(back),
            "seqs": [x.get("seq") for x in back]}


def bridge_touch(port: str, tab: str = ""):
    """记录一次页面心跳（页面每次取件都算），用于前端判断「有没有活着的桥接页面」。

    ε-7：额外按 tabId 记一份（见 BRIDGE_TABS）。tab 为空时只更新端口账 —— 老版本
    脚本不带 tab，行为与改动前完全一致（向后兼容）。
    """
    now = time.time()
    with PULL_LOCK:
        BRIDGE_SEEN[port] = now
        if tab:
            BRIDGE_TABS[tab] = {"port": port, "t": now}
            if len(BRIDGE_TABS) > BRIDGE_TAB_KEEP:
                # 按最近活跃时刻裁掉最老的，而不是按插入顺序（开开关关也不失控）
                old = sorted(BRIDGE_TABS.items(), key=lambda kv: kv[1].get("t", 0.0))
                for k, _r in old[:len(BRIDGE_TABS) - BRIDGE_TAB_KEEP]:
                    BRIDGE_TABS.pop(k, None)


def bridge_pages() -> list:
    """最近 BRIDGE_TTL 秒内活跃的桥接页面端口。"""
    now = time.time()
    with PULL_LOCK:
        return sorted(p for p, t in BRIDGE_SEEN.items() if now - t <= BRIDGE_TTL)


def bridge_script_src() -> Path:
    """桥接脚本源文件（随前端静态资源一起分发，所以也跟着前端一起热更）。"""
    return frontend_dir() / "bridge" / "comfy-cmv-bridge.js"


def bridge_installed():
    """已安装到 ComfyUI 时返回目标路径，否则 None。"""
    root = comfy_root()
    if not root:
        return None
    dst = root / "custom_nodes" / BRIDGE_NODE / "web" / bridge_script_src().name
    return str(dst) if dst.is_file() else None


def bridge_stale() -> bool:
    """已装的桥接脚本与当前版本内容不一致 = 有更新没重装。

    为什么必须检测：ComfyUI 只在启动时扫描一次 custom_nodes，装过的脚本不会自动跟随
    ComfyMediaViewer 升级。改了桥接行为却让用户继续跑旧脚本，等于修复没送达。
    """
    dst = bridge_installed()
    if not dst:
        return False
    try:
        src = bridge_script_src()
        return (not src.is_file()) or (Path(dst).read_bytes() != src.read_bytes())
    except OSError:
        return False


def bridge_active():
    """ComfyUI 是否**已经加载**了桥接扩展（= 页面打开就会执行它）。

    判据是 ComfyUI 启动时生成的 /extensions 清单。扩展写进 custom_nodes 后
    **必须重启 ComfyUI** 才会进这份清单，因此三种状态要分清：

        True  —— 已生效：用户页面一打开/刷新就会上报心跳
        False —— 已写盘但没重启，页面根本拿不到脚本（最容易误解的状态）
        None  —— ComfyUI 不可达，无法判定

    这里只发只读的 GET /extensions。注意：**绝不能去碰带副作用的端点**
    （ComfyUI-Manager 的 /api/manager/reboot 就是 GET 即生效的，探一下
    就会把用户的 ComfyUI 关掉）。
    """
    if bridge_installed() is None:          # 没装 → 谈不上“已加载”，也不必打扰 ComfyUI
        return False
    try:
        with _OPENER_NO_PROXY.open(COMFY_BASE + "/extensions", timeout=1.5) as r:
            names = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None
    if not isinstance(names, list):
        return None
    tag = "/extensions/" + BRIDGE_NODE + "/"
    return any(tag.lower() in str(n).lower() for n in names)


def bridge_install() -> dict:
    """把桥接脚本装进 ComfyUI 的 custom_nodes（重启 ComfyUI 后自动加载）。"""
    root = comfy_root()
    if not root:
        return {"ok": False,
                "error": "未找到 ComfyUI 安装目录（可设 COMFYUI_ROOT 环境变量指定）"}
    src = bridge_script_src()
    if not src.is_file():
        return {"ok": False, "error": "桥接脚本资源缺失: " + str(src)}
    pkg = root / "custom_nodes" / BRIDGE_NODE
    try:
        (pkg / "web").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(src), str(pkg / "web" / src.name))
        (pkg / "__init__.py").write_text(BRIDGE_INIT, encoding="utf-8")
    except OSError as ex:
        return {"ok": False, "error": f"写入 ComfyUI 目录失败: {ex}"}
    log(f"[bridge] 已安装桥接脚本 → {pkg}")
    return {"ok": True, "path": str(pkg),
            "url": f"/extensions/{BRIDGE_NODE}/{src.name}"}


def app_dir() -> Path:
    """源码运行 = 脚本目录；PyInstaller 运行 = 解包资源目录(_MEIPASS)。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).parent


def frontend_dir() -> Path:
    """前端静态资源目录：**热更新目录优先**，其次才是内置那份。

    这是整个热更新机制唯一必须动的地方 —— 页面入口、/css /js /assets /bridge
    全部经由它取文件，所以只要这一处认了 hotfix，前端就整体可热更。

    判据是 index.html 是否存在，而不是目录是否存在：解压到一半、或用户手动删了
    主页面时，宁可回落到内置版本也不要送出一个空白页面。
    """
    if _hotfix_active():
        return FRONTEND_HOTFIX_DIR
    if USE_NEXT_FRONTEND and not getattr(sys, "frozen", False):
        return app_dir() / "frontend-next"
    return app_dir() / "frontend"


def frontend_version() -> str:
    """当前**实际生效**的前端版本：热更生效时取记录值，否则与程序同版本。"""
    if _hotfix_active():
        return str(load_applied().get("frontend_version") or APP_VERSION)
    return APP_VERSION


STATIC_TYPES = {".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
                ".svg": "image/svg+xml", ".png": "image/png",
                ".ico": "image/x-icon", ".woff2": "font/woff2",
                ".map": "application/json; charset=utf-8"}


def page_file() -> Path:
    """页面入口：模块化后是 frontend/index.html；保留旧的根目录 index.html 兜底。"""
    p = frontend_dir() / "index.html"
    return p if p.is_file() else (app_dir() / "index.html")


def frontend_file(rel: str):
    """把 /css/x.css、/js/x.js 映射到 frontend_dir() 下的文件；越界或不存在返回 None。

    /bridge/ 是回传·拉取模式的桥接脚本（给 ComfyUI 页面取用，不在本页面加载）。
    """
    rel = urllib.parse.unquote(rel).lstrip("/")
    if not re.fullmatch(r"(?:css|js|assets|bridge)/[A-Za-z0-9_.\-]+", rel):
        return None
    base = frontend_dir().resolve()
    try:
        p = (base / rel).resolve()
    except OSError:
        return None
    if p != base and base not in p.parents:          # 防目录穿越
        return None
    return p if p.is_file() else None


_THUMB_WARNED = set()          # §6.1：同一张图只记一次，滚屏/批量时别把日志刷满


def image_content_version(p: Path) -> str:
    """强内容版本；只在图片实际显示/打开时读取，不在目录列表全量计算。"""
    digest = hashlib.sha256()
    with p.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256-" + digest.hexdigest()


from backend.storage.work_limit import WorkLimit, WorkBusy
from backend.storage.response_cache import ResponseCache
META_RESPONSE_CACHE = ResponseCache()
THUMB_WORK = WorkLimit(2)
from backend.storage.single_flight import SingleFlight
THUMB_FLIGHT = SingleFlight(2)
from backend.storage.playback_sessions import PlaybackSessions, PlaybackExpired
import atexit
PLAYBACK_SESSIONS = PlaybackSessions()
atexit.register(PLAYBACK_SESSIONS.clear)


def thumb_for(p: Path, size: int = 320, frozen=None):
    with THUMB_WORK.enter():
        return _thumb_for(p, size=size, frozen=frozen)


def targeted_thumb(p, size, root_path, media_id=None, expected_version=None):
    from backend.storage.media_stream import verified_stream, MediaStreamConflict
    # Admit before either hashing pass or spooling. The same slot covers decode.
    key = (str(root_path), os.path.abspath(p), size, media_id, expected_version)
    with THUMB_FLIGHT.enter(key), THUMB_WORK.enter():
        target = _SQL_CATALOG.verify(root_path, p)
        if ((media_id and media_id != target["mediaId"]) or
                (expected_version and expected_version != "rev:" + target["revisionId"])):
            raise MediaStreamConflict("缩略图目标已变化")
        # A completed thumbnail is an immutable derivative of the verified hash.
        # Keep strict source verification and selected-version checks above, but
        # avoid copying/hashing the source again when that exact derivative exists.
        cached = _thumbnail_cache_path(p, "sha256-" + target["contentHash"], size)
        if cached.is_file():
            return cached
        with verified_stream(p, target["contentHash"]) as frozen:
            return _thumb_for(p, size=size, frozen=frozen)


def _thumbnail_cache_path(p, version, size):
    key = hashlib.sha1(f"{p}|{version}|{size}|jpeg-82-v1".encode("utf-8")).hexdigest()[:20] + ".jpg"
    return THUMB_DIR / key


def _thumb_for(p: Path, size: int = 320, frozen=None):
    """生成/命中磁盘缓存缩略图，失败返回 None（前端回退到原图）。"""
    if not HAS_PIL or p.suffix.lower() not in IMAGE_EXTS:
        return None
    try:
        # 同路径、同时间戳、同大小覆盖时仍须重做正在显示的缩略图。
        version = frozen[2] if frozen is not None else image_content_version(p)
        out = _thumbnail_cache_path(p, version, size)
        if out.exists():
            return out
        from PIL import ImageOps
        with Image.open(frozen[0] if frozen is not None else p) as im:
            im = ImageOps.exif_transpose(im)   # 按 EXIF 方向转正，竖拍缩略图不再躺倒
            if im.mode != "RGB":
                im = im.convert("RGB")         # RGBA/LA/P 统一转 RGB：JPEG 不支持 alpha
            im.thumbnail((size, size))
            THUMB_DIR.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(f".{_tmp_tag()}.tmp")
            im.save(tmp, "JPEG", quality=82)
            tmp.replace(out)
        return out
    except Exception as ex:
        # §6.1：以前这里是「吞掉一切、零痕迹」—— 缩略图**生成失败**与**本来就没有缩略图**
        # 在日志、界面、API 三处都不可区分，用户只看到「这张图显示得慢/是糊的」。
        # 返回值语义保持 None（前端照旧回退原图），但必须留痕，且按名字限流一次。
        key = "%s@%d" % (p.name, size)
        if key not in _THUMB_WARNED and len(_THUMB_WARNED) < 500:
            _THUMB_WARNED.add(key)
            log(f"[warn] 缩略图生成失败 {p.name}: {type(ex).__name__}: {ex}")
        return None


# ---------------------------------------------------------------- prompt index

IDX_FILE = DATA_DIR / "prompt_index.json"
IDX_IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp")   # 参与提示词索引的格式
PARSER_VER = 10         # 解析逻辑变更时 +1，使旧缓存整体失效重建
                        # 10：提示词不再混入 <lora:...>；不跨正负侧；ZeroOut 侧为空；新增工作流类型 wfk
REINDEX_INTERVAL = 30   # 索引完成后距下次允许增量刷新的最小秒数（出图后 30s 内可搜到新图）
# P0-1：单次索引扫描允许的目录读取失败上限。超过即认定「盘读不到」而不是「图没了」，
# 丢弃本代扫描结果、保留旧索引。30 是个宽松值：正常库偶尔一两个子目录被独占不算事，
# 而「外接盘掉线」会让**所有**子目录一起失败，远超此数。
INDEX_SCAN_FAIL_MAX = 30
IDX = {"root": None, "map": {}, "done": 0, "total": 0, "building": False,
       "built_at": 0.0, "stale": False, "stale_reason": ""}
IDX_LOCK = threading.Lock()
IDX_THREAD = [None]
# P1-15：索引「代际」令牌。force_reindex 会把 IDX_THREAD[0] 置 None 好让
# ensure_index 起新线程，但**旧线程并不会因此停下** —— 于是两个 _index_builder
# 同时在跑，各自把 IDX["map"] 整体替换，后收工的那个用更旧的扫描结果盖掉新的，
# 「重建」看起来点了没反应（索引停在旧结果）。做法：每次重建/换根都给 gen+1，
# 线程只在「自己这一代仍是最新」时才允许写回 IDX / 落盘，否则静默丢弃结果。
IDX_GEN = [0]


def _wf_class(meta: dict, parsed: dict) -> str:
    """工作流分类：full=有完整 UI 工作流 / prompt=只有提示词参数 / none=无生成参数。"""
    raw = meta.get("workflow")
    if isinstance(raw, str) and raw.strip():
        return "full"
    return "prompt" if parsed else "none"


def _legacy_metadata(p):
    return png_meta(p) if p.suffix.lower() == '.png' else (parse_jpeg_meta(p) if HAS_PIL else {})


def _metadata_digest(meta):
    return hashlib.sha256(json.dumps(meta,sort_keys=True,ensure_ascii=False,default=str).encode('utf-8')).hexdigest()


def _index_entry(p: Path, st: os.stat_result, prepared_meta=None) -> dict:
    """解析单个图片的提示词摘要，用于搜索。"""
    meta = prepared_meta if prepared_meta is not None else _legacy_metadata(p)
    parsed = parse_meta(meta) or {}

    def _s(v):                      # 索引字段统一为字符串，防异常值污染搜索与统计聚合
        return v if isinstance(v, str) else ""

    return {
        "mtime": int(st.st_mtime), "mtime_ns": st.st_mtime_ns, "ctime_ns": st.st_ctime_ns,
        "size": st.st_size, "file_id": [st.st_dev, st.st_ino], "metadata_hash": _metadata_digest(meta),
        "pos": _s(parsed.get("positive")),
        "neg": _s(parsed.get("negative")),
        "model": _s(parsed.get("model")),
        "loras": "; ".join(str(x) for x in (parsed.get("loras") or [])),
        "sampler": _s(parsed.get("sampler")),
        "dim": _s((meta.get("IHDR") or "").replace(" ", "")),
        "wfc": _wf_class(meta, parsed),     # 工作流分类（full/prompt/none）
        "wfk": _s(parsed.get("wfk")),       # 工作流类型（t2i/i2i/cn）
    }


def _idx_entry_ok(v) -> bool:
    """索引缓存条目的有效性判定（B4-A4 · CMV3-LOCAL-FIX，从旧工程回植 · P1，2026-10-09）。

    必须是 dict，且搜索/统计消费的必填键都在、类型对得上：`search_index`
    拼 hay 时直接取 `e["pos"]`，`stats_view` 取 `e.get("wfc")`，坏条目会让
    **每秒轮询**的 `/api/stats` 持续 500。缺一条即判无效 —— 丢掉的条目重扫
    会补回，留着的坏条目会经 old → newmap → 落盘**永久留档**。
    """
    if not isinstance(v, dict):
        return False
    for k in ("pos", "neg", "model", "loras"):
        if not isinstance(v.get(k), str):
            return False
    return isinstance(v.get("mtime"), int) and isinstance(v.get("size"), int)


def _save_idx_cache(map_obj=None):
    """落盘索引缓存；map_obj 传入时用它（构建中的新表），否则用当前已发布的表。"""
    try:
        IDX_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = IDX_FILE.with_suffix(f".{_tmp_tag()}.tmp")
        tmp.write_text(json.dumps({"root": IDX["root"], "pver": PARSER_VER,
                                   "map": IDX["map"] if map_obj is None else map_obj},
                                  ensure_ascii=False), "utf-8")
        tmp.replace(IDX_FILE)
    except OSError as ex:
        # §6.1：此前完全静默。索引缓存丢了本身不致命（下次会重建），
        # 但「磁盘写不进去」这件事必须能在日志里看见 —— 它往往先于其它写失败出现。
        log(f"[warn] 索引缓存落盘失败: {type(ex).__name__}: {ex}")


def _index_builder(root: str):
    """后台构建/增量校验索引；根目录切换或重建时自行中止。

    P1-15：进来先认领一个**代际号** mygen。之后每次要写回 IDX / 落盘前都比一次
    `IDX_GEN[0] == mygen` —— 只要期间有人发起过重建或换根（代际+1），这一代线程
    的全部产出即作废，避免旧扫描覆盖新结果。
    """
    myroot = root
    with IDX_LOCK:
        mygen = IDX_GEN[0]
    try:
        rootp = Path(root)
        with IDX_LOCK:
            if IDX_GEN[0] != mygen:              # 还没开工就被更新的一代取代了
                return
            same_root = IDX["root"] == root
            old = dict(IDX["map"]) if same_root else {}
            if IDX_FILE.is_file():
                try:
                    cache = json.loads(IDX_FILE.read_text("utf-8"))
                    if (cache.get("root") == root
                            and cache.get("pver") == PARSER_VER):
                        # B4-A5（CMV3-LOCAL-FIX，从旧工程回植）：缓存是「用户能改的
                        # 文件」，唯一的装载口必须过滤。一条非 dict / 缺键的条目一旦
                        # 进了 old → newmap → 又被写回磁盘，就**永久留档**：搜索侧
                        # e["pos"] 抛 KeyError/TypeError，统计侧 e.get("wfc") 对 str
                        # 抛 AttributeError ⇒ 前端每秒轮询的 /api/stats 持续 500。
                        # 丢弃即触发重扫补回，无副作用。
                        old.update({k: v for k, v in (cache.get("map") or {}).items()
                                    if _idx_entry_ok(v)})
                except (OSError, ValueError, AttributeError, TypeError):
                    pass
            # 增量校验期间继续沿用旧索引（新表建好再整体替换）：
            # 否则每 30 秒一次的增量校验会短暂清空索引，期间提示词搜索返回 0 条、
            # 左栏「工作流」数量也会掉到 0。
            IDX.update({"root": root, "map": old if same_root else {},
                        "done": 0, "total": 0, "building": True})
        imgs = []
        _stack = [(str(rootp), 0)]
        _visited = {os.path.realpath(str(rootp))}
        # P0-1（2026-10-08 全代码审计）：遍历失败必须计数，不能一律 `continue` 吞掉。
        # 外接盘（G:）休眠 / 掉线 / USB 瞬断时 root 的 scandir 会抛 OSError，旧代码
        # 静默跳过 ⇒ imgs 保持为空 ⇒ 下面把**空表发布并落盘**，真实库 20.6MB 的
        # prompt_index.json 当场被替换成空表，界面还显示「已建好」，用户完全无法
        # 与「图片被删光」区分，且需重新全库解析才能恢复。
        # 现在：root 自身读不到 = 硬失败（直接丢弃本代结果）；子目录失败也计数，
        # 超阈值同样丢弃 —— 宁可保留旧索引（stale），绝不把「读不到」当成「没有」。
        _scan_fail = 0
        _root_fail = False
        while _stack:
            _cur, _depth = _stack.pop()
            try:
                with os.scandir(_cur) as it:
                    for e in it:
                        try:
                            if e.is_dir():
                                if _depth < 10:
                                    rp = os.path.realpath(e.path)
                                    if rp not in _visited:
                                        _visited.add(rp)
                                        _stack.append((e.path, _depth + 1))
                            elif os.path.splitext(e.name)[1].lower() in IDX_IMG_EXTS:
                                imgs.append(Path(e.path))
                        except OSError:
                            _scan_fail += 1
                            continue
            except OSError as ex:
                _scan_fail += 1
                if os.path.normcase(os.path.realpath(_cur)) == os.path.normcase(os.path.realpath(str(rootp))):
                    _root_fail = True
                log(f"[warn] 索引遍历失败 {_cur}: {type(ex).__name__}: {ex}")
                continue
        if _root_fail or _scan_fail > INDEX_SCAN_FAIL_MAX:
            # 保留旧索引（含 IDX["map"] 与磁盘缓存），只落 building 旗标并留痕。
            # 不发布、不落盘 —— 这是本函数唯一「不产出」的出口。
            with IDX_LOCK:
                if IDX_GEN[0] != mygen:          # 已被新代取代：什么都别动
                    return
                IDX["building"] = False
                IDX["built_at"] = time.time()    # 算是「刚校验过」，避免 30 秒后再撞一次
                IDX["stale"] = True
                IDX["stale_reason"] = ("根目录不可读" if _root_fail
                                       else f"{_scan_fail} 个子目录读取失败")
                IDX["total"] = len(IDX["map"])   # 进度条别停在半路
                IDX["done"] = len(IDX["map"])
            log(f"[error] 索引构建中止：{'根目录不可读' if _root_fail else f'遍历失败 {_scan_fail} 次'}"
                f" —— 保留旧索引（{len(old)} 条），未覆盖磁盘缓存")
            return
        with IDX_LOCK:
            IDX["total"] = len(imgs)
            IDX["stale"] = False
            IDX["stale_reason"] = ""
        newmap = {}
        changed = False                           # 有新增/重解析才落盘，避免增量校验空转写盘
        for i, p in enumerate(imgs):
            if STATE.get("root") != myroot:      # 用户已切换目录：清场后放弃
                with IDX_LOCK:
                    if IDX_GEN[0] == mygen:      # 只由「仍是最新一代」的线程清场，别把新线程的地盘掀了
                        IDX.update({"root": None, "map": {}, "done": 0,
                                    "total": 0, "building": False})
                return
            with IDX_LOCK:
                if IDX_GEN[0] != mygen:          # P1-15：已被重建/换根取代 → 放弃，不写回
                    return
            try:
                st = p.stat()
                key = str(p)
                e = old.get(key)
                material = _legacy_metadata(p)
                if e and e.get("metadata_hash") == _metadata_digest(material) and e.get("mtime_ns") == st.st_mtime_ns and e.get("ctime_ns") == st.st_ctime_ns and e.get("file_id") == [st.st_dev, st.st_ino] and e.get("size") == st.st_size:
                    newmap[key] = e              # 未变更，复用旧条目
                else:
                    newmap[key] = _index_entry(p, st, material)
                    changed = True
            except Exception as ex:            # 单张坏图（畸形 PNG / 权限）不得拖垮整批索引
                log(f"[warn] 索引跳过 {p.name}: {type(ex).__name__}: {ex}")
                continue
            with IDX_LOCK:
                if IDX_GEN[0] != mygen:          # 进度也别再往新版的地盘上写
                    return
                IDX["done"] = i + 1              # 只更新进度；索引表等建完再整体替换
            if changed and (i + 1) % 300 == 0:
                _save_idx_cache(newmap)          # 阶段性落盘（无变更不写盘，避免写放大）
        # P0-1：最后一道闸 —— 旧表非空、新表为空 ⇒ 几乎只能是「盘读不到」而不是
        # 「图真的被删光了」。此时**拒绝发布、拒绝落盘**，保留旧索引（上一步已置 stale）。
        if not newmap and old:
            with IDX_LOCK:
                if IDX_GEN[0] != mygen:
                    return
                IDX["building"] = False
                IDX["built_at"] = time.time()
                IDX["stale"] = True
                IDX["stale_reason"] = "新扫描结果为空，疑似根目录不可读，已保留旧索引"
                IDX["total"] = len(IDX["map"])
                IDX["done"] = len(IDX["map"])
            log(f"[error] 索引重建结果为空（旧索引 {len(old)} 条）—— 拒绝覆盖，保持旧索引")
            return
        with IDX_LOCK:
            if IDX_GEN[0] != mygen:              # P1-15：收工前最后一道闸 —— 旧代结果禁止覆盖新索引
                return
            IDX["map"] = newmap                  # 整体替换：期间旧索引一直可用
            IDX["building"] = False
            IDX["built_at"] = time.time()
        if changed or len(newmap) != len(old):   # 有新增/改动/删除才落盘
            _save_idx_cache(newmap)
    except Exception:
        with IDX_LOCK:
            if IDX_GEN[0] == mygen:              # 只有当前代才该把 building 落下来
                IDX["building"] = False


def ensure_index():
    """搜索时调用：索引缺失或根目录不匹配则启动后台构建。

    索引已存在时按 REINDEX_INTERVAL 节流做增量刷新——_index_builder
    按 mtime+size 复用旧条目，重跑只会重新 stat 校验并解析新增/变更
    文件，因此出图后不必重启进程也能搜索到新图。
    """
    now = time.time()
    root = STATE.get("root")
    if not root:
        return
    with IDX_LOCK:
        t = IDX_THREAD[0]
        if t and t.is_alive():
            return
        if (IDX["root"] == root and IDX.get("built_at")
                and now - IDX["built_at"] < REINDEX_INTERVAL):
            return                      # 索引已建成（含 0 结果）且在节流窗口内
        # B4-A1（CMV3-LOCAL-FIX，从旧工程回植）：登记与启动必须在同一临界区。
        # `Thread.is_alive()` 对「已登记但还没 start()」的线程返回 **False** —— 若把
        # start() 放到锁外，并发调用 B 会看到未启动的 T1 而误判「没有线程在跑」，
        # 再建一条 T2 并覆盖登记；两条 _index_builder 领到同一个 mygen（代际闸形同
        # 虚设），全库的元数据被解析两遍、外接盘 I/O 与进度计数各走一套。
        #
        # 旗标也必须由**调用方线程同步**置起：`IDX["building"] = True` 原先只写在
        # _index_builder 里 ⇒ 本函数返回的那一刻它还是上一轮的 False。而 /api/search
        # 正是带着这个旗标回话的（它只调 ensure_index() 就立刻 search_index()）——
        # 冷启动后的第一次搜索于是自称「没在建索引」并返回 0 条：前端把「正在建」
        # 显示成「没搜到」，测试侧「等 building 落下来」的循环也会当场跳出。
        # 实测 20/20 轮复现（旧工程 dev/_idx_building_probe.py）。
        IDX.update({"done": 0, "total": 0, "building": True})
        t = threading.Thread(target=_index_builder, args=(root,), daemon=True)
        IDX_THREAD[0] = t
        t.start()


def _wait_index(timeout: float = 5.0):
    """筛选面板用：确保提示词索引可用（已就绪立即返回；正在建则最多等 timeout 秒）。

    索引建好后 meta_batch 可以整批命中内存，不必逐张读文件；磁盘有缓存时
    重建通常 1~2 秒内完成（只做 mtime+size 增量校验）。
    """
    root = STATE.get("root")
    if not root:
        return
    ensure_index()
    t0 = time.time()
    while time.time() - t0 < timeout:
        with IDX_LOCK:
            if IDX["root"] == root and not IDX["building"] and IDX["map"]:
                return
        time.sleep(0.1)


def search_index(q: str, base_dir: str = "") -> dict:
    with IDX_LOCK:
        building = IDX["building"]
        done, total = IDX["done"], IDX["total"]
        snap = list(IDX["map"].items())
    terms = q.lower().split()              # 多关键词 AND：全部命中才算
    prefix = ""
    if base_dir:
        prefix = base_dir.rstrip("\\/").lower() + os.sep   # 限定当前目录（含子目录）
    favs = load_favs()
    tgs = load_tags()
    _nctx = nsfw_ctx()          # 一份快照整个循环复用：每张都取锁/读盘会明显拖慢搜索
    results = []
    for path, e in snap:
        if prefix and not path.lower().startswith(prefix):
            continue
        hay = "\n".join((e["pos"], e["neg"], e["model"], e["loras"]))
        low = hay.lower()
        pos0, ok = None, True
        for t in terms:
            i = low.find(t)
            if i < 0:
                ok = False
                break
            if pos0 is None:
                pos0 = i
        if not ok:
            continue
        i = pos0 or 0
        snip = hay[max(0, i - 40):i + 80].replace("\n", " ").strip()  # 保留原始大小写
        _ext = os.path.splitext(path)[1].lower()
        _typ = "video" if _ext in VIDEO_EXTS else "image"
        results.append({
            "name": os.path.basename(path), "path": path,
            "type": _typ, "size": e["size"], "mtime": e["mtime"],
            "playable": _typ == "image" or _ext in PLAYABLE_VIDEO,
            "png": _ext == ".png", "fav": path in favs,
            "favcat": favs[path]["cat"] if path in favs else None,
            "tags": sorted(tgs.get(path, [])),      # 搜索语法 tag: 需要（提示词模式同样生效）
            "snippet": snip,
        })
    # 批次 F · F4（CMV3-LOCAL-FIX 回植）：**先排序切片、再算 nsfw**。
    # 排序键是 mtime，与 n/ns 无关 ⇒ 换序不改变结果集与顺序。
    # 改前是「给全部命中算 nsfw → 再切 500」：命中 3244 条时白算 2744 次（≈290 ms）。
    results.sort(key=lambda x: -x["mtime"])
    top = results[:500]
    for r in top:
        r["n"], r["ns"] = nsfw_fields(r["path"], r["mtime"], r["size"], _nctx)
    return {"building": building, "done": done, "total": total,
            "results": top}


# ---------------------------------------------------------------- NSFW 内容检测（V2.11.0）
# 分数在服务端算、落盘缓存、由列表接口一次性带出 —— 不给每张卡片单开请求。
# 这是整个功能的性能红线：一屏 100 张卡片多 100 个请求，页面直接废掉。
#
# 两张表性质完全不同，别混：
#   nsfw_index/       派生数据。随便删、随便重建，不影响任何用户内容。
#   nsfw_manual.json  用户数据。人工纠正过的判定，重建索引**不能**动它。
#
# 与提示词索引刻意分成两条线程：索引要在 1~2 秒内可用，推理慢一个数量级，
# 混在一条线程里会把提示词搜索一起拖住。

# V2.11.3：缓存目录化 —— 一个根目录一份文件（见 _nsfw_cache_path）。
# 以前是单个 nsfw_index.json 只记得住「最后扫的那个根」，用户在多根目录间
# 来回切就来回作废、切回去就得重扫。人工纠正表不受影响，仍是全局一份。
NSFW_DIR = DATA_DIR / "nsfw_index"
NSFW_LEGACY_FILE = DATA_DIR / "nsfw_index.json"   # V2.11.2 及以前的单文件，迁移后留 .bak
NSFW_MANUAL_FILE = DATA_DIR / "nsfw_manual.json"
NSFW_MODELS_DIR = DATA_DIR / "models" / "nsfw"     # 精确档等额外模型的落点
NSFW_IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
NSFW_DEFAULT_THRESHOLD = 0.60                      # score >= 它算 NSFW
NSFW_THRESHOLD_RANGE = (0.05, 0.95)
NSFW_AUTO_INTERVAL = 60.0     # 扫过一遍之后，增量补测的最小间隔（出图后最多 1 分钟能被认出）

NSFW = {"root": None, "map": {}, "done": 0, "total": 0,
        "building": False, "built_at": 0.0, "model": "",
        # B4-A8（CMV3-LOCAL-FIX）：上一次扫描的失败原因（全失败 / 部分失败），
        # 由 nsfw_status 带出去 —— 否则「跑完了但零结果」用户完全无从察觉。
        "last_error": ""}
NSFW_LOCK = threading.Lock()
NSFW_THREAD = [None]
# B4-A7：单张推理失败逐条记日志的上限（坏图批量出现时不刷屏，其余交给收工汇总）。
_NSFW_SKIP_LOG_MAX = 5
_NSFW_MANUAL = {"at": -1.0, "map": {}}      # 按文件 mtime 缓存，别每个请求都读一次盘
_NSFW_MANUAL_LOCK = threading.Lock()

# ---- 根目录变更迁移（V2.12.0）----
# 换根（移动/改名/换盘符）后，所有按绝对路径存的东西集体失配：NSFW 缓存文件名是
# md5(根)（_nsfw_cache_path），map 键与人工纠正/收藏/标签/标记/最近的键全是绝对路径。
# 数据其实都还在盘上，只是键对不上了 —— 这里做一次「指纹验证 + 前缀重写」把它们接回家。
NSFW_MIGRATE_RATIO = 0.60     # 指纹命中率门槛（实测定的默认值）
NSFW_MIGRATE_MIN_HITS = 10    # 命中数下限：防「碰巧相似」的库被误迁；小库按比例放宽
NSFW_MIGRATE_SAMPLE = 500     # 指纹抽样上限：打分只看这么多张，大库不必全比
_MIGRATE_LOCK = threading.Lock()
_MIGRATE_STATE = {"running": False, "done": None, "last_try": 0.0}
# done   : 最近一次迁移的报告 dict（前端经 /api/migrate_report 取走即清，一次迁移只提示一次）
# last_try: 自动检测的节流戳 —— stats 是秒级轮询，没有它「索引没建好等下一拍」会变成
#           每秒一次的全目录扫描

# 把「数据目录下的模型目录」也纳入查找（设置页下载的精确档落在这里）。
# 随包分发的 models/nsfw 由 nsfw.py 自己按程序目录 / PyInstaller 解包目录去找 ——
# 这个项目要给别人用，模型必须跟着包走，不能依赖联网下载。
NSFW_ENGINE.configure([NSFW_MODELS_DIR])


def nsfw_tiers() -> tuple:
    """可用档位 id。从引擎的登记表取，免得两边各写一份枚举、早晚对不上。"""
    m = getattr(NSFW_ENGINE, "MODELS", None)
    return tuple(m.keys()) if m else ("light",)


def nsfw_available() -> bool:
    return bool(NSFW_ENGINE and NSFW_ENGINE.available())


def nsfw_reason() -> str:
    """不可用的原因原文。要能在界面上显示出来，而不是只躺在日志里。"""
    return "" if not NSFW_ENGINE else NSFW_ENGINE.unavailable_reason()


def load_nsfw_model() -> str:
    """当前档位。非法值或模型文件被删了一律回落默认档 —— 设置里存着 precise
    但文件不在了，也不能让整个功能崩掉。"""
    want = str(load_settings().get("nsfw_model") or "").strip()
    tiers = nsfw_tiers()
    try:
        if want in tiers and NSFW_ENGINE.find_model(want)[0]:
            return want
    except Exception:
        pass
    return tiers[0] if tiers else "light"


def valid_nsfw_model(mid: str) -> bool:
    """档位合法性（与 save_nsfw_model 同口径）。

    B9-P1-2（2026-10-10 · CMV3-LOCAL-FIX，从旧工程回植）：独立出来是为了让
    `/api/prefs` 能**先全量校验、后统一写盘** —— 否则一次请求里排在非法项
    前面的字段已经落盘了（见该端点的注释）。
    """
    return str(mid) in nsfw_tiers()


def save_nsfw_model(mid: str) -> bool:
    if not valid_nsfw_model(mid):
        return False
    with SETTINGS_LOCK:
        s = load_settings()
        s["nsfw_model"] = mid
        return save_settings(s)


_NSFW_THR = {"at": -1.0, "v": NSFW_DEFAULT_THRESHOLD}


def load_nsfw_threshold() -> float:
    """阈值判定会在「每一条媒体」上被调用一次（收藏夹一屏就是几百条），
    所以这里按 settings.json 的 mtime 缓存 —— 只 stat 一次，不 read+parse。
    写入方（save_nsfw_threshold）会把 at 置 -1 强制重读，不会读到旧值。"""
    try:
        at = SETTINGS_FILE.stat().st_mtime
    except OSError:
        at = -1.0
    if _NSFW_THR["at"] == at:
        return _NSFW_THR["v"]
    try:
        v = float(load_settings().get("nsfw_threshold", NSFW_DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        v = NSFW_DEFAULT_THRESHOLD
    lo, hi = NSFW_THRESHOLD_RANGE
    v = max(lo, min(hi, v))
    _NSFW_THR["at"], _NSFW_THR["v"] = at, v
    return v


def valid_nsfw_threshold(v) -> bool:
    """阈值合法性（与 save_nsfw_threshold 同口径）。

    B9-P1-2（2026-10-10 · CMV3-LOCAL-FIX，从旧工程回植）：供 `/api/prefs`
    两趟校验用 —— 校验必须能独立于写盘调用。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    lo, hi = NSFW_THRESHOLD_RANGE
    return lo <= f <= hi


def save_nsfw_threshold(v) -> bool:
    """阈值存服务端而不是前端 localStorage：判定发生在服务端，阈值是判定的一部分。
    前端自己记一份的话，切换浏览器 / 桥接脚本读到的就是两个不同的判定标准
    （delete_mode 已经踩过这个坑，注释里还留着）。"""
    if not valid_nsfw_threshold(v):
        return False
    f = float(v)
    with SETTINGS_LOCK:
        s = load_settings()
        s["nsfw_threshold"] = round(f, 3)
        ok = save_settings(s)
    _NSFW_THR["at"] = -1.0            # 强制下次重读，避免停在旧值上
    return ok


def nsfw_enabled() -> bool:
    """总开关。关掉后列表不带判定、界面上的三态控件一并停用。"""
    try:
        on = bool(load_settings().get("nsfw_enabled", True))
    except Exception:
        on = True
    # Revision-bound manual decisions remain usable without an inference model.
    return on and (_SQL_SAFETY is not None or nsfw_available())


def save_nsfw_enabled(on: bool) -> bool:
    with SETTINGS_LOCK:
        s = load_settings()
        s["nsfw_enabled"] = bool(on)
        return save_settings(s)


# ---- 人工纠正表 ----

def load_nsfw_manual() -> dict:
    """{路径: "nsfw"|"sfw"}。按文件 mtime 缓存，避免每个请求都读盘。"""
    try:
        at = NSFW_MANUAL_FILE.stat().st_mtime
    except OSError:
        at = -1.0
    with _NSFW_MANUAL_LOCK:
        if _NSFW_MANUAL["at"] == at:
            return _NSFW_MANUAL["map"]
    m = {}
    if at >= 0:
        try:
            d = json.loads(NSFW_MANUAL_FILE.read_text("utf-8"))
            if isinstance(d, dict):
                for k, v in d.items():
                    if v in ("nsfw", "sfw"):
                        m[str(k)] = v
        except (OSError, ValueError):
            m = {}
    with _NSFW_MANUAL_LOCK:
        _NSFW_MANUAL["at"], _NSFW_MANUAL["map"] = at, m
    return m


def set_nsfw_manual(path: str, label, request_identity=None) -> dict:
    """写人工纠正；label 传 None = 撤销纠正、回到模型判定。

    这个文件是用户资产，写坏了没处恢复 —— 先写临时文件再原子换名，全程串行。
    """
    try:
        p = str(Path(path).resolve())
    except Exception:
        return {"ok": False, "error": "路径非法"}
    if _SQL_SAFETY is not None:
        if label not in (None, "nsfw", "sfw"):
            return {"ok": False, "error": "label 只能是 nsfw / sfw / null"}
        verified = _SQL_CATALOG.verify(STATE["root"], p)
        supplied = request_identity or {}
        from backend.storage.legacy_adapter import IdentityConflictError
        if supplied.get("mediaId") and supplied["mediaId"] != verified["mediaId"]:
            raise IdentityConflictError("mediaId 与当前路径不一致")
        if supplied.get("expectedVersion") and supplied["expectedVersion"] not in (
                verified["contentVersion"], 'rev:' + verified['revisionId']):
            raise IdentityConflictError("内容版本已变化，不能保存旧版本人工判定")
        if label is None:
            _SQL_SAFETY.clear_manual(verified["libraryId"], verified["mediaId"], verified["revisionId"])
        else:
            _SQL_SAFETY.set_manual(verified["libraryId"], verified["mediaId"], verified["revisionId"],
                                   label, verified_revision=verified)
        return {"ok": True, "path": p, "label": label, "revisionId": verified["revisionId"]}
    cur = load_nsfw_manual()               # 先在外面读（它自己取锁，重入会死锁）
    d = dict(cur)
    if label is None:
        d.pop(p, None)
    elif label in ("nsfw", "sfw"):
        d[p] = label
    else:
        return {"ok": False, "error": "label 只能是 nsfw / sfw / null"}
    try:
        NSFW_MANUAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = NSFW_MANUAL_FILE.with_suffix(f".{_tmp_tag()}.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False), "utf-8")
        tmp.replace(NSFW_MANUAL_FILE)
    except OSError as ex:
        return {"ok": False, "error": f"写入失败：{type(ex).__name__}: {ex}"}
    with _NSFW_MANUAL_LOCK:
        _NSFW_MANUAL["at"], _NSFW_MANUAL["map"] = -1.0, d   # -1 强制下次重读，拿到新 mtime
    return {"ok": True, "path": p, "label": d.get(p)}


def nsfw_manual_clear() -> dict:
    """清空人工纠正。会提示一句后果：清完这些图回到模型判定，
    下次检测可能被重新标成 NSFW —— 这是用户点按钮时该知道的。"""
    if _SQL_SAFETY is not None:
        count = _SQL_SAFETY.clear_all_manual(_SQL_ORG.library_uuid)
        return {"ok": True, "count": count}
    n = len(load_nsfw_manual())
    try:
        NSFW_MANUAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = NSFW_MANUAL_FILE.with_suffix(f".{_tmp_tag()}.tmp")
        tmp.write_text("{}", "utf-8")
        tmp.replace(NSFW_MANUAL_FILE)
    except OSError as ex:
        return {"ok": False, "error": f"写入失败：{type(ex).__name__}: {ex}"}
    with _NSFW_MANUAL_LOCK:
        _NSFW_MANUAL["at"], _NSFW_MANUAL["map"] = -1.0, {}
    return {"ok": True, "cleared": n}


# ---- 派生缓存（V2.11.3：按根目录分文件共存）----
# 旧版是单个 nsfw_index.json，只记得住「最后扫的那个根目录」——用户在多个根
# 目录之间频繁切换时，每切一次整表作废，切回来就得重扫。现在一个根目录一份
# 文件（文件名 = 规范化路径的 md5 前 16 位），来回切各自的热缓存都还在。

def _nsfw_cache_path(root: str) -> Path:
    """根目录 → 它自己的缓存文件。拿 md5 做文件名而不是直接拼路径：
    Windows 路径里的冒号反斜杠当不了文件名；且同一目录的不同写法（大小写、
    结尾斜杠）应得到同一份缓存，所以哈希前先 normcase+normpath。"""
    norm = os.path.normcase(os.path.normpath(root))
    return NSFW_DIR / (hashlib.md5(norm.encode("utf-8", "replace")).hexdigest()[:16]
                       + ".json")


def _nsfw_migrate_legacy():
    """旧版单文件缓存 → 按根目录分文件。幂等：迁移完旧文件改名 .bak 留底，
    下次进来第一行就返回。任何失败都不抛 —— 迁移失败顶多损失一次重扫，
    不能挡住服务启动（「不能 500、不能崩」同款思路）。"""
    tmp = None                  # 迁移写一半失败时要能清掉半截 tmp（并发输家走的正是这条路）
    try:
        if not NSFW_LEGACY_FILE.is_file():
            return
        d = json.loads(NSFW_LEGACY_FILE.read_text("utf-8"))
        if isinstance(d, dict):
            root = d.get("root")
            if isinstance(root, str) and root and isinstance(d.get("map"), dict):
                dst = _nsfw_cache_path(root)
                if not dst.is_file():        # 新文件已存在（同根重扫过）：以新的为准
                    NSFW_DIR.mkdir(parents=True, exist_ok=True)
                    payload = dict(d)
                    payload["v"] = 1
                    tmp = dst.with_suffix(f".{_tmp_tag()}.tmp")
                    tmp.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
                    tmp.replace(dst)
                    log(f"[nsfw] 旧版单文件缓存已迁移 -> {dst.name}")
        NSFW_LEGACY_FILE.replace(NSFW_LEGACY_FILE.parent / "nsfw_index.json.bak")
    except (OSError, ValueError) as ex:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
        log(f"[warn] 旧 NSFW 缓存迁移跳过: {ex}")


def _load_nsfw_cache(root: str, model: str):
    """返回 (map, built_at)。只读自己这根目录的文件；版本/根/档位对不上就整表
    作废 —— 换了模型档，旧分数和新分数没有可比性，混在一起会得到自相矛盾的判定。"""
    try:
        d = json.loads(_nsfw_cache_path(root).read_text("utf-8"))
    except (OSError, ValueError):
        return {}, 0.0
    if not isinstance(d, dict) or d.get("v") != 1:
        return {}, 0.0
    # 根目录比对走 normcase：Windows 下大小写、斜杠写法差异不该作废整表
    if os.path.normcase(str(d.get("root") or "")) != os.path.normcase(root) \
            or d.get("model") != model:
        return {}, 0.0
    m = d.get("map")
    if isinstance(m, dict):
        # 缓存是"用户能改的文件"，整条坏掉（手改/写崩成字符串、列表等）不能带进内存
        # 炸请求 —— 在唯一的装载口把非 dict 条目丢掉，重扫会补回来。
        # 审查实测：一条 "garbage" 就能让 /api/list 和 /api/nsfw/status 双 500，
        # 而 status 是前端每秒轮询的接口。
        m = {k: v for k, v in m.items() if isinstance(v, dict)}
    else:
        m = {}
    try:
        at = float(d.get("at") or 0.0)
    except (TypeError, ValueError):
        at = 0.0
    return m, at


def _save_nsfw_cache(mp: dict, root: str, model: str):
    """原子换名落盘，只写 root 自己的文件。失败只记日志 —— 派生数据，
    丢了重扫一遍就行，不该让用户看到报错。

    mp/root/model 由调用方显式传入，不从 NSFW[] 现读：扫描线程收尾时用户可能
    已经切走，按 NSFW 现值落盘会把 A 根目录的分数写进 B 的文件里（本版修的竞态）。
    """
    tmp = None                                   # 先占位：失败分支要清 tmp，不能引 NameError
    try:
        NSFW_DIR.mkdir(parents=True, exist_ok=True)
        dst = _nsfw_cache_path(root)
        tmp = dst.with_suffix(f".{_tmp_tag()}.tmp")
        tmp.write_text(json.dumps({"v": 1, "root": root, "model": model,
                                   "at": time.time(), "map": mp},
                                  ensure_ascii=False), "utf-8")
        # Windows 上 `os.replace` = MoveFileEx(REPLACE_EXISTING)：**只要还有别的句柄开在
        # dst 上就会失败（WinError 5 拒绝访问）**，而被开着的概率并不小 ——
        # `/api/nsfw/status` 是前端每秒轮询的，它的 nsfw_prime → _load_nsfw_cache 读的
        # 就是这个文件。实测（dev/_nsfw_flag_order_probe.py Part 5）在一次运行里就撞到
        # 两次：「[warn] NSFW 缓存写入失败: [WinError 5] ... 64be007ba60a0c27.tmp ->
        # ...64be007ba60a0c27.json」—— 整批扫描结果**一条都没落盘**，只留一行日志，
        # 用户第二天打开发现「昨天扫的都白扫了」。派生数据丢了顶多重扫，但这个重扫
        # 本来是能免掉的，所以给 3 次机会、递增退避，等读者先松手。
        last = None
        for _try in range(3):
            try:
                tmp.replace(dst)
                last = None
                break
            except OSError as ex:
                last = ex
                time.sleep(0.05 * (_try + 1))
        if last is not None:
            raise last
    except OSError as ex:
        if tmp is not None:
            try:
                tmp.unlink()                     # 换名输了（并发写/进程被杀）别留半截 tmp 在缓存目录
            except OSError:
                pass
        log(f"[warn] NSFW 缓存写入失败: {ex}")


def nsfw_prime(root: str = None):
    """把该根目录自己的缓存读进内存（切根目录时调）。只读，不触发任何推理。"""
    _nsfw_migrate_legacy()                      # 幂等，首次之后立刻返回
    root = root or STATE.get("root")
    if not root:
        return
    model = load_nsfw_model()
    mp, at = _load_nsfw_cache(root, model)
    with NSFW_LOCK:
        NSFW.update({"root": root, "model": model, "map": mp,
                     "done": len(mp), "total": len(mp),
                     "building": False, "built_at": at,
                     # B4-A10d：换根读缓存 = 上一轮的失败原因不再适用，必须清掉 ——
                     # 否则新根明明扫得好好的，界面却一直挂着上一条假错。
                     "last_error": ""})


def nsfw_ctx():
    """一次请求取一份快照，循环里反复用。

    返回 `(entries, model, threshold, manual, enabled)`。

    `enabled`（总开关）一并算进来是**性能红线**（批次 F · F1，CMV3-LOCAL-FIX 回植）：
    `nsfw_fields()` 在逐条循环里会被调用上万次，此前它每次都自己调
    `nsfw_enabled()` → `nsfw_available()` + `load_settings()`，一次 `/api/list`
    就是 9726 次读盘，端到端从 97 ms 涨到 2.6 s。
    总开关在**一次请求内翻转**是不可能场景，所以整个循环共用一份快照是安全的。
    """
    enabled = nsfw_enabled()
    if _SQL_SAFETY is not None:
        model, threshold = load_nsfw_model(), load_nsfw_threshold()
        return (_SQL_SAFETY.current_snapshot(_SQL_ORG.library_uuid, model_id=model, threshold=threshold),
                model, threshold, {}, enabled)
    with NSFW_LOCK:
        entries, model = NSFW["map"], NSFW["model"]
    if not model:
        # 首次请求：先把缓存与档位装进来（纯读，不触发推理）。
        # 装完 NSFW["model"] 就有值了，后续请求走上面那条零成本路径。
        nsfw_prime()
        with NSFW_LOCK:
            entries, model = NSFW["map"], NSFW["model"]
    return entries, model, load_nsfw_threshold(), load_nsfw_manual(), enabled


def nsfw_fields(path, mtime, size, ctx=None, identity=None):
    """返回 (label, score)。

    score == -1 是「这条没有可用分数，请以 label 为准」的信号，涵盖四种情况：
    还没检测 / 检测失败 / 文件被改过 / **人工纠正**。
    前端据此决定能不能自己按阈值重算：有分数 → 阈值滑块改完立刻生效（不必重扫、不必重拉列表）；
    没分数 → 乖乖用服务端给的标签。**人工纠正必须走这条路** ——
    否则前端拿阈值一算，就能把用户明确标注过的判定翻回去。
    """
    if ctx is None:
        ctx = nsfw_ctx()
    # 严格 5 元解包（批次 F · F1，CMV3-LOCAL-FIX 回植）：总开关走 ctx，
    # **不在这里**调 nsfw_enabled()。不做「兼容 4 元组」的兜底 —— 那会让
    # 「ctx 忘了带 enabled」静默退回每张读盘的慢路径（2.6 s 那个坑），
    # 宁可当场炸出来。
    entries, model, thr, manual, enabled = ctx
    if not enabled:
        return "unknown", -1.0
    if _SQL_SAFETY is not None:
        identity = identity or _SQL_CATALOG.current(path)
        if identity is None or identity["revisionId"] is None:
            return "unknown", -1.0
        effective = entries.get(str(path))
        if effective is None or effective["revisionId"] != identity["revisionId"]:
            return "unknown", -1.0
        return effective["label"], effective["score"] if effective["score"] is not None else -1.0
    m = manual.get(str(path))
    if m in ("nsfw", "sfw"):
        return m, -1.0
    e = entries.get(str(path))
    if not isinstance(e, dict) or mtime is None:   # 类型判断而非 if not e：非空 str/list 也拦得住
        return "unknown", -1.0
    try:
        if int(e.get("m", -1)) != int(mtime) or int(e.get("s", -1)) != int(size if size is not None else -1):
            return "unknown", -1.0
        if e.get("md") != model:
            return "unknown", -1.0         # 换了模型档位：旧分数没有可比性
        sc = float(e.get("sc", -1.0))
    except (TypeError, ValueError):
        return "unknown", -1.0
    return ("nsfw" if sc >= thr else "sfw"), round(sc, 6)


def nsfw_label_of(path, mtime, size, ctx=None) -> str:
    """返回 "nsfw" | "sfw" | "unknown"。三档判定，顺序不能换：

    1. 人工纠正 —— 最高优先。用户说是什么就是什么，重扫也翻不回去。
    2. 模型分数 —— 但要 mtime+size+档位三者全对得上，否则旧分作废。
    3. 其余一律 unknown（还没测 / 测失败 / 文件变了）。

    为什么用 unknown 兜底而不是 sfw：把「没测过」当「安全」，
    会在用户眼里表现成「这功能好像没用」—— 其实他只是还没点开始检测。
    """
    return nsfw_fields(path, mtime, size, ctx)[0]


def _nsfw_builder(root: str, model: str):
    """后台检测线程：遍历 → 增量复用 → 推理 → 落盘。根目录切换时自行中止。"""
    myroot = root
    # 扫描是低频、用户显式触发的后台任务，起止都留一行：出了「扫了但没结果」
    # 这类问题，没有这两行就只能靠猜（这次就吃过亏——中止分支静默退场，查了半天）。
    log(f"[nsfw] 扫描开始 root={myroot} model={model}")
    try:
        old, _at = _load_nsfw_cache(root, model)
        imgs = []
        stack, seen = [(str(Path(root)), 0)], set()
        try:
            seen.add(os.path.realpath(root))
        except OSError:
            pass
        while stack:
            cur, depth = stack.pop()
            try:
                with os.scandir(cur) as it:
                    for e in it:
                        try:
                            if e.is_dir():
                                if depth < 10:
                                    rp = os.path.realpath(e.path)
                                    if rp not in seen:
                                        seen.add(rp)
                                        stack.append((e.path, depth + 1))
                            elif os.path.splitext(e.name)[1].lower() in NSFW_IMG_EXTS:
                                imgs.append(e.path)
                        except OSError:
                            continue
            except OSError:
                continue
        with NSFW_LOCK:
            NSFW["total"] = len(imgs)
        log(f"[nsfw] 待检 {len(imgs)} 张")
        # B4-A6（CMV3-LOCAL-FIX，从旧工程回植）：预热返回 False = 会话根本没建起来
        # （模型损坏 / ort 建会话失败 / 依赖缺失）。接着空转全库只会白等十几分钟，
        # 最后交一份空表还报「完成」—— 当场收工，如实回报。
        if not NSFW_ENGINE.warm(model):
            _err = (NSFW_ENGINE.last_load_error(model)
                    or "检测引擎未能加载模型")
            log(f"[warn] NSFW 中止：模型加载失败 —— {_err}")
            with NSFW_LOCK:
                NSFW.update({"map": {}, "done": 0, "total": 0,
                             "building": False, "last_error": f"模型加载失败：{_err}"})
            return
        newmap, changed = {}, False
        skipped = 0                              # B4-A7：predict 返回 None 的条数（坏图 / 会话半死）
        for i, p in enumerate(imgs):
            if STATE.get("root") != myroot:      # 用户切了根目录：清场放弃
                log(f"[nsfw] 中止：根目录已切换（STATE={STATE.get('root')} myroot={myroot}）")
                if newmap:
                    # V2.11.3：已扫到的部分落进自己根目录的文件，下次接着用。
                    # 表按根分文件之后这一步才安全 —— 不会污染别的根目录。
                    # ⚠️ 同收尾处：先落盘再置 building=False（宣告完成不许抢在落盘前面）。
                    _save_nsfw_cache(newmap, myroot, model)
                with NSFW_LOCK:
                    NSFW.update({"root": None, "map": {}, "done": 0,
                                 "total": 0, "building": False})
                return
            try:
                st = os.stat(p)
                key = str(p)
                e = old.get(key)
                if (_SQL_SAFETY is None and e and int(e.get("m", -1)) == int(st.st_mtime)
                        and int(e.get("s", -1)) == st.st_size
                        and e.get("md") == model):
                    newmap[key] = e              # 没变且同档位：直接复用，不重跑推理
                else:
                    before = _SQL_CATALOG.verify(myroot, p) if _SQL_SAFETY is not None else None
                    r = NSFW_ENGINE.predict(p, model)
                    if r is None:
                        # B4-A7：坏图/解码失败跳过（读时自然落成 unknown），但**必须留痕** ——
                        # 整批零结果时，这里是唯一能事后归因的地方。逐条只在头几张打，
                        # 之后交给收工汇总，避免坏图批量出现时刷屏。
                        skipped += 1
                        if skipped <= _NSFW_SKIP_LOG_MAX:
                            log(f"[warn] NSFW 推理返回空 #{skipped} "
                                f"{p}（{NSFW_ENGINE.last_load_error(model) or '该图解码失败'}）")
                        continue
                    if _SQL_SAFETY is not None:
                        after = _SQL_CATALOG.verify(myroot, p)
                        if after["revisionId"] != before["revisionId"]:
                            continue
                        _SQL_SAFETY.set_model_score(after["libraryId"], after["mediaId"], after["revisionId"],
                                                   r["sc"], model, metadata={"top": r["top"]})
                    newmap[key] = {"m": int(st.st_mtime), "s": st.st_size, "md": model,
                                   "sc": r["sc"], "tp": r["top"]}
                    changed = True
            except Exception as ex:              # 单张出错不得拖垮整批（_index_builder 同款兜底）
                log(f"[warn] NSFW 跳过 {os.path.basename(p)}: {type(ex).__name__}: {ex}")
                continue
            with NSFW_LOCK:
                NSFW["done"] = i + 1
            # 每 500 张把表露出去一次：进度条、以及「边扫边出结果」都靠它。
            # 公布的是**部分表**（还没轮到的读成 unknown）而不是旧表 —— 诚实一点，
            # 反正单张 2 ms，全库也就十几秒，没必要为这点时间差维护两套表。
            if changed and (i + 1) % 500 == 0:
                with NSFW_LOCK:
                    NSFW["map"] = dict(newmap)
        # B4-A8（CMV3-LOCAL-FIX）：「跑完但零结果」是这条链路最坏的形态：进度照走满、
        # 状态报「完成」，用户以为保护在生效。按失败率如实留痕，交给 nsfw_status 带出去。
        _lerr = ""
        if skipped and not newmap:
            _lerr = (f"全部 {skipped} 张推理失败（模型不可用或图片解码异常），"
                     f"本次未产出任何结果")
            log(f"[warn] NSFW 扫描结束但零结果：{_lerr}")
        elif skipped:
            _lerr = f"{skipped} 张推理失败已跳过（共成功 {len(newmap)} 张）"
            log(f"[warn] NSFW 扫描完成：{len(newmap)} 张成功，{_lerr}")
        # ⚠️ 落盘的顺序有讲究：**先落盘、后宣告「收工」**（`building=False`）。
        # 反过来写会让「宣告完成」与「文件可用」之间留一段窗口 —— 消费者在窗口里读到
        # 的是「扫完了，可它的派生缓存还读不到」（旧工程 tests/nsfw_check.py H 段实测翻红）。
        # 注意 `NSFW["map"]` 与旗标一起后移：对外可见的结果与「已完成」保持同一时点。
        # 显式带 myroot/model：此刻用户可能已切走，绝不能按 NSFW 现值落盘
        _save_nsfw_cache(newmap, myroot, model)
        with NSFW_LOCK:
            NSFW["map"] = newmap
            NSFW["built_at"] = time.time()
            NSFW["last_error"] = _lerr
            NSFW["building"] = False
        log(f"[nsfw] 扫描完成 {len(newmap)} 张")
    except Exception as ex:
        log(f"[warn] NSFW 扫描异常: {type(ex).__name__}: {ex}")
        with NSFW_LOCK:
            NSFW["building"] = False
            # B4-A8：整批异常也要留痕（与单张失败分开表述）
            NSFW["last_error"] = f"扫描异常：{type(ex).__name__}: {ex}"


def ensure_nsfw(auto_only: bool = True) -> bool:
    """启动后台检测。

    auto_only=True（默认，挂在既有轮询里）只在「这个根目录已经扫过」时做增量补测 ——
    出图后不用手动点，但首次打开软件**绝不会**把 CPU 打满。全库首扫必须由用户点。
    """
    root = STATE.get("root")
    if not root or not nsfw_enabled():
        return False
    now = time.time()
    model = load_nsfw_model()
    with NSFW_LOCK:
        t = NSFW_THREAD[0]
        if t and t.is_alive():
            return False
        if NSFW["root"] != root:
            return False                          # 换过根目录：等 nsfw_status 先 prime
        if not NSFW.get("built_at"):
            return False                          # 从没扫过：等用户点「开始检测」
        if now - NSFW["built_at"] < NSFW_AUTO_INTERVAL:
            return False
        NSFW.update({"model": model, "done": 0, "total": 0, "building": True})
        t = threading.Thread(target=_nsfw_builder, args=(root, model), daemon=True)
        NSFW_THREAD[0] = t
        t.start()                                 # B4-A2（CMV3-LOCAL-FIX）：起线程也须在锁内
    return True


def nsfw_start(force: bool = False) -> dict:
    """用户点「开始检测 / 重新检测」。force=True 先丢掉派生缓存（人工纠正不动）。"""
    if not nsfw_available():
        return {"ok": False, "error": nsfw_reason() or "检测引擎不可用"}
    root = STATE.get("root")
    if not root:
        return {"ok": False, "error": "尚未设置根目录"}
    with NSFW_LOCK:
        if NSFW["building"]:
            return {"ok": True, "building": True, "msg": "已在检测中"}
    cleared = nsfw_clear_cache() if force else 0
    model = load_nsfw_model()
    with NSFW_LOCK:
        # B4-A3（CMV3-LOCAL-FIX，从旧工程回植）：清缓存与载模型都在锁外，并发点两次
        # 可能双双穿过上面那道 building 预检；这里必须**再挡一次**，否则同一档位会各起
        # 一条推理线程、两条都写 NSFW["map"]。start() 同样必须在锁内
        # （`is_alive()` 对「已登记未启动」的线程返回 False）。
        t = NSFW_THREAD[0]
        if NSFW["building"] or (t and t.is_alive()):
            return {"ok": True, "building": True, "msg": "已在检测中"}
        NSFW.update({"root": root, "model": model, "done": 0, "total": 0,
                     "building": True, "map": {} if force else dict(NSFW["map"]),
                     "last_error": ""})
        t = threading.Thread(target=_nsfw_builder, args=(root, model), daemon=True)
        NSFW_THREAD[0] = t
        t.start()
    return {"ok": True, "building": True, "cleared": cleared, "model": model}


def nsfw_clear_cache() -> int:
    """丢弃当前根目录的派生缓存（人工纠正表不动，其他根目录的缓存也不动 ——
    V2.11.3 起按根分文件，「重新检测」清的是自己这份）。返回丢掉的条数。"""
    root = STATE.get("root")
    if not root:
        return 0
    f = _nsfw_cache_path(root)
    n = 0
    try:
        n = len(json.loads(f.read_text("utf-8")).get("map") or {})
    except (OSError, ValueError, AttributeError):
        n = 0
    try:
        f.unlink()
    except OSError:
        pass
    return n


# ---- 根目录变更迁移（V2.12.0）----
# 场景：D:\图库 被移动/改名/换了盘符。此时缓存文件名（md5(根)）变了、map 键和
# 人工纠正/收藏/标签/标记/最近的键全对不上 —— 数据都在盘上，只是找不回来。
# 思路：拿新根下的文件清单拼回旧路径查旧表，size 一致算指纹命中；命中够多就
# 认定「只是搬了家」，把六张表的键整体重写到新根。目录结构变过（命中率低）
# 不自动迁，走设置页手动兜底（用户指定旧根，跳过门槛、保留逐条验证）。


def _migrate_key_prefix(table: dict, old_root: str, new_root: str):
    """dict 形态路径表（manual/favs/tags/marks 共用）的前缀重写。
    逐条 isfile 验证：验证不过的条目**原样保留**而不是丢弃 —— 键迁错比键失配
    更糟（失配顶多显示不出来，迁错会张冠李戴）。返回 (新表, 迁移条数)。"""
    old_n, new_n = os.path.normpath(old_root), os.path.normpath(new_root)
    pre_old = os.path.normcase(old_n + os.sep)
    out, n = {}, 0
    for k, v in table.items():
        kn = os.path.normpath(str(k))
        if os.path.normcase(kn).startswith(pre_old):
            nk = new_n + os.sep + kn[len(old_n) + 1:]
            # 已有目标记录时保留用户在新根上做的修改，也保留旧键供日后核对。
            # 只迁移没有冲突的条目，避免 JSON 插入顺序决定谁覆盖谁。
            if os.path.isfile(nk) and nk not in table and nk not in out:
                out[nk] = v
                n += 1
                continue
        out[str(k)] = v
    return out, n


def _migrate_recent_prefix(items: list, old_root: str, new_root: str):
    """recent 表是 list（[{path,...}]），键在 value 里，单独一份重写。"""
    old_n, new_n = os.path.normpath(old_root), os.path.normpath(new_root)
    pre_old = os.path.normcase(old_n + os.sep)
    out, n = [], 0
    for e in items:
        p = str((e or {}).get("path") or "")
        kn = os.path.normpath(p)
        if p and os.path.normcase(kn).startswith(pre_old):
            nk = new_n + os.sep + kn[len(old_n) + 1:]
            if os.path.isfile(nk):
                e2 = dict(e)
                e2["path"] = nk
                out.append(e2)
                n += 1
                continue
        out.append(e)
    return out, n


def _migrate_write_json(path: Path, obj) -> None:
    """迁移专用落盘：先把现文件留一份 .mig-bak 再原子覆盖。
    这些表全是用户资产（收藏/标签没有任何重建途径），bak 是最后的保险；
    bak 写不出来不拦迁移 —— 此刻原文件一个字节都还没动，失败可重跑（幂等）。"""
    try:
        if path.is_file():
            path.with_suffix(path.suffix + ".mig-bak").write_bytes(path.read_bytes())
    except OSError:
        pass
    if not _save_json(path, obj):
        raise WriteError(f"{path.name}写入失败（磁盘不可写或文件被占用）")


def _migrate_sample(new_root: str) -> list:
    """新根的指纹样本 [(相对路径, mtime, size)]。直接走文件系统而不是等提示词
    索引建好：索引比它慢一个数量级，迁移要赶在用户反应过来之前完成，等不起。"""
    out = []
    base = os.path.normpath(new_root)
    try:
        for cur, dirs, files in os.walk(base):
            dirs[:] = dirs[:64]          # 变态深的树抽样够用了，别为一两个环白烧时间
            for name in files:
                if os.path.splitext(name)[1].lower() in NSFW_IMG_EXTS:
                    p = os.path.join(cur, name)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    out.append((os.path.relpath(p, base), int(st.st_mtime), st.st_size))
                    if len(out) >= NSFW_MIGRATE_SAMPLE:
                        return out
    except OSError:
        pass
    return out


def _migrate_read_cache(root: str):
    """读某根的缓存文件，不管档位。迁移是搬运不是判定 —— 档位匹配是读端
    （_load_nsfw_cache）的职责，这里过滤会把「换根+换档」的用户挡在门外。"""
    try:
        d = json.loads(_nsfw_cache_path(root).read_text("utf-8"))
    except (OSError, ValueError):
        return {}, ""
    if not isinstance(d, dict) or d.get("v") != 1:
        return {}, ""
    m = d.get("map")
    return (m if isinstance(m, dict) else {}), str(d.get("model") or "")


def _migrate_score(sample: list, old_root: str, old_map: dict) -> int:
    """按「拼回旧路径后 size 一致」计命中。mtime 不参与计分：复制、网盘回迁都
    可能动 mtime，把它卡死会把最需要救援的用户挡在门外（迁移时会用 stat 对齐）。"""
    base = os.path.normpath(old_root)
    idx = {}
    for k in old_map:
        idx[os.path.normcase(os.path.normpath(str(k)))] = k
    hits = 0
    for rel, _mt, size in sample:
        e = old_map.get(idx.get(os.path.normcase(os.path.normpath(
            base + os.sep + str(rel))), ""))
        try:
            if isinstance(e, dict) and e.get("s") is not None and int(e["s"]) == size:
                hits += 1
        except (TypeError, ValueError):
            continue
    return hits


def _migrate_candidates(new_root: str):
    """候选旧根 = 数据目录里每个 NSFW 缓存文件的 root 字段（map 顺手带回：
    打分和迁移用同一份材料，不用读两遍）。当前根自己的文件不算候选。"""
    want = os.path.normcase(os.path.normpath(new_root))
    try:
        files = sorted(NSFW_DIR.glob("*.json"))
    except OSError:
        return
    for f in files:
        try:
            d = json.loads(f.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict) or d.get("v") != 1:
            continue
        r = str(d.get("root") or "")
        if not r or os.path.normcase(os.path.normpath(r)) == want:
            continue
        m = d.get("map")
        if isinstance(m, dict) and m:
            yield r, m, str(d.get("model") or "")


def _migrate_run(new_root: str, old_root: str = None, manual: bool = False) -> dict:
    """执行一次迁移。old_root 缺省时自动选候选（打分最高且过门槛）；
    manual=True 由设置页显式指定旧根 —— 跳过门槛（用户说迁就迁），验证照做。

    全程 copy 语义：所有旧文件一律不动，迁错了把目录改回去就复原。
    落盘顺序是刻意的：用户数据表先写，NSFW 缓存文件**最后**写 —— 它存在
    就代表迁移完整走完，自动检测靠这个判断「要不要再试」。
    """
    report = {"ok": False, "migrated": False, "new_root": new_root,
              "old_root": old_root or "", "cache": 0, "manual": 0,
              "favs": 0, "tags": 0, "marks": 0, "recent": 0}
    if _SQL_ORG is not None:
        report["error"] = "数据库模式需要按媒体身份核对移动，不能运行旧路径推断迁移"
        report["code"] = "IDENTITY_MIGRATION_REQUIRED"
        return report
    try:
        # ---- 自动模式：选候选 + 门槛（old_root 在这里才确定，路径规范化必须放在它后面）----
        old_map, old_model = {}, ""
        if not manual:
            sample = _migrate_sample(new_root)
            if not sample:
                report["error"] = "新目录里没有可核对的图片"
                return report
            best = None
            for cand_root, cand_map, cand_model in _migrate_candidates(new_root):
                h = _migrate_score(sample, cand_root, cand_map)
                if best is None or h > best[0]:
                    best = (h, cand_root, cand_map, cand_model)
            if best is None:
                report["error"] = "没有可用的旧缓存"
                return report
            h, cand_root, cand_map, cand_model = best
            # 小库按比例放宽下限：总共 8 张的库不可能凑够 10 个命中
            need = max(1, min(NSFW_MIGRATE_MIN_HITS,
                              int(len(sample) * NSFW_MIGRATE_RATIO)))
            if h < need or h / len(sample) < NSFW_MIGRATE_RATIO:
                # 明确告诉调用方「不是搬家」——自动入口据此记住不再重试
                report["reason"] = f"指纹命中率不足（{h}/{len(sample)}，需 {need}）"
                report["ok"] = True
                return report
            old_root, old_map, old_model = cand_root, cand_map, cand_model
        else:
            old_map, old_model = _migrate_read_cache(old_root)
        report["old_root"] = old_root

        old_n, new_n = os.path.normpath(old_root), os.path.normpath(new_root)
        if os.path.normcase(old_n) == os.path.normcase(new_n):
            report["error"] = "新旧路径相同，无需迁移"
            return report

        # ---- P2（V2.12.1）检查点①：切根即中止。用户切走 = 放弃这个根，别再动
        # 他的表。检查点必须压在任何写盘之前；这里过了之后到写缓存之间还有检查点②
        # 兜底。中止不记 skip（用户可能切回来，30s 后轮询会再试）。----
        if STATE.get("root") != new_root:
            report["aborted"] = True
            report["reason"] = "根目录已切换，迁移中止"
            report["ok"] = True
            return report

        # ---- ① 用户数据表：前缀重写（验证不过的条目原样保留）----
        # P1（V2.12.1）：快照→重写→落盘必须整段在同一把表锁里。旧写法快照在锁外、
        # 只给写盘上锁，3000 键的 isfile 循环把窗口拉到数百毫秒 —— 审计探针实测
        # 窗口内 40 次收藏 toggle 被 40/40 整表覆盖。锁内化后与 toggle_fav（同锁
        # 读写）串行，谁也不吞谁；load_* 本身不拿锁，锁内调用不会死锁。
        # manual 例外：load_nsfw_manual 内部自己拿 _NSFW_MANUAL_LOCK，锁内再调会
        # 递归死锁 —— 保持锁外快照（表小、写频极低，毫秒级窗口可接受）。
        man = load_nsfw_manual()
        man2, report["manual"] = _migrate_key_prefix(man, old_root, new_root)
        if report["manual"]:
            with _NSFW_MANUAL_LOCK:
                _migrate_write_json(NSFW_MANUAL_FILE, man2)
                _NSFW_MANUAL["at"] = -1.0          # 失效缓存，下个请求拿到新表

        with FAV_LOCK:
            favs2, report["favs"] = _migrate_key_prefix(load_favs(), old_root, new_root)
            if report["favs"]:
                _migrate_write_json(FAV_FILE, favs2)

        with TAGS_LOCK:
            tags2, report["tags"] = _migrate_key_prefix(load_tags(), old_root, new_root)
            if report["tags"]:
                _migrate_write_json(TAGS_FILE, tags2)

        with MARKS_LOCK:
            marks2, report["marks"] = _migrate_key_prefix(load_marks(), old_root, new_root)
            if report["marks"]:
                _migrate_write_json(MARKS_FILE, marks2)

        # recent 是易失性的使用记录（丢了无感），不为它上锁 —— 重写窗口内并发的
        # recent_add 可能被覆盖，可接受（V2.12.1 审计定级：不修）。
        rec, report["recent"] = _migrate_recent_prefix(load_recent(), old_root, new_root)
        if report["recent"]:
            _migrate_write_json(RECENT_FILE, rec)

        # ---- ② NSFW 派生缓存：键重写 + mtime 对齐（读端 nsfw_fields 卡 mtime）----
        # P2 检查点②：用户表已动、缓存还没写 —— 这中途切根就收手（已写的表不回滚，
        # copy 语义 + 30s 重试自愈）。P16：builder 正在扫就让路，它收尾会落一份
        # 全新的完整缓存，迁移半路插一份旧分数只会跟新结果打架。
        with NSFW_LOCK:
            _building = bool(NSFW["building"])
        if _building or STATE.get("root") != new_root:
            report["aborted"] = True
            report["reason"] = ("检测进行中，迁移只迁了用户数据表" if _building
                                else "根目录已切换，迁移中止")
            report["ok"] = True
            return report
        if old_map:
            pre_old = os.path.normcase(old_n + os.sep)
            new_map = {}
            for k, e in old_map.items():
                if not isinstance(e, dict):
                    continue
                kn = os.path.normpath(str(k))
                if not os.path.normcase(kn).startswith(pre_old):
                    continue                       # 别的根的条目：不掺和
                nk = new_n + os.sep + kn[len(old_n) + 1:]
                try:
                    st = os.stat(nk)               # isfile + 新 mtime 一次拿到
                except OSError:
                    continue                       # 新根下没这文件：留在旧文件里
                e2 = dict(e)
                e2["m"] = int(st.st_mtime)         # 复制/回迁会动 mtime：对齐新值，
                new_map[nk] = e2                   # 否则全表会被打成 unknown 白迁一趟
                report["cache"] += 1
            if report["cache"]:
                # model 沿用旧文件的档位：行为等同「同一份缓存换了个根」，
                # 之后换档作废走既有逻辑，迁移不掺和档位的事
                _save_nsfw_cache(new_map, new_root, old_model or load_nsfw_model())

        if not any(report[k] for k in ("cache", "manual", "favs", "tags", "marks", "recent")):
            report["reason"] = "没有可迁移的条目"
            report["ok"] = True
            return report
        report["ok"] = True
        report["migrated"] = True
        return report
    except Exception as ex:                        # 迁移失败不能 500：报告里如实带回
        report["error"] = f"{type(ex).__name__}: {ex}"
        return report


def _migrate_auto_worker(new_root: str):
    with _MIGRATE_LOCK:
        if _MIGRATE_STATE["running"]:
            return
        _MIGRATE_STATE["running"] = True
    try:
        r = _migrate_run(new_root, manual=False)
        if r.get("migrated"):
            with _MIGRATE_LOCK:
                _MIGRATE_STATE["done"] = r
            log(f"[migrate] 已从旧目录找回数据: {r}")
            # P13（V2.12.1）：迁移只写盘不刷内存 —— 内存那份是切根时 prime 的，
            # 那时候缓存文件还不存在，读到的必然是空表。不在这里补 prime，找回的
            # 分数要等用户再切一次根（或重启）才上屏。扫描进行中不刷：builder 收尾
            # 有自己的落盘节奏，prime 半路改 building/map 会打架（prime 与用户点
            # 扫描的极小竞态窗口最多导致重扫一遍，可接受）。
            with NSFW_LOCK:
                _building = bool(NSFW["building"])
            if not _building and STATE.get("root") == new_root:
                nsfw_prime(new_root)
        elif r.get("error"):
            log(f"[warn] 根目录迁移失败: {r.get('error')}")
        elif r.get("reason"):
            if r.get("aborted"):
                # 切根/检测中是暂态：不记 skip，用户切回来（或扫完）后下一拍就会再试
                log(f"[migrate] 迁移中止: {r.get('reason')}")
            else:
                # 命中率不足 = 真的是个新目录，不是搬家。记住这个根不再重试，
                # 否则 stats 每隔 30 秒就会白扫一遍文件系统。
                _MIGRATE_STATE["skip"] = os.path.normcase(os.path.normpath(new_root))
                log(f"[migrate] 跳过迁移: {r.get('reason')}")
    finally:
        with _MIGRATE_LOCK:
            _MIGRATE_STATE["running"] = False


def root_migrate_maybe():
    """stats 轮询每拍都调。发现「当前根没有缓存文件」才考虑迁移 —— 有缓存说明
    这个根路径没变过（或上次迁移已完整做完），一分钟节流防止把轮询变成扫描器。"""
    if _SQL_ORG is not None:
        return
    root = STATE.get("root")
    if not root or not NSFW_DIR.is_dir():
        return
    try:
        if _nsfw_cache_path(root).is_file():
            return
    except OSError:
        return
    if _MIGRATE_STATE.get("skip") == os.path.normcase(os.path.normpath(root)):
        return
    now = time.time()
    with _MIGRATE_LOCK:
        if _MIGRATE_STATE["running"]:
            return
        if now - _MIGRATE_STATE["last_try"] < 30.0:
            return
        _MIGRATE_STATE["last_try"] = now
    threading.Thread(target=_migrate_auto_worker, args=(root,), daemon=True).start()


def nsfw_status() -> dict:
    """设置面板与前端启动查询用。扫描进行中不统计 counts ——
    进度轮询每隔一秒来一次，全表遍历一次没必要。"""
    root = STATE.get("root")
    if _SQL_SAFETY is not None:
        entries, model, threshold, _, _enabled = nsfw_ctx()
        counts = {"nsfw": 0, "sfw": 0, "unknown": 0}
        manual = 0
        for path, entry in entries.items():
            try:
                Path(path).relative_to(Path(root))
            except (ValueError, TypeError):
                continue
            counts[entry["label"]] += 1
            manual += int(entry["origin"] == "manual")
        with NSFW_LOCK:
            building, done, total = bool(NSFW["building"]), int(NSFW["done"]), int(NSFW["total"])
        return {"available": nsfw_available(), "reason": nsfw_reason(), "enabled": nsfw_enabled(),
                "model": model, "threshold": threshold, "threshold_range": list(NSFW_THRESHOLD_RANGE),
                "tiers": NSFW_ENGINE.available_models() if NSFW_ENGINE else [],
                "models_dir": str(NSFW_MODELS_DIR), "building": building, "done": done, "total": total,
                "counts": counts, "manual": manual, "counts_partial": True,
                "coverage": "catalogued_current_revisions"}
    with NSFW_LOCK:
        building = bool(NSFW["building"])
        stale = (NSFW["root"] != root)
    if root and stale and not building:
        nsfw_prime(root)                        # 换过根目录：先把这份缓存读进来
    with NSFW_LOCK:
        building = bool(NSFW["building"])
        done, total = int(NSFW["done"]), int(NSFW["total"])
        model = NSFW["model"] or load_nsfw_model()
        entries = NSFW["map"]
        # B4-A9（CMV3-LOCAL-FIX）：把上一次扫描的失败原因带出去（空串=正常）
        last_error = NSFW.get("last_error", "")
    thr = load_nsfw_threshold()
    counts = {"nsfw": 0, "sfw": 0, "unknown": 0}
    if not building and entries:
        manual = load_nsfw_manual()
        seen = set()
        for k, e in entries.items():
            seen.add(k)
            lb = manual.get(k)
            if lb not in ("nsfw", "sfw"):
                if not isinstance(e, dict):
                    lb = "unknown"      # 坏条目按没分数算：这个接口前端每秒轮询，炸不得
                else:
                    try:
                        lb = "nsfw" if float(e.get("sc", -1.0)) >= thr else "sfw"
                    except (TypeError, ValueError):
                        lb = "unknown"
            counts[lb] = counts.get(lb, 0) + 1
        for k, lb in manual.items():
            if k not in seen:
                counts[lb] = counts.get(lb, 0) + 1
    tiers = []
    if NSFW_ENGINE:
        try:
            tiers = NSFW_ENGINE.available_models()
        except Exception:
            tiers = []
    return {
        "available": nsfw_available(),
        "reason": nsfw_reason(),
        "enabled": nsfw_enabled(),
        "model": model,
        "threshold": thr,
        "threshold_range": list(NSFW_THRESHOLD_RANGE),
        "tiers": tiers,
        "models_dir": str(NSFW_MODELS_DIR),
        "building": building,
        "done": done,
        "total": total,
        "counts": counts,
        "manual": len(load_nsfw_manual()),
        "last_error": last_error,   # B4-A9（CMV3-LOCAL-FIX）：模型加载失败 / 整批推理失败；空串=正常
    }


def strip_png_meta(data: bytes) -> bytes:
    """剔除 PNG 的元数据文本块（tEXt/zTXt/iTXt/eXIf/tIME），像素数据原样保留。

    纯字节级手术不重编码；任何结构异常时原样返回（fail-safe）。
    """
    try:
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            return data
        out = bytearray(data[:8])
        i = 8
        while i + 12 <= len(data):
            ln = int.from_bytes(data[i:i + 4], "big")
            ctype = data[i + 4:i + 8]
            if ln > len(data) - i - 12:
                return data                       # 长度异常，放弃剥离
            if ctype not in (b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"tIME"):
                out += data[i:i + 12 + ln]
            i += 12 + ln
            if ctype == b"IEND":
                return bytes(out)
        return data
    except Exception:
        return data


def strip_jpg_meta(data: bytes) -> bytes:
    """剔除 JPEG 的元数据段（APP1=EXIF/XMP、APP13=IPTC、COM 注释），像素原样保留。"""
    try:
        if data[:2] != b"\xff\xd8":
            return data
        out = bytearray(b"\xff\xd8")
        i = 2
        while i + 4 <= len(data):
            if data[i] != 0xFF:
                return data
            marker = data[i + 1]
            if marker == 0xD9:                    # EOI
                out += data[i:i + 2]
                return bytes(out)
            if marker == 0xDA:                    # SOS：之后是像素熵编码数据，原样保留
                out += data[i:]
                return bytes(out)
            ln = int.from_bytes(data[i + 2:i + 4], "big")
            if ln < 2 or i + 2 + ln > len(data):
                return data
            if marker not in (0xE1, 0xED, 0xFE):  # 丢弃 APP1 / APP13 / COM
                out += data[i:i + 2 + ln]
            i += 2 + ln
        return bytes(out)
    except Exception:
        return data


def strip_webp_meta(data: bytes) -> bytes:
    """剔除 WebP 的 EXIF/XMP 块并清掉 VP8X 对应标志位（RIFF 字节级手术，像素原样保留）。"""
    try:
        if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
            return data
        body = bytearray()
        i = 12
        while i + 8 <= len(data):
            ctype = data[i:i + 4]
            ln = int.from_bytes(data[i + 4:i + 8], "little")
            end = i + 8 + ln + (ln & 1)           # RIFF 块按 2 字节对齐
            if end > len(data):
                return data
            if ctype in (b"EXIF", b"XMP "):
                i = end                            # 整块丢弃
                continue
            if ctype == b"VP8X" and ln >= 1:
                flags = data[i + 8] & ~(0x08 | 0x04)   # 清 EXIF/XMP 标志位
                body += data[i:i + 8] + bytes([flags]) + data[i + 9:end]
            else:
                body += data[i:end]
            i = end
        out = bytearray(b"RIFF\x00\x00\x00\x00WEBP") + body
        out[4:8] = (len(out) - 8).to_bytes(4, "little")
        return bytes(out)
    except Exception:
        return data


def _mp4_boxes(data: bytes, start: int, end: int):
    """遍历 [start,end) 范围里的 box，产出 (box起, 头长, 类型, 载荷起, 载荷止)。"""
    i = start
    while i + 8 <= end:
        size = int.from_bytes(data[i:i + 4], "big")
        btype = data[i + 4:i + 8]
        head = 8
        if size == 1:                    # 64 位 largesize
            if i + 16 > end:
                return
            size = int.from_bytes(data[i + 8:i + 16], "big")
            head = 16
        elif size == 0:                  # 0 = 一直延伸到文件尾
            size = end - i
        if size < head or i + size > end:
            return
        yield i, head, btype, i + head, i + size
        i += size


def strip_mp4_meta(data: bytes) -> bytes:
    """清空 mp4 的元数据容器：moov 下的 udta / meta，以及顶层 uuid（XMP）。

    为什么是**等长清零**而不是删除：mp4 的 stco/co64 记录的是**绝对文件偏移**，
    删字节会让后面的 mdat 整体前移、偏移表全部失效（视频花屏/不播）。做法是把
    udta/meta 的 box 类型改写成 free 并清零载荷 —— 长度不变、偏移不变（播放零
    风险），元数据字节真实消失（不是隐藏：strings/binwalk 也捞不出来）。
    ComfyUI 视频工作流（VideoHelperSuite 等）经 ffmpeg 把提示词写进
    moov/udta/meta/ilst，这一刀正好覆盖。结构异常原样返回（fail-safe）。
    """
    try:
        if len(data) < 12 or data[4:8] != b"ftyp":
            return data
        out = bytearray(data)
        for off, head, btype, ps, pe in _mp4_boxes(data, 0, len(data)):
            if btype == b"moov":
                for o2, h2, t2, s2, e2 in _mp4_boxes(data, ps, pe):
                    if t2 in (b"udta", b"meta"):
                        out[o2 + 4:o2 + 8] = b"free"      # 解析器见 free 直接跳过
                        for k in range(s2, e2):
                            out[k] = 0                    # 载荷清零
            elif btype == b"uuid":                            # 顶层 XMP 常见载体
                for k in range(ps, pe):
                    out[k] = 0
        return bytes(out)
    except Exception:
        return data


def strip_gif_meta(data: bytes) -> bytes:
    """删掉 GIF 的注释扩展与 XMP 应用扩展。

    GIF 是流式块结构（没有绝对偏移表），删除扩展块不破坏解码 —— 比等长清零更
    干净（字节真的没了）。解码序列其余部分原样复制；遇到不认识的字节保守截断
    并返回已处理结果。结构异常原样返回（fail-safe）。
    """
    try:
        if len(data) < 13 or data[:6] not in (b"GIF87a", b"GIF89a"):
            return data
        n = len(data)
        out = bytearray(data[:13])                    # 头(6) + 逻辑屏幕描述符(7)
        i = 13
        if data[10] & 0x80:                           # 全局色表紧随其后
            g = 3 * (2 ** ((data[10] & 0x07) + 1))
            if i + g > n:
                return data
            out += data[i:i + g]
            i += g

        def _sub_end(pos):
            """子块链末尾（含终止 0x00）的下一个位置。"""
            while pos < n and data[pos]:
                pos += 1 + data[pos]
            return min(pos + 1, n)

        while i < n:
            b = data[i]
            if b == 0x3B:                             # 文件结束
                out += data[i:i + 1]
                break
            if b == 0x21 and i + 2 <= n:              # 扩展块
                label = data[i + 1]
                if label == 0xFF:                     # 应用扩展：11 字节固定头
                    if i + 3 > n:
                        break
                    j = i + 3 + data[i + 2]
                else:
                    j = i + 2
                j = _sub_end(j)
                if label == 0xFE or (label == 0xFF and
                                     data[i + 3:i + 14] == b"XMP DataXMP"):
                    i = j                             # 注释 / XMP：整块丢弃
                    continue
                out += data[i:j]
                i = j
                continue
            if b == 0x2C:                             # 图像块
                if i + 10 > n:
                    break
                j = i + 10                            # 0x2C + 9 字节描述符
                if data[i + 9] & 0x80:                # 局部色表
                    j += 3 * (2 ** ((data[i + 9] & 0x07) + 1))
                if j > n:
                    break
                j = _sub_end(j + 1)                   # LZW 最小码长 + 数据子块
                out += data[i:j]
                i = j
                continue
            break                                     # 未知字节：保守收手
        return bytes(out)
    except Exception:
        return data


def _unique_path(p: Path) -> Path:
    """目标已存在时依次加 _1/_2/… 后缀，保持原名优先。"""
    if not p.exists():
        return p
    stem, suf = p.stem, p.suffix
    for i in range(1, 1000):
        q = p.with_name(f"{stem}_{i}{suf}")
        if not q.exists():
            return q
    return p.with_name(f"{stem}_{int(time.time())}{suf}")


# ---------------------------------------------------------------- trash（应用内回收站）

def load_trash() -> dict:
    if _SQL_ORG is not None:
        return _catalog_trash().index()
    """回收站索引 {key: {name, orig, t, size}}。"""
    try:
        d = json.loads((TRASH_DIR / "index.json").read_text("utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_trash(idx: dict):
    try:
        TRASH_DIR.mkdir(parents=True, exist_ok=True)
        tmp = TRASH_DIR / f"index.{_tmp_tag()}.tmp"
        tmp.write_text(json.dumps(idx, ensure_ascii=False), "utf-8")
        tmp.replace(TRASH_DIR / "index.json")
        return True
    except OSError as ex:
        log(f"[warn] 回收站索引写入失败: {ex}")
        return False


def trash_move(p: Path):
    """移入应用内回收站。成功返回 (None, key)，失败返回 (错误信息, None)。"""
    if _SQL_ORG is not None:
        from backend.operations.trash import TrashError
        try:
            return _catalog_trash().for_path(p).move(p)
        except TrashError as error:
            return str(error), None
    if not p.is_file():
        return "文件不存在或已被删除", None
    if not inside_root(p):
        return "文件在根目录之外，拒绝删除", None
    moved = None                     # 提前占位：失败分支要清残留，不能引 NameError
    try:
        st = p.stat()
        key = f"{int(time.time() * 1000)}_{os.urandom(3).hex()}"
        TRASH_DIR.mkdir(parents=True, exist_ok=True)
        moved = TRASH_DIR / (key + p.suffix)
        shutil.move(str(p), str(moved))
    except OSError as ex:
        # B6-1（CMV3-LOCAL-FIX，2026-10-09 全代码审计）：图库在外接盘、DATA_DIR 在系统盘
        # （跨卷）时 `shutil.move` 走「复制 + 删源」两段，**每一段都可能失败**：
        #   ① 复制写一半抛错（目标盘空间不足 / 中断）⇒ 回收站里留下半截文件；
        #   ② 复制成功但删源失败（源被占用 / 只读）⇒ 留下完整副本。
        # 实测两种情形**原文件都完好留在原地**（不会两处丢失），但回收站里会多出
        # 一个不在 index.json 里的文件：UI 看不见、`trash_gc` 管不着（只遍历索引）、
        # `trash_purge` 也清不掉（只 glob 索引里的 key）⇒ 数据盘被静默持续占用；
        # 用户看到「删除失败」会重试，每次重试再泄漏一份，直到系统盘被吃满，
        # 然后 save_trash / save_favs 也开始失败，故障向外扩散。
        # ⇒ 失败分支必须把残留清掉（清不掉也要如实说明）。
        left = ""
        try:
            if moved is not None and moved.exists():
                moved.unlink()
        except OSError as ex2:
            left = f"（回收站里残留 {moved.name} 未能清除：{ex2}）"
            log(f"[error] 移入回收站失败且残留未清：{moved}：{ex2}")
        return f"移入回收站失败: {ex}{left}", None
    with TRASH_LOCK:
        idx = load_trash()
        idx[key] = {"name": p.name, "orig": str(p), "t": int(time.time()),
                    "size": st.st_size,
                    # B6-3：把真实落盘名记进索引 —— 无扩展名的文件（README / a.）
                    # 落盘名里**不含点**，靠 glob(key+".*") 反查永远匹配不到它。
                    "stored": moved.name}
        if not save_trash(idx):
            # §6.1：索引没落盘 ⇒ 这个 key 不存在于下次 load_trash() 的结果里，
            # 文件就成了「在回收站目录里、但谁也看不见」的孤儿（也不受自动清理管辖）。
            # 光「说一句失败了」不够 —— 必须**把文件退回去**（回滚），别留孤儿。
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(moved), str(p))
            except OSError as ex2:
                log(f"[error] 回收站索引写入失败且回滚失败：文件仍在 {moved}，原路径 {p}：{ex2}")
                return (f"移入回收站失败：索引与回滚均失败，文件暂存在 {moved}（详见日志）", None)
            log(f"[warn] 回收站索引写入失败，已把 {p.name} 退回原位置")
            return "移入回收站失败：索引写入失败（磁盘可能已满），文件已退回原位置", None
    return None, key


def _trash_stored_path(key: str, entry: dict = None):
    """按 key 找到回收站里真正落盘的那个文件；找不到返回 None（B6-3 · CMV3-LOCAL-FIX）。

    为什么不能再用 `TRASH_DIR.glob(key + ".*")`：落盘名是 `key + Path.suffix`，
    **无扩展名的文件**（`README` / `a.` / Windows 会去掉尾部点）后缀为空 ⇒
    落盘名就是 `key` 本身（恒不含点），而 `glob("key.*")` 要求「key 后面必须有点」
    ⇒ 永远匹配不到。实测后果：点「还原」返回「回收站文件已丢失」（文件其实好端端
    躺在目录里），旧代码随即**删掉索引条目** ⇒ 该文件从回收站 UI 永久消失；
    此后 `trash_purge([key])` 也因为同一个 glob 匹配不到而 `purged: 0`
    ⇒ 索引没了、文件还在，对用户等于永久丢失，回收站目录还永久残留。

    新口径：① 优先用索引里记的落盘名（新条目都有 `stored`）；
    ② 老条目没有 `stored` ⇒ 用「名字等于 key 或以 `key.` 开头」前缀匹配兜底
    （点号保证了不会误命中别的 key）。
    """
    if entry:
        nm = str(entry.get("stored") or "")
        if nm:
            cand = TRASH_DIR / nm
            try:
                if cand.is_file():
                    return cand
            except OSError:
                pass
    try:
        hit = [f for f in TRASH_DIR.iterdir()
               if f.is_file() and (f.name == key or f.name.startswith(key + "."))]
    except OSError:
        return None
    if not hit:
        return None
    try:                                  # 万一有历史残留，取最大的那个（真正的数据）
        hit.sort(key=lambda f: f.stat().st_size, reverse=True)
    except OSError:
        pass
    return hit[0]


def _move_trash_restore(src: Path, dest: Path):
    # shutil.move falls back to copy+unlink even for a sharing violation.
    # That can create a restored copy while leaving the locked trash source.
    # Retry the atomic rename; copy only for an actual cross-volume move.
    for attempt in range(6):
        try:
            os.rename(src, dest)
            return
        except OSError as ex:
            if getattr(ex, "winerror", None) in (32, 33) and attempt < 5:
                time.sleep(0.1 * (attempt + 1))
                continue
            if ex.errno == 18 or getattr(ex, "winerror", None) == 17:
                shutil.move(str(src), str(dest))
                return
            raise


def trash_restore(key: str) -> dict:
    if _SQL_ORG is not None:
        from backend.operations.trash import TrashError
        try:
            return _catalog_trash().for_item(key).restore(key)
        except TrashError as error:
            return {"ok": False, "error": str(error), "pending": False}
    with TRASH_LOCK:
        idx = load_trash()
        e = idx.get(key)
        if not e:
            return {"ok": False, "error": "回收站条目不存在"}
        src = _trash_stored_path(key, e)
        if not src:
            # B6-3：**绝不**在这里删索引条目 —— 条目是用户唯一还能看见它的地方。
            # 旧代码 `del idx[key]` + 报「回收站文件已丢失」，可文件其实还在目录里
            # （无扩展名条目就是这条路径），于是它从回收站 UI 永久消失、purge 也
            # 够不到。改为保留条目、把「期望的文件名」如实说出来，可重试、可手工处理。
            return {"ok": False, "keep": True,
                    "error": "回收站里找不到这个文件（索引记录的文件名：%s）。"
                             "条目已保留 —— 可稍后重试，或到数据目录的 trash\\ 里手工处理"
                             % (e.get("stored") or (key + ".*"))}
        orig = Path(e["orig"])
        snap_fav = e.get("fav")
        snap_tags = e.get("tags")
        snap_mark = e.get("mark")
        try:
            orig.parent.mkdir(parents=True, exist_ok=True)
            dest = _unique_path(orig)
            _move_trash_restore(src, dest)
        except OSError as ex:
            return {"ok": False, "error": f"还原失败: {ex}"}
        del idx[key]
        # §6.1：索引没落盘 ⇒ 条目残留，用户再点一次「还原」会看到「条目不存在」，
        # 而文件其实早就回去了（脏索引 + 误报）。带回 idx_stale 让前端能说清。
        idx_stale = not save_trash(idx)
        if idx_stale:
            log("[warn] 回收站索引落盘失败（还原之后），条目可能残留")
    # P1-2：还原「文件」与还原「它的收藏 / 标签 / 标记」是两件事 —— 后者失败也必须说出来，
    # 否则用户看到「已还原」却发现收藏没了，且没有任何线索（沿用 P0-2 数据目录迁移的口径）。
    failed = []
    if snap_fav or snap_tags or snap_mark:
        failed = _restore_meta(str(dest), fav=snap_fav, tags=snap_tags, mark=snap_mark)
    r = {"ok": True, "path": str(dest), "renamed": dest.name != e["name"]}
    if idx_stale:
        r["idx_stale"] = True
    if failed:
        r["partial"] = True
        r["failed"] = failed
        r["warning"] = ("文件已还原到原位置，但 " + "、".join(failed)
                        + "未能写回（磁盘不可写或被占用），这部分需要手工恢复")
        log(f"[warn] 还原后元数据回写失败：{failed}  path={dest}")
    return r


def _restore_meta(path: str, fav=None, tags=None, mark=None) -> list:
    """把移入回收站前快照的收藏/标签/标记写回还原后的文件路径。

    P1-2：返回**写失败的类别名列表**（空列表=全部成功）。此前三个 save 的返回值
    一个都没看，写盘失败（磁盘满 / 只读 / 文件被占用）被完全吞掉，调用方照报成功。
    """
    global _FAIL_SAVE_AT
    if os.environ.get("CMV_TEST_FAIL_SAVE") == "favs":
        _FAIL_SAVE_AT = "favs"          # 测试注入：只影响本次恢复回写，返回前复位
    bad = []
    if fav:
        with FAV_LOCK:
            fv = load_favs()
            if path not in fv:
                fv[path] = {"t": int(time.time()), "cat": fav if isinstance(fav, str) and fav else "默认"}
                if not save_favs(fv):
                    bad.append("收藏")
    if tags:
        clean, seen = [], set()
        for t in tags:
            t = _norm_tag(t)
            if t and t not in seen:
                clean.append(t)
                seen.add(t)
        with TAGS_LOCK:
            ts = load_tags()
            if clean:
                ts[path] = clean
            elif path in ts:
                ts.pop(path, None)
            if not save_tags(ts):
                bad.append("标签")
    if mark and mark in MARK_LABELS:
        with MARKS_LOCK:
            mk = load_marks()
            if path not in mk:
                mk[path] = {"state": mark, "updated": int(time.time())}
                if not _save_json(MARKS_FILE, mk):
                    bad.append("标记")
    _FAIL_SAVE_AT = None                 # 注入只在一次恢复内有效
    return bad


def trash_purge(keys: list, all_: bool = False) -> dict:
    if _SQL_ORG is not None:
        svc = _catalog_trash()
        # P0-2 补丁（CMV3-LOCAL-FIX）：CatalogTrash.purge 只认**显式 keys**（空输入恒
        # no-op，见其 docstring），而前端「清空回收站」发的是 {"all":true,"keys":[]}
        # —— 旧写法把它原样透传成 purge([]) ⇒ **SQLite 模式下「清空回收站」点什么
        # 都不删**（用户以为清干净了，条目还在）。这里把 all_ 翻成「库内全部 trashed
        # key」，与下面的 legacy 分支同义。
        if all_:
            keys = [it["key"] for it in svc.list_items()]
        return svc.purge(keys)
    """彻底删除指定条目，返回删除文件数。

    ⚠️ P0-2（CMV3-LOCAL-FIX，2026-10-08 全代码审计）：「清空全部」曾**隐含**在
       `if not keys` 里 —— keys 缺字段 / 为 null / 为 [] 三种形态全都落进
       `keys = list(idx)`，于是任何一次「前端漏传字段」都变成**不可逆的整站清空**，
       且无二次确认。更糟的是它与「用户确实想清空」在服务端完全不可区分。
       现在拆开：**清空全部必须显式传 `all_=True`**（HTTP 侧 `{"all":true}`）；
       `keys` 为空 ⇒ 什么都不做（no-op），返回 purged=0，绝不代用户做决定。
       调用方：`trash_gc` 走显式 `keys=` 列表；前端「清空回收站」改传 `{"all":true}`。

    §6.1：此前 `except OSError: pass` —— 文件删不掉（被占用 / 只读 / 权限）时静默跳过，
    可索引项**已经先删了** ⇒ 文件变成「在回收站目录里但谁也看不见」的孤儿：
    白占空间、不受自动清理管辖、也无法重试。现在两处都改：
      ① 删不掉的条目**保留在索引里**（用户看得见、能重试）；
      ② 失败明细如实带回给前端（`failed` + `error`）。
    """
    with TRASH_LOCK:
        idx = load_trash()
        if all_:
            keys = list(idx)
        elif not keys:
            log("[trash] purge 收到空 keys 且未指定 all —— 按 no-op 处理（P0-2：不再默认整站清空）")
            return {"ok": True, "purged": 0, "empty": True}
        n = 0
        failed = []
        missing = []
        for k in keys:
            if k not in idx:
                continue
            # B6-3：不再用 `glob(k + ".*")` 找落盘文件 —— 无扩展名条目（README / a.）
            # 的落盘名恒不含点，那个 glob 永远匹配不到。旧行为是 purged:0 且**摘索引**
            # ⇒ 条目没了、文件还留在回收站目录里，谁都不管（用户等于永久丢失）。
            hit = _trash_stored_path(k, idx[k])
            if hit is None:
                missing.append({"key": k, "name": idx[k].get("name") or k,
                                "stored": idx[k].get("stored") or (k + ".*")})
                log("[warn] 回收站文件缺失，条目保留: %s（key=%s，期望文件名 %s）"
                    % (idx[k].get("name") or k, k,
                       idx[k].get("stored") or (k + ".*")))
                continue
            stuck = []
            try:
                hit.unlink()
                n += 1
            except OSError as ex:
                stuck.append("%s: %s" % (hit.name, ex))
            if stuck:
                failed.append({"key": k, "name": idx[k].get("name") or k,
                               "files": len(stuck), "reason": stuck[0]})
                log("[warn] 回收站文件删除失败，条目保留: %s（%d 个文件）: %s"
                    % (idx[k].get("name") or k, len(stuck), stuck[0]))
            else:
                del idx[k]              # 文件都删干净了才摘索引
        if not save_trash(idx):
            log("[warn] 回收站索引落盘失败（purge 之后）")
    _dir_usage_invalidate(TRASH_DIR)     # F5：清空回收站后，设置页占用数字要立刻更新
    r = {"ok": True, "purged": n}
    if failed:
        r["failed"] = failed
        r["error"] = ("部分文件未能彻底删除（可能被占用或只读），"
                      "已保留在回收站列表中，可稍后重试")
    if missing:
        # B6-3：条目在、文件没了 —— 既不静默摘条目，也不谎报「已清理」。
        r["missing"] = missing
        r["error"] = ((r["error"] + "；" if r.get("error") else "")
                      + "%d 个条目的文件在回收站目录里找不到（条目已保留，"
                        "未计入清理数）" % len(missing))
    return r


def load_trash_keep_days() -> int:
    """回收站自动清理的保留天数。

    P1-3：0 = 关闭自动清理（回收站永不自动丢东西）。此前是写死的模块常量 30 天，
    到点就在启动时**静默永久删除**——用户完全无从察觉、也无从关闭，等发现时文件已没了。
    默认值保持 30（行为不放大），只是让它变得**可查、可配、可关**。
    优先级：环境变量 CMV_TRASH_KEEP_DAYS > settings.json 的 trash_keep_days > 默认 30。
    """
    raw = os.environ.get("CMV_TRASH_KEEP_DAYS")
    if raw is None or not str(raw).strip():
        raw = load_settings().get("trash_keep_days")
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return TRASH_KEEP_DAYS
    return n if n >= 0 else TRASH_KEEP_DAYS


def trash_gc(keep_days: int = None):
    """启动时清理超龄回收站条目（锁内收集、锁外清理，避免锁重入）。

    keep_days=None ⇒ 取运行时配置；<=0 ⇒ 关闭清理（什么都不做）。
    P1-3：清理**必须留痕** —— 此前是静默 delete，用户丢文件后查不到任何记录。
    """
    kd = load_trash_keep_days() if keep_days is None else int(keep_days)
    if kd <= 0:
        log("[trash] 回收站自动清理已关闭（trash_keep_days=0）")
    else:
        cutoff = time.time() - kd * 86400
        with TRASH_LOCK:
            items = load_trash()
            dead = [k for k, e in items.items() if (e.get("t") or 0) < cutoff]
        if dead:
            names = [str(items[k].get("name") or k) for k in dead[:5]]
            more = "" if len(dead) <= 5 else f" 等 {len(dead)} 个"
            log(f"[trash] 自动清理 {len(dead)} 个超过 {kd} 天的条目："
                + "、".join(names) + more)
            trash_purge(dead)
        else:
            log(f"[trash] 自动清理检查完成：没有超过 {kd} 天的条目")
    # B6-1（CMV3-LOCAL-FIX）：把「不在索引里」的文件点名叫出来。⚠️ 这一段与 kd 无关 ——
    # 自动清理关掉（kd=0）时孤儿照样存在，不能跟着 early-return 一起被跳过。
    # 只统计不删（见 trash_orphans 的说明）。此前完全没有信号：设置页占用数字
    # 比回收站列表大，用户只会以为软件算错了。
    orphan = trash_orphans()
    if orphan:
        shown = "、".join(orphan[:5]) + (f" 等 {len(orphan)} 个" if len(orphan) > 5 else "")
        log(f"[trash] 回收站里发现 {len(orphan)} 个不在索引里的文件（不显示、不自动清理，"
            f"只能手工删）：{shown}")


def trash_orphans() -> list:
    """回收站目录里「不在索引里」的文件名（B6-1 泄漏残留 + 历史孤儿）。

    这类文件最阴的地方是**三不管**：界面只列索引里的条目 ⇒ 用户看不见；
    `trash_gc` 只遍历索引 ⇒ 不会被自动清理；`trash_purge` 只 glob 索引里的
    key ⇒ 手动也清不掉。它们白占数据盘，并且让「设置页占用数字」与回收站
    列表对不上（用户会以为软件算错了）。

    这里只**列出**、不删 —— 它们不在索引里，用户无从确认来源，自动删掉比
    留着更危险。由 `trash_gc` 在启动时点名叫出来，并由设置页显示个数。
    """
    try:
        entries = load_trash()
    except Exception:                                  # noqa: BLE001 读索引失败不该拖垮统计
        entries = {}
    exact = set()
    for k, e in entries.items():
        exact.add(k)
        nm = e.get("stored")
        if nm:
            exact.add(str(nm))
    keys = list(entries)
    out = []
    try:
        for f in TRASH_DIR.iterdir():
            if not f.is_file() or f.name == "index.json" or f.name.startswith("index."):
                continue
            if f.name in exact:
                continue
            # 前缀兜底：老条目（B6-3 之前）没记 stored，落盘名是「key + 后缀」
            if any(f.name.startswith(k + ".") for k in keys):
                continue
            out.append(f.name)
    except OSError:
        return []
    return sorted(out)


def trash_list() -> list:
    if _SQL_ORG is not None:
        return _catalog_trash().list_items()
    out = []
    for k, e in load_trash().items():
        ext = os.path.splitext(e.get("name", ""))[1].lower()
        out.append({"key": k, "name": e.get("name", "?"),
                    "orig": e.get("orig", ""), "t": e.get("t", 0),
                    "size": e.get("size", 0),
                    "type": "image" if ext in IMAGE_EXTS else
                            ("video" if ext in VIDEO_EXTS else "file")})
    out.sort(key=lambda x: -x["t"])
    return out


# ---------------------------------------------------------------- 系统回收站（Windows Shell）

FO_DELETE = 0x0003
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040          # 关键位：有它才是「移入回收站」，没有就是永久删除
FOF_NOERRORUI = 0x0400
# ⚠️ P0-1：FOF_ALLOWUNDO|FOF_NOCONFIRMATION 在「卷上没有回收站」时会被 Windows 降级成
# 永久删除，且返回码仍是 0 —— 文件不可恢复，而调用方以为成功了（exFAT / 网络映射盘 /
# 部分 USB 可移动盘 / 超配额都会走到这条路径）。加上这一位后，Windows 必须弹
# 「确实要永久删除吗」而不是静默销毁，是本函数的最后一道兜底。
FOF_WANTNUKEWARNING = 0x4000

_SHFILEOP = None                # 惰性缓存 SHFILEOPSTRUCTW（非 Windows 无 ctypes.wintypes）
_SHFILEOP_LOCK = threading.Lock()
_RB_SUPPORT = {}                # P0-1：卷 → 是否有系统回收站（SHQueryRecycleBinW 查询结果缓存）
_RB_SUPPORT_LOCK = threading.Lock()
# N-1：卷 → Shell API 返回码是否已被「硬证据」确立为不可信（见 _rb_confirm_moved）。
# 只在拿到过 $I 取证之后才置位，不是猜的；按卷隔离，缺陷卷不影响正常卷。
_RB_LIE = {}
_RB_LIE_LOCK = threading.Lock()
# B6-2（CMV3-LOCAL-FIX）：卷 → 首次「返回 0 的成功删样本」是否已取证通过
# （见 _rb_verify_first_success）。三重含义：True=本卷确实进了回收站 /
# False=本卷不接收（后续改走应用内回收站）。按卷缓存 ⇒ 批量删除只多扫一次 $I。
_RB_SAMPLED = {}
_RB_SAMPLED_LOCK = threading.Lock()
# B6-2b（CMV3-LOCAL-FIX，2026-10-10 阶段 3 回植）：卷 → (扫描时刻, _rb_records 结果)
#
# 为什么必须有：`_rb_records` 是**全量**扫本卷 $Recycle.Bin 下的所有 $I 记录。
# 原注释写的「实测本机 9966 个 $I 全量读取仅 0.49s」在**今天已完全不成立** ——
#   实测本机 C 盘 $Recycle.Bin 有 **61334 条 $I**，全量扫一次 **34.78s**
#   （G 盘 57 条 / 0.58s，D 盘 0 条）。而 /api/delete 的前端/门禁 HTTP 超时是 30s
#   ⇒ 在 C 盘上删任何一个文件都会 **超时**，界面表现为「删除卡死」，
#   门禁侧 `delmode_check` C1 直接 `TimeoutError: timed out`。
#   这不是测试环境特例：用户机器上回收站攒到几万条是常态（尤其数据盘）。
#
# 语义：TTL 内复用同一次扫描结果。`_rb_confirm_moved` / `_rb_verify_first_success`
#   判「本条记录的时刻 ts >= t_before」用的仍是记录**自带 FILETIME**，
#   缓存只影响「我们能看见哪些 $I」，不影响时间判据。
# ⚠️ **本次删除后必须主动失效**（见 _rb_records_invalidate）—— 否则刚删进去的
#   那条 $I 还在缓存里看不见 ⇒ 取证判失败 ⇒ 把「真进了回收站」谎报成失败
#   （正是 N-1 要修的那类谎报，方向相反）。**每一次删除成功/尝试后都失效。**
_RB_REC_CACHE = {}
_RB_REC_LOCK = threading.Lock()
_RB_REC_TTL = 20.0            # 秒；一次删除操作内多次取证复用，跨操作重新扫


def _rb_records_invalidate(drv: str = None):
    """作废 $I 记录缓存。本次删除**之后**必须调用（见 _RB_REC_CACHE 说明）。

    C29（2026-10-11）：缓存键已由 `drv` 改为 `(drv, 分钟桶)` ⇒ 这里必须**按前缀**删。
    否则单卷失效会静默失效 —— 刚删进去的 $I 仍在缓存里看不见，取证判失败，把
    「真进了回收站」谎报成失败（正是 N-1 要修的那一类，只是方向相反）。
    """
    with _RB_REC_LOCK:
        if drv is None:
            _RB_REC_CACHE.clear()
        else:
            for k in [k for k in _RB_REC_CACHE if k[0] == drv]:
                _RB_REC_CACHE.pop(k, None)


def _shfileop_struct():
    """SHFILEOPSTRUCTW 结构体（延迟构造）。

    ctypes.wintypes 只在 Windows 上存在，模块级引用会让非 Windows 直接导入失败。
    """
    global _SHFILEOP
    with _SHFILEOP_LOCK:
        if _SHFILEOP is None:
            from ctypes import wintypes as W

            class SHFILEOPSTRUCTW(ctypes.Structure):
                _fields_ = [("hwnd", W.HWND),
                            ("wFunc", W.UINT),
                            ("pFrom", W.LPCWSTR),
                            ("pTo", W.LPCWSTR),
                            ("fFlags", ctypes.c_uint16),   # FILEOP_FLAGS 是 WORD
                            ("fAnyOperationsAborted", W.BOOL),
                            ("hNameMappings", ctypes.c_void_p),
                            ("lpszProgressTitle", W.LPCWSTR)]

            _SHFILEOP = SHFILEOPSTRUCTW
        return _SHFILEOP


def _rb_nuke_on_delete(p: Path) -> bool:
    """该卷是否被设成「不把文件放进回收站，直接永久删除」（B6-2 前置判定）。

    注册表位置（资源管理器「回收站属性」里的按卷设置）：
        HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer
            \\BitBucket\\Volume\\<卷GUID>\\NukeOnDelete = 1
    卷 GUID 由 `GetVolumeNameForVolumeMountPointW` 取到（形如 `\\\\?\\Volume{GUID}\\`），
    子键名是其中的 `{GUID}` —— **本机实测确认**：C: → `{66c36c59-…}`、
    D: → `{0de88e57-…}`，两者都在 BitBucket\\Volume 下且带 `NukeOnDelete=0`。
    （个别系统上子键可能带 `Volume` 前缀，两种写法都试。）

    为什么要这道前置：`SHQueryRecycleBinW` 只看返回码，**感知不到这个配置** ——
    于是「卷确实有回收站（查询成功）但它被设成 NukeOnDelete」时，带 FOF_ALLOWUNDO
    的删除会被 Windows 静默降级成永久销毁**且返回 0**，而调用方据此回报
    「已移入系统回收站」，用户以为还能还原、实际已不可恢复。

    读不到 / 非 Windows / 取不到卷 ⇒ 返回 False（**不拦**）：这是前置拦截，漏判还有
    `FOF_WANTNUKEWARNING` 与 rc==0 的首次成功样本取证兜底；反过来误判成 True 会把
    正常卷的删除全赶去应用内回收站，代价更大。
    """
    if os.name != "nt":
        return False
    try:
        import winreg
        from ctypes import wintypes as W
        drv = os.path.splitdrive(str(p))[0]
        if not drv:
            return False
        buf = ctypes.create_unicode_buffer(64)
        kernel32 = ctypes.windll.kernel32
        kernel32.GetVolumeNameForVolumeMountPointW.argtypes = [W.LPCWSTR, W.LPWSTR, W.DWORD]
        kernel32.GetVolumeNameForVolumeMountPointW.restype = W.BOOL
        if not kernel32.GetVolumeNameForVolumeMountPointW(drv + "\\", buf, 64):
            return False
        tail = buf.value.rstrip("\\").split("\\")[-1]        # "Volume{GUID}"
        if not tail:
            return False
        cands = [tail]
        if tail.lower().startswith("volume"):
            cands.append(tail[len("Volume"):])               # "{GUID}"
        path = (r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                r"\BitBucket\Volume")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            n = winreg.QueryInfoKey(k)[0]
            low = {winreg.EnumKey(k, i).lower(): winreg.EnumKey(k, i)
                   for i in range(n)}
        for c in cands:
            real = low.get(c.lower())
            if not real:
                continue
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                    path + "\\" + real) as vk:
                    val, _t = winreg.QueryValueEx(vk, "NukeOnDelete")
                return bool(int(val))
            except (OSError, ValueError, TypeError):
                continue
        return False
    except (OSError, ValueError, TypeError, ImportError, AttributeError):
        return False


def _recycle_bin_supported(p: Path) -> bool:
    """该卷是否真的有系统回收站（P0-1 前置判定）。

    SHFileOperationW 带 FOF_ALLOWUNDO 时，若目标卷没有回收站（exFAT / 网络映射盘 /
    部分 USB 可移动盘），Windows 会**静默改成永久删除且返回 0**。所以删除前先问一次
    SHQueryRecycleBinW：查不到回收站的卷直接判失败，由 /api/delete 退回应用内回收站
    （可还原），而不是交给 Windows 悄悄销毁。

    结果按卷缓存 —— 批量删除几百个文件时只查一次，不放大开销。

    ⚠️ B6-2c（CMV3-LOCAL-FIX，2026-10-10 阶段 3）：**不能再把 SHQueryRecycleBinW
       当作第一判据** —— 那是个**聚合统计**调用，在回收站条目多时慢到不可用。
       实测本机 C 盘（$Recycle.Bin 下 61334 条 $I / 6.1 万条）：
           SHQueryRecycleBinW("C:\\\\") 单次 **11.2 ~ 17.8s**
           （同机器 G 盘 57 条：0.063s）
       而 /api/delete 的调用方（前端 fetch / 门禁 urllib）超时是 30s ⇒
       **首次在 C 盘删任何一个文件都会卡十几秒到超时**，界面表现为「删除卡死」，
       门禁侧 `delmode_check` C1 直接 `TimeoutError: timed out`。
       且它是"first-call"，缓存救不了首删。真实用户机器上回收站攒到几万条是常态。

       改为 **目录探测优先、Shell 查询兜底**：
         ① 轻量判据（O(1)、无 Shell 调用）：该卷 `$Recycle.Bin` 目录存在、
            且当前用户的 SID 子目录存在且**可写**（能建临时文件即删）。
            这三条同时成立 ⇒ 该卷按设计就支持回收站 —— 直接判 True，**不调 Shell**。
         ② 只有①不成立时才退回 SHQueryRecycleBinW（此时通常是 exFAT/网络盘等
            真没有回收站的情形，条目必为 0 ⇒ 查询本身也快）。
       保留 NukeOnDelete 注册表补强（B6-2）不变。
    """
    if os.name != "nt":
        return False
    drv = os.path.splitdrive(str(p))[0]
    if not drv:
        return False
    root = drv if drv.endswith("\\") else drv + "\\"
    with _RB_SUPPORT_LOCK:
        if root in _RB_SUPPORT:
            return _RB_SUPPORT[root]
    ok = _rb_dir_probe(root)
    if ok is None:                         # ① 探测不出结论 ⇒ 才走慢的 Shell 查询
        ok = False
        try:
            from ctypes import wintypes as W
            shell32 = ctypes.windll.shell32

            class SHQUERYRBINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint32),
                            ("i64Size", ctypes.c_int64),
                            ("i64NumItems", ctypes.c_int64)]

            info = SHQUERYRBINFO()
            info.cbSize = ctypes.sizeof(info)
            shell32.SHQueryRecycleBinW.argtypes = [W.LPCWSTR,
                                                   ctypes.POINTER(SHQUERYRBINFO)]
            shell32.SHQueryRecycleBinW.restype = ctypes.c_long
            ok = (shell32.SHQueryRecycleBinW(root, ctypes.byref(info)) == 0)
        except (OSError, AttributeError, ValueError, TypeError):
            ok = False                 # 查不动时不拦：还有 FOF_WANTNUKEWARNING 兜底
    if ok and _rb_nuke_on_delete(p):
        # B6-2：查询成功 ≠ 删了会进回收站。卷被设成「不将文件移入回收站」时，
        # Windows 会**静默永久删除且返回 0** —— 前置判定形同虚设。
        # 按「该卷没有系统回收站」处理 ⇒ 调用方退回应用内回收站（可还原）。
        log(f"[回收站] 卷 {root} 被设为「不将文件移入回收站」（NukeOnDelete）—— "
            f"按「该卷没有系统回收站」处理，删除退回应用内回收站（可还原）")
        ok = False
    with _RB_SUPPORT_LOCK:
        _RB_SUPPORT[root] = ok
    return ok


def _rb_dir_probe(root: str):
    """B6-2c 轻量判据：该卷回收站**按设计可用**？

    依次确认（全部 O(1)，不调 Shell）：
      ① `<root>$Recycle.Bin` 存在且是目录；
      ② 其下有**当前用户**的 SID 子目录（Vista+ 每用户一个）；
      ③ 该 SID 目录**可写** —— 建一个临时文件随即删掉。
    三条都成立 ⇒ True（该卷支持回收站，无需再问 Shell）。
    ①②任一不成立 ⇒ False（该卷确实没有回收站目录）。
    ③失败（权限）⇒ **None**（不可判定，交由调用方走 Shell 兜底，不擅自判死）。

    为什么要「当前用户」而不是「随便哪个能写的 SID 目录」：
      `$Recycle.Bin` 下通常有多个 SID —— 实测本机 C 盘是 `S-1-5-18`（SYSTEM）
      与当前用户 `S-1-5-21-<machine>-<uid>` 两个。**只有当前用户自己的 SID 目录
      会被 Windows 用于存放他的删除**；别的用户（或 SYSTEM）的目录即使侥幸可写，
      也证明不了「本用户删文件会进回收站」——那正是 P0-1 要拦的静默永久删除。
      所以：**当前用户 SID 优先**，其次才考虑「不认识的但可写」的目录；
      已知**系统 SID**（见 _RB_SYS_SIDS）只作最后兜底，且不作为判 True 的依据。

    为什么可写性要实测：SID 目录存在但不可写时，带 FOF_ALLOWUNDO 的删除同样会被
    Windows 静默降级成永久删除 —— 这与「卷没有回收站」是同一后果。实测一次比
    依赖聚合查询便宜四个数量级（~0.5s vs 11~18s）。
    """
    base = os.path.join(root, "$Recycle.Bin")
    if not os.path.isdir(base):
        return False
    try:
        sids = os.listdir(base)
    except OSError:
        return False
    if not sids:
        return False

    def _writable(sdir):
        probe = os.path.join(sdir, ".cmvprobe")
        try:
            with open(probe, "wb") as fh:
                fh.write(b"x")
            os.unlink(probe)
            return True
        except OSError:
            return False

    # ① 当前用户 SID 优先（最可信 —— 就是 Windows 会用来存他删除的那个目录）
    me = _current_sid()
    if me:
        for sid in sids:
            if os.path.normcase(sid) != os.path.normcase(me):
                continue
            sdir = os.path.join(base, sid)
            if os.path.isdir(sdir):
                return True if _writable(sdir) else None
            return None                    # 目录名对上了但不是目录 ⇒ 不可判定
    # ② 其次：任何一个**非系统** SID 目录可写（本机常见单用户情形兜底）
    for sid in sids:
        if sid.upper() in _RB_SYS_SIDS:
            continue
        sdir = os.path.join(base, sid)
        if os.path.isdir(sdir) and _writable(sdir):
            return True
    # ③ 剩下只有系统 SID ⇒ 不可判定，交 Shell 兜底（不擅自判 True/False）
    return None


# 已知的内置账户 SID（不属于某个真实登录用户）—— 见 _rb_dir_probe 的说明。
_RB_SYS_SIDS = frozenset((
    "S-1-5-18",        # LOCAL SYSTEM
    "S-1-5-19",        # LOCAL SERVICE
    "S-1-5-20",        # NETWORK SERVICE
    "S-1-5-32-544",    # Administrators（组 SID，出现时同样不作个人依据）
))


def _current_sid():
    """当前进程登录用户的 SID 字符串（取不到返回 ""）。

    为什么走 ctypes 取令牌而不是别的路：
      · `os.getlogin()` 给的是**用户名**不是 SID，且无控制台时会抛错；
      · 起 `whoami` 子进程要 spawn 一个进程（几十 ms 且引入外部依赖）；
      · `%USERDOMAIN%\\%USERNAME%` 同样只有名字，还得再查一次映射表。
    ⇒ 直接用 `OpenProcessToken(GetCurrentProcess())` + `GetTokenInformation(TokenUser)`
      + `ConvertSidToStringSidW` —— 纯内存、无子进程、无第三方依赖。

    失败一律返回 ""（调用方按「不知道」处理，走「非系统 SID 可写」兜底），
    绝不猜一个 SID 出来 —— 猜错会把「别的用户的回收站」当成「本用户的」。
    """
    try:
        import ctypes
        from ctypes import wintypes as W

        advapi32 = ctypes.windll.advapi32
        kernel32 = ctypes.windll.kernel32
        TOKEN_QUERY = 0x0008
        TokenUser = 1

        # ⚠️ **必须显式声明 argtypes/restype**。不声明时 ctypes 按默认的
        #    c_int 处理返回值/指针 ⇒ `GetTokenInformation(… None, 0, &need)`
        #    这个「只为取大小」的调用会拿不到内存、`need.value` 恒 0 ⇒ 整条链
        #    静默返回 ""（本机实测：不声明时 SID 取不到，声明后正常拿到）。
        advapi32.OpenProcessToken.argtypes = [W.HANDLE, W.DWORD,
                                              ctypes.POINTER(W.HANDLE)]
        advapi32.OpenProcessToken.restype = W.BOOL
        advapi32.GetTokenInformation.argtypes = [W.HANDLE, ctypes.c_int,
                                                 ctypes.c_void_p, W.DWORD,
                                                 ctypes.POINTER(W.DWORD)]
        advapi32.GetTokenInformation.restype = W.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,
                                                    ctypes.POINTER(W.LPWSTR)]
        advapi32.ConvertSidToStringSidW.restype = W.BOOL

        h = W.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(),
                                         TOKEN_QUERY, ctypes.byref(h)):
            return ""
        try:
            need = W.DWORD(0)
            advapi32.GetTokenInformation(h, TokenUser, None, 0, ctypes.byref(need))
            if not need.value:
                return ""
            buf = ctypes.create_string_buffer(need.value)
            if not advapi32.GetTokenInformation(h, TokenUser, buf, need.value,
                                                ctypes.byref(need)):
                return ""
            # TOKEN_USER 的第一个成员就是 SID_AND_ATTRIBUTES.Sid（一个指针）
            sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
            if not sid_ptr:
                return ""
            strsid = W.LPWSTR()
            if not advapi32.ConvertSidToStringSidW(ctypes.c_void_p(sid_ptr),
                                                   ctypes.byref(strsid)):
                return ""
            try:
                return strsid.value or ""
            finally:
                kernel32.LocalFree(strsid)
        finally:
            kernel32.CloseHandle(h)
    except (OSError, AttributeError, ValueError, TypeError):
        return ""


# ---------------------------------------------------------------------------
# N-1：系统回收站「谎报失败」的独立取证
#
# 实测（2026-09-19 本机 Win11，见 _artifacts/reports/audit_fix_n1_20260919/）：
#   · 文件**确实成功移入回收站**时，shell32!SHFileOperationW 返回 2；
#   · 而 2 同时又是 ERROR_FILE_NOT_FOUND ——「路径根本不存在」的返回码。
#   · 真失败长这样：文件被独占打开时返回 32（ERROR_SHARING_VIOLATION），且原路径仍在。
# 也就是说**返回码在这个 API 上没有任何区分力**，成功与否只能独立取证。
#
# 取证链两道，缺一不可：
#   ① 原路径必须已经消失 —— 文件还在就一律算失败（实测独占锁定即此种情形）；
#   ② 本卷回收站里必须有一条**点名该路径**、且删除时刻晚于本次调用前的 $I 记录。
#
# ⚠️ 绝不能只凭「文件不见了」判成功：永久删除同样会让文件消失，而那正是 P0-1
#    要拦住的事（没有回收站的卷会被 Windows 静默降级成永久删除）。
# ---------------------------------------------------------------------------
# C29（2026-10-11 · 阶段 3 回植批次）：$I 扫描的可扩展性
# ---------------------------------------------------------------------------
# 本机 C 盘 $I 已到 191,372 条（实测单条 open+read 0.21ms ⇒ 全量一次 ≈41s），而客户端
# 是 urlopen(timeout=30) ⇒ 必然超时（C26 取证：delmode_check C 段 30s 超时、
# p1bfix_recycle_check 跑 526.9s）。三处改动，**不放松任何判据**：
#   ① 元数据预筛（只 stat、不 open）：$I 是 Windows 在「移入回收站」那一刻**新建**的
#      544 字节文件，其 st_ctime 即入站时刻；只对 ctime >= since - SLACK 的条目做
#      open+解析。Windows 上 os.scandir 的 DirEntry.stat() 不额外发系统调用
#      （FindFirstFile 已带回 WIN32_FIND_DATA），故 19 万条预筛 < 1s。
#   ② 命中的条目并行解析（多核摊开）。
#   ③ **预筛为空 ⇒ 退化为全量扫** —— 硬约束：早年注释明确否定过「只扫 mtime 较新
#      的 $I」，理由是 $I 若保留原文件老时间戳就会漏掉真记录（= 谎报失败）。本实现
#      用 ctime（**创建时刻**，不是 mtime）+「空集退化」双重兜住：真失败路径下预筛
#      必然为空 ⇒ 老老实实全量，绝不因筛掉而放行。
# 返回语义：since 非空时只含「入站时刻不早于 since - SLACK」的子集。两个调用方
#   （_rb_confirm_moved / _rb_verify_first_success）都只做 recs.get(目标路径)，
#   已逐一核对 ⇒ 与返回全集对判据等价。
_RB_CTIME_SLACK = 120.0        # 秒；客户端与文件系统时钟可能有偏差，留宽裕余量
_RB_POOL = None
_RB_POOL_LOCK = threading.Lock()


def _rb_pool():
    """本模块共用的 $I 解析线程池（按需创建，进程退出时由解释器回收）。"""
    global _RB_POOL
    with _RB_POOL_LOCK:
        if _RB_POOL is None:
            _RB_POOL = concurrent.futures.ThreadPoolExecutor(
                max_workers=min(8, (os.cpu_count() or 4) * 2),
                thread_name_prefix="rb-scan")
        return _RB_POOL


def _rb_parse_one(path):
    """解析单条 $I，返回 (规范化原路径, 删除时刻 unix)；读不动/格式不符返回 None。

    $I 记录布局（Vista+，version>=2）：
        0..8   版本号      8..16  文件大小（字节）   16..24 删除时刻(FILETIME)
        24..28 路径字符数  28..    UTF-16LE 原路径
    （version 1 的旧记录在 24 处是定长 260 字符路径，一并兜住。）
    """
    try:
        with open(path, "rb") as fh:
            b = fh.read(4096)
    except OSError:
        return None
    if len(b) < 24:
        return None
    try:
        ft = int.from_bytes(b[16:24], "little")
        if ft <= 0:
            return None
        ts = (ft - 116444736000000000) / 1e7      # FILETIME → unix 秒
        ver = int.from_bytes(b[0:8], "little")
        if ver >= 2:
            if len(b) < 28:
                return None
            n = int.from_bytes(b[24:28], "little")
            raw = b[28:28 + n * 2]
        else:
            raw = b[24:24 + 520]
    except (ValueError, OverflowError):
        return None
    orig = raw.decode("utf-16-le", "replace").split("\x00")[0]
    if not orig:
        return None
    return os.path.normcase(orig.strip("\\")), ts


def _rb_records(drv: str, since: float = None):
    """扫该卷回收站的 $I 记录，返回 {规范化原路径: 删除时刻(unix)}；查不动返回 None。

    用记录**自带的 FILETIME** 而不是文件 mtime 判「是不是这次删的」：实测同一路径删
    两次会留下两条记录，只有时刻能区分（实测该字段精度优于 0.1s，与系统时钟一致）。

    ⚠️ 返回 None 是**硬信号** —— 调用方必须据此判失败。绝不能因为「查不到证据」就
    放行，那等于把 P0-1 的防线拆了。

    缓存：按 (卷, since 的分钟桶) 维度做 TTL 缓存 —— 同一次批量删除里的多次取证
    共享一份结果；跨桶重新扫。since 为 None（全量）也各占一档。
    ⚠️ C31：命中缓存还要求「快照时刻 >= since」—— 快照早于窗口起点就不可信（见下文）。

    B6-2b（2026-10-10）：加按卷 TTL 缓存。
    C29（2026-10-11）：预筛 + 并行 + 空集退化（见文件上方 C29 说明块）。
    C31（2026-10-11）：缓存可信性判据（born >= since）—— 修 C29 分钟桶缓存漏判。"""
    if since:
        bucket = int(since // 60)                     # 分钟桶：同批删除共享缓存
        cutoff = bucket * 60.0 - _RB_CTIME_SLACK      # 比 since 更保守（宁可多留）
    else:
        bucket, cutoff = None, None
    ck = (drv, bucket)
    with _RB_REC_LOCK:
        hit = _RB_REC_CACHE.get(ck)
        if hit is not None and (time.time() - hit[0]) < _RB_REC_TTL:
            # C31（2026-10-11）：快照**必须不早于窗口起点**（born >= since）才可信。
            #   否则 (born, since) 之间新落进回收站的 $I 不在快照里 ⇒ 把「真进了回收站」
            #   判成「查不到该路径的记录」⇒ 原样复现 N-1 的谎报失败（方向相反）。
            #   事故实测（p1bfix_recycle_check B11）：B10 直调 _rb_confirm_moved 建立了
            #   分钟桶快照（born=T10），紧接 B11 在**同一桶内**删除一个**新文件**并取证
            #   （since=T11 > T10）⇒ 命中陈旧快照 ⇒ recycle_bin_delete 返回「错误码 2」。
            #   ⚠️ 这条判据比「调用方记得 invalidate」更根本：_rb_records 是只读函数，
            #      不该把「删除后必须作废缓存」的责任外包给每一个调用方。
            #   `since is None`（全量调用；产品代码不用，只有门禁/调试用）沿用原 TTL 语义。
            if since is None or hit[0] >= since:
                return hit[1]
    root = os.path.join(drv, "$Recycle.Bin")
    try:
        sids = os.listdir(root)
    except OSError:
        return None                       # 连目录都列不出来 ⇒ 查不动

    # ---- ① 元数据预筛（只 stat，不 open）----
    cand, n_i = [], 0
    for sid in sids:
        sdir = os.path.join(root, sid)
        try:
            with os.scandir(sdir) as it:
                for e in it:
                    if not e.name.startswith("$I"):
                        continue
                    n_i += 1
                    if cutoff is not None:
                        try:
                            if e.stat(follow_symlinks=False).st_ctime < cutoff:
                                continue
                        except OSError:
                            continue      # stat 不动 ⇒ 当不满足（下面 ③ 兜）
                    cand.append(e.path)
        except OSError:
            continue                      # 其他用户的 SID 目录可能拒绝访问

    # ---- ③ 预筛为空 ⇒ 退化为全量（绝不因筛掉而放行）----
    if cutoff is not None and not cand and n_i:
        log("[回收站] 预筛为空（%d 条 $I 的 ctime 都早于 %.1f）⇒ 退化为全量扫，"
            "确保不因预筛而漏判" % (n_i, cutoff))
        for sid in sids:
            sdir = os.path.join(root, sid)
            try:
                with os.scandir(sdir) as it:
                    cand.extend(e.path for e in it if e.name.startswith("$I"))
            except OSError:
                continue

    # ---- ② 并行解析 ----
    res = {}
    if cand:
        for r in _rb_pool().map(_rb_parse_one, cand, chunksize=256):
            if r is None:
                continue
            key, ts = r
            if ts > res.get(key, 0.0):    # 同路径多条记录时取最新那条
                res[key] = ts
    with _RB_REC_LOCK:
        _RB_REC_CACHE[ck] = (time.time(), res)
    return res



def _rb_confirm_moved(p: Path, t_before: float) -> bool:
    """N-1：SHFileOperationW 返回码异常时，独立取证「它到底进没进回收站」。

    两道证据都成立才返回 True（判据细节见上面的长注释）。
    证据②只在**本卷首次**遇到返回码异常时做，否则批量删几千个文件会扫几千遍回收站；
    之后该卷走 _RB_LIE 缓存 —— 但证据①（原路径是否消失）**每次都做**，不省。
    """
    if os.path.lexists(str(p)):                       # ① 文件还在 ⇒ 一律失败
        return False
    drv = os.path.splitdrive(str(p))[0]
    if not drv:
        return False
    root = drv if drv.endswith("\\") else drv + "\\"
    with _RB_LIE_LOCK:
        if _RB_LIE.get(root) is True:
            return True                               # 本卷已确立：① 成立即可判成功
    recs = _rb_records(root, since=t_before)
    if recs is None:
        return False                                  # 查不动 ⇒ 保守判失败
    ts = recs.get(os.path.normcase(str(p).strip("\\")))
    if ts is None or ts < t_before:                   # ② 没有点名本路径的新记录
        return False
    with _RB_LIE_LOCK:
        _RB_LIE[root] = True
    log("[回收站] %s 已进回收站（$I 记录取证成立），但 shell32 返回码不可信；"
        "本卷后续删除不再信任返回码（原路径是否消失仍逐次校验）" % p)
    return True


def _rb_verify_first_success(p: Path, t_before: float) -> bool:
    """B6-2（CMV3-LOCAL-FIX）：`rc == 0 && !fAnyOperationsAborted` 分支的
    **首次成功样本取证**（每卷一次）。

    为什么连「返回 0 且未中止」也不能直接信：卷**确实有回收站**（SHQueryRecycleBinW
    查询成功）但它被设成「不将文件移入回收站」（`NukeOnDelete`）/ 回收站配额为 0 /
    当前用户 SID 目录不可写时，带 `FOF_ALLOWUNDO` 的删除会被 Windows **静默降级成
    永久销毁，且仍然返回 0**。此时回报成功 = 界面弹「已移入系统回收站」，
    用户以为还能还原，实际已不可恢复 —— 正是 P0-1 想拦住的同一类事故，只是绕过了
    「该卷有没有回收站」那道前置判定（`_rb_nuke_on_delete` 拦住了最常见的
    NukeOnDelete 配置，这里是它漏判时的第二道）。

    判据与 N-1 的取证链同口径：① 原路径必须已消失；② 本卷 `$I` 里必须有一条点名该
    路径、且时刻不早于调用前的记录。都成立才判「真的进了回收站」。

    ③ `$I` **读不动**（权限）时不能判失败 —— 那会把「查不到」变成「一定没进」，
       属另一种谎报；这种情况沿用旧行为（信返回码）且不再重复扫。

    开销：每卷只在**首次**成功删除时扫一次 `$I`（本机实测 9966 条 0.49s），
    之后走 `_RB_SAMPLED` 缓存 ⇒ 批量删几千个文件只多这一次。
    """
    drv = os.path.splitdrive(str(p))[0]
    root = drv + "\\" if drv and not drv.endswith("\\") else drv
    with _RB_SAMPLED_LOCK:
        if root in _RB_SAMPLED:
            return _RB_SAMPLED[root]
    if os.path.lexists(str(p)):          # ① 文件还在 ⇒ 这次操作其实没发生，如实报失败。
        return False                     #    不写缓存：单次异常不该把整个卷长期判死。
    recs = _rb_records(root, since=t_before) if root else None
    if recs is None:                     # ③ 查不动 ⇒ 不可判定，沿用旧行为（且不再重扫）
        with _RB_SAMPLED_LOCK:
            _RB_SAMPLED[root] = True
        log("[回收站] 读不到本卷 $I 记录，首次成功样本无法取证 —— "
            "本次沿用 SHFileOperationW 返回码，之后不再重复扫描")
        return True
    ts = recs.get(os.path.normcase(str(p).strip("\\")))
    ok = ts is not None and ts >= t_before
    with _RB_SAMPLED_LOCK:
        _RB_SAMPLED[root] = ok
    if not ok:
        log(f"[error] 卷 {root} 的系统回收站没有接收文件（$I 里查不到 {p} 的记录）—— "
            f"该卷疑似「不将文件移入回收站」或回收站配额已满；本卷后续删除改用"
            f"应用内回收站。⚠️ 本次删除的文件已不可恢复")
    return ok


def recycle_bin_delete(p: Path):
    """把单个文件移入 Windows 系统回收站。成功返回 None，失败返回错误信息。

    走 shell32!SHFileOperationW（系统自带 API，不引第三方依赖、不增打包体积）。
    两个易踩的点：
      ① 请求跑在 ThreadingHTTPServer 的子线程上，Shell API 要求线程先初始化 COM，
         所以显式 CoInitializeEx / CoUninitialize（本线程自己初始化的才反初始化）；
      ② SHFileOperationW 的 pFrom 要求**双 NUL 结尾**的多字符串，少一个 NUL 会读到垃圾。

    ⚠️ 第三点（P0-1）：「进不了回收站」必须如实报错，绝不能回报成功 —— 否则调用方
       不会退回应用内回收站，文件就永久没了，而界面弹的是「已移入系统回收站」。
       两道保险：前置 SHQueryRecycleBinW 判卷 + fFlags 带 FOF_WANTNUKEWARNING。

    ⚠️ 第四点（N-1）：反过来的谎报也存在 —— 文件**确实**进了回收站时 shell32
       仍返回 2，导致「删除成功」被报成失败：界面弹红，调用方还去试注定失败
       的 trash_move，最终把「已删除」报成「删除失败」。返回码非 0 时改用
       _rb_confirm_moved 独立取证；取证不成立仍按原样报错（宁误报失败，
       绝不误报成功）。

    ⚠️ 第五点（B6-2 · CMV3-LOCAL-FIX）：**返回 0 也不等于进了回收站**。前置判定只看
       SHQueryRecycleBinW 的返回码，感知不到该卷被设成「不将文件移入回收站」
       （NukeOnDelete，见 _rb_nuke_on_delete）——那种卷会静默永久删除且返回 0。
       两道补强：① 前置判定先读注册表的 NukeOnDelete；② 每次「首次成功删除」
       仍做一次 $I 样本取证（_rb_verify_first_success，按卷缓存）。
    """
    if os.name != "nt":
        return "当前系统没有 Windows 回收站"
    if not p.is_file():
        return "文件不存在或已被删除"
    if not inside_root(p):
        return "文件在根目录之外，拒绝删除"
    if not _recycle_bin_supported(p):
        # 这个卷没有回收站：SHFileOperationW 会静默永久删除，宁可报错让上层降级
        return "该卷没有系统回收站，已改用应用内回收站"
    try:
        from ctypes import wintypes as W
        ole32 = ctypes.windll.ole32
        shell32 = ctypes.windll.shell32
    except (OSError, AttributeError) as ex:
        return f"系统回收站不可用: {ex}"
    hr = ole32.CoInitializeEx(None, 0x2)          # COINIT_APARTMENTTHREADED
    own_com = hr in (0, 1)                        # S_OK / S_FALSE 才是自己初始化的
    try:                                          # RPC_E_CHANGED_MODE 时沿用既有模型
        op = _shfileop_struct()()
        op.wFunc = FO_DELETE
        op.fFlags = (FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT
                     | FOF_WANTNUKEWARNING)   # P0-1：禁止静默永久删除
        buf = ctypes.create_unicode_buffer(str(p) + "\0")   # 末尾再加 ctypes 自带的 NUL
        op.pFrom = ctypes.cast(buf, W.LPCWSTR)              # 必须留引用，否则被 GC 回收
        shell32.SHFileOperationW.restype = ctypes.c_int
        shell32.SHFileOperationW.argtypes = [ctypes.POINTER(_shfileop_struct())]
        # N-1：取证窗口起点。**刻意不留容差** —— 实测 $I 内的删除时刻(FILETIME)与
        # time.time() 一致到 0.1s 以内，而 shell 记录时刻必然发生在本行之后，所以
        # 「记录时刻 >= t_before」是严格成立的。任何向后容差都只是**假成功窗口**：
        # 容差内用户以前删掉同一路径留下的旧记录会被当成本次删除的证据。
        t_before = time.time()
        rc = shell32.SHFileOperationW(ctypes.byref(op))
        if rc == 0 and not op.fAnyOperationsAborted:
            # B6-2（CMV3-LOCAL-FIX，2026-10-09 全代码审计）：**返回 0 不等于进了回收站** ——
            # 「不将文件移入回收站」/ 配额为 0 的卷会静默永久删除且照样返回 0。
            # 每卷首次成功删除时做一次 $I 样本取证（之后走缓存，批量删除只多这一次）。
            if _rb_verify_first_success(p, t_before):
                return None
            return ("系统回收站没有接收文件（本卷疑似「不将文件移入回收站」或"
                    "回收站配额已满），已改用应用内回收站")
        # ---- N-1：返回码非 0 **不等于**失败 ----------------------------------
        # 本机实测：成功进回收站也返回 2，而 2 同时又是「路径不存在」的返回码 ——
        # 返回码在这里没有任何区分力，必须独立取证（见 _rb_confirm_moved）。
        if _rb_confirm_moved(p, t_before):
            return None
        if rc:
            return f"系统回收站返回错误码 {rc}"
        return "系统回收站操作被中止"
    except (OSError, ValueError, TypeError) as ex:
        return f"移入系统回收站失败: {ex}"
    finally:
        # B6-2b：**每一次删除尝试之后都必须作废本卷 $I 缓存** ——
        # 刚才那次 SHFileOperationW 可能刚写进一条新 $I，缓存里看不见它；
        # 若不作废，紧随其后的取证（_rb_verify_first_success / _rb_confirm_moved）
        # 会判「查不到该路径的记录」⇒ 把真进了回收站报成失败（N-1 同类谎报，方向相反）。
        # 放在 finally ⇒ 覆盖上面**全部** return 分支（含异常与提前 return）。
        _drv = os.path.splitdrive(str(p))[0]
        if _drv:
            _rb_records_invalidate(_drv if _drv.endswith("\\") else _drv + "\\")
        if own_com:
            try:
                ole32.CoUninitialize()
            except OSError:
                pass


# ---------------------------------------------------------------- 查重（dhash 感知哈希）

def _dhash(p: Path) -> int:
    """dHash 64 位感知哈希：9x8 灰度相邻像素比较。"""
    from PIL import ImageOps
    with Image.open(p) as im:
        im = ImageOps.exif_transpose(im)
        g = im.convert("L").resize((9, 8))
        px = list(g.getdata())
    bits = 0
    for r in range(8):
        row = r * 9
        for c in range(8):
            bits = (bits << 1) | (1 if px[row + c] > px[row + c + 1] else 0)
    return bits


def find_dupes(path: Path, recursive: bool, limit: int = 3000,
               near: int = 6) -> dict:
    """扫描目录找相似图片：精确哈希分桶后，桶代表间按汉明距离 ≤ near 合并。"""
    files = []
    stack = [(str(path), 0)]
    # ε-12（§6.3）：递归遍历加 realpath 防环（与 _index_builder 同款口径）。
    # 此前只有深度上限 —— junction/符号链接自环会让同一目录反复入栈，
    # limit=3000 的扫描预算被环目录吃满，真正的图反而一张都扫不到。
    visited = {os.path.realpath(str(path))}
    while stack and len(files) < limit:
        d, lvl = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    if len(files) >= limit:
                        break
                    try:
                        if e.is_dir():
                            if recursive and lvl < 10:
                                rp = os.path.realpath(e.path)
                                if rp not in visited:
                                    visited.add(rp)
                                    stack.append((e.path, lvl + 1))
                        elif os.path.splitext(e.name)[1].lower() in IMAGE_EXTS:
                            files.append(e.path)
                    except OSError:
                        continue
        except OSError:
            continue
    exact = {}                       # hash -> [(hash, path, stat), ...]
    scanned_identities = {}
    changed_during_scan = 0
    for fp in files:
        try:
            p = Path(fp)
            before = _SQL_CATALOG.verify(STATE["root"], p) if _SQL_CATALOG is not None else None
            perceptual_hash = _dhash(p)
            file_stat = p.stat()
            if before is not None:
                after = _SQL_CATALOG.verify(STATE["root"], p)
                if (before["mediaId"], before["revisionId"]) != (after["mediaId"], after["revisionId"]):
                    changed_during_scan += 1
                    continue
                scanned_identities[fp] = after
            exact.setdefault(perceptual_hash, []).append((None, fp, file_stat))
        except Exception:
            continue
    # B6-5（CMV3-LOCAL-FIX，2026-10-09 全代码审计）：分组合并改成「**组内全对**」判据。
    #
    # 为什么不是「只跟组代表比」：那样组内两两距离可以到 2*near。实测复刻该分组逻辑
    # （dev 的合成哈希实验）：三张图 A/B/C 满足 d(A,B)=6、d(A,C)=6、d(B,C)=12
    # （sorted 之后 A 当代表）会并成**同一组**，组内最大两两距离 12 = near 的两倍 ——
    # 64 位里 19% 比特不同，很可能是同场景不同曝光 / 不同裁剪 / 加了水印的**不同图**，
    # 而前端把整组当「重复图」默认保留第一张、删其余 ⇒ 用户按「重复」一键删就是真丢图。
    # 全对检查维持不变量「组内任意两张距离都 ≤ near」，从根上消灭 2*near 这个野值。
    # （注：阈值本身不松 —— 独立比特模型下随机 64 位两两距离 ≤6 的比例实测 0/200000。）
    groups = []                      # [ [entries...] ]
    for h in sorted(exact):
        placed = None
        for g in groups:
            if all(bin(m[0] ^ h).count("1") <= near for m in g):
                placed = g
                break
        if placed is None:
            placed = [(h, x[1], x[2]) for x in exact[h]]
            groups.append(placed)
        else:
            placed.extend((h, x[1], x[2]) for x in exact[h])
    out = []
    for g in groups:
        if len(g) < 2:
            continue
        items = [{"path": fp, "name": os.path.basename(fp),
                  "size": st.st_size, "mtime": int(st.st_mtime)}
                 for _, fp, st in g]
        if _SQL_CATALOG is not None:
            for item in items:
                try:
                    identity = scanned_identities[item["path"]]
                    item["mediaId"] = identity["mediaId"]
                    item["expectedVersion"] = "rev:" + identity["revisionId"]
                except (KeyError, OSError, RuntimeError, ValueError):
                    item["identityUnavailable"] = True
        items.sort(key=lambda x: -x["mtime"])       # 最新在前，前端默认保留第一张
        # B6-5：把「是不是完全重复」与「组内最大距离」如实带出去 —— 前端据此把
        # 近似组与完全重复组分开呈现，并且**不给近似组「一键全删」的入口**
        # （近似组里可能是不同图，全删 = 真丢图）。组太大时不作结论（maxdist=-1）。
        hs = [m[0] for m in g]
        md = 0
        if len(hs) <= 1000:
            for i in range(len(hs)):
                hi = hs[i]
                for j in range(i + 1, len(hs)):
                    d = bin(hi ^ hs[j]).count("1")
                    if d > md:
                        md = d
        else:
            md = -1
        out.append({"items": items, "exact": (md == 0), "maxdist": md})
    out.sort(key=lambda g: -len(g["items"]))
    return {"groups": out[:100], "scanned": len(files), "changedDuringScan": changed_during_scan,
            # ε-11（§6.4）：组数截断（>100 组丢弃）此前不带任何信号，
            # truncated 只反映扫描上限 —— 两种截断都要如实上报。
            "truncated": len(files) >= limit or len(out) > 100}


# ---------------------------------------------------------------- 目录签名（自动刷新）

_SIG = {"key": None, "at": 0.0, "sig": ""}
_SIG_LOCK = threading.Lock()
SIG_TTL = 1.5                      # 同一目录指纹的短缓存秒数（多标签页/多处轮询合并成一次遍历）


def dir_signature(path: Path, recursive: bool) -> str:
    """目录变更指纹：根 mtime + 各级目录 mtime + 媒体文件数。

    新增/删除文件必然改变所在目录的 mtime，因此无需逐文件 stat 即可检测变化，
    大目录轮询成本也只有目录遍历本身。SIG_TTL 内的重复请求直接复用上次结果。
    """
    key = (str(path), bool(recursive))
    now = time.time()
    with _SIG_LOCK:
        if _SIG["key"] == key and now - _SIG["at"] < SIG_TTL:
            return _SIG["sig"]
    sig = _dir_signature_calc(path, recursive)
    with _SIG_LOCK:
        _SIG.update({"key": key, "at": now, "sig": sig})
    return sig


def _dir_signature_calc(path: Path, recursive: bool) -> str:
    """Complete stat fingerprint; streaming memory and no media-body reads."""
    from backend.storage.directory_fingerprint import fingerprint
    try:
        return fingerprint(path,recursive,extensions=IMAGE_EXTS | VIDEO_EXTS)
    except OSError:
        return ""


# ---------------------------------------------------------------- 批量元数据 / 统计

def meta_batch(paths: list) -> dict:
    """批量提取轻量元数据 {path: {model, sampler, w, h}}（筛选面板用）。

    优先命中提示词索引（常驻内存，覆盖全量、秒回）；索引里没有的（新增文件、
    未参与索引的格式）再现场解析，现场解析上限 META_BATCH_MAX，避免首次打开
    筛选面板时把大目录整轮读一遍。
    """
    out = {}
    with IDX_LOCK:
        idx = dict(IDX["map"])
    pending = []
    for raw in paths:
        key = str(raw)
        e = idx.get(key)
        if not e:
            pending.append(key)
            continue
        w = h = None
        m = re.match(r"(\d+)\s*x\s*(\d+)", e.get("dim") or "")
        if m:
            w, h = int(m.group(1)), int(m.group(2))
        out[key] = {"model": e.get("model") or "", "sampler": e.get("sampler") or "",
                    "w": w, "h": h,
                    # 工作流分类/类型，与 nav_stats 的统计同一判定口径
                    "wfk": e.get("wfk") or "",
                    "wf": e.get("wfc") or ("prompt" if (e.get("pos") or e.get("model")
                                                         or e.get("sampler")) else "none")}
    for raw in pending[:META_BATCH_MAX]:
        try:
            p = Path(str(raw))
            if not p.is_file() or not inside_root(p):
                continue
            suf = p.suffix.lower()
            if suf == ".png":
                meta = png_meta(p)
            elif suf in (".jpg", ".jpeg", ".webp") and HAS_PIL:
                meta = parse_jpeg_meta(p)
            else:
                continue
            parsed = parse_meta(meta) or {}
            w = h = None
            m = re.match(r"(\d+) x (\d+)", meta.get("IHDR") or "")
            if m:
                w, h = int(m.group(1)), int(m.group(2))
            out[str(raw)] = {"model": parsed.get("model") or "",
                             "sampler": parsed.get("sampler") or "",
                             "w": w, "h": h,
                             "wfk": parsed.get("wfk") or "",
                             "wf": _wf_class(meta, parsed)}
        except (OSError, ValueError):
            continue
    # ε-11（§6.4）：现场解析被 META_BATCH_MAX 截断时必须给信号 —— 此前调用方
    # 无法区分「都没有元数据」与「超限没解析」，筛选维度会静默缺失。
    # 哨兵键放顶层扁平 dict 里（历史上 meta_batch 直接返回 path→meta 映射，
    # 改包一层会破坏两个消费方的合并语义）；前端聚合循环按「非对象跳过」兜住。
    out["__meta_truncated"] = max(0, len(pending) - META_BATCH_MAX)
    return out


# ---------------------------------------------------------------- 统一数量口径

CENSUS_TTL = 45.0                  # 目录普查缓存秒数（过期或换根目录后重扫）
_CENSUS = {"root": None, "at": 0.0, "data": None}
_CENSUS_LOCK = threading.Lock()


def _index_busy() -> bool:
    """提示词索引线程是否正在跑（全树遍历期间不宜再叠加一套遍历）。"""
    with IDX_LOCK:
        return bool(IDX.get("building"))


def census_invalidate():
    """删除/还原等改动文件的操作后调用，让下一次 /api/stats 重新扫描目录。"""
    with _CENSUS_LOCK:
        _CENSUS["data"] = None
        _CENSUS["at"] = 0.0


def media_census(root: str = "", max_age: float = CENSUS_TTL) -> dict:
    """扫描根目录统计图片/视频/格式分布，带 TTL 缓存。

    遍历规则与 _index_builder 保持一致（深度上限 10、realpath 去重防成环、
    跳过不可读目录）；差别是这里同时统计视频——视频不进提示词索引，
    所以「全部」口径必须单独走一遍目录，不能拿索引条数冒充。
    """
    root = root or STATE.get("root") or ""
    if not root:
        return {"root": "", "images": 0, "videos": 0, "total": 0, "formats": {}}
    now = time.time()
    with _CENSUS_LOCK:
        hot = (_CENSUS["root"] == root and _CENSUS["data"] is not None)
        if hot and now - _CENSUS["at"] < max_age:
            return dict(_CENSUS["data"])
        data = dict(_CENSUS["data"]) if hot else None
    if data is not None and _index_busy():
        # 索引线程正在全树扫描：这次先用旧数，避免两套遍历同时压磁盘（索引完下次请求自然更新）
        # ε-11（§6.4）：旧值必须带 stale 标记 —— 此前返回值与新鲜值无法区分，
        # 公共函数被别处直接消费时会被当成全量真值。
        return dict(data, stale=True)
    images = videos = 0
    formats: dict = {}
    unreadable = 0                  # 遍历中扫不动的目录数（含 root 自身）
    root_unreadable = False
    rootp = Path(root)
    stack = [(str(rootp), 0)]
    visited = {os.path.realpath(str(rootp))}
    while stack:
        cur, depth = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir():
                            if depth < 10:
                                rp = os.path.realpath(e.path)
                                if rp not in visited:
                                    visited.add(rp)
                                    stack.append((e.path, depth + 1))
                        else:
                            ext = os.path.splitext(e.name)[1].lower()
                            if ext in IMAGE_EXTS:
                                images += 1
                            elif ext in VIDEO_EXTS:
                                videos += 1
                            else:
                                continue
                            formats[ext] = formats.get(ext, 0) + 1
                    except OSError:
                        continue
        except OSError as ex:
            unreadable += 1
            if cur == str(rootp):
                root_unreadable = True
                log(f"[census] 根目录不可读（未挂载 / 无权限 / 已离线）：{rootp}："
                    f"{type(ex).__name__}: {ex}")
            continue
    # P0-16（CMV3-LOCAL-FIX，从旧工程回植）：根目录根本扫不动时，绝不能返回
    # total=0 冒充「一张图都没有」—— 那跟「用户真的清空了」在界面上完全不可区分，
    # 用户只会以为图丢了。带 error 字段如实上报，并**不写缓存**：下一次请求重新试，
    # 盘/权限一恢复，数字立刻回来（旧实现会把 0 缓存 45 秒，误导窗更长）。
    if root_unreadable:
        return {"root": root, "images": 0, "videos": 0, "total": 0, "formats": {},
                "stale": False, "unreadable": unreadable,
                "error": "根目录当前不可读（未挂载 / 无权限 / 已离线），数量不可用"}
    data = {"root": root, "images": images, "videos": videos,
            "total": images + videos, "formats": formats, "stale": False,
            "unreadable": unreadable}
    with _CENSUS_LOCK:
        _CENSUS.update({"root": root, "at": now, "data": data})
    return dict(data)


def nav_stats() -> dict:
    """左栏导航与筛选共用的统一数量口径（各处数字都从这里取，避免各算各的）。

    - total/images/videos：根目录下（含子目录）的全局口径，与「含子文件夹」开关无关
    - favorites：只统计文件仍在的收藏（收藏表里可能残留已删除/已移动的路径）
    - workflow：提示词索引中能解析出正向提示词或模型/采样器的图片数；索引未建完时为 0，
      前端据此显示占位（…）而不是假的 0
    - tags：与 /api/tags_list 同一份 tag_stats()，保证标签计数一致
    """
    cens = media_census()
    fav_alive = sum(1 for p in load_favs() if os.path.isfile(p))
    with IDX_LOCK:
        snap = list(IDX["map"].values())
        building, done, total = IDX["building"], IDX["done"], IDX["total"]
        idx_stale, idx_stale_reason = IDX.get("stale", False), IDX.get("stale_reason", "")
    wf_full = wf_prompt = wf_none = 0
    wfk = {"t2i": 0, "i2i": 0, "cn": 0, "other": 0}
    for e in snap:
        c = e.get("wfc")
        if c not in ("full", "prompt", "none"):      # 旧缓存条目：按字段兜底判定
            c = "prompt" if (e.get("pos") or e.get("model") or e.get("sampler")) else "none"
        if c == "full":
            wf_full += 1
        elif c == "prompt":
            wf_prompt += 1
        else:
            wf_none += 1
        k = e.get("wfk") or ""
        wfk[k if k in ("t2i", "i2i", "cn") else "other"] += 1
    marks = load_marks()
    recent_alive = sum(1 for e in load_recent() if os.path.isfile(e["path"]))
    marks_alive = sum(1 for p in marks if os.path.isfile(p))
    return {"total": cens["total"], "images": cens["images"], "videos": cens["videos"],
            "favorites": fav_alive, "formats": cens["formats"],
            "workflow": wf_full + wf_prompt, "wf_full": wf_full,
            "wf_prompt": wf_prompt, "wf_none": wf_none, "wfk": wfk,
            "recent": recent_alive, "marked": marks_alive, "marks": mark_stats(marks),
            "mark_map": {p: v.get("state") for p, v in marks.items() if v.get("state")},
            "indexed": len(snap),
            "index_building": building, "index_done": done, "index_total": total,
            # P0-1：索引本轮校验失败（根/子目录读不到）时如实上报，前端据此提示
            # 「显示的是上一次的结果」而不是假装一切正常 —— 这正是原先最误导的地方。
            "index_stale": bool(idx_stale), "index_stale_reason": idx_stale_reason,
            "tags": tag_stats()}


def stats_view() -> dict:
    """从提示词索引聚合出图统计。索引构建中时返回进度由前端提示。"""
    with IDX_LOCK:
        building = IDX["building"]
        done, total = IDX["done"], IDX["total"]
        snap = list(IDX["map"].items())
    if not snap:
        return {"building": building, "done": done, "total": total, "count": 0}
    days, models, samplers, dims = {}, {}, {}, {}
    nbytes = 0
    tmin = tmax = None
    for _path, e in snap:
        nbytes += e.get("size") or 0
        for src, dst in ((e.get("model"), models), (e.get("sampler"), samplers),
                         (e.get("dim"), dims)):
            if isinstance(src, str) and src:   # 非字符串（历史缓存里的连线引用）跳过，不能当 dict key
                dst[src] = dst.get(src, 0) + 1
        mt = e.get("mtime") or 0
        if mt <= 0:
            # ε-11（§6.4）：mtime 缺失/为 0 的条目不进时间桶（bytes / Top10 照常统计）——
            # 此前把统计起点拉到 1970-01-01，span 与 by_day 横轴全被撑爆。
            continue
        t = time.localtime(mt)
        day = f"{t.tm_year:04}-{t.tm_mon:02}-{t.tm_mday:02}"
        days[day] = days.get(day, 0) + 1
        if tmin is None or mt < tmin:
            tmin = mt
        if tmax is None or mt > tmax:
            tmax = mt
    span = ([time.strftime("%Y-%m-%d", time.localtime(tmin)),
             time.strftime("%Y-%m-%d", time.localtime(tmax))] if tmin is not None else None)
    return {"building": building, "done": done, "total": total,
            "count": len(snap), "bytes": nbytes, "span": span,
            "by_day": sorted(days.items())[-30:],
            "by_model": sorted(models.items(), key=lambda x: -x[1])[:10],
            "by_sampler": sorted(samplers.items(), key=lambda x: -x[1])[:10],
            "by_dim": sorted(dims.items(), key=lambda x: -x[1])[:10]}


# ---------------------------------------------------------------- 收藏夹视图

# ---------------------------------------------------------------- 最近使用 / 已标记

RECENT_FILE = DATA_DIR / "recent.json"
MARKS_FILE = DATA_DIR / "marks.json"
RECENT_MAX = 200                    # 只保留最近 200 条（按路径去重）
RECENT_LOCK = threading.Lock()
MARKS_LOCK = threading.Lock()
RECENT_ACTIONS = {"preview": "预览", "comfy": "打开 ComfyUI", "download": "下载",
                  "copy": "复制提示词", "folder": "打开位置"}
# 标记与收藏分开：收藏=长期保留，标记=临时工作状态
MARK_STATES = (("review", "待处理"), ("publish", "待发布"),
               ("compare", "待比较"), ("delete", "待删除"))
MARK_LABELS = dict(MARK_STATES)


def _save_json(path: Path, obj) -> bool:
    """原子写 JSON（先写临时文件再替换），失败只记日志不抛。"""
    try:
        if _SQL_BULK is not None and path == MARKS_FILE:
            _SQL_BULK.replace_marks(obj)
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{_tmp_tag()}.tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(path)
        return True
    except Exception as ex:
        log(f"[warn] 写入失败 {path.name}: {type(ex).__name__}: {ex}")
        return False


def load_recent() -> list:
    """最近使用 [{path, action, time}]，最新在前。"""
    if _SQL_RECENT is not None:
        return [{"path": row["path"], "action": row["action"], "time": int(row["time"])}
                for row in _SQL_RECENT.read(_SQL_ORG.profile_uuid, limit=RECENT_MAX)]
    out = []
    try:
        data = json.loads(RECENT_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return out
    if isinstance(data, list):
        for e in data:
            if isinstance(e, dict) and e.get("path"):
                out.append({"path": str(e["path"]),
                            "action": str(e.get("action") or ""),
                            "time": int(e.get("time") or 0)})
    return out


def recent_add(path: str, action: str) -> dict:
    """记录一次使用：按路径去重（保留最新一条），最多 RECENT_MAX 条。"""
    key = str(Path(path).resolve())
    act = action if action in RECENT_ACTIONS else "preview"
    if _SQL_RECENT is not None:
        target = Path(key)
        asset = _SQL_CATALOG.observe(STATE["root"], target, target.stat())
        _SQL_RECENT.add(_SQL_ORG.profile_uuid, asset["mediaId"], act)
        return {"ok": True, "count": len(load_recent())}
    with RECENT_LOCK:
        items = [e for e in load_recent() if e["path"] != key]
        items.insert(0, {"path": key, "action": act, "time": int(time.time())})
        del items[RECENT_MAX:]
        if not _save_json(RECENT_FILE, items):     # P1-4：不写成功就别报成功
            return {"ok": False, "count": len(items),
                    "error": "最近使用记录写入失败（磁盘不可写或被占用）"}
    return {"ok": True, "count": len(items)}


def recent_clear() -> dict:
    if _SQL_RECENT is not None:
        _SQL_RECENT.clear(_SQL_ORG.profile_uuid)
        return {"ok": True, "count": 0}
    with RECENT_LOCK:
        if not _save_json(RECENT_FILE, []):        # P1-4：清空没落盘 ⇒ 界面不能报「已清空」
            return {"ok": False, "count": 0,
                    "error": "最近使用记录清空失败（磁盘不可写或被占用）"}
    return {"ok": True, "count": 0}


def load_marks() -> dict:
    """标记表 {路径: {state, updated}}。"""
    if _SQL_ORG is not None:
        return {path: {"state": value["state"], "updated": int(value["updated"] or 0)}
                for path, value in _SQL_ORG.projection()["marks"].items()}
    try:
        data = json.loads(MARKS_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict) and v.get("state") in MARK_LABELS:
                out[str(k)] = {"state": str(v["state"]),
                               "updated": int(v.get("updated") or 0)}
    return out


def mark_stats(marks: dict = None) -> list:
    """各状态计数 [{state, label, count}]，顺序固定便于左栏展示。

    ε-11（§6.4）：只统计**文件仍在**的标记 —— 与 nav_stats 的 marked / favorites
    同一口径。此前全量统计（含文件已删/已移走的），左栏数字与点进去的列表对不上。
    """
    m = load_marks() if marks is None else marks
    cnt = {}
    for p, v in m.items():
        s = v.get("state")
        if s and os.path.isfile(p):
            cnt[s] = cnt.get(s, 0) + 1
    return [{"state": st, "label": lab, "count": cnt.get(st, 0)}
            for st, lab in MARK_STATES]


def mark_set(path: str, state: str) -> dict:
    """设置或清除标记；state 为空/"none" 表示取消。"""
    key = str(Path(path).resolve())
    st = (state or "").strip()
    with MARKS_LOCK:
        m = load_marks()
        if not st or st == "none":
            m.pop(key, None)
            cur = ""
        elif st in MARK_LABELS:
            m[key] = {"state": st, "updated": int(time.time())}
            cur = st
        else:
            return {"ok": False, "error": "未知的标记状态"}
        if not _save_json(MARKS_FILE, m):          # P1-4：没落盘就报失败，别让角标闪完就没了
            return {"ok": False, "state": cur, "marks": mark_stats(m),
                    "error": "标记写入失败（磁盘不可写或被占用）"}
    return {"ok": True, "state": cur, "marks": mark_stats(m)}


def _pack_media(path: str, extra: dict = None, nctx=None, *, identity=None, file_stat=None):
    """把一个绝对路径打包成网格卡片用的 item；不存在/非媒体返回 None。

    nctx 可由调用方传入一份 NSFW 快照（列表循环里复用同一份，省掉逐条取锁）；
    不传就自己取一份，单条调用场景下这点成本可以忽略。
    """
    p = Path(path)
    try:
        st = file_stat if file_stat is not None else p.stat()
        if not stat_mod.S_ISREG(st.st_mode):
            return None
    except OSError:
        return None
    ext = p.suffix.lower()
    t = "image" if ext in IMAGE_EXTS else ("video" if ext in VIDEO_EXTS else None)
    if not t:
        return None
    _nl, _ns = nsfw_fields(str(p), int(st.st_mtime), st.st_size, nctx, identity)
    it = {"name": p.name, "path": str(p), "type": t, "size": st.st_size,
          "mtime": int(st.st_mtime),
          "playable": t == "image" or ext in PLAYABLE_VIDEO,
          "png": ext == ".png", "fav": False, "favcat": None,
          # 收藏视图 / 最近使用 / 已标记全走这里，补一处就全覆盖 ——
          # 漏掉的话会出现「网格里是模糊的、收藏夹里却是原图」这种自相矛盾的界面。
          # （回收站不走 _pack_media，是**有意**的：那是管理视图，用户刚删的图要能看清再决定还原。）
          "n": _nl, "ns": _ns}
    if identity:
        it.update(mediaId=identity["mediaId"], revisionId=identity["revisionId"],
                  expectedVersion=identity["contentVersion"])
    if extra:
        it.update(extra)
    return it


def recent_view(limit: int = 0) -> list:
    """最近使用视图（跳过已不存在的文件），附带收藏/标签/动作信息。"""
    favs, tags = load_favs(), load_tags()
    prepared = []
    for e in load_recent():
        p = Path(e["path"])
        try:
            st = p.stat()
        except OSError:
            continue
        if not stat_mod.S_ISREG(st.st_mode) or p.suffix.lower() not in IMAGE_EXTS | VIDEO_EXTS:
            continue
        prepared.append((e, p, st))
        if limit and len(prepared) >= limit:
            break
    if not prepared:
        return []
    identities = (_SQL_CATALOG.observe_many(STATE["root"], [(p, st) for _, p, st in prepared])
                  if _SQL_CATALOG is not None else [None] * len(prepared))
    nctx = nsfw_ctx()
    out = []
    for (e, p, st), identity in zip(prepared, identities):
        it = _pack_media(e["path"], nctx=nctx, identity=identity, file_stat=st)
        if not it:
            continue
        it["fav"] = e["path"] in favs
        it["favcat"] = (favs.get(e["path"]) or {}).get("cat")
        it["tags"] = sorted(tags.get(e["path"], []))
        it["ract"] = e["action"]
        it["rtime"] = e["time"]
        out.append(it)
    return out


def marks_view(state: str = "") -> list:
    """已标记视图（按标记时间倒序），可按状态过滤。"""
    favs, tags, marks = load_favs(), load_tags(), load_marks()
    out = []
    for path, v in marks.items():
        if state and state != "*" and v.get("state") != state:
            continue
        it = _pack_media(path)
        if not it:
            continue
        it["fav"] = path in favs
        it["favcat"] = (favs.get(path) or {}).get("cat")
        it["tags"] = sorted(tags.get(path, []))
        it["mark"] = v.get("state")
        it["marklabel"] = MARK_LABELS.get(v.get("state"), "")
        it["marktime"] = v.get("updated") or 0
        out.append(it)
    out.sort(key=lambda x: -x["marktime"])
    return out


def fav_view(cat: str = "") -> list:
    """跨目录列出所有仍存在的收藏 [{item}]，按分类+时间排序。

    注意：这里的 item 是**手搓**的，不走 _pack_media（收藏表里已经存了分类，
    _pack_media 会把 fav/favcat 覆盖成初始值）。所以新加的字段这里要单独补一份 ——
    V2.11.0 就漏过 n 字段，表现是「网格里糊了、收藏夹里是原图」（验证脚本抓到的）。
    """
    favs = load_favs()
    tags = load_tags()                          # P1：下面的 item 要带 tags（见下）
    out = []
    _nctx = nsfw_ctx()                          # 循环外取一份，别每条都取锁
    for path, v in favs.items():
        c = v.get("cat") or "默认"
        if cat and cat != "*" and c != cat:
            continue
        try:
            p = Path(path)
            if not p.is_file():
                continue
            st = p.stat()
        except OSError:
            continue
        ext = p.suffix.lower()
        t = "image" if ext in IMAGE_EXTS else ("video" if ext in VIDEO_EXTS else None)
        if not t:
            continue
        _nl, _ns = nsfw_fields(path, int(st.st_mtime), st.st_size, _nctx)
        out.append({"name": p.name, "path": path, "type": t,
                    "size": st.st_size, "mtime": int(st.st_mtime),
                    "playable": t == "image" or ext in PLAYABLE_VIDEO,
                    "png": ext == ".png", "fav": True, "favcat": c,
                    # P1（CMV3-LOCAL-FIX，2026-10-09 全代码审计）：/api/list、
                    # /api/tags_filter、/api/search 都带 tags，唯独手搓的这里漏了 ——
                    # `tag:xxx` 在收藏夹视图里恒筛空（queryFilter 的 tag 分支
                    # `have=(it.tags||[])` 拿到 []），大图标签面板也显示 0 个，
                    # 用户以为标签丢了。与 recent_view 同口径补上。
                    # （同 V2.11.0 漏 n 字段一类，见 docstring。）
                    "tags": sorted(tags.get(path, [])),
                    "n": _nl, "ns": _ns})
    out.sort(key=lambda x: (x["favcat"], -x["mtime"]))
    return out


def _exif_transpose_bytes(ext: str, data: bytes):
    """ε-11（§6.4）：EXIF Orientation≠1 时把像素转正（返回新 bytes）；无则返回 None。

    剥元数据默认「像素不重编码」，但 Orientation 标记随 APP1/eXIf/EXIF 块一起被剥掉，
    导出的 JPEG/WebP 在其它查看器里会旋转 90°/180°。仅在确有旋转标记时才走 PIL
    重编码把方向物化进像素（ComfyUI 生成图无 EXIF，零开销）；任何异常返回 None
    （fail-safe，保持原行为）。
    """
    if not HAS_PIL or ext not in (".jpg", ".jpeg", ".webp", ".png"):
        return None
    try:
        from PIL import ImageOps
        with Image.open(io.BytesIO(data)) as im:
            exif = im.getexif()
            if not exif or int(exif.get(274, 1) or 1) == 1:
                return None
            fixed = ImageOps.exif_transpose(im)
            out = io.BytesIO()
            if ext == ".png":
                fixed.save(out, "PNG")
            elif ext == ".webp":
                fixed.save(out, "WEBP", quality=95)
            else:
                if fixed.mode not in ("RGB", "L"):
                    fixed = fixed.convert("RGB")
                fixed.save(out, "JPEG", quality=95)
            return out.getvalue()
    except Exception:
        return None


def _make_stripper(ext: str, fn):
    """ε-11：包一层「先转正再剥」—— 三个调用点（批量导出 / 下载清理 / 还原清理）统一生效。"""
    def _f(data: bytes) -> bytes:
        fixed = _exif_transpose_bytes(ext, data)
        if fixed is not None:
            data = fixed
        return fn(data)
    return _f


_STRIP_BY_EXT = {ext: _make_stripper(ext, fn) for ext, fn in
                 {".png": strip_png_meta, ".webp": strip_webp_meta,
                  ".jpg": strip_jpg_meta, ".jpeg": strip_jpg_meta,
                  ".mp4": strip_mp4_meta, ".gif": strip_gif_meta}.items()}


def batch_export(paths: list, dest: str, verified_targets=None) -> dict:
    """把多个文件复制到目标目录：图片剥离生成元数据，其余原样复制。

    目标目录不存在则创建（含多级）；目标已存在同名文件时加 _1/_2 后缀。
    ε-14（§6.8）：dest **有意不校验 inside_root** —— 导出语义就是写到用户指定的
    任意目录（U 盘、下载目录等，审计 §5.1 已从 P1 降级为 P2 留档）。来源侧
    paths 逐条过 inside_root，风险面只剩「用户自己选的目标」。
    """
    destp = Path(dest.strip().strip('"'))
    if not destp.is_absolute():
        return {"ok": False, "error": "请输入绝对路径（如 D:\\export）"}
    try:
        destp.mkdir(parents=True, exist_ok=True)
    except OSError as ex:
        return {"ok": False, "error": f"目标目录无法创建: {ex}"}
    done, failed = 0, []
    from backend.storage.media_stream import verified_stream, MediaStreamConflict
    from backend.storage.save_download import save_download
    from io import BytesIO
    for index, raw in enumerate(paths):
        try:
            p = Path(str(raw))
            if not p.is_file() or not inside_root(p):
                failed.append({"name": os.path.basename(str(raw)),
                               "error": "文件不存在或在根目录之外"})
                continue
            ext = p.suffix.lower()
            identity = verified_targets[index] if verified_targets else None
            context = verified_stream(p, identity["contentHash"]) if identity else p.open("rb")
            with context as opened:
                stream = opened[0] if identity else opened
                if ext in _STRIP_BY_EXT:
                    stream = BytesIO(_STRIP_BY_EXT[ext](stream.read()))
                save_download(destp, p.name, stream)
            done += 1
        except (OSError, MediaStreamConflict, WorkBusy) as ex:
            failed.append({"name": os.path.basename(str(raw)), "error": str(ex)})
    if done:
        with SETTINGS_LOCK:
            s = load_settings()
            s["last_export_dir"] = str(destp.resolve())   # 记住上次目录，下次默认填
            save_settings(s)
    return {"ok": not failed, "done": done, "failed": failed,
            "dest": str(destp.resolve())}


# ---------------------------------------------------------------- handler

HTTP_4XX_PER_MIN = 60


HTTP_4XX = {"min": 0, "n": 0, "drop": 0}


class Handler(BaseHTTPRequestHandler):
    server_version = "MediaViewer/" + APP_VERSION
    protocol_version = "HTTP/1.1"
    # ε-12（§6.3 · CMV3-LOCAL-FIX 回植）：socket 超时（读写都生效）。此前无超时 ——
    # 一个建立连接后既不发完请求头也不关的客户端，会永久占住 ThreadingHTTPServer 的
    # 一个线程；多个慢连接即可把线程池耗尽（DoS 不需要恶意，浏览器崩溃也会留下这种
    # 半开连接）。120s：正常请求毫秒级完成；大图流式响应里客户端暂停超过 2 分钟才允许
    # 断（恢复播放时 <video> 会自动按 range 重连，无感）。
    timeout = 120

    def log_message(self, fmt, *args):
        """§6.1：此前恒 pass ⇒ 服务端零访问日志，出问题只能靠猜。
        只记 4xx/5xx（正常的 2xx 不记）。

        ⚠️ 但「都记」会带来新问题：缺缩略图 / 文件已删这类 404 在滚屏与索引期
        可能成百上千次，而 log() 的轮转只留 1 个备份（2MB）⇒ 会把更有价值的日志挤掉，
        **等于把「没有日志」换成「日志里全是噪音」**。故：
          · 5xx 不限流（每次都记）；
          · 4xx 按分钟窗口限流（HTTP_4XX_PER_MIN 条/分），溢出时补一条汇总说明。
        """
        try:
            msg = fmt % args if args else str(fmt)
        except Exception:
            msg = str(fmt)
        is5 = any(t in msg for t in ('" 5', "code 5"))
        is4 = any(t in msg for t in ('" 4', "code 4"))
        if not (is4 or is5):
            return
        if is5:
            log("[http] " + msg)
            return
        now = int(time.time() // 60)
        if HTTP_4XX["min"] != now:
            if HTTP_4XX["drop"]:
                log("[http] 上一分钟另有 %d 条 4xx 未逐条记录（已限流）" % HTTP_4XX["drop"])
            HTTP_4XX["min"] = now
            HTTP_4XX["n"] = 0
            HTTP_4XX["drop"] = 0
        if HTTP_4XX["n"] >= HTTP_4XX_PER_MIN:
            HTTP_4XX["drop"] += 1
            return
        HTTP_4XX["n"] += 1
        log("[http] " + msg)

    # ---- helpers -------------------------------------------------------

    def _host_ok(self) -> bool:
        """只响应本机 Host，防 DNS 重绑定让外网站点读本机文件（LAN 模式除外）。"""
        if STATE.get("lan"):
            return True
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip().lower()
        return host in ("", "127.0.0.1", "localhost", "[::1]")

    def _auth_ok(self, qs) -> bool:
        """LAN 模式下校验 token（query 或 cookie）。本机模式恒通过。

        token 比较用 secrets.compare_digest（常量时间），避免按字符提前返回泄漏前缀。
        """
        if not STATE.get("lan"):
            return True
        tok = STATE.get("token") or ""
        if not tok:
            return False
        got = qs.get("token", [""])[0]
        if got and secrets.compare_digest(got, tok):
            return True
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "cmv_token":
                return bool(v) and secrets.compare_digest(v, tok)
        return False

    _LAN_GATE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>访问验证</title></head>
<body style="margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
background:#131314;color:#e8e6e3;font:14px/1.6 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif">
<div style="background:#1d1d1f;border:1px solid #333338;border-radius:12px;padding:28px;width:300px;text-align:center">
<div style="margin-bottom:14px;font-size:15px">ComfyMediaViewer 局域网访问</div>
<input id="t" placeholder="输入访问令牌" style="width:100%;box-sizing:border-box;font:inherit;
padding:8px 10px;background:#26262a;border:1px solid #333338;border-radius:8px;color:#e8e6e3;outline:none">
<button onclick="go()" style="margin-top:12px;width:100%;font:inherit;padding:8px;background:#4a90d9;
color:#fff;border:0;border-radius:8px;cursor:pointer">进入</button></div>
<script>function go(){var v=document.getElementById('t').value.trim();
if(v)location.href='/?token='+encodeURIComponent(v)}
document.getElementById('t').addEventListener('keydown',function(e){if(e.key==='Enter')go()})</script>
</body></html>"""

    def _lan_gate(self):
        """LAN 鉴权门：未通过验证时给出 401 网关页（输入令牌后带 ?token= 重进）。"""
        body = self._LAN_GATE.encode("utf-8")
        self.send_response(401)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200, cors=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cors:
            # 桥接端点专用：只有明确放行的来源才回 CORS 头，其余一概不回
            self.send_header("Access-Control-Allow-Origin", cors)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def _cors_origin(self):
        """桥接端点的跨源许可：只放行环回来源（ComfyUI 端口可变，故不限端口）。

        返回可回写的 Origin，空/不放行时返回 None。不回 * 是刻意的：
        域名来源（含公网页面）拿不到 CORS 头，就读不到响应体。
        """
        o = (self.headers.get("Origin") or "").rstrip("/")
        if not o:
            return None
        try:
            host = (urllib.parse.urlparse(o).hostname or "").lower()
        except Exception:
            return None
        return o if host in ("127.0.0.1", "localhost", "::1") else None

    def _send_file(self, p: Path, download=False, check_root=True, clean=False, frozen=None):
        if frozen is None and (not p.is_file() or (check_root and not inside_root(p))):
            return self._json({"error": "not found or outside root"}, 404)
        ext = p.suffix.lower()
        if ext not in MEDIA_EXTS:
            # 非媒体（.html/.svg/.txt/.lnk…）一律当附件下载，避免导航到
            # /api/file?p=… 时被同源内联渲染执行脚本
            download = True
        if clean and download and ext in _STRIP_BY_EXT:
            try:
                data = _STRIP_BY_EXT[ext](frozen[0].read() if frozen is not None else p.read_bytes())
                name = p.stem + "_clean" + p.suffix
                self.send_response(200)
                self.send_header("Content-Type",
                                 mimetypes.guess_type(str(p))[0] or "application/octet-stream")
                self.send_header("X-Content-Type-Options", "nosniff")
                # 如实告知「这一份真的抹过了」：前端按它说话（.avi/.mkv 之类不在
                # 白名单时后端走原始下载，前端就不会再谎报「元数据已抹除」）
                self.send_header("X-Clean-Applied", "1")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{urllib.parse.quote(name)}"')
                self.end_headers()
                self.wfile.write(data)
                return
            except OSError:
                pass                              # 读取失败 → 回退原始下载
        if frozen is not None:
            frozen[0].seek(0)
        # P0-4：is_file() 与 stat() 之间存在 TOCTOU 窗口（外接盘掉线 / 文件被删），
        # stat 抛的 OSError 在头发出前 ⇒ 同样走单份 404 报文，不触发兜底二次报文。
        # 内容校验器：同名文件被重建（删除后重新生成到同一文件名）时 URL 不变，
        # 必须让缓存能自己发现内容已换，否则浏览器会一直拿旧图（配合前端 &v= 版本号双保险）
        # 图片按实际字节生成强 ETag。视频保留 stat 版本以避免每个 Range 请求
        # 都读完整部视频；前端会把未严格校验的视频明确标记为尽力缓存。
        # ⚠️ `image_content_version` 同样要**打开文件读全量字节**（v3 强 ETag）⇒
        #    独占句柄 / 外接盘掉线时它也抛 OSError，必须一并纳入这条守卫，
        #    否则失败会冒泡到 do_GET 兜底变成 500（= P0-4 的语义退化）。
        try:
            stt = frozen[1] if frozen is not None else p.stat()
            size = stt.st_size
            etag = (f'"{frozen[2]}"' if frozen is not None
                    else f'"{image_content_version(p)}"' if ext in IMAGE_EXTS
                    else f'"{stt.st_mtime_ns}-{size}"')
        except OSError:
            return self._json({"error": "file unreadable"}, 404)
        ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        last_mod = self.date_time_string(stt.st_mtime)
        rng = self.headers.get("Range")
        if not rng:                       # 带 Range 的（视频拖进度）不做 304，交给下面的分段逻辑
            inm = (self.headers.get("If-None-Match") or "").strip()
            fresh = False
            if inm:
                fresh = inm == "*" or etag in [t.strip() for t in inm.split(",")]
            else:
                ims = self.headers.get("If-Modified-Since")
                if ims:
                    try:
                        _t = email.utils.parsedate_to_datetime(ims)
                        fresh = _t is not None and int(stt.st_mtime) <= int(_t.timestamp())
                    except (TypeError, ValueError):
                        fresh = False
            if fresh:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", "0")   # 显式给长度，避免 keep-alive 下等 body 挂死
                self.end_headers()
                return
        start, end = 0, size - 1
        partial = False
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                elif m.group(2):                      # suffix range: bytes=-N
                    start = max(0, size - int(m.group(2)))
                partial = True
        if start >= size or start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("X-Content-Type-Options", "nosniff")
            # 必须显式给 Content-Length：本服务是 HTTP/1.1 + keep-alive，
            # 无长度又无响应体时客户端会一直等响应体直到连接关闭（永不发生）→ 请求挂死
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        length = end - start + 1
        # P0-4（审计二批次·第 3 级）：**先确认可读、再发响应头**。
        # 原实现是 end_headers() 之后才 open()，而 open 抛的 OSError
        # （FileNotFoundError / PermissionError / 外接盘掉线 / OneDrive 占位）不在
        # 当时的捕获集里 ⇒ 异常冒泡到 do_GET 兜底再发一份 500 报文 ⇒ 客户端先收到
        # 「200 + Content-Length: N」却零字节，body 变成**第二份 HTTP 报文**，
        # keep-alive 同连接出现多个状态行、后续响应错位配发（协议破坏，已实测）。
        # 修法：头发出之前就把文件打开，打不开就走单份 404 报文路径。
        # 命名说明：frozen 的字节流是调用方（`with … as frozen:`）冻结并校验过的，
        # 生命期归调用方 ⇒ 用 nullcontext 包一层后**同样叫 f**，于是下面只有一条
        # `with f:` 路径，且 frozen[0] 不会被本函数提前关闭。
        from contextlib import nullcontext
        try:
            f = nullcontext(frozen[0]) if frozen is not None else open(p, "rb")
        except OSError:
            return self._json({"error": "open failed"}, 404)
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-cache")      # 复用前必须校验：内容可能被同名重建覆盖
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", last_mod)
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download:
            self.send_header("Content-Disposition",
                             f'attachment; filename="{urllib.parse.quote(p.name)}"')
        self.end_headers()
        try:
            with f:
                fh = f if frozen is None else frozen[0]
                fh.seek(start)
                left = length
                while left > 0:
                    chunk = fh.read(min(CHUNK, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except OSError:
            # P0-4：写循环里也可能抛 OSError（盘掉线 / 读取中途失败）。此时头已发出，
            # 无法再改状态码，但**必须吞掉**，否则同样会触发 do_GET 兜底的第二次报文。
            # ε-14（§6.8）：BrokenPipe / ConnectionReset / ConnectionAborted 都是
            # OSError 的子类，被本条一并覆盖 —— 客户端（下载器/杀软中间人）强杀连接时
            # Windows 抛的是 ConnectionAborted 而不是 ConnectionReset，漏捕会刷 ERROR 日志。
            pass

    def _page(self):
        html = page_file().read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.send_header("Cache-Control", "no-store")   # 页面随 exe 更新，禁用浏览器缓存
        self.end_headers()
        self.wfile.write(html)

    def _static(self, path: str):
        """/css/ /js/ /assets/ /bridge/ 静态资源：走条件请求（304），取代原 no-store。

        （CMV3-LOCAL-FIX，从旧工程 server.py 回植 · 批次 E-P5 2026-10-06）
        原先这四个前缀一律 `no-store`，每次启动/F5 都要重下 731KB / 25 个文件（实测）。
        改成 `no-cache` + ETag/Last-Modified 后，内容未变的文件在浏览器缓存里直接复用
        （回 304 空体），省掉的是传输与解析。

        ⚠️ 为什么**不能**用 `max-age=...` 长期缓存：这些文件名**不带内容 hash**
        （`app.js` 而非 `app.<hash>.js`），换版本后浏览器会一直拿旧 JS ⇒ 症状是
        「界面没更新」甚至白屏，用户会当成 exe 坏了。`no-cache` 每次都回源校验，
        是这个命名方式下唯一安全的做法。要消除请求数就必须先给文件名加 hash。

        ETag 用 mtime_ns + size，与 `_send_file` 同口径：源码模式下改完 JS 即失效
        （开发态 F5 立刻看到新内容），打包后 exe 换了 JS 也必然对不上。
        """
        fp = frontend_file(path)
        if not fp:
            return self._json({"error": "not found"}, 404)
        try:
            stt = fp.stat()
        except OSError:
            return self._json({"error": "not found"}, 404)
        etag = f'"{stt.st_mtime_ns}-{stt.st_size}"'
        last_mod = self.date_time_string(stt.st_mtime)
        inm = (self.headers.get("If-None-Match") or "").strip()
        fresh = False
        if inm:
            fresh = inm == "*" or etag in [t.strip() for t in inm.split(",")]
        else:
            ims = self.headers.get("If-Modified-Since")
            if ims:
                try:
                    _t = email.utils.parsedate_to_datetime(ims)
                    fresh = _t is not None and int(stt.st_mtime) <= int(_t.timestamp())
                except (TypeError, ValueError):
                    fresh = False
        if fresh:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", last_mod)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", "0")   # 显式给长度，避免 keep-alive 下等 body 挂死
            self.end_headers()
            return
        data = fp.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type",
                         STATIC_TYPES.get(fp.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")   # 复用前必须校验：文件名不带 hash
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", last_mod)
        self.end_headers()
        self.wfile.write(data)

    # ---- GET -----------------------------------------------------------

    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "forbidden host"}, 403)
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        # LAN 模式：页面路径带有效令牌 → 种 cookie 后 302 到干净 URL（令牌不留在地址栏）
        tok = qs.get("token", [""])[0]
        if (STATE.get("lan") and tok and u.path in ("/", "/index.html")
                and secrets.compare_digest(tok, str(STATE.get("token") or ""))):
            self.send_response(302)
            self.send_header("Set-Cookie",
                             f"cmv_token={tok}; Path=/; Max-Age=31536000; SameSite=Strict; HttpOnly")
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not self._auth_ok(qs):
            return self._lan_gate()
        if MIGRATION_PROTECT:
            if u.path == "/api/info":
                return self._json({"title": APP_TITLE, "version": APP_VERSION,
                                   "root": STATE["root"], "data_dir": str(DATA_DIR),
                                   "data_mode": DATA_MODE, "storageMode": "legacy",
                                   "migrationProtected": True,
                                   # P0-11（CMV3-LOCAL-FIX 回植）：如实自报 pid。桌面壳靠它做
                                   # 退出兜底 —— 壳内「重启程序」= 后端自起新进程接管同端口，
                                   # 壳登记的旧 pid 已成死号（taskkill 必失败且错误被吞），
                                   # 只能按端口反查真身。保护态下壳同样在跑，缺它兜底会退化
                                   # 成「取不到就不杀」⇒ 新后端常驻成孤儿并锁住 backend exe。
                                   "pid": os.getpid(),
                                   # B1-3（CMV3-LOCAL-FIX）：保护态下同样如实上报数据目录可写性
                                   # —— 门禁 `p1fix_datasafe_20261009_check` 的 A3 判据取的是
                                   # 源码里**第一个** `/api/info` 块，而这是它（迁移保护分支）。
                                   # 语义上也该有：搬迁前用户最需要知道目标目录能不能写。
                                   "data_write": data_write_state(),
                                   "capabilities": {"organizationWrites": False}})
            return self._json({"error": "migration protection is active",
                               "code": "MIGRATION_PROTECTED"}, 503)
        # P1-38：带写副作用的 GET（桥接取件/回执）拒绝跨站请求。
        # 这些端点原先只在 _host_ok（只认 Host，而 Host 由 URL 决定、攻击者可控）
        # 之下裸奔 ⇒ 任意外网页面 <img src="http://127.0.0.1:8765/api/pull">
        # 即可取走并销毁一件排队的工作流。
        if u.path in self.WRITE_GET_PATHS and self._cross_site_write_get():
            log("[security] 拒绝跨站写 GET：%s" % u.path)
            return self._json({"error": "forbidden"}, 403)
        # B8-P1-1（CMV3-LOCAL-FIX，2026-10-10）：敏感 GET（目录枚举）同款拒跨站。
        if u.path in self.SENSITIVE_GET_PATHS and self._cross_site_write_get():
            log("[security] 拒绝跨站敏感 GET：%s" % u.path)
            return self._json({"error": "forbidden"}, 403)
        try:
            if u.path in ("/", "/index.html"):
                return self._page()
            if u.path == '/api/v2/classic/resolve-media':
                from backend.storage.classic_media import resolve_media
                from backend.storage.catalog import IdentityConflictError, IdentityNotFoundError
                try:
                    if _SQL_CATALOG is None or set(qs)-{'mediaId','revisionId','token'} \
                            or len(qs.get('mediaId',[]))!=1 or any(len(v)!=1 for v in qs.values()):
                        raise ValueError('需要明确的媒体和文件版本')
                    return self._json(resolve_media(_SQL_CATALOG,qs['mediaId'][0],qs.get('revisionId',[None])[0]))
                except IdentityConflictError as error:
                    return self._json({'error':str(error),'code':'media_conflict'},409)
                except IdentityNotFoundError as error:
                    return self._json({'error':str(error),'code':'media_not_found'},404)
                except (ValueError,OSError) as error:
                    return self._json({'error':str(error)},400)
            if u.path in ('/api/v2/classic/data','/api/v2/classic/data/job'):
                from backend.storage.classic_data import list_jobs,read_job
                try:
                    if u.path.endswith('/job'):
                        if set(qs)-{'kind','jobId','token'} or len(qs.get('kind',[]))!=1 or len(qs.get('jobId',[]))!=1:
                            raise ValueError('需要明确的任务类型和编号')
                        job=read_job(PORTABLE_FLAG,DATA_DIR,qs['kind'][0],qs['jobId'][0])
                        return self._json({'job':job,'runtimeVerified':False})
                    return self._json({'current':{'dataDir':str(DATA_DIR),'storageMode':'sqlite' if _SQL_ORG else 'legacy-json',
                                                 'selector':str(PORTABLE_FLAG)},
                        'legacySource':{'available':True,'reason':''},'jobs':list_jobs(PORTABLE_FLAG,DATA_DIR)})
                except (ValueError,OSError,RuntimeError) as error:
                    return self._json({'error':str(error)},400)
            if u.path in ('/api/v2/workflows/families','/api/v2/workflows/versions',
                          '/api/v2/workflows/detail','/api/v2/workflows/captures',
                          '/api/v2/workflows/export','/api/v2/workflows/definitions',
                          '/api/v2/workflows/definition','/api/v2/workflows/capture-identity',
                          '/api/v2/workflows/candidates','/api/v2/workflows/sources',
                          '/api/v2/workflows/source','/api/v2/workflows/source-bindings'):
                if _SQL_WORKFLOWS is None:
                    return self._json({'error':'工作流库需要数据库模式','code':'CAPABILITY_UNAVAILABLE'},501)
                from backend.metadata.workflow import WorkflowIdentityError, WorkflowFormatError
                from backend.metadata.workflow_source_store import WorkflowSourceStoreError, WorkflowSourceStoreConflict
                from backend.metadata.workflow_source_bindings import WorkflowSourceBindingError, WorkflowSourceBindingConflict
                try:
                    after=int(qs.get('afterId',['0'])[0])
                    limit=int(qs.get('limit',['50'])[0])
                    version=qs.get('versionUuid',[''])[0]
                    if u.path.endswith('/families'):
                        result=_SQL_WORKFLOWS.families(after_id=after,limit=limit)
                    elif u.path.endswith('/versions'):
                        result=_SQL_WORKFLOWS.versions(qs.get('workflowUuid',[''])[0],after_id=after,limit=limit)
                    elif u.path.endswith('/detail'):
                        result=_SQL_WORKFLOWS.read_version(version)
                    elif u.path.endswith('/captures'):
                        result=_SQL_WORKFLOWS.captures(version,after_id=after,limit=limit)
                    elif u.path.endswith('/definitions'):
                        result=_SQL_WORKFLOWS.definitions(qs.get('workflowUuid',[''])[0],after_id=after,limit=limit)
                    elif u.path.endswith('/definition'):
                        result=_SQL_WORKFLOWS.read_definition(qs.get('definitionUuid',[''])[0])
                    elif u.path.endswith('/capture-identity'):
                        result=_SQL_WORKFLOWS.read_capture_identity(qs.get('captureUuid',[''])[0])
                    elif u.path.endswith('/candidates'):
                        result=_SQL_WORKFLOWS.candidates(qs.get('captureUuid',[''])[0],after_id=after,limit=limit)
                    elif u.path.endswith('/sources'):
                        result=_SQL_WORKFLOWS.sources(qs.get('snapshotUuid',[''])[0],after_id=after,limit=limit)
                    elif u.path.endswith('/source'):
                        result=_SQL_WORKFLOWS.read_source(qs.get('sourceUuid',[''])[0])
                    elif u.path.endswith('/source-bindings'):
                        result=_SQL_WORKFLOWS.source_bindings(qs.get('captureUuid',[''])[0],after_id=after,limit=limit)
                    else:
                        result=_SQL_WORKFLOWS.export_capture(version,qs.get('captureUuid',[''])[0])
                    return self._json(result)
                except (WorkflowSourceStoreConflict,WorkflowSourceBindingConflict) as error:
                    return self._json({'error':str(error),'code':'WORKFLOW_SOURCE_CONFLICT'},409)
                except (WorkflowIdentityError,WorkflowSourceStoreError,WorkflowSourceBindingError) as error:
                    return self._json({'error':str(error)},404)
                except (ValueError,WorkflowFormatError) as error:
                    return self._json({'error':str(error)},400)
            if u.path in ('/api/v2/models/roots', '/api/v2/models/verification/status', '/api/v2/models/references'):
                if _SQL_MODELS is None:
                    return self._json({'error':'模型身份功能需要数据库模式','code':'CAPABILITY_UNAVAILABLE'},501)
                from backend.metadata.model_verification_jobs import ModelVerificationJobError
                try:
                    if u.path.endswith('/roots'):
                        return self._json({'roots':_SQL_MODELS.roots()})
                    if u.path.endswith('/references'):
                        return self._json({'references':_SQL_MODELS.references(
                            qs.get('mediaUuid',[''])[0],qs.get('revisionUuid',[''])[0])})
                    return self._json(_SQL_MODELS.verification_status(qs.get('jobUuid',[''])[0]))
                except ValueError as error:
                    return self._json({'error':str(error)},400)
                except ModelVerificationJobError as error:
                    return self._json({'error':str(error),'code':'MODEL_JOB_UNAVAILABLE'},404)
            if u.path.startswith(("/css/", "/js/", "/assets/", "/bridge/")):   # 模块化前端静态资源 + 回传桥接脚本
                # CMV3-LOCAL-FIX（E-P5 回植）：原为 200 + `Cache-Control: no-store`，
                # 每次启动/F5 重下 731KB/25 文件。改走 _static() 条件请求（ETag/304）。
                return self._static(u.path)
            if u.path == "/api/info":
                return self._json({"root": STATE["root"], "has_pil": HAS_PIL,
                                   "storageMode": "sqlite" if _SQL_ORG is not None else "legacy",
                                   "capabilities": {"sqliteUserStateV1": _SQL_ORG is not None, "mediaIdentityV1": _SQL_CATALOG is not None,
                                                    "revisionTokensV1": _SQL_CATALOG is not None, "queryV1": _SQL_QUERY is not None},
                                   "title": APP_TITLE,
                                   "version": APP_VERSION,
                                   # P0-11（CMV3-LOCAL-FIX 回植）：如实自报 pid —— 桌面壳反查的
                                   # **唯一**口径。壳内「重启程序」= 后端自起新进程接管同端口，
                                   # 壳登记的旧 pid 随即成死号（`taskkill /PID <死号>` 必失败
                                   # 且错误被吞）⇒ 缺这个字段时壳只能走「取不到就不杀」的保守
                                   # 分支，新后端常驻成孤儿，并一直锁着
                                   # %LOCALAPPDATA%\ComfyMediaViewer\backend\ComfyMediaViewer.exe
                                   # （下次换版时 remove_file 报「部署后端失败」，用户以为打包坏了）。
                                   "pid": os.getpid(),
                                   "frontend_version": frontend_version(),
                                   "hotfix_dir": str(HOTFIX_DIR),
                                   "update_url": update_url(),
                                   "update_auto_check": update_auto_check(),
                                   "settings_file": str(SETTINGS_FILE),
                                   "delete_mode": load_delete_mode(),
                                   # P1-34：最近一次「重启程序」起新进程失败的原因
                                   # （null=没失败过/正在重启中；非空=重启失败但仍在本进程服务）
                                   "restart_failed": (None if _RestartState["ok"] is not False
                                                      else _RestartState["err"]),
                                   "trash_keep_days": load_trash_keep_days(),
                                   # 前端启动时同步一次（和 delete_mode 同款套路）：
                                   # 阈值/开关的权威值在服务端，前端只做缓存与显示
                                   "nsfw_available": nsfw_available(),
                                   "nsfw_reason": nsfw_reason(),
                                   "nsfw_enabled": nsfw_enabled(),
                                   "nsfw_threshold": load_nsfw_threshold(),
                                   "last_export_dir": load_settings().get("last_export_dir", ""),
                                   "download_dir": load_settings().get("download_dir", ""),
                                   # B1-3（CMV3-LOCAL-FIX）：数据目录可写状态 —— 便携模式
                                   # 指向只读 U 盘 / 外接盘掉线时，界面才有话可说
                                   "data_write": data_write_state()})
            if u.path == "/api/update/status":
                return self._json(UPDATER.get_state())
            if u.path == "/api/nsfw/status":     # 内容安全：引擎可用性 / 档位 / 阈值 / 扫描进度
                return self._json(nsfw_status())
            if u.path == "/api/sysinfo":          # 设置面板「数据 / 关于」页：目录占用与版本
                return self._json(sysinfo())
            if u.path == "/api/list":
                return self.api_list(qs)
            if u.path == "/api/search":
                q = qs.get("q", [""])[0].strip()
                if not q:
                    return self._json({"building": False, "done": 0,
                                       "total": 0, "results": []})
                ensure_index()
                base = qs.get("base", [""])[0]
                if base:
                    bp = Path(base)
                    if not bp.is_dir() or not inside_root(bp):
                        base = ""           # 非法 base：退回全根目录搜索
                return self._json(search_index(q, base))
            if u.path == "/api/meta":
                return self.api_meta(qs)
            if u.path == "/api/v2/media/detail":
                if _SQL_ARCHIVE is None:
                    return self._json({"error": "数据库详情尚未启用", "code": "CAPABILITY_UNAVAILABLE"}, 501)
                media_id = qs.get("mediaId", [""])[0]
                if not media_id:
                    return self._json({"error": "mediaId is required"}, 400)
                from backend.metadata.archive import MetadataRevisionError
                try:
                    detail = _SQL_ARCHIVE.read_media(_SQL_ORG.library_uuid, media_id,
                                                     qs.get("revisionId", [None])[0])
                except MetadataRevisionError as error:
                    return self._json({"error": str(error), "code": "media_not_found"}, 404)
                snapshot = detail.get("metadataArchive")
                detail["generation"] = (_SQL_GENERATION.read_current(snapshot["uuid"])
                                        if snapshot is not None else None)
                from backend.metadata.wire import seed_text_for_client
                return self._json(seed_text_for_client(detail))
            if u.path == "/api/v2/query/directories":
                if _SQL_QUERY is None:
                    return self._json({"error": "数据库查询尚未启用", "code": "CAPABILITY_UNAVAILABLE"}, 501)
                from backend.query.spec import QuerySpecError
                from backend.query.cursor import CursorError
                try:
                    if set(qs) - {"rootId", "directory", "limit", "cursor", "token"} or any(len(values) != 1 for values in qs.values()):
                        raise QuerySpecError("Invalid directory request")
                    limit_text = qs.get("limit", ["100"])[0]
                    if not limit_text.isascii() or not limit_text.isdecimal() or len(limit_text) > 3:
                        raise QuerySpecError("Invalid directory limit")
                    return self._json(_SQL_QUERY.directories(qs.get("rootId", [None])[0], qs.get("directory", [None])[0],
                                      limit=int(limit_text), cursor=qs.get("cursor", [None])[0]))
                except CursorError as error:
                    return self._json({"error": str(error), "code": error.code}, error.status)
                except QuerySpecError as error:
                    return self._json({"error": str(error), "code": "invalid_query"}, 400)
            if u.path == "/api/v2/migration/preparation-status":
                from backend.migration.preparation_job import read_preparation_job
                from backend.migration.status import preparation_status
                try:
                    job=read_preparation_job(PORTABLE_FLAG,qs.get('jobId',[''])[0])
                    if job['source'] != str(DATA_DIR.absolute()):
                        return self._json({'error':'任务不属于当前数据目录'},409)
                    status=preparation_status(job['destination'],job['runUuid']) if job.get('runUuid') else None
                    return self._json({'job':job,'preparation':status,'authorityPublished':False})
                except (ValueError,OSError,RuntimeError) as error:
                    return self._json({'error':str(error)},400)
            if u.path == "/api/v2/migration/installation-status":
                from backend.migration.installation_job import read_installation_job, pending_installation_job
                try:
                    job_id = qs.get('jobId',[''])[0]
                    preparation_id = qs.get('preparationJobId',[''])[0]
                    if bool(job_id) == bool(preparation_id):
                        raise ValueError('需要一个安装任务编号或准备任务编号')
                    if preparation_id:
                        from backend.migration.preparation_job import read_preparation_job
                        preparation = read_preparation_job(PORTABLE_FLAG, preparation_id)
                        if preparation['source'] != str(DATA_DIR.absolute()):
                            return self._json({'error':'准备任务不属于当前数据目录'},409)
                        job = pending_installation_job(PORTABLE_FLAG, preparation['destination'])
                        if job is None:
                            return self._json({'job':None,'runtimeVerified':False})
                    else:
                        job = read_installation_job(PORTABLE_FLAG, job_id)
                    if job['source'] != str(DATA_DIR.absolute()):
                        return self._json({'error':'安装任务不属于当前数据目录'},409)
                    return self._json({'job':job,'runtimeVerified':False})
                except (ValueError,OSError,RuntimeError) as error:
                    return self._json({'error':str(error)},400)
            if u.path == "/api/v2/migration/installation-jobs":
                from backend.migration.installation_job import discover_installation_jobs
                try:
                    return self._json({'items':discover_installation_jobs(PORTABLE_FLAG,DATA_DIR),
                                       'runtimeVerified':False})
                except (ValueError,OSError,RuntimeError) as error:
                    return self._json({'error':str(error)},400)
            if u.path == "/api/v2/migration/recovery-status":
                from backend.migration.recovery_job import read_recovery_job
                try:
                    if set(qs)-{'jobId','token'} or len(qs.get('jobId',[]))!=1:
                        raise ValueError('需要一个恢复任务编号')
                    job=read_recovery_job(PORTABLE_FLAG,qs['jobId'][0])
                    if job['source'] != str(DATA_DIR.absolute()):
                        return self._json({'error':'恢复任务不属于当前数据目录'},409)
                    return self._json({'job':job,'selectorChanged':False,
                                       'canResume':job['state']!='completed' and not Path(job['destination']).exists()})
                except (ValueError,OSError,RuntimeError) as error:
                    return self._json({'error':str(error)},400)
            if u.path == "/api/v2/migration/recovery-jobs":
                from backend.migration.recovery_job import discover_recovery_jobs
                try:
                    return self._json({'items':discover_recovery_jobs(PORTABLE_FLAG,DATA_DIR),'selectorChanged':False})
                except (ValueError,OSError,RuntimeError) as error:
                    return self._json({'error':str(error)},400)
            if u.path == "/api/v2/migration/recovery-selection-status":
                from backend.migration.recovery_selection_job import read_selection_job
                try:
                    if set(qs)-{'jobId','token'} or len(qs.get('jobId',[]))!=1:
                        raise ValueError('需要一个恢复库切换任务编号')
                    job=read_selection_job(PORTABLE_FLAG,qs['jobId'][0])
                    if str(DATA_DIR.absolute()) not in (job['source'],job['target']):
                        return self._json({'error':'切换任务不属于当前数据目录'},409)
                    return self._json({'job':job,'runtimeVerified':False})
                except (ValueError,OSError,RuntimeError) as error:
                    return self._json({'error':str(error)},400)
            if u.path == "/api/v2/migration/recovery-selection-jobs":
                from backend.migration.recovery_selection_job import discover_selection_jobs
                try:
                    return self._json({'items':discover_selection_jobs(PORTABLE_FLAG,DATA_DIR),'runtimeVerified':False})
                except (ValueError,OSError,RuntimeError) as error:
                    return self._json({'error':str(error)},400)
            if u.path == "/api/v2/migration/downgrade-review":
                if _SQL_ORG is None:
                    return self._json({"storageMode":"legacy-json","readyForLegacyStartup":False,
                                       "message":"当前尚未启用数据库；降级检查适用于数据库模式。"})
                from backend.migration.export_legacy import organization_export_preview
                return self._json({"storageMode":"sqlite",**organization_export_preview(
                    _SQL_ORG.database, _SQL_ORG.profile_uuid)})
            if u.path == '/api/v2/workflows/decision':
                if _SQL_WORKFLOW_DECISIONS is None:
                    return self._json({'error':'工作流确认需要数据库模式','code':'CAPABILITY_UNAVAILABLE'},501)
                from backend.metadata.workflow_decisions import WorkflowDecisionScopeError, WorkflowDecisionNotFound
                try:
                    if set(qs)-{'captureUuid','token'} or len(qs.get('captureUuid',[])) != 1:
                        raise ValueError('Invalid workflow decision query')
                    return self._json(_SQL_WORKFLOW_DECISIONS.read(_SQL_ORG.profile_uuid,qs['captureUuid'][0]))
                except (ValueError,WorkflowDecisionScopeError,WorkflowDecisionNotFound) as error:
                    return self._json({'error':str(error)},400)
            if u.path == '/api/v2/relations/media':
                if _SQL_RELATIONS is None:
                    return self._json({'error':'创作关系需要数据库模式','code':'CAPABILITY_UNAVAILABLE'},501)
                try:
                    if set(qs)-{'mediaUuid','revisionUuid','afterId','limit','token'} \
                            or len(qs.get('mediaUuid',[])) != 1 \
                            or any(len(value)!=1 for value in qs.values()):
                        raise ValueError('Invalid media execution query')
                    return self._json(_SQL_RELATIONS.executions_for_media(qs['mediaUuid'][0],
                        revision_uuid=qs.get('revisionUuid',[None])[0],
                        after_id=int(qs.get('afterId',['0'])[0]),limit=int(qs.get('limit',['50'])[0])))
                except ValueError as error:
                    return self._json({'error':str(error),'code':'invalid_relation_query'},400)
            if u.path == '/api/v2/relations/history':
                if _SQL_RELATIONS is None:
                    return self._json({'error':'创作关系需要数据库模式','code':'CAPABILITY_UNAVAILABLE'},501)
                try:
                    if set(qs)-{'relationUuid','afterVersion','limit','token'} or len(qs.get('relationUuid',[])) != 1 or any(len(value)!=1 for value in qs.values()):
                        raise ValueError('Invalid relation history query')
                    return self._json(_SQL_RELATIONS.history(_SQL_ORG.profile_uuid,qs['relationUuid'][0],
                        after_version=int(qs.get('afterVersion',['0'])[0]),limit=int(qs.get('limit',['50'])[0])))
                except ValueError as error:
                    return self._json({'error':str(error),'code':'invalid_relation_query'},400)
            if u.path == '/api/v2/relations/graph':
                if _SQL_RELATIONS is None:
                    return self._json({'error':'创作关系需要数据库模式','code':'CAPABILITY_UNAVAILABLE'},501)
                try:
                    if set(qs)-{'executionUuid','includeCandidates','token'} or len(qs.get('executionUuid',[])) != 1 \
                            or len(qs.get('includeCandidates',['0'])) != 1 \
                            or qs.get('includeCandidates',['0'])[0] not in ('0','1'):
                        raise ValueError('Invalid relation graph query')
                    return self._json(_SQL_RELATIONS.graph(_SQL_ORG.profile_uuid, qs['executionUuid'][0],
                        include_candidates=qs.get('includeCandidates',['0'])[0]=='1'))
                except ValueError as error:
                    return self._json({'error':str(error),'code':'invalid_relation_query'},400)
            if u.path == "/api/v2/query/context":
                if _SQL_QUERY is None:
                    return self._json({"error": "数据库查询尚未启用", "code": "CAPABILITY_UNAVAILABLE"}, 501)
                with STATE_LOCK:
                    active_root = STATE["root"]
                return self._json(_SQL_QUERY.context(active_root))
            if u.path == "/api/v2/relocation/status":
                from backend.migration.relocation_job import read_relocation_job
                try:
                    if set(qs)-{'jobId','token'} or len(qs.get('jobId',[]))!=1:
                        raise ValueError('A single jobId is required')
                    return self._json({'ok':True,'job':read_relocation_job(PORTABLE_FLAG,qs['jobId'][0])})
                except FileNotFoundError:
                    return self._json({'error':'搬家任务不存在','code':'relocation_not_found'},404)
                except (ValueError,OSError) as error:
                    return self._json({'error':str(error),'code':'invalid_relocation_job'},400)
            if u.path == "/api/v2/catalog/scan/status":
                if _SQL_SCAN_JOBS is None:
                    return self._json({"error": "数据库扫描尚未启用", "code": "CAPABILITY_UNAVAILABLE"}, 501)
                try:
                    return self._json(_SQL_SCAN_JOBS.status(qs.get("jobId", [""])[0]))
                except ValueError as error:
                    return self._json({"error": str(error), "code": "scan_not_found"}, 404)
            if u.path == "/api/favs":
                return self._json({"cats": fav_cats()})
            if u.path == "/api/v2/migration/recent-review":
                return self._json({"storageMode": "sqlite" if _SQL_RECENT is not None else "legacy-json",
                                   "report": _SQL_RECENT.import_report(_SQL_ORG.profile_uuid) if _SQL_RECENT is not None else None})
            if u.path == "/api/datadir_status":   # ε-12（§6.3）：datadir 后台迁移进度
                return self._json(datadir_status())
            if u.path == "/api/tags_list":
                return self._json({"tags": tag_stats()})
            if u.path == "/api/tags_get":
                p = qs.get("p", [""])[0]
                if not p:
                    return self._json({"error": "bad request"}, 400)
                return self._json({"tags": tags_of(str(Path(p).resolve()))})
            if u.path == "/api/tags_filter":
                raw = qs.get("tags", [""])[0]
                mode = qs.get("mode", ["and"])[0]
                if mode not in ("and", "or", "not"):
                    mode = "and"
                rec = qs.get("rec", ["1"])[0] == "1"
                base = qs.get("path", [""])[0]
                # ε-14（§6.8）：base 此前无校验 —— GET 可对任意目录做标签查询，
                # 借结果集探测根外文件的存在性。显式传 base 时要求在 root 内
                # （缺省走 STATE["root"]，天然安全）。
                if base and not inside_root(Path(base)):
                    return self._json({"error": "目录在根目录之外"}, 400)
                wanted = [t for t in raw.split("|") if t.strip()]
                excl = [t for t in (qs.get("not", [""])[0] or "").split("|") if t.strip()]
                return self._json({"items": tags_filter(wanted, base, mode, rec, excl),
                                   "tags": wanted, "mode": mode, "not": excl})
            if u.path == "/api/roots":
                return self._json({"roots": load_roots()})
            if u.path == "/api/templates":
                return self._json({"templates": list_templates()})
            if u.path == "/api/pull":            # 回传·拉取模式：ComfyUI 页面内脚本取件
                origin = self._cors_origin()
                want = (qs.get("port", [""])[0] or "").strip()
                tab = (qs.get("tab", [""])[0] or "").strip()   # ε-7：标签页标识（可空）
                # P0-10：页面侧报告「我正忙 / 不在载入」。带 busy=1 时不跑 abort 兜底回收
                # （旧判据会把正在载入的件当 abort 抢走重投 ⇒ 同一工作流进画布两遍）。
                busy = (qs.get("busy", [""])[0] or "").strip() == "1"
                if want:                         # 空轮询也算心跳，据此判断页面是否还在
                    bridge_touch(want, tab)
                return self._json(bridge_take(want, reclaim=not busy) or {"seq": 0},
                                  cors=origin)
            if u.path == "/api/pull/ack":        # 页面回执·载入成功 -> 该件才真正销毁（V2.8.4）
                return self._json(bridge_ack(qs.get("seq", [""])[0],
                                             qs.get("claim", [""])[0]),   # P0-9：投递代次
                                  cors=self._cors_origin())
            if u.path == "/api/pull/nack":       # 页面回执·载入失败 -> 放回队列重投（V2.8.4）
                return self._json(bridge_nack(qs.get("seq", [""])[0],
                                              qs.get("try", [""])[0],      # ε-7：带退避
                                              qs.get("claim", [""])[0]),   # P0-9：投递代次
                                  cors=self._cors_origin())
            if u.path == "/api/pull/drop":       # 页面回执·**已放弃** -> 终结该件 + 记账（ε-7 §6.7 P2-2）
                return self._json(bridge_drop(qs.get("seq", [""])[0],
                                              qs.get("reason", [""])[0],
                                              qs.get("name", [""])[0],
                                              qs.get("claim", [""])[0]),   # P0-9：投递代次
                                  cors=self._cors_origin())
            if u.path == "/api/pull/release":    # 页面取件超时放弃 -> 立刻放回队列（P1-32）
                return self._json(bridge_release((qs.get("port", [""])[0] or "").strip()),
                                  cors=self._cors_origin())
            if u.path == "/api/bridge/status":   # 前端查询：有没有活着的桥接页面
                return self._json({"ok": True, "pages": bridge_pages(),
                                   "tabs": bridge_tabs(),           # ε-7：标签页维度
                                   "dropped": bridge_dropped(),     # ε-7：被放弃的件
                                   "queued": len(PULL_QUEUE),
                                   "target": _comfy_port(),
                                   "installed": bridge_installed(),
                                   "stale": bridge_stale(),
                                   "active": bridge_active()},
                                  cors=self._cors_origin())
            if u.path == "/api/file":
                target_path = Path(qs.get("p", [""])[0])
                if qs.get('playback'):
                    try:
                        identity = (str(Path(STATE['root']).resolve()), str(target_path.resolve()),
                                    qs.get('mediaId', [''])[0], qs.get('expectedVersion', [''])[0])
                        if target_path.suffix.lower() not in VIDEO_EXTS or qs.get('dl') or qs.get('clean'):
                            return self._json({'error':'Playback only supports video viewing'},400)
                        with PLAYBACK_SESSIONS.read(qs['playback'][0],identity) as frozen:
                            return self._send_file(target_path,frozen=frozen)
                    except PlaybackExpired as error:
                        return self._json({'error':str(error),'code':'playback_expired'},410)
                if _SQL_CATALOG is not None and (qs.get("mediaId") or qs.get("expectedVersion")):
                    if not target_path.is_file() or not inside_root(target_path):
                        return self._json({"error": "not found or outside root"}, 404)
                    target = _SQL_CATALOG.verify(STATE["root"], target_path)
                    expected_id = qs.get("mediaId", [None])[0]
                    expected_version = qs.get("expectedVersion", [None])[0]
                    if ((expected_id and expected_id != target["mediaId"]) or
                            (expected_version and expected_version != "rev:" + target["revisionId"])):
                        return self._json({"error": "媒体身份或版本已变化，请重新打开", "code": "media_conflict"}, 409)
                    from backend.storage.media_stream import verified_stream, MediaStreamConflict
                    try:
                        with verified_stream(target_path, target["contentHash"], reuse=target_path.suffix.lower() in VIDEO_EXTS) as frozen:
                            return self._send_file(target_path, download=qs.get("dl") == ["1"],
                                                   clean=qs.get("clean") == ["1"], frozen=frozen)
                    except MediaStreamConflict as error:
                        return self._json({"error": str(error), "code": "media_conflict"}, 409)
                    except WorkBusy as error:
                        return self._json({"error": str(error), "code": getattr(error, "code", "media_busy")}, getattr(error, "status", 503))
                return self._send_file(target_path,
                                       download=qs.get("dl") == ["1"],
                                       clean=qs.get("clean") == ["1"])
            if u.path == "/api/content_version":
                rp = Path(qs.get("p", [""])[0])
                if not rp.is_file() or not inside_root(rp) or rp.suffix.lower() not in IMAGE_EXTS:
                    return self._json({"error": "图片不存在或不在当前根目录"}, 404)
                try:
                    if _SQL_CATALOG is not None:
                        verified_identity = _SQL_CATALOG.verify(STATE["root"], rp)
                        version = "sha256-" + verified_identity["contentHash"]
                    else:
                        verified_identity = {}
                        version = image_content_version(rp)
                except OSError as ex:
                    return self._json({"error": f"读取图片版本失败：{ex}"}, 500)
                return self._json({"path": str(rp), "contentVersion": version,
                                   "mediaId": verified_identity.get("mediaId"),
                                   "revisionId": verified_identity.get("revisionId"),
                                   "revisionToken": verified_identity.get("contentVersion"),
                                   "verified": True})
            if u.path == "/api/thumb":
                try:
                    size = int(qs.get("s", ["320"])[0])
                except ValueError:
                    size = 320
                size = max(64, min(size, 1024))   # 钳制尺寸，防异常参数撑爆缓存
                rp = Path(qs.get("p", [""])[0])
                if not rp or not inside_root(rp):      # 越权路径一律拒绝
                    return self._json({"error": "forbidden"}, 403)
                if _SQL_CATALOG is not None and (qs.get("mediaId") or qs.get("expectedVersion")):
                    if not rp.is_file():
                        return self._json({"error": "no source"}, 404)
                    from backend.storage.media_stream import MediaStreamConflict
                    try:
                        t = targeted_thumb(rp, size, STATE["root"], qs.get("mediaId", [None])[0],
                                           qs.get("expectedVersion", [None])[0])
                    except MediaStreamConflict as error:
                        return self._json({"error": str(error), "code": "media_conflict"}, 409)
                    except WorkBusy as error:
                        return self._json({"error": str(error), "code": getattr(error, "code", "media_busy")}, getattr(error, "status", 503))
                else:
                    try:
                        t = thumb_for(rp, size=size)
                    except WorkBusy as error:
                        return self._json({"error": str(error), "code": getattr(error, "code", "media_busy")}, getattr(error, "status", 503))
                if t:
                    return self._send_file(t, check_root=False)
                return self._json({"error": "no thumb"}, 404)
            if u.path == "/api/browse":          # 目录选择器：列出子目录/上级/盘符（不视为浏览 root，任意合法目录均可）
                p = qs.get("path", [""])[0]
                if not p:
                    return self._json({"drives": _fixed_dirs(), "path": "", "parent": None, "dirs": []})
                bp = Path(p)
                if not bp.is_dir():
                    return self._json({"error": "目录不存在"}, 400)
                dirs = []
                try:
                    with os.scandir(bp) as it:
                        for e in it:
                            try:
                                if e.is_dir():
                                    cnt = _dir_media_count(e.path)
                                    dirs.append({"name": e.name, "path": e.path, "n": cnt, "tag": "dir"})
                            except OSError:
                                continue
                except OSError as ex:
                    return self._json({"error": str(ex)}, 500)
                dirs.sort(key=lambda x: x["name"].lower())
                parent = str(bp.parent) if bp.parent != bp else None
                comfy = None
                try:
                    cr = comfy_root()
                    if cr and (cr / "output").is_dir():
                        comfy = str(cr / "output")
                    elif cr and cr.name.lower() == "output":
                        comfy = str(cr)
                except Exception:
                    comfy = None
                return self._json({"drives": _fixed_dirs(), "path": str(bp),
                                   "parent": parent, "dirs": dirs, "comfy": comfy})
            if u.path == "/api/dirinfo":            # 目录指纹：前端轮询检测新文件
                p = Path(qs.get("path", [""])[0] or STATE["root"])
                if not p.is_dir() or not inside_root(p):
                    return self._json({"error": "目录不存在或在根目录之外"}, 400)
                return self._json({"sig": dir_signature(
                    p, qs.get("rec", ["0"])[0] == "1")})
            if u.path == "/api/dupes":              # 重复图检测
                if not HAS_PIL:                     # 需要 Pillow 做 dHash，缺库时给提示而不是 500
                    return self._json({"error": "查重需要 Pillow（pip install pillow）",
                                       "need_pil": True}, 503)
                p = Path(qs.get("path", [""])[0] or STATE["root"])
                if not p.is_dir() or not inside_root(p):
                    return self._json({"error": "目录不存在或在根目录之外"}, 400)
                return self._json(find_dupes(
                    p, qs.get("rec", ["0"])[0] == "1"))
            if u.path == "/api/fav_view":           # 跨目录收藏视图
                return self._json({"items": fav_view(qs.get("cat", [""])[0])})
            if u.path == "/api/stats":
                ensure_index()
                # 顺带做一次 NSFW 增量补测：挂在既有轮询上，不另开定时器。
                # auto_only=True 保证「从没扫过」时什么都不做 —— 首次打开软件
                # 不会因为看了眼统计就把 CPU 打满；用户点过「开始检测」之后才自动跟上新图。
                ensure_nsfw(auto_only=True)
                # 同款思路挂上换根迁移检测：前端每秒都在调这里，迁移检测自己有
                # 「新根无缓存文件才动手」+ 30 秒节流，空转成本接近零。
                root_migrate_maybe()
                j = stats_view()
                j["nav"] = nav_stats()      # 左栏导航统一口径（全部/图片/视频/收藏/工作流/标签）
                # P4（V2.12.1）：迁移完成待领取时随 stats 捎个信 —— 前端只在
                # boot+4s / 切根+4s 各查一次报告，大库迁移超 4s 提示就永远丢了；
                # 挂在 stats 上等于每次统计刷新都有一次领取机会（报告取走即清）。
                with _MIGRATE_LOCK:
                    j["mig"] = _MIGRATE_STATE["done"] is not None
                return self._json(j)
            if u.path == "/api/migrate_report":  # 换根迁移报告（取走即清：一次迁移只提示一次）
                with _MIGRATE_LOCK:
                    r = _MIGRATE_STATE["done"]
                    _MIGRATE_STATE["done"] = None
                return self._json({"report": r})
            if u.path == "/api/recent_view":        # 最近使用视图
                return self._json({"items": recent_view()})
            if u.path == "/api/marks_view":         # 已标记视图（可按状态过滤）
                return self._json({"items": marks_view(qs.get("state", [""])[0]),
                                   "marks": mark_stats()})
            if u.path == "/api/trash":           # 回收站列表
                return self._json({"items": trash_list()})
            if u.path == "/api/trash_thumb":        # 回收站缩略图（路径服务端拼接，免穿越）
                key = qs.get("key", [""])[0]
                if not re.fullmatch(r"(?:\d+_[0-9a-f]{6}|[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})", key):
                    return self._json({"error": "bad key"}, 400)
                if key not in load_trash():
                    return self._json({"error": "no thumb"}, 404)
                f = next(TRASH_DIR.glob(key + ".*"), None)
                if not f or f.suffix.lower() not in IMAGE_EXTS:
                    return self._json({"error": "no thumb"}, 404)
                t = thumb_for(f, size=160)
                if t:
                    return self._send_file(t, check_root=False)
                return self._json({"error": "no thumb"}, 404)
            return self._json({"error": "unknown endpoint"}, 404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # ε-14（§6.8）：补 ConnectionAbortedError —— 客户端（下载器/杀软中间人）
            # 强杀连接时 Windows 抛的是它而不是 ConnectionReset，漏捕会刷 ERROR 日志。
            pass
        except Exception as ex:
            # §6.1：响应里带了 error，但**服务端一点痕迹都没有** —— 排障只能靠猜。
            from backend.storage.catalog import IdentityConflictError,FileChangedDuringVerification
            if u.path in ('/api/meta','/api/content_version','/api/file','/api/thumb') \
                    and isinstance(ex,(IdentityConflictError,FileChangedDuringVerification)):
                return self._json({'error':'媒体身份或版本已变化，请刷新后重新打开','code':'media_conflict'},409)
            # traceback 进日志（P1-39 已建落盘日志基建）；路径**剥掉 query**，
            # 免得 LAN 模式的 ?token=xxx 被写进日志。do_GET / do_POST 两处兜底同款。
            log("[error] %s 处理失败: %s: %s\n%s"
                % ((self.path or "").split("?")[0], type(ex).__name__, ex,
                   traceback.format_exc()))
            try:
                self._json({"error": f"{type(ex).__name__}: {ex}"}, 500)
            except Exception:
                pass

    def _classic_handoff(self,kind,job):
        from backend.storage.classic_data import read_job,NATIVE
        from backend.migration.relocation_job import _birth
        from backend.storage.runtime_state import _safe_path
        read_job(PORTABLE_FLAG,DATA_DIR,kind,job['jobId'])
        birth=_birth(os.getpid())
        if birth is None: raise ValueError('当前程序身份不可读取')
        workers=globals().setdefault('_CLASSIC_DATA_WORKERS',{})
        lock=globals().setdefault('_CLASSIC_DATA_WORKER_LOCK',threading.Lock())
        key=(kind,job['jobId'])
        with lock:
            existing=workers.get(key)
            if existing is None or existing.poll() is not None:
                command=([sys.executable] if getattr(sys,'frozen',False) else [sys.executable,str(Path(__file__).resolve())])
                command+=['--classic-data-worker',kind,'--selector',str(PORTABLE_FLAG),'--current',str(DATA_DIR),
                    '--job-id',job['jobId'],'--parent-pid',str(os.getpid()),'--parent-birth',str(birth)]
                folder='.cmv-classic-imports' if kind=='preparation' else NATIVE[kind][0]
                log_path=_safe_path(PORTABLE_FLAG.parent,folder+'/'+job['jobId']+'/worker.log')
                with log_path.open('ab') as stream:
                    workers[key]=subprocess.Popen(command,stdout=stream,stderr=stream,stdin=subprocess.DEVNULL,
                        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),close_fds=True,env=_restart_env())
        return self._json({'ok':True,'job':job,'handoffRequired':True,'workerStarted':True,
                           'authorityPublished':False,'runtimeVerified':False})

    def api_list(self, qs):
        path = qs.get("path", [""])[0] or STATE["root"]
        recursive = qs.get("rec", ["0"])[0] == "1"
        p = Path(path)
        if not p.is_dir() or not inside_root(p):
            return self._json({"error": "目录不存在或在根目录之外"}, 400)
        dirs, items = [], []
        selected_root=STATE['root']
        pending_stats=[]
        favs = load_favs()
        tags = load_tags()
        # 一份 NSFW 快照整个循环复用。这是性能红线：每张卡片各取一次锁+读一次盘，
        # 一次 api_list 就要多几千次系统调用，网格直接卡住。
        _nctx = nsfw_ctx()

        def pack(name: str, sp: str, ext: str, t: str, st) -> dict:
            # ns = 分数（前端改阈值时现场重算，省一次全量重拉）；-1 = 以 n 为准
            pending_stats.append((sp,st))
            return {"mediaId": None, "revisionId": None,
                    "name": name, "path": sp, "type": t,
                    "size": st.st_size, "mtime": int(st.st_mtime),
                    "playable": t == "image" or ext in PLAYABLE_VIDEO,
                    "png": ext == ".png", "fav": sp in favs,
                    "favcat": favs[sp]["cat"] if sp in favs else None,
                    "tags": list(tags.get(sp, [])),
                    "n": "unknown", "ns": -1}

        def media_type(ext: str):
            if ext in IMAGE_EXTS:
                return "image"
            if ext in VIDEO_EXTS:
                return "video"
            return None

        try:
            # 子文件夹卡片：与文件扫描同一趟 scandir（DirEntry 自带类型，免二次 stat）
            with os.scandir(p) as it:
                for e in it:
                    try:
                        if e.is_dir():
                            dirs.append({"name": e.name, "path": e.path,
                                         "n": _count_media(Path(e.path)) if recursive
                                         else _dir_media_count(e.path)})
                    except OSError:
                        continue
            if recursive:
                stack = [(str(p), 0)]
                visited = {os.path.realpath(str(p))}
                while stack:
                    cur, depth = stack.pop()
                    try:
                        with os.scandir(cur) as it:
                            for e in it:
                                try:
                                    if e.is_dir():
                                        if depth < 10:
                                            rp = os.path.realpath(e.path)
                                            if rp not in visited:
                                                visited.add(rp)
                                                stack.append((e.path, depth + 1))
                                        continue
                                    ext = os.path.splitext(e.name)[1].lower()
                                    t = media_type(ext)
                                    if t:
                                        items.append(pack(e.name, e.path, ext, t,
                                                          e.stat()))
                                except OSError:
                                    continue
                    except OSError:
                        continue
            else:
                with os.scandir(p) as it:
                    for e in it:
                        try:
                            if e.is_dir():
                                continue
                            ext = os.path.splitext(e.name)[1].lower()
                            t = media_type(ext)
                            if t:
                                items.append(pack(e.name, e.path, ext, t, e.stat()))
                        except OSError:
                            continue
        except OSError as ex:
            return self._json({"error": str(ex)}, 500)
        identities=(_SQL_CATALOG.observe_many(selected_root,pending_stats) if _SQL_CATALOG is not None else [{} for _ in pending_stats])
        for item,(sp,st),identity in zip(items,pending_stats,identities):
            item.update(mediaId=identity.get('mediaId'),revisionId=identity.get('revisionId'))
            item['n'],item['ns']=nsfw_fields(sp,int(st.st_mtime),st.st_size,_nctx,identity or None)
        self._json({"root": selected_root, "path": str(p.resolve()),
                    "has_thumb": HAS_PIL, "dirs": dirs, "items": items})

    def _publish_generation(self, archive_record):
        from backend.metadata.pipeline import publish_archived_generation
        result = publish_archived_generation(_SQL_GENERATION, archive_record)
        if _SQL_PROMPT_REFRESH is not None:
            _SQL_PROMPT_REFRESH.refresh()
        return result

    def _meta_cache_key(self, verified, path):
        with _SQL_QUERY.state._connection() as db:
            generation = _SQL_QUERY.state.read_in_transaction(db)['indexGeneration']
        return (str(path), verified['mediaId'], verified['revisionId'], verified['contentHash'], generation)

    def api_meta(self, qs):
        p = Path(qs.get("p", [""])[0])
        if not p.is_file() or not inside_root(p):
            return self._json({"error": "not found or outside root"}, 404)
        st = p.stat()
        suffix = p.suffix.lower()
        meta = {}
        archive_record = None
        generation_record = None
        verified = None
        try:
            solo_root = SOLO_DIR.resolve()
            resolved_path = p.resolve()
            solo_name = re.fullmatch(r"[0-9a-f]{16}\.(?:png|jpg|webp|gif|bmp)", resolved_path.name)
            is_solo_temp = resolved_path.parent == solo_root and solo_name is not None
        except OSError:
            is_solo_temp = False
        if _SQL_CATALOG is not None and not is_solo_temp:
            verified = _SQL_CATALOG.verify(STATE["root"], p)
            expected_id = qs.get("mediaId", [None])[0]
            expected_version = qs.get("expectedVersion", [None])[0]
            if ((expected_id and expected_id != verified["mediaId"]) or
                    (expected_version and expected_version != "rev:" + verified["revisionId"])):
                return self._json({"error": "媒体身份或版本已变化，请重新打开", "code": "media_conflict"}, 409)
        if verified is not None and _SQL_QUERY is not None:
            cached = META_RESPONSE_CACHE.get(self._meta_cache_key(verified, p))
            if cached is not None:
                return self._json(cached)
        if suffix == ".png":
            if _SQL_ARCHIVE is not None and verified is not None:
                archive_record = _SQL_ARCHIVE.capture_png(verified, p)
                generation_record = self._publish_generation(archive_record)
            meta = png_meta(p)
        elif suffix in (".jpg", ".jpeg", ".webp"):
            if _SQL_ARCHIVE is not None and verified is not None:
                archive_record = (_SQL_ARCHIVE.capture_webp(verified, p) if suffix == '.webp'
                                  else _SQL_ARCHIVE.capture_jpeg(verified, p))
                generation_record = self._publish_generation(archive_record)
                saved_generation = _SQL_GENERATION.read_current(archive_record['uuid'])
                if saved_generation is not None:
                    generation_record = {**generation_record, 'result': saved_generation['result']}
            meta = parse_jpeg_meta(p)
        elif suffix in VIDEO_EXTS:
            if _SQL_ARCHIVE is not None and verified is not None and suffix in ('.mp4', '.mov', '.m4v', '.webm', '.mkv'):
                archive_record = (_SQL_ARCHIVE.capture_ebml(verified, p)
                                  if suffix in ('.webm', '.mkv')
                                  else _SQL_ARCHIVE.capture_mp4(verified, p))
                generation_record = self._publish_generation(archive_record)
                saved_generation = _SQL_GENERATION.read_current(archive_record['uuid'])
                if saved_generation is not None:
                    generation_record = {**generation_record, 'result': saved_generation['result']}
            # 视频：读容器里的 prompt / workflow，产出的结构与 png_meta 一致，
            # 所以下面那句 parse_meta(meta) 原样就能用 —— 图片视频共用一条解析路径
            meta = video_meta(p)
        if verified is not None and (qs.get("mediaId") or qs.get("expectedVersion")):
            after = _SQL_CATALOG.verify(STATE["root"], p)
            if (after["mediaId"], after["revisionId"]) != (verified["mediaId"], verified["revisionId"]):
                return self._json({"error": "读取参数时媒体内容变化，请重试", "code": "media_conflict"}, 409)
        if archive_record is not None and _SQL_ARCHIVE is not None:
            # 首次采集返回的记录不带分段明细（只有快照已存在时才带）。统一从库里读完整档案，
            # 让 /api/meta 与 /api/v2/media/detail 口径一致 —— 详情面板要展示
            # JPEG 注释 / EXIF UserComment / XMP 字段，也必须能识别「是否根本没有来源」。
            archive_record = _SQL_ARCHIVE.read_snapshot(archive_record["uuid"])
        out = {"name": p.name, "size": st.st_size, "mtime": int(st.st_mtime),
               "mediaId": verified["mediaId"] if verified else None,
               "revisionId": verified["revisionId"] if verified else None,
                                   "metadataArchive": ({"snapshotId": archive_record["uuid"],
                                    "state": archive_record["state"], "coverage": archive_record["coverage"],
                                    "source_format": archive_record["source_format"],
                                    "result": {"dimensions": archive_record["result"].get("dimensions"),
                                               "capture_scope": archive_record["result"].get("capture_scope"),
                                               # 分段只回轻量字段：raw_payload_b64 是「原始档案」用途，
                                               # 体积随文件增长，不应出现在这个摘要响应里。
                                               # WebP 的原始段键是 chunks、PNG 的是 text_chunks/records、
                                               # 视频的是 text_candidates —— 只回 segments 会让这些格式
                                               # 在详情页被误判成「原图没有内嵌的生成参数来源」。
                                               "segments": [{key: value for key, value in segment.items()
                                                             if key != "raw_payload_b64"}
                                                            for segment in (archive_record["result"].get("segments") or [])],
                                               "chunks": [{key: value for key, value in chunk.items()
                                                          if key != "raw_payload_b64"}
                                                         for chunk in (archive_record["result"].get("chunks") or [])],
                                               "text_chunks": archive_record["result"].get("text_chunks") or [],
                                               "text_candidates": archive_record["result"].get("text_candidates") or [],
                                               "errors": archive_record["result"].get("errors", [])[:10]}}
                                   if archive_record is not None else None),
               "generation": generation_record,
               "meta": meta, "parsed": parse_meta(meta),
               # E5-7：区分「读失败」与「本来就没有」。为 None 时前端照旧说「没有内嵌生成信息」；
               # 有值时前端改说「读取失败：<原因>」—— 不再把「读不到」讲成「本来没有」。
               "metaError": meta_fail_reason(p)}
        from backend.metadata.wire import seed_text_for_client
        out = seed_text_for_client(out)
        if verified is not None and _SQL_QUERY is not None and not out.get('metaError'):
            META_RESPONSE_CACHE.put(self._meta_cache_key(verified,p),out)
        return self._json(out)

    # ---- 单图查看（V2.10.7）--------------------------------------------

    def api_solo(self):
        """接收一张外部图片 -> 落 SOLO_DIR -> 返回绝对路径（前端随后照常预览）。

        只收原始字节，且 Content-Type 必须显式是受支持的图片类型：跨站页面想
        往这里塞文件，必先过 CORS 预检（image 系与 application/octet-stream 都
        不在安全列表里），再叠加 do_POST 顶部的 _same_origin()，两道都过不去。
        库里原本的图片不会走这条路 —— 前端只有「打开图片」按钮会调它。
        """
        ct = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ct not in SOLO_CTYPES:
            return self._json({"error": "unsupported content-type"}, 415)
        if not self.headers.get("Content-Length"):
            return self._json({"error": "length required"}, 411)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json({"error": "bad request"}, 400)
        if n <= 0:
            return self._json({"error": "empty body"}, 400)
        if n > SOLO_MAX_BYTES:
            return self._json(
                {"error": "图片超过 %d MB 上限" % (SOLO_MAX_BYTES >> 20)}, 413)
        raw = self._post_body        # ε-14：body 已在 do_POST 顶部统一消费
        if len(raw) != n:
            return self._json({"error": "incomplete body"}, 400)
        ext = solo_sniff(raw)
        if not ext:
            return self._json({"error": "仅支持 PNG / JPG / WEBP / GIF / BMP"}, 415)
        dest = SOLO_DIR / (hashlib.sha1(raw).hexdigest()[:16] + ext)
        try:
            SOLO_DIR.mkdir(parents=True, exist_ok=True)
            if not dest.is_file() or dest.stat().st_size != len(raw):
                # 先写 .part 再原子换名：避免前端在写盘途中读到半截文件。
                # .part 名字带线程号，并发上传同一张图时不会互相踩。
                tmp = SOLO_DIR / ("%s.%s.part" % (dest.name, _tmp_tag()))
                tmp.write_bytes(raw)
                os.replace(tmp, dest)
        except OSError as ex:
            return self._json(
                {"error": "写入失败：%s: %s" % (type(ex).__name__, ex)}, 500)
        solo_prune()
        return self._json({"ok": True, "path": str(dest), "name": dest.name,
                           "size": len(raw), "dir": str(SOLO_DIR)})

    # ---- POST ----------------------------------------------------------

    def _same_origin(self) -> bool:
        """校验 POST 来源：无 Origin（同源 fetch/旧浏览器）放行；
        带 Origin 时必须与 Host 一致，跨站网页发来的请求一律拒绝。"""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            return urllib.parse.urlparse(origin).netloc == self.headers.get("Host", "")
        except Exception:
            return False

    # P1-38：带写副作用的 GET 端点。浏览器对 <img>/<script>/<link> 发起的跨站
    # 请求**不带 Origin**，所以光靠 _same_origin() 拦不住；这几条必须叠一层
    # 「跨站请求不得转发」的手段：
    #   ① Sec-Fetch-Site: 现代 Chromium/Edge/Firefox 对**所有**请求都带，
    #      值为 cross-site 即跨站 —— 这是最直接的判据，且 <img> 也带；
    #   ② Origin 存在且非环回 → 拒（带 Origin 的场景走 _same_origin 同款口径）；
    #   ③ Referer 存在且非环回 → 拒（老浏览器兜底）。
    # ⚠️ B8-P1-3（CMV3-LOCAL-FIX，2026-10-10，从旧工程回植）：以上三条在「三个头
    #    **全缺**」时原先一律放行（fail-open）⇒ `<img referrerpolicy="no-referrer">`
    #    或任何不发 Sec-Fetch-Site 的旧浏览器照样能把件取走。现在：**有头就按头判**，
    #    三个都没有才回退到浏览器指纹（`_browser_fetch`）—— 浏览器一律拒，
    #    本机脚本放行（否则门禁/CLI 全被打成 403）。
    # ⚠️ 只对下面这几个端点 + `SENSITIVE_GET_PATHS` 生效，**不**能全局加：
    #    静态资源经 <img>/<link> 加载，被这些头带着走会把正常页面弄坏。
    WRITE_GET_PATHS = ("/api/pull", "/api/pull/ack", "/api/pull/nack",
                       "/api/pull/drop", "/api/pull/release", "/api/pull/status",
                       # B8-P1-2（CMV3-LOCAL-FIX，2026-10-10）：下面这些 GET 同样带
                       # **写副作用**，此前榜单上没有它们 ⇒ 任意网页一张 <img> 就能让
                       # 后端起全量索引 / 换根迁移线程、逼它写缩略图缓存，还能把
                       # 「取走即清」的迁移报告吞掉 —— 用户唯一一次换根提示被吃掉，
                       # 界面上看起来什么都没发生。
                       "/api/stats",           # ensure_index / ensure_nsfw / root_migrate_maybe
                       "/api/migrate_report",  # 取走即清（破坏性读）
                       "/api/search",          # ensure_index（冷启动会起索引线程）
                       "/api/thumb",           # 写缩略图缓存
                       "/api/trash_thumb",     # 写回收站缩略图缓存
                       "/api/dupes",           # 全目录 Pillow 解码：读盘 + CPU 放大
                       )

    # B8-P1-1（CMV3-LOCAL-FIX，2026-10-10）：没有写副作用、但**会吐出本机信息**的 GET
    # —— 目录选择器返回全盘一级目录名 + 绝对路径（含 ComfyUI 安装路径）。今天跨源还
    # 读不到响应体（本端不回 ACAO），可一旦 CORS 放宽或 Host 白名单被绕开，它就是
    # **整个文件系统的枚举器**。所以与写副作用 GET 同款拒跨站，先把口子收在同一道闸门上。
    SENSITIVE_GET_PATHS = ("/api/browse",)

    @staticmethod
    def _origin_is_loopback(url: str) -> bool:
        """判断一个 URL/Origin 串是否指向环回地址（空串也算放行，由调用方决定）。"""
        if not url:
            return True
        try:
            host = (urllib.parse.urlparse(url).hostname or "").lower()
        except Exception:
            return False
        return host in ("127.0.0.1", "localhost", "::1", "[::1]")

    def _browser_fetch(self) -> bool:
        """这条请求带不带**浏览器指纹**（Fetch Metadata 头 / 浏览器 UA）？

        B8-P1-3（CMV3-LOCAL-FIX，2026-10-10）：只在「Sec-Fetch-Site / Origin /
        Referer **三个头全缺**」这一个歧义点上用得上 —— 浏览器
        （`<img referrerpolicy="no-referrer">`、旧浏览器）一律当跨站；本机脚本
        （门禁 / CLI / 用户自己的工具）放行，因为它们本来就能直接读写文件系统，
        对它们做 CSRF 判定没有意义，只会把本地工具链打成一片 403。
        **只要有一个头能判，就轮不到它**（见调用方）。
        """
        if (self.headers.get("Sec-Fetch-Site") is not None
                or self.headers.get("Sec-Fetch-Mode") is not None):
            return True
        return "mozilla" in (self.headers.get("User-Agent") or "").lower()

    def _cross_site_write_get(self) -> bool:
        """True = 这是一条**跨站**对写副作用 GET 的请求，必须拒。

        B8-P1-3（CMV3-LOCAL-FIX，2026-10-10）：原实现在「三个头**全缺**」时直接放行
        （fail-open）⇒ `<img referrerpolicy="no-referrer">` 本就不带 Referer、
        旧浏览器也不发 Sec-Fetch-Site，于是照样能把件取走并冻结。现在改成：
        **只要能判就判**（有头就按头判），三个都没有时才回退到浏览器指纹 ——
        见 `_browser_fetch`。
        """
        # ① Sec-Fetch-Site（最可靠：<img> 也带）
        sfs = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if sfs:
            return sfs not in ("same-origin", "same-site", "none")
        # ② Origin 在就按 Origin 判（非环回 = 跨站）
        origin = self.headers.get("Origin")
        if origin is not None:
            return not self._origin_is_loopback(origin)
        # ③ Referer 兜底（无 Sec-Fetch-Site 时）
        referer = self.headers.get("Referer")
        if referer:
            return not self._origin_is_loopback(referer)
        # ④ 三个头全缺：**只有这里**存在「放行还是拒绝」的分歧 ⇒ 交给浏览器指纹。
        return self._browser_fetch()

    def do_POST(self):
        if not self._host_ok():
            return self._json({"error": "forbidden host"}, 403)
        u = urllib.parse.urlparse(self.path)
        # ε-12（§6.3）：POST body 全局上限（各端点 rfile.read 前的统一闸门）。
        # 超限时**不读 body** 并断开连接 —— keep-alive 下残留 body 若不处理，
        # 会被下一次请求当成请求行解析出乱码；close_connection 让服务端直接关。
        if self.headers.get("Content-Length") is not None:
            try:
                _cl = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self.close_connection = True
                return self._json({"error": "bad content-length"}, 400)
            if _cl < 0 or _cl > MAX_BODY_BYTES:
                self.close_connection = True
                log("[http] 拒绝超大 POST body：%d 字节 (>%d) %s"
                    % (_cl, MAX_BODY_BYTES, u.path))
                return self._json(
                    {"error": "body 超过 %d MB 上限" % (MAX_BODY_BYTES >> 20)}, 413)
            # ε-14（§6.8）：body 在此**统一消费** —— 此前 /api/restart、/api/activate
            # 等不读 body 的路由会把 keep-alive 连接上的残留字节留给下一个请求，
            # 被当成请求行解析出乱码（HTTP/1.1 复用连接的必踩坑）。
            # 各分支改从 self._post_body 取，不再各自 rfile.read。
            self._post_body = self.rfile.read(_cl)
        else:
            self._post_body = b""
        # CSRF 防护：跨站请求必带 Origin 头，同源 POST 的 Origin 与 Host 一致
        if not self._same_origin():
            return self._json({"error": "forbidden"}, 403)
        # 对读 JSON body 的写操作端点强制 application/json，
        # 杜绝 text/plain 简单请求绕过 CORS 预检（/api/open 只用查询参数，不在列）
        if u.path in ("/api/root", "/api/delete", "/api/fav", "/api/tags",
                      "/api/trash_restore", "/api/trash_purge", "/api/meta_batch",
                      "/api/batch_export", "/api/send", "/api/export", "/api/export_save",
                      "/api/open_in_comfy", "/api/recent", "/api/mark", "/api/prefs",
                      "/api/datadir", "/api/nsfw/label", "/api/save_download",
                      "/api/update/config", "/api/v2/playback/start", "/api/v2/playback/close",
                      "/api/v2/relocation/prepare", "/api/v2/models/roots/register",
                      "/api/v2/models/verification/start", "/api/v2/models/verification/cancel",
                      "/api/v2/models/files/register", "/api/v2/models/references/confirm",
                      "/api/v2/models/references/revoke", "/api/v2/workflows/import",
                      "/api/v2/workflows/sources/publish", "/api/v2/workflows/sources/import",
                      "/api/v2/relations/executions", "/api/v2/relations/candidates",
                      "/api/v2/relations/confirm", "/api/v2/relations/revoke",
                      "/api/v2/workflows/confirm", "/api/v2/workflows/revoke") or u.path.startswith('/api/v2/classic/data/'):
            ct = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ct != "application/json":
                return self._json({"error": "forbidden"}, 403)
        qs = urllib.parse.parse_qs(u.query)
        if not self._auth_ok(qs):
            return self._json({"error": "unauthorized"}, 401)
        if MIGRATION_PROTECT:
            return self._json({"error": "migration protection is active",
                               "code": "MIGRATION_PROTECTED"}, 503)
        if _SQL_ORG is not None and u.path == "/api/datadir":
            return self._json({"error": "数据库模式搬家需要关闭程序后执行；设置界面的自动搬家尚未接入",
                               "code": "OFFLINE_RELOCATION_REQUIRED"}, 503)
        try:
            if u.path.startswith('/api/v2/classic/data/'):
                from backend.storage.classic_data import create_import,read_job,bind_installation
                try:
                    request=json.loads(self._post_body.decode('utf-8'))
                    if not isinstance(request,dict): raise ValueError('资料操作参数无效')
                    action=u.path.rsplit('/',1)[-1]
                    if action=='legacy-prepare':
                        if set(request)!={'sourceDataDir','destination'}: raise ValueError('需要旧数据目录和新的材料目录')
                        if any(not isinstance(v,str) or not Path(v).is_absolute() for v in request.values()):
                            raise ValueError('资料目录需要完整路径')
                        job=create_import(PORTABLE_FLAG,DATA_DIR,request['sourceDataDir'],request['destination'])
                        return self._classic_handoff('preparation',job)
                    if action=='legacy-install':
                        if set(request)!={'preparationJobId','bundleDirectory'}: raise ValueError('需要准备任务和新的安装材料目录')
                        preparation=read_job(PORTABLE_FLAG,DATA_DIR,'preparation',request['preparationJobId'])
                        if preparation['state']!='completed' or not preparation.get('status',{}).get('preparationReady'):
                            raise ValueError('准备结果尚未通过核对')
                        from backend.migration.installation_job import create_installation_job,read_installation_job
                        path=create_installation_job(preparation['destination'],request['bundleDirectory'],PORTABLE_FLAG,
                                                     run_uuid=preparation['runUuid'])
                        job=read_installation_job(PORTABLE_FLAG,path.parent.name)
                        bind_installation(PORTABLE_FLAG,DATA_DIR,preparation['jobId'],job)
                        return self._classic_handoff('installation',job)
                    if action=='run':
                        if set(request)!={'kind','jobId'}: raise ValueError('需要明确的资料任务')
                        job=read_job(PORTABLE_FLAG,DATA_DIR,request['kind'],request['jobId'])
                        return self._classic_handoff(request['kind'],job)
                    if action=='exit':
                        if request: raise ValueError('退出请求不接受其他字段')
                        self._json({'ok':True,'exiting':True})
                        timer=threading.Timer(.3,self.server.shutdown); timer.daemon=True; timer.start()
                        return
                    return self._json({'error':'未知资料操作'},404)
                except (ValueError,OSError,RuntimeError,UnicodeError) as error:
                    return self._json({'error':str(error)},400)
            if u.path == '/api/v2/workflows/sources/import':
                if _SQL_WORKFLOWS is None:
                    return self._json({'error':'工作流来源需要数据库模式','code':'CAPABILITY_UNAVAILABLE'},501)
                from backend.metadata.workflow import WorkflowIdentityError, WorkflowFormatError
                from backend.metadata.workflow_source_store import WorkflowSourceStoreError, WorkflowSourceStoreConflict
                from backend.metadata.workflow_source_bindings import WorkflowSourceBindingError, WorkflowSourceBindingConflict
                try:
                    request=json.loads(self._post_body.decode('utf-8'))
                    if not isinstance(request,dict) or set(request) != {'sourceUuid','workflowUuid','name'} or not all(isinstance(value,str) and value.strip() for value in request.values()):
                        raise ValueError('需要明确来源、家族身份和名称')
                    return self._json(_SQL_WORKFLOWS.import_source(request['sourceUuid'],
                        workflow_uuid=request['workflowUuid'],name=request['name']))
                except (ValueError,WorkflowFormatError) as error:
                    return self._json({'error':str(error)},400)
                except (WorkflowSourceStoreConflict,WorkflowSourceBindingConflict,WorkflowIdentityError) as error:
                    return self._json({'error':str(error),'code':'WORKFLOW_SOURCE_CONFLICT'},409)
                except (WorkflowSourceStoreError,WorkflowSourceBindingError) as error:
                    return self._json({'error':str(error)},404)
            if u.path == '/api/v2/workflows/sources/publish':
                if _SQL_WORKFLOWS is None:
                    return self._json({'error':'工作流来源需要数据库模式','code':'CAPABILITY_UNAVAILABLE'},501)
                from backend.metadata.workflow_source_store import WorkflowSourceStoreError, WorkflowSourceStoreConflict
                try:
                    request=json.loads(self._post_body.decode('utf-8'))
                    if not isinstance(request,dict) or set(request) != {'snapshotUuid'} or not isinstance(request['snapshotUuid'],str) or not request['snapshotUuid'].strip():
                        raise ValueError('需要明确的元数据快照身份')
                    return self._json(_SQL_WORKFLOWS.publish_sources(request['snapshotUuid']))
                except ValueError as error:
                    return self._json({'error':str(error)},400)
                except WorkflowSourceStoreConflict as error:
                    return self._json({'error':str(error),'code':'WORKFLOW_SOURCE_CONFLICT'},409)
                except WorkflowSourceStoreError as error:
                    return self._json({'error':str(error)},404)
            if u.path == '/api/v2/workflows/import':
                if _SQL_WORKFLOWS is None:
                    return self._json({'error':'工作流库需要数据库模式','code':'CAPABILITY_UNAVAILABLE'},501)
                from backend.metadata.workflow import WorkflowIdentityError, WorkflowFormatError
                try:
                    request=json.loads(self._post_body.decode('utf-8'))
                    if not isinstance(request,dict) or set(request) != {'rawWorkflow','workflowUuid','name'}:
                        raise ValueError('需要工作流原文、明确家族身份和名称')
                    if not all(isinstance(request[key],str) and request[key].strip() for key in request):
                        raise ValueError('工作流导入字段必须是非空文本')
                    result=_SQL_WORKFLOWS.capture(request['rawWorkflow'],
                        workflow_uuid=request['workflowUuid'],name=request['name'])
                    return self._json(result)
                except (ValueError,WorkflowFormatError) as error:
                    return self._json({'error':str(error)},400)
                except WorkflowIdentityError as error:
                    return self._json({'error':str(error),'code':'WORKFLOW_IDENTITY_CONFLICT'},409)
            if u.path in ('/api/v2/models/roots/register','/api/v2/models/verification/start',
                          '/api/v2/models/verification/cancel','/api/v2/models/files/register',
                          '/api/v2/models/references/confirm','/api/v2/models/references/revoke'):
                if _SQL_MODELS is None:
                    return self._json({'error':'模型身份功能需要数据库模式','code':'CAPABILITY_UNAVAILABLE'},501)
                from backend.metadata.model_api import dispatch_model_write
                from backend.metadata.model_verification_jobs import ModelVerificationJobError
                from backend.metadata.model_identity_store import ModelIdentityConflictError
                try:
                    result=dispatch_model_write(_SQL_MODELS,u.path.removeprefix('/api/v2/models/'),
                                                json.loads(self._post_body.decode('utf-8')))
                    return self._json(result)
                except ModelIdentityConflictError as error:
                    return self._json({'error':str(error),'code':'MODEL_IDENTITY_CONFLICT'},409)
                except (ValueError,OSError) as error:
                    return self._json({'error':str(error)},400)
                except ModelVerificationJobError as error:
                    return self._json({'error':str(error),'code':'MODEL_JOB_CONFLICT'},409)
            if u.path == '/api/v2/migration/install-job':
                from backend.migration.preparation_job import read_preparation_job
                from backend.migration.installation_job import create_installation_job, read_installation_job, pending_installation_job
                from backend.migration.status import preparation_status
                try:
                    request=json.loads(self._post_body.decode('utf-8'))
                    if not isinstance(request,dict) or set(request)!={'preparationJobId','bundleDirectory'} \
                            or not isinstance(request['bundleDirectory'],str) or not Path(request['bundleDirectory']).is_absolute():
                        raise ValueError('需要准备任务编号和独立安装材料目录完整路径')
                    preparation=read_preparation_job(PORTABLE_FLAG,request['preparationJobId'])
                    if preparation['source'] != str(DATA_DIR.absolute()):
                        return self._json({'error':'准备任务不属于当前数据目录'},409)
                    if not preparation.get('runUuid'):
                        raise ValueError('准备任务尚未完成核对')
                    with _DD_MIG_LOCK:
                        pending=pending_installation_job(PORTABLE_FLAG,preparation['destination'])
                        if pending:
                            return self._json({'error':'已有安装任务，请恢复同一次任务',
                                               'code':'INSTALLATION_PENDING','job':pending},409)
                        status=preparation_status(preparation['destination'],preparation['runUuid'])
                        if not status['preparationReady']:
                            raise ValueError('准备尚未通过核对；已有安装请使用原任务恢复')
                        path=create_installation_job(preparation['destination'],request['bundleDirectory'],PORTABLE_FLAG,
                                                     run_uuid=preparation['runUuid'])
                        job=read_installation_job(PORTABLE_FLAG,path.parent.name)
                    return self._classic_handoff('installation',job)
                except (ValueError,OSError,RuntimeError) as error:
                    return self._json({'error':str(error)},400)
            if u.path == '/api/v2/migration/recovery-selection-job':
                from backend.migration.recovery_selection_job import create_selection_job, read_selection_job, discover_selection_jobs
                if _SQL_ORG is None:
                    return self._json({'error':'备份后选择恢复库需要当前数据库可读取'},409)
                try:
                    request=json.loads(self._post_body.decode('utf-8'))
                    if not isinstance(request,dict) or set(request)!={'restoreJobId','backupDirectory'} \
                            or any(not isinstance(value,str) for value in request.values()):
                        raise ValueError('需要已完成恢复任务编号和独立备份目录')
                    with _DD_MIG_LOCK:
                        pending=[job for job in discover_selection_jobs(PORTABLE_FLAG,DATA_DIR)
                                 if job['state'] in ('waiting-for-exit','selecting')]
                        if pending:
                            return self._json({'error':'已有恢复库切换任务，请检查原任务',
                                               'code':'RECOVERY_SELECTION_PENDING','job':pending[0]},409)
                        path=create_selection_job(PORTABLE_FLAG,request['restoreJobId'],request['backupDirectory'])
                        job=read_selection_job(PORTABLE_FLAG,path.parent.name)
                    return self._classic_handoff('recoverySelection',job)
                except (ValueError,OSError,RuntimeError) as error:
                    return self._json({'error':str(error)},400)
            if u.path == '/api/v2/migration/recovery-job':
                from backend.migration.recovery_job import create_recovery_job, read_recovery_job, discover_recovery_jobs
                try:
                    request=json.loads(self._post_body.decode('utf-8'))
                    if not isinstance(request,dict) or request.get('action') not in ('export','restore'):
                        raise ValueError('需要完整恢复动作和独立目标目录')
                    expected={'action','destination'} | ({'bundle'} if request['action']=='restore' else set())
                    if set(request)!=expected or any(not isinstance(request[key],str) for key in expected):
                        raise ValueError('恢复参数不完整或包含未知字段')
                    if request['action']=='export' and _SQL_ORG is None:
                        return self._json({'error':'完整恢复材料导出需要数据库模式'},409)
                    with _DD_MIG_LOCK:
                        pending=[job for job in discover_recovery_jobs(PORTABLE_FLAG,DATA_DIR)
                                 if job['state'] in ('waiting-for-exit','running')]
                        if pending:
                            return self._json({'error':'已有完整恢复任务，请先检查原任务',
                                               'code':'RECOVERY_PENDING','job':pending[0]},409)
                        path=create_recovery_job(DATA_DIR,PORTABLE_FLAG,request['action'],request['destination'],
                                                 bundle=request.get('bundle'))
                        job=read_recovery_job(PORTABLE_FLAG,path.parent.name)
                    return self._classic_handoff('recovery',job)
                except (ValueError,OSError,RuntimeError) as error:
                    return self._json({'error':str(error)},400)
            if u.path == '/api/v2/migration/export-organization':
                if _SQL_ORG is None:
                    return self._json({'error':'组织数据导出需要数据库模式'},409)
                from backend.migration.export_legacy import export_organization
                try:
                    request=json.loads(self._post_body.decode('utf-8'))
                    if not isinstance(request,dict) or set(request)!={'destination'} or not isinstance(request['destination'],str):
                        raise ValueError('需要新的独立导出目录完整路径')
                    destination=Path(request['destination'])
                    if not destination.is_absolute() or '..' in destination.parts:
                        raise ValueError('导出目录必须是完整路径')
                    source=DATA_DIR.absolute()
                    if destination==source or source in destination.parents or destination in source.parents:
                        raise ValueError('导出目录必须与当前数据目录完全分离')
                    report=export_organization(_SQL_ORG.database,_SQL_ORG.profile_uuid,destination)
                    return self._json({'scope':report['scope'],'readyForLegacyStartup':False,
                                       'report':str(destination/'export-manifest.json'),
                                       'requiresSeparateRecovery':report['retainedDomains']})
                except (ValueError,OSError,RuntimeError) as error:
                    return self._json({'error':str(error)},400)
            if u.path == '/api/v2/migration/prepare-job':
                if _SQL_ORG is not None:
                    return self._json({'error':'当前已使用数据库，无需旧数据迁移准备'},409)
                from backend.migration.preparation_job import create_preparation_job, read_preparation_job, pending_preparation_job
                try:
                    request=json.loads(self._post_body.decode('utf-8'))
                    if not isinstance(request,dict) or set(request)!={'destination'} or not isinstance(request['destination'],str) or not Path(request['destination']).is_absolute():
                        raise ValueError('需要新的独立准备目录完整路径')
                    with _DD_MIG_LOCK:
                        pending=pending_preparation_job(PORTABLE_FLAG,DATA_DIR)
                        if pending:
                            return self._json({'error':'已有未完成准备任务，请恢复同一任务',
                                               'code':'PREPARATION_PENDING','job':pending},409)
                        job_path=create_preparation_job(DATA_DIR,request['destination'],PORTABLE_FLAG)
                        job=read_preparation_job(PORTABLE_FLAG,job_path.parent.name)
                    return self._json({'ok':True,'job':job,'handoffRequired':True,'authorityPublished':False})
                except (ValueError,OSError) as error:
                    return self._json({'error':str(error)},400)
            if u.path == '/api/v2/relocation/prepare':
                if _SQL_ORG is None:
                    return self._json({'error':'当前数据模式尚不支持此搬家流程','code':'CAPABILITY_UNAVAILABLE'},501)
                from backend.migration.relocation_job import create_relocation_job,latest_relocation_job,read_relocation_job
                try:
                    request=json.loads(self._post_body.decode('utf-8'))
                    if not isinstance(request,dict) or set(request)!={'destination'} or \
                            not isinstance(request['destination'],str) or not Path(request['destination']).is_absolute():
                        raise ValueError('需要一个新的完整目标目录路径')
                    with _DD_MIG_LOCK:
                        previous=latest_relocation_job(PORTABLE_FLAG,DATA_DIR)
                        if previous and previous['state'] not in ('completed','error'):
                            return self._json({'error':'已有未完成搬家任务，请先完成或恢复该任务',
                                               'code':'RELOCATION_PENDING','job':previous},409)
                        job_path=create_relocation_job(DATA_DIR,request['destination'],PORTABLE_FLAG)
                        job=read_relocation_job(PORTABLE_FLAG,job_path.parent.name)
                    return self._classic_handoff('relocation',job)
                except (ValueError,OSError) as error:
                    return self._json({'error':str(error),'code':'invalid_relocation_destination'},400)
            if u.path in ('/api/v2/playback/start','/api/v2/playback/close'):
                if _SQL_CATALOG is None:
                    return self._json({'error':'Playback requires catalog identity','code':'CAPABILITY_UNAVAILABLE'},501)
                from backend.storage.media_stream import verified_playback, MediaStreamConflict
                try:
                    d=json.loads(self._post_body.decode('utf-8'))
                    if not isinstance(d,dict) or not all(isinstance(d.get(key),str) and d[key]
                            for key in ('path','mediaId','expectedVersion')):
                        raise ValueError('Playback requires path, mediaId and expectedVersion')
                    p=Path(d['path']).resolve()
                    identity=(str(Path(STATE['root']).resolve()),str(p),d['mediaId'],d['expectedVersion'])
                    if u.path.endswith('/close'):
                        if not isinstance(d.get('playback'),str):
                            raise ValueError('Playback token required')
                        return self._json({'ok':True,'closed':PLAYBACK_SESSIONS.close(d['playback'],identity)})
                    if not p.is_file() or not inside_root(p) or p.suffix.lower() not in VIDEO_EXTS:
                        return self._json({'error':'Video unavailable or outside root'},404)
                    from backend.storage.playback_service import start_playback
                    token,identity=start_playback(PLAYBACK_SESSIONS,_SQL_CATALOG,identity[0],p,
                                                  d['mediaId'],d['expectedVersion'])
                    if str(Path(STATE['root']).resolve())!=identity[0]:
                        PLAYBACK_SESSIONS.close(token,identity)
                        return self._json({'error':'Root changed during playback preparation','code':'media_conflict'},409)
                    return self._json({'playback':token,'mediaId':d['mediaId'],
                                      'revisionId':d['expectedVersion'][4:],'lifetime':PLAYBACK_SESSIONS.lifetime})
                except (PlaybackExpired,MediaStreamConflict) as error:
                    return self._json({'error':str(error),'code':'media_conflict'},409)
                except WorkBusy as error:
                    return self._json({'error':str(error),'code':getattr(error,'code','media_busy')},getattr(error,'status',503))
                except (ValueError,UnicodeError) as error:
                    return self._json({'error':str(error),'code':'invalid_playback'},400)
            if u.path == "/api/activate":
                # 单实例：由二次启动的本程序进程 POST 调用（GET 会被任意网页用
                # <img>/<link> 触发成骚扰性弹窗，故只接受 POST + 同源校验）
                port = self.server.server_address[1]
                open_url = f"http://127.0.0.1:{port}/"
                if STATE.get("lan"):
                    open_url += f"?token={STATE.get('token', '')}"
                _t = threading.Timer(0.1, lambda: webbrowser.open(open_url))
                _t.daemon = True   # ε-13（§6.6）：非守护会拖住进程退出；Timer 构造器不收 daemon 关键字
                _t.start()
                log("[activate] 已唤起浏览器")
                return self._json({"ok": True})
            if u.path in ('/api/v2/workflows/confirm','/api/v2/workflows/revoke'):
                if _SQL_WORKFLOW_DECISIONS is None:
                    return self._json({'error':'工作流确认需要数据库模式','code':'CAPABILITY_UNAVAILABLE'},501)
                from backend.metadata.workflow_decisions import (WorkflowDecisionConflict,
                    WorkflowDecisionScopeError,WorkflowDecisionNotFound,WorkflowDecisionTransitionError)
                try:
                    def unique_workflow_fields(pairs):
                        fields = {}
                        for key, value in pairs:
                            if key in fields:
                                raise ValueError('Duplicate workflow decision field')
                            fields[key] = value
                        return fields
                    def invalid_workflow_constant(value):
                        raise ValueError('Non-finite workflow decision value: ' + value)
                    request = json.loads(self._post_body.decode('utf-8'),
                        object_pairs_hook=unique_workflow_fields,
                        parse_constant=invalid_workflow_constant)
                    fields = {'captureUuid','expectedDecisionVersion','reason'}
                    if u.path.endswith('/confirm'):
                        fields.add('targetWorkflowUuid')
                    if not isinstance(request,dict) or set(request)!=fields:
                        raise ValueError('Invalid workflow decision fields')
                    arguments = {'expected_decision_version':request['expectedDecisionVersion'],'reason':request['reason']}
                    result = (_SQL_WORKFLOW_DECISIONS.confirm(_SQL_ORG.profile_uuid,request['captureUuid'],
                        request['targetWorkflowUuid'],**arguments) if u.path.endswith('/confirm') else
                        _SQL_WORKFLOW_DECISIONS.revoke(_SQL_ORG.profile_uuid,request['captureUuid'],**arguments))
                    return self._json(result)
                except WorkflowDecisionConflict as error:
                    return self._json({'error':str(error),'code':'workflow_decision_conflict'},409)
                except (ValueError,UnicodeError,TypeError,WorkflowDecisionScopeError,
                        WorkflowDecisionNotFound,WorkflowDecisionTransitionError) as error:
                    return self._json({'error':str(error)},400)
            if u.path in ('/api/v2/relations/executions','/api/v2/relations/candidates',
                          '/api/v2/relations/confirm','/api/v2/relations/revoke'):
                if _SQL_RELATIONS is None:
                    return self._json({'error':'创作关系需要数据库模式','code':'CAPABILITY_UNAVAILABLE'},501)
                from backend.metadata.relation_api import dispatch_relation_write
                def unique_relation_fields(pairs):
                    result = {}
                    for key,value in pairs:
                        if key in result:
                            raise ValueError('Duplicate creation relation field')
                        result[key] = value
                    return result
                try:
                    request = json.loads(self._post_body.decode('utf-8'), object_pairs_hook=unique_relation_fields,
                        parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Non-finite relation value')))
                    return self._json(dispatch_relation_write(_SQL_RELATIONS, _SQL_ORG.profile_uuid,
                        u.path.removeprefix('/api/v2/relations/'),request))
                except (ValueError,TypeError,UnicodeError,RecursionError) as error:
                    conflict = str(error) in ('Decision version conflict','Confirmed derivation would form a cycle')
                    return self._json({'error':str(error),'code':'relation_conflict' if conflict else 'invalid_relation'},
                                      409 if conflict else 400)
            if u.path == "/api/v2/media/query":
                if _SQL_QUERY is None:
                    return self._json({"error": "数据库查询尚未启用", "code": "CAPABILITY_UNAVAILABLE"}, 501)
                from backend.query.spec import QuerySpecError
                from backend.query.cursor import CursorError
                def unique_query_object(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise QuerySpecError("Duplicate request field")
                        result[key] = value
                    return result
                def invalid_query_constant(value):
                    raise QuerySpecError("Non-finite request value")
                try:
                    request = json.loads(self._post_body.decode("utf-8"),
                                         object_pairs_hook=unique_query_object,
                                         parse_constant=invalid_query_constant)
                    result = _SQL_QUERY.query(request, safety_context={
                        'modelId': load_nsfw_model() if nsfw_enabled() else None,
                        'threshold': load_nsfw_threshold()})
                    return self._json(result)
                except CursorError as error:
                    return self._json({"error": str(error), "code": error.code}, error.status)
                except (QuerySpecError, ValueError, UnicodeError, RecursionError) as error:
                    return self._json({"error": str(error), "code": "invalid_query"}, 400)
            if u.path in ("/api/v2/catalog/scan/start", "/api/v2/catalog/scan/cancel"):
                if _SQL_SCAN_JOBS is None:
                    return self._json({"error": "数据库扫描尚未启用", "code": "CAPABILITY_UNAVAILABLE"}, 501)
                from backend.storage.scan_jobs import ScanBusy
                from backend.storage.catalog import CatalogError
                try:
                    request = json.loads(self._post_body.decode("utf-8"))
                    field = "rootId" if u.path.endswith("/start") else "jobId"
                    if not isinstance(request, dict) or set(request) != {field} or not isinstance(request[field], str):
                        raise ValueError("Explicit scan identity required")
                    result = (_SQL_SCAN_JOBS.start(request[field]) if field == "rootId"
                              else _SQL_SCAN_JOBS.cancel(request[field]))
                    return self._json(result)
                except ScanBusy as error:
                    return self._json({"error": str(error), "code": "scan_busy"}, 409)
                except (ValueError, UnicodeError, CatalogError) as error:
                    return self._json({"error": str(error), "code": "invalid_scan"}, 400)
            if u.path == "/api/solo":              # 单图查看：收一张外部图片（V2.10.7）
                return self.api_solo()
            if u.path == "/api/update/check":     # 热更新：检查（联网，同步回结果）
                return self._json(update_check_sync())
            if u.path == "/api/update/apply":     # 热更新：开工（后台跑，前端轮询 status）
                return self._json(update_apply_async())
            if u.path == "/api/update/config":    # 热更新：保存更新源 / 自动检查开关
                body = self._post_body
                try:
                    d = json.loads(body.decode("utf-8"))
                    if not isinstance(d, dict):
                        raise ValueError
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                with SETTINGS_LOCK:
                    s = load_settings()
                    if "update_url" in d:
                        s["update_url"] = str(d.get("update_url") or "").strip()
                    if "auto_check" in d:
                        s["update_auto_check"] = bool(d.get("auto_check"))
                    if not save_settings(s):
                        return self._json({"error": "更新设置保存失败"}, 500)
                return self._json({"ok": True, "update_url": update_url(),
                                   "update_auto_check": update_auto_check()})
            if u.path == "/api/restart":
                # 一键重启：先回包，再由后台线程起新进程并退出自己
                # P1-34：起进程失败的结论在后端线程里才知道，前端可轮询 /api/info
                #        的 restart_failed 字段确认（正常重启时本进程已退出，页面重连）
                _RestartState["ok"] = None
                _RestartState["err"] = ""
                restart_self(self.server)
                return self._json({"ok": True, "msg": "正在重启…"})
            if u.path == "/api/root":
                body = self._post_body
                try:
                    new = str(Path(json.loads(body.decode("utf-8"))["path"]).resolve())
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                if not Path(new).is_dir():
                    return self._json({"error": "目录不存在"}, 400)
                if _SQL_CATALOG is not None:
                    _SQL_CATALOG.ensure_root(new)
                with STATE_LOCK:
                    previous_root = STATE["root"]
                    STATE["root"] = new
                    if previous_root != new:
                        PLAYBACK_SESSIONS.clear()
                with IDX_LOCK:
                    # P1-15：换根即换代。旧根的在跑线程未必立刻察觉（它只在循环里查
                    # STATE["root"]），换代后它即使写回也会被 _index_builder 的闸门拦下。
                    IDX_GEN[0] += 1
                    IDX_THREAD[0] = None
                remembered = True
                if REMEMBER_ROOTS:
                    # B1-3（CMV3-LOCAL-FIX）：必须检查 add_root 的返回值 —— 数据目录不可写
                    # 时「常用目录 / 上次目录」看着改了、重启回原样，界面不能沉默。
                    remembered = add_root(new)
                # Library navigation must preserve user annotations, including
                # temporarily missing files and disconnected storage.
                nsfw_prime(new)                   # NSFW 缓存是按根目录走的，换目录要换表
                resp = {"ok": True, "root": new, "pruned": 0}
                if not remembered:
                    _ws = data_write_state()
                    resp["warning"] = ("目录已切换，但「常用目录 / 上次目录」未能写入数据"
                                       "目录（%s）—— 重启后会回到原来的目录。请检查数据"
                                       "目录是否只读 / 所在盘是否掉线。"
                                       % (_ws.get("error") or "写入失败"))
                    resp["data_write_error"] = _ws.get("error") or ""
                return self._json(resp)
            if u.path == "/api/roots/remove":
                try:
                    path = json.loads(self._post_body.decode("utf-8"))["path"]
                    if not isinstance(path, str) or not path:
                        raise ValueError
                except (ValueError, KeyError, TypeError):
                    return self._json({"error": "bad request"}, 400)
                if not remove_root(path):
                    return self._json({"error": "目录未保存或设置保存失败"}, 400)
                return self._json({"ok": True, "roots": load_roots()})
            if u.path == "/api/batch_export":
                body = self._post_body
                try:
                    d = json.loads(body.decode("utf-8"))
                    paths = d.get("paths") or []
                    dest = str(d.get("dest") or "")
                    if not isinstance(paths, list) or not paths or not dest.strip():
                        raise ValueError
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                verified_targets = None
                if _SQL_CATALOG is not None and d.get("targets") is not None:
                    targets = d["targets"]
                    if (not isinstance(targets, list) or len(targets) != len(paths) or
                            any(not isinstance(target, dict) or target.get("path") != raw
                                for target, raw in zip(targets, paths))):
                        return self._json({"error": "invalid export targets"}, 400)
                    verified_targets = []
                    for target in targets:
                        source = Path(target["path"])
                        if not source.is_file() or not inside_root(source):
                            return self._json({"error": "export source unavailable"}, 404)
                        identity = _SQL_CATALOG.verify(STATE["root"], source)
                        if ((target.get("mediaId") and target["mediaId"] != identity["mediaId"]) or
                                (target.get("expectedVersion") and target["expectedVersion"] != "rev:" + identity["revisionId"])):
                            return self._json({"error": "导出目标已变化，请重新选择", "code": "media_conflict"}, 409)
                        verified_targets.append(identity)
                return self._json(batch_export(paths, dest, verified_targets))
            if u.path == "/api/open":
                p = Path(qs.get("p", [""])[0])
                if not p.is_file() or not inside_root(p):
                    return self._json({"error": "not found or outside root"}, 404)
                reveal_in_explorer(p)          # 打开并强制置前，避免弹到后台
                return self._json({"ok": True})
            if u.path == "/api/delete":
                body = self._post_body
                try:
                    d = json.loads(body.decode("utf-8"))
                    if isinstance(d.get("paths"), list):       # 批量
                        raw = [str(Path(x).resolve()) for x in d["paths"] if x]
                    else:                                      # 单个（兼容旧调用）
                        raw = [str(Path(d["path"]).resolve())]
                    # 去重：同一路径传两次时第二次会因「文件不存在」被计入 failed，
                    # 前端会把已成功删除的文件误标成失败
                    targets = list(dict.fromkeys(raw))
                    # ⚠️ P1-1：这里原本是 [:500] 静默截断，踩过一个坑 —— 服务端只删前 500 条
                    #   却照常回 deleted=500/failed=[]，而前端按「全部路径 − failed」计算已删集合，
                    #   于是第 501 条以后的文件**磁盘还在、界面已消失**（实测 620 条残留 120 个）。
                    #   现在改为：不截断（本机回环请求，几千条也只多几秒）；确实超上限时
                    #   把被丢弃的路径并入 failed 如实上报，前端就不会把它们当成已删。
                    dropped = []
                    if len(targets) > DELETE_MAX_PATHS:
                        dropped = targets[DELETE_MAX_PATHS:]
                        targets = targets[:DELETE_MAX_PATHS]
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                if not targets:
                    return self._json({"error": "bad request"}, 400)
                identity_targets = d.get("targets")
                identities = {}
                if identity_targets is not None:
                    if not isinstance(identity_targets, list):
                        return self._json({"error": "invalid delete identities"}, 400)
                    try:
                        for entry in identity_targets:
                            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                                raise ValueError
                            locator = str(Path(entry["path"]).resolve())
                            if locator in identities:
                                raise ValueError
                            identities[locator] = entry
                        if set(identities) != set(raw):
                            raise ValueError
                    except (ValueError, TypeError, OSError):
                        return self._json({"error": "invalid delete identities"}, 400)
                elif not isinstance(d.get("paths"), list):
                    identities[targets[0]] = d
                if _SQL_CATALOG is not None:
                    from backend.storage.legacy_adapter import IdentityConflictError
                    # Validate the entire requested set before any disk movement.
                    for locator in targets:
                        candidate = Path(locator)
                        if not candidate.is_file() or not inside_root(candidate):
                            continue
                        expected = identities.get(locator, {})
                        verified = _SQL_CATALOG.verify(STATE["root"], candidate)
                        if expected.get("mediaId") and expected["mediaId"] != verified["mediaId"]:
                            raise IdentityConflictError("删除目标身份已变化，请重新选择")
                        if expected.get("expectedVersion") and expected["expectedVersion"] != "rev:" + verified["revisionId"]:
                            raise IdentityConflictError("删除目标内容已变化，请重新查看")
                # 删除去向：请求可显式指定（批量脚本 / 桥接），缺省用设置面板里的偏好
                mode = str(d.get("mode") or "").strip().lower()
                if mode not in DELETE_MODES:
                    mode = load_delete_mode()
                # CMV3-LOCAL-FIX(2026-10-11 · 承接 C4「恢复 system 能力」)：
                #   这里此前有一道 `if _SQL_ORG is not None and mode == "system": 503`
                #   的 stub（"数据库模式的系统回收站操作尚未接入"）。它是 v3 骨架自带的
                #   待办，不是架构约束 —— 系统回收站删除走的是 Windows Shell API
                #   （recycle_bin_delete），与后端用 JSON 还是 SQLite 存组织数据无关。
                #   删掉这道闸后，DB 模式与 legacy 共用下面同一套循环：
                #     系统删除成功 ⇒ 文件进 Windows 回收站、**不动**收藏/标签/标记
                #       （用户从资源管理器还原后路径不变，元数据照样有效）；
                #     目标卷无回收站 ⇒ 自动退回应用内回收站（catalog trash 记账）。
                #   /api/list 直接 scandir 文件系统，删除后列表自然不再出现该件。
                deleted, failed = 0, []
                dead = []              # 删除成功的全部路径（计数 + 目录统计失效用）
                purge_meta = []        # 需要清掉收藏/标签/标记的路径
                keys = {}              # 应用内回收站 key（写快照 / 供撤销还原）
                fellback = []          # 系统回收站不可用、退回应用内回收站的路径
                for t in targets:
                    p = Path(t)
                    # ε-14（§6.8）：路由层此前不做 inside_root —— 本机回环之外，
                    # DNS rebinding / 恶意页面（若同源校验被绕过）可借 POST 删任意盘文件。
                    # 逐条校验：根外的路径进 failed 如实回报，不静默吞。
                    if not inside_root(p):
                        failed.append(t)
                        continue
                    if mode == "system":
                        err = recycle_bin_delete(p)
                        if err is None:
                            pass       # 进了系统回收站：不动收藏/标签/标记 —— 用户从资源管理器
                                       # 还原后路径没变，元数据照样有效，清了反而丢数据
                        else:
                            # 目标卷没有回收站（exFAT / 网络盘）或路径超长 → 退回应用内回收站。
                            # 让「删除」这个意图达成，比点了没反应好；响应里回报数量告知用户
                            err, _key = trash_move(p)
                            if err:
                                failed.append(t)
                                continue
                            keys[t] = _key
                            purge_meta.append(t)
                            fellback.append(t)
                    else:
                        err, _key = trash_move(p)
                        if err:
                            failed.append(t)
                            continue
                        keys[t] = _key
                        purge_meta.append(t)
                    deleted += 1
                    dead.append(t)
                    with IDX_LOCK:                       # 同步清理搜索索引
                        IDX["map"].pop(t, None)
                meta_failed = []                         # §6.1：元数据清理失败要带回去，别让「已删除」独占说法
                if purge_meta and _SQL_ORG is None:       # SQLite keeps organization attached to Asset.
                    # ① 只取快照，不动原表（2026-09-24 外审 P1-1：原顺序是「先清表、后写快照」，
                    #    快照写盘失败时原表已清、回收站又没有快照 ⇒ 收藏/标签/标记真丢。
                    #    现改为先把快照持久化进回收站条目，成功才清原表；快照写失败则原表原地保留）
                    fav_snap, tag_snap, mark_snap = {}, {}, {}
                    with FAV_LOCK:
                        favs = load_favs()
                        for t in purge_meta:
                            if t in favs:
                                fav_snap[t] = favs[t].get("cat") or "默认"
                    with TAGS_LOCK:
                        tgs = load_tags()
                        for t in purge_meta:
                            if t in tgs:
                                tag_snap[t] = list(tgs[t])
                    with MARKS_LOCK:
                        mk = load_marks()
                        for t in purge_meta:
                            if t in mk:
                                mark_snap[t] = mk[t].get("state")
                    snap_ok = True
                    if fav_snap or tag_snap or mark_snap:  # ② 快照先落盘，供还原时恢复
                        with TRASH_LOCK:
                            idx = load_trash()
                            for t in purge_meta:
                                k = keys.get(t)
                                if not k or k not in idx:
                                    continue
                                if t in fav_snap:
                                    idx[k]["fav"] = fav_snap[t]
                                if t in tag_snap:
                                    idx[k]["tags"] = tag_snap[t]
                                if t in mark_snap:
                                    idx[k]["mark"] = mark_snap[t]
                            if not save_trash(idx):      # 快照没落盘 ⇒ 不清原表，元数据原地保留
                                snap_ok = False
                                meta_failed.append("回收站元数据快照")
                    if snap_ok:                          # ③ 快照已持久化才清原表
                        with FAV_LOCK:
                            if fav_snap:
                                favs = load_favs()
                                for t in fav_snap:
                                    favs.pop(t, None)
                                if not save_favs(favs):  # §6.1：文件已删、收藏没清干净 ⇒ 必须说出来
                                    meta_failed.append("收藏")
                        with TAGS_LOCK:
                            if tag_snap:
                                tgs = load_tags()
                                for t in tag_snap:
                                    tgs.pop(t, None)
                                if not save_tags(tgs):
                                    meta_failed.append("标签")
                        with MARKS_LOCK:
                            if mark_snap:
                                mk = load_marks()
                                for t in mark_snap:
                                    mk.pop(t, None)
                                if not _save_json(MARKS_FILE, mk):
                                    meta_failed.append("标记")
                if deleted:
                    census_invalidate()                  # 目录数量变了，下次统计重扫
                failed = failed + dropped                # P1-1：被截断的路径如实计入未删除
                if len(targets) == 1 and failed:         # 单删失败：保持旧响应语义
                    return self._json({"ok": False, "error": failed[0]})
                resp = {"ok": True, "deleted": deleted, "failed": failed,
                        "mode": mode, "fallback": len(fellback),
                        "dropped": len(dropped)}
                if meta_failed:                      # §6.1：文件删了、元数据没跟上 ⇒ 如实带回
                    resp["meta_failed"] = meta_failed
                    resp["warning"] = ("文件已删除，但 " + "、".join(meta_failed)
                                       + " 未能写回（磁盘可能已满或被占用）")
                return self._json(resp)
            if u.path == "/api/trash_restore":
                body = self._post_body
                try:
                    key = str(json.loads(body.decode("utf-8"))["key"])
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                r = trash_restore(key)
                if r.get("ok"):
                    census_invalidate()               # 文件回到根目录，数量要重算
                    with IDX_LOCK:
                        IDX["built_at"] = 0.0         # 索引立即允许增量刷新
                return self._json(r)
            if u.path == "/api/trash_purge":
                body = self._post_body
                try:
                    data = json.loads(body.decode("utf-8")) if body else {}
                    if not isinstance(data, dict):
                        raise ValueError
                    keys = data.get("keys") or []
                    if not isinstance(keys, list):
                        raise ValueError
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                # P0-2（CMV3-LOCAL-FIX）：清空全部必须**显式** `{"all":true}` ——
                # keys 缺字段 / null / [] 三种「前端漏传」形态一律按 no-op 处理，
                # 绝不代用户做「整站清空」这个不可逆的决定。
                census_invalidate()                   # 彻底删除：文件不再存在
                return self._json(trash_purge(keys, all_=(data.get("all") is True)))
            if u.path == "/api/save_download":      # 下载到用户指定目录（设置页可配）
                # 与浏览器下载（/api/file?dl=1）并行的第二条路：用户在设置里配了
                # 「下载保存位置」就落盘到那儿（跨桌面端/web 行为一致、位置可记），
                # 没配则前端根本不会调这里。
                body = self._post_body
                try:
                    d = json.loads(body.decode("utf-8")) if body else {}
                    if not isinstance(d, dict):
                        raise ValueError
                    src = Path(str(d["path"]))
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                if not src.is_file() or not inside_root(src):
                    return self._json({"error": "文件不存在或在根目录之外"}, 404)
                download_identity = None
                if _SQL_CATALOG is not None and (d.get("mediaId") or d.get("expectedVersion")):
                    download_identity = _SQL_CATALOG.verify(STATE["root"], src)
                    if ((d.get("mediaId") and d["mediaId"] != download_identity["mediaId"]) or
                            (d.get("expectedVersion") and d["expectedVersion"] != "rev:" + download_identity["revisionId"])):
                        return self._json({"error": "下载目标已变化，请重新选择", "code": "media_conflict"}, 409)
                dest_root = str(load_settings().get("download_dir") or "").strip()
                if not dest_root:
                    return self._json({"error": "尚未设置下载目录"}, 400)
                destp = Path(dest_root)
                try:
                    destp.mkdir(parents=True, exist_ok=True)
                except OSError as ex:
                    return self._json({"error": f"下载目录无法创建：{ex}"}, 400)
                ext = src.suffix.lower()
                cleaned = bool(d.get("clean")) and ext in _STRIP_BY_EXT
                # 命名与浏览器下载那条路保持一致：抹过的加 _clean 后缀
                name = (src.stem + "_clean" + src.suffix) if cleaned else src.name
                from backend.storage.media_stream import verified_stream, MediaStreamConflict
                from backend.storage.save_download import save_download
                from io import BytesIO
                try:
                    context = (verified_stream(src, download_identity["contentHash"])
                               if download_identity is not None else src.open("rb"))
                    with context as opened:
                        stream = opened[0] if download_identity is not None else opened
                        if cleaned:
                            stream = BytesIO(_STRIP_BY_EXT[ext](stream.read()))
                        target = save_download(destp, name, stream)
                except MediaStreamConflict as ex:
                    return self._json({"error": str(ex), "code": "media_conflict"}, 409)
                except WorkBusy as ex:
                    return self._json({"error": str(ex), "code": getattr(ex, "code", "media_busy")}, getattr(ex, "status", 503))
                except OSError as ex:
                    return self._json({"error": f"写入失败：{ex}"}, 500)
                return self._json({"ok": True, "dest": str(target), "name": target.name,
                                   "cleaned": cleaned})
            if u.path == "/api/prefs":
                # 界面偏好（目前只有删除去向）。放在服务端是因为「删到哪里」由后端执行，
                # 前端 localStorage 在别的窗口/桥接脚本里读不到，会各删各的。
                body = self._post_body
                try:
                    d = json.loads(body.decode("utf-8"))
                    if not isinstance(d, dict):
                        raise ValueError
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                # B1-3（2026-10-09 · CMV3-LOCAL-FIX，从旧工程回植）：「值非法」与「写盘失败」
                # 必须分开报 —— save_* 的 False 同时代表这两件事，此前一律报成「只能是
                # app 或 system」/「只能是 0-3650 的整数」。数据目录不可写时用户按提示怎么改
                # 都不对，而真正的原因（磁盘写不进去）一个字都没说。改为**先校验、后写盘**。
                # B9-P1-2（2026-10-10 · 同上）：再把「逐项写盘」收紧成**两趟** —— 原先一次请求里
                # 排在非法项**前面**的字段已经落盘了（实测 {"delete_mode":"system",
                # "trash_keep_days":99999} 报 400，可 delete_mode 已变成 system：用户以为
                # 这次什么都没改，删除去向却静默换掉，之后所有删除都不再进应用内回收站）。
                # 现在：第 1 趟只校验（一个字段都不落盘），全绿了第 2 趟才统一写盘。
                patch = {}
                if "delete_mode" in d:
                    dm = str(d["delete_mode"] or "").strip().lower()
                    if dm not in DELETE_MODES:
                        return self._json({"error": "delete_mode 只能是 app 或 system"}, 400)
                    patch["delete_mode"] = dm
                if "trash_keep_days" in d:
                    if not valid_keep_days(d["trash_keep_days"]):
                        return self._json({"error": "trash_keep_days 只能是 0-3650 的整数"
                                                    "（0=关闭自动清理）"}, 400)
                    patch["trash_keep_days"] = d["trash_keep_days"]
                if "nsfw_threshold" in d:
                    if not valid_nsfw_threshold(d["nsfw_threshold"]):
                        lo, hi = NSFW_THRESHOLD_RANGE
                        return self._json(
                            {"error": f"nsfw_threshold 只能是 {lo}~{hi} 之间的数"}, 400)
                    patch["nsfw_threshold"] = d["nsfw_threshold"]
                if "nsfw_model" in d:
                    if not valid_nsfw_model(str(d["nsfw_model"])):
                        return self._json({"error": "nsfw_model 不是可用档位"}, 400)
                    patch["nsfw_model"] = str(d["nsfw_model"])
                if "nsfw_enabled" in d:
                    patch["nsfw_enabled"] = bool(d["nsfw_enabled"])
                if "download_dir" in d:
                    # 空串 = 恢复「浏览器默认下载」；非空必须是绝对路径
                    # （否则后端写盘时才对不上，反馈太晚）
                    dd = str(d["download_dir"] or "").strip().strip('"')
                    if dd and not Path(dd).is_absolute():
                        return self._json({"error": "下载目录要填绝对路径（如 D:\\下载）"}, 400)
                    patch["download_dir"] = dd
                # ── 第 2 趟：校验全过，这才开始落盘（写盘失败一律 500 + 如实原因）──
                def _prefs_write_fail(what, _ex=None):
                    _ws = data_write_state()
                    return self._json(
                        {"ok": False, "data_write_error": _ws.get("error") or "",
                         "error": "%s没能写入数据目录，设置未生效（%s）；"
                                  "起始目录只读、或数据目录所在盘掉线时会这样"
                                  % (what, _ws.get("error") or "写入失败")}, 500)
                if "delete_mode" in patch and not save_delete_mode(patch["delete_mode"]):
                    return _prefs_write_fail("「删除去向」")
                if "trash_keep_days" in patch and not save_trash_keep_days(patch["trash_keep_days"]):
                    return _prefs_write_fail("「回收站保留天数」")
                if "nsfw_threshold" in patch and not save_nsfw_threshold(patch["nsfw_threshold"]):
                    return _prefs_write_fail("「NSFW 阈值」")
                if "nsfw_model" in patch and not save_nsfw_model(patch["nsfw_model"]):
                    return _prefs_write_fail("「NSFW 档位」")
                if "nsfw_enabled" in patch and not save_nsfw_enabled(patch["nsfw_enabled"]):
                    return _prefs_write_fail("「NSFW 总开关」")
                if "download_dir" in patch:
                    with SETTINGS_LOCK:
                        s = load_settings()
                        s["download_dir"] = patch["download_dir"]
                        dd_ok = save_settings(s)
                    if not dd_ok:
                        return _prefs_write_fail("「下载目录」")
                return self._json({"ok": True, "delete_mode": load_delete_mode(),
                        "trash_keep_days": load_trash_keep_days(),
                                   "nsfw_enabled": nsfw_enabled(),
                                   "nsfw_threshold": load_nsfw_threshold(),
                                   "nsfw_model": load_nsfw_model(),
                                   "download_dir": load_settings().get("download_dir", "")})
            if u.path == "/api/reindex":           # 设置 · 数据页：丢弃索引缓存并重扫（不动原图）
                if not force_reindex():
                    return self._json({"error": "尚未设置根目录，无法重建索引"}, 400)
                return self._json({"ok": True})
            if u.path == "/api/rescan":            # F26：强制重扫当前目录（丢缓存，不丢索引表）
                r = force_rescan(qs.get("rec", ["0"])[0] == "1")
                return self._json(r, 200 if r.get("ok") else 400)
            if u.path == "/api/cache_clear":       # 设置 · 数据页：按目标清缓存（目前只有缩略图）
                body = self._post_body
                try:
                    d = json.loads(body.decode("utf-8")) if body else {}
                    if not isinstance(d, dict):
                        raise ValueError
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                what = str(d.get("what") or "thumbs").strip().lower()
                if what == "thumbs":
                    return self._json({"ok": True, "what": what, "removed": clear_thumb_cache()})
                if what == "solo":                 # 单图暂存副本（不动原图、不动库内文件）
                    return self._json({"ok": True, "what": what, "removed": clear_solo_cache()})
                return self._json({"error": "unknown target"}, 400)
            if u.path == "/api/open_data":         # 设置 · 数据页：在资源管理器里打开数据目录
                try:
                    DATA_DIR.mkdir(parents=True, exist_ok=True)
                except OSError as ex:
                    return self._json({"error": f"{type(ex).__name__}: {ex}"}, 500)
                err = launch_with_default_app(DATA_DIR)
                if err:
                    return self._json({"error": err}, 500)
                return self._json({"ok": True})
            if u.path == "/api/datadir":           # F28 便携模式：切换数据目录（须重启生效）
                body = self._post_body
                try:
                    d = json.loads(body.decode("utf-8")) if body else {}
                    if not isinstance(d, dict):
                        raise ValueError
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                r = datadir_switch(d.get("mode"), bool(d.get("migrate")))
                if r.get("error"):
                    return self._json(r, 400)
                return self._json(r)
            if u.path == "/api/meta_batch":
                body = self._post_body
                try:
                    paths = json.loads(body.decode("utf-8")).get("paths") or []
                    if not isinstance(paths, list):
                        raise ValueError
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                _wait_index()           # 索引就绪则整批命中内存；正在建则最多等几秒，避免逐张读文件
                return self._json(meta_batch(paths))
            if u.path == "/api/fav":
                body = self._post_body
                try:
                    d = json.loads(body.decode("utf-8"))
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                op = d.get("op")
                if op == "add_cat":                      # 预建分类（0 条也保留）
                    ok = add_preset_cat(str(d.get("cat") or ""))
                    return self._json({"ok": ok, "cats": fav_cats()})
                if op == "del_cat":                      # 仅允许删空分类
                    ok = del_preset_cat(str(d.get("cat") or ""))
                    return self._json({"ok": ok, "cats": fav_cats()})
                try:
                    target = str(d["path"])
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                if not target:
                    return self._json({"error": "bad request"}, 400)
                tp = Path(target).resolve()
                if not tp.is_file() or not inside_root(tp):
                    return self._json({"error": "not found or outside root"}, 404)
                _prepare_organization_target(str(tp), d)
                if op == "set_cat":
                    r = set_fav_cat(str(Path(target).resolve()), d.get("cat"))
                    if r is None:
                        return self._json({"error": "未收藏该文件"}, 404)
                    return self._json({"ok": True, "fav": True,
                                       "favcat": d.get("cat") or "默认"})
                # FAV-01：带 fav 字段 = **显式目标状态**（幂等 + 单调序，后点击者胜）。
                # 不带则退回 toggle 语义，兼容未升级的旧前端。
                tgt = str(Path(target).resolve())
                if isinstance(d.get("fav"), bool):
                    try:
                        sq = int(d.get("seq") or 0)
                    except (TypeError, ValueError):
                        sq = 0
                    fav = set_fav(tgt, d["fav"], d.get("cat") or "默认", sq)
                else:
                    fav = toggle_fav(tgt, d.get("cat") or "默认")
                favs = load_favs()
                cat = favs.get(tgt, {}).get("cat")
                # 返回**磁盘真值**而非入参：过期写入被丢弃时，前端据此校正界面
                return self._json({"ok": True, "fav": tgt in favs, "favcat": cat})
            if u.path == "/api/recent":             # 记录最近使用（前端在预览/下载/复制/打开时调用）
                body = self._post_body
                try:
                    d = json.loads(body.decode("utf-8"))
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                if d.get("op") == "clear":
                    return self._json(recent_clear())
                try:
                    target = str(Path(str(d["path"])).resolve())
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                if not Path(target).is_file() or not inside_root(Path(target)):
                    return self._json({"error": "not found or outside root"}, 404)
                return self._json(recent_add(target, str(d.get("action") or "preview")))
            if u.path == "/api/mark":               # 已标记：设置/取消（state 为空=取消）
                body = self._post_body
                try:
                    d = json.loads(body.decode("utf-8"))
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                try:
                    target = str(Path(str(d["path"])).resolve())
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                state = str(d.get("state") or "")
                # ε-14（§6.8）：取消标记（state 空）此前跳过校验 —— 可对任意路径
                # 清记录。统一要求 root 内，与设置分支同口径。
                if not Path(target).is_file() or not inside_root(Path(target)):
                    return self._json({"error": "not found or outside root"}, 404)
                _prepare_organization_target(target, d)
                return self._json(mark_set(target, state))
            if u.path == "/api/tags":
                body = self._post_body
                try:
                    d = json.loads(body.decode("utf-8"))
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                op = d.get("op")
                try:
                    target = str(Path(d["path"]).resolve())
                except Exception:
                    target = ""
                if op in ("add", "remove", "set"):
                    if not target:
                        return self._json({"error": "bad request"}, 400)
                    p = Path(target)
                    if not p.is_file() or not inside_root(p):
                        return self._json({"error": "not found or outside root"}, 404)
                    _prepare_organization_target(target, d)
                if op == "add":
                    tag = str(d.get("tag") or "")
                    if not _norm_tag(tag):
                        return self._json({"error": "无效的标签名"}, 400)
                    cur = tags_add(target, tag)
                    return self._json({"ok": True, "tags": cur, "tags_all": tag_stats()})
                if op == "add_many":
                    # P1-18：批量贴标签走单请求，避免 N×M 次串行往返与失真计数
                    paths = d.get("paths")
                    if not isinstance(paths, list) or not paths:
                        return self._json({"error": "bad request"}, 400)
                    tags = d.get("tags")
                    if not isinstance(tags, list) or not tags:
                        return self._json({"error": "bad request"}, 400)
                    targets = d.get("targets")
                    if targets is not None:
                        if not isinstance(targets, list) or len(targets) != len(paths) or any(
                                not isinstance(entry, dict) or entry.get("path") != paths[index]
                                for index, entry in enumerate(targets)):
                            return self._json({"error": "invalid batch identities"}, 400)
                    clean_p = []
                    for index, p in enumerate(paths):
                        try:
                            rp = str(Path(str(p)).resolve())
                        except Exception:
                            continue
                        fp = Path(rp)
                        if fp.is_file() and inside_root(fp):
                            _prepare_organization_target(rp, targets[index] if targets is not None else {})
                            clean_p.append(rp)
                    if not clean_p:
                        return self._json({"error": "not found or outside root"}, 404)
                    r = tags_add_many(clean_p, tags)
                    # `ok` 只作成功标志；计数走 r["pairs"]（见 tags_add_many
                    # 的 docstring）。覆盖 ok 不再吃掉任何数字。
                    r["ok"] = True
                    return self._json(r)
                if op == "remove":
                    tag = str(d.get("tag") or "")
                    cur = tags_remove(target, tag)
                    return self._json({"ok": True, "tags": cur, "tags_all": tag_stats()})
                if op == "set":
                    cur = tags_set(target, d.get("tags") or [])
                    return self._json({"ok": True, "tags": cur, "tags_all": tag_stats()})
                if op == "rename":
                    r = tag_rename(str(d.get("from") or ""), str(d.get("to") or ""))
                    if not r.get("ok"):
                        return self._json(r, 400)
                    r["tags_all"] = tag_stats()
                    return self._json(r)
                if op == "delete":
                    r = tag_delete(str(d.get("tag") or ""))
                    if not r.get("ok"):
                        return self._json(r, 400)
                    r["tags_all"] = tag_stats()
                    return self._json(r)
                return self._json({"error": "unknown op"}, 400)
            if u.path == "/api/send":
                body = self._post_body
                try:
                    target = str(Path(json.loads(body.decode("utf-8"))["path"]).resolve())
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                p = Path(target)
                if not p.is_file() or not inside_root(p):
                    return self._json({"error": "not found or outside root"}, 404)
                if p.suffix.lower() != ".png":
                    return self._json({"error": "仅支持 PNG（含内嵌 prompt 的格式）"}, 400)
                raw = png_meta(p).get("prompt")
                if not raw:
                    return self._json({"error": "该 PNG 没有内嵌 prompt，无法回传"}, 400)
                try:
                    prompt = json.loads(raw)
                except ValueError:
                    return self._json({"error": "内嵌 prompt 不是合法 JSON"}, 400)
                return self._json(send_to_comfy(prompt))
            if u.path == "/api/export":
                body = self._post_body
                try:
                    d = json.loads(body.decode("utf-8"))
                    target = str(Path(d["path"]).resolve())
                    tpl = d.get("template") or None
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                p = Path(target)
                if not p.is_file() or not inside_root(p):
                    return self._json({"error": "not found or outside root"}, 404)
                r = export_workflow(p, tpl)
                if r.get("ok"):
                    base = Path(p).stem
                    if r["source"] == "png":
                        name = base + "_内嵌"
                    else:
                        t = tpl if not tpl.endswith(".json") else tpl[:-5]
                        name = base + "_" + t
                    r["name"] = name + ".json"
                return self._json(r)
            if u.path == "/api/export_save":       # 导出工作流 JSON 到用户选定路径（原生另存为框）
                # 与 /api/export 的分工：/api/export 只把 JSON 回传前端（旧浏览器下载用），
                # 而桌面壳（Tauri/WebView2）没有下载事件接管 —— <a download> 点了不落盘，
                # 用户以为导出了其实磁盘上没有。这里由后端弹系统「另存为」框并写盘，
                # 桌面端 / web 行为一致，保存位置也可控。
                body = self._post_body
                try:
                    d = json.loads(body.decode("utf-8"))
                    target = str(Path(d["path"]).resolve())
                    tpl = d.get("template") or None
                    explicit = str(d.get("dest") or "").strip()   # 给了就直写（自动化测试用）
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                p = Path(target)
                if not p.is_file() or not inside_root(p):
                    return self._json({"error": "not found or outside root"}, 404)
                r = export_workflow(p, tpl)
                if not r.get("ok"):
                    resp = {"ok": False, "error": r.get("error", "导出失败")}
                    # 未内嵌 UI 工作流且没给模板 ⇒ 让前端弹模板选择器再重试
                    if not tpl and "模板" in str(r.get("error", "")):
                        resp["need_template"] = True
                    return self._json(resp)
                # 建议文件名（与 /api/export 同规则：内嵌加 _内嵌，模板合成加 _<模板名>）
                base = Path(p).stem
                if r["source"] == "png":
                    name = base + "_内嵌"
                else:
                    t = tpl if not tpl.endswith(".json") else tpl[:-5]
                    name = base + "_" + t
                name += ".json"
                if explicit:
                    dest = explicit
                else:
                    # 默认落在原图所在目录（导出到图片旁边，最好找）
                    dest = _ask_save_path(name, str(p.parent))
                    if dest is None:                  # 弹不出框（非 Windows / 调用失败）⇒ 自动落盘
                        dest = str(_unique_path(p.parent / name))
                    elif not dest:                    # 用户取消：不写、不报错
                        return self._json({"ok": False, "canceled": True})
                dp = Path(dest)
                try:
                    # 先写 .part 再原子换名：中途失败不留半截文件（与 save_download 同款）
                    tmp = dp.with_name(f"{dp.name}.{_tmp_tag()}.part")
                    tmp.write_text(r["json"], encoding="utf-8")
                    os.replace(tmp, dp)
                except OSError as ex:
                    return self._json({"ok": False, "error": f"写入失败：{ex}"}, 500)
                return self._json({"ok": True, "path": str(dp), "name": dp.name})
            if u.path == "/api/open_in_comfy":
                body = self._post_body
                try:
                    d = json.loads(body.decode("utf-8"))
                    target = str(Path(d["path"]).resolve())
                    tpl = d.get("template") or None
                    mode = str(d.get("mode") or "window")
                    port = str(d.get("port") or "")
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                p = Path(target)
                if not p.is_file() or not inside_root(p):
                    return self._json({"error": "not found or outside root"}, 404)
                if mode == "pull":       # 送给你已经开着的那个 ComfyUI 页面（无需新窗口）
                    return self._json(bridge_push(p, tpl, port))
                r = export_workflow(p, tpl)
                if not r.get("ok"):
                    return self._json(r)
                return self._json(open_comfy_page(json.loads(r["json"])))
            if u.path == "/api/bridge/install":     # 把桥接脚本装进 ComfyUI custom_nodes
                return self._json(bridge_install())
            if u.path == "/api/root/migrate":       # 换根迁移：设置页手动兜底（同步执行）
                # 自动迁移靠指纹命中率判断「是不是搬家」，目录结构变过的场景它不敢动；
                # 这里用户显式指定旧根 —— 人说迁就迁，跳过门槛，逐条验证照做。
                body = self._post_body
                try:
                    d = json.loads(body.decode("utf-8"))
                    old_root = str((d or {}).get("old_root") or "").strip()
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                # 目录搬家后旧路径本来就可能不存在；迁移只需要其路径前缀。
                if not old_root or not Path(old_root).is_absolute():
                    return self._json({"error": "请填写旧目录的绝对路径"}, 400)
                root = STATE.get("root")
                if not root:
                    return self._json({"error": "尚未设置根目录"}, 400)
                expected_root = str((d or {}).get("expected_root") or "").strip()
                if expected_root and os.path.normcase(os.path.normpath(expected_root)) != os.path.normcase(os.path.normpath(root)):
                    return self._json({"error": "图库根目录已切换，请刷新并重新确认恢复目标"}, 409)
                # P3（V2.12.1）：新旧同路径是请求本身的问题，路由层直接 400，
                # 不必进 _migrate_run 兜一圈再拿 500 表达（审计探针 S4 记录在案）。
                if os.path.normcase(os.path.normpath(old_root)) == \
                        os.path.normcase(os.path.normpath(root)):
                    return self._json({"error": "新旧路径相同，无需迁移"}, 400)
                # P29（V2.12.1）：手动迁移与自动迁移共用 running 守卫 —— 两路并发
                # 改同一批表，后写赢前写，谁落盘的都可能是半成品。
                with _MIGRATE_LOCK:
                    if _MIGRATE_STATE["running"]:
                        return self._json({"error": "迁移正在进行中，请稍后再试"}, 409)
                    _MIGRATE_STATE["running"] = True
                try:
                    r = _migrate_run(root, old_root=old_root, manual=True)
                finally:
                    with _MIGRATE_LOCK:
                        _MIGRATE_STATE["running"] = False
                if r.get("ok") and r.get("migrated"):
                    log(f"[migrate] 手动迁移完成: {r}")
                    # P13 同款：设置页同步等到了结果，列表也得立刻见到分数
                    with NSFW_LOCK:
                        _building = bool(NSFW["building"])
                    if not _building:
                        nsfw_prime(root)
                return self._json(r, 200 if r.get("ok") else 500)
            if u.path == "/api/nsfw/scan":          # 内容安全：开始检测 / 重新检测
                # force=1 → 先丢派生缓存重扫（人工纠正表不动）；缺省做增量，秒级完成。
                # 参数走查询串而不是 body：这个按钮不该因为一个 Content-Type 就被 403 掉。
                r = nsfw_start(force=qs.get("force", ["0"])[0] == "1")
                return self._json(r, 200 if r.get("ok") else 400)
            if u.path == "/api/nsfw/label":         # 内容安全：人工纠正单张判定
                # 模型误判是必然的（插画/画风/构图都能骗过它），所以人工覆盖不是
                # 可选装饰，而是这个功能能不能长期用下去的关键 —— 纠正过就是纠正过。
                body = self._post_body
                try:
                    d = json.loads(body.decode("utf-8"))
                    if not isinstance(d, dict):
                        raise ValueError
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                if d.get("clear"):                  # 清空全部人工纠正（设置页）
                    return self._json(nsfw_manual_clear())
                try:
                    p = str(Path(d["path"]).resolve())
                    lab = d.get("label")
                    if lab is not None:
                        lab = str(lab).strip().lower()
                        if lab == "":
                            lab = None
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                if not p:
                    return self._json({"error": "bad request"}, 400)
                # P0-3（CMV3-LOCAL-FIX，2026-10-08 全代码审计，从旧工程回植）：本端点
                # 原先是域内**唯一**不做路径校验的路径型写端点 —— 任意绝对路径都能被
                # 写进「人工纠正表」（键污染），与 recycle_bin_delete / 各 strip 端点相比
                # 明显缺一道闸。纠正表的键本就必须是根内的一个真实文件，多校验不误伤：
                # 越界 / 目录 / 不存在的路径一律 400，绝不落盘。
                if not inside_root(Path(p)):
                    return self._json({"error": "路径在根目录之外，拒绝写入纠正表"}, 400)
                if not os.path.isfile(p):
                    return self._json({"error": "文件不存在，拒绝写入纠正表"}, 400)
                r = set_nsfw_manual(p, lab, d)
                return self._json(r, 200 if r.get("ok") else 400)
            return self._json({"error": "unknown endpoint"}, 404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # ε-14（§6.8）：补 ConnectionAbortedError —— 客户端（下载器/杀软中间人）
            # 强杀连接时 Windows 抛的是它而不是 ConnectionReset，漏捕会刷 ERROR 日志。
            pass
        except Exception as ex:
            # §6.1：响应里带了 error，但**服务端一点痕迹都没有** —— 排障只能靠猜。
            if _SQL_ORG is not None:
                from backend.storage.legacy_adapter import IdentityConflictError
                if isinstance(ex, IdentityConflictError):
                    return self._json({"error": str(ex), "code": "identity_conflict"}, 409)
            # traceback 进日志（P1-39 已建落盘日志基建）；路径**剥掉 query**，
            # 免得 LAN 模式的 ?token=xxx 被写进日志。do_GET / do_POST 两处兜底同款。
            log("[error] %s 处理失败: %s: %s\n%s"
                % ((self.path or "").split("?")[0], type(ex).__name__, ex,
                   traceback.format_exc()))
            try:
                self._json({"error": f"{type(ex).__name__}: {ex}"}, 500)
            except Exception:
                pass


class QuietServer(ThreadingHTTPServer):
    """静默处理客户端中断（浏览器刷新/取消加载属正常现象），不打印堆栈。"""
    def handle_error(self, request, client_address):
        pass


# ---------------------------------------------------------------- 设置面板支撑（V1 批次 2）


# 批次 F · F5（CMV3-LOCAL-FIX，从旧工程回植）：目录占用 TTL 短缓存。
# `sysinfo()` 一次要算三个目录，其中缩略图目录实测 **12956 个文件**（单目录就 120 ms），
# 而设置面板每打开一次都全量遍历一次。界面显示的是「占用快照」，秒级滞后无感。
# **清除 / GC 之后必须主动失效**（`_dir_usage_invalidate`），否则「点完数字不变」
# 会被用户当成「没清成功」—— 这是本缓存唯一需要人工布线的地方。
_DIR_USAGE = {}
_DIR_USAGE_LOCK = threading.Lock()
DIR_USAGE_TTL = 30.0


def _dir_usage_invalidate(d: Path = None):
    """失效某个目录（不传则全部）的占用缓存。删 / 清缓存后必须调。"""
    with _DIR_USAGE_LOCK:
        if d is None:
            _DIR_USAGE.clear()
        else:
            _DIR_USAGE.pop(str(d), None)


def _dir_usage(d: Path) -> dict:
    """目录占用：文件数与总字节。这几个目录都是平铺的，不递归子目录。

    批次 F（F5）：带 `DIR_USAGE_TTL` 短缓存，理由见上方注释。
    """
    k = str(d)
    now = time.time()
    with _DIR_USAGE_LOCK:
        hit = _DIR_USAGE.get(k)
        if hit is not None and now - hit[0] < DIR_USAGE_TTL:
            return dict(hit[1])
    n, b = 0, 0
    try:
        if d.is_dir():
            for f in d.iterdir():
                try:
                    if f.is_file():
                        n += 1
                        b += f.stat().st_size
                except OSError:
                    continue
    except OSError:
        pass
    res = {"count": n, "bytes": b}
    with _DIR_USAGE_LOCK:
        _DIR_USAGE[k] = (now, res)
    return dict(res)


# 迁移清单：CMV 自己的数据。edge-profile 刻意不在列 —— 那是浏览器配置（可再生、体积大），
# 搬到新位置只会让「切换」变得又慢又占地方。
DATA_FILES = ("settings.json", "favorites.json", "tags.json",
              "recent.json", "marks.json", "prompt_index.json",
              "favorites.seq.json", "nsfw_manual.json")
DATA_SUBDIRS = ("thumbs", "trash")


def datadir_for(mode: str) -> Path:
    """某个模式对应的标准落点（不代表当前已生效的那个）。

    ε-13（§6.6）：便携模式走 `_portable_flag_dir()` —— 与 `_resolve_data_dir()`
    同一套标记 JSON 解析，口径不再分叉（详见那边注释）。
    """
    if mode == "portable":
        if PORTABLE_FLAG.is_file():
            return _portable_flag_dir() or (_exe_dir() / PORTABLE_DATA_NAME)
        return _exe_dir() / PORTABLE_DATA_NAME
    return _user_data_dir()


def _migrate_data(src: Path, dst: Path) -> dict:
    """把源数据目录里 CMV 的条目复制到目标。逐条捕异常。

    ε-11（§6.4）：同名文件分两种情形，不再一律静默跳过 ——
      · 大小 + mtime 都一致 ⇒ 视为同一份，跳过（来回切换幂等、不产生垃圾文件）；
      · 内容不同 ⇒ 目标端旧文件先改名备份为 `<名>.cmv-conflict-<ts>` 再写入源端这份。
        「不覆盖用户数据」的底线不变（P0-2）：旧数据改名保留、可见可恢复，
        而不是像以前那样原地跳过 —— 来回切换后新目录里跑的还是旧数据，观感等同丢数据。
    逐条捕异常的理由同批量删除 —— 一个文件坏掉不该让整次迁移失败。

    ⚠️ P0-2：失败**必须上报**。「逐条捕异常」+「返回值里没有失败数」这个组合踩过一个坑：
      exFAT/满盘/被占用导致 copy2 失败时，后端只 log 一句（无控制台 exe 下这句直接丢弃），
      返回值只有 {files, skipped} ⇒ 前端看到 files>0 就弹「已切换…复制 N 个文件」。
      重启后发现 favorites.json / settings.json 没过去，表现就是「数据全丢了」，
      而真身还躺在旧目录 —— 用户不会回去找。所以这里显式累计 failed 与失败清单。
    """
    n_f = n_skip = n_b = n_conf = 0
    failed = []
    _ts = str(int(time.time()))

    def _one(s: Path, d: Path):
        nonlocal n_f, n_skip, n_b, n_conf
        if d.exists():
            try:
                ss, ds = s.stat(), d.stat()
                if ds.st_size == ss.st_size:
                    # ε-11：判「同一份」小文件（各 JSON，≤1MB）直接比内容 ——
                    # 同名不同内容常是同秒写入，size+mtime 会误判成相同而静默跳过；
                    # 持久数据及回收站材料逐块比较，不能用同大小/时间冒充同内容；
                    # 仅可再生的大缩略图缓存沿用mtime口径。
                    if ss.st_size <= (1 << 20) or s.parent.name != 'thumbs':
                        with open(str(s), "rb") as _a, open(str(d), "rb") as _b:
                            same = True
                            while True:
                                chunk = _a.read(1024 * 1024)
                                if chunk != _b.read(1024 * 1024):
                                    same = False
                                    break
                                if not chunk:
                                    break
                    else:
                        same = int(ds.st_mtime) == int(ss.st_mtime)
                    if same:
                        n_skip += 1                    # 同一份内容：跳过（幂等）
                        return
            except OSError:
                pass
            bak = d.with_name(d.name + ".cmv-conflict-" + _ts)
            try:
                d.rename(bak)                      # 备份目标端旧文件，绝不覆盖
                n_conf += 1
            except OSError as ex:
                failed.append({"name": d.name,
                               "error": f"同名旧文件备份失败 {type(ex).__name__}: {ex}"})
                log(f"[warn] 迁移备份 {d} 失败: {ex}")
                return
        try:
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(s), str(d))
            n_f += 1
            n_b += d.stat().st_size
        except OSError as ex:
            failed.append({"name": d.name,
                           "error": f"{type(ex).__name__}: {ex}"})
            log(f"[warn] 迁移 {s} 失败: {ex}")

    for name in DATA_FILES:
        s = src / name
        if s.is_file():
            _one(s, dst / name)
    for name in DATA_SUBDIRS:
        s = src / name
        if not s.is_dir():
            continue
        try:
            for f in s.iterdir():
                if f.is_file():
                    _one(f, dst / name / f.name)
        except OSError as ex:
            failed.append({"name": name, "error": f"{type(ex).__name__}: {ex}"})
            log(f"[warn] 遍历 {s} 失败: {ex}")
    return {"files": n_f, "skipped": n_skip, "bytes": n_b,
            "conflicts": n_conf,
            "failed": len(failed), "failed_items": failed[:20],
            "dirs": len([x for x in DATA_SUBDIRS if (dst / x).is_dir()])}


def datadir_switch(mode: str, migrate: bool) -> dict:
    """切换数据目录并落盘标记（F28）。返回给前端的摘要。

    只写标记、不改内存里的 DATA_DIR —— 那 8 个常量在启动时算好，运行中改不了，
    所以如实返回 restart_required，由前端引导重启。宁可明说，也不要让用户
    「切了但当前会话还是老目录」却毫无提示。
    """
    mode = str(mode or "").strip().lower()
    if mode not in ("user", "portable"):
        return {"error": "mode 只能是 user 或 portable"}
    src, dst = DATA_DIR, datadir_for(mode)
    try:
        same = dst.resolve() == src.resolve()
    except OSError:
        same = str(dst) == str(src)
    if same:
        return {"ok": True, "same": True, "mode": mode, "dir": str(dst),
                "restart_required": False,
                "migrated": {"files": 0, "skipped": 0, "bytes": 0,
                             "failed": 0, "failed_items": [], "dirs": 0}}
    # 可写性先探一下：便携模式落在程序目录，装在 Program Files 下会失败，
    # 这时候要给出可读的原因，而不是留一个半途而废的状态。
    try:
        dst.mkdir(parents=True, exist_ok=True)
        probe = dst / ".cmv-write-test"
        probe.write_text("x", "utf-8")
        probe.unlink()
    except OSError as ex:
        return {"error": f"目标目录不可写：{dst}（{type(ex).__name__}: {ex}）"}

    if migrate:
        # ε-12（§6.3）：迁移挪后台线程 —— 立即返回 migrating，前端轮询
        # /api/datadir_status。便携标记必须等迁移**完成后**才写（在
        # _datadir_finalize 里），否则用户中途重启会拿到一份没搬完的新目录。
        with _DD_MIG_LOCK:
            if _DD_MIG["state"] == "running":
                return {"error": "已有一次数据目录迁移在进行中，请等它完成", "busy": True}
            _DD_MIG["state"] = "running"
            _DD_MIG["result"] = None
            _DD_MIG["mode"] = mode

        def _job():
            try:
                resp = _datadir_finalize(mode, src, dst, _migrate_data(src, dst))
            except Exception as ex:          # 迁移线程不许静默死：异常也如实落槽
                import traceback
                log("[datadir] 迁移线程异常: " + traceback.format_exc(limit=3))
                resp = {"error": f"迁移线程异常：{type(ex).__name__}: {ex}"}
            with _DD_MIG_LOCK:
                _DD_MIG["state"] = "error" if resp.get("error") else "done"
                _DD_MIG["result"] = resp

        threading.Thread(target=_job, name="datadir-migrate", daemon=True).start()
        return {"migrating": True, "mode": mode, "dir": str(dst), "from": str(src),
                "restart_required": True}
    return _datadir_finalize(mode, src, dst,
                             {"files": 0, "skipped": 0, "bytes": 0,
                              "failed": 0, "failed_items": [], "dirs": 0})


def _datadir_finalize(mode: str, src: Path, dst: Path, copied: dict) -> dict:
    """ε-12（§6.3）：迁移完成后的收尾 —— 写/删便携标记并构造响应。

    从 datadir_switch 拆出来：同步路径（不迁移）与后台迁移线程共用。
    """
    if mode == "portable":
        try:
            tmp = PORTABLE_FLAG.with_suffix(".tmp")
            tmp.write_text(json.dumps({"dir": str(dst)}, ensure_ascii=False, indent=1), "utf-8")
            tmp.replace(PORTABLE_FLAG)
        except OSError as ex:
            return {"error": f"便携标记写入失败：{PORTABLE_FLAG}（{type(ex).__name__}: {ex}）",
                    "migrated": copied}
    else:
        try:
            if PORTABLE_FLAG.exists():
                PORTABLE_FLAG.unlink()
        except OSError as ex:
            return {"error": f"便携标记删除失败：{PORTABLE_FLAG}（{type(ex).__name__}: {ex}）",
                    "migrated": copied}
    log(f"[datadir] 切换为 {mode}：{src} -> {dst}（迁移 {copied['files']} 个文件"
        f"，跳过 {copied['skipped']} 个已存在，失败 {copied.get('failed', 0)} 个）")
    # P0-2：有文件没搬过去时，别让前端报「一切正常」—— 带上 partial 让 UI 改用警告色
    if copied.get("failed"):
        names = "、".join(x["name"] for x in copied.get("failed_items", [])[:5])
        return {"ok": True, "mode": mode, "dir": str(dst), "from": str(src),
                "migrated": copied, "restart_required": True, "partial": True,
                "warning": (f"有 {copied['failed']} 个文件未能复制到新目录"
                            f"（{names}）。这些数据仍留在原目录：{src}")}
    return {"ok": True, "mode": mode, "dir": str(dst), "from": str(src),
            "migrated": copied, "restart_required": True}


_DD_MIG = {"state": "idle", "result": None, "mode": None}


_DD_MIG_LOCK = threading.Lock()


def datadir_status() -> dict:
    with _DD_MIG_LOCK:
        return {"state": _DD_MIG["state"], "mode": _DD_MIG["mode"],
                "result": _DD_MIG["result"]}


def sysinfo() -> dict:
    """设置面板「数据 / 关于」页的只读快照。

    刻意做成一个端点、而不是让前端拼：占用统计要 stat 磁盘，交给后端一次算完；
    顺带把「数据目录」这种只有后端知道的绝对路径一并带出去（前端没法自己算
    %LOCALAPPDATA%，桌面壳与浏览器模式下还可能不同）。
    """
    with IDX_LOCK:
        idx_n = len(IDX["map"])
        idx_root = IDX["root"]
        idx_built = IDX["built_at"]
        idx_done = int(IDX["done"])
        idx_total = int(IDX["total"])
        idx_building = bool(IDX["building"])
    # F26：目录指纹短缓存现状（扫描结果缓存 F25 尚未实现，如实报「未启用」，
    # 面板宁可显示真实状态，也不要写死一句「已启用」骗用户）
    with _SIG_LOCK:
        _sig_key, _sig_at = _SIG["key"], float(_SIG["at"])
    idx_bytes = 0
    try:
        if IDX_FILE.is_file():
            idx_bytes = IDX_FILE.stat().st_size
    except OSError:
        pass
    try:
        from backend.migration.relocation_job import latest_relocation_job
        relocation = {"job": latest_relocation_job(PORTABLE_FLAG,DATA_DIR)}
    except (OSError,ValueError) as error:
        relocation = {"job": None, "statusError": str(error)}
    return {
        "ok": True,
        "version": APP_VERSION,
        "has_pil": HAS_PIL,
        "data_dir": str(DATA_DIR),
        "settings_file": str(SETTINGS_FILE),
        # F28 数据目录位置：mode 是**当前进程实际在用**的；pending=True 表示磁盘上的
        # 选择已经变了、但这个进程还是老目录（切换后未重启），前端据此提示「需重启生效」。
        "datadir": {
            "dir": str(DATA_DIR),
            "relocation": relocation,
            "storage_mode": "sqlite" if _SQL_ORG is not None else "legacy",
            "switch_available": _SQL_ORG is None,
            "root_recovery_available": _SQL_ORG is None,
            "switch_reason": ("数据库模式搬家需要关闭程序后执行；自动搬家入口正在接入。"
                              if _SQL_ORG is not None else ""),
            "mode": DATA_MODE,
            "exe_dir": str(_exe_dir()),
            "flag": str(PORTABLE_FLAG),
            "flag_exists": PORTABLE_FLAG.is_file(),
            "pending": (("portable" if PORTABLE_FLAG.is_file() else "user") != DATA_MODE),
            "targets": {"user": str(_user_data_dir()),
                        "portable": str(_exe_dir() / PORTABLE_DATA_NAME)},
        },
        "thumbs": dict(_dir_usage(THUMB_DIR), dir=str(THUMB_DIR)),
        # B1-3（CMV3-LOCAL-FIX）：数据目录写入状态（与 /api/info 同源）。设置页「数据」页
        # 每次刷新都会重新读一次，用户在那里能直接看到「写不进去」的红字告警。
        "data_write": data_write_state(),
        # P1-3：回收站自动清理是可配的 —— 界面必须能回答「它会不会自己删」
        "trash": dict(_dir_usage(TRASH_DIR), dir=str(TRASH_DIR),
                      keep_days=load_trash_keep_days()),
        # 单图暂存：打开过的外部图片副本（哈希命名、上限 20 张，V2.10.7）
        "solo": dict(_dir_usage(SOLO_DIR), dir=str(SOLO_DIR)),
        "index": {"count": idx_n, "bytes": idx_bytes, "file": str(IDX_FILE),
                  "root": idx_root, "built_at": int(idx_built or 0),
                  # F26 索引进度：done/total 由 _index_builder 逐个文件更新，building 表在跑
                  "progress": {"done": idx_done, "total": idx_total,
                               "building": idx_building,
                               "pct": (int(idx_done * 100 / idx_total)
                                       if idx_total else 0)}},
        # F26 扫描缓存：result_cache=False 是**事实**而非占位 —— F25 落地后改 True
        "scan": {"result_cache": False,
                 "sig_ttl": SIG_TTL,
                 "sig_cached": _sig_key is not None,
                 "sig_path": (_sig_key[0] if _sig_key else ""),
                 "sig_recursive": (bool(_sig_key[1]) if _sig_key else False),
                 "sig_age": (int(time.time() - _sig_at) if _sig_at else -1)},
    }


def clear_thumb_cache() -> int:
    """清空缩略图缓存（只删缓存目录里的文件，不动任何原图）。返回删除个数。"""
    n = 0
    try:
        if THUMB_DIR.is_dir():
            for f in THUMB_DIR.iterdir():
                try:
                    if f.is_file():
                        f.unlink()
                        n += 1
                except OSError:
                    continue
    except OSError:
        pass
    _dir_usage_invalidate(THUMB_DIR)     # F5（CMV3-LOCAL-FIX）：清完不失效 ⇒ 设置页数字不变，像没清成功
    return n


def force_reindex() -> bool:
    """丢弃内存索引与磁盘缓存，重新触发一次完整扫描（不动原图）。

    「重建」就是要真的从头来：只重置 built_at 的话 _index_builder 仍会按 mtime+size
    复用旧条目、磁盘缓存也还在，用户点了会疑惑「怎么一秒就完事」。所以这里连同
    IDX_FILE 一起丢掉。代价是重建期间提示词搜索为空 —— 前端已配确认弹窗与
    底栏进度（V3 状态中枢），不是静默行为。
    """
    root = STATE.get("root")
    if not root:
        return False
    with IDX_LOCK:
        IDX_GEN[0] += 1               # P1-15：先换代 —— 在跑的旧线程自此不得再写回 IDX
        IDX["map"] = {}
        IDX["done"] = 0
        IDX["total"] = 0
        IDX["built_at"] = 0.0
        IDX_THREAD[0] = None          # 允许 ensure_index 起新线程；
                                      # 旧线程仍在跑，但代际已被取代 ⇒ 收工时自行作废（见 _index_builder）
    try:
        if IDX_FILE.is_file():
            IDX_FILE.unlink()
    except OSError:
        pass
    ensure_index()
    return True


def force_rescan(recursive: bool = False) -> dict:
    """强制重扫当前目录：给「目录明明变了、界面却没动」一个立刻生效的出口。

    要一并丢掉两处缓存，缺一个都会表现成「点了没反应」：
      ① 目录指纹短缓存 `_SIG` —— 否则 SIG_TTL(1.5s) 内拿到的还是旧指纹，
         前端自动刷新比对后认为「没变化」，把这次点击整个吞掉；
      ② 索引增量节流位 `IDX["built_at"]` —— 否则 ensure_index 在 REINDEX_INTERVAL(30s)
         窗口内直接 return，刚出的图仍然搜不到（这正是 F25 缓存最容易踩的坑，先备好阀）。
    第三处在前端：`lastSig` 得由调用方重载目录时覆盖，服务端管不着。

    刻意**不动** `IDX["map"]` 与 `IDX_FILE` —— 那是「重建索引」的职责，两者代价差一个
    数量级：重扫只重新走一趟目录算个新指纹，重建要把每张图重新解析一遍。
    """
    root = STATE.get("root")
    if not root:
        return {"ok": False, "error": "尚未设置根目录，无法重扫"}
    with _SIG_LOCK:
        _SIG.update({"key": None, "at": 0.0, "sig": ""})
    with IDX_LOCK:
        IDX["built_at"] = 0.0                 # 下一趟 ensure_index 立即做增量校验
    p = Path(root)
    sig = dir_signature(p, recursive)
    n, exts = 0, (IMAGE_EXTS | VIDEO_EXTS)
    try:
        with os.scandir(p) as it:             # 只数当层媒体文件，递归计数由指纹负责
            for e in it:
                try:
                    if e.is_file() and os.path.splitext(e.name)[1].lower() in exts:
                        n += 1
                except OSError:
                    continue
    except OSError:
        pass
    return {"ok": True, "root": root, "sig": sig, "files": n,
            "recursive": bool(recursive)}


def gc_thumbs(max_mb: int = 512, keep_days: int = 30):
    """缩略图缓存 GC：清残留 .tmp、删超龄条目，总量超限按最旧优先裁剪。"""
    try:
        if not THUMB_DIR.is_dir():
            return
        for f in THUMB_DIR.glob("*.tmp"):
            try:
                f.unlink()
            except OSError:
                pass
        infos, total = [], 0
        for f in THUMB_DIR.glob("*.jpg"):
            try:
                st = f.stat()
            except OSError:
                continue
            infos.append((st.st_mtime, st.st_size, f))
            total += st.st_size
        cutoff = time.time() - keep_days * 86400
        for mt, sz, f in infos:
            if mt < cutoff:
                try:
                    f.unlink()
                    total -= sz
                except OSError:
                    pass
        limit = max_mb * 1024 * 1024
        if total > limit:
            for mt, sz, f in sorted(infos):
                if total <= limit:
                    break
                try:
                    f.unlink()
                    total -= sz
                except OSError:
                    pass
    except OSError:
        pass
    finally:
        _dir_usage_invalidate(THUMB_DIR)     # F5（CMV3-LOCAL-FIX）：GC 可能删了东西，占用数字要重算


# ── 落盘日志（P1-39）────────────────────────────────────────────────────
# 打包成无控制台 exe 时 sys.stdout is None，log() 完全静默 ⇒ 「用户看到白屏，
# 磁盘上零痕迹」，排障只能靠猜。这里同时写一份到 LOG_FILE，并装 excepthook，
# 让未捕获异常也留痕。日志失败**绝不能**影响主流程（所有写入都吞异常）。
LOG_LOCK = threading.Lock()
LOG_FILE = None              # 由 init_logging() 定，未初始化时只 print


def log_path() -> Path:
    """日志落点：exe 同目录的 cmv-data\\logs\\cmv.log（与数据目录同处，便携）。"""
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    return base / "cmv-data" / "logs" / "cmv.log"


def log(msg: str):
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        if not getattr(sys.stdout, "_cmv_log_stream", False):
            print(line, flush=True)
    except Exception:  # 打包成无控制台 exe 时 stdout 可能为 None
        pass
    if LOG_FILE is None:
        return
    try:
        with LOG_LOCK:
            # 单文件上限 2MB：超了轮转成 .1（只保留一代，避免无限增长）
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > 2 * 1024 * 1024:
                bak = LOG_FILE.with_suffix(".log.1")
                try:
                    if bak.exists():
                        bak.unlink()
                    LOG_FILE.rename(bak)
                except Exception:
                    pass
            with io.open(LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass                 # 日志永远不能反过来弄崩主流程


def _excepthook(etype, value, tb):
    """未捕获异常落盘（P1-39）。默认钩子是往 stderr 打，无控制台时全丢。"""
    try:
        txt = "".join(traceback.format_exception(etype, value, tb))
    except Exception:
        txt = "%s: %s" % (etype, value)
    log("[FATAL] 未捕获异常：\n" + txt)
    try:
        sys.__excepthook__(etype, value, tb)
    except Exception:
        pass


def init_logging():
    """装日志落点 + excepthook + stdout/stderr 兜底。main() 最开始调用。"""
    global LOG_FILE
    try:
        p = log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        LOG_FILE = p
    except Exception:
        LOG_FILE = None
    try:
        sys.excepthook = _excepthook
    except Exception:
        pass
    # 无控制台 exe：把 stdout/stderr 接到日志，避免 print 静默失败
    if sys.stdout is None or sys.stderr is None:
        class _LogStream:
            _cmv_log_stream = True

            def write(self, s):
                s = (s or "").rstrip("\n")
                if s:
                    log(s)
            def flush(self):
                pass
        dummy = _LogStream()
        if sys.stdout is None:
            sys.stdout = dummy
        if sys.stderr is None:
            sys.stderr = dummy
    log("[%s] 日志已启用：%s" % (APP_TITLE, LOG_FILE))


_RestartState = {"ok": None, "err": ""}     # P1-34：最近一次重启的起进程结果


_TRAY_ICON = None                            # ε-13（§6.6）：托盘句柄全局登记，


_MEI_PREFIX = "_MEI"                        # PyInstaller onefile 解包目录名前缀


_MEI_MARKER = "mei_owner.txt"               # 随包分发的归属标记（见 ComfyMediaViewer.spec）


_MEI_MARKER_TEXT = "ComfyMediaViewer-onefile-bundle"    # ⚠️ 与 mei_owner.txt 内容、spec 三处同步


_PROC_START_TS = time.time()                # 本进程启动时刻（比它新的 _MEI 一概不动，防并发互删）


def _is_our_mei_dir(path) -> bool:
    """判断 path 是不是「本程序解包出来的」_MEI 目录 —— 靠随包分发的归属标记。

    只看目录名不够：同一台机器上任何别的 PyInstaller onefile 程序（甚至别的
    版本的 CMV）都会留下同前缀的目录，删了就是毁别人正在用的程序。
    """
    try:
        f = Path(path) / _MEI_MARKER
        return f.is_file() and f.read_text("utf-8", "replace").strip() == _MEI_MARKER_TEXT
    except Exception:
        return False


def _sweep_stale_meipass(verbose=True, temp_root=None, current=None, started=None):
    """清掉本程序遗留在 %TEMP% 里的旧解包目录，返回 (删除数, 跳过数)。

    五条判据**缺一不可**（任一条不过就不动它）：
      ① 名字以 `_MEI` 开头  ② 是目录
      ③ 目录内带本程序归属标记（免得误删别的 PyInstaller 程序）
      ④ 不是当前进程正在使用的那个（`sys._MEIPASS`）
      ⑤ 最后修改时间早于本次进程启动（防两个实例并发启动时互删）
    删除失败**一律静默跳过**：Windows 上正在被使用的 _MEI 其 dll 有独占锁，
    rmtree 必然抛 WinError 32 —— 这正好当天然保险，不必自己去查「谁在用」。

    只在打包运行（frozen）时动手：源码直启根本没有 _MEIPASS，也绝不允许去动
    用户的 %TEMP%（那只会误伤别的程序）。
    参数 temp_root / current / started 只为单元测试可注入；正常调用一律走默认。
    """
    if not getattr(sys, "frozen", False):
        return (0, 0)
    try:
        base = Path(temp_root) if temp_root else Path(tempfile.gettempdir())
        cur = Path(current) if current else Path(getattr(sys, "_MEIPASS", "") or "")
        t0 = _PROC_START_TS if started is None else started
        if not base.is_dir():
            return (0, 0)
        cur_key = os.path.normcase(os.path.realpath(str(cur))) if str(cur) else ""
        names = sorted(os.listdir(str(base)))
    except Exception as ex:
        if verbose:
            log(f"[mei-sweep] 扫描解包目录失败（忽略）：{ex}")     # 清理绝不能拖累启动
        return (0, 0)

    removed = kept = 0
    for name in names:
        if not name.startswith(_MEI_PREFIX):                    # ① 前缀
            continue
        p = base / name
        try:
            if not p.is_dir():                                  # ② 必须是目录
                continue
            key = os.path.normcase(os.path.realpath(str(p)))
            if cur_key and key == cur_key:                      # ④ 当前正在跑的那份
                continue
            if not _is_our_mei_dir(p):                          # ③ 归属标记
                continue
            if p.stat().st_mtime > t0:                          # ⑤ 比本次启动还新
                continue
            shutil.rmtree(str(p))
            removed += 1
        except Exception:
            kept += 1                                           # 被占用/权限不足 ⇒ 下次再来
            continue
    if verbose and (removed or kept):
        log(f"[mei-sweep] 清理旧解包目录：删除 {removed} 个，跳过 {kept} 个（被占用或非本程序）")
    return (removed, kept)


def _restart_env():
    """「重启程序」起新进程用的环境变量：**剔除 PyInstaller 的解包残留变量**。

    这是 `_MEI` 永久残留的成因 ②（实测 173 个）。PyInstaller onefile 的 bootloader
    靠环境变量向子进程传递「已经解包到哪里」：老版本用 `_MEIPASS2`，新版本用
    `_PYI_APPLICATION_HOME_DIR` / `_PYI_ARCHIVE_FILE` / `_PYI_PARENT_PROCESS_LEVEL`
    这一族。直接 `os.environ.copy()` 传给新进程的后果是：新 bootloader 一看这些变量
    还在，就认定「已经解包过了」，于是**既不解包到新目录、也不在退出时删目录** ——
    而旧目录本来该由旧 bootloader 删，旧 bootloader 又被 `os._exit(0)` 带走了
    ⇒ 那个目录从此没有任何人负责，永久留在 %TEMP%。

    剔掉之后，新进程就是一次**干净的解包**：它自己那棵树负责自己那份目录，
    生命周期闭合。（清理旧目录另有 _sweep_stale_meipass 兜底。）
    """
    env = os.environ.copy()
    for k in [k for k in env if k == "_MEIPASS2" or k.startswith("_PYI_")]:
        env.pop(k, None)
    return env


def restart_self(httpd=None, delay: float = 0.4, in_shell: bool = None):
    """原地重启：起一个新进程接管，然后退出自己（网页上的「重启程序」按钮用）。

    不用 os.execv——它在 Windows 上不给参数加引号，路径里带空格（本项目目录名就带）
    会把参数拆断，新进程直接起不来。改用 subprocess（会正确加引号）+ 先关监听
    socket，让新进程能绑回同一端口；--no-browser 强制追加，避免又弹一个标签页
    （用户当前页面本来就会自动重连）。

    P1-34：起新进程失败时**必须把监听还回来**并明确报错。旧实现先
    server_close() 再 Popen，Popen 抛 OSError 时只 log() + return ⇒ 端口没了、
    进程还在、用户无任何提示（「点了重启，界面就死了」）。现在失败则重新绑定
    同一端口继续服务，并把原因写进日志与 HTTP 响应。

    P1-35：桌面壳里**不能**用 DETACHED_PROCESS。壳退出时按登记的 sidecar pid
    `taskkill /T` 杀树，而脱离父进程的新后端 pid ≠ 壳登记的 pid ⇒ 壳一关，
    重启出来的后端成了**常驻孤儿**；下次启动还会被 _probe_own_instance 认成
    「已有实例」直接退出（「程序打不开了，但进程列表里有一个」）。
    检测到处于壳中（--app-dir 或环境变量）时改用普通子进程，保持父子关系。
    """
    if in_shell is None:
        in_shell = bool(_APP_DIR_OVERRIDE) or bool(os.environ.get("CMV_IN_SHELL"))

    def _go():
        time.sleep(delay)                       # 先让 HTTP 响应发出去
        if getattr(sys, "frozen", False):       # 打包成 exe：argv[0] 就是 exe 本身
            args = [sys.executable] + sys.argv[1:]
        else:
            args = [sys.executable, os.path.abspath(sys.argv[0])] + sys.argv[1:]
        if "--no-browser" not in args:
            args.append("--no-browser")
        log("[restart] 正在重启：" + " ".join(args))
        # ⚠️ 顺序很关键（P1-34）：**先起新进程，成功了才关监听**。
        #    旧实现反着来（先 server_close 再 Popen），Popen 一失败就两头空：
        #    端口已经没了、进程还活着、用户点了重启界面就死。
        #    另外 socketserver 的 server_close() 会**永久关掉套接字**，
        #    之后再 server_bind()/server_activate() 必然 WinError 10038
        #    （在非套接字上操作）—— 回滚根本救不回来。所以只能靠「延后关闭」
        #    来保证失败时监听毫发无伤。
        try:
            # P1-35：壳内不脱离（保持父子关系，壳才能按 pid 杀树）；
            #        独立运行才用 DETACHED_PROCESS 脱离父进程。
            flags = 0 if in_shell else (0x00000008 if _IS_WIN else 0)
            # V2.12.6：env 必须过一遍 _restart_env() —— 直接继承 os.environ 会把
            #         `_MEIPASS2` / `_PYI_*` 带给新进程，新 bootloader 于是认为
            #         「已解包」，既不解包也不删目录，旧 _MEI 从此永久残留（成因 ②）。
            subprocess.Popen(
                args, close_fds=True, cwd=os.getcwd(), creationflags=flags,
                env=_restart_env(),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
        except OSError as ex:
            # P1-34：新进程没起来 —— 监听**压根没关过**，直接继续服务
            log(f"[restart] 启动新进程失败：{ex}；监听未受影响，程序继续运行")
            _RestartState["ok"] = False
            _RestartState["err"] = str(ex)
            return
        # 新进程已就位，现在才释放端口（否则新进程只能顺延到下一个端口）
        try:
            if httpd is not None:
                httpd.server_close()
        except Exception:
            pass
        _RestartState["ok"] = True
        _RestartState["err"] = ""
        # ε-13（§6.6）：os._exit 会跳过 main 的 finally（那里才有 tray_icon.stop()），
        # 托盘图标残留在通知区直到鼠标扫过才消失 —— 「重启后残留旧图标」。
        # os._exit 不触发 atexit，所以必须在这里显式清掉。
        if _TRAY_ICON is not None:
            try:
                _TRAY_ICON.stop()
            except Exception:
                pass
        # 壳内：新后端继承父进程，壳退出时会连它一起杀；本进程正常退出即可
        os._exit(0)

    threading.Thread(target=_go, daemon=True).start()


# ── Windows：把「资源管理器 / 外部程序」窗口带到前台 ────────────────────
# 本程序以无控制台方式运行（后台进程），由它启动的 explorer / 图片查看器
# 受 Windows「前台锁」限制，默认只会在任务栏闪烁而不弹出。这里在启动后
# 轮询找到新出现的顶层窗口，再用「合成 Alt 键 + AttachThreadInput」解除
# 前台锁并激活它，从而让窗口真正弹到最前面。
_IS_WIN = (sys.platform == "win32")
if _IS_WIN:
    import ctypes
    import ctypes.wintypes as _wt

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)

    _user32.EnumWindows.argtypes = [_WNDENUMPROC, ctypes.c_long]
    _user32.EnumWindows.restype = ctypes.c_bool
    _user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    _user32.GetClassNameW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    _user32.IsWindowVisible.restype = ctypes.c_bool
    _user32.IsIconic.argtypes = [ctypes.c_void_p]
    _user32.IsIconic.restype = ctypes.c_bool
    _user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _user32.ShowWindow.restype = ctypes.c_bool
    _user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    _user32.SetForegroundWindow.restype = ctypes.c_bool
    _user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
    _user32.BringWindowToTop.restype = ctypes.c_bool
    _user32.GetForegroundWindow.argtypes = []
    _user32.GetForegroundWindow.restype = ctypes.c_void_p
    _user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    _user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
    _user32.AttachThreadInput.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_bool]
    _user32.AttachThreadInput.restype = ctypes.c_bool
    _user32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_ulong, ctypes.c_void_p]
    _user32.keybd_event.restype = None
    _kernel32.GetCurrentThreadId.argtypes = []
    _kernel32.GetCurrentThreadId.restype = ctypes.c_ulong
else:
    _user32 = None
    _kernel32 = None

_SKIP_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman",
                 "WorkerW", "Windows.UI.Core.CoreWindow", "EdgeUiInputTopWndClass",
                 "Shell_TrayWnd", "NotifyIconOverflowWindow", "tooltips_class32"}


def _snapshot_windows():
    """返回 {hwnd: (class_name, title)}，只含可见的顶层窗口。"""
    if not _IS_WIN:
        return {}
    out = {}

    @_WNDENUMPROC
    def _cb(hwnd, _lp):
        try:
            if not hwnd or not _user32.IsWindowVisible(hwnd):
                return True
            cbuf = ctypes.create_unicode_buffer(256)
            _user32.GetClassNameW(hwnd, cbuf, 256)
            tbuf = ctypes.create_unicode_buffer(512)
            _user32.GetWindowTextW(hwnd, tbuf, 512)
            out[int(hwnd)] = (cbuf.value, tbuf.value)
        except Exception:
            pass
        return True

    try:
        _user32.EnumWindows(_cb, 0)
    except Exception:
        pass
    return out


def _force_foreground(hwnd) -> bool:
    """强制把 hwnd 提到最前：先合成 Alt 解除前台锁，失败再挂接输入线程重试。"""
    if not _IS_WIN or not hwnd:
        return False
    try:
        _user32.ShowWindow(hwnd, 9 if _user32.IsIconic(hwnd) else 5)  # 9=SW_RESTORE 5=SW_SHOW
    except Exception:
        pass

    def _try_activate():
        try:
            _user32.keybd_event(0x12, 0, 0, None)            # Alt down
            try:
                _user32.SetForegroundWindow(hwnd)
                _user32.BringWindowToTop(hwnd)
            finally:
                _user32.keybd_event(0x12, 0, 0x0002, None)   # Alt up (KEYEVENTF_KEYUP)
        except Exception:
            return False
        time.sleep(0.03)
        try:
            fg = _user32.GetForegroundWindow()
            return bool(fg) and int(fg) == int(hwnd)
        except Exception:
            return False

    if _try_activate():
        return True
    try:                                                     # 兜底：挂接前台线程绕过前台锁
        fg = _user32.GetForegroundWindow()
        fg_tid = _user32.GetWindowThreadProcessId(fg, None) if fg else 0
        cur_tid = _kernel32.GetCurrentThreadId()
        attached = bool(fg_tid and fg_tid != cur_tid
                        and _user32.AttachThreadInput(cur_tid, fg_tid, True))
        try:
            return _try_activate()
        finally:
            if attached:
                _user32.AttachThreadInput(cur_tid, fg_tid, False)
    except Exception:
        return False


def _bring_new_window_to_front(before, cls=None, title_hint="", timeout=3.0):
    """在 before 快照之后新出现的窗口里挑一个置前。cls 为窗口类名过滤。"""
    if not _IS_WIN:
        return False
    target = 0
    deadline = time.time() + timeout
    while time.time() < deadline:
        for hwnd, (c, t) in _snapshot_windows().items():
            if hwnd in before or c in _SKIP_CLASSES:
                continue
            if cls and c != cls:
                continue
            if not cls:
                # ε-13（§6.6）：cls=None（系统默认程序打开）时此前只要求「有标题」——
                # 4 秒窗口期里弹出的**任何**有标题窗口（微信通知、别的程序）都会被
                # 抢到前台。现在必须有 title_hint（文件名）命中才认，宁可不置前
                # （顶多任务栏闪烁）也不抢无关窗口。
                if not title_hint or title_hint.lower() not in t.lower():
                    continue
            target = hwnd
            break
        if target:
            break
        time.sleep(0.15)
    if not target and cls and title_hint:
        # explorer 可能复用了已打开的窗口（不产生新 hwnd）：按标题（目录名）找
        for hwnd, (c, t) in _snapshot_windows().items():
            if c == cls and t.strip().startswith(title_hint):
                target = hwnd
                break
    if not target:
        log("（未定位到新窗口，目标可能仅在任务栏闪烁）")
        return False
    ok = _force_foreground(target)
    log(("（窗口已置前 hwnd=%s）" % target) if ok else "（窗口置前失败，仅在任务栏闪烁）")
    return ok


def reveal_in_explorer(path: Path) -> None:
    """资源管理器中选中文件，并把窗口带到前台。"""
    before = _snapshot_windows()
    subprocess.Popen(["explorer", "/select,", str(path)])
    threading.Thread(target=_bring_new_window_to_front,
                     args=(before, "CabinetWClass", path.parent.name or "", 3.0),
                     daemon=True).start()


def launch_with_default_app(path: Path) -> str:
    """用系统默认程序打开，并把新窗口带到前台。返回空串表示成功，否则为错误信息。"""
    before = _snapshot_windows()
    try:
        os.startfile(str(path))
    except (OSError, AttributeError) as ex:
        return f"{type(ex).__name__}: {ex}"
    threading.Thread(target=_bring_new_window_to_front,
                     args=(before, None, path.name, 4.0), daemon=True).start()
    # ε-13（§6.6）：cls=None 必须带 title_hint（文件名）—— 看图器的窗口标题
    # 普遍含文件名，凭它过滤才不会把 4 秒里弹出的无关窗口抢到前台。
    return ""


def _make_server(host: str, preferred: int, handler):
    """按端口段直接尝试 bind，成功即返回 —— 避免「先探测空闲、再绑定」之间被抢的竞态。"""
    last = None
    for p in [preferred] + [preferred + i for i in range(1, 11)] + [0]:
        try:
            return QuietServer((host, p), handler)
        except OSError as ex:
            last = ex
    raise last if last else OSError("no available port")


def _probe_own_instance(preferred: int, tries: int = 11):
    """探测 preferred 起的端口段上是否已有本程序实例在监听。

    判定依据：HTTP 响应的 Server 头以 MediaViewer/ 开头（404/403 也带）。
    命中返回端口号，否则返回 None。"""
    import socket
    if tries > 1:
        from concurrent.futures import ThreadPoolExecutor
        ports = list(range(preferred, min(preferred + min(tries, 11), 65536)))
        with ThreadPoolExecutor(max_workers=len(ports)) as pool:
            found = list(pool.map(lambda port: _probe_own_instance(port, 1), ports))
        return next((port for port in found if port is not None), None)
    for p in range(preferred, preferred + tries):
        s = None
        try:
            s = socket.create_connection(("127.0.0.1", p), timeout=0.4)
            s.settimeout(1.5)
            s.sendall(b"GET /api/info HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
            data = b""
            while len(data) < 4096:
                c = s.recv(2048)
                if not c:
                    break
                data += c
        except OSError:
            continue
        finally:
            try:
                if s:
                    s.close()
            except OSError:
                pass
        if re.search(r"(?im)^server:\s*MediaViewer/", data.decode("latin-1", "ignore")):
            return p
    return None


def _tray_image():
    """托盘图标：优先打包资源（sys._MEIPASS）里的 app.ico，其次脚本同目录。"""
    from PIL import Image
    cands = []
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        cands.append(Path(meipass) / "app.ico")
    cands.append(Path(__file__).resolve().parent / "app.ico")
    for p in cands:
        try:
            if p.is_file():
                return Image.open(p)
        except Exception:
            continue
    return None


def _start_tray(httpd, url):
    """托盘图标线程：左键/双击=打开浏览器，右键菜单=打开/数据目录/退出。

    pystray 未安装或图标缺失时静默降级（不影响服务本身）。返回 icon 或 None。"""
    try:
        import pystray
    except ImportError:
        log("（pystray 未安装，托盘图标不可用）")
        return None
    img = _tray_image()
    if img is None:
        log("（托盘图标资源缺失，托盘不可用）")
        return None

    state = {"icon": None}

    def _open(item=None):
        webbrowser.open(url)

    def _open_data(item=None):
        d = DATA_DIR                     # F28：便携模式下托盘也要打开程序旁那个目录
        try:
            d.mkdir(parents=True, exist_ok=True)
            launch_with_default_app(d)     # Windows 资源管理器
        except OSError:
            pass

    def _quit(item=None):
        ic = state["icon"]
        if ic:
            try:
                ic.stop()
            except Exception:
                pass
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    menu = pystray.Menu(
        pystray.MenuItem("打开浏览器", _open, default=True),   # default=左键/双击
        pystray.MenuItem("打开数据目录", _open_data),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", _quit),
    )
    icon = pystray.Icon("cmv", img, "ComfyMediaViewer", menu)
    state["icon"] = icon
    threading.Thread(target=icon.run, daemon=True).start()
    return icon


def main():
    ap = argparse.ArgumentParser(description="ComfyUI 媒体浏览器")
    ap.add_argument("--root", default=None,
                    help="浏览的根目录（缺省时自动探测 ComfyUI output）")
    ap.add_argument("--port", type=int, default=8765, help="端口，默认 8765，被占用时自动后移")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--no-remember", action="store_true",
                    help="不把本次根目录写入用户「常用目录」（自动化脚本用，避免污染用户配置）")
    ap.add_argument("--lan", action="store_true",
                    help="局域网模式：监听 0.0.0.0，令牌鉴权（控制台打印带令牌的访问链接）")
    ap.add_argument("--app-dir", default=None,
                    help="程序目录（便携模式的标记与数据落点挂在这上面）。桌面壳把后端 exe "
                         "释放到别处运行时会传它，以还原用户真正放程序的那个目录；"
                         "普通直启不用传。环境变量 CMV_EXE_DIR 优先级更高")
    # 内部参数，不对用户暴露：新版 exe 被旧版拉起来当「更新器」时用（见 updater.swap_exe）。
    ap.add_argument("--apply-update", nargs=2, default=None, help=argparse.SUPPRESS)
    ap.add_argument("--no-update-check", action="store_true",
                    help="本次启动不做静默更新检查（自动化脚本 / 离线环境用）")
    ap.add_argument("--no-hotfix", action="store_true",
                    help="忽略热更覆盖，强制使用源码 / 内置的前端（开发时确认自己的改动真的生效用）")
    ap.add_argument("--frontend-next", action="store_true",
                    help="源码开发预览新版前端；自动忽略热更覆盖，不改变正式入口")
    ap.add_argument("--migration-protect", action="store_true",
                    help="迁移保护：仅提供状态接口，暂停浏览、写入、清理及更新任务")
    args = ap.parse_args()
    # P1-39：最早启用日志 —— 之后的任何崩溃（含 argparse 之后的路径）都会留痕
    if not MIGRATION_PROTECT:
        init_logging()
    # CMV3-LOCAL-FIX(C3) 续：main() 阶段的迁移检查。
    #   为什么模块级那次不够：有些门禁（如 delmode_check）**在 import server 之后**
    #   才建素材目录并调 add_root()（settings.json 这时才有 roots）。模块级那次
    #   拿不到 roots 只能跳过；等 main() 时 roots 已在 settings.json 里，就能迁成。
    #   迁移成功后 **必须重启进程** —— `_initialize_catalog_storage()` 只跑一次，
    #   而 DATA_DIR 等常量在导入期就固化了；就地继续跑会拿到半 legacy 半 sqlite 的
    #   混合态（比不迁更危险）。
    if _AUTO_MIGRATE and not MIGRATION_PROTECT:
        try:
            if _auto_migrate_if_needed(DATA_DIR, [args.root or ""]):
                print("[CMV_AUTO_MIGRATE] main() 阶段完成迁移 ⇒ 重启进程以加载 SQLite 权威",
                      flush=True)
                sys.stdout.flush()
                os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as _am_err:                          # noqa: BLE001
            print("!! CMV_AUTO_MIGRATE main() 阶段失败：%s: %s"
                  % (type(_am_err).__name__, _am_err), flush=True)
            raise
    # A failed recursive delete can remove unlocked Python archives before it
    # reaches a locked DLL in another live instance. Ownership and timestamps
    # do not prove inactivity. Leave other extraction directories untouched;
    # the bootloader cleans its own directory on normal process exit.
    if args.apply_update and MIGRATION_PROTECT:
        ap.error("迁移保护期间不能执行更新")
    if args.apply_update:
        # 本进程的身份是「更新器」而非浏览器：等旧进程退出 → 把旧 exe 改名备份 →
        # 复制自己去目标路径 → 启动它 → 自己退出。必须早于单实例探测，否则会被
        # 认成「已有实例在运行」而直接退出，替换就永远不会发生。
        try:
            old_pid = int(args.apply_update[0])
        except ValueError:
            old_pid = 0
        log(f"[update] 以更新器身份启动，等待旧进程 {old_pid} 退出")
        UPDATER.swap_exe(sys.executable, args.apply_update[1], wait_pid=old_pid)
        return
    # B1-3（CMV3-LOCAL-FIX，从旧工程回植）：启动即做一次数据目录可写自检。
    # 只在 `/api/info` 里惰性探是探不到「启动期就写不进去」的 —— 界面要等到用户
    # 打开设置页才会看到红字，而用户通常根本不会去打开。
    try:
        probe_data_dir()
    except Exception:                    # noqa: BLE001
        pass                    # 自检本身绝不能拖累启动（函数内部已吞 OSError）
    # V2.12.6：清扫上一次运行遗留的 onefile 解包目录（%TEMP%\_MEIxxxx）。
    # 放在单实例检测**之前**：即便本进程随后因「已有实例」而退出，也顺手把这活干了。
    # 清理必然失败于「正在被别的实例使用的那份」（Windows 文件锁），不必自己判占用。
    try:
        _sweep_stale_meipass()
    except Exception:
        pass                    # 纯兜底：清理失败绝不能拖累启动（函数内部已吞，这里防意外）
    global REMEMBER_ROOTS, IGNORE_HOTFIX, USE_NEXT_FRONTEND
    REMEMBER_ROOTS = not args.no_remember and not MIGRATION_PROTECT
    IGNORE_HOTFIX = args.no_hotfix or args.frontend_next
    USE_NEXT_FRONTEND = args.frontend_next
    # --app-dir 在导入期已被 _preparse_app_dir() 读走（模块级常量要用），这里只是把
    # argparse 收到的值再确认一遍，防止有人用 add_argument 覆写或默认值漂移。
    global _APP_DIR_OVERRIDE
    if args.app_dir:
        _APP_DIR_OVERRIDE = args.app_dir

    # —— 单实例：目标端口段上已有本程序实例 → 唤起它的浏览器，本进程退出 ——
    if not args.lan and not MIGRATION_PROTECT:  # 保护模式不能合并到普通实例
        prior = _probe_own_instance(args.port)
        if prior:
            if not args.no_browser:
                try:
                    import urllib.request
                    _OPENER_NO_PROXY.open(
                        urllib.request.Request(
                            f"http://127.0.0.1:{prior}/api/activate", method="POST"),
                        timeout=3)
                except OSError:
                    pass
            log(f"[{APP_TITLE}] 已有实例在运行（端口 {prior}），已唤起其窗口，本进程退出。")
            return

    from backend.migration.guard import DataWriterGuard, WriterBusy
    import atexit
    try:
        guard = DataWriterGuard(DATA_DIR).acquire()
    except WriterBusy:
        log("数据目录正被另一个新版进程使用，请关闭该进程后再启动。")
        return
    atexit.register(guard.release)
    if not MIGRATION_PROTECT:
        from backend.storage.runtime_state import recover_catalog_journal
        recover_catalog_journal(DATA_DIR,guard)
        if _FRESH_DATA_DIR:
            from backend.storage.bootstrap import initialize_fresh
            initialize_fresh(DATA_DIR,guard)
        _initialize_catalog_storage()
    # 惰性探测：显式传了 --root 就不做全盘扫描，启动即开
    root = args.root or default_root()
    with STATE_LOCK:
        STATE["root"] = str(Path(root).resolve())
        if _SQL_ORG is not None:
            _SQL_CATALOG.ensure_root(STATE["root"])
            recovered = _catalog_trash().recover()
            if recovered:
                log("[catalog] 已复核 %d 项未完成文件操作" % len(recovered))
        if args.lan:
            STATE["lan"] = True
            STATE["token"] = secrets.token_urlsafe(6)
        if REMEMBER_ROOTS:
            add_root(STATE["root"])   # 记入常用目录（下次免传）；--no-remember 时跳过
        httpd = _make_server("0.0.0.0" if args.lan else "127.0.0.1",
                             args.port, Handler)
    # Preserve favorites when a drive or file is temporarily unavailable.
    if not MIGRATION_PROTECT:
        UPDATER.cleanup_leftovers(_exe_dir())
        threading.Thread(target=gc_thumbs, daemon=True).start()
        if _SQL_ORG is None:
            threading.Thread(target=trash_gc, daemon=True).start()
    if not MIGRATION_PROTECT and not args.lan and not args.no_update_check and update_auto_check() and update_url():
        threading.Thread(target=update_check_bg, daemon=True).start()   # 静默查一次更新
    port = httpd.server_address[1]
    if args.lan:
        url = f"http://127.0.0.1:{port}/?token={STATE['token']}"
        log(f"[{APP_TITLE}] LAN 模式，访问令牌: {STATE['token']}")
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("10.255.255.255", 1))    # 不真正发包，仅让系统选出本机局域网地址
            log(f"局域网访问: http://{s.getsockname()[0]}:{port}/?token={STATE['token']}")
            s.close()
        except OSError:
            pass
    else:
        url = f"http://127.0.0.1:{port}/"
    log(f"[{APP_TITLE}] {url}  root={STATE['root']}  Pillow={'on' if HAS_PIL else 'off'}")
    if _hotfix_active():
        # 明说一句：否则「改了前端没生效」会变成一场毫无线索的排查
        log(f"[hotfix] 前端来自热更覆盖：{FRONTEND_HOTFIX_DIR}"
            f"（版本 {frontend_version()}）；要用内置/源码那份就删掉该目录，或加 --no-hotfix 启动")
    log("按 Ctrl+C 退出，或用托盘图标退出。")
    if not args.no_browser and not MIGRATION_PROTECT:
        # ε-13（§6.6）：Timer 非守护 —— 启动 0.6s 内进程因故退出（单实例合并、
        # 端口异常）时，这个定时器会把进程拖住 0.6s 并且**反向弹出浏览器**，
        # 用户看到的是「程序都退了又自己开了个网页」。守护线程随主线程退出即消失。
        _t = threading.Timer(0.6, lambda: webbrowser.open(url))
        _t.daemon = True   # ε-13（§6.6）：非守护会拖住进程退出；Timer 构造器不收 daemon 关键字
        _t.start()
    tray_icon = None
    if not args.lan and not MIGRATION_PROTECT:
        tray_icon = _start_tray(httpd, url)
        if tray_icon is not None:
            globals()["_TRAY_ICON"] = tray_icon
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("bye.")
    finally:
        if tray_icon:
            try:
                tray_icon.stop()
            except Exception:
                pass


if __name__ == "__main__":
    main()
