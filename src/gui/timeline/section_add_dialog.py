# セクション追加の区間指定ダイアログ (docs/request/ver3/resolve13.md §5.3)
#
# 元動画 (VOD) の区間を H:MM:SS で指定する。採点グラフを見ながら数値を決められるよう、
# 初期値には「グラフで最後に選んだ位置」や現在の再生ヘッド位置を渡せる。
#
# 既存セクションと重なる場合は 1 つへ統合される (resolve13 §3-4)。
# 破壊的に見える挙動のため、追加を押す前に統合後の区間を画面へ出す。
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from ...utils.logger import get_logger

_logger = get_logger(__name__)


# 秒を "H:MM:SS" へ整形する (archive_timeline_dialog._fmt と同じ表記)
def format_hms(seconds):
    total = int(max(float(seconds or 0.0), 0.0))
    return f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"


# "H:MM:SS" / "MM:SS" / 秒数 のいずれかを秒へ直す。読めなければ None。
# 利用者がどの書き方をしても受けるため、区切りの数で解釈を変える。
def parse_hms(text):
    raw = str(text or "").strip()
    if not raw:
        return None
    parts = raw.split(":")
    try:
        values = [float(p) for p in parts]
    except ValueError:
        return None
    if any(v < 0 for v in values):
        return None
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return values[0] * 60.0 + values[1]
    if len(values) == 3:
        return values[0] * 3600.0 + values[1] * 60.0 + values[2]
    return None


class SectionAddDialog(QDialog):

    # vod_duration : 元動画の全長 (秒)。0/None なら上限チェックをしない
    # default_start: 開始の初期値 (秒)
    # cfg          : archive.config.section_add_config の戻り値
    # preview_cb   : callable(開始, 終了) -> 案内文字列。統合の予告に使う
    def __init__(self, vod_duration, default_start, cfg, preview_cb=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("セクションの追加")
        self._duration = float(vod_duration or 0.0)
        self._cfg = cfg
        self._preview_cb = preview_cb
        self._start = 0.0
        self._end = 0.0

        start = max(float(default_start or 0.0), 0.0)
        end = start + float(cfg["default_length_sec"])
        if self._duration > 0:
            start = min(start, self._duration)
            end = min(end, self._duration)
        self._build_ui(start, end)
        self._on_changed()

    def _build_ui(self, start, end):
        root = QVBoxLayout(self)

        if self._duration > 0:
            head = f"元動画の区間を指定してください（全長 {format_hms(self._duration)}）"
        else:
            head = "元動画の区間を指定してください"
        root.addWidget(QLabel(head))

        form = QFormLayout()
        self.start_edit = QLineEdit(format_hms(start))
        self.start_edit.setToolTip("H:MM:SS / MM:SS / 秒数 のいずれでも入力できます")
        self.end_edit = QLineEdit(format_hms(end))
        self.end_edit.setToolTip(self.start_edit.toolTip())
        self.start_edit.textChanged.connect(self._on_changed)
        self.end_edit.textChanged.connect(self._on_changed)
        form.addRow("開始", self.start_edit)
        form.addRow("終了", self.end_edit)
        root.addLayout(form)

        # 長さと、統合が起きる場合の予告をここへ出す
        self.info_label = QLabel("")
        self.info_label.setWordWrap(True)
        root.addWidget(self.info_label)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=self)
        self.buttons.button(QDialogButtonBox.Ok).setText("追加")
        self.buttons.button(QDialogButtonBox.Cancel).setText("キャンセル")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)

    # 入力のたびに検証し、案内と「追加」ボタンの可否を更新する
    def _on_changed(self):
        start = parse_hms(self.start_edit.text())
        end = parse_hms(self.end_edit.text())
        message, ok = self._validate(start, end)
        if ok:
            self._start, self._end = start, end
            extra = self._preview_cb(start, end) if self._preview_cb else ""
            if extra:
                message = f"{message}\n{extra}"
        self.info_label.setText(message)
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(ok)

    # 戻り値: (案内文字列, 追加できるか)
    def _validate(self, start, end):
        if start is None or end is None:
            return "開始・終了は H:MM:SS / MM:SS / 秒数 で入力してください", False
        if end <= start:
            return "終了は開始より後にしてください", False
        minimum = float(self._cfg["min_length_sec"])
        if end - start < minimum:
            return f"区間が短すぎます（{minimum:g} 秒以上にしてください）", False
        if self._duration > 0 and end > self._duration:
            return f"終了が元動画の全長（{format_hms(self._duration)}）を超えています", False
        return f"長さ {format_hms(end - start)}", True

    # 指定された区間 (開始, 終了) を秒で返す
    def selected_range(self):
        return self._start, self._end
