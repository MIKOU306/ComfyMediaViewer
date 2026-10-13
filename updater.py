# -*- coding: utf-8 -*-
"""updater 占位模块（3.0 合并 · 阶段 1）

背景：本地旧 `server.py` 没有热更新；第三方 3.0.0 的 `server.py` **硬依赖**
`import updater`（第 231 行），并在 7 处调用其符号。2026-10-10 主人拍板
「updater 先搁置，骨架跑通再说」——为了不把 599 行联网更新器一起带进新工程，
这里提供一个**行为空转**的替身：

    不联网 · 不下载 · 不写盘 · 不替换 exe

因此 `/api/update/check` `/api/update/apply` 会明确回「更新功能未启用」，
而不会静默联网。`load_applied()` 返回空字典 ⇒ 前端版本号自然回落到
`APP_VERSION`，与「没有热更覆盖」的状态一致。

⚠️ 启用真更新器时：删除本文件，把第三方 `source/updater.py` 原样放回同名位置即可
   （接口与真模块完全一致，server.py 无需改动）。
"""
from __future__ import annotations

import threading
from pathlib import Path

MANIFEST_NAME = "latest.json"
UA = "ComfyMediaViewer-Updater/1.0"
PAYLOAD_DIRS = ("frontend",)
KEEP_BACKUPS = 2

DISABLED_MSG = "更新功能未启用（3.0 合并阶段 1 的 updater 占位模块）"

_LOCK = threading.Lock()
STATE = {
    "phase": "idle",
    "pct": 0,
    "msg": "",
    "error": "",
    "target_version": "",
    "restart_required": False,
    "available": False,
    "notes": "",
    "exe_blocked": False,
}

_LOGGER = None


class UpdateError(Exception):
    """与真模块同名，保证调用方 `except UPDATER.UpdateError` 不会 NameError。"""


def set_logger(fn):
    global _LOGGER
    _LOGGER = fn


def log(msg: str):
    if _LOGGER:
        try:
            _LOGGER(msg)
        except Exception:
            pass


def set_state(**kw):
    with _LOCK:
        STATE.update(kw)


def get_state() -> dict:
    with _LOCK:
        return dict(STATE)


def load_applied(hotfix_dir) -> dict:
    """永远返回空 —— 等价于「没有任何热更覆盖」。"""
    return {}


def save_applied(hotfix_dir, data: dict):
    """不落盘。"""
    return None


def check(manifest_url: str, app_version: str, frontend_version: str,
          exe_path=None, cache_file=None, allow_exe: bool = True) -> dict:
    set_state(phase="idle", pct=0, msg="", error="", available=False)
    log("[update] 占位模块：忽略检查请求 " + str(manifest_url))
    return {"ok": False, "error": DISABLED_MSG, "disabled": True}


def apply(man_url: str, work_dir, hotfix_dir, app_version: str,
          exe_path=None, cache_file=None, allow_exe: bool = True) -> dict:
    log("[update] 占位模块：忽略应用请求 " + str(man_url))
    return {"ok": False, "error": DISABLED_MSG, "disabled": True}


def swap_exe(new_exe, target_exe, wait_pid: int = 0, timeout: float = 60) -> bool:
    """不替换任何文件。"""
    log("[update] 占位模块：拒绝替换 exe")
    return False


def cleanup_leftovers(exe_dir):
    """只清半截下载（*.part）；失败静默。"""
    try:
        for p in Path(exe_dir).glob("*.part"):
            try:
                p.unlink()
            except OSError:
                pass
    except OSError:
        pass
