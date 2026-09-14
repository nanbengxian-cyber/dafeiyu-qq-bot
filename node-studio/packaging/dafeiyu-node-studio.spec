# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

# PyInstaller exposes SPECPATH as the spec directory; its parent is this module root.
ROOT = Path(SPECPATH).resolve().parent
STATIC = ROOT / "dafeiyu_flow" / "static"
GRAPHS = ROOT / "graphs"
DATA_FILES = (
    [(str(path), "dafeiyu_flow/static") for path in sorted(STATIC.iterdir()) if path.is_file()]
    + [(str(path), "graphs") for path in sorted(GRAPHS.glob("*.json"))]
)

analysis = Analysis(
    [str(ROOT / "packaging" / "windows_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=DATA_FILES,
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
