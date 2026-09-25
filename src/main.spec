# -*- mode: python ; coding: utf-8 -*-

from glob import glob

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        # コメント字幕のアイコン (ver3 resolve11 §5.10)
        ('comment_icon.png', 'src'),
        # 透かし素材 (ver5 resolve §8-3)
        ('watermark.png', 'src'),
        # トラッキングぼかしのモデル (ver5 resolve2 §8-3)。
        # models/ が空でもビルドは通り、アプリは通常どおり起動する
        # (設定画面に「モデルが見つかりません」と出るだけ / §8-6)。
        #
        # ライセンス表記 (licenses/) はここには入れない。onedir では datas が
        # _internal/ 配下へ入ってしまい、利用者が開く場所にならないため、
        # リリース手順で exe 直下へコピーする (§5.10.2)。
        # ver5 resolve8: 人物同定 (osnet) と輪郭 (silhouette) を廃止したため、
        # **使うモデルだけを名前で指定する** (古い .onnx が残っていても同梱しない)。
        *[(path, 'src/models') for path in glob('models/yolox_tiny.onnx')],
    ] + collect_data_files('budoux'),          # BudouX のモデルJSON等を同梱 (request11)
    hiddenimports=collect_submodules('budoux'),  # BudouX 遅延 import 対策 (request11)
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
    name='main',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
