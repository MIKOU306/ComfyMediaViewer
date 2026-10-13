#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ComfyMediaViewer · NSFW 内容检测（可选依赖模块）
==================================================

判一张图「色情 / 擦边 / 正常」，供主程序的自动标记与 SFW-NSFW 显示切换使用。

设计基调与 server.py 一致：**有则用，没有就优雅降级**。
    onnxruntime 装了 + 模型文件在 → 功能可用
    缺任何一样               → available() 为 False，界面提示，**不报错、不影响任何既有功能**

为什么推理放服务端而不是浏览器：权重不用每个窗口重下、结果能落盘复用、
并且与「元数据由服务端索引算好、列表接口一次性带出」的既有架构一致。

模型：nsfwjs 系（infinitered/nsfwjs 的 MobileNetV2，TF 转 ONNX），
      5 类输出 drawings / hentai / neutral / porn / sexy。
      本地实测：输入 input_1 [N,224,224,3] NHWC、/255 归一化、输出 dense_3 [N,5] 已 softmax，
      单张 CPU 1.4~2.8 ms，模型加载 48 ms。

⚠️ 口径提醒：`drawings` **不计入** NSFW 分数。
   否则正常插画、像素画、二次元场景图会被大批误杀 —— 这条是这个功能能不能用的关键。

命令行自检：
    python nsfw.py 图1.png 图2.jpg     # 打印每张的 5 类概率与总分
    python nsfw.py --list              # 列出找到的模型与可用性
