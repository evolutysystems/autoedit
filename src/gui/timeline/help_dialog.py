# Timeline 編集画面の取り扱い説明 (docs/request/ver9/resolve19.md)
# 「？」ボタンから開く説明画像のビューア。
#
# 説明は画像 (既定 src/t1.jpg / src/t2.jpg) を縦に並べて見せるだけで、
# 編集・書き出しの処理には一切関わらない。
# 画像が読めないときも画面を壊さず、欠けたページを案内文へ置き換える (§3.5)。
import os
import sys

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...i18n import tr
from ...utils.logger import get_logger
from .. import theme

_logger = get_logger(__name__)


# 説明画像の置き場の基準ディレクトリを返す (§3.4)
# (comment_decor.resolve_icon_path / blur.models._base_dir と同じ考え方)
# 非凍結: src/              … このファイルは src/gui/timeline/ なので 3 つ上
# 凍結  : sys._MEIPASS/src  … .spec の datas で同梱したもの (§3.6)
def _base_dir():
    if getattr(sys, "frozen", False):
        return os.path.join(getattr(sys, "_MEIPASS", ""), "src")
    here = os.path.dirname(os.path.abspath(__file__))        # src/gui/timeline
    return os.path.dirname(os.path.dirname(here))            # src


# 設定のファイル名 (src/ からの相対、または絶対パス) を実体パスへ直す (§3.4)
# 見つからなければ None を返す (欠けたページは案内文に置き換える / §3.5)。
# 相対指定が src/ の外へ出るものは受け付けない
# (設定経由で任意のファイルを開く口にしないため)。
def resolve_image_path(name):
    text = str(name or "").strip()
    if not text:
        return None
    if os.path.isabs(text):
        return text if os.path.exists(text) else None
    base = os.path.normpath(_base_dir())
    path = os.path.normpath(os.path.join(base, text))
    try:
        inside = os.path.commonpath([base, path]) == base
    except ValueError:      # ドライブが違う等で比較できない場合は外と見なす
        inside = False
    if not inside:
        _logger.warning("説明画像の指定が src の外を指しています: %r", name)
        return None
    return path if os.path.exists(path) else None


# 「？」ボタンを出すか (§3.2 / §3.4)
# 設定の enabled が真で、かつ読める画像が 1 枚以上あること。
# 「押しても何も起きないボタン」を残さないため、判定をここへ 1 本化する。
def is_enabled(help_cfg):
    cfg = help_cfg or {}
    if not cfg.get("enabled", True):
        return False
    return any(resolve_image_path(name) for name in (cfg.get("images") or ()))


# 「？」ボタンを作ってボタン行の末尾へ置く (§3.2)
# 置き場所は「元に戻す」の左 (要望 R2) のため、**呼び出し側は「元に戻す」を
# 足す前にこれを呼ぶ**。説明を出せないときは何も置かず None を返す。
#
# button_row : 置き先の QHBoxLayout
# help_cfg   : timeline_config(settings)["help"]
# on_clicked : 押されたときに呼ぶもの (ダイアログを開く処理)
def install_button(button_row, help_cfg, on_clicked, parent=None):
    cfg = help_cfg or {}
    if not is_enabled(cfg):
        return None
    button = QPushButton("？", parent)
    # Enter の取りこぼしで説明が開かないようにする (既存の保存・ぼかしと同じ)
    button.setAutoDefault(False)
    button.setToolTip(tr("この画面の使い方を開きます"))
    # 記号 1 文字ぶんの幅。iconOnly 扱いで左右の余白を詰める (ver3 resolve4 §5.3)
    button.setFixedWidth(max(int(cfg.get("button_width_px", 32) or 32), 1))
    theme.mark_icon_button(button)
    button.clicked.connect(on_clicked)
    button_row.addWidget(button)
    return button


# 説明画像 1 枚を「幅に合わせて縮めて」描くラベル (§3.3)
# 原寸 (1920x1080) のまま貼ると窓に収まらないため、与えられた幅に合わせて
# 縦横比を保って縮小する。拡大はしない (粗くなるだけで読みやすくならない)。
class _HelpPage(QLabel):

    # pixmap       : 元の画像 (縮小前。貼り直しのため原寸で持ち続ける)
    # min_width_px : これ未満には縮めない幅。下回る窓では横スクロールで見せる
    def __init__(self, pixmap, min_width_px, parent=None):
        super().__init__(parent)
        self._source = pixmap
        self._min_width_px = max(int(min_width_px), 1)
        # 直前に配った幅。同じ幅で呼ばれたら貼り直さないための覚え
        self._applied_width = 0
        self.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.setMinimumWidth(self._min_width_px)

    # 表示幅を決めて貼り直す。呼び出し側 (ダイアログ) が resize のたびに呼ぶ。
    # 同じ幅で呼ばれたら何もしない (resizeEvent の再入で無限に貼り直さないため)。
    def apply_width(self, width_px):
        width = max(int(width_px), self._min_width_px)
        width = min(width, self._source.width())          # 等倍を上限にする
        if width == self._applied_width:
            return
        self._applied_width = width
        scaled = self._source.scaledToWidth(width, Qt.SmoothTransformation)
        self.setPixmap(scaled)
        # 高さは縮小後の実寸に合わせる (縦スクロールの総量がこれで決まる)
        self.setFixedHeight(scaled.height())

    # 現在貼ってある幅 (テスト用 / §6 T5)
    def applied_width(self):
        return self._applied_width


