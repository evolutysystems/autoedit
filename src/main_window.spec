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

import json
import os
import re
import tempfile
from glob import glob

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


# 同梱する初期設定から開発端末のローカルパスを落とす (security.md F5)。
#
# settings/setting.json は **開発端末で実際に使っている設定ファイル**で、
# アプリが起動のたびに書き戻す。そのまま datas へ入れると
# `D:/develop/autoedit/input` のような開発者のディレクトリ構成が
# 配布物 (_internal/src/settings/setting.json) に載って全利用者へ渡る。
# 利用者側にも存在しないパスなので、設定として役に立つこともない。
#
# そこでビルドのたびに該当キーを空へ戻した**複製**を作り、それを同梱する。
# 空 = settings_window.DEFAULT_SETTINGS と同じ状態で、
# インストーラが output_directory を利用者の選択で上書きする (installer/AutoEdit.iss §6.2)。
_BLANKED_GENERAL_KEYS = ('video_directory', 'opening_video', 'ending_video', 'output_directory')

# 「編集の続き」の履歴も一覧ごと空へ戻す (security.md F5)。
# 既定は空 (settings_window.DEFAULT_SETTINGS) で、開発端末が開いたファイルの
# 置き場所を配布物へ載せる意味もないため、general の 4 キーと同じ扱いにする。
_EMPTIED_LIST_PATHS = (
    ('timeline', 'project', 'recent'),
    ('timeline', 'project', 'recent_archive'),
)

# ドライブレター付きの絶対パスと UNC パス
_LOCAL_PATH_RE = re.compile(r'^(?:[A-Za-z]:[\\/]|\\\\)')


# data (dict/list/str の入れ子) から絶対パスらしい値を "キー経路=値" で拾う
def _find_local_paths(data, path='$'):
    if isinstance(data, dict):
        for key, value in data.items():
            yield from _find_local_paths(value, f'{path}.{key}')
    elif isinstance(data, list):
        for index, value in enumerate(data):
            yield from _find_local_paths(value, f'{path}[{index}]')
    elif isinstance(data, str) and _LOCAL_PATH_RE.match(data):
        yield f'{path} = {data}'


def _build_shipped_settings(source='settings/setting.json'):
    with open(source, encoding='utf-8-sig') as handle:
        data = json.load(handle)

    general = data.get('general')
    if isinstance(general, dict):
        for key in _BLANKED_GENERAL_KEYS:
            if general.get(key):
                general[key] = ''

    # 最近使ったプロジェクトの一覧を空へ戻す (アプリが起動のたびに書き足すため)
    for keys in _EMPTIED_LIST_PATHS:
        node = data
        for key in keys[:-1]:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, dict) and node.get(keys[-1]):
            node[keys[-1]] = []

    # 空へ戻すキーは既知のものだけ。上で拾えない場所 (新しい設定項目や
    # recent/extra_dirs のような一覧) に絶対パスが残っていたら、
    # 何を空にすべきかはここでは決められないため**ビルドを止める**。
    leaked = sorted(_find_local_paths(data))
    if leaked:
        raise SystemExit(
            'setting.json にローカルの絶対パスが残っています。'
            'このファイルは配布物へそのまま同梱されるため、'
            '該当する設定を空にしてからビルドしてください (security.md F5):\n  '
            + '\n  '.join(leaked))

    # API 接続先を空へ戻す (sec2 N1)。
    #
    # 空なら src/services/config.py の _DEFAULT_BASE_URL (現運用の環境) へ倒れる。
    # setting.json は開発端末で日常的に書き換わるファイルのため、そのまま同梱すると
    # **そのときの接続先が配布物へ焼き付く**。接続先は config.py の 1 行で決めたいので、
    # ここでは必ず空へ落とす。開発端末は環境変数 STRETHEUS_API_BASE_URL で
    # 別環境を指すため、空にしても開発時のテストには影響しない。
    api = data.get('api')
    if isinstance(api, dict) and api.get('base_url'):
        api['base_url'] = ''

    # 認証情報は配布物へ入れない (client_id は公開前提なので対象外)
    secret = data.get('archive', {}).get('auth', {}).get('client_secret')
    if secret:
        raise SystemExit(
            'setting.json の archive.auth.client_secret が入っています。'
            '配布物に同梱されるため空にしてからビルドしてください (security.md F5)。')

    destination = os.path.join(tempfile.mkdtemp(prefix='stretheus-shipped-settings-'),
                               'setting.json')
    with open(destination, 'w', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=4)
    return destination


