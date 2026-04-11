# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules


ROOT = Path(os.path.abspath(os.environ.get("NANOHARNESS_PROJECT_ROOT", os.getcwd())))
APP_NAME = os.environ.get("NANOHARNESS_APP_NAME", "NanoHarness")
BUNDLE_ID = os.environ.get("NANOHARNESS_BUNDLE_ID", "com.nanoharness.app")
DISPLAY_VERSION = os.environ.get("NANOHARNESS_BUILD_VERSION", "0.0.0-dev")
BUNDLE_SHORT_VERSION = os.environ.get("NANOHARNESS_BUNDLE_SHORT_VERSION", "0.0.0")
BUNDLE_BUILD_VERSION = os.environ.get("NANOHARNESS_BUNDLE_BUILD_VERSION", "0")
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
    [str(ROOT / "packaging" / "gui_launcher.py")],
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
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=True,
    icon=ICON_PATH,
    target_arch=TARGET_ARCH,
    codesign_identity=CODESIGN_IDENTITY,
)
coll = COLLECT(
    exe,
    analysis.binaries,
    analysis.zipfiles,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
app = BUNDLE(
    coll,
    name=f"{APP_NAME}.app",
    icon=ICON_PATH,
    bundle_identifier=BUNDLE_ID,
    info_plist={
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleShortVersionString": BUNDLE_SHORT_VERSION,
        "CFBundleVersion": BUNDLE_BUILD_VERSION,
        "NSHighResolutionCapable": "True",
        "LSApplicationCategoryType": "public.app-category.developer-tools",
        "NSHumanReadableCopyright": f"NanoHarness {DISPLAY_VERSION}",
    },
)
