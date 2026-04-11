# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules


ROOT = Path(os.path.abspath(os.environ.get("NANOHARNESS_PROJECT_ROOT", os.getcwd())))
CLI_NAME = os.environ.get("NANOHARNESS_CLI_NAME", "nanoh")
ICON_PATH = os.environ.get("NANOHARNESS_ICON") or None
TARGET_ARCH = os.environ.get("NANOHARNESS_TARGET_ARCH") or None
CODESIGN_IDENTITY = os.environ.get("NANOHARNESS_CODESIGN_IDENTITY") or None

datas = []
binaries = []
hiddenimports = []

for package_name in ("webview", "fastapi", "uvicorn", "textual"):
    pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(package_name)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hiddenimports

hiddenimports += collect_submodules("nanoharness")

analysis = Analysis(
    [str(ROOT / "packaging" / "cli_launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.zipfiles,
    analysis.datas,
    [],
    name=CLI_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=ICON_PATH,
    target_arch=TARGET_ARCH,
    codesign_identity=CODESIGN_IDENTITY,
)