a = Analysis(
    ['gui/main_window.py'],          # GUI エントリポイント
    pathex=['..'],                   # 'from src.xxx' 解決のためリポジトリルートを追加
    binaries=[],                     # CUDA 同梱は廃止 (CPU 実行のため不要 / resolve8.md)
    datas=[
        # 初期設定を実行時参照先へ同梱する。開発端末のローカルパスを落とした
        # 複製を作って入れる (上の _build_shipped_settings / security.md F5)
        (_build_shipped_settings(), 'src/settings'),
        # アプリアイコンを実行時のウィンドウ/タスクバー用に同梱する。
        # main_window._resolve_app_icon_path が _internal/src/gui/app.ico を解決する。
        ('gui/app.ico', 'src/gui'),
        # コメント字幕のアイコン (ver3 resolve11 §5.10)。
        # comment_decor.resolve_icon_path が _internal/src/comment_icon.png を解決する。
        ('comment_icon.png', 'src'),
        # FFmpeg/ffprobe を PATH 非依存にするため同梱する (error 20260708 / HowToRelease §3.2)。
        # onedir では _internal/ffmpeg/ へ展開され、ffmpeg_runner._resolve_exe が sys._MEIPASS 基準で解決する。
        # ソースは src/ffmpeg/ に配置(git 管理外・HowToRelease §2 で入手)。binaries でなく datas で純コピーする。
        ('ffmpeg/ffmpeg.exe', 'ffmpeg'),
        ('ffmpeg/ffprobe.exe', 'ffmpeg'),
        # 透かし素材 (ver5 resolve §8-3)。watermark_overlay が _internal/src/watermark.png を解決する。
        ('watermark.png', 'src'),
        # Timeline 画面の取り扱い説明 (ver9 resolve19 §3.6)。
        # help_dialog.resolve_image_path が _internal/src/t1.jpg を解決する。
        # setting.json の timeline.help.images で増やしたときは、ここにも足す
        # (入れ忘れると配布版だけ「？」ボタンが消える)。
        ('t1.jpg', 'src'),
        ('t2.jpg', 'src'),
        # トラッキングぼかしのモデル (ver5 resolve8 §9.4)。_internal/src/models/ へ入る。
        # ver5 resolve8 で人物同定 (osnet) と輪郭 (silhouette) を廃止したため、
        # **使うモデルだけを名前で指定する** (models/ に古い .onnx が残っていても同梱しない)。
        # models/ が空でもビルドは通り、アプリは通常どおり起動する
        # (囲みを模様の変化だけで追うようになる / resolve8 §5.13)。
        # ライセンス表記 (licenses/) は datas に入れず、リリース手順で exe 直下へコピーする (HowToRelease §5.2.1)。
        *[(path, 'src/models') for path in glob('models/yolox_tiny.onnx')],
    ] + collect_data_files('budoux')     # BudouX のモデルJSON等を同梱 (request11)
      + collect_data_files('faster_whisper')  # Silero VAD モデル (assets/silero_vad_v6.onnx)。無いと VAD 無しへ落ち、幻聴対策が効かない
      + collect_data_files('twitchdl')   # twitch-dl の同梱データ (flow17 R3 / Twitch取得)
      + collect_data_files('certifi'),   # httpx(twitch-dl) の HTTPS 用ルート証明書 (cacert.pem)
    hiddenimports=[
        'faster_whisper',            # 遅延 import のため明示
        # アーカイブ結果画面の採点グラフ (flow17 R2 / resolve17 §4.7.1)。
        # Qt アドオンのため明示同梱する。CUDA/torch は従来どおり非同梱 (CPU-only 維持)。
        'PySide6.QtCharts',
        # 字幕編集画面の動画プレビュー (resolve23 §5.1)。
        # 未同梱環境では静止画フォールバックで動作するが、再生のため明示同梱する。
        'PySide6.QtMultimedia',
        'PySide6.QtMultimediaWidgets',
        # Timeline 編集画面のフレーム取得 (ver3 resolve.md §6.4-1)。
        # faster-whisper 経由で導入済みだが、遅延 import のため明示する。
        # 未同梱でも ffmpeg フレーム抽出へフォールバックする。
        'av',
        # twitch-dl を本体exeの multi-call で動かすため CLI とその依存を明示同梱する (flow17 R3)。
        'twitchdl.cli',
    ] + collect_submodules('budoux')     # BudouX 遅延 import 対策 (request11)
      + collect_submodules('twitchdl')   # twitch-dl (VOD/コメント取得) を同梱 (flow17 R3)
      + collect_submodules('httpx')      # twitch-dl の HTTP クライアント
      + collect_submodules('httpcore')   # httpx の下位 (h11 等)
      + collect_submodules('m3u8'),      # twitch-dl の HLS プレイリスト解析
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # ビルド時にだけ必要なもの。配布物へ入れない
        # (StretheusPlan/security.md の F13)。
        #
        # tools/collect_licenses.py が _BUILD_ONLY として扱っているのに
        # 凍結配布へ入っており、--scan-bundle が「manifest に載っていない同梱物」
        # として報告していた。ライセンス表記の対象でもないため、
        # 載せるのではなく外すのが正しい。
        #
        # 既知の脆弱性 (CVE-2025-47273 / PackageIndex のパストラバーサル) を
        # 配布物から外す意味もある。アプリは PackageIndex を使わない。
        #
        # 検証: 3 つを sys.meta_path で遮断した状態でも
        #   ・アプリの主要モジュール 8 本の import が成功する
        #   ・tests 全件 (1115 件) が合格する
        # ことを確認済み。
        'setuptools',
        'pkg_resources',

        # 使っていない重量依存 (sec2 S6)。約 361MB を占めていた。
        #
        # torch を引いているのは modules/speaker_filter.py (話者照合によるテロップの
        # 絞り込み) だけで、この機能は **実測で成立せず不採用が確定している**
        # (docs/quality/resolve1.md §12.6・§12.7 / 既定 "off"・設定画面にも出さない)。
        # speaker_filter は torch を遅延 import し、**読めなければ警告を出して素通りする**
        # (_import_torch) ため、外してもアプリは通常どおり動く。
        # 絞り込みを試したくなったら、ここから torch/torchaudio を外して再ビルドすれば戻る。
        #
        # transformers はソースから一切参照していない (faster-whisper の conversion extra や
        # sentence-transformers などが推移的に引いていただけ)。
        # 音声認識は faster-whisper + ctranslate2 (CPU) で、torch も transformers も要らない。
        #
        # 容量だけでなく、**使っていないコードを配らない**ことが目的でもある
        # (transformers はモデルの自動ダウンロード経路を持つ)。
        'torch',
        'torchaudio',
        'transformers',
    ],
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
