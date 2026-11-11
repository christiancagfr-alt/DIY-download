# -*- mode: python ; coding: utf-8 -*-
import sys
from PyInstaller.utils.hooks import collect_all

datas = [('assets', 'assets'), ('logo.png', '.')]
binaries = []
hiddenimports = ['yt_dlp']
curl_data, curl_bins, curl_imports = collect_all('curl_cffi')
datas += curl_data
binaries += curl_bins
hiddenimports += curl_imports

tmp_ret = collect_all('yt_dlp')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['sheets_batch_downloader_modern.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    [] if sys.platform == "darwin" else a.binaries,
    [] if sys.platform == "darwin" else a.datas,
    [],
    exclude_binaries=sys.platform == "darwin",
    name='DIYDownloader',
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
    icon=['logo.icns' if sys.platform == 'darwin' else 'logo.ico'],
)

if sys.platform == 'darwin':
    collection = COLLECT(exe, a.binaries, a.datas, name='DIYDownloader')
    app = BUNDLE(collection, name='DIYDownloader.app', icon='logo.icns',
                 bundle_identifier='com.diydownloader.app')
