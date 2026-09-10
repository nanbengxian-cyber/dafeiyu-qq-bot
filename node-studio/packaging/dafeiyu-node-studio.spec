# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

# PyInstaller defines SPECPATH as the directory containing this spec file.
ROOT = Path(SPECPATH).resolve().parent

analysis = Analysis(
    [str(ROOT / "windows_entry.py")],
    pathex=[str(ROOT.parent)],
    binaries=[],
    datas=[
        (str(ROOT.parent / "dafeiyu_flow" / "static"), "dafeiyu_flow/static"),
        (str(ROOT.parent / "graphs"), "graphs"),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest"],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="dafeiyu-node-studio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
