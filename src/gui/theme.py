# ガラスモーフィズムのテーマ (docs/request/ver3/resolve3.md)
#
# 本モジュールは「デザイントークンの唯一の出どころ」である (§3-3 / §4-2)。
# QSS が効く標準ウィジェットも、QPainter で自前描画する Timeline も、
# ここで定義した同じトークンを読むことで見た目を一貫させる。
#
# 主な責務:
#   * ライト / ダーク 2 セットのトークン保持 (R8 / §5.2)
#   * OS のライト/ダーク設定の判定と、実行中の切り替えへの追随 (§5.10)
#   * アプリ全体へ適用する QSS 1 枚の生成 (§5.3)
#   * QSS が効かない箇所の保険となる QPalette の生成
#   * ウィンドウ背景 (グラデーション + 光球) の描画 (§3-2)
#   * Windows のネイティブ背景効果 (Acrylic / Mica) の要求 (§5.4)
#
# 設計上の制約 (§3-1):
#   Qt Widgets には CSS の backdrop-filter に相当する機能が無いため、
#   ウィンドウ「内部」のパネルは背後をぼかせない。半透明 + 境界線 + ハイライトによる
#   「すりガラスの見立て」になる。本物のぼかしはウィンドウ背景に限りネイティブ効果で得る。
#
# 例外方針 (§7):
#   テーマの不具合でアプリが起動できない状態を作らない。
#   本モジュールの公開関数はすべて失敗しても素の見た目へフォールバックする。
import hashlib
import logging
import os
import re
import sys
import tempfile

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QLinearGradient,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QPolygonF,
    QRadialGradient,
)
from PySide6.QtWidgets import QApplication, QHBoxLayout, QWidget

# モジュールロガー (標準ライブラリのみ使用。アプリ実行時は上位ハンドラへ伝播する)
_logger = logging.getLogger(__name__)


# ==================================================================
# objectName 定数 (§5.2 公開 API)
# 「ガラスパネルにしたい箇所へ objectName を付ける」だけで QSS が当たる (§5.9)。
# ==================================================================

PANEL = "glassPanel"                # 半透明の薄いガラス (装飾用の面)
PANEL_STRONG = "glassPanelStrong"   # 文字・入力を載せる濃いめのガラス (§3-4)
NOTE = "noteLabel"                  # 補足テキスト (旧 color:#888 の置き換え)
TITLE = "sectionTitle"              # 見出し (旧 border-bottom 直書きの置き換え)
PRIMARY_BUTTON = "primaryButton"    # 主要動作 (グラデーション塗り / §5.2-2)
# 長い文言の主要動作。グラデーションは両端で明度が変わるため、文字量が多いボタンは
# 単色で塗って読みやすさを揃える (§5.2-2)。載せる文字色は accent.on が自動で選ぶ。
PRIMARY_BUTTON_SOLID = "primaryButtonSolid"
DANGER_BUTTON = "dangerButton"      # 破壊的動作 (輪郭のみ / §5.2-3)
PREVIEW_CANVAS = "previewCanvas"    # プレビューの映像領域 (テーマ対象外・常に黒 / §5.6)

# 画面中央に縦へ積むボタンの共通幅 (resolve4 §5.10 / ver3 resolve15 C8)。
# 用途は 2 つある。
#   ① 各タブの主要動作ボタン (クリップ用「実行」/ アーカイブ用「採点開始」)。
#      幅を明示しないと列に 1 つだけ残ったボタンが横幅いっぱいへ広がってしまう。
#   ② クリップ用タブの「続きから」(resolve15 §5.6-1)。実行ボタンの真上に並ぶため、
#      幅が違うと左右の端が揃わない。幅を 2 か所で別々に持つと片方だけ直したときに
#      再びずれるため、同じ定数を使う。
# 両タブから使うため theme に置く (archive_tab から main_window は import できない)。
PRIMARY_ACTION_BUTTON_WIDTH_PX = 96

# アイコン描画サイズ (px) と記号 (resolve4 §3-5)。
# 画像素材を増やさず Unicode 記号を QPixmap へ描いて QIcon 化する。
BUTTON_ICON_PX = 18
SETTINGS_GLYPH = "⚙"    # ⚙ 設定 (歯車)
RUN_GLYPH = "▶"         # ▶ 実行 / 採点開始 (再生)
# 記号グリフを確実に描画するためのフォント候補 (Windows 標準の記号フォント)。
# 既定 UI フォントは歯車(U+2699)等を持たない場合があるため明示する。
ICON_FONT_FAMILIES = ["Segoe UI Symbol", "Segoe UI Emoji", "Segoe UI"]

# チェック印・ドロップダウン矢印 (ver3 resolve15 §5.3)。
# QSS の ::indicator / ::down-arrow と必ず同じ値にすること (ずれると印が欠ける)。
CHECK_INDICATOR_PX = 14
COMBO_ARROW_PX = 9
COMBO_DROPDOWN_WIDTH_PX = 18

# タブ右上コーナーの下余白 (ver3 resolve15 D1)。Qt のレイアウト既定間隔と同じ 6px。
# QTabWidget はコーナーをタブバーの高さいっぱいに置くため、これが無いと
# ボタンの下辺がペイン (タブの中身の面) へ接する。
TAB_CORNER_BOTTOM_MARGIN_PX = 6

# ドラッグ&ドロップ領域 (ver3 resolve15 C1)
DROP_AREA = "dropArea"   # 破線のガラス面 (QSS の #dropArea)
DROP_GLYPH = "⬇"         # ⬇ ここへ落とす
CLEAR_GLYPH = "✕"        # ✕ 選択を消す
DROP_ICON_PX = 44
# D&D 領域の文言は画面の主役のため既定より 1 段大きくする (pt 加算)。
# 選択後は太字にして「案内文」と「選ばれた名前」を見分けやすくする。
DROP_TEXT_POINT_DELTA = 1


# ==================================================================
# setting.json の ui セクション既定 (§6)
# settings_window.DEFAULT_SETTINGS はここを取り込む (定義の重複を避けるため)。
# ==================================================================

DEFAULT_UI_SETTINGS = {
    # "glass" = ガラスモーフィズム (既定) / "system" = 従来の Qt 既定へ完全に戻す
    "theme": "glass",
    # 明暗 (R8 / §5.10)。"auto" = Windows の設定に追従 (既定) / "dark" / "light"
    "theme_mode": "auto",
    # Windows のネイティブすりガラス (§5.4)。効かない環境では自動的に無視される
    "native_backdrop": True,
    # 差し色。設計時は R9 に従いアイコン主色の赤 #DC0810 だったが、要望により
    # 暖色のオレンジ #E8A15C へ変更した。明るい色のため白文字は載らない
    # (白 2.17:1 / 黒 9.68:1) が、アクセント上の文字色 accent.on と
    # 線用の accent.line を自動導出するため、可読性の基準は満たされる (§3-4)。
    "accent_color": "#E8A15C",
    # ダークのみ: グラデーション終端に使う一段深いオレンジ (同色相 29°)。
    # 主色と色相を揃え、ボタンが差し色そのものとして見えるようにする (§5.2-2)。
    "accent_bright_dark": "#D9822B",
    # 形状 (両モード共通)
    "corner_radius_px": 16,
    "control_radius_px": 10,
    # ガラス層の不透明度。ライトとダークで意味が逆になる (§3-6-2)
    "glass": {
        "dark": {"bg": 0.07, "bg_strong": 0.12, "border": 0.16, "highlight": 0.28},
        "light": {"bg": 0.55, "bg_strong": 0.75, "border": 0.80, "highlight": 0.95},
    },
    # 背景 (§3-2 / §5.2-4)。アクセントを埋もれさせないため赤に寄せすぎない
    "background": {
        "dark": {"from": "#14111A", "to": "#221A20",
                 "glow_a": "#6E2A18", "glow_b": "#1E2440"},
        "light": {"from": "#F5F4F6", "to": "#EAE7EC",
                  "glow_a": "#F7DCD0", "glow_b": "#E2E4EF"},
        "opacity": 1.0,                 # ネイティブ効果が無いとき
        "opacity_with_backdrop": 0.55,  # ネイティブ効果が乗ったとき (デスクトップを透かす)
        # ウィンドウ自体を透過させてネイティブ効果を見せるか (§5.4 / Q6)。
        # true にすると本物のすりガラスが得られるが、環境によっては
        # 動画ウィジェットの描画に影響するため既定は false (安全側)。
        "translucent_window": False,
    },
    # Timeline は明暗に追従せず常にダーク (§3-6-3)。背景の不透明度だけ調整できる。
    # ライトとダークで値が違うのは §3-6-2 と同じ理由で、明るい下地の上では
    # 同じ不透明度だと「黒い背景」にならず、クリップと文字が沈むため
    # (ライト 0.55 ではラベルのコントラストが 3.45:1 まで落ち §3-4 の 4.5:1 を満たさない)。
    # 数値 1 つ (旧形式) を書いた setting.json も両モード共通値として受け付ける。
    "timeline_backdrop_opacity": {"dark": 0.55, "light": 0.82},
    # 可読性の下限。これを下回る不透明度は指定されても切り上げる (§3-4)
    "min_text_backdrop_opacity": 0.10,
    # メイン画面 (ver3 resolve15 §7)。
    # 従来 MainWindow は resize() を呼ばず中身に合わせて開いていたが、
    # 「画面上部の一定割合」を D&D 領域にするには基準になる高さが要るため初期サイズを持たせる。
    # timeline.ui.window_width / window_height と同じ考え方。
    "main_window": {
        "width_px": 700,             # 現行の最小幅 (実測 680) より狭いと効かない
        "height_px": 520,            # 比率の基準になる高さ
        # D&D 領域が占めるタブページ高の比率。
        # 当初は要望どおり 0.40 だったが、画面下部に余白が残るため 0.60 へ広げた
        # (request15 追加要望 / resolve15 C1)。
        "drop_zone_ratio": 0.60,
        "drop_zone_min_height_px": 140,   # 縮めたときの下限
        # 隠したファイル選択欄と「参照...」を出すか (resolve15 C2)。
        # 既定は非表示。true にすると従来どおりの行が戻る。
        "show_file_row": False,
    },
}