"""

import os
import sys
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------- 依赖探测

try:
    import numpy as np
    HAS_NP = True
except ImportError:                     # pragma: no cover - 环境相关
    HAS_NP = False

try:
    import onnxruntime as ort
    HAS_ORT = True
except ImportError:
    ort = None
    HAS_ORT = False

try:
    from PIL import Image, ImageOps
    HAS_PIL = True
except ImportError:
    Image = ImageOps = None
    HAS_PIL = False


# ---------------------------------------------------------------- 模型登记表
# 每个模型只登记「怎么预处理 + 门面信息」，输入输出张量名**运行时从 session 读**——
# 不同导出版本的命名不一定一致（参考实现里叫 input_1/dense_3，但那是巧合，不是契约）。
# 硬编码名字会在换模型时静默出错，读一次 session 就没有这个问题。
LABELS = ("drawings", "hentai", "neutral", "porn", "sexy")   # 顺序与模型输出对齐
NSFW_IDX = (1, 3, 4)                    # hentai / porn / sexy —— 计入总分的三类
DRAWINGS_IDX = 0                        # 单列出来是因为它必须被排除，写清楚免得后人改回去

MODELS = {
    "light": {
        "file": "nsfwjs.onnx",
        "label": "轻量档",
        "desc": "MobileNetV2 · 224×224 · 约 10 MB · 单张约 2 ms",
        "size": 224,
        "norm": "div255",               # 参考实现：np.array(im)/255.0
        "ver": 1,                       # 预处理或模型换版本就 +1，让旧缓存整体失效
    },
    "precise": {
        "file": "inception_v3.onnx",
        "label": "精确档",
        "desc": "InceptionV3 · 299×299 · 约 85 MB · 误判更少但更慢",
        "size": 299,
        "norm": "div255",               # 同源导出，与轻量档共用同一套预处理
        "ver": 1,
    },
}
DEFAULT_MODEL = "light"


# ---------------------------------------------------------------- 路径解析
# 模型查找顺序（先找到先用）：
#   1. CMV_NSFW_MODELS 环境变量        —— 验证脚本 / 自定义部署
#   2. PyInstaller 解包目录 models/nsfw —— 打包后随 exe 分发的那份（离线可用）
#   3. 程序目录 models/nsfw            —— 源码直启；也方便用户手动丢精确档进来
#   4. 数据目录 models/nsfw            —— 设置面板一键下载的落点
# 「随包分发」是第 2/3 条：这个项目要给别人用，模型必须跟着包走，不能依赖联网。

def _exe_dir() -> Path:
    """程序所在目录。与 server.py 的 _exe_dir 同口径（含 CMV_EXE_DIR 覆盖）。"""
    env = os.environ.get("CMV_EXE_DIR")
    if env:
        return Path(env).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


_MODEL_DIRS = []
_MODEL_DIRS_LOCK = threading.Lock()


def _default_model_dirs():
    dirs = []
    env = os.environ.get("CMV_NSFW_MODELS")
    if env:
        dirs.append(Path(env))
    meipass = getattr(sys, "_MEIPASS", None)     # PyInstaller onefile 的解包目录
    if meipass:
        dirs.append(Path(meipass) / "models" / "nsfw")
    dirs.append(_exe_dir() / "models" / "nsfw")
    try:
        lad = os.environ.get("LOCALAPPDATA", str(Path.home()))
        dirs.append(Path(lad) / "ComfyMediaViewer" / "models" / "nsfw")
    except Exception:
        pass
    return dirs


def configure(model_dirs=None):
    """由主程序注入额外的模型查找目录（数据目录随便携模式变，只有 server.py 知道）。

    放在模块级变量而不是函数参数：扫描线程、预览请求都会用到，逐处传参太啰嗦。
    """
    global _MODEL_DIRS
    with _MODEL_DIRS_LOCK:
        _MODEL_DIRS = [Path(p) for p in (model_dirs or [])] + _default_model_dirs()


configure()                                  # 先按默认值初始化，主程序可随时再调


def model_dirs():
    with _MODEL_DIRS_LOCK:
        return list(_MODEL_DIRS)


def find_model(model_id: str):
    """返回 (Path, cfg) 或 (None, cfg)。"""
    cfg = MODELS.get(model_id)
    if not cfg:
        return None, None
    for d in model_dirs():
        p = d / cfg["file"]
        try:
            if p.is_file() and p.stat().st_size > 0:
                return p, cfg
        except OSError:
            continue
    return None, cfg


def available_models():
    """给设置面板用：每个档位能不能用、模型文件在哪、多大。"""
    out = []
    for mid, cfg in MODELS.items():
        p, _ = find_model(mid)
        out.append({
            "id": mid, "label": cfg["label"], "desc": cfg["desc"],
            "file": cfg["file"],
            "ready": bool(p) and HAS_ORT and HAS_PIL,
            "found": bool(p),
            "path": str(p) if p else "",
        })
    return out


def unavailable_reason() -> str:
    """为什么不可用 —— 界面要把这句话原样显示给用户，别让它只出现在日志里。"""
    if not HAS_NP or not HAS_ORT:
        # 决策 1A（2026-09-19）：正式分发**随包**带引擎 + 模型，正常情况下打包版不会走到这里。
        # 走到这里 = 构建机上没装 onnxruntime/numpy（modulegraph 静默没收全），或源码模式缺依赖。
        # 所以这句**不能**再说「本版未随包分发」—— 1A 下那是句假话。
        if getattr(sys, "frozen", False):
            return "本版未打进推理引擎（打包时构建环境缺少 onnxruntime / numpy，需重新打包）"
        return "未安装推理引擎（pip install onnxruntime numpy）"
    if not HAS_PIL:
        return "未安装 Pillow（pip install pillow）"
    if not find_model(DEFAULT_MODEL)[0] and not any(find_model(m)[0] for m in MODELS):
        return "未找到模型文件（models/nsfw/*.onnx）"
    return ""


def available() -> bool:
    return not unavailable_reason()


# ---------------------------------------------------------------- 推理会话
_SESS = {}                       # {model_id: InferenceSession}
_SESS_LOCK = threading.Lock()
_SESS_META = {}                  # {model_id: {"in","out","size","loaded_at"}}
LOAD_MS = {}                     # {model_id: 加载耗时}，给「关于」页显示
# B4-A10（CMV3-LOCAL-FIX，从旧工程回植）：最近一次会话加载**失败**的原因。
# `_session()` 吞异常返回 (None, None) 是刻意的（一张坏图不该搞挂整批扫描），
# 但吞掉之后调用方只能看到「预测返回 None」，与「图坏了」不可区分 ——
# 整批失败时无从归因。这里留一份原始异常给上层读。
_LOAD_ERR = {}                   # {model_id: "TypeName: msg"}


def _session(model_id: str):
    """懒加载 + 单例。第一次真正要用才加载，进程启动不变慢。"""
    if not HAS_ORT or not HAS_PIL:
        _LOAD_ERR[model_id] = ("缺少依赖：" + ("onnxruntime" if not HAS_ORT else "Pillow"))
        return None, None
    with _SESS_LOCK:
        if model_id in _SESS:
            return _SESS[model_id], _SESS_META[model_id]
        path, cfg = find_model(model_id)
        if not path:
            _LOAD_ERR[model_id] = f"未找到模型文件（档位 {model_id}）"
            return None, None
        try:
            opts = ort.SessionOptions()
            # 别吃满所有核：用户很可能同时在跑 ComfyUI 出图，抢核会让两边都变慢
            try:
                opts.intra_op_num_threads = max(1, min(4, (os.cpu_count() or 4)))
            except Exception:
                pass
            t0 = time.time()
            s = ort.InferenceSession(str(path), sess_options=opts,
                                     providers=["CPUExecutionProvider"])
            LOAD_MS[model_id] = (time.time() - t0) * 1000
            ins = s.get_inputs()[0]
            outs = s.get_outputs()[0]
            # 尺寸以模型自报的形状为准，不迷信登记表里的数字（换模型时少一个踩坑点）
            size = cfg["size"]
            try:
                shp = list(ins.shape)
                if isinstance(shp[-1], int) and shp[-1] == 3 and isinstance(shp[1], int):
                    size = shp[1]
            except Exception:
                pass
            _SESS[model_id] = s
            _SESS_META[model_id] = {"in": ins.name, "out": outs.name, "size": size,
                                    "path": str(path)}
            _LOAD_ERR.pop(model_id, None)    # 这次加载成功：抹掉上一次的失败留痕
            return s, _SESS_META[model_id]
        except Exception as ex:
            _LOAD_ERR[model_id] = f"{type(ex).__name__}: {ex}"
            return None, None


def last_load_error(model_id: str = None) -> str:
    """最近一次会话加载失败的原因；没失败过返回 ""（B4-A10）。给扫描线程如实回报用。"""
    if model_id:
        return _LOAD_ERR.get(model_id, "")
    return next(iter(_LOAD_ERR.values()), "")


def warm(model_id: str = DEFAULT_MODEL) -> bool:
    """预热：扫描开始前调一次，把加载耗时从「第一张图」里挪出来。"""
    s, _ = _session(model_id)
    return s is not None


def reset():
    """清掉已加载的 session（换模型 / 设置页点了重载时用）。"""
    with _SESS_LOCK:
        _SESS.clear()
        _SESS_META.clear()
        _LOAD_ERR.clear()


# ---------------------------------------------------------------- 打分
def _prep(path: Path, size: int, norm: str):
    """读图 → 转正 → RGB → 缩放 → 归一化 → NHWC float32。"""
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)      # 按 EXIF 转正，竖拍图不会躺倒影响判定
        if im.mode != "RGB":
            im = im.convert("RGB")
        im = im.resize((size, size), Image.NEAREST)   # 参考实现用 NEAREST，跟随它
        a = np.asarray(im, dtype=np.float32)
    if norm == "div255":
        a /= 255.0
    return a[None, ...]


def predict(path, model_id: str = None):
    """单张打分。

    返回 {"sc": 0.87, "top": "porn", "raw": {...}, "model": "light"}

    **不返回 nsfw/sfw 标签** —— 阈值是可配置的服务端设置，判定归调用方，
    这个模块只负责「算分」。分和阈值分家之后，用户改阈值不必重跑推理。

    任何失败（模型缺失 / 图损坏 / 解码失败）一律返回 None。
    **绝不让一张坏图把整批扫描搞挂**，这是 _index_builder 已经趟过的坑。
    """
    model_id = model_id or DEFAULT_MODEL
    s, meta = _session(model_id)
    if s is None:
        return None
    try:
        p = Path(path)
        x = _prep(p, meta["size"], MODELS[model_id]["norm"])
        out = s.run([meta["out"]], {meta["in"]: x})[0]
        probs = [float(v) for v in out[0]]
        if len(probs) != len(LABELS):
            return None                       # 输出维度对不上：宁可标 unknown，不猜
        raw = dict(zip(LABELS, probs))
        sc = sum(raw[LABELS[i]] for i in NSFW_IDX)
        top = LABELS[max(range(len(probs)), key=lambda i: probs[i])]
        return {"sc": round(sc, 6), "top": top,
                "raw": {k: round(v, 6) for k, v in raw.items()},
                "model": model_id}
    except Exception:
        return None


# ---------------------------------------------------------------- CLI 自检
def _cli(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--list":
        print("onnxruntime:", "OK" if HAS_ORT else "缺失",
              "| numpy:", "OK" if HAS_NP else "缺失",
              "| Pillow:", "OK" if HAS_PIL else "缺失")
        print("查找目录:")
        for d in model_dirs():
            print("   ", d, "" if d.is_dir() else "(不存在)")
        print("模型:")
        for m in available_models():
            flag = "可用" if m["ready"] else ("文件缺失" if not m["found"] else "引擎缺失")
            print(f"    {m['id']:8s} {m['label']:6s} {flag:8s} {m['path'] or m['file']}")
        print("不可用原因:", unavailable_reason() or "（无，功能可用）")
        return 0
    if not available():
        print("功能不可用:", unavailable_reason(), file=sys.stderr)
        return 1
    model = DEFAULT_MODEL
    if argv[0] == "--model":
        model = argv[1]
        argv = argv[2:]
    warm(model)
    print(f"模型 {model} 加载完成 {LOAD_MS.get(model, 0):.0f} ms")
    rc = 0
    for a in argv:
        p = Path(a)
        if not p.is_file():
            print(f"  跳过（不存在）: {a}")
            rc = 1
            continue
        t0 = time.time()
        r = predict(p, model)
        dt = (time.time() - t0) * 1000
        if not r:
            print(f"  失败: {p.name}  ({dt:.0f} ms)")
            rc = 1
            continue
        print(f"  sc={r['sc']:.3f} top={r['top']:8s} ({dt:.1f} ms)  {p.name}")
        print("        " + "  ".join(f"{k}={v:.3f}" for k, v in r["raw"].items()))
    return rc


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
