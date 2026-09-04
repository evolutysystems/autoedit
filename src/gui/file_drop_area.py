# ドラッグ&ドロップでファイルを選ぶ領域 (docs/request/ver3/resolve15.md §5.5)
#
# 何も選ばれていないときは案内文とアイコンを出し、選ばれたら名前と × ボタンへ変える。
# 落とす操作そのものは親 (タブ) が受け取る。この部品は「今何が選ばれているか」を
# 見せるだけで、ファイルの妥当性判断や実行には関与しない。
#
# 種別 (動画 / プロジェクト) も知らない。呼び出し側が名前と補足を決めて渡すため、
# アーカイブ切り抜き用タブへもそのまま載せられる (resolve15 §11 F1)。
import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import theme


class FileDropArea(QWidget):

    # × が押された (実際に選択を消すかどうかは親が決める)
    cleared = Signal()

    # placeholder: 未選択のときに出す案内文
    # hint       : 案内文の下に出す補足 (未指定なら行ごと出さない)
    def __init__(self, placeholder, hint="", parent=None):
        super().__init__(parent)
        self._placeholder = placeholder
        self._default_hint = hint
        self._name = ""
        self._build_ui()
        self.clear()

    def _build_ui(self):
        # QSS の #dropArea (破線のガラス面) を当てる。
        # WA_StyledBackground が無いと素の QWidget は背景を描かない (mark_panel と同じ作法)。
        self.setObjectName(theme.DROP_AREA)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)
        root.addStretch(1)

        # ドラッグ&ドロップを表すアイコン (未選択のときだけ出す)
        self.icon_label = QLabel()
        self.icon_label.setAlignment(Qt.AlignCenter)
        root.addWidget(self.icon_label)

        # 文言 + × ボタンの行 (中央そろえ)
        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        name_row.addStretch(1)
        self.text_label = QLabel()
        self.text_label.setAlignment(Qt.AlignCenter)
        # 画面の主役のため既定より 1 段大きくする (theme.DROP_TEXT_POINT_DELTA)
        text_font = self.text_label.font()
        text_font.setPointSize(max(text_font.pointSize(), 1)
                               + theme.DROP_TEXT_POINT_DELTA)
        self.text_label.setFont(text_font)
        name_row.addWidget(self.text_label)
        self.clear_button = QPushButton()
        self.clear_button.setToolTip("選択を取り消す")
        theme.mark_icon_button(self.clear_button)
        self.clear_button.clicked.connect(self.cleared.emit)
        name_row.addWidget(self.clear_button)
        name_row.addStretch(1)
        root.addLayout(name_row)

        # 補足 (何が選べるか / 実行すると何が起きるか)
        self.hint_label = QLabel()
        self.hint_label.setAlignment(Qt.AlignCenter)
        theme.mark_note(self.hint_label)
        root.addWidget(self.hint_label)
        root.addStretch(1)

        self.refresh_theme()

    # ------------------------------------------------------------------
    # 表示の切り替え
    # ------------------------------------------------------------------

    # 選択された名前を出し、× ボタンを表示する。
    # name    : 表示名 (通常はファイル名)
    # tooltip : フルパス (同名ファイルを見分けるため必ず渡す)
    # hint    : 補足の差し替え (未指定なら既定の補足へ戻す)
    def set_selection(self, name, tooltip="", hint=""):
        self._name = name or ""
        self.icon_label.setVisible(False)
        self.clear_button.setVisible(True)
        self.text_label.setToolTip(tooltip or name or "")
        self.hint_label.setText(hint or self._default_hint)
        self._update_text()

    # 案内文へ戻し、× ボタンを隠す
    def clear(self):
        self._name = ""
        self.icon_label.setVisible(True)
        self.clear_button.setVisible(False)
        self.text_label.setToolTip("")
        self.hint_label.setText(self._default_hint)
        self._update_text()

    def has_selection(self):
        return bool(self._name)

    # 今出している文言 (テストと状態表示の確認用)
    def text(self):
        return self._name or self._placeholder

    # ドラッグ中の見た目にする (QSS の #dropArea[dropActive="true"])
    def set_drop_active(self, active):
        if self.property("dropActive") == bool(active):
            return
        self.setProperty("dropActive", bool(active))
        # 動的プロパティの変更は自動では QSS へ反映されないため再評価させる
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    # 実行中は選択を変えさせない (× を無効化する)
    def set_busy(self, busy):
        self.clear_button.setEnabled(not busy)

    # 明暗の切り替えでアイコンを描き直す (QPixmap へ焼き込むため追随しない / resolve3 §5.10-2)
    def refresh_theme(self):
        icon_color = theme.color("text.secondary")
        self.icon_label.setPixmap(
            theme.glyph_icon(theme.DROP_GLYPH, icon_color, theme.DROP_ICON_PX)
            .pixmap(theme.DROP_ICON_PX, theme.DROP_ICON_PX))
        self.clear_button.setIcon(
            theme.glyph_icon(theme.CLEAR_GLYPH, theme.color("text.primary")))

    # ------------------------------------------------------------------
    # 文言の描画 (長い名前は幅に合わせて省略する)
    # ------------------------------------------------------------------

    # 幅が変わったら省略位置を計算し直す
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_text()

    # 名前 (または案内文) を、入る幅へ収まるよう中央省略して表示する。
    # 省略してもフルパスはツールチップに残るため情報は失われない。
    def _update_text(self):
        text = self.text()
        # 選ばれた名前は太字、案内文は通常 (どちらの状態か一目で分かるようにする)
        font = self.text_label.font()
        if font.bold() != self.has_selection():
            font.setBold(self.has_selection())
            self.text_label.setFont(font)
        # × ボタンと左右の余白を除いた、文言に使える幅
        available = max(self.width() - 32 - (self.clear_button.width()
                                             if self.clear_button.isVisible() else 0), 80)
        metrics = self.text_label.fontMetrics()
        self.text_label.setText(metrics.elidedText(text, Qt.ElideMiddle, available))

    # 表示名にする (パスならファイル名だけを取り出す)
    @staticmethod
    def display_name(path):
        return os.path.basename(path) if path else ""