# ==================================================================
# デザイントークン (§5.2)
# ==================================================================

# 両モード共通 (形状)。数値は設定 corner_radius_px / control_radius_px で上書きされる。
SHAPE = {
    "radius.panel": "16px",
    "radius.control": "10px",
    "border.width": "1px",
}

DARK = {
    # ── 背景 (最背面。すりガラスが乗る土台 / §3-2)
    "bg.from":         "#14111A",   # 左上 (ほぼ無彩の暗色 / §5.2-4)
    "bg.to":           "#221A20",   # 右下
    "bg.glow.a":       "#6E2A18",   # 光球 1 (暖色・控えめ)
    "bg.glow.b":       "#1E2440",   # 光球 2 (寒色。背景が赤一色になるのを避ける)

    # ── ガラス層: 暗い下地に「白を薄く」乗せる (§3-6-2)
    "glass.bg":        "rgba(255,255,255,0.07)",
    "glass.bg.strong": "rgba(255,255,255,0.12)",   # 入力欄・文字の背後 (§3-4)
    "glass.bg.hover":  "rgba(255,255,255,0.14)",
    "glass.border":    "rgba(255,255,255,0.16)",
    "glass.highlight": "rgba(255,255,255,0.28)",   # 上辺のハイライト
    "glass.shadow":    "rgba(0,0,0,0.35)",

    # ── テキスト
    "text.primary":    "#F4F1F2",
    "text.secondary":  "#B9AEB2",
    "text.disabled":   "#7A6E72",

    # ── アクセント (オレンジ)。暗背景では 8.61:1 と十分に映える。
    # 実際の値は設定 ui.accent_color から解決される (ここは既定の控え / §5.2)
    "accent":          "#E8A15C",   # 塗り (載せる文字は accent.on が自動で黒を選ぶ)
    "accent.bright":   "#D9822B",   # 暗背景でのみ使うグラデーション終端
    "accent.hover":    "#FFB771",
    "accent.pressed":  "#C1864D",
    "danger":          "#E8A15C",   # 色では分けず形で分ける (§5.2-3)
    "warning":         "#FFC46B",
    "success":         "#6BE0A8",
}

LIGHT = {
    # ── 背景: 明るいグレー〜青白
    "bg.from":         "#F5F4F6",
    "bg.to":           "#EAE7EC",
    "bg.glow.a":       "#F7DCD0",   # 光球 (暖色・淡く)
    "bg.glow.b":       "#E2E4EF",   # 光球 (寒色)

    # ── ガラス層: 明るい下地では「白を濃く」乗せないと成立しない (§3-6-2)
    "glass.bg":        "rgba(255,255,255,0.55)",
    "glass.bg.strong": "rgba(255,255,255,0.75)",
    "glass.bg.hover":  "rgba(255,255,255,0.85)",
    "glass.border":    "rgba(255,255,255,0.80)",
    "glass.highlight": "rgba(255,255,255,0.95)",
    "glass.shadow":    "rgba(0,0,0,0.12)",         # ライトでは影を弱くする

    # ── テキスト
    "text.primary":    "#1B1B1F",
    "text.secondary":  "#5A555C",
    "text.disabled":   "#9A949B",

    # ── アクセント (オレンジ)。塗りはダークと同色を使う。
    # ただし明背景に対しては 1.98:1 しかないため、線・枠・フォーカスには
    # 自動で暗く寄せた accent.line (#B88049 = 3.08:1) が使われる (§3-4)
    "accent":          "#E8A15C",
    "accent.bright":   "#E8A15C",   # 明背景ではグラデーションを付けず主色のまま
    "accent.hover":    "#FFB771",
    "accent.pressed":  "#C1864D",
    "danger":          "#C1864D",   # 明背景では濃い方が読める
    "warning":         "#B26A00",
    "success":         "#1B7A4B",
}

# Timeline は常にダーク基調 (§3-6-3)。ライトモードでも切り替えない。
# ガラス化するのは背景だけで、クリップ・再生ヘッド・選択枠は timeline_view.py の
# 既存 QColor 定数をそのまま使う (R10 / §5.7-2)。
TIMELINE = {
    "timeline.bg":        "rgba(20,18,24,0.55)",   # トラック領域の下地 (ガラス)
    "timeline.track":     "rgba(255,255,255,0.04)",
    "timeline.track.alt": "rgba(255,255,255,0.06)",
    "timeline.ruler":     "rgba(255,255,255,0.08)",
    "timeline.grid":      "rgba(255,255,255,0.16)",
    "timeline.text":      "#ECECEC",
}


# ==================================================================
# 色ユーティリティ
# ==================================================================

# "rgba(r,g,b,a)" 表記を取り出す正規表現 (a は 0.0〜1.0)
_RGBA_RE = re.compile(
    r"^rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([0-9.]+)\s*\)$", re.IGNORECASE)


# トークン文字列 ("#RRGGBB" / "rgba(...)") を QColor へ変換する。
# 解釈できない場合は None を返す (呼び出し側で既定へ戻すため / §7)。
def parse_color(value):
    text = str(value or "").strip()
    match = _RGBA_RE.match(text)
    if match:
        red, green, blue = (int(match.group(i)) for i in (1, 2, 3))
        alpha = float(match.group(4))
        if not all(0 <= c <= 255 for c in (red, green, blue)):
            return None
        color = QColor(red, green, blue)
        color.setAlphaF(max(0.0, min(alpha, 1.0)))
        return color
    color = QColor(text)
    return color if color.isValid() else None


# QColor を "#RRGGBB" 文字列にする (不透明色のトークン生成用)
def _hex(color):
    return "#{:02X}{:02X}{:02X}".format(color.red(), color.green(), color.blue())


# 白を指定不透明度で重ねる QSS 文字列を作る (ガラス層のトークン生成用)
def _white(opacity):
    return "rgba(255,255,255,{:.3f})".format(max(0.0, min(float(opacity), 1.0)))


# sRGB 値 1 チャンネルを相対輝度成分へ変換する (WCAG の定義)
def _linear(channel):
    value = channel / 255.0
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


# 相対輝度 (WCAG 2.x)
def relative_luminance(color):
    return (0.2126 * _linear(color.red())
            + 0.7152 * _linear(color.green())
            + 0.0722 * _linear(color.blue()))


# 2 色のコントラスト比を返す (WCAG 2.x)。本文 4.5:1 / UI 部品 3:1 の判定に使う (§3-4)。
# 文字列でも QColor でも受け付ける。解釈できない色は 1.0 (最低値) を返す。
def contrast_ratio(color_a, color_b):
    first = color_a if isinstance(color_a, QColor) else parse_color(color_a)
    second = color_b if isinstance(color_b, QColor) else parse_color(color_b)
    if first is None or second is None:
        return 1.0
    lum_a = relative_luminance(first)
    lum_b = relative_luminance(second)
    lighter, darker = max(lum_a, lum_b), min(lum_a, lum_b)
    return (lighter + 0.05) / (darker + 0.05)


# ==================================================================
# 設定の読み取りと検証 (§7)
# ==================================================================

# 入れ子辞書を既定で補完した新しい辞書を返す (元の設定は書き換えない)
def _merge_defaults(user, defaults):
    merged = {}
    for key, default_value in defaults.items():
        value = user.get(key) if isinstance(user, dict) else None
        if isinstance(default_value, dict):
            if value is not None and not isinstance(value, dict):
                # 旧形式 (入れ子ではなく値 1 つ) の指定はそのまま渡し、
                # 各キーの検証関数で解釈させる (timeline_backdrop_opacity の後方互換)。
                merged[key] = value
            else:
                merged[key] = _merge_defaults(value if isinstance(value, dict) else {},
                                              default_value)
        else:
            merged[key] = default_value if value is None else value
    return merged


