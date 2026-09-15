import copy
import json
import logging
import os
import shutil
import sys
from PySide6.QtCore import Qt
from PySide6.QtGui import QDoubleValidator, QFont, QFontDatabase, QIntValidator
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

# ガラスモーフィズムのテーマ (ver3 resolve3)。
# デザイントークンと ui セクションの既定は src/gui/theme.py が唯一の出どころ (§4-2)。
# 本ファイルは単独実行 (python src/settings/settings_window.py) もできるため、
# パッケージ外から起動された場合はリポジトリルートを sys.path へ補ってから読み込む。
# color_field は色文字列の変換と色見本の描画を Timeline のインスペクタと共有する
# ための小部品 (resolve6 §5.4)。theme と同じ経路で読み込む。
try:
    from src.gui import color_field, theme
except ImportError:
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from src.gui import color_field, theme

DEFAULT_UI_SETTINGS = theme.DEFAULT_UI_SETTINGS

# モジュールロガー (フォント追加の INFO/WARNING 出力用 / resolve16 §6)
# アプリ実行時は上位で構成済みハンドラへ伝播する。標準ライブラリのみ使用。
_logger = logging.getLogger(__name__)

# ===== 定数定義 =====
# 設定ファイルパス
SETTINGS_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "setting.json")

# ウィンドウ設定
WINDOW_TITLE = "切り抜き自動編集 設定"
# 横幅は項目名ラベル幅 + 入力欄最小幅で決まるため、両者を抑えて全体を縮小する
WINDOW_WIDTH = 300
WINDOW_HEIGHT = 760

# 字幕言語選択肢
SUBTITLE_LANGUAGES = ["ja", "en"]

# 動画ファイルフィルタ
VIDEO_FILE_FILTER = "動画ファイル (*.mp4 *.mov *.avi *.mkv *.flv *.wmv);;すべてのファイル (*)"

# フォントプレビュー (request15 / resolve16 §4.1)
# ドロップダウン項目・現在値・サンプル欄をこのポイントサイズで描画する (画面表示専用)。
# 焼き込みサイズ(subtitle.font_size)とは無関係。
FONT_PREVIEW_POINT_SIZE = 12
FONT_PREVIEW_SAMPLE_TEXT = "あいうえお 永 ABCabc 0123"

# フォント追加 (request15 / resolve16 §4.2)
# 対応フォントファイル拡張子 (libass 焼き込みで解決可能な形式に限定)。
FONT_FILE_EXTENSIONS = (".ttf", ".otf", ".ttc")
FONT_ADD_FILTER = "フォントファイル (*.ttf *.otf *.ttc)"
FONT_ADD_NOTE = "追加できるのは .ttf / .otf / .ttc のみ"

# プレースホルダー文言
# 無音音量閾値(dB)/無音最小継続時間/発話検出閾値(dB) は resolve7 により UI から削除
PLACEHOLDER_FADE_DURATION = "境界フェードの秒数(例: 0.05)"
# テロップ役割別カラーのプレースホルダ (配信者/サブ/コメント)
PLACEHOLDER_COLOR = "#ED1C24"
PLACEHOLDER_SUB_COLOR = "#FFF100"
PLACEHOLDER_COMMENT_COLOR = "#FFFFFF"
PLACEHOLDER_OUTLINE_COLOR = "&H00000000"
PLACEHOLDER_BACK_COLOR = "&H64000000"
PLACEHOLDER_SPEECH_MIN_DURATION = "発話区間検出の最小無音長(例: 0.2)"
PLACEHOLDER_SPEECH_PAD = "表示区間の前後パディング秒(例: 0.1)"

# テロップ絞り込み方式選択肢 (value, 表示テキスト)
SPEECH_GATE_MODE_OPTIONS = [
    ("trim", "前後の無音を切詰め"),
    ("split", "発話小区間ごとに分割"),
]

# テロップ改行方式 (request11): BudouX による文節改行 / 従来の文字数改行
WRAP_ENGINE_OPTIONS = [
    ("budoux", "BudouX(文節)"),
    ("length", "文字数"),
]

# 縁取りスタイル選択肢 (value, 表示テキスト)
BORDER_STYLE_OPTIONS = [
    (1, "縁取り + ドロップシャドウ"),
    (3, "不透明ボックス"),
]

# 音声認識の実行デバイス選択肢 (value, 表示テキスト)
# CUDA 不使用方針 (docs/request/resolve8.md) により GPU(CUDA) 実行は廃止し CPU のみ提供する
WHISPER_DEVICE_OPTIONS = [
    ("cpu", "CPU"),
]

# 計算精度 (compute_type) 選択肢 (value, 表示テキスト)
# CPU 実行のため auto は int8。GPU 専用の float16 は提供しない (resolve8.md)
WHISPER_COMPUTE_TYPE_OPTIONS = [
    ("auto", "自動"),
    ("int8", "int8"),
    ("float32", "float32"),
]

# 表示位置選択肢 (value, 表示テキスト) — ASS のテンキー配置
ALIGNMENT_OPTIONS = [
    (1, "下段（左）"),
    (2, "下段（中央）"),
    (3, "下段（右）"),
    (4, "中段（左）"),
    (5, "中段（中央）"),
    (6, "中段（右）"),
    (7, "上段（左）"),
    (8, "上段（中央）"),
    (9, "上段（右）"),
]

