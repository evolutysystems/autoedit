# -*- mode: python ; coding: utf-8 -*-
# GUI ランチャ(main_window.py)を起点にした windowed/onedir ビルド設定
# (docs/HowToRelease.md §4 準拠)
# 注: アイコン(gui/app.ico)は未配置のため icon 指定は省略している。
#     setting.json の同梱先は settings_window.py が __file__ 基準で参照する
#     'src/settings' に合わせる(実行時に初期設定を解決できるようにするため)。
#
# CUDA 不使用方針 (docs/request/resolve8.md): 音声認識は CPU 実行に統一したため、
# CUDA ランタイム DLL (nvidia-*-cu12) の同梱は廃止した。これにより配布ペイロードを
# 大幅に削減し、GitHub Releases の 1ファイル 2GiB 上限に収めやすくする。

a = Analysis(
    ['gui/main_window.py'],          # GUI エントリポイント
    pathex=['..'],                   # 'from src.xxx' 解決のためリポジトリルートを追加
    binaries=[],                     # CUDA 同梱は廃止 (CPU 実行のため不要 / resolve8.md)
    datas=[
        ('settings/setting.json', 'src/settings'),   # 初期設定を実行時参照先へ同梱
    ],
    hiddenimports=[
        'faster_whisper',            # 遅延 import のため明示
    ],
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
    exclude_binaries=True,           # onedir: バイナリは COLLECT 側へ
    name='AutoEdit',                 # 生成される実行ファイル名
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                       # AV 誤検知を避けるため UPX は無効
    console=False,                   # GUI: コンソール窓を出さない
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
    upx=False,
    upx_exclude=[],
    name='AutoEdit',                 # 出力フォルダ名 dist/AutoEdit/
)