# 色指定を検証する。不正なら既定へ戻し WARNING を 1 度出す (§7 / §8)。
def _validated_color(value, default, key):
    if parse_color(value) is not None:
        return value
    _logger.warning('ui.%s が不正なため既定を使用します: "%s"', key, value)
    return default


# 不透明度を 0.0〜1.0 へ丸める。範囲外は WARNING (§7 / §8)。
def _validated_opacity(value, default, key):
    try:
        number = float(value)
    except (TypeError, ValueError):
        _logger.warning('ui.%s が不正なため既定を使用します: "%s"', key, value)
        return float(default)
    if number < 0.0 or number > 1.0:
        _logger.warning("ui.%s が範囲外のため 0.0〜1.0 へ丸めます: %s", key, number)
        return max(0.0, min(number, 1.0))
    return number


# 正の px を検証する。0 以下・非数値は既定へ戻す (ver3 resolve15 §7)。
# 半径 (0 を許す) とは別関数にしている。ウィンドウ幅 0 は破綻するため。
def _validated_size(value, default, key):
    try:
        number = int(value)
    except (TypeError, ValueError):
        _logger.warning('ui.%s が不正なため既定を使用します: "%s"', key, value)
        return int(default)
    if number <= 0:
        _logger.warning("ui.%s が 0 以下のため既定を使用します: %s", key, number)
        return int(default)
    return number


# 半径 (px) を検証する。負値・非数値は既定へ戻す。
def _validated_radius(value, default, key):
    try:
        number = int(value)
    except (TypeError, ValueError):
        _logger.warning('ui.%s が不正なため既定を使用します: "%s"', key, value)
        return int(default)
    if number < 0:
        _logger.warning("ui.%s が負のため既定を使用します: %s", key, number)
        return int(default)
    return number


# 直近に apply() へ渡された設定 (settings=None で呼ばれたときの既定として使う)
_current_settings = None

# 解決済みトークンのキャッシュ。paintEvent で多発するため 1 回だけ解決する (§8)。
_token_cache = {}

# 解決済み QColor のキャッシュ ((モード, トークン名) → QColor)。
# 自前描画ウィジェットは 1 フレームに何度も色を引くため、文字列解析を繰り返さない。
_color_cache = {}

# ネイティブ背景効果が実際に乗っているか (背景の不透明度の切り替えに使う / §5.4)
_backdrop_active = False

# ウィンドウを透過させたか (translucent_window が有効かつ効果適用に成功した場合のみ)
_translucent_active = False

# 「明暗が判定できない」INFO を繰り返さないためのフラグ (§8)
_unknown_scheme_logged = False

# テーマ適用前の QPalette (ui.theme = "system" へ戻すときに復元する / §4-4)
_original_palette = None

# 解決済みの明暗モード (settings 省略時のみ)。paintEvent から何度も引かれるため保持する。
_mode_cache = None


# 検証済み設定のキャッシュ (settings 省略時のみ)。
# paintEvent から色を引くたびに全キーを検証し直すと描画コストになり、
# 不正値の WARNING も毎フレーム出てしまうため 1 回だけ解決する (§8)。
_config_cache = None


# ui セクションを既定で補完し、検証済みの設定辞書を返す
def _config(settings=None):
    global _config_cache
    if settings is None and _config_cache is not None:
        return _config_cache
    resolved = _resolve_config(settings)
    if settings is None:
        _config_cache = resolved
    return resolved


# 設定の補完と検証の本体 (キャッシュを介さない)
def _resolve_config(settings=None):
    source = settings if settings is not None else _current_settings
    ui = source.get("ui") if isinstance(source, dict) else None
    cfg = _merge_defaults(ui if isinstance(ui, dict) else {}, DEFAULT_UI_SETTINGS)

    # 色
    cfg["accent_color"] = _validated_color(
        cfg["accent_color"], DEFAULT_UI_SETTINGS["accent_color"], "accent_color")
    cfg["accent_bright_dark"] = _validated_color(
        cfg["accent_bright_dark"], DEFAULT_UI_SETTINGS["accent_bright_dark"],
        "accent_bright_dark")
    for scheme in ("dark", "light"):
        for key in ("from", "to", "glow_a", "glow_b"):
            cfg["background"][scheme][key] = _validated_color(
                cfg["background"][scheme][key],
                DEFAULT_UI_SETTINGS["background"][scheme][key],
                "background.{}.{}".format(scheme, key))

    # 形状
    cfg["corner_radius_px"] = _validated_radius(
        cfg["corner_radius_px"], DEFAULT_UI_SETTINGS["corner_radius_px"],
        "corner_radius_px")
    cfg["control_radius_px"] = _validated_radius(
        cfg["control_radius_px"], DEFAULT_UI_SETTINGS["control_radius_px"],
        "control_radius_px")

    # 不透明度
    cfg["min_text_backdrop_opacity"] = _validated_opacity(
        cfg["min_text_backdrop_opacity"],
        DEFAULT_UI_SETTINGS["min_text_backdrop_opacity"], "min_text_backdrop_opacity")
    floor = cfg["min_text_backdrop_opacity"]
    for scheme in ("dark", "light"):
        for key in ("bg", "bg_strong", "border", "highlight"):
            cfg["glass"][scheme][key] = _validated_opacity(
                cfg["glass"][scheme][key], DEFAULT_UI_SETTINGS["glass"][scheme][key],
                "glass.{}.{}".format(scheme, key))
        # 文字・入力を載せる面が薄すぎると読めなくなるため下限へ切り上げる (§3-4)
        if cfg["glass"][scheme]["bg_strong"] < floor:
            _logger.warning("文字背後の不透明度を下限 %.2f へ切り上げました (指定: %.2f)",
                            floor, cfg["glass"][scheme]["bg_strong"])
            cfg["glass"][scheme]["bg_strong"] = floor
    for key in ("opacity", "opacity_with_backdrop"):
        cfg["background"][key] = _validated_opacity(
            cfg["background"][key], DEFAULT_UI_SETTINGS["background"][key],
            "background.{}".format(key))
    cfg["timeline_backdrop_opacity"] = _validated_timeline_opacity(
        cfg["timeline_backdrop_opacity"])

    # メイン画面 (ver3 resolve15 §7)
    window = cfg["main_window"]
    defaults = DEFAULT_UI_SETTINGS["main_window"]
    for key in ("width_px", "height_px", "drop_zone_min_height_px"):
        window[key] = _validated_size(
            window[key], defaults[key], "main_window.{}".format(key))
    window["drop_zone_ratio"] = _validated_opacity(
        window["drop_zone_ratio"], defaults["drop_zone_ratio"],
        "main_window.drop_zone_ratio")
    window["show_file_row"] = bool(window["show_file_row"])
    return cfg


# Timeline 背景の不透明度を {"dark": x, "light": y} 形式へ正規化する。
# 旧形式 (数値 1 つ) は両モード共通値として受け付ける (後方互換)。
def _validated_timeline_opacity(value):
    defaults = DEFAULT_UI_SETTINGS["timeline_backdrop_opacity"]
    if not isinstance(value, dict):
        shared = _validated_opacity(value, defaults["dark"], "timeline_backdrop_opacity")
        return {"dark": shared, "light": shared}
    return {
        scheme: _validated_opacity(
            value.get(scheme, defaults[scheme]), defaults[scheme],
            "timeline_backdrop_opacity.{}".format(scheme))
        for scheme in ("dark", "light")
    }


# ガラステーマが有効か (ui.theme = "system" のときは従来の Qt 既定へ完全に戻す / §7)
def is_enabled(settings=None):
    return _config(settings)["theme"] != "system"


# メイン画面の寸法設定を返す (ver3 resolve15 §7)。
# 初期サイズ・D&D 領域の比率・隠した行の表示可否をまとめて持つ。
# ui.theme = "system" でも寸法は使うため、is_enabled とは独立に返す。
def main_window_config(settings=None):
    return _config(settings)["main_window"]


# ==================================================================
# 明暗モードの判定 (§5.10-1)
# ==================================================================

# Windows のレジストリから「アプリのモード」を読む (§5.10-1 の 2 段目)。
# 1 ならライト・0 ならダーク。読めなければ None (Windows 以外・失敗時も None)。
def _read_windows_apps_theme():
    if sys.platform != "win32":
        return None
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            value, _type = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return "light" if int(value) == 1 else "dark"
    except Exception:  # noqa: BLE001 (レジストリが読めなくても既定へ倒すだけ)
        return None