# デフォルト値(setting.jsonが空または欠落キーがある場合に補完)
# 新規セクション (silence_cut/ffmpeg/logging) を追加しつつ既存キーは維持
DEFAULT_SETTINGS = {
    "general": {
        "video_directory": "",
        "opening_video": "",
        "opening_enabled": True,
        "ending_video": "",
        "ending_enabled": True,
        "output_directory": "",
        # 起動時の自動更新チェック ON/OFF (request_autoupdate.md §7)
        "auto_update_check": True,
    },
    "subtitle": {
        "language": "ja",
        "own_subtitle_color": "",
        # テロップ役割別カラー (own_subtitle_color=配信者カラー / sub=サブ / comment=コメント)
        "sub_subtitle_color": "#FFF100",
        "comment_subtitle_color": "#FFFFFF",
        # コメント役割のテロップ先頭に付与するラベル (空文字で無効。焼き込み時のみ付与)
        # ver3 resolve11 C3: アイコン表示へ置き換えたため既定は空文字 (旧値は起動時に移行する)
        "comment_label": "",
        "enabled": True,
        "review_enabled": True,
        "review_on_empty_skip": True,
        "speech_gate_enabled": True,
        "speech_threshold_db": -16,
        "speech_min_duration_sec": 0.2,
        "speech_gate_mode": "trim",
        "speech_pad_sec": 0.1,
        "font_family": "Yu Gothic UI",
        "font_size": 48,
        # 追加フォント格納ディレクトリ (resolve16 §4.5)。SETTINGS_DIR 相対で解決する。
        # 既定 "fonts" = settings/fonts。全実行で共有する。
        "custom_fonts_dir": "fonts",
        # 改行設定 (request11): BudouX による文節改行を既定とし、下限〜上限で折り返す
        "wrap_engine": "budoux",
        "min_line_length": 15,
        "max_line_length": 20,
        # アウトライン色 (配信者=outline_color / sub / comment)。ASS 形式 &HAABBGGRR。
        "outline_color": "&H00000000",
        "sub_outline_color": "&H00000000",
        "comment_outline_color": "&H00000000",
        "outline_width": 3,
        "back_color": "&H64000000",
        "bold": False,
        "italic": False,
        "underline": False,
        "strikeout": False,
        "spacing": 0,
        "angle": 0,
        "border_style": 1,
        "alignment": 2,
        "margin_l": 40,
        "margin_r": 40,
        "margin_v": 60,
        # ── コメント役割だけの配置と装飾 (ver3 resolve11 §7.1 / §7.4)
        # 配置: 中央の左 (ASS an4)。アイコンを左横へ確実に置くため左寄せ (1/4/7) のみ許す。
        "comment_alignment": 4,
        # 左余白 = margin_l(40) + アイコン幅(150) + 間隔(50)。アイコン左端 40px に揃う。
        # アイコンは ver3 resolve14 で 1.5 倍 (100→150)。位置はこの左余白から
        # 逆算される (x = comment_margin_l - gap - size) ため、必ず対で直すこと。
        "comment_margin_l": 240,
        "comment_margin_r": 40,
        "comment_margin_v": 60,
        # アイコン (src/comment_icon.png) の表示
        "comment_icon_enabled": True,
        "comment_icon_path": "",          # 空 = 同梱の src/comment_icon.png
        "comment_icon_size_px": 150,      # ver3 resolve14 で 1.5 倍 (100→150)
        "comment_icon_gap_px": 50,        # アイコン右端と文字左端の間隔
        # enable 式が長くなったときに -filter_script:v へ逃がす閾値 (文字数)
        "comment_icon_filter_script_chars": 8000,
        # 背景ボックス (アイコン + 文字をまとめて囲う角丸の箱 / ver3 resolve11 C13)
        "comment_bg_enabled": True,
        "comment_bg_color": "&H80000000",  # 黒・透明度50% (ASS &HAABBGGRR / AA=80)
        "comment_bg_radius_px": 24,
        "comment_bg_padding_px": 24,
        # 文字幅の推定係数 (libass の実寸は測れないため font_size 比で見積もる)
        # 既定は同梱の既定フォント (Yu Gothic UI) で実測した値。
        # 1 文字あたりの実測値 (font_size 比 / フォントサイズ48で計測):
        #   Yu Gothic UI  全角 0.55(かな)〜0.75(漢字) / 半角 0.33〜0.41
        #   Yu Gothic     全角 0.75〜0.77           / 半角 0.35〜0.48
        #   Meiryo        全角 0.65〜0.67           / 半角 0.32〜0.42
        #   MS Gothic     全角 0.97〜0.99           / 半角 0.48〜0.50 ← 等幅なので要調整
        # 背景が文字より狭くなる (文字がはみ出す) 場合はこの値を上げる。
        # MS ゴシック等の等幅フォントを使うときは 1.0 / 0.5 にする。
        "comment_bg_char_width_full": 0.78,
        "comment_bg_char_width_half": 0.45,
        "comment_bg_line_height_ratio": 1.2,
        # 重ね順 (背景 < 本文)。ASS の Layer は数値が大きいほど前面。
        "comment_bg_layer": 0,
        "comment_text_layer": 1,
        "engine": "whisper",
        "whisper_model": "large-v3",
        "whisper_device": "cpu",
        "whisper_compute_type": "int8",
        "whisper_beam_size": 8,
        "remove_fillers": False,
        # ハルシネーション抑制 (対策A / docs/error/20260626/resolve.md)
        "whisper_vad_filter": True,
        "whisper_vad_min_silence_ms": 500,
        "whisper_condition_on_previous_text": False,
        "whisper_no_speech_threshold": 0.6,
        # テロップ表示タイミング整形 (docs/error/20260627/resolve.md)
        "word_timestamps": True,
        "display_max_hold_sec": 2.0,
        "display_min_duration_sec": 0.5,
    },
    "silence_cut": {
        # 無音カット ON/OFF (resolve12)。既定 True で従来挙動を維持する。
        "enabled": True,
        # カット方式 (ver3 resolve.md §7.1)。
        #   "edit_points" = 実カットせず編集点(JSON)として保持し Timeline で扱う (新既定)
        #   "physical"    = 従来どおり動画を実際にカットする
        # timeline.enabled=false のときは値に関わらず physical として扱う。
        "mode": "edit_points",
        "noise_threshold_db": -30,
        "min_silence_duration_sec": 0.6,
        "fade_enabled": False,
        "fade_duration_sec": 0.05,
        "extract_mode": "seek",
    },
    # 音量解析 (resolve7) — 無音カット前の解析と単一カット閾値を管理する
    # UI には項目を出さず setting.json で管理する。確定値は last_cut_db に保存される。
    "volume_analysis": {
        "enabled": True,
        "base_noise_db": -30,
        "base_min_silence_sec": 0.3,
        "min_region_sec": 0.8,
        "max_regions": 100,
        "metric": "mean",
        "cut_min_silence_sec": 0.6,
        "last_cut_db": -28,
    },
    # ラウドネス正規化 (resolve22) — YouTube 向けに音声レベルを揃える先頭工程。
    # UI には項目を出さず setting.json で管理する (§10-6)。
    "loudness": {
        "enabled": True,          # 正規化 ON/OFF (false で従来挙動)
        "target_i": -14.0,        # Integrated 目標 (LUFS) = YouTube 推奨
        "target_tp": -1.0,        # True Peak 目標 (dBTP)
        "target_lra": 11.0,       # Loudness Range 目標 (LU)
        "two_pass": True,         # true=2パス測定+線形適用 (精度優先) / false=1パス dynamic
        "audio_bitrate": "192k",  # 適用パスの音声ビットレート
    },
    # 字幕編集画面の動画プレビュー (resolve23) — 選択行の時刻から segment_sec 秒を
    # 字幕焼き込み付きの低解像度 mp4 として生成し QtMultimedia で再生する。
    # UI には項目を出さず setting.json で管理する。enabled=false で従来 UI のまま。
    "preview": {
        "enabled": True,
        "segment_sec": 20,      # 1回のプレビュー生成区間長 (秒)
        "width": 640,           # プレビュー生成解像度 (幅 px。高さはアスペクト維持)
        "preset": "ultrafast",  # プレビュー用 x264 preset (速度優先)
        "crf": 28,              # プレビュー用品質 (大=軽い)
        "audio_enabled": True,  # プレビュー音声の既定 (ミュートボタンで切替可)
    },
    # Timeline 型クリップ編集画面 (ver3 / docs/request/ver3/resolve.md §9)
    # 上=プレビュー / 下=Timeline の新編集画面と、編集点方式の無音カットを制御する。
    # enabled=false で従来 UI (SubtitleEditorDialog) ・従来フローへ完全に戻る。
    # UI には項目を出さず setting.json で管理する。
    "timeline": {
        "enabled": True,
        "project_dir": "",                     # 空 = general.output_directory
        "project_suffix": ".timeline.json",
        # 編集画面の自動保存の間隔 (秒)。0=無効 (既定)。
        # 有効にすると <プロジェクト>.autosave.json へ書き、次に開くとき
        # 本体より新しければ「復元しますか」を出す (ver3 resolve7 §5.9)。
        "autosave_sec": 0,
        "keep_project_file": True,             # false なら「決定」後に削除する
        # 設定の OP/ED を Timeline へ自動配置するか (§6.9)。
        # false でも D&D による手動追加は可能。配置条件は concat_processor と同一。
        "opening_ending": {
            "auto_place": True,
        },
        # オーバーレイ (画像・動画) の大きさ (ver3 resolve7 §5.2/§5.3)
        "overlay": {
            # 拡大率の下限・上限 (キャンバス幅に対する比率)
            "min_scale": 0.02,
            "max_scale": 4.0,
            # プレビュー上の四隅ハンドルの一辺 (画面ピクセル。ビューの拡大率に依らず一定)
            "resize_handle_px": 10,
            # インスペクタの「大きさ」欄の刻み (%)
            "scale_step_percent": 1.0,
            # D&D で置いた直後の大きさ
            #   "fit_width" = キャンバス幅いっぱい (現行の挙動 / 既定)
            #   "native"    = 素材のピクセル数どおり (キャンバスより大きければ収める)
            "default_scale_mode": "fit_width",
        },
        # プロジェクトの保存・再編集 (ver3 resolve7 §5.7〜§5.11)
        "project": {
            # 編集画面に保存ボタン・Ctrl+S を出す (false で従来どおり保存操作なし)
            "save_button": True,
            # 未保存のまま閉じようとしたら確認する
            "confirm_on_close": True,
            # 保存時に本編素材 (正規化後の中間ファイル) をプロジェクトの隣へ複製する。
            # true にすると開くのが速くなる代わりに動画 1 本ぶんのディスクを使う。
            "keep_media": False,
            # 複製先フォルダの接尾辞 (<プロジェクト名> + これ)
            "media_dir_suffix": ".media",
            # 素材が見つからないときの方針
            #   "renormalize" = 元動画からラウドネス正規化をやり直す (既定 / 保存時と同じ音量)
            #   "use_source"  = 元動画をそのまま使う (速いが音量が正規化前になる)
            "missing_media_policy": "renormalize",
            # 自動保存ファイルの接尾辞 (timeline.autosave_sec > 0 のときに使う)
            "autosave_suffix": ".autosave.json",
            # main_window の「編集の続き」に出す履歴
            "recent_limit": 10,
            "recent": [],
            # アーカイブ切り抜き用プロジェクトの既定名に挟む識別子 (ver3 resolve9 §3-5)。
            # 同じ VOD からクリップ用とアーカイブ用を作っても名前が衝突しないようにする。
            "archive_suffix": ".archive",
            # アーカイブ切り抜き用の「編集の続き」履歴 (クリップ用の recent と分ける)
            "recent_archive": [],
            # 保存時に正規化済みの音声だけを <プロジェクト名>.media/ へ残す
            # (ver3 resolve9 §3-1 案D)。ラウドネス正規化は映像を -c:v copy で通すため、
            # 音声さえ残しておけば開くときは「切り出し + 多重化」のコピー2回で復元できる。
            # false にすると開くときにラウドネス正規化をやり直すことになる (数分)。
            "keep_audio": True,
            # サイドカー音声の尺が保存値とどれだけズレたら使わないか (秒)
            "sidecar_tolerance_sec": 0.5,
            # 一覧に削除ボタンを出す
            "delete_button": True,
            # 削除をゴミ箱経由にする (false で完全削除 / ver3 resolve9 §3-7)
            "delete_to_trash": True,
            # 削除時に複製素材フォルダ <プロジェクト名>.media/ も消す
            "delete_media_dir": True,
            # 一覧にリネームボタンを出す
            "rename_enabled": True,
            # プロジェクト一覧画面 (ver3 resolve9 §5.12)
            "library": {
                "enabled": True,
                "window_width": 980,
                "window_height": 620,
                # 走査へ加えるフォルダ (既定は project_dir / general.output_directory のみ)
                "extra_dirs": [],
                # 走査を再帰にするか (既定は直下のみ)
                "scan_recursive": False,
                # 一覧に出す上限 (超えた分は更新日時の古いものから落とす)
                "max_items": 100,
                # 初期の並び ("updated_desc" / "name_asc" / "size_desc")
                "sort": "updated_desc",
                # V1 の帯に並べるコマ数 (1 で単一サムネイル)
                "thumbnail_count": 3,
                # 1 コマの幅 (高さはアスペクト比を保つ)
                "thumbnail_width_px": 160,
                # サムネイルのキャッシュ置き場 (相対指定は src/settings 基準)
                "thumbnail_dir": "project_thumbs",
                # キャッシュを保持するプロジェクト数の上限
                "thumbnail_cache_limit": 200,
            },
        },
        "min_clip_sec": 0.05,                  # これ未満へはトリムできない
        # Timeline の右クリック「字幕追加」で置く字幕の既定の尺 (秒)。
        # 次の字幕まで入らない場合はその手前まで縮めて置く。
        "default_subtitle_sec": 2.0,
        # 役割別の字幕トラック (ver3 resolve11 §7.3)。
        # 役割を「コメント」にした字幕を専用トラックへ移し、通常字幕と同時に出せるようにする。
        # 字幕トラックは 1 本の中では重ねられないため、別トラックにするのが唯一の方法。
        "subtitle_tracks": {
            # false で従来どおり字幕トラック 1 本の運用へ戻す
            "role_track_enabled": True,
            "comment_track_id": "S2",
            "comment_track_name": "Comment",
            "comment_track_index": 2,
            # false なら役割だけ変えてトラックは動かさない
            "auto_move_on_role_change": True,
        },
        # Delete キー単独の割り当て (ver3 resolve2 R7)。true=リップル削除 (既定) /
        # false=空白を残す。Shift+Delete は常にもう一方。
        # 両方とも右クリックメニューからも実行できる。
        "ripple_delete": True,
        # 旧既定 (false) からの移行を 1 度だけ行うための記録 (resolve2 §9)。
        # 移行後にユーザーが false へ戻した場合、再び書き換えないようにする。
        "ripple_delete_migrated": False,
        # リップルで一緒に詰める対象 (resolve2 R8)。
        # "all" = 全トラック (字幕・オーバーレイも詰める / 既定)
        # "same" = 操作したトラックのみ (旧挙動。切り戻し用)
        # 音声トラックは値に関わらず対象外 (V1 からの導出で自動的に追従するため)。
        "ripple_sync_tracks": "all",
        "snap_enabled": True,
        # 再生ヘッドを編集点へ吸着させる (resolve2 R1)。Alt 押下中は一時無効。
        "snap_playhead": True,
        "snap_threshold_px": 8,
        "default_zoom_px_per_sec": 40,
        "zoom_min_px_per_sec": 2,
        "zoom_max_px_per_sec": 400,
        "ui": {
            "split_ratio": 0.55,               # 上(プレビュー):下(Timeline) の初期比
            "track_height_px": 56,
            "subtitle_track_height_px": 40,
            "header_width_px": 88,
            "trim_handle_px": 6,
            "ruler_min_label_px": 60,
            "window_width": 1280,
            "window_height": 820,
            # Timeline 左下のズームボタン (－ ＋) の幅 (ver3 resolve4 E4)。
            # QSS の左右余白ぶん、記号が見切れない幅が要る。
            "zoom_button_width_px": 40,
            # クリップのアウトラインの太さ (ver3 resolve4 E5)。
            # 太さは矩形の内側へ描くため、クリップの占有幅は変わらない。
            "clip_outline_width_px": 2,
            "clip_outline_selected_width_px": 3,
            # アーカイブ用画面 (ver3 resolve5 §7)。採点グラフのドック配置と
            # クリップバーの寸法。dock_state は利用者が動かした結果の記憶。
            "archive_graph_dock_area": "top",       # "top"/"left"/"right"/"bottom"
            "archive_graph_dock_height_px": 220,
            "archive_clip_selector_width_px": 260,
            "archive_dock_state": "",
            # 字幕インスペクタの色見本ボタンの幅 (ver3 resolve6 §7)
            "subtitle_color_swatch_width_px": 28,
        },
        "preview": {
            "backend": "pyav",                 # "pyav" (既定) | "ffmpeg" (強制フォールバック)
            "width": 960,                      # プレビュー描画の基準幅
            "update_debounce_ms": 60,
            "cache_frames": 8,
            "high_quality_button": True,       # 「高精度プレビュー」ボタンの表示
            "audio_enabled": True,             # 起動時のミュート状態 (false=ミュート)
            "audio_volume": 0.8,               # 0.0〜1.0
            "audio_chunk_sec": 30,             # 1 回に生成する音声チャンク長 (秒)
            "audio_prefetch_sec": 8,           # 残りがこれを切ったら次チャンクを先読み
            "play_fps": 15,                    # 再生中の映像更新レート上限
            # 倍速再生 / 倍速逆再生 (ver3 resolve2 R10・R11)
            "playback_rate": 2.0,              # Q / E の倍率 (「倍速」= 2.0)
            "reverse_play_fps": 8,             # 倍速逆再生時の映像更新レート上限
            # トランスポートのボタン幅 (ver3 resolve4 E2)。
            # QSS の左右余白ぶん、記号が見切れない幅が要る。
            "transport_button_width_px": 40,       # ⏮ ▶ ⏭ 🔊 (1 記号)
            "transport_wide_button_width_px": 52,  # ◀◀ ▶▶ (2 記号ぶん広い)
            # 「高精度プレビュー」ボタンの右側の余白 (ver3 resolve4 E3)
            "transport_row_right_margin_px": 8,
            # ── 再生の応答改善 (ver3 resolve6 §7) ──────────────────
            # 直前に読んだ位置からこの秒数以内の前進なら seek せず読み進める。
            # 0 にすると常に seek する = 従来動作へ戻る。
            "sequential_decode_sec": 2.0,
            # 再生中だけ映像をこの幅へ縮小して取得する (0 = 原寸のまま)。
            # 停止・スクラブ中は原寸に戻るため、粗くなるのは再生中だけ。
            # 素材の幅がこれ以下なら縮小しない。出力動画の画質には影響しない。
            "playback_width": 960,
            # 再生中のフレーム間引き "none" | "nonref" | "bidir"
            "playback_skip_frame": "none",
            # PyAV が無い環境で、再生中だけ常駐 ffmpeg のパイプ読み出しを使う
            "frame_pipe_fallback": True,
            # 音声チャンクの作り方。"single_process"=断片ごとに -ss 入力を並べて
            # 1 回の ffmpeg で作る (既定) / "per_piece"=断片ごとに起動する従来方式
            "audio_build_mode": "single_process",
            # ▶ を押したとき最初に作る短いチャンクの長さ (秒)
            "audio_startup_chunk_sec": 6,
            # 保持しておくチャンクの本数 (少し戻して再生し直すときの作り直しを防ぐ)
            "audio_chunk_cache": 3,
            # プレビュー音声の品質 (編集用の確認音のため軽くしてある)。
            # 0 / 空文字なら ffmpeg セクションの値 (48000 / aac) を使う。
            "audio_sample_rate": 24000,
            "audio_channels": 1,
            "audio_codec": "pcm_s16le",
            # 画面を開いた直後に先頭の起動用チャンクを先読みする
            "audio_prefetch_on_open": True,
            # ── スクラブ (再生ヘッドドラッグ中の音声 / ver3 resolve7 §3-7・§5.13) ──
            # false で従来どおり完全に無音のドラッグに戻る
            "scrub_audio_enabled": True,
            # 1 粒の間隔 (ms)。短いほど追従が細かくなるが setPosition の回数が増える
            "scrub_interval_ms": 60,
            # 手元に音が無いときに作る短いチャンクの長さ (秒)
            "scrub_chunk_sec": 4.0,
            # 動きが止まってから黙るまでの時間 (ms)
            "scrub_hold_ms": 120,
            # これ未満の移動は「止まっている」とみなす (秒)
            "scrub_min_delta_sec": 0.01,
            # 通常再生に対する音量比 (スクラブ音は耳に付きやすいので下げられるようにする)
            "scrub_volume": 0.8,
            # "grain" = 等倍で粒を撒く (既定 / 音程が変わらず逆方向でも鳴る)
            # "match" = ドラッグ速度に再生レートを合わせる (音程が変わる / 環境依存)
            "scrub_rate_mode": "grain",
            # スクラブ中も再生中と同じ軽い取得 (縮小・前進デコード) を使う
            "scrub_low_quality_frames": True,
        },
        # キー割り当て (ver3 resolve2 §6)。空文字で無効。
        # S には何も割り当てないため項目自体を置かない。
        "shortcuts": {
            "split": "W",
            "ripple_trim_before": "A",
            "ripple_trim_after": "D",
            "play_pause": "Space",
            "play_fast_forward": "E",
            "play_fast_backward": "Q",
            "split_alt": "Ctrl+B",
            "delete": "Delete",
            "delete_alternate": "Shift+Delete",
            # 選択中のノードを削除するだけ (後続を詰めない / ver3 resolve2 R12)
            "delete_plain": "Backspace",
            "undo": "Ctrl+Z",
            "redo": "Ctrl+Y",
            "redo_alt": "Ctrl+Shift+Z",
            "step_backward": "Left",
            "step_forward": "Right",
            "go_start": "Home",
            "go_end": "End",
            "zoom_in": "Ctrl++",
            "zoom_in_alt": "Ctrl+=",
            "zoom_out": "Ctrl+-",
            # プロジェクトの保存 (ver3 resolve7 §5.7)
            "save": "Ctrl+S",
            "save_as": "Ctrl+Shift+S",
            # ノードのコピー＆ペースト (ver3 resolve10 §7)。
            # 貼り付けは 1 種類だけ (Ctrl+Shift+V は割り当てない)。
            "copy": "Ctrl+C",
            "paste": "Ctrl+V",
        },
        # 貼り付けの方針 (ver3 resolve10 §7)
        "paste": {
            # 右へずらす範囲
            #   "base_syncs_all" (既定) … V1 へ貼るときだけ全トラックを同量ずらす
            #                             (字幕が V1 と一緒に動く)。V2 以降・字幕へ
            #                             貼るときは干渉したトラックだけ
            #   "track"                … 常に干渉したトラックだけ
            #   "all"                  … 常に全トラック
            "ripple_scope": "base_syncs_all",
            # 貼り付け位置を跨ぐクリップの扱い ("split" = 分割 / "shift_whole" = 丸ごと移動)
            "insert_policy": "split",
            # アーカイブ用 V1 の archive_clip_index ("inherit" = 直前から引き継ぐ / "keep")
            "archive_index_policy": "inherit",
            # 貼り付け後に再生ヘッドを貼った範囲の終端へ送る (連続貼り付け用)
            "move_playhead_to_end": True,
            # 貼り付けたノードを選択状態にする
            "select_pasted": True,
        },
        "media": {
            "video_extensions": [".mp4", ".mov", ".avi", ".mkv", ".flv", ".wmv"],
            "image_extensions": [".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"],
            "default_image_duration_sec": 5.0,
            "max_video_tracks": 8,
        },
        "render": {
            # "black"=空白を黒+無音として出力 (既定) / "close"=空白を詰める
            "gap_policy": "black",
            "extract_mode": "seek",
            "concat_reencode": False,
            "overlay_enabled": True,           # false でオーバーレイ合成をスキップ (切り分け用)
            # 連結前の中間パートの音声コーデック (20260812 resolve3 §7)。
            # AAC のエンコーダ遅延 (1024 サンプル = 21.3ms) が部品ごとに尺へ乗り、
            # concat 連結で部品数ぶん累積してテロップがずれるため既定は非圧縮。
            # "aac" にすると従来動作へ戻る。
            "intermediate_audio_codec": "pcm_s16le",
            # 中間パートの容器。pcm_s16le は mp4 に格納できないため .mov を使う。
            "intermediate_container": ".mov",
            # 部品の尺をフレーム境界へタイル化する (絶対フレーム番号の差で決める)。
            # false にすると従来どおりモデルの秒をそのまま -t へ渡す。
            "frame_quantize": True,
        },
    },
    "ffmpeg": {
        # 同梱 FFmpeg を PATH 非依存で参照する相対パス (error 20260708)。
        # 非凍結(開発)実行では ffmpeg_runner._resolve_exe が PATH の ffmpeg.exe を解決する。
        "executable": "ffmpeg/ffmpeg.exe",
        "ffprobe_executable": "ffmpeg/ffprobe.exe",
        "video_codec": "libx264",
        "audio_codec": "aac",
        "preset": "medium",
        "crf": 20,
        "output_width": 1920,
        "output_height": 1080,
        "output_fps": 60,
        "audio_sample_rate": 48000,
    },
    # 縦動画(YouTube Shorts / TikTok)対応 (request14 / resolve14)
    # 縦検出時のみ有効。フォント/改行/配置/余白は横(subtitle)設定を上書きする。
    "vertical": {
        "enabled": True,
        "output_width": 1080,
        "output_height": 1920,
        "font_size": 90,
        "min_line_length": 10,
        "max_line_length": 15,
        "alignment": 2,
        "margin_l": 40,
        "margin_r": 40,
        "margin_v": 320,
        # コメント役割の縦用上書き (ver3 resolve11 §7.2)。
        # アイコンは 100x100 (ver3 resolve14 で 2 倍 / 50→100)。
        # 左余白 = 40 + 100 + 50 = 190 とし、横と同じくアイコン左端 40px に揃える。
        # 位置は左余白から逆算されるため、大きさと左余白は必ず対で直すこと。
        "comment_alignment": 4,
        "comment_margin_l": 190,
        "comment_margin_r": 40,
        "comment_margin_v": 320,
        "comment_icon_size_px": 100,
        "comment_icon_gap_px": 50,
    },
    "logging": {
        "log_dir": "logs",
        "level": "INFO",
    },
    # アーカイブ切り抜き (request17 / flow17)。R1: 方式A中核採点(音量/無音/コメント)
    # → TOP5 → 切り抜き+字幕焼き込み。Twitch取得・任意スコアラ・方式Bは後続リリース。
    "archive": {
        "enabled": True,
        "download": {
            # R3: ローカル指定に加え Twitch VOD を twitch-dl で自動取得できる (flow17 R3)。
            "downloader": "local",       # "local" | "twitch-dl" (UI の入力ソース選択で切替)
            "twitch_dl_path": "twitch-dl",  # twitch-dl CLI (同梱 or PATH。ffmpeg と同方式で解決)
            # 希望する取得画質。"source"=無変換(chunked)。VOD によっては存在しない
            # (トランスコードのみの VOD がある / error 20260810)。
            "vod_format": "source",
            # 希望画質が無いとき、利用できる最高画質へ自動で落とす (error 20260810)
            "vod_quality_fallback": True,
            "chat_source": "twitch-dl",  # コメントは twitch-dl chat json に一本化
            "work_dir": "archive_work",  # 取得物の作業ディレクトリ (settings 相対 or 絶対)
        },
        # Twitch ログイン (OAuth インプリシットフロー / resolve17 §4.3.0)。
        # client_id は dev.twitch.tv で登録したアプリの値を設定する (Client-Secret は不要)。
        # リダイレクトURL には http://localhost:<redirect_port> を登録すること。
        "auth": {
            "client_id": "mcu1dwig8t6bmv87xp6s08y2s757r5",           # OAuth 用 Client-ID (未設定ならログイン不可)
            "client_secret": "",       # 未使用 (インプリシットフローでは不要。後方互換で保持)
            "redirect_port": 3737,     # ローカル redirect 受信ポート
            "owner_only": True,        # 自分が所有する VOD のみ許可 (resolve17 §8-1)
        },
        "scoring": {
            "method": "A",
            "top_n": 10,          # 切り抜き候補の上限件数 (TOP10)
            # 方式A: 3分窓/45秒スライド。5分窓ではスコープが広すぎて別トークテーマを
            # 巻き込むため、より狭い窓に変更 (window_sec/slide_sec=180/45)。
            "window_sec": 180,          # 方式A: 3分窓
            "slide_sec": 45,            # 方式A: 45秒スライド (= セル幅)
            "clip_pad_sec": 0,          # 採用区間の前後パディング
            "loud_percentile": 0.8,     # 大声セル判定 (max_db の上位分位)
            "silence_ratio_threshold": 0.6,  # 無音セル判定 (無音率がこれ以上)
            "method_a": {
                "weights": {"emotion": 0.55, "comment": 0.45},
                # R1 は音声のみのため loud/long_silence を使用 (他はML必要=後続)
                "emotion_points": {
                    "big_laugh": 15, "loud": 10, "surprise": 8,
                    "cry": 15, "anger": 12, "long_silence": -8,
                },
                "comment": {"w_point_per_char": 1, "rate_spike_bonus": 10},
            },
        },
        # ストリームマーカー (ver3 resolve16)。Twitch VOD に打たれたマーカーの
        # 前後を「必ず残すセクション」にする。採点式そのものには影響しない
        # (採点結果と合流させ、重なりは既存のマージ規約で 1 つに統合する)。
        # 取得には Twitch ログインの user:read:broadcast スコープが要るため、
        # 旧バージョンでログイン済みの場合は再ログインが必要 (resolve16 §3-5)。
        "markers": {
            "enabled": True,            # false でマーカーを取得も反映もしない
            "before_sec": 120,          # マーカー位置の前に必ず含める秒数 (要望: 2分)
            "after_sec": 120,           # マーカー位置の後に必ず含める秒数 (要望: 2分)
        },
        # 切り抜き出力ファイル名の接頭辞 (flow17 R1)
        "output": {
            "clip_prefix": "archive",
        },
        # セクション追加 (ver3 resolve13)。Timeline 編集画面から元動画の区間を
        # 指定してセクションを足す。採点方式には影響しない。
        "section_add": {
            "enabled": True,            # false で「セクション追加...」ボタンを出さない
            # 追加ダイアログの既定尺 (秒)。未指定なら scoring.window_sec を使う
            "default_length_sec": 180,
            "min_length_sec": 1.0,      # これより短い区間は追加させない
            "merge_on_overlap": True,   # 既存セクションと重なったら 1 つへ統合する
        },
        # クリップ処理 (request18): 各クリップを現行クリップ用と同じ工程に通す。
        "clip_pipeline": {
            "silence_cut": True,       # 各クリップで無音カットを行う
            "subtitle_review": True,   # 各クリップでテロップ編集画面を出す
            "volume_dialog": False,    # 音量/カット閾値ダイアログは出さない
        },
        # 結合 (request18): TOP5 を結合して 1 本にし、OP/ED を1回だけ付ける。
        "combine": {
            "enabled": True,
            "opening_ending": True,    # 結合1本に OP/ED を付ける (実体は general フラグ+素材有無)
            "keep_individual": False,  # 個別クリップも残すか (既定: 残さない)
            "combined_suffix": "combined",
        },
        # テーマ・イントロカード (resolve19): 字幕一覧画面でテーマを入力した
        # クリップにのみ発火。開始前 buffer_sec を復元しブラー+黒帯+中央テーマを重ね、
        # 本編全長には左上へ小さくテーマを焼く。未入力クリップは現行どおり。
        "intro_card": {
            "enabled": True,            # 機能全体のスイッチ (false でテーマ欄も出さない)
            "buffer_sec": 1.5,          # 開始前に戻すバッファ秒数 (要望: 1.5)
            "blur_sigma": 18,           # 画面全体ブラーの強さ (gblur sigma)
            "margin_top_px": 300,       # 黒帯の上余白 (要望: 300)
            "margin_bottom_px": 300,    # 黒帯の下余白 (要望: 300)
            "box_color": "black",       # 黒色要素の色
            "box_opacity": 1.0,         # 黒色要素の不透明度 (1.0=完全不透明)
            "title_font_family": "",    # 中央テーマのフォント (空=subtitle.font_family)
            "title_font_size": 112,     # 中央テーマの文字サイズ (1080p 目安)
            "title_color": "#FFFFFF",   # 中央テーマの文字色
            "tag_font_family": "",      # 左上タグのフォント (空=subtitle.font_family)
            "tag_font_size": 40,        # 左上タグの文字サイズ
            "tag_color": "#FFFFFF",     # 左上タグの文字色
            "tag_bg_opacity": 0.45,     # 左上タグ背景の不透明度 (可読性用・0で無効)
            "tag_margin_l": 40,         # 左上タグの左マージン
            "tag_margin_v": 30,         # 左上タグの上マージン
            "mute_intro": False,        # イントロ 1.5 秒の音声を無音化するか
        },
    },
    # DaVinci Resolve プロジェクトファイル出力 (resolve20 §6)
    # 字幕一覧画面 (クリップ用) と結果画面 (アーカイブ用) の出力ボタンから使用する。
    # UI には項目を出さず setting.json で管理する (追加のみ・既存キーは不変)。
    "export": {
        "resolve": {
            "enabled": True,               # 出力ボタンの表示 (false でボタン非表示)
            "format": "fcpxml",            # "fcpxml" のみ実装 ("edl_srt"/"otio" は将来拡張)
            "fcpxml_version": "1.9",       # 生成する FCPXML のスキーマ版
            "event_name": "Stretheus",     # FCPXML の event 名
            "title_effect_uid": "",        # Text+ 用 effect UID (空= Basic Title 相当)
            "clip_edit_points": "context",  # クリップ用の編集点取得 ("context"=保持値)
            "include_intro_card": True,    # テーマ/タグ文字を出力に含めるか
            "clip_prefix": "clip",         # クリップ用出力の接頭辞 (空文字で接頭辞なし)
            "title": {                     # 位置変換の係数 (ハードコード回避)
                "coord_space": "normalized",  # "normalized" | "pixel"
                "origin": "center",           # Resolve 座標の原点
            },
            "subtitle": {                  # 字幕の出力方式 (resolve21 §6)
                "mode": "caption",         # "caption"=字幕トラック / "title"=Text+ / "both"
                "caption_format": "ITT",   # caption の書式 ("ITT" のみ実装)
                "language": "ja",          # caption ロールの言語コード
                "srt_sidecar": True,       # SRT サイドカーを併せて出力する
            },
        },
    },
    # Stretheus API の接続先 (StretheusAPI resolve2 §6.2)。
    # Client ID / RedirectUri / scope はここに持たない。API の
    # GET /api/auth/twitch/authorize-params から取得する (§2.4)。
    # 開発時は dev (https://stretheusapi-dev.azurewebsites.net) や
    # localhost へ base_url を差し替える。
    "api": {
        "base_url": "https://stretheusapi.azurewebsites.net",
        "timeout_sec": 15,
    },
    # 画面の見た目 (ガラスモーフィズム / resolve3 §6)。
    # 既定の実体は src/gui/theme.py が持つ (デザイントークンの唯一の出どころ)。
    # ui.theme = "system" で従来の Qt 既定へ完全に戻せる。
    "ui": copy.deepcopy(DEFAULT_UI_SETTINGS),
}

