# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['C:\\Users\\Shi_Y\\OneDrive\\Codes\\my-tools\\Sym-Link\\src\\main.py'],
    pathex=[],
    binaries=[],
    datas=[('C:\\Users\\Shi_Y\\OneDrive\\Codes\\my-tools\\Sym-Link\\src\\ui\\assets', 'ui/assets')],
    hiddenimports=[],
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
    a.binaries,
    a.datas,
    [],
    name='SymLiSync',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['C:\\Users\\Shi_Y\\OneDrive\\Codes\\my-tools\\Sym-Link\\src\\ui\\assets\\icon.ico'],
)