# 現在の明暗モードを返す ("dark" | "light")。
# 設定 ui.theme_mode: "auto" (既定・OS 追従) / "dark" / "light"
def mode(settings=None):
    global _unknown_scheme_logged, _mode_cache
    if settings is None and _mode_cache is not None:
        return _mode_cache
    resolved = _resolve_mode(settings)
    if settings is None:
        _mode_cache = resolved
    return resolved


# 明暗モードの判定本体 (キャッシュを介さない)。
# OS 側で切り替わったときは invalidate_cache() でキャッシュを捨ててから引き直す (§5.10-2)。
def _resolve_mode(settings=None):
    global _unknown_scheme_logged
    configured = _config(settings)["theme_mode"]
    if configured in ("dark", "light"):
        return configured

    # 1) Qt に聞く (プラットフォームプラグインが OS 設定を解決してくれる)
    try:
        hints = QApplication.styleHints()
        if hints is not None:
            scheme = hints.colorScheme()
            if scheme == Qt.ColorScheme.Light:
                return "light"
            if scheme == Qt.ColorScheme.Dark:
                return "dark"
    except Exception:  # noqa: BLE001 (QApplication 未生成でも既定へ倒す)
        pass

    # 2) Unknown のときは Windows のレジストリを見る (§2.4)
    resolved = _read_windows_apps_theme()
    if resolved is not None:
        return resolved

    # 3) それも取れなければダーク (動画編集ソフトの慣例)
    if not _unknown_scheme_logged:
        _logger.info("OS の配色を判定できないためダークで表示します")
        _unknown_scheme_logged = True
    return "dark"


# ==================================================================
# トークンの解決 (§5.2)
# ==================================================================

# 解決済みトークンのキャッシュを捨てる。
# OS の明暗が切り替わったとき・設定を変えたときに呼ぶ (§5.10-2)。
def invalidate_cache():
    global _config_cache, _mode_cache
    _token_cache.clear()
    _color_cache.clear()
    # 記号アセットも色に依存するため引き直させる (ver3 resolve15 §5.3)。
    # ファイル自体は残るが、同じ色なら同じ名前になるため増え続けない。
    _asset_cache.clear()
    _config_cache = None
    _mode_cache = None


# 設定と明暗モードを反映したトークン辞書を返す。
# 戻り値は QSS へ埋め込める文字列 (色は "#RRGGBB" か "rgba(...)")。
def tokens(settings=None):
    current_mode = mode(settings)
    cached = _token_cache.get(current_mode)
    if cached is not None:
        return cached

    cfg = _config(settings)
    base = DARK if current_mode == "dark" else LIGHT
    resolved = dict(SHAPE)
    resolved.update(base)
    # Timeline は明暗に追従しない (§3-6-3)。常に同じ値を載せる。
    resolved.update(TIMELINE)

    # 形状を設定で上書き
    resolved["radius.panel"] = "{}px".format(cfg["corner_radius_px"])
    resolved["radius.control"] = "{}px".format(cfg["control_radius_px"])

    # 背景を設定で上書き
    background = cfg["background"][current_mode]
    resolved["bg.from"] = background["from"]
    resolved["bg.to"] = background["to"]
    resolved["bg.glow.a"] = background["glow_a"]
    resolved["bg.glow.b"] = background["glow_b"]

    # ガラス層の不透明度を設定で上書き (ライトとダークで意味が逆 / §3-6-2)
    glass = cfg["glass"][current_mode]
    resolved["glass.bg"] = _white(glass["bg"])
    resolved["glass.bg.strong"] = _white(glass["bg_strong"])
    # ホバーは「濃いガラス」より一段だけ濃くする (設定項目を増やさず追従させる)
    resolved["glass.bg.hover"] = _white(min(glass["bg_strong"] + 0.02, 1.0))
    resolved["glass.border"] = _white(glass["border"])
    resolved["glass.highlight"] = _white(glass["highlight"])

    # アクセントを設定で上書き (R9)。ライトでは朱橙が弱いため主色を使う (§5.2-1)
    accent = cfg["accent_color"]
    resolved["accent"] = accent
    resolved["accent.bright"] = (
        cfg["accent_bright_dark"] if current_mode == "dark" else accent)
    accent_color = parse_color(accent)
    resolved["accent.hover"] = _hex(accent_color.lighter(115))
    resolved["accent.pressed"] = _hex(accent_color.darker(120))
    resolved["danger"] = accent if current_mode == "dark" else _hex(accent_color.darker(120))

    # アクセントの上に載せる文字色 (白/黒のうち読める方を選ぶ / §3-4)。
    # 差し色を明るい色に変えても塗りボタンの文字が読めなくなることはない。
    resolved["accent.on"] = _on_accent(accent_color)

    # 線・枠・フォーカスに使うアクセント。背景と近すぎる場合だけ 3:1 まで寄せる。
    # 既に基準を満たしていれば主色のまま (ダークでは通常そのまま使われる)。
    background_color = parse_color(resolved["bg.from"])
    resolved["accent.line"] = _hex(_ensure_contrast(accent_color, background_color))
    resolved["danger.line"] = _hex(
        _ensure_contrast(parse_color(resolved["danger"]), background_color))

    # Timeline 背景の不透明度だけは設定で調整できる (§5.2-4)。
    # 色は常にダークのまま、明るい下地の上では濃く敷いて「黒い背景」を保つ (§3-6-3)。
    timeline_bg = parse_color(TIMELINE["timeline.bg"])
    timeline_bg.setAlphaF(cfg["timeline_backdrop_opacity"][current_mode])
    resolved["timeline.bg"] = "rgba({},{},{},{:.3f})".format(
        timeline_bg.red(), timeline_bg.green(), timeline_bg.blue(),
        timeline_bg.alphaF())

    _token_cache[current_mode] = resolved
    return resolved


# トークンを QColor で引く (自前描画ウィジェット用 / §3-3)。
# 未知の名前は不可視の透明色を返し、描画側を落とさない。
def color(name, settings=None):
    key = (mode(settings), name)
    cached = _color_cache.get(key)
    if cached is not None:
        return cached
    value = tokens(settings).get(name)
    if value is None:
        _logger.warning("未定義のテーマトークンが要求されました: %s", name)
        return QColor(0, 0, 0, 0)
    parsed = parse_color(value)
    resolved = parsed if parsed is not None else QColor(0, 0, 0, 0)
    _color_cache[key] = resolved
    return resolved


# ==================================================================
# QSS の生成 (§5.3)
# ==================================================================