# スタイル定数
TITLE_FONT_SIZE = 14
# 注意書きラベルの文字サイズ (旧 font-size:11px 直書きの置き換え / resolve3 §5.3)
FONT_NOTE_POINT_SIZE = 8
# 項目名ラベルの最小幅 (長い文言は自動で広がるため最小値のみ指定)
COLUMN_LABEL_WIDTH = 100
# 入力欄の最小幅 (横幅全体を支配するため小さめに設定して画面を縮小する)
INPUT_FIELD_MIN_WIDTH = 160
# カラー欄の最小幅 (ver3 resolve4 §5.6-1)。役割別の 3 色を横 1 行へ並べるため、
# INPUT_FIELD_MIN_WIDTH より狭くする。書式が違うと必要幅も違うためグループごとに持つ。
COLOR_FIELD_MIN_WIDTH = 104          # HTML #RRGGBB (7 文字 / 実測 102px)
OUTLINE_COLOR_FIELD_MIN_WIDTH = 140  # ASS &HAABBGGRR (10 文字 / 実測 138px)
# 役割別カラーの並び順と表示名 (テロップ/アウトラインで共通)
COLOR_ROLE_LABELS = ("配信者", "サブ", "コメント")


# setting.json を読み込み JSON(dict) を返す。読めない/壊れている場合は None。
# UTF-8(BOM許容) を最優先し、失敗時は CP932 で回復を試みる
# (旧インストーラが ANSI 保存した破損ファイルの救済 / error 20260707)。
def _read_settings_file():
    for enc in ("utf-8-sig", "cp932"):
        try:
            with open(SETTINGS_FILE, "r", encoding=enc) as f:
                content = f.read().strip()
        except (OSError, UnicodeDecodeError):
            continue
        if not content:
            return None
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            continue
    return None


