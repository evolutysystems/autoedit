# アーカイブ採点結果の簡易確認ダイアログ (flow17 R1)
# TOP5 の区間・点数を一覧表示し、使用可否を選ばせる。「完了」で切り抜き+焼き込みへ進む。
# R2 で採点グラフ・字幕修正・プレビューを備えた本画面 (ArchiveResultWindow) に発展させる。
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

# 既存の時間整形を再利用し表記を揃える
from ..modules.subtitle_generator import _format_ass_time

# 列定義
_COL_USE = 0
_COL_RANK = 1
_COL_RANGE = 2
_COL_SCORE = 3
_COL_DETAIL = 4
_HEADERS = ["使用", "順位", "区間", "点数", "内訳(感情/コメント)"]


# 「H:MM:SS.cc → H:MM:SS.cc」形式の区間表示
def _format_range(start, end):
    return f"{_format_ass_time(start)} → {_format_ass_time(end)}"


# 採点結果ダイアログ
# clips: [{"index","start","end","score","emotion","comment","use"}]
class ArchiveResultDialog(QDialog):

    def __init__(self, clips, parent=None):
        super().__init__(parent)
        self.setWindowTitle("採点結果 (TOP5)")
        self.resize(560, 360)
        self._clips = [dict(c) for c in clips]
        self._build_ui()
        self._populate()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.addWidget(QLabel(
            "採点上位の切り抜き候補です。使用する候補にチェックを入れ、\n"
            "「完了」で切り抜きと字幕焼き込みを開始します。"
        ))

        self.table = QTableWidget(self)
        self.table.setColumnCount(len(_HEADERS))
        self.table.setHorizontalHeaderLabels(_HEADERS)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(_COL_USE, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(_COL_RANK, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(_COL_RANGE, QHeaderView.Stretch)
        header.setSectionResizeMode(_COL_SCORE, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(_COL_DETAIL, QHeaderView.ResizeToContents)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        root.addWidget(self.table)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self.decide_button = QPushButton("完了")
        self.decide_button.setDefault(True)
        self.decide_button.clicked.connect(self.accept)
        self.cancel_button = QPushButton("キャンセル")
        self.cancel_button.clicked.connect(self.reject)
        button_row.addWidget(self.decide_button)
        button_row.addWidget(self.cancel_button)
        root.addLayout(button_row)

    def _populate(self):
        self.table.setRowCount(len(self._clips))
        for row, clip in enumerate(self._clips):
            # 使用チェック (初期は use)
            use_item = QTableWidgetItem()
            use_item.setFlags((use_item.flags() | Qt.ItemIsUserCheckable) & ~Qt.ItemIsEditable)
            use_item.setCheckState(Qt.Checked if clip.get("use", True) else Qt.Unchecked)
            use_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, _COL_USE, use_item)

            rank = QTableWidgetItem(str(clip.get("index", row + 1)))
            rank.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, _COL_RANK, rank)

            self.table.setItem(row, _COL_RANGE,
                               QTableWidgetItem(_format_range(clip["start"], clip["end"])))

            score = QTableWidgetItem(f"{clip.get('score', 0):.1f}")
            score.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, _COL_SCORE, score)

            detail = QTableWidgetItem(f"{clip.get('emotion', 0):.0f} / {clip.get('comment', 0):.0f}")
            detail.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, _COL_DETAIL, detail)

    # 使用可否を反映したクリップ一覧を返す
    def result_clips(self):
        results = []
        for row, clip in enumerate(self._clips):
            use_item = self.table.item(row, _COL_USE)
            use = use_item is not None and use_item.checkState() == Qt.Checked
            out = dict(clip)
            out["use"] = use
            results.append(out)
        return results