# QSS テンプレート。{token.name} をトークンで置換する。
# CSS のブロック括弧は改行が続くため、プレースホルダ正規表現とは衝突しない。
_QSS_TEMPLATE = """
/* 土台。ウィンドウ自体は背景を描かない (paintEvent が描く / §3-2) */
QWidget { color: {text.primary}; }
QDialog, QMainWindow { background: transparent; }

/* ガラスパネル (objectName="glassPanel") */
#glassPanel {
    background-color: {glass.bg};
    border: {border.width} solid {glass.border};
    border-radius: {radius.panel};
}
/* 文字・入力を載せる濃いめのガラス (§3-4) */
#glassPanelStrong {
    background-color: {glass.bg.strong};
    border: {border.width} solid {glass.border};
    border-radius: {radius.panel};
}
/* 角丸を持たないガラスパネル (resolve4 E1)。
   分割ウィンドウの一区画のように、周囲といっぱいまで接する面に使う。
   #glassPanel / #glassPanelStrong は ID 指定のため、型 + 属性の規則より
   優先度が高い。ID を含めた規則を併記しないと角丸が外れない。 */
QWidget[flatPanel="true"],
#glassPanel[flatPanel="true"],
#glassPanelStrong[flatPanel="true"] { border-radius: 0px; }

/* ドック (アーカイブ画面の採点グラフ / ver3 resolve5 §5.9)。
   指定が無いとタイトルバーだけ OS 既定の明るい帯になり、ガラス面の中で浮く。
   閉じる・切り離しのボタンは既定のまま残す (消すと操作できなくなるため)。 */
QDockWidget { color: {text.primary}; }
QDockWidget::title {
    background: {glass.bg.strong};
    border: {border.width} solid {glass.border};
    padding: 4px 8px;
}

/* プレビューの映像領域は意図的にテーマ対象外 (§5.6)。
   映像の周囲に色を付けると焼き込み結果の色判断を誤らせるため、常に黒のままにする。
   ここに置くのは「インライン指定を撤去しつつ、テーマで塗り替えない」ことを明示するため。 */
#previewCanvas { background: #111111; color: #AAAAAA; border: none; }

/* 補足テキスト (旧 color:#888 の直書きを集約 / §5.3) */
#noteLabel { color: {text.secondary}; }
/* 見出し (旧 border-bottom: 1px solid #888 の直書きを集約) */
#sectionTitle {
    padding-top: 8px;
    border-bottom: {border.width} solid {glass.border};
}

QPushButton {
    background-color: {glass.bg.strong};
    border: {border.width} solid {glass.border};
    border-radius: {radius.control};
    padding: 6px 14px;
}
/* アイコンだけを載せるボタン (resolve4 §5.3)。
   既定の左右 14px は記号 (実測 12px 幅) に対して過剰で、見切れの原因になる。
   縦の余白と角丸は通常ボタンと揃え、横だけ詰める。
   塗り/輪郭の種別 (objectName) と独立に効かせるため動的プロパティで指定する。 */
QPushButton[iconOnly="true"] { padding: 6px 6px; }
QPushButton:hover    { background-color: {glass.bg.hover}; border-color: {glass.highlight}; }
QPushButton:pressed  { background-color: {accent.pressed}; color: {accent.on}; }
QPushButton:checked  { background-color: {accent}; color: {accent.on}; border-color: {accent}; }
QPushButton:disabled { color: {text.disabled}; background-color: {glass.bg}; }
QPushButton:default  { border-color: {accent.line}; }
/* 主要動作 (objectName="primaryButton") はアイコンのグラデーションで塗る (§5.2-2) */
#primaryButton {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                stop:0 {accent}, stop:1 {accent.bright});
    color: {accent.on};
    border: none;
}
#primaryButton:hover   { background: {accent.hover}; }
#primaryButton:pressed { background: {accent.pressed}; }
/* 長い文言の主要動作は単色で塗る (グラデーションの明るい端では白文字が読めない / §5.2-2) */
#primaryButtonSolid {
    background: {accent};
    color: {accent.on};
    border: none;
}
#primaryButtonSolid:hover   { background: {accent.hover}; }
#primaryButtonSolid:pressed { background: {accent.pressed}; }
/* 破壊的動作 (objectName="dangerButton") は塗らず輪郭で示す (§5.2-3) */
#dangerButton {
    background: transparent;
    color: {danger.line};
    border: {border.width} solid {danger.line};
}
#dangerButton:hover { background-color: {glass.bg.hover}; }

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background-color: {glass.bg.strong};
    border: {border.width} solid {glass.border};
    border-radius: {radius.control};
    padding: 4px 8px;
    selection-background-color: {accent};
    selection-color: {accent.on};
}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus { border-color: {accent.line}; }
QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled,
QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled { color: {text.disabled}; }
/* ドロップダウンの一覧はポップアップのため濃いガラスで塗る (背後が透けない) */
QComboBox QAbstractItemView {
    background-color: {glass.bg.strong};
    border: {border.width} solid {glass.border};
    selection-background-color: {accent};
    selection-color: {accent.on};
}
/* ドロップダウンの押しボタンを消し、矢印だけを見せる (ver3 resolve15 §2.3)。
   ::drop-down を書かないとスタイルが既定の押しボタンを描き、右端だけ一段明るい面
   (実測 #4F4F4F / 本体 #383838) になって浮き出て見える。部品カタログ
   (docs/layout/theme.css の .combo) は面を持たず三角だけのため、それに合わせる。
   矢印そのものは _QSS_ASSET_TEMPLATE で載せる (生成できなければ素のスタイルへ落ちる)。 */
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: {combo.dropdown.width};
    border: none;
    background: transparent;
}
QComboBox::down-arrow { width: {combo.arrow.size}; height: {combo.arrow.size}; }

/* ドラッグ&ドロップ領域 (ver3 resolve15 C1)。破線で「ここへ落とせる」ことを示す。
   ドラッグ中は dropActive でアクセント色へ変える (受理できることの合図)。 */
#dropArea {
    background-color: {glass.bg};
    border: {border.width} dashed {glass.border};
    border-radius: {radius.panel};
}
#dropArea[dropActive="true"] {
    background-color: {glass.bg.hover};
    border-color: {accent.line};
}

QTabWidget::pane {
    background: {glass.bg};
    border: {border.width} solid {glass.border};
    border-radius: {radius.panel};
}
/* 先頭タブが選ばれている間だけ、ペインの左上を角丸にしない (resolve4 M1 / S1)。
   タブバーは左端から始まるため、選択中の先頭タブとペインが繋がって見えるようにする。
   他のタブへ切り替えるとプロパティが false になり角丸へ戻る (回答 Q6 / §5.2-1)。 */
QTabWidget[firstTabSelected="true"]::pane { border-top-left-radius: 0px; }
QTabBar::tab {
    background: transparent;
    padding: 8px 18px;
    color: {text.secondary};
}
QTabBar::tab:selected {
    background: {glass.bg.strong};
    color: {text.primary};
    border-top-left-radius: {radius.control};
    border-top-right-radius: {radius.control};
}
QTabBar::tab:hover { color: {text.primary}; }

QTableWidget, QTableView, QListWidget, QListView, QTreeWidget, QTreeView {
    background: transparent;
    gridline-color: {glass.border};
    alternate-background-color: {glass.bg};
    selection-background-color: {accent};
    selection-color: {accent.on};
    border: {border.width} solid {glass.border};
    border-radius: {radius.control};
}
QHeaderView::section {
    background: {glass.bg.strong};
    border: none;
    border-bottom: {border.width} solid {glass.border};
    padding: 6px;
}

QProgressBar {
    background: {glass.bg};
    border: {border.width} solid {glass.border};
    border-radius: {radius.control};
    text-align: center;
}
QProgressBar::chunk {
    background: {accent.line};
    border-radius: {radius.control};
}

/* スクロールバー: 溝は透明にし、つまみだけをガラスにする。
   一部だけ QSS を当てると矢印が中途半端に残るため、増減ボタンは明示的に潰す。 */
QScrollBar:vertical   { background: transparent; width: 10px; margin: 0px; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0px; }
QScrollBar::handle {
    background: {glass.bg.hover};
    border-radius: 5px;
    min-width: 24px;
    min-height: 24px;
}
QScrollBar::handle:hover { background: {glass.highlight}; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0px; width: 0px; border: none; background: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }

QMenu {
    background: {glass.bg.strong};
    border: {border.width} solid {glass.border};
    border-radius: {radius.control};
}
QMenu::item { padding: 6px 18px; }
QMenu::item:selected { background: {accent.pressed}; color: {accent.on}; }
QMenuBar { background: transparent; }
QMenuBar::item:selected { background: {glass.bg.hover}; }

QToolTip {
    background: {glass.bg.strong};
    color: {text.primary};
    border: {border.width} solid {glass.border};
}

QGroupBox {
    border: {border.width} solid {glass.border};
    border-radius: {radius.panel};
    margin-top: 10px;
    padding-top: 8px;
}
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0px 4px; }

QSplitter::handle { background: {glass.border}; }
QSlider::groove:horizontal { background: {glass.bg.strong}; height: 4px; border-radius: 2px; }
QSlider::handle:horizontal {
    background: {accent.line};
    width: 12px;
    margin: -5px 0px;
    border-radius: 6px;
}
QCheckBox, QRadioButton, QLabel { background: transparent; }
QCheckBox:disabled, QRadioButton:disabled { color: {text.disabled}; }
/* チェック欄の枠 (ver3 resolve5)。QSS を当てた時点で Qt は既定の枠を描かなくなり、
   未チェックだと何も見えない = 押せる場所が分からなくなるため、ここで枠を描く。 */
QCheckBox::indicator {
    width: 14px;
    height: 14px;
    border: {border.width} solid {glass.border};
    border-radius: 3px;
    background: {glass.bg.strong};
}
QCheckBox::indicator:hover { border-color: {glass.highlight}; }
QCheckBox::indicator:checked { background: {accent}; border-color: {accent}; }
QCheckBox::indicator:disabled { border-color: {text.disabled}; }
"""

# 実行時に描いた PNG を使う規則 (ver3 resolve15 §5.2)。
# 生成に失敗したときは丸ごと出力しない。チェックは「印なしの塗り」(従来の見た目)、
# 矢印は素のスタイルへ落ちるだけで、起動は妨げない (§7 の方針)。
# パスは空白や日本語を含み得るため必ず引用符で囲む。
_QSS_ASSET_TEMPLATE = """
QCheckBox::indicator:checked { image: url("{asset.check}"); }
QComboBox::down-arrow { image: url("{asset.arrow}"); }
"""

# 色ではない寸法トークン (QSS のプレースホルダへ埋める / ver3 resolve15)。
# QSS の値と Python 側の定数が食い違わないよう、定数から組み立てる。
_SHAPE_EXTRA = {
    "combo.dropdown.width": "{}px".format(COMBO_DROPDOWN_WIDTH_PX),
    "combo.arrow.size": "{}px".format(COMBO_ARROW_PX),
}

# プレースホルダ (英小文字・数字・ドットのみ) を拾う。CSS の { 改行 } とは一致しない。
_PLACEHOLDER_RE = re.compile(r"\{([a-z][a-z0-9.]*)\}")


