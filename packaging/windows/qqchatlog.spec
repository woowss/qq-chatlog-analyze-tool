# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata


# PyInstaller 执行 spec 时不会提供 __file__；SPECPATH 是 spec 所在目录。
ROOT = Path(SPECPATH).resolve().parents[1]

# web 包里的模板与静态文件不靠 Python import 自动发现，必须显式放进冻结目录。
datas = [
    (str(ROOT / "web" / "templates"), "web/templates"),
    (str(ROOT / "web" / "static"), "web/static"),
]
datas += collect_data_files("jieba")
datas += copy_metadata("qqchatlog")

# 这些包的大部分导入是静态的；完整收集项目内模块和 Flask session 后端，
# 避免冻结后才暴露出开发环境里没有的动态导入。
hiddenimports = []
for package in ("analyzer", "parser", "webapp"):
    hiddenimports.extend(collect_submodules(package))
hiddenimports.extend(
    [
        "flask_session",
        "flask_session.cachelib",
    ]
)


a = Analysis(
    [str(ROOT / "packaging" / "windows" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter.test"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="QQChatLog",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="QQChatLog",
)