# 取り扱い説明の画面 (§3.3)
# help_cfg: timeline_config(settings)["help"]
class TimelineHelpDialog(QDialog):

    # 初期化
    def __init__(self, help_cfg, parent=None):
        super().__init__(parent)
        self._cfg = help_cfg or {}
        self._margin_px = max(int(self._cfg.get("image_margin_px", 12) or 0), 0)
        self._pages = []
        # 貼り直しの最中に入り直さないための印 (下の _relayout_pages)
        self._relayouting = False
        self.setWindowTitle(tr("使い方"))
        # ガラスダイアログ化 (既存ダイアログと同じ / volume_threshold_dialog)
        theme.install_window_background(self)
        self.setMinimumSize(
            max(int(self._cfg.get("window_min_width_px", 480) or 1), 1),
            max(int(self._cfg.get("window_min_height_px", 360) or 1), 1))
        self.resize(
            max(int(self._cfg.get("window_width", 980) or 1), 1),
            max(int(self._cfg.get("window_height", 760) or 1), 1))
        self._build_ui()

    # 画面構築
    def _build_ui(self):
        root = QVBoxLayout(self)

        # スクロールできる器 (要望 R5)。縦横ともバーは必要なときだけ出す。
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        # viewport は既定で不透明に塗られるため、ガラス背景を透かす
        self._scroll.viewport().setAutoFillBackground(False)

        container = QWidget()
        container.setAutoFillBackground(False)
        pages_layout = QVBoxLayout(container)
        pages_layout.setContentsMargins(
            self._margin_px, self._margin_px, self._margin_px, self._margin_px)
        pages_layout.setSpacing(
            max(int(self._cfg.get("image_spacing_px", 12) or 0), 0))

        # 設定の順に 1 枚ずつ並べる (要望 R4)
        for name in (self._cfg.get("images") or ()):
            widget = self._build_page(name)
            if widget is not None:
                pages_layout.addWidget(widget)
        # 画像が縦に短いときに上へ寄せる
        pages_layout.addStretch(1)

        self._scroll.setWidget(container)
        root.addWidget(self._scroll, 1)

        # 閉じる
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self.close_button = QPushButton(tr("閉じる"))
        self.close_button.setDefault(True)
        self.close_button.clicked.connect(self.accept)
        button_row.addWidget(self.close_button)
        root.addLayout(button_row)

        # ビューポートの幅が確定するのは**この窓の resizeEvent より後**である。
        # 窓側だけを見ていると最初の 1 回が組み立て中の幅 (= 最小幅) で止まるため、
        # ビューポート自身の大きさの変化を拾って貼り直す。
        self._scroll.viewport().installEventFilter(self)
        self._relayout_pages()

    # 説明画像 1 枚ぶんのウィジェットを作る (§3.5)
    # 読めなければ案内文のラベルへ置き換え、画面は開けるようにする。
    def _build_page(self, name):
        path = resolve_image_path(name)
        if path is None:
            _logger.warning("説明画像が見つかりません: %r", name)
            return self._missing_label(name)
        try:
            pixmap = QPixmap(path)
        except Exception:  # noqa: BLE001 (読めなくても画面は開く)
            _logger.exception("説明画像の読み込みに失敗しました: %s", path)
            return self._missing_label(name)
        if pixmap.isNull():
            _logger.warning("説明画像を読み込めませんでした: %s", path)
            return self._missing_label(name)
        page = _HelpPage(
            pixmap, self._cfg.get("image_min_width_px", 640), parent=self)
        self._pages.append(page)
        return page

    # 読めなかったページの代わりに出す案内文
    def _missing_label(self, name):
        label = QLabel(tr("説明画像が見つかりません: {name}", name=str(name or "")))
        label.setObjectName("noteLabel")
        label.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        label.setWordWrap(True)
        return label

    # ビューポートの大きさが変わったら各ページを貼り直す (§3.3)
    def eventFilter(self, obj, event):
        if (event.type() == QEvent.Resize
                and obj is self._scroll.viewport()):
            self._relayout_pages()
        return super().eventFilter(obj, event)

    # ビューポート幅から左右の余白を除いた幅を各ページへ配る。
    # 貼り直すとスクロールバーの有無が変わってビューポートも動くため、
    # 入れ子で呼ばれても 1 回で済むように印で守る。
    def _relayout_pages(self):
        if not self._pages or self._relayouting:
            return
        self._relayouting = True
        try:
            width = self._scroll.viewport().width() - self._margin_px * 2
            for page in self._pages:
                page.apply_width(width)
        finally:
            self._relayouting = False

    # 並んでいる説明ページ (テスト用 / §6 T3)
    def pages(self):
        return list(self._pages)