# ==================================================================
# 記号アセットの生成 (ver3 resolve15 §5.3)
#
# QSS の image: url(...) はファイルパスしか受け付けない (データ URI は使えない) ため、
# チェック印と下向き矢印を PNG へ描いて一時フォルダへ置き、そのパスを QSS へ埋め込む。
# 置き場所に tempfile を使うのは updater と同じ理由で、インストール先が
# 書き込み不可 (Program Files) でも成立させるため。
#
# 記号フォントではなく QPainter の図形で描くのは、14px の枠に収める印は
# フォント依存だと環境ごとに太さが揃わないため。
# ==================================================================

_ASSET_DIR_NAME = "stretheus_theme"
# 高 DPI 用に実寸の 3 倍で描き、QSS の width/height で縮めさせる
_ASSET_SCALE = 3

# 生成済みアセットのキャッシュ ((モード, 印の色, 矢印の色) → {トークン名: パス})。
# 色をキーに含めるのは、テーマやアクセント色を変えたときに取り違えないため。
_asset_cache = {}


# チェック印を描いた PNG を作りパスを返す (accent.on = アクセント塗りの上に載せる色)
def _write_check_png(directory, stamp, color_value):
    size = CHECK_INDICATOR_PX * _ASSET_SCALE
    path = os.path.join(directory, "check_{}_{}.png".format(size, stamp))
    if os.path.exists(path):
        return path
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(parse_color(color_value) or QColor("#000000"))
        pen.setWidthF(size * 0.18)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.drawPolyline(QPolygonF([
            QPointF(size * 0.20, size * 0.52),
            QPointF(size * 0.42, size * 0.74),
            QPointF(size * 0.80, size * 0.26),
        ]))
    finally:
        painter.end()
    if not pixmap.save(path, "PNG"):
        raise OSError("チェック印を保存できませんでした: {}".format(path))
    return path


# 下向き三角を描いた PNG を作りパスを返す (色は text.secondary = カタログと同じ)
def _write_arrow_png(directory, stamp, color_value):
    size = COMBO_ARROW_PX * _ASSET_SCALE
    path = os.path.join(directory, "arrow_{}_{}.png".format(size, stamp))
    if os.path.exists(path):
        return path
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setBrush(parse_color(color_value) or QColor("#000000"))
        painter.setPen(Qt.NoPen)
        painter.drawPolygon(QPolygonF([
            QPointF(size * 0.08, size * 0.30),
            QPointF(size * 0.92, size * 0.30),
            QPointF(size * 0.50, size * 0.74),
        ]))
    finally:
        painter.end()
    if not pixmap.save(path, "PNG"):
        raise OSError("ドロップダウンの矢印を保存できませんでした: {}".format(path))
    return path


# チェック印・矢印の PNG を用意し、QSS へ埋めるパスを返す。
# 作れなければ {} を返し、呼び出し側は image: の規則ごと出力しない (§3-2)。
def _indicator_assets(settings=None):
    resolved = tokens(settings)
    key = (mode(settings), resolved["accent.on"], resolved["text.secondary"])
    cached = _asset_cache.get(key)
    if cached is not None:
        return cached
    try:
        directory = os.path.join(tempfile.gettempdir(), _ASSET_DIR_NAME)
        os.makedirs(directory, exist_ok=True)
        stamp = hashlib.md5("|".join(key).encode("utf-8")).hexdigest()[:8]
        paths = {
            # QSS の url() は / 区切りで書く (Windows の \ はエスケープ扱いになる)
            "asset.check": _write_check_png(directory, stamp, key[1]).replace("\\", "/"),
            "asset.arrow": _write_arrow_png(directory, stamp, key[2]).replace("\\", "/"),
        }
    except Exception:  # noqa: BLE001 (印が出ないだけで起動は妨げない / §3-2)
        _logger.exception("チェック印・矢印の生成に失敗しました (印なしで続行します)")
        return {}
    _asset_cache[key] = paths
    return paths


# トークンを埋め込んだ QSS 文字列を返す (§5.3)
def build_qss(settings=None):
    resolved = dict(tokens(settings))
    resolved.update(_SHAPE_EXTRA)

    def replace(match):
        name = match.group(1)
        value = resolved.get(name)
        if value is None:
            _logger.warning("QSS に未定義のトークンが含まれています: %s", name)
            return match.group(0)
        return value

    qss = _PLACEHOLDER_RE.sub(replace, _QSS_TEMPLATE)
    # 記号の画像が作れたときだけ image: の規則を足す (ver3 resolve15 §3-2)
    assets = _indicator_assets(settings)
    if assets:
        resolved.update(assets)
        qss += _PLACEHOLDER_RE.sub(replace, _QSS_ASSET_TEMPLATE)
    return qss


# QSS が効かない箇所の保険となる QPalette を返す
def build_palette(settings=None):
    resolved = tokens(settings)
    palette = QPalette()
    text = parse_color(resolved["text.primary"])
    disabled = parse_color(resolved["text.disabled"])
    accent = parse_color(resolved["accent"])
    window = parse_color(resolved["bg.from"])
    # 入力欄などの「面」はガラスを不透明化した近似色を使う (パレットは α を扱えないため)
    base = _flatten(parse_color(resolved["glass.bg.strong"]), window)

    palette.setColor(QPalette.Window, window)
    palette.setColor(QPalette.WindowText, text)
    palette.setColor(QPalette.Base, base)
    palette.setColor(QPalette.AlternateBase, _flatten(
        parse_color(resolved["glass.bg"]), window))
    palette.setColor(QPalette.Text, text)
    palette.setColor(QPalette.Button, base)
    palette.setColor(QPalette.ButtonText, text)
    palette.setColor(QPalette.ToolTipBase, base)
    palette.setColor(QPalette.ToolTipText, text)
    palette.setColor(QPalette.Highlight, accent)
    # 選択中の文字色もアクセントに合わせて自動で選ぶ (白文字を直書きしない / §3-4)
    palette.setColor(QPalette.HighlightedText, parse_color(resolved["accent.on"]))
    palette.setColor(QPalette.Link, accent)
    palette.setColor(QPalette.PlaceholderText, parse_color(resolved["text.secondary"]))
    for group in (QPalette.Disabled,):
        palette.setColor(group, QPalette.WindowText, disabled)
        palette.setColor(group, QPalette.Text, disabled)
        palette.setColor(group, QPalette.ButtonText, disabled)
    return palette


# アクセントの上に載せる文字色を決める (白か黒のうちコントラストが高い方)。
# 差し色は setting.json で変更できるため、白文字を直書きすると明るい差し色
# (例 #E8A15C は白文字 2.17:1) で本文基準 4.5:1 を満たせなくなる。
# 「可読性は装飾より優先する」(§3-4) に従い、色ではなく比率で選ぶ。
_ON_ACCENT_LIGHT = "#FFFFFF"
_ON_ACCENT_DARK = "#1B1B1F"


def _on_accent(accent):
    light_ratio = contrast_ratio(QColor(_ON_ACCENT_LIGHT), accent)
    dark_ratio = contrast_ratio(QColor(_ON_ACCENT_DARK), accent)
    return _ON_ACCENT_LIGHT if light_ratio >= dark_ratio else _ON_ACCENT_DARK


# 背景に対して最低限のコントラストを満たすまで色を寄せた QColor を返す。
# 差し色を線・枠・フォーカス表示に使うとき、背景と近いと見えなくなるため
# (例 #E8A15C はライト背景 #F5F4F6 に対し 1.98:1)、UI 部品の基準 3:1 まで
# 明るい背景なら暗く、暗い背景なら明るく寄せる。色相は変えない。
_UI_MIN_CONTRAST = 3.0
_CONTRAST_STEPS = 64            # 明度を寄せる分割数
# 明背景か暗背景かの分かれ目 (知覚上の中間グレーの相対輝度)
_MID_LUMINANCE = 0.18


def _ensure_contrast(color_value, background, min_ratio=_UI_MIN_CONTRAST):
    if contrast_ratio(color_value, background) >= min_ratio:
        return color_value
    # 寄せる向きは「背景が明るいか暗いか」だけで決める。
    # 2 色の明暗を比べて決めると、背景よりわずかに明るい色を更に明るくしてしまい
    # (例 明背景 #F5F4F6 に対する #FAF7F2)、いつまでも基準へ届かない。
    toward_black = relative_luminance(background) > _MID_LUMINANCE
    target = QColor(0, 0, 0) if toward_black else QColor(255, 255, 255)
    adjusted = QColor(color_value)
    for step in range(1, _CONTRAST_STEPS + 1):
        ratio = step / _CONTRAST_STEPS
        adjusted = QColor(
            int(round(color_value.red() * (1 - ratio) + target.red() * ratio)),
            int(round(color_value.green() * (1 - ratio) + target.green() * ratio)),
            int(round(color_value.blue() * (1 - ratio) + target.blue() * ratio)),
        )
        if contrast_ratio(adjusted, background) >= min_ratio:
            return adjusted
    return adjusted


