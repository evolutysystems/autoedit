# 色の入力部品と色文字列の変換 (docs/request/ver3/resolve6.md §5.4)
#
# 設定画面 (settings_window) と Timeline のインスペクタで同じ形式・同じ見た目の
# 色入力を使うため、変換ロジックと小部品をここへ集約する。
# 設定画面の従来の挙動を変えないよう、変換関数は元の実装と同じ規則で書いてある。
#
# 色の形式は 2 種類ある (setting.json と同じ使い分け):
#   with_alpha=False : HTML  #RRGGBB      … 塗り (文字色)
#   with_alpha=True  : ASS   &HAABBGGRR   … 縁・背景 (BGR 並び・アルファは反転)
from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QPushButton, QWidget

from . import theme

# 解釈できない値のときに使う色 (呼び出し側が fallback を渡さなかった場合)
_FALLBACK_COLOR = "#FFFFFF"
_FALLBACK_OUTLINE_COLOR = "&H00000000"

# 色見本の枠線 (テーマ無効時)。glass.border は半透明のため縁取りにならない。
_SWATCH_BORDER_FALLBACK = "#888"

# 「既定 (指定なし)」を表す空欄のときにボタンへ出す文字
EMPTY_LABEL = "既定"


# 設定文字列を QColor へ変換する (不正・空欄時は fallback を返す)
# with_alpha=True: ASS &HAABBGGRR (BGR 並び・アルファ反転) / False: HTML #RRGGBB
def parse_color(text, with_alpha, fallback=None):
    s = (text or "").strip()
    try:
        if with_alpha:
            # ASS: &HAABBGGRR。6桁(アルファ省略)は補完受理する
            hex_ = s[2:] if s.upper().startswith("&H") else s
            hex_ = hex_.rjust(8, "0")[-8:]
            aa, bb, gg, rr = hex_[0:2], hex_[2:4], hex_[4:6], hex_[6:8]
            color = QColor(int(rr, 16), int(gg, 16), int(bb, 16))
            color.setAlpha(255 - int(aa, 16))  # ASS→Qt はアルファ反転
            return color
        # HTML: #RRGGBB
        color = QColor(s if s.startswith("#") else "#" + s)
        if color.isValid():
            return color
        return QColor(fallback or _FALLBACK_COLOR)
    except (ValueError, TypeError):
        # 再帰で既定値を解釈し直す (fallback 自体が不正なら定数へ倒れる)
        default = fallback or (_FALLBACK_OUTLINE_COLOR if with_alpha else _FALLBACK_COLOR)
        if default != (text or "").strip():
            return parse_color(default, with_alpha)
        return QColor(_FALLBACK_COLOR)


# QColor を保存形式の文字列へ変換する
# with_alpha=True: ASS &HAABBGGRR / False: HTML #RRGGBB
def format_color(color, with_alpha):
    if with_alpha:
        aa = 255 - color.alpha()  # Qt→ASS はアルファ反転
        return "&H{:02X}{:02X}{:02X}{:02X}".format(
            aa, color.blue(), color.green(), color.red())
    return "#{:02X}{:02X}{:02X}".format(color.red(), color.green(), color.blue())


# ボタンを色見本として塗る
# ここはテーマの適用対象外 (resolve3 §5.5)。塗りは「利用者が設定した色そのもの」を
# 見せる機能であり、テーマで塗り替えると設定値が見えなくなる。枠線だけテーマから引く。
# allow_empty=True かつ空欄のときは「既定」と分かる破線の枠で示す (resolve6 §5.4)。
def apply_swatch(button, text, with_alpha, fallback=None, min_width_px=28,
                 allow_empty=False):
    border = (theme.color("text.secondary").name() if theme.is_enabled()
              else _SWATCH_BORDER_FALLBACK)
    if allow_empty and not (text or "").strip():
        button.setStyleSheet(
            f"background-color: transparent; min-width: {int(min_width_px)}px; "
            f"border: 1px dashed {border};")
        button.setText("")
        return
    color = parse_color(text, with_alpha, fallback)
    # 不透明度はボタン背景では無視し、色相のみ提示 (視認性優先)
    button.setStyleSheet(
        f"background-color: {color.name()}; min-width: {int(min_width_px)}px; "
        f"border: 1px solid {border};")
    button.setText("")  # 色面のみ。ラベルは項目名側で表現


