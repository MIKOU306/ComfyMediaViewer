# -*- mode: python ; coding: utf-8 -*-
import glob as _glob

# 内容安全（NSFW，V2.12.5）：**引擎与模型都随包**（主人 2026-09-19 决策 1A + 3）。
#   理由：这个程序是要发给别人用的 —— 让对方为了一个可选功能去联网下 10 MB 模型、
#         再自己装 onnxruntime，等于这功能对绝大多数人根本不存在。离线可用是硬要求。
#   代价（如实记账，不假装没有）：
#         onnxruntime ≈ 40 MB + numpy ≈ 15 MB + 模型 9.84 MB
#         ⇒ exe 从 14.5 MB 涨到 **约 60 MB**（打包前估算）；**实测 43.05 MB**（UPX 压完，V2.12.5 产物）。
#   用 glob 而不是写死文件名：只放轻量档就只打轻量档，手动丢进来的精确档会自动带上。
_nsfw_models = [(p, 'models/nsfw') for p in sorted(_glob.glob('models/nsfw/*.onnx'))]
_mei_marker = [('mei_owner.txt', '.')]

a = Analysis(
    ['server.py'],
    pathex=[],
    binaries=[],
    datas=[('frontend', 'frontend'), ('app.ico', '.')] + _mei_marker + _nsfw_models,
    # nsfw 是我们自己的模块；onnxruntime / numpy 由 nsfw.py 的顶层 import 自动被
    # modulegraph 发现，不必列在这里 —— 列了反而会在「没装依赖」的构建机上直接失败。
    hiddenimports=['ws_client', 'pystray', 'pystray._win32', 'nsfw', 'updater'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # ⚠️ 决策 1A 的口子就在这行：**不许排除** onnxruntime / numpy。
    #    排除了 = 「代码在、引擎不在」，界面显示不可用，那正是 1B 的行为。
    #    想回到 1B（小包）就在这里加回 excludes=['onnxruntime', 'numpy']，
    #    但必须同步改 models/nsfw/README.md 与 nsfw.py 的 unavailable_reason() 文案
    #    —— 三处口径不一致会让「界面说的话」和「包里有什么」对不上。
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ComfyMediaViewer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    icon='app.ico',
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