# 2 つのトークンを source-over で 1 色へ畳んだ QColor を返す (§5.8)。
# 半透明の層を 2 枚重ねて塗ると 1 ピクセルあたり 2 回の合成が走る。
# あらかじめ 1 色へ畳んでおけば塗りが 1 回で済み、見た目は完全に同じになる
# (source-over は結合的なため、畳んだ色を下地へ重ねた結果は 2 枚重ねと一致する)。
def composite_color(top_name, bottom_name, settings=None):
    key = (mode(settings), "composite", top_name, bottom_name)
    cached = _color_cache.get(key)
    if cached is not None:
        return cached
    top = color(top_name, settings)
    bottom = color(bottom_name, settings)
    top_alpha = top.alphaF()
    bottom_alpha = bottom.alphaF()
    out_alpha = top_alpha + bottom_alpha * (1.0 - top_alpha)
    if out_alpha <= 0.0:
        resolved = QColor(0, 0, 0, 0)
    else:
        def channel(top_value, bottom_value):
            blended = (top_value * top_alpha
                       + bottom_value * bottom_alpha * (1.0 - top_alpha)) / out_alpha
            return int(round(max(0.0, min(blended, 255.0))))

        resolved = QColor(channel(top.red(), bottom.red()),
                          channel(top.green(), bottom.green()),
                          channel(top.blue(), bottom.blue()))
        resolved.setAlphaF(min(out_alpha, 1.0))
    _color_cache[key] = resolved
    return resolved


# 半透明色を下地の上に合成した不透明色を返す (パレット用)
def _flatten(overlay, backdrop):
    alpha = overlay.alphaF()
    return QColor(
        int(overlay.red() * alpha + backdrop.red() * (1 - alpha)),
        int(overlay.green() * alpha + backdrop.green() * (1 - alpha)),
        int(overlay.blue() * alpha + backdrop.blue() * (1 - alpha)),
    )


# ==================================================================
# 適用 (§5.9)
# ==================================================================

# アプリ全体へテーマを適用する (QSS + パレット)。何度呼んでも安全 (§3-6-4)。
# ui.theme = "system" の場合は QSS を外し、従来の Qt 既定へ完全に戻す (§7)。
def apply(app, settings=None):
    global _current_settings, _original_palette
    if settings is not None:
        _current_settings = settings
    invalidate_cache()
    # 初回に素のパレットを控えておく (system へ戻すときに復元するため / §4-4)
    if _original_palette is None:
        _original_palette = QPalette(app.palette())
    try:
        if not is_enabled(settings):
            app.setStyleSheet("")
            app.setPalette(_original_palette)
            _logger.info("テーマを適用: system (従来の Qt 既定)")
            return False
        current_mode = mode(settings)
        app.setPalette(build_palette(settings))
        app.setStyleSheet(build_qss(settings))
        configured = _config(settings)["theme_mode"]
        _logger.info(
            "テーマを適用: glass / %s (%s) / ネイティブ背景効果: %s",
            "ライト" if current_mode == "light" else "ダーク",
            "OS 設定に追従" if configured == "auto" else "設定で固定",
            "有効" if _backdrop_active else "無効",
        )
        return True
    except Exception:  # noqa: BLE001 (見た目の失敗で起動を妨げない / §7)
        _logger.exception("テーマの適用に失敗したため素の見た目で起動します")
        try:
            app.setStyleSheet("")
            if _original_palette is not None:
                app.setPalette(_original_palette)
        except Exception:  # noqa: BLE001
            pass
        return False


# OS の明暗切り替えを購読する (R8 / §5.10-2)。設定が "auto" のときだけ効く。
# on_changed は引数なしで呼ばれる。購読できたら True。
def watch_color_scheme(app, on_changed, settings=None):
    if _config(settings)["theme_mode"] != "auto":
        return False
    try:
        app.styleHints().colorSchemeChanged.connect(lambda _scheme: on_changed())
        return True
    except Exception:  # noqa: BLE001 (購読できなくても起動時の判定は効く)
        _logger.info("OS の配色変更を購読できないため起動時の判定のみで表示します")
        return False


# ==================================================================
# ネイティブのすりガラス (Windows / §3-1 B / §5.4)
# ==================================================================

# DWM の属性 ID (dwmapi.h)
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20   # タイトルバーをダークにする
_DWMWA_CAPTION_COLOR = 35             # タイトルバーの色 (§3-5 A)
_DWMWA_SYSTEMBACKDROP_TYPE = 38       # Windows 11 22621+
_DWMSBT_TRANSIENTWINDOW = 3           # Acrylic 相当


# QColor を DWM の COLORREF (0x00BBGGRR) へ変換する
def _colorref(qcolor):
    return (qcolor.blue() << 16) | (qcolor.green() << 8) | qcolor.red()


# ウィンドウへネイティブのすりガラスを要求する (Windows のみ / §5.4)。
# 失敗しても致命的ではない (グラデーション背景で成立する) ため False を返すだけにする。
#
# 注意: ウィンドウ自体を透過させないと Acrylic は視認できない。透過は動画ウィジェットの
#   描画に影響し得るため、設定 ui.background.translucent_window で明示的に有効にした
#   ときだけ行う (既定 false / Q6 の実機確認後に判断する)。
def apply_native_backdrop(widget, settings=None):
    global _backdrop_active, _translucent_active
    cfg = _config(settings)
    if not is_enabled(settings) or not cfg["native_backdrop"] or sys.platform != "win32":
        return False
    try:
        import ctypes

        # 透過はネイティブウィンドウ生成前に設定する (生成後だとウィンドウが作り直される)
        want_translucent = bool(cfg["background"]["translucent_window"])
        if want_translucent and not widget.testAttribute(Qt.WA_TranslucentBackground):
            widget.setAttribute(Qt.WA_TranslucentBackground, True)

        hwnd = int(widget.winId())
        dwm = ctypes.windll.dwmapi
        current_mode = mode(settings)

        # 1) タイトルバーの明暗を本文と揃える
        dark = ctypes.c_int(1 if current_mode == "dark" else 0)
        dwm.DwmSetWindowAttribute(
            hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE,
            ctypes.byref(dark), ctypes.sizeof(dark))

        # 2) システム背景効果 (Acrylic 相当) を要求する
        backdrop = ctypes.c_int(_DWMSBT_TRANSIENTWINDOW)
        result = dwm.DwmSetWindowAttribute(
            hwnd, _DWMWA_SYSTEMBACKDROP_TYPE,
            ctypes.byref(backdrop), ctypes.sizeof(backdrop))

        # 3) タイトルバーの色を背景グラデーションの始点に合わせる (§3-5 A)
        caption = ctypes.c_int(_colorref(color("bg.from", settings)))
        dwm.DwmSetWindowAttribute(
            hwnd, _DWMWA_CAPTION_COLOR,
            ctypes.byref(caption), ctypes.sizeof(caption))

        if result != 0:  # S_OK 以外は古い Windows (22621 未満)
            _logger.info("ネイティブの背景効果は利用できません (グラデーション背景で表示します)")
            _backdrop_active = False
            _translucent_active = False
            return False
        _backdrop_active = True
        _translucent_active = want_translucent
        return True
    except Exception:  # noqa: BLE001 (非 Windows / API 失敗はグラデーションで成立する)
        _logger.info("ネイティブの背景効果は利用できません (グラデーション背景で表示します)")
        _backdrop_active = False
        _translucent_active = False
        return False


# ==================================================================
# 背景の描画 (§3-2)
# ==================================================================

# 光球の中心位置 (幅・高さに対する比) と半径 (長辺に対する比)。
# アクセントを埋もれさせないため暖色は控えめ・寒色を対に置く (§5.2-4)。
_GLOW_A = (0.22, 0.16, 0.62, 0.55)   # (cx 比, cy 比, 半径比, 中心の不透明度)
_GLOW_B = (0.84, 0.82, 0.58, 0.45)


# 背景グラデーションを描く (各ウィンドウの paintEvent から呼ぶ / §3-2)。
# ui.theme = "system" のときは何も描かない (従来の見た目のまま)。
def paint_background(painter, rect, settings=None):
    if not is_enabled(settings):
        return
    try:
        rectf = QRectF(rect)
        if rectf.isEmpty():
            return
        cfg = _config(settings)
        opacity = (cfg["background"]["opacity_with_backdrop"]
                   if _translucent_active else cfg["background"]["opacity"])

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)

        # 斜めグラデーション (左上 → 右下)
        # 透過ウィンドウでは既に描かれた不透明画素を置き換える必要があるため Source で塗る
        if _translucent_active:
            painter.setCompositionMode(QPainter.CompositionMode_Source)
        gradient = QLinearGradient(rectf.topLeft(), rectf.bottomRight())
        gradient.setColorAt(0.0, _with_opacity(color("bg.from", settings), opacity))
        gradient.setColorAt(1.0, _with_opacity(color("bg.to", settings), opacity))
        painter.fillRect(rectf, QBrush(gradient))

        # 光球 (アクセントの光を散らす。上に重ねるので通常合成に戻す)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        _paint_glow(painter, rectf, color("bg.glow.a", settings), _GLOW_A, opacity)
        _paint_glow(painter, rectf, color("bg.glow.b", settings), _GLOW_B, opacity)
        painter.restore()
    except Exception:  # noqa: BLE001 (背景が描けなくても画面は成立する / §7)
        _logger.exception("背景の描画に失敗しました")


