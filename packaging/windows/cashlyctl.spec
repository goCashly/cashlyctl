# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


repo_root = Path(SPECPATH).parents[1]

a = Analysis(
    [str(repo_root / "packaging" / "windows" / "cashlyctl-entry.py")],
    pathex=[str(repo_root / "src"), str(repo_root)],
    binaries=[],
    datas=[
        (str(repo_root / "README.md"), "."),
        (str(repo_root / "LICENSE"), "."),
        (str(repo_root / "docs"), "docs"),
    ],
    hiddenimports=collect_submodules("cashlyctl"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="cashlyctl",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="cashlyctl",
)