# 設定ファイルを読み込む(空またはエラーの場合はデフォルトを返す)
# persist_new_keys=True の場合、新バージョンで増えた新規キーを補完した結果を
# 一度だけ setting.json へ書き戻す (request_autoupdate.md §8.2)。
# 既存値は _merge_with_defaults がユーザー値優先で保持するため上書きにはならない。
def load_settings(persist_new_keys=True):
    if not os.path.exists(SETTINGS_FILE):
        return DEFAULT_SETTINGS.copy()

    data = _read_settings_file()
    if data is None:
        return DEFAULT_SETTINGS.copy()

    merged = _merge_with_defaults(data)
    # 旧版から引き継いだレガシー値を現行既定へ正規化する (error 20260709)
    migrated = _normalize_legacy_values(merged)
    # 新規キーが増えた or レガシー値を修正したら 一度だけ永続化する
    if persist_new_keys and (migrated or _has_new_keys(data, merged)):
        try:
            save_settings(merged)
        except OSError:
            pass  # 保存失敗しても実行は継続 (メモリ上は補完済み)
    return merged


# 既知のレガシー FFmpeg 実行ファイル値 (20260708 の既定変更前の裸名)。
# 小文字で比較する。
_LEGACY_FFMPEG_EXE_VALUES = {"ffmpeg", "ffmpeg.exe", "ffprobe", "ffprobe.exe"}


# 旧バージョンから引き継いだレガシー設定値を現行の既定値へ正規化する (error 20260709)。
# 変更があれば True を返す。
# 背景: 20260708 の修正で ffmpeg.executable / ffprobe_executable の既定を裸名 "ffmpeg"/"ffprobe"
#   から同梱相対パス "ffmpeg/ffmpeg.exe"/"ffmpeg/ffprobe.exe" へ変更した。しかしアップデート時は
#   インストーラが旧 setting.json を復元し、_merge_with_defaults は既存値を温存するため、裸名の
#   まま残り凍結配布物の同梱 FFmpeg を解決できず「FFmpeg 実行ファイルが見つかりません: ffmpeg」
#   となる。既知のレガシー値に限って既定へ置換し、次回起動で自己修復させる。
def _normalize_legacy_values(merged):
    changed = False
    ffmpeg = merged.get("ffmpeg")
    if isinstance(ffmpeg, dict):
        for key in ("executable", "ffprobe_executable"):
            value = ffmpeg.get(key)
            if isinstance(value, str) and value.strip().lower() in _LEGACY_FFMPEG_EXE_VALUES:
                default = DEFAULT_SETTINGS["ffmpeg"][key]
                if value != default:
                    ffmpeg[key] = default
                    changed = True
    if _migrate_comment_label(merged):
        changed = True
    if _migrate_comment_icon_size(merged):
        changed = True
    if _fill_twitch_client_id(merged):
        changed = True
    if _fill_timeline_nested_defaults(merged):
        changed = True
    if _fill_ui_nested_defaults(merged):
        changed = True
    if _migrate_ripple_delete(merged):
        changed = True
    return changed


# コメント役割の先頭ラベル「コメント：」を撤去する (ver3 resolve11 C3 / §8-1)
# 背景: ラベルはアイコン表示へ置き換えたため既定を空文字にしたが、_merge_with_defaults は
#   既存のユーザー値を温存するため、旧 setting.json では「コメント：」が残り続ける。
#   FFmpeg 実行ファイルのレガシー正規化と同じ扱いで、既知の旧既定値のときだけ空へ寄せる。
#   利用者が独自の文言を入れている場合は上書きしない (その文言は尊重する)。
_LEGACY_COMMENT_LABELS = ("コメント：", "コメント:")


def _migrate_comment_label(merged):
    subtitle = merged.get("subtitle")
    if not isinstance(subtitle, dict):
        return False
    value = subtitle.get("comment_label")
    if not isinstance(value, str) or value.strip() not in _LEGACY_COMMENT_LABELS:
        return False
    subtitle["comment_label"] = ""
    _logger.info("コメント役割の先頭ラベルを撤去しました (アイコン表示へ置き換え)")
    return True


# コメントアイコンの拡大 (ver3 resolve14)。
# 背景: アイコンが小さいという要望で既定を横 1.5 倍 / 縦 2 倍にしたが、
#   _merge_with_defaults は既存のユーザー値を温存するため、旧 setting.json では
#   小さいままになる。_migrate_comment_label と同じ扱いで、既知の旧既定値のときだけ寄せる。
# 【重要】comment_icon_size_px と comment_margin_l は対で意味を持つ
#   (左余白 = 40 + アイコン + 間隔 / resolve14 §2.3)。アイコンの位置は
#   x = comment_margin_l - gap - size で逆算されるため、片方だけ寄せると
#   アイコンが画面端へはみ出す。2 つとも旧既定のときだけ 2 つまとめて動かす。
_LEGACY_COMMENT_ICON = {
    "subtitle": {"comment_icon_size_px": 100, "comment_margin_l": 190},
    "vertical": {"comment_icon_size_px": 50, "comment_margin_l": 140},
}


def _migrate_comment_icon_size(merged):
    changed = False
    for section, legacy in _LEGACY_COMMENT_ICON.items():
        target = merged.get(section)
        if not isinstance(target, dict):
            continue
        # 1 つでも独自の値なら触らない (利用者の調整を尊重する)
        if any(target.get(key) != value for key, value in legacy.items()):
            continue
        for key in legacy:
            target[key] = DEFAULT_SETTINGS[section][key]
        changed = True
    if changed:
        _logger.info("コメントアイコンを拡大しました (横 1.5 倍 / 縦 2 倍)")
    return changed


# 空のままの Twitch Client-ID を既定 (アプリが配布する公開値) で補う (error 20260820)
# 背景: Client-ID は長らく配布 setting.json にしか無かった。更新インストールでは
#   インストーラが旧 setting.json を復元し、_merge_with_defaults はキーが在れば
#   既存値 (空文字) を温存するため、新しい既定が永久に届かず
#   「Twitch の Client-ID が未設定です」となる。FFmpeg のレガシー値と同じ扱いで、
#   空のときだけ既定へ寄せて次回起動で自己修復させる。
#   利用者が自前の Client-ID を入れている場合は空ではないため上書きしない。
def _fill_twitch_client_id(merged):
    archive = merged.get("archive")
    if not isinstance(archive, dict):
        return False
    auth = archive.get("auth")
    if not isinstance(auth, dict):
        return False
    default = DEFAULT_SETTINGS["archive"]["auth"]["client_id"]
    if not default:
        return False
    if str(auth.get("client_id", "") or "").strip():
        return False
    auth["client_id"] = default
    _logger.info("Twitch の Client-ID が未設定のため既定値を設定しました")
    return True


# timeline セクションの入れ子 (ui / preview / media / render / shortcuts …) について、
# 欠落キーを既定で補完する (ver3 resolve2 §6)。
# _merge_with_defaults はセクション単位の浅いマージのため、入れ子辞書はユーザー値で
# まるごと置き換わり、新バージョンで増えたキーが setting.json へ現れない。
# ショートカットのように「利用者が編集する前提」の項目が見えないままになると困るため、
# timeline セクションに限って 1 段深く補完する。既存値は上書きしない。
def _fill_timeline_nested_defaults(merged):
    return _fill_nested_defaults(merged, "timeline")


# ui セクションの入れ子 (glass / background) についても欠落キーを既定で補完する
# (ver3 resolve3 §6)。ガラスの不透明度や背景色は利用者が setting.json で
# 調整する前提のため、新バージョンで増えたキーがファイル上に現れる必要がある。
def _fill_ui_nested_defaults(merged):
    return _fill_nested_defaults(merged, "ui")


# 指定セクションの入れ子辞書について、欠落キーを既定で再帰的に補完する。
# _merge_with_defaults はセクション単位の浅いマージのため、入れ子辞書はユーザー値で
# まるごと置き換わり、新バージョンで増えたキーが setting.json へ現れない。
# 既存値は上書きしない。変更があれば True を返す。
def _fill_nested_defaults(merged, section):
    current = merged.get(section)
    defaults = DEFAULT_SETTINGS.get(section, {})
    if not isinstance(current, dict):
        return False
    return _fill_dict_defaults(current, defaults)


# 辞書へ既定値を再帰的に流し込む (既存値は温存する)
def _fill_dict_defaults(current, defaults):
    changed = False
    for key, default_value in defaults.items():
        if not isinstance(default_value, dict):
            continue
        value = current.get(key)
        if not isinstance(value, dict):
            continue
        for sub_key, sub_default in default_value.items():
            if sub_key not in value:
                value[sub_key] = copy.deepcopy(sub_default)
                changed = True
        if _fill_dict_defaults(value, default_value):
            changed = True
    return changed


# Delete キーの既定をリップル削除へ寄せる 1 回きりの移行 (ver3 resolve2 §9 / 回答 Q5)
# 背景: ripple_delete は旧既定 false で既に setting.json へ書き込まれているため、
#   DEFAULT_SETTINGS を true にしただけでは既存ユーザーの動作が変わらない。
#   ここで 1 度だけ true へ寄せる。移行済みフラグを持たせることで、移行後に
#   ユーザーが意図して false へ戻した場合に再び書き換えることはしない。
def _migrate_ripple_delete(merged):
    timeline = merged.get("timeline")
    if not isinstance(timeline, dict):
        return False
    if timeline.get("ripple_delete_migrated"):
        return False
    timeline["ripple_delete_migrated"] = True
    if timeline.get("ripple_delete") is False:
        timeline["ripple_delete"] = True
    return True


# 読み込んだデータに不足項目があればデフォルトで補完する
def _merge_with_defaults(data):
    merged = DEFAULT_SETTINGS.copy()
    for section, defaults in DEFAULT_SETTINGS.items():
        # 入れ子ごと複製する。浅いコピーだと補完先の入れ子辞書が DEFAULT_SETTINGS と
        # 同一実体になり、欠落キー補完 (_fill_nested_defaults) が既定を汚し得るため。
        merged[section] = copy.deepcopy(defaults)
        if section in data and isinstance(data[section], dict):
            for key, value in data[section].items():
                merged[section][key] = value
    return merged


# merged に data へ無い新規キー (セクション or セクション内キー) が1つでもあれば True。
# 既存値の変更は判定に含めない (新規追加のみを永続化トリガとする)。
def _has_new_keys(data, merged):
    for section, values in merged.items():
        if section not in data:
            return True
        if isinstance(values, dict) and isinstance(data.get(section), dict):
            if any(key not in data[section] for key in values):
                return True
    return False


# 設定をsetting.jsonに保存する
def save_settings(settings):
    os.makedirs(SETTINGS_DIR, exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=4)


# 追加フォント格納ディレクトリを解決する (resolve16 §4.5)
# custom_fonts_dir は SETTINGS_DIR 相対 (既定 "fonts" = settings/fonts)。全実行で共有する。
def resolve_fonts_dir(settings):
    subtitle = settings.get("subtitle", {}) if isinstance(settings, dict) else {}
    rel = subtitle.get("custom_fonts_dir", "fonts") or "fonts"
    return os.path.join(SETTINGS_DIR, rel)


# 格納ディレクトリ内の対応フォントを Qt へ登録する (起動時のプレビュー/一覧反映用)
# 焼き込み側(libass)は別プロセスのため fontsdir で別途参照する (resolve16 §4.3)。
def register_fonts_in_dir(fonts_dir):
    if not fonts_dir or not os.path.isdir(fonts_dir):
        return
    for name in sorted(os.listdir(fonts_dir)):
        if os.path.splitext(name)[1].lower() in FONT_FILE_EXTENSIONS:
            QFontDatabase.addApplicationFont(os.path.join(fonts_dir, name))