# 放射グラデーションの光球を 1 つ描く
def _paint_glow(painter, rectf, glow_color, spec, opacity):
    cx_ratio, cy_ratio, radius_ratio, alpha = spec
    center = QPointF(rectf.x() + rectf.width() * cx_ratio,
                     rectf.y() + rectf.height() * cy_ratio)
    radius = max(rectf.width(), rectf.height()) * radius_ratio
    if radius <= 0:
        return
    gradient = QRadialGradient(center, radius)
    gradient.setColorAt(0.0, _with_opacity(glow_color, alpha * opacity))
    gradient.setColorAt(1.0, _with_opacity(glow_color, 0.0))
    painter.fillRect(rectf, QBrush(gradient))


# 指定不透明度を掛けた QColor の複製を返す
def _with_opacity(qcolor, opacity):
    result = QColor(qcolor)
    result.setAlphaF(max(0.0, min(qcolor.alphaF() * float(opacity), 1.0)))
    return result


# ==================================================================
# ウィジェットへの適用ヘルパ (§5.9)
# ==================================================================

# ウィンドウ背景 (グラデーション + 光球) を描くイベントフィルタ。
# 各ウィンドウへ paintEvent を書き足す代わりに、これを 1 行取り付けるだけで済ませる。
# Qt は塗り潰し済みの背景を描いてから QPaintEvent を配送するため、
# フィルタ側で先に描いた背景の上に本来の描画が乗る。
class _BackgroundPainter(QObject):

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Paint and watched.isVisible():
            painter = QPainter(watched)
            paint_background(painter, watched.rect())
            painter.end()
        return False  # 本来の paintEvent も実行させる


# ウィンドウへ背景描画を取り付ける (トップレベルのウィンドウ/ダイアログ専用)。
# 二重取り付けを避けるため、取り付け済みなら何もしない。
def install_window_background(widget):
    if not is_enabled():
        return
    if getattr(widget, "_glass_background_filter", None) is not None:
        return
    painter_filter = _BackgroundPainter(widget)
    widget.installEventFilter(painter_filter)
    # ガベージコレクトを防ぐためウィジェット側で参照を持つ
    widget._glass_background_filter = painter_filter


# ウィジェットをガラスパネルとして扱う (§5.9)。
# 素の QWidget は QSS の background-color を描かないため WA_StyledBackground を立てる。
# rounded=False で角丸を外す (resolve4 E1)。塗りの濃さ (strong) と独立に指定できる。
def mark_panel(widget, strong=False, rounded=True):
    widget.setObjectName(PANEL_STRONG if strong else PANEL)
    widget.setAttribute(Qt.WA_StyledBackground, True)
    widget.setProperty("flatPanel", not rounded)


# アイコンのみのボタンとして扱う (左右の余白を詰める / resolve4 §5.3)。
# 記号やアイコンが見切れるのを防ぐ。塗り (primaryButton) との併用もできる。
def mark_icon_button(button):
    button.setProperty("iconOnly", True)


# 先頭タブ選択時にペイン左上の角丸を外す (resolve4 M1 / S1 / §5.2)。
# QSS には「どのタブが選択中か」を表す状態が無いため、動的プロパティで橋渡しする。
# 取り付けた時点の選択状態も反映するので、生成直後に 1 度呼ぶだけでよい。
def bind_tab_pane_corner(tab_widget):
    def sync(index):
        try:
            tab_widget.setProperty("firstTabSelected", index == 0)
            # プロパティの変更は自動では QSS へ反映されないため再評価させる。
            # 切り替えのたびに両方向で呼ぶことで、他タブでは角丸へ戻る (回答 Q6)。
            tab_widget.style().unpolish(tab_widget)
            tab_widget.style().polish(tab_widget)
            tab_widget.update()
        except Exception:  # noqa: BLE001 (角丸が変わらないだけでタブ操作は妨げない)
            _logger.exception("タブの角丸切り替えに失敗しました")

    tab_widget.currentChanged.connect(sync)
    sync(tab_widget.currentIndex())


# タブ右上のコーナーへウィジェットを置く (ver3 resolve15 D1)。
# QTabWidget はコーナーをタブバーの高さいっぱいに置くため、直接入れると
# ボタンの下辺がペインへ接する。QSS では余白を作れない (QTabBar の規則はタブにしか
# 効かない) ため、余白付きの入れ物で包んでから渡す。
# 戻り値は入れ物。呼び出し側は包んだウィジェット自身の参照をそのまま使い続けられる。
def install_tab_corner(tab_widget, widget, corner=Qt.TopRightCorner):
    holder = QWidget(tab_widget)
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, TAB_CORNER_BOTTOM_MARGIN_PX)
    layout.setSpacing(0)
    layout.addWidget(widget)
    tab_widget.setCornerWidget(holder, corner)
    return holder


# Unicode 記号を指定色で描画して QIcon 化する (resolve4 §3-5)。
# 画像素材を追加せずボタンをアイコン表示にするためのヘルパ。
# メイン画面とアーカイブ用タブの双方から使うため theme に置く
# (archive_tab から main_window は import できない = 循環するため)。
def glyph_icon(glyph, color_value, size_px=BUTTON_ICON_PX):
    pixmap = QPixmap(size_px, size_px)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setPen(color_value)
    font = painter.font()
    font.setFamilies(ICON_FONT_FAMILIES)
    font.setPointSizeF(size_px * 0.72)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignCenter, glyph)
    painter.end()
    return QIcon(pixmap)


# 主要動作ボタン (実行 / 採点開始) の見た目を揃える (resolve4 §5.10)。
# アクセント塗り + アイコンのみ + 同一幅。載せるアイコンの色は accent.on を使う。
def setup_primary_action_button(button, glyph, tooltip):
    mark_primary(button)
    mark_icon_button(button)
    button.setToolTip(tooltip)
    button.setFixedWidth(PRIMARY_ACTION_BUTTON_WIDTH_PX)
    button.setIconSize(QSize(BUTTON_ICON_PX, BUTTON_ICON_PX))
    refresh_primary_action_icon(button, glyph)


# 主要動作ボタンのアイコンを現在のテーマ色で描き直す (resolve4 §5.10-3)。
# アイコンは QPixmap へ焼き込むため、OS の明暗が切り替わったら作り直す必要がある。
def refresh_primary_action_icon(button, glyph):
    if is_enabled():
        icon_color = color("accent.on")
    else:
        # ui.theme = "system" では従来どおりパレットのボタン文字色を使う
        icon_color = button.palette().color(QPalette.ButtonText)
    button.setIcon(glyph_icon(glyph, icon_color))


# 補足テキストのラベルとして扱う (旧 color:#888 の置き換え)
def mark_note(label):
    label.setObjectName(NOTE)


# 見出しラベルとして扱う (旧 border-bottom 直書きの置き換え)
def mark_title(label):
    label.setObjectName(TITLE)


# 主要動作のボタンとして扱う (§5.2-2)。
# solid=True は長い文言のボタン用 (グラデーションだと白文字のコントラストが足りない)。
def mark_primary(button, solid=False):
    button.setObjectName(PRIMARY_BUTTON_SOLID if solid else PRIMARY_BUTTON)


# 破壊的動作のボタンとして扱う (§5.2-3)
def mark_danger(button):
    button.setObjectName(DANGER_BUTTON)


# プレビューの映像領域として扱う (テーマで塗り替えない黒面 / §5.6)
def mark_preview_canvas(widget):
    widget.setObjectName(PREVIEW_CANVAS)
    widget.setAttribute(Qt.WA_StyledBackground, True)


# 開いているすべてのトップレベルウィンドウへ貼り替えを反映する (§5.10-2)。
# QSS を再適用しただけでは自前描画ウィジェットが更新されないため、明示的に再描画させる。
def refresh_all_windows(app):
    try:
        for widget in app.topLevelWidgets():
            widget.setStyleSheet(widget.styleSheet())  # QSS の再評価を促す
            widget.update()
    except Exception:  # noqa: BLE001 (再描画に失敗しても状態は失われない)
        _logger.exception("テーマ貼り替え後の再描画に失敗しました")
