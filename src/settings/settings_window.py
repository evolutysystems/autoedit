import json
import logging
import os
import shutil
import sys
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIntValidator
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
        "comment_label": "コメント：",
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
            # R1 はローカル mp4 指定のみ。twitch-dl 取得は R3 で追加する。
            "downloader": "local",
            "work_dir": "archive_work",
        },
        "scoring": {
            "method": "A",
            "top_n": 5,
            "window_sec": 300,          # 方式A: 5分窓
            "slide_sec": 60,            # 方式A: 1分スライド (= セル幅)
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
        # 切り抜き出力ファイル名の接頭辞 (flow17 R1)
        "output": {
            "clip_prefix": "archive",
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
    },
}

# スタイル定数
TITLE_FONT_SIZE = 14
# 項目名ラベルの最小幅 (長い文言は自動で広がるため最小値のみ指定)
COLUMN_LABEL_WIDTH = 100
# 入力欄の最小幅 (横幅全体を支配するため小さめに設定して画面を縮小する)
INPUT_FIELD_MIN_WIDTH = 160


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
    return changed


# 読み込んだデータに不足項目があればデフォルトで補完する
def _merge_with_defaults(data):
    merged = DEFAULT_SETTINGS.copy()
    for section, defaults in DEFAULT_SETTINGS.items():
        merged[section] = defaults.copy()
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

        # 配信者カラー (旧「文字色」= own_subtitle_color) — HTML #RRGGBB 形式 (アルファ無し)
        self.color_edit = QLineEdit()
        self.color_edit.setPlaceholderText(PLACEHOLDER_COLOR)
        grid.addWidget(self._make_column_label("配信者カラー"), row, 0)
        grid.addLayout(self._make_color_picker(self.color_edit, with_alpha=False), row, 1)
        row += 1

        # サブカラー (sub_subtitle_color) — HTML #RRGGBB 形式 (アルファ無し)
        self.sub_color_edit = QLineEdit()
        self.sub_color_edit.setPlaceholderText(PLACEHOLDER_SUB_COLOR)
        grid.addWidget(self._make_column_label("サブカラー"), row, 0)
        grid.addLayout(self._make_color_picker(self.sub_color_edit, with_alpha=False), row, 1)
        row += 1

        # コメントカラー (comment_subtitle_color) — HTML #RRGGBB 形式 (アルファ無し)
        self.comment_color_edit = QLineEdit()
        self.comment_color_edit.setPlaceholderText(PLACEHOLDER_COMMENT_COLOR)
        grid.addWidget(self._make_column_label("コメントカラー"), row, 0)
        grid.addLayout(self._make_color_picker(self.comment_color_edit, with_alpha=False), row, 1)
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
        font_note_label.setStyleSheet("color:#888; font-size:11px;")
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

        # 配信者アウトラインカラー (旧「アウトラインの色」= outline_color)
        # ASS 形式 &HAABBGGRR / アルファ有り
        self.outline_color_edit = QLineEdit()
        self.outline_color_edit.setPlaceholderText(PLACEHOLDER_OUTLINE_COLOR)
        grid.addWidget(self._make_column_label("配信者アウトラインカラー"), row, 0)
        grid.addLayout(self._make_color_picker(self.outline_color_edit, with_alpha=True), row, 1)
        row += 1

        # サブアウトラインカラー (sub_outline_color) — ASS 形式 &HAABBGGRR / アルファ有り
        self.sub_outline_color_edit = QLineEdit()
        self.sub_outline_color_edit.setPlaceholderText(PLACEHOLDER_OUTLINE_COLOR)
        grid.addWidget(self._make_column_label("サブアウトラインカラー"), row, 0)
        grid.addLayout(self._make_color_picker(self.sub_outline_color_edit, with_alpha=True), row, 1)
        row += 1

        # コメントアウトラインカラー (comment_outline_color) — ASS 形式 &HAABBGGRR / アルファ有り
        self.comment_outline_color_edit = QLineEdit()
        self.comment_outline_color_edit.setPlaceholderText(PLACEHOLDER_OUTLINE_COLOR)
        grid.addWidget(self._make_column_label("コメントアウトラインカラー"), row, 0)
        grid.addLayout(self._make_color_picker(self.comment_outline_color_edit, with_alpha=True), row, 1)
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

    # タイトル用ラベルを生成する
    def _make_title(self, text):
        label = QLabel(text)
        font = QFont()
        font.setBold(True)
        font.setPointSize(TITLE_FONT_SIZE)
        label.setFont(font)
        label.setStyleSheet("padding-top: 8px; border-bottom: 1px solid #888;")
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
    def _make_color_picker(self, line_edit, with_alpha):
        line_edit.setMinimumWidth(INPUT_FIELD_MIN_WIDTH)
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
    def _parse_color(self, text, with_alpha):
        s = (text or "").strip()
        try:
            if with_alpha:
                # ASS: &HAABBGGRR (BGR 並び・アルファ反転)。6桁(アルファ省略)は補完受理する
                hex_ = s[2:] if s.upper().startswith("&H") else s
                hex_ = hex_.rjust(8, "0")[-8:]
                aa, bb, gg, rr = hex_[0:2], hex_[2:4], hex_[4:6], hex_[6:8]
                color = QColor(int(rr, 16), int(gg, 16), int(bb, 16))
                color.setAlpha(255 - int(aa, 16))  # ASS→Qt はアルファ反転
                return color
            # HTML: #RRGGBB
            color = QColor(s if s.startswith("#") else "#" + s)
            return color if color.isValid() else QColor(PLACEHOLDER_COLOR)
        except (ValueError, TypeError):
            # フォールバック既定色(プレースホルダー定数を流用しハードコードを避ける)
            fallback = PLACEHOLDER_OUTLINE_COLOR if with_alpha else PLACEHOLDER_COLOR
            return self._parse_color(fallback, with_alpha)

    # QColor を設定保存形式の文字列へ変換する
    # with_alpha=True: ASS &HAABBGGRR / False: HTML #RRGGBB
    def _format_color(self, color, with_alpha):
        if with_alpha:
            aa = 255 - color.alpha()  # Qt→ASS はアルファ反転
            return "&H{:02X}{:02X}{:02X}{:02X}".format(
                aa, color.blue(), color.green(), color.red()
            )
        return "#{:02X}{:02X}{:02X}".format(color.red(), color.green(), color.blue())

    # ボタン上の色見本を現在の入力値で塗り替える(手入力・ピッカー双方に追従)
    def _update_swatch(self, button, line_edit, with_alpha):
        color = self._parse_color(line_edit.text(), with_alpha)
        # 不透明度はボタン背景では無視し、色相のみ提示(視認性優先)
        button.setStyleSheet(
            f"background-color: {color.name()}; min-width: 28px; border: 1px solid #888;"
        )
        button.setText("")  # 色面のみ。ラベルは項目名側で表現

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