# 設定画面ウィンドウクラス
class SettingsWindow(QWidget):

    # 初期化処理
    def __init__(self):
        super().__init__()
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
        # ガラスモーフィズムの背景を敷く (resolve3 §3-2)。QSS はアプリ全体へ
        # 適用済みのため、ここでは背景グラデーションを描くだけでよい。
        theme.install_window_background(self)

        # 入力ウィジェット参照を保持
        self.video_dir_edit = None
        self.opening_edit = None
        self.opening_enabled_check = None
        self.ending_edit = None
        self.ending_enabled_check = None
        self.output_dir_edit = None
        # 自動更新チェック ON/OFF (request_autoupdate.md)
        self.auto_update_check_check = None
        # 無音カット関係ウィジェット参照
        # 無音音量閾値(dB)/無音最小継続時間 は resolve7 により UI から削除
        # 無音カット ON/OFF (resolve12)
        self.silence_enabled_check = None
        self.fade_enabled_check = None
        self.fade_duration_edit = None
        self.language_combo = None
        # テロップ役割別カラー入力欄 (配信者=color_edit / サブ / コメント)
        self.color_edit = None
        self.sub_color_edit = None
        self.comment_color_edit = None
        self.subtitle_enabled_check = None
        # 字幕編集画面 (レビュー) ON/OFF ウィジェット参照
        self.review_enabled_check = None
        # 発話検出 (テロップ表示絞り込み) 関係ウィジェット参照
        # 発話検出閾値(dB) は resolve7 により UI から削除
        self.speech_gate_enabled_check = None
        self.speech_min_duration_edit = None
        self.speech_gate_mode_combo = None
        self.speech_pad_edit = None
        # 音声認識 (Whisper) 実行デバイス/計算精度ウィジェット参照
        self.whisper_device_combo = None
        self.whisper_compute_type_combo = None
        # フォント関係ウィジェット参照
        self.font_size_edit = None
        self.font_family_combo = None
        # フォントプレビュー欄 (選択中フォントでサンプル文字を描画 / resolve16 §4.1)
        self.font_preview_label = None
        # 改行設定 (request11): 方式コンボ / 下限・上限文字数
        self.wrap_engine_combo = None
        self.min_line_length_edit = None
        self.max_line_length_edit = None
        # アウトライン色 (配信者=outline_color_edit / サブ / コメント)
        self.outline_color_edit = None
        self.sub_outline_color_edit = None
        self.comment_outline_color_edit = None
        self.outline_width_edit = None
        self.back_color_edit = None
        self.bold_check = None
        self.italic_check = None
        self.underline_check = None
        self.strikeout_check = None
        self.spacing_edit = None
        self.angle_edit = None
        self.border_style_combo = None
        self.alignment_combo = None
        self.margin_l_edit = None
        self.margin_r_edit = None
        self.margin_v_edit = None
        # 縦動画(Shorts/TikTok)タブ ウィジェット参照 (request14)
        self.vertical_enabled_check = None
        self.vertical_output_width_edit = None
        self.vertical_output_height_edit = None
        self.vertical_font_size_edit = None
        self.vertical_min_line_length_edit = None
        self.vertical_max_line_length_edit = None
        self.vertical_alignment_combo = None
        self.vertical_margin_l_edit = None
        self.vertical_margin_r_edit = None
        self.vertical_margin_v_edit = None
        # アーカイブ切り抜き: テーマ・イントロカード ウィジェット参照 (resolve19 §5)
        self.intro_enabled_check = None
        self.intro_buffer_sec_edit = None
        self.intro_blur_sigma_edit = None
        self.intro_margin_top_edit = None
        self.intro_margin_bottom_edit = None
        self.intro_box_color_edit = None
        self.intro_box_opacity_edit = None
        self.intro_title_font_combo = None
        self.intro_title_font_size_edit = None
        self.intro_title_color_edit = None
        self.intro_tag_font_combo = None
        self.intro_tag_font_size_edit = None
        self.intro_tag_color_edit = None
        self.intro_tag_bg_opacity_edit = None
        self.intro_tag_margin_l_edit = None
        self.intro_tag_margin_v_edit = None
        self.intro_mute_check = None

        # 追加フォント (settings/fonts) を Qt へ登録してから UI を構築する
        # (フォント一覧・プレビューに追加フォントを反映する / resolve16 §4.2)
        self._fonts_dir = resolve_fonts_dir(load_settings())
        register_fonts_in_dir(self._fonts_dir)

        self._build_ui()
        self._load_to_ui()

    # 画面構築処理
    # タブ (一般 / 字幕) と共通の保存ボタンを構成する
    def _build_ui(self):
        root_layout = QVBoxLayout(self)

        tabs = QTabWidget()
        tabs.addTab(self._build_general_tab(), "一般")
        tabs.addTab(self._build_subtitle_tab(), "字幕")
        tabs.addTab(self._build_vertical_tab(), "縦動画")
        tabs.addTab(self._build_archive_tab(), "アーカイブ")
        # 先頭 (「一般」) タブ選択時のみペイン左上を四角にする (resolve4 S1)
        theme.bind_tab_pane_corner(tabs)
        root_layout.addWidget(tabs)

        # 保存ボタン (全タブ共通・タブ外に配置)
        save_button = QPushButton("保存")
        save_button.setMinimumHeight(32)
        save_button.clicked.connect(self._on_save)
        button_layout = QHBoxLayout()
        button_layout.addStretch(1)
        button_layout.addWidget(save_button)
        root_layout.addLayout(button_layout)

    # 「一般」タブを構築する
    def _build_general_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        row = 0

        # 動画ディレクトリ
        self.video_dir_edit = QLineEdit()
        grid.addWidget(self._make_column_label("動画ディレクトリ"), row, 0)
        grid.addLayout(self._make_dir_picker(self.video_dir_edit), row, 1)
        row += 1

        # オープニング動画 (チェックON時のみ本編へ結合する)
        self.opening_edit = QLineEdit()
        self.opening_enabled_check = QCheckBox("結合する")
        opening_layout = self._make_file_picker(self.opening_edit)
        opening_layout.addWidget(self.opening_enabled_check)
        grid.addWidget(self._make_column_label("オープニング動画"), row, 0)
        grid.addLayout(opening_layout, row, 1)
        row += 1

        # エンディング動画 (チェックON時のみ本編へ結合する)
        self.ending_edit = QLineEdit()
        self.ending_enabled_check = QCheckBox("結合する")
        ending_layout = self._make_file_picker(self.ending_edit)
        ending_layout.addWidget(self.ending_enabled_check)
        grid.addWidget(self._make_column_label("エンディング動画"), row, 0)
        grid.addLayout(ending_layout, row, 1)
        row += 1

        # 出力ディレクトリ (空の場合は入力動画と同じ場所へ出力)
        self.output_dir_edit = QLineEdit()
        grid.addWidget(self._make_column_label("出力ディレクトリ"), row, 0)
        grid.addLayout(self._make_dir_picker(self.output_dir_edit), row, 1)
        row += 1

        # 自動更新チェック ON/OFF (request_autoupdate.md §7)
        self.auto_update_check_check = QCheckBox("起動時に更新を確認する")
        grid.addWidget(self._make_column_label("自動更新"), row, 0)
        grid.addWidget(self.auto_update_check_check, row, 1)
        row += 1

        # ===== 無音カット (silence_cut) =====
        # 無音音量閾値(dB)・無音最小継続時間(秒) は resolve7 により UI から削除。
        # 無音判定の閾値は音量解析で確定する単一値 (volume_analysis.last_cut_db) を使用する。

        # 無音カット ON/OFF (resolve12)
        self.silence_enabled_check = QCheckBox("無音カットを有効にする")
        grid.addWidget(self._make_column_label("無音カット"), row, 0)
        grid.addWidget(self.silence_enabled_check, row, 1)
        row += 1

        # 境界フェード ON/OFF
        self.fade_enabled_check = QCheckBox("セグメント境界にフェードを付与する")
        grid.addWidget(self._make_column_label("フェード"), row, 0)
        grid.addWidget(self.fade_enabled_check, row, 1)
        row += 1

        # フェード秒数 (float)
        self.fade_duration_edit = QLineEdit()
        self.fade_duration_edit.setPlaceholderText(PLACEHOLDER_FADE_DURATION)
        self.fade_duration_edit.setMinimumWidth(INPUT_FIELD_MIN_WIDTH)
        grid.addWidget(self._make_column_label("フェード時間(秒)"), row, 0)
        grid.addWidget(self.fade_duration_edit, row, 1)
        row += 1

        layout.addLayout(grid)
        layout.addStretch(1)
        return page

    # 「字幕」タブを構築する (字幕・フォント関係を集約)
    def _build_subtitle_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        row = 0

        # テロップ生成 ON/OFF (要件②)
        self.subtitle_enabled_check = QCheckBox("テロップ生成を有効にする")
        grid.addWidget(self._make_column_label("テロップ生成"), row, 0)
        grid.addWidget(self.subtitle_enabled_check, row, 1)
        row += 1

        # 字幕編集画面 ON/OFF (GUI ランチャ実行時のみ有効。CLI 実行では画面は出ない)
        self.review_enabled_check = QCheckBox("焼き込み前に字幕編集画面を開く")
        grid.addWidget(self._make_column_label("字幕編集"), row, 0)
        grid.addWidget(self.review_enabled_check, row, 1)
        row += 1

        # ===== 発話検出 (テロップ表示の絞り込み) =====
        # 無音カット閾値とは別に、テロップを表示する発話区間を判定する閾値群
        # 発話検出 ON/OFF
        self.speech_gate_enabled_check = QCheckBox("発話区間のみテロップを表示する")
        grid.addWidget(self._make_column_label("発話検出"), row, 0)
        grid.addWidget(self.speech_gate_enabled_check, row, 1)
        row += 1

        # 発話検出閾値(dB) は resolve7 により UI から削除。
        # テロップの発話絞り込みは無効化され、無音カットの単一閾値へ一本化された。

        # 発話区間検出の最小無音長 (float)
        self.speech_min_duration_edit = QLineEdit()
        self.speech_min_duration_edit.setPlaceholderText(PLACEHOLDER_SPEECH_MIN_DURATION)
        self.speech_min_duration_edit.setMinimumWidth(INPUT_FIELD_MIN_WIDTH)
        grid.addWidget(self._make_column_label("発話最小無音長(秒)"), row, 0)
        grid.addWidget(self.speech_min_duration_edit, row, 1)
        row += 1

        # 絞り込み方式 (ドロップダウン: value/text 分離)
        self.speech_gate_mode_combo = self._make_value_combo(SPEECH_GATE_MODE_OPTIONS)
        grid.addWidget(self._make_column_label("絞り込み方式"), row, 0)
        grid.addWidget(self.speech_gate_mode_combo, row, 1)
        row += 1

        # 表示区間の前後パディング (float)
        self.speech_pad_edit = QLineEdit()
        self.speech_pad_edit.setPlaceholderText(PLACEHOLDER_SPEECH_PAD)
        self.speech_pad_edit.setMinimumWidth(INPUT_FIELD_MIN_WIDTH)
        grid.addWidget(self._make_column_label("前後パディング(秒)"), row, 0)
        grid.addWidget(self.speech_pad_edit, row, 1)
        row += 1

        # ===== 音声認識 (Whisper) 実行環境 =====
        # 実行デバイス (CUDA 不使用方針により CPU のみ / resolve8.md)
        self.whisper_device_combo = self._make_value_combo(WHISPER_DEVICE_OPTIONS)
        grid.addWidget(self._make_column_label("実行デバイス"), row, 0)
        grid.addWidget(self.whisper_device_combo, row, 1)
        row += 1

        # 計算精度 (auto: CPU 実行のため int8)
        self.whisper_compute_type_combo = self._make_value_combo(WHISPER_COMPUTE_TYPE_OPTIONS)
        grid.addWidget(self._make_column_label("計算精度"), row, 0)
        grid.addWidget(self.whisper_compute_type_combo, row, 1)
        row += 1

        # 字幕言語
        self.language_combo = QComboBox()
        self.language_combo.addItems(SUBTITLE_LANGUAGES)
        self.language_combo.setMinimumWidth(INPUT_FIELD_MIN_WIDTH)
        grid.addWidget(self._make_column_label("字幕言語"), row, 0)
        grid.addWidget(self.language_combo, row, 1)
        row += 1

        # テロップ役割別カラー (配信者 / サブ / コメント) — HTML #RRGGBB 形式 (アルファ無し)
        # 縦 3 行から横 1 行へ変更した (ver3 resolve4 S2)。参照名は変えないため
        # 保存・読み込み (_load_to_ui / _collect_settings) には影響しない。
        self.color_edit = QLineEdit()
        self.color_edit.setPlaceholderText(PLACEHOLDER_COLOR)
        self.sub_color_edit = QLineEdit()
        self.sub_color_edit.setPlaceholderText(PLACEHOLDER_SUB_COLOR)
        self.comment_color_edit = QLineEdit()
        self.comment_color_edit.setPlaceholderText(PLACEHOLDER_COMMENT_COLOR)
        grid.addWidget(self._make_column_label("テロップカラー"), row, 0)
        grid.addLayout(self._make_color_role_row(
            (self.color_edit, self.sub_color_edit, self.comment_color_edit),
            with_alpha=False, field_width=COLOR_FIELD_MIN_WIDTH), row, 1)
        row += 1

        # フォントの大きさ (整数のみ)
        self.font_size_edit = self._make_int_edit()
        grid.addWidget(self._make_column_label("フォントの大きさ"), row, 0)
        grid.addWidget(self.font_size_edit, row, 1)
        row += 1

        # フォント種類 (インストール済 + 追加フォント一覧から選択) + フォント追加ボタン
        # 各項目はそのフォント自身で描画され、プレビュー代わりになる (resolve16 §4.1)
        self.font_family_combo = self._make_font_family_combo()
        add_font_button = QPushButton("追加…")
        add_font_button.setToolTip("フォントファイル(.ttf/.otf/.ttc)を追加する")
        add_font_button.clicked.connect(self._on_add_font)
        font_row_layout = QHBoxLayout()
        font_row_layout.setContentsMargins(0, 0, 0, 0)
        font_row_layout.addWidget(self.font_family_combo)
        font_row_layout.addWidget(add_font_button)
        grid.addWidget(self._make_column_label("フォント種類"), row, 0)
        grid.addLayout(font_row_layout, row, 1)
        row += 1

        # フォントプレビュー (選択中フォントでサンプル文字を描画 / resolve16 §4.1)
        self.font_preview_label = QLabel(FONT_PREVIEW_SAMPLE_TEXT)
        grid.addWidget(self._make_column_label("プレビュー"), row, 0)
        grid.addWidget(self.font_preview_label, row, 1)
        row += 1

        # 追加可能フォントの注意書き (同カラムに小さく記載 / resolve16 §4.2)
        font_note_label = QLabel(FONT_ADD_NOTE)
        theme.mark_note(font_note_label)
        # 文字サイズは QSS ではなくフォントで指定する (インライン指定の撤去 / resolve3 §5.3)
        note_font = QFont(font_note_label.font())
        note_font.setPointSize(FONT_NOTE_POINT_SIZE)
        font_note_label.setFont(note_font)
        grid.addWidget(font_note_label, row, 1)
        row += 1

        # 選択変更でプレビュー(現在値+サンプル欄)を更新する
        self.font_family_combo.currentIndexChanged.connect(self._update_font_preview)
        self._update_font_preview()  # 初期表示

        # 改行方式 (request11): BudouX(文節) / 文字数
        self.wrap_engine_combo = self._make_value_combo(WRAP_ENGINE_OPTIONS)
        grid.addWidget(self._make_column_label("改行方式"), row, 0)
        grid.addWidget(self.wrap_engine_combo, row, 1)
        row += 1

        # 1行下限文字数 (整数のみ / BudouX 時のみ有効)
        self.min_line_length_edit = self._make_int_edit()
        grid.addWidget(self._make_column_label("1行下限文字数"), row, 0)
        grid.addWidget(self.min_line_length_edit, row, 1)
        row += 1

        # 1行最大文字数 (整数のみ)
        self.max_line_length_edit = self._make_int_edit()
        grid.addWidget(self._make_column_label("1行最大文字数"), row, 0)
        grid.addWidget(self.max_line_length_edit, row, 1)
        row += 1

        # アウトライン役割別カラー (配信者 / サブ / コメント)
        # ASS 形式 &HAABBGGRR / アルファ有り。テロップカラーと同じく横 1 行へ並べる
        # (ver3 resolve4 S3 / 回答 Q4)。書式が長いぶん入力欄は広めの幅を使う。
        self.outline_color_edit = QLineEdit()
        self.outline_color_edit.setPlaceholderText(PLACEHOLDER_OUTLINE_COLOR)
        self.sub_outline_color_edit = QLineEdit()
        self.sub_outline_color_edit.setPlaceholderText(PLACEHOLDER_OUTLINE_COLOR)
        self.comment_outline_color_edit = QLineEdit()
        self.comment_outline_color_edit.setPlaceholderText(PLACEHOLDER_OUTLINE_COLOR)
        grid.addWidget(self._make_column_label("アウトラインカラー"), row, 0)
        grid.addLayout(self._make_color_role_row(
            (self.outline_color_edit, self.sub_outline_color_edit,
             self.comment_outline_color_edit),
            with_alpha=True, field_width=OUTLINE_COLOR_FIELD_MIN_WIDTH), row, 1)
        row += 1

        # アウトラインの太さ (整数のみ)
        self.outline_width_edit = self._make_int_edit()
        grid.addWidget(self._make_column_label("アウトラインの太さ"), row, 0)
        grid.addWidget(self.outline_width_edit, row, 1)
        row += 1

        # 背景/影の色 (ASS 形式 &HAABBGGRR / アルファ有り)
        self.back_color_edit = QLineEdit()
        self.back_color_edit.setPlaceholderText(PLACEHOLDER_BACK_COLOR)
        grid.addWidget(self._make_column_label("背景/影の色"), row, 0)
        grid.addLayout(self._make_color_picker(self.back_color_edit, with_alpha=True), row, 1)
        row += 1

        # 装飾 (太字/斜体/下線/打消し線) を横並びで配置
        self.bold_check = QCheckBox("太字")
        self.italic_check = QCheckBox("斜体")
        self.underline_check = QCheckBox("下線")
        self.strikeout_check = QCheckBox("打消し線")
        decoration_layout = QHBoxLayout()
        decoration_layout.setContentsMargins(0, 0, 0, 0)
        decoration_layout.addWidget(self.bold_check)
        decoration_layout.addWidget(self.italic_check)
        decoration_layout.addWidget(self.underline_check)
        decoration_layout.addWidget(self.strikeout_check)
        decoration_layout.addStretch(1)
        grid.addWidget(self._make_column_label("装飾"), row, 0)
        grid.addLayout(decoration_layout, row, 1)
        row += 1

        # 文字間隔 (整数のみ)
        self.spacing_edit = self._make_int_edit()
        grid.addWidget(self._make_column_label("文字間隔"), row, 0)
        grid.addWidget(self.spacing_edit, row, 1)
        row += 1

        # 回転角度 (整数のみ)
        self.angle_edit = self._make_int_edit()
        grid.addWidget(self._make_column_label("回転角度"), row, 0)
        grid.addWidget(self.angle_edit, row, 1)
        row += 1

        # 縁取りスタイル (ドロップダウン: value/text 分離)
        self.border_style_combo = self._make_value_combo(BORDER_STYLE_OPTIONS)
        grid.addWidget(self._make_column_label("縁取りスタイル"), row, 0)
        grid.addWidget(self.border_style_combo, row, 1)
        row += 1

        # 表示位置 (ドロップダウン: value/text 分離)
        self.alignment_combo = self._make_value_combo(ALIGNMENT_OPTIONS)
        grid.addWidget(self._make_column_label("表示位置"), row, 0)
        grid.addWidget(self.alignment_combo, row, 1)
        row += 1

        # 余白 (左/右/下) を横並びで配置
        self.margin_l_edit = self._make_int_edit()
        self.margin_r_edit = self._make_int_edit()
        self.margin_v_edit = self._make_int_edit()
        margin_layout = QHBoxLayout()
        margin_layout.setContentsMargins(0, 0, 0, 0)
        margin_layout.addWidget(QLabel("左"))
        margin_layout.addWidget(self.margin_l_edit)
        margin_layout.addWidget(QLabel("右"))
        margin_layout.addWidget(self.margin_r_edit)
        margin_layout.addWidget(QLabel("下"))
        margin_layout.addWidget(self.margin_v_edit)
        grid.addWidget(self._make_column_label("余白"), row, 0)
        grid.addLayout(margin_layout, row, 1)
        row += 1

        layout.addLayout(grid)
        layout.addStretch(1)
        return page

    # 「縦動画」タブを構築する (request14 / resolve14)
    # 縦動画(YouTube Shorts / TikTok)検出時に適用する出力サイズ・フォント・改行・配置を設定する。
    # フォント種類/色/装飾は「字幕」タブの設定を共有し、ここでは縦専用の上書き値のみを扱う。
    def _build_vertical_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        row = 0

        # 縦動画対応 ON/OFF (OFF 時は縦動画も横として処理する)
        self.vertical_enabled_check = QCheckBox("縦動画を自動判定して縦仕様で出力する")
        grid.addWidget(self._make_column_label("縦動画対応"), row, 0)
        grid.addWidget(self.vertical_enabled_check, row, 1)
        row += 1

        # 出力サイズ (幅/高) — 既定 1080x1920 (9:16)
        self.vertical_output_width_edit = self._make_int_edit()
        self.vertical_output_height_edit = self._make_int_edit()
        size_layout = QHBoxLayout()
        size_layout.setContentsMargins(0, 0, 0, 0)
        size_layout.addWidget(QLabel("幅"))
        size_layout.addWidget(self.vertical_output_width_edit)
        size_layout.addWidget(QLabel("高"))
        size_layout.addWidget(self.vertical_output_height_edit)
        grid.addWidget(self._make_column_label("出力サイズ"), row, 0)
        grid.addLayout(size_layout, row, 1)
        row += 1

        # 縦動画用フォントの大きさ (要望3)
        self.vertical_font_size_edit = self._make_int_edit()
        grid.addWidget(self._make_column_label("フォントの大きさ"), row, 0)
        grid.addWidget(self.vertical_font_size_edit, row, 1)
        row += 1

        # 1行下限文字数 (縦は 10〜15 目安 / 要望4)
        self.vertical_min_line_length_edit = self._make_int_edit()
        grid.addWidget(self._make_column_label("1行下限文字数"), row, 0)
        grid.addWidget(self.vertical_min_line_length_edit, row, 1)
        row += 1

        # 1行最大文字数 (縦は 10〜15 目安 / 要望4)
        self.vertical_max_line_length_edit = self._make_int_edit()
        grid.addWidget(self._make_column_label("1行最大文字数"), row, 0)
        grid.addWidget(self.vertical_max_line_length_edit, row, 1)
        row += 1

        # 表示位置 (Shorts/TikTok の下端UIを避けるため下段中央+余白が既定 / 要望5)
        self.vertical_alignment_combo = self._make_value_combo(ALIGNMENT_OPTIONS)
        grid.addWidget(self._make_column_label("表示位置"), row, 0)
        grid.addWidget(self.vertical_alignment_combo, row, 1)
        row += 1

        # 余白 (左/右/下) — 下端UI帯を避けるため下余白(下)は大きめが既定
        self.vertical_margin_l_edit = self._make_int_edit()
        self.vertical_margin_r_edit = self._make_int_edit()
        self.vertical_margin_v_edit = self._make_int_edit()
        margin_layout = QHBoxLayout()
        margin_layout.setContentsMargins(0, 0, 0, 0)
        margin_layout.addWidget(QLabel("左"))
        margin_layout.addWidget(self.vertical_margin_l_edit)
        margin_layout.addWidget(QLabel("右"))
        margin_layout.addWidget(self.vertical_margin_r_edit)
        margin_layout.addWidget(QLabel("下"))
        margin_layout.addWidget(self.vertical_margin_v_edit)
        grid.addWidget(self._make_column_label("余白"), row, 0)
        grid.addLayout(margin_layout, row, 1)
        row += 1

        layout.addLayout(grid)
        layout.addStretch(1)
        return page

    # 「アーカイブ」タブを構築する (resolve19 §5)
    # アーカイブ切り抜きのテーマ・イントロカード演出を設定する。
    # 字幕一覧画面でテーマを入力したクリップにのみ発火する演出のパラメータ。
    def _build_archive_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        row = 0

        # 見出し (アーカイブ切り抜き 本体の有効/無効)
        # archive.enabled はメイン画面「アーカイブ切り抜き」タブの表示可否を決める
        # マスタースイッチ。従来は setting.json 直接編集でしか切替できなかったため
        # GUI から切替できるようにする (無効時はタブが「無効」表示になる)。
        # タイトル/説明/チェックは (末尾で追加される grid とは別に) layout へ直接足し、
        # 「テーマ・イントロカード」節より前に来るよう表示順を保つ。
        layout.addWidget(self._make_title("アーカイブ切り抜き"))
        archive_note = QLabel(
            "メイン画面の「アーカイブ切り抜き」タブを使えるようにします。\n"
            "変更はアプリの再起動後に反映されます。"
        )
        theme.mark_note(archive_note)
        layout.addWidget(archive_note)

        self.archive_enabled_check = QCheckBox("アーカイブ切り抜きを有効にする")
        archive_enable_layout = QHBoxLayout()
        archive_enable_layout.setContentsMargins(0, 0, 0, 0)
        archive_enable_layout.addWidget(self._make_column_label("機能"))
        archive_enable_layout.addWidget(self.archive_enabled_check)
        archive_enable_layout.addStretch(1)
        layout.addLayout(archive_enable_layout)

        # 見出し (テーマ・イントロカード)
        layout.addWidget(self._make_title("テーマ・イントロカード"))
        note = QLabel(
            "字幕一覧画面でテーマを入力したクリップに、開始前バッファを復元して\n"
            "ブラー+黒帯+中央テーマのイントロを付け、本編には左上へテーマを焼きます。"
        )
        theme.mark_note(note)
        layout.addWidget(note)

        # 機能 ON/OFF (OFF 時はテーマ欄自体を出さない)
        self.intro_enabled_check = QCheckBox("テーマ・イントロカードを有効にする")
        grid.addWidget(self._make_column_label("機能"), row, 0)
        grid.addWidget(self.intro_enabled_check, row, 1)
        row += 1

        # 開始前バッファ秒数 (切り抜きで削れた直前を復元する長さ)
        self.intro_buffer_sec_edit = self._make_float_edit()
        grid.addWidget(self._make_column_label("開始前バッファ(秒)"), row, 0)
        grid.addWidget(self.intro_buffer_sec_edit, row, 1)
        row += 1

        # ブラー強度 (gblur sigma)
        self.intro_blur_sigma_edit = self._make_float_edit()
        grid.addWidget(self._make_column_label("ブラー強度"), row, 0)
        grid.addWidget(self.intro_blur_sigma_edit, row, 1)
        row += 1

        # 黒帯の上下余白 (px) を横並びで配置
        self.intro_margin_top_edit = self._make_int_edit()
        self.intro_margin_bottom_edit = self._make_int_edit()
        margin_layout = QHBoxLayout()
        margin_layout.setContentsMargins(0, 0, 0, 0)
        margin_layout.addWidget(QLabel("上"))
        margin_layout.addWidget(self.intro_margin_top_edit)
        margin_layout.addWidget(QLabel("下"))
        margin_layout.addWidget(self.intro_margin_bottom_edit)
        grid.addWidget(self._make_column_label("黒帯の余白(px)"), row, 0)
        grid.addLayout(margin_layout, row, 1)
        row += 1

        # 黒帯の色 (ffmpeg 色名または #RRGGBB) と不透明度 を横並びで配置
        self.intro_box_color_edit = QLineEdit()
        self.intro_box_color_edit.setMinimumWidth(INPUT_FIELD_MIN_WIDTH)
        self.intro_box_color_edit.setPlaceholderText("black")
        self.intro_box_opacity_edit = self._make_float_edit()
        box_layout = QHBoxLayout()
        box_layout.setContentsMargins(0, 0, 0, 0)
        box_layout.addWidget(QLabel("色"))
        box_layout.addWidget(self.intro_box_color_edit)
        box_layout.addWidget(QLabel("不透明度"))
        box_layout.addWidget(self.intro_box_opacity_edit)
        grid.addWidget(self._make_column_label("黒帯"), row, 0)
        grid.addLayout(box_layout, row, 1)
        row += 1

        # 中央テーマ フォント (空=字幕フォントに従う)
        self.intro_title_font_combo = self._make_optional_font_combo()
        grid.addWidget(self._make_column_label("中央テーマ フォント"), row, 0)
        grid.addWidget(self.intro_title_font_combo, row, 1)
        row += 1

        # 中央テーマ 文字サイズ
        self.intro_title_font_size_edit = self._make_int_edit()
        grid.addWidget(self._make_column_label("中央テーマ サイズ"), row, 0)
        grid.addWidget(self.intro_title_font_size_edit, row, 1)
        row += 1

        # 中央テーマ 文字色 (HTML #RRGGBB)
        self.intro_title_color_edit = QLineEdit()
        grid.addWidget(self._make_column_label("中央テーマ 文字色"), row, 0)
        grid.addLayout(self._make_color_picker(self.intro_title_color_edit, with_alpha=False), row, 1)
        row += 1

        # 左上タグ フォント (空=字幕フォントに従う)
        self.intro_tag_font_combo = self._make_optional_font_combo()
        grid.addWidget(self._make_column_label("左上タグ フォント"), row, 0)
        grid.addWidget(self.intro_tag_font_combo, row, 1)
        row += 1

        # 左上タグ 文字サイズ
        self.intro_tag_font_size_edit = self._make_int_edit()
        grid.addWidget(self._make_column_label("左上タグ サイズ"), row, 0)
        grid.addWidget(self.intro_tag_font_size_edit, row, 1)
        row += 1

        # 左上タグ 文字色 (HTML #RRGGBB)
        self.intro_tag_color_edit = QLineEdit()
        grid.addWidget(self._make_column_label("左上タグ 文字色"), row, 0)
        grid.addLayout(self._make_color_picker(self.intro_tag_color_edit, with_alpha=False), row, 1)
        row += 1

        # 左上タグ 背景不透明度 (0で背景なし)
        self.intro_tag_bg_opacity_edit = self._make_float_edit()
        grid.addWidget(self._make_column_label("左上タグ 背景不透明度"), row, 0)
        grid.addWidget(self.intro_tag_bg_opacity_edit, row, 1)
        row += 1

        # 左上タグ 余白 (左/上) を横並びで配置
        self.intro_tag_margin_l_edit = self._make_int_edit()
        self.intro_tag_margin_v_edit = self._make_int_edit()
        tag_margin_layout = QHBoxLayout()
        tag_margin_layout.setContentsMargins(0, 0, 0, 0)
        tag_margin_layout.addWidget(QLabel("左"))
        tag_margin_layout.addWidget(self.intro_tag_margin_l_edit)
        tag_margin_layout.addWidget(QLabel("上"))
        tag_margin_layout.addWidget(self.intro_tag_margin_v_edit)
        grid.addWidget(self._make_column_label("左上タグ 余白(px)"), row, 0)
        grid.addLayout(tag_margin_layout, row, 1)
        row += 1

        # イントロ音声の無音化
        self.intro_mute_check = QCheckBox("イントロの音声を無音化する")
        grid.addWidget(self._make_column_label("イントロ音声"), row, 0)
        grid.addWidget(self.intro_mute_check, row, 1)
        row += 1

        layout.addLayout(grid)
        layout.addStretch(1)
        return page

    # タイトル用ラベルを生成する
    def _make_title(self, text):
        label = QLabel(text)
        font = QFont()
        font.setBold(True)
        font.setPointSize(TITLE_FONT_SIZE)
        label.setFont(font)
        # 余白と下線は QSS 側 (#sectionTitle) が持つ (インライン指定の撤去 / resolve3 §5.3)
        theme.mark_title(label)
        return label

    # 項目名ラベルを生成する
    def _make_column_label(self, text):
        label = QLabel(text)
        label.setMinimumWidth(COLUMN_LABEL_WIDTH)
        label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        return label

    # 整数入力用の QLineEdit を生成する (バリデータ付き)
    def _make_int_edit(self):
        edit = QLineEdit()
        edit.setValidator(QIntValidator())
        edit.setMinimumWidth(INPUT_FIELD_MIN_WIDTH)
        return edit

    # 小数入力用の QLineEdit を生成する (バリデータ付き / resolve19)
    def _make_float_edit(self):
        edit = QLineEdit()
        validator = QDoubleValidator()
        validator.setBottom(0.0)  # 秒数・強度・不透明度はいずれも非負
        edit.setValidator(validator)
        edit.setMinimumWidth(INPUT_FIELD_MIN_WIDTH)
        return edit

    # 先頭に「(字幕フォントに従う)」= 空値 を持つフォント種類コンボを生成する (resolve19)
    # 空選択のとき intro_card の *_font_family を "" として保存する。
    def _make_optional_font_combo(self):
        combo = QComboBox()
        combo.setEditable(False)
        combo.setMinimumWidth(INPUT_FIELD_MIN_WIDTH)
        combo.addItem("(字幕フォントに従う)", "")  # 既定 = 空文字
        for family in QFontDatabase.families():
            combo.addItem(family, family)
            combo.setItemData(combo.count() - 1, QFont(family, FONT_PREVIEW_POINT_SIZE), Qt.FontRole)
        return combo

    # value/text を分離したドロップダウンを生成する
    # options は (value, 表示テキスト) のリスト
    def _make_value_combo(self, options):
        combo = QComboBox()
        combo.setMinimumWidth(INPUT_FIELD_MIN_WIDTH)
        for value, text in options:
            combo.addItem(text, value)
        return combo

    # ディレクトリ選択用の入力欄+ボタンを生成する
    def _make_dir_picker(self, line_edit):
        line_edit.setMinimumWidth(INPUT_FIELD_MIN_WIDTH)
        button = QPushButton("参照…")
        button.clicked.connect(lambda: self._choose_directory(line_edit))
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(line_edit)
        layout.addWidget(button)
        return layout

    # ファイル選択用の入力欄+ボタンを生成する
    def _make_file_picker(self, line_edit):
        line_edit.setMinimumWidth(INPUT_FIELD_MIN_WIDTH)
        button = QPushButton("参照…")
        button.clicked.connect(lambda: self._choose_file(line_edit))
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(line_edit)
        layout.addWidget(button)
        return layout

    # 色入力欄 + カラーピッカー起動ボタン(色見本付き)を生成する
    # with_alpha=True のとき ASS(&HAABBGGRR) 形式、False のとき HTML(#RRGGBB) 形式として扱う
    # field_width で入力欄の最小幅を変えられる (ver3 resolve4 S2 / S3)。
    # 役割別カラーを横 3 列へ並べるときは既定より狭い値を渡す。
    def _make_color_picker(self, line_edit, with_alpha, field_width=None):
        line_edit.setMinimumWidth(
            INPUT_FIELD_MIN_WIDTH if field_width is None else field_width)
        button = QPushButton()
        button.clicked.connect(lambda: self._choose_color(line_edit, with_alpha))
        # 入力欄の値が変わったら色見本を更新する(手入力にも追従)
        line_edit.textChanged.connect(
            lambda: self._update_swatch(button, line_edit, with_alpha)
        )
        self._update_swatch(button, line_edit, with_alpha)  # 初期表示
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(line_edit)
        layout.addWidget(button)
        return layout

    # 役割別カラー 3 種を横 1 行に組む (ver3 resolve4 S2 / S3)
    # 各列は「役割名 (上) + 入力欄 + 色見本 (下)」の縦積みにして、
    # 横に並べてもどの欄がどの役割か分かるようにする。
    # line_edits は COLOR_ROLE_LABELS と同じ順 (配信者/サブ/コメント)。
    def _make_color_role_row(self, line_edits, with_alpha, field_width):
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        for label_text, line_edit in zip(COLOR_ROLE_LABELS, line_edits):
            column = QVBoxLayout()
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(2)
            role_label = QLabel(label_text)
            theme.mark_note(role_label)
            column.addWidget(role_label)
            # 幅は最小値のみ与える。固定にすると画面が狭いときに溢れるため。
            column.addLayout(
                self._make_color_picker(line_edit, with_alpha, field_width))
            row.addLayout(column)
        row.addStretch(1)
        return row

    # カラーピッカーを開き、選択結果を所定形式の文字列で line_edit へ書き戻す
    def _choose_color(self, line_edit, with_alpha):
        initial = self._parse_color(line_edit.text(), with_alpha)  # 現在値を QColor へ
        if with_alpha:
            # アウトライン/背景は透明度も選択可能にする
            options = QColorDialog.ColorDialogOption.ShowAlphaChannel
        else:
            options = QColorDialog.ColorDialogOption(0)
        color = QColorDialog.getColor(initial, self, "色を選択", options)
        if color.isValid():
            line_edit.setText(self._format_color(color, with_alpha))

    # 設定文字列を QColor へ変換する(不正・空欄時は既定色を返す)
    # with_alpha=True: ASS &HAABBGGRR / False: HTML #RRGGBB
    # 実体は color_field へ移した (Timeline のインスペクタと共有するため / resolve6 §5.4)。
    # フォールバック色はプレースホルダー定数を渡し、従来と同じ結果になるようにしている。
    def _parse_color(self, text, with_alpha):
        fallback = PLACEHOLDER_OUTLINE_COLOR if with_alpha else PLACEHOLDER_COLOR
        return color_field.parse_color(text, with_alpha, fallback)

    # QColor を設定保存形式の文字列へ変換する
    # with_alpha=True: ASS &HAABBGGRR / False: HTML #RRGGBB
    def _format_color(self, color, with_alpha):
        return color_field.format_color(color, with_alpha)

    # ボタン上の色見本を現在の入力値で塗り替える(手入力・ピッカー双方に追従)
    # ここはテーマの適用対象外 (resolve3 §5.5)。
    # 塗りは「ユーザーが設定した字幕の色そのもの」を表示する機能であり、
    # テーマで塗り替えると設定値が見えなくなる。枠線の色だけテーマから引く。
    def _update_swatch(self, button, line_edit, with_alpha):
        fallback = PLACEHOLDER_OUTLINE_COLOR if with_alpha else PLACEHOLDER_COLOR
        color_field.apply_swatch(button, line_edit.text(), with_alpha, fallback)

    # ディレクトリ選択ダイアログを開く
    def _choose_directory(self, line_edit):
        start_dir = line_edit.text() or SETTINGS_DIR
        path = QFileDialog.getExistingDirectory(self, "ディレクトリを選択", start_dir)
        if path:
            line_edit.setText(path)

    # ファイル選択ダイアログを開く
    def _choose_file(self, line_edit):
        start_dir = os.path.dirname(line_edit.text()) if line_edit.text() else SETTINGS_DIR
        path, _ = QFileDialog.getOpenFileName(
            self, "ファイルを選択", start_dir, VIDEO_FILE_FILTER
        )
        if path:
            line_edit.setText(path)

    # 既存設定をUIへ反映する
    def _load_to_ui(self):
        # 表示用と保存マージ用に元設定を保持する
        self._loaded_settings = load_settings()
        general = self._loaded_settings.get("general", {})
        subtitle = self._loaded_settings.get("subtitle", {})
        silence_cut = self._loaded_settings.get("silence_cut", {})
        vertical = self._loaded_settings.get("vertical", {})
        archive = self._loaded_settings.get("archive", {})
        intro_card = archive.get("intro_card", {})

        self.video_dir_edit.setText(general.get("video_directory", ""))
        self.opening_edit.setText(general.get("opening_video", ""))
        self.opening_enabled_check.setChecked(bool(general.get("opening_enabled", True)))
        self.ending_edit.setText(general.get("ending_video", ""))
        self.ending_enabled_check.setChecked(bool(general.get("ending_enabled", True)))
        self.output_dir_edit.setText(general.get("output_directory", ""))
        self.auto_update_check_check.setChecked(bool(general.get("auto_update_check", True)))

        # 無音カット
        # 無音音量閾値(dB)/無音最小継続時間 は resolve7 により UI から削除
        self.silence_enabled_check.setChecked(bool(silence_cut.get("enabled", True)))
        self.fade_enabled_check.setChecked(bool(silence_cut.get("fade_enabled", False)))
        self.fade_duration_edit.setText(str(silence_cut.get("fade_duration_sec", 0.05)))

        self._set_combo_value(self.language_combo, subtitle.get("language", ""))

        # テロップ役割別カラー (配信者/サブ/コメント)
        self.color_edit.setText(subtitle.get("own_subtitle_color", ""))
        self.sub_color_edit.setText(subtitle.get("sub_subtitle_color", ""))
        self.comment_color_edit.setText(subtitle.get("comment_subtitle_color", ""))
        self.subtitle_enabled_check.setChecked(bool(subtitle.get("enabled", True)))
        self.review_enabled_check.setChecked(bool(subtitle.get("review_enabled", True)))

        # 発話検出 (テロップ表示絞り込み)
        self.speech_gate_enabled_check.setChecked(
            bool(subtitle.get("speech_gate_enabled", True))
        )
        # 発話検出閾値(dB) は resolve7 により UI から削除
        self.speech_min_duration_edit.setText(
            str(subtitle.get("speech_min_duration_sec", 0.2))
        )
        self._set_combo_data(self.speech_gate_mode_combo, subtitle.get("speech_gate_mode", "trim"))
        self.speech_pad_edit.setText(str(subtitle.get("speech_pad_sec", 0.1)))

        # 音声認識 (Whisper) 実行デバイス/計算精度
        self._set_combo_data(self.whisper_device_combo, subtitle.get("whisper_device", "cpu"))
        self._set_combo_data(
            self.whisper_compute_type_combo, subtitle.get("whisper_compute_type", "int8")
        )

        # フォント関係
        self.font_size_edit.setText(str(subtitle.get("font_size", 48)))
        self._set_font_family(subtitle.get("font_family", "Yu Gothic UI"))
        # 改行設定 (request11)
        self._set_combo_data(self.wrap_engine_combo, subtitle.get("wrap_engine", "budoux"))
        self.min_line_length_edit.setText(str(subtitle.get("min_line_length", 15)))
        self.max_line_length_edit.setText(str(subtitle.get("max_line_length", 20)))
        # アウトライン色 (配信者/サブ/コメント)
        self.outline_color_edit.setText(subtitle.get("outline_color", ""))
        self.sub_outline_color_edit.setText(subtitle.get("sub_outline_color", ""))
        self.comment_outline_color_edit.setText(subtitle.get("comment_outline_color", ""))
        self.outline_width_edit.setText(str(subtitle.get("outline_width", 3)))
        self.back_color_edit.setText(subtitle.get("back_color", ""))
        self.bold_check.setChecked(bool(subtitle.get("bold", False)))
        self.italic_check.setChecked(bool(subtitle.get("italic", False)))
        self.underline_check.setChecked(bool(subtitle.get("underline", False)))
        self.strikeout_check.setChecked(bool(subtitle.get("strikeout", False)))
        self.spacing_edit.setText(str(subtitle.get("spacing", 0)))
        self.angle_edit.setText(str(subtitle.get("angle", 0)))
        self._set_combo_data(self.border_style_combo, subtitle.get("border_style", 1))
        self._set_combo_data(self.alignment_combo, subtitle.get("alignment", 2))
        self.margin_l_edit.setText(str(subtitle.get("margin_l", 40)))
        self.margin_r_edit.setText(str(subtitle.get("margin_r", 40)))
        self.margin_v_edit.setText(str(subtitle.get("margin_v", 60)))

        # 縦動画 (request14)
        self.vertical_enabled_check.setChecked(bool(vertical.get("enabled", True)))
        self.vertical_output_width_edit.setText(str(vertical.get("output_width", 1080)))
        self.vertical_output_height_edit.setText(str(vertical.get("output_height", 1920)))
        self.vertical_font_size_edit.setText(str(vertical.get("font_size", 90)))
        self.vertical_min_line_length_edit.setText(str(vertical.get("min_line_length", 10)))
        self.vertical_max_line_length_edit.setText(str(vertical.get("max_line_length", 15)))
        self._set_combo_data(self.vertical_alignment_combo, vertical.get("alignment", 2))
        self.vertical_margin_l_edit.setText(str(vertical.get("margin_l", 40)))
        self.vertical_margin_r_edit.setText(str(vertical.get("margin_r", 40)))
        self.vertical_margin_v_edit.setText(str(vertical.get("margin_v", 320)))

        # アーカイブ切り抜き: 本体の有効/無効 (メイン画面タブの表示可否)。
        # 既定は archive_tab の判定 (.get("enabled", False)) に合わせ、キー欠落時は無効表示にする。
        self.archive_enabled_check.setChecked(bool(archive.get("enabled", False)))

        # アーカイブ切り抜き: テーマ・イントロカード (resolve19 §5)
        self.intro_enabled_check.setChecked(bool(intro_card.get("enabled", True)))
        self.intro_buffer_sec_edit.setText(str(intro_card.get("buffer_sec", 1.5)))
        self.intro_blur_sigma_edit.setText(str(intro_card.get("blur_sigma", 18)))
        self.intro_margin_top_edit.setText(str(intro_card.get("margin_top_px", 300)))
        self.intro_margin_bottom_edit.setText(str(intro_card.get("margin_bottom_px", 300)))
        self.intro_box_color_edit.setText(str(intro_card.get("box_color", "black")))
        self.intro_box_opacity_edit.setText(str(intro_card.get("box_opacity", 1.0)))
        self._set_optional_font(self.intro_title_font_combo, intro_card.get("title_font_family", ""))
        self.intro_title_font_size_edit.setText(str(intro_card.get("title_font_size", 112)))
        self.intro_title_color_edit.setText(str(intro_card.get("title_color", "#FFFFFF")))
        self._set_optional_font(self.intro_tag_font_combo, intro_card.get("tag_font_family", ""))
        self.intro_tag_font_size_edit.setText(str(intro_card.get("tag_font_size", 40)))
        self.intro_tag_color_edit.setText(str(intro_card.get("tag_color", "#FFFFFF")))
        self.intro_tag_bg_opacity_edit.setText(str(intro_card.get("tag_bg_opacity", 0.45)))
        self.intro_tag_margin_l_edit.setText(str(intro_card.get("tag_margin_l", 40)))
        self.intro_tag_margin_v_edit.setText(str(intro_card.get("tag_margin_v", 30)))
        self.intro_mute_check.setChecked(bool(intro_card.get("mute_intro", False)))

    # 任意フォントコンボを値で選択する (空=先頭「字幕フォントに従う」/ 一覧に無ければ補完追加)
    def _set_optional_font(self, combo, family):
        if not family:
            combo.setCurrentIndex(0)
            return
        index = combo.findData(family)
        if index < 0:
            combo.addItem(family, family)
            index = combo.findData(family)
        combo.setCurrentIndex(index if index >= 0 else 0)

    # コンボボックスに値が含まれていれば選択状態にする
    def _set_combo_value(self, combo, value):
        index = combo.findText(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    # userData(value) を持つコンボボックスを value で選択状態にする
    def _set_combo_data(self, combo, value):
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    # フォント種類を選択する (一覧に無い場合は補完追加して設定値を維持)
    def _set_font_family(self, family):
        if not family:
            return
        index = self.font_family_combo.findText(family)
        if index < 0:
            self.font_family_combo.addItem(family)
            index = self.font_family_combo.findText(family)
        self.font_family_combo.setCurrentIndex(index)

    # フォント種類コンボを生成する (全項目を自フォントで描画=プレビュー / resolve16 §4.1)
    def _make_font_family_combo(self):
        combo = QComboBox()
        combo.setEditable(False)
        combo.setMinimumWidth(INPUT_FIELD_MIN_WIDTH)
        self._populate_font_combo(combo)
        return combo

    # フォント一覧をコンボへ充填し、各項目をそのフォント自身で描画する
    def _populate_font_combo(self, combo):
        combo.clear()
        for family in QFontDatabase.families():
            combo.addItem(family)
            # 各項目を自フォントで描画してプレビュー代わりにする
            combo.setItemData(
                combo.count() - 1, QFont(family, FONT_PREVIEW_POINT_SIZE), Qt.FontRole
            )

    # フォント追加後などに一覧を再構築し、直前の選択を復元する
    def _reload_font_combo(self):
        current = self.font_family_combo.currentText()
        self._populate_font_combo(self.font_family_combo)
        self._set_font_family(current)

    # 選択中フォントでプレビュー(ドロップダウン現在値+サンプル欄)を更新する
    def _update_font_preview(self):
        if self.font_preview_label is None:
            return
        family = self.font_family_combo.currentText()
        if not family:
            return
        preview_font = QFont(family, FONT_PREVIEW_POINT_SIZE)
        self.font_family_combo.setFont(preview_font)  # 現在値プレビュー
        self.font_preview_label.setFont(preview_font)  # サンプル文字プレビュー

    # 「フォント追加」処理 (resolve16 §4.2)
    # 対応拡張子(.ttf/.otf/.ttc)のみ受理し、settings/fonts へコピー→Qt 登録→一覧/プレビュー反映する。
    def _on_add_font(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "フォントファイルを選択", SETTINGS_DIR, FONT_ADD_FILTER
        )
        if not path:
            return
        # 対応拡張子以外は弾く (焼き込みで解決できない形式を防ぐ)
        if os.path.splitext(path)[1].lower() not in FONT_FILE_EXTENSIONS:
            _logger.warning("非対応フォントを拒否: %s", path)
            QMessageBox.warning(self, "非対応フォント", f"{FONT_ADD_NOTE}。")
            return
        # 格納ディレクトリ(settings/fonts)へコピーする
        try:
            os.makedirs(self._fonts_dir, exist_ok=True)
            dest = os.path.join(self._fonts_dir, os.path.basename(path))
            if os.path.abspath(dest) != os.path.abspath(path):
                shutil.copy2(path, dest)
        except OSError as e:
            _logger.warning("フォントのコピーに失敗: %s -> %s (%s)", path, self._fonts_dir, e)
            QMessageBox.warning(self, "追加失敗", f"フォントの追加に失敗しました。\n{e}")
            return
        # Qt へ登録し、追加フォントのファミリ名を取得する
        font_id = QFontDatabase.addApplicationFont(dest)
        if font_id < 0:
            _logger.warning("フォントの Qt 登録に失敗: %s", dest)
            QMessageBox.warning(self, "追加失敗", "フォントの読み込みに失敗しました。")
            return
        families = QFontDatabase.applicationFontFamilies(font_id)
        # 一覧を再構築し、追加フォントを選択状態にする
        self._reload_font_combo()
        if families:
            self._set_font_family(families[0])
        self._update_font_preview()
        shown = "、".join(families) if families else os.path.basename(dest)
        # 追加ファイル名・登録ファミリ名・格納先を INFO 出力する (resolve16 §6)
        _logger.info("フォント追加: %s -> %s (ファミリ: %s)", os.path.basename(path), dest, shown)
        QMessageBox.information(self, "追加完了", f"フォントを追加しました:\n{shown}")

    # 文字列を整数化する (空・非数値時は既定値を返す)
    def _to_int(self, text, default):
        try:
            return int(str(text).strip())
        except (TypeError, ValueError):
            return default

    # 文字列を浮動小数化する (空・非数値時は既定値を返す)
    def _to_float(self, text, default):
        try:
            return float(str(text).strip())
        except (TypeError, ValueError):
            return default

    # UI の入力値から保存用の設定辞書を組み立てる
    # UI 外のセクション (silence_cut/ffmpeg/logging) を破壊しないようマージする
    def _collect_settings(self):
        # 既存設定をベースにし、UI で扱う項目だけを上書きする
        base = getattr(self, "_loaded_settings", None) or load_settings()
        settings = {key: dict(value) for key, value in base.items() if isinstance(value, dict)}

        settings.setdefault("general", {}).update({
            "video_directory": self.video_dir_edit.text().strip(),
            "opening_video": self.opening_edit.text().strip(),
            "opening_enabled": self.opening_enabled_check.isChecked(),
            "ending_video": self.ending_edit.text().strip(),
            "ending_enabled": self.ending_enabled_check.isChecked(),
            "output_directory": self.output_dir_edit.text().strip(),
            "auto_update_check": self.auto_update_check_check.isChecked(),
        })
        # 無音音量閾値(dB)/無音最小継続時間 は resolve7 により UI から削除。
        # 既存値は base (load_settings) からそのまま引き継がれ上書きしない。
        settings.setdefault("silence_cut", {}).update({
            "enabled": self.silence_enabled_check.isChecked(),
            "fade_enabled": self.fade_enabled_check.isChecked(),
            "fade_duration_sec": self._to_float(self.fade_duration_edit.text(), 0.05),
        })
        settings.setdefault("subtitle", {}).update({
            "language": self.language_combo.currentText(),
            # テロップ役割別カラー (配信者/サブ/コメント)
            "own_subtitle_color": self.color_edit.text().strip(),
            "sub_subtitle_color": self.sub_color_edit.text().strip(),
            "comment_subtitle_color": self.comment_color_edit.text().strip(),
            "enabled": self.subtitle_enabled_check.isChecked(),
            "review_enabled": self.review_enabled_check.isChecked(),
            # 発話検出 (テロップ表示絞り込み)
            # 発話検出閾値(dB) は resolve7 により UI から削除 (既存値は base から引継ぎ)
            "speech_gate_enabled": self.speech_gate_enabled_check.isChecked(),
            "speech_min_duration_sec": self._to_float(
                self.speech_min_duration_edit.text(), 0.2
            ),
            "speech_gate_mode": self.speech_gate_mode_combo.currentData(),
            "speech_pad_sec": self._to_float(self.speech_pad_edit.text(), 0.1),
            # 音声認識 (Whisper) 実行デバイス/計算精度
            "whisper_device": self.whisper_device_combo.currentData(),
            "whisper_compute_type": self.whisper_compute_type_combo.currentData(),
            # フォント関係 (数値は既定値へフォールバック、色/フォントは文字列保存)
            "font_size": self._to_int(self.font_size_edit.text(), 48),
            "font_family": self.font_family_combo.currentText(),
            # 改行設定 (request11)
            "wrap_engine": self.wrap_engine_combo.currentData(),
            "min_line_length": self._to_int(self.min_line_length_edit.text(), 15),
            "max_line_length": self._to_int(self.max_line_length_edit.text(), 20),
            # アウトライン色 (配信者/サブ/コメント)
            "outline_color": self.outline_color_edit.text().strip(),
            "sub_outline_color": self.sub_outline_color_edit.text().strip(),
            "comment_outline_color": self.comment_outline_color_edit.text().strip(),
            "outline_width": self._to_int(self.outline_width_edit.text(), 3),
            "back_color": self.back_color_edit.text().strip(),
            "bold": self.bold_check.isChecked(),
            "italic": self.italic_check.isChecked(),
            "underline": self.underline_check.isChecked(),
            "strikeout": self.strikeout_check.isChecked(),
            "spacing": self._to_int(self.spacing_edit.text(), 0),
            "angle": self._to_int(self.angle_edit.text(), 0),
            "border_style": self.border_style_combo.currentData(),
            "alignment": self.alignment_combo.currentData(),
            "margin_l": self._to_int(self.margin_l_edit.text(), 40),
            "margin_r": self._to_int(self.margin_r_edit.text(), 40),
            "margin_v": self._to_int(self.margin_v_edit.text(), 60),
        })
        # 縦動画 (request14)。出力サイズ・フォント・改行・配置・余白の縦専用上書き値。
        settings.setdefault("vertical", {}).update({
            "enabled": self.vertical_enabled_check.isChecked(),
            "output_width": self._to_int(self.vertical_output_width_edit.text(), 1080),
            "output_height": self._to_int(self.vertical_output_height_edit.text(), 1920),
            "font_size": self._to_int(self.vertical_font_size_edit.text(), 90),
            "min_line_length": self._to_int(self.vertical_min_line_length_edit.text(), 10),
            "max_line_length": self._to_int(self.vertical_max_line_length_edit.text(), 15),
            "alignment": self.vertical_alignment_combo.currentData(),
            "margin_l": self._to_int(self.vertical_margin_l_edit.text(), 40),
            "margin_r": self._to_int(self.vertical_margin_r_edit.text(), 40),
            "margin_v": self._to_int(self.vertical_margin_v_edit.text(), 320),
        })
        # アーカイブ切り抜き: 本体の有効/無効 (メイン画面タブの表示可否) を保存。
        # archive セクションの他キー (download/scoring/output/clip_pipeline/combine) は
        # base から引き継ぎ、enabled と intro_card サブ辞書のみ UI 値で差し替える。
        settings.setdefault("archive", {})["enabled"] = self.archive_enabled_check.isChecked()
        settings["archive"]["intro_card"] = {
            "enabled": self.intro_enabled_check.isChecked(),
            "buffer_sec": self._to_float(self.intro_buffer_sec_edit.text(), 1.5),
            "blur_sigma": self._to_float(self.intro_blur_sigma_edit.text(), 18),
            "margin_top_px": self._to_int(self.intro_margin_top_edit.text(), 300),
            "margin_bottom_px": self._to_int(self.intro_margin_bottom_edit.text(), 300),
            "box_color": self.intro_box_color_edit.text().strip() or "black",
            "box_opacity": self._to_float(self.intro_box_opacity_edit.text(), 1.0),
            "title_font_family": self.intro_title_font_combo.currentData() or "",
            "title_font_size": self._to_int(self.intro_title_font_size_edit.text(), 112),
            "title_color": self.intro_title_color_edit.text().strip() or "#FFFFFF",
            "tag_font_family": self.intro_tag_font_combo.currentData() or "",
            "tag_font_size": self._to_int(self.intro_tag_font_size_edit.text(), 40),
            "tag_color": self.intro_tag_color_edit.text().strip() or "#FFFFFF",
            "tag_bg_opacity": self._to_float(self.intro_tag_bg_opacity_edit.text(), 0.45),
            "tag_margin_l": self._to_int(self.intro_tag_margin_l_edit.text(), 40),
            "tag_margin_v": self._to_int(self.intro_tag_margin_v_edit.text(), 30),
            "mute_intro": self.intro_mute_check.isChecked(),
        }
        return settings

    # 現在の UI 値を保存する (成功時 True を返す)
    # show_message=True のときのみ完了ダイアログを表示する
    def _save_current(self, show_message=True):
        settings = self._collect_settings()
        try:
            save_settings(settings)
            # 保存後の状態を loaded として保持しておく
            self._loaded_settings = settings
            if show_message:
                QMessageBox.information(self, "保存完了", "設定を保存しました。")
            return True
        except OSError as e:
            QMessageBox.critical(self, "保存エラー", f"保存に失敗しました。\n{e}")
            return False

    # 保存ボタン押下処理
    def _on_save(self):
        self._save_current(show_message=True)


# エントリーポイント
def main():
    app = QApplication(sys.argv)
    window = SettingsWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
