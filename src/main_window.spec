# -*- mode: python ; coding: utf-8 -*-
# GUI ランチャ(main_window.py)を起点にした windowed/onedir ビルド設定
# (docs/HowToRelease.md §4 準拠)
# 注: アイコン(gui/app.ico)を EXE へ埋め込み、実行時のウィンドウ/タスクバー用に
#     datas でも同梱する (main_window._resolve_app_icon_path が sys._MEIPASS 基準で解決)。
#     setting.json の同梱先は settings_window.py が __file__ 基準で参照する
#     'src/settings' に合わせる(実行時に初期設定を解決できるようにするため)。
#
# CUDA 不使用方針 (docs/request/resolve8.md): 音声認識は CPU 実行に統一したため、
# CUDA ランタイム DLL (nvidia-*-cu12) の同梱は廃止した。これにより配布ペイロードを
# 大幅に削減し、GitHub Releases の 1ファイル 2GiB 上限に収めやすくする。

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

a = Analysis(
    ['gui/main_window.py'],          # GUI エントリポイント
    pathex=['..'],                   # 'from src.xxx' 解決のためリポジトリルートを追加
    binaries=[],                     # CUDA 同梱は廃止 (CPU 実行のため不要 / resolve8.md)
    datas=[
        ('settings/setting.json', 'src/settings'),   # 初期設定を実行時参照先へ同梱
        # アプリアイコンを実行時のウィンドウ/タスクバー用に同梱する。
        # main_window._resolve_app_icon_path が _internal/src/gui/app.ico を解決する。
        ('gui/app.ico', 'src/gui'),
        # FFmpeg/ffprobe を PATH 非依存にするため同梱する (error 20260708 / HowToRelease §3.2)。
        # onedir では _internal/ffmpeg/ へ展開され、ffmpeg_runner._resolve_exe が sys._MEIPASS 基準で解決する。
        # ソースは src/ffmpeg/ に配置(git 管理外・HowToRelease §2 で入手)。binaries でなく datas で純コピーする。
        ('ffmpeg/ffmpeg.exe', 'ffmpeg'),
        ('ffmpeg/ffprobe.exe', 'ffmpeg'),
    ] + collect_data_files('budoux'),   # BudouX のモデルJSON等を同梱 (request11)
    hiddenimports=[
        'faster_whisper',            # 遅延 import のため明示
    ] + collect_submodules('budoux'),   # BudouX 遅延 import 対策 (request11)
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
    name='Stretheus',                # 生成される実行ファイル名
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
    icon='gui/app.ico',              # exe 埋め込みアイコン (エクスプローラ/タスクバー)
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Stretheus',                # 出力フォルダ名 dist/Stretheus/
)
