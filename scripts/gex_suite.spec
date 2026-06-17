# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for GEX Suite (PySide6 one-folder build).
#
# Usage (from repository root ``GEX_suite/``):
#   pip install -r requirements-build.txt
#   pyinstaller scripts/gex_suite.spec
#
# Output: ``dist/GEXSuite/GEXSuite.exe`` (Windows) or ``dist/GEXSuite/GEXSuite`` (macOS/Linux).
# Playwright 瀏覽器二進位不會一併打入；若需凍結版 Scraper/TV 自動化，請另行處理 playwright install 路徑或改為僅 Chart 模式。

from pathlib import Path

spec_dir = Path(SPECPATH).resolve()
PROJECT = spec_dir.parent

block_cipher = None

a = Analysis(
    [str(PROJECT / "main.py")],
    pathex=[str(PROJECT)],
    binaries=[],
    datas=[],
    hiddenimports=[
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtPrintSupport",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="GEXSuite",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="GEXSuite",
)