# 色見本ボタン + 入力欄 + 「既定へ」ボタンを 1 つにまとめた部品 (resolve6 §5.4)
# 空文字は「指定なし = 既定の色に従う」を表す。
class ColorField(QWidget):

    # 利用者が値を確定したときだけ出す (1 打鍵ごとに Undo 履歴を埋めないため)
    color_committed = Signal(str)

    # with_alpha     : True=ASS &HAABBGGRR / False=HTML #RRGGBB
    # swatch_width_px: 色見本ボタンの幅
    # placeholder    : 入力欄に薄く出す例 (空欄時の意味を伝える)
    def __init__(self, with_alpha, swatch_width_px=28, placeholder="",
                 parent=None):
        super().__init__(parent)
        self._with_alpha = bool(with_alpha)
        self._swatch_width = int(swatch_width_px)
        self._value = ""
        self._updating = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self._swatch = QPushButton()
        self._swatch.setFixedWidth(self._swatch_width)
        self._swatch.setToolTip("色を選ぶ")
        self._swatch.clicked.connect(self._choose_color)
        layout.addWidget(self._swatch)

        self._edit = QLineEdit()
        self._edit.setPlaceholderText(placeholder or EMPTY_LABEL)
        # 手入力にも色見本を追従させる (確定は editingFinished のときだけ)
        self._edit.textChanged.connect(self._refresh_swatch)
        self._edit.editingFinished.connect(self._commit_from_edit)
        layout.addWidget(self._edit, 1)

        self._clear_button = QPushButton(EMPTY_LABEL + "へ")
        self._clear_button.setToolTip("個別指定をやめて既定の色に戻します")
        self._clear_button.clicked.connect(self._clear)
        layout.addWidget(self._clear_button)

        self._refresh_swatch()

    # ------------------------------------------------------------------
    # 値
    # ------------------------------------------------------------------

    # 現在の値 ("" = 既定)
    def value(self):
        return self._value

    # 画面から値を流し込む (color_committed は出さない)
    def set_value(self, text):
        self._updating = True
        try:
            self._value = str(text or "")
            self._edit.setText(self._value)
            self._refresh_swatch()
        finally:
            self._updating = False

    # ------------------------------------------------------------------
    # 操作
    # ------------------------------------------------------------------

    def _choose_color(self):
        # 遅延 import: 画面を開くまで QColorDialog を読み込まない
        from PySide6.QtWidgets import QColorDialog

        initial = parse_color(self._value, self._with_alpha)
        if self._with_alpha:
            # 縁・背景は透明度も選べるようにする
            options = QColorDialog.ColorDialogOption.ShowAlphaChannel
        else:
            options = QColorDialog.ColorDialogOption(0)
        color = QColorDialog.getColor(initial, self, "色を選択", options)
        if color.isValid():
            self._commit(format_color(color, self._with_alpha))

    def _clear(self):
        self._commit("")

    def _commit_from_edit(self):
        self._commit(self._edit.text().strip())

    # 値を確定して通知する (変化が無ければ何もしない)
    def _commit(self, text):
        if self._updating:
            return
        value = str(text or "")
        if value == self._value:
            return
        self._value = value
        self._updating = True
        try:
            self._edit.setText(value)
        finally:
            self._updating = False
        self._refresh_swatch()
        self.color_committed.emit(value)

    def _refresh_swatch(self):
        apply_swatch(self._swatch, self._edit.text(), self._with_alpha,
                     min_width_px=self._swatch_width, allow_empty=True)
